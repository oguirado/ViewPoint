"""Profile T2 protected observations using only QD's provisioned private fixtures."""

import argparse
from collections import Counter
from contextlib import ExitStack, contextmanager
from functools import wraps
from hashlib import sha256
import json
import os
from pathlib import Path
from statistics import median
import subprocess
import sys
from time import CLOCK_MONOTONIC, clock_gettime_ns
from unittest.mock import patch


DIRECT = Path(__file__).resolve().parents[2]
DRIVER = DIRECT / "qa/live_driver"
PERF = DIRECT / "qa/perf"
SOURCE = DIRECT / "privacy/__init__.py"
CPUS = [16, 17, 18, 19]
LIMITS = {"memory.high": 6 << 30, "memory.max": 8 << 30}
WARMUPS = 2
SAMPLES = 100
VIEWPORT = [1920, 1080]
PINNED_INPUTS = (
    "accept/t2/LIVE_t2.py", "accept/t2/score.py", "accept/t2/telemetry.py",
    "attach/__init__.py", "bus/broker/broker.py", "channel/cdp/__init__.py",
    "fixtures/fixture_server.py", "focus/__init__.py", "focus/x11.py", "frames/source.py",
    "perception/demand/__init__.py", "perception/lifecycle/machine.py",
    "perception/receipts/__init__.py", "perception/stream/stream.py",
    "qa/live_driver/acceptance.py", "qa/live_driver/driver.py", "qa/live_driver/inventory.py",
    "qa/live_driver/ports.py", "qa/live_driver/runtime.py", "qa/live_driver/semantic.py",
    "qa/live_driver/t2_workload.py", "qa/perf/benchmark.py", "qa/perf/compare.py",
    "schemas/validation.py", "../wender-agent/wender/perceive/ax.py",
    "../wender-agent/wender/perceive/cdp_port.py",
)
sys.path[:0] = [str(DRIVER), str(PERF)]

from compare import (ARMS, LOAD5_TOLERANCE, OCCUPANCY_TOLERANCE,
                     TEMPERATURE_TOLERANCE_MILLIC, host_controls, require)


def now():
    return clock_gettime_ns(CLOCK_MONOTONIC)


def file_digest(path):
    return sha256(path.read_bytes()).hexdigest()


class Stages:
    """Measure exclusive stage time, including nested sentinel calls."""

    def __init__(self):
        self.elapsed = Counter()
        self.calls = Counter()
        self.children = []

    @contextmanager
    def span(self, name):
        self.children.append(0)
        started = now()
        try:
            yield
        finally:
            elapsed = now() - started
            self.elapsed[name] += elapsed - self.children.pop()
            self.calls[name] += 1
            if self.children:
                self.children[-1] += elapsed

    def wrap(self, owner, name, stage):
        original = getattr(owner, name)

        @wraps(original)
        def measure(*args, **kwargs):
            label = stage(*args, **kwargs) if callable(stage) else stage
            with self.span(label):
                return original(*args, **kwargs)

        return patch.object(owner, name, measure)


def telemetry_functions():
    os.environ["VP_PERF_DIRECT"] = str(DIRECT)
    from benchmark import host_snapshot, verify_affinity
    return host_snapshot, verify_affinity


def pin_inputs():
    return {relative: file_digest(DIRECT / relative) for relative in PINNED_INPUTS}


def git(*args):
    return subprocess.check_output(["git", "-C", str(DIRECT), *args]).strip()


def base_matches_checkout(base, head):
    return subprocess.run(
        ["git", "-C", str(DIRECT), "diff", "--quiet", base, head, "--",
         "privacy/__init__.py", *PINNED_INPUTS], check=False,
    ).returncode == 0


def set_fixture_frame(browser, index):
    # The benchmark variant of the stock timer has no wall-clock updates.
    from runtime import require as fixture_require

    tick = f"TICK_{index % 2}"
    fixture_require(browser.page.read_bool(
        f"window.__perfSetTick({json.dumps(tick)}) === true"
    ), "deterministic timer update failed")


def exercise(harness, workload, plan, stages, samples):
    from focus import FocusMonitor
    from focus.x11 import X11FocusSource
    from ports import Footprint, FramePorts
    from privacy import PrivacySentinel
    from frames import source
    from runtime import Browser, Display, Page, process_start
    from schemas.validation import validate_contract

    host_snapshot, verify_affinity = telemetry_functions()
    placement, scope = harness.signature()
    combo, = plan["combinations"]
    expected = plan["combinations"][combo]
    display, pid, session, binding = workload.target(combo)
    harness.session_check(session, binding, display, pid, expected)
    cell = workload.cells[combo]
    focus = FocusMonitor(X11FocusSource(display, pid), clock=now)
    fixture_pids = (cell.browser.child.pid, pid)

    def run():
        require(workload.verify_fixture(combo, session, binding), "fixture witness absent")
        identities = tuple(process_start(process) for process in fixture_pids)
        before = workload.inventory()
        attach_started = now()
        with stages.span("attach_and_wake"):
            attached = workload.attach(combo)
            validate_contract("vp_workspace_call.attach.result/v1", attached)
        attach_ns = now() - attach_started
        attach_stages = dict(stages.elapsed)
        try:
            require(attached == {"session_id": session["session_id"], "binding": binding},
                    "attach identity changed")
            require(workload.verify_fixture(combo, session, binding)
                    and tuple(process_start(process) for process in fixture_pids) == identities,
                    "owned fixture changed during attach")
            # Host-wide inventory is diagnostic here. Other private workloads
            # can start or exit independently of this cell's qualified fixture.
            after = workload.inventory()
            changed = {kind: len(set(before[kind]) ^ set(after[kind])) for kind in before}
            denial = harness.protected(workload, combo, binding, login=True)
            require(denial == {"acquisitions": 0, "publications": 0, "code": "HUMAN_NEEDED"},
                    "login reached the capture callback")
            for _ in range(WARMUPS):
                require(workload.warmup(combo), "CDP semantic warm-up failed")
            rows = []
            for index in range(1 + WARMUPS + samples):
                set_fixture_frame(cell.browser, index)
                stages.elapsed.clear()
                stages.calls.clear()
                marks = harness.Markers()
                with ExitStack() as stack:
                    for owner, name, label in (
                        (PrivacySentinel, "observe", "privacy_sentinel"),
                        (PrivacySentinel, "_read", "privacy_metadata_read"),
                        (FramePorts, "metadata", "privacy_metadata"),
                        (Footprint, "read", "privacy_footprint"),
                        (FramePorts, "current", "binding_witness"),
                        (source.FrameSource, "_capture_raster", "cdp_acquisition"),
                        (Browser, "check", "browser_identity"),
                        (Browser, "geometry", "window_geometry"),
                        (Display, "hidden", "workspace_visibility"),
                        (Page, "call", lambda _, method, *args, **kwargs:
                         {"Page.captureScreenshot": "cdp_screenshot",
                          "Accessibility.getFullAXTree": "cdp_ax"}.get(method, "cdp_witness")),
                        (source, "inspect_png", "png_validation_and_pixel_digest"),
                        (source, "sha256", "frame_sha256"),
                        (source, "validate_contract", "frame_and_demand_contracts"),
                        (harness, "validate_contract", "protected_frame_contract"),
                    ):
                        stack.enter_context(stages.wrap(owner, name, label))
                    protected = harness.protected(workload, combo, binding, login=False)
                require(protected is not None, "protected acquisition refused")
                frame = protected["frame"]
                require([frame["width"], frame["height"]] == VIEWPORT, "CDP viewport changed")
                # Verify the semantic response separately from the protected interval.
                with stages.span("semantic_cdp_observe"):
                    request, reply = workload.perform(combo, index, frame, marks)
                with stages.span("semantic_reply_contracts"):
                    validate_contract("vp.channel.req/v1", request)
                    validate_contract("vp.channel.reply/v1", reply)
                timing = marks.finish()
                harness.timeline(timing, complete=True)
                require(reply.get("ok") is True and reply.get("value_sha256") == expected["readback_sha256"],
                        "semantic readback differs")
                host = host_snapshot(scope)
                rows.append({
                    "stratum": "cold" if index == 0 else "warmup" if index <= WARMUPS else "warm",
                    "fixture_index": index % 2,
                    "frame_sha256": frame["frame_sha256"],
                    "protected_start_ns": protected["start_mono_ns"],
                    "protected_end_ns": protected["end_mono_ns"],
                    "timing": timing,
                    "stage_ns": dict(stages.elapsed),
                    "stage_calls": dict(stages.calls),
                    "host": {key: host[key] for key in (
                        "mono_ns", "loadavg", "mem_available_kib", "cpu_perf_limited",
                        "processor_cooling_state", "temperature_millicelsius",
                    )},
                })
                for process in fixture_pids:
                    verify_affinity(process)
            return {"operations": rows, "wakes": [], "privacy_denial": denial,
                    "inventory_delta": {kind: len(after[kind]) - len(before[kind]) for kind in before},
                    "host_identity_changes_during_attach": changed,
                    "attach_ns": attach_ns, "attach_stage_ns": attach_stages}
        finally:
            detached = workload.detach(combo, session["session_id"])
            validate_contract("vp_workspace_call.detach.result/v1", detached)
            require(workload.verify_fixture(combo, session, binding)
                    and tuple(process_start(process) for process in fixture_pids) == identities,
                    "owned fixture changed before detach completed")

    with patch.dict(os.environ, {"XAUTHORITY": cell.browser.display.env["XAUTHORITY"]}):
        row, trace = focus.run_passive(binding["surface_id"], run, qualified=True)
    require(trace["continuous"], "focus changed during the private run")
    row.update(focus={"continuous": True}, owned_identity_verified=True, benchmark_scope=placement,
               fixture_pins=plan["fixtures"], workload_sha256=plan["workload_sha256"],
               source_sha256=file_digest(SOURCE), profile=VIEWPORT)
    return {combo: row}


def run_private_worker(args, driver):
    import t2_workload
    from perception import receipts

    stages = Stages()
    with stages.wrap(receipts, "verify_transition", "transition_receipt_verification"):
        with patch.object(t2_workload, "exercise", lambda harness, workload, plan:
                          exercise(harness, workload, plan, stages, args.samples)):
            sys.argv = [str(DRIVER / "driver.py"), *args.driver_args]
            return driver.main()


def collect(args):
    import driver

    require(not args.output.exists(), "receipt output already exists")
    base = git("rev-parse", "--verify", "--end-of-options", f"{args.base}^{{commit}}").decode()
    head_before = git("rev-parse", "--verify", "HEAD").decode()
    require(base_matches_checkout(base, head_before), "benchmark inputs changed after reference commit")
    scope = driver.require_scope()
    limits = {key: int((scope / key).read_text()) for key in LIMITS}
    require(limits == LIMITS and sorted(os.sched_getaffinity(0)) == CPUS, "invalid host placement")
    source = args.source or SOURCE
    baseline = subprocess.check_output(
        ["git", "-C", str(DIRECT), "show", f"{base}:components/vp-direct/privacy/__init__.py"],
    )
    require(baseline == SOURCE.read_bytes() and (args.source is None) == (args.arm == "baseline"),
            "baseline source pin differs or wrong source override for arm")
    timer = DIRECT / "fixtures/site/timer.html"
    driver_sandbox = driver.sandbox

    def sandbox(work, worker_args, tool_dir, **kwargs):
        command = driver_sandbox(work, worker_args, tool_dir, **kwargs)
        separator = command.index("--")
        # Keep the stock page markup and use controlled text changes in its allowed region.
        fixture = work / "benchmark-timer.html"
        head, delimiter, tail = timer.read_text().partition("<script>")
        require(delimiter and "</script>" in tail, "stock timer fixture changed")
        fixture.write_text(head + """<script>
window.__perfSetTick = (text) => {
  const region = document.getElementById('timer-region');
  region.textContent = text;
  return region.textContent === text;
};
</script>""" + tail.split("</script>", 1)[1])
        return [*command[:separator], "--ro-bind", str(fixture),
                "/checkout/components/vp-direct/fixtures/site/timer.html",
                "--ro-bind", str(source.resolve(strict=True)),
                "/checkout/components/vp-direct/privacy/__init__.py",
                "--", *command[separator + 1:separator + 3],
                "/checkout/components/vp-direct/privacy/tests/perf_observation.py",
                "worker", "--samples", str(args.samples), *command[separator + 4:]]

    captured = {}

    def export(directory, _destination):
        capture = directory / "capture.json"
        if not capture.is_file():
            return
        captures = json.loads(capture.read_bytes())
        row, = captures["cells"].values()
        require(captures["status"] == "SMOKE" and row["source_sha256"] == file_digest(source),
                "private benchmark source pin differs")
        captured.update(row)

    host_snapshot, _ = telemetry_functions()
    inputs = pin_inputs()
    source_sha256 = file_digest(source)
    before = host_snapshot(scope)
    rc = None
    try:
        with patch.object(driver, "sandbox", sandbox), patch.object(driver, "export_acceptance", export):
            sys.argv = [str(DRIVER / "driver.py"), "--display", args.display,
                        "--apps", "1", "--arm", args.arm, "--warmups", str(WARMUPS),
                        "--smoke-workload", "--receipt-dir", str(args.output), "accept/t2/LIVE_t2.py"]
            rc = driver.main()
    finally:
        head_after = git("rev-parse", "--verify", "HEAD").decode()
        stable_inputs = (inputs == pin_inputs() and source_sha256 == file_digest(source)
                         and base_matches_checkout(base, head_after))
        report = {**captured, "before": before, "after": host_snapshot(scope), "rc": rc,
                  "inputs": inputs, "stable_inputs": stable_inputs,
                  "arm": args.arm, "base_commit": base, "checkout_heads": [head_before, head_after],
                  "benchmark_sha256": file_digest(Path(__file__)), "clock": "CLOCK_MONOTONIC",
                  "cpu_affinity": CPUS, "limits_bytes": limits, "warmups": WARMUPS, "samples": args.samples}
        with args.output.open("x") as output:
            json.dump(report, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
    return rc if stable_inputs else 1


def quantiles(values):
    return [median(values), sorted(values)[len(values) - len(values) // 20 - 1]]


def observations(report):
    require(report["rc"] == 0 and report["stable_inputs"] is True and report["clock"] == "CLOCK_MONOTONIC"
            and report["cpu_affinity"] == CPUS and report["limits_bytes"] == LIMITS
            and report["profile"] == VIEWPORT and report["focus"] == {"continuous": True}
            and report["owned_identity_verified"] is True
            and report["privacy_denial"] == {
                "acquisitions": 0, "publications": 0, "code": "HUMAN_NEEDED",
            }, "failed arm, incorrect placement or missing privacy witness")
    rows = report["operations"]
    require(report["warmups"] > 0 and report["samples"] > 0, "untimed arm")
    require([row["stratum"] for row in rows] == ["cold"] + ["warmup"] * report["warmups"]
            + ["warm"] * report["samples"], "missing, extra or misordered samples")
    pins = tuple(row["frame_sha256"] for row in rows[:2])
    require(pins[0] != pins[1], "alternating fixture pixels did not change")
    warm = []
    for index, row in enumerate(rows):
        marks = row["timing"]
        start, end = row["protected_start_ns"], row["protected_end_ns"]
        require(marks["t_submit"] <= start < end <= marks["t_admitted"] <= marks["t_first_byte"]
                <= marks["t_complete"], "inconsistent protection/semantic receipt boundaries")
        require(row["fixture_index"] == index % 2 and row["frame_sha256"] == pins[index % 2],
                "fixture pixels drifted")
        calls = row["stage_calls"]
        require(all(calls.get(stage) == count for stage, count in {
            "privacy_sentinel": 3, "privacy_metadata_read": 6, "privacy_footprint": 8, "cdp_screenshot": 1,
            "cdp_ax": 1, "png_validation_and_pixel_digest": 2, "frame_sha256": 3,
            "protected_frame_contract": 1, "semantic_reply_contracts": 1,
        }.items()), "privacy, capture, digest or receipt stage missing")
        if row["stratum"] == "warm":
            warm.append(dict(row["stage_ns"], protected=end - start,
                             proposal_to_receipt=marks["t_complete"] - marks["t_submit"]))
    telemetry = dict(report, blocks=[{"runs": rows}])
    return {
        "pins": pins,
        "stages": {key: quantiles([row[key] for row in warm]) for key in sorted(warm[0])},
        "host_controls": host_controls(telemetry),
    }


def compare(reports):
    require(tuple(report["arm"] for report in reports) == ARMS, "complete ABBA order required")
    for key in ("base_commit", "benchmark_sha256", "fixture_pins", "workload_sha256", "warmups", "samples", "inputs"):
        require(all(report[key] == reports[0][key] for report in reports), f"arm inputs differ: {key}")
    a1, b1, b2, a2 = [report["source_sha256"] for report in reports]
    require(a1 == a2 and b1 == b2 and a1 != b1, "missing distinct stable candidate")
    require(all(a["after"]["mono_ns"] < b["before"]["mono_ns"] for a, b in zip(reports, reports[1:])),
            "ABBA arms overlap or are out of order")
    summaries = [observations(report) for report in reports]
    require(all(summary["pins"] == summaries[0]["pins"] for summary in summaries),
            "alternating frame digests differ across arms")
    controls = [summary["host_controls"] for summary in summaries]
    pairs, confounds = [], []
    for baseline, candidate in ((0, 1), (3, 2)):
        for metric, tolerance in (("occupancy", OCCUPANCY_TOLERANCE), ("load5", LOAD5_TOLERANCE),
                                  ("peak_millic", TEMPERATURE_TOLERANCE_MILLIC)):
            if abs(controls[baseline][metric] - controls[candidate][metric]) > tolerance:
                confounds.append({"metric": metric, "baseline": baseline, "candidate": candidate})
        old = summaries[baseline]["stages"]["protected"]
        new = summaries[candidate]["stages"]["protected"]
        pairs.append({"baseline": baseline, "candidate": candidate,
                      "baseline_p50_p95_ns": old, "candidate_p50_p95_ns": new,
                      "passed": new[0] <= old[0] and new[1] < old[1]})
    return dict(result="INVALID" if confounds else "PASS" if all(pair["passed"] for pair in pairs) else "FAIL",
                pairs=pairs, confounds=confounds, arms=summaries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("collect")
    run.add_argument("--display", required=True)
    run.add_argument("--arm", choices=sorted(set(ARMS)), required=True)
    run.add_argument("--base", required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--source", type=Path)
    run.add_argument("--samples", type=int, default=SAMPLES)
    score = sub.add_parser("score")
    score.add_argument("receipts", type=Path, nargs="+")
    if sys.argv[1:2] == ["worker"]:
        import driver

        # The driver's forwarded arguments begin immediately after the sample count.
        args = argparse.Namespace(samples=int(sys.argv[3]), driver_args=sys.argv[4:])
        return run_private_worker(args, driver)
    args = parser.parse_args()
    if args.command == "collect":
        require(args.samples > 0, "positive sample count required")
        return collect(args)
    try:
        reports = [json.loads(path.read_bytes()) for path in args.receipts]
        if len(reports) == 1:
            result = dict(result="PROFILE", **observations(reports[0]))
        else:
            result = compare(reports)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        result = dict(result="INVALID", reason=str(error))
    result["raw_sha256"] = [file_digest(path) for path in args.receipts]
    print(json.dumps(result, sort_keys=True))
    return 0 if result["result"] in ("PROFILE", "PASS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
