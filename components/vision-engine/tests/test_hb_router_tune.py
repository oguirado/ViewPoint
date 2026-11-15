"""Lane R guards: router v2 tuned on the sealed blind DEV set, measured once on the sealed blind TEST set."""
import hashlib, json, os, re, shutil, subprocess, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TU = os.path.join(ROOT, "eval", "hb_router", "tune")
DEV = os.path.join(ROOT, "eval", "hb_router", "dev", "queries_all_dev.jsonl")
TEST = os.path.join(ROOT, "eval", "hb_router", "heldout", "queries_all_test.jsonl")
DEV_SHA = "b6bce6f82944eb1dced8254bdab04383a97eb992bd0b39ba94ff032ce846809b"
TEST_SHA = "cd9bc2dd880c38ddcba1a9d92012fe411fa408e6228fb35b5eee939b8950d80d"
REV = "956e1bb"


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def jload(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def script_text():
    with open(os.path.join(TU, "RUNBOOK_TUNED.md"), encoding="utf-8") as f:
        m = re.search(r"<!-- SCRIPT -->\n```bash\n(.*?)```", f.read(), re.S)
    return m.group(1) if m else None


def run_script(testcase, args, name):
    scr = os.path.join(ROOT, "build", "test_tune_%s_%d" % (name, os.getpid()))
    os.makedirs(scr)
    testcase.addCleanup(shutil.rmtree, scr, True)
    sh = os.path.join(scr, "runbook.sh")
    with open(sh, "w", encoding="utf-8") as f:
        f.write(script_text())
    r = subprocess.run(["bash", sh] + [a.replace("@", scr) for a in args], cwd=ROOT, capture_output=True, text=True, timeout=900)
    return r, scr


class Params(unittest.TestCase):
    def test_params_json_carries_tuner_receipt(self):
        p = jload(os.path.join(ROOT, "params.json"))
        self.assertEqual(p["schema"], "hb.params.v2")
        self.assertEqual(p["dev_sha256"], DEV_SHA)
        self.assertEqual(sha(DEV), DEV_SHA)
        self.assertEqual(p["grid_sha256"], sha(os.path.join(TU, "grid_hb.json")))
        self.assertIsNotNone(p["abstain"])
        self.assertEqual(set(p["abstain"]), {"m_min", "tau_cov", "tau_oov"})
        self.assertNotIn("abstain_reason", p)
        rep = jload(os.path.join(TU, "tune_report.json"))
        self.assertEqual(rep["dev_sha256"], DEV_SHA)
        self.assertEqual(rep["stage2"]["chosen"], p["find"])
        self.assertEqual(rep["stage4"]["n_impact"], 0)   # no IMPACT rows in DEV: stage skipped, declared
        self.assertIsNone(rep["stage5"])

    def test_params_not_derived_from_test(self):
        self.assertNotIn(TEST_SHA, json.dumps(jload(os.path.join(ROOT, "params.json"))))


class Blind(unittest.TestCase):
    def test_tuner_refuses_test_path(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "hbtools_v2", "hb_tune.py"), "--dev", TEST, "--out", os.path.join(ROOT, "build", "never_params.json")],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 4)
        self.assertIn("E_BLIND", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(ROOT, "build", "never_params.json")))

    def test_tuner_refuses_test_labelled_rows(self):
        scr = os.path.join(ROOT, "build", "test_tune_rows_%d" % os.getpid())
        os.makedirs(scr)
        self.addCleanup(shutil.rmtree, scr, True)
        with open(DEV, encoding="utf-8") as f:
            row = json.loads(f.readline())
        row["split"] = "TEST"
        q = os.path.join(scr, "rows.jsonl")
        with open(q, "w", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "hbtools_v2", "hb_tune.py"), "--dev", q, "--out", os.path.join(scr, "p.json")],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 4)
        self.assertIn("E_BLIND", r.stderr)

    def test_runbook_tune_mode_refuses_test_file(self):
        r, scr = run_script(self, ["tune", REV, TEST, "@/p.json", "@/w", "@/r.json"], "tm")
        self.assertEqual(r.returncode, 4)
        self.assertIn("E_BLIND", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(scr, "p.json")))

    def test_runbook_blind_guard_precedes_any_read(self):
        # the tune guard compares the resolved path to the DEV file; a relabelled TEST copy would slip past hb_tune's own guard, so it must come first
        s = script_text()
        self.assertLess(s.index("E_BLIND"), s.index("E_SEAL"))
        self.assertLess(s.index("E_BLIND"), s.index("git archive"))
        self.assertIn('[ "$(readlink -f "$Q")" != "$DEVQ" ]', s)

    def test_runbook_seal_mismatch_exits_3(self):
        scr = os.path.join(ROOT, "build", "test_tune_sealq_%d" % os.getpid())
        os.makedirs(scr)
        self.addCleanup(shutil.rmtree, scr, True)
        q = os.path.join(scr, "q.jsonl")
        shutil.copy(DEV, q)
        with open(os.path.join(scr, "q.sha256"), "w") as f:
            f.write("0" * 64 + "  q.jsonl\n")
        r, _ = run_script(self, ["eval", REV, q, "@/o.json", "@/w", "NONE"], "seal")
        self.assertEqual(r.returncode, 3)
        self.assertIn("E_SEAL", r.stderr)


class Receipts(unittest.TestCase):
    def test_tuned_test_receipt_stamped_with_both_shas(self):
        d = jload(os.path.join(TU, "run_test_tuned.json"))
        self.assertEqual(d["queries_sha256"], TEST_SHA)
        self.assertEqual(d["test_sha256"], TEST_SHA)
        self.assertEqual(d["dev_sha256"], DEV_SHA)
        self.assertEqual(d["params_sha256"], sha(os.path.join(ROOT, "params.json")))
        self.assertEqual((d["n_rows"], d["systems"], d["thin_dev"]), (120, ["v2", "B1"], False))
        self.assertEqual(d["corpus_books_sha256"], jload(os.path.join(ROOT, "eval", "hb_router", "heldout", "run_after.json"))["corpus_books_sha256"])

    def test_dev_receipts(self):
        b, a = (jload(os.path.join(TU, n)) for n in ("run_dev_before.json", "run_dev_after.json"))
        self.assertEqual((b["queries_sha256"], a["queries_sha256"]), (DEV_SHA, DEV_SHA))
        self.assertIsNone(b["params_sha256"])
        self.assertEqual(a["params_sha256"], sha(os.path.join(ROOT, "params.json")))
        self.assertEqual(b["book_md_sha256"], a["book_md_sha256"])
        self.assertEqual(b["rr"]["B1"], a["rr"]["B1"])     # control does not use params
        self.assertGreater(float(a["metrics"]["v2"]["ABSENT"]["recall"]), float(b["metrics"]["v2"]["ABSENT"]["recall"]))

    def test_result_md_matches_receipts(self):
        with open(os.path.join(TU, "RESULT_TUNED.md"), encoding="utf-8") as f:
            r = f.read()
        d = jload(os.path.join(TU, "run_test_tuned.json"))
        for s in (TEST_SHA, DEV_SHA, d["metrics"]["v2"]["FIND"]["raw"]["recall_at_5"], d["metrics"]["v2"]["ABSENT"]["recall"]):
            self.assertIn(s, r)
        self.assertIn("held-out TEST, blind author", r)


class Reproduction(unittest.TestCase):
    def _repro(self, mode_args, name, receipt):
        r, scr = run_script(self, mode_args, name)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        with open(os.path.join(scr, "o.json"), "rb") as a, open(os.path.join(TU, receipt), "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_runbook_reproduces_run_test_tuned_byte_identically(self):
        self._repro(["eval", REV, TEST, "@/o.json", "@/w", os.path.join(ROOT, "params.json")], "t", "run_test_tuned.json")

    def test_runbook_reproduces_run_dev_before_byte_identically(self):
        self._repro(["eval", REV, DEV, "@/o.json", "@/w", "NONE"], "d", "run_dev_before.json")


if __name__ == "__main__":
    unittest.main()
