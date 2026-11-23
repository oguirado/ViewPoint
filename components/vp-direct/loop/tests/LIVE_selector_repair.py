"""LIVE, operator-owned private fixture after H1 and qualified semantic readback.

The local trusted driver supplies provision(url, case) -> (bound ReplayLoop,
failed_step_seq), break_selector(loop, seq, repairable=bool), and
dispatches(loop) -> sequence of original step numbers from the independent
transport. broker_events(loop) reads the actual broker records. Provision
a fresh admitted goal for each case and start the private app before this
script. The script serves only the repo fixture on a loopback port.
"""
import argparse
from copy import deepcopy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import runpy
import sys
from threading import Thread

VP = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(VP), str(VP.parents[1] / "agents")]
from fixtures.fixture_server import make_handler
from loop import ReplayLoop
from rewind.machine import Refused


def check(ok, message):
    if not ok:
        raise RuntimeError(message)


def run_case(driver, url, repairable):
    case = "repair" if repairable else "unrepairable"
    loop, seq = driver["provision"](url, case)
    check(isinstance(loop, ReplayLoop), "driver did not bind R1")
    start = loop.goal.replay()
    check(start["i"] == 0 and 1 < seq < start["n"], "need a verified prefix and a pending suffix")
    lane = start["loop"]
    original = loop.store.load_workflow(lane["site"], lane["workflow"], lane["revision"])["workflow"]
    for _ in range(seq - 1):
        check(loop.advance() == "VERIFIED", "prefix step failed")
    prior = list(driver["dispatches"](loop))
    driver["break_selector"](loop, seq, repairable=repairable)
    check(loop.advance() == "FAILED_STEP" and loop.goal.replay()["loop"]["failed_step_seq"] == seq,
          "broken selector did not produce the expected failed_step_seq")
    bound = start["n"] + 5 * (1 + start["T"] + start["B"] + start["D"])
    for _ in range(bound):
        outcome = loop.advance()
        if outcome in ("COMPLETE", "HARD_STOP_ESCALATED", "HUMAN_NEEDED"):
            break
    state = loop.goal.replay()
    calls = list(driver["dispatches"](loop))
    check(calls[:len(prior)] == prior and all(i >= seq for i in calls[len(prior):]),
          "transport replayed a verified prefix")
    if repairable:
        check(outcome == "COMPLETE" and state["i"] == start["n"] and state["t"] == start["t"] + 1,
              "repair did not resume through the suffix")
        revision = state["loop"]["revision"]
        candidate = loop.store.load_workflow(lane["site"], lane["workflow"], revision)["workflow"]
        expected = deepcopy(original)
        expected["steps"][seq - 1]["target"] = candidate["steps"][seq - 1]["target"]
        check(revision != lane["revision"] and candidate == expected and candidate["status"] == "draft",
              "candidate changed outside the failed selector")
    else:
        check(outcome == "HARD_STOP_ESCALATED" and any(
            e.get("rec") == "HUMAN_NEEDED" and e.get("goal_id") == state["goal_id"] and e.get("by") == "broker"
            for e in driver["broker_events"](loop)), "missing terminal state or broker handoff")
        try:
            loop.advance()
        except Refused as refusal:
            check(refusal.code == "NO_LEASE", "unexpected terminal refusal")
        else:
            raise RuntimeError("agent admitted after hard stop")
    return dict(case=case, outcome=outcome, goal_id=state["goal_id"],
                revision=state["loop"]["revision"], failed_step_seq=seq, attempts=state["t"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--driver", type=Path, required=True)
    driver = runpy.run_path(str(parser.parse_args().driver))
    with ThreadingHTTPServer(("127.0.0.1", 0), make_handler(VP / "fixtures/site")) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/chat.html"
            print(json.dumps([run_case(driver, url, repairable) for repairable in (True, False)]))
        finally:
            server.shutdown()
            thread.join()


if __name__ == "__main__":
    main()
