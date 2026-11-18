"""Score pool latency from four raw ABBA receipts."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
from statistics import median


PROFILES = ((32, 32), (1920, 1080), (1920, 1080), (32, 32))
ARMS = ("baseline", "candidate", "candidate", "baseline")
# Paired comparison limits for background occupancy, load5, and peak temperature.
OCCUPANCY_TOLERANCE = 0.10
LOAD5_TOLERANCE = 2
TEMPERATURE_TOLERANCE_MILLIC = 5_000
SATURATION = {"name": "pool_saturation", "reason": "session capacity exhausted"}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def elapsed_ns(row):
    start, admit, first, end = (row[key] for key in
                                 ("submit_ns", "admit_ns", "first_byte_ns", "complete_ns"))
    require(0 < start <= admit <= first <= end and row["end_to_end_ns"] == end - start,
            "inconsistent monotonic latency markers")
    return end - start


def changing_series(report):
    blocks = [block for block in report["blocks"] if block["frame_workload"] == "changing"]
    require(tuple(tuple(block["profile"]) for block in blocks) == PROFILES,
            "missing or out-of-order changing-frame profiles")
    warmups = report["warmups"]
    samples = report["warm_samples_per_state_per_block"]
    require(warmups > 0 and samples > 0, "missing warmups or timed samples")
    expected_strata = ["cold"] + ["warmup"] * warmups + ["warm"] * samples
    pins = []
    timings = {profile: [] for profile in PROFILES}
    for block in blocks:
        fixtures = (block["fixture_sha256"], block["alternate_fixture_sha256"])
        digests = (block["frame_sha256"], block["alternate_frame_sha256"])
        require(fixtures[0] != fixtures[1] and digests[0] != digests[1], "changing frame pins repeat")
        pins.append((fixtures, digests))
        rows = block["runs"]
        require([row["stratum"] for row in rows] == expected_strata, "incomplete changing-frame samples")
        require(block["refusals"] == [SATURATION],
                "changing-frame saturation refusal differs")
        for index, row in enumerate(rows):
            require(row["state_before_source"] == "ACTIVE"
                    and row["frame_sha256"] == digests[index % 2]
                    and row["changed"] is (True if index else None), "frame did not alternate")
            elapsed = elapsed_ns(row)
            if row["stratum"] == "warm":
                timings[tuple(block["profile"])].append(elapsed)
    return pins, timings


def unchanged_series(report):
    blocks = [block for block in report["blocks"] if block["frame_workload"] == "unchanged"]
    require(tuple(tuple(block["profile"]) for block in blocks) == PROFILES,
            "missing or out-of-order unchanged-frame profiles")
    warmups, samples = report["warmups"], report["warm_samples_per_state_per_block"]
    require(warmups > 0 and samples > 0, "missing warmups or timed samples")
    strata = ["cold"] + ["warmup"] * warmups + ["warm"] * samples
    states = ("ASLEEP", "OFF", "ACTIVE")
    pins = []
    timings = {(1920, 1080): []}
    for block in blocks:
        digest = block["frame_sha256"]
        pins.append((block["fixture_sha256"], digest))
        expected_rows = [(state, stratum) for state in states for stratum in strata]
        rows = block["runs"]
        require([(row["state_before_source"], row["stratum"]) for row in rows] == expected_rows,
                "incomplete unchanged-frame samples")
        for row in rows:
            has_history = row["state_before_source"] == "ACTIVE" and row["stratum"] != "cold"
            require(row["frame_sha256"] == digest and row["changed"] is (False if has_history else None),
                    "unchanged frame or history differs")
            elapsed = elapsed_ns(row)
            if tuple(block["profile"]) in timings and has_history and row["stratum"] == "warm":
                timings[tuple(block["profile"])].append(elapsed)
        asleep = {"name": "retired_asleep_token", "reason": "stale stream token"}
        off = {"name": "retired_off_token", "reason": "stale stream token"}
        require(block["refusals"] == [SATURATION, *([asleep] * len(strata)), SATURATION,
                                     *([off] * (len(strata) - 1)), SATURATION],
                "unchanged-frame refusal sequence differs")
    return pins, timings


def host_controls(report):
    before, after = report["before"], report["after"]
    samples = [before, *(row["host"] for block in report["blocks"] for row in block["runs"]), after]
    require(all(a["mono_ns"] < b["mono_ns"] for a, b in zip(samples, samples[1:])),
            "invalid host sample ordering")
    for sample in samples:
        require(sample["loadavg"][1] <= 24 and sample["mem_available_kib"] >= 20 * 2**20,
                "host outside load or memory bounds")
        for key in ("cpu_perf_limited", "processor_cooling_state"):
            controls = sample[key]
            require(controls and all(value == 0 for value in controls.values()),
                    "missing or active CPU throttle indicators")
    deltas = [0] * 8
    for cpu in range(16):
        deltas = [total + end - start for total, start, end in zip(
            deltas, before["cpu_ticks"][f"cpu{cpu}"], after["cpu_ticks"][f"cpu{cpu}"], strict=True)]
    total = sum(deltas)
    require(total > 0 and min(deltas) >= 0, "invalid competing CPU counters")
    occupancy = (total - deltas[3] - deltas[4]) / total
    return {"occupancy": occupancy, "load5": before["loadavg"][1],
            "peak_millic": max(max(sample["temperature_millicelsius"].values()) for sample in samples)}


def compare(reports, *, workload="changing"):
    require(tuple(report["arm"] for report in reports) == ARMS, "complete ABBA order required")
    require(all(report["failures"] == [] and report["clock"] == "CLOCK_MONOTONIC"
                and report["cpu_affinity"] == [16, 17, 18, 19]
                and report["limits_bytes"] == {"memory.high": 6 << 30, "memory.max": 8 << 30}
                for report in reports), "benchmark arm failed or placement differs")
    for key in ("base_commit", "binding_sha256", "stream_sha256", "seed", "warmups",
                "benchmark_sha256", "warm_samples_per_state_per_block"):
        require(len({report[key] for report in reports}) == 1, f"arm inputs differ: {key}")
    hashes = [report["pool_sha256"] for report in reports]
    require(hashes[0] == hashes[3] and hashes[1] == hashes[2] and hashes[0] != hashes[1],
            "pool source does not identify a distinct stable baseline and candidate")
    series_reader = {"changing": changing_series, "unchanged": unchanged_series}[workload]
    series = [series_reader(report) for report in reports]
    require(all(pins == series[0][0] for pins, _ in series), "fixture pins differ between arms")
    controls = [host_controls(report) for report in reports]
    require(all(a["after"]["mono_ns"] < b["before"]["mono_ns"] for a, b in zip(reports, reports[1:])),
            "arms overlap or were collected out of order")

    pairs = []
    confounds = []
    for baseline, candidate in ((0, 1), (3, 2)):
        for key, limit in (("occupancy", OCCUPANCY_TOLERANCE), ("load5", LOAD5_TOLERANCE),
                           ("peak_millic", TEMPERATURE_TOLERANCE_MILLIC)):
            if abs(controls[baseline][key] - controls[candidate][key]) > limit:
                confounds.append({"metric": key, "baseline": baseline, "candidate": candidate})
        for profile, baseline_values in series[baseline][1].items():
            candidate_values = series[candidate][1][profile]
            count = len(baseline_values)
            rank95 = count - count // 20 - 1
            baseline_ns = [median(baseline_values), sorted(baseline_values)[rank95]]
            candidate_ns = [median(candidate_values), sorted(candidate_values)[rank95]]
            if workload == "unchanged":
                passed = candidate_ns[0] < baseline_ns[0]
            else:
                passed = all(new <= old for old, new in zip(baseline_ns, candidate_ns, strict=True))
            pairs.append(dict(profile=profile, baseline=baseline, candidate=candidate, samples_per_arm=count,
                              baseline_p50_p95_ns=baseline_ns, candidate_p50_p95_ns=candidate_ns,
                              result="PASS" if passed else "FAIL"))
    result = "INVALID" if confounds else "PASS" if all(pair["result"] == "PASS" for pair in pairs) else "FAIL"
    return dict(result=result, pairs=pairs, host_controls=controls, confounds=confounds, workload=workload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", choices=("changing", "unchanged"), default="changing")
    parser.add_argument("receipts", type=Path, nargs=4, metavar="RECEIPT")
    args = parser.parse_args()
    try:
        raw = [path.read_bytes() for path in args.receipts]
        result = compare([json.loads(value) for value in raw], workload=args.workload)
        result["raw_sha256"] = [sha256(value).hexdigest() for value in raw]
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
        result = dict(result="INVALID", reason=str(error))
    print(json.dumps(result, sort_keys=True))
    return {"PASS": 0, "FAIL": 1, "INVALID": 2}[result["result"]]


if __name__ == "__main__":
    raise SystemExit(main())
