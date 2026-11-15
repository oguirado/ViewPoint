"""eval/v2x/gm_corners.py - Global motion estimation via sparse corner voting.

Design Goal:
    At most 16.7 ms per 1920x1088 frame as TARGET.

OPS Unit Description:
    OPS (operations) counts the total number of elementary array element accesses,
    comparisons, arithmetic operations, and reductions performed during motion
    estimation and tile consensus calculations.

Algorithm:
    Computes an integer Harris-like corner response from box-summed products of
    integer gradients, selects up to 256 strongest corners in deterministic raster
    order on ties with non-maximum suppression radius 4, hashes the 8x8 patch
    around each corner in both frames, builds a dictionary from previous-frame
    patch hash to unique positions over all corners and over a dense stride-2 scan
    when needed, votes the displacement of every unique hash match, takes the mode
    with at least 6 votes and a margin over runner-up candidates, and verifies
    with integer SAD over the overlap region.
"""

import math
import numpy as np

OPS = 0


def reset_ops():
    """Reset global operation counter OPS."""
    global OPS
    OPS = 0


def _box_sum_5x5(arr):
    global OPS
    row_sum = arr[:-4, :] + arr[1:-3, :] + arr[2:-2, :] + arr[3:-1, :] + arr[4:, :]
    box = row_sum[:, :-4] + row_sum[:, 1:-3] + row_sum[:, 2:-2] + row_sum[:, 3:-1] + row_sum[:, 4:]
    res = np.zeros_like(arr)
    res[2:-2, 2:-2] = box
    OPS += arr.size * 8
    return res


def _detect_corners(img, max_corners=256, nms_r=4):
    global OPS
    H, W = img.shape
    if H < 10 or W < 10:
        return []

    img_i = img.astype(np.int32)
    I_x = np.zeros((H, W), dtype=np.int32)
    I_y = np.zeros((H, W), dtype=np.int32)

    I_x[1:-1, 1:-1] = img_i[1:-1, 2:] - img_i[1:-1, :-2]
    I_y[1:-1, 1:-1] = img_i[2:, 1:-1] - img_i[:-2, 1:-1]

    I_xx = I_x * I_x
    I_yy = I_y * I_y
    I_xy = I_x * I_y
    OPS += img.size * 5

    S_xx = _box_sum_5x5(I_xx.astype(np.int64))
    S_yy = _box_sum_5x5(I_yy.astype(np.int64))
    S_xy = _box_sum_5x5(I_xy.astype(np.int64))

    det = S_xx * S_yy - S_xy * S_xy
    trace = S_xx + S_yy
    R = det - ((trace * trace) >> 4)
    OPS += img.size * 4

    R[:nms_r, :] = 0
    R[-nms_r:, :] = 0
    R[:, :nms_r] = 0
    R[:, -nms_r:] = 0
    R[R <= 0] = 0

    ys, xs = np.nonzero(R > 0)
    if len(ys) == 0:
        return []

    scores = R[ys, xs]
    OPS += ys.size

    # Sort by (-score, y, x) for deterministic raster order on ties
    order = np.lexsort((xs, ys, -scores))
    sorted_ys = ys[order]
    sorted_xs = xs[order]

    corners = []
    suppressed = np.zeros((H, W), dtype=bool)

    for y, x in zip(sorted_ys, sorted_xs):
        if not suppressed[y, x]:
            if 4 <= y < H - 4 and 4 <= x < W - 4:
                corners.append((int(y), int(x)))
            y0, y1 = max(0, y - nms_r), min(H, y + nms_r + 1)
            x0, x1 = max(0, x - nms_r), min(W, x + nms_r + 1)
            suppressed[y0:y1, x0:x1] = True
            OPS += (y1 - y0) * (x1 - x0)
            if len(corners) >= max_corners:
                break

    return corners


def _compute_full_sad(cur, prev, dx, dy):
    global OPS
    H, W = cur.shape
    cy0, cy1 = max(0, dy), min(H, H + dy)
    cx0, cx1 = max(0, dx), min(W, W + dx)

    if cy0 >= cy1 or cx0 >= cx1:
        return 1 << 60

    py0, py1 = cy0 - dy, cy1 - dy
    px0, px1 = cx0 - dx, cx1 - dx

    cur_sub = cur[cy0:cy1, cx0:cx1].astype(np.int64)
    prev_sub = prev[py0:py1, px0:px1].astype(np.int64)

    overlap_area = (cy1 - cy0) * (cx1 - cx0)
    total_area = H * W

    sad = int(np.sum(np.abs(cur_sub - prev_sub)))
    sad_norm = (sad * total_area) // overlap_area

    OPS += overlap_area * 3
    return sad_norm


def estimate_global_motion(prev_luma, cur_luma, max_d, min_votes=6):
    """Estimate global integer displacement (dx, dy) using sparse corner voting.

    Returns (dx, dy, score) or None if flat, periodic, or ambiguous.
    """
    global OPS
    prev_arr = np.asarray(prev_luma, dtype=np.uint8)
    cur_arr = np.asarray(cur_luma, dtype=np.uint8)

    H, W = cur_arr.shape
    OPS += cur_arr.size * 2

    # Flat frame check
    if np.max(cur_arr) == np.min(cur_arr) and np.max(prev_arr) == np.min(prev_arr):
        return None

    cur_corners = _detect_corners(cur_arr, max_corners=256, nms_r=4)
    if not cur_corners:
        return None

    prev_corners = _detect_corners(prev_arr, max_corners=256, nms_r=4)

    prev_hash_dict = {}
    for py, px in prev_corners:
        patch = prev_arr[py-4:py+4, px-4:px+4]
        h = patch.tobytes()
        if h not in prev_hash_dict:
            prev_hash_dict[h] = (py, px)
        else:
            prev_hash_dict[h] = None
        OPS += 64

    votes = {}
    for cy, cx in cur_corners:
        patch = cur_arr[cy-4:cy+4, cx-4:cx+4]
        h = patch.tobytes()
        pos = prev_hash_dict.get(h)
        if pos is not None:
            py, px = pos
            dx = cx - px
            dy = cy - py
            if abs(dx) <= max_d and abs(dy) <= max_d:
                vec = (dx, dy)
                votes[vec] = votes.get(vec, 0) + 1
        OPS += 64

    top_count = max(votes.values()) if votes else 0

    if top_count < min_votes:
        for py in range(4, H - 4, 2):
            for px in range(4, W - 4, 2):
                patch = prev_arr[py-4:py+4, px-4:px+4]
                h = patch.tobytes()
                if h not in prev_hash_dict:
                    prev_hash_dict[h] = (py, px)
                elif prev_hash_dict[h] != (py, px):
                    prev_hash_dict[h] = None
                OPS += 64

        votes = {}
        for cy, cx in cur_corners:
            patch = cur_arr[cy-4:cy+4, cx-4:cx+4]
            h = patch.tobytes()
            pos = prev_hash_dict.get(h)
            if pos is not None:
                py, px = pos
                dx = cx - px
                dy = cy - py
                if abs(dx) <= max_d and abs(dy) <= max_d:
                    vec = (dx, dy)
                    votes[vec] = votes.get(vec, 0) + 1
            OPS += 64

    if not votes:
        return None

    sorted_votes = sorted(
        votes.items(),
        key=lambda item: (-item[1], abs(item[0][0]) + abs(item[0][1]), item[0][0], item[0][1])
    )

    top_vec, top_count = sorted_votes[0]
    if top_count < min_votes:
        return None

    runner_up_count = 0
    for vec, count in sorted_votes[1:]:
        if max(abs(vec[0] - top_vec[0]), abs(vec[1] - top_vec[1])) > 2:
            runner_up_count = count
            break

    if top_count - runner_up_count < 2:
        return None

    c_dx, c_dy = top_vec

    best_sad = 1 << 60
    best_vec = None

    for dy in range(c_dy - 1, c_dy + 2):
        for dx in range(c_dx - 1, c_dx + 2):
            if abs(dx) <= max_d and abs(dy) <= max_d:
                sad = _compute_full_sad(cur_arr, prev_arr, dx, dy)
                if sad < best_sad:
                    best_sad = sad
                    best_vec = (dx, dy)

    if best_vec is None or best_sad == (1 << 60):
        return None

    best_dx, best_dy = best_vec
    return (int(best_dx), int(best_dy), int(best_sad))


def consensus_tiles(prev, cur, s, dx, dy):
    """Return a boolean numpy array of shape (gh, gw) indicating tiles consistent with (dx, dy)."""
    global OPS
    prev_arr = np.asarray(prev, dtype=np.uint8)
    cur_arr = np.asarray(cur, dtype=np.uint8)

    H, W = cur_arr.shape
    gh = math.ceil(H / s)
    gw = math.ceil(W / s)

    ty = np.arange(gh)
    tx = np.arange(gw)

    in_bounds_y = (ty * s - dy >= 0) & (np.minimum((ty + 1) * s, H) - dy <= H)
    in_bounds_x = (tx * s - dx >= 0) & (np.minimum((tx + 1) * s, W) - dx <= W)
    tile_in_bounds = in_bounds_y[:, None] & in_bounds_x[None, :]

    cy0, cy1 = max(0, dy), min(H, H + dy)
    cx0, cx1 = max(0, dx), min(W, W + dx)
    py0, py1 = cy0 - dy, cy1 - dy
    px0, px1 = cx0 - dx, cx1 - dx

    valid_pixels = np.zeros((H, W), dtype=bool)
    if cy0 < cy1 and cx0 < cx1:
        valid_pixels[cy0:cy1, cx0:cx1] = (cur_arr[cy0:cy1, cx0:cx1] == prev_arr[py0:py1, px0:px1])

    padded = np.ones((gh * s, gw * s), dtype=bool)
    padded[:H, :W] = valid_pixels

    tile_all_equal = padded.reshape(gh, s, gw, s).all(axis=(1, 3))
    mask = tile_in_bounds & tile_all_equal

    OPS += H * W + gh * gw * s * s
    return mask
