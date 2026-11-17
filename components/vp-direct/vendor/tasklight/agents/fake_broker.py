#!/usr/bin/env python3
"""TEST DOUBLE of the capsule broker (lane tl-agents). Not the broker: no Blender, no
bridge, no judge. It turns intent files into the record sequences the F1 broker and the
rw1 rewind contract define, each carrying task_id / microtask_id, with verdicts scripted
by `policy`. Its only real logic is the planning gate (protocol.validate_planned_intent).

policy: {"<microtask_id>": ["JUDGE_REJECT"]}: the scripted verdict of that microtask
(one microtask = one event, tm1); a microtask not named is verified.
"""
import argparse
import hashlib
import json
import os
import sys
import time

import protocol as P

contract = P.contract
V_OK = P.schema.OK_VERDICTS
REQUEUE = P.schema.REQUEUE
V_ROLLBACK = ("ALL_REJECTED", "JUDGE_REJECT")


class FakeBroker:
    def __init__(self, rt, policy=None, gate=True, rewind=True, agents=contract.AGENT_IDS):
        self.rt, self.gate, self.rewind = rt, gate, rewind
        self.policy = {k: list(v) for k, v in (policy or {}).items()}
        self.agents = list(agents)
        self.seq = self.tick = self.turn = 0
        self.scene_epoch = 1
        self.points = 0
        self.last_good = "p0"
        self.done = set()
        self.task_off = {}
        self.drops = {}
        self.enq = set()
        self.ring = 0
        for d in ("intents", "frames"):
            os.makedirs(os.path.join(rt, d), exist_ok=True)
        self.claims = contract.load_claims(rt)
        self.frame = contract.newest_frame(rt) or 0
        if not self.frame:
            self._frame(None)
        self._append({"rec": "ckpt", "point_id": "p0", "tick": 0, "scene_epoch": 1, "frame": self.frame})

    def _append(self, rec):
        self.seq += 1
        rec = dict(rec, v=1, seq=self.seq, ts_ms=int(time.time() * 1000))
        with open(os.path.join(self.rt, "events.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        return rec

    def _frame(self, by_event):
        self.frame += 1
        p = os.path.join(self.rt, "frames", "frame_%d.json" % self.frame)
        with open(p + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"v": 1, "frame": self.frame, "png": "", "sha256": "", "synthetic": True,
                       "ts_ms": int(time.time() * 1000), "changed_regions": [], "by_event": by_event}, fh)
        os.replace(p + ".tmp", p)

    def _pending(self, agent_id):
        d = os.path.join(self.rt, "intents", agent_id)
        try:
            names = [n for n in os.listdir(d) if n.endswith(".json")]
        except OSError:
            return []
        names = [n for n in names if (agent_id, n) not in self.done]

        def num(n):
            m = contract.EVENT_ID_RE.match(n[:-5])
            return int(m.group("n")) if m else 10 ** 9
        return sorted(names, key=num)

    def copy_tasks(self):
        """tm1: the broker copies new task-file records (open / plan / close) into the log."""
        root = os.path.join(self.rt, "tasks")
        for aid in self.agents:
            d = os.path.join(root, aid)
            for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
                p = os.path.join(d, n)
                with open(p, "rb") as fh:
                    fh.seek(self.task_off.get(p, 0))
                    chunk = fh.read()
                end = chunk.rfind(b"\n") + 1
                self.task_off[p] = self.task_off.get(p, 0) + end
                for raw in chunk[:end].splitlines():
                    try:
                        rec = json.loads(raw.decode("utf-8"))
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and rec.get("rec") == "task" and rec.get("agent_id") == aid:
                        self._append(rec)

    def step(self):
        self.copy_tasks()
        return self._turn()

    def _turn(self):
        """One turn: the next agent in the ring that has a queued intent. Returns the close
        record, or None when no agent has anything queued (PASS all round)."""
        for i in range(len(self.agents)):
            aid = self.agents[(self.ring + i) % len(self.agents)]
            names = self._pending(aid)
            if names:
                self.ring = (self.ring + i + 1) % len(self.agents)
                self.done.add((aid, names[0]))
                return self._serve(aid, names[0])
        return None

    def _serve(self, aid, name):
        path = os.path.join(self.rt, "intents", aid, name)
        try:
            with open(path, encoding="utf-8") as fh:
                obj = json.load(fh)
        except (OSError, ValueError) as e:
            return self._append({"rec": "close", "event_id": name[:-5], "agent_id": aid,
                                 "verdict": "CONTRACT_REFUSED", "reason": str(e)[:200], "dispatched": False})
        tm = obj.get("tm") if isinstance(obj, dict) and isinstance(obj.get("tm"), dict) else {}
        ids = {k: tm[k] for k in ("task_id", "microtask_id") if isinstance(tm.get(k), str)}
        eid = name[:-5]
        if self.gate:                                    # THE GATE (negative control target)
            plan = None
            tid = ids.get("task_id")
            if isinstance(tid, str) and P.schema.TASK_RE.match(tid):
                try:
                    with open(P.plan_path(self.rt, aid, tid), encoding="utf-8") as fh:
                        plan = json.load(fh)
                except (OSError, ValueError):
                    plan = None
            errs = P.validate_planned_intent(obj, plan=plan, claims=self.claims, agent_id=aid,
                                             event_id=eid, newest=self.frame)
            if errs:
                code = P.refusal_code(errs)
                return self._append(dict(ids, rec="close", event_id=eid, agent_id=aid, dispatched=False,
                                         verdict="CONTRACT_REFUSED" if code == "CONTRACT" else code,
                                         reason=errs[0][:300]))
        mid = ids.get("microtask_id")
        lane = obj.get("lane")
        if eid not in self.enq:
            self.enq.add(eid)
            self._append(dict(ids, rec="enqueue", tm=tm, event_id=eid, agent_id=aid, lane=lane, kind=obj.get("kind"),
                          based_on_frame=obj.get("based_on_frame"), write_set=obj.get("write_set"),
                          script_sha256=hashlib.sha256(str(obj.get("script", "")).encode()).hexdigest()))
        self.turn += 1
        self._append(dict(ids, rec="turn", turn=self.turn, turn_epoch=self.turn, agent_id=aid,
                          event_id=eid, head_frame=self.frame, hold_ms=60000))
        q = self.policy.get(mid) or []
        ok = "VERIFIED" if lane == "gui" else "STATE_VERIFIED"
        v = q.pop(0) if q else ok
        if v in REQUEUE:                                 # back to the tail: same event, served again
            self.done.discard((aid, name))
            self.drops[eid] = self.drops.get(eid, 0) + 1
            return self._append(dict(ids, rec="close", event_id=eid, agent_id=aid, verdict=v,
                                     turn=self.turn, dispatched=False, drops=self.drops[eid]))
        if v not in V_OK and v not in V_ROLLBACK and v != "JOB_ERROR":
            return self._append(dict(ids, rec="close", event_id=eid, agent_id=aid, verdict=v,
                                     turn=self.turn, dispatched=False, reason="scripted by policy"))
        if not self.rewind:                              # F1-only shape: no txn records
            if v in V_OK:
                self.tick += 1
                self._frame(eid)
            return self._append(dict(ids, rec="close", event_id=eid, agent_id=aid, verdict=v,
                                     turn=self.turn, tick=self.tick, dispatched=True))
        att = eid + ".a1"
        self._append(dict(ids, rec="txn", phase="begin", event_id=eid, attempt=att,
                          base_point=self.last_good, verdict=None, reason=None))
        if v in V_OK:
            self._append(dict(ids, rec="judge", attempt=att, pick=True, judge_version="fake",
                              oracles=[{"name": "expect", "ok": True, "detail": "scripted"}]))
            self.tick += 1
            self.points += 1
            pt = "p%d" % self.points
            self._frame(eid)
            self._append({"rec": "ckpt", "point_id": pt, "tick": self.tick,
                          "scene_epoch": self.scene_epoch, "frame": self.frame})
            self._append(dict(ids, rec="txn", phase="commit", event_id=eid, attempt=att,
                              base_point=self.last_good, verdict=v, reason=None, point=pt, tick=self.tick))
            self.last_good = pt
            return self._append(dict(ids, rec="close", event_id=eid, agent_id=aid, verdict=v,
                                     turn=self.turn, tick=self.tick, dispatched=True))
        tv = "JUDGE_REJECT" if v in ("ALL_REJECTED", "JUDGE_REJECT") else v
        if tv == "JUDGE_REJECT":
            self._append(dict(ids, rec="judge", attempt=att, pick=False, judge_version="fake",
                              oracles=[{"name": "expect", "ok": False, "detail": "scripted"}]))
        self.scene_epoch += 1
        self._append(dict(ids, rec="control", op="rewind", to=self.last_good, by="broker", attempt=att,
                          scene_epoch_after=self.scene_epoch, digest_match=True))
        self._append(dict(ids, rec="txn", phase="rollback", event_id=eid, attempt=att,
                          base_point=self.last_good, verdict=tv, reason="scripted by policy"))
        self._frame(None)
        return self._append(dict(ids, rec="close", event_id=eid, agent_id=aid, turn=self.turn,
                                 verdict="ALL_REJECTED" if tv == "JUDGE_REJECT" else v, dispatched=True))

    def director_rewind(self, to, reason="director rewind"):
        self.scene_epoch += 1
        self.last_good = to
        self._frame(None)
        return self._append({"rec": "control", "op": "rewind", "to": to, "by": "director",
                             "scene_epoch_after": self.scene_epoch, "digest_match": True, "reason": reason})

    def control(self, op, by="director-agent", reason=""):
        return self._append({"rec": "control", "op": op, "by": by, "reason": reason})

    def run(self, idle_s=2.0, max_s=60.0, turn_ms=20):
        t_end, last = time.time() + max_s, time.time()
        n = 0
        while time.time() < t_end and time.time() - last < idle_s:
            if self.step() is not None:
                n += 1
                last = time.time()
            time.sleep(turn_ms / 1000.0)
        return n


def main(argv=None):
    ap = argparse.ArgumentParser(description="fake broker (test double) for planned agents")
    ap.add_argument("--rt", required=True)
    ap.add_argument("--policy", default="{}", help="JSON {step id: [verdict, ...]}")
    ap.add_argument("--no-rewind", action="store_true", help="F1-only record shape (no txn)")
    ap.add_argument("--idle-s", type=float, default=3.0)
    ap.add_argument("--max-s", type=float, default=60.0)
    a = ap.parse_args(argv)
    if not os.path.realpath(a.rt).startswith("/dev/shm/vqa-cu/ref"):
        print("RT must resolve under /dev/shm/vqa-cu/ref*", file=sys.stderr)
        return 2
    b = FakeBroker(a.rt, json.loads(a.policy), rewind=not a.no_rewind)
    n = b.run(a.idle_s, a.max_s)
    print(json.dumps({"type": "fake_broker", "turns": n, "seq": b.seq, "tick": b.tick}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
