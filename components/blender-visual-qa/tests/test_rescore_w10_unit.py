import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import rescore_w10


class TestRescoreW10Unit(unittest.TestCase):

    def test_cp_lower_edge_cases(self):
        """Test cp_lower edge cases: k=0, k=n, small n."""
        # k <= 0 or n <= 0 -> 0.0
        self.assertEqual(rescore_w10.cp_lower(0, 10), 0.0)
        self.assertEqual(rescore_w10.cp_lower(-1, 10), 0.0)
        self.assertEqual(rescore_w10.cp_lower(5, 0), 0.0)
        self.assertEqual(rescore_w10.cp_lower(5, -2), 0.0)

        # k == n -> alpha ** (1 / n)
        alpha = rescore_w10.ALPHA  # 0.05
        expected_k_eq_n = alpha ** (1.0 / 10)
        self.assertAlmostEqual(rescore_w10.cp_lower(10, 10), expected_k_eq_n, places=6)
        self.assertAlmostEqual(rescore_w10.cp_lower(1, 1), alpha, places=6)

        # Small n tabulated / computed check (e.g. k=5, n=10, alpha=0.05)
        # Clopper-Pearson lower bound for k=5, n=10, 1-sided 95% CI (alpha=0.05)
        # Tail condition: sum_{i=5}^{10} (10 choose i) p^i (1-p)^(10-i) = 0.05
        res_5_10 = rescore_w10.cp_lower(5, 10)
        self.assertGreater(res_5_10, 0.222)
        self.assertLess(res_5_10, 0.223)

    def test_independence(self):
        """Test independence() detecting FIT/GATE seed overlap."""
        rows = [
            {"bench": "b1", "seed": "s1", "capture_sha256": "sha_a"},
            {"bench": "b1", "seed": "s2", "capture_sha256": "sha_b"},
            {"bench": "b1", "seed": "s3", "capture_sha256": "sha_b"},  # gate seed with same capture as s2
            {"bench": "b2", "seed": "s1", "capture_sha256": "sha_c"},
            {"bench": "b2", "seed": "s3", "capture_sha256": "sha_d"},
        ]
        fit_s = {"s1", "s2"}
        gate_s = {"s3"}

        res = rescore_w10.independence(rows, fit_s, gate_s)

        # bench b1: fit captures {"sha_a", "sha_b"}, gate captures {"sha_b"}. overlap = 1 ("sha_b")
        self.assertEqual(res["b1"]["fit_distinct"], 2)
        self.assertEqual(res["b1"]["gate_distinct"], 1)
        self.assertEqual(res["b1"]["overlap"], 1)

        # bench b2: fit captures {"sha_c"}, gate captures {"sha_d"}. overlap = 0
        self.assertEqual(res["b2"]["fit_distinct"], 1)
        self.assertEqual(res["b2"]["gate_distinct"], 1)
        self.assertEqual(res["b2"]["overlap"], 0)

    @patch.object(rescore_w10, "calibrate")
    @patch.object(rescore_w10, "score")
    def test_gate_checks(self, mock_score, mock_calibrate):
        """Test gate_checks calls calibrate.check_targets(score.summarize(...))."""
        gate_rows = [{"seed": "s1"}, {"seed": "s2"}]
        gate_seeds = ["s1", "s2"]

        mock_summary = {"mean_ssim": 0.95}
        mock_score.summarize.return_value = mock_summary
        mock_calibrate.check_targets.return_value = [{"name": "check1", "status": "PASS"}]

        res = rescore_w10.gate_checks(gate_rows, gate_seeds)

        mock_score.summarize.assert_called_once_with(gate_rows)
        mock_calibrate.check_targets.assert_called_once_with(mock_summary, True, gate_rows, gate_seeds)
        self.assertEqual(res, [{"name": "check1", "status": "PASS"}])

    @patch.object(rescore_w10, "calibrate")
    @patch.object(rescore_w10, "score")
    def test_seed_success(self, mock_score, mock_calibrate):
        """Test seed_success over a mixed pass/fail gate population."""
        gate_rows = [
            {"seed": "s1", "val": 10},
            {"seed": "s2", "val": 20},
        ]
        gate_seeds = ["s1", "s2"]

        def mock_check_targets_side_effect(summary, flag, rows, seeds):
            seed = seeds[0]
            if seed == "s1":
                return [
                    {"name": "check_a", "value": 0.9, "status": "PASS"},
                    {"name": "check_b", "value": 0.5, "status": "FAIL"},
                ]
            else:
                return [
                    {"name": "check_a", "value": 0.95, "status": "PASS"},
                    {"name": "check_b", "value": 0.8, "status": "PASS"},
                ]

        mock_calibrate.check_targets.side_effect = mock_check_targets_side_effect

        res = rescore_w10.seed_success(gate_rows, gate_seeds)

        # check_a: s1 (PASS), s2 (PASS) -> k=2, m=2
        self.assertIn("check_a", res)
        self.assertEqual(res["check_a"][0], 2)
        self.assertEqual(res["check_a"][1], 2)
        self.assertIsInstance(res["check_a"][2], float)

        # check_b: s1 (FAIL), s2 (PASS) -> k=1, m=2
        self.assertIn("check_b", res)
        self.assertEqual(res["check_b"][0], 1)
        self.assertEqual(res["check_b"][1], 2)
        self.assertIsInstance(res["check_b"][2], float)

    def test_render_md(self):
        """Test render_md producing a table that contains every gate row check."""
        doc = {
            "official": {"passed": True, "refusal": False, "split_violations": 0},
            "gate_t0_q4": [4],
            "frozen_t0_q4": 4,
            "seed_success_cp95": {
                "check1": [2, 2, 0.2236],
                "check2": [1, 2, 0.0253]
            },
            "diagnostic_gate_only": [
                {"name": "check1", "op": ">=", "bound": 0.8, "value": 0.9, "den": 100, "status": "PASS"},
                {"name": "check2", "op": ">=", "bound": 0.95, "value": 0.85, "den": 100, "status": "FAIL"}
            ],
            "independence": {
                "bench1": {"fit_distinct": 5, "gate_distinct": 5, "overlap": 0}
            }
        }

        md = rescore_w10.render_md(doc)

        self.assertIn("# W10X offline rescore", md)
        self.assertIn("check1", md)
        self.assertIn("check2", md)
        self.assertIn("bench1", md)
        self.assertIn("2/2", md)
        self.assertIn("1/2", md)

    @patch.object(rescore_w10, "calibrate")
    @patch.object(rescore_w10, "score")
    def test_rescore(self, mock_score, mock_calibrate):
        """Test rescore() execution with temporary run and output directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            run_dir = tmp_path / "run"
            out_dir = tmp_path / "out"
            run_dir.mkdir()

            rows_content = json.dumps({"bench": "b1", "seed": "s1", "capture_sha256": "abc", "t0_q4": 4}) + "\n"
            (run_dir / "rows.jsonl").write_text(rows_content)
            (run_dir / "calib_report.json").write_text(json.dumps({
                "seeds": {"fit": ["s0"], "gate": ["s1"]},
                "renderer": "cycles",
                "grid": "g1",
                "frozen_t0_q4": 4
            }))
            (run_dir / "still_manifest.json").write_bytes(b'{"samples_q4": 4}')

            mock_score.canon.sha256_bytes.return_value = "dummy_sha256"
            mock_calibrate.calibrate.return_value = (None, {
                "passed": True, "refusal": False, "split_violations": 0, "split_detail": {}, "T0_Q4": 4
            })
            mock_calibrate.check_targets.return_value = [
                {"name": "check1", "op": ">=", "bound": 0.8, "value": 0.9, "status": "PASS"}
            ]

            doc = rescore_w10.rescore(run_dir, out_dir)

            self.assertEqual(doc["rows"], 1)
            self.assertTrue(doc["official"]["passed"])
            self.assertTrue((out_dir / "rescore_report.json").exists())
            self.assertTrue((out_dir / "RESCORE.md").exists())

            # Test invalid output dir validation
            with self.assertRaises(SystemExit):
                rescore_w10.rescore(run_dir, run_dir)

    @patch.object(rescore_w10, "rescore")
    def test_main(self, mock_rescore):
        """Test main() CLI entrypoint."""
        mock_rescore.return_value = {"official": {"refusal": False}}
        with tempfile.TemporaryDirectory() as tmpdir:
            run_p = str(Path(tmpdir) / "run")
            out_p = str(Path(tmpdir) / "out")
            rc = rescore_w10.main(["--run", run_p, "--out", out_p])
            self.assertEqual(rc, 0)
            mock_rescore.assert_called_once()


if __name__ == "__main__":
    unittest.main()
