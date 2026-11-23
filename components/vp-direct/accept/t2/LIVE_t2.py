"""LIVE A5 collection on a pre-provisioned private fixture. See README.md."""

import argparse
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from score import CPUS, OPERATIONS, REST, WAKE, EvidenceError, check_inventory, digest, plan_check, require, timeline
from telemetry import HostTelemetry, Sampler, now, signature
from focus import FocusMonitor, FocusRefused
from focus.x11 import X11FocusSource
from privacy import PrivacyBlocked, PrivacySentinel
from schemas.validation import canonical_bytes, validate_contract


class Markers:
    """The caller marks actual admission and receipt of the first result byte."""

    def __init__(self):
        require(sorted(os.sched_getaffinity(0)) == CPUS, "timed worker lost CPU placement")
        self.row = {"t_submit": now(), "t_admitted": None, "t_first_byte": None}

    def admitted(self):
        require(self.row["t_admitted"] is None, "duplicate admission marker")
        self.row["t_admitted"] = now()

    def first_byte(self):
        require(self.row["t_admitted"] is not None and self.row["t_first_byte"] is None,
                "unexpected first-byte marker")
        self.row["t_first_byte"] = now()

    def finish(self):
        self.row["t_complete"] = now()
        timeline(self.row)
        return self.row


def protected(driver, combo, binding, *, login):
    privacy_binding, read, capture, gap, human = driver.privacy_ports(combo, login=login)
    require(privacy_binding.surface_id == binding["surface_id"]
            and privacy_binding.authority_epoch == binding["authority_epoch"]
            and privacy_binding.incarnation == binding["incarnation"], "privacy source binding changed")
    sentinel = PrivacySentinel(privacy_binding, read, record_gap=gap, request_human=human, clock=now)
    calls = {"acquisitions": 0, "publications": 0}

    def acquire(metadata):
        calls["acquisitions"] += 1
        if not login:
            return capture(metadata)

    def publish(frame):
        calls["publications"] += 1
        return frame

    started = now()
    try:
        frame = sentinel.observe(acquire, publish)
    except PrivacyBlocked as exc:
        if login:
            return dict(calls, code=exc.code)
        return None
    if login:
        return dict(calls, code="OK")
    validate_contract("vp.frame/v1", frame)
    return {"start_mono_ns": started, "end_mono_ns": now(), "frame": frame}


def session_check(session, binding, display, server_pid, expected):
    validate_contract("vp.session/v1", session)
    validate_contract("vp.surface_binding/v1", binding)
    require(re.fullmatch(r":[1-9][0-9]*", display)
            and session["display"] == f"xpra:{display[1:]}"
            and session["site_group"] == "fixture" and session["state"] == "AGENT"
            and session["backend"] == expected["backend"]
            and session["incarnation"] == binding["incarnation"]
            and session["lease"]["authority_epoch"] == binding["authority_epoch"]
            and session["lease"]["holder"] == binding["owner"], "private fixture identity missing")
    require(type(server_pid) is int and server_pid > 0
            and (Path("/proc") / str(server_pid) / "exe").resolve(strict=True).name == "Xvfb"
            and Path("/proc", str(session["window"]["pid"]), "stat").is_file(),
            "private server or fixture app identity missing")


def collect_cell(driver, combo, expected, plan, cell):
    # target() supplies identity only. X11 activity starts after the native trace is armed.
    display, server_pid, session, binding = driver.target(combo)
    session_check(session, binding, display, server_pid, expected)
    cell.update(session=session, private_display=display, operations=[], wakes={state: [] for state in WAKE})
    monitor = FocusMonitor(X11FocusSource(display, server_pid), clock=now)

    def run():
        require(driver.verify_fixture(combo, session, binding) is True, "private fixture witness unavailable")
        cell["start_mono_ns"] = now()
        before = deepcopy(driver.inventory())
        attach_start = now()
        receipt = driver.attach(combo)
        attach_end = now()
        try:
            validate_contract("vp_workspace_call.attach.result/v1", receipt)
            require(receipt == {"session_id": session["session_id"], "binding": binding}, "attach identity changed")
            after = deepcopy(driver.inventory())
            cell["attach"] = {"before": before, "after": after, "start_mono_ns": attach_start,
                              "end_mono_ns": attach_end, "receipt": receipt}
            check_inventory(before, after)
            cell["privacy_denial"] = protected(driver, combo, binding, login=True)
            require(cell["privacy_denial"] == {"acquisitions": 0, "publications": 0, "code": "HUMAN_NEEDED"},
                    "synthetic login exclusion failed")
            completed = 0
            for _ in range(plan["warmups"]):
                require(driver.warmup(combo) is True, "fixture warm-up failed")
                completed += 1
            cell["warmups_completed"] = completed
            for index in range(OPERATIONS):
                marks = Markers()
                row = dict(index=index, request=None, reply=None, privacy=None)
                try:
                    observation = protected(driver, combo, binding, login=False)
                    if observation is not None:
                        request, reply = driver.perform(combo, index, observation["frame"], marks)
                        validate_contract("vp.channel.req/v1", request)
                        validate_contract("vp.channel.reply/v1", reply)
                        saved_request = {key: value for key, value in request.items()
                                         if key not in {"input_text", "slot_id", "url", "key"}}
                        row.update(request=saved_request, reply=reply, privacy=observation)
                finally:
                    row.update(marks.finish())
                    cell["operations"].append(row)
            rest = driver.rest(combo, REST)
            validate_contract("vp.transition_receipt/v1", rest["receipt"])
            cell["rest"] = rest
            for state in WAKE:
                for _ in range(plan["wake_samples"]):
                    prior = driver.prepare_wake(combo, state)
                    validate_contract("vp.perception_state/v1", prior)
                    require(prior["state"] == state and prior["session_id"] == session["session_id"]
                            and all(prior[key] == binding[key] for key in ("surface_id", "incarnation", "authority_epoch")),
                            "wake state differs from the attached session")
                    marks = Markers()
                    row = {"prior_state": prior, "receipt": None}
                    try:
                        wake = driver.wake(combo, state, marks)
                        validate_contract("vp.transition_receipt/v1", wake)
                        row["receipt"] = wake
                    finally:
                        row.update(marks.finish())
                        cell["wakes"][state].append(row)
        finally:
            cell["detach"] = driver.detach(combo, session["session_id"])
            validate_contract("vp_workspace_call.detach.result/v1", cell["detach"])
            require(driver.verify_fixture(combo, session, binding) is True, "foreign fixture identity lost on detach")
            cell["end_mono_ns"] = now()

    try:
        _, cell["focus"] = monitor.run_passive(binding["surface_id"], run, qualified=True)
    except FocusRefused as exc:
        cell["focus"] = exc.trace
        raise


def load_driver(path):
    spec = importlib.util.spec_from_file_location("a5_operator_fixture", path.resolve(strict=True))
    require(spec is not None and spec.loader is not None, "trusted fixture adapter required")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def payload(args):
    run = {"arm": args.arm, "numeric_evidence": "MEASURED", "error": None, "cells": {}, "samples": [],
           "kernel_uvm_watermarks": None, "local_reuse": None, "provider_reuse": None}
    try:
        run["scope"], cgroup = signature()
        plan = json.loads(args.plan.read_bytes())
        plan_check(plan)
        run["plan_sha256"] = digest(plan)
        require(sha256(args.driver.read_bytes()).hexdigest() == plan["workload_sha256"], "workload source changed")
        for name, pin in plan["fixtures"].items():
            fixture = (args.plan.parent / name).resolve(strict=True)
            require(fixture.is_relative_to(args.plan.parent.resolve()), "fixture outside plan directory")
            bytes_on_disk = fixture.read_bytes()
            require(sha256(bytes_on_disk).hexdigest() == pin, "fixture bytes changed")
        repo = Path(__file__).resolve().parents[4]
        run["commit"] = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        run["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        telem = HostTelemetry(cgroup, plan["competing_cgroups"], plan["throttle_counters"])
        require(bool(telem.thermal_paths) and bool(telem.throttle_paths), "thermal telemetry AUSENTE")
        driver = load_driver(args.driver).prepare(plan)
        run["start_mono_ns"] = now()
        with Sampler(telem, run["samples"], run["start_mono_ns"]):
            for combo, expected in plan["combinations"].items():
                cell = {}
                run["cells"][combo] = cell
                collect_cell(driver, combo, expected, plan, cell)
        run["end_mono_ns"] = now()
    except Exception as exc:
        # Exception messages may contain application content.
        run["error"] = str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__
        run["end_mono_ns"] = now()
    return run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=("baseline", "candidate"), required=True)
    for name in ("plan", "driver", "receipt"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    # Reserve the raw receipt before any operation. An interrupted arm cannot be replayed.
    os.umask(0o077)
    with args.receipt.open("xb") as output:
        run = payload(args)
        data = canonical_bytes(run)
        output.write(data)
    print(json.dumps({"status": "UNSCORED" if run["error"] is None else "UNAVAILABLE",
                      "path": str(args.receipt.resolve()), "sha256": sha256(data).hexdigest()}, sort_keys=True))
    return int(run["error"] is not None)


if __name__ == "__main__":
    sys.exit(main())
