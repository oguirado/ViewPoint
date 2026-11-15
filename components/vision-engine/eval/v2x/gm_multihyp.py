"""eval/v2x/gm_multihyp.py - Global motion estimation via multi-hypothesis SAD with alias rejection.

Design Goal:
TARGET: at most 16.7 ms per 1920x1088 frame.

OPS Unit Description:
OPS unit description: Each operation adds the number of elementary pixel or element operations
performed (e.g., array elements summed, subtracted, or compared) across frame processing steps.
"""

import numpy as np
from visioncore.layout import grid_params

OPS = 0


def reset_ops():
    """Reset the module-level OPS counter to zero."""
    global OPS
    OPS = 0


def estimate_global_motion(prev_luma, cur_luma, max_d, k_1d=4, k_4x=8):
    """Estimate global integer displacement (dx, dy) between prev_luma and cur_luma.

    Returns (dx, dy, score) as Python ints, or None if flat, periodic, or ambiguous.
    cur[y, x] corresponds to prev[y - dy, x - dx].
    Uses integer arithmetic for all candidate selection and decision logic.
    """
    global OPS

    prev_arr = np.asarray(prev_luma, dtype=np.uint8)
    cur_arr = np.asarray(cur_luma, dtype=np.uint8)

    H, W = cur_arr.shape
    total_pixels = H * W
    OPS += 2 * total_pixels

    # Flat frame check
    cur_min, cur_max = np.min(cur_arr), np.max(cur_arr)
    prev_min, prev_max = np.min(prev_arr), np.max(prev_arr)
    if cur_min == cur_max and prev_min == prev_max:
        return None

    # Require minimum overlap area for meaningful motion estimation (e.g., >= 5% of total frame area)
    min_overlap = total_pixels // 20

    # Source 1: 1D Row and Column projections
    cur_rows = cur_arr.astype(np.int64).sum(axis=1)
    prev_rows = prev_arr.astype(np.int64).sum(axis=1)
    cur_cols = cur_arr.astype(np.int64).sum(axis=0)
    prev_cols = prev_arr.astype(np.int64).sum(axis=0)
    OPS += 2 * total_pixels

    max_dy = min(max_d, H - 1)
    max_dx = min(max_d, W - 1)

    # 1D dy SAD search
    dy_candidates_all = []
    for dy in range(-max_dy, max_dy + 1):
        if dy >= 0:
            c_sub = cur_rows[dy:]
            p_sub = prev_rows[:H - dy]
        else:
            c_sub = cur_rows[:H + dy]
            p_sub = prev_rows[-dy:]

        overlap = len(c_sub)
        if overlap <= 0:
            continue

        sad = np.sum(np.abs(c_sub - p_sub))
        sad_norm = (int(sad) * H) // overlap
        dy_candidates_all.append((sad_norm, abs(dy), dy))
        OPS += overlap

    dy_candidates_all.sort(key=lambda item: (item[0], item[1], item[2]))

    top_dys = []
    seen_dy = set()
    for _, _, dy in dy_candidates_all:
        if dy not in seen_dy:
            seen_dy.add(dy)
            top_dys.append(dy)
            if len(top_dys) >= k_1d:
                break

    # 1D dx SAD search
    dx_candidates_all = []
    for dx in range(-max_dx, max_dx + 1):
        if dx >= 0:
            c_sub = cur_cols[dx:]
            p_sub = prev_cols[:W - dx]
        else:
            c_sub = cur_cols[:W + dx]
            p_sub = prev_cols[-dx:]

        overlap = len(c_sub)
        if overlap <= 0:
            continue

        sad = np.sum(np.abs(c_sub - p_sub))
        sad_norm = (int(sad) * W) // overlap
        dx_candidates_all.append((sad_norm, abs(dx), dx))
        OPS += overlap

    dx_candidates_all.sort(key=lambda item: (item[0], item[1], item[2]))

    top_dxs = []
    seen_dx = set()
    for _, _, dx in dx_candidates_all:
        if dx not in seen_dx:
            seen_dx.add(dx)
            top_dxs.append(dx)
            if len(top_dxs) >= k_1d:
                break

    # Source 1 combined pairs (always include 0 in dy and dx to ensure pure 1D motion is captured)
    candidate_vectors = set()
    dy_pool = set(top_dys)
    dy_pool.add(0)
    dx_pool = set(top_dxs)
    dx_pool.add(0)

    for dy in dy_pool:
        for dx in dx_pool:
            if abs(dx) <= max_d and abs(dy) <= max_d:
                candidate_vectors.add((dx, dy))

    # Source 2: 4x downsampled exhaustive SAD with adaptive sampling
    cur_4x = cur_arr[::4, ::4].astype(np.int64)
    prev_4x = prev_arr[::4, ::4].astype(np.int64)
    H4, W4 = cur_4x.shape

    max_dy4 = min(max_d // 4, H4 - 1)
    max_dx4 = min(max_d // 4, W4 - 1)

    step_y = max(1, H4 // 32)
    step_x = max(1, W4 // 32)

    sad_4x_candidates = []
    for dy4 in range(-max_dy4, max_dy4 + 1, step_y):
        cy0, cy1 = max(0, dy4), min(H4, H4 + dy4)
        if cy0 >= cy1:
            continue
        py0, py1 = cy0 - dy4, cy1 - dy4
        c_slice = cur_4x[cy0:cy1, :]
        p_slice = prev_4x[py0:py1, :]
        h_sub = cy1 - cy0

        for dx4 in range(-max_dx4, max_dx4 + 1, step_x):
            cx0, cx1 = max(0, dx4), min(W4, W4 + dx4)
            if cx0 >= cx1:
                continue
            px0, px1 = cx0 - dx4, cx1 - dx4
            w_sub = cx1 - cx0

            # Skip tiny overlaps in 4x downsampled search
            if (h_sub * 16) * (w_sub * 16) < min_overlap:
                continue

            sad = np.sum(np.abs(c_slice[:, cx0:cx1] - p_slice[:, px0:px1]))
            sad_norm = (int(sad) * (H4 * W4)) // (h_sub * w_sub)
            sad_4x_candidates.append((sad_norm, abs(dx4) + abs(dy4), dx4, dy4))
            OPS += h_sub * w_sub

    sad_4x_candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))

    top_4x = []
    seen_4x = set()
    for _, _, dx4, dy4 in sad_4x_candidates:
        vec = (dx4 * 4, dy4 * 4)
        if vec not in seen_4x:
            seen_4x.add(vec)
            top_4x.append(vec)
            if len(top_4x) >= k_4x:
                break

    for dx, dy in top_4x:
        if abs(dx) <= max_d and abs(dy) <= max_d:
            candidate_vectors.add((dx, dy))

    # Source 3: Zero vector
    if abs(0) <= max_d:
        candidate_vectors.add((0, 0))

    # Full resolution SAD evaluation for all gathered candidate hypotheses
    full_eval_results = []
    cur_full = cur_arr.astype(np.int64)
    prev_full = prev_arr.astype(np.int64)

    for dx, dy in candidate_vectors:
        cy0, cy1 = max(0, dy), min(H, H + dy)
        cx0, cx1 = max(0, dx), min(W, W + dx)
        if cy0 >= cy1 or cx0 >= cx1:
            continue

        overlap_area = (cy1 - cy0) * (cx1 - cx0)
        if overlap_area < min_overlap:
            continue

        py0, py1 = cy0 - dy, cy1 - dy
        px0, px1 = cx0 - dx, cx1 - dx

        c_sub = cur_full[cy0:cy1, cx0:cx1]
        p_sub = prev_full[py0:py1, px0:px1]

        sad = np.sum(np.abs(c_sub - p_sub))
        sad_norm = (int(sad) * total_pixels) // overlap_area
        full_eval_results.append((sad_norm, abs(dx) + abs(dy), abs(dy), dx, dy))
        OPS += overlap_area

    if not full_eval_results:
        return None

    full_eval_results.sort(key=lambda item: (item[0], item[1], item[2], item[3], item[4]))
    best_score, _, _, best_dx, best_dy = full_eval_results[0]

    # Dominant period detection via 1D autocorrelation / 1D SAD diff
    def find_dominant_period(proj_1d):
        N = len(proj_1d)
        max_p = min(N // 2, 64)
        if max_p < 2:
            return 0
        p_scores = []
        for p in range(2, max_p + 1):
            c_sub = proj_1d[p:]
            p_sub = proj_1d[:N - p]
            overlap = len(c_sub)
            if overlap <= 0:
                continue
            sad = np.sum(np.abs(c_sub - p_sub))
            sad_norm = (int(sad) * N) // overlap
            p_scores.append((sad_norm, p))
            global OPS
            OPS += overlap
        if not p_scores:
            return 0
        p_scores.sort(key=lambda item: (item[0], item[1]))
        best_p_sad, best_p = p_scores[0]
        # Check if period SAD is significantly low (periodic signal)
        mean_p_sad = sum(s for s, _ in p_scores) // len(p_scores)
        if best_p_sad * 2 <= mean_p_sad:
            return best_p
        return 0

    Py = find_dominant_period(cur_rows)
    Px = find_dominant_period(cur_cols)

    # Margin check helper (integer arithmetic)
    relative_margin = (best_score * 5) // 100 + 50

    # Periodicity check: check vector plus or minus one period
    alias_test_vectors = []
    if Py > 0:
        alias_test_vectors.extend([(best_dx, best_dy + Py), (best_dx, best_dy - Py)])
    if Px > 0:
        alias_test_vectors.extend([(best_dx + Px, best_dy), (best_dx - Px, best_dy)])

    for a_dx, a_dy in alias_test_vectors:
        if abs(a_dx) <= max_d and abs(a_dy) <= max_d and (a_dx, a_dy) != (best_dx, best_dy):
            cy0, cy1 = max(0, a_dy), min(H, H + a_dy)
            cx0, cx1 = max(0, a_dx), min(W, W + a_dx)
            overlap_area = (cy1 - cy0) * (cx1 - cx0)
            if cy0 < cy1 and cx0 < cx1 and overlap_area >= min_overlap:
                py0, py1 = cy0 - a_dy, cy1 - a_dy
                px0, px1 = cx0 - a_dx, cx1 - a_dx
                sad = np.sum(np.abs(cur_full[cy0:cy1, cx0:cx1] - prev_full[py0:py1, px0:px1]))
                a_score = (int(sad) * total_pixels) // overlap_area
                OPS += overlap_area
                if a_score - best_score <= relative_margin:
                    return None

    # Ambiguity check across non-adjacent full evaluation candidates
    for cand_score, _, _, cand_dx, cand_dy in full_eval_results[1:]:
        if max(abs(cand_dx - best_dx), abs(cand_dy - best_dy)) >= 4:
            if cand_score - best_score <= relative_margin:
                return None

    return (int(best_dx), int(best_dy), int(best_score))


def consensus_tiles(prev_luma, cur_luma, s, dx, dy):
    """Return a boolean (gh, gw) numpy array of tiles whose content equals

    prev content shifted by (dx, dy).
    Whole destination tile must equal in-bounds translated previous pixels.
    Tiles whose source leaves the frame are False.
    Vectorized with numpy.
    """
    prev_arr = np.asarray(prev_luma, dtype=np.uint8)
    cur_arr = np.asarray(cur_luma, dtype=np.uint8)

    H, W = cur_arr.shape
    grid_s, gw, gh = grid_params(W, H)

    # Tile index grids
    ty = np.arange(gh)[:, None]
    tx = np.arange(gw)[None, :]

    cy0 = ty * s
    cy1 = np.minimum((ty + 1) * s, H)
    cx0 = tx * s
    cx1 = np.minimum((tx + 1) * s, W)

    py0 = cy0 - dy
    py1 = cy1 - dy
    px0 = cx0 - dx
    px1 = cx1 - dx

    # Source tile must be fully in-bounds in prev
    in_bounds = (py0 >= 0) & (py1 <= H) & (px0 >= 0) & (px1 <= W)

    # Element-wise match in overlapping frame region
    cy0_valid, cy1_valid = max(0, dy), min(H, H + dy)
    cx0_valid, cx1_valid = max(0, dx), min(W, W + dx)
    py0_valid, py1_valid = cy0_valid - dy, cy1_valid - dy
    px0_valid, px1_valid = cx0_valid - dx, cx1_valid - dx

    match_map = np.zeros((H, W), dtype=bool)
    if cy0_valid < cy1_valid and cx0_valid < cx1_valid:
        match_map[cy0_valid:cy1_valid, cx0_valid:cx1_valid] = (
            cur_arr[cy0_valid:cy1_valid, cx0_valid:cx1_valid]
            == prev_arr[py0_valid:py1_valid, px0_valid:px1_valid]
        )

    # Pad match_map to exact tile boundary multiple (gh * s, gw * s) with True
    pad_h = gh * s
    pad_w = gw * s
    match_padded = np.ones((pad_h, pad_w), dtype=bool)
    match_padded[:H, :W] = match_map

    # Check that all pixels in each (s, s) block match
    tile_matches = match_padded.reshape(gh, s, gw, s).all(axis=(1, 3))

    return in_bounds & tile_matches
