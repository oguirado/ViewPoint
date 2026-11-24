"""Validate and seal public synthetic capsule episode evidence."""

import hashlib
import json
import re

SCHEMA_VERSION = "rea.episode_provenance/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_STEP_FIELDS = {"target_id", "start_ns", "end_ns"}


def _object(value, fields, path):
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ValueError(f"{path} must be a JSON object with string keys")
    missing = fields - value.keys()
    if missing:
        raise ValueError(f"{path} is missing required fields: {', '.join(sorted(missing))}")
    unexpected = value.keys() - fields
    if unexpected:
        raise ValueError(f"{path} has unexpected fields: {', '.join(sorted(unexpected))}")


def _text(value, path):
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{path} must be a nonempty trimmed string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{path} must be valid UTF-8 text") from error
    return value


def _digest(value, path):
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{path} must be a lowercase SHA-256 hex digest")
    return value


def _nonnegative_int(value, path):
    if type(value) is not int or value < 0:
        raise ValueError(f"{path} must be a nonnegative integer")
    return value


def seal_episode(record):
    """Return a receipt with schema, normalized evidence and its SHA-256.

    The input is a JSON object with a named steps object. Each step supplies
    an interval on the declared host monotonic clock for the same session.
    The before and after steps also supply decoded frame digests and
    observation ordinals. A malformed or inconsistent record raises
    ValueError. The caller supplies the capture evidence and its provenance.
    """
    _object(record, {"episode_id", "target_id", "clock", "source_sha256", "steps"}, "record")
    target = _text(record["target_id"], "target_id")
    evidence = {
        "episode_id": _text(record["episode_id"], "episode_id"),
        "target_id": target,
        "clock": _text(record["clock"], "clock"),
        "source_sha256": _digest(record["source_sha256"], "source_sha256"),
        "steps": {},
    }
    steps = record["steps"]
    _object(steps, {"before", "action", "after"}, "steps")

    normalized_steps = evidence["steps"]
    for name in ("before", "action", "after"):
        path = f"steps.{name}"
        step = steps[name]
        fields = {"action_id"} if name == "action" else {"frame_sha256", "frame_index"}
        _object(step, _STEP_FIELDS | fields, path)
        step_target = _text(step["target_id"], f"{path}.target_id")
        if step_target != target:
            raise ValueError(f"{path}.target_id must match the episode target_id")
        start = _nonnegative_int(step["start_ns"], f"{path}.start_ns")
        end = _nonnegative_int(step["end_ns"], f"{path}.end_ns")
        if start > end:
            raise ValueError(f"{path}.start_ns must be at or before end_ns")

        normalized = {"target_id": step_target, "start_ns": start, "end_ns": end}
        if name == "action":
            normalized["action_id"] = _text(step["action_id"], f"{path}.action_id")
        else:
            normalized["frame_sha256"] = _digest(step["frame_sha256"], f"{path}.frame_sha256")
            normalized["frame_index"] = _nonnegative_int(step["frame_index"], f"{path}.frame_index")
        normalized_steps[name] = normalized

    before = normalized_steps["before"]
    action = normalized_steps["action"]
    after = normalized_steps["after"]
    if before["end_ns"] >= action["start_ns"]:
        raise ValueError("steps.before.end_ns must precede steps.action.start_ns")
    if action["end_ns"] >= after["start_ns"]:
        raise ValueError("steps.action.end_ns must precede steps.after.start_ns")
    if before["frame_index"] >= after["frame_index"]:
        raise ValueError("steps.before.frame_index must precede steps.after.frame_index")

    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "schema": SCHEMA_VERSION,
        "evidence": evidence,
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }
