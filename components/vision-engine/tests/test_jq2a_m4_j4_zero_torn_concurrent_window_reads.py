"""Contract tests for jq2a-m4-j4-zero-torn-concurrent-window-reads.

Target API contract from owning implementation jq2a-m4-c1-stream-bank:
Export WindowStreamBank, Handle, StreamSpec, StreamToken, FrameRef, Drop from vadapt.window_stream_bank.
Implement Gate G M4 4: Torn or overwritten frames never delivered.
INV-M4-1: Separate single-writer rings, increasing per-stream frame sequence, immutable geometry per epoch.
INV-M4-2: Quota admission is transactional across requested rings; account actual reservations exactly once.
INV-M4-3: Stable even sequence, matching render index, repeated sequence validation and CRC verification precede delivery.
"""

import unittest
import numpy as np
from vadapt import rtring


def _get_owner_module():
    import vadapt.window_stream_bank as wsb
    return wsb


class TestZeroTornConcurrentWindowReads(unittest.TestCase):
    """Contract tests for WindowStreamBank zero-torn concurrent window reads."""

    SUPPORTED_KINDS = ("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window")

    def _get_wsb(self, **kwargs):
        mod = _get_owner_module()
        if mod is None:
            self.fail("vadapt.window_stream_bank.WindowStreamBank module/symbol absent")
        bank = mod.WindowStreamBank(**kwargs)
        self.addCleanup(lambda: self.assertEqual(bank.reserved_bytes, 0))
        return bank

    def _close_token(self, bank, token):
        try:
            bank.close(token)
        except self._get_symbols().StaleStreamError:
            pass  # An intentional close/resize has already retired this token.

    def _get_symbols(self):
        mod = _get_owner_module()
        if mod is None:
            self.fail("vadapt.window_stream_bank module absent")
        return mod

    def test_positive_control_stable_ram_slots_delivered(self):
        """Positive control: Stable RAM slots published and read without corruption yield valid FrameRef."""
        mod = self._get_symbols()
        bank = self._get_wsb(quota_bytes=10 * 1024 * 1024)
        h = mod.Handle("x11_window", "win-1", 1)
        spec = mod.StreamSpec(width=64, height=48, slots=4, quota_bytes=1024 * 1024)
        token = bank.open(h, spec)
        self.addCleanup(self._close_token, bank, token)
        self.assertIsNotNone(token)
        self.assertEqual(token.handle, h)

        rgb = np.full((48, 64, 3), 128, dtype=np.uint8)
        bank.publish(token, rgb, frame_seq=1)

        result = bank.read(token)
        self.assertIsInstance(result, mod.FrameRef)
        self.assertEqual(result.handle, h)
        self.assertEqual(result.frame_seq, 1)
        self.assertEqual(result.width, 64)
        self.assertEqual(result.height, 48)
        self.assertTrue(np.array_equal(result.rgb, rgb))
        self.assertTrue(hasattr(result, "digest"))

    def test_gate_g_m4_4_step_hook_torn_frame_detected_and_dropped(self):
        """Gate G M4 4: Step hook alters ring/sequence during read, causing torn frame revalidation failure to return Drop."""
        mod = self._get_symbols()

        # Own the RAM ring supplied through the supported factory seam.
        buffers = []
        def ring_factory(handle, spec):
            buffer = bytearray(rtring.ring_bytes(spec.slots, spec.width, spec.height))
            buffers.append(buffer)
            return buffer

        def corrupting_hook(phase):
            if phase == "after_copy":
                # Change only the copied slot's sequence: a writer is in flight.
                off = rtring.HDR + (10 % spec.slots) * rtring.SLOT_HDR
                sequence = rtring._Q.unpack_from(buffers[0], off)[0]
                rtring._Q.pack_into(buffers[0], off, sequence | 1)

        bank = self._get_wsb(quota_bytes=10 * 1024 * 1024, ring_factory=ring_factory)
        h = mod.Handle("cdp_tab", "tab-101", 1)
        spec = mod.StreamSpec(width=32, height=32, slots=2, quota_bytes=512 * 1024)
        token = bank.open(h, spec)
        self.addCleanup(self._close_token, bank, token)

        rgb = np.zeros((32, 32, 3), dtype=np.uint8)
        bank.publish(token, rgb, frame_seq=10)

        # Execute read with step_hook injecting sequence/CRC corruption
        result = bank.read(token, step_hook=corrupting_hook)
        self.assertIsInstance(result, mod.Drop)
        self.assertEqual(result.handle, h)
        self.assertIn(result.reason, ("torn", "lapped"))
        self.assertEqual(result.reason, "torn")

    def test_inv_m4_1_separate_single_writer_rings_and_supported_kinds(self):
        """INV-M4-1: Separate single-writer rings, increasing frame_seq, epoch geometry, handle generation identity."""
        mod = self._get_symbols()
        bank = self._get_wsb(quota_bytes=20 * 1024 * 1024)

        for kind in self.SUPPORTED_KINDS:
            h = mod.Handle(kind, f"target-{kind}", 1)
            spec = mod.StreamSpec(width=16, height=16, slots=2, quota_bytes=256 * 1024)
            token = bank.open(h, spec)
            self.addCleanup(self._close_token, bank, token)
            self.assertEqual(token.handle.kind, kind)

            rgb1 = np.ones((16, 16, 3), dtype=np.uint8)
            rgb2 = np.ones((16, 16, 3), dtype=np.uint8) * 2
            bank.publish(token, rgb1, frame_seq=1)
            bank.publish(token, rgb2, frame_seq=2)

            res = bank.read(token)
            self.assertIsInstance(res, mod.FrameRef)
            self.assertEqual(res.frame_seq, 2)
            bank.close(token)

    def test_inv_m4_1_generation_change_advances_epoch(self):
        """INV-M4-1: Changing handle generation or geometry via resize advances epoch and invalidates old tokens."""
        mod = self._get_symbols()
        bank = self._get_wsb(quota_bytes=10 * 1024 * 1024)
        h1 = mod.Handle("godot_window", "godot-1", 1)
        spec1 = mod.StreamSpec(width=32, height=32, slots=2, quota_bytes=512 * 1024)
        token1 = bank.open(h1, spec1)
        self.addCleanup(self._close_token, bank, token1)

        spec2 = mod.StreamSpec(width=64, height=64, slots=2, quota_bytes=1024 * 1024)
        token2 = bank.resize(token1, spec2)
        self.addCleanup(self._close_token, bank, token2)
        self.assertNotEqual(token1.epoch, token2.epoch)

        # Reading with stale token1 should fail or drop
        with self.assertRaises((ValueError, KeyError)):
            bank.read(token1)

    def test_inv_m4_2_transactional_quota_admission_exact_accounting(self):
        """INV-M4-2: Quota admission is transactional across requested rings and accounts reservations exactly once."""
        mod = self._get_symbols()
        # The shared quota charges actual ring layout bytes, not spec limits.
        size = rtring.ring_bytes(2, 32, 32)
        bank = self._get_wsb(quota_bytes=2 * size - 1)
        h1 = mod.Handle("xvfb_screen", "screen-0", 1)
        spec1 = mod.StreamSpec(width=32, height=32, slots=2, quota_bytes=size)
        token1 = bank.open(h1, spec1)
        self.addCleanup(self._close_token, bank, token1)
        self.assertIsNotNone(token1)
        self.assertEqual(bank.reserved_bytes, size)

        h2 = mod.Handle("xvfb_screen", "screen-1", 1)
        spec2 = mod.StreamSpec(width=32, height=32, slots=2, quota_bytes=size)

        # Only the shared budget differs: a second ring is one byte too large.
        with self.assertRaises((ValueError, RuntimeError, MemoryError)):
            bank.open(h2, spec2)
        self.assertEqual(bank.reserved_bytes, size)

        # Verify rollback did not release pre-existing stream1 reservation
        rgb = np.full((32, 32, 3), 50, dtype=np.uint8)
        bank.publish(token1, rgb, frame_seq=1)
        res = bank.read(token1)
        self.assertIsInstance(res, mod.FrameRef)

    def test_inv_m4_3_bounded_retries_yield_explicit_drops_no_sleep(self):
        """INV-M4-3: Repeated sequence validation and CRC verification precede delivery; bounded retries yield Drop."""
        mod = self._get_symbols()
        buffers = []
        def ring_factory(handle, spec):
            buffer = bytearray(rtring.ring_bytes(spec.slots, spec.width, spec.height))
            buffers.append(buffer)
            return buffer

        bank = self._get_wsb(quota_bytes=10 * 1024 * 1024, ring_factory=ring_factory)
        h = mod.Handle("capsule", "cap-77", 1)
        spec = mod.StreamSpec(width=16, height=16, slots=2, quota_bytes=256 * 1024)
        token = bank.open(h, spec)
        self.addCleanup(self._close_token, bank, token)

        # Step hook that continuously mutates slot sequence on every retry attempt
        attempts = 0
        def infinite_lapped_hook(stage):
            nonlocal attempts
            if stage == "before_copy":
                attempts += 1
                off = rtring.HDR + (5 % spec.slots) * rtring.SLOT_HDR
                sequence = rtring._Q.unpack_from(buffers[0], off)[0]
                rtring._Q.pack_into(buffers[0], off, sequence + 2)

        rgb = np.ones((16, 16, 3), dtype=np.uint8)
        bank.publish(token, rgb, frame_seq=5)

        res = bank.read(token, step_hook=infinite_lapped_hook)
        # Should return a Drop when retries are exhausted
        self.assertTrue(isinstance(res, (mod.FrameRef, mod.Drop)))
        self.assertIsInstance(res, mod.Drop)
        self.assertEqual(res.reason, "torn")
        self.assertGreater(attempts, 0)
        self.assertLessEqual(attempts, 8)

    def test_canonical_handle_order_and_lock_order_checks(self):
        """Multi-stream operations validate canonical handle order before taking metadata locks."""
        mod = self._get_symbols()
        bank = self._get_wsb(quota_bytes=10 * 1024 * 1024)

        h_a = mod.Handle("cdp_tab", "a", 1)
        h_b = mod.Handle("cdp_tab", "b", 1)

        # Ordering h_b, h_a (reversed) should raise or reject.
        with self.assertRaises(ValueError):
            bank.check_lock_order([h_b, h_a])
        # Correct order h_a, h_b should pass.
        self.assertTrue(bank.check_lock_order([h_a, h_b]))

    def test_record_classes_field_order_and_immutability(self):
        """Validate canonical frozen dataclass record field contracts and immutability."""
        mod = self._get_symbols()
        h = mod.Handle("x11_window", "w-1", 1)
        self.assertEqual(h.kind, "x11_window")
        self.assertEqual(h.canonical_id, "w-1")
        self.assertEqual(h.generation, 1)

        with self.assertRaises(Exception):
            h.generation = 2  # Frozen

        spec = mod.StreamSpec(width=100, height=100, slots=4, quota_bytes=10000)
        token = mod.StreamToken(handle=h, epoch=1)
        self.assertEqual(token.handle, h)
        self.assertEqual(token.epoch, 1)

        drop = mod.Drop(handle=h, epoch=1, frame_seq=5, reason="lapped")
        self.assertEqual(drop.reason, "lapped")


if __name__ == "__main__":
    unittest.main()
