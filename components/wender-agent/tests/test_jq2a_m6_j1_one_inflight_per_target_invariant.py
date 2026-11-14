"""Tests for Gate G M6 1: One Inflight Claim Per Target Invariant (INV-M6-2).

Task ID: jq2a-m6-j1-one-inflight-per-target-invariant
Owning implementation task_id: jq2a-m6-c1-window-scheduler
"""

from dataclasses import dataclass, field
import threading
import unittest
from typing import Dict, List, Optional, Set

import numpy as np

# Provider module probe / fallback record definitions
try:
    from wender.perceive.session_registry import (
        ActionRequest,
        ActorLease,
        ChangeResult,
        FocusLease,
        FrameRef,
        Handle,
        Observation,
        PerceptionResult,
        Receipt,
        StreamSpec,
        StreamToken,
        TargetRecord,
        Ticket,
        WindowDescription,
        WindowFrames,
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
        session_id: Optional[str]
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
        reason: Optional[str]

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
        claims: np.ndarray
        ops: dict

    @dataclass(frozen=True)
    class PerceptionResult:
        handle: Handle
        observation: Optional[Observation]
        error: Optional[str]

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
        error: Optional[str]


def expected_failure_if_owner_absent(func):
    """Applies unittest.expectedFailure only while the owner module/symbol is absent.

    Removes the decorator automatically when wender.act.window_scheduler.WindowScheduler exists.
    """
    try:
        from wender.act.window_scheduler import WindowScheduler
        has_owner = WindowScheduler is not None
    except ImportError:
        has_owner = False

    if not has_owner:
        return unittest.expectedFailure(func)
    return func


class ContractClaimHarness:
    """Contract test harness for Gate G M6 1 one-inflight-per-target invariant.

    Models barrier-interleaved submit, claim, and complete for aliases addressing
    one canonical destination vs independent targets.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._queued: Dict[str, ActionRequest] = {}
        self._inflight_by_canonical: Dict[str, Set[str]] = {}  # canonical_id -> set of action_ids
        self._state_history: Dict[str, List[str]] = {}  # action_id -> list of states
        self._terminal: Dict[str, Receipt] = {}
        self._quarantined: Set[str] = set()
        self._callback_held_locks: List[bool] = []

    def submit(self, request: ActionRequest) -> Ticket:
        with self._lock:
            self._queued[request.action_id] = request
            self._state_history.setdefault(request.action_id, []).append("queued")
            return Ticket(action_id=request.action_id, handle=request.handle)

    def claim_atomic(self, action_id: str, callback=None) -> bool:
        """Atomic check-and-claim under fence boundary (positive control behavior)."""
        with self._lock:
            if action_id not in self._queued:
                return False
            req = self._queued[action_id]
            canonical_id = req.handle.canonical_id
            active_claims = self._inflight_by_canonical.get(canonical_id, set())
            if len(active_claims) > 0:
                return False  # Target already has an inflight claim
            # Atomic state transition
            del self._queued[action_id]
            self._inflight_by_canonical.setdefault(canonical_id, set()).add(action_id)
            self._state_history[action_id].append("claimed")

        # Invoke callback outside lock to verify no mutex held across callbacks
        if callback:
            lock_held_during_cb = self._lock.locked()
            self._callback_held_locks.append(lock_held_during_cb)
            callback()
        return True

    def claim_non_atomic_mutant(self, action_id: str, barrier: threading.Barrier, callback=None) -> bool:
        """Negative mutant: non-atomic check then claim.

        Checks inflight status outside lock, waits at barrier, then acquires lock and claims.
        Exposes race condition where contended claims both dispatch inflight.
        """
        if action_id not in self._queued:
            return False
        req = self._queued[action_id]
        canonical_id = req.handle.canonical_id

        # Non-atomic check
        with self._lock:
            active_claims = self._inflight_by_canonical.get(canonical_id, set())
            is_busy = len(active_claims) > 0

        barrier.wait()  # Barrier interleave

        if is_busy:
            return False

        # Non-atomic claim after check
        with self._lock:
            if action_id in self._queued:
                del self._queued[action_id]
            self._inflight_by_canonical.setdefault(canonical_id, set()).add(action_id)
            self._state_history[action_id].append("claimed")

        if callback:
            lock_held_during_cb = self._lock.locked()
            self._callback_held_locks.append(lock_held_during_cb)
            callback()
        return True

    def complete(self, ticket: Ticket, result: Receipt) -> None:
        with self._lock:
            action_id = ticket.action_id
            canonical_id = ticket.handle.canonical_id
            active_claims = self._inflight_by_canonical.get(canonical_id, set())
            if action_id in active_claims:
                active_claims.remove(action_id)
                if not active_claims:
                    del self._inflight_by_canonical[canonical_id]
            self._terminal[action_id] = result
            self._state_history[action_id].append("terminal")

    def quarantine(self, ticket: Ticket) -> None:
        with self._lock:
            action_id = ticket.action_id
            canonical_id = ticket.handle.canonical_id
            active_claims = self._inflight_by_canonical.get(canonical_id, set())
            if action_id in active_claims:
                active_claims.remove(action_id)
                if not active_claims:
                    del self._inflight_by_canonical[canonical_id]
            self._quarantined.add(action_id)
            self._state_history[action_id].append("quarantined")

    def inflight_count_for_canonical(self, canonical_id: str) -> int:
        with self._lock:
            return len(self._inflight_by_canonical.get(canonical_id, set()))

    def total_inflight_count(self) -> int:
        with self._lock:
            return sum(len(s) for s in self._inflight_by_canonical.values())


class TestGateGM61OneInflightPerTargetInvariant(unittest.TestCase):
    """Harness unit tests for Gate G M6 1: Contended claims dispatch only once."""

    def test_positive_control_independent_targets_dispatch_concurrently(self):
        """Positive control: independent target destinations dispatch concurrently."""
        harness = ContractClaimHarness()
        h_dest1 = Handle(kind="cdp_tab", canonical_id="target_alpha", generation=1)
        h_dest2 = Handle(kind="x11_window", canonical_id="target_beta", generation=1)

        req1 = ActionRequest(handle=h_dest1, action_id="act_1", epoch=1, payload={}, cost=1, deadline_ns=1000)
        req2 = ActionRequest(handle=h_dest2, action_id="act_2", epoch=1, payload={}, cost=1, deadline_ns=1000)

        t1 = harness.submit(req1)
        t2 = harness.submit(req2)

        claimed1 = harness.claim_atomic("act_1")
        claimed2 = harness.claim_atomic("act_2")

        self.assertTrue(claimed1)
        self.assertTrue(claimed2)
        self.assertEqual(harness.total_inflight_count(), 2)
        self.assertEqual(harness.inflight_count_for_canonical("target_alpha"), 1)
        self.assertEqual(harness.inflight_count_for_canonical("target_beta"), 1)

    def test_positive_control_aliases_serialized_one_inflight(self):
        """Gate G M6 1: Aliases of one canonical destination serialize to at most one inflight claim."""
        harness = ContractClaimHarness()
        # Capsule and child xvfb_screen sharing canonical_id "target_gamma"
        h_capsule = Handle(kind="capsule", canonical_id="target_gamma", generation=1)
        h_screen = Handle(kind="xvfb_screen", canonical_id="target_gamma", generation=1)

        req1 = ActionRequest(handle=h_capsule, action_id="act_capsule", epoch=1, payload={}, cost=1, deadline_ns=1000)
        req2 = ActionRequest(handle=h_screen, action_id="act_screen", epoch=1, payload={}, cost=1, deadline_ns=1000)

        t1 = harness.submit(req1)
        t2 = harness.submit(req2)

        # First claim succeeds
        claimed1 = harness.claim_atomic("act_capsule")
        self.assertTrue(claimed1)
        self.assertEqual(harness.inflight_count_for_canonical("target_gamma"), 1)

        # Contended claim for alias destination fails while first is inflight
        claimed2 = harness.claim_atomic("act_screen")
        self.assertFalse(claimed2)
        self.assertEqual(harness.inflight_count_for_canonical("target_gamma"), 1)

        # Completing first claim frees canonical destination
        harness.complete(t1, Receipt(action_id="act_capsule", handle=h_capsule, status="ok", reason=None))
        self.assertEqual(harness.inflight_count_for_canonical("target_gamma"), 0)

        # Subsequent claim for alias now succeeds
        claimed2_retry = harness.claim_atomic("act_screen")
        self.assertTrue(claimed2_retry)
        self.assertEqual(harness.inflight_count_for_canonical("target_gamma"), 1)

    def test_negative_mutant_non_atomic_check_then_claim_violates_invariant(self):
        """Negative mutant: non-atomic check then claim allows multiple inflight claims for same target."""
        harness = ContractClaimHarness()
        h_alias1 = Handle(kind="cdp_tab", canonical_id="target_delta", generation=1)
        h_alias2 = Handle(kind="godot_window", canonical_id="target_delta", generation=1)

        req1 = ActionRequest(handle=h_alias1, action_id="act_m1", epoch=1, payload={}, cost=1, deadline_ns=1000)
        req2 = ActionRequest(handle=h_alias2, action_id="act_m2", epoch=1, payload={}, cost=1, deadline_ns=1000)

        harness.submit(req1)
        harness.submit(req2)

        barrier = threading.Barrier(2)
        results = [None, None]

        def thread_claim(idx, act_id):
            results[idx] = harness.claim_non_atomic_mutant(act_id, barrier)

        th1 = threading.Thread(target=thread_claim, args=(0, "act_m1"))
        th2 = threading.Thread(target=thread_claim, args=(1, "act_m2"))

        th1.start()
        th2.start()
        th1.join()
        th2.join()

        # Mutant permitted BOTH contended claims to dispatch inflight simultaneously
        self.assertTrue(results[0])
        self.assertTrue(results[1])
        # Invariant violation detected: inflight count for canonical_id is 2 (> 1)
        self.assertGreater(harness.inflight_count_for_canonical("target_delta"), 1)

    def test_attempt_identity_counted_exactly_once_no_mutex_in_callbacks(self):
        """Count each admitted attempt identity exactly once and verify no mutex held across callbacks."""
        harness = ContractClaimHarness()
        h_dest = Handle(kind="cdp_tab", canonical_id="target_epsilon", generation=1)
        req = ActionRequest(handle=h_dest, action_id="act_lifecycle", epoch=1, payload={}, cost=1, deadline_ns=1000)

        ticket = harness.submit(req)
        self.assertEqual(harness._state_history["act_lifecycle"], ["queued"])

        cb_called = []
        claimed = harness.claim_atomic("act_lifecycle", callback=lambda: cb_called.append(True))
        self.assertTrue(claimed)
        self.assertTrue(cb_called)
        self.assertEqual(harness._state_history["act_lifecycle"], ["queued", "claimed"])
        self.assertEqual(harness._callback_held_locks, [False], "No mutex must be held across injected callbacks")

        harness.complete(ticket, Receipt(action_id="act_lifecycle", handle=h_dest, status="ok", reason=None))
        self.assertEqual(harness._state_history["act_lifecycle"], ["queued", "claimed", "terminal"])

        # State transition history proves identity was counted exactly once per state stage
        history = harness._state_history["act_lifecycle"]
        self.assertEqual(len(history), 3)
        self.assertEqual(history.count("queued"), 1)
        self.assertEqual(history.count("claimed"), 1)
        self.assertEqual(history.count("terminal"), 1)


def scheduler_fixture():
    """Use the bounded scheduler's shared-fence provider contracts offline."""
    from types import SimpleNamespace
    from tests.test_jq2a_m6_c1_window_scheduler_implementation import (
        FakeClock, MetadataProbe, FakeRegistry, FakeLocks, FakeFocus,
        FakeBudget, FakeChain, FakeDispatch,
    )
    from wender.act.window_scheduler import WindowScheduler, WriterFence

    clock = FakeClock()
    fence = WriterFence(clock=clock)
    probe = MetadataProbe(fence)
    class Registry(FakeRegistry):
        def add(self, handle, *, epoch=0, resource=None, closed=False):
            from wender.perceive.session_registry import TargetRecord
            key = (handle.kind, handle.canonical_id, handle.generation)
            self.records[key] = TargetRecord(
                handle, object(), True, epoch, "offline-session", True, closed)
            self.resources[key] = resource or (handle.kind + ":" + handle.canonical_id)

    registry = Registry(probe)
    locks = FakeLocks(registry, probe)
    focus = FakeFocus(registry, probe)
    budget = FakeBudget(probe)
    dispatch = FakeDispatch(fence, probe)
    scheduler = WindowScheduler(
        registry=registry, locks=locks, focus_policy=focus, budget=budget,
        chain=FakeChain(probe), writer_fence=fence, dispatch=dispatch,
        clock=clock, max_queue=10, max_targets=5, service_cap_ns=10,
    )
    return SimpleNamespace(scheduler=scheduler, registry=registry, locks=locks,
                           focus=focus, fence=fence, dispatch=dispatch)


class TestWindowSchedulerOwnerContract(unittest.TestCase):
    """Production owner conformance tests for wender.act.window_scheduler.WindowScheduler.

    Probes the named owner module and symbol explicitly.
    Applies individually scoped expectedFailure while absent.
    """

    @expected_failure_if_owner_absent
    def test_window_scheduler_export_and_symbol(self):
        """Owner contract probe: Export WindowScheduler from wender.act.window_scheduler."""
        from wender.act.window_scheduler import WindowScheduler
        self.assertIsNotNone(WindowScheduler)

    @expected_failure_if_owner_absent
    def test_window_scheduler_one_inflight_per_canonical_target_invariant(self):
        """Owner contract: Contended claims dispatch only once per canonical target destination."""
        from wender.act.window_scheduler import WindowScheduler

        from wender.act.window_scheduler import Completion
        fixture = scheduler_fixture()
        scheduler = fixture.scheduler
        # Retain sent work until an explicit terminal acknowledgment arrives.
        fixture.dispatch.behavior = lambda request, context: context.send(
            fixture.dispatch.sends.append, request.action_id)

        h_capsule = Handle(kind="capsule", canonical_id="target_owner_1", generation=1)
        h_screen = Handle(kind="xvfb_screen", canonical_id="target_owner_1", generation=1)

        fixture.registry.add(h_capsule, epoch=1, resource="target_owner_1")
        fixture.registry.add(h_screen, epoch=1, resource="target_owner_1")

        req1 = ActionRequest(handle=h_capsule, action_id="req_owner_1", epoch=1, payload={}, cost=1, deadline_ns=1000)
        req2 = ActionRequest(handle=h_screen, action_id="req_owner_2", epoch=1, payload={}, cost=1, deadline_ns=1000)

        ticket1 = scheduler.submit(req1)
        ticket2 = scheduler.submit(req2)

        self.assertIsInstance(ticket1, Ticket)
        self.assertIsInstance(ticket2, Ticket)
        receipts_step1 = scheduler.step(now_ns=100)
        self.assertEqual(receipts_step1, [])  # Sent, awaiting acknowledgment.
        self.assertEqual(fixture.dispatch.sends, ["req_owner_1"])
        self.assertEqual(scheduler.step(now_ns=150), [])
        self.assertEqual(fixture.dispatch.sends, ["req_owner_1"])

        scheduler.complete(ticket1, Completion(
            "req_owner_1", h_capsule, "SUCCEEDED", True))
        receipts_step2 = scheduler.step(now_ns=200)
        self.assertEqual(len(receipts_step2), 1)
        self.assertEqual(receipts_step2[0].status, "SUCCEEDED")
        self.assertEqual(fixture.dispatch.sends, ["req_owner_1", "req_owner_2"])
        scheduler.complete(ticket2, Completion(
            "req_owner_2", h_screen, "SUCCEEDED", True))


if __name__ == "__main__":
    unittest.main()
