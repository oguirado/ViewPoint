# SPDX-License-Identifier: AGPL-3.0-only
"""Raster evidence controls for movable overlays, learned tiles and partial outputs."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import numpy as np

from game.scene import hud_mask, reconstruct, validate_scene
from game.scene.calibration import _actor_extents, _rim_and_seams

from test_scene import fixtures, frames_for


def recording(status, size=14, positions=range(0, 29, 4)):
    width, height = 192, 112
    for x in positions:
        rgb = np.full((height, width, 3), (48, 61, 72), dtype=np.uint8)
        for k in range(40):
            rgb[19 + k * 31 % 64, (k * 71 - x // 4) % width] = (173, 182, 208)
        floor = height - size
        rgb[floor:] = (121, 109, 90)
        rgb[floor] = (236, 190, 120)
        rgb[floor, (size - 1 - x) % size::size] = (121, 109, 90)
        left, top, w, h = status
        rgb[top:top + h, left:left + w] = (68, 37, 65)
        rgb[top + 3:top + h - 2:3, left + 2:left + w - 2:3] = (232, 231, 220)
        yield rgb


def run(images):
    frames = frames_for(images)
    with patch("game.scene.reconstruct.read_frames", return_value=frames):
        return reconstruct("unused")


class CalibrationTests(unittest.TestCase):
    def test_learns_top_bottom_and_side_hud_and_nondefault_tiles(self):
        for status in ((42, 2, 130, 16), (19, 96, 128, 16), (174, 21, 15, 74)):
            with self.subTest(status=status):
                shot, = run(recording(status))["shots"]
                self.assertTrue(shot["tile_witnesses"])
                self.assertEqual(shot["level_map"]["tiles"]["tile_size_px"], 14)
                self.assertIn("#", shot["level_map"]["tiles"]["grid"][-1])
                for row in shot["observations"]:
                    self.assertEqual(row["hud"]["status_xywh"], list(status))
                    self.assertNotIn("degraded", row)
                self.assertEqual([s["offset_px"][0] for s in shot["level_map"]["camera_samples"]],
                                 list(range(0, 29, 4)))

    def test_unanchored_cut_records_the_alias_and_withholds_witnesses(self):
        scene = run(recording((12, 4, 128, 16), size=16, positions=(0, 4, 8, 12, 200, 204)))
        self.assertEqual(len(scene["shots"]), 2)
        cut = scene["shots"][1]
        self.assertEqual(cut["start_reason"], "camera_discontinuity")
        self.assertFalse(cut["tile_witnesses"])
        for row in cut["observations"]:
            self.assertIn("camera_period_unresolved", row.get("degraded", []))
            self.assertGreaterEqual(row["uncertainty_px"][0], 768)

    def test_missing_components_emit_a_valid_unknown_grid(self):
        try:
            scene = run([np.zeros((2, 3, 3), dtype=np.uint8)])
        except ValueError as exc:
            self.fail(f"missing components refused the entire scene: {exc}")
        self.assertEqual(scene["viewport_px"], [3, 2])
        shot, = scene["shots"]
        self.assertEqual(shot["level_map"]["tiles"]["grid"], [[None]])
        self.assertFalse(shot["tile_witnesses"])
        row, = shot["observations"]
        self.assertIsNone(row["hud"]["status_xywh"])
        self.assertEqual(hud_mask(row["hud"], (3, 2)).sum(), 0)
        self.assertIn("tile_geometry_unresolved", row["degraded"])
        self.assertIn("status_band_unresolved", row["degraded"])
        self.assertIn("camera_landmarks_unresolved", row["degraded"])
        self.assertTrue(all(row["uncertainty_px"]))

    def test_changed_hud_descriptor_cannot_bind_a_tile_witness(self):
        scene = run(recording((19, 96, 128, 16)))
        shot, = scene["shots"]
        witness = shot["tile_witnesses"][0]
        index = witness["frame_index"]
        dx, dy = shot["level_map"]["camera_samples"][index]["offset_px"]
        row = shot["observations"][index]
        status = [witness["column"] * 14 - dx, witness["row"] * 14 - dy, 14, 14]
        row["hud"]["status_xywh"] = status
        with self.assertRaisesRegex(ValueError, "occluded by HUD"):
            validate_scene(scene)
        row["hud"]["status_xywh"] = None
        row["degraded"] = ["status_band_unresolved"]
        with self.assertRaisesRegex(ValueError, "outside a resolved visible footprint"):
            validate_scene(scene)

    def test_rejects_geometry_inconsistent_with_recovery_state(self):
        blank = run([np.zeros((2, 3, 3), dtype=np.uint8)])
        for target, reason in (
            ("missing reason", "inconsistent with its recovery state"),
            ("radius without panel", "inconsistent with its recovery state"),
            ("outside viewport", "outside the viewport"),
            ("calibrated tile size", "cannot claim a calibrated grid"),
            ("invented grid", "cannot claim a calibrated grid"),
            ("legacy tile size", "legacy tile size is inconsistent"),
        ):
            with self.subTest(target=target):
                scene = deepcopy(blank)
                shot, = scene["shots"]
                row, = shot["observations"]
                if target == "missing reason":
                    row["degraded"].remove("status_band_unresolved")
                elif target == "radius without panel":
                    row["hud"]["panel_corner_radius_px"] = 3
                elif target == "outside viewport":
                    row["hud"]["panel_xywh"] = [1, 1, 3, 1]
                    row["hud"]["panel_corner_radius_px"] = 1
                elif target == "calibrated tile size":
                    shot["level_map"]["tiles"]["tile_size_px"] = 14
                elif target == "invented grid":
                    shot["level_map"]["tiles"]["grid"] = [[None, "."]]
                else:
                    scene = fixtures()["valid"]
                    scene["shots"][0]["level_map"]["tiles"]["tile_size_px"] = 14
                with self.assertRaisesRegex(ValueError, reason):
                    validate_scene(scene)

    def test_terminal_seam_needs_visible_rim_and_matching_fill(self):
        luma = np.array([[160] * 7 + [72], [72] * 8])
        hud = np.zeros_like(luma, dtype=bool)
        self.assertEqual(_rim_and_seams(luma, hud, 0, 160, 72).tolist(), [7])
        for y, x in ((0, 5), (0, 7), (1, 7)):
            with self.subTest(occluded=(y, x)):
                covered = hud.copy()
                covered[y, x] = True
                self.assertEqual(_rim_and_seams(luma, covered, 0, 160, 72).tolist(), [])

    def test_clipped_sprite_extent_blocks_neighboring_cell(self):
        marked = np.zeros((48, 64), dtype=bool)
        marked[30, 61:] = True
        mask = _actor_extents(marked)
        self.assertTrue(mask[32, 63])
        self.assertFalse(mask[32, 58])
        self.assertFalse(mask[41, 63])
