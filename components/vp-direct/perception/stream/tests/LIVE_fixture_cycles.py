"""LIVE only. Use an already-open fixture timer page on a dedicated private Xvfb.

Supply the validated broker binding JSON, the operator's Xvfb PID (started
with :N, -nocursor, -screen 0 WxHxD and -fbdir), and the fixture server port.
The private window manager must expose _NET_ACTIVE_WINDOW. Run inside the
writer's capped scope. This probe only inspects the existing display.
"""
import argparse
from hashlib import sha256
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import subprocess
import sys

DIRECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(DIRECT))
from perception.stream import PerceptionStream, StreamError
from vadapt import rtring, x11_ramcap


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def private_source(pid):
    process = Path("/proc") / str(pid)
    require((process / "exe").resolve(strict=True).name == "Xvfb", "expected Xvfb PID")
    args = [os.fsdecode(arg) for arg in (process / "cmdline").read_bytes().split(b"\0") if arg]
    displays = [arg for arg in args if re.fullmatch(r":[1-9][0-9]*", arg)]
    require(len(displays) == 1 and ":0" not in args and "-nocursor" in args,
            "expected a dedicated private display and -nocursor")
    require(args.count("-fbdir") == 1 and args.count("-screen") == 1, "ambiguous Xvfb configuration")
    idx = args.index("-screen")
    require(args[idx + 1] == "0", "expected screen zero")
    spec = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)x[1-9][0-9]*", args[idx + 2])
    require(spec, "unknown Xvfb geometry")
    config = x11_ramcap.SourceConfig(args[args.index("-fbdir") + 1], "Xvfb_screen0",
                                      int(spec[1]), int(spec[2]), f"Xvfb process {pid} -nocursor")
    return displays[0], config


def fixture_window(display):
    def prop(*args):
        return subprocess.check_output(["xprop", "-display", display, *args], timeout=3, text=True)
    active = re.search(r"window id # (0x[0-9a-fA-F]+)", prop("-root", "_NET_ACTIVE_WINDOW"))
    require(active and int(active[1], 16), "fixture window unavailable")
    require("Fixture timer page (DOM changes)" in prop("-id", active[1], "_NET_WM_NAME"),
            "active window does not show the fixture timer title")
    return active[1]


def verify_fixture(port):
    document = (DIRECT / "fixtures/site/timer.html").read_bytes()
    conn = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request("GET", "/timer.html")
        response = conn.getresponse()
        require(response.status == 200 and response.getheader("X-Fixture-Server") == "vp-direct"
                and response.read(len(document) + 1) == document, "fixture server or page mismatch")
    finally:
        conn.close()


def run(args):
    verify_fixture(args.fixture_port)
    display, config = private_source(args.xvfb_pid)
    xid = fixture_window(display)
    binding = json.loads(args.binding.read_text())
    stream = PerceptionStream(binding, config, ring_slots=2, interval_ns=20_000_000,
                              ttl_ns=3_000_000_000, max_frame_bytes=args.max_frame_bytes,
                              max_ring_bytes=rtring.ring_bytes(2, config.width, config.height))
    try:
        obs = stream.wake()
        tokens, observations, digests = {obs.token}, {obs.observation_id}, {obs.frame_sha256}
        for _ in range(100):
            require(fixture_window(display) == xid, "fixture window changed")
            before = stream.status()
            stream.pause(obs.token)
            paused = stream.status()
            require(paused["state"] == "ASLEEP" and paused["ring_id"] == before["ring_id"]
                    and paused["reserved_bytes"] == before["reserved_bytes"] > 0, "pause lost ring")
            new = stream.wake()
            require(new.stream_generation == obs.stream_generation + 1
                    and new.token not in tokens and new.observation_id not in observations,
                    "repeated token, generation or observation")
            require(stream.status()["last_scrubbed_ring"] == before["ring_id"], "missing scrub witness")
            try:
                stream.read(obs.token, new.observation_id)
            except StreamError:
                pass
            else:
                raise RuntimeError("retired token admitted")
            require(sha256(stream.read(new.token, new.observation_id)).hexdigest() == new.frame_sha256,
                    "observation digest mismatch")
            tokens.add(new.token)
            observations.add(new.observation_id)
            digests.add(new.frame_sha256)
            obs = new
        require(fixture_window(display) == xid and len(digests) > 1, "no timer frame change witnessed")
    finally:
        stream.close()
    require(stream.status()["reserved_bytes"] == 0, "final ring still charged")
    print(json.dumps(dict(status="PASS", cycles=100, observations=len(observations),
                          distinct_frame_digests=len(digests))))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--xvfb-pid", type=int, required=True)
    parser.add_argument("--fixture-port", type=int, required=True)
    parser.add_argument("--max-frame-bytes", type=int, required=True)
    run(parser.parse_args())
