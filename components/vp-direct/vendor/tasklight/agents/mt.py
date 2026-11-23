#!/usr/bin/env python3
"""Shell tool a real (agy) agent uses to follow the microtask protocol (lane tl-agents).
  mt.py plan   --agent A --rt RT --goal TEXT --steps steps.json
  mt.py next   --agent A --rt RT        poll + enqueue the one runnable microtask
  mt.py revise --agent A --rt RT --microtask ID --body-file F     (pending microtasks only)
  mt.py show   --agent A --rt RT
Each call prints one JSON line. State lives in RT/plans and RT/tasks, so calls are independent."""
import argparse
import json
import os
import sys

import protocol as P
from planned_agent import PlannedAgent

contract = P.contract


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("plan", "next", "revise", "show"))
    ap.add_argument("--agent", required=True, choices=contract.AGENT_IDS)
    ap.add_argument("--rt", default=None)
    ap.add_argument("--goal")
    ap.add_argument("--steps")
    ap.add_argument("--microtask")
    ap.add_argument("--body-file")
    a = ap.parse_args(argv)
    rt = contract.rt_root(a.rt)
    out = lambda **kw: print(json.dumps(kw, sort_keys=True)) or 0     # noqa: E731
    pp = P.plan_path(rt, a.agent, P.schema.task_id(a.agent, 1))
    if a.cmd == "plan":
        if os.path.exists(pp):
            return out(error="PLAN_EXISTS", path=pp) or 2
        with open(a.steps, encoding="utf-8") as fh:
            steps = json.load(fh)
        ag = PlannedAgent(a.agent, rt, steps=steps, goal=a.goal)
        bad = []
        ag.write_plan()
        for m in ag.plan.microtasks:
            rec = ag.build_intent(m, contract.newest_frame(rt) or 0)
            bad += ["%s: %s" % (m["microtask_id"], e) for e in contract.validate_intent(P.strip_tm(rec), claims=ag.claims)
                    if not e.startswith("event_id")]
            if m["lane"] == "script":
                bad += ["%s: foreign collection %s" % (m["microtask_id"], f)
                        for f in (contract.foreign_collections(m["body"], a.agent, ag.claims) or [])]
        if bad:
            os.remove(pp)
            os.remove(P.task_path(rt, a.agent, ag.plan.task_id))
            return out(error="PLAN_REFUSED", errors=bad) or 2
        return out(task_id=ag.plan.task_id, microtasks=[m["microtask_id"] for m in ag.plan.microtasks])
    if not os.path.exists(pp):
        return out(error="PLAN_MISSING") or 2
    ag = PlannedAgent(a.agent, rt, steps=[])
    ag.resume()
    acts = ag.poll()
    st = {m["microtask_id"]: m["state"] for m in ag.plan.microtasks}
    if a.cmd == "show":
        return out(task=ag.plan.state, microtasks=st)
    if a.cmd == "revise":
        m = ag.plan.mt(a.microtask)
        if m is None or m["state"] != "pending":
            return out(error="MICROTASK_NOT_OPEN") or 2
        with open(a.body_file, encoding="utf-8") as fh:
            m["body"] = fh.read()
        ag.plan.save(rt)
        return out(revised=a.microtask)
    if ag.finished():
        if not ag.plan.d.get("closed"):
            ag.plan.close()
            ag.plan.save(rt)
        return out(task=ag.plan.state, outcome=ag.plan.d["outcome"], microtasks=st)
    rec = ag.next_intent()
    if rec is not None:
        return out(enqueued=rec["tm"]["microtask_id"], event_id=rec["event_id"], replans=acts, microtasks=st)
    return out(wait="in_flight" if ag.plan.in_flight() else "handoff",
               waiting_on=ag.plan.waiting_on(ag.model), replans=acts, microtasks=st)


if __name__ == "__main__":
    sys.exit(main())
