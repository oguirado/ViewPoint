"""QT2b collection with explicitly absent paired controls and the stock cell gates."""

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
from select import select
import subprocess
import sys
from time import sleep, time_ns

# The sampler subprocess uses isolated Python and the same harness directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from score import (INTERVAL, OPERATIONS, PROFILE, REST, WINDOW, EvidenceError, check_cell,
                   check_thresholds, digest, is_digest, natural, require)
from telemetry import HostTelemetry, now, signature

ABSENT_CONTROLS = ("competing_cgroups", "pair_tolerance", "throttle_counters")
SECOND = 1_000_000_000
STOCK_PATH = Path("/work/qt2-stock.py")


def timer_names(count):
    return [f"timer-{index}/cdp/observe" for index in range(1, count + 1)]


def plan_check(plan):
    require(all(key in plan and plan[key] is None for key in ABSENT_CONTROLS),
            "QT2b requires explicit absence of all paired controls")
    require(is_digest(plan["workload_sha256"]), "workload digest missing")
    require(set(plan["fixtures"]) == {"timer.html", "login.html"}
            and all(map(is_digest, plan["fixtures"].values())), "fixture digests missing")
    require(natural(plan["warmups"]), "warm-up count missing")
    require(natural(plan["wake_samples"]) and plan["wake_samples"] > 0, "wake samples missing")
    require(isinstance(plan["combinations"], dict) and bool(plan["combinations"]), "profile matrix missing")
    require(set(plan["combinations"]) == set(timer_names(len(plan["combinations"]))),
            "unexpected QT2b timer identifiers")
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


def wait_until(stamp):
    while (remaining := stamp - now()) > 0:
        sleep(remaining / SECOND)


def sample_host(output_path):
    scope, cgroup = signature()
    telemetry = AvailableTelemetry(cgroup)
    print(json.dumps(scope), flush=True)
    start = int(sys.stdin.readline())
    with output_path.open("x") as output:
        for index, due in enumerate(range(start, start + WINDOW, INTERVAL)):
            wait_until(due)
            if now() >= due + INTERVAL:
                continue
            sample = telemetry.read()
            if due <= sample["mono_ns"] <= sample["end_mono_ns"] < due + INTERVAL:
                output.write(json.dumps({"slot_index": index, **sample}) + "\n")
        wait_until(start + WINDOW)


@contextmanager
def sampler_process(path, run):
    worker = subprocess.Popen(
        [sys.executable, "-IB", str(Path(__file__).resolve()), "--sample", str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        require(select([worker.stdout], [], [], 8)[0], "resource sampler startup timed out")
        run["sampler_scope"] = json.loads(worker.stdout.readline())
        require(run["sampler_scope"] == run["scope"], "resource sampler placement differs")
        start = now() + INTERVAL
        worker.stdin.write(f"{start}\n")
        worker.stdin.close()
        wait_until(start)
        yield start
        timeout = max(1, (start + WINDOW - now()) / SECOND + 2)
        require(worker.wait(timeout=timeout) == 0, "resource sampler failed")
    finally:
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=3)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=3)
        worker.stdin.close()
        worker.stdout.close()
        if path.is_file():
            with path.open() as captured:
                run["samples"].extend(json.loads(line) for line in captured)


def error_reason(exc):
    # Unexpected errors may contain application content.
    return str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__


def cell_summary(run, cell, expected, plan):
    focus = cell["focus"]
    last_slot = run["start_mono_ns"] + WINDOW
    trace_end = focus["end_mono_ns"]
    summary = check_cell(cell, expected, plan, run["start_mono_ns"], max(last_slot, trace_end))
    try:
        require(trace_end <= last_slot, "focus trace outside sampling window")
        check_thresholds(summary)
    except EvidenceError as exc:
        summary.update(row_result="FAIL", row_reason=str(exc))
    else:
        summary.update(row_result="PASS", row_reason=None)
    summary.update(
        verdict="not qualified" if summary["row_result"] == "FAIL" else "unavailable",
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
        run["scope"], _ = signature()
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
        names = timer_names(len(plan["combinations"]))
        require(1 <= first_timer <= len(names), "requested timer is outside the plan")
        run["selected_combinations"] = names[first_timer - 1:]
        run["previous_combinations"] = names[:first_timer - 1]
        require(args.arm == "baseline", "QT2b is a single-arm measurement")
        require(deadline * SECOND - time_ns() >= WINDOW + 5 * SECOND,
                "insufficient time to complete the required sampling window")
        driver = stock.load_driver(args.driver).prepare(plan)
        minimum_budget = REST + 10 * SECOND
        try:
            with sampler_process(args.receipt.parent / "qt2b-telemetry.jsonl", run) as start:
                run["start_mono_ns"] = start
                last_slot = start + WINDOW
                print(json.dumps({"qt2b_collection": "started", "combinations": run["selected_combinations"],
                                  "first_timer": first_timer}), flush=True)
                for combo in run["selected_combinations"]:
                    started = now()
                    window_left = max(0, last_slot - started)
                    deadline_left = max(0, deadline * SECOND - time_ns())
                    if min(window_left, deadline_left) < minimum_budget:
                        summary = {
                            "verdict": "not-done", "reason": "insufficient time for another complete combination",
                            "window_remaining_ns": window_left, "deadline_remaining_ns": deadline_left,
                            "estimated_budget_ns": minimum_budget,
                        }
                    else:
                        cell = run["cells"][combo] = {}
                        expected = plan["combinations"][combo]
                        try:
                            stock.collect_cell(driver, combo, expected, plan, cell)
                            elapsed = now() - started
                            minimum_budget = max(minimum_budget, elapsed + 5 * SECOND)
                            summary = cell_summary(run, cell, expected, plan)
                        except Exception as exc:
                            attempts = len(cell.get("operations", []))
                            summary = {
                                "verdict": "not qualified" if attempts == OPERATIONS else "not-done",
                                "reason": error_reason(exc), "recorded_attempts": attempts,
                                "window_remaining_ns": max(0, last_slot - now()),
                                "deadline_remaining_ns": max(0, deadline * SECOND - time_ns()),
                            }
                    run["summaries"][combo] = summary
                    print(json.dumps({"qt2b_cell": combo, **summary}), flush=True)
        finally:
            run["sampling_slots_observed"] = len(run["samples"])
            run["sampling_slots_required"] = WINDOW // INTERVAL
        require(len(run["samples"]) == WINDOW // INTERVAL, "resource samples missing")
    except Exception as exc:
        run["error"] = error_reason(exc)
    run["end_mono_ns"] = now()
    run["end_epoch_ns"] = time_ns()
    return run


if __name__ == "__main__":
    from importlib.util import module_from_spec, spec_from_file_location

    if len(sys.argv) == 3 and sys.argv[1] == "--sample":
        sample_host(Path(sys.argv[2]))
        raise SystemExit(0)
    spec = spec_from_file_location("qt2_stock", STOCK_PATH)
    stock = module_from_spec(spec)
    spec.loader.exec_module(stock)
    stock.payload = lambda args: measure(args, stock)
    raise SystemExit(stock.main())
