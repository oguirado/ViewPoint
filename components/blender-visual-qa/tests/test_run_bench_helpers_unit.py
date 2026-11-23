"""Unit tests for pure helper functions in blender_visual_qa.truth.run_bench."""

import tempfile
import unittest
from pathlib import Path

import numpy as np

from blender_visual_qa.truth import run_bench as rb
from blender_visual_qa.truth import score


class TestRunBenchHelpersUnit(unittest.TestCase):
    def test_chunk_ranges_properties(self):
        """Property over nframes 1 to 64 and several chunk sizes, checking ranges are contiguous,
        inclusive, cover 1 to nframes and never exceed the size."""
        chunk_sizes = [1, 4, 8, 10, 64]
        for nframes in range(1, 65):
            for size in chunk_sizes:
                ranges = rb.chunk_ranges(nframes, size=size)
                self.assertTrue(len(ranges) > 0, f"nframes={nframes}, size={size}")

                prev_hi = 0
                total_frames = 0
                for lo, hi in ranges:
                    self.assertEqual(lo, prev_hi + 1, f"Not contiguous at lo={lo}, prev_hi={prev_hi}")
                    self.assertLessEqual(lo, hi, f"Invalid range ({lo}, {hi})")
                    self.assertLessEqual(hi - lo + 1, size, f"Chunk size exceeded in ({lo}, {hi}) for size {size}")
                    self.assertLessEqual(hi, nframes, f"hi={hi} exceeded nframes={nframes}")
                    total_frames += (hi - lo + 1)
                    prev_hi = hi

                self.assertEqual(prev_hi, nframes, f"Did not cover up to nframes={nframes}")
                self.assertEqual(total_frames, nframes, f"Total frames mismatch for nframes={nframes}")

    def test_parse_spp(self):
        """parse_spp for valid tiers, duplicates, unknown tiers, and non-integer text."""
        # Valid single tier
        self.assertEqual(rb.parse_spp("4"), [4])
        # Valid multiple tiers
        self.assertEqual(rb.parse_spp("4,16,64"), [4, 16, 64])

        # Duplicates
        with self.assertRaises(rb.BenchError) as ctx:
            rb.parse_spp("4,4")
        self.assertEqual(ctx.exception.code, "E_BUDGET")

        # Unknown tier
        with self.assertRaises(rb.BenchError) as ctx:
            rb.parse_spp("8")
        self.assertEqual(ctx.exception.code, "E_BUDGET")

        # Non-integer text
        with self.assertRaises(rb.BenchError) as ctx:
            rb.parse_spp("four")
        self.assertEqual(ctx.exception.code, "E_USAGE")
        self.assertEqual(ctx.exception.rc, 2)

    def test_seed_expression(self):
        """seed_expression for both seed modes checking emitted Python text without running it."""
        expr_fixed = rb.seed_expression("B1", 5, "fixed", 4)
        self.assertIn("import bpy,sys;", expr_fixed)
        self.assertIn("cycles_seed(1,5,0)", expr_fixed)
        self.assertIn("s.cycles.samples=4", expr_fixed)
        self.assertNotIn("render_pre", expr_fixed)

        expr_per_frame = rb.seed_expression("B1", 5, "per_frame", 16)
        self.assertIn("import bpy,sys;", expr_per_frame)
        self.assertIn("cycles_seed(1,5,0)", expr_per_frame)
        self.assertIn("s.cycles.samples=16", expr_per_frame)
        self.assertIn("bpy.app.handlers.render_pre.append", expr_per_frame)
        self.assertIn("cycles_seed(1,5,sc.frame_current)", expr_per_frame)

    def test_scene_slot_injectivity(self):
        """scene_slot checking injectivity of k modulo 169 for every bench."""
        for bench in rb.BENCHES:
            slots = set()
            for k_mod in range(169):
                slot = rb.scene_slot(bench, k_mod)
                self.assertNotIn(slot, slots, f"Collision in bench {bench} for k_mod={k_mod}")
                slots.add(slot)
            self.assertEqual(len(slots), 169, f"Bench {bench} did not generate 169 unique slots")

    def test_confusion_and_labels_tl_count(self):
        """confusion and labels_tl_count on small synthetic arrays with excluded labels."""
        self.assertEqual(rb.labels_tl_count(), score.TL_MAX + 1)

        # Create synthetic arrays with included and excluded labels
        tl = np.array([0, 1, 2, 3, 8, 11], dtype=np.int64)
        cls_pred = np.array([0, 1, 2, 3, 4, 6], dtype=np.int64)

        conf = rb.confusion(tl, cls_pred)
        self.assertEqual(conf.shape, (rb.labels_tl_count(), 7))

        # Check that excluded labels (1, 2, 8 in score.EXCLUDED) have all zero counts
        for ex in score.EXCLUDED:
            self.assertTrue(np.all(conf[ex] == 0), f"Excluded label {ex} has non-zero counts")

        # Check non-excluded entries
        self.assertEqual(conf[0, 0], 1)
        self.assertEqual(conf[3, 3], 1)
        self.assertEqual(conf[11, 6], 1)

    def test_admit_unit_and_unit_peak_no_ramws(self):
        """admit_unit and unit_peak on the path where no RAM workspace is active."""
        original_ws = rb._Store.ws
        rb._Store.ws = None
        try:
            # admit_unit should safely return None without error
            res = rb.admit_unit(320, 180, 8)
            self.assertIsNone(res)

            # unit_peak on nonexistent path
            peak_nonexistent = rb.unit_peak(Path("/nonexistent/path/for/unit_peak"))
            self.assertEqual(peak_nonexistent, 0)

            # unit_peak on an existing temp directory
            with tempfile.TemporaryDirectory() as tmpdir:
                peak_tmp = rb.unit_peak(Path(tmpdir))
                self.assertIsInstance(peak_tmp, int)
                self.assertGreaterEqual(peak_tmp, 0)
        finally:
            rb._Store.ws = original_ws


if __name__ == "__main__":
    unittest.main()
