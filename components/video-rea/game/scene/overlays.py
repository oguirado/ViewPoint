# SPDX-License-Identifier: AGPL-3.0-only
"""Locate screen-locked overlays from decoded raster evidence."""
from dataclasses import dataclass

import numpy as np


def luminance(rgb):
    # Work in luma to keep the original pixel boundaries after 4:2:0 decoding.
    return np.rint(rgb @ np.array([.299, .587, .114], dtype=np.float32)).astype(np.int16)


def mode(values):
    return int(np.bincount(values.ravel(), minlength=256).argmax())


@dataclass(frozen=True)
class Overlay:
    box: tuple[int, int, int, int]
    radius: int | None = None


def status_band(samples, sky):
    """Find a persistent, filled rectangle with contrasting interior marks."""
    median = np.median(samples, axis=0).astype(np.int16)
    height, width = median.shape
    counts = np.bincount(median.ravel(), minlength=256)
    best, best_vote = None, 0.0
    for base in np.flatnonzero(counts >= min(width, height)):
        if abs(int(base) - sky) < 6:
            continue
        common = np.abs(median - base) <= 4
        rows = common.sum(axis=1)
        cols = common.sum(axis=0)
        ys = np.flatnonzero(rows >= max(5, rows.max() * .45))
        xs = np.flatnonzero(cols >= max(5, cols.max() * .45))
        if not ys.size or not xs.size:
            continue
        y0, y1, x0, x1 = int(ys[0]), int(ys[-1]) + 1, int(xs[0]), int(xs[-1]) + 1
        w, h = x1 - x0, y1 - y0
        if min(w, h) < 6 or max(w, h) < 2.5 * min(w, h):
            continue
        area = np.s_[y0:y1, x0:x1]
        fill = common[area].mean()
        if fill < .66:
            continue
        # Text should be inside the band. This excludes bright terrain rims.
        interior = median[y0 + 2:y1 - 2, x0 + 2:x1 - 2]
        ink = (interior > base + 55).sum()
        if ink < 12:
            # A blank band can still be visible from its filled footprint.
            # A terrain interior touches a continuous brighter rim instead.
            neighbors = []
            if y0:
                neighbors.append(median[y0 - 1, x0:x1])
            if y1 < height:
                neighbors.append(median[y1, x0:x1])
            if x0:
                neighbors.append(median[y0:y1, x0 - 1])
            if x1 < width:
                neighbors.append(median[y0:y1, x1])
            if fill < .95 or any((edge > base + 30).mean() > .80 for edge in neighbors):
                continue
        locked = (np.abs(samples[:, y0:y1, x0:x1] - base) <= 8).mean()
        vote = w * h * fill * locked
        if locked >= .65 and vote > best_vote:
            best, best_vote = Overlay((x0, y0, w, h)), vote
    return best


def outlined_panel(luma, status, sky):
    """Find an outlined screen rectangle, including the bounds of rounded corners."""
    bright = luma > sky + 65
    if status is not None:
        x, y, w, h = status.box
        bright[y:y + h, x:x + w] = False
    # Continuous outlines have orthogonal edges on both sides. The two aligned
    # horizontal runs act as a witness distinct from text or scrolling terrain.
    middle = bright[:, :-2] & bright[:, 1:-1] & bright[:, 2:]
    rows = np.flatnonzero(middle.sum(axis=1) >= luma.shape[1] * .35)
    for top in rows:
        for bottom in rows[rows > top + 10]:
            common = np.flatnonzero(middle[top] & middle[bottom])
            if common.size < .35 * luma.shape[1]:
                continue
            left_edge, right_edge = int(common[0]) + 1, int(common[-1]) + 1
            sides = bright[top + 2:bottom - 1].mean(axis=0)
            left = np.flatnonzero(sides[:left_edge + 1] > .80)
            right = np.flatnonzero(sides[right_edge:] > .80)
            if left.size and right.size:
                x0, x1 = int(left[-1]), right_edge + int(right[0])
                inset = min(left_edge - x0, x1 - right_edge)
                radius = max(1, inset)
                return Overlay((x0, int(top), x1 - x0 + 1, int(bottom - top + 1)), radius)
    return None
