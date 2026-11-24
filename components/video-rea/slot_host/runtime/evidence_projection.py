# SPDX-License-Identifier: AGPL-3.0-only
"""Projection of REA slot-host evidence onto frozen Tools records.

Emits ``attempt_evidence_record/v0.3`` records (external launch
``started``/``observed``/``aborted_before_spawn`` and host-compare
``started``/``observed``) plus ``rea.retention_index/v1`` entries,
offline and fail-closed.  v0.2 remains frozen and supported: this
module never rewrites a v0.2 record.

Authentication and live custody are not established here: the
authenticated retention service (T01), attribution witnesses (T03),
live readback (T04), recovery service (T05) and the vision mapping
grant (T06) remain denying seams.  Builders raise ``ValueError`` for
any input they cannot represent truthfully instead of fabricating
identity, custody or release facts.

Record digests use the Tools convention: ``sha256`` over the sorted
JSON encoding with a trailing newline (``record_bytes``).  The
command digest (``command_sha256``) binds the exact command object;
``start_sha256`` binds the started record; ``execution_sha256`` on
artifact and cleanup binds the execution object.
"""

import hashlib
import json
import re
from collections import Counter
from fractions import Fraction

V02_REF = "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2"
V02_ID = "https://metaviewer.local/contracts/v0.2/attempt_evidence_record.schema.json"
V02_SHA = "b3bfd6715abfa60a0866b655d23088d8bee09b0cf8e0a3b44c156e5692c19d8c"
V03_REF = "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.3"
V03_ID = "https://metaviewer.local/contracts/v0.3/attempt_evidence_record.schema.json"
V03_SHA = "9402c78a4780cab53d9ad9726b103d05e76b504fe693c16b572078c9d8c98dc7"
INDEX_SCHEMA = "rea.retention_index/v1"
INDEX_SHA = "9c4113e4981e0b9356e642534cc3fc9684ab03a51265ebdfe5e934f8207e98ad"
PROFILE_SCHEMA_V1 = "rea.slot_evidence_profile/v1"

RECORD_LIMIT = 33_554_432
ARTIFACT_LIMIT = 65_536
_MAX_SAFE_INTEGER = 2**53 - 1

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$")
_SLOT_ID_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ATTEMPT_RE = re.compile(r"^[0-9a-f]{32}$")
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
# Same host, port and suffix grammar as the frozen registered_slot definition.
_REGISTERED_SOURCE_URL = re.compile(
    rf"(?ai:https://(?!(?:{_HOST_LABEL}\.)*invalid\.?(?=[:/?#]|\Z))"
    rf"(?:{_HOST_LABEL}(?:\.{_HOST_LABEL})*\.?|\[[0-9a-f:.]+\])(?::[0-9]{{1,5}})?)"
    r"(?:[/?#][^\s\x00-\x1f\x7f]*)?"
)


def _check_id(value, field):
    if type(value) is not str or not (1 <= len(value) <= 256) or _ID_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {field}: {value!r}")


def _check_sha(value, field):
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {field}: {value!r}")


def _check_integer(value, field, minimum=0):
    if type(value) is not int or not minimum <= value <= _MAX_SAFE_INTEGER:
        raise ValueError(f"{field} must be an integer between {minimum} and {_MAX_SAFE_INTEGER}")


def is_registered_source_url(value):
    return type(value) is str and len(value) <= 2048 and _REGISTERED_SOURCE_URL.fullmatch(value) is not None


def _check_attempt(value, field="attempt_id"):
    if type(value) is not str or _ATTEMPT_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {field}: {value!r}")


def record_bytes(value) -> bytes:
    return (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def record_digest(value) -> str:
    return hashlib.sha256(record_bytes(value)).hexdigest()


def jcs_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def jcs_digest(value) -> str:
    return hashlib.sha256(jcs_bytes(value)).hexdigest()


def v03_attempt_id(rea_attempt_id: str) -> str:
    """Deterministic 32-hex attempt id for a REA attempt identifier."""
    if type(rea_attempt_id) is not str or not rea_attempt_id:
        raise ValueError("rea_attempt_id must be a nonempty string")
    return hashlib.sha256(("rea/attempt/v0.3:" + rea_attempt_id).encode("utf-8")).hexdigest()[:32]


def wire_attempt_id(rea_attempt_id: str, *, retained: bool) -> str:
    if retained:
        _check_attempt(rea_attempt_id)
        return rea_attempt_id
    return v03_attempt_id(rea_attempt_id)


def request_sha256(request_id: str, rea_attempt_id: str) -> str:
    _check_id(request_id, "request_id")
    return record_digest({"attempt_id": rea_attempt_id, "request_id": request_id})


def env_sha256(env: dict) -> str:
    if not isinstance(env, dict) or any(type(k) is not str or type(v) is not str for k, v in env.items()):
        raise ValueError("env must be a string map")
    return jcs_digest(env)


def _project_slot(slot: dict) -> dict:
    """REA ``slot`` group -> v0.3 ``slot`` (registered_slot needs https)."""
    if not isinstance(slot, dict):
        raise ValueError("slot must be an object")
    tool_id = slot.get("tool_id")
    _check_id(tool_id, "slot.tool_id")
    version = slot.get("tool_version")
    if type(version) is not str or not (1 <= len(version) <= 128):
        raise ValueError("slot.tool_version must be a string of 1-128 chars")
    licence = slot.get("upstream_licence_id")
    if type(licence) is not str or not (1 <= len(licence) <= 128):
        raise ValueError("slot.upstream_licence_id must be a string of 1-128 chars")
    source_url = slot.get("source_url")
    if not is_registered_source_url(source_url):
        raise ValueError("slot.source_url must be a registered HTTPS URL")
    out = {
        "tool_id": tool_id,
        "version": version,
        "upstream_license_id": licence,
        "source_url": source_url,
        "user_installed": slot.get("user_installed", True) is True,
        "out_of_process": slot.get("out_of_process", True) is True,
        "use_disposition": slot.get("use_disposition", "external_execution"),
    }
    if out["use_disposition"] not in ("external_execution", "internal_research"):
        raise ValueError("slot.use_disposition is not an admitted disposition")
    for key in ("provider_id", "provider_revision", "implementation_sha256", "manifest_sha256", "adapter_sha256"):
        value = slot.get(key)
        if value is not None:
            if key == "provider_id":
                _check_id(value, f"slot.{key}")
            elif key == "provider_revision":
                if type(value) is not str or not (1 <= len(value) <= 128):
                    raise ValueError("slot.provider_revision must be a string of 1-128 chars")
            else:
                _check_sha(value, f"slot.{key}")
            out[key] = value
    return out


def _project_replay(replay: dict | None) -> dict | None:
    if replay is None:
        return None
    if not isinstance(replay, dict):
        raise ValueError("replay must be an object")
    tape = replay.get("input_tape_sha256")
    mode = replay.get("determinism_mode")
    _check_sha(tape, "replay.input_tape_sha256")
    if mode not in ("exact", "seeded", "distribution", "uncontrolled"):
        raise ValueError(f"unsupported determinism_mode: {mode!r}")
    out = {"input_tape_sha256": tape, "determinism_mode": mode}
    if mode == "seeded":
        seed = replay.get("seed")
        _check_integer(seed, "replay.seed")
        out["seed"] = seed
    elif "seed" in replay:
        raise ValueError("seed is only valid for seeded replay")
    if mode == "distribution":
        count = replay.get("sample_count")
        _check_integer(count, "replay.sample_count", 1)
        out["sample_count"] = count
    if "controls" in replay:
        controls = replay["controls"]
        if not isinstance(controls, dict) or set(controls) - {"clock", "rng", "external_effects"} or not {"clock", "rng", "external_effects"} <= set(controls):
            raise ValueError("replay controls must be complete")
        for key in ("clock", "rng", "external_effects"):
            if controls.get(key) not in ("controlled", "uncontrolled", "not_applicable"):
                raise ValueError(f"replay control {key} is invalid")
        out["controls"] = dict(controls)
    return out


def _check_event(event) -> None:
    if not isinstance(event, dict):
        raise ValueError("event must be an object")
    if set(event) - {"event_id", "t_ms", "kind", "input_digest", "occurrence", "t_ticks"}:
        raise ValueError("event has unsupported fields")
    _check_id(event.get("event_id"), "event.event_id")
    _check_integer(event.get("t_ms"), "event.t_ms")
    kind = event.get("kind")
    if type(kind) is not str or not (1 <= len(kind) <= 128):
        raise ValueError("event.kind must be a string of 1-128 chars")
    _check_sha(event.get("input_digest"), "event.input_digest")
    for key in ("occurrence", "t_ticks"):
        if key in event:
            _check_integer(event[key], f"event.{key}")


def _check_time_base(value, field):
    if not isinstance(value, dict) or set(value) - {"num", "den"} or not {"num", "den"} <= set(value):
        raise ValueError(f"{field} must be a time_base object")
    for key in ("num", "den"):
        _check_integer(value[key], f"{field}.{key}", 1)


def _check_timeline_side(side, field) -> bytes:
    """Validate one timeline side; returns the bound event bytes."""
    if not isinstance(side, dict):
        raise ValueError(f"{field} must be an object")
    if "events_object_ref" in side:
        raise ValueError(f"{field}.events_object_ref requires an object-index terminal")
    if set(side) - {"clock_source", "origin", "canonicalization", "timeline_sha256", "events", "time_base"}:
        raise ValueError(f"{field} has unsupported fields")
    if side.get("clock_source") not in ("pts", "wall", "monotonic"):
        raise ValueError(f"{field}.clock_source is invalid")
    if side.get("origin") not in ("episode_start", "recording_start", "first_input"):
        raise ValueError(f"{field}.origin is invalid")
    if side.get("canonicalization") != "rea.timeline_digest/v1":
        raise ValueError(f"{field}.canonicalization is invalid")
    _check_sha(side.get("timeline_sha256"), f"{field}.timeline_sha256")
    if "time_base" in side:
        _check_time_base(side["time_base"], f"{field}.time_base")
    events = side.get("events")
    if not isinstance(events, list) or len(events) > 1024:
        raise ValueError(f"{field}.events must be a list of at most 1024 events")
    base = side.get("time_base")
    ordinals = Counter()
    seen = set()
    previous = None
    for event in events:
        _check_event(event)
        event_id = event["event_id"]
        if event_id in seen:
            raise ValueError(f"{field} has duplicate event_id")
        seen.add(event_id)
        group = (event["kind"], event["input_digest"])
        if event.get("occurrence", ordinals[group]) != ordinals[group]:
            raise ValueError(f"{field} has an incoherent occurrence")
        ordinals[group] += 1
        source_ms = event["t_ms"]
        if "t_ticks" in event:
            if base is None:
                raise ValueError(f"{field} with t_ticks requires time_base")
            source_ms = Fraction(event["t_ticks"] * base["num"] * 1000, base["den"])
            if event["t_ms"] != source_ms // 1:
                raise ValueError(f"{field} event t_ms does not match t_ticks projection")
        if previous is not None and previous > source_ms:
            raise ValueError(f"{field} events out of source order")
        previous = source_ms
    raw = jcs_bytes(events)
    if hashlib.sha256(raw).hexdigest() != side["timeline_sha256"]:
        raise ValueError(f"{field} timeline digest mismatch")
    return raw


def _check_media_side(side, field) -> None:
    if not isinstance(side, dict):
        raise ValueError(f"{field} must be an object")
    if set(side) - {"recording_sha256", "container", "codec", "clock_source", "frames", "frames_unavailable_reason", "time_base"}:
        raise ValueError(f"{field} has unsupported fields")
    _check_sha(side.get("recording_sha256"), f"{field}.recording_sha256")
    for key in ("container", "codec"):
        if type(side.get(key)) is not str or not (1 <= len(side[key]) <= 64):
            raise ValueError(f"{field}.{key} must be a string of 1-64 chars")
    if side.get("clock_source") not in ("pts", "wall", "monotonic"):
        raise ValueError(f"{field}.clock_source is invalid")
    frames = side.get("frames")
    if not isinstance(frames, list) or len(frames) > 1024:
        raise ValueError(f"{field}.frames must be a list of at most 1024 frames")
    for frame in frames:
        if not isinstance(frame, dict) or set(frame) - {"frame_index", "pts_ms", "decoded_frame_sha256", "pts_ticks", "frame_file_sha256"}:
            raise ValueError(f"{field} frame has unsupported fields")
        for key in ("frame_index", "pts_ms"):
            _check_integer(frame.get(key), f"{field}.{key}")
        _check_sha(frame.get("decoded_frame_sha256"), f"{field} decoded_frame_sha256")
        if "pts_ticks" in frame:
            _check_integer(frame["pts_ticks"], f"{field}.pts_ticks")
        if "frame_file_sha256" in frame:
            _check_sha(frame["frame_file_sha256"], f"{field} frame_file_sha256")
    if not frames:
        reason = side.get("frames_unavailable_reason")
        if type(reason) is not str or not 1 <= len(reason) <= 2000:
            raise ValueError(f"{field} empty frames require a 1-2000 character frames_unavailable_reason")
    if frames and "frames_unavailable_reason" in side:
        raise ValueError(f"{field} frames_unavailable_reason only with empty frames")
    if "time_base" in side:
        _check_time_base(side["time_base"], f"{field}.time_base")
    if any("pts_ticks" in frame for frame in frames) and "time_base" not in side:
        raise ValueError(f"{field} with pts_ticks requires time_base")


def _common_fields(receipt: dict, *, include_media_timeline=True, include_slot=True) -> dict:
    """Common optional groups (episode/media/timeline/slot/replay) from a REA receipt."""
    if not isinstance(receipt, dict):
        raise ValueError("receipt must be an object")
    episode = receipt.get("episode") or {}
    common: dict = {}
    if isinstance(episode, dict):
        if episode.get("episode_id") is not None:
            _check_id(episode["episode_id"], "episode.episode_id")
            common["episode_id"] = episode["episode_id"]
        if episode.get("parent_episode_id") is not None:
            if "episode_id" not in common:
                raise ValueError("parent_episode_id requires episode_id")
            _check_id(episode["parent_episode_id"], "episode.parent_episode_id")
            common["parent_episode_id"] = episode["parent_episode_id"]
    media = receipt.get("media") if include_media_timeline else None
    if media is not None:
        if not isinstance(media, dict) or not media or set(media) - {"reference", "candidate"}:
            raise ValueError("media must name reference and/or candidate sides")
        for side_name, side in media.items():
            _check_media_side(side, f"media.{side_name}")
        common["media"] = media
    timeline = receipt.get("timeline") if include_media_timeline else None
    if timeline is not None:
        if not isinstance(timeline, dict) or not timeline or set(timeline) - {"reference", "candidate"}:
            raise ValueError("timeline must name reference and/or candidate sides")
        for side_name, side in timeline.items():
            _check_timeline_side(side, f"timeline.{side_name}")
        common["timeline"] = timeline
    slot = receipt.get("slot") if include_slot else None
    if slot is not None:
        common["slot"] = _project_slot(slot)
    replay = _project_replay(receipt.get("replay"))
    if replay is not None:
        common["replay_inputs"] = replay
    return common


def external_process_from_receipt(
    receipt: dict,
    *,
    installation_ref: str,
    request_sha: str | None = None,
) -> dict:
    """Build the v0.3 ``external_process`` binding from a REA receipt."""
    _check_id(installation_ref, "installation_ref")
    if not isinstance(receipt, dict):
        raise ValueError("receipt must be an object")
    attempt = receipt.get("attempt") or {}
    slot = receipt.get("slot") or {}
    slot_id = slot.get("tool_id")
    if type(slot_id) is not str or len(slot_id) > 24 or _SLOT_ID_RE.fullmatch(slot_id) is None:
        raise ValueError(f"slot_id {slot_id!r} must be kebab-case with at most 24 characters")
    provider_id = slot.get("provider_id")
    _check_id(provider_id, "slot.provider_id")
    provider_revision = slot.get("provider_revision")
    if type(provider_revision) is not str or not (1 <= len(provider_revision) <= 128):
        raise ValueError("slot.provider_revision must be a string of 1-128 chars")
    manifest_sha = slot.get("manifest_sha256")
    _check_sha(manifest_sha, "slot.manifest_sha256")
    implementation_sha = slot.get("implementation_sha256")
    _check_sha(implementation_sha, "slot.implementation_sha256")
    request_id = attempt.get("request_id")
    _check_id(request_id, "attempt.request_id")
    rea_attempt = attempt.get("attempt_id")
    if type(rea_attempt) is not str or not rea_attempt:
        raise ValueError("attempt.attempt_id must be a nonempty string")
    if request_sha is None:
        request_sha = request_sha256(request_id, rea_attempt)
    _check_sha(request_sha, "request_sha256")
    out = {
        "installation_ref": installation_ref,
        "slot_id": slot_id,
        "provider_id": provider_id,
        "provider_revision": provider_revision,
        "manifest_sha256": manifest_sha,
        "implementation_sha256": implementation_sha,
        "request_id": request_id,
        "request_sha256": request_sha,
    }
    adapter_sha = slot.get("adapter_sha256")
    if adapter_sha is not None:
        _check_sha(adapter_sha, "slot.adapter_sha256")
        out["adapter_sha256"] = adapter_sha
    return out


def _check_command(command: dict) -> dict:
    if not isinstance(command, dict) or set(command) - {"kind", "arguments", "cwd", "timeout_ms", "max_output_bytes", "env_sha256"}:
        raise ValueError("command has unsupported fields")
    if command.get("kind") != "argv":
        raise ValueError("command.kind must be argv for external_process")
    arguments = command.get("arguments")
    if not isinstance(arguments, list) or not (1 <= len(arguments) <= 256):
        raise ValueError("command.arguments must be a list of 1-256 strings")
    for item in arguments:
        if type(item) is not str or len(item) > 4096 or "\x00" in item or "\r" in item or "\n" in item:
            raise ValueError("command argument is invalid")
    if not arguments[0].startswith("/"):
        raise ValueError("command.arguments[0] must be the absolute admitted executable path")
    cwd = command.get("cwd")
    if type(cwd) is not str or not cwd.startswith("/") or "\x00" in cwd or len(cwd) > 4096:
        raise ValueError("command.cwd must be an absolute path")
    for key in ("timeout_ms", "max_output_bytes"):
        _check_integer(command.get(key), f"command.{key}", 1)
    _check_sha(command.get("env_sha256"), "command.env_sha256")
    return command


def _check_scope(scope: dict) -> dict:
    if not isinstance(scope, dict) or set(scope) - {"kind", "id", "cgroup_dev", "cgroup_ino", "memory_high_bytes", "memory_max_bytes", "memory_swap_max_bytes"}:
        raise ValueError("scope has unsupported fields")
    if scope.get("kind") not in ("systemd_scope", "cgroup_v2", "posix_child"):
        raise ValueError("scope.kind is invalid")
    _check_id(scope.get("id"), "scope.id")
    if ("cgroup_dev" in scope) != ("cgroup_ino" in scope):
        raise ValueError("cgroup_dev and cgroup_ino must appear together")
    if scope["kind"] == "posix_child" and ("cgroup_dev" in scope or "cgroup_ino" in scope):
        raise ValueError("posix_child scope carries no cgroup identity")
    for key, minimum in (("cgroup_dev", 0), ("cgroup_ino", 1), ("memory_high_bytes", 1),
                         ("memory_max_bytes", 1), ("memory_swap_max_bytes", 0)):
        if key in scope and (scope[key] is not None or key.startswith("cgroup")):
            _check_integer(scope[key], f"scope.{key}", minimum)
    return scope


def _check_record_size(record: dict) -> None:
    if len(record_bytes(record)) > RECORD_LIMIT:
        raise ValueError("record exceeds the v0.3 size cap")


def build_external_started(
    receipt: dict,
    *,
    installation_ref: str,
    command: dict,
    scope: dict,
    executor_id: str,
    request_sha: str | None = None,
    retained: bool = False,
) -> dict:
    """External launch ``started`` record from a REA opened receipt."""
    if retained:
        _check_sha(request_sha, "admitted request_sha256")
    _check_id(executor_id, "executor_id")
    _check_command(command)
    _check_scope(scope)
    attempt = receipt.get("attempt") or {}
    record: dict = {
        "schema": "attempt_evidence_record/v0.3",
        "execution_kind": "external_process",
        "state": "started",
        "attempt_id": wire_attempt_id(attempt.get("attempt_id", ""), retained=retained),
        "external_process": external_process_from_receipt(receipt, installation_ref=installation_ref, request_sha=request_sha),
        "command": command,
        "command_sha256": record_digest(command),
        "scope": scope,
        "executor_id": executor_id,
    }
    record.update(_common_fields(receipt, include_media_timeline=not retained))
    _check_record_size(record)
    return record


def _check_process(process: dict) -> dict:
    if not isinstance(process, dict) or set(process) - {"pid", "start_id", "job_id", "starttime_ticks", "boot_id"}:
        raise ValueError("process has unsupported fields")
    pid = process.get("pid")
    if type(pid) is not int or not (1 <= pid <= 2_147_483_647):
        raise ValueError("process.pid is invalid")
    _check_id(process.get("start_id"), "process.start_id")
    _check_id(process.get("job_id"), "process.job_id")
    _check_integer(process.get("starttime_ticks"), "process.starttime_ticks", 1)
    if "boot_id" in process:
        _check_id(process["boot_id"], "process.boot_id")
    return process


def _check_launch_observation(observation: dict, *, implementation_sha: str, executed_path: str) -> dict:
    if not isinstance(observation, dict) or set(observation) - {"observer_id", "observation_id", "receipt_sha256", "executable_sha256", "executed_path"}:
        raise ValueError("launch_observation has unsupported fields")
    _check_id(observation.get("observer_id"), "launch_observation.observer_id")
    _check_id(observation.get("observation_id"), "launch_observation.observation_id")
    _check_sha(observation.get("receipt_sha256"), "launch_observation.receipt_sha256")
    if observation.get("executable_sha256") != implementation_sha:
        raise ValueError("launch_observation executable digest must equal the admitted implementation")
    if "executed_path" in observation and observation["executed_path"] != executed_path:
        raise ValueError("launch_observation executed_path must agree with command arguments[0]")
    return observation


def project_producer(receipt: dict, *, pid: int, starttime_ticks: int) -> dict:
    """v0.3 observation producer from a REA receipt producer."""
    observation = (receipt.get("observation") or {})
    producer = observation.get("producer") or {}
    kind = producer.get("kind")
    if kind not in ("external_slot", "host_mini_tool", "host_level"):
        raise ValueError("producer kind is invalid")
    pid_ok = type(pid) is int and 1 <= pid <= 2_147_483_647
    ticks_ok = type(starttime_ticks) is int and 1 <= starttime_ticks <= _MAX_SAFE_INTEGER
    process_bound = producer.get("process_bound") is True
    out = {"kind": kind, "id": producer.get("id"), "process_bound": bool(process_bound)}
    _check_id(out["id"], "producer.id")
    if producer.get("version") is not None:
        version = producer["version"]
        if type(version) is not str or not (1 <= len(version) <= 128):
            raise ValueError("producer.version must be a string of 1-128 chars")
        out["version"] = version
    if kind == "external_slot" and process_bound:
        if not pid_ok or not ticks_ok:
            raise ValueError("process-bound external_slot requires pid and starttime_ticks")
        impl = producer.get("implementation_sha256")
        _check_sha(impl, "producer.implementation_sha256")
        out["implementation_sha256"] = impl
        out["pid"] = pid
        out["starttime_ticks"] = starttime_ticks
    elif producer.get("implementation_sha256") is not None:
        impl = producer["implementation_sha256"]
        _check_sha(impl, "producer.implementation_sha256")
        out["implementation_sha256"] = impl
    if process_bound and kind != "external_slot":
        raise ValueError("only external_slot producers may be process-bound here")
    return out


def project_ticket(ticket: dict | None, *, level: str) -> dict | None:
    """v0.3 redacted vision ticket, or None when level is not vision.

    Restricted tickets and tickets without owner/release custody deny
    (owner-pending T06: the vision mapping grant).
    """
    if level != "vision":
        if ticket is not None:
            raise ValueError("vision_pool_ticket only with perception_level vision")
        return None
    if not isinstance(ticket, dict):
        raise ValueError("vision perception requires a vision_pool_ticket")
    if ticket.get("projection") != "redacted" or "lease_id" in ticket:
        raise ValueError("only the redacted vision_pool_ticket projection is representable (T06)")
    if ticket.get("status") != "granted":
        raise ValueError("vision_pool_ticket.status must be granted")
    out = {}
    _check_sha(ticket.get("lease_id_sha256"), "ticket.lease_id_sha256")
    out["projection"] = "redacted"
    out["status"] = "granted"
    out["lease_id_sha256"] = ticket["lease_id_sha256"]
    front = ticket.get("front")
    if type(front) is not str or not (1 <= len(front) <= 128):
        raise ValueError("ticket.front must be a string of 1-128 chars")
    out["front"] = front
    owner_pid = ticket.get("owner_pid")
    if type(owner_pid) is not int or not (1 <= owner_pid <= 2_147_483_647):
        raise ValueError("ticket.owner_pid custody is missing (owner-pending T06)")
    out["owner_pid"] = owner_pid
    owner_ticks = ticket.get("owner_starttime_ticks")
    _check_integer(owner_ticks, "ticket.owner_starttime_ticks", 1)
    out["owner_starttime_ticks"] = owner_ticks
    _check_sha(ticket.get("grant_receipt_sha256"), "ticket.grant_receipt_sha256")
    out["grant_receipt_sha256"] = ticket["grant_receipt_sha256"]
    released = ticket.get("released_after_stop")
    if type(released) is not bool:
        raise ValueError("ticket.released_after_stop custody is missing (owner-pending T06)")
    out["released_after_stop"] = released
    if released:
        _check_sha(ticket.get("release_receipt_sha256"), "ticket.release_receipt_sha256")
        out["release_receipt_sha256"] = ticket["release_receipt_sha256"]
    elif "release_receipt_sha256" in ticket:
        raise ValueError("release_receipt_sha256 only with released_after_stop")
    return out


def project_observation(receipt: dict, *, pid: int | None = None, starttime_ticks: int | None = None) -> dict:
    observation = (receipt.get("observation") or {})
    level = observation.get("perception_level")
    if level not in ("none", "cdp_atspi", "vision"):
        raise ValueError(f"invalid perception_level: {level!r}")
    if observation.get("outside_capsule") is not True:
        raise ValueError("observation must be outside_capsule")
    producer = project_producer(receipt, pid=pid or 0, starttime_ticks=starttime_ticks or 0)
    out = {"outside_capsule": True, "perception_level": level, "producer": producer}
    ticket = project_ticket(observation.get("vision_pool_ticket"), level=level)
    if ticket is not None:
        out["vision_pool_ticket"] = ticket
    return out


def _project_divergence(verdict):
    divergence = verdict.get("first_divergence")
    if divergence is None:
        for check in verdict.get("checks") or []:
            if (isinstance(check, dict) and check.get("outcome") == "mismatch"
                    and isinstance(check.get("first_divergence"), dict)):
                divergence = dict(check["first_divergence"], check_id=check.get("check_id"))
                break
    ids = ("check_id", "event_id", "case_id")
    unsigned = ("occurrence", "frame_index", "reference_t_ms", "candidate_t_ms")
    if not isinstance(divergence, dict) or set(divergence) - {*ids, *unsigned, "delta_ms", "note"}:
        raise ValueError("mismatch requires a supported first_divergence with check_id")
    _check_id(divergence.get("check_id"), "first_divergence.check_id")
    for key in ("event_id", "case_id"):
        if key in divergence:
            _check_id(divergence[key], f"first_divergence.{key}")
    for key in unsigned:
        if key in divergence:
            _check_integer(divergence[key], f"first_divergence.{key}")
    if "delta_ms" in divergence:
        _check_integer(divergence["delta_ms"], "first_divergence.delta_ms", -_MAX_SAFE_INTEGER)
    if "note" in divergence and (type(divergence["note"]) is not str or len(divergence["note"]) > 2000):
        raise ValueError("first_divergence.note must be a string of at most 2000 characters")
    return dict(divergence)


def project_verdict(receipt: dict) -> dict:
    """REA verdict -> v0.3 rea_verdict (abstain reasons are the frozen enum)."""
    verdict = receipt.get("verdict")
    if not isinstance(verdict, dict):
        raise ValueError("compare verdict is required")
    result = verdict.get("result")
    if result not in ("match", "mismatch", "abstain"):
        raise ValueError(f"invalid verdict result: {result!r}")
    allowed_reasons = {
        "control_refused", "uncertain_outcome", "exchange_invalid", "evidence_binding_rejected",
        "custody_unverified", "tape_mismatch", "slot_stale", "stale_evidence", "slot_error",
        "slot_incomplete", "slot_budget_exceeded", "slot_claim_only", "slot_host_disagreement",
        "empty_reference", "timeline_invalid", "clock_source_mismatch", "clock_source_unsuitable",
        "missing_clock_binding", "origin_mismatch", "missing_reference_coverage", "coverage_insufficient",
        "perception_ceiling", "perception_unavailable", "vision_pool_timeout", "vision_pool_not_granted",
        "lease_custody_unverified", "policy_undeclared", "unrepeatable_effect", "contrast_failed",
        "negative_control_missing", "negative_control_unactivated", "negative_control_survived",
        "determinism_mode_unsupported",
    }
    out: dict = {
        "result": result,
        "comparator_id": verdict.get("comparator_id", "host.compare"),
        "version": verdict.get("comparator_version", "1"),
        "contrast_ids": list(verdict.get("contrast_ids") or []),
        "negative_control_ids": [c.get("control_id") for c in verdict.get("negative_controls") or [] if isinstance(c, dict)],
        "oracle_independence": verdict.get("oracle_independence", "independent"),
        "policy": dict(verdict.get("policy") or {}),
        "independent_qualification": verdict.get("independent_qualification") is True,
    }
    _check_id(out["comparator_id"], "verdict.comparator_id")
    if type(out["version"]) is not str or not (1 <= len(out["version"]) <= 128):
        raise ValueError("verdict version must be a string of 1-128 chars")
    if out["oracle_independence"] not in ("independent", "derived_from_same_source"):
        raise ValueError("verdict oracle_independence is invalid")
    if out["independent_qualification"] and (result != "match" or out["oracle_independence"] != "independent"):
        raise ValueError("independent_qualification requires an independent match")
    policy = out["policy"]
    if set(policy) - {"id", "version", "sha256"} or not {"id", "version", "sha256"} <= set(policy):
        raise ValueError("verdict policy must be an id/version/sha256 object")
    _check_id(policy["id"], "policy.id")
    _check_sha(policy["sha256"], "policy.sha256")
    if type(policy["version"]) is not str or not 1 <= len(policy["version"]) <= 64:
        raise ValueError("policy.version must be a string of 1-64 characters")
    for field in ("negative_control_ids", "contrast_ids"):
        if len(out[field]) > 128:
            raise ValueError(f"{field} must carry at most 128 entries")
        for value in out[field]:
            _check_id(value, f"{field} entry")
    if result == "abstain":
        reason = verdict.get("abstain_reason")
        if reason not in allowed_reasons:
            raise ValueError(f"abstain_reason {reason!r} is not a frozen v0.3 reason")
        out["abstain_reason"] = reason
    if result == "mismatch":
        out["first_divergence"] = _project_divergence(verdict)
    return out


def _artifact_bytes(*, sha256: str, payload: bytes | None) -> bytes:
    if payload is None:
        raw = record_bytes({"sha256": sha256})
    else:
        raw = payload
    if len(raw) > ARTIFACT_LIMIT:
        raise ValueError("inline artifact exceeds the artifact byte cap")
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError("artifact payload digest does not match its sha256")
    return raw


def _check_observers(started, artifact_observer_id, cleanup_observer_id, cleanup_state):
    executor_id = started.get("executor_id")
    _check_id(executor_id, "started.executor_id")
    if artifact_observer_id == executor_id:
        raise ValueError("artifact requires an independent observer")
    if cleanup_state == "confirmed_retired" and cleanup_observer_id == executor_id:
        raise ValueError("confirmed retirement requires an independent cleanup observer")


def build_external_observed(
    receipt: dict,
    started: dict,
    *,
    installation_ref: str,
    scope: dict,
    process: dict,
    launch_observation: dict,
    artifact_payload: bytes | None = None,
    artifact_sha256: str | None = None,
    attribution_receipt_sha256: str | None = None,
    artifact_observer_id: str = "observer/capture",
    artifact_observation_id: str = "capture/rea",
    cleanup_observer_id: str = "observer/cleanup",
    cleanup_observation_id: str = "cleanup/rea",
    cleanup_state: str = "confirmed_retired",
    cleanup_retirement_sha256: str | None = None,
    request_sha: str | None = None,
    retained: bool = False,
) -> dict:
    """External launch ``observed`` record bound to its started record."""
    if retained:
        _check_sha(request_sha, "admitted request_sha256")
    _check_scope(scope)
    _check_process(process)
    if not isinstance(started, dict) or started.get("state") != "started":
        raise ValueError("started must be a started record")
    start_sha = record_digest(started)
    attempt = receipt.get("attempt") or {}
    attempt_v03 = wire_attempt_id(attempt.get("attempt_id", ""), retained=retained)
    if started.get("attempt_id") != attempt_v03:
        raise ValueError("started record does not bind this attempt")
    external = external_process_from_receipt(receipt, installation_ref=installation_ref, request_sha=request_sha)
    if retained and "adapter_sha256" in started.get("external_process", {}):
        external["adapter_sha256"] = started["external_process"]["adapter_sha256"]
    if started.get("external_process") != external:
        raise ValueError("external_process must match the started record")
    command = _check_command(started.get("command"))
    command_sha = record_digest(command)
    if started.get("command_sha256") != command_sha or started.get("scope") != scope:
        raise ValueError("observed command digest and scope must match the started record")
    _check_launch_observation(launch_observation, implementation_sha=external["implementation_sha256"],
                              executed_path=command["arguments"][0])
    execution = {
        "attempt_id": attempt_v03,
        "external_process": external,
        "command_sha256": command_sha,
        "scope": scope,
        "process": process,
        "launch_observation": launch_observation,
    }
    execution_sha = record_digest(execution)
    _check_id(artifact_observer_id, "artifact.observer_id")
    _check_id(artifact_observation_id, "artifact.observation_id")
    _check_id(cleanup_observer_id, "cleanup.observer_id")
    _check_id(cleanup_observation_id, "cleanup.observation_id")
    if cleanup_state not in ("confirmed_retired", "unknown"):
        raise ValueError("cleanup state must be confirmed_retired or unknown")
    _check_observers(started, artifact_observer_id, cleanup_observer_id, cleanup_state)
    if cleanup_state == "confirmed_retired" and not cleanup_retirement_sha256:
        raise ValueError("confirmed retirement requires a retirement receipt digest (T03 witness seam)")
    if cleanup_state == "unknown" and cleanup_retirement_sha256:
        raise ValueError("unknown cleanup carries no retirement receipt")
    pid = process.get("pid")
    ticks = process.get("starttime_ticks")
    # The REA producer must agree with the launch tuple: a process-bound
    # producer with a different pid cannot project (T03 witness seam).
    producer = ((receipt.get("observation") or {}).get("producer") or {})
    if producer.get("process_bound") is True and producer.get("pid") not in (None, pid):
        raise ValueError("REA producer pid disagrees with the launch tuple")
    observation = project_observation(receipt, pid=pid, starttime_ticks=ticks)
    # Host observation of its own process: outside_capsule producer that
    # did not prove a process binding stays process_bound=false.
    if retained:
        artifact_bytes = b""
        artifact_sha = hashlib.sha256(artifact_bytes).hexdigest()
    elif artifact_sha256 is not None:
        _check_sha(artifact_sha256, "artifact.sha256")
        artifact_sha = artifact_sha256
        artifact_bytes = _artifact_bytes(sha256=artifact_sha, payload=artifact_payload)
    else:
        slot_result = (attempt or {}).get("slot_result")
        artifact_sha = slot_result.get("sha256") if isinstance(slot_result, dict) else None
        if slot_result is not None:
            _check_sha(artifact_sha, "artifact.sha256")
            artifact_bytes = _artifact_bytes(sha256=artifact_sha, payload=artifact_payload)
        else:
            artifact_sha = record_digest({"attempt": attempt_v03, "execution": execution_sha, "kind": "inline_payload"})
            artifact_bytes = record_bytes({"attempt": attempt_v03, "execution": execution_sha, "kind": "inline_payload"})
    record: dict = {
        "schema": "attempt_evidence_record/v0.3",
        "execution_kind": "external_process",
        "state": "observed",
        "attempt_id": attempt_v03,
        "start_sha256": start_sha,
        "execution": execution,
        "observation": observation,
        "artifact": {
            "execution_sha256": execution_sha,
            "observer_id": artifact_observer_id,
            "observation_id": artifact_observation_id,
            "sha256": artifact_sha,
            "bytes": len(artifact_bytes),
            "content_kind": "inline_payload",
        },
        "cleanup": {
            "execution_sha256": execution_sha,
            "observer_id": cleanup_observer_id,
            "observation_id": cleanup_observation_id,
            "state": cleanup_state,
        },
    }
    if observation["producer"].get("kind") == "external_slot":
        # An independently attributed observation (T03) requires an
        # attribution receipt; offline the launch-observation receipt
        # is the host's own launch witness, never a foreign proof.
        attribution = attribution_receipt_sha256 or launch_observation.get("receipt_sha256")
        _check_sha(attribution, "artifact.attribution_receipt_sha256")
        record["artifact"]["attribution_receipt_sha256"] = attribution
    if cleanup_state == "confirmed_retired":
        _check_sha(cleanup_retirement_sha256, "cleanup.retirement_receipt_sha256")
        record["cleanup"]["retirement_receipt_sha256"] = cleanup_retirement_sha256
    record.update(_common_fields(receipt))
    _check_record_size(record)
    return record


def build_external_aborted(
    receipt: dict,
    started: dict,
    *,
    abort_reason: str,
    artifact_observer_id: str = "observer/capture",
    artifact_observation_id: str = "capture/rea",
    cleanup_observer_id: str = "observer/cleanup",
    cleanup_observation_id: str = "cleanup/rea",
    cleanup_state: str = "confirmed_retired",
    cleanup_retirement_sha256: str | None = None,
    retained: bool = False,
) -> dict:
    """External launch ``aborted_before_spawn`` record (no process ran)."""
    _check_id(abort_reason, "abort_reason")
    _check_id(artifact_observer_id, "artifact.observer_id")
    _check_id(artifact_observation_id, "artifact.observation_id")
    _check_id(cleanup_observer_id, "cleanup.observer_id")
    _check_id(cleanup_observation_id, "cleanup.observation_id")
    if cleanup_state not in ("confirmed_retired", "unknown"):
        raise ValueError("cleanup state must be confirmed_retired or unknown")
    if not isinstance(started, dict) or started.get("state") != "started":
        raise ValueError("started must be a started record")
    _check_observers(started, artifact_observer_id, cleanup_observer_id, cleanup_state)
    start_sha = record_digest(started)
    attempt_v03 = started.get("attempt_id")
    _check_attempt(attempt_v03)
    if retained and (receipt.get("attempt") or {}).get("attempt_id") != attempt_v03:
        raise ValueError("refusal does not bind the started attempt")
    if cleanup_state == "confirmed_retired" and not cleanup_retirement_sha256:
        raise ValueError("confirmed retirement requires a retirement receipt digest (T03 witness seam)")
    if cleanup_state == "unknown" and cleanup_retirement_sha256:
        raise ValueError("unknown cleanup carries no retirement receipt")
    payload = record_bytes({"abort_reason": abort_reason, "attempt": attempt_v03, "start": start_sha})
    artifact = {
        "start_sha256": start_sha,
        "observer_id": artifact_observer_id,
        "observation_id": artifact_observation_id,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "content_kind": "inline_payload",
    }
    cleanup: dict = {
        "start_sha256": start_sha,
        "observer_id": cleanup_observer_id,
        "observation_id": cleanup_observation_id,
        "state": cleanup_state,
    }
    if cleanup_state == "confirmed_retired":
        _check_sha(cleanup_retirement_sha256, "cleanup.retirement_receipt_sha256")
        cleanup["retirement_receipt_sha256"] = cleanup_retirement_sha256
    record: dict = {
        "schema": "attempt_evidence_record/v0.3",
        "execution_kind": "external_process",
        "state": "aborted_before_spawn",
        "attempt_id": attempt_v03,
        "start_sha256": start_sha,
        "abort_reason": abort_reason,
        "artifact": artifact,
        "cleanup": cleanup,
    }
    if (receipt.get("attempt") or {}).get("operation") == "compare":
        verdict = project_verdict(receipt)
        if verdict["result"] != "abstain":
            raise ValueError("an aborted comparison requires an abstain verdict")
        record["verdict"] = verdict
    record.update(_common_fields(receipt, include_media_timeline=False))
    _check_record_size(record)
    return record


def build_compare_started(
    receipt: dict,
    *,
    executor_id: str,
    reference_attempt_id: str,
    candidate_attempt_id: str,
    request_sha: str | None = None,
    retained: bool = False,
) -> dict:
    """Host-compare ``started`` record from a REA compare receipt."""
    if retained:
        _check_sha(request_sha, "admitted request_sha256")
    _check_id(executor_id, "executor_id")
    _check_attempt(reference_attempt_id, "reference_attempt_id")
    _check_attempt(candidate_attempt_id, "candidate_attempt_id")
    attempt = receipt.get("attempt") or {}
    if attempt.get("operation") != "compare":
        raise ValueError("compare started requires a compare receipt")
    request_id = attempt.get("request_id")
    _check_id(request_id, "attempt.request_id")
    rea_attempt = attempt.get("attempt_id")
    if request_sha is None:
        request_sha = request_sha256(request_id, rea_attempt)
    _check_sha(request_sha, "request_sha256")
    record: dict = {
        "schema": "attempt_evidence_record/v0.3",
        "execution_kind": "external_process",
        "state": "started",
        "attempt_id": wire_attempt_id(rea_attempt, retained=retained),
        "operation": "compare",
        "executor_id": executor_id,
        "request_id": request_id,
        "request_sha256": request_sha,
        "reference_attempt_id": reference_attempt_id,
        "candidate_attempt_id": candidate_attempt_id,
    }
    record.update(_common_fields(receipt, include_slot=False, include_media_timeline=not retained))
    _check_record_size(record)
    return record


def build_compare_observed(
    receipt: dict,
    started: dict,
    *,
    artifact_observer_id: str = "observer/capture",
    artifact_observation_id: str = "capture/rea",
    cleanup_observer_id: str = "observer/cleanup",
    cleanup_observation_id: str = "cleanup/rea",
    cleanup_state: str = "confirmed_retired",
    cleanup_retirement_sha256: str | None = None,
    retained: bool = False,
) -> dict:
    """Host-compare ``observed`` record bound to its started record."""
    _check_id(artifact_observer_id, "artifact.observer_id")
    _check_id(artifact_observation_id, "artifact.observation_id")
    _check_id(cleanup_observer_id, "cleanup.observer_id")
    _check_id(cleanup_observation_id, "cleanup.observation_id")
    if cleanup_state not in ("confirmed_retired", "unknown"):
        raise ValueError("cleanup state must be confirmed_retired or unknown")
    if not isinstance(started, dict) or started.get("state") != "started":
        raise ValueError("started must be a started record")
    _check_observers(started, artifact_observer_id, cleanup_observer_id, cleanup_state)
    start_sha = record_digest(started)
    attempt_v03 = started.get("attempt_id")
    _check_attempt(attempt_v03)
    if attempt_v03 != wire_attempt_id((receipt.get("attempt") or {}).get("attempt_id", ""), retained=retained):
        raise ValueError("started record does not bind this attempt")
    if cleanup_state == "confirmed_retired" and not cleanup_retirement_sha256:
        raise ValueError("confirmed retirement requires a retirement receipt digest (T03 witness seam)")
    if cleanup_state == "unknown" and cleanup_retirement_sha256:
        raise ValueError("unknown cleanup carries no retirement receipt")
    payload = record_bytes({"attempt": attempt_v03, "kind": "compare", "start": start_sha})
    # The host comparator is a host_mini_tool / host_level producer,
    # process_bound=false, never an external_slot.
    observation = project_observation(receipt)
    producer = observation["producer"]
    if producer["kind"] not in ("host_mini_tool", "host_level") or producer["process_bound"] is not False:
        raise ValueError("compare observation requires a non-process-bound host producer")
    artifact = {
        "start_sha256": start_sha,
        "observer_id": artifact_observer_id,
        "observation_id": artifact_observation_id,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "content_kind": "inline_payload",
    }
    cleanup: dict = {
        "start_sha256": start_sha,
        "observer_id": cleanup_observer_id,
        "observation_id": cleanup_observation_id,
        "state": cleanup_state,
    }
    if cleanup_state == "confirmed_retired":
        _check_sha(cleanup_retirement_sha256, "cleanup.retirement_receipt_sha256")
        cleanup["retirement_receipt_sha256"] = cleanup_retirement_sha256
    record: dict = {
        "schema": "attempt_evidence_record/v0.3",
        "execution_kind": "external_process",
        "state": "observed",
        "attempt_id": attempt_v03,
        "start_sha256": start_sha,
        "operation": "compare",
        "observation": observation,
        "verdict": project_verdict(receipt),
        "artifact": artifact,
        "cleanup": cleanup,
    }
    record.update(_common_fields(receipt, include_slot=False))
    _check_record_size(record)
    return record


def verify_index_objects(profile: dict, entries: list[dict]) -> None:
    """Check the complete per-side object graph against the inner REA profile."""
    profile_raw = record_bytes(profile)
    profiles = [entry for entry in entries if entry["role"] == "rea_profile"]
    if len(profiles) != 1 or (profiles[0]["bytes"], profiles[0]["sha256"]) != (
        len(profile_raw), hashlib.sha256(profile_raw).hexdigest(),
    ):
        raise ValueError("the index must bind the exact inner profile")
    expected = {}
    for name, side in profile.get("media", {}).items():
        _check_media_side(side, f"media.{name}")
        expected["recording", name] = side["recording_sha256"]
    for name, side in profile.get("timeline", {}).items():
        expected["timeline_events", name] = hashlib.sha256(
            _check_timeline_side(side, f"timeline.{name}"),
        ).hexdigest()
    primary = [entry for entry in entries if entry["role"] in ("recording", "timeline_events")]
    provider_result = profile["attempt"].get("slot_result")
    results = [entry["sha256"] for entry in entries if entry["role"] == "provider_result"]
    if results != ([] if provider_result is None else [provider_result["sha256"]]):
        raise ValueError("the index does not bind the exact provider result octets")
    if len(primary) != len(expected) or {
        (entry["role"], entry["side"]): entry["sha256"] for entry in primary
    } != expected:
        raise ValueError("the index does not bind the recording and event bytes of each side")


def build_uploaded_index(profile, admission, terminal, upload, *, recordings=None, payloads=None):
    """Upload immutable octets and substitute only verified service references."""
    recordings = {} if recordings is None else recordings
    if recordings.keys() != profile.get("media", {}).keys():
        raise ValueError("each retained recording requires its actual bytes")
    for side, raw in recordings.items():
        if type(raw) is not bytes or hashlib.sha256(raw).hexdigest() != profile["media"][side]["recording_sha256"]:
            raise ValueError("recording bytes do not bind the observed side")
    entries = [upload("rea-profile", "rea_profile", record_bytes(profile))]
    terminal = dict(terminal)
    for side, raw in recordings.items():
        entries.append(upload(f"recording-{side}", "recording", raw) | {"side": side})
    if "timeline" in profile:
        terminal["timeline"] = {}
        for side, value in profile["timeline"].items():
            events = _check_timeline_side(value, f"timeline.{side}")
            entry = upload(f"timeline-{side}", "timeline_events", events) | {"side": side}
            entries.append(entry)
            terminal["timeline"][side] = {
                **{key: item for key, item in value.items() if key != "events"},
                "events_object_ref": entry["object_ref"],
            }
    for role, raw in (payloads or {}).items():
        entries.append(upload(role, role, raw))
    index = build_retention_index(
        profile, profile_sha256=entries[0]["sha256"], profile_schema=admission["profile_schema"],
        profile_schema_sha256=admission["profile_schema_sha256"], objects=entries,
        request_sha=admission["request_sha256"], admission_sha256=admission["admission_sha256"],
        policy_sha256=terminal.get("verdict", {}).get("policy", {}).get("sha256"), retained=True,
    )
    artifact = record_bytes(index)
    terminal["artifact"] = terminal["artifact"] | {
        "content_kind": "object_index", "bytes": len(artifact),
        "sha256": hashlib.sha256(artifact).hexdigest(),
    }
    return terminal, index, artifact


def build_retention_index(
    receipt: dict,
    *,
    profile_sha256: str,
    profile_schema: str = PROFILE_SCHEMA_V1,
    profile_schema_sha256: str | None = None,
    objects: list[dict] | None = None,
    request_sha: str | None = None,
    admission_sha256: str | None = None,
    policy_sha256: str | None = None,
    control_outcome: str | None = None,
    retained: bool = False,
) -> dict:
    """Build the frozen index from acknowledged uploads or an offline fixture.

    Exactly one ``rea_profile`` object binds the canonical receipt
    bytes; its digest must equal ``profile_sha256``.  Additional
    objects (recordings, events, tapes, results) carry their own
    bytes/digest and unique refs; media/timeline sides present on the
    receipt each contribute their role objects.
    """
    _check_sha(profile_sha256, "profile_sha256")
    if profile_schema_sha256 is not None:
        _check_sha(profile_schema_sha256, "profile_schema_sha256")
    attempt = receipt.get("attempt") or {}
    episode = receipt.get("episode") or {}
    request_id = attempt.get("request_id")
    _check_id(request_id, "attempt.request_id")
    rea_attempt = attempt.get("attempt_id")
    if type(rea_attempt) is not str or not rea_attempt:
        raise ValueError("attempt.attempt_id must be a nonempty string")
    operation = attempt.get("operation")
    if operation not in ("record", "replay", "compare"):
        raise ValueError(f"invalid operation: {operation!r}")
    if retained:
        _check_sha(request_sha, "admitted request_sha256")
        _check_sha(admission_sha256, "admission_sha256")
        _check_sha(profile_schema_sha256, "profile_schema_sha256")
    if request_sha is None:
        request_sha = request_sha256(request_id, rea_attempt)
    _check_sha(request_sha, "request_sha256")
    _check_id(profile_schema, "profile_schema")
    raw_profile = record_bytes(receipt)
    if hashlib.sha256(raw_profile).hexdigest() != profile_sha256:
        raise ValueError("profile_sha256 must be the digest of the canonical receipt bytes")
    entries: list[dict] = [] if retained else [{
        "object_ref": f"object/{v03_attempt_id(rea_attempt)}/rea-profile",
        "role": "rea_profile",
        "bytes": len(raw_profile),
        "sha256": profile_sha256,
    }]
    if objects:
        for entry in objects:
            if not isinstance(entry, dict) or set(entry) - {"object_ref", "role", "bytes", "sha256", "side"}:
                raise ValueError("index object has unsupported fields")
            _check_id(entry.get("object_ref"), "object.object_ref")
            if entry.get("role") not in ("rea_profile", "recording", "frame", "timeline_events", "input_tape", "comparison", "observation", "provider_result", "cleanup"):
                raise ValueError(f"invalid index object role: {entry.get('role')!r}")
            if retained and not re.fullmatch(r"obj_[0-9a-f]{32}", entry["object_ref"]):
                raise ValueError("retained object requires a Tools object reference")
            if not retained and entry["role"] == "rea_profile":
                raise ValueError("offline index supplies its own profile entry")
            _check_integer(entry.get("bytes"), "index object bytes")
            _check_sha(entry.get("sha256"), "object.sha256")
            if entry["role"] in ("recording", "frame", "timeline_events") and entry.get("side") not in ("reference", "candidate"):
                raise ValueError("recording/frame/timeline_events objects require a side")
            if entry["role"] not in ("recording", "frame", "timeline_events") and "side" in entry:
                raise ValueError("side is only valid for recording/frame/timeline_events objects")
            entries.append(dict(entry))
    if retained:
        verify_index_objects(receipt, entries)
    for group in (() if retained else ("media", "timeline")):
        value = receipt.get(group)
        if not isinstance(value, dict):
            continue
        for side_name, side in value.items():
            if side_name not in ("reference", "candidate"):
                raise ValueError(f"{group} must name reference and/or candidate sides")
            if group == "media":
                role = "recording"
                raw = record_bytes(side)
                obj_ref = f"object/{v03_attempt_id(rea_attempt)}/media/{side_name}"
            else:
                role = "timeline_events"
                if "events" in side:
                    raw = _check_timeline_side(side, f"timeline.{side_name}")
                else:
                    continue  # offloaded events are indexed via `objects`
                obj_ref = f"object/{v03_attempt_id(rea_attempt)}/timeline/{side_name}"
            entries.append({"object_ref": obj_ref, "role": role, "side": side_name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    refs = [entry["object_ref"] for entry in entries]
    if len(set(refs)) != len(refs):
        raise ValueError("index object_ref values must be unique")
    index: dict = {
        "schema": INDEX_SCHEMA,
        "request_id": request_id,
        "attempt_id": wire_attempt_id(rea_attempt, retained=retained),
        "operation": operation,
        "profile_schema": profile_schema,
        "profile_schema_sha256": profile_schema_sha256 or profile_sha256,
        "request_sha256": request_sha,
        "profile_sha256": profile_sha256,
        "objects": entries,
    }
    if episode.get("episode_id") is not None:
        _check_id(episode["episode_id"], "episode.episode_id")
        index["episode_id"] = episode["episode_id"]
    if episode.get("parent_episode_id") is not None:
        if "episode_id" not in index:
            raise ValueError("parent_episode_id requires episode_id")
        _check_id(episode["parent_episode_id"], "episode.parent_episode_id")
        index["parent_episode_id"] = episode["parent_episode_id"]
    if attempt.get("slot_role") is not None:
        _check_id(attempt["slot_role"], "attempt.slot_role")
        index["slot_role"] = attempt["slot_role"]
    outcome = control_outcome or attempt.get("control_outcome")
    if outcome is not None:
        _check_id(outcome, "control_outcome")
        index["control_outcome"] = outcome
    if attempt.get("reconciles_attempt_id") is not None:
        if retained:
            raise ValueError("the frozen service does not support index reconciliation")
        prior = attempt["reconciles_attempt_id"]
        _check_id(prior, "reconciles_attempt_id")
        # Both IDs in the REA profile are in the REA namespace, even when
        # one happens to have the same shape as a Tools ID.
        index["reconciles_attempt_id"] = v03_attempt_id(prior)
        if index["reconciles_attempt_id"] == index["attempt_id"]:
            raise ValueError("reconciliation requires a distinct prior attempt")
    if admission_sha256 is not None:
        _check_sha(admission_sha256, "admission_sha256")
        index["admission_sha256"] = admission_sha256
    if policy_sha256 is not None:
        _check_sha(policy_sha256, "policy_sha256")
        index["policy_sha256"] = policy_sha256
    if len(entries) > 256:
        raise ValueError("index carries at most 256 objects")
    if len(record_bytes(index)) > ARTIFACT_LIMIT:
        raise ValueError("index exceeds the artifact byte cap")
    return index

def profile_schema_sha256() -> str:
    """Digest of the on-disk rea.slot_evidence_profile/v1 schema file."""
    try:
        from pathlib import Path
        path = Path(__file__).resolve().parents[2] / "contracts" / "evidence" / "rea.slot_evidence_profile_v1.schema.json"
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception as exc:
        raise ValueError(f"cannot read the REA profile schema: {exc}") from exc


def build_bundle(
    receipt: dict,
    *,
    launched: bool,
    installation_ref: str,
    command: dict,
    scope: dict,
    executor_id: str = "host/rea-launcher",
    process: dict | None = None,
    launch_observation: dict | None = None,
    artifact_payload: bytes | None = None,
    artifact_sha256: str | None = None,
    cleanup_state: str = "confirmed_retired",
    cleanup_retirement_sha256: str | None = None,
    abort_reason: str = "LIVE_EXECUTION_PARKED",
    reference_attempt_id: str | None = None,
    candidate_attempt_id: str | None = None,
) -> dict:
    """Build the complete offline v0.3 bundle for one REA receipt.

    Returns {"started", "terminal", "retention_index"}.  The terminal
    is ``observed`` when a process ran, otherwise
    ``aborted_before_spawn``.  Retention is local only (T01 seam):
    no authenticated service is contacted.
    """
    operation = (receipt.get("attempt") or {}).get("operation")
    if operation == "compare":
        if reference_attempt_id is None or candidate_attempt_id is None:
            raise ValueError("compare bundle requires reference/candidate attempt ids")
        started = build_compare_started(
            receipt, executor_id=executor_id,
            reference_attempt_id=reference_attempt_id, candidate_attempt_id=candidate_attempt_id,
        )
        terminal = build_compare_observed(
            receipt, started,
            cleanup_state=cleanup_state, cleanup_retirement_sha256=cleanup_retirement_sha256,
        )
    else:
        started = build_external_started(
            receipt, installation_ref=installation_ref, command=command, scope=scope, executor_id=executor_id,
        )
        if launched:
            if process is None or launch_observation is None:
                raise ValueError("observed bundle requires process and launch_observation")
            terminal = build_external_observed(
                receipt, started,
                installation_ref=installation_ref, scope=scope, process=process,
                launch_observation=launch_observation,
                artifact_payload=artifact_payload, artifact_sha256=artifact_sha256,
                cleanup_state=cleanup_state, cleanup_retirement_sha256=cleanup_retirement_sha256,
            )
        else:
            terminal = build_external_aborted(
                receipt, started, abort_reason=abort_reason,
                cleanup_state=cleanup_state, cleanup_retirement_sha256=cleanup_retirement_sha256,
            )
    index = build_retention_index(
        receipt,
        profile_sha256=record_digest(receipt),
        profile_schema_sha256=profile_schema_sha256(),
        request_sha=started.get("external_process", started)["request_sha256"],
    )
    return {"started": started, "terminal": terminal, "retention_index": index}
