#!/usr/bin/env python3
"""Planning agent (lane tl-agents): plan first, then one microtask at a time.

Loop: write the task + microtask list (RT/plans/...), then repeat
  poll()        fold new events.jsonl records into the plan; re-plan on rejection / rollback
  next_intent() enqueue the ONE runnable microtask as an intent citing task_id + microtask_id
until the task is done or blocked.

Stub mode (this CLI) takes its steps from product/ref_agents/stub_agent.py PLAYBOOK, so it
is deterministic and needs no model. Same arguments as stub_agent.py (--agent --rt --count).
"""
import argparse
import json
import os
import re
import sys
import time

import protocol as P
from planner import Plan

contract = P.contract
import stub_agent  # noqa: E402  (product/ref_agents)

GOALS = {"modeler-agent": "Block out the character body",
         "props-agent": "Give the character a sword, a shield and a crest",
         "look-agent": "Floor, palette and three-point lighting",
         "camera-agent": "Review the character from front, side and three-quarter views"}
# Handoffs between agents: step number -> step lineage ids that must be verified first.
STUB_DEPS = {"props-agent": {1: ["t.modeler-agent.1.m3"]},
             "camera-agent": {1: ["t.modeler-agent.1.m1"]}}
NAME_RE = re.compile(r'(?:cube|ball|cone|obj_for)\("([A-Za-z0-9_.]+)"\s*[,)]')


class PlanMissing(Exception):
    pass


def probe_expect(colname, names):
    """rw1 expect predicate {"probe", "equals"}: the named objects exist in the collection."""
    full = sorted("%s.%s" % (colname, n) for n in names)
    src = ('import bpy, json\n'
           'c = bpy.data.collections.get(%r)\n'
           'have = [o.name for o in c.objects] if c is not None else []\n'
           'print("RW_RESULT " + json.dumps({"present": sorted(n for n in %r if n in have)}))\n'
           % (colname, full))
    return {"probe": src, "equals": {"present": full}}


def stub_steps(agent_id, count=None, claims=None, rewind=False, deps=False):
    claims = contract.DEFAULT_CLAIMS if claims is None else claims
    colname = claims[agent_id][0][4:]
    steps = []
    for i, rec in enumerate(stub_agent.build_intents(agent_id, 0, count, 1, claims, rewind=rewind)):
        script = rec.get("script")
        expect = rec["expect"]
        if rewind and script is not None:
            names = NAME_RE.findall(stub_agent.PLAYBOOK[agent_id][i][0])
            if names:
                pe = probe_expect(colname, names)
                if not contract.check_script(pe["probe"], probe=True):
                    expect = pe
        steps.append({"text": rec["expect"].split(";")[0].rstrip("."), "accept": rec["expect"],
                      "expect": expect, "lane": rec["lane"],
                      "body": script if script is not None else rec["keys"], "touches": rec.get("touches"),
                      "after": STUB_DEPS.get(agent_id, {}).get(i + 1, []) if deps else []})
    return steps


class PlannedAgent:
    def __init__(self, agent_id, rt, steps=None, goal=None, count=None, rewind=False, deps=False):
        self.agent_id, self.rt = agent_id, rt
        self.claims = contract.load_claims(rt)
        self.steps = steps if steps is not None else stub_steps(agent_id, count, self.claims, rewind, deps)
        self.goal = goal or GOALS.get(agent_id, "task")
        self.plan = None
        self.model = P.schema.Model()      # tm1 fold of events.jsonl (own and peers' microtasks)
        self._offset = 0
        self.frozen = False

    # ---- 1. plan
    def write_plan(self):
        self.plan = Plan(self.agent_id, self.goal, self.steps)
        return self.plan.save(self.rt)

    def resume(self):
        """Pick up the plan of an earlier run (restart, or a real agent's next shell call)."""
        self.plan = Plan.load(P.plan_path(self.rt, self.agent_id, P.schema.task_id(self.agent_id, 1)))
        return self.plan

    # ---- 2. read the broker's records
    def poll(self):
        """Feed new complete lines of events.jsonl to the model, then sync the plan with it."""
        try:
            with open(os.path.join(self.rt, "events.jsonl"), "rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
        except OSError:
            chunk = b""
        end = chunk.rfind(b"\n") + 1
        self._offset += end
        for raw in chunk[:end].splitlines():
            try:
                rec = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue                                    # poison line: skipped
            self.model.feed(rec)
            if isinstance(rec, dict) and rec.get("rec") == "control" and rec.get("op") in ("stop", "resume"):
                self.frozen = rec["op"] == "stop"
        if self.plan is None:
            return []
        before = json.dumps(self.plan.d, sort_keys=True)
        acts = self.plan.sync(self.model)
        if json.dumps(self.plan.d, sort_keys=True) != before or self.plan._out:
            self.plan.save(self.rt)
        return acts

    # ---- 3. enqueue one microtask
    def build_intent(self, m, frame):
        rec = {"v": contract.V, "rec": "enqueue",
               "event_id": "%s-%d" % (self.agent_id, stub_agent.next_event_number(self.rt, self.agent_id)),
               "agent_id": self.agent_id, "tm": self.plan.tm(m),
               "based_on_frame": frame, "expect": m["expect"]}
        if m["lane"] == "gui":
            rec.update(lane="gui", kind="key", write_set=[], keys=list(m["body"]))
        else:
            rec.update(lane="script", kind="job", write_set=[self.claims[self.agent_id][0]],
                       script=m["body"])
            if m.get("touches") is not None:
                rec["touches"] = m["touches"]
        return rec

    def next_intent(self):
        """Write the intent of the one runnable microtask. None = nothing to do now."""
        if self.plan is None or not self.plan.written:
            raise PlanMissing("PLAN_MISSING: write the task and its microtasks before any intent")
        if self.frozen:
            return None
        m = self.plan.next_runnable(self.model)
        if m is None:
            return None
        frame = contract.newest_frame(self.rt)
        if frame is None:
            return None
        rec = self.build_intent(m, frame)
        self.plan.mark_queued(m["microtask_id"], rec["event_id"])
        errs = P.validate_planned_intent(rec, plan=self.plan.d, claims=self.claims, newest=frame)
        if errs:
            raise ValueError("own intent refused: %s" % errs)
        self.plan.save(self.rt)                 # plan says "queued" before the intent exists
        stub_agent.write_intent(self.rt, rec)
        return rec

    def finished(self):
        return self.plan is not None and self.plan.state in ("done", "blocked")


def main(argv=None):
    ap = argparse.ArgumentParser(description="planning stub agent (tasks + microtasks)")
    ap.add_argument("--agent", required=True, choices=contract.AGENT_IDS)
    ap.add_argument("--rt", default=None)
    ap.add_argument("--count", type=int, default=None, help="number of microtasks to plan")
    ap.add_argument("--rewind", action="store_true", help="rw1 fields: touches + probe expects")
    ap.add_argument("--deps", action="store_true", help="plan the cross-agent handoffs (STUB_DEPS)")
    ap.add_argument("--wait-frame-s", type=float, default=5.0)
    ap.add_argument("--max-seconds", type=float, default=60.0)
    ap.add_argument("--dep-wait-s", type=float, default=30.0)
    ap.add_argument("--poll-ms", type=int, default=50)
    a = ap.parse_args(argv)
    rt = contract.rt_root(a.rt)

    def say(**kw):
        kw["agent_id"] = a.agent
        print(json.dumps(kw, sort_keys=True), flush=True)

    t0 = time.time()
    while contract.newest_frame(rt) is None and time.time() - t0 < a.wait_frame_s:
        time.sleep(0.1)
    if contract.newest_frame(rt) is None:
        say(type="error", error="no frame in RT/frames")
        return 3
    ag = PlannedAgent(a.agent, rt, count=a.count, rewind=a.rewind, deps=a.deps)
    path = ag.write_plan()
    say(type="plan", task_id=ag.plan.task_id, path=path,
        microtasks=[m["microtask_id"] for m in ag.plan.microtasks])
    deadline = time.time() + a.max_seconds
    dep_since = None
    while not ag.finished() and time.time() < deadline:
        for act in ag.poll():
            say(type="plan_" + act[0], detail=list(act[1:]))
        rec = ag.next_intent()
        if rec is not None:
            say(type="stub_intent", event_id=rec["event_id"], microtask_id=rec["tm"]["microtask_id"],
                based_on_frame=rec["based_on_frame"])
        waiting = ag.plan.waiting_on(ag.model)
        if waiting and not ag.plan.in_flight():
            dep_since = dep_since or time.time()
            if time.time() - dep_since > a.dep_wait_s:
                m = next(x for x in ag.plan.live() if x["state"] == "pending")
                ag.plan.block(m, "DEP_TIMEOUT:%s" % ",".join(waiting))
                ag.plan.d["state"] = "blocked"
                ag.plan.save(rt)
        else:
            dep_since = None
        time.sleep(a.poll_ms / 1000.0)
    ag.poll()
    ag.plan.close()
    ag.plan.save(rt)
    st = ag.plan.state
    say(type="result", is_error=st != "done", task_state=st, outcome=ag.plan.d["outcome"],
        microtasks={m["microtask_id"]: m["state"] for m in ag.plan.microtasks})
    return 0 if st == "done" else (4 if st == "blocked" else 5)


if __name__ == "__main__":
    sys.exit(main())
