# SPDX-License-Identifier: AGPL-3.0-only
"""Regressions for partial solid evidence and same-screen origin resets."""
from copy import deepcopy
import unittest

import numpy as np

from game.scene import validate_scene
from game.scene.overlays import luminance
from game.scene.raster import Raster, SKY_PERIOD
from game.scene.reconstruct import _camera_step, _unwrap
from test_calibration import recording, run


STATUS = (22, 96, 144, 16)


class SceneRP4Tests(unittest.TestCase):
    def test_bottom_hud_keeps_visible_fragments_and_unknown_covered_tiles(self):
        shot, = run(recording(STATUS, size=16, positions=(0, 0, 0)))["shots"]
        self.assertEqual(shot["level_map"]["tiles"]["grid"][6],
                         ["#", *([None] * 10), "#"])
        supports = {w["column"]: w["visible_xywh"] for w in shot["tile_witnesses"]
                    if w["row"] == 6 and "visible_xywh" in w}
        self.assertEqual(supports, {1: [16, 96, 6, 16], 10: [166, 96, 10, 16]})

    def test_viewport_edge_keeps_a_solid_fragment_but_not_a_pit(self):
        image, = recording((22, 0, 144, 16), size=16, positions=(4,))
        shot, = run([image] * 3)["shots"]
        self.assertIsNone(shot["level_map"]["tiles"]["grid"][6][12])
        self.assertIn({"row": 6, "column": 12, "frame_index": 0, "visible_xywh": [188, 96, 4, 16]},
                      shot["tile_witnesses"])
        for hidden in (slice(96, None), slice(96, 97), slice(97, None)):
            with self.subTest(hidden=hidden):
                damaged = image.copy()
                damaged[hidden, 188:] = (48, 61, 72)
                shot, = run([damaged] * 3)["shots"]
                self.assertFalse(any((w["row"], w["column"]) == (6, 12) for w in shot["tile_witnesses"]))

    def test_one_painted_column_cannot_infer_a_tile(self):
        from game.scene import calibration

        image, = recording((22, 0, 144, 16), size=16, positions=(1,))
        self.assertTrue(hasattr(calibration, "_partial_solids"))
        luma = luminance(image)
        fill = int(luma[97, 0])
        terrain = calibration.Terrain(16, 96, int(luma[96, 0]), fill, fill)
        self.assertNotIn((6, 12), calibration._partial_solids(
            luma, np.zeros_like(luma, dtype=bool), terrain, (1, 0), 96))
        shot, = run([image] * 3)["shots"]
        self.assertIsNone(shot["level_map"]["tiles"]["grid"][6][12])
        self.assertFalse(any((w["row"], w["column"]) == (6, 12) for w in shot["tile_witnesses"]))

    def test_partial_support_cannot_claim_air_or_replace_a_full_footprint(self):
        scene = run(recording(STATUS, size=16, positions=(0, 0, 0)))
        shot, = scene["shots"]
        with self.subTest("air"):
            changed = deepcopy(shot)
            changed["level_map"]["tiles"]["grid"][6][1] = "."
            scene["shots"][0] = changed
            with self.assertRaisesRegex(ValueError, "partial tile evidence requires solid terrain"):
                validate_scene(scene)
        with self.subTest("unclipped"):
            changed = deepcopy(shot)
            full = next(w for w in changed["tile_witnesses"] if (w["row"], w["column"]) == (6, 0))
            full["visible_xywh"] = [0, 96, 8, 16]
            scene["shots"][0] = changed
            with self.assertRaisesRegex(ValueError, "requires a clipped footprint"):
                validate_scene(scene)

    def test_partial_support_in_hud_outside_tile_or_without_rim_is_refused(self):
        scene = run(recording(STATUS, size=16, positions=(0, 0, 0)))
        for box, reason in (([19, 96, 6, 16], "occluded by HUD"),
                            ([14, 96, 6, 16], "inside the tile"),
                            ([16, 97, 6, 15], "inside the tile"),
                            ([16, 96, 1, 16], "scene schema rejected")):
            with self.subTest(box=box):
                changed = deepcopy(scene)
                witnesses = changed["shots"][0]["tile_witnesses"]
                witness = next((w for w in witnesses if (w["row"], w["column"]) == (6, 1)), None)
                self.assertIsNotNone(witness, "the partial witness must exist before corruption")
                witness["visible_xywh"] = box
                with self.assertRaisesRegex(ValueError, reason):
                    validate_scene(changed)

    def test_same_screen_return_to_witnessed_spawn_marks_reset_without_cut(self):
        images = list(recording((0, 0, 192, 16), size=16, positions=(0, 0, 32, 0, 0)))
        # Same color and source coordinates at departure and at the respawn.
        # The title outline is visible only in the first frame.
        images[0][34, 35:156] = images[0][78, 35:156] = (210, 215, 215)
        images[0][37:76, 31] = images[0][37:76, 160] = (210, 215, 215)
        for index in (1, 3, 4):
            images[index][84:96, 12:21] = (143, 239, 218)
        scene = run(images)
        self.assertEqual([shot["observations"][0]["frame_index"] for shot in scene["shots"]], [0, 1, 2])
        reset = scene["shots"][-1]["observations"][1]
        self.assertIs(reset.get("camera_reset"), True)
        self.assertEqual(scene["shots"][-1]["level_map"]["camera_samples"][1]["offset_px"], [0, 0])
        self.assertNotIn("camera_reset", scene["shots"][-1]["observations"][2])

    def test_reset_marker_requires_resolved_origin(self):
        scene = run(recording(STATUS, size=16, positions=(0, 4, 0)))
        for index, uncertainty in ((0, [0, 0]), (1, [0, 0]), (2, [2, 0])):
            with self.subTest(index=index):
                changed = deepcopy(scene)
                obs = changed["shots"][0]["observations"][index]
                obs.update(camera_reset=True, uncertainty_px=uncertainty)
                with self.assertRaisesRegex(ValueError, "reset requires a resolved return to the origin"):
                    validate_scene(changed)

    def test_reset_marker_may_be_absent_or_true(self):
        scene = run(recording(STATUS, size=16, positions=(0, 4, 0)))
        scene["shots"][0]["observations"][2]["camera_reset"] = False
        with self.assertRaisesRegex(ValueError, "scene schema rejected"):
            validate_scene(scene)

    def test_a_complete_witness_supersedes_a_partial_witness(self):
        shot, = run(recording((22, 0, 144, 16), size=16, positions=range(4, 17, 4)))["shots"]
        witness = next(w for w in shot["tile_witnesses"] if (w["row"], w["column"]) == (6, 12))
        self.assertEqual(witness, {"row": 6, "column": 12, "frame_index": 3})

    def test_clipped_support_cannot_extend_past_the_viewport(self):
        scene = run(recording((22, 0, 144, 16), size=16, positions=(4, 4, 4)))
        witnesses = scene["shots"][0]["tile_witnesses"]
        witness = next((w for w in witnesses if (w["row"], w["column"]) == (6, 12)), None)
        self.assertIsNotNone(witness, "the edge witness must exist before corruption")
        witness["visible_xywh"][2] += 1
        with self.assertRaisesRegex(ValueError, "outside a resolved visible footprint"):
            validate_scene(scene)

    def test_witness_geometry_accepts_integral_json_numbers(self):
        scene = run(recording(STATUS, size=16, positions=(0, 0, 0)))
        shot, = scene["shots"]
        for sample in shot["level_map"]["camera_samples"]:
            sample["offset_px"] = list(map(float, sample["offset_px"]))
        for witness in shot["tile_witnesses"]:
            witness.update(row=float(witness["row"]), column=float(witness["column"]))
            if "visible_xywh" in witness:
                witness["visible_xywh"] = list(map(float, witness["visible_xywh"]))
        self.assertIs(validate_scene(scene), scene)

    def test_partial_support_needs_an_aligned_full_height_floor(self):
        from game.scene import calibration

        self.assertTrue(hasattr(calibration, "_partial_solids"))

        image, = recording((22, 0, 144, 16), size=16, positions=(4,))
        luma = luminance(image)
        hud = np.zeros_like(luma, dtype=bool)
        terrain = calibration.Terrain(16, 96, int(luma[96, 0]), int(luma[97, 0]), int(luma[97, 0]))
        self.assertTrue(calibration._partial_solids(luma, hud, terrain, (4, 0), 96))
        self.assertFalse(calibration._partial_solids(luma, hud, terrain, (4, 1), 96))
        self.assertFalse(calibration._partial_solids(luma, hud, terrain, (4, -112), 96))
        self.assertFalse(calibration._partial_solids(luma[:-1], hud[:-1], terrain, (4, 0), 96))

    def test_partial_support_uses_resolved_world_wrap_only(self):
        self.assertIn("partial_solids", Raster.__dataclass_fields__)
        local = Raster((4, 0), (0, 0), None, {}, partial_solids={(6, 12): (188, 96, 4, 16)})
        wrapped, error = _unwrap(local, SKY_PERIOD, 8, 0, SKY_PERIOD, 16)
        self.assertEqual(error, 0)
        self.assertEqual(wrapped.partial_solids, {(6, 12 + SKY_PERIOD // 16): (188, 96, 4, 16)})
        for budget, size in ((SKY_PERIOD + 8, 16), (8, 14)):
            with self.subTest(budget=budget, size=size):
                wrapped, _ = _unwrap(local, SKY_PERIOD, budget, 0, SKY_PERIOD, size)
                self.assertFalse(wrapped.partial_solids)
        unanchored, uncertainty, cut = _camera_step(local, Raster((200, 0), (0, 0), None, {}),
                                                   8, 0, False, SKY_PERIOD, 16)
        self.assertTrue(cut)
        self.assertGreaterEqual(uncertainty, SKY_PERIOD)
        self.assertFalse(unanchored.partial_solids)
