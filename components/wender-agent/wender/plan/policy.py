# @module {"id":"wa.plan.policy","layer":2,"owner":"LEARN","deps":["wa.learn.cascade","wa.perceive.ax","wa.state.schema"],"book":"HB-WA-s4-learn"}
"""Deterministic structural planner policy over observation/v0.2 and vqa.element-table/v1.

[TARGET, unit decision] Pure RAM/pipe planning policy with zero browser, desktop,
network, or disk frame I/O. Reuses wender.learn.cascade.intent_to_action for
structural tier-0 singleton selection with deterministic score_ppm fallback, and
emits a validated wender.state/v1 witness via wender.state.schema.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re

from wender.learn.cascade import intent_to_action
from wender.perceive.ax import M_ACT
from wender.state.schema import (
    SCHEMA_ID as STATE_SCHEMA_ID,
    StateError,
    dumps as dump_state_json,
    validate_state as validate_wender_state,
)

# @anchor:wa.plan.policy.DEFAULT_TICK_WINDOW
DEFAULT_TICK_WINDOW = 5
# @anchor:wa.plan.policy.DEFAULT_HOLDINGS_PROPOSAL_ID
DEFAULT_HOLDINGS_PROPOSAL_ID = "holdings-proposal-1"
# @anchor:wa.plan.policy.DEFAULT_HOLDINGS_MAX_MS
DEFAULT_HOLDINGS_MAX_MS = 5000
# @anchor:wa.plan.policy.ELEMENT_TABLE_SCHEMA
ELEMENT_TABLE_SCHEMA = "vqa.element-table/v1"

_MAX_DEPTH = 32
_MAX_DIMENSION = 16384
_MAX_SURFACE_ID = 128
_MAX_ROLE = 128
_MAX_SUMMARY = 65536
_MAX_SAFE_INT = (1 << 53) - 1
_RGB24 = "rgb24"
_HOLDINGS_SOURCE = "holdings_lane"

_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
_HEX16_RE = re.compile(r"[0-9a-f]{16}\Z")
_SURFACE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
_RAMWS_RE = re.compile(r"/dev/shm(/[A-Za-z0-9_][A-Za-z0-9._-]{0,127}){1,8}\Z")
_STOP_RE = re.compile(r"^\s*(?:Stop|Detener)\b", re.IGNORECASE)
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")

_FRAME_KEYS = frozenset({
    "schema", "surface_id", "generation", "tick", "ts_ns", "width",
    "height", "format", "ramws_path", "sha256", "capture_us",
})
_OBSERVATION_REQUIRED = frozenset({"schema", "frame_ref"})
_OBSERVATION_KEYS = frozenset({"schema", "frame_ref", "vision", "ground_truth"})
_VISION_KEYS = frozenset({"summary", "changed_regions", "elements"})
_VISION_ELEMENT_KEYS = frozenset({"bbox", "role", "name", "confidence"})
_GROUND_TRUTH_KEYS = frozenset({"source", "state_sha256", "tick"})
_TABLE_KEYS = frozenset({"schema", "frame_sha256", "width", "height", "elements"})
_ROW_KEYS = frozenset({"bbox", "role", "name", "score_ppm"})
_BBOX_KEYS = frozenset({"x", "y", "w", "h"})
_RESPONSE_KEYS = frozenset({"action", "expectation", "state", "target_node"})
_PROPOSAL_KEYS = frozenset({
    "schema", "proposal_id", "surface_id", "based_on_frame",
    "action", "expectation", "budget",
})
_BASED_ON_FRAME_KEYS = frozenset({"sha256", "generation"})
_BUDGET_KEYS = frozenset({"max_ms", "max_retries"})
_CLICK_BUTTONS = frozenset({"left", "middle", "right"})
_HOLDINGS_INPUT_TYPES = frozenset({"click", "key", "type", "scroll"})

# @anchor:wa.plan.policy.EXIT_CODES
EXIT_CODES: dict[str, int] = {
    "bad_shape": 1,
    "empty_goal": 2,
    "frame_mismatch": 3,
    "out_of_bounds": 4,
    "no_target": 5,
    "generation_control": 6,
    "alignment": 7,
    "policy": 8,
}


# @anchor:wa.plan.policy.PlannerError
class PlannerError(ValueError):
    """Typed planner refusal carrying a machine-readable code and exit status."""

    # @anchor:wa.plan.policy.PlannerError.__init__
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        self.exit_code = EXIT_CODES.get(code, 1)
        super().__init__(detail)


def _fail(code: str, detail: str) -> None:
    raise PlannerError(code, detail)


def _check_clean_str(value: object, *, what: str, min_len: int = 0, max_len: int = _MAX_SUMMARY) -> str:
    if not isinstance(value, str) or "\x00" in value or _SURROGATE_RE.search(value):
        _fail("bad_shape", f"{what} must be a valid UTF-8 string")
    if not (min_len <= len(value) <= max_len):
        _fail("bad_shape", f"{what} length out of bounds")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError:
        _fail("bad_shape", f"{what} contains a surrogate")
    if len(raw) > _MAX_SUMMARY:
        _fail("bad_shape", f"{what} UTF-8 bytes exceed maximum")
    return value


def _check_int(value: object, *, minimum: int = 0, maximum: int = _MAX_SAFE_INT, what: str) -> int:
    if type(value) is not int:
        _fail("bad_shape", f"{what} must be an integer")
    if value < minimum or value > maximum:
        _fail("bad_shape", f"{what} out of range")
    return value


def _reject_non_canonical(value: object, depth: int = 0) -> None:
    stack: list[tuple[object, int]] = [(value, depth)]
    while stack:
        current, cur_depth = stack.pop()
        if cur_depth > _MAX_DEPTH:
            _fail("bad_shape", "nesting exceeds limit")
        if current is None or type(current) in (bool, int):
            continue
        if type(current) is str:
            if "\x00" in current or _SURROGATE_RE.search(current):
                _fail("bad_shape", "string contains invalid code point")
            continue
        if isinstance(current, float):
            _fail("bad_shape", "canonical payload carries a float")
        if isinstance(current, dict):
            for key, item in current.items():
                if type(key) is not str or "\x00" in key or _SURROGATE_RE.search(key):
                    _fail("bad_shape", "object key is not a valid string")
                stack.append((item, cur_depth + 1))
            continue
        if isinstance(current, list):
            for item in current:
                stack.append((item, cur_depth + 1))
            continue
        _fail("bad_shape", "payload carries a non-JSON value")


# @anchor:wa.plan.policy.canonical_json
def canonical_json(value: object) -> str:
    """Serialize value to canonical compact UTF-8 JSON without floats."""
    _reject_non_canonical(value, 0)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


# @anchor:wa.plan.policy.is_generation_control
def is_generation_control(name: object) -> bool:
    """Return True when an element label names a generation-control button."""
    return isinstance(name, str) and _STOP_RE.search(name) is not None


def _check_bbox(box: object, *, width: int, height: int, what: str, oob_code: str = "out_of_bounds") -> dict:
    if not isinstance(box, dict) or set(box) != _BBOX_KEYS:
        _fail("bad_shape", f"{what} keys invalid")
    if not all(type(box[k]) is int for k in ("x", "y", "w", "h")):
        _fail("bad_shape", f"{what} coordinates must be integers")
    bx, by, bw, bh = box["x"], box["y"], box["w"], box["h"]
    if bw < 1 or bh < 1 or bw > _MAX_DIMENSION or bh > _MAX_DIMENSION:
        _fail("bad_shape", f"{what} dimensions must be positive")
    if bx < 0 or by < 0 or bx + bw > width or by + bh > height:
        _fail(oob_code, f"{what} escapes frame bounds")
    return box


def _validate_frame_ref(frame: object, *, contract_version: str) -> dict:
    if not isinstance(frame, dict) or set(frame) != _FRAME_KEYS:
        _fail("bad_shape", "frame_ref keys invalid")
    _reject_non_canonical(frame, 0)
    if frame["schema"] != f"frame_ref/{contract_version}":
        _fail("bad_shape", "frame_ref schema version mismatch")
    surface_id = _check_clean_str(frame["surface_id"], what="frame_ref.surface_id", min_len=1, max_len=_MAX_SURFACE_ID)
    if _SURFACE_ID_RE.fullmatch(surface_id) is None:
        _fail("bad_shape", "frame_ref.surface_id malformed")
    _check_int(frame["generation"], minimum=1, what="frame_ref.generation")
    _check_int(frame["tick"], minimum=0, what="frame_ref.tick")
    _check_int(frame["ts_ns"], minimum=0, what="frame_ref.ts_ns")
    width = _check_int(frame["width"], minimum=1, maximum=_MAX_DIMENSION, what="frame_ref.width")
    height = _check_int(frame["height"], minimum=1, maximum=_MAX_DIMENSION, what="frame_ref.height")
    if frame["format"] != _RGB24:
        _fail("bad_shape", "frame_ref.format must be rgb24")
    ramws_path = _check_clean_str(frame["ramws_path"], what="frame_ref.ramws_path", min_len=10, max_len=255)
    if _RAMWS_RE.match(ramws_path) is None:
        _fail("bad_shape", "frame_ref.ramws_path must stay in /dev/shm")
    sha256 = _check_clean_str(frame["sha256"], what="frame_ref.sha256", min_len=64, max_len=64)
    if _HEX64_RE.fullmatch(sha256) is None:
        _fail("bad_shape", "frame_ref.sha256 must be 64 lowercase hex")
    _check_int(frame["capture_us"], minimum=0, what="frame_ref.capture_us")
    return frame


def _validate_vision(vision: object, *, width: int, height: int) -> dict:
    if not isinstance(vision, dict) or set(vision) != _VISION_KEYS:
        _fail("bad_shape", "vision keys invalid")
    _check_clean_str(vision["summary"], what="vision.summary", min_len=0, max_len=_MAX_SUMMARY)
    regions = vision["changed_regions"]
    if not isinstance(regions, list):
        _fail("bad_shape", "vision.changed_regions must be a list")
    for idx, region in enumerate(regions):
        _check_bbox(region, width=width, height=height, what=f"vision.changed_regions[{idx}]")
    elements = vision["elements"]
    if not isinstance(elements, list):
        _fail("bad_shape", "vision.elements must be a list")
    for idx, elem in enumerate(elements):
        if not isinstance(elem, dict) or set(elem) != _VISION_ELEMENT_KEYS:
            _fail("bad_shape", f"vision.elements[{idx}] keys invalid")
        _check_bbox(elem["bbox"], width=width, height=height, what=f"vision.elements[{idx}].bbox")
        _check_clean_str(elem["role"], what=f"vision.elements[{idx}].role", min_len=1, max_len=_MAX_ROLE)
        _check_clean_str(elem["name"], what=f"vision.elements[{idx}].name", min_len=0, max_len=_MAX_SUMMARY)
        conf = elem["confidence"]
        if type(conf) not in (int, float) or not math.isfinite(conf) or conf < 0 or conf > 1:
            _fail("bad_shape", f"vision.elements[{idx}].confidence must be in [0.0, 1.0]")
    return vision


def _validate_ground_truth(truth: object) -> dict:
    if not isinstance(truth, dict) or set(truth) != _GROUND_TRUTH_KEYS:
        _fail("bad_shape", "ground_truth keys invalid")
    _reject_non_canonical(truth, 0)
    if truth["source"] != _HOLDINGS_SOURCE:
        _fail("bad_shape", "ground_truth.source must be holdings_lane")
    sha256 = _check_clean_str(truth["state_sha256"], what="ground_truth.state_sha256", min_len=64, max_len=64)
    if _HEX64_RE.fullmatch(sha256) is None:
        _fail("bad_shape", "ground_truth.state_sha256 must be 64 lowercase hex")
    _check_int(truth["tick"], minimum=0, what="ground_truth.tick")
    return truth


def _validate_observation(observation: object) -> dict:
    if not isinstance(observation, dict):
        _fail("bad_shape", "observation must be an object")
    keys = set(observation)
    if not (_OBSERVATION_REQUIRED <= keys <= _OBSERVATION_KEYS):
        _fail("bad_shape", "observation keys invalid")
    schema = observation.get("schema")
    if schema == "observation/v0.1":
        version = "v0.1"
    elif schema == "observation/v0.2":
        version = "v0.2"
    else:
        _fail("bad_shape", "observation schema must be observation/v0.2 or observation/v0.1")
    frame = _validate_frame_ref(observation.get("frame_ref"), contract_version=version)
    if "vision" in observation:
        _validate_vision(observation["vision"], width=frame["width"], height=frame["height"])
    if "ground_truth" in observation:
        _validate_ground_truth(observation["ground_truth"])
    return frame


def _validate_element_table(element_table: object, *, frame: dict) -> dict:
    _reject_non_canonical(element_table, 0)
    if not isinstance(element_table, dict) or set(element_table) != _TABLE_KEYS:
        _fail("bad_shape", "element table keys invalid")
    if element_table.get("schema") != ELEMENT_TABLE_SCHEMA:
        _fail("bad_shape", "element table schema invalid")
    if not isinstance(element_table.get("elements"), list):
        _fail("bad_shape", "element table elements must be a list")

    table_sha = element_table.get("frame_sha256")
    table_w = element_table.get("width")
    table_h = element_table.get("height")
    if (
        not isinstance(table_sha, str)
        or _HEX64_RE.fullmatch(table_sha) is None
        or type(table_w) is not int
        or type(table_h) is not int
        or table_w < 1
        or table_h < 1
    ):
        _fail("bad_shape", "element table frame metadata malformed")

    if table_sha != frame["sha256"]:
        _fail("frame_mismatch", "element table frame_sha256 mismatch")
    if table_w != frame["width"] or table_h != frame["height"]:
        _fail("frame_mismatch", "element table dimensions mismatch")

    for idx, row in enumerate(element_table["elements"]):
        if not isinstance(row, dict) or set(row) != _ROW_KEYS:
            _fail("bad_shape", f"element row[{idx}] keys invalid")
        _check_clean_str(row.get("role"), what=f"element row[{idx}].role", min_len=1, max_len=_MAX_ROLE)
        _check_clean_str(row.get("name"), what=f"element row[{idx}].name", min_len=0, max_len=_MAX_SUMMARY)
        score = row.get("score_ppm")
        if type(score) is not int or not 0 <= score <= 1_000_000:
            _fail("bad_shape", f"element row[{idx}].score_ppm invalid")
        _check_bbox(row.get("bbox"), width=frame["width"], height=frame["height"], what=f"element row[{idx}].bbox")

    return element_table


# @anchor:wa.plan.policy.project_vision_element_table
def project_vision_element_table(observation: dict) -> dict:
    """Project observation.vision.elements into a frame-bound vqa.element-table/v1 dict."""
    frame = _validate_observation(observation)
    vision = observation.get("vision")
    if not isinstance(vision, dict):
        _fail("bad_shape", "observation.vision required to project element table")
    rows: list[dict] = []
    for elem in vision["elements"]:
        box = elem["bbox"]
        score_ppm = int(round(float(elem["confidence"]) * 1_000_000))
        rows.append({
            "bbox": {"x": box["x"], "y": box["y"], "w": box["w"], "h": box["h"]},
            "role": elem["role"],
            "name": elem["name"],
            "score_ppm": score_ppm,
        })
    return {
        "schema": ELEMENT_TABLE_SCHEMA,
        "frame_sha256": frame["sha256"],
        "width": frame["width"],
        "height": frame["height"],
        "elements": rows,
    }


# @anchor:wa.plan.policy.build_state_witness
def build_state_witness(frame: dict, element_table: dict) -> tuple[dict, list[dict]]:
    """Build and validate a deterministic wender.state/v1 witness from element_table."""
    nodes: list[dict] = []
    actionable: list[str] = []
    for idx, row in enumerate(element_table["elements"]):
        seed = f"{frame['sha256']}:{idx}:{row['role']}:{row['name']}".encode("utf-8")
        node_id = hashlib.sha256(seed).hexdigest()[:16]
        is_stop = is_generation_control(row["name"])
        box = row["bbox"]
        node = {
            "id": node_id,
            "role": row["role"],
            "name": row["name"],
            "depth": 0,
            "parent": None,
            "ordinal": idx,
            "states": {},
            "actionable": not is_stop,
            "box": [box["x"], box["y"], box["w"], box["h"]],
        }
        nodes.append(node)
        if not is_stop:
            actionable.append(node_id)

    ax_sha256 = hashlib.sha256(dump_state_json(nodes).encode("utf-8")).hexdigest()
    stable_nodes = [
        {k: (None if k == "box" else v) for k, v in node.items()}
        for node in nodes
    ]
    ax_stable_sha256 = hashlib.sha256(dump_state_json(stable_nodes).encode("utf-8")).hexdigest()
    state = {
        "schema": STATE_SCHEMA_ID,
        "source": {"kind": "vision", "url": None, "title": None},
        "prune": {"name": "vqa.element-table", "version": 1},
        "ax_sha256": ax_sha256,
        "ax_stable_sha256": ax_stable_sha256,
        "viewport": {"w": frame["width"], "h": frame["height"]},
        "nodes": nodes,
        "actionable": actionable,
        "counts": {
            "raw": len(nodes),
            "nodes": len(nodes),
            "actionable": len(actionable),
        },
    }
    try:
        errors = validate_wender_state(state)
    except StateError as exc:
        _fail("bad_shape", str(exc))
    if errors:
        _fail("bad_shape", "; ".join(errors))
    return state, nodes


# @anchor:wa.plan.policy.select_target_index
def select_target_index(elements: list[dict], goal: str) -> int:
    """Select an actionable target index via cascade tier-0 singleton match and score_ppm tiebreak."""
    if not elements:
        _fail("no_target", "element table contains no elements")
    candidates: list[tuple[int, dict]] = [
        (idx, row)
        for idx, row in enumerate(elements)
        if not is_generation_control(row["name"])
    ]
    if not candidates:
        _fail("generation_control", "refusing generation-control target")

    needle = goal.strip().lower()
    cascade_nodes = [
        {
            "id": f"e{idx}",
            "role": row["role"].strip().lower(),
            "name": row["name"],
            "canon_name": row["name"].strip().lower(),
            "state_word": M_ACT,
            "match_level": "L0_EXACT",
        }
        for idx, row in candidates
    ]
    cascade_table = {
        "known_screen": True,
        "blocked_control_present": 0,
        "elements": cascade_nodes,
    }
    workflow_step = {"intent": needle, "action": {"control": "goal_target"}}
    for control_spec in (
        {"canon_name": needle},
        {"role": needle},
        {"pattern": re.escape(needle)},
    ):
        tier0 = intent_to_action(
            needle,
            workflow_step,
            {"goal_target": control_spec},
            cascade_table,
        )
        if tier0 is not None and tier0.get("tier") == 0:
            target_id = tier0.get("target_id", "")
            if isinstance(target_id, str) and target_id.startswith("e"):
                return int(target_id[1:])

    exact = [
        (idx, row)
        for idx, row in candidates
        if row["name"].strip().lower() == needle or row["role"].strip().lower() == needle
    ]
    if exact:
        pool = exact
    else:
        partial = [
            (idx, row)
            for idx, row in candidates
            if (row["name"] and needle in row["name"].lower())
            or (row["role"] and needle in row["role"].lower())
        ]
        pool = partial if partial else candidates

    best_idx, _ = max(pool, key=lambda item: (item[1]["score_ppm"], -item[0]))
    return best_idx


def _check_action(action: object) -> None:
    if not isinstance(action, dict):
        _fail("bad_shape", "action must be an object")
    kind = action.get("type")
    if kind == "click":
        if set(action) != {"type", "x", "y", "button"}:
            _fail("bad_shape", "click action keys invalid")
        _check_int(action["x"], minimum=0, what="click.x")
        _check_int(action["y"], minimum=0, what="click.y")
        if action["button"] not in _CLICK_BUTTONS:
            _fail("bad_shape", "click.button invalid")
    elif kind == "key":
        if set(action) != {"type", "keys"} or not isinstance(action["keys"], list) or not (1 <= len(action["keys"]) <= 4096):
            _fail("bad_shape", "key action invalid")
        for key in action["keys"]:
            _check_clean_str(key, what="key name", min_len=1, max_len=_MAX_SUMMARY)
    elif kind == "type":
        if set(action) != {"type", "text"}:
            _fail("bad_shape", "type action keys invalid")
        _check_clean_str(action["text"], what="type.text", min_len=1, max_len=_MAX_SUMMARY)
    elif kind == "scroll":
        if set(action) != {"type", "dx", "dy"}:
            _fail("bad_shape", "scroll action keys invalid")
        _check_int(action["dx"], minimum=-_MAX_SAFE_INT, what="scroll.dx")
        _check_int(action["dy"], minimum=-_MAX_SAFE_INT, what="scroll.dy")
    elif kind == "wait":
        if set(action) != {"type", "ms"}:
            _fail("bad_shape", "wait action keys invalid")
        _check_int(action["ms"], minimum=1, maximum=3_600_000, what="wait.ms")
    else:
        _fail("bad_shape", "unsupported action type")


def _check_expectation(expectation: object) -> None:
    if not isinstance(expectation, dict):
        _fail("bad_shape", "expectation must be an object")
    kind = expectation.get("type")
    if kind == "changed_region":
        if set(expectation) != {"type", "bbox"}:
            _fail("bad_shape", "changed_region expectation keys invalid")
        box = expectation["bbox"]
        if not isinstance(box, dict) or set(box) != _BBOX_KEYS:
            _fail("bad_shape", "changed_region bbox keys invalid")
        _check_int(box["x"], minimum=0, what="bbox.x")
        _check_int(box["y"], minimum=0, what="bbox.y")
        _check_int(box["w"], minimum=1, maximum=_MAX_DIMENSION, what="bbox.w")
        _check_int(box["h"], minimum=1, maximum=_MAX_DIMENSION, what="bbox.h")
    elif kind == "state_sha256_change":
        if set(expectation) != {"type", "before", "min_tick", "max_tick"}:
            _fail("bad_shape", "state_sha256_change expectation keys invalid")
        before = _check_clean_str(expectation["before"], what="expectation.before", min_len=64, max_len=64)
        if _HEX64_RE.fullmatch(before) is None:
            _fail("bad_shape", "expectation.before must be 64 lowercase hex")
        min_tick = _check_int(expectation["min_tick"], minimum=0, what="expectation.min_tick")
        max_tick = _check_int(expectation["max_tick"], minimum=0, what="expectation.max_tick")
        if min_tick > max_tick:
            _fail("bad_shape", "expectation tick window is inverted")
    elif kind == "element_state":
        if set(expectation) != {"type", "role", "name", "state"}:
            _fail("bad_shape", "element_state expectation keys invalid")
        _check_clean_str(expectation["role"], what="expectation.role", min_len=1, max_len=_MAX_ROLE)
        _check_clean_str(expectation["name"], what="expectation.name", min_len=0, max_len=_MAX_SUMMARY)
        _check_clean_str(expectation["state"], what="expectation.state", min_len=1, max_len=_MAX_ROLE)
    elif kind == "text_present":
        if set(expectation) != {"type", "text"}:
            _fail("bad_shape", "text_present expectation keys invalid")
        _check_clean_str(expectation["text"], what="expectation.text", min_len=1, max_len=_MAX_SUMMARY)
    else:
        _fail("bad_shape", "unsupported expectation type")


def _bbox_midpoint(box: dict) -> tuple[int, int]:
    return box["x"] + box["w"] // 2, box["y"] + box["h"] // 2


def _select_vision_click(frame: dict, vision: dict) -> tuple[dict, dict | None]:
    elements = vision.get("elements")
    if isinstance(elements, list) and elements:
        candidates: list[tuple[int, dict]] = [
            (idx, elem)
            for idx, elem in enumerate(elements)
            if isinstance(elem, dict) and not is_generation_control(elem.get("name", ""))
        ]
        if not candidates:
            _fail("generation_control", "refusing generation-control target")
        _, chosen = max(
            candidates,
            key=lambda item: (float(item[1].get("confidence", 0.0)), -item[0]),
        )
        box = chosen["bbox"]
        bx, by, bw, bh = box["x"], box["y"], box["w"], box["h"]
        click_x, click_y = _bbox_midpoint(box)
        if not (0 <= click_x < frame["width"] and 0 <= click_y < frame["height"]):
            _fail("out_of_bounds", "computed click escapes frame bounds")
        return (
            {"type": "click", "x": click_x, "y": click_y, "button": "left"},
            {"x": bx, "y": by, "w": bw, "h": bh},
        )
    return (
        {"type": "click", "x": frame["width"] // 2, "y": frame["height"] // 2, "button": "left"},
        None,
    )


# @anchor:wa.plan.policy.plan_holdings_step
def plan_holdings_step(
    observation: dict,
    *,
    tick_window: int = DEFAULT_TICK_WINDOW,
    action: dict | None = None,
) -> tuple[dict, dict]:
    """Emit (action, expectation) for an observation; never lane_submit."""
    frame = _validate_observation(observation)
    if type(tick_window) is not int or tick_window < 1:
        _fail("bad_shape", "tick_window must be a positive integer")

    truth = observation.get("ground_truth")
    vision = observation.get("vision")
    if not isinstance(truth, dict) and not isinstance(vision, dict):
        _fail("bad_shape", "observation requires ground_truth or vision")

    chosen_bbox: dict | None = None
    if action is not None:
        _reject_non_canonical(action, 0)
        if not isinstance(action, dict):
            _fail("bad_shape", "action must be a dict")
        if action.get("type") == "lane_submit":
            _fail("policy", "lane_submit is forbidden for Holdings")
        _check_action(action)
        if action.get("type") not in _HOLDINGS_INPUT_TYPES:
            _fail("policy", "Holdings action must be an input action")
        if action["type"] == "click":
            if not (0 <= action["x"] < frame["width"] and 0 <= action["y"] < frame["height"]):
                _fail("out_of_bounds", "click coordinates outside frame bounds")
        chosen_action = copy.deepcopy(action)
    elif isinstance(vision, dict):
        if not isinstance(truth, dict) and not vision.get("elements"):
            _fail("no_target", "vision.elements contains no elements")
        chosen_action, chosen_bbox = _select_vision_click(frame, vision)
    else:
        chosen_action = {
            "type": "click",
            "x": frame["width"] // 2,
            "y": frame["height"] // 2,
            "button": "left",
        }

    if isinstance(truth, dict):
        base_tick = truth["tick"]
        expectation = {
            "type": "state_sha256_change",
            "before": truth["state_sha256"],
            "min_tick": base_tick + 1,
            "max_tick": base_tick + tick_window,
        }
    else:
        if chosen_bbox is None:
            chosen_bbox = {"x": 0, "y": 0, "w": frame["width"], "h": frame["height"]}
        expectation = {
            "type": "changed_region",
            "bbox": chosen_bbox,
        }

    _check_action(chosen_action)
    _check_expectation(expectation)
    return copy.deepcopy(chosen_action), copy.deepcopy(expectation)


# @anchor:wa.plan.policy.plan_holdings
def plan_holdings(
    observation: dict,
    *,
    proposal_id: str = DEFAULT_HOLDINGS_PROPOSAL_ID,
    tick_window: int = DEFAULT_TICK_WINDOW,
    max_ms: int = DEFAULT_HOLDINGS_MAX_MS,
    max_retries: int = 0,
    action: dict | None = None,
) -> dict:
    """Emit one action_proposal/v0.2 for an observation; never lane_submit."""
    frame = _validate_observation(observation)
    _check_clean_str(proposal_id, what="proposal_id", min_len=1, max_len=128)
    if type(max_ms) is not int or not 1 <= max_ms <= 3_600_000:
        _fail("bad_shape", "max_ms out of range")
    if type(max_retries) is not int or not 0 <= max_retries <= 10:
        _fail("bad_shape", "max_retries out of range")

    chosen_action, expectation = plan_holdings_step(
        observation,
        tick_window=tick_window,
        action=action,
    )
    proposal = {
        "schema": "action_proposal/v0.2",
        "proposal_id": proposal_id,
        "surface_id": frame["surface_id"],
        "based_on_frame": {
            "sha256": frame["sha256"],
            "generation": frame["generation"],
        },
        "action": chosen_action,
        "expectation": expectation,
        "budget": {
            "max_ms": max_ms,
            "max_retries": max_retries,
        },
    }
    recheck_holdings_proposal(proposal, observation)
    return copy.deepcopy(proposal)


# @anchor:wa.plan.policy.recheck_holdings_proposal
def recheck_holdings_proposal(proposal: object, observation: dict | None = None) -> dict:
    """Validate an action_proposal/v0.2 against its source observation."""
    if observation is None:
        _fail("bad_shape", "observation required for proposal recheck")
    frame = _validate_observation(observation)
    _reject_non_canonical(proposal, 0)
    if not isinstance(proposal, dict) or set(proposal) != _PROPOSAL_KEYS:
        _fail("bad_shape", "proposal keys invalid")
    if proposal.get("schema") != "action_proposal/v0.2":
        _fail("bad_shape", "proposal schema must be action_proposal/v0.2")
    _check_clean_str(proposal.get("proposal_id"), what="proposal_id", min_len=1, max_len=128)
    _check_clean_str(proposal.get("surface_id"), what="surface_id", min_len=1, max_len=128)

    based = proposal.get("based_on_frame")
    if not isinstance(based, dict) or set(based) != _BASED_ON_FRAME_KEYS:
        _fail("bad_shape", "based_on_frame keys invalid")
    sha = _check_clean_str(based.get("sha256"), what="based_on_frame.sha256", min_len=64, max_len=64)
    if _HEX64_RE.fullmatch(sha) is None:
        _fail("bad_shape", "based_on_frame.sha256 invalid")
    _check_int(based.get("generation"), minimum=1, what="based_on_frame.generation")

    budget = proposal.get("budget")
    if not isinstance(budget, dict) or set(budget) != _BUDGET_KEYS:
        _fail("bad_shape", "budget keys invalid")
    _check_int(budget.get("max_ms"), minimum=1, maximum=3_600_000, what="budget.max_ms")
    _check_int(budget.get("max_retries"), minimum=0, maximum=10, what="budget.max_retries")

    action = proposal.get("action")
    if isinstance(action, dict) and action.get("type") == "lane_submit":
        _fail("policy", "lane_submit is forbidden for Holdings")
    _check_action(action)
    expectation = proposal.get("expectation")
    _check_expectation(expectation)

    if proposal["surface_id"] != frame["surface_id"]:
        _fail("frame_mismatch", "proposal surface_id disagrees with observation frame")
    if based["sha256"] != frame["sha256"] or based["generation"] != frame["generation"]:
        _fail("frame_mismatch", "proposal based_on_frame disagrees with observation frame")

    if action["type"] not in _HOLDINGS_INPUT_TYPES:
        _fail("policy", "proposal action must be an input action")
    if action["type"] == "click":
        if not (0 <= action["x"] < frame["width"] and 0 <= action["y"] < frame["height"]):
            _fail("out_of_bounds", "click coordinates outside frame bounds")

    truth = observation.get("ground_truth")
    if isinstance(truth, dict):
        if expectation.get("type") != "state_sha256_change":
            _fail("bad_shape", "Holdings expectation must be state_sha256_change")
        if expectation["before"] != truth["state_sha256"]:
            _fail("frame_mismatch", "expectation before digest disagrees with ground_truth")
        if expectation["min_tick"] < truth["tick"] + 1:
            _fail("bad_shape", "expectation min_tick must advance past observed tick")
        if expectation["min_tick"] > expectation["max_tick"]:
            _fail("bad_shape", "expectation tick window is inverted")
    else:
        if expectation.get("type") != "changed_region":
            _fail("bad_shape", "expectation without ground_truth must be changed_region")
        _check_bbox(expectation["bbox"], width=frame["width"], height=frame["height"], what="expectation.bbox")

    return copy.deepcopy(proposal)


# @anchor:wa.plan.policy.plan
def plan(
    observation: dict,
    element_table: dict | None = None,
    goal: str | None = None,
    *,
    proposal_id: str = DEFAULT_HOLDINGS_PROPOSAL_ID,
    tick_window: int = DEFAULT_TICK_WINDOW,
    max_ms: int = DEFAULT_HOLDINGS_MAX_MS,
    max_retries: int = 0,
    action: dict | None = None,
) -> dict:
    """Produce a Holdings action_proposal/v0.2 or an element-table click plan."""
    if element_table is None and goal is None:
        return plan_holdings(
            observation,
            proposal_id=proposal_id,
            tick_window=tick_window,
            max_ms=max_ms,
            max_retries=max_retries,
            action=action,
        )

    if not isinstance(goal, str):
        _fail("empty_goal", "goal must be a non-empty string")
    _check_clean_str(goal, what="goal")
    cleaned_goal = goal.strip()
    if not cleaned_goal:
        _fail("empty_goal", "goal must not be empty or whitespace")

    frame = _validate_observation(observation)
    checked_table = _validate_element_table(element_table, frame=frame)
    chosen_idx = select_target_index(checked_table["elements"], cleaned_goal)
    chosen = checked_table["elements"][chosen_idx]
    box = chosen["bbox"]
    bx, by, bw, bh = box["x"], box["y"], box["w"], box["h"]
    click_x, click_y = _bbox_midpoint(box)
    if not (0 <= click_x < frame["width"] and 0 <= click_y < frame["height"]):
        _fail("out_of_bounds", "computed click escapes frame bounds")
    if not (bx <= click_x < bx + bw and by <= click_y < by + bh):
        _fail("alignment", "computed click escapes target bbox")

    state, nodes = build_state_witness(frame, checked_table)
    target_node = copy.deepcopy(nodes[chosen_idx])
    click_action = {
        "type": "click",
        "x": click_x,
        "y": click_y,
        "button": "left",
    }
    if "ground_truth" in observation:
        if type(tick_window) is not int or tick_window < 1:
            _fail("bad_shape", "tick_window must be a positive integer")
        truth = observation["ground_truth"]
        expectation = {
            "type": "state_sha256_change",
            "before": truth["state_sha256"],
            "min_tick": truth["tick"] + 1,
            "max_tick": truth["tick"] + tick_window,
        }
    else:
        expectation = {
            "type": "changed_region",
            "bbox": {"x": bx, "y": by, "w": bw, "h": bh},
        }
    response = {
        "action": click_action,
        "expectation": expectation,
        "state": state,
        "target_node": target_node,
    }
    recheck_plan(response, observation=observation, element_table=element_table)
    return copy.deepcopy(response)


def _check_target_custody(target_node: object, *, latest: list[dict], frame: dict) -> None:
    if not isinstance(target_node, dict) or not isinstance(latest, list):
        _fail("bad_shape", "target custody invalid")
    matches = [row for row in latest if row.get("id") and row.get("id") == target_node.get("id")]
    stable = lambda row: {k: v for k, v in row.items() if k != "value"}
    if len(matches) != 1 or stable(matches[0]) != stable(target_node):
        _fail("bad_shape", "target custody mismatch")
    if is_generation_control(target_node.get("name", "")):
        _fail("generation_control", "refusing generation-control target")
    box = target_node.get("box")
    if not isinstance(box, list) or len(box) != 4 or any(type(v) is not int for v in box):
        _fail("out_of_bounds", "target_node box invalid")
    tx, ty, tw, th = box
    if tx < 0 or ty < 0 or tw < 1 or th < 1 or tx + tw > frame["width"] or ty + th > frame["height"]:
        _fail("out_of_bounds", "target_node box escapes frame bounds")


# @anchor:wa.plan.policy.recheck_plan
def recheck_plan(
    response: object,
    observation: dict | None = None,
    element_table: dict | None = None,
) -> dict:
    """Caller-side re-validation of a planner response against frame and element_table."""
    if (
        element_table is None
        and isinstance(response, dict)
        and response.get("schema") == "action_proposal/v0.2"
    ):
        return recheck_holdings_proposal(response, observation)

    if observation is None or element_table is None:
        _fail("bad_shape", "observation and element_table required for recheck")
    frame = _validate_observation(observation)
    checked_table = _validate_element_table(element_table, frame=frame)
    _reject_non_canonical(response, 0)

    if not isinstance(response, dict) or set(response) != _RESPONSE_KEYS:
        _fail("bad_shape", "planner response keys invalid")

    action = response["action"]
    expectation = response["expectation"]
    state = response["state"]
    target_node = response["target_node"]

    if isinstance(action, dict) and action.get("type") == "lane_submit":
        _fail("policy", "lane_submit is forbidden")

    _check_action(action)
    _check_expectation(expectation)

    if action.get("type") != "click" or action.get("button") != "left":
        _fail("bad_shape", "planner action must be a left click")

    click_x, click_y = action["x"], action["y"]
    if not (0 <= click_x < frame["width"] and 0 <= click_y < frame["height"]):
        _fail("out_of_bounds", "click coordinates outside frame bounds")

    if not isinstance(state, dict):
        _fail("bad_shape", "state must be an object")
    try:
        errors = validate_wender_state(state)
    except StateError as exc:
        _fail("bad_shape", str(exc))
    if errors:
        _fail("bad_shape", "; ".join(errors))

    if state.get("viewport") != {"w": frame["width"], "h": frame["height"]}:
        _fail("frame_mismatch", "state viewport disagrees with frame")

    _check_target_custody(target_node, latest=state["nodes"], frame=frame)

    if target_node.get("actionable") is not True or target_node.get("id") not in state["actionable"]:
        _fail("bad_shape", "target_node is not actionable")

    tx, ty, tw, th = target_node["box"]
    if not (tx <= click_x < tx + tw and ty <= click_y < ty + th):
        _fail("alignment", "click falls outside target_node box")

    target_bbox = {"x": tx, "y": ty, "w": tw, "h": th}
    if "ground_truth" in observation and expectation.get("type") == "state_sha256_change":
        truth = observation["ground_truth"]
        if expectation["before"] != truth["state_sha256"]:
            _fail("frame_mismatch", "expectation before digest disagrees with ground_truth")
        if expectation["min_tick"] < truth["tick"] + 1:
            _fail("bad_shape", "expectation min_tick must advance past observed tick")
    else:
        if expectation.get("type") != "changed_region":
            _fail("bad_shape", "planner expectation must be changed_region")
        exp_bbox = expectation["bbox"]
        _check_bbox(exp_bbox, width=frame["width"], height=frame["height"], what="expectation.bbox")
        if exp_bbox != target_bbox:
            _fail("alignment", "expectation bbox disagrees with target_node box")

    ordinal = target_node.get("ordinal")
    elements = checked_table["elements"]
    if (
        type(ordinal) is not int
        or not 0 <= ordinal < len(elements)
        or elements[ordinal]["bbox"] != target_bbox
        or elements[ordinal]["role"] != target_node.get("role")
        or elements[ordinal]["name"] != target_node.get("name")
    ):
        _fail("bad_shape", "target_node does not match element_table row")

    expected_state, _ = build_state_witness(frame, checked_table)
    if state != expected_state:
        _fail("bad_shape", "state witness disagrees with element_table")

    return copy.deepcopy(response)


# @anchor:wa.plan.policy.validate_plan_response
validate_plan_response = recheck_plan
