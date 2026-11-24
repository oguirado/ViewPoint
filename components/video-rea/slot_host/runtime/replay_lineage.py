# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed replay lineage validation (U10).

The host-injected ``tape_resolver(parent_episode_id, tape_sha256)``
returns an authorized retention record with ``parent_episode_id`` and
``input_tape_sha256``, or None if the pair does not resolve. Invalid or
unretained lineage raises ReplayLineageError. The production resolver
is owner-pending (ViewPoint_Tools T04) and its absence raises
ReplayUnavailableError.
"""

import re

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$")
_REPLAY_KEYS = {"seed", "input_tape_sha256", "determinism_mode", "sample_count", "controls"}
_MODES = ("exact", "seeded", "distribution", "uncontrolled")
_CONTROL_KEYS = {"clock", "rng", "external_effects"}
_CONTROL_VALUES = {"controlled", "uncontrolled", "not_applicable"}


class ReplayLineageError(ValueError):
    """Replay lineage refused before launch (mapped to SCHEMA_INVALID)."""


class ReplayUnavailableError(RuntimeError):
    """Replay retention is unavailable (mapped to AVAILABILITY_PARKED)."""


def validate_parent_episode_id(value) -> str:
    """Require a non-empty valid parent episode identifier."""
    if value is None:
        raise ReplayLineageError("behaviour.replay requires parent_episode_id")
    if type(value) is not str or len(value) < 1 or len(value) > 256 or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ReplayLineageError(f"invalid parent_episode_id: {value!r}")
    return value


def validate_replay_block(replay) -> dict:
    """Validate a replay block and return its normalized copy.

    Mirrors the receipt-level replay rules so a block accepted here is
    the block the retained receipt will carry.
    """
    if type(replay) is not dict or not replay:
        raise ReplayLineageError("replay requires a witnessed input tape")
    if set(replay.keys()) - _REPLAY_KEYS:
        raise ReplayLineageError("replay contains unsupported fields")
    if "input_tape_sha256" not in replay or "determinism_mode" not in replay:
        raise ReplayLineageError("replay requires input_tape_sha256 and determinism_mode")
    tape = replay["input_tape_sha256"]
    if type(tape) is not str or _SHA256_RE.fullmatch(tape) is None:
        raise ReplayLineageError("replay input_tape_sha256 must be a sha256")
    mode = replay["determinism_mode"]
    if type(mode) is not str or mode not in _MODES:
        raise ReplayLineageError(f"unsupported determinism_mode: {mode!r}")
    clean: dict = {"input_tape_sha256": tape, "determinism_mode": mode}
    if mode == "seeded":
        seed = replay.get("seed")
        if type(seed) is not int or seed < 0:
            raise ReplayLineageError("seeded replay requires a nonnegative integer seed")
        clean["seed"] = seed
    elif "seed" in replay:
        raise ReplayLineageError("seed is only valid for seeded replay")
    if mode == "distribution":
        count = replay.get("sample_count")
        if type(count) is not int or count < 1:
            raise ReplayLineageError("distribution replay requires sample_count >= 1")
        clean["sample_count"] = count
    elif "sample_count" in replay:
        raise ReplayLineageError("sample_count is only valid for distribution replay")
    if mode == "exact":
        controls = replay.get("controls")
        if not isinstance(controls, dict) or set(controls) - _CONTROL_KEYS or not _CONTROL_KEYS <= set(controls):
            raise ReplayLineageError("exact replay requires clock, rng and external_effects controls")
        for ctrl_key in ("clock", "rng", "external_effects"):
            value = controls[ctrl_key]
            if type(value) is not str or value not in _CONTROL_VALUES:
                raise ReplayLineageError(f"exact replay {ctrl_key} control is invalid")
        if controls.get("clock") not in ("controlled", "not_applicable") or controls.get("rng") not in ("controlled", "not_applicable"):
            raise ReplayLineageError("exact replay clock/rng must be controlled or not_applicable")
        clean["controls"] = dict(controls)
    elif "controls" in replay:
        raise ReplayLineageError("controls are only valid for exact replay")
    return clean


def _check_retained(parent: str, tape: str, tape_resolver) -> None:
    if not callable(tape_resolver):
        raise ReplayUnavailableError("replay retention resolver unavailable (owner-pending: T04)")
    try:
        record = tape_resolver(parent, tape)
    except (ReplayLineageError, ReplayUnavailableError):
        raise
    except Exception as exc:
        raise ReplayUnavailableError("replay retention lookup failed") from exc
    if record is None:
        raise ReplayLineageError("replay parent episode and input tape are not retained as a pair")
    if (type(record) is not dict or record.get("parent_episode_id") != parent
            or record.get("input_tape_sha256") != tape):
        raise ReplayLineageError("retention lookup did not confirm the requested parent episode and input tape")


def resolve_replay_lineage(payload: dict, *, tape_sha256: str | None = None, tape_resolver=None) -> dict:
    """Resolve and validate replay lineage before launch.

    Returns ``{"parent_episode_id", "tape_sha256", "replay"}`` where
    ``replay`` is the verified block to forward to ``run_bound``.
    Any missing or disagreeing lineage raises :class:`ReplayLineageError`.
    """
    if type(payload) is not dict:
        raise ReplayLineageError("payload must be an object")
    parent = validate_parent_episode_id(payload.get("parent_episode_id"))
    caller_tape = payload.get("tape_sha256")
    if tape_sha256 is not None and caller_tape is not None and tape_sha256 != caller_tape:
        raise ReplayLineageError("caller tape_sha256 disagrees with host tape_sha256")
    explicit_tape = tape_sha256 if tape_sha256 is not None else caller_tape
    replay_raw = payload.get("replay")
    if replay_raw is None:
        if explicit_tape is None:
            raise ReplayLineageError("replay requires tape_sha256 or replay.input_tape_sha256")
        if type(explicit_tape) is not str or _SHA256_RE.fullmatch(explicit_tape) is None:
            raise ReplayLineageError("tape_sha256 must be a sha256 hex digest")
        clean = validate_replay_block({"input_tape_sha256": explicit_tape, "determinism_mode": "uncontrolled"})
    else:
        if type(replay_raw) is not dict:
            raise ReplayLineageError("replay must be an object")
        inner_tape = replay_raw.get("input_tape_sha256")
        if explicit_tape is not None and inner_tape is not None and explicit_tape != inner_tape:
            raise ReplayLineageError("tape_sha256 disagrees with replay.input_tape_sha256")
        clean = validate_replay_block(replay_raw)
    effective_tape = clean["input_tape_sha256"]
    if explicit_tape is not None and explicit_tape != effective_tape:
        raise ReplayLineageError("tape_sha256 disagrees with replay.input_tape_sha256")
    _check_retained(parent, effective_tape, tape_resolver)
    return {"parent_episode_id": parent, "tape_sha256": effective_tape, "replay": clean}


__all__ = (
    "ReplayLineageError",
    "ReplayUnavailableError",
    "validate_parent_episode_id",
    "validate_replay_block",
    "resolve_replay_lineage",
)
