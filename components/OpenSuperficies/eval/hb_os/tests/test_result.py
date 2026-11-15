"""RESULT.md vs the committed TEST receipts: recompute the sign-flip p, wins/losses and the cited stamps; compare.py guards."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction as F

HERE = os.path.dirname(os.path.abspath(__file__))
EVAL = os.path.abspath(os.path.join(HERE, ".."))
REPO = os.path.abspath(os.path.join(EVAL, "..", ".."))
RUNS = os.path.join(EVAL, "runs")
sys.path.insert(0, os.path.join(REPO, "hyperbooks", "_tools", "hbtools_v2"))
import hb_eval  # noqa: E402


def receipt(label):
    return json.load(open(os.path.join(RUNS, "run_test_%s.json" % label)))


class Result(unittest.TestCase):
    def setUp(self):
        self.text = open(os.path.join(EVAL, "RESULT.md")).read()
        b, a = receipt("before"), receipt("after")
        rb, ra = b["rr"]["v2"], a["rr"]["v2"]
        self.d = [F(ra[q]) - F(rb[q]) for q in sorted(ra) if q in rb]
        self.b, self.a = b, a

    def test_p_recomputed_from_receipts(self):
        nz = [x for x in self.d if x != 0]
        self.assertEqual((len(self.d), len(nz)), (60, 12))
        self.assertEqual((sum(1 for x in nz if x > 0), sum(1 for x in nz if x < 0)), (10, 2))
        p = F(hb_eval.sign_flip_p(self.d))
        self.assertEqual(p, F(23, 512))
        self.assertLess(p, F(1, 20))
        self.assertGreater(sum(self.d), 0)

    def test_result_states_the_recomputed_numbers(self):
        for needle in ("23/512", "10 wins / 2 losses", "0.0449", "12/60", "needs replication", "met the pre-registered threshold narrowly"):
            self.assertIn(needle, self.text)
        self.assertIn("%.3f" % float(self.b["metrics"]["v2"]["FIND"]["adjudicated"]["mrr"]), self.text)
        self.assertIn(self.a["metrics"]["v2"]["FIND"]["adjudicated"]["mrr"], self.text)
        self.assertIn(self.a["metrics"]["v2"]["ABSENT"]["f1"], self.text)
        self.assertIn("not a general claim", self.text)

    def test_errata_matches_stamps_and_prereg_seal(self):
        for r in (self.b, self.a):
            self.assertIn(r["stamp"]["params_sha256"][:8], self.text)
        self.assertEqual(self.b["stamp"]["params_sha256"], self.a["stamp"]["params_sha256"])
        self.assertIn("be9da324", self.text)
        import hashlib
        h = hashlib.sha256(open(os.path.join(EVAL, "PREREG.md"), "rb").read()).hexdigest()
        self.assertTrue(h.startswith("264502f7"))
        self.assertIn("264502f7", self.text)

    def test_parity_and_cost_sections(self):
        self.assertEqual((self.b["stamp"]["build_link_issues_test_files"], self.a["stamp"]["build_link_issues_test_files"]), (11, 0))
        self.assertRegex(self.text, r"\b11\b.*\b0\b")
        self.assertIn("precio_ausente", self.text)
        self.assertIn("1.1303", self.text)


class Compare(unittest.TestCase):
    def run_cmp(self, *args):
        p = subprocess.run(["taskset", "-c", "10-14", "timeout", "120", sys.executable, "-B", os.path.join(EVAL, "compare.py"), *args],
                           capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr

    def test_compare_matches_result(self):
        rc, out, _ = self.run_cmp()
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual((d["wins"], d["losses"], d["nonzero"], d["p_exact_fraction"], d["met_threshold"]), (10, 2, 12, "23/512", True))

    def test_usage_and_stamp_mismatch(self):
        self.assertEqual(self.run_cmp("--bogus")[0], 2)
        t = tempfile.mkdtemp(prefix="cmp_")
        for l in ("before", "after"):
            shutil.copy(os.path.join(RUNS, "run_test_%s.json" % l), t)
        a = json.load(open(os.path.join(t, "run_test_after.json")))
        a["stamp"]["params_sha256"] = "0" * 64
        json.dump(a, open(os.path.join(t, "run_test_after.json"), "w"))
        rc, _, err = self.run_cmp("--runs", t)
        self.assertEqual(rc, 5)
        self.assertIn("params_sha256", err)
        self.assertEqual(self.run_cmp("--runs", os.path.join(t, "nope"))[0], 5)


if __name__ == "__main__":
    unittest.main()
