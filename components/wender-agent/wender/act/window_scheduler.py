# @module {"id":"wa.act.window_scheduler","layer":2,"owner":"ACT","deps":["wa.perceive.session_registry","wa.act.target_lock_registry","wa.act.focus_policy","wa.act.budget_governor"],"book":"HB-WA-s2-act"}
"""Bounded, offline-testable scheduling over explicit ownership contracts.

No provider callback runs while ``WriterFence.metadata_lock`` is held. Providers
prepare reservations nonblockingly; a final fence transaction publishes the
claim and dispatch permit together. Every transport write must use that permit.
See docs/spec/jq2a_m6_c1_window_scheduler.md for the integration prerequisites.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
import threading
import time
from typing import Protocol

import numpy as np


_missing = []
try:
    from wender.perceive.session_registry import (
        ActionRequest, ActorLease, FocusLease, FrameRef, Handle, Observation,
        Receipt, TargetRecord, Ticket, WindowFrames,
    )
except ModuleNotFoundError as exc:
    if exc.name != "wender.perceive.session_registry":
        raise
    _missing.append(exc.name)

    # Exact frozen fallback layouts, used ONLY while the provider is absent.
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
    class Ticket:
        action_id: str
        handle: Handle

    @dataclass(frozen=True)
    class WindowFrames:
        handle: Handle
        epoch: int
        frames: tuple
        frame_seqs: tuple
        dpr_ppm: int


try:
    from wender.act.target_lock_registry import TargetLockRegistry
except ModuleNotFoundError as exc:
    if exc.name != "wender.act.target_lock_registry":
        raise
    _missing.append(exc.name)

    class TargetLockRegistry(Protocol):
        def try_acquire(self, handle, *, owner, expires_ns): ...
        def release(self, lease): ...
        def quarantine(self, lease): ...


try:
    from wender.act.focus_policy import FocusPolicy
except ModuleNotFoundError as exc:
    if exc.name != "wender.act.focus_policy":
        raise
    _missing.append(exc.name)

    class FocusPolicy(Protocol):
        def try_acquire(self, lease, *, epoch): ...
        def release(self, lease): ...
        def quarantine(self, lease): ...


try:
    from wender.act.budget_governor import BudgetGovernor
except ModuleNotFoundError as exc:
    if exc.name != "wender.act.budget_governor":
        raise
    _missing.append(exc.name)

    class BudgetGovernor(Protocol):
        def reserve(self, handle, cost, *, action_id, deadline_ns): ...
        def settle(self, reservation, *, consumed): ...


MISSING_PREREQUISITES = tuple(_missing)
del _missing
TARGET_KINDS = frozenset(("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"))
PREREQUISITE_GATES = ("identity_streams", "ownership_perception", "readiness", "writer_fence")


class InvalidRequest(ValueError):
    """An identity, epoch, deadline, or evidence contract was violated."""


class LockOrderError(ValueError):
    """A metadata acquisition trace would invert the canonical order."""


class FenceRefused(RuntimeError):
    """A write did not cross the shared authorization boundary."""


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise InvalidRequest(name + " must be an integer >= " + str(minimum))
    return value


def _handle_key(handle):
    try:
        kind, ident, generation = handle.kind, handle.canonical_id, handle.generation
    except AttributeError as exc:
        raise InvalidRequest("malformed handle") from exc
    if kind not in TARGET_KINDS or not isinstance(ident, str) or not ident.strip():
        raise InvalidRequest("unsupported kind or empty canonical identity")
    return kind, ident, _integer(generation, "generation")


def _handle(handle):
    return Handle(*_handle_key(handle))


def _ticket_key(ticket):
    if not isinstance(ticket.action_id, str) or not ticket.action_id.strip():
        raise InvalidRequest("action_id must be nonempty")
    return _handle_key(ticket.handle), ticket.action_id


def check_lock_order(ranks):
    """Validate ranks 0..6, optionally ``(rank, canonical_target)`` entries.

    Named ranks are scheduler/fence, registry, budget, target_actor,
    focus_domain, stream_bank, stream. Equal-rank keys must increase strictly;
    the caller releases in reverse order. This function never waits.
    """
    names = {"scheduler": 0, "fence": 0, "registry": 1, "budget": 2,
             "target_actor": 3, "focus_domain": 4, "stream_bank": 5, "stream": 6}
    previous_rank, previous_key = -1, None
    for entry in ranks:
        rank, key = entry if isinstance(entry, (tuple, list)) else (entry, None)
        rank = names.get(rank, rank) if isinstance(rank, str) else rank
        if type(rank) is not int or not 0 <= rank <= 6:
            raise LockOrderError("unknown metadata rank")
        if key is not None:
            if hasattr(key, "canonical_id"):
                key = _handle_key(key)
            elif isinstance(key, str):
                key = (key,)
            elif not isinstance(key, tuple):
                raise LockOrderError("canonical key must be a handle, string or tuple")
        if rank < previous_rank:
            raise LockOrderError("inverted metadata rank")
        if rank == previous_rank:
            if key is None or previous_key is None:
                raise LockOrderError("same-rank acquisitions require canonical keys")
            try:
                ordered = previous_key < key
            except TypeError as exc:
                raise LockOrderError("incomparable canonical keys") from exc
            if not ordered:
                raise LockOrderError("inverted or repeated canonical target")
        previous_rank, previous_key = rank, key
    return True


@dataclass
class _Permit:
    ticket: Ticket
    resource: str
    deadline_ns: int
    epoch: int
    possible_send: bool = False
    revoked: bool = False


class WriterFence:
    """Shared scheduler/writer metadata boundary; no I/O under this lock.

    ``check`` is the linearization point of *each* write, including cleanup.
    True marks a possible send before returning; STOP afterwards cannot recall
    it. Bind the same instance to the scheduler and every transport adapter.
    Registry incarnation retirement must call ``invalidate``; geometry or
    navigation changes must call ``publish_epoch`` before exposing the record.
    These revoke old work without clearing or inventing a user STOP.
    """

    def __init__(self, *, clock=None):
        self.metadata_lock = threading.RLock()
        self._clock = clock or time.monotonic_ns
        self._now_ns = 0
        self._aliases = {}
        self._epochs = {}
        self._invalidated = set()
        self._stopped_handles = set()
        self._stopped_resources = set()
        self._quarantined = set()
        self._permits = {}
        self._global_stop = False

    def bind(self, handle, resource, *, epoch=None):
        key = _handle_key(handle)
        if not isinstance(resource, str) or not resource.strip():
            raise InvalidRequest("canonical_resource must be a nonempty string")
        with self.metadata_lock:
            previous = self._aliases.get(key)
            if previous is not None and previous != resource:
                raise InvalidRequest("canonical resource changed within an incarnation")
            self._aliases[key] = resource
            if key in self._stopped_handles:
                self._stopped_resources.add(resource)
            if epoch is not None:
                self.publish_epoch(handle, epoch)

    def resource_for(self, handle):
        with self.metadata_lock:
            return self._aliases.get(_handle_key(handle))

    def publish_epoch(self, handle, epoch):
        key = _handle_key(handle)
        _integer(epoch, "epoch")
        with self.metadata_lock:
            previous = self._epochs.get(key)
            if previous is not None and epoch < previous:
                raise InvalidRequest("geometry epoch regressed")
            self._epochs[key] = epoch
            permit = self._permits.get(self._aliases.get(key))
            if (permit is not None and _handle_key(permit.ticket.handle) == key
                    and permit.epoch != epoch):
                permit.revoked = True
                if permit.possible_send:
                    self._quarantined.add(permit.resource)

    def invalidate(self, handle):
        key = _handle_key(handle)
        with self.metadata_lock:
            self._invalidated.add(key)
            permit = self._permits.get(self._aliases.get(key))
            if permit is not None and _handle_key(permit.ticket.handle) == key:
                permit.revoked = True
                if permit.possible_send:
                    self._quarantined.add(permit.resource)

    def advance(self, now_ns):
        _integer(now_ns, "now_ns")
        with self.metadata_lock:
            self._now_ns = max(self._now_ns, now_ns)
            return self._now_ns

    @property
    def now_ns(self):
        with self.metadata_lock:
            return self._now_ns

    def reason(self, handle, resource=None):
        key = _handle_key(handle)
        with self.metadata_lock:
            resource = resource or self._aliases.get(key)
            if self._global_stop or key in self._stopped_handles or resource in self._stopped_resources:
                return "STOPPED"
            if key in self._invalidated:
                return "STALE_IDENTITY"
            if resource in self._quarantined:
                return "QUARANTINED"
            return None

    def authorize(self, ticket, resource, deadline_ns, epoch):
        with self.metadata_lock:
            if (self.reason(ticket.handle, resource) or resource in self._permits
                    or self._now_ns >= deadline_ns
                    or self._epochs.get(_handle_key(ticket.handle)) != epoch):
                return None
            permit = _Permit(ticket, resource, deadline_ns, epoch)
            self._permits[resource] = permit
            return permit

    def check(self, handle, *, ticket=None, permit=None, now_ns=None):
        # The injected clock is a callback: sample it BEFORE acquiring metadata.
        now = self._clock() if now_ns is None else now_ns
        _integer(now, "clock")
        key = _handle_key(handle)
        with self.metadata_lock:
            self._now_ns = max(self._now_ns, now)
            resource = self._aliases.get(key)
            active = self._permits.get(resource)
            if (active is None or self.reason(handle, resource)
                    or _handle_key(active.ticket.handle) != key
                    or active.revoked or self._epochs.get(key) != active.epoch
                    or self._now_ns >= active.deadline_ns):
                return False
            if permit is not None and permit is not active:
                return False
            if ticket is not None and _ticket_key(ticket) != _ticket_key(active.ticket):
                return False
            active.possible_send = True
            return True

    def stop(self, handle=None):
        key = None if handle is None else _handle_key(handle)
        with self.metadata_lock:
            if key is None:
                self._global_stop = True
            else:
                self._stopped_handles.add(key)
                resource = self._aliases.get(key)
                if resource is not None:
                    self._stopped_resources.add(resource)

    def quarantine(self, resource):
        with self.metadata_lock:
            self._quarantined.add(resource)

    def retire(self, permit):
        with self.metadata_lock:
            if permit is not None and self._permits.get(permit.resource) is permit:
                del self._permits[permit.resource]

    def reconcile(self, resource):
        with self.metadata_lock:
            if resource in self._permits:
                raise InvalidRequest("cannot reconcile an active writer permit")
            self._quarantined.discard(resource)


@dataclass(frozen=True)
class Completion:
    """A terminal acknowledgment from the injected adapter, not a timeout guess."""
    action_id: str
    handle: Handle
    status: str
    acknowledged: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class DispatchContext:
    ticket: Ticket
    actor_lease: ActorLease
    focus_lease: FocusLease
    writer_fence: WriterFence
    deadline_ns: int
    _clock: object = field(repr=False)
    _permit: object = field(repr=False)

    def check(self):
        return self.writer_fence.check(self.ticket.handle, ticket=self.ticket,
                                       permit=self._permit, now_ns=self._clock())

    def send(self, callback, *args, **kwargs):
        if not self.check():
            raise FenceRefused("STOP, quarantine, retired permit or absolute deadline")
        return callback(*args, **kwargs)

    def remaining_ns(self):
        return max(0, self.deadline_ns - _integer(self._clock(), "clock"))


@dataclass
class _Job:
    request: ActionRequest
    ticket: Ticket
    resource: str
    sequence: int
    stamps: tuple
    state: str = "ADMITTING"
    reservation: object = None
    actor_lease: object = None
    focus_lease: object = None
    permit: _Permit | None = None
    receipt: Receipt | None = None
    cancelled: bool = False
    mode: str = "fair"
    round_id: int = 0
    quarantine_notified: bool = False
    admitted: bool = False


def _copy_frame(frame, handle, epoch):
    if _handle_key(frame.handle) != _handle_key(handle) or frame.epoch != epoch:
        raise InvalidRequest("frame identity or geometry epoch mismatch")
    _integer(frame.epoch, "frame epoch")
    seq = _integer(frame.frame_seq, "frame sequence")
    width, height = _integer(frame.width, "frame width", 1), _integer(frame.height, "frame height", 1)
    rgb = frame.rgb
    if (not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8
            or rgb.shape != (height, width, 3) or not rgb.flags.c_contiguous):
        raise InvalidRequest("owned contiguous uint8 RGB frame required")
    owned = rgb.copy(order="C")
    digest = sha256(owned.tobytes()).hexdigest()
    if not isinstance(frame.digest, str) or frame.digest != digest:
        raise InvalidRequest("frame content digest mismatch")
    owned.flags.writeable = False
    return FrameRef(handle, epoch, seq, width, height, owned, digest)


def _snapshot(request):
    try:
        handle = _handle(request.handle)
        action_id = request.action_id
        if not isinstance(action_id, str) or not action_id.strip():
            raise InvalidRequest("action_id must be nonempty")
        epoch = _integer(request.epoch, "epoch")
        cost = _integer(request.cost, "cost", 1)
        deadline = _integer(request.deadline_ns, "deadline_ns")
        if not isinstance(request.payload, dict):
            raise InvalidRequest("payload must be a dict")
        payload = deepcopy(request.payload)
        stamps = []
        if "observation" in request.payload:
            obs = request.payload["observation"]
            if _handle_key(obs.handle) != _handle_key(handle) or obs.epoch != epoch:
                raise InvalidRequest("observation identity or epoch mismatch")
            _integer(obs.epoch, "observation epoch")
            seq = _integer(obs.frame_seq, "observation sequence")
            dpr = _integer(obs.dpr_ppm, "dpr_ppm", 1)
            if not isinstance(obs.state, dict):
                raise InvalidRequest("observation state must be a dict")
            frame = _copy_frame(obs.frame, handle, epoch)
            if frame.frame_seq != seq:
                raise InvalidRequest("observation and frame sequences differ")
            payload["observation"] = Observation(handle, epoch, seq, dpr, deepcopy(obs.state), frame)
            stamps.append((seq, frame.digest))
        if "window_frames" in request.payload:
            frames = request.payload["window_frames"]
            if _handle_key(frames.handle) != _handle_key(handle) or frames.epoch != epoch:
                raise InvalidRequest("window frames identity or epoch mismatch")
            _integer(frames.epoch, "window frames epoch")
            dpr = _integer(frames.dpr_ppm, "dpr_ppm", 1)
            if not frames.frames or len(frames.frames) != len(frames.frame_seqs):
                raise InvalidRequest("window frames must be nonempty and paired with sequences")
            copied, previous = [], -1
            for frame, seq in zip(frames.frames, frames.frame_seqs):
                seq = _integer(seq, "local frame sequence")
                if seq <= previous:
                    raise InvalidRequest("local frame sequences must increase")
                if isinstance(frame, np.ndarray):
                    if (frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8
                            or not frame.flags.c_contiguous):
                        raise InvalidRequest("window frame must be contiguous uint8 RGB")
                    frame = FrameRef(handle, epoch, seq, frame.shape[1], frame.shape[0],
                                     frame, sha256(frame.tobytes()).hexdigest())
                owned = _copy_frame(frame, handle, epoch)
                if owned.frame_seq != seq:
                    raise InvalidRequest("window frame sequence mismatch")
                copied.append(owned)
                stamps.append((seq, owned.digest))
                previous = seq
            payload["window_frames"] = WindowFrames(handle, epoch, tuple(copied), tuple(frames.frame_seqs), dpr)
        return ActionRequest(handle, action_id, epoch, payload, cost, deadline), tuple(stamps)
    except AttributeError as exc:
        raise InvalidRequest("malformed request/evidence") from exc


class WindowScheduler:
    """Unit-target DRR with one bounded EDF promotion per reserved fair visit.

    Budget reservations debit global AND per-target credits on admission; only
    proven-unsent cancellation refunds them. Providers must be nonblocking and
    must never free an active or quarantined lease merely because its clock
    expires: only positive terminal/quiescence evidence permits reuse.
    ``step`` performs at most one preparation/dispatch, and drains receipts.
    """

    def __init__(self, *, registry, locks, focus_policy, budget, chain,
                 writer_fence, dispatch, clock=None, max_queue: int,
                 max_targets: int, service_cap_ns: int | None = None):
        self.max_queue = _integer(max_queue, "max_queue", 1)
        self.max_targets = _integer(max_targets, "max_targets", 1)
        if service_cap_ns is not None:
            _integer(service_cap_ns, "service_cap_ns", 1)
        self.service_cap_ns = service_cap_ns
        self.registry, self.locks, self.focus_policy = registry, locks, focus_policy
        self.budget, self.chain, self.writer_fence, self.dispatch = budget, chain, writer_fence, dispatch
        self._clock = clock or time.monotonic_ns
        self._lock = writer_fence.metadata_lock
        self._jobs, self._claims, self._finished, self._last_frames = {}, {}, {}, {}
        self._ring, self._receipts = deque(), deque()
        self._sequence, self._now_ns, self._round = 0, 0, 0
        self._fair_due, self._urgent_used = None, False
        self.fair_visits, self.eligibility_stalls = {}, {}
        self.urgent_dispatches = 0
        self.starvation_bound = 2 * self.max_targets
        self.diagnostics = []

    def _now(self, supplied=None):
        sample = _integer(self._clock(), "clock")
        if supplied is not None:
            sample = max(sample, _integer(supplied, "now_ns"))
        with self._lock:
            self._now_ns = self.writer_fence.advance(max(self._now_ns, sample))
            return self._now_ns

    def _prerequisite_error(self):
        if MISSING_PREREQUISITES and getattr(self.dispatch, "offline", False) is not True:
            return "AUSENTE: " + ", ".join(MISSING_PREREQUISITES)
        gates = getattr(self.dispatch, "prerequisite_gates", None)
        if not isinstance(gates, dict) or any(gates.get(name) is not True for name in PREREQUISITE_GATES):
            return "AUSENTE: prerequisite gates must be explicitly passed"
        if getattr(self.dispatch, "writer_fence", None) is not self.writer_fence:
            return "AUSENTE: dispatch must share the transport writer fence"
        required = ((self.registry, ("get", "canonical_resource")),
                    (self.locks, ("try_acquire", "release", "quarantine")),
                    (self.focus_policy, ("try_acquire", "release", "quarantine")),
                    (self.budget, ("reserve", "settle")), (self.chain, ("is_settled",)))
        if any(not callable(getattr(provider, name, None)) for provider, names in required for name in names):
            return "AUSENTE: required provider interface"
        return None

    def _current(self, request):
        record = self.registry.get(request.handle)
        if record is None or _handle_key(record.handle) != _handle_key(request.handle):
            return False
        return (record.closed is False and type(record.epoch) is int
                and record.epoch == request.epoch)

    def _emit_locked(self, job, status, reason):
        if job.receipt is None:
            job.receipt = Receipt(job.request.action_id, job.request.handle, status, reason)
            if job.admitted:
                self._receipts.append(job.receipt)

    def _end_round_locked(self, job):
        if self._fair_due is job:
            self._fair_due, self._urgent_used = None, False
            self._round += 1

    def _retire_locked(self, job):
        self.writer_fence.retire(job.permit)
        if self._claims.get(job.resource) is job:
            del self._claims[job.resource]
        self._jobs.pop(_ticket_key(job.ticket), None)
        self._finished[_ticket_key(job.ticket)] = job.receipt
        job.state = "DONE"
        self._end_round_locked(job)
        if not any(other.resource == job.resource for other in self._jobs.values()):
            self._ring = deque(resource for resource in self._ring if resource != job.resource)

    def submit(self, request):
        request, stamps = _snapshot(request)
        ticket = Ticket(request.action_id, request.handle)
        now = self._now()
        if self.service_cap_ns is None:
            return Receipt(request.action_id, request.handle, "CONFIG_REQUIRED", "service_cap_ns is required")
        error = self._prerequisite_error()
        if error:
            return Receipt(request.action_id, request.handle, "AUSENTE", error)
        if not self._current(request):
            return Receipt(request.action_id, request.handle, "REFUSED", "STALE_IDENTITY_OR_EPOCH")
        resource = self.registry.canonical_resource(request.handle)
        self.writer_fence.bind(request.handle, resource, epoch=request.epoch)
        now = self._now()
        key = _ticket_key(ticket)
        with self._lock:
            reason = self.writer_fence.reason(request.handle, resource)
            if reason:
                return Receipt(request.action_id, request.handle, "REFUSED", reason)
            if key in self._jobs or key in self._finished:
                return Receipt(request.action_id, request.handle, "REFUSED", "DUPLICATE_ACTION")
            if len(self._jobs) >= self.max_queue:
                return Receipt(request.action_id, request.handle, "REFUSED", "QUEUE_FULL")
            population = {job.resource for job in self._jobs.values()}
            if resource not in population and len(population) >= self.max_targets:
                return Receipt(request.action_id, request.handle, "REFUSED", "TARGET_LIMIT")
            ahead = sum(job.resource == resource for job in self._jobs.values())
            needed = self.starvation_bound * (ahead + 1) * self.service_cap_ns
            if request.deadline_ns - now < needed:
                return Receipt(request.action_id, request.handle, "REFUSED", "INFEASIBLE_DEADLINE")
            self._sequence += 1
            job = _Job(request, ticket, resource, self._sequence, stamps)
            self._jobs[key] = job  # Includes admission in all capacity/duplicate bounds.
            if resource not in self._ring:
                self._ring.append(resource)
        try:
            reservation = self.budget.reserve(request.handle, request.cost,
                action_id=request.action_id, deadline_ns=request.deadline_ns)
            with self._lock:
                job.reservation = reservation
            now = self._now()
            current = self._current(request)
            with self._lock:
                reason = self.writer_fence.reason(request.handle, resource)
                prior = self._last_frames.get((_handle_key(request.handle), request.epoch))
                stale = prior is not None and any(seq < prior[0] or (seq == prior[0] and digest != prior[1])
                                                   for seq, digest in stamps)
                infeasible = request.deadline_ns - now < needed
                if not (job.cancelled or reason or infeasible
                        or reservation is None or not current or stale):
                    job.state = "QUEUED"
                    job.admitted = True
                    if stamps:
                        self._last_frames[(_handle_key(request.handle), request.epoch)] = max(stamps)
                    return ticket
            reason = (reason or ("BUDGET_EXHAUSTED" if reservation is None else
                      "STALE_FRAME" if stale else "STALE_IDENTITY_OR_EPOCH" if not current else
                      "DEADLINE_EXPIRED" if now >= request.deadline_ns else
                      "INFEASIBLE_DEADLINE" if infeasible else "STOPPED"))
            self._finish(job, "REFUSED", reason, preparer=True)
        except Exception as exc:
            self._finish(job, "REFUSED", "ADMISSION_ERROR:" + type(exc).__name__, preparer=True)
            raise
        return job.receipt

    def _select(self):
        with self._lock:
            candidates = {}
            for job in self._jobs.values():
                if (job.state == "QUEUED" and not job.cancelled and job.resource not in self._claims
                        and not self.writer_fence.reason(job.request.handle, job.resource)):
                    old = candidates.get(job.resource)
                    # FIFO within one TARGET preserves the admission backlog
                    # bound; urgent EDF compares those target heads only.
                    if old is None or job.sequence < old.sequence:
                        candidates[job.resource] = job
            if self._fair_due is None:
                for _ in range(len(self._ring)):
                    resource = self._ring[0]
                    self._ring.rotate(-1)
                    if resource in candidates:
                        self._fair_due = candidates[resource]
                        self._urgent_used = False
                        break
            fair = self._fair_due
            if fair is None or fair.state != "QUEUED":
                return None
            job, mode = fair, "fair"
            if not self._urgent_used:
                urgent = min((j for r, j in candidates.items() if r != fair.resource),
                             key=lambda j: (j.request.deadline_ns, j.sequence), default=None)
                if urgent and (urgent.request.deadline_ns, urgent.sequence) < (fair.request.deadline_ns, fair.sequence):
                    job, mode = urgent, "urgent"
                    self._urgent_used = True
            if job.resource in self._claims:
                return None
            # Atomic test-and-claim. Injected methods are called only AFTER exit.
            self._claims[job.resource] = job
            job.state, job.mode, job.round_id = "PREPARING", mode, self._round
            if mode == "fair":
                key = _handle_key(job.request.handle)
                self.fair_visits[key] = self.fair_visits.get(key, 0) + 1
                # The unit TARGET visit is reserved here, before any provider
                # call. An independent target may prepare while this one is
                # busy; a late urgent preparation from the old round is revoked.
                self._end_round_locked(job)
            return job

    def _release_tokens(self, job, *, keep_budget=False):
        # Reverse acquisition order. Providers perform local bookkeeping only.
        for field_name, provider in (("focus_lease", self.focus_policy), ("actor_lease", self.locks)):
            token = getattr(job, field_name)
            if token is not None:
                provider.release(token)
                setattr(job, field_name, None)
        if not keep_budget and job.reservation is not None:
            consumed = job.permit is not None and job.permit.possible_send
            self.budget.settle(job.reservation, consumed=consumed)
            job.reservation = None

    def _notify_quarantine(self, job):
        with self._lock:
            if job.quarantine_notified:
                return
            job.quarantine_notified = True
        for provider, lease in ((self.locks, job.actor_lease), (self.focus_policy, job.focus_lease)):
            if lease is not None:
                try:
                    provider.quarantine(lease)
                except Exception as exc:
                    with self._lock:
                        self.diagnostics.append((job.ticket, "QUARANTINE_ERROR", type(exc).__name__))

    def _finish(self, job, status, reason, *, acknowledged=False, preparer=False):
        with self._lock:
            if job.state == "DONE":
                return
            possible = job.permit is not None and job.permit.possible_send
            # Completion and STOP/deadline share THIS linearization point, not
            # the earlier unlocked provider/clock observations in complete().
            if acknowledged:
                stopped = self.writer_fence.reason(job.request.handle, job.resource) == "STOPPED"
                expired = self.writer_fence.now_ns >= job.request.deadline_ns
                revoked = job.permit is not None and job.permit.revoked
                if stopped or expired or revoked:
                    status = "UNKNOWN" if possible else "STOPPED" if stopped else "REFUSED"
                    reason = "STOPPED" if stopped else "DEADLINE_EXPIRED" if expired else "STALE_IDENTITY_OR_EPOCH"
            if possible and not acknowledged:
                status = "UNKNOWN"
            self._emit_locked(job, status, reason)
            if job.state == "RELEASING":
                job.cancelled = True
                return
            if job.state in ("ADMITTING", "PREPARING") and not preparer:
                job.cancelled = True
                return
            self._end_round_locked(job)
            if possible and not acknowledged:
                job.state = "QUARANTINED"
                self.writer_fence.quarantine(job.resource)
                quarantine = True
            else:
                job.state = "RELEASING"
                # Keep the shared claim published across unlocked provider
                # release/accounting. A second scheduler may acquire a freed
                # actor token but cannot authorize until this transaction ends.
                if job.permit is not None:
                    job.permit.revoked = True
                quarantine = False
        if quarantine:
            self._notify_quarantine(job)
            return
        try:
            self._release_tokens(job)
        except Exception as exc:
            with self._lock:
                job.state = "QUARANTINED"
                self.writer_fence.quarantine(job.resource)
                self.diagnostics.append((job.ticket, "RELEASE_ERROR", type(exc).__name__))
            self._notify_quarantine(job)
            return
        with self._lock:
            self._retire_locked(job)
            # Only an explicit terminal acknowledgment can remove quarantine.
            if acknowledged:
                self.writer_fence.reconcile(job.resource)

    def _retry(self, job, reason):
        with self._lock:
            key = _handle_key(job.request.handle)
            self.eligibility_stalls[key] = self.eligibility_stalls.get(key, 0) + 1
            job.state = "RELEASING"
        try:
            self._release_tokens(job, keep_budget=True)
        except Exception as exc:
            with self._lock:
                job.state = "QUARANTINED"
                self.writer_fence.quarantine(job.resource)
                self._end_round_locked(job)
                self._emit_locked(job, "UNKNOWN", "OWNERSHIP_RELEASE_ERROR:" + type(exc).__name__)
            self._notify_quarantine(job)
            return
        now = self._now()
        with self._lock:
            if self._claims.get(job.resource) is job:
                del self._claims[job.resource]
            self._end_round_locked(job)
            job.state = "QUEUED"
            blocked = self.writer_fence.reason(job.request.handle, job.resource)
            cancel = job.cancelled or blocked or now >= job.request.deadline_ns
        if cancel:
            self._finish(job, "REFUSED", blocked or (
                "DEADLINE_EXPIRED" if now >= job.request.deadline_ns else "STOPPED"))

    def _invalid_lease(self, job, reason):
        # A returned foreign capability is never ours to release/quarantine.
        # Retain the requested resource claim pending positive reconciliation.
        with self._lock:
            job.state = "QUARANTINED"
            self.writer_fence.quarantine(job.resource)
            self._end_round_locked(job)
            self._emit_locked(job, "REFUSED", reason)
            self.diagnostics.append((job.ticket, reason, "provider returned an invalid capability"))
        self._notify_quarantine(job)

    def _prepare(self, job):
        request = job.request
        try:
            if not self._current(request):
                self._finish(job, "REFUSED", "STALE_IDENTITY_OR_EPOCH", preparer=True)
                return None
            settled = self.chain.is_settled(request.handle, request.epoch)
            if settled is not True:
                self._retry(job, "UNSETTLED")
                return None
            lease = self.locks.try_acquire(request.handle, owner=request.action_id,
                                          expires_ns=request.deadline_ns)
            if lease is None:
                self._retry(job, "ACTOR_BUSY")
                return None
            try:
                valid_actor = (_handle_key(lease.handle) == _handle_key(request.handle)
                    and lease.owner == request.action_id and type(lease.generation) is int
                    and lease.generation >= 0 and isinstance(lease.nonce, bytes) and bool(lease.nonce)
                    and type(lease.expires_ns) is int and lease.expires_ns >= request.deadline_ns)
            except (AttributeError, InvalidRequest):
                valid_actor = False
            if not valid_actor:
                self._invalid_lease(job, "INVALID_ACTOR_LEASE")
                return None
            job.actor_lease = lease
            focus = self.focus_policy.try_acquire(lease, epoch=request.epoch)
            if focus is None:
                self._retry(job, "FOCUS_BUSY")
                return None
            try:
                valid_focus = (_handle_key(focus.lease.handle) == _handle_key(request.handle)
                    and focus.lease.nonce == lease.nonce and focus.lease.owner == lease.owner
                    and type(focus.lease.generation) is int and focus.lease.generation == lease.generation
                    and type(focus.lease.expires_ns) is int and focus.lease.expires_ns == lease.expires_ns
                    and type(focus.epoch) is int and focus.epoch == request.epoch
                    and isinstance(focus.domain, str) and bool(focus.domain.strip()))
            except (AttributeError, InvalidRequest):
                valid_focus = False
            if not valid_focus:
                self._invalid_lease(job, "INVALID_FOCUS_LEASE")
                return None
            job.focus_lease = focus
            current = self._current(request)
            ready = self.chain.is_settled(request.handle, request.epoch)
            prerequisite = self._prerequisite_error()
            now = self._now()
            with self._lock:
                reason = self.writer_fence.reason(request.handle, job.resource)
                expired = now >= request.deadline_ns
                infeasible = request.deadline_ns - now < self.service_cap_ns
                stale_urgent = job.mode == "urgent" and job.round_id != self._round
                cancelled = job.cancelled or reason or infeasible
                if not (cancelled or not current or ready is not True or prerequisite or stale_urgent):
                    permit = self.writer_fence.authorize(job.ticket, job.resource,
                                                         request.deadline_ns, request.epoch)
                    if permit is not None and job.reservation is not None:
                        job.permit, job.state = permit, "ACTIVE"
                        if job.mode == "urgent":
                            self.urgent_dispatches += 1
                        self._end_round_locked(job)
                        return DispatchContext(job.ticket, lease, focus, self.writer_fence,
                                               request.deadline_ns, self._now, permit)
            if cancelled or not current or prerequisite:
                self._finish(job, "AUSENTE" if prerequisite else "REFUSED",
                             prerequisite or reason or ("DEADLINE_EXPIRED" if expired else
                             "INFEASIBLE_DEADLINE" if infeasible else
                             "STALE_IDENTITY_OR_EPOCH" if not current else "STOPPED"), preparer=True)
            else:
                self._retry(job, "ELIGIBILITY_CHANGED")
        except Exception as exc:
            self._finish(job, "REFUSED", "PREPARATION_ERROR:" + type(exc).__name__, preparer=True)
            with self._lock:
                self.diagnostics.append((job.ticket, "PREPARATION_ERROR", str(exc)))
        return None

    def _expire(self, now):
        with self._lock:
            expired = [j for j in self._jobs.values() if (now >= j.request.deadline_ns
                       or (j.permit is not None and j.permit.revoked))
                       and j.state not in ("DONE", "QUARANTINED", "RELEASING")]
        for job in expired:
            self._finish(job, "REFUSED", "DEADLINE_EXPIRED" if now >= job.request.deadline_ns
                         else "STALE_IDENTITY_OR_EPOCH")

    def step(self, now_ns: int):
        self._expire(self._now(now_ns))
        job = self._select()
        if job is not None:
            context = self._prepare(job)
            if context is not None:
                try:
                    result = self.dispatch(job.request, context)
                    if result is not None:
                        self.complete(job.ticket, result)
                except Exception as exc:
                    self._finish(job, "REFUSED", "DISPATCH_ERROR:" + type(exc).__name__)
                self._expire(self._now())
        with self._lock:
            receipts = list(self._receipts)
            self._receipts.clear()
            return receipts

    def complete(self, ticket, result) -> None:
        key = _ticket_key(ticket)
        try:
            if _ticket_key(result) != key:
                raise InvalidRequest("completion identity does not match ticket")
            status = result.status
            if status not in ("SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"):
                raise InvalidRequest("completion must have an explicit terminal status")
        except AttributeError as exc:
            raise InvalidRequest("malformed completion") from exc
        now = self._now()
        with self._lock:
            job = self._jobs.get(key)
            if job is None:
                if key in self._finished:
                    return
                raise InvalidRequest("unknown ticket")
            if (job.permit is None
                    or job.state not in ("ACTIVE", "QUARANTINED", "RELEASING")):
                raise InvalidRequest("ticket was not dispatched")
            acknowledged = getattr(result, "acknowledged", False) is True and status != "UNKNOWN"
            blocked = self.writer_fence.reason(job.request.handle, job.resource)
            expired = now >= job.request.deadline_ns
        if expired or blocked == "STOPPED":
            self._finish(job, "UNKNOWN", "DEADLINE_EXPIRED" if expired else "STOPPED")
        self._finish(job, status if acknowledged else "UNKNOWN",
                     getattr(result, "reason", None) if acknowledged else "MISSING_TERMINAL_ACK",
                     acknowledged=acknowledged)

    def stop(self, handle=None) -> None:
        if handle is not None:
            handle = _handle(handle)
            # A retired registry entry must not prevent fencing a retained job.
            self.writer_fence.stop(handle)
            resource = self.writer_fence.resource_for(handle)
            if resource is None:
                resource = self.registry.canonical_resource(handle)
                self.writer_fence.bind(handle, resource)
        else:
            resource = None
        with self._lock:
            self.writer_fence.stop(handle)
            jobs = [job for job in self._jobs.values() if handle is None or job.resource == resource]
        for job in jobs:
            self._finish(job, "STOPPED", "STOPPED")

    def reconcile(self, handle: Handle, *, acknowledged: bool = False) -> None:
        handle = _handle(handle)
        if acknowledged is not True:
            return
        with self._lock:
            jobs = [job for job in self._jobs.values()
                    if _handle_key(job.request.handle) == _handle_key(handle)
                    and (job.state == "QUARANTINED" or
                         (job.state == "ACTIVE" and job.permit is not None and job.permit.revoked))]
        for job in jobs:
            self._finish(job, "UNKNOWN", "RECONCILED_QUIESCENT", acknowledged=True)
