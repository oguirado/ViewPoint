"""R2 tests: RewindBroker on the real F1 broker + real F1 bridge core on a thread, fake scene/vision/
store/judge from R0 fakes (no Blender). Track P (claude-fable-5-2).

    python3 -m unittest -v test_lanes        # from this directory

Every guarded property is a function prop_*(obs) -> bool. A positive test asserts it on RewindBroker;
the matching negative control (test_nc_*) runs the same scenario on a mutant with the guard removed
and asserts the property is then violated (so the positive test would go red without the guard).
"""
import hashlib
import hmac
import inspect
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "ref_rewind"))
import fakes  # noqa: E402
import schema as RW  # noqa: E402
import broker as B  # noqa: E402
from rewind_broker import RewindBroker, NOT_CONFIRMED  # noqa: E402

GOOD = "scene['col:PROPS'].append('table')"
GOOD2 = "scene['col:PROPS'].append('table2')"
WRONG = "scene['col:PROPS'].append('table10x')"                                   # judge reject (probe)
SPILL = "scene['col:PROPS'].append('t'); scene['col:CHAR_mesh'].append('moved')"   # F1 VERIFIED_WITH_SPILL
RAISE = "scene['col:PROPS'].append('half'); raise RuntimeError('boom')"            # F1 JOB_ERROR, partial effect
RAISE_CLEAN = "raise RuntimeError('boom')"                                         # JOB_ERROR, no pixel changes
SLOW = "scene['col:PROPS'].append('late'); time.sleep(1.2)"                        # outlives the job cap
PROBE = "print(rw_result({'value': scene['col:PROPS']}))"
EXPECT = {"probe": PROBE, "equals": ["table"]}
PROPS = dict(write_set=("col:PROPS",))


class Crash(Exception):
    """Stands for the broker process dying mid-transaction."""


class Cap(fakes.Capsule):
    """fakes.Capsule with a RewindBroker wired to the fake ops/store/judge."""

    def __init__(self, cls=RewindBroker, ops=fakes.FakeOps, judge=fakes.fake_judge, store_kw=None, cap=5.0, **bkw):
        rt = tempfile.mkdtemp(prefix="vprw_r2_")
        self._rt_made = rt
        self.store = fakes.FakeStore(rt, **(store_kw or {}))
        kw = {"ops": ops, "store": self.store, "judge": judge}
        kw.update(bkw)
        fakes.Capsule.__init__(self, cls=cls, rt=rt, cap=cap, broker_kw=kw)

    def close(self):
        fakes.Capsule.close(self)
        shutil.rmtree(self._rt_made, ignore_errors=True)

    def run_until(self, eid, max_steps=16):
        """Step until the event has a non-ingest close record; returns the step verdicts."""
        out = []
        for _ in range(max_steps):
            out.append(self.broker.step())
            if any(not r.get("ingest") and not r.get("requeued") for r in self.events("close", event_id=eid)):
                return out
            if self.broker.mode != "IDLE":
                return out
        raise AssertionError("event %s not closed after %d steps: %s" % (eid, max_steps, out))

    def recs(self):
        """Compact view of the rewind records in log order."""
        out = []
        for r in RW.read_events(os.path.join(self.rt, "events.jsonl")):
            if r.get("rec") == "txn":
                out.append("txn.%s:%s:%s" % (r["phase"], r["attempt"], r["verdict"]))
            elif r.get("rec") == "judge":
                out.append("judge:%s:%s" % (r["attempt"], r["pick"]))
            elif r.get("rec") == "ckpt":
                out.append("ckpt:%s" % r["point_id"])
            elif r.get("rec") == "control":
                out.append("control.%s:%s" % (r["op"], r.get("to") or r.get("by")))
            elif r.get("rec") == "close" and not r.get("ingest") and r.get("event_id"):
                out.append("close:%s:%s" % (r["event_id"], r["verdict"]))
        return out

    def audit(self):
        return RW.replay(RW.read_events(os.path.join(self.rt, "events.jsonl")))


class Base(unittest.TestCase):
    def cap(self, **kw):
        c = Cap(**kw)
        self.addCleanup(c.close)
        return c

    def assertClean(self, c):
        self.assertEqual(c.audit().violations, [])


# ---------------------------------------------------------------- properties (shared by tests and negative controls)
def scen_rollback(c):
    """A failing first attempt, nothing else: the capsule must be back on the base afterwards."""
    c.broker.step()                                  # modeler PASS (marks genesis p0)
    base = dict(c.digest())
    c.put("props-agent", "e1", script=RAISE, **PROPS)
    c.run_until("e1")
    return {"base": base, "after": c.digest(), "verdicts": c.verdicts("e1"), "mode": c.broker.mode}


def prop_digest_equals_base(obs):
    return obs["verdicts"] == [RW.ALL_REJECTED] and RW.digest_match(obs["after"], obs["base"])


def scen_stale_gui(c):
    """A gui intent is queued, then another lane's attempt is rolled back without any pixel change
    (F1's own frame check would auto-rebase the intent: only the epoch bump can stop it)."""
    c.broker.step()
    c.put("camera-agent", "g1", lane="gui", keys=["KP_1"], write_set=())
    c.put("props-agent", "e1", script=RAISE_CLEAN, **PROPS)
    for _ in range(4):                                # props, look, camera, modeler
        c.broker.step()
    return {"gui": c.verdicts("g1"), "acts": list(c.vision.acts), "epoch": c.broker.scene_epoch}


def prop_stale_gui_not_dispatched(obs):
    return obs["gui"] == ["STALE_FRAME"] and obs["acts"] == []


def scen_frontier(c):
    """Ask for an export from inside an open transaction (tick not confirmed yet)."""
    seen = {}

    def judge(attempt, base, after, run_probe=None):
        b = c.broker
        seen["inside"] = b.export_point(b.tick + 1)
        seen["inside_prev"] = b.export_point(b.tick)
        seen["proj_inside"] = b.projection()
        return fakes.fake_judge(attempt, base, after, run_probe=run_probe)

    c.broker.judge = judge
    c.broker.step()
    c.put("props-agent", "e1", script=GOOD, **PROPS)
    c.run_until("e1")
    seen["after"] = c.broker.export_point(1)
    seen["beyond"] = c.broker.export_point(2)
    return seen


def prop_frontier_holds(obs):
    return (obs["inside"]["ok"] is False and obs["inside"]["code"] == NOT_CONFIRMED
            and obs["inside_prev"]["ok"] is False and obs["proj_inside"] == []
            and obs["beyond"]["ok"] is False and obs["after"]["ok"] is True)


# ---------------------------------------------------------------- mutants (guards removed)
class NoDigestCheck(RewindBroker):
    def _digest_ok(self, got, point):
        return True


class NoEpochBump(RewindBroker):
    def _bump_epoch(self, r):
        pass


class NoFrontier(RewindBroker):
    def _frontier_ok(self, tick):
        return True


class NoExtValidation(RewindBroker):
    def _validate(self, agent, it):
        return B.Broker._validate(self, agent, it)


class NoPreDispatch(RewindBroker):
    def _pre_dispatch(self):
        if self.rw.last_good is None:
            return RewindBroker._pre_dispatch(self)
        return None


# ---------------------------------------------------------------- tests
class F1Contract(Base):
    """Pins the F1 surface RewindBroker overrides or calls (rw-build 'Open risks': private methods)."""

    SIGS = {"__init__": "(self, rt, job, nonce, vision=None, agents=AGENTS, differ=diff_regions, job_cap_s=JOB_CAP_S, poll_s=0.1, log=None)",
            "step": "(self)", "_dispatch_script": "(self, ev)", "_dispatch_gui": "(self, ev)",
            "_check_fresh": "(self, ev)", "_validate": "(self, agent, it)", "_append": "(self, rec)",
            "_capture": "(self, by_event, regions)", "control": "(self, op, by, reason)",
            "_late_outbox_scan": "(self)", "admit": "(self, ev)", "ingest": "(self)"}

    def test_signatures(self):
        for name, want in self.SIGS.items():
            src = inspect.getsource(getattr(B.Broker, name))
            got = " ".join(src.split("def %s" % name, 1)[1].split(":\n", 1)[0].split())
            self.assertEqual(got, want, name)

    def test_step_uses_the_hooks(self):
        src = inspect.getsource(B.Broker.step)
        for needle in ("self.admit(ev)", "self._dispatch_script(ev)", "self._dispatch_gui(ev)",
                       "self._capture(eid,", "self.tick += 1", 'verdict in ("OUTCOME_UNCERTAIN", "INPUT_UNCERTAIN")'):
            self.assertIn(needle, src)
        self.assertIn("self._check_fresh", inspect.getsource(B.Broker.admit))
        self.assertIn("self._validate(agent, it)", inspect.getsource(B.Broker.ingest))
        self.assertIn("self.expired", inspect.getsource(B.Broker._late_outbox_scan))

    def test_f1_untouched(self):
        import subprocess
        p = subprocess.run(["git", "diff", "--stat", "b255057", "--", "product/ref_broker"],
                           cwd=os.path.dirname(os.path.dirname(HERE)), capture_output=True, text=True)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, ""))

    def test_needs_units(self):
        rt = tempfile.mkdtemp(prefix="vprw_r2_")
        self.addCleanup(shutil.rmtree, rt, True)
        with self.assertRaises(B.BrokerError):
            RewindBroker(rt, "j", "n", vision=None)


class Commit(Base):
    def test_accept_commits_and_marks(self):
        c = self.cap()
        c.broker.step()
        c.put("props-agent", "e1", script=GOOD, expect=EXPECT, **PROPS)
        c.run_until("e1")
        self.assertEqual(c.recs(), ["ckpt:p0", "txn.begin:e1.a1:None", "judge:e1.a1:True", "ckpt:p1",
                                    "txn.commit:e1.a1:STATE_VERIFIED", "close:e1:STATE_VERIFIED"])
        b = c.broker
        self.assertEqual((b.rw.last_good, b.rw.confirmed_tick, b.scene_epoch, b.tick), ("p1", 1, 1, 1))
        commit = c.events("txn", phase="commit")[0]
        close = c.events("close", event_id="e1")[0]
        self.assertEqual((commit["tick"], commit["point"], close["tick"], close["point"]), (1, "p1", 1, "p1"))
        self.assertEqual(c.events("ckpt", point_id="p1")[0]["digest"], c.digest())
        self.assertClean(c)

    def test_lanes_are_serial_txns_from_last_good(self):
        c = self.cap()
        c.put("modeler-agent", "m1")
        c.put("props-agent", "e1", script=GOOD, **PROPS)
        for _ in range(3):
            c.broker.step()
        c.put("camera-agent", "g1", lane="gui", keys=["KP_1"], write_set=())   # cites the head frame
        c.broker.step()
        begins = [(r["attempt"], r["base_point"]) for r in c.events("txn", phase="begin")]
        self.assertEqual(begins, [("m1.a1", "p0"), ("e1.a1", "p1"), ("g1.a1", "p2")])
        self.assertEqual(c.verdicts("g1"), ["VERIFIED"])
        self.assertEqual((c.broker.rw.last_good, c.broker.rw.confirmed_tick), ("p3", 3))
        self.assertClean(c)

    def test_mark_refused_is_rolled_back(self):
        c = self.cap(store_kw={"budget_bytes": 120})       # p0 fits, p1 does not
        c.broker.step()
        base = dict(c.digest())
        c.put("props-agent", "e1", script=GOOD, alternatives=[GOOD2], **PROPS)
        c.run_until("e1")
        self.assertEqual(c.verdicts("e1"), [RW.CKPT_BUDGET])
        self.assertEqual(c.digest(), base)
        self.assertEqual(len(c.events("txn", phase="begin")), 1)   # stall guard: no further attempt
        self.assertEqual(c.broker.rw.last_good, "p0")
        self.assertClean(c)


class Row1Rejects(Base):
    def test_job_error_then_alternative(self):
        c = self.cap()
        c.broker.step()
        c.put("props-agent", "e1", script=RAISE, alternatives=[GOOD], expect=EXPECT, **PROPS)
        c.run_until("e1")
        self.assertEqual(c.recs(), ["ckpt:p0", "txn.begin:e1.a1:None", "control.rewind:p0", "txn.rollback:e1.a1:JOB_ERROR",
                                    "txn.begin:e1.a2:None", "judge:e1.a2:True", "ckpt:p1",
                                    "txn.commit:e1.a2:STATE_VERIFIED", "close:e1:STATE_VERIFIED"])
        rw = c.events("control", op="rewind")[0]
        self.assertEqual((rw["digest_match"], rw["scene_epoch_after"], rw["by"], rw["attempt"]), (True, 2, "broker", "e1.a1"))
        self.assertEqual(c.scene["col:PROPS"], ["table"])          # 'half' is gone, a2 applied on the base
        self.assertEqual([r["base_point"] for r in c.events("txn", phase="begin")], ["p0", "p0"])
        self.assertEqual(c.events("ckpt", point_id="p1")[0]["scene_epoch"], 2)
        self.assertClean(c)

    def test_spill_none_left_all_rejected_state_is_base(self):
        c = self.cap()
        c.broker.step()
        base = dict(c.digest())
        c.put("props-agent", "e1", script=SPILL, **PROPS)
        c.run_until("e1")
        self.assertEqual(c.recs()[1:], ["txn.begin:e1.a1:None", "control.rewind:p0",
                                        "txn.rollback:e1.a1:VERIFIED_WITH_SPILL", "close:e1:ALL_REJECTED"])
        self.assertEqual(c.digest(), base)
        self.assertEqual((c.broker.rw.last_good, c.broker.rw.confirmed_tick, c.broker.mode), ("p0", 0, "IDLE"))
        self.assertClean(c)

    def test_judge_reject_then_alternative(self):
        c = self.cap()
        c.broker.step()
        c.put("props-agent", "e1", script=WRONG, alternatives=[SPILL, GOOD], expect=EXPECT, **PROPS)
        c.run_until("e1")
        self.assertEqual([x for x in c.recs() if x.startswith(("txn", "judge"))],
                         ["txn.begin:e1.a1:None", "judge:e1.a1:False", "txn.rollback:e1.a1:JUDGE_REJECT",
                          "txn.begin:e1.a2:None", "txn.rollback:e1.a2:VERIFIED_WITH_SPILL",
                          "txn.begin:e1.a3:None", "judge:e1.a3:True", "txn.commit:e1.a3:STATE_VERIFIED"])
        self.assertEqual((c.scene["col:PROPS"], c.scene["col:CHAR_mesh"], c.broker.scene_epoch), (["table"], [], 3))
        self.assertClean(c)

    def test_rollback_restores_base_digest(self):
        self.assertTrue(prop_digest_equals_base(scen_rollback(self.cap())))

    def test_nc_noop_restore_breaks_digest_equals_base(self):
        """Negative control: restore replaced by a no-op (and the broker's digest check removed, or it
        would freeze instead): 'digest equals base' must fail."""
        class NoopOps(fakes.FakeOps):
            restore_script = staticmethod(fakes.noop_restore_script)
        obs = scen_rollback(self.cap(cls=NoDigestCheck, ops=NoopOps))
        self.assertEqual(obs["verdicts"], [RW.ALL_REJECTED])
        self.assertFalse(prop_digest_equals_base(obs))


class Row2Uncertain(Base):
    def test_outcome_uncertain_recovers_without_human(self):
        c = self.cap(cap=0.5, restore_cap_s=5.0)
        c.broker.step()
        c.put("props-agent", "e1", script=SLOW, alternatives=[GOOD], expect=EXPECT, **PROPS)
        c.run_until("e1")
        self.assertEqual([x for x in c.recs() if x.startswith(("txn", "control", "close"))],
                         ["txn.begin:e1.a1:None", "control.rewind:p0", "txn.rollback:e1.a1:OUTCOME_UNCERTAIN",
                          "txn.begin:e1.a2:None", "txn.commit:e1.a2:STATE_VERIFIED", "close:e1:STATE_VERIFIED"])
        self.assertEqual(c.broker.mode, "IDLE")                    # no RECONCILE, no human resume
        self.assertEqual(c.events("control", op="reconcile"), [])
        self.assertEqual(c.scene["col:PROPS"], ["table"])          # the 'late' effect was undone
        self.assertEqual(c.ran.count("e1.a1"), 1)                  # never repeated in place
        c.broker.step()                                            # the late outbox of a1 is quarantined
        self.assertEqual([r["verdict"] for r in c.events("close", event_id="e1.a1")], ["LEASE_EXPIRED"])
        self.assertClean(c)

    def test_input_uncertain_gui_recovers_without_human(self):
        c = self.cap()
        c.vision.key_rc["KP_3"] = 5
        c.put("camera-agent", "g1", lane="gui", keys=["KP_1", "KP_3"], write_set=())
        base = None
        for _ in range(4):
            c.broker.step()
            base = base or dict(c.digest())
        self.assertEqual(c.verdicts("g1"), [RW.ALL_REJECTED])
        self.assertEqual([x for x in c.recs() if x.startswith("txn")],
                         ["txn.begin:g1.a1:None", "txn.rollback:g1.a1:INPUT_UNCERTAIN"])
        self.assertEqual(c.digest(), base)                         # KP_1 had changed the view: undone
        self.assertEqual((c.broker.mode, c.broker.scene_epoch), ("IDLE", 2))
        self.assertClean(c)


class Row3RestoreFails(Base):
    def frozen(self, **kw):
        class NoopOps(fakes.FakeOps):
            restore_script = staticmethod(fakes.noop_restore_script)
        c = self.cap(ops=NoopOps, **kw)
        c.put("modeler-agent", "m1")
        c.broker.step()
        c.put("props-agent", "e1", script=RAISE, alternatives=[GOOD], **PROPS)
        out = c.run_until("e1")
        return c, out

    def test_mismatch_freezes_and_only_director_rewind_to_last_good_leaves(self):
        c, out = self.frozen()
        b = c.broker
        self.assertEqual(out[-1], RW.RESTORE_MISMATCH)
        self.assertEqual(c.verdicts("e1"), [RW.RESTORE_MISMATCH])
        rw = c.events("control", op="rewind")[-1]
        self.assertEqual((rw["digest_match"], rw["scene_epoch_after"]), (False, 2))   # epoch bumps anyway
        self.assertEqual((b.mode, b.rw.open_txn["attempt"]), ("FROZEN", "e1.a1"))
        self.assertEqual(len(c.events("txn", phase="begin")), 2)   # m1.a1, e1.a1: a2 was never tried
        self.assertEqual(b.step(), "FROZEN")
        with self.assertRaises(B.BrokerError):
            b.control("resume", "director", "skip the restore")
        with self.assertRaises(B.BrokerError):
            b.rewind("p0")                                         # not last_good (p1)
        with self.assertRaises(B.BrokerError):
            b.rewind("p1", by="props-agent")
        self.assertEqual(b.rewind("p1")["ok"], False)              # still the no-op restore
        self.assertEqual(b.mode, "FROZEN")
        b.ops = fakes.FakeOps                                      # operator repairs the capsule
        r = b.rewind("p1", reason="operator")
        self.assertEqual((r["ok"], r["scene_epoch"]), (True, 4))
        self.assertEqual(c.digest(), c.events("ckpt", point_id="p1")[0]["digest"])
        self.assertIsNone(b.rw.open_txn)
        self.assertEqual(c.recs()[-1], "txn.rollback:e1.a1:JOB_ERROR")
        self.assertEqual(c.events("control", op="rewind")[-1]["by"], "director")
        b.control("resume", "director", "restored")
        self.assertEqual(b.step(), "PASS")
        self.assertClean(c)

    def test_frozen_survives_restart(self):
        c, _ = self.frozen()
        b = c.restart_broker()
        self.assertEqual((b.mode, b.rw.open_txn["attempt"], b.scene_epoch), ("FROZEN", "e1.a1", 2))
        self.assertEqual(b.step(), "FROZEN")
        with self.assertRaises(B.BrokerError):
            b.control("resume", "director", "skip")

    def test_corrupt_checkpoint_refuses_restore(self):
        c = self.cap()
        c.broker.step()
        with open(c.store.path_for("p0"), "ab") as f:
            f.write(b" ")                                          # tamper with the base .blend
        n_restore = lambda: sum(1 for e in c.ran if e.startswith("ctl.restore"))   # noqa: E731
        c.put("props-agent", "e1", script=RAISE, **PROPS)
        c.run_until("e1")
        self.assertEqual(c.verdicts("e1"), [RW.CKPT_CORRUPT])
        self.assertEqual((c.broker.mode, n_restore()), ("FROZEN", 0))
        self.assertIn(RW.CKPT_CORRUPT, c.events("control", op="rewind")[0]["reason"])

    def test_unauthenticated_restore_answer_freezes(self):
        c = self.cap(restore_cap_s=3.0)
        c.broker.step()
        base = dict(c.digest())
        stop = threading.Event()

        def judge(attempt, base_, after, run_probe=None):
            c.stop_bridge()                                        # the real bridge is gone ...

            def forge():                                           # ... and someone else answers the restore
                while not stop.is_set():
                    for n in os.listdir(os.path.join(c.rt, "inbox")):
                        if n.endswith(".py") and "ctl.restore" in n:
                            eid = n[:-3].split("_", 1)[1]
                            body = "OK\nMETA %s\n%s\n" % (json.dumps({"mac": hmac.new(b"guess", eid.encode(), hashlib.sha256).hexdigest()}),
                                                          RW.result_line({"op": "restore", "digest": base}))
                            with open(os.path.join(c.rt, "outbox", eid + ".out"), "w") as f:
                                f.write(body)
                            return
                    time.sleep(0.01)
            threading.Thread(target=forge, daemon=True).start()
            return RW.judge_rec(attempt["attempt"], False, [RW.oracle("delta", False, "test reject")])

        self.addCleanup(stop.set)
        c.broker.judge = judge
        c.put("props-agent", "e1", script=GOOD, **PROPS)
        c.run_until("e1")
        self.assertEqual(c.verdicts("e1"), [RW.RESTORE_UNAUTH])
        self.assertEqual(c.broker.mode, "FROZEN")
        self.assertEqual(c.events("control", op="rewind")[0]["digest_match"], False)
        self.assertEqual(c.events("txn", phase="rollback"), [])   # a forged 'restored' is not a rollback

    def test_restore_deadline_freezes(self):
        c = self.cap(restore_cap_s=0.3)
        c.broker.step()

        def judge(attempt, base_, after, run_probe=None):
            c.stop_bridge()
            return RW.judge_rec(attempt["attempt"], False, [RW.oracle("delta", False, "test reject")])

        c.broker.judge = judge
        c.put("props-agent", "e1", script=GOOD, **PROPS)
        c.run_until("e1")
        self.assertEqual(c.verdicts("e1"), [RW.RESTORE_TIMEOUT])
        self.assertEqual(c.broker.mode, "FROZEN")


class Row4Crash(Base):
    def crashed(self, cls=RewindBroker, alternatives=(GOOD,)):
        c = self.cap(cls=cls)
        c.broker.step()
        base = dict(c.digest())

        def judge(attempt, base_, after, run_probe=None):
            raise Crash("broker dies between begin and commit")

        c.broker.judge = judge
        c.put("props-agent", "e1", script=WRONG, alternatives=list(alternatives), expect=EXPECT, **PROPS)
        with self.assertRaises(Crash):
            c.broker.step()
        self.assertNotEqual(c.digest(), base)                      # the attempt's effect is in the scene
        c.broker_kw["judge"] = fakes.fake_judge
        b = c.restart_broker()
        self.assertEqual(b.rw.open_txn["attempt"], "e1.a1")
        return c, b, base

    def test_open_txn_is_undone_before_any_dispatch(self):
        c, b, base = self.crashed()
        n_jobs = len(c.ran)
        self.assertEqual(b.step(), "PASS")                         # look-agent's (empty) turn
        self.assertEqual(c.ran[n_jobs:], ["ctl.restore.p0.e2"])    # the only job: the undo
        self.assertEqual(c.digest(), base)
        self.assertEqual(c.recs()[-2:], ["control.rewind:p0", "txn.rollback:e1.a1:OUTCOME_UNCERTAIN"])
        c.run_until("e1")
        self.assertEqual(c.verdicts("e1"), ["STATE_VERIFIED"])
        self.assertEqual([r["attempt"] for r in c.events("txn", phase="begin")], ["e1.a1", "e1.a2"])   # a1 never repeated
        self.assertEqual(c.scene["col:PROPS"], ["table"])
        self.assertClean(c)

    def test_single_script_crashed_event_closes_all_rejected(self):
        c, b, base = self.crashed(alternatives=())
        c.run_until("e1")
        self.assertEqual(c.verdicts("e1"), [RW.ALL_REJECTED])
        self.assertEqual(c.digest(), base)
        self.assertEqual(c.ran.count("e1.a1"), 1)
        self.assertClean(c)

    def test_nc_recovery_removed_leaves_the_effect(self):
        """Negative control: without the pre-dispatch undo the next alternative runs on a dirty scene."""
        c, b, base = self.crashed(cls=NoPreDispatch)
        for _ in range(5):
            b.step()
        self.assertNotEqual(c.scene["col:PROPS"], ["table"])
        self.assertNotEqual(c.audit().violations, [])

    def test_crash_between_commit_and_close_applies_once(self):
        c = self.cap()
        c.broker.step()
        c.put("props-agent", "e1", script=GOOD, **PROPS)
        real_look, calls = c.vision.look, []

        def look():
            calls.append(1)
            if len(calls) == 2:                                    # 1st look: judge frame; 2nd: F1 close capture
                raise Crash("broker dies after txn commit, before the F1 close")
            return real_look()

        c.vision.look = look
        with self.assertRaises(Crash):
            c.broker.step()
        self.assertEqual(c.recs()[-1], "txn.commit:e1.a1:STATE_VERIFIED")
        c.vision.look = real_look
        c.restart_broker()
        c.run_until("e1")
        close = c.events("close", event_id="e1")[0]
        self.assertEqual((close["verdict"], close.get("recovered_commit"), close["tick"]), ("STATE_VERIFIED", True, 1))
        self.assertEqual((c.ran.count("e1.a1"), c.scene["col:PROPS"]), (1, ["table"]))
        self.assertClean(c)


    def _crash_commit_close_two_agents(self, **flags):
        """xr-core R2-1: same crash, but ANOTHER agent has queued work when the broker restarts."""
        c = self.cap()
        c.broker.step()
        c.put("props-agent", "e1", script=GOOD, **PROPS)
        c.put("look-agent", "l1", script="scene['col:LOOK'].append('key')", write_set=("col:LOOK",))
        real_look, calls = c.vision.look, []

        def look():
            calls.append(1)
            if len(calls) == 2:
                raise Crash("broker dies after txn commit, before the F1 close")
            return real_look()

        c.vision.look = look
        with self.assertRaises(Crash):
            c.broker.step()
        self.assertEqual(c.recs()[-1], "txn.commit:e1.a1:STATE_VERIFIED")
        c.vision.look = real_look
        c.restart_broker()
        for k, v in flags.items():
            setattr(c.broker, k, v)
        for _ in range(8):
            c.broker.step()
        commits = [(r["attempt"], r["tick"]) for r in c.events("txn", phase="commit")]
        closes = [(r["event_id"], r["tick"], bool(r.get("recovered_commit")))
                  for r in c.events("close") if r.get("event_id") in ("e1", "l1") and not r.get("ingest")]
        self.assertEqual(commits, [("e1.a1", 1), ("l1.a1", 2)], "two commits must not share a tick")
        self.assertEqual(closes, [("e1", 1, True), ("l1", 2, False)], "recovered commit closes first, on its tick")
        self.assertEqual((c.ran.count("e1.a1"), c.ran.count("l1.a1")), (1, 1))
        self.assertClean(c)
        proj = {p["event_id"]: p for p in c.broker.projection()}
        self.assertTrue(proj["e1"]["committed"] and proj["l1"]["committed"], proj)

    def test_crash_between_commit_and_close_other_agent_queued(self):
        self._crash_commit_close_two_agents()

    def test_nc_recovered_commit_left_to_round_robin_goes_red(self):
        with self.assertRaises(AssertionError):
            self._crash_commit_close_two_agents(close_recovered_first=False)


class TornTail(Base):
    """xr-core R2-2: the broker dies mid-append with a txn open; recovery appends a control rewind."""

    def _torn_tail_recovery(self, **bk):
        c = self.cap()
        c.broker.step()
        base = dict(c.digest())

        def judge(attempt, base_, after, run_probe=None):
            with open(os.path.join(c.rt, "events.jsonl"), "a") as f:
                f.write('{"rec": "judge", "attempt": "e1.a1", "pi')      # torn tail, no newline
            raise Crash("broker dies while writing the judge record")

        c.broker.judge = judge
        c.put("props-agent", "e1", script=GOOD, **PROPS)
        with self.assertRaises(Crash):
            c.broker.step()
        c.broker_kw["judge"] = fakes.fake_judge
        c.broker_kw.update(bk)
        b = c.restart_broker()
        self.assertEqual(b.rw.open_txn and b.rw.open_txn["attempt"], "e1.a1")
        b.step()                                                       # undo of the open txn
        self.assertEqual(c.digest(), base)
        live_epoch = b.scene_epoch
        st = c.audit()                                                 # replay from the file alone
        self.assertEqual((st.scene_epoch, len(st.rewinds)), (live_epoch, 1), "the recovery rewind must be readable")
        self.assertEqual(st.violations, [])
        self.assertEqual(c.restart_broker().scene_epoch, live_epoch, "epoch must not fall back after a restart")
        with open(os.path.join(c.rt, "events.jsonl"), "rb") as f:
            for line in f.read().splitlines():
                json.loads(line)

    def test_recovery_append_after_torn_tail_is_readable(self):
        self._torn_tail_recovery()

    def test_nc_no_tail_heal_goes_red(self):
        with self.assertRaises(AssertionError):
            self._torn_tail_recovery(heal_tail=False)


class MarkFailure(Base):
    """xr-core R2-3: the mark job of an ACCEPTED attempt is not answered (F1 TURN_TIMEOUT / uncertain)."""

    def _mark_fails(self, code, **flags):
        state = {"n": 0}

        class Flaky(RewindBroker):
            def _mark(self, tick):
                if self.rw.last_good is not None and state["n"] == 0:
                    state["n"] += 1
                    self._point_n += 1
                    return None, code           # what _mark returns for that dispatch_control code
                return RewindBroker._mark(self, tick)

        c = self.cap(cls=Flaky)
        for k, v in flags.items():
            setattr(c.broker, k, v)
        c.broker.step()
        c.put("props-agent", "e1", script=GOOD, alternatives=[GOOD2], **PROPS)
        for _ in range(8):
            c.broker.step()
        closes = [(r["verdict"], r.get("requeued")) for r in c.events("close", event_id="e1") if not r.get("ingest")]
        self.assertEqual(closes, [("CKPT_MARK_FAILED", False)], "closed once, not re-queued")
        self.assertEqual(c.ran.count("e1.a2"), 0, "no alternative may run after attempt 1 was accepted")
        self.assertEqual(c.scene["col:PROPS"], [], "accepted-but-unmarked attempt is rolled back")
        self.assertEqual(c.broker.mode, "IDLE", "a clean rollback must not leave the queue in RECONCILE")
        self.assertClean(c)

    def test_mark_timeout_is_final(self):
        self._mark_fails("TURN_TIMEOUT")

    def test_mark_uncertain_does_not_reconcile(self):
        self._mark_fails("OUTCOME_UNCERTAIN")

    def test_nc_raw_f1_code_goes_red(self):
        for code in ("TURN_TIMEOUT", "OUTCOME_UNCERTAIN"):
            with self.assertRaises(AssertionError):
                self._mark_fails(code, map_mark_failure=False)


class Row5Epoch(Base):
    def test_restore_makes_queued_gui_intents_stale(self):
        c = self.cap()
        obs = scen_stale_gui(c)
        self.assertTrue(prop_stale_gui_not_dispatched(obs), obs)
        self.assertEqual(obs["epoch"], 2)
        close = [r for r in c.events("close", event_id="g1")][0]
        self.assertEqual((close["requeued"], close["drops"]), (True, 1))
        self.assertIn("scene_epoch 2", close["reason"])
        # resubmit on the new epoch (re-observed head frame) -> VERIFIED
        c.put("camera-agent", "g1", lane="gui", keys=["KP_1"], write_set=(), based=c.broker.frame)
        c.run_until("g1")
        self.assertEqual(c.verdicts("g1"), ["STALE_FRAME", "VERIFIED"])
        self.assertEqual(c.events("txn", phase="commit")[-1]["attempt"], "g1.a1")
        self.assertClean(c)

    def test_script_intents_survive_a_restore(self):
        c = self.cap()
        c.broker.step()
        c.put("look-agent", "l1", script="scene['col:LOOK'].append('key')", write_set=("col:LOOK",))
        c.put("props-agent", "e1", script=RAISE, **PROPS)
        c.run_until("l1")
        self.assertEqual((c.verdicts("e1"), c.verdicts("l1")), ([RW.ALL_REJECTED], ["STATE_VERIFIED"]))

    def test_nc_epoch_bump_removed_dispatches_stale_gui(self):
        """Negative control: epoch bump removed -> the stale gui intent is dispatched."""
        obs = scen_stale_gui(self.cap(cls=NoEpochBump))
        self.assertFalse(prop_stale_gui_not_dispatched(obs))
        self.assertEqual(obs["gui"], ["VERIFIED"])

    def test_nc_epoch_bump_removed_is_seen_by_the_audit(self):
        c = self.cap(cls=NoEpochBump)
        c.broker.step()
        c.put("props-agent", "e1", script=RAISE, alternatives=[RAISE], **PROPS)
        c.run_until("e1")
        self.assertIn("EPOCH_NOT_BUMPED", [v[1] for v in c.audit().violations])


class Frontier(Base):
    def test_export_and_projection_stop_at_confirmed_tick(self):
        c = self.cap()
        obs = scen_frontier(c)
        self.assertTrue(prop_frontier_holds(obs), obs)
        self.assertEqual((obs["after"]["point"], obs["after"]["path"]), ("p1", c.store.path_for("p1")))
        self.assertEqual(obs["inside"]["open_txn"], "e1.a1")

    def test_rejected_tick_projects_as_not_committed(self):
        c = self.cap()
        c.broker.step()
        c.put("props-agent", "e1", script=SPILL, **PROPS)
        c.put("look-agent", "l1", script="scene['col:LOOK'].append('key')", write_set=("col:LOOK",))
        c.run_until("e1")
        self.assertEqual(c.broker.projection(), [])                # tick 1 closed ALL_REJECTED: nothing confirmed
        self.assertEqual(c.broker.export_point(1)["code"], NOT_CONFIRMED)
        c.run_until("l1")
        proj = c.broker.projection()
        self.assertEqual([(p["tick"], p["event_id"], p["verdict"], p["committed"], p["point"]) for p in proj],
                         [(1, "e1", RW.ALL_REJECTED, False, None), (2, "l1", "STATE_VERIFIED", True, "p1")])
        self.assertEqual(c.broker.export_point(1)["point"], "p0")  # state at the rejected tick = base

    def test_nc_frontier_check_removed_exports_unconfirmed_tick(self):
        """Negative control: frontier check removed -> the export of an unconfirmed tick goes through."""
        obs = scen_frontier(self.cap(cls=NoFrontier))
        self.assertTrue(obs["inside"]["ok"])
        self.assertFalse(prop_frontier_holds(obs))


class Replay(Base):
    def test_rebuild_from_events_gives_the_same_state(self):
        c = self.cap()
        c.put("modeler-agent", "m1")
        c.put("modeler-agent", "m2", script="scene['col:CHAR_mesh'].append(2)")
        c.put("props-agent", "e1", script=WRONG, alternatives=[GOOD], expect=EXPECT, **PROPS)
        c.put("props-agent", "e2", script=SPILL, **PROPS)
        c.put("camera-agent", "g1", lane="gui", keys=["KP_1"], write_set=())
        c.put("camera-agent", "g2", lane="gui", keys=["KP_3"], write_set=())
        snaps = []
        for i in range(7):
            c.broker.step()
            before = c.broker.snapshot()
            after = c.restart_broker().snapshot()
            self.assertEqual(before, after, "step %d" % i)
            snaps.append(after)
        self.assertEqual(snaps[-1]["rw"]["open_txn"], None)
        self.assertGreaterEqual(snaps[-1]["scene_epoch"], 3)
        self.assertEqual(snaps[-1]["ring"], snaps[-1]["rw"]["points"])
        self.assertClean(c)

    def test_nc_snapshot_detects_a_lost_record(self):
        """Negative control for the determinism test: drop the rewind record from the log -> the
        rebuilt state differs."""
        c = self.cap()
        c.broker.step()
        c.put("props-agent", "e1", script=RAISE, alternatives=[GOOD], **PROPS)
        c.run_until("e1")
        before = c.broker.snapshot()
        p = os.path.join(c.rt, "events.jsonl")
        with open(p) as f:
            lines = [ln for ln in f if '"op": "rewind"' not in ln]
        with open(p, "w") as f:
            f.writelines(lines)
        self.assertNotEqual(c.restart_broker().snapshot(), before)


class NoLease(Base):
    def put_raw(self, c, agent, name, obj):
        with open(os.path.join(c.rt, "intents", agent, name + ".json"), "w") as f:
            json.dump(obj, f)

    def scenario(self, c):
        c.broker.step()
        it = {"v": 1, "rec": "enqueue", "event_id": "x1", "agent_id": "props-agent", "lane": "script",
              "based_on_frame": 0, "write_set": ["col:PROPS"], "script": GOOD, "op": "rewind", "to": "p0"}
        self.put_raw(c, "props-agent", "x1", it)
        self.put_raw(c, "props-agent", "x2", RW.rewind_rec("p0", 2, True))
        self.put_raw(c, "props-agent", "x3", dict(it, event_id="ctl.restore.p0.e2", op=None))
        for _ in range(4):
            c.broker.step()
        return [(r["event_id"], r["verdict"]) for r in c.events("close") if r.get("ingest")]

    def test_agent_cannot_submit_rewind(self):
        c = self.cap()
        got = self.scenario(c)
        self.assertEqual(sorted(got, key=str), [("ctl.restore.p0.e2", "NO_LEASE"), ("x1", "NO_LEASE"), (None, "NO_LEASE")])
        self.assertEqual((c.events("control", op="rewind"), c.events("txn"), c.broker.scene_epoch), ([], [], 1))
        self.assertEqual(c.scene["col:PROPS"], [])

    def test_bad_extension_fields_refused(self):
        c = self.cap()
        c.broker.step()
        c.put("props-agent", "k1", script=GOOD, alternatives=[GOOD, GOOD, GOOD], **PROPS)
        c.put("camera-agent", "k2", lane="gui", keys=["KP_1"], write_set=(), alternatives=[GOOD])
        c.broker.step()
        self.assertEqual(sorted((r["event_id"], r["verdict"]) for r in c.events("close") if r.get("ingest")),
                         [("k1", "BAD_INTENT"), ("k2", "BAD_INTENT")])

    def test_nc_ext_validation_removed_admits_op_rewind(self):
        """Negative control: without the R0 intent check the op:rewind intent is enqueued and run."""
        c = self.cap(cls=NoExtValidation)
        got = self.scenario(c)
        self.assertNotIn(("x1", "NO_LEASE"), got)
        self.assertEqual(c.verdicts("x1"), ["STATE_VERIFIED"])


if __name__ == "__main__":
    unittest.main()
