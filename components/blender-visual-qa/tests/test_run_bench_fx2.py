"""FX round 2 on W9 (JW9b-2, JW9b-3): the runner PRODUCES every acceptance metric from actual stage outputs (engine
selector, regions, global_mv; truth labels), those rows drive calibrate, and B3/B5 truth never falls back to the noisy
beauty residual (calibration runs capture clean residuals by default; absent clean evidence refuses)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from blender_visual_qa.truth import calibrate, run_bench as rb, score  # noqa: E402
from tests.test_calibrate import good_rows, split_set  # noqa: E402
from tests.test_labels import W, H  # noqa: E402

F = 3
EVIDENCE = ("c1b_flips", "c5_select", "b5o_global_mv", "s4_iou_ppm")


def stage(bench, n_obj=4):
    """Synthetic engine + truth stage outputs for one bench: n_obj RIGID tiles of object 5 moving (2, 0) and detected
    MOVED (B5o: every tile pans (3, 0)); one engine region covers the object; engine global_mv = the pan."""
    g = rb.Grid(W, H)
    m = np.asarray(g.mortons, dtype=np.uint32)
    pan = bench == "B5o"
    k = g.N if pan else (0 if bench == "B6" else n_obj)
    dx = 3 if pan else 2
    tl = np.zeros((F, g.N), np.int64)
    ob = np.zeros((F, g.N), np.int64)
    tmv = np.zeros((F, g.N, 2), np.int64)
    tl[1:, :k], ob[1:, :k], tmv[1:, :k, 0] = 3, 5, dx
    tiles = []
    for f in range(F):
        cls = np.where(tl[f] == 3, score.C_MOVED, score.C_UNCHANGED).astype(np.uint8)
        tiles.append({"morton": m, "cls": cls, "resid": np.where(tl[f] == 3, 250, 0).astype(np.uint8),
                      "mv_dx": np.where(tl[f] == 3, dx, 0).astype(np.int8), "mv_dy": np.zeros(g.N, np.int8)})
    regions = [[]] + [[{"spans": [["%x" % int(x), "%x" % int(x)] for x in m[:k]]}] if k else [] for _ in range(1, F)]
    vrun = SimpleNamespace(grid=g, tiles=tiles, params_sha256="x", regions=regions,
                           global_mv=[(0, 0)] + [(dx, 0) if pan else (0, 0)] * (F - 1))
    tt = {"tl": tl, "mv": tmv, "ob": ob}
    return vrun, tt, g


def analyze(bench, seed=0, table=None, clean=None):
    vrun, tt, g = stage(bench)
    lres = {"sha256": "s", "frame_ids": list(range(F))}
    seen = {}

    def labels(run, resid=None, profile=None):
        seen["resid"] = resid
        return tt, lres, Path(run)
    with tempfile.TemporaryDirectory() as d, \
            patch.object(rb, "vision_analyze", return_value=(vrun, 0)), \
            patch.object(rb, "raw_resid", return_value=np.full((F, g.N), 9, np.int64)), \
            patch.object(rb, "truth_labels", side_effect=labels), \
            patch.object(rb, "read_coverage", return_value=np.zeros((F, g.N), np.int64)), \
            patch.object(rb, "capture_sha", return_value="cap-%s-%d" % (bench, seed)):
        res = rb.analyze_bench(bench, d, 4, table or rb.vparams.effective(None), seed=seed, clean=clean)
    return res, seen


class ProductionTests(unittest.TestCase):  # JW9b-2
    def test_analyze_bench_produces_every_acceptance_metric(self):
        b6, _ = analyze("B6")
        self.assertEqual(b6["row"]["c1b_flips"], 0)
        b1, _ = analyze("B1")
        self.assertEqual(b1["row"]["s4_iou_ppm"], 1000000)
        self.assertGreater(b1["row"]["c5_select"]["n"], 0)
        self.assertEqual(b1["row"]["c5_select"]["hit"], b1["row"]["c5_select"]["n"])
        b5o, _ = analyze("B5o")
        self.assertEqual(b5o["row"]["b5o_global_mv"], {"n": F - 1, "exact": F - 1})

    def test_c5_runs_the_engine_selector(self):
        from visioncore import pyramid
        with patch.object(pyramid.Pyramid, "select", lambda self, B, rsel=None: []):  # selector drops every node
            b1, _ = analyze("B1")
        self.assertGreater(b1["row"]["c5_select"]["n"], 0)
        self.assertEqual(b1["row"]["c5_select"]["hit"], 0)

    def test_produced_evidence_drives_calibrate_and_seed_gaps_refuse(self):
        rows, sd, meta = split_set(good_rows(n=2), good_rows(n=2))
        rows = [{k: v for k, v in r.items() if k not in EVIDENCE} for r in rows]  # no injected evidence
        rows = [dict(r, bench="B7") if r["seed"] in sd["gate"] and r["bench"] == "B1" else r for r in rows]
        produced = [analyze(b, seed=s)[0]["row"] for s in sd["gate"] for b in ("B1", "B5o", "B6")]
        own = {(r["bench"], r["seed"]) for r in produced}  # one row per (bench, seed): produced rows own theirs
        rows = [r for r in rows if (r["bench"], r["seed"]) not in own]
        p, rep = calibrate.calibrate(rows + produced, [4], "blender", "r", "g", "ab", seeds=sd, still_meta=meta)
        self.assertIsNotNone(p, [c for c in rep["checks"] if c["status"] == "FAIL"])
        for bench, metric in (("B5o", "b5o_global_mv_ppm"), ("B6", "c1b_class_flips"), ("B1", "s4_iou_median_ppm")):
            drop = [r for r in produced if not (r["bench"] == bench and r["seed"] == sd["gate"][-1])]
            p, rep = calibrate.calibrate(rows + drop, [4], "blender", "r", "g", "ab", seeds=sd, still_meta=meta)
            self.assertIsNone(p)
            c = next(c for c in rep["checks"] if c["name"] == metric)
            self.assertEqual((c["status"], c["missing_seeds"]), ("FAIL", [sd["gate"][-1]]))


class CleanTests(unittest.TestCase):  # JW9b-3
    W10 = ["--out", "x", "--frames", "60", "--spp", "4,16,64", "--seeds", "fit:0-9,gate:10-39", "--jobs", "4", "--strict"]

    def _validate(self, argv):
        a = rb.parse_args(argv)
        with patch.object(rb.os.path, "exists", return_value=True):
            rb.validate(a)
        return a

    def test_w10_command_enables_clean_captures(self):
        self.assertEqual(self._validate(self.W10).clean_spp, rb.CLEAN_SPP_START)
        self.assertEqual(self._validate(["--out", "x", "--benches", "B1"]).clean_spp, 0)

    def test_w10_clean_path_executes_for_b3(self):
        a = self._validate(self.W10)
        calls = []
        with tempfile.TemporaryDirectory() as d, patch.object(rb, "render_bench", side_effect=lambda b, run, *x: run.mkdir(parents=True)), \
                patch.object(rb, "clean_resid", side_effect=lambda *x: calls.append(x[1]) or (np.zeros((2, 2), np.int64), x[-1])):
            tok = rb._OPTIONS.set({"clean_spp": a.clean_spp})
            try:
                rb.run_one("B3", d, 32, 32, 2, 4, rb.vparams.effective(None), analyze=False)
                rb.run_one("B1", d, 32, 32, 2, 4, rb.vparams.effective(None), analyze=False)
            finally:
                rb._OPTIONS.reset(tok)
            self.assertTrue((Path(d) / "B3" / "clean_resid.npy").exists())
        self.assertEqual(calls, ["B3"])

    def test_no_noisy_substitution_for_clean_benches(self):
        res, seen = analyze("B3")
        self.assertIsNone(seen["resid"])  # never the noisy beauty residual
        self.assertIs(res["row"]["clean_resid"], False)
        g = rb.Grid(W, H)
        clean = np.full((F, g.N), 1, np.int64)
        res, seen = analyze("B3", clean=clean)
        self.assertIs(seen["resid"], clean)  # disagreement noisy (9) vs clean (1): labels follow clean
        self.assertIs(res["row"]["clean_resid"], True)

    def test_calibrate_refuses_clean_benches_without_clean_evidence(self):
        rows, sd, meta = split_set(good_rows(n=2), good_rows(n=2))
        b3 = dict(json.loads(json.dumps(rows[-1])), bench="B3", seed=sd["gate"][0], capture_sha256="cap-b3", clean_resid=False)
        p, rep = calibrate.calibrate(rows + [b3], [4], "blender", "r", "g", "ab", seeds=sd, still_meta=meta)
        self.assertIsNone(p)
        self.assertEqual(rep["refusal"], "clean_evidence_absent")
        b3["clean_resid"] = True
        p, rep = calibrate.calibrate(rows + [b3], [4], "blender", "r", "g", "ab", seeds=sd, still_meta=meta)
        self.assertIsNotNone(p)


if __name__ == "__main__":
    unittest.main()
