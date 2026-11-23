import glob
import json
import os
import tempfile
import unittest
from pathlib import Path

from blender_visual_qa.truth import calibrate, score
from visioncore import params
from tests.test_score import synth, row
from tests.test_calibrate import good_rows, evidence_rows, still_row


class CalibrateHelpersUnitTests(unittest.TestCase):
    def test_band_ppm(self):
        # Default hysteresis permille from visioncore params
        expected_default = int(params.PARAMS["HYST_BAND_PERMILLE"]) * 1000
        self.assertEqual(calibrate.band_ppm(), expected_default)

        # Explicit hysteresis permille
        self.assertEqual(calibrate.band_ppm(25), 25000)
        self.assertEqual(calibrate.band_ppm(0), 0)

    def test_still_membership(self):
        fit_seeds = [1, 2, 3]
        still_seeds = [1, 2]

        # 1. Absent / invalid still captures
        self.assertEqual(
            calibrate.still_membership([], fit_seeds, still_seeds, None),
            ["still_probe_capture_absent"]
        )
        self.assertEqual(
            calibrate.still_membership([], fit_seeds, still_seeds, []),
            ["still_probe_capture_absent"]
        )
        self.assertEqual(
            calibrate.still_membership([], fit_seeds, still_seeds, ["cap1", ""]),
            ["still_probe_capture_absent"]
        )
        self.assertEqual(
            calibrate.still_membership([], fit_seeds, still_seeds, [123]),
            ["still_probe_capture_absent"]
        )

        # 2. Unbound captures: capture shas not present in rows matching bench=B6, seed in fit_s and still_seeds
        rows = [
            still_row(1, "cap1"),
            still_row(2, "cap2"),
        ]
        # cap3 is unbound
        bad = calibrate.still_membership(rows, fit_seeds, still_seeds, ["cap1", "cap3"])
        self.assertIn("still_probe_capture_unbound", bad)

        # 3. Seed-unbound still captures: declared still_seeds not fully represented by the captures
        # Here still_seeds is [1, 2], but rows only supply cap1 for seed 1
        rows_partial = [
            still_row(1, "cap1"),
            still_row(3, "cap2"),  # seed 3 is not in still_seeds [1, 2]
        ]
        bad_seed = calibrate.still_membership(rows_partial, fit_seeds, still_seeds, ["cap1", "cap2"])
        self.assertIn("still_probe_capture_unbound", bad_seed)  # cap2 is for seed 3 (not in still_seeds)
        self.assertIn("still_probe_seed_unbound", bad_seed)

        # 4. Valid still membership
        rows_valid = [
            still_row(1, "cap1"),
            still_row(2, "cap2"),
        ]
        self.assertEqual(calibrate.still_membership(rows_valid, fit_seeds, still_seeds, ["cap1", "cap2"]), [])

    def test_missing_population(self):
        gate_seeds = [10, 11, 12]

        # Full evidence for all gate seeds
        rows = evidence_rows(gate_seeds)
        self.assertEqual(calibrate.missing_population(rows, gate_seeds), {})

        # Remove evidence for seed 12 for b4n_temporal_fp_tiles
        rows_gap = [r for r in rows if not (r.get("bench") == "B4n" and r.get("seed") == 12)]
        gaps = calibrate.missing_population(rows_gap, gate_seeds)
        self.assertEqual(gaps, {"b4n_temporal_fp_tiles": [12]})

    def test_check_targets(self):
        gate_seeds = [10, 11]
        gate_rows = evidence_rows(gate_seeds)

        summary = score.summarize(good_rows(n=2))

        # Nominal check
        checks = calibrate.check_targets(summary, strict=False, gate_rows=gate_rows, gate_seeds=gate_seeds)
        self.assertTrue(isinstance(checks, list))

        # Check mandatory metric with no value is FAIL and absent is True
        summary_empty_tl = dict(summary, tl={})
        checks_empty = calibrate.check_targets(summary_empty_tl, strict=False, gate_rows=[], gate_seeds=gate_seeds)
        static_fp = next(c for c in checks_empty if c["name"] == "static_fp")
        self.assertEqual(static_fp["status"], "FAIL")
        self.assertTrue(static_fp.get("absent"))

        # Check missing_seeds is attached when gaps exist
        # If gate_rows is empty, all required checks with population have gaps
        for c in checks_empty:
            if "missing_seeds" in c:
                self.assertEqual(c["missing_seeds"], gate_seeds)

    def test_clean_evidence_missing(self):
        # CLEAN_BENCHES are ("B3", "B5")
        rows = [
            {"bench": "B3", "seed": 1, "clean_resid": True},
            {"bench": "B3", "seed": 2, "clean_resid": False},
            {"bench": "B5", "seed": 3},  # clean_resid missing
            {"bench": "B1", "seed": 4},  # not a clean bench
        ]
        missing = calibrate.clean_evidence_missing(rows)
        self.assertEqual(missing, [("B3", 2), ("B5", 3)])

    def test_load_rows(self):
        with tempfile.TemporaryDirectory() as d:
            dp = Path(d)
            f1 = dp / "a.jsonl"
            f2 = dp / "b.jsonl"

            r1 = {"bench": "B1", "seed": 1, "val": 100}
            r2 = {"bench": "B2", "seed": 2, "val": 200}
            r3 = {"bench": "B3", "seed": 3, "val": 300}

            f1.write_text(score.canon.dumps(r1) + score.canon.dumps(r2), encoding="utf-8")
            f2.write_text("\n\n" + score.canon.dumps(r3) + "\n", encoding="utf-8")

            loaded = calibrate.load_rows([str(dp / "*.jsonl")])
            self.assertEqual(len(loaded), 3)
            self.assertEqual(loaded[0], r1)
            self.assertEqual(loaded[1], r2)
            self.assertEqual(loaded[2], r3)


if __name__ == "__main__":
    unittest.main()
