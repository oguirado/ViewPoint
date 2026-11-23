"""Replay A5 benchmark measurements using the existing ViewPoint contracts."""

import argparse
from hashlib import sha256
import json
from math import ceil, isfinite
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jsonschema import ValidationError
from schemas.validation import canonical_bytes, validate_contract

MS = 1_000_000
SECOND = 1_000 * MS
# T2 acceptance targets.
SIZE = (1920, 1080)
PROFILE = "private-xvfb"
OPERATIONS = 100
INTERVAL = 100 * MS
WINDOW = 600 * SECOND
REST = 60 * SECOND
WAKE = {"ASLEEP": 1_000 * MS, "OFF": 3_000 * MS}
RESOURCE_KINDS = {"containers", "guests", "private_display_servers"}
AGENT_COUNTERS = {"acquisitions", "analyses", "ocr", "inferences", "decisions"}
CPUS = [16, 17, 18, 19]


class EvidenceError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise EvidenceError(reason)


def digest(value):
    return sha256(canonical_bytes(value)).hexdigest()


def is_digest(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def natural(value):
    return type(value) is int and value >= 0


def number(value):
    return type(value) in (int, float) and isfinite(value) and value >= 0


def p95(values):
    require(bool(values), "empty latency population")
    return sorted(values)[ceil(len(values) * .95) - 1]


def distribution(values):
    observed = sorted(value for value in values if value is not None)
    if not observed:
        return {"n": 0, "p50_ns": None, "p95_ns": None}
    return {"n": len(observed), "p50_ns": observed[ceil(len(observed) * .50) - 1],
            "p95_ns": observed[ceil(len(observed) * .95) - 1]}


def plan_check(plan):
    require(is_digest(plan["workload_sha256"]), "workload digest missing")
    require(bool(plan["fixtures"]) and all(map(is_digest, plan["fixtures"].values())), "fixture digests missing")
    require(natural(plan["warmups"]), "warm-up count missing")
    require(natural(plan["wake_samples"]) and plan["wake_samples"] > 0, "wake samples missing")
    require(isinstance(plan["combinations"], dict) and bool(plan["combinations"]), "profile matrix missing")
    for combo, cell in plan["combinations"].items():
        require(cell.get("profile") == PROFILE, "profile is unavailable")
        require(bool(combo) and is_digest(cell["readback_sha256"]) and cell["backend"] in ("cdp", "atspi", "vision")
                and cell["op"] in ("observe", "locate", "act", "assert", "wait_event"),
                "invalid combination or semantic oracle")
    limits = plan["pair_tolerance"]
    require(all(number(limits[k]) for k in ("load", "temperature_millic", "competing_memory_bytes",
                                           "competing_cpu_usec")),
            "pair tolerances missing")
    require(isinstance(plan["throttle_counters"], list)
            and all(isinstance(path, str) and path.startswith("/sys/") for path in plan["throttle_counters"]),
            "throttle counter coordinates missing")
    require(isinstance(plan["competing_cgroups"], list)
            and all(isinstance(name, str) and bool(name) for name in plan["competing_cgroups"])
            and len(set(plan["competing_cgroups"])) == len(plan["competing_cgroups"]),
            "competing cgroup inventory missing")


def timeline(row, *, complete=False):
    submit, admitted, first, done = (row[key] for key in ("t_submit", "t_admitted", "t_first_byte", "t_complete"))
    require(natural(submit) and natural(done) and submit <= done, "missing or reversed monotonic boundary")
    require(admitted is None or (natural(admitted) and submit <= admitted <= done), "invalid admission boundary")
    require(first is None or (admitted is not None and natural(first) and admitted <= first <= done),
            "invalid first-byte boundary")
    require(not complete or first is not None, "missing boundary for completed work")
    return submit, done


def components(row):
    submit, done = timeline(row)
    admitted, first = row["t_admitted"], row["t_first_byte"]
    return {"queue_ns": None if admitted is None else admitted - submit,
            "wake_ns": None if first is None else first - admitted,
            "end_to_end_ns": done - submit,
            "service_ns": None if first is None else done - first}


def check_focus(trace, surface, start, end):
    validate_contract("vp.focus_trace/v1", trace)
    require(trace["continuous"] and trace["surface_id"] == surface
            and trace["start_mono_ns"] <= start <= end <= trace["end_mono_ns"]
            and trace["end_mono_ns"] > trace["start_mono_ns"], "continuous focus coverage missing")
    events = trace["events"]
    require(bool(events) and events[0]["kind"] == "_NET_ACTIVE_WINDOW"
            and events[0]["mono_ns"] == trace["start_mono_ns"], "initial focus witness missing")
    active = events[0]["xid"]
    require(all(event["kind"] == "XInput2"
                or (event["kind"] == "_NET_ACTIVE_WINDOW" and event["xid"] == active)
                for event in events), "unsolicited focus activity")


def check_inventory(before, after):
    for snapshot in (before, after):
        require(snapshot.keys() == RESOURCE_KINDS, "incomplete resource inventory")
        for values in snapshot.values():
            require(isinstance(values, list) and all(isinstance(v, str) and bool(v) for v in values)
                    and len(values) == len(set(values)), "invalid resource inventory")
    deltas = {kind: len(after[kind]) - len(before[kind]) for kind in sorted(RESOURCE_KINDS)}
    require(all(set(before[kind]) == set(after[kind]) for kind in RESOURCE_KINDS),
            "host attach changed container, guest or display-server identities")
    return deltas


def check_rest(rest, start, end):
    receipt = rest["receipt"]
    validate_contract("vp.transition_receipt/v1", receipt)
    require(receipt["from"] == "ACTIVE" and receipt["to"] == "ASLEEP" and receipt["complete"]
            and {"agent-drain", "demand", "stream", "app"} <= set(receipt["acks"]), "unconfirmed drain")
    ack, stop = rest["ack_mono_ns"], rest["end_mono_ns"]
    require(natural(ack) and natural(stop) and start <= ack <= stop <= end and stop - ack >= REST,
            "rest coverage shorter than target")
    for edge in ("before", "after"):
        counts = rest[edge]
        require(counts.keys() == AGENT_COUNTERS and all(map(natural, counts.values())), "agent rest counters missing")
    require(rest["before"] == rest["after"], "agent activity during rest")


def check_wake(row, state, binding, session_id, start, end):
    first, last = timeline(row, complete=True)
    receipt = row["receipt"]
    validate_contract("vp.transition_receipt/v1", receipt)
    prior = row["prior_state"]
    validate_contract("vp.perception_state/v1", prior)
    require(prior["state"] == state and prior["since_mono_ns"] <= first
            and prior["session_id"] == session_id
            and all(prior[key] == binding[key] for key in ("surface_id", "incarnation", "authority_epoch")),
            "wake lacks bound prior state")
    require(start <= first <= last <= end and receipt["from"] == "WAKING" and receipt["to"] == "ACTIVE"
            and receipt["complete"] and receipt["fresh_observation_id"]
            and {"identity", "capability", "stream"} <= set(receipt["acks"]), "wake lacks fresh lifecycle evidence")
    return components(row)["wake_ns"], receipt["fresh_observation_id"]


def check_cell(cell, expected, plan, start, end):
    attach = cell["attach"]
    validate_contract("vp_workspace_call.attach.result/v1", attach["receipt"])
    binding = attach["receipt"]["binding"]
    surface = binding["surface_id"]
    session_id = attach["receipt"]["session_id"]
    session = cell["session"]
    validate_contract("vp.session/v1", session)
    require(session["session_id"] == session_id and session["incarnation"] == binding["incarnation"]
            and session["lease"]["authority_epoch"] == binding["authority_epoch"]
            and session["site_group"] == "fixture" and session["state"] == "AGENT"
            and session["backend"] == expected["backend"]
            and session["lease"]["holder"] == binding["owner"]
            and session["display"] == f"xpra:{cell['private_display'][1:]}"
            and re.fullmatch(r":[1-9][0-9]*", cell["private_display"]), "private session binding missing")
    deltas = check_inventory(attach["before"], attach["after"])
    trace = cell["focus"]
    require(start <= trace["start_mono_ns"] < trace["end_mono_ns"] <= end, "focus trace outside sampling window")
    check_focus(trace, surface, cell["start_mono_ns"], cell["end_mono_ns"])
    start, end = cell["start_mono_ns"], cell["end_mono_ns"]
    require(start <= attach["start_mono_ns"] <= attach["end_mono_ns"] <= end, "attach outside focus trace")
    require(cell["warmups_completed"] == plan["warmups"], "warm-up count wrong")
    denied = cell["privacy_denial"]
    require(denied["code"] == "HUMAN_NEEDED" and type(denied["acquisitions"]) is int
            and type(denied["publications"]) is int and denied["acquisitions"] == denied["publications"] == 0,
            "source exclusion was unproved or allowed acquisition")
    operations = cell["operations"]
    require([row["index"] for row in operations] == list(range(OPERATIONS)),
            "operation manifest incomplete or reordered")
    counts = {"verified": 0, "refused": 0, "failed": 0}
    timings, protected = [], []
    prior_end = attach["end_mono_ns"]
    request_ids = set()
    for row in operations:
        request, reply = row["request"], row["reply"]
        if request is not None:
            validate_contract("vp.channel.req/v1", request)
            require(request.get("session_id") == attach["receipt"]["session_id"]
                    and request["incarnation"] == binding["incarnation"]
                    and request["authority_epoch"] == binding["authority_epoch"]
                    and request["backend"] == expected["backend"] and request["op"] == expected["op"]
                    and request["capsule_id"] == session["capsule_id"]
                    and request.get("dry_run", False) is False
                    and request.get("target", {}).get("cdp_target_id") in (None, session.get("cdp_target_id"))
                    and not (request.keys() & {"input_text", "slot_id", "url", "key"})
                    and request["request_id"] not in request_ids, "operation identity or qualification changed")
            request_ids.add(request["request_id"])
        if reply is not None:
            validate_contract("vp.channel.reply/v1", reply)
            require(request is not None, "reply lacks request")
        successful = reply is not None and reply["ok"]
        submit, done = timeline(row, complete=successful)
        require(prior_end <= submit <= done <= end, "operation outside measured order")
        prior_end = done
        timings.append(components(row))
        if row["privacy"] is not None:
            require(request is not None, "protected frame lacks an operation")
            probe = row["privacy"]
            descriptor = probe["frame"]
            validate_contract("vp.frame/v1", descriptor)
            a, b = probe["start_mono_ns"], probe["end_mono_ns"]
            require(submit <= a <= descriptor["captured_mono_ns"] <= b <= done
                    and (descriptor["width"], descriptor["height"]) == SIZE
                    and descriptor["frame_kind"] in {"cdp_png", "xwd", "xpra"}
                    and (descriptor["frame_kind"] != "cdp_png"
                         or descriptor["witness"]["cdp_target_id"] == session.get("cdp_target_id")),
                    "protected frame has the wrong timing, display size or source")
            protected.append(b - a)
        if (successful and row["privacy"] is not None and reply.get("fallback_from") is None
                and reply.get("value_sha256") == expected["readback_sha256"]):
            counts["verified"] += 1
        elif reply is not None and not successful:
            counts["refused"] += 1
        else:
            counts["failed"] += 1
    check_rest(cell["rest"], prior_end, end)
    wake_ns, wake_timings = {}, {}
    previous = cell["rest"]["end_mono_ns"]
    seen = set()
    for state in WAKE:
        rows = cell["wakes"][state]
        require(len(rows) == plan["wake_samples"], "wake population incomplete")
        times, timing_rows = [], []
        for row in rows:
            elapsed, fresh = check_wake(row, state, binding, session_id, previous, end)
            require(fresh not in seen, "reused wake observation")
            previous = row["t_complete"]
            seen.add(fresh)
            times.append(elapsed)
            timing_rows.append(components(row))
        wake_ns[state] = p95(times)
        wake_timings[state] = timing_distributions(timing_rows)
    validate_contract("vp_workspace_call.detach.result/v1", cell["detach"])
    require(cell["detach"] == {"detached": True, "closed_foreign": False}, "foreign application custody changed")
    operations = timing_distributions(timings)
    observation = distribution(protected)
    return dict(counts, proposal_p95_ns=operations["end_to_end_ns"]["p95_ns"],
                observation_p95_ns=observation["p95_ns"], wake_p95_ns=wake_ns, resource_deltas=deltas,
                operation_timings=operations, protected_observation=observation, wake_timings=wake_timings)


def timing_distributions(rows):
    return {key: distribution(row[key] for row in rows)
            for key in ("queue_ns", "wake_ns", "end_to_end_ns", "service_ns")}


def check_samples(run, plan):
    start, end = run["start_mono_ns"], run["end_mono_ns"]
    require(natural(start) and natural(end) and end - start >= WINDOW, "sampling window too short")
    samples = run["samples"]
    require(len(samples) == WINDOW // INTERVAL, "resource samples missing")
    last = start - 1
    throttle_baseline = None
    competing = {}
    thermal = []
    for index, sample in enumerate(samples):
        stamp = sample["mono_ns"]
        due = start + index * INTERVAL
        require(max(last + 1, due) <= stamp < due + INTERVAL, "missed or reordered resource sample")
        last = stamp
        require(natural(sample["end_mono_ns"]) and stamp <= sample["end_mono_ns"] < due + INTERVAL,
                "resource sample exceeded its slot")
        require(natural(sample["mem_available_bytes"]) and sample["mem_available_bytes"] > 0
                and natural(sample["scope_memory_current"]) and natural(sample["scope_memory_peak"])
                and sample["scope_memory_peak"] >= sample["scope_memory_current"],
                "host or cgroup memory accounting missing")
        require(len(sample["load"]) == 3 and all(map(number, sample["load"])), "host load missing")
        for key in ("cpu_psi", "memory_psi", "scope_cpu_psi", "scope_memory_psi"):
            require(sample[key].keys() == {"some", "full"} and all(
                len(row) == 3 and all(map(number, row)) for row in sample[key].values()), "PSI samples missing")
        require(type(sample["thermal_millic"]) is int, "thermal sample missing")
        thermal.append(sample["thermal_millic"])
        counters = sample["throttle"]
        require(isinstance(counters, dict) and bool(counters) and all(map(natural, counters.values())),
                "thermal throttle accounting missing")
        if throttle_baseline is None:
            throttle_baseline = counters
        require(counters == throttle_baseline, "thermal throttle event or counter set changed")
        require(set(sample["competing"]) == set(plan["competing_cgroups"]), "competing cgroup sample missing")
        for name, info in sample["competing"].items():
            peak, first_cpu, last_cpu = competing.get(name, (0, info["cpu_usec"], info["cpu_usec"]))
            require(natural(info["memory_bytes"]) and natural(info["cpu_usec"]) and info["cpu_usec"] >= last_cpu,
                    "invalid competing cgroup counters")
            competing[name] = max(peak, info["memory_bytes"]), first_cpu, info["cpu_usec"]
    return {
        "mem_available_min": min(s["mem_available_bytes"] for s in samples),
        "scope_memory_peak": max(s["scope_memory_peak"] for s in samples),
        "scope_sample_p95_ns": p95([s["end_mono_ns"] - s["mono_ns"] for s in samples]),
        "load_max": [max(s["load"][i] for s in samples) for i in range(3)],
        "thermal_max": max(thermal),
        "thermal_sources": sorted(throttle_baseline),
        "competing": {name: (peak, last_cpu - first_cpu)
                      for name, (peak, first_cpu, last_cpu) in competing.items()},
    }


def check_run(run, plan):
    require(run["plan_sha256"] == digest(plan) and run["error"] is None, "wrong workload or incomplete run")
    require(bool(run["boot_id"]) and isinstance(run["commit"], str)
            and re.fullmatch(r"[a-f0-9]{40}", run["commit"]), "measurement provenance missing")
    scope = run["scope"]
    require(scope["affinity"] == CPUS and set(CPUS) <= set(scope["cpuset"])
            and scope["memory_high"] == 6 * 1024**3 and scope["memory_max"] == 8 * 1024**3,
            "scope or CPU placement mismatch")
    require(run["cells"].keys() == plan["combinations"].keys(), "measured profile matrix incomplete")
    host = check_samples(run, plan)
    cells = {key: check_cell(run["cells"][key], expected, plan, run["start_mono_ns"],
                             run["start_mono_ns"] + WINDOW)
             for key, expected in plan["combinations"].items()}
    return dict(host=host, cells=cells)


def check_pair(baseline, candidate, limits):
    require(baseline["thermal_sources"] == candidate["thermal_sources"]
            and abs(baseline["thermal_max"] - candidate["thermal_max"]) <= limits["temperature_millic"],
            "thermally asymmetric pair")
    require(all(abs(a - b) <= limits["load"] for a, b in zip(baseline["load_max"], candidate["load_max"])),
            "competing host load changed")
    for name, (baseline_memory, baseline_cpu) in baseline["competing"].items():
        memory, cpu = candidate["competing"][name]
        require(abs(baseline_memory - memory) <= limits["competing_memory_bytes"]
                and abs(baseline_cpu - cpu) <= limits["competing_cpu_usec"], "competing cgroup work changed")
    # Allow at most a ten-percent drop from that pair's baseline headroom.
    require(candidate["mem_available_min"] * 10 >= baseline["mem_available_min"] * 9,
            "memory margin below target")
    return 100 * candidate["mem_available_min"] / baseline["mem_available_min"]


def check_thresholds(summary):
    require(summary["verified"] * 100 >= OPERATIONS * 95, "verified ratio below target")
    require(summary["proposal_p95_ns"] <= 2_000 * MS, "proposal-to-receipt p95 above target")
    observation = summary["observation_p95_ns"]
    require(observation is not None and observation <= 500 * MS, "protected observation p95 above target")
    for state, threshold in WAKE.items():
        require(summary["wake_p95_ns"][state] <= threshold, f"{state} wake p95 above target")


def score(plan, runs):
    """Score an ABBA series. All conditions must pass for availability."""
    metrics = []
    try:
        plan_check(plan)
        require([run["arm"] for run in runs] == ["baseline", "candidate", "candidate", "baseline"],
                "complete ABBA series required")
        a, b, c, d = runs
        evidence = a["numeric_evidence"]
        require(evidence in ("TARGET", "MEASURED") and all(run["numeric_evidence"] == evidence for run in runs),
                "mixed measurement and fixture evidence")
        require(a["commit"] == d["commit"] and b["commit"] == c["commit"], "commit changed within an arm")
        require(len({run["boot_id"] for run in runs}) == 1
                and all(left["end_mono_ns"] < right["start_mono_ns"] for left, right in zip(runs, runs[1:])),
                "overlapping, reordered or cross-boot measurements")
        for run in runs:
            metrics.append(check_run(run, plan))
        require(all(run["scope"]["cpuset"] == a["scope"]["cpuset"] for run in runs),
                "effective cpuset changed between arms")
        for trial in metrics[1:3]:
            for summary in trial["cells"].values():
                check_thresholds(summary)
        margins = [check_pair(metrics[i]["host"], metrics[j]["host"], plan["pair_tolerance"])
                   for i, j in ((0, 1), (3, 2))]
    except (EvidenceError, ValidationError, KeyError, TypeError, AttributeError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, EvidenceError) else "missing or invalid benchmark or frozen evidence"
        return {"status": "UNAVAILABLE", "available": False, "reason": reason, "runs": metrics}
    return {"status": "PASS", "available": evidence == "MEASURED", "numeric_evidence": evidence,
            "runs": metrics, "margin_percent": margins}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("receipts", type=Path, nargs=4, metavar="ARM")
    args = parser.parse_args()
    try:
        plan = json.loads(args.plan.read_bytes())
        files = [path.read_bytes() for path in args.receipts]
        report = score(plan, [json.loads(data) for data in files])
        report["raw_receipts"] = [{"path": str(path.resolve()), "sha256": sha256(data).hexdigest()}
                                  for path, data in zip(args.receipts, files)]
    except (OSError, ValueError):
        report = {"status": "UNAVAILABLE", "reason": "unreadable benchmark evidence"}
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report.get("available") is True else 1


if __name__ == "__main__":
    sys.exit(main())
