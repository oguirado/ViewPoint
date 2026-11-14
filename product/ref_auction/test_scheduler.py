"""Tests for the turn-order auction (Track P, Claude-written).   python3 -m unittest -v test_scheduler

Every negative control re-runs the scenario with ONE safety switched off and asserts the bad outcome
then happens, i.e. the positive assertion is carried by that safety and by nothing else.
"""
import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scheduler as S  # noqa: E402
import sim as M  # noqa: E402

RING = M.RING
A, B, C, D = RING


def view(elig, bids=None, turn=0, head=5, mode="IDLE", scores=None):
    """elig: [(agent, lane, based_on_frame)]"""
    e = [{"agent_id": a, "event_id": a + "-1", "lane": lane,
          "intent": {"event_id": a + "-1", "agent_id": a, "lane": lane, "based_on_frame": based}}
         for a, lane, based in elig]
    return {"turn": turn, "tick": 0, "mode": mode, "ring": list(RING), "head_frame": head, "eligible": e,
            "bids": {a: {a + "-1": b} for a, b in (bids or {}).items()}, "scores": scores}


def auctions(log):
    return [r for r in log if r.get("rec") == "auction"]


class H1Regression(unittest.TestCase):
    def test_zero_bids_saturated_queues_reproduce_round_robin(self):
        mk = lambda: {a: M.Agent("script") for a in RING}
        rr = M.Sim(RING, S.RoundRobinScheduler(RING), mk()).run(60)
        au = M.Sim(RING, S.AuctionScheduler(RING), mk()).run(60)
        self.assertEqual(au.served, rr.served)
        self.assertEqual(au.served[:5], [A, B, C, D, A])
        self.assertTrue(all(r["price"] == 0 for r in auctions(au.log)))

    def test_alpha0_ties_follow_the_ring_after_the_last_served(self):
        rng = M.Lcg(7)
        s = S.AuctionScheduler(RING, alpha=0, aging_guard=False)
        for t in range(300):
            elig = [(a, "script", 5) for a in RING if rng.below(3)]
            last = s.last_served
            p = s.pick(view(elig, turn=t))
            if elig:
                start = RING.index(last) + 1 if last else 0   # independent oracle: walk the ring
                walk = [RING[(start + i) % len(RING)] for i in range(len(RING))]
                self.assertEqual(p["agent_id"], next(a for a in walk if a in [x[0] for x in elig]))

    def test_documented_gap_staggered_arrivals_prefer_the_longest_waiter(self):
        # ring A,B,C; A served twice while C waits, then B arrives. Round-robin would serve B
        # (next in ring); alpha*wait serves C. Zero bids equal round-robin ONLY on saturated queues.
        s = S.AuctionScheduler(RING[:3], w_max=50)
        ring = RING[:3]
        v = lambda el, t: dict(view([(a, "script", 5) for a in el], turn=t), ring=list(ring))
        s.wait[C], s.last_served = 2, A
        self.assertEqual(s.pick(v([B, C], 3))["agent_id"], C)


class Starvation(unittest.TestCase):
    def scenario(self, **kw):
        # camera-agent never re-observes: its GUI intent is stale forever (readiness 0)
        agents = {a: M.Agent("script", bid=3) for a in RING}
        agents[D] = M.Agent("gui", refresh=None)
        sched = S.AuctionScheduler(RING, **kw)
        sim = M.Sim(RING, sched, agents)
        sim.frame = 1  # the intent D drafts at frame 1 goes stale after the first tick
        return sim.run(300), sched

    def test_unready_agent_is_still_offered_a_turn_within_the_bound(self):
        sim, sched = self.scenario()
        self.assertGreater(sim.served.count(D), 20)
        self.assertLessEqual(max(sim.max_gap.values()), sched.p.bound)
        guard = [r for r in auctions(sim.log) if r["reason"] == "aging_guard"]
        self.assertTrue(guard and all(r["winner"] == D and r["price"] == 0 for r in guard))
        self.assertEqual(S.verify_log(sim.log, RING), [])

    def test_negative_control_without_the_aging_guard_it_starves(self):
        sim, _ = self.scenario(aging_guard=False)
        self.assertEqual(sim.served.count(D), 0)  # 300 rounds, never offered a turn, never told it is stale

    def test_bound_holds_on_random_histories(self):
        for seed in range(12):
            rng = M.Lcg(seed)
            n = 2 + seed % 4
            ring = RING[:n] if n <= 4 else RING + ("extra-agent",)
            agents = {a: M.Agent("gui" if rng.below(2) else "script", bid=lambda r, g=rng: g.below(40) - 5,
                                 refresh=1 + rng.below(4), arrive=lambda r, g=rng: g.below(3) > 0) for a in ring}
            sched = S.AuctionScheduler(ring)
            sim = M.Sim(ring, sched, agents).run(1500)
            self.assertLessEqual(max(sim.max_gap.values()), sched.p.bound, "seed %d" % seed)
            self.assertEqual(S.verify_log(sim.log, ring), [], "seed %d" % seed)
            self.assertTrue(all(v >= 0 for r in auctions(sim.log) for v in r["balances"].values()))

    def test_negative_control_rich_bidder_starves_a_zero_bidder_without_guard_and_clamp(self):
        def run(**kw):
            agents = {A: M.Agent("script", bid=10 ** 9), B: M.Agent("script", bid=0)}
            return M.Sim(RING[:2], S.AuctionScheduler(RING[:2], alpha=0, **kw), agents).run(200)
        self.assertEqual(run(aging_guard=False, clamp_bids=False).served.count(B), 0)
        safe = run()
        self.assertGreaterEqual(safe.served.count(B), 200 // (safe.sched.p.bound + 1))


class BidInflation(unittest.TestCase):
    def run_sim(self, **kw):
        agents = {a: M.Agent("script", bid=1) for a in RING}
        agents[C] = M.Agent("script", bid=10 ** 9)  # the inflator
        sched = S.AuctionScheduler(RING, **kw)
        return M.Sim(RING, sched, agents).run(400), sched

    def test_inflated_bid_is_clamped_to_budget_and_cannot_buy_the_queue(self):
        sim, sched = self.run_sim()
        for r in auctions(sim.log):
            row = next(x for x in r["eligible"] if x["agent_id"] == C)
            self.assertEqual(row["bid"], 10 ** 9)
            self.assertLessEqual(row["bid_eff"], min(row["balance"], sched.p.bid_cap))
            self.assertLessEqual(r["price"], sched.p.bid_cap)
            self.assertTrue(all(v >= 0 for v in r["balances"].values()))
        share = sim.stats()["share_pct"]
        self.assertLessEqual(share[C], 40, share)       # 25 % would be the equal share
        self.assertTrue(all(share[a] >= 15 for a in RING), share)
        self.assertEqual(S.verify_log(sim.log, RING), [])

    def test_negative_control_without_clamp_and_guard_the_inflator_takes_every_turn(self):
        sim, _ = self.run_sim(clamp_bids=False, aging_guard=False, alpha=0)
        self.assertEqual(sim.stats()["share_pct"][C], 100)

    def test_untrusted_bid_values_count_as_zero(self):
        for bad in (True, 3.9, "9", None, -5, [7], {"bid": 7}, float("inf")):
            s = S.AuctionScheduler(RING)
            p = s.pick(view([(A, "script", 5), (B, "script", 5)], bids={B: bad}))
            row = p["records"][0]["eligible"][1]
            self.assertEqual((row["bid_eff"], p["agent_id"]), (0, A), repr(bad))
            json.dumps(p["records"])

    def test_price_is_the_critical_bid_and_truthful_bidding_is_weakly_dominant_per_turn(self):
        rng = M.Lcg(3)
        p = S.Params(4)
        for _ in range(400):
            others = [{"agent_id": a, "event_id": "e", "bid": rng.below(9), "wait": rng.below(4),
                       "readiness": 1 + rng.below(1000), "balance": 8} for a in (B, C, D)]
            me = {"agent_id": A, "event_id": "e", "wait": rng.below(4), "readiness": 1 + rng.below(1000), "balance": 8}
            value = rng.below(9)

            def utility(bid):
                _, w, _, price, _ = S.decide(others + [dict(me, bid=bid)], RING, D, p)
                return (value - price if w["agent_id"] == A else 0), w["agent_id"] == A, price
            truthful = utility(value)[0]
            prices = set()
            for bid in range(9):
                u, won, price = utility(bid)
                self.assertLessEqual(u, truthful)
                if won:
                    prices.add(price)
                    self.assertLessEqual(price, bid)
            self.assertLessEqual(len(prices), 1)  # the price does not depend on the winning bid


class Readiness(unittest.TestCase):
    def test_stale_frame_intent_scores_zero_and_cannot_outbid_a_ready_one(self):
        self.assertEqual(S.default_readiness({"lane": "gui", "based_on_frame": 4}, 5), 0)
        self.assertEqual(S.default_readiness({"lane": "gui", "based_on_frame": 5}, 5), 1000)
        self.assertEqual(S.default_readiness({"lane": "script", "based_on_frame": 0}, 5), 1000)
        self.assertEqual(S.default_readiness(None, 5), 0)
        s = S.AuctionScheduler(RING)
        p = s.pick(view([(A, "gui", 4), (B, "gui", 5)], bids={A: 8, B: 0}))
        stale = p["records"][0]["eligible"][0]
        self.assertEqual((stale["readiness"], stale["score"], stale["bid_eff"]), (0, 0, 8))
        self.assertEqual((p["agent_id"], p["price"], p["reason"]), (B, 0, "auction"))
        self.assertEqual(s.balance[A], s.p.balance_init)  # the loser is not charged

    def test_negative_control_without_readiness_the_stale_high_bid_wins(self):
        s = S.AuctionScheduler(RING, use_readiness=False)
        p = s.pick(view([(A, "gui", 4), (B, "gui", 5)], bids={A: 8, B: 0}))
        self.assertEqual(p["agent_id"], A)

    def test_judge_scores_in_the_view_override_the_default_and_are_clamped(self):
        s = S.AuctionScheduler(RING)
        p = s.pick(view([(A, "script", 5), (B, "script", 5)], scores={A: {A + "-1": 0}, B: {B + "-1": 99999}}))
        rows = p["records"][0]["eligible"]
        self.assertEqual([r["readiness"] for r in rows], [0, 1000])
        self.assertEqual(p["agent_id"], B)

    def test_higher_readiness_wins_at_equal_bids_and_a_broken_judge_scores_zero(self):
        s = S.AuctionScheduler(RING)
        p = s.pick(view([(A, "script", 5), (B, "script", 5)], scores={A: {A + "-1": 300}, B: {B + "-1": 900}}))
        self.assertEqual(p["agent_id"], B)

        def boom(intent, head):
            raise RuntimeError("judge down")
        s = S.AuctionScheduler(RING, scorer=boom)
        p = s.pick(view([(A, "script", 5), (B, "script", 5)], bids={A: 8}))
        self.assertEqual((p["reason"], p["price"], p["agent_id"]), ("all_unready", 0, A))

    def test_all_unready_goes_to_the_longest_waiter_for_free(self):
        s = S.AuctionScheduler(RING)
        s.wait[C] = 3
        p = s.pick(view([(A, "gui", 1), (C, "gui", 2)], bids={A: 8}))
        self.assertEqual((p["agent_id"], p["reason"], p["price"]), (C, "all_unready", 0))


class Budgets(unittest.TestCase):
    def pick_paid(self, s):
        p = s.pick(view([(A, "script", 5), (B, "script", 5)], bids={A: 5, B: 3}))
        self.assertEqual((p["agent_id"], p["price"], p["records"][0]["runner_up"]), (A, 3, B))
        return p

    def test_charge_at_pick_kept_on_agent_fault_refunded_on_broker_fault(self):
        for verdict, kept in (("STALE_FRAME", 3), ("VERIFIED", 3), ("TURN_TIMEOUT", 3), ("ACTUATOR_BUSY", 0)):
            s = S.AuctionScheduler(RING)
            p = self.pick_paid(s)
            self.assertEqual(s.balance[A], s.p.balance_init - 3)
            rec = s.settle(p, {"verdict": verdict, "verify_frame_ref": None})[0]
            self.assertEqual((rec["charged"], rec["refunded"], s.balance[A]),
                             (3, 3 - kept, s.p.balance_init - kept), verdict)
            self.assertEqual(s.settle(p, {"verdict": verdict}), [], "settle must be idempotent")
            self.assertEqual(s.balance[A], s.p.balance_init - kept)

    def test_income_per_closed_tick_is_clipped_at_the_cap_and_uncertain_is_held(self):
        s = S.AuctionScheduler(RING)
        s.balance[D] = s.p.balance_cap
        p = self.pick_paid(s)
        rec = s.settle(p, {"verdict": "OUTCOME_UNCERTAIN", "verify_frame_ref": 6})[0]
        self.assertEqual((rec["income"], rec["clipped"], rec["held"], s.balance[D]), (1, 1, True, s.p.balance_cap))
        self.assertEqual(s.balance[B], s.p.balance_init + 1)

    def test_an_agent_cannot_spend_more_than_it_holds(self):
        s = S.AuctionScheduler(RING)
        s.balance[A] = 2
        p = s.pick(view([(A, "script", 5), (B, "script", 5)], bids={A: 8, B: 7}))
        self.assertEqual(p["agent_id"], B)  # A's effective bid is 2
        self.assertGreaterEqual(min(s.balance.values()), 0)


class Records(unittest.TestCase):
    def sim(self):
        rng = M.Lcg(11)
        agents = {a: M.Agent("gui", bid=lambda r, g=rng: g.below(12), refresh=1 + i) for i, a in enumerate(RING)}
        return M.Sim(RING, S.AuctionScheduler(RING), agents).run(200)

    def test_every_decision_is_one_self_contained_auction_record(self):
        sim = self.sim()
        recs = auctions(sim.log)
        self.assertEqual(len(recs), 200)
        need = {"v", "rec", "turn", "tick", "policy", "params", "eligible", "winner", "event_id", "reason", "price",
                "runner_up", "head_frame", "balances", "waits", "last_served"}
        for r in recs:
            self.assertLessEqual(need, set(r))
            self.assertIn(r["reason"], ("auction", "aging_guard", "sole", "none", "all_unready"))
            for row in r["eligible"]:
                self.assertLessEqual({"agent_id", "event_id", "bid", "bid_eff", "wait", "readiness", "score", "balance"},
                                     set(row))
        closes = [r for r in sim.log if r["rec"] == "close"]
        self.assertEqual([r["winner"] for r in recs], [c["agent_id"] for c in closes])
        self.assertEqual(S.verify_log(sim.log, RING), [])

    def test_negative_control_replay_catches_a_tampered_record(self):
        log = self.sim().log
        idx = [i for i, r in enumerate(log) if r.get("rec") == "auction" and r["reason"] == "auction" and r["price"] > 0]
        for mutate in (lambda r: r.update(winner=r["runner_up"]), lambda r: r.update(price=r["price"] - 1),
                       lambda r: r["eligible"][0].update(score=r["eligible"][0]["score"] + 50),
                       lambda r: r["balances"].update({r["winner"]: r["balances"][r["winner"]] + 5})):
            bad = copy.deepcopy(log)
            mutate(bad[idx[3]])
            self.assertNotEqual(S.verify_log(bad, RING), [])
        bad = copy.deepcopy(log)
        j = next(i for i, r in enumerate(bad) if r.get("rec") == "auction_settle")
        bad[j]["balances"][A] += 1
        self.assertNotEqual(S.verify_log(bad, RING), [])

    # ---- xr-capsule-auction A1/A2: forgeries that kept the sum (or logged their own params) passed
    def honest(self):
        """6 turns: A outbids B twice (price > 0), one of them closes ACTUATOR_BUSY (refund)."""
        s, log = S.AuctionScheduler(RING), []
        for t in range(6):
            p = s.pick(view([(A, "script", 5), (B, "script", 5), (C, "script", 5)], bids={A: 6, B: 3}, turn=t))
            verdict = "ACTUATOR_BUSY" if t == 1 else "VERIFIED"
            log += p["records"] + s.settle(p, {"verdict": verdict, "verify_frame_ref": None if t == 1 else t})
        self.assertEqual(S.verify_log(log, RING), [])
        return log

    def settles(self, log):
        return [i for i, r in enumerate(log) if r["rec"] == "auction_settle"]

    def test_replay_flags_money_created_at_a_broker_fault_settle(self):
        log = self.honest()
        j = next(i for i in self.settles(log) if log[i]["charged"] == 0)
        r = log[j]
        r.update(verdict="ACTUATOR_BUSY", charged=5, refunded=5, balance=r["balance"] + 5)
        r["balances"][r["agent_id"]] += 5
        self.assertTrue(any("charged" in b for b in S.verify_log(log, RING)), S.verify_log(log, RING))

    def test_replay_flags_a_transfer_between_agents_at_settle(self):
        log = self.honest()
        r = log[self.settles(log)[2]]
        r["balances"][C] -= 3
        r["balances"][A] += 3
        r["balance"] = r["balances"][r["agent_id"]]
        self.assertTrue(any("balances after settle" in b for b in S.verify_log(log, RING)), S.verify_log(log, RING))

    def test_replay_flags_forged_income_and_balances_above_the_cap(self):
        log = self.honest()
        r = log[next(i for i in self.settles(log) if log[i]["income"] == 1)]
        r["income"] = 50
        r["balances"] = {a: v + 49 for a, v in r["balances"].items()}
        r["balance"] = r["balances"][r["agent_id"]]
        self.assertTrue(any("income" in b for b in S.verify_log(log, RING)), S.verify_log(log, RING))

    def test_replay_flags_params_drift_and_unpinned_params(self):
        log = self.honest()
        bad = copy.deepcopy(log)
        bad[0]["params"]["bid_cap"] = 1000  # record 0 disagrees with every later record
        self.assertTrue(any("params drift" in b for b in S.verify_log(bad, RING)))
        self.assertEqual(S.verify_log(log, RING, S.Params(len(RING))), [])
        for r in bad:
            if r["rec"] == "auction":
                r["params"]["bid_cap"] = 1000  # consistent forgery: caught against the run configuration
        self.assertEqual(S.verify_log(bad, RING), [])
        self.assertTrue(any("params drift" in b for b in S.verify_log(bad, RING, S.Params(len(RING)))))
        bad = copy.deepcopy(log)
        k = [i for i, r in enumerate(bad) if r["rec"] == "auction"][3]
        bad[k]["params"]["w_max"] = 999  # mid-run: caught even with no expected params
        self.assertTrue(any("params drift" in b for b in S.verify_log(bad, RING)))
        bad = copy.deepcopy(log)
        for r in bad:
            if r["rec"] == "auction":
                r["params"]["aging_guard"] = False  # consistently logged, but a disabled safety
        self.assertTrue(any("safety switch" in b for b in S.verify_log(bad, RING)))

    def test_replay_flags_a_settle_that_is_not_the_open_pick(self):
        log = self.honest()
        j = self.settles(log)[0]
        self.assertTrue(any("without an open pick" in b for b in S.verify_log(log[:j + 1] + [log[j]] + log[j + 1:], RING)))

    def test_same_inputs_give_byte_identical_logs(self):
        self.assertEqual(json.dumps(self.sim().log, sort_keys=True), json.dumps(self.sim().log, sort_keys=True))

    def test_hold_and_none_do_not_pick(self):
        s = S.AuctionScheduler(RING)
        p = s.pick(view([(A, "script", 5)], mode="RECONCILE"))
        self.assertEqual((p["agent_id"], p["reason"], p["records"][0]["reason"]), (None, "hold", "hold"))
        p = s.pick(view([]))
        self.assertEqual((p["agent_id"], p["reason"]), (None, "none"))
        self.assertEqual(s.settle(p, {"verdict": "PASS"}), [])
        self.assertEqual(s.picks, 0)

    def test_restore_from_the_log_continues_identically(self):
        rng = M.Lcg(5)
        views = []
        for t in range(120):
            el = [(a, "gui", 5 - rng.below(2)) for a in RING if rng.below(4)]
            views.append(view(el, bids={a: rng.below(9) for a, _, _ in el}, turn=t))
        s1, log = S.AuctionScheduler(RING), []
        for v in views[:70]:
            p = s1.pick(v)
            log += p["records"] + s1.settle(p, {"verdict": "VERIFIED", "verify_frame_ref": 1})
        s2 = S.AuctionScheduler(RING).restore(log)
        self.assertEqual((s2.balance, s2.wait, s2.last_served), (s1.balance, s1.wait, s1.last_served))
        for v in views[70:]:
            p1, p2 = s1.pick(v), s2.pick(v)
            self.assertEqual(p1["records"], p2["records"])
            s1.settle(p1, {"verdict": "VERIFIED", "verify_frame_ref": 1})
            s2.settle(p2, {"verdict": "VERIFIED", "verify_frame_ref": 1})

    def test_only_the_fifo_head_of_an_agent_competes(self):
        s = S.AuctionScheduler(RING)
        v = view([(A, "script", 5), (A, "script", 5), ("ghost-agent", "script", 5)])
        self.assertEqual([r["agent_id"] for r in s.pick(v)["records"][0]["eligible"]], [A])

    def test_bad_params_are_refused(self):
        for kw in ({"alpha": -1}, {"w_max": 1.5}, {"bid_cap": True}):
            with self.assertRaises(ValueError):
                S.Params(4, **kw)
        with self.assertRaises(ValueError):
            S.AuctionScheduler((A, A))


class MatchedAB(unittest.TestCase):
    def test_auction_wastes_fewer_turns_than_round_robin_on_the_same_trace(self):
        r = M.ab(400)
        rr, au = r["round_robin"], r["auction"]
        self.assertLess(au["verdicts"].get("STALE_FRAME", 0), rr["verdicts"].get("STALE_FRAME", 0))
        self.assertGreater(au["verdicts"]["VERIFIED"], rr["verdicts"]["VERIFIED"])
        self.assertLessEqual(max(au["max_gap"].values()), S.Params(4).bound)
        self.assertTrue(all(v > 0 for v in au["share_pct"].values()), au)


if __name__ == "__main__":
    unittest.main()
