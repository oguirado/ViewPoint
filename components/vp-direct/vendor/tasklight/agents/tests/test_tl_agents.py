#!/usr/bin/env python3
"""tl-agents: planning agents against the fake broker (stub mode, no model, no Blender).
RT only under /dev/shm/vqa-cu/ref-tlagents-*; removed after each test."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import protocol as P            # noqa: E402
from planner import Plan        # noqa: E402
from planned_agent import PlannedAgent, PlanMissing, stub_steps   # noqa: E402
from fake_broker import FakeBroker                                 # noqa: E402
import stub_agent               # noqa: E402

schema = P.schema
ROOT = "/dev/shm/vqa-cu"


class Base(unittest.TestCase):
    def setUp(self):
        os.makedirs(ROOT, exist_ok=True)
        self.rt = tempfile.mkdtemp(prefix="ref-tlagents-", dir=ROOT)
        self.addCleanup(shutil.rmtree, self.rt, True)

    def events(self):
        return schema.read_events(os.path.join(self.rt, "events.jsonl"))

    def drive(self, broker, agents, rounds=60):
        """Deterministic lockstep: every agent polls and enqueues, then one broker turn."""
        for _ in range(rounds):
            for a in agents:
                a.poll()
                a.next_intent()
            if broker.step() is None and all(a.finished() for a in agents):
                break
        for a in agents:
            a.poll()

    def agent(self, aid, **kw):
        a = PlannedAgent(aid, self.rt, **kw)
        a.write_plan()
        return a

    def states(self, a):
        return {m["microtask_id"]: m["state"] for m in a.plan.microtasks}


class PlanFirst(Base):
    def test_plan_is_on_disk_before_any_intent(self):
        FakeBroker(self.rt)
        a = PlannedAgent("modeler-agent", self.rt)
        with self.assertRaises(PlanMissing):          # NEGATIVE: act before plan
            a.next_intent()
        self.assertEqual(os.listdir(os.path.join(self.rt, "intents")), [])
        a.write_plan()
        plan = json.load(open(P.plan_path(self.rt, "modeler-agent", "t.modeler-agent.1")))
        self.assertEqual([m["microtask_id"] for m in plan["microtasks"]],
                         ["t.modeler-agent.1.m%d" % k for k in (1, 2, 3)])
        self.assertTrue(all(m["expect"] and m["state"] == "pending" for m in plan["microtasks"]))
        rec = json.loads(open(P.task_path(self.rt, "modeler-agent", "t.modeler-agent.1")).readline())
        self.assertEqual((rec["rec"], rec["op"], len(rec["plan"])), ("task", "open", 3))
        self.assertEqual(schema.fold([rec]).violations, [])

    def test_one_microtask_at_a_time(self):
        FakeBroker(self.rt)
        a = self.agent("modeler-agent")
        r1 = a.next_intent()
        self.assertEqual(r1["tm"]["microtask_id"], "t.modeler-agent.1.m1")
        self.assertIsNone(a.next_intent())            # m1 in flight: nothing else is enqueued
        self.assertEqual(len(os.listdir(os.path.join(self.rt, "intents", "modeler-agent"))), 1)
        with self.assertRaises(ValueError):
            a.plan.mark_queued("t.modeler-agent.1.m2", "modeler-agent-2")

    def test_plan_without_expect_is_refused(self):
        with self.assertRaises(ValueError):
            Plan("modeler-agent", "g", [{"text": "x", "expect": "", "lane": "script", "body": "pass"}])


class HappyPath(Base):
    def test_three_agents_all_verified_and_log_audits_clean(self):
        b = FakeBroker(self.rt)
        ags = [self.agent(x, deps=True) for x in ("modeler-agent", "props-agent", "camera-agent")]
        self.drive(b, ags)
        for a in ags:
            self.assertEqual(a.plan.state, "done", self.states(a))
        ev = self.events()
        model = schema.fold(ev)
        self.assertEqual(model.violations, [])
        self.assertEqual(sum(m["state"] == "verified" for m in model.mt.values()), 8)
        # every queue record of a microtask carries its id
        for r in ev:
            if r["rec"] in ("enqueue", "turn", "close", "txn", "judge"):
                self.assertTrue(schema.MID_RE.match(r.get("microtask_id", "")), r)
        # handoff: props m1 waited for modeler m3
        order = [r["microtask_id"] for r in ev if r["rec"] == "close"]
        self.assertLess(order.index("t.modeler-agent.1.m3"), order.index("t.props-agent.1.m1"))


class Replan(Base):
    def test_rollback_makes_a_new_microtask(self):
        b = FakeBroker(self.rt, {"t.modeler-agent.1.m2": ["JUDGE_REJECT"]})
        a = self.agent("modeler-agent")
        self.drive(b, [a])
        st = self.states(a)
        self.assertEqual(st["t.modeler-agent.1.m2"], "rolled_back")
        self.assertEqual(st["t.modeler-agent.1.m4"], "verified")      # the re-plan
        m4 = a.plan.mt("t.modeler-agent.1.m4")
        self.assertEqual(m4["supersedes"], "t.modeler-agent.1.m2")
        self.assertEqual(st["t.modeler-agent.1.m3"], "verified")      # unblocked by the repair
        self.assertEqual(a.plan.state, "done")
        ev = self.events()
        self.assertEqual(schema.fold(ev).violations, [])
        closes = [(r["microtask_id"], r["verdict"]) for r in ev if r["rec"] == "close"]
        self.assertEqual(closes, [("t.modeler-agent.1.m1", "STATE_VERIFIED"), ("t.modeler-agent.1.m2", "ALL_REJECTED"),
                                  ("t.modeler-agent.1.m4", "STATE_VERIFIED"), ("t.modeler-agent.1.m3", "STATE_VERIFIED")])
        self.assertTrue(any(r["rec"] == "task" and r["op"] == "plan" for r in ev))

    def test_rejection_without_rewind_also_replans(self):
        b = FakeBroker(self.rt, {"t.props-agent.1.m1": ["CLAIM_REFUSED"]}, rewind=False)
        a = self.agent("props-agent")
        self.drive(b, [a])
        st = self.states(a)
        self.assertEqual((st["t.props-agent.1.m1"], st["t.props-agent.1.m3"]), ("rejected", "verified"))
        self.assertEqual(a.plan.state, "done")

    def test_retries_exhausted_blocks_the_task(self):
        pol = {"t.look-agent.1.m%d" % k: ["JOB_ERROR"] for k in (1, 3, 4)}
        b = FakeBroker(self.rt, pol)
        a = self.agent("look-agent")
        self.drive(b, [a])
        st = self.states(a)
        self.assertEqual([st["t.look-agent.1.m%d" % k] for k in (1, 3, 4)], ["rolled_back"] * 3)
        self.assertEqual(st["t.look-agent.1.m2"], "blocked")          # DEP_FAILED, never enqueued
        self.assertEqual(a.plan.state, "blocked")
        self.assertEqual(len(st), 4)                                  # no fifth microtask
        self.assertEqual(len(os.listdir(os.path.join(self.rt, "intents", "look-agent"))), 3)

    def test_stale_frame_requeues_the_same_microtask(self):
        b = FakeBroker(self.rt, {"t.camera-agent.1.m1": ["STALE_FRAME"]})
        a = self.agent("camera-agent", count=1)
        self.drive(b, [a])
        self.assertEqual(self.states(a), {"t.camera-agent.1.m1": "verified"})   # no new microtask
        self.assertEqual(schema.fold(self.events()).mt["t.camera-agent.1.m1"]["drops"], 1)

    def test_director_rewind_rolls_back_verified_and_replans(self):
        b = FakeBroker(self.rt)
        a = self.agent("modeler-agent")
        for _ in range(2):                           # m1 -> p1, m2 -> p2
            a.poll(); a.next_intent(); b.step()
        a.poll()
        self.assertEqual(self.states(a)["t.modeler-agent.1.m2"], "verified")
        b.director_rewind("p1")
        self.drive(b, [a])
        st = self.states(a)
        self.assertEqual(st["t.modeler-agent.1.m1"], "verified")      # commit tick 1 <= tick(p1)
        self.assertEqual(st["t.modeler-agent.1.m2"], "rolled_back")   # rule R
        self.assertEqual(a.plan.mt("t.modeler-agent.1.m4")["supersedes"], "t.modeler-agent.1.m2")
        self.assertEqual(a.plan.state, "done")
        self.assertEqual(schema.fold(self.events()).violations, [])


class Gate(Base):
    """NEGATIVE CONTROLS. test_no_microtask_id_* go red if the MICROTASK_MISSING check is removed
    (see test_mutant_gate_off_dispatches, which shows what the broker does without it)."""

    def bare_intent(self):
        rec = stub_agent.build_intents("modeler-agent", 1, 1)[0]      # a valid F1 intent, no tm
        self.assertEqual(P.contract.validate_intent(rec), [])
        return rec

    def test_no_microtask_id_refused_by_validator(self):
        rec = self.bare_intent()
        self.assertEqual(P.refusal_code(P.validate_planned_intent(rec, newest=1)), "MICROTASK_MISSING")
        rec["tm"] = {"v": 1, "task_id": "t.modeler-agent.1", "text": "x", "after": [], "supersedes": None}
        self.assertEqual(P.refusal_code(P.validate_planned_intent(rec, newest=1)), "MICROTASK_MISSING")

    def test_no_microtask_id_refused_by_broker_no_dispatch(self):
        b = FakeBroker(self.rt)
        self.agent("modeler-agent")                                   # a plan exists; the intent ignores it
        stub_agent.write_intent(self.rt, self.bare_intent())
        c = b.step()
        self.assertEqual((c["rec"], c["verdict"], c["dispatched"]), ("close", "MICROTASK_MISSING", False))
        ev = self.events()
        self.assertEqual([r["rec"] for r in ev if r["rec"] in ("enqueue", "turn", "txn")], [])
        self.assertEqual(b.tick, 0)
        self.assertEqual(P.contract.newest_frame(self.rt), 1)         # no new frame

    def test_mutant_gate_off_dispatches(self):
        b = FakeBroker(self.rt, gate=False)
        stub_agent.write_intent(self.rt, self.bare_intent())
        c = b.step()
        self.assertEqual((c["verdict"], c["dispatched"]), ("STATE_VERIFIED", True))

    def test_unknown_foreign_and_closed_microtasks_refused(self):
        b = FakeBroker(self.rt)
        a = self.agent("modeler-agent")
        good = a.build_intent(a.plan.microtasks[0], 1)
        plan = a.plan.d
        self.assertEqual(P.validate_planned_intent(good, plan=plan, newest=1), [])
        x = json.loads(json.dumps(good)); x["tm"]["microtask_id"] = "t.modeler-agent.1.m9"
        self.assertEqual(P.refusal_code(P.validate_planned_intent(x, plan=plan, newest=1)), "MICROTASK_UNKNOWN")
        x = json.loads(json.dumps(good)); x["tm"].update(task_id="t.props-agent.1", microtask_id="t.props-agent.1.m1")
        self.assertEqual(P.refusal_code(P.validate_planned_intent(x, plan=plan, newest=1)), "TM_FOREIGN_TASK")
        self.assertEqual(P.refusal_code(P.validate_planned_intent(good, plan=None, newest=1)), "PLAN_MISSING")
        x = json.loads(json.dumps(good)); x["write_set"] = ["col:CHAR_props"]
        self.assertEqual(P.refusal_code(P.validate_planned_intent(x, plan=plan, newest=1)), "CONTRACT")
        self.drive(b, [a])                                            # m1 is verified now
        again = dict(good, event_id="modeler-agent-9")
        self.assertEqual(P.refusal_code(P.validate_planned_intent(again, plan=a.plan.d, newest=9)),
                         "MICROTASK_NOT_OPEN")

    def test_base_contract_alone_would_refuse_tm(self):
        a = self.agent("modeler-agent")
        FakeBroker(self.rt)
        good = a.build_intent(a.plan.microtasks[0], 1)
        self.assertTrue(any("tm: unknown field" in e for e in P.contract.validate_intent(good)))


class Cli(Base):
    def test_stub_cli_with_fake_broker_process(self):
        FakeBroker(self.rt)                                           # frame 1 + genesis
        os.remove(os.path.join(self.rt, "events.jsonl"))
        d = os.path.dirname(HERE)
        bp = subprocess.Popen([sys.executable, os.path.join(d, "fake_broker.py"), "--rt", self.rt,
                               "--policy", json.dumps({"t.modeler-agent.1.m1": ["JUDGE_REJECT"]}),
                               "--idle-s", "2", "--max-s", "30"], stdout=subprocess.PIPE)
        ap = subprocess.run([sys.executable, os.path.join(d, "planned_agent.py"), "--agent", "modeler-agent",
                             "--rt", self.rt, "--rewind", "--max-seconds", "25"],
                            stdout=subprocess.PIPE, timeout=40)
        bp.wait(timeout=40)
        lines = [json.loads(x) for x in ap.stdout.decode().splitlines()]
        self.assertEqual(ap.returncode, 0, lines)
        self.assertEqual(lines[0]["type"], "plan")                    # plan is the first thing it says
        res = lines[-1]
        self.assertEqual(res["task_state"], "done")
        self.assertEqual(res["microtasks"]["t.modeler-agent.1.m1"], "rolled_back")
        self.assertEqual(res["microtasks"]["t.modeler-agent.1.m4"], "verified")
        m = schema.fold(self.events())
        self.assertEqual(m.violations, [])
        self.assertEqual(m.tasks["t.modeler-agent.1"]["state"], "done")
        # rw1 probe expect predicates validate under the base contract
        self.assertIsInstance(stub_steps("modeler-agent", rewind=True)[0]["expect"], dict)


class MtTool(Base):
    """The shell tool a real agy agent drives (briefs/microtask_protocol.md)."""

    def mt(self, *args):
        p = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "mt.py")] + list(args)
                           + ["--agent", "props-agent", "--rt", self.rt], stdout=subprocess.PIPE, timeout=30)
        return p.returncode, json.loads(p.stdout.decode().splitlines()[-1])

    def test_plan_next_replan_done(self):
        b = FakeBroker(self.rt, {"t.props-agent.1.m1": ["JUDGE_REJECT"]})
        self.assertEqual(self.mt("next")[1], {"error": "PLAN_MISSING"})          # NEGATIVE: act before plan
        steps = [{k: s[k] for k in ("text", "expect", "lane", "body")} for s in stub_steps("props-agent")]
        sp = os.path.join(self.rt, "steps.json")
        json.dump(steps, open(sp, "w"))
        rc, o = self.mt("plan", "--goal", "sword and shield", "--steps", sp)
        self.assertEqual((rc, len(o["microtasks"])), (0, 2))
        seen = []
        for _ in range(12):
            rc, o = self.mt("next")
            seen.append(o)
            if "task" in o:
                break
            b.step()
        self.assertEqual(o["task"], "done", seen)
        self.assertEqual(o["microtasks"], {"t.props-agent.1.m1": "rolled_back", "t.props-agent.1.m3": "verified",
                                           "t.props-agent.1.m2": "verified"})
        self.assertIn(["replan", "t.props-agent.1.m1", "t.props-agent.1.m3"], [x for s in seen for x in s.get("replans", [])])
        b.step()
        m = schema.fold(self.events())
        self.assertEqual((m.violations, m.tasks["t.props-agent.1"]["state"]), ([], "done"))

    def test_plan_with_foreign_collection_refused(self):
        FakeBroker(self.rt)
        steps = [{"text": "x", "expect": "y", "lane": "script", "body": stub_steps("modeler-agent")[0]["body"]}]
        sp = os.path.join(self.rt, "steps.json")
        json.dump(steps, open(sp, "w"))
        rc, o = self.mt("plan", "--goal", "g", "--steps", sp)
        self.assertEqual((rc, o.get("error")), (2, "PLAN_REFUSED"), o)
        self.assertFalse(os.path.exists(P.plan_path(self.rt, "props-agent", "t.props-agent.1")))


if __name__ == "__main__":
    unittest.main()
