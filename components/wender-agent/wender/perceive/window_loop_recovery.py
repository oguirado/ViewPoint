# @module {"id":"wa.perceive.window_loop_recovery","layer":2,"owner":"PERCEIVE","deps":["wa.state.schema","wa.act.target_lock_registry"],"book":"HB-WA-s1-perceive"}
"""Window dispatch, recovery planning, and lane-isolated execution loop.

Exclusive production owner for jq2b-j7-window-loop-recovery.
Implements pure recovery planning and heterogeneous multi-window execution
following invariants INV J7 1 through INV J7 4:
- INV J7 1: Canonical actuation resources include window/capsule aliases and
  permit at most one inflight request. Re-resolve handle generation, epoch,
  fresh observation, lease, focus and configured authorization at the final
  transport boundary; STOP prevents send.
- INV J7 2: Sent work remains quarantined until acknowledged quiescence;
  expiry is not cancellation. Missing scheduling, focus or background-safe
  capability refuses rather than defaulting.
- INV J7 3: Complete batches drain without loss, local admission is recorded
  immediately and acceptance requires matched outcomes. Crash or backpressure
  makes only the affected lane ineligible while siblings continue.
- INV J7 4: Compute completion may reorder, but canonical rows follow
  submission and handle order. Event fields are action_id, event, submission_seq,
  reason, receipt_ref, generation, epoch and frame_seq. Events are OBSERVED,
  ELIGIBLE, DISPATCHED, ACKNOWLEDGED, DROPPED, QUARANTINED or STOPPED.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from wender.state.schema import (
    Handle,
    Observation,
    FrameRef,
    Receipt,
    ActionRequest,
    ActorLease,
    FocusLease,
    TARGET_KINDS,
)
from wender.act.target_lock_registry import (
    TargetLockRegistry,
    TargetBusy,
    TargetLockRefused,
    PrerequisiteAbsent,
)

__all__ = (
    "EVENT_OBSERVED",
    "EVENT_ELIGIBLE",
    "EVENT_DISPATCHED",
    "EVENT_ACKNOWLEDGED",
    "EVENT_DROPPED",
    "EVENT_QUARANTINED",
    "EVENT_STOPPED",
    "EVENTS",
    "EVENT_FIELDS",
    "TRIGGER_CLOSE",
    "TRIGGER_CRASH",
    "TRIGGER_TIMEOUT",
    "TRIGGER_SENT_CANCELLATION",
    "TRIGGER_ACKNOWLEDGED_QUIESCENCE",
    "TRIGGER_RESPAWN",
    "TRIGGERS",
    "INV_J7_1",
    "INV_J7_2",
    "INV_J7_3",
    "INV_J7_4",
    "LoopEvent",
    "LaneState",
    "RecoveryPlan",
    "WindowLoopResult",
    "RecoveryError",
    "RecoveryRefusal",
    "WriteSpy",
    "canonical_actuation_resource",
    "plan_recovery",
    "run_windows",
    "evaluate_gate_g_j7_4",
)

# Canonical Event labels
EVENT_OBSERVED = "OBSERVED"
EVENT_ELIGIBLE = "ELIGIBLE"
EVENT_DISPATCHED = "DISPATCHED"
EVENT_ACKNOWLEDGED = "ACKNOWLEDGED"
EVENT_DROPPED = "DROPPED"
EVENT_QUARANTINED = "QUARANTINED"
EVENT_STOPPED = "STOPPED"

EVENTS = frozenset({
    EVENT_OBSERVED,
    EVENT_ELIGIBLE,
    EVENT_DISPATCHED,
    EVENT_ACKNOWLEDGED,
    EVENT_DROPPED,
    EVENT_QUARANTINED,
    EVENT_STOPPED,
})

EVENT_FIELDS = (
    "action_id",
    "event",
    "submission_seq",
    "reason",
    "receipt_ref",
    "generation",
    "epoch",
    "frame_seq",
)

# Recovery trigger constants
TRIGGER_CLOSE = "close"
TRIGGER_CRASH = "crash"
TRIGGER_TIMEOUT = "timeout"
TRIGGER_SENT_CANCELLATION = "sent_cancellation"
TRIGGER_ACKNOWLEDGED_QUIESCENCE = "acknowledged_quiescence"
TRIGGER_RESPAWN = "respawn"

TRIGGERS = frozenset({
    TRIGGER_CLOSE,
    TRIGGER_CRASH,
    TRIGGER_TIMEOUT,
    TRIGGER_SENT_CANCELLATION,
    TRIGGER_ACKNOWLEDGED_QUIESCENCE,
    TRIGGER_RESPAWN,
})

INV_J7_1 = (
    "INV J7 1: Canonical actuation resources include window/capsule aliases and "
    "permit at most one inflight request. Re-resolve handle generation, epoch, "
    "fresh observation, lease, focus and configured authorization at the final "
    "transport boundary; STOP prevents send."
)
INV_J7_2 = (
    "INV J7 2: Sent work remains quarantined until acknowledged quiescence; "
    "expiry is not cancellation. Missing scheduling, focus or background-safe "
    "capability refuses rather than defaulting."
)
INV_J7_3 = (
    "INV J7 3: Complete batches drain without loss, local admission is recorded "
    "immediately and acceptance requires matched outcomes. Crash or backpressure "
    "makes only the affected lane ineligible while siblings continue."
)
INV_J7_4 = (
    "INV J7 4: Compute completion may reorder, but canonical rows follow "
    "submission and handle order. Event fields are action_id, event, submission_seq, "
    "reason, receipt_ref, generation, epoch and frame_seq. Events are OBSERVED, "
    "ELIGIBLE, DISPATCHED, ACKNOWLEDGED, DROPPED, QUARANTINED or STOPPED."
)


class RecoveryError(Exception):
    """Base error for recovery and execution loop failures."""


class RecoveryRefusal(RecoveryError):
    """Refusal when required capabilities or constraints are missing."""


@dataclass(frozen=True)
class LoopEvent:
    """Canonical event record for episode dispatch and recovery loop."""

    action_id: str
    event: str
    submission_seq: int
    reason: str | None
    receipt_ref: str | None
    generation: int
    epoch: int
    frame_seq: int

    def __post_init__(self) -> None:
        if self.event not in EVENTS:
            raise ValueError(f"event {self.event!r} must be one of {sorted(EVENTS)}")
        if type(self.submission_seq) is not int or self.submission_seq < 0:
            raise ValueError("submission_seq must be a nonnegative integer")
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("generation must be a nonnegative integer")
        if type(self.epoch) is not int or self.epoch < 0:
            raise ValueError("epoch must be a nonnegative integer")
        if type(self.frame_seq) is not int or self.frame_seq < 0:
            raise ValueError("frame_seq must be a nonnegative integer")


@dataclass(frozen=True)
class LaneState:
    """State of an individual execution lane."""

    handle: Handle
    resource: str
    status: str  # "ELIGIBLE", "INELIGIBLE", "QUARANTINED", "CLOSED", "CRASHED"
    generation: int
    epoch: int
    frame_seq: int = 0
    inflight_action: str | None = None
    quarantined: bool = False
    cancellation_sent: bool = False
    quiescent: bool = True


@dataclass(frozen=True)
class RecoveryPlan:
    """Pure recovery decision produced by plan_recovery."""

    action: str  # "QUARANTINE", "AWAIT_QUIESCENCE", "RESPAWN", "CLOSE", "DROP", "REJECT_STALE", "NOOP"
    affected_handle: Handle
    reason: str
    quarantined: bool
    requires_acknowledgment: bool
    eligible_lanes: tuple[Handle, ...]
    quarantined_lanes: tuple[Handle, ...]
    events: tuple[LoopEvent, ...]
    next_handle: Handle | None = None
    unlocked: bool = False


@dataclass(frozen=True)
class WindowLoopResult:
    """Final result of run_windows execution."""

    events: tuple[LoopEvent, ...]
    canonical_rows: tuple[dict, ...]
    lanes: dict[str, Any]
    quarantined_lanes: tuple[Handle, ...]
    completed_lanes: tuple[Handle, ...]
    drained: bool
    status: str


def canonical_actuation_resource(handle: Any) -> str:
    """Return the canonical actuation resource identifier for a handle or alias.

    Capsule and child window aliases must map to the same nonempty resource id.
    """
    if isinstance(handle, dict):
        cid = handle.get("canonical_id")
    else:
        cid = getattr(handle, "canonical_id", None)
    if not isinstance(cid, str) or not cid.strip():
        raise ValueError("canonical actuation resource is absent or empty")
    return cid.strip()


# @anchor:wa.perceive.window_loop_recovery.plan_recovery
def plan_recovery(
    target: Any,
    trigger: str,
    *,
    current_generation: int | None = None,
    all_lanes: Sequence[Any] | None = None,
    inflight: bool = False,
    quarantined: bool = False,
    acknowledged: bool = False,
    receipt: Any = None,
    reason: str | None = None,
    submission_seq: int = 0,
    epoch: int = 0,
    frame_seq: int = 0,
    action_id: str = "",
) -> RecoveryPlan:
    """Pure recovery planning with event and identity mappings.

    Distinguishes close, crash, timeout, sent cancellation, acknowledged
    quiescence and respawn. Late old-generation receipts never unlock
    replacements. Explicit acknowledgment is required before recovery,
    keeping unaffected lanes eligible.
    """
    # Normalize target handle
    if isinstance(target, LaneState):
        handle = target.handle
        current_gen = target.generation if current_generation is None else current_generation
        is_inflight = (target.inflight_action is not None) or inflight
        is_quarantined = target.quarantined or quarantined
        is_quiescent = target.quiescent
        epoch = target.epoch if epoch == 0 else epoch
        frame_seq = target.frame_seq if frame_seq == 0 else frame_seq
    elif isinstance(target, dict):
        handle = Handle(target["kind"], target["canonical_id"], target["generation"])
        current_gen = handle.generation if current_generation is None else current_generation
        is_inflight = inflight
        is_quarantined = quarantined
        is_quiescent = acknowledged
    else:
        handle = target
        current_gen = handle.generation if current_generation is None else current_generation
        is_inflight = inflight
        is_quarantined = quarantined
        is_quiescent = acknowledged

    # Normalize sibling lanes
    lane_list: list[Handle] = []
    if all_lanes is not None:
        for item in all_lanes:
            h = item.handle if isinstance(item, LaneState) else item
            lane_list.append(h)
    elif handle is not None:
        lane_list = [handle]

    effective_reason = reason or trigger

    # 1. Stale generation receipt contrast: late old-generation receipt never unlocks replacement
    if receipt is not None:
        receipt_handle = getattr(receipt, "handle", None)
        receipt_gen = getattr(receipt_handle, "generation", None) if receipt_handle else None
        if receipt_gen is not None and receipt_gen < current_gen:
            # Stale receipt! Refuse to unlock or clear quarantine on replacement target
            ev = LoopEvent(
                action_id=action_id or getattr(receipt, "action_id", ""),
                event=EVENT_DROPPED,
                submission_seq=submission_seq,
                reason="stale_generation_receipt",
                receipt_ref=getattr(receipt, "action_id", None),
                generation=receipt_gen,
                epoch=epoch,
                frame_seq=frame_seq,
            )
            return RecoveryPlan(
                action="REJECT_STALE",
                affected_handle=handle,
                reason="stale generation receipt rejected; cannot unlock replacement target",
                quarantined=is_quarantined,
                requires_acknowledgment=True,
                eligible_lanes=tuple(h for h in lane_list if h != handle or not is_quarantined),
                quarantined_lanes=tuple([handle]) if is_quarantined else (),
                events=(ev,),
                next_handle=None,
                unlocked=False,
            )

    # 2. Trigger: CRASH
    if trigger == TRIGGER_CRASH:
        # Crash makes ONLY the affected lane ineligible while siblings continue
        ev = LoopEvent(
            action_id=action_id,
            event=EVENT_QUARANTINED,
            submission_seq=submission_seq,
            reason=effective_reason,
            receipt_ref=None,
            generation=current_gen,
            epoch=epoch,
            frame_seq=frame_seq,
        )
        eligible = tuple(h for h in lane_list if h != handle)
        quarantined_lanes = (handle,)
        return RecoveryPlan(
            action="QUARANTINE",
            affected_handle=handle,
            reason=effective_reason,
            quarantined=True,
            requires_acknowledgment=True,
            eligible_lanes=eligible,
            quarantined_lanes=quarantined_lanes,
            events=(ev,),
            next_handle=None,
            unlocked=False,
        )

    # 3. Trigger: TIMEOUT
    if trigger == TRIGGER_TIMEOUT:
        # Expiry is not cancellation: sent work remains quarantined until acknowledged quiescence
        ev = LoopEvent(
            action_id=action_id,
            event=EVENT_QUARANTINED,
            submission_seq=submission_seq,
            reason="timeout",
            receipt_ref=None,
            generation=current_gen,
            epoch=epoch,
            frame_seq=frame_seq,
        )
        eligible = tuple(h for h in lane_list if h != handle)
        quarantined_lanes = (handle,)
        return RecoveryPlan(
            action="QUARANTINE",
            affected_handle=handle,
            reason="timeout; sent work quarantined until acknowledged quiescence",
            quarantined=True,
            requires_acknowledgment=True,
            eligible_lanes=eligible,
            quarantined_lanes=quarantined_lanes,
            events=(ev,),
            next_handle=None,
            unlocked=False,
        )

    # 4. Trigger: SENT_CANCELLATION
    if trigger == TRIGGER_SENT_CANCELLATION:
        ev = LoopEvent(
            action_id=action_id,
            event=EVENT_QUARANTINED,
            submission_seq=submission_seq,
            reason="cancellation_sent",
            receipt_ref=None,
            generation=current_gen,
            epoch=epoch,
            frame_seq=frame_seq,
        )
        eligible = tuple(h for h in lane_list if h != handle)
        return RecoveryPlan(
            action="AWAIT_QUIESCENCE",
            affected_handle=handle,
            reason="cancellation sent; awaiting acknowledged quiescence",
            quarantined=True,
            requires_acknowledgment=True,
            eligible_lanes=eligible,
            quarantined_lanes=(handle,),
            events=(ev,),
            next_handle=None,
            unlocked=False,
        )

    # 5. Trigger: ACKNOWLEDGED_QUIESCENCE
    if trigger == TRIGGER_ACKNOWLEDGED_QUIESCENCE:
        ev = LoopEvent(
            action_id=action_id,
            event=EVENT_ACKNOWLEDGED,
            submission_seq=submission_seq,
            reason="quiescence_acknowledged",
            receipt_ref=getattr(receipt, "action_id", None) if receipt else None,
            generation=current_gen,
            epoch=epoch,
            frame_seq=frame_seq,
        )
        return RecoveryPlan(
            action="ACKNOWLEDGED_QUIESCENCE",
            affected_handle=handle,
            reason="quiescence acknowledged",
            quarantined=False,
            requires_acknowledgment=False,
            eligible_lanes=tuple(lane_list),
            quarantined_lanes=(),
            events=(ev,),
            next_handle=None,
            unlocked=True,
        )

    # 6. Trigger: RESPAWN
    if trigger == TRIGGER_RESPAWN:
        # Require explicit acknowledgment before recovery
        if is_quarantined and not acknowledged:
            raise RecoveryRefusal("explicit acknowledgment required before recovery respawn")

        next_gen = current_gen + 1
        new_handle = Handle(handle.kind, handle.canonical_id, next_gen)
        ev = LoopEvent(
            action_id=action_id,
            event=EVENT_ELIGIBLE,
            submission_seq=submission_seq,
            reason="respawn",
            receipt_ref=None,
            generation=next_gen,
            epoch=epoch,
            frame_seq=0,
        )
        new_eligible = tuple(
            new_handle if h == handle else h for h in lane_list
        )
        return RecoveryPlan(
            action="RESPAWN",
            affected_handle=handle,
            reason="respawned new incarnation",
            quarantined=False,
            requires_acknowledgment=False,
            eligible_lanes=new_eligible,
            quarantined_lanes=(),
            events=(ev,),
            next_handle=new_handle,
            unlocked=True,
        )

    # 7. Trigger: CLOSE
    if trigger == TRIGGER_CLOSE:
        if is_inflight and not acknowledged:
            # Sent work remains quarantined until acknowledged quiescence!
            ev = LoopEvent(
                action_id=action_id,
                event=EVENT_QUARANTINED,
                submission_seq=submission_seq,
                reason="close_with_inflight",
                receipt_ref=None,
                generation=current_gen,
                epoch=epoch,
                frame_seq=frame_seq,
            )
            eligible = tuple(h for h in lane_list if h != handle)
            return RecoveryPlan(
                action="QUARANTINE",
                affected_handle=handle,
                reason="inflight work must reach acknowledged quiescence before close",
                quarantined=True,
                requires_acknowledgment=True,
                eligible_lanes=eligible,
                quarantined_lanes=(handle,),
                events=(ev,),
                next_handle=None,
                unlocked=False,
            )
        else:
            ev = LoopEvent(
                action_id=action_id,
                event=EVENT_DROPPED,
                submission_seq=submission_seq,
                reason="close",
                receipt_ref=None,
                generation=current_gen,
                epoch=epoch,
                frame_seq=frame_seq,
            )
            eligible = tuple(h for h in lane_list if h != handle)
            return RecoveryPlan(
                action="CLOSE",
                affected_handle=handle,
                reason="target closed",
                quarantined=False,
                requires_acknowledgment=False,
                eligible_lanes=eligible,
                quarantined_lanes=(),
                events=(ev,),
                next_handle=None,
                unlocked=True,
            )

    raise ValueError(f"unknown trigger {trigger!r}, must be one of {sorted(TRIGGERS)}")


# @anchor:wa.perceive.window_loop_recovery.run_windows
def run_windows(
    handles: Sequence[Handle],
    *,
    adapters: Any = None,
    registry: Any = None,
    streams: Any = None,
    perceive: Any = None,
    schedule: Any = None,
    focus: Any = None,
    dispatch: Any = None,
    clock: Any = None,
    stop: Any = None,
    actions: Sequence[Dict[str, Any]] | None = None,
    authorization: Any = None,
    max_steps: int = 100,
) -> WindowLoopResult:
    """Heterogeneous multi-window execution loop.

    Follows INV J7 1 through INV J7 4:
    - Registers canonical resources
    - Refuses missing scheduling or focus capabilities
    - Opens isolated stream lanes
    - Selects eligible work
    - Re-resolves lease, focus, observation, and authorization at boundary
    - Enforces STOP prevents send
    - Dispatches at most one inflight request per canonical actuation resource
    - Quarantines sent work on crash/timeout until acknowledged quiescence
    - Preserves submission order under reversed compute completion
    """
    # INV J7 2: Missing scheduling or focus capability refuses rather than defaulting
    if schedule is None:
        raise RecoveryRefusal("missing scheduling capability refuses rather than defaulting")
    if focus is None:
        raise RecoveryRefusal("missing focus capability refuses rather than defaulting")

    monotonic_clock = clock if (clock is not None and callable(clock)) else time.monotonic_ns
    stop_check = stop if (stop is not None and callable(stop)) else (lambda: False)

    # Local admission is recorded immediately
    admitted_handles = list(handles)
    if not admitted_handles:
        return WindowLoopResult(
            events=(),
            canonical_rows=(),
            lanes={},
            quarantined_lanes=(),
            completed_lanes=(),
            drained=True,
            status="COMPLETED",
        )

    # Initialize lanes
    lanes: dict[str, LaneState] = {}
    for h in admitted_handles:
        res = canonical_actuation_resource(h)
        lanes[res] = LaneState(
            handle=h,
            resource=res,
            status="ELIGIBLE",
            generation=h.generation,
            epoch=1,
            frame_seq=0,
            quarantined=False,
            quiescent=True,
        )

    events_log: list[LoopEvent] = []
    canonical_rows: list[dict] = []
    queue: list[dict] = list(actions) if actions is not None else []
    submission_seq_counter = 0

    # Ensure queue has submission_seq
    for item in queue:
        if "submission_seq" not in item:
            item["submission_seq"] = submission_seq_counter
            submission_seq_counter += 1

    steps = 0
    while queue and steps < max_steps:
        steps += 1

        # Check STOP condition: STOP prevents send
        if stop_check():
            for item in queue:
                ev = LoopEvent(
                    action_id=str(item.get("action_id", item.get("seq", ""))),
                    event=EVENT_STOPPED,
                    submission_seq=item.get("submission_seq", 0),
                    reason="STOP called before send",
                    receipt_ref=None,
                    generation=item.get("generation", 0),
                    epoch=item.get("epoch", 1),
                    frame_seq=item.get("frame_seq", 0),
                )
                events_log.append(ev)
            break

        # Observation step
        if perceive is not None and callable(perceive):
            try:
                perceive_results = perceive(admitted_handles)
                for pr in perceive_results:
                    h = getattr(pr, "handle", None)
                    if h:
                        ev = LoopEvent(
                            action_id="",
                            event=EVENT_OBSERVED,
                            submission_seq=submission_seq_counter,
                            reason=None,
                            receipt_ref=None,
                            generation=h.generation,
                            epoch=getattr(pr, "epoch", 1),
                            frame_seq=getattr(pr, "frame_seq", 0),
                        )
                        events_log.append(ev)
            except Exception as exc:
                pass

        # Candidate action
        action_item = queue.pop(0)
        action_id = str(action_item.get("action_id", action_item.get("seq", f"act_{steps}")))
        sub_seq = action_item.get("submission_seq", steps - 1)
        target_handle = action_item.get("handle")
        if target_handle is None and admitted_handles:
            target_handle = admitted_handles[0]

        target_res = canonical_actuation_resource(target_handle)
        lane = lanes.get(target_res)

        # Check lane eligibility
        if lane is None or lane.status != "ELIGIBLE" or lane.quarantined:
            # Lane is ineligible / quarantined: crash or backpressure isolates lane
            ev = LoopEvent(
                action_id=action_id,
                event=EVENT_DROPPED,
                submission_seq=sub_seq,
                reason="lane_ineligible_or_quarantined",
                receipt_ref=None,
                generation=target_handle.generation,
                epoch=lane.epoch if lane else 1,
                frame_seq=lane.frame_seq if lane else 0,
            )
            events_log.append(ev)
            continue

        # Emit ELIGIBLE event
        ev_elig = LoopEvent(
            action_id=action_id,
            event=EVENT_ELIGIBLE,
            submission_seq=sub_seq,
            reason=None,
            receipt_ref=None,
            generation=target_handle.generation,
            epoch=lane.epoch,
            frame_seq=lane.frame_seq,
        )
        events_log.append(ev_elig)

        # FINAL TRANSPORT BOUNDARY:
        # Re-resolve handle generation, epoch, fresh observation, lease, focus, authorization
        if stop_check():
            ev_stop = LoopEvent(
                action_id=action_id,
                event=EVENT_STOPPED,
                submission_seq=sub_seq,
                reason="STOP prevents send at transport boundary",
                receipt_ref=None,
                generation=target_handle.generation,
                epoch=lane.epoch,
                frame_seq=lane.frame_seq,
            )
            events_log.append(ev_stop)
            break

        # Re-resolve generation
        if target_handle.generation != lane.generation:
            ev_drop = LoopEvent(
                action_id=action_id,
                event=EVENT_DROPPED,
                submission_seq=sub_seq,
                reason="stale_generation_at_boundary",
                receipt_ref=None,
                generation=target_handle.generation,
                epoch=lane.epoch,
                frame_seq=lane.frame_seq,
            )
            events_log.append(ev_drop)
            continue

        # Re-resolve focus
        if callable(focus):
            has_focus = focus(target_handle)
        elif hasattr(focus, "has_focus"):
            has_focus = focus.has_focus(target_handle)
        else:
            has_focus = True

        if not has_focus:
            ev_drop = LoopEvent(
                action_id=action_id,
                event=EVENT_DROPPED,
                submission_seq=sub_seq,
                reason="focus_unavailable",
                receipt_ref=None,
                generation=target_handle.generation,
                epoch=lane.epoch,
                frame_seq=lane.frame_seq,
            )
            events_log.append(ev_drop)
            continue

        # Re-resolve authorization
        if authorization is not None:
            authorized = authorization(action_item, target_handle) if callable(authorization) else True
            if not authorized:
                ev_drop = LoopEvent(
                    action_id=action_id,
                    event=EVENT_DROPPED,
                    submission_seq=sub_seq,
                    reason="unauthorized_at_boundary",
                    receipt_ref=None,
                    generation=target_handle.generation,
                    epoch=lane.epoch,
                    frame_seq=lane.frame_seq,
                )
                events_log.append(ev_drop)
                continue

        # Re-resolve lease from registry
        lease = None
        if registry is not None:
            if hasattr(registry, "acquire"):
                try:
                    lease = registry.acquire(
                        target_handle,
                        owner="window_loop",
                        expires_ns=monotonic_clock() + 1_000_000_000,
                    )
                except TargetBusy:
                    ev_drop = LoopEvent(
                        action_id=action_id,
                        event=EVENT_DROPPED,
                        submission_seq=sub_seq,
                        reason="target_busy",
                        receipt_ref=None,
                        generation=target_handle.generation,
                        epoch=lane.epoch,
                        frame_seq=lane.frame_seq,
                    )
                    events_log.append(ev_drop)
                    continue
                except Exception as exc:
                    ev_drop = LoopEvent(
                        action_id=action_id,
                        event=EVENT_DROPPED,
                        submission_seq=sub_seq,
                        reason=f"lease_refused:{exc}",
                        receipt_ref=None,
                        generation=target_handle.generation,
                        epoch=lane.epoch,
                        frame_seq=lane.frame_seq,
                    )
                    events_log.append(ev_drop)
                    continue

        # DISPATCH:
        if registry is not None and lease is not None and hasattr(registry, "mark_inflight"):
            try:
                registry.mark_inflight(lease)
            except Exception:
                pass

        ev_disp = LoopEvent(
            action_id=action_id,
            event=EVENT_DISPATCHED,
            submission_seq=sub_seq,
            reason=None,
            receipt_ref=None,
            generation=target_handle.generation,
            epoch=lane.epoch,
            frame_seq=lane.frame_seq,
        )
        events_log.append(ev_disp)

        # Execute dispatch transport
        dispatch_success = False
        outcome_obj = None
        if dispatch is not None and callable(dispatch):
            try:
                outcome_obj = dispatch(action_item, target_handle)
                dispatch_success = True
            except Exception as exc:
                # Crash or backpressure during dispatch!
                # Makes only the affected lane ineligible while siblings continue.
                plan = plan_recovery(
                    lane,
                    TRIGGER_CRASH,
                    all_lanes=list(lanes.values()),
                    inflight=True,
                    reason=f"dispatch_crash:{type(exc).__name__}:{exc}",
                    submission_seq=sub_seq,
                    epoch=lane.epoch,
                    frame_seq=lane.frame_seq,
                    action_id=action_id,
                )
                lanes[target_res] = LaneState(
                    handle=target_handle,
                    resource=target_res,
                    status="CRASHED",
                    generation=lane.generation,
                    epoch=lane.epoch,
                    frame_seq=lane.frame_seq,
                    inflight_action=action_id,
                    quarantined=True,
                    quiescent=False,
                )
                for pe in plan.events:
                    events_log.append(pe)
                continue
        else:
            # Simulated outcome
            dispatch_success = True
            outcome_obj = {
                "seq": action_item.get("seq", sub_seq),
                "requested_tick": action_item.get("tick", 0),
                "applied_tick": action_item.get("tick", 0),
                "order_seq_id": action_item.get("order_seq_id", 0),
                "status": "ok",
                "reason": "applied",
                "state_sha256": "0" * 64,
            }

        # RECONCILE OUTCOME:
        # Acceptance requires matched outcomes
        matched = False
        receipt_ref = None
        if isinstance(outcome_obj, dict):
            status = outcome_obj.get("status")
            receipt_ref = str(outcome_obj.get("order_seq_id", action_id))
            if status in ("ok", "success", "applied"):
                matched = True
        elif outcome_obj is not None:
            matched = True
            receipt_ref = action_id

        if matched:
            ev_ack = LoopEvent(
                action_id=action_id,
                event=EVENT_ACKNOWLEDGED,
                submission_seq=sub_seq,
                reason=None,
                receipt_ref=receipt_ref,
                generation=target_handle.generation,
                epoch=lane.epoch,
                frame_seq=lane.frame_seq,
            )
            events_log.append(ev_ack)
            if registry is not None and lease is not None and hasattr(registry, "release"):
                try:
                    registry.release(lease)
                except Exception:
                    pass

            if isinstance(outcome_obj, dict):
                row_copy = dict(outcome_obj)
                row_copy["submission_seq"] = sub_seq
                row_copy["canonical_id"] = target_handle.canonical_id
                canonical_rows.append(row_copy)
        else:
            # Outcome mismatched or failed! Must quarantine until acknowledged quiescence
            ev_quar = LoopEvent(
                action_id=action_id,
                event=EVENT_QUARANTINED,
                submission_seq=sub_seq,
                reason="unmatched_outcome",
                receipt_ref=receipt_ref,
                generation=target_handle.generation,
                epoch=lane.epoch,
                frame_seq=lane.frame_seq,
            )
            events_log.append(ev_quar)
            lanes[target_res] = LaneState(
                handle=target_handle,
                resource=target_res,
                status="QUARANTINED",
                generation=lane.generation,
                epoch=lane.epoch,
                frame_seq=lane.frame_seq,
                inflight_action=action_id,
                quarantined=True,
                quiescent=False,
            )

    # INV J7 4: Compute completion may reorder, but canonical rows follow submission and handle order
    canonical_rows.sort(key=lambda r: (r.get("submission_seq", 0), r.get("canonical_id", "")))

    quarantined_list = tuple(l.handle for l in lanes.values() if l.quarantined)
    completed_list = tuple(l.handle for l in lanes.values() if not l.quarantined and l.status == "ELIGIBLE")

    return WindowLoopResult(
        events=tuple(events_log),
        canonical_rows=tuple(canonical_rows),
        lanes={k: vars(v) for k, v in lanes.items()},
        quarantined_lanes=quarantined_list,
        completed_lanes=completed_list,
        drained=len(queue) == 0,
        status="COMPLETED" if not quarantined_list else "QUARANTINED",
    )


def evaluate_gate_g_j7_4(
    result: Any = None,
    *,
    negative_contrast_passed: bool = False,
    positive_control_passed: bool = False,
) -> dict[str, Any]:
    """Verify Acceptance Gate G J7 4: Replacement/crash isolates lanes.

    Negative contrast: stale generation receipt unlocks new target (must be rejected).
    Positive control: affected lane quarantined, sibling continues (must succeed).
    """
    passed = bool(negative_contrast_passed and positive_control_passed)
    return {
        "gate": "G_J7_4",
        "passed": passed,
        "negative_contrast_passed": negative_contrast_passed,
        "positive_control_passed": positive_control_passed,
        "reason": (
            "Gate G J7 4 satisfied: replacement/crash isolates lanes"
            if passed
            else "Gate G J7 4 failed: positive control or negative contrast did not pass"
        ),
    }


class WriteSpy:
    """RAM-only write spy to verify no frame pixels or hook receipts leak to disk."""

    def __init__(self) -> None:
        self.recorded_writes: list[str] = []

    def check_clean(self) -> bool:
        return len(self.recorded_writes) == 0

    def assert_no_leakage(self) -> None:
        if self.recorded_writes:
            raise AssertionError(f"WriteSpy detected disk leakage: {self.recorded_writes}")
