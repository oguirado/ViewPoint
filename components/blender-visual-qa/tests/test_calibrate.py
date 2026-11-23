import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from blender_visual_qa.truth import calibrate, score
from blender_visual_qa.truth.score import VqaError
from tests.test_score import synth, row


def good_rows(fp_resid=10, n=3):
    tl, cls, mv = synth(N=11)
    tl[:, 10] = 5   # DEFORM tile: the acceptance set needs every class
    cls[:, 10] = score.C_CHANGED
    res = np.zeros(tl.shape, np.int32)
    res[tl == 0] = fp_resid
    return [row(tl, cls, mv, mv.copy(), resid=res) for _ in range(n)]


def evidence_rows(gate_seeds):
    """Complete independent temporal/C1b/C5/B5o/IoU evidence for every GATE seed (JW9-2 mandatory set)."""
    out = []
    full = {"n": 4, "dirty": 4, "cls_ok": 4}
    zero = {"schema": score.SCHEMA, "grid": "grid:640x360:s16", "spp": 16, "t0_q4": 32, "frames": 8, "tiles": 4, "excluded": 0,
            "mv_n": 0, "mv_exact": 0, "mv_err_hist": [0] * score.MV_HIST, "drift_duty_n": 0, "drift_duty_hit": 0,
            "drift_duty_ppm": 0, "frozen_hit": {}, "flicker_fp_tiles": 0, "tl": {}}
    for i, g in enumerate(gate_seeds):
        t = dict(zero, bench="B4", seed=g, capture_sha256="ev-b4-%d" % g, tl={"9": dict(full), "10": dict(full), "11": dict(full)},
                 drift_duty_n=10, drift_duty_hit=10, drift_duty_ppm=1000000, latency_max={"9": 3},
                 frozen_hit={"ge300k": {"n": 4, "hit": 4}, "lt50k": {"n": 0, "hit": 0}, "50k_150k": {"n": 0, "hit": 0}})
        out.append(t)
        out.append(dict(zero, bench="B4n", seed=g, capture_sha256="ev-b4n-%d" % g))
        out.append(dict(zero, bench="B6", seed=g, capture_sha256="ev-b6-%d" % g, c1b_flips=0))
        out.append(dict(zero, bench="B1", seed=g, capture_sha256="ev-b1-%d" % g, s4_iou_ppm=900000))
        out.append(dict(zero, bench="B2", seed=g, capture_sha256="ev-b2-%d" % g, s4_iou_ppm=900000,
                        c5_select={"n": 10, "hit": 10}))
        out.append(dict(zero, bench="B5o", seed=g, capture_sha256="ev-b5o-%d" % g, b5o_global_mv={"n": 10, "exact": 10}))
    return out


def still_row(seed, cap):
    """FIT B6 still-probe row whose capture identity the still manifest binds (JW9b-1)."""
    return {"schema": score.SCHEMA, "bench": "B6", "seed": seed, "capture_sha256": cap, "grid": "grid:640x360:s16", "spp": 16,
            "t0_q4": 32, "frames": 8, "tiles": 4, "excluded": 0, "mv_n": 0, "mv_exact": 0, "mv_err_hist": [0] * score.MV_HIST,
            "drift_duty_n": 0, "drift_duty_hit": 0, "drift_duty_ppm": 0, "frozen_hit": {}, "flicker_fp_tiles": 0, "tl": {}}


def stamp(rows, seeds, tag):
    out = []
    for r, s in zip(rows, seeds):
        r = json.loads(json.dumps(r))
        r["seed"], r["capture_sha256"] = s, "%s-%d" % (tag, s)
        out.append(r)
    return out


def split_set(fit_rows, gate_rows, fit0=1, gate0=100, evidence=True, still=(4,)):
    """-> (rows, seeds, still_meta): fit_rows/gate_rows stamped with disjoint seeds + capture shas (+ full GATE evidence),
    GATE rows scored at the T0 frozen from FIT with the still probe `still` (see freeze)."""
    fs = list(range(fit0, fit0 + len(fit_rows)))
    gs = list(range(gate0, gate0 + len(gate_rows)))
    gate = stamp(gate_rows, gs, "gate")
    own = {(r.get("bench"), r["seed"]) for r in gate}  # one row per (bench, seed): given GATE rows win over evidence
    if evidence:  # JW9c-3: S4 acceptance evidence lives on B1 (given B1 rows get the IoU observation, given keys win)
        gate = [dict(r, s4_iou_ppm=r.get("s4_iou_ppm", 900000)) if r.get("bench") == "B1" else r for r in gate]
    ev = [r for r in evidence_rows(gs) if (r["bench"], r["seed"]) not in own] if evidence else []
    rows = stamp(fit_rows, fs, "fit") + gate + ev
    rows.append(still_row(fs[0], "still-%d" % fs[0]))
    sd = {"fit": fs, "gate": gs}
    return freeze(rows, sd, list(still)), sd, {"seeds": fs[:1], "capture_sha256": ["still-%d" % fs[0]]}


def freeze(rows, seeds, still):
    """GATE rows scored at the T0 frozen from FIT (what run_tier does; ADV-W9-03) -> new row list."""
    fit = [r for r in rows if r.get("seed") in set(seeds["fit"])]
    t0 = min(calibrate.fit_t0(fit, still)["T0_Q4"], calibrate.params.BOUNDS["T0_Q4"][1])
    return [dict(r, t0_q4=t0) if r.get("seed") in set(seeds["gate"]) else r for r in rows]


def calib(rows, still, adapter, renderer, grid, src, strict=False, seeds=None, evidence=True):
    """Legacy-shaped call: `rows` are used as FIT (copy) and GATE rows with a valid frozen partition."""
    all_rows, sd, meta = split_set(rows, rows, evidence=evidence)
    return calibrate.calibrate(freeze(all_rows, sd, still), still, adapter, renderer, grid, src, strict, sd, meta)


class CalibrateTests(unittest.TestCase):
    def test_t0_still_formula(self):
        self.assertEqual(calibrate.t0_still([0, 4]), 32)
        self.assertEqual(calibrate.t0_still([40, 3]), 32)  # M=2: rank 999*1//1000 = 0 -> q = 3
        self.assertEqual(calibrate.t0_still([40] * 1000), (5 * 40 + 3) // 4)
        with self.assertRaises(VqaError):
            calibrate.t0_still([])

    def test_profile_takes_max_of_still_and_fp(self):
        p, rep = calib(good_rows(fp_resid=100), [4], "blender", "cycles-cpu-16spp", "g", "ab")
        self.assertEqual(rep["T0_still"], 32)
        self.assertEqual(rep["T0_fp"], 91)  # 91*1.1//1 = 100 >= resid 100; 90 -> 99 < 100
        self.assertEqual(p["T0_Q4"], 91)
        self.assertEqual(p["schema"], "vqa.profile/v1")
        self.assertEqual(p["adapter"], "blender")
        self.assertTrue(rep["passed"])

    def test_refused_on_static_fp(self):  # bt.t0lo.01 analogue
        rows = good_rows()
        rows[0]["tl"]["0"]["dirty"] = 1
        p, rep = calib(rows, [4], "blender", "r", "g", "ab")
        self.assertIsNone(p)
        self.assertFalse(rep["passed"])
        self.assertIn("static_fp", [c["name"] for c in rep["checks"] if c["status"] == "FAIL"])

    def test_refused_on_low_recall(self):  # bt.t0hi.01 analogue
        rows = good_rows()
        rows[0]["tl"]["6"]["dirty"] = 0
        rows[1]["tl"]["6"]["dirty"] = 0
        rows[2]["tl"]["6"]["dirty"] = 0
        p, _ = calib(rows, [4], "blender", "r", "g", "ab")
        self.assertIsNone(p)

    def test_absent_metric_refuses_even_without_strict(self):
        rows = good_rows()
        for r in rows:
            del r["tl"]["6"]
        for strict in (False, True):
            p, rep = calib(rows, [4], "blender", "r", "g", "ab", strict=strict)
            self.assertIsNone(p)
            self.assertIn("shade_strong_recall", [c["name"] for c in rep["checks"] if c.get("absent")])

    def test_t0_out_of_bounds(self):
        p, rep = calib(good_rows(fp_resid=5000), [4], "blender", "r", "g", "ab")
        self.assertIsNone(p)
        self.assertFalse(rep["t0_in_bounds"])

    def test_deterministic_report(self):
        a = calib(good_rows(), [4], "blender", "r", "g", "ab")[1]
        b = calib(good_rows(), [4], "blender", "r", "g", "ab")[1]
        self.assertEqual(score.canon.dumps(a), score.canon.dumps(b))

    def test_cli_pass_and_refuse(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            rp = d / "rows.jsonl"
            rows, sd, meta = split_set(good_rows(), good_rows())
            rp.write_text("".join(score.canon.dumps(r) for r in rows))
            st = d / "still.json"
            st.write_text(json.dumps({"samples_q4": [3, 5], "still_meta": meta}))
            out = d / "profile.json"
            args = ["--rows", str(rp), "--still", str(st), "--out", str(out), "--grid", "g", "--seeds", "fit:1-3,gate:100-102"]
            self.assertEqual(calibrate.main(args), 0)
            self.assertEqual(json.loads(out.read_text())["T0_Q4"], 32)
            self.assertTrue((d / "calib_report.json").exists())
            bad = good_rows()
            bad[0]["tl"]["0"]["dirty"] = 2
            rows, sd, meta = split_set(good_rows(), bad)
            rp.write_text("".join(score.canon.dumps(r) for r in rows))
            out.unlink()
            self.assertEqual(calibrate.main(args), 5)
            self.assertFalse(out.exists())

    def test_cli_bad_still(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "r.jsonl").write_text("")
            (d / "s.json").write_text("{}")
            rc = calibrate.main(["--rows", str(d / "r.jsonl"), "--still", str(d / "s.json"),
                                 "--out", str(d / "p.json"), "--grid", "g"])
            self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
