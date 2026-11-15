"""Exact false-positive attribution, boundaries, and true-FLICKER exclusion."""
import unittest
from unittest.mock import patch
import numpy as np

from eval.v2eval.false_flicker_probe import analyze_false_flicker, CAUSE_BUCKETS
from visioncore.grid import Grid


class DummyVisionRun:
    """Mock VisionRun to return hand-built tile classifications and frame rows."""

    def __init__(self, grid, n_frames, tiles, frame_rows):
        self.grid = grid
        self.tiles = tiles
        self.frame_rows = frame_rows


class TestFalseFlickerProbeBuckets(unittest.TestCase):

    def setUp(self):
        self.grid = Grid(64, 64)
        self.assertEqual(self.grid.s, 16)
        self.assertEqual(self.grid.gw, 4)
        self.assertEqual(self.grid.gh, 4)
        self.assertEqual(self.grid.N, 16)

    def test_moved_displacement_bucket_boundaries_and_edge_flags(self):
        """Test displacement threshold s=16 boundary and edge flag classification."""
        # 64x64 grid with tile size s=16 has gw=4, gh=4.
        # Tile grid positions in Morton order for 4x4 grid:
        # tx=1, ty=0 -> tile 1 (first row, edge)
        # tx=1, ty=3 -> tile 11 (last row, edge)
        # tx=0, ty=1 -> tile 2 (first col, edge)
        # tx=3, ty=1 -> tile 7 (last col, edge)
        # tx=1, ty=1 -> tile 3 (interior)
        # tx=2, ty=1 -> tile 6 (interior)
        # tx=1, ty=2 -> tile 9 (interior)
        # tx=2, ty=2 -> tile 12 (interior)

        test_cases = [
            # (tile, dx, dy, expected_bucket, expected_edge, description)
            # max abs comp = 15 (< 16 -> moved_small)
            (3, 15, 0, "moved_small", False, "Interior tile max comp +15"),
            (6, 0, -15, "moved_small", False, "Interior tile max comp -15"),
            (9, -15, 15, "moved_small", False, "Interior tile diagonal max comp 15"),
            (12, 10, -15, "moved_small", False, "Interior tile diagonal max comp 15"),
            # max abs comp = 16 (>= 16 -> moved_large_displacement)
            (1, 16, 0, "moved_large_displacement", True, "First row edge tile max comp +16"),
            (11, -16, 5, "moved_large_displacement", True, "Last row edge tile max comp -16"),
            (2, 16, -16, "moved_large_displacement", True, "First col edge tile diagonal max comp 16"),
            (7, -10, -16, "moved_large_displacement", True, "Last col edge tile max comp -16"),
            # max abs comp = 17 (>= 16 -> moved_large_displacement)
            (3, 0, 17, "moved_large_displacement", False, "Interior tile max comp +17"),
            (6, -17, -17, "moved_large_displacement", False, "Interior tile diagonal max comp 17"),
            (9, 17, -10, "moved_large_displacement", False, "Interior tile max comp +17"),
            (12, -14, 17, "moved_large_displacement", False, "Interior tile max comp +17"),
        ]

        n_frames = len(test_cases)
        scene = {
            "scene_id": "DEV:scroll_small:0:test_bucket_grid",
            "kind": "scroll_small",
            "w": 64,
            "h": 64,
            "n": n_frames,
            "split": "DEV",
        }

        events = []
        tiles_list = []
        frame_rows = []

        for f, (tile, dx, dy, _, _, _) in enumerate(test_cases):
            cls_array = np.zeros(16, dtype=np.uint8)
            cls_array[tile] = 4  # Predicted FLICKER
            tiles_list.append({"cls": cls_array})
            frame_rows.append({"global_mv": None})

            events.append({
                "f0": f,
                "f1": f + 1,
                "ideal": 3,  # MOVED
                "tiles": [tile],
                "mv": [dx, dy],
            })

        def mock_analyze(src, workers=1, cpus=None):
            return DummyVisionRun(self.grid, n_frames, tiles_list, frame_rows)

        with patch("eval.v2eval.false_flicker_probe.derive", return_value=events):
            res = analyze_false_flicker([scene], analyze_fn=mock_analyze)

        kind_data = res["kinds"]["scroll_small"]
        self.assertEqual(kind_data["total_false_flicker_tile_frames"], len(test_cases))

        # Count expected buckets
        expected_counts = {bucket: 0 for bucket in CAUSE_BUCKETS}
        for _, _, _, expected_bucket, _, _ in test_cases:
            expected_counts[expected_bucket] += 1

        # Pin exact cause bucket counts
        self.assertEqual(kind_data["counts_by_cause_bucket"], expected_counts)
        self.assertEqual(res["summary"]["counts_by_cause_bucket"], expected_counts)

        # Verify each record
        records = kind_data["records"]
        self.assertEqual(len(records), len(test_cases))
        for f, (tile, dx, dy, expected_bucket, expected_edge, desc) in enumerate(test_cases):
            rec = records[f]
            self.assertEqual(rec["frame"], f)
            self.assertEqual(rec["tile"], tile)
            self.assertEqual(rec["cause_bucket"], expected_bucket, f"Failed bucket for {desc}")
            self.assertEqual(rec["is_edge"], expected_edge, f"Failed edge flag for {desc}")
            self.assertEqual(rec["truth_class"], "MOVED")
            self.assertEqual(rec["truth_mv"], [dx, dy])

    def test_truth_class_cause_bucket_attributions(self):
        """Test attribution of truth classes to cause buckets under attribution contract."""
        # Truth class mapping under analyze_false_flicker contract:
        # MOVED -> moved_small or moved_large_displacement
        # CHANGED -> changed_texture
        # UNCHANGED, JITTER, FROZEN, DRIFT -> other
        # FLICKER is a true positive, excluded from records and all aggregates.

        class_cases = [
            (0, "UNCHANGED", "other", 0),
            (1, "JITTER", "other", 1),
            (2, "CHANGED", "changed_texture", 2),
            (4, "FLICKER", "other", 4),
            (5, "FROZEN", "other", 5),
            (6, "DRIFT", "other", 6),
        ]

        n_frames = len(class_cases)
        scene = {
            "scene_id": "DEV:scroll_small:0:test_class_grid",
            "kind": "scroll_small",
            "w": 64,
            "h": 64,
            "n": n_frames,
            "split": "DEV",
        }

        events = []
        tiles_list = []
        frame_rows = []

        for f, (ideal_cls, name, expected_bucket, tile) in enumerate(class_cases):
            cls_array = np.zeros(16, dtype=np.uint8)
            cls_array[tile] = 4  # Predicted FLICKER
            tiles_list.append({"cls": cls_array})
            frame_rows.append({"global_mv": None})

            events.append({
                "f0": f,
                "f1": f + 1,
                "ideal": ideal_cls,
                "tiles": [tile],
            })

        def mock_analyze(src, workers=1, cpus=None):
            return DummyVisionRun(self.grid, n_frames, tiles_list, frame_rows)

        with patch("eval.v2eval.false_flicker_probe.derive", return_value=events):
            res = analyze_false_flicker([scene], analyze_fn=mock_analyze)

        kind_data = res["kinds"]["scroll_small"]
        false_cases = [(f, case) for f, case in enumerate(class_cases) if case[0] != 4]
        self.assertEqual(kind_data["total_false_flicker_tile_frames"], len(false_cases))

        # Pin exact cause bucket counts
        expected_counts = {
            "moved_large_displacement": 0,
            "moved_small": 0,
            "changed_texture": 1,
            "other": 4,
        }
        self.assertEqual(kind_data["counts_by_cause_bucket"], expected_counts)

        # Pin exact truth class counts
        expected_truth_counts = {
            "UNCHANGED": 1,
            "JITTER": 1,
            "CHANGED": 1,
            "MOVED": 0,
            "FLICKER": 0,
            "FROZEN": 1,
            "DRIFT": 1,
        }
        self.assertEqual(kind_data["counts_by_truth_class"], expected_truth_counts)
        self.assertEqual(res["summary"]["counts_by_truth_class"], expected_truth_counts)
        self.assertEqual(res["summary"]["counts_by_cause_bucket"], expected_counts)
        self.assertEqual(res["summary"]["total_false_flicker_tile_frames"], len(false_cases))

        # Verify individual record attributions
        records = kind_data["records"]
        self.assertEqual(len(records), len(false_cases))
        for rec, (f, (ideal_cls, name, expected_bucket, tile)) in zip(records, false_cases):
            self.assertEqual(rec["frame"], f)
            self.assertEqual(rec["tile"], tile)
            self.assertEqual(rec["truth_class"], name)
            self.assertEqual(rec["cause_bucket"], expected_bucket)

    def test_true_positive_flicker_overcount_defect(self):
        """Regression for false_flicker_probe true-positive overcount defect.

        false_flicker_probe true-positive overcount defect: analyze_false_flicker counts all
        predicted FLICKER tile-frames including true positives (truth FLICKER) as false flicker
        tile-frames. When given one true FLICKER prediction (truth FLICKER) and one false FLICKER
        prediction (truth CHANGED), the correct total_false_flicker_tile_frames count is 1
        (excluding the true positive), in every aggregate as well as the records.
        """
        scene = {
            "scene_id": "DEV:scroll_small:0:test_overcount_grid",
            "kind": "scroll_small",
            "w": 64,
            "h": 64,
            "n": 1,
            "split": "DEV",
        }

        # Tile 3: Truth FLICKER (ideal 4), predicted FLICKER (class 4) -> True Positive
        # Tile 6: Truth CHANGED (ideal 2), predicted FLICKER (class 4) -> False Positive
        events = [
            {"f0": 0, "f1": 1, "ideal": 4, "tiles": [3]},  # Truth FLICKER
            {"f0": 0, "f1": 1, "ideal": 2, "tiles": [6]},  # Truth CHANGED
        ]

        cls_array = np.zeros(16, dtype=np.uint8)
        cls_array[3] = 4  # Predicted FLICKER
        cls_array[6] = 4  # Predicted FLICKER

        def mock_analyze(src, workers=1, cpus=None):
            tiles = [{"cls": cls_array}]
            frame_rows = [{"global_mv": None}]
            return DummyVisionRun(self.grid, 1, tiles, frame_rows)

        with patch("eval.v2eval.false_flicker_probe.derive", return_value=events):
            res = analyze_false_flicker([scene], analyze_fn=mock_analyze)

        # Correct count excluding true positive (truth FLICKER) should be 1.
        self.assertEqual(res["summary"]["total_false_flicker_tile_frames"], 1)
        kind_data = res["kinds"]["scroll_small"]
        expected_truth = {name: int(name == "CHANGED")
                          for name in res["summary"]["counts_by_truth_class"]}
        expected_causes = {bucket: int(bucket == "changed_texture")
                           for bucket in CAUSE_BUCKETS}
        for aggregate in (res["summary"], kind_data):
            self.assertEqual(aggregate["total_false_flicker_tile_frames"], 1)
            self.assertEqual(aggregate["counts_by_truth_class"], expected_truth)
            self.assertEqual(aggregate["counts_by_cause_bucket"], expected_causes)
        self.assertEqual(len(kind_data["records"]), 1)
        self.assertEqual(kind_data["records"][0]["tile"], 6)
        self.assertEqual(kind_data["records"][0]["truth_class"], "CHANGED")
        self.assertEqual(kind_data["records"][0]["cause_bucket"], "changed_texture")


if __name__ == "__main__":
    unittest.main()
