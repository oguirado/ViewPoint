"""R2: transactional multi-lane queue on top of the F1 reference broker (rewindable Blender capsule).

Track P (declared Claude work, claude-fable-5-2). Contract: product/ref_rewind/CONTRACT.md (rw1).
F1 (product/ref_broker) is imported, never edited: RewindBroker subclasses Broker and overrides only
the hook points named in rw-build.md F2 (step, _dispatch_script, _dispatch_gui, _check_fresh,
_validate, _append, control). test_lanes.F1Contract pins the signatures this file relies on.

    sys.path.insert(0, <product/ref_rewind_lanes>); from rewind_broker import RewindBroker
    b = RewindBroker(rt, job, nonce, ops=<R1 ops module>, store=<R1 Store>, judge=<R3 judge>)
    b.step() ... ; b.rewind(b.rw.last_good) ; b.export_point(tick) ; b.projection()

Per admitted event (each per-agent FIFO is a lane; one Blender, so attempts are serial):
    txn begin(base = last_good) -> F1 dispatch of attempt <event_id>.a<k> -> F1 verdict
      -> digest job + frame + judge -> mark job + ckpt + txn commit
      or restore(base) job + control rewind (digest_match) + txn rollback -> next alternative.
"""
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PRODUCT = os.path.dirname(_HERE)
for _p in (os.path.join(_PRODUCT, "ref_rewind"), os.path.join(_PRODUCT, "ref_broker")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import broker as B  # noqa: E402  (F1, imported, never edited)
import schema as RW  # noqa: E402  (R0, frozen)

NOT_CONFIRMED = "NOT_CONFIRMED"      # export / projection asked beyond the commit frontier
MARK_FAILED = "CKPT_MARK_FAILED"     # mark job of an accepted attempt failed for a non-store reason (rolled back)
F1_REACTIVE = ("TURN_TIMEOUT", "ACTUATOR_BUSY", "OUTCOME_UNCERTAIN", "INPUT_UNCERTAIN")  # F1 re-queues / RECONCILEs on these
NO_GENESIS = "NO_GENESIS"            # the genesis point p0 could not be marked yet (bridge not up)
RESTORE_PENDING = "RESTORE_PENDING"  # resume refused: a failed restore or an open txn is outstanding
# F1 verdicts that are neither commit nor rollback verdicts in rw1. A txn was begun, so it must end
# in a rollback; the F1 verdict is kept in txn.reason. The rewind broker never requeues an attempt.
ROLLBACK_AS = {"TURN_TIMEOUT": "OUTCOME_UNCERTAIN", "ACTUATOR_BUSY": "INPUT_UNCERTAIN",
               "NO_VISIBLE_CHANGE": "INPUT_UNCERTAIN"}


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


class RewindBroker(B.Broker):
    def __init__(self, rt, job, nonce, ops=None, store=None, judge=None,
                 restore_cap_s=RW.RESTORE_CAP_S, load_ui=True, **kw):
        if ops is None or store is None or judge is None:
            raise B.BrokerError("RewindBroker needs ops (R1), store (R1) and judge (R3)")
        self.ops, self.store, self.judge = ops, store, judge
        self.restore_cap_s, self.load_ui = restore_cap_s, load_ui
        self.rw = RW.RewindState()     # shared fold of events.jsonl (R0); updated on every append
        self.scene_epoch = RW.SCENE_EPOCH_0
        self._epoch_seq = 0            # seq of the newest control rewind: older gui intents are stale
        self._commits = []             # [(tick, point, attempt, event_id)] in log order
        self._closed_attempts = set()  # attempts named by an F1 close record (commit fully closed)
        self.close_recovered_first = True   # False only in negative controls
        self.map_mark_failure = True        # False only in negative controls
        self.heal_tail = kw.pop("heal_tail", True)   # False only in negative controls
        self._ctl_n = 0
        B.Broker.__init__(self, rt, job, nonce, **kw)   # takes the single-writer lock
        if self.heal_tail:
            self._heal_tail()
        for r in RW.read_events(self.p_events):
            self._fold(r)
        nums = [int(p[1:]) for p in list(self.rw.points) + list(self.store.points()) if RW.POINT_ID_RE.match(p)]
        self._point_n = max(nums) + 1 if nums else 0

    def _heal_tail(self):
        """A crashed writer can leave events.jsonl without a final newline. The first append after
        the restart (recovery's control rewind) would be glued onto that line and both records lost
        on the next replay. Under the lock: cut a torn tail; terminate a complete unterminated one."""
        try:
            with open(self.p_events, "r+b") as f:
                data = f.read()
                if not data or data.endswith(b"\n"):
                    return
                good = data.rfind(b"\n") + 1
                try:
                    json.loads(data[good:].decode("utf-8"))
                    f.write(b"\n")              # complete record, only its newline is missing
                except ValueError:
                    f.truncate(good)            # torn record: never acknowledged, safe to drop
                f.flush()
                os.fsync(f.fileno())
        except FileNotFoundError:
            return

    # ---------------------------------------------------------------- log fold (replay == live)
    def _append(self, rec):
        rec = B.Broker._append(self, rec)
        self._fold(rec)
        return rec

    def _fold(self, r):
        self.rw.apply(r)
        if r.get("rec") == "close" and r.get("attempt") and not r.get("ingest"):
            self._closed_attempts.add(r["attempt"])
        if not RW.is_rewind_record(r):
            return
        if r["rec"] == "control":
            self._bump_epoch(r)
        elif r["rec"] == "txn" and r.get("phase") == "commit":
            self._commits.append((r.get("tick"), r.get("point"), r.get("attempt"), r.get("event_id")))

    def _bump_epoch(self, r):
        """EPOCH BUMP: every restore moves the capsule to a new scene_epoch; every gui intent
        enqueued before this record is stale (closes STALE_FRAME, may resubmit)."""
        if isinstance(r.get("scene_epoch_after"), int):
            self.scene_epoch = max(self.scene_epoch, r["scene_epoch_after"])
        self._epoch_seq = max(self._epoch_seq, r.get("seq", 0))

    # ---------------------------------------------------------------- INTENT / ADMIT hooks
    def _validate(self, agent, it):
        ext = RW.validate_intent_ext(it) if isinstance(it, dict) else None
        if ext and ext[0] == "NO_LEASE":   # op:rewind, rec:control or a ctl.* id from an agent
            return ext
        return B.Broker._validate(self, agent, it) or ext

    def _check_fresh(self, ev):
        if ev["lane"] == "gui" and ev.get("seq", 0) < self._epoch_seq:
            return "STALE_FRAME", ("enqueued before scene_epoch %d (restore at seq %d): re-observe and resubmit"
                                   % (self.scene_epoch, self._epoch_seq))
        return B.Broker._check_fresh(self, ev)

    # ---------------------------------------------------------------- control jobs
    def _job_n(self):
        self._ctl_n += 1
        return "%d-%d" % (self.seq, self._ctl_n)

    def _digest(self):
        r = RW.dispatch_control(self, RW.control_job_id("digest", self._job_n()), self.ops.digest_script())
        d = (r["result"] or {}).get("digest") if r["ok"] else None
        return d if isinstance(d, dict) and d else None

    def _run_probe(self, script):
        """run_probe for the judge: probe job, then an independent digest job (the probe's own
        account of the digest is not trusted for PROBE_NOT_READONLY)."""
        r = RW.dispatch_control(self, RW.control_job_id("probe", self._job_n()), script)
        res = r["result"]
        value = res["value"] if isinstance(res, dict) and "value" in res else res
        return {"ok": bool(r["ok"] and res is not None), "result": value, "digest": self._digest() or {}}

    def _pinned(self):
        pins = [self.rw.last_good] if self.rw.last_good else []
        if self.rw.open_txn:
            pins.append(self.rw.open_txn["base_point"])
        return tuple(pins)

    def _mark(self, tick):
        """Mark job + store admission + rec:ckpt. Returns (ckpt record, None) or (None, code)."""
        pid = RW.point_id(self._point_n)
        self._point_n += 1          # point ids (and mark job names) are single-use, also after a failure
        r = RW.dispatch_control(self, RW.control_job_id("mark", pid), self.ops.mark_script(self.store.path_for(pid)))
        digest = (r["result"] or {}).get("digest") if r["ok"] else None
        if not digest:
            return None, "JOB_ERROR" if r["code"] == "OK" else r["code"]
        try:
            rec = self.store.admit(pid, tick, self.scene_epoch, digest, self.frame, pinned=self._pinned())
        except RW.CkptError as e:
            return None, e.code
        return self._append(rec), None

    def _digest_ok(self, got, point):
        """DIGEST CHECK: a restore counts only if the digest after it equals the point's digest."""
        return RW.digest_match(got, self.rw.points[point]["digest"])

    def _restore(self, point, by, verdict, attempt=None):
        """Restore job to `point` + control rewind record (scene_epoch+1 always). On success a fresh
        frame is captured (the frame ring restarts). On failure: control stop -> FROZEN. Returns
        (ok, code)."""
        epoch_after = self.scene_epoch + 1
        code, match = None, False
        if point not in self.rw.points:
            code = RW.CKPT_MISSING
        else:
            try:
                path = self.store.verify(point)        # sha256 of the .blend re-checked (R1)
            except RW.CkptError as e:
                code = e.code
        if code is None:
            cap, self.job_cap_s = self.job_cap_s, self.restore_cap_s
            try:
                r = RW.dispatch_control(self, RW.control_job_id("restore", point, "e%d" % epoch_after),
                                        self.ops.restore_script(path, self.load_ui))
            finally:
                self.job_cap_s = cap
            if not r["ok"]:
                code = {"TURN_TIMEOUT": RW.RESTORE_TIMEOUT, "JOB_ERROR": RW.RESTORE_MISMATCH}.get(r["code"], RW.RESTORE_UNAUTH)
            else:
                match = self._digest_ok((r["result"] or {}).get("digest"), point)
                if not match:
                    code = RW.RESTORE_MISMATCH
        reason = verdict if code is None else "%s;%s" % (verdict, code)
        self._append(RW.rewind_rec(point, epoch_after, match, by=by, reason=reason, attempt=attempt))
        if match:
            self._capture(RW.control_job_id("restore", point, "e%d" % epoch_after), None)
            return True, None
        B.Broker.control(self, "stop", "broker", "%s: restore to %s failed; only a director rewind to last_good is admitted" % (code, point))
        return False, code

    # ---------------------------------------------------------------- open txn (crash / failed restore)
    def _open_verdict(self):
        """Rollback verdict of the open txn, from the log alone."""
        a = self.rw.open_txn["attempt"]
        for r in reversed(self.rw.rewinds):
            if r.get("attempt") == a:
                v = (r.get("reason") or "").split(";")[0]
                if v in RW.ROLLBACK_VERDICTS:
                    return v
        if self.rw.judged.get(a) is False:
            return RW.JUDGE_REJECT
        return "OUTCOME_UNCERTAIN"   # broker died between begin and commit/rollback: effect unknown

    def _undo_open(self, by, why):
        """ARIES-style undo: restore the base of the open txn, then log its rollback."""
        t = self.rw.open_txn
        verdict = self._open_verdict()
        ok, code = self._restore(t["base_point"], by, verdict, attempt=t["attempt"])
        if ok:
            self._append(RW.txn_rec("rollback", t["event_id"], t["attempt"], t["base_point"], verdict=verdict, reason=why))
        return ok, code

    def _close_recovered_commit(self):
        """A txn commit without its F1 close (broker died in between) is closed HERE, before anything
        else is dispatched, with the tick stored in the commit record. Leaving it to F1's round-robin
        lets another agent's event commit first and take the same tick (TICK_BACKWARDS, and the
        projection drops the recovered event). Returns the close verdict, or None if nothing pends."""
        pend = next((c for c in self._commits if c[2] not in self._closed_attempts), None)
        if pend is None or not self.close_recovered_first:
            return None
        tick, point, att, eid = pend
        ev, agent = None, None
        for a, q in self.queues.items():
            ev = next((e for e in q if e["event_id"] == eid), None)
            if ev is not None:
                agent = a
                q.remove(ev)
                break
        if ev is None:      # the intent is not queued any more: nothing F1 could close either
            self._closed_attempts.add(att)
            return None
        verdict = "STATE_VERIFIED" if ev["lane"] == "script" else "VERIFIED"
        self.status[eid] = "closed"
        dispatch_frame = self.frame
        if isinstance(tick, int):
            self.tick = max(self.tick, tick)
        else:
            self.tick += 1
        frame_rec = self._capture(eid, None)
        self._append({"rec": "close", "turn": self.turn, "event_id": eid, "agent_id": agent, "lane": ev["lane"],
                      "rebased": bool(ev.get("resubmit") or ev.get("auto_rebased")),
                      "dispatch_capture_seq": dispatch_frame, "verify_frame_ref": frame_rec["frame"],
                      "own_regions": frame_rec["changed_regions"], "spill_regions": [],
                      "tick": self.tick, "verdict": verdict, "drops": self.drops.get(eid, 0), "requeued": False,
                      "turn_epoch_after": self.turn_epoch, "attempt": att, "point": point,
                      "recovered_commit": True,
                      "reason": "commit of %s found in the log; closed before any other dispatch" % att})
        self._cursor(ev, "result")
        return verdict

    def _pre_dispatch(self):
        """Runs before F1 step(): nothing is dispatched while a txn is open or without a genesis point."""
        if self.rw.open_txn is not None:
            ok, _ = self._undo_open("broker", "recovery: open txn found before dispatch; base restored")
            if not ok:
                return self.mode
        v = self._close_recovered_commit()
        if v:
            return v
        if self.rw.last_good is None:
            rec, code = self._mark(self.tick)
            if rec is None:
                self.log("genesis mark failed: %s" % code)
                return NO_GENESIS
        return None

    # ---------------------------------------------------------------- one turn
    def step(self):
        if self.mode != "IDLE":
            return self.mode
        r = self._pre_dispatch()
        if r:
            return r
        return B.Broker.step(self)

    def _dispatch_script(self, ev):
        if RW.is_control_id(ev["event_id"]):      # broker-owned control job: plain F1 dispatch
            return B.Broker._dispatch_script(self, ev)
        return self._transact(ev)

    def _dispatch_gui(self, ev):
        return self._transact(ev)

    def _raw_dispatch(self, ev, att, script):
        if ev["lane"] == "script":
            return B.Broker._dispatch_script(self, {"event_id": att, "script": script, "script_sha256": _sha(script),
                                                    "write_set": ev.get("write_set") or []})
        return B.Broker._dispatch_gui(self, ev)

    def _transact(self, ev):
        """The transaction of one admitted event. Returns (verdict, extra) to F1 step(), which closes
        the event: a commit verdict, ALL_REJECTED (state = base), or a restore/ckpt failure code."""
        eid = ev["event_id"]
        scripts = ([ev["script"]] + list(ev.get("alternatives") or [])) if ev["lane"] == "script" else [None]
        used = [a for a in self.rw.attempts if RW.split_attempt(a)[0] == eid]
        done = [a for a in used if self.rw.attempts[a] == "commit"]
        if done:   # broker died between txn commit and the F1 close: applied once, never again
            c = next(c for c in self._commits if c[2] == done[0])
            return ("STATE_VERIFIED" if ev["lane"] == "script" else "VERIFIED"), {
                "attempt": done[0], "point": c[1], "recovered_commit": True,
                "reason": "commit of %s found in the log; not dispatched again" % done[0]}
        base = self.rw.last_good
        base_rec = self.rw.points[base]
        base_png = os.path.join(self.d_frames, "frame_%d.png" % base_rec["frame"])
        base_view = {"point_id": base, "digest": base_rec["digest"],
                     "frame_png": base_png if os.path.exists(base_png) else None}
        history = [[a, self.rw.attempts[a]] for a in used]
        for k in range(len(used) + 1, min(len(scripts), RW.K_MAX) + 1):   # an attempt id is never reused
            att, script = RW.attempt_id(eid, k), scripts[k - 1]
            self._append(RW.txn_rec("begin", eid, att, base))
            verdict, extra = self._raw_dispatch(ev, att, script)
            rb, why = None, extra.get("reason")
            if verdict in RW.COMMIT_VERDICTS:
                after = self._digest()
                if after is None:
                    rb, why = "OUTCOME_UNCERTAIN", "digest job after the attempt was not answered"
                else:
                    self._capture(att, None)
                    j = self.judge({"attempt": att, "intent": ev, "script": script, "verdict": verdict, "extra": dict(extra)},
                                   base_view, {"digest": after, "frame_png": self.frame_png, "job_ms": extra.get("job_ms")},
                                   run_probe=self._run_probe)
                    RW.validate_record(j)
                    if j.get("rec") != "judge" or j.get("attempt") != att:
                        raise B.BrokerError("judge answered for %r, asked about %r" % (j.get("attempt"), att))
                    j = self._append(j)
                    if j["pick"]:
                        tick = self.tick + 1               # the tick F1 gives this event at close
                        ck, code = self._mark(tick)
                        if ck is not None:
                            self._append(RW.txn_rec("commit", eid, att, base, verdict=verdict, point=ck["point_id"], tick=tick))
                            extra.update({"attempt": att, "point": ck["point_id"], "attempts": history + [[att, "commit"]]})
                            return verdict, extra
                        # accepted but not checkpointable (stall guard): undo and stop trying
                        ok, rcode = self._restore(base, "broker", "JOB_ERROR", attempt=att)
                        if ok:
                            self._append(RW.txn_rec("rollback", eid, att, base, verdict="JOB_ERROR",
                                                    reason="%s: accepted state could not be checkpointed" % code))
                        # the attempt is rolled back and final: F1 must not see a verdict it re-queues or
                        # RECONCILEs on (a re-queue would run the next alternative after an accept)
                        closing = code if (code or "").startswith("CKPT_") else MARK_FAILED
                        if not self.map_mark_failure:
                            closing = code
                        return (closing if ok else rcode), {"attempt": att, "attempts": history + [[att, "rollback"]],
                                                            "reason": "mark failed (%s)" % code}
                    rb, why = RW.JUDGE_REJECT, "; ".join("%s:%s" % (o["name"], o["detail"]) for o in j["oracles"] if not o["ok"])[:300]
            elif verdict in RW.ROLLBACK_VERDICTS:
                rb = verdict
            else:
                rb, why = ROLLBACK_AS.get(verdict, "OUTCOME_UNCERTAIN"), "f1=%s: %s" % (verdict, extra.get("reason"))
            if rb == "OUTCOME_UNCERTAIN":
                self.expired[att] = self.turn_epoch    # a late outbox of this attempt is quarantined by F1
            ok, rcode = self._restore(base, "broker", rb, attempt=att)
            if not ok:   # FROZEN, txn stays open (contract s3)
                return rcode, {"attempt": att, "attempts": history + [[att, "open"]],
                               "reason": "restore to %s failed after %s" % (base, rb)}
            self._append(RW.txn_rec("rollback", eid, att, base, verdict=rb, reason=why))
            history.append([att, rb])
        return RW.ALL_REJECTED, {"attempts": history, "base_point": base,
                                 "reason": "every alternative was rolled back; state = %s" % base}

    # ---------------------------------------------------------------- control
    def control(self, op, by, reason):
        if op == "resume" and (self.rw.open_txn is not None or (self.rw.rewinds and not self.rw.rewinds[-1]["digest_match"])):
            raise B.BrokerError("%s: rewind to last_good (%s) first" % (RESTORE_PENDING, self.rw.last_good))
        return B.Broker.control(self, op, by, reason)

    def rewind(self, to, by="director", reason=""):
        """Director control (rewindctl rewind --to). Only last_good is admitted; agents have no path
        to this method (intents with op/rec control/ctl.* ids are refused NO_LEASE at ingest)."""
        if by != "director":
            raise B.BrokerError("NO_LEASE: rewind is a broker/director control; by=%r refused" % (by,))
        if to != self.rw.last_good:
            raise B.BrokerError("NO_LEASE: only a rewind to last_good (%s) is admitted, not %r" % (self.rw.last_good, to))
        if self.rw.open_txn is not None:
            ok, code = self._undo_open("director", "director rewind: %s" % reason)
        else:
            ok, code = self._restore(to, "director", "director", attempt=None)
        return {"ok": ok, "code": code or "OK", "to": to, "mode": self.mode, "scene_epoch": self.scene_epoch}

    # ---------------------------------------------------------------- commit frontier
    def _frontier_ok(self, tick):
        """FRONTIER CHECK: external effects only for ticks <= confirmed_tick and with no open txn."""
        return RW.exportable(tick, self.rw)

    def export_point(self, tick):
        """Checkpoint file that an export (glb, render) of `tick` may read, or a NOT_CONFIRMED refusal."""
        if not self._frontier_ok(tick):
            return {"ok": False, "code": NOT_CONFIRMED, "tick": tick, "confirmed_tick": self.rw.confirmed_tick,
                    "open_txn": self.rw.open_txn["attempt"] if self.rw.open_txn else None}
        prior = [c for c in self._commits if c[0] <= tick]
        point, ctick = (prior[-1][1], prior[-1][0]) if prior else (next(iter(self.rw.points), None), 0)
        try:
            path = self.store.verify(point)
        except RW.CkptError as e:
            return {"ok": False, "code": e.code, "tick": tick, "point": point}
        return {"ok": True, "code": "OK", "tick": tick, "state_tick": ctick, "point": point, "path": path,
                "blend_sha256": self.rw.points[point]["blend_sha256"]}

    def projection(self):
        """Projection records for room/TaskLight: one per closed dispatched event inside the frontier."""
        commits = {c[3]: c for c in self._commits}
        out = []
        for r in RW.read_events(self.p_events):
            if r.get("rec") != "close" or r.get("ingest") or r.get("verify_frame_ref") is None:
                continue
            if not self._frontier_ok(r.get("tick")):
                continue
            c = commits.get(r["event_id"])
            out.append({"tick": r["tick"], "event_id": r["event_id"], "agent_id": r.get("agent_id"),
                        "verdict": r["verdict"], "committed": bool(c and c[0] == r["tick"]),
                        "point": c[1] if c and c[0] == r["tick"] else None, "frame": r["verify_frame_ref"]})
        return out

    def snapshot(self):
        """State that must be identical after a rebuild from events.jsonl (replay determinism)."""
        return {"queues": {a: [e["event_id"] for e in q] for a, q in self.queues.items()},
                "status": dict(self.status), "turn": self.turn, "tick": self.tick, "frame": self.frame,
                "mode": self.mode, "scene_epoch": self.scene_epoch, "epoch_seq": self._epoch_seq,
                "commits": [list(c) for c in self._commits], "rw": self.rw.summary(), "ring": list(self.store.points())}
