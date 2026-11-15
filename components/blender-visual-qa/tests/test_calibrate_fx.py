"""FX r1 (JW9-1, JW9-2): calibrate refuses absent/empty/overlapping FIT-GATE partitions and incomplete acceptance evidence."""
import json
import tempfile
import unittest
from pathlib import Path

from blender_visual_qa.truth import calibrate, score
from tests.test_calibrate import good_rows, split_set, evidence_rows


def run(rows, sd, meta, still=(4,)):
    return calibrate.calibrate(rows, list(still), "blender", "r", "g", "ab", seeds=sd, still_meta=meta)


def cli(rows, still_meta, seeds):
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "rows.jsonl").write_text("".join(score.canon.dumps(r) for r in rows))
        (d / "still.json").write_text(json.dumps({"samples_q4": [4], "still_meta": still_meta}))
        a = ["--rows", str(d / "rows.jsonl"), "--still", str(d / "still.json"), "--out", str(d / "p.json"), "--grid", "g"]
        rc = calibrate.main(a + (["--seeds", seeds] if seeds else []))
        rep = json.loads((d / "calib_report.json").read_text()) if (d / "calib_report.json").exists() else None
        return rc, (d / "p.json").exists(), rep


class SplitLeakTests(unittest.TestCase):
    def base(self):
        return split_set(good_rows(), good_rows())

    def test_baseline_passes(self):
        p, rep = run(*self.base())
        self.assertIsNotNone(p, rep["checks"])

    def test_absent_partition_refused_python_and_cli(self):
        rows, sd, meta = self.base()
        p, rep = run(rows, None, meta)
        self.assertIsNone(p)
        self.assertEqual(rep["refusal"], "split_leak")
        self.assertEqual(cli(rows, meta, None)[:2], (5, False))   # strict CLI on a passing set without --seeds

    def test_empty_arm_refused_no_fallback_to_all_rows(self):
        rows, sd, meta = self.base()
        for arm in ("fit", "gate"):
            s2 = dict(sd, **{arm: []})
            p, rep = run(rows, s2, meta)
            self.assertIsNone(p, arm)
            self.assertEqual(rep["refusal"], "split_leak")
        self.assertEqual(cli(rows, meta, "fit:1-3,gate:50-49")[:2], (5, False))

    def test_duplicate_capture_sha_under_different_seeds_refused(self):
        rows, sd, meta = self.base()
        gate_cap = next(r["capture_sha256"] for r in rows if r.get("seed") == sd["gate"][0] and r["bench"] == "B1")
        for r in rows:
            if r.get("seed") == sd["fit"][0] and r["bench"] == "B1":
                r["capture_sha256"] = gate_cap
        p, rep = run(rows, sd, meta)
        self.assertIsNone(p)
        self.assertIn("capture_sha_overlap", rep["split_violations"])
        rc, wrote, _ = cli(rows, meta, "fit:1-3,gate:100-102")
        self.assertEqual((rc, wrote), (5, False))

    def test_row_without_capture_sha_or_seed_outside_partition_refused(self):
        rows, sd, meta = self.base()
        rows[0].pop("capture_sha256")
        self.assertIn("row_without_capture_sha256", run(rows, sd, meta)[1]["split_violations"])
        rows, sd, meta = self.base()
        rows[0]["seed"] = 999
        self.assertIn("row_seed_outside_partition", run(rows, sd, meta)[1]["split_violations"])

    def test_still_probe_must_be_bound_to_fit(self):
        rows, sd, meta = self.base()
        for bad, why in ((None, "still_probe_unbound"), ({"seeds": [sd["gate"][0]]}, "still_probe_not_fit"),
                         ({"seeds": [sd["fit"][0]], "capture_sha256": [rows[-1]["capture_sha256"]] and
                           [next(r["capture_sha256"] for r in rows if r.get("seed") == sd["gate"][0])]}, "still_probe_capture_in_gate")):
            p, rep = run(rows, sd, bad)
            self.assertIsNone(p, why)
            self.assertIn(why, rep["split_violations"])

    def test_seed_overlap_is_split_leak(self):
        rows, sd, meta = split_set(good_rows(n=2), good_rows(n=2), gate0=2)
        p, rep = run(rows, sd, meta)
        self.assertEqual((p, rep["refusal"], rep["seeds_overlap"]), (None, "split_leak", [2]))


class AcceptanceCompletenessTests(unittest.TestCase):
    def mutate(self, fn):
        rows, sd, meta = split_set(good_rows(), good_rows())
        gate = set(sd["gate"])
        fn([r for r in rows if r.get("seed") in gate])
        return run(rows, sd, meta)

    def refused(self, fn, check):
        p, rep = self.mutate(fn)
        self.assertIsNone(p, check)
        self.assertEqual(rep["refusal"], None)
        self.assertEqual(rep["split_violations"], [])
        self.assertIn(check, [c["name"] for c in rep["checks"] if c["status"] == "FAIL"])

    def drop(self, key, bench=None):
        def f(gate):
            for r in gate:
                if bench is None or r["bench"] == bench:
                    r.pop(key, None)
        return f

    def test_zero_frozen_hits_refused(self):
        def f(gate):
            for r in gate:
                if r["bench"] == "B4":
                    r["frozen_hit"]["ge300k"]["hit"] = 0
        self.refused(f, "frozen_ge300k_seed_success")

    def test_zero_flicker_hits_refused(self):
        def f(gate):
            for r in gate:
                if r["bench"] == "B4":
                    r["tl"]["9"]["cls_ok"] = 0
        self.refused(f, "flicker_seed_success")

    def test_single_seed_flicker_miss_refused(self):   # seed-level success: 30/30 means every seed
        def f(gate):
            next(r for r in gate if r["bench"] == "B4")["tl"]["9"]["cls_ok"] = 3
        self.refused(f, "flicker_seed_success")

    def test_flicker_latency_over_four_frames_refused(self):
        def f(gate):
            next(r for r in gate if r["bench"] == "B4")["latency_max"] = {"9": 5}
        self.refused(f, "flicker_latency_max_frames")

    def test_missing_drift_frozen_temporal_rows_refused(self):
        def f(gate):
            for r in gate:
                if r["bench"] == "B4":
                    r["tl"].pop("11")
                    r["tl"].pop("10")
        self.refused(f, "drift_seed_success")

    def test_b4n_fp_tile_refused(self):
        def f(gate):
            next(r for r in gate if r["bench"] == "B4n")["flicker_fp_tiles"] = 1
        self.refused(f, "b4n_temporal_fp_tiles")

    def test_c1b_flip_and_absence_refused(self):
        def f(gate):
            next(r for r in gate if r["bench"] == "B6")["c1b_flips"] = 1
        self.refused(f, "c1b_class_flips")
        self.refused(self.drop("c1b_flips"), "c1b_class_flips")

    def test_missing_c5_refused(self):
        self.refused(self.drop("c5_select"), "c5_select_ppm")

    def test_c5_below_100_refused(self):
        def f(gate):
            next(r for r in gate if "c5_select" in r)["c5_select"] = {"n": 10, "hit": 9}
        self.refused(f, "c5_select_ppm")

    def test_b5o_motion_missing_or_wrong_refused(self):
        self.refused(self.drop("b5o_global_mv"), "b5o_global_mv_ppm")
        def f(gate):
            next(r for r in gate if "b5o_global_mv" in r)["b5o_global_mv"] = {"n": 10, "exact": 9}
        self.refused(f, "b5o_global_mv_ppm")

    def test_iou_missing_or_low_refused(self):
        self.refused(self.drop("s4_iou_ppm"), "s4_iou_median_ppm")
        def f(gate):
            for r in gate:
                if "s4_iou_ppm" in r:
                    r["s4_iou_ppm"] = 700000
        self.refused(f, "s4_iou_median_ppm")

    def test_missing_class_rows_refuse_without_strict(self):   # ABSENT is a refusal, not a pass
        for tl in ("0", "3", "4", "5", "6", "7"):
            def f(gate, tl=tl):
                for r in gate:
                    r["tl"].pop(tl, None)
            p, rep = self.mutate(f)
            self.assertIsNone(p, tl)
            self.assertTrue(any(c.get("absent") for c in rep["checks"]), tl)

    def test_complete_evidence_checks_have_denominators(self):
        rows, sd, meta = split_set(good_rows(), good_rows())
        p, rep = run(rows, sd, meta)
        names = {c["name"] for c in rep["checks"]}
        for n in ("flicker_seed_success", "drift_seed_success", "frozen_ge300k_seed_success", "c1b_class_flips", "c5_select_ppm",
                  "b5o_global_mv_ppm", "s4_iou_median_ppm", "b4n_temporal_fp_tiles", "mv_exact", "mv_err_p95_px"):
            self.assertIn(n, names)
        self.assertEqual([c["den"] for c in rep["checks"] if c["name"] == "flicker_seed_success"], [len(sd["gate"])])



class ArgonTests(unittest.TestCase):  # v12ar ADV_W9
    def test_s4_iou_median_sorts_rows(self):  # bt.argon.05
        rows = [{"bench": "B1", "seed": s, "s4_iou_ppm": v} for s, v in zip((1, 2, 3), (950000, 600000, 900000))]
        self.assertEqual(dict((n, v) for n, v, *_ in calibrate.required_checks({}, rows, [1, 2, 3]))["s4_iou_median_ppm"], 900000)

if __name__ == "__main__":
    unittest.main()
