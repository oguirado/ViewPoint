"""2x2 block hash lookup sub-tile motion variant for V2 claim-only post-pass.

Algorithm:
1. Hashes non-flat 2x2 blocks in the previous frame within the tile neighborhood
   expanded by max_d into a dictionary mapping 2x2 pixel tuple to previous positions.
2. Identifies all changed, non-flat 2x2 blocks in the current frame tile.
3. For each changed 2x2 block, looks up matching previous positions and votes for
   displacements (dx, dy) with abs(dx) <= max_d, abs(dy) <= max_d, and (dx, dy) != (0,0).
4. Selects the vote mode with strict margin over runner-up (if unique mode exists).
5. Validates candidate vector against the hard EDGE_RULE:
   - Destination tile (clipped at frame boundary) translated by (dx, dy) must equal
     cur_luma exactly.
   - The entire source region in prev_luma must be strictly within frame bounds.
   - The tile must not equal prev_luma at zero shift.
6. Returns (dx, dy, n) where n is the evidence count (vote count for winning vector),
   or None if no valid motion is found or if multiple distinct vectors tie.

OPS Unit:
Elementary array element operations. Each 2x2 block comparison/lookup charges 4 OPS,
tile equality checks charge (tile_height * tile_width) OPS, and hashing charges 4 OPS
per 2x2 window in the neighborhood.
"""

from collections import defaultdict
import numpy as np
from visioncore.layout import grid_params

OPS = 0


def reset_ops():
    """Reset the global elementary operation counter OPS to 0."""
    global OPS
    OPS = 0


def subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d):
    """Find sub-tile translation (dx, dy) for changed tile using 2x2 block hash lookup.

    Args:
        prev_luma (np.ndarray): 2D uint8 luma plane of previous frame (H, W).
        cur_luma (np.ndarray): 2D uint8 luma plane of current frame (H, W).
        s (int): Tile size in pixels (e.g., 16, 32).
        tile_xy (tuple): (tx, ty) tile grid coordinates.
        max_d (int): Maximum search displacement in pixels (abs(dx) <= max_d, abs(dy) <= max_d).

    Returns:
        tuple (dx, dy, n) or None:
            dx, dy: Integer pixel translation from prev to cur frame.
            n: Evidence count (number of 2x2 block votes for the selected motion vector).
            Returns None if no valid translation explains the tile or hard rule fails.
    """
    global OPS

    prev = np.asarray(prev_luma)
    cur = np.asarray(cur_luma)

    if prev.ndim != 2 or cur.ndim != 2 or prev.shape != cur.shape:
        return None

    H, W = cur.shape
    s_grid, gw, gh = grid_params(W, H)
    if s is None:
        s = s_grid

    tx, ty = tile_xy
    if not (0 <= tx < gw and 0 <= ty < gh):
        return None

    # Destination tile pixel bounds clipped to frame
    x0 = tx * s
    x1 = min(W, (tx + 1) * s)
    y0 = ty * s
    y1 = min(H, (ty + 1) * s)

    tw = x1 - x0
    th = y1 - y0

    if tw <= 0 or th <= 0:
        return None

    # Zero-shift check: tile must not equal previous frame at zero shift
    cur_tile = cur[y0:y1, x0:x1]
    prev_tile = prev[y0:y1, x0:x1]
    OPS += th * tw

    if np.array_equal(cur_tile, prev_tile):
        return None

    # Neighborhood pixel bounds in prev_luma expanded by max_d
    px0 = max(0, x0 - max_d)
    px1 = min(W, x1 + max_d)
    py0 = max(0, y0 - max_d)
    py1 = min(H, y1 + max_d)

    # Hash non-flat 2x2 blocks in prev_luma within expanded neighborhood
    hash_table = defaultdict(list)
    n_prev_blocks = 0
    for py in range(py0, py1 - 1):
        for px in range(px0, px1 - 1):
            n_prev_blocks += 1
            b00 = int(prev[py, px])
            b01 = int(prev[py, px + 1])
            b10 = int(prev[py + 1, px])
            b11 = int(prev[py + 1, px + 1])
            if b00 != b01 or b00 != b10 or b00 != b11:
                key = (b00, b01, b10, b11)
                hash_table[key].append((px, py))

    OPS += n_prev_blocks * 4

    # Identify changed, non-flat 2x2 blocks in cur_luma tile and vote displacements
    votes = defaultdict(int)
    n_cur_blocks = 0

    for by in range(y0, y1 - 1):
        for bx in range(x0, x1 - 1):
            n_cur_blocks += 1
            c00 = int(cur[by, bx])
            c01 = int(cur[by, bx + 1])
            c10 = int(cur[by + 1, bx])
            c11 = int(cur[by + 1, bx + 1])

            p00 = int(prev[by, bx])
            p01 = int(prev[by, bx + 1])
            p10 = int(prev[by + 1, bx])
            p11 = int(prev[by + 1, bx + 1])

            if (c00, c01, c10, c11) != (p00, p01, p10, p11):
                if c00 != c01 or c00 != c10 or c00 != c11:
                    cur_key = (c00, c01, c10, c11)
                    if cur_key in hash_table:
                        matches = hash_table[cur_key]
                        OPS += len(matches)
                        for px, py in matches:
                            dx = bx - px
                            dy = by - py
                            if abs(dx) <= max_d and abs(dy) <= max_d and (dx != 0 or dy != 0):
                                votes[(dx, dy)] += 1

    OPS += n_cur_blocks * 8

    if not votes:
        return None

    # Filter candidate displacements by hard EDGE_RULE:
    # Whole destination tile must equal in-bounds translated previous pixels
    valid_candidates = []
    for (dx, dy), vote_count in votes.items():
        sx0 = x0 - dx
        sx1 = x1 - dx
        sy0 = y0 - dy
        sy1 = y1 - dy

        # Hard rule requires whole source region to be inside frame bounds
        if 0 <= sx0 and sx1 <= W and 0 <= sy0 and sy1 <= H:
            OPS += th * tw
            src_tile = prev[sy0:sy1, sx0:sx1]
            if np.array_equal(cur_tile, src_tile):
                valid_candidates.append(((dx, dy), vote_count))

    if not valid_candidates:
        return None

    # Sort valid candidates by vote count descending
    valid_candidates.sort(key=lambda item: item[1], reverse=True)

    # Check vote mode and margin over runner-up
    top_vec, top_votes = valid_candidates[0]
    if len(valid_candidates) > 1:
        runner_up_votes = valid_candidates[1][1]
        if top_votes == runner_up_votes:
            return None

    return (int(top_vec[0]), int(top_vec[1]), int(top_votes))
