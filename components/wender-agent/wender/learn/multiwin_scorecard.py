# @module {"id":"wa.learn.multiwin_scorecard","layer":2,"owner":"LEARN","deps":[],"book":"HB-WA-s3-remember"}
"""Deterministic, RAM-only integration evidence for independently addressed targets.

Public records deliberately retain the positional field order of the M7 contract.
Adapters are injected objects, keyed by ``(kind, canonical_id, generation)``. No
transport, process, native capture, filesystem, or real clock is constructed here.
An adapter has ``offline is True``, ``resource_key: str``, ``focus_domain: str|None``,
``gates`` and the following interfaces (all values are structurally compatible):

* reset(*, seed: int, clock: FakeClock)
* registry.resolve(handle) -> TargetRecord
* stream.deliver(frame: FrameRef) -> FrameRef | None  (None records a drop)
* perception.observe(frame) -> Observation; perception.reduce(observation)
  -> ChangeResult (claims is a boolean HxW mask; ops are nonnegative integers)
* scheduler.admit(action, observation, change, now_ns) -> decision mapping
* actor.execute(action, observation, now_ns) -> Receipt (local admission)
* terminal(action, receipt, now_ns) -> Receipt (authoritative terminal)

A decision has admitted: bool, eligible: bool, reason: str|None, telemetry: dict.
Telemetry requires resource_key; active_resource (candidate-inclusive); the five
booleans focus_authorized, handle_fresh, fence_clear, guard_passed,
geometry_matches; integer global_committed, global_reserved, global_cap,
target_committed, target_reserved, target_cap, wait_opportunities, queue_depth,
fairness_cost, quantum, urgent_cap; and active_focus if focus_domain is nonempty.
Missing telemetry refuses the send; safety and budget violations refuse it too.
An admitted candidate owns exactly one resource/focus slot and its global/target
reservation must cover ActionRequest.cost. A separate conservative charged ledger
checks cumulative global and canonical-resource costs; cap changes are refused.
The declared fairness bound applies to fairness_cost=quantum=urgent_cap=1 only.

Each prerequisite G-M1-1..G-M6-5 and G-M8-1..G-M8-5 must be supplied by every
adapter as {status: 'PASS', evidence: nonempty str, expected_failure: False}.
These are injected offline evidence, never certification of native prerequisites.
An absent/failed prerequisite blocks all integration dispatch, including reset.

Scenario.targets is a tuple of Handles. Each frame event is a mapping with handle,
scene, epoch, frame_seq, rgb (uint8 HxWx3), truth (bool HxW), and optional action
(ActionRequest), at_ns (offset, default 0), drop_expected (default False). A
recovery event has kind='recover', handle, previous_handle, acknowledged=True and
at_ns; it requires a larger generation of the same kind/id and a quarantined old
incarnation. expected_outcomes maps every declared action_id to a terminal string.
SUCCESS, COMPLETED, ACCEPTED and EXECUTED are successful terminal names; UNKNOWN,
ADMITTED, PENDING, QUEUED and PROPOSED are never terminal completion.

The complete scenario and its RAM arrays/actions are detached before callbacks.
Delivered frames, observations and claims are detached again at admission. A
readonly numpy view alone is not proof that its underlying owner is immutable.
The production runner executes the interleaved trace, independently reset isolated
lanes, and a second independently reset interleaved trace. Raw comparisons, not
constant assertions, establish isolation, operation equality and replay evidence.
An incarnation is a separate adapter and RNG; Godot/CDP cursor ownership is never
inferred from canonical_id alone. A resource_key names the actuation destination;
a capsule and its child screen must use the same key for the same destination.
Uncertain completion fences both resource and focus domain. Acknowledged recovery
can release only its current predecessor's fence, never a newer generation's.
Isolated references retain observed external resource/focus quarantine stalls;
that explicit eligibility conditioning is recorded in the manifest, not counted
as successful perception or as cross-target reducer contamination.

WindowEvidence.ops['counts'] holds reducer operations. The reserved '_trial'
entry holds a shared deterministic manifest, allowing score_windows(rows) to
detect omitted targets/actions/rows without another argument. Its seal hashes
that manifest and all row evidence (excluding the seal itself and diagnostics).
It is integrity evidence, not a secret signature. Duplicate scene names across
different handles are valid; no duplicate scenes are passed to another aggregate.
Every required metric is independent; metrics['terminal_completeness']['gates']
contains the complete 40-gate vector. Missing denominators are None and fail,
including per-target frame/operation support and positive simulated elapsed time.
Diagnostics are excluded only from report-level bags, never from semantic state,
payloads, counts, or action-id maps. Simulated elapsed nanoseconds are
retained; host speed, native capture, G-T4 and G-T5 are explicitly AUSENTE.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace

import numpy as np

try:
    from wender.perceive.session_registry import (
        ActionRequest, ChangeResult, FrameRef, Handle, Observation, Receipt,
    )
except ModuleNotFoundError as exc:
    # An internal dependency/import failure must propagate, not select a fallback.
    if exc.name != "wender.perceive.session_registry":
        raise

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
    class ChangeResult:
        handle: Handle
        epoch: int
        settled: bool
        claims: np.ndarray
        ops: dict


@dataclass(frozen=True)
class Scenario:
    name: str
    targets: tuple
    events: tuple
    expected_outcomes: dict


@dataclass(frozen=True)
class WindowEvidence:
    handle: Handle
    scene: str
    epoch: int
    frame_seq: int
    tp: int
    fp: int
    fn: int
    tn: int
    ops: dict
    frame_supported: bool
    dropped: bool
    exact: bool | None
    terminal: str | None
    expected_terminal: str | None
    seal: str
    violations: tuple


@dataclass(frozen=True)
class TrialReport:
    rows: tuple
    outcomes: dict
    faults: tuple


@dataclass(frozen=True)
class Scorecard:
    windows: tuple
    pooled: dict
    metrics: dict
    violations: tuple
    passed: bool


@dataclass
class FakeClock:
    now_ns: int = 0

    def __post_init__(self):
        _integer(self.now_ns, "now_ns")

    def __call__(self):
        return self.now_ns

    def advance(self, delta_ns: int):
        _integer(delta_ns, "delta_ns")
        self.now_ns += delta_ns
        return self.now_ns


class EvidenceError(ValueError):
    """Malformed or unbound integration evidence, before executor admission."""


KINDS = ("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window")
REQUIRED_GATES = tuple(f"G-M{m}-{g}" for m in range(1, 9) for g in range(1, 6))
PREREQUISITE_GATES = tuple(g for g in REQUIRED_GATES if not g.startswith("G-M7-"))
METRIC_KEYS = ("cross_window_bleed", "safety_violations", "frame_exactness",
               "frame_drops", "operation_totals", "eligibility_stalls",
               "starvation", "terminal_completeness", "pooled_confusion")
_SUCCESS = frozenset({"SUCCESS", "COMPLETED", "ACCEPTED", "EXECUTED"})
_INCOMPLETE = frozenset({"UNKNOWN", "ADMITTED", "PENDING", "QUEUED", "PROPOSED",
                         "AUSENTE", "ABSENT", ""})
_QUARANTINE = frozenset({"UNKNOWN", "TIMEOUT", "CRASH", "ERROR"})
_DIAGNOSTICS = frozenset({"diagnostics", "wall_time_ns", "wall_time_s", "wall_time",
                         "wall_clock_timestamp", "timestamp", "host", "hostname",
                         "pid", "host_speed", "vm_timing", "elapsed_wall_ns"})
_SAFETY_BOOLS = ("focus_authorized", "handle_fresh", "fence_clear", "guard_passed",
                 "geometry_matches")
_TELEMETRY_INTS = ("active_resource", "global_committed", "global_reserved",
                   "global_cap", "target_committed", "target_reserved", "target_cap",
                   "wait_opportunities", "queue_depth", "fairness_cost", "quantum",
                   "urgent_cap")

__all__ = ("run_trial", "score_windows", "canonical_bytes", "Scenario",
           "WindowEvidence", "TrialReport", "Scorecard", "FakeClock", "EvidenceError",
           "Handle", "FrameRef", "Observation", "Receipt", "ActionRequest",
           "ChangeResult", "REQUIRED_GATES", "PREREQUISITE_GATES")


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise EvidenceError(f"{name} must be an integer >= {minimum}")
    return value


def _string(value, name):
    if not isinstance(value, str) or not value:
        raise EvidenceError(f"{name} must be a nonempty string")
    value.encode("utf-8")
    return value


def handle_key(handle):
    try:
        kind, name, generation = handle.kind, handle.canonical_id, handle.generation
    except AttributeError as exc:
        raise EvidenceError("handle lacks canonical fields") from exc
    if kind not in KINDS:
        raise EvidenceError("unsupported handle kind")
    return kind, _string(name, "canonical_id"), _integer(generation, "generation", 1)


def _key_text(key):
    return json.dumps(list(key), ensure_ascii=False, separators=(",", ":"))


def _plain(value):
    """Strict semantic values. No nested field name can erase user evidence."""
    if value is None or type(value) in (str, int, bool):
        if isinstance(value, str):
            value.encode("utf-8")
        return value
    if isinstance(value, Mapping):
        if any(type(k) is not str for k in value):
            raise EvidenceError("canonical mapping keys must be strings")
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    raise EvidenceError("unsupported canonical value: " + type(value).__name__)


def _json(value):
    return json.dumps(_plain(value), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _report_bag(value):
    """Only report-level diagnostic bags are projected, never semantic state."""
    return {k: v for k, v in value.items() if k not in _DIAGNOSTICS}


def _row_plain(row, *, manifest=True, seal=True):
    result = {f.name: getattr(row, f.name) for f in fields(WindowEvidence)}
    result["handle"] = list(handle_key(row.handle))
    result["ops"] = {k: v for k, v in _report_bag(row.ops).items() if manifest or k != "_trial"}
    if not seal:
        result.pop("seal")
    return _plain(result)


def canonical_bytes(report):
    """Canonical UTF-8 JSON without diagnostic host or wall-clock fields.

    Only the public TrialReport and Scorecard schemas are projected. Floats,
    numpy scalars/arrays, arbitrary objects and non-string dictionary keys in
    report evidence are errors, not repr() strings with host-dependent identity.
    """
    if isinstance(report, TrialReport):
        return _json({"rows": [_row_plain(row) for row in report.rows],
                      "outcomes": _report_bag(report.outcomes),
                      "faults": [_report_bag(f) if isinstance(f, Mapping) else f for f in report.faults]})
    if isinstance(report, Scorecard):
        return _json({"windows": [_row_plain(row) for row in report.windows],
                      "pooled": report.pooled, "metrics": report.metrics,
                      "violations": report.violations, "passed": report.passed})
    raise EvidenceError("canonical_bytes requires TrialReport or Scorecard")


def _identity(row):
    return (*handle_key(row.handle), row.scene, row.epoch, row.frame_seq)


def _seal(rows, manifest):
    return hashlib.sha256(_json({"manifest": manifest,
                                "rows": [_row_plain(r, manifest=False, seal=False)
                                         for r in rows]})).hexdigest()


def _terminal(status):
    return status.upper() if isinstance(status, str) else "UNKNOWN"


def _complete(status):
    return isinstance(status, str) and _terminal(status) not in _INCOMPLETE


def _gate(status="AUSENTE", evidence=None):
    return {"status": status, "evidence": evidence, "scope": "injected_offline"}


def _preflight(targets, adapters):
    gates = {g: _gate() for g in REQUIRED_GATES}
    missing = []
    evidence = {g: [] for g in PREREQUISITE_GATES}
    statuses = {g: [] for g in PREREQUISITE_GATES}
    for target in targets:
        key = handle_key(target)
        adapter = adapters.get(key)
        label = _key_text(key)
        if adapter is None:
            missing.append("adapter:" + label)
            continue
        if getattr(adapter, "offline", None) is not True:
            missing.append("offline:" + label)
        for component, method in ((None, "reset"), (None, "terminal"),
                                  ("registry", "resolve"), ("stream", "deliver"),
                                  ("perception", "observe"), ("perception", "reduce"),
                                  ("scheduler", "admit"), ("actor", "execute")):
            owner = adapter if component is None else getattr(adapter, component, None)
            if not callable(getattr(owner, method, None)):
                missing.append("interface:" + label + ":" + str(component) + "." + method)
        if not isinstance(getattr(adapter, "resource_key", None), str) or not adapter.resource_key:
            missing.append("resource_key:" + label)
        if getattr(adapter, "focus_domain", None) is not None and (
                not isinstance(adapter.focus_domain, str) or not adapter.focus_domain):
            missing.append("focus_domain:" + label)
        declared = getattr(adapter, "gates", {})
        if not isinstance(declared, Mapping):
            declared = {}
        for g in PREREQUISITE_GATES:
            record = declared.get(g)
            valid = (isinstance(record, Mapping) and record.get("status") == "PASS"
                     and isinstance(record.get("evidence"), str) and bool(record["evidence"])
                     and record.get("expected_failure") is False)
            status = "PASS" if valid else (
                "FAIL" if isinstance(record, Mapping) and record.get("status") == "FAIL"
                else "AUSENTE")
            statuses[g].append(status)
            evidence[g].append({"handle": list(key), "status": status,
                                "evidence": record.get("evidence") if isinstance(record, Mapping)
                                and isinstance(record.get("evidence"), str) else None})
    for g in PREREQUISITE_GATES:
        status = ("PASS" if len(statuses[g]) == len(targets) and statuses[g]
                  and all(s == "PASS" for s in statuses[g]) else
                  "FAIL" if "FAIL" in statuses[g] else "AUSENTE")
        gates[g] = _gate(status, evidence[g])
        if status != "PASS":
            missing.append(g)
    return gates, tuple(missing)


def _validate_scenario(scenario, seed, clock, adapters):
    _string(scenario.name, "scenario.name")
    _integer(seed, "seed")
    if not isinstance(scenario.targets, tuple) or not scenario.targets:
        raise EvidenceError("targets must be a nonempty tuple")
    keys = [handle_key(h) for h in scenario.targets]
    if len(set(keys)) != len(keys):
        raise EvidenceError("duplicate target handle")
    if not isinstance(adapters, Mapping):
        raise EvidenceError("adapters must map canonical handle keys")
    if not callable(clock) or not callable(getattr(clock, "advance", None)):
        raise EvidenceError("an injected advancing integer clock is required")
    _integer(clock(), "clock")
    if not isinstance(scenario.events, tuple) or not isinstance(scenario.expected_outcomes, dict):
        raise EvidenceError("events must be a tuple and expected_outcomes a dict")
    expected = {}
    for action_id, terminal in scenario.expected_outcomes.items():
        expected[_string(action_id, "action_id")] = _string(terminal, "expected terminal")
    identities, actions, previous_at, local = [], {}, 0, {}
    known = set(keys)
    for event in scenario.events:
        if not isinstance(event, Mapping):
            raise EvidenceError("event must be a mapping")
        key = handle_key(event.get("handle"))
        if key not in known:
            raise EvidenceError("foreign event handle")
        at = _integer(event.get("at_ns", 0), "at_ns")
        if at < previous_at:
            raise EvidenceError("events must be ordered by simulated time")
        previous_at = at
        if event.get("kind", "frame") == "recover":
            old = handle_key(event.get("previous_handle"))
            if old not in known or old[:2] != key[:2] or old[2] >= key[2]:
                raise EvidenceError("recovery requires a declared newer incarnation")
            continue
        if event.get("kind", "frame") != "frame":
            raise EvidenceError("unsupported event kind")
        scene = _string(event.get("scene"), "scene")
        epoch = _integer(event.get("epoch"), "epoch")
        seq = _integer(event.get("frame_seq"), "frame_seq")
        if key in local and (epoch < local[key][0] or (epoch == local[key][0] and seq <= local[key][1])):
            raise EvidenceError("frame epoch/sequence must increase locally")
        local[key] = epoch, seq
        identity = (*key, scene, epoch, seq)
        if identity in identities:
            raise EvidenceError("duplicate event identity")
        identities.append(identity)
        rgb, truth = event.get("rgb"), event.get("truth")
        if (not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.ndim != 3
                or rgb.shape[2] != 3 or not rgb.shape[0] or not rgb.shape[1]):
            raise EvidenceError("rgb must be nonempty uint8 HxWx3")
        if not isinstance(truth, np.ndarray) or truth.dtype != np.bool_ or truth.shape != rgb.shape[:2]:
            raise EvidenceError("truth must be a boolean HxW array")
        if type(event.get("drop_expected", False)) is not bool:
            raise EvidenceError("drop_expected must be bool")
        source_action = event.get("action")
        action = (ActionRequest(Handle(*handle_key(source_action.handle)), source_action.action_id,
                                source_action.epoch, copy.deepcopy(source_action.payload),
                                source_action.cost, source_action.deadline_ns)
                  if source_action is not None else None)
        if action is not None:
            aid = _string(action.action_id, "action_id")
            if aid in actions or aid not in expected:
                raise EvidenceError("action id duplicated or lacks explicit expected outcome")
            if handle_key(action.handle) != key or action.epoch != epoch:
                raise EvidenceError("action handle/epoch differs from its frame")
            _integer(action.epoch, "action.epoch")
            _integer(action.cost, "action.cost")
            _integer(action.deadline_ns, "action.deadline_ns")
            if not isinstance(action.payload, dict):
                raise EvidenceError("action payload must be dict")
            _json(action.payload)
            actions[aid] = {"handle": list(key), "expected": expected[aid], "cost": action.cost}
    if set(actions) != set(expected):
        raise EvidenceError("expected outcomes contain undeclared actions")
    return identities, actions


def _snapshot_scenario(scenario):
    """Detach the complete trace before any adapter or clock callback can run."""
    if (not isinstance(scenario.targets, tuple) or not isinstance(scenario.events, tuple)
            or not isinstance(scenario.expected_outcomes, dict)):
        raise EvidenceError("scenario requires tuple targets/events and dict outcomes")
    targets = tuple(Handle(*handle_key(h)) for h in scenario.targets)
    events = []
    for event in scenario.events:
        if not isinstance(event, Mapping):
            raise EvidenceError("event must be a mapping")
        data = copy.deepcopy(dict(event))
        data["handle"] = Handle(*handle_key(event.get("handle")))
        if event.get("kind", "frame") == "recover":
            data["previous_handle"] = Handle(*handle_key(event.get("previous_handle")))
        else:
            for name, dtype in (("rgb", np.uint8), ("truth", np.bool_)):
                value = event.get(name)
                if not isinstance(value, np.ndarray) or value.dtype != dtype:
                    raise EvidenceError(name + " has the wrong array dtype")
                data[name] = np.frombuffer(value.tobytes(order="C"), dtype=dtype).reshape(value.shape)
            original = event.get("action")
            if original is not None:
                try:
                    data["action"] = ActionRequest(Handle(*handle_key(original.handle)), original.action_id,
                        original.epoch, copy.deepcopy(original.payload), original.cost, original.deadline_ns)
                except AttributeError as exc:
                    raise EvidenceError("action lacks required fields") from exc
        events.append(data)
    return Scenario(scenario.name, targets, tuple(events), copy.deepcopy(scenario.expected_outcomes))


def _owned_frame(event):
    # Backed by immutable bytes, not merely setflags(False) on a writable owner.
    rgb = np.frombuffer(event["rgb"].tobytes(order="C"), dtype=np.uint8).reshape(event["rgb"].shape)
    return FrameRef(event["handle"], event["epoch"], event["frame_seq"],
                    int(rgb.shape[1]), int(rgb.shape[0]), rgb,
                    hashlib.sha256(rgb.tobytes()).hexdigest())


def _check_frame(frame, expected):
    if handle_key(frame.handle) != handle_key(expected.handle):
        raise EvidenceError("cross_window_frame")
    for name in ("epoch", "frame_seq", "width", "height"):
        _integer(getattr(frame, name), "frame." + name, 1 if name in ("width", "height") else 0)
        if getattr(frame, name) != getattr(expected, name):
            raise EvidenceError("frame_" + name + "_mismatch")
    rgb = frame.rgb
    if (not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8
            or rgb.shape != (frame.height, frame.width, 3) or not rgb.flags.c_contiguous
            or rgb.flags.writeable):
        raise EvidenceError("frame_not_owned_readonly_rgb")
    if frame.digest != hashlib.sha256(rgb.tobytes()).hexdigest():
        raise EvidenceError("torn_frame")
    return rgb


def _freeze_delivered(frame):
    """A readonly view can have a writable owner; detach it at admission."""
    data = frame.rgb.tobytes(order="C")
    if hashlib.sha256(data).hexdigest() != frame.digest:
        raise EvidenceError("torn_frame")
    rgb = np.frombuffer(data, dtype=np.uint8).reshape((frame.height, frame.width, 3))
    return FrameRef(Handle(*handle_key(frame.handle)), frame.epoch, frame.frame_seq,
                    frame.width, frame.height, rgb, frame.digest)


def _check_observation(observation, frame):
    if handle_key(observation.handle) != handle_key(frame.handle):
        raise EvidenceError("cross_window_observation")
    _integer(observation.epoch, "observation.epoch")
    _integer(observation.frame_seq, "observation.frame_seq")
    if observation.epoch != frame.epoch or observation.frame_seq != frame.frame_seq:
        raise EvidenceError("observation_identity_mismatch")
    _integer(observation.dpr_ppm, "dpr_ppm", 1)
    if not isinstance(observation.state, dict):
        raise EvidenceError("observation state must be a dict")
    _json(observation.state)
    _check_frame(observation.frame, frame)
    if not np.array_equal(observation.frame.rgb, frame.rgb):
        raise EvidenceError("observation_frame_mismatch")


def _check_receipt(receipt, action):
    if handle_key(receipt.handle) != handle_key(action.handle) or receipt.action_id != action.action_id:
        raise EvidenceError("cross_window_receipt")
    _string(receipt.status, "receipt.status")
    if receipt.reason is not None and not isinstance(receipt.reason, str):
        raise EvidenceError("receipt reason must be a string or None")


def _decision(decision, adapter, target_count, action):
    if not isinstance(decision, Mapping):
        raise EvidenceError("scheduler decision must be a mapping")
    for name in ("admitted", "eligible"):
        if type(decision.get(name)) is not bool:
            raise EvidenceError("scheduler " + name + " is AUSENTE")
    reason = decision.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise EvidenceError("scheduler reason must be string or None")
    if not decision["eligible"] and not reason:
        raise EvidenceError("eligibility stall lacks explicit reason")
    raw = decision.get("telemetry")
    if not isinstance(raw, Mapping):
        raise EvidenceError("scheduler telemetry is AUSENTE")
    telemetry = {"resource_key": _string(raw.get("resource_key"), "resource_key")}
    if telemetry["resource_key"] != adapter.resource_key:
        raise EvidenceError("cross_window_resource")
    for name in _TELEMETRY_INTS:
        telemetry[name] = _integer(raw.get(name), name)
    for name in _SAFETY_BOOLS:
        if type(raw.get(name)) is not bool:
            raise EvidenceError(name + " is AUSENTE")
        telemetry[name] = raw[name]
    if getattr(adapter, "focus_domain", None):
        telemetry["active_focus"] = _integer(raw.get("active_focus"), "active_focus")
    violations = []
    if decision["admitted"]:
        if not decision["eligible"]:
            violations.append("admitted_ineligible")
        for name in _SAFETY_BOOLS:
            if not telemetry[name]:
                violations.append("unsafe_" + name)
        if telemetry["active_resource"] != 1:
            violations.append("resource_concurrency")
        if getattr(adapter, "focus_domain", None) and telemetry.get("active_focus") != 1:
            violations.append("focus_concurrency")
    for prefix in ("global", "target"):
        if telemetry[prefix + "_committed"] + telemetry[prefix + "_reserved"] > telemetry[prefix + "_cap"]:
            violations.append(prefix + "_budget_exceeded")
        if decision["admitted"] and telemetry[prefix + "_reserved"] < action.cost:
            violations.append(prefix + "_budget_unreserved")
    if any(telemetry[n] != 1 for n in ("fairness_cost", "quantum", "urgent_cap")):
        violations.append("fairness_contract_AUSENTE")
    if decision["eligible"] and telemetry["wait_opportunities"] > 2 * target_count:
        violations.append("starvation")
    return {"admitted": decision["admitted"], "eligible": decision["eligible"],
            "reason": reason, "telemetry": telemetry}, violations


def _run_once(scenario, seed, clock, adapters, external_stalls=None):
    rows, faults, actions, recoveries = [], [], {}, []
    quarantined, fenced_resources, fenced_focus, active = set(), {}, {}, set()
    consumed_recoveries = set()
    global_charged, target_charged = 0, defaultdict(int)
    observed_global_cap, observed_target_caps = None, {}
    external_stalls = external_stalls or {}
    start = _integer(clock(), "clock")
    last_time = start

    def now():
        nonlocal last_time
        value = _integer(clock(), "clock")
        if value < last_time:
            raise EvidenceError("clock moved backwards")
        last_time = value
        return value

    reset_failed = set()
    for handle in sorted(scenario.targets, key=handle_key):
        key = handle_key(handle)
        local_seed = int.from_bytes(hashlib.sha256(_json([seed, list(key)])).digest()[:16], "big")
        try:
            adapters[key].reset(seed=local_seed, clock=clock)
            now()
        except Exception as exc:
            reset_failed.add(key)
            faults.append({"handle": list(key), "stage": "reset", "terminal": "ERROR",
                           "error_type": type(exc).__name__})
    for index, event in enumerate(scenario.events):
        current_time = now()
        due = start + event.get("at_ns", 0)
        if current_time < due:
            clock.advance(due - current_time)
            now()
        key = handle_key(event["handle"])
        adapter = adapters[key]
        if event.get("kind", "frame") == "recover":
            old = handle_key(event["previous_handle"])
            recovery_pair = (old, key)
            resource = adapter.resource_key
            domain = getattr(adapter, "focus_domain", None)
            ok = (event.get("acknowledged") is True and old in quarantined
                  and key not in quarantined and key not in reset_failed
                  and recovery_pair not in consumed_recoveries
                  and fenced_resources.get(resource) == old
                  and (domain is None or fenced_focus.get(domain) == old))
            if ok:
                try:
                    record = adapter.registry.resolve(event["handle"])
                    ok = (handle_key(record.handle) == key and record.closed is False
                          and adapters[old].resource_key == adapter.resource_key
                          and getattr(adapters[old], "focus_domain", None) == domain)
                except Exception:
                    ok = False
            if ok:
                fenced_resources.pop(resource)
                if domain is not None:
                    fenced_focus.pop(domain)
                consumed_recoveries.add(recovery_pair)
            recoveries.append({"previous_handle": list(old), "handle": list(key),
                               "generation": key[2], "acknowledged": event.get("acknowledged") is True,
                               "recovered": ok})
            if not ok:
                quarantined.add(key)
                faults.append({"handle": list(key), "stage": "recovery", "terminal": "QUARANTINED"})
            continue
        frame = _owned_frame(event)
        source_action = event.get("action")
        action = (ActionRequest(Handle(*handle_key(source_action.handle)), source_action.action_id,
                                source_action.epoch, copy.deepcopy(source_action.payload),
                                source_action.cost, source_action.deadline_ns)
                  if source_action is not None else None)
        expected = scenario.expected_outcomes.get(action.action_id) if action is not None else None
        terminal, admitted, supported, dropped, exact = None, False, False, False, None
        confusion = [0, 0, 0, 0]
        violations = []
        details = {"counts": {}, "drop_expected": event.get("drop_expected", False),
                   "pixel_mismatches": 0, "torn": False, "sent": False,
                   "state_digest": None, "claims_digest": None, "dpr_ppm": None,
                   "dispatch": None, "scheduler_attempted": False, "stall_reason": None,
                   "action_id": action.action_id if action is not None else None,
                   "admitted": False, "resource_key": adapter.resource_key,
                   "charged_cost": 0, "runtime_global_charged": global_charged,
                   "runtime_target_charged": target_charged[adapter.resource_key],
                   "focus_domain": getattr(adapter, "focus_domain", None),
                   "quarantined": key in quarantined, "event_index": index}
        stage = "registry"
        try:
            if key in reset_failed:
                raise EvidenceError("reset_AUSENTE")
            domain = getattr(adapter, "focus_domain", None)
            event_id = (*key, event["scene"], frame.epoch, frame.frame_seq)
            if (key in quarantined or adapter.resource_key in fenced_resources
                    or (domain is not None and domain in fenced_focus)
                    or event_id in external_stalls):
                terminal = "QUARANTINED" if action is not None else None
                details["quarantined"] = True
                details["stall_reason"] = ("quarantined" if key in quarantined else
                                           external_stalls.get(event_id, "external_quarantine"))
            else:
                record = adapter.registry.resolve(event["handle"])
                if handle_key(record.handle) != key or record.closed is not False:
                    raise EvidenceError("stale_handle")
                _integer(record.epoch, "record.epoch")
                if record.epoch != frame.epoch:
                    raise EvidenceError("stale_geometry")
                stage = "stream"
                delivered = adapter.stream.deliver(frame)
                if delivered is None:
                    dropped = True
                    terminal = "DROPPED" if action is not None else None
                    details["stall_reason"] = "frame_dropped"
                else:
                    rgb = _check_frame(delivered, frame)
                    supported = True
                    mismatches = int(np.count_nonzero(np.any(rgb != frame.rgb, axis=2)))
                    details["pixel_mismatches"] = mismatches
                    exact = mismatches == 0
                    if not exact:
                        raise EvidenceError("pixel_mismatch")
                    delivered = _freeze_delivered(delivered)
                    stage = "perception"
                    observation = adapter.perception.observe(delivered)
                    _check_observation(observation, delivered)
                    observation = Observation(Handle(*key), delivered.epoch, delivered.frame_seq,
                                              observation.dpr_ppm, copy.deepcopy(observation.state), delivered)
                    details["state_digest"] = hashlib.sha256(_json(observation.state)).hexdigest()
                    details["dpr_ppm"] = observation.dpr_ppm
                    change = adapter.perception.reduce(observation)
                    _integer(change.epoch, "change.epoch")
                    if handle_key(change.handle) != key or change.epoch != frame.epoch:
                        raise EvidenceError("cross_window_change")
                    if type(change.settled) is not bool:
                        raise EvidenceError("change.settled must be bool")
                    claims = change.claims
                    if not isinstance(claims, np.ndarray) or claims.dtype != np.bool_ or claims.shape != event["truth"].shape:
                        raise EvidenceError("claims must be a boolean HxW mask")
                    if not isinstance(change.ops, dict) or any(type(k) is not str or not k for k in change.ops):
                        raise EvidenceError("operation counts must have nonempty string keys")
                    details["counts"] = {k: _integer(v, "operation count") for k, v in change.ops.items()}
                    claims = np.frombuffer(claims.tobytes(order="C"), dtype=np.bool_).reshape(claims.shape)
                    change = ChangeResult(Handle(*key), change.epoch, change.settled,
                                          claims, copy.deepcopy(details["counts"]))
                    details["claims_digest"] = hashlib.sha256(claims.tobytes(order="C")).hexdigest()
                    if hashlib.sha256(_json(observation.state)).hexdigest() != details["state_digest"]:
                        raise EvidenceError("cross_window_state_mutation")
                    truth = event["truth"]
                    confusion = [int(np.count_nonzero(claims & truth)),
                                 int(np.count_nonzero(claims & ~truth)),
                                 int(np.count_nonzero(~claims & truth)),
                                 int(np.count_nonzero(~claims & ~truth))]
                    if action is not None:
                        stage = "scheduler"
                        if not change.settled:
                            terminal = "NOT_READY"
                            details["stall_reason"] = "not_settled"
                        elif now() >= action.deadline_ns:
                            terminal = "TIMEOUT"
                            details["stall_reason"] = "deadline_expired"
                        else:
                            details["scheduler_attempted"] = True
                            decision, bad = _decision(adapter.scheduler.admit(action, observation, change, now()),
                                                      adapter, len(scenario.targets), action)
                            details["dispatch"] = decision
                            telem = decision["telemetry"]
                            cap = telem["global_cap"]
                            target_cap = telem["target_cap"]
                            if observed_global_cap is None:
                                observed_global_cap = cap
                            elif observed_global_cap != cap:
                                bad.append("global_budget_cap_changed")
                            previous_cap = observed_target_caps.setdefault(adapter.resource_key, target_cap)
                            if previous_cap != target_cap:
                                bad.append("target_budget_cap_changed")
                            if decision["admitted"]:
                                if global_charged + action.cost > cap:
                                    bad.append("global_budget_exceeded")
                                if target_charged[adapter.resource_key] + action.cost > target_cap:
                                    bad.append("target_budget_exceeded")
                            if hashlib.sha256(_json(observation.state)).hexdigest() != details["state_digest"]:
                                bad.append("cross_window_state_mutation")
                            violations.extend(bad)
                            if bad:
                                terminal = "REFUSED"
                            elif not decision["admitted"]:
                                terminal = decision["reason"] or "REFUSED"
                                details["stall_reason"] = decision["reason"] or "refused"
                            elif adapter.resource_key in active:
                                terminal = "REFUSED"
                                violations.append("resource_concurrency")
                            elif now() >= action.deadline_ns:
                                terminal = "TIMEOUT"
                            else:
                                active.add(adapter.resource_key)
                                stage = "actor"
                                details["sent"] = True
                                global_charged += action.cost
                                target_charged[adapter.resource_key] += action.cost
                                details["charged_cost"] = action.cost
                                receipt = adapter.actor.execute(action, observation, now())
                                _check_receipt(receipt, action)
                                admitted = _terminal(receipt.status) in _SUCCESS | {"ADMITTED"}
                                if admitted:
                                    stage = "terminal"
                                    final = adapter.terminal(action, receipt, now())
                                    _check_receipt(final, action)
                                    terminal = final.status
                                else:
                                    # A local refusal cannot become success through
                                    # an unrelated/optimistic terminal callback.
                                    terminal = receipt.status
                                    global_charged -= action.cost
                                    target_charged[adapter.resource_key] -= action.cost
                                    details["charged_cost"] = 0
                                if hashlib.sha256(_json(observation.state)).hexdigest() != details["state_digest"]:
                                    raise EvidenceError("cross_window_state_mutation")
                                if now() >= action.deadline_ns and _terminal(terminal) in _SUCCESS:
                                    violations.append("deadline_restarted")
                                    terminal = "TIMEOUT"
                                active.discard(adapter.resource_key)
        except Exception as exc:
            terminal = ("TIMEOUT" if isinstance(exc, TimeoutError) else
                        "CRASH" if isinstance(exc, ConnectionError) else "ERROR") if action is not None else None
            if isinstance(exc, EvidenceError):
                violations.append(str(exc))
                details["torn"] = str(exc) == "torn_frame"
            else:
                # No arbitrary exception message/repr enters deterministic evidence.
                violations.append("component_" + stage + "_" + type(exc).__name__)
            faults.append({"handle": list(key), "action_id": details["action_id"], "stage": stage,
                           "terminal": terminal, "error_type": type(exc).__name__})
            active.discard(adapter.resource_key)
        if terminal is not None and (_terminal(terminal) in _QUARANTINE
                                     or (admitted and not _complete(terminal))):
            quarantined.add(key)
            fenced_resources[adapter.resource_key] = key
            domain = getattr(adapter, "focus_domain", None)
            if domain is not None:
                fenced_focus[domain] = key
            details["quarantined"] = True
            # Expected transport faults are outcomes, not automatically violations.
            if _terminal(terminal) in ("CRASH", "TIMEOUT") and terminal == expected:
                violations = [v for v in violations if not v.startswith("component_")]
            if not any(f.get("action_id") == details["action_id"] for f in faults):
                faults.append({"handle": list(key), "action_id": details["action_id"],
                               "stage": "terminal", "terminal": terminal})
        details["admitted"] = admitted
        details["runtime_global_charged"] = global_charged
        details["runtime_target_charged"] = target_charged[adapter.resource_key]
        if action is not None:
            actions[action.action_id] = {"handle": list(key), "terminal": terminal,
                                         "expected": expected, "admitted": admitted,
                                         "sent": details["sent"]}
        rows.append(WindowEvidence(event["handle"], event["scene"], frame.epoch, frame.frame_seq,
                                   *confusion, details, supported, dropped, exact, terminal,
                                   expected, "", tuple(violations)))
    elapsed = now() - start
    if elapsed < 0:
        raise EvidenceError("clock moved backwards")
    return tuple(rows), {"actions": actions, "elapsed_ns": elapsed, "recoveries": recoveries,
                         "quarantines": [list(k) for k in sorted(quarantined)]}, tuple(faults)


def _execution_bytes(rows, outcomes, faults):
    return _json({"rows": [_row_plain(r, manifest=False, seal=False) for r in rows],
                  "outcomes": outcomes, "faults": faults})


def run_trial(scenario, *, seed, clock, adapters):
    """Run injected prerequisites, interleaved/isolated/repeated replay, then seal."""
    scenario = _snapshot_scenario(scenario)
    identities, required_actions = _validate_scenario(scenario, seed, clock, adapters)
    gates, missing = _preflight(scenario.targets, adapters)
    absent = {"measured_host_speed": "AUSENTE", "native_capture": "AUSENTE",
              "G-T4": "AUSENTE", "G-T5": "AUSENTE"}
    if missing:
        return TrialReport((), {"scenario": scenario.name, "seed": seed, "blocked": True,
                               "missing_prerequisites": list(missing), "gates": gates,
                               "actions": {}, "absent": absent,
                               "scope": "injected_offline"},
                           ({"stage": "prerequisites", "terminal": "AUSENTE"},))
    start = clock()
    rows, outcomes, faults = _run_once(scenario, seed, clock, adapters)
    initial_bytes = _execution_bytes(rows, outcomes, faults)
    baseline = {}
    isolated_faults = []
    external_stalls = {_identity(r): r.ops["stall_reason"] for r in rows
                       if r.ops.get("stall_reason") == "external_quarantine"}
    # Incarnations of one lane share recovery events, but reset/RNG per handle.
    lanes = sorted({handle_key(h)[:2] for h in scenario.targets})
    for lane in lanes:
        targets = tuple(h for h in scenario.targets if handle_key(h)[:2] == lane)
        events = tuple(e for e in scenario.events if handle_key(e["handle"])[:2] == lane)
        aids = {e["action"].action_id for e in events if e.get("action") is not None}
        isolated = Scenario(scenario.name, targets, events,
                            {k: v for k, v in scenario.expected_outcomes.items() if k in aids})
        solo_rows, _, solo_faults = _run_once(isolated, seed, FakeClock(start), adapters, external_stalls)
        if solo_faults:
            isolated_faults.append({"lane": list(lane), "faults": list(solo_faults)})
        baseline.update({_identity(r): r for r in solo_rows})
    repeated_rows, repeated_outcomes, repeated_faults = _run_once(scenario, seed, FakeClock(start), adapters)
    repeated_bytes = _execution_bytes(repeated_rows, repeated_outcomes, repeated_faults)
    enriched = []
    for row in rows:
        solo = baseline.get(_identity(row))
        details = copy.deepcopy(row.ops)
        details["expected_counts"] = copy.deepcopy(solo.ops["counts"]) if solo is not None else None
        details["isolated_confusion"] = [solo.tp, solo.fp, solo.fn, solo.tn] if solo is not None else None
        details["isolated_match"] = (solo is not None and
            all(row.ops.get(k) == solo.ops.get(k) for k in ("state_digest", "claims_digest", "dpr_ppm"))
            and (row.tp, row.fp, row.fn, row.tn) == (solo.tp, solo.fp, solo.fn, solo.tn))
        violations = list(row.violations)
        if not details["isolated_match"]:
            violations.append("cross_window_state_bleed")
        if details["counts"] != details["expected_counts"]:
            violations.append("operation_totals_mismatch")
        enriched.append(replace(row, ops=details, violations=tuple(violations)))
    manifest = {"version": "multiwin-scorecard-v1", "scenario": scenario.name, "seed": seed,
                "targets": [list(handle_key(h)) for h in scenario.targets],
                "identities": [list(i) for i in identities], "actions": required_actions,
                "gates": gates, "replay_match": initial_bytes == repeated_bytes,
                "replay_sha256": hashlib.sha256(initial_bytes).hexdigest(),
                "repeat_sha256": hashlib.sha256(repeated_bytes).hexdigest(),
                "isolated_support": len(baseline), "elapsed_ns": outcomes["elapsed_ns"],
                "isolated_external_stalls": [list(k) for k in sorted(external_stalls)],
                "isolated_faults": isolated_faults, "repeat_faults": list(repeated_faults),
                "recoveries": outcomes["recoveries"], "quarantines": outcomes["quarantines"],
                "scope": "injected_offline", "absent": absent}
    seal = _seal(enriched, manifest)
    sealed = tuple(replace(r, seal=seal, ops={**r.ops, "_trial": copy.deepcopy(manifest)})
                   for r in enriched)
    card = score_windows(sealed)
    outcomes = {**outcomes, "scenario": scenario.name, "seed": seed, "blocked": False,
                "gates": card.metrics["terminal_completeness"]["gates"],
                "passed": card.passed, "absent": absent, "scope": "injected_offline",
                "isolated_faults": isolated_faults, "repeat_faults": list(repeated_faults),
                "execution_runs": len(lanes) + 2,
                "supported_kinds": sorted({h.kind for h in scenario.targets}),
                "missing_supported_kinds": sorted(set(KINDS) - {h.kind for h in scenario.targets}),
                "canonical_replay": {"matched": initial_bytes == repeated_bytes,
                                     "first_sha256": manifest["replay_sha256"],
                                     "repeat_sha256": manifest["repeat_sha256"]}}
    return TrialReport(sealed, outcomes, faults)


def _metric(numerator, denominator, passed, **extra):
    if denominator is None or denominator == 0:
        numerator, denominator, passed = None, None, False
    return {"numerator": numerator, "denominator": denominator, "passed": bool(passed), **extra}


def _validate_row(row):
    handle_key(row.handle)
    _string(row.scene, "scene")
    for name in ("epoch", "frame_seq", "tp", "fp", "fn", "tn"):
        _integer(getattr(row, name), name)
    if not isinstance(row.ops, dict) or not isinstance(row.violations, tuple):
        raise EvidenceError("row ops/violations have wrong types")
    if any(not isinstance(v, str) for v in row.violations):
        raise EvidenceError("violation must be a string")
    if type(row.frame_supported) is not bool or type(row.dropped) is not bool:
        raise EvidenceError("frame support/drop flags must be bool")
    if row.exact is not None and type(row.exact) is not bool:
        raise EvidenceError("exact must be bool or None")
    if not isinstance(row.seal, str):
        raise EvidenceError("seal must be a string")
    if any(v is not None and not isinstance(v, str) for v in (row.terminal, row.expected_terminal)):
        raise EvidenceError("terminals must be strings or None")
    _row_plain(row)


def score_windows(rows):
    """Validate independently sealed windows, preserve every metric, conjoin gates.

    Invalid schemas raise EvidenceError. Duplicate/foreign/missing rows, mixed or
    absent seals and mismatched evidence return a failing Scorecard with counts.
    """
    rows = tuple(rows)
    for row in rows:
        _validate_row(row)
    violations = []
    manifests = [r.ops.get("_trial") for r in rows]
    manifest = manifests[0] if manifests and isinstance(manifests[0], dict) else {}
    manifest_ok = bool(manifest) and all(m == manifest for m in manifests)
    if not manifest_ok:
        violations.append("missing_or_mixed_manifest")
    identities = [_identity(r) for r in rows]
    if len(set(identities)) != len(identities):
        violations.append("duplicate_window_identity")
    declared = {tuple(h) for h in manifest.get("targets", [])}
    expected_ids = [tuple(i) for i in manifest.get("identities", [])]
    if Counter(identities) != Counter(expected_ids):
        violations.append("missing_or_foreign_rows")
    if {handle_key(r.handle) for r in rows} != declared:
        violations.append("missing_or_foreign_target")
    seals = {r.seal for r in rows}
    seal_ok = bool(rows) and len(seals) == 1 and "" not in seals and manifest_ok
    if seal_ok:
        seal_ok = next(iter(seals)) == _seal(rows, manifest)
    if not seal_ok:
        violations.append("missing_mixed_or_invalid_seal")
    for row in rows:
        violations.extend(row.violations)
    pooled = {k: sum(getattr(row, k) for row in rows) for k in ("tp", "fp", "fn", "tn")}
    frame_support = sum(r.frame_supported and not r.dropped for r in rows)
    exact_support = sum(r.frame_supported and not r.dropped and r.exact is not None for r in rows)
    exact_good = sum(r.frame_supported and not r.dropped and r.exact is True for r in rows)
    drops = sum(r.dropped for r in rows)
    expected_drops = sum(r.ops.get("drop_expected") is True for r in rows)
    correct_drops = sum(type(r.ops.get("drop_expected")) is bool
                        and r.dropped == r.ops["drop_expected"] for r in rows)
    pixels = sum(pooled.values())
    target_frame_support = []
    for key in sorted(declared):
        target_rows = [r for r in rows if handle_key(r.handle) == key]
        delivered = sum(r.frame_supported and not r.dropped for r in target_rows)
        good = sum(r.frame_supported and not r.dropped and r.exact is True for r in target_rows)
        target_frame_support.append({"handle": list(key),
                                     **_metric(good, delivered, good == delivered)})
        if delivered == 0:
            violations.append("missing_frame_support:" + _key_text(key))
    bleed = sum(1 for v in violations if any(t in v for t in
                ("cross_window", "foreign", "identity_mismatch", "missing_or_foreign_target")))
    bleed += sum(r.ops.get("isolated_match") is not True for r in rows)
    safety_count = sum(1 for v in violations if any(t in v for t in
                       ("unsafe_", "concurrency", "budget_", "stale_", "deadline_", "guard_", "admitted_ineligible")))
    counts, expected_counts, ops_support, ops_matches = defaultdict(int), defaultdict(int), 0, 0
    target_ops_support, target_ops_matches = defaultdict(int), defaultdict(int)
    decisions, eligible, stalls, fair, decision_support, eligibility_support = 0, 0, {}, 0, 0, 0
    queue_maxima, per_window_ops = {}, []
    action_rows = {}
    action_duplicates = False
    for row in rows:
        actual, expected = row.ops.get("counts"), row.ops.get("expected_counts")
        good_counts = (isinstance(actual, dict) and isinstance(expected, dict)
                       and all(type(k) is str and type(v) is int and v >= 0 for k, v in actual.items())
                       and all(type(k) is str and type(v) is int and v >= 0 for k, v in expected.items()))
        if good_counts:
            for name in set(actual) | set(expected):
                counts[name] += actual.get(name, 0)
                expected_counts[name] += expected.get(name, 0)
            ops_support += sum(expected.values())
            target_ops_support[handle_key(row.handle)] += sum(expected.values())
            if actual == expected:
                ops_matches += sum(expected.values())
                target_ops_matches[handle_key(row.handle)] += sum(expected.values())
        per_window_ops.append({"identity": list(_identity(row)), "actual": actual,
                               "isolated": expected, "matches": good_counts and actual == expected})
        aid = row.ops.get("action_id")
        if aid is not None:
            if aid in action_rows:
                action_duplicates = True
            action_rows[aid] = row
            decisions += row.ops.get("scheduler_attempted") is True
            decision = row.ops.get("dispatch")
            if isinstance(decision, dict) and type(decision.get("eligible")) is bool:
                telemetry = decision.get("telemetry", {})
                decision_support += 1
                eligibility_support += 1
                key_text = _key_text(handle_key(row.handle))
                depth = telemetry.get("queue_depth")
                if type(depth) is int:
                    queue_maxima[key_text] = max(queue_maxima.get(key_text, 0), depth)
                if decision["eligible"]:
                    eligible += 1
                    wait = telemetry.get("wait_opportunities")
                    if (type(wait) is int and 0 <= wait <= 2 * len(declared)
                            and all(telemetry.get(k) == 1 for k in ("fairness_cost", "quantum", "urgent_cap"))):
                        fair += 1
                else:
                    reason = decision.get("reason")
                    label = reason if isinstance(reason, str) and reason else "AUSENTE"
                    stalls.setdefault(key_text, {})[label] = stalls.setdefault(key_text, {}).get(label, 0) + 1
            elif row.ops.get("scheduler_attempted") is False:
                reason = row.ops.get("stall_reason")
                if reason in ("quarantined", "external_quarantine", "frame_dropped", "deadline_expired", "not_settled"):
                    key_text = _key_text(handle_key(row.handle))
                    stalls.setdefault(key_text, {})[reason] = stalls.setdefault(key_text, {}).get(reason, 0) + 1
                    if _complete(row.terminal) and row.terminal == row.expected_terminal:
                        eligibility_support += 1
    required = manifest.get("actions", {})
    if not isinstance(required, dict):
        required = {}
    if set(action_rows) != set(required) or action_duplicates:
        violations.append("missing_foreign_or_duplicate_action")
    nominal_total = nominal_good = fault_total = fault_good = 0
    terminal_good = True
    missing_terminals = []
    for aid, requirement in required.items():
        row = action_rows.get(aid)
        expected = requirement.get("expected") if isinstance(requirement, dict) else None
        match = (row is not None and _complete(row.terminal) and _complete(expected)
                 and row.terminal == expected and row.expected_terminal == expected
                 and list(handle_key(row.handle)) == requirement.get("handle"))
        if not match:
            terminal_good = False
            missing_terminals.append(aid)
        if _terminal(expected) in _SUCCESS:
            # Missing admission must not remove an expected successful action.
            nominal_total += 1
            nominal_good += bool(match and row.ops.get("admitted") is True)
        else:
            fault_total += 1
            fault_good += bool(match)
    if missing_terminals:
        violations.append("incomplete_or_unexpected_terminal")
    successful = sum(r.ops.get("admitted") is True and _terminal(r.terminal) in _SUCCESS
                     and r.terminal == r.expected_terminal for r in rows)
    elapsed = manifest.get("elapsed_ns")
    elapsed_supported = type(elapsed) is int and elapsed > 0
    if not elapsed_supported:
        violations.append("simulated_elapsed_AUSENTE")
    target_operation_support = []
    for key in sorted(declared):
        support, matched = target_ops_support[key], target_ops_matches[key]
        target_operation_support.append({"handle": list(key),
                                         **_metric(matched, support, matched == support)})
        if support == 0:
            violations.append("missing_operation_support:" + _key_text(key))
    throughput = {"numerator": successful if type(elapsed) is int and elapsed > 0 else None,
                  "denominator": elapsed if type(elapsed) is int and elapsed > 0 else None,
                  "unit": "successful_actions_per_simulated_nanosecond"}
    gates = {g: copy.deepcopy(manifest.get("gates", {}).get(g, _gate())) for g in REQUIRED_GATES}
    coverage_ok = bool(rows) and not any("foreign" in v or "duplicate_window" in v for v in violations)
    gates["G-M7-1"] = _gate("PASS" if rows and manifest.get("replay_match") is True
                                  and manifest.get("replay_sha256") == manifest.get("repeat_sha256")
                                  else "FAIL", "independent repeated canonical replay")
    gates["G-M7-2"] = _gate("PASS" if rows and bleed == 0
                                  and manifest.get("isolated_support", 0) >= len(rows) else "FAIL",
                                  "isolated target state, claims, DPR and operation comparisons")
    gates["G-M7-3"] = _gate("PASS" if coverage_ok else "FAIL", "all declared identities and kinds retained")
    gates["G-M7-4"] = _gate("PASS" if seal_ok and coverage_ok and pixels > 0 else "FAIL",
                                  "manifest seal and elementwise integer confusion")
    gates["G-M7-5"] = _gate("PASS" if required and terminal_good and not missing_terminals
                                  and all(r.get("recovered") is True for r in manifest.get("recoveries", []))
                                  else "FAIL", "explicit terminal and recovery outcomes")
    metrics = {
        "cross_window_bleed": _metric(bleed, len(rows), bleed == 0 and coverage_ok),
        "safety_violations": _metric(safety_count, decisions,
                                     safety_count == 0 and decision_support == decisions),
        "frame_exactness": _metric(exact_good, frame_support,
                                   exact_good == frame_support and exact_support == frame_support
                                   and all(t["passed"] for t in target_frame_support)
                                   and all(not r.ops.get("torn", False)
                                           and r.ops.get("pixel_mismatches") == 0 for r in rows),
                                   targets=target_frame_support,
                                   delivered=frame_support, pixel_mismatches=sum(r.ops.get("pixel_mismatches", 0)
                                                                               for r in rows)),
        "frame_drops": _metric(correct_drops, len(rows), correct_drops == len(rows),
                               observed=drops, expected=expected_drops),
        "operation_totals": _metric(ops_matches, ops_support,
                                    ops_matches == ops_support and all(p["matches"] for p in per_window_ops)
                                    and all(t["passed"] for t in target_operation_support),
                                    actual=dict(sorted(counts.items())), isolated=dict(sorted(expected_counts.items())),
                                    windows=per_window_ops, targets=target_operation_support),
        "eligibility_stalls": _metric(sum(sum(v.values()) for v in stalls.values()), len(action_rows),
                                      eligibility_support == len(action_rows)
                                      and not any("AUSENTE" in v for v in stalls.values()),
                                      windows=stalls, observed_decisions=decision_support,
                                      queue_maxima=queue_maxima),
        "starvation": _metric(fair, eligible, fair == eligible, bound_opportunities=2 * len(declared)),
        "terminal_completeness": _metric(nominal_good, nominal_total,
                                         nominal_good == nominal_total and terminal_good
                                         and set(action_rows) == set(required) and not action_duplicates
                                         and elapsed_supported,
                                         fault_outcomes={"numerator": fault_good if fault_total else None,
                                                         "denominator": fault_total or None,
                                                         "matched": fault_good == fault_total},
                                         missing=missing_terminals, gates=gates,
                                         simulated_throughput=throughput,
                                         quarantines=manifest.get("quarantines", []),
                                         recoveries=manifest.get("recoveries", [])),
        "pooled_confusion": _metric(pooled["tp"] + pooled["tn"], pixels,
                                    pooled["fp"] == 0 and pooled["fn"] == 0 and coverage_ok and seal_ok,
                                    **pooled),
    }
    violations = tuple(dict.fromkeys(violations))
    passed = (not violations and seal_ok and all(m["passed"] for m in metrics.values())
              and all(isinstance(g, dict) and g.get("status") == "PASS" for g in gates.values()))
    return Scorecard(rows, pooled, metrics, violations, passed)
