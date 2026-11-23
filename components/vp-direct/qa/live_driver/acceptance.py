"""Provision private fixtures and launch the T1 and T2 acceptance producers."""

from contextlib import ExitStack
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from ports import binding_for, call_entry, demand_for, session_for
from runtime import SCREEN_HEIGHT, SCREEN_WIDTH, WORK, eventually, fixture_site, open_browser, require

HERE = Path(__file__).resolve().parent
CPUS = {16, 17, 18, 19}
T1_SIZE = (800, 600)
T1_MAX_FRAME = T1_SIZE[0] * T1_SIZE[1] * 4 + 4096
GROUP = Path("/sys/fs/cgroup")
T2_DRIVER = HERE / "t2_workload.py"


def frozen_json(path, data):
    from schemas.validation import canonical_bytes

    with path.open("xb") as stream:
        stream.write(canonical_bytes(data))
    return path


def load_harness(name):
    directory = HERE.parents[1] / "accept" / name
    sys.path.insert(0, str(directory))
    return importlib.import_module(f"LIVE_{name}")


def fixture_scope():
    membership = Path("/proc/self/cgroup").read_text().strip()
    require(membership.startswith("0::/") and membership.endswith(".scope/qd-fixture"),
            "acceptance needs a delegated private fixture cgroup")
    return membership[3:]


def rss_snapshot(directory):
    """Sum per-process RSS (shared pages can be counted more than once)."""
    pids = set((directory / "cgroup.procs").read_text().split()) - {"0"}
    rss = []
    for pid in pids:
        try:
            rows = Path(f"/proc/{pid}/status").read_text().splitlines()
        except (FileNotFoundError, ProcessLookupError):
            continue
        for row in rows:
            if row.startswith("VmRSS:"):
                rss.append(int(row.split()[1]) * 1024)
                break
    require(bool(rss), "fixture process RSS AUSENTE")
    return {"fixture_rss_bytes": sum(rss), "rss_processes": len(rss),
            "fixture_charged_bytes": int((directory / "memory.current").read_text()),
            "scope_charge_bytes": int((directory.parent / "memory.current").read_text()),
            "scope_peak_bytes": int((directory.parent / "memory.peak").read_text())}


def wait_for_fixture_inventory(scopes):
    from perception.baseline.inventory import IncompleteInventory, collect

    previous = None

    def stable():
        nonlocal previous
        try:
            census = collect(scopes)
        except IncompleteInventory:
            previous = None
            return False
        identities = [(p["pid"], p["starttime_ticks"]) for scope in census["scopes"]
                      for p in scope["processes"]]
        ready = identities == previous
        previous = identities
        return ready

    eventually(stable, "private fixtures did not reach a stable P1 process inventory", timeout=20)


def start_app(stack, displays, name, port, *, t2=False, login=False):
    display = displays.start(stack, name, framebuffer=not login,
                             size=(SCREEN_WIDTH, SCREEN_HEIGHT) if t2 else T1_SIZE, private_paths=True)
    if t2 and not login:
        display.start_bus(stack)
    if not login:
        display.start_xpra(stack)
    page = "login.html" if login else "timer.html"
    browser = stack.enter_context(open_browser(display, f"http://127.0.0.1:{port}/{page}", kiosk=t2 and not login))
    if t2 and not login:
        eventually(lambda: browser.geometry()[1] == display.size, "fullscreen CDP viewport is unavailable")
    return browser


def identity(browser, port):
    return {"display": browser.display.name, "xvfb_pid": browser.display.server.pid,
            "xid": browser.xid, "browser_pid": browser.child.pid, "cdp_port": browser.port,
            "fixture_port": port, "witness": browser.witness, "fixture_sha256": browser.fixture_digest,
            "synthetic_profile": str(browser.profile)}


def t1_manifest(requests, browsers, port, cgroup, ring_bytes, directory):
    from schemas.validation import validate_contract

    entries = []
    require(len(browsers) <= len(requests), "more T1 fixtures than planned requests")
    for index, state in enumerate(requests):
        if index >= len(browsers):
            entries.append({"state": state, "surface_id": f"qd-refusal-{index}"})
            continue
        browser = browsers[index]
        session = session_for(browser)
        binding = dict(binding_for(browser), surface_id=f"qd-t1-{index}", endpoint=f"qd-t1-app-{index}")
        demand = demand_for(binding, "agent")
        payload = {"state": state, "surface_id": binding["surface_id"]}
        for key, contract, row in (("session", "vp.session/v1", session),
                                    ("binding", "vp.surface_binding/v1", binding),
                                    ("agent_demand", "vp.demand/v1", demand)):
            validate_contract(contract, row)
            payload[key] = str(frozen_json(directory / f"{index}-{key}.json", row))
        fixture = identity(browser, port)
        payload["private_fixture"] = dict(fixture, max_frame_bytes=T1_MAX_FRAME)
        entries.append(payload)
    return frozen_json(directory / "manifest.json",
                       {"surfaces": entries, "fixture_cgroups": [cgroup], "ring_bytes": ring_bytes})


def t1_exercise(harness, manifest, scopes, scratch):
    """Exercise the stock T1 admission and wake functions without the timed schedule."""
    from admission import AdmissionGate, AdmissionPlan, ResourceUsage, UsageSnapshot
    from bus.broker import Broker

    owners, admissions = {}, {}
    verifier = harness.TransitionVerifier()

    def usage():
        surfaces = {}
        for surface, owner in owners.items():
            status = owner.stream.status()
            reserved = status["reserved_bytes"]
            surfaces[surface] = (ResourceUsage(status["ring_id"], reserved, 0,
                                                reserved if status["state"] == "ASLEEP" else 0)
                                 if reserved else None)
        return UsageSnapshot(surfaces)

    ring = manifest["ring_bytes"]
    gate = AdmissionGate(usage, scopes=scopes)
    with Broker(scratch) as broker, ExitStack() as stack:
        for entry in manifest["surfaces"]:
            surface, state = entry["surface_id"], entry["state"]
            with gate.reserve(AdmissionPlan(surface, state, "private-xvfb", False, ring)) as decision:
                admissions[surface] = decision
                if not decision["admitted"]:
                    continue
                require("private_fixture" in entry, "planned capacity refusal was unexpectedly admitted")
                owner, _, _ = harness.provision(entry, broker, ring, scopes[0][1], stack, verifier)
                owners[surface] = owner
                if state != "OFF":
                    _, _, _, observation = verifier.call(owner, "wake")
                    harness.verify_frame(owner, observation)
                    harness.park(owner, state, verifier)

        rows = []
        for state in ("ASLEEP", "OFF"):
            for admitted in (True, False):
                entry = next((row for row in manifest["surfaces"]
                              if row["state"] == state and (row["surface_id"] in owners) == admitted), None)
                if entry is None:
                    continue
                surface = entry["surface_id"]
                row = harness.cycle(len(rows), surface, state, owners.get(surface), admissions[surface],
                                    gate, ring, set(), verifier)
                rows.append(row)
        refused = [row for row in admissions.values() if not row["admitted"]]
        require(all(row["surface_count"] == harness.CAPACITY + 1 and row["margin_percent"] >= 15
                    for row in refused), "T1 refusal is not attributable to the capacity ceiling")
        expected = {"verified", "capacity_refused"} if refused else {"verified"}
        require({row["outcome"] for row in rows} == expected and
                len(owners) == min(len(admissions), harness.CAPACITY), "T1 exercise did not settle")
        return {"admissions": list(admissions.values()), "cycles": rows}


def run_t1(args, displays, directory, stack):
    from driver import enter_runner
    from vadapt import rtring

    harness = load_harness("t1")
    smoke_small = args.smoke_workload and args.apps == 2
    # The first sixteen requests represent the resident cohort in both full profiles.
    states = ["ASLEEP", "OFF"] if smoke_small else ["ACTIVE"] * 4 + ["ASLEEP"] * 6 + ["OFF"] * 6
    if args.apps == 32:
        states += ["ASLEEP"] * 6 + ["OFF"] * 10
    cgroup = fixture_scope()
    port = stack.enter_context(fixture_site())
    browsers = [start_app(stack, displays, name, port) for name in displays.planned]
    ring_bytes = rtring.ring_bytes(2, *T1_SIZE)
    manifest = t1_manifest(states, browsers, port, cgroup, ring_bytes, directory)
    roster = [{"surface_id": row["surface_id"], "state": row["state"]}
              for row in json.loads(manifest.read_bytes())["surfaces"]]
    plan = [(row["surface_id"], row["state"]) for row in roster] if smoke_small else harness.cycle_plan(roster)
    workload = {"roster": roster, "ring_bytes": ring_bytes, "plan": plan}
    frozen_json(directory / "t1-plan.json", workload)
    enter_runner(acceptance=True)
    scopes = [("runner", harness.scope_path()), ("fixture", cgroup)]
    wait_for_fixture_inventory(scopes)
    receipt = dict(rss_snapshot(GROUP / cgroup.lstrip("/")), requested=len(states), fixtures=len(browsers),
                   workload_sha256=harness.digest(workload), manifest_sha256=sha256(manifest.read_bytes()).hexdigest(),
                   fixture_sha256=browsers[0].fixture_digest)
    print(json.dumps(receipt), flush=True)
    capture = directory / "capture.json"
    if args.smoke_workload:
        receipt["placement"] = harness.placement()
        receipt["source_ceilings"] = harness.source_ceilings(harness.DIRECT.parents[1])
        receipt.update(t1_exercise(harness, json.loads(manifest.read_bytes()), scopes, directory / "broker"))
        receipt["scope_peak_bytes"] = int((GROUP / cgroup.lstrip("/")).parent.joinpath("memory.peak").read_text())
        frozen_json(capture, dict(receipt, status="SMOKE"))
        admitted = sum(row["admitted"] for row in receipt["admissions"])
        print(json.dumps({"cycles": [row["outcome"] for row in receipt["cycles"]], "admissions": admitted,
                          "capacity_refusals": len(states) - admitted, "scope_peak_bytes": receipt["scope_peak_bytes"],
                          "status": "SMOKE"}))
    else:
        pair = ["--pair-plan", "/work/controls.json"] if args.controls else []
        call_entry(args.check, [manifest, capture, "--arm", args.arm, "--order-index", args.order_index,
                                "--warmups", args.warmups, *pair])


def t2_plan(directory, apps, warmups, wake_samples, controls):
    from runtime import SITE
    from t2_workload import EXPECTED_READBACK

    fixtures = {}
    for name in ("timer.html", "login.html"):
        data = (SITE / name).read_bytes()
        (directory / name).write_bytes(data)
        fixtures[name] = sha256(data).hexdigest()
    plan = dict(
        workload_sha256=sha256(T2_DRIVER.read_bytes()).hexdigest(), fixtures=fixtures,
        combinations={f"timer-{index + 1}/cdp/observe": {
            "profile": "private-xvfb", "backend": "cdp", "op": "observe",
            "readback_sha256": sha256(EXPECTED_READBACK).hexdigest(),
        } for index in range(apps)},
        warmups=warmups, wake_samples=wake_samples,
        **controls,
    )
    return plan, frozen_json(directory / "t2-plan.json", plan)


def run_t2(args, displays, directory, stack):
    from driver import enter_runner, get_ports
    from t2_workload import T2Workload, exercise

    harness = load_harness("t2")
    controls = (json.loads(Path("/work/controls.json").read_bytes()) if args.controls else {
        "competing_cgroups": [], "throttle_counters": [],
        "pair_tolerance": dict.fromkeys(("load", "temperature_millic", "competing_memory_bytes",
                                         "competing_cpu_usec"), 0),
    })
    require(isinstance(controls, dict) and set(controls) ==
            {"competing_cgroups", "throttle_counters", "pair_tolerance"}, "invalid T2 telemetry controls")
    plan, plan_path = t2_plan(directory, args.apps, args.warmups, args.wake_samples, controls)
    harness.plan_check(plan)
    capture = directory / "capture.json"
    cgroup = fixture_scope()
    port = stack.enter_context(fixture_site())
    apps = {combo: start_app(stack, displays, name, port, t2=True)
            for combo, name in zip(plan["combinations"], displays.planned[:-1], strict=True)}
    login = start_app(stack, displays, displays.planned[-1], port, t2=True, login=True)
    require(all(browser.fixture_digest == plan["fixtures"]["timer.html"] for browser in apps.values())
            and login.fixture_digest == plan["fixtures"]["login.html"], "provisioned T2 fixture digest changed")
    identities = {name: identity(browser, port) for name, browser in apps.items()}
    identities["login"] = identity(login, port)
    frozen_json(directory / "manifest.json", identities)
    enter_runner(acceptance=True)
    resources = rss_snapshot(GROUP / cgroup.lstrip("/"))
    print(json.dumps(dict(resources, fixtures=len(apps), plan_sha256=harness.digest(plan),
                          workload_sha256=plan["workload_sha256"])), flush=True)
    get_ports().acceptance = T2Workload(plan, directory, stack, apps, login, cgroup)
    if args.smoke_workload:
        cells = exercise(harness, harness.load_driver(T2_DRIVER).prepare(plan), plan)
        placement, _ = harness.signature()
        frozen_json(capture, {"status": "SMOKE", "cells": cells, "scope": placement, "resources": resources,
                             "plan_sha256": harness.digest(plan)})
        print(json.dumps({"status": "SMOKE", "cells": {name: {"operations": len(row["operations"]),
                          "wakes": len(row["wakes"]), "focus": row["focus"]["continuous"],
                          "privacy_denial": row["privacy_denial"], "inventory_delta": row["inventory_delta"]}
                          for name, row in cells.items()}}))
    else:
        try:
            call_entry(args.check, ["--arm", args.arm, "--plan", plan_path, "--driver", T2_DRIVER,
                                    "--receipt", capture])
        except SystemExit:
            if capture.is_file() and capture.stat().st_size:
                error = json.loads(capture.read_bytes())["error"]
                print(json.dumps({"error": error}), flush=True)
            raise


def run_acceptance(args, displays, kind):
    directory = WORK / "acceptance"
    directory.mkdir(mode=0o700)
    os.environ["XAUTHORITY"] = str(WORK / "Xauthority")
    with ExitStack() as stack:
        entry = run_t1 if kind == "t1" else run_t2
        entry(args, displays, directory, stack)
