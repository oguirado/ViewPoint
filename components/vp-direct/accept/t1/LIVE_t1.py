"""LIVE only. Measure T1 on pre-existing private Chrome/Xvfb timer fixtures."""

import argparse
from contextlib import ExitStack
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import sleep
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark import (TimedStream, TransitionVerifier, competing_paths, context, host_sample, now,
                       paired_receipt, placement, scope_dir, scope_path, throttle_paths, verify_frame)
from score import (CAPACITY, CYCLES, DIRECT, DURATION_NS, check_pair_plan, contract, cycle_plan, digest,
                   require, score, source_ceilings)

from admission import AdmissionGate, AdmissionPlan, ResourceUsage, UsageSnapshot
from admission.gate import MAX_SURFACES
from bus.broker import Broker
from perception.baseline.inventory import collect
from perception.demand import DemandCoordinator
from perception.demand.tests.LIVE_recording_sleep import check_window
from perception.lifecycle import LifecycleError, PerceptionLifecycle, RuntimeEvidence
from perception.receipts import ReceiptError
from perception.stream import StreamError
from perception.stream.tests.LIVE_fixture_cycles import private_source, verify_fixture
from vadapt import rtring


def start_ticks(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def provision(entry, broker, ring_bytes, own_scope, stack, verifier):
    args = SimpleNamespace(**entry["private_fixture"])
    args.synthetic_profile = Path(args.synthetic_profile)
    require(args.synthetic_profile.is_absolute()
            and args.synthetic_profile == args.synthetic_profile.resolve()
            and args.synthetic_profile != Path.home()
            and Path.home() / ".local" / "share" / "vp-profiles" not in args.synthetic_profile.parents
            and args.synthetic_profile != Path.home() / ".local" / "share" / "vp-profiles",
            "Expected a dedicated synthetic profile path")
    verify_fixture(args.fixture_port)
    display, source = private_source(args.xvfb_pid)
    xid = check_window(display, args)
    identities = tuple(start_ticks(pid) for pid in (args.browser_pid, args.xvfb_pid))
    session, binding, agent = (json.loads(Path(entry[key]).read_bytes())
                               for key in ("session", "binding", "agent_demand"))
    for name, record in (("vp.session/v1", session), ("vp.surface_binding/v1", binding),
                         ("vp.demand/v1", agent)):
        contract(name, record)
    require(session["kind"] == "browser" and session["browser"] == "google-chrome"
            and session["state"] == "AGENT" and session["window"]["pid"] == args.browser_pid
            and int(session["window"]["xid"], 0) == int(xid, 0)
            and session["incarnation"] == binding["incarnation"]
            and session["lease"]["authority_epoch"] == binding["authority_epoch"]
            and session["lease"]["holder"] == binding["owner"]
            and binding["surface_id"] == entry["surface_id"], "Private fixture binding differs")
    require(rtring.ring_bytes(2, source.width, source.height) == ring_bytes, "Fixture ring cost differs")

    def qualified(current):
        return (current == binding and private_source(args.xvfb_pid)[0] == display
                and check_window(display, args) == xid
                and identities == tuple(start_ticks(pid) for pid in (args.browser_pid, args.xvfb_pid)))

    def runtime():
        scope, = collect([("runner", own_scope)])["scopes"]
        pids = {p["pid"] for p in scope["processes"]}
        require(args.browser_pid not in pids and args.xvfb_pid not in pids, "Fixture shares runner scope")
        require(qualified(binding), "Private fixture changed")
        # The runner owns the synchronous agent reader. Any other PID in its scope is exclusive.
        require(os.getpid() in pids, "Runner missing from its scope")
        return RuntimeEvidence(binding["incarnation"], tuple(sorted(pids - {os.getpid()})),
                               scope["current_bytes"])

    stream = TimedStream(binding, source, ring_slots=2, interval_ns=20_000_000,
                         ttl_ns=3_000_000_000, max_frame_bytes=args.max_frame_bytes,
                         max_ring_bytes=ring_bytes)
    stack.callback(stream.close)
    coordinator = DemandCoordinator(binding["surface_id"], interests=["VERIFIED"],
                                    authorize=lambda row: row == agent, drain=lambda: True)
    broker.bind(session["session_id"], binding, lease_state="AGENT")
    owner = PerceptionLifecycle(broker, session["session_id"], binding, coordinator, agent, stream,
                                qualified=qualified, runtime=runtime,
                                drain_agent=lambda _: not runtime().exclusive_pids, on_transition=verifier)
    return owner, (display, args.browser_pid, args.xvfb_pid, args.synthetic_profile), qualified


def park(owner, state, verifier):
    if state == "ASLEEP":
        verifier.call(owner, "sleep")
    elif state == "OFF":
        verifier.call(owner, "off")


def wait_until(deadline):
    while (remaining := deadline - now()) > 0:
        sleep(remaining / 1_000_000_000)


def cycle(index, surface, state, owner, initial, gate, ring_bytes, blocked, verifier):
    before = host_sample()
    submit = now()
    row = {"cycle": index, "surface_id": surface, "state": state, "host_before": before,
           "wake_admission": None}
    admitted = first = None
    if surface in blocked:
        row["failure"] = "unsettled_previous_sample"
        outcome, complete = "failed", now()
    elif owner is None:
        outcome = ("capacity_refused" if initial["surface_count"] == CAPACITY + 1
                   and initial["margin_percent"] >= 15 else "refused")
        complete = now()
    else:
        try:
            with gate.reserve(AdmissionPlan(surface, state, "private-xvfb", False, ring_bytes)) as decision:
                row["wake_admission"] = decision
                if decision["admitted"]:
                    admitted = now()
                    p4, p7, states, observation = verifier.call(owner, "wake")
                    first = owner.stream.first_byte_ns
                    verify_frame(owner, observation)
                    complete = now()
                    row.update(p4=p4, p7=p7, broker_states=states)
                    outcome = "verified"
                    park(owner, state, verifier)
                else:
                    outcome, complete = "refused", now()
        except (LifecycleError, ReceiptError, StreamError, ValueError, OSError, subprocess.SubprocessError) as error:
            outcome, complete = "failed", now()
            row["failure"] = getattr(error, "code", type(error).__name__)
            if admitted is not None:
                blocked.add(surface)
    row.update(outcome=outcome, markers=dict(t_submit=submit, t_admitted=admitted,
                                            t_first_byte=first, t_complete=complete),
               host_after=host_sample())
    return row


def run(args, output):
    manifest_bytes = args.manifest.read_bytes()
    manifest = json.loads(manifest_bytes)
    entries = manifest["surfaces"]
    roster = [{key: entry[key] for key in ("surface_id", "state")} for entry in entries]
    plan = cycle_plan(roster)
    ring_bytes = manifest["ring_bytes"]
    require(type(ring_bytes) is int and ring_bytes > 0 and MAX_SURFACES == CAPACITY,
            "Invalid fixture ring cost or changed surface ceiling")
    require(type(args.warmups) is int and args.warmups >= 0, "Warmup count cannot be negative")
    ceilings = source_ceilings(DIRECT.parents[1])
    pair_plan = None
    competing, throttle = {}, {}
    if args.pair_plan is not None:
        pair_plan = json.loads(args.pair_plan.read_bytes())
        check_pair_plan(pair_plan)
        competing = competing_paths(pair_plan["competing_cgroups"], scope_dir())
        throttle = throttle_paths(pair_plan["throttle_counters"])
    benchmark = context(args.arm, args.order_index, workload="private-chrome-t1",
                        fixture_sha256=sha256((DIRECT / "fixtures/site/timer.html").read_bytes()).hexdigest(),
                        warmups=args.warmups, samples=len(plan), competing=competing, throttle=throttle)
    benchmark["warmup_markers"] = []
    host_samples = []
    benchmark["workload_sha256"] = digest({"roster": roster, "ring_bytes": ring_bytes, "plan": plan})
    benchmark["manifest_sha256"] = sha256(manifest_bytes).hexdigest()
    benchmark["runner_sha256"] = {name: sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                  for name in ("LIVE_t1.py", "benchmark.py", "score.py")}
    own_scope = scope_path()
    scopes = [("runner", own_scope)] + [(f"private-fixture-{i}", path)
                                      for i, path in enumerate(manifest["fixture_cgroups"])]
    fixture_pids = {proc["pid"] for scope in collect(scopes)["scopes"] if scope["cgroup"] != own_scope
                    for proc in scope["processes"]}
    owners, admissions, qualified_by_surface = {}, {}, {}
    verifier = TransitionVerifier()

    def usage():
        surfaces = {}
        for surface, owner in owners.items():
            stream = owner.stream.status()
            reserved = stream["reserved_bytes"]
            surfaces[surface] = (ResourceUsage(stream["ring_id"], reserved, 0,
                                              reserved if stream["state"] == "ASLEEP" else 0)
                                 if reserved else None)
        return UsageSnapshot(surfaces)

    gate = AdmissionGate(usage, scopes=scopes)
    cycles, blocked = [], set()
    with TemporaryDirectory() as scratch, Broker(scratch) as broker, ExitStack() as stack:
        identities = []
        for entry in entries:
            surface, state = entry["surface_id"], entry["state"]
            with gate.reserve(AdmissionPlan(surface, state, "private-xvfb", False, ring_bytes)) as decision:
                admissions[surface] = decision
                if not decision["admitted"]:
                    continue
                owner, identity, qualified = provision(entry, broker, ring_bytes, own_scope, stack, verifier)
                require(set(identity[1:3]) <= fixture_pids
                        and all(value not in {prior[i] for prior in identities}
                                for i, value in enumerate(identity)), "Shared or unaccounted private fixture")
                identities.append(identity)
                owners[surface] = owner
                qualified_by_surface[surface] = qualified
                if state != "OFF":
                    _, _, _, observation = verifier.call(owner, "wake")
                    verify_frame(owner, observation)
                    park(owner, state, verifier)

        for index in range(args.warmups):
            surface, state = plan[index % len(plan)]
            placement()
            row = cycle(index, surface, state, owners.get(surface), admissions[surface], gate,
                        ring_bytes, blocked, verifier)
            require(row["outcome"] in ("verified", "capacity_refused"), "Warmup did not settle")
            benchmark["warmup_markers"].append(row["markers"])

        cohort_start = [broker.perception_state(surface) for surface in owners]
        start = now()
        for index, (surface, state) in enumerate(plan):
            wait_until(start + index * (DURATION_NS // CYCLES))
            placement()
            if pair_plan is not None:
                host_samples.append(host_sample(competing=competing, throttle=throttle))
            cycles.append(cycle(index, surface, state, owners.get(surface), admissions[surface], gate,
                                ring_bytes, blocked, verifier))
        wait_until(start + DURATION_NS)
        end = now()
        cohort_end = [broker.perception_state(surface) for surface in owners]

        # Recheck application identity even for the open OFF cohort.
        for surface, owner in owners.items():
            require(qualified_by_surface[surface](owner.binding), "Private fixture exited during the scenario")

    benchmark["after"] = host_sample(competing=competing, throttle=throttle)
    capture = dict(roster=roster, admissions=list(admissions.values()), surface_ceiling=MAX_SURFACES,
                   source_ceilings=ceilings, cohort_start=cohort_start, cohort_end=cohort_end,
                   plan_sha256=digest(plan), start_mono_ns=start, end_mono_ns=end,
                   cycles=cycles)
    if pair_plan is None:
        receipt = dict(capture, benchmark=benchmark)
    else:
        receipt = paired_receipt(capture, benchmark, digest(pair_plan), host_samples)
    json.dump(receipt, output, sort_keys=True, indent=2, allow_nan=False)
    output.write("\n")
    output.flush()
    result = score(capture)
    print(json.dumps(dict(result, raw_capture=str(args.output)), sort_keys=True))
    return int(result["result"] == "FAIL")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--arm", required=True, choices=("baseline", "candidate"))
    parser.add_argument("--order-index", required=True, type=int)
    parser.add_argument("--warmups", required=True, type=int)
    parser.add_argument("--pair-plan", type=Path)
    args = parser.parse_args()
    placement()
    with args.output.open("x") as output:
        return run(args, output)


if __name__ == "__main__":
    raise SystemExit(main())
