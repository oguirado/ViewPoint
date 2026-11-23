#!/usr/bin/env python3
"""tl-live: projection of task/microtask transitions to OpenSwitchboard and TaskLight (context-wall MCP).

Track P (claude-fable-5-2). The sinks are ref_projection's (OsbSink, TaskLightSink): any object with
`post(agent, act, text, ref=, key=)` / `call(tool, agent, key=, **args)` works (the tests use recorders).

| transition            | osb act (ref = id)        | TaskLight                                                        |
|-----------------------|---------------------------|------------------------------------------------------------------|
| task planned          | claim (task_id)           | post_plan (title, one step per microtask with accept) + status   |
| microtask queued      | claim (microtask_id)      | post_step pending outcome "queued" + post_status                 |
| microtask acting      | ack                       | post_step active                                                 |
| microtask verified    | evidence (microtask_id)   | post_step done + outcome                                         |
| microtask rejected    | refute (microtask_id)     | post_step blocked + outcome (why)                                |
| microtask rolled_back | refute (microtask_id)     | post_step blocked + outcome "rolled back ..." + status           |
| microtask blocked     | refute (microtask_id)     | post_step blocked; post_status blocked                           |
| judge rejects attempt | refute (microtask_id)     | none (state unchanged until the rewind)                          |
| handoff edge ready    | evidence                  | post_status to=<downstream> "tm1 handoff A -> B ready"           |
| re-plan               | claim (new microtask_id)  | post_plan again: same title, full list, new step APPENDED        |
| task done             | verdict (task_id)         | close_task                                                       |
Script sources and keys never leave the machine: only ids, step text, accept text and verdict reasons do.
"""

MAX_STEPS, TEXT_MAX, ACCEPT_MAX, OUTCOME_MAX, TITLE_MAX = 12, 80, 160, 240, 120


def clip(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "…"


def short(mid):
    """'t.props-agent.1.m2' -> 'm2' (the card already names the agent and the task)."""
    return mid.rsplit(".", 1)[1]


class MicrotaskProjector:
    def __init__(self, osb, tasklight, wall_agent=lambda a: a, capsule="Blender"):
        self.osb, self.tl, self.wall_agent, self.capsule = osb, tasklight, wall_agent, capsule
        self.steps = {}     # task_id -> [microtask_id] in posted order (append-only)
        self.n = 0
        self.failed = []

    def _key(self, what):
        self.n += 1
        return "tl-live:%d:%s" % (self.n, what)

    def _osb(self, agent, act, text, ref):
        if not self.osb.post(agent, act, clip(text, 900), ref=ref, key=self._key("osb:" + act)):
            self.failed.append(["osb", agent, act, ref])

    def _tl(self, tool, agent, **args):
        if not self.tl.call(tool, self.wall_agent(agent), key=self._key(tool), **args):
            self.failed.append(["tasklight", agent, tool, args.get("index")])

    def _plan(self, task):
        ids = self.steps.setdefault(task.task_id, [])
        for m in task.microtasks:
            if m.microtask_id not in ids:
                ids.append(m.microtask_id)          # append-only: never remove or reorder
        by = {m.microtask_id: m for m in task.microtasks}
        steps = [{"text": clip("%s %s%s" % (short(i), ("(retry of %s) " % short(by[i].supersedes)) if by[i].supersedes else "",
                                            by[i].text), TEXT_MAX), "accept": clip(by[i].accept, ACCEPT_MAX)}
                 for i in ids[:MAX_STEPS]]
        self._tl("post_plan", task.agent_id, title=clip("[%s] %s" % (task.agent_id, task.title), TITLE_MAX), steps=steps)

    def plan(self, task):
        self._osb(task.agent_id, "claim", "task %s: %s (%d microtasks: %s)" % (
            task.task_id, task.title, len(task.microtasks), ", ".join(short(m.microtask_id) for m in task.microtasks)),
            task.task_id)
        self._plan(task)
        self._tl("post_status", task.agent_id, text=clip("planned %d microtasks" % len(task.microtasks), OUTCOME_MAX))

    def _step(self, task_id, m, state, outcome=None):
        i = self.steps.get(task_id, []).index(m.microtask_id)
        args = {"index": i, "state": state}
        if outcome:
            args["outcome"] = clip(outcome, OUTCOME_MAX)
        self._tl("post_step", m.agent_id, **args)

    def apply(self, tr, board):
        t = tr["t"]
        if t == "task_done":
            task = tr["task"]
            c = {k: len([m for m in task.microtasks if m.state == k]) for k in ("verified", "rejected", "rolled_back", "blocked")}
            line = "done: verified %(verified)d, rejected %(rejected)d, rolled_back %(rolled_back)d, blocked %(blocked)d" % c
            self._osb(task.agent_id, "verdict", "task %s %s" % (task.task_id, line), task.task_id)
            self._tl("close_task", task.agent_id, outcome=clip(line, OUTCOME_MAX))
            return
        m = tr["microtask"]
        task = board.task_of(m)
        if t == "note":
            self._osb(m.agent_id, "refute", "microtask %s: %s" % (m.microtask_id, tr["text"]), m.microtask_id)
            return
        if t == "requeue":
            self._step(task.task_id, m, "pending", tr["text"])
            return
        if t == "handoff":
            d = tr["to"]
            text = "tm1 handoff %s -> %s ready" % (m.microtask_id, d.microtask_id)
            self._osb(m.agent_id, "evidence", text, m.microtask_id)  # not act handoff: it would wait for an ack
            self._tl("post_status", m.agent_id, text=text, to=self.wall_agent(d.agent_id))
            return
        if t == "replan":
            old = tr["old"]
            self._osb(m.agent_id, "claim", "re-plan: microtask %s supersedes %s (%s): %s" % (
                m.microtask_id, old.microtask_id, old.state, m.text), m.microtask_id)
            self._plan(task)
            self._tl("post_status", m.agent_id, text=clip("re-planned: %s supersedes %s" % (short(m.microtask_id), short(old.microtask_id)), OUTCOME_MAX))
            return
        s, why = tr["state"], tr.get("reason") or ""
        k = short(m.microtask_id)
        if s == "queued":
            deps = (" after " + ", ".join(m.after)) if m.after else ""
            self._osb(m.agent_id, "claim", "microtask %s queued%s: %s" % (m.microtask_id, deps, m.text), m.microtask_id)
            self._step(task.task_id, m, "pending", "queued")
            self._tl("post_status", m.agent_id, text="waiting: %s queued" % k)
        elif s == "acting":
            self._osb(m.agent_id, "ack", "microtask %s acting (%s)" % (m.microtask_id, why or "turn"), m.microtask_id)
            self._step(task.task_id, m, "active")
            self._tl("post_status", m.agent_id, text="acting: %s" % k)
        elif s == "verified":
            self._osb(m.agent_id, "evidence", "microtask %s verified: %s" % (m.microtask_id, why), m.microtask_id)
            self._step(task.task_id, m, "done", "verified: %s" % why)
        elif s == "rejected":
            self._osb(m.agent_id, "refute", "microtask %s rejected: %s" % (m.microtask_id, why), m.microtask_id)
            self._step(task.task_id, m, "blocked", "rejected: %s" % why)
        elif s == "rolled_back":
            self._osb(m.agent_id, "refute", "microtask %s %s" % (m.microtask_id, why), m.microtask_id)
            self._step(task.task_id, m, "blocked", why)
            self._tl("post_status", m.agent_id, text="rolled back: %s" % k)
        elif s == "blocked":
            self._osb(m.agent_id, "refute", "microtask %s blocked: %s" % (m.microtask_id, why), m.microtask_id)
            self._step(task.task_id, m, "blocked", "blocked: %s" % why)
            self._tl("post_status", m.agent_id, text="blocked: %s" % k)
