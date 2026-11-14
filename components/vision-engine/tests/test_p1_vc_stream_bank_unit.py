"""Area C: deterministic RAM-only contract and regression tests for WindowStreamBank.

No capture host, sockets, disk frames, mock production readers, or skipped tests.
All ring sizes, header offsets, CRCs and pixel digests use independent oracles.
"""

from dataclasses import fields
import hashlib
import struct
import threading
import unittest
from unittest.mock import patch
import zlib

import numpy as np

from vadapt import rtring
from vadapt import window_stream_bank as subject
from vadapt.window_stream_bank import (
    Drop, FrameRef, Handle, QuotaError, StaleStreamError, StreamError,
    StreamSpec, StreamToken, WindowStreamBank, check_lock_order,
)


def exact_size(w=3, h=2, slots=2):
    # Independent wire-layout arithmetic, rather than deriving the oracle
    # from the production ring_bytes function being checked.
    return ((64 + 64 * slots + 4095) // 4096) * 4096 + slots * w * h * 3


def spec(w=3, h=2, slots=2):
    return StreamSpec(w, h, slots, exact_size(w, h, slots))


def pixels(value, w=3, h=2):
    return np.full((h, w, 3), value, dtype=np.uint8)


class TrackedRing:
    def __init__(self, size):
        self.buffer = bytearray(size)
        self.close_calls = 0
        self.acks = 0

    def close(self):
        self.close_calls += 1
        try:
            self.buffer.clear()
        except BufferError:
            return False
        self.acks += 1
        return True


class CountingFactory:
    def __init__(self):
        self.owners = []
        self.observed_reservations = []
        self.bank = None

    def __call__(self, handle, requested):
        if self.bank is not None:
            self.observed_reservations.append(self.bank.reserved_bytes)
        owner = TrackedRing(exact_size(requested.width, requested.height, requested.slots))
        self.owners.append(owner)
        return owner

    @property
    def allocations(self):
        return len(self.owners)

    @property
    def cleanup_acks(self):
        return sum(o.acks for o in self.owners)


class StreamBankContract(unittest.TestCase):
    def setUp(self):
        self.a = Handle("cdp_tab", "a", 1)
        self.b = Handle("xvfb_screen", "b", 7)
        self.s = spec()
        self.size = exact_size()

    def test_public_shape_and_independent_ring_layout(self):
        self.assertEqual([f.name for f in fields(StreamSpec)],
                         ["width", "height", "slots", "quota_bytes"])
        self.assertEqual([f.name for f in fields(StreamToken)], ["handle", "epoch"])
        self.assertEqual([f.name for f in fields(FrameRef)],
                         ["handle", "epoch", "frame_seq", "width", "height", "rgb", "digest"])
        self.assertEqual([f.name for f in fields(Drop)],
                         ["handle", "epoch", "frame_seq", "reason"])
        self.assertEqual(self.size, 4132)
        self.assertEqual(rtring.ring_bytes(2, 3, 2), self.size)
        self.assertEqual(exact_size(640, 360, 1), 695296)
        self.assertEqual(exact_size(640, 360, 2), 1386496)
        self.assertTrue(issubclass(QuotaError, StreamError))
        self.assertTrue(issubclass(StaleStreamError, StreamError))
        self.assertTrue(issubclass(StreamError, ValueError))

    def test_owned_immutable_rgb_digest_after_caller_mutation(self):
        bank = WindowStreamBank(quota_bytes=self.size)
        token = bank.open(self.a, self.s)
        source = np.arange(18, dtype=np.uint8).reshape(2, 3, 3)
        expected = bytes(range(18))
        self.assertIsNone(bank.publish(token, source, frame_seq=0))
        source[:] = 255
        frame = bank.read(token)
        self.assertIsInstance(frame, FrameRef)
        self.assertEqual((frame.epoch, frame.frame_seq, frame.width, frame.height),
                         (0, 0, 3, 2))
        self.assertEqual(frame.rgb.tobytes(), expected)
        self.assertEqual(frame.digest, hashlib.sha256(expected).hexdigest())
        self.assertFalse(frame.rgb.flags.writeable)
        with self.assertRaises(ValueError):
            frame.rgb.setflags(write=True)
        bank.close(token)
        self.assertEqual(bank.reserved_bytes, 0)
        self.assertEqual(bank.retry_cleanup(), 0)

    def test_real_ring_headers_and_crc_oracles(self):
        factory = CountingFactory()
        bank = WindowStreamBank(quota_bytes=self.size, ring_factory=factory)
        token = bank.open(self.a, self.s)
        raw = factory.owners[0].buffer
        frame_bytes = bytes([23] * 18)
        bank.publish(token, pixels(23), frame_seq=4)
        header = struct.unpack_from("<8sIIIIQQQ", raw, 0)
        self.assertEqual(header, (b"VQARING1", 2, 2, 3, 2, 18, 4096, 5))
        self.assertEqual(struct.unpack_from("<QQ", raw, 64)[0:2], (2, 4))
        self.assertEqual(struct.unpack_from("<I", raw, 64 + 16)[0],
                         zlib.crc32(frame_bytes))
        self.assertEqual(bytes(raw[4096:4114]), frame_bytes)
        actual = bank.read(token)
        self.assertIsInstance(actual, FrameRef)
        self.assertEqual(actual.digest, hashlib.sha256(frame_bytes).hexdigest())
        bank.close(token)
        self.assertEqual((factory.allocations, factory.cleanup_acks), (1, 1))
        self.assertEqual(len(raw), 0)

    def test_crc_and_payload_corruption_never_mint_frames(self):
        factory = CountingFactory()
        bank = WindowStreamBank(quota_bytes=2 * self.size, ring_factory=factory)
        t1, t2 = bank.open_many(((self.a, self.s), (self.b, self.s)))
        for t in (t1, t2):
            bank.publish(t, pixels(41), frame_seq=0)
        factory.owners[0].buffer[4096] ^= 0x10
        factory.owners[1].buffer[64 + 16] ^= 0x01
        for t in (t1, t2):
            drop = bank.read(t)
            self.assertIsInstance(drop, Drop)
            self.assertEqual(drop.reason, "torn")
            self.assertFalse(hasattr(drop, "rgb"))
        bank.close(t1)
        bank.close(t2)
        self.assertEqual(factory.cleanup_acks, 2)
        self.assertEqual(bank.reserved_bytes, 0)

    def test_invalid_dimensions_and_exact_refusal_without_allocations(self):
        bad = [(0, 2, 2), (True, 2, 2), (3.5, 2, 2), (3, -1, 2),
               (3, 2, False), (3, 2, 0), (3, 2, 2 ** 32)]
        for w, h, slots in bad:
            with self.subTest(w=w, h=h, slots=slots):
                with self.assertRaisesRegex(StreamError, "exact integer"):
                    StreamSpec(w, h, slots, 10000)
        factory = CountingFactory()
        bank = WindowStreamBank(quota_bytes=self.size, ring_factory=factory)
        with self.assertRaisesRegex(QuotaError, "ring_bytes exceeds StreamSpec.quota_bytes"):
            bank.open(self.a, StreamSpec(3, 2, 2, self.size - 1))
        self.assertEqual((factory.allocations, bank.reserved_bytes), (0, 0))
        too_small = WindowStreamBank(quota_bytes=self.size - 1, ring_factory=factory)
        with self.assertRaisesRegex(QuotaError, "transaction exceeds remaining shared quota"):
            too_small.open(self.a, self.s)
        self.assertEqual((factory.allocations, too_small.reserved_bytes), (0, 0))

    def test_generation_floor_retirement_and_active_lane_fencing(self):
        bank = WindowStreamBank(quota_bytes=3 * self.size)
        legacy = Handle("cdp_tab", "a", 0)  # representable on legacy wire
        self.assertEqual(legacy.generation, 0)
        with self.assertRaisesRegex(StreamError, "generation must be an exact integer"):
            bank.open(legacy, self.s)
        self.assertEqual(bank.reserved_bytes, 0)
        t = bank.open(Handle("cdp_tab", "a", 2), self.s)
        with self.assertRaisesRegex(StaleStreamError, "generation already opened"):
            bank.open(Handle("cdp_tab", "a", 3), self.s)
        bank.close(t)
        for generation in (1, 2):
            with self.assertRaisesRegex(StaleStreamError, "generation already opened"):
                bank.open(Handle("cdp_tab", "a", generation), self.s)
        newer = bank.open(Handle("cdp_tab", "a", 3), self.s)
        bank.close(newer)
        self.assertEqual(bank.reserved_bytes, 0)

    def test_lock_order_duplicate_lanes_and_atomic_shortfall(self):
        with self.assertRaisesRegex(StreamError, "unique and in canonical order"):
            check_lock_order((self.b, self.a))
        with self.assertRaisesRegex(StreamError, "unique and in canonical order"):
            check_lock_order((self.a, self.a))
        factory = CountingFactory()
        bank = WindowStreamBank(quota_bytes=2 * self.size - 1, ring_factory=factory)
        with self.assertRaisesRegex(QuotaError, "transaction exceeds remaining shared quota"):
            bank.open_many(((self.a, self.s), (self.b, self.s)))
        self.assertEqual((factory.allocations, bank.reserved_bytes), (0, 0))
        another = Handle("cdp_tab", "a", 2)
        with self.assertRaisesRegex(StaleStreamError, "generation already opened"):
            bank.open_many(((self.a, self.s), (another, self.s)))
        self.assertEqual((factory.allocations, bank.reserved_bytes), (0, 0))

    def test_partial_factory_failure_rolls_back_only_new_allocations(self):
        factory = CountingFactory()
        failure = [True]
        def fail_once(handle, request):
            if handle == self.b and failure[0]:
                failure[0] = False
                raise RuntimeError("injected second allocation failure")
            return factory(handle, request)
        bank = WindowStreamBank(quota_bytes=3 * self.size, ring_factory=fail_once)
        with self.assertRaisesRegex(RuntimeError, "second allocation failure"):
            bank.open_many(((self.a, self.s), (self.b, self.s)))
        self.assertEqual(bank.reserved_bytes, 0)
        self.assertEqual((factory.allocations, factory.cleanup_acks), (1, 1))
        token = bank.open(self.a, self.s)  # failed admission never retired generation
        bank.publish(token, pixels(13), frame_seq=0)
        self.assertEqual(bank.read(token).rgb.tobytes(), bytes([13] * 18))
        bank.close(token)
        self.assertEqual((factory.allocations, factory.cleanup_acks), (2, 2))

    def test_live_alias_wrapped_memoryview_rejected_without_cleanup_of_sibling(self):
        factory = CountingFactory()
        class AliasOwner:
            def __init__(self, buffer):
                self.buffer = memoryview(buffer)
                self.close_calls = 0
            def close(self):
                self.close_calls += 1
                return False
        alias = []
        def allocator(handle, request):
            if handle == self.b:
                a = AliasOwner(factory.owners[0].buffer)
                alias.append(a)
                return a
            return factory(handle, request)
        bank = WindowStreamBank(quota_bytes=2 * self.size, ring_factory=allocator)
        token = bank.open(self.a, self.s)
        bank.publish(token, pixels(57), frame_seq=0)
        with self.assertRaisesRegex(StreamError, "ring_factory reused a live ring buffer"):
            bank.open(self.b, self.s)
        self.assertEqual(bank.reserved_bytes, self.size)
        self.assertEqual((factory.allocations, factory.cleanup_acks), (1, 0))
        self.assertEqual(alias[0].close_calls, 0)
        self.assertEqual(bank.read(token).rgb.tobytes(), bytes([57] * 18))
        alias[0].buffer.release()
        bank.close(token)
        self.assertEqual((factory.cleanup_acks, bank.reserved_bytes), (1, 0))

    def test_resize_retains_epochs_sequences_and_requires_double_budget(self):
        f = CountingFactory()
        bank = WindowStreamBank(quota_bytes=4 * self.size, ring_factory=f)
        f.bank = bank
        t1, t2 = bank.open_many(((self.a, self.s), (self.b, self.s)))
        bank.publish(t1, pixels(11), frame_seq=9)
        bank.publish(t2, pixels(22), frame_seq=40)
        grown = bank.resize_many(((t1, self.s), (t2, self.s)))
        self.assertEqual(f.observed_reservations, [2*self.size, 2*self.size,
                                                    4*self.size, 4*self.size])
        self.assertEqual((f.allocations, f.cleanup_acks), (4, 2))
        self.assertEqual(bank.reserved_bytes, 2 * self.size)
        with self.assertRaisesRegex(StaleStreamError, "stale geometry epoch"):
            bank.read(t1)
        with self.assertRaisesRegex(StreamError, "frame_seq must increase monotonically"):
            bank.publish(grown[0], pixels(11), frame_seq=9)
        bank.publish(grown[0], pixels(33), frame_seq=10)
        bank.publish(grown[1], pixels(44), frame_seq=41)
        self.assertEqual((bank.read(grown[0]).frame_seq, bank.read(grown[1]).frame_seq),
                         (10, 41))
        bank.close(grown[0])
        self.assertEqual(bank.reserved_bytes, self.size)
        self.assertEqual(bank.read(grown[1]).rgb.tobytes(), bytes([44] * 18))
        bank.close(grown[1])
        self.assertEqual((f.allocations, f.cleanup_acks, bank.reserved_bytes), (4, 4, 0))

    def test_failed_resize_reserves_nothing_and_old_frames_survive(self):
        factory = CountingFactory()
        bank = WindowStreamBank(quota_bytes=3 * self.size - 1, ring_factory=factory)
        t1, t2 = bank.open_many(((self.a, self.s), (self.b, self.s)))
        bank.publish(t1, pixels(1), frame_seq=0)
        bank.publish(t2, pixels(2), frame_seq=0)
        with self.assertRaisesRegex(QuotaError, "transaction exceeds remaining shared quota"):
            bank.resize_many(((t1, self.s), (t2, self.s)))
        self.assertEqual((bank.reserved_bytes, factory.allocations), (2*self.size, 2))
        self.assertEqual(bank.read(t1).rgb.tobytes(), bytes([1] * 18))
        self.assertEqual(bank.read(t2).rgb.tobytes(), bytes([2] * 18))
        bank.close(t1)
        bank.close(t2)
        self.assertEqual((bank.reserved_bytes, factory.cleanup_acks), (0, 2))

    def test_bytearray_borrowed_view_close_retry_exact_delta(self):
        allocated = []
        def factory(handle, requested):
            raw = bytearray(exact_size(requested.width, requested.height, requested.slots))
            allocated.append(raw)
            return raw
        bank = WindowStreamBank(quota_bytes=self.size, ring_factory=factory)
        token = bank.open(self.a, self.s)
        borrowed = memoryview(allocated[0])
        bank.close(token)
        self.assertEqual(bank.reserved_bytes, self.size)
        self.assertEqual(bank.retry_cleanup(), 0)
        self.assertEqual(len(allocated[0]), self.size)
        borrowed.release()
        self.assertEqual(bank.retry_cleanup(), 1)
        self.assertEqual((bank.reserved_bytes, len(allocated[0]), len(allocated)), (0, 0, 1))
        self.assertEqual(bank.retry_cleanup(), 0)
        with self.assertRaisesRegex(StaleStreamError, "closed generation"):
            bank.read(token)

    def test_retained_old_resize_charge_and_cleanup_acknowledgement(self):
        factory = CountingFactory()
        bank = WindowStreamBank(quota_bytes=2*self.size, ring_factory=factory)
        old = bank.open(self.a, self.s)
        borrowed = memoryview(factory.owners[0].buffer)
        new = bank.resize(old, self.s)
        self.assertEqual((factory.allocations, factory.cleanup_acks), (2, 0))
        self.assertEqual(bank.reserved_bytes, 2*self.size)
        self.assertEqual(bank.retry_cleanup(), 0)
        bank.close(new)
        self.assertEqual((bank.reserved_bytes, factory.cleanup_acks), (self.size, 1))
        borrowed.release()
        self.assertEqual(bank.retry_cleanup(), 1)
        self.assertEqual((bank.reserved_bytes, factory.cleanup_acks), (0, 2))

    def test_pending_resize_blocks_old_publication_and_close(self):
        owner = CountingFactory()
        old_token = []
        observed = []
        def factory(handle, request):
            if old_token:
                with self.assertRaisesRegex(StreamError, "cannot publish during pending resize"):
                    bank.publish(old_token[0], pixels(9), frame_seq=1)
                with self.assertRaisesRegex(StreamError, "cannot close during pending resize"):
                    bank.close(old_token[0])
                observed.append(bank.reserved_bytes)
            return owner(handle, request)
        bank = WindowStreamBank(quota_bytes=2*self.size, ring_factory=factory)
        token = bank.open(self.a, self.s)
        old_token.append(token)
        bank.publish(token, pixels(1), frame_seq=0)
        newer = bank.resize(token, self.s)
        self.assertEqual(observed, [2*self.size])
        self.assertEqual(bank.reserved_bytes, self.size)
        bank.publish(newer, pixels(4), frame_seq=1)
        self.assertEqual(bank.read(newer).rgb.tobytes(), bytes([4]*18))
        bank.close(newer)
        self.assertEqual(bank.reserved_bytes, 0)

    def test_invalid_sequences_and_tokens_leave_last_frame_intact(self):
        bank = WindowStreamBank(quota_bytes=self.size)
        token = bank.open(self.a, self.s)
        bank.publish(token, pixels(5), frame_seq=0)
        for value in (True, -1, 0, 1.5, (1 << 64) - 1):
            with self.subTest(seq=value):
                with self.assertRaises(StreamError):
                    bank.publish(token, pixels(99), frame_seq=value)
                self.assertEqual(bank.read(token).rgb.tobytes(), bytes([5]*18))
        for epoch in (-1, True, 0.5):
            with self.subTest(epoch=epoch):
                with self.assertRaises(StreamError):
                    bank.read(StreamToken(token.handle, epoch))
        with self.assertRaisesRegex(StreamError, "source must be exact uint8"):
            bank.publish(token, pixels(7).astype(np.int32), frame_seq=1)
        self.assertEqual(bank.read(token).frame_seq, 0)
        bank.close(token)

    def test_read_close_interleave_retains_mapping_and_drops_stale_epoch(self):
        bank = WindowStreamBank(quota_bytes=self.size)
        token = bank.open(self.a, self.s)
        bank.publish(token, pixels(7), frame_seq=0)
        rendezvous, release = threading.Barrier(2), threading.Barrier(2)
        results, errors = [], []
        def hook(phase):
            if phase == "before_copy":
                rendezvous.wait(timeout=3)
                release.wait(timeout=3)
        def reader():
            try:
                results.append(bank.read(token, step_hook=hook))
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=reader, name="p1-reader")
        thread.start()
        try:
            rendezvous.wait(timeout=3)
            bank.close(token)
            self.assertEqual(bank.reserved_bytes, self.size)
            release.wait(timeout=3)
        finally:
            rendezvous.abort()
            release.abort()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive(), "reader leaked")
        if errors:
            raise errors[0]
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], Drop)
        self.assertEqual(results[0].reason, "stale_epoch")
        self.assertEqual((bank.reserved_bytes, bank.retry_cleanup()), (0, 0))

    def test_actual_reader_writer_barrier_interleave_rejects_torn_evidence(self):
        one = spec(slots=1)
        bank = WindowStreamBank(quota_bytes=one.quota_bytes)
        token = bank.open(self.a, one)
        bank.publish(token, pixels(0), frame_seq=0)
        start, finish = threading.Barrier(2), threading.Barrier(2)
        errors, results, counts = [], [], [0]
        def hook(phase):
            if phase == "before_copy":
                counts[0] += 1
                start.wait(timeout=3)
                finish.wait(timeout=3)
        def writer():
            try:
                for i in range(1, 9):
                    start.wait(timeout=3)
                    bank.publish(token, pixels(i), frame_seq=i)
                    finish.wait(timeout=3)
            except BaseException as exc:
                errors.append(exc)
        producer = threading.Thread(target=writer, name="p1-writer")
        producer.start()
        try:
            results.append(bank.read(token, step_hook=hook))
        finally:
            start.abort()
            finish.abort()
            producer.join(timeout=3)
        self.assertFalse(producer.is_alive(), "writer leaked")
        if errors:
            raise errors[0]
        self.assertEqual(counts, [8])
        self.assertIsInstance(results[0], Drop)
        self.assertIn(results[0].reason, {"torn", "lapped"})
        self.assertFalse(hasattr(results[0], "rgb"))
        frame = bank.read(token)
        self.assertIsInstance(frame, FrameRef)
        self.assertEqual(frame.frame_seq, 8)
        self.assertEqual(frame.rgb.tobytes(), bytes([8] * 18))
        bank.close(token)

    def test_hooks_do_not_hold_registry_mutex(self):
        bank = WindowStreamBank(quota_bytes=self.size)
        token = bank.open(self.a, self.s)
        bank.publish(token, pixels(14), frame_seq=0)
        errors, seen = [], []
        def hook(phase):
            if phase == "before_copy":
                def probe():
                    try:
                        seen.append(bank.reserved_bytes)
                    except BaseException as exc:
                        errors.append(exc)
                worker = threading.Thread(target=probe, name="p1-lock-probe")
                worker.start()
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive(), "hook ran under registry mutex")
        self.assertIsInstance(bank.read(token, step_hook=hook), FrameRef)
        if errors:
            raise errors[0]
        self.assertEqual(seen, [self.size])
        bank.close(token)

    def test_four_target_ram_budget_is_not_host_timing_evidence(self):
        s = spec(640, 360, slots=2)
        size = 1386496
        handles = tuple(Handle("x11_window", "screen-%d" % i, 1) for i in range(4))
        factory = CountingFactory()
        bank = WindowStreamBank(quota_bytes=4 * size, ring_factory=factory)
        tokens = bank.open_many(tuple((h, s) for h in handles))
        self.assertEqual((len(tokens), bank.reserved_bytes, factory.allocations),
                         (4, 5545984, 4))
        for i, token in enumerate(tokens):
            bank.publish(token, pixels(i + 10, 640, 360), frame_seq=0)
        for i, token in enumerate(tokens):
            result = bank.read(token)
            self.assertIsInstance(result, FrameRef)
            self.assertEqual(result.digest,
                             hashlib.sha256(bytes([i+10]) * (640*360*3)).hexdigest())
            bank.close(token)
            self.assertEqual(bank.reserved_bytes, (3-i)*size)
        self.assertEqual((factory.cleanup_acks, bank.retry_cleanup()), (4, 0))

    def test_explicit_mutants_are_killed_by_independent_oracles(self):
        # Two deliberately defective methods are installed only transiently;
        # both must be distinguished by specific positive-control assertions.
        bank = WindowStreamBank(quota_bytes=self.size)
        token = bank.open(self.a, self.s)
        bank.publish(token, pixels(15), frame_seq=0)
        baseline = bank.read(token)
        self.assertIsInstance(baseline, FrameRef)  # green precedes mutants
        original_read = WindowStreamBank.read
        def status_only(*args, **kwargs):
            return Drop(token.handle, token.epoch, None, "absent")
        with patch.object(WindowStreamBank, "read", status_only):
            with self.assertRaisesRegex(AssertionError, "FrameRef"):
                self.assertIsInstance(bank.read(token), FrameRef)
        self.assertIs(WindowStreamBank.read, original_read)
        owner = bank._active[token.handle].owner
        original_close = subject._MemoryRing.close
        def dishonest_release(self):
            return True  # mutation: acknowledge without reclaiming bytearray
        with patch.object(subject._MemoryRing, "close", dishonest_release):
            bank.close(token)
            with self.assertRaisesRegex(AssertionError, "None"):
                self.assertIsNone(owner.buffer)
        self.assertIs(subject._MemoryRing.close, original_close)
        # Kills=2/2, survivors=0, equivalent=0, invalid=0. Both assertions
        # are independent of the monkeypatched method under test.


if __name__ == "__main__":
    unittest.main()
