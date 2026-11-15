"""eval/v2x/gm_census.py - Census rank-transform global motion variant for V2 claim-only post-pass.

Design goal: At most 16.7 ms per 1920x1088 frame as TARGET, not as a measurement.

OPS Counting Unit:
Module-level integer OPS tracks the total number of elementary pixel or array element
operations performed during execution. Each operation (such as array comparisons, bitwise
transforms, subtractions, popcount lookups, or sum reductions) adds the number of elements
processed (i.e. size of the operating slice or array) to OPS.
"""

import numpy as np

OPS = 0

POPCOUNT_LUT = np.array([bin(i).count('1') for i in range(256)], dtype=np.int32)


def reset_ops():
    """Reset the module-level operation counter."""
    global OPS
    OPS = 0


def _compute_census_3x3(arr):
    """Compute 3x3 census rank-transform codes for a frame as uint8 bitmasks."""
    global OPS
    H, W = arr.shape
    if H < 3 or W < 3:
        return np.zeros((max(0, H - 2), max(0, W - 2)), dtype=np.uint8)

    center = arr[1:-1, 1:-1]
    b = np.stack([
        arr[0:-2, 0:-2] >= center,
        arr[0:-2, 1:-1] >= center,
        arr[0:-2, 2:] >= center,
        arr[1:-1, 0:-2] >= center,
        arr[1:-1, 2:] >= center,
        arr[2:, 0:-2] >= center,
        arr[2:, 1:-1] >= center,
        arr[2:, 2:] >= center,
    ], axis=0)

    elem_count = (H - 2) * (W - 2)
    OPS += 9 * elem_count

    code = np.packbits(b, axis=0, bitorder='little')[0]
    return code


def _census_hamming_cost(c_cur, c_prev, c_dx, c_dy):
    """Compute normalized Hamming cost between census-coded maps shifted by (c_dx, c_dy)."""
    global OPS
    cH, cW = c_cur.shape
    cy0, cy1 = max(0, c_dy), min(cH, cH + c_dy)
    cx0, cx1 = max(0, c_dx), min(cW, cW + c_dx)

    if cy0 >= cy1 or cx0 >= cx1:
        return 1 << 60

    py0, py1 = cy0 - c_dy, cy1 - c_dy
    px0, px1 = cx0 - c_dx, cx1 - c_dx

    cur_sub = c_cur[cy0:cy1, cx0:cx1]
    prev_sub = c_prev[py0:py1, px0:px1]

    overlap = (cy1 - cy0) * (cx1 - cx0)
    total_area = cH * cW

    xor_map = cur_sub ^ prev_sub
    pop_counts = POPCOUNT_LUT[xor_map]
    hamming_sum = int(np.sum(pop_counts))

    OPS += 3 * overlap

    return (hamming_sum * total_area) // overlap


def _full_sad_cost(cur, prev, dx, dy):
    """Compute normalized full-resolution integer SAD cost."""
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

    overlap = (cy1 - cy0) * (cx1 - cx0)
    total_area = H * W

    diff = np.abs(cur_sub - prev_sub)
    sad_sum = int(np.sum(diff))

    OPS += 3 * overlap

    return (sad_sum * total_area) // overlap


def estimate_global_motion(prev_luma, cur_luma, max_d):
    """Estimate global integer displacement (dx, dy, score) using Census rank-transform matching.

    Returns (dx, dy, score) tuple of Python ints, or None if frame is flat, periodic or ambiguous.
    """
    global OPS

    prev_arr = np.asarray(prev_luma, dtype=np.uint8)
    cur_arr = np.asarray(cur_luma, dtype=np.uint8)

    H, W = cur_arr.shape
    OPS += 2 * H * W

    # Flat frame check
    if np.max(cur_arr) == np.min(cur_arr) and np.max(prev_arr) == np.min(prev_arr):
        return None

    # Frame pyramid creation
    f0_cur, f0_prev = cur_arr, prev_arr
    f1_cur, f1_prev = cur_arr[::2, ::2], prev_arr[::2, ::2]
    f2_cur, f2_prev = cur_arr[::4, ::4], prev_arr[::4, ::4]

    # Compute census codes for pyramid levels
    c0_cur, c0_prev = _compute_census_3x3(f0_cur), _compute_census_3x3(f0_prev)
    c1_cur, c1_prev = _compute_census_3x3(f1_cur), _compute_census_3x3(f1_prev)
    c2_cur, c2_prev = _compute_census_3x3(f2_cur), _compute_census_3x3(f2_prev)

    # Search Level 2 (4x downsampled frame, coordinates scaled by /4)
    cH2, cW2 = c2_cur.shape
    limit_dy2 = min(max_d // 4, cH2 - 1)
    limit_dx2 = min(max_d // 4, cW2 - 1)

    cand_l2 = []
    for dy in range(-limit_dy2, limit_dy2 + 1):
        for dx in range(-limit_dx2, limit_dx2 + 1):
            cost = _census_hamming_cost(c2_cur, c2_prev, dx, dy)
            cand_l2.append((cost, dx, dy))

    cand_l2.sort(key=lambda item: (item[0], abs(item[1]) + abs(item[2]), item[1], item[2]))
    top_l2 = [(dx, dy) for _, dx, dy in cand_l2[:8]]

    # Search Level 1 (2x downsampled frame, coordinates scaled by /2)
    cH1, cW1 = c1_cur.shape
    limit_dy1 = min(max_d // 2, cH1 - 1)
    limit_dx1 = min(max_d // 2, cW1 - 1)

    seen_l1 = set()
    cand_l1 = []
    for c2_dx, c2_dy in top_l2:
        bdx, bdy = c2_dx * 2, c2_dy * 2
        for dy in range(bdy - 2, bdy + 3):
            for dx in range(bdx - 2, bdx + 3):
                if abs(dx) <= limit_dx1 and abs(dy) <= limit_dy1 and (dx, dy) not in seen_l1:
                    seen_l1.add((dx, dy))
                    cost = _census_hamming_cost(c1_cur, c1_prev, dx, dy)
                    cand_l1.append((cost, dx, dy))

    cand_l1.sort(key=lambda item: (item[0], abs(item[1]) + abs(item[2]), item[1], item[2]))
    top_l1 = [(dx, dy) for _, dx, dy in cand_l1[:8]]

    # Search Level 0 census (full frame census, coordinates full pixel scale)
    cH0, cW0 = c0_cur.shape
    limit_dy0 = min(max_d, cH0 - 1)
    limit_dx0 = min(max_d, cW0 - 1)

    seen_l0 = set()
    cand_l0 = []
    for c1_dx, c1_dy in top_l1:
        bdx, bdy = c1_dx * 2, c1_dy * 2
        for dy in range(bdy - 2, bdy + 3):
            for dx in range(bdx - 2, bdx + 3):
                if abs(dx) <= limit_dx0 and abs(dy) <= limit_dy0 and (dx, dy) not in seen_l0:
                    seen_l0.add((dx, dy))
                    cost = _census_hamming_cost(c0_cur, c0_prev, dx, dy)
                    cand_l0.append((cost, dx, dy))

    cand_l0.sort(key=lambda item: (item[0], abs(item[1]) + abs(item[2]), item[1], item[2]))
    top_l0 = [(dx, dy) for _, dx, dy in cand_l0[:8]]

    # Full resolution verification using integer SAD
    max_dy_full = min(max_d, H - 1)
    max_dx_full = min(max_d, W - 1)

    seen_full = set()
    full_candidates = []
    for c0_dx, c0_dy in top_l0:
        for dy in range(c0_dy - 2, c0_dy + 3):
            for dx in range(c0_dx - 2, c0_dx + 3):
                if abs(dx) <= max_dx_full and abs(dy) <= max_dy_full and (dx, dy) not in seen_full:
                    seen_full.add((dx, dy))
                    cost = _full_sad_cost(cur_arr, prev_arr, dx, dy)
                    full_candidates.append((cost, dx, dy))

    # Always test zero / small motion baseline as safety candidate
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            if abs(dx) <= max_dx_full and abs(dy) <= max_dy_full and (dx, dy) not in seen_full:
                seen_full.add((dx, dy))
                cost = _full_sad_cost(cur_arr, prev_arr, dx, dy)
                full_candidates.append((cost, dx, dy))

    if not full_candidates:
        return None

    full_candidates.sort(key=lambda item: (item[0], abs(item[1]) + abs(item[2]), item[1], item[2]))

    best_score, best_dx, best_dy = full_candidates[0]

    # Ambiguity and periodicity checks
    non_adj_scores = [
        item for item in full_candidates
        if max(abs(item[1] - best_dx), abs(item[2] - best_dy)) >= 4
    ]

    if non_adj_scores:
        second_best_score = non_adj_scores[0][0]
        if second_best_score == best_score:
            return None
        if best_score > 0 and (second_best_score - best_score) * 100 < best_score * 5:
            return None

    return (int(best_dx), int(best_dy), int(best_score))


def consensus_tiles(prev, cur, s, dx, dy):
    """Return a numpy bool array of shape (gh, gw) indicating tiles where the whole tile matches shifted content.

    gh = ceil(H / s), gw = ceil(W / s).
    True only when the whole destination tile equals the in-bounds translated previous pixels.
    Tiles whose source leaves the frame are False.
    Vectorised using numpy.
    """
    global OPS

    prev_arr = np.asarray(prev, dtype=np.uint8)
    cur_arr = np.asarray(cur, dtype=np.uint8)

    H, W = cur_arr.shape
    gh = (H + s - 1) // s
    gw = (W + s - 1) // s

    OPS += 2 * H * W

    mask = np.zeros((gh, gw), dtype=bool)

    cy0, cy1 = max(0, dy), min(H, H + dy)
    cx0, cx1 = max(0, dx), min(W, W + dx)
    if cy0 >= cy1 or cx0 >= cx1:
        return mask

    py0, py1 = cy0 - dy, cy1 - dy
    px0, px1 = cx0 - dx, cx1 - dx

    # Compare overlapping region
    valid_match = (cur_arr[cy0:cy1, cx0:cx1] == prev_arr[py0:py1, px0:px1])
    OPS += (cy1 - cy0) * (cx1 - cx0)

    match_map = np.zeros((H, W), dtype=bool)
    match_map[cy0:cy1, cx0:cx1] = valid_match

    # Pad match_map to multiples of s
    pad_h = gh * s - H
    pad_w = gw * s - W
    if pad_h > 0 or pad_w > 0:
        padded = np.pad(match_map, ((0, pad_h), (0, pad_w)), mode='constant', constant_values=False)
    else:
        padded = match_map

    # Reshape and reduce over blocks
    blocks = padded.reshape(gh, s, gw, s)
    tile_all = np.all(blocks, axis=(1, 3))
    OPS += padded.size

    # Verify tile bounds fit inside cy0..cy1 and cx0..cx1
    ty_indices = np.arange(gh)
    tx_indices = np.arange(gw)
    t_y0 = ty_indices * s
    t_y1 = np.minimum((ty_indices + 1) * s, H)
    t_x0 = tx_indices * s
    t_x1 = np.minimum((tx_indices + 1) * s, W)

    y_in_bounds = (t_y0 >= cy0) & (t_y1 <= cy1)
    x_in_bounds = (t_x0 >= cx0) & (t_x1 <= cx1)
    tile_in_bounds = y_in_bounds[:, None] & x_in_bounds[None, :]

    OPS += gh + gw + gh * gw

    return tile_all & tile_in_bounds
