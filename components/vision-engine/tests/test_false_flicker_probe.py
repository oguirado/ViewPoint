"""Unit tests for eval.v2eval.false_flicker_probe."""
import json
import unittest
import numpy as np

from eval.gui_synth.scenes import build
from eval.v2eval.baseline import load_dev, selection
from eval.v2eval.exploration import quick_selection
from eval.v2eval.false_flicker_probe import analyze_false_flicker, main
from visioncore.grid import Grid


class DummyVisionRun:
    """Mock VisionRun to return hand-built tile classifications and frame rows."""

    def __init__(self, grid, n_frames, tiles, frame_rows):
        self.grid = grid
        self.tiles = tiles
        self.frame_rows = frame_rows


class TestFalseFlickerProbe(unittest.TestCase):

    def test_bucket_logic_and_determinism(self):
        """Test bucket classification logic using a monkeypatched analyze_fn."""
        dev = load_dev()
        all_scenes = selection(dev)

        # Select 2 small scenes of different kinds using quick_selection
        scroll_scenes = [s for s in all_scenes if s["kind"] == "scroll_small"]
        canvas_scenes = [s for s in all_scenes if s["kind"] == "canvas_anim"]
        selected_scenes = quick_selection([scroll_scenes[0], canvas_scenes[0]], 2)

        def mock_analyze(src, workers=1, cpus=None):
            scene = src.scene
            grid = Grid(scene["w"], scene["h"])
            n_frames = scene["n"]
            n_tiles = grid.N

            tiles = []
            frame_rows = []

            for f in range(n_frames):
                # Class 0 by default
                f_cls = np.zeros(n_tiles, dtype=np.uint8)

                if scene["kind"] == "scroll_small" and f == 1:
                    # Set tile 0 and tile 1 to predicted FLICKER (class 4)
                    f_cls[0] = 4
                    f_cls[1] = 4

                tiles.append({"cls": f_cls})
                # Simulate published global MV on frame 1
                global_mv = [4, 2] if f == 1 else None
                frame_rows.append({"global_mv": global_mv})

            return DummyVisionRun(grid, n_frames, tiles, frame_rows)

        result = analyze_false_flicker(selected_scenes, analyze_fn=mock_analyze)

        # Verify schema
        self.assertEqual(result["schema"], "vqa.false_flicker_probe/v1")
        self.assertEqual(result["split"], "DEV")
        self.assertEqual(result["measurement_tag"], "VM")

        # Verify JSON determinism (keys sorted)
        json_str1 = json.dumps(result, sort_keys=True, indent=2)
        json_str2 = json.dumps(result, sort_keys=True, indent=2)
        self.assertEqual(json_str1, json_str2)

        # Verify scroll_small results
        scroll_kind = selected_scenes[0]["kind"] if selected_scenes[0]["kind"] == "scroll_small" else selected_scenes[1]["kind"]
        scroll_res = result["kinds"][scroll_kind]
        self.assertEqual(scroll_res["total_false_flicker_tile_frames"], 2)

        # Verify record details for tile 0 and tile 1
        records = scroll_res["records"]
        self.assertEqual(len(records), 2)
        r0 = records[0]
        self.assertEqual(r0["frame"], 1)
        self.assertEqual(r0["tile"], 0)
        self.assertTrue(r0["global_vector_published"])
        self.assertTrue(r0["is_edge"])  # tile 0 is top-left edge
        self.assertIn(r0["cause_bucket"], ("moved_large_displacement", "moved_small", "changed_texture", "other"))

    def test_smoke_real_pipeline_schema(self):
        """Smoke test running the real pipeline on the smallest DEV scene."""
        dev = load_dev()
        scenes = selection(dev)
        # Select smallest scene
        tiny_scene = quick_selection(scenes, 1)[0]

        result = analyze_false_flicker([tiny_scene])

        # Validate schema
        self.assertEqual(result["schema"], "vqa.false_flicker_probe/v1")
        self.assertEqual(result["split"], "DEV")
        self.assertEqual(result["measurement_tag"], "VM")
        self.assertIn("kinds", result)
        self.assertIn("summary", result)

        kind = tiny_scene["kind"]
        self.assertIn(kind, result["kinds"])
        kind_res = result["kinds"][kind]

        self.assertIn("counts_by_truth_class", kind_res)
        self.assertIn("counts_by_cause_bucket", kind_res)
        self.assertIn("records", kind_res)
        self.assertIn("total_false_flicker_tile_frames", kind_res)

        summary = result["summary"]
        self.assertIn("counts_by_truth_class", summary)
        self.assertIn("counts_by_cause_bucket", summary)
        self.assertIn("total_false_flicker_tile_frames", summary)


if __name__ == "__main__":
    unittest.main()
