"""Sub-tile motion variant: exact pixel translation of changed pixels only.

Provides subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d) for V2 post-pass.
Computes changed-pixel mask over the 3x3 tile neighbourhood, forms candidate vectors
from bounding box offsets of changed pixels and pairing up to 16 changed pixels in
raster order with equal-valued previous pixels inside max_d, tests candidate vectors,
and enforces the hard edge/tile equality rule.

Unit of OPS: Total number of element/pixel operations performed (array elements
compared, transformed, or reduced across operations).
"""

import numpy as np
from visioncore.layout import grid_params

OPS = 0


def reset_ops():
    """Reset the module-level OPS counter to zero."""
    global OPS
    OPS = 0
    return 0


def subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d):
    """Find sub-tile integer translation (dx, dy) for changed pixels in tile_xy.

    Args:
        prev_luma (np.ndarray): 2D array (H, W) of previous frame luma values.
        cur_luma (np.ndarray): 2D array (H, W) of current frame luma values.
        s (int or None): Tile size in pixels.
        tile_xy (tuple): (tx, ty) tile grid index.
        max_d (int): Maximum displacement search radius in pixels.

    Returns:
        tuple (dx, dy, n) or None:
            dx, dy: Integer pixel translation from prev to cur frame.
                    (cur_luma[y, x] == prev_luma[y - dy, x - dx])
            n: Evidence count (number of changed pixels in 3x3 neighbourhood).
            Returns None if no candidate satisfies conditions or if ambiguous.
    """
    global OPS

    prev = np.asarray(prev_luma)
    cur = np.asarray(cur_luma)
    H, W = cur.shape

    s_grid, gw, gh = grid_params(W, H)
    if s is None:
        s = s_grid

    tx, ty = tile_xy
    if not (0 <= tx < gw and 0 <= ty < gh):
        return None

    # Determine 3x3 neighbor tile grid bounds
    tx_min = max(0, tx - 1)
    tx_max = min(gw - 1, tx + 1)
    ty_min = max(0, ty - 1)
    ty_max = min(gh - 1, ty + 1)

    # Pixel bounds for the union of tile and its 8-neighbors
    x0 = tx_min * s
    x1 = min(W, (tx_max + 1) * s)
    y0 = ty_min * s
    y1 = min(H, (ty_max + 1) * s)

    # Compute changed-pixel mask over 3x3 tile neighbourhood
    cur_neigh = cur[y0:y1, x0:x1]
    prev_neigh = prev[y0:y1, x0:x1]
    diff_mask = (cur_neigh != prev_neigh)
    OPS += diff_mask.size

    if not np.any(diff_mask):
        OPS += diff_mask.size
        return None
    OPS += diff_mask.size

    # Changed pixel coordinates in neighbourhood and frame
    changed_indices = np.argwhere(diff_mask)  # (ry, rx) in raster order
    OPS += diff_mask.size
    n_changed = len(changed_indices)
    if n_changed == 0:
        return None

    changed_y = y0 + changed_indices[:, 0]
    changed_x = x0 + changed_indices[:, 1]

    # Form candidate vectors
    candidates = []

    # Source 1: Bounding box offsets between cur changed pixels and prev changed pixels
    # Determine background color in 3x3 neighbourhood
    unchanged_mask = ~diff_mask
    OPS += diff_mask.size
    if np.any(unchanged_mask):
        OPS += diff_mask.size
        unchanged_vals = cur_neigh[unchanged_mask]
        OPS += unchanged_vals.size
        vals, counts = np.unique(unchanged_vals, return_counts=True)
        OPS += unchanged_vals.size
        bg_val = vals[np.argmax(counts)]
    else:
        vals, counts = np.unique(cur_neigh, return_counts=True)
        OPS += cur_neigh.size
        bg_val = vals[np.argmax(counts)]

    cur_vals = cur[changed_y, changed_x]
    prev_vals = prev[changed_y, changed_x]
    OPS += 2 * n_changed

    cur_fg_mask = (cur_vals != bg_val)
    prev_fg_mask = (prev_vals != bg_val)
    OPS += 2 * n_changed

    cur_fg_y = changed_y[cur_fg_mask]
    cur_fg_x = changed_x[cur_fg_mask]
    prev_fg_y = changed_y[prev_fg_mask]
    prev_fg_x = changed_x[prev_fg_mask]

    if len(cur_fg_y) > 0 and len(prev_fg_y) > 0:
        cur_min_y, cur_max_y = int(cur_fg_y.min()), int(cur_fg_y.max())
        cur_min_x, cur_max_x = int(cur_fg_x.min()), int(cur_fg_x.max())
        prev_min_y, prev_max_y = int(prev_fg_y.min()), int(prev_fg_y.max())
        prev_min_x, prev_max_x = int(prev_fg_x.min()), int(prev_fg_x.max())
        OPS += len(cur_fg_y) * 2 + len(prev_fg_y) * 2

        dy1 = cur_min_y - prev_min_y
        dx1 = cur_min_x - prev_min_x
        dy2 = cur_max_y - prev_max_y
        dx2 = cur_max_x - prev_max_x

        for dx, dy in ((dx1, dy1), (dx2, dy2)):
            if abs(dx) <= max_d and abs(dy) <= max_d and (dx != 0 or dy != 0):
                candidates.append((dx, dy))

    # Source 2: Pair first 16 changed pixels in raster order with equal-valued prev pixels inside max_d
    max_pts = min(16, n_changed)
    for i in range(max_pts):
        cy = int(changed_y[i])
        cx = int(changed_x[i])
        val = cur[cy, cx]

        py_min = max(0, cy - max_d)
        py_max = min(H - 1, cy + max_d)
        px_min = max(0, cx - max_d)
        px_max = min(W - 1, cx + max_d)

        prev_win = prev[py_min:py_max + 1, px_min:px_max + 1]
        OPS += prev_win.size

        m_rys, m_rxs = np.where(prev_win == val)
        OPS += prev_win.size

        dys = cy - (py_min + m_rys)
        dxs = cx - (px_min + m_rxs)
        OPS += len(dys) * 2

        valid = (np.abs(dxs) <= max_d) & (np.abs(dys) <= max_d) & ((dxs != 0) | (dys != 0))
        OPS += len(dys)

        for dx, dy in zip(dxs[valid], dys[valid]):
            candidates.append((int(dx), int(dy)))

    # Deduplicate candidates deterministically
    unique_candidates = sorted(set(candidates), key=lambda p: (p[0]**2 + p[1]**2, abs(p[0]), abs(p[1]), p[0], p[1]))

    # Target tile bounds (clipped at frame boundary)
    dtile_x0 = tx * s
    dtile_x1 = min(W, (tx + 1) * s)
    dtile_y0 = ty * s
    dtile_y1 = min(H, (ty + 1) * s)

    cur_tile = cur[dtile_y0:dtile_y1, dtile_x0:dtile_x1]
    prev_tile = prev[dtile_y0:dtile_y1, dtile_x0:dtile_x1]
    OPS += cur_tile.size

    # Hard Rule check: tile must NOT be equal to prev frame at zero shift
    if np.array_equal(cur_tile, prev_tile):
        return None

    valid_vectors = []

    for dx, dy in unique_candidates:
        # Candidate Test: Every changed pixel in 3x3 neighbourhood must equal translated prev pixel
        pys = changed_y - dy
        pxs = changed_x - dx
        OPS += n_changed * 2

        in_bounds = (pys >= 0) & (pys < H) & (pxs >= 0) & (pxs < W)
        OPS += n_changed

        if not np.all(in_bounds):
            OPS += n_changed
            continue
        OPS += n_changed

        cur_match = cur[changed_y, changed_x]
        prev_match = prev[pys, pxs]
        OPS += 2 * n_changed

        if not np.array_equal(cur_match, prev_match):
            continue

        # Hard Rule on destination tile:
        # Whole destination tile must equal translated previous frame with source inside frame.
        src_x0 = dtile_x0 - dx
        src_x1 = dtile_x1 - dx
        src_y0 = dtile_y0 - dy
        src_y1 = dtile_y1 - dy

        if not (0 <= src_x0 and src_x1 <= W and 0 <= src_y0 and src_y1 <= H):
            continue

        prev_src_tile = prev[src_y0:src_y1, src_x0:src_x1]
        OPS += cur_tile.size

        if np.array_equal(cur_tile, prev_src_tile):
            valid_vectors.append((int(dx), int(dy)))

    if len(valid_vectors) == 1:
        v_dx, v_dy = valid_vectors[0]
        return (v_dx, v_dy, int(n_changed))

    # If 0 or >1 distinct valid vectors, return None
    return None
