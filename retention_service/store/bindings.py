"""Cross-record bindings beyond the accepted wire and frozen schemas."""

from collections import Counter
from fractions import Fraction
import json

from ..contracts.validation import ContractError, strict_json
from ..contracts.wire import record_bytes
from .files import digest

LAUNCH_PINS = (
    "installation_ref", "slot_id", "provider_id", "provider_revision",
    "manifest_sha256", "implementation_sha256", "adapter_sha256",
)
INDEX_PINS = (
    "attempt_id", "request_id", "request_sha256", "operation",
    "profile_schema", "profile_schema_sha256",
)


def require(condition, message):
    if not condition:
        raise ContractError(message)


def start_binding(admission, start):
    require(start["attempt_id"] == admission["attempt_id"]
            and start["executor_id"] == admission["executor_id"]
            and start["execution_kind"] == "external_process",
            "start does not bind the admitted attempt and executor")
    if admission["operation"] == "compare":
        fields = ("operation", "request_id", "request_sha256",
                  "reference_attempt_id", "candidate_attempt_id")
        require(all(start.get(key) == admission[key] for key in fields),
                "start does not bind the ordered comparison sources")
    else:
        external = start.get("external_process", {})
        require(all(external.get(key) == admission.get(key)
                    for key in (*LAUNCH_PINS, "request_id", "request_sha256")),
                "external start does not bind the admission pins")
        check_slot(admission, start)
        command = start["command"]
        require(start["command_sha256"] == digest(record_bytes(command)),
                "command digest does not bind the exact command")
        require(command["cwd"].startswith("/") and "\0" not in command["cwd"]
                and command["arguments"][0] and all("\0" not in item
                                                    for item in command["arguments"]),
                "invalid external command invocation")
        high = start["scope"].get("memory_high_bytes")
        maximum = start["scope"].get("memory_max_bytes")
        require(high is None or maximum is None or high <= maximum,
                "invalid scope memory bounds")


def check_slot(admission, record):
    if "slot" not in record:
        return
    slot = record["slot"]
    for key in ("source_url", "provider_id", "provider_revision", "manifest_sha256",
                "implementation_sha256", "adapter_sha256", "tool_id"):
        admitted_key = "slot_id" if key == "tool_id" else key
        if key in slot:
            require(slot[key] == admission.get(admitted_key),
                    f"registered slot disagrees with admitted {admitted_key}")


def terminal_binding(admission, start, start_sha, terminal, *, claimed):
    require(terminal["attempt_id"] == start["attempt_id"]
            and terminal["execution_kind"] == start["execution_kind"]
            and terminal["start_sha256"] == start_sha,
            "terminal does not bind the stored start")
    for key in ("episode_id", "parent_episode_id", "slot", "replay_inputs"):
        if key in start and key in terminal:
            require(start[key] == terminal[key], f"terminal changed {key}")
    if "source_url" in admission:
        check_slot(admission, terminal)
    artifact = terminal["artifact"]
    cleanup = terminal["cleanup"]
    require(artifact["observer_id"] != start["executor_id"],
            "artifact requires an independent witness")
    if cleanup["state"] == "confirmed_retired":
        require(cleanup["observer_id"] != start["executor_id"],
                "retirement requires an independent witness")
    if admission["operation"] == "compare":
        require(terminal.get("operation") == "compare" and not claimed,
                "comparison terminal or launch state is incompatible")
    elif terminal["state"] == "observed":
        require(claimed and "execution" in terminal, "observed launch requires its durable claim")
        execution = terminal["execution"]
        require(execution["attempt_id"] == start["attempt_id"]
                and all(execution[key] == start[key]
                        for key in ("command_sha256", "scope", "external_process")),
                "execution does not bind the dispatched start")
        launch = execution["launch_observation"]
        require(launch["observer_id"] != start["executor_id"]
                and launch["executable_sha256"] == admission["implementation_sha256"]
                and launch.get("executed_path", start["command"]["arguments"][0])
                == start["command"]["arguments"][0],
                "launch witness does not bind the staged executable")
        producer = terminal["observation"]["producer"]
        if producer["process_bound"]:
            require((producer["pid"], producer["starttime_ticks"]) ==
                    (execution["process"]["pid"], execution["process"]["starttime_ticks"]),
                    "producer does not bind the observed process")
        by_execution = digest(record_bytes(execution))
        require(artifact["execution_sha256"] == by_execution
                and cleanup["execution_sha256"] == by_execution,
                "witnesses do not bind the execution digest")
        return
    else:
        require("operation" not in terminal and "execution" not in terminal,
                "processless refusal contains launch execution")
        require(cleanup["observer_id"] != start["executor_id"],
                "processless refusal requires an independent no-spawn witness")
    require(artifact["start_sha256"] == start_sha and cleanup["start_sha256"] == start_sha,
            "processless witnesses do not bind the stored start")


def verify_timeline(record, read_events):
    for name, side in record.get("timeline", {}).items():
        if "events_object_ref" in side:
            raw = read_events(name, side["events_object_ref"])
            events = strict_json(raw)
        else:
            raw = None
            events = side["events"]
        require(isinstance(events, list) and len(events) <= 1024, "invalid timeline event list")
        seen = set()
        occurrences = Counter()
        previous = None
        for event in events:
            require(isinstance(event, dict)
                    and {"event_id", "kind", "input_digest", "t_ms"} <= event.keys()
                    and all(isinstance(event[key], str)
                            for key in ("event_id", "kind", "input_digest"))
                    and all(type(event[key]) is int and event[key] >= 0
                            for key in ("t_ms", "occurrence", "t_ticks") if key in event),
                    "invalid timeline event")
            event_id = event["event_id"]
            require(event_id not in seen, "duplicate timeline event")
            seen.add(event_id)
            signature = event["kind"], event["input_digest"]
            require(event.get("occurrence", occurrences[signature]) == occurrences[signature],
                    "incorrect timeline occurrence")
            occurrences[signature] += 1
            position = event["t_ms"]
            if "t_ticks" in event:
                require("time_base" in side, "timeline ticks require a time base")
                base = side["time_base"]
                position = Fraction(event["t_ticks"] * base["num"] * 1000, base["den"])
                require(event["t_ms"] == position // 1, "incorrect timeline tick projection")
            require(previous is None or previous <= position, "timeline events are out of order")
            previous = position
        # Frozen events have ASCII keys and safe unsigned integers. Unicode string
        # values use JSON's UTF-8 form without ASCII escapes in the JCS domain.
        try:
            canonical = json.dumps(events, ensure_ascii=False, allow_nan=False, sort_keys=True,
                                   separators=(",", ":")).encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ContractError("timeline contains a non-Unicode string") from exc
        require((raw is None or raw == canonical) and digest(canonical) == side["timeline_sha256"],
                "timeline does not match its canonical digest")
        if raw is not None:
            # Replacing the reference with the decoded events enables the frozen
            # record to assert the same event and time-base schemas.
            replacement = dict(side, events=events)
            del replacement["events_object_ref"]
            record = dict(record, timeline={**record["timeline"], name: replacement})
    return record


def artifact_index(validator, admission, start, terminal, raw, uploads, read_object, validate_profile):
    artifact = terminal["artifact"]
    media = terminal.get("media", {})
    compare = admission["operation"] == "compare"
    require(len(raw) == artifact["bytes"] and digest(raw) == artifact["sha256"],
            "artifact byte count or digest mismatch")
    if artifact["content_kind"] == "inline_payload":
        require(compare or not media, "recording bytes require an object index")
        validate_profile(admission, strict_json(raw), terminal)
        expanded = verify_timeline(terminal, lambda *_: require(False, "unexpected event reference"))
        return {}, expanded

    index = strict_json(raw)
    validator.validate_index(index)
    require(all(index[field] == admission[field] for field in INDEX_PINS)
            and index.get("admission_sha256", admission["admission_sha256"])
            == admission["admission_sha256"], "index does not bind the admission")
    for field in ("episode_id", "parent_episode_id"):
        if field in index:
            require(all(index[field] == record[field] for record in (start, terminal)
                        if field in record), f"index changed {field}")
    policy = terminal.get("verdict", {}).get("policy", {})
    if "policy_sha256" in index and "sha256" in policy:
        require(index["policy_sha256"] == policy["sha256"], "index changed the verdict policy")
    require("reconciles_attempt_id" not in index, "index reconciliation is not supported")
    objects = {entry["object_ref"]: entry for entry in index["objects"]}
    by_role = {}
    for ref, entry in objects.items():
        require(ref in uploads and all(entry[key] == uploads[ref][key]
                                       for key in ("bytes", "sha256")),
                "object does not bind this attempt's verified upload")
        role = entry["role"]
        side = entry.get("side")
        group = role, side
        by_role.setdefault(group, []).append(entry)
    profile = by_role[("rea_profile", None)][0]
    validate_profile(admission, strict_json(read_object(profile["object_ref"], 33_554_432)), terminal)

    def events(side, ref):
        require(ref in objects and objects[ref] in by_role.get(("timeline_events", side), ()),
                "events reference does not name its own side's timeline object")
        return read_object(ref, 33_554_432)

    timeline = terminal.get("timeline", {})
    for (role, side), group in by_role.items():
        if role == "timeline_events":
            require(len(group) == 1 and side in timeline
                    and group[0]["sha256"] == timeline[side]["timeline_sha256"],
                    "timeline object lacks a unique matching terminal side")
        elif role == "recording":
            require(len(group) == 1 and side in media
                    and group[0]["sha256"] == media[side]["recording_sha256"],
                    "recording object lacks a unique matching terminal side")
    require(compare or all(("recording", side) in by_role for side in media),
            "terminal media requires matching recording bytes")
    expanded = verify_timeline(terminal, events)
    validator.validate("Terminal", expanded)
    return objects, expanded
