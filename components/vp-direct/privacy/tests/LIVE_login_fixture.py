"""LIVE A2. Verify the loopback login fixture on an authorized private display.

Use the writer's capped systemd scope with python3 -I -B and pass --driver FILE
and --display DISPLAY. The trusted operator driver verifies the existing
private display with verify_private_display(display). Its context manager
open_login_fixture(url, display) yields (binding, read_metadata, acquire)
for the witnessed fixture window with no signed-in profile. The driver owns
GUI custody. Metadata and capture ports have the PrivacySentinel contract.
"""

import argparse
from contextlib import nullcontext
from hashlib import sha256
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
import runpy
import sys
from threading import Thread


VP_DIRECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(VP_DIRECT))
from fixtures.fixture_server import make_handler
from privacy import PrivacyBlocked, PrivacySentinel
from recorder.timeline import TimelinePosition, TimelineRecorder
from schemas.validation import canonical_bytes


def check(binding, read_metadata, acquire):
    captures = 0
    notices = []

    def capture(metadata):
        nonlocal captures
        captures += 1
        return acquire(metadata)

    def unexpected_publication(_):
        raise SystemExit("LIVE privacy check: protected observation was published")

    recorder = TimelineRecorder(surface_id=binding.surface_id, capsule_id="fixture-capsule",
                                incarnation=binding.incarnation, register=lambda _: nullcontext(),
                                verify_ram_frame=lambda *_: False)
    demand = {
        "schema": "vp.demand/v1", "surface_id": binding.surface_id, "demand_id": "login-fixture",
        "consumer": "fixture-recorder", "interest": "VERIFIED", "purpose": "recording",
        "lease": "fixture-lease", "credits": 1,
    }
    sentinel = PrivacySentinel(
        binding, read_metadata, request_human=notices.append,
        record_gap=lambda active, when: recorder.set_gap(
            TimelinePosition(when, "fixture-capsule", binding.incarnation), "privacy", active=active),
    )
    with recorder.recording(demand):
        try:
            sentinel.observe(capture, unexpected_publication)
        except PrivacyBlocked as exc:
            code = exc.code
        else:
            raise SystemExit("LIVE privacy check: fixture login was not refused")
        timeline = recorder.snapshot()

    rows = timeline["events"]
    if (code != "HUMAN_NEEDED" or notices != ["login"] or captures
            or len(rows) != 1 or rows[0].get("gap") is not True
            or rows[0].get("op") != "privacy_gap_start" or "frame" in rows[0]):
        raise SystemExit("LIVE privacy check: missing login handoff, privacy gap, or zero-capture proof")
    return {
        "verdict": "PASS", "code": code, "capture_calls": captures, "frames": 0,
        "timeline_sha256": sha256(canonical_bytes(timeline)).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--driver", required=True, type=Path)
    parser.add_argument("--display", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"(?:xpra)?:[1-9][0-9]*", args.display):
        parser.error("an authorized non-operator private display is required")
    driver = runpy.run_path(str(args.driver))
    if driver["verify_private_display"](args.display) is not True:
        raise SystemExit("LIVE privacy check: private display identity unverified")

    handler = make_handler(VP_DIRECT / "fixtures" / "site")
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/login.html"
            with driver["open_login_fixture"](url, args.display) as ports:
                receipt = check(*ports)
        finally:
            server.shutdown()
            thread.join()
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
