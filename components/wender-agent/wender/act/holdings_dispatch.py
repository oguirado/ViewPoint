# @module {"id":"wa.act.holdings_dispatch","layer":2,"owner":"ACT","deps":["wa.act.target_lock_registry"],"book":"HB-WA-s2-act"}
"""Fail-closed, source-agnostic Holdings dispatch with quarantined leases.

The request, observation, record and receipt retain their upstream structural
contracts. No pending scheduler, focus adapter, transport or driver is imported.
The caller supplies the authoritative callbacks and all time limits explicitly.
"""

from dataclasses import dataclass, replace
import re

from wender.act.target_lock_registry import TargetBusy, TargetLockRefused


class DispatchRefused(ValueError):
    """No transport send is permitted by the current authoritative evidence."""


class DispatchStopped(DispatchRefused):
    """STOP has been observed at a dispatch fence."""


@dataclass(frozen=True)
class ObservedTarget:
    """Volatile pair of a canonical Observation and its monotonic capture time."""
    observation: object
    captured_ns: int


@dataclass(frozen=True)
class LaneAction:
    request: object
    source_policy: str
    submission_seq: int


@dataclass(frozen=True)
class DispatchEvent:
    action_id: str
    event: str
    submission_seq: int
    reason: str | None
    receipt_ref: str | None
    generation: int
    epoch: int
    frame_seq: int | None


@dataclass(frozen=True)
class DispatchResult:
    request: object
    submission_seq: int
    sent: bool
    pending: bool
    acknowledged: bool
    lease: object | None
    receipt: object | None
    events: tuple


_EVENTS = frozenset({"OBSERVED", "ELIGIBLE", "DISPATCHED", "ACKNOWLEDGED",
                     "DROPPED", "QUARANTINED", "STOPPED"})
_POLICY_KINDS = {
    "cdp": frozenset({"cdp_tab"}),
    "brokered_ipc": frozenset({"godot_window", "capsule", "xvfb_screen", "x11_window"}),
}
_SUPPORTED_KINDS = frozenset().union(*_POLICY_KINDS.values())
_SOURCE_KINDS = {"cdp": "cdp", "brokered_ipc": "godot"}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _number(name, value, *, positive=False):
    if type(value) is not int or value < (1 if positive else 0):
        raise DispatchRefused("INVALID:%s" % name)
    return value


def _handle(handle):
    try:
        kind, ident, generation = handle.kind, handle.canonical_id, handle.generation
    except AttributeError as exc:
        raise DispatchRefused("INVALID:handle") from exc
    if (kind not in _SUPPORTED_KINDS
            or not isinstance(ident, str) or not ident
            or ident != ident.strip() or "\x00" in ident):
        raise DispatchRefused("INVALID:handle")
    return (kind, ident, _number("generation", generation, positive=True))


def _event(request, seq, kind, *, reason=None, receipt_ref=None, frame_seq=None):
    if kind not in _EVENTS:
        raise ValueError("unknown dispatch event")
    return DispatchEvent(request.action_id, kind, seq, reason, receipt_ref,
                         request.handle.generation, request.epoch, frame_seq)


def _required(name, fn):
    if not callable(fn):
        raise DispatchRefused("AUSENTE:%s" % name)


def _stop(stop):
    value = stop()
    if type(value) is not bool:
        raise DispatchRefused("INVALID:stop_result")
    if value:
        raise DispatchStopped("STOP")


def _check_clock(clock, request, lease, *, owner=None):
    now = _number("clock", clock())
    if now >= request.deadline_ns:
        raise DispatchRefused("DEADLINE_EXPIRED")
    if lease is not None:
        if (_handle(getattr(lease, "handle", None)) != _handle(request.handle)
                or getattr(lease, "generation", None) != request.handle.generation
                or (owner is not None and getattr(lease, "owner", None) != owner)
                or not isinstance(getattr(lease, "nonce", None), bytes)
                or not lease.nonce):
            raise DispatchRefused("INVALID:lease")
        expires_ns = _number("expires_ns", getattr(lease, "expires_ns", None), positive=True)
        if now >= expires_ns:
            raise DispatchRefused("LEASE_EXPIRED")
    return now


def _validate(request, seq, source_policy, lease_ttl_ns, max_observation_age_ns,
              owner, locks, resolve, observe, schedule, admit, authorize, send, clock,
              stop, focus, background_safe):
    _number("submission_seq", seq)
    _number("lease_ttl_ns", lease_ttl_ns, positive=True)
    _number("max_observation_age_ns", max_observation_age_ns, positive=True)
    if not isinstance(owner, str) or not owner.strip() or owner != owner.strip() or "\x00" in owner:
        raise DispatchRefused("AUSENTE:owner")
    try:
        action_id = request.action_id
        epoch = request.epoch
        payload = request.payload
        cost = request.cost
        deadline = request.deadline_ns
    except AttributeError as exc:
        raise DispatchRefused("INVALID:ActionRequest") from exc
    if (not isinstance(action_id, str) or not (1 <= len(action_id) <= 128)
            or action_id != action_id.strip() or "\x00" in action_id):
        raise DispatchRefused("INVALID:action_id")
    key = _handle(request.handle)
    _number("epoch", epoch)
    _number("cost", cost)
    _number("deadline_ns", deadline, positive=True)
    if not isinstance(payload, dict):
        raise DispatchRefused("INVALID:payload")
    if source_policy not in _POLICY_KINDS or key[0] not in _POLICY_KINDS[source_policy]:
        raise DispatchRefused("INVALID:source_policy")
    if locks is None or not all(callable(getattr(locks, n, None)) for n in
                                ("acquire", "mark_inflight", "release", "reconcile_quarantine")):
        raise DispatchRefused("AUSENTE:TargetLockRegistry")
    for name, fn in (("resolve", resolve), ("observe", observe),
                     ("schedule", schedule), ("admit", admit), ("authorize", authorize),
                     ("send", send), ("clock", clock), ("stop", stop)):
        _required(name, fn)
    _required("focus" if source_policy == "cdp" else "background_safe",
              focus if source_policy == "cdp" else background_safe)


def _fence(request, *, source_policy, resolve, observe, schedule, admit, authorize,
           clock, stop, focus, background_safe, max_observation_age_ns,
           owner=None, lease=None):
    """Resolve live state, freshness and capabilities; never hold a target mutex."""
    _stop(stop)
    now = _check_clock(clock, request, lease, owner=owner)
    record = resolve(request.handle)
    if record is None or bool(getattr(record, "closed", True)):
        raise DispatchRefused("AUSENTE:open_target")
    if _handle(getattr(record, "handle", None)) != _handle(request.handle):
        raise DispatchRefused("STALE:generation")
    if _number("registry_epoch", getattr(record, "epoch", None)) != request.epoch:
        raise DispatchRefused("STALE:epoch")
    try:
        snap = observe(request.handle)
    except Exception as exc:
        if type(exc).__name__ == "FrameValidationError":
            raise DispatchRefused("STALE:frame_binding") from exc
        raise
    if not isinstance(snap, ObservedTarget):
        raise DispatchRefused("AUSENTE:ObservedTarget")
    obs = snap.observation
    if _handle(getattr(obs, "handle", None)) != _handle(request.handle):
        raise DispatchRefused("STALE:observation_handle")
    if _number("observation_epoch", getattr(obs, "epoch", None)) != request.epoch:
        raise DispatchRefused("STALE:observation_epoch")
    frame_seq = _number("frame_seq", getattr(obs, "frame_seq", None))
    _number("dpr_ppm", getattr(obs, "dpr_ppm", None), positive=True)
    frame = getattr(obs, "frame", None)
    if (_handle(getattr(frame, "handle", None)) != _handle(request.handle)
            or getattr(frame, "epoch", None) != request.epoch
            or getattr(frame, "frame_seq", None) != frame_seq
            or _number("width", getattr(frame, "width", None), positive=True) < 1
            or _number("height", getattr(frame, "height", None), positive=True) < 1
            or not isinstance(getattr(frame, "digest", None), str)
            or _SHA256.fullmatch(frame.digest) is None):
        raise DispatchRefused("STALE:frame_binding")
    captured = _number("captured_ns", snap.captured_ns)
    if captured > now or now - captured > max_observation_age_ns:
        raise DispatchRefused("STALE:observation_age")
    state = getattr(obs, "state", None)
    source = state.get("source") if isinstance(state, dict) else None
    if not isinstance(source, dict) or source.get("kind") != _SOURCE_KINDS[source_policy]:
        raise DispatchRefused("INVALID:source_provenance")
    if admit(request, record, obs) is not True:
        raise DispatchRefused("REFUSED:conductor_admission")
    if schedule(request, record, obs) is not True:
        raise DispatchRefused("REFUSED:scheduler")
    capability = focus if source_policy == "cdp" else background_safe
    if capability(request, record, obs) is not True:
        raise DispatchRefused("REFUSED:focus" if source_policy == "cdp"
                              else "REFUSED:background_safe")
    if authorize(request, record, obs) is not True:
        raise DispatchRefused("REFUSED:authorization")
    _check_clock(clock, request, lease, owner=owner)
    _stop(stop)
    return record, obs, frame_seq


def _reason(exc):
    if isinstance(exc, DispatchRefused):
        return str(exc)
    if isinstance(exc, TargetBusy):
        return "TARGET_BUSY"
    if isinstance(exc, TargetLockRefused):
        return "LEASE_REFUSED"
    # Never reflect exception text: callbacks may embed pixels, receipts, or secrets.
    return "CALLBACK_ERROR:%s" % type(exc).__name__


def dispatch_guarded(request, *, locks, authorize, send, clock, stop, resolve,
                     observe, schedule, admit, source_policy, lease_ttl_ns,
                     max_observation_age_ns, owner, submission_seq=0,
                     focus=None, background_safe=None):
    """At most one guarded transport call. Every possible send stays fenced.

    Success here means *local admission*, NOT successful episode application.
    The returned lease/receipt are volatile and must never be persisted.
    """
    events = []
    lease = None
    frame_seq = None
    try:
        _validate(request, submission_seq, source_policy, lease_ttl_ns,
                  max_observation_age_ns, owner, locks, resolve, observe,
                  schedule, admit, authorize, send, clock, stop, focus, background_safe)
        _, _, frame_seq = _fence(
            request, source_policy=source_policy, resolve=resolve, observe=observe,
            schedule=schedule, admit=admit, authorize=authorize, clock=clock, stop=stop,
            focus=focus, background_safe=background_safe,
            max_observation_age_ns=max_observation_age_ns, owner=owner)
        events.append(_event(request, submission_seq, "OBSERVED", frame_seq=frame_seq))
        now = _check_clock(clock, request, None, owner=owner)
        lease = locks.acquire(request.handle, owner,
                              expires_ns=min(request.deadline_ns, now + lease_ttl_ns),
                              now_ns=now)
        events.append(_event(request, submission_seq, "ELIGIBLE", frame_seq=frame_seq))
    except DispatchStopped as exc:
        events.append(_event(request, submission_seq, "STOPPED", reason=_reason(exc),
                             frame_seq=frame_seq))
        return DispatchResult(request, submission_seq, False, False, False, None, None, tuple(events))
    except Exception as exc:
        events.append(_event(request, submission_seq, "DROPPED", reason=_reason(exc),
                             frame_seq=frame_seq))
        return DispatchResult(request, submission_seq, False, False, False, None, None, tuple(events))

    # A short, metadata-only transition fences *possible* send. The subsequent
    # external callbacks are deliberately outside TargetLockRegistry's mutex.
    try:
        if stop() is True:
            locks.release(lease)
            events.append(_event(request, submission_seq, "STOPPED", reason="STOP",
                                 frame_seq=frame_seq))
            return DispatchResult(request, submission_seq, False, False, False,
                                  None, None, tuple(events))
        locks.mark_inflight(lease)
    except Exception as exc:
        # An expired unsent lease may not be releasable by the merged lock API.
        try:
            locks.release(lease)
        except Exception:
            events.append(_event(request, submission_seq, "QUARANTINED",
                                 reason="UNSENT_LEASE_UNRELEASABLE", frame_seq=frame_seq))
            return DispatchResult(request, submission_seq, False, True, False,
                                  lease, None, tuple(events))
        events.append(_event(request, submission_seq,
                             "STOPPED" if isinstance(exc, DispatchStopped) else "DROPPED",
                             reason=_reason(exc), frame_seq=frame_seq))
        return DispatchResult(request, submission_seq, False, False, False,
                              None, None, tuple(events))

    sent = False
    receipt = None
    try:
        # Re-observe/re-resolve AFTER the lease is marked and immediately before
        # the sole transport send. STOP, deadline and authorization run again.
        record, obs, frame_seq = _fence(
            request, source_policy=source_policy, resolve=resolve, observe=observe,
            schedule=schedule, admit=admit, authorize=authorize, clock=clock, stop=stop,
            focus=focus, background_safe=background_safe,
            max_observation_age_ns=max_observation_age_ns, owner=owner, lease=lease)
        sent = True  # a throwing transport may have sent; never assume not.
        receipt = send(request, record, obs)
        if (getattr(receipt, "action_id", None) != request.action_id
                or _handle(getattr(receipt, "handle", None)) != _handle(request.handle)
                or getattr(receipt, "status", None) not in ("accepted", "sent", "queued")
                or (getattr(receipt, "reason", None) is not None
                    and not isinstance(getattr(receipt, "reason", None), str))):
            raise DispatchRefused("INVALID:transport_receipt")
        events.append(_event(request, submission_seq, "DISPATCHED",
                             receipt_ref=request.action_id, frame_seq=frame_seq))
        return DispatchResult(request, submission_seq, True, True, False,
                              lease, receipt, tuple(events))
    except Exception as exc:
        # Even STOP after mark_inflight needs a backend-confirmed quiescence to
        # clear the possible-send fence; expiry is NEVER treated as cancellation.
        try:
            locks.reconcile_quarantine(request.handle, acknowledged=False)
        except Exception as reconcile_error:
            events.append(_event(request, submission_seq, "QUARANTINED",
                                 reason="QUARANTINE_RECONCILE_ERROR:%s"
                                 % type(reconcile_error).__name__, frame_seq=frame_seq))
        events.append(_event(request, submission_seq, "QUARANTINED",
                             reason=_reason(exc), frame_seq=frame_seq))
        return DispatchResult(request, submission_seq, sent, True, False,
                              lease, receipt, tuple(events))


def match_holdings_outcome(request, outcome, *, expected_order_seq_id):
    """Only a complete, explicit matching Holdings applied outcome is evidence."""
    if not isinstance(outcome, dict) or set(outcome) != {
            "seq", "requested_tick", "applied_tick", "order_seq_id", "status",
            "reason", "state_sha256"}:
        return False
    if not isinstance(expected_order_seq_id, str) or not expected_order_seq_id:
        return False
    payload = getattr(request, "payload", None)
    if not isinstance(payload, dict):
        return False
    try:
        if not (type(payload["tick"]) is int and type(payload["seq"]) is int
                and payload["tick"] >= 0 and payload["seq"] >= 0
                and payload["cmd"] == "pause" and payload["args"] == {}):
            return False
    except (KeyError, TypeError):
        return False
    for key in ("seq", "requested_tick", "applied_tick"):
        if type(outcome[key]) is not int or outcome[key] < 0:
            return False
    return (outcome["seq"] == payload["seq"]
            and outcome["requested_tick"] == payload["tick"]
            and outcome["applied_tick"] >= outcome["requested_tick"]
            and outcome["order_seq_id"] == expected_order_seq_id
            and outcome["status"] == "applied"
            and isinstance(outcome["reason"], str)
            and isinstance(outcome["state_sha256"], str)
            and _SHA256.fullmatch(outcome["state_sha256"]) is not None)


def acknowledge_outcome(result, outcome, *, locks, quiescent, expected_order_seq_id):
    """Release only after matched Holdings outcome AND positive quiescence."""
    if not result.pending or not result.sent or result.receipt is None:
        raise DispatchRefused("AUSENTE:dispatch_receipt")
    _required("quiescent", quiescent)
    matched = match_holdings_outcome(result.request, outcome,
                                     expected_order_seq_id=expected_order_seq_id)
    if not matched or quiescent(result.request, result.receipt, outcome) is not True:
        locks.reconcile_quarantine(result.request.handle, acknowledged=False)
        ev = _event(result.request, result.submission_seq, "QUARANTINED",
                    reason="UNMATCHED_OUTCOME" if not matched else "NOT_QUIESCENT",
                    receipt_ref=result.request.action_id,
                    frame_seq=result.events[-1].frame_seq)
        return replace(result, events=result.events + (ev,))
    locks.reconcile_quarantine(result.request.handle, acknowledged=True)
    ev = _event(result.request, result.submission_seq, "ACKNOWLEDGED",
                receipt_ref=result.request.action_id,
                frame_seq=result.events[-1].frame_seq)
    return replace(result, pending=False, acknowledged=True, lease=None,
                   receipt=None, events=result.events + (ev,))


def dispatch_batch(items, *, readiness=None, runner=None, **injected):
    """Drain a heterogeneous batch; compute failures isolate lanes.

    A runner's map may complete readiness checks out of order. Readiness MUST
    be side-effect-free; actual guarded sends run once in canonical order.
    """
    rows = list(items)
    for item in rows:
        if not isinstance(item, LaneAction):
            raise DispatchRefused("INVALID:LaneAction")
        _number("submission_seq", item.submission_seq)
    prepared = {}

    def check(pair):
        idx, item = pair
        try:
            return (idx, readiness(item) is True, None)
        except Exception as exc:
            return (idx, False, _reason(exc))

    if readiness is not None:
        _required("readiness", readiness)
        if runner is not None:
            _required("runner.map", getattr(runner, "map", None))
            try:
                for idx, ok, reason in runner.map(check, enumerate(rows)):
                    if type(idx) is not int or not 0 <= idx < len(rows):
                        raise DispatchRefused("INVALID:runner_index")
                    if idx in prepared:
                        prepared[idx] = (False, "DUPLICATE:runner_index")
                    else:
                        prepared[idx] = (ok, reason)
            except Exception:
                # Compute only: safe to retry missing readiness checks individually.
                pass
        for idx, item in enumerate(rows):
            if idx not in prepared:
                _, ok, reason = check((idx, item))
                prepared[idx] = (ok, reason)

    ordering = sorted(enumerate(rows), key=lambda pair: (
        pair[1].submission_seq, _handle(pair[1].request.handle), pair[0]))
    results = []
    for idx, item in ordering:
        allowed, reason = prepared.get(idx, (True, None))
        if not allowed:
            ev = _event(item.request, item.submission_seq, "DROPPED",
                        reason=reason or "REFUSED:readiness")
            results.append(DispatchResult(item.request, item.submission_seq,
                                          False, False, False, None, None, (ev,)))
        else:
            results.append(dispatch_guarded(item.request, source_policy=item.source_policy,
                                            submission_seq=item.submission_seq, **injected))
    return tuple(results)
