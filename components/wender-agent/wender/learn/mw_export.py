# @module {"id":"wa.learn.mw_export","layer":2,"owner":"LEARN","deps":["wa.remember.skills","wa.remember.program_map","wa.tools.memoryweb_gate"],"book":"HB-WA-s4-learn"}
"""Render an admitted Wender C-11 skill into MemoryWeb workflow, explanation, demo_steps and run_bundle drafts.

[TARGET, unit decision] B3 wender skill renderer contract:
- Admission gate: every export entry point validates its input through
  `wender.remember.skills.admit` and the C-11 promotion gate (`support_n >= 3`,
  `min_err >= 0`, `evidence.reproduced is True`, nonblank `evidence.verified_by`,
  positive exact `evidence.version`). Unverified, non-C-11, or malformed skills
  raise `MwExportError` (a `SkillRejected` subclass) before any draft is built.
- Vocabulary mapping (`wender/learn/cu_action.schema.json:19-23`):
  `click` -> demo_steps `click`, workflow `click`
  `key`   -> demo_steps `key`,   workflow `key`
  `type`  -> demo_steps `paste`, workflow `paste` (with `slot-<seq>`, integer `text_len` only, never raw text)
  `wait`  -> demo_steps `wait`,  workflow `wait_signal`
  `done`  -> demo_steps `wait`,  workflow `wait_signal` (0 ms terminal marker)
- Formats mirrored from memoryweb (`mw.workflow.v1`, `mw.step.v1`, `mw.demo_steps.v1`,
  `mw.run_bundle.v1`, and six-heading `EXPLANATION.md`) without importing memoryweb code.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from tools import memoryweb_gate as gate
from wender.remember import skills
from wender.remember.program_map import canonical_json
from wender.remember.skills import SkillRejected

ROOT = Path(__file__).resolve().parents[2]
MW_V1_SCHEMA_PATH = ROOT / "docs" / "spec" / "mw.v1.schema.json"
CU_ACTION_SCHEMA_PATH = ROOT / "wender" / "learn" / "cu_action.schema.json"

SCHEMA_PINS = {
    "cu_action.schema.json": "52948ac097dffa74cccdfabbd477d39d43b622bd60f2a02b501469f89f799203",
    "demo-steps.v1.json": "fc4f6bf7f86c21f415b4c4641d4ced823ae5a268d4f13753675ac6a542826187",
    "demo.v1.json": "0e7706fb8b17360818d7d8572c7937ab15b3969e4361e5cb3b314b61443ae124",
    "mw.run_bundle.v1.schema.json": "3d63d974afe79100d49ac2eabd0fa1cde95efc2594286d8dbbe4bbdabaf2303a",
    "mw.v1.schema.json": "b85fd5742f768e1a9cb5478ebe628fac7f7a9f7cf34e111c8fd1ab3dc6a95025",
    "step.v1.json": "2984037a4724e33d9db18736ffad5941e73f639b29c44926b7cc935c7bd1ad81",
    "workflow.v1.json": "de22c4c71c3aaf5d8412a33de92ca7eaa26692254b8f43782cf2e61df0eaf619",
}

CU_ACTION_VOCABULARY = ("click", "key", "type", "wait", "done")
DEMO_STEP_KINDS = ("click", "right_click", "double_click", "paste", "key", "scroll", "drag", "wait")
WORKFLOW_STEP_KINDS = ("click", "right_click", "double_click", "paste", "key", "scroll", "drag", "wait_signal")

CU_TO_DEMO_KIND = {
    "click": "click",
    "key": "key",
    "type": "paste",
    "wait": "wait",
    "done": "wait",
}

CU_TO_WORKFLOW_KIND = {
    "click": "click",
    "key": "key",
    "type": "paste",
    "wait": "wait_signal",
    "done": "wait_signal",
}

DEMO_TO_WORKFLOW_KIND = {
    "click": "click",
    "right_click": "right_click",
    "double_click": "double_click",
    "paste": "paste",
    "key": "key",
    "scroll": "scroll",
    "drag": "drag",
    "wait": "wait_signal",
}

EXPLANATION_HEADINGS = (
    "Intent",
    "Starting state",
    "Variable slots",
    "Ambiguous controls",
    "Expected result",
    "Exceptions",
)

_STOPWORDS = frozenset((
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "for", "from", "how", "in",
    "into", "is", "it", "of", "on", "or", "the", "then", "to", "up", "use", "when", "with",
))

_STEP_PHRASE = {
    "click": "clicks",
    "right_click": "right clicks",
    "double_click": "double clicks",
    "paste": "pastes",
    "key": "key presses",
    "scroll": "scrolls",
    "drag": "drags",
    "wait": "waits",
}

_RUN_BUNDLE_ROOT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "schema": {"const": "mw.run_bundle.v1"},
        "site_id": {"$ref": "#/$defs/slug"},
        "site_name": {"$ref": "#/$defs/text_short"},
        "domains": {"type": "array", "items": {"$ref": "#/$defs/hostname"}, "minItems": 1, "maxItems": 16, "uniqueItems": True},
        "recorded_date": {"$ref": "#/$defs/date"},
        "run_id": {"$ref": "#/$defs/slug"},
        "receipt_sha256": {"$ref": "#/$defs/sha256"},
        "journal_head_sha256": {"anyOf": [{"$ref": "#/$defs/sha256"}, {"type": "null"}]},
        "demo_id": {"$ref": "#/$defs/slug"},
        "workflow_id": {"anyOf": [{"$ref": "#/$defs/slug"}, {"type": "null"}]},
        "task_summary": {"type": "string", "minLength": 1, "maxLength": 240},
        "tags": {"type": "array", "items": {"$ref": "#/$defs/tag"}, "minItems": 0, "maxItems": 16, "uniqueItems": True},
        "surface": {"$ref": "#/$defs/surface"},
        "steps": {"type": "array", "items": {"$ref": "#/$defs/bundle_step"}, "minItems": 1, "maxItems": 256},
        "vision_elements": {"type": "array", "items": {"$ref": "#/$defs/vision_element"}, "minItems": 0, "maxItems": 64},
        "state_refs": {"type": "array", "items": {"$ref": "#/$defs/state_ref"}, "minItems": 0, "maxItems": 64},
        "refusals": {"type": "array", "items": {"$ref": "#/$defs/refusal"}, "minItems": 0, "maxItems": 64},
    },
    "required": [
        "schema", "site_id", "site_name", "domains", "recorded_date", "run_id", "receipt_sha256",
        "journal_head_sha256", "demo_id", "workflow_id", "task_summary", "tags", "surface",
        "steps", "vision_elements", "state_refs", "refusals",
    ],
}

RUN_BUNDLE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$defs": {
        "slug": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,62}$"},
        "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "date": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
        "hostname": {"type": "string", "pattern": "^[a-z0-9.-]+$", "minLength": 1, "maxLength": 253},
        "tag": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,31}$"},
        "text_short": {"type": "string", "minLength": 1, "maxLength": 80},
        "text_med": {"type": "string", "minLength": 1, "maxLength": 400},
        "int32": {"type": "integer", "minimum": -2147483648, "maximum": 2147483647},
        "bps": {"type": "integer", "minimum": 0, "maximum": 10000},
        "point_px": {"type": "array", "items": {"$ref": "#/$defs/int32"}, "minItems": 2, "maxItems": 2},
        "box_px": {"type": "array", "items": {"$ref": "#/$defs/int32"}, "minItems": 4, "maxItems": 4},
        "surface": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "viewport_px": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 2147483647}, "minItems": 2, "maxItems": 2},
                "ax_enabled": {"type": "boolean"},
                "auth_boundary": {"type": "string", "enum": ["anonymous", "operator_capsule"]},
                "perception_mode": {"type": "string", "enum": ["vision_only", "ax_plus_vision", "structured_plus_vision"]},
            },
            "required": ["viewport_px", "ax_enabled", "auth_boundary", "perception_mode"],
        },
        "bundle_mark": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "mark_id": {"$ref": "#/$defs/text_med"},
                "kind": {"$ref": "#/$defs/text_med"},
                "box_px": {"$ref": "#/$defs/box_px"},
                "relative_xy_bps": {"type": "array", "items": {"$ref": "#/$defs/bps"}, "minItems": 2, "maxItems": 2},
                "ocr_text": {"anyOf": [{"$ref": "#/$defs/text_med"}, {"type": "null"}]},
                "ocr_score_promille": {"anyOf": [{"type": "integer", "minimum": 0, "maximum": 1000}, {"type": "null"}]},
                "missing_reason": {"anyOf": [{"$ref": "#/$defs/text_med"}, {"type": "null"}]},
            },
            "required": ["mark_id", "kind", "box_px", "relative_xy_bps", "ocr_text", "ocr_score_promille", "missing_reason"],
        },
        "bundle_step": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "seq": {"type": "integer", "minimum": 1},
                "kind": {"type": "string", "enum": ["click", "right_click", "double_click", "paste", "key", "scroll", "drag", "wait"]},
                "button": {"anyOf": [{"type": "integer", "minimum": 1, "maximum": 3}, {"type": "null"}]},
                "recorded_point_px": {"anyOf": [{"$ref": "#/$defs/point_px"}, {"type": "null"}]},
                "modifiers": {"type": "array", "items": {"$ref": "#/$defs/text_short"}, "minItems": 0, "maxItems": 8},
                "key_name": {"anyOf": [{"$ref": "#/$defs/text_short"}, {"type": "null"}]},
                "text_len": {"anyOf": [{"type": "integer", "minimum": 0, "maximum": 100000}, {"type": "null"}]},
                "gap_before_ms": {"type": "integer", "minimum": 0},
                "gap_after_ms": {"type": "integer", "minimum": 0},
                "expected_change_box_px": {"anyOf": [{"$ref": "#/$defs/box_px"}, {"type": "null"}]},
                "frame_before_sha256": {"$ref": "#/$defs/sha256"},
                "frame_after_sha256": {"$ref": "#/$defs/sha256"},
                "verdict": {"type": "string", "enum": ["admitted", "refused"]},
                "verify_method": {"anyOf": [{"type": "string", "enum": ["pixel", "element", "state_sha256"]}, {"type": "null"}]},
                "mark": {"anyOf": [{"$ref": "#/$defs/bundle_mark"}, {"type": "null"}]},
            },
            "required": [
                "seq", "kind", "button", "recorded_point_px", "modifiers", "key_name", "text_len",
                "gap_before_ms", "gap_after_ms", "expected_change_box_px", "frame_before_sha256",
                "frame_after_sha256", "verdict", "verify_method", "mark",
            ],
        },
        "vision_element": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "id": {"$ref": "#/$defs/slug"},
                "role": {"type": "string", "enum": ["button", "checkbox", "dialog", "dropdown", "input", "link", "menu", "stepper", "tab", "toggle"]},
                "label": {"$ref": "#/$defs/text_short"},
                "box_px": {"$ref": "#/$defs/box_px"},
                "frame_sha256": {"$ref": "#/$defs/sha256"},
                "confidence_ppm": {"type": "integer", "minimum": 0, "maximum": 1000000},
            },
            "required": ["id", "role", "label", "box_px", "frame_sha256", "confidence_ppm"],
        },
        "state_ref": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "id": {"$ref": "#/$defs/slug"},
                "state_sha256": {"$ref": "#/$defs/sha256"},
                "summary": {"$ref": "#/$defs/text_med"},
            },
            "required": ["id", "state_sha256", "summary"],
        },
        "refusal": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "id": {"$ref": "#/$defs/slug"},
                "step_seq": {"type": "integer", "minimum": 1},
                "reason_code": {
                    "type": "string",
                    "enum": ["STALE_FRAME", "OUT_OF_BOUNDS", "BUDGET_EXHAUSTED", "SURFACE_NOT_AGENT_CONTROLLED", "POLICY", "TIMEOUT", "LOW_CONFIDENCE"],
                },
                "failure_tag": {"type": "string", "enum": ["invalid-input", "lazy", "overlay", "stale-element", "timeout", "wrong-state"]},
                "summary": {"$ref": "#/$defs/text_med"},
                "mitigation": {"$ref": "#/$defs/text_med"},
            },
            "required": ["id", "step_seq", "reason_code", "failure_tag", "summary", "mitigation"],
        },
        "run_bundle": _RUN_BUNDLE_ROOT,
    },
    **_RUN_BUNDLE_ROOT,
}


# @anchor:wa.learn.mw_export.MwExportError
class MwExportError(SkillRejected):
    """Refusal error raised when a skill or export parameter violates admission or schema rules."""

    # @anchor:wa.learn.mw_export.MwExportError.__init__
    def __init__(self, message, *, code="SKILL_REJECTED"):
        super().__init__(message)
        self.code = code


# @anchor:wa.learn.mw_export.canon_bytes
def canon_bytes(value):
    """Return canonical UTF-8 JSON bytes with a trailing newline."""
    return (gate.canon(value) + "\n").encode("utf-8")


# @anchor:wa.learn.mw_export.sha256_hex
def sha256_hex(data):
    """Return 64-character lowercase hexadecimal SHA-256 of bytes or UTF-8 str."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


# @anchor:wa.learn.mw_export.schema_view
def schema_view(root_name):
    """Build a per-root schema view from docs/spec/mw.v1.schema.json."""
    bundle = gate.strict_load(MW_V1_SCHEMA_PATH.read_bytes())
    defs = gate.compile_bundle(bundle)
    if root_name not in defs:
        raise MwExportError("unknown schema root: " + str(root_name), code="SCHEMA_ROOT")
    return {
        "$schema": bundle["$schema"],
        "$defs": defs,
        **defs[root_name],
    }


# @anchor:wa.learn.mw_export.verify_schema_pins
def verify_schema_pins():
    """Verify all pinned schema SHA-256 digests against local schema files and views."""
    mw_raw = MW_V1_SCHEMA_PATH.read_bytes()
    cu_raw = CU_ACTION_SCHEMA_PATH.read_bytes()
    computed = {
        "mw.v1.schema.json": sha256_hex(mw_raw),
        "cu_action.schema.json": sha256_hex(cu_raw),
        "mw.run_bundle.v1.schema.json": sha256_hex(canon_bytes(RUN_BUNDLE_SCHEMA)),
        "workflow.v1.json": sha256_hex(canon_bytes(schema_view("workflow"))),
        "demo-steps.v1.json": sha256_hex(canon_bytes(schema_view("demo_steps"))),
        "step.v1.json": sha256_hex(canon_bytes(schema_view("step"))),
        "demo.v1.json": sha256_hex(canon_bytes(schema_view("demo"))),
    }
    for name, expected in SCHEMA_PINS.items():
        if computed.get(name) != expected:
            raise MwExportError("schema pin mismatch for " + name, code="SCHEMA_PIN")
    return computed


# @anchor:wa.learn.mw_export.tokens
def tokens(text):
    """Extract normalized MemoryWeb index tokens from text."""
    out = []
    for tok in re.findall(r"[a-z0-9]+", str(text).lower()):
        if len(tok) < 2 or tok in _STOPWORDS:
            continue
        if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]
        if tok not in out:
            out.append(tok)
    return out


# @anchor:wa.learn.mw_export.slugify
def slugify(value, default="wender-skill"):
    """Normalize an arbitrary identifier into a valid MemoryWeb slug (^[a-z0-9][a-z0-9-]{0,62}$)."""
    if type(value) is not str:
        raise MwExportError("identifier must be a string", code="INVALID_SLUG")
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not cleaned:
        cleaned = default
    cleaned = cleaned[:63].rstrip("-")
    if not cleaned or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", cleaned) is None:
        raise MwExportError("cannot form valid slug", code="INVALID_SLUG")
    return cleaned


def _require_slug(value, field_name):
    if type(value) is not str or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value) is None:
        raise MwExportError(field_name + " must be a valid slug", code="INVALID_SLUG")
    return value


def _require_date(value):
    if type(value) is not str or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise MwExportError("recorded_date must be YYYY-MM-DD", code="INVALID_DATE")
    return value


def _require_sha256(value, field_name):
    if type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise MwExportError(field_name + " must be 64 lowercase hex chars", code="INVALID_SHA256")
    return value


def _clean_sentence(text):
    t = " ".join(str(text).split()).strip()
    if not t:
        raise MwExportError("sentence text must be nonblank", code="EMPTY_TEXT")
    return t if t.endswith(".") else (t + ".")


# @anchor:wa.learn.mw_export.admit_c11_skill
def admit_c11_skill(skill):
    """Validate and admit a C-11 skill (dict or canonical Markdown envelope).

    Refuses any unverified, legacy (non-C-11), or malformed skill per `skills.admit`
    and `skills.render_skill_md`. Returns a defensive copy of the admitted dict.
    """
    if type(skill) is str:
        parsed = skills.parse_skill_md(skill)
        skills.admit(parsed)
        skills.render_skill_md(parsed)
        return deepcopy(parsed)
    if type(skill) is not dict:
        raise MwExportError("skill must be a dict or C-11 Markdown string", code="SKILL_REJECTED")
    skills.admit(skill)
    skills.render_skill_md(skill)
    return deepcopy(skill)


# @anchor:wa.learn.mw_export.map_cu_action_kind
def map_cu_action_kind(kind):
    """Map a cu.action/v1 kind ('click'|'key'|'type'|'wait'|'done') or native demo kind to mw.demo_steps.v1 kind."""
    if type(kind) is not str:
        raise MwExportError("action kind must be a string", code="INVALID_ACTION_KIND")
    if kind in CU_TO_DEMO_KIND:
        return CU_TO_DEMO_KIND[kind]
    if kind in DEMO_STEP_KINDS:
        return kind
    if kind == "wait_signal":
        return "wait"
    raise MwExportError("unsupported action kind: " + kind, code="INVALID_ACTION_KIND")


# @anchor:wa.learn.mw_export.map_workflow_action_kind
def map_workflow_action_kind(kind):
    """Map a cu.action/v1 kind ('click'|'key'|'type'|'wait'|'done') or demo kind to mw.step.v1 action.kind."""
    demo_kind = map_cu_action_kind(kind)
    return DEMO_TO_WORKFLOW_KIND[demo_kind]


def _extract_action_and_expectation(step_or_action):
    """Extract (path, action_dict, expectation_dict) from a C-11 step, cu.action/v1 envelope, or raw action dict."""
    if type(step_or_action) is not dict or not step_or_action:
        raise MwExportError("step or action must be a nonempty dict", code="INVALID_STEP")
    if step_or_action.get("schema") == "cu.action/v1":
        act = step_or_action.get("action")
        if type(act) is not dict or not act:
            raise MwExportError("cu.action/v1 action must be a nonempty dict", code="INVALID_STEP")
        exp = step_or_action.get("expectation") if isinstance(step_or_action.get("expectation"), dict) else None
        return "coordinates", act, exp
    if "path" in step_or_action and "action" in step_or_action and isinstance(step_or_action["action"], dict):
        path = step_or_action["path"]
        if path not in ("api", "ax", "coordinates"):
            raise MwExportError("step path must be api|ax|coordinates", code="INVALID_STEP")
        act = step_or_action["action"]
        if not act:
            raise MwExportError("step action must be a nonempty dict", code="INVALID_STEP")
        exp = step_or_action.get("expectation") if isinstance(step_or_action.get("expectation"), dict) else (
            act.get("expectation") if isinstance(act.get("expectation"), dict) else None
        )
        return path, act, exp
    exp = step_or_action.get("expectation") if isinstance(step_or_action.get("expectation"), dict) else None
    return "coordinates", step_or_action, exp


def _infer_cu_kind(action):
    """Resolve explicit or structural action kind from a C-11 step action object."""
    if "kind" in action and action["kind"] is not None:
        kind = action["kind"]
        if type(kind) is not str:
            raise MwExportError("action.kind must be a string", code="INVALID_ACTION_KIND")
        return kind
    if "text" in action or "text_len" in action:
        return "type"
    if "keysym" in action or "key_name" in action or "key" in action:
        return "key"
    if "ms" in action and len(action) == 1:
        return "wait"
    if ("x" in action and "y" in action) or "element_id" in action or "mark_id" in action or "name" in action:
        return "click"
    raise MwExportError("cannot infer action kind from action fields", code="INVALID_ACTION_KIND")


def _extract_change_box(action, expectation):
    if "expected_change_box_px" in action and action["expected_change_box_px"] is not None:
        box = action["expected_change_box_px"]
        if (type(box) is list and len(box) == 4 and all(type(v) is int for v in box)
                and box[2] > box[0] and box[3] > box[1]):
            return list(box)
        raise MwExportError("invalid expected_change_box_px", code="INVALID_BOX")
    if expectation and expectation.get("kind") == "changed_region" and isinstance(expectation.get("region"), dict):
        reg = expectation["region"]
        if all(type(reg.get(k)) is int for k in ("x", "y", "w", "h")) and reg["w"] > 0 and reg["h"] > 0:
            return [reg["x"], reg["y"], reg["x"] + reg["w"], reg["y"] + reg["h"]]
        raise MwExportError("invalid expectation.region", code="INVALID_BOX")
    return None


def _extract_mark(action):
    if "mark" in action and action["mark"] is not None:
        m = action["mark"]
        if type(m) is not dict:
            raise MwExportError("mark must be a dict or null", code="INVALID_MARK")
        box = m.get("box_px")
        if not (type(box) is list and len(box) == 4 and all(type(v) is int for v in box)
                and box[2] > box[0] and box[3] > box[1]):
            raise MwExportError("invalid mark.box_px", code="INVALID_MARK")
        return {
            "mark_id": str(m.get("mark_id", "m-1"))[:400],
            "kind": str(m.get("kind", "button"))[:400],
            "box_px": list(box),
            "relative_xy_bps": list(m.get("relative_xy_bps", [5000, 5000])),
            "ocr_text": m.get("ocr_text"),
            "ocr_score_promille": m.get("ocr_score_promille"),
            "missing_reason": m.get("missing_reason"),
        }
    return None


# @anchor:wa.learn.mw_export.map_cu_action_to_demo_step
def map_cu_action_to_demo_step(step_or_action, seq=1):
    """Map one C-11 step, cu.action/v1 envelope, or action dict into a validated mw.demo_steps.v1 demo_step."""
    if type(seq) is not int or seq < 1:
        raise MwExportError("seq must be a positive exact int", code="INVALID_SEQ")
    _, action, expectation = _extract_action_and_expectation(step_or_action)
    raw_kind = _infer_cu_kind(action)
    demo_kind = map_cu_action_kind(raw_kind)

    button = None
    recorded_point_px = None
    key_name = None
    text_len = None
    gap_before_ms = action.get("gap_before_ms", 0) if type(action.get("gap_before_ms")) is int else 0
    gap_after_ms = action.get("gap_after_ms", 100) if type(action.get("gap_after_ms")) is int else 100
    if gap_before_ms < 0 or gap_after_ms < 0:
        raise MwExportError("gap_before_ms and gap_after_ms must be nonnegative", code="INVALID_GAP")

    modifiers = action.get("modifiers", [])
    if type(modifiers) is not list or not all(type(m) is str and 1 <= len(m) <= 80 for m in modifiers):
        raise MwExportError("modifiers must be a list of short strings", code="INVALID_MODIFIERS")
    modifiers = list(modifiers)

    if raw_kind == "done":
        if action.get("text") is not None or action.get("keysym") is not None:
            raise MwExportError("done action cannot carry text or keysym", code="INVALID_ACTION_FIELDS")
        gap_before_ms = 0
        gap_after_ms = 0
    elif demo_kind in ("click", "right_click", "double_click"):
        raw_btn = action.get("button")
        if raw_btn is not None:
            if type(raw_btn) is not int or not (1 <= raw_btn <= 3):
                raise MwExportError("button must be 1, 2, or 3", code="INVALID_BUTTON")
            button = raw_btn
        else:
            button = 2 if demo_kind == "right_click" else 1
        if "x" in action or "y" in action:
            x, y = action.get("x"), action.get("y")
            if type(x) is not int or type(y) is not int:
                raise MwExportError("click x and y must be exact integers", code="INVALID_POINT")
            recorded_point_px = [x, y]
        elif action.get("recorded_point_px") is not None:
            pt = action["recorded_point_px"]
            if type(pt) is not list or len(pt) != 2 or not all(type(v) is int for v in pt):
                raise MwExportError("recorded_point_px must be [x, y] ints", code="INVALID_POINT")
            recorded_point_px = list(pt)
    elif demo_kind == "key":
        kval = action.get("keysym") if action.get("keysym") is not None else (
            action.get("key_name") if action.get("key_name") is not None else action.get("key")
        )
        if type(kval) is not str or not kval.strip() or len(kval) > 80:
            raise MwExportError("key action requires nonblank keysym or key_name (<= 80 chars)", code="INVALID_KEY")
        key_name = kval
    elif demo_kind == "paste":
        if "text" in action and action["text"] is not None:
            tval = action["text"]
            if type(tval) is not str or len(tval) < 1 or len(tval) > 100000:
                raise MwExportError("type action text must be a nonempty string", code="INVALID_TEXT")
            text_len = len(tval)
        elif "text_len" in action and action["text_len"] is not None:
            tlen = action["text_len"]
            if type(tlen) is not int or tlen < 0 or tlen > 100000:
                raise MwExportError("paste action text_len must be an int in 0..100000", code="INVALID_TEXT_LEN")
            text_len = tlen
        else:
            raise MwExportError("type/paste action requires text or text_len", code="INVALID_TEXT")
    elif demo_kind == "wait":
        if "ms" in action and action["ms"] is not None:
            ms = action["ms"]
            if type(ms) is not int or ms < 0:
                raise MwExportError("wait action ms must be a nonnegative int", code="INVALID_WAIT")
            gap_after_ms = ms

    demo_step = {
        "seq": seq,
        "kind": demo_kind,
        "button": button,
        "recorded_point_px": recorded_point_px,
        "modifiers": modifiers,
        "key_name": key_name,
        "text_len": text_len,
        "gap_before_ms": gap_before_ms,
        "gap_after_ms": gap_after_ms,
        "expected_change_box_px": _extract_change_box(action, expectation),
        "mark": _extract_mark(action),
    }
    canonical_json(demo_step)
    return demo_step


def _step_frame_digests(skill_canonical, step_count, frame_digests=None):
    """Return a list of step_count + 1 deterministic 64-hex frame SHA-256 digests."""
    if frame_digests is not None:
        if type(frame_digests) not in (list, tuple) or len(frame_digests) != step_count + 1:
            raise MwExportError("frame_digests must have len(steps) + 1 items", code="INVALID_FRAMES")
        return [_require_sha256(d, "frame_digest") for d in frame_digests]
    return [sha256_hex(f"{skill_canonical}:frame:{i}") for i in range(step_count + 1)]


# @anchor:wa.learn.mw_export.render_demo_steps
def render_demo_steps(skill, *, site_id="wender-site", demo_id=None):
    """Validate an admitted C-11 skill and render a canonical mw.demo_steps.v1 dict."""
    admitted = admit_c11_skill(skill)
    _require_slug(site_id, "site_id")
    resolved_demo_id = _require_slug(demo_id, "demo_id") if demo_id is not None else slugify(admitted["id"] + "-demo")
    steps = [
        map_cu_action_to_demo_step(step, seq=idx)
        for idx, step in enumerate(admitted["steps"], 1)
    ]
    doc = {
        "schema": "mw.demo_steps.v1",
        "demo_id": resolved_demo_id,
        "site_id": site_id,
        "steps": steps,
    }
    defs = gate.compile_bundle(gate.strict_load(MW_V1_SCHEMA_PATH.read_bytes()))
    if not gate.conforms(doc, defs["demo_steps"], defs):
        raise MwExportError("rendered demo_steps failed mw.demo_steps.v1 validation", code="SCHEMA_INVALID")
    return doc


# @anchor:wa.learn.mw_export.render_workflow_steps
def render_workflow_steps(skill, *, frame_digests=None):
    """Validate an admitted C-11 skill and render (mw.step.v1 steps, text_slots)."""
    admitted = admit_c11_skill(skill)
    skill_canon = canonical_json(admitted)
    digests = _step_frame_digests(skill_canon, len(admitted["steps"]), frame_digests=frame_digests)

    wf_steps = []
    text_slots = []
    defs = gate.compile_bundle(gate.strict_load(MW_V1_SCHEMA_PATH.read_bytes()))

    for idx, raw_step in enumerate(admitted["steps"], 1):
        path, action_obj, _ = _extract_action_and_expectation(raw_step)
        demo_step = map_cu_action_to_demo_step(raw_step, seq=idx)
        act_kind = map_workflow_action_kind(demo_step["kind"])
        slot_id = ("slot-%d" % idx) if demo_step["text_len"] is not None else None
        if slot_id is not None:
            slot_doc = {
                "slot_id": slot_id,
                "transport": "clipboard_paste",
                "clear_first": False,
                "budget_ms": 1000,
                "max_length": max(1, demo_step["text_len"]),
                "sensitivity": "private_runtime_only",
                "chip_clear_steps": [],
                "empty_len_allowed": [0],
                "post_type_len_allowed": ["exact_n"],
            }
            if not gate.conforms(slot_doc, defs["slot"], defs):
                raise MwExportError("rendered slot failed schema validation", code="SCHEMA_INVALID")
            text_slots.append(slot_doc)

        dx_px = action_obj.get("dx_px") if act_kind in ("drag", "scroll") and type(action_obj.get("dx_px")) is int else None
        dy_px = action_obj.get("dy_px") if act_kind in ("drag", "scroll") and type(action_obj.get("dy_px")) is int else None

        step_doc = {
            "schema": "mw.step.v1",
            "seq": idx,
            "protected": False,
            "action": {
                "kind": act_kind,
                "button": demo_step["button"],
                "text_slot": slot_id,
                "key_name": demo_step["key_name"],
                "dx_px": dx_px,
                "dy_px": dy_px,
                "destination": None,
                "delivery_budget_ms": 1000,
                "path": path,
            },
            "target": None,
            "preconditions": [],
            "already_satisfied": None,
            "verify": {
                "mode": "state_signal",
                "predicate": {
                    "kind": "state_signal",
                    "capability_id": "mw.frame.stable.v1",
                    "bindings": [],
                    "evidence_refs": [digests[idx]],
                    "comparison": "truth",
                    "snapshot_ref": None,
                },
                "change_box_px": demo_step["expected_change_box_px"],
                "postcondition_threshold_bps": None,
                "recorded_gap_ms": demo_step["gap_after_ms"],
                "wait_multiplier_num": 1,
                "wait_multiplier_den": 1,
                "deadline_ms": 5000,
                "poll_interval_ms": 100,
                "consecutive_polls": 1,
                "ram_evidence_ref": digests[idx],
                "mask_boxes_px": [],
                "busy_predicate": None,
                "fallback_verifiers": [],
            },
            "fallback_targets": [],
            "capture_before": [],
            "when": None,
            "repeat": None,
            "locate_wait_ms": 0,
            "locate_poll_ms": 100,
        }
        if not gate.conforms(step_doc, defs["step"], defs):
            raise MwExportError("rendered step failed mw.step.v1 validation", code="SCHEMA_INVALID")
        wf_steps.append(step_doc)

    return wf_steps, text_slots


# @anchor:wa.learn.mw_export.render_explanation
def render_explanation(skill, *, site_id="wender-site", run_id=None, receipt_sha256=None, frame_digests=None):
    """Validate an admitted C-11 skill and render the six-heading EXPLANATION.md draft text."""
    admitted = admit_c11_skill(skill)
    _require_slug(site_id, "site_id")
    skill_canon = canonical_json(admitted)
    skill_sha = sha256_hex(skill_canon)
    resolved_run_id = _require_slug(run_id, "run_id") if run_id is not None else slugify("run-" + admitted["id"])
    resolved_receipt = (
        _require_sha256(receipt_sha256, "receipt_sha256")
        if receipt_sha256 is not None
        else admitted["evidence"].get("sha256", skill_sha)
    )
    digests = _step_frame_digests(skill_canon, len(admitted["steps"]), frame_digests=frame_digests)
    demo_steps = [map_cu_action_to_demo_step(s, seq=i) for i, s in enumerate(admitted["steps"], 1)]

    counts = {}
    for s in demo_steps:
        counts[s["kind"]] = counts.get(s["kind"], 0) + 1
    ordered_kinds = [k for k in ("key", "click", "paste", "scroll", "right_click", "double_click", "drag", "wait") if k in counts]
    breakdown = ", ".join("%d %s" % (counts[k], _STEP_PHRASE[k]) for k in ordered_kinds)

    pastes = [s for s in demo_steps if s["text_len"] is not None]
    marks = [s for s in demo_steps if s["mark"] is not None]
    support_n = admitted.get("support_n", admitted["evidence"].get("support_n"))
    version = admitted["evidence"]["version"]

    if pastes:
        slots_line = "%d steps carry operator text length (%s); only text length is stored and raw text is never recorded." % (
            len(pastes),
            ", ".join("step %d length %d" % (s["seq"], s["text_len"]) for s in pastes),
        )
    else:
        slots_line = "No variable text slots were recorded in this run."

    if marks:
        controls_line = "%d steps carry a UI mark in the recorded steps document." % len(marks)
    else:
        controls_line = "No ambiguous UI marks were recorded in this run."

    pre_keys = sorted(admitted["precondition_signature"].keys())
    start_line = "Initial frame digest `%s` with precondition keys (%s) captured before step 1." % (
        digests[0][:16],
        ", ".join(pre_keys),
    )

    post_keys = sorted(admitted["expected_post"].keys())
    result_line = "%d admitted steps and 0 refused steps ending at frame digest `%s` (receipt `%s`, %d checkpoints, postcondition keys: %s)." % (
        len(demo_steps),
        digests[-1][:16],
        resolved_receipt[:16],
        len(admitted["checkpoints"]),
        ", ".join(post_keys),
    )

    recovery = admitted["failure_recovery"]
    rec_policy = recovery.get("policy", "abort")
    rec_steps = len(recovery.get("steps", []))
    exc_line = "None recorded in the run bundle; C-11 failure recovery policy is `%s` (%d recovery steps)." % (
        rec_policy,
        rec_steps,
    )

    lines = [
        '<a id="intent"></a>',
        "## Intent",
        "",
        "%s Admitted C-11 skill `%s` (version %d, support_n %d) exported as run `%s` on site `%s` with %d steps (%s)." % (
            _clean_sentence(admitted["intent"]),
            admitted["id"],
            version,
            support_n,
            resolved_run_id,
            site_id,
            len(demo_steps),
            breakdown,
        ),
        "",
        "## Starting state",
        "",
        start_line,
        "",
        "## Variable slots",
        "",
        slots_line,
        "",
        "## Ambiguous controls",
        "",
        controls_line,
        "",
        "## Expected result",
        "",
        result_line,
        "",
        "## Exceptions",
        "",
        exc_line,
        "",
    ]
    return "\n".join(lines)


# @anchor:wa.learn.mw_export.render_workflow_draft
def render_workflow_draft(
    skill,
    *,
    site_id="wender-site",
    workflow_id=None,
    demo_id=None,
    recorded_date="2026-10-28",
    run_id=None,
    receipt_sha256=None,
    frame_digests=None,
):
    """Validate an admitted C-11 skill and render a schema-valid mw.workflow.v1 draft dict."""
    admitted = admit_c11_skill(skill)
    _require_slug(site_id, "site_id")
    _require_date(recorded_date)

    wf_id = _require_slug(workflow_id, "workflow_id") if workflow_id is not None else slugify(admitted["id"])
    dm_id = _require_slug(demo_id, "demo_id") if demo_id is not None else slugify(admitted["id"] + "-demo")
    rn_id = _require_slug(run_id, "run_id") if run_id is not None else slugify("run-" + admitted["id"])

    skill_canon = canonical_json(admitted)
    skill_sha = sha256_hex(skill_canon)
    resolved_receipt = (
        _require_sha256(receipt_sha256, "receipt_sha256")
        if receipt_sha256 is not None
        else admitted["evidence"].get("sha256", skill_sha)
    )
    digests = _step_frame_digests(skill_canon, len(admitted["steps"]), frame_digests=frame_digests)

    demo_steps_doc = render_demo_steps(admitted, site_id=site_id, demo_id=dm_id)
    steps_sha256 = sha256_hex(canon_bytes(demo_steps_doc))
    wf_steps, text_slots = render_workflow_steps(admitted, frame_digests=digests)

    kw = tokens(admitted["intent"])[:8]
    if not kw:
        kw = [wf_id]

    support_n = admitted.get("support_n", admitted["evidence"].get("support_n"))
    runs_n = admitted["evidence"].get("runs", support_n)

    wf_doc = {
        "schema": "mw.workflow.v1",
        "id": wf_id,
        "site_id": site_id,
        "status": "draft",
        "admission_basis": "operator_demo",
        "baseline_id": None,
        "fallback": {"kind": "ask_operator", "workflow_id": None},
        "trigger_sig": {
            "task_kind": wf_id,
            "keywords": kw,
            "select_when": [],
            "reject_when": [],
        },
        "engine_compat": [{
            "engine": "wender-agent",
            "version_constraint": "any",
            "required_capabilities": ["placeholder-capability"],
        }],
        "segmentation_params": {"hi": 1, "lo": 0, "quiet": 1, "min_len": 1},
        "preconditions": [],
        "text_slots": text_slots,
        "steps": wf_steps,
        "antipattern_guards": [],
        "oracle": {
            "postconditions": [{
                "kind": "state_signal",
                "capability_id": "mw.frame.stable.v1",
                "bindings": [],
                "evidence_refs": [digests[-1]],
                "comparison": "truth",
                "snapshot_ref": None,
            }],
            "planted_mutant_ids": [],
            "mutants_killed": 0,
            "mutants_total": 0,
            "receipt_refs": [],
        },
        "stats": {
            "support_n": support_n,
            "replay_pass": runs_n,
            "reproduced_ppm": 1000000,
            "min_r_e": None,
            "saved_tokens_bps": None,
            "online_shadow_runs": None,
            "shadow_regressions": None,
            "online_fail_streak": None,
            "margin_gain": None,
            "margin_mass": None,
            "margin_bound": None,
            "oos_mass_bps": None,
        },
        "provenance": {
            "source": "engine-run",
            "tier": 3,
            "recorded": recorded_date,
            "source_ref": "engine-run bundle %s, steps sha256 prefix %s" % (rn_id, steps_sha256[:16]),
            "run_id": rn_id,
            "quote_sha256": None,
            "receipt_sha256": resolved_receipt,
            "derived_from_sha256": skill_sha,
        },
        "execution_policy": {
            "allow_capsule_restart": False,
            "forbidden_keys": [],
            "min_action_pace_ms": 0,
            "min_notice_s": 10,
            "requires_capsule_lease": True,
            "requires_operator_go_ahead_on_write": True,
        },
        "evaluation_profile": {
            "kind": "initial_task_v1",
            "fixture_ids": [dm_id],
            "cohort_sha256": resolved_receipt,
            "baseline_workflow_sha256": None,
        },
        "script_ref": None,
        "script_sha256": None,
    }
    defs = gate.compile_bundle(gate.strict_load(MW_V1_SCHEMA_PATH.read_bytes()))
    if not gate.conforms(wf_doc, defs["workflow"], defs):
        raise MwExportError("rendered workflow draft failed mw.workflow.v1 validation", code="SCHEMA_INVALID")
    return wf_doc


# @anchor:wa.learn.mw_export.render_run_bundle
def render_run_bundle(
    skill,
    *,
    site_id="wender-site",
    site_name="Wender Site",
    domains=("example.invalid",),
    recorded_date="2026-10-28",
    run_id=None,
    demo_id=None,
    workflow_id=None,
    tags=(),
    viewport_px=(1280, 800),
    receipt_sha256=None,
    frame_digests=None,
):
    """Validate an admitted C-11 skill and render a schema-valid mw.run_bundle.v1 dict."""
    admitted = admit_c11_skill(skill)
    _require_slug(site_id, "site_id")
    _require_date(recorded_date)
    if type(site_name) is not str or not (1 <= len(site_name.strip()) <= 80):
        raise MwExportError("site_name must be 1..80 chars", code="INVALID_SITE_NAME")

    wf_id = _require_slug(workflow_id, "workflow_id") if workflow_id is not None else slugify(admitted["id"])
    dm_id = _require_slug(demo_id, "demo_id") if demo_id is not None else slugify(admitted["id"] + "-demo")
    rn_id = _require_slug(run_id, "run_id") if run_id is not None else slugify("run-" + admitted["id"])

    skill_canon = canonical_json(admitted)
    skill_sha = sha256_hex(skill_canon)
    resolved_receipt = (
        _require_sha256(receipt_sha256, "receipt_sha256")
        if receipt_sha256 is not None
        else admitted["evidence"].get("sha256", skill_sha)
    )
    digests = _step_frame_digests(skill_canon, len(admitted["steps"]), frame_digests=frame_digests)

    bundle_steps = []
    for idx, raw_step in enumerate(admitted["steps"], 1):
        path, _, _ = _extract_action_and_expectation(raw_step)
        dstep = map_cu_action_to_demo_step(raw_step, seq=idx)
        verify_method = "element" if path == "ax" else ("state_sha256" if path == "api" else "pixel")
        bundle_steps.append({
            "seq": idx,
            "kind": dstep["kind"],
            "button": dstep["button"],
            "recorded_point_px": dstep["recorded_point_px"],
            "modifiers": dstep["modifiers"],
            "key_name": dstep["key_name"],
            "text_len": dstep["text_len"],
            "gap_before_ms": dstep["gap_before_ms"],
            "gap_after_ms": dstep["gap_after_ms"],
            "expected_change_box_px": dstep["expected_change_box_px"],
            "frame_before_sha256": digests[idx - 1],
            "frame_after_sha256": digests[idx],
            "verdict": "admitted",
            "verify_method": verify_method,
            "mark": dstep["mark"],
        })

    paths_used = {s["path"] for s in admitted["steps"]}
    ax_enabled = "ax" in paths_used
    perception_mode = "structured_plus_vision" if "api" in paths_used else (
        "ax_plus_vision" if ax_enabled else "vision_only"
    )

    state_refs = [
        {
            "id": "state-precondition",
            "state_sha256": sha256_hex(canonical_json(admitted["precondition_signature"])),
            "summary": ("Precondition state for %s." % admitted["id"])[:400],
        },
        {
            "id": "state-postcondition",
            "state_sha256": sha256_hex(canonical_json(admitted["expected_post"])),
            "summary": ("Expected postcondition state for %s." % admitted["id"])[:400],
        },
    ]

    bundle_doc = {
        "schema": "mw.run_bundle.v1",
        "site_id": site_id,
        "site_name": site_name,
        "domains": list(domains),
        "recorded_date": recorded_date,
        "run_id": rn_id,
        "receipt_sha256": resolved_receipt,
        "journal_head_sha256": skill_sha,
        "demo_id": dm_id,
        "workflow_id": wf_id,
        "task_summary": admitted["intent"][:240],
        "tags": list(tags),
        "surface": {
            "viewport_px": list(viewport_px),
            "ax_enabled": ax_enabled,
            "auth_boundary": "operator_capsule",
            "perception_mode": perception_mode,
        },
        "steps": bundle_steps,
        "vision_elements": [],
        "state_refs": state_refs,
        "refusals": [],
    }
    rb_defs = gate.compile_bundle(RUN_BUNDLE_SCHEMA)
    if not gate.conforms(bundle_doc, rb_defs["run_bundle"], rb_defs):
        raise MwExportError("rendered run_bundle failed mw.run_bundle.v1 validation", code="SCHEMA_INVALID")
    return bundle_doc


# @anchor:wa.learn.mw_export.export_skill_draft
def export_skill_draft(
    skill,
    *,
    site_id="wender-site",
    site_name="Wender Site",
    domains=("example.invalid",),
    recorded_date="2026-10-28",
    run_id=None,
    demo_id=None,
    workflow_id=None,
    tags=(),
    viewport_px=(1280, 800),
    receipt_sha256=None,
    frame_digests=None,
):
    """Render an admitted C-11 skill into workflow, demo_steps, explanation and run_bundle drafts."""
    admitted = admit_c11_skill(skill)
    skill_canon = canonical_json(admitted)
    skill_sha = sha256_hex(skill_canon)
    digests = _step_frame_digests(skill_canon, len(admitted["steps"]), frame_digests=frame_digests)

    demo_steps_doc = render_demo_steps(admitted, site_id=site_id, demo_id=demo_id)
    wf_doc = render_workflow_draft(
        admitted,
        site_id=site_id,
        workflow_id=workflow_id,
        demo_id=demo_steps_doc["demo_id"],
        recorded_date=recorded_date,
        run_id=run_id,
        receipt_sha256=receipt_sha256,
        frame_digests=digests,
    )
    explanation_md = render_explanation(
        admitted,
        site_id=site_id,
        run_id=wf_doc["provenance"]["run_id"],
        receipt_sha256=wf_doc["provenance"]["receipt_sha256"],
        frame_digests=digests,
    )
    run_bundle_doc = render_run_bundle(
        admitted,
        site_id=site_id,
        site_name=site_name,
        domains=domains,
        recorded_date=recorded_date,
        run_id=wf_doc["provenance"]["run_id"],
        demo_id=demo_steps_doc["demo_id"],
        workflow_id=wf_doc["id"],
        tags=tags,
        viewport_px=viewport_px,
        receipt_sha256=wf_doc["provenance"]["receipt_sha256"],
        frame_digests=digests,
    )
    return {
        "skill_sha256": skill_sha,
        "workflow": wf_doc,
        "workflow_steps": deepcopy(wf_doc["steps"]),
        "text_slots": deepcopy(wf_doc["text_slots"]),
        "demo_steps": demo_steps_doc,
        "explanation": explanation_md,
        "explanation_bytes": explanation_md.encode("utf-8"),
        "run_bundle": run_bundle_doc,
    }
