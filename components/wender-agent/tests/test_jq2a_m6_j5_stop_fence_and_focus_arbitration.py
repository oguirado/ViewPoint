# @module {"id":"tests.test_jq2a_m6_j5_stop_fence_and_focus_arbitration","layer":3,"owner":"TESTS","deps":["wa.state.schema"]}
"""@module tests.test_jq2a_m6_j5_stop_fence_and_focus_arbitration - Contract tests for STOP fence and focus arbitration.

Gate G M6 5: STOP fences next send and focus serializes domains.
Owning implementation task_id: jq2a-m6-c1-window-scheduler.
Target module: wender.act.window_scheduler.WindowScheduler
"""

import importlib
import unittest
import numpy

try:
    from wender.perceive.session_registry import (
        Handle, TargetRecord, FrameRef, Observation, Receipt, ActionRequest,
        ActorLease, FocusLease, StreamSpec, StreamToken, Ticket, ChangeResult,
        PerceptionResult, WindowFrames, WindowDescription
    )
except ModuleNotFoundError:
    from dataclasses import dataclass

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


# @anchor:tests.test_jq2a_m6_j5_stop_fence_and_focus_arbitration.is_owner_present
def is_owner_present() -> bool:
    """Explicitly probe if owning implementation wender.act.window_scheduler is present."""
    try:
        mod = importlib.import_module("wender.act.window_scheduler")
        return hasattr(mod, "WindowScheduler")
    except ModuleNotFoundError as e:
        if e.name in (
            "wender.act.window_scheduler",
            "wender.perceive.session_registry",
            "wender.act.target_lock_registry",
            "wender.act.focus_policy",
            "wender.act.budget_governor",
        ):
            return False
        raise


# @anchor:tests.test_jq2a_m6_j5_stop_fence_and_focus_arbitration.expected_failure_if_owner_absent
def expected_failure_if_owner_absent(fn):
    """Apply expectedFailure decorator when the owning module is absent."""
    if not is_owner_present():
        return unittest.expectedFailure(fn)
    return fn


# @anchor:tests.test_jq2a_m6_j5_stop_fence_and_focus_arbitration.FakeWriterFence
class FakeWriterFence:
    """Mock writer fence harness for testing fence boundaries."""

    def __init__(self):
        self.stopped_handles = set()
        self.global_stopped = False
        self.sent_requests = []

    def stop(self, handle=None):
        if handle is None:
            self.global_stopped = True
        else:
            self.stopped_handles.add(handle)

    def check(self, handle):
        if self.global_stopped or handle in self.stopped_handles:
            raise RuntimeError("Writer fence tripped: STOP active")
        return True

    def send(self, request):
        self.check(request.handle)
        self.sent_requests.append(request)


# @anchor:tests.test_jq2a_m6_j5_stop_fence_and_focus_arbitration.TestContractHarnessFixtures
class TestContractHarnessFixtures(unittest.TestCase):
    """Positive control tests for shared contract records and fake harnesses."""

    def test_record_dataclasses_frozen_and_fields(self):
        h = Handle(kind="cdp_tab", canonical_id="tab-01", generation=1)
        self.assertEqual(h.kind, "cdp_tab")
        self.assertEqual(h.canonical_id, "tab-01")
        self.assertEqual(h.generation, 1)
        with self.assertRaises(Exception):
            h.generation = 2  # Frozen check

        req = ActionRequest(
            handle=h,
            action_id="act-101",
            epoch=1,
            payload={"type": "click"},
            cost=1,
            deadline_ns=1000000000,
        )
        self.assertEqual(req.action_id, "act-101")
        self.assertEqual(req.cost, 1)

    def test_fake_writer_fence_stops_sends(self):
        fence = FakeWriterFence()
        h1 = Handle(kind="cdp_tab", canonical_id="tab-01", generation=1)
        req = ActionRequest(
            handle=h1, action_id="a1", epoch=1, payload={}, cost=1, deadline_ns=100
        )

        fence.send(req)
        self.assertEqual(len(fence.sent_requests), 1)

        fence.stop(h1)
        with self.assertRaises(RuntimeError):
            fence.send(req)

    def test_lock_order_rank_rejection_harness(self):
        def check_lock_order(ranks):
            if any(ranks[i] > ranks[i + 1] for i in range(len(ranks) - 1)):
                raise ValueError("Inverted lock order rank detected")

        check_lock_order([1, 2, 3])  # Valid
        with self.assertRaises(ValueError):
            check_lock_order([3, 1])  # Inverted rank rejected immediately


# @anchor:tests.test_jq2a_m6_j5_stop_fence_and_focus_arbitration.TestStopFenceAndFocusArbitrationContract
class TestStopFenceAndFocusArbitrationContract(unittest.TestCase):
    """Gate G M6 5 contract tests: STOP fences next send and focus serializes domains."""

    @expected_failure_if_owner_absent
    def test_stop_fences_next_send_and_cleanup_attempts(self):
        from wender.act.window_scheduler import WindowScheduler

        from tests.test_jq2a_m6_j1_one_inflight_per_target_invariant import scheduler_fixture
        fixture = scheduler_fixture()
        sched = fixture.scheduler
        fence = fixture.fence

        h1 = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        req1 = ActionRequest(
            handle=h1, action_id="act-1", epoch=1, payload={"cmd": "click"}, cost=1, deadline_ns=1000
        )

        fixture.registry.add(h1, epoch=1)

        # Stop target h1
        sched.stop(h1)

        # Submitting or stepping request for stopped handle should not cross writer fence
        res = sched.submit(req1)
        receipts = sched.step(now_ns=500)

        # Verify negative mutant: no cleanup or retry send crosses fence after STOP
        self.assertEqual(len(fixture.dispatch.sends), 0)
        self.assertEqual((res.status, res.reason), ("REFUSED", "STOPPED"))
        self.assertEqual(receipts, [])

    @expected_failure_if_owner_absent
    def test_sent_work_retains_claim_across_stop(self):
        from wender.act.window_scheduler import WindowScheduler

        from tests.test_jq2a_m6_j1_one_inflight_per_target_invariant import scheduler_fixture
        fixture = scheduler_fixture()
        sched = fixture.scheduler
        fence = fixture.fence

        h1 = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        req1 = ActionRequest(
            handle=h1, action_id="act-1", epoch=1, payload={"cmd": "click"}, cost=1, deadline_ns=1000
        )

        fixture.registry.add(h1, epoch=1)
        fixture.dispatch.behavior = lambda request, context: context.send(
            fixture.dispatch.sends.append, request.action_id)
        ticket = sched.submit(req1)
        dispatches = sched.step(now_ns=100)

        # Work sent before STOP
        sched.stop(h1)

        # Sent work retains claim and cannot be recalled by STOP
        # Reconcile or terminal acknowledgment is required to release claim
        self.assertIsNotNone(ticket)
        self.assertIsInstance(ticket, Ticket)
        self.assertEqual(dispatches, [])
        self.assertEqual(fixture.dispatch.sends, ["act-1"])
        self.assertIn("cdp_tab:tab-1", fixture.locks.held)
        self.assertEqual([r.status for r in sched.step(now_ns=200)], ["UNKNOWN"])
        self.assertEqual(fixture.dispatch.sends, ["act-1"])
        sched.reconcile(h1, acknowledged=True)
        self.assertEqual(fixture.locks.held, {})

    @expected_failure_if_owner_absent
    def test_focus_policy_serializes_conflicting_domains_distinct_progress(self):
        from wender.act.window_scheduler import WindowScheduler

        from tests.test_jq2a_m6_j1_one_inflight_per_target_invariant import scheduler_fixture
        fixture = scheduler_fixture()
        sched = fixture.scheduler
        fence = fixture.fence

        h1 = Handle(kind="cdp_tab", canonical_id="domainA-tab1", generation=1)
        h2 = Handle(kind="cdp_tab", canonical_id="domainB-tab1", generation=1)

        req1 = ActionRequest(
            handle=h1, action_id="act-domA", epoch=1, payload={}, cost=1, deadline_ns=1000
        )
        req2 = ActionRequest(
            handle=h2, action_id="act-domB", epoch=1, payload={}, cost=1, deadline_ns=1000
        )

        fixture.registry.add(h1, epoch=1)
        fixture.registry.add(h2, epoch=1)
        self.assertIsInstance(sched.submit(req1), Ticket)
        self.assertIsInstance(sched.submit(req2), Ticket)

        # Distinct healthy input domains progress in arbitration
        receipts = sched.step(now_ns=200)
        self.assertIsNotNone(receipts)
        self.assertEqual([r.status for r in receipts], ["SUCCEEDED"])
        self.assertEqual([r.status for r in sched.step(now_ns=300)], ["SUCCEEDED"])
        self.assertEqual(fixture.dispatch.sends, ["act-domA", "act-domB"])

    @expected_failure_if_owner_absent
    def test_check_lock_order_rejects_reverse_ranks_without_hanging(self):
        from wender.act.window_scheduler import WindowScheduler

        # Expose check_lock_order(ranks) and reject inverted rank or canonical target order without waiting
        from wender.act.window_scheduler import check_lock_order
        with self.assertRaises(ValueError):
            check_lock_order([2, 1])


if __name__ == "__main__":
    unittest.main()
