"""Sub-tile motion search variant using 4x4 block search for V2 claim-only post-pass.

Algorithm:
1. Tile the 3x3 neighborhood of tile_xy with 4x4 blocks clipped at frame boundaries.
2. Identify changed 4x4 blocks between prev_luma and cur_luma.
3. Candidate displacements (dx, dy) within max_d are sorted by increasing distance
   d^2 = dx^2 + dy^2, with tie-breaking order abs(dx), abs(dy), dx, dy.
4. Pad prev_luma by max_d with mode="edge" for candidate block matching.
5. Find the first candidate vector under which every changed 4x4 block matches exactly.
6. Apply the hard edge rule (EDGE_RULE) to the target tile tile_xy:
   - Target tile clipped at frame edge must not equal prev_luma at zero shift.
   - Target tile source in prev_luma must lie entirely within frame bounds.
   - Target tile clipped at frame edge must equal prev_luma translated by (dx, dy).
   If hard rule passes, return (dx, dy, n_blocks); otherwise return None.

OPS Counter:
Module-level integer `OPS` counts total elementary pixel comparison operations across
4x4 block diffs, candidate block matches, and hard rule tile comparisons. Unit: pixels compared.
Use `reset_ops()` to reset the counter to zero.
"""

import numpy as np
from visioncore.layout import grid_params

OPS = 0


def reset_ops():
    """Reset the module-level OPS counter to zero."""
    global OPS
    OPS = 0


def subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d):
    """Find sub-tile integer translation (dx, dy) for changed 4x4 blocks in tile_xy's neighborhood.

    Args:
        prev_luma (np.ndarray): 2D array (H, W) of previous frame luma values.
        cur_luma (np.ndarray): 2D array (H, W) of current frame luma values.
        s (int): Tile size in pixels (e.g. 16, 32, 64).
        tile_xy (tuple): (tx, ty) tile grid index.
        max_d (int): Maximum search displacement in pixels (abs(dx) <= max_d, abs(dy) <= max_d).

    Returns:
        tuple (dx, dy, n_blocks) or None:
            dx, dy: Integer pixel translation from prev to cur frame.
            n_blocks: Number of changed 4x4 blocks in the 3x3 neighborhood.
            Returns None if no blocks changed or no valid translation satisfies the algorithm and hard rule.
    """
    global OPS

    prev_luma = np.asarray(prev_luma)
    cur_luma = np.asarray(cur_luma)
    H, W = cur_luma.shape

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

    # Identify changed 4x4 blocks in the union region (clipped at frame edge)
    changed_blocks = []
    for by in range(y0, y1, 4):
        by_end = min(H, by + 4)
        for bx in range(x0, x1, 4):
            bx_end = min(W, bx + 4)
            cur_blk = cur_luma[by:by_end, bx:bx_end]
            prev_blk = prev_luma[by:by_end, bx:bx_end]
            blk_size = (by_end - by) * (bx_end - bx)
            OPS += blk_size
            if not np.array_equal(cur_blk, prev_blk):
                changed_blocks.append((bx, by, bx_end, by_end))

    n_blocks = len(changed_blocks)
    if n_blocks == 0:
        return None

    # Target tile bounds clipped at frame edge
    ty0 = ty * s
    ty1 = min(H, (ty + 1) * s)
    tx0 = tx * s
    tx1 = min(W, (tx + 1) * s)
    cur_tile = cur_luma[ty0:ty1, tx0:tx1]
    prev_tile_zero = prev_luma[ty0:ty1, tx0:tx1]
    tile_size = (ty1 - ty0) * (tx1 - tx0)

    # Pad prev_luma by max_d with mode="edge" for candidate search
    prev_padded = np.pad(prev_luma, max_d, mode="edge")

    # Candidate displacements (dx, dy) with abs(dx) <= max_d, abs(dy) <= max_d, excluding (0, 0)
    candidates = []
    for dx in range(-max_d, max_d + 1):
        for dy in range(-max_d, max_d + 1):
            if dx == 0 and dy == 0:
                continue
            candidates.append((dx, dy))

    # Sort candidates by distance and deterministic tie-breaking
    candidates.sort(key=lambda p: (p[0] ** 2 + p[1] ** 2, abs(p[0]), abs(p[1]), p[0], p[1]))

    cur_blks = [cur_luma[by:by_end, bx:bx_end] for bx, by, bx_end, by_end in changed_blocks]

    for dx, dy in candidates:
        match = True
        for (bx, by, bx_end, by_end), cur_blk in zip(changed_blocks, cur_blks):
            py0 = by - dy + max_d
            py1 = by_end - dy + max_d
            px0 = bx - dx + max_d
            px1 = bx_end - dx + max_d
            prev_blk = prev_padded[py0:py1, px0:px1]
            blk_size = (by_end - by) * (bx_end - bx)
            OPS += blk_size
            if not np.array_equal(cur_blk, prev_blk):
                match = False
                break

        if match:
            # Candidate matches all changed 4x4 blocks; now apply Hard Rule (EDGE_RULE) to target tile
            # 1. Target tile must not equal prev_luma at zero shift
            OPS += tile_size
            if np.array_equal(cur_tile, prev_tile_zero):
                return None

            # 2. Whole source tile must lie inside frame
            spy0 = ty0 - dy
            spy1 = ty1 - dy
            spx0 = tx0 - dx
            spx1 = tx1 - dx
            if spy0 < 0 or spy1 > H or spx0 < 0 or spx1 > W:
                return None

            # 3. Whole destination tile must equal translated prev_luma
            prev_tile_trans = prev_luma[spy0:spy1, spx0:spx1]
            OPS += tile_size
            if np.array_equal(cur_tile, prev_tile_trans):
                return (int(dx), int(dy), int(n_blocks))
            else:
                return None

    return None
