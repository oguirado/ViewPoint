"""Exact 8x8 subtile search with a target-local exposure guard.

The search and displacement tie-break follow ``eval.v2proto_subtile``. A
neighbour's motion is insufficient evidence: every changed target block must
match, and at least half of the target's changed pixels must be destinations
of non-background source pixels. Inputs, views and padding stay in RAM.
"""

from operator import index

import numpy as np

from visioncore.layout import grid_params


def _changed_blocks(prev, cur, x0, y0, x1, y1):
    """Return current block views, including each region's partial edges."""
    blocks = []
    for by in range(y0, y1, 8):
        by_end = min(by + 8, y1)
        for bx in range(x0, x1, 8):
            bx_end = min(bx + 8, x1)
            block = cur[by:by_end, bx:bx_end]
            if not np.array_equal(block, prev[by:by_end, bx:bx_end]):
                blocks.append((bx, by, block))
    return blocks


def _blocks_match(prev_padded, blocks, dx, dy, pad):
    for bx, by, block in blocks:
        height, width = block.shape
        px, py = bx - dx + pad, by - dy + pad
        if not np.array_equal(block, prev_padded[py:py + height, px:px + width]):
            return False
    return True


def subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d):
    """Return ``(dx, dy, n_blocks)`` for supported motion, otherwise ``None``.

    Luma inputs are read-only uint8 arrays of shape (H, W). Positive dx/dy
    moves right/down; tile_xy is (tx, ty). The search covers nonzero vectors
    with both absolute components at most max_d. ``n_blocks`` counts changed
    8x8 blocks in the target's clipped 3x3 tile neighbourhood, as in the
    prototype. Partial target blocks are checked at their actual extents.

    Background is the mode of the previous neighbourhood (the lowest luma
    wins a tie). A changed target pixel counts as a destination only when its
    source under the candidate vector differs from that mode. Exactly half
    is sufficient; a majority of exposed background is rejected. The
    prototype's eight-pixel neighbourhood evidence floor and edge padding
    are retained. A missing previous frame or unchanged target cannot claim.
    """
    if prev_luma is None or cur_luma is None:
        return None
    prev = np.asarray(prev_luma)
    cur = np.asarray(cur_luma)
    if prev.ndim != 2 or cur.ndim != 2 or prev.shape != cur.shape:
        raise ValueError("luma frames must have the same two-dimensional shape")
    height, width = cur.shape
    if not height or not width:
        return None

    s = grid_params(width, height)[0] if s is None else index(s)
    if s <= 0:
        raise ValueError("tile size must be positive")
    max_d = index(max_d)
    if max_d <= 0:
        return None

    # Explicit s controls both tile bounds and grid bounds.
    gw, gh = (width + s - 1) // s, (height + s - 1) // s
    tx, ty = (index(coordinate) for coordinate in tile_xy)
    if not (0 <= tx < gw and 0 <= ty < gh):
        return None
    tx0, ty0 = tx * s, ty * s
    tx1, ty1 = min(width, tx0 + s), min(height, ty0 + s)
    x0, y0 = max(0, tx - 1) * s, max(0, ty - 1) * s
    x1, y1 = min(width, (tx + 2) * s), min(height, (ty + 2) * s)

    target_cur = cur[ty0:ty1, tx0:tx1]
    target_changed = target_cur != prev[ty0:ty1, tx0:tx1]
    changed_count = int(np.count_nonzero(target_changed))
    if changed_count == 0:
        return None

    union_prev, union_cur = prev[y0:y1, x0:x1], cur[y0:y1, x0:x1]
    background = int(np.bincount(union_prev.ravel(), minlength=256).argmax())
    if not np.any(target_changed & (target_cur != background)):
        return None
    union_support = (union_cur != union_prev) & (
        (union_cur != background) | (union_prev != background)
    )
    if int(np.count_nonzero(union_support)) < 8:
        return None

    target_blocks = _changed_blocks(prev, cur, tx0, ty0, tx1, ty1)
    union_blocks = _changed_blocks(prev, cur, x0, y0, x1, y1)
    n_blocks = len(union_blocks)
    if not target_blocks or not n_blocks:
        return None

    # Padding is the prototype's RAM-only edge extension; inputs are untouched.
    prev_padded = np.pad(prev, max_d, mode="edge")
    candidates = [
        (dx, dy)
        for dx in range(-max_d, max_d + 1)
        for dy in range(-max_d, max_d + 1)
        if dx != 0 or dy != 0
    ]
    candidates.sort(
        key=lambda p: (p[0] ** 2 + p[1] ** 2, abs(p[0]), abs(p[1]), p[0], p[1])
    )

    for dx, dy in candidates:
        # Check entire changed blocks, including their unchanged pixels. Build
        # target blocks separately so neighbour alignment cannot omit an edge.
        if not _blocks_match(prev_padded, target_blocks, dx, dy, max_d):
            continue
        if not _blocks_match(prev_padded, union_blocks, dx, dy, max_d):
            continue

        px, py = tx0 - dx + max_d, ty0 - dy + max_d
        source = prev_padded[py:py + (ty1 - ty0), px:px + (tx1 - tx0)]
        destination_count = int(np.count_nonzero(target_changed & (source != background)))
        if 2 * destination_count < changed_count:
            continue
        return dx, dy, n_blocks

    return None
