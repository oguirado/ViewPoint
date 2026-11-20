# SPDX-License-Identifier: AGPL-3.0-only
from dataclasses import replace
import unittest
from unittest.mock import patch

import game.scene as scene_api
from game.scene.raster import SKY_PERIOD, SKY_X, SKY_Y, WIDTH
from game.scene.reconstruct import _partition

from test_scene import frames_for, reference_raster


def occurrences(frames):
    return [record for _, record in _partition(frames)]


def without_stars(x, y=0):
    image = reference_raster(x, y)
    image[SKY_Y, (SKY_X - x // 4) % WIDTH, 1] = 0
    return image


class CameraRepairTests(unittest.TestCase):
    def test_unanchored_cut_resets_period_and_suppresses_witnesses(self):
        positions = [*range(760, 1541, 4), 40, 44]
        frames = frames_for([reference_raster(x) for x in positions] + [reference_raster(spawn=True)])
        records = occurrences(frames)
        self.assertEqual(records[-4][1].offset[0], 1540)
        self.assertEqual(records[-3][2], "camera_discontinuity")
        self.assertEqual(records[-3][1].offset[0], 40)
        self.assertEqual(records[-2][1].offset[0], 44)
        self.assertIsNone(records[-2][2])
        for _, raster, _ in records[-3:-1]:
            self.assertGreaterEqual(raster.uncertainty[0], 2 * SKY_PERIOD)
            self.assertEqual(raster.cells, {})
        self.assertEqual(records[-1][2], "camera_discontinuity")
        self.assertEqual(records[-1][1].offset, (0, 0))
        self.assertEqual(records[-1][1].uncertainty, (0, 0))
        self.assertTrue(records[-1][1].cells)

    def test_wrap_uncertainty_does_not_hide_local_cut(self):
        frames = frames_for(reference_raster(x) for x in (0, 8, 12, 300, 304))
        frames[1:] = [replace(frame, ticks=frame.ticks + 8000) for frame in frames[1:]]
        records = occurrences(frames)
        self.assertGreaterEqual(records[2][1].uncertainty[0], SKY_PERIOD)
        self.assertIsNone(records[2][2])
        self.assertEqual(records[3][2], "camera_discontinuity")
        self.assertEqual(records[3][1].offset[0], 300)
        self.assertGreaterEqual(records[3][1].uncertainty[0], SKY_PERIOD)
        self.assertIsNone(records[4][2])

    def test_hud_boundary_does_not_certify_a_carried_period(self):
        with_panel = reference_raster(4)
        with_panel[38:73, 36:56, 1] = 53
        frames = frames_for([reference_raster(x) for x in (760, 764, 768)] + [with_panel])
        records = occurrences(frames)
        self.assertEqual(records[2][1].offset[0], 768)
        self.assertEqual(records[3][2], "hud_transition")
        self.assertEqual(records[3][1].offset[0], 4)
        self.assertGreaterEqual(records[3][1].uncertainty[0], SKY_PERIOD)
        self.assertEqual(records[3][1].cells, {})

    def test_circular_step_cannot_cross_negative_x_when_anchored(self):
        records = occurrences(frames_for(reference_raster(x) for x in (0, 764)))
        self.assertEqual(records[1][2], "camera_discontinuity")

    def test_vertical_cut_is_detected_with_stable_phase(self):
        records = occurrences(frames_for([reference_raster(), reference_raster(y=7)]))
        self.assertEqual(records[1][2], "camera_discontinuity")
        self.assertGreaterEqual(records[1][1].uncertainty[0], SKY_PERIOD)

    def test_respawn_after_ambiguous_gap_uses_local_displacement(self):
        frames = frames_for([reference_raster(), reference_raster(4), reference_raster(spawn=True)])
        frames[1:] = [replace(frame, ticks=frame.ticks + 8000) for frame in frames[1:]]
        records = occurrences(frames)
        self.assertGreaterEqual(records[1][1].uncertainty[0], SKY_PERIOD)
        self.assertEqual(records[2][2], "camera_discontinuity")
        self.assertEqual(records[2][1].uncertainty, (0, 0))

    def test_single_sky_occlusion_preserves_camera_and_hud_occurrence(self):
        occluded = without_stars(8)
        occluded[38:73, 36:56, 1] = 53
        frames = frames_for([reference_raster(4), occluded, reference_raster(12)])
        records = occurrences(frames)
        self.assertEqual([reason for _, _, reason in records],
                         ["recording_start", "hud_transition", "hud_transition"])
        self.assertEqual(records[1][1].offset, (4, 0))
        self.assertGreaterEqual(records[1][1].uncertainty[0], 5)
        self.assertTrue(records[1][1].panel)
        self.assertEqual(records[1][1].cells, {})
        self.assertEqual(records[2][1].offset, (12, 0))
        self.assertEqual(records[2][1].uncertainty, (0, 0))
        self.assertTrue(records[2][1].cells)

    def test_hidden_frame_does_not_erase_later_cut(self):
        frames = frames_for([reference_raster(4), without_stars(8), reference_raster(300)])
        records = occurrences(frames)
        self.assertIsNone(records[1][2])
        self.assertEqual(records[2][2], "camera_discontinuity")
        self.assertGreaterEqual(records[2][1].uncertainty[0], SKY_PERIOD)

    def test_hud_boundaries_remain_pending_across_missing_sky(self):
        with_panel = without_stars(4)
        with_panel[38:73, 36:56, 1] = 53
        images = [reference_raster(x) for x in (760, 764, 768)]
        records = occurrences(frames_for(images + [with_panel, without_stars(8), reference_raster(12)]))
        self.assertEqual([reason for _, _, reason in records[3:]], ["hud_transition", "hud_transition", None])
        self.assertEqual(records[-1][1].offset[0], 12)
        self.assertGreaterEqual(records[-1][1].uncertainty[0], SKY_PERIOD)

    def test_hidden_sky_does_not_reset_the_floor_fallback(self):
        no_floor = reference_raster(8, 3)
        no_floor[93] = 0
        images = [reference_raster(0, 3), without_stars(4, 3), no_floor, reference_raster(12, 3)]
        records = occurrences(frames_for(images))
        self.assertEqual([raster.offset[1] for _, raster, _ in records], [3, 3, 3, 3])
        self.assertGreater(records[2][1].uncertainty[1], 0)

    def test_leading_and_trailing_sky_occlusions_emit_uncertain_samples(self):
        frames = frames_for([without_stars(4), reference_raster(8), without_stars(12)])
        with patch("game.scene.reconstruct.read_frames", return_value=frames):
            shot, = scene_api.reconstruct("unused")["shots"]
        self.assertEqual([entry["frame_index"] for entry in shot["observations"]], [0, 1, 2])
        for index in (0, 2):
            self.assertGreaterEqual(shot["observations"][index]["uncertainty_px"][0], 5)
            self.assertEqual(shot["level_map"]["camera_samples"][index]["offset_px"], [8, 0])
        self.assertEqual(shot["observations"][1]["uncertainty_px"], [0, 0])
        self.assertTrue(shot["tile_witnesses"])
        self.assertEqual({w["frame_index"] for w in shot["tile_witnesses"]}, {1})

    def test_all_sky_occluded_has_no_recording_anchor(self):
        with self.assertRaisesRegex(ValueError, "unresolved synthetic sky registration"):
            list(_partition(frames_for([without_stars(0), without_stars(4)])))
