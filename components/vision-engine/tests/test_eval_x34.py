import json
import tempfile
import unittest
from pathlib import Path

from eval import x3_power as X3, x4_fp as X4


class Stats(unittest.TestCase):
    def test_cp_known_values(self):
        self.assertAlmostEqual(X3.cp_lower(30, 30), 0.05 ** (1 / 30), places=6)   # 0.905
        self.assertAlmostEqual(X3.cp_lower(29, 30), 0.8514, places=3)
        self.assertAlmostEqual(X3.cp_upper(0, 4400000), 6.8e-7, delta=5e-9)
        self.assertEqual(X3.cp_lower(0, 30), 0.0)

    def test_draw_deterministic(self):
        self.assertEqual(X3.draw("a", 1, 100), X3.draw("a", 1, 100))
        self.assertNotEqual(X3.draw("a", 1, 1 << 30), X3.draw("a", 2, 1 << 30))


class X3Power(unittest.TestCase):
    def test_spec_deterministic_and_in_bounds(self):
        for mu in X3.MUTANTS:
            a = X3.trial_spec(mu, 2, 5)
            self.assertEqual(a, X3.trial_spec(mu, 2, 5))
            x, y, w, h = a["rect"]
            self.assertTrue(x >= 0 and y >= 0 and x + w <= 256 and y + h <= X3.MAX_Y)

    def test_trial_detects_large_flicker_and_freeze(self):
        self.assertTrue(X3.run_trial(X3.trial_spec("flicker", 0, 0))["detected"])
        r = X3.run_trial(X3.trial_spec("frozen_subregion", 0, 0))
        self.assertTrue(r["detected"] and r["schema"] == "vqa.eval-row/v1" and r["x"] == "X3")

    def test_negative_wrong_expectation_not_detected(self):
        spec = X3.trial_spec("flicker", 0, 0)
        spec["expect"] = ["FROZEN"]  # a flicker mutant is not a freeze: must not count
        self.assertFalse(X3.run_trial(spec)["detected"])

    def test_summary_gate_logic(self):
        def rows(miss):
            out = []
            for mu in X3.MUTANTS:
                for lv in range(3):
                    for s in range(30):
                        out.append({"mutant": mu, "level": lv, "detected": not (miss and (mu, lv) == ("shift", 2) and s < miss)})
            return out
        self.assertEqual(X3.summarize(rows(0), 30)["verdict"], "PASS")
        self.assertEqual(X3.summarize(rows(1), 30)["verdict"], "PASS")   # 29/30 per cell, aggregate still >= 0.95
        self.assertEqual(X3.summarize(rows(2), 30)["verdict"], "FAIL")   # 28/30
        self.assertEqual(X3.summarize([], 30)["verdict"], "FAIL")

    def test_cli_writes_jsonl_and_summary(self):
        with tempfile.TemporaryDirectory() as d:
            rows, _ = X3.run_rows(1, d, mutants=["paste"])
            self.assertEqual(len(rows), 3)
            lines = (Path(d) / "X3.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 3)
            self.assertEqual(json.loads(lines[0])["x"], "X3")


def _full_rows():
    """Bound, zero-FP rows for the whole registered sample at the seal config (made-up tile counts, no driver run)."""
    import hashlib
    from visioncore.grid import Grid
    tf = Grid(X4.SEAL_W, X4.SEAL_H).N * (X4.N_FRAMES - 1)
    return [{"schema": "vqa.eval-row/v1", "x": "X4", "item": c, "tier": "PX", "control": c, "seed": sd, "rep": 0,
             "tile_frames": tf, "fault_tiles": 0, "dirty_tiles": 0, "fp_tiles": 0, "drift_tiles": 0, "classes": {},
             "fp": False, "split": "test", "capture_sha256": hashlib.sha256(("%s|%d" % (c, sd)).encode()).hexdigest(),
             "partition_sha256": X4.partition_sha(), "config_sha256": X4.config_sha()}
            for c in X4.CONTROLS for sd in range(X4.TEST_SEEDS)]


class X4FalsePositive(unittest.TestCase):
    def test_clean_static_and_jitter_zero_fp(self):
        for n in ("clean_static", "gpu_jitter"):
            r = X4.run_trial(n, 0)
            self.assertEqual((r["fp_tiles"], r["fp"]), (0, False), n)

    def test_negative_planted_fault_counts_as_fp(self):
        # FX4: summarize binds rows to the frozen partition + config; trials at the small size need their own config sha
        cfg = X4.config_sha(X4.DEF_W, X4.DEF_H, X4.N_FRAMES)
        r = X4.run_trial("clean_static", 0)
        r2 = dict(r, seed=1, capture_sha256="1" * 64, fp_tiles=5, fault_tiles=5, fp=True)
        s = X4.summarize([r, r2], 2, cfg)
        self.assertEqual(s["verdict"], "FAIL")
        self.assertEqual(s["fp_runs"], 1)
        self.assertEqual(s["fp_tiles"], 5)
        # same rows without the planted fault: zero FP runs (the fault, not the binding, is what is counted)
        s0 = X4.summarize([r, dict(r2, fp_tiles=0, fault_tiles=0, fp=False)], 2, cfg)
        self.assertEqual((s0["fp_runs"], s0["fp_tiles"]), (0, 0))
        # complete seal-size sample: one planted FP run flips PASS -> FAIL
        full = _full_rows()
        self.assertEqual(X4.summarize(full)["verdict"], "PASS")
        bad = [dict(full[0], fp_tiles=5, fault_tiles=5, fp=True)] + full[1:]
        sb = X4.summarize(bad)
        self.assertEqual((sb["verdict"], sb["fp_runs"]), ("FAIL", 1))

    def test_summary_pass_when_clean(self):
        # PASS needs the full registered sample (6 controls x 30 test seeds, >= required tile-frames): made-up counts, unit logic only
        s = X4.summarize(_full_rows())
        self.assertEqual((s["verdict"], s["clean_fp_tile_ppm"], s["sample_complete"], s["bound_met"]), ("PASS", 0, True, True))
        # a single real clean trial is zero-FP but an incomplete sample: never PASS
        r = X4.run_trial("clean_static", 0)
        s1 = X4.summarize([r], 1, X4.config_sha(X4.DEF_W, X4.DEF_H, X4.N_FRAMES))
        self.assertEqual((s1["verdict"], s1["fp_runs"], s1["sample_complete"]), ("FAIL", 0, False))
        self.assertEqual(X4.summarize([], 1)["verdict"], "FAIL")

    def test_rows_written(self):
        with tempfile.TemporaryDirectory() as d:
            rows, _ = X4.run_rows(1, d, controls=["clean_static", "shader_pulse"])
            self.assertEqual(len(rows), 2)
            self.assertEqual(json.loads((Path(d) / "X4.jsonl").read_text().splitlines()[1])["control"], "shader_pulse")


if __name__ == "__main__":
    unittest.main()


class X4SplitLeak(unittest.TestCase):
    def test_split_labels_and_cal_row_refused(self):
        from visioncore.errors import VqaError
        t = X4.run_trial("clean_static", 0)
        c = X4.run_trial("clean_static", 30)
        self.assertEqual((t["split"], c["split"]), ("test", "cal"))
        with self.assertRaises(VqaError) as cm:
            X4.summarize([t, c], 1)
        self.assertEqual((cm.exception.code, cm.exception.rc, cm.exception.detail["reason"]), ("E_GATE", 5, "split_leak"))

    def test_cal_run_writes_rows_without_summary(self):
        with tempfile.TemporaryDirectory() as d:
            rows, summ = X4.run_rows(1, d, controls=["clean_static"], seed_start=30)
            self.assertEqual((rows[0]["split"], summ), ("cal", None))
