# SPDX-License-Identifier: AGPL-3.0-only
"""Localized temporal proposals, with independent motion and recurrence evidence."""
import cv2
import numpy as np

from .geometry import area, channel_max, components, crop, inside, overlap


def luma(rgb):
    channels = rgb.astype(np.uint16)
    return ((77 * channels[..., 0] + 150 * channels[..., 1]
             + 29 * channels[..., 2] + 128) >> 8).astype(np.uint8)


def matching(row, candidates, *, same_kind=True):
    return next((other for other in candidates
                 if (row.kind == other.kind or not same_kind and other.kind != "text")
                 and row.layer == other.layer
                 and 3 * area(overlap(row.bbox, other.bbox))
                 >= 2 * max(area(row.bbox), area(other.bbox))), None)


def descriptor_return(a, b, first, second):
    """Ignore codec jitter and thin overlays when comparing two recent descriptors."""
    if any(matching(row, b) is None for row in a if row.kind != "text"):
        return False
    if any(matching(row, a) is None for row in b if row.kind != "text"):
        return False
    delta = channel_max(cv2.absdiff(first, second))
    for row in (*a, *b):
        if row.ink and np.count_nonzero(crop(delta, row.ink) >= 20) * 5 >= area(row.ink):
            return False
    return int(np.count_nonzero(delta >= 24)) * 250 < delta.size


def coherent_motion(before, after, region, vector, prior, current):
    """Check region translation using witnessed shifts of text and the engine vector."""
    old, new = crop(before, region), crop(after, region)
    if min(old.shape[:2]) < 8:
        return False
    proposals = {tuple(vector)}
    for a in prior:
        if a.ink is None or not area(overlap(a.ink, region)):
            continue
        for b in current:
            if b.kind != a.kind or b.ink is None or not area(overlap(b.ink, region)):
                continue
            dx, dy = b.ink[0]-a.ink[0], b.ink[1]-a.ink[1]
            if abs(a.ink[2]-b.ink[2]) <= 3 and abs(a.ink[3]-b.ink[3]) <= 2:
                proposals.add((dx, dy))
    base = int(cv2.absdiff(old, new).sum())
    for dx, dy in proposals:
        h, w = old.shape
        if not (dx or dy) or 3*abs(dx) >= w or 3*abs(dy) >= h:
            continue
        ox, oy = max(0, -dx), max(0, -dy)
        nx, ny = max(0, dx), max(0, dy)
        ww, hh = w-abs(dx), h-abs(dy)
        error = int(cv2.absdiff(old[oy:oy+hh, ox:ox+ww], new[ny:ny+hh, nx:nx+ww]).sum())
        if 3 * error * w*h < 2 * base * ww*hh:
            return True
    return False


def without_overlays(changed):
    """Remove narrow carets and compact pointer rings, retaining nearby ink."""
    mask = changed.copy()
    for x, y, w, h, _ in components(changed):
        if w <= 2 and h >= 3*w:
            mask[y:y+h, x:x+w] = 0
    joined = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    _, labels, stats, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
    for label, (x, y, w, h, count) in enumerate(stats[1:], 1):
        if not (16 <= min(w, h) <= max(w, h) <= 32
                and 4*min(w, h) >= 3*max(w, h) and 5*count < 4*w*h):
            continue
        part = (labels[y:y+h, x:x+w] == label).astype(np.uint8)
        # A click ring changes its perimeter and leaves some interior untouched.
        rim = part.copy()
        rim[h//4:3*h//4, w//4:3*w//4] = 0
        center = part[h//4:3*h//4, w//4:3*w//4]
        if 4*cv2.countNonZero(center) < 3*center.size and cv2.countNonZero(rim) >= 2*h+w:
            mask[y:y+h, x:x+w][part != 0] = 0
    return mask


def pointer_shapes(gray, changed):
    """Find active wedges with a long edge and a widening, then narrowing body."""
    _, labels, stats, _ = cv2.connectedComponentsWithStats(
        cv2.compare(gray, 230, cv2.CMP_GE), connectivity=8)
    for label, (x, y, w, h, count) in enumerate(stats[1:], 1):
        if not (8 <= h <= 24 and 4 <= w < h and count * 5 >= w*h):
            continue
        box = tuple(map(int, (x, y, w, h)))
        part = crop(labels, box) == label
        widths = np.count_nonzero(part, axis=1)
        if 4 * np.count_nonzero(part[:, :2].any(axis=1)) < 3*h:
            continue
        if 2*max(widths[0], widths[-1]) >= widths.max():
            continue
        if 2*np.count_nonzero(part & (crop(changed, box) != 0)) >= count:
            yield box


def mask_moving_pointer(changed, before, after):
    prior = tuple(pointer_shapes(before, changed))
    current = tuple(pointer_shapes(after, changed))
    moved = {box for old in prior for new in current
             if abs(old[2]-new[2]) + abs(old[3]-new[3]) <= 3 and old[:2] != new[:2]
             for box in (old, new)}
    if not moved:
        return changed
    mask = changed.copy()
    _, labels, stats, _ = cv2.connectedComponentsWithStats(changed, connectivity=8)
    for x, y, w, h in moved:
        # The pointer's connected residual can include the click halo and the
        # ink it covered. Keep separate changes beyond that local footprint.
        anchor = (x, y, w, h)
        neighborhood = (x-h-1, y-h-1, w+2*h+2, 3*h+2)
        for label in np.unique(crop(labels, anchor)):
            if not label:
                continue
            residual = tuple(map(int, stats[label, :4]))
            if inside(residual, neighborhood) and 3 * area(overlap(residual, anchor)) >= area(anchor):
                crop(mask, residual)[crop(labels, residual) == label] = 0
    return mask


def compare(before, after, prior, current, motion=None, *, transient_return=False):
    delta = channel_max(cv2.absdiff(before, after))
    height, width = delta.shape
    padded = np.pad(delta, ((0, -height % 16), (0, -width % 16)))
    tiles = padded.reshape(padded.shape[0] // 16, 16, padded.shape[1] // 16, 16)
    maxima = tiles.max(axis=(1, 3))
    changed = cv2.compare(delta, 23, cv2.CMP_GT)
    left, right = luma(before), luma(after)
    significant = without_overlays(mask_moving_pointer(changed, left, right))

    # Raw residual still supports the tile witness. Filter working evidence inside
    # regions with a coherent translation or an edit in a stable input field.
    scrolling = False
    edit_regions = []
    excluded = significant.copy()
    for rows, other in ((prior, current), (current, prior)):
        for row in rows:
            if row.kind not in {"list", "text_field"} or matching(row, other, same_kind=False) is None:
                continue
            local = crop(significant, row.bbox)
            if cv2.countNonZero(local) < 18:
                continue
            if row.kind == "text_field":
                edit_regions.append(row.bbox)
                crop(excluded, row.bbox)[:] = 0
            elif rows is prior and coherent_motion(left, right, row.bbox, (0, 0), prior, current):
                scrolling = True
                crop(excluded, row.bbox)[:] = 0

    geometry_change = False
    overlay = False
    scoped_change = set()
    for one, other in ((prior, current), (current, prior)):
        for row in one:
            partner = matching(row, other)
            if row.kind == "text":
                if partner is None and matching(row, other, same_kind=False) is not None:
                    # The same painted outline can be grouped as ink after a
                    # hover. That reclassification supplies no new label.
                    continue
                # Requiring distributed ink support rejects narrow cursor crosses
                # and single antialiased outline fragments on a long line.
                local_delta = crop(excluded, row.ink or row.bbox)
                ys, xs = np.nonzero(local_delta)
                span = int(xs.max() - xs.min() + 1) if len(xs) else 0
                local_text = not any(item.kind != "text" for item in one)
                if (len(xs) >= max(32 if local_text else 54, area(row.bbox)//8)
                    and span >= min(row.bbox[2]*3//4, 14 if local_text else 25)
                    and np.unique(ys).size >= max(6, row.bbox[3]//2)
                    and row.bbox[2] >= (14 if local_text else 24)):
                    scoped_change.add("text")
                continue

            local_delta = crop(excluded, row.bbox)
            pixels = cv2.countNonZero(local_delta)
            peer = partner or matching(row, other, same_kind=False)
            x, y, w, h = row.bbox
            inset = max(1, min(w, h)//8)
            core = local_delta[inset:h-inset, inset:w-inset]
            # Sparse compact marks are easily formed by the moving cursor.
            minimum = max(20, core.size//(2 if max(w, h) <= 2*16 else 5))
            if peer is None and core.size and cv2.countNonZero(core) >= minimum:
                geometry_change = True
                overlay |= row.kind in {"dialog", "menu"}
                scoped_change.add(row.kind)
            elif partner and row.ink and partner.ink and not edit_regions and row.kind in {
                    "button", "menu", "menu_item"}:
                common = overlap(row.ink, partner.ink)
                ink_delta = crop(excluded, common)
                changed_cols = np.count_nonzero(np.any(ink_delta, axis=0))
                if area(common) and cv2.countNonZero(ink_delta) > 90 and changed_cols >= max(18, common[2]//2):
                    scoped_change.add("label")

            if (partner and row.kind == "button" and row.fill and partner.fill
                    and not edit_regions
                    and max(abs(a-b) for a, b in zip(row.fill, partner.fill)) >= 18
                    and pixels * 3 >= area(row.bbox)):
                scoped_change.add("fill_state")

    vector = (0, 0)
    translation = False
    active = cv2.boundingRect(excluded)
    if (scoped_change or geometry_change) and min(active[2:]) >= 8 and not overlay:
        if motion is not None:
            vector = tuple(map(int, motion(right, left, 16)))
        margins = max(3, min(active[2:])//4)
        x, y, w, h = active
        left_x, upper_y = max(0, x-margins), max(0, y-margins)
        active = (left_x, upper_y, min(width, x+w+margins)-left_x,
                  min(height, y+h+margins)-upper_y)
        translation = coherent_motion(left, right, active, vector, prior, current)

    changed_count = cv2.countNonZero(excluded)
    large = changed_count * 20 >= width*height
    return {
        "boundary": bool((large or geometry_change or scoped_change)
                         and not translation and not transient_return),
        "exact_adjacent_recurrence": not bool(delta.any()),
        "descriptor_changed": bool(geometry_change or scoped_change),
        "change_kinds": sorted(scoped_change),
        "translation_supported": translation or scrolling,
        "editable_content_changed": bool(edit_regions),
        "vision_motion_xy": vector,
        "changed_pixels": cv2.countNonZero(changed),
        "dirty_tiles": int(((tiles >= 24).sum(axis=(1, 3)) >= 12).sum()),
        "total_tiles": int(maxima.size),
        "max_residual": int(maxima.max()),
    }
