"""Replay one T1 scenario from a local benchmark capture and frozen receipts."""

import argparse
from collections import Counter
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import re
import sys

DIRECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(DIRECT))
from jsonschema import ValidationError
from schemas.validation import canonical_bytes, validate_contract

# T1 targets (except CAPACITY, the existing admission ceiling).
CAPACITY = 16
MIXES = {16: (4, 6, 6), 32: (4, 12, 16)}
STATES = ("ACTIVE", "ASLEEP", "OFF")
CYCLES = 100
MS = 1_000_000
DURATION_NS = 600_000 * MS
WAKE_LIMITS_MS = {"ASLEEP": (250, 1000), "OFF": (1000, 3000)}
QUEUE_P95_LIMIT_MS = 500
MIN_VERIFIED_PERCENT = 95
ADMISSION = "vp.admission/v1"
TRANSITION = "vp.transition_receipt/v1"
PERCEPTION = "vp.perception_state/v1"
ORDER = ("baseline", "candidate", "candidate", "baseline")
CPUS = (16, 17, 18, 19)
MEMORY_HIGH = 6 << 30
MEMORY_MAX = 8 << 30
PAIR_LIMITS = ("load", "temperature_millic", "competing_memory_bytes", "competing_cpu_usec")


class EvidenceError(ValueError):
    """The supplied capture does not establish the claimed outcome."""


def require(condition, message):
    if not condition:
        raise EvidenceError(message)


def contract(name, record):
    try:
        validate_contract(name, record)
    except KeyError:
        raise EvidenceError(f"SCHEMA_GAP {name}") from None
    except ValidationError as error:
        field = ".".join(str(part) for part in error.absolute_path)
        raise EvidenceError(f"Invalid {name} {field}") from None


def digest(value):
    return sha256(canonical_bytes(value)).hexdigest()


def cycle_plan(roster):
    groups = {state: [row["surface_id"] for row in roster if row["state"] == state]
              for state in STATES}
    require(len(roster) in MIXES and tuple(map(len, groups.values())) == MIXES[len(roster)],
            "Unexpected scenario state mix")
    ids = [row["surface_id"] for row in roster]
    require(all(isinstance(surface, str) and surface.strip() for surface in ids)
            and len(set(ids)) == len(ids), "Duplicate or invalid planned surface")
    plan = []
    for index in range(CYCLES):
        state = STATES[1 + index % 2]
        surfaces = groups[state]
        plan.append((surfaces[(index // 2) % len(surfaces)], state))
    return plan


def source_ceilings(repo):
    """Read the daemon and UI ceiling declarations in the target checkout."""
    declarations = {
        "MaxSurfaces": ("daemon-adapter/internal/daemon/environment.go", r'\bMaxSurfaces:\s*([^,]+)'),
        "MAX_CAPSULES": ("ui/electron/profilePicker.js", r'\bexport const MAX_CAPSULES\s*=\s*([^;]+)'),
    }
    values = {}
    for name, (path, pattern) in declarations.items():
        with (repo / path).open(encoding="utf-8") as source:
            matches = [value.strip() for line in source for value in re.findall(pattern, line)]
        require(matches and all(value == str(CAPACITY) for value in matches),
                f"{name} ceiling changed or declaration unavailable")
        values[name] = CAPACITY
    return values


def percentile_ns(values, percent):
    """Nearest rank, with thresholds applied to unrounded nanoseconds."""
    if not values:
        return None
    return sorted(values)[(len(values) * percent + 99) // 100 - 1]


def capture_times(markers, outcome, start, end):
    names = ("t_submit", "t_admitted", "t_first_byte", "t_complete")
    require(set(markers) == set(names), "Missing or unexpected clock marker")
    submit, admitted, first, complete = (markers[name] for name in names)
    require(all(type(value) is int and start <= value <= end for value in
                (submit, complete)) and submit < complete, "Invalid sample interval")
    present = [value for value in (submit, admitted, first, complete) if value is not None]
    require(all(type(value) is int for value in present) and present == sorted(present),
            "Clock markers out of order")
    require(admitted is not None or first is None, "First byte without admission")
    if outcome == "verified":
        require(admitted is not None and first is not None, "Verified sample lacks clock markers")
    if outcome == "capacity_refused":
        require(admitted is None and first is None, "Capacity refusal has admission or first byte")
    return submit, admitted, first, complete


def check_wake(row, state, surface, markers, used_observations):
    p4, p7 = row["p4"], row["p7"]
    contract(TRANSITION, p4)
    contract(TRANSITION, p7)
    require(p4 == p7 and p7["from"] == "WAKING" and p7["to"] == "ACTIVE"
            and p7["complete"] and set(p7["acks"]) == {"identity", "capability", "stream"},
            "Wake lacks matching P4/P7 verification")
    observation = p7["fresh_observation_id"]
    require(observation and observation not in used_observations, "Missing or reused observation")
    used_observations.add(observation)
    boundaries = row["broker_states"]
    for boundary in boundaries:
        contract(PERCEPTION, boundary)
    require([boundary["state"] for boundary in boundaries] == [state, "WAKING", "ACTIVE"],
            "Wake stratum differs from observed broker states")
    prior, waking, active = boundaries
    require(all(boundary["surface_id"] == surface and
                all(boundary[key] == prior[key] for key in ("session_id", "incarnation", "authority_epoch"))
                for boundary in boundaries), "Wake broker identity mismatch")
    require(prior["stream_generation"] == waking["stream_generation"] < active["stream_generation"],
            "Wake lacks a new stream generation")
    _, admitted, first, complete = markers
    require(prior["since_mono_ns"] <= admitted <= waking["since_mono_ns"] <= first
            <= active["since_mono_ns"] <= complete, "Broker states do not bracket the observation")


def score(data):
    roster = data["roster"]
    plan = cycle_plan(roster)
    size = len(roster)
    admission_counts = Counter(admitted=0, capacity_refused=0, other_refused=0)
    decisions = data["admissions"]
    require(len(decisions) == size, "Missing or extra initial admission receipt")
    require(data["surface_ceiling"] == CAPACITY, "Admission ceiling changed")
    require(data["source_ceilings"] == {"MaxSurfaces": CAPACITY, "MAX_CAPSULES": CAPACITY},
            "Daemon or UI ceiling evidence differs")
    admitted_ids, capacity_ids = set(), set()
    for request, receipt in zip(roster, decisions, strict=True):
        contract(ADMISSION, receipt)
        surface = request["surface_id"]
        require(receipt["surface_id"] == surface and receipt["state"] == request["state"]
                and receipt["surface_count"] == len(admitted_ids) + 1,
                "Initial admission does not match the ordered cohort")
        if receipt["admitted"]:
            admitted_ids.add(surface)
            admission_counts["admitted"] += 1
        elif (receipt["surface_count"] == CAPACITY + 1 and receipt["margin_percent"] >= 15
              and (not receipt["gpu_dependent"] or receipt["gpu_accounted"])):
            capacity_ids.add(surface)
            admission_counts["capacity_refused"] += 1
        else:
            admission_counts["other_refused"] += 1

    start, end = data["start_mono_ns"], data["end_mono_ns"]
    require(type(start) is int and type(end) is int and start >= 0 and end - start >= DURATION_NS,
            "Scenario coverage is shorter than the T1 window")
    require(data["plan_sha256"] == digest(plan), "Planned cycle digest differs")
    cohort = {row["surface_id"]: row["state"] for row in roster if row["surface_id"] in admitted_ids}
    initial = {}
    for snapshot, boundary_time in zip((data["cohort_start"], data["cohort_end"]), (start, end), strict=True):
        require(len(snapshot) == len(cohort), "Incomplete resident cohort")
        by_surface = {}
        for record in snapshot:
            contract(PERCEPTION, record)
            surface = record["surface_id"]
            require(surface in cohort and record["state"] == cohort[surface]
                    and record["since_mono_ns"] <= boundary_time, "Resident cohort state differs")
            by_surface[surface] = tuple(record[field] for field in ("session_id", "incarnation", "authority_epoch"))
        require(by_surface.keys() == cohort.keys(), "Missing or duplicate resident surface")
        require(not initial or initial == by_surface, "Resident cohort identity changed")
        initial = by_surface
    rows = data["cycles"]
    require(len(rows) == len(plan), "Missing or extra cycle receipt")
    counts = Counter(verified=0, capacity_refused=0, refused=0, failed=0)
    wake = {"ASLEEP": [], "OFF": []}
    queue, end_to_end = [], []
    observations = set()
    previous_end = start
    for index, ((surface, state), row) in enumerate(zip(plan, rows, strict=True)):
        require(type(row["cycle"]) is int and (row["cycle"], row["surface_id"], row["state"])
                == (index, surface, state), f"Cycle {index} does not match the plan")
        outcome = row["outcome"]
        require(outcome in counts, f"Cycle {index} has an unknown outcome")
        marks = capture_times(row["markers"], outcome, start, end)
        submit, admitted, first, complete = marks
        slot_ns = DURATION_NS // CYCLES
        require(previous_end <= submit and start + slot_ns * index <= submit
                < start + slot_ns * (index + 1), f"Cycle {index} outside planned time slot")
        previous_end = complete
        if surface in capacity_ids:
            require(outcome == "capacity_refused", "A planned capacity refusal was lost or relabeled")
        elif surface not in admitted_ids:
            require(outcome == "refused", "An unadmitted surface was used")
        else:
            require(outcome != "capacity_refused", "Capacity refusal claimed for an incumbent")
            admission = row["wake_admission"]
            if admission is not None:
                contract(ADMISSION, admission)
                require(admission["surface_id"] == surface and admission["state"] == state
                        and admission["surface_count"] == len(admitted_ids)
                        and admission["admitted"] == (admitted is not None),
                        "Wake admission does not match the incumbent or clock marker")
            else:
                require(admitted is None, "An admission marker lacks its frozen receipt")
        if outcome == "verified":
            check_wake(row, state, surface, marks, observations)
            prior = row["broker_states"][0]
            require(tuple(prior[field] for field in ("session_id", "incarnation", "authority_epoch"))
                    == initial[surface], "Cycle differs from the resident cohort identity")
            wake[state].append(first - admitted)
        if admitted is not None:
            queue.append(admitted - submit)
        end_to_end.append(complete - submit)
        counts[outcome] += 1

    wake_ns = {state: {f"p{p}": percentile_ns(samples, p) for p in (50, 95)}
               for state, samples in wake.items()}
    queue_ns = percentile_ns(queue, 95)
    performance = (
        counts["verified"] * 100 >= MIN_VERIFIED_PERCENT * len(plan)
        and queue_ns is not None and queue_ns <= QUEUE_P95_LIMIT_MS * MS
        and all(metric is not None and metric <= limit * MS
                for state, limits in WAKE_LIMITS_MS.items()
                for metric, limit in zip(wake_ns[state].values(), limits, strict=True))
    )
    expected_capacity = size > CAPACITY and len(admitted_ids) == CAPACITY and len(capacity_ids) == size - CAPACITY
    result = "FAIL"
    if expected_capacity:
        result = "EXPECTED_CAPACITY_REFUSAL"
    elif size == CAPACITY and len(admitted_ids) == CAPACITY and performance:
        result = "PASS"
    return {
        "scenario": size, "state_mix": dict(zip(STATES, MIXES[size], strict=True)), "result": result,
        "admissions": dict(admission_counts), "outcomes": dict(counts), "cycle_denominator": len(plan),
        "verified_percent": 100 * counts["verified"] / len(plan), "queue_p95_ns": queue_ns,
        "wake_ns": wake_ns, "wake_samples": {state: len(samples) for state, samples in wake.items()},
        "end_to_end_p95_ns": percentile_ns(end_to_end, 95), "duration_ns": end - start,
    }


def natural(value):
    return type(value) is int and value >= 0


def nonnegative(value):
    return type(value) in (int, float) and isfinite(value) and value >= 0


def check_pair_plan(plan):
    limits = plan["pair_tolerance"]
    require(all(nonnegative(limits[key]) for key in PAIR_LIMITS), "Pair tolerances missing")
    for key in ("throttle_counters", "competing_cgroups"):
        sources = plan[key]
        require(isinstance(sources, list) and all(isinstance(source, str) and bool(source) for source in sources)
                and len(set(sources)) == len(sources), "Pair source coordinates missing or duplicated")
    require(bool(plan["throttle_counters"]) and all(path.startswith("/sys/") for path in plan["throttle_counters"]),
            "Thermal throttle sources missing")


def paired_run(run, plan):
    bench, capture = run["benchmark"], run["scenario"]
    start, end = run["start_mono_ns"], run["end_mono_ns"]
    require((start, end) == (capture["start_mono_ns"], capture["end_mono_ns"]) and run["error"] is None,
            "Scenario boundaries differ or arm is incomplete")
    require(bench["clock"] == "CLOCK_MONOTONIC" and run["numeric_evidence"] in ("MEASURED", "TARGET"),
            "Benchmark clock or evidence tag missing")
    require(isinstance(run["boot_id"], str) and bool(run["boot_id"])
            and re.fullmatch(r"[a-f0-9]{40}", run["commit"]), "Benchmark provenance missing")
    for key in ("workload_sha256", "fixture_sha256", "manifest_sha256"):
        require(re.fullmatch(r"[a-f0-9]{64}", bench[key]), "Benchmark workload or fixture pin missing")
    require(natural(bench["samples"]) and bench["samples"] == len(capture["cycles"]),
            "Scored sample count differs")
    warmups = bench["warmup_markers"]
    require(natural(bench["warmups"]) and len(warmups) == bench["warmups"],
            "Warm-up count differs")

    scope = run["scope"]
    require(scope["affinity"] == list(CPUS) and set(CPUS) <= set(scope["cpuset"])
            and (scope["memory_high"], scope["memory_max"]) == (MEMORY_HIGH, MEMORY_MAX),
            "Benchmark placement or scope limits differ")
    before, after = bench["before"], bench["after"]
    require(all(natural(sample["mono_ns"]) for sample in (before, after))
            and before["mono_ns"] <= start <= end <= after["mono_ns"], "Benchmark host sample window differs")
    prior = before["mono_ns"]
    for markers in warmups:
        _, _, _, complete = capture_times(markers, "warmup", prior, start)
        require(complete < start, "Warm-up overlaps the scored window")
        prior = complete

    samples = run["samples"]
    require(len(samples) == len(capture["cycles"]), "Per-cycle host sample missing")
    slot = DURATION_NS // CYCLES
    for index, (sample, row) in enumerate(zip(samples, capture["cycles"], strict=True)):
        stamp = sample["mono_ns"]
        require(natural(stamp) and start + slot * index <= stamp <= row["markers"]["t_submit"]
                and stamp < start + slot * (index + 1), "Host sample outside its cycle")
    telemetry = [before, *samples, after]
    for sample in telemetry:
        require(type(sample["thermal_max_millic"]) is int
                and set(sample["throttle"]) == set(plan["throttle_counters"])
                and all(map(natural, sample["throttle"].values())),
                "Thermal state or throttle evidence missing")
        require(len(sample["load1_5_15"]) == 3 and all(map(nonnegative, sample["load1_5_15"])),
                "Competing host load missing")
        require(set(sample["competing"]) == set(plan["competing_cgroups"]),
                "Competing cgroup coverage differs")
    require(all(sample["throttle"] == before["throttle"] for sample in telemetry),
            "Thermal throttle counter changed")
    competing = {}
    for name in plan["competing_cgroups"]:
        work = [sample["competing"][name] for sample in telemetry]
        require(all(natural(row[key]) for row in work for key in ("memory_bytes", "cpu_usec")),
                "Invalid competing cgroup counters")
        cpu = [row["cpu_usec"] for row in work]
        require(cpu == sorted(cpu), "Competing cgroup counters moved backwards")
        competing[name] = (max(row["memory_bytes"] for row in work), cpu[-1] - cpu[0])
    return {
        "load_max": [max(column) for column in zip(*(sample["load1_5_15"] for sample in telemetry), strict=True)],
        "thermal_max": max(sample["thermal_max_millic"] for sample in telemetry),
        "competing": competing,
    }


def check_pair(baseline, candidate, limits):
    require(abs(baseline["thermal_max"] - candidate["thermal_max"]) <= limits["temperature_millic"],
            "Thermally asymmetric pair")
    require(all(abs(a - b) <= limits["load"] for a, b in
                zip(baseline["load_max"], candidate["load_max"], strict=True)), "Competing host load changed")
    for name, (memory, cpu) in baseline["competing"].items():
        peer_memory, peer_cpu = candidate["competing"][name]
        require(abs(memory - peer_memory) <= limits["competing_memory_bytes"]
                and abs(cpu - peer_cpu) <= limits["competing_cpu_usec"], "Competing cgroup work changed")


def score_comparison(captures, plan):
    """Require a complete comparable ABBA series before scoring its scenarios."""
    check_pair_plan(plan)
    require(tuple(run["arm"] for run in captures) == ORDER
            and all(type(run["benchmark"]["order_index"]) is int and run["benchmark"]["order_index"] == index
                    for index, run in enumerate(captures)), "Complete ABBA series required")
    a, b, c, d = captures
    require(a["commit"] == d["commit"] and b["commit"] == c["commit"], "Commit changed within an arm")
    plan_sha256 = digest(plan)
    require(all(run["plan_sha256"] == plan_sha256 for run in captures), "Predeclared pair plan changed")
    pins = ("workload_sha256", "fixture_sha256", "manifest_sha256", "warmups", "samples")
    require(all(run[key] == a[key] for run in captures for key in ("boot_id", "numeric_evidence"))
            and all(run["scope"][key] == a["scope"][key] for run in captures
                    for key in ("affinity", "cpuset", "memory_high", "memory_max"))
            and all(run["benchmark"][key] == a["benchmark"][key] for run in captures for key in pins)
            and all(run["scenario"]["plan_sha256"] == a["scenario"]["plan_sha256"] for run in captures),
            "Benchmark workload, placement or collection plan changed")
    require(all(left["benchmark"]["after"]["mono_ns"] < right["benchmark"]["before"]["mono_ns"]
                for left, right in zip(captures, captures[1:])), "Benchmark arms overlap or are out of order")
    hosts = [paired_run(capture, plan) for capture in captures]
    for reference, comparison in ((0, 1), (3, 2)):
        check_pair(hosts[reference], hosts[comparison], plan["pair_tolerance"])
    scenarios = [score(run["scenario"]) for run in captures]
    return {"result": scenarios[0]["result"] if all(row["result"] == scenarios[0]["result"] for row in scenarios)
            else "FAIL", "numeric_evidence": a["numeric_evidence"], "runs": scenarios}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", type=Path, nargs="+")
    parser.add_argument("--pair-plan", type=Path)
    args = parser.parse_args()
    try:
        raw = [path.read_bytes() for path in args.captures]
        captures = [json.loads(data) for data in raw]
        if args.pair_plan is None:
            require(len(captures) == 1, "Multiple captures require --pair-plan")
            result = score(captures[0].get("scenario", captures[0]))
            result.update(raw_capture=str(args.captures[0]), raw_sha256=sha256(raw[0]).hexdigest())
        else:
            result = score_comparison(captures, json.loads(args.pair_plan.read_bytes()))
            result["raw_receipts"] = [{"path": str(path), "sha256": sha256(data).hexdigest()}
                                      for path, data in zip(args.captures, raw, strict=True)]
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
        parser.exit(2, f"T1 invalid evidence: {error}\n")
    print(json.dumps(result, sort_keys=True))
    return int(result["result"] == "FAIL")


if __name__ == "__main__":
    raise SystemExit(main())
