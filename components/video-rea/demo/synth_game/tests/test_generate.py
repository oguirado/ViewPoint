# SPDX-License-Identifier: AGPL-3.0-only
"""Acceptance checks against published media, input logs and calibration labels."""
import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat

from demo.synth_game.generate import generate, main
from demo.synth_game.media import MEDIA_TIMEOUT_S, run_media_tool
from demo.synth_game.replay import check_trajectory
from demo.synth_game.timing import probe_timing, validate_timing


AUDIBLE = {"jump", "pickup", "damage", "death", "win", "lose"}
PHYSICS_FIELDS = (
    ("gravity_m_s2", "gravity_px_s2", 1.05),
    ("friction_m_s2", "friction_px_s2", 0),
    ("run_speed_m_s", "run_speed_px_s", 1.10),
    ("jump_impulse_m_s", "jump_impulse_px_s", 1.10),
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(root):
    return {str(p.relative_to(root)): digest(p) for p in root.rglob("*") if p.is_file()}


class GameRecording(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = tempfile.TemporaryDirectory()
        cls.addClassCleanup(scratch.cleanup)
        cls.root = Path(scratch.name)
        cls.movie = cls.root / "normal"
        generate(cls.movie, 17, False)
        cls.truth = json.loads((cls.movie / "ground_truth.json").read_text())
        cls.inputs = [json.loads(line) for line in (cls.movie / "inputs.jsonl").read_text().splitlines()]

    def test_determinism(self):
        reference = tree(self.movie)
        for label, seed in (("repeat", 17), ("another", 83)):
            other = self.root / label
            generate(other, seed, False)
            self.assertEqual(reference, tree(other))
        self.assertIs(self.truth["randomized"], False)
        self.assertIsNone(self.truth["spawn_seed"])
        main(["--out", str(self.root / "random"), "--seed", "17", "--random"])
        for label, seed in (("random-repeat", 17), ("random-other", 83)):
            generate(self.root / label, seed, True)
        a, b = self.root / "random", self.root / "random-other"
        self.assertEqual(tree(a), tree(self.root / "random-repeat"))
        self.assertNotEqual(digest(a / "recording.mp4"), digest(b / "recording.mp4"))
        random_truth = json.loads((a / "ground_truth.json").read_text())
        other_truth = json.loads((b / "ground_truth.json").read_text())
        self.assertNotEqual(digest(a / "recording.mp4"), digest(self.movie / "recording.mp4"))
        for truth, seed in ((random_truth, 17), (other_truth, 83)):
            self.assertIs(truth["randomized"], True)
            self.assertEqual(truth["spawn_seed"], seed)
            for enemy in ("walker", "firefly"):
                self.assertNotEqual(truth["enemy_spawns"][enemy], self.truth["enemy_spawns"][enemy])
        for enemy in ("walker", "firefly"):
            self.assertNotEqual(random_truth["enemy_spawns"][enemy], other_truth["enemy_spawns"][enemy])
        for directory, truth in ((a, random_truth), (b, other_truth)):
            inputs = [json.loads(line) for line in (directory / "inputs.jsonl").read_text().splitlines()]
            self.assertLessEqual(check_trajectory(truth, inputs), 0.5)

    def test_container_timing(self):
        timing = probe_timing(self.movie / "recording.mp4")
        validate_timing(self.truth, timing)
        self.assertGreater(len(set(b - a for a, b in zip(timing.pts_ms, timing.pts_ms[1:]))), 1)
        altered = copy.deepcopy(self.truth)
        altered["frames"][5]["pts_ms"] += 1
        validate_timing(altered, timing)
        altered["frames"][5]["pts_ms"] += Decimal("0.1")
        with self.assertRaisesRegex(ValueError, "PTS mismatch"):
            validate_timing(altered, timing)
        altered["frames"][5]["pts_ms"] = self.truth["frames"][5]["pts_ms"]
        altered["video"]["duration_ms"] += 1
        with self.assertRaisesRegex(ValueError, "duration"):
            validate_timing(altered, timing)
        with self.assertRaisesRegex(ValueError, "frame count"):
            validate_timing(self.truth, replace(timing, pts_ms=timing.pts_ms[:-1]))
        with self.assertRaisesRegex(ValueError, "PTS mismatch"):
            validate_timing(self.truth, replace(timing, pts_ms=(Decimal("NaN"), *timing.pts_ms[1:])))
        event_pts = {e["pts_ms"] for e in self.truth["events"]}
        quiet_gaps = [
            after - before for before, after in zip(timing.pts_ms, timing.pts_ms[1:])
            if before not in event_pts and after not in event_pts
        ]
        self.assertGreater(quiet_gaps.count(self.truth["physics"]["fixed_step_ms"]), 1)

    def test_publication_rejects_sub_millisecond_encoder_drift(self):
        def offset_pts(movie):
            actual = probe_timing(movie)
            return replace(actual, pts_ms=(actual.pts_ms[0] + Decimal("0.1"), *actual.pts_ms[1:]))

        target = self.root / "drift"
        with patch("demo.synth_game.generate.probe_timing", side_effect=offset_pts):
            with self.assertRaisesRegex(ValueError, "PTS mismatch"):
                generate(target, 17)
        self.assertFalse(target.exists())

    def test_independent_replay(self):
        self.assertLessEqual(check_trajectory(self.truth, self.inputs), 0.5)
        for world, screen, factor in PHYSICS_FIELDS:
            with self.subTest(field=world):
                altered = copy.deepcopy(self.truth)
                altered["physics"]["world"][world] *= factor
                altered["physics"]["screen"][screen] *= factor
                with self.assertRaisesRegex(ValueError, "trajectory error.*px"):
                    check_trajectory(altered, self.inputs)
            with self.subTest(field=screen):
                altered = copy.deepcopy(self.truth)
                altered["physics"]["screen"][screen] = 0
                with self.assertRaisesRegex(ValueError, "screen physics"):
                    check_trajectory(altered, self.inputs)

    def test_replay_checks_metadata_and_states(self):
        with self.assertRaisesRegex(ValueError, "empty comparison"):
            check_trajectory(dict(self.truth, frames=[]), [])
        altered = copy.deepcopy(self.truth)
        first = next(frame for frame in altered["frames"] if frame["screen"] == "play")
        first["entities"]["player"]["x_px"] = float("nan")
        with self.assertRaisesRegex(ValueError, "trajectory error.*nan"):
            check_trajectory(altered, self.inputs)
        frame_index = next(f["frame_index"] for f in self.truth["frames"] if f["screen"] == "play")
        for field in ("camera_x_px", "camera_y_px", "score", "lives"):
            with self.subTest(field=field):
                altered = copy.deepcopy(self.truth)
                altered["frames"][frame_index][field] += 1
                with self.assertRaisesRegex(ValueError, field):
                    check_trajectory(altered, self.inputs)
        for actor in ("player", "walker", "firefly", "star_west", "star_east"):
            with self.subTest(actor=actor):
                altered = copy.deepcopy(self.truth)
                altered["frames"][frame_index]["entities"][actor]["state"] = "invalid"
                with self.assertRaisesRegex(ValueError, f"{actor}.*state"):
                    check_trajectory(altered, self.inputs)

    def test_visible_entities_and_rules(self):
        truth = self.truth
        frames = truth["frames"]
        self.assertEqual(digest(self.movie / "recording.mp4"), truth["video"]["sha256"])
        self.assertEqual({f["screen"] for f in frames}, {"title", "play", "game_over", "win"})
        self.assertGreater(max(f["camera_x_px"] for f in frames), 0)
        self.assertGreater(len(truth["level"]["rows"][0]) * truth["level"]["tile_px"], truth["video"]["width"])
        self.assertEqual(len(frames), len(self.inputs))
        samples = {}
        prev_tick = -1
        for f, row in zip(frames, self.inputs):
            self.assertEqual((f["frame_index"], f["pts_ms"]), (row["frame_index"], row["pts_ms"]))
            self.assertEqual([s["tick"] for s in row["steps"]], list(range(prev_tick + 1, f["tick"] + 1)))
            prev_tick = f["tick"]
            samples[f["pts_ms"]] = f
            png = self.movie / f["path"]
            self.assertEqual(f["frame_sha256"], digest(png))
            self.assert_provenance(f, f)
            with Image.open(png) as img:
                self.assert_overlay(img, f)
                if f["screen"] == "play":
                    self.assert_scene_pixels(img, f)
        self.assertEqual(set(truth["rules"]["audible_events"]), AUDIBLE)
        expected_cues = [{"kind": e["kind"], "pts_ms": e["pts_ms"]}
                         for e in truth["events"] if e["kind"] in AUDIBLE]
        self.assertEqual({cue["kind"] for cue in expected_cues}, AUDIBLE)
        self.assertEqual(truth["audio_cues"], expected_cues)
        self.assertEqual(truth["audio_cue_pts_ms"], [cue["pts_ms"] for cue in expected_cues])
        for event in truth["events"]:
            frame = samples[event["pts_ms"]]
            self.assertEqual(event["frame_index"], frame["frame_index"])
            self.assertEqual(event["frame_sha256"], frame["frame_sha256"])
            self.assert_provenance(event, frame)
            if event["kind"] == "pickup":
                self.assertEqual(frame["score"] - frames[frame["frame_index"] - 1]["score"],
                                 truth["rules"]["pickup_points"])
            if event["kind"] == "damage":
                self.assertEqual(frames[frame["frame_index"] - 1]["lives"] - frame["lives"],
                                 truth["rules"]["damage_lives"])
        for kind, states in (("walker", {"left", "right"}), ("firefly", {"rise", "fall"}),
                             ("player", {"idle", "run", "rise", "fall", "dead"}),
                             ("star_west", {"available", "collected"}),
                             ("star_east", {"available", "collected"})):
            self.assertEqual({f["entities"][kind]["state"] for f in frames if kind in f["entities"]}, states)

    def assert_provenance(self, record, frame):
        self.assertEqual(record["provenance"], {
            "video_sha256": self.truth["video"]["sha256"], "label": "observed",
            **{key: frame[key] for key in ("frame_index", "frame_sha256", "pts_ms")},
        })

    def test_damage_keeps_contact_pose_until_respawn(self):
        frames = self.truth["frames"]
        events = self.truth["events"]
        respawn, = [e for e in events if e["kind"] == "respawn"]
        hit = frames[respawn["frame_index"] - 1]
        reset = frames[respawn["frame_index"]]
        damage, = [e for e in events if e["kind"] == "damage" and e["frame_index"] == hit["frame_index"]]
        self.assertIn(("death", hit["frame_index"]), [(e["kind"], e["frame_index"]) for e in events])
        rules = self.truth["rules"]
        self.assertEqual(hit["entities"]["player"]["state"], "dead")
        self.assertEqual(hit["lives"], reset["lives"])
        self.assertGreater(hit["lives"], 0)
        self.assertEqual(reset["tick"] - hit["tick"], 1)
        self.assertEqual(reset["entities"]["player"]["x_px"], rules["start_x_px"])
        player, hazard = hit["entities"]["player"], hit["entities"][damage["subject"]]
        radius = rules["enemy_radius_px"]
        self.assertLessEqual(abs(player["x_px"] - hazard["x_px"]), rules["player_half_width_px"] + radius)
        self.assertLessEqual(player["y_px"] - rules["player_height_px"], hazard["y_px"] + radius)
        self.assertLessEqual(hazard["y_px"] - radius, player["y_px"])

    def overlay_reference(self, frame, width):
        # Test the displayed text and modal independently of the renderer.
        ref = Image.new("RGB", (width, 79), "#22364b")
        draw = ImageDraw.Draw(ref)
        font = ImageFont.load_default(size=10)
        draw.text((4, 1), f"SCORE  {frame['score']:03d}", fill="#f4e9ce", font=font)
        draw.text((width - 55, 1), f"LIVES  {frame['lives']}", fill="#f4e9ce", font=font)
        if frame["screen"] != "play":
            title, subtitle = {
                "title": ("BEACON RUN", "Press start"),
                "game_over": ("GAME OVER", "Press start to retry"),
                "win": ("BEACON REACHED", "Course complete"),
            }[frame["screen"]]
            draw.rectangle((36, 39, 155, 73), fill="#182f42")
            draw.text((width // 2, 47), title, anchor="mm", fill="#ffcb69", font=font)
            draw.text((width // 2, 65), subtitle, anchor="mm", fill="#e3ece9", font=font)
        return ref

    def assert_overlay(self, img, frame):
        ref = self.overlay_reference(frame, img.width)
        regions = [(0, 0, img.width, 16)]
        if frame["screen"] != "play":
            regions.append((36, 39, 156, 74))
        for box in regions:
            with self.subTest(frame=frame["frame_index"], box=box):
                self.assertIsNone(ImageChops.difference(ref.crop(box), img.crop(box)).getbbox())

    def assert_scene_pixels(self, img, frame):
        camera = frame["camera_x_px"]
        entities = frame["entities"]
        for actor, rgb in (("walker", (237, 112, 90)), ("firefly", (190, 170, 255)),
                           ("star_west", (255, 203, 105)), ("star_east", (255, 203, 105))):
            x = round(entities[actor]["x_px"] - camera)
            y = round(entities[actor]["y_px"])
            if 0 <= x < img.width:
                with self.subTest(actor=actor, frame=frame["frame_index"]):
                    self.assertEqual(img.getpixel((x, y)) == rgb,
                                     entities[actor]["state"] != "collected")
        x = round(entities["player"]["x_px"] - camera)
        y = round(entities["player"]["y_px"])
        for dx, dy, inside in ((-4, -12, True), (4, -12, True), (0, -1, True),
                               (-5, -12, False), (5, -12, False), (0, -13, False), (0, 0, False)):
            if 0 <= x + dx < img.width and 16 <= y + dy < img.height:
                with self.subTest(player_at=(x + dx, y + dy), frame=frame["frame_index"]):
                    self.assertEqual(img.getpixel((x + dx, y + dy)) == (82, 235, 207), inside)
        floor = self.truth["rules"]["floor_y_px"]
        tile_px = self.truth["level"]["tile_px"]
        tiles = self.truth["level"]["rows"][-1]
        for sx in range(img.width):
            column, tx = divmod(sx + camera, tile_px)
            expected = tiles[column] == "#" and tx < tile_px - 1
            self.assertEqual(img.getpixel((sx, floor)) == (158, 197, 183), expected,
                             f"floor edge at screen x={sx}, camera={camera}")

    def test_decoded_recording(self):
        decoded = self.root / "decoded"
        decoded.mkdir()
        run_media_tool("ffmpeg", [
            "-nostdin", "-v", "error", "-threads", "1", "-i", str(self.movie / "recording.mp4"),
            "-fps_mode", "passthrough", "-threads", "1", "-start_number", "0", "%05d.png",
        ], cwd=decoded, timeout=MEDIA_TIMEOUT_S)
        frames = self.truth["frames"]
        self.assertEqual(len(list(decoded.glob("*.png"))), len(frames))

        def assert_player_at(frame, x_offset=0):
            player = frame["entities"]["player"]
            x = round(player["x_px"] - frame["camera_x_px"]) + x_offset
            y = round(player["y_px"]) - 3
            with Image.open(decoded / Path(frame["path"]).name) as img:
                rgb = img.getpixel((x, y))
            self.assertLess(max(abs(a - b) for a, b in zip(rgb, (82, 235, 207))), 55)

        play = [f for f in frames if f["screen"] == "play"]
        for frame in play:
            assert_player_at(frame)
        with self.assertRaises(AssertionError):
            assert_player_at(play[0], x_offset=16)

        def overlay_error(image, frame):
            box = (36, 39, 156, 74)
            diff = ImageChops.difference(image.crop(box), self.overlay_reference(frame, image.width).crop(box))
            return sum(ImageStat.Stat(diff).mean)

        screens = ("title", "game_over", "win")
        expected = {screen: next(f for f in frames if f["screen"] == screen) for screen in screens}
        for frame in (f for f in frames if f["screen"] in screens):
            with Image.open(decoded / Path(frame["path"]).name) as img:
                errors = {screen: overlay_error(img, label) for screen, label in expected.items()}
                self.assertLess(errors[frame["screen"]], 25, f"decoded overlay at frame {frame['frame_index']}")
                self.assertEqual(min(errors, key=errors.get), frame["screen"])

        def assert_player_alignment(img, frame):
            player = frame["entities"]["player"]
            x = round(player["x_px"] - frame["camera_x_px"])
            y = round(player["y_px"])
            box = (x - 7, y - 15, x + 8, y + 3)
            with Image.open(self.movie / frame["path"]) as source:
                ref = source.crop(box)

            def alignment_error(offset):
                dx, dy = offset
                shifted = (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)
                return sum(ImageStat.Stat(ImageChops.difference(img.crop(shifted), ref)).mean)

            self.assertEqual(min(((0, 0), (-2, 0), (2, 0), (0, -2), (0, 2)), key=alignment_error), (0, 0))

        with Image.open(decoded / Path(play[0]["path"]).name) as img:
            assert_player_alignment(img, play[0])
            for dx, dy in ((2, 0), (0, 2)):
                with self.subTest(dx=dx, dy=dy), self.assertRaises(AssertionError):
                    assert_player_alignment(ImageChops.offset(img, dx, dy), play[0])

    def test_output_preserved_on_retry(self):
        before = tree(self.movie)
        with self.assertRaisesRegex(ValueError, "empty output"):
            generate(self.movie, 83, True)
        self.assertEqual(before, tree(self.movie))
        target = self.root / "empty-target"
        target.mkdir()
        link = self.root / "link"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "empty output"):
            generate(link, 83, True)
        self.assertTrue(link.is_symlink())
        self.assertEqual(list(target.iterdir()), [])
