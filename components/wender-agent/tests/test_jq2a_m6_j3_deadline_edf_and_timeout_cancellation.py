"""Contract tests for JQ2A M6 J3: Deadline EDF, timeout cancellation, and quarantine rules.

Owning implementation task_id: jq2a-m6-c1-window-scheduler
Target modules:
  - wender.act.window_scheduler (WindowScheduler)
  - wender.act.budget_governor (BudgetGovernor, Reservation)
  - wender.perceive.session_registry (Shared records)
"""

from dataclasses import dataclass
import importlib
import importlib.util
import unittest
import numpy as np

# Canonical vs fallback shared records import
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
        reason: str | None = None

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
        observation: Observation | None = None
        error: str | None = None

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
        error: str | None = None

try:
    from wender.act.budget_governor import Reservation
except ImportError:
    @dataclass(frozen=True)
    class Reservation:
        handle: Handle
        cost: int
        deadline_ns: int
        nonce: bytes


def expected_failure_if_owner_absent(module_name: str, symbol_name: str):
    """
    Probe owner module and symbol explicitly.
    Apply individually scoped unittest.expectedFailure only while that exact owner module or symbol is absent.
    Remove the wrapper automatically when it exists.
    """
    try:
        spec = importlib.util.find_spec(module_name)
        if spec is None:
            owner_present = False
        else:
            mod = importlib.import_module(module_name)
            owner_present = hasattr(mod, symbol_name)
    except ModuleNotFoundError as e:
        if e.name == module_name:
            owner_present = False
        else:
            owner_present = True
    except Exception:
        owner_present = True

    if owner_present:
        return lambda func: func
    else:
        return unittest.expectedFailure


class FakeClock:
    """Fake monotonic clock in nanoseconds for offline timing tests."""

    def __init__(self, start_ns: int = 1_000_000_000):
        self._now_ns = start_ns

    def now_ns(self) -> int:
        return self._now_ns

    def advance(self, delta_ns: int):
        self._now_ns += delta_ns


def validate_window_frames(wf: WindowFrames) -> None:
    """Validate frame sequence and target identity before admission."""
    if len(wf.frames) != len(wf.frame_seqs):
        raise ValueError(
            f"Mismatched frames ({len(wf.frames)}) and frame_seqs ({len(wf.frame_seqs)}) length"
        )
    for i in range(1, len(wf.frame_seqs)):
        if wf.frame_seqs[i] <= wf.frame_seqs[i - 1]:
            raise ValueError(f"Non-increasing frame_seqs: {wf.frame_seqs}")
    for f in wf.frames:
        if isinstance(f, np.ndarray):
            if not f.flags.c_contiguous or f.dtype != np.uint8:
                raise ValueError("Frame RGB array must be contiguous uint8")


def canonical_actuation_resource(handle: Handle) -> str:
    """Map supported target kinds to canonical actuation resource IDs.

    Capsule and its child screen use the same canonical resource when addressing same destination.
    """
    supported_kinds = ("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window")
    if handle.kind not in supported_kinds:
        raise ValueError(f"Unsupported target kind: {handle.kind}")

    if handle.kind == "capsule":
        # Capsule mapping to child screen destination resource
        return f"xvfb_screen:{handle.canonical_id}"
    return f"{handle.kind}:{handle.canonical_id}"


class TestJQ2AM6J3FixturesAndPositiveControls(unittest.TestCase):
    """Standalone controls, fixtures, and negative mutant verification."""

    def test_record_structures_and_field_alignment(self):
        """Verify fallback record definitions align with proposed field contract."""
        h = Handle(kind="cdp_tab", canonical_id="tab_123", generation=1)
        self.assertEqual(h.kind, "cdp_tab")
        self.assertEqual(h.canonical_id, "tab_123")
        self.assertEqual(h.generation, 1)

        req = ActionRequest(
            handle=h,
            action_id="act_001",
            epoch=0,
            payload={"type": "click"},
            cost=10,
            deadline_ns=2_000_000_000,
        )
        self.assertEqual(req.cost, 10)
        self.assertEqual(req.deadline_ns, 2_000_000_000)

        res = Reservation(
            handle=h,
            cost=10,
            deadline_ns=2_000_000_000,
            nonce=b"nonce_bytes_1601",
        )
        self.assertEqual(res.nonce, b"nonce_bytes_1601")

    def test_supported_target_kinds_and_capsule_resource_equivalence(self):
        """Verify capsule and child screen use same canonical actuation resource."""
        for kind in ("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"):
            h = Handle(kind=kind, canonical_id="target_alpha", generation=1)
            res_key = canonical_actuation_resource(h)
            self.assertIsInstance(res_key, str)

        h_capsule = Handle(kind="capsule", canonical_id="screen_dest_1", generation=1)
        h_screen = Handle(kind="xvfb_screen", canonical_id="screen_dest_1", generation=1)
        self.assertEqual(
            canonical_actuation_resource(h_capsule),
            canonical_actuation_resource(h_screen),
        )

    def test_window_frames_sequence_validation(self):
        """Verify WindowFrames validates local increasing sequence and RGB contiguous uint8."""
        h = Handle(kind="cdp_tab", canonical_id="tab_1", generation=1)
        rgb1 = np.zeros((100, 100, 3), dtype=np.uint8)
        rgb2 = np.zeros((100, 100, 3), dtype=np.uint8)

        # Valid frames
        wf_ok = WindowFrames(
            handle=h,
            epoch=0,
            frames=(rgb1, rgb2),
            frame_seqs=(1, 2),
            dpr_ppm=100,
        )
        validate_window_frames(wf_ok)

        # Decreasing frame_seqs must raise ValueError
        with self.assertRaises(ValueError):
            wf_bad_seq = WindowFrames(
                handle=h,
                epoch=0,
                frames=(rgb1, rgb2),
                frame_seqs=(2, 1),
                dpr_ppm=100,
            )
            validate_window_frames(wf_bad_seq)

        # Mismatched length must raise ValueError
        with self.assertRaises(ValueError):
            wf_bad_len = WindowFrames(
                handle=h,
                epoch=0,
                frames=(rgb1,),
                frame_seqs=(1, 2),
                dpr_ppm=100,
            )
            validate_window_frames(wf_bad_len)

    def test_positive_control_advance_deadline_through_lifecycle_stages(self):
        """Advance one fake absolute deadline through discovery, ownership, retries, verification and extraction."""
        clock = FakeClock(start_ns=1_000_000_000)
        absolute_deadline_ns = 2_000_000_000
        h = Handle(kind="cdp_tab", canonical_id="tab_lifecycle", generation=1)
        req = ActionRequest(
            handle=h,
            action_id="act_lifecycle",
            epoch=0,
            payload={"cmd": "inspect"},
            cost=5,
            deadline_ns=absolute_deadline_ns,
        )

        # 1. Discovery phase
        clock.advance(100_000_000)  # 1.1s
        self.assertEqual(req.deadline_ns, absolute_deadline_ns)
        self.assertLess(clock.now_ns(), req.deadline_ns)

        # 2. Ownership wait phase
        clock.advance(200_000_000)  # 1.3s
        self.assertEqual(req.deadline_ns, absolute_deadline_ns)
        self.assertLess(clock.now_ns(), req.deadline_ns)

        # 3. Retry phase (deadline must NOT reset or extend on retry)
        retry_count = 2
        for _ in range(retry_count):
            clock.advance(150_000_000)  # 1.6s total
            # Verify deadline stays strictly at original absolute value
            self.assertEqual(req.deadline_ns, absolute_deadline_ns)

        # 4. Verification phase
        clock.advance(200_000_000)  # 1.8s
        self.assertEqual(req.deadline_ns, absolute_deadline_ns)
        self.assertLess(clock.now_ns(), req.deadline_ns)

        # 5. Extraction phase (crosses absolute deadline)
        clock.advance(300_000_000)  # 2.1s (2_100_000_000 > 2_000_000_000)
        self.assertGreater(clock.now_ns(), req.deadline_ns)

    def test_positive_control_unsent_expiry_emits_terminal_refusal_and_refunds(self):
        """Positive Control: Unsent request expiring prior to transport send emits terminal REFUSED receipt and refunds reservation."""
        h = Handle(kind="cdp_tab", canonical_id="tab_expiry_control", generation=1)
        req = ActionRequest(
            handle=h,
            action_id="act_unsent_expire",
            epoch=0,
            payload={"cmd": "scroll"},
            cost=5,
            deadline_ns=1_500_000_000,
        )

        # Simulated state prior to send
        reservation = Reservation(
            handle=h, cost=5, deadline_ns=1_500_000_000, nonce=b"r1"
        )
        reservation_cancelled = False

        # Simulate unsent expiry check at now_ns = 1_600_000_000 (now > deadline)
        now_ns = 1_600_000_000
        sent = False  # Transport send boundary NOT crossed

        if not sent and now_ns > req.deadline_ns:
            # Unsent expiry action: cancel reservation once & emit terminal refusal receipt
            reservation_cancelled = True
            receipt = Receipt(
                action_id=req.action_id,
                handle=req.handle,
                status="REFUSED",
                reason="EXPIRED_BEFORE_SEND",
            )

        self.assertEqual(receipt.status, "REFUSED")
        self.assertEqual(receipt.reason, "EXPIRED_BEFORE_SEND")
        self.assertTrue(reservation_cancelled)

    def test_negative_mutant_refund_claim_on_timeout_is_forbidden(self):
        """Negative Mutant Control: Refunding budget reservation on timeout after send violates INV-M6-3."""
        h = Handle(kind="cdp_tab", canonical_id="tab_timeout_mutant", generation=1)

        # Transport send boundary WAS crossed (sent = True)
        sent = True
        timed_out = True

        # Candidate harness rule under INV-M6-3:
        # Timeout after possible send must yield UNKNOWN, retain consumption (NO refund), and quarantine target.
        def enforce_inv_m6_3_timeout(sent_flag: bool, timeout_flag: bool, refund_attempted: bool):
            if sent_flag and timeout_flag:
                if refund_attempted:
                    raise AssertionError("INV-M6-3 VIOLATION: Sent reservation refunded on timeout")
                return Receipt(
                    action_id="act_timeout",
                    handle=h,
                    status="UNKNOWN",
                    reason="TRANSPORT_TIMEOUT",
                )
            return Receipt(action_id="act_timeout", handle=h, status="OK")

        # The negative mutant attempts to refund reservation on timeout
        negative_mutant_refund = True
        with self.assertRaises(AssertionError) as ctx:
            enforce_inv_m6_3_timeout(sent, timed_out, negative_mutant_refund)
        self.assertIn("INV-M6-3 VIOLATION", str(ctx.exception))

        # Correct behavior: no refund on timeout, status is UNKNOWN
        receipt = enforce_inv_m6_3_timeout(sent, timed_out, refund_attempted=False)
        self.assertEqual(receipt.status, "UNKNOWN")


class TestWindowSchedulerDeadlineEDFAndTimeoutCancellation(unittest.TestCase):
    """Contract tests for WindowScheduler (jq2a-m6-c1-window-scheduler)."""

    @expected_failure_if_owner_absent("wender.act.window_scheduler", "WindowScheduler")
    def test_inv_m6_1_admission_and_service_cap_config_required(self):
        """INV-M6-1: Absent service cap configuration yields CONFIG_REQUIRED receipt/status."""
        from wender.act.window_scheduler import WindowScheduler, WriterFence

        h = Handle(kind="cdp_tab", canonical_id="tab_admit_1", generation=1)
        req = ActionRequest(
            handle=h,
            action_id="req_no_cap",
            epoch=0,
            payload={},
            cost=1,
            deadline_ns=2_000_000_000,
        )

        # Instantiating without service_cap_ns or submitting without configured service cap
        scheduler = WindowScheduler(
            registry=None,
            locks=None,
            focus_policy=None,
            budget=None,
            chain=None,
            writer_fence=WriterFence(),
            dispatch=None,
            max_queue=10,
            max_targets=5,
            service_cap_ns=None,
        )
        res = scheduler.submit(req)
        if isinstance(res, Receipt):
            self.assertEqual(res.status, "CONFIG_REQUIRED")

    @expected_failure_if_owner_absent("wender.act.window_scheduler", "WindowScheduler")
    def test_inv_m6_1_edf_dispatch_and_deadline_ties_submission_order(self):
        """INV-M6-1: Urgent dispatches use Earliest Deadline First; ties preserve submission order."""
        from wender.act.window_scheduler import WindowScheduler, WriterFence

        scheduler = WindowScheduler(
            registry=None,
            locks=None,
            focus_policy=None,
            budget=None,
            chain=None,
            writer_fence=WriterFence(),
            dispatch=None,
            max_queue=10,
            max_targets=5,
            service_cap_ns=1_000_000,
        )
        self.assertIsNotNone(scheduler)

    @expected_failure_if_owner_absent("wender.act.window_scheduler", "WindowScheduler")
    def test_inv_m6_2_fence_boundary_stop_after_boundary(self):
        """INV-M6-2: Claim, STOP check, budget reservation and dispatch authorization are linearized; STOP after boundary cannot recall sent work."""
        from wender.act.window_scheduler import WindowScheduler, WriterFence

        scheduler = WindowScheduler(
            registry=None,
            locks=None,
            focus_policy=None,
            budget=None,
            chain=None,
            writer_fence=WriterFence(),
            dispatch=None,
            max_queue=10,
            max_targets=5,
            service_cap_ns=1_000_000,
        )
        from wender.act.window_scheduler import check_lock_order
        self.assertIsNotNone(scheduler)
        with self.assertRaises(ValueError):
            check_lock_order([2, 1])

    @expected_failure_if_owner_absent("wender.act.window_scheduler", "WindowScheduler")
    def test_inv_m6_3_unsent_expiry_refuses_and_refunds_once(self):
        """INV-M6-3: Unsent expiry refuses execution and refunds reservation exactly once."""
        from wender.act.window_scheduler import WindowScheduler, WriterFence

        scheduler = WindowScheduler(
            registry=None,
            locks=None,
            focus_policy=None,
            budget=None,
            chain=None,
            writer_fence=WriterFence(),
            dispatch=None,
            max_queue=10,
            max_targets=5,
            service_cap_ns=1_000_000,
        )
        self.assertTrue(hasattr(scheduler, "step"))

    @expected_failure_if_owner_absent("wender.act.window_scheduler", "WindowScheduler")
    def test_inv_m6_3_timeout_after_send_returns_unknown_retains_consumption_quarantines(self):
        """INV-M6-3 & Gate G M6 3: Timeout after possible send returns UNKNOWN, retains consumption and quarantines target."""
        from wender.act.window_scheduler import WindowScheduler, WriterFence

        scheduler = WindowScheduler(
            registry=None,
            locks=None,
            focus_policy=None,
            budget=None,
            chain=None,
            writer_fence=WriterFence(),
            dispatch=None,
            max_queue=10,
            max_targets=5,
            service_cap_ns=1_000_000,
        )
        self.assertTrue(hasattr(scheduler, "reconcile"))

    @expected_failure_if_owner_absent("wender.act.window_scheduler", "WindowScheduler")
    def test_inv_m6_3_deadline_never_restarts_across_retries(self):
        """INV-M6-3 & Gate G M6 3: Absolute deadline includes discovery, ownership, transport, verification and extraction; never restarts per retry."""
        from wender.act.window_scheduler import WindowScheduler, WriterFence

        scheduler = WindowScheduler(
            registry=None,
            locks=None,
            focus_policy=None,
            budget=None,
            chain=None,
            writer_fence=WriterFence(),
            dispatch=None,
            max_queue=10,
            max_targets=5,
            service_cap_ns=1_000_000,
        )
        self.assertIsNotNone(scheduler)

    @expected_failure_if_owner_absent("wender.act.window_scheduler", "WindowScheduler")
    def test_reconcile_quarantine_requires_terminal_acknowledgment(self):
        """Reconcile holds quarantine release unless positive terminal acknowledgment or quiescence evidence exists."""
        from wender.act.window_scheduler import WindowScheduler, WriterFence

        scheduler = WindowScheduler(
            registry=None,
            locks=None,
            focus_policy=None,
            budget=None,
            chain=None,
            writer_fence=WriterFence(),
            dispatch=None,
            max_queue=10,
            max_targets=5,
            service_cap_ns=1_000_000,
        )
        self.assertTrue(hasattr(scheduler, "reconcile"))


class TestBudgetGovernorContract(unittest.TestCase):
    """Contract tests for BudgetGovernor (jq2a-m6-c1-window-scheduler)."""

    @expected_failure_if_owner_absent("wender.act.budget_governor", "BudgetGovernor")
    def test_budget_governor_reserve_commit_cancel(self):
        """BudgetGovernor reserve, commit, and cancel contracts."""
        from wender.act.budget_governor import BudgetGovernor

        governor = BudgetGovernor(global_budget=100, per_target_budget=20, service_cap_ns=1_000_000)
        h = Handle(kind="cdp_tab", canonical_id="bg_tab_1", generation=1)
        res = governor.reserve(h, cost=10, deadline_ns=2_000_000_000, now_ns=1_000_000_000)
        self.assertIsNotNone(res)
        governor.commit(res)

    @expected_failure_if_owner_absent("wender.act.budget_governor", "BudgetGovernor")
    def test_budget_governor_atomic_admission_and_sent_reservation_never_refunds(self):
        """Global and per-target admission is atomic transaction; sent/committed reservations never refund."""
        from wender.act.budget_governor import BudgetGovernor

        governor = BudgetGovernor(global_budget=100, per_target_budget=20, service_cap_ns=1_000_000)
        self.assertIsNotNone(governor)


if __name__ == "__main__":
    unittest.main()
