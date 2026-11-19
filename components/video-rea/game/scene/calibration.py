# SPDX-License-Identifier: AGPL-3.0-only
"""Learn Beacon Run's screen layers and grid from decoded frames."""
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from .overlays import Overlay, luminance, mode, outlined_panel, status_band
from .raster import Raster, SKY_LANDMARK_X, SKY_STRIDE, SKY_Y, hud_mask


@dataclass(frozen=True)
class Terrain:
    size: int
    floor_row: int
    rim: int
    fill: int
    detail: int


def _descriptor(status, panel):
    return {"status_xywh": list(status.box) if status else None,
            "panel_xywh": list(panel.box) if panel else None,
            "panel_corner_radius_px": panel.radius if panel else None}


@lru_cache(maxsize=32)
def _mask(width, height, status, panel):
    return hud_mask(_descriptor(status, panel), (width, height))


def _long_edges(luma, hud, sky):
    # A terrain rim has sky above it, and a darker interior below it.
    above, rim, fill = luma[:-2], luma[1:-1], luma[2:]
    candidates = ((np.abs(above - sky) <= 17) & (rim - fill >= 21)
                  & (fill > sky + 13) & ~(hud[:-2] | hud[1:-1] | hud[2:]))
    return candidates[:, 1:-1] & candidates[:, :-2] & candidates[:, 2:]


def _rim_and_seams(luma, hud, row, rim, fill):
    visible = ~hud[row] & ~hud[row + 1]
    rim_px = (np.abs(luma[row] - rim) <= 13) & visible
    interior = np.abs(luma[row + 1] - fill) <= 13
    dark = (np.abs(luma[row] - fill) <= 15) & interior & visible
    seam = np.zeros_like(rim_px)
    seam[1:-1] = rim_px[:-2] & dark[1:-1] & rim_px[2:]
    # The last pixel of a tile rim can land exactly at the viewport's right
    # boundary. Admit it when a longer visible rim immediately precedes it.
    seam[-1] = len(rim_px) >= 5 and rim_px[-5:-1].all() and dark[-1]
    return np.flatnonzero(seam)


def learn_terrain(samples, masks, sky):
    if samples.shape[1] < 3 or samples.shape[2] < 5:
        return None
    rows = Counter()
    for luma, hud in zip(samples, masks, strict=True):
        edge = _long_edges(luma, hud, sky)
        hits = edge.sum(axis=1)
        if hits.max() >= 12:
            rows.update({int(hits.argmax()) + 1: int(hits.max())})
    if not rows:
        return None
    row = rows.most_common(1)[0][0]
    rims, fills = [], []
    for luma, hud in zip(samples, masks, strict=True):
        exposed = _long_edges(luma, hud, sky)[row - 1]
        rims.extend(luma[row, 1:-1][exposed])
        fills.extend(luma[row + 1, 1:-1][exposed])
    rim, fill = mode(np.array(rims)), mode(np.array(fills))
    spacings = Counter()
    for luma, hud in zip(samples, masks, strict=True):
        seams = _rim_and_seams(luma, hud, row, rim, fill)
        spacings.update(int(gap) for gap in np.diff(seams) if 4 <= gap <= luma.shape[0])
    if not spacings:
        return None
    size, votes = spacings.most_common(1)[0]
    if votes < 3:
        return None
    # The most frequent brighter interior shade can be a repeating tile motif.
    colors = Counter()
    for luma, hud in zip(samples, masks, strict=True):
        lower = luma[row + 1:row + size]
        colors.update(lower[(lower > fill + 12) & (lower < rim - 10) & ~hud[row + 1:row + size]])
    detail = int(colors.most_common(1)[0][0]) if colors else fill
    return Terrain(size, row, rim, fill, detail)


def _stars(width, height):
    """Beacon Run's reference sparse parallax landmark, in projected pixels."""
    valid = SKY_Y < height
    return SKY_Y[valid], (SKY_LANDMARK_X[valid] - np.arange(width)[:, None]) % width


def _actor_extents(foreground):
    """Conservatively mask the rectangular support of connected foreground marks."""
    mask = foreground.copy()
    remaining = set(zip(*np.nonzero(foreground)))
    while remaining:
        seed = remaining.pop()
        component = [seed]
        for y, x in component:
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                neighbor = y + dy, x + dx
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    component.append(neighbor)
        ys, xs = zip(*component)
        x0, x1 = min(xs), max(xs)
        clipped = x0 == 0 or x1 == foreground.shape[1] - 1
        if len(component) >= 3 or clipped:
            # A viewport edge can expose only the wing or tip of a sprite.
            # Discount nearby pixels when measuring unobstructed sky evidence.
            margin = 6 if clipped else 0
            y0, y1 = max(0, min(ys) - margin), max(ys) + margin + 1
            mask[y0:y1, x0:x1 + 1] = True
    return mask


def _observe(luma, hud, sky, terrain, offset, floor, stars):
    x, y = offset
    size = terrain.size
    is_sky = np.abs(luma - sky) <= 17
    star_y, star_x = stars
    is_sky[star_y, star_x] |= luma[star_y, star_x] > sky + 28
    # Exclude the interiors of transient silhouettes from the sky evidence.
    foreground = ~is_sky & ~hud
    foreground[floor:] = False
    clear_sky = is_sky & ~_actor_extents(foreground)
    seams = (np.arange(luma.shape[1]) + x) % size == size - 1
    cells = {}
    height, width = luma.shape
    area = size * size
    for row in range(max(0, (y + size - 1) // size), (y + height) // size):
        sy = row * size - y
        ys = slice(sy, sy + size)
        solid_columns = _solid_columns(luma[ys], terrain, seams)
        for col in range((x + size - 1) // size, (x + width) // size):
            sx = col * size - x
            xs = slice(sx, sx + size)
            if hud[ys, xs].any():
                continue
            solid = solid_columns[xs].sum()
            # Confirm a complete tile with repeated vertical paint support.
            # Isolated corrupted columns do not erase a well-supported rim.
            if sy == floor and solid * 4 >= size * 3:
                cells[row, col] = "#"
            elif (solid < 2 and is_sky[ys, xs].sum() * 10 >= 7 * area
                  and clear_sky[ys, xs].sum() * 2 >= area):
                # A sprite can cover more than a quarter of a fully visible tile.
                # Sky outside the foreground footprint still provides an air witness.
                cells[row, col] = "."
    return cells


def _solid_columns(paint, terrain, seams):
    fill = np.abs(paint[1:] - terrain.fill) <= 13
    detail = np.abs(paint[1:] - terrain.detail) <= 13
    deep_fill = (fill | detail).sum(axis=0) >= terrain.size - 3
    surface = np.where(seams, np.abs(paint[0] - terrain.fill) <= 15,
                       np.abs(paint[0] - terrain.rim) <= 20)
    return deep_fill & surface


def _sparse_floor(luma, hud, terrain, offsets, rows):
    """Refine the parallax phase from clipped rim and deep-fill columns."""
    x = np.arange(luma.shape[1])
    seams = (x + offsets[:, None]) % terrain.size == terrain.size - 1
    scores = []
    for floor in rows:
        bottom = floor + terrain.size
        if bottom > luma.shape[0]:
            break
        clear = ~hud[floor:bottom].any(axis=0)
        columns = _solid_columns(luma[floor:bottom], terrain, seams)
        scores.append((floor, np.count_nonzero(columns & clear, axis=1)))
    best = max((counts.max() for _, counts in scores), default=0)
    winners = [(floor, counts) for floor, counts in scores if counts.max() == best]
    if best < 4 or len(winners) != 1:
        return None, None
    floor, counts = winners[0]
    matches = offsets[counts == best]
    return floor, int(matches[0]) if len(matches) == 1 else None


def _partial_solids(luma, hud, terrain, offset, floor):
    """Find solid tile fragments where the HUD or viewport clips a floor cell."""
    x, y = offset
    size = terrain.size
    width = luma.shape[1]
    if y + floor < 0 or (y + floor) % size or floor + size > luma.shape[0]:
        return {}

    # Compression near a sharp HUD edge can disturb individual luma samples.
    # A deep fill column and the expected rim/seam still give independent support.
    paint = luma[floor:floor + size]
    clear = ~hud[floor:floor + size].any(axis=0)
    seams = (np.arange(width) + x) % size == size - 1
    supported = clear & _solid_columns(paint, terrain, seams)
    row = (y + floor) // size
    result = {}
    for col in range(x // size, (x + width + size - 1) // size):
        screen_x = col * size - x
        left, right = max(0, screen_x), min(width, screen_x + size)
        if right - left == size and clear[left:right].all():
            continue
        # Keep the longest contiguous span, so the witness never bridges HUD or
        # background pixels. A lone matching column does not establish a tile.
        boundaries = np.flatnonzero(np.diff(supported[left:right], prepend=False, append=False))
        if boundaries.size:
            starts, ends = boundaries[::2], boundaries[1::2]
            best = int((ends - starts).argmax())
            span = int(ends[best] - starts[best])
            if span >= 2:
                result[row, col] = (left + int(starts[best]), floor, span, size)
    return result


@dataclass(frozen=True)
class Calibration:
    width: int
    height: int
    sky: int
    status: Overlay | None
    terrain: Terrain | None
    star_y: np.ndarray
    star_x: np.ndarray
    spawn: tuple[int, int, int, int, int] | None

    @property
    def period(self):
        return self.width * SKY_STRIDE

    def measure(self, image, prior_y):
        luma = luminance(image)
        panel = outlined_panel(luma, self.status, self.sky)
        hud = _mask(self.width, self.height, self.status, panel)
        descriptor = _descriptor(self.status, panel)
        issues = []
        if self.status is None:
            issues.append("status_band_unresolved")
        if self.terrain is None:
            issues.append("tile_geometry_unresolved")
        votes = np.count_nonzero((luma[self.star_y, self.star_x] >= self.sky + 30)
                                 & ~hud[self.star_y, self.star_x], axis=1)
        shift = int(votes.argmax())
        if votes[shift] < 8 or np.count_nonzero(votes >= votes[shift] - 3) != 1:
            return Raster(None, (0, 0), panel, {}, hud=descriptor,
                          degraded=(*issues, "camera_landmarks_unresolved"))

        start = shift * SKY_STRIDE
        x, y = start + (SKY_STRIDE - 1) // 2, prior_y
        x_error, y_error = SKY_STRIDE // 2, self.height
        cells = {}
        partial = {}
        if self.terrain is not None:
            t = self.terrain
            y_error = t.size
            possible = _long_edges(luma, hud, self.sky).sum(axis=1)
            # Restrict the floor search to the learned bottom band so a
            # similarly shaded overlay cannot establish vertical motion.
            lo = max(0, t.floor_row - 1 - t.size // 2)
            hi = min(possible.size, t.floor_row + t.size // 2)
            peak = int(possible[lo:hi].argmax()) + lo
            offsets = np.arange(start, start + SKY_STRIDE)
            if possible[peak] >= 8:
                floor = peak + 1
                # World floor is the last full grid band within the viewport.
                y, y_error = self.height - t.size - floor, 0
            floor = self.height - t.size - y
            if 0 <= floor < self.height - 1:
                seams = _rim_and_seams(luma, hud, floor, t.rim, t.fill)
                seam_votes = np.bincount(seams % t.size, minlength=t.size)
                counts = seam_votes[(t.size - 1 - offsets) % t.size]
                if counts.max() and np.count_nonzero(counts == counts.max()) == 1:
                    x, x_error = int(offsets[counts.argmax()]), 0
            if x_error or y_error:
                rows = range(lo + 1, hi + 1) if y_error else (floor,)
                floor_fit, x_fit = _sparse_floor(luma, hud, t, offsets, rows)
                if floor_fit is not None:
                    floor = floor_fit
                    y, y_error = self.height - t.size - floor, 0
                if x_fit is not None and x_error:
                    x, x_error = x_fit, 0
            if self.status is not None and not (x_error or y_error):
                cells = _observe(luma, hud, self.sky, t, (x, y), floor,
                                 (self.star_y, self.star_x[shift]))
                partial = _partial_solids(luma, hud, t, (x, y), floor)
        if x_error or y_error:
            issues.append("camera_grid_alignment_unresolved")
        at_spawn = False
        if self.spawn is not None and not (x or x_error or y_error):
            sx, sy, w, h, base = self.spawn
            patch = luma[sy - y:sy - y + h, sx:sx + w]
            unobscured = ~hud[sy - y:sy - y + h, sx:sx + w]
            at_spawn = patch.shape == (h, w) and bool(
                ((np.abs(patch - base) <= 15) & unobscured).mean() > .55)
        return Raster((x, y), (x_error, y_error), panel, cells, at_spawn, descriptor,
                      tuple(issues), partial_solids=partial)


def calibrate(frames):
    height, width = frames[0].image.shape[:2]
    indexes = np.unique(np.linspace(0, len(frames) - 1, min(33, len(frames)), dtype=int))
    sampled = np.stack([luminance(frames[index].image) for index in indexes])
    sky = mode(sampled)
    status = status_band(sampled, sky)
    panels = [outlined_panel(sample, status, sky) for sample in sampled]
    masks = [_mask(width, height, status, panel) for panel in panels]
    terrain = learn_terrain(sampled, masks, sky)
    star_y, star_x = _stars(width, height)
    spawn = _spawn_template(frames, status, sky, terrain)
    return Calibration(width, height, sky, status, terrain, star_y, star_x, spawn)


def _spawn_template(frames, status, sky, terrain):
    """Learn the origin actor from the first visible departure from an overlay."""
    if terrain is None:
        return None
    saw_panel = False
    for frame in frames:
        luma = luminance(frame.image)
        panel = outlined_panel(luma, status, sky)
        if panel is not None:
            saw_panel = True
            continue
        if not saw_panel:
            return None
        floor = terrain.floor_row
        # Look for a compact, vertically contiguous foreground patch at the
        # left of the departure view, immediately above the ground.
        area = luma[max(0, floor - terrain.size):floor, :luma.shape[1] // 4]
        columns = np.flatnonzero((area > sky + 55).sum(axis=0) >= max(3, terrain.size // 2))
        if not columns.size:
            return None
        x0 = int(columns[0])
        consecutive = np.flatnonzero(np.diff(columns) != 1)
        x1 = int(columns[consecutive[0]] if consecutive.size else columns[-1]) + 1
        pixels = area[:, x0:x1]
        rows = np.flatnonzero((pixels > sky + 55).sum(axis=1) >= max(2, (x1 - x0) // 2))
        if x1 - x0 < 3 or not rows.size:
            return None
        top = floor - area.shape[0] + int(rows[0])
        return x0, top, x1 - x0, floor - top, mode(pixels[int(rows[0]):])
    return None
