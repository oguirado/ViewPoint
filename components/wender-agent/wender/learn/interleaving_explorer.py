# @module {"id":"wa.learn.interleaving_explorer","layer":2,"owner":"LEARN","deps":["wa.act.target_lock_registry","wa.perceive.parallel_perceive","wa.perceive.session_registry"],"book":"HB-WA-s3-remember"}
"""Deterministic priority-concurrency (PCT) and Kahn dependency explorer.

Models bounded schedule enumeration, priority-inversion yield points, and
replayable ordered actor-step traces with explicit controlled yield hooks.
Exercises real ``TargetLockRegistry`` and ``perceive_many`` APIs through inline
structural collaborators without replacing production authority enforcement.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
import hashlib
import importlib
import random
import threading
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np

from wender.act.target_lock_registry import (
    ActorLease,
    PrerequisiteAbsent,
    TargetBusy,
    TargetLockRefused,
    TargetLockRegistry,
)
from wender.perceive.parallel_perceive import ObservationError, perceive_many
from wender.perceive.session_registry import (
    FrameRef,
    Handle,
    Observation,
    PerceptionResult,
    SessionRegistry,
    StaleHandleError,
    TargetRecord,
)


MAX_EXPLORER_THREADS = 4
MAX_BOUNDED_SCHEDULES = 32
DEFAULT_JOIN_TIMEOUT_S = 2.0


class InterleavingExplorerError(ValueError):
    """Base error for invalid dependency graphs, schedules, or replay traces."""


class DuplicateStepError(InterleavingExplorerError):
    """Raised when two steps share the same step_id."""


class MissingPredecessorError(InterleavingExplorerError):
    """Raised when a step references an unknown predecessor step_id."""


class CyclicDependencyError(InterleavingExplorerError):
    """Raised when the step dependency graph contains a cycle."""


class ScheduleReplayError(InterleavingExplorerError):
    """Raised when a replay trace violates step readiness, completeness, or bounds."""


class PendingDependencyAbsentError(RuntimeError):
    """Raised when a lazy import targets an unmerged sibling deliverable (AUSENTE)."""


def require_merged_dependency(module_name: str, symbol_name: str | None = None) -> Any:
    """Lazy import guard that blocks on absent sibling deliverables rather than fabricating APIs."""
    if not isinstance(module_name, str) or not module_name.strip():
        raise PendingDependencyAbsentError("AUSENTE: module_name must be a non-empty string")
    try:
        mod = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise PendingDependencyAbsentError(
            f"AUSENTE: dependency module {module_name!r} is not merged in this snapshot"
        ) from exc
    if symbol_name is not None:
        if not isinstance(symbol_name, str) or not symbol_name.strip():
            raise PendingDependencyAbsentError("AUSENTE: symbol_name must be a non-empty string")
        if not hasattr(mod, symbol_name):
            raise PendingDependencyAbsentError(
                f"AUSENTE: symbol {symbol_name!r} in {module_name!r} is not merged in this snapshot"
            )
        return getattr(mod, symbol_name)
    return mod


def _require_exact_int(value: object, name: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if type(value) is not int:
        raise InterleavingExplorerError(f"{name} must be an exact integer")
    if minimum is not None and value < minimum:
        raise InterleavingExplorerError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise InterleavingExplorerError(f"{name} must be <= {maximum}")
    return value


def _require_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise InterleavingExplorerError(f"{name} must be a non-empty stripped string")
    return value


@dataclass(frozen=True)
class ExplorerStep:
    """Single atomic or yield-bounded actor step in a dependency graph."""

    step_id: str
    actor: str
    priority: int = 0
    predecessors: tuple[str, ...] = ()
    action: Callable[[StepExecutionContext], Any] | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_identifier(self.step_id, "step_id")
        _require_identifier(self.actor, "actor")
        _require_exact_int(self.priority, "priority")
        if isinstance(self.predecessors, (str, bytes, bytearray)):
            raise InterleavingExplorerError("predecessors must be an iterable of step_id strings, not a bare string")
        try:
            preds = tuple(self.predecessors)
        except TypeError as exc:
            raise InterleavingExplorerError("predecessors must be an iterable of step_id strings") from exc
        deduped: list[str] = []
        seen: set[str] = set()
        for pred in preds:
            _require_identifier(pred, "predecessor step_id")
            if pred not in seen:
                seen.add(pred)
                deduped.append(pred)
        object.__setattr__(self, "predecessors", tuple(deduped))
        if self.action is not None and not callable(self.action):
            raise InterleavingExplorerError("action must be callable or None")
        if not isinstance(self.payload, Mapping):
            raise InterleavingExplorerError("payload must be a mapping")
        object.__setattr__(self, "payload", dict(self.payload))


@dataclass(frozen=True)
class TraceEvent:
    """Single ordered execution record in a schedule or replay trace."""

    index: int
    step_id: str
    actor: str
    priority: int
    effective_priority: int
    enabled_step_ids: tuple[str, ...]
    preempted: bool = False
    outcome: Any = None


@dataclass(frozen=True)
class ScheduleTrace:
    """Replayable ordered actor-step trace produced by Kahn or PCT exploration."""

    schedule_id: int
    step_ids: tuple[str, ...]
    actors: tuple[str, ...]
    events: tuple[TraceEvent, ...]
    depth_switches: tuple[tuple[int, str, int], ...] = ()

    @property
    def actor_step_pairs(self) -> tuple[tuple[str, str], ...]:
        return tuple(zip(self.actors, self.step_ids))


@dataclass
class StepExecutionContext:
    """Mutable execution state passed to step actions during replay."""

    step: ExplorerStep
    index: int
    yield_hook: Callable[[str], None]
    shared_state: dict[str, Any]
    outcomes: dict[str, Any]

    def yield_control(self, label: str = "checkpoint") -> None:
        self.yield_hook(label)


# @anchor:wa.learn.interleaving_explorer.validate_step_dag
def validate_step_dag(steps: Iterable[ExplorerStep]) -> tuple[tuple[ExplorerStep, ...], dict[str, ExplorerStep], dict[str, tuple[str, ...]], dict[str, int]]:
    """Validate non-duplicate steps, existing predecessors, and acyclicity via Kahn's algorithm."""
    if isinstance(steps, (str, bytes, bytearray)):
        raise InterleavingExplorerError("steps must be an iterable of ExplorerStep instances")
    try:
        step_tuple = tuple(steps)
    except TypeError as exc:
        raise InterleavingExplorerError("steps must be an iterable of ExplorerStep instances") from exc

    by_id: dict[str, ExplorerStep] = {}
    for step in step_tuple:
        if not isinstance(step, ExplorerStep):
            raise InterleavingExplorerError("each item in steps must be an ExplorerStep")
        if step.step_id in by_id:
            raise DuplicateStepError(f"duplicate step_id: {step.step_id!r}")
        by_id[step.step_id] = step

    in_degree: dict[str, int] = {step.step_id: 0 for step in step_tuple}
    dependents: dict[str, list[str]] = {step.step_id: [] for step in step_tuple}

    for step in step_tuple:
        for pred_id in step.predecessors:
            if pred_id not in by_id:
                raise MissingPredecessorError(
                    f"step {step.step_id!r} references missing predecessor {pred_id!r}"
                )
            if pred_id == step.step_id:
                raise CyclicDependencyError(f"self-dependency cycle on step {step.step_id!r}")
            in_degree[step.step_id] += 1
            dependents[pred_id].append(step.step_id)

    for sid in dependents:
        dependents[sid].sort(key=lambda k: (-by_id[k].priority, by_id[k].actor, k))

    # Trial Kahn pass to verify acyclicity.
    work_in = dict(in_degree)
    ready = [sid for sid, deg in work_in.items() if deg == 0]
    ready.sort(key=lambda k: (-by_id[k].priority, by_id[k].actor, k))
    queue = deque(ready)
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        for child in dependents[current]:
            work_in[child] -= 1
            if work_in[child] == 0:
                queue.append(child)

    if visited != len(step_tuple):
        cyclic_ids = sorted(sid for sid, deg in work_in.items() if deg > 0)
        raise CyclicDependencyError(f"dependency cycle detected involving steps: {cyclic_ids!r}")

    frozen_dependents = {k: tuple(v) for k, v in dependents.items()}
    return step_tuple, by_id, frozen_dependents, in_degree


# @anchor:wa.learn.interleaving_explorer.kahn_topological_schedule
def kahn_topological_schedule(steps: Iterable[ExplorerStep], *, schedule_id: int = 0) -> ScheduleTrace:
    """Compute the canonical priority-ordered Kahn topological schedule."""
    _require_exact_int(schedule_id, "schedule_id", minimum=0)
    step_tuple, by_id, dependents, in_degree = validate_step_dag(steps)
    if not step_tuple:
        return ScheduleTrace(schedule_id=schedule_id, step_ids=(), actors=(), events=())

    work_in = dict(in_degree)
    enabled: set[str] = {sid for sid, deg in work_in.items() if deg == 0}
    ordered_ids: list[str] = []
    ordered_actors: list[str] = []
    events: list[TraceEvent] = []

    def _sort_key(sid: str) -> tuple[int, str, str]:
        st = by_id[sid]
        return (-st.priority, st.actor, st.step_id)

    for idx in range(len(step_tuple)):
        sorted_enabled = tuple(sorted(enabled, key=_sort_key))
        chosen_id = sorted_enabled[0]
        enabled.remove(chosen_id)
        chosen = by_id[chosen_id]
        ordered_ids.append(chosen_id)
        ordered_actors.append(chosen.actor)
        events.append(
            TraceEvent(
                index=idx,
                step_id=chosen_id,
                actor=chosen.actor,
                priority=chosen.priority,
                effective_priority=chosen.priority,
                enabled_step_ids=sorted_enabled,
                preempted=False,
                outcome=None,
            )
        )
        for child in dependents[chosen_id]:
            work_in[child] -= 1
            if work_in[child] == 0:
                enabled.add(child)

    return ScheduleTrace(
        schedule_id=schedule_id,
        step_ids=tuple(ordered_ids),
        actors=tuple(ordered_actors),
        events=tuple(events),
        depth_switches=(),
    )


# @anchor:wa.learn.interleaving_explorer.enumerate_kahn_schedules
def enumerate_kahn_schedules(
    steps: Iterable[ExplorerStep],
    *,
    max_schedules: int = MAX_BOUNDED_SCHEDULES,
) -> tuple[ScheduleTrace, ...]:
    """Deterministically enumerate up to ``max_schedules`` topological schedules."""
    _require_exact_int(max_schedules, "max_schedules", minimum=1, maximum=MAX_BOUNDED_SCHEDULES)
    step_tuple, by_id, dependents, in_degree = validate_step_dag(steps)
    if not step_tuple:
        return (ScheduleTrace(schedule_id=0, step_ids=(), actors=(), events=()),)

    def _sort_key(sid: str) -> tuple[int, str, str]:
        st = by_id[sid]
        return (-st.priority, st.actor, st.step_id)

    schedules: list[ScheduleTrace] = []
    initial_enabled = frozenset(sid for sid, deg in in_degree.items() if deg == 0)

    def _backtrack(
        work_in: dict[str, int],
        enabled: frozenset[str],
        chosen_events: list[TraceEvent],
        chosen_ids: list[str],
        chosen_actors: list[str],
    ) -> None:
        if len(schedules) >= max_schedules:
            return
        if len(chosen_ids) == len(step_tuple):
            schedules.append(
                ScheduleTrace(
                    schedule_id=len(schedules),
                    step_ids=tuple(chosen_ids),
                    actors=tuple(chosen_actors),
                    events=tuple(chosen_events),
                    depth_switches=(),
                )
            )
            return

        sorted_enabled = tuple(sorted(enabled, key=_sort_key))
        idx = len(chosen_ids)
        for idx_choice, sid in enumerate(sorted_enabled):
            if len(schedules) >= max_schedules:
                return
            st = by_id[sid]
            next_in = dict(work_in)
            next_enabled = set(enabled)
            next_enabled.remove(sid)
            for child in dependents[sid]:
                next_in[child] -= 1
                if next_in[child] == 0:
                    next_enabled.add(child)

            chosen_ids.append(sid)
            chosen_actors.append(st.actor)
            chosen_events.append(
                TraceEvent(
                    index=idx,
                    step_id=sid,
                    actor=st.actor,
                    priority=st.priority,
                    effective_priority=st.priority,
                    enabled_step_ids=sorted_enabled,
                    preempted=(idx_choice > 0),
                    outcome=None,
                )
            )
            _backtrack(next_in, frozenset(next_enabled), chosen_events, chosen_ids, chosen_actors)
            chosen_events.pop()
            chosen_actors.pop()
            chosen_ids.pop()

    _backtrack(dict(in_degree), initial_enabled, [], [], [])
    return tuple(schedules)


# @anchor:wa.learn.interleaving_explorer.pct_schedule
def pct_schedule(
    steps: Iterable[ExplorerStep],
    *,
    seed: int = 0,
    depth: int = 1,
    schedule_id: int = 0,
) -> ScheduleTrace:
    """Compute a deterministic Priority-Concurrency Testing (PCT) schedule for a given seed and depth."""
    _require_exact_int(seed, "seed")
    _require_exact_int(depth, "depth", minimum=0)
    _require_exact_int(schedule_id, "schedule_id", minimum=0)
    step_tuple, by_id, dependents, in_degree = validate_step_dag(steps)
    if not step_tuple:
        return ScheduleTrace(schedule_id=schedule_id, step_ids=(), actors=(), events=())

    rng = random.Random(seed)
    actors = sorted({step.actor for step in step_tuple})
    base_priorities = list(range(depth + 1, depth + 1 + len(actors)))
    rng.shuffle(base_priorities)
    actor_priority: dict[str, int] = dict(zip(actors, base_priorities))

    total_steps = len(step_tuple)
    effective_depth = min(depth, max(0, total_steps - 1))
    switch_points = sorted(rng.sample(range(total_steps), effective_depth)) if effective_depth > 0 else []
    switch_map: dict[int, int] = {pt: depth - idx for idx, pt in enumerate(switch_points)}

    work_in = dict(in_degree)
    enabled: set[str] = {sid for sid, deg in work_in.items() if deg == 0}
    ordered_ids: list[str] = []
    ordered_actors: list[str] = []
    events: list[TraceEvent] = []
    depth_switches: list[tuple[int, str, int]] = []

    def _effective_key(sid: str) -> tuple[int, int, str, str]:
        st = by_id[sid]
        eff = actor_priority[st.actor]
        return (-eff, -st.priority, st.actor, st.step_id)

    for idx in range(total_steps):
        sorted_enabled = tuple(sorted(enabled, key=_effective_key))
        top_before = sorted_enabled[0]
        preempted = False
        if idx in switch_map and len(sorted_enabled) > 1:
            demoted_actor = by_id[top_before].actor
            new_prio = switch_map[idx]
            actor_priority[demoted_actor] = new_prio
            depth_switches.append((idx, demoted_actor, new_prio))
            sorted_enabled = tuple(sorted(enabled, key=_effective_key))
            preempted = sorted_enabled[0] != top_before

        chosen_id = sorted_enabled[0]
        enabled.remove(chosen_id)
        chosen = by_id[chosen_id]
        eff_prio = actor_priority[chosen.actor]
        ordered_ids.append(chosen_id)
        ordered_actors.append(chosen.actor)
        events.append(
            TraceEvent(
                index=idx,
                step_id=chosen_id,
                actor=chosen.actor,
                priority=chosen.priority,
                effective_priority=eff_prio,
                enabled_step_ids=sorted_enabled,
                preempted=preempted,
                outcome=None,
            )
        )
        for child in dependents[chosen_id]:
            work_in[child] -= 1
            if work_in[child] == 0:
                enabled.add(child)

    return ScheduleTrace(
        schedule_id=schedule_id,
        step_ids=tuple(ordered_ids),
        actors=tuple(ordered_actors),
        events=tuple(events),
        depth_switches=tuple(depth_switches),
    )


def explore_pct_schedules(
    steps: Iterable[ExplorerStep],
    *,
    seed: int = 0,
    depth: int = 2,
    max_schedules: int = MAX_BOUNDED_SCHEDULES,
) -> tuple[ScheduleTrace, ...]:
    """Explore up to ``max_schedules`` distinct deterministic PCT schedules."""
    _require_exact_int(seed, "seed")
    _require_exact_int(depth, "depth", minimum=0)
    _require_exact_int(max_schedules, "max_schedules", minimum=1, maximum=MAX_BOUNDED_SCHEDULES)
    step_tuple, _, _, _ = validate_step_dag(steps)
    if not step_tuple:
        return (ScheduleTrace(schedule_id=0, step_ids=(), actors=(), events=()),)

    seen_orders: set[tuple[str, ...]] = set()
    traces: list[ScheduleTrace] = []

    for attempt in range(max_schedules * 4):
        if len(traces) >= max_schedules:
            break
        candidate = pct_schedule(
            step_tuple,
            seed=seed + attempt,
            depth=depth,
            schedule_id=len(traces),
        )
        if candidate.step_ids not in seen_orders:
            seen_orders.add(candidate.step_ids)
            traces.append(candidate)

    if len(traces) < max_schedules:
        for enum_trace in enumerate_kahn_schedules(step_tuple, max_schedules=max_schedules):
            if len(traces) >= max_schedules:
                break
            if enum_trace.step_ids not in seen_orders:
                seen_orders.add(enum_trace.step_ids)
                traces.append(
                    replace(enum_trace, schedule_id=len(traces))
                )

    return tuple(traces)


class ControlledYieldBarrier:
    """Explicit controlled yield coordinator using stdlib Events and Barriers."""

    def __init__(self, step_ids: Sequence[str], *, timeout_s: float = DEFAULT_JOIN_TIMEOUT_S) -> None:
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or timeout_s <= 0 or timeout_s > DEFAULT_JOIN_TIMEOUT_S:
            raise InterleavingExplorerError(f"timeout_s must be in (0, {DEFAULT_JOIN_TIMEOUT_S}]")
        self.step_ids = tuple(step_ids)
        self.timeout_s = float(timeout_s)
        self._turn_events: dict[str, threading.Event] = {sid: threading.Event() for sid in self.step_ids}
        self._done_events: dict[str, threading.Event] = {sid: threading.Event() for sid in self.step_ids}
        self._yield_log: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def release_step(self, step_id: str) -> None:
        if step_id not in self._turn_events:
            raise ScheduleReplayError(f"unknown step_id in controlled yield barrier: {step_id!r}")
        self._turn_events[step_id].set()

    def wait_for_turn(self, step_id: str) -> None:
        ev = self._turn_events.get(step_id)
        if ev is None:
            raise ScheduleReplayError(f"unknown step_id waiting for turn: {step_id!r}")
        if not ev.wait(timeout=self.timeout_s):
            raise TimeoutError(f"timed out waiting for turn on step {step_id!r}")

    def record_yield(self, step_id: str, label: str) -> None:
        with self._lock:
            self._yield_log.append((step_id, str(label)))

    def mark_step_done(self, step_id: str) -> None:
        ev = self._done_events.get(step_id)
        if ev is None:
            raise ScheduleReplayError(f"unknown step_id marking completion: {step_id!r}")
        ev.set()

    def wait_for_step_done(self, step_id: str) -> None:
        ev = self._done_events.get(step_id)
        if ev is None:
            raise ScheduleReplayError(f"unknown step_id waiting for completion: {step_id!r}")
        if not ev.wait(timeout=self.timeout_s):
            raise TimeoutError(f"timed out waiting for completion of step {step_id!r}")

    @property
    def yield_log(self) -> tuple[tuple[str, str], ...]:
        with self._lock:
            return tuple(self._yield_log)


# @anchor:wa.learn.interleaving_explorer.replay_schedule
def replay_schedule(
    steps: Iterable[ExplorerStep],
    schedule: ScheduleTrace | Sequence[str],
    *,
    shared_state: dict[str, Any] | None = None,
    use_threads: bool = False,
    max_threads: int = MAX_EXPLORER_THREADS,
    timeout_s: float = DEFAULT_JOIN_TIMEOUT_S,
) -> tuple[ScheduleTrace, dict[str, Any], tuple[tuple[str, str], ...]]:
    """Replay an ordered step trace deterministically, either inline or across bounded worker threads."""
    if type(use_threads) is not bool:
        raise InterleavingExplorerError("use_threads must be a bool")
    _require_exact_int(max_threads, "max_threads", minimum=1, maximum=MAX_EXPLORER_THREADS)
    if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or timeout_s <= 0 or timeout_s > DEFAULT_JOIN_TIMEOUT_S:
        raise InterleavingExplorerError(f"timeout_s must be in (0, {DEFAULT_JOIN_TIMEOUT_S}]")

    step_tuple, by_id, dependents, in_degree = validate_step_dag(steps)
    if isinstance(schedule, ScheduleTrace):
        ordered_ids = schedule.step_ids
        schedule_id = schedule.schedule_id
        depth_switches = schedule.depth_switches
        prior_events = {ev.step_id: ev for ev in schedule.events}
    else:
        if isinstance(schedule, (str, bytes, bytearray)):
            raise ScheduleReplayError("schedule must be a ScheduleTrace or sequence of step_id strings")
        try:
            ordered_ids = tuple(schedule)
        except TypeError as exc:
            raise ScheduleReplayError("schedule must be a ScheduleTrace or sequence of step_id strings") from exc
        schedule_id = 0
        depth_switches = ()
        prior_events = {}

    if len(ordered_ids) != len(step_tuple):
        raise ScheduleReplayError(
            f"replay schedule length {len(ordered_ids)} does not match step count {len(step_tuple)}"
        )
    if len(set(ordered_ids)) != len(ordered_ids):
        raise ScheduleReplayError("replay schedule contains duplicate step_ids")

    # Verify every step is enabled when scheduled.
    work_in = dict(in_degree)
    enabled: set[str] = {sid for sid, deg in work_in.items() if deg == 0}
    step_enabled_snapshots: list[tuple[str, ...]] = []
    for sid in ordered_ids:
        if sid not in by_id:
            raise ScheduleReplayError(f"replay schedule references unknown step_id {sid!r}")
        if sid not in enabled:
            raise ScheduleReplayError(f"step {sid!r} is not enabled at its scheduled position")
        step_enabled_snapshots.append(tuple(sorted(enabled)))
        enabled.remove(sid)
        for child in dependents[sid]:
            work_in[child] -= 1
            if work_in[child] == 0:
                enabled.add(child)

    state = {} if shared_state is None else shared_state
    if not isinstance(state, dict):
        raise InterleavingExplorerError("shared_state must be a dict or None")
    outcomes: dict[str, Any] = {}
    barrier = ControlledYieldBarrier(ordered_ids, timeout_s=timeout_s)

    if not use_threads or not ordered_ids:
        events: list[TraceEvent] = []
        for idx, sid in enumerate(ordered_ids):
            st = by_id[sid]
            barrier.release_step(sid)
            barrier.wait_for_turn(sid)
            ctx = StepExecutionContext(
                step=st,
                index=idx,
                yield_hook=lambda label, _sid=sid: barrier.record_yield(_sid, label),
                shared_state=state,
                outcomes=outcomes,
            )
            outcome = st.action(ctx) if st.action is not None else None
            outcomes[sid] = outcome
            barrier.mark_step_done(sid)
            prev = prior_events.get(sid)
            events.append(
                TraceEvent(
                    index=idx,
                    step_id=sid,
                    actor=st.actor,
                    priority=st.priority,
                    effective_priority=prev.effective_priority if prev is not None else st.priority,
                    enabled_step_ids=prev.enabled_step_ids if prev is not None else step_enabled_snapshots[idx],
                    preempted=prev.preempted if prev is not None else False,
                    outcome=outcome,
                )
            )
        replayed = ScheduleTrace(
            schedule_id=schedule_id,
            step_ids=ordered_ids,
            actors=tuple(by_id[sid].actor for sid in ordered_ids),
            events=tuple(events),
            depth_switches=depth_switches,
        )
        return replayed, outcomes, barrier.yield_log

    # Multi-threaded controlled execution with bounded worker threads (<= 4),
    # start barrier, per-step Event release, and 2.0-second bounded joins.
    index_by_id = {sid: idx for idx, sid in enumerate(ordered_ids)}
    actors_in_order: list[str] = []
    for sid in ordered_ids:
        act = by_id[sid].actor
        if act not in actors_in_order:
            actors_in_order.append(act)

    worker_count = min(max_threads, len(actors_in_order))
    buckets: list[list[str]] = [[] for _ in range(worker_count)]
    actor_to_bucket = {act: i % worker_count for i, act in enumerate(actors_in_order)}
    for sid in ordered_ids:
        buckets[actor_to_bucket[by_id[sid].actor]].append(sid)

    start_barrier = threading.Barrier(worker_count + 1, timeout=timeout_s)
    worker_errors: list[BaseException] = []
    error_lock = threading.Lock()
    state_lock = threading.RLock()

    def _worker(assigned_step_ids: Sequence[str]) -> None:
        try:
            start_barrier.wait(timeout=timeout_s)
            for sid in assigned_step_ids:
                barrier.wait_for_turn(sid)
                st = by_id[sid]
                idx = index_by_id[sid]
                ctx = StepExecutionContext(
                    step=st,
                    index=idx,
                    yield_hook=lambda label, _sid=sid: barrier.record_yield(_sid, label),
                    shared_state=state,
                    outcomes=outcomes,
                )
                outcome = st.action(ctx) if st.action is not None else None
                with state_lock:
                    outcomes[sid] = outcome
                barrier.mark_step_done(sid)
        except BaseException as exc:  # pragma: no cover - captured and re-raised on main thread
            with error_lock:
                worker_errors.append(exc)
            for sid in assigned_step_ids:
                barrier.mark_step_done(sid)

    threads = [
        threading.Thread(target=_worker, args=(bucket,), name=f"pct-replay-{i}", daemon=False)
        for i, bucket in enumerate(buckets)
    ]
    for t in threads:
        t.start()

    try:
        start_barrier.wait(timeout=timeout_s)
        for sid in ordered_ids:
            barrier.release_step(sid)
            barrier.wait_for_step_done(sid)
            with error_lock:
                if worker_errors:
                    raise worker_errors[0]
    finally:
        for sid in ordered_ids:
            barrier.release_step(sid)
            barrier.mark_step_done(sid)
        leaked: list[str] = []
        for t in threads:
            t.join(timeout=timeout_s)
            if t.is_alive():
                leaked.append(t.name)
        if leaked:
            raise RuntimeError(f"leaked replay threads after {timeout_s}s join: {leaked!r}")

    with error_lock:
        if worker_errors:
            raise worker_errors[0]

    events = []
    for idx, sid in enumerate(ordered_ids):
        st = by_id[sid]
        prev = prior_events.get(sid)
        events.append(
            TraceEvent(
                index=idx,
                step_id=sid,
                actor=st.actor,
                priority=st.priority,
                effective_priority=prev.effective_priority if prev is not None else st.priority,
                enabled_step_ids=prev.enabled_step_ids if prev is not None else step_enabled_snapshots[idx],
                preempted=prev.preempted if prev is not None else False,
                outcome=outcomes.get(sid),
            )
        )
    replayed = ScheduleTrace(
        schedule_id=schedule_id,
        step_ids=ordered_ids,
        actors=tuple(by_id[sid].actor for sid in ordered_ids),
        events=tuple(events),
        depth_switches=depth_switches,
    )
    return replayed, outcomes, barrier.yield_log


class InlineSessionRegistryCollaborator:
    """Inline collaborator wrapping a real ``SessionRegistry`` with optional canonical resource aliasing."""

    def __init__(self, session_registry: SessionRegistry | None = None, *, clock: Callable[[], int] | None = None) -> None:
        self._clock = (lambda: 100) if clock is None else clock
        self.session_registry = SessionRegistry(clock=self._clock) if session_registry is None else session_registry
        self._resource_aliases: dict[tuple[str, str], str] = {}

    def register_handle(
        self,
        kind: str,
        canonical_id: str,
        *,
        owned: bool = False,
        transport: object = None,
        actuation_resource: str | None = None,
    ) -> Handle:
        handle = self.session_registry.register(kind, canonical_id, transport or object(), owned=owned)
        if actuation_resource is not None:
            self._resource_aliases[(handle.kind, handle.canonical_id)] = actuation_resource
        return handle

    def resolve(self, handle: Handle) -> TargetRecord:
        try:
            return self.session_registry.resolve(handle)
        except StaleHandleError as exc:
            raise LookupError(str(exc)) from exc

    def canonical_resource(self, handle: Handle) -> str:
        return self._resource_aliases.get((handle.kind, handle.canonical_id), handle.canonical_id)


def _frame_bytes(frame: Any) -> bytes:
    data = getattr(frame, "data", None)
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    rgb = getattr(frame, "rgb", None)
    if isinstance(rgb, np.ndarray):
        return rgb.tobytes(order="C")
    if isinstance(rgb, (bytes, bytearray)):
        return bytes(rgb)
    raise TypeError("frame must expose .data bytes or .rgb buffer")


@dataclass(frozen=True)
class DeterministicVisionAdapter:
    """Offline vision adapter implementing screen_identity, changed_regions, and screen_fingerprint."""

    with_fingerprint: bool = True

    def screen_identity(self, frame: Any) -> str:
        digest = hashlib.sha256(_frame_bytes(frame)).hexdigest()
        return digest[:16]

    def changed_regions(self, previous: Any, current: Any) -> list[dict[str, int]]:
        if _frame_bytes(previous) == _frame_bytes(current):
            return []
        return [{"x": 0, "y": 0, "w": int(current.width), "h": int(current.height)}]

    def screen_fingerprint(self, frame: Any) -> bytes:
        if not self.with_fingerprint:
            raise AttributeError("fingerprint disabled")
        return hashlib.sha256(_frame_bytes(frame)).digest()[:8]


class ControlledOrderRunner:
    """Deterministic runner.map collaborator that executes perception groups in a controlled permutation."""

    def __init__(self, order: Sequence[int] | None = None, *, fail_after: int | None = None) -> None:
        self.order = None if order is None else tuple(order)
        self.fail_after = fail_after
        self.executed_indices: list[int] = []

    def map(self, func: Callable[[Any], Any], jobs: Iterable[Any]) -> list[Any]:
        job_list = list(jobs)
        n = len(job_list)
        if self.order is None:
            indices = list(reversed(range(n)))
        else:
            indices = [i for i in self.order if 0 <= i < n]
            seen = set(indices)
            for i in range(n):
                if i not in seen:
                    indices.append(i)

        results_by_pos: dict[int, Any] = {}
        for step_count, pos in enumerate(indices):
            if self.fail_after is not None and step_count >= self.fail_after:
                raise RuntimeError("injected runner failure")
            self.executed_indices.append(pos)
            results_by_pos[pos] = func(job_list[pos])

        return [results_by_pos[i] for i in range(n)]


def build_valid_observation(
    handle: Handle,
    *,
    epoch: int = 0,
    frame_seq: int = 0,
    dpr_ppm: int = 1000000,
    width: int = 4,
    height: int = 4,
    fill_byte: int = 64,
    nodes: list[dict[str, Any]] | None = None,
) -> Observation:
    """Construct a strict canonical Observation with a real SHA-256 FrameRef and AX nodes list."""
    rgb = np.full((height, width, 3), fill_byte & 0xFF, dtype=np.uint8)
    frame_ref = FrameRef.from_rgb(handle, epoch, frame_seq, rgb)
    ax_nodes = (
        [{"id": "node-1", "role": "button", "name": "OK", "actionable": True, "bbox": {"x": 0, "y": 0, "w": width, "h": height}}]
        if nodes is None
        else list(nodes)
    )
    state = {
        "nodes": ax_nodes,
        "viewport": {"w": width, "h": height, "dpr_ppm": dpr_ppm},
    }
    return Observation(handle, epoch, frame_seq, dpr_ppm, state, frame_ref)


def explore_target_lock_interleavings(
    owners: Sequence[str] = ("actor-a", "actor-b"),
    *,
    seed: int = 0,
    max_schedules: int = 8,
) -> tuple[ScheduleTrace, ...]:
    """Model contend/inflight/quarantine schedules against a real TargetLockRegistry."""
    if len(owners) < 2:
        raise InterleavingExplorerError("at least two owners are required to explore lock contention")
    _require_exact_int(max_schedules, "max_schedules", minimum=1, maximum=MAX_BOUNDED_SCHEDULES)

    base_steps = [
        ExplorerStep(step_id=f"attempt_{owner}", actor=owner, priority=idx + 1)
        for idx, owner in enumerate(owners)
    ]
    traces = explore_pct_schedules(base_steps, seed=seed, depth=1, max_schedules=max_schedules)
    realized: list[ScheduleTrace] = []

    for trace in traces:
        now_box = [100]
        collab = InlineSessionRegistryCollaborator(clock=lambda: now_box[0])
        handle = collab.register_handle("cdp_tab", "shared-tab", actuation_resource="shared-resource")
        lock_registry = TargetLockRegistry(collab, clock=lambda: now_box[0])

        def _make_action(actor_name: str) -> Callable[[StepExecutionContext], dict[str, Any]]:
            def _act(ctx: StepExecutionContext) -> dict[str, Any]:
                ctx.yield_control("before_acquire")
                try:
                    lease = lock_registry.acquire(handle, actor_name, expires_ns=500, now_ns=now_box[0])
                    return {"status": "acquired", "owner": lease.owner, "nonce_len": len(lease.nonce)}
                except TargetBusy as exc:
                    return {"status": "busy", "error": type(exc).__name__}

            return _act

        run_steps = [replace(st, action=_make_action(st.actor)) for st in base_steps]
        replayed, _, _ = replay_schedule(run_steps, trace)
        realized.append(replayed)

    return tuple(realized)


def explore_perceive_many_interleavings(
    observations: Sequence[Observation],
    *,
    max_schedules: int = 8,
) -> tuple[tuple[tuple[int, ...], list[PerceptionResult]], ...]:
    """Run ``perceive_many`` across bounded group completion orders and return (order, results)."""
    _require_exact_int(max_schedules, "max_schedules", minimum=1, maximum=MAX_BOUNDED_SCHEDULES)
    obs_list = list(observations)
    adapter = DeterministicVisionAdapter(with_fingerprint=True)
    group_keys: list[tuple[str, str, int, int]] = []
    seen_keys: set[tuple[str, str, int, int]] = set()
    for obs in obs_list:
        h = getattr(obs, "handle", None)
        if h is not None and hasattr(h, "kind") and hasattr(h, "canonical_id") and hasattr(h, "generation"):
            key = (h.kind, h.canonical_id, h.generation, getattr(obs, "epoch", 0))
            if key not in seen_keys:
                seen_keys.add(key)
                group_keys.append(key)

    group_steps = [
        ExplorerStep(step_id=f"group_{i}", actor=f"worker_{i % MAX_EXPLORER_THREADS}", priority=len(group_keys) - i)
        for i in range(len(group_keys))
    ]
    schedules = enumerate_kahn_schedules(group_steps, max_schedules=max_schedules)
    runs: list[tuple[tuple[int, ...], list[PerceptionResult]]] = []
    for sched in schedules:
        order = tuple(int(sid.split("_")[1]) for sid in sched.step_ids)
        runner = ControlledOrderRunner(order=order)
        results = perceive_many(obs_list, adapter=adapter, runner=runner)
        runs.append((tuple(runner.executed_indices), results))
    return tuple(runs)
