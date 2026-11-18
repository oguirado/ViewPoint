"""Validate a sealed factory trace against the harness's expected capture manifest."""

import argparse
from collections import defaultdict
from functools import lru_cache
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from .recorder import (
    BUILD_PATTERN, DIGEST_PATTERN, PIN_PATH, PSI_FIELDS, SCHEMA_PATH, SESSION_PATTERN,
    PressureSample, pressure_ppm,
)


# Required effective CPU placement and absolute niceness from the S-TORO contract.
_CPUS = list(range(16))
_CPUSET = ",".join(map(str, _CPUS))
_PLACEMENT = {"nice": 15, "cpus": _CPUS}
_DROP_FIELDS = ("ring_drops", "unregistered_drops", "invalid_drops")
_SPAN_KEYS = frozenset((
    "process_id", "toro_session", "operation", "stream_id", "parent_span_id",
    "stage", "worker", "worker_generation", "test_id", "request_id", "phase", "clock_id",
))
_LEG_KEYS = ("arm_id", "pair_id", "variant", "request_id", "workload_sha256", "root_span_id")
_TIMING_KEYS = ("start_ns", "end_ns", "clock_id", "placement_entry", "placement_work",
                "psi", "psi_pre")
_OFF_ARMS = ("TRACE-job", "TRACE-gate")


class TraceError(ValueError):
    """A capture or its independent authority failed a named gate."""


def _require(condition, reason):
    if not condition:
        raise TraceError(reason)


def _unique_object(pairs):
    result = dict(pairs)
    _require(len(pairs) == len(result), "duplicate_json_key")
    return result


def _bad_number(_value):
    raise TraceError("non_integer_json_number")


def _load(data):
    return json.loads(data, object_pairs_hook=_unique_object, parse_float=_bad_number,
                      parse_constant=_bad_number)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _matches(pattern, value):
    return isinstance(value, str) and bool(pattern.fullmatch(value))


def _number(value, minimum=0):
    return type(value) is int and value >= minimum


@lru_cache(maxsize=1)
def _validator(schema_bytes):
    schema = _load(schema_bytes)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _validate_timing(span):
    start, end = span.get("start_ns"), span.get("end_ns")
    _require(_number(start) and _number(end, start + 1), "wall")
    _require(_text(span.get("clock_id")), "clock")
    for field in ("placement_entry", "placement_work"):
        effective = span.get(field)
        _require(isinstance(effective, dict) and effective == _PLACEMENT
                 and type(effective["nice"]) is int
                 and all(type(cpu) is int for cpu in effective["cpus"]), "placement")
    for field in ("psi", "psi_pre"):
        pressure = span.get(field)
        _require(isinstance(pressure, dict) and set(pressure) == set(PSI_FIELDS), "missing_psi")
        for item in pressure.values():
            _require(isinstance(item, dict) and set(item) == {"value", "absence_reason"}
                     and _number(item["value"]) and item["value"] <= 1_000_000
                     and item["absence_reason"] is None, "missing_psi")


def _read_capture(path, capture, manifest, spans, validator):
    process_id = capture["process_id"]
    sequences = defaultdict(int)
    checkpoint = None
    events = 0
    last_timestamp = 0
    with path.open("rb") as stream:
        for raw in stream:
            _require(raw.endswith(b"\n"), "truncated_jsonl")
            row = _load(raw)
            invalid = next(validator.iter_errors(row), None)
            if invalid is not None:
                raise TraceError(f"schema_invalid: {invalid.json_path}")
            _require(checkpoint is None, "row_after_final")
            _require(row["session_id"] == manifest["session_id"]
                     and row["process_id"] == process_id, "capture_join")
            _require(row["git_sha"] == manifest["git_sha"], "build")
            _require(row["cpuset"] == _CPUSET, "placement")
            timestamp = row["timestamp_ns"]
            _require(timestamp >= last_timestamp, "clock_reset")
            last_timestamp = timestamp
            if row["kind"] == "checkpoint":
                _require(not any(row[key] for key in _DROP_FIELDS), "capture_drops")
                _require(row["record_count"] == events, "checkpoint_count")
                if row["final"]:
                    checkpoint = row
                continue

            thread = row["thread"]
            sequences[thread] += 1
            events += 1
            _require(row["sequence"] == sequences[thread], "sequence_gap")
            extension = row.get("extension")
            if extension is not None:
                _add_span(row, extension, manifest, spans)
    _require(checkpoint is not None, "missing_final")
    _require(events == capture["event_count"], "event_count")
    return events


def _add_span(row, span, manifest, spans):
    _require(span["dropped"] == 0, "capture_drops")
    _require(span["contract_pins_sha256"] == manifest["contract_pins_sha256"], "pins")
    _validate_timing(span)
    _require(_number(span["cpu_ns"]["value"], 1)
             and _number(span["peak_rss_bytes"]["value"], 1), "missing_resource_metric")
    _require(span["end_ns"] <= row["timestamp_ns"], "span_clock")
    _require(span["copied_bytes"]["value"] == row["copy_bytes"], "copy_binding")
    _require((span["worker"] is None) == (span["worker_generation"] is None)
             and (row["operation"] != "test" or span["test_id"] is not None), "span_identity")

    span_id = span["span_id"]
    _require(span_id not in spans, "duplicate_span")
    expected = manifest["spans"].get(span_id)
    physical = {key: row[key] for key in ("process_id", "toro_session", "operation")}
    observed = {key: (physical if key in physical else span)[key] for key in _SPAN_KEYS}
    _require(expected == observed, "span_join")
    spans[span_id] = span


def _leg_key(leg):
    return tuple(leg[key] for key in ("arm_id", "pair_id", "variant"))


def _raw_pressure_samples(witness, field):
    values = witness.get(field)
    _require(isinstance(values, list) and len(values) == 2, "raw_pressure")
    samples = []
    for value in values:
        _require(isinstance(value, dict)
                 and set(value) in ({"timestamp_ns", "some_us"},
                                    {"timestamp_ns", "some_us", "read_start_ns"})
                 and _number(value["timestamp_ns"]) and isinstance(value["some_us"], list)
                 and len(value["some_us"]) == len(PSI_FIELDS)
                 and all(_number(total) for total in value["some_us"]), "raw_pressure")
        read_start = value.get("read_start_ns")
        _require(read_start is None or (_number(read_start) and
                 read_start <= value["timestamp_ns"]), "raw_pressure")
        samples.append(PressureSample(value["timestamp_ns"], tuple(value["some_us"]), read_start))
    return samples


def _verify_raw_pressure(witness):
    work = _raw_pressure_samples(witness, "pressure_samples")
    pre = _raw_pressure_samples(witness, "pressure_pre_samples")
    _require(work[0].timestamp_ns <= witness["start_ns"]
             and witness["end_ns"] <= work[1].timestamp_ns
             and pre[1].timestamp_ns <= witness["start_ns"]
             and pressure_ppm(work) == witness["psi"]
             and pressure_ppm(pre) == witness["psi_pre"], "raw_pressure")


def _read_legs(directory, manifest, spans):
    pairs = defaultdict(dict)
    legs = {}
    for leg in manifest["legs"]:
        _require(all(_text(leg.get(key)) for key in _LEG_KEYS[:-1])
                 and leg["variant"] in ("A", "B")
                 and _matches(DIGEST_PATTERN, leg["workload_sha256"]), "leg_manifest")
        key = _leg_key(leg)
        _require(key not in legs, "duplicate_leg")
        legs[key] = leg
        root_id = leg["root_span_id"]
        if root_id is None:
            _require(leg["arm_id"] in _OFF_ARMS and leg["variant"] == "A", "missing_root")
        else:
            root = spans.get(root_id)
            _require(root is not None and root["phase"] == "end_to_end"
                     and root["parent_span_id"] is None and _leg_key(root) == key
                     and root["request_id"] == leg["request_id"], "root_join")
        raw = (directory / leg["raw_evidence_path"]).read_bytes()
        _require(hashlib.sha256(raw).hexdigest() == leg["raw_evidence_sha256"], "raw_evidence")
        witness = _load(raw)
        _require(all(field in witness and witness[field] == leg[field] for field in _LEG_KEYS)
                 and all(witness.get(field) == manifest[field] for field in
                         ("session_id", "git_sha", "contract_pins_sha256")), "raw_evidence_join")
        _validate_timing(witness)
        _verify_raw_pressure(witness)
        if root_id is not None:
            _require(witness["clock_id"] == root["clock_id"]
                     and witness["start_ns"] <= root["start_ns"] < root["end_ns"] <= witness["end_ns"],
                     "root_evidence_clock")
        pairs[key[:2]][key[2]] = (leg, witness)
    return legs, pairs


def _verify_span_tree(spans, legs):
    complete = set()
    for origin in spans:
        cursor = origin
        visited = set()
        while cursor not in complete:
            _require(cursor not in visited, "span_cycle")
            visited.add(cursor)
            span = spans[cursor]
            leg = legs.get(_leg_key(span))
            _require(leg is not None and leg["request_id"] == span["request_id"], "leg_join")
            parent_id = span["parent_span_id"]
            if parent_id is None:
                _require(cursor == leg["root_span_id"] and span["phase"] == "end_to_end",
                         "root_join")
                break
            parent = spans.get(parent_id)
            _require(parent is not None and span["phase"] != "end_to_end"
                     and _leg_key(parent) == _leg_key(span)
                     and parent["request_id"] == span["request_id"]
                     and parent["clock_id"] == span["clock_id"]
                     and parent["start_ns"] <= span["start_ns"] < span["end_ns"] <= parent["end_ns"],
                     "parent_join")
            if parent["worker"] is not None and parent["worker"] == span["worker"]:
                _require(parent["worker_generation"] == span["worker_generation"], "worker_join")
            if parent["test_id"] is not None and span["test_id"] is not None:
                _require(parent["test_id"] == span["test_id"], "test_join")
            cursor = parent_id
        complete.update(visited)


def analyse(manifest_path, *, pins_path=PIN_PATH):
    """Return paired independent wall times after every declared capture passes.

    The orchestration caller owns the manifest, including producer counts, span
    coverage and hashes of the independent leg evidence. Never derive that
    authority from the telemetry JSONL being checked.
    """
    try:
        manifest_path = Path(manifest_path)
        manifest = _load(manifest_path.read_bytes())
        _require(_matches(SESSION_PATTERN, manifest["session_id"])
                 and _matches(BUILD_PATTERN, manifest["git_sha"]), "manifest_identity")
        pins_bytes = Path(pins_path).read_bytes()
        _require(hashlib.sha256(pins_bytes).hexdigest() == manifest["contract_pins_sha256"],
                 "pins")
        pinned_schema = _load(pins_bytes)["boundaries"]["telemetry"]
        schema_bytes = SCHEMA_PATH.read_bytes()
        _require(hashlib.sha256(schema_bytes).hexdigest() == pinned_schema["sha256"],
                 "schema_pins")
        validator = _validator(schema_bytes)
        _require(validator.schema["$id"] == pinned_schema["schema_id"], "schema_pins")
        captures = manifest["captures"]
        _require(isinstance(captures, list) and captures and isinstance(manifest["spans"], dict)
                 and isinstance(manifest["legs"], list), "capture_manifest")
        processes = set()
        files = set()
        event_count = 0
        spans = {}
        for capture in captures:
            process_id = capture["process_id"]
            path = (manifest_path.parent / capture["path"]).resolve(strict=True)
            _require(_number(process_id, 1) and _number(capture["event_count"])
                     and process_id not in processes and path not in files, "capture_manifest")
            processes.add(process_id)
            files.add(path)
            event_count += _read_capture(path, capture, manifest, spans, validator)
        _require(spans and set(manifest["spans"]) == set(spans), "missing_span")
        legs, pairs = _read_legs(manifest_path.parent, manifest, spans)
        _require(pairs and all(set(pair) == {"A", "B"} for pair in pairs.values()), "incomplete_pair")
        _verify_span_tree(spans, legs)

        results = []
        for (arm, pair_id), pair in sorted(pairs.items()):
            leg_a, a = pair["A"]
            leg_b, b = pair["B"]
            _require(leg_a["workload_sha256"] == leg_b["workload_sha256"], "paired_workload")
            _require(a["clock_id"] == b["clock_id"]
                     and (a["end_ns"] <= b["start_ns"] or b["end_ns"] <= a["start_ns"]),
                     "paired_clock")
            results.append({"arm_id": arm, "pair_id": pair_id,
                            "wall_a_ns": a["end_ns"] - a["start_ns"],
                            "wall_b_ns": b["end_ns"] - b["start_ns"]})
        return {"event_count": event_count, "span_count": len(spans), "pairs": results}
    except (OSError, KeyError, TypeError, AttributeError, json.JSONDecodeError, UnicodeError) as exc:
        raise TraceError(f"capture_input: {exc}") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--pins", type=Path, default=PIN_PATH)
    args = parser.parse_args(argv)
    try:
        result = analyse(args.manifest, pins_path=args.pins)
    except TraceError as exc:
        print(json.dumps({"verdict": "FAIL", "reason": str(exc)}))
        return 5
    print(json.dumps({"verdict": "PASS", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
