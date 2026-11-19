# SPDX-License-Identifier: AGPL-3.0-only
"""Regression checks for wide-v1 fixture geometry and the custody handoff."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from demo.synth_game import wide_truth
from demo.synth_game.game import HEIGHT, TILE, WIDTH
from demo.synth_game.generate import generate
from demo.synth_game.wide import PROFILE, atlas, terrain, timeline


class WideFixture(unittest.TestCase):
    def test_seeded_strata_and_variation(self):
        references = []
        for seed in (0, 17, 42, 83, 1 << 127):
            with self.subTest(seed=seed):
                truth = wide_truth.build_truth(seed)
                wide_truth.require_coverage(truth)
                self.assertEqual(truth["strata_counts"]["sprite_sheet"], 4)
                self.assertEqual(sum(truth["visibility_counts"].values()),
                                 sum(len(row) for row in truth["level"]["rows"]))
                self.assertEqual({key for key, value in truth["cells"].items() if value is None},
                                 {key for key, state in truth["visibility"].items() if state == wide_truth.NEVER})
                references.append(truth)
        self.assertGreater(len({len(ref["level"]["rows"]) for ref in references}), 1)
        self.assertGreater(len({tuple(ref["sky"]["gradient_rgb"][0]) for ref in references}), 1)
        self.assertGreater(len({tuple(ref["frames"][0]["camera_xy"]) for ref in references}), 1)

    def test_exact_slope_and_clip_masks(self):
        seed = 17
        rows = terrain(seed)
        tiles, _, _ = atlas(seed)
        self.assertEqual(list(tiles["/"].getchannel("A").getdata()).count(255), TILE * (TILE + 1) // 2)
        self.assertEqual(list(tiles["\\"].getchannel("A").getdata()).count(255), TILE * (TILE + 1) // 2)
        frames = list(timeline(seed))
        for index, key in ((2, "one_pixel_column"), (3, "one_pixel_row")):
            frame = frames[index]
            raster, cells, counts = wide_truth.frame_geometry(frame, rows)
            x, y = frame.camera
            self.assertEqual(cells[3, 3], wide_truth.PARTIAL)
            self.assertGreater(counts[key], 0)
            seen = {(px, py) for py, bits in enumerate(raster) for px in range(WIDTH) if bits >> px & 1}
            clip = {(px, py)
                    for px in range(max(0, 3 * TILE - x), min(WIDTH, 4 * TILE - x))
                    for py in range(max(0, 3 * TILE - y), min(HEIGHT, 4 * TILE - y))}
            self.assertEqual(len(clip), TILE)
            self.assertTrue(clip <= seen)

    def test_missing_slope_is_refused(self):
        old_rows = terrain(17)
        flat_rows = tuple(row.replace("/", "#").replace("\\", "#") for row in old_rows)
        with patch.object(wide_truth, "terrain", return_value=flat_rows):
            with self.assertRaisesRegex(ValueError, "rising_slope, falling_slope"):
                wide_truth.require_coverage(wide_truth.build_truth(17))

    def test_observation_and_private_truth_handoff(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            public = root / "public"
            generate(public, 17, profile=PROFILE)
            self.assertEqual({path.name for path in public.iterdir()},
                             {"frames", "inputs.jsonl", "recording.mp4"})
            rows = [json.loads(line) for line in (public / "inputs.jsonl").read_text().splitlines()]
            self.assertTrue(all(set(row) == {"frame_index", "pts_ms", "steps"} for row in rows))
            self.assertTrue(all(set(action) == {"tick", "sample"} for row in rows for action in row["steps"]))
            with self.assertRaisesRegex(ValueError, "outside the observation packet"):
                wide_truth.publish_truth(public, 17, public / "truth.json")

            private = root / "truth.json"
            wide_truth.publish_truth(public, 17, private)
            self.assertEqual(private.stat().st_mode & 0o777, 0o600)
            truth = json.loads(private.read_text())
            self.assertEqual(truth["schema"], wide_truth.TRUTH_SCHEMA)
            self.assertEqual(truth["seed"], 17)
            self.assertEqual(truth["frames"][0]["camera_xy"], list(next(timeline(17)).camera))
            self.assertEqual(truth["video"]["sha256"], truth["observation_sha256"]["recording.mp4"])
            with self.assertRaises(FileExistsError):
                wide_truth.publish_truth(public, 17, private)
            with self.assertRaisesRegex(ValueError, "observation .*do(?:es)? not match|observation digest mismatch"):
                wide_truth.regenerate_truth(public, 83, root)
            movie = public / "recording.mp4"
            data = movie.read_bytes()
            movie.write_bytes(bytes([data[0] ^ 1]) + data[1:])
            with self.assertRaisesRegex(ValueError, "observation digest mismatch: recording.mp4"):
                wide_truth.regenerate_truth(public, 17, root)
