# SPDX-License-Identifier: AGPL-3.0-only
"""Validate the RP2 records and cross-check scene occurrence and tile lineage."""
from fractions import Fraction
from functools import lru_cache
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from contracts.semantics import check_record
from source.records import CONTRACTS, canonical, digest

from .frames import vision_milliseconds, vision_origin
from .raster import HEIGHT, TILE, WIDTH, hud_descriptor, hud_mask


SCENE_ROOT = Path(__file__).resolve().parent


def _validator():
    files = ("rea.game.level_map_v1.schema.json", "rea.frame_v1.schema.json")
    pins = json.loads((CONTRACTS / "PINS.json").read_bytes())["files"]
    resources = []
    for name in files:
        schema = json.loads((CONTRACTS / name).read_bytes())
        if digest(canonical(schema)) != pins[name]:
            raise ValueError(f"RP2 contract pin mismatch: {name}")
        resources.append((schema["$id"], Resource.from_contents(schema)))
    name = "scene.schema.json"
    scene = json.loads((SCENE_ROOT / name).read_bytes())
    scene_pins = json.loads((SCENE_ROOT / "PINS.json").read_bytes())["files"]
    if digest(canonical(scene)) != scene_pins.get(name):
        raise ValueError(f"scene schema pin mismatch: {name}")
    return Draft202012Validator(scene, registry=Registry().with_resources(resources))


def validate_scene(scene):
    """Check schema plus occurrence order, shot lineage, and visible tile footprints."""
    error = next(_validator().iter_errors(scene), None)
    if error is not None:
        raise ValueError(f"scene schema rejected {error.json_path} ({error.validator})")
    base = Fraction(int(scene["time_base"]["numerator"]), int(scene["time_base"]["denominator"]))
    origin = vision_origin(int(scene["shots"][0]["observations"][0]["pts_ticks"]))
    next_index, previous_ticks = 0, None
    viewport = tuple(scene.get("viewport_px", (WIDTH, HEIGHT)))
    for shot_id, shot in enumerate(scene["shots"]):
        if (shot["shot_id"] != shot_id or shot["geometry_epoch"] != shot_id
                or (shot["start_reason"] == "recording_start") != (shot_id == 0)):
            raise ValueError("invalid shot or geometry epoch sequence")
        level = shot["level_map"]
        check_record(level)
        tiles = level["tiles"]
        if "viewport_px" not in scene and tiles["tile_size_px"] != TILE:
            raise ValueError("legacy tile size is inconsistent with the raster profile")
        unresolved = ["tile_geometry_unresolved" in row.get("degraded", [])
                      for row in shot["observations"]]
        if any(unresolved) and (not all(unresolved) or tiles["tile_size_px"] != 1 or tiles["grid"] != [[None]]):
            raise ValueError("unresolved tile geometry cannot claim a calibrated grid")
        records = shot["observations"], level["camera_samples"], level["evidence"]
        if len({len(record) for record in records}) != 1:
            raise ValueError("unequal occurrence, camera and evidence counts")
        offsets = {}
        for observation, sample, evidence in zip(*records, strict=True):
            ticks = int(observation["pts_ticks"])
            pts_ms = vision_milliseconds(ticks, base, origin)
            if (observation["frame_index"] != next_index or
                    (previous_ticks is not None and ticks < previous_ticks) or
                    evidence["frame_index"] != next_index or evidence["video_sha256"] != scene["video_sha256"] or
                    evidence["pts_ms"] != pts_ms or evidence["label"] != "observed" or
                    sample["provenance"] != dict(evidence, label="inferred")):
                raise ValueError("camera or evidence does not match its source occurrence")
            descriptor = observation["hud"]
            if "viewport_px" in scene:
                _check_hud(descriptor, viewport, observation.get("degraded", []))
            elif descriptor != hud_descriptor(descriptor["panel_xywh"] is not None):
                raise ValueError("HUD geometry is inconsistent with the raster profile")
            if observation.get("camera_reset") and (
                    not next_index or sample["offset_px"][0] != 0 or any(observation["uncertainty_px"])):
                raise ValueError("camera reset requires a resolved return to the origin")
            offsets[next_index] = sample["offset_px"], observation["uncertainty_px"], descriptor
            next_index += 1
            previous_ticks = ticks
        if level["derived_from"] != list(dict.fromkeys(item["frame_sha256"] for item in level["evidence"])):
            raise ValueError("derived_from does not match shot PNGs")
        _check_tiles(shot, offsets, viewport)
    return scene


def _check_hud(descriptor, viewport, degraded):
    width, height = viewport
    status, panel = descriptor["status_xywh"], descriptor["panel_xywh"]
    radius = descriptor["panel_corner_radius_px"]
    if ((status is None) != ("status_band_unresolved" in degraded)
            or (panel is None) != (radius is None)):
        raise ValueError("HUD geometry is inconsistent with its recovery state")
    for box in (status, panel):
        if box is not None:
            x, y, w, h = box
            if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
                raise ValueError("HUD geometry is outside the viewport")


@lru_cache(maxsize=32)
def _witness_mask(status, panel, radius, viewport):
    return hud_mask({"status_xywh": status, "panel_xywh": panel, "panel_corner_radius_px": radius},
                    viewport)


def _check_tiles(shot, offsets, viewport):
    tiles = shot["level_map"]["tiles"]
    size = int(tiles["tile_size_px"])
    ox, oy = tiles["origin_px"]
    grid = tiles["grid"]
    known = {(r, c) for r, row in enumerate(grid) for c, code in enumerate(row) if code is not None}
    witnessed = set()
    width, height = viewport
    for witness in shot["tile_witnesses"]:
        row, col = int(witness["row"]), int(witness["column"])
        frame_index = witness["frame_index"]
        support = witness.get("visible_xywh")
        if ((row, col) in witnessed or frame_index not in offsets or row >= len(grid) or col >= len(grid[row])
                or (support is None and (row, col) not in known)):
            raise ValueError("invalid or duplicate tile witness or witness from another shot")
        (dx, dy), uncertainty, hud = offsets[frame_index]
        x, y = ox + col * size - dx, oy + row * size - dy
        left, top, w, h = support if support is not None else (x, y, size, size)
        if (hud["status_xywh"] is None or any(uncertainty)
                or not (0 <= left <= width - w and 0 <= top <= height - h)):
            raise ValueError("tile witness is outside a resolved visible footprint")
        if support is not None:
            # A partial solid witness may also back a raster projection in the grid.
            if grid[row][col] not in (None, "#"):
                raise ValueError("partial tile evidence requires solid terrain")
            if not (x <= left <= x + size - w and w < size and top == y and h == size):
                raise ValueError("partial terrain evidence is not inside the tile from rim to base")
        x, y = int(x), int(y)
        left, top, w, h = map(int, (left, top, w, h))
        status, panel = hud["status_xywh"], hud["panel_xywh"]
        mask = _witness_mask(tuple(status) if status else None, tuple(panel) if panel else None,
                             hud["panel_corner_radius_px"], viewport)
        if support is not None and 0 <= x <= width - size and not mask[y:y + size, x:x + size].any():
            raise ValueError("partial terrain evidence requires a clipped footprint")
        if mask[top:top + h, left:left + w].any():
            raise ValueError("tile witness footprint is occluded by HUD")
        witnessed.add((row, col))
    if known - witnessed:
        raise ValueError("observed tile is missing a witness")
