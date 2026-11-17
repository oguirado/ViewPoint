#!/usr/bin/env python3
"""Microtask protocol, agent side (lane tl-agents, Track P, claude-fable-5-2).

Schema: tm1 (product/ref_tasklight/model/SCHEMA.md, frozen by tl-model; schema.py is
imported, never redefined). Every PLANNED intent carries the object
  "tm": {"v":1,"task_id":"t.<agent>.<n>","microtask_id":"<task_id>.m<k>","text",...}
tm1 keeps `tm` optional for old intents; this gate is the planning mode: an intent that
does not cite a microtask of a plan on disk is refused.

The base contract (product/ref_agents/contract.py) refuses unknown fields, so validation
is two-step: `tm` here (schema.validate_tm + the plan), the rest with the base validator.
Nothing in ref_agents or model/ is edited.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(HERE, "..", "..", "ref_agents"), os.path.join(HERE, "..", "model")):
    _p = os.path.normpath(_p)
    if _p not in sys.path:
        sys.path.insert(0, _p)
import contract  # noqa: E402  (product/ref_agents)
import schema    # noqa: E402  (product/ref_tasklight/model, tm1)

_BASE_VALIDATE = contract.validate_intent     # kept: run_planned.py patches the module attribute
OPEN = ("pending", "queued")                  # plan states in which an intent may cite the microtask
CODES = ("MICROTASK_MISSING", "PLAN_MISSING", "MICROTASK_UNKNOWN", "MICROTASK_NOT_OPEN",
         "MICROTASK_BUSY", "CONTRACT")        # + the TM_* codes of schema.validate_tm


def strip_tm(obj):
    return {k: v for k, v in obj.items() if k != "tm"}


def validate_planned_intent(obj, plan=None, require_plan=True, claims=None, agent_id=None,
                            event_id=None, newest=None):
    """Errors as "CODE: text" (empty list = admit). plan = decoded RT/plans/<agent>/<task>.json."""
    if not isinstance(obj, dict):
        return ["CONTRACT: record: must be a JSON object"]
    errs = []
    tm = obj.get("tm")
    if not isinstance(tm, dict) or tm.get("microtask_id") is None:   # THE CHECK of the negative control
        errs.append("MICROTASK_MISSING: an intent must cite the microtask it executes (tm.microtask_id)")
    else:
        errs.extend("%s: %s" % cd for cd in schema.validate_tm(obj))
        if not errs:
            if plan is None:
                if require_plan:
                    errs.append("PLAN_MISSING: no plan on disk for task %r" % (tm.get("task_id"),))
            else:
                errs.extend(_against_plan(obj, tm, plan))
    errs.extend("CONTRACT: " + e for e in _BASE_VALIDATE(
        strip_tm(obj), claims=claims, agent_id=agent_id, event_id=event_id, newest=newest))
    return errs


def _against_plan(obj, tm, plan):
    mid = tm["microtask_id"]
    if plan.get("task_id") != tm["task_id"] or plan.get("agent_id") != obj.get("agent_id"):
        return ["TM_FOREIGN_TASK: plan %r/%r does not match the intent" % (plan.get("agent_id"), plan.get("task_id"))]
    mts = plan.get("microtasks") or []
    mt = next((x for x in mts if x.get("microtask_id") == mid), None)
    if mt is None:
        return ["MICROTASK_UNKNOWN: %r is not in the plan (rev %s)" % (mid, plan.get("rev"))]
    if mt.get("state") not in OPEN or mt.get("event_id") not in (None, obj.get("event_id")):
        return ["MICROTASK_NOT_OPEN: %r is %s (event %s)" % (mid, mt.get("state"), mt.get("event_id"))]
    busy = [x["microtask_id"] for x in mts if x is not mt and x.get("state") in ("queued", "acting")]
    if busy:
        return ["MICROTASK_BUSY: %s is still in flight; one microtask at a time" % ", ".join(busy)]
    return []


def refusal_code(errs):
    return errs[0].split(":", 1)[0] if errs else None


def plan_path(rt, agent_id, task_id):
    return os.path.join(rt, "plans", agent_id, task_id + ".json")


def task_path(rt, agent_id, task_id):
    """tm1 task file: one `rec:"task"` record per line (open, plan, close), agent-written."""
    return os.path.join(rt, "tasks", agent_id, task_id + ".json")
