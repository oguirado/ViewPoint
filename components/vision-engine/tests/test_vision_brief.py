"""Pinning tests for eval/vision_brief.py vqa.vision-brief/v1 encoder."""

import json
import random
import sys
import unittest
from pathlib import Path

import numpy as np

from eval import vision_brief
from visioncore import layout, regions as R
from visioncore.change import TILE_DT


def make_frame_row(
    frame=0,
    grid="grid:1280x720:s16",
    cov_ppm=1000,
    global_mv=None,
    regions_cnt=1,
):
    return {
        "schema": "vqa.core-frame/v1",
        "frame": frame,
        "grid": grid,
        "n": [0, 0, 1, 0, 0, 0, 0],
        "cov_ppm": cov_ppm,
        "global_mv": global_mv,
        "regions": regions_cnt,
        "tiles_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    }


def make_region_row(
    frame=0,
    reg_id="r0:000a",
    grid="grid:1280x720:s16",
    cls="CHANGED",
    n_tiles=4,
    bbox_px=None,
    mv=None,
    kind="SMOOTH",
):
    if bbox_px is None:
        bbox_px = [16, 16, 32, 32]
    if mv is None:
        mv = [0, 0]
    return {
        "schema": "vqa.core-region/v1",
        "id": reg_id,
        "frame": frame,
        "grid": grid,
        "cls": cls,
        "n_tiles": n_tiles,
        "bbox_t": [1, 1, 2, 2],
        "bbox_px": bbox_px,
        "mv": mv,
        "r_max": 10,
        "r_sum": 40,
        "edge_ppm": 1000,
        "kind": kind,
        "shade_only": False,
        "spans": [["0001", "0004"]],
    }


class TestDeterminism(unittest.TestCase):
    def test_byte_equal_across_calls_and_shuffled_input(self):
        frames = [make_frame_row(f, cov_ppm=1000 + f * 100) for f in range(5)]
        regions = []
        for f in range(5):
            for r_idx in range(4):
                anchor = "%04x" % (r_idx + 1)
                regions.append(
                    make_region_row(
                        frame=f,
                        reg_id=f"r{f}:{anchor}",
                        n_tiles=(r_idx + 1) * 2,
                        bbox_px=[r_idx * 40, f * 10, 32, 32],
                    )
                )

        b1 = vision_brief.encode(frames, regions)
        c1 = vision_brief.canonical(b1)

        b2 = vision_brief.encode(frames, regions)
        c2 = vision_brief.canonical(b2)
        self.assertEqual(c1, c2)

        # Shuffle input order
        rnd = random.Random(1337)
        shuffled_frames = list(frames)
        shuffled_regions = list(regions)
        rnd.shuffle(shuffled_frames)
        rnd.shuffle(shuffled_regions)

        b3 = vision_brief.encode(shuffled_frames, shuffled_regions)
        c3 = vision_brief.canonical(b3)
        self.assertEqual(c1, c3)


class TestSizeProxyBudget(unittest.TestCase):
    def test_budget_never_exceeded(self):
        for num_regions in (1, 10, 100, 1000):
            for budget in (200, 600, 2000):
                frames = [make_frame_row(0)]
                regions = []
                for idx in range(num_regions):
                    anchor = "%04x" % (idx + 1)
                    regions.append(
                        make_region_row(
                            frame=0,
                            reg_id=f"r0:{anchor}",
                            n_tiles=(idx % 50) + 1,
                            bbox_px=[(idx % 10) * 32, ((idx // 10) % 10) * 32, 16, 16],
                        )
                    )

                brief = vision_brief.encode(
                    frames, regions, max_regions=1000, budget_tokens=budget
                )
                sp = vision_brief.size_proxy(brief)
                self.assertLessEqual(
                    sp,
                    budget,
                    f"Size proxy {sp} exceeded budget {budget} for num_regions={num_regions}",
                )


class TestTruncation(unittest.TestCase):
    def test_drops_lowest_ranked_regions_first_then_crops(self):
        frames = [make_frame_row(0)]
        regions = []
        # Create 10 regions with increasing n_tiles
        for i in range(1, 11):
            anchor = "%04x" % i
            regions.append(
                make_region_row(
                    frame=0,
                    reg_id=f"r0:{anchor}",
                    n_tiles=i,  # Rank 10 has highest n_tiles, Rank 1 has lowest
                    bbox_px=[i * 40, 10, 32, 32],
                )
            )

        # Restrict max_regions to 5
        brief = vision_brief.encode(
            frames, regions, max_regions=5, max_crops=5, budget_tokens=2000
        )
        kept_ids = [r["id"] for r in brief["regions"]]
        expected_ids = ["r0:%04x" % i for i in range(10, 5, -1)]
        self.assertEqual(kept_ids, expected_ids)
        self.assertEqual(brief["truncated"], 5)  # 5 regions dropped

        # Force tight budget that drops remaining regions
        brief_tight = vision_brief.encode(
            frames, regions, max_regions=5, max_crops=5, budget_tokens=60
        )
        self.assertLess(len(brief_tight["regions"]), 5)
        self.assertGreater(brief_tight["truncated"], 5)


class TestCropBoxes(unittest.TestCase):
    def test_closed_padded_clamped_merged(self):
        grid = "grid:100x100:s16"
        frames = [make_frame_row(0, grid=grid)]
        regions = [
            # Region 1: [10, 10, 20, 20] -> closed [10, 10, 29, 29] -> pad 8 -> [2, 2, 37, 37]
            make_region_row(
                0,
                reg_id="r0:0001",
                grid=grid,
                n_tiles=10,
                bbox_px=[10, 10, 20, 20],
            ),
            # Region 2: [30, 30, 20, 20] -> closed [30, 30, 49, 49] -> pad 8 -> [22, 22, 57, 57]
            # Overlaps Region 1! Merged -> [2, 2, 57, 57]
            make_region_row(
                0,
                reg_id="r0:0002",
                grid=grid,
                n_tiles=9,
                bbox_px=[30, 30, 20, 20],
            ),
            # Region 3 near top-left edge: [0, 0, 5, 5] -> closed [0, 0, 4, 4] -> pad 8 -> [-8, -8, 12, 12]
            # Clamped to [0, 0, 99, 99] -> [0, 0, 12, 12]
            make_region_row(
                0,
                reg_id="r0:0003",
                grid=grid,
                n_tiles=8,
                bbox_px=[0, 0, 5, 5],
            ),
        ]

        brief = vision_brief.encode(
            frames, regions, max_regions=5, max_crops=5, pad_px=8
        )
        crops = brief["crops"]
        # Overlapping Region 1 and Region 2 crop box [2, 2, 37, 37] and [22, 22, 57, 57]
        # and Region 3 crop box [0, 0, 12, 12] (which also overlaps [2, 2, 57, 57]!)
        # So all three merge into a single bounding box [0, 0, 57, 57]!
        self.assertEqual(crops, [[0, 0, 57, 57]])


class TestFlickerMerging(unittest.TestCase):
    def test_flicker_toggling_across_6_frames(self):
        # Frame window 0..5 (6 frames)
        frames = [make_frame_row(f) for f in range(6)]
        regions = []
        # Region '000a' present on frames 0, 2, 4 (toggling ON/OFF across 6 frames)
        for f in (0, 2, 4):
            regions.append(
                make_region_row(
                    frame=f,
                    reg_id=f"r{f}:000a",
                    cls="FLICKER",
                    n_tiles=4,
                )
            )

        brief = vision_brief.encode(frames, regions)
        self.assertEqual(brief["frame_range"], [0, 5])
        self.assertEqual(len(brief["regions"]), 1)
        r = brief["regions"][0]
        self.assertEqual(r["cls"], "FLICKER")
        self.assertEqual(r["first_frame"], 0)
        self.assertEqual(r["last_frame"], 4)
        self.assertEqual(r["toggles"], 5)


class TestUnchangedFrames(unittest.TestCase):
    def test_unchanged_frames_yield_empty_region_list(self):
        frames = [make_frame_row(f, cov_ppm=0) for f in range(4)]
        brief = vision_brief.encode(frames, [])
        self.assertEqual(brief["regions"], [])
        self.assertEqual(brief["crops"], [])
        self.assertEqual(brief["truncated"], 0)


class TestMalformedRows(unittest.TestCase):
    def test_missing_frame_key_raises_value_error(self):
        frame_bad = make_frame_row(0)
        del frame_bad["cov_ppm"]
        with self.assertRaises(ValueError) as ctx:
            vision_brief.encode([frame_bad], [])
        self.assertIn("cov_ppm", str(ctx.exception))

    def test_missing_region_key_raises_value_error(self):
        frame = make_frame_row(0)
        reg_bad = make_region_row(0)
        del reg_bad["bbox_px"]
        with self.assertRaises(ValueError) as ctx:
            vision_brief.encode([frame], [reg_bad])
        self.assertIn("bbox_px", str(ctx.exception))


class TestVisionCoreIntegration(unittest.TestCase):
    def test_encode_from_regions_of_synthetic_tiles(self):
        W, H = 1280, 720
        # Build synthetic tile array in RAM
        items = [
            (layout.morton(0, 0), {"cls": 2, "flags": 0, "resid": 20, "dx": 0, "dy": 0, "edge": 10}),
            (layout.morton(1, 0), {"cls": 2, "flags": 0, "resid": 20, "dx": 0, "dy": 0, "edge": 10}),
        ]
        t = np.zeros(len(items), TILE_DT)
        for i, (m, v) in enumerate(items):
            t[i]["morton"] = m
            t[i]["cls"] = v["cls"]
            t[i]["flags"] = v["flags"]
            t[i]["resid"] = v["resid"]
            t[i]["mv_dx"] = v["dx"]
            t[i]["mv_dy"] = v["dy"]
            t[i]["edge"] = v["edge"]

        vc_regions = R.regions_of(t, W, H, 0)
        frame_row = make_frame_row(0, regions_cnt=len(vc_regions))

        brief = vision_brief.encode([frame_row], vc_regions)
        self.assertEqual(len(brief["regions"]), 1)
        self.assertEqual(brief["regions"][0]["n_tiles"], 2)


if __name__ == "__main__":
    unittest.main()
