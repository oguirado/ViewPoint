# SPDX-License-Identifier: AGPL-3.0-only
"""Regression tests for raster-compatible scenes and sprite-obscured air."""
from io import BytesIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from game.scene.__main__ import main
from game.scene.calibration import Terrain, _observe
from test_calibration import recording
from test_scene import frames_for


class SceneRP6Tests(unittest.TestCase):
    def test_cli_projects_witnessed_solids_and_leaves_unseen_tiles_unknown(self):
        status = (22, 96, 144, 16)
        frames = frames_for(recording(status, size=16, positions=(0, 0, 0)))
        stdout = BytesIO()
        with (patch("game.scene.reconstruct.read_frames", return_value=frames),
              patch("game.scene.__main__.sys.stdout", SimpleNamespace(buffer=stdout))):
            self.assertEqual(main(["unused"]), 0)

        shot, = json.loads(stdout.getvalue())["shots"]
        grid = shot["level_map"]["tiles"]["grid"]
        self.assertEqual(grid[6], ["#", "#", *([None] * 8), "#", "#"])
        self.assertEqual(grid[3][2], ".")
        self.assertIn({"row": 6, "column": 1, "frame_index": 0,
                       "visible_xywh": [16, 96, 6, 16]}, shot["tile_witnesses"])

    def test_air_has_sufficient_sky_around_a_small_sprite(self):
        luma = np.full((32, 32), 60, dtype=np.int16)
        y, x = np.ogrid[:16, :16]
        luma[:16, :16][(y - 8) ** 2 + (x - 8) ** 2 < 25] = 240
        luma[3:13, 19:28] = 240
        terrain = Terrain(16, 16, 190, 110, 110)
        cells = _observe(luma, np.zeros_like(luma, dtype=bool), 60, terrain,
                         (0, 0), 16, ([], []))
        self.assertEqual(cells.get((0, 0)), ".")
        self.assertNotIn((0, 1), cells)
