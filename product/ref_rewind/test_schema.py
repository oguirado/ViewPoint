"""R0 tests: schema validators, the shared replay fold, and the fakes against the REAL F1 broker and
bridge core (no Blender, no vision).

    python3 -m unittest -v test_schema        (from product/ref_rewind/)

Negative controls: every guard has a test that feeds the exact input the guard exists for and asserts the
refusal, so deleting the guard turns that test red; scenario-level controls (`*_red_without_*`) re-run the
reference transaction with a mutant (no-op restore, sha check off, accept-all judge) and require the
scenario's own assertions to FAIL.
"""
import hashlib
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fakes  # noqa: E402
import schema as RW  # noqa: E402

D = {"col:CHAR_mesh": "aa", "col:PROPS": "bb", "ui:view3d": "cc"}
SHA = "0" * 64
GOOD = "scene['col:PROPS'].append('table')"
WRONG = "scene['col:PROPS'].append('table10x'); scene['col:CHAR_mesh'].append('moved')"   # spill + wrong size
PROBE = "print(rw_result({'value': scene['col:PROPS'], 'digest': digest()}))"


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def run_txn(cap, store, st, eid, scripts, judge=fakes.fake_judge, restore=fakes.restore_script, n0=1):
    """Reference transaction (what R2 automates): serial attempts from the same base point, judge,
    restore + rollback on reject, mark + commit on accept. Every record goes through broker._append
    (the broker stays the only writer) and through the shared fold `st`."""
    b = cap.broker

    def log(rec):
        st.apply(b._append(rec))

    base_id = st.last_good
    base_rec = store.get(base_id)
    it = {"event_id": eid, "lane": "script", "write_set": ["col:PROPS"], "script": scripts[0],
          "alternatives": scripts[1:], "expect": {"probe": PROBE, "equals": ["table"]}}
    assert RW.validate_intent_ext(it) is None
    base_png = b.frame_png
    for k, (att, script) in enumerate(RW.attempt_scripts(it)):
        log(RW.txn_rec("begin", eid, att, base_id))
        verdict, extra = b._dispatch_script({"event_id": att, "script": script, "script_sha256": _sha(script),
                                             "write_set": it["write_set"]})
        b._capture(att, None)
        after = cap.control("digest", fakes.digest_script(), "%d%d" % (n0, k))["result"]["digest"]

        def run_probe(src, _n="%d%d" % (n0, k)):
            r = cap.control("probe", src, _n)
            res = r["result"] or {}
            return {"ok": r["ok"], "result": res.get("value"), "digest": res.get("digest")}

        j = judge({"attempt": att, "intent": it, "script": script, "verdict": verdict, "extra": extra},
                  {"point_id": base_id, "digest": base_rec["digest"], "frame_png": base_png},
                  {"digest": after, "frame_png": b.frame_png, "job_ms": extra.get("job_ms")}, run_probe=run_probe)
        log(j)
        if j["pick"] and verdict in RW.COMMIT_VERDICTS:
            b.tick += 1
            pid = RW.point_id(len(store.points()))
            m = cap.control("mark", fakes.mark_script(store.path_for(pid)), pid)
            log(store.admit(pid, b.tick, st.scene_epoch, m["result"]["digest"], b.frame))
            log(RW.txn_rec("commit", eid, att, base_id, verdict=verdict, point=pid, tick=b.tick))
            return "COMMIT"
        epoch_after = st.scene_epoch + 1
        r = cap.control("restore", restore(store.verify(base_id)), base_id, "e%d" % epoch_after)
        match = bool(r["ok"] and r["result"] and RW.digest_match(r["result"]["digest"], base_rec["digest"]))
        log(RW.rewind_rec(base_id, epoch_after, match, reason=verdict, attempt=att))
        if not match:
            log({"rec": "control", "op": "stop", "by": "broker", "reason": RW.RESTORE_MISMATCH})
            return "FROZEN"
        b._capture(None, None)
        log(RW.txn_rec("rollback", eid, att, base_id,
                       verdict=verdict if verdict in RW.ROLLBACK_VERDICTS else RW.JUDGE_REJECT, reason="rejected"))
    return RW.ALL_REJECTED


def genesis(cap):
    store, st = fakes.FakeStore(cap.rt), RW.RewindState()
    m = cap.control("mark", fakes.mark_script(store.path_for("p0")), "p0")
    st.apply(cap.broker._append(store.admit("p0", 0, st.scene_epoch, m["result"]["digest"], cap.broker.frame)))
    return store, st


class Base(unittest.TestCase):
    def cap(self, **kw):
        c = fakes.Capsule(**kw)
        self.addCleanup(c.close)
        return c

    def code(self, code, fn, *a, **k):
        with self.assertRaises(RW.SchemaError) as cm:
            fn(*a, **k)
        self.assertEqual(cm.exception.code, code)


class Records(Base):
    def test_builders_round_trip(self):
        for r in (RW.ckpt_rec("p1", 1, 1, SHA, 10, D, 3),
                  RW.txn_rec("begin", "props-agent-1", "props-agent-1.a1", "p1"),
                  RW.txn_rec("commit", "props-agent-1", "props-agent-1.a2", "p1", verdict="STATE_VERIFIED", point="p2", tick=2),
                  RW.txn_rec("rollback", "props-agent-1", "props-agent-1.a1", "p1", verdict=RW.JUDGE_REJECT, reason="probe"),
                  RW.judge_rec("props-agent-1.a1", False, [RW.oracle("expect", False, "x"), RW.oracle("delta", True)]),
                  RW.rewind_rec("p1", 2, True)):
            self.assertIs(RW.validate_record(r), r)
            self.assertTrue(RW.is_rewind_record(r))

    def test_ckpt_guards(self):
        self.code("BAD_POINT", RW.ckpt_rec, "point1", 1, 1, SHA, 10, D, 3)
        self.code("BAD_SHA", RW.ckpt_rec, "p1", 1, 1, "abc", 10, D, 3)
        self.code("BAD_EPOCH", RW.ckpt_rec, "p1", 1, 0, SHA, 10, D, 3)
        self.code("BAD_DIGEST", RW.ckpt_rec, "p1", 1, 1, SHA, 10, {}, 3)
        self.code("BAD_DIGEST", RW.ckpt_rec, "p1", 1, 1, SHA, 10, {"objects": "aa"}, 3)
        self.code("BAD_REC", RW.ckpt_rec, "p1", 1, 1, SHA, 0, D, 3)

    def test_txn_guards(self):
        self.code("BAD_ATTEMPT", RW.txn_rec, "begin", "ev-1", "ev-2.a1", "p1")
        self.code("BAD_ATTEMPT", RW.txn_rec, "begin", "ev-1", "ev-1.a4", "p1")       # K_MAX
        self.code("BAD_ATTEMPT", RW.txn_rec, "begin", "ctl.mark.p1", "ctl.mark.p1.a1", "p1")
        self.code("BAD_PHASE", RW.txn_rec, "abort", "ev-1", "ev-1.a1", "p1")
        self.code("BAD_VERDICT", RW.txn_rec, "begin", "ev-1", "ev-1.a1", "p1", verdict="STATE_VERIFIED")
        self.code("BAD_VERDICT", RW.txn_rec, "commit", "ev-1", "ev-1.a1", "p1", verdict="JOB_ERROR", point="p2", tick=1)
        self.code("BAD_VERDICT", RW.txn_rec, "rollback", "ev-1", "ev-1.a1", "p1", verdict="STATE_VERIFIED")
        self.code("BAD_POINT", RW.txn_rec, "commit", "ev-1", "ev-1.a1", "p1", verdict="STATE_VERIFIED", tick=1)
        self.code("BAD_REC", RW.txn_rec, "commit", "ev-1", "ev-1.a1", "p1", verdict="STATE_VERIFIED", point="p2")

    def test_judge_guards_dead_oracle(self):
        ok, bad = RW.oracle("delta", True), RW.oracle("expect", False, "no")
        self.code("BAD_PICK", RW.judge_rec, "ev-1.a1", True, [bad, ok])               # pick over a failed oracle
        self.code("BAD_PICK", RW.judge_rec, "ev-1.a1", True, [RW.oracle("cost", True)])  # pick with no deciding oracle
        self.code("BAD_PICK", RW.judge_rec, "ev-1.a1", False, [ok])                   # reject without a failed oracle
        self.code("BAD_ORACLES", RW.judge_rec, "ev-1.a1", True, [ok, RW.oracle("expect", True)])  # order
        self.code("BAD_ORACLES", RW.judge_rec, "ev-1.a1", True, [])
        self.code("BAD_ORACLES", RW.judge_rec, "ev-1.a1", True, [{"name": "vibes", "ok": True, "detail": ""}])

    def test_rewind_is_broker_only(self):
        self.code("NO_LEASE", RW.rewind_rec, "p1", 2, True, by="props-agent")
        self.code("BAD_EPOCH", RW.rewind_rec, "p1", 1, True)
        self.code("BAD_POINT", RW.rewind_rec, "last_good", 2, True)
        self.code("NOT_REWIND", RW.validate_record, {"rec": "control", "op": "resume"})

    def test_ids(self):
        self.assertEqual(RW.attempt_id("props-agent-7", 2), "props-agent-7.a2")
        self.assertEqual(RW.split_attempt("a.b.a1.a3"), ("a.b.a1", 3))
        self.code("BAD_ATTEMPT", RW.attempt_id, "x" * 74, 1)
        self.code("BAD_ATTEMPT", RW.split_attempt, "ev_1.a1")
        self.assertEqual(RW.control_job_id("restore", "p1", "e2"), "ctl.restore.p1.e2")
        self.code("BAD_JOB", RW.control_job_id, "rm", "p1")
        self.code("BAD_JOB", RW.control_job_id, "mark", "p_1")
        from broker import EVENT_ID_RE   # pinned to F1
        self.assertEqual(RW.EVENT_ID_RE.pattern, EVENT_ID_RE.pattern)
        for j in (RW.attempt_id("x" * 73, 3), RW.control_job_id("restore", "p999999", "e123456")):
            self.assertTrue(EVENT_ID_RE.match(j))

    def test_result_line(self):
        out = "noise\n" + RW.result_line({"digest": D}) + "\n"
        self.assertEqual(RW.parse_result(out), {"digest": D})
        self.assertIsNone(RW.parse_result("OK\nno result here"))
        self.assertIsNone(RW.parse_result(RW.RESULT_PREFIX + "[1]"))
        self.code("RESULT_TOO_LONG", RW.result_line, {"x": "y" * RW.RESULT_MAX})

    def test_digest_helpers(self):
        b = dict(D, **{"col:PROPS": "zz", "mat:Wood": "m1"})
        self.assertEqual(RW.digest_delta(D, b), ["col:PROPS", "mat:Wood"])
        self.assertTrue(RW.digest_match(D, dict(D)))
        self.assertFalse(RW.digest_match(D, b))
        self.assertFalse(RW.digest_match({}, {}))    # two empty digests prove nothing
        it = {"lane": "script", "write_set": ["col:PROPS"]}
        self.assertEqual(RW.delta_outside(["col:PROPS", "mat:Wood"], it), ["mat:Wood"])
        self.assertEqual(RW.delta_outside(["col:PROPS", "mat:Wood"], dict(it, touches=["mat:Wood"])), [])
        self.assertEqual(RW.delta_outside(["ui:view3d"], {"lane": "gui", "write_set": []}), [])
        self.assertEqual(RW.delta_outside(["ui:view3d"], it), ["ui:view3d"])


class IntentExt(Base):
    IT = {"v": 1, "rec": "enqueue", "event_id": "props-agent-1", "agent_id": "props-agent", "lane": "script",
          "based_on_frame": 0, "write_set": ["col:PROPS"], "script": "pass", "expect": "text"}

    def bad(self, verdict, **kw):
        r = RW.validate_intent_ext(dict(self.IT, **kw))
        self.assertIsNotNone(r, kw)
        self.assertEqual(r[0], verdict, r)

    def test_old_intent_and_extension_valid(self):
        self.assertIsNone(RW.validate_intent_ext(self.IT))
        it = dict(self.IT, alternatives=["a", "b"], expect={"probe": "print(1)", "equals": 1}, touches=["mat:Wood", "render"])
        self.assertIsNone(RW.validate_intent_ext(it))
        self.assertEqual([a for a, _ in RW.attempt_scripts(it)], ["props-agent-1.a1", "props-agent-1.a2", "props-agent-1.a3"])

    def test_refusals(self):
        self.bad(RW.BAD_INTENT, alternatives=["a", "b", "c"])            # K_MAX
        self.bad(RW.BAD_INTENT, alternatives=["a", ""])
        self.bad(RW.BAD_INTENT, alternatives=["a"], lane="gui")
        self.bad(RW.BAD_INTENT, expect={"probe": "print(1)"})
        self.bad(RW.BAD_INTENT, expect=7)
        self.bad(RW.BAD_INTENT, touches=["col:CHAR_mesh"])                # col:* goes through the claim check
        self.bad(RW.BAD_INTENT, event_id="x" * 74)
        self.bad("NO_LEASE", event_id="ctl.restore.p0.e2")                # agents cannot name control jobs
        self.bad("NO_LEASE", op="rewind", to="p0")                        # agents cannot submit a rewind
        self.bad("NO_LEASE", rec="control")


class Replay(Base):
    def log(self):
        e, a1, a2 = "props-agent-1", "props-agent-1.a1", "props-agent-1.a2"
        return [RW.ckpt_rec("p0", 0, 1, SHA, 10, D, 0),
                RW.txn_rec("begin", e, a1, "p0"),
                RW.judge_rec(a1, False, [RW.oracle("delta", False, "spill")]),
                RW.rewind_rec("p0", 2, True, attempt=a1),
                RW.txn_rec("rollback", e, a1, "p0", verdict=RW.JUDGE_REJECT),
                RW.txn_rec("begin", e, a2, "p0"),
                RW.judge_rec(a2, True, [RW.oracle("delta", True)]),
                RW.ckpt_rec("p1", 1, 2, SHA, 10, dict(D, **{"col:PROPS": "new"}), 4),
                RW.txn_rec("commit", e, a2, "p0", verdict="STATE_VERIFIED", point="p1", tick=1)]

    def codes(self, recs):
        return [c for _, c, _ in RW.replay(recs).violations]

    def test_clean_log(self):
        st = RW.replay(self.log() + [{"rec": "close", "tick": 1}, {"rec": "turn"}])
        self.assertEqual(st.violations, [])
        self.assertEqual(st.summary(), {
            "scene_epoch": 2, "confirmed_tick": 1, "last_good": "p1", "points": ["p0", "p1"], "open_txn": None,
            "attempts": {"props-agent-1.a1": "rollback", "props-agent-1.a2": "commit"}, "rewinds": 1,
            "frozen": False, "violations": 0})
        self.assertEqual(RW.replay(self.log()).summary(), st.summary())   # deterministic

    def test_rollback_without_restore_is_flagged(self):
        recs = self.log()
        del recs[3]
        self.assertIn("ROLLBACK_WITHOUT_RESTORE", self.codes(recs))
        recs = self.log()
        recs[3] = RW.rewind_rec("p0", 2, False)
        st = RW.replay(recs[:5])
        self.assertIn("ROLLBACK_WITHOUT_RESTORE", [c for _, c, _ in st.violations])
        self.assertTrue(st.frozen)

    def test_epoch_must_bump_by_one(self):
        recs = self.log()
        recs[3] = RW.rewind_rec("p0", 3, True)
        self.assertIn("EPOCH_NOT_BUMPED", self.codes(recs))
        recs = self.log()
        recs[7] = RW.ckpt_rec("p1", 1, 1, SHA, 10, D, 4)     # point marked under the pre-rewind epoch
        self.assertIn("EPOCH_SKEW", self.codes(recs))

    def test_commit_needs_judge_and_point(self):
        recs = self.log()
        del recs[6]
        self.assertIn("COMMIT_UNJUDGED", self.codes(recs))
        recs = self.log()
        del recs[7]
        self.assertIn("UNKNOWN_POINT", self.codes(recs))
        recs = self.log()
        del recs[2]
        self.assertIn("ROLLBACK_UNJUDGED", self.codes(recs))

    def test_serial_attempts_never_repeated(self):
        recs = self.log()
        self.assertIn("TXN_OVERLAP", self.codes(recs[:2] + [RW.txn_rec("begin", "x-1", "x-1.a1", "p0")]))
        self.assertIn("ATTEMPT_REUSED", self.codes(recs[:5] + [recs[1]]))
        self.assertIn("TXN_NOT_OPEN", self.codes(recs[:1] + [recs[4]]))
        self.assertIn("BASE_NOT_LAST_GOOD", self.codes(recs + [RW.txn_rec("begin", "x-1", "x-1.a1", "p0")]))
        self.assertIn("POINT_REUSED", self.codes(recs + [recs[0]]))
        self.assertIn("TICK_BACKWARDS", self.codes(recs + [
            RW.txn_rec("begin", "x-1", "x-1.a1", "p1"), RW.judge_rec("x-1.a1", True, [RW.oracle("delta", True)]),
            RW.txn_rec("commit", "x-1", "x-1.a1", "p1", verdict="STATE_VERIFIED", point="p1", tick=1)]))

    def test_open_txn_visible_for_crash_recovery_and_frontier(self):
        st = RW.replay(self.log()[:2])
        self.assertEqual(st.open_txn["attempt"], "props-agent-1.a1")
        self.assertEqual(st.open_txn["base_point"], "p0")
        self.assertFalse(RW.exportable(0, st))            # nothing leaves while a txn is open
        st = RW.replay(self.log())
        self.assertTrue(RW.exportable(1, st))
        self.assertFalse(RW.exportable(2, st))            # unconfirmed tick

    def test_malformed_record_is_a_violation_not_a_crash(self):
        self.assertEqual(self.codes([{"rec": "txn", "phase": "begin"}]), ["BAD_ATTEMPT"])


class FakeCapsule(Base):
    def test_reference_transaction_reject_rewind_accept(self):
        c = self.cap()
        store, st = genesis(c)
        f0 = c.broker.frame_png
        self.assertEqual(run_txn(c, store, st, "props-agent-1", [WRONG, GOOD]), "COMMIT")
        self.assertEqual(c.scene["col:PROPS"], ["table"])
        self.assertEqual(c.scene["col:CHAR_mesh"], [])                 # the wrong edit is gone
        st2 = RW.replay(c.events())                                     # rebuilt from events.jsonl only
        self.assertEqual(st2.violations, [])
        self.assertEqual(st2.summary(), st.summary())
        self.assertEqual((st2.scene_epoch, st2.confirmed_tick, st2.last_good), (2, 1, "p1"))
        self.assertEqual(st2.attempts, {"props-agent-1.a1": "rollback", "props-agent-1.a2": "commit"})
        rw = c.events("control", op="rewind")
        self.assertEqual([(r["to"], r["digest_match"], r["scene_epoch_after"]) for r in rw], [("p0", True, 2)])
        j = c.events("judge", attempt="props-agent-1.a1")[0]
        self.assertEqual([(o["name"], o["ok"]) for o in j["oracles"]],
                         [("expect", False), ("delta", False), ("witness", True), ("cost", True)])
        # vision witness of the rollback: the frame captured after the restore equals the base frame
        frames = sorted(int(n[6:-5]) for n in os.listdir(os.path.join(c.rt, "frames")) if n.endswith(".json"))
        after_restore = os.path.join(c.rt, "frames", "frame_%d.png" % frames[2])
        self.assertEqual(fakes.fake_differ(f0, after_restore), [])
        self.assertEqual(store.get("p1")["digest"], c.digest())

    def test_all_rejected_leaves_base_state(self):
        c = self.cap()
        store, st = genesis(c)
        self.assertEqual(run_txn(c, store, st, "props-agent-1", [WRONG, WRONG + " "]), RW.ALL_REJECTED)
        self.assertEqual(c.digest(), store.get("p0")["digest"])
        self.assertEqual((st.scene_epoch, st.confirmed_tick, st.last_good, st.violations), (3, 0, "p0", []))

    def test_red_without_restore(self):
        """Negative control: restore replaced by a no-op -> digest != base -> FROZEN, wrong edit still there."""
        c = self.cap()
        store, st = genesis(c)
        self.assertEqual(run_txn(c, store, st, "props-agent-1", [WRONG, GOOD], restore=fakes.noop_restore_script), "FROZEN")
        self.assertEqual(c.events("control", op="rewind")[0]["digest_match"], False)
        self.assertTrue(RW.replay(c.events()).frozen)
        with self.assertRaises(AssertionError):
            self.assertEqual(c.scene["col:CHAR_mesh"], [])

    def test_red_without_judge(self):
        """Negative control (dead oracle): an accept-all judge commits a wrong edit that stays inside the
        write_set (so F1's spill verdict cannot catch it); the final state assertion must fail."""
        wrong_in_set = "scene['col:PROPS'].append('table10x')"       # inside write_set: only the probe catches it
        c = self.cap()
        store, st = genesis(c)
        self.assertEqual(run_txn(c, store, st, "props-agent-1", [wrong_in_set, GOOD]), "COMMIT")
        self.assertEqual(c.scene["col:PROPS"], ["table"])
        c2 = self.cap()
        store2, st2 = genesis(c2)
        self.assertEqual(run_txn(c2, store2, st2, "props-agent-1", [wrong_in_set, GOOD], judge=fakes.accept_all_judge), "COMMIT")
        with self.assertRaises(AssertionError):
            self.assertEqual(c2.scene["col:PROPS"], ["table"])

    def test_probe_that_mutates_is_rejected(self):
        c = self.cap()
        store, st = genesis(c)
        b = c.broker
        it = {"event_id": "props-agent-1", "lane": "script", "write_set": ["col:PROPS"], "script": GOOD,
              "expect": {"probe": "scene['col:PROPS'].append('x'); " + PROBE, "equals": ["table"]}}
        c.control("digest", GOOD, 1)
        after = c.digest()

        def run_probe(src):
            r = c.control("probe", src, 1)
            return {"ok": r["ok"], "result": r["result"]["value"], "digest": c.digest()}

        j = fakes.fake_judge({"attempt": "props-agent-1.a1", "intent": it, "script": GOOD, "verdict": "STATE_VERIFIED", "extra": {}},
                             {"point_id": "p0", "digest": store.get("p0")["digest"], "frame_png": None},
                             {"digest": after, "frame_png": None, "job_ms": 1}, run_probe=run_probe)
        self.assertFalse(j["pick"])
        self.assertEqual(j["oracles"][0], RW.oracle("expect", False, RW.PROBE_NOT_READONLY))
        self.assertIsNotNone(b)

    def test_tampered_checkpoint_refused(self):
        c = self.cap()
        store, _ = genesis(c)
        p = store.verify("p0")
        with open(p, "r+b") as f:
            f.write(b"X")                                              # 1 byte flipped
        with self.assertRaises(RW.CkptError) as cm:
            store.verify("p0")
        self.assertEqual(cm.exception.code, RW.CKPT_CORRUPT)
        weak = fakes.FakeStore(c.rt, check_sha=False)                  # negative control: sha check removed
        weak._recs = store._recs
        with self.assertRaises(AssertionError):
            with self.assertRaises(RW.CkptError):
                weak.verify("p0")
        with self.assertRaises(RW.CkptError) as cm:
            store.verify("p9")
        self.assertEqual(cm.exception.code, RW.CKPT_MISSING)

    def test_store_budget_and_missing(self):
        c = self.cap()
        store = fakes.FakeStore(c.rt, budget_bytes=1)
        with self.assertRaises(RW.CkptError) as cm:
            store.admit("p0", 0, 1, c.digest(), 0)
        self.assertEqual(cm.exception.code, RW.CKPT_MISSING)
        c.control("mark", fakes.mark_script(store.path_for("p0")), "p0")
        with self.assertRaises(RW.CkptError) as cm:
            store.admit("p0", 0, 1, c.digest(), 0)
        self.assertEqual(cm.exception.code, RW.CKPT_BUDGET)

    def test_control_job_unauthenticated_answer_is_not_ok(self):
        c = self.cap(with_bridge=False, cap=0.3)
        jid = RW.control_job_id("restore", "p0", "e2")
        with open(os.path.join(c.rt, "outbox", jid + ".out.tmp"), "w") as f:
            f.write("OK\nMETA {\"mac\": \"forged\"}\n" + RW.result_line({"digest": c.digest()}) + "\n")
        import threading
        t = threading.Timer(0.1, os.replace, (os.path.join(c.rt, "outbox", jid + ".out.tmp"),
                                              os.path.join(c.rt, "outbox", jid + ".out")))
        t.start()
        r = RW.dispatch_control(c.broker, jid, fakes.restore_script("/nonexistent"))
        t.join()
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "OUTCOME_UNCERTAIN")
        self.assertIsNone(r["result"])
        self.code("BAD_JOB", RW.dispatch_control, c.broker, "props-agent-1", "pass")

    def test_restore_load_ui_flag_and_gui_key_in_digest(self):
        c = self.cap()
        store, _ = genesis(c)
        c.vision.act(["--key", "KP_1"])
        self.assertEqual(RW.digest_delta(store.get("p0")["digest"], c.digest()), ["ui:view3d"])
        r = c.control("restore", fakes.restore_script(store.verify("p0"), load_ui=False), "p0", "e2")
        self.assertFalse(RW.digest_match(r["result"]["digest"], store.get("p0")["digest"]))
        r = c.control("restore", fakes.restore_script(store.verify("p0"), load_ui=True), "p0", "e3")
        self.assertTrue(RW.digest_match(r["result"]["digest"], store.get("p0")["digest"]))

    def test_f1_broker_recovers_over_rewind_records(self):
        """F1 is imported, not modified: its _recover must skip the four new record types."""
        c = self.cap()
        store, st = genesis(c)
        run_txn(c, store, st, "props-agent-1", [WRONG, GOOD])
        c.put("modeler-agent", "modeler-agent-1")
        self.assertEqual(c.broker.run(1), ["STATE_VERIFIED"])
        seq, tick, mode = c.broker.seq, c.broker.tick, c.broker.mode
        b = c.restart_broker()
        self.assertEqual((b.seq, b.mode), (seq, mode))
        self.assertGreaterEqual(tick, b.tick)        # F1 only counts its own close records
        self.assertEqual(RW.replay(c.events()).summary(), st.summary())


def _has_commit(rev):
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        p = subprocess.run(["git", "cat-file", "-e", f"{rev}^{{commit}}"], cwd=root,
                           capture_output=True, text=True)
        return p.returncode == 0
    except OSError:
        return False


class F1Untouched(Base):
    @unittest.skipUnless(_has_commit("b255057"), "commit b255057 not in repository history")
    def test_ref_broker_unchanged_since_b255057(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        p = subprocess.run(["git", "diff", "--stat", "b255057", "--", "product/ref_broker"], cwd=root,
                           capture_output=True, text=True)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, ""))

    def test_f1_hook_signatures_pinned(self):
        import inspect
        import broker as B
        self.assertEqual(list(inspect.signature(B.Broker._dispatch_script).parameters), ["self", "ev"])
        self.assertEqual(list(inspect.signature(B.Broker._append).parameters), ["self", "rec"])
        self.assertEqual(list(inspect.signature(B.Broker._capture).parameters), ["self", "by_event", "regions"])
        self.assertEqual(list(inspect.signature(B.Broker.control).parameters), ["self", "op", "by", "reason"])
        for name in ("step", "admit", "_recover", "ingest", "_validate"):
            self.assertTrue(callable(getattr(B.Broker, name)))


if __name__ == "__main__":
    unittest.main()
