"""Lane H (v12e room HD): H_DEAD re-fit on the enlarged CAL split (X4 seeds 40..199, X3 seeds 40..69; never 0..29,
1000..1029 or 2000..2029). The fit table eval/splits/hdead_cal_v3.json shows no H_DEAD in 3..8 with both 0 FP runs and
X3 cell power >= 29/30, so H_DEAD stays 4 and G-E2 iid_noise is a KNOWN_LIMITATIONS row. The two edges of the
trade-off are re-measured here on CAL seeds through the real drivers."""
import json
import unittest
from pathlib import Path
from unittest import mock

from eval import x3_power as X3, x4_fp as X4
from eval.splits import HERE
from visioncore import params as P

FIT = HERE / "hdead_cal_v3.json"
ROOT = Path(__file__).resolve().parent.parent


def fit():
    return json.loads(FIT.read_text())


class HDeadFit(unittest.TestCase):
    def test_table_covers_sweep_and_selection_rule(self):
        d = fit()
        self.assertEqual((d["x4_cal_seeds"], d["x3_cal_seeds"]), ([40, 199], [40, 69]))
        t = {r["h_dead"]: r for r in d["table"]}
        self.assertEqual(sorted(t), [3, 4, 5, 6, 7, 8])
        for r in t.values():
            self.assertEqual((r["x4_runs"], r["x3_trials"]), (960, 450))
            self.assertEqual(r["zero_fp"], r["x4_fp_runs_total"] == 0)
            self.assertEqual(r["power_ok"], r["x3_min_cell"] >= 29 and r["x3_aggregate_cp95_lower_ppm"] >= 950000)
        h0 = min(h for h, r in t.items() if r["zero_fp"])
        self.assertEqual((h0, d["h0"], d["h_star"]), (5, 5, 6))
        self.assertEqual(d["feasible"], [h for h, r in sorted(t.items()) if r["zero_fp"] and r["power_ok"]])
        self.assertEqual(d["feasible"], [])
        self.assertEqual((t[4]["x4_fp_runs"]["iid_noise"], t[5]["x3_cells"]["shift/L2"]), (16, 27))

    def test_registry_keeps_power_preserving_value(self):
        self.assertEqual(fit()["selected"], 4)   # HD fit (no sig64 band); superseded by NR: tests/golden/nr_sigdead_fit.json

    def test_iid_noise_edge_reproduces_on_cal(self):
        """cal seed 42: 1 FP tile at H_DEAD 4, none at 5 (X4 SEAL config)."""
        s = fit()["repro"]["x4_iid_fp_at_4"][0]
        self.assertEqual(s, [42, 1])
        with mock.patch.dict(P.PARAMS, {"H_DEAD": 4, "SIG_DEAD_Q4": 0}):
            self.assertEqual(X4.run_trial("iid_noise", 42, X4.SEAL_W, X4.SEAL_H)["fp_tiles"], 1)
        with mock.patch.dict(P.PARAMS, {"H_DEAD": 5, "SIG_DEAD_Q4": 0}):
            self.assertEqual(X4.run_trial("iid_noise", 42, X4.SEAL_W, X4.SEAL_H)["fp_tiles"], 0)

    def test_shift_power_edge_reproduces_on_cal(self):
        """cal seed 45: X3 shift/L2 (1 px) detected at H_DEAD 4, missed at 5."""
        self.assertIn(45, fit()["repro"]["x3_shift_L2_missed_at_5"])
        spec = X3.trial_spec("shift", 2, 45)
        with mock.patch.dict(P.PARAMS, {"H_DEAD": 4, "SIG_DEAD_Q4": 0}):
            self.assertTrue(X3.run_trial(spec)["detected"])
        with mock.patch.dict(P.PARAMS, {"H_DEAD": 5, "SIG_DEAD_Q4": 0}):
            self.assertFalse(X3.run_trial(spec)["detected"])

    def test_known_limitations_row(self):
        text = (ROOT / "KNOWN_LIMITATIONS.md").read_text()
        row = [ln for ln in text.splitlines() if ln.startswith("| KL-5 |")]
        self.assertEqual(len(row), 1)
        self.assertIn("G-E2 at H_DEAD 4 on split v2: 2/180 FP runs", row[0])
        for ev in ("eval/splits/hdead_cal_v3.json", "eval/splits/x4_v3.json", "175bf43b", "897 ppb"):
            self.assertIn(ev, row[0])


if __name__ == "__main__":
    unittest.main()
