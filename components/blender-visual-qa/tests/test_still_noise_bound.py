import io
import json
import tempfile
import unittest
from pathlib import Path

from blender_visual_qa.truth import calibrate, score
from blender_visual_qa.truth.score import VqaError
from tools import still_noise_bound


class StillNoiseBoundTests(unittest.TestCase):
    def setUp(self):
        self.still_path = Path("tests/golden/truth/still.json")
        self.rows_path = Path("tests/golden/truth/rows_pass.jsonl")
        self.report_path = Path("tests/golden/truth/report_pass.json")

        self.still_data = json.loads(self.still_path.read_text(encoding="utf-8"))
        self.samples_q4 = self.still_data["samples_q4"]

        self.rows = [
            score.canon.loads(ln)
            for ln in self.rows_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        self.report_data = json.loads(self.report_path.read_text(encoding="utf-8"))
        self.seeds = self.report_data["seeds"]
        self.fit_s = set(self.seeds["fit"])
        self.fit_rows = [r for r in self.rows if r.get("seed") in self.fit_s]

    def test_t0_still_reproduced(self):
        doc = still_noise_bound.compute_bounds(self.samples_q4, self.fit_rows, self.rows)
        expected_t0_still = calibrate.t0_still(self.samples_q4)
        self.assertEqual(doc["T0_still"], expected_t0_still)

    def test_eps_monotone_in_alpha_and_b(self):
        doc = still_noise_bound.compute_bounds(self.samples_q4, self.fit_rows, self.rows)
        bounds = doc["bounds"]

        # Map (alpha, b) -> eps
        eps_map = {(b_entry["alpha"], b_entry["b"]): b_entry["eps"] for b_entry in bounds}

        # Assert monotonicity in alpha: smaller alpha -> larger eps
        for b in (64, 256):
            self.assertLess(eps_map[(0.01, b)], eps_map[(0.001, b)])
            self.assertLess(eps_map[(0.001, b)], eps_map[(0.0001, b)])

        # Assert monotonicity in b: larger b -> smaller eps
        for alpha in (0.01, 0.001, 0.0001):
            self.assertLess(eps_map[(alpha, 256)], eps_map[(alpha, 64)])

    def test_gate_rows_never_read(self):
        doc_orig = still_noise_bound.compute_bounds(self.samples_q4, self.fit_rows, self.rows)

        # Corrupt GATE rows with massive FP residuals and dirty counts
        corrupted_rows = []
        for r in self.rows:
            r_copy = json.loads(json.dumps(r))
            if r_copy.get("seed") not in self.fit_s:
                r_copy["fp_resid_max_q4"] = 99999
                r_copy["tl"] = {"0": {"dirty": 999, "n": 999, "cls_ok": 0}}
            corrupted_rows.append(r_copy)

        doc_corrupted = still_noise_bound.compute_bounds(self.samples_q4, self.fit_rows, corrupted_rows)

        # FIT-derived fields must remain strictly identical
        self.assertEqual(doc_orig["T0_still"], doc_corrupted["T0_still"])
        self.assertEqual(doc_orig["T0_fp"], doc_corrupted["T0_fp"])
        self.assertEqual(doc_orig["contaminated"], doc_corrupted["contaminated"])
        self.assertEqual(doc_orig["mu"], doc_corrupted["mu"])
        self.assertEqual(doc_orig["R"], doc_corrupted["R"])
        self.assertEqual(doc_orig["bounds"], doc_corrupted["bounds"])

    def test_empty_probe_refused_same_error_code(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "run"
            out_dir = Path(tmp_dir) / "out"
            run_dir.mkdir()

            # Write empty still manifest
            (run_dir / "still_manifest.json").write_text(json.dumps({"samples_q4": []}))
            (run_dir / "rows.jsonl").write_text("".join(score.canon.dumps(r) + "\n" for r in self.rows))

            # Test direct call raises VqaError("E_SCHEMA", ...)
            with self.assertRaises(VqaError) as cm:
                still_noise_bound.run_still_noise_bound(str(run_dir), str(out_dir))
            self.assertEqual(cm.exception.rc, 2)
            self.assertEqual(cm.exception.code, "E_SCHEMA")
            self.assertEqual(cm.exception.detail.get("reason"), "empty still probe")

            # Test main() prints JSON with rc 2 and returns 2
            stdout_buf = io.StringIO()
            old_stdout = still_noise_bound.sys.stdout
            try:
                still_noise_bound.sys.stdout = stdout_buf
                rc = still_noise_bound.main(["--run", str(run_dir), "--out", str(out_dir)])
            finally:
                still_noise_bound.sys.stdout = old_stdout

            self.assertEqual(rc, 2)
            out_obj = json.loads(stdout_buf.getvalue().strip())
            self.assertEqual(out_obj["code"], "E_SCHEMA")
            self.assertEqual(out_obj["detail"]["reason"], "empty still probe")
            self.assertEqual(out_obj["rc"], 2)


if __name__ == "__main__":
    unittest.main()
