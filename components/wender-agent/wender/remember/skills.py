# @module {"id":"wa.remember.skills","layer":1,"owner":"REMEMBER","deps":["wa.remember.program_map"],"book":"HB-WA-s3-remember"}
"""Skill admission, C-11 promotion and canonical metadata-only Markdown.

Legacy records retain their seven-field admission and extra metadata. C-11
records opt into typed promotion thresholds and versioned interchange. These
functions validate evidence claims; only the supplied replayer can produce
verification evidence. No function executes a skill or migrates a journal.
"""
from copy import deepcopy
import json
import re

from .program_map import canonical_json

REQUIRED = ("id", "intent", "params", "steps", "checkpoints", "provenance", "evidence")
_C11_FIELDS = ("precondition_signature", "expected_post", "failure_recovery")
_C11_ALL = frozenset(REQUIRED + _C11_FIELDS + ("support_n", "min_err"))
_MARKDOWN_PREFIX = "# Wender C-11 skill unit (v1)\n\n```json\n"
_MARKDOWN_SUFFIX = "\n```\n"


# @anchor:wa.remember.skills.SkillRejected
class SkillRejected(ValueError):
    pass


def _canonical(value):
    try:
        return canonical_json(value)
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise SkillRejected(str(exc)) from exc


def _nonblank(value, field):
    if field == "id" and type(value) is not str:
        raise SkillRejected("skill id must be a string")
    if type(value) is not str or not value.strip():
        raise SkillRejected(field + " must be a nonblank string")


def _object(value, field):
    if type(value) is not dict or not value:
        raise SkillRejected(field + " must be a nonempty object")


def _fields(value, required, optional=()):
    if type(value) is not dict or not set(required).issubset(value) or set(value) - (
            set(required) | set(optional)):
        raise SkillRejected("missing or unknown C-11 fields")


def _positive_version(value):
    if type(value) is not int or value < 1:
        raise SkillRejected("evidence.version must be a positive exact int")
    return value


def _base(skill, *, allow_unverified=False):
    """Keep the established six rejection stages and their error messages."""
    if type(skill) is not dict:
        raise SkillRejected("skill must be an object")
    missing = [key for key in REQUIRED if key not in skill]
    if missing:
        raise SkillRejected("missing %s" % missing)
    if not skill["steps"]:
        raise SkillRejected("no steps")
    if type(skill["steps"]) is not list:
        raise SkillRejected("steps must be a list")
    if not skill["checkpoints"]:
        raise SkillRejected("no verification checkpoints")
    if type(skill["checkpoints"]) is not list:
        raise SkillRejected("checkpoints must be a list")
    ev = skill["evidence"]
    if allow_unverified:
        if type(ev) is not dict or type(ev.get("reproduced")) is not bool or type(
                ev.get("verified_by")) is not str:
            raise SkillRejected("reproduction not verified (evidence.reproduced/verified_by)")
    elif type(ev) is not dict or ev.get("reproduced") is not True or not ev.get("verified_by"):
        raise SkillRejected("reproduction not verified (evidence.reproduced/verified_by)")
    if type(skill["provenance"]) is not dict or not skill["provenance"].get("source"):
        raise SkillRejected("provenance.source required")
    for step in skill["steps"]:
        if type(step) is not dict or step.get("path") not in ("api", "ax", "coordinates"):
            raise SkillRejected("step path must be api|ax|coordinates")
    _nonblank(skill["id"], "id")
    _nonblank(skill["intent"], "intent")
    if type(skill["params"]) is not dict:
        raise SkillRejected("params must be an object")
    # Legacy source/verifier truthiness, arbitrary checkpoint metadata, optional
    # version and extra fields remain supported. C-11 strengthens them below.
    _canonical(skill)


def _is_c11(skill):
    return (any(key in skill for key in _C11_FIELDS + ("support_n", "min_err")) or
            any(key in skill["evidence"] for key in ("support_n", "min_err")))


def _structured_steps(steps, *, allow_empty=False):
    if type(steps) is not list or (not steps and not allow_empty):
        raise SkillRejected("C-11 steps must be a nonempty list")
    for step in steps:
        _fields(step, ("path", "action"))
        if step["path"] not in ("api", "ax", "coordinates"):
            raise SkillRejected("step path must be api|ax|coordinates")
        _object(step["action"], "action")


def _c11(skill, *, require_version):
    """Validate either the structured unit or the existing evidence-counter unit.

    Top-level support_n/min_err select the closed structured C-11 schema. The
    previously published production fixture carries those counters in evidence
    and keeps legacy action/checkpoint/recovery metadata; that form is retained
    without coercing or silently translating its predicates.
    """
    if not _is_c11(skill):
        raise SkillRejected("C-11 promotion fields required")
    for field in _C11_FIELDS:
        if field not in skill:
            raise SkillRejected("missing C-11 field: " + field)
        _object(skill[field], field)
    ev = skill["evidence"]
    structured = "support_n" in skill or "min_err" in skill
    if structured:
        _fields(skill, _C11_ALL)
        _structured_steps(skill["steps"])
        for checkpoint in skill["checkpoints"]:
            _fields(checkpoint, ("after_step", "expected_post"))
            index = checkpoint["after_step"]
            if type(index) is not int or not 0 <= index < len(skill["steps"]):
                raise SkillRejected("after_step must be an in-range zero-based exact int")
            _object(checkpoint["expected_post"], "checkpoint.expected_post")
        recovery = skill["failure_recovery"]
        _fields(recovery, ("policy", "steps"))
        if recovery["policy"] == "abort":
            _structured_steps(recovery["steps"], allow_empty=True)
            if recovery["steps"]:
                raise SkillRejected("abort recovery must have no steps")
        elif recovery["policy"] == "replay":
            _structured_steps(recovery["steps"])
        else:
            raise SkillRejected("unknown recovery policy")
        _fields(skill["provenance"], ("source",))
        _fields(ev, ("reproduced", "verified_by"), ("version", "runs", "sha256"))
        counters = skill
    else:
        if "support_n" not in ev or "min_err" not in ev:
            raise SkillRejected("C-11 evidence support_n and min_err required")
        counters = ev
    _nonblank(skill["provenance"]["source"], "provenance.source")
    if type(ev.get("reproduced")) is not bool or type(ev.get("verified_by")) is not str:
        raise SkillRejected("C-11 evidence requires Boolean reproduction and verifier text")
    if type(counters["support_n"]) is not int or counters["support_n"] < 0:
        raise SkillRejected("support_n must be a nonnegative exact int")
    if type(counters["min_err"]) is not int:
        raise SkillRejected("min_err must be an exact int")
    if require_version or "version" in ev:
        _positive_version(ev.get("version"))
    if "runs" in ev and (type(ev["runs"]) is not int or ev["runs"] < 1):
        raise SkillRejected("evidence.runs must be a positive exact int")
    if "sha256" in ev and (type(ev["sha256"]) is not str or
            re.fullmatch(r"[0-9a-f]{64}", ev["sha256"]) is None):
        raise SkillRejected("evidence.sha256 must be 64 lowercase hexadecimal characters")
    return (counters["support_n"] >= 3 and counters["min_err"] >= 0 and
            ev["reproduced"] is True and bool(ev["verified_by"].strip()))


# @anchor:wa.remember.skills.admit
def admit(skill):
    """Return canonical metadata for a valid legacy skill or promoted C-11 unit."""
    _base(skill)
    if _is_c11(skill) and not _c11(skill, require_version=True):
        raise SkillRejected("skill does not satisfy the C-11 promotion gate")
    return _sculpt_canonical(skill)


# @anchor:wa.remember.skills.promotion_eligible
def promotion_eligible(skill):
    """Validate C-11 draft shape and evaluate claims; never invent evidence."""
    _base(skill, allow_unverified=True)
    return _c11(skill, require_version=False)


# @anchor:wa.remember.skills.next_evidence_version
def next_evidence_version(previous=None):
    """Allocate 1, or a validated prior record's positive exact version plus 1."""
    if previous is None:
        return 1
    admit(previous)
    if not _c11(previous, require_version=True):
        raise SkillRejected("previous record does not satisfy the C-11 promotion gate")
    return _positive_version(previous["evidence"].get("version")) + 1


# @anchor:wa.remember.skills.promote_skill
def promote_skill(draft, previous=None):
    """Return an independent C-11 revision, retaining all evidence metadata."""
    if not promotion_eligible(draft):
        raise SkillRejected("skill does not satisfy the C-11 promotion gate")
    version = next_evidence_version(previous)
    if previous is not None and previous["id"] != draft["id"]:
        raise SkillRejected("previous record belongs to another skill id")
    if "version" in draft["evidence"] and draft["evidence"]["version"] != version:
        raise SkillRejected("supplied version is not the exact next version")
    promoted = deepcopy(draft)
    promoted["evidence"]["version"] = version
    admit(promoted)
    return promoted


# @anchor:wa.remember.skills.render_skill_md
def render_skill_md(skill):
    """Render a C-11 unit as one canonical Unicode JSON line in a fixed fence."""
    canonical = admit(skill)
    _c11(skill, require_version=True)
    return _MARKDOWN_PREFIX + canonical + _MARKDOWN_SUFFIX


def _unique_object(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise SkillRejected("duplicate JSON object key: " + key)
        value[key] = child
    return value


def _reject_number(value):
    raise SkillRejected("noninteger JSON number: " + value)


# @anchor:wa.remember.skills.parse_skill_md
def parse_skill_md(text):
    """Reject ambiguous JSON, alternate envelopes and noncanonical encodings."""
    if type(text) is not str or not text.startswith(_MARKDOWN_PREFIX) or not text.endswith(
            _MARKDOWN_SUFFIX):
        raise SkillRejected("invalid Markdown envelope")
    payload = text[len(_MARKDOWN_PREFIX):-len(_MARKDOWN_SUFFIX)]
    if not payload or "\n" in payload or "\r" in payload:
        raise SkillRejected("payload must be one nonempty JSON line")
    try:
        skill = json.loads(payload, object_pairs_hook=_unique_object,
                           parse_float=_reject_number, parse_constant=_reject_number)
    except (ValueError, TypeError, RecursionError) as exc:
        raise SkillRejected("invalid strict JSON payload") from exc
    if render_skill_md(skill) != text:
        raise SkillRejected("payload is not in the canonical spelling")
    return deepcopy(skill)


# Explicit long names mirror the independent specification's vocabulary.
render_skill_markdown = render_skill_md
parse_skill_markdown = parse_skill_md


def _sculpt_canonical(skill):
    # Preserve published legacy source coordinates while extending admission.
    if skill.get("domain") == "sculpt" or "sculpt" in skill:
        from .sculpt import validate_sculpt_skill
        validate_sculpt_skill(skill)
    return _canonical(skill)
