# SPDX-License-Identifier: AGPL-3.0-only
from copy import deepcopy
from dataclasses import replace
from fractions import Fraction
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

import game.scene as scene_api
from game.scene.frames import Frame
from game.scene.raster import HEIGHT, SKY_X, SKY_Y, WIDTH, measure, register
from game.scene.reconstruct import _partition, _stitch
from source.records import canonical, digest


ROOT = Path(__file__).resolve().parents[1]


def fixtures():
    return json.loads((ROOT / "scene.fixtures.json").read_bytes())


def reference_raster(x=0, y=0, platforms=(), spawn=False):
    """An isolated reference grid with a status strip, terrain and star features."""
    raster = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    raster[0:16, :, 1] = 53
    raster[SKY_Y, (SKY_X - x // 4) % WIDTH, 1] = 140
    floor = 96 - y
    raster[floor:, :, 1] = 70
    raster[floor, :, 1] = 180
    raster[floor, (15 - x) % 16::16, 1] = 70
    for row, col in platforms:
        sx, sy = col * 16 - x, row * 16 - y
        raster[sy:sy + 16, sx:sx + 16, 1] = 70
        raster[sy, sx:sx + 15, 1] = 180
    if spawn:
        raster[floor - 12:floor, 12:21] = (82, 235, 207)
    return raster


def frames_for(images, spacing=40):
    return [Frame(image, i, spacing * i, Fraction(1, 1000), "a" * 64, "b" * 64, "c" * 64)
            for i, image in enumerate(images)]


class SceneTests(unittest.TestCase):
    def test_scene_public_entry_points(self):
        try:
            from game.scene import reconstruct, validate_scene
        except ImportError:
            self.fail("G1A must expose reconstruction and validation")
        self.assertTrue(callable(reconstruct))
        self.assertTrue(callable(validate_scene))

    def test_hud_mask_is_public(self):
        self.assertIn("hud_mask", scene_api.__all__)
        self.assertEqual(scene_api.hud_mask(fixtures()["valid"]["shots"][0]["observations"][0]["hud"]).shape,
                         (HEIGHT, WIDTH))

    def test_pinned_fixtures(self):
        from game.scene import validate_scene
        pins = json.loads((ROOT / "PINS.json").read_bytes())["files"]
        self.assertEqual(set(pins), {"scene.schema.json", "scene.fixtures.json"})
        for name, pin in pins.items():
            self.assertEqual(digest(canonical(json.loads((ROOT / name).read_bytes()))), pin)
        examples = fixtures()
        self.assertEqual(validate_scene(examples["valid"]), examples["valid"])
        with self.assertRaisesRegex(ValueError, "scene schema rejected.*required"):
            validate_scene(examples["invalid"])

    def test_runtime_scene_pin_after_validator_warmup(self):
        scene = fixtures()["valid"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            schema_path = root / "scene.schema.json"
            schema = json.loads((ROOT / schema_path.name).read_bytes())
            (root / "PINS.json").write_bytes(canonical({"files": {schema_path.name: digest(canonical(schema))}}))
            schema_path.write_text(json.dumps(schema))
            with patch("game.scene.validation.SCENE_ROOT", root, create=True):
                self.assertEqual(scene_api.validate_scene(scene), scene)
                schema["title"] = "Drifted scene schema"
                schema_path.write_bytes(canonical(schema))
                with self.assertRaisesRegex(ValueError, "scene schema pin mismatch"):
                    scene_api.validate_scene(scene)

    def test_rejects_bad_rp2_schema_pin(self):
        from game.scene.validation import validate_scene
        with tempfile.TemporaryDirectory() as tmp:
            name = "rea.game.level_map_v1.schema.json"
            contracts = Path(__file__).resolve().parents[3] / "contracts"
            root = Path(tmp)
            for file in ("PINS.json", "rea.frame_v1.schema.json"):
                (root / file).write_bytes((contracts / file).read_bytes())
            changed = json.loads((contracts / name).read_bytes())
            changed["title"] = "Drifted contract"
            (root / name).write_text(json.dumps(changed))
            with patch("game.scene.validation.CONTRACTS", root):
                with self.assertRaisesRegex(ValueError, "RP2 contract pin mismatch"):
                    validate_scene(fixtures()["valid"])

    def test_unseen_tile_fails_without_witness(self):
        from game.scene import validate_scene
        scene = fixtures()["valid"]
        scene["shots"][0]["level_map"]["tiles"]["grid"][0][1] = "."
        with self.assertRaisesRegex(ValueError, "observed tile is missing a witness"):
            validate_scene(scene)

    def test_semantic_guards(self):
        from game.scene import validate_scene
        valid = fixtures()["valid"]
        for case, reason in (
            ("epoch", "invalid shot or geometry epoch"),
            ("extra camera", "unequal occurrence, camera and evidence counts"),
            ("rational pts", "does not match its source occurrence"),
            ("hud descriptor", "HUD geometry is inconsistent"),
            ("lineage", "derived_from does not match shot PNGs"),
            ("tile legend", "missing from the legend"),
            ("duplicate witness", "invalid or duplicate tile witness"),
            ("outside raster", "outside a resolved visible footprint"),
            ("uncertain", "outside a resolved visible footprint"),
            ("inside HUD", "occluded by HUD"),
        ):
            with self.subTest(case=case):
                value = deepcopy(valid)
                shot = value["shots"][0]
                level = shot["level_map"]
                if case == "epoch":
                    shot["geometry_epoch"] = 1
                elif case == "extra camera":
                    level["camera_samples"].append(deepcopy(level["camera_samples"][0]))
                elif case == "rational pts":
                    shot["observations"][0]["pts_ticks"] = 1
                    value["time_base"]["denominator"] = 30000
                elif case == "hud descriptor":
                    shot["observations"][0]["hud"]["status_xywh"][3] = 8
                elif case == "lineage":
                    level["derived_from"] = ["0" * 64]
                elif case == "tile legend":
                    level["tiles"]["grid"][1][0] = "?"
                elif case == "duplicate witness":
                    shot["tile_witnesses"].append(shot["tile_witnesses"][0].copy())
                elif case == "outside raster":
                    level["camera_samples"][0]["offset_px"] = [1, 0]
                elif case == "uncertain":
                    shot["observations"][0]["uncertainty_px"] = [1, 0]
                elif case == "inside HUD":
                    level["tiles"]["grid"][0][0], level["tiles"]["grid"][1][0] = ".", None
                    shot["tile_witnesses"][0]["row"] = 0
                with self.assertRaisesRegex(ValueError, reason):
                    validate_scene(value)

    def test_raster_witness_excludes_actors_and_hud(self):
        raster = reference_raster()
        raster[30, 40, 1] = 230  # Foreground inside the air cell at row 1, column 2.
        result = measure(raster)
        self.assertEqual(result.offset, (0, 0))
        self.assertEqual(result.uncertainty, (0, 0))
        self.assertEqual(result.cells[1, 1], ".")
        self.assertEqual(result.cells[6, 0], "#")
        self.assertNotIn((1, 2), result.cells)
        self.assertFalse(any(row == 0 for row, _ in result.cells))

    def test_raster_guards(self):
        for case, reason in (
            ("dimensions", "unsupported synthetic raster geometry"),
            ("status", "unrecognized synthetic HUD status strip"),
        ):
            with self.subTest(case=case):
                raster = reference_raster()
                if case == "dimensions":
                    raster = raster[:-1]
                elif case == "status":
                    raster[:16] = 0
                with self.assertRaisesRegex(ValueError, reason):
                    measure(raster)

    def test_conflicting_tile_observations_remain_unknown(self):
        from game.scene.raster import Raster
        image = reference_raster()
        first = Frame(image, 0, 0, Fraction(1, 1000), "a" * 64, "b" * 64, "c" * 64)
        second = Frame(image, 1, 20, Fraction(1, 1000), "a" * 64, "d" * 64, "e" * 64)
        shot = _stitch(0, [(first, Raster((0, 0), (0, 0), False, {(6, 0): "#"}), "recording_start"),
                           (second, Raster((0, 0), (0, 0), False, {(6, 0): "."}), None)])
        self.assertIsNone(shot["level_map"]["tiles"]["grid"][6][0])
        self.assertEqual(shot["tile_witnesses"], [])

    def test_ambiguous_registration_cannot_witness_a_tile(self):
        raster = reference_raster()
        raster[96, 14::16, 1] = 70
        result = measure(raster)
        self.assertGreater(result.uncertainty[0], 0)
        self.assertEqual(result.cells, {})

    def test_wrap_keeps_two_periods_and_reverse_pan_in_one_shot(self):
        positions = [*range(0, 1541, 4), *range(1536, 759, -4)]
        records = [entry for _, entry in _partition(frames_for(reference_raster(x) for x in positions))]
        self.assertEqual([raster.offset[0] for _, raster, _ in records], positions)
        self.assertEqual([reason for _, _, reason in records if reason], ["recording_start"])
        peak = max(positions)
        cells = records[positions.index(peak)][1].cells
        self.assertEqual(cells.get((6, (peak + 15) // 16)), "#")
        self.assertNotIn((6, 0), cells)

    def test_respawn_at_origin_splits_even_after_a_wrap_or_small_jump(self):
        for positions in ([0, 4, 0], [*range(0, 781, 4), 0]):
            with self.subTest(previous=positions[-2]):
                images = [reference_raster(x) for x in positions[:-1]] + [reference_raster(spawn=True)]
                records = [entry for _, entry in _partition(frames_for(images))]
                self.assertEqual(records[-1][1].offset, (0, 0))
                self.assertEqual([reason for _, _, reason in records if reason],
                                 ["recording_start", "camera_discontinuity"])
        reversal = [entry for _, entry in _partition(frames_for(reference_raster(x) for x in (0, 4, 0)))]
        self.assertIsNone(reversal[-1][2])
        jump = [entry for _, entry in _partition(frames_for(reference_raster(x) for x in (0, 40)))]
        self.assertEqual(jump[-1][2], "camera_discontinuity")

    def test_gap_cannot_invent_a_period_or_tile_witness(self):
        frames = frames_for([reference_raster(x) for x in (0, 8, 12)] + [reference_raster(spawn=True)])
        frames[1:] = [replace(frame, ticks=frame.ticks + 8000) for frame in frames[1:]]
        records = [entry for _, entry in _partition(frames)]
        for _, raster, _ in records[1:3]:
            self.assertGreaterEqual(raster.uncertainty[0], 768)
            self.assertEqual(raster.cells, {})
        self.assertEqual(records[-1][1].offset, (0, 0))
        self.assertEqual(records[-1][1].uncertainty, (0, 0))
        self.assertTrue(records[-1][1].cells)
        unique = [entry for _, entry in _partition(frames_for(
            [reference_raster(0), reference_raster(400)], spacing=4000))]
        self.assertEqual(unique[-1][1].uncertainty, (0, 0))
        self.assertIsNone(unique[-1][2])

    def test_unresolved_baseline_or_seam_degrades_only_that_frame(self):
        for case in ("sparse baseline", "tied baseline", "seam"):
            with self.subTest(case=case):
                image = reference_raster(7, 3)
                if case == "sparse baseline":
                    image[93, 64:] = 0
                elif case == "tied baseline":
                    image[92] = image[93]
                else:
                    image[95] = 0
                records = [entry for _, entry in _partition(frames_for(
                    [reference_raster(4, 3), image, reference_raster(8, 3)]))]
                degraded = records[1][1]
                self.assertLessEqual(abs(degraded.offset[0] - 7), degraded.uncertainty[0])
                self.assertGreater(degraded.uncertainty[0], 0)
                self.assertEqual(degraded.offset[1], 3)
                self.assertEqual(degraded.uncertainty[1] > 0, case != "seam")
                self.assertEqual(degraded.cells, {})
                self.assertTrue(records[2][1].cells)
                self.assertEqual([reason for _, _, reason in records if reason], ["recording_start"])

    def test_seam_votes_ignore_hud_at_both_sampled_rows(self):
        green = reference_raster()[:, :, 1]
        for row in (96, 98):
            with self.subTest(row=row):
                hud = np.zeros((HEIGHT, WIDTH), dtype=bool)
                hud[row, 15::16] = True
                _, uncertainty, *_ = register(green, hud)
                self.assertGreater(uncertainty[0], 0)

    def test_elevated_solid_requires_a_full_tile_signature(self):
        image = reference_raster(platforms=[(3, 2), (3, 3), (3, 4), (3, 5), (3, 6)])
        image[48, 63, 1] = 180  # Tile (3, 3) lacks a soil-colored seam pixel.
        image[63, 64, 1] = 0    # Tile (3, 4) has an interrupted footprint.
        image[49, 80, 1] = 0    # Tile (3, 5) has an interruption below the highlight.
        image[48, 96, 1] = 70   # Tile (3, 6) has an interrupted highlight.
        result = measure(image)
        self.assertEqual(result.cells[(3, 2)], "#")
        self.assertNotIn((3, 3), result.cells)
        self.assertNotIn((3, 4), result.cells)
        self.assertNotIn((3, 5), result.cells)
        self.assertNotIn((3, 6), result.cells)
