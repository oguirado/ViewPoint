# SPDX-License-Identifier: AGPL-3.0-only
"""D12 controls for complete tile labels and clipped raster evidence."""
from copy import deepcopy
import unittest

import numpy as np

from game.scene import validate_scene
from game.scene.calibration import Terrain, _observe, _sparse_floor

from test_calibration import recording, run


class D12VisibilityTests(unittest.TestCase):
    def test_complete_air_at_final_pan_edge_survives_foreground(self):
        images = list(recording((22, 0, 144, 16), size=16, positions=(4, 8, 12, 16)))
        # The goal pole dominates one column in the only complete view.
        images[-1][80:96, 187] = (255, 255, 255)
        shot, = run(images)["shots"]
        self.assertEqual(shot["level_map"]["tiles"]["grid"][5][12], ".")
        self.assertIn({"row": 5, "column": 12, "frame_index": 3}, shot["tile_witnesses"])

    def test_hud_edge_codec_noise_keeps_complete_air_and_solid(self):
        sky, fill, rim = 60, 110, 190
        terrain = Terrain(16, 16, rim, fill, fill)
        luma = np.full((32, 32), sky, dtype=np.int16)
        hud = np.zeros_like(luma, dtype=bool)
        hud[:16, :16] = True
        luma[:16, 16] = 83  # Codec halo directly outside the HUD rectangle.
        luma[16:] = fill
        luma[16, :15] = luma[16, 16:31] = rim
        luma[16:20, 16] = sky  # An adjacent actor disturbs the terrain rim.
        observed = _observe(luma, hud, sky, terrain, (0, 0), 16, ([], []))
        self.assertEqual(observed, {(0, 1): ".", (1, 0): "#", (1, 1): "#"})

    def test_air_requires_sky_support_and_a_hud_clear_footprint(self):
        sky, fill, rim = 60, 110, 190
        terrain = Terrain(16, 32, rim, fill, fill)
        luma = np.full((48, 48), sky, dtype=np.int16)
        hud = np.zeros_like(luma, dtype=bool)
        luma[:16, :5] = 240   # Too little sky under the foreground.
        luma[16, :3] = rim    # A terrain-like rim and body oppose the sky claim.
        luma[17:32, :3] = fill
        hud[0, 16] = True     # A single overlapping HUD pixel blocks the tile.
        observed = _observe(luma, hud, sky, terrain, (1, 0), 32, ([], []))
        self.assertNotIn((0, 0), observed)  # Clipped by the viewport.
        self.assertNotIn((0, 1), observed)  # Clipped by the HUD.
        self.assertNotIn((0, 3), observed)  # Clipped on the far edge.
        aligned = _observe(luma, hud, sky, terrain, (0, 0), 32, ([], []))
        self.assertNotIn((0, 0), aligned)
        self.assertNotIn((1, 0), aligned)
        self.assertEqual(aligned[0, 2], ".")

    def test_air_does_not_count_sky_inside_a_large_foreground_outline(self):
        terrain = Terrain(16, 16, 190, 110, 110)
        luma = np.full((32, 32), 60, dtype=np.int16)
        hud = np.zeros_like(luma, dtype=bool)
        luma[1, 1:15] = luma[14, 1:15] = 240
        luma[1:15, 1] = luma[1:15, 14] = 240
        self.assertNotIn((0, 0), _observe(luma, hud, 60, terrain, (0, 0), 16, ([], [])))

    def test_sparse_rim_resolves_only_supported_axes(self):
        terrain = Terrain(16, 16, 190, 110, 110)
        luma = np.full((48, 48), 60, dtype=np.int16)
        hud = np.zeros_like(luma, dtype=bool)
        offsets = np.arange(8, 12)
        luma[16, :5] = 190
        luma[17:32, :6] = luma[16, 5] = 110
        self.assertEqual(_sparse_floor(luma, hud, terrain, offsets, range(13, 17)), (16, 10))
        hud[16, 3:6] = True
        self.assertEqual(_sparse_floor(luma, hud, terrain, offsets, (16,)), (None, None))
        hud[:] = False
        luma[16:32, 16:22] = luma[16:32, :6]
        luma[16:32, :6] = 60
        luma[16, 16:20] = 190
        luma[16:32, 20:22] = 60
        self.assertEqual(_sparse_floor(luma, hud, terrain, offsets, (16,)), (16, None))
        luma[32:48, :] = luma[16:32, :]
        self.assertEqual(_sparse_floor(luma, hud, terrain, offsets, (16, 32)), (None, None))

    def test_sparse_rim_requires_room_for_a_full_tile_height(self):
        terrain = Terrain(16, 33, 190, 110, 110)
        luma = np.full((48, 48), 60, dtype=np.int16)
        hud = np.zeros_like(luma, dtype=bool)
        luma[33, :5] = 190
        luma[34:, :6] = luma[33, 5] = 110
        self.assertEqual(_sparse_floor(luma, hud, terrain, np.arange(8, 12), (33,)), (None, None))

    def test_clipped_solid_contributes_raster_support_but_no_label(self):
        scene = run(recording((22, 0, 144, 16), size=16, positions=(4, 4, 4)))
        shot, = scene["shots"]
        self.assertIsNone(shot["level_map"]["tiles"]["grid"][6][12])
        self.assertIn({"row": 6, "column": 12, "frame_index": 0, "visible_xywh": [188, 96, 4, 16]},
                      shot["tile_witnesses"])
        legacy = deepcopy(scene)
        legacy["shots"][0]["level_map"]["tiles"]["grid"][6][12] = "#"
        self.assertIs(validate_scene(legacy), legacy)
        changed = deepcopy(scene)
        changed["shots"][0]["tile_witnesses"].append(shot["tile_witnesses"][-1])
        with self.assertRaisesRegex(ValueError, "duplicate tile witness"):
            validate_scene(changed)
        changed["shots"][0]["tile_witnesses"].pop()
        changed["shots"][0]["tile_witnesses"][-1]["column"] += 1
        with self.assertRaisesRegex(ValueError, "invalid or duplicate tile witness"):
            validate_scene(changed)
