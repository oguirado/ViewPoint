"""R4 unit tests (no Blender): rewindctl audit/status/emit-script and the live-proof fixtures.

Fixtures are the REAL logs of the live runs under evidence/ (nominal + the two must-FAIL runs).
Negative controls: every audit check has a log on which it is the deciding check; with that check
skipped (audit(..., skip=)) the same log is reported differently, so a removed check turns a test red.
"""
import copy
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rewindctl as CTL      # noqa: E402
import rewind_live as LIVE   # noqa: E402
import schema as RW          # noqa: E402
import judge as J            # noqa: E402
import ops as R1OPS          # noqa: E402

EV = os.path.join(HERE, "evidence")
REPO = os.path.dirname(CTL.PRODUCT)


def log(name):
    return RW.read_events(os.path.join(EV, name, "events.jsonl"))


def checks(out):
    return sorted(set(p[0] for p in out["problems"]))


class AuditRealLogs(unittest.TestCase):
    def test_nominal_log_is_clean(self):
        out = CTL.audit(log("nominal"))
        self.assertTrue(out["ok"], out["problems"])
        self.assertEqual(out["rollbacks"], ["e1.a1"])
        self.assertEqual([c[0] for c in out["commits"]], ["m1.a1", "e1.a2", "g2.a1"])
        self.assertEqual(out["summary"]["scene_epoch"], 2)
        self.assertEqual(out["summary"]["last_good"], "p3")

    def test_no_restore_log_is_red(self):
        out = CTL.audit(log("mutant_no_restore"))
        self.assertFalse(out["ok"])
        self.assertIn("closed", checks(out))

    def test_nc_closed_check_removed_passes_the_no_restore_log(self):
        out = CTL.audit(log("mutant_no_restore"), skip=("closed",))
        self.assertNotIn("closed", checks(out))
        self.assertLess(len(out["problems"]), len(CTL.audit(log("mutant_no_restore"))["problems"]))

    def test_accept_all_log_is_red_on_commit_delta(self):
        out = CTL.audit(log("mutant_judge_accept_all"))
        self.assertEqual(checks(out), ["commit_delta"])
        self.assertIn("col:CHAR_mesh", out["problems"][0][2])

    def test_nc_commit_delta_removed_accepts_the_wrong_edit(self):
        self.assertTrue(CTL.audit(log("mutant_judge_accept_all"), skip=("commit_delta",))["ok"])


class AuditMutatedLogs(unittest.TestCase):
    """One synthetic mutation of the nominal log per check; the check must report it, and must be
    silent when skipped (control)."""

    def setUp(self):
        self.recs = copy.deepcopy(log("nominal"))

    def decided_by(self, check):
        full = CTL.audit(self.recs)
        self.assertFalse(full["ok"])
        self.assertIn(check, checks(full))
        without = CTL.audit(self.recs, skip=(check,))
        self.assertNotIn(check, checks(without))
        self.assertLess(len(without["problems"]), len(full["problems"]))      # the check carried findings
        self.assertTrue(CTL.audit(self.recs, skip=CTL.AUDIT_CHECKS)["ok"])     # and nothing else is hidden

    def test_rewind_without_digest_match(self):
        for r in self.recs:
            if r.get("rec") == "control" and r.get("op") == "rewind":
                r["digest_match"] = False
        self.decided_by("rollback_restored")
        self.assertIn("replay", checks(CTL.audit(self.recs)))    # R0 fold agrees (ROLLBACK_WITHOUT_RESTORE)

    def test_rewind_record_missing(self):
        self.recs = [r for r in self.recs if not (r.get("rec") == "control" and r.get("op") == "rewind")]
        self.decided_by("rollback_restored")

    def test_commit_without_judge_pick(self):
        self.recs = [r for r in self.recs if not (r.get("rec") == "judge" and r.get("attempt") == "e1.a2")]
        self.decided_by("commit_judged")

    def test_commit_on_a_stale_base(self):
        for r in self.recs:
            if r.get("rec") == "txn" and r.get("attempt") == "g2.a1":
                r["base_point"] = "p0"
        self.decided_by("commit_chain")

    def test_commit_with_undeclared_delta(self):
        for r in self.recs:
            if r.get("rec") == "enqueue" and r.get("event_id") == "e1":
                r["write_set"] = ["col:LOOK_lights"]
        self.decided_by("commit_delta")

    def test_open_txn_at_the_end(self):
        last = max(i for i, r in enumerate(self.recs) if r.get("rec") == "txn" and r.get("phase") == "commit")
        self.recs = self.recs[:last]
        self.decided_by("closed")

    def test_epoch_not_bumped_is_a_replay_violation(self):
        for r in self.recs:
            if r.get("rec") == "control" and r.get("op") == "rewind":
                r["scene_epoch_after"] = 1
        self.decided_by("replay")


class StatusAndCli(unittest.TestCase):
    def test_status_of_the_real_logs(self):
        s = CTL.status(log("nominal"))
        self.assertEqual((s["mode"], s["scene_epoch"], s["last_good"], s["open_txn"]), ("IDLE", 2, "p3", None))
        self.assertEqual(s["last_rewind"]["to"], "p1")
        self.assertIs(s["last_rewind"]["digest_match"], True)
        s = CTL.status(log("mutant_no_restore"))
        self.assertEqual((s["mode"], s["open_txn"], s["frozen"]), ("FROZEN", "e1.a1", True))

    def cli(self, *args):
        p = subprocess.run([sys.executable, os.path.join(HERE, "rewindctl.py")] + list(args), capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout

    def test_audit_cli_exit_codes(self):
        rc, out = self.cli("audit", "--events", os.path.join(EV, "nominal", "events.jsonl"))
        self.assertEqual((rc, json.loads(out)["ok"]), (0, True))
        for m in ("mutant_no_restore", "mutant_judge_accept_all"):     # must-FAIL: exit non-zero
            rc, out = self.cli("audit", "--events", os.path.join(EV, m, "events.jsonl"))
            self.assertEqual((rc, json.loads(out)["ok"]), (1, False), m)
        rc, _ = self.cli("audit", "--events", os.path.join(EV, "absent.jsonl"))
        self.assertEqual(rc, 2)

    def test_emit_script_is_r1_ops_text(self):
        rc, out = self.cli("emit-script", "mark", "--point-path", "/dev/shm/vqa-cu/ref-rwX/ckpt/p9.blend")
        self.assertEqual((rc, out), (0, R1OPS.mark_script("/dev/shm/vqa-cu/ref-rwX/ckpt/p9.blend")))
        rc, out = self.cli("emit-script", "restore", "--point-path", "/dev/shm/vqa-cu/ref-rwX/ckpt/p9.blend", "--sha256", "ab" * 32)
        self.assertEqual(out, R1OPS.restore_script("/dev/shm/vqa-cu/ref-rwX/ckpt/p9.blend", True, sha256="ab" * 32))
        self.assertIn("open_mainfile", out)
        rc, out = self.cli("emit-script", "digest")
        self.assertEqual((rc, out), (0, R1OPS.digest_script()))
        self.assertIn(RW.RESULT_PREFIX, out)
        p = subprocess.run([sys.executable, os.path.join(HERE, "rewindctl.py"), "emit-script", "restore", "--point-path", "rel.blend"],
                           capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)


class Scenario(unittest.TestCase):
    def test_props_intent_is_a_valid_rw1_intent(self):
        it = {"v": 1, "rec": "enqueue", "event_id": "e1", "agent_id": "props-agent", "lane": "script",
              "write_set": ["col:PROPS_set"], "script": LIVE.PROPS_WRONG, "alternatives": [LIVE.PROPS_GOOD],
              "expect": {"probe": LIVE.PROBE_UNDER, "equals": True}}
        self.assertIsNone(RW.validate_intent_ext(it))
        self.assertEqual([a for a, _ in RW.attempt_scripts(it)], ["e1.a1", "e1.a2"])
        bad = dict(it, op="rewind")
        self.assertEqual(RW.validate_intent_ext(bad)[0], "NO_LEASE")     # agents cannot ask for a rewind

    def test_mutants_really_break_the_guard(self):
        self.assertIn("open_mainfile", R1OPS.restore_script("/x/p1.blend", True))
        self.assertNotIn("open_mainfile", LIVE.NoRestoreOps.restore_script("/x/p1.blend", True))
        j = LIVE.accept_all_judge({"attempt": "e1.a1"}, {}, {})
        RW.validate_record(j)
        self.assertIs(j["pick"], True)

    def test_real_judge_rejects_what_the_accept_all_mutant_took(self):
        recs = log("mutant_judge_accept_all")
        pts = RW.replay(recs).points
        intent = next(r for r in recs if r.get("rec") == "enqueue" and r.get("event_id") == "e1")
        att = {"attempt": "e1.a1", "intent": intent, "verdict": "STATE_VERIFIED", "extra": {}}
        j = J.judge(att, {"digest": pts["p1"]["digest"]}, {"digest": pts["p2"]["digest"], "job_ms": 1},
                    run_probe=lambda s: {"ok": True, "result": False, "digest": pts["p2"]["digest"]})
        self.assertIs(j["pick"], False)
        self.assertEqual(J.reject_reason(j).split(":")[0], "expect")

    def test_rollback_witness_on_the_real_frames(self):
        d = os.path.join(EV, "nominal")
        ok = J.rollback_witness(os.path.join(d, "frame_p1.png"), os.path.join(d, "frame_restored_p1.png"), differ=LIVE.witness_differ)
        self.assertTrue(ok["ok"], ok)
        wrong = J.rollback_witness(os.path.join(d, "frame_p1.png"), os.path.join(d, "frame_e1a1_wrong.png"), differ=LIVE.witness_differ)
        self.assertFalse(wrong["ok"])                                      # control: the wrong edit is visible
        self.assertIn(J.WITNESS_NOT_RESTORED, wrong["detail"])

    def test_live_refuses_foreign_rt_and_job(self):
        for args in (["--job", "vprw4", "--rt", "/dev/shm/vqa-cu/hybrid"], ["--job", "other", "--rt", "/dev/shm/vqa-cu/ref-rw4-x"]):
            p = subprocess.run([sys.executable, os.path.join(HERE, "rewind_live.py"), "--evidence", "/nonexistent"] + args,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 2, p.stdout)
            self.assertIn("refused", p.stdout)


class Untouched(unittest.TestCase):
    """R4 integrates by import: F1 and R0-R3 are byte-identical to their own branches."""

    def diff(self, ref, path):
        p = subprocess.run(["git", "-C", REPO, "diff", "--stat", ref, "--", path], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def test_f1_and_units_untouched(self):
        for ref, path in (("b255057", "product/ref_broker"), ("p-fable-r0", "product/ref_rewind"),
                          ("p-fable-r1", "product/ref_rewind_ckpt"), ("p-fable-r2", "product/ref_rewind_lanes"),
                          ("p-fable-r3", "product/ref_rewind_verify")):
            self.assertEqual(self.diff(ref, path), "", "%s differs from %s" % (path, ref))


if __name__ == "__main__":
    unittest.main()
