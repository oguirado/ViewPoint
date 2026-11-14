# @module {"id":"wa.tests.test_jq2a_m2_j4_keyboard_pointer_focus_routing","layer":1,"owner":"ACT","deps":[],"book":"HB-WA-s2-act"}
"""Contract tests for gate G M2 4: Keyboard/pointer require matching geometry and focus (jq2a-m2-j4).

Validates focus routing across distinct windows with equal node IDs, different DPR,
viewport geometry, and input domains. Confirms that sibling viewport substitution
refuses before send, matching focus lease and current epoch succeed, and verifies
INV-M2-1, INV-M2-2, and INV-M2-3 contracts.
"""

from dataclasses import dataclass
from enum import Enum, auto
import inspect
import unittest
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    import numpy as np
except ImportError:
    np = None


# --- Shared Canonical Dataclasses (or Fallbacks) ---

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
        claims: Any
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


class FocusClass(Enum):
    BACKGROUND_SAFE = auto()
    FOCUS_REQUIRED = auto()


class FocusError(ValueError):
    """Raised on focus policy refusal or routing mismatch."""
    pass


class ActionError(ValueError):
    """Raised on action execution failure or authorization refusal."""
    pass


# --- Owner Probing Decorator ---

def owner_landed() -> bool:
    """Probes if owning implementation task_id jq2a-m2-c2-focus-executor has landed."""
    try:
        from wender.act import focus_policy, target_lock_registry
        from wender.act.cdp_executor import run_ax_action
        sig = inspect.signature(run_ax_action)
        if "focus" in sig.parameters and "lease" in sig.parameters:
            return True
        return False
    except ImportError:
        return False


def require_owner(func):
    """Applies unittest.expectedFailure if owning implementation is absent."""
    if not owner_landed():
        return unittest.expectedFailure(func)
    return func


# --- Test Fixtures & Fake Components ---

class FakeClock:
    def __init__(self, now_ns: int = 1_000_000_000):
        self._now_ns = now_ns

    def __call__(self) -> int:
        return self._now_ns

    def now_ns(self) -> int:
        return self._now_ns

    def advance(self, ns: int):
        self._now_ns += ns


class FakeCdp:
    def __init__(self):
        self.calls = []

    def send(self, method: str, params: Optional[dict] = None, session_id: Optional[str] = None) -> dict:
        payload = dict(params or {})
        self.calls.append({"method": method, "params": payload, "session_id": session_id})
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj_1"}}
        if method == "Runtime.callFunctionOn":
            return {}
        if method == "DOM.getBoxModel":
            return {"model": {"border": [10, 10, 50, 10, 50, 30, 10, 30]}}
        return {}


# --- Local Reference Model for Focus & Lock Routing (Gate G M2 4 Harness) ---

SUPPORTED_TARGET_KINDS = {"cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"}


def canonical_resource_key(handle: Handle) -> Tuple[str, str]:
    if handle.kind not in SUPPORTED_TARGET_KINDS:
        raise ValueError(f"Unsupported target kind: {handle.kind}")
    # Capsule and child screen share the same canonical actuation resource when addressing same destination
    if handle.kind in ("capsule", "xvfb_screen"):
        return ("capsule_resource", handle.canonical_id)
    return (handle.kind, handle.canonical_id)


class ContractTargetLockRegistry:
    def __init__(self, registry=None, clock=None):
        self.registry = registry or {}
        self.clock = clock or FakeClock()
        self.locks: Dict[Tuple[str, str], ActorLease] = {}
        self.inflight: Dict[Tuple[str, str], ActorLease] = {}
        self.quarantine: Dict[Tuple[str, str], bool] = {}
        self._nonce_counter = 1

    def acquire(self, handle: Handle, owner: str, *, expires_ns: int, now_ns: Optional[int] = None) -> ActorLease:
        if type(handle.generation) is not int or handle.generation < 0:
            raise TypeError("generation must be a non-negative integer")
        if not owner or not isinstance(owner, str):
            raise TypeError("owner must be a nonempty string")
        curr_now = now_ns if now_ns is not None else self.clock()
        key = canonical_resource_key(handle)

        # INV-M2-1: single in-flight / target action
        if key in self.inflight:
            raise ActionError(f"Target {key} currently has in-flight action")

        existing = self.locks.get(key)
        if existing and existing.expires_ns > curr_now:
            if existing.owner != owner:
                raise ActionError(f"Target {key} locked by owner {existing.owner}")

        nonce = f"nonce_{self._nonce_counter}".encode("utf-8")
        self._nonce_counter += 1
        lease = ActorLease(
            handle=handle,
            owner=owner,
            generation=handle.generation,
            nonce=nonce,
            expires_ns=expires_ns,
        )
        self.locks[key] = lease
        return lease

    def mark_inflight(self, lease: ActorLease):
        key = canonical_resource_key(lease.handle)
        existing = self.locks.get(key)
        if not existing or existing.nonce != lease.nonce:
            raise ActionError("Forged or expired lease cannot mark in-flight")
        self.inflight[key] = lease

    def release(self, lease: ActorLease, *, now_ns: Optional[int] = None):
        key = canonical_resource_key(lease.handle)
        existing = self.locks.get(key)
        if not existing:
            raise ActionError("No lock found to release")
        if existing.nonce != lease.nonce:
            raise ActionError("Nonce mismatch on release (forged or consumed lease)")
        self.locks.pop(key, None)
        self.inflight.pop(key, None)

    def reconcile_quarantine(self, handle: Handle, *, acknowledged: bool):
        key = canonical_resource_key(handle)
        self.quarantine[key] = not acknowledged


class ContractFocusPolicy:
    def __init__(self, registry, locks, clock=None):
        self.registry = registry
        self.locks = locks
        self.clock = clock or FakeClock()
        self.current_focus: Dict[str, Tuple[Handle, int]] = {}  # domain -> (handle, epoch)
        self.current_modals: Dict[str, Optional[Handle]] = {}  # domain -> modal_handle

    def set_focus(self, handle: Handle, *, domain: str, epoch: int):
        self.current_focus[domain] = (handle, epoch)

    def set_modal(self, handle: Optional[Handle], *, domain: str):
        self.current_modals[domain] = handle

    def grant(self, lease: ActorLease, *, domain: str, now_ns: Optional[int] = None) -> FocusLease:
        curr_now = now_ns if now_ns is not None else self.clock()
        if lease.expires_ns <= curr_now:
            raise FocusError("Cannot grant focus lease for expired actor lease")

        key = canonical_resource_key(lease.handle)
        active_lock = self.locks.locks.get(key)
        if not active_lock or active_lock.nonce != lease.nonce:
            raise FocusError("Foreign or invalid actor lease")

        # Modal check
        modal_handle = self.current_modals.get(domain)
        if modal_handle is not None and modal_handle != lease.handle:
            raise FocusError("Modal active on domain blocking focus grant")

        focus_info = self.current_focus.get(domain)
        if not focus_info:
            raise FocusError(f"No focus established in domain {domain}")

        f_handle, f_epoch = focus_info
        if f_handle.canonical_id != lease.handle.canonical_id or f_handle.kind != lease.handle.kind:
            raise FocusError("Target does not hold current focus in domain (no focus theft)")

        return FocusLease(lease=lease, domain=domain, epoch=f_epoch)

    def classify(self, path: str, op: str) -> FocusClass:
        if path == "api":
            return FocusClass.BACKGROUND_SAFE
        if op in ("press", "insert_text", "hover", "lpath_click") or path in ("coordinates", "godot"):
            return FocusClass.FOCUS_REQUIRED
        return FocusClass.FOCUS_REQUIRED


class ContractFocusRouter:
    """Simulates multi-window target dispatch and verifies Gate G M2 4."""

    def __init__(self, registry: ContractTargetLockRegistry, focus_policy: ContractFocusPolicy):
        self.registry = registry
        self.focus_policy = focus_policy
        self.dispatched_events: List[dict] = []

    def dispatch(
        self,
        *,
        action: dict,
        target_handle: Handle,
        viewport_geometry: Tuple[int, int],
        dpr_ppm: int,
        input_domain: str,
        lease: ActorLease,
        focus: Optional[FocusLease],
        epoch: int,
    ) -> Receipt:
        # INV-M2-3: Bare context entry and bare-string path sets raise TypeError
        if isinstance(action.get("path"), str):
            raise TypeError("Bare string path sets raise TypeError")

        op = action.get("op", "click")
        path = action.get("path_choice", "ax")
        focus_class = self.focus_policy.classify(path, op)

        # INV-M2-2: Focus-required operations need current same-target focus in input domain
        if focus_class == FocusClass.FOCUS_REQUIRED:
            if focus is None:
                return Receipt(
                    action_id=action.get("action_id", "act_1"),
                    handle=target_handle,
                    status="refused",
                    reason="focus_required_missing_lease",
                )
            if focus.lease.handle != target_handle:
                # Sibling viewport substitution refusal
                return Receipt(
                    action_id=action.get("action_id", "act_1"),
                    handle=target_handle,
                    status="refused",
                    reason="sibling_viewport_substitution_refused",
                )
            if focus.domain != input_domain:
                return Receipt(
                    action_id=action.get("action_id", "act_1"),
                    handle=target_handle,
                    status="refused",
                    reason="input_domain_mismatch",
                )
            if focus.epoch != epoch:
                return Receipt(
                    action_id=action.get("action_id", "act_1"),
                    handle=target_handle,
                    status="refused",
                    reason="stale_epoch_refused",
                )

        # Gate G M2 4: Check geometry matching
        # Action target node's viewport must match the window geometry
        act_viewport = action.get("viewport")
        if act_viewport and act_viewport != viewport_geometry:
            return Receipt(
                action_id=action.get("action_id", "act_1"),
                handle=target_handle,
                status="refused",
                reason="mismatched_geometry_refused",
            )

        # Mark in-flight
        self.registry.mark_inflight(lease)
        self.dispatched_events.append({
            "action_id": action.get("action_id", "act_1"),
            "handle": target_handle,
            "dpr_ppm": dpr_ppm,
            "viewport": viewport_geometry,
            "op": op,
            "domain": input_domain,
        })
        self.registry.release(lease)

        return Receipt(
            action_id=action.get("action_id", "act_1"),
            handle=target_handle,
            status="ok",
            reason=None,
        )


# --- Test Suite ---

class TestKeyboardPointerFocusRoutingContract(unittest.TestCase):
    """Tests Gate G M2 4 and focus routing across equal node IDs in distinct viewports."""

    def setUp(self):
        self.clock = FakeClock(now_ns=1_000_000_000)
        self.lock_registry = ContractTargetLockRegistry(clock=self.clock)
        self.focus_policy = ContractFocusPolicy(registry={}, locks=self.lock_registry, clock=self.clock)
        self.router = ContractFocusRouter(self.lock_registry, self.focus_policy)

        # Window A setup (CDP Tab / Window 1)
        self.handle_win_a = Handle(kind="cdp_tab", canonical_id="win_a", generation=1)
        self.dpr_win_a = 100  # DPR 1.0 (in ppm)
        self.viewport_win_a = (1024, 768)
        self.domain_win_a = "input_domain_tab_a"
        self.epoch_win_a = 10

        # Window B setup (CDP Tab / Window 2) - Sibling Viewport
        self.handle_win_b = Handle(kind="cdp_tab", canonical_id="win_b", generation=1)
        self.dpr_win_b = 200  # DPR 2.0 (in ppm)
        self.viewport_win_b = (1920, 1080)
        self.domain_win_b = "input_domain_tab_b"
        self.epoch_win_b = 20

        # Equal Node IDs in both windows
        self.node_id = "btn_submit_id"

    # --- Gate G M2 4 & Focus Routing Contracts ---

    def test_positive_control_same_target_routed_input_succeeds(self):
        """Positive control: Keyboard/pointer action with matching focus lease and current epoch dispatches cleanly."""
        # Establish focus for Window A
        self.focus_policy.set_focus(self.handle_win_a, domain=self.domain_win_a, epoch=self.epoch_win_a)

        # Acquire lock and grant focus lease for Window A
        lease_a = self.lock_registry.acquire(self.handle_win_a, owner="actor_1", expires_ns=2_000_000_000)
        focus_a = self.focus_policy.grant(lease_a, domain=self.domain_win_a)

        action = {
            "action_id": "act_win_a_press",
            "op": "press",
            "target": self.node_id,
            "key": "Enter",
            "viewport": self.viewport_win_a,
        }

        receipt = self.router.dispatch(
            action=action,
            target_handle=self.handle_win_a,
            viewport_geometry=self.viewport_win_a,
            dpr_ppm=self.dpr_win_a,
            input_domain=self.domain_win_a,
            lease=lease_a,
            focus=focus_a,
            epoch=self.epoch_win_a,
        )

        self.assertEqual(receipt.status, "ok")
        self.assertIsNone(receipt.reason)
        self.assertEqual(len(self.router.dispatched_events), 1)
        self.assertEqual(self.router.dispatched_events[0]["handle"], self.handle_win_a)
        self.assertEqual(self.router.dispatched_events[0]["dpr_ppm"], 100)

    def test_negative_mutant_sibling_viewport_substitution_refuses_before_send(self):
        """Negative mutant: Attempting to route input using sibling viewport lease/focus refuses before send."""
        # Establish focus for Window A and Window B separately
        self.focus_policy.set_focus(self.handle_win_a, domain=self.domain_win_a, epoch=self.epoch_win_a)
        self.focus_policy.set_focus(self.handle_win_b, domain=self.domain_win_b, epoch=self.epoch_win_b)

        # Acquire lease and grant focus for Window A
        lease_a = self.lock_registry.acquire(self.handle_win_a, owner="actor_1", expires_ns=2_000_000_000)
        focus_a = self.focus_policy.grant(lease_a, domain=self.domain_win_a)

        # Attempt to use Window A's focus lease to dispatch action targeted at Window B (Sibling Viewport)
        action_on_win_b = {
            "action_id": "act_win_b_click",
            "op": "lpath_click",
            "target": self.node_id,  # Same node ID!
            "viewport": self.viewport_win_b,
        }

        receipt = self.router.dispatch(
            action=action_on_win_b,
            target_handle=self.handle_win_b,  # Target is Window B
            viewport_geometry=self.viewport_win_b,
            dpr_ppm=self.dpr_win_b,
            input_domain=self.domain_win_b,
            lease=lease_a,  # Sibling lease!
            focus=focus_a,  # Sibling focus!
            epoch=self.epoch_win_b,
        )

        self.assertEqual(receipt.status, "refused")
        self.assertEqual(receipt.reason, "sibling_viewport_substitution_refused")
        # Ensure ZERO dispatch events recorded
        self.assertEqual(len(self.router.dispatched_events), 0)

    def test_mismatched_geometry_refuses_before_send(self):
        """Gate G M2 4: Pointer action with geometry mismatch against target window viewport refuses before send."""
        self.focus_policy.set_focus(self.handle_win_a, domain=self.domain_win_a, epoch=self.epoch_win_a)
        lease_a = self.lock_registry.acquire(self.handle_win_a, owner="actor_1", expires_ns=2_000_000_000)
        focus_a = self.focus_policy.grant(lease_a, domain=self.domain_win_a)

        # Action supplies Window B's viewport geometry to Window A target
        action_bad_geo = {
            "action_id": "act_bad_geo",
            "op": "hover",
            "target": self.node_id,
            "viewport": self.viewport_win_b,  # Mismatched geometry!
        }

        receipt = self.router.dispatch(
            action=action_bad_geo,
            target_handle=self.handle_win_a,
            viewport_geometry=self.viewport_win_a,
            dpr_ppm=self.dpr_win_a,
            input_domain=self.domain_win_a,
            lease=lease_a,
            focus=focus_a,
            epoch=self.epoch_win_a,
        )

        self.assertEqual(receipt.status, "refused")
        self.assertEqual(receipt.reason, "mismatched_geometry_refused")
        self.assertEqual(len(self.router.dispatched_events), 0)

    def test_stale_epoch_refuses_before_send(self):
        """Gate G M2 4: Navigation or geometry change updating epoch renders previous focus lease stale."""
        self.focus_policy.set_focus(self.handle_win_a, domain=self.domain_win_a, epoch=self.epoch_win_a)
        lease_a = self.lock_registry.acquire(self.handle_win_a, owner="actor_1", expires_ns=2_000_000_000)
        focus_a = self.focus_policy.grant(lease_a, domain=self.domain_win_a)

        # Target epoch advances due to navigation/geometry change
        new_epoch = self.epoch_win_a + 1

        action = {
            "action_id": "act_stale_epoch",
            "op": "insert_text",
            "target": self.node_id,
            "text": "hello",
            "viewport": self.viewport_win_a,
        }

        receipt = self.router.dispatch(
            action=action,
            target_handle=self.handle_win_a,
            viewport_geometry=self.viewport_win_a,
            dpr_ppm=self.dpr_win_a,
            input_domain=self.domain_win_a,
            lease=lease_a,
            focus=focus_a,
            epoch=new_epoch,  # New epoch!
        )

        self.assertEqual(receipt.status, "refused")
        self.assertEqual(receipt.reason, "stale_epoch_refused")
        self.assertEqual(len(self.router.dispatched_events), 0)

    def test_capsule_and_child_screen_share_canonical_actuation_resource(self):
        """INV-M2-3: Capsule and its child screen use the same canonical actuation resource."""
        capsule_handle = Handle(kind="capsule", canonical_id="dest_screen_1", generation=1)
        screen_handle = Handle(kind="xvfb_screen", canonical_id="dest_screen_1", generation=1)

        lease_cap = self.lock_registry.acquire(capsule_handle, owner="actor_1", expires_ns=2_000_000_000)
        self.lock_registry.mark_inflight(lease_cap)

        # Attempting to acquire lock for child screen addressing same destination raises ActionError
        with self.assertRaises(ActionError):
            self.lock_registry.acquire(screen_handle, owner="actor_2", expires_ns=2_000_000_000)

    def test_expired_actor_lease_refuses_focus_grant(self):
        """INV-M2-1/M2-2: Expired actor lease refuses focus grant without focus theft."""
        self.focus_policy.set_focus(self.handle_win_a, domain=self.domain_win_a, epoch=self.epoch_win_a)
        expired_lease = ActorLease(
            handle=self.handle_win_a,
            owner="actor_1",
            generation=1,
            nonce=b"nonce_exp",
            expires_ns=500_000_000,  # Expired relative to clock now_ns=1_000_000_000
        )

        with self.assertRaises(FocusError):
            self.focus_policy.grant(expired_lease, domain=self.domain_win_a)

    def test_foreign_domain_focus_routing_refuses(self):
        """INV-M2-2: Action dispatched to domain other than focus lease domain refuses before send."""
        self.focus_policy.set_focus(self.handle_win_a, domain=self.domain_win_a, epoch=self.epoch_win_a)
        lease_a = self.lock_registry.acquire(self.handle_win_a, owner="actor_1", expires_ns=2_000_000_000)
        focus_a = self.focus_policy.grant(lease_a, domain=self.domain_win_a)

        action = {
            "action_id": "act_foreign_domain",
            "op": "press",
            "target": self.node_id,
            "key": "Enter",
            "viewport": self.viewport_win_a,
        }

        # Dispatch with mismatched domain
        receipt = self.router.dispatch(
            action=action,
            target_handle=self.handle_win_a,
            viewport_geometry=self.viewport_win_a,
            dpr_ppm=self.dpr_win_a,
            input_domain="different_domain",  # Foreign domain!
            lease=lease_a,
            focus=focus_a,
            epoch=self.epoch_win_a,
        )

        self.assertEqual(receipt.status, "refused")
        self.assertEqual(receipt.reason, "input_domain_mismatch")

    # --- INV-M2-1 & Lock Invariants ---

    def test_inv_m2_1_single_target_inflight_action(self):
        """INV-M2-1: A canonical target can have at most one TARGET in-flight action across interfaces."""
        lease_a = self.lock_registry.acquire(self.handle_win_a, owner="actor_1", expires_ns=2_000_000_000)
        self.lock_registry.mark_inflight(lease_a)

        # Attempting to acquire a second lock on same target while action is in-flight raises ActionError
        with self.assertRaises(ActionError):
            self.lock_registry.acquire(self.handle_win_a, owner="actor_2", expires_ns=2_000_000_000)

    def test_inv_m2_1_nonce_fenced_release(self):
        """INV-M2-1: Forged or stale nonce lease release attempts fail without releasing active lock."""
        lease_a = self.lock_registry.acquire(self.handle_win_a, owner="actor_1", expires_ns=2_000_000_000)
        forged_lease = ActorLease(
            handle=self.handle_win_a,
            owner="actor_1",
            generation=1,
            nonce=b"forged_nonce",
            expires_ns=2_000_000_000,
        )

        with self.assertRaises(ActionError):
            self.lock_registry.release(forged_lease)

        # Original lock remains active
        self.assertIn(("cdp_tab", "win_a"), self.lock_registry.locks)

    # --- INV-M2-2 & INV-M2-3 Focus & Safety Rules ---

    def test_inv_m2_2_focus_required_operations_default_to_refusal_without_focus(self):
        """INV-M2-2: Coordinates, keyboard insertion and unknown operations refuse without focus evidence."""
        action = {
            "action_id": "act_coord",
            "op": "lpath_click",
            "path_choice": "coordinates",
            "target": self.node_id,
        }

        # Dispatch with focus=None
        receipt = self.router.dispatch(
            action=action,
            target_handle=self.handle_win_a,
            viewport_geometry=self.viewport_win_a,
            dpr_ppm=self.dpr_win_a,
            input_domain=self.domain_win_a,
            lease=self.lock_registry.acquire(self.handle_win_a, owner="actor_1", expires_ns=2_000_000_000),
            focus=None,
            epoch=self.epoch_win_a,
        )

        self.assertEqual(receipt.status, "refused")
        self.assertEqual(receipt.reason, "focus_required_missing_lease")

    def test_inv_m2_2_background_safe_api_operations_do_not_require_focus(self):
        """INV-M2-2: API path operations are background-safe and do not require focus lease."""
        action = {
            "action_id": "act_api",
            "op": "read_state",
            "path_choice": "api",
            "target": self.node_id,
        }

        lease_a = self.lock_registry.acquire(self.handle_win_a, owner="actor_1", expires_ns=2_000_000_000)

        receipt = self.router.dispatch(
            action=action,
            target_handle=self.handle_win_a,
            viewport_geometry=self.viewport_win_a,
            dpr_ppm=self.dpr_win_a,
            input_domain=self.domain_win_a,
            lease=lease_a,
            focus=None,  # API ops do not require focus lease
            epoch=self.epoch_win_a,
        )

        self.assertEqual(receipt.status, "ok")

    def test_inv_m2_2_modal_blocked_focus_grant_refuses(self):
        """INV-M2-2: Active modal on domain blocks focus grant to non-modal target without focus theft."""
        self.focus_policy.set_focus(self.handle_win_a, domain=self.domain_win_a, epoch=self.epoch_win_a)

        # Set modal to Window B on Window A's domain
        self.focus_policy.set_modal(self.handle_win_b, domain=self.domain_win_a)

        lease_a = self.lock_registry.acquire(self.handle_win_a, owner="actor_1", expires_ns=2_000_000_000)

        with self.assertRaises(FocusError) as ctx:
            self.focus_policy.grant(lease_a, domain=self.domain_win_a)
        self.assertIn("Modal active", str(ctx.exception))

    def test_inv_m2_3_bare_string_path_set_raises_typeerror(self):
        """INV-M2-3: Bare string path set raises TypeError."""
        lease_a = self.lock_registry.acquire(self.handle_win_a, owner="actor_1", expires_ns=2_000_000_000)
        action = {
            "action_id": "act_bare",
            "path": "api",  # Bare string path
        }

        with self.assertRaises(TypeError):
            self.router.dispatch(
                action=action,
                target_handle=self.handle_win_a,
                viewport_geometry=self.viewport_win_a,
                dpr_ppm=self.dpr_win_a,
                input_domain=self.domain_win_a,
                lease=lease_a,
                focus=None,
                epoch=self.epoch_win_a,
            )

    # --- Owner Module Probing Tests (@require_owner) ---

    @require_owner
    def test_owner_focus_policy_module_exports(self):
        """Probes jq2a-m2-c2-focus-executor FocusPolicy class exports and constructor."""
        from wender.act.focus_policy import FocusPolicy
        from wender.act.target_lock_registry import TargetLockRegistry

        from wender.perceive.session_registry import SessionRegistry
        sessions = SessionRegistry()
        handle = sessions.register("cdp_tab", "tab1", object(), owned=False)
        registry = TargetLockRegistry(sessions, clock=self.clock)
        policy = FocusPolicy(sessions, registry)
        self.assertIsNotNone(policy)

    @require_owner
    def test_owner_target_lock_registry_acquire_and_release(self):
        """Probes jq2a-m2-c2-focus-executor TargetLockRegistry acquire, release, mark_inflight, reconcile_quarantine."""
        from wender.act.target_lock_registry import TargetLockRegistry
        from wender.perceive.session_registry import Handle, ActorLease

        from wender.perceive.session_registry import SessionRegistry
        sessions = SessionRegistry()
        handle = sessions.register("cdp_tab", "tab1", object(), owned=False)
        registry = TargetLockRegistry(sessions, clock=self.clock)
        lease = registry.acquire(handle, owner="builder_1", expires_ns=self.clock() + 1_000_000_000)
        self.assertIsInstance(lease, ActorLease)
        registry.mark_inflight(lease)
        with self.assertRaises(ValueError):
            registry.release(lease)
        registry.reconcile_quarantine(handle, acknowledged=True, expected_lease=lease)

    @require_owner
    def test_owner_run_ax_action_extended_keywords(self):
        """Probes jq2a-m2-c2-focus-executor run_ax_action signature extension for lease and focus kwargs."""
        from wender.act.cdp_executor import run_ax_action
        sig = inspect.signature(run_ax_action)
        self.assertIn("lease", sig.parameters)
        self.assertIn("focus", sig.parameters)
        self.assertIn("observation", sig.parameters)
        self.assertIn("registry", sig.parameters)
        self.assertIn("focus_policy", sig.parameters)


if __name__ == "__main__":
    unittest.main()
