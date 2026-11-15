"""Contract tests for CDP executor lock and fresh gate (jq2a-m2-j3-cdp-executor-lock-and-fresh-gate).

Feeds missing, tied and stale-epoch fresh observations into a lease-bearing executor.
Requires refusal before mutating scroll or send, no swallowed StopIteration and no
irreversible coordinate retry; a unique current observation permits exactly one dispatch.
Gate G M2 3: Unreadable fresh gate refuses before send.
The negative mutant is swallow StopIteration; positive control is fresh unique backend dispatches.
Owning implementation task_id: jq2a-m2-c2-focus-executor.
"""

import dataclasses
import time
import unittest
from typing import Dict, List, Optional, Tuple, Any

import numpy as np

from wender.perceive import ax


# Define fallback canonical frozen dataclasses for proposed records if unmerged.
# Strict field order as specified in contract.
try:
    from wender.perceive.session_registry import (
        Handle,
        ActorLease,
        FocusLease,
        Observation,
        FrameRef,
        Receipt,
        ActionRequest,
        StreamSpec,
        StreamToken,
        Ticket,
        ChangeResult,
        PerceptionResult,
    )
except ImportError:
    @dataclasses.dataclass(frozen=True)
    class Handle:
        kind: str
        canonical_id: str
        generation: int

    @dataclasses.dataclass(frozen=True)
    class FrameRef:
        handle: Handle
        epoch: int
        frame_seq: int
        width: int
        height: int
        rgb: Any
        digest: str

    @dataclasses.dataclass(frozen=True)
    class Observation:
        handle: Handle
        epoch: int
        frame_seq: int
        dpr_ppm: int
        state: Dict[str, Any]
        frame: FrameRef

    @dataclasses.dataclass(frozen=True)
    class Receipt:
        action_id: str
        handle: Handle
        status: str
        reason: Optional[str]

    @dataclasses.dataclass(frozen=True)
    class ActionRequest:
        handle: Handle
        action_id: str
        epoch: int
        payload: Dict[str, Any]
        cost: int
        deadline_ns: int

    @dataclasses.dataclass(frozen=True)
    class ActorLease:
        handle: Handle
        owner: str
        generation: int
        nonce: bytes
        expires_ns: int

    @dataclasses.dataclass(frozen=True)
    class FocusLease:
        lease: ActorLease
        domain: str
        epoch: int

    @dataclasses.dataclass(frozen=True)
    class StreamSpec:
        width: int
        height: int
        slots: int
        quota_bytes: int

    @dataclasses.dataclass(frozen=True)
    class StreamToken:
        handle: Handle
        epoch: int

    @dataclasses.dataclass(frozen=True)
    class Ticket:
        action_id: str
        handle: Handle

    @dataclasses.dataclass(frozen=True)
    class ChangeResult:
        handle: Handle
        epoch: int
        settled: bool
        claims: np.ndarray
        ops: Dict[str, Any]

    @dataclasses.dataclass(frozen=True)
    class PerceptionResult:
        handle: Handle
        observation: Optional[Observation]
        error: Optional[str]

try:
    from wender.perceive.session_registry import WindowFrames, WindowDescription
except ImportError:
    @dataclasses.dataclass(frozen=True)
    class WindowFrames:
        handle: Handle
        epoch: int
        frames: Tuple[Any, ...]
        frame_seqs: Tuple[int, ...]
        dpr_ppm: int

    @dataclasses.dataclass(frozen=True)
    class WindowDescription:
        handle: Handle
        epoch: int
        frame_seqs: Tuple[int, ...]
        descriptors: Tuple[Any, ...]
        regions: Tuple[Any, ...]
        error: Optional[str]


# Target owner modules probing
def _probe_owner_symbol(module_path: str, symbol_name: Optional[str] = None) -> bool:
    import importlib
    try:
        mod = importlib.import_module(module_path)
        if symbol_name is not None:
            return hasattr(mod, symbol_name)
        return True
    except ImportError:
        return False


HAS_TARGET_LOCK_REGISTRY = _probe_owner_symbol("wender.act.target_lock_registry", "TargetLockRegistry")
HAS_FOCUS_POLICY = _probe_owner_symbol("wender.act.focus_policy", "FocusPolicy")
HAS_SESSION_REGISTRY = _probe_owner_symbol("wender.perceive.session_registry", "Handle")


def _expected_failure_if(condition: bool):
    """Dynamically applies @unittest.expectedFailure only while condition holds true (e.g. absent symbol)."""
    if condition:
        return unittest.expectedFailure
    return lambda func: func


class FakeCdp:
    """Fake CDP transport for offline testing."""

    def __init__(self):
        self.calls = []

    def send(self, method, params=None, session_id=None):
        payload = dict(params or {})
        self.calls.append({"method": method, "params": payload, "session_id": session_id})
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj-101"}}
        if method == "Runtime.callFunctionOn":
            return {}
        if method == "DOM.getBoxModel":
            return {"model": {"border": [10, 10, 50, 10, 50, 30, 10, 30]}}
        return {}


def _make_dummy_observation(handle: Handle, epoch: int, frame_seq: int, nodes: List[dict]) -> Observation:
    rgb = np.zeros((10, 10, 3), dtype=np.uint8)
    frame = FrameRef(
        handle=handle,
        epoch=epoch,
        frame_seq=frame_seq,
        width=10,
        height=10,
        rgb=rgb,
        digest="digest123",
    )
    return Observation(
        handle=handle,
        epoch=epoch,
        frame_seq=frame_seq,
        dpr_ppm=100,
        state={"nodes": nodes},
        frame=frame,
    )


def _make_target_node(name="Submit", backend_id=101, ordinal=0, ancestors=("main",), attrs=()):
    n = {
        "id": "t1",
        "backend_id": backend_id,
        "role": "button",
        "name": name,
        "depth": 1,
        "ancestors": ancestors,
        "attrs": attrs,
        "ordinal": ordinal,
        "cardinality": 1,
    }
    n["id"] = ax.stable_id(n)
    return n


class TestCdpExecutorLockAndFreshGate(unittest.TestCase):
    """Tests for CDP executor lock and fresh gate (Gate G M2 3)."""

    def setUp(self):
        from wender.perceive.session_registry import SessionRegistry
        from wender.act.target_lock_registry import TargetLockRegistry
        from wender.act.focus_policy import FocusPolicy
        from wender.act.multiwin_isolation_verifier import StructuralSessionRegistry, StructuralWriterFence

        self.sessions = SessionRegistry()
        self.handle = self.sessions.register("cdp_tab", "tab-123", object(), owned=False)
        record = self.sessions.bind_attachment(self.handle, "sess1", owned=False)
        self.locks = TargetLockRegistry(self.sessions)
        self.lease = self.locks.acquire(self.handle, "executor-1", expires_ns=time.monotonic_ns() + 10**9)
        self.addCleanup(self.locks.release, self.lease)
        self.policy = FocusPolicy(self.sessions, self.locks)
        self.policy.set_focus(self.handle, domain="cdp:sess1", epoch=record.epoch)
        self.focus = self.policy.grant(self.lease, domain="cdp:sess1")
        self.authority = StructuralSessionRegistry([record])
        self.fence = StructuralWriterFence()

    def authority_kwargs(self, target):
        record = self.sessions.get(self.handle)
        frame = FrameRef.from_rgb(self.handle, record.epoch, 1, np.zeros((10, 10, 3), dtype=np.uint8))
        observation = Observation(self.handle, record.epoch, 1, 1_000_000,
                                  {"nodes": [target]}, frame)
        self.authority.set_current_observation(observation)
        return dict(observation=observation, registry=self.authority,
                    focus_policy=self.policy, writer_fence=self.fence)

    # --- Target Lock and Focus Policy Contract Tests (Pending build task: jq2a-m2-c2-focus-executor) ---

    @_expected_failure_if(not HAS_TARGET_LOCK_REGISTRY)
    def test_target_lock_registry_acquire_and_release(self):
        """Task jq2a-m2-c2-focus-executor: TargetLockRegistry acquire returns ActorLease and release releases ownership."""
        if not HAS_TARGET_LOCK_REGISTRY:
            raise ImportError("TargetLockRegistry from wender.act.target_lock_registry is absent")
        from wender.act.target_lock_registry import TargetLockRegistry
        registry = TargetLockRegistry(self.sessions)
        handle = self.sessions.register("cdp_tab", "tab-independent", object(), owned=False)
        lease = registry.acquire(handle, "owner-1", expires_ns=time.monotonic_ns() + 10**9)
        self.assertIsInstance(lease, ActorLease)
        registry.release(lease)

    @_expected_failure_if(not HAS_FOCUS_POLICY)
    def test_focus_policy_grant_and_classify(self):
        """Task jq2a-m2-c2-focus-executor: FocusPolicy grant returns FocusLease and classify handles operations."""
        if not HAS_FOCUS_POLICY:
            raise ImportError("FocusPolicy from wender.act.focus_policy is absent")
        from wender.act.focus_policy import FocusPolicy
        fp = self.policy
        fl = fp.grant(self.lease, domain="cdp:sess1")
        self.assertIsInstance(fl, FocusLease)
        from wender.act.focus_policy import FocusClass
        self.assertEqual(fp.classify("ax", "click"), FocusClass.SESSION_SCOPED)
        self.assertEqual(fp.classify("ax", "insert_text"), FocusClass.FOCUS_REQUIRED)

    @_expected_failure_if(not HAS_FOCUS_POLICY)
    def test_cdp_executor_accepts_lease_and_focus_kwonly_arguments(self):
        """Task jq2a-m2-c2-focus-executor: run_ax_action accepts lease and focus keyword arguments."""
        from wender.act.cdp_executor import run_ax_action
        cdp = FakeCdp()
        target = _make_target_node()
        action = {"op": "click", "target": target["id"]}
        # Expecting run_ax_action with lease and focus parameters to run when target owner is built
        res = run_ax_action(
            cdp,
            "sess1",
            [target],
            action,
            read_nodes=iter([[target], [_make_target_node(name="Clicked")]]).__next__,
            lease=self.lease,
            focus=self.focus,
            **self.authority_kwargs(target),
            sleep=lambda s: None,
        )
        self.assertEqual(res["ok"], 1)

    # --- Fresh Gate G M2 3 Refusal Tests (Negative mutants & boundary conditions) ---

    def test_unreadable_fresh_gate_refuses_when_read_nodes_raises_stop_iteration(self):
        """Gate G M2 3: Unreadable fresh gate (read_nodes raises StopIteration) must raise ActionError before send, without swallowing StopIteration."""
        from wender.act.cdp_executor import ActionError, run_ax_action
        cdp = FakeCdp()
        target = _make_target_node()
        action = {
            "op": "click",
            "target": target["id"],
            "irreversible": True,
            "expect": {"target_name": "Submitted"},
        }

        def raising_reader():
            raise StopIteration("fresh snapshot unreadable")

        # Must not swallow StopIteration; should raise ActionError or fail
        with self.assertRaises((ActionError, StopIteration)):
            run_ax_action(
                cdp,
                "sess1",
                [target],
                action,
                read_nodes=raising_reader,
                retries=2,
                sleep=lambda s: None,
            )

        # Confirm refusal happened before send (no transport calls)
        self.assertEqual(cdp.calls, [])

    def test_gate_refuses_on_missing_fresh_observation_without_scrolling_or_send(self):
        """Gate G M2 3: Missing fresh observation refuses before mutating scroll or send."""
        from wender.act.cdp_executor import ActionError, run_ax_action
        cdp = FakeCdp()
        target = _make_target_node()
        action = {
            "op": "click",
            "target": target["id"],
            "irreversible": True,
            "expect": {"target_name": "Submitted"},
        }

        def empty_reader():
            return []

        with self.assertRaises(ActionError):
            run_ax_action(
                cdp,
                "sess1",
                [target],
                action,
                read_nodes=empty_reader,
                retries=2,
                sleep=lambda s: None,
            )

        # Must refuse before scroll (DOM.scrollIntoViewIfNeeded) or send
        self.assertEqual(cdp.calls, [])

    def test_gate_refuses_on_tied_fresh_observation_without_send(self):
        """Gate G M2 3: Tied fresh observation introduces ambiguity and refuses before dispatch."""
        from wender.act.cdp_executor import ActionError, run_ax_action
        cdp = FakeCdp()
        target1 = _make_target_node(backend_id=101)
        target2 = _make_target_node(backend_id=102)
        action = {
            "op": "click",
            "target": target1["id"],
            "irreversible": True,
            "expect": {"target_name": "Submitted"},
        }

        def tied_reader():
            return [target1, target2]

        with self.assertRaises(ActionError):
            run_ax_action(
                cdp,
                "sess1",
                [target1],
                action,
                read_nodes=tied_reader,
                retries=2,
                sleep=lambda s: None,
            )

        self.assertEqual(cdp.calls, [])

    def test_irreversible_action_no_coordinate_retry_on_failure(self):
        """INV-M2-3: Irreversible actions dispatch once TARGET, without coordinate fallback."""
        from wender.act.cdp_executor import run_ax_action
        cdp = FakeCdp()
        target = _make_target_node()
        action = {
            "op": "click",
            "target": target["id"],
            "irreversible": True,
            "expect": {"target_name": "Submitted"},
        }

        # Reader returns initial target continuously (expectation 'Submitted' fails)
        reader = lambda: [target]

        receipt = run_ax_action(
            cdp,
            "sess1",
            [target],
            action,
            read_nodes=reader,
            retries=2,
            allow_coordinates=True,  # Even with allow_coordinates=True, irreversible must NOT fallback to coordinates
            sleep=lambda s: None,
        )

        # Receipt indicates failure at AX path, verified 0
        self.assertEqual(receipt["ok"], 0)
        self.assertEqual(receipt["path"], "ax")

        # Confirm no coordinate dispatch calls were attempted
        mouse_calls = [c for c in cdp.calls if c["method"] == "Input.dispatchMouseEvent"]
        self.assertEqual(mouse_calls, [])

    @_expected_failure_if(not HAS_FOCUS_POLICY)
    def test_stale_epoch_or_unregistered_lease_refuses(self):
        """Task jq2a-m2-c2-focus-executor: Stale epoch fresh observation or expired/unregistered lease refuses dispatch."""
        from wender.act.cdp_executor import ActionError, run_ax_action
        cdp = FakeCdp()
        target = _make_target_node()
        action = {"op": "click", "target": target["id"]}
        stale_lease = ActorLease(
            handle=self.handle,
            owner="executor-1",
            generation=1,  # Valid shape, but expired and unregistered
            nonce=b"nonce12345678901",
            expires_ns=time.monotonic_ns() - 1000,  # Expired
        )
        with self.assertRaises(ActionError):
            run_ax_action(
                cdp,
                "sess1",
                [target],
                action,
                read_nodes=lambda: [target],
                lease=stale_lease,
                focus=self.focus,
                **self.authority_kwargs(target),
                sleep=lambda s: None,
            )

    # --- Positive Control ---

    def test_positive_control_fresh_unique_backend_dispatches(self):
        """Positive control: A fresh unique observation permits exactly one dispatch successfully."""
        from wender.act.cdp_executor import run_ax_action
        cdp = FakeCdp()
        initial_target = _make_target_node(backend_id=101)
        fresh_target = _make_target_node(backend_id=202)

        def reader():
            return [fresh_target]

        action = {
            "op": "click",
            "target": initial_target["id"],
            "irreversible": True,
            "expect": {"target_name": "Submit"},
        }

        receipt = run_ax_action(
            cdp,
            "sess1",
            [initial_target],
            action,
            read_nodes=reader,
            retries=1,
            sleep=lambda s: None,
        )

        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["verified"], 1)
        # Verify fresh backend_id 202 was used in transport
        self.assertEqual(cdp.calls[0]["params"].get("backendNodeId"), 202)


if __name__ == "__main__":
    unittest.main()
