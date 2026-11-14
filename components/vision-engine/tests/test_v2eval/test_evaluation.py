import copy
import gzip
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
from eval import v2proto_global_motion as original_global
from eval.v2proto_subtile import subtile_motion
from eval.gui_synth.score import score
from eval.v2eval import native
from eval.v2eval.baseline import ROOT, assert_match, load_dev, selection
from eval.v2eval.postpass import PostPass, apply_claims, arrays
from visioncore.desc import TILE_DTYPE
from visioncore.grid import Grid


class NativeDifferential(unittest.TestCase):
    def test_sad_exact_random_edges_and_no_overlap(self):
        rng = np.random.default_rng(82)
        lib = native.library()
        for h, w in ((9, 11), (32, 48), (45, 63)):
            p, c = (rng.integers(0, 256, (h, w), dtype=np.uint8) for _ in range(2))
            for dx, dy in ((0, 0), (-7, 4), (3, -8), (w-1, h-1), (w, 0), (0, -h)):
                actual = lib.sad(p, c, dx, dy)
                self.assertEqual(actual, original_global._compute_sad(p, c, dx, dy))

    def test_global_search_retains_original_vector_score_and_abstention(self):
        rng = np.random.default_rng(14)
        a = rng.integers(0, 256, (64, 80), dtype=np.uint8)
        fixtures = [(a, np.roll(a, (3, -9), axis=(0, 1))),
                    (a, rng.integers(0, 256, a.shape, dtype=np.uint8)),
                    (np.zeros_like(a), np.ones_like(a)),
                    (np.tile(np.arange(8, dtype=np.uint8), (64, 10)),
                     np.tile(np.arange(8, dtype=np.uint8), (64, 10)))]
        for p, c in fixtures:
            self.assertEqual(native.estimate(p, c, 15),
                             original_global.estimate_global_motion(p, c, 15))
        # max_d exceeds the plane dimensions: exercise exact empty-overlap
        # sentinel rejection in Python before native pipe requests.
        p = a[:17, :21]
        c = np.roll(p, 2, axis=1)
        self.assertEqual(native.estimate(p, c, 31),
                         original_global.estimate_global_motion(p, c, 31))

    def test_subtile_candidate_order_padding_neighborhood_and_partial_edges(self):
        rng = np.random.default_rng(11)
        for w, h in ((64, 48), (63, 45)):
            grid = Grid(w, h)
            a = rng.integers(0, 256, (h, w), dtype=np.uint8)
            low = np.zeros((h, w), dtype=np.uint8)
            low[12:20, 20:28] = a[12:20, 20:28]
            fixtures = [(a, a), (a, np.roll(a, (3, -4), axis=(0, 1))),
                        (a, rng.integers(0, 256, a.shape, dtype=np.uint8)),
                        (low, np.roll(low, 4, axis=1))]
            ids = np.arange(grid.N)
            for p, c in fixtures:
                rows = native.subtiles(p, c, grid, ids, 5)
                for t, row in zip(ids, rows):
                    expected = subtile_motion(p, c, grid.s,
                                              (int(grid.tx[t]), int(grid.ty[t])), 5)
                    actual = tuple(int(v) for v in row) if row[2] else None
                    self.assertEqual(actual, expected)

    def test_global_uniqueness_blind_spot_has_two_exact_far_aliases(self):
        # A defect witness, not a fix to the read-only research prototype.
        tile = np.random.default_rng(7).integers(0, 256, (16, 16), dtype=np.uint8)
        p = np.tile(tile, (4, 4))
        c = np.roll(p, 3, axis=1)
        self.assertEqual(original_global._compute_sad(p, c, 3, 0), 0)
        self.assertEqual(original_global._compute_sad(p, c, -13, 0), 0)
        self.assertIsNotNone(native.estimate(p, c, 31))

    def test_subtile_exposed_partial_edge_is_proved_only_by_neighbor_blocks(self):
        # Preserve and document the read-only prototype's false-claim defect.
        grid = Grid(64, 36)
        rng = np.random.default_rng(96)
        p = rng.integers(0, 256, (36, 64), dtype=np.uint8)
        c = np.empty_like(p)
        c[:-4] = p[4:]
        c[-4:] = rng.integers(0, 256, (4, 64), dtype=np.uint8)
        actual = subtile_motion(p, c, grid.s, (1, 2), grid.s-1)
        self.assertIsNotNone(actual)
        self.assertEqual(actual[:2], (0, -4))
        # The destination bottom tile has only four rows: its translated
        # source is outside the frame, yet the prototype returns a claim.
        self.assertGreater(grid.H-actual[1], grid.H)

    def test_bulk_and_1080p_global_search_match_original_exactly(self):
        from eval.gui_synth.scenes import build
        from eval.gui_synth.gen import render
        from visioncore.desc import luma_of_padded
        for kind, k, w, h in (("scroll_small", 0, 320, 180),
                              ("scroll_h", 2, 320, 180),
                              ("scroll_large", 0, 1920, 1080)):
            frames = iter(render(build(kind, k, w=w, h=h)))
            p = luma_of_padded(next(frames)[1])
            c = luma_of_padded(next(frames)[1])
            radius = max(w, h)-1
            self.assertEqual(native.estimate(p, c, radius),
                             original_global.estimate_global_motion(p, c, radius))


class ClaimContract(unittest.TestCase):
    def setUp(self):
        self.grid = Grid(64, 48)
        self.tiles = np.zeros(self.grid.N, dtype=TILE_DTYPE)
        self.tiles["cls"] = 2
        self.rgb = np.zeros((48, 64, 3), dtype=np.uint8)

    def test_independent_claims_morton_mapping_int64_and_input_immutable(self):
        adapter = PostPass(self.grid, cache_size=0)
        adapter.update(self.tiles, self.rgb)
        cur = self.rgb.copy()
        cur[0, 0] = 100
        baseline = self.tiles.copy()
        mask = np.zeros((self.grid.gh, self.grid.gw), dtype=bool)
        mask[1, 2] = True
        matches = np.zeros((self.grid.N, 3), dtype=np.int32)
        matches[0] = (4, 0, 1)
        states = np.zeros(self.grid.N, dtype=np.int64)
        states[-1] = 4
        with patch("eval.v2eval.native.estimate", return_value=(200, 0, 0)), \
             patch("eval.v2eval.postpass.consensus_tiles", return_value=mask), \
             patch("eval.v2eval.native.subtiles", return_value=matches), \
             patch.object(adapter.tracker, "update", return_value=(states, states)):
            out, claims = adapter.update(self.tiles, cur)
        np.testing.assert_array_equal(self.tiles, baseline)
        moved_id = self.grid.order[1, 2]
        self.assertEqual(out["global_motion"]["mv_dx"][moved_id], 200)
        self.assertEqual(np.flatnonzero(claims["global_motion"]["mask"]).tolist(), [moved_id])
        for name in ("global_motion", "flicker", "subtile", "combined"):
            mask = (np.logical_or.reduce([c["mask"] for c in claims.values()])
                    if name == "combined" else claims[name]["mask"])
            for field in ("cls", "mv_dx", "mv_dy"):
                np.testing.assert_array_equal(out[name][field][~mask], baseline[field][~mask])
        self.assertEqual(out["combined"]["cls"][-1], 4)

    def test_zero_motion_does_not_relabel_static_tiles(self):
        adapter = PostPass(self.grid)
        adapter.update(self.tiles, self.rgb)
        cur = self.rgb.copy()
        cur[0, 0] = 1
        with patch("eval.v2eval.native.estimate", return_value=(0, 0, 3)), \
             patch("eval.v2eval.native.subtiles", return_value=np.zeros((self.grid.N, 3), np.int32)):
            out, claims = adapter.update(self.tiles, cur)
        self.assertFalse(claims["global_motion"]["mask"].any())

    def test_subtile_never_promotes_non_changed_baseline(self):
        self.tiles["cls"] = 1
        adapter = PostPass(self.grid)
        adapter.update(self.tiles, self.rgb)
        cur = self.rgb.copy(); cur[0, 0] = 22
        with patch("eval.v2eval.native.estimate", return_value=None), \
             patch("eval.v2eval.native.subtiles") as kernel:
            out, claims = adapter.update(self.tiles, cur)
        kernel.assert_not_called()
        self.assertFalse(claims["subtile"]["mask"].any())

    def test_cache_retains_class_eligibility_and_causal_tracker_progress(self):
        adapter = PostPass(self.grid)
        cur = self.rgb.copy(); cur[0, 0] = 30
        zero = np.zeros((self.grid.N, 3), np.int32)
        with patch("eval.v2eval.native.estimate", return_value=None), \
             patch("eval.v2eval.native.subtiles", return_value=zero):
            for i in range(8):
                out, _ = adapter.update(self.tiles, self.rgb if i%2 == 0 else cur,
                                        frame_key=str(i%2))
        self.assertGreater(adapter.cache_hits, 0)
        self.assertEqual(adapter.calls["flicker"], 8)

    def test_real_tracker_declares_held_phase_and_warmup_is_causal(self):
        adapter = PostPass(self.grid)
        for i in range(12):
            self.tiles["xh64"][:] = (i//3)%2
            out, claims = adapter.update(self.tiles, self.rgb)
            self.assertEqual(bool(claims["flicker"]["mask"].any()), i >= 11)

    def test_conflict_precedence_is_explicit(self):
        base = arrays(self.tiles)
        mask = np.zeros(self.grid.N, bool); mask[2] = True
        zero = np.zeros(self.grid.N, dtype=np.int64)
        a = {"mask": mask, "cls": 3, "mv_dx": zero+7, "mv_dy": zero}
        b = {"mask": mask, "cls": 4, "mv_dx": zero, "mv_dy": zero}
        out = apply_claims(base, [a, b])
        self.assertEqual((out["cls"][2], out["mv_dx"][2]), (4, 0))


class DevBaselineRegression(unittest.TestCase):
    def test_unavailable_cost_and_empty_silent_miss_support_stay_null(self):
        from eval.v2eval.run import add_honesty, omit_scorer_default_timings
        cls = np.zeros((2, 2), dtype=np.int64)
        row = score(cls, cls, cls, [], scene_id="DEV:static", kind="static_clean")
        add_honesty(row)
        omit_scorer_default_timings(row)
        self.assertIsNone(row["honesty"]["silent_miss_ppm"])
        self.assertIsNone(row["metrics"]["ms_per_frame"])
        self.assertFalse(row["per_class"]["MOVED"]["recall"]["defined"])

    def test_selection_is_exactly_sealed_dev(self):
        from eval.v2eval.run import verify_source_seal
        dev = load_dev()
        self.assertTrue(verify_source_seal(dev))
        scenes = selection(dev)
        self.assertEqual({s["scene_id"] for s in scenes}, set(dev["scene_units"]))
        self.assertTrue(all(s["split"] == "DEV" and 0 <= s["k"] < 64 for s in scenes))

    def test_recomputed_full_dev_metrics_match_and_mutant_fails(self):
        result_path = ROOT/"eval/v2eval/results.json"
        if result_path.exists():
            actual = json.loads(result_path.read_text())["variants"]["baseline"]
        else:
            from eval.gui_synth.score import aggregate
            from eval.v2eval.run import SCRATCH, identity
            dev = load_dev()
            paths = [SCRATCH/(identity(s)+".baseline.json") for s in selection(dev)]
            if not all(p.exists() for p in paths):
                self.skipTest("full DEV baseline is still computing")
            actual = aggregate([json.loads(p.read_text()) for p in paths])
        self.assertTrue(assert_match(actual, load_dev()))
        mutant = copy.deepcopy(actual)
        mutant["confusion"][3][2] += 1
        with self.assertRaisesRegex(ValueError, "baseline mismatch"):
            assert_match(mutant, load_dev())

    def test_hand_computed_metrics_detect_wrong_motion_and_transposed_confusion(self):
        cls = np.array([[0, 0], [3, 2], [3, 4]], dtype=np.int64)
        dx = np.array([[0, 0], [12, 0], [-12, 0]], dtype=np.int64)
        dy = np.zeros_like(dx)
        events = [{"scene_id": "DEV:fixture", "f0": 1, "f1": 3, "tiles": [0],
                   "ideal": 3, "v1_expected": 2, "v1_basis": "HYP", "mv": [12, 0]},
                  {"scene_id": "DEV:fixture", "f0": 1, "f1": 2, "tiles": [1],
                   "ideal": 2, "v1_expected": 2, "v1_basis": "HYP"}]
        row = score(cls, dx, dy, events, scene_id="DEV:fixture", kind="typing")
        self.assertEqual(row["mv_exact"]["fraction"], "1/2")
        self.assertEqual(row["confusion"][0][4], 1)
        self.assertEqual(row["confusion"][4][0], 0)
        self.assertFalse(row["per_class"]["FLICKER"]["recall"]["defined"])
