"""W9 S2: p99.9 T0_still, band-aware T0_fp, FIT/GATE seeds, refusal attribution, goldens (IMPL-GAP-5/6/8, G-T7)."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from blender_visual_qa.truth import calibrate, score
from blender_visual_qa.truth.score import VqaError
from tests.test_calibrate import good_rows, calib, split_set
from tests.test_score import synth, row

GOLD = Path(__file__).resolve().parent / "golden" / "truth"


def seeded(rows, seeds):
    out = []
    for r, s in zip(rows, seeds):
        r = json.loads(json.dumps(r))
        r["seed"] = s
        out.append(r)
    return out


class T0Tests(unittest.TestCase):
    def test_still_uses_p999_rank_not_max(self):
        xs = [10] * 999 + [200]          # M=1000: rank 999*999//1000 = 998 -> q=10; max would give 253
        self.assertEqual(calibrate.t0_still(xs), 32)
        xs = [100] * 998 + [101, 400]    # rank 998 -> 101... M=1000 -> idx 998
        self.assertEqual(calibrate.t0_still(xs), (5 * 101 + 3) // 4)

    def test_fp_least_t0_with_band(self):  # bt.t0fp.01: max+1 (no band) would give 111
        rows = good_rows(fp_resid=110)
        t = calibrate.t0_fp(rows)
        self.assertEqual(t, 100)
        self.assertGreaterEqual(t * 1100000 // 1000000, 110)
        self.assertLess((t - 1) * 1100000 // 1000000, 110)
        self.assertEqual(calibrate.t0_fp([]), 1)
        self.assertEqual(calibrate.t0_fp(good_rows(fp_resid=0)), 1)

    def test_fp_least_property(self):
        for mx in range(1, 400):
            t = calibrate.t0_fp([{"fp_resid_max_q4": mx}])
            self.assertGreaterEqual(t * 1100000 // 1000000, mx)
            self.assertTrue(t == 1 or (t - 1) * 1100000 // 1000000 < mx)


class SeedTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(calibrate.parse_seeds("fit:0-2,gate:5-6"), {"fit": [0, 1, 2], "gate": [5, 6]})
        with self.assertRaises(VqaError):
            calibrate.parse_seeds("train:0-2")

    def _cal(self, fit, gate, **kw):
        rows, sd, meta = split_set(fit, gate, **kw)
        return rows, sd, meta

    def test_overlap_refuses_bt_samedata(self):
        rows, sd, meta = self._cal(good_rows(n=2), good_rows(n=2), gate0=2)   # fit 1,2 / gate 2,3
        p, rep = calibrate.calibrate(rows, [4], "blender", "r", "g", "ab", seeds=sd, still_meta=meta)
        self.assertIsNone(p)
        self.assertEqual((rep["refusal"], rep["seeds_overlap"]), ("split_leak", [2]))
        self.assertIn("seed_overlap", rep["split_violations"])

    def test_disjoint_seeds_pass_and_split(self):
        rows, sd, meta = self._cal(good_rows(n=2), good_rows(n=2))
        p, rep = calibrate.calibrate(rows, [4], "blender", "r", "g", "ab", seeds=sd, still_meta=meta)
        self.assertIsNotNone(p)
        self.assertEqual((rep["seeds_overlap"], rep["split_violations"], rep["refusal"]), ([], [], None))
        self.assertTrue(rep["split_sha256"])

    def test_fit_residual_never_comes_from_gate_rows(self):
        gate = good_rows(fp_resid=10, n=1)
        gate[0]["fp_resid_max_q4"] = 300
        rows, sd, meta = self._cal(good_rows(fp_resid=10, n=1), gate)
        _, rep = calibrate.calibrate(rows, [4], "blender", "r", "g", "ab", seeds=sd, still_meta=meta)
        self.assertEqual(rep["T0_fp"], calibrate.t0_fp([{"fp_resid_max_q4": 10}]))

    def test_gate_rows_decide_static_fp(self):
        gate = good_rows(n=1)
        gate[0]["tl"]["0"]["dirty"] = 1
        rows, sd, meta = self._cal(good_rows(n=1), gate)
        p, rep = calibrate.calibrate(rows, [4], "blender", "r", "g", "ab", seeds=sd, still_meta=meta)
        self.assertIsNone(p)
        self.assertEqual(rep["fp_attribution"][0]["tl"], "STATIC")

    def test_cli_overlap_rc5_no_profile(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            rows, sd, meta = self._cal(good_rows(n=2), good_rows(n=2), gate0=2)
            (d / "rows.jsonl").write_text("".join(score.canon.dumps(r) for r in rows))
            (d / "still.json").write_text(json.dumps({"samples_q4": [4], "still_meta": meta}))
            rc = calibrate.main(["--rows", str(d / "rows.jsonl"), "--still", str(d / "still.json"), "--out", str(d / "p.json"),
                                 "--grid", "g", "--seeds", "fit:1-2,gate:2-3"])
            self.assertEqual(rc, 5)
            self.assertFalse((d / "p.json").exists())
            self.assertEqual(json.loads((d / "calib_report.json").read_text())["refusal"], "split_leak")


class AttributionTests(unittest.TestCase):
    def test_contamination_names_bench_and_tile_class(self):
        rows = good_rows(fp_resid=1020)
        for r in rows:
            r["bench"] = "B4"
            r["fp_resid_by_tl"] = {"0": 1020}
            r["tl"]["0"]["dirty"] = 2
        rows[0]["bench"] = "B5"
        p, rep = calib(rows, [4], "blender", "r", "g", "ab")
        self.assertIsNone(p)
        self.assertEqual(rep["refusal"], "fp_pop_contaminated")
        names = {(a["bench"], a["tl"]) for a in rep["fp_attribution"]}
        self.assertEqual(names, {("B4", "STATIC"), ("B5", "STATIC")})
        self.assertEqual(sum(a["fp_tiles"] for a in rep["fp_attribution"] if a["bench"] == "B4"), 4)


class GoldenTests(unittest.TestCase):
    def _run(self, d, rows_name, extra=()):
        out = Path(d)
        rc = calibrate.main(["--rows", str(GOLD / rows_name), "--still", str(GOLD / "still.json"), "--out", str(out / "p.json"),
                             "--grid", "grid:640x360:s16", "--renderer", "cycles-cpu-4spp-box",
                             "--seeds", "fit:0-1,gate:10-11", *extra])
        return rc, out

    def test_pass_golden_byte_identical_x3(self):
        outs = []
        for _ in range(3):
            with tempfile.TemporaryDirectory() as d:
                rc, out = self._run(d, "rows_pass.jsonl", ("--strict",))
                self.assertEqual(rc, 0)
                outs.append(((out / "p.json").read_bytes(), (out / "calib_report.json").read_bytes()))
        self.assertEqual(len(set(outs)), 1)
        self.assertEqual(outs[0][0], (GOLD / "profile_pass.json").read_bytes())
        self.assertEqual(outs[0][1], (GOLD / "report_pass.json").read_bytes())

    def test_params_change_proof(self):
        """Golden report differs from the one reproduced by the previous pinned wheel (6b6679a) ONLY by params_sha256."""
        proof = json.loads((GOLD / "params_change_proof.json").read_text())
        cfg = json.loads((GOLD.parents[2] / "config/vision.json").read_text())
        self.assertEqual(proof["params_diff_old_new"], {"H_DEAD": [4, 2], "SIG_DEAD_Q4": [None, 24]})
        self.assertEqual(proof["report_keys_differing"], ["params_sha256"])
        self.assertTrue(proof["reproduced_with_old_engine"])
        self.assertTrue(proof["reproduced_old_report_byte_identical"])
        self.assertTrue(proof["profile_identical_old_new"])
        rep = json.loads((GOLD / "report_pass.json").read_text())
        self.assertEqual(rep["params_sha256"], proof["new"]["params_sha256"])
        self.assertEqual(hashlib.sha256((GOLD / "report_pass.json").read_bytes()).hexdigest(), proof["new"]["report_sha256"])
        self.assertNotEqual(proof["old"]["params_sha256"], proof["new"]["params_sha256"])
        self.assertEqual(proof["engine_old"], "6b6679a925ff69249faf508aa510e25b2dd19500")
        self.assertEqual(proof["engine_new"], proof["repin_monorepo"]["to_engine"])
        self.assertEqual(proof["new_wheel_sha256"], proof["repin_monorepo"]["to_wheel_sha256"])
        self.assertEqual(proof["repin_wheel_notice"]["from_wheel_sha256"], proof["new_wheel_sha256"])
        self.assertEqual(proof["repin_public_source"], {
            "from_wheel_sha256": proof["repin_wheel_notice"]["to_wheel_sha256"],
            "to_engine": cfg["pin"]["commit"],
            "to_wheel_path": cfg["engine_path"],
            "to_wheel_sha256": cfg["pin"]["wheel_sha256"],
        })
        # the chain stays anchored: the pre-6b6679a golden proof is kept as previous_step
        self.assertEqual(proof["previous_step"]["params_diff_old_new"], {"H_DEAD": [2, 4]})

    def test_failing_rows_rc5_no_profile(self):
        for _ in range(3):
            with tempfile.TemporaryDirectory() as d:
                rc, out = self._run(d, "rows_fail.jsonl")
                self.assertEqual(rc, 5)
                self.assertFalse((out / "p.json").exists())
                self.assertEqual(json.loads((out / "calib_report.json").read_text())["passed"], False)


if __name__ == "__main__":
    unittest.main()
