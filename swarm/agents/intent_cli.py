"""Deterministic Intent CLI & SDK for ViewPoint Swarm headless agents.

Enforces CONTRACT.md §5.1 intent schemas, §4 claim prefixes, and strict
capsule boundaries (read-only frames, atomic writes only to intents/<agent_id>/,
never writing to SWARM_INBOX, never calling vision).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from swarm.agents import (
    AGENT_SPECS,
    ALLOWED_GUI_KEYS,
    CONTRACT_VERSION,
    DEFAULT_EVENTS_LOG,
    DEFAULT_FRAMES_DIR,
    DEFAULT_GUI_FOOTPRINT,
    DEFAULT_INBOX_DIR,
    DEFAULT_INTENTS_DIR,
    DEFAULT_WAIT_TIMEOUT_S,
    STALE_MAX_DROPS,
    VERDICT_NO_VISIBLE_CHANGE,
    VERDICT_QUEUE_REFUSAL,
    VERDICT_RETRYABLE_STALE,
    VERDICT_SUCCESS,
)

EVENT_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
AGENT_ID_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,63}\Z")


class IntentValidationError(ValueError):
    """Raised when an intent or boundary check fails locally."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _has_control_or_whitespace(value: str) -> bool:
    return any(ord(ch) <= 32 or ord(ch) == 127 for ch in value)


def validate_event_id(event_id: Any) -> str:
    """Validate event_id with strict fullmatch, no '..' and no whitespace/control chars."""
    if (
        not isinstance(event_id, str)
        or not event_id
        or ".." in event_id
        or _has_control_or_whitespace(event_id)
        or not EVENT_ID_RE.fullmatch(event_id)
    ):
        raise IntentValidationError("VALIDATION_ERROR", f"Invalid event_id: {event_id!r}")
    return event_id


def validate_agent_id(agent_id: Any) -> str:
    """Validate agent_id with strict fullmatch, no '..' and no whitespace/control chars."""
    if (
        not isinstance(agent_id, str)
        or not agent_id
        or ".." in agent_id
        or _has_control_or_whitespace(agent_id)
        or not AGENT_ID_RE.fullmatch(agent_id)
    ):
        raise IntentValidationError("VALIDATION_ERROR", f"Invalid agent_id: {agent_id!r}")
    return agent_id


class WaitTimeoutError(TimeoutError):
    """Raised when wait-event exceeds --timeout-s (CLI exit code 124)."""


def resolve_events_log(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    env_val = os.environ.get("SWARM_EVENTS_LOG")
    return Path(env_val) if env_val else DEFAULT_EVENTS_LOG


def resolve_inbox_dir() -> Path:
    env_val = os.environ.get("SWARM_INBOX")
    return Path(env_val) if env_val else DEFAULT_INBOX_DIR


def assert_safe_intent_target(target_path: Path, intents_dir: Path) -> None:
    """Refuse any path targeting SWARM_INBOX or escaping intents_dir."""
    inbox_candidates = [DEFAULT_INBOX_DIR, resolve_inbox_dir()]
    resolved_target = Path(os.path.realpath(target_path))
    resolved_intents = Path(os.path.realpath(intents_dir))

    for inbox in inbox_candidates:
        resolved_inbox = Path(os.path.realpath(inbox))
        if resolved_intents == resolved_inbox or resolved_inbox in resolved_intents.parents:
            raise IntentValidationError(
                "BOUNDARY_VIOLATION",
                f"Refusing to write inside SWARM_INBOX ({resolved_inbox})",
            )
        if resolved_target == resolved_inbox or resolved_inbox in resolved_target.parents:
            raise IntentValidationError(
                "BOUNDARY_VIOLATION",
                f"Refusing to write target inside SWARM_INBOX ({resolved_inbox})",
            )

    if resolved_intents not in resolved_target.parents:
        raise IntentValidationError(
            "BOUNDARY_VIOLATION",
            f"Target {resolved_target} escapes intents_dir {resolved_intents}",
        )


def read_latest_frame(frames_dir: Path, wait_s: float = 10.0) -> dict[str, Any]:
    """Wait for and parse frames_dir/latest.json cleanly."""
    latest_path = Path(frames_dir) / "latest.json"
    deadline = time.monotonic() + max(0.0, wait_s)
    last_err: Exception | None = None
    required_int_keys = ("frame", "view_epoch", "scene_epoch")

    while True:
        if latest_path.is_file():
            try:
                raw = latest_path.read_text(encoding="utf-8")
                data = json.loads(raw)
                if isinstance(data, dict) and all(
                    k in data and isinstance(data[k], int) and not isinstance(data[k], bool)
                    for k in required_int_keys
                ):
                    return data
                last_err = ValueError(
                    "latest.json missing required integer keys (frame, view_epoch, scene_epoch)"
                )
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                last_err = exc
        if time.monotonic() >= deadline:
            detail = f" ({last_err})" if last_err else ""
            raise TimeoutError(
                f"Timed out waiting for valid latest.json at {latest_path}{detail}"
            )
        time.sleep(0.1)


def read_events_records(events_log: Path) -> list[dict[str, Any]]:
    """Read complete newline-terminated JSONL records from events_log (returns [] if absent)."""
    if not events_log.is_file():
        return []
    records: list[dict[str, Any]] = []
    with events_log.open("rb") as fh:
        for raw_line in fh:
            if not raw_line.endswith(b"\n"):
                continue
            stripped = raw_line.decode("utf-8", errors="replace").strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                records.append(obj)
    return records


def extract_agent_state(
    records: list[dict[str, Any]],
    agent_id: str,
    last_n: int = 5,
) -> dict[str, Any]:
    """Extract latest claim and last N close receipts for agent_id."""
    spec = AGENT_SPECS.get(agent_id, {})
    latest_claim: dict[str, Any] | None = None
    closes: list[dict[str, Any]] = []

    for rec in records:
        if rec.get("agent_id") != agent_id:
            continue
        kind = rec.get("rec")
        if kind == "claim":
            latest_claim = rec
        elif kind == "close":
            closes.append(rec)

    if latest_claim is not None:
        lease_epoch = int(latest_claim.get("lease_epoch", 1))
        write_prefixes = list(
            latest_claim.get("write_prefixes", spec.get("write_prefixes", []))
        )
        allow_gui = bool(latest_claim.get("allow_gui", spec.get("allow_gui", False)))
        lease_source = "events_log"
    else:
        lease_epoch = 1
        write_prefixes = list(spec.get("write_prefixes", []))
        allow_gui = bool(spec.get("allow_gui", False))
        lease_source = "default"

    return {
        "lease_epoch": lease_epoch,
        "lease_source": lease_source,
        "lease_id": latest_claim.get("lease_id") if latest_claim else None,
        "expires_ts_ms": latest_claim.get("expires_ts_ms") if latest_claim else None,
        "write_prefixes": write_prefixes,
        "allow_gui": allow_gui,
        "last_closes": closes[-max(0, last_n) :],
    }


def parse_int_quad(raw: str | None, name: str) -> list[int] | None:
    if raw is None:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) != 4:
        raise IntentValidationError(
            "VALIDATION_ERROR", f"{name} must be 4 comma-separated integers: x,y,w,h"
        )
    try:
        vals = [int(p) for p in parts]
    except ValueError as exc:
        raise IntentValidationError(
            "VALIDATION_ERROR", f"{name} contains non-integer value: {raw}"
        ) from exc
    if vals[2] <= 0 or vals[3] <= 0:
        raise IntentValidationError(
            "VALIDATION_ERROR", f"{name} width and height must be > 0: {vals}"
        )
    return vals


def parse_xy(raw: str | None) -> list[int] | None:
    if raw is None:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) != 2:
        raise IntentValidationError(
            "VALIDATION_ERROR", "cursor-xy must be 2 comma-separated integers: x,y"
        )
    try:
        return [int(parts[0]), int(parts[1])]
    except ValueError as exc:
        raise IntentValidationError(
            "VALIDATION_ERROR", f"cursor-xy contains non-integer value: {raw}"
        ) from exc


def validate_intent_dict(
    intent: dict[str, Any],
    *,
    write_prefixes: list[str] | None = None,
    allow_gui: bool | None = None,
) -> None:
    """Validate an intent payload against CONTRACT.md §5.1 and §4.2."""
    required_keys = (
        "v",
        "event_id",
        "agent_id",
        "lane",
        "action_class",
        "kind",
        "based_on_frame",
        "view_epoch",
        "scene_epoch",
        "lease_epoch",
        "write_set",
        "target_region",
        "footprint",
        "expect",
        "script",
        "script_sha256",
        "keys",
        "max_acts",
        "created_ts_ms",
    )
    for key in required_keys:
        if key not in intent:
            raise IntentValidationError("VALIDATION_ERROR", f"Missing required field: {key}")

    if intent["v"] != CONTRACT_VERSION:
        raise IntentValidationError("VALIDATION_ERROR", f"v must be {CONTRACT_VERSION}")

    agent_id = validate_agent_id(intent["agent_id"])
    event_id = validate_event_id(intent["event_id"])

    for int_field in ("based_on_frame", "view_epoch", "scene_epoch", "lease_epoch", "created_ts_ms"):
        if not isinstance(intent[int_field], int) or isinstance(intent[int_field], bool):
            raise IntentValidationError("VALIDATION_ERROR", f"{int_field} must be int")

    if not isinstance(intent["expect"], str) or not intent["expect"].strip():
        raise IntentValidationError("VALIDATION_ERROR", "expect must be a non-empty string")

    lane = intent["lane"]
    if lane == "script":
        if intent["action_class"] != "state_job" or intent["kind"] != "job":
            raise IntentValidationError(
                "VALIDATION_ERROR", "script lane requires action_class='state_job', kind='job'"
            )
        write_set = intent["write_set"]
        if not isinstance(write_set, list) or not write_set or not all(
            isinstance(w, str) and w for w in write_set
        ):
            raise IntentValidationError(
                "CLAIM_REFUSED", "script lane requires a non-empty list[str] write_set"
            )
        prefixes = (
            write_prefixes
            if write_prefixes is not None
            else AGENT_SPECS.get(agent_id, {}).get("write_prefixes", [])
        )
        if not prefixes:
            raise IntentValidationError(
                "NO_LEASE", f"No write_prefixes configured for agent {agent_id}"
            )
        for entry in write_set:
            if not any(entry.startswith(p) for p in prefixes):
                raise IntentValidationError(
                    "CLAIM_REFUSED",
                    f"write_set entry {entry!r} violates prefixes {prefixes}",
                )
        script = intent["script"]
        if not isinstance(script, str) or not script.strip():
            raise IntentValidationError("VALIDATION_ERROR", "script must be non-empty str")
        expected_sha = hashlib.sha256(script.encode("utf-8")).hexdigest()
        if intent["script_sha256"] != expected_sha:
            raise IntentValidationError("VALIDATION_ERROR", "script_sha256 mismatch")
        if intent["keys"] is not None or intent["max_acts"] != 1:
            raise IntentValidationError(
                "VALIDATION_ERROR", "script lane requires keys=null and max_acts=1"
            )
    elif lane == "gui":
        gui_ok = (
            allow_gui
            if allow_gui is not None
            else bool(AGENT_SPECS.get(agent_id, {}).get("allow_gui", False))
        )
        if not gui_ok or agent_id != "camera-agent":
            raise IntentValidationError(
                "CLAIM_REFUSED",
                f"GUI lane is permitted only for camera-agent (got {agent_id!r})",
            )
        if intent["action_class"] != "view_change" or intent["kind"] != "key":
            raise IntentValidationError(
                "VALIDATION_ERROR", "gui key lane requires action_class='view_change', kind='key'"
            )
        footprint = intent["footprint"]
        if (
            not isinstance(footprint, list)
            or len(footprint) != 4
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in footprint)
            or footprint[2] <= 0
            or footprint[3] <= 0
        ):
            raise IntentValidationError(
                "FOOTPRINT_MISSING",
                "gui lane requires footprint=[x,y,w,h] with w>0, h>0",
            )
        keys = intent["keys"]
        if not isinstance(keys, list) or not (1 <= len(keys) <= 8):
            raise IntentValidationError(
                "CLAIM_REFUSED", "gui keys must contain between 1 and 8 keys"
            )
        bad_keys = [k for k in keys if k not in ALLOWED_GUI_KEYS]
        if bad_keys:
            raise IntentValidationError(
                "CLAIM_REFUSED",
                f"Disallowed GUI key(s): {bad_keys}; allowed: {list(ALLOWED_GUI_KEYS)}",
            )
        if intent["max_acts"] != len(keys):
            raise IntentValidationError(
                "VALIDATION_ERROR", "gui max_acts must equal len(keys)"
            )
        if intent["script"] is not None or intent["script_sha256"] is not None:
            raise IntentValidationError(
                "VALIDATION_ERROR", "gui lane requires script=null and script_sha256=null"
            )
    else:
        raise IntentValidationError("VALIDATION_ERROR", f"Unsupported lane: {lane!r}")


def _fsync_dir(directory: Path) -> None:
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _atomic_write_json_file(target_path: Path, payload: dict[str, Any]) -> Path:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f".{target_path.name}.tmp.{os.getpid()}")
    serialized = json.dumps(payload, indent=2, sort_keys=False) + "\n"
    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            fh.write(serialized)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target_path)
        _fsync_dir(target_path.parent)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise
    return target_path


def atomic_write_json(target_path: Path, payload: dict[str, Any], intents_dir: Path) -> Path:
    """Write JSON atomically inside intents_dir/<agent_id>/<event_id>.json."""
    assert_safe_intent_target(target_path, intents_dir)
    if target_path.exists():
        raise IntentValidationError(
            "DUPLICATE_EVENT", f"Intent already exists at {target_path}"
        )
    return _atomic_write_json_file(target_path, payload)


def assert_agent_identity(agent_id: str) -> None:
    """Enforce SWARM_AGENT_ID binding when runner exports an agent identity."""
    bound_agent = os.environ.get("SWARM_AGENT_ID")
    if bound_agent and bound_agent != agent_id:
        raise IntentValidationError(
            "CLAIM_REFUSED",
            f"Process is bound to SWARM_AGENT_ID={bound_agent!r}; cannot act as {agent_id!r}",
        )


def offset_metadata_path(intents_dir: Path, agent_id: str, event_id: str) -> Path:
    return Path(intents_dir) / agent_id / ".offsets" / f"{event_id}.json"


def record_enqueue_offset(
    *,
    intents_dir: Path,
    agent_id: str,
    event_id: str,
    events_log: Path,
    created_ts_ms: int,
    script_sha256: str | None,
) -> int:
    """Snapshot events_log byte size and persist it under <intents_dir>/<agent_id>/.offsets/."""
    meta_path = offset_metadata_path(intents_dir, agent_id, event_id)
    assert_safe_intent_target(meta_path, intents_dir)
    offset_bytes = events_log.stat().st_size if events_log.is_file() else 0
    _atomic_write_json_file(
        meta_path,
        {
            "offset_bytes": offset_bytes,
            "created_ts_ms": int(created_ts_ms),
            "script_sha256": script_sha256,
        },
    )
    return offset_bytes


def load_enqueue_offset(
    *,
    event_id: str,
    intents_dir: Path | None = None,
    agent_id: str | None = None,
) -> int | None:
    """Load recorded events.jsonl byte offset for event_id if present."""
    root = Path(intents_dir) if intents_dir is not None else DEFAULT_INTENTS_DIR
    candidates: list[Path] = []
    if agent_id:
        candidates.append(offset_metadata_path(root, agent_id, event_id))
    else:
        bound = os.environ.get("SWARM_AGENT_ID")
        if bound:
            candidates.append(offset_metadata_path(root, bound, event_id))
        if root.is_dir():
            candidates.extend(sorted(root.glob(f"*/.offsets/{event_id}.json")))

    best_offset: int | None = None
    best_ts = -1
    seen: set[Path] = set()
    for cand in candidates:
        if cand in seen or not cand.is_file():
            continue
        seen.add(cand)
        try:
            data = json.loads(cand.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("offset_bytes"), int):
            ts_ms = int(data.get("created_ts_ms", 0))
            if best_offset is None or ts_ms >= best_ts:
                best_ts = ts_ms
                best_offset = max(0, int(data["offset_bytes"]))
    return best_offset


def build_script_intent(
    *,
    agent_id: str,
    event_id: str,
    write_set: list[str],
    expect: str,
    script_source: str,
    frames_dir: Path,
    events_log: Path,
    lease_epoch: int | None = None,
    target_region: list[int] | None = None,
    footprint: list[int] | None = None,
    cursor_xy: list[int] | None = None,
    wait_s: float = 10.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_agent_id(agent_id)
    validate_event_id(event_id)
    frame_meta = read_latest_frame(frames_dir, wait_s=wait_s)
    records = read_events_records(events_log)
    state = extract_agent_state(records, agent_id)
    effective_lease = lease_epoch if lease_epoch is not None else state["lease_epoch"]
    if cursor_xy is None:
        cursor_xy = AGENT_SPECS.get(agent_id, {}).get("cursor_xy")

    script_sha256 = hashlib.sha256(script_source.encode("utf-8")).hexdigest()
    intent: dict[str, Any] = {
        "v": CONTRACT_VERSION,
        "event_id": event_id,
        "agent_id": agent_id,
        "lane": "script",
        "action_class": "state_job",
        "kind": "job",
        "based_on_frame": int(frame_meta["frame"]),
        "view_epoch": int(frame_meta["view_epoch"]),
        "scene_epoch": int(frame_meta["scene_epoch"]),
        "lease_epoch": int(effective_lease),
        "write_set": write_set,
        "target_region": target_region,
        "footprint": footprint,
        "expect": expect,
        "script": script_source,
        "script_sha256": script_sha256,
        "keys": None,
        "max_acts": 1,
        "cursor_xy": cursor_xy,
        "created_ts_ms": int(time.time() * 1000),
    }
    validate_intent_dict(
        intent,
        write_prefixes=state["write_prefixes"],
        allow_gui=state["allow_gui"],
    )
    return intent, frame_meta


def build_gui_intent(
    *,
    agent_id: str,
    event_id: str,
    keys: list[str],
    footprint: list[int],
    expect: str,
    frames_dir: Path,
    events_log: Path,
    lease_epoch: int | None = None,
    target_region: list[int] | None = None,
    cursor_xy: list[int] | None = None,
    wait_s: float = 10.0,
    based_on_frame_override: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_agent_id(agent_id)
    validate_event_id(event_id)
    frame_meta = read_latest_frame(frames_dir, wait_s=wait_s)
    records = read_events_records(events_log)
    state = extract_agent_state(records, agent_id)
    effective_lease = lease_epoch if lease_epoch is not None else state["lease_epoch"]
    if cursor_xy is None:
        cursor_xy = AGENT_SPECS.get(agent_id, {}).get("cursor_xy")
    based_on_frame = (
        int(based_on_frame_override)
        if based_on_frame_override is not None
        else int(frame_meta["frame"])
    )

    intent: dict[str, Any] = {
        "v": CONTRACT_VERSION,
        "event_id": event_id,
        "agent_id": agent_id,
        "lane": "gui",
        "action_class": "view_change",
        "kind": "key",
        "based_on_frame": based_on_frame,
        "view_epoch": int(frame_meta["view_epoch"]),
        "scene_epoch": int(frame_meta["scene_epoch"]),
        "lease_epoch": int(effective_lease),
        "write_set": [],
        "target_region": target_region,
        "footprint": footprint,
        "expect": expect,
        "script": None,
        "script_sha256": None,
        "keys": keys,
        "max_acts": len(keys),
        "cursor_xy": cursor_xy,
        "created_ts_ms": int(time.time() * 1000),
    }
    validate_intent_dict(
        intent,
        write_prefixes=state["write_prefixes"],
        allow_gui=state["allow_gui"],
    )
    return intent, frame_meta


def observe_state(
    *,
    frames_dir: Path,
    events_log: Path,
    agent_id: str,
    wait_s: float = 30.0,
    last_n: int = 5,
) -> dict[str, Any]:
    validate_agent_id(agent_id)
    frame_meta = read_latest_frame(frames_dir, wait_s=wait_s)
    frame_num = int(frame_meta["frame"])
    records = read_events_records(events_log)
    agent_state = extract_agent_state(records, agent_id, last_n=last_n)
    return {
        "agent_id": agent_id,
        "frame": frame_num,
        "frame_png": str(Path(frames_dir) / f"frame_{frame_num}.png"),
        "frame_json": str(Path(frames_dir) / f"frame_{frame_num}.json"),
        "view_epoch": int(frame_meta["view_epoch"]),
        "scene_epoch": int(frame_meta["scene_epoch"]),
        "changed_regions": frame_meta.get("changed_regions", []),
        "ts_ms": frame_meta.get("ts_ms"),
        "lease_epoch": agent_state["lease_epoch"],
        "lease_source": agent_state["lease_source"],
        "lease_id": agent_state["lease_id"],
        "write_prefixes": agent_state["write_prefixes"],
        "allow_gui": agent_state["allow_gui"],
        "last_closes": agent_state["last_closes"],
    }


def wait_for_close_event(
    *,
    event_id: str,
    events_log: Path,
    timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
    poll_interval_s: float = 0.2,
    from_offset: int | None = None,
    intents_dir: Path | None = None,
    agent_id: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Tail events_log from >= offset_bytes until a terminal close receipt for event_id."""
    validate_event_id(event_id)
    if agent_id is not None:
        validate_agent_id(agent_id)
    if from_offset is not None:
        start_offset = max(0, int(from_offset))
    else:
        loaded_offset = load_enqueue_offset(
            event_id=event_id,
            intents_dir=intents_dir,
            agent_id=agent_id,
        )
        start_offset = loaded_offset if loaded_offset is not None else 0

    deadline = time.monotonic() + max(0.0, timeout_s)
    offset = start_offset

    while True:
        if events_log.is_file():
            file_size = events_log.stat().st_size
            if file_size > offset:
                with events_log.open("rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                if chunk:
                    last_nl = chunk.rfind(b"\n")
                    if last_nl != -1:
                        complete_bytes = chunk[: last_nl + 1]
                        offset += len(complete_bytes)
                        for raw_line in complete_bytes.split(b"\n"):
                            stripped = raw_line.decode("utf-8", errors="replace").strip()
                            if not stripped:
                                continue
                            try:
                                rec = json.loads(stripped)
                            except json.JSONDecodeError:
                                continue
                            if (
                                isinstance(rec, dict)
                                and rec.get("rec") == "close"
                                and rec.get("event_id") == event_id
                            ):
                                verdict = str(rec.get("verdict", ""))
                                if verdict in VERDICT_SUCCESS:
                                    return 0, rec
                                if verdict in VERDICT_NO_VISIBLE_CHANGE:
                                    return 2, rec
                                if verdict in VERDICT_RETRYABLE_STALE:
                                    drops = int(rec.get("drops", 0))
                                    if drops >= STALE_MAX_DROPS:
                                        return 3, rec
                                    continue
                                if verdict in VERDICT_QUEUE_REFUSAL:
                                    return 4, rec
                                return 1, rec
        if time.monotonic() >= deadline:
            raise WaitTimeoutError(
                f"Timed out after {timeout_s}s waiting for close receipt of {event_id!r}"
            )
        time.sleep(poll_interval_s)


def _cmd_observe(args: argparse.Namespace) -> int:
    payload = observe_state(
        frames_dir=Path(args.frames_dir),
        events_log=resolve_events_log(args.events_log),
        agent_id=args.agent,
        wait_s=args.wait_s,
        last_n=args.last_n,
    )
    print(json.dumps(payload, indent=2))
    return 0


def _cmd_enqueue_script(args: argparse.Namespace) -> int:
    validate_agent_id(args.agent)
    validate_event_id(args.event_id)
    assert_agent_identity(args.agent)
    script_path = Path(args.script_file)
    if not script_path.is_file():
        raise IntentValidationError("VALIDATION_ERROR", f"Script file not found: {script_path}")
    script_source = script_path.read_text(encoding="utf-8")
    write_set = [w.strip() for w in args.write_set.split(",") if w.strip()]
    intents_dir = Path(args.intents_dir)
    events_log = resolve_events_log(args.events_log)
    target_path = intents_dir / args.agent / f"{args.event_id}.json"

    assert_safe_intent_target(target_path, intents_dir)
    intent, _ = build_script_intent(
        agent_id=args.agent,
        event_id=args.event_id,
        write_set=write_set,
        expect=args.expect,
        script_source=script_source,
        frames_dir=Path(args.frames_dir),
        events_log=events_log,
        lease_epoch=args.lease_epoch,
        target_region=parse_int_quad(args.target_region, "target-region"),
        footprint=parse_int_quad(args.footprint, "footprint"),
        cursor_xy=parse_xy(args.cursor_xy),
        wait_s=args.wait_s,
    )
    if target_path.exists():
        raise IntentValidationError(
            "DUPLICATE_EVENT", f"Intent already exists at {target_path}"
        )
    offset_bytes = record_enqueue_offset(
        intents_dir=intents_dir,
        agent_id=args.agent,
        event_id=intent["event_id"],
        events_log=events_log,
        created_ts_ms=intent["created_ts_ms"],
        script_sha256=intent["script_sha256"],
    )
    atomic_write_json(target_path, intent, intents_dir)
    print(
        json.dumps(
            {
                "status": "ENQUEUED",
                "intent_path": str(target_path),
                "event_id": intent["event_id"],
                "agent_id": intent["agent_id"],
                "lane": intent["lane"],
                "based_on_frame": intent["based_on_frame"],
                "lease_epoch": intent["lease_epoch"],
                "script_sha256": intent["script_sha256"],
                "events_log_offset": offset_bytes,
            },
            indent=2,
        )
    )
    return 0


def _cmd_enqueue_gui(args: argparse.Namespace) -> int:
    validate_agent_id(args.agent)
    validate_event_id(args.event_id)
    assert_agent_identity(args.agent)
    keys = [k.strip() for k in args.keys.split(",") if k.strip()]
    footprint = parse_int_quad(args.footprint, "footprint")
    assert footprint is not None
    intents_dir = Path(args.intents_dir)
    events_log = resolve_events_log(args.events_log)
    target_path = intents_dir / args.agent / f"{args.event_id}.json"

    assert_safe_intent_target(target_path, intents_dir)
    intent, _ = build_gui_intent(
        agent_id=args.agent,
        event_id=args.event_id,
        keys=keys,
        footprint=footprint,
        expect=args.expect,
        frames_dir=Path(args.frames_dir),
        events_log=events_log,
        lease_epoch=args.lease_epoch,
        target_region=parse_int_quad(args.target_region, "target-region"),
        cursor_xy=parse_xy(args.cursor_xy),
        wait_s=args.wait_s,
        based_on_frame_override=args.based_on_frame,
    )
    if target_path.exists():
        raise IntentValidationError(
            "DUPLICATE_EVENT", f"Intent already exists at {target_path}"
        )
    offset_bytes = record_enqueue_offset(
        intents_dir=intents_dir,
        agent_id=args.agent,
        event_id=intent["event_id"],
        events_log=events_log,
        created_ts_ms=intent["created_ts_ms"],
        script_sha256=intent["script_sha256"],
    )
    atomic_write_json(target_path, intent, intents_dir)
    print(
        json.dumps(
            {
                "status": "ENQUEUED",
                "intent_path": str(target_path),
                "event_id": intent["event_id"],
                "agent_id": intent["agent_id"],
                "lane": intent["lane"],
                "based_on_frame": intent["based_on_frame"],
                "lease_epoch": intent["lease_epoch"],
                "keys": intent["keys"],
                "footprint": intent["footprint"],
                "events_log_offset": offset_bytes,
            },
            indent=2,
        )
    )
    return 0


def _cmd_wait_event(args: argparse.Namespace) -> int:
    rc, receipt = wait_for_close_event(
        event_id=args.event_id,
        events_log=resolve_events_log(args.events_log),
        timeout_s=args.timeout_s,
        poll_interval_s=args.poll_interval_s,
        from_offset=args.from_offset,
        intents_dir=Path(args.intents_dir),
        agent_id=args.agent,
    )
    print(json.dumps(receipt, indent=2))
    return rc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m swarm.agents.intent_cli",
        description="Deterministic Intent CLI for ViewPoint Swarm agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_obs = sub.add_parser("observe", help="Read latest.json and agent lease/close state")
    p_obs.add_argument("--frames-dir", default=str(DEFAULT_FRAMES_DIR))
    p_obs.add_argument("--events-log", default=None)
    p_obs.add_argument("--agent", required=True)
    p_obs.add_argument("--wait-s", type=float, default=30.0)
    p_obs.add_argument("--last-n", type=int, default=5)
    p_obs.set_defaults(func=_cmd_observe)

    p_scr = sub.add_parser("enqueue-script", help="Validate and enqueue a script intent")
    p_scr.add_argument("--agent", required=True)
    p_scr.add_argument("--event-id", required=True)
    p_scr.add_argument("--write-set", required=True)
    p_scr.add_argument("--expect", required=True)
    p_scr.add_argument("--script-file", required=True)
    p_scr.add_argument("--intents-dir", default=str(DEFAULT_INTENTS_DIR))
    p_scr.add_argument("--frames-dir", default=str(DEFAULT_FRAMES_DIR))
    p_scr.add_argument("--events-log", default=None)
    p_scr.add_argument("--lease-epoch", type=int, default=None)
    p_scr.add_argument("--target-region", default=None)
    p_scr.add_argument("--footprint", default=None)
    p_scr.add_argument("--cursor-xy", default=None)
    p_scr.add_argument("--wait-s", type=float, default=10.0)
    p_scr.set_defaults(func=_cmd_enqueue_script)

    p_gui = sub.add_parser("enqueue-gui", help="Validate and enqueue a camera-agent GUI intent")
    p_gui.add_argument("--agent", default="camera-agent")
    p_gui.add_argument("--event-id", required=True)
    p_gui.add_argument("--keys", required=True)
    p_gui.add_argument(
        "--footprint",
        default=",".join(str(x) for x in DEFAULT_GUI_FOOTPRINT),
    )
    p_gui.add_argument("--expect", required=True)
    p_gui.add_argument("--intents-dir", default=str(DEFAULT_INTENTS_DIR))
    p_gui.add_argument("--frames-dir", default=str(DEFAULT_FRAMES_DIR))
    p_gui.add_argument("--events-log", default=None)
    p_gui.add_argument("--lease-epoch", type=int, default=None)
    p_gui.add_argument("--target-region", default=None)
    p_gui.add_argument("--cursor-xy", default=None)
    p_gui.add_argument("--wait-s", type=float, default=10.0)
    p_gui.add_argument("--based-on-frame", type=int, default=None)
    p_gui.set_defaults(func=_cmd_enqueue_gui)

    p_wait = sub.add_parser("wait-event", help="Tail events.jsonl until close receipt")
    p_wait.add_argument("--event-id", required=True)
    p_wait.add_argument("--events-log", default=None)
    p_wait.add_argument("--timeout-s", type=float, default=DEFAULT_WAIT_TIMEOUT_S)
    p_wait.add_argument("--poll-interval-s", type=float, default=0.2)
    p_wait.add_argument("--from-offset", type=int, default=None)
    p_wait.add_argument("--intents-dir", default=str(DEFAULT_INTENTS_DIR))
    p_wait.add_argument("--agent", default=None)
    p_wait.set_defaults(func=_cmd_wait_event)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except IntentValidationError as exc:
        print(json.dumps({"error": exc.code, "detail": exc.message}), file=sys.stderr)
        return 1
    except WaitTimeoutError as exc:
        print(json.dumps({"error": "TIMEOUT", "detail": str(exc)}), file=sys.stderr)
        return 124
    except TimeoutError as exc:
        print(json.dumps({"error": "TIMEOUT", "detail": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
