"""Replay the QT2b raw captures and write a digest index to standard output."""

from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

EVIDENCE = Path(__file__).resolve().parent
SURFACE = EVIDENCE.parent
REPO = SURFACE.parents[3]
DEADLINE = datetime.fromisoformat("2026-11-18T06:10:00+01:00")

sys.path.insert(0, str(SURFACE))
import score

FROZEN = {
    "agents/metaviewer_agents/mcp_server.py":
        "a31d4381c53be7b2417b0acf46ad42008ed2710ac58c0929a1748c392357d862",
    "contracts/v0.2/mv_tool_call.schema.json":
        "7da7513b1b457dfeb08689c0ae70b2b950a2e093dab8706d99267acbd868cd5f",
}
ABSENT = ("competing_cgroups", "pair_tolerance", "throttle_counters")


def pin(path):
    return sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def require(condition, message):
    if not condition:
        raise ValueError(message)


def record_cell(run, plan, name, data):
    start = run["start_mono_ns"]
    window_end = start + score.WINDOW
    trace = data["focus"]
    summary = score.check_cell(data, plan["combinations"][name], plan, start,
                               max(window_end, trace["end_mono_ns"]))
    end_to_end = summary["operation_timings"]["end_to_end_ns"]
    require(sum(summary[key] for key in ("verified", "refused", "failed"))
            == end_to_end["n"] == score.OPERATIONS, "incomplete denominator")
    issues = []
    if trace["end_mono_ns"] > window_end:
        issues.append("focus trace outside the 600 s sampling window")
    try:
        score.check_thresholds(summary)
    except score.EvidenceError as exc:
        issues.append(str(exc))
    before, after = data["attach"]["before"], data["attach"]["after"]
    return {
        **summary, "focus_continuous": trace["continuous"], "focus_event_count": len(trace["events"]),
        "focus_trace_ns": trace["end_mono_ns"] - trace["start_mono_ns"],
        "focus_work_ns": data["end_mono_ns"] - data["start_mono_ns"],
        "source_exclusion": data["privacy_denial"],
        "rest_ns": data["rest"]["end_mono_ns"] - data["rest"]["ack_mono_ns"],
        "rest_counters_unchanged": data["rest"]["before"] == data["rest"]["after"],
        "attach_inventory_counts": {key: {"before": len(before[key]), "after": len(after[key])}
                                    for key in sorted(before)},
        "wake_samples_per_state": {key: len(data["wakes"][key]) for key in score.WAKE},
        "row_verdict": "not qualified" if issues else "unavailable",
        "row_reasons": issues, "paired_qualification": "UNAVAILABLE",
    }


def record_completed_attempts(plan, name, data, verdict):
    """Measure completed attempts when a later cell gate withheld qualification."""
    expected = plan["combinations"][name]
    operations = data["operations"]
    require([row["index"] for row in operations] == list(range(score.OPERATIONS)),
            "operation manifest incomplete")
    attach = data["attach"]
    binding = attach["receipt"]["binding"]
    counts = dict.fromkeys(("verified", "refused", "failed"), 0)
    timing, protected = [], []
    previous = attach["end_mono_ns"]
    for row in operations:
        request, reply, observation = (row[key] for key in ("request", "reply", "privacy"))
        successful = reply is not None and reply["ok"]
        submit, complete = score.timeline(row, complete=successful)
        require(previous <= submit <= complete, "reordered operation")
        previous = complete
        timing.append(score.components(row))
        if request is not None:
            score.validate_contract("vp.channel.req/v1", request)
            require(request["session_id"] == attach["receipt"]["session_id"]
                    and request["incarnation"] == binding["incarnation"]
                    and request["authority_epoch"] == binding["authority_epoch"]
                    and request["backend"] == expected["backend"] and request["op"] == expected["op"],
                    "operation identity differs")
        if reply is not None:
            score.validate_contract("vp.channel.reply/v1", reply)
            require(request is not None, "reply lacks request")
        if observation is not None:
            frame = observation["frame"]
            score.validate_contract("vp.frame/v1", frame)
            start, end = observation["start_mono_ns"], observation["end_mono_ns"]
            require(request is not None and submit <= start <= frame["captured_mono_ns"] <= end <= complete
                    and (frame["width"], frame["height"]) == score.SIZE,
                    "protected observation timing or size differs")
            protected.append(end - start)
        if (successful and observation is not None and reply.get("fallback_from") is None
                and reply.get("value_sha256") == expected["readback_sha256"]):
            counts["verified"] += 1
        elif reply is not None and not successful:
            counts["refused"] += 1
        else:
            counts["failed"] += 1
    attach_counts = {key: {"before": len(attach["before"][key]), "after": len(attach["after"][key])}
                     for key in sorted(score.RESOURCE_KINDS)}
    distribution = score.timing_distributions(timing)
    observation = score.distribution(protected)
    rest = data.get("rest")
    wake_results = {}
    for state in score.WAKE:
        samples = []
        for row in data["wakes"][state]:
            if rest is not None and row.get("receipt") is not None:
                elapsed, _ = score.check_wake(row, state, binding, attach["receipt"]["session_id"],
                                              rest["end_mono_ns"], row["t_complete"])
                samples.append(elapsed)
        wake_results[state] = score.distribution(samples)
    focus = data.get("focus")
    return {
        **verdict, **counts, "attempt_only_evidence": True, "row_verdict": "not qualified",
        "proposal_p95_ns": distribution["end_to_end_ns"]["p95_ns"],
        "observation_p95_ns": observation["p95_ns"], "operation_timings": distribution,
        "protected_observation": observation,
        "focus_continuous": focus["continuous"] if focus else None,
        "rest_ns": rest["end_mono_ns"] - rest["ack_mono_ns"] if rest else None,
        "resource_deltas": score.check_inventory(attach["before"], attach["after"]),
        "attach_inventory_counts": attach_counts,
        "source_exclusion": data.get("privacy_denial"),
        "wake_p95_ns": {state: result["p95_ns"] for state, result in wake_results.items()},
        "wake_samples_per_state": {state: result["n"] for state, result in wake_results.items()},
        "paired_qualification": "UNAVAILABLE",
    }


def replay(capture, source_pins):
    directory = capture.parent
    plan_path = directory / "t2-plan.json"
    plan, run = read(plan_path), read(capture)
    require(run["numeric_evidence"] == "MEASURED" and run["mode"] == "QT2B_MEASUREMENT_ONLY"
            and run["paired_qualification"] == "UNAVAILABLE", "wrong evidence mode")
    require(all(key in plan and plan[key] is None for key in ABSENT), "paired controls were populated")
    require(not score.score(plan, [])["available"], "stock replay unexpectedly available")
    require(run["plan_sha256"] == score.digest(plan) == pin(plan_path), "plan pin mismatch")
    require(plan["workload_sha256"] == pin(directory / "t2_workload.py"), "workload pin mismatch")
    for name, digest in plan["fixtures"].items():
        require(pin(directory / name) == digest, f"fixture pin mismatch: {name}")
    require(run["stock_collector_sha256"] == pin(SURFACE / "LIVE_t2.py"), "stock collector pin mismatch")
    require(run["measurement_entry_sha256"] in source_pins, "measurement entry source pin mismatch")
    scope = run["scope"]
    require(scope["affinity"] == score.CPUS and set(score.CPUS) <= set(scope["cpuset"])
            and scope["memory_high"] == 6 * 1024**3 and scope["memory_max"] == 8 * 1024**3,
            "captured CPU or RAM constraint mismatch")

    start = run["start_mono_ns"]
    samples = run["samples"]
    last_slot_index = -1
    for index, sample in enumerate(samples):
        slot_index = sample.get("slot_index", index)
        require(last_slot_index < slot_index < score.WINDOW // score.INTERVAL,
                f"invalid sampler slot index: {slot_index}")
        last_slot_index = slot_index
        due = start + slot_index * score.INTERVAL
        require(due <= sample["mono_ns"] <= sample["end_mono_ns"] < due + score.INTERVAL,
                f"invalid sampler slot: {slot_index}")
        require(all(sample[key] is None for key in ("thermal_millic", "throttle", "competing")),
                f"undeclared thermal or control sample in slot: {slot_index}")
    if "sampler_scope" in run:
        require(run["sampler_scope"] == scope, "sampler CPU and memory scope differ")
    ordered = sorted(plan["combinations"], key=lambda name: int(name.split("/", 1)[0].split("-", 1)[1]))
    cells = {}
    for name in ordered:
        data = run["cells"].get(name)
        if data and {"focus", "rest", "detach"} <= data.keys() and len(data["operations"]) == score.OPERATIONS:
            try:
                cells[name] = record_cell(run, plan, name, data)
            except (ValueError, KeyError, score.ValidationError) as exc:
                cells[name] = record_completed_attempts(plan, name, data, {
                    "reason": str(exc), "recorded_attempts": len(data["operations"]),
                })
        elif data and "attach" in data and len(data["operations"]) == score.OPERATIONS:
            cells[name] = record_completed_attempts(plan, name, data, run["summaries"][name])
        elif name in run["summaries"]:
            cells[name] = run["summaries"][name]
    return {
        "capture_sha256": pin(capture), "plan_sha256": run["plan_sha256"],
        "manifest_sha256": pin(directory / "manifest.json"),
        "workload_sha256": plan["workload_sha256"], "fixture_sha256": plan["fixtures"],
        "measurement_entry": source_pins[run["measurement_entry_sha256"]],
        "commit": run["commit"], "scope": scope, "collector_error": run["error"],
        "arm": run["arm"],
        "sampling": {
            "interval_ns": score.INTERVAL, "required_window_ns": score.WINDOW,
            "start_mono_ns": start, "end_mono_ns": run["end_mono_ns"],
            "slots_required": score.WINDOW // score.INTERVAL, "slots_observed": len(samples),
            "slot_coverage": "PASS" if len(samples) == score.WINDOW // score.INTERVAL
            and run["end_mono_ns"] - start >= score.WINDOW else "FAIL",
            "duration_ns": run["end_mono_ns"] - start,
            "minimum_mem_available_bytes": min((s["mem_available_bytes"] for s in samples), default=None),
            "peak_runner_cgroup_bytes": max((s["scope_memory_peak"] for s in samples), default=None),
            "thermal": None, "paired_baseline_margin": None,
        },
        "combinations": cells,
    }


def read_log(log):
    lines = log.read_text().splitlines()
    started, finished = (datetime.fromisoformat(lines[index]) for index in (0, -2))
    require(finished <= DEADLINE and started <= finished, f"invalid run wall times: {log.name}")
    require(re.fullmatch(r"rc=\d+", lines[-1]), f"missing run return code: {log.name}")
    entries = [json.loads(line) for line in lines if line.startswith("{")]
    cleanup = next((row for row in entries if "worker_rc" in row), None)
    refusal = next((line for line in lines if line.startswith("QD refused:")), None)
    require((cleanup and cleanup["leftover_scoped_processes"] == 0 and cleanup["private_root_removed"])
            or (cleanup is None and refusal == "QD refused: requested X display is already occupied"),
            f"cleanup proof missing: {log.name}")
    receipt = next((row for row in entries if "receipt" in row), None)
    return {
        "started": started.isoformat(), "finished": finished.isoformat(), "rc": int(lines[-1][3:]),
        "deadline_remaining_seconds": (DEADLINE - finished).total_seconds(),
        "worker_cleanup": cleanup, "startup_refusal": refusal, "receipt": receipt,
        "placement_resources": next((row for row in entries if "fixture_rss_bytes" in row), None),
        "x11_endpoints_removed": next((row for row in entries if "x11_lock_removed" in row), None),
    }


def main():
    for name, digest in FROZEN.items():
        require(pin(REPO / name) == digest, f"frozen file changed: {name}")
    sources = sorted(EVIDENCE.glob("measurement_live*.py"))
    source_pins = {pin(path): path.name for path in sources}
    runs = {}
    logs = sorted(path for path in EVIDENCE.glob("*.log") if not path.stem.startswith("tests"))
    relocation_file = EVIDENCE / "sampler-relocation.json"
    relocation = read(relocation_file) if relocation_file.is_file() else None
    for log in logs:
        record = read_log(log)
        capture = EVIDENCE / log.stem / "capture.json"
        if capture.is_file():
            record["replay"] = replay(capture, source_pins)
            require(record["receipt"] and record["receipt"]["sha256"] == record["replay"]["capture_sha256"],
                    f"exported capture digest mismatch: {log.name}")
            if relocation and relocation["before"] == "0::" + record["replay"]["scope"]["path"]:
                timing = record["replay"]["sampling"]
                require(timing["start_mono_ns"] <= relocation["started_mono_ns"]
                        <= relocation["finished_mono_ns"] <= timing["end_mono_ns"]
                        and relocation["after"] == relocation["before"].rsplit("/", 1)[0] + "/qd-control"
                        and relocation["cpu_affinity"] == score.CPUS
                        and relocation["parent_memory_max_bytes"] == 8 * 1024**3,
                        "sampler relocation evidence does not match the observed window")
                record["sampler_relocation"] = relocation
        runs[log.stem] = record
    require(any("replay" in run for run in runs.values()), "live captures absent")
    require(relocation is None or any("sampler_relocation" in run for run in runs.values()),
            "sampler relocation has no matching capture")
    tests_log = (EVIDENCE / "tests.log").read_text()
    tests_count = re.search(r"^Ran (\d+) tests", tests_log, re.MULTILINE)
    require(tests_count and tests_log.endswith("OK\nrc=0\n"), "offline test result is missing or failed")
    paths = [path for path in EVIDENCE.rglob("*") if path.is_file() and path.name != "execution.json"]
    paths.append(SURFACE / "tests" / "test_qt2b_measurement.py")
    files = {str(path.relative_to(REPO)): pin(path) for path in sorted(paths)}
    print(json.dumps({
        "evidence": "MEASURED", "cpu_affinity": score.CPUS, "deadline": DEADLINE.isoformat(),
        "paired_qualification": "UNAVAILABLE", "absent": [*ABSENT, "thermal", "paired_baseline_margin"],
        "frozen_sha256": FROZEN, "files_sha256": files, "runs": runs,
        "offline_tests": {"rc": 0, "passed": int(tests_count[1]), "failed": 0},
    }, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
