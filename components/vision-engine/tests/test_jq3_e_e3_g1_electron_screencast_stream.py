"""Acceptance test suite for jq3-e-e3-g1-electron-screencast-stream (G-E3-1).

Covers:
- INV-E3-1: Declare and validate layout before hashing or pixel buffer access.
- INV-E3-2: Separate raw_sha256 from normalized_sha256; remove padding and
  reorder only as explicitly declared across asymmetric top/bottom pixels,
  RGB vs BGR sentinels, odd widths, and meaningful alpha policies.
- INV-E3-3: Capture transaction freezes presentation changes, reads pixels and
  tree, rechecks revision, and releases memory on success, cancellation, or
  timeout; unknown compositor residency or revision evidence keeps live path
  BLOCKED.
- INV-E3-4: Forbid persistent pixels on disk or in receipts; consume JQ2-B
  WindowStreamBank without creating a second stream.
- Mutant kill: M-vadapt-window-stream-bank-1 in WindowStreamBank._staging via
  real production paths in an in-memory AST mutant harness with positive
  control, unique qualified anchors, package-compatible compilation, and
  finally cleanup.
- Property loop: 64 seeded stdlib random.Random cases with independent pure-Python
  oracle and replayable seed/case diagnostics.
- Concurrency: 32 recorded logical schedules using threads, barriers (2.0s),
  Events, bounded joins (2.0s), at most 4 threads, barrier cleanup, and zero
  leaked threads or memory.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import os
import random
import struct
import sys
import threading
import time
import types
import unittest

import numpy as np

from vadapt import jq3_chromium_pixels as cpx
from vadapt import window_stream_bank as wsb
from vadapt import xfb

VM_BUDGET_SECONDS = 15.0
PROPERTY_TARGET_CASES = 64
CONCURRENCY_TARGET_SCHEDULES = 32
MAX_CONCURRENCY_THREADS = 4
BARRIER_TIMEOUT_SECONDS = 2.0
JOIN_TIMEOUT_SECONDS = 2.0
MASTER_SEED = 20261020


def _build_xwd_ram(
    width: int, height: int, rgb: np.ndarray, *, pad: int = 0
) -> bytes:
    """Build an in-RAM XWD buffer from a canonical (H, W, 3) uint8 RGB array."""
    name = b"Xvfb main window\x00"
    hs = xfb.MIN_HEADER + len(name)
    bpl = width * 4 + pad
    head = (
        struct.pack(
            ">25I",
            hs,
            xfb.XWD_VERSION,
            xfb.ZPIXMAP,
            24,
            width,
            height,
            0,
            0,
            32,
            0,
            32,
            32,
            bpl,
            xfb.TRUECOLOR,
            0xFF0000,
            0x00FF00,
            0x0000FF,
            8,
            0,
            0,
            width,
            height,
            0,
            0,
            0,
        )
        + name
    )
    body = bytearray()
    for y in range(height):
        row = bytearray(bpl)
        for x in range(width):
            r, g, b = int(rgb[y, x, 0]), int(rgb[y, x, 1]), int(rgb[y, x, 2])
            row[x * 4 : x * 4 + 4] = bytes((b, g, r, 0))
        for p in range(width * 4, bpl):
            row[p] = (y * 17 + p * 31 + 9) & 0xFF
        body.extend(row)
    return head + bytes(body)


def _pure_python_oracle(
    raw_bytes: bytes,
    width: int,
    height: int,
    channel_order: str,
    stride_bytes: int,
    orientation: str,
    alpha_policy: str,
) -> tuple[str, str, bytes]:
    """Independently compute (raw_sha256, normalized_sha256, rgb24_bytes) in pure Python."""
    raw_sha = hashlib.sha256(raw_bytes).hexdigest()
    bpp = 3 if channel_order in ("RGB", "BGR") else 4
    out = bytearray(width * height * 3)

    for out_y in range(height):
        src_y = (height - 1 - out_y) if orientation == "bottom_up" else out_y
        row_start = src_y * stride_bytes
        for x in range(width):
            px_off = row_start + x * bpp
            if channel_order == "RGB":
                r, g, b, a = (
                    raw_bytes[px_off],
                    raw_bytes[px_off + 1],
                    raw_bytes[px_off + 2],
                    255,
                )
            elif channel_order == "BGR":
                b, g, r, a = (
                    raw_bytes[px_off],
                    raw_bytes[px_off + 1],
                    raw_bytes[px_off + 2],
                    255,
                )
            elif channel_order == "RGBA":
                r, g, b, a = (
                    raw_bytes[px_off],
                    raw_bytes[px_off + 1],
                    raw_bytes[px_off + 2],
                    raw_bytes[px_off + 3],
                )
            elif channel_order == "BGRA":
                b, g, r, a = (
                    raw_bytes[px_off],
                    raw_bytes[px_off + 1],
                    raw_bytes[px_off + 2],
                    raw_bytes[px_off + 3],
                )
            elif channel_order == "ARGB":
                a, r, g, b = (
                    raw_bytes[px_off],
                    raw_bytes[px_off + 1],
                    raw_bytes[px_off + 2],
                    raw_bytes[px_off + 3],
                )
            elif channel_order == "ABGR":
                a, b, g, r = (
                    raw_bytes[px_off],
                    raw_bytes[px_off + 1],
                    raw_bytes[px_off + 2],
                    raw_bytes[px_off + 3],
                )
            elif channel_order == "RGBX":
                r, g, b, a = (
                    raw_bytes[px_off],
                    raw_bytes[px_off + 1],
                    raw_bytes[px_off + 2],
                    255,
                )
            elif channel_order == "BGRX":
                b, g, r, a = (
                    raw_bytes[px_off],
                    raw_bytes[px_off + 1],
                    raw_bytes[px_off + 2],
                    255,
                )
            elif channel_order == "XRGB":
                r, g, b, a = (
                    raw_bytes[px_off + 1],
                    raw_bytes[px_off + 2],
                    raw_bytes[px_off + 3],
                    255,
                )
            elif channel_order == "XBGR":
                b, g, r, a = (
                    raw_bytes[px_off + 1],
                    raw_bytes[px_off + 2],
                    raw_bytes[px_off + 3],
                    255,
                )
            else:
                raise AssertionError(f"unexpected channel_order {channel_order}")

            if channel_order in ("RGBA", "BGRA", "ARGB", "ABGR"):
                if alpha_policy == "unpremultiplied":
                    r = (r * a + 127) // 255
                    g = (g * a + 127) // 255
                    b = (b * a + 127) // 255
                elif alpha_policy == "premultiplied":
                    if a == 0:
                        r, g, b = 0, 0, 0
                    else:
                        r = (r * 255 + (a // 2)) // a
                        g = (g * 255 + (a // 2)) // a
                        b = (b * 255 + (a // 2)) // a

            dst_off = (out_y * width + x) * 3
            out[dst_off] = r
            out[dst_off + 1] = g
            out[dst_off + 2] = b

    norm_bytes = bytes(out)
    norm_sha = hashlib.sha256(norm_bytes).hexdigest()
    return raw_sha, norm_sha, norm_bytes


class _QualifiedAstMutator(ast.NodeTransformer):
    """In-memory AST mutator anchored on a unique qualified Class.method or top-level function."""

    def __init__(self, qualified_anchor: str, transform_fn):
        parts = qualified_anchor.split(".")
        if len(parts) == 1:
            self.target_class = None
            self.target_func = parts[0]
        elif len(parts) == 2:
            self.target_class, self.target_func = parts
        else:
            raise ValueError(f"invalid qualified anchor {qualified_anchor!r}")
        self.transform_fn = transform_fn
        self._current_class: str | None = None
        self.anchor_matches = 0
        self.site_mutations = 0

    def visit_ClassDef(self, node: ast.ClassDef):
        prev = self._current_class
        self._current_class = node.name
        try:
            return self.generic_visit(node)
        finally:
            self._current_class = prev

    def visit_FunctionDef(self, node: ast.FunctionDef):
        if (
            self._current_class == self.target_class
            and node.name == self.target_func
        ):
            self.anchor_matches += 1
            cloned = copy.deepcopy(node)
            mutated, count = self.transform_fn(cloned)
            self.site_mutations += count
            return mutated
        return self.generic_visit(node)


def _compile_in_memory_mutant(
    module_obj: types.ModuleType,
    qualified_anchor: str,
    transform_fn,
) -> types.ModuleType:
    """Parse, mutate, and compile a module in memory without touching disk."""
    source_path = os.path.abspath(module_obj.__file__)
    with open(source_path, "r", encoding="utf-8") as fh:
        source_text = fh.read()
    tree = ast.parse(source_text, filename=source_path)
    mutator = _QualifiedAstMutator(qualified_anchor, transform_fn)
    mutated_tree = mutator.visit(tree)
    ast.fix_missing_locations(mutated_tree)
    if mutator.anchor_matches != 1:
        raise RuntimeError(
            f"expected unique anchor {qualified_anchor}, matched {mutator.anchor_matches}"
        )
    if mutator.site_mutations != 1:
        raise RuntimeError(
            f"expected unique mutation site in {qualified_anchor}, mutated {mutator.site_mutations}"
        )
    code_obj = compile(mutated_tree, filename=source_path, mode="exec")
    mutant_mod = types.ModuleType(module_obj.__name__)
    mutant_mod.__file__ = source_path
    mutant_mod.__package__ = module_obj.__package__
    exec(code_obj, mutant_mod.__dict__)
    return mutant_mod


def _disable_raw_is_none_in_staging(
    func_node: ast.FunctionDef,
) -> tuple[ast.FunctionDef, int]:
    """Mutant M-vadapt-window-stream-bank-1: disable `if raw is None:` rejection in WindowStreamBank._staging."""
    count = 0

    class _DropRawIsNone(ast.NodeTransformer):

        def visit_If(self, node: ast.If):
            nonlocal count
            if (
                isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "raw"
                and len(node.test.ops) == 1
                and isinstance(node.test.ops[0], ast.Is)
                and len(node.test.comparators) == 1
                and isinstance(node.test.comparators[0], ast.Constant)
                and node.test.comparators[0].value is None
            ):
                count += 1
                node.test = ast.Constant(value=False)
                return node
            return self.generic_visit(node)

    return _DropRawIsNone().visit(func_node), count


def _drop_revision_recheck_in_capture(
    func_node: ast.FunctionDef,
) -> tuple[ast.FunctionDef, int]:
    """Owned mutant: disable revision recheck in execute_capture_transaction."""
    count = 0

    class _DropRevCheck(ast.NodeTransformer):

        def visit_If(self, node: ast.If):
            nonlocal count
            for child in ast.walk(node.test):
                if isinstance(child, ast.Name) and child.id == "rev_after":
                    count += 1
                    node.test = ast.Constant(value=False)
                    return node
            return self.generic_visit(node)

    return _DropRevCheck().visit(func_node), count


def _drop_bottom_up_flip_in_normalize(
    func_node: ast.FunctionDef,
) -> tuple[ast.FunctionDef, int]:
    """Owned mutant: disable bottom_up vertical flip in normalize_chromium_pixels."""
    count = 0

    class _DropFlip(ast.NodeTransformer):

        def visit_If(self, node: ast.If):
            nonlocal count
            if (
                isinstance(node.test, ast.Compare)
                and len(node.test.comparators) == 1
                and isinstance(node.test.comparators[0], ast.Constant)
                and node.test.comparators[0].value == "bottom_up"
            ):
                count += 1
                node.test = ast.Constant(value=False)
                return node
            return self.generic_visit(node)

    return _DropFlip().visit(func_node), count


class TestJq3ChromiumPixelsAcceptance(unittest.TestCase):
    """Acceptance gate G-E3-1 test suite for vadapt.jq3_chromium_pixels."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._vm_suite_start = time.perf_counter()

    @classmethod
    def tearDownClass(cls) -> None:
        vm_elapsed = time.perf_counter() - cls._vm_suite_start
        if vm_elapsed >= VM_BUDGET_SECONDS:
            raise AssertionError(
                f"VM suite elapsed {vm_elapsed:.3f}s exceeded VM budget {VM_BUDGET_SECONDS:.1f}s"
            )

    def test_inv_e3_1_layout_validated_before_hashing(self) -> None:
        """INV-E3-1: Validate all 10 layout fields before touching the pixel buffer or hashing."""
        vm_t0 = time.perf_counter()

        class _PoisonBuffer:

            def __bytes__(self):
                raise AssertionError(
                    "buffer should not be accessed when layout declaration is invalid"
                )

        poison = _PoisonBuffer()
        base = {
            "width": 5,
            "height": 4,
            "channel_order": "BGRA",
            "stride_bytes": 24,
            "orientation": "top_down",
            "alpha_policy": "opaque",
            "color_space": "sRGB",
            "dpr_ppm": 1_000_000,
            "zoom_ppm": 1_000_000,
            "environment_id": "env-valid-01",
        }
        invalid_mutations = [
            ("width", 0, "E_LAYOUT_DIMENSIONS"),
            ("width", True, "E_LAYOUT_DIMENSIONS"),
            ("height", -1, "E_LAYOUT_DIMENSIONS"),
            ("channel_order", "CMYK", "E_LAYOUT_CHANNEL_ORDER"),
            ("stride_bytes", 19, "E_LAYOUT_STRIDE"),
            ("orientation", "sideways", "E_LAYOUT_ORIENTATION"),
            ("alpha_policy", "invalid_alpha", "E_LAYOUT_ALPHA_POLICY"),
            ("color_space", "display-p3", "E_LAYOUT_COLOR_SPACE"),
            ("dpr_ppm", 0, "E_LAYOUT_DPR"),
            ("zoom_ppm", 99_999, "E_LAYOUT_ZOOM"),
            ("environment_id", "bad env id!", "E_LAYOUT_ENVIRONMENT"),
        ]
        for field_name, bad_val, expected_code in invalid_mutations:
            bad_decl = dict(base)
            bad_decl[field_name] = bad_val
            with self.assertRaises(
                cpx.ChromiumPixelError, msg=f"field={field_name}"
            ) as cm:
                cpx.normalize_chromium_pixels(poison, bad_decl)
            self.assertEqual(cm.exception.code, expected_code)

        vm_dt = time.perf_counter() - vm_t0
        self.assertLess(vm_dt, VM_BUDGET_SECONDS, f"VM test timing {vm_dt:.3f}s")

    def test_inv_e3_2_digest_separation_sentinels_odd_width_and_alpha(
        self,
    ) -> None:
        """INV-E3-2: Separate raw_sha256 and normalized_sha256 across odd widths, sentinels, and alpha."""
        w, h = 7, 5  # odd width
        rgb = cpx.build_sentinel_rgb(w, h, seed=13)
        # Verify asymmetric top/bottom and RGB/BGR sentinels
        self.assertEqual(tuple(rgb[0, 0]), (250, 40, 10))
        self.assertEqual(tuple(rgb[h - 1, 0]), (20, 85, 245))
        self.assertNotEqual(tuple(rgb[0, 0]), tuple(rgb[h - 1, 0]))
        self.assertNotEqual(int(rgb[0, 0, 0]), int(rgb[0, 0, 2]))

        decl_padded_bgra = cpx.PixelLayoutDeclaration(
            width=w,
            height=h,
            channel_order="BGRA",
            stride_bytes=w * 4 + 9,
            orientation="bottom_up",
            alpha_policy="opaque",
            color_space="sRGB",
            dpr_ppm=1_500_000,
            zoom_ppm=1_000_000,
            environment_id="env-odd-width",
        )
        raw_pad_a = cpx.encode_raw_chromium_buffer(
            rgb, decl_padded_bgra, pad_seed=0x11
        )
        raw_pad_b = cpx.encode_raw_chromium_buffer(
            rgb, decl_padded_bgra, pad_seed=0xEE
        )

        norm_a = cpx.normalize_chromium_pixels(raw_pad_a, decl_padded_bgra)
        norm_b = cpx.normalize_chromium_pixels(raw_pad_b, decl_padded_bgra)

        # Raw digests differ because row-stride padding differs; normalized digests match.
        self.assertNotEqual(norm_a.raw_sha256, norm_b.raw_sha256)
        self.assertEqual(norm_a.normalized_sha256, norm_b.normalized_sha256)
        self.assertNotEqual(norm_a.raw_sha256, norm_a.normalized_sha256)
        self.assertTrue(np.array_equal(norm_a.rgb, rgb))
        self.assertFalse(norm_a.rgb.flags.writeable)

        # Meaningful alpha: unpremultiplied vs discard vs require_opaque
        alpha_plane = np.full((h, w), 128, dtype=np.uint8)
        alpha_plane[0, 0] = 64
        decl_unpremult = cpx.PixelLayoutDeclaration(
            width=w,
            height=h,
            channel_order="RGBA",
            stride_bytes=w * 4 + 4,
            orientation="top_down",
            alpha_policy="unpremultiplied",
            color_space="sRGB",
            dpr_ppm=1_000_000,
            zoom_ppm=1_000_000,
            environment_id="env-alpha",
        )
        decl_discard = cpx.PixelLayoutDeclaration(
            width=w,
            height=h,
            channel_order="RGBA",
            stride_bytes=w * 4 + 4,
            orientation="top_down",
            alpha_policy="discard",
            color_space="sRGB",
            dpr_ppm=1_000_000,
            zoom_ppm=1_000_000,
            environment_id="env-alpha",
        )
        decl_opaque = cpx.PixelLayoutDeclaration(
            width=w,
            height=h,
            channel_order="RGBA",
            stride_bytes=w * 4 + 4,
            orientation="top_down",
            alpha_policy="require_opaque",
            color_space="sRGB",
            dpr_ppm=1_000_000,
            zoom_ppm=1_000_000,
            environment_id="env-alpha",
        )
        raw_rgba = cpx.encode_raw_chromium_buffer(
            rgb, decl_unpremult, alpha_plane=alpha_plane, pad_seed=0x42
        )
        norm_unpremult = cpx.normalize_chromium_pixels(
            raw_rgba, decl_unpremult
        )
        norm_discard = cpx.normalize_chromium_pixels(raw_rgba, decl_discard)

        self.assertEqual(norm_unpremult.raw_sha256, norm_discard.raw_sha256)
        self.assertNotEqual(
            norm_unpremult.normalized_sha256, norm_discard.normalized_sha256
        )
        self.assertEqual(
            int(norm_unpremult.rgb[0, 0, 0]), (250 * 64 + 127) // 255
        )

        with self.assertRaises(cpx.ChromiumPixelError) as cm:
            cpx.normalize_chromium_pixels(raw_rgba, decl_opaque)
        self.assertEqual(cm.exception.code, "E_ALPHA_NOT_OPAQUE")

    def test_positive_control_and_negative_contrasts_with_xfb_pair(
        self,
    ) -> None:
        """Positive control: declared matching exact RAM pair; negative contrast: stale revision or wrong declaration."""
        w, h = 9, 6  # odd width
        rgb = cpx.build_sentinel_rgb(w, h, seed=21)

        # Reference leg from XWD RAM buffer decoded through vadapt.xfb.rgb24
        xwd_buf = _build_xwd_ram(w, h, rgb, pad=8)
        xwd_rgb_bytes = xfb.rgb24(xwd_buf)
        self.assertEqual(
            hashlib.sha256(xwd_rgb_bytes).hexdigest(),
            xfb.frame_sha256(xwd_buf),
        )

        decl_xwd_rgb = cpx.PixelLayoutDeclaration(
            width=w,
            height=h,
            channel_order="RGB",
            stride_bytes=w * 3,
            orientation="top_down",
            alpha_policy="none",
            color_space="sRGB",
            dpr_ppm=2_000_000,
            zoom_ppm=1_000_000,
            environment_id="env-pair-check",
        )
        decl_chromium = cpx.PixelLayoutDeclaration(
            width=w,
            height=h,
            channel_order="BGRA",
            stride_bytes=w * 4 + 12,
            orientation="bottom_up",
            alpha_policy="opaque",
            color_space="sRGB",
            dpr_ppm=2_000_000,
            zoom_ppm=1_000_000,
            environment_id="env-pair-check",
        )
        raw_chromium = cpx.encode_raw_chromium_buffer(
            rgb, decl_chromium, pad_seed=0x77
        )

        # Positive control: exact RAM pair at matching revision 42
        report = cpx.verify_ram_pair_parity(
            raw_chromium,
            decl_chromium,
            42,
            xwd_rgb_bytes,
            decl_xwd_rgb,
            42,
            expected_decl_a=decl_chromium,
            expected_decl_b=decl_xwd_rgb,
        )
        self.assertTrue(report.matched)
        self.assertEqual(report.normalized_sha256, xfb.frame_sha256(xwd_buf))
        self.assertNotEqual(report.raw_sha256_a, report.raw_sha256_b)

        # Negative contrast 1: identical-looking pixels bound to a stale revision (41 != 42)
        with self.assertRaises(cpx.ChromiumPixelError) as cm_rev:
            cpx.verify_ram_pair_parity(
                raw_chromium,
                decl_chromium,
                41,
                xwd_rgb_bytes,
                decl_xwd_rgb,
                42,
            )
        self.assertEqual(cm_rev.exception.code, "E_REVISION_MISMATCH")

        # Negative contrast 2a: wrong channel_order declaration (RGBA instead of BGRA)
        wrong_channel_decl = cpx.PixelLayoutDeclaration(
            width=w,
            height=h,
            channel_order="RGBA",
            stride_bytes=w * 4 + 12,
            orientation="bottom_up",
            alpha_policy="opaque",
            color_space="sRGB",
            dpr_ppm=2_000_000,
            zoom_ppm=1_000_000,
            environment_id="env-pair-check",
        )
        with self.assertRaises(cpx.ChromiumPixelError) as cm_chan:
            cpx.verify_ram_pair_parity(
                raw_chromium,
                wrong_channel_decl,
                42,
                xwd_rgb_bytes,
                decl_xwd_rgb,
                42,
            )
        self.assertEqual(cm_chan.exception.code, "E_PARITY_MISMATCH")

        # Negative contrast 2b: identical-looking pre-swapped pixels with wrong declaration against expected contract
        with self.assertRaises(cpx.ChromiumPixelError) as cm_decl:
            cpx.verify_ram_pair_parity(
                xwd_rgb_bytes,
                decl_xwd_rgb,
                42,
                xwd_rgb_bytes,
                decl_xwd_rgb,
                42,
                expected_decl_a=decl_chromium,
            )
        self.assertEqual(cm_decl.exception.code, "E_DECLARATION_MISMATCH")

    def test_enumerated_behavioral_cases_positive_and_single_condition_negative(
        self,
    ) -> None:
        """Enumerate and verify each positive and single-condition negative case independently."""
        matrix = cpx.run_behavioral_case_matrix()
        expected_cases = {
            "pos_stride_odd_width_padded": ("stride", True, None),
            "neg_stride_too_small": ("stride", False, "E_LAYOUT_STRIDE"),
            "pos_channel_order_bgr_reordered": ("channel_order", True, None),
            "neg_channel_order_mismatch": (
                "channel_order",
                False,
                "E_PARITY_MISMATCH",
            ),
            "pos_orientation_bottom_up_flipped": ("orientation", True, None),
            "neg_orientation_mismatch": (
                "orientation",
                False,
                "E_PARITY_MISMATCH",
            ),
            "pos_matching_exact_ram_pair": ("revision", True, None),
            "neg_stale_revision_mismatch": (
                "revision",
                False,
                "E_REVISION_MISMATCH",
            ),
            "pos_digest_separation": ("digest_separation", True, None),
            "neg_digest_conflation_rejected": (
                "digest_separation",
                False,
                "E_STREAM_DIGEST_MISMATCH",
            ),
        }
        self.assertEqual(set(matrix.keys()), set(expected_cases.keys()))
        for case_id, (cat, is_pos, exp_code) in expected_cases.items():
            rep = matrix[case_id]
            self.assertTrue(rep.passed, f"case {case_id} failed: {rep}")
            self.assertEqual(rep.category, cat)
            self.assertEqual(rep.positive, is_pos)
            self.assertEqual(rep.error_code, exp_code)

    def test_inv_e3_3_and_inv_e3_4_capture_transaction_and_single_stream(
        self,
    ) -> None:
        """INV-E3-3 & INV-E3-4: Transaction freezes presentation, rechecks revision, releases RAM, and reuses JQ2-B stream."""
        w, h, slots = 5, 4, 2
        rgb0 = cpx.build_sentinel_rgb(w, h, seed=1)
        rgb1 = cpx.build_sentinel_rgb(w, h, seed=2)
        decl = cpx.PixelLayoutDeclaration(
            width=w,
            height=h,
            channel_order="BGRA",
            stride_bytes=w * 4 + 8,
            orientation="bottom_up",
            alpha_policy="opaque",
            color_space="sRGB",
            dpr_ppm=1_000_000,
            zoom_ppm=1_000_000,
            environment_id="env-tx-1",
        )
        raw0 = cpx.encode_raw_chromium_buffer(rgb0, decl, pad_seed=0x21)
        raw1 = cpx.encode_raw_chromium_buffer(rgb1, decl, pad_seed=0x54)
        tree0 = cpx.PresentationTreeSnapshot(
            revision=10,
            environment_id="env-tx-1",
            width=w,
            height=h,
            nodes=({"role": "RootWebArea", "id": 1},),
        )
        tree1 = cpx.PresentationTreeSnapshot(
            revision=11,
            environment_id="env-tx-1",
            width=w,
            height=h,
            nodes=({"role": "RootWebArea", "id": 1, "updated": True},),
        )
        att0 = cpx.CompositorAttestation(
            residency="ram_verified",
            revision_evidence="cdp-frame-tree-rev-10",
            presentation_revision=10,
            environment_id="env-tx-1",
        )
        producer = cpx.FakeChromiumProducer(
            declaration=decl, raw_bytes=raw0, tree=tree0, attestation=att0
        )

        ring_size = wsb.rtring.ring_bytes(slots, w, h)
        bank = wsb.WindowStreamBank(quota_bytes=2 * ring_size)
        handle = wsb.Handle("cdp_tab", "electron-main-tab", 1)
        spec = wsb.StreamSpec(
            width=w, height=h, slots=slots, quota_bytes=ring_size
        )
        adapter = cpx.ChromiumStreamAdapter(bank, handle, spec)

        # Stage a concurrent presentation update while frozen during capture
        def hook(stage: str) -> None:
            if stage == "after_pixels":
                applied = producer.stage_presentation_update(
                    11, raw1, decl, tree1
                )
                self.assertFalse(
                    applied,
                    "update must be deferred while presentation is frozen",
                )

        res0 = cpx.execute_capture_transaction(
            producer,
            stream_adapter=adapter,
            frame_seq=0,
            step_hook=hook,
        )
        self.assertEqual(res0.receipt.state, "COMMITTED")
        self.assertTrue(res0.receipt.memory_released)
        self.assertEqual(res0.receipt.presentation_revision, 10)
        self.assertEqual(res0.receipt.tree_revision, 10)
        self.assertIsNotNone(res0.frame_ref)
        self.assertEqual(
            res0.frame_ref.digest, res0.normalized.normalized_sha256
        )
        self.assertTrue(all(lease.released for lease in producer.issued_leases))
        self.assertFalse(producer.frozen)
        # After unfreeze, deferred revision 11 is now active
        self.assertEqual(producer.current_revision(), 11)

        # Second capture reuses the exact same JQ2-B stream without opening a second stream
        res1 = cpx.execute_capture_transaction(
            producer,
            stream_adapter=adapter,
            frame_seq=1,
        )
        self.assertEqual(res1.receipt.presentation_revision, 11)
        self.assertEqual(bank.reserved_bytes, ring_size)
        with self.assertRaises(cpx.ChromiumPixelError) as cm_dup:
            adapter.forbid_second_stream()
        self.assertEqual(cm_dup.exception.code, "E_SECOND_STREAM_FORBIDDEN")

        # Unknown compositor residency or revision evidence keeps live path BLOCKED
        for bad_att in (
            cpx.CompositorAttestation("unknown", "rev-ev", 11, "env-tx-1"),
            cpx.CompositorAttestation("disk", "rev-ev", 11, "env-tx-1"),
            cpx.CompositorAttestation("ram_verified", "unknown", 11, "env-tx-1"),
            cpx.CompositorAttestation("ram_verified", "", 11, "env-tx-1"),
            cpx.CompositorAttestation("ram_verified", "rev-ev", None, "env-tx-1"),
        ):
            blocked_prod = cpx.FakeChromiumProducer(
                declaration=decl,
                raw_bytes=raw1,
                tree=tree1,
                attestation=bad_att,
            )
            with self.assertRaises(cpx.ChromiumPixelError) as cm_blk:
                cpx.execute_capture_transaction(blocked_prod)
            self.assertEqual(cm_blk.exception.code, "E_LIVE_BLOCKED")
            self.assertEqual(cm_blk.exception.state, "BLOCKED")
            self.assertTrue(
                all(lease.released for lease in blocked_prod.issued_leases)
            )

        # Cancellation and timeout release memory and unfreeze
        cancel_ev = threading.Event()
        with self.assertRaises(cpx.ChromiumPixelError) as cm_cancel:
            cpx.execute_capture_transaction(
                producer,
                step_hook=lambda st: cancel_ev.set()
                if st == "after_tree"
                else None,
                cancel_event=cancel_ev,
            )
        self.assertEqual(cm_cancel.exception.code, "E_CANCELLED")
        self.assertTrue(all(lease.released for lease in producer.issued_leases))
        self.assertFalse(producer.frozen)

        fake_now = [1_000_000]

        def advance_on_tree(stage: str) -> None:
            if stage == "after_tree":
                fake_now[0] += 5_000_000_000

        with self.assertRaises(cpx.ChromiumPixelError) as cm_timeout:
            cpx.execute_capture_transaction(
                producer,
                clock=lambda: fake_now[0],
                timeout_ns=1_000_000_000,
                step_hook=advance_on_tree,
            )
        self.assertEqual(cm_timeout.exception.code, "E_TIMEOUT")
        self.assertTrue(all(lease.released for lease in producer.issued_leases))
        self.assertFalse(producer.frozen)

        adapter.close()
        self.assertEqual(bank.reserved_bytes, 0)

    def test_kill_mutant_m_vadapt_window_stream_bank_1_and_owned_mutants(
        self,
    ) -> None:
        """Kill M-vadapt-window-stream-bank-1 and owned AST mutants via semantic AssertionError."""
        w, h, slots = 5, 4, 2
        ring_size = wsb.rtring.ring_bytes(slots, w, h)
        rgb = cpx.build_sentinel_rgb(w, h, seed=9)
        decl = cpx.PixelLayoutDeclaration(
            width=w,
            height=h,
            channel_order="BGRA",
            stride_bytes=w * 4 + 4,
            orientation="bottom_up",
            alpha_policy="opaque",
            color_space="sRGB",
            dpr_ppm=1_000_000,
            zoom_ppm=1_000_000,
            environment_id="env-mutant-check",
        )
        raw = cpx.encode_raw_chromium_buffer(rgb, decl)
        tree = cpx.PresentationTreeSnapshot(
            revision=5, environment_id="env-mutant-check", width=w, height=h
        )
        att = cpx.CompositorAttestation(
            residency="ram_verified",
            revision_evidence="cdp-rev-5",
            presentation_revision=5,
            environment_id="env-mutant-check",
        )

        def probe_missing_staging_bytes(wsb_mod: types.ModuleType) -> None:
            """Exercise ChromiumStreamAdapter + WindowStreamBank._staging when ring_factory buffer is None."""
            orig_wsb = cpx.window_stream_bank
            cpx.window_stream_bank = wsb_mod
            try:

                class _NullBufferOwner:

                    def __init__(self):
                        self.buffer = None

                    def close(self):
                        return False

                bank = wsb_mod.WindowStreamBank(
                    quota_bytes=2 * ring_size,
                    ring_factory=lambda handle, spec: _NullBufferOwner(),
                )
                handle = wsb_mod.Handle("cdp_tab", "mutant-tab", 1)
                spec = wsb_mod.StreamSpec(
                    width=w, height=h, slots=slots, quota_bytes=ring_size
                )
                adapter = cpx.ChromiumStreamAdapter(bank, handle, spec)
                producer = cpx.FakeChromiumProducer(
                    declaration=decl, raw_bytes=raw, tree=tree, attestation=att
                )
                caught: Exception | None = None
                try:
                    cpx.execute_capture_transaction(
                        producer, stream_adapter=adapter, frame_seq=0
                    )
                except wsb_mod.StreamError as exc:
                    caught = exc
                except Exception as exc:
                    self.fail(
                        f"expected StreamError on missing staging buffer, got {type(exc).__name__}: {exc}"
                    )
                self.assertIsNotNone(
                    caught,
                    "missing staging bytes must refuse without accepting a frame",
                )
                self.assertEqual(
                    str(caught), "ring_factory returned no writable buffer"
                )
                self.assertEqual(bank.reserved_bytes, 0)
                self.assertEqual(len(bank._retained), 0)
                self.assertEqual(len(bank._storage_ids), 0)
            finally:
                cpx.window_stream_bank = orig_wsb

        # 1. Real-module positive control for M-vadapt-window-stream-bank-1
        probe_missing_staging_bytes(wsb)

        # 2. Mutated module for M-vadapt-window-stream-bank-1
        orig_sys_wsb = sys.modules.get("vadapt.window_stream_bank")
        try:
            mutant_wsb = _compile_in_memory_mutant(
                wsb,
                "WindowStreamBank._staging",
                _disable_raw_is_none_in_staging,
            )
            with self.assertRaises(
                AssertionError,
                msg="M-vadapt-window-stream-bank-1 must be killed by semantic AssertionError",
            ):
                probe_missing_staging_bytes(mutant_wsb)
        finally:
            if orig_sys_wsb is not None:
                sys.modules["vadapt.window_stream_bank"] = orig_sys_wsb
            cpx.window_stream_bank = wsb

        # 3. Owned module AST mutant 1: dropping revision recheck in execute_capture_transaction
        def probe_owned_revision_recheck(cpx_mod: types.ModuleType) -> None:
            tampered_producer = cpx_mod.FakeChromiumProducer(
                declaration=decl,
                raw_bytes=raw,
                tree=tree,
                attestation=att,
                tamper_revision_on_recheck=6,
            )
            caught_err = None
            try:
                cpx_mod.execute_capture_transaction(tampered_producer)
            except cpx_mod.ChromiumPixelError as exc:
                caught_err = exc
            self.assertIsNotNone(
                caught_err, "tampered revision on recheck must be rejected"
            )
            self.assertEqual(caught_err.code, "E_REVISION_MISMATCH")

        probe_owned_revision_recheck(cpx)
        mutant_cpx_rev = _compile_in_memory_mutant(
            cpx,
            "execute_capture_transaction",
            _drop_revision_recheck_in_capture,
        )
        with self.assertRaises(AssertionError):
            probe_owned_revision_recheck(mutant_cpx_rev)

        # 4. Owned module AST mutant 2: dropping bottom_up vertical flip in normalize_chromium_pixels
        def probe_owned_bottom_up_flip(cpx_mod: types.ModuleType) -> None:
            norm = cpx_mod.normalize_chromium_pixels(raw, decl)
            self.assertEqual(tuple(norm.rgb[0, 0]), (250, 40, 10))
            self.assertEqual(tuple(norm.rgb[h - 1, 0]), (20, 85, 245))

        probe_owned_bottom_up_flip(cpx)
        mutant_cpx_flip = _compile_in_memory_mutant(
            sys.modules[cpx.normalize_chromium_pixels.__module__],
            "normalize_chromium_pixels",
            _drop_bottom_up_flip_in_normalize,
        )
        with self.assertRaises(AssertionError):
            probe_owned_bottom_up_flip(mutant_cpx_flip)

    def test_seeded_property_loops_64_cases_with_independent_oracle(
        self,
    ) -> None:
        """Property loop: 64 seeded random cases with independent pure-Python oracle."""
        rng = random.Random(MASTER_SEED)
        completed = 0

        for case_idx in range(PROPERTY_TARGET_CASES):
            case_seed = rng.randint(1, (1 << 30) - 1)
            case_rng = random.Random(case_seed)
            # Mix odd and even widths (ensuring odd widths are heavily exercised)
            width = case_rng.choice([1, 3, 4, 5, 7, 8, 9, 11, 13, 15, 16, 19])
            height = case_rng.choice([2, 3, 4, 5, 6, 7, 9])
            channel_order = case_rng.choice(cpx.CHANNEL_ORDERS)
            bpp = 3 if channel_order in cpx.THREE_CHANNEL_ORDERS else 4
            pad = case_rng.choice([0, 1, 2, 3, 5, 7, 12, 16])
            stride_bytes = width * bpp + pad
            orientation = case_rng.choice(cpx.ORIENTATIONS)

            if channel_order in cpx.THREE_CHANNEL_ORDERS:
                alpha_policy = case_rng.choice(
                    ["none", "opaque", "require_opaque"]
                )
            elif channel_order in cpx.PAD_CHANNEL_ORDERS:
                alpha_policy = case_rng.choice(["discard", "opaque"])
            else:
                alpha_policy = case_rng.choice(
                    [
                        "opaque",
                        "require_opaque",
                        "discard",
                        "unpremultiplied",
                        "premultiplied",
                    ]
                )

            dpr_ppm = case_rng.choice(
                [1_000_000, 1_250_000, 1_500_000, 2_000_000]
            )
            zoom_ppm = case_rng.choice(
                [750_000, 1_000_000, 1_250_000, 1_500_000]
            )
            env_id = f"env-prop-{case_idx:02d}-{case_seed & 0xFFFF:04x}"

            decl = cpx.PixelLayoutDeclaration(
                width=width,
                height=height,
                channel_order=channel_order,
                stride_bytes=stride_bytes,
                orientation=orientation,
                alpha_policy=alpha_policy,
                color_space="sRGB",
                dpr_ppm=dpr_ppm,
                zoom_ppm=zoom_ppm,
                environment_id=env_id,
            )

            # Build raw buffer with valid alpha constraints for the chosen policy
            raw_buf = bytearray(stride_bytes * height)
            for y in range(height):
                row_off = y * stride_bytes
                for x in range(width):
                    px_off = row_off + x * bpp
                    if channel_order in ("RGBA", "BGRA", "ARGB", "ABGR"):
                        if alpha_policy in ("opaque", "require_opaque"):
                            a = 255
                        else:
                            a = case_rng.randint(0, 255)
                        if alpha_policy == "premultiplied":
                            r = case_rng.randint(0, a) if a > 0 else 0
                            g = case_rng.randint(0, a) if a > 0 else 0
                            b = case_rng.randint(0, a) if a > 0 else 0
                        else:
                            r = case_rng.randint(0, 255)
                            g = case_rng.randint(0, 255)
                            b = case_rng.randint(0, 255)
                    else:
                        a = case_rng.randint(0, 255)
                        r = case_rng.randint(0, 255)
                        g = case_rng.randint(0, 255)
                        b = case_rng.randint(0, 255)

                    if channel_order == "RGB":
                        raw_buf[px_off : px_off + 3] = bytes((r, g, b))
                    elif channel_order == "BGR":
                        raw_buf[px_off : px_off + 3] = bytes((b, g, r))
                    elif channel_order == "RGBA":
                        raw_buf[px_off : px_off + 4] = bytes((r, g, b, a))
                    elif channel_order == "BGRA":
                        raw_buf[px_off : px_off + 4] = bytes((b, g, r, a))
                    elif channel_order == "ARGB":
                        raw_buf[px_off : px_off + 4] = bytes((a, r, g, b))
                    elif channel_order == "ABGR":
                        raw_buf[px_off : px_off + 4] = bytes((a, b, g, r))
                    elif channel_order == "RGBX":
                        raw_buf[px_off : px_off + 4] = bytes((r, g, b, a))
                    elif channel_order == "BGRX":
                        raw_buf[px_off : px_off + 4] = bytes((b, g, r, a))
                    elif channel_order == "XRGB":
                        raw_buf[px_off : px_off + 4] = bytes((a, r, g, b))
                    elif channel_order == "XBGR":
                        raw_buf[px_off : px_off + 4] = bytes((a, b, g, r))

                for p in range(width * bpp, stride_bytes):
                    raw_buf[row_off + p] = case_rng.randint(0, 255)

            raw_bytes = bytes(raw_buf)
            exp_raw_sha, exp_norm_sha, exp_rgb_bytes = _pure_python_oracle(
                raw_bytes,
                width,
                height,
                channel_order,
                stride_bytes,
                orientation,
                alpha_policy,
            )
            diag = (
                f"replay diagnostic: master_seed={MASTER_SEED} case_idx={case_idx} "
                f"case_seed={case_seed} decl={decl}"
            )
            norm = cpx.normalize_chromium_pixels(raw_bytes, decl)
            self.assertEqual(norm.raw_sha256, exp_raw_sha, diag)
            self.assertEqual(norm.normalized_sha256, exp_norm_sha, diag)
            self.assertEqual(norm.rgb.tobytes(order="C"), exp_rgb_bytes, diag)
            self.assertEqual(norm.rgb.shape, (height, width, 3), diag)
            completed += 1

        self.assertEqual(completed, PROPERTY_TARGET_CASES)

    def test_concurrency_32_recorded_schedules_bounded_barriers_and_joins(
        self,
    ) -> None:
        """Concurrency: 32 recorded logical schedules, <=4 threads, 2s barriers/joins, 0 leaks."""
        completed_schedules = 0

        for sched_idx in range(CONCURRENCY_TARGET_SCHEDULES):
            mode = sched_idx % 4
            w, h, slots = 5, 4, 2
            ring_size = wsb.rtring.ring_bytes(slots, w, h)
            bank = wsb.WindowStreamBank(quota_bytes=2 * ring_size)
            handle = wsb.Handle("cdp_tab", f"conc-tab-{sched_idx}", 1)
            spec = wsb.StreamSpec(
                width=w, height=h, slots=slots, quota_bytes=ring_size
            )
            adapter = cpx.ChromiumStreamAdapter(bank, handle, spec)

            rgb_a = cpx.build_sentinel_rgb(w, h, seed=sched_idx + 1)
            rgb_b = cpx.build_sentinel_rgb(w, h, seed=sched_idx + 101)
            decl = cpx.PixelLayoutDeclaration(
                width=w,
                height=h,
                channel_order="BGRA",
                stride_bytes=w * 4 + 8,
                orientation="bottom_up",
                alpha_policy="opaque",
                color_space="sRGB",
                dpr_ppm=1_000_000,
                zoom_ppm=1_000_000,
                environment_id=f"env-conc-{sched_idx}",
            )
            raw_a = cpx.encode_raw_chromium_buffer(
                rgb_a, decl, pad_seed=0x10 + sched_idx
            )
            raw_b = cpx.encode_raw_chromium_buffer(
                rgb_b, decl, pad_seed=0x80 + sched_idx
            )
            tree_a = cpx.PresentationTreeSnapshot(
                revision=100 + sched_idx,
                environment_id=decl.environment_id,
                width=w,
                height=h,
            )
            tree_b = cpx.PresentationTreeSnapshot(
                revision=200 + sched_idx,
                environment_id=decl.environment_id,
                width=w,
                height=h,
            )
            att_a = cpx.CompositorAttestation(
                residency="ram_verified",
                revision_evidence=f"rev-{100 + sched_idx}",
                presentation_revision=100 + sched_idx,
                environment_id=decl.environment_id,
            )
            producer = cpx.FakeChromiumProducer(
                declaration=decl, raw_bytes=raw_a, tree=tree_a, attestation=att_a
            )

            barrier_enter = threading.Barrier(2, timeout=BARRIER_TIMEOUT_SECONDS)
            barrier_exit = threading.Barrier(2, timeout=BARRIER_TIMEOUT_SECONDS)
            cancel_event = threading.Event()
            done_event = threading.Event()
            schedule_lock = threading.Lock()
            recorded_schedule: list[str] = []
            worker_errors: list[BaseException] = []
            threads: list[threading.Thread] = []

            def record(step: str) -> None:
                with schedule_lock:
                    recorded_schedule.append(step)

            try:
                if mode == 0:
                    # Mode 0: Concurrent presentation update while frozen is deferred until unfreeze
                    def tx_hook(stage: str) -> None:
                        if stage == "after_pixels":
                            record("tx:frozen_at_pixels")
                            barrier_enter.wait(timeout=BARRIER_TIMEOUT_SECONDS)
                            barrier_exit.wait(timeout=BARRIER_TIMEOUT_SECONDS)
                            record("tx:resume_to_tree")

                    def mutator_worker() -> None:
                        try:
                            barrier_enter.wait(timeout=BARRIER_TIMEOUT_SECONDS)
                            applied = producer.stage_presentation_update(
                                200 + sched_idx, raw_b, decl, tree_b
                            )
                            record(f"mutator:staged_while_frozen={applied}")
                            barrier_exit.wait(timeout=BARRIER_TIMEOUT_SECONDS)
                        except BaseException as exc:
                            worker_errors.append(exc)

                    t_mut = threading.Thread(
                        target=mutator_worker, name=f"mut-{sched_idx}"
                    )
                    threads.append(t_mut)
                    t_mut.start()

                    res = cpx.execute_capture_transaction(
                        producer,
                        stream_adapter=adapter,
                        frame_seq=0,
                        step_hook=tx_hook,
                    )
                    record(
                        f"tx:committed_rev={res.receipt.presentation_revision}"
                    )
                    record(
                        f"producer:post_unfreeze_rev={producer.current_revision()}"
                    )
                    expected_schedule = [
                        "tx:frozen_at_pixels",
                        "mutator:staged_while_frozen=False",
                        "tx:resume_to_tree",
                        f"tx:committed_rev={100 + sched_idx}",
                        f"producer:post_unfreeze_rev={200 + sched_idx}",
                    ]

                elif mode == 1:
                    # Mode 1: Concurrent cancellation at after_tree releases lease and unfreezes
                    def tx_hook(stage: str) -> None:
                        if stage == "after_tree":
                            record("tx:at_after_tree")
                            barrier_enter.wait(timeout=BARRIER_TIMEOUT_SECONDS)
                            barrier_exit.wait(timeout=BARRIER_TIMEOUT_SECONDS)

                    def cancel_worker() -> None:
                        try:
                            barrier_enter.wait(timeout=BARRIER_TIMEOUT_SECONDS)
                            cancel_event.set()
                            record("canceller:set_cancel")
                            barrier_exit.wait(timeout=BARRIER_TIMEOUT_SECONDS)
                        except BaseException as exc:
                            worker_errors.append(exc)

                    t_can = threading.Thread(
                        target=cancel_worker, name=f"can-{sched_idx}"
                    )
                    threads.append(t_can)
                    t_can.start()

                    with self.assertRaises(cpx.ChromiumPixelError) as cm:
                        cpx.execute_capture_transaction(
                            producer,
                            stream_adapter=adapter,
                            frame_seq=0,
                            cancel_event=cancel_event,
                            step_hook=tx_hook,
                        )
                    self.assertEqual(cm.exception.code, "E_CANCELLED")
                    record(
                        f"tx:cancelled_released={all(l.released for l in producer.issued_leases)}"
                    )
                    expected_schedule = [
                        "tx:at_after_tree",
                        "canceller:set_cancel",
                        "tx:cancelled_released=True",
                    ]

                elif mode == 2:
                    # Mode 2: Concurrent bypass-freeze tamper detected by INV-E3-3 revision recheck
                    def tx_hook(stage: str) -> None:
                        if stage == "after_tree":
                            record("tx:before_recheck")
                            barrier_enter.wait(timeout=BARRIER_TIMEOUT_SECONDS)
                            barrier_exit.wait(timeout=BARRIER_TIMEOUT_SECONDS)

                    def tamper_worker() -> None:
                        try:
                            barrier_enter.wait(timeout=BARRIER_TIMEOUT_SECONDS)
                            producer.stage_presentation_update(
                                200 + sched_idx,
                                raw_b,
                                decl,
                                tree_b,
                                bypass_freeze=True,
                            )
                            record("tamper:bypassed_freeze")
                            barrier_exit.wait(timeout=BARRIER_TIMEOUT_SECONDS)
                        except BaseException as exc:
                            worker_errors.append(exc)

                    t_tam = threading.Thread(
                        target=tamper_worker, name=f"tam-{sched_idx}"
                    )
                    threads.append(t_tam)
                    t_tam.start()

                    with self.assertRaises(cpx.ChromiumPixelError) as cm:
                        cpx.execute_capture_transaction(
                            producer,
                            stream_adapter=adapter,
                            frame_seq=0,
                            step_hook=tx_hook,
                        )
                    self.assertEqual(cm.exception.code, "E_REVISION_MISMATCH")
                    record("tx:rejected_revision_mismatch")
                    expected_schedule = [
                        "tx:before_recheck",
                        "tamper:bypassed_freeze",
                        "tx:rejected_revision_mismatch",
                    ]

                else:
                    # Mode 3: Producer commit + concurrent stream reader verification via Event + Barrier
                    tok = adapter.ensure_open()

                    def reader_worker() -> None:
                        try:
                            barrier_enter.wait(timeout=BARRIER_TIMEOUT_SECONDS)
                            self.assertTrue(
                                done_event.wait(timeout=BARRIER_TIMEOUT_SECONDS)
                            )
                            ref = bank.read(tok)
                            self.assertIsInstance(ref, wsb.FrameRef)
                            record(f"reader:read_seq={ref.frame_seq}")
                            barrier_exit.wait(timeout=BARRIER_TIMEOUT_SECONDS)
                        except BaseException as exc:
                            worker_errors.append(exc)

                    t_rd = threading.Thread(
                        target=reader_worker, name=f"rd-{sched_idx}"
                    )
                    threads.append(t_rd)
                    t_rd.start()

                    barrier_enter.wait(timeout=BARRIER_TIMEOUT_SECONDS)
                    res = cpx.execute_capture_transaction(
                        producer,
                        stream_adapter=adapter,
                        frame_seq=0,
                    )
                    record(f"writer:published_seq={res.receipt.frame_seq}")
                    done_event.set()
                    barrier_exit.wait(timeout=BARRIER_TIMEOUT_SECONDS)
                    expected_schedule = [
                        "writer:published_seq=0",
                        "reader:read_seq=0",
                    ]

                self.assertLessEqual(len(threads), MAX_CONCURRENCY_THREADS)
                for t in threads:
                    t.join(timeout=JOIN_TIMEOUT_SECONDS)
                    self.assertFalse(
                        t.is_alive(),
                        f"leaked thread {t.name} in schedule {sched_idx}",
                    )

                self.assertEqual(
                    worker_errors,
                    [],
                    f"worker errors in schedule {sched_idx}: {worker_errors}",
                )
                self.assertEqual(
                    recorded_schedule,
                    expected_schedule,
                    f"incomplete or out-of-order schedule {sched_idx}",
                )
                self.assertFalse(producer.frozen)
                self.assertTrue(
                    all(lease.released for lease in producer.issued_leases)
                )
                completed_schedules += 1
            finally:
                barrier_enter.abort()
                barrier_exit.abort()
                for t in threads:
                    if t.is_alive():
                        t.join(timeout=JOIN_TIMEOUT_SECONDS)
                adapter.close()
                self.assertEqual(bank.reserved_bytes, 0)

        self.assertEqual(completed_schedules, CONCURRENCY_TARGET_SCHEDULES)


if __name__ == "__main__":
    unittest.main()
