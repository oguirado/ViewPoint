#!/usr/bin/env python3
"""tm1: TASK and MICROTASK as first-class objects of the capsule queue.

Track P, declared Claude work (claude-fable-5-2). Contract: SCHEMA.md (frozen).
Stdlib only. Pure: `fold` reads broker records, `calls` maps them to TaskLight
context-wall calls. Nothing here writes events.jsonl (the broker is its only writer).
"""
import copy
import json
import re
import sys

V = 1
STATES = ("pending", "queued", "acting", "verified", "rejected", "rolled_back", "blocked")
TERMINAL = {"verified", "rejected", "rolled_back"}
FAILED = {"rejected", "rolled_back", "blocked"}
OK_VERDICTS = {"VERIFIED", "STATE_VERIFIED", "VERIFIED_WITH_SPILL"}
REQUEUE = {"STALE_FRAME", "TURN_TIMEOUT"}
UNCERTAIN = {"INPUT_UNCERTAIN", "OUTCOME_UNCERTAIN"}
NOOP = {"DUPLICATE_EVENT", "PASS"}
PARK_DROPS = 3

_AG = r"[a-z0-9][a-z0-9_-]{0,47}"  # no dot inside an agent id: keeps the id grammar unambiguous
TASK_RE = re.compile(r"^t\.(%s)\.([1-9]\d{0,5})$" % _AG)
MID_RE = re.compile(r"^(t\.(%s)\.([1-9]\d{0,5}))\.m([1-9]\d{0,5})$" % _AG)
ATTEMPT_RE = re.compile(r"^(.*)\.a\d+$")
POINT_RE = re.compile(r"^p\d+$")
HANDOFF_RE = re.compile(r"^tm1 handoff (\S+) -> (\S+) ready$")
TEXT_MAX, ACCEPT_MAX, AFTER_MAX = 72, 160, 8
TL_STEPS, TL_STEP_TEXT, TL_OUTCOME, TL_TITLE = 12, 80, 240, 120  # [CITED bridge/room/store.py:72-104]
TL_STEP_STATES = {"pending", "active", "done", "blocked"}


def task_id(agent_id, n=1):
    return "t.%s.%d" % (agent_id, n)


def microtask_id(tid, k):
    return "%s.m%d" % (tid, k)


def parse_microtask_id(mid):
    """-> (task_id, agent_id, n, k) or None."""
    m = MID_RE.match(mid) if isinstance(mid, str) else None
    return (m.group(1), m.group(2), int(m.group(3)), int(m.group(4))) if m else None


def clip(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "…"


def validate_tm(intent):
    """Field-level check of an intent's `tm` object. -> list of (code, detail). No tm = valid."""
    tm = intent.get("tm") if isinstance(intent, dict) else None
    if tm is None:
        return []
    out = []
    if not isinstance(tm, dict) or tm.get("v") != V:
        return [("TM_BAD_FIELD", "tm must be an object with v=1")]
    extra = set(tm) - {"v", "task_id", "microtask_id", "text", "accept", "after", "supersedes"}
    if extra:
        out.append(("TM_BAD_FIELD", "unknown keys %s" % sorted(extra)))
    p = parse_microtask_id(tm.get("microtask_id"))
    if p is None or not TASK_RE.match(str(tm.get("task_id"))):
        return out + [("TM_BAD_ID", "task_id/microtask_id grammar")]
    if p[0] != tm["task_id"]:
        out.append(("TM_BAD_ID", "microtask_id not under task_id"))
    if p[1] != intent.get("agent_id"):
        out.append(("TM_FOREIGN_TASK", "task of %s in an intent of %s" % (p[1], intent.get("agent_id"))))
    text = tm.get("text")
    if not isinstance(text, str) or not (1 <= len(text) <= TEXT_MAX):
        out.append(("TM_BAD_FIELD", "text 1..%d chars" % TEXT_MAX))
    acc = tm.get("accept")
    if acc is not None and (not isinstance(acc, str) or len(acc) > ACCEPT_MAX):
        out.append(("TM_BAD_FIELD", "accept"))
    after = tm.get("after", [])
    if not isinstance(after, list) or len(after) > AFTER_MAX or len(set(map(str, after))) != len(after):
        out.append(("TM_BAD_FIELD", "after"))
    else:
        for a in after:
            if parse_microtask_id(a) is None:
                out.append(("TM_BAD_ID", "after %r" % (a,)))
            elif a == tm["microtask_id"]:
                out.append(("TM_DEP_CYCLE", "self"))
    sup = tm.get("supersedes")
    if sup is not None:
        ps = parse_microtask_id(sup)
        if ps is None:
            out.append(("TM_BAD_ID", "supersedes"))
        elif ps[0] != tm["task_id"] or sup == tm["microtask_id"]:
            out.append(("TM_BAD_SUPERSEDE", "must be another microtask of the same task"))
    return out


def stamp_intent(intent, tid, k, text, accept=None, after=(), supersedes=None):
    """Copy of an F1/rw1 intent carrying the tm object. Raises ValueError on a bad result."""
    out = copy.deepcopy(intent)
    tm = {"v": V, "task_id": tid, "microtask_id": microtask_id(tid, k), "text": text,
          "after": list(after), "supersedes": supersedes}
    if accept is not None:
        tm["accept"] = accept
    out["tm"] = tm
    bad = validate_tm(out)
    if bad:
        raise ValueError("%s: %s" % bad[0])
    return out


class Model:
    """Fold of events.jsonl into tasks and microtasks. JSON-serialisable via `snapshot()`."""

    def __init__(self):
        self.tasks, self.mt = {}, {}
        self.by_event = {}      # event_id -> microtask_id
        self.order = []         # microtask ids in creation order
        self.points = {"p0": 0}
        self.open_txn = None    # microtask_id with a txn begin and no commit/rollback
        self.violations = []
        self.seq = 0
        self.unjoined = 0
        self._handed = set()

    # ---- helpers
    def bad(self, code, detail):
        self.violations.append({"seq": self.seq, "code": code, "detail": clip(detail, 200)})

    def chain_end(self, mid):
        seen = set()
        while self.mt[mid].get("superseded_by") and mid not in seen:
            seen.add(mid)
            mid = self.mt[mid]["superseded_by"]
        return mid

    def satisfied(self, mid):
        return mid in self.mt and self.mt[self.chain_end(mid)]["state"] == "verified"

    def dep_failed(self, mid):
        if mid not in self.mt:
            return False
        end = self.chain_end(mid)
        if self.mt[end]["state"] == "verified":
            return False
        cur = mid
        while True:
            if self.mt[cur]["state"] in FAILED:
                return True
            if cur == end:
                return False
            cur = self.mt[cur]["superseded_by"]

    def open_task_of(self, agent):
        for t in self.tasks.values():
            if t["agent_id"] == agent and not t["closed"]:
                return t
        return None

    def _set(self, mid, state, reason, ch):
        m = self.mt[mid]
        if m["state"] == state:
            return
        frm = m["state"]
        if state == "blocked":
            m["prev_state"] = frm
        m["state"], m["reason"] = state, reason
        m["history"].append([self.seq, state, reason])
        ch.append({"kind": "state", "microtask_id": mid, "from": frm, "to": state, "reason": reason})
        if state in FAILED:
            self._propagate_fail(mid, ch)
        elif state == "verified":
            self._on_verified(mid, ch)

    def _dependents(self, mid):
        return [d for d in self.order if mid in self.mt[d]["after"]]

    def _roots(self, mid):
        """mid and every microtask it (transitively) supersedes."""
        out, cur = [], mid
        while cur and cur in self.mt and cur not in out:
            out.append(cur)
            cur = self.mt[cur].get("supersedes")
        return out

    def _propagate_fail(self, mid, ch):
        for root in self._roots(mid):
            for d in self._dependents(root):
                if self.mt[d]["state"] in ("pending", "queued", "acting"):
                    self._set(d, "blocked", "DEP_FAILED:%s" % root, ch)

    def _on_verified(self, mid, ch):
        for root in self._roots(mid):
            for d in self._dependents(root):
                dm = self.mt[d]
                if dm["state"] == "blocked" and str(dm["reason"]).startswith("DEP_FAILED:") \
                        and not any(self.dep_failed(a) for a in dm["after"]):
                    self._set(d, dm.get("prev_state") or "queued", "DEP_REPAIRED:%s" % mid, ch)
                    self._on_unblocked(d, ch)
                self._handoff(mid, d, ch)

    def _on_unblocked(self, mid, ch):
        for d in self._dependents(mid):
            dm = self.mt[d]
            if dm["state"] == "blocked" and dm["reason"] == "DEP_FAILED:%s" % mid \
                    and not any(self.dep_failed(a) for a in dm["after"]):
                self._set(d, dm.get("prev_state") or "queued", "DEP_REPAIRED:%s" % mid, ch)
                self._on_unblocked(d, ch)

    def _handoff(self, src, dst, ch):
        if self.mt[src]["agent_id"] != self.mt[dst]["agent_id"] and (src, dst) not in self._handed:
            self._handed.add((src, dst))
            ch.append({"kind": "handoff", "from": src, "to": dst,
                       "from_agent": self.mt[src]["agent_id"], "to_agent": self.mt[dst]["agent_id"]})

    def _cycle(self, mid, after):
        stack, seen = list(after), set()
        while stack:
            x = stack.pop()
            if x == mid:
                return True
            if x in seen or x not in self.mt:
                continue
            seen.add(x)
            stack.extend(self.mt[x]["after"])
        return False

    def _task(self, tid, agent, title=None, parent=None, derived=False, ch=None):
        t = self.tasks.get(tid)
        if t is None:
            t = self.tasks[tid] = {"task_id": tid, "agent_id": agent, "title": title or tid, "parent": parent,
                                   "children": [], "microtasks": [], "closed": False, "state": "open",
                                   "outcome": None, "derived": derived, "next_k": 1}
            if parent in self.tasks:
                self.tasks[parent]["children"].append(tid)
            if ch is not None:
                ch.append({"kind": "task_open", "task_id": tid})
        return t

    def _create(self, mid, spec, ch, derived=False):
        """New microtask in `pending`. -> True when created."""
        p = parse_microtask_id(mid)
        if p is None:
            self.bad("TM_BAD_ID", repr(mid))
            return False
        if mid in self.mt:
            self.bad("TM_ID_REUSED", mid)
            return False
        tid, agent = p[0], p[1]
        t = self.tasks.get(tid)
        if t is None or t["closed"]:
            self.bad("TM_TASK_NOT_OPEN", tid)
            return False
        after = [a for a in (spec.get("after") or []) if isinstance(a, str)]
        if mid in after or self._cycle(mid, after):
            self.bad("TM_DEP_CYCLE", mid)
            after = []
        sup = spec.get("supersedes")
        if sup is not None:
            old = self.mt.get(sup)
            if old is None or old["task_id"] != tid or old.get("superseded_by") or old["state"] not in FAILED:
                self.bad("TM_BAD_SUPERSEDE", "%s -> %s" % (mid, sup))
                sup = None
        text = spec.get("text")
        self.mt[mid] = {"microtask_id": mid, "task_id": tid, "agent_id": agent, "k": p[3],
                        "index": len(t["microtasks"]), "text": text if isinstance(text, str) and text else None,
                        "accept": spec.get("accept") if isinstance(spec.get("accept"), str) else None,
                        "after": after, "supersedes": sup, "superseded_by": None, "event_id": None,
                        "state": "pending", "prev_state": None, "reason": None, "drops": 0, "attempts": [],
                        "commit_tick": None, "commit_point": None, "last_rollback": None, "derived": derived,
                        "history": [[self.seq, "pending", None]]}
        t["microtasks"].append(mid)
        t["next_k"] = max(t["next_k"], p[3] + 1)
        self.order.append(mid)
        if sup:
            self.mt[sup]["superseded_by"] = mid
        ch.append({"kind": "created", "microtask_id": mid, "task_id": tid})
        return True

    def _join(self, rec, eid=None):
        """microtask of a record by event_id; a stamped id that disagrees is TM_ID_MISMATCH."""
        eid = rec.get("event_id") if eid is None else eid
        mid = self.by_event.get(eid) if isinstance(eid, str) else None
        st = rec.get("microtask_id")
        if st is not None and mid is not None and st != mid:
            self.bad("TM_ID_MISMATCH", "%s stamped %r, join %s" % (rec.get("rec"), st, mid))
        return mid

    # ---- records
    def feed(self, rec):
        """Apply one broker record. -> list of changes (see SCHEMA.md section 4)."""
        ch = []
        if not isinstance(rec, dict):
            return ch
        s = rec.get("seq")
        self.seq = s if isinstance(s, int) and not isinstance(s, bool) and s > self.seq else self.seq + 1
        fn = getattr(self, "_rec_" + str(rec.get("rec")), None)
        if fn:
            fn(rec, ch)
        return ch

    def _rec_task(self, rec, ch):
        tid, agent, op = rec.get("task_id"), rec.get("agent_id"), rec.get("op")
        m = TASK_RE.match(tid) if isinstance(tid, str) else None
        if not m:
            return self.bad("TM_BAD_ID", repr(tid))
        if m.group(1) != agent:
            return self.bad("TM_FOREIGN_TASK", "%s by %r" % (tid, agent))
        if op == "open":
            if tid in self.tasks:
                return self.bad("TM_ID_REUSED", tid)
            if self.open_task_of(agent):
                return self.bad("TM_TASK_OVERLAP", "%s while %s is open" % (tid, self.open_task_of(agent)["task_id"]))
            parent = rec.get("parent")
            if parent is not None and parent not in self.tasks:
                self.bad("TM_BAD_FIELD", "unknown parent %r" % (parent,))
                parent = None
            self._task(tid, agent, clip(rec.get("title") or tid, 100), parent, ch=ch)
        elif tid not in self.tasks or self.tasks[tid]["closed"]:
            return self.bad("TM_TASK_NOT_OPEN", tid)
        if op in ("open", "plan"):
            for e in rec.get("plan") or []:
                if isinstance(e, dict) and str(e.get("microtask_id", "")).startswith(tid + ".m"):
                    self._create(e.get("microtask_id"), e, ch)
                else:
                    self.bad("TM_BAD_ID", "plan entry outside %s" % tid)
        elif op == "close":
            t = self.tasks[tid]
            t["closed"], t["outcome"] = True, clip(rec.get("outcome") or "", TL_OUTCOME) or None
            t["state"] = "done" if self.task_done(tid) else "failed"
            ch.append({"kind": "task_close", "task_id": tid})
        elif op != "open":
            self.bad("TM_BAD_FIELD", "task op %r" % (op,))

    def _rec_enqueue(self, rec, ch):
        eid, agent = rec.get("event_id"), rec.get("agent_id")
        if not isinstance(eid, str) or not isinstance(agent, str) or eid in self.by_event:
            return
        for code, detail in validate_tm(rec):
            self.bad(code, "%s: %s" % (eid, detail))
        tm = rec.get("tm") if isinstance(rec.get("tm"), dict) else {}
        mid = rec.get("microtask_id", tm.get("microtask_id"))
        if tm.get("microtask_id") not in (None, mid):
            self.bad("TM_ID_MISMATCH", "enqueue stamped %r, tm %r" % (mid, tm.get("microtask_id")))
        derived = mid is None
        if derived:
            if not re.match("^%s$" % _AG, agent):
                return self.bad("TM_BAD_ID", "agent %r" % agent)
            t = self.open_task_of(agent)
            if t is None:
                n = 1 + sum(1 for x in self.tasks.values() if x["agent_id"] == agent)
                t = self._task(task_id(agent, n), agent, derived=True, ch=ch)
            mid = microtask_id(t["task_id"], t["next_k"])
        p = parse_microtask_id(mid)
        if p is None:
            return self.bad("TM_BAD_ID", repr(mid))
        if p[1] != agent:
            return self.bad("TM_FOREIGN_TASK", "%s enqueued by %s" % (mid, agent))
        if p[0] not in self.tasks:
            if self.open_task_of(agent):
                return self.bad("TM_TASK_OVERLAP", p[0])
            self._task(p[0], agent, derived=True, ch=ch)
        if mid not in self.mt:
            if not self._create(mid, tm, ch, derived=derived):
                return
        elif self.mt[mid]["state"] != "pending" or self.mt[mid]["event_id"]:
            return self.bad("TM_ID_REUSED", mid)
        m = self.mt[mid]
        for a in m["after"]:
            if a not in self.mt:
                self.bad("TM_DEP_UNKNOWN", "%s after %s" % (mid, a))
        m["event_id"] = eid
        if m["text"] is None:
            m["text"] = clip("%s %s" % (rec.get("lane", "intent"), rec.get("kind", "")), TEXT_MAX)
        self.by_event[eid] = mid
        self._set(mid, "queued", None, ch)
        bad = [a for a in m["after"] if self.dep_failed(a)]
        if bad:
            self._set(mid, "blocked", "DEP_FAILED:%s" % bad[0], ch)
        for a in m["after"]:
            if self.satisfied(a):
                self._handoff(self.chain_end(a), mid, ch)

    def _rec_auction(self, rec, ch):
        self._join(rec)

    def _rec_cursor(self, rec, ch):
        self._join(rec)

    def _rec_judge(self, rec, ch):
        self._join(rec, self._attempt_event(rec))

    def _start(self, mid, ch, what):
        m = self.mt[mid]
        if m["state"] == "queued":
            if not all(self.satisfied(a) for a in m["after"]):
                self.bad("TM_DEP_ORDER", "%s %s before its dependencies are verified" % (what, mid))
            self._set(mid, "acting", None, ch)
        elif m["state"] != "acting":
            self.bad("TM_BAD_TRANSITION", "%s: %s -> acting" % (mid, m["state"]))

    def _rec_turn(self, rec, ch):
        agent = rec.get("agent_id")
        queued = [x for x in self.order if self.mt[x]["agent_id"] == agent and self.mt[x]["state"] == "queued"]
        st = rec.get("microtask_id")
        if st is not None:
            if st not in self.mt or self.mt[st]["agent_id"] != agent:
                return self.bad("TM_ID_MISMATCH", "turn of %s stamped %r" % (agent, st))
            return self._start(st, ch, "turn")
        # unstamped: the agent's oldest queued microtask; if one is already acting the turn serves that one
        if queued and not any(self.mt[x]["agent_id"] == agent and self.mt[x]["state"] == "acting" for x in self.order):
            self._start(queued[0], ch, "turn")
            self.mt[queued[0]]["turn_guess"] = self.mt[queued[0]]["state"] == "acting"

    def _unguess(self, mid, ch):
        """An unstamped turn was joined to the agent's oldest queued microtask; the broker then served
        another one (`mid`): the guessed one never left the queue."""
        for x in self.order:
            o = self.mt[x]
            if x != mid and o.get("turn_guess") and o["state"] == "acting" and o["agent_id"] == self.mt[mid]["agent_id"]:
                o["state"], o["reason"] = "queued", "turn served %s" % mid
                o["history"].append([self.seq, "queued", o["reason"]])
                ch.append({"kind": "requeue", "microtask_id": x, "from": "acting", "to": "queued", "reason": o["reason"]})
        for x in self.order:
            if self.mt[x]["agent_id"] == self.mt[mid]["agent_id"]:
                self.mt[x]["turn_guess"] = False

    @staticmethod
    def _attempt_event(rec):
        if isinstance(rec.get("event_id"), str):
            return rec["event_id"]
        m = ATTEMPT_RE.match(str(rec.get("attempt", "")))
        return m.group(1) if m else None

    def _rec_txn(self, rec, ch):
        mid = self._join(rec, self._attempt_event(rec))
        if mid is None:
            self.unjoined += 1
            return
        m, phase = self.mt[mid], rec.get("phase")
        if phase == "begin":
            self._unguess(mid, ch)
            self._start(mid, ch, "txn begin")
            m["attempts"].append({"attempt": rec.get("attempt"), "state": "open"})
            self.open_txn = mid
        elif phase in ("commit", "rollback"):
            if m["attempts"]:
                m["attempts"][-1]["state"] = "committed" if phase == "commit" else "rolled_back"
            m["last_rollback"] = None
            if phase == "commit":
                m["commit_tick"], m["commit_point"] = rec.get("tick"), rec.get("point")
            else:
                m["last_rollback"] = {"to": rec.get("base_point"), "reason": rec.get("reason") or rec.get("verdict")}
                ch.append({"kind": "attempt_rolled_back", "microtask_id": mid, "attempt": rec.get("attempt"),
                           "to": rec.get("base_point"), "reason": m["last_rollback"]["reason"]})
            if self.open_txn == mid:
                self.open_txn = None

    def _rec_ckpt(self, rec, ch):
        if isinstance(rec.get("point_id"), str) and isinstance(rec.get("tick"), int):
            self.points[rec["point_id"]] = rec["tick"]

    def _rec_close(self, rec, ch):
        v, eid = rec.get("verdict"), rec.get("event_id")
        if eid is None or v in NOOP:
            return
        mid = self._join(rec)
        if mid is None:
            st = rec.get("microtask_id")
            if st in self.mt and self.mt[st]["state"] == "pending" and not self.mt[st]["event_id"]:
                self._set(st, "rejected", str(v), ch)  # refused before it was ever queued (QUEUE_FULL)
            else:
                self.unjoined += 1
            return
        m = self.mt[mid]
        self._unguess(mid, ch)
        if m["state"] not in ("queued", "acting"):
            if not (m["state"] == "blocked" and v not in OK_VERDICTS):
                self.bad("TM_BAD_TRANSITION", "%s: %s -> close %s" % (mid, m["state"], v))
            return
        drops = rec.get("drops")
        if isinstance(drops, int) and not isinstance(drops, bool):
            m["drops"] = drops
        if v == "ALL_REJECTED" or m["last_rollback"]:
            lr = m["last_rollback"] or {}
            self._set(mid, "rolled_back", "to %s: %s" % (lr.get("to") or "base", lr.get("reason") or v), ch)
        elif v in OK_VERDICTS:
            if m["commit_tick"] is None:
                m["commit_tick"] = rec.get("tick") if isinstance(rec.get("tick"), int) else None
            self._set(mid, "verified", v, ch)
        elif v in REQUEUE:
            if m["drops"] >= PARK_DROPS:
                self._set(mid, "blocked", "PARKED:%s" % v, ch)
            else:
                frm = m["state"]
                m["state"], m["reason"] = "queued", "%s, drops %d" % (v, m["drops"])
                m["history"].append([self.seq, "queued", m["reason"]])
                ch.append({"kind": "requeue", "microtask_id": mid, "from": frm, "to": "queued", "reason": m["reason"]})
        elif v in UNCERTAIN:
            self._set(mid, "blocked", str(v), ch)
        else:
            self._set(mid, "rejected", str(v), ch)

    def _rec_control(self, rec, ch):
        op = rec.get("op")
        if op == "rewind":
            to = rec.get("to")
            if to not in self.points:
                return self.bad("TM_BAD_FIELD", "rewind to unknown point %r" % (to,))
            cut = self.points[to]
            hit = [x for x in self.order if self.mt[x]["state"] == "verified"
                   and isinstance(self.mt[x]["commit_tick"], int) and self.mt[x]["commit_tick"] > cut]
            st = rec.get("microtasks")
            if st is not None and (not isinstance(st, list) or sorted(map(str, st)) != sorted(hit)):
                self.bad("TM_ID_MISMATCH", "rewind stamped %r, rule R gives %r" % (st, hit))
            for x in hit:
                self._set(x, "rolled_back", "to %s: %s" % (to, clip(rec.get("reason") or "rewind", 80)), ch)
        elif op == "stop":
            for x in self.order:
                if self.mt[x]["state"] == "acting":
                    self._set(x, "blocked", "STOPPED", ch)
        elif op == "resume":
            for x in self.order:
                if self.mt[x]["state"] == "blocked" and self.mt[x]["reason"] == "STOPPED":
                    self._set(x, self.mt[x]["prev_state"] or "queued", "RESUMED", ch)
                    self._on_unblocked(x, ch)

    # ---- queries
    def task_done(self, tid):
        t = self.tasks[tid]
        live = [x for x in t["microtasks"] if not self.mt[x]["superseded_by"]]
        return bool(live) and all(self.mt[x]["state"] == "verified" for x in live)

    def task_settled(self, tid):
        return all(self.mt[x]["state"] in TERMINAL or self.mt[x]["superseded_by"] for x in self.tasks[tid]["microtasks"])

    def counts(self, tid):
        c = dict.fromkeys(STATES, 0)
        for x in self.tasks[tid]["microtasks"]:
            c[self.mt[x]["state"]] += 1
        return c

    def snapshot(self):
        return {"v": V, "tasks": self.tasks, "microtasks": self.mt, "violations": self.violations,
                "points": self.points, "open_txn": self.open_txn, "unjoined": self.unjoined}


def fold(records):
    m = Model()
    for r in records:
        m.feed(r)
    return m


def admissible(model, mid):
    """May the broker dispatch this microtask now? -> (ok, code)."""
    m = model.mt.get(mid)
    if m is None:
        return False, "TM_DEP_UNKNOWN"
    if m["state"] not in ("queued", "acting"):
        return False, "TM_BAD_TRANSITION"
    if not all(model.satisfied(a) for a in m["after"]):
        return False, "TM_DEP_UNMET"
    return True, None


def stamp_record(model, rec):
    """Copy of a record with microtask_id/task_id added by the join (what a tm1 broker appends).
    Call it BEFORE model.feed(rec) for turn records, after for enqueue."""
    out, kind = dict(rec), rec.get("rec")
    mid = None
    if kind in ("enqueue", "auction", "close", "cursor"):
        mid = model.by_event.get(rec.get("event_id"))
    elif kind in ("txn", "judge"):
        mid = model.by_event.get(Model._attempt_event(rec))
    elif kind == "turn":
        q = [x for x in model.order if model.mt[x]["agent_id"] == rec.get("agent_id")
             and model.mt[x]["state"] in ("queued", "acting")]
        mid = next((x for x in q if model.mt[x]["state"] == "acting"), q[0] if q else None)
    elif kind == "control" and rec.get("op") == "rewind" and rec.get("to") in model.points:
        cut = model.points[rec["to"]]
        out["microtasks"] = [x for x in model.order if model.mt[x]["state"] == "verified"
                             and isinstance(model.mt[x]["commit_tick"], int) and model.mt[x]["commit_tick"] > cut]
    if mid:
        out["microtask_id"], out["task_id"] = mid, model.mt[mid]["task_id"]
    return out


# ---------------------------------------------------------------- TaskLight mapping (SCHEMA.md section 6)
class Wall:
    """Pure mapping model changes -> context-wall MCP calls. `feed` returns the calls of one record."""

    def __init__(self, room, model=None):
        self.room, self.model = room, model or Model()
        self.status, self.held, self.closed, self.agg = {}, [], set(), {}
        self._n = 0

    def _call(self, tool, agent, **args):
        self._n += 1
        return {"key": "%s.%d" % (self.model.seq, self._n), "tool": tool,
                "arguments": dict(room=self.room, agent=agent, **args)}

    def _step_text(self, m):
        retry = ""
        if m["supersedes"]:
            retry = "(retry of m%d) " % self.model.mt[m["supersedes"]]["k"]
        return clip("m%d %s%s" % (m["k"], retry, m["text"] or "microtask"), TL_STEP_TEXT)

    def plan(self, tid):
        t, mt = self.model.tasks[tid], self.model.mt
        mids = t["microtasks"]
        direct = mids if len(mids) <= TL_STEPS else mids[:TL_STEPS - 1]
        steps = []
        for x in direct:
            st = {"text": self._step_text(mt[x])}
            if mt[x]["accept"]:
                st["accept"] = clip(mt[x]["accept"], ACCEPT_MAX)
            steps.append(st)
        if len(mids) > TL_STEPS:
            steps.append({"text": "+%d more microtasks" % (len(mids) - TL_STEPS + 1)})
        return self._call("post_plan", t["agent_id"], title=clip("[%s] %s" % (t["agent_id"], t["title"]), TL_TITLE),
                          steps=steps)

    def _status(self, agent, text, out):
        text = clip(text, TL_OUTCOME)
        if self.status.get(agent) != text:
            self.status[agent] = text
            out.append(self._call("post_status", agent, text=text))

    def _step(self, m, state, outcome, out):
        t = self.model.tasks[m["task_id"]]
        if len(t["microtasks"]) > TL_STEPS and m["index"] >= TL_STEPS - 1:
            group = [self.model.mt[x] for x in t["microtasks"][TL_STEPS - 1:]]
            if any(g["state"] in ("pending", "queued", "acting") for g in group):
                state = "active"
            elif all(g["state"] == "verified" or g["superseded_by"] for g in group):
                state = "done"
            else:
                state = "blocked"
            outcome = "m%d %s" % (m["k"], outcome or m["state"])
            if self.agg.get(t["task_id"]) == (state, outcome):
                return
            self.agg[t["task_id"]] = (state, outcome)
            index = TL_STEPS - 1
        else:
            index = m["index"]
        args = {"index": index, "state": state}
        if outcome:
            args["outcome"] = clip(outcome, TL_OUTCOME)
        out.append(self._call("post_step", m["agent_id"], **args))

    def _render(self, c, out):
        mt = self.model.mt
        k = c["kind"]
        if k == "handoff":
            out.append(self._call("post_status", c["from_agent"], to=c["to_agent"],
                                  text="tm1 handoff %s -> %s ready" % (c["from"], c["to"])))
            return
        if k == "task_close":
            return self._close(c["task_id"], out)
        if k == "attempt_rolled_back":
            m = mt[c["microtask_id"]]
            return self._status(m["agent_id"], "acting: m%d attempt %s rolled back to %s (%s)" % (
                m["k"], c["attempt"], c["to"], c["reason"]), out)
        if k not in ("state", "requeue"):
            return
        m, to, why = mt[c["microtask_id"]], c["to"], c.get("reason")
        tag = "m%d" % m["k"]
        if k == "requeue":
            self._step(m, "pending", "back to tail: %s" % why, out)
            self._status(m["agent_id"], "waiting: %s back to tail" % tag, out)
        elif to == "queued":
            self._step(m, "pending", "queued" if c["from"] == "pending" else "queued again: %s" % why, out)
            self._status(m["agent_id"], "waiting: %s queued" % tag, out)
        elif to == "acting":
            self._step(m, "active", None, out)
            self._status(m["agent_id"], "acting: %s %s" % (tag, m["text"] or ""), out)
        elif to == "pending":
            self._step(m, "pending", None, out)
        elif to == "verified":
            self._step(m, "done", "verified: %s" % why, out)
            self._status(m["agent_id"], "waiting: %s verified" % tag, out)
        elif to == "rejected":
            self._step(m, "blocked", "rejected: %s" % why, out)
            self._status(m["agent_id"], "blocked: %s rejected (%s)" % (tag, why), out)
        elif to == "rolled_back":
            self._step(m, "blocked", "rolled back %s" % why, out)
            self._status(m["agent_id"], "rolled back: %s" % tag, out)
        elif to == "blocked":
            self._step(m, "blocked", "blocked: %s" % why, out)
            self._status(m["agent_id"], "blocked: %s (%s)" % (tag, why), out)

    def _close(self, tid, out):
        if tid in self.closed or not self.model.tasks[tid]["microtasks"]:
            return
        self.closed.add(tid)
        t, c = self.model.tasks[tid], self.model.counts(tid)
        word = "done" if self.model.task_done(tid) else "failed"
        text = "%s: verified %d, rejected %d, rolled_back %d, blocked %d" % (
            word, c["verified"], c["rejected"], c["rolled_back"], c["blocked"])
        if t["outcome"]:
            text += ". " + t["outcome"]
        out.append(self._call("close_task", t["agent_id"], outcome=clip(text, TL_OUTCOME)))

    def feed(self, rec):
        changes = self.model.feed(rec)
        self._n, out = 0, []
        gate = self.model.open_txn
        if gate is None and self.held:      # txn settled: release what was held, in order
            held, self.held = self.held, []
            for c in held:
                self._render(c, out)
        new_plan = []
        for c in changes:
            if c["kind"] == "created" and c["task_id"] not in new_plan:
                new_plan.append(c["task_id"])
        for tid in new_plan:                 # the plan first: a step index must exist before its post_step
            out.append(self.plan(tid))
        for c in changes:
            mid = c.get("microtask_id")
            if gate is not None and mid == gate and c["kind"] in ("state", "requeue") and c["to"] != "acting":
                self.held.append(c)          # rw1 section 3: no projection of an unsettled txn
            else:
                self._render(c, out)
        return out

    def finalize(self):
        """End of run: close every settled open card. Keys F.<n>."""
        out = []
        for tid, t in self.model.tasks.items():
            if not t["closed"] and self.model.task_settled(tid):
                self._close(tid, out)
        for i, c in enumerate(out):
            c["key"] = "F.%d" % (i + 1)
        return out


def calls(records, room, finalize=True):
    """All TaskLight calls for a log, in order. Pure function of (records, room)."""
    w, out = Wall(room), []
    for r in records:
        out.extend(w.feed(r))
    if finalize:
        out.extend(w.finalize())
    return out


def parse_inbox(events):
    """read_inbox/wait_inbox events -> handoff notices. The text is untrusted: only an exact
    `tm1 handoff A -> B ready` whose sender is the agent inside A is returned, as a wake hint."""
    out = []
    for e in events if isinstance(events, list) else []:
        if not isinstance(e, dict):
            continue
        text = e.get("untrusted_text", e.get("text"))
        m = HANDOFF_RE.match(text) if isinstance(text, str) else None
        if not m:
            continue
        a, b = parse_microtask_id(m.group(1)), parse_microtask_id(m.group(2))
        if a and b and a[1] == e.get("agent") and (e.get("to") in (None, b[1])):
            out.append({"from": m.group(1), "to": m.group(2), "from_agent": a[1], "to_agent": b[1],
                        "lamport": e.get("lamport")})
    return out


def read_events(path, max_line=1 << 20):
    out = []
    with open(path, "rb") as f:
        for line in f:
            if len(line) > max_line or not line.endswith(b"\n"):
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                out.append(r)
    return out


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if len(a) < 2 or a[0] not in ("audit", "calls", "state"):
        sys.stderr.write("usage: schema.py audit|state|calls <events.jsonl> [room]\n")
        return 2
    try:
        recs = read_events(a[1])
    except OSError as ex:
        sys.stderr.write("cannot read %s: %s\n" % (a[1], ex))
        return 2
    if a[0] == "calls":
        for c in calls(recs, a[2] if len(a) > 2 else "demo"):
            print(json.dumps(c, sort_keys=True))
        return 0
    m = fold(recs)
    if a[0] == "state":
        print(json.dumps(m.snapshot(), sort_keys=True))
    else:
        by = dict.fromkeys(STATES, 0)
        for x in m.mt.values():
            by[x["state"]] += 1
        print(json.dumps({"records": len(recs), "tasks": len(m.tasks), "microtasks": len(m.mt), "states": by,
                          "unjoined": m.unjoined, "violations": m.violations}, sort_keys=True))
    return 1 if m.violations else 0


if __name__ == "__main__":
    sys.exit(main())
