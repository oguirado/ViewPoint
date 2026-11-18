"""Build immutable MemoryWeb drafts from an admitted, reviewed recording."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from jsonschema import Draft202012Validator, ValidationError

from control.core.control import Handler, Outcome
from recorder.store.memoryweb_store import StoreError, canonical_bytes
from schemas.validation import validate_contract

if TYPE_CHECKING:
    from metaviewer_agents.mw_owner import ReplayOwner


_CONTRACTS = Path(__file__).resolve().parents[4] / "memoryweb-builtin/standards/schemas"
_LIMIT = 65_536
_CLICK_ACTIONS = {"click", "right_click"}


class CompileError(ValueError):
    """A refusal before candidate publication."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ReviewedClick:
    kind: str
    button: int
    target: dict
    postcondition: dict


@dataclass(frozen=True)
class RecordingReview:
    """Trusted recorder/owner input. Keys in clicks and navigation are event indexes.

    The reviewer attests to the observed target, button and post-state for each
    indexed action in the exact, redacted timeline named by timeline_sha256.
    The explanation is reviewed public text. Never populate this object from
    control request arguments.
    """

    timeline_sha256: str
    demo_id: str
    workflow_id: str
    recorded_date: str
    surface: dict
    explanation: bytes
    evaluation_profile: dict
    clicks: dict[int, ReviewedClick]
    navigation: dict[int, dict]


class RecordingResolver(Protocol):
    """Resolve a C1-stamped context to an authorized, sealed U2a recording.

    Check caller and agent_instance, the session or capsule incarnation, and
    current authority epoch against trusted state. Return None if the binding
    or reviewed recording is unavailable. The timeline is the sealed U2a
    snapshot or its canonical bytes. RecordingReview binds the SHA-256 of those
    bytes and uses zero-based indexes in the snapshot's event order. The owner
    is the ReplayOwner for the admitted site. Review evidence comes from the
    trusted owner, since rec.timeline/v1 has no typed replay target or
    postcondition fields.
    """

    def __call__(self, context: dict, /) -> tuple[bytes | dict, RecordingReview, ReplayOwner] | None: ...


@dataclass(frozen=True)
class Compilation:
    demo: bytes
    demo_steps: bytes
    workflow: bytes
    explanation: bytes

    def digests(self):
        return {name + "_sha256": _digest(getattr(self, name))
                for name in ("demo", "demo_steps", "workflow", "explanation")}


def _digest(raw):
    return sha256(raw).hexdigest()


@lru_cache(maxsize=None)
def _validator(name):
    filename = name + ".v1.json"
    raw = (_CONTRACTS / filename).read_bytes()
    pins = json.loads((_CONTRACTS / "PINS.json").read_bytes())
    if pins["views"].get(filename) != _digest(raw):
        raise CompileError("SCHEMA_PIN_MISMATCH")
    return Draft202012Validator(json.loads(raw))


def _validate_memoryweb(name, value):
    try:
        _validator(name).validate(value)
    except ValidationError:
        raise CompileError("OUT_OF_SUPPORT") from None


def _encode(value, limit=_LIMIT):
    try:
        raw = canonical_bytes(value)
    except (ValueError, TypeError, RecursionError):
        raise CompileError("OUT_OF_SUPPORT") from None
    if len(raw) > limit:
        raise CompileError("ARTIFACT_OVERSIZE")
    return raw


def _decode_timeline(timeline):
    if isinstance(timeline, bytes):
        if len(timeline) > _LIMIT:
            raise CompileError("ARTIFACT_OVERSIZE")
        try:
            raw = timeline
            timeline = json.loads(raw)
        except (ValueError, RecursionError):
            raise CompileError("OUT_OF_SUPPORT") from None
        if raw != _encode(timeline):
            raise CompileError("JSON_NONCANONICAL")
    else:
        raw = _encode(timeline)
        timeline = json.loads(raw)
    try:
        validate_contract("rec.timeline/v1", timeline)
    except ValidationError:
        raise CompileError("OUT_OF_SUPPORT") from None
    return timeline["events"], _digest(raw)


def _ordered_actions(events, review, owner):
    if not events:
        raise CompileError("OUT_OF_SUPPORT")
    binding = (events[0]["capsule_id"], events[0]["incarnation"])
    actions = []
    preconditions = []
    clicks, navigation = set(), set()
    previous = events[0]["mono_ns"]
    for index, row in enumerate(events):
        if (row["capsule_id"], row["incarnation"]) != binding:
            raise CompileError("NO_LEASE")
        if row.get("gap") is True:
            raise CompileError("OUT_OF_SUPPORT")
        if row["mono_ns"] < previous:
            raise CompileError("OUT_OF_SUPPORT")
        previous = row["mono_ns"]
        kind = row.get("action")
        if kind is None:
            # U2a drops the action from legacy text rows without a value digest.
            # Refuse incomplete action rows before building a replay.
            if (row["source"], row.get("op")) in {("demo", "text"), ("channel", "act")}:
                raise CompileError("OUT_OF_SUPPORT")
            continue
        if row["source"] not in {"demo", "channel"}:
            raise CompileError("OUT_OF_SUPPORT")
        if kind == "navigate":
            predicate = review.navigation.get(index)
            if actions or predicate is None:
                raise CompileError("OUT_OF_SUPPORT")
            if not owner.predicate_supported(predicate):
                raise CompileError("OUT_OF_SUPPORT")
            navigation.add(index)
            preconditions.append(deepcopy(predicate))
            continue
        if kind not in _CLICK_ACTIONS:
            raise CompileError("OUT_OF_SUPPORT")
        click = review.clicks.get(index)
        if not isinstance(click, ReviewedClick):
            raise CompileError("SCHEMA_GAP")
        if click.kind != kind or (kind == "click" and click.button == 3):
            raise CompileError("OUT_OF_SUPPORT")
        clicks.add(index)
        actions.append((row["mono_ns"], click))
    if not actions or set(review.clicks) != clicks or set(review.navigation) != navigation:
        raise CompileError("OUT_OF_SUPPORT")
    return actions, preconditions


def _workflow_step(seq, click, recorded_gap_ms):
    return {
        "schema": "mw.step.v1", "seq": seq, "protected": True,
        "action": {
            "kind": click.kind, "path": "coordinates", "button": click.button,
            "text_slot": None, "key_name": None, "dx_px": None, "dy_px": None,
            "destination": None, "delivery_budget_ms": 10_000,
        },
        "target": deepcopy(click.target), "preconditions": [], "capture_before": [],
        "fallback_targets": [], "when": None, "already_satisfied": None, "repeat": None,
        "locate_poll_ms": 100, "locate_wait_ms": 5_000,
        "verify": {
            "mode": "state_signal", "predicate": deepcopy(click.postcondition),
            "busy_predicate": None, "fallback_verifiers": [], "consecutive_polls": 1,
            "poll_interval_ms": 100, "deadline_ms": 5_000, "change_box_px": None,
            "postcondition_threshold_bps": None, "recorded_gap_ms": recorded_gap_ms,
            "wait_multiplier_num": 1, "wait_multiplier_den": 1,
            "ram_evidence_ref": None, "mask_boxes_px": [],
        },
    }


def _draft(steps, preconditions, site, review, provenance):
    return {
        "schema": "mw.workflow.v1", "id": review.workflow_id, "site_id": site["id"],
        "status": "draft", "admission_basis": "operator_demo", "baseline_id": None,
        "evaluation_profile": deepcopy(review.evaluation_profile),
        "fallback": {"kind": "ask_operator", "workflow_id": None},
        "engine_compat": [deepcopy(engine) for engine in site["engine_compat"]
                          if engine["engine"] == "wender-agent"],
        "execution_policy": deepcopy(site["execution_policy"]),
        "trigger_sig": {"task_kind": review.workflow_id, "keywords": [],
                        "select_when": preconditions, "reject_when": []},
        "preconditions": preconditions, "text_slots": [], "steps": steps,
        "segmentation_params": {"hi": 1, "lo": 0, "quiet": 1, "min_len": 1},
        "antipattern_guards": [], "script_ref": None, "script_sha256": None,
        "oracle": {"postconditions": [steps[-1]["verify"]["predicate"]],
                   "planted_mutant_ids": [], "mutants_killed": 0, "mutants_total": 0,
                   "receipt_refs": []},
        "provenance": provenance,
        "stats": dict.fromkeys(_validator("workflow").schema["$defs"]["stats"]["properties"]),
    }


def _validate_draft(workflow, owner):
    # These checks also guard future producers of draft steps within the compiler.
    if workflow["status"] != "draft" or not workflow["steps"]:
        raise CompileError("OUT_OF_SUPPORT")
    for step in workflow["steps"]:
        if step["action"]["kind"] not in _CLICK_ACTIONS or step["target"] is None:
            raise CompileError("OUT_OF_SUPPORT")
        _validate_memoryweb("step", step)
        target = step["target"]
        if not owner.target_supported(target):
            raise CompileError("OUT_OF_SUPPORT")
        predicate = target["frame_predicate"]
        if (not owner.predicate_supported(predicate) or
                target["anchor_ref"] != "ax:" + predicate["bindings"][0]["value"]):
            raise CompileError("OUT_OF_SUPPORT")
    _validate_memoryweb("workflow", workflow)
    for engine in workflow["engine_compat"]:
        if owner.capability_check(engine, None).rc != 0:
            raise CompileError("OUT_OF_SUPPORT")
    if owner.supports(workflow).result is not owner.Truth.TRUE:
        raise CompileError("OUT_OF_SUPPORT")


def compile_recording(timeline, review, owner, *, context=None):
    """Return canonical documents after checking them against the real replay owner.

    The caller supplies a ReplayOwner for the admitted site. Compilation uses
    its static support check and never calls the owner's execution backend.
    """
    events, timeline_sha256 = _decode_timeline(timeline)
    if not isinstance(review, RecordingReview) or review.timeline_sha256 != timeline_sha256:
        raise CompileError("SCHEMA_GAP")
    if context is not None and events:
        for field in ("capsule_id", "incarnation"):
            if field in context and context[field] != events[0][field]:
                raise CompileError("NO_LEASE")
    if not all(isinstance(value, dict) for value in
               (review.clicks, review.navigation, review.evaluation_profile, review.surface)):
        raise CompileError("OUT_OF_SUPPORT")
    actions, preconditions = _ordered_actions(events, review, owner)
    if (review.evaluation_profile.get("kind") != "initial_task_v1" or
            review.evaluation_profile.get("baseline_workflow_sha256") is not None):
        raise CompileError("OUT_OF_SUPPORT")
    try:
        if date.fromisoformat(review.recorded_date).isoformat() != review.recorded_date:
            raise ValueError
        explanation = review.explanation
        if not isinstance(explanation, bytes) or not explanation or len(explanation) > 16_384:
            raise ValueError
        explanation.decode("utf-8")
    except (TypeError, ValueError):
        raise CompileError("OUT_OF_SUPPORT") from None

    steps, demo_rows = [], []
    end = events[-1]["mono_ns"]
    previous = events[0]["mono_ns"]
    for seq, (time, click) in enumerate(actions, 1):
        next_time = actions[seq][0] if seq < len(actions) else end
        gap_after = (next_time - time) // 1_000_000
        steps.append(_workflow_step(seq, click, gap_after))
        demo_rows.append({
            "seq": seq, "kind": click.kind, "button": click.button, "recorded_point_px": None,
            "modifiers": [], "key_name": None, "text_len": None, "mark": None,
            "gap_before_ms": (time - previous) // 1_000_000, "gap_after_ms": gap_after,
            "expected_change_box_px": None,
        })
        previous = time

    site = deepcopy(owner.corpus.site)
    _validate_memoryweb("site", site)
    if review.surface.get("ax_enabled") is not True:
        raise CompileError("OUT_OF_SUPPORT")
    explanation_sha256 = _digest(explanation)
    provenance = {
        "source": "human", "tier": 1, "recorded": review.recorded_date,
        "source_ref": f"demos/{review.demo_id}/EXPLANATION.md",
        "quote_sha256": explanation_sha256, "derived_from_sha256": timeline_sha256,
        "run_id": None, "receipt_sha256": None,
    }
    demo_steps = {"schema": "mw.demo_steps.v1", "demo_id": review.demo_id,
                  "site_id": site["id"], "steps": demo_rows}
    demo_steps_raw = _encode(demo_steps)
    _validate_memoryweb("demo-steps", demo_steps)
    demo = {
        "schema": "mw.demo.v1", "demo_id": review.demo_id, "site_id": site["id"],
        "surface": deepcopy(review.surface), "recorded_date": review.recorded_date,
        "steps_file": "steps.json", "steps_sha256": _digest(demo_steps_raw),
        "explanation": {"file": "EXPLANATION.md", "sha256": explanation_sha256, "quote_tier": 1,
                        "redaction": "reviewed_original", "source_explanation_sha256": None},
        "events_summary": {"total_events": len(events), "segments_count": len(steps),
                           "duration_ms": (end - events[0]["mono_ns"]) // 1_000_000, "text_slots": []},
        "wire_normalization": {"filtered_noise_keys": [], "frame_storage": "ram_only",
                               "sparse_rows_normalized": True},
        "provenance": provenance,
    }
    draft = _draft(steps, preconditions, site, review, provenance)
    workflow_raw = _encode(draft)
    _validate_draft(draft, owner)
    _validate_memoryweb("demo", demo)
    return Compilation(_encode(demo, 32_768), demo_steps_raw, workflow_raw, explanation)


def vp_record_compile(resolve_recording: RecordingResolver, store):
    """Supply the C1 handler for the admitted vp_record_compile request.

    Only an executed request adds a U2c revision. The frozen legacy result is
    empty, so the reply points to the stored workflow through evidence[].
    """
    def run(request):
        context = request["context"]
        if not ("session_id" in context or {"capsule_id", "incarnation"} <= context.keys()):
            return Outcome(False, "NO_LEASE")
        try:
            binding = resolve_recording(dict(context))
            if binding is None:
                return Outcome(False, "HOLD", not_verified=("recording_selection",))
            timeline, review, owner = binding
            output = compile_recording(timeline, review, owner, context=context)
            if request["dry_run"]:
                return Outcome(True, "OK", not_verified=("candidate_publication", "workflow_replay"))
            saved = store.save_workflow(output.workflow)
        except (CompileError, StoreError) as error:
            return Outcome(False, error.code)
        workflow = json.loads(output.workflow)
        digest = saved["workflow_sha256"]
        path = f"sites/{workflow['site_id']}/revisions/{digest}/{workflow['id']}.json"
        return Outcome(True, "OK", evidence=({"path": path, "sha256": digest},),
                       not_verified=("workflow_replay",))
    return Handler(execute=run, preview=run)
