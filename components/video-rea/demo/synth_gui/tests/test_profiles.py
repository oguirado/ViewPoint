# SPDX-License-Identifier: AGPL-3.0-only

import contextlib
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageColor, ImageFont

from demo.synth_gui import generate, ground_truth


DEV_HASHES = {
    42: (
        "2fec3707284d5a218c9a322c736b429736a79e45185d5cc315f0dc05ea562f28",
        "28bbd78cfee16048b1f7e8421bf0bafb579a15968c7214d4ac426f28fb20f2dd",
    ),
    43: (
        "40934713ed3932f4d765e8cbc1457737c0ab2809ac938b50b908f0f8beb62fe7",
        "51d4fffa2eb92ac16190ec3d8fb426207953e10c26e451d20fba351288224488",
    ),
}
OUTPUTS = ("recording.mp4", "ground_truth.json")


def hashes(out):
    return tuple(hashlib.sha256((out / name).read_bytes()).hexdigest() for name in OUTPUTS)


def record(out, seed, profile=None):
    args = ["--out", str(out), "--seed", str(seed)]
    if profile is not None:
        args += ["--profile", profile]
    generate.main(args)
    return json.loads((out / "ground_truth.json").read_text(encoding="utf-8"))


def dimensions(truth):
    appearance = truth.get("appearance", {})
    first = {
        screen: next(frame for frame in truth["frames"] if frame["screen_id"] == screen)
        for screen in truth["transition_graph"]["screens"]
    }
    main = {widget["id"]: widget for widget in first["main"]["widgets"]}
    width, height = (truth["video"][key] for key in ("width", "height"))
    list_x, _, list_w, _ = main["project_list"]["bbox"]
    return {
        "theme": appearance.get("theme"),
        "palette": appearance.get("palette"),
        "font_size": appearance.get("font_size_px"),
        "window_size": (width, height),
        "widget_positions": (list_x * 100 // width, list_w * 100 // width),
        "widget_counts": len(main),
        "text_content": main["app_title"]["text"],
        "screen_order": tuple(first),
        "dialog_style": appearance.get("dialog_style"),
        "guards": next(edge["guard"] for edge in truth["transition_graph"]["edges"]
                       if edge["target"] == "save"),
    }


class ProfileTests(unittest.TestCase):
    def output_root(self):
        return Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_dev_matches_pre_change_hashes(self):
        root = self.output_root()
        for seed, expected in DEV_HASHES.items():
            for profile in (None, "dev"):
                with self.subTest(seed=seed, profile=profile):
                    out = root / f"{seed}-{profile}"
                    record(out, seed, profile)
                    self.assertEqual(hashes(out), expected)

    def test_holdout_seeds_vary_at_least_five_dimensions(self):
        root = self.output_root()
        first, second = (dimensions(record(root / str(seed), seed, "holdout"))
                         for seed in (0, 1))
        changed = [name for name in first if first[name] != second[name]]
        self.assertGreaterEqual(len(changed), 5, changed)

    def test_holdout_seed_is_byte_identical_and_has_real_vfr(self):
        root = self.output_root()
        truth = record(root / "first", -29, "holdout")
        record(root / "repeat", -29, "holdout")
        self.assertEqual(hashes(root / "first"), hashes(root / "repeat"))
        timing = ground_truth.probe_timing(root / "first" / "recording.mp4")
        ground_truth.validate_timing(truth, timing)
        self.assertGreater(len({b-a for a, b in zip(timing.pts_ms, timing.pts_ms[1:])}), 1)

    def test_unknown_profile_is_rejected(self):
        out = self.output_root() / "invalid"
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            record(out, 0, "invalid")
        self.assertEqual(raised.exception.code, 2)
        with self.assertRaisesRegex(ValueError, "Unknown synthetic GUI profile"):
            generate.generate(out, 0, profile="invalid")
        self.assertFalse(out.exists())

    def test_twenty_holdouts_pass_truth_consistency(self):
        signatures = []
        for seed in range(20):
            with self.subTest(seed=seed), tempfile.TemporaryDirectory() as temporary:
                truth = record(Path(temporary), seed, "holdout")
                self.assertEqual(truth["profile"], "holdout")
                ground_truth.validate_ground_truth(truth)
                saves = [action["frame_index"] for action in truth["actions"]
                         if action["target"] == "save" and action["kind"] == "click"]
                self.assertEqual([truth["frames"][index]["screen_id"] for index in saves],
                                 ["detail", "detail", "main"])
                short = next(w["text"] for w in truth["frames"][saves[1]-1]["widgets"]
                             if w["id"] == "notes")
                self.assertTrue(0 < len(short) < truth["note_min_length"])
                signatures.append(dimensions(truth))
        if signatures:
            for name in signatures[0]:
                with self.subTest(dimension=name):
                    self.assertGreater(len({json.dumps(row[name], sort_keys=True)
                                            for row in signatures}), 1)

    def test_appearance_matches_the_rendered_frame(self):
        out = self.output_root() / "raster"
        truth = record(out, 0, "holdout")
        appearance = truth["appearance"]
        first = truth["frames"][0]
        width, height = (truth["video"][key] for key in ("width", "height"))
        palette = appearance["palette"]
        with Image.open(out / first["path"]) as image:
            self.assertEqual(image.size, (width, height))
            self.assertEqual(image.getpixel((0, 0)), ImageColor.getrgb(palette["chrome"]))
            self.assertEqual(image.getpixel((0, height-1)), ImageColor.getrgb(palette["main"]))
        title = next(w for w in first["widgets"] if w["id"] == "app_title")
        font = ImageFont.load_default(size=appearance["font_size_px"])
        left, top, right, bottom = font.getmask(title["text"], mode="L").getbbox()
        self.assertEqual(title["text_bbox"][2:], [right-left, bottom-top])
        settings = next(frame for frame in truth["frames"] if frame["screen_id"] == "settings")
        dialog = next(w for w in settings["widgets"] if w["id"] == "settings_dialog")
        x, y, w, h = dialog["bbox"]
        scrim = appearance["dialog_style"]["scrim_percent"]
        with Image.open(out / settings["path"]) as image:
            self.assertEqual(image.getpixel((x+w-8, y+h//2)), ImageColor.getrgb(palette["dialog"]))
            self.assertEqual(image.getpixel((0, height-1)),
                             tuple(channel * scrim // 100 for channel in ImageColor.getrgb(palette["main"])))


class HoldoutTruthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(cls.enterClassContext(tempfile.TemporaryDirectory()))
        cls.truth = record(root / "holdout", 0, "holdout")

    def setUp(self):
        self.broken = copy.deepcopy(self.truth)

    def widget(self, widget_id, frame=0):
        return next(widget for widget in self.broken["frames"][frame]["widgets"]
                    if widget["id"] == widget_id)

    def save_sources(self):
        return [action["frame_index"] - 1 for action in self.broken["actions"]
                if action["target"] == "save" and action["kind"] == "click"]

    def test_checker_rejects_corrupt_text_geometry(self):
        widget = next(widget for widget in self.broken["frames"][0]["widgets"]
                      if widget["id"] == "file_menu")
        widget["text_bbox"][0] = 0
        with self.assertRaisesRegex(ValueError, "text bbox"):
            ground_truth.validate_ground_truth(self.broken)

    def test_checker_rejects_out_of_frame_slot(self):
        self.widget("file_menu")["slot_bbox"][0] = -1
        with self.assertRaisesRegex(ValueError, "slot outside"):
            ground_truth.validate_ground_truth(self.broken)

    def test_checker_rejects_inconsistent_widget_bbox(self):
        self.widget("file_menu")["bbox"][0] += 1
        with self.assertRaisesRegex(ValueError, "differs from painted geometry"):
            ground_truth.validate_ground_truth(self.broken)

    def test_checker_rejects_inconsistent_widget_links(self):
        self.widget("project")["parent_id"] = "missing"
        with self.assertRaisesRegex(ValueError, "widget links"):
            ground_truth.validate_ground_truth(self.broken)

    def test_checker_rejects_bad_guard_threshold(self):
        self.broken["note_min_length"] = 0
        with self.assertRaisesRegex(ValueError, "Invalid holdout note guard"):
            ground_truth.validate_ground_truth(self.broken)

    def test_checker_rejects_save_button_state(self):
        self.widget("save", self.save_sources()[0])["enabled"] = True
        with self.assertRaisesRegex(ValueError, "Save button"):
            ground_truth.validate_ground_truth(self.broken)

    def test_checker_rejects_guard_violation_in_transition(self):
        short, accepted = self.save_sources()[1:]
        self.widget("notes", accepted)["text"] = self.widget("notes", short)["text"]
        self.widget("save", accepted)["enabled"] = False
        with self.assertRaisesRegex(ValueError, "Screen change violates its guard"):
            ground_truth.validate_ground_truth(self.broken)

    def test_checker_rejects_guard_violation_on_save_click(self):
        short, accepted = self.save_sources()[1:]
        self.widget("notes", short)["text"] = self.widget("notes", accepted)["text"]
        self.widget("save", short)["enabled"] = True
        with self.assertRaisesRegex(ValueError, "Save click"):
            ground_truth.validate_ground_truth(self.broken)

    def test_checker_rejects_bad_frame_duration(self):
        self.broken["frames"][0]["duration_ms"] += 1
        with self.assertRaisesRegex(ValueError, "duration differs"):
            ground_truth.validate_ground_truth(self.broken)
