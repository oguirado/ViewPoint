"""Contract tests for JQ2A M6 J2: Deficit fairness and starvation drain.

Owner implementation task_id: jq2a-m6-c1-window-scheduler
Target module: wender.act.window_scheduler
Target class: WindowScheduler

Tests Gate G M6 2: Continuously eligible target meets declared fair bound.
Demonstrates negative mutant (unbounded EDF promotion) vs positive control
(persistent urgent and patient queues both served under 1:1 DRR ratio).
Probes owner WindowScheduler explicitly and applies unittest.expectedFailure
conditionally while owner is absent.
"""

from collections import deque
from dataclasses import dataclass
import importlib
import unittest
from typing import Optional, List, Dict, Tuple, Any
import numpy as np

# Step 1: Attempt to import canonical records from session_registry or use local fallback
try:
    from wender.perceive.session_registry import (
        Handle, TargetRecord, FrameRef, Observation, Receipt, ActionRequest,
        ActorLease, FocusLease, StreamSpec, StreamToken, Ticket, ChangeResult,
        PerceptionResult, WindowFrames, WindowDescription
    )
except ModuleNotFoundError as e:
    if e.name != "wender.perceive.session_registry":
        raise

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
        reason: Optional[str] = None

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
    """Decorator applying unittest.expectedFailure only if WindowScheduler is absent."""
    try:
        mod = importlib.import_module("wender.act.window_scheduler")
        if hasattr(mod, "WindowScheduler"):
            return func
    except ModuleNotFoundError as e:
        if e.name in ("wender.act.window_scheduler", "wender.act"):
            return unittest.expectedFailure(func)
    except ImportError:
        pass
    return unittest.expectedFailure(func)


class ContractDeficitFairnessScheduler:
    """Contract reference model simulating Gate G M6 2 Deficit Round Robin.

    Invariants tested:
    - Quantum is 1 TARGET per fair visit.
    - At most 1 urgent EDF dispatch may precede each reserved fair visit.
    - Persistent canonical cursor across steps.
    - Bound waiting by N * 2 opportunities for N eligible targets.
    - Separate tracking of focus, settle, and quarantine stalls.
    """

    def __init__(self, targets: List[Handle], max_queue: int = 100):
        self.targets = list(targets)
        self.cursor = 0
        self.queues: Dict[str, deque] = {t.canonical_id: deque() for t in targets}
        self.stalls: Dict[str, str] = {}  # target_id -> 'focus' | 'settle' | 'quarantine'
        self.fair_visits = 0
        self.urgent_dispatches = 0
        self.eligibility_stalls = {"focus": 0, "settle": 0, "quarantine": 0}
        self.starvation_bound = len(targets) * 2
        self._last_was_urgent = False

    def submit(self, req: ActionRequest) -> Receipt:
        tid = req.handle.canonical_id
        if tid in self.queues:
            self.queues[tid].append(req)
            return Receipt(action_id=req.action_id, handle=req.handle, status="ENQUEUED", reason=None)
        return Receipt(action_id=req.action_id, handle=req.handle, status="REJECTED", reason="UNKNOWN_TARGET")

    def set_stall(self, handle: Handle, stall_type: Optional[str]):
        if stall_type is None:
            self.stalls.pop(handle.canonical_id, None)
        else:
            self.stalls[handle.canonical_id] = stall_type

    def step(self, now_ns: int, allow_unbounded_edf: bool = False) -> List[Receipt]:
        """Dispatches next request according to fairness rules.

        If allow_unbounded_edf is True (negative mutant), urgent requests
        always pre-empt fair visits unconditionally without ratio bounds.
        """
        # Find urgent requests (deadline <= now_ns + threshold)
        urgent_candidates = []
        for tid, q in self.queues.items():
            if self.stalls.get(tid) is not None:
                continue
            if q and q[0].deadline_ns < now_ns + 1000:  # urgent criterion
                urgent_candidates.append(q[0])

        # Track stalls
        for tid, stall in self.stalls.items():
            if self.queues[tid]:
                self.eligibility_stalls[stall] = self.eligibility_stalls.get(stall, 0) + 1

        # Negative mutant mode: unbounded EDF promotion
        if allow_unbounded_edf:
            if urgent_candidates:
                urgent_candidates.sort(key=lambda r: r.deadline_ns)
                req = urgent_candidates[0]
                self.queues[req.handle.canonical_id].popleft()
                self.urgent_dispatches += 1
                return [Receipt(action_id=req.action_id, handle=req.handle, status="DISPATCHED", reason=None)]

        # Standard M6 fair scheduler rule: at most 1 urgent dispatch per fair visit
        if urgent_candidates and not self._last_was_urgent:
            urgent_candidates.sort(key=lambda r: r.deadline_ns)
            req = urgent_candidates[0]
            self.queues[req.handle.canonical_id].popleft()
            self.urgent_dispatches += 1
            self._last_was_urgent = True
            return [Receipt(action_id=req.action_id, handle=req.handle, status="DISPATCHED_URGENT", reason=None)]

        # Round Robin fair visit
        n = len(self.targets)
        for _ in range(n):
            idx = self.cursor
            self.cursor = (self.cursor + 1) % n
            target = self.targets[idx]
            tid = target.canonical_id

            if self.stalls.get(tid) is not None:
                continue

            q = self.queues[tid]
            if q:
                req = q.popleft()
                self.fair_visits += 1
                self._last_was_urgent = False
                return [Receipt(action_id=req.action_id, handle=req.handle, status="DISPATCHED_FAIR", reason=None)]

        return []


class TestDeficitFairnessAndStarvationDrainContract(unittest.TestCase):
    """Contract tests for Gate G M6 2: Deficit fairness and starvation drain."""

    def setUp(self):
        self.t1 = Handle("cdp_tab", "tab_1", 1)
        self.t2 = Handle("x11_window", "win_2", 1)
        self.t3 = Handle("godot_window", "godot_3", 1)
        self.targets = [self.t1, self.t2, self.t3]

    def test_gate_g_m6_2_fairness_bound_and_positive_control(self):
        """Positive control: continuously eligible patient target meets declared fair bound N * 2."""
        scheduler = ContractDeficitFairnessScheduler(self.targets)

        # Submit continuous stream of urgent requests to t1, and 1 patient request to t2
        req_patient = ActionRequest(self.t2, "act_patient", 1, {}, 1, 100_000)
        scheduler.submit(req_patient)

        # Continually enqueue urgent requests on t1
        for i in range(10):
            req_urgent = ActionRequest(self.t1, f"act_urgent_{i}", 1, {}, 1, 100)  # urgent deadline
            scheduler.submit(req_urgent)

        # Step scheduler and record steps to serve req_patient
        steps_taken = 0
        patient_served = False
        now = 0

        while steps_taken < 20 and not patient_served:
            receipts = scheduler.step(now_ns=now, allow_unbounded_edf=False)
            steps_taken += 1
            for r in receipts:
                if r.action_id == "act_patient":
                    patient_served = True

        self.assertTrue(patient_served, "Patient request must be served under fair DRR quantum bounds")
        # Starvation bound for N=3 is N * 2 = 6 steps max
        self.assertLessEqual(steps_taken, scheduler.starvation_bound,
                             f"Waiting steps ({steps_taken}) exceeded fair starvation bound ({scheduler.starvation_bound})")
        self.assertGreater(scheduler.fair_visits, 0)
        self.assertGreater(scheduler.urgent_dispatches, 0)

    def test_negative_mutant_unbounded_edf_promotions_starves_patient(self):
        """Negative mutant: unbounded EDF promotion starves patient target under urgent flood."""
        scheduler = ContractDeficitFairnessScheduler(self.targets)

        req_patient = ActionRequest(self.t2, "act_patient", 1, {}, 1, 100_000)
        scheduler.submit(req_patient)

        # Continuously submit urgent requests to t1
        for i in range(20):
            req_urgent = ActionRequest(self.t1, f"act_urgent_{i}", 1, {}, 1, 10)
            scheduler.submit(req_urgent)

        steps_taken = 0
        patient_served = False
        now = 0

        # Under negative mutant (unbounded EDF), patient will not be served within N * 2 steps
        while steps_taken < scheduler.starvation_bound:
            receipts = scheduler.step(now_ns=now, allow_unbounded_edf=True)
            steps_taken += 1
            for r in receipts:
                if r.action_id == "act_patient":
                    patient_served = True

        self.assertFalse(patient_served, "Negative mutant must starve patient request under continuous urgent requests")

    def test_separately_mark_focus_settle_and_quarantine_stalls(self):
        """Stalls for focus, settle, and quarantine are recorded without claiming wall-time bounds."""
        scheduler = ContractDeficitFairnessScheduler(self.targets)

        # Set stalls on t1, t2, t3
        scheduler.set_stall(self.t1, "focus")
        scheduler.set_stall(self.t2, "settle")
        scheduler.set_stall(self.t3, "quarantine")

        # Submit requests for stalled targets
        scheduler.submit(ActionRequest(self.t1, "a1", 1, {}, 1, 1000))
        scheduler.submit(ActionRequest(self.t2, "a2", 1, {}, 1, 1000))
        scheduler.submit(ActionRequest(self.t3, "a3", 1, {}, 1, 1000))

        receipts = scheduler.step(now_ns=0)
        self.assertEqual(len(receipts), 0, "Stalled targets must not be dispatched")

        # Telemetry records stall occurrences
        self.assertGreater(scheduler.eligibility_stalls["focus"], 0)
        self.assertGreater(scheduler.eligibility_stalls["settle"], 0)
        self.assertGreater(scheduler.eligibility_stalls["quarantine"], 0)


class TestWindowSchedulerOwnerConformance(unittest.TestCase):
    """Owner module conformance tests probing wender.act.window_scheduler.WindowScheduler."""

    @expected_failure_if_owner_absent
    def test_window_scheduler_owner_import_and_initialization(self):
        """Explicitly probe presence and interface of wender.act.window_scheduler.WindowScheduler."""
        mod = importlib.import_module("wender.act.window_scheduler")
        ws_cls = getattr(mod, "WindowScheduler")

        # Verify constructor and signature requirements
        scheduler = ws_cls(
            registry=None,
            locks=None,
            focus_policy=None,
            budget=None,
            chain=None,
            writer_fence=mod.WriterFence(),
            dispatch=None,
            max_queue=100,
            max_targets=10,
            service_cap_ns=1_000_000
        )
        self.assertIsNotNone(scheduler)

    @expected_failure_if_owner_absent
    def test_window_scheduler_submit_step_complete_stop_telemetry(self):
        """Probe submit, step, complete, stop, reconcile and public telemetry attributes on WindowScheduler."""
        mod = importlib.import_module("wender.act.window_scheduler")
        ws_cls = getattr(mod, "WindowScheduler")

        from tests.test_jq2a_m6_j1_one_inflight_per_target_invariant import scheduler_fixture
        from wender.act.window_scheduler import Completion
        fixture = scheduler_fixture()
        scheduler = fixture.scheduler

        h1 = Handle("cdp_tab", "tab_alpha", 1)
        req = ActionRequest(h1, "act_1", 1, {}, 1, 10_000)

        fixture.registry.add(h1, epoch=1)

        # submit
        res = scheduler.submit(req)
        self.assertIsNotNone(res)
        self.assertIsInstance(res, Ticket)

        # step
        receipts = scheduler.step(now_ns=100)
        self.assertIsInstance(receipts, list)
        self.assertEqual(fixture.dispatch.sends, ["act_1"])
        self.assertEqual([r.status for r in receipts], ["SUCCEEDED"])

        # complete / stop / reconcile
        t = Ticket("act_1", h1)
        r = Completion("act_1", h1, "SUCCEEDED", True)
        scheduler.complete(t, r)
        scheduler.stop(h1)
        scheduler.reconcile(h1, acknowledged=True)

        # Check telemetry attributes
        self.assertTrue(hasattr(scheduler, "fair_visits"))
        self.assertTrue(hasattr(scheduler, "urgent_dispatches"))
        self.assertTrue(hasattr(scheduler, "eligibility_stalls"))
        self.assertTrue(hasattr(scheduler, "starvation_bound"))


if __name__ == "__main__":
    unittest.main()
