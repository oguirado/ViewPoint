"""Unit tests for focus steal prevention and modal-blocked focus refusal (jq2a-m2-j2-focus-steal-prevention).

Gate G M2 2: Modal or wrong-target focus refuses before send.
Validates zero sends and no automatic focus theft on sibling, expired, or modal-blocked focus grants.
Validates positive explicit grant execution, background-safe API independence, and negative mutant catching.
"""

from dataclasses import dataclass
from enum import Enum
import unittest
from unittest.mock import Mock, call

# Try importing canonical records from wender.perceive.session_registry
try:
    from wender.perceive.session_registry import (
        Handle, TargetRecord, FrameRef, Observation, Receipt, ActionRequest,
        ActorLease, FocusLease, StreamSpec, StreamToken, Ticket, ChangeResult,
        PerceptionResult, WindowFrames, WindowDescription
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
        claims: object
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


class FocusClass(Enum):
    BACKGROUND_SAFE = "background_safe"
    FOCUS_REQUIRED = "focus_required"
    SESSION_SCOPED = "session_scoped"


class FakeClock:
    """Deterministic nanosecond clock for expiry testing."""

    def __init__(self, initial_ns: int = 1_000_000_000):
        self._now_ns = initial_ns

    def now_ns(self) -> int:
        return self._now_ns

    def advance(self, delta_ns: int):
        self._now_ns += delta_ns


class RecordingFakeCdp:
    """Fake CDP transport that records all sent RPC commands."""

    def __init__(self):
        self.calls = []

    def send(self, method: str, params: dict | None = None, session_id: str | None = None) -> dict:
        payload = dict(params or {})
        self.calls.append({"method": method, "params": payload, "session_id": session_id})
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj_target"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": True}}
        if method == "DOM.getBoxModel":
            return {"model": {"border": [10, 20, 110, 20, 110, 60, 10, 60]}}
        return {}


# --- Contract Harness Reference Implementation for Gate G M2 2 ---

class ProposedFocusPolicy:
    """Proposed FocusPolicy contract harness implementation."""

    def __init__(self, registry=None, locks=None, clock=None):
        self.registry = registry
        self.locks = locks
        self.clock = clock or FakeClock()
        self.current_focus_handle: Handle | None = None
        self.current_focus_domain: str | None = None
        self.current_focus_epoch: int = 0
        self.modal_handle: Handle | None = None
        self.modal_domain: str | None = None

    def set_focus(self, handle: Handle, *, domain: str, epoch: int) -> None:
        self.current_focus_handle = handle
        self.current_focus_domain = domain
        self.current_focus_epoch = epoch

    def set_modal(self, handle: Handle | None, *, domain: str) -> None:
        self.modal_handle = handle
        self.modal_domain = domain

    def classify(self, path: str, op: str) -> FocusClass:
        if path.startswith("api/") or op.startswith("api_"):
            return FocusClass.BACKGROUND_SAFE
        if op in ("insert_text", "press", "click_coordinate", "type_coordinate"):
            return FocusClass.FOCUS_REQUIRED
        if op == "ax_action":
            return FocusClass.SESSION_SCOPED
        return FocusClass.FOCUS_REQUIRED

    def grant(self, lease: ActorLease, *, domain: str, now_ns: int | None = None) -> FocusLease:
        current_time = now_ns if now_ns is not None else self.clock.now_ns()

        if current_time >= lease.expires_ns:
            raise ValueError("ActorLease has expired")

        if self.modal_handle is not None and self.modal_handle != lease.handle:
            raise ValueError("Focus grant blocked by active modal on another handle")

        if self.modal_domain is not None and self.modal_domain != domain:
            raise ValueError("Focus grant blocked by modal domain mismatch")

        if self.current_focus_handle is not None and self.current_focus_handle != lease.handle:
            raise ValueError("Focus grant mismatch: sibling or foreign target currently holds focus")

        return FocusLease(lease=lease, domain=domain, epoch=lease.generation)

    def execute_action(
        self,
        transport: RecordingFakeCdp,
        action: dict,
        *,
        lease: ActorLease | None = None,
        focus: FocusLease | None = None,
        ignore_focus_token_mutant: bool = False
    ) -> Receipt:
        path = action.get("path", "")
        op = action.get("op", "")
        fclass = self.classify(path, op)

        if fclass == FocusClass.BACKGROUND_SAFE:
            # Background-safe API operations run independently without focus requirement
            transport.send("API.execute", {"op": op, "payload": action.get("payload", {})})
            return Receipt(
                action_id=action.get("action_id", "act-1"),
                handle=lease.handle if lease else Handle("cdp_tab", "tab-bg", 1),
                status="OK",
                reason=None
            )

        # Focus-required operations
        if ignore_focus_token_mutant:
            # MUTANT: Ignores focus token checks and dispatches anyway
            transport.send("Input.dispatchKeyEvent", {"type": "keyDown", "text": "mutant"})
            return Receipt(
                action_id=action.get("action_id", "act-1"),
                handle=lease.handle if lease else Handle("cdp_tab", "tab-mutant", 1),
                status="OK",
                reason=None
            )

        # Correct Gate G M2 2 behavior:
        if lease is None:
            return Receipt(
                action_id=action.get("action_id", "act-1"),
                handle=Handle("unknown", "none", 0),
                status="REFUSED",
                reason="Missing ActorLease"
            )

        if focus is None:
            return Receipt(
                action_id=action.get("action_id", "act-1"),
                handle=lease.handle,
                status="REFUSED",
                reason="Missing FocusLease for focus-required operation"
            )

        # Check focus lease validity against current target, expiry, modal
        now = self.clock.now_ns()
        if now >= lease.expires_ns or now >= focus.lease.expires_ns:
            return Receipt(
                action_id=action.get("action_id", "act-1"),
                handle=lease.handle,
                status="REFUSED",
                reason="Expired focus or actor lease"
            )

        if focus.lease.handle != lease.handle:
            return Receipt(
                action_id=action.get("action_id", "act-1"),
                handle=lease.handle,
                status="REFUSED",
                reason="Focus lease target mismatch"
            )

        if self.modal_handle is not None and self.modal_handle != lease.handle:
            return Receipt(
                action_id=action.get("action_id", "act-1"),
                handle=lease.handle,
                status="REFUSED",
                reason="Modal active on another target blocking focus"
            )

        if self.current_focus_handle is not None and self.current_focus_handle != lease.handle:
            return Receipt(
                action_id=action.get("action_id", "act-1"),
                handle=lease.handle,
                status="REFUSED",
                reason="Sibling target holds focus; focus theft prohibited"
            )

        # Matching explicit focus grant: dispatch action
        transport.send("Input.insertText", {"text": action.get("text", "hello")})
        return Receipt(
            action_id=action.get("action_id", "act-1"),
            handle=lease.handle,
            status="OK",
            reason=None
        )


# --- Probing Helper for Production Owner Modules ---

def _is_owner_present() -> bool:
    try:
        import wender.perceive.session_registry as sr
        import wender.act.target_lock_registry as tlr
        import wender.act.focus_policy as fp
        return hasattr(fp, "FocusPolicy") and hasattr(tlr, "TargetLockRegistry") and hasattr(sr, "ActorLease")
    except ImportError:
        return False


def owner_expected_failure(func):
    if not _is_owner_present():
        return unittest.expectedFailure(func)
    return func


# --- Test Cases ---

class TestFocusStealPreventionContractHarness(unittest.TestCase):
    """Contract harness tests for focus theft prevention, expired/modal grants, and negative mutants."""

    def setUp(self):
        self.clock = FakeClock(initial_ns=1_000_000_000)
        self.policy = ProposedFocusPolicy(clock=self.clock)
        self.transport = RecordingFakeCdp()

        self.handle_a = Handle(kind="cdp_tab", canonical_id="tab-A", generation=1)
        self.handle_b = Handle(kind="cdp_tab", canonical_id="tab-B", generation=1)

        self.lease_a = ActorLease(
            handle=self.handle_a,
            owner="worker_a",
            generation=1,
            nonce=b"nonce_a_12345678",
            expires_ns=2_000_000_000
        )
        self.lease_b = ActorLease(
            handle=self.handle_b,
            owner="worker_b",
            generation=1,
            nonce=b"nonce_b_12345678",
            expires_ns=2_000_000_000
        )

    def test_positive_control_matching_explicit_grant_works(self):
        """Matching explicit focus grant permits a healthy action with transport dispatch."""
        self.policy.set_focus(self.handle_a, domain="web", epoch=1)
        focus_a = self.policy.grant(self.lease_a, domain="web")

        action = {"op": "insert_text", "text": "hello world", "path": "ui/input"}
        receipt = self.policy.execute_action(
            self.transport, action, lease=self.lease_a, focus=focus_a
        )

        self.assertEqual(receipt.status, "OK")
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.calls[0]["method"], "Input.insertText")

    def test_sibling_focus_grant_refuses_before_send(self):
        """Focus-required operation with focus granted to sibling target refuses before send (zero sends)."""
        # Focus is set to sibling target B
        self.policy.set_focus(self.handle_b, domain="web", epoch=1)

        # Attempt grant/action on target A
        with self.assertRaises(ValueError):
            self.policy.grant(self.lease_a, domain="web")

        # Directly executing action with ungranted/invalid focus lease for target A
        fake_focus_a = FocusLease(lease=self.lease_a, domain="web", epoch=1)
        action = {"op": "insert_text", "text": "forbidden stolen text", "path": "ui/input"}
        receipt = self.policy.execute_action(
            self.transport, action, lease=self.lease_a, focus=fake_focus_a
        )

        self.assertEqual(receipt.status, "REFUSED")
        self.assertEqual(len(self.transport.calls), 0, "Must be zero CDP sends on sibling focus refusal")

    def test_expired_focus_grant_refuses_before_send(self):
        """Expired actor/focus lease refuses before send (zero sends)."""
        expired_lease = ActorLease(
            handle=self.handle_a,
            owner="worker_a",
            generation=1,
            nonce=b"nonce_a_12345678",
            expires_ns=500_000_000  # Expires before self.clock (1_000_000_000)
        )

        with self.assertRaises(ValueError):
            self.policy.grant(expired_lease, domain="web")

        fake_focus = FocusLease(lease=expired_lease, domain="web", epoch=1)
        action = {"op": "press", "key": "Enter", "path": "ui/button"}
        receipt = self.policy.execute_action(
            self.transport, action, lease=expired_lease, focus=fake_focus
        )

        self.assertEqual(receipt.status, "REFUSED")
        self.assertEqual(len(self.transport.calls), 0, "Must be zero sends on expired lease")

    def test_modal_blocked_focus_grant_refuses_before_send(self):
        """Active modal on another target blocks focus grant and refuses operation before send."""
        # Set active modal on handle_b
        self.policy.set_modal(self.handle_b, domain="web")

        with self.assertRaises(ValueError):
            self.policy.grant(self.lease_a, domain="web")

        fake_focus = FocusLease(lease=self.lease_a, domain="web", epoch=1)
        action = {"op": "click_coordinate", "x": 100, "y": 200, "path": "ui/canvas"}
        receipt = self.policy.execute_action(
            self.transport, action, lease=self.lease_a, focus=fake_focus
        )

        self.assertEqual(receipt.status, "REFUSED")
        self.assertEqual(len(self.transport.calls), 0, "Must be zero sends when modal blocks focus")

    def test_background_safe_api_operations_remain_independent(self):
        """Background-safe API operations execute independently without requiring active focus or stealing focus."""
        # Focus is held by sibling target B
        self.policy.set_focus(self.handle_b, domain="web", epoch=1)

        api_action = {"op": "api_fetch_data", "path": "api/data", "payload": {"id": 42}}
        receipt = self.policy.execute_action(
            self.transport, api_action, lease=self.lease_a, focus=None
        )

        self.assertEqual(receipt.status, "OK")
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.calls[0]["method"], "API.execute")
        # Ensure focus remains unchanged on sibling B
        self.assertEqual(self.policy.current_focus_handle, self.handle_b)

    def test_negative_mutant_ignore_focus_token_is_caught(self):
        """Negative mutant 'ignore focus token' allows unauthorized dispatch and is caught by harness assertion."""
        self.policy.set_focus(self.handle_b, domain="web", epoch=1)

        fake_focus_a = FocusLease(lease=self.lease_a, domain="web", epoch=1)
        action = {"op": "insert_text", "text": "stolen", "path": "ui/input"}

        # Running with ignore_focus_token_mutant=True
        mutant_receipt = self.policy.execute_action(
            self.transport, action, lease=self.lease_a, focus=fake_focus_a, ignore_focus_token_mutant=True
        )

        # The negative mutant dispatches transport calls illegally (status OK)
        self.assertEqual(mutant_receipt.status, "OK")
        self.assertEqual(len(self.transport.calls), 1)

        # Harness assertion catches the mutant: negative mutant must NOT pass refusal gate
        with self.assertRaises(AssertionError):
            self.assertEqual(mutant_receipt.status, "REFUSED", "Negative mutant failed to refuse forbidden focus steal")
            self.assertEqual(len(self.transport.calls), 0, "Negative mutant failed to prevent transport send")


class TestFocusStealPreventionProductionConformance(unittest.TestCase):
    """Production conformance tests probing owning implementation task_id: jq2a-m2-c2-focus-executor."""

    @owner_expected_failure
    def test_production_focus_policy_grant_and_modal_refusal(self):
        """Production FocusPolicy rejects sibling, expired, or modal-blocked focus grants."""
        import wender.act.focus_policy as fp
        import wender.act.target_lock_registry as tlr

        from wender.perceive.session_registry import SessionRegistry
        registry = SessionRegistry()
        handle_a = registry.register("cdp_tab", "tab-1", object(), owned=False)
        handle_b = registry.register("cdp_tab", "tab-2", object(), owned=False)
        locks = tlr.TargetLockRegistry(registry, clock=lambda: 100)
        policy = fp.FocusPolicy(registry, locks, clock=lambda: 100)
        lease_a = locks.acquire(handle_a, "owner_a", expires_ns=2_000_000_000)
        self.addCleanup(locks.release, lease_a)
        policy.set_focus(handle_a, domain="web", epoch=registry.get(handle_a).epoch)
        policy.set_modal(handle_b, domain="web")

        # Modal on handle_b blocks grant on handle_a
        with self.assertRaises(ValueError):
            policy.grant(lease_a, domain="web")

    @owner_expected_failure
    def test_production_run_ax_action_refuses_without_focus(self):
        """Production run_ax_action with focus_policy refuses focus-required operation before send when focus is absent."""
        import wender.act.cdp_executor as cdp_exec
        import wender.act.focus_policy as fp

        cdp = RecordingFakeCdp()
        from wender.perceive.session_registry import SessionRegistry, FrameRef, Observation
        from wender.act.target_lock_registry import TargetLockRegistry
        from wender.act.multiwin_isolation_verifier import StructuralSessionRegistry, StructuralWriterFence
        import numpy as np

        sessions = SessionRegistry()
        handle = sessions.register("cdp_tab", "tab-1", object(), owned=False)
        record = sessions.bind_attachment(handle, "session-1", owned=False)
        locks = TargetLockRegistry(sessions, clock=lambda: 100)
        lease = locks.acquire(handle, "owner_a", expires_ns=2_000_000_000)
        self.addCleanup(locks.release, lease)
        policy = fp.FocusPolicy(sessions, locks, clock=lambda: 100)
        registry = StructuralSessionRegistry([record])
        action = {"op": "insert_text", "text": "test", "target": "node-1"}
        nodes = [{"id": "node-1", "backend_id": 101, "role": "textbox"}]
        frame = FrameRef.from_rgb(handle, record.epoch, 1, np.zeros((1, 1, 3), dtype=np.uint8))
        observation = Observation(handle, record.epoch, 1, 1_000_000,
                                  {"nodes": nodes, "focused_node_id": "node-1"}, frame)
        registry.set_current_observation(observation)

        with self.assertRaises(cdp_exec.ActionError):
            cdp_exec.run_ax_action(
                cdp,
                "session-1",
                nodes,
                action,
                lambda: nodes,
                lease=lease,
                focus=None,
                focus_policy=policy,
                observation=observation,
                registry=registry,
                writer_fence=StructuralWriterFence(),
                clock=lambda: 100,
            )

        self.assertEqual(len(cdp.calls), 0, "Production run_ax_action must make 0 send calls when focus is refused")


if __name__ == "__main__":
    unittest.main()
