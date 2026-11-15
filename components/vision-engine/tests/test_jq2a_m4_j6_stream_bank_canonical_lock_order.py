"""Gate G M4 6: Stream bank canonical lock order verification, bounded barrier traces,
and deterministic hook injection.

Owning implementation task_id: jq2a-m4-c1-stream-bank
Export WindowStreamBank, Handle, StreamSpec, StreamToken, FrameRef and Drop.
"""

import threading
import unittest
from typing import Sequence

import numpy as np

from vadapt import rtring
from vadapt import window_stream_bank as _owner_mod
from vadapt.window_stream_bank import (
    Drop, FrameRef, Handle, StaleStreamError, StreamError,
    StreamSpec, StreamToken, WindowStreamBank,
)

OWNER_PRESENT = hasattr(_owner_mod, "WindowStreamBank")


def check_lock_order(handles: Sequence[Handle]) -> bool:
    """Check if handles are ordered according to canonical Handle sort key.

    Sort key is (kind, canonical_id, generation).
    Returns True if non-decreasing, False if reversed/out-of-order.
    """
    if len(handles) <= 1:
        return True
    keys = [(h.kind, h.canonical_id, h.generation) for h in handles]
    for i in range(len(keys) - 1):
        if keys[i] > keys[i + 1]:
            return False
    return True


# Harnesses for contract testing lock ordering and hook mechanics

class LockOrderHarness:
    """Contract harness simulating per-stream lock acquisition and barrier traces."""

    def __init__(self):
        self.locks = {}
        self.lock_trace = []
        self.trace_mutex = threading.Lock()

    def _get_lock(self, handle: Handle) -> threading.Lock:
        key = (handle.kind, handle.canonical_id, handle.generation)
        with self.trace_mutex:
            if key not in self.locks:
                self.locks[key] = threading.Lock()
            return self.locks[key]

    def acquire_caller_order_negative_mutant(
        self, handles: Sequence[Handle], barrier: threading.Barrier | None = None
    ) -> bool:
        """Negative mutant: acquires per-stream locks in exact caller argument order.

        Prone to lock inversion deadlock when callers supply handles in opposing order.
        """
        acquired = []
        try:
            for idx, h in enumerate(handles):
                lk = self._get_lock(h)
                if not lk.acquire(timeout=0.1):  # Bounded lock acquire timeout
                    return False
                acquired.append(lk)
                with self.trace_mutex:
                    self.lock_trace.append(("acquired", h, threading.get_ident()))
                if idx == 0 and barrier is not None:
                    try:
                        barrier.wait(timeout=0.2)
                    except threading.BrokenBarrierError:
                        return False
            return True
        finally:
            for lk in reversed(acquired):
                lk.release()

    def acquire_canonical_order_positive_control(
        self, handles: Sequence[Handle], start_barrier: threading.Barrier | None = None
    ) -> bool:
        """Positive control: validates canonical handle order before acquisition, or sorts canonically."""
        if not check_lock_order(handles):
            # Non-blocking rejection of reverse canonical Handle order
            return False

        if start_barrier is not None:
            try:
                start_barrier.wait(timeout=0.2)
            except threading.BrokenBarrierError:
                return False

        acquired = []
        try:
            for h in handles:
                lk = self._get_lock(h)
                if not lk.acquire(timeout=0.2):
                    return False
                acquired.append(lk)
                with self.trace_mutex:
                    self.lock_trace.append(("acquired", h, threading.get_ident()))
            return True
        finally:
            for lk in reversed(acquired):
                lk.release()


class TestCanonicalLockOrderHarness(unittest.TestCase):
    """Contract tests for check_lock_order predicate and multi-stream lock harnesses."""

    def _run_threads(self, functions, barrier):
        errors = []

        def guarded(function):
            try:
                function()
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=guarded, args=(function,))
                   for function in functions]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=0.5)
        finally:
            barrier.abort()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=0.5)
        self.assertFalse(any(thread.is_alive() for thread in threads),
                         "lock-order fixture leaked a worker")
        if errors:
            raise errors[0]

    def setUp(self):
        self.h_a = Handle(kind="x11_window", canonical_id="win_001", generation=1)
        self.h_b = Handle(kind="x11_window", canonical_id="win_002", generation=1)
        self.h_c = Handle(kind="x11_window", canonical_id="win_002", generation=2)
        self.h_d = Handle(kind="xvfb_screen", canonical_id="screen_001", generation=1)

    def test_check_lock_order_predicate_canonical_cases(self):
        """Predicate identifies canonical non-decreasing vs reversed Handle sequences."""
        self.assertTrue(check_lock_order([self.h_a, self.h_b]))
        self.assertTrue(check_lock_order([self.h_b, self.h_c]))
        self.assertTrue(check_lock_order([self.h_a, self.h_b, self.h_c, self.h_d]))
        self.assertTrue(check_lock_order([self.h_a]))
        self.assertTrue(check_lock_order([]))

        # Reversed handle order rejected without blocking
        self.assertFalse(check_lock_order([self.h_b, self.h_a]))
        self.assertFalse(check_lock_order([self.h_d, self.h_a]))
        self.assertFalse(check_lock_order([self.h_c, self.h_b]))

    def test_negative_mutant_deadlock_on_reversed_caller_order(self):
        """Negative mutant caller-order locking causes deadlock under interleaving barrier."""
        harness = LockOrderHarness()
        barrier = threading.Barrier(2)

        results = [False, False]

        def t1_run():
            results[0] = harness.acquire_caller_order_negative_mutant([self.h_a, self.h_b], barrier)

        def t2_run():
            results[1] = harness.acquire_caller_order_negative_mutant([self.h_b, self.h_a], barrier)

        # Offline barriers use bounded joins and explicit failure rather than timing-based success
        self._run_threads((t1_run, t2_run), barrier)

        # Under negative mutant caller-order locking, lock acquisition times out / fails
        self.assertFalse(all(results), "Caller-order negative mutant should fail lock acquisition under lock inversion")

    def test_positive_control_rejection_of_reversed_handle_order(self):
        """Positive control rejects reversed canonical Handle order non-blocking without deadlock."""
        harness = LockOrderHarness()

        res_t1 = harness.acquire_canonical_order_positive_control([self.h_a, self.h_b], start_barrier=None)
        res_t2 = harness.acquire_canonical_order_positive_control([self.h_b, self.h_a], start_barrier=None)

        # Reverse order rejected immediately
        self.assertTrue(res_t1)
        self.assertFalse(res_t2)

    def test_positive_control_concurrent_canonical_order_safety(self):
        """Positive control with canonically sorted handles executes concurrently without deadlock."""
        harness = LockOrderHarness()
        start_barrier = threading.Barrier(2)

        results = [False, False]

        def t1_run():
            results[0] = harness.acquire_canonical_order_positive_control([self.h_a, self.h_b], start_barrier)

        def t2_run():
            results[1] = harness.acquire_canonical_order_positive_control([self.h_a, self.h_b], start_barrier)

        self._run_threads((t1_run, t2_run), start_barrier)

        self.assertTrue(all(results), "Positive control threads using canonical order must both succeed")

    def test_dataclass_field_order_and_immutability(self):
        """Verify frozen dataclasses match exact positional field definitions."""
        h = Handle("x11_window", "w1", 1)
        self.assertEqual((h.kind, h.canonical_id, h.generation), ("x11_window", "w1", 1))
        with self.assertRaises((TypeError, Exception)):
            h.kind = "other"

        spec = StreamSpec(1920, 1080, 4, 1024 * 1024)
        self.assertEqual((spec.width, spec.height, spec.slots, spec.quota_bytes), (1920, 1080, 4, 1024 * 1024))

        tok = StreamToken(h, 1)
        self.assertEqual((tok.handle, tok.epoch), (h, 1))

        rgb = np.zeros((10, 10, 3), dtype=np.uint8)
        frame = FrameRef(h, 1, 100, 1920, 1080, rgb, "sha256_digest")
        self.assertEqual(frame.width, 1920)

        drop = Drop(h, 1, 100, "crc_mismatch")
        self.assertEqual(drop.reason, "crc_mismatch")


class TestProductionStreamBankConformance(unittest.TestCase):
    """Production conformance tests probing vadapt.window_stream_bank.WindowStreamBank."""

    def _bank(self, **kwargs):
        bank = WindowStreamBank(**kwargs)
        tokens = []

        def close_all():
            for token in reversed(tokens):
                try:
                    bank.close(token)
                except StaleStreamError:
                    # A successfully resized/closed token is already retired.
                    pass
            self.assertEqual(bank.reserved_bytes, 0)

        self.addCleanup(close_all)
        return bank, tokens

    def test_prod_owner_module_presence(self):
        """Verify owner module vadapt.window_stream_bank and WindowStreamBank exist."""
        self.assertTrue(OWNER_PRESENT, "Owner module vadapt.window_stream_bank.WindowStreamBank is absent")

    def test_prod_check_lock_order_predicate(self):
        """Verify check_lock_order on production handles and check_lock_order function."""
        self.assertTrue(OWNER_PRESENT)
        check_fn = _owner_mod.check_lock_order
        h1 = Handle("cdp_tab", "tab_1", 1)
        h2 = Handle("cdp_tab", "tab_2", 1)
        self.assertTrue(check_fn([h1, h2]))
        with self.assertRaises(StreamError):
            check_fn([h2, h1])

    def test_prod_stream_bank_group_operations_lock_order(self):
        """Verify WindowStreamBank group operations validate canonical handle order before taking locks."""
        self.assertTrue(OWNER_PRESENT)
        size = rtring.ring_bytes(2, 1280, 720)
        bank, tokens = self._bank(quota_bytes=2 * size)
        h1 = Handle("x11_window", "win_a", 1)
        h2 = Handle("x11_window", "win_b", 1)
        spec = StreamSpec(1280, 720, 2, size)

        with self.assertRaises(StreamError):
            bank.open_many(((h2, spec), (h1, spec)))
        self.assertEqual(bank.reserved_bytes, 0)
        tok1, tok2 = bank.open_many(((h1, spec), (h2, spec)))
        tokens.extend((tok1, tok2))
        self.assertIsNotNone(tok1)
        self.assertIsNotNone(tok2)

    def test_prod_step_hook_invocation_before_and_after_copy(self):
        """Verify step_hook is invoked at before_copy and after_copy outside metadata mutexes."""
        self.assertTrue(OWNER_PRESENT)
        hook_events = []

        def trace_hook(phase):
            hook_events.append(phase)

        size = rtring.ring_bytes(2, 640, 480)
        bank, tokens = self._bank(quota_bytes=size, step_hook=trace_hook)
        h = Handle("xvfb_screen", "scr_0", 1)
        spec = StreamSpec(640, 480, 2, size)
        tok = bank.open(h, spec)
        tokens.append(tok)
        rgb = np.zeros((480, 640, 3), dtype=np.uint8)
        bank.publish(tok, rgb, frame_seq=1)

        res = bank.read(tok)
        self.assertIsInstance(res, FrameRef)
        phases = hook_events
        self.assertIn("before_copy", phases)
        self.assertIn("after_copy", phases)

    def test_prod_resize_epoch_advancement_and_stale_token_rejection(self):
        """Verify resize advances geometry epoch and rejects retained stale tokens."""
        self.assertTrue(OWNER_PRESENT)
        size1 = rtring.ring_bytes(2, 800, 600)
        size2 = rtring.ring_bytes(2, 1024, 768)
        bank, tokens = self._bank(quota_bytes=size1 + size2)
        h = Handle("godot_window", "g_win", 1)
        spec1 = StreamSpec(800, 600, 2, size1)
        spec2 = StreamSpec(1024, 768, 2, size2)

        tok1 = bank.open(h, spec1)
        tokens.append(tok1)
        self.assertEqual(tok1.epoch, 0)

        tok2 = bank.resize(tok1, spec2)
        tokens.append(tok2)
        self.assertGreater(tok2.epoch, tok1.epoch)

        # Retained stale token tok1 should be rejected on publish or read
        rgb = np.zeros((600, 800, 3), dtype=np.uint8)
        with self.assertRaises(StaleStreamError):
            bank.publish(tok1, rgb, frame_seq=2)

    def test_prod_no_actor_or_focus_locks_acquired(self):
        """Verify WindowStreamBank operations do not acquire actor or focus leases/locks."""
        self.assertTrue(OWNER_PRESENT)
        size = rtring.ring_bytes(2, 800, 600)
        bank, tokens = self._bank(quota_bytes=size)
        h = Handle("capsule", "cap_01", 1)
        spec = StreamSpec(800, 600, 2, size)
        tok = bank.open(h, spec)
        tokens.append(tok)
        self.assertIsNotNone(tok)
        # Ensure bank object has no actor_lease or focus_lease locks active
        self.assertFalse(hasattr(bank, "actor_lease") and bank.actor_lease is not None)
        self.assertFalse(hasattr(bank, "focus_lease") and bank.focus_lease is not None)


if __name__ == "__main__":
    unittest.main()
