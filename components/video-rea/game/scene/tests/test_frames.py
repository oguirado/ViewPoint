# SPDX-License-Identifier: AGPL-3.0-only
from fractions import Fraction
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from game.scene.frames import read_frames
from game.scene.raster import Raster
from game.scene.reconstruct import _stitch
from game.scene.validation import validate_scene
from source.records import canonical, digest


class FrameTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        with BytesIO() as data:
            with Image.new("RGB", (3, 2), "red") as png:
                png.save(data, format="PNG")
                self.rgb = digest(png.tobytes())
            self.data = data.getvalue()
        sha = digest(self.data)
        self.relative = f"artifacts/{sha}.png"
        self.path = self.root / self.relative
        self.path.parent.mkdir()
        self.path.write_bytes(self.data)
        base = {"numerator": 1, "denominator": 30000}
        video = "1" * 64
        self.manifest = {"schema": "vqa.frame/v1", "storage": "png_dir", "pixel_format": "rgb24",
                         "channels": 3, "width": 3, "height": 2, "count": 3,
                         "provenance": {"video_sha256": video, "time_base": base, "origin_pts": -1},
                         "frames": [{"i": i, "file": self.relative, "sha256": self.rgb,
                                     "file_sha256": sha, "t_us": t} for i, t in enumerate((0, 67, 67))]}
        self.clock = {"video_sha256": video, "time_base": base, "vision_origin_pts": -1,
                      "width": 3, "height": 2, "frame_count": 3,
                      "occurrences": [{"frame_index": i, "pts": t, "artifact_ref": f"af://sha256/{sha}",
                                       "rgb_sha256": self.rgb} for i, t in enumerate((-1, 1, 1))]}

    def read(self):
        (self.root / "frames.manifest.json").write_bytes(canonical(self.manifest))
        (self.root / "clock.json").write_bytes(canonical(self.clock))
        return list(read_frames(self.root))

    def test_retains_rational_pts_and_duplicate_png_occurrences(self):
        frames = self.read()
        self.assertEqual([f.index for f in frames], [0, 1, 2])
        self.assertEqual([f.ticks for f in frames], [-1, 1, 1])
        self.assertEqual(frames[-1].time_base, Fraction(1, 30000))
        self.assertEqual([f.provenance["pts_ms"] for f in frames], [0, 1 / 15, 1 / 15])
        self.assertEqual(frames[0].file_sha256, frames[-1].file_sha256)
        self.assertEqual(frames[-1].rgb_sha256, self.rgb)

    def test_positive_first_pts_uses_w1_zero_origin(self):
        self.clock["vision_origin_pts"] = self.manifest["provenance"]["origin_pts"] = 0
        for occurrence, row, ticks in zip(self.clock["occurrences"], self.manifest["frames"], (1, 3, 3)):
            occurrence["pts"] = ticks
            row["t_us"] = round(ticks * Fraction(1000000, 30000))
        self.assertEqual([f.provenance["pts_ms"] for f in self.read()], [1 / 30, .1, .1])

    def test_scene_checks_one_w1_origin_across_shots(self):
        first, second, third = self.read()
        raster = Raster((0, 0), (0, 0), False, {})
        scene = {
            "video_sha256": first.video_sha256, "time_base": self.clock["time_base"],
            "shots": [_stitch(0, [(first, raster, "recording_start")]),
                      _stitch(1, [(second, raster, "camera_discontinuity"), (third, raster, None)])],
        }
        self.assertEqual(validate_scene(scene), scene)
        level = scene["shots"][1]["level_map"]
        for evidence, sample in zip(level["evidence"], level["camera_samples"], strict=True):
            self.assertEqual(evidence["pts_ms"], 1 / 15)
            evidence["pts_ms"] = sample["provenance"]["pts_ms"] = 1 / 30
        with self.assertRaisesRegex(ValueError, "does not match its source occurrence"):
            validate_scene(scene)

    def test_metadata_guards(self):
        for case, reason in (
            ("missing", "invalid W1 frame source metadata"),
            ("storage", "expected a W1 RGB PNG directory"),
            ("counts", "W1 clock and manifest disagree"),
            ("digest syntax", "invalid W1 PNG digest"),
            ("nonmonotonic", "unjoined W1 occurrence"),
            ("origin", "W1 clock and manifest disagree"),
            ("timestamp", "unjoined W1 occurrence"),
            ("foreign path", "unjoined W1 occurrence"),
        ):
            with self.subTest(case=case):
                manifest, clock = canonical(self.manifest), canonical(self.clock)
                if case == "missing":
                    del self.clock["occurrences"]
                elif case == "storage":
                    self.manifest["storage"] = "video"
                elif case == "counts":
                    self.clock["frame_count"] -= 1
                elif case == "digest syntax":
                    self.manifest["frames"][0]["file_sha256"] = "../clock.json"
                elif case == "nonmonotonic":
                    self.clock["occurrences"][2]["pts"] = 0
                    self.manifest["frames"][2]["t_us"] = 33
                elif case == "origin":
                    self.clock["vision_origin_pts"] = self.manifest["provenance"]["origin_pts"] = -2
                    for row, t_us in zip(self.manifest["frames"], (33, 100, 100)):
                        row["t_us"] = t_us
                elif case == "timestamp":
                    self.manifest["frames"][1]["t_us"] = 0
                elif case == "foreign path":
                    self.manifest["frames"][0]["file"] = "ground_truth.json"
                try:
                    with self.assertRaisesRegex(ValueError, reason):
                        self.read()
                finally:
                    self.manifest, self.clock = json.loads(manifest), json.loads(clock)

    def test_file_digest_guard(self):
        self.path.write_bytes(self.data + b"modified")
        with self.assertRaisesRegex(ValueError, "W1 PNG digest mismatch"):
            self.read()

    def test_decoded_raster_digest_guard(self):
        self.clock["occurrences"][0]["rgb_sha256"] = self.manifest["frames"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "W1 decoded raster mismatch"):
            self.read()

    def test_symlink_is_not_an_admitted_raster(self):
        target = self.root / "outside-artifacts.png"
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "must not traverse symlinks"):
            self.read()
