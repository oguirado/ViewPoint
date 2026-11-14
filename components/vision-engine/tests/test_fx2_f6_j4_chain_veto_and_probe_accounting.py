"""Unit tests for chain flicker veto and false flicker probe accounting contracts."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np

from eval.gui_synth.labels import NAMES, derive
from eval.gui_synth.scenes import build
from eval.v2eval.chain import run_chain
from eval.v2eval.false_flicker_probe import analyze_false_flicker, CAUSE_BUCKETS
from eval.v2eval.run import metrics_from_predictions
from visioncore.desc import TILE_DTYPE
from visioncore.grid import Grid


class DummyVisionRun:
    """Mock VisionRun for false_flicker_probe analyze_fn injection."""

    def __init__(self, grid, n_frames, tiles, frame_rows):
        self.grid = grid
        self.tiles = tiles
        self.frame_rows = frame_rows


class MockVariant:
    """In-memory claim-aware adapter for the current run_chain seam."""

    def __init__(self, spec, grid, budget_ms=1000, updates=None, claim_masks=None):
        self.spec = spec
        self.grid = grid
        self.budget_ms = budget_ms
        self.module_sha256 = "00" * 32
        self.status = "OK"
        self.error = None
        self.ops = 100
        self.frames = 0
        self.updates = updates or []
        self.claim_masks = claim_masks or []
        self.last_claim = None
        self.closed = False

    def update(self, prev, cur, tiles_frame):
        res = self.updates[self.frames]
        # Worker results contain positive claims separately from their baseline
        # overlay. The masks are fixture inputs, not inferred from class changes.
        self.last_claim = {
            "mask": np.array(self.claim_masks[self.frames], dtype=np.bool_),
            "cls": 3 if self.spec.startswith("gm=") else 4,
            "mv_dx": np.asarray(res["mv_dx"], dtype=np.int64).copy(),
            "mv_dy": np.asarray(res["mv_dy"], dtype=np.int64).copy(),
        }
        self.frames += 1
        return res

    def close(self):
        self.closed = True


class TestChainVetoAndProbeAccounting(unittest.TestCase):

    # -------------------------------------------------------------------------
    # PART 1: Real chain entry point veto & precedence accounting
    # -------------------------------------------------------------------------

    def _setup_chain_fixture(self):
        """Construct tiny RAM scene fixture with 4 distinct tile roles for run_chain."""
        # Use gui_synth build to produce complete scene dictionary structure with seed, etc.
        scene = build("static_clean", k=0, w=32, h=32, n=2, split="DEV")
        scene["scene_id"] = "DEV:static_clean:0:chain_fixture"

        grid = Grid(scene["w"], scene["h"])
        self.assertEqual(grid.N, 4)

        # Build 2-frame structured numpy array for engine.tiles
        tiles_struct = np.zeros((2, 4), dtype=TILE_DTYPE)
        tiles_struct["morton"] = grid.mortons

        # Frame 0: static background (cls=0)
        # Frame 1: 4 distinct tile roles:
        # Tile 0: Rejected baseline FLICKER (baseline cls=4, FL tracker abstains/no claim)
        # Tile 1: Genuine tracker FLICKER (baseline cls=0, FL tracker claims cls=4)
        # Tile 2: GM and FL overlap (baseline cls=2, GM claims cls=3 mv_dx=-3 mv_dy=5, FL claims cls=4)
        # Tile 3: Untouched non-FLICKER (baseline cls=0, neither variant claims)
        tiles_struct[1]["cls"] = np.array([4, 0, 2, 0], dtype=np.uint8)

        # Baseline per-frame tiles list for mock analyze
        base_f0 = tiles_struct[0]
        base_f1 = tiles_struct[1]

        # Adapter outputs for Frame 1
        gm_out_f1 = {
            "cls": np.array([4, 0, 3, 0], dtype=np.int64),
            "mv_dx": np.array([0, 0, -3, 0], dtype=np.int64),
            "mv_dy": np.array([0, 0, 5, 0], dtype=np.int64),
        }
        fl_out_f1 = {
            "cls": np.array([0, 4, 4, 0], dtype=np.int64),
            "mv_dx": np.zeros(4, dtype=np.int64),
            "mv_dy": np.zeros(4, dtype=np.int64),
        }

        return scene, grid, tiles_struct, [base_f0, base_f1], gm_out_f1, fl_out_f1

    def test_chain_execution_preconditions_and_immutability(self):
        """Exercise real run_chain entry point and verify input baseline/claim immutability."""
        scene, grid, tiles_struct, base_tiles, gm_out_f1, fl_out_f1 = self._setup_chain_fixture()

        # Deep copies to assert non-mutation
        base_f1_cls_orig = base_tiles[1]["cls"].copy()
        gm_out_f1_cls_orig = gm_out_f1["cls"].copy()
        fl_out_f1_cls_orig = fl_out_f1["cls"].copy()

        gm_mock = MockVariant("gm=mock_gm", grid, updates=[base_tiles[0], gm_out_f1],
                              claim_masks=[[False] * 4, [False, False, True, False]])
        fl_mock = MockVariant("fl=mock_fl", grid, updates=[base_tiles[0], fl_out_f1],
                              claim_masks=[[False] * 4, [False, True, True, False]])

        def mock_variant_factory(spec, grid_arg, budget_ms=1000):
            if "gm=" in spec:
                return gm_mock
            return fl_mock

        def mock_analyze(src, workers=1, cpus=None):
            frame_rows = [{"global_mv": None}, {"global_mv": None}]
            return DummyVisionRun(grid, 2, tiles_struct, frame_rows)

        captured_predictions = {}
        events = derive(scene, grid)

        def mock_metrics_from_predictions(preds, evs, sc):
            for k in ("cls", "mv_dx", "mv_dy"):
                captured_predictions[k] = preds[k].copy()
            return metrics_from_predictions(preds, evs, sc)

        with patch("eval.v2eval.chain.analyze", side_effect=mock_analyze), \
             patch("eval.v2eval.chain._ClaimVariant", side_effect=mock_variant_factory), \
             patch("eval.v2eval.chain.metrics_from_predictions", side_effect=mock_metrics_from_predictions):

            res = run_chain([scene], "gm=mock_gm", "fl=mock_fl", order="gm_then_fl")

        # Verify schema & execution status
        self.assertEqual(res["schema"], "vqa.v2eval-chain/v1")
        self.assertEqual(res["variants"]["gm_then_fl"]["status"], "OK")
        self.assertTrue(gm_mock.closed)
        self.assertTrue(fl_mock.closed)
        self.assertNotIn("baseline_flicker_veto", res)

        # Assert baseline and claim arrays were not mutated
        np.testing.assert_array_equal(base_tiles[1]["cls"], base_f1_cls_orig)
        np.testing.assert_array_equal(gm_out_f1["cls"], gm_out_f1_cls_orig)
        np.testing.assert_array_equal(fl_out_f1["cls"], fl_out_f1_cls_orig)

        # Precondition check on current run_chain output for Frame 1
        cls_f1 = captured_predictions["cls"][1]
        # Mock FL specs opt out by default: rejected baseline FLICKER stays 4.
        self.assertEqual(cls_f1[0], 4)
        # Genuine tracker FLICKER (tile 1) is 4
        self.assertEqual(cls_f1[1], 4)
        # GM precedence on tile 2 is class 3 with exact signed motion fields (-3, 5)
        self.assertEqual(cls_f1[2], 3)
        self.assertEqual(captured_predictions["mv_dx"][1][2], -3)
        self.assertEqual(captured_predictions["mv_dy"][1][2], 5)
        # Untouched tile 3 is 0
        self.assertEqual(cls_f1[3], 0)

    def test_chain_veto_desired_demotion_contract(self):
        """Desired veto contract demotes rejected baseline FLICKER to CHANGED and preserves GM winner."""
        scene, grid, tiles_struct, base_tiles, gm_out_f1, fl_out_f1 = self._setup_chain_fixture()

        gm_mock = MockVariant("gm=mock_gm", grid, updates=[base_tiles[0], gm_out_f1],
                              claim_masks=[[False] * 4, [False, False, True, False]])
        fl_mock = MockVariant("fl=mock_fl", grid, updates=[base_tiles[0], fl_out_f1],
                              claim_masks=[[False] * 4, [False, True, True, False]])

        veto_frames = []

        class FixedVeto:
            """Supply explicit negative evidence to isolate chain accounting.

            Two static frames cannot establish real detector history. This
            isolates negative-evidence composition; the S1/S2/S3 suite exercises
            positive tracker declarations separately. The real chain must still
            demote the baseline and preserve claims.
            """

            def __init__(self, grid_arg):
                self.current_tiles = None

            def update(self, tiles, rgb, *, frame_id):
                veto_frames.append(frame_id)
                self.current_tiles = tiles.copy()
                return SimpleNamespace(mask=np.array(
                    [frame_id == 1, False, False, False], dtype=np.bool_))

        def mock_variant_factory(spec, grid_arg, budget_ms=1000):
            if "gm=" in spec:
                return gm_mock
            return fl_mock

        def mock_analyze(src, workers=1, cpus=None):
            frame_rows = [{"global_mv": None}, {"global_mv": None}]
            return DummyVisionRun(grid, 2, tiles_struct, frame_rows)

        captured_predictions = {}
        events = derive(scene, grid)

        def mock_metrics_from_predictions(preds, evs, sc):
            for k in ("cls", "mv_dx", "mv_dy"):
                captured_predictions[k] = preds[k].copy()
            return metrics_from_predictions(preds, evs, sc)

        with patch("eval.v2eval.chain.analyze", side_effect=mock_analyze), \
             patch("eval.v2eval.chain._ClaimVariant", side_effect=mock_variant_factory), \
             patch("eval.v2eval.chain.BaselineFlickerVeto", FixedVeto), \
             patch("eval.v2eval.chain.metrics_from_predictions", side_effect=mock_metrics_from_predictions):

            res = run_chain([scene], "gm=mock_gm", "fl=mock_fl", order="gm_then_fl",
                            baseline_flicker_veto=True)

        self.assertEqual(res["variants"]["gm_then_fl"]["status"], "OK")
        self.assertTrue(res["baseline_flicker_veto"]["enabled"])
        self.assertEqual(veto_frames, [0, 1])
        self.assertTrue(gm_mock.closed)
        self.assertTrue(fl_mock.closed)

        cls_f1 = captured_predictions["cls"][1]

        # Desired veto contract under fx2-f6-s2-flicker-veto-and-fl-v3:
        # Tile 0: Rejected baseline FLICKER demoted to CHANGED (class 2)
        # Tile 1: Genuine tracker FLICKER preserved (class 4)
        # Tile 2: GM precedence over FL preserved (class 3)
        # Tile 3: Untouched non-FLICKER preserved (class 0)
        expected_cls_f1 = np.array([2, 4, 3, 0], dtype=np.int64)
        np.testing.assert_array_equal(cls_f1, expected_cls_f1)
        self.assertEqual(captured_predictions["mv_dx"][1][2], -3)
        self.assertEqual(captured_predictions["mv_dy"][1][2], 5)

    # -------------------------------------------------------------------------
    # PART 2: analyze_false_flicker probe accounting across multiple kinds
    # -------------------------------------------------------------------------

    def _setup_probe_scenes_and_events(self, use_structured_arrays=False):
        """Construct synthetic multi-kind scenes and events for probe accounting."""
        grid = Grid(64, 64)
        self.assertEqual(grid.s, 16)
        self.assertEqual(grid.gw, 4)
        self.assertEqual(grid.gh, 4)
        self.assertEqual(grid.N, 16)

        # 4 scenes across 3 kinds:
        # kind_a ("scroll_h"): scene_a1, scene_a2
        # kind_b ("canvas_anim"): scene_b1
        # kind_c ("static_clean"): scene_c1 (all-TP kind)

        scenes = [
            {"scene_id": "DEV:scroll_h:0", "kind": "scroll_h", "w": 64, "h": 64, "n": 1, "split": "DEV"},
            {"scene_id": "DEV:scroll_h:1", "kind": "scroll_h", "w": 64, "h": 64, "n": 1, "split": "DEV"},
            {"scene_id": "DEV:canvas_anim:0", "kind": "canvas_anim", "w": 64, "h": 64, "n": 1, "split": "DEV"},
            {"scene_id": "DEV:static_clean:0", "kind": "static_clean", "w": 64, "h": 64, "n": 1, "split": "DEV"},
        ]

        def make_tile_repr(cls_arr):
            if not use_structured_arrays:
                return {"cls": cls_arr}
            # Structured numpy array format
            dtype = np.dtype([("cls", np.uint8), ("mv_dx", np.int16), ("mv_dy", np.int16)])
            arr = np.zeros(len(cls_arr), dtype=dtype)
            arr["cls"] = cls_arr
            return arr

        # Predictions and events per scene
        data_by_scene = {}

        # scene_a1 (scroll_h):
        # Tile 0: Truth MOVED (ideal=3, mv=[20, 0]) -> moved_large_displacement, Pred FLICKER (4)
        # Tile 1: Truth MOVED (ideal=3, mv=[5, 0]) -> moved_small, Pred FLICKER (4)
        # Tile 2: Truth CHANGED (ideal=2) -> changed_texture, Pred FLICKER (4)
        # Tile 3: Truth UNCHANGED (ideal=0) -> other, Pred FLICKER (4)
        # Tile 4: Truth FLICKER (ideal=4) -> True Positive FLICKER, Pred FLICKER (4)
        # Tile 5: Truth UNCHANGED (ideal=0), Pred UNCHANGED (0) -> non-flicker prediction
        cls_a1 = np.zeros(16, dtype=np.uint8)
        cls_a1[[0, 1, 2, 3, 4]] = 4
        data_by_scene["DEV:scroll_h:0"] = {
            "tiles": [make_tile_repr(cls_a1)],
            "frame_rows": [{"global_mv": None}],
            "events": [
                {"f0": 0, "f1": 1, "ideal": 3, "tiles": [0], "mv": [20, 0]},
                {"f0": 0, "f1": 1, "ideal": 3, "tiles": [1], "mv": [5, 0]},
                {"f0": 0, "f1": 1, "ideal": 2, "tiles": [2]},
                {"f0": 0, "f1": 1, "ideal": 0, "tiles": [3]},
                {"f0": 0, "f1": 1, "ideal": 4, "tiles": [4]},
                {"f0": 0, "f1": 1, "ideal": 0, "tiles": [5]},
            ],
        }

        # scene_a2 (scroll_h):
        # Tile 0: Truth MOVED (ideal=3, mv=[0, -18]) -> moved_large_displacement, Pred FLICKER (4)
        # Tile 1: Truth JITTER (ideal=1) -> other, Pred FLICKER (4)
        # Tile 2: Truth FLICKER (ideal=4) -> True Positive FLICKER, Pred FLICKER (4)
        cls_a2 = np.zeros(16, dtype=np.uint8)
        cls_a2[[0, 1, 2]] = 4
        data_by_scene["DEV:scroll_h:1"] = {
            "tiles": [make_tile_repr(cls_a2)],
            "frame_rows": [{"global_mv": None}],
            "events": [
                {"f0": 0, "f1": 1, "ideal": 3, "tiles": [0], "mv": [0, -18]},
                {"f0": 0, "f1": 1, "ideal": 1, "tiles": [1]},
                {"f0": 0, "f1": 1, "ideal": 4, "tiles": [2]},
            ],
        }

        # scene_b1 (canvas_anim):
        # Tile 0: Truth CHANGED (ideal=2) -> changed_texture, Pred FLICKER (4)
        # Tile 1: Truth FROZEN (ideal=5) -> other, Pred FLICKER (4)
        # Tile 2: Truth FLICKER (ideal=4) -> True Positive FLICKER, Pred FLICKER (4)
        cls_b1 = np.zeros(16, dtype=np.uint8)
        cls_b1[[0, 1, 2]] = 4
        data_by_scene["DEV:canvas_anim:0"] = {
            "tiles": [make_tile_repr(cls_b1)],
            "frame_rows": [{"global_mv": None}],
            "events": [
                {"f0": 0, "f1": 1, "ideal": 2, "tiles": [0]},
                {"f0": 0, "f1": 1, "ideal": 5, "tiles": [1]},
                {"f0": 0, "f1": 1, "ideal": 4, "tiles": [2]},
            ],
        }

        # scene_c1 (static_clean) - All-TP kind:
        # Tile 0: Truth FLICKER (ideal=4) -> True Positive FLICKER, Pred FLICKER (4)
        # Tile 1: Truth FLICKER (ideal=4) -> True Positive FLICKER, Pred FLICKER (4)
        # All other tiles: Pred UNCHANGED (0)
        cls_c1 = np.zeros(16, dtype=np.uint8)
        cls_c1[[0, 1]] = 4
        data_by_scene["DEV:static_clean:0"] = {
            "tiles": [make_tile_repr(cls_c1)],
            "frame_rows": [{"global_mv": None}],
            "events": [
                {"f0": 0, "f1": 1, "ideal": 4, "tiles": [0]},
                {"f0": 0, "f1": 1, "ideal": 4, "tiles": [1]},
            ],
        }

        def mock_analyze(src, workers=1, cpus=None):
            sc_id = src.scene["scene_id"]
            sc_data = data_by_scene[sc_id]
            return DummyVisionRun(grid, src.scene["n"], sc_data["tiles"], sc_data["frame_rows"])

        def mock_derive(scene, grid_arg):
            return data_by_scene[scene["scene_id"]]["events"]

        return scenes, mock_analyze, mock_derive

    def test_probe_accounting_invariants_and_format_equivalence(self):
        """Test probe per-kind and summary accounting invariants with class dicts vs structured arrays."""
        for use_struct in (False, True):
            scenes, mock_analyze, mock_derive = self._setup_probe_scenes_and_events(use_struct)

            with patch("eval.v2eval.false_flicker_probe.derive", side_effect=mock_derive):
                res = analyze_false_flicker(scenes, analyze_fn=mock_analyze)

            self.assertEqual(res["schema"], "vqa.false_flicker_probe/v1")
            self.assertEqual(res["split"], "DEV")

            kinds = res["kinds"]
            summary = res["summary"]

            # Per-kind total equals its record count, bucket sum, and truth-class sum
            for kind_name, k_data in kinds.items():
                tot = k_data["total_false_flicker_tile_frames"]
                self.assertEqual(tot, len(k_data["records"]), f"Kind {kind_name} record count mismatch")
                self.assertEqual(tot, sum(k_data["counts_by_cause_bucket"].values()), f"Kind {kind_name} bucket sum mismatch")
                self.assertEqual(tot, sum(k_data["counts_by_truth_class"].values()), f"Kind {kind_name} truth sum mismatch")

            # Summary equals exact sum of kinds
            for tc in NAMES:
                expected_tc_sum = sum(k["counts_by_truth_class"][tc] for k in kinds.values())
                self.assertEqual(summary["counts_by_truth_class"][tc], expected_tc_sum)

            for bucket in CAUSE_BUCKETS:
                expected_b_sum = sum(k["counts_by_cause_bucket"][bucket] for k in kinds.values())
                self.assertEqual(summary["counts_by_cause_bucket"][bucket], expected_b_sum)

            expected_tot_sum = sum(k["total_false_flicker_tile_frames"] for k in kinds.values())
            self.assertEqual(summary["total_false_flicker_tile_frames"], expected_tot_sum)

            # Check all-TP kind static_clean is present and has initialized counters
            self.assertIn("static_clean", kinds)
            c_kind = kinds["static_clean"]
            self.assertIsInstance(c_kind["records"], list)
            self.assertIsInstance(c_kind["counts_by_truth_class"], dict)
            self.assertIsInstance(c_kind["counts_by_cause_bucket"], dict)

    def test_probe_accounting_reordering_determinism(self):
        """Check scene list reordering yields identical counters and record multisets."""
        scenes, mock_analyze, mock_derive = self._setup_probe_scenes_and_events(False)
        reordered_scenes = [scenes[3], scenes[2], scenes[1], scenes[0]]

        with patch("eval.v2eval.false_flicker_probe.derive", side_effect=mock_derive):
            res1 = analyze_false_flicker(scenes, analyze_fn=mock_analyze)
            res2 = analyze_false_flicker(reordered_scenes, analyze_fn=mock_analyze)

        # Summary match
        self.assertEqual(res1["summary"], res2["summary"])

        # Kinds match
        self.assertEqual(set(res1["kinds"].keys()), set(res2["kinds"].keys()))
        for kind in res1["kinds"]:
            k1 = res1["kinds"][kind]
            k2 = res2["kinds"][kind]
            self.assertEqual(k1["counts_by_truth_class"], k2["counts_by_truth_class"])
            self.assertEqual(k1["counts_by_cause_bucket"], k2["counts_by_cause_bucket"])
            self.assertEqual(k1["total_false_flicker_tile_frames"], k2["total_false_flicker_tile_frames"])

            # Multiset of records comparison
            rec1_tuples = sorted([(r["scene_id"], r["frame"], r["tile"], r["truth_class"], r["cause_bucket"]) for r in k1["records"]])
            rec2_tuples = sorted([(r["scene_id"], r["frame"], r["tile"], r["truth_class"], r["cause_bucket"]) for r in k2["records"]])
            self.assertEqual(rec1_tuples, rec2_tuples)

    def test_probe_tp_exclusion_per_kind_records_and_truth_class(self):
        """True-positive FLICKER predictions must be excluded from records and truth-class counts per kind."""
        scenes, mock_analyze, mock_derive = self._setup_probe_scenes_and_events(False)

        with patch("eval.v2eval.false_flicker_probe.derive", side_effect=mock_derive):
            res = analyze_false_flicker(scenes, analyze_fn=mock_analyze)

        # Desired probe contract under fx2-f6-s2-flicker-veto-and-fl-v3:
        # Excluded true positives must appear in neither records nor truth-class counts
        for kind_name, k_data in res["kinds"].items():
            self.assertEqual(k_data["counts_by_truth_class"]["FLICKER"], 0,
                             f"Kind {kind_name} includes true-positive FLICKER in counts_by_truth_class")
            for rec in k_data["records"]:
                self.assertNotEqual(rec["truth_class"], "FLICKER",
                                    f"Kind {kind_name} includes true-positive FLICKER in records")

        # For all-TP kind static_clean: initialized counters must be explicitly zero / empty list
        c_data = res["kinds"]["static_clean"]
        self.assertEqual(c_data["total_false_flicker_tile_frames"], 0)
        self.assertEqual(c_data["records"], [])

    def test_probe_tp_exclusion_cause_bucket_other_accounting(self):
        """True-positive FLICKER predictions must not be rebucketed into cause_bucket 'other'."""
        scenes, mock_analyze, mock_derive = self._setup_probe_scenes_and_events(False)

        with patch("eval.v2eval.false_flicker_probe.derive", side_effect=mock_derive):
            res = analyze_false_flicker(scenes, analyze_fn=mock_analyze)

        # Desired probe contract:
        # For kind_a (scroll_h):
        # Non-FLICKER 'other' FPs: scene_a1 tile 3 (UNCHANGED) + scene_a2 tile 1 (JITTER) = 2.
        # True-positive FLICKER tiles (scene_a1 tile 4, scene_a2 tile 2) must NOT be counted in 'other'.
        # Including the two true positives would incorrectly give four.
        self.assertEqual(res["kinds"]["scroll_h"]["counts_by_cause_bucket"]["other"], 2)

    def test_probe_tp_exclusion_summary_exact_fp_totals(self):
        """Summary total_false_flicker_tile_frames must equal exact sum of false positives excluding true positives."""
        scenes, mock_analyze, mock_derive = self._setup_probe_scenes_and_events(False)

        with patch("eval.v2eval.false_flicker_probe.derive", side_effect=mock_derive):
            res = analyze_false_flicker(scenes, analyze_fn=mock_analyze)

        # Desired summary contract:
        # Total FPs across scenes:
        # scroll_h: 2 moved_large + 1 moved_small + 1 changed + 2 other = 6
        # canvas_anim: 1 changed + 1 other = 2
        # static_clean: 0
        # Grand total is 8; including the five true positives would give 13.
        self.assertEqual(res["summary"]["total_false_flicker_tile_frames"], 8)


if __name__ == "__main__":
    unittest.main()
