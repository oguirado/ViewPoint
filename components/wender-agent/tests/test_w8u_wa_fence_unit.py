"""Offline W8U fence gate using the merged registry/focus/scheduler owners.

All dimensions, revision pins, capacities, clocks and vectors below are TARGET
fixtures.  VM timings are unittest timings, never host measurements.  The fake
authority authenticates by minted object identity outside the action payload;
it does not implement locks, focus policy, writer permits or session records.
Frame bytes exist only in RAM and independent digests are computed in memory.
The absent production OS/wire/action/receipt owners are not locally simulated.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, replace
import hashlib
import io
import os
from pathlib import Path
import socket
import struct
import subprocess
import unittest
from unittest.mock import patch

import numpy as np

from wender.act import target_lock_registry as lock_module
from wender.act.focus_policy import FocusPolicy, FocusRefused
from wender.act.target_lock_registry import TargetBusy, TargetLockRefused, TargetLockRegistry
from wender.act.window_scheduler import (
    Completion, DispatchContext, FenceRefused, Ticket, WindowScheduler, WriterFence,
)
from wender.act.w8u_fence import (
    AuthenticatedRegistryAdapter, DisplayGrant, FenceError, FocusEvidence,
    RevisionPinnedFrames, SchedulerFocusAdapter, SchedulerLockAdapter, W8UFence,
)
from wender.perceive.session_registry import (
    ActionRequest, FrameRef, Handle, Observation, SessionRegistry,
)


# This is an explicit synthetic TARGET pin, not an observed upstream revision.
TARGET_REVISION = "a" * 40


def handle_key(handle):
    return handle.kind, handle.canonical_id, handle.generation


class MetadataProbe:
    def __init__(self):
        self.writer = None
        self.calls = []
        self.additional_locks = []

    def register(self, lock, name):
        self.additional_locks.append((lock, name))

    def __call__(self, operation):
        if lock_module._SLOT_GUARD._is_owned():
            raise AssertionError(operation + " called under target metadata lock")
        if self.writer is not None and self.writer.metadata_lock._is_owned():
            raise AssertionError(operation + " called under writer metadata lock")
        for lock, name in self.additional_locks:
            owned = lock._is_owned() if hasattr(lock, "_is_owned") else lock.locked()
            if owned:
                raise AssertionError(operation + " called under " + name + " metadata lock")
        self.calls.append(operation)


class FakeClock:
    def __init__(self, probe):
        self.probe = probe
        self.ns = 100

    def __call__(self):
        self.probe("clock")
        return self.ns


class AuthenticatedAuthority:
    """Test-only capability issuer; caller strings never authenticate a grant."""

    def __init__(self, sessions, probe):
        self.sessions, self.probe = sessions, probe
        self.grants = {}
        self.current_focus = {}
        self._issued_grants = {}
        self._issued_focus = {}
        self.revoked = set()
        self.on_grant = None

    def issue(self, handle, *, seat="seat-A", display="display-A", domain="input-A"):
        record = self.sessions.resolve(handle)
        grant = DisplayGrant(handle, record.epoch, seat, display, domain, object())
        self.grants[handle_key(handle)] = grant
        self._issued_grants[id(grant)] = (
            grant, handle_key(handle), record.epoch, seat, display, domain, grant.proof,
        )
        return grant

    def focus(self, handle, *, editable=True):
        grant = self.grants[handle_key(handle)]
        evidence = FocusEvidence(handle, self.sessions.resolve(handle).epoch,
                                 grant.domain, editable, object())
        self.current_focus[grant.domain] = evidence
        self._issued_focus[id(evidence)] = (
            evidence, handle_key(handle), evidence.epoch, evidence.domain,
            editable, evidence.proof,
        )
        return evidence

    def grant_for(self, handle):
        self.probe("grant_for")
        if self.on_grant is not None:
            self.on_grant()
        return self.grants.get(handle_key(handle))

    def verify_grant(self, grant, *, registry, now_ns):
        self.probe("verify_grant")
        original = self._issued_grants.get(id(grant))
        return (registry is self.sessions and type(now_ns) is int
                and original is not None and original[0] is grant
                and id(grant) not in self.revoked
                and (handle_key(grant.handle), grant.epoch, grant.seat,
                     grant.display, grant.domain, grant.proof) == original[1:])

    def focus_for(self, handle):
        self.probe("focus_for")
        grant = self.grants.get(handle_key(handle))
        if grant is None:
            return None
        current = self.current_focus.get(grant.domain)
        return current if current is not None and current.handle == handle else None

    def verify_focus(self, evidence, *, registry, now_ns):
        self.probe("verify_focus")
        original = self._issued_focus.get(id(evidence))
        return (registry is self.sessions and type(now_ns) is int
                and original is not None and original[0] is evidence
                and self.current_focus.get(evidence.domain) is evidence
                and (handle_key(evidence.handle), evidence.epoch, evidence.domain,
                     evidence.editable, evidence.proof) == original[1:])


@dataclass(frozen=True)
class FrameWitness:
    handle: object
    epoch: int
    sequence: int
    digest: str
    proof: object


class FrameProvider:
    contract_id = "w8u-os-contract"
    revision = TARGET_REVISION

    def __init__(self, probe):
        self.probe = probe
        self.witnesses = {}
        self.allowed = True
        self.on_verify = None
        # Independent fixture builder: fixed-width dimensions and a digest of
        # generated bytes. No wire magic or shared digest domain is invented.
        sample = bytes(range(12))
        self.expected_vector = struct.pack(">II", 2, 2) + hashlib.sha256(sample).digest()

    def vectors(self):
        accepted = struct.pack(">II", 2, 2) + hashlib.sha256(bytes(range(12))).digest()
        rejected = accepted[:-1] + bytes([accepted[-1] ^ 1])
        return accepted, rejected

    def verify_vector(self, vector, *, revision):
        self.probe("verify_vector")
        return revision == self.revision and vector == self.expected_vector

    def witness(self, observation):
        # This expectation is computed from raw RAM pixels independently of
        # the production FrameRef factory and does not copy its digest field.
        digest = hashlib.sha256(memoryview(observation.frame.rgb).tobytes()).hexdigest()
        witness = FrameWitness(observation.handle, observation.epoch,
                               observation.frame_seq, digest, object())
        self.witnesses[id(witness)] = witness
        return witness

    def verify(self, observation, evidence, *, revision, now_ns):
        self.probe("frame.verify")
        if self.on_verify is not None:
            self.on_verify()
        if not self.allowed or revision != self.revision:
            return False
        if self.witnesses.get(id(evidence)) is not evidence:
            return False
        digest = hashlib.sha256(memoryview(observation.frame.rgb).tobytes()).hexdigest()
        return (type(now_ns) is int
                and (evidence.handle, evidence.epoch, evidence.sequence, evidence.digest)
                == (observation.handle, observation.epoch, observation.frame_seq, digest)
                and observation.frame.digest == digest)


class InjectedBudget:
    def __init__(self, probe):
        self.probe = probe
        self.allowed = True
        self.valid = True
        self.tokens = {}
        self.settlements = []
        self.on_validate = None

    def reserve(self, handle, cost, *, action_id, deadline_ns):
        self.probe("budget.reserve")
        if not self.allowed:
            return None
        token = object()
        self.tokens[token] = [handle, cost, action_id, deadline_ns, None]
        return token

    def validate(self, reservation, *, handle, action_id, now_ns):
        self.probe("budget.validate")
        if self.on_validate is not None:
            self.on_validate()
        values = self.tokens.get(reservation)
        return (self.valid and values is not None and values[0] == handle
                and values[2] == action_id and values[4] is None
                and type(now_ns) is int and now_ns < values[3])

    def settle(self, reservation, *, consumed):
        self.probe("budget.settle")
        if type(consumed) is not bool:
            raise AssertionError("budget consumed must be an exact bool")
        values = self.tokens[reservation]
        if values[4] is not None:
            raise AssertionError("budget reservation settled twice")
        values[4] = consumed
        self.settlements.append((reservation, consumed))


class FenceGate(unittest.TestCase):
    def setUp(self):
        self.io_guards = ExitStack()
        self.addCleanup(self.io_guards.close)
        self.disk_calls = []

        def forbidden(*args, **kwargs):
            self.disk_calls.append("forbidden sink")
            raise AssertionError("offline gate forbids disk, network and process I/O")

        for target in (
            "builtins.open", "io.open", "pathlib.Path.open", "os.open",
            "socket.socket", "socket.create_connection", "subprocess.Popen",
            "subprocess.run", "os.system", "os.popen",
        ):
            self.io_guards.enter_context(patch(target, side_effect=forbidden))

        self.probe = MetadataProbe()
        self.clock = FakeClock(self.probe)
        self.writer = WriterFence(clock=self.clock)
        self.probe.writer = self.writer
        self.sessions = SessionRegistry(clock=self.clock)
        self.probe.register(self.sessions._lock, "session registry")
        self.authority = AuthenticatedAuthority(self.sessions, self.probe)
        # Real, distinct valid incarnations: bind_alias alone would hide the
        # physical-resource arbitration requirement this gate is exercising.
        self.capsule = self.sessions.register("capsule", "capsule-A", object(), owned=False)
        self.screen = self.sessions.register("xvfb_screen", "screen-A", object(), owned=False)
        self.child = self.sessions.register("x11_window", "child-A", object(), owned=False)
        self.other = self.sessions.register("xvfb_screen", "screen-B", object(), owned=False)
        for handle in (self.capsule, self.screen, self.child):
            self.authority.issue(handle)
        self.authority.issue(self.other, display="display-B", domain="input-B")
        self.authority.focus(self.capsule)
        self.authority.focus(self.other)
        self.registry = AuthenticatedRegistryAdapter(
            self.sessions, self.authority, clock=self.clock)
        self.probe.register(self.registry._lock, "registry adapter")
        self.raw = TargetLockRegistry(self.registry, clock=self.clock)
        self.locks = SchedulerLockAdapter(self.raw)
        self.probe.register(self.locks._lock, "lock adapter")
        self.focus = SchedulerFocusAdapter(
            self.registry, self.locks, self.authority, clock=self.clock)
        self.probe.register(self.focus._lock, "focus adapter")
        self.probe.register(self.focus.policy._mutex, "focus policy")
        self.frame_provider = FrameProvider(self.probe)
        accepted, rejected = self.frame_provider.vectors()
        self.frames = RevisionPinnedFrames(
            self.frame_provider, revision=TARGET_REVISION,
            accepted_vector=accepted, rejected_vector=rejected)
        self.budget = InjectedBudget(self.probe)
        self.fence = W8UFence(
            registry=self.registry, locks=self.locks, focus=self.focus,
            writer_fence=self.writer, budget=self.budget,
            frames=self.frames, clock=self.clock)
        self.probe.register(self.fence._lock, "W8U fence")
        self.observations = {}
        self.evidence = {}
        for handle in (self.capsule, self.screen, self.child, self.other):
            pixels = np.arange(18, dtype=np.uint8).reshape((2, 3, 3))
            frame = self.sessions.admit_frame(handle, 1, pixels)
            observation = self.sessions.admit_observation(
                handle, frame, state={"editable": True}, dpr_ppm=1_000_000)
            self.observations[handle_key(handle)] = observation
            self.evidence[handle_key(handle)] = self.frame_provider.witness(observation)
        self.events = []
        self.next_action = 0

    def tearDown(self):
        self.assertEqual(self.disk_calls, [], "no frame or other data may reach a disk sink")

    def request(self, handle=None, *, epoch=None, deadline=None, action_id=None):
        handle = self.capsule if handle is None else handle
        self.next_action += 1
        return ActionRequest(
            handle, action_id or "action-%d" % self.next_action,
            self.sessions.resolve(handle).epoch if epoch is None else epoch,
            {"verb": "pointer", "args": {"operation": "click", "x": 0, "y": 0, "button": 1}},
            1, self.clock.ns + 10_000 if deadline is None else deadline,
        )

    def prepare(self, handle=None, **kwargs):
        request = self.request(handle, **kwargs)
        return self.fence.prepare(
            request, observation=self.observations[handle_key(request.handle)],
            frame_evidence=self.evidence[handle_key(request.handle)])

    def actuator(self, request):
        self.probe("actuator")
        self.events.append((request.action_id, handle_key(request.handle)))
        return Completion(request.action_id, request.handle, "SUCCEEDED", True)

    def lost_ack(self, request):
        self.probe("actuator.lost_ack")
        self.events.append((request.action_id, handle_key(request.handle)))
        return None

    def assert_code(self, code, callback, *args, **kwargs):
        with self.assertRaises(FenceError) as caught:
            callback(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def take(self, handle=None, *, owner="operator", deadline=10_100):
        return self.raw.acquire(self.capsule if handle is None else handle,
                                owner, expires_ns=deadline)

    def test_positive_control_genuine_nonce_real_focus_one_event_and_replay(self):
        prepared = self.prepare()
        self.assertIsInstance(prepared.context, DispatchContext)
        self.assertIsInstance(self.focus.policy, FocusPolicy)
        self.assertTrue(self.raw.owns(prepared.context.actor_lease, now_ns=self.clock.ns))
        self.assertIs(prepared.context.focus_lease.lease, prepared.context.actor_lease)
        self.assertEqual(self.events, [])  # Admission is not an effect.
        completion = self.fence.send(prepared, self.actuator)
        self.assertEqual(completion.status, "SUCCEEDED")
        self.assertTrue(completion.acknowledged)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.fence.send(prepared, self.actuator), completion)
        self.assertEqual(len(self.events), 1)
        self.assertFalse(self.raw.owns(prepared.context.actor_lease, now_ns=self.clock.ns))
        fresh = self.take(self.screen)
        self.raw.release(fresh)

    def test_identical_prepare_is_idempotent_and_changed_payload_refuses(self):
        request = self.request(action_id="stable-action")
        observation = self.observations[handle_key(self.capsule)]
        evidence = self.evidence[handle_key(self.capsule)]
        prepared = self.fence.prepare(request, observation=observation, frame_evidence=evidence)
        identical = replace(request)
        self.assertIs(self.fence.prepare(identical, observation=observation,
                                        frame_evidence=evidence), prepared)
        changed = replace(request)
        changed.payload["args"]["x"] = 1
        with patch.object(self.raw, "acquire", wraps=self.raw.acquire) as acquire:
            self.assert_code("E_SCHEMA", self.fence.prepare, changed,
                             observation=observation, frame_evidence=evidence)
            self.assertEqual(acquire.call_count, 0)
        self.assertEqual(self.events, [])
        self.fence.send(prepared, self.actuator)
        self.assertEqual(len(self.events), 1)

    def test_mutating_callers_request_cannot_change_prepared_actuator_payload(self):
        request = self.request()
        prepared = self.fence.prepare(
            request, observation=self.observations[handle_key(self.capsule)],
            frame_evidence=self.evidence[handle_key(self.capsule)])
        request.payload["args"]["x"] = 2

        def inspect_payload(dispatched):
            self.assertEqual(dispatched.payload["args"]["x"], 0)
            return self.actuator(dispatched)

        self.fence.send(prepared, inspect_payload)
        self.assertEqual(len(self.events), 1)

    def test_mutating_prepared_payload_is_changed_action_and_never_sends(self):
        prepared = self.prepare()
        prepared.request.payload["args"]["x"] = 2
        self.assert_code("E_SCHEMA", self.fence.send, prepared, self.actuator)
        self.assertEqual(self.events, [])

    def test_registry_get_uses_real_current_epoch_and_closed_state(self):
        record = self.registry.get(self.capsule)
        self.assertEqual(record, self.sessions.resolve(self.capsule))
        self.assertFalse(record.closed)
        self.sessions.advance_epoch(self.capsule)
        self.assertEqual(self.registry.get(self.capsule).epoch, 1)
        self.sessions.close(self.capsule)
        self.assert_code("E_LOCK", self.registry.get, self.capsule)
        self.assertEqual(self.events, [])

    def test_distinct_capsule_screen_child_have_one_custody_resource(self):
        self.assertEqual(len({handle_key(h) for h in (self.capsule, self.screen, self.child)}), 3)
        resources = {self.registry.canonical_resource(h) for h in (self.capsule, self.screen, self.child)}
        self.assertEqual(len(resources), 1)
        lease = self.take()
        contender = TargetLockRegistry(self.registry, clock=self.clock)
        for handle in (self.screen, self.child):
            with self.subTest(kind=handle.kind), self.assertRaisesRegex(
                    TargetBusy, "already owned or quarantined"):
                contender.acquire(handle, "later-agent", expires_ns=10_100)
        independent = contender.acquire(self.other, "independent", expires_ns=10_100)
        self.assertTrue(contender.owns(independent, now_ns=self.clock.ns))
        contender.release(independent)
        self.raw.release(lease)
        self.assertEqual(self.events, [])

    def test_second_adapter_for_same_registry_cannot_split_lock_namespace(self):
        second = AuthenticatedRegistryAdapter(self.sessions, self.authority, clock=self.clock)
        self.assertIs(second, self.registry)
        first = self.take()
        second_locks = SchedulerLockAdapter(TargetLockRegistry(second, clock=self.clock))
        self.assertIsNone(second_locks.try_acquire(self.child, owner="later", expires_ns=10_100))
        self.raw.release(first)
        self.assertEqual(self.events, [])

    def test_conflicting_authority_cannot_rebind_same_session_registry(self):
        other_authority = AuthenticatedAuthority(self.sessions, self.probe)
        self.assert_code("E_LOCK", AuthenticatedRegistryAdapter,
                         self.sessions, other_authority, clock=self.clock)
        self.assertEqual(self.events, [])

    def test_copied_and_foreign_leases_fail_real_focus_policy(self):
        lease = self.take()
        self.focus.policy.set_focus(self.capsule, domain="input-A", epoch=0)
        foreign_registry = SessionRegistry(clock=self.clock)
        foreign_handle = foreign_registry.register("capsule", "capsule-A", object(), owned=False)
        self.assertEqual(foreign_handle, self.capsule)
        foreign = TargetLockRegistry(foreign_registry, clock=self.clock)
        foreign_lease = foreign.acquire(foreign_handle, "foreign", expires_ns=10_100)
        for forged in (replace(lease), replace(lease, owner="swapped"), foreign_lease):
            with self.subTest(owner=forged.owner):
                self.assertFalse(self.raw.owns(forged, now_ns=self.clock.ns))
                # The direct real-owner assertion prevents an import crash or
                # arbitrary wrapper exception from counting as a refusal.
                with self.assertRaisesRegex(FocusRefused, "^actor lease nonce is not current/owned$"):
                    self.focus.policy.grant(forged, domain="input-A", now_ns=self.clock.ns)
        foreign.release(foreign_lease)
        self.raw.release(lease)
        self.assertEqual(self.events, [])

    def test_stale_nonce_cannot_own_or_release_reacquired_slot(self):
        old = self.take()
        self.raw.release(old)
        current = self.take()
        self.assertNotEqual(old.nonce, current.nonce)
        self.assertFalse(self.raw.owns(old, now_ns=self.clock.ns))
        with self.assertRaisesRegex(TargetLockRefused, "^forged, consumed or foreign lease$"):
            self.raw.release(old)
        self.assertTrue(self.raw.owns(current, now_ns=self.clock.ns))
        self.raw.release(current)
        self.assertEqual(self.events, [])

    def test_copied_actor_lease_cannot_pass_the_full_send_boundary(self):
        prepared = self.prepare()
        object.__setattr__(prepared.context, "actor_lease", replace(prepared.context.actor_lease))
        self.assert_code("E_LOCK", self.fence.send, prepared, self.actuator)
        self.assertEqual(self.events, [])

    def test_in_place_owner_swap_cannot_pass_the_full_send_boundary(self):
        prepared = self.prepare()
        object.__setattr__(prepared.context.actor_lease, "owner", "swapped-owner")
        self.assert_code("E_LOCK", self.fence.send, prepared, self.actuator)
        self.assertEqual(self.events, [])

    def test_foreign_actor_lease_cannot_pass_the_full_send_boundary(self):
        prepared = self.prepare()
        foreign_registry = SessionRegistry(clock=self.clock)
        foreign_handle = foreign_registry.register("capsule", "capsule-A", object(), owned=False)
        foreign = TargetLockRegistry(foreign_registry, clock=self.clock)
        foreign_lease = foreign.acquire(foreign_handle, prepared.request.action_id,
                                        expires_ns=prepared.request.deadline_ns)
        object.__setattr__(prepared.context, "actor_lease", foreign_lease)
        self.assert_code("E_LOCK", self.fence.send, prepared, self.actuator)
        self.assertEqual(self.events, [])
        foreign.release(foreign_lease)

    def test_mint_snapshots_reject_in_place_nonce_owner_handle_generation_expiry_changes(self):
        lease = self.take()
        edits = {
            "nonce": b"not-the-minted-nonce", "owner": "other-owner",
            "handle": self.other, "generation": lease.generation + 1,
            "expires_ns": lease.expires_ns + 1,
        }
        for name, value in edits.items():
            original = getattr(lease, name)
            with self.subTest(field=name):
                object.__setattr__(lease, name, value)
                try:
                    self.assertFalse(self.raw.owns(lease, now_ns=self.clock.ns))
                    with self.assertRaisesRegex(TargetLockRefused, "^forged, consumed or foreign lease$"):
                        self.raw.release(lease)
                finally:
                    object.__setattr__(lease, name, original)
                self.assertTrue(self.raw.owns(lease, now_ns=self.clock.ns))
        self.raw.release(lease)
        self.assertEqual(self.events, [])

    def test_owns_rejects_noninteger_negative_time_and_retired_identity(self):
        lease = self.take()
        for value in (True, -1, 1.0, None):
            with self.subTest(value=value):
                self.assertFalse(self.raw.owns(lease, now_ns=value))
        self.sessions.close(self.capsule)
        self.assertFalse(self.raw.owns(lease, now_ns=self.clock.ns))
        self.raw.release(lease)
        self.assertEqual(self.events, [])

    def test_mint_snapshot_rejects_in_place_nested_handle_mutation(self):
        lease = self.take()
        for name, value in (("canonical_id", "different-id"),
                            ("generation", lease.handle.generation + 1),
                            ("kind", "godot_window")):
            original = getattr(lease.handle, name)
            with self.subTest(field=name):
                object.__setattr__(lease.handle, name, value)
                try:
                    self.assertFalse(self.raw.owns(lease, now_ns=self.clock.ns))
                    with self.assertRaisesRegex(TargetLockRefused, "^forged, consumed or foreign lease$"):
                        self.raw.release(lease)
                finally:
                    object.__setattr__(lease.handle, name, original)
                self.assertTrue(self.raw.owns(lease, now_ns=self.clock.ns))
        self.raw.release(lease)
        self.assertEqual(self.events, [])

    def test_owns_revalidates_after_unlocked_provider_release_reacquire(self):
        lease = self.take()
        new = []

        def replace_claim():
            self.authority.on_grant = None
            self.raw.release(lease)
            new.append(self.take(owner="replacement"))

        self.authority.on_grant = replace_claim
        self.assertFalse(self.raw.owns(lease, now_ns=self.clock.ns))
        self.assertTrue(self.raw.owns(new[0], now_ns=self.clock.ns))
        self.raw.release(new[0])
        self.assertEqual(self.events, [])

    def test_owns_revalidates_after_unlocked_provider_quarantine(self):
        lease = self.take()
        self.raw.mark_inflight(lease)

        def quarantine_claim():
            self.authority.on_grant = None
            self.raw.reconcile_quarantine(self.capsule, acknowledged=False)

        self.authority.on_grant = quarantine_claim
        self.assertFalse(self.raw.owns(lease, now_ns=self.clock.ns))
        self.raw.reconcile_quarantine(self.capsule, acknowledged=True)
        self.assertEqual(self.events, [])

    def test_expired_sent_lease_and_negative_reconciliation_retain_custody(self):
        lease = self.take(deadline=101)
        self.raw.mark_inflight(lease)
        self.clock.ns = 101
        self.assertFalse(self.raw.owns(lease, now_ns=self.clock.ns))
        with self.assertRaisesRegex(TargetLockRefused, "^expired lease is fenced, not released$"):
            self.raw.release(lease)
        self.raw.reconcile_quarantine(self.capsule, acknowledged=False)
        with self.assertRaisesRegex(TargetBusy, "already owned or quarantined"):
            self.raw.acquire(self.screen, "later", expires_ns=20_000)
        self.raw.reconcile_quarantine(self.capsule, acknowledged=True)
        fresh = self.take(self.child)
        self.raw.release(fresh)
        self.assertEqual(self.events, [])

    def test_scheduler_adapter_cannot_turn_sent_release_into_acknowledgement(self):
        lease = self.locks.try_acquire(self.capsule, owner="sent", expires_ns=10_100)
        self.locks.mark_inflight(lease)
        self.assert_code("E_UNCERTAIN", self.locks.release, lease)
        self.assertIsNone(self.locks.try_acquire(self.child, owner="retry", expires_ns=10_100))
        self.locks.acknowledge(lease, acknowledged=True)
        self.locks.release(lease)
        fresh = self.take()
        self.raw.release(fresh)
        self.assertEqual(self.events, [])

    def test_missing_alias_proof_refuses_before_acquisition(self):
        del self.authority.grants[handle_key(self.capsule)]
        with patch.object(self.raw, "acquire", wraps=self.raw.acquire) as acquire:
            self.assert_code("E_LOCK", self.prepare)
            self.assertEqual(acquire.call_count, 0)
        self.assertEqual(self.events, [])

    def test_fabricated_proof_is_not_authentication(self):
        original = self.authority.grants[handle_key(self.capsule)]
        self.authority.grants[handle_key(self.capsule)] = replace(original, proof=object())
        with patch.object(self.raw, "acquire", wraps=self.raw.acquire) as acquire:
            self.assert_code("E_LOCK", self.prepare)
            self.assertEqual(acquire.call_count, 0)
        self.assertEqual(self.events, [])

    def test_foreign_handle_grant_refuses_before_acquisition(self):
        self.authority.grants[handle_key(self.capsule)] = self.authority.grants[handle_key(self.other)]
        with patch.object(self.raw, "acquire", wraps=self.raw.acquire) as acquire:
            self.assert_code("E_LOCK", self.prepare)
            self.assertEqual(acquire.call_count, 0)
        self.assertEqual(self.events, [])

    def test_conflicting_authenticated_resource_refuses_before_acquisition(self):
        self.registry.canonical_resource(self.capsule)
        self.authority.issue(self.capsule, display="a-different-display", domain="different-domain")
        with patch.object(self.raw, "acquire", wraps=self.raw.acquire) as acquire:
            self.assert_code("E_LOCK", self.prepare)
            self.assertEqual(acquire.call_count, 0)
        self.assertEqual(self.events, [])

    def test_conflicting_focus_domains_for_one_display_refuse(self):
        self.registry.canonical_resource(self.capsule)
        self.authority.issue(self.screen, domain="conflicting-input-domain")
        self.authority.focus(self.screen)
        with patch.object(self.raw, "acquire", wraps=self.raw.acquire) as acquire:
            self.assert_code("E_LOCK", self.prepare, self.screen)
            self.assertEqual(acquire.call_count, 0)
        self.assertEqual(self.events, [])

    def test_provider_without_proof_interface_refuses_explicitly(self):
        sessions = SessionRegistry(clock=self.clock)
        self.assert_code("E_LOCK", AuthenticatedRegistryAdapter, sessions, object(), clock=self.clock)
        self.assertEqual(self.events, [])

    def test_resource_and_domain_strings_in_payload_cannot_supply_authority(self):
        del self.authority.grants[handle_key(self.capsule)]
        request = self.request()
        request.payload.update(resource="display-A", domain="input-A", authenticated=True)
        with patch.object(self.raw, "acquire", wraps=self.raw.acquire) as acquire:
            self.assert_code("E_LOCK", self.fence.prepare, request,
                             observation=self.observations[handle_key(self.capsule)],
                             frame_evidence=self.evidence[handle_key(self.capsule)])
            self.assertEqual(acquire.call_count, 0)
        self.assertEqual(self.events, [])

    def test_revoked_proof_between_prepare_and_send_refuses(self):
        prepared = self.prepare()
        self.authority.revoked.add(id(self.authority.grants[handle_key(self.capsule)]))
        self.assert_code("E_LOCK", self.fence.send, prepared, self.actuator)
        self.assertEqual(self.events, [])

    def test_missing_focus_evidence_does_not_fabricate_focus_grant(self):
        self.authority.current_focus.clear()
        with patch.object(self.focus.policy, "set_focus", wraps=self.focus.policy.set_focus) as set_focus:
            self.assert_code("E_FOCUS", self.prepare)
            self.assertEqual(set_focus.call_count, 0)
        self.assertEqual(self.events, [])

    def test_focus_evidence_with_forged_proof_refuses(self):
        current = self.authority.current_focus["input-A"]
        self.authority.current_focus["input-A"] = replace(current, proof=object())
        self.assert_code("E_FOCUS", self.prepare)
        self.assertEqual(self.events, [])

    def test_copied_focus_lease_and_consumed_focus_cannot_authorize(self):
        lease = self.locks.try_acquire(self.capsule, owner="focus-test", expires_ns=10_100)
        focus = self.focus.try_acquire(lease, epoch=0)
        observation = self.observations[handle_key(self.capsule)]
        self.assert_code("E_FOCUS", self.focus.validate, lease, replace(focus),
                         observation, now_ns=self.clock.ns)
        self.focus.validate(lease, focus, observation, now_ns=self.clock.ns)
        self.focus.consume(focus)
        self.assert_code("E_FOCUS", self.focus.validate, lease, focus,
                         observation, now_ns=self.clock.ns)
        self.focus.release(focus)
        self.locks.release(lease)
        self.assertEqual(self.events, [])

    def test_focus_steal_after_prepare_refuses_before_send(self):
        prepared = self.prepare()
        self.authority.focus(self.child)
        self.assert_code("E_FOCUS", self.fence.send, prepared, self.actuator)
        self.assertEqual(self.events, [])

    def test_modal_change_after_prepare_refuses_before_send(self):
        prepared = self.prepare()
        self.focus.policy.set_modal(self.child, domain="input-A")
        self.assert_code("E_FOCUS", self.fence.send, prepared, self.actuator)
        self.assertEqual(self.events, [])

    def test_real_epoch_change_after_prepare_refuses_before_send(self):
        prepared = self.prepare()
        self.sessions.advance_epoch(self.capsule)
        self.assert_code("E_EPOCH", self.fence.send, prepared, self.actuator)
        self.assertEqual(self.events, [])

    def test_wrong_request_epoch_refuses_before_acquisition(self):
        with patch.object(self.raw, "acquire", wraps=self.raw.acquire) as acquire:
            self.assert_code("E_EPOCH", self.prepare, epoch=1)
            self.assertEqual(acquire.call_count, 0)
        self.assertEqual(self.events, [])

    def test_stop_between_prepare_and_send_has_zero_events(self):
        prepared = self.prepare()
        self.fence.stop(self.capsule)
        self.assert_code("E_LOCK", self.fence.send, prepared, self.actuator)
        self.assertEqual(self.events, [])

    def test_native_dispatch_context_rechecks_stop_at_send_boundary(self):
        prepared = self.prepare()
        self.writer.stop(self.capsule)
        with self.assertRaisesRegex(FenceRefused, "^STOP, quarantine, retired permit or absolute deadline$"):
            prepared.context.send(self.actuator, prepared.request)
        self.assertEqual(self.events, [])

    def test_stop_during_unlocked_budget_revalidation_is_seen_by_final_send_fence(self):
        prepared = self.prepare()
        self.budget.on_validate = lambda: self.writer.stop(self.capsule)
        self.assert_code("E_LOCK", self.fence.send, prepared, self.actuator)
        self.assertEqual(self.events, [])

    def test_stop_on_alias_blocks_display_but_other_display_remains_usable(self):
        prepared = self.prepare()
        independent = self.prepare(self.other)
        self.fence.stop(self.child)
        self.assert_code("E_LOCK", self.fence.send, prepared, self.actuator)
        self.assertEqual(self.events, [])
        self.fence.send(independent, self.actuator)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0][1], handle_key(self.other))

    def test_deadline_before_prepare_or_before_send_never_emits(self):
        self.assert_code("E_DEADLINE", self.prepare, deadline=self.clock.ns)
        prepared = self.prepare()
        self.clock.ns = prepared.request.deadline_ns
        self.assert_code("E_DEADLINE", self.fence.send, prepared, self.actuator)
        self.assertEqual(self.events, [])

    def test_budget_refusal_and_revocation_emit_nothing(self):
        self.budget.allowed = False
        self.assert_code("E_BUDGET", self.prepare)
        self.assertEqual(self.events, [])
        self.budget.allowed = True
        prepared = self.prepare()
        self.budget.valid = False
        self.assert_code("E_BUDGET", self.fence.send, prepared, self.actuator)
        self.assertEqual(self.events, [])

    def test_missing_frame_witness_and_bad_revision_refuse(self):
        request = self.request()
        self.assert_code("E_STALE_FRAME", self.fence.prepare, request,
                         observation=self.observations[handle_key(self.capsule)],
                         frame_evidence=object())
        prepared = self.prepare()
        self.frame_provider.revision = "b" * 40
        self.assert_code("E_STALE_FRAME", self.fence.send, prepared, self.actuator)
        self.assertEqual(self.events, [])

    def test_frame_provider_must_have_pin_and_independent_accept_reject_vectors(self):
        accepted, rejected = self.frame_provider.vectors()
        for revision, positive, negative in (
            ("", accepted, rejected),
            ("branch/integration", accepted, rejected),
            ("b" * 40, accepted, rejected),
            (TARGET_REVISION, rejected, accepted),
            (TARGET_REVISION, accepted, accepted),
        ):
            with self.subTest(revision=revision, same_vectors=positive == negative):
                self.assert_code("E_STALE_FRAME", RevisionPinnedFrames,
                                 self.frame_provider, revision=revision,
                                 accepted_vector=positive, rejected_vector=negative)
        self.assert_code("E_STALE_FRAME", RevisionPinnedFrames, object(),
                         revision=TARGET_REVISION, accepted_vector=accepted,
                         rejected_vector=rejected)
        self.assertEqual(self.events, [])

    def test_rejected_vector_cannot_be_optimistically_accepted(self):
        accepted, rejected = self.frame_provider.vectors()
        with patch.object(self.frame_provider, "verify_vector", return_value=True):
            self.assert_code("E_STALE_FRAME", RevisionPinnedFrames,
                             self.frame_provider, revision=TARGET_REVISION,
                             accepted_vector=accepted, rejected_vector=rejected)
        self.assertEqual(self.events, [])

    def test_frame_proof_revocation_after_prepare_refuses(self):
        prepared = self.prepare()
        self.frame_provider.allowed = False
        self.assert_code("E_STALE_FRAME", self.fence.send, prepared, self.actuator)
        self.assertEqual(self.events, [])

    def test_lost_ack_quarantines_no_new_id_retry_expiry_not_release(self):
        prepared = self.prepare()
        self.assert_code("E_UNCERTAIN", self.fence.send, prepared, self.lost_ack)
        self.assertEqual(len(self.events), 1)
        self.assert_code("E_UNCERTAIN", self.fence.send, prepared, self.actuator)
        self.authority.focus(self.screen)
        self.assert_code("E_LOCK", self.prepare, self.screen, action_id="new-id-retry")
        self.assertEqual(len(self.events), 1)
        self.clock.ns = prepared.request.deadline_ns + 1
        self.assertFalse(self.raw.owns(prepared.context.actor_lease, now_ns=self.clock.ns))
        with self.assertRaisesRegex(TargetLockRefused, "^expired lease is fenced, not released$"):
            self.raw.release(prepared.context.actor_lease)
        self.assert_code("E_UNCERTAIN", self.fence.reconcile, prepared, acknowledged=False)
        self.assertIsNone(self.locks.try_acquire(self.child, owner="still-held", expires_ns=self.clock.ns + 1000))
        self.fence.reconcile(prepared, acknowledged=True)
        second = self.prepare(self.screen)
        self.fence.send(second, self.actuator)
        self.assertEqual(len(self.events), 2)

    def test_unknown_completion_cannot_claim_effect_even_if_flag_is_true(self):
        prepared = self.prepare()

        def unknown(request):
            self.actuator(request)
            return Completion(request.action_id, request.handle, "UNKNOWN", True)

        self.assert_code("E_UNCERTAIN", self.fence.send, prepared, unknown)
        self.assertEqual(len(self.events), 1)
        self.fence.reconcile(prepared, acknowledged=True)

    def test_foreign_terminal_ack_and_callback_error_preserve_quarantine(self):
        prepared = self.prepare()

        def foreign(request):
            self.actuator(request)
            return Completion("different-action", request.handle, "SUCCEEDED", True)

        self.assert_code("E_UNCERTAIN", self.fence.send, prepared, foreign)
        self.assertEqual(len(self.events), 1)
        self.fence.reconcile(prepared, acknowledged=True)
        second = self.prepare()

        def failure(request):
            self.actuator(request)
            raise RuntimeError("in-memory acknowledgement channel closed")

        self.assert_code("E_UNCERTAIN", self.fence.send, second, failure)
        self.assertEqual(len(self.events), 2)
        self.fence.reconcile(second, acknowledged=True)

    def test_failed_terminal_ack_is_not_relabelled_as_success(self):
        prepared = self.prepare()

        def failed(request):
            self.actuator(request)
            return Completion(request.action_id, request.handle, "FAILED", True)

        completion = self.fence.send(prepared, failed)
        self.assertEqual(completion.status, "FAILED")
        self.assertEqual(len(self.events), 1)

    def test_copied_preparation_cannot_send_but_original_remains_usable(self):
        prepared = self.prepare()
        self.assert_code("E_LOCK", self.fence.send, replace(prepared), self.actuator)
        self.assertEqual(self.events, [])
        self.fence.send(prepared, self.actuator)
        self.assertEqual(len(self.events), 1)

    def test_prepared_capability_replacements_refuse_without_consuming_original(self):
        prepared = self.prepare()
        replacements = {
            "observation": replace(prepared.observation),
            "frame_evidence": object(),
            "resource": self.registry.canonical_resource(self.other),
            "reservation": object(),
        }
        for name, replacement in replacements.items():
            original = getattr(prepared, name)
            with self.subTest(field=name):
                object.__setattr__(prepared, name, replacement)
                try:
                    self.assert_code("E_LOCK", self.fence.send, prepared, self.actuator)
                    self.assertEqual(self.events, [])
                finally:
                    object.__setattr__(prepared, name, original)
        original_clock = prepared.context._clock
        object.__setattr__(prepared.context, "_clock", lambda: 0)
        try:
            self.assert_code("E_LOCK", self.fence.send, prepared, self.actuator)
            self.assertEqual(self.events, [])
        finally:
            object.__setattr__(prepared.context, "_clock", original_clock)
        self.assertTrue(self.raw.owns(prepared.context.actor_lease, now_ns=self.clock.ns))
        self.fence.send(prepared, self.actuator)
        self.assertEqual(len(self.events), 1)

    def test_stale_adapter_ack_or_quarantine_cannot_touch_newer_sent_claim(self):
        old = self.locks.try_acquire(self.capsule, owner="old", expires_ns=10_100)
        self.locks.mark_inflight(old)
        # A separate trusted reconciler has already retired this exact mint.
        self.raw.reconcile_quarantine(self.capsule, acknowledged=True)
        current = self.locks.try_acquire(self.capsule, owner="current", expires_ns=10_100)
        self.locks.mark_inflight(current)

        def stale_ack_and_release():
            self.locks.acknowledge(old, acknowledged=True)
            self.locks.release(old)

        self.assert_code("E_LOCK", stale_ack_and_release)
        self.assertTrue(self.raw.owns(current, now_ns=self.clock.ns))
        self.assert_code("E_LOCK", self.locks.quarantine, old)
        self.assertTrue(self.raw.owns(current, now_ns=self.clock.ns))
        with self.assertRaisesRegex(TargetBusy, "already owned or quarantined"):
            self.take(self.child)
        self.locks.acknowledge(current, acknowledged=True)
        self.locks.release(current)
        self.assertEqual(self.events, [])

    def test_raw_expected_lease_reconciliation_rejects_stale_and_copied_mints(self):
        old = self.take()
        self.raw.mark_inflight(old)
        self.raw.reconcile_quarantine(self.capsule, acknowledged=True, expected_lease=old)
        current = self.take()
        self.raw.mark_inflight(current)
        for bad in (old, replace(current)):
            for acknowledged in (False, True):
                with self.subTest(stale=bad is old, acknowledged=acknowledged):
                    with self.assertRaisesRegex(TargetLockRefused, "^forged, consumed or foreign lease$"):
                        self.raw.reconcile_quarantine(
                            self.capsule, acknowledged=acknowledged, expected_lease=bad)
                    self.assertTrue(self.raw.owns(current, now_ns=self.clock.ns))
        self.raw.reconcile_quarantine(self.capsule, acknowledged=True, expected_lease=current)
        self.assertEqual(self.events, [])

    def test_acknowledged_recovery_resumes_cleanup_without_releasing_focus_twice(self):
        prepared = self.prepare()
        with patch.object(self.focus, "release", wraps=self.focus.release) as release_focus:
            with patch.object(self.budget, "settle", wraps=self.budget.settle) as settle:
                with patch.object(self.locks, "release",
                                  side_effect=FenceError("E_LOCK", "transient release failure")):
                    self.assert_code("E_UNCERTAIN", self.fence.send, prepared, self.actuator)
                self.assertEqual(len(self.events), 1)
                self.fence.reconcile(prepared, acknowledged=True)
                self.assertEqual(release_focus.call_count, 1)
                self.assertEqual(settle.call_count, 1)
        self.assertFalse(self.raw.owns(prepared.context.actor_lease, now_ns=self.clock.ns))
        self.assertIsNone(self.writer.reason(self.capsule, prepared.resource))
        self.assert_code("E_UNCERTAIN", self.fence.send, prepared, self.actuator)
        self.assertEqual(len(self.events), 1)
        fresh = self.take(self.child)
        self.raw.release(fresh)

    def test_quarantine_cleanup_failure_keeps_original_uncertainty_recoverable(self):
        prepared = self.prepare()
        with patch.object(self.locks, "quarantine",
                          side_effect=FenceError("E_LOCK", "transient quarantine failure")):
            original = self.assert_code("E_UNCERTAIN", self.fence.send, prepared, self.lost_ack)
        repeated = self.assert_code("E_UNCERTAIN", self.fence.send, prepared, self.actuator)
        self.assertIs(repeated, original)
        self.assertEqual(len(self.events), 1)
        self.fence.reconcile(prepared, acknowledged=True)
        self.assertFalse(self.raw.owns(prepared.context.actor_lease, now_ns=self.clock.ns))
        self.assertIsNone(self.writer.reason(self.capsule, prepared.resource))
        fresh = self.take(self.screen)
        self.raw.release(fresh)
        self.assertEqual(len(self.events), 1)

    def test_unminted_lease_properties_never_run_under_metadata_lock(self):
        property_calls = []
        gate = self

        class HostileLease:
            @property
            def nonce(self):
                gate.probe("hostile.nonce")
                property_calls.append("nonce")
                return b"forged"

        hostile = HostileLease()
        self.assertFalse(self.raw.owns(hostile, now_ns=self.clock.ns))
        with self.assertRaisesRegex(TargetLockRefused, "^forged, consumed or foreign lease$"):
            self.raw.release(hostile)
        self.assertEqual(property_calls, [])
        self.assertEqual(self.events, [])

    def test_minted_lease_with_hostile_handle_or_nonce_never_calls_properties(self):
        lease = self.take()
        calls = []
        gate = self

        class HostileHandle:
            @property
            def kind(self):
                gate.probe("hostile.kind")
                calls.append("kind")
                return "capsule"

        class HostileNonce(bytes):
            def __hash__(self):
                gate.probe("hostile.hash")
                calls.append("hash")
                return bytes.__hash__(self)

        original_handle, original_nonce = lease.handle, lease.nonce
        object.__setattr__(lease, "handle", HostileHandle())
        try:
            self.assertFalse(self.raw.owns(lease, now_ns=self.clock.ns))
            with self.assertRaisesRegex(TargetLockRefused, "^forged, consumed or foreign lease$"):
                self.raw.release(lease)
        finally:
            object.__setattr__(lease, "handle", original_handle)
        object.__setattr__(lease, "nonce", HostileNonce(original_nonce))
        try:
            self.assertFalse(self.raw.owns(lease, now_ns=self.clock.ns))
            with self.assertRaisesRegex(TargetLockRefused, "^missing or invalid issued nonce$"):
                self.raw.release(lease)
        finally:
            object.__setattr__(lease, "nonce", original_nonce)
        self.assertEqual(calls, [])
        self.assertTrue(self.raw.owns(lease, now_ns=self.clock.ns))
        self.raw.release(lease)
        self.assertEqual(self.events, [])

    def test_reentrant_reconciliation_cannot_settle_or_release_twice(self):
        prepared = self.prepare()
        self.assert_code("E_UNCERTAIN", self.fence.send, prepared, self.lost_ack)
        self.assertEqual(len(self.events), 1)
        original_settle = self.budget.settle
        reentrant_errors = []

        def reenter(reservation, *, consumed):
            reentrant_errors.append(self.assert_code(
                "E_LOCK", self.fence.reconcile, prepared, acknowledged=True))
            original_settle(reservation, consumed=consumed)

        with patch.object(self.budget, "settle", side_effect=reenter) as settle:
            self.fence.reconcile(prepared, acknowledged=True)
            self.assertEqual(settle.call_count, 1)
        self.assertEqual(len(reentrant_errors), 1)
        self.assertEqual(self.budget.settlements, [(prepared.reservation, True)])
        self.assertFalse(self.raw.owns(prepared.context.actor_lease, now_ns=self.clock.ns))
        fresh = self.take(self.child)
        self.raw.release(fresh)
        self.assertEqual(len(self.events), 1)

    def test_completed_prepare_replay_after_deadline_returns_cached_one_event(self):
        request = self.request()
        observation = self.observations[handle_key(self.capsule)]
        evidence = self.evidence[handle_key(self.capsule)]
        prepared = self.fence.prepare(request, observation=observation, frame_evidence=evidence)
        completion = self.fence.send(prepared, self.actuator)
        self.clock.ns = request.deadline_ns + 1
        replay = self.fence.prepare(replace(request), observation=observation, frame_evidence=evidence)
        self.assertIs(replay, prepared)
        self.assertIs(self.fence.send(replay, self.actuator), completion)
        self.assertEqual(len(self.events), 1)

    def test_terminal_ack_crossing_deadline_stays_uncertain_without_second_event(self):
        prepared = self.prepare()

        def late_ack(request):
            completion = self.actuator(request)
            self.clock.ns = request.deadline_ns
            return completion

        self.assert_code("E_UNCERTAIN", self.fence.send, prepared, late_ack)
        self.assertEqual(len(self.events), 1)
        self.assert_code("E_UNCERTAIN", self.fence.send, prepared, self.actuator)
        self.fence.reconcile(prepared, acknowledged=True)
        fresh = self.raw.acquire(self.child, "after-quiescence", expires_ns=self.clock.ns + 1000)
        self.raw.release(fresh)
        self.assertEqual(len(self.events), 1)

    def test_terminal_ack_crossing_stop_stays_uncertain_without_second_event(self):
        prepared = self.prepare()

        def stopped_ack(request):
            completion = self.actuator(request)
            self.writer.stop(request.handle)
            return completion

        self.assert_code("E_UNCERTAIN", self.fence.send, prepared, stopped_ack)
        self.assertEqual(len(self.events), 1)
        self.assert_code("E_UNCERTAIN", self.fence.send, prepared, self.actuator)
        self.fence.reconcile(prepared, acknowledged=True)
        self.assertEqual(self.writer.reason(self.capsule, prepared.resource), "STOPPED")
        independent = self.prepare(self.other)
        self.fence.send(independent, self.actuator)
        self.assertEqual(len(self.events), 2)
        self.assertEqual(self.events[-1][1], handle_key(self.other))

    def test_all_external_callbacks_are_outside_metadata_locks(self):
        prepared = self.prepare()
        self.fence.send(prepared, self.actuator)
        required = {"clock", "grant_for", "verify_grant", "focus_for", "verify_focus",
                    "verify_vector", "frame.verify", "budget.reserve", "budget.validate",
                    "budget.settle", "actuator"}
        self.assertTrue(required.issubset(self.probe.calls))
        self.assertEqual(len(self.events), 1)

    def test_real_window_scheduler_composes_explicit_lock_and_focus_adapters(self):
        gate = self

        class Chain:
            def is_settled(self, handle, epoch):
                gate.probe("chain.is_settled")
                return gate.sessions.resolve(handle).epoch == epoch

        class Dispatch:
            offline = True
            writer_fence = gate.writer
            prerequisite_gates = {"identity_streams": True, "ownership_perception": True,
                                  "readiness": True, "writer_fence": True}

            def __call__(self, request, context):
                gate.probe("scheduler.dispatch")
                observation = request.payload["observation"]
                gate.focus.validate(context.actor_lease, context.focus_lease,
                                    observation, now_ns=gate.clock.ns)
                gate.focus.consume(context.focus_lease)
                gate.locks.mark_inflight(context.actor_lease)
                result = context.send(gate.actuator, request)
                gate.locks.acknowledge(context.actor_lease, acknowledged=result.acknowledged)
                return result

        scheduler = WindowScheduler(
            registry=self.registry, locks=self.locks, focus_policy=self.focus,
            budget=self.budget, chain=Chain(), writer_fence=self.writer,
            dispatch=Dispatch(), clock=self.clock, max_queue=4, max_targets=2,
            service_cap_ns=10)
        request = self.request()
        request.payload["observation"] = self.observations[handle_key(self.capsule)]
        ticket = scheduler.submit(request)
        self.assertIsInstance(ticket, Ticket)
        receipts = scheduler.step(self.clock.ns)
        self.assertEqual([(r.action_id, r.status) for r in receipts], [(request.action_id, "SUCCEEDED")])
        self.assertEqual(scheduler.diagnostics, [])
        self.assertEqual(len(self.events), 1)
        fresh = self.take(self.child)
        self.raw.release(fresh)


if __name__ == "__main__":
    unittest.main()
