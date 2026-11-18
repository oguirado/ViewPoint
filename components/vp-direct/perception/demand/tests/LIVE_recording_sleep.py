"""LIVE P2 only: record a private fixture across the agent's demand withdrawal.

Run with python3 -I -B inside the capped systemd scope. Supply the already
admitted fixture's broker binding, its agent and recording vp.demand/v1 records,
and the operator-provisioned synthetic profile path. The existing browser must
have been launched on the dedicated Xvfb with the fixture timer URL. Only
process arguments and X11 window properties are used for profile identity.
Frames are collected in a bounded in-memory recording. This probe exercises
agent sleep at the P2 boundary; P4 owns the separate lifecycle acknowledgement.
"""
import argparse
from collections import deque
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from time import monotonic, sleep

DIRECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(DIRECT))
from perception.demand import DemandCoordinator, DemandError
from perception.stream import PerceptionStream
from perception.stream.tests.LIVE_fixture_cycles import fixture_window, private_source, verify_fixture
from schemas.validation import validate_contract
from vadapt import rtring


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def check_window(display, args):
    xid = fixture_window(display)
    def xprop(*options):
        return subprocess.check_output(["xprop", "-display", display, *options], text=True, timeout=3)

    clients = re.findall(r"0x[0-9a-fA-F]+", xprop("-root", "_NET_CLIENT_LIST"))
    require([int(x, 16) for x in clients] == [int(xid, 16)], "expected only the fixture window")
    pid = xprop("-id", xid, "_NET_WM_PID").strip()
    require(pid == f"_NET_WM_PID(CARDINAL) = {args.browser_pid}", "fixture process changed")
    process = Path("/proc") / str(args.browser_pid)
    require((process / "exe").resolve(strict=True).as_posix() == "/opt/google/chrome/chrome",
            "expected the dedicated fixture Chrome process")
    argv = [os.fsdecode(word) for word in (process / "cmdline").read_bytes().split(b"\0") if word]
    profiles = [word for word in argv if word.startswith("--user-data-dir=")]
    require(args.synthetic_profile.is_absolute() and profiles == [f"--user-data-dir={args.synthetic_profile}"],
            "fixture profile mismatch")
    require(f"http://127.0.0.1:{args.fixture_port}/timer.html" in argv, "fixture launch URL mismatch")
    return xid


def run(args):
    verify_fixture(args.fixture_port)
    display, config = private_source(args.xvfb_pid)
    xid = check_window(display, args)
    binding, agent, recording = (json.loads(path.read_text()) for path in
                                 (args.binding, args.agent_demand, args.recording_demand))
    validate_contract("vp.surface_binding/v1", binding)
    for row in (agent, recording):
        validate_contract("vp.demand/v1", row)
        require(row["surface_id"] == binding["surface_id"] and row["interest"] == "VERIFIED",
                "expected verified demand for the bound surface")
    require(agent["demand_id"] != recording["demand_id"] and agent["consumer"] != recording["consumer"],
            "agent and recorder must be independent subscriptions")
    require(recording["purpose"] == "recording", "dedicated recording demand required")

    stream = PerceptionStream(binding, config, ring_slots=2, interval_ns=20_000_000,
                              ttl_ns=3_000_000_000, max_frame_bytes=args.max_frame_bytes,
                              max_ring_bytes=rtring.ring_bytes(2, config.width, config.height))
    token = None
    def drain():
        if token is None:
            status = stream.status()
            return status["state"] == "OFF" and status["reserved_bytes"] == 0
        stream.pause(token)
        status = stream.status()
        return status["state"] == "ASLEEP" and status["reserved_bytes"] > 0

    coordinator = DemandCoordinator(binding["surface_id"], interests=["VERIFIED"], drain=drain,
                                    authorize=lambda row: row in (agent, recording))
    frames = deque(maxlen=2)

    def deliver(handle, sequence, subscriber):
        nonlocal token
        require(check_window(display, args) == xid, "fixture window replaced")
        coordinator.publish(handle, sequence, "timer-frame")
        work, = coordinator.claim("VERIFIED", budget=1, timeout=0)
        require((work.consumer, work.lease) == (subscriber["consumer"], subscriber["lease"]),
                "delivery claimed under wrong consumer")
        observation = stream.wake() if token is None else stream.observe(token)
        token = observation.token
        pixels = stream.read(token, observation.observation_id)
        with coordinator.borrow(work, len(pixels)):
            require(check_window(display, args) == xid, "identity changed during capture")
            require(sha256(pixels).hexdigest() == observation.frame_sha256, "bad RAM frame")
            if subscriber == recording:
                frames.append((observation, pixels))
        coordinator.complete(work)

    try:
        with coordinator.register(recording) as recorder_handle:
            with coordinator.register(agent) as agent_handle:
                deliver(agent_handle, 1, agent)
                deliver(recorder_handle, 1, recording)
                before = frames[-1][0]
                running = stream.status()
            # The sleeping agent no longer owns a subscription. Continued
            # recording must use the existing producer, ring and generation.
            try:
                coordinator.publish(agent_handle, 2, "retired-agent")
            except DemandError as exc:
                require(str(exc) == "NO_LEASE", "unexpected agent refusal")
            else:
                raise RuntimeError("sleeping agent could publish")
            state = coordinator.snapshot()
            require([r["demand_id"] for r in state["demands"]] == [recording["demand_id"]],
                    "recording obligation lost at agent sleep")
            deadline = monotonic() + 3
            seq = 1
            while monotonic() < deadline:
                seq += 1
                deliver(recorder_handle, seq, recording)
                after = frames[-1][0]
                if after.frame_sha256 != before.frame_sha256:
                    break
                sleep(0.05)
            else:
                raise RuntimeError("no changed timer frame recorded after agent sleep")
            require(after.token == before.token and after.render_index > before.render_index
                    and after.observation_id != before.observation_id, "stale or restarted recording")
            require(stream.status()["ring_id"] == running["ring_id"], "recording ring replaced")
        coordinator.release()
        require(stream.status()["state"] == "ASLEEP", "last demand did not drain producer")
    finally:
        frames.clear()
        stream.close()
    return {"status": "PASS", "surface_id": binding["surface_id"], "recorded_frames": seq,
            "before_frame_sha256": before.frame_sha256, "after_frame_sha256": after.frame_sha256}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--agent-demand", required=True, type=Path)
    parser.add_argument("--recording-demand", required=True, type=Path)
    parser.add_argument("--synthetic-profile", required=True, type=Path)
    parser.add_argument("--browser-pid", required=True, type=int)
    parser.add_argument("--xvfb-pid", required=True, type=int)
    parser.add_argument("--fixture-port", required=True, type=int)
    parser.add_argument("--max-frame-bytes", required=True, type=int)
    print(json.dumps(run(parser.parse_args()), sort_keys=True))
