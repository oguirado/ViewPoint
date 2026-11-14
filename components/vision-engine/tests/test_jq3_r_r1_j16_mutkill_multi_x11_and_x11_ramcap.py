"""tests/test_jq3_r_r1_j16_mutkill_multi_x11_and_x11_ramcap.py

Contract, boundary, and mutation-killing tests for vadapt/multi_x11_ramcap.py
and vadapt/x11_ramcap.py.

All 15 mutations are executed in isolated dynamically compiled module instances
and verified to trigger an AssertionError on the exact same behavioral assertion
that passes on unmutated source.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import importlib
import importlib.util
import os
import re
import sys
import tempfile
import types
import unittest
import zlib

import numpy as np

import vadapt.multi_x11_ramcap as mx11
import vadapt.x11_ramcap as x11
from vadapt import ramws, rtring, xfb


@dataclasses.dataclass(frozen=True)
class _StructuralHandle:
    kind: str
    canonical_id: str
    generation: int


def _make_test_ring(width: int, height: int, slots: int = 2) -> bytearray:
    """Build a valid rtring v2 buffer with 0s or dummy frames."""
    size = rtring.ring_bytes(slots, width, height)
    buf = bytearray(size)
    mx11._GLOBAL_HEADER.pack_into(
        buf, 0, rtring.MAGIC, rtring.VERSION, slots, width, height,
        width * height * 3, rtring.data_off(slots), 0
    )
    return buf


def _write_frame_to_ring(buf: bytearray, width: int, height: int, slots: int,
                         slot_seq: int, image: np.ndarray) -> None:
    """Write frame `slot_seq` into `buf` with valid CRC, headers, and head."""
    slot = slot_seq % slots
    hdr_off = rtring.HDR + slot * rtring.SLOT_HDR
    pixel_off = rtring.data_off(slots) + slot * width * height * 3
    pixels = image.tobytes(order="C")

    # even seq -> seq & index -> payload -> CRC -> head
    mx11._UINT64.pack_into(buf, hdr_off, (slot_seq * 2) & mx11._UINT64_MAX)
    mx11._UINT64.pack_into(buf, hdr_off + 8, slot_seq)
    buf[pixel_off:pixel_off + len(pixels)] = pixels
    mx11._UINT32.pack_into(buf, hdr_off + rtring.CRC_OFF, zlib.crc32(pixels))
    mx11._UINT64.pack_into(buf, hdr_off, (slot_seq * 2 + 2) & mx11._UINT64_MAX)
    mx11._UINT64.pack_into(buf, rtring.HEAD_OFF, slot_seq + 1)


def _compile_mutant(filepath: str, module_name: str, transformer_fn) -> types.ModuleType:
    """Load module source, apply AST/string mutation via transformer_fn, and compile in isolation."""
    with open(filepath, "r", encoding="utf-8") as f:
        src = f.read()

    mutated_src = transformer_fn(src)
    if mutated_src == src:
        raise ValueError(f"Mutation transformer for {module_name} made no changes!")

    prev_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_loader(module_name, loader=None)
        mut_mod = importlib.util.module_from_spec(spec)
        mut_mod.__file__ = filepath
        mut_mod.__name__ = module_name
        sys.modules[module_name] = mut_mod

        # Execute mutated module code
        code = compile(mutated_src, filepath, "exec")
        exec(code, mut_mod.__dict__)
        return mut_mod
    finally:
        sys.dont_write_bytecode = prev_dont_write_bytecode


class TestJq3R1J16MutkillMultiX11AndX11Ramcap(unittest.TestCase):

    def tearDown(self):
        # Clean up any temporary mutant modules registered in sys.modules
        for k in list(sys.modules.keys()):
            if k.startswith("mut_"):
                sys.modules.pop(k, None)

    # -------------------------------------------------------------------------
    # PART 1: Functional and Boundary Tests (Real Code)
    # -------------------------------------------------------------------------

    def test_asymmetric_crop_coordinate_swap_protection(self):
        """Asymmetric frame and crop so x/y or width/height coordinate swap fails match."""
        W, H = 100, 50
        spec_w, spec_h = 30, 20
        x, y = 10, 5

        full_img = np.zeros((H, W, 3), dtype=np.uint8)
        for r in range(H):
            for c in range(W):
                full_img[r, c] = [(r * 3) % 256, (c * 5) % 256, (r + c) % 256]

        ring_buf = _make_test_ring(W, H, slots=2)
        _write_frame_to_ring(ring_buf, W, H, slots=2, slot_seq=0, image=full_img)

        handle = mx11.Handle("xvfb_screen", "s1", 1)
        screen = mx11.TrustedScreen(handle, 100, W, H, ring_buf)
        spec = mx11.CropSpec(handle, 100, x, y, spec_w, spec_h, 0)

        class SingleSource:
            def resolve(self, h):
                return screen if h == handle else None

        results = mx11.capture_many([spec], source=SingleSource())
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], mx11.FrameRef)

        ref = results[0]
        expected_crop = full_img[y:y + spec_h, x:x + spec_w]
        np.testing.assert_array_equal(ref.rgb, expected_crop)

        swapped_crop = full_img[x:x + spec_h, y:y + spec_w]
        self.assertFalse(np.array_equal(ref.rgb, swapped_crop))

    def test_sequence_exhaustion_arithmetic_and_quota_admission_boundaries(self):
        """Sequence exhaustion raises CaptureFailure or Drop; quota admission enforces limit."""
        quota = rtring.ring_bytes(2, 10, 10)
        bank = mx11.MemoryStreamBank(quota_bytes=quota, slots=2)

        h1 = mx11.Handle("xvfb_screen", "disp1", 1)
        spec1 = mx11.CropSpec(h1, 1, 0, 0, 10, 10, 0)
        spec3 = mx11.CropSpec(mx11.Handle("xvfb_screen", "disp2", 1), 1, 0, 0, 10, 10, 0)

        bank.admit([(spec1, 10, 10)])
        self.assertEqual(bank.reserved_bytes, quota)

        with self.assertRaises(mx11.CaptureFailure) as cm:
            bank.admit([(spec3, 10, 10)])
        self.assertEqual(cm.exception.reason, "quota")

        full_img = np.zeros((10, 10, 3), dtype=np.uint8)
        bank.publish(spec1, full_img)

        with self.assertRaises(mx11.CaptureFailure) as cm:
            bank.publish(spec1, full_img)
        self.assertEqual(cm.exception.reason, "stale_sequence")

    def test_token_field_order_and_descriptor_identity(self):
        """_token produces StreamToken with expected ordering and length prefix formatting."""
        h = mx11.Handle("xvfb_screen", "screen:0", 2)
        spec = mx11.CropSpec(h, 50, 10, 20, 30, 40, 5)
        token = mx11._token(spec)

        self.assertIsInstance(token, mx11.StreamToken)
        self.assertEqual(token.epoch, 50)
        self.assertEqual(token.handle.kind, "xvfb_screen")
        self.assertEqual(token.handle.generation, 2)
        self.assertEqual(token.handle.canonical_id, "8:screen:0:crop:10:20:30:40")

    def test_invalid_retries_and_noncontiguous_ring_layout(self):
        """Invalid retries or noncontiguous ring memory views raise CaptureFailure."""
        W, H = 20, 20
        buf = _make_test_ring(W, H, 2)

        with self.assertRaises(mx11.CaptureFailure) as cm:
            mx11._read_owned(buf, W, H, 0, retries=0)
        self.assertEqual(cm.exception.reason, "invalid_retries")

        f_arr = np.zeros((H, W, 3), dtype=np.uint8, order="F")
        with self.assertRaises(mx11.CaptureFailure) as cm:
            mx11._ring_layout(f_arr, W, H)
        self.assertEqual(cm.exception.reason, "invalid_source_ring")

    # -------------------------------------------------------------------------
    # PART 2: Mutation Killing Tests (M-vadapt-multi-x11-ramcap-1..8 and M-vadapt-x11-ramcap-1..7)
    # -------------------------------------------------------------------------

    def test_kill_m_multi_x11_1_operator_flip_in_publish(self):
        """M-vadapt-multi-x11-ramcap-1: operator flip slot * spec.width -> slot + spec.width at line 333."""
        W, H = 20, 10
        img = np.full((H, W, 3), 123, dtype=np.uint8)

        def run_assertion(m_mod):
            bank = m_mod.MemoryStreamBank(quota_bytes=100000, slots=3)
            h = m_mod.Handle("xvfb_screen", "s1", 1)
            spec = m_mod.CropSpec(h, 1, 0, 0, W, H, frame_seq=1)
            bank.admit([(spec, W, H)])
            try:
                ref = bank.publish(spec, img)
                if not np.array_equal(ref.rgb, img):
                    raise AssertionError("Published RGB does not match input image")
            except m_mod.CaptureFailure as exc:
                raise AssertionError(f"publish failed with CaptureFailure: {exc}")

        run_assertion(mx11)

        def transform(src: str) -> str:
            anchor = "slot * spec.width * spec.height * 3"
            replace = "slot + spec.width * spec.height * 3"
            self.assertEqual(src.count(anchor), 1)
            return src.replace(anchor, replace, 1)

        mut_mod = _compile_mutant(mx11.__file__, "mut_mx11_1", transform)
        with self.assertRaises(AssertionError):
            run_assertion(mut_mod)

    def test_kill_m_multi_x11_2_boundary_increment_in_read_owned(self):
        """M-vadapt-multi-x11-ramcap-2: boundary increment head_before - 1 -> head_before - 0 at line 196."""
        W, H = 10, 10
        slots = 2
        img = np.full((H, W, 3), 77, dtype=np.uint8)

        buf2 = _make_test_ring(W, H, slots=2)
        _write_frame_to_ring(buf2, W, H, slots=2, slot_seq=0, image=img)
        _write_frame_to_ring(buf2, W, H, slots=2, slot_seq=1, image=img)

        def run_assertion(m_mod):
            try:
                out = m_mod._read_owned(buf2, W, H, requested=0)
                if not isinstance(out, np.ndarray):
                    raise AssertionError("Expected ndarray from _read_owned")
            except m_mod.CaptureFailure as exc:
                raise AssertionError(f"_read_owned failed unexpectedly: {exc.reason}")

        run_assertion(mx11)

        def transform(src: str) -> str:
            anchor = "if head_before - 1 - requested >= slots:"
            replace = "if head_before - requested >= slots:"
            self.assertEqual(src.count(anchor), 1)
            return src.replace(anchor, replace, 1)

        mut_mod = _compile_mutant(mx11.__file__, "mut_mx11_2", transform)
        with self.assertRaises(AssertionError):
            run_assertion(mut_mod)

    def test_kill_m_multi_x11_3_boundary_increment_in_bank_init(self):
        """M-vadapt-multi-x11-ramcap-3: boundary increment slots limit at line 262."""
        def run_assertion(m_mod):
            try:
                b = m_mod.MemoryStreamBank(quota_bytes=1000, slots=(1 << 32) - 1)
                if b.slots != (1 << 32) - 1:
                    raise AssertionError("Bank slots not stored properly")
            except ValueError:
                raise AssertionError("MemoryStreamBank rejected UINT32_MAX slots")

            try:
                m_mod.MemoryStreamBank(quota_bytes=1000, slots=1 << 32)
            except ValueError:
                pass
            else:
                raise AssertionError("MemoryStreamBank accepted slots > UINT32_MAX")

        run_assertion(mx11)

        def transform(src: str) -> str:
            anchor = "slots > (1 << 32) - 1"
            replace = "slots > (1 << 32) - 2"
            self.assertEqual(src.count(anchor), 1)
            return src.replace(anchor, replace, 1)

        mut_mod = _compile_mutant(mx11.__file__, "mut_mx11_3", transform)
        with self.assertRaises(AssertionError):
            run_assertion(mut_mod)

    def test_kill_m_multi_x11_4_drop_check_in_read_owned(self):
        """M-vadapt-multi-x11-ramcap-4: drop check in _read_owned at line 188; anchor not _index(retries, positive=True);."""
        W, H = 10, 10
        buf = _make_test_ring(W, H, 2)

        def run_assertion(m_mod):
            try:
                m_mod._read_owned(buf, W, H, 0, retries=0)
            except m_mod.CaptureFailure as exc:
                if exc.reason != "invalid_retries":
                    raise AssertionError(f"Expected invalid_retries, got {exc.reason}")
            else:
                raise AssertionError("retries=0 was allowed without CaptureFailure")

        run_assertion(mx11)

        def transform(src: str) -> str:
            anchor = "if not _index(retries, positive=True):"
            replace = "if False:"
            self.assertEqual(src.count(anchor), 1)
            return src.replace(anchor, replace, 1)

        mut_mod = _compile_mutant(mx11.__file__, "mut_mx11_4", transform)
        with self.assertRaises(AssertionError):
            run_assertion(mut_mod)

    def test_kill_m_multi_x11_5_drop_check_in_ring_layout(self):
        """M-vadapt-multi-x11-ramcap-5: drop check in _ring_layout at line 162; anchor view.ndim != 1 or view.itemsize != 1 or not view.c_contiguous."""
        W, H = 10, 10
        f_arr = np.zeros((H, W, 3), dtype=np.uint8, order="F")

        def run_assertion(m_mod):
            try:
                m_mod._ring_layout(f_arr, W, H)
            except m_mod.CaptureFailure as exc:
                if exc.reason != "invalid_source_ring":
                    raise AssertionError(f"Expected invalid_source_ring, got {exc.reason}")
            except (BufferError, TypeError, ValueError) as exc:
                raise AssertionError(f"Expected CaptureFailure('invalid_source_ring'), got {type(exc).__name__}: {exc}")
            else:
                raise AssertionError("Fortran array was accepted without CaptureFailure")

        run_assertion(mx11)

        def transform(src: str) -> str:
            anchor = "if view.ndim != 1 or view.itemsize != 1 or not view.c_contiguous:"
            replace = "if False:"
            self.assertEqual(src.count(anchor), 1)
            return src.replace(anchor, replace, 1)

        mut_mod = _compile_mutant(mx11.__file__, "mut_mx11_5", transform)
        with self.assertRaises(AssertionError):
            run_assertion(mut_mod)

    def test_kill_m_multi_x11_6_drop_check_in_check_geometry(self):
        """M-vadapt-multi-x11-ramcap-6: drop kind check in _check_request, reached by _check_geometry; anchor key[0] != "xvfb_screen"."""
        h = _StructuralHandle("non_xvfb_kind", "s1", 1)
        buf = _make_test_ring(10, 10, 2)

        def run_assertion(m_mod):
            spec = m_mod.CropSpec(h, 1, 0, 0, 10, 10, 0)
            screen = m_mod.TrustedScreen(h, 1, 10, 10, buf)
            try:
                m_mod._check_geometry(spec, screen)
            except m_mod.CaptureFailure as exc:
                if exc.reason != "unsupported_mode":
                    raise AssertionError(f"Expected unsupported_mode, got {exc.reason}")
            else:
                raise AssertionError("non_xvfb_kind was accepted without unsupported_mode failure")

        run_assertion(mx11)

        def transform(src: str) -> str:
            anchor = 'if key[0] != "xvfb_screen":'
            replace = "if False:"
            self.assertEqual(src.count(anchor), 1)
            return src.replace(anchor, replace, 1)

        mut_mod = _compile_mutant(mx11.__file__, "mut_mx11_6", transform)
        with self.assertRaises(AssertionError):
            run_assertion(mut_mod)

    def test_kill_m_multi_x11_7_drop_check_in_capture_many(self):
        """M-vadapt-multi-x11-ramcap-7: drop check in capture_many at line 425; anchor screen is None."""
        class NullSource:
            def resolve(self, handle):
                return None

        def run_assertion(m_mod):
            h = m_mod.Handle("xvfb_screen", "s1", 1)
            spec = m_mod.CropSpec(h, 1, 0, 0, 10, 10, 0)
            try:
                res = m_mod.capture_many([spec], source=NullSource())
                if not isinstance(res[0], m_mod.Drop) or res[0].reason != "source_unavailable":
                    raise AssertionError(f"Expected Drop(source_unavailable), got {res[0]}")
            except (AttributeError, TypeError) as exc:
                raise AssertionError(f"Expected Drop(source_unavailable), got exception {type(exc).__name__}: {exc}")

        run_assertion(mx11)

        def transform(src: str) -> str:
            anchor = "if screen is None:"
            replace = "if False:"
            self.assertEqual(src.count(anchor), 1)
            return src.replace(anchor, replace, 1)

        mut_mod = _compile_mutant(mx11.__file__, "mut_mx11_7", transform)
        with self.assertRaises(AssertionError):
            run_assertion(mut_mod)

    def test_kill_m_multi_x11_8_swap_arguments_in_token(self):
        """M-vadapt-multi-x11-ramcap-8: swap arguments in _token at line 132; anchor StreamToken(Handle(kind, identifier, generation), spec.epoch)."""
        h = mx11.Handle("xvfb_screen", "screen1", 2)
        spec = mx11.CropSpec(h, epoch=99, x=5, y=10, width=15, height=20, frame_seq=0)

        def run_assertion(m_mod):
            tok = m_mod._token(spec)
            if not isinstance(tok.handle, m_mod.Handle):
                raise AssertionError("token.handle is not a Handle instance")
            if tok.epoch != 99:
                raise AssertionError(f"token.epoch is {tok.epoch}, expected 99")
            if tok.handle.canonical_id != "7:screen1:crop:5:10:15:20":
                raise AssertionError(f"token.handle.canonical_id is {tok.handle.canonical_id}")

        run_assertion(mx11)

        def transform(src: str) -> str:
            anchor = "return StreamToken(Handle(kind, identifier, generation), spec.epoch)"
            replace = "return StreamToken(spec.epoch, Handle(kind, identifier, generation))"
            self.assertEqual(src.count(anchor), 1)
            return src.replace(anchor, replace, 1)

        mut_mod = _compile_mutant(mx11.__file__, "mut_mx11_8", transform)
        with self.assertRaises(AssertionError):
            run_assertion(mut_mod)

    def test_kill_m_x11_1_boundary_increment_left_in_capture(self):
        """M-vadapt-x11-ramcap-1: boundary increment in RamCapture._capture at line 451; anchor 1 << 64 -> 1 << 65."""
        def run_assertion(m_mod):
            s = m_mod._Session(
                session_id="s1", alias="a1", params=(), config=None, size=100,
                expires_ns=2000000000000, interval_ns=1000, cancel=m_mod.threading.Event()
            )
            s.state = "running"
            s.frames = (1 << 64) - 1

            class MockSource:
                def read(self, guard):
                    return b""
                def check(self):
                    pass

            s.source = MockSource()

            cap = object.__new__(m_mod.RamCapture)
            cap.clock = lambda: 1000
            cap._clock_lock = m_mod.threading.Lock()
            cap._last_clock = None

            orig_rgb24 = m_mod.xfb.rgb24
            m_mod.xfb.rgb24 = lambda raw: b"\x00" * 300

            try:
                cap._capture(s, should_wait=False)
            except m_mod.CaptureError as exc:
                if exc.code != "E_BUDGET":
                    raise AssertionError(f"Expected E_BUDGET, got {exc.code}")
            except (AttributeError, TypeError) as exc:
                raise AssertionError(f"Expected E_BUDGET exception, got {type(exc).__name__}: {exc}")
            else:
                raise AssertionError("frames == (1<<64)-1 did not trigger E_BUDGET exception")
            finally:
                m_mod.xfb.rgb24 = orig_rgb24

        run_assertion(x11)

        def transform(src: str) -> str:
            anchor = "if s.frames >= (1 << 64) - 1:"
            replace = "if s.frames >= (1 << 65) - 1:"
            self.assertEqual(src.count(anchor), 1)
            return src.replace(anchor, replace, 1)

        mut_mod = _compile_mutant(x11.__file__, "mut_x11_1", transform)
        with self.assertRaises(AssertionError):
            run_assertion(mut_mod)

    def test_kill_m_x11_2_boundary_increment_right_in_capture(self):
        """M-vadapt-x11-ramcap-2: boundary increment right in RamCapture._capture at line 451; anchor (1 << 64) - 1 -> (1 << 64)."""
        def run_assertion(m_mod):
            s = m_mod._Session(
                session_id="s1", alias="a1", params=(), config=None, size=100,
                expires_ns=2000000000000, interval_ns=1000, cancel=m_mod.threading.Event()
            )
            s.state = "running"
            s.frames = (1 << 64) - 1

            class MockSource:
                def read(self, guard):
                    return b""
                def check(self):
                    pass

            s.source = MockSource()

            cap = object.__new__(m_mod.RamCapture)
            cap.clock = lambda: 1000
            cap._clock_lock = m_mod.threading.Lock()
            cap._last_clock = None

            orig_rgb24 = m_mod.xfb.rgb24
            m_mod.xfb.rgb24 = lambda raw: b"\x00" * 300

            try:
                cap._capture(s, should_wait=False)
            except m_mod.CaptureError as exc:
                if exc.code != "E_BUDGET":
                    raise AssertionError(f"Expected E_BUDGET, got {exc.code}")
            except (AttributeError, TypeError) as exc:
                raise AssertionError(f"Expected E_BUDGET exception, got {type(exc).__name__}: {exc}")
            else:
                raise AssertionError("frames == (1<<64)-1 did not trigger E_BUDGET exception")
            finally:
                m_mod.xfb.rgb24 = orig_rgb24

        run_assertion(x11)

        def transform(src: str) -> str:
            anchor = "if s.frames >= (1 << 64) - 1:"
            replace = "if s.frames >= (1 << 64):"
            self.assertEqual(src.count(anchor), 1)
            return src.replace(anchor, replace, 1)

        mut_mod = _compile_mutant(x11.__file__, "mut_x11_2", transform)
        with self.assertRaises(AssertionError):
            run_assertion(mut_mod)

    def test_kill_m_x11_3_drop_check_in_source_check(self):
        """M-vadapt-x11-ramcap-3: drop check in _Source.check at line 149; anchor self.fd is None or _identity(self.fd) != self.identity."""
        def run_assertion(m_mod):
            src_obj = object.__new__(m_mod._Source)
            src_obj.fd = None
            src_obj.identity = (1, 2, 3)
            src_obj.config = None
            src_obj.layout = None

            try:
                src_obj.check()
            except m_mod.CaptureError as exc:
                if exc.code != "E_SOURCE_CHANGED":
                    raise AssertionError(f"Expected E_SOURCE_CHANGED, got {exc.code}")
            except (AttributeError, TypeError) as exc:
                raise AssertionError(f"Expected CaptureError('E_SOURCE_CHANGED'), got {type(exc).__name__}: {exc}")
            else:
                raise AssertionError("self.fd = None in _Source.check() did not raise CaptureError")

        run_assertion(x11)

        def transform(src: str) -> str:
            anchor = "if self.fd is None or _identity(self.fd) != self.identity:"
            replace = "if False:"
            self.assertEqual(src.count(anchor), 1)
            return src.replace(anchor, replace, 1)

        mut_mod = _compile_mutant(x11.__file__, "mut_x11_3", transform)
        with self.assertRaises(AssertionError):
            run_assertion(mut_mod)

    def test_kill_m_x11_4_drop_check_in_check_workspace(self):
        """M-vadapt-x11-ramcap-4: drop check in RamCapture._check_workspace at line 331; anchor os.path.realpath(path) != os.path.join(root, rtring.REL)."""
        class MockWS:
            root = "/tmp/fake_root"
            def path(self, rel):
                return "/tmp/fake_root/other_dir/escaped_ring"

        def run_assertion(m_mod):
            orig_ws_root = m_mod.RamCapture._workspace_root
            m_mod.RamCapture._workspace_root = classmethod(lambda cls, ws: "/tmp/fake_root")

            try:
                try:
                    m_mod.RamCapture._check_workspace(MockWS())
                except m_mod.CaptureError as exc:
                    if exc.code != "E_RAM_ESCAPE" or "ring path escapes its workspace" not in str(exc):
                        raise AssertionError(f"Expected 'ring path escapes its workspace', got {exc}")
                else:
                    raise AssertionError("Escaped workspace path was allowed without E_RAM_ESCAPE")
            finally:
                m_mod.RamCapture._workspace_root = orig_ws_root

        run_assertion(x11)

        def transform(src: str) -> str:
            anchor = "if os.path.realpath(path) != os.path.join(root, rtring.REL):"
            replace = "if False:"
            self.assertEqual(src.count(anchor), 1)
            return src.replace(anchor, replace, 1)

        mut_mod = _compile_mutant(x11.__file__, "mut_x11_4", transform)
        with self.assertRaises(AssertionError):
            run_assertion(mut_mod)

    def test_kill_m_x11_5_drop_check_in_name(self):
        """M-vadapt-x11-ramcap-5: drop check in _name at line 39; anchor type(value) is not str or re.fullmatch(...)."""
        def run_assertion(m_mod):
            try:
                m_mod._name("bad session name!")
            except m_mod.CaptureError as exc:
                if exc.code != "E_CONFIG":
                    raise AssertionError(f"Expected E_CONFIG, got {exc.code}")
            else:
                raise AssertionError("_name accepted invalid name with spaces")

        run_assertion(x11)

        def transform(src: str) -> str:
            anchor = 'if type(value) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is None:'
            replace = "if False:"
            self.assertEqual(src.count(anchor), 1)
            return src.replace(anchor, replace, 1)

        mut_mod = _compile_mutant(x11.__file__, "mut_x11_5", transform)
        with self.assertRaises(AssertionError):
            run_assertion(mut_mod)

    def test_kill_m_x11_6_swap_arguments_in_check_workspace(self):
        """M-vadapt-x11-ramcap-6: swap arguments in RamCapture._check_workspace at line 341; anchor CaptureError("E_RAM_ESCAPE", "unsafe workspace path")."""
        class ErrWS:
            root = "/tmp/nonexistent_dir_xyz_123"
            def path(self, rel):
                raise OSError("Directory missing")

        def run_assertion(m_mod):
            orig_ws_root = m_mod.RamCapture._workspace_root
            m_mod.RamCapture._workspace_root = classmethod(lambda cls, ws: "/tmp/nonexistent_dir_xyz_123")
            try:
                try:
                    m_mod.RamCapture._check_workspace(ErrWS())
                except m_mod.CaptureError as exc:
                    if exc.code != "E_RAM_ESCAPE":
                        raise AssertionError(f"Expected exc.code == 'E_RAM_ESCAPE', got {exc.code}")
                else:
                    raise AssertionError("ErrWS did not raise CaptureError")
            finally:
                m_mod.RamCapture._workspace_root = orig_ws_root

        run_assertion(x11)

        def transform(src: str) -> str:
            anchor = 'raise CaptureError("E_RAM_ESCAPE", "unsafe workspace path")'
            replace = 'raise CaptureError("unsafe workspace path", "E_RAM_ESCAPE")'
            self.assertEqual(src.count(anchor), 1)
            return src.replace(anchor, replace, 1)

        mut_mod = _compile_mutant(x11.__file__, "mut_x11_6", transform)
        with self.assertRaises(AssertionError):
            run_assertion(mut_mod)

    def test_kill_m_x11_7_swap_arguments_in_workspace_root(self):
        """M-vadapt-x11-ramcap-7: swap arguments in RamCapture._workspace_root at line 316; anchor CaptureError("E_RAM_ESCAPE", "absolute workspace root required")."""
        class RelativeWS:
            root = "relative/path/ws"

        def run_assertion(m_mod):
            try:
                m_mod.RamCapture._workspace_root(RelativeWS())
            except m_mod.CaptureError as exc:
                if exc.code != "E_RAM_ESCAPE":
                    raise AssertionError(f"Expected exc.code == 'E_RAM_ESCAPE', got {exc.code}")
            else:
                raise AssertionError("Relative workspace root did not raise CaptureError")

        run_assertion(x11)

        def transform(src: str) -> str:
            anchor = 'raise CaptureError("E_RAM_ESCAPE", "absolute workspace root required")'
            replace = 'raise CaptureError("absolute workspace root required", "E_RAM_ESCAPE")'
            self.assertEqual(src.count(anchor), 1)
            return src.replace(anchor, replace, 1)

        mut_mod = _compile_mutant(x11.__file__, "mut_x11_7", transform)
        with self.assertRaises(AssertionError):
            run_assertion(mut_mod)


if __name__ == "__main__":
    unittest.main()
