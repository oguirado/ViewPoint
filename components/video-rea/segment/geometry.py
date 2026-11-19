# SPDX-License-Identifier: AGPL-3.0-only
"""Widget and ink proposals from locally observed fills, contours and occlusion."""
from dataclasses import dataclass, replace

import cv2
import numpy as np


@dataclass(frozen=True)
class Detection:
    kind: str
    bbox: tuple
    layer: str
    ink: tuple | None = None
    visible: tuple | None = None
    fill: tuple | None = None
    light_ink: bool | None = None
    container: bool = False

    def __post_init__(self):
        if self.visible is None:
            object.__setattr__(self, "visible", (self.bbox,))

    @property
    def small(self):
        return self.bbox[2] < 44 or self.bbox[3] < 24


@dataclass(frozen=True)
class Surface:
    box: tuple
    fill: tuple
    rim: bool


def right(box):
    return box[0] + box[2]


def bottom(box):
    return box[1] + box[3]


def overlap(a, b):
    x, y = max(a[0], b[0]), max(a[1], b[1])
    return x, y, max(0, min(right(a), right(b)) - x), max(0, min(bottom(a), bottom(b)) - y)


def area(box):
    return box[2] * box[3]


def inside(box, outer):
    return area(overlap(box, outer)) == area(box)


def visible_boxes(box, cover):
    x, y, w, h = overlap(box, cover)
    if not w or not h:
        return (box,)
    fragments = ((box[0], box[1], box[2], y - box[1]),
                 (box[0], y + h, box[2], bottom(box) - y - h),
                 (box[0], y, x - box[0], h),
                 (x + w, y, right(box) - x - w, h))
    return tuple(part for part in fragments if area(part))


def crop(image, box):
    x, y, w, h = box
    return image[y:y+h, x:x+w]


def color_distance(a, b):
    return max(abs(p - q) for p, q in zip(a, b))


def channel_max(rgb):
    a, b, c = cv2.split(rgb)
    return cv2.max(cv2.max(a, b), c)


def components(mask):
    _, _, values, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    return values[1:]


def dominant_colors(rgb, region):
    """Use occupied quantization bins as seeds, then refine to observed RGB."""
    sample = crop(rgb, region)[::3, ::3].reshape(-1, 3).astype(np.int32)
    bins = (sample[:, 0] >> 3) * 1024 + (sample[:, 1] >> 3) * 32 + (sample[:, 2] >> 3)
    histogram = np.bincount(bins, minlength=32768)
    ids = np.argsort(histogram, kind="stable")[-32:][::-1]
    colors = []
    for idx in ids:
        if histogram[idx] < 4:
            break
        pixels = sample[bins == idx]
        color = tuple(int(c) for c in np.sort(pixels, axis=0)[len(pixels) // 2])
        if all(color_distance(color, old) > 6 for old in colors):
            colors.append(color)
    return colors


def rim_extent(rgb, box, fill, region):
    """Follow a thin, coherent edge outside a fill; do not swallow the backdrop."""
    x, y, w, h = box
    ends = [x, y, x+w, y+h]
    painted = 0
    for side in range(4):
        axis = side % 2
        start, stop = (y + h//4, y + 3*h//4) if axis == 0 else (x + w//4, x + 3*w//4)
        direction = -1 if side < 2 else 1
        bound = region[axis] if direction < 0 else region[axis] + region[axis+2] - 1
        adjacent = ends[side] - (direction < 0)
        far = adjacent + 3 * direction
        if direction * (far - bound) > 0:
            continue
        stripes = [rgb[start:stop, adjacent+step*direction] if axis == 0
                   else rgb[adjacent+step*direction, start:stop] for step in range(4)]
        colors = [tuple(map(int, np.sort(line, axis=0)[len(line)//2])) for line in stripes]
        depth = 0
        for current in colors[:3]:
            if color_distance(current, colors[-1]) <= 8:
                break
            depth += 1
        if depth and color_distance(colors[0], fill) >= 7:
            painted += 1
            ends[side] += depth * direction
    return (ends[0], ends[1], ends[2]-ends[0], ends[3]-ends[1]), painted >= 2


def proposals(rgb, region, marks=()):
    """Find rectangular, filled contours while ignoring thin glyphs and cursor strokes."""
    rx, ry, rw, rh = region
    pixels = crop(rgb, region)
    smooth = cv2.medianBlur(pixels, 3)
    used = np.zeros((rh, rw), np.uint8)
    labels = np.zeros((rh, rw), np.uint8)
    candidates = []
    centers = dominant_colors(smooth, (0, 0, rw, rh))
    # Give each pixel to its closest observed fill before component extraction.
    best = np.full((rh, rw), 255, np.uint8)
    for index, fill in enumerate(centers, 1):
        distance = channel_max(cv2.absdiff(smooth, fill + (0,)))
        closer = (distance < best) & (distance <= 11)
        best[closer] = distance[closer]
        labels[closer] = index
    for index, fill in enumerate(centers, 1):
        cv2.compare(labels, index, cv2.CMP_EQ, dst=used)
        mask = cv2.morphologyEx(used, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        for x, y, w, h, count in components(mask):
            if min(w, h) < 8:
                continue
            box = tuple(map(int, (x+rx, y+ry, w, h)))
            # A filled rectangle has sustained matte runs near its perimeter.
            # Glyphs and unfilled rims do not satisfy this test.
            part = mask[y:y+h, x:x+w]
            ix, iy = max(1, w//10), max(1, h//10)
            top = part[iy:iy+3, w//5:4*w//5]
            bot = part[h-iy-3:h-iy, w//5:4*w//5]
            left = part[h//5:4*h//5, ix:ix+3]
            right_edge = part[h//5:4*h//5, w-ix-3:w-ix]
            support = [5*np.count_nonzero(side) >= 4*side.size for side in (top, bot, left, right_edge)]
            matte = sum(support) >= 3 or (support[0] and support[1] and count*2 >= w*h)
            strip_x, strip_y = max(3, w//18), max(3, h//18)
            edges = (part[:strip_y, w//5:4*w//5], part[-strip_y:, w//5:4*w//5],
                     part[h//5:4*h//5, :strip_x], part[h//5:4*h//5, -strip_x:])
            rim = min(w, h) >= 17 and all(
                np.count_nonzero(np.any(side, axis=i//2)) * 5 >= 4 * side.shape[1-i//2]
                for i, side in enumerate(edges))
            if not matte and not rim:
                continue
            if min(w, h) < 17 and (not all(support) or count*6 < 5*w*h):
                continue
            if w >= rw-3 or h >= rh-3:
                continue
            box, extent = rim_extent(rgb, box, fill, region)
            measured = fill
            if rim and not matte:
                inner = (box[0]+4, box[1]+4, box[2]-8, box[3]-8)
                palette = dominant_colors(rgb, inner)
                if not palette:
                    continue
                measured = palette[0]
            candidates.append(Surface(box, measured, rim or extent))
    for mark in marks:
        box = tuple(mark[k] for k in ("x", "y", "w", "h"))
        if not inside(box, region) or mark["rim_promille"] < 720 or min(box[2:]) < 16:
            continue
        x, y, w, h = box
        inner = (x+w//4, y+h//4, max(1, w//2), max(1, h//2))
        fill = dominant_colors(rgb, inner)
        if not fill:
            continue
        sample = crop(rgb, region)
        y0, y1 = max(0, y-ry-6), min(rh-1, y-ry+h+5)
        x0, x1 = max(0, x-rx-6), min(rw-1, x-rx+w+5)
        outside = np.concatenate((sample[y0, x0:x1+1], sample[y1, x0:x1+1],
                                  sample[y0:y1+1, x0], sample[y0:y1+1, x1]))
        outside = tuple(map(int, np.sort(outside, axis=0)[len(outside)//2]))
        if color_distance(fill[0], outside) >= 12:
            candidates.append(Surface(box, fill[0], True))
    unique = []
    for surface in sorted(candidates, key=lambda item: area(item.box), reverse=True):
        if not any(
                10 * area(overlap(surface.box, old.box)) >= 8 * max(area(surface.box), area(old.box))
                or inside(surface.box, old.box) and (
                    right(old.box) - right(surface.box) < 6 and old.box[0] + 6 > surface.box[0]
                    or bottom(old.box) - bottom(surface.box) < 6 and old.box[1] + 6 > surface.box[1])
                for old in unique):
            unique.append(surface)
    return unique


def line_boxes(mask):
    """Join glyph strokes on approximately the same baseline."""
    glyphs = [int(h) for x, y, w, h, count in components(mask)
              if h >= 5 and min(w, h) >= 2 and count >= 6]
    if not glyphs:
        return
    scale = sorted(glyphs)[len(glyphs)//2]
    connected = cv2.dilate(mask, np.ones((3, max(5, scale)), np.uint8))
    for x, y, w, h, _ in components(connected):
        ys, xs = np.nonzero(mask[y:y+h, x:x+w])
        if len(xs) < 9:
            continue
        width = int(xs.max()-xs.min()+1)
        height = int(ys.max()-ys.min()+1)
        if height >= 6 and width*3 >= 2*height and height <= 3*max(scale, 6):
            yield (int(x + xs.min()), int(y + ys.min()), width, height)


def ink_in(gray, background, free, box, inset=0):
    x, y, w, h = box
    inner = (x+inset, y+inset, w-2*inset, h-2*inset)
    if min(inner[2:]) <= 0:
        return []
    pixels = crop(gray, inner)
    bg = crop(background, inner) if isinstance(background, np.ndarray) else background
    delta = pixels.astype(np.int16) - bg
    lines = []
    for light, signed in ((False, -delta), (True, delta)):
        eligible = (signed > 21).astype(np.uint8) * crop(free, inner)
        for bx, by, bw, bh in line_boxes(eligible):
            if bh * 4 < 3 * h or bh <= 12:
                lines.append(((bx+inner[0], by+inner[1], bw, bh), light))
    return lines


def luminance(rgb):
    red, green, blue = rgb
    return (77*red + 150*green + 29*blue + 128) >> 8


def align_cover(residual, box):
    """Align a cover to the strongest continuous change at each raster edge."""
    x, y, w, h = box
    edges = [x, y, x+w, y+h]
    for side, original in enumerate(edges.copy()):
        vertical = side % 2 == 0
        scan = residual.T if vertical else residual
        low, high = (y+h//4, y+3*h//4) if vertical else (x+w//4, x+3*w//4)
        polarity = 1 if side < 2 else -1
        best = (0, 0, original)
        for point in range(max(1, original-2), min(len(scan), original+3)):
            jump = polarity * (scan[point, low:high].astype(np.int16) - scan[point-1, low:high])
            if jump.size:
                support = int(np.sort(jump)[jump.size//2])
                best = max(best, (support, -abs(point-original), point))
        if best[0] >= 10:
            edges[side] = best[2]
            continue
        # A color-contour proposal can absorb a dimmed neighboring surface.
        # Follow a contiguous patch of agreement with the expected backdrop
        # inward until a coherent modal edge supplies independent evidence.
        extent = w if vertical else h
        for offset in range(3, extent//8 + 1):
            point = original + polarity * offset
            outside = scan[point-1 if polarity > 0 else point, low:high]
            if 2*np.count_nonzero(outside <= 10) < outside.size:
                break
            jump = polarity * (scan[point, low:high].astype(np.int16) - scan[point-1, low:high])
            if 2*np.count_nonzero(jump >= 10) >= jump.size:
                edges[side] = point
                break
    return edges[0], edges[1], edges[2]-edges[0], edges[3]-edges[1]


def nested_ink(surface, parent, gray, free):
    """Reject compact, unsupported shapes that repeat a matte or its ink."""
    if surface.rim or 4*area(surface.box) >= area(parent.box):
        return False
    if color_distance(surface.fill, parent.fill) <= 11:
        return True
    w, h = surface.box[2:]
    for ink, _ in ink_in(gray, luminance(parent.fill), free, parent.box, inset=3):
        if w <= ink[3] and h <= ink[3] and inside(surface.box, ink):
            return True
    return False


def continues_matte(rgb, surface, region):
    """Find compact color islands whose fill continues outside their rectangle."""
    x, y, w, h = surface.box
    if surface.rim or min(w, h) >= 17:
        return False
    sides = ((x, y-2, w, 2), (x, y+h, w, 2), (x-2, y, 2, h), (x+w, y, 2, h))
    shared = 0
    for side in sides:
        if inside(side, region):
            distance = channel_max(cv2.absdiff(crop(rgb, side), surface.fill + (0,)))
            shared += 3 * np.count_nonzero(distance <= 10) >= 2 * distance.size
    return shared >= 2


def local(rgb, region, cover=False, marks=(), modal_residual=None):
    surfaces = [surface for surface in proposals(rgb, region, marks)
                if not cover or area(surface.box)*10 < area(region)*9]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    backdrop = cv2.medianBlur(gray, 19)
    free = np.zeros(rgb.shape[:2], np.uint8)
    crop(free, region)[:] = 255
    if modal_residual is not None:
        outside = np.uint8(crop(modal_residual, region) <= 10)
        _, labels, stats, _ = cv2.connectedComponentsWithStats(outside, connectivity=8)
        edge_labels = np.unique(np.concatenate((labels[0], labels[-1], labels[:, 0], labels[:, -1])))
        plausible = [label for label in edge_labels if label and 4*stats[label, -1] < outside.size]
        boundary = np.uint8(np.isin(labels, plausible))
        boundary = cv2.dilate(boundary, np.ones((5, 5), np.uint8))
        crop(free, region)[boundary != 0] = 0
    # A glyph or a cursor ring over a flat field can supply a color component.
    # Only separately witnessed surfaces may contribute to its widget hierarchy.
    surfaces = [s for s in surfaces if not continues_matte(rgb, s, region) and not any(
        p != s and inside(s.box, p.box) and nested_ink(s, p, gray, free) for p in surfaces)]
    children = {s: [t for t in surfaces if s != t and inside(t.box, s.box)] for s in surfaces}
    kinds = {}
    menus = set()
    for surface, nested in children.items():
        x, y, w, h = surface.box
        if nested:
            child = max(nested, key=lambda t: area(t.box))
            siblings = [t for t in nested if not inside(t.box, child.box)]
            kinds[surface] = ("menu" if not siblings and child.box[2]*4 >= w*3
                              and child.box[1] - y >= child.box[3]//2 else "list")
        else:
            has_menu_parent = any(kinds.get(p) == "menu" and surface in children[p] for p in kinds)
            kinds[surface] = "menu_item" if has_menu_parent else "button"
    for surface in surfaces:
        if kinds[surface] == "button" and any(
                kinds[p] == "menu" and surface in children[p] for p in surfaces):
            kinds[surface] = "menu_item"
    if not cover:
        lists = [s for s in surfaces if kinds[s] == "list"]
        popovers = [s for s in surfaces if kinds[s] == "menu"]
        anchors = [s for s in surfaces if kinds[s] == "button"
                   and any(s.box[1] <= p.box[1]+s.box[3]//2 and s.box[2]*2 < p.box[2]
                           for p in lists + popovers)]
        if anchors:
            anchor = min(anchors, key=lambda s: (s.box[1], s.box[2]))
            kinds[anchor] = "menu"
            ax, ay, aw, ah = anchor.box
            for surface in surfaces:
                sx, sy, sw, sh = surface.box
                if (kinds[surface] == "button" and abs(sx-ax) <= max(4, aw//10)
                        and ah <= sy-ay <= 2*ah and sw >= aw and sh*2 >= ah*3
                        and not any(area(overlap(surface.box, panel.box)) for panel in lists)):
                    kinds[surface] = "menu"
                    menus.add(surface)

    results = []
    for surface in sorted(surfaces, key=lambda s: area(s.box)):
        box = surface.box
        kind = kinds[surface]
        available = ink_in(gray, luminance(surface.fill), free, box, inset=5 if surface.rim else 3)
        ink, light = max(available, key=lambda entry: entry[0][2], default=(None, None))
        if kind == "menu" and (surface in menus or children[surface]):
            ink, light = min(available, key=lambda entry: (entry[0][1], -entry[0][2]),
                             default=(None, None))
        if kind == "button" and box[2] * 10 >= region[2] * 6:
            surrounding = crop(gray, region)
            baseline = int(np.sort(surrounding[::8, ::8], axis=None)[surrounding[::8, ::8].size//2])
            broad_field = (surface.rim and box[2] * 5 >= region[2] * 4
                           and abs(luminance(surface.fill)-baseline) <= 36)
            if (broad_field or surface.rim and light is not True and luminance(surface.fill) >= baseline - 24
                    or light is not None and box[3] * 7 >= region[3]):
                kind = "text_field"
        if kind == "list":
            ink, light = None, None
        if kind not in {"list", "menu"} or not children[surface] and surface not in menus:
            x, y, w, h = box
            outer = (max(0, x-3), max(0, y-3), w+6, h+6)
            crop(free, outer)[:] = 0
        else:
            x, y, w, h = box
            for edge in ((x, y, w, 4), (x, y+h-4, w, 4), (x, y, 4, h), (x+w-4, y, 4, h)):
                crop(free, edge)[:] = 0
        if ink:
            crop(free, ink)[:] = 0
        results.append(Detection(kind, box, "foreground", ink, fill=surface.fill,
                                 light_ink=light, container=bool(children[surface]) or surface in menus))
    if cover:
        x, y, w, h = region
        for edge in ((x, y, w, 6), (x, y+h-6, w, 6), (x, y, 6, h), (x+w-6, y, 6, h)):
            crop(free, edge)[:] = 0
        matte = dominant_colors(rgb, region)
        titles = ink_in(gray, luminance(matte[0]) if matte else backdrop, free, region, inset=7)
        eligible = [entry for entry in titles if entry[0][2] >= 3*entry[0][3]
                    and entry[0][1]+entry[0][3] <= y+h//3]
        ink, light = min(eligible or titles, key=lambda entry: (entry[0][1], -entry[0][2]),
                         default=(None, None))
        if ink:
            crop(free, ink)[:] = 0
        results.append(Detection("dialog", region, "foreground", ink, light_ink=light, container=True))

    edge = cv2.morphologyEx(backdrop, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8))
    free[edge >= 18] = 0
    lines = ink_in(gray, backdrop, free, region)
    kept = []
    for box, light in sorted(lines, key=lambda row: area(row[0]), reverse=True):
        # Surface edges create small, long halos; real strokes vary in both axes.
        if box[2] < 7 or box[3] * 6 > region[3] or box[2] > 10*box[3] and box[3] < 10 or any(
                2 * area(overlap(box, prior)) > min(area(box), area(prior)) for prior in kept):
            continue
        kept.append(box)
        results.append(Detection("text", box, "foreground", box, light_ink=light))
    return results


def scrim_cover(rgb, backdrop):
    """Estimate a global multiplicative scrim and find opaque regions over it."""
    if backdrop is None:
        return (), None
    sample = backdrop[::5, ::5].astype(np.int32)
    current = rgb[::5, ::5].astype(np.int32)
    denom = sample.sum(axis=2)
    perimeter = np.zeros(denom.shape, bool)
    height, width = perimeter.shape
    oy, ox = max(1, height//30), max(1, width//30)
    perimeter[:oy, :] = perimeter[-oy:, :] = True
    perimeter[:, :ox] = perimeter[:, -ox:] = True
    meaningful = (denom >= 38) & perimeter
    if not meaningful.any():
        return (), None
    ratios = (100 * current.sum(axis=2)[meaningful] + denom[meaningful]//2) // denom[meaningful]
    counts = np.bincount(np.minimum(ratios, 101), minlength=102)
    support = np.convolve(counts, np.ones(5, dtype=int), mode="same")
    rough = int(np.argmax(support[15:86])) + 15
    dim = rough - 2 + int(np.argmax(counts[rough-2:rough+3]))
    if support[dim]*100 < ratios.size*53 or support[dim] <= support[100]:
        return (), None
    expected = (backdrop.astype(np.uint16) * dim // 100).astype(np.uint8)
    residual = channel_max(cv2.absdiff(rgb, expected))
    # A modal attenuates chrome as well as the main canvas. Require witnesses
    # at opposite edges so a simple page background change cannot mimic it.
    band = max(1, rgb.shape[0] // 30)
    undimmed = channel_max(cv2.absdiff(rgb, backdrop))
    for span in (slice(None, band), slice(-band, None)):
        side, original = residual[span], undimmed[span]
        if (3 * np.count_nonzero(side <= 7) < 2 * side.size
                or 3 * int(side.sum()) >= 2 * int(original.sum())):
            return (), None
    mask = cv2.morphologyEx(cv2.compare(residual, 13, cv2.CMP_GT), cv2.MORPH_OPEN,
                            np.ones((5, 5), np.uint8))
    height, width = residual.shape
    boxes = []
    for x, y, w, h, count in components(mask):
        if w*h*35 < height*width or count*5 < w*h*4:
            continue
        box = tuple(map(int, (x, y, w, h)))
        if not (x and y and x+w < width and y+h < height):
            continue
        # A removed panel exposes the same scrim/background as its surroundings.
        pixels = crop(rgb, box)
        near = rgb[max(0, y-6):min(height, y+h+6), max(0, x-6):min(width, x+w+6)]
        outside = np.concatenate((near[:4].reshape(-1, 3), near[-4:].reshape(-1, 3),
                                  near[:, :4].reshape(-1, 3), near[:, -4:].reshape(-1, 3)))
        inside_color = np.sort(pixels[::4, ::4].reshape(-1, 3), axis=0)
        outside_color = np.sort(outside, axis=0)
        if color_distance(tuple(map(int, inside_color[len(inside_color)//2])),
                          tuple(map(int, outside_color[len(outside_color)//2]))) >= 12:
            boxes.append(box)
    raster = (0, 0, width, height)
    candidates = []
    for row in proposals(rgb, raster):
        box = row.box
        if not row.rim or 28*area(box) < width*height:
            continue
        x, y, w, h = box
        inside_delta = residual[y+h//5:y+4*h//5, x+w//5:x+4*w//5]
        if 2*np.count_nonzero(inside_delta >= 12) >= inside_delta.size:
            candidates.append(align_cover(residual, box))
    fitted = []
    for candidate in sorted(candidates, key=area, reverse=True):
        if not any(area(overlap(candidate, old))*2 >= area(candidate) for old in fitted):
            fitted.append(candidate)
    for box in boxes:
        if not any(3*area(overlap(box, candidate)) >= area(box) for candidate in fitted):
            fitted.append(align_cover(residual, box))
    return tuple(fitted), expected


def detect(rgb, previous=None, prior=(), marks=(), *, alternative=None):
    height, width = rgb.shape[:2]
    raster = (0, 0, width, height)
    covers, expected = scrim_cover(rgb, previous)
    if alternative is not None:
        alternate_covers, alternate_expected = scrim_cover(rgb, alternative[0])
        if alternate_covers:
            # Compare the scrim fits on their common visible pixels only.
            free = np.ones((height, width), bool)
            for box in (*covers, *alternate_covers):
                crop(free, box)[:] = False
            if not covers or (free.any()
                              and cv2.absdiff(rgb, alternate_expected)[free].sum()
                              < cv2.absdiff(rgb, expected)[free].sum()):
                covers, expected, prior = alternate_covers, alternate_expected, alternative[1]
    if not covers:
        rows = local(rgb, raster, marks=marks)
    else:
        residual = channel_max(cv2.absdiff(rgb, expected))
        rows = [row for cover in covers
                for row in local(rgb, cover, cover=True, marks=marks, modal_residual=residual)]
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        old_edges = channel_max(cv2.morphologyEx(expected, cv2.MORPH_GRADIENT, kernel))
        new_edges = channel_max(cv2.morphologyEx(rgb, cv2.MORPH_GRADIENT, kernel))
        for row in prior:
            fragments = row.visible
            for cover in covers:
                fragments = tuple(part for box in fragments for part in visible_boxes(box, cover))
            if row.kind == "menu" and row.container:
                # A removed popover can have a similar matte to its backdrop.
                # Require its perimeter edges to survive the scrim as well.
                x, y, w, h = row.bbox
                border = ((x, y, w, 3), (x, y+h-3, w, 3),
                          (x, y, 3, h), (x+w-3, y, 3, h))
                perimeter = [overlap(part, edge) for part in fragments for edge in border]
                supported = expected_edges = 0
                for part in perimeter:
                    if area(part):
                        expected_pixels = crop(old_edges, part) >= 6
                        observed = (crop(new_edges, part) >= 6) & (crop(residual, part) <= 9)
                        expected_edges += int(np.count_nonzero(expected_pixels))
                        supported += int(np.count_nonzero(expected_pixels & observed))
                if not expected_edges or supported * 2 < expected_edges:
                    continue
            if not fragments or sum(np.count_nonzero(crop(residual, part) <= 13)
                                    for part in fragments) * 2 < sum(map(area, fragments)):
                continue
            ink = row.ink
            if ink and any(area(overlap(ink, cover)) for cover in covers):
                ink = None
            if ink and np.count_nonzero(crop(residual, ink) <= 13)*3 < area(ink)*2:
                ink = None
            rows.append(replace(row, layer="parent", visible=fragments, ink=ink))
    return sorted(rows, key=lambda row: (row.layer, row.bbox, row.kind))
