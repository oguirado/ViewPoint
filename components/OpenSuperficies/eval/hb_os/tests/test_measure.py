"""measure.sh protocol guards: usage, missing/forged seal, second TEST refused. Hermetic (tmp dirs via MEASURE_* env); never builds an index."""
import hashlib, os, subprocess, tempfile, unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
SH = os.path.join(REPO, "eval", "hb_os", "measure.sh")


LBL = "before_681dc0b"


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Measure(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="measure_")
        self.ev = os.path.join(self.t, "eval")
        for d in ("dev", "heldout"):
            os.makedirs(os.path.join(self.ev, d))
        self.book = os.path.join(self.t, "book")
        os.makedirs(self.book)
        open(os.path.join(self.book, "book.md"), "w").write("# x\n")
        self.runs = os.path.join(self.t, "runs")
        os.makedirs(self.runs)
        self.seal("dev/hb_dev", b'{"qid":"a"}\n')
        self.seal("heldout/hb_test", b'{"qid":"b"}\n')

    def seal(self, stem, data):
        p = os.path.join(self.ev, stem)
        open(p + ".jsonl", "wb").write(data)
        open(p + ".sha256", "w").write("%s  %s.jsonl\n" % (sha(data), stem))

    def run_sh(self, *args):
        env = dict(os.environ, MEASURE_EVAL_DIR=self.ev, MEASURE_RUNS_DIR=self.runs, MEASURE_BUILD_DIR=os.path.join(self.t, "build"))
        p = subprocess.run(["taskset", "-c", "10-14", "timeout", "120", "bash", SH, *args], env=env, capture_output=True, text=True)
        return p.returncode, p.stderr

    def test_usage(self):
        self.assertEqual(self.run_sh()[0], 2)
        self.assertEqual(self.run_sh("only-label")[0], 2)
        self.assertEqual(self.run_sh("bad label!", self.book)[0], 2)
        self.assertEqual(self.run_sh(LBL, os.path.join(self.t, "nope"))[0], 2)

    def test_stamp_records_driver_head_sha(self):
        self.assertIn("driver_head_sha256", open(SH).read())

    def test_label_allow_list(self):
        for bad in ("x", "after2", "before2", "After", "before_681dc0b2", "test", "before-1"):
            rc, err = self.run_sh(bad, self.book)
            self.assertEqual(rc, 2, bad)
            self.assertIn("usage", err)

    def test_committed_pilot_labels_never_rerun(self):
        for lab in ("before", "after"):
            rc, err = self.run_sh(lab, self.book)
            self.assertEqual(rc, 5, lab)
            self.assertIn("E_TEST_ONCE", err)

    def test_altered_pilot_receipt_refused(self):
        for lab in ("before", "after"):
            open(os.path.join(self.runs, "run_test_%s.json" % lab), "w").write("{}")
            rc, err = self.run_sh(lab, self.book)
            self.assertEqual(rc, 5, lab)
            self.assertIn("E_SEAL", err)

    def test_missing_seal_refused(self):
        os.remove(os.path.join(self.ev, "heldout", "hb_test.sha256"))
        rc, err = self.run_sh(LBL, self.book)
        self.assertEqual(rc, 5)
        self.assertIn("E_SEAL", err)

    def test_forged_seal_refused(self):
        open(os.path.join(self.ev, "dev", "hb_dev.jsonl"), "a").write('{"qid":"c"}\n')
        rc, err = self.run_sh(LBL, self.book)
        self.assertEqual(rc, 5)
        self.assertIn("E_SEAL", err)

    def test_second_test_run_refused(self):
        open(os.path.join(self.runs, "run_test_%s.json" % LBL), "w").write("{}")
        rc, err = self.run_sh(LBL, self.book)
        self.assertEqual(rc, 5)
        self.assertIn("E_TEST_ONCE", err)

    def test_test_lock_marker_refused(self):
        os.makedirs(os.path.join(self.t, "build", "hb_os_" + LBL))
        open(os.path.join(self.t, "build", "hb_os_" + LBL, "TEST.lock"), "w").write("1")
        rc, err = self.run_sh(LBL, self.book)
        self.assertEqual(rc, 5)
        self.assertIn("E_TEST_ONCE", err)


if __name__ == "__main__":
    unittest.main()
