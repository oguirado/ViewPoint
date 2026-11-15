#!/usr/bin/env python3
"""tl-live: tasks and microtasks of the live proof (schema tm1), folded from the broker's events.jsonl.

Track P (claude-fable-5-2). Stdlib only. Follows product/ref_tasklight/model/SCHEMA.md (tm1, frozen):
task_id 't.<agent_id>.<n>', microtask_id '<task_id>.m<k>' (never reused), one microtask = one event_id,
intent object `tm{v,task_id,microtask_id,text,accept,after,supersedes}`, a retry is a NEW microtask with
`supersedes`. This file is the live lane's self-contained fold of the subset of tm1 the proof exercises
(tl-model's schema.py did not exist when the lane started); the reference brokers do not stamp
microtask_id on turn/close/txn/judge, so those records are joined by event_id (tm1 section 3, join column).

A microtask changes state ONLY from a broker record:
  enqueue(event_id)                                  -> queued
  txn begin(event_id)                                -> acting
  judge(attempt=<event_id>.aN, pick=false)           -> no state change (note: attempt rejected)
  control rewind(attempt=..., digest_match=true)     -> rolled_back  (proven=True)
  close VERIFIED / STATE_VERIFIED / ..._WITH_SPILL   -> verified
  close ALL_REJECTED                                 -> rolled_back; without a digest-proven rewind it is
                                                        recorded as violation ROLLBACK_UNPROVEN
  close STALE_FRAME / TURN_TIMEOUT                   -> back to queued (drops < 3), else blocked
  close INPUT_UNCERTAIN / OUTCOME_UNCERTAIN          -> blocked
  close anything else                                -> rejected
A rejected / rolled_back / blocked microtask with a remaining plan is retried as the next m<k> (supersedes).
"""
import json
import os

STATES = ("pending", "queued", "acting", "verified", "rejected", "rolled_back", "blocked")
VERIFIED = {"VERIFIED", "VERIFIED_WITH_SPILL", "STATE_VERIFIED"}
NOT_DISPATCHED = {"STALE_FRAME", "TURN_TIMEOUT"}
UNCERTAIN = {"INPUT_UNCERTAIN", "OUTCOME_UNCERTAIN"}
TERMINAL = {"verified", "rejected", "rolled_back", "blocked"}
ALLOWED = {
    "pending": {"queued", "blocked"},
    "queued": {"acting", "rejected", "blocked", "queued"},
    "acting": {"verified", "rejected", "rolled_back", "blocked"},
    "rejected": set(), "rolled_back": set(), "verified": set(), "blocked": set(),
}


def event_id_of(microtask_id):
    """Broker event ids allow [A-Za-z0-9.-] (no '_'), <= 80 chars; the microtask id is used as is."""
    return microtask_id


class Microtask:
    def __init__(self, microtask_id, task_id, agent_id, text, accept, intent, after=(), supersedes=None, plans=()):
        self.microtask_id, self.task_id, self.agent_id = microtask_id, task_id, agent_id
        self.text, self.accept, self.intent = text, accept, dict(intent)
        self.after, self.supersedes = list(after), supersedes
        self.plans = list(plans)          # remaining re-plan intents (deterministic stub knowledge)
        self.state, self.history, self.reason = "pending", ["pending"], None
        self.event_id, self.superseded_by, self.closed = event_id_of(microtask_id), None, False
        self.proven, self.drops = False, 0

    def to_json(self):
        return {"microtask_id": self.microtask_id, "task_id": self.task_id, "agent_id": self.agent_id,
                "text": self.text, "accept": self.accept, "state": self.state, "history": self.history,
                "reason": self.reason, "event_id": self.event_id, "after": self.after,
                "supersedes": self.supersedes, "superseded_by": self.superseded_by, "rollback_proven": self.proven}


class Task:
    def __init__(self, agent_id, k, title):
        self.agent_id, self.task_id, self.title = agent_id, "t.%s.%d" % (agent_id, k), title
        self.microtasks, self.closed = [], False

    def add(self, text, accept, intent, after=(), plans=(), supersedes=None):
        m = Microtask("%s.m%d" % (self.task_id, 1 + len(self.microtasks)), self.task_id, self.agent_id, text, accept,
                      intent, after, supersedes, plans)
        self.microtasks.append(m)
        return m

    def done(self):
        """Every supersede chain ends verified (tm1 section 5)."""
        return bool(self.microtasks) and all(m.state == "verified" for m in self.microtasks if m.superseded_by is None)

    def stuck(self):
        return any(m.state in TERMINAL and m.state != "verified" and m.superseded_by is None and m.closed and not m.plans
                   for m in self.microtasks)

    def to_json(self):
        return {"task_id": self.task_id, "agent_id": self.agent_id, "title": self.title, "done": self.done(),
                "microtasks": [m.to_json() for m in self.microtasks]}


class Board:
    """All tasks of the run + the fold of broker records into microtask transitions."""

    def __init__(self, tasks, require_digest_proof=True):
        self.tasks = list(tasks)
        self.require_digest_proof = require_digest_proof   # False only in the negative control
        self.violations = []

    def all(self):
        return [m for t in self.tasks for m in t.microtasks]

    def by_event(self, eid):
        return next((m for m in self.all() if m.event_id == eid), None)

    def task_of(self, m):
        return next(t for t in self.tasks if t.task_id == m.task_id)

    def by_id(self, mid):
        return next((m for m in self.all() if m.microtask_id == mid), None)

    def satisfied(self, mid):
        """tm1 section 5: X is satisfied when X, or the newest microtask of its supersede chain, is verified."""
        m = self.by_id(mid)
        while m is not None and m.superseded_by:
            m = self.by_id(m.superseded_by)
        return m is not None and m.state == "verified"

    def _move(self, m, state, reason=None):
        if state not in ALLOWED[m.state]:
            self.violations.append([m.microtask_id, m.state, state])
            return None
        m.state, m.reason = state, reason if reason is not None else m.reason
        m.history.append(state)
        return {"t": "state", "microtask": m, "state": state, "reason": reason}

    def feed(self, rec):
        """One broker record -> list of transitions ({"t": "state"|"replan"|"task_done", ...})."""
        out, kind = [], rec.get("rec")
        if kind == "enqueue":
            m = self.by_event(rec.get("event_id"))
            if m and m.state == "pending":
                if (rec.get("tm") or {}).get("microtask_id") != m.microtask_id:
                    self.violations.append([m.microtask_id, "TM_ID_MISMATCH", (rec.get("tm") or {}).get("microtask_id")])
                out.append(self._move(m, "queued"))
        elif kind == "txn" and rec.get("phase") == "begin":
            m = self.by_event(rec.get("event_id"))
            if m and m.state == "queued":
                out.append(self._move(m, "acting", rec.get("attempt")))
        elif kind == "judge" and rec.get("pick") is False:
            m = self.by_event(str(rec.get("attempt", "")).rsplit(".a", 1)[0])
            if m and m.state == "acting":
                why = ", ".join("%s: %s" % (o.get("name"), o.get("detail")) for o in rec.get("oracles", []) if not o.get("ok"))
                m.reason = "judge rejected %s (%s)" % (rec.get("attempt"), why)
                out.append({"t": "note", "microtask": m, "text": m.reason})
        elif kind == "control" and rec.get("op") == "rewind":
            m = self.by_event(str(rec.get("attempt", "")).rsplit(".a", 1)[0])
            proven = rec.get("digest_match") is True or not self.require_digest_proof
            if m and m.state == "acting" and proven:
                m.proven = True
                out.append(self._move(m, "rolled_back", "rolled back to %s: %s" % (rec.get("to"), m.reason or rec.get("reason"))))
        elif kind == "close" and rec.get("event_id") and not rec.get("ingest"):
            m = self.by_event(rec["event_id"])
            if m and not m.closed:
                v = rec.get("verdict")
                if v in NOT_DISPATCHED and rec.get("drops", 0) < 3:
                    m.drops = rec.get("drops", 0)
                    return [{"t": "requeue", "microtask": m, "text": "back to tail: %s, drops %s" % (v, m.drops)}]
                m.closed = True
                if v in VERIFIED:
                    if m.state == "queued":
                        out.append(self._move(m, "acting"))
                    out.append(self._move(m, "verified", v))
                elif v == "ALL_REJECTED":
                    if m.state != "rolled_back":
                        self.violations.append([m.microtask_id, "ROLLBACK_UNPROVEN", rec.get("seq")])
                        out.append(self._move(m, "rolled_back", "rolled back: ALL_REJECTED (no digest-proven rewind on record)"))
                elif v in NOT_DISPATCHED or v in UNCERTAIN:
                    out.append(self._move(m, "blocked", "%s: %s" % (v, rec.get("reason"))))
                else:
                    out.append(self._move(m, "rejected", "%s: %s" % (v, rec.get("reason"))))
                out += self._after_close(m)
        elif kind == "close" and rec.get("ingest") and rec.get("event_id"):
            m = self.by_event(rec["event_id"])
            if m and m.state == "pending":
                m.closed = True
                out.append(self._move(m, "blocked", "%s: %s" % (rec.get("verdict"), rec.get("reason"))))
        return [x for x in out if x]

    def _after_close(self, m):
        out, t = [], self.task_of(m)
        if m.state in ("rejected", "rolled_back", "blocked") and m.plans:   # RETRY: a new microtask, appended
            p = m.plans[0]
            new = t.add(p["text"], p["accept"], p["intent"], m.after, m.plans[1:], supersedes=m.microtask_id)
            m.superseded_by = new.microtask_id
            out.append({"t": "replan", "microtask": new, "old": m})
        if m.state == "verified":
            for d in self.all():                         # handoff edges that just became satisfied
                if d.agent_id != m.agent_id and d.state == "pending" and any(
                        self._chain_has(x, m) for x in d.after) and all(self.satisfied(x) for x in d.after):
                    out.append({"t": "handoff", "microtask": m, "to": d})
        if t.done() and not t.closed:
            t.closed = True
            out.append({"t": "task_done", "task": t})
        return out

    def _chain_has(self, mid, m):
        x = self.by_id(mid)
        while x is not None:
            if x is m:
                return True
            x = self.by_id(x.superseded_by) if x.superseded_by else None
        return False

    def next_intents(self, frame):
        """INTENT phase of the stub agents: per agent, the first pending microtask whose dependencies hold and
        with nothing of that agent in flight. Returns [(microtask, intent dict)] (contract intent + ids)."""
        out = []
        for t in self.tasks:
            if any(m.state in ("queued", "acting") or (m.state == "pending" and getattr(m, "sent", False))
                   for m in t.microtasks):
                continue
            for m in t.microtasks:
                if m.state == "pending" and all(self.satisfied(d) for d in m.after):
                    it = {"v": 1, "rec": "enqueue", "event_id": m.event_id, "agent_id": m.agent_id,
                          "based_on_frame": frame, "expect": m.accept}
                    it.update(m.intent)
                    it["tm"] = {"v": 1, "task_id": m.task_id, "microtask_id": m.microtask_id, "text": m.text[:72],
                                "accept": m.accept[:160], "after": m.after, "supersedes": m.supersedes}
                    m.sent = True
                    out.append((m, it))
                    break
        return out

    def to_json(self):
        return {"tasks": [t.to_json() for t in self.tasks], "violations": self.violations}


def write_intent(rt, intent):
    """Contract path RT/intents/<agent_id>/<event_id>.json, atomic."""
    d = os.path.join(rt, "intents", intent["agent_id"])
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, intent["event_id"] + ".tmp")
    with open(tmp, "w") as f:
        json.dump(intent, f)
    os.replace(tmp, os.path.join(d, intent["event_id"] + ".json"))
