"""eval/v2x/gm_gradproj.py - Gradient projection global motion estimator for V2 claim-only post-pass.

Design Goal / Performance Target:
TARGET: At most 16.7 ms per 1920x1088 frame.

OPS Operational Unit:
Every elementary pixel or element operation is counted as the size (number of elements)
of every array compared, transformed, or reduced. Calling reset_ops() resets OPS to 0,
and subsequent calls to estimate_global_motion or consensus_tiles accumulate operation counts.
"""

import numpy as np

OPS = 0


def reset_ops():
    """Reset the global OPS counter to zero."""
    global OPS
    OPS = 0


def _add_ops(count):
    global OPS
    OPS += int(count)


def _compute_gradient_projections(luma_arr):
    """Compute gradient magnitude and row/column zero-mean projections.

    Gradient magnitude: |luma[y, x+1] - luma[y, x]| + |luma[y+1, x] - luma[y, x]|
    """
    H, W = luma_arr.shape
    if H < 2 or W < 2:
        return np.zeros(H, dtype=np.int64), np.zeros(W, dtype=np.int64), 0

    luma = luma_arr.astype(np.int64)
    # diff_x: (H-1, W-1), diff_y: (H-1, W-1)
    diff_x = np.abs(luma[:-1, 1:] - luma[:-1, :-1])
    _add_ops(2 * (H - 1) * (W - 1))

    diff_y = np.abs(luma[1:, :-1] - luma[:-1, :-1])
    _add_ops(2 * (H - 1) * (W - 1))

    grad = diff_x + diff_y
    _add_ops((H - 1) * (W - 1))

    max_grad = np.max(grad) if grad.size > 0 else 0
    _add_ops(grad.size)

    row_proj = np.sum(grad, axis=1)  # shape (H-1,)
    _add_ops(grad.size)

    col_proj = np.sum(grad, axis=0)  # shape (W-1,)
    _add_ops(grad.size)

    # Pad projections to H and W
    row_proj_full = np.zeros(H, dtype=np.int64)
    row_proj_full[:-1] = row_proj

    col_proj_full = np.zeros(W, dtype=np.int64)
    col_proj_full[:-1] = col_proj

    # Subtract integer mean
    r_mean = int(np.sum(row_proj_full)) // H
    _add_ops(H)
    row_zm = row_proj_full - r_mean
    _add_ops(H)

    c_mean = int(np.sum(col_proj_full)) // W
    _add_ops(W)
    col_zm = col_proj_full - c_mean
    _add_ops(W)

    return row_zm, col_zm, int(max_grad)


def _eval_1d_sad(cur_proj, prev_proj, shift):
    """Evaluate 1D SAD between cur_proj and prev_proj shifted by `shift`.

    cur_proj[i] ~ prev_proj[i - shift]
    """
    N = len(cur_proj)
    if abs(shift) >= N:
        return 1 << 60
    if shift >= 0:
        c_sub = cur_proj[shift:]
        p_sub = prev_proj[:N - shift]
    else:
        c_sub = cur_proj[:N + shift]
        p_sub = prev_proj[-shift:]
    overlap = len(c_sub)
    if overlap <= 0:
        return 1 << 60
    _add_ops(overlap * 2)
    sad = int(np.sum(np.abs(c_sub - p_sub)))
    _add_ops(overlap)
    return (sad * N) // overlap


def _search_projection_shift(cur_proj, prev_proj, max_shift):
    """Search 1D shift between cur_proj and prev_proj using coarse-to-fine decimation (8, 4, 2, 1)."""
    N = len(cur_proj)
    if N == 0 or max_shift <= 0:
        return [0], [(0, 0)]

    def downsample(proj, dec):
        if dec == 1:
            return proj
        n_blocks = len(proj) // dec
        if n_blocks == 0:
            return proj
        trimmed = proj[:n_blocks * dec].reshape(n_blocks, dec)
        _add_ops(len(proj))
        res = np.sum(trimmed, axis=1)
        _add_ops(len(proj))
        return res

    # Stage 1: D = 8
    cur_8 = downsample(cur_proj, 8)
    prev_8 = downsample(prev_proj, 8)
    max_s8 = max_shift // 8

    cand_8 = []
    for s8 in range(-max_s8, max_s8 + 1):
        score = _eval_1d_sad(cur_8, prev_8, s8)
        cand_8.append((score, s8))

    cand_8.sort(key=lambda x: (x[0], abs(x[1]), x[1]))
    top_s8 = list(dict.fromkeys([s8 for _, s8 in cand_8[:5]]))

    # Stage 2: D = 4
    cur_4 = downsample(cur_proj, 4)
    prev_4 = downsample(prev_proj, 4)
    max_s4 = max_shift // 4

    cand_4_set = set()
    for s8 in top_s8:
        for offset in range(-2, 3):
            s4 = s8 * 2 + offset
            if -max_s4 <= s4 <= max_s4:
                cand_4_set.add(s4)

    cand_4 = [(_eval_1d_sad(cur_4, prev_4, s4), s4) for s4 in cand_4_set]
    cand_4.sort(key=lambda x: (x[0], abs(x[1]), x[1]))
    top_s4 = list(dict.fromkeys([s4 for _, s4 in cand_4[:5]]))

    # Stage 3: D = 2
    cur_2 = downsample(cur_proj, 2)
    prev_2 = downsample(prev_proj, 2)
    max_s2 = max_shift // 2

    cand_2_set = set()
    for s4 in top_s4:
        for offset in range(-2, 3):
            s2 = s4 * 2 + offset
            if -max_s2 <= s2 <= max_s2:
                cand_2_set.add(s2)

    cand_2 = [(_eval_1d_sad(cur_2, prev_2, s2), s2) for s2 in cand_2_set]
    cand_2.sort(key=lambda x: (x[0], abs(x[1]), x[1]))
    top_s2 = list(dict.fromkeys([s2 for _, s2 in cand_2[:5]]))

    # Stage 4: D = 1
    cand_1_set = set()
    for s2 in top_s2:
        for offset in range(-2, 3):
            s1 = s2 * 2 + offset
            if -max_shift <= s1 <= max_shift:
                cand_1_set.add(s1)

    cand_1_set.add(0)

    cand_1 = [(_eval_1d_sad(cur_proj, prev_proj, s1), s1) for s1 in cand_1_set]
    cand_1.sort(key=lambda x: (x[0], abs(x[1]), x[1]))

    top_3 = []
    for score, s in cand_1:
        if s not in top_3:
            top_3.append(s)
            if len(top_3) >= 3:
                break

    return top_3, cand_1


def _compute_full_luma_sad(cur, prev, dx, dy):
    """Compute normalized full-resolution integer SAD between cur and prev for shift (dx, dy).

    cur[y, x] ~ prev[y - dy, x - dx]
    """
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

    diff = np.abs(cur_sub - prev_sub)
    _add_ops(overlap_area * 2)

    sad = int(np.sum(diff))
    _add_ops(overlap_area)

    sad_norm = (sad * total_area) // overlap_area
    return sad_norm


def estimate_global_motion(prev_luma, cur_luma, max_d):
    """Estimate global integer displacement (dx, dy) between prev_luma and cur_luma.

    Returns (dx, dy, score) tuple of ints or None if flat, periodic, or ambiguous.
    """
    prev_arr = np.asarray(prev_luma, dtype=np.uint8)
    cur_arr = np.asarray(cur_luma, dtype=np.uint8)

    H, W = cur_arr.shape

    # Flat frame check
    _add_ops(H * W * 2)
    if np.max(cur_arr) == np.min(cur_arr) and np.max(prev_arr) == np.min(prev_arr):
        return None

    # Compute gradient projections
    cur_row_zm, cur_col_zm, cur_max_grad = _compute_gradient_projections(cur_arr)
    prev_row_zm, prev_col_zm, prev_max_grad = _compute_gradient_projections(prev_arr)

    if cur_max_grad == 0 and prev_max_grad == 0:
        return None

    max_dy = min(max_d, H - 1)
    max_dx = min(max_d, W - 1)

    if max_dy <= 0 and max_dx <= 0:
        return None

    # 1D shift search over projection arrays using coarse-to-fine decimation
    top_dys, row_scores_all = _search_projection_shift(cur_row_zm, prev_row_zm, max_dy)
    top_dxs, col_scores_all = _search_projection_shift(cur_col_zm, prev_col_zm, max_dx)

    # Form top 3x3 candidate (dx, dy) pairs
    candidate_pairs = []
    for dy in top_dys:
        for dx in top_dxs:
            if abs(dx) <= max_d and abs(dy) <= max_d:
                candidate_pairs.append((dx, dy))

    if not candidate_pairs:
        return None

    # Full-resolution verification of top candidates
    scored_candidates = []
    for dx, dy in candidate_pairs:
        score = _compute_full_luma_sad(cur_arr, prev_arr, dx, dy)
        scored_candidates.append((score, dx, dy))

    scored_candidates.sort(key=lambda item: (item[0], abs(item[1]) + abs(item[2]), item[1], item[2]))

    best_score, best_dx, best_dy = scored_candidates[0]

    # Distant alias ambiguity check
    non_adj = [
        item for item in scored_candidates
        if max(abs(item[1] - best_dx), abs(item[2] - best_dy)) >= 4
    ]

    if non_adj:
        second_score = non_adj[0][0]
        if second_score == best_score:
            return None
        # Margin rejection for ambiguous / periodic frames
        if best_score > 0 and (second_score - best_score) * 100 // best_score < 5:
            return None

    return (int(best_dx), int(best_dy), int(best_score))


def consensus_tiles(prev_luma, cur_luma, s, dx, dy):
    """Return numpy bool array of shape (gh, gw) indicating tiles that match translated prev_luma.

    EDGE_RULE: whole destination tile must equal in-bounds translated previous pixels;
    tiles whose source leaves the frame are False. Fully vectorized with numpy.
    """
    prev_arr = np.asarray(prev_luma, dtype=np.uint8)
    cur_arr = np.asarray(cur_luma, dtype=np.uint8)

    H, W = cur_arr.shape
    gh = (H + s - 1) // s
    gw = (W + s - 1) // s

    if dx == 0 and dy == 0:
        return np.zeros((gh, gw), dtype=bool)

    cy0, cy1 = max(0, dy), min(H, H + dy)
    cx0, cx1 = max(0, dx), min(W, W + dx)

    if cy0 >= cy1 or cx0 >= cx1:
        return np.zeros((gh, gw), dtype=bool)

    # Pixel equality map over in-bounds translated region
    _add_ops((cy1 - cy0) * (cx1 - cx0) * 2)
    eq_map = np.zeros((H, W), dtype=bool)
    eq_map[cy0:cy1, cx0:cx1] = (cur_arr[cy0:cy1, cx0:cx1] == prev_arr[cy0 - dy:cy1 - dy, cx0 - dx:cx1 - dx])

    # Pad equality map to grid dimensions (gh * s, gw * s) with False for out-of-bounds frame pixels
    padded_eq = np.zeros((gh * s, gw * s), dtype=bool)
    padded_eq[:H, :W] = eq_map

    # Tile consensus: all pixels in a tile must be True
    _add_ops(gh * s * gw * s)
    mask = padded_eq.reshape(gh, s, gw, s).all(axis=(1, 3))
    return mask
