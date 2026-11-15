# @module {"id":"wa.perceive.proto1_dead_targets","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.session_registry","wa.perceive.harvest_broker","wa.act.target_lock_registry"],"book":"HB-WA-s1-perceive"}
"""Fail-closed composition of registry STOP, broker teardown, and external quiescence.

This module does not own a real display or network connection.  A trusted,
*injected* external channel establishes joined transport termination (or an
acknowledged owned-session detachment) after target-scoped teardown.  A closed
TargetRecord alone is never treated as a termination acknowledgement.

The composer exclusively owns registrations and command admission for its
registry/broker pair.  Direct calls to the broker or registry outside this
composer invalidate its lane-fencing guarantees. The separate closer completes
teardown; broker metadata follows the acknowledged registry tombstone without
transport I/O or mutation of broker private internals.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
import threading
import time
from uuid import uuid4
from typing import Any, Callable

from wender.perceive.session_registry import (
    Handle, Receipt, SessionRegistry, StaleHandleError, RegistryError,
)
from wender.perceive.harvest_broker import HarvestBroker, StoppedError
from wender.act.target_lock_registry import TargetLockRegistry, TargetLockRefused


class LifecycleRefused(RuntimeError):
    """Transition lacks a current identity, lease, or positive quiescence."""


class ProductionDefect(LifecycleRefused):
    """An unchanged producer prevents a contractually safe transition."""


@dataclass(frozen=True)
class CommandReply:
    """RAM-only reply snapshot; it contains no handle to the external transport."""
    handle: Handle
    epoch: int
    sequence: int
    payload: object
    _seal: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class TerminationProof:
    """Composer-issued proof after a trusted external join, not a caller assertion."""
    handle: Handle
    _seal: object = field(repr=False, compare=False)


# @anchor:wa.perceive.proto1_dead_targets.ClosureReceipt
@dataclass(frozen=True)
class ClosureReceipt:
    """Completed teardown calls; external join is still required for reclamation."""
    handle: Handle
    generation: int
    actions_done: tuple[str, ...]
    final_state: str
    status: str = "closed"
    receipt_id: str = field(default_factory=lambda: uuid4().hex)


# @anchor:wa.perceive.proto1_dead_targets.PostStopCloser
class PostStopCloser:
    """Separate cleanup owner; never submits commands through the stopped broker.

    The lifecycle calls this only while holding its drained send/publication
    gates. Receipts are retained by exact generation. Failed attempts are fenced
    so teardown is never replayed after an uncertain call.
    Registry quarantine remains until the lifecycle verifies an external join.
    """

    def __init__(self, registry: SessionRegistry) -> None:
        self.registry = registry
        self._receipts: dict[Handle, ClosureReceipt] = {}
        self._started: set[Handle] = set()
        self._gate = threading.Lock()

    def close(self, record) -> ClosureReceipt:
        with self._gate:
            retired, _, pending = self.registry.tombstone(record.handle)
            if (not retired.closed or not pending or
                    retired.handle != record.handle or
                    retired.transport is not record.transport or
                    retired.owned is not record.owned or
                    retired.session_id != record.session_id or
                    retired.attachment_owned is not record.attachment_owned):
                raise LifecycleRefused("closer requires the exact quarantined STOP generation")
            prior = self._receipts.get(record.handle)
            if prior is not None:
                return prior
            if record.handle in self._started:
                raise LifecycleRefused("uncertain teardown must not be repeated")
            self._started.add(record.handle)
            transport = record.transport
            actions = []
            if record.attachment_owned and record.session_id is not None:
                detacher = getattr(transport, "detach_session", None)
                if not callable(detacher):
                    detacher = getattr(transport, "detach", None)
                if callable(detacher):
                    detacher(record.session_id)
                elif record.handle.kind == "cdp_tab" and callable(getattr(transport, "send", None)):
                    transport.send("Target.detachFromTarget", {"sessionId": record.session_id})
                else:
                    raise LifecycleRefused("target-scoped session detacher unavailable")
                actions.append("detach_session")
            if record.owned:
                closer = getattr(transport, "close_target", None)
                if callable(closer):
                    closer(record.handle.canonical_id)
                elif record.handle.kind == "cdp_tab" and callable(getattr(transport, "send", None)):
                    transport.send("Target.closeTarget", {"targetId": record.handle.canonical_id})
                else:
                    raise LifecycleRefused("target-scoped closer unavailable")
                actions.append("close_target")
            final_state = ("target_closed" if record.owned else
                           "session_detached" if record.attachment_owned else
                           "caller_owned_target_preserved")
            receipt = ClosureReceipt(record.handle, record.handle.generation,
                                     tuple(actions), final_state)
            self._receipts[record.handle] = receipt
            return receipt


@dataclass
class _Target:
    handle: Handle
    transport: object
    owned: bool
    session_id: str | None
    attachment_owned: bool
    phase: str = "active"  # Composer transition only; registry.closed is authority.
    lease: object | None = None
    lease_sent: bool = False
    unsent_release_failed: bool = False
    seq: int = 0
    close_receipt: object | None = None
    proof: TerminationProof | None = None
    pending: dict[int, tuple] = field(default_factory=dict)
    send_gate: threading.Lock = field(default_factory=threading.Lock, repr=False)
    publish_gate: threading.Lock = field(default_factory=threading.Lock, repr=False)


class DeadTargetLifecycle:
    """Compose real registry, real broker, real target leases, and trusted joins.

    Required channel contract (only trust a channel supplied by the caller's
    security boundary):

    * joined_target(handle, transport) -> True only after actual target-scoped
      termination has completed and all transport I/O has joined.
    * joined_detachment(handle, transport, session_id) -> True only after the
      owned session was detached; the underlying caller-owned target stays open.
    * joined_quiescence(handle, transport) -> True only after outstanding I/O
      on an unowned, unattached target has quiesced.

    Channel booleans never come directly from an untrusted acknowledgement
    message.  Proofs are minted and tracked by object identity inside here.
    """

    def __init__(self, registry: SessionRegistry, broker: HarvestBroker,
                 locks: TargetLockRegistry, channel: object,
                 *, clock: Callable[[], int] | None = None) -> None:
        if not isinstance(registry, SessionRegistry):
            raise TypeError("real SessionRegistry required")
        if not isinstance(broker, HarvestBroker) or broker.registry is not registry:
            raise TypeError("real HarvestBroker must use the same registry")
        if not isinstance(locks, TargetLockRegistry) or locks.registry is not registry:
            raise TypeError("real TargetLockRegistry must use the same registry")
        if channel is None:
            raise TypeError("trusted external termination channel required")
        self.registry = registry
        self.broker = broker
        self.locks = locks
        self.channel = channel
        self._closer = PostStopCloser(registry)
        self._clock = time.monotonic_ns if clock is None else clock
        if not callable(self._clock):
            raise TypeError("clock must be callable")
        self._mutex = threading.RLock()
        self._targets: dict[Handle, _Target] = {}
        self._current: dict[tuple[str, str], Handle] = {}
        self._poisoned = False

    def _now(self) -> int:
        n = self._clock()
        if type(n) is not int or n < 0:
            raise LifecycleRefused("monotonic_ns clock must return a nonnegative integer")
        return n

    @staticmethod
    def _handle(value: object) -> Handle:
        """Normalize structural handles; the real Handle enforces generation >= 1."""
        try:
            return Handle(value.kind, value.canonical_id, value.generation)
        except AttributeError as exc:
            raise LifecycleRefused("handle must expose kind, canonical_id, generation") from exc

    def _target(self, handle: object) -> _Target:
        h = self._handle(handle)
        target = self._targets.get(h)
        if target is None:
            raise LifecycleRefused("unknown exact target generation")
        return target

    def _active_record(self, target: _Target):
        if target.phase != "active":
            raise LifecycleRefused("target generation is retired")
        record = self.registry.resolve(target.handle)
        if record.closed or record.handle != target.handle or record.transport is not target.transport:
            raise LifecycleRefused("registry no longer owns target identity")
        if self._current.get((target.handle.kind, target.handle.canonical_id)) != target.handle:
            raise LifecycleRefused("a newer generation owns the alias")
        return record

    def register(self, kind: str, canonical_id: str, transport: object, *,
                 owned: bool, session_id: str | None = None,
                 attachment_owned: bool = False,
                 successor_handoff: ClosureReceipt | None = None) -> Handle:
        """Issue identity at the *real* registry, then require broker parity.

        Ordinary re-registration creates metadata only and preserves STOP.
        To activate a successor, explicitly pass the predecessor's exact issued
        closure receipt as successor_handoff after cleanup acknowledgement.
        """
        if type(owned) is not bool or type(attachment_owned) is not bool:
            raise TypeError("ownership flags must be bool")
        if session_id is None and attachment_owned:
            raise LifecycleRefused("attachment ownership needs a real session id")
        if session_id is not None and (not isinstance(session_id, str) or not session_id.strip()):
            raise LifecycleRefused("session_id must be a nonblank string")
        with self._mutex:
            if self._poisoned:
                raise ProductionDefect("broker/registry identities were previously mismatched")
            old = self._current.get((kind, canonical_id))
            if old is not None and self._targets[old].phase != "reclaimed":
                raise LifecycleRefused("target already live or quarantined")
            handoff = None
            if successor_handoff is not None:
                if (old is None or successor_handoff is not self._targets[old].close_receipt or
                        successor_handoff.handle != old or
                        successor_handoff.generation != old.generation):
                    raise ProductionDefect("successor requires the exact issued predecessor closure receipt")
                handoff = (old, old.generation, successor_handoff.receipt_id)
            h = self.registry.register(kind, canonical_id, transport, owned=owned)
            if session_id is not None:
                self.registry.bind_attachment(h, session_id, owned=attachment_owned)
            bh = self.broker.register(kind, canonical_id, transport, owned=owned,
                                      session_id=session_id, attachment_owned=attachment_owned,
                                      successor_handoff=handoff)
            bound = self.broker.get_target_record(bh)
            if (bh != h or bound is None or bound.handle != h or
                bound.transport is not transport or bound.owned is not owned or
                bound.session_id != session_id or
                bound.attachment_owned is not attachment_owned):
                self._poisoned = True
                # Do not acknowledge or reclaim a half-bound generation.
                self.registry.stop(h)
                self.broker.stop(bh)
                raise ProductionDefect("broker and real registry did not issue the same identity/ownership")
            self._targets[h] = _Target(h, transport, owned, session_id, attachment_owned)
            self._current[(kind, canonical_id)] = h
            return h

    def acquire_actor(self, handle: object, owner: str, *, expires_ns: int) -> object:
        """Reserve exactly one nonce-fenced actor for an active generation."""
        with self._mutex:
            target = self._target(handle)
            self._active_record(target)
            if target.lease is not None:
                raise LifecycleRefused("target has an outstanding actor lease")
            lease = self.locks.acquire(target.handle, owner, expires_ns=expires_ns,
                                       now_ns=self._now())
            target.lease = lease
            return lease

    def issue(self, handle: object, lease: object, method: str,
              params: dict | None = None) -> CommandReply:
        """Send once through the real bounded broker lane; never publish here.

        The send gate runs through completion of the transport I/O.  STOP first
        fences admission, then waits for this gate, so it cannot return while
        a post-STOP send could still start.  The issued, detached reply may be
        delivered later; a generation/epoch check will reject it after a rebind.
        """
        with self._mutex:
            target = self._target(handle)
        with target.send_gate:
            with self._mutex:
                record = self._active_record(target)
                if target.lease is not lease or lease.handle != target.handle:
                    raise LifecycleRefused("missing or foreign exact-generation actor")
                if target.lease_sent:
                    raise LifecycleRefused("actor lease is already sent/quarantined")
                if not self.locks.owns(lease, now_ns=self._now()):
                    raise LifecycleRefused("expired/fenced lease cannot authorize send")
                self.locks.mark_inflight(lease)
                target.lease_sent = True
                target.seq += 1
                sequence, epoch = target.seq, record.epoch
            try:
                reply = self.broker.command(target.handle, method, params)
            except StoppedError as exc:
                # A new generation may encounter the broker's stale
                # canonical-id STOP tombstone (unowned production defect).
                raise ProductionDefect("broker fenced a live registry generation") from exc
            if isinstance(reply, dict) and reply.get("status") in (
                    "discarded_late_reply", "discarded_epoch_mismatch"):
                raise LifecycleRefused("broker discarded an obsolete reply")
            with self._mutex:
                self._active_record(target)
                token = CommandReply(target.handle, epoch, sequence,
                                     deepcopy(reply), object())
                # Keep an independently owned issuance snapshot.  A frozen
                # dataclass can still be tampered with through object.__setattr__
                # or via a mutable payload reference supplied to its caller.
                # The published value must come from this separate snapshot.
                target.pending[id(token)] = (
                    token, target.handle, epoch, sequence,
                    deepcopy(token.payload), token._seal,
                )
                return token

    def publish(self, reply: CommandReply, callback: Callable[[object], Any] | None = None) -> object:
        """Deliver only an internally issued reply for the *live* incarnation."""
        if not isinstance(reply, CommandReply):
            raise LifecycleRefused("forged/unissued command result")
        with self._mutex:
            target = self._target(reply.handle)
        with target.publish_gate:
            with self._mutex:
                issued = target.pending.get(id(reply))
                if issued is None or issued[0] is not reply:
                    raise LifecycleRefused("stale, replayed or forged reply")
                _, bound_handle, bound_epoch, bound_seq, payload, seal = issued
                if (reply.handle != bound_handle or reply.epoch != bound_epoch
                        or reply.sequence != bound_seq or reply._seal is not seal
                        or reply.payload != payload):
                    del target.pending[id(reply)]
                    raise LifecycleRefused("issued reply was tampered with")
                record = self._active_record(target)
                if bound_epoch != record.epoch:
                    del target.pending[id(reply)]
                    raise LifecycleRefused("reply belongs to an obsolete navigation epoch")
                del target.pending[id(reply)]
            safe_payload = deepcopy(payload)
            if callback is not None:
                callback(safe_payload)
            return safe_payload

    def command(self, handle: object, lease: object, method: str,
                params: dict | None = None, *,
                publisher: Callable[[object], Any] | None = None) -> object:
        """Combined issue/publication for consumers not deliberately delaying replies."""
        reply = self.issue(handle, lease, method, params)
        return self.publish(reply, publisher)

    def capture(self, handle: object, frame_seq: int, rgb: object, *, epoch: int):
        """Admit RGB24 through the real registry, preserving its RAM-owned hash.

        No frame bytes or encoded frame are logged or written to disk.
        """
        with self._mutex:
            target = self._target(handle)
        with target.send_gate:
            with self._mutex:
                record = self._active_record(target)
                if type(epoch) is not int or record.epoch != epoch:
                    raise LifecycleRefused("explicit capture epoch mismatches the live record")
            frame = self.registry.admit_frame(target.handle, frame_seq, rgb, epoch=epoch)
            with self._mutex:
                self._active_record(target)
            return frame

    def observe(self, handle: object, frame: object, *, state: dict, dpr_ppm: int):
        """Produce a source-bound observation; never fabricate a settled state."""
        with self._mutex:
            target = self._target(handle)
        with target.send_gate:
            with self._mutex:
                self._active_record(target)
            observation = self.registry.admit_observation(target.handle, frame,
                                                          state=state, dpr_ppm=dpr_ppm)
            with self._mutex:
                self._active_record(target)
            return observation

    def advance_epoch(self, handle: object, *, validated: bool) -> int:
        if validated is not True:
            raise LifecycleRefused("navigation requires independent positive validation")
        with self._mutex:
            target = self._target(handle)
        with target.send_gate:
            with self._mutex:
                self._active_record(target)
                # The producer broker begins epochs at 1, registry at 0. Their
                # values are not interchangeable; only registry epoch is public.
                epoch = self.registry.advance_epoch(target.handle)
                self.broker.advance_epoch(target.handle)
                target.pending.clear()
                return epoch

    def stop(self, handle: object):
        """Retire locally, await accepted sends, then invoke both real STOP APIs.

        No receipt, deadline, expired actor lease or external ACK is sufficient
        to remove quarantine.  Nothing calls transport.close here.
        """
        with self._mutex:
            target = self._target(handle)
            if target.phase != "active":
                raise LifecycleRefused("stop is only valid once for an active generation")
            self._active_record(target)
            target.phase = "retiring"
        with target.send_gate, target.publish_gate:
            with self._mutex:
                target.pending.clear()
                if target.lease is not None and not target.lease_sent:
                    try:
                        self.locks.release(target.lease)
                        target.lease = None
                    except TargetLockRefused:
                        # An expired unsent claim is a producer limitation. It
                        # must remain quarantined rather than be silently lost.
                        target.unsent_release_failed = True
            self.broker.stop(target.handle)
            receipt = self.registry.stop(target.handle)
            if receipt.status not in ("stopped", "already_closed") or receipt.handle != target.handle:
                raise ProductionDefect("registry did not retire the expected live generation")
            try:
                retired, _, _ = self.registry.tombstone(target.handle)
            except StaleHandleError as exc:
                raise ProductionDefect("registry did not retain the expected tombstone") from exc
            if retired.handle != target.handle or not retired.closed:
                raise ProductionDefect("registry tombstone does not match the retired generation")
            with self._mutex:
                target.phase = "stopped"
            # The broker may already have retired our shared registry generation.
            return Receipt("stop", target.handle, "stopped", "transport cleanup deferred")

    def close(self, handle: object):
        """Once-only ownership-aware external teardown, after registry STOP."""
        with self._mutex:
            target = self._target(handle)
            if target.phase == "closed":
                return target.close_receipt
            if target.phase != "stopped":
                raise LifecycleRefused("close requires STOP and lane quiescence")
            target.phase = "closing"
        with target.send_gate, target.publish_gate:
            record = self.broker.get_target_record(target.handle)
            if record is None or record.handle != target.handle:
                raise ProductionDefect("broker lost the stopped generation")
            receipt = self._closer.close(record)
            with self._mutex:
                target.close_receipt = receipt
                target.phase = "closed" if receipt.status == "closed" else "close_failed"
                return receipt

    def prove_termination(self, handle: object) -> TerminationProof:
        """Call a trusted *joined* external channel after close, never a timer."""
        with self._mutex:
            target = self._target(handle)
            if target.phase != "closed":
                raise LifecycleRefused("successful target-scoped close is prerequisite")
            if target.unsent_release_failed:
                raise ProductionDefect("expired unsent producer claim cannot be safely cleared")
            record, _at, pending = self.registry.tombstone(target.handle)
            if record.handle != target.handle or not record.closed or not pending:
                raise LifecycleRefused("expected quarantined generation not present")
            brec = self.broker.get_target_record(target.handle)
            if (brec is None or brec.handle != target.handle or
                    brec.transport is not target.transport or
                    target.close_receipt is None or
                    target.close_receipt.handle != target.handle or
                    target.close_receipt.generation != target.handle.generation or
                    target.close_receipt.status != "closed"):
                raise LifecycleRefused("closer did not finish target-scoped close")
            if target.proof is not None:
                return target.proof
            if target.owned:
                name, args = "joined_target", (target.handle, target.transport)
            elif target.attachment_owned and target.session_id is not None:
                name, args = "joined_detachment", (target.handle, target.transport, target.session_id)
            else:
                name, args = "joined_quiescence", (target.handle, target.transport)
        # A channel method is part of the trusted integration contract; its
        # absence or non-True acknowledgement is NOT proof of termination.
        method = getattr(self.channel, name, None)
        if not callable(method) or method(*args) is not True:
            raise LifecycleRefused("joined external termination/detachment not established")
        with self._mutex:
            if target.phase != "closed" or target.proof is not None:
                if target.proof is not None:
                    return target.proof
                raise LifecycleRefused("target state changed during external join")
            proof = TerminationProof(target.handle, object())
            target.proof = proof
            return proof

    def acknowledge_cleanup(self, handle: object, proof: TerminationProof) -> None:
        """Reconcile only the exact issued proof and actor lease, then registry.

        There is deliberately no `acknowledged=True` or caller-provided timeout
        shortcut.  A proof from another generation is always rejected.
        """
        with self._mutex:
            target = self._target(handle)
            if target.phase != "closed" or proof is not target.proof or proof is None:
                raise LifecycleRefused("no exact-generation internally issued termination proof")
            if proof.handle != target.handle:
                raise LifecycleRefused("termination proof generation mismatch")
            _record, _at, pending = self.registry.tombstone(target.handle)
            if not pending:
                raise LifecycleRefused("cleanup was already reconciled")
            if target.lease is not None:
                if not target.lease_sent or target.unsent_release_failed:
                    raise LifecycleRefused("unreconciled unsent actor prevents release")
                self.locks.reconcile_quarantine(target.handle, acknowledged=True,
                                                expected_lease=target.lease)
                target.lease = None
            self.registry.acknowledge_cleanup(target.handle)
            if not self.broker.reconcile_acknowledged_cleanup(target.handle, target.close_receipt):
                raise ProductionDefect("broker did not reconcile joined external cleanup")
            target.phase = "reclaimed"

    def snapshot(self, handle: object) -> dict:
        """Metadata only; no mutable transport object or frame bytes escape."""
        with self._mutex:
            target = self._target(handle)
            tombstone_pending = None
            if target.phase != "active":
                try:
                    _, _, tombstone_pending = self.registry.tombstone(target.handle)
                except StaleHandleError:
                    pass
            return {"identity": (target.handle.kind, target.handle.canonical_id,
                                  target.handle.generation),
                    "phase": target.phase,
                    "registry_quarantined": tombstone_pending,
                    "has_actor_lease": target.lease is not None,
                    "pending_publications": len(target.pending)}
