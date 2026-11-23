"""LIVE only. Exercise an operator-provisioned, single-window private Chrome fixture.

Run this file with python3 -I -B in its own capped systemd scope. Supply the
already-open P3 timer fixture on dedicated Xvfb, the positive P2 synthetic
profile/process witnesses, and the admitted session, binding and agent demand
JSON records. This runner is the broker/observer. P3 is pull-driven in that
process; every other process in the scope counts as exclusive. Chrome and
Xvfb must already be running outside it. Receipts are written to stdout.
"""
import argparse
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from unittest.mock import patch

DIRECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(DIRECT))
from bus.broker import Broker
from perception.baseline.inventory import collect
from perception.demand import DemandCoordinator
from perception.demand.tests.LIVE_recording_sleep import check_window
from perception.lifecycle import LifecycleError, PerceptionLifecycle, RuntimeEvidence
from perception.stream import PerceptionStream
from perception.stream.tests.LIVE_fixture_cycles import private_source, require, verify_fixture
from schemas.validation import validate_contract
from vadapt import rtring


def run(args, *, on_transition=None):
    verify_fixture(args.fixture_port)
    display, source = private_source(args.xvfb_pid)
    xid = check_window(display, args)
    session, binding, agent = [json.loads(path.read_bytes()) for path in
                               (args.session, args.binding, args.agent_demand)]
    validate_contract("vp.session/v1", session)
    validate_contract("vp.surface_binding/v1", binding)
    validate_contract("vp.demand/v1", agent)
    require(session["kind"] == "browser" and session["browser"] == "google-chrome"
            and session["state"] == "AGENT" and session["window"]["pid"] == args.browser_pid
            and int(session["window"]["xid"], 0) == int(xid, 0)
            and session["incarnation"] == binding["incarnation"]
            and session["lease"]["authority_epoch"] == binding["authority_epoch"]
            and session["lease"]["holder"] == binding["owner"], "session or lease mismatch")

    def start_tick(pid):
        return (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19]

    identity = (start_tick(args.browser_pid), start_tick(args.xvfb_pid))

    def qualified(current):
        return (current == binding and check_window(display, args) == xid
                and private_source(args.xvfb_pid)[0] == display
                and identity == (start_tick(args.browser_pid), start_tick(args.xvfb_pid)))

    def runtime():
        require(qualified(binding), "private fixture changed")
        scope, = collect()["scopes"]
        pids = {p["pid"] for p in scope["processes"]}
        require(os.getpid() in pids and args.browser_pid not in pids and args.xvfb_pid not in pids,
                "expected a dedicated runner scope separate from the fixture")
        return RuntimeEvidence(binding["incarnation"], tuple(sorted(pids - {os.getpid()})),
                               scope["current_bytes"])

    # This private rehearsal has one synchronous consumer and no delegated
    # workers. P4 waits for admitted calls, P3 verifies Stop and ring readback.
    coordinator = DemandCoordinator(binding["surface_id"], interests=["VERIFIED"],
                                    authorize=lambda row: row == agent, drain=lambda: True)
    stream = PerceptionStream(binding, source, ring_slots=2, interval_ns=20_000_000,
                              ttl_ns=3_000_000_000, max_frame_bytes=args.max_frame_bytes,
                              max_ring_bytes=rtring.ring_bytes(2, source.width, source.height))
    receipts = []
    with TemporaryDirectory() as scratch, Broker(scratch) as broker:
        broker.bind(session["session_id"], binding, lease_state="AGENT")
        owner = PerceptionLifecycle(
            broker, session["session_id"], binding, coordinator, agent, stream,
            qualified=qualified, runtime=runtime, drain_agent=lambda _: not runtime().exclusive_pids,
            on_transition=on_transition,
        )

        def call(op):
            receipt = owner.vp_perception_call(dict(op=op, session_id=session["session_id"], reason="private-fixture"))
            require(receipt["complete"], "incomplete transition")
            receipts.append(receipt)
            return receipt["fresh_observation_id"]

        try:
            old = call("wake")
            require(owner.read_frame(old), "empty initial frame")
            call("sleep")
            start = monotonic()
            kinds = ("acquisition", "analysis", "ocr", "inference", "decision")
            counts = dict.fromkeys(kinds, 0)
            with patch.object(stream._capture, "capture_once", wraps=stream._capture.capture_once) as capture:
                while True:
                    for kind in kinds:
                        try:
                            owner.run_agent(kind, old, lambda: counts.update({kind: counts[kind] + 1}))
                        except LifecycleError as exc:
                            require(exc.code == "NO_LEASE", "unexpected rest refusal")
                        else:
                            raise RuntimeError("agent work admitted after drain")
                    remaining = 60 - (monotonic() - start)
                    if remaining <= 0:
                        break
                    sleep(min(remaining, 1))
                require(not capture.call_count and not any(counts.values()), "work observed during rest")
            rest_seconds = monotonic() - start
            awake = call("wake")
            require(awake != old and owner.read_frame(awake), "stale wake observation")
            call("off")
            require(not runtime().exclusive_pids, "exclusive process retained on OFF")
            final = call("wake")
            require(final not in (old, awake) and owner.read_frame(final), "stale OFF wake")
            require(broker.lease(session["session_id"])["state"] == "AGENT", "session authority changed")
            call("off")  # Release only the rehearsal's agent reader.
            return dict(receipts=receipts, rest_seconds=rest_seconds, agent_work=counts,
                        private_display=display, operator_desktop="NOT-DONE")
        finally:
            stream.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("session", "binding", "agent-demand", "synthetic-profile"):
        parser.add_argument("--" + name, required=True, type=Path)
    for name in ("browser-pid", "xvfb-pid", "fixture-port", "max-frame-bytes"):
        parser.add_argument("--" + name, required=True, type=int)
    print(json.dumps(run(parser.parse_args()), sort_keys=True))
