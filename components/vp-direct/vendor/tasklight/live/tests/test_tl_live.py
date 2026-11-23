"""Offline tests of the tl-live fold and projection (no Blender, no rooms). Track P (claude-fable-5-2).
Negative controls: test_nc_* go red if the named guard is removed (each states the guard)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import microtasks as M   # noqa: E402
import tl_project as TP  # noqa: E402


class Rec:
    def __init__(self):
        self.calls = []

    def post(self, agent, act, text, ref=None, key=None):
        if act == "refute" and not ref:
            raise ValueError("refute needs a ref")
        self.calls.append(("osb", agent, act, ref, text))
        return True

    def call(self, tool, agent, key=None, **args):
        self.calls.append(("tl", agent, tool, args))
        return True


def board(digest_proof=True):
    a = M.Task("modeler-agent", 1, "Model")
    a1 = a.add("body", "body exists", {"lane": "script", "write_set": ["col:CHAR_mesh"], "script": "x"})
    b = M.Task("props-agent", 1, "Table")
    b.add("table wrong", "under body", {"lane": "script", "write_set": ["col:PROPS_set"], "script": "bad"},
          after=[a1.microtask_id], plans=[{"text": "table right", "accept": "under body",
                                           "intent": {"lane": "script", "write_set": ["col:PROPS_set"], "script": "good"}}])
    return M.Board([a, b], require_digest_proof=digest_proof)


def enq(it):
    return dict(it, seq=1)


def run(bd, script, proj=None):
    """Drive the board like the live loop: intents -> enqueue records -> scripted broker records."""
    log = []
    for step in script:
        for m, it in bd.next_intents(0):
            log.append(enq(it))
        log += step
        while log:
            for tr in bd.feed(log.pop(0)):
                if proj:
                    proj.apply(tr, bd)
    for m, it in bd.next_intents(0):
        log.append(enq(it))
    return log


OK1 = [{"rec": "txn", "phase": "begin", "event_id": "t.modeler-agent.1.m1", "attempt": "t.modeler-agent.1.m1.a1"},
       {"rec": "close", "event_id": "t.modeler-agent.1.m1", "verdict": "STATE_VERIFIED"}]
E = "t.props-agent.1.m1"
BEGIN = {"rec": "txn", "phase": "begin", "event_id": E, "attempt": E + ".a1"}
JUDGE = {"rec": "judge", "attempt": E + ".a1", "pick": False, "oracles": [{"name": "expect", "ok": False, "detail": "EXPECT_MISMATCH"}]}
REWIND = {"rec": "control", "op": "rewind", "to": "p1", "attempt": E + ".a1", "digest_match": True, "scene_epoch_after": 2}
ALLREJ = {"rec": "close", "event_id": E, "verdict": "ALL_REJECTED"}
OK2 = [{"rec": "txn", "phase": "begin", "event_id": "t.props-agent.1.m2", "attempt": "t.props-agent.1.m2.a1"},
       {"rec": "close", "event_id": "t.props-agent.1.m2", "verdict": "STATE_VERIFIED"}]


class Fold(unittest.TestCase):
    def test_reject_rollback_replan_verified(self):
        bd, r = board(), Rec()
        pj = TP.MicrotaskProjector(r, r)
        for t in bd.tasks:
            pj.plan(t)
        run(bd, [OK1, [BEGIN, JUDGE, REWIND, ALLREJ], OK2], pj)
        by = {m.microtask_id: m for m in bd.all()}
        self.assertEqual(by[E].history, ["pending", "queued", "acting", "rolled_back"])
        self.assertTrue(by[E].proven)
        self.assertEqual(by[E].superseded_by, "t.props-agent.1.m2")
        self.assertEqual(by["t.props-agent.1.m2"].history, ["pending", "queued", "acting", "verified"])
        self.assertEqual(by["t.props-agent.1.m2"].supersedes, E)
        self.assertTrue(all(t.closed for t in bd.tasks))
        self.assertEqual(bd.violations, [])
        plans = [c[3]["steps"] for c in r.calls if c[0] == "tl" and c[1] == "props-agent" and c[2] == "post_plan"]
        self.assertEqual(len(plans), 2)
        self.assertEqual(plans[1][0], plans[0][0])                      # append-only
        self.assertIn("(retry of m1)", plans[1][1]["text"])
        steps = [(c[3]["index"], c[3]["state"]) for c in r.calls if c[0] == "tl" and c[1] == "props-agent" and c[2] == "post_step"]
        self.assertEqual(steps, [(0, "pending"), (0, "active"), (0, "blocked"), (1, "pending"), (1, "active"), (1, "done")])
        self.assertEqual([c[1] for c in r.calls if c[0] == "tl" and c[2] == "close_task"], ["modeler-agent", "props-agent"])
        hand = [c for c in r.calls if c[0] == "tl" and c[2] == "post_status" and c[3].get("to") == "props-agent"]
        self.assertEqual(len(hand), 1)
        self.assertNotIn("bad", repr(r.calls).replace("blocked", ""))   # script source never projected
        self.assertTrue(all(c[3] for c in r.calls if c[0] == "osb" and c[2] == "refute"))

    def test_handoff_blocks_downstream_until_verified(self):
        bd = board()
        first = bd.next_intents(0)
        self.assertEqual([m.microtask_id for m, _ in first], ["t.modeler-agent.1.m1"])   # props waits for the handoff
        self.assertEqual(first[0][1]["tm"]["microtask_id"], first[0][1]["event_id"])
        self.assertEqual(bd.next_intents(0), [])                                          # one microtask at a time

    def test_nc_rollback_needs_digest_proof(self):
        """Guard: `proven = digest_match is True`. A rewind with digest_match=false must not prove the rollback."""
        bd = board()
        run(bd, [OK1, [BEGIN, JUDGE, dict(REWIND, digest_match=False), ALLREJ]])
        m = bd.by_id(E)
        self.assertFalse(m.proven)
        self.assertIn([E, "ROLLBACK_UNPROVEN", None], bd.violations)
        # the same stream with the guard switched off is (wrongly) clean: the check above is what detects it
        bd2 = board(digest_proof=False)
        run(bd2, [OK1, [BEGIN, JUDGE, dict(REWIND, digest_match=False), ALLREJ]])
        self.assertEqual(bd2.violations, [])

    def test_nc_enqueue_without_tm_is_a_violation(self):
        """Guard: the enqueue record must carry tm.microtask_id equal to the planned microtask."""
        bd = board()
        (m, it), = bd.next_intents(0)
        it.pop("tm")
        bd.feed(enq(it))
        self.assertEqual(bd.violations, [[m.microtask_id, "TM_ID_MISMATCH", None]])

    def test_nc_illegal_transition_refused(self):
        """Guard: ALLOWED table. A verified microtask cannot be rolled back by a late judge/rewind of its attempt."""
        bd = board()
        run(bd, [OK1])
        m = bd.by_id("t.modeler-agent.1.m1")
        self.assertIsNone(bd._move(m, "acting"))
        self.assertEqual(m.state, "verified")
        self.assertEqual(bd.violations, [[m.microtask_id, "verified", "acting"]])

    def test_stale_frame_requeues_then_other_code_rejects(self):
        bd = board()
        run(bd, [[{"rec": "close", "event_id": "t.modeler-agent.1.m1", "verdict": "STALE_FRAME", "drops": 1}]])
        m = bd.by_id("t.modeler-agent.1.m1")
        self.assertEqual((m.state, m.closed), ("queued", False))
        bd.feed({"rec": "close", "event_id": m.event_id, "verdict": "CLAIM_REFUSED", "reason": "x"})
        self.assertEqual(m.state, "rejected")
        self.assertFalse(bd.tasks[0].done())


if __name__ == "__main__":
    unittest.main()
