# SPDX-License-Identifier: AGPL-3.0-only
"""Independent synthetic oracle. Only this test reads the admitted dev labels."""
from copy import deepcopy
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from game.scene import reconstruct, validate_scene
from game.scene.raster import hud_mask
from source.records import canonical, digest


DEV_PINS = {
    "normal": {
        "recording.mp4": "5319f50b9fcd49b59668810b760f06f3f0687036fe49d3e9a44eb3c1ed1c4267",
        "ground_truth.json": "6338e49efdaefd6dcac1f95a548327a14db7fd2a7688e564032ca963cb8c53d8",
        "inputs.jsonl": "677560d88e599bd1d5c4f86da74698b75efee53de10353f17e1fd847f6ed9b02",
    },
    "random": {
        "recording.mp4": "9daf3e8b3aafef2047dfb79db8684e7ea9176ade872f8eb4b58a774cfa0e9cf6",
        "ground_truth.json": "9f2c3351da1d0098231cb0f556c13f5ab5cdd13e7f1d7010b80bf42e5cc2d334",
        "inputs.jsonl": "6afdc5f1f9add593134e858ae98d05f5ec3d96ab86802647dcf13089b6c05493",
    },
}


def load_dev_arm(arm, decoded, inputs):
    pins = DEV_PINS[arm]
    data = {}
    for name, expected_digest in pins.items():
        data[name] = (inputs / name).read_bytes()
        if digest(data[name]) != expected_digest:
            raise ValueError(f"dev input digest mismatch: {name}")
    scene = reconstruct(decoded)
    if scene["video_sha256"] != pins["recording.mp4"]:
        raise ValueError("dev decoded video digest mismatch")
    return decoded, json.loads(data["ground_truth.json"]), scene


def iou(actual, expected):
    union = actual | expected
    if not union:
        raise AssertionError("empty IoU denominator")
    return len(actual & expected) / len(union)


@lru_cache(maxsize=2)
def hud_pixels(panel):
    image = Image.new("1", (192, 112))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 191, 15), fill=1)
    if panel:
        draw.rounded_rectangle((31, 34, 160, 78), radius=4, fill=1)
    return {i for i, value in enumerate(image.getdata()) if value}


def visible_tiles(frame, truth, mask):
    level = truth["level"]
    tile = level["tile_px"]
    x, y = frame["camera_x_px"], frame["camera_y_px"]
    video = truth["video"]
    width, height = video["width"], video["height"]
    for row_index, row in enumerate(level["rows"]):
        for col, code in enumerate(row):
            sx, sy = col * tile - x, row_index * tile - y
            if not (0 <= sx <= width - tile and 0 <= sy <= height - tile):
                continue
            if any(py * width + px in mask for py in range(sy, sy + tile) for px in range(sx, sx + tile)):
                continue
            yield row_index, col, code


def score(scene, truth):
    frames = truth["frames"]
    actual_cuts = [shot["observations"][0]["frame_index"] for shot in scene["shots"][1:]]
    cut_set = [i for i in range(1, len(frames)) if frames[i]["screen"] != frames[i - 1]["screen"]]
    if actual_cuts != cut_set:
        raise AssertionError(f"observed cuts {actual_cuts} differ from truth {cut_set}")
    resets = [row["frame_index"] for shot in scene["shots"] for row in shot["observations"]
              if row.get("camera_reset")]
    respawns = {event["frame_index"] for event in truth["events"] if event["kind"] == "respawn"
                and frames[event["frame_index"] - 1]["camera_x_px"] !=
                frames[event["frame_index"]]["camera_x_px"]}
    if set(resets) - set(actual_cuts) != respawns:
        raise AssertionError(f"observed camera resets {resets} differ from truth respawns {sorted(respawns)}")
    errors, hud_ious, map_ious = [], [], []
    for shot in scene["shots"]:
        level = shot["level_map"]
        expected = set()
        per_frame = {}
        for sample, observation in zip(level["camera_samples"], shot["observations"], strict=True):
            frame = frames[observation["frame_index"]]
            if sample["provenance"]["pts_ms"] != frame["pts_ms"]:
                raise AssertionError("camera PTS mismatch")
            errors.append(math.dist(sample["offset_px"], [frame["camera_x_px"], frame["camera_y_px"]]))
            mask = hud_pixels(frame["screen"] != "play")
            predicted = {i for i, value in enumerate(hud_mask(observation["hud"]).flat) if value}
            hud_ious.append(iou(predicted, mask))
            visible = set(visible_tiles(frame, truth, mask))
            per_frame[frame["frame_index"]] = visible
            expected.update(visible)
        actual = {(r, c, code) for r, row in enumerate(level["tiles"]["grid"])
                  for c, code in enumerate(row) if code is not None}
        witnesses = {(w["row"], w["column"]): w for w in shot["tile_witnesses"]}
        full = set()
        for (row, col), witness in witnesses.items():
            index = witness["frame_index"]
            if "visible_xywh" in witness:
                if truth["level"]["rows"][row][col] != "#":
                    raise AssertionError(f"partial solid {(row, col)} disagrees with truth")
                if level["tiles"]["grid"][row][col] is not None:
                    raise AssertionError(f"partial solid {(row, col)} has a tile label")
                x, y, w, h = witness["visible_xywh"]
                frame = frames[index]
                size = truth["level"]["tile_px"]
                sx, sy = col * size - frame["camera_x_px"], row * size - frame["camera_y_px"]
                if not (sx <= x <= sx + size - w and sy == y and h == size):
                    raise AssertionError(f"partial solid {(row, col, code)} has no support in witness {index}")
            else:
                code = level["tiles"]["grid"][row][col]
                if (row, col, code) not in per_frame[index]:
                    raise AssertionError(f"tile {(row, col, code)} has no visible footprint in witness {index}")
                full.add((row, col, code))
        if actual != full:
            raise AssertionError("complete witnesses do not match labelled tiles")
        map_ious.append(iou(full, expected))
    if len(errors) != len(frames):
        raise AssertionError("camera coverage mismatch")
    return {"frames": len(frames), "p95_px": sorted(errors)[math.ceil(.95 * len(errors)) - 1],
            "hud_iou_min": min(hud_ious), "map_iou_min": min(map_ious), "map_ious": map_ious,
            "cuts": actual_cuts, "camera_resets": resets}


def assert_gates(test, metrics):
    test.assertLessEqual(metrics["p95_px"], 4, "camera p95 exceeds 4 px")
    test.assertGreaterEqual(metrics["hud_iou_min"], .90, "HUD mask IoU below .90")
    test.assertGreaterEqual(metrics["map_iou_min"], .90, "visible map IoU below .90")


@unittest.skipUnless(os.environ.get("G1A_DEV_DECODED") and os.environ.get("G1A_DEV_TRUTH"),
                     "set both G1A development evidence roots to run the pinned dev oracle")
class DevTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs = {}
        for arm in ("normal", "random"):
            decoded = Path(os.environ["G1A_DEV_DECODED"]) / arm
            inputs = Path(os.environ["G1A_DEV_TRUTH"]) / arm
            cls.runs[arm] = load_dev_arm(arm, decoded, inputs)

    def test_development_gates(self):
        for arm, (_, truth, scene) in self.runs.items():
            with self.subTest(arm=arm):
                metrics = score(scene, truth)
                print(f"[MEASURED] {arm} {json.dumps(metrics, sort_keys=True)}", flush=True)
                assert_gates(self, metrics)

    def test_dev_pins_reject_changed_inputs_and_decoded_video(self):
        decoded, _, scene = self.runs["normal"]
        inputs = Path(os.environ["G1A_DEV_TRUTH"]) / "normal"
        read_bytes = Path.read_bytes
        for name in ("recording.mp4", "ground_truth.json", "inputs.jsonl"):
            with self.subTest(name=name):
                def changed(path):
                    data = read_bytes(path)
                    return data + b"modified" if path == inputs / name else data

                with patch.object(Path, "read_bytes", changed):
                    with self.assertRaisesRegex(ValueError, f"dev input digest mismatch: {name}"):
                        load_dev_arm("normal", decoded, inputs)
        with patch(f"{__name__}.reconstruct", return_value=dict(scene, video_sha256="0" * 64)):
            with self.assertRaisesRegex(ValueError, "dev decoded video digest mismatch"):
                load_dev_arm("normal", decoded, inputs)

    def test_reconstruction_is_deterministic(self):
        decoded, _, scene = self.runs["normal"]
        self.assertEqual(canonical(scene), canonical(reconstruct(decoded)))

    def test_truth_plus_twelve_fails_camera_gate(self):
        _, truth, scene = self.runs["normal"]
        shifted = deepcopy(scene)
        for shot in shifted["shots"]:
            for sample in shot["level_map"]["camera_samples"]:
                index = sample["provenance"]["frame_index"]
                sample["offset_px"][0] = truth["frames"][index]["camera_x_px"] + 12
        metrics = score(shifted, truth)
        print(f"[MEASURED] truth +12 px mutant p95={metrics['p95_px']} px", flush=True)
        self.assertGreaterEqual(metrics["p95_px"], 12)
        with self.assertRaisesRegex(AssertionError, "camera p95 exceeds"):
            assert_gates(self, metrics)

    def test_visibility_gates_reject_subthreshold_scores(self):
        nominal = {"p95_px": 0, "hud_iou_min": 1, "map_iou_min": 1}
        for metric, message in (("hud_iou_min", "HUD mask IoU below"),
                                ("map_iou_min", "visible map IoU below")):
            with self.subTest(metric=metric):
                with self.assertRaisesRegex(AssertionError, message):
                    assert_gates(self, dict(nominal, **{metric: .89}))

    def test_cut_membership_is_shot_local(self):
        _, _, scene = self.runs["normal"]
        corrupted = deepcopy(scene)
        witness = corrupted["shots"][1]["tile_witnesses"][0]
        witness["frame_index"] = corrupted["shots"][0]["observations"][0]["frame_index"]
        with self.assertRaisesRegex(ValueError, "another shot"):
            validate_scene(corrupted)
