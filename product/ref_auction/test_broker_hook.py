"""Integration of the auction with product/ref_broker WITHOUT editing it (Track P, Claude-written).

The broker source is read from the F1 worktree (or VP_REF_BROKER_DIR), copied to a temp dir, given the
one-line scheduler seam (broker_adapter.patched_source) and driven with its own fake vision + bridge.
Skipped when the reference broker is not on this machine.   python3 -m unittest -v test_broker_hook
"""
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import broker_adapter as BA  # noqa: E402
import scheduler as S  # noqa: E402

SRC = os.environ.get("VP_REF_BROKER_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ref_broker"))
HAVE = all(os.path.isfile(os.path.join(SRC, f)) for f in ("broker.py", "bridge.py", "test_broker.py"))
M, P, L, C = "modeler-agent", "props-agent", "look-agent", "camera-agent"
COL = {M: "col:CHAR_mesh", P: "col:PROPS", L: "col:LOOK", C: "col:CAM"}


@unittest.skipUnless(HAVE, "reference broker not found at %s" % SRC)
class Hook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="vpref_auction_hook_")
        for f in ("bridge.py", "test_broker.py"):
            shutil.copy(os.path.join(SRC, f), cls.tmp)
        with open(os.path.join(SRC, "broker.py")) as f:
            cls.orig = f.read()
        with open(os.path.join(cls.tmp, "broker.py"), "w") as f:
            f.write(BA.patched_source(cls.orig))
        sys.path.insert(0, cls.tmp)
        cls.T = importlib.import_module("test_broker")  # binds the patched temp copy of broker
        cls.AB = BA.make_auction_broker(cls.T.B.Broker)

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(cls.tmp)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def env(self, sched=True, **kw):
        e = self.T.Env(cls=self.AB)
        self.addCleanup(e.close)
        if sched:
            e.broker.scheduler = S.AuctionScheduler(e.broker.agents, **kw)
        return e

    def put(self, e, agent, eid, bid=None, **kw):
        if kw.get("lane") != "gui":
            kw.setdefault("script", "scene[%r].append(%r); paint()" % (COL[agent], eid))
        e.put(agent, eid, write_set=[COL[agent]], **kw)
        if bid is not None:
            self.bid(e, agent, eid, bid)

    def bid(self, e, agent, eid, bid, **over):
        d = os.path.join(e.rt, "bids", agent)
        os.makedirs(d, exist_ok=True)
        rec = {"v": 1, "rec": "bid", "agent_id": agent, "event_id": eid, "bid": bid}
        rec.update(over)
        with open(os.path.join(d, eid + ".json"), "w") as f:
            json.dump(rec, f)

    # ---- the seam alone changes nothing
    def test_seam_is_one_line_and_the_brokers_own_suite_stays_green(self):
        self.assertEqual(self.orig.count(BA.SEAM_OLD), 1)
        patched = BA.patched_source(self.orig)
        self.assertEqual(patched.replace(BA.SEAM_METHOD, "").replace(BA.SEAM_NEW, BA.SEAM_OLD), self.orig)
        r = subprocess.run([sys.executable, "-m", "unittest", "test_broker"], cwd=self.tmp, capture_output=True,
                           text=True, timeout=300)
        tail = r.stderr.strip().splitlines()[-3:]
        self.assertEqual(r.returncode, 0, tail)
        print("\n[ref_broker suite on the seam-patched copy] %s" % " | ".join(tail), file=sys.stderr)

    def test_no_scheduler_means_round_robin_and_no_auction_records(self):
        e = self.env(sched=False)
        self.put(e, L, "l-1")
        self.put(e, M, "m-1")
        self.assertEqual(e.broker.run(4), ["STATE_VERIFIED", "PASS", "STATE_VERIFIED", "PASS"])
        self.assertEqual(e.events("auction") + e.events("auction_settle"), [])

    # ---- the auction orders real broker turns
    def test_bids_order_the_turns_and_every_decision_is_on_record_before_its_turn(self):
        e = self.env()
        self.put(e, M, "m-1", bid=0)
        self.put(e, P, "p-1", bid=2)
        self.put(e, L, "l-1", bid=6)
        self.put(e, C, "c-1", bid=4)
        self.assertEqual(e.broker.run(4), ["STATE_VERIFIED"] * 4)
        self.assertEqual(e.ran, ["l-1", "c-1", "p-1", "m-1"])
        ev = e.events()
        au, turns = e.events("auction"), e.events("turn")
        self.assertEqual([a["winner"] for a in au], [t["agent_id"] for t in turns])
        self.assertEqual([a["price"] for a in au], [4, 2, 0, 0])  # critical bids: P's 2, then P's 2 again (waits cancel), then no rival bid
        for a, t in zip(au, turns):
            self.assertEqual(a["seq"] + 1, t["seq"])  # auction record immediately precedes its turn record
        st = e.events("auction_settle")
        self.assertEqual([(s["agent_id"], s["charged"], s["verdict"]) for s in st],
                         [(L, 4, "STATE_VERIFIED"), (C, 2, "STATE_VERIFIED"), (P, 0, "STATE_VERIFIED"),
                          (M, 0, "STATE_VERIFIED")])
        self.assertEqual(S.verify_log(ev, e.broker.agents), [])
        self.assertEqual(e.broker.run(1), ["PASS"])  # nobody eligible: ring agent passes, reason none
        self.assertEqual(e.events("auction")[-1]["reason"], "none")

    def test_stale_gui_intent_loses_to_a_fresh_one_despite_the_bid_and_admit_stays_the_authority(self):
        e = self.env()
        self.put(e, M, "m-0")
        e.broker.run(1)                       # frame 0 -> 1
        self.put(e, C, "c-1", lane="gui", based=0, bid=8)       # stale, max bid
        self.put(e, L, "l-1", bid=0)                             # ready (script lane), zero bid
        self.assertEqual(e.broker.run(2), ["STATE_VERIFIED", "STALE_FRAME"])
        a = e.events("auction")[1]
        row = {r["agent_id"]: r for r in a["eligible"]}
        self.assertEqual((a["winner"], row[C]["readiness"], row[C]["score"], row[C]["bid"]), (L, 0, 0, 8))
        self.assertEqual(e.verdicts("c-1"), ["STALE_FRAME"])   # the broker, not the auction, refuses it

    def test_negative_control_same_scenario_without_readiness_serves_the_stale_bidder_first(self):
        e = self.env(use_readiness=False)
        self.put(e, M, "m-0")
        e.broker.run(1)
        self.put(e, C, "c-1", lane="gui", based=0, bid=8)
        self.put(e, L, "l-1", bid=0)                             # ready (script lane), zero bid
        self.assertEqual(e.broker.run(1), ["STALE_FRAME"])

    def test_bid_files_are_untrusted(self):
        e = self.env()
        self.put(e, M, "m-1")
        self.put(e, P, "p-1")
        self.bid(e, P, "p-1", 6, agent_id=M)             # mislabelled
        self.put(e, L, "l-1")
        self.bid(e, L, "l-1", "999")                      # not an int
        self.put(e, C, "c-1")
        with open(os.path.join(e.rt, "bids", C), "w") as f:   # not even a directory
            f.write("x")
        e.broker.run(1)
        a = e.events("auction")[0]
        self.assertEqual([r["bid"] for r in a["eligible"]], [0, 0, 0, 0])
        self.assertEqual(a["winner"], M)

    def test_restart_recovers_with_auction_records_in_the_log(self):
        e = self.env()
        for i in range(2):
            self.put(e, M, "m-%d" % i, bid=1)
            self.put(e, L, "l-%d" % i, bid=5)
        e.broker.run(2)
        before = dict(e.broker.scheduler.balance)
        e.broker.close()
        e.broker = e.new_broker()
        self.assertEqual((e.broker.turn, e.broker.tick), (2, 2))
        e.broker.scheduler = S.AuctionScheduler(e.broker.agents).restore(e.events())
        self.assertEqual(e.broker.scheduler.balance, before)
        e.broker.run(2)
        self.assertEqual(sorted(e.ran), ["l-0", "l-1", "m-0", "m-1"])
        self.assertEqual(S.verify_log(e.events(), e.broker.agents), [])

    # ---- the real judge (au-judge) through the real adapter (au-judge refute L14)
    def test_real_judge_scores_reach_the_auction_rows(self):
        try:
            from judge.judge import Judge  # noqa: F401
        except ImportError:
            self.skipTest("judge package not present")
        e = self.env()
        BA.attach_judge(e.broker)
        self.put(e, M, "m-1")
        self.put(e, L, "l-1")
        e.broker.run(1)
        self.put(e, C, "c-1", lane="gui", based=0, bid=8)   # stale: gated by the judge
        e.broker.run(1)
        au = e.events("auction")
        self.assertEqual([a["reason"] for a in au], ["auction", "auction"])
        self.assertTrue(all(r["readiness"] > 0 for r in au[0]["eligible"]), au[0]["eligible"])
        row = {r["agent_id"]: r for r in au[1]["eligible"]}
        self.assertEqual((row[C]["readiness"], au[1]["winner"]), (0, L))
        self.assertNotIn("scorer_errors", au[0])
        # au-judge's independent replay recomputes every row's readiness from events.jsonl alone
        from judge.replay import audit
        violations, stats = audit(e.events(), e.broker.claims, self.T.B.GUI_AGENTS)
        self.assertEqual((violations, stats["auctions"]), ([], 2))
        self.assertEqual(S.verify_log(e.events(), e.broker.agents), [])

    def test_a_failing_scorer_is_visible_in_the_record(self):
        e = self.env()
        e.broker.scheduler.scorer = BA.judge_scorer(object())   # not a judge: every call raises
        self.put(e, M, "m-1")
        self.put(e, L, "l-1")
        e.broker.run(1)
        a = e.events("auction")[0]
        self.assertEqual((a["reason"], a["scorer_errors"]), ("all_unready", [M, L]))



class BidPath(unittest.TestCase):
    """xr A4: an agent-chosen event_id must not move the bid read outside RT/bids/<agent>/."""

    def test_event_id_with_path_parts_reads_as_bid_zero(self):
        rt = tempfile.mkdtemp(prefix="vpau-bidpath-")
        try:
            os.makedirs(os.path.join(rt, "bids", "a-agent"))
            os.makedirs(os.path.join(rt, "bids", "b-agent"))
            def put(path, agent, eid, bid=7):
                with open(path, "w") as f:
                    json.dump({"v": 1, "rec": "bid", "agent_id": agent, "event_id": eid, "bid": bid}, f)
            put(os.path.join(rt, "bids", "a-agent", "e-1.json"), "a-agent", "e-1")
            self.assertEqual(BA.read_bid(rt, "a-agent", "e-1"), 7)  # the honest path still works
            put(os.path.join(rt, "secret.json"), "a-agent", "../../secret")
            self.assertEqual(BA.read_bid(rt, "a-agent", "../../secret"), 0)
            put(os.path.join(rt, "bids", "b-agent", "x.json"), "a-agent", "../b-agent/x")
            self.assertEqual(BA.read_bid(rt, "a-agent", "../b-agent/x"), 0)  # a rival's directory
            absid = os.path.join(rt, "abs")
            put(absid + ".json", "a-agent", absid)
            self.assertEqual(BA.read_bid(rt, "a-agent", absid), 0)
            self.assertEqual(BA.read_bid(rt, "a-agent", None), 0)
        finally:
            shutil.rmtree(rt, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
