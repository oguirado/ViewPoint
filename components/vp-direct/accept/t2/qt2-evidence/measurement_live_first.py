"""QT2b collection with explicitly absent paired controls and the stock cell gates."""

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from time import time_ns

from score import (INTERVAL, OPERATIONS, PROFILE, REST, WINDOW, EvidenceError, check_cell,
                   check_thresholds, digest, is_digest, natural, require)
from telemetry import HostTelemetry, Sampler, now, signature

ABSENT_CONTROLS = ("competing_cgroups", "pair_tolerance", "throttle_counters")
SECOND = 1_000_000_000
STOCK_PATH = Path("/work/qt2-stock.py")


def plan_check(plan):
    require(all(key in plan and plan[key] is None for key in ABSENT_CONTROLS),
            "QT2b requires explicit absence of all paired controls")
    require(is_digest(plan["workload_sha256"]), "workload digest missing")
    require(set(plan["fixtures"]) == {"timer.html", "login.html"}
            and all(map(is_digest, plan["fixtures"].values())), "fixture digests missing")
    require(natural(plan["warmups"]), "warm-up count missing")
    require(natural(plan["wake_samples"]) and plan["wake_samples"] > 0, "wake samples missing")
    require(isinstance(plan["combinations"], dict) and bool(plan["combinations"]), "profile matrix missing")
    for combo, cell in plan["combinations"].items():
        require(combo and cell["profile"] == PROFILE and cell["backend"] == "cdp"
                and cell["op"] == "observe" and is_digest(cell["readback_sha256"]),
                "invalid QT2b combination or semantic oracle")


def verify_pins(plan, plan_path, driver_path):
    require(sha256(driver_path.read_bytes()).hexdigest() == plan["workload_sha256"],
            "workload source changed")
    for name, pin in plan["fixtures"].items():
        fixture = (plan_path.parent / name).resolve(strict=True)
        require(fixture.is_relative_to(plan_path.parent.resolve()), "fixture outside plan directory")
        require(sha256(fixture.read_bytes()).hexdigest() == pin, "fixture bytes changed")


class AvailableTelemetry(HostTelemetry):
    def __init__(self, cgroup):
        self.cgroup = cgroup
        self.thermal_paths = ()
        self.throttle_paths = ()
        self.competing = {}

    def read(self):
        return dict(super().read(), throttle=None, competing=None)


def error_reason(exc):
    # Unexpected errors may contain application content.
    return str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__


def cell_summary(run, cell, expected, plan):
    summary = check_cell(cell, expected, plan, run["start_mono_ns"], run["start_mono_ns"] + WINDOW)
    try:
        check_thresholds(summary)
    except EvidenceError as exc:
        summary.update(row_result="FAIL", row_reason=str(exc))
    else:
        summary.update(row_result="PASS", row_reason=None)
    focus = cell["focus"]
    summary.update(
        verdict="unavailable",
        scheduled_attempts=OPERATIONS,
        protected_coverage=summary["protected_observation"]["n"],
        focus_continuous=focus["continuous"],
        focus_trace_ns=focus["end_mono_ns"] - focus["start_mono_ns"],
        focus_work_ns=cell["end_mono_ns"] - cell["start_mono_ns"],
        focus_events=len(focus["events"]),
        rest_ns=cell["rest"]["end_mono_ns"] - cell["rest"]["ack_mono_ns"],
    )
    return summary


def measure(args, stock):
    run = {
        "arm": args.arm, "mode": "QT2B_MEASUREMENT_ONLY", "numeric_evidence": "MEASURED",
        "paired_qualification": "UNAVAILABLE", "unavailable": [*ABSENT_CONTROLS, "thermal"],
        "error": None, "cells": {}, "summaries": {}, "samples": [], "kernel_uvm_watermarks": None,
        "local_reuse": None, "provider_reuse": None,
    }
    try:
        run["scope"], cgroup = signature()
        plan = json.loads(args.plan.read_bytes())
        plan_check(plan)
        run["plan_sha256"] = digest(plan)
        verify_pins(plan, args.plan, args.driver)
        repo = Path(__file__).resolve().parents[4]
        run["commit"] = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        run["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        run["stock_collector_sha256"] = sha256(STOCK_PATH.read_bytes()).hexdigest()
        run["measurement_entry_sha256"] = sha256(Path(__file__).read_bytes()).hexdigest()
        deadline = int(os.environ["QT2B_DEADLINE_EPOCH"])
        first_timer = int(os.environ["QT2B_FIRST_TIMER"])
        require(1 <= first_timer <= len(plan["combinations"]), "requested timer is outside the plan")
        require(args.arm == "baseline", "QT2b is a single-arm measurement")
        require(deadline * SECOND - time_ns() >= WINDOW + 5 * SECOND,
                "insufficient time to complete the required sampling window")
        driver = stock.load_driver(args.driver).prepare(plan)
        run["start_mono_ns"] = now()
        last_slot = run["start_mono_ns"] + WINDOW
        print(json.dumps({"qt2b_collection": "started", "combinations": list(plan["combinations"]),
                          "first_timer": first_timer}), flush=True)
        try:
            with Sampler(AvailableTelemetry(cgroup), run["samples"], run["start_mono_ns"]):
                for index, (combo, expected) in enumerate(plan["combinations"].items(), 1):
                    if index < first_timer:
                        continue
                    remaining = min(last_slot - now(), deadline * SECOND - time_ns())
                    if remaining <= REST + 10 * SECOND:
                        run["summaries"][combo] = {
                            "verdict": "not-done", "reason": "insufficient time for rest and collection",
                            "remaining_ns": max(0, remaining),
                        }
                        continue
                    cell = run["cells"][combo] = {}
                    try:
                        stock.collect_cell(driver, combo, expected, plan, cell)
                        run["summaries"][combo] = cell_summary(run, cell, expected, plan)
                    except Exception as exc:
                        run["summaries"][combo] = {
                            "verdict": "not-done", "reason": error_reason(exc),
                            "recorded_attempts": len(cell.get("operations", [])),
                            "remaining_ns": max(0, min(last_slot - now(), deadline * SECOND - time_ns())),
                        }
                    print(json.dumps({"qt2b_cell": combo, **run["summaries"][combo]}), flush=True)
        finally:
            run["sampling_slots_observed"] = len(run["samples"])
            run["sampling_slots_required"] = WINDOW // INTERVAL
        require(len(run["samples"]) == WINDOW // INTERVAL, "resource samples missing")
    except Exception as exc:
        run["error"] = error_reason(exc)
    run["end_mono_ns"] = now()
    return run


if __name__ == "__main__":
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("qt2_stock", STOCK_PATH)
    stock = module_from_spec(spec)
    spec.loader.exec_module(stock)
    stock.payload = lambda args: measure(args, stock)
    raise SystemExit(stock.main())
