# @module {"id":"wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing","layer":1,"owner":"TEST","deps":[],"book":"HB-WA-s2-act"}
"""Contract tests for lease expiry, owner fencing, and quarantine persistence (Gate G M2 5).

Fix / Task ID: jq2a-m2-j5-lease-expiry-and-owner-fencing
Owning implementation task_id: jq2a-m2-c1-target-locks
"""

import dataclasses
import unittest
from typing import Any

try:
    import numpy
except ImportError:
    numpy = Any


def _probe_target_lock_registry() -> type | None:
    # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing._probe_target_lock_registry
    try:
        from wender.act.target_lock_registry import TargetLockRegistry  # type: ignore

        return TargetLockRegistry
    except ImportError:
        return None


_TARGET_LOCK_REGISTRY = _probe_target_lock_registry()


def expected_if_absent(func):
    # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.expected_if_absent
    """Apply unittest.expectedFailure only while TargetLockRegistry is absent."""
    if _TARGET_LOCK_REGISTRY is None:
        return unittest.expectedFailure(func)
    return func


# Import canonical records from session_registry if present, else use frozen fallback dataclasses.
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
    @dataclasses.dataclass(frozen=True)
    class Handle:
        # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.Handle
        kind: str
        canonical_id: str
        generation: int

    @dataclasses.dataclass(frozen=True)
    class TargetRecord:
        # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.TargetRecord
        handle: Handle
        transport: object
        owned: bool
        epoch: int
        session_id: str | None
        attachment_owned: bool
        closed: bool

    @dataclasses.dataclass(frozen=True)
    class FrameRef:
        # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.FrameRef
        handle: Handle
        epoch: int
        frame_seq: int
        width: int
        height: int
        rgb: object
        digest: str

    @dataclasses.dataclass(frozen=True)
    class Observation:
        # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.Observation
        handle: Handle
        epoch: int
        frame_seq: int
        dpr_ppm: int
        state: dict
        frame: FrameRef

    @dataclasses.dataclass(frozen=True)
    class Receipt:
        # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.Receipt
        action_id: str
        handle: Handle
        status: str
        reason: str | None

    @dataclasses.dataclass(frozen=True)
    class ActionRequest:
        # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.ActionRequest
        handle: Handle
        action_id: str
        epoch: int
        payload: dict
        cost: int
        deadline_ns: int

    @dataclasses.dataclass(frozen=True)
    class ActorLease:
        # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.ActorLease
        handle: Handle
        owner: str
        generation: int
        nonce: bytes
        expires_ns: int

    @dataclasses.dataclass(frozen=True)
    class FocusLease:
        # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.FocusLease
        lease: ActorLease
        domain: str
        epoch: int

    @dataclasses.dataclass(frozen=True)
    class StreamSpec:
        # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.StreamSpec
        width: int
        height: int
        slots: int
        quota_bytes: int

    @dataclasses.dataclass(frozen=True)
    class StreamToken:
        # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.StreamToken
        handle: Handle
        epoch: int

    @dataclasses.dataclass(frozen=True)
    class Ticket:
        # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.Ticket
        action_id: str
        handle: Handle

    @dataclasses.dataclass(frozen=True)
    class ChangeResult:
        # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.ChangeResult
        handle: Handle
        epoch: int
        settled: bool
        claims: Any
        ops: dict

    @dataclasses.dataclass(frozen=True)
    class PerceptionResult:
        # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.PerceptionResult
        handle: Handle
        observation: Observation | None
        error: str | None

    @dataclasses.dataclass(frozen=True)
    class WindowFrames:
        # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.WindowFrames
        handle: Handle
        epoch: int
        frames: tuple
        frame_seqs: tuple
        dpr_ppm: int

    @dataclasses.dataclass(frozen=True)
    class WindowDescription:
        # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.WindowDescription
        handle: Handle
        epoch: int
        frame_seqs: tuple
        descriptors: tuple
        regions: tuple
        error: str | None


class TestRecordValueContracts(unittest.TestCase):
    # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.TestRecordValueContracts
    """Positive controls verifying record structure and duck-typing consistency without unmerged owner modules."""

    def test_handle_record_construction(self):
        # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.TestRecordValueContracts.test_handle_record_construction
        handle = Handle(kind="cdp_tab", canonical_id="tab-123", generation=1)
        self.assertEqual(handle.kind, "cdp_tab")
        self.assertEqual(handle.canonical_id, "tab-123")
        self.assertEqual(handle.generation, 1)

    def test_actor_lease_record_construction(self):
        # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.TestRecordValueContracts.test_actor_lease_record_construction
        handle = Handle(kind="x11_window", canonical_id="win-456", generation=2)
        lease = ActorLease(
            handle=handle,
            owner="worker-1",
            generation=2,
            nonce=b"\x00" * 16,
            expires_ns=1000000,
        )
        self.assertEqual(lease.handle, handle)
        self.assertEqual(lease.owner, "worker-1")
        self.assertEqual(lease.generation, 2)
        self.assertEqual(lease.nonce, b"\x00" * 16)
        self.assertEqual(lease.expires_ns, 1000000)

    def test_receipt_record_construction(self):
        # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.TestRecordValueContracts.test_receipt_record_construction
        handle = Handle(kind="xvfb_screen", canonical_id="scr-1", generation=1)
        receipt = Receipt(action_id="act-1", handle=handle, status="OK", reason=None)
        self.assertEqual(receipt.action_id, "act-1")
        self.assertEqual(receipt.status, "OK")


class TestGateM2J5LeaseExpiryAndOwnerFencing(unittest.TestCase):
    # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.TestGateM2J5LeaseExpiryAndOwnerFencing
    """Contract tests for Gate G M2 5: Expiry/replayed release never unlocks active command."""

    @expected_if_absent
    def test_gate_g_m2_5_expiry_after_send_does_not_unlock_in_flight_command(self):
        # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.TestGateM2J5LeaseExpiryAndOwnerFencing.test_gate_g_m2_5_expiry_after_send_does_not_unlock_in_flight_command
        # Target build task: jq2a-m2-c1-target-locks
        from wender.act.target_lock_registry import TargetLockRegistry

        from wender.perceive.session_registry import SessionRegistry
        sessions = SessionRegistry()
        transport = object()
        registry = TargetLockRegistry(registry=sessions, clock=lambda: 100)
        handle = sessions.register("cdp_tab", "tab-1", transport, owned=False)

        lease = registry.acquire(handle, "owner-1", expires_ns=200, now_ns=100)
        registry.mark_inflight(lease)

        # Time passes beyond expiry
        # Negative mutant: expiry automatically frees in-flight lease
        # Correct contract: expiry fences ownership but never proves a sent command stopped; quarantine persists.
        # New acquire attempt must fail or be refused because in-flight command is not acknowledged quiescent.
        with self.assertRaises((RuntimeError, ValueError, PermissionError)):
            registry.acquire(handle, "owner-2", expires_ns=500, now_ns=300)

    @expected_if_absent
    def test_replayed_release_and_forged_nonce_fenced_after_new_generation(self):
        # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.TestGateM2J5LeaseExpiryAndOwnerFencing.test_replayed_release_and_forged_nonce_fenced_after_new_generation
        # Target build task: jq2a-m2-c1-target-locks
        from wender.act.target_lock_registry import TargetLockRegistry

        from wender.perceive.session_registry import SessionRegistry
        sessions = SessionRegistry()
        transport = object()
        registry = TargetLockRegistry(registry=sessions, clock=lambda: 100)
        handle_gen1 = sessions.register("cdp_tab", "tab-1", transport, owned=False)

        lease1 = registry.acquire(handle_gen1, "owner-1", expires_ns=1000, now_ns=100)

        # Quiescent release of lease1 succeeds
        registry.release(lease1)

        # Register new generation on same target
        sessions.close(handle_gen1)
        handle_gen2 = sessions.register("cdp_tab", "tab-1", transport, owned=False)
        lease2 = registry.acquire(handle_gen2, "owner-2", expires_ns=2000, now_ns=200)

        # Replaying release of old lease1 must fail or have no effect on gen2
        with self.assertRaises((RuntimeError, ValueError, PermissionError)):
            registry.release(lease1)

        # Forging a lease with wrong/forged nonce or generation must fail
        forged_lease = ActorLease(
            handle=handle_gen2,
            owner="owner-2",
            generation=2,
            nonce=b"forged_nonce_123",
            expires_ns=2000,
        )
        with self.assertRaises((RuntimeError, ValueError, PermissionError)):
            registry.release(forged_lease)

    @expected_if_absent
    def test_quarantine_persists_until_explicit_quiescence_reconciliation(self):
        # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.TestGateM2J5LeaseExpiryAndOwnerFencing.test_quarantine_persists_until_explicit_quiescence_reconciliation
        # Target build task: jq2a-m2-c1-target-locks
        from wender.act.target_lock_registry import TargetLockRegistry

        from wender.perceive.session_registry import SessionRegistry
        sessions = SessionRegistry()
        transport = object()
        registry = TargetLockRegistry(registry=sessions, clock=lambda: 100)
        handle = sessions.register("cdp_tab", "tab-1", transport, owned=False)

        lease = registry.acquire(handle, "owner-1", expires_ns=200, now_ns=100)
        registry.mark_inflight(lease)

        # Unacknowledged reconciliation leaves target in quarantine
        registry.reconcile_quarantine(handle, acknowledged=False)

        with self.assertRaises((RuntimeError, ValueError, PermissionError)):
            registry.acquire(handle, "owner-2", expires_ns=1000, now_ns=300)

        # Positive control: explicit acknowledged quiescence permits new acquisition
        registry.reconcile_quarantine(handle, acknowledged=True)
        new_lease = registry.acquire(handle, "owner-2", expires_ns=1000, now_ns=350)
        self.assertIsNotNone(new_lease)
        self.assertEqual(new_lease.owner, "owner-2")

    @expected_if_absent
    def test_positive_control_quiescent_owner_release_succeeds(self):
        # @anchor wa.test.test_jq2a_m2_j5_lease_expiry_and_owner_fencing.TestGateM2J5LeaseExpiryAndOwnerFencing.test_positive_control_quiescent_owner_release_succeeds
        # Target build task: jq2a-m2-c1-target-locks
        from wender.act.target_lock_registry import TargetLockRegistry

        from wender.perceive.session_registry import SessionRegistry
        sessions = SessionRegistry()
        transport = object()
        registry = TargetLockRegistry(registry=sessions, clock=lambda: 100)
        handle = sessions.register("x11_window", "win-99", transport, owned=False)

        lease = registry.acquire(handle, "owner-1", expires_ns=1000, now_ns=100)
        self.assertEqual(lease.owner, "owner-1")

        # Quiescent owner release
        registry.release(lease)

        # Next owner can acquire target immediately
        lease2 = registry.acquire(handle, "owner-2", expires_ns=2000, now_ns=150)
        self.assertEqual(lease2.owner, "owner-2")


if __name__ == "__main__":
    unittest.main()
