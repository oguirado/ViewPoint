"""LIVE only. Use the authorized timer fixture already open in private Xvfb.

Supply the broker binding, dedicated Xvfb PID, loopback fixture port, and
synthetic Chrome profile identity. Run in the writer's capped systemd scope.
This check does not launch a browser or open the profile directory.
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

DIRECT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(DIRECT), str(DIRECT / "perception/stream/tests")]

from LIVE_fixture_cycles import fixture_window, private_source, require, verify_fixture
from perception.pool import PerceptionPool, PoolError
from perception.stream import PerceptionStream
from vadapt.rtring import ring_bytes


def verify_window(display, xid, pid, profile):
    require(pid > 0 and profile.is_absolute(), "explicit private browser identity required")
    require(fixture_window(display) == xid, "fixture window changed")
    output = subprocess.check_output(
        ["xprop", "-display", display, "-id", xid, "_NET_WM_PID"], text=True, timeout=3,
    )
    found = re.fullmatch(r"_NET_WM_PID\(CARDINAL\) = ([1-9][0-9]*)\s*", output)
    require(found and int(found[1]) == pid, "fixture window PID mismatch")
    process = Path("/proc") / str(pid)
    require((process / "exe").resolve(strict=True) == Path("/opt/google/chrome/chrome"),
            "unexpected private fixture browser")
    with (process / "cmdline").open("rb") as cmdline:
        argv = cmdline.read(65537)
    require(len(argv) <= 65536, "browser identity exceeds argument limit")
    profiles = [arg for arg in argv.split(b"\0") if arg.startswith(b"--user-data-dir=")]
    require(profiles == [b"--user-data-dir=" + os.fsencode(profile)], "fixture profile mismatch")


def run(args):
    verify_fixture(args.fixture_port)
    require(args.xvfb_pid > 0, "explicit private Xvfb PID required")
    display, source = private_source(args.xvfb_pid)
    xid = fixture_window(display)
    binding = json.loads(args.binding.read_text())
    verify_window(display, xid, args.browser_pid, args.profile_dir)
    frame_bytes = source.width * source.height * 3
    require(args.max_xwd_bytes >= frame_bytes, "XWD limit cannot hold the RGB24 frame")
    pool = PerceptionPool(
        DIRECT.parent / "vision-engine", max_sessions=2, max_frame_bytes=frame_bytes,
        max_reserved_bytes=3 * frame_bytes, transport="vserve", timeout=5,
    )

    with TemporaryDirectory(prefix="vp-live-p5-", dir="/dev/shm") as workspace:
        stream = PerceptionStream(
            binding, source, ring_slots=2, interval_ns=20_000_000, ttl_ns=5_000_000_000,
            max_frame_bytes=args.max_xwd_bytes,
            max_ring_bytes=ring_bytes(2, source.width, source.height),
            workspace_root=Path(workspace) / "rings",
        )

        def sample(session, observation):
            verify_window(display, xid, args.browser_pid, args.profile_dir)
            result = session.describe_observation(stream, observation, width=source.width,
                                                  height=source.height)
            require(result.diagnostics.last_transport == "vserve"
                    and result.diagnostics.last_fallback_reason is None, "unexpected transport")
            require(result.description.frame_sha256 == observation.frame_sha256,
                    "frame digest mismatch")
            return result.description

        try:
            left = pool.open_session("left", binding=binding, trust_domain="private-fixture")
            right = pool.open_session("right", binding=binding, trust_domain="private-fixture")
            require(pool.status().partitions == 1, "expected one shared worker")
            observation = stream.wake()
            require(sample(left, observation).changed is None, "left inherited history")
            require(sample(right, observation).changed is None, "right inherited history")
            left.close()
            try:
                sample(left, observation)
            except PoolError as exc:
                require(str(exc) == "session is retired", "unexpected retirement refusal")
            else:
                raise RuntimeError("retired client accepted")
            require(pool.status().active_sessions == 1, "peer session lost")
            fresh = stream.observe(observation.token)
            require(sample(right, fresh).changed is not None, "peer history lost")
            right.close()
            require(pool.status().partitions == pool.status().reserved_bytes == 0,
                    "pool still holds resources")
        finally:
            try:
                pool.close()
            finally:
                stream.close()
    print(json.dumps({"status": "PASS", "transport": "vserve", "peer_history_retained": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--xvfb-pid", required=True, type=int)
    parser.add_argument("--fixture-port", required=True, type=int)
    parser.add_argument("--browser-pid", required=True, type=int)
    parser.add_argument("--profile-dir", required=True, type=Path)
    parser.add_argument("--max-xwd-bytes", required=True, type=int)
    run(parser.parse_args())
