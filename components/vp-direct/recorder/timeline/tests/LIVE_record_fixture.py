"""LIVE after H1. Record a short demo on the loopback fixture site.

Pass --driver FILE with the orchestrator's trusted integration driver. The
driver exposes bind(url) -> (TimelineRecorder, demand) and record_demo(url,
recorder). The latter drives the fixture through the approved H1 path, joins
actual DemoEvent and RAM capture feeds, and returns after delivery drains.
The driver owns GUI custody. The check serves only the local fixture site and
reports a verdict and timeline digest.

Run in the capped writer scope with python3 -I -B from the post-H1 harness.
"""

import argparse
from hashlib import sha256
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import runpy
import sys
from threading import Thread


VP_DIRECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(VP_DIRECT))
from fixtures.fixture_server import make_handler
from recorder.timeline import TimelineRecorder
from schemas.validation import canonical_bytes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--driver", required=True, type=Path)
    args = parser.parse_args()
    driver = runpy.run_path(str(args.driver))
    handler = make_handler(VP_DIRECT / "fixtures" / "site")
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/chat.html"
            recorder, demand = driver["bind"](url)
            if not isinstance(recorder, TimelineRecorder):
                raise SystemExit("LIVE driver must bind the U2a recorder")
            with recorder.recording(demand):
                driver["record_demo"](url, recorder)
                timeline = recorder.snapshot()
                rows = timeline["events"]
                if not any(row["source"] == "demo" and "action" in row for row in rows):
                    raise SystemExit("LIVE fixture lacks a recorded demo action")
                if not any("frame" in row for row in rows):
                    raise SystemExit("LIVE fixture lacks a verified RAM frame")
        finally:
            server.shutdown()
            worker.join()
    print(json.dumps({"verdict": "PASS", "timeline_sha256": sha256(canonical_bytes(timeline)).hexdigest()}))


if __name__ == "__main__":
    main()
