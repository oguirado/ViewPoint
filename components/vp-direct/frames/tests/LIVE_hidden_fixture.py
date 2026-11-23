#!/usr/bin/env python3
"""LIVE S5. Use an already open, dismissed timer fixture on display :0.

The orchestrator supplies the registry-correlated Chrome PID, XID and target,
and the qualified viewport's raster dimensions. This script checks the X11
workspace/geometry and CDP identity at capture time. It emits only frame evidence
and fixture tick values, and does not launch, move or close a window.

Run this check in the writer memory scope with python3 -I -B.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

DIRECT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(DIRECT), str(DIRECT.parent / "wender-agent")]

from frames.cdp import CDPRaster
from frames.source import FrameSource, FrameUnavailable, HostFrameState, inspect_png
from wender.perceive.cdp_port import CdpPort


def x11(program, *args):
    env = dict(os.environ, DISPLAY=":0", LC_ALL="C")
    return subprocess.run([program, *args], env=env, capture_output=True, text=True,
                          check=True, timeout=5).stdout


def workspace_number(property_output, name):
    value = re.search(rf"^{re.escape(name)} = ([0-9]+)$", property_output, re.MULTILINE)
    if value is None:
        raise FrameUnavailable(f"X11 property unavailable: {name}")
    return int(value[1])


def current_binding(args):
    native = x11("xprop", "-notype", "-id", args.xid, "_NET_WM_PID", "_NET_WM_DESKTOP")
    if workspace_number(native, "_NET_WM_PID") != args.pid:
        raise FrameUnavailable("XID PID does not match the operator's binding")
    window_workspace = workspace_number(native, "_NET_WM_DESKTOP")
    active_workspace = workspace_number(
        x11("xprop", "-notype", "-root", "_NET_CURRENT_DESKTOP"), "_NET_CURRENT_DESKTOP"
    )
    if window_workspace == active_workspace or window_workspace == 0xFFFFFFFF:
        raise FrameUnavailable("fixture window is not dismissed")
    stats = x11("xwininfo", "-id", args.xid)
    rect = []
    for label in ("Absolute upper-left X", "Absolute upper-left Y", "Width", "Height"):
        value = re.search(rf"^\s*{label}:\s+(-?[0-9]+)$", stats, re.MULTILINE)
        if value is None:
            raise FrameUnavailable("missing X11 geometry")
        rect.append(int(value[1]))
    return HostFrameState(
        surface_id=args.surface_id, incarnation=args.incarnation, authority_epoch=args.authority_epoch,
        geometry_epoch=args.geometry_epoch, window_witness=f"{args.xid}:{args.pid}:{window_workspace}",
        window_rect=tuple(rect), raster_size=(args.width, args.height),
        display=":0", hidden=True, backend="cdp", cdp_target_id=args.target_id,
    )


class TimerFixture:
    def __init__(self, port, fixture_url):
        self.port = port
        self.url = fixture_url
        self.tick = None

    def send(self, method, params, *, session_id):
        reply = self.port.send(method, params, session_id=session_id)
        if method == "Page.getFrameTree" and reply["frameTree"]["frame"].get("url") != self.url:
            raise FrameUnavailable("target is outside the qualified local fixture")
        if method == "Accessibility.getFullAXTree":
            names = {node.get("name", {}).get("value") for node in reply["nodes"]}
            ticks = {int(name[5:]) for name in names
                     if isinstance(name, str) and re.fullmatch(r"TICK_[0-9]+", name)}
            if "FIXTURE_TIMER_OK" not in names or len(ticks) != 1:
                raise FrameUnavailable("fixture marker or timer witness unavailable")
            self.tick = ticks.pop()
        return reply


@contextmanager
def fixture_demand(_demand):
    # This isolated fixture run uses a local registration. P2 wiring belongs
    # to the integrated run.
    yield


def probe(args):
    current_binding(args)
    port = CdpPort(port=args.port, host="127.0.0.1", timeout=5)
    session_id = None
    try:
        port.start()
        info = port.send("Target.getTargetInfo", {"targetId": args.target_id})["targetInfo"]
        if (info.get("targetId"), info.get("type"), info.get("url")) != (
            args.target_id, "page", args.fixture_url
        ):
            raise FrameUnavailable("CDP target does not identify the local timer fixture")
        session_id = port.send("Target.attachToTarget", {
            "targetId": args.target_id, "flatten": True,
        })["sessionId"]
        fixture = TimerFixture(port, args.fixture_url)
        source = FrameSource(lambda _: current_binding(args),
                             {"cdp": CDPRaster(fixture.send, session_id)}, fixture_demand)
        demand = {
            "schema": "vp.demand/v1", "demand_id": "s5-live", "surface_id": args.surface_id,
            "consumer": "s5-live", "interest": "VERIFIED", "purpose": "timer-capture",
            "lease": "s5-live-fixture", "credits": 1,
        }

        def capture():
            frame = source.capture(demand)
            try:
                return fixture.tick, inspect_png(source.read_ram(frame["ram_ref"]),
                                                 (args.width, args.height)), frame
            finally:
                source.release(frame["ram_ref"])

        # Keep only the evidence after each RAM borrow. Released handles are
        # omitted from the emitted live evidence.
        first_tick, first_pixels, first = capture()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            time.sleep(0.15)
            second_tick, second_pixels, second = capture()
            if second_tick > first_tick and second_pixels != first_pixels:
                for frame in (first, second):
                    frame.pop("ram_ref")
                return {"verdict": "PASS", "before_tick": first_tick, "after_tick": second_tick,
                        "before": first, "after": second}
        raise FrameUnavailable("fixture tick and decoded pixels did not both advance")
    finally:
        try:
            if session_id:
                port.send("Target.detachFromTarget", {"sessionId": session_id})
        finally:
            port.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("xid", "target-id", "fixture-url", "surface-id", "incarnation"):
        parser.add_argument("--" + option, required=True)
    for option in ("pid", "port", "width", "height", "authority-epoch", "geometry-epoch"):
        parser.add_argument("--" + option, type=int, required=True)
    args = parser.parse_args()
    url = urlsplit(args.fixture_url)
    if (
        url.scheme != "http" or not re.fullmatch(r"127\.0\.0\.1:[0-9]+", url.netloc)
        or not 0 < (url.port or 0) <= 65535 or url.path != "/timer.html" or url.query or url.fragment
        or not re.fullmatch(r"0x[0-9a-fA-F]+", args.xid)
        or not 0 < args.port <= 65535 or min(args.width, args.height, args.pid) < 1
        or min(args.authority_epoch, args.geometry_epoch) < 0
    ):
        parser.error("need the loopback timer fixture and a valid X11/CDP binding")
    print(json.dumps(probe(args), sort_keys=True))


if __name__ == "__main__":
    main()
