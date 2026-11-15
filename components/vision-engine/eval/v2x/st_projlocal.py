"""Local separable projections sub-tile motion algorithm for V2 claim-only post-pass.

Provides subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d) to estimate sub-tile
integer translation (dx, dy) using local 1D row and column projections of changed pixels
within a 3x3 tile neighborhood.

Algorithm details:
1. Identify the 3x3 tile neighborhood around tile_xy = (tx, ty).
2. Restrict the neighborhood to rows and columns that contain changed pixels between
   prev_luma and cur_luma.
3. Compute 1D row projections (summing across changed columns) and column projections
   (summing across changed rows) for cur_luma and for prev_luma shifted by candidate
   displacements dy and dx within [-max_d, max_d].
4. Search dy and dx separately by integer Sum of Absolute Differences (SAD) between 1D projections.
5. Take the 3 best dy candidates and 3 best dx candidates (ordered by SAD and deterministic
   distance tie-breakers), forming up to 9 combined candidate pairs (dx, dy).
6. Evaluate candidate pairs in total projection cost order against the EDGE_RULE hard rule:
   - The destination tile must not equal prev_luma at zero shift (dx=0, dy=0).
   - The whole destination tile clipped at frame edge must match prev_luma translated by (dx, dy)
     (padded at frame boundary with edge mode for shifts within max_d).
7. If several distinct candidate vectors tie for the minimum total projection cost among
   valid candidates that satisfy the hard rule, return None due to ambiguity.
8. Otherwise, return (dx, dy, n), where n is the number of changed pixels in the 3x3 neighborhood.

Work Counting (OPS):
- Exposes module-level integer `OPS` and `reset_ops()`.
- Every call increments `OPS` by the number of elementary element operations performed,
  counted as the total size of arrays compared, transformed, or reduced.
"""

import numpy as np
from visioncore.layout import grid_params

OPS = 0


def reset_ops():
    """Reset the global OPS counter to zero."""
    global OPS
    OPS = 0


def subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d):
    """Find sub-tile integer translation (dx, dy) using local separable projections.

    Args:
        prev_luma (np.ndarray): 2D array (H, W) of previous frame luma values.
        cur_luma (np.ndarray): 2D array (H, W) of current frame luma values.
        s (int): Tile size in pixels (e.g., 16, 32).
        tile_xy (tuple): (tx, ty) tile grid index.
        max_d (int): Maximum search displacement in pixels (abs(dx) <= max_d, abs(dy) <= max_d).

    Returns:
        tuple (dx, dy, n) or None:
            dx, dy: Integer pixel translation from prev to cur frame.
            n: Number of changed pixels in the 3x3 tile neighborhood (evidence count).
            Returns None if no valid translation satisfies the hard rule or if candidates are ambiguous.
    """
    global OPS

    prev_luma = np.asarray(prev_luma)
    cur_luma = np.asarray(cur_luma)
    H, W = cur_luma.shape

    if s is None:
        s_grid, gw, gh = grid_params(W, H)
        s = s_grid
    else:
        gw = (W + s - 1) // s
        gh = (H + s - 1) // s

    tx, ty = tile_xy
    if not (0 <= tx < gw and 0 <= ty < gh):
        return None

    # Destination tile pixel bounds clipped at frame edge
    x0_tile = tx * s
    x1_tile = min(W, (tx + 1) * s)
    y0_tile = ty * s
    y1_tile = min(H, (ty + 1) * s)

    if x0_tile >= W or y0_tile >= H:
        return None

    cur_tile = cur_luma[y0_tile:y1_tile, x0_tile:x1_tile]
    prev_tile_zero = prev_luma[y0_tile:y1_tile, x0_tile:x1_tile]
    tile_ops = cur_tile.size
    OPS += tile_ops

    # Hard rule pre-check: if destination tile is unchanged at zero shift, return None
    if np.array_equal(cur_tile, prev_tile_zero):
        return None

    # 3x3 tile neighborhood bounds
    tx_min = max(0, tx - 1)
    tx_max = min(gw - 1, tx + 1)
    ty_min = max(0, ty - 1)
    ty_max = min(gh - 1, ty + 1)

    x0_neigh = tx_min * s
    x1_neigh = min(W, (tx_max + 1) * s)
    y0_neigh = ty_min * s
    y1_neigh = min(H, (ty_max + 1) * s)

    cur_neigh = cur_luma[y0_neigh:y1_neigh, x0_neigh:x1_neigh]
    prev_neigh = prev_luma[y0_neigh:y1_neigh, x0_neigh:x1_neigh]
    neigh_ops = cur_neigh.size
    OPS += neigh_ops

    diff = (cur_neigh != prev_neigh)
    n_changed = int(np.count_nonzero(diff))
    if n_changed == 0:
        return None

    # Identify rows and columns within the neighborhood that contain changed pixels
    row_mask = diff.any(axis=1)
    col_mask = diff.any(axis=0)
    OPS += diff.shape[0] + diff.shape[1]

    R = np.where(row_mask)[0] + y0_neigh
    C = np.where(col_mask)[0] + x0_neigh

    if len(R) == 0 or len(C) == 0:
        return None

    # Restricted sub-array for current frame
    cur_sub = cur_luma[np.ix_(R, C)]
    sub_ops = cur_sub.size
    OPS += sub_ops

    # Compute row projection (sum across columns) and col projection (sum across rows)
    cur_row_proj = cur_sub.sum(axis=1, dtype=np.int64)
    cur_col_proj = cur_sub.sum(axis=0, dtype=np.int64)
    OPS += 2 * sub_ops

    # Fast local window extraction and edge padding
    # Bounding window for 3x3 neighborhood + max_d margin
    y0_pad = max(0, y0_neigh - max_d)
    y1_pad = min(H, y1_neigh + max_d)
    x0_pad = max(0, x0_neigh - max_d)
    x1_pad = min(W, x1_neigh + max_d)

    pad_top = max_d - (y0_neigh - y0_pad)
    pad_bottom = max_d - (y1_pad - y1_neigh)
    pad_left = max_d - (x0_neigh - x0_pad)
    pad_right = max_d - (x1_pad - x1_neigh)

    prev_sub_window = prev_luma[y0_pad:y1_pad, x0_pad:x1_pad]
    prev_local_padded = np.pad(
        prev_sub_window,
        ((pad_top, pad_bottom), (pad_left, pad_right)),
        mode="edge"
    )
    OPS += prev_local_padded.size

    # Local index mapping into prev_local_padded
    R_local = R - y0_neigh + max_d
    C_local = C - x0_neigh + max_d

    # Search dy
    dy_candidates = []
    for dy in range(-max_d, max_d + 1):
        prev_sub_row = prev_local_padded[np.ix_(R_local - dy, C_local)]
        prev_row_proj = prev_sub_row.sum(axis=1, dtype=np.int64)
        sad_y = int(np.sum(np.abs(cur_row_proj - prev_row_proj)))
        OPS += prev_sub_row.size + 2 * len(R)
        dy_candidates.append((sad_y, abs(dy), dy))

    dy_candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    OPS += len(dy_candidates)
    best_dy = [item[2] for item in dy_candidates[:3]]
    dy_sad_map = {item[2]: item[0] for item in dy_candidates[:3]}

    # Search dx
    dx_candidates = []
    for dx in range(-max_d, max_d + 1):
        prev_sub_col = prev_local_padded[np.ix_(R_local, C_local - dx)]
        prev_col_proj = prev_sub_col.sum(axis=0, dtype=np.int64)
        sad_x = int(np.sum(np.abs(cur_col_proj - prev_col_proj)))
        OPS += prev_sub_col.size + 2 * len(C)
        dx_candidates.append((sad_x, abs(dx), dx))

    dx_candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    OPS += len(dx_candidates)
    best_dx = [item[2] for item in dx_candidates[:3]]
    dx_sad_map = {item[2]: item[0] for item in dx_candidates[:3]}

    # Combine into up to 9 candidate pairs
    pairs = []
    for dy_val in best_dy:
        for dx_val in best_dx:
            if dx_val == 0 and dy_val == 0:
                continue
            cost = dx_sad_map[dx_val] + dy_sad_map[dy_val]
            pairs.append((cost, dx_val, dy_val))

    if not pairs:
        return None

    # Sort pairs by total projection cost, then distance, then abs(dx), abs(dy), dx, dy
    pairs.sort(key=lambda p: (p[0], p[1]**2 + p[2]**2, abs(p[1]), abs(p[2]), p[1], p[2]))
    OPS += len(pairs)

    # Validate against Hard Rule: whole destination tile must match shifted previous frame
    # Sliced from local padded array around the tile
    y0_tile_pad = max(0, y0_tile - max_d)
    y1_tile_pad = min(H, y1_tile + max_d)
    x0_tile_pad = max(0, x0_tile - max_d)
    x1_tile_pad = min(W, x1_tile + max_d)

    tpad_top = max_d - (y0_tile - y0_tile_pad)
    tpad_bottom = max_d - (y1_tile_pad - y1_tile)
    tpad_left = max_d - (x0_tile - x0_tile_pad)
    tpad_right = max_d - (x1_tile_pad - x1_tile)

    tile_sub_window = prev_luma[y0_tile_pad:y1_tile_pad, x0_tile_pad:x1_tile_pad]
    tile_local_padded = np.pad(
        tile_sub_window,
        ((tpad_top, tpad_bottom), (tpad_left, tpad_right)),
        mode="edge"
    )

    tile_h, tile_w = cur_tile.shape
    valid_candidates = []
    for cost, dx_val, dy_val in pairs:
        py0_loc = max_d - dy_val
        px0_loc = max_d - dx_val
        prev_src = tile_local_padded[py0_loc:py0_loc + tile_h, px0_loc:px0_loc + tile_w]

        OPS += cur_tile.size
        if np.array_equal(cur_tile, prev_src):
            valid_candidates.append((cost, dx_val, dy_val))

    if not valid_candidates:
        return None

    # Check for ambiguity at the minimum cost level
    min_cost = valid_candidates[0][0]
    best_candidates = [c for c in valid_candidates if c[0] == min_cost]

    if len(best_candidates) > 1:
        # Check if vectors are distinct
        distinct_vectors = set((c[1], c[2]) for c in best_candidates)
        if len(distinct_vectors) > 1:
            return None

    _, best_dx_val, best_dy_val = best_candidates[0]
    return (int(best_dx_val), int(best_dy_val), int(n_changed))
