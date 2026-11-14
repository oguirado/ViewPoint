"""Edge-map matching sub-tile motion algorithm for V2 post-pass.

Computes an integer edge map from absolute horizontal and vertical luma
differences above a fixed threshold of 24 over the frame neighborhood.
Scores candidate translation vectors within max_d by the count of coincident
edge pixels minus non-coincident ones, selects the top 4 candidate vectors,
and enforces the strict EDGE_RULE in score order.

Unit of OPS:
    OPS counts element-wise array operations (comparisons, arithmetic differences,
    logical operations, and array reductions) equal to the size of the arrays processed.
"""

import numpy as np
from visioncore.layout import grid_params

OPS = 0


def reset_ops():
    """Reset the global OPS counter to zero."""
    global OPS
    OPS = 0


def _add_ops(count):
    global OPS
    OPS += int(count)


def _compute_edge_map(luma):
    """Compute binary edge map (1 where abs(dh) + abs(dv) > 24, else 0)."""
    H, W = luma.shape
    luma_i = luma.astype(np.int32)
    dh = np.zeros((H, W), dtype=np.int32)
    dh[:, :-1] = np.abs(luma_i[:, 1:] - luma_i[:, :-1])
    dv = np.zeros((H, W), dtype=np.int32)
    dv[:-1, :] = np.abs(luma_i[1:, :] - luma_i[:-1, :])

    edge_sum = dh + dv
    edge_map = (edge_sum > 24).astype(np.int32)

    # Operations counted: 2 diffs + 1 sum + 1 threshold compare = 4 * H * W ops
    _add_ops(H * W * 4)
    return edge_map


def subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d):
    """Find sub-tile integer translation (dx, dy) using edge-map matching.

    Args:
        prev_luma (np.ndarray): 2D array (H, W) of previous frame luma values.
        cur_luma (np.ndarray): 2D array (H, W) of current frame luma values.
        s (int): Tile size in pixels (e.g., 16, 32).
        tile_xy (tuple): (tx, ty) tile grid index.
        max_d (int): Maximum search displacement in pixels (abs(dx) <= max_d, abs(dy) <= max_d).

    Returns:
        tuple (dx, dy, n) or None:
            dx, dy: Integer pixel translation from prev to cur frame.
            n: Evidence count (number of coincident edge pixels, at least 1).
            Returns None if no motion vector satisfies the hard edge rule or if evidence is ambiguous.
    """
    prev_luma = np.asarray(prev_luma)
    cur_luma = np.asarray(cur_luma)
    H, W = cur_luma.shape
    _add_ops(prev_luma.size + cur_luma.size)

    s_grid, gw, gh = grid_params(W, H)
    if s is None:
        s = s_grid

    tx, ty = tile_xy
    if not (0 <= tx < gw and 0 <= ty < gh):
        return None

    # Destination tile bounds clipped at frame edges
    x0 = tx * s
    x1 = min(W, (tx + 1) * s)
    y0 = ty * s
    y1 = min(H, (ty + 1) * s)

    if x0 >= x1 or y0 >= y1:
        return None

    cur_tile = cur_luma[y0:y1, x0:x1]
    prev_tile_zero = prev_luma[y0:y1, x0:x1]
    _add_ops(cur_tile.size)

    # Hard rule condition: destination tile must NOT equal previous frame at zero shift
    if np.array_equal(cur_tile, prev_tile_zero):
        return None

    # Compute edge maps for both frames
    cur_edge_map = _compute_edge_map(cur_luma)
    prev_edge_map = _compute_edge_map(prev_luma)

    cur_edge_tile = cur_edge_map[y0:y1, x0:x1]

    # Evaluate candidate vectors (dx, dy) within max_d
    candidates = []
    for dx in range(-max_d, max_d + 1):
        for dy in range(-max_d, max_d + 1):
            if dx == 0 and dy == 0:
                continue

            sx0, sx1 = x0 - dx, x1 - dx
            sy0, sy1 = y0 - dy, y1 - dy

            # Source region must be entirely inside the previous frame
            if not (0 <= sx0 and sx1 <= W and 0 <= sy0 and sy1 <= H):
                continue

            prev_edge_tile = prev_edge_map[sy0:sy1, sx0:sx1]

            # Coincident edge pixels & non-coincident edge pixels
            coincident = np.sum((cur_edge_tile == 1) & (prev_edge_tile == 1))
            non_coincident = np.sum((cur_edge_tile == 1) ^ (prev_edge_tile == 1))
            _add_ops(cur_edge_tile.size * 4)

            score = int(coincident - non_coincident)
            candidates.append((score, int(coincident), dx, dy))

    if not candidates:
        return None

    # Sort candidates by score descending, distance ascending, then deterministic dx, dy
    candidates.sort(key=lambda c: (c[0], -(c[2]**2 + c[3]**2), -abs(c[2]), -abs(c[3]), -c[2], -c[3]), reverse=True)

    # Keep top 4 candidates
    top_4 = candidates[:4]

    # Evaluate Hard Rule on top 4 candidates in score order
    passing_candidates = []
    for score, coincident, dx, dy in top_4:
        sx0, sx1 = x0 - dx, x1 - dx
        sy0, sy1 = y0 - dy, y1 - dy

        prev_source = prev_luma[sy0:sy1, sx0:sx1]
        _add_ops(cur_tile.size)

        if np.array_equal(cur_tile, prev_source):
            passing_candidates.append((score, coincident, dx, dy))

    if not passing_candidates:
        return None

    # Filter/rank passing candidates: must have positive edge evidence (coincident >= 1)
    # and highest score must be unique among passing candidates
    max_pass_score = passing_candidates[0][0]
    top_passes = [c for c in passing_candidates if c[0] == max_pass_score]

    if len(top_passes) > 1:
        # Ambiguous: multiple vectors pass with the same top score
        return None

    best_score, best_coincident, best_dx, best_dy = top_passes[0]
    if best_coincident < 1:
        # No coincident edge pixels to support motion evidence
        return None

    return (int(best_dx), int(best_dy), int(best_coincident))
