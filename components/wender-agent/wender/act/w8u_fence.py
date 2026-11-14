# @module {"id":"wa.act.w8u_fence","layer":2,"owner":"ACT","deps":["wa.act.focus_policy","wa.act.target_lock_registry","wa.act.window_scheduler","wa.perceive.session_registry"],"book":"HB-WA-s2-act"}
"""Explicit W8U custody adapters over the existing production owners.

This is an in-process fence, not an action/wire parser or a host attestation
issuer. Authentication, budget and revision-pinned OS evidence are injected
capabilities. Missing capabilities refuse; caller payloads never name authority.
All capability calls and irreversible sends run outside metadata locks.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import threading
import time
import weakref

from wender.act.focus_policy import FocusPolicy, FocusRefused
from wender.act.target_lock_registry import TargetBusy, TargetLockRefused, TargetLockRegistry
from wender.act.window_scheduler import Completion, DispatchContext, FenceRefused, WriterFence
from wender.perceive.session_registry import (
    ActionRequest, ActorLease, FocusLease, FrameRef, Handle, Observation,
    RegistryError, SessionRegistry, Ticket,
)


class FenceError(ValueError):
    """Owned refusal code; a refusal is never evidence of an applied effect."""

    def __init__(self, code, detail):
        self.code = code
        super().__init__(code + ": " + detail)


def _integer(value, code, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise FenceError(code, name + " must be an exact integer")
    return value


def _name(value, code, name):
    if (type(value) is not str or not value or value != value.strip()
            or "\x00" in value):
        raise FenceError(code, name + " is absent or invalid")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise FenceError(code, name + " is not valid UTF-8") from exc
    return value


def _key(handle):
    if type(handle) is not Handle:
        raise FenceError("E_LOCK", "a real registry handle is required")
    if (type(handle.kind) is not str or type(handle.canonical_id) is not str
            or type(handle.generation) is not int):
        raise FenceError("E_LOCK", "handle fields were replaced")
    return (handle.kind, handle.canonical_id, handle.generation)


def _methods(provider, names, code):
    # This checks one explicit interface, never searches for permissive aliases.
    if provider is None or any(not callable(getattr(provider, name, None)) for name in names):
        raise FenceError(code, "AUSENTE: required capability " + ", ".join(names))


@dataclass(frozen=True)
class DisplayGrant:
    handle: Handle
    epoch: int
    seat: str
    display: str
    domain: str
    proof: object


@dataclass(frozen=True)
class FocusEvidence:
    handle: Handle
    epoch: int
    domain: str
    editable: bool
    proof: object


_ADAPTER_GUARD = threading.RLock()
_ADAPTERS = weakref.WeakValueDictionary()


class AuthenticatedRegistryAdapter:
    """One lock namespace per real SessionRegistry and authenticated authority.

The trusted provider must implement grant_for/verify_grant and
focus_for/verify_focus. A proof object or display string alone is insufficient.
Provider verification authenticates exact handle, epoch, seat, display and
domain outside the action payload. Rebinding a live incarnation is refused.
"""

    def __new__(cls, sessions, authority, *, clock=None):
        if not isinstance(sessions, SessionRegistry):
            raise FenceError("E_LOCK", "real SessionRegistry prerequisite AUSENTE")
        _methods(authority, ("grant_for", "verify_grant", "focus_for", "verify_focus"), "E_LOCK")
        clock = time.monotonic_ns if clock is None else clock
        if not callable(clock):
            raise FenceError("E_LOCK", "monotonic clock prerequisite AUSENTE")
        # No callbacks inside the cache transaction. A retained lock keeps its
        # adapter alive, so a new wrapper can never split an occupied namespace.
        with _ADAPTER_GUARD:
            current = _ADAPTERS.get(id(sessions))
            if current is not None:
                if current.sessions is not sessions or current.authority is not authority:
                    raise FenceError("E_LOCK", "conflicting authority for session registry")
                if current.clock is not clock:
                    raise FenceError("E_LOCK", "conflicting clock for session registry")
                return current
            result = super().__new__(cls)
            result.sessions, result.authority, result.clock = sessions, authority, clock
            result._lock = threading.RLock()
            result._bindings, result._domains = {}, {}
            _ADAPTERS[id(sessions)] = result
            return result

    def __init__(self, sessions, authority, *, clock=None):
        # __new__ initializes atomically; repeated construction is intentional.
        pass

    def get(self, handle):
        key = _key(handle)
        try:
            record = self.sessions.resolve(handle)
        except RegistryError as exc:
            raise FenceError("E_LOCK", "unknown, closed or superseded target") from exc
        if (_key(record.handle) != key or record.closed is not False
                or type(record.epoch) is not int or record.epoch < 0):
            raise FenceError("E_LOCK", "invalid current registry record")
        return record

    def grant_for(self, handle):
        record = self.get(handle)
        now = _integer(self.clock(), "E_LOCK", "clock")
        grant = self.authority.grant_for(handle)
        if (type(grant) is not DisplayGrant or grant.proof is None
                or _key(grant.handle) != _key(record.handle)
                or type(grant.epoch) is not int or grant.epoch != record.epoch):
            raise FenceError("E_LOCK", "authenticated seat/display grant is absent or mismatched")
        binding = tuple(_name(getattr(grant, field), "E_LOCK", field)
                        for field in ("seat", "display", "domain"))
        if self.authority.verify_grant(grant, registry=self.sessions, now_ns=now) is not True:
            raise FenceError("E_LOCK", "seat/display proof was not authenticated")
        # Verification may have raced epoch retirement. Do not publish stale proof.
        latest = self.get(handle)
        if latest.epoch != record.epoch:
            raise FenceError("E_EPOCH", "registry epoch changed during grant verification")
        resource = json.dumps(binding[:2], ensure_ascii=False, separators=(",", ":"))
        key = _key(record.handle)
        with self._lock:
            previous = self._bindings.get(key)
            domain = self._domains.get(resource)
            if previous is not None and previous != binding:
                raise FenceError("E_LOCK", "conflicting physical alias for handle incarnation")
            if domain is not None and domain != binding[2]:
                raise FenceError("E_LOCK", "conflicting input domains for physical display")
            self._bindings[key], self._domains[resource] = binding, binding[2]
        return grant

    def canonical_resource(self, handle):
        grant = self.grant_for(handle)
        # Internal unambiguous key, not a wire digest or caller-supplied id.
        return json.dumps((grant.seat, grant.display), ensure_ascii=False, separators=(",", ":"))


@dataclass
class _Held:
    lease: ActorLease
    handle: Handle
    nonce: bytes
    owner: str
    generation: int
    expires_ns: int
    phase: str = "HELD"
    acknowledged: bool = False


class SchedulerLockAdapter:
    """Scheduler-shaped methods without converting release into an implicit ACK."""

    def __init__(self, locks):
        if not isinstance(locks, TargetLockRegistry):
            raise FenceError("E_LOCK", "real target lock registry prerequisite AUSENTE")
        self.raw = locks
        self._lock, self._held = threading.RLock(), {}

    def _held_locked(self, lease):
        state = self._held.get(id(lease))
        if (state is None or state.lease is not lease or type(lease) is not ActorLease
                or type(lease.nonce) is not bytes or lease.nonce != state.nonce
                or type(lease.owner) is not str or lease.owner != state.owner
                or type(lease.generation) is not int or lease.generation != state.generation
                or type(lease.expires_ns) is not int or lease.expires_ns != state.expires_ns
                or _key(lease.handle) != _key(state.handle)):
            raise FenceError("E_LOCK", "forged, consumed or foreign adapter lease")
        return state

    def try_acquire(self, handle, *, owner, expires_ns):
        try:
            lease = self.raw.acquire(handle, owner, expires_ns=expires_ns)
        except TargetBusy:
            return None
        except TargetLockRefused as exc:
            raise FenceError("E_LOCK", str(exc)) from exc
        state = _Held(lease, Handle(*_key(lease.handle)), lease.nonce,
                      lease.owner, lease.generation, lease.expires_ns)
        with self._lock:
            self._held[id(lease)] = state
        return lease

    def owns(self, lease, *, now_ns):
        return self.raw.owns(lease, now_ns=now_ns)

    def mark_inflight(self, lease):
        with self._lock:
            state = self._held_locked(lease)
            if state.phase != "HELD":
                raise FenceError("E_LOCK", "lease already marked as sent")
            state.phase = "MARKING"
        try:
            self.raw.mark_inflight(lease)
        except TargetLockRefused as exc:
            with self._lock:
                state.phase = "HELD"
            raise FenceError("E_LOCK", str(exc)) from exc
        with self._lock:
            state.phase = "SENT"

    def quarantine(self, lease):
        with self._lock:
            state = self._held_locked(lease)
            phase = state.phase
        if phase == "HELD":
            self.mark_inflight(lease)
        elif phase not in ("SENT", "QUARANTINED"):
            raise FenceError("E_UNCERTAIN", "lease transition requires reconciliation")
        try:
            self.raw.reconcile_quarantine(state.handle, acknowledged=False, expected_lease=lease)
        except TargetLockRefused as exc:
            raise FenceError("E_LOCK", str(exc)) from exc
        with self._lock:
            state.phase = "QUARANTINED"

    def acknowledge(self, lease, *, acknowledged):
        """Trusted terminal/quiescence evidence, never an expiry or release guess."""
        if acknowledged is not True:
            raise FenceError("E_UNCERTAIN", "positive quiescence acknowledgement is required")
        with self._lock:
            state = self._held_locked(lease)
            if state.phase not in ("SENT", "QUARANTINED"):
                raise FenceError("E_LOCK", "no sent lease to acknowledge")
            state.acknowledged = True

    def release(self, lease):
        with self._lock:
            state = self._held_locked(lease)
            phase = state.phase
            if phase != "HELD" and not state.acknowledged:
                raise FenceError("E_UNCERTAIN", "sent custody requires acknowledged quiescence")
            if phase not in ("HELD", "SENT", "QUARANTINED"):
                raise FenceError("E_UNCERTAIN", "lease transition is not quiescent")
            state.phase = "RELEASING"
        try:
            if phase == "HELD":
                self.raw.release(lease)
            else:
                self.raw.reconcile_quarantine(state.handle, acknowledged=True, expected_lease=lease)
        except TargetLockRefused as exc:
            with self._lock:
                state.phase = phase
            raise FenceError("E_LOCK", str(exc)) from exc
        with self._lock:
            self._held.pop(id(lease))

    def reconcile(self, lease, *, acknowledged):
        if acknowledged is not True:
            self.quarantine(lease)
            raise FenceError("E_UNCERTAIN", "negative reconciliation retains custody")
        self.acknowledge(lease, acknowledged=True)
        self.release(lease)


@dataclass
class _FocusToken:
    token: FocusLease
    consumed: bool = False


class SchedulerFocusAdapter:
    """Translate verified focus events into real one-use FocusPolicy grants."""

    def __init__(self, registry, locks, authority, *, clock=None):
        if registry.authority is not authority or locks.raw.registry is not registry:
            raise FenceError("E_FOCUS", "focus providers must share authenticated registry")
        self.registry, self.locks, self.authority = registry, locks, authority
        self.clock = time.monotonic_ns if clock is None else clock
        self.policy = FocusPolicy(registry, locks, clock=self.clock)
        self._lock, self._tokens = threading.RLock(), {}

    def evidence_for(self, handle, *, epoch, now_ns):
        grant = self.registry.grant_for(handle)
        if grant.epoch != epoch:
            raise FenceError("E_EPOCH", "focus requested at the wrong epoch")
        evidence = self.authority.focus_for(handle)
        if (type(evidence) is not FocusEvidence or evidence.proof is None
                or _key(evidence.handle) != _key(handle)
                or type(evidence.epoch) is not int or evidence.epoch != epoch
                or type(evidence.editable) is not bool or type(evidence.domain) is not str
                or evidence.domain != grant.domain):
            raise FenceError("E_FOCUS", "trusted focus evidence or domain is absent")
        if self.authority.verify_focus(evidence, registry=self.registry.sessions,
                                       now_ns=now_ns) is not True:
            raise FenceError("E_FOCUS", "focus evidence was not authenticated")
        return evidence

    def try_acquire(self, lease, *, epoch):
        now = _integer(self.clock(), "E_FOCUS", "clock")
        evidence = self.evidence_for(lease.handle, epoch=epoch, now_ns=now)
        try:
            self.policy.set_focus(lease.handle, domain=evidence.domain, epoch=epoch)
            token = self.policy.grant(lease, domain=evidence.domain, now_ns=now)
        except FocusRefused as exc:
            raise FenceError("E_FOCUS", str(exc)) from exc
        with self._lock:
            self._tokens[id(token)] = _FocusToken(token)
        return token

    def _token_locked(self, token):
        state = self._tokens.get(id(token))
        if state is None or state.token is not token:
            raise FenceError("E_FOCUS", "focus grant is copied, foreign or retired")
        return state

    def validate(self, lease, focus, observation, *, now_ns):
        with self._lock:
            if self._token_locked(focus).consumed:
                raise FenceError("E_FOCUS", "focus grant was consumed")
        evidence = self.evidence_for(lease.handle, epoch=observation.epoch, now_ns=now_ns)
        if focus.lease is not lease or focus.domain != evidence.domain:
            raise FenceError("E_FOCUS", "focus lease does not bind the actor and trusted domain")
        try:
            self.policy.validate(lease, focus, observation, domain=evidence.domain, now_ns=now_ns)
        except FocusRefused as exc:
            raise FenceError("E_FOCUS", str(exc)) from exc
        return evidence

    def consume(self, focus):
        with self._lock:
            state = self._token_locked(focus)
            if state.consumed:
                raise FenceError("E_FOCUS", "focus grant was consumed")
            state.consumed = True
        try:
            self.policy.consume(focus)
        except FocusRefused as exc:
            raise FenceError("E_FOCUS", str(exc)) from exc

    def release(self, focus):
        with self._lock:
            state = self._token_locked(focus)
            consumed = state.consumed
            state.consumed = True
        if not consumed:
            try:
                self.policy.consume(focus)
            except FocusRefused:
                # A locally minted grant may already have been invalidated by
                # a trusted focus/modal event. Cleanup cannot restore authority.
                pass
        with self._lock:
            self._tokens.pop(id(focus))

    def quarantine(self, focus):
        with self._lock:
            state = self._token_locked(focus)
            consumed = state.consumed
        if not consumed:
            try:
                self.consume(focus)
            except FenceError:
                # The exact owned token is now retired locally in either case.
                pass


class RevisionPinnedFrames:
    """Require an explicit OS owner pin and independent positive/negative vectors.

The caller provides vectors independently of the provider. Synthetic unit
vectors prove adapter behavior only; they do not certify the production owner.
No wire constants, END numbers or digest domains are selected here.
"""

    def __init__(self, provider, *, revision, accepted_vector, rejected_vector):
        _methods(provider, ("verify_vector", "verify"), "E_STALE_FRAME")
        if (type(revision) is not str or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision) is None
                or getattr(provider, "contract_id", None) != "w8u-os-contract"
                or getattr(provider, "revision", None) != revision
                or type(accepted_vector) is not bytes or not accepted_vector
                or type(rejected_vector) is not bytes or not rejected_vector
                or accepted_vector == rejected_vector):
            raise FenceError("E_STALE_FRAME", "AUSENTE: OS revision pin or independent vectors")
        if (provider.verify_vector(accepted_vector, revision=revision) is not True
                or provider.verify_vector(rejected_vector, revision=revision) is not False):
            raise FenceError("E_STALE_FRAME", "OS positive/negative vectors disagree")
        self.provider, self.revision = provider, revision

    def validate(self, observation, evidence, *, now_ns):
        if (self.provider.contract_id != "w8u-os-contract" or self.provider.revision != self.revision
                or type(observation) is not Observation or type(observation.frame) is not FrameRef):
            raise FenceError("E_STALE_FRAME", "OS pin or real observation is absent")
        frame = observation.frame
        if ((_key(observation.handle), observation.epoch, observation.frame_seq)
                != (_key(frame.handle), frame.epoch, frame.frame_seq)
                or frame.rgb.shape != (frame.height, frame.width, 3)
                or str(frame.rgb.dtype) != "uint8"
                or hashlib.sha256(frame.rgb.tobytes(order="C")).hexdigest() != frame.digest):
            raise FenceError("E_STALE_FRAME", "frame identity, dimensions or bytes disagree")
        if self.provider.verify(observation, evidence, revision=self.revision, now_ns=now_ns) is not True:
            raise FenceError("E_STALE_FRAME", "frame proof was rejected")


@dataclass(frozen=True)
class Prepared:
    request: ActionRequest
    context: DispatchContext
    observation: Observation
    frame_evidence: object
    resource: str
    reservation: object


@dataclass
class _Entry:
    prepared: Prepared
    stamp: tuple
    actor: ActorLease
    focus: FocusLease
    permit: object
    context: DispatchContext
    observation: Observation
    observation_stamp: tuple
    frame_evidence: object
    resource: str
    reservation: object
    context_clock: object
    state: str = "PREPARED"
    marked: bool = False
    budget_settled: bool = False
    focus_released: bool = False
    actor_released: bool = False
    permit_retired: bool = False
    completion: Completion | None = None
    error: FenceError | None = None


def _payload_json(payload):
    def check(value):
        if value is None or type(value) in (str, bool, int):
            return
        if type(value) is list:
            for child in value:
                check(child)
            return
        if type(value) is dict and all(type(key) is str for key in value):
            for child in value.values():
                check(child)
            return
        raise FenceError("E_SCHEMA", "internal payload must contain JSON values without floats")
    check(payload)
    try:
        result = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        result.encode("utf-8", "strict")
        return result
    except (TypeError, ValueError, UnicodeError) as exc:
        raise FenceError("E_SCHEMA", "invalid internal request snapshot") from exc


def _request_stamp(request):
    if type(request) is not ActionRequest:
        raise FenceError("E_SCHEMA", "real ActionRequest prerequisite AUSENTE")
    return (_key(request.handle), _name(request.action_id, "E_SCHEMA", "action_id"),
            _integer(request.epoch, "E_EPOCH", "epoch"), _payload_json(request.payload),
            _integer(request.cost, "E_BUDGET", "cost", 1),
            _integer(request.deadline_ns, "E_DEADLINE", "deadline"))


def _from_stamp(stamp):
    handle, action_id, epoch, payload, cost, deadline = stamp
    return ActionRequest(Handle(*handle), action_id, epoch, json.loads(payload), cost, deadline)


def _observation_stamp(observation):
    if type(observation) is not Observation or type(observation.frame) is not FrameRef:
        raise FenceError("E_STALE_FRAME", "real observation prerequisite AUSENTE")
    frame = observation.frame
    return (_key(observation.handle), observation.epoch, observation.frame_seq,
            observation.dpr_ppm, _payload_json(observation.state), _key(frame.handle),
            frame.epoch, frame.frame_seq, frame.width, frame.height, frame.digest)


class W8UFence:
    """Prepare and send once through genuine leases and the native writer fence.

Input is an already parsed internal ActionRequest; the separately owned action
parser must validate w8u.action/v1 before invoking this boundary. Budget and
frame verification are mandatory injected interfaces. Terminal callbacks must
return real Completion evidence; None/errors/UNKNOWN retain physical custody.
"""

    def __init__(self, *, registry, locks, focus, writer_fence, budget, frames, clock=None):
        if (not isinstance(registry, AuthenticatedRegistryAdapter)
                or not isinstance(locks, SchedulerLockAdapter) or locks.raw.registry is not registry
                or not isinstance(focus, SchedulerFocusAdapter) or focus.locks is not locks
                or not isinstance(writer_fence, WriterFence)):
            raise FenceError("E_LOCK", "explicit registry/lock/focus/writer adapters are required")
        _methods(budget, ("reserve", "validate", "settle"), "E_BUDGET")
        if not isinstance(frames, RevisionPinnedFrames):
            raise FenceError("E_STALE_FRAME", "revision-pinned frame verifier prerequisite AUSENTE")
        self.registry, self.locks, self.focus = registry, locks, focus
        self.writer_fence, self.budget, self.frames = writer_fence, budget, frames
        self.clock = time.monotonic_ns if clock is None else clock
        self._lock, self._entries, self._actions = threading.RLock(), {}, {}
        self.diagnostics = []

    def _current(self, request):
        now = _integer(self.clock(), "E_DEADLINE", "clock")
        if now >= request.deadline_ns:
            raise FenceError("E_DEADLINE", "absolute deadline expired")
        record = self.registry.get(request.handle)
        if record.epoch != request.epoch:
            self.writer_fence.publish_epoch(request.handle, record.epoch)
            raise FenceError("E_EPOCH", "current registry epoch differs from request")
        return now

    def _observation(self, request, observation):
        if (type(observation) is not Observation or _key(observation.handle) != _key(request.handle)
                or observation.epoch != request.epoch):
            raise FenceError("E_STALE_FRAME", "observation does not bind current request")
        try:
            # Use the real registry's latest sequence/digest check; an injected
            # provider cannot turn an unregistered or superseded frame current.
            self.registry.sessions.admit_observation(request.handle, observation.frame,
                state=observation.state, dpr_ppm=observation.dpr_ppm)
        except RegistryError as exc:
            raise FenceError("E_STALE_FRAME", "registry rejected current frame provenance") from exc

    def prepare(self, request, *, observation, frame_evidence):
        stamp = _request_stamp(request)
        snapshot = _from_stamp(stamp)
        observation_stamp = _observation_stamp(observation)
        replay_stamp = stamp + observation_stamp
        # An identical replay returns the original capability/outcome even
        # after expiry. It never admits new work or bypasses send revalidation.
        with self._lock:
            previous = self._actions.get(snapshot.action_id)
            if previous is not None:
                if previous[0] != replay_stamp:
                    raise FenceError("E_SCHEMA", "action id reused with changed payload or evidence")
                if previous[1] is None:
                    raise FenceError("E_LOCK", "action preparation is already in progress")
                return previous[1]
        # Authentication and real epoch checks precede any lock acquisition.
        now = self._current(snapshot)
        resource = self.registry.canonical_resource(snapshot.handle)
        self._observation(snapshot, observation)
        self.frames.validate(observation, frame_evidence, now_ns=now)
        with self._lock:
            previous = self._actions.get(snapshot.action_id)
            if previous is not None:
                if previous[0] != replay_stamp:
                    raise FenceError("E_SCHEMA", "action id reused with changed payload or evidence")
                if previous[1] is None:
                    raise FenceError("E_LOCK", "action preparation is already in progress")
                return previous[1]
            self._actions[snapshot.action_id] = (replay_stamp, None)
        actor = focus = permit = reservation = None
        try:
            self.writer_fence.bind(snapshot.handle, resource, epoch=snapshot.epoch)
            if self.writer_fence.reason(snapshot.handle, resource):
                raise FenceError("E_LOCK", "writer is stopped or quarantined")
            reservation = self.budget.reserve(snapshot.handle, snapshot.cost,
                action_id=snapshot.action_id, deadline_ns=snapshot.deadline_ns)
            if reservation is None:
                raise FenceError("E_BUDGET", "budget reservation was refused")
            actor = self.locks.try_acquire(snapshot.handle, owner=snapshot.action_id,
                                           expires_ns=snapshot.deadline_ns)
            if actor is None:
                raise FenceError("E_LOCK", "physical target is already owned or quarantined")
            focus = self.focus.try_acquire(actor, epoch=snapshot.epoch)
            now = self._current(snapshot)
            if not self.locks.owns(actor, now_ns=now):
                raise FenceError("E_LOCK", "actor lease nonce is not current/owned")
            ticket = Ticket(snapshot.action_id, snapshot.handle)
            self.writer_fence.advance(now)
            permit = self.writer_fence.authorize(ticket, resource, snapshot.deadline_ns, snapshot.epoch)
            if permit is None:
                raise FenceError("E_LOCK", "writer permit was refused")
            context = DispatchContext(ticket, actor, focus, self.writer_fence,
                                      snapshot.deadline_ns, self.clock, permit)
            prepared = Prepared(snapshot, context, observation, frame_evidence, resource, reservation)
            entry = _Entry(prepared, stamp, actor, focus, permit, context,
                           observation, observation_stamp, frame_evidence,
                           resource, reservation, self.clock)
            with self._lock:
                self._entries[id(prepared)] = entry
                self._actions[snapshot.action_id] = (replay_stamp, prepared)
            return prepared
        except Exception as original:
            self.writer_fence.retire(permit)
            operations = []
            if focus is not None:
                operations.append(("rollback_focus", lambda: self.focus.release(focus)))
            if actor is not None:
                operations.append(("rollback_actor", lambda: self.locks.release(actor)))
            if reservation is not None:
                operations.append(("rollback_budget", lambda: self.budget.settle(reservation, consumed=False)))
            for name, operation in operations:
                try:
                    operation()
                except Exception as cleanup_error:
                    self.writer_fence.quarantine(resource)
                    with self._lock:
                        self.diagnostics.append((snapshot.action_id, name, type(cleanup_error).__name__))
            with self._lock:
                self._actions.pop(snapshot.action_id, None)
            # Retained custody is never hidden by replacing the original
            # refusal with a secondary cleanup exception.
            raise original

    def _entry(self, prepared):
        with self._lock:
            entry = self._entries.get(id(prepared))
            if entry is None or entry.prepared is not prepared:
                raise FenceError("E_LOCK", "preparation was not minted by this fence")
        if _request_stamp(prepared.request) != entry.stamp:
            raise FenceError("E_SCHEMA", "prepared request was modified")
        context = prepared.context
        if (context is not entry.context or type(context) is not DispatchContext
                or context.actor_lease is not entry.actor
                or context.focus_lease is not entry.focus or context._permit is not entry.permit
                or context.writer_fence is not self.writer_fence
                or context._clock is not entry.context_clock
                or prepared.observation is not entry.observation
                or prepared.frame_evidence is not entry.frame_evidence
                or type(prepared.resource) is not str or prepared.resource != entry.resource
                or prepared.reservation is not entry.reservation
                or context.deadline_ns != entry.stamp[5]
                or (context.ticket.action_id, _key(context.ticket.handle)) != (entry.stamp[1], entry.stamp[0])):
            raise FenceError("E_LOCK", "prepared dispatch capability was replaced")
        if _observation_stamp(entry.observation) != entry.observation_stamp:
            raise FenceError("E_STALE_FRAME", "admitted observation was modified")
        return entry

    def _settle(self, entry, *, consumed):
        if not entry.budget_settled:
            self.budget.settle(entry.reservation, consumed=consumed)
            entry.budget_settled = True

    def _retire_permit(self, entry):
        if not entry.permit_retired:
            self.writer_fence.retire(entry.permit)
            entry.permit_retired = True

    def _release_focus(self, entry):
        if not entry.focus_released:
            self.focus.release(entry.focus)
            entry.focus_released = True

    def _release_actor(self, entry):
        if not entry.actor_released:
            self.locks.release(entry.actor)
            entry.actor_released = True

    def _diagnostic(self, entry, operation, exc):
        with self._lock:
            self.diagnostics.append((entry.stamp[1], operation, type(exc).__name__))

    def _quarantine(self, entry):
        with self._lock:
            entry.state = "QUARANTINING"
        try:
            self.writer_fence.quarantine(entry.resource)
            operations = []
            if not entry.actor_released:
                operations.append(("quarantine_actor", lambda: self.locks.quarantine(entry.actor)))
            if not entry.focus_released:
                operations.append(("quarantine_focus", lambda: self.focus.quarantine(entry.focus)))
            operations.append(("retire_permit", lambda: self._retire_permit(entry)))
            for name, operation in operations:
                try:
                    operation()
                except Exception as exc:
                    # Keep the original dispatch refusal/uncertainty and leave
                    # resumable quarantine even if one cleanup provider fails.
                    self._diagnostic(entry, name, exc)
        finally:
            with self._lock:
                entry.state = "QUARANTINED"

    def send(self, prepared, callback):
        entry = self._entry(prepared)
        with self._lock:
            if entry.completion is not None:
                return entry.completion
            if entry.error is not None:
                raise entry.error
            if entry.state != "PREPARED":
                raise FenceError("E_UNCERTAIN", "action is already sending or awaiting reconciliation")
            entry.state = "SENDING"
        request = _from_stamp(entry.stamp)
        try:
            now = self._current(request)
            if self.registry.canonical_resource(request.handle) != entry.resource:
                raise FenceError("E_LOCK", "physical resource changed after preparation")
            if self.locks.owns(entry.actor, now_ns=now) is not True:
                raise FenceError("E_LOCK", "actor lease nonce is not current/owned")
            self._observation(request, entry.observation)
            self.frames.validate(entry.observation, entry.frame_evidence, now_ns=now)
            if self.budget.validate(entry.reservation, handle=request.handle,
                                   action_id=request.action_id, now_ns=now) is not True:
                raise FenceError("E_BUDGET", "budget capability was revoked or expired")
            now = self._current(request)
            evidence = self.focus.validate(entry.actor, entry.focus, entry.observation, now_ns=now)
            if request.payload.get("verb") == "text" and evidence.editable is not True:
                raise FenceError("E_FOCUS", "text requires authenticated editable focus")
            self._entry(prepared)  # Detect replacement during unlocked capabilities.
            # These real owners retire focus and fence custody before the send.
            # No readiness check calls WriterFence.check: it marks possible_send.
            self.focus.consume(entry.focus)
            self.locks.mark_inflight(entry.actor)
            entry.marked = True
            # DispatchContext.send invokes WriterFence.check immediately before
            # the callback and releases its metadata lock before irreversible I/O.
            completion = entry.context.send(callback, request)
            if (type(completion) is not Completion or completion.action_id != request.action_id
                    or _key(completion.handle) != _key(request.handle)
                    or completion.status not in ("SUCCEEDED", "FAILED", "CANCELLED")
                    or completion.acknowledged is not True):
                raise FenceError("E_UNCERTAIN", "missing or conflicting terminal acknowledgement")
            if (self.clock() >= request.deadline_ns
                    or self.writer_fence.reason(request.handle, entry.resource)
                    or entry.permit.revoked):
                raise FenceError("E_UNCERTAIN", "terminal acknowledgement crossed STOP, retirement or deadline")
        except Exception as exc:
            if isinstance(exc, FenceError):
                error = exc
            elif isinstance(exc, FenceRefused):
                code = "E_DEADLINE" if self.clock() >= request.deadline_ns else "E_LOCK"
                error = FenceError(code, str(exc))
            else:
                error = FenceError("E_UNCERTAIN" if entry.marked else "E_LOCK",
                                   "capability or actuator failed: " + type(exc).__name__)
            with self._lock:
                entry.error = error
            if entry.marked:
                self._quarantine(entry)
            else:
                self._retire_permit(entry)
                self._release_focus(entry)
                try:
                    self._release_actor(entry)
                except FenceError as cleanup_error:
                    # Expiry/tampering cannot silently free retained custody.
                    self.writer_fence.quarantine(entry.resource)
                    self._diagnostic(entry, "release_unsent", cleanup_error)
                self._settle(entry, consumed=False)
                with self._lock:
                    entry.state = "REFUSED"
            raise error from exc
        # Only positive terminal evidence enables release of a sent raw lease.
        try:
            self.locks.acknowledge(entry.actor, acknowledged=True)
            self._settle(entry, consumed=True)
            self._release_focus(entry)
            self._release_actor(entry)
            self._retire_permit(entry)
        except Exception as exc:
            error = FenceError("E_UNCERTAIN", "terminal bookkeeping requires reconciliation")
            with self._lock:
                entry.error = error
            self._quarantine(entry)
            raise error from exc
        with self._lock:
            entry.completion, entry.state = completion, "DONE"
        return completion

    def reconcile(self, prepared, *, acknowledged):
        entry = self._entry(prepared)
        with self._lock:
            if entry.state != "QUARANTINED":
                raise FenceError("E_LOCK", "no quarantined preparation to reconcile")
            # Reserve cleanup without holding metadata across provider calls.
            # Concurrent/reentrant acknowledgements cannot settle twice.
            entry.state = "RECONCILING"
        try:
            if acknowledged is not True:
                if not entry.actor_released:
                    self.locks.quarantine(entry.actor)
                raise FenceError("E_UNCERTAIN", "negative reconciliation retains custody")
            if not entry.actor_released:
                self.locks.acknowledge(entry.actor, acknowledged=True)
            self._settle(entry, consumed=True)
            self._release_focus(entry)
            self._release_actor(entry)
            self._retire_permit(entry)
            self.writer_fence.reconcile(entry.resource)
        except Exception:
            with self._lock:
                entry.state = "QUARANTINED"
            raise
        else:
            with self._lock:
                entry.state = "RECONCILED"
        # Original error remains: reconciliation never retries or invents effect.

    def stop(self, handle=None):
        self.writer_fence.stop(handle)
        if handle is not None and self.writer_fence.resource_for(handle) is None:
            resource = self.registry.canonical_resource(handle)
            self.writer_fence.bind(handle, resource)

