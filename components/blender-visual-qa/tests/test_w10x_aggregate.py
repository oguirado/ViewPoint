"""W10X: night-1 'every check None' = split_leak refusal (capture_sha_overlap), seed-independent scenes, offline rescore."""
import glob
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from blender_visual_qa.truth import calibrate
from blender_visual_qa.truth import run_bench as rb
from tests.test_calibrate import good_rows, split_set, freeze

ROOT = Path(__file__).resolve().parents[1]
N1 = ROOT / "build" / "frozen_301d38a" / "w10" / "run_spp4"
STILL = [4]
_spec = importlib.util.spec_from_file_location("rescore_w10", ROOT / "tools" / "rescore_w10.py")
rescore_w10 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rescore_w10)


def leaked_set():
    """Valid frozen partition, then one GATE row gets the capture of a FIT row of its bench (a seed-blind render)."""
    rows, sd, meta = split_set(good_rows(n=3), good_rows(n=3))
    rows = freeze(rows, sd, STILL)
    f = next(r for r in rows if r["seed"] == sd["fit"][0])
    g = next(r for r in rows if r["seed"] == sd["gate"][0] and r.get("bench") == f.get("bench"))
    g["capture_sha256"] = f["capture_sha256"]
    return rows, sd, meta, f


def write_run(d, rows, sd, meta):
    d.mkdir(parents=True)
    (d / "rows.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    (d / "calib_report.json").write_text(json.dumps({"seeds": sd, "renderer": "r", "grid": "g", "frozen_t0_q4": 32}))
    (d / "still_manifest.json").write_text(json.dumps({"samples_q4": STILL, "still_meta": meta}))


class CauseTests(unittest.TestCase):
    def test_capture_overlap_refuses_and_names_the_bench(self):
        rows, sd, meta, f = leaked_set()
        _, rep = calibrate.calibrate(rows, STILL, "blender", "r", "g", "src", True, sd, meta)
        self.assertEqual(rep["refusal"], "split_leak")
        self.assertIn("capture_sha_overlap", rep["split_violations"])
        targets = {t[0] for t in calibrate.TARGETS}
        self.assertTrue(all(c["value"] is None for c in rep["checks"] if c["name"] in targets))  # night-1 shape
        self.assertEqual([(d["bench"], d["fit"], d["gate"]) for d in rep["split_detail"]],
                         [(f.get("bench"), [sd["fit"][0]], [sd["gate"][0]])])

    def test_valid_partition_has_values_and_no_detail(self):
        rows, sd, meta = split_set(good_rows(n=3), good_rows(n=3))
        _, rep = calibrate.calibrate(freeze(rows, sd, STILL), STILL, "blender", "r", "g", "src", True, sd, meta)
        self.assertNotIn("split_detail", rep)
        self.assertIsNotNone(next(c for c in rep["checks"] if c["name"] == "static_fp")["value"])

    def test_summary_prints_refusal_and_seed_columns(self):
        rep = {"grid": "g", "spp": 4, "frames": 4, "renderer": "r", "failures": [], "frozen_t0_q4": 281,
               "benches": [{"bench": "B1", "seed": 10, "seed_group": "gate", "cpu_ms_per_frame": 1,
                            "metrics": {"tl": {}, "dirty_precision_ppm": None, "jitter_fp_ppm": 0, "mv_exact_ppm": None,
                                        "mv_err_p95_px": None, "class_precision_ppm": {}}}],
               "calibration": {"refusal": "split_leak", "split_violations": ["capture_sha_overlap"], "checks": [],
                               "split_detail": [{"bench": "B2", "fit": [0], "gate": [10], "capture_sha256": "ab" * 32}]}}
        md = rb.summary_md(rep)
        self.assertIn("REFUSED: **split_leak**", md)
        self.assertIn("overlap B2 fit [0] gate [10]", md)
        self.assertIn("| B1 | gate | 10 |", md)


class RescoreTests(unittest.TestCase):
    def test_cp_lower(self):
        for k, n, lb in ((10, 10, 0.741), (20, 20, 0.861), (30, 30, 0.905), (9, 10, 0.606)):
            self.assertAlmostEqual(rescore_w10.cp_lower(k, n), lb, places=3)
        self.assertEqual(rescore_w10.cp_lower(0, 10), 0.0)

    def test_rescore_reports_gate_values_despite_refusal(self):
        rows, sd, meta, _ = leaked_set()
        with tempfile.TemporaryDirectory() as t:
            run = Path(t) / "run"
            write_run(run, rows, sd, meta)
            before = {p.name: p.read_bytes() for p in run.iterdir()}
            doc = rescore_w10.rescore(run, Path(t) / "out")
            self.assertEqual(doc["official"]["refusal"], "split_leak")
            self.assertFalse(doc["certifying"])
            sfp = next(c for c in doc["diagnostic_gate_only"] if c["name"] == "static_fp")
            self.assertIsNotNone(sfp["value"])
            self.assertEqual({p.name: p.read_bytes() for p in run.iterdir()}, before)
            self.assertTrue((Path(t) / "out" / "RESCORE.md").is_file())

    def test_rescore_never_overwrites(self):
        rows, sd, meta, _ = leaked_set()
        with tempfile.TemporaryDirectory() as t:
            run = Path(t) / "run"
            write_run(run, rows, sd, meta)
            for out in (run, run / "sub"):
                with self.assertRaises(SystemExit):
                    rescore_w10.rescore(run, out)
            (Path(t) / "full").mkdir()
            (Path(t) / "full" / "x").write_text("x")
            with self.assertRaises(SystemExit):
                rescore_w10.rescore(run, Path(t) / "full")


@unittest.skipUnless((N1 / "rows.jsonl").is_file(), "night-1 frozen evidence absent")
class NightOneEvidenceTests(unittest.TestCase):
    def test_night1_cause_is_capture_overlap(self):
        rows = [json.loads(ln) for ln in (N1 / "rows.jsonl").read_text().splitlines()]
        st = json.loads((N1 / "still_manifest.json").read_text())
        sd = json.loads((N1 / "calib_report.json").read_text())["seeds"]
        _, rep = calibrate.calibrate(rows, st["samples_q4"], "blender", "r", "g", "src", True, sd, st["still_meta"])
        self.assertEqual(rep["split_violations"][:3], ["capture_sha_overlap", "still_probe_capture_in_gate", "still_probe_seed_unbound"])
        self.assertIn("still_probe_degenerate", rep["split_violations"])  # W10S: night-1 B6 still probe is all zero
        self.assertEqual({d["bench"] for d in rep["split_detail"]}, {"B1x", "B2", "B5o", "B6", "B8", "BS"})

    def test_night1_scenes_do_not_depend_on_seed(self):
        by = {}
        for p in glob.glob(str(N1 / "*" / "seed_*" / "*" / "*.scene.json")):
            by.setdefault(Path(p).parent.name, set()).add(hashlib.sha256(Path(p).read_bytes()).hexdigest())
        self.assertEqual(len(by), 13)
        self.assertEqual({b: len(v) for b, v in by.items()}, {b: 1 for b in by})


if __name__ == "__main__":
    unittest.main()
