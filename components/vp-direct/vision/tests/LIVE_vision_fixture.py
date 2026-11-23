"""LIVE R2. Exercise an existing synthetic scene on a private display.

Run in the writer memory scope with python3 -I -B. Pass the orchestrator's
trusted --driver file and --display. The driver verifies the display and owns
the scene. Its open_vision_fixture(display) context manager supplies
(mode, lifecycle, change_scene) with the real local providers and no actuation
port. Provision OCR for the fixture's Continue button. change_scene() makes a
visible, deterministic change while keeping the button available.
"""

import argparse
import json
from pathlib import Path
import re
import runpy
import sys

DIRECT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(DIRECT), str(DIRECT.parents[1] / "agents")]

from vision import VisionError


def require(condition, message):
    if not condition:
        raise SystemExit("LIVE R2: " + message)


def check(mode, lifecycle, change_scene):
    def control(op, request_id, args):
        tool = "vp_vision_" + op
        result = mode.execute(dict(
            schema="vp.control.req/v1", request_id=request_id, op=tool, args=args,
            context=dict(caller="agent", agent_instance=lifecycle.binding["owner"]),
            dry_run=False,
        ))
        require(result.ok, result.code)
        return result.result

    def start(request_id):
        return control("start", request_id, dict(session_id=lifecycle.session_id,
                                                 goal="click Continue", steps=4))

    status = control("status", "initial", {})
    require(status["perception_state"] in ("OFF", "ASLEEP"), "initial state is not resting")
    old = start("start")["observation_id"]
    try:
        first = mode.analyze(old)
        require(first["changed"] is None, "initial frame reused a baseline")
        change_scene()
        changed = mode.analyze(old)
        require(changed["changed"] is True and changed["frame_sha256"] != first["frame_sha256"],
                "fixture change did not produce verified new pixels")
        sleep = lifecycle.vp_perception_call(dict(op="sleep", session_id=lifecycle.session_id,
                                                  reason="synthetic scene"))
        require(sleep["complete"] and sleep["to"] == "ASLEEP", "drain was not acknowledged")
        fresh = start("resume")["observation_id"]
        require(fresh and fresh != old, "wake reused an observation")
        try:
            mode.analyze(old)
        except VisionError as exc:
            require(exc.code == "STALE_FRAME", exc.code)
        else:
            raise SystemExit("LIVE R2: retired observation was accepted")
        resumed = mode.analyze(fresh)
        require(resumed["changed"] is None, "comparison history survived sleep")
    finally:
        stopped = control("stop", "stop", {})
        require(not stopped["running"], "vision remains running")
    return dict(verdict="PASS", first_sha256=first["frame_sha256"],
                changed_sha256=changed["frame_sha256"], resumed_sha256=resumed["frame_sha256"],
                transport=resumed["transport"], retired_observation="STALE_FRAME")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--driver", required=True, type=Path)
    parser.add_argument("--display", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"xpra:[1-9][0-9]*", args.display):
        parser.error("an authorized private xpra display is required")
    driver = runpy.run_path(str(args.driver))
    require(driver["verify_private_display"](args.display) is True, "private display identity unverified")
    with driver["open_vision_fixture"](args.display) as ports:
        receipt = check(*ports)
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
