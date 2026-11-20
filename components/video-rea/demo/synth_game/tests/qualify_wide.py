# SPDX-License-Identifier: AGPL-3.0-only
"""Public writer-side qualification against the admitted custody scorers."""

import argparse
import copy
import json
import runpy
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageChops, ImageDraw

from demo.synth_game import wide_truth
from demo.synth_game.generate import digest, generate
from demo.synth_game.media import run_media_tool
from demo.synth_game.tests.test_holdout import DEV_PINS
from demo.synth_game.wide import PROFILE, atlas, terrain, timeline
from game.scene.calibration import calibrate
from game.scene.overlays import luminance, mode
from game.scene.reconstruct import MAX_PAN_PX_PER_SECOND


SCORER_PIN = "5f7c4648571d16af44f122494ac4881487aca1b3c558f298057b163a6c921889"
VISIBILITY_PIN = "b981d4ff046b83da7c1afe917741e8a87d4f8ffbc3eaf8559b0330b6e1742e11"
THRESHOLDS_PIN = "610a0cdef617f8ca3b8fbb6865469de62548cd67919559d285d2ebe5c04a2d0f"
CONTROLS = ("floor_only", "fixed_pan", "star_lattice", "modal_luma", "lost_slope",
            "changed_dev_pin", "missing_fully_visible", "invented_unseen")


def load_pinned(path, pin):
    if digest(path) != pin:
        raise ValueError("Custody scorer pin mismatch")
    return runpy.run_path(str(path))


def decoded_images(observations, root):
    decoded = root / "decoded"
    decoded.mkdir()
    run_media_tool("ffmpeg", ["-nostdin", "-v", "error", "-threads", "1", "-i",
                             str(observations / "recording.mp4"), "-fps_mode", "passthrough",
                             str(decoded / "%05d.png")], cwd=root, timeout=30)
    results = []
    for path in sorted(decoded.iterdir()):
        with Image.open(path) as image:
            results.append(np.array(image.convert("RGB")))
    return results


def mask_rectangles(mask):
    return wide_truth.rectangles([
        sum(1 << int(x) for x in np.flatnonzero(row)) for row in np.asarray(mask)
    ])


def fixture_prediction(seed, truth):
    # Paint a world atlas once, then project it with Pillow. This exercises
    # the truth writer's independent scanline clipping and HUD subtraction.
    rows, size = truth["level"]["rows"], truth["level"]["tile_px"]
    tiles, _, _ = atlas(seed)
    canvas = Image.new("L", (len(rows[0]) * size, len(rows) * size))
    for row, codes in enumerate(rows):
        for col, code in enumerate(codes):
            if code in tiles:
                canvas.paste(tiles[code].getchannel("A"), (col * size, row * size))
    predicted = {}
    width, height = truth["width"], truth["height"]
    for frame in timeline(seed):
        x, y = frame.camera
        hud = Image.new("L", (width, height))
        if frame.hud is not None:
            left, top, w, h = frame.hud
            ImageDraw.Draw(hud).rectangle((left, top, left + w - 1, top + h - 1), fill=255)
        terrain_mask = canvas.crop((x, y, x + width, y + height))
        predicted[str(frame.index)] = {
            "camera_xy": [x, y], "hud": mask_rectangles(hud),
            "map": mask_rectangles(ImageChops.subtract(terrain_mask, hud)),
        }
    return {"frames": predicted, "cells": truth["cells"], "cuts": truth["cuts"]}


def current_floor_only(truth, prediction):
    rows, tile = truth["level"]["rows"], truth["level"]["tile_px"]
    floor = next(y for y, row in enumerate(rows) if row.count("#") > len(row) // 2)
    for i, frame in prediction["frames"].items():
        floor_top = floor * tile - truth["frames"][int(i)]["camera_xy"][1]
        frame["map"] = [rect for rect in frame["map"] if floor_top <= rect[1] < floor_top + tile]
    prediction["cells"] = {key: code for key, code in prediction["cells"].items()
                           if code != "solid" or int(key.split(",")[1]) == floor}


def fixed_pan(truth, prediction):
    for i, frame in enumerate(truth["frames"]):
        if not i or i in truth["cuts"]:
            continue
        previous = prediction["frames"][str(i - 1)]["camera_xy"]
        dt = frame["pts_ms"] - truth["frames"][i - 1]["pts_ms"]
        budget = 1 + MAX_PAN_PX_PER_SECOND * dt / 1000
        prediction["frames"][str(i)]["camera_xy"] = [
            min(max(target, origin - budget), origin + budget)
            for target, origin in zip(frame["camera_xy"], previous, strict=True)
        ]


def star_lattice(images, prediction):
    calibrated = calibrate([SimpleNamespace(image=image) for image in images])
    y = 0
    for i, image in enumerate(images):
        measured = calibrated.measure(image, y)
        if measured.offset is not None:
            y = measured.offset[1]
        prediction["frames"][str(i)]["camera_xy"] = (list(measured.offset)
                                                       if measured.offset is not None else None)


def modal_luma(images, prediction, raster):
    # Use the RP6 luma conversion and its scalar sky tolerance. Keep camera,
    # cells and HUD correct so the map score isolates the sky assumption.
    modal = mode(np.stack([luminance(image) for image in images]))
    for i, image in enumerate(images):
        frame = prediction["frames"][str(i)]
        height, width = image.shape[:2]
        hud = raster(frame["hud"], width, height)
        mask = np.abs(luminance(image) - modal) > 17
        frame["map"] = wide_truth.rectangles([
            sum(1 << int(x) for x in np.flatnonzero(row)) & ~occlusion
            for row, occlusion in zip(mask, hud, strict=True)
        ])


def require_dev_pin(directory):
    actual = tuple(digest(directory / name) for name in ("recording.mp4", "ground_truth.json"))
    if actual != DEV_PINS[False]:
        raise ValueError("Pinned dev fixture digest mismatch")


def require_refusal(action, expected):
    try:
        action()
    except ValueError as exc:
        if expected in str(exc):
            return expected
        raise
    raise AssertionError(f"Negative control accepted: {expected}")


def run_case(index, root, legacy, accepted, gates):
    seed = 8100 + index
    observations = root / "observations"
    generate(observations, seed, profile=PROFILE)
    truth = wide_truth.regenerate_truth(observations, seed, root)
    oracle = wide_truth.scorer_input(truth, legacy["raster"])
    nominal = fixture_prediction(seed, truth)

    def score(prediction):
        return accepted["score_visibility"](oracle, prediction, gates, 1, legacy["score_game"])

    positive = score(nominal)
    if not positive["pass"]:
        raise AssertionError(positive["failed"])
    if any(shot["iou"] != 1 for shot in positive["map"]["shots"]) or positive["hud"]["iou"] != 1:
        raise AssertionError("Rendered atlas does not match exact authored mask geometry")
    negative = copy.deepcopy(nominal)
    name = CONTROLS[index]
    extra = {}
    if name == "lost_slope":
        rows = tuple(row.replace("/", "#").replace("\\", "#") for row in terrain(seed))
        with patch.object(wide_truth, "terrain", return_value=rows):
            failed = require_refusal(lambda: wide_truth.require_coverage(wide_truth.build_truth(seed)),
                                     "rising_slope, falling_slope")
    elif name == "changed_dev_pin":
        dev = root / "dev"
        generate(dev, 17)
        require_dev_pin(dev)
        movie = dev / "recording.mp4"
        data = movie.read_bytes()
        movie.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        failed = require_refusal(lambda: require_dev_pin(dev), "Pinned dev fixture digest mismatch")
    else:
        gate = "map"
        if name == "floor_only":
            current_floor_only(truth, negative)
        elif name == "fixed_pan":
            fixed_pan(truth, negative)
            gate = "camera"
        elif name == "star_lattice":
            star_lattice(decoded_images(observations, root), negative)
            gate = "camera"
        elif name == "modal_luma":
            modal_luma(decoded_images(observations, root), negative, legacy["raster"])
        elif name == "missing_fully_visible":
            del negative["cells"][min(oracle["fully_visible"])]
        elif name == "invented_unseen":
            key = min(key for key, state in truth["visibility"].items() if state == wide_truth.NEVER)
            negative["cells"][key] = "solid"
            gate = "unseen"
        result = score(negative)
        if result["pass"] or gate not in result["failed"]:
            raise AssertionError(f"{name} survived: {result['failed']}")
        failed = gate
        extra = {"negative_camera_p95_px": result["camera"]["p95_px"],
                 "negative_camera_samples": result["camera"]["samples"],
                 "negative_min_shot_iou": min(shot["iou"] for shot in result["map"]["shots"])}
    return {"S": 1, "F": 0, "U": 0, "K": 1, "M": 1, "control": name, "rejected_by": failed,
            "nominal_min_shot_iou": min(shot["iou"] for shot in positive["map"]["shots"]),
            "nominal_camera_p95_px": positive["camera"]["p95_px"],
            "visibility_counts": truth["visibility_counts"],
            "mandatory_strata": len(truth["strata_counts"]), **extra}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, type=int, choices=range(len(CONTROLS)))
    parser.add_argument("--scorer", required=True, type=Path)
    parser.add_argument("--visibility-scorer", required=True, type=Path)
    parser.add_argument("--thresholds", required=True, type=Path)
    args = parser.parse_args()
    legacy = load_pinned(args.scorer, SCORER_PIN)
    accepted = load_pinned(args.visibility_scorer, VISIBILITY_PIN)
    if digest(args.thresholds) != THRESHOLDS_PIN:
        raise ValueError("Custody thresholds pin mismatch")
    gates = json.loads(args.thresholds.read_text())["game"]
    with tempfile.TemporaryDirectory() as scratch:
        print(json.dumps(run_case(args.case, Path(scratch), legacy, accepted, gates), sort_keys=True))


if __name__ == "__main__":
    main()
