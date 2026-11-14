# @module {"id":"wa.act.godot_window_driver","layer":2,"owner":"ACT","deps":["wa.act.godot_executor","wa.perceive.godot"],"book":"HB-WA-s2-act"}
"""Fenced, per-incarnation Godot T2 driver. Local admission is NEVER game completion.

Unmerged session/target/lock/scheduler providers are composed through explicit
structural contracts, never by inventing default permissive implementations.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import re
import threading
import time
import weakref
from typing import Any

from ..perceive.godot import read_outbox_batches, validate_uitree
from ..state.schema import dumps, validate_state

try:
    from ..perceive.session_registry import (  # canonical records once landed
        Handle, FrameRef, Observation, Receipt, ActionRequest, ActorLease, FocusLease,
    )
    _SESSION_RECORDS_PRESENT = True
except ModuleNotFoundError as exc:
    if exc.name != "wender.perceive.session_registry":
        raise  # an internal import failure is not a missing provider
    _SESSION_RECORDS_PRESENT = False

    @dataclass(frozen=True)
    class Handle:
        kind: str
        canonical_id: str
        generation: int

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

try:
    from ..perceive.godot_window_target import GodotWindowTarget
except ModuleNotFoundError as exc:
    if exc.name != "wender.perceive.godot_window_target":
        raise
    GodotWindowTarget = None  # structural offline fixture only
try:
    from .target_lock_registry import TargetLockRegistry
except ModuleNotFoundError as exc:
    if exc.name != "wender.act.target_lock_registry":
        raise
    TargetLockRegistry = None
try:
    from .window_scheduler import WindowScheduler
except ModuleNotFoundError as exc:
    if exc.name != "wender.act.window_scheduler":
        raise
    WindowScheduler = None

SUPPORTED_KINDS = frozenset({"cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"})
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
MAX_TICK_TRAIL_PER_FLUSH = 24  # TARGET, not a producer-side guarantee
MAX_RGB_BYTES = 64 * 1024 * 1024
# An executor owns exactly one command cursor, so it cannot serve two drivers.
_EXECUTOR_BINDINGS = weakref.WeakKeyDictionary()
_EXECUTOR_BINDINGS_GUARD = threading.Lock()


class GodotDriverError(ValueError):
    """Invalid target, request or protocol binding."""


class GodotProtocolError(GodotDriverError):
    """The T2 decoder returned an invalid or oversized complete flush."""


class PrerequisiteUnavailable(GodotDriverError):
    """A production prerequisite has not landed or is not implemented."""


def _integer(value: Any, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _handle_key(value: Any) -> tuple[str, str, int] | None:
    kind = getattr(value, "kind", None)
    cid = getattr(value, "canonical_id", None)
    gen = getattr(value, "generation", None)
    if (kind not in SUPPORTED_KINDS or not isinstance(cid, str) or not cid
            or not _integer(gen)):
        return None
    return kind, cid, gen


def _same_handle(a: Any, b: Any) -> bool:
    ka = _handle_key(a)
    return ka is not None and ka == _handle_key(b)


def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        if name not in obj:
            raise GodotDriverError(f"missing {name} in target/envelope")
        return obj[name]
    if not hasattr(obj, name):
        raise GodotDriverError(f"missing {name} in target/envelope")
    return getattr(obj, name)


def _basis_digest(base: dict) -> str:
    return hashlib.sha256(dumps(base).encode("utf-8")).hexdigest()


def _check_frame(observation: Any, handle: Any, epoch: int, frame_seq: int, basis: str) -> bool:
    """Checks owned RGB evidence without changing it or treating it as native capture."""
    if (not _same_handle(observation.handle, handle)
            or observation.epoch != epoch or observation.frame_seq != frame_seq
            or not _integer(observation.dpr_ppm, 1)
            or not isinstance(observation.state, dict)
            or validate_state(observation.state)
            or _basis_digest(observation.state) != basis):
        return False
    frame = observation.frame
    if (not _same_handle(frame.handle, handle) or frame.epoch != epoch
            or frame.frame_seq != frame_seq
            or not _integer(frame.width, 1) or not _integer(frame.height, 1)
            or frame.width * frame.height * 3 > MAX_RGB_BYTES
            or not isinstance(frame.digest, str) or not _HEX64.fullmatch(frame.digest)):
        return False
    rgb = frame.rgb
    try:
        import numpy as np
        if isinstance(rgb, np.ndarray):
            if (rgb.dtype != np.uint8 or not rgb.flags.c_contiguous
                    or rgb.flags.writeable or rgb.shape != (frame.height, frame.width, 3)):
                return False
            raw = rgb.tobytes()
        elif isinstance(rgb, bytes):
            raw = rgb
        elif (isinstance(rgb, memoryview) and rgb.readonly and rgb.c_contiguous
              and isinstance(rgb.obj, bytes)):
            raw = bytes(rgb)
        else:
            return False
    except (TypeError, ValueError):
        return False
    return len(raw) == frame.width * frame.height * 3 and hashlib.sha256(raw).hexdigest() == frame.digest


@dataclass
class _Pending:
    action_id: str
    handle: Any
    epoch: int
    seq: int
    tick: int
    frame_seq: int
    basis_sha256: str
    payload_digest: str
    resource: str
    lock_claim: object
    fence_claim: object


class GodotWindowDriver:
    """Serial window actor with explicit scheduler, lock and focus contracts.

    Required injected interfaces (no no-op defaults):
      target.snapshot() -> handle, epoch, committed_tick, basis_sha256,
        base_state, sidecar, frame_seq, actuation_resource
      locks.try_acquire(resource, lease) -> claim|None; locks.release(resource, claim)
      writer_fence.try_reserve(handle, lease, request, now_ns) -> claim|None;
        writer_fence.release(claim)
      focus_policy.authorize(handle, lease, focus) -> bool
      executor.dispatch(command, committed_tick, basis_sha256) -> dict;
        executor.next_seq -> int
    """

    def __init__(self, target: Any, executor: Any, *, locks: Any,
                 focus_policy: Any, writer_fence: Any, clock: Any = None) -> None:
        # No missing provider is replaced by an always-allow implementation.
        # Concrete injected adapters must satisfy every operation below. This
        # permits the specified structural contracts while providers are merging;
        # missing actual behavior still fails closed before dispatch.
        for obj, methods in ((target, ("snapshot",)),
                             (executor, ("dispatch",)),
                             (locks, ("try_acquire", "release")),
                             (focus_policy, ("authorize",)),
                             (writer_fence, ("try_reserve", "release"))):
            if any(not callable(getattr(obj, m, None)) for m in methods):
                raise PrerequisiteUnavailable(f"missing required injected method {methods}")
        if not _integer(getattr(executor, "next_seq", None), 1):
            raise PrerequisiteUnavailable("executor must expose public next_seq")
        self.target = target
        self.executor = executor
        self.locks = locks
        self.focus_policy = focus_policy
        self.writer_fence = writer_fence
        self.clock = clock if clock is not None else time.monotonic_ns
        if not callable(self.clock):
            raise GodotDriverError("clock must be callable")
        self._guard = threading.Lock()
        self._operating = False
        self._next_seq = 1
        self._committed_tick: int | None = None
        self._basis_sha256: str | None = None
        self._pending: _Pending | None = None
        self._pending_bytes = b""
        self._validated_batches: list[tuple[dict, ...]] = []
        self._tick_trail: list[dict] = []
        self._last_order_seq_id = 0
        self._quarantined = False
        try:
            with _EXECUTOR_BINDINGS_GUARD:
                previous = _EXECUTOR_BINDINGS.get(executor)
                if previous is not None and previous() is not None:
                    raise GodotDriverError("GodotExecutor already bound to another window driver")
                _EXECUTOR_BINDINGS[executor] = weakref.ref(self)
        except TypeError as exc:
            raise PrerequisiteUnavailable("executor must support weakref ownership") from exc

    @property
    def next_seq(self) -> int:
        return self._next_seq

    @property
    def committed_tick(self) -> int | None:
        return self._committed_tick

    @property
    def basis_sha256(self) -> str | None:
        return self._basis_sha256

    @property
    def pending_bytes(self) -> bytes:
        return self._pending_bytes

    @property
    def tick_trail(self) -> tuple[dict, ...]:
        return tuple(copy.deepcopy(self._tick_trail))

    @property
    def validated_batches(self) -> tuple[tuple[dict, ...], ...]:
        return tuple(copy.deepcopy(self._validated_batches))

    def drain_tick_trail(self) -> tuple[dict, ...]:
        """Explicit consumer drain; no automatic truncation of tick stamps."""
        with self._guard:
            if self._operating:
                raise GodotDriverError("driver busy")
            result = tuple(copy.deepcopy(self._tick_trail))
            self._tick_trail.clear()
            return result

    def _enter(self) -> bool:
        with self._guard:
            if self._operating:
                return False
            self._operating = True
            return True

    def _leave(self) -> None:
        with self._guard:
            self._operating = False

    @staticmethod
    def _receipt(request: Any, status: str, reason: str | None = None) -> Receipt:
        return Receipt(request.action_id, request.handle, status, reason)

    def _snapshot(self) -> dict:
        raw = self.target.snapshot()
        snap = {k: _field(raw, k) for k in (
            "handle", "epoch", "committed_tick", "basis_sha256", "base_state",
            "sidecar", "frame_seq", "actuation_resource")}
        if (_handle_key(snap["handle"]) is None or snap["handle"].kind != "godot_window"
                or not _integer(snap["epoch"]) or not _integer(snap["frame_seq"], 1)
                or not _integer(snap["committed_tick"]) or not isinstance(snap["actuation_resource"], str)
                or not snap["actuation_resource"].strip()
                or not isinstance(snap["basis_sha256"], str)
                or not _HEX64.fullmatch(snap["basis_sha256"])):
            raise GodotDriverError("invalid Godot target snapshot metadata")
        base = copy.deepcopy(snap["base_state"])
        sidecar = copy.deepcopy(snap["sidecar"])
        base_errors = validate_state(base)
        if base_errors:
            raise GodotDriverError("invalid base state: " + "; ".join(base_errors))
        sidecar_errors = validate_uitree(base, sidecar)
        if sidecar_errors:
            raise GodotDriverError("invalid UITree sidecar: " + "; ".join(sidecar_errors))
        if _basis_digest(base) != snap["basis_sha256"]:
            raise GodotDriverError("target basis digest does not match validated base state")
        return snap

    def _check_lease(self, request: Any, lease: Any, focus: Any, now: int) -> str | None:
        if (_handle_key(request.handle) is None or request.handle.kind != "godot_window"
                or not isinstance(request.action_id, str) or not request.action_id
                or not _integer(request.epoch) or not _integer(request.cost)
                or not _integer(request.deadline_ns, 1)):
            return "REQUEST_INVALID"
        if (not _same_handle(request.handle, lease.handle)
                or not _integer(lease.generation)
                or lease.generation != request.handle.generation
                or not isinstance(lease.owner, str) or not lease.owner
                or not isinstance(lease.nonce, bytes) or not lease.nonce
                or not _integer(lease.expires_ns, 1)
                or min(lease.expires_ns, request.deadline_ns) <= now):
            return "ACTOR_LEASE_INVALID"
        if focus is not None and (
                not _same_handle(focus.lease.handle, lease.handle)
                or focus.lease.nonce != lease.nonce
                or focus.lease.owner != lease.owner
                or focus.epoch != request.epoch
                or not isinstance(focus.domain, str) or not focus.domain):
            return "FOCUS_LEASE_INVALID"
        return None

    def _command(self, request: Any, snapshot: dict) -> tuple[dict, str] | str:
        payload = request.payload
        if not isinstance(payload, dict) or set(payload) - {
                "cmd", "args", "tick", "basis_sha256", "frame_seq", "observation"}:
            return "COMMAND_METADATA"
        if not {"cmd", "args", "tick", "basis_sha256", "frame_seq"}.issubset(payload):
            return "COMMAND_METADATA"
        if (not isinstance(payload["cmd"], str) or not payload["cmd"]
                or not isinstance(payload["args"], dict)
                or not _integer(payload["tick"]) or not _integer(payload["frame_seq"], 1)):
            return "COMMAND_INVALID"
        if payload["basis_sha256"] != snapshot["basis_sha256"]:
            return "BASIS_MISMATCH"
        if payload["frame_seq"] != snapshot["frame_seq"]:
            return "FRAME_MISMATCH"
        if "observation" in payload:
            try:
                frame_ok = _check_frame(payload["observation"], request.handle,
                                        request.epoch, payload["frame_seq"], snapshot["basis_sha256"])
            except (AttributeError, ValueError, TypeError):
                frame_ok = False
            if not frame_ok:
                return "FRAME_BINDING"
        record = {"args": copy.deepcopy(payload["args"]), "cmd": payload["cmd"],
                  "seq": self._next_seq, "tick": payload["tick"]}
        try:
            # Validate a canonical, serializable command; the executor remains
            # authoritative about supported commands and trusted policies.
            from ..perceive.godot import validate_inbox_record
            validate_inbox_record(record)
            bound = {"record": record, "basis_sha256": payload["basis_sha256"],
                     "frame_seq": payload["frame_seq"],
                     "frame_digest": (payload["observation"].frame.digest
                                      if "observation" in payload else None)}
            fingerprint = hashlib.sha256(dumps(bound).encode("utf-8")).hexdigest()
        except (ValueError, TypeError, UnicodeError):
            return "COMMAND_INVALID"
        return record, fingerprint

    def _release(self, pending: _Pending) -> None:
        try:
            self.locks.release(pending.resource, pending.lock_claim)
        finally:
            self.writer_fence.release(pending.fence_claim)

    def execute(self, request: ActionRequest, *, lease: ActorLease,
                focus: FocusLease | None) -> Receipt:
        """Try one fenced Godot action; 'pending' is local admission, NOT applied."""
        if not self._enter():
            return self._receipt(request, "rejected", "DRIVER_BUSY")
        fence_claim = None
        lock_claim = None
        resource = None
        dispatched = False
        try:
            if self._quarantined:
                return self._receipt(request, "rejected", "QUARANTINED")
            if self._pending is not None:
                return self._receipt(request, "rejected", "ACK_PENDING")
            if len(self._tick_trail) >= MAX_TICK_TRAIL_PER_FLUSH:
                return self._receipt(request, "rejected", "TICK_TRAIL_BACKPRESSURE")
            now = self.clock()
            reason = self._check_lease(request, lease, focus, now)
            if reason:
                return self._receipt(request, "rejected", reason)
            snapshot = self._snapshot()
            if not _same_handle(request.handle, snapshot["handle"]) or request.epoch != snapshot["epoch"]:
                return self._receipt(request, "rejected", "TARGET_INCARNATION_OR_EPOCH")
            if self._committed_tick is not None and (
                    self._committed_tick != snapshot["committed_tick"]
                    or self._basis_sha256 != snapshot["basis_sha256"]):
                return self._receipt(request, "rejected", "STALE_TARGET_STATE")
            if self._next_seq != self.executor.next_seq:
                return self._receipt(request, "rejected", "SEQUENCE_DESYNC")
            command = self._command(request, snapshot)
            if isinstance(command, str):
                return self._receipt(request, "rejected", command)
            record, fingerprint = command
            if getattr(self.executor, "transport_enabled", None) is False:
                return self._receipt(request, "rejected", "TRANSPORT_OFF")
            resource = snapshot["actuation_resource"]
            fence_claim = self.writer_fence.try_reserve(request.handle, lease, request, now)
            if fence_claim is None or fence_claim is False:
                return self._receipt(request, "rejected", "SCHEDULER_BUSY")
            lock_claim = self.locks.try_acquire(resource, lease)
            if lock_claim is None or lock_claim is False:
                return self._receipt(request, "rejected", "ACTOR_BUSY")
            if not self.focus_policy.authorize(request.handle, lease, focus):
                return self._receipt(request, "rejected", "FOCUS_DENIED")
            # No metadata mutex is held across admission or transport.
            dispatched = True
            decision = self.executor.dispatch(record, snapshot["committed_tick"], snapshot["basis_sha256"])
            if not isinstance(decision, dict):
                raise GodotDriverError("executor returned non-dict admission")
            if decision.get("status") != "admitted":
                if decision.get("reason") == "NEEDS_CONFIRM":
                    return self._receipt(request, "proposed", "NEEDS_CONFIRM")
                return self._receipt(request, "rejected", str(decision.get("reason", "EXECUTOR_REFUSED")))
            if (decision.get("seq") != self._next_seq or decision.get("tick") != record["tick"]
                    or self.executor.next_seq != self._next_seq + 1):
                raise GodotDriverError("executor admitted a mismatched sequence or tick")
            self._pending = _Pending(request.action_id, request.handle, request.epoch,
                                     self._next_seq, record["tick"], snapshot["frame_seq"],
                                     snapshot["basis_sha256"], fingerprint, resource,
                                     lock_claim, fence_claim)
            lock_claim = None  # claims are retained until authoritative T2 ack
            fence_claim = None
            if self._committed_tick is None:
                self._committed_tick = snapshot["committed_tick"]
                self._basis_sha256 = snapshot["basis_sha256"]
            self._next_seq += 1
            return self._receipt(request, "pending", "AWAITING_T2")
        except Exception:
            if dispatched:
                # Transport/admission exceptions are ambiguous; never replay.
                self._quarantined = True
                if lock_claim is not None and fence_claim is not None:
                    self._pending = _Pending(request.action_id, request.handle, request.epoch,
                                             self._next_seq, request.payload.get("tick", 0),
                                             request.payload.get("frame_seq", 0),
                                             self._basis_sha256 or "", "", resource,
                                             lock_claim, fence_claim)
                    lock_claim = fence_claim = None
            raise
        finally:
            if lock_claim is not None and resource is not None:
                self.locks.release(resource, lock_claim)
            if fence_claim is not None:
                self.writer_fence.release(fence_claim)
            self._leave()

    def confirm_completion(self, request: ActionRequest, outbox: object) -> Receipt:
        """Accept only a validated T2 outcome bound to this pending command and target.

        outbox is a session-owned mapping/object with handle, epoch, basis_sha256,
        frame_seq, data (bytes) and decode_batches (injected decoder). The native
        Holdings envelope and viewport capture are not implied by this contract.
        """
        if not self._enter():
            return self._receipt(request, "rejected", "DRIVER_BUSY")
        try:
            pending = self._pending
            if pending is None:
                return self._receipt(request, "rejected", "NO_PENDING")
            if (request.action_id != pending.action_id or not _same_handle(request.handle, pending.handle)
                    or request.epoch != pending.epoch):
                return self._receipt(request, "rejected", "REQUEST_MISMATCH")
            command = self._command_for_recheck(request, pending.seq)
            if command != pending.payload_digest:
                return self._receipt(request, "rejected", "REQUEST_MUTATED")
            if (not _same_handle(_field(outbox, "handle"), pending.handle)
                    or _field(outbox, "epoch") != pending.epoch
                    or _field(outbox, "basis_sha256") != pending.basis_sha256
                    or _field(outbox, "frame_seq") != pending.frame_seq):
                self._quarantined = True
                return self._receipt(request, "quarantined", "T2_IDENTITY_OR_FRAME_MISMATCH")
            chunk = _field(outbox, "data")
            decoder = _field(outbox, "decode_batches")
            if not isinstance(chunk, (bytes, bytearray, memoryview)) or not callable(decoder):
                raise GodotProtocolError("T2 envelope needs bytes and an injected decoder")
            try:
                validated = read_outbox_batches(self._pending_bytes + bytes(chunk), decoder)
            except ValueError as exc:
                self._quarantined = True
                raise GodotProtocolError(f"invalid complete T2 batch: {exc}") from exc
            # Never silently trim tick records. Each received flush has a hard
            # target bound; a producer without flush control cannot be certified.
            received = sum(len(batch) for batch in validated.batches)
            if received > MAX_TICK_TRAIL_PER_FLUSH or any(
                    len(batch) > MAX_TICK_TRAIL_PER_FLUSH for batch in validated.batches):
                self._quarantined = True
                raise GodotProtocolError("oversized T2 flush (>24 records)")
            self._pending_bytes = validated.pending
            for batch in validated.batches:
                self._validated_batches.append(tuple(copy.deepcopy(batch)))
                self._tick_trail.extend(copy.deepcopy(rec) for rec in batch if "requested_tick" in rec)
            outcomes = [rec for batch in validated.batches for rec in batch
                        if "requested_tick" in rec and "applied_tick" in rec]
            if not outcomes:
                self._quarantined = True  # no acknowledgment: retain claim and refuse re-dispatch
                return self._receipt(request, "pending", "AWAITING_T2")
            if (len(outcomes) != 1 or outcomes[0]["seq"] != pending.seq
                    or outcomes[0]["requested_tick"] != pending.tick
                    or outcomes[0]["order_seq_id"] <= self._last_order_seq_id
                    or (outcomes[0]["status"] == "accepted"
                        and outcomes[0]["applied_tick"] < pending.tick)):
                self._quarantined = True
                return self._receipt(request, "quarantined", "T2_SEQUENCE_OR_TICK_MISMATCH")
            outcome = outcomes[0]
            self._last_order_seq_id = outcome["order_seq_id"]
            if outcome["status"] == "accepted":
                self._committed_tick = outcome["applied_tick"]
                self._basis_sha256 = outcome["state_sha256"]
                status, reason = "applied", None
            elif outcome["status"] == "proposed":
                status, reason = "proposed", outcome["reason"]
            else:
                status, reason = "rejected", outcome["reason"] or "ENGINE_REJECTED"
            self._pending = None
            self._release(pending)
            if self._pending_bytes:
                self._quarantined = True
            else:
                self._quarantined = False
            return self._receipt(request, status, reason)
        finally:
            self._leave()

    @staticmethod
    def _command_for_recheck(request: ActionRequest, seq: int) -> str | None:
        payload = request.payload
        if not isinstance(payload, dict) or not all(k in payload for k in ("cmd", "args", "tick")):
            return None
        try:
            record = {"args": copy.deepcopy(payload["args"]), "cmd": payload["cmd"],
                      "seq": seq, "tick": payload["tick"]}
            bound = {"record": record, "basis_sha256": payload.get("basis_sha256"),
                     "frame_seq": payload.get("frame_seq"),
                     "frame_digest": (payload["observation"].frame.digest
                                      if "observation" in payload else None)}
            return hashlib.sha256(dumps(bound).encode("utf-8")).hexdigest()
        except (AttributeError, KeyError, ValueError, TypeError, UnicodeError):
            return None
