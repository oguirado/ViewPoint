# SPDX-License-Identifier: AGPL-3.0-only
"""Custodian-side regeneration of private wide-v1 labels and raster masks."""

import argparse
import json
import os
import tempfile
from collections import Counter
from itertools import islice, pairwise
from pathlib import Path

from .game import HEIGHT, STEP_MS, TILE, WIDTH
from .generate import digest, generate
from .wide import AIR, POS_PER_SHOT, PROFILE, SHOTS, TERRAIN, atlas, background, terrain, tile_scanlines, timeline


TRUTH_SCHEMA = "rea.synth_game.wide_truth/v1"
REQUIRED_STRATA = (
    "multirow_terrain", "elevated_platform", "cliff", "rising_slope", "falling_slope",
    "signed_x", "signed_y", "horizontal_reversal", "vertical_reversal", "fast_pan",
    "coloured_sky", "parallax", "sprite_sheet", "moving_hud", "absent_hud",
    "one_pixel_column", "one_pixel_row", "cuts", "fully_visible", "partially_visible",
    "never_visible",
)
FULL = "fully_visible"
PARTIAL = "partially_visible"
NEVER = "never_visible"


def mask_bits(left, right):
    return ((1 << (right - left)) - 1) << left


def frame_geometry(frame, rows):
    """Return viewport/HUD geometry and D12 cell visibility for one frame."""
    x, y = frame.camera
    hud = [0] * HEIGHT
    if frame.hud is not None:
        left, top, width, height = frame.hud
        hud[top:top + height] = [mask_bits(left, left + width)] * height
    map_rows = [0] * HEIGHT
    visible = {}
    coverage = Counter()
    solid_rows = set()
    for row in range(max(0, y // TILE), min(len(rows), (y + HEIGHT + TILE - 1) // TILE)):
        sy = row * TILE - y
        top, bottom = max(0, sy), min(HEIGHT, sy + TILE)
        for col in range(max(0, x // TILE), min(len(rows[row]), (x + WIDTH + TILE - 1) // TILE)):
            sx = col * TILE - x
            left, right = max(0, sx), min(WIDTH, sx + TILE)
            bits = mask_bits(left, right)
            area = sum((bits & ~hud[line]).bit_count() for line in range(top, bottom))
            if not area:
                continue
            visible[row, col] = FULL if area == TILE * TILE else PARTIAL
            code = rows[row][col]
            paint = 0
            for local_y, start, stop in tile_scanlines(code):
                line = sy + local_y
                lo, hi = max(left, sx + start), min(right, sx + stop)
                if top <= line < bottom and lo < hi:
                    support = mask_bits(lo, hi) & ~hud[line]
                    map_rows[line] |= support
                    paint += support.bit_count()
            if paint:
                solid_rows.add(row)
                coverage[TERRAIN[code]] += 1
                if right - left == 1:
                    coverage["one_pixel_column"] += 1
                if bottom - top == 1:
                    coverage["one_pixel_row"] += 1
    if len(solid_rows) > 1:
        coverage["multirow_terrain"] += 1
    return map_rows, visible, coverage


def rectangles(scanlines):
    """Serialize packed scanlines as deterministic half-open XYWH rectangles."""
    result = []
    for y, bits in enumerate(scanlines):
        x = 0
        while bits:
            gap = (bits & -bits).bit_length() - 1
            bits >>= gap
            x += gap
            width = (bits ^ (bits + 1)).bit_length() - 1
            result.append([x, y, width, 1])
            bits >>= width
            x += width
    return result


def actor_masks(frame, poses):
    mask = poses[frame.pose].getchannel("A")
    x, y = (actor - camera for actor, camera in zip(frame.actor, frame.camera, strict=True))
    screen = [0] * HEIGHT
    for sy in range(max(0, y), min(HEIGHT, y + TILE)):
        for sx in range(max(0, x), min(WIDTH, x + TILE)):
            if mask.getpixel((sx - x, sy - y)):
                screen[sy] |= 1 << sx
    visible = screen.copy()
    if frame.hud is not None:
        left, top, width, height = frame.hud
        mask = mask_bits(left, left + width)
        for row in range(top, top + height):
            visible[row] &= ~mask
    return rectangles(screen), rectangles(visible)


def build_truth(seed):
    """Produce the private reference from authored geometry at delivered occurrences."""
    rows = terrain(seed)
    _, poses, colors = atlas(seed)
    sky = background(seed, colors["="])
    samples = list(timeline(seed))
    frames, cuts = [], []
    detected = {}
    coverage = Counter()
    for frame in samples:
        if frames and frame.shot != frames[-1]["shot"]:
            cuts.append(frame.index)
        raster, visibility, hits = frame_geometry(frame, rows)
        coverage.update(hits)
        for index, kind in visibility.items():
            if kind == FULL or index not in detected:
                detected[index] = kind
        x, y = frame.camera
        actor_mask, actor_visible = actor_masks(frame, poses)
        frames.append({
            "frame_index": frame.index, "pts_ms": frame.pts_ms, "shot": frame.shot,
            "camera_xy": [x, y], "map": rectangles(raster),
            "hud": [list(frame.hud)] if frame.hud else [],
            "parallax_xy": [[x * rate // 8, y * rate // 8] for rate, *_ in sky.layers],
            "actor": {"world_xy_px": list(frame.actor), "pose": frame.pose,
                      "viewport_mask": actor_mask, "visible_mask": actor_visible},
        })
    for a, b in pairwise(samples):
        if a.shot != b.shot:
            continue
        dx, dy = (right - left for left, right in zip(a.camera, b.camera, strict=True))
        dt = b.pts_ms - a.pts_ms
        coverage["fast_pan"] += abs(dx) * 1000 > 100 * dt
        for key, step in (("horizontal", dx), ("vertical", dy)):
            coverage[f"{key}_forward"] += step > 0
            coverage[f"{key}_backward"] += step < 0
    coverage["horizontal_reversal"] = min(coverage["horizontal_forward"], coverage["horizontal_backward"])
    coverage["vertical_reversal"] = min(coverage["vertical_forward"], coverage["vertical_backward"])
    coverage["signed_x"] = sum(frame.camera[0] < 0 for frame in samples)
    coverage["signed_y"] = sum(frame.camera[1] < 0 for frame in samples)
    coverage["coloured_sky"] = sum(len(set(color)) > 1 for color in (sky.upper, sky.lower))
    coverage["parallax"] = len({tuple(map(tuple, frame["parallax_xy"])) for frame in frames})
    coverage["sprite_sheet"] = len({frame["actor"]["pose"] for frame in frames
                                    if frame["actor"]["visible_mask"]})
    coverage["moving_hud"] = len({frame.hud for frame in samples if frame.hud is not None})
    coverage["absent_hud"] = sum(frame.hud is None for frame in samples)
    coverage["cuts"] = len(cuts)

    visibility, cells = {}, {}
    for y, row in enumerate(rows):
        for x, code in enumerate(row):
            key = f"{x},{y}"
            state = detected.get((y, x), NEVER)
            visibility[key] = state
            cells[key] = None if state == NEVER else "air" if code == AIR else "solid"
    counts = Counter(visibility.values())
    coverage.update(counts)
    return {
        "schema": TRUTH_SCHEMA, "profile": PROFILE, "seed": seed,
        "width": WIDTH, "height": HEIGHT, "frames": frames, "cuts": cuts,
        "level": {"tile_px": TILE, "rows": rows, "legend": TERRAIN},
        "cells": cells, "visibility": visibility, "visibility_counts": dict(counts),
        "strata_counts": {key: coverage[key] for key in REQUIRED_STRATA},
        "sky": {"gradient_rgb": [sky.upper, sky.lower], "platform_rgb": colors["="],
                "parallax_layers": sky.layers, "gradient_phase": sky.phase},
    }


def require_coverage(truth):
    missing = [key for key in REQUIRED_STRATA if truth["strata_counts"].get(key, 0) <= 0]
    if missing:
        raise ValueError(f"Missing mandatory wide strata: {', '.join(missing)}")


def scorer_input(truth, raster):
    """Normalize for the existing custody score_game and score_visibility functions."""
    require_coverage(truth)
    width, height = truth["width"], truth["height"]
    return {
        "width": width, "height": height, "cuts": truth["cuts"], "cells": truth["cells"],
        "fully_visible": {key for key, state in truth["visibility"].items() if state == FULL},
        "frames": [{"camera_xy": frame["camera_xy"],
                    "map": raster(frame["map"], width, height),
                    "hud": raster(frame["hud"], width, height)}
                   for frame in truth["frames"]],
    }


def manifest(directory):
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Expected a regular wide observation directory")
    names = {"recording.mp4", "inputs.jsonl", "frames"}
    if {path.name for path in islice(directory.iterdir(), len(names) + 1)} != names:
        raise ValueError("Unexpected content in wide observation directory")
    frames = directory / "frames"
    if frames.is_symlink() or not frames.is_dir():
        raise ValueError("Expected a regular wide frames directory")
    filenames = {f"{index:05d}.png" for index in range(SHOTS * POS_PER_SHOT)}
    files = sorted(islice(frames.iterdir(), len(filenames) + 1))
    if {path.name for path in files} != filenames:
        raise ValueError("Unexpected frame paths in wide observation directory")
    paths = (directory / "recording.mp4", directory / "inputs.jsonl", *files)
    entries = {}
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError("Expected regular wide observation files")
        entries[path.relative_to(directory).as_posix()] = path.stat().st_size
    return entries


def verify_observations(observations, reference):
    expected = manifest(reference)
    if manifest(observations) != expected:
        raise ValueError("Wide observation paths or byte lengths do not match")
    hashes = {}
    for name in expected:
        expected_hash = digest(reference / name)
        if digest(observations / name) != expected_hash:
            raise ValueError(f"Wide observation digest mismatch: {name}")
        hashes[name] = expected_hash
    return hashes


def regenerate_truth(observations, seed, scratch_dir):
    """Re-emit independently from the held seed and authenticate the observation packet."""
    observations = Path(observations)
    truth = build_truth(seed)
    require_coverage(truth)
    with tempfile.TemporaryDirectory(dir=scratch_dir, prefix=".wide-verify-") as scratch:
        reference = Path(scratch) / "observations"
        generate(reference, seed, profile=PROFILE)
        hashes = verify_observations(observations, reference)
    truth["observation_sha256"] = hashes
    for frame in truth["frames"]:
        index = frame["frame_index"]
        frame["provenance"] = {"video_sha256": hashes["recording.mp4"], "frame_index": index,
                               "frame_sha256": hashes[f"frames/{index:05d}.png"],
                               "pts_ms": frame["pts_ms"], "label": "observed"}
    truth["video"] = {"path": "recording.mp4", "width": WIDTH, "height": HEIGHT,
                      "sha256": hashes["recording.mp4"],
                      "duration_ms": truth["frames"][-1]["pts_ms"] + STEP_MS}
    return truth


def publish_truth(observations, seed, out):
    """Publish a private, exclusive truth file outside the observation packet."""
    observations, out = Path(observations), Path(out)
    if out.resolve().is_relative_to(observations.resolve()):
        raise ValueError("Wide truth must be outside the observation packet")
    if out.exists() or out.is_symlink():
        raise FileExistsError(out)
    truth = regenerate_truth(observations, seed, out.parent)
    with tempfile.TemporaryDirectory(dir=out.parent, prefix=".wide-truth-") as scratch:
        staged = Path(scratch) / "truth.json"
        staged.write_text(json.dumps(truth, sort_keys=True, separators=(",", ":")) + "\n",
                          encoding="utf-8")
        os.chmod(staged, 0o600)
        os.link(staged, out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", required=True, type=Path)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    publish_truth(args.observations, args.seed, args.out)


if __name__ == "__main__":
    main()
