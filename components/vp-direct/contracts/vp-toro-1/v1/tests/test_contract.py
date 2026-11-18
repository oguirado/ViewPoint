# SPDX-License-Identifier: AGPL-3.0-only
"""Offline contract oracles using synthetic observations and capture rows."""

from array import array
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import errno
import fcntl
from hashlib import sha256
import json
import math
import mmap
import os
from pathlib import Path
import random
import socket
import statistics
import sys
from threading import Lock
import unittest
from unittest.mock import Mock, patch

from jsonschema import Draft202012Validator, ValidationError


ROOT = Path(__file__).resolve().parents[1]
AMOUNTS = ("logical_bytes", "copy_bytes", "dram_bytes", "nvme_bytes", "wait_ns")
DROP_FIELDS = ("ring_drops", "unregistered_drops", "invalid_drops")
CAUSE_KEY = ("process_id", "thread", "sequence", "field")
REQUIRED_SEALS = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW
VP_OPERATIONS = (
    "capture digest privacy_check serve observe infer plan act verify receipt "
    "admit refuse wake sleep drain record rewind_restore encode"
).split()
TORO_OPERATIONS = (
    "open close lane_create subscribe reserve publish take borrow cpu_view "
    "release credit revoke pin unpin hash copy io retain"
).split()

ENDPOINTS = {
    "P": ("identical_work_start", "synchronous_work_and_required_receipt_completion",
          "verified_receipts_per_second", "higher", "independent_outer_elapsed_seconds"),
    "O": ("scheduled_protected_observation_start", "first_eligible_protected_result",
          "p95", "lower", "all_scheduled_attempts"),
    "W": ("attempt_start_before_reservation", "first_valid_committed_receipt",
          "p95", "lower", "all_starts"),
    "R": ("scheduled_wake_request", "first_protected_result_and_committed_receipt",
          "p95", "lower", "all_scheduled_wakes"),
    "Z": ("handoff_of_prepared_payload_or_ticket", "ack_after_protected_use_and_release",
          "p95", "lower", "all_scheduled_handoffs"),
    "U": ("capture_work_start", "synchronous_verified_frame_completion",
          "participating_cgroup_cpu_seconds_per_verified_frame", "lower", "verified_frames"),
    "T-impact": ("suite_launch", "suite_results_complete", "wall_seconds", "lower", "scheduled_suite_runs"),
    "T-shards": ("suite_launch", "suite_results_complete", "wall_seconds", "lower", "scheduled_suite_runs"),
}
BUDGETS = {
    "scope_incremental_ns": (41, "complete_enabled_scope",
                             "calibrated_complete_enabled_elapsed_minus_matched_empty_elapsed_ns"),
    "instrument_share_pct": (1, "independent_enabled_outer_elapsed_ns",
                             "100 * instrument_attributable_elapsed_ns"),
    "verified_throughput_loss_pct": (2, "disabled_verified_receipts_per_outer_second",
                                     "100 * (disabled_rate - enabled_rate)"),
}
PRESSURE = {
    "psi_cpu_some_avg10_pct": "cpu_some_avg10_pct",
    "psi_memory_full_avg10_pct": "memory_full_avg10_pct",
    "psi_io_full_avg10_pct": "io_full_avg10_pct",
}
REQUIRED_SAMPLE = {*PRESSURE, "thermal_celsius", "viewpoint_cpuset", "factory_cpuset",
                   "factory_cgroups", "useful_work"}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def read_json(path):
    return json.loads((ROOT / path).read_bytes(), object_pairs_hook=unique_keys)


def assert_schema_pin(raw, members):
    require(sha256(raw).hexdigest() == members.get("candidate.schema.json"), "changed schema")


def assert_endpoint(arm, endpoint):
    fields = ("start", "stop", "statistic", "direction", "denominator")
    expected = dict(zip(fields, ENDPOINTS[arm], strict=True))
    expected.update(clock="independent_outer_monotonic", completion="synchronous")
    require(endpoint == expected, "swapped timer or endpoint")


def assert_budgets(protocol):
    fields = ("upper_max", "denominator", "numerator")
    require(protocol["budgets"] == {
        key: dict(zip(fields, value, strict=True)) for key, value in BUDGETS.items()
    }, "softened budget or changed denominator")
    require(protocol["budget_statistic"] ==
            "upper paired confidence bound; all applicable bounds must pass per profile",
            "changed budget statistic")


def assert_thresholds(protocol, rust, observe):
    decision = protocol["decision"]
    require((decision["keep_lower_gain_pct_min"], decision["discard_upper_gain_pct_max"],
             decision["marginal_gain_interval_pct"]) == (5, -5, [-5, 5]), "changed decision threshold")
    require(protocol["acceptance"]["T1"]["min_verified_pct"] == 95, "changed T1 threshold")
    require(protocol["acceptance"]["T2"] == {"protected_observation_p95_ms_max": 500,
                                             "absolute_gate_separate": True} and
            observe["absolute_t2_gate_ms"] == 500, "changed T2 threshold")
    require(rust["protected_tail_regression_upper_pct_max"] == 2, "changed U threshold")


def measured(value):
    return type(value) in (float, int) and math.isfinite(value) and value >= 0


def covariate_rates(samples, controls, cgroups, elapsed):
    require(measured(elapsed) and elapsed > 0, "missing covariate: elapsed time")
    require(cgroups and set(samples) == {"arm_start", "arm_end"}, "missing covariate: boundaries or factory")
    for sample in samples.values():
        require(REQUIRED_SAMPLE <= sample.keys(), "missing covariate")
        require(all(measured(sample[name]) for name in (*PRESSURE, "useful_work")), "missing covariate value")
        require(sample["thermal_celsius"] and all(measured(v) for v in sample["thermal_celsius"].values()),
                "missing thermal evidence")
        require(sample["viewpoint_cpuset"] == controls["timed_viewpoint_cpus"] and
                sample["factory_cpuset"] == controls["factory_cpus"], "invalid affinity")
        require(all(sample[name] <= controls["pressure_abort_ceilings"][key]
                    for name, key in PRESSURE.items()), "pressure abort")
        require(set(sample["factory_cgroups"]) == set(cgroups), "missing or unexpected factory cgroup")
        for counters in sample["factory_cgroups"].values():
            require(all(measured(counters.get(name)) for name in ("work_total", "throttle_total")),
                    "missing covariate: factory counter")
    start, end = samples["arm_start"], samples["arm_end"]
    require(start["thermal_celsius"].keys() == end["thermal_celsius"].keys(), "thermal attribution")
    require(end["useful_work"] >= start["useful_work"], "invalid useful work delta")
    rates = {}
    for cgroup in cgroups:
        for name in ("work_total", "throttle_total"):
            delta = end["factory_cgroups"][cgroup][name] - start["factory_cgroups"][cgroup][name]
            require(delta >= 0, "invalid factory counter delta")
            rates[cgroup, name] = delta / elapsed
    require(any(value > 0 for (_, name), value in rates.items() if name == "work_total"), "idle factory")
    return rates


def assert_matched(a, b, tolerance):
    require(a.keys() == b.keys(), "mismatched counter coverage")
    for key, value in a.items():
        other = b[key]
        require(measured(value) and measured(other), "unmeasured competitor counter")
        denominator = max(abs(value), abs(other))
        if denominator == 0:
            require(key[1] == "throttle_total", "zero competitor work")
        else:
            require(100 * abs(value - other) / denominator <= tolerance, "unmatched factory rate")


def decode_rows(raw):
    require(raw.endswith(b"\n"), "torn JSONL")
    return [json.loads(line, object_pairs_hook=unique_keys) for line in raw.splitlines()]


def assert_canonical_cpuset(cpuset):
    if cpuset is None:
        raise ValueError("noncanonical cpuset")
    ids = [int(cpu) for cpu in cpuset.split(",")]
    require(cpuset == ",".join(str(cpu) for cpu in sorted(set(ids))), "noncanonical cpuset")


def assert_cause_entry(cause, session):
    require(cause.keys() == {*CAUSE_KEY, "session_id", "cause"}, "invalid sidecar cause fields")
    require(cause["session_id"] == session and
            all(type(cause[field]) is int for field in CAUSE_KEY[:-1]) and
            cause["field"] in AMOUNTS and
            cause["cause"] in ("unavailable", "not_applicable"), "invalid sidecar cause")


def validate_capture(rows, fixture, validator):
    roster = fixture["emitters"]
    counts, sequences = Counter(), Counter()
    producer_clocks, checkpoint_clocks, latest_clocks = {}, {}, {}
    closed, missing_causes, scope_clocks = set(), set(), {}
    for row in rows:
        validator.validate(row)
        assert_canonical_cpuset(row["cpuset"])
        pid = str(row["process_id"])
        require(row["session_id"] == fixture["session_id"] and pid in roster, "foreign row")
        require(pid not in closed, "row after close")
        emitter = roster[pid]
        require(all(row[field] is not None and row[field] == emitter[field]
                    for field in ("git_sha", "cpuset")), "emitter attribution mismatch")
        ns = row["timestamp_ns"]
        require(ns >= checkpoint_clocks.get(pid, 0), "clock before checkpoint")
        if row["kind"] == "checkpoint":
            require(row["record_count"] == counts[pid], "checkpoint count")
            require(not any(row[field] for field in DROP_FIELDS), "telemetry loss")
            require(ns >= latest_clocks.get(pid, 0), "checkpoint before event")
            checkpoint_clocks[pid] = ns
            if row["final"]:
                closed.add(pid)
            continue

        thread = str(row["thread"])
        producer = (pid, thread)
        require(row["component"] in emitter["components"] and thread in emitter["producers"],
                "unexpected component or producer")
        require(row["sequence"] == sequences[producer] + 1, "sequence gap or duplicate")
        require(ns >= producer_clocks.get(producer, 0), "backward producer clock")
        counts[pid] += 1
        sequences[producer] += 1
        producer_clocks[producer] = ns
        latest_clocks[pid] = max(ns, latest_clocks.get(pid, 0))
        key = (row["process_id"], row["thread"], row["sequence"])
        scope_clocks[key] = ns
        missing_causes.update((*key, field) for field in AMOUNTS if row[field] is None)

    require(closed == set(roster), "missing final close")
    for pid, emitter in roster.items():
        for thread, accounting in emitter["producers"].items():
            require(type(accounting["drops"]) is int and accounting["drops"] == 0 and
                    type(accounting["calls"]) is int and type(accounting["admitted"]) is int and
                    0 < accounting["calls"] == accounting["admitted"] == sequences[pid, thread],
                    "missing producer or unreconciled accounting")

    boundaries = fixture["joined_scope"]
    clocks = {}
    for name, boundary in boundaries.items():
        key = tuple(boundary[field] for field in ("process_id", "thread", "sequence"))
        require(key in scope_clocks, "missing clock boundary")
        clocks[name] = scope_clocks[key]
    require(clocks["stop"] >= clocks["start"], "invalid joined clock interval")

    causes = set()
    for cause in fixture["causes"]:
        assert_cause_entry(cause, fixture["session_id"])
        key = tuple(cause[field] for field in CAUSE_KEY)
        require(key not in causes, "duplicate sidecar cause")
        causes.add(key)
    require(causes == missing_causes, "nullable counter cause mismatch")
    return sum(counts.values())


def scheduled_quantile(values, deadlines, quantile):
    """Deadlines are right-censoring lower bounds for missing completions."""
    observed = sorted(value for value in values if value is not None)
    rank = math.ceil(quantile * len(values))
    if not rank or len(observed) < rank:
        return None
    estimate = observed[rank - 1]
    if any(value is None and deadline < estimate for value, deadline in zip(values, deadlines, strict=True)):
        return None
    return estimate


def block_gains(blocks, draws, seed, statistic):
    rng = random.Random(seed)
    gains = []
    for _ in range(draws):
        sample = [blocks[rng.randrange(len(blocks))] for _ in blocks]
        a, b = (statistic(part for block in sample for part in block[side]) for side in (0, 1))
        require(a is not None and a > 0 and b is not None, "unidentified matched resample")
        gains.append(100 * (a - b) / a)
    return gains


def pooled_p95(observations):
    values, deadlines = zip(*observations, strict=True)
    return scheduled_quantile(values, deadlines, .95)


def pooled_ratio(observations):
    numerators, denominators = zip(*observations, strict=True)
    denominator = sum(denominators)
    return sum(numerators) / denominator if denominator > 0 else None


def require_recipient(policy, role, evidence, frame, clearance):
    require(all(evidence.get(key) is True for key in policy["common_requirements"]),
            "retain frame in S5: handoff prerequisites")
    if role in policy["staged_roles"] and all(
        evidence.get(key) is True for key in policy["staging_requirements"]
    ):
        return
    require(clearance is not None and clearance.get("verdict") == policy["clearance_verdict"] and
            all(key in frame and key in clearance and frame[key] is not None and
                clearance[key] == frame[key] for key in policy["clearance_binding"]),
            "retain frame in S5: recipient clearance")


def validate_memfd(fd, offset, length):
    require(0 <= offset <= sys.maxsize and 0 <= length <= sys.maxsize - offset,
            "unrepresentable extent")
    require(fcntl.fcntl(fd, fcntl.F_GET_SEALS) & REQUIRED_SEALS == REQUIRED_SEALS,
            "unsealed memfd")
    require(os.fstat(fd).st_size == offset + length, "wrong backing extent")


@contextmanager
def fixture_memfd(payload, seals=REQUIRED_SEALS):
    fd = os.memfd_create("vp-bulk-contract-fixture", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        os.write(fd, payload)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, seals)
        yield fd
    finally:
        os.close(fd)


def validate_view(data, length):
    require(0 <= length <= sys.maxsize, "unrepresentable view length")
    require(data is not None or length == 0, "null nonempty view")
    if length:
        require(data.readonly and len(data) >= length, "invalid view backing")
    return memoryview(b"") if length == 0 else data[:length]


class FixtureLease:
    """A contract witness with a real mapping and a separately issued view."""

    def __init__(self, fd, length):
        validate_memfd(fd, 0, length)
        self.mapping = mmap.mmap(fd, length, access=mmap.ACCESS_READ)
        self.lock = Lock()
        self.broken = False
        self.released = False

    def cpu_view(self):
        with self.lock:
            require(not self.broken and not self.released, "broken or stale lease")
            return memoryview(self.mapping)

    def break_and_drain(self):
        with self.lock:
            self.broken = True
            return "ok" if self.released else "again"

    def release(self):
        with self.lock:
            require(not self.released, "already released")
            self.mapping.close()
            self.released = True


def fixture_publish(blocks, available, before_commit):
    """Model a provider that commits a prefix and owns only its duplicates."""
    prefix = blocks[:available]
    duplicates = []
    try:
        for kind, fd, length in prefix:
            if kind == "end":
                require(fd == -1 and length == 0, "malformed END")
            else:
                require(kind in ("bytes", "frame") and fd >= 0 and (length > 0 or kind == "bytes"),
                        "malformed payload")
                duplicate = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 0)
                duplicates.append(duplicate)
                validate_memfd(duplicate, 0, length)
        status = before_commit(duplicates)
        require(status in ("ok", "invalid", "deadline", "broken", "stale"), "invalid publish status")
        if status == "ok":
            result, duplicates = duplicates, []
            return ("ok" if len(prefix) == len(blocks) else "again"), len(prefix), result
        return status, 0, []
    except ValueError:
        return "invalid", 0, []
    except RuntimeError:
        return "broken", 0, []
    finally:
        for fd in duplicates:
            os.close(fd)


def fixture_backend(mode, load_pinned, negotiate, open_provider):
    if mode in ("off", "observe"):
        return "normal", None
    require(mode == "on", "invalid mode")
    provider = load_pinned()
    if provider is None:
        return "normal", "provider_absent"
    require(negotiate(provider) == "ok", "provider ABI mismatch")
    result = open_provider(provider)
    if result == "refused_before_custody":
        return "normal", "verified_open_refused"
    require(result == "ok", "provider session broken")
    return "provider", None


def custody_trace(operations):
    outstanding, used, released, receipts = set(), set(), set(), set()
    broken = False
    for action, identity in operations:
        if action == "borrow":
            require(not broken and identity not in outstanding | released, "invalid borrow")
            outstanding.add(identity)
        elif action == "use":
            require(not broken and identity in outstanding, "invalid use")
            used.add(identity)
        elif action == "receipt":
            require(not broken and identity in used and identity not in receipts, "duplicate or late receipt")
            receipts.add(identity)
        elif action in ("timeout", "child_death"):
            broken = True
        elif action == "release":
            require(identity in outstanding, "invalid release")
            outstanding.remove(identity)
            released.add(identity)
        elif action == "late_reply":
            require(broken and identity in outstanding | released, "invalid late reply")
        else:
            raise ValueError("unknown custody action")
    require(not outstanding, "undrained session")
    return receipts, released


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = read_json("manifest.json")
        cls.protocol = read_json("protocol.json")
        cls.schema_raw = (ROOT / "candidate.schema.json").read_bytes()
        cls.schema = json.loads(cls.schema_raw)
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)
        cls.raw = (ROOT / "fixtures/join.jsonl").read_bytes()
        cls.rows = decode_rows(cls.raw)
        cls.fixture = read_json("fixtures/nullable-causes.json")

    def test_frozen_bytes(self):
        paths = {"CONTRACT.md", "candidate.schema.json", "bulk_v1.h", "protocol.json", "sources.json",
                 "fixtures/join.jsonl", "fixtures/nullable-causes.json", "fixtures/psi-preflight.json",
                 "tests/abi_layout.c", "tests/test_contract.py"}
        paths.update(f"arms/{arm}.json" for arm in ENDPOINTS)
        actual = {item.relative_to(ROOT).as_posix() for item in ROOT.rglob("*") if item.is_file()}
        self.assertEqual(actual, paths | {"manifest.json"})
        self.assertEqual(self.manifest["sha256"], {
            path: sha256((ROOT / path).read_bytes()).hexdigest() for path in paths
        })
        self.assertEqual(self.manifest["R9"], "candidate-compatible")
        self.assertIsNone(self.manifest["accepted_schema_sha256"])

    def test_nominal(self):
        assert_schema_pin(self.schema_raw, self.manifest["sha256"])
        assert_budgets(self.protocol)
        assert_thresholds(self.protocol, read_json("arms/U.json"), read_json("arms/O.json"))
        for arm in ENDPOINTS:
            registration = read_json(f"arms/{arm}.json")
            assert_endpoint(arm, registration["endpoint"])
        self.assertEqual(read_json("arms/W.json")["timeout_s"], 30)
        self.assertEqual(read_json("arms/R.json")["cycle_s"], {"active": 60, "idle": 60})
        self.assertEqual(self.protocol["acceptance"]["T1"]["denominator"], "attempts_assigned_to_admitted_surfaces")

    def test_changed_schema(self):
        altered = deepcopy(self.schema)
        altered["title"] += " changed"
        raw = json.dumps(altered).encode("utf-8")
        Draft202012Validator.check_schema(altered)
        Draft202012Validator(altered).validate(self.rows[1])
        with self.assertRaisesRegex(ValueError, "changed schema"):
            assert_schema_pin(raw, self.manifest["sha256"])

    def test_swapped_timer(self):
        nominal = read_json("arms/P.json")["endpoint"]
        for altered in (
            nominal | {"clock": "recorder_clock"},
            nominal | {"completion": "asynchronous", "stop": "queue_submit"},
            nominal | {"start": nominal["stop"], "stop": nominal["start"]},
        ):
            with self.subTest(altered=altered):
                with self.assertRaisesRegex(ValueError, "swapped timer"):
                    assert_endpoint("P", altered)

    def test_missing_covariate(self):
        for name in sorted(REQUIRED_SAMPLE | {"work_total", "throttle_total"}):
            with self.subTest(name=name):
                samples = sample_pair()
                end = samples["arm_end"]
                if name in ("work_total", "throttle_total"):
                    del end["factory_cgroups"]["factory-fixture"][name]
                else:
                    del end[name]
                with self.assertRaisesRegex(ValueError, "missing covariate"):
                    covariate_rates(samples, self.protocol["controls"], ["factory-fixture"], 1)

    def test_softened_budget(self):
        for name in BUDGETS:
            for change in ({"upper_max": BUDGETS[name][0] + 1}, {"denominator": "different_basis"}):
                with self.subTest(name=name, change=change):
                    altered = deepcopy(self.protocol)
                    altered["budgets"][name].update(change)
                    with self.assertRaisesRegex(ValueError, "softened budget"):
                        assert_budgets(altered)

    def test_softened_qualification_thresholds(self):
        rust, observe = read_json("arms/U.json"), read_json("arms/O.json")
        for path, field, value in (
            (("decision",), "keep_lower_gain_pct_min", 4),
            (("decision",), "discard_upper_gain_pct_max", -6),
            (("decision",), "marginal_gain_interval_pct", [-6, 6]),
            (("acceptance", "T1"), "min_verified_pct", 94),
            (("acceptance", "T2"), "protected_observation_p95_ms_max", 501),
        ):
            with self.subTest(field=field):
                altered = deepcopy(self.protocol)
                section = altered
                for key in path:
                    section = section[key]
                section[field] = value
                with self.assertRaisesRegex(ValueError, "changed .* threshold"):
                    assert_thresholds(altered, rust, observe)
        with self.assertRaisesRegex(ValueError, "changed U threshold"):
            assert_thresholds(self.protocol, rust | {"protected_tail_regression_upper_pct_max": 3}, observe)
        with self.assertRaisesRegex(ValueError, "changed T2 threshold"):
            assert_thresholds(self.protocol, rust, observe | {"absolute_t2_gate_ms": 501})

    def test_required_guards_are_discriminating(self):
        cases = (
            ("test_changed_schema", "assert_schema_pin"),
            ("test_swapped_timer", "assert_endpoint"),
            ("test_missing_covariate", "covariate_rates"),
            ("test_softened_budget", "assert_budgets"),
            ("test_softened_qualification_thresholds", "assert_thresholds"),
            ("test_nullable_sidecar_rejects_free_text", "assert_cause_entry"),
            ("test_memfd_recipient_policy", "require_recipient"),
            ("test_memfd_seals_before_mapping", "validate_memfd"),
        )
        for test_name, guard in cases:
            with self.subTest(guard=guard):
                result = unittest.TestResult()
                with patch.object(sys.modules[__name__], guard, return_value=None) as bypass:
                    ContractTests(test_name).run(result)
                bypass.assert_called()
                self.assertEqual(result.testsRun, 1)
                self.assertEqual(result.errors, [])
                self.assertTrue(result.failures)
                for _, failure in result.failures:
                    self.assertIn("ValueError not raised", failure)

    def test_join_vectors(self):
        self.assertEqual(validate_capture(self.rows, self.fixture, self.validator), 5)
        viewpoint, native = self.rows[1], self.rows[2]
        for operation in VP_OPERATIONS:
            self.validator.validate(viewpoint | {"operation": operation})
            with self.assertRaises(ValidationError):
                self.validator.validate(native | {"operation": operation})
        for operation in TORO_OPERATIONS:
            self.validator.validate(native | {"operation": operation})
            with self.assertRaises(ValidationError):
                self.validator.validate(viewpoint | {"operation": operation})
        for change in ({"operation": "fallback"}, {"copy_bytes": 1}, {"unregistered_field": 0}):
            with self.assertRaises(ValidationError):
                self.validator.validate(viewpoint | change)

    def test_join_attribution(self):
        for change, reason in (
            ({"process_id": 999}, "foreign row"),
            ({"session_id": "00000000-0000-4000-8000-000000000099"}, "foreign row"),
            ({"git_sha": "f" * 40}, "attribution mismatch"),
            ({"cpuset": "16"}, "attribution mismatch"),
            ({"component": "local", "operation": "open"}, "unexpected component"),
        ):
            with self.subTest(change=change):
                rows = list(self.rows)
                rows[1] = rows[1] | change
                with self.assertRaisesRegex(ValueError, reason):
                    validate_capture(rows, self.fixture, self.validator)

    def test_canonical_cpuset(self):
        for raw in ("17,16", "16,16", "016,17"):
            with self.subTest(raw=raw):
                rows, fixture = deepcopy((self.rows, self.fixture))
                fixture["emitters"]["101"]["cpuset"] = raw
                for row in rows:
                    if row["process_id"] == 101:
                        row["cpuset"] = raw
                with self.assertRaisesRegex(ValueError, "noncanonical cpuset"):
                    validate_capture(rows, fixture, self.validator)
        with self.assertRaises(ValidationError):
            self.validator.validate(self.rows[1] | {"cpuset": "16-17"})

    def test_loss_gap_close_duplicate_clock(self):
        for index, change, reason in (
            (3, {"sequence": 3}, "sequence gap"),
            (3, {"sequence": 1}, "duplicate"),
            (3, {"timestamp_ns": 99}, "backward producer clock"),
            (6, {"ring_drops": 1}, "telemetry loss"),
            (6, {"record_count": 2}, "checkpoint count"),
            (6, {"timestamp_ns": 129}, "checkpoint before event"),
        ):
            with self.subTest(change=change):
                rows = list(self.rows)
                rows[index] = rows[index] | change
                with self.assertRaisesRegex(ValueError, reason):
                    validate_capture(rows, self.fixture, self.validator)
        with self.assertRaisesRegex(ValidationError, "valid under any"):
            validate_capture(self.rows[:-1] + [self.rows[-1] | {"invalid_drops": None}],
                             self.fixture, self.validator)
        with self.assertRaisesRegex(ValueError, "missing final close"):
            validate_capture(self.rows[:-1], self.fixture, self.validator)
        with self.assertRaisesRegex(ValueError, "row after close"):
            validate_capture(self.rows + [self.rows[1]], self.fixture, self.validator)
        fixture = deepcopy(self.fixture)
        fixture["emitters"]["101"]["producers"]["10"] = {"calls": 1, "admitted": 1, "drops": 0}
        with self.assertRaisesRegex(ValueError, "missing producer"):
            validate_capture(self.rows, fixture, self.validator)
        fixture = self.fixture | {"joined_scope": {
            "start": self.fixture["joined_scope"]["stop"], "stop": self.fixture["joined_scope"]["start"],
        }}
        with self.assertRaisesRegex(ValueError, "invalid joined clock interval"):
            validate_capture(self.rows, fixture, self.validator)
        with self.assertRaisesRegex(ValueError, "torn JSONL"):
            decode_rows(self.raw[:-1])
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            decode_rows(b'{"schema": "duplicate", ' + self.raw[1:])

    def test_nullable_causes(self):
        for causes in (self.fixture["causes"][1:], self.fixture["causes"] + [self.fixture["causes"][0]]):
            with self.assertRaisesRegex(ValueError, "cause"):
                validate_capture(self.rows, self.fixture | {"causes": causes}, self.validator)
        rows = list(self.rows)
        rows[1] = rows[1] | {"dram_bytes": 0}
        with self.assertRaisesRegex(ValueError, "cause mismatch"):
            validate_capture(rows, self.fixture, self.validator)

    def test_nullable_sidecar_rejects_free_text(self):
        for change in ({"title": "synthetic window title"}, {"cause": "https://fixture.invalid/"}):
            with self.subTest(change=change):
                entry, *rest = self.fixture["causes"]
                mutated = self.fixture | {"causes": [entry | change, *rest]}
                with self.assertRaisesRegex(ValueError, "invalid sidecar cause"):
                    validate_capture(self.rows, mutated, self.validator)

    def test_custody(self):
        nominal = [("borrow", 1), ("use", 1), ("receipt", 1), ("release", 1)]
        self.assertEqual(custody_trace(nominal), ({1}, {1}))
        for failure in ("timeout", "child_death"):
            broken = [("borrow", 1), (failure, 1), ("late_reply", 1), ("release", 1)]
            self.assertEqual(custody_trace(broken), (set(), {1}))
            with self.assertRaisesRegex(ValueError, "undrained session"):
                custody_trace(broken[:-1])
            with self.assertRaisesRegex(ValueError, "invalid use"):
                custody_trace(broken[:2] + [("use", 1)])
        with self.assertRaisesRegex(ValueError, "invalid release"):
            custody_trace(nominal + [("release", 1)])
        with self.assertRaisesRegex(ValueError, "duplicate or late receipt"):
            custody_trace(nominal + [("receipt", 1)])

    def test_memfd_recipient_policy(self):
        policy = read_json("arms/Z.json")["recipient_policy"]
        self.assertEqual(policy["on_denial"], "retain_ram_ref_inside_s5")
        requirements = ("authenticated_expected_peer", "matching_owner_and_trust_domain",
                        "current_capture_authorization", "full_extent_including_padding_authorized",
                        "no_prior_frame_tail", "no_recall_or_overwrite_scrub_required")
        roles = ("s5_capture_stage", "s5_capture_staging_checker")
        staging = ("registered_role", "zero_retention", "tracked_settlement")
        evidence = dict.fromkeys(requirements + staging, True)
        frame = {"payload": "synthetic-frame", "recipient": "checker-A", "session": 11, "lease": 12,
                 "generation": 13, "epoch": 14, "observation": 15, "privacy_revision": 16}
        clearance = frame | {"verdict": "CLEAR"}
        deliveries = []

        def handoff(role, checks, receipt):
            require_recipient(policy, role, checks, frame, receipt)
            deliveries.append(frame["recipient"])

        with self.assertRaisesRegex(ValueError, "recipient clearance"):
            handoff("unrelated_checker", evidence, None)
        self.assertEqual(deliveries, [])
        for key in frame:
            with self.subTest(binding=key):
                with self.assertRaisesRegex(ValueError, "recipient clearance"):
                    handoff("unrelated_checker", evidence, clearance | {key: "wrong-binding"})
                missing = clearance.copy()
                del missing[key]
                with self.assertRaisesRegex(ValueError, "recipient clearance"):
                    handoff("unrelated_checker", evidence, missing)
        with self.assertRaisesRegex(ValueError, "recipient clearance"):
            handoff("unrelated_checker", evidence, clearance | {"verdict": "HUMAN_NEEDED"})
        for key in requirements:
            with self.subTest(prerequisite=key):
                with self.assertRaisesRegex(ValueError, "handoff prerequisites"):
                    handoff("unrelated_checker", evidence | {key: False}, clearance)
        for key in staging:
            with self.subTest(staging=key):
                with self.assertRaisesRegex(ValueError, "recipient clearance"):
                    handoff(roles[1], evidence | {key: False}, None)
        self.assertEqual(deliveries, [])
        for role in roles:
            handoff(role, evidence, None)
        handoff("unrelated_checker", evidence, clearance)
        self.assertEqual(deliveries, ["checker-A"] * 3)
        self.assertCountEqual(policy["staged_roles"], roles)

    def test_memfd_seals_before_mapping(self):
        payload = b"frame-canary"
        for seal in (fcntl.F_SEAL_WRITE, fcntl.F_SEAL_SHRINK, fcntl.F_SEAL_GROW):
            with self.subTest(missing_seal=seal), fixture_memfd(payload, REQUIRED_SEALS & ~seal) as fd:
                with patch.object(mmap, "mmap") as mapping:
                    with self.assertRaisesRegex(ValueError, "unsealed memfd"):
                        FixtureLease(fd, len(payload))
                    mapping.assert_not_called()
        with fixture_memfd(payload) as fd:
            with patch.object(mmap, "mmap") as mapping:
                for length in (len(payload) - 1, len(payload) + 1):
                    with self.assertRaisesRegex(ValueError, "wrong backing extent"):
                        FixtureLease(fd, length)
                with self.assertRaisesRegex(ValueError, "unrepresentable extent"):
                    validate_memfd(fd, sys.maxsize, 1)
                mapping.assert_not_called()
            self.assertFalse(fcntl.fcntl(fd, fcntl.F_GET_SEALS) & fcntl.F_SEAL_SEAL)
            for edit in (lambda: os.pwrite(fd, b"!", 0), lambda: os.ftruncate(fd, 1),
                         lambda: os.ftruncate(fd, len(payload) + 1)):
                with self.assertRaises(PermissionError):
                    edit()

    def test_scm_rights_survives_sender_close(self):
        payload = b"synthetic-cleared-frame"
        slots = array("i")
        sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with sender, receiver:
            with fixture_memfd(payload) as original:
                rights = array("i", [original])
                sender.sendmsg([b"!"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)])
                message, ancillary, flags, _ = receiver.recvmsg(
                    1, socket.CMSG_SPACE(rights.itemsize), socket.MSG_CMSG_CLOEXEC)
            try:
                for level, kind, data in ancillary:
                    if (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS):
                        slots.frombytes(data)
                self.assertEqual(message, b"!")
                self.assertFalse(flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC))
                self.assertEqual(len(slots), 1)
                self.assertFalse(os.get_inheritable(slots[0]))
                validate_memfd(slots[0], 0, len(payload))
                with mmap.mmap(slots[0], len(payload), access=mmap.ACCESS_READ) as received:
                    self.assertEqual(received[:], payload)
            finally:
                for fd in slots:
                    os.close(fd)

    def test_empty_and_nonempty_views(self):
        self.assertEqual(bytes(validate_view(None, 0)), b"")
        self.assertEqual(bytes(validate_view(memoryview(b"view"), 4)), b"view")
        for data, length, reason in (
            (None, 1, "null nonempty view"),
            (memoryview(b"x"), sys.maxsize + 1, "unrepresentable view length"),
            (memoryview(b"x"), 2, "invalid view backing"),
            (memoryview(bytearray(b"x")), 1, "invalid view backing"),
        ):
            with self.subTest(length=length, reason=reason):
                with self.assertRaisesRegex(ValueError, reason):
                    validate_view(data, length)

    def test_live_view_survives_concurrent_break(self):
        payload = b"live-view-canary"
        with fixture_memfd(payload) as fd, ThreadPoolExecutor(max_workers=1) as worker:
            lease = FixtureLease(fd, len(payload))
            try:
                with lease.cpu_view() as view:
                    self.assertEqual(worker.submit(lease.break_and_drain).result(timeout=5), "again")
                    self.assertEqual(bytes(view), payload)
                    self.assertFalse(lease.mapping.closed)
                    with self.assertRaisesRegex(ValueError, "broken or stale lease"):
                        lease.cpu_view()
                    self.assertEqual(lease.break_and_drain(), "again")
            finally:
                lease.release()
            self.assertTrue(lease.mapping.closed)
            self.assertEqual(lease.break_and_drain(), "ok")
            self.assertEqual(lease.break_and_drain(), "ok")
            with self.assertRaisesRegex(ValueError, "already released"):
                lease.release()

    def test_drain_reclaim_mutant(self):
        def reclaim_early(lease):
            lease.mapping.close()

        result = unittest.TestResult()
        with patch.object(FixtureLease, "break_and_drain", reclaim_early):
            ContractTests("test_live_view_survives_concurrent_break").run(result)
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(result.failures, [])
        self.assertEqual(len(result.errors), 1)
        self.assertIn("BufferError", result.errors[0][1])

    def test_publish_prefix_and_fd_ownership(self):
        payload = b"publish-canary"
        owned = []

        def close_owned():
            for fd in owned:
                os.close(fd)

        def commit(_pending):
            return "ok"

        self.addCleanup(close_owned)
        with fixture_memfd(payload) as original, fixture_memfd(b"") as empty:
            blocks = [("bytes", original, len(payload)), ("bytes", empty, 0), ("end", -1, 0)]
            self.assertEqual(fixture_publish(blocks, 0, commit), ("again", 0, []))
            status, consumed, fds = fixture_publish(blocks, 1, commit)
            owned.extend(fds)
            self.assertEqual((status, consumed, len(fds)), ("again", 1, 1))
            status, consumed, fds = fixture_publish(blocks[1:], 2, commit)
            owned.extend(fds)
            self.assertEqual((status, consumed, len(fds)), ("ok", 2, 1))
            self.assertEqual(fixture_publish([], 0, commit), ("ok", 0, []))
            self.assertEqual(os.pread(original, len(payload), 0), payload)
            self.assertEqual(os.fstat(empty).st_size, 0)
            self.assertTrue(all(not os.get_inheritable(fd) for fd in owned))
        self.assertEqual(os.pread(owned[0], len(payload), 0), payload)
        self.assertEqual(os.fstat(owned[1]).st_size, 0)

    def test_publish_rollback_and_panic_containment(self):
        payload = b"rollback-canary"
        with fixture_memfd(payload) as original:
            block = ("bytes", original, len(payload))
            for status in ("invalid", "deadline", "broken", "stale", "panic"):
                with self.subTest(status=status):
                    seen = []

                    def fail_after_dup(fds):
                        seen.extend(fds)
                        if status == "panic":
                            raise RuntimeError("synthetic panic")
                        return status

                    expected = "broken" if status == "panic" else status
                    self.assertEqual(fixture_publish([block], 1, fail_after_dup), (expected, 0, []))
                    self.assertEqual(len(seen), 1)
                    with self.assertRaises(OSError) as error:
                        os.fstat(seen[0])
                    self.assertEqual(error.exception.errno, errno.EBADF)
                    self.assertEqual(os.pread(original, len(payload), 0), payload)
            for bad in (("bytes", -1, 0), ("end", original, 0), ("end", -1, 1),
                        ("frame", original, 0)):
                commit = Mock()
                self.assertEqual(fixture_publish([block, bad], 2, commit), ("invalid", 0, []))
                commit.assert_not_called()

    def test_provider_modes_fail_closed(self):
        provider = object()
        calls = Mock()
        load, negotiate, open_provider = calls.load, calls.negotiate, calls.open
        for mode in ("off", "observe"):
            self.assertEqual(fixture_backend(mode, load, negotiate, open_provider), ("normal", None))
        self.assertEqual(calls.mock_calls, [])
        load.return_value = None
        self.assertEqual(fixture_backend("on", load, negotiate, open_provider), ("normal", "provider_absent"))
        negotiate.assert_not_called()
        open_provider.assert_not_called()
        load.return_value = provider
        for status in ("mismatch", "missing_symbol", "invalid_table", "bootstrap_panic"):
            negotiate.return_value = status
            with self.assertRaisesRegex(ValueError, "ABI mismatch"):
                fixture_backend("on", load, negotiate, open_provider)
        open_provider.assert_not_called()
        load.side_effect = OSError("provider load failed")
        with self.assertRaisesRegex(OSError, "provider load failed"):
            fixture_backend("on", load, negotiate, open_provider)
        open_provider.assert_not_called()
        load.side_effect = None
        negotiate.return_value = "ok"
        for opened, expected in (("ok", ("provider", None)),
                                 ("refused_before_custody", ("normal", "verified_open_refused"))):
            calls.reset_mock()
            open_provider.return_value = opened
            self.assertEqual(fixture_backend("on", load, negotiate, open_provider), expected)
            self.assertEqual([event[0] for event in calls.mock_calls], ["load", "negotiate", "open"])
        open_provider.return_value = "failed_after_custody"
        with self.assertRaisesRegex(ValueError, "provider session broken"):
            fixture_backend("on", load, negotiate, open_provider)

    def test_block_estimator(self):
        estimator = self.protocol["estimator"]
        self.assertEqual((estimator["pilot_pairs"], estimator["exclude_pilots"]), (3, True))
        self.assertEqual(estimator["qualification_blocks_per_profile"], 12)
        self.assertEqual(estimator["balanced_orders"], {"ABBA": 6, "BAAB": 6})
        self.assertEqual(estimator["min_scheduled_observations_per_latency_arm_per_block"], 100)
        self.assertEqual((estimator["bootstrap_resamples"], estimator["seed"], estimator["confidence_pct"]),
                         (10000, 521, 95))
        self.assertEqual(estimator["resampling_unit"], "whole matched block with both arms and all attempts")
        def observations(value, tail, ordinary):
            return [(value, 250)] * ordinary + [(tail, 250)] * (100 - ordinary)

        blocks = [(observations(10, 200, 96), observations(8, 30, 90)),
                  (observations(20, 80, 90), observations(15, 40, 96))]
        gains = block_gains(blocks, 8, estimator["seed"], pooled_p95)
        self.assertEqual(gains, [-200, 81.25, 62.5, 81.25, 62.5, 81.25, -200, 62.5])
        cuts = statistics.quantiles(range(0, 100, 10), n=40, method="inclusive")
        self.assertEqual((cuts[0], cuts[-1]), (2.25, 87.75))
        censored = ([(10, 250)] * 100, [(None, 250)] * 100)
        with self.assertRaisesRegex(ValueError, "unidentified matched resample"):
            block_gains([censored], 1, estimator["seed"], pooled_p95)
        values = list(range(1, 96)) + [None] * 5
        self.assertEqual(scheduled_quantile(values, [100] * 100, .95), 95)
        self.assertIsNone(scheduled_quantile(values, [100] * 100, .99))
        self.assertIsNone(scheduled_quantile(values, [40] * 100, .95))

    def test_non_latency_preregistration(self):
        estimator = self.protocol["estimator"]
        self.assertEqual(estimator["minimum_scheduled_work_per_non_latency_arm_per_block"], {
            "P": {"registered_workloads": 2}, "U": {"frame_attempts": 100},
            "T-impact": {"suite_runs": 2}, "T-shards": {"suite_runs": 2},
        })
        self.assertEqual(estimator["ratio_pooling"], {
            "P": "sum verified receipts / sum independent outer elapsed seconds",
            "U": "sum participating cgroup CPU seconds / sum verified frames",
        })
        self.assertEqual(pooled_ratio([(8, 2), (12, 12)]), 20 / 14)
        self.assertIsNone(pooled_ratio([(8, 0), (12, 0)]))
        self.assertEqual(read_json("arms/T-impact.json")["profiles"], [
            "registered tree and leaf change", "registered tree and shared module change",
            "registered tree and schema change", "registered tree and toolchain change",
        ])

    def test_matched_controls(self):
        controls = self.protocol["controls"]
        self.assertEqual(set(controls["required_at_each_sample"]), REQUIRED_SAMPLE)
        self.assertEqual(controls["required_per_cgroup"], ["work_total", "throttle_total"])
        self.assertEqual(controls["sample_at"], ["arm_start", "arm_end"])
        self.assertEqual(controls["pair_tolerance_pct"], 10)
        self.assertEqual(controls["pressure_abort_ceilings"],
                         read_json("fixtures/psi-preflight.json")["prospective_abort_ceilings"]["values"])
        saturated = sample_pair()
        saturated["arm_end"]["psi_cpu_some_avg10_pct"] = 51
        with self.assertRaisesRegex(ValueError, "pressure abort"):
            covariate_rates(saturated, controls, ["factory-fixture"], 1)
        samples = sample_pair()
        rates = covariate_rates(samples, controls, ["factory-fixture"], 1)
        key = ("factory-fixture", "work_total")
        assert_matched(rates, rates | {key: 90}, 10)
        with self.assertRaisesRegex(ValueError, "unmatched factory rate"):
            assert_matched(rates, rates | {key: 89}, 10)
        with self.assertRaisesRegex(ValueError, "unmeasured"):
            assert_matched(rates, rates | {("factory-fixture", "throttle_total"): None}, 10)
        with self.assertRaisesRegex(ValueError, "zero competitor work"):
            assert_matched(rates | {key: 0}, rates | {key: 0}, 10)
        samples["arm_end"]["factory_cgroups"]["factory-fixture"]["work_total"] = 20
        with self.assertRaisesRegex(ValueError, "idle factory"):
            covariate_rates(samples, controls, ["factory-fixture"], 1)

    def test_source_receipts(self):
        sources = read_json("sources.json")
        worktree = ROOT.parents[4]
        preserved = (
            "agents/metaviewer_agents/mcp_server.py",
            "contracts/v0.2/mv_tool_call.schema.json",
            "components/vp-direct/accept/t1/qt1-evidence/profiles.json",
            "components/vp-direct/accept/t1/qt1-evidence/execution.json",
            "components/vp-direct/accept/t2/qt2-evidence/measurement-controls.json",
            "components/vp-direct/qa/perf/compare.py",
        )
        self.assertEqual(set(preserved), sources["viewpoint"]["sha256"].keys())
        for path in preserved:
            self.assertEqual(sha256((worktree / path).read_bytes()).hexdigest(), sources["viewpoint"]["sha256"][path])
        self.assertEqual(sources["campaign"]["sha256"]["specs9/A-SEC_REPORT.md"],
                         "476275787adeba90c83554e926d67732b0ec7bb69a9cdc725287f99d3ed0e756")
        self.assertEqual(sources["upstream_acceptance"]["R9"], "candidate-compatible")
        self.assertIsNone(sources["upstream_acceptance"]["accepted_cross_reader_receipt"])
        self.assertEqual(sources["prototype_inputs"]["X-I"]["design_requirement"], "native recorder")


def sample_pair():
    start = {
        "psi_cpu_some_avg10_pct": 20, "psi_memory_full_avg10_pct": 0.01, "psi_io_full_avg10_pct": 0.1,
        "thermal_celsius": {"fixture-sensor": 42}, "viewpoint_cpuset": "16-19", "factory_cpuset": "0-15",
        "factory_cgroups": {"factory-fixture": {"work_total": 20, "throttle_total": 0}}, "useful_work": 0,
    }
    end = deepcopy(start)
    end["factory_cgroups"]["factory-fixture"]["work_total"] = 120
    end["useful_work"] = 1
    return {"arm_start": start, "arm_end": end}


if __name__ == "__main__":
    unittest.main(verbosity=2)
