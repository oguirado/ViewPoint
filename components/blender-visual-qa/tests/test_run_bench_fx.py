"""FX round 1 on W9 (JW9-3/4/5): effective profile reaches the truth labels, coverage flows to the scorer,
W10-scale command parsing, chunked capture/decode and a collision-free seed plan."""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from blender_visual_qa.truth import run_bench as rb, score  # noqa: E402
from tests.test_labels import W, H  # noqa: E402
from tests.test_labels_w9 import make_run  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


class ProfileFlowTests(unittest.TestCase):  # JW9-3
    def test_truth_labels_follow_the_effective_profile(self):
        def strong(t0):
            with tempfile.TemporaryDirectory() as d:
                g = make_run(d, anim=True)
                tt, _, _ = rb.truth_labels(d, np.full((2, g.N), 70, np.int64), {"T0_Q4": t0})
                return int((tt["tl"][1] == 6).sum())
        self.assertGreater(strong(32), 0)
        self.assertEqual(strong(128), 0)

    def _spy_analyze(self, **kw):
        seen = {}
        g = rb.Grid(W, H)
        vrun = SimpleNamespace(grid=g, tiles=[None, None], params_sha256="x")

        def spy(run, resid=None, profile=None):
            seen.update(resid=resid, profile=profile)
            raise rb.BenchError("E_SPY", "stop")
        noisy = np.full((2, g.N), 9, np.int64)
        with patch.object(rb, "vision_analyze", return_value=(vrun, 0)), patch.object(rb, "raw_resid", return_value=noisy), \
                patch.object(rb, "truth_labels", side_effect=spy):
            with self.assertRaises(rb.BenchError):
                rb.analyze_bench("B1", "unused", 4, rb.vparams.effective({"T0_Q4": 125}), **kw)
        return seen, noisy, g

    def test_analyze_bench_passes_the_table_t0_as_profile(self):
        seen, _, _ = self._spy_analyze()
        self.assertEqual(seen["profile"], {"T0_Q4": 125})

    def test_clean_residual_replaces_noisy_beauty_residual_for_labels(self):
        g = rb.Grid(W, H)
        clean = np.zeros((2, g.N), np.int64)
        seen, noisy, _ = self._spy_analyze(clean=clean)
        self.assertIs(seen["resid"], clean)
        seen2, noisy2, _ = self._spy_analyze()
        self.assertTrue((seen2["resid"] == noisy2).all())  # B1 (not a clean bench): beauty residual for INVISIBLE

    def test_clean_selfcheck_doubles_then_refuses_rc6(self):
        g = rb.Grid(W, H)
        tiles = [object(), object()]
        vrun = SimpleNamespace(grid=g, tiles=tiles)
        spps = []
        with patch.object(rb, "render_clean", side_effect=lambda b, run, w, h, n, spp, k: spps.append(spp) or Path("c%d" % k)), \
                patch.object(rb, "vision_analyze", return_value=(vrun, 0)), \
                patch.object(rb.change, "residual", return_value=np.full(g.N, 9999, np.int64)):
            with self.assertRaises(rb.BenchError) as ctx:
                rb.clean_resid("r", "B3", rb.vparams.effective(None), W, H, 2)
        self.assertEqual(ctx.exception.rc, 6)
        self.assertEqual(sorted(set(spps)), [1024, 2048, 4096])


class CoverageTests(unittest.TestCase):  # JW9-4
    def _run(self, cov_ppm, write_meta=True):
        d = tempfile.mkdtemp()
        g = rb.Grid(W, H)
        ldir = Path(d) / "labels"
        ldir.mkdir()
        if write_meta:
            (ldir / "frames_meta.jsonl").write_text("".join(
                json.dumps({"frame": f, "cov_ppm": cov_ppm, "cam_motion": 0}) + "\n" for f in (1, 2, 3)))
        tl = np.full((3, g.N), score.TL_FROZEN, np.int64)
        tt = {"tl": tl, "mv": np.zeros((3, g.N, 2), np.int64)}
        tiles = [{"cls": np.full(g.N, score.C_FROZEN, np.uint8), "mv_dx": np.zeros(g.N, np.int8),
                  "mv_dy": np.zeros(g.N, np.int8), "morton": np.arange(g.N, dtype=np.uint32),
                  "resid": np.zeros(g.N, np.uint8)} for _ in range(3)]
        vrun = SimpleNamespace(grid=g, tiles=tiles, params_sha256="x", regions=[[], [], []], global_mv=[(0, 0)] * 3)
        lres = {"sha256": "s", "frame_ids": [1, 2, 3]}
        with patch.object(rb, "vision_analyze", return_value=(vrun, 0)), patch.object(rb, "raw_resid", return_value=np.zeros((3, g.N), np.int64)), \
                patch.object(rb, "truth_labels", return_value=(tt, lres, ldir)):
            return rb.analyze_bench("B4f", d, 4, rb.vparams.effective(None)), g

    def test_frozen_strata_fill_from_frames_meta(self):
        res, g = self._run(400000)
        fh = res["row"]["frozen_hit"]
        self.assertEqual(fh["ge300k"], {"n": 3 * g.N, "hit": 3 * g.N})
        self.assertEqual(fh["lt50k"]["n"], 0)
        res, g = self._run(20000)
        self.assertEqual(res["row"]["frozen_hit"]["lt50k"]["n"], 3 * g.N)

    def test_missing_or_misaligned_coverage_blocks(self):
        with self.assertRaises(rb.BenchError) as ctx:
            self._run(400000, write_meta=False)
        self.assertEqual((ctx.exception.code, ctx.exception.rc), ("E_TRUTH_COVERAGE", 5))
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "frames_meta.jsonl").write_text(json.dumps({"frame": 1, "cov_ppm": 1, "cam_motion": 0}) + "\n")
            with self.assertRaises(rb.BenchError):
                rb.read_coverage(d, [1, 2], 4)

    def test_rows_carry_capture_identity(self):
        res, _ = self._run(1)
        self.assertIn("capture_sha256", res["row"])


class W10ScaleTests(unittest.TestCase):  # JW9-5
    W10 = ["--out", "x", "--frames", "60", "--spp", "4,16,64", "--seeds", "fit:0-9,gate:10-39", "--jobs", "4", "--strict"]

    def test_w10_command_parses(self):
        a = rb.parse_args(self.W10)
        with patch.object(rb, "BLENDER", sys.executable):
            benches, w, h = rb.validate(a)
        self.assertEqual((a.frames, a.spp_tiers, a.jobs, a.cpu_plan), (60, [4, 16, 64], 4, "w10"))
        self.assertEqual(len(benches), 13)
        self.assertEqual(len(set(rb.CPU_PLANS["w10"])), 4)

    def test_limits_still_hold(self):
        for bad in (["--frames", "61"], ["--spp", "8"], ["--spp", "4,4"], ["--spp", "128"], ["--jobs", "5"]):
            argv = [x for x in self.W10 if x != "--strict"]
            with self.subTest(bad=bad), patch.object(rb, "BLENDER", sys.executable), self.assertRaises(rb.BenchError):
                a = rb.parse_args(argv + bad if bad[0] != "--jobs" else argv[:-2] + bad)
                rb.validate(a)

    def test_chunk_ranges_cover_frames_exactly_and_are_bounded(self):
        for n in (2, 8, 9, 60):
            ch = rb.chunk_ranges(n)
            self.assertEqual([f for lo, hi in ch for f in range(lo, hi + 1)], list(range(1, n + 1)))
            self.assertTrue(all(hi - lo + 1 <= rb.CHUNK_FRAMES for lo, hi in ch))

    def test_render_bench_runs_bounded_chunks_and_decodes_each(self):
        cmds = []
        with tempfile.TemporaryDirectory() as d:
            def fake(cmd, scratch, log, timeout=0):
                cmds.append(list(cmd))
                if any(str(c).endswith("bench_scenes.py") for c in cmd):
                    (Path(scratch) / "B1.scene.json").write_text(json.dumps({"seed_mode": "fixed"}))
                return 0
            tok = rb._OPTIONS.set({"decode": "external", "determinism_check": True})
            try:
                with patch.object(rb, "run_blender", side_effect=fake):
                    rb.render_bench("B1", d, 160, 90, 60, 4, 0)
            finally:
                rb._OPTIONS.reset(tok)
        beauty = [c for c in cmds if "-a" in c]
        passes = [c for c in cmds if any(str(x).endswith("passes.py") for x in c)]
        decode = [c for c in cmds if "blender_visual_qa.truth.decode_worker" in c]
        self.assertEqual(len(beauty), 8)
        self.assertEqual(len(passes), 8)
        self.assertEqual(len(decode), 8)
        ranges = [tuple(int(v) for v in c[c.index("--frames") + 1].split(":")) for c in passes]
        self.assertEqual(ranges, rb.chunk_ranges(60))
        self.assertEqual([("--determinism-check" in c) for c in passes], [True] + [False] * 7)

    def test_seed_mode_table_matches_bench_scenes(self):
        src = (REPO / "bpy_side" / "bench_scenes.py").read_text()
        found, cur = set(), None
        for line in src.splitlines():
            m = re.match(r"def (b\w+)\(c, w, h\)", line)
            cur = m[1] if m else cur
            if 'seed_mode = "per_frame"' in line:
                found.add(next(b for b in rb.BENCHES if b.lower() == cur))
        self.assertEqual(found, set(rb.PER_FRAME_BENCHES))

    def test_planned_w10_seed_membership_is_collision_free(self):
        for fit, gate in ((range(0, 10), range(10, 40)), (range(0, 10), range(100, 130))):
            rb.check_seed_collisions(rb.BENCHES, list(fit) + list(gate), 60)
        seeds = set()
        for b in rb.BENCHES:
            for k in range(40):
                for ri in (range(61) if b in rb.PER_FRAME_BENCHES else (0,)):
                    seeds.add(rb.cycles_seed(rb.BENCH_NO[b], k, ri))
        self.assertEqual(len(seeds), 11 * 40 + 2 * 40 * 61)

    def test_naive_all_per_frame_schedule_collides(self):  # review probe seed60: why the plan is pre-registered
        with self.assertRaises(rb.BenchError) as ctx:
            rb.check_seed_collisions(rb.BENCHES, list(range(40)), 60, per_frame=set(rb.BENCHES))
        self.assertIn("collision", str(ctx.exception))

    def test_clean_capture_seed_space_is_reserved(self):
        with self.assertRaises(rb.BenchError):
            rb.parse_seeds("fit:0-1,gate:524288-524289")

    def test_spp_tiers_run_as_separate_calibrations(self):
        calls = []
        with tempfile.TemporaryDirectory() as d, patch.object(rb, "BLENDER", sys.executable), \
                patch.object(rb, "run_tier", side_effect=lambda a, b, w, h, spp, out: calls.append((spp, out.name)) or (5 if spp == 16 else 0)):
            rc = rb.main(["--out", d, "--benches", "B6", "--frames", "2", "--spp", "4,16,64"])
        self.assertEqual(calls, [(4, "spp_4"), (16, "spp_16"), (64, "spp_64")])
        self.assertEqual(rc, 5)



def tier(calib_ret=None, seeds="fit:0-1,gate:2-3"):
    """Drive rb.main through run_tier with mocked render/analyze/calibrate -> (rc, report, captured, analyzed, out files)."""
    import inspect
    captured, analyzed = {}, []

    def result(bench, seed, t0):
        return {"bench": bench, "seed": seed, "grid": "test", "frames": 2, "cpu_ms_per_frame": 0,
                "row": {"bench": bench, "seed": seed, "t0_q4": t0}, "still_q4": [100 + seed],
                "metrics": {"tl": {}, "dirty_precision_ppm": None, "jitter_fp_ppm": None, "mv_exact_ppm": None,
                            "mv_err_p95_px": None, "class_precision_ppm": {}}}

    def fake_run(bench, root, w, h, frames, spp, table, seed=0, analyze=True, scene_seed=None):
        if not analyze:
            return {"bench": bench, "seed": seed, "run_dir": "unused"}
        return dict(result(bench, seed, int(table["T0_Q4"])), run_dir="unused")

    def fake_analyze(bench, run, spp, table, t_wall=None, seed=0, clean=None):
        analyzed.append((seed, int(table["T0_Q4"])))
        r = result(bench, seed, int(table["T0_Q4"]))
        r.pop("still_q4")
        return r

    def calibrate(rows, samples, adapter, renderer, grid, source, strict=False, seeds=None, still_meta=None):
        captured.update(rows=rows, source=source)
        return calib_ret or (None, {"T0_Q4": 0, "checks": [], "passed": False})

    with tempfile.TemporaryDirectory() as d, patch.object(rb, "run_one", side_effect=fake_run), \
            patch.object(rb, "analyze_bench", side_effect=fake_analyze), \
            patch.object(rb.calibrate, "calibrate", side_effect=calibrate):
        calibrate.__signature__ = inspect.signature(calibrate)
        rb.calibrate.calibrate.__signature__ = inspect.signature(calibrate)
        rc = rb.main(["--out", d, "--benches", "B6", "--frames", "2", "--seeds", seeds, "--jobs", "2"])
        rep = json.loads((Path(d) / "calib_report.json").read_text())
        files = {p.name: p.read_bytes() for p in Path(d).iterdir() if p.is_file()}
    return rc, rep, captured, analyzed, files


class ArgonTests(unittest.TestCase):  # v12ar ADV_W9
    def test_clean_resid_returns_temporal_resid(self):  # bt.argon.07: the twin diff is a self-check, not the residual
        g = rb.Grid(W, H)
        vrun = SimpleNamespace(grid=g, tiles=[object(), object()])
        temporal = np.array([[0] * g.N, [50] * g.N], np.int64)
        with patch.object(rb, "render_clean", side_effect=lambda b, run, w, h, n, spp, k: Path("c%d" % k)), \
                patch.object(rb, "vision_analyze", return_value=(vrun, 0)), \
                patch.object(rb.change, "residual", return_value=np.zeros(g.N, np.int64)), \
                patch.object(rb, "raw_resid", return_value=temporal):
            res, spp = rb.clean_resid("r", "B3", rb.vparams.effective(None), W, H, 2)
        self.assertEqual((int(res[1].max()), spp), (50, rb.CLEAN_SPP_START))

    def test_bench_metrics_dirty_precision(self):  # bt.argon.08: dirty verdicts on clean tiles are not TPs
        from tests.test_score import row
        tl = np.array([[0, 3], [0, 3]], np.uint8)
        cls = np.array([[0, 0], [score.C_CHANGED, score.C_MOVED]], np.uint8)
        mv = np.zeros((2, 2, 2), np.int64)
        self.assertEqual(rb.bench_metrics(row(tl, cls, mv, mv.copy()), rb.confusion(tl, cls))["dirty_precision_ppm"], 500000)

    def test_run_tier_refuses_when_t0_moves(self):  # bt.argon.09
        rc, rep, _, _, files = tier(({"T0_Q4": 999}, {"T0_Q4": 999, "checks": [], "passed": True}))
        self.assertEqual((rc, rep["calibration"]["refusal"]), (5, "t0_not_frozen"))
        self.assertEqual(rep["frozen_t0_q4"], (5 * 100 + 3) // 4)
        self.assertNotIn("profile.json", files)


class FrozenFlowTests(unittest.TestCase):  # ADV-W9-02 (2), ADV-W9-03 (2)
    def test_fit_rows_are_not_relabelled_at_t0(self):
        rc, rep, cap, analyzed, _ = tier()
        t0 = (5 * 100 + 3) // 4
        self.assertNotEqual(t0, rb.vparams.effective(None)["T0_Q4"])
        self.assertEqual(analyzed, [(2, t0), (3, t0)])  # only GATE is (re)scored, once, at the frozen T0
        by = {r["seed"]: r["t0_q4"] for r in cap["rows"]}
        self.assertEqual(by, {0: 32, 1: 32, 2: t0, 3: t0})  # FIT rows are the pass-1 rows T0 was frozen on

    def test_still_manifest_carries_still_meta_and_is_hashed(self):
        rc, rep, cap, _, files = tier()
        doc = json.loads(files["still_manifest.json"])
        self.assertEqual(doc["still_meta"]["seeds"], [0, 1])
        self.assertEqual(cap["source"], score.canon.sha256_bytes(files["still_manifest.json"]))

if __name__ == "__main__":
    unittest.main()
