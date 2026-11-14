"""Tests of the readiness judge on synthetic histories. Run from product/ref_auction:
    python3 -m unittest judge.test_judge -v
"""
import json
import os
import random
import sys
import tempfile
import unittest
from unittest import mock

from . import judge as J
from . import llm_advice as L

CLAIMS = {"modeler-agent": ["col:CHAR_mesh"], "props-agent": ["col:PROPS"],
          "look-agent": ["col:LOOK"], "camera-agent": ["col:CAM"]}
COL = {a: c[0] for a, c in CLAIMS.items()}


def script(agent="modeler-agent", n=1, base=10, **kw):
    it = {"v": 1, "rec": "enqueue", "event_id": "%s-%d" % (agent, n), "agent_id": agent, "lane": "script",
          "kind": "job", "based_on_frame": base, "write_set": [COL[agent]], "script": "import bpy\n", "expect": "x"}
    it.update(kw)
    return it


def gui(agent="camera-agent", n=1, base=10, keys=("KP_1",), **kw):
    it = {"v": 1, "rec": "enqueue", "event_id": "%s-%d" % (agent, n), "agent_id": agent, "lane": "gui",
          "kind": "key", "based_on_frame": base, "write_set": [], "keys": list(keys), "expect": "front view"}
    it.update(kw)
    return it


def close(agent, n, verdict, regions=((600, 300, 200, 200),), lane="script", **kw):
    r = {"v": 1, "rec": "close", "event_id": "%s-%d" % (agent, n), "agent_id": agent, "verdict": verdict,
         "lane": lane, "own_regions": [list(x) for x in regions], "spill_regions": [], "drops": 0}
    r.update(kw)
    return r


def rollback(agent, n, verdict="JOB_ERROR"):
    eid = "%s-%d" % (agent, n)
    return {"v": 1, "rec": "txn", "phase": "rollback", "event_id": eid, "attempt": eid + ".a1",
            "base_point": "p0", "verdict": verdict}


def hist(*recs):
    return J.History.from_events(list(recs))


def S(intent, head=10, h=None, **kw):
    return J.score_intent(intent, head, h or J.History(), CLAIMS, **kw)


class Contract(unittest.TestCase):
    def test_range_types_and_breakdown(self):
        r = S(script())
        self.assertIs(type(r["score"]), int)
        self.assertTrue(0 <= r["score"] <= 1000)
        self.assertEqual(set(r["features"]), set(J.WEIGHTS))
        for k, v in r["features"].items():
            self.assertIs(type(v), int, k)
            self.assertTrue(0 <= v <= 1000, k)
        self.assertEqual(r["base"], sum(J.WEIGHTS[k] * r["features"][k] for k in J.WEIGHTS) // 1000)
        self.assertEqual(sum(J.WEIGHTS.values()), 1000)

    def test_bounds_reached(self):
        h = hist(*[close("modeler-agent", i, "STATE_VERIFIED") for i in range(16)])
        best = S(script(n=99), h=h)
        self.assertGreaterEqual(best["score"], 950)
        self.assertLessEqual(best["score"], 1000)

    def test_deterministic_and_pure(self):
        recs = [close("modeler-agent", i, v) for i, v in enumerate(["VERIFIED", "JOB_ERROR", "STATE_VERIFIED"])]
        h = hist(*recs)
        it = script(n=50)
        before = json.dumps(it, sort_keys=True)
        a, b = S(it, h=h), S(it, h=hist(*recs))
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertEqual(json.dumps(it, sort_keys=True), before, "the intent must not be mutated")

    def test_no_float_anywhere(self):
        rng = random.Random(7)
        for _ in range(200):
            recs = [close("props-agent", i, rng.choice(J.GOOD + J.SOFT + J.HARD),
                          regions=[(rng.randrange(1900), rng.randrange(1000), rng.randrange(1, 900), rng.randrange(1, 900))])
                    for i in range(rng.randrange(20))]
            r = S(script("props-agent", n=77, base=rng.randrange(0, 11)), h=hist(*recs))
            self.assertIs(type(r["score"]), int)
            self.assertTrue(0 <= r["score"] <= 1000)
            self.assertTrue(all(type(v) is int for v in r["features"].values()))

    def test_incremental_feed_equals_batch(self):
        recs = [close("look-agent", i, v) for i, v in enumerate(["VERIFIED", "STALE_FRAME", "VERIFIED"])]
        h = J.History()
        for r in recs:
            h.feed(r)
        self.assertEqual(h.track("look-agent"), hist(*recs).track("look-agent"))

    def test_events_file_with_torn_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "events.jsonl")
            with open(p, "w") as f:
                f.write(json.dumps(close("look-agent", 1, "VERIFIED")) + "\n")
                f.write('{"rec": "close", "agent_id": "look-ag')  # torn append
            h = J.History.from_events(p)
            self.assertEqual(len(h.outcomes["look-agent"]), 1)
            self.assertEqual(J.History.from_events(os.path.join(d, "absent")).records, 0)


class Gates(unittest.TestCase):
    """Negative controls: what the broker's ADMIT phase would refuse scores exactly 0."""

    def test_stale_gui_intent_scores_zero(self):
        h = hist(*[close("camera-agent", i, "VERIFIED", lane="gui") for i in range(16)])  # a perfect record
        fresh, stale = S(gui(base=10), head=10, h=h), S(gui(base=9), head=10, h=h)
        self.assertGreater(fresh["score"], 800)
        self.assertEqual((stale["score"], stale["gate"]), (0, "STALE_FRAME"))

    def test_stale_gate_is_what_zeroes_it_mutant(self):
        # negative control of the test above: with the freshness gate removed the same intent scores > 0
        with mock.patch.object(J, "f_freshness", lambda it, head, ident: (1000, None)):
            self.assertGreater(S(gui(base=9), head=10)["score"], 0)

    def test_identical_pixels_rebase_is_not_stale(self):
        r = S(gui(base=9), head=10, identical_pixels=True)
        self.assertIsNone(r["gate"])
        self.assertEqual(r["features"]["freshness"], 900)

    def test_frame_from_the_future_or_outside_ring(self):
        self.assertEqual(S(gui(base=11), head=10)["gate"], "FRAME_EVICTED")
        self.assertEqual(S(script(base=11), head=10)["gate"], "FRAME_EVICTED")
        self.assertEqual(S(script(base=0), head=J.FRAME_RING + 1)["gate"], "FRAME_EVICTED")
        self.assertIsNone(S(script(base=1), head=J.FRAME_RING + 1)["gate"])

    def test_script_freshness_decays_monotonically(self):
        vals = [S(script(base=40 - lag), head=40)["features"]["freshness"] for lag in range(J.FRAME_RING + 1)]
        self.assertEqual(vals[0], 1000)
        self.assertEqual(vals, sorted(vals, reverse=True))
        self.assertGreaterEqual(vals[-1], 400)
        self.assertGreater(S(script(base=40), head=40)["score"], S(script(base=20), head=40)["score"])

    def test_claim_mismatch_scores_zero(self):
        steal = script(write_set=["col:PROPS"])
        self.assertEqual((S(steal)["score"], S(steal)["gate"]), (0, "CLAIM_MISMATCH"))
        self.assertEqual(S(script(write_set=["col:CHAR_mesh", "col:PROPS"]))["gate"], "CLAIM_MISMATCH")
        self.assertEqual(S(script(write_set=[]))["gate"], "CLAIM_MISMATCH")
        self.assertEqual(J.score_intent(script(), 10, J.History(), {})["gate"], "CLAIM_MISMATCH")
        self.assertIsNone(S(script())["gate"])

    def test_gui_lease(self):
        self.assertEqual(S(gui("props-agent"), gui_agents=("camera-agent",))["gate"], "NO_LEASE")
        self.assertIsNone(S(gui("camera-agent"), gui_agents=("camera-agent",))["gate"])

    def test_malformed_intents(self):
        for bad in (None, [], {}, script(lane="voice"), script(based_on_frame="10"), script(based_on_frame=True),
                    script(script="  "), gui(keys=[]), script(agent_id=""), {"rec": "close"}):
            r = J.score_intent(bad, 10, J.History(), CLAIMS)
            self.assertEqual((r["score"], r["gate"]), (0, "BAD_INTENT"), bad)

    def test_drops_lower_then_park(self):
        scores = []
        for d in range(3):
            h = hist(close("camera-agent", 1, "PASS", drops=d, lane="gui"))  # PASS: carries drops, no outcome
            scores.append(S(gui(), h=h)["score"])
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertGreater(scores[0], scores[2])
        parked = S(gui(), h=hist(close("camera-agent", 1, "STALE_FRAME", drops=3, lane="gui")))
        self.assertEqual((parked["score"], parked["gate"]), (0, "PARKED"))


class Track(unittest.TestCase):
    def clean(self, agent, n=8, start=0):
        return [close(agent, start + i, "STATE_VERIFIED") for i in range(n)]

    def test_three_recent_rollbacks_score_lower(self):
        good = self.clean("modeler-agent")
        bad = self.clean("props-agent")
        for i in range(3):
            bad += [close("props-agent", 100 + i, "JOB_ERROR"), rollback("props-agent", 100 + i)]
        h = hist(*(good + bad))
        a, b = S(script("modeler-agent", n=500), h=h), S(script("props-agent", n=500), h=h)
        self.assertGreater(a["score"], b["score"] + 100)
        self.assertGreater(a["features"]["track"], b["features"]["track"] + 400)
        for k in ("freshness", "claim", "effect", "cost", "retry"):
            self.assertEqual(a["features"][k], b["features"][k], "only the track record may differ: %s" % k)

    def test_rollback_control_mutant(self):
        # negative control: if rollbacks are not fed, the two agents tie
        recs = self.clean("props-agent")
        with_rb = recs + [r for i in range(3) for r in (close("props-agent", 100 + i, "VERIFIED"), rollback("props-agent", 100 + i, "JUDGE_REJECT"))]
        without = recs + [close("props-agent", 100 + i, "VERIFIED") for i in range(3)]
        self.assertLess(hist(*with_rb).track("props-agent"), hist(*without).track("props-agent") - 400)

    def test_each_rollback_lowers_monotonically(self):
        vals = []
        for k in range(5):
            recs = self.clean("look-agent")
            for i in range(k):
                recs += [close("look-agent", 100 + i, "JOB_ERROR"), rollback("look-agent", 100 + i)]
            vals.append(hist(*recs).track("look-agent"))
        self.assertTrue(all(x > y for x, y in zip(vals, vals[1:])), vals)

    def test_rollback_counted_once_with_its_close(self):
        h = hist(close("look-agent", 1, "JOB_ERROR"), rollback("look-agent", 1))
        self.assertEqual(h.outcomes["look-agent"], [["look-agent-1", 0, J.SEV_ROLLBACK]])
        # txn records carry no agent_id: the owner comes from the enqueue record
        h2 = hist(script("look-agent", n=5), rollback("look-agent", 5))
        self.assertEqual(h2.outcomes["look-agent"], [["look-agent-5", 0, J.SEV_ROLLBACK]])
        self.assertEqual(hist(rollback("ghost", 1)).outcomes, {}, "an unattributable rollback blames nobody")

    def test_recovery_recent_weighs_more_than_old(self):
        rb = [r for i in range(3) for r in (close("a", i, "JOB_ERROR"), rollback("a", i))]
        ok = [close("a", 10 + i, "VERIFIED") for i in range(8)]
        recovered, relapsed = hist(*(rb + ok)).track("a"), hist(*(ok + rb)).track("a")
        self.assertGreater(recovered, relapsed + 300)
        self.assertGreater(recovered, 600, "three old rollbacks must not bury an agent that then verified 8 times")
        self.assertLess(relapsed, 500, "three fresh rollbacks put a formerly clean agent below a newcomer")

    def test_window_forgets(self):
        recs = [close("a", i, "JOB_ERROR") for i in range(5)] + [close("a", 10 + i, "VERIFIED") for i in range(J.WINDOW)]
        self.assertEqual(hist(*recs).track("a"), hist(*recs[5:]).track("a"))

    def test_new_agent_is_neutral_and_between(self):
        h = hist(*(self.clean("good") + [close("bad", i, "JOB_ERROR") for i in range(8)]))
        self.assertEqual(h.track("newcomer"), 500)
        self.assertTrue(h.track("bad") < 500 < h.track("good"))

    def test_severity_order(self):
        base = self.clean("a", 4)
        t = {v: hist(*(base + [close("a", 9, v)])).track("a") for v in ("VERIFIED", "NO_VISIBLE_CHANGE", "JOB_ERROR")}
        t["ROLLBACK"] = hist(*(base + [close("a", 9, "JOB_ERROR"), rollback("a", 9)])).track("a")
        self.assertTrue(t["VERIFIED"] > t["NO_VISIBLE_CHANGE"] > t["JOB_ERROR"] > t["ROLLBACK"], t)
        self.assertLess(hist(*(base + [close("a", 9, "SOME_NEW_CODE")])).track("a"), t["NO_VISIBLE_CHANGE"],
                        "unknown verdicts fail closed (hard)")

    def test_broker_backpressure_is_not_the_agents_fault(self):
        base = self.clean("a", 4)
        noise = [close("a", 20 + i, v) for i, v in enumerate(("PASS", "QUEUE_FULL", "ACTUATOR_BUSY"))]
        self.assertEqual(hist(*(base + noise)).track("a"), hist(*base).track("a"))

    def test_calibration_on_synthetic_success_rates(self):
        """Bernoulli histories with soft failures: mean track follows the analytic Beta mean and keeps the order."""
        n_eff = sum(J.decay(i) for i in range(J.WINDOW)) / 1024.0
        means = []
        for p in (0.1, 0.3, 0.5, 0.7, 0.9):
            tot = 0
            for seed in range(300):
                rng = random.Random(seed * 1000 + int(p * 10))
                recs = [close("a", i, "VERIFIED" if rng.random() < p else "NO_VISIBLE_CHANGE") for i in range(J.WINDOW)]
                tot += hist(*recs).track("a")
            mean = tot / 300.0
            expect = 1000 * (1 + n_eff * p) / (2 + n_eff)
            self.assertLess(abs(mean - expect), 25, (p, mean, expect))
            means.append(mean)
        self.assertEqual(means, sorted(means))
        self.assertGreater(means[-1] - means[0], 500, "the feature must spread, not saturate")

    def test_calibration_rank_agreement(self):
        """Single histories: the higher true success rate wins in at least 90% of 0.9-vs-0.5 pairs."""
        wins = 0
        for seed in range(400):
            rng = random.Random(seed)
            t = {}
            for p in (0.9, 0.5):
                t[p] = hist(*[close("a", i, "VERIFIED" if rng.random() < p else "JOB_ERROR") for i in range(J.WINDOW)]).track("a")
            wins += t[0.9] > t[0.5]
        self.assertGreaterEqual(wins, 360, wins)


class EffectAndCost(unittest.TestCase):
    def test_union_area_exact(self):
        self.assertEqual(J.union_area([[0, 0, 10, 10], [5, 5, 10, 10]]), 175)
        self.assertEqual(J.union_area([[0, 0, 10, 10], [0, 0, 10, 10]]), 100)
        self.assertEqual(J.union_area([[-5, -5, 10, 10]]), 25, "clipped to the screen")
        self.assertEqual(J.union_area([[0, 0, 5000, 5000]]), 1920 * 1080)
        self.assertEqual(J.union_area([[1, 2], "x", None, [0, 0, 0, 9]]), 0, "junk regions are ignored")
        self.assertEqual(J.area_permille([[0, 0, 1920, 540]]), 500)
        rng = random.Random(3)
        for _ in range(50):  # against a brute-force raster on a small screen
            boxes = [[rng.randrange(-3, 20), rng.randrange(-3, 20), rng.randrange(0, 12), rng.randrange(0, 12)] for _ in range(6)]
            brute = sum(1 for x in range(24) for y in range(24) if any(b[0] <= x < b[0] + b[2] and b[1] <= y < b[1] + b[3] for b in boxes))
            self.assertEqual(J.union_area(boxes, (24, 24)), brute)

    def test_effect_tent(self):
        self.assertEqual([J.f_effect(p) for p in (None, 0, 5, 100, 250, 1000)], [500, 200, 1000, 1000, 1000, 400])
        ps = list(range(0, 1001))
        vals = [J.f_effect(p) for p in ps]
        self.assertTrue(all(200 <= v <= 1000 for v in vals))
        self.assertEqual(vals[:6], sorted(vals[:6]))
        self.assertEqual(vals[250:], sorted(vals[250:], reverse=True))

    def test_effect_from_changed_regions_history(self):
        def after(regions):
            h = hist(*[close("camera-agent", i, "VERIFIED", regions=regions, lane="gui") for i in range(4)])
            return S(gui(n=50), h=h)
        local, invisible, full = after([(600, 300, 400, 300)]), after([]), after([(0, 0, 1920, 1080)])
        self.assertEqual(local["features"]["effect"], 1000)
        self.assertEqual(invisible["features"]["effect"], 200)
        self.assertEqual(full["features"]["effect"], 400)
        self.assertTrue(local["score"] > full["score"] > invisible["score"])
        self.assertEqual(local["effect_permille"], 400 * 300 * 1000 // (1920 * 1080))

    def test_effect_is_per_lane_and_ignores_unverified(self):
        h = hist(close("camera-agent", 1, "VERIFIED", regions=[(0, 0, 1920, 1080)], lane="script"),
                 close("camera-agent", 2, "NO_VISIBLE_CHANGE", regions=[], lane="gui"))
        self.assertIsNone(h.expected_effect("camera-agent", "gui"))
        self.assertEqual(h.expected_effect("camera-agent", "script"), 1000)

    def test_target_region_used_without_history(self):
        self.assertEqual(S(gui(target_region=[600, 300, 200, 200]))["features"]["effect"], 1000)
        self.assertEqual(S(gui())["features"]["effect"], 500)

    def test_script_is_cheaper_than_gui(self):
        s, g1, g3 = S(script("camera-agent")), S(gui(keys=["KP_1"])), S(gui(keys=["KP_1", "KP_3", "KP_7"]))
        self.assertTrue(s["features"]["cost"] > g1["features"]["cost"] > g3["features"]["cost"])
        self.assertTrue(s["score"] > g1["score"] > g3["score"])
        self.assertGreaterEqual(S(gui(keys=["KP_1"] * 40))["features"]["cost"], 100)
        self.assertLess(S(script(script="x = 1\n" * 3000))["features"]["cost"], s["features"]["cost"])


class LlmAdvice(unittest.TestCase):
    def test_absent_advice_changes_nothing(self):
        it = script()
        self.assertEqual(S(it)["score"], S(it, advice={})["score"])
        self.assertEqual(S(it, advice={"other": {"adj": -100, "intent_sig": J.intent_sig(it)}})["llm_adj"], 0)

    def test_advice_is_bounded_and_bound_to_the_intent(self):
        it = script()
        base = S(it)["score"]
        sig = J.intent_sig(it)
        self.assertEqual(S(it, advice={it["event_id"]: {"adj": -40, "intent_sig": sig}})["score"], base - 40)
        self.assertEqual(S(it, advice={it["event_id"]: {"adj": -9999, "intent_sig": sig}})["score"], base - J.LLM_CLAMP)
        self.assertLessEqual(S(it, advice={it["event_id"]: {"adj": 9999, "intent_sig": sig}})["score"], 1000)
        edited = dict(it, script="import bpy\nbpy.ops.wm.quit_blender()\n")
        self.assertEqual(S(edited, advice={it["event_id"]: {"adj": 90, "intent_sig": sig}})["llm_adj"], 0,
                         "advice given for another script must not transfer")
        for junk in ({"adj": 1.5, "intent_sig": sig}, {"adj": "50", "intent_sig": sig}, {"adj": True, "intent_sig": sig}, "x", None):
            self.assertEqual(S(it, advice={it["event_id"]: junk})["llm_adj"], 0)

    def test_advice_cannot_lift_a_gated_intent(self):
        it = gui(base=9)
        r = S(it, head=10, advice={it["event_id"]: {"adj": 100, "intent_sig": J.intent_sig(it)}})
        self.assertEqual((r["score"], r["gate"]), (0, "STALE_FRAME"))

    def test_hot_path_never_spawns_or_opens(self):
        h = hist(*[close("modeler-agent", i, "VERIFIED") for i in range(16)])
        it = script(n=99)
        with mock.patch("subprocess.run", side_effect=AssertionError("model call in the hot path")), \
                mock.patch("builtins.open", side_effect=AssertionError("file I/O in the hot path")):
            self.assertGreater(S(it, h=h, advice={it["event_id"]: {"adj": 5, "intent_sig": J.intent_sig(it)}})["score"], 0)

    def test_parse_reply(self):
        self.assertEqual(L.parse_reply('noise {"adj": -30, "why": "risky"} tail'), {"adj": -30, "why": "risky"})
        self.assertEqual(L.parse_reply('{"adj": 5000}')["adj"], J.LLM_CLAMP)
        for bad in ("", "no json", '{"adj": "7"}', '{"adj": 1.5}', "[1]", '{"adj": true}'):
            self.assertIsNone(L.parse_reply(bad), bad)

    def _rt(self, d, intents):
        for it in intents:
            p = os.path.join(d, "intents", it["agent_id"])
            os.makedirs(p, exist_ok=True)
            with open(os.path.join(p, it["event_id"] + ".json"), "w") as f:
                json.dump(it, f)
        with open(os.path.join(d, "claims.json"), "w") as f:
            json.dump(CLAIMS, f)
        with open(os.path.join(d, "events.jsonl"), "w") as f:
            f.write(json.dumps({"rec": "turn", "turn": 1, "head_frame": 10}) + "\n")

    def test_offline_tool_with_fake_model(self):
        fake = "%s -c \"import sys; sys.stdin.read(); print('{\\\"adj\\\": -25, \\\"why\\\": \\\"fake\\\"}')\"" % sys.executable
        with tempfile.TemporaryDirectory() as d:
            ok, stale, thief = script(base=10), gui(base=3), script("props-agent", write_set=["col:CAM"])
            self._rt(d, [ok, stale, thief])
            cache = L.run(d, fake, timeout_s=20)
            self.assertEqual(list(cache), [ok["event_id"]], "gated intents are never sent to the model")
            self.assertEqual(cache[ok["event_id"]]["adj"], -25)
            with open(os.path.join(d, "judge_advice.json")) as f:
                advice = json.load(f)
            self.assertEqual(S(ok, advice=advice)["score"], S(ok)["score"] - 25)
            # second run: cached per intent version, the model is not called again
            with mock.patch.object(L, "advise", side_effect=AssertionError("called twice")):
                self.assertEqual(L.run(d, fake), cache)

    def test_offline_tool_survives_a_broken_model(self):
        with tempfile.TemporaryDirectory() as d:
            self._rt(d, [script()])
            for cmd in ("%s -c \"print('I think it is fine')\"" % sys.executable,
                        "%s -c \"import sys; sys.exit(3)\"" % sys.executable,
                        "/nonexistent/model-cli"):
                self.assertEqual(L.run(d, cmd, timeout_s=20), {})


class Queue(unittest.TestCase):
    def test_score_queue_on_a_mixed_room(self):
        recs = [close("modeler-agent", i, "STATE_VERIFIED") for i in range(8)]
        recs += [r for i in range(3) for r in (close("props-agent", i, "JOB_ERROR"), rollback("props-agent", i))]
        h = hist(*recs)
        q = [script("modeler-agent", n=50), script("props-agent", n=50), gui("camera-agent", n=50, base=7),
             script("look-agent", n=50, write_set=["col:CAM"])]
        r = J.score_queue(q, 10, h, CLAIMS)
        order = sorted(r, key=lambda e: (-r[e]["score"], e))
        self.assertEqual(order[:2], ["modeler-agent-50", "props-agent-50"])
        self.assertEqual({e: r[e]["gate"] for e in order[2:]},
                         {"camera-agent-50": "STALE_FRAME", "look-agent-50": "CLAIM_MISMATCH"})
        self.assertTrue(all(r[e]["score"] == 0 for e in order[2:]))

    def test_judge_wrapper_matches_pure_function_and_learns(self):
        j = J.Judge(CLAIMS, gui_agents=("camera-agent",))
        it = script("props-agent", n=9, base=4)
        first = j.scorer(it, 4)
        self.assertEqual(first, J.score_intent(it, 4, J.History(), CLAIMS)["score"])
        for r in (close("props-agent", 1, "JOB_ERROR", verify_frame_ref=5), rollback("props-agent", 1)):
            j.observe(r)
        self.assertLess(j.scorer(it, 5), first)
        self.assertEqual(j.score(it)["score"], j.scorer(it, 5), "head defaults to the last verify frame seen")
        self.assertEqual(j.score(gui("props-agent"), {"head_frame": 10})["gate"], "NO_LEASE")
        self.assertEqual(j.score(it, {"head_frame": 5, "advice": {it["event_id"]: {"adj": -7, "intent_sig": J.intent_sig(it)}}})["llm_adj"], -7)

    def test_from_rt(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "claims.json"), "w") as f:
                json.dump(CLAIMS, f)
            with open(os.path.join(d, "events.jsonl"), "w") as f:
                f.write(json.dumps(close("look-agent", 1, "VERIFIED", verify_frame_ref=3)) + "\n")
            j = J.Judge.from_rt(d)
            self.assertEqual(j.history.head_frame, 3)
            self.assertIsNone(j.score(script("look-agent", n=2, base=3))["gate"])
            self.assertEqual(J.Judge.from_rt(os.path.join(d, "nope")).score(script())["gate"], "CLAIM_MISMATCH")


if __name__ == "__main__":
    unittest.main()
