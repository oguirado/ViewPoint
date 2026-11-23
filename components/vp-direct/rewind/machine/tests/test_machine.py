from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import inspect
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest import TestCase
from unittest.mock import patch

DIRECT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(DIRECT), str(DIRECT.parent / "blender-visual-qa")]

from bus.broker import Broker
from rewind.machine import GoalMachine, Refused, potential
from rewind.machine import machine, transitions as tr
from vqa3d_bridge import QualityResult, RenderPredicate


def example(name):
    return json.loads((DIRECT / "schemas/tests/examples" / (name + "_v1.valid.json")).read_bytes())


def quality(score, digest="c" * 64, predicates=(True,)):
    return QualityResult(digest, score, tuple(RenderPredicate(str(i), ok)
                                              for i, ok in enumerate(predicates)))


class Interrupted(BaseException):
    pass


class GoalTests(TestCase):
    def setUp(self):
        root = TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(root.cleanup)
        self.path = Path(root.name) / "goals.sqlite3"
        self.binding = example("vp.surface_binding")
        self.waking = example("vp.transition_receipt")
        self.sleeping = dict(self.waking, to="ASLEEP", fresh_observation_id=None)
        self.sleeping["from"] = "ACTIVE"
        self.make()

    def make(self, goal="g", T=5, B=3, D=3, backend="blender_rw1", *, epsilon=10):
        initial = example("vp.rewind_receipt")
        initial.update(goal_id=goal, op="checkpoint", backend=backend, confirmed_tick=0)
        initial["budget"].update(attempts_limit=T, rewinds_limit=B, attempts_used=0, rewinds_used=0)
        self.m = GoalMachine.create(self.path, goal_id=goal, session_id="session",
                                    binding=self.binding, checkpoint=initial, score=100, steps=3,
                                    duplicates=D, epsilon=epsilon, action_quantum="0.01",
                                    observation_id="o0", generation=1)

    def reload(self):
        self.m = GoalMachine(self.path, self.m.goal_id)
        self.assertEqual(self.m.status(), self.m.replay())

    def refused(self, code, fn, *args, **kw):
        with self.assertRaises(Refused) as err:
            fn(*args, **kw)
        self.assertEqual(err.exception.code, code)

    def slept(self, receipt, **kwargs):
        s = self.m.status()
        return self.m.slept(receipt, generation=s["generation"],
                            authority_epoch=s["authority_epoch"], **kwargs)

    def awakened(self, receipt, **kwargs):
        return self.m.awakened(receipt, flight=self.m.status()["flight"], **kwargs)

    def failed_wake(self):
        return self.m.failed_wake(self.m.status()["flight"])

    def propose(self, attempt="bad.a1", action=None, **kw):
        s = self.m.status()
        return self.m.propose(attempt, {"value": 1.001} if action is None else action,
                              observation=s["observation"], supersedes=s["supersedes"], **kw)

    def reject(self):
        self.assertEqual(self.propose(), "EXECUTING")
        self.assertEqual(self.m.judge("bad.a1", "b" * 64, quality(109)), "JUDGE_REJECT")
        if self.m.status()["phase"] == "REJECTED":
            self.m.observed("rejected", 1)

    @staticmethod
    def restore(claim, s):
        receipt = deepcopy(s["checkpoint"])
        receipt.update(op="rewind", scene_epoch=s["scene_epoch"] + 1,
                       authority_epoch=s["authority_epoch"] + int(receipt["backend"] == "microvm_v0_2"))
        receipt["budget"].update(attempts_used=s["t"], rewinds_used=s["r"])
        return receipt

    def rewound(self, action=None):
        action = {"value": 2} if action is None else action
        self.assertEqual(self.propose("alt.a1", action, retry=True), "REWINDING")
        self.assertEqual(self.m.run_restore(self.restore), "READY")
        self.m.observed("after-restore", 2)

    def mark(self, attempt="alt.a1", state="d" * 64):
        s = self.m.status()
        point = deepcopy(s["checkpoint"])
        point.update(last_good="p1", confirmed_tick=1, scene_epoch=s["scene_epoch"],
                     authority_epoch=s["authority_epoch"], state_sha256=state)
        point["budget"].update(attempts_used=s["t"], rewinds_used=s["r"])
        return self.m.commit(attempt, point)

    def test_reject_rewind_commit_and_batch_as_one_attempt(self):
        self.reject()
        batch = {"seeds": [31, 32], "views": [1, 2, 3]}
        self.rewound(batch)
        self.refused("BAD_INTENT", self.propose, "alt.a1", {"value": 9})
        self.assertEqual(self.propose("alt.a1", batch), "EXECUTING")
        self.assertEqual(self.m.judge("alt.a1", "d" * 64, quality(111, "e" * 64)), "JUDGING")
        self.assertEqual(self.mark(), "COMMITTED")
        self.reload()
        s = self.m.status()
        self.assertEqual((s["t"], s["r"], s["best"], s["points"]), (2, 1, 111, ["p0", "p1"]))
        self.assertEqual(s["failed_branches"][0]["score_after"], 109)

    def test_natural_requires_a_nonnegative_int(self):
        self.assertTrue(tr.natural(0))
        for invalid in (-1, True, 0.0):
            with self.subTest(value=repr(invalid)):
                self.assertFalse(tr.natural(invalid))

    def test_goal_admits_minimum_positive_epsilon(self):
        self.refused("BAD_INTENT", self.make, "zero-epsilon", epsilon=0)
        self.make("minimum-epsilon", epsilon=1)
        self.assertEqual(self.m.status()["epsilon"], 1)

    def test_proposal_refuses_invalid_touch_container_or_element(self):
        for touches in ("surface", [""]):
            with self.subTest(touches=touches):
                self.refused("BAD_INTENT", self.propose, touches=touches)
        self.assertEqual(self.propose(touches=["surface"]), "EXECUTING")

    def test_equality_loses_and_every_hard_predicate_must_pass(self):
        for goal, score, predicates in (("equal", 110, (True,)), ("spill", 999, (True, False)),
                                        ("empty", 999, ())):
            self.make(goal)
            self.propose()
            self.assertEqual(self.m.judge("bad.a1", "b" * 64, quality(score, predicates=predicates)),
                             "JUDGE_REJECT")
            self.assertEqual(self.m.status()["best"], 100)

    def test_checkpoint_must_bind_judged_state(self):
        self.propose()
        self.m.judge("bad.a1", "b" * 64, quality(111))
        self.refused("CKPT_CORRUPT", self.mark, "bad.a1", "e" * 64)
        self.assertEqual(self.m.status()["checkpoint"]["last_good"], "p0")

    def test_quantized_branch_duplicate_survives_restart(self):
        self.reject()
        self.reload()
        self.assertEqual(self.propose("new.a1", {"value": 1.004}, retry=True), "BRANCH_ALREADY_FAILED")
        self.assertEqual(self.m.status()["t"], 1)

    def test_duplicate_charged_and_attempt_reuse_never_dispatches(self):
        self.reject()
        self.assertEqual(self.propose("bad.a1", {"value": 2}, retry=True), "ATTEMPT_REUSED")
        self.assertEqual(self.m.status()["d"], 1)
        self.reload()
        self.assertEqual(self.m.status()["attempts"], ["bad.a1"])

    def test_prior_state_or_digest_rejected_after_restore(self):
        for goal, state, scene in (("state", "b", "f"), ("digest", "f", "c")):
            self.make(goal)
            self.reject()
            self.rewound()
            self.propose("alt.a1", {"value": 2})
            self.assertEqual(self.m.judge("alt.a1", state * 64, quality(999, scene * 64)),
                             "STATE_ALREADY_FAILED")

    def test_budget_edge_and_cleanup_once_at_last_good(self):
        self.make("exhaust", T=1)
        self.propose()
        self.m.judge("bad.a1", "d" * 64, quality(111))
        self.mark("bad.a1")
        self.m.observed("next", 2)
        self.assertEqual(self.propose("next.a1", {"delta": 2}), "CLEANUP")
        calls = []

        def cleanup(claim, s):
            calls.append(claim)
            return self.restore(claim, s)

        self.assertEqual(self.m.run_restore(cleanup), tr.STOP)
        self.assertEqual(calls, ["ctl.restore.p1.e2"])
        self.reload()
        self.refused("NO_LEASE", self.m.run_restore, cleanup)
        self.refused("NO_LEASE", self.propose)
        self.assertEqual((self.m.status()["t"], self.m.status()["r"]), (1, 0))

    def test_duplicate_budget_exhaustion_runs_nothing(self):
        self.make("dups", D=2)
        self.reject()
        for _ in range(2):
            self.assertEqual(self.propose("bad.a1", retry=True), "ATTEMPT_REUSED")
        self.assertEqual(self.m.status()["phase"], "CLEANUP")
        self.m.run_restore(self.restore)
        self.assertEqual((self.m.status()["d"], self.m.status()["t"]), (2, 1))

    def test_zero_rewind_budget_still_performs_cleanup(self):
        self.make("zero", B=0)
        self.reject()
        self.assertEqual(self.m.status()["phase"], "CLEANUP")
        self.assertEqual(self.m.run_restore(self.restore), tr.STOP)
        self.assertEqual(self.m.status()["r"], 0)

    def test_claim_links_delivery_to_flight_and_duplicate_rolls_back(self):
        self.slept(self.sleeping)
        self.assertEqual(self.m.claim_external_wake("lease-sequence"), 1)
        self.reload()
        self.refused("NO_LEASE", self.m.claim_external_wake, "lease-sequence")
        self.assertEqual(self.m.request_wake(), "WAKE_PENDING")
        self.assertEqual(self.m.replay()["work_flights"], {"lease-sequence": 1})
        self.assertEqual(self.m.replay()["work"], {"lease-sequence": "consumed"})

    def test_frozen_rest_off_rejects_stale_epoch_and_preserves_budgets(self):
        self.slept(self.sleeping)
        before = self.m.replay()
        off = dict(self.sleeping, to="OFF", acks=["stream", "app"])
        off["from"] = "ASLEEP"
        self.refused("NO_LEASE", self.m.slept, off, authority_epoch=before["authority_epoch"] + 1,
                     generation=before["generation"])
        self.assertEqual(self.slept(off), "OFF")
        self.reload()
        self.assertEqual(self.m.status(), dict(before, perception="OFF"))

    def test_mutant_frozen_rest_off_recharges_an_empty_sleep(self):
        self.slept(self.sleeping)
        off = dict(self.sleeping, to="OFF", acks=["stream", "app"])
        off["from"] = "ASLEEP"
        before = self.m.status()
        def charge_again(state, **args):
            state["t"] += 1
            return tr.rest_off(state, **args)
        with patch.dict(tr.HANDLERS, rest_off=charge_again):
            self.assertEqual(self.slept(off), "OFF")
            with self.assertRaises(AssertionError):
                self.assertEqual(self.m.status()["t"], before["t"])
        self.refused("RESTORE_MISMATCH", self.m.replay)

    def test_failed_restore_hard_stops_without_second_restore(self):
        for failure in ("digest_match", "epoch", "foreign", "pin", "timeout", "null"):
            self.make(failure)
            self.reject()
            self.propose("alt.a1", {"value": 2}, retry=True)

            def broken(claim, state):
                r = self.restore(claim, state)
                if failure == "timeout":
                    raise TimeoutError()
                if failure == "null":
                    return None
                changes = {"digest_match": {"digest_match": False}, "epoch": {"scene_epoch": 1},
                           "foreign": {"goal_id": "someone-else"}, "pin": {"state_sha256": "9" * 64}}
                return dict(r, **changes[failure])

            self.assertEqual(self.m.run_restore(broken), tr.STOP)
            self.reload()
            self.refused("NO_LEASE", self.m.run_restore, self.restore)
            self.assertEqual(self.m.status()["reason"],
                             "RESTORE_TIMEOUT" if failure == "timeout" else "RESTORE_MISMATCH")

    def test_restart_after_restore_claim_never_resends(self):
        self.reject()
        self.propose("alt.a1", {"value": 2}, retry=True)
        captured = []

        def lost(claim, state):
            captured.append((claim, self.restore(claim, state)))
            raise Interrupted()

        with self.assertRaises(Interrupted):
            self.m.run_restore(lost)
        self.reload()
        self.refused("UNCERTAIN_OUTCOME", self.m.run_restore, lost)
        claim, receipt = captured.pop()
        self.assertEqual(self.m.finish_restore(claim, receipt=receipt), "READY")
        self.refused("NO_LEASE", self.m.finish_restore, claim, receipt=receipt)

    def test_vm_restore_renews_authority_and_retires_observation(self):
        self.make("vm", backend="microvm_v0_2")
        self.reject()
        self.rewound()
        self.assertEqual(self.m.status()["authority_epoch"], 2)
        self.refused("STALE_FRAME", self.m.observed, "o0", 3)

    def test_concurrent_claims_allow_one_execution(self):
        barrier = Barrier(2)

        def contend():
            client = GoalMachine(self.path, "g")
            barrier.wait()
            try:
                return client.propose("race.a1", {"value": 1}, observation="o0")
            except Refused as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(contend) for _ in range(2)]
        self.assertCountEqual([f.result() for f in results], ["EXECUTING", "NO_LEASE"])
        self.assertEqual(self.m.replay()["t"], 1)

    def test_real_broker_human_fence_and_fresh_return(self):
        with Broker(self.path.parent / "broker") as broker:
            broker.bind("session", self.binding, lease_state="AGENT")
            before = potential(self.m.status())
            take = broker.human_lease("session", "HUMAN", 1, "operator")
            self.assertEqual(self.m.human_lease(take), "HUMAN")
            self.reload()
            self.refused("NO_LEASE", self.propose)
            self.refused("NO_LEASE", self.m.request_wake)
            returned = broker.human_lease("session", "AGENT", 2, "operator")
            self.refused("STALE_FRAME", self.m.human_lease, returned, observation_id="o0", generation=1)
            self.m.human_lease(returned, observation_id="returned", generation=2)
            self.assertEqual(potential(self.m.replay()), before)
            self.refused("STALE_FRAME", self.m.propose, "new.a1", {"x": 1}, observation="o0")
            self.assertEqual(self.propose(), "EXECUTING")

    def test_sleep_uses_credit_once_after_restart(self):
        self.m.verified_step(0, "o0")
        self.slept(self.sleeping)
        self.reload()
        self.assertEqual((self.m.status()["a"], self.m.status()["q"], self.m.status()["t"]), (0, 1, 0))
        self.refused("NO_LEASE", self.propose)
        self.m.request_wake()
        self.refused("STALE_FRAME", self.awakened, dict(self.waking, fresh_observation_id="o0"),
                     binding=self.binding, generation=2)
        self.awakened(self.waking, binding=self.binding, generation=2)
        self.slept(self.sleeping)
        self.assertEqual(self.m.status()["t"], 1)

    def test_failed_wakes_quarantine_and_reopen_costs_rebase(self):
        self.slept(self.sleeping)
        for expected in ("ASLEEP", "QUARANTINED"):
            self.m.request_wake()
            self.assertEqual(self.failed_wake(), expected)
        self.reload()
        self.refused("NO_LEASE", self.m.request_wake)
        self.refused("STALE_FRAME", self.awakened, self.waking, binding=self.binding,
                     generation=1, reopen=True)
        self.assertEqual(self.awakened(self.waking, binding=self.binding, generation=3, reopen=True),
                         "ACTIVE")
        s = self.m.status()
        self.assertEqual((s["t"], s["r"], s["q"]), (3, 1, 0))

    def test_external_demand_races_agent_in_both_orders(self):
        for order in ("agent-first", "external-first", "external-finished"):
            self.make(order)
            self.m.admit_external_work("work-a")
            self.slept(self.sleeping)
            saved = potential(self.m.status())
            if order == "agent-first":
                self.assertEqual(self.m.request_wake(), 1)
                self.reload()
                self.assertEqual(self.m.request_wake(external_token="work-a"), "WAKE_PENDING")
            else:
                self.assertEqual(self.m.request_wake(external_token="work-a"), 1)
                self.assertEqual(potential(self.m.status()), saved)
                self.reload()
                if order == "external-first":
                    self.assertEqual(self.m.request_wake(), "WAKE_PENDING")
            self.awakened(self.waking, binding=self.binding, generation=2)
            if order == "external-finished":
                self.refused("NO_LEASE", self.propose)
                self.assertEqual(self.m.request_wake(), "ACTIVE")
            self.assertEqual(self.m.status()["work"]["work-a"],
                             "admitted" if order == "agent-first" else "consumed")
            self.m.settle_external_work("work-a")
            self.assertEqual(self.m.admit_external_work("work-a"), "ALREADY_ADMITTED")
            self.refused("NO_LEASE", self.m.request_wake, external_token="work-a")
            self.assertEqual(self.m.request_wake(), "DUPLICATE_WAKE")
            self.assertEqual(self.m.replay()["d"], 1)

    def test_no_external_token_no_wake_and_no_budget_on_external_wake(self):
        self.slept(self.sleeping, external=True)
        before = potential(self.m.status())
        self.refused("NO_LEASE", self.m.request_wake, external_token="notification")
        self.m.admit_external_work("admitted")
        self.m.request_wake(external_token="admitted")
        self.awakened(self.waking, binding=self.binding, generation=2)
        self.assertEqual(potential(self.m.status()), before)

    def test_strict_descent_on_agent_transitions_and_reconcile(self):
        def lowers(fn, *args, **kw):
            before = potential(self.m.status())
            fn(*args, **kw)
            self.reload()
            self.assertLess(potential(self.m.status()), before)

        lowers(self.m.verified_step, 0, "o0")
        lowers(self.slept, self.sleeping)
        lowers(self.m.request_wake)
        self.awakened(self.waking, binding=self.binding, generation=2)
        lowers(self.m.request_wake)  # duplicate
        lowers(self.slept, self.sleeping)  # empty
        lowers(self.m.request_wake)
        lowers(self.failed_wake)
        lowers(self.m.request_wake)
        lowers(self.failed_wake)
        lowers(self.awakened, dict(self.waking, fresh_observation_id="opened"),
               binding=self.binding, generation=3, reopen=True)
        lowers(self.propose)
        self.m.uncertain("bad.a1")
        self.reload()
        self.refused("NO_LEASE", self.propose)
        lowers(self.m.reconcile, "bad.a1", "verified", "readback", 3)
        self.m.judge("bad.a1", "b" * 64, quality(100))
        self.m.observed("next", 3)
        lowers(self.propose, "alt.a1", {"value": 2}, retry=True)

    def test_replay_detects_divergent_projection(self):
        with sqlite3.connect(self.path) as db:
            state = self.m.status()
            state["a"] = 1
            db.execute("UPDATE goals SET state=? WHERE id='g'", (json.dumps(state),))
        self.refused("RESTORE_MISMATCH", self.m.replay)

    def test_restore_epoch_advances_exactly_once(self):
        before = self.m.status()["scene_epoch"]
        self.reject()
        self.rewound()
        self.assertEqual(self.m.status()["scene_epoch"], before + 1)

    def test_failed_branch_blocks_even_identical_action(self):
        self.reject()
        self.assertEqual(self.propose("alt.a1", retry=True), "BRANCH_ALREADY_FAILED")

    def test_reconciliation_is_single_use(self):
        self.propose()
        self.assertEqual(self.m.uncertain("bad.a1"), "UNCERTAIN_OUTCOME")
        self.assertEqual(self.m.status()["u"], 1)
        self.m.reconcile("bad.a1", "verified", "read", 1)
        self.refused("NO_LEASE", self.m.uncertain, "bad.a1")
        self.refused("NO_LEASE", self.m.reconcile, "bad.a1", "no_effect", "read2", 1)

    def kill(self, module, original, replacement, oracle):
        """Execute the mutated guard in memory, prove reach, then require oracle failure."""
        hits = []

        def hit(value):
            hits.append(True)
            return value

        source = inspect.getsource(module)
        self.assertEqual(source.count(original), 1)
        ns = dict(vars(module), hit=hit)
        exec(compile(source.replace(original, replacement), module.__file__, "exec"), ns)
        if module is tr:
            ns["Refused"] = Refused
            from types import SimpleNamespace
            instrument = patch.object(machine, "tr", SimpleNamespace(**ns))
        else:
            instrument = patch.object(GoalMachine, "run_restore", ns["GoalMachine"].run_restore)
        with instrument, self.assertRaises((AssertionError, Refused)) as rejected:
            oracle()
        if isinstance(rejected.exception, Refused):
            expected = "BUDGET_EXCEEDED" if original.startswith('if s["t"]') else "NON_DESCENDING"
            self.assertEqual(rejected.exception.code, expected)
        self.assertTrue(hits, "mutated expression never executed")

    def test_mutant_commit_on_equal(self):
        self.kill(tr, "return score > best + epsilon", "return hit(score >= best + epsilon)",
                  self.test_equality_loses_and_every_hard_predicate_must_pass)

    def test_mutant_skip_epoch_bump(self):
        self.kill(tr, 's.update(scene_epoch=receipt["scene_epoch"]',
                  's.update(scene_epoch=hit(s["scene_epoch"])',
                  self.test_restore_epoch_advances_exactly_once)

    def test_mutant_skip_cleanup_restore(self):
        self.kill(machine, "receipt = broker_restore(claim, state)",
                  'receipt = hit(None) if state["phase"] == "CLEANUP" else broker_restore(claim, state)',
                  self.test_budget_edge_and_cleanup_once_at_last_good)

    def test_mutant_hash_without_quantization(self):
        self.kill(tr, "round(Fraction(str(value)) / step)", "hit(str(value))",
                  self.test_quantized_branch_duplicate_survives_restart)

    def test_mutant_ignore_failed_branch_ledger(self):
        self.kill(tr, 'if any(row["base_point"] == base', 'if hit(False) and any(row["base_point"] == base',
                  self.test_failed_branch_blocks_even_identical_action)

    def test_mutant_attempt_budget_off_by_one(self):
        self.kill(tr, 'if s["t"] >= s["T"] or (retry', 'if hit(s["t"] > s["T"]) or (retry',
                  self.test_budget_edge_and_cleanup_once_at_last_good)

    def test_mutant_drop_duplicate_charge(self):
        self.kill(tr, 's["d"] += 1', 's["d"] += hit(0)',
                  self.test_duplicate_charged_and_attempt_reuse_never_dispatches)

    def test_mutant_restore_stale_credit(self):
        self.kill(tr, "s.update(a=0, q=1)", 's.update(a=hit(s["a"]), q=1)',
                  self.test_sleep_uses_credit_once_after_restart)

    def test_stale_drain_and_completion_cannot_settle_new_flight(self):
        self.slept(self.sleeping)
        first = self.m.request_wake()
        self.failed_wake()
        second = self.m.request_wake()
        self.assertGreater(second, first)
        self.refused("STALE_FRAME", self.m.failed_wake, first)
        self.refused("STALE_FRAME", self.m.awakened, self.waking, binding=self.binding,
                     flight=first, generation=2)
        self.m.awakened(self.waking, binding=self.binding, flight=second, generation=2)
        self.refused("STALE_FRAME", self.m.slept, self.sleeping, generation=1, authority_epoch=1)
        self.assertEqual(self.m.replay()["perception"], "ACTIVE")

    def test_recompile_preserves_verified_prefix_and_spent_budgets(self):
        self.m.verified_step(0, "o0")
        self.m.observed("r1", 1)
        self.propose()
        self.m.judge("bad.a1", "d" * 64, quality(111))
        receipt = deepcopy(self.m.status()["checkpoint"])
        receipt.update(last_good="p1", confirmed_tick=1, state_sha256="d" * 64)
        receipt["budget"]["attempts_used"] = 1
        self.refused("BAD_INTENT", self.m.commit, "bad.a1", receipt, revised_steps=0)
        self.m.commit("bad.a1", receipt, revised_steps=4)
        s = self.m.replay()
        self.assertEqual((s["n"], s["i"], s["t"], s["attempts"]), (4, 1, 1, ["bad.a1"]))

    def test_ring_full_preserves_genesis_and_last_good(self):
        self.make("ring", T=10)
        for index in range(1, 9):
            attempt = f"op{index}.a1"
            self.propose(attempt, {"index": index})
            state = str(index) * 64
            decision = self.m.judge(attempt, state, quality(100 + 11 * index))
            if index == 8:
                self.assertEqual(decision, "CKPT_BUDGET")
                break
            self.assertEqual(decision, "JUDGING")
            receipt = deepcopy(self.m.status()["checkpoint"])
            receipt.update(last_good=f"p{index}", confirmed_tick=index, state_sha256=state)
            receipt["budget"]["attempts_used"] = index
            self.m.commit(attempt, receipt)
            self.m.observed(f"obs{index}", 1)
        s = self.m.replay()
        self.assertEqual(s["points"], [f"p{i}" for i in range(8)])
        self.assertEqual((s["phase"], s["checkpoint"]["last_good"]), ("CLEANUP", "p7"))

    def test_unencodable_restore_and_bound_backend_refusal_stop_once(self):
        for goal in ("invalid", "refused"):
            self.make(goal, B=0)
            self.reject()

            def broken(claim, state):
                if goal == "invalid":
                    return {"bad": float("nan")}
                return dict(self.restore(claim, state), ok=False, code="CKPT_CORRUPT")

            self.assertEqual(self.m.run_restore(broken), tr.STOP)
            self.assertEqual(self.m.replay()["reason"],
                             "RESTORE_MISMATCH" if goal == "invalid" else "CKPT_CORRUPT")

    def test_process_exit_after_broker_dispatch_keeps_single_restore_claim(self):
        import subprocess

        self.reject()
        self.propose("alt.a1", {"value": 2}, retry=True)
        calls = self.path.parent / "broker-calls"
        run = """
import os
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from rewind.machine import GoalMachine
def interrupted(claim, state):
    with Path(sys.argv[3]).open("x") as witness:
        witness.write(claim)
        witness.flush()
        os.fsync(witness.fileno())
    os._exit(73)
GoalMachine(sys.argv[2], "g").run_restore(interrupted)
"""
        result = subprocess.run([sys.executable, "-I", "-B", "-c", run, str(DIRECT),
                                 str(self.path), str(calls)], timeout=20, check=False)
        self.assertEqual(result.returncode, 73)
        self.reload()
        self.assertEqual(calls.read_text(), "ctl.restore.p0.e2")
        self.refused("UNCERTAIN_OUTCOME", self.m.run_restore, self.restore)
        self.assertEqual(self.m.finish_restore(calls.read_text(), error="RESTORE_TIMEOUT"), tr.STOP)
        self.refused("NO_LEASE", self.propose)

    def test_unresolved_reconcile_escalates_once_before_human_return(self):
        self.propose()
        self.m.uncertain("bad.a1")
        self.assertEqual(self.m.reconcile("bad.a1", "unknown", "readback", 1), "HUMAN_NEEDED")
        self.reload()
        self.refused("NO_LEASE", self.m.reconcile, "bad.a1", "verified", "again", 1)
        self.refused("NO_LEASE", self.propose)
        with Broker(self.path.parent / "handoff") as broker:
            broker.bind("session", self.binding, lease_state="AGENT")
            self.m.human_lease(broker.human_lease("session", "HUMAN", 1, "stuck"))
            self.m.human_lease(broker.human_lease("session", "AGENT", 2, "operator"),
                               observation_id="post-human", generation=1)
        self.assertEqual(self.m.replay()["t"], 2)
        self.assertEqual(self.propose("fixed.a1", {"repair": 1}), "EXECUTING")

    def test_inflight_human_take_preserves_counters_and_fences_old_judgment(self):
        self.propose()
        before = potential(self.m.status())
        with Broker(self.path.parent / "inflight") as broker:
            broker.bind("session", self.binding, lease_state="AGENT")
            self.m.human_lease(broker.human_lease("session", "HUMAN", 1, "operator"))
            self.refused("NO_LEASE", self.m.judge, "bad.a1", "b" * 64, quality(123))
            self.m.human_lease(broker.human_lease("session", "AGENT", 2, "operator"),
                               observation_id="returned", generation=2)
        self.assertEqual(potential(self.m.replay()), before)
        self.refused("NO_LEASE", self.m.judge, "bad.a1", "b" * 64, quality(123))
        self.m.uncertain("bad.a1")
        self.assertEqual(self.m.reconcile("bad.a1", "no_effect", "readback", 2), "READY")
        self.assertEqual(self.m.status()["attempts"], ["bad.a1"])
