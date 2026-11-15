#!/usr/bin/env python3
"""One agent's task: an ordered list of microtasks, kept in step with the tm1 model.

The fold is model/schema.py (`schema.Model`): the plan never decides a state itself, it
adopts the model's state of its microtasks and reacts:
  rejected / rolled_back -> a NEW microtask (next m<k>, `supersedes` = the failed one)
  blocked (parked, uncertain) or retries exhausted -> the task ends blocked
Files (agent-written, atomic / append-only):
  RT/plans/<agent>/<task_id>.json   working plan: bodies, expects, states, history
  RT/tasks/<agent>/<task_id>.json   tm1 task records (open / plan / close), one per line;
                                    the broker copies them into events.jsonl
"""
import json
import os

import protocol as P

schema = P.schema
MAX_RETRIES = 2      # supersede chain of at most 2 retries per step [TARGET]


class Plan:
    def __init__(self, agent_id, goal, steps, task_no=1, max_retries=MAX_RETRIES, parent=None):
        """steps: [{text, expect, lane, body, accept?, after?, fallbacks?, touches?}]
        body = bpy source (lane script) or key list (lane gui); after = handoff deps
        (microtask ids of OTHER agents); the sequence inside the task is added here."""
        if not steps:
            raise ValueError("a plan needs at least one microtask")
        tid = schema.task_id(agent_id, task_no)
        self.d = {"v": 1, "rec": "plan", "schema": "tm1", "task_id": tid, "agent_id": agent_id,
                  "title": schema.clip(goal, 100), "parent": parent, "state": "planned", "rev": 0,
                  "max_retries": max_retries, "outcome": None, "next_k": 1, "microtasks": []}
        self._out = []           # task records not yet appended to the task file
        self.written = False
        prev = None
        for s in steps:
            if not (isinstance(s.get("expect"), (str, dict)) and s["expect"]):
                raise ValueError("microtask %r has no expect predicate" % (s.get("text"),))
            m = self._new(s, ([prev] if prev else []) + list(s.get("after", ())), None)
            prev = m["microtask_id"]
        self._out.append({"v": 1, "rec": "task", "op": "open", "task_id": tid, "agent_id": agent_id,
                          "title": self.d["title"], "parent": parent,
                          "plan": [self._entry(m) for m in self.microtasks]})

    def _new(self, step, after, supersedes):
        mid = schema.microtask_id(self.d["task_id"], self.d["next_k"])
        self.d["next_k"] += 1
        exp = step["expect"]
        m = {"microtask_id": mid, "text": schema.clip(step["text"], 72),
             "accept": schema.clip(step.get("accept") or (exp if isinstance(exp, str) else "probe equals"), 160),
             "expect": exp, "lane": step["lane"], "body": step["body"],
             "fallbacks": list(step.get("fallbacks", ())), "touches": step.get("touches"),
             "after": after, "supersedes": supersedes, "superseded_by": None,
             "state": "pending", "reason": None, "event_id": None, "history": []}
        if supersedes:
            self.microtasks.insert(self.microtasks.index(self.mt(supersedes)) + 1, m)
        else:
            self.microtasks.append(m)
        return m

    @staticmethod
    def _entry(m):
        return {k: m[k] for k in ("microtask_id", "text", "accept", "after", "supersedes")}

    def tm(self, m):
        """The `tm` object of the intent that executes microtask m."""
        return dict(self._entry(m), v=1, task_id=self.task_id)

    # ---- persistence
    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        self = cls.__new__(cls)
        self.d, self._out, self.written = d, [], True
        return self

    def save(self, rt):
        """Task records first (append), then the plan (atomic). Both before any intent."""
        self.d["rev"] += 1
        tp = P.task_path(rt, self.agent_id, self.task_id)
        os.makedirs(os.path.dirname(tp), exist_ok=True)
        if self._out:
            with open(tp, "a", encoding="utf-8") as fh:
                for r in self._out:
                    fh.write(json.dumps(r, sort_keys=True) + "\n")
            self._out = []
        p = P.plan_path(rt, self.agent_id, self.task_id)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(self.d, fh, sort_keys=True, indent=1)
            fh.write("\n")
        os.replace(p + ".tmp", p)
        self.written = True
        return p

    # ---- views
    agent_id = property(lambda s: s.d["agent_id"])
    task_id = property(lambda s: s.d["task_id"])
    state = property(lambda s: s.d["state"])
    microtasks = property(lambda s: s.d["microtasks"])

    def mt(self, mid):
        return next((m for m in self.microtasks if m["microtask_id"] == mid), None)

    def live(self):
        return [m for m in self.microtasks if m["superseded_by"] is None]

    def in_flight(self):
        return [m for m in self.microtasks if m["state"] in ("queued", "acting")]

    def chain_len(self, m):
        n = 0
        while m["supersedes"]:
            m, n = self.mt(m["supersedes"]), n + 1
        return n

    def next_runnable(self, model):
        """The one microtask that may be enqueued now (None = wait). Its `after` list must be
        satisfied in the model: own predecessors verified, handoffs from other agents verified."""
        if self.in_flight() or self.state in ("done", "blocked"):
            return None
        for m in self.live():
            if m["state"] == "pending":
                return m if all(model.satisfied(a) for a in m["after"]) else None
        return None

    def waiting_on(self, model):
        for m in self.live():
            if m["state"] == "pending":
                return [a for a in m["after"] if not model.satisfied(a)]
        return []

    # ---- transitions
    def _set(self, m, state, reason):
        m["history"].append({"from": m["state"], "to": state, "reason": reason})
        m["state"], m["reason"] = state, reason

    def mark_queued(self, mid, event_id):
        if self.in_flight():
            raise ValueError("MICROTASK_BUSY: one microtask at a time")
        m = self.mt(mid)
        if m["state"] != "pending":
            raise ValueError("MICROTASK_NOT_OPEN: %s is %s" % (mid, m["state"]))
        self._set(m, "queued", "intent written")
        m["event_id"] = event_id
        self.d["state"] = "active"

    def sync(self, model):
        """Adopt the model's states; re-plan. -> [("replan", old, new) | ("blocked", mid, why)
        | ("state", mid, state)]"""
        acts = []
        for m in list(self.microtasks):
            mm = model.mt.get(m["microtask_id"])
            if mm is None or mm["state"] == m["state"]:
                continue
            if m["state"] == "queued" and mm["state"] == "pending":
                continue                        # our intent is written, the broker has not read it yet
            self._set(m, mm["state"], mm.get("reason"))
            acts.append(("state", m["microtask_id"], mm["state"]))
            if mm["state"] in ("rejected", "rolled_back") and m["superseded_by"] is None:
                acts.extend(self.replan(m))
            elif mm["state"] == "blocked" and not str(mm.get("reason") or "").startswith("DEP_FAILED"):
                acts.append(("blocked", m["microtask_id"], mm.get("reason")))
        self._settle()
        return acts

    def replan(self, m):
        """A rejected / rolled-back microtask gets a successor, or the step is given up."""
        j = self.chain_len(m) + 1
        if j > self.d["max_retries"]:
            return [("blocked", m["microtask_id"], "retries exhausted")]
        fb = m["fallbacks"]
        base = m["text"].split(" (retry")[0]
        step = {"text": schema.clip("%s (retry %d)" % (base[:60], j), 72), "accept": m["accept"],
                "expect": m["expect"], "lane": m["lane"], "touches": m["touches"], "fallbacks": fb,
                "body": fb[min(j, len(fb)) - 1] if fb else m["body"]}
        new = self._new(step, list(m["after"]), m["microtask_id"])
        m["superseded_by"] = new["microtask_id"]
        self._out.append({"v": 1, "rec": "task", "op": "plan", "task_id": self.task_id,
                          "agent_id": self.agent_id, "plan": [self._entry(new)]})
        return [("replan", m["microtask_id"], new["microtask_id"])]

    def block(self, m, why):
        self._set(m, "blocked", why)
        self._settle()

    def _settle(self):
        live = self.live()
        if all(m["state"] == "verified" for m in live):
            new = "done"
        elif any(m["state"] in ("queued", "acting") for m in live):
            new = "active"
        else:
            dead = [m for m in live if m["state"] in ("rejected", "rolled_back")
                    or (m["state"] == "blocked" and not str(m["reason"] or "").startswith("DEP_FAILED"))]
            new = "blocked" if dead else self.d["state"]
        self.d["state"] = new
        c = {s: sum(m["state"] == s for m in self.microtasks) for s in ("verified", "rejected", "rolled_back", "blocked")}
        self.d["outcome"] = ("%s: verified %d, rejected %d, rolled_back %d, blocked %d"
                             % ("done" if new == "done" else "failed", c["verified"], c["rejected"],
                                c["rolled_back"], c["blocked"])) if new in ("done", "blocked") else None

    def close(self):
        """tm1 `task close` record; call once, when the agent stops working on the task."""
        self._out.append({"v": 1, "rec": "task", "op": "close", "task_id": self.task_id,
                          "agent_id": self.agent_id, "outcome": self.d["outcome"] or "failed: abandoned"})
        self.d["closed"] = True
