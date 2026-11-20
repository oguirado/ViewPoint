# SPDX-License-Identifier: AGPL-3.0-only
"""Raster mask utilities and the RP2 reference camera calibration."""
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw

from .overlays import Overlay


WIDTH, HEIGHT = 192, 112
TILE = 16
WORLD_FLOOR_Y = HEIGHT - TILE
SPAWN_X = TILE
STATUS = [0, 0, WIDTH, 16]
PANEL = [31, 34, 130, 45]
PANEL_RADIUS = 4
# Reference star lattice measured in the origin view of the synthetic profile.
# The coarse shift advances one raster pixel per four projected camera pixels.
SKY_LANDMARK_X = np.arange(40) * 71
SKY_X = SKY_LANDMARK_X % WIDTH
SKY_Y = 19 + np.arange(40) * 31 % 64
SKY_STRIDE = 4
SKY_PERIOD = WIDTH * SKY_STRIDE
SKY_PROJECTIONS = (SKY_X - np.arange(WIDTH)[:, None]) % WIDTH
# The profile's floor projects to the bottom tile band. A one-tile search
# interval admits vertical jitter without confusing two grid-aligned platforms.
FLOOR_ROWS = np.arange(WORLD_FLOOR_Y - TILE // 2, WORLD_FLOOR_Y + TILE // 2)
MIN_SEAM_VOTES = 5


@dataclass(frozen=True)
class Raster:
    offset: tuple[int, int] | None
    uncertainty: tuple[int, int]
    panel: bool | Overlay | None
    cells: dict
    at_spawn: bool = False
    hud: dict | None = None
    degraded: tuple[str, ...] = ()
    partial_solids: dict[tuple[int, int], tuple[int, int, int, int]] = field(default_factory=dict)
    camera_reset: bool = False


def hud_descriptor(panel):
    return {"status_xywh": STATUS.copy(), "panel_xywh": PANEL.copy() if panel else None,
            "panel_corner_radius_px": PANEL_RADIUS if panel else None}


def hud_mask(descriptor, viewport=(WIDTH, HEIGHT)):
    """Expand status and panel rectangles to the covered screen pixels."""
    image = Image.new("1", viewport)
    draw = ImageDraw.Draw(image)
    status = descriptor["status_xywh"]
    if status is not None:
        x, y, w, h = status
        draw.rectangle((x, y, x + w - 1, y + h - 1), fill=1)
    panel = descriptor["panel_xywh"]
    if panel is not None:
        x, y, w, h = panel
        draw.rounded_rectangle((x, y, x + w - 1, y + h - 1),
                               radius=descriptor["panel_corner_radius_px"], fill=1)
    return np.asarray(image, dtype=bool)


def detect_panel(green):
    if not 44 <= np.median(green[4:13, 65:125]) <= 65:
        raise ValueError("unrecognized synthetic HUD status strip")
    # Interior patch away from text and exposed to camera motion only when unmasked.
    return int(np.count_nonzero((green[38:73, 36:56] >= 39) & (green[38:73, 36:56] <= 62))) > 550


def floor_uncertainty(y):
    floor = WORLD_FLOOR_Y - y
    return int(max(abs(floor - FLOOR_ROWS[0]), abs(floor - FLOOR_ROWS[-1])))


def register(green, hud, prior_y=0):
    # Sky points provide X modulo the parallax period even without visible terrain.
    bright = (green > 120) & ~hud
    sky_votes = np.count_nonzero(bright[SKY_Y, SKY_PROJECTIONS], axis=1)
    shift = int(sky_votes.argmax())
    if sky_votes[shift] < 10 or np.count_nonzero(sky_votes >= sky_votes[shift] - 3) != 1:
        return None
    start = shift * SKY_STRIDE
    midpoint = start + (SKY_STRIDE - 1) // 2
    half_bin = SKY_STRIDE // 2

    # The terrain's bright top edge and soil-colored seams refine Y and X.
    counts = np.count_nonzero(bright[FLOOR_ROWS], axis=1)
    best = int(counts.argmax())
    if counts[best] < WIDTH // 2 or np.count_nonzero(counts >= counts[best] - 6) != 1:
        floor = WORLD_FLOOR_Y - prior_y
        return (midpoint, prior_y), (half_bin, floor_uncertainty(prior_y)), floor, shift
    floor = int(FLOOR_ROWS[best])
    y = WORLD_FLOOR_Y - floor
    soil_row = floor + 2
    soil = (green[soil_row] > 47) & (green[soil_row] < 90) & ~hud[soil_row] & ~hud[floor]
    seam_votes = np.bincount(np.flatnonzero(soil & ~bright[floor]) % TILE, minlength=TILE)
    offsets = np.arange(start, start + SKY_STRIDE)
    votes = seam_votes[(TILE - 1 - offsets) % TILE]
    top = int(votes.max())
    if top < MIN_SEAM_VOTES:
        return (midpoint, y), (half_bin, 0), floor, shift
    plausible = offsets[votes >= max(MIN_SEAM_VOTES, top - 2)]
    offset = int(offsets[votes.argmax()])
    return (offset, y), (int(np.abs(plausible - offset).max()), 0), floor, shift


def observe_cells(green, hud, offset, shift):
    x, y = offset
    # A solid footprint has a bright top, a soil-colored seam, and soil below.
    # The same signature applies to ground and elevated platforms.
    sky = green <= 37
    star_x = (SKY_X - shift) % WIDTH
    sky[SKY_Y, star_x] = True
    soil = (green >= 46) & (green <= 115)
    cells = {}
    for row in range(max(0, (y + TILE - 1) // TILE), (y + HEIGHT) // TILE):
        sy = row * TILE - y
        for col in range((x + TILE - 1) // TILE, (x + WIDTH) // TILE):
            sx = col * TILE - x
            ys, xs = slice(sy, sy + TILE), slice(sx, sx + TILE)
            if hud[ys, xs].any():
                continue
            if sky[ys, xs].all():
                cells[row, col] = "."
            elif (soil[sy + 1:sy + TILE, xs].all() and soil[sy, sx + TILE - 1]
                  and (green[sy, sx:sx + TILE - 1] > 120).all()):
                cells[row, col] = "#"
    return cells


def measure(image, prior_y=0):
    if image.shape != (HEIGHT, WIDTH, 3):
        raise ValueError("unsupported synthetic raster geometry")
    green = image[:, :, 1]
    panel = detect_panel(green)
    hud = hud_mask(hud_descriptor(panel))
    registration = register(green, hud, prior_y)
    if registration is None:
        return Raster(None, (0, 0), panel, {})
    offset, uncertainty, floor, shift = registration
    cells = {} if any(uncertainty) else observe_cells(green, hud, offset, shift)
    # Sample the player body's left interior, away from its eye and the HUD.
    # At a witnessed spawn the player is grounded at the left edge of the level.
    body = image[floor - 10:floor - 2, SPAWN_X - 3:SPAWN_X]
    at_spawn = not any(uncertainty) and offset[0] == 0 and bool(
        ((body[:, :, 0] < 125) & (body[:, :, 1] > 211) & (body[:, :, 2] > 150)).all())
    return Raster(offset, uncertainty, panel, cells, at_spawn)
