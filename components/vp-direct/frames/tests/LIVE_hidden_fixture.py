#!/usr/bin/env python3
"""LIVE S5+. Verify a dismissed timer and a protected login on a private display.

Run with python3 -I -B under the writer's capped scope. The operator-supplied
driver must verify the existing private display via verify_private_display.
Its open_frame_fixture(url, display) context yields (current, read_metadata,
acquire) for the positively identified, unsigned-in fixture window. current
returns a HostFrameState, and acquire returns a bound Raster. read_metadata
implements the A2 full-footprint and monotonic revision contract. The fixture
driver owns display, browser and profile custody.
"""

import argparse
from hashlib import sha256
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
import runpy
import sys
from threading import Thread
import time

DIRECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(DIRECT))

from fixtures.fixture_server import make_handler
from frames.source import FrameSource, FrameUnavailable, HostFrameState, inspect_png
from perception.demand import DemandCoordinator
from privacy import PrivacySentinel
from recorder.timeline import TimelinePosition, TimelineRecorder
from schemas.validation import canonical_bytes


def check_timer(source, demand):
    def capture():
        frame = source.capture(demand)
        ram_ref = frame.pop("ram_ref")
        try:
            size = frame["width"], frame["height"]
            return frame, inspect_png(source.read_ram(ram_ref), size)
        finally:
            source.release(ram_ref)

    before, before_pixels = capture()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        time.sleep(0.15)
        after, after_pixels = capture()
        if (after_pixels != before_pixels and
                after["witness"]["ax_sha256"] != before["witness"]["ax_sha256"] and
                after["captured_mono_ns"] > before["captured_mono_ns"]):
            return {"verdict": "PASS", "before": before, "after": after}
    raise FrameUnavailable("timer did not advance in both pixels and the AX witness")


def check_fixture(fixture, display, current, read_metadata, acquire):
    initial = current()
    if (not isinstance(initial, HostFrameState) or initial.display != display or
            (fixture == "timer" and (not initial.hidden or initial.backend != "cdp"))):
        raise FrameUnavailable("the fixture lacks the requested private capture binding")

    def checked_binding(surface_id):
        state = current()
        if not isinstance(state, HostFrameState) or state != initial or state.surface_id != surface_id:
            raise FrameUnavailable("private fixture binding changed")
        return state

    demand = {
        "schema": "vp.demand/v1", "surface_id": initial.surface_id, "demand_id": "s5-live",
        "consumer": "s5-live", "interest": "VERIFIED", "purpose": "fixture-capture",
        "lease": "s5-live-fixture", "credits": 1,
    }
    recording = dict(demand, demand_id="s5-live-recording", consumer="fixture-recorder",
                     purpose="recording")
    coordinator = DemandCoordinator(initial.surface_id, interests={"VERIFIED"},
                                    authorize=lambda row: row in (demand, recording),
                                    drain=lambda: True)
    capsule_id = "s5-fixture"
    recorder = TimelineRecorder(surface_id=initial.surface_id, capsule_id=capsule_id,
                                incarnation=initial.incarnation, register=coordinator.register,
                                verify_ram_frame=lambda *_: False)
    notices = []
    calls = 0

    def capture(state):
        nonlocal calls
        calls += 1
        return acquire(state)

    def record_gap(active, when):
        position = TimelinePosition(when, capsule_id, initial.incarnation)
        recorder.set_gap(position, "privacy", active=active)

    binding = initial.privacy_binding()
    sentinel = PrivacySentinel(binding, read_metadata, record_gap=record_gap,
                               request_human=notices.append)
    source = FrameSource(checked_binding, {initial.backend: capture}, coordinator.register,
                         privacy_for=lambda state: sentinel if state.privacy_binding() == binding else None)
    with recorder.recording(recording):
        if fixture == "timer":
            result = check_timer(source, demand)
            if notices or recorder.snapshot()["events"]:
                raise FrameUnavailable("timer unexpectedly entered a privacy gap")
            return result

        try:
            source.capture(demand)
        except FrameUnavailable as exc:
            code = exc.code
        else:
            raise FrameUnavailable("login fixture published a frame")
        timeline = recorder.snapshot()
        rows = timeline["events"]
        if (code != "HUMAN_NEEDED" or notices != ["login"] or calls or
                len(rows) != 1 or rows[0].get("op") != "privacy_gap_start" or
                rows[0].get("gap") is not True or "frame" in rows[0]):
            raise FrameUnavailable("login fixture lacked zero-capture or privacy-gap evidence")
        return {"verdict": "PASS", "code": code, "capture_calls": calls, "frames": 0,
                "timeline_sha256": sha256(canonical_bytes(timeline)).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--driver", required=True, type=Path)
    parser.add_argument("--display", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"xpra:[1-9][0-9]*", args.display):
        parser.error("an authorized private Xpra display is required")
    driver = runpy.run_path(str(args.driver))
    if driver["verify_private_display"](args.display) is not True:
        raise SystemExit("LIVE S5+ needs a positively identified private display")

    handler = make_handler(DIRECT / "fixtures" / "site")
    results = {}
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for fixture in ("timer", "login"):
                url = f"http://127.0.0.1:{server.server_port}/{fixture}.html"
                with driver["open_frame_fixture"](url, args.display) as ports:
                    results[fixture] = check_fixture(fixture, args.display, *ports)
        finally:
            server.shutdown()
            thread.join()
    print(json.dumps(results, sort_keys=True))


if __name__ == "__main__":
    main()
