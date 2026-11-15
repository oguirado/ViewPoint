"""Sub-tile motion search prototype for vision v2.

Provides subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d) to analyze
sub-tile integer translation of 8x8 blocks within a tile and its 8-neighbors.
"""

import numpy as np
from visioncore.layout import grid_params


def subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d):
    """Find sub-tile integer translation (dx, dy) for changed 8x8 blocks in tile_xy's neighborhood.

    Args:
        prev_luma (np.ndarray): 2D array (H, W) of previous frame luma values.
        cur_luma (np.ndarray): 2D array (H, W) of current frame luma values.
        s (int): Tile size in pixels (e.g., 16, 32, 64).
        tile_xy (tuple): (tx, ty) tile grid index.
        max_d (int): Maximum search displacement in pixels (abs(dx) <= max_d, abs(dy) <= max_d).

    Returns:
        tuple (dx, dy, n_blocks) or None:
            dx, dy: Integer pixel translation from prev to cur frame.
            n_blocks: Number of changed 8x8 blocks in the union neighborhood.
            Returns None if no blocks changed or no valid translation explains all changed blocks.
    """
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

    # Pixel bounds for target tile
    tgt_x0 = tx * s
    tgt_x1 = min(W, (tx + 1) * s)
    tgt_y0 = ty * s
    tgt_y1 = min(H, (ty + 1) * s)

    # Pixel bounds for the union of tile and its 8-neighbors
    x0 = tx_min * s
    x1 = min(W, (tx_max + 1) * s)
    y0 = ty_min * s
    y1 = min(H, (ty_max + 1) * s)

    # Target tile changed pixels
    tgt_cur = cur_luma[tgt_y0:tgt_y1, tgt_x0:tgt_x1]
    tgt_prev = prev_luma[tgt_y0:tgt_y1, tgt_x0:tgt_x1]
    tgt_diff_mask = (tgt_cur != tgt_prev)

    # Uncovered background check: if the target tile has changed pixels,
    # it must contain foreground content in cur_luma (not pure uncovered background).
    # Estimate background luma value as mode of union
    union_prev = prev_luma[y0:y1, x0:x1]
    bg_val = int(np.bincount(union_prev.ravel()).argmax())

    tgt_changed_fg_cur = tgt_diff_mask & (tgt_cur != bg_val)
    if not np.any(tgt_changed_fg_cur):
        return None

    # Minimum-evidence rule for thin structures: require at least 8 changed foreground pixels in union changed blocks
    tgt_changed_fg_union = (cur_luma[y0:y1, x0:x1] != prev_luma[y0:y1, x0:x1]) & (
        (cur_luma[y0:y1, x0:x1] != bg_val) | (prev_luma[y0:y1, x0:x1] != bg_val)
    )
    if int(np.sum(tgt_changed_fg_union)) < 8:
        return None

    # Identify changed 8x8 blocks in the union region (handling partial edge blocks explicitly)
    changed_blocks = []
    for by in range(y0, y1, 8):
        by_end = min(by + 8, H)
        for bx in range(x0, x1, 8):
            bx_end = min(bx + 8, W)
            cur_blk = cur_luma[by:by_end, bx:bx_end]
            prev_blk = prev_luma[by:by_end, bx:bx_end]
            if not np.array_equal(cur_blk, prev_blk):
                changed_blocks.append((bx, by, bx_end - bx, by_end - by))

    n_blocks = len(changed_blocks)
    if n_blocks == 0:
        return None

    # Edge pad prev_luma by max_d to safely query shifted sub-block windows
    prev_padded = np.pad(prev_luma, max_d, mode="edge")

    # Candidate displacements (dx, dy) with abs(dx) <= max_d, abs(dy) <= max_d
    candidates = []
    for dx in range(-max_d, max_d + 1):
        for dy in range(-max_d, max_d + 1):
            if dx == 0 and dy == 0:
                continue
            candidates.append((dx, dy))

    # Sort candidates by distance and deterministic tie-breaking
    candidates.sort(key=lambda p: (p[0] ** 2 + p[1] ** 2, abs(p[0]), abs(p[1]), p[0], p[1]))

    # Pre-extract changed blocks from cur_luma
    cur_blks = [cur_luma[by:by + bh, bx:bx + bw] for bx, by, bw, bh in changed_blocks]

    for dx, dy in candidates:
        match = True
        for (bx, by, bw, bh), cur_blk in zip(changed_blocks, cur_blks):
            py0 = by - dy + max_d
            px0 = bx - dx + max_d
            prev_blk = prev_padded[py0:py0 + bh, px0:px0 + bw]
            if not np.array_equal(cur_blk, prev_blk):
                match = False
                break

        if not match:
            continue

        # Check that every changed pixel in target tile is explained by translation (dx, dy)
        py_shifted = tgt_y0 - dy + max_d
        px_shifted = tgt_x0 - dx + max_d
        prev_target_shifted = prev_padded[py_shifted:py_shifted + (tgt_y1 - tgt_y0),
                                          px_shifted:px_shifted + (tgt_x1 - tgt_x0)]

        if np.array_equal(tgt_cur[tgt_diff_mask], prev_target_shifted[tgt_diff_mask]):
            return (dx, dy, n_blocks)

    return None
