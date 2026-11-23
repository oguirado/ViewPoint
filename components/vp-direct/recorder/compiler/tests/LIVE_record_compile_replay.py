"""LIVE after H1 and exposure. Record, compile, preview and replay on a private fixture.

Run this only from the operator's approved private-display harness. The trusted
integration driver exposes bind(url) and record_demo(url, recorder) as in
U2a's LIVE_record_fixture.py, review(url, timeline) returning (review, owner),
and replay(url, compilation, dry_run=bool). The replay driver stages the
compilation in its authorized workspace and returns (vp.control.reply/v1,
mw.run.v1 or None). Preview must return no run receipt. The replay driver owns
GUI custody and selects the exact compiled draft by digest.
"""

import argparse
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import runpy
import sys
from threading import Thread


VP = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(VP))

from fixtures.fixture_server import make_handler
from recorder.compiler import _validate_memoryweb, compile_recording
from recorder.timeline import TimelineRecorder
from schemas.validation import validate_contract


def check_reply(reply):
    validate_contract("vp.control.reply/v1", reply)
    if reply["op"] != "vp_replay_workflow" or reply["ok"] is not True or reply["code"] != "OK":
        raise SystemExit("LIVE replay refused")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--driver", required=True, type=Path)
    args = parser.parse_args()
    driver = runpy.run_path(str(args.driver))
    handler = make_handler(VP / "fixtures" / "site")
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        worker = Thread(target=server.serve_forever)
        worker.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/chat.html"
            recorder, demand = driver["bind"](url)
            if not isinstance(recorder, TimelineRecorder):
                raise SystemExit("LIVE driver must bind the U2a recorder")
            with recorder.recording(demand):
                driver["record_demo"](url, recorder)
                timeline = recorder.snapshot()
            if not any(row["source"] == "demo" and "action" in row for row in timeline["events"]):
                raise SystemExit("LIVE requires a recorded demo action")
            review, owner = driver["review"](url, timeline)
            compilation = compile_recording(timeline, review, owner)
            digests = compilation.digests()
            reply, run = driver["replay"](url, compilation, dry_run=True)
            check_reply(reply)
            if run is not None:
                raise SystemExit("LIVE preview produced a run receipt")
            reply, run = driver["replay"](url, compilation, dry_run=False)
            check_reply(reply)
            _validate_memoryweb("run", run)
            workflow = json.loads(compilation.workflow)
            if (run["workflow_sha256"] != digests["workflow_sha256"] or
                    run["workflow_id"] != workflow["id"] or run["site_id"] != workflow["site_id"] or
                    run["mode"] != "replay" or run["rc"] != 0 or run["verdict_code"] != "VERIFIED" or
                    run["steps_total"] != len(workflow["steps"]) or
                    run["steps_verified"] != len(workflow["steps"])):
                raise SystemExit("LIVE receipt did not verify the compiled workflow")
        finally:
            server.shutdown()
            worker.join()
    print(json.dumps({"verdict": "PASS", **digests, "receipt": run}, sort_keys=True))


if __name__ == "__main__":
    main()
