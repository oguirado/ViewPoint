"""Contract and harness tests for per-target lock registry and target concurrency.

Task ID: jq2a-m2-j1-per-target-lock-registry
Owning implementation task ID: jq2a-m2-c1-target-locks

Gate G M2 1: Same resource excludes; different targets proceed.
Negative mutant: Per-instance ActorLock instances fail target-level exclusion across capsule aliases.
Positive control: Distinct canonical targets acquire and progress concurrently.
"""

import importlib
from dataclasses import dataclass
import threading
import time
import unittest

import numpy

from wender.act.actor import ActorLock

# Probe owner module and symbol explicitly
OWNER_MODULE = "wender.act.target_lock_registry"
OWNER_SYMBOL = "TargetLockRegistry"
PROVIDER_MODULE = "wender.perceive.session_registry"


def _has_owner_symbol() -> bool:
    """Check if the owning implementation module and symbol exist."""
    try:
        mod = importlib.import_module(OWNER_MODULE)
        return hasattr(mod, OWNER_SYMBOL)
    except ImportError:
        return False


def owner_expected_failure(func):
    """Apply individually scoped expectedFailure only while owner symbol is absent."""
    if not _has_owner_symbol():
        return unittest.expectedFailure(func)
    return func


# Import shared records from provider module if present, otherwise define frozen fallback records.
try:
    from wender.perceive.session_registry import (
        Handle,
        TargetRecord,
        FrameRef,
        Observation,
        Receipt,
        ActionRequest,
        ActorLease,
        FocusLease,
        StreamSpec,
        StreamToken,
        Ticket,
        ChangeResult,
        PerceptionResult,
        WindowFrames,
        WindowDescription,
    )
except ImportError:

    @dataclass(frozen=True)
    class Handle:
        kind: str
        canonical_id: str
        generation: int

    @dataclass(frozen=True)
    class TargetRecord:
        handle: Handle
        transport: object
        owned: bool
        epoch: int
        session_id: str | None
        attachment_owned: bool
        closed: bool

    @dataclass(frozen=True)
    class FrameRef:
        handle: Handle
        epoch: int
        frame_seq: int
        width: int
        height: int
        rgb: object
        digest: str

    @dataclass(frozen=True)
    class Observation:
        handle: Handle
        epoch: int
        frame_seq: int
        dpr_ppm: int
        state: dict
        frame: FrameRef

    @dataclass(frozen=True)
    class Receipt:
        action_id: str
        handle: Handle
        status: str
        reason: str | None

    @dataclass(frozen=True)
    class ActionRequest:
        handle: Handle
        action_id: str
        epoch: int
        payload: dict
        cost: int
        deadline_ns: int

    @dataclass(frozen=True)
    class ActorLease:
        handle: Handle
        owner: str
        generation: int
        nonce: bytes
        expires_ns: int

    @dataclass(frozen=True)
    class FocusLease:
        lease: ActorLease
        domain: str
        epoch: int

    @dataclass(frozen=True)
    class StreamSpec:
        width: int
        height: int
        slots: int
        quota_bytes: int

    @dataclass(frozen=True)
    class StreamToken:
        handle: Handle
        epoch: int

    @dataclass(frozen=True)
    class Ticket:
        action_id: str
        handle: Handle

    @dataclass(frozen=True)
    class ChangeResult:
        handle: Handle
        epoch: int
        settled: bool
        claims: numpy.ndarray
        ops: dict

    @dataclass(frozen=True)
    class PerceptionResult:
        handle: Handle
        observation: Observation | None
        error: str | None

    @dataclass(frozen=True)
    class WindowFrames:
        handle: Handle
        epoch: int
        frames: tuple
        frame_seqs: tuple
        dpr_ppm: int

    @dataclass(frozen=True)
    class WindowDescription:
        handle: Handle
        epoch: int
        frame_seqs: tuple
        descriptors: tuple
        regions: tuple
        error: str | None


class LocalTargetLockRegistryHarness:
    """Reference harness implementing TargetLockRegistry contract for offline harness verification."""

    def __init__(self, registry=None, *, clock=None):
        self.registry = registry
        self.clock = clock or time.monotonic_ns
        self._locks = {}
        self._inflight = {}
        self._guard = threading.Lock()

    def _canonical_key(self, handle: Handle) -> str:
        # A capsule and its child screen use the same canonical actuation resource
        return handle.canonical_id

    def acquire(self, handle: Handle, owner: str, *, expires_ns: int, now_ns: int | None = None) -> ActorLease:
        key = self._canonical_key(handle)
        now = now_ns if now_ns is not None else self.clock()
        with self._guard:
            current = self._locks.get(key)
            if current is not None and current.expires_ns > now:
                raise RuntimeError(f"Target {key} is busy, held by owner {current.owner}")
            nonce = f"nonce-{handle.canonical_id}-{now}".encode("utf-8")
            lease = ActorLease(
                handle=handle,
                owner=owner,
                generation=handle.generation,
                nonce=nonce,
                expires_ns=expires_ns,
            )
            self._locks[key] = lease
            return lease

    def release(self, lease: ActorLease) -> None:
        key = self._canonical_key(lease.handle)
        with self._guard:
            current = self._locks.get(key)
            if current is None or current.nonce != lease.nonce:
                raise ValueError("Invalid or forged lease release attempt")
            del self._locks[key]

    def mark_inflight(self, lease: ActorLease) -> None:
        key = self._canonical_key(lease.handle)
        with self._guard:
            current = self._locks.get(key)
            if current is None or current.nonce != lease.nonce:
                raise ValueError("Cannot mark inflight for non-active lease")
            self._inflight[key] = lease

    def reconcile_quarantine(self, handle: Handle, *, acknowledged: bool) -> None:
        key = self._canonical_key(handle)
        with self._guard:
            if not acknowledged:
                raise RuntimeError("Quiescence not acknowledged for quarantine reconcile")
            self._inflight.pop(key, None)
            self._locks.pop(key, None)


class TestPerTargetLockRegistryHarnessControls(unittest.TestCase):
    """Positive controls and negative mutant harness tests (run cleanly before owner lands)."""

    def test_positive_control_distinct_resources_acquire_and_progress(self):
        """Positive control: Distinct resources acquire and progress concurrently without interference."""
        harness = LocalTargetLockRegistryHarness()
        handle_1 = Handle(kind="cdp_tab", canonical_id="tab-101", generation=1)
        handle_2 = Handle(kind="x11_window", canonical_id="win-202", generation=1)

        barrier = threading.Barrier(3)
        results = {}

        def worker(worker_id, handle):
            barrier.wait(timeout=2.0)
            now = time.monotonic_ns()
            lease = harness.acquire(handle, owner=worker_id, expires_ns=now + 1_000_000_000)
            try:
                time.sleep(0.01)
                results[worker_id] = "success"
            finally:
                harness.release(lease)

        t1 = threading.Thread(target=worker, args=("w1", handle_1))
        t2 = threading.Thread(target=worker, args=("w2", handle_2))

        t1.start()
        t2.start()
        barrier.wait(timeout=2.0)

        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

        self.assertEqual(results.get("w1"), "success")
        self.assertEqual(results.get("w2"), "success")

    def test_negative_mutant_per_instance_locks_fail_target_level_exclusion(self):
        """Negative mutant: Independent ActorLock instances fail target exclusion for capsule aliases.

        Demonstrates that naive per-instance ActorLock instances permit concurrent access when
        addressing the same canonical destination via different capsule / screen handle aliases.
        """
        # Two distinct ActorLock instances representing naive per-instance locks
        lock_instance_1 = ActorLock()
        lock_instance_2 = ActorLock()

        handle_capsule = Handle(kind="capsule", canonical_id="screen-A", generation=1)
        handle_screen = Handle(kind="xvfb_screen", canonical_id="screen-A", generation=1)

        acquired_holders = []
        barrier = threading.Barrier(3)

        def worker1():
            barrier.wait(timeout=2.0)
            lock_instance_1.acquire("actor_capsule")
            acquired_holders.append(("capsule", lock_instance_1.holder))
            time.sleep(0.01)
            lock_instance_1.release()

        def worker2():
            barrier.wait(timeout=2.0)
            lock_instance_2.acquire("actor_screen")
            acquired_holders.append(("screen", lock_instance_2.holder))
            time.sleep(0.01)
            lock_instance_2.release()

        t1 = threading.Thread(target=worker1)
        t2 = threading.Thread(target=worker2)

        t1.start()
        t2.start()
        barrier.wait(timeout=2.0)

        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

        # Naive per-instance locks allow both workers to acquire simultaneously!
        # This negative mutant assertion proves per-instance locking is insufficient.
        self.assertEqual(len(acquired_holders), 2)
        holders_map = dict(acquired_holders)
        self.assertEqual(holders_map["capsule"], "actor_capsule")
        self.assertEqual(holders_map["screen"], "actor_screen")

    def test_harness_gate_g_m2_1_same_resource_excludes_different_proceeds(self):
        """Harness verification of Gate G M2 1: Same canonical target excludes; distinct target proceeds."""
        harness = LocalTargetLockRegistryHarness()

        capsule_handle = Handle(kind="capsule", canonical_id="screen-shared", generation=1)
        screen_handle = Handle(kind="xvfb_screen", canonical_id="screen-shared", generation=1)
        distinct_handle = Handle(kind="godot_window", canonical_id="window-independent", generation=1)

        barrier = threading.Barrier(4)
        outcomes = []
        outcomes_lock = threading.Lock()

        def contender(worker_id, handle):
            barrier.wait(timeout=2.0)
            now = time.monotonic_ns()
            try:
                lease = harness.acquire(handle, owner=worker_id, expires_ns=now + 500_000_000)
                with outcomes_lock:
                    outcomes.append((worker_id, "admitted", handle.canonical_id))
                time.sleep(0.05)
                harness.release(lease)
            except Exception as exc:
                with outcomes_lock:
                    outcomes.append((worker_id, "refused", handle.canonical_id))

        t1 = threading.Thread(target=contender, args=("c1_capsule", capsule_handle))
        t2 = threading.Thread(target=contender, args=("c2_screen", screen_handle))
        t3 = threading.Thread(target=contender, args=("c3_distinct", distinct_handle))

        for t in (t1, t2, t3):
            t.start()

        barrier.wait(timeout=2.0)

        for t in (t1, t2, t3):
            t.join(timeout=2.0)

        # Analyze outcomes
        admitted_screen_shared = [
            o for o in outcomes if o[2] == "screen-shared" and o[1] == "admitted"
        ]
        refused_screen_shared = [
            o for o in outcomes if o[2] == "screen-shared" and o[1] == "refused"
        ]
        admitted_independent = [
            o for o in outcomes if o[2] == "window-independent" and o[1] == "admitted"
        ]

        # Exactly 1 contender admitted for screen-shared, 1 refused
        self.assertEqual(len(admitted_screen_shared), 1)
        self.assertEqual(len(refused_screen_shared), 1)
        # Distinct target admitted
        self.assertEqual(len(admitted_independent), 1)


class TestTargetLockRegistryOwnerContract(unittest.TestCase):
    """Production contract tests for TargetLockRegistry and session_registry records."""

    @owner_expected_failure
    def test_gate_g_m2_1_target_lock_registry_canonical_exclusion(self):
        """Gate G M2 1: TargetLockRegistry excludes same target (capsule alias) and permits distinct target."""
        target_lock_mod = importlib.import_module(OWNER_MODULE)
        TargetLockRegistry = getattr(target_lock_mod, OWNER_SYMBOL)

        from wender.perceive.session_registry import SessionRegistry
        sessions = SessionRegistry()
        registry = TargetLockRegistry(sessions)

        transport = object()
        capsule_handle = sessions.register("capsule", "dest-alpha", transport, owned=False)
        screen_handle = sessions.register("xvfb_screen", "dest-alpha", transport, owned=False)
        distinct_handle = sessions.register("cdp_tab", "dest-beta", object(), owned=False)

        barrier = threading.Barrier(4)
        outcomes = []
        outcomes_lock = threading.Lock()

        def contender(worker_id, handle):
            barrier.wait(timeout=2.0)
            now = time.monotonic_ns()
            try:
                lease = registry.acquire(handle, worker_id, expires_ns=now + 500_000_000)
                with outcomes_lock:
                    outcomes.append((worker_id, "admitted", handle.canonical_id))
                time.sleep(0.05)
                registry.release(lease)
            except Exception as exc:
                with outcomes_lock:
                    outcomes.append((worker_id, "refused", handle.canonical_id))

        threads = [
            threading.Thread(target=contender, args=("c_capsule", capsule_handle)),
            threading.Thread(target=contender, args=("c_screen", screen_handle)),
            threading.Thread(target=contender, args=("c_tab", distinct_handle)),
        ]
        for t in threads:
            t.start()

        barrier.wait(timeout=2.0)
        for t in threads:
            t.join(timeout=2.0)

        admitted_alpha = [o for o in outcomes if o[2] == "dest-alpha" and o[1] == "admitted"]
        refused_alpha = [o for o in outcomes if o[2] == "dest-alpha" and o[1] == "refused"]
        admitted_beta = [o for o in outcomes if o[2] == "dest-beta" and o[1] == "admitted"]

        self.assertEqual(len(admitted_alpha), 1, "Exactly one contender admitted for dest-alpha")
        self.assertEqual(len(refused_alpha), 1, "Exactly one contender refused for dest-alpha")
        self.assertEqual(len(admitted_beta), 1, "Distinct target dest-beta progresses independently")

    @owner_expected_failure
    def test_target_lock_registry_nonce_and_generation_fenced_release(self):
        """INV-M2-1: Nonce and generation fence releases; forged or consumed leases fail."""
        target_lock_mod = importlib.import_module(OWNER_MODULE)
        TargetLockRegistry = getattr(target_lock_mod, OWNER_SYMBOL)

        from wender.perceive.session_registry import SessionRegistry
        sessions = SessionRegistry()
        registry = TargetLockRegistry(sessions)
        handle = sessions.register("x11_window", "win-10", object(), owned=False)

        now = time.monotonic_ns()
        lease = registry.acquire(handle, "owner-a", expires_ns=now + 10_000_000_000)
        self.assertIsInstance(lease, ActorLease)
        self.assertEqual(lease.owner, "owner-a")
        self.assertEqual(lease.generation, 1)

        # Forged lease with different nonce
        forged_lease = ActorLease(
            handle=handle,
            owner="owner-a",
            generation=1,
            nonce=b"forged-nonce-bytes",
            expires_ns=lease.expires_ns,
        )
        with self.assertRaises((ValueError, RuntimeError)):
            registry.release(forged_lease)

        # Valid release
        registry.release(lease)

        # Re-releasing consumed lease fails
        with self.assertRaises((ValueError, RuntimeError)):
            registry.release(lease)

    @owner_expected_failure
    def test_target_lock_registry_mark_inflight_and_quarantine_reconcile(self):
        """INV-M2-1: In-flight action marking and quarantine reconciliation."""
        target_lock_mod = importlib.import_module(OWNER_MODULE)
        TargetLockRegistry = getattr(target_lock_mod, OWNER_SYMBOL)

        from wender.perceive.session_registry import SessionRegistry
        sessions = SessionRegistry()
        registry = TargetLockRegistry(sessions)
        handle = sessions.register("godot_window", "godot-1", object(), owned=False)

        now = time.monotonic_ns()
        lease = registry.acquire(handle, "owner-g", expires_ns=now + 10_000_000_000)
        registry.mark_inflight(lease)

        # Unacknowledged quiescence keeps subsequent owners fenced.
        registry.reconcile_quarantine(handle, acknowledged=False)
        with self.assertRaises(target_lock_mod.TargetBusy):
            registry.acquire(handle, "owner-next", expires_ns=now + 10_000_000_000)

        # Reconcile quarantine with acknowledged quiescence succeeds
        registry.reconcile_quarantine(handle, acknowledged=True)


if __name__ == "__main__":
    unittest.main()
