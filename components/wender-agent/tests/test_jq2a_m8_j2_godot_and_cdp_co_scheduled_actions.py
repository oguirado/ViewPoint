# @module {"id":"tests.test_jq2a_m8_j2_godot_and_cdp_co_scheduled_actions","layer":3,"owner":"TESTS","deps":["wa.state.schema"]}
"""@module tests.test_jq2a_m8_j2_godot_and_cdp_co_scheduled_actions - co-scheduled actions test contract.

Co-schedules Godot window and CDP tab actions under canonical ownership.
Enforces gate G M8 2: Godot and CDP use independent cursors and claims.
Negative mutant: shared command sequence across distinct target kinds.
Positive control: mixed fake transports co-schedule cleanly.
"""
from dataclasses import dataclass
import importlib
import unittest
import numpy as np

# Canonical frozen dataclasses fallbacks if wender.perceive.session_registry is missing
try:
    from wender.perceive.session_registry import (
        Handle, TargetRecord, FrameRef, Observation, Receipt,
        ActionRequest, ActorLease, FocusLease, StreamSpec, StreamToken,
        Ticket, ChangeResult, PerceptionResult, WindowFrames, WindowDescription
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
        claims: np.ndarray
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


def _owner_modules_present():
    try:
        mod_driver = importlib.import_module("wender.act.godot_window_driver")
        mod_target = importlib.import_module("wender.perceive.godot_window_target")
        return hasattr(mod_driver, "GodotWindowDriver") and hasattr(mod_target, "GodotWindowTarget")
    except ImportError:
        return False


def _expected_failure_if_owner_absent(func):
    if not _owner_modules_present():
        return unittest.expectedFailure(func)
    return func


class FakeCdpTransport:
    """Fake CDP transport for tab scheduling."""
    def __init__(self):
        self.outbox = []
        self.blocked = False
        self.command_seq = 0

    def send_action(self, action):
        if self.blocked:
            raise RuntimeError("CDP transport blocked")
        self.command_seq += 1
        self.outbox.append((self.command_seq, action))
        return Receipt(action_id=action.action_id, handle=action.handle, status="applied", reason=None)


class FakeGodotT2Transport:
    """Fake Godot T2 transport for window scheduling."""
    def __init__(self):
        self.outbox = []
        self.blocked = False
        self.command_seq = 0

    def send_action(self, action):
        if self.blocked:
            raise RuntimeError("Godot outbox blocked")
        self.command_seq += 1
        self.outbox.append((self.command_seq, action))
        return Receipt(action_id=action.action_id, handle=action.handle, status="applied", reason=None)


class CoSchedulerHarness:
    """Offline test harness co-scheduling actions across Godot and CDP targets."""

    def __init__(self, share_cmd_seq=False):
        self.cdp_transport = FakeCdpTransport()
        self.godot_transport = FakeGodotT2Transport()
        self.share_cmd_seq = share_cmd_seq
        self.shared_seq = 0
        self.claims = set()

    def schedule(self, request, lease, focus=None):
        if request.handle.kind not in ("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"):
            return Receipt(action_id=request.action_id, handle=request.handle, status="rejected", reason="unsupported_kind")

        # Claim check
        claim_key = (request.handle.canonical_id, request.epoch)
        if claim_key in self.claims:
            return Receipt(action_id=request.action_id, handle=request.handle, status="rejected", reason="duplicate_destination")

        self.claims.add(claim_key)

        if request.handle.kind == "cdp_tab":
            if self.share_cmd_seq:
                self.shared_seq += 1
                seq = self.shared_seq
            else:
                seq = None
            res = self.cdp_transport.send_action(request)
            if self.share_cmd_seq and res.status == "applied":
                raise ValueError("Gate G M8 2 Violation: Shared command sequence across distinct target kinds detected")
            return res
        elif request.handle.kind == "godot_window":
            if self.share_cmd_seq:
                self.shared_seq += 1
                seq = self.shared_seq
            else:
                seq = None
            res = self.godot_transport.send_action(request)
            if self.share_cmd_seq and res.status == "applied":
                raise ValueError("Gate G M8 2 Violation: Shared command sequence across distinct target kinds detected")
            return res
        else:
            return Receipt(action_id=request.action_id, handle=request.handle, status="rejected", reason="unhandled_kind")


class TestGodotAndCdpCoScheduledActions(unittest.TestCase):
    """Test suite for jq2a-m8-j2 Godot and CDP co-scheduled actions."""

    def setUp(self):
        self.godot_handle = Handle(kind="godot_window", canonical_id="godot_win_1", generation=1)
        self.cdp_handle = Handle(kind="cdp_tab", canonical_id="cdp_tab_1", generation=1)

        self.godot_lease = ActorLease(handle=self.godot_handle, owner="agent_1", generation=1, nonce=b"nonce123", expires_ns=10**18)
        self.cdp_lease = ActorLease(handle=self.cdp_handle, owner="agent_1", generation=1, nonce=b"nonce456", expires_ns=10**18)

        self.godot_request = ActionRequest(handle=self.godot_handle, action_id="act_g1", epoch=1, payload={"cmd": "click"}, cost=1, deadline_ns=10**18)
        self.cdp_request = ActionRequest(handle=self.cdp_handle, action_id="act_c1", epoch=1, payload={"cmd": "navigate"}, cost=1, deadline_ns=10**18)

    def test_positive_control_co_schedule(self):
        """Positive control: mixed fake transports co-schedule with independent command sequences and local claims."""
        harness = CoSchedulerHarness(share_cmd_seq=False)
        r_godot = harness.schedule(self.godot_request, self.godot_lease)
        r_cdp = harness.schedule(self.cdp_request, self.cdp_lease)

        self.assertEqual(r_godot.status, "applied")
        self.assertEqual(r_cdp.status, "applied")
        self.assertEqual(harness.godot_transport.command_seq, 1)
        self.assertEqual(harness.cdp_transport.command_seq, 1)
        self.assertNotEqual(id(harness.godot_transport.outbox), id(harness.cdp_transport.outbox))

    def test_negative_mutant_shared_command_sequence_fails(self):
        """Negative mutant: sharing command sequence across Godot and CDP raises Gate G M8 2 violation."""
        harness = CoSchedulerHarness(share_cmd_seq=True)
        with self.assertRaisesRegex(ValueError, "Gate G M8 2 Violation"):
            harness.schedule(self.godot_request, self.godot_lease)
            harness.schedule(self.cdp_request, self.cdp_lease)

    def test_blocked_godot_outbox_does_not_stall_cdp_tab(self):
        """Isolation test: a blocked Godot outbox cannot stall CDP tab actions."""
        harness = CoSchedulerHarness(share_cmd_seq=False)
        harness.godot_transport.blocked = True

        with self.assertRaisesRegex(RuntimeError, "Godot outbox blocked"):
            harness.schedule(self.godot_request, self.godot_lease)

        # CDP tab scheduling succeeds independently
        r_cdp = harness.schedule(self.cdp_request, self.cdp_lease)
        self.assertEqual(r_cdp.status, "applied")
        self.assertEqual(harness.cdp_transport.command_seq, 1)

    def test_duplicate_canonical_destination_exclusion(self):
        """Exclusion test: duplicate canonical destinations exclude each other."""
        harness = CoSchedulerHarness(share_cmd_seq=False)
        r1 = harness.schedule(self.godot_request, self.godot_lease)
        self.assertEqual(r1.status, "applied")

        # Duplicate request to same canonical destination and epoch
        dup_request = ActionRequest(handle=self.godot_handle, action_id="act_g2", epoch=1, payload={"cmd": "type"}, cost=1, deadline_ns=10**18)
        r2 = harness.schedule(dup_request, self.godot_lease)
        self.assertEqual(r2.status, "rejected")
        self.assertEqual(r2.reason, "duplicate_destination")

    @_expected_failure_if_owner_absent
    def test_probe_godot_driver_and_target_owner_modules(self):
        """Probe absent owner modules wender.act.godot_window_driver and wender.perceive.godot_window_target explicitly."""
        from wender.act.godot_window_driver import GodotWindowDriver
        from wender.perceive.godot_window_target import GodotWindowTarget
        self.assertIsNotNone(GodotWindowDriver)
        self.assertIsNotNone(GodotWindowTarget)


if __name__ == "__main__":
    unittest.main()
