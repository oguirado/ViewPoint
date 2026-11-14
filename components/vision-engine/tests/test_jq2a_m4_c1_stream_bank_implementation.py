"""Offline contract tests of the real WindowStreamBank public API.

Only RAM bytearrays, NumPy pixels and injected deterministic hooks are used.
No X server, Godot, network, disk frame, process or workspace fixture.
"""

import hashlib
import struct
import threading
import types
import unittest

import numpy as np

from vadapt import rtring
from vadapt.window_stream_bank import (
    Drop, FrameRef, Handle, QuotaError, StaleStreamError, StreamError,
    StreamSpec, WindowStreamBank, check_lock_order,
)


def spec(w=3, h=2, slots=2):
    return StreamSpec(w, h, slots, rtring.ring_bytes(slots, w, h))


def pix(value, w=3, h=2):
    return np.full((h, w, 3), value, dtype=np.uint8)


class TestWindowStreamBank(unittest.TestCase):
    def setUp(self):
        self.a = Handle("cdp_tab", "tab/a", 1)
        self.b = Handle("xvfb_screen", "screen/b", 12)
        self.s = spec()
        self.size = self.s.quota_bytes

    def test_independent_generations_geometry_and_identity(self):
        bank = WindowStreamBank(quota_bytes=2 * self.size)
        ta, tb = bank.open_many(((self.a, self.s), (self.b, self.s)))
        self.assertEqual(bank.reserved_bytes, 2 * self.size)
        bank.publish(ta, pix(7), frame_seq=0)
        bank.publish(tb, pix(29), frame_seq=0)
        aa, bb = bank.read(ta), bank.read(tb)
        self.assertIsInstance(aa, FrameRef)
        self.assertEqual(aa.handle, self.a)
        self.assertEqual(aa.epoch, 0)
        self.assertEqual(aa.frame_seq, 0)
        np.testing.assert_array_equal(aa.rgb, pix(7))
        np.testing.assert_array_equal(bb.rgb, pix(29))
        self.assertNotEqual(aa.digest, bb.digest)
        self.assertEqual(aa.digest, hashlib.sha256(pix(7).tobytes()).hexdigest())
        self.assertEqual(aa.rgb.dtype, np.uint8)
        self.assertTrue(aa.rgb.flags.c_contiguous)
        self.assertFalse(aa.rgb.flags.writeable)
        bank.close(ta)
        bank.close(tb)
        self.assertEqual(bank.reserved_bytes, 0)

    def test_generation_and_closed_incarnation_never_reopens(self):
        bank = WindowStreamBank(quota_bytes=3 * self.size)
        t = bank.open(self.a, self.s)
        bank.close(t)
        with self.assertRaises(StaleStreamError):
            bank.read(t)
        with self.assertRaises(StaleStreamError):
            bank.publish(t, pix(1), frame_seq=0)
        with self.assertRaises(StaleStreamError):
            bank.open(self.a, self.s)
        new = Handle(self.a.kind, self.a.canonical_id, self.a.generation + 1)
        next_token = bank.open(new, self.s)
        bank.publish(next_token, pix(31), frame_seq=0)
        self.assertEqual(bank.read(next_token).handle.generation, 2)
        bank.close(next_token)

    def test_resize_epoch_and_monotonic_sequence(self):
        bank = WindowStreamBank(quota_bytes=3 * self.size)
        token = bank.open(self.a, self.s)
        bank.publish(token, pix(8), frame_seq=18)
        before = bank.read(token)
        next_token = bank.resize(token, self.s)
        self.assertEqual(next_token.epoch, token.epoch + 1)
        self.assertEqual(bank.reserved_bytes, self.size)
        with self.assertRaises(StaleStreamError):
            bank.read(token)
        with self.assertRaises(StaleStreamError):
            bank.close(token)
        self.assertEqual(bank.read(next_token).reason, "absent")
        with self.assertRaises(StreamError):
            bank.publish(next_token, pix(9), frame_seq=18)
        bank.publish(next_token, pix(9), frame_seq=19)
        self.assertEqual(bank.read(next_token).frame_seq, 19)
        np.testing.assert_array_equal(before.rgb, pix(8))
        bank.close(next_token)
        self.assertEqual(bank.reserved_bytes, 0)

    def test_spec_quota_and_global_exact_boundary(self):
        bank = WindowStreamBank(quota_bytes=2 * self.size - 1)
        t = bank.open(self.a, self.s)
        with self.assertRaises(QuotaError):
            bank.open(self.b, self.s)
        self.assertEqual(bank.reserved_bytes, self.size)
        bad = StreamSpec(3, 2, 2, self.size - 1)
        with self.assertRaises(QuotaError):
            WindowStreamBank(quota_bytes=2 * self.size).open(self.a, bad)
        bank.close(t)
        self.assertEqual(bank.reserved_bytes, 0)
        exact = WindowStreamBank(quota_bytes=self.size)
        t = exact.open(self.a, self.s)
        self.assertEqual(exact.reserved_bytes, self.size)
        exact.close(t)

    def test_multi_admission_atomic_refusal_and_canonical_order(self):
        bank = WindowStreamBank(quota_bytes=2 * self.size - 1)
        with self.assertRaises(QuotaError):
            bank.open_many(((self.a, self.s), (self.b, self.s)))
        self.assertEqual(bank.reserved_bytes, 0)
        with self.assertRaises(StreamError):
            check_lock_order((self.b, self.a))
        with self.assertRaises(StreamError):
            bank.open_many(((self.b, self.s), (self.a, self.s)))
        with self.assertRaises(StreamError):
            check_lock_order((self.a, self.a))
        self.assertTrue(check_lock_order((self.a, self.b)))
        t = bank.open(self.a, self.s)
        bank.close(t)

    def test_rollback_only_new_reservations_and_existing_survives(self):
        class FailingFactory:
            calls = 0
            def __call__(self, handle, s):
                self.calls += 1
                if handle.canonical_id == "screen/b":
                    raise RuntimeError("injected allocation failure")
                return bytearray(rtring.ring_bytes(s.slots, s.width, s.height))
        keep = Handle("cdp_tab", "0-existing", 1)
        f = FailingFactory()
        bank = WindowStreamBank(quota_bytes=3 * self.size, ring_factory=f)
        kt = bank.open(keep, self.s)
        bank.publish(kt, pix(42), frame_seq=1)
        with self.assertRaisesRegex(RuntimeError, "allocation failure"):
            bank.open_many(((self.a, self.s), (self.b, self.s)))
        self.assertEqual(bank.reserved_bytes, self.size)
        np.testing.assert_array_equal(bank.read(kt).rgb, pix(42))
        # A failed transaction did not poison the generation.
        at = bank.open(self.a, self.s)
        bank.close(at)
        bank.close(kt)
        self.assertEqual(bank.reserved_bytes, 0)

    def test_unknown_live_mapping_remains_charged_after_close(self):
        class Ring:
            def __init__(self, n):
                self.buffer = bytearray(n)
                self.mapped = True
                self.close_calls = 0
            def close(self):
                self.close_calls += 1
                return not self.mapped
        rings = []
        def factory(handle, s):
            r = Ring(rtring.ring_bytes(s.slots, s.width, s.height))
            rings.append(r)
            return r
        bank = WindowStreamBank(quota_bytes=self.size, ring_factory=factory)
        t = bank.open(self.a, self.s)
        bank.close(t)
        self.assertEqual(bank.reserved_bytes, self.size)
        with self.assertRaises(QuotaError):
            bank.open(self.b, self.s)
        self.assertEqual(bank.retry_cleanup(), 0)
        rings[0].mapped = False
        self.assertEqual(bank.retry_cleanup(), 1)
        self.assertEqual(bank.retry_cleanup(), 0)
        self.assertEqual(bank.reserved_bytes, 0)
        t2 = bank.open(self.b, self.s)
        rings[1].mapped = False
        bank.close(t2)
        self.assertEqual(bank.reserved_bytes, 0)

    def test_failure_during_factory_initialization_retains_unmapped_unknown(self):
        class Ring:
            def __init__(self, n):
                self.buffer = bytearray(n - 1)
                self.live = True
            def close(self):
                return not self.live
        rings = []
        def factory(h, s):
            r = Ring(rtring.ring_bytes(s.slots, s.width, s.height))
            rings.append(r)
            return r
        bank = WindowStreamBank(quota_bytes=self.size, ring_factory=factory)
        with self.assertRaises(StreamError):
            bank.open(self.a, self.s)
        self.assertEqual(bank.reserved_bytes, self.size)
        rings[0].live = False
        self.assertEqual(bank.retry_cleanup(), 1)
        self.assertEqual(bank.reserved_bytes, 0)

    def test_unsupported_targets_and_mutable_or_wrong_pixels_refuse(self):
        with self.assertRaises(StreamError):
            Handle("unknown", "id", 0)
        with self.assertRaises(StreamError):
            Handle("cdp_tab", "id", True)
        with self.assertRaises(StreamError):
            StreamSpec(1, 1, 0, 100)
        with self.assertRaises(StreamError):
            WindowStreamBank(quota_bytes=True)
        bank = WindowStreamBank(quota_bytes=self.size)
        token = bank.open(self.a, self.s)
        for bad in (np.zeros((2, 3), dtype=np.uint8),
                    np.zeros((2, 3, 3), dtype=np.uint16)):
            with self.assertRaises(StreamError):
                bank.publish(token, bad, frame_seq=0)
        bank.publish(token, pix(1), frame_seq=0)
        with self.assertRaises(StreamError):
            bank.publish(token, pix(1), frame_seq=0)
        with self.assertRaises(StreamError):
            bank.publish(token, pix(2), frame_seq=-1)
        bank.close(token)

    def test_crc_torn_after_copy_never_delivers(self):
        ring = bytearray(self.size)
        bank = WindowStreamBank(quota_bytes=self.size, ring_factory=lambda h, s: ring)
        token = bank.open(self.a, self.s)
        bank.publish(token, pix(4), frame_seq=0)
        counts = []
        def hook(phase):
            counts.append(phase)
            if phase == "after_copy":
                rtring._I.pack_into(ring, rtring.HDR + rtring.CRC_OFF, 0)
        got = bank.read(token, step_hook=hook)
        self.assertIsInstance(got, Drop)
        self.assertEqual(got.reason, "torn")
        self.assertEqual(counts[:2], ["before_copy", "after_copy"])
        self.assertLessEqual(counts.count("before_copy"), 8)
        bank.close(token)

    def test_corrupt_pixel_detected_by_crc_even_if_header_stable(self):
        ring = bytearray(self.size)
        bank = WindowStreamBank(quota_bytes=self.size, ring_factory=lambda h, s: ring)
        token = bank.open(self.a, self.s)
        bank.publish(token, pix(77), frame_seq=0)
        start = rtring.data_off(self.s.slots)
        def hook(phase):
            if phase == "before_copy":
                ring[start] = 0  # do not restore the valid CRC-matching bytes
        got = bank.read(token, step_hook=hook)
        self.assertIsInstance(got, Drop)
        self.assertEqual(got.reason, "torn")
        bank.close(token)

    def test_slot_odd_sequence_and_changed_render_index_refuse(self):
        ring = bytearray(self.size)
        bank = WindowStreamBank(quota_bytes=self.size, ring_factory=lambda h, s: ring)
        token = bank.open(self.a, self.s)
        bank.publish(token, pix(12), frame_seq=0)
        def hook(phase):
            if phase == "after_copy":
                rtring._Q.pack_into(ring, rtring.HDR + 8, 999)
        got = bank.read(token, step_hook=hook)
        self.assertIsInstance(got, Drop)
        self.assertEqual(got.reason, "lapped")
        rtring._Q.pack_into(ring, rtring.HDR + 8, 0)
        rtring._Q.pack_into(ring, rtring.HDR, 3)  # in-flight odd sequence
        self.assertEqual(bank.read(token).reason, "torn")
        bank.close(token)

    def test_before_copy_overwrite_cannot_deliver_obsolete_pixels(self):
        single = spec(slots=1)
        bank = WindowStreamBank(quota_bytes=single.quota_bytes)
        token = bank.open(self.a, single)
        bank.publish(token, pix(7), frame_seq=0)
        next_seq = [1]
        def hook(phase):
            if phase == "before_copy":
                bank.publish(token, pix(next_seq[0] + 7), frame_seq=next_seq[0])
                next_seq[0] += 1
        got = bank.read(token, step_hook=hook)
        self.assertIsInstance(got, Drop)
        self.assertIn(got.reason, ("torn", "lapped"))
        self.assertEqual(next_seq[0], 9)
        np.testing.assert_array_equal(bank.read(token).rgb, pix(15))
        bank.close(token)

    def test_returned_owned_readonly_pixels_survive_wrap_and_resize(self):
        single = spec(slots=1)
        bank = WindowStreamBank(quota_bytes=2 * single.quota_bytes)
        token = bank.open(self.a, single)
        bank.publish(token, pix(1), frame_seq=0)
        retained = bank.read(token)
        with self.assertRaises(ValueError):
            retained.rgb.setflags(write=True)
        bank.publish(token, pix(2), frame_seq=1)
        next_token = bank.resize(token, single)
        bank.publish(next_token, pix(3), frame_seq=2)
        np.testing.assert_array_equal(retained.rgb, pix(1))
        self.assertEqual(retained.digest, hashlib.sha256(pix(1).tobytes()).hexdigest())
        bank.close(next_token)

    def test_hook_exception_propagates_and_read_pin_is_released(self):
        bank = WindowStreamBank(quota_bytes=self.size)
        token = bank.open(self.a, self.s)
        bank.publish(token, pix(5), frame_seq=0)
        def hook(phase):
            if phase == "after_copy":
                raise RuntimeError("hook-failure")
        with self.assertRaisesRegex(RuntimeError, "hook-failure"):
            bank.read(token, step_hook=hook)
        bank.close(token)
        self.assertEqual(bank.reserved_bytes, 0)

    def test_hook_can_publish_from_other_thread_without_registry_lock(self):
        bank = WindowStreamBank(quota_bytes=self.size)
        token = bank.open(self.a, self.s)
        bank.publish(token, pix(1), frame_seq=0)
        called = []
        def hook(phase):
            if phase == "before_copy":
                worker = threading.Thread(target=lambda: (bank.publish(token, pix(2), frame_seq=1),
                                                          called.append("published")))
                worker.start()
                worker.join(1.5)
                self.assertFalse(worker.is_alive(), "step hook ran under registry mutex")
        got = bank.read(token, step_hook=hook)
        self.assertEqual(called, ["published"])
        self.assertIsInstance(got, FrameRef)
        self.assertIn(got.frame_seq, (0, 1))  # a non-overwritten slot may remain valid
        self.assertEqual(bank.read(token).frame_seq, 1)
        bank.close(token)

    def test_resize_many_all_or_nothing_with_preexisting_frames(self):
        s = self.s
        bank = WindowStreamBank(quota_bytes=3 * self.size)
        at, bt = bank.open_many(((self.a, s), (self.b, s)))
        bank.publish(at, pix(1), frame_seq=0)
        bank.publish(bt, pix(2), frame_seq=0)
        # Two new rings require 2*size while both old are still pinned.
        with self.assertRaises(QuotaError):
            bank.resize_many(((at, s), (bt, s)))
        self.assertEqual(bank.reserved_bytes, 2 * self.size)
        np.testing.assert_array_equal(bank.read(at).rgb, pix(1))
        np.testing.assert_array_equal(bank.read(bt).rgb, pix(2))
        bank.close(at)
        bank.close(bt)
        self.assertEqual(bank.reserved_bytes, 0)

    def test_factory_reuse_of_live_ring_rejects_without_touching_owner(self):
        shared = bytearray(self.size)
        bank = WindowStreamBank(quota_bytes=2 * self.size,
                                ring_factory=lambda h, s: shared)
        first = bank.open(self.a, self.s)
        bank.publish(first, pix(51), frame_seq=0)
        with self.assertRaisesRegex(StreamError, "reused a live ring"):
            bank.open(self.b, self.s)
        self.assertEqual(bank.reserved_bytes, self.size)
        np.testing.assert_array_equal(bank.read(first).rgb, pix(51))
        bank.close(first)
        self.assertEqual(bank.reserved_bytes, 0)

    def test_resize_during_copy_refuses_stale_epoch_and_defers_cleanup(self):
        bank = WindowStreamBank(quota_bytes=2 * self.size)
        first = bank.open(self.a, self.s)
        bank.publish(first, pix(4), frame_seq=0)
        renewed = []
        def hook(phase):
            if phase == "after_copy":
                renewed.append(bank.resize(first, self.s))
                self.assertEqual(bank.reserved_bytes, 2 * self.size)
        got = bank.read(first, step_hook=hook)
        self.assertEqual(got.reason, "stale_epoch")
        self.assertEqual(bank.reserved_bytes, self.size)
        self.assertEqual(len(renewed), 1)
        bank.publish(renewed[0], pix(6), frame_seq=1)
        np.testing.assert_array_equal(bank.read(renewed[0]).rgb, pix(6))
        bank.close(renewed[0])
        self.assertEqual(bank.reserved_bytes, 0)

    def test_structural_cross_module_handles_are_supported(self):
        bank = WindowStreamBank(quota_bytes=self.size)
        handle = types.SimpleNamespace(kind="capsule", canonical_id="same-destination", generation=2)
        ss = types.SimpleNamespace(width=3, height=2, slots=2, quota_bytes=self.size)
        token = bank.open(handle, ss)
        foreign_token = types.SimpleNamespace(handle=handle, epoch=0)
        bank.publish(foreign_token, pix(3), frame_seq=0)
        self.assertEqual(bank.read(foreign_token).handle, token.handle)
        bank.close(foreign_token)


if __name__ == "__main__":
    unittest.main()
