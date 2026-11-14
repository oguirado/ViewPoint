"""Tests of the readiness replay audit. Run from product/ref_auction:
    python3 -m unittest judge.test_replay -v
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from . import judge as J
from . import replay as R
from .test_judge import CLAIMS, script, gui, close, rollback


def auction(turn, head, rows, winner, reason="auction"):
    w = next((r for r in rows if r["agent_id"] == winner), None)
    return {"v": 1, "rec": "auction", "turn": turn, "tick": turn, "policy": "vickrey_aging_v1", "head_frame": head,
            "eligible": rows, "winner": winner, "event_id": w and w["event_id"], "reason": reason, "price": 0}


def row(intent, readiness):
    return {"agent_id": intent["agent_id"], "event_id": intent["event_id"], "bid": 0, "wait": 0, "readiness": readiness}


def honest_log():
    """Two auctions produced by a scheduler that asks the judge at pick time, with a rollback in between."""
    j = J.Judge(CLAIMS)
    log = []

    def add(r):
        log.append(r)
        j.observe(r)

    m, p = script("modeler-agent", 1, base=0), script("props-agent", 1, base=0)
    add(m), add(p)
    add(auction(1, 0, [row(m, j.scorer(m, 0)), row(p, j.scorer(p, 0))], "modeler-agent"))
    add(close("modeler-agent", 1, "JOB_ERROR", verify_frame_ref=1))
    add(rollback("modeler-agent", 1))
    m2 = script("modeler-agent", 2, base=1)
    add(m2)
    add(auction(2, 1, [row(m2, j.scorer(m2, 1)), row(p, j.scorer(p, 1))], "props-agent"))
    add(close("props-agent", 1, "STATE_VERIFIED", verify_frame_ref=2))
    return log


def codes(log, **kw):
    return [v["code"] for v in R.audit(log, CLAIMS, **kw)[0]]


class Replay(unittest.TestCase):
    def test_honest_log_is_clean(self):
        log = honest_log()
        bad, stats = R.audit(log, CLAIMS)
        self.assertEqual(bad, [])
        self.assertEqual(stats, {"auctions": 2, "rows": 4, "holds": 0})
        a2 = [r for r in log if r["rec"] == "auction"][1]["eligible"]
        self.assertLess(a2[0]["readiness"], a2[1]["readiness"], "the rolled-back agent is now behind")

    def test_scheduler_that_ignores_the_judge_is_flagged(self):
        log = honest_log()
        for r in log:
            if r["rec"] == "auction":
                for e in r["eligible"]:
                    e["readiness"] = 1000  # a stand-in scorer instead of the judge
        self.assertEqual(set(codes(log)), {"READINESS_MISMATCH"})

    def test_stale_memory_is_flagged(self):
        log = honest_log()
        fresh = J.Judge(CLAIMS)
        a2 = [r for r in log if r["rec"] == "auction"][1]
        m2 = next(r for r in log if r.get("event_id") == "modeler-agent-2" and r["rec"] == "enqueue")
        a2["eligible"][0]["readiness"] = fresh.scorer(m2, 1)  # scored without the rollback
        bad = R.audit(log, CLAIMS)[0]
        self.assertEqual([(v["code"], v["agent_id"], v["turn"]) for v in bad], [("READINESS_MISMATCH", "modeler-agent", 2)])

    def test_all_zero_readiness_swallowed_exception_is_flagged(self):
        # the failure seen in the cell: the scorer raised, the scheduler recorded 0 for every row
        log = honest_log()
        a1 = next(r for r in log if r["rec"] == "auction")
        for e in a1["eligible"]:
            e["readiness"] = 0
        a1["reason"] = "all_unready"
        self.assertEqual(codes(log), ["READINESS_MISMATCH", "READINESS_MISMATCH"])

    def test_gated_winner(self):
        cam = gui("camera-agent", 1, base=0)
        m = script("modeler-agent", 1, base=3)
        rows = [row(m, J.Judge(CLAIMS).scorer(m, 3)), row(cam, 0)]
        self.assertEqual(codes([m, cam, auction(1, 3, rows, "camera-agent")]), ["GATED_WINNER"])
        for reason in R.UNREADY_OK:
            self.assertEqual(codes([m, cam, auction(1, 3, rows, "camera-agent", reason)]), [], reason)
        self.assertEqual(codes([m, cam, auction(1, 3, rows, "modeler-agent")]), [])

    def test_stale_gui_row_accepts_only_zero_or_the_rebase_score(self):
        cam = gui("camera-agent", 1, base=2)
        rebased = J.score_intent(cam, 3, J.History(), CLAIMS, identical_pixels=True)["score"]
        for val, want in ((0, []), (rebased, []), (rebased + 1, ["READINESS_MISMATCH"]), (1000, ["READINESS_MISMATCH"])):
            self.assertEqual(codes([cam, auction(1, 3, [row(cam, val)], None, "none")]), want, val)

    def test_unknown_intent_and_spoofed_owner(self):
        m = script("modeler-agent", 1, base=0)
        self.assertEqual(codes([auction(1, 0, [row(m, 800)], "modeler-agent")]), ["UNKNOWN_INTENT"])
        spoof = dict(row(m, 800), agent_id="props-agent")
        self.assertEqual(codes([m, auction(1, 0, [spoof], "props-agent")]), ["UNKNOWN_INTENT"])

    def test_missing_head_frame_and_hold(self):
        m = script("modeler-agent", 1, base=0)
        a = auction(1, None, [row(m, 800)], "modeler-agent")
        self.assertEqual(codes([m, a]), ["NO_HEAD_FRAME"])
        hold = dict(auction(1, None, [], None, "hold"))
        bad, stats = R.audit([m, hold], CLAIMS)
        self.assertEqual((bad, stats["holds"], stats["auctions"]), ([], 1, 0))

    def test_llm_tolerance_is_bounded_and_never_lifts_a_gate(self):
        m = script("modeler-agent", 1, base=0)
        base = J.Judge(CLAIMS).scorer(m, 0)
        log = [m, auction(1, 0, [row(m, base - 60)], "modeler-agent", "sole")]
        self.assertEqual(codes(log), ["READINESS_MISMATCH"])
        self.assertEqual(codes(log, llm_tolerance=J.LLM_CLAMP), [])
        log = [m, auction(1, 0, [row(m, base - 101)], "modeler-agent", "sole")]
        self.assertEqual(codes(log, llm_tolerance=J.LLM_CLAMP), ["READINESS_MISMATCH"])
        thief = script("props-agent", 1, base=0, write_set=["col:CAM"])
        log = [thief, auction(1, 0, [row(thief, 50)], None, "none")]
        self.assertEqual(codes(log, llm_tolerance=J.LLM_CLAMP), ["READINESS_MISMATCH"])

    def test_records_after_the_auction_do_not_leak_into_it(self):
        # the close that follows a pick must not be visible to that pick's recomputation
        log = honest_log()
        i = next(k for k, r in enumerate(log) if r["rec"] == "auction")
        moved = log[:i] + [log[i + 1], log[i + 2], log[i]] + log[i + 3:]  # auction recorded after its own close
        self.assertIn("READINESS_MISMATCH", codes(moved))

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "claims.json"), "w") as f:
                json.dump(CLAIMS, f)
            log = honest_log()

            def run():
                with open(os.path.join(d, "events.jsonl"), "w") as f:
                    f.write("".join(json.dumps(r) + "\n" for r in log) + '{"rec": "clo')
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = R.main(["--rt", d])
                return rc, [json.loads(x) for x in buf.getvalue().splitlines()]
            rc, out = run()
            self.assertEqual((rc, out[-1]["violations"], out[-1]["auctions"]), (0, 0, 2))
            log[2]["eligible"][0]["readiness"] = 1
            rc, out = run()
            self.assertEqual((rc, out[0]["code"], out[0]["line"]), (1, "READINESS_MISMATCH", 3))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(R.main(["--rt", os.path.join(d, "absent")]), 2)


if __name__ == "__main__":
    unittest.main()
