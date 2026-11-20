# SPDX-License-Identifier: AGPL-3.0-only

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path


def file_hashes(directory):
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


class SyntheticGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from demo.synth_gui import generate, ground_truth

        cls.truth_checks = ground_truth
        root = Path(cls.enterClassContext(tempfile.TemporaryDirectory()))
        cls.run_a, cls.run_b, cls.other_seed = (root / name for name in ("a", "b", "other"))
        for directory, seed in ((cls.run_a, 42), (cls.run_b, 42), (cls.other_seed, 43)):
            generate.main(["--out", str(directory), "--seed", str(seed)])
        cls.truth = json.loads((cls.run_a / "ground_truth.json").read_text(encoding="utf-8"))
        cls.timing = ground_truth.probe_timing(cls.run_a / "recording.mp4")

    def assert_recordings_equal(self, left, right):
        self.assertEqual(file_hashes(left), file_hashes(right))

    def test_same_seed_is_byte_identical(self):
        self.assert_recordings_equal(self.run_a, self.run_b)

    def test_different_seed_rejects_byte_identity(self):
        with self.assertRaises(AssertionError):
            self.assert_recordings_equal(self.run_a, self.other_seed)
        self.assertNotEqual(
            file_hashes(self.run_a)["recording.mp4"],
            file_hashes(self.other_seed)["recording.mp4"],
        )

    def test_container_pts_match_all_labelled_frames(self):
        self.truth_checks.validate_timing(self.truth, self.timing)
        timestamps = self.timing.pts_ms
        gaps = {end - start for start, end in zip(timestamps, timestamps[1:])}
        self.assertGreater(len(gaps), 1)

    def test_shifted_frame_pts_are_rejected(self):
        broken = copy.deepcopy(self.truth)
        broken["frames"][1]["pts_ms"] += 5
        with self.assertRaisesRegex(ValueError, "PTS mismatch"):
            self.truth_checks.validate_timing(broken, self.timing)

    def test_ground_truth_is_consistent(self):
        self.truth_checks.validate_ground_truth(self.truth)
        frames = self.truth["frames"]
        self.assertTrue({"main", "detail", "settings", "confirm"} <= {
            frame["screen_id"] for frame in frames
        })
        self.assertGreaterEqual(len(self.truth["actions"]), 20)
        self.assertEqual({action["kind"] for action in self.truth["actions"]}, {
            "cursor_move", "click", "type",
        })
        widgets = [widget for frame in frames for widget in frame["widgets"]]
        self.assertTrue({"menu", "button", "text_field"} <= {widget["kind"] for widget in widgets})
        self.assertTrue(any(widget["kind"] == "text_field" and widget["text"] for widget in widgets))
        frame_files = file_hashes(self.run_a / "frames")
        self.assertEqual(
            {Path(frame["path"]).name: frame["frame_sha256"] for frame in frames},
            frame_files,
        )

    def test_dangling_transition_is_rejected(self):
        broken = copy.deepcopy(self.truth)
        broken["transition_graph"]["edges"][0]["to_screen"] = "missing"
        with self.assertRaisesRegex(ValueError, "unknown screen"):
            self.truth_checks.validate_ground_truth(broken)

    def test_action_at_end_of_video_is_rejected(self):
        broken = copy.deepcopy(self.truth)
        broken["actions"][-1]["pts_ms"] = broken["video"]["duration_ms"]
        with self.assertRaisesRegex(ValueError, "outside the video"):
            self.truth_checks.validate_ground_truth(broken)

    def test_graph_occurrence_drift(self):
        truth = copy.deepcopy(self.truth)
        truth["transition_graph"]["edges"][0]["frame_indices"].pop()
        with self.assertRaisesRegex(ValueError, "graph differs"):
            self.truth_checks.validate_ground_truth(truth)

    def test_pointer_misses_target(self):
        truth = copy.deepcopy(self.truth)
        truth["actions"][0]["pointer"] = [0, 0]
        truth["frames"][truth["actions"][0]["frame_index"]]["pointer"] = [0, 0]
        with self.assertRaisesRegex(ValueError, "misses its target"):
            self.truth_checks.validate_ground_truth(truth)

    def test_workflow_order_and_coverage(self):
        for change in (lambda ids: ids.reverse(), lambda ids: ids.pop()):
            truth = copy.deepcopy(self.truth)
            change(truth["workflows"][0]["action_ids"])
            with self.assertRaisesRegex(ValueError, "cover each action"):
                self.truth_checks.validate_ground_truth(truth)

    def test_frame_order(self):
        truth = copy.deepcopy(self.truth)
        truth["frames"][-1]["pts_ms"] = truth["frames"][-2]["pts_ms"]
        with self.assertRaisesRegex(ValueError, "PTS order"):
            self.truth_checks.validate_ground_truth(truth)

    def test_frame_count(self):
        from dataclasses import replace
        timing = replace(self.timing, pts_ms=self.timing.pts_ms[:-1])
        with self.assertRaisesRegex(ValueError, "frame count"):
            self.truth_checks.validate_timing(self.truth, timing)

    def test_duplicate_widget_id(self):
        truth = copy.deepcopy(self.truth)
        truth["frames"][0]["widgets"].append(truth["frames"][0]["widgets"][0])
        with self.assertRaisesRegex(ValueError, "Duplicate widget"):
            self.truth_checks.validate_ground_truth(truth)

    def test_widget_outside_frame(self):
        truth = copy.deepcopy(self.truth)
        truth["frames"][0]["widgets"][0]["bbox"][0] = -1
        with self.assertRaisesRegex(ValueError, "outside frame"):
            self.truth_checks.validate_ground_truth(truth)

    def test_nonzero_first_pts(self):
        truth = copy.deepcopy(self.truth)
        truth["frames"][0]["pts_ms"] = 1
        with self.assertRaisesRegex(ValueError, "PTS zero"):
            self.truth_checks.validate_ground_truth(truth)

    def test_action_frame_pts_disagree(self):
        truth = copy.deepcopy(self.truth)
        truth["actions"][0]["pts_ms"] += 1
        with self.assertRaisesRegex(ValueError, "does not match its frame"):
            self.truth_checks.validate_ground_truth(truth)

    def test_container_duration_is_independent(self):
        from dataclasses import replace
        from unittest.mock import patch
        from demo.synth_gui import generate
        bad = replace(self.timing, duration_ms=self.timing.duration_ms + 17)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(generate, "probe_timing", return_value=bad):
                with self.assertRaisesRegex(ValueError, "MP4 duration differs"):
                    generate.generate(Path(directory), self.truth["seed"])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_final_hold_packet_duration(self):
        from decimal import Decimal
        from demo.synth_gui.media import run_media_tool
        frames = self.truth["frames"]
        packets = json.loads(run_media_tool("ffprobe", [
            "-v", "error", "-select_streams", "v:0", "-show_entries",
            "packet=duration_time", "-of", "json", "recording.mp4",
        ], cwd=self.run_a))["packets"]
        duration = Decimal(packets[-1]["duration_time"]) * 1000
        self.assertGreater(duration, 1)
        self.assertEqual(duration, frames[-1]["duration_ms"])
        self.assertEqual(duration, self.truth["video"]["duration_ms"] - frames[-1]["pts_ms"])
        self.assertEqual(frames[-1]["frame_role"], "hold")
        self.assertEqual(frames[-1]["frame_sha256"], frames[-2]["frame_sha256"])

    def test_seed_changes_schedule_and_layout(self):
        other = json.loads((self.other_seed / "ground_truth.json").read_text())
        self.assertNotEqual([f["pts_ms"] for f in self.truth["frames"][:4]],
                            [f["pts_ms"] for f in other["frames"][:4]])
        self.assertNotEqual([f["id"] for f in self.truth["workflows"]],
                            [f["id"] for f in other["workflows"]])

        def project_box(truth):
            return next(w["bbox"] for w in truth["frames"][0]["widgets"] if w["id"] == "project")
        self.assertNotEqual(project_box(self.truth), project_box(other))

    def test_inter_action_pixels_and_labels(self):
        frames = self.truth["frames"]
        roles = {f["frame_role"] for f in frames}
        self.assertTrue({"action", "hold", "motion", "hover", "scroll"} <= roles)
        for a in self.truth["actions"]:
            i = a["frame_index"]
            self.assertEqual(frames[i]["frame_role"], "action")
            self.assertEqual(frames[i]["pointer"], a["pointer"])
            if a["kind"] == "cursor_move":
                motion = []
                j = i - 1
                while frames[j]["frame_role"] == "motion":
                    motion.append(frames[j])
                    j -= 1
                self.assertGreaterEqual(len(motion), 2)
                points = [frames[j]] + list(reversed(motion)) + [frames[i]]
                self.assertEqual(len({tuple(f["pointer"]) for f in points}), len(points))
                self.assertEqual(len({f["frame_sha256"] for f in points}), len(points))
                self.assertEqual(frames[i+1]["hover_target"], a["target"])
                self.assertNotEqual(frames[i+1]["frame_sha256"], frames[i]["frame_sha256"])
        for i, f in enumerate(frames):
            if f["frame_role"] == "hold":
                self.assertEqual(frames[i-1]["frame_sha256"], f["frame_sha256"])
            if f["frame_role"] == "scroll":
                self.assertEqual(f["pointer"], frames[i-1]["pointer"])
                self.assertGreater(f["scroll_offset_px"], frames[i-1]["scroll_offset_px"])
                project = lambda frame: next(w for w in frame["widgets"] if w["id"] == "project")
                self.assertLess(project(f)["bbox"][1], project(frames[i-1])["bbox"][1])

    def test_text_bbox_matches_raster_and_slot(self):
        from PIL import Image, ImageChops
        frame = self.truth["frames"][0]
        with Image.open(self.run_a / frame["path"]) as image:
            for widget in frame["widgets"]:
                if widget["kind"] != "text":
                    continue
                x, y, w, h = widget["slot_bbox"]
                slot = image.crop((x, y, x+w, y+h))
                bg = Image.new("RGB", slot.size, slot.getpixel((0, 0)))
                left, top, right, bottom = ImageChops.difference(slot, bg).getbbox()
                self.assertEqual(widget["bbox"], [x+left, y+top, right-left, bottom-top])
                self.assertEqual(widget["text_bbox"], widget["bbox"])

    def test_modal_parent_pixels_and_widget_links(self):
        from PIL import Image
        frames = self.truth["frames"]
        for modal, parent in (("confirm", "detail"), ("settings", "main")):
            f = next(f for f in frames if f["screen_id"] == modal)
            base = next(b for b in reversed(frames[:f["frame_index"]]) if b["screen_id"] == parent)
            self.assertEqual(f["parent_screen"], parent)
            with Image.open(self.run_a / base["path"]) as src, Image.open(self.run_a / f["path"]) as overlay:
                # Header includes title ink and empty chrome, away from the dialog and cursor.
                crop = (0, 0, 200, 42)
                original = src.crop(crop)
                actual = overlay.crop(crop)
                self.assertGreater(len(set(actual.getdata())), 2)
                self.assertEqual(original.point(lambda x: x // 2).tobytes(), actual.tobytes())
                crop = (24, 170, 90, 245)  # content specific to the parent, outside the overlay
                self.assertEqual(src.crop(crop).point(lambda x: x // 2).tobytes(),
                                 overlay.crop(crop).tobytes())
            labelled = {w["id"]: w for w in f["widgets"]}
            self.assertEqual(labelled["app_title"]["modal_occlusion"], "none")
            if modal == "confirm":
                self.assertEqual(labelled["notes"]["modal_occlusion"], "partial")
            else:
                self.assertEqual(labelled["scroll_down"]["modal_occlusion"], "full")
        for f in frames:
            widgets = {w["id"]: w for w in f["widgets"]}
            for w in widgets.values():
                if w["parent_id"] is not None:
                    self.assertIn(w["id"], widgets[w["parent_id"]]["child_ids"])
            if f["screen_id"] == "confirm":
                self.assertEqual(widgets["cancel_archive"]["parent_id"], "archive_dialog")
            if "settings_item" in widgets:
                self.assertEqual(widgets["settings_item"]["parent_id"], "file_popover")
            if "project" in widgets:
                self.assertEqual(widgets["project_list"]["kind"], "list")
                self.assertEqual(widgets["project"]["parent_id"], "project_list")

    def test_guard_outcomes_typed_delta_and_edge_causes(self):
        frames, actions = self.truth["frames"], self.truth["actions"]
        by_id = {a["id"]: a for a in actions}
        clicks = [a for a in actions if a["kind"] == "click" and a["target"] == "save"]
        self.assertEqual(len(clicks), 2)
        for click, enabled in zip(clicks, (False, True)):
            i = click["frame_index"]
            save = next(w for w in frames[i-1]["widgets"] if w["id"] == "save")
            self.assertIs(save["enabled"], enabled)
            self.assertEqual(frames[i]["screen_id"] != frames[i-1]["screen_id"], enabled)
        for a in actions:
            if a["kind"] == "type":
                i = a["frame_index"]
                def value(frame):
                    return next(w["text"] for w in frame["widgets"] if w["id"] == a["target"])
                self.assertEqual(value(frames[i-1]) + a["typed_text"], a["value_after"])
                self.assertEqual(value(frames[i]), a["value_after"])
        graph = self.truth["transition_graph"]
        for edge in graph["edges"]:
            self.assertEqual([by_id[k]["frame_index"] for k in edge["action_ids"]], edge["frame_indices"])
        save_edge = next(e for e in graph["edges"] if clicks[1]["id"] in e["action_ids"])
        self.assertEqual(save_edge["guard"], {"field": "notes", "predicate": "nonempty"})
        self.assertTrue(any(a["target"] == "confirm_archive" and not a["observed"]
                            for a in graph["alternatives"]["confirm"]))

    def test_reject_unwitnessed_screen_change(self):
        truth = copy.deepcopy(self.truth)
        frame = next(f for f in truth["frames"] if f["frame_role"] == "hover")
        frame["screen_id"] = "confirm"
        with self.assertRaisesRegex(ValueError, "matching authored exit"):
            self.truth_checks.validate_ground_truth(truth)

    def test_reject_guard_with_empty_source_field(self):
        truth = copy.deepcopy(self.truth)
        edge = next(e for e in truth["transition_graph"]["edges"] if e["guard"])
        before = truth["frames"][edge["frame_indices"][0]-1]
        next(w for w in before["widgets"] if w["id"] == "notes")["text"] = ""
        with self.assertRaisesRegex(ValueError, "violates its guard"):
            self.truth_checks.validate_ground_truth(truth)

    def test_reject_wrong_transition_action_id(self):
        truth = copy.deepcopy(self.truth)
        edge = truth["transition_graph"]["edges"][0]
        edge["action_ids"][0] = truth["actions"][0]["id"]
        with self.assertRaisesRegex(ValueError, "graph differs"):
            self.truth_checks.validate_ground_truth(truth)

    def test_reject_action_witness_mismatch(self):
        truth = copy.deepcopy(self.truth)
        truth["actions"][0]["id"] = "unrelated"
        with self.assertRaisesRegex(ValueError, "witness differs"):
            self.truth_checks.validate_ground_truth(truth)

    def test_reject_transition_without_click(self):
        truth = copy.deepcopy(self.truth)
        edge = truth["transition_graph"]["edges"][0]
        action = next(a for a in truth["actions"] if a["id"] == edge["action_ids"][0])
        action["kind"] = "cursor_move"
        with self.assertRaisesRegex(ValueError, "requires a click"):
            self.truth_checks.validate_ground_truth(truth)

    def test_reject_action_on_modal_backdrop(self):
        truth = copy.deepcopy(self.truth)
        action = next(a for a in truth["actions"] if a["target"] == "cancel_archive")
        action["target"] = "archive"
        with self.assertRaisesRegex(ValueError, "no visible target"):
            self.truth_checks.validate_ground_truth(truth)

    def test_reject_frame_with_missing_action(self):
        truth = copy.deepcopy(self.truth)
        truth["actions"].pop(0)
        with self.assertRaisesRegex(ValueError, "Missing action"):
            self.truth_checks.validate_ground_truth(truth)

    def test_action_fields_and_provenance(self):
        contracts = Path(__file__).resolve().parents[3] / "contracts"
        schema = json.loads((contracts / "rea.action_v1.schema.json").read_text())
        for action in self.truth["actions"]:
            record = {k: v for k, v in action.items() if k not in {"frame_index", "pts_ms"}}
            record["schema"] = schema["title"]
            self.assertLessEqual(set(schema["required"]), record.keys())
            self.assertLessEqual(record.keys(), set(schema["properties"]))
            self.assertIn(record["kind"], schema["properties"]["kind"]["enum"])
            frame = self.truth["frames"][action["frame_index"]]
            self.assertEqual(record["provenance"], {
                "video_sha256": self.truth["video"]["sha256"],
                "pts_ms": action["pts_ms"], "frame_index": frame["frame_index"],
                "frame_sha256": frame["frame_sha256"], "label": "observed",
            })

    def test_reject_swapped_action_frame_hash(self):
        truth = copy.deepcopy(self.truth)
        action = truth["actions"][0]
        action["provenance"]["frame_sha256"] = truth["frames"][0]["frame_sha256"]
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.truth_checks.validate_ground_truth(truth)
