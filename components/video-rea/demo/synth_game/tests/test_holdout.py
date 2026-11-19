# SPDX-License-Identifier: AGPL-3.0-only
"""Compatibility, variation and integrity gates for the holdout recording."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from demo.synth_game.generate import digest, generate, main


DEV_PINS = {
    False: ("5319f50b9fcd49b59668810b760f06f3f0687036fe49d3e9a44eb3c1ed1c4267",
            "6338e49efdaefd6dcac1f95a548327a14db7fd2a7688e564032ca963cb8c53d8"),
    True: ("9daf3e8b3aafef2047dfb79db8684e7ea9176ade872f8eb4b58a774cfa0e9cf6",
           "9f2c3351da1d0098231cb0f556c13f5ab5cdd13e7f1d7010b80bf42e5cc2d334"),
}
HOLDOUT_SEEDS = (*range(17), 42, 83, 2**64 - 1)


def read_truth(directory):
    return json.loads((directory / "ground_truth.json").read_text(encoding="utf-8"))


def dimensions(truth):
    level = truth["level"]
    floor = level["rows"][truth["rules"]["floor_y_px"] // level["tile_px"]]
    return {
        "width": len(floor) * level["tile_px"],
        "layout": tuple(i for i, tile in enumerate(floor) if tile == "."),
        "palette": truth.get("palette"),
        "enemy placement": {name: (spawn["x_px"], spawn["y_px"])
                            for name, spawn in truth["enemy_spawns"].items()},
        # Compare offsets on a common tick schedule, excluding event-driven frame insertions.
        "camera path": tuple(frame["camera_x_px"] for frame in truth["frames"]
                             if frame["tick"] % 12 == 0),
        "HUD position": truth["frames"][0].get("widgets", {}).get("hud", {}).get("bbox_px"),
    }


class HoldoutRecording(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = tempfile.TemporaryDirectory()
        cls.addClassCleanup(scratch.cleanup)
        cls.root = Path(scratch.name)

    def record(self, name, seed, *flags):
        directory = self.root / name
        if not directory.exists():
            main(["--out", str(directory), "--seed", str(seed), *flags])
        return directory

    def test_default_dev_pins(self):
        self.assert_dev_pins()

    def test_explicit_dev_pins(self):
        self.assert_dev_pins("--profile", "dev")

    def test_cli_rejects_unknown_profile(self):
        target = self.root / "cli-unknown"
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            self.record(target.name, 42, "--profile", "unknown")
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(target.exists())

    def test_api_rejects_unknown_profile(self):
        target = self.root / "api-unknown"
        with self.assertRaisesRegex(ValueError, "Unknown game profile"):
            generate(target, 42, profile="unknown")
        self.assertFalse(target.exists())

    def assert_dev_pins(self, *flags):
        for randomized, expected in DEV_PINS.items():
            with self.subTest(randomized=randomized):
                args = (*flags, "--random") if randomized else flags
                directory = self.record(f"dev-{bool(flags)}-{randomized}", 17, *args)
                actual = tuple(digest(directory / name) for name in ("recording.mp4", "ground_truth.json"))
                self.assertEqual(actual, expected)

    def test_holdouts_vary(self):
        a, b = (dimensions(read_truth(self.record(f"holdout-{seed}", seed, "--profile", "holdout")))
                for seed in (42, 83))
        differences = [name for name in a if a[name] != b[name]]
        self.assertGreaterEqual(len(differences), 5, differences)

    def test_holdout_determinism(self):
        first = self.record("holdout-42", 42, "--profile", "holdout")
        second = self.record("holdout-repeat", 42, "--profile", "holdout")
        self.assertEqual({p.relative_to(first): digest(p) for p in first.rglob("*") if p.is_file()},
                         {p.relative_to(second): digest(p) for p in second.rglob("*") if p.is_file()})

    def test_holdout_truth_consistency(self):
        from demo.synth_game.ground_truth import check_recording

        for seed in HOLDOUT_SEEDS:
            with self.subTest(seed=seed):
                check_recording(self.record(f"holdout-{seed}", seed, "--profile", "holdout"))

    def test_holdout_has_unseen_cells_outside_every_camera_window(self):
        for seed in HOLDOUT_SEEDS:
            with self.subTest(seed=seed):
                truth = read_truth(self.record(f"holdout-{seed}", seed, "--profile", "holdout"))
                width, height = truth["video"]["width"], truth["video"]["height"]
                tile = truth["level"]["tile_px"]
                cameras = {(frame["camera_x_px"], frame["camera_y_px"]) for frame in truth["frames"]}
                self.assertTrue(cameras)
                outside = set()
                for y, row in enumerate(truth["level"]["rows"]):
                    for x in range(len(row)):
                        if all((x + 1) * tile <= left or x * tile >= left + width
                               or (y + 1) * tile <= top or y * tile >= top + height
                               for left, top in cameras):
                            outside.add(f"{x},{y}")
                self.assertGreaterEqual(len(outside), 8)
                cells = truth["cells"]
                for key in outside:
                    self.assertIn(key, cells)
                    self.assertIsNone(cells[key], key)
                self.assertEqual(truth["unseen_cell_count"], sum(value is None for value in cells.values()))

    def test_truth_checker_rejects_shifted_hud(self):
        from demo.synth_game.ground_truth import check_recording

        directory = self.record("holdout-corrupted", 42, "--profile", "holdout")
        truth = read_truth(directory)
        truth["frames"][0]["widgets"]["hud"]["bbox_px"][0] += 1
        (directory / "ground_truth.json").write_text(json.dumps(truth), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "widget geometry"):
            check_recording(directory)


if __name__ == "__main__":
    unittest.main()
