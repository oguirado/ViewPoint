"""W2/C2 (RPERF-4/5/6, regions prefilter): pipeline wiring, S4 prefilter equivalence, S2 y reuse, param purity."""
import hashlib
import unittest
from unittest import mock

import numpy as np

from tests.test_change import mk, run
from tests.test_change_fix import grid_tiles
from vadapt.synth import synthetic as syn
from visioncore import change, desc, frame as fr, layout, params as P, pipeline, regions as rg
from visioncore.errors import VqaError
from visioncore.grid import Grid

# sha256[:16] over run._blobs(with_vision=False), refreshed after NR (SIG_DEAD_Q4 24, H_DEAD 2; class counts unchanged).
# Pre-NR pins reproduced by reverting only change.py/params.py/thresholds.*; see golden/nr_pin_proof.json (FX3: fx3_pin_proof.json).
BASE_E_SHA = {
    "clean_static": "4596855632543012", "flicker": "4219e65fd50ce84f", "frozen_subregion": "8445dd8421172e8d",
    "gpu_jitter": "aaf69d594c3463e2", "scroll": "fa7dbdbd9e06c400", "slow_drift": "053cf1d485dae77e",
    "small_overlap": "645e5b725caa0246"}


def source(scenario, W=256, H=160, n=16):
    frs, _, _ = syn.render_scenario(scenario, 1, W, H, n)
    return fr.MemoryFrameSource([fr.Frame(i, i * 33333, i, np.ascontiguousarray(r)) for i, r in enumerate(frs)],
                                {"width": W, "height": H})


def blob_sha(run_):
    h = hashlib.sha256()
    for k, v in sorted(run_._blobs(with_vision=False).items()):
        h.update(k.encode())
        h.update(v)
    return h.hexdigest()[:16]


def regions_reference(tiles, W, H, frame):
    """BASE_E regions_of: scans every tile record (kept as the equivalence oracle)."""
    s, gw, gh = layout.grid_params(W, H)
    keep = []
    for i in range(len(tiles)):
        if int(tiles[i]["cls"]) in (2, 3, 4, 5, 6):
            tx, ty = layout.unmorton(int(tiles[i]["morton"]))
            if tx >= gw or ty >= gh:
                raise VqaError("E_VC_ID_RANGE", {"morton": int(tiles[i]["morton"])})
            keep.append(i)
    return rg.regions_of(tiles[keep] if keep else tiles[:0], W, H, frame)


class TestPipelineWiring(unittest.TestCase):
    def test_bit_exact_vs_base_e_all_scenarios(self):
        for sc in syn.SCENARIOS:
            with self.subTest(sc):
                self.assertEqual(blob_sha(pipeline.analyze(source(sc))), BASE_E_SHA[sc])

    def test_describe_receives_prev_tiles_and_shared_y(self):  # RPERF-1b wiring + RPERF-6 (fails on BASE_E)
        real = desc.describe_padded
        calls = []

        def spy(*a, **k):
            calls.append(k)
            return real(*a, **k)

        with mock.patch.object(desc, "describe_padded", spy), \
                mock.patch.object(change, "luma_padded", side_effect=AssertionError("second luma")):
            pipeline.analyze(source("clean_static", n=16))
        self.assertEqual(len(calls), 16)
        self.assertIsNone(calls[0].get("prev_tiles"))
        self.assertTrue(all(c.get("prev_tiles") is not None for c in calls[1:]))
        self.assertTrue(all(c.get("y_plane") is not None for c in calls))

    def test_prev_tiles_is_raw_describe_output(self):  # never the classified `out`
        real = desc.describe_padded
        seen, outs = [], []

        def spy(*a, **k):
            r = real(*a, **k)
            outs.append(r[0].copy())
            seen.append(k.get("prev_tiles"))
            return r

        with mock.patch.object(desc, "describe_padded", spy):
            pipeline.analyze(source("flicker", n=16))
        for k in range(1, len(seen)):
            self.assertEqual(seen[k].tobytes(), outs[k - 1].tobytes())
            self.assertFalse(seen[k]["cls"].any())   # raw: S2 fields still zero


class TestClassifyReusesDescLuma(unittest.TestCase):
    def test_y_from_desc_is_byte_equal_to_change_luma(self):
        frs, _, _ = syn.render_scenario("scroll", 1, 256, 160, 16)
        g = Grid(256, 160)
        for rgb in frs[:4]:
            padded = g.pad(rgb)
            self.assertTrue(np.array_equal(desc.luma_of_padded(padded), change.luma_padded(rgb, g.s, g.gw, g.gh)))

    def test_classify_out_identical_with_either_luma(self):
        frs, _, _ = syn.render_scenario("scroll", 1, 256, 160, 16)
        g = Grid(256, 160)
        shapes = (g.s, g.gw, g.gh)
        res = []
        for luma in (lambda rgb, p: change.luma_padded(rgb, g.s, g.gw, g.gh), lambda rgb, p: desc.luma_of_padded(p)):
            st, prev, digest = None, None, hashlib.sha256()
            for rgb in frs:
                p = g.pad(rgb)
                tiles, weq = desc.describe_padded(p, g, prev)
                y = luma(rgb, p)
                if st is None:
                    st = change.ClassState.init(tiles)
                    out, st, _ = change.classify_first(st, tiles, y_cur=y)
                else:
                    out, st, _ = change.classify_tiles(st, tiles, weq, y_cur=y, grid=shapes)
                digest.update(out.tobytes())
                prev = p
            res.append(digest.hexdigest())
        self.assertEqual(res[0], res[1])


class TestRegionsPrefilter(unittest.TestCase):
    def test_equal_to_full_scan_on_pipeline_tiles(self):
        for sc in ("flicker", "frozen_subregion", "small_overlap", "scroll"):
            run_ = pipeline.analyze(source(sc))
            for k, t in enumerate(run_.tiles):
                self.assertEqual(rg.regions_of(t, 256, 160, k), regions_reference(t, 256, 160, k), (sc, k))

    def test_out_of_grid_morton_raises_even_on_clean_tile(self):  # fails on BASE_E (skipped cls 0 tiles)
        s, gw, gh = layout.grid_params(256, 160)
        t = mk(gw * gh)
        t["morton"] = [layout.morton(i % gw, i // gw) for i in range(gw * gh)]
        t["morton"][3] = layout.morton(gw, 0)       # outside the grid, cls UNCHANGED
        with self.assertRaises(VqaError) as c:
            rg.regions_of(t, 256, 160, 0)
        self.assertEqual(c.exception.code, "E_VC_ID_RANGE")

    def test_out_of_grid_morton_raises_on_dirty_tile(self):
        s, gw, gh = layout.grid_params(256, 160)
        t = mk(gw * gh)
        t["morton"] = [layout.morton(i % gw, i // gw) for i in range(gw * gh)]
        t["morton"][3] = layout.morton(0, gh)
        t["cls"][3] = 2
        with self.assertRaises(VqaError):
            rg.regions_of(t, 256, 160, 0)

    def test_compact_v_matches_scalar(self):
        m = np.arange(0, 65536, 7, dtype=np.int64)
        self.assertEqual([int(x) for x in rg._compact_v(m)], [layout.unmorton(int(v))[0] for v in m])


class TestParamsPurity(unittest.TestCase):
    def test_mv_sad_ratio_den_read_from_effective_table(self):  # RPERF-5 / RCORE-7
        s, gw, gh = 16, 8, 6
        r = np.random.RandomState(5)
        big = (r.randint(0, 40, (gh * s, gw * s + 64)) + 2 * np.arange(gw * s + 64)[None, :]).astype(np.int16)
        prev = big[:, 32:32 + gw * s]
        cur = big[:, 28:28 + gw * s] + r.randint(-2, 3, (gh * s, gw * s)).astype(np.int16)
        t0, t1 = grid_tiles(gw, gh, s, prev), grid_tiles(gw, gh, s, cur)

        def n_moved(params):
            st = change.ClassState.init(t0)
            change.classify_first(st, t0, y_cur=prev)
            o, _, _ = change.classify_tiles(st, t1, np.zeros(len(t1), bool), y_cur=cur, grid=(s, gw, gh),
                                            params=params)
            return int((o["cls"] == change.C_MOVED).sum())
        p16 = dict(P.PARAMS)
        p16["MV_SAD_RATIO_DEN"] = 16
        self.assertGreater(n_moved(None), 0)
        self.assertEqual(n_moved(p16), 0)
        self.assertEqual(P.PARAMS["MV_SAD_RATIO_DEN"], 3)   # the global table is untouched and irrelevant

    def test_drift_threshold_follows_profile_t0(self):  # RPERF-4 / RCORE-1
        for t0 in (32, 60, 64):
            p = P.effective({"t0_q4": t0})
            te, tx_, du = change.thresholds(p)
            self.assertEqual(du, max(te, P.PARAMS["DRIFT_U_Q4"]))
            self.assertGreaterEqual(du, te)
        frames = []
        for i in range(20):
            t = mk(8)
            t["y_q4"] += np.uint16(48 if i % 2 else 0)
            frames.append(t)
        for o, _ in run(frames, params=P.effective({"t0_q4": 64}))[1:]:
            self.assertFalse((o["cls"] == change.C_DRIFT).any())


if __name__ == "__main__":
    unittest.main()
