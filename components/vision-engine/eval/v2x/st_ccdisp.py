"""Sub-tile motion variant using connected-component displacement.

Algorithm:
- Labels 4-connected components of foreground pixels (pixels differing from
  the neighbourhood mode luma) inside the 3x3 tile neighbourhood of both frames.
- Matches components between frames by area, bounding-box size (height, width),
  and pixel content hash.
- Computes displacement vectors (dx, dy) = (cur_pos - prev_pos) for matched component pairs.
- Requires all matched pairs touching the target tile to agree on a single displacement
  (dx, dy) with abs(dx) <= max_d and abs(dy) <= max_d.
- Applies the Hard Edge Rule: the whole destination tile (clipped at frame edge)
  must equal the previous frame translated by (dx, dy) with the whole source inside
  the frame, and the destination tile must not equal the previous frame at zero shift.

OPS counter unit:
Every call adds the number of elementary pixel/element operations performed,
counted as the size of every array compared, transformed, or reduced.
Function reset_ops() resets the OPS counter to zero.
"""

import hashlib
import numpy as np
from visioncore.layout import grid_params

OPS = 0


def reset_ops():
    """Reset the global OPS operation counter to 0."""
    global OPS
    OPS = 0


def _add_ops(count):
    global OPS
    OPS += int(count)


def _find_components(mask, luma_sub, off_y, off_x):
    """Find 4-connected components of foreground pixels in mask.

    Args:
        mask (np.ndarray): 2D boolean array of foreground pixels.
        luma_sub (np.ndarray): 2D uint8 array of luma values for mask.
        off_y (int): Global row offset.
        off_x (int): Global column offset.

    Returns:
        list of dicts containing component properties.
    """
    h_sub, w_sub = mask.shape
    visited = np.zeros((h_sub, w_sub), dtype=bool)
    _add_ops(h_sub * w_sub)

    components = []
    for r in range(h_sub):
        for c in range(w_sub):
            if mask[r, c] and not visited[r, c]:
                queue = [(r, c)]
                visited[r, c] = True
                comp_pixels = []
                while queue:
                    curr_r, curr_c = queue.pop()
                    comp_pixels.append((curr_r + off_y, curr_c + off_x))
                    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                        nr, nc = curr_r + dr, curr_c + dc
                        if 0 <= nr < h_sub and 0 <= nc < w_sub:
                            if mask[nr, nc] and not visited[nr, nc]:
                                visited[nr, nc] = True
                                queue.append((nr, nc))

                area = len(comp_pixels)
                _add_ops(area)

                ys = [p[0] for p in comp_pixels]
                xs = [p[1] for p in comp_pixels]
                min_y, max_y = min(ys), max(ys)
                min_x, max_x = min(xs), max(xs)
                bb_h = max_y - min_y + 1
                bb_w = max_x - min_x + 1

                loc_min_y, loc_max_y = min_y - off_y, max_y - off_y
                loc_min_x, loc_max_x = min_x - off_x, max_x - off_x

                patch = luma_sub[loc_min_y:loc_max_y + 1, loc_min_x:loc_max_x + 1].copy()
                comp_local_mask = np.zeros(patch.shape, dtype=bool)
                for pr, pc in comp_pixels:
                    comp_local_mask[pr - min_y, pc - min_x] = True

                patch[~comp_local_mask] = 0
                _add_ops(patch.size)

                pixel_hash = hashlib.sha256(patch.tobytes()).hexdigest()

                components.append({
                    'area': area,
                    'bbox': (min_y, max_y, min_x, max_x),
                    'bb_size': (bb_h, bb_w),
                    'hash': pixel_hash,
                    'pixels': set(comp_pixels)
                })

    return components


def subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d):
    """Find sub-tile integer translation (dx, dy, n) using connected-component displacement.

    Args:
        prev_luma (np.ndarray): 2D array (H, W) of previous frame luma values.
        cur_luma (np.ndarray): 2D array (H, W) of current frame luma values.
        s (int): Tile size in pixels (e.g., 16, 32).
        tile_xy (tuple): (tx, ty) tile grid index.
        max_d (int): Maximum search displacement in pixels (abs(dx) <= max_d, abs(dy) <= max_d).

    Returns:
        tuple (dx, dy, n) or None:
            dx, dy: Integer pixel translation from prev to cur frame.
            n: Number of matched evidence pixels / count (n >= 1).
            Returns None if no motion or invalid/ambiguous vector.
    """
    prev_luma = np.asarray(prev_luma, dtype=np.uint8)
    cur_luma = np.asarray(cur_luma, dtype=np.uint8)
    _add_ops(prev_luma.size + cur_luma.size)

    H, W = cur_luma.shape
    s_grid, gw, gh = grid_params(W, H)
    if s is None:
        s = s_grid

    tx, ty = tile_xy
    if not (0 <= tx < gw and 0 <= ty < gh):
        return None

    # Target tile clipped pixel boundaries
    x_min = tx * s
    x_max = min(W, (tx + 1) * s)
    y_min = ty * s
    y_max = min(H, (ty + 1) * s)

    cur_tile = cur_luma[y_min:y_max, x_min:x_max]
    prev_tile_zero = prev_luma[y_min:y_max, x_min:x_max]
    _add_ops(cur_tile.size * 2)

    # Check if target tile is unchanged at zero shift
    if np.array_equal(cur_tile, prev_tile_zero):
        return None

    # Determine 3x3 neighbor tile grid bounds
    tx_min = max(0, tx - 1)
    tx_max = min(gw - 1, tx + 1)
    ty_min = max(0, ty - 1)
    ty_max = min(gh - 1, ty + 1)

    nx0 = tx_min * s
    nx1 = min(W, (tx_max + 1) * s)
    ny0 = ty_min * s
    ny1 = min(H, (ty_max + 1) * s)

    prev_sub = prev_luma[ny0:ny1, nx0:nx1]
    cur_sub = cur_luma[ny0:ny1, nx0:nx1]
    _add_ops(prev_sub.size + cur_sub.size)

    # Calculate neighbourhood mode luma
    combined = np.concatenate([prev_sub.ravel(), cur_sub.ravel()])
    _add_ops(combined.size)
    counts = np.bincount(combined, minlength=256)
    _add_ops(256)
    mode_luma = int(np.argmax(counts))

    # Foreground masks (pixels differing from mode luma)
    prev_fg = (prev_sub != mode_luma)
    cur_fg = (cur_sub != mode_luma)
    _add_ops(prev_sub.size + cur_sub.size)

    if not np.any(cur_fg) and not np.any(prev_fg):
        return None

    comps_prev = _find_components(prev_fg, prev_sub, ny0, nx0)
    comps_cur = _find_components(cur_fg, cur_sub, ny0, nx0)

    if not comps_prev or not comps_cur:
        return None

    # Index comps_prev by matching signature: area, bb_h, bb_w (or bb_w if fully within neighbourhood), hash
    prev_dict = {}
    for cp in comps_prev:
        sig = (cp['area'], cp['bb_size'][0], cp['hash'])
        prev_dict.setdefault(sig, []).append(cp)

    # Find matched pairs touching the target tile
    displacements = []
    matched_pixel_count = 0

    for cc in comps_cur:
        sig = (cc['area'], cc['bb_size'][0], cc['hash'])
        matching_cps = prev_dict.get(sig, [])

        cc_touches = any(y_min <= py < y_max and x_min <= px < x_max for py, px in cc['pixels'])

        for cp in matching_cps:
            cp_touches = any(y_min <= py < y_max and x_min <= px < x_max for py, px in cp['pixels'])

            if cc_touches or cp_touches:
                dx = cc['bbox'][2] - cp['bbox'][2]
                dy = cc['bbox'][0] - cp['bbox'][0]

                if abs(dx) <= max_d and abs(dy) <= max_d and (dx != 0 or dy != 0):
                    displacements.append((dx, dy))
                    matched_pixel_count += cc['area']

    if not displacements:
        return None

    # All matched pairs touching target tile must agree on the same (dx, dy)
    first_disp = displacements[0]
    if any(disp != first_disp for disp in displacements):
        return None

    dx, dy = first_disp

    # Apply Hard Edge Rule:
    # cur_luma(y, x) == prev_luma(y - dy, x - dx)
    # The whole source area corresponding to the target tile must fall entirely inside the prev_luma frame:
    # 0 <= x - dx < W and 0 <= y - dy < H for all (x, y) in target tile [x_min:x_max, y_min:y_max].
    src_x0 = x_min - dx
    src_x1 = x_max - dx
    src_y0 = y_min - dy
    src_y1 = y_max - dy

    if src_x0 < 0 or src_x1 > W or src_y0 < 0 or src_y1 > H:
        return None

    # 2. Whole destination tile must equal translated previous frame
    prev_src = prev_luma[src_y0:src_y1, src_x0:src_x1]
    _add_ops(cur_tile.size)
    if not np.array_equal(cur_tile, prev_src):
        return None

    # 3. Destination tile must not equal previous frame at zero shift
    _add_ops(cur_tile.size)
    if np.array_equal(cur_tile, prev_tile_zero):
        return None

    n = max(1, matched_pixel_count)
    return (int(dx), int(dy), int(n))
