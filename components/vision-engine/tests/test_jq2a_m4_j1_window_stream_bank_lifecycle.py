"""Contract tests for WindowStreamBank lifecycle, ring separation, quota admission,
epoch advancement, and Gate G M4 1 conformance.

Task ID: jq2a-m4-j1-window-stream-bank-lifecycle
Owning implementation task_id: jq2a-m4-c1-stream-bank
"""

import unittest
import numpy as np
from vadapt import rtring


def _import_owner():
    """Import target owner module symbols, raising ImportError if absent."""
    from vadapt.window_stream_bank import (
        WindowStreamBank,
        Handle,
        StreamSpec,
        StreamToken,
        FrameRef,
        Drop,
        StaleStreamError,
        check_lock_order,
    )

    return {
        "WindowStreamBank": WindowStreamBank,
        "Handle": Handle,
        "StreamSpec": StreamSpec,
        "StreamToken": StreamToken,
        "FrameRef": FrameRef,
        "Drop": Drop,
        "StaleStreamError": StaleStreamError,
        "check_lock_order": check_lock_order,
    }


class TestWindowStreamBankLifecycle(unittest.TestCase):
    """Contract tests for WindowStreamBank ring separation, handle generation, epoch advancement,
    and Gate G M4 1 positive controls and negative mutants."""

    def _close_token(self, bank, token):
        """Cleanup may encounter an intentionally closed or resized token."""
        try:
            bank.close(token)
        except _import_owner()["StaleStreamError"]:
            pass

    def test_identical_geometry_different_handles_separate_rings(self):
        """INV-M4-1: Open separate single-writer rings for identical geometry but different handles."""
        symbols = _import_owner()
        WSB = symbols["WindowStreamBank"]
        Handle = symbols["Handle"]
        StreamSpec = symbols["StreamSpec"]
        FrameRef = symbols["FrameRef"]

        bank = WSB(quota_bytes=100 * 1024 * 1024)
        self.addCleanup(lambda: self.assertEqual(bank.reserved_bytes, 0))

        h1 = Handle(kind="x11_window", canonical_id="win-101", generation=1)
        h2 = Handle(kind="x11_window", canonical_id="win-102", generation=1)

        spec = StreamSpec(width=640, height=480, slots=4, quota_bytes=10 * 1024 * 1024)

        t1 = bank.open(h1, spec)
        self.addCleanup(self._close_token, bank, t1)
        t2 = bank.open(h2, spec)
        self.addCleanup(self._close_token, bank, t2)

        self.assertNotEqual(t1.handle, t2.handle)

        rgb1 = np.full((480, 640, 3), 111, dtype=np.uint8)
        rgb2 = np.full((480, 640, 3), 222, dtype=np.uint8)

        bank.publish(t1, rgb1, frame_seq=1)
        bank.publish(t2, rgb2, frame_seq=1)

        res1 = bank.read(t1)
        res2 = bank.read(t2)

        self.assertIsInstance(res1, FrameRef)
        self.assertIsInstance(res2, FrameRef)

        self.assertEqual(res1.handle, h1)
        self.assertEqual(res2.handle, h2)
        self.assertEqual(int(res1.rgb[0, 0, 0]), 111)
        self.assertEqual(int(res2.rgb[0, 0, 0]), 222)

    def test_close_and_respawn_target_and_resize_sibling(self):
        """Close and respawn one target (advancing generation) and resize another target (advancing epoch)."""
        symbols = _import_owner()
        WSB = symbols["WindowStreamBank"]
        Handle = symbols["Handle"]
        StreamSpec = symbols["StreamSpec"]
        FrameRef = symbols["FrameRef"]
        StaleStreamError = symbols["StaleStreamError"]

        bank = WSB(quota_bytes=100 * 1024 * 1024)
        self.addCleanup(lambda: self.assertEqual(bank.reserved_bytes, 0))

        h1_gen1 = Handle(kind="x11_window", canonical_id="win-201", generation=1)
        h2_gen1 = Handle(kind="x11_window", canonical_id="win-202", generation=1)

        spec_a = StreamSpec(width=320, height=240, slots=4, quota_bytes=5 * 1024 * 1024)
        spec_b = StreamSpec(width=640, height=480, slots=4, quota_bytes=10 * 1024 * 1024)

        tok1_gen1 = bank.open(h1_gen1, spec_a)
        self.addCleanup(self._close_token, bank, tok1_gen1)
        tok2_epoch0 = bank.open(h2_gen1, spec_a)
        self.addCleanup(self._close_token, bank, tok2_epoch0)
        self.assertEqual(tok1_gen1.epoch, 0)
        self.assertEqual(tok2_epoch0.epoch, 0)

        # Publish initial frame to sibling tok2_epoch0
        rgb_sibling = np.full((240, 320, 3), 50, dtype=np.uint8)
        bank.publish(tok2_epoch0, rgb_sibling, frame_seq=1)

        # Close tok1_gen1
        bank.close(tok1_gen1)

        # Respawn target 1 with new generation handle
        h1_gen2 = Handle(kind="x11_window", canonical_id="win-201", generation=2)
        tok1_gen2 = bank.open(h1_gen2, spec_a)
        self.addCleanup(self._close_token, bank, tok1_gen2)
        self.assertEqual(tok1_gen2.epoch, 0)

        # Resize target 2 (advancing epoch)
        tok2_epoch1 = bank.resize(tok2_epoch0, spec_b)
        self.addCleanup(self._close_token, bank, tok2_epoch1)

        self.assertNotEqual(tok2_epoch0.epoch, tok2_epoch1.epoch)
        self.assertGreater(tok2_epoch1.epoch, tok2_epoch0.epoch)

        # Old generation token (tok1_gen1) must refuse reads/publishes
        with self.assertRaises(StaleStreamError):
            bank.publish(tok1_gen1, rgb_sibling, frame_seq=2)

        self.assertEqual(tok1_gen1.handle, h1_gen1)
        with self.assertRaises(StaleStreamError):
            bank.read(tok1_gen1)

        # Stale token tok2_epoch0 must be refused after resize
        with self.assertRaises(StaleStreamError):
            bank.publish(tok2_epoch0, rgb_sibling, frame_seq=2)

        # Unaffected sibling tok2_epoch1 retains independent stream and allows publication
        rgb_new = np.full((480, 640, 3), 99, dtype=np.uint8)
        bank.publish(tok2_epoch1, rgb_new, frame_seq=2)

        res_sibling = bank.read(tok2_epoch1)
        self.assertIsInstance(res_sibling, FrameRef)
        self.assertEqual(res_sibling.handle, h2_gen1)
        self.assertEqual(res_sibling.epoch, tok2_epoch1.epoch)
        self.assertEqual(int(res_sibling.rgb[0, 0, 0]), 99)

    def test_gate_g_m4_1_closed_incarnation_negative_mutant_vs_positive_control(self):
        """Gate G M4 1: Rings and geometry epochs never alias.
        Negative mutant: Reusing closed incarnation token fails/refuses.
        Positive control: Fresh stream read matches pixels."""
        symbols = _import_owner()
        WSB = symbols["WindowStreamBank"]
        Handle = symbols["Handle"]
        StreamSpec = symbols["StreamSpec"]
        FrameRef = symbols["FrameRef"]
        StaleStreamError = symbols["StaleStreamError"]

        bank = WSB(quota_bytes=50 * 1024 * 1024)
        self.addCleanup(lambda: self.assertEqual(bank.reserved_bytes, 0))

        h_old = Handle(kind="cdp_tab", canonical_id="tab-777", generation=1)
        spec = StreamSpec(width=100, height=100, slots=2, quota_bytes=2 * 1024 * 1024)

        tok_closed = bank.open(h_old, spec)
        self.addCleanup(self._close_token, bank, tok_closed)
        rgb_old = np.full((100, 100, 3), 12, dtype=np.uint8)
        bank.publish(tok_closed, rgb_old, frame_seq=1)

        # Close closed incarnation
        bank.close(tok_closed)

        # Negative mutant check: Reusing closed incarnation token fails/refuses
        with self.assertRaises(StaleStreamError):
            bank.publish(tok_closed, rgb_old, frame_seq=2)

        self.assertEqual(tok_closed.handle, h_old)
        with self.assertRaises(StaleStreamError) as refused:
            bank.read(tok_closed)
        self.assertIn("closed", str(refused.exception).lower())

        # Positive control check: Respawn target with fresh generation, open fresh token
        h_fresh = Handle(kind="cdp_tab", canonical_id="tab-777", generation=2)
        tok_fresh = bank.open(h_fresh, spec)
        self.addCleanup(self._close_token, bank, tok_fresh)

        rgb_fresh = np.full((100, 100, 3), 24, dtype=np.uint8)
        bank.publish(tok_fresh, rgb_fresh, frame_seq=1)

        read_fresh = bank.read(tok_fresh)
        self.assertIsInstance(read_fresh, FrameRef)
        self.assertEqual(read_fresh.handle, h_fresh)
        self.assertEqual(read_fresh.frame_seq, 1)
        self.assertEqual(int(read_fresh.rgb[0, 0, 0]), 24)

    def test_transactional_quota_admission_and_rollback(self):
        """INV-M4-2: Quota admission is transactional across requested rings.
        Exceeding quota limits rolls back without releasing pre-existing reservations."""
        symbols = _import_owner()
        WSB = symbols["WindowStreamBank"]
        Handle = symbols["Handle"]
        StreamSpec = symbols["StreamSpec"]

        # The ledger charges physical ring bytes, not the per-stream limit.
        size = rtring.ring_bytes(4, 640, 480)
        bank = WSB(quota_bytes=2 * size - 1)
        self.addCleanup(lambda: self.assertEqual(bank.reserved_bytes, 0))

        h1 = Handle(kind="xvfb_screen", canonical_id="scr-1", generation=1)
        spec1 = StreamSpec(width=640, height=480, slots=4, quota_bytes=size)
        tok1 = bank.open(h1, spec1)
        self.addCleanup(self._close_token, bank, tok1)
        self.assertEqual(bank.reserved_bytes, size)

        # A second exact-size ring exceeds the shared budget by one byte.
        h2 = Handle(kind="xvfb_screen", canonical_id="scr-2", generation=1)
        spec2 = StreamSpec(width=640, height=480, slots=4, quota_bytes=size)

        with self.assertRaises((ValueError, RuntimeError)):
            bank.open(h2, spec2)
        self.assertEqual(bank.reserved_bytes, size)

        # Pre-existing reservation for h1 remains intact and operational
        rgb1 = np.full((480, 640, 3), 77, dtype=np.uint8)
        bank.publish(tok1, rgb1, frame_seq=1)
        ref1 = bank.read(tok1)
        self.assertEqual(ref1.handle, h1)

    def test_canonical_handle_ordering_for_multi_stream_lock(self):
        """Group operations validate canonical handle ordering before taking per-stream metadata locks.
        check_lock_order(handles) rejects reversed handle order without blocking."""
        symbols = _import_owner()
        check_lock_order = symbols["check_lock_order"]
        Handle = symbols["Handle"]

        h_a = Handle(kind="capsule", canonical_id="cap-a", generation=1)
        h_b = Handle(kind="capsule", canonical_id="cap-b", generation=1)

        # The exported pure guard does not require a bank instance.
        self.assertTrue(check_lock_order([h_a, h_b]))
        with self.assertRaises(ValueError):
            check_lock_order([h_b, h_a])

    def test_step_hook_invocation_and_copy_revalidation(self):
        """INV-M4-3: Invoke step_hook at before_copy and after_copy, permitting fixtures to alter sequence or CRC."""
        symbols = _import_owner()
        WSB = symbols["WindowStreamBank"]
        Handle = symbols["Handle"]
        StreamSpec = symbols["StreamSpec"]
        FrameRef = symbols["FrameRef"]
        Drop = symbols["Drop"]

        hook_calls = []

        def trace_hook(phase):
            hook_calls.append(phase)

        bank = WSB(quota_bytes=50 * 1024 * 1024, step_hook=trace_hook)
        self.addCleanup(lambda: self.assertEqual(bank.reserved_bytes, 0))

        h = Handle(kind="godot_window", canonical_id="godot-1", generation=1)
        spec = StreamSpec(width=100, height=100, slots=2, quota_bytes=2 * 1024 * 1024)

        tok = bank.open(h, spec)
        self.addCleanup(self._close_token, bank, tok)
        rgb = np.full((100, 100, 3), 88, dtype=np.uint8)

        bank.publish(tok, rgb, frame_seq=1)

        ref = bank.read(tok)
        self.assertIsInstance(ref, FrameRef)
        self.assertIn("before_copy", hook_calls)
        self.assertIn("after_copy", hook_calls)
        self.assertEqual(hook_calls, ["before_copy", "after_copy"])


if __name__ == "__main__":
    unittest.main()
