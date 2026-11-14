"""Tests for eval.v2eval.frame_property_fuzzer and G-R2-2 vision invariants.

Embeds its own AST mutation harness and tiny in-RAM fixtures without importing
sibling test harnesses. Covers deterministic SplitMix64 seed splitting, explicit
state snapshot/restore, exact O(1) replay by (seed, case_index), hierarchical
shrinking termination with preserved invalidity or oracle disagreement, Python
integer extent/conversion/precedence oracles, capture wrap-boundary mutant kills
(M-vadapt-x11-ramcap-1, M-vadapt-x11-ramcap-2), and projection-pair OPS plus
_tile_sums kills.
"""
from __future__ import annotations

import ast
from contextvars import copy_context
import copy
from dataclasses import dataclass, replace
from pathlib import Path
import sys
import threading
import types
from typing import Callable
import unittest

import numpy as np

from eval.v2eval import frame_property_fuzzer as fuzzer
from eval.v2eval import postpass as real_postpass
from eval.v2x import gm_bands_hv as real_gm
from vadapt import x11_ramcap as real_ramcap
from vadapt import xfb as real_xfb


REPO_ROOT = Path(__file__).resolve().parents[1]

OUTCOME_KILLED = "killed"
OUTCOME_SURVIVED = "survived"
OUTCOME_EQUIVALENT = "equivalent"
OUTCOME_MISSING_ANCHOR = "missing_anchor"
OUTCOME_NONUNIQUE_ANCHOR = "nonunique_anchor"
OUTCOME_POSITIVE_FAILED = "positive_failed"
OUTCOME_IMPORT_ERROR = "import_error"
OUTCOME_UNRELATED_EXCEPTION = "unrelated_exception"


@dataclass(frozen=True)
class MutantSiteSpec:
    """Self-contained specification of an AST mutation site and behavioral test."""

    mutant_id: str
    module_rel_path: str
    package_name: str
    real_module: types.ModuleType
    qualified_scope: str
    containing_expr_src: str
    structural_selector: str
    expected_anchor_src: str
    transformation_label: str
    mutator: Callable[[ast.AST], ast.AST]
    behavioral_check: Callable[[types.ModuleType], None]


def _resolve_qualified_scope(tree: ast.Module, qualified_scope: str) -> list[ast.AST]:
    """Find AST scope node(s) by dotted class/function path without ordinals."""
    parts = [p for p in qualified_scope.split(".") if p]
    if not parts:
        return [tree]

    def _walk_body(stmts: list[ast.stmt], rem: list[str]) -> list[ast.AST]:
        head = rem[0]
        tail = rem[1:]
        found: list[ast.AST] = []
        for st in stmts:
            if isinstance(st, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and st.name == head:
                if not tail:
                    found.append(st)
                else:
                    found.extend(_walk_body(st.body, tail))
        return found

    return _walk_body(tree.body, parts)


def _iter_local_scope_nodes(scope_node: ast.AST) -> list[ast.AST]:
    """Collect AST nodes inside scope_node without descending into nested defs."""
    nodes: list[ast.AST] = []

    def _visit(node: ast.AST, root: bool = False) -> None:
        if not root and isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            return
        nodes.append(node)
        for child in ast.iter_child_nodes(node):
            _visit(child, root=False)

    _visit(scope_node, root=True)
    return nodes


def _select_subnode(root_node: ast.AST, selector: str) -> list[ast.AST]:
    """Navigate structurally from a unique containing AST node to the target node."""
    steps = [s.strip() for s in selector.split("/") if s.strip()]
    if not steps:
        return []
    current: list[ast.AST] = [root_node]
    for step in steps:
        if ":" in step:
            nav, want_type = step.split(":", 1)
        else:
            nav, want_type = step, None
        nxt: list[ast.AST] = []
        for node in current:
            if nav == "self":
                cands = [node]
            elif "[" in nav and nav.endswith("]"):
                field_name, idx_txt = nav[:-1].split("[", 1)
                if not idx_txt.isdigit():
                    return []
                idx = int(idx_txt)
                seq = getattr(node, field_name, None)
                if isinstance(seq, list) and 0 <= idx < len(seq) and isinstance(seq[idx], ast.AST):
                    cands = [seq[idx]]
                else:
                    cands = []
            else:
                val = getattr(node, nav, None)
                cands = [val] if isinstance(val, ast.AST) else []
            for c in cands:
                if want_type is None or type(c).__name__ == want_type:
                    nxt.append(c)
        current = nxt
        if not current:
            return []
    return current


def _replace_node_identity(root: ast.AST, target: ast.AST, replacement: ast.AST) -> bool:
    """Replace exact AST object identity `target` in `root` with `replacement`."""
    for parent in ast.walk(root):
        for fname, fval in ast.iter_fields(parent):
            if fval is target:
                setattr(parent, fname, replacement)
                return True
            if isinstance(fval, list):
                for idx, item in enumerate(fval):
                    if item is target:
                        fval[idx] = replacement
                        return True
    return False


def execute_mutant_spec(spec: MutantSiteSpec) -> dict[str, object]:
    """Execute a positive check on the real module, then run the AST mutant in RAM."""
    try:
        copy_context().run(spec.behavioral_check, spec.real_module)
    except BaseException as exc:
        return {
            "mutant_id": spec.mutant_id,
            "outcome": OUTCOME_POSITIVE_FAILED,
            "detail": f"{type(exc).__name__}: {exc}",
        }

    src_path = REPO_ROOT / spec.module_rel_path
    source_text = src_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=spec.module_rel_path)

    scopes = _resolve_qualified_scope(tree, spec.qualified_scope)
    if not scopes:
        return {"mutant_id": spec.mutant_id, "outcome": OUTCOME_MISSING_ANCHOR, "detail": "missing scope"}
    if len(scopes) != 1:
        return {"mutant_id": spec.mutant_id, "outcome": OUTCOME_NONUNIQUE_ANCHOR, "detail": "nonunique scope"}

    scope_nodes = _iter_local_scope_nodes(scopes[0])
    containing_matches = [n for n in scope_nodes if not isinstance(n, ast.Expr) and ast.unparse(n).strip() == spec.containing_expr_src.strip()]
    if not containing_matches:
        return {"mutant_id": spec.mutant_id, "outcome": OUTCOME_MISSING_ANCHOR, "detail": "missing containing expr"}
    if len(containing_matches) != 1:
        return {"mutant_id": spec.mutant_id, "outcome": OUTCOME_NONUNIQUE_ANCHOR, "detail": "nonunique containing expr"}

    containing_node = containing_matches[0]
    targets = _select_subnode(containing_node, spec.structural_selector)
    if not targets:
        return {"mutant_id": spec.mutant_id, "outcome": OUTCOME_MISSING_ANCHOR, "detail": "missing selector target"}
    if len(targets) != 1:
        return {"mutant_id": spec.mutant_id, "outcome": OUTCOME_NONUNIQUE_ANCHOR, "detail": "nonunique selector target"}

    target_node = targets[0]
    anchor_unparsed = ast.unparse(target_node).strip()
    if anchor_unparsed != spec.expected_anchor_src.strip():
        return {
            "mutant_id": spec.mutant_id,
            "outcome": OUTCOME_MISSING_ANCHOR,
            "detail": f"anchor mismatch: got {anchor_unparsed!r}, want {spec.expected_anchor_src!r}",
        }

    anchor_lineno = getattr(target_node, "lineno", getattr(containing_node, "lineno", None))
    new_node = ast.copy_location(spec.mutator(copy.deepcopy(target_node)), target_node)
    if not _replace_node_identity(tree, target_node, new_node):
        return {"mutant_id": spec.mutant_id, "outcome": OUTCOME_MISSING_ANCHOR, "detail": "replace failed"}
    ast.fix_missing_locations(tree)

    private_name = f"{spec.package_name}._priv_mut_{spec.mutant_id.replace('-', '_')}"
    saved_dont_write = sys.dont_write_bytecode
    saved_modules = dict(sys.modules)
    try:
        sys.dont_write_bytecode = True
        try:
            code_obj = compile(tree, filename=f"<inmem:{spec.mutant_id}>", mode="exec")
            mut_mod = types.ModuleType(private_name)
            mut_mod.__file__ = str(src_path)
            mut_mod.__package__ = spec.package_name
            sys.modules[private_name] = mut_mod
            exec(code_obj, mut_mod.__dict__)
        except BaseException as exc:
            return {
                "mutant_id": spec.mutant_id,
                "outcome": OUTCOME_IMPORT_ERROR,
                "lineno": anchor_lineno,
                "detail": f"{type(exc).__name__}: {exc}",
            }

        try:
            copy_context().run(spec.behavioral_check, mut_mod)
        except AssertionError as exc:
            return {
                "mutant_id": spec.mutant_id,
                "outcome": OUTCOME_KILLED,
                "lineno": anchor_lineno,
                "detail": str(exc),
            }
        except BaseException as exc:
            return {
                "mutant_id": spec.mutant_id,
                "outcome": OUTCOME_UNRELATED_EXCEPTION,
                "lineno": anchor_lineno,
                "detail": f"{type(exc).__name__}: {exc}",
            }

        return {
            "mutant_id": spec.mutant_id,
            "outcome": OUTCOME_SURVIVED,
            "lineno": anchor_lineno,
            "detail": "assertion passed on mutated module",
        }
    finally:
        sys.modules.clear()
        sys.modules.update(saved_modules)
        sys.dont_write_bytecode = saved_dont_write


class TestSplitMix64AndReplay(unittest.TestCase):
    """Test explicit RNG state, exact integer guards, seed splitting, and O(1) replay."""

    def test_splitmix64_explicit_state_and_exact_integer_guards(self):
        rng = fuzzer.SplitMix64(0x123456789ABCDEF0)
        self.assertEqual(rng.state, 0x123456789ABCDEF0)
        snap = rng.snapshot()
        seq_a = [rng.next_u64() for _ in range(8)]
        self.assertNotEqual(rng.state, snap)

        rng.restore(snap)
        self.assertEqual(rng.state, snap)
        seq_b = [rng.next_u64() for _ in range(8)]
        self.assertEqual(seq_a, seq_b)

        cloned = rng.clone()
        self.assertEqual(cloned.state, rng.state)
        self.assertEqual([cloned.next_u64() for _ in range(4)], [rng.next_u64() for _ in range(4)])

        for bad_seed in (True, False, 1.5, "123", np.int64(7)):
            with self.subTest(bad_seed=bad_seed):
                with self.assertRaises(TypeError):
                    fuzzer.SplitMix64(bad_seed)
        for out_of_range in (-1, (1 << 64)):
            with self.subTest(out_of_range=out_of_range):
                with self.assertRaises(ValueError):
                    fuzzer.SplitMix64(out_of_range)

        with self.assertRaises(TypeError):
            rng.randint(True, 5)
        with self.assertRaises(ValueError):
            rng.randint(10, 5)
        with self.assertRaises(TypeError):
            rng.chance(True, 2)
        with self.assertRaises(ValueError):
            rng.chance(3, 2)

    def test_deterministic_seed_tree_independence_and_exact_replay(self):
        seed = 0xCAFEBABE12345678
        tree = fuzzer.SeedTree(seed)
        derived = {
            (dom, idx): tree.derive_seed(dom, idx)
            for dom in fuzzer.DOMAINS
            for idx in range(16)
        }
        self.assertEqual(len(set(derived.values())), len(derived))

        xwd_seq = [fuzzer.generate_xwd_case(seed, i) for i in range(12)]
        motion_seq = [fuzzer.generate_motion_band_case(seed, i) for i in range(12)]
        frame_seq = [fuzzer.generate_frame_case(seed, i) for i in range(12)]

        engine = fuzzer.FramePropertyFuzzer(seed)
        for idx in (11, 0, 7, 3, 9):
            rx = engine.replay("xwd", idx)
            self.assertEqual(rx.buf, xwd_seq[idx].buf)
            self.assertEqual(rx.fault_kind, xwd_seq[idx].fault_kind)
            self.assertTrue(np.array_equal(rx.rgb, xwd_seq[idx].rgb))

            rm = engine.replay("motion", idx)
            self.assertEqual(rm.pattern, motion_seq[idx].pattern)
            self.assertEqual((rm.dx, rm.dy, rm.tile_side, rm.max_d),
                             (motion_seq[idx].dx, motion_seq[idx].dy, motion_seq[idx].tile_side, motion_seq[idx].max_d))
            self.assertTrue(np.array_equal(rm.prev_luma, motion_seq[idx].prev_luma))
            self.assertTrue(np.array_equal(rm.cur_luma, motion_seq[idx].cur_luma))

            rf = engine.replay("frame", idx)
            self.assertEqual(rf.fault_kind, frame_seq[idx].fault_kind)
            for a, b in zip(rf.rgb_frames, frame_seq[idx].rgb_frames):
                self.assertTrue(np.array_equal(a, b))

    def test_multithreaded_seed_tree_and_ops_isolation_with_barrier(self):
        num_workers = 4
        barrier = threading.Barrier(num_workers)
        errors: list[BaseException] = []
        results: dict[int, tuple[int, int]] = {}
        lock = threading.Lock()
        before_threads = {t.ident for t in threading.enumerate()}

        def worker(worker_id: int) -> None:
            try:
                barrier.wait(timeout=5.0)
                case = fuzzer.generate_motion_band_case(0x9988776655443322 + worker_id, worker_id, mode="scrolled_band")
                real_gm.reset_ops()
                mask = real_gm.consensus_tiles(case.prev_luma, case.cur_luma, case.tile_side, case.dx, case.dy)
                ops_now = int(real_gm.OPS)
                want = fuzzer.independent_consensus_oracle(case.prev_luma, case.cur_luma, case.tile_side, case.dx, case.dy)
                assert np.array_equal(mask, want), "threaded consensus mismatch"
                assert ops_now > 0, "threaded OPS must be positive"
                with lock:
                    results[worker_id] = (ops_now, int(mask.sum()))
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(num_workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive(), "Worker thread leaked past bounded join")

        if errors:
            raise errors[0]
        self.assertEqual(len(results), num_workers)
        after_threads = {t.ident for t in threading.enumerate()}
        self.assertEqual(before_threads, after_threads)


class TestGeneratorsAndOracles(unittest.TestCase):
    """Test XWD, motion-band, and frame generators against independent Python-int oracles."""

    def test_xwd_generator_covers_odd_widths_padding_opposite_byte_order_and_invalid_extents(self):
        seed = 0xA5A5A5A55A5A5A5A
        saw_odd_width = False
        saw_padding = False
        saw_msb_byte_order = False
        saw_directcolor = False
        saw_invalid_codes: set[str] = set()

        for idx in range(80):
            case = fuzzer.generate_xwd_case(seed, idx, mode="mixed")
            self.assertIs(case.rgb.dtype, np.dtype(np.uint8))
            self.assertIsNone(case.rgb.base)
            outcome = fuzzer.evaluate_xwd_case(case)
            if case.fault_kind == "valid":
                self.assertIsNone(outcome, f"Valid XWD case {idx} failed: {outcome}")
                if case.width % 2 == 1:
                    saw_odd_width = True
                if case.pad_bytes > 0:
                    saw_padding = True
                if case.byte_order == 1:
                    saw_msb_byte_order = True
                if case.visual_class == real_xfb.DIRECTCOLOR:
                    saw_directcolor = True
            else:
                self.assertIsNotNone(outcome)
                self.assertEqual(outcome[0], "invalidity", f"Unexpected oracle disagreement on {case.fault_kind}: {outcome}")
                saw_invalid_codes.add(outcome[2])

        self.assertTrue(saw_odd_width)
        self.assertTrue(saw_padding)
        self.assertTrue(saw_msb_byte_order)
        self.assertTrue(saw_directcolor)
        for expected_code in (
            "E_XWD_SHORT",
            "E_XWD_VERSION",
            "E_XWD_FORMAT",
            "E_XWD_BYTE_ORDER",
            "E_XWD_VISUAL",
            "E_XWD_GEOMETRY",
            "E_XWD_SIZE",
        ):
            self.assertIn(expected_code, saw_invalid_codes)

    def test_xfb_consistent_read_and_tries_guards(self):
        case = fuzzer.generate_xwd_case(0x1122334455667788, 0, mode="valid")
        for bad_tries in (True, False, 0, 1, 2.5, "2"):
            with self.subTest(bad_tries=bad_tries):
                with self.assertRaises(real_xfb.XwdError) as ctx:
                    real_xfb.consistent_read(lambda: case.buf, tries=bad_tries)
                self.assertEqual(ctx.exception.code, "E_XWD_TRIES")

        for bad_max in (True, False, 0, -5, 100.0):
            with self.subTest(bad_max=bad_max):
                with self.assertRaises(real_xfb.XwdError) as ctx:
                    real_xfb.consistent_read(lambda: case.buf, tries=2, max_bytes=bad_max)
                self.assertEqual(ctx.exception.code, "E_XWD_LIMIT")

        snapshots = [bytearray(case.buf), bytearray(case.buf)]
        snapshots[0][-1] ^= 0xFF
        snapshots.append(bytearray(case.buf))
        idx = [0]

        def read_fn():
            out = snapshots[idx[0]]
            idx[0] += 1
            return out

        got = real_xfb.consistent_read(read_fn, tries=3, max_bytes=len(case.buf))
        self.assertEqual(got, case.buf)

    def test_motion_band_generator_and_independent_oracles(self):
        seed = 0x3141592653589793
        saw_scrolled = False
        saw_static = False
        saw_flat = False
        saw_invalid = False

        for idx in range(60):
            case = fuzzer.generate_motion_band_case(seed, idx, mode="mixed")
            res = fuzzer.evaluate_motion_case(case)
            if case.pattern.startswith("invalid_"):
                self.assertIsNotNone(res)
                self.assertEqual(res[0], "invalidity", f"Unexpected motion disagreement at {idx}: {res}")
                saw_invalid = True
            else:
                self.assertIsNone(res, f"Valid motion case {idx} ({case.pattern}) failed: {res}")
                if case.pattern == "scrolled_band":
                    saw_scrolled = True
                elif case.pattern == "static_textured":
                    saw_static = True
                elif case.pattern == "flat":
                    saw_flat = True

        self.assertTrue(saw_scrolled)
        self.assertTrue(saw_static)
        self.assertTrue(saw_flat)
        self.assertTrue(saw_invalid)

        valid_case = fuzzer.generate_motion_band_case(seed, 2, mode="scrolled_band")
        want_pairs, want_ops = fuzzer.independent_projection_pairs_ops_oracle(
            valid_case.prev_luma, valid_case.cur_luma, valid_case.max_d, 8
        )
        real_gm.reset_ops()
        got_pairs = real_gm._projection_pairs(valid_case.prev_luma, valid_case.cur_luma, valid_case.max_d, 8)
        self.assertEqual(got_pairs, want_pairs)
        self.assertEqual(int(real_gm.OPS), want_ops)

    def test_frame_ownership_and_precedence_oracles(self):
        seed = 0x2718281828459045
        saw_valid = False
        saw_invalid = False
        saw_discontinuous = False

        for idx in range(40):
            case = fuzzer.generate_frame_case(seed, idx, mode="mixed")
            res = fuzzer.evaluate_frame_case(case)
            if case.fault_kind == "valid":
                self.assertIsNone(res, f"Valid frame case {idx} failed: {res}")
                own = fuzzer.verify_stream_ownership(case)
                self.assertTrue(own["valid"])
                self.assertEqual(own["discontinuous"], own["expected_discontinuous"])
                if own["discontinuous"]:
                    saw_discontinuous = True
                saw_valid = True
            else:
                self.assertIsNotNone(res)
                self.assertEqual(res[0], "invalidity")
                saw_invalid = True

        self.assertTrue(saw_valid)
        self.assertTrue(saw_invalid)
        self.assertTrue(saw_discontinuous)


class TestHierarchicalShrinking(unittest.TestCase):
    """Test hierarchical shrinking termination and preserved invalidity / oracle disagreement."""

    def test_shrink_preserves_xwd_invalidity_and_reduces_extents(self):
        for fault in ("short_buffer", "bad_bpl", "geometry_mismatch", "bad_byte_order"):
            with self.subTest(fault=fault):
                case = fuzzer.generate_xwd_case(0xABCDEF0123456789, 4, mode=fault)
                big_rgb = fuzzer.SplitMix64(999).uint8_rgb(13, 21)
                big_case = fuzzer.rebuild_xwd_case(replace(
                    case,
                    width=21 if case.width > 0 else 0,
                    height=13 if case.height > 0 else 0,
                    header_size=real_xfb.MIN_HEADER + 16,
                    ncolors=3,
                    pad_bytes=12,
                    bytes_per_line=(21 * 4 + 12) if fault != "bad_bpl" else (21 * 4 + 2),
                    expected_width=23 if fault == "geometry_mismatch" else 21,
                    expected_height=13,
                    size_delta=-12 if fault == "short_buffer" else 0,
                    rgb=big_rgb,
                ))
                orig_sig = fuzzer.evaluate_xwd_case(big_case)
                self.assertIsNotNone(orig_sig)
                self.assertEqual(orig_sig[0], "invalidity")

                shrunk = fuzzer.hierarchical_shrink(big_case, fuzzer.evaluate_xwd_case)
                self.assertTrue(shrunk.terminated)
                self.assertGreater(shrunk.steps, 0)
                self.assertEqual(fuzzer.evaluate_xwd_case(shrunk.minimal_case), orig_sig)
                self.assertLess(len(shrunk.minimal_case.buf), len(big_case.buf))
                self.assertEqual(shrunk.minimal_case.ncolors, 0)
                self.assertEqual(shrunk.minimal_case.header_size, real_xfb.MIN_HEADER)

    def test_shrink_preserves_oracle_disagreement_across_domains(self):
        xwd_case = fuzzer.rebuild_xwd_case(replace(
            fuzzer.generate_xwd_case(0x13579BDF2468ACE0, 1, mode="valid"),
            width=19,
            height=11,
            byte_order=1,
            pad_bytes=8,
            bytes_per_line=19 * 4 + 8,
            expected_width=19,
            expected_height=11,
            header_size=real_xfb.MIN_HEADER + 8,
            ncolors=2,
            rgb=fuzzer.SplitMix64(42).uint8_rgb(11, 19),
        ))

        def broken_rgb24(buf):
            hd = real_xfb.validate_frame(buf)
            raw = real_xfb.rgb24(buf)
            if hd.byte_order == 1 and (hd.width % 2 == 1):
                ba = bytearray(raw)
                ba[0] ^= 0xFF
                return bytes(ba)
            return raw

        pred_xwd = lambda c: fuzzer.evaluate_xwd_case(c, rgb24_fn=broken_rgb24)
        sig_xwd = pred_xwd(xwd_case)
        self.assertEqual(sig_xwd[:2], ("oracle_disagreement", "rgb24_mismatch"))

        shrunk_xwd = fuzzer.hierarchical_shrink(xwd_case, pred_xwd)
        self.assertTrue(shrunk_xwd.terminated)
        self.assertEqual(pred_xwd(shrunk_xwd.minimal_case), sig_xwd)
        self.assertEqual(shrunk_xwd.minimal_case.width % 2, 1)
        self.assertEqual((shrunk_xwd.minimal_case.height, shrunk_xwd.minimal_case.width), (1, 1))

        motion_case = fuzzer.generate_motion_band_case(0x13579BDF2468ACE0, 3, mode="scrolled_band")
        pred_motion = lambda c: fuzzer.evaluate_motion_case(
            c,
            consensus_fn=lambda p, q, s, dx, dy: ~real_gm.consensus_tiles(p, q, s, dx, dy),
        )
        sig_motion = pred_motion(motion_case)
        self.assertEqual(sig_motion, ("oracle_disagreement", "consensus_mismatch", "scrolled_band"))
        shrunk_motion = fuzzer.hierarchical_shrink(motion_case, pred_motion)
        self.assertTrue(shrunk_motion.terminated)
        self.assertEqual(pred_motion(shrunk_motion.minimal_case), sig_motion)
        self.assertLessEqual(shrunk_motion.minimal_case.height * shrunk_motion.minimal_case.width,
                             motion_case.height * motion_case.width)

        frame_case = fuzzer.generate_frame_case(0x13579BDF2468ACE0, 5, mode="valid")
        pred_frame = lambda c: fuzzer.evaluate_frame_case(
            c,
            apply_claims_fn=lambda base, claims, flicker_veto=None: {
                "cls": base["cls"] + 1,
                "mv_dx": base["mv_dx"].copy(),
                "mv_dy": base["mv_dy"].copy(),
            },
        )
        sig_frame = pred_frame(frame_case)
        self.assertEqual(sig_frame, ("oracle_disagreement", "precedence_mismatch", "cls"))
        shrunk_frame = fuzzer.hierarchical_shrink(frame_case, pred_frame)
        self.assertTrue(shrunk_frame.terminated)
        self.assertEqual(pred_frame(shrunk_frame.minimal_case), sig_frame)


class TestMutationCatalogueAndWrapBoundaryKills(unittest.TestCase):
    """Test named catalogue contrasts M-vadapt-x11-ramcap-1/2 and vision mutant kills."""

    @staticmethod
    def _assert_ramcap_wrap_boundary(mod: types.ModuleType) -> None:
        probe = fuzzer.run_capture_wrap_boundary_probe(ramcap_module=mod)
        assert probe["initial_frames"] == 1
        assert probe["penultimate_error"] is None, f"penultimate error: {probe['penultimate_error']}"
        assert probe["penultimate_render_index"] == (1 << 64) - 2, (
            f"penultimate capture failed or returned wrong index: {probe}"
        )
        assert probe["frames_after_penultimate"] == (1 << 64) - 1, (
            f"expected frames == (1<<64)-1 after penultimate capture, got {probe['frames_after_penultimate']}"
        )
        assert probe["exhausted_code"] == "E_BUDGET", (
            f"expected CaptureError E_BUDGET at (1<<64)-1, got {probe['exhausted_code']}: {probe['exhausted_detail']}"
        )
        assert "ring render index exhausted" in str(probe["exhausted_detail"]), (
            f"unexpected exhaustion detail: {probe['exhausted_detail']}"
        )
        assert probe["frames_after_exhaustion"] == (1 << 64) - 1, (
            f"frames rolled over after exhaustion: {probe['frames_after_exhaustion']}"
        )
        assert probe["state_after_exhaustion"] == "failed", (
            f"expected state 'failed' after E_BUDGET, got {probe['state_after_exhaustion']}"
        )

    @staticmethod
    def _assert_gm_projection_ops_and_tile_sums(mod: types.ModuleType) -> None:
        case = fuzzer.generate_motion_band_case(0x3141592653589793, 2, mode="scrolled_band")
        for shape in ((32, 32), (case.height, case.width)):
            diff_mask = (case.cur_luma[:shape[0], :shape[1]] != case.prev_luma[:shape[0], :shape[1]])
            want_sums = fuzzer.independent_tile_sums_oracle(diff_mask, 16)
            got_sums = mod._tile_sums(diff_mask, 16)
            assert got_sums.shape == want_sums.shape and np.array_equal(got_sums, want_sums), "direct _tile_sums disagreed with Python-integer oracle"

        want_pairs, want_ops = fuzzer.independent_projection_pairs_ops_oracle(
            case.prev_luma, case.cur_luma, case.max_d, 8
        )
        mod.reset_ops()
        got_pairs = mod._projection_pairs(case.prev_luma, case.cur_luma, case.max_d, 8)
        assert got_pairs == want_pairs, "_projection_pairs candidate set mismatch"
        assert int(mod.OPS) == want_ops, f"_projection_pairs OPS mismatch: {mod.OPS} != {want_ops}"

    @staticmethod
    def _assert_xfb_invariants(mod: types.ModuleType) -> None:
        for idx in range(25):
            case = fuzzer.generate_xwd_case(0xA5A5A5A55A5A5A5A, idx, mode="mixed")
            res = fuzzer.evaluate_xwd_case(
                case,
                parse_header_fn=mod.parse_header,
                validate_frame_fn=mod.validate_frame,
                rgb24_fn=mod.rgb24,
            )
            if case.fault_kind == "valid":
                assert res is None, f"valid XWD case failed on module: {res}"
            else:
                assert res is not None and res[0] == "invalidity", f"invalid XWD case not rejected properly: {res}"

    @staticmethod
    def _assert_postpass_precedence(mod: types.ModuleType) -> None:
        for idx in range(20):
            case = fuzzer.generate_frame_case(0x2718281828459045, idx, mode="valid")
            res = fuzzer.evaluate_frame_case(case, apply_claims_fn=mod.apply_claims)
            assert res is None, f"postpass precedence oracle disagreement: {res}"

    def test_named_catalogue_contrasts_and_mutant_kills(self):
        specs = (
            MutantSiteSpec(
                mutant_id="M-vadapt-x11-ramcap-1",
                module_rel_path="vadapt/x11_ramcap.py",
                package_name="vadapt",
                real_module=real_ramcap,
                qualified_scope="RamCapture._capture",
                containing_expr_src="s.frames >= (1 << 64) - 1",
                structural_selector="comparators[0]:BinOp/left:BinOp",
                expected_anchor_src="1 << 64",
                transformation_label="boundary increment -> left",
                mutator=lambda node: ast.BinOp(left=node, op=ast.Sub(), right=ast.Constant(value=1)),
                behavioral_check=self._assert_ramcap_wrap_boundary,
            ),
            MutantSiteSpec(
                mutant_id="M-vadapt-x11-ramcap-2",
                module_rel_path="vadapt/x11_ramcap.py",
                package_name="vadapt",
                real_module=real_ramcap,
                qualified_scope="RamCapture._capture",
                containing_expr_src="s.frames >= (1 << 64) - 1",
                structural_selector="comparators[0]:BinOp",
                expected_anchor_src="(1 << 64) - 1",
                transformation_label="boundary increment -> right",
                mutator=lambda node: ast.BinOp(left=node, op=ast.Add(), right=ast.Constant(value=1)),
                behavioral_check=self._assert_ramcap_wrap_boundary,
            ),
            MutantSiteSpec(
                mutant_id="M-eval-v2x-gm-bands-hv-proj-ops",
                module_rel_path="eval/v2x/gm_bands_hv.py",
                package_name="eval.v2x",
                real_module=real_gm,
                qualified_scope="_projection_pairs",
                containing_expr_src="_add_ops((hi - lo) * w * 2)",
                structural_selector="args[0]:BinOp/right:Constant",
                expected_anchor_src="2",
                transformation_label="2 -> 1 in _add_ops((hi - lo) * w * 2)",
                mutator=lambda node: ast.Constant(value=1),
                behavioral_check=self._assert_gm_projection_ops_and_tile_sums,
            ),
            MutantSiteSpec(
                mutant_id="M-eval-v2x-gm-bands-hv-tile-sums",
                module_rel_path="eval/v2x/gm_bands_hv.py",
                package_name="eval.v2x",
                real_module=real_gm,
                qualified_scope="_tile_sums",
                containing_expr_src="((h + s - 1) // s, (w + s - 1) // s)",
                structural_selector="elts[0]:BinOp/left:BinOp/right:Constant",
                expected_anchor_src="1",
                transformation_label="1 -> 0 in (h + s - 1) // s",
                mutator=lambda node: ast.Constant(value=0),
                behavioral_check=self._assert_gm_projection_ops_and_tile_sums,
            ),
            MutantSiteSpec(
                mutant_id="M-vadapt-xfb-byte-order",
                module_rel_path="vadapt/xfb.py",
                package_name="vadapt",
                real_module=real_xfb,
                qualified_scope="rgb24",
                containing_expr_src="hd.byte_order == 0",
                structural_selector="comparators[0]:Constant",
                expected_anchor_src="0",
                transformation_label="0 -> 1 in hd.byte_order == 0",
                mutator=lambda node: ast.Constant(value=1),
                behavioral_check=self._assert_xfb_invariants,
            ),
            MutantSiteSpec(
                mutant_id="M-eval-v2eval-postpass-veto-cls4",
                module_rel_path="eval/v2eval/postpass.py",
                package_name="eval.v2eval",
                real_module=real_postpass,
                qualified_scope="apply_claims",
                containing_expr_src="flicker_veto & (baseline['cls'] == 4)",
                structural_selector="right:Compare/comparators[0]:Constant",
                expected_anchor_src="4",
                transformation_label="4 -> 2 in baseline['cls'] == 4",
                mutator=lambda node: ast.Constant(value=2),
                behavioral_check=self._assert_postpass_precedence,
            ),
        )

        for spec in specs:
            with self.subTest(mutant_id=spec.mutant_id):
                res = execute_mutant_spec(spec)
                self.assertEqual(
                    res["outcome"],
                    OUTCOME_KILLED,
                    f"Mutant {spec.mutant_id} was not killed: {res}",
                )
        self.assertEqual(specs[0].expected_anchor_src, "1 << 64")
        self.assertEqual(specs[1].expected_anchor_src, "(1 << 64) - 1")


class TestSingleConditionNegativesAndHarnessAccounting(unittest.TestCase):
    """Test single-condition negatives for xfb, x11_ramcap, gm_bands_hv, and harness accounting."""

    def test_single_condition_negatives_across_vision_producers(self):
        # 1. XwdError string form and short header message
        short_buf = b"\x00" * 12
        with self.assertRaises(real_xfb.XwdError) as ctx:
            real_xfb.parse_header(short_buf)
        self.assertEqual(ctx.exception.code, "E_XWD_SHORT")
        self.assertEqual(str(ctx.exception), f"E_XWD_SHORT len {len(short_buf)} < {real_xfb.MIN_HEADER}")

        # 2. Bad tries message
        with self.assertRaises(real_xfb.XwdError) as ctx:
            real_xfb.consistent_read(lambda: b"", tries=1)
        self.assertEqual(ctx.exception.code, "E_XWD_TRIES")
        self.assertEqual(str(ctx.exception), "E_XWD_TRIES tries must be an exact integer >= 2")

        # 3. xfb.store and read_screen_fd with in-RAM mocked descriptor/stream seams (no frame files)
        import io
        import stat as stat_mod
        from unittest import mock
        valid_case = fuzzer.generate_xwd_case(0x5555555555555555, 0, mode="valid")
        reserved = []
        written_bytes = bytearray()

        class _MemSink(io.BytesIO):
            def __exit__(self, *args):
                written_bytes[:] = self.getvalue()
                return False

        fake_ws = types.SimpleNamespace(
            reserve=lambda n: reserved.append(n),
            path=lambda name: "/dev/shm/inmem_slot",
        )
        with mock.patch("vadapt.xfb.open", side_effect=lambda p, mode="wb": _MemSink(), create=True):
            digest = real_xfb.store(fake_ws, "slot0", valid_case.buf)
        self.assertEqual(reserved, [len(valid_case.buf)])
        self.assertEqual(bytes(written_bytes), valid_case.buf)
        self.assertEqual(len(digest), 64)

        fake_stat = types.SimpleNamespace(st_mode=stat_mod.S_IFREG | 0o600, st_dev=1, st_ino=99, st_size=len(valid_case.buf))
        before_calls = []
        with mock.patch("vadapt.xfb.os.fstat", return_value=fake_stat), \
             mock.patch("vadapt.xfb.os.pread", side_effect=lambda fd, n, off: valid_case.buf[off:off + n]):
            read_buf = real_xfb.read_screen_fd(
                77,
                max_bytes=len(valid_case.buf),
                tries=2,
                width=valid_case.width,
                height=valid_case.height,
                before_read=lambda: before_calls.append(1),
            )
        self.assertEqual(read_buf, valid_case.buf)
        self.assertGreaterEqual(len(before_calls), 3)

        # 4. SourceConfig single-condition negatives
        with self.assertRaises(real_ramcap.CaptureError) as ctx:
            real_ramcap.SourceConfig("/dev/shm/x11", "screen0", 8, 8, "-nocursor", kind="vnc")
        self.assertEqual(ctx.exception.code, "E_SOURCE_KIND")

        with self.assertRaises(real_ramcap.CaptureError) as ctx:
            real_ramcap.SourceConfig("/dev/shm/x11", "screen0", 8, 8, "   ", kind="xvfb-xwd")
        self.assertEqual(ctx.exception.code, "E_NOCURSOR")

        with self.assertRaises(real_ramcap.CaptureError) as ctx:
            real_ramcap.SourceConfig("relative/root", "screen0", 8, 8, "-nocursor")
        self.assertEqual(ctx.exception.code, "E_SOURCE_PATH")

        with self.assertRaises(real_ramcap.CaptureError) as ctx:
            real_ramcap.SourceConfig("/dev/shm/x11", "/abs/screen0", 8, 8, "-nocursor")
        self.assertEqual(ctx.exception.code, "E_SOURCE_PATH")

        with self.assertRaises(real_ramcap.CaptureError) as ctx:
            real_ramcap.SourceConfig("/dev/shm/x11", "screen0", True, 8, "-nocursor")
        self.assertEqual(ctx.exception.code, "E_CONFIG")

        # 5. gm_bands_hv single-condition exact error messages
        good_luma = np.zeros((32, 32), dtype=np.uint8)
        with self.assertRaises(TypeError) as ctx:
            real_gm.estimate_global_motion(good_luma.astype(np.float32), good_luma, 8)
        self.assertEqual(str(ctx.exception), "luma must be uint8")

        with self.assertRaises(ValueError) as ctx:
            real_gm.estimate_global_motion(good_luma, np.zeros((32, 16), dtype=np.uint8), 8)
        self.assertEqual(str(ctx.exception), "frames must have the same nonempty H by W shape")

        with self.assertRaises(TypeError) as ctx:
            real_gm.consensus_tiles(good_luma, good_luma, 16, True, 0)
        self.assertEqual(str(ctx.exception), "displacements must be integers")

        with self.assertRaises(ValueError) as ctx:
            real_gm.consensus_tiles(good_luma, good_luma, 0, 0, 0)
        self.assertEqual(str(ctx.exception), "tile side must be a positive integer")

    def test_mutation_harness_outcome_separation_and_fuzzer_self_kill(self):
        # Verify fuzzer self-mutation kill on SplitMix64.next_u64
        def check_fuzzer_determinism(mod: types.ModuleType) -> None:
            r1 = mod.SplitMix64(42)
            assert r1.next_u64() == 0xBDD732262FEB6E95, "SplitMix64 word mismatch"

        kill_spec = MutantSiteSpec(
            mutant_id="M-fuzzer-splitmix-gamma",
            module_rel_path="eval/v2eval/frame_property_fuzzer.py",
            package_name="eval.v2eval",
            real_module=fuzzer,
            qualified_scope="SplitMix64.next_u64",
            containing_expr_src="self._state + GOLDEN_GAMMA & MASK64",
            structural_selector="left:BinOp/right:Name",
            expected_anchor_src="GOLDEN_GAMMA",
            transformation_label="GOLDEN_GAMMA -> MIX1",
            mutator=lambda node: ast.Name(id="MIX1", ctx=ast.Load()),
            behavioral_check=check_fuzzer_determinism,
        )
        res_kill = execute_mutant_spec(kill_spec)
        self.assertEqual(res_kill["outcome"], OUTCOME_KILLED)

        # Verify failed positive never enters killed numerator
        failed_pos_spec = replace(
            kill_spec,
            mutant_id="M-fuzzer-failed-pos",
            behavioral_check=lambda mod: (_ for _ in ()).throw(AssertionError("boom on real")),
        )
        res_pos = execute_mutant_spec(failed_pos_spec)
        self.assertEqual(res_pos["outcome"], OUTCOME_POSITIVE_FAILED)
        self.assertNotEqual(res_pos["outcome"], OUTCOME_KILLED)

        # Verify unrelated exception stays separate from killed
        unrelated_spec = replace(
            kill_spec,
            mutant_id="M-fuzzer-unrelated-exc",
            mutator=lambda node: ast.BinOp(left=ast.Constant(value=1), op=ast.Div(), right=ast.Constant(value=0)),
        )
        res_unrel = execute_mutant_spec(unrelated_spec)
        self.assertEqual(res_unrel["outcome"], OUTCOME_UNRELATED_EXCEPTION)
        self.assertNotEqual(res_unrel["outcome"], OUTCOME_KILLED)

        # Verify missing anchor stays separate from killed
        missing_spec = replace(
            kill_spec,
            mutant_id="M-fuzzer-missing-anchor",
            containing_expr_src="nonexistent_expression_xyz()",
        )
        res_missing = execute_mutant_spec(missing_spec)
        self.assertEqual(res_missing["outcome"], OUTCOME_MISSING_ANCHOR)
        self.assertNotEqual(res_missing["outcome"], OUTCOME_KILLED)

if __name__ == "__main__":
    unittest.main()
