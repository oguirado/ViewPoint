"""eval/v2x/gm_phasecorr.py - Global motion estimator using Phase Correlation (FFT).

Design Goal / Performance Target:
    TARGET: at most 16.7 ms per 1920x1088 frame.

OPS Unit Description:
    OPS counts elementary pixel or array element operations performed during
    candidate generation, downsampling, transformation, SAD cost evaluation,
    and consensus verification. Every array element compared, transformed, or
    reduced adds 1 to OPS per operation.
"""

import math
import numpy as np

# Module-level operation counter
OPS = 0


def reset_ops():
    """Reset the global OPS counter."""
    global OPS
    OPS = 0


def consensus_tiles(prev_luma, cur_luma, s, dx, dy):
    """Compute grid tile motion consensus boolean mask.

    Returns a boolean numpy array of shape (gh, gw) where:
        gh = ceil(H / s)
        gw = ceil(W / s)
    True only when the whole destination tile equals the in-bounds translated
    previous pixels (EDGE_RULE: tiles whose source leaves the frame are False).
    Vectorized with numpy.
    """
    global OPS
    prev = np.asarray(prev_luma, dtype=np.uint8)
    cur = np.asarray(cur_luma, dtype=np.uint8)
    H, W = cur.shape
    gh = math.ceil(H / s)
    gw = math.ceil(W / s)

    # Destination pixel (y, x) matches if y - dy in [0, H) and x - dx in [0, W)
    # and cur[y, x] == prev[y - dy, x - dx].
    cy0, cy1 = max(0, dy), min(H, H + dy)
    cx0, cx1 = max(0, dx), min(W, W + dx)
    py0, py1 = cy0 - dy, cy1 - dy
    px0, px1 = cx0 - dx, cx1 - dx

    match_mask = np.zeros((H, W), dtype=bool)
    if cy0 < cy1 and cx0 < cx1:
        match_mask[cy0:cy1, cx0:cx1] = (cur[cy0:cy1, cx0:cx1] == prev[py0:py1, px0:px1])
        OPS += (cy1 - cy0) * (cx1 - cx0)

    # Pad match_mask to (gh * s, gw * s) with True for pixels beyond (H, W)
    padded = np.ones((gh * s, gw * s), dtype=bool)
    padded[:H, :W] = match_mask

    # Vectorized check across (s, s) tile blocks
    res = padded.reshape(gh, s, gw, s).swapaxes(1, 2).reshape(gh, gw, s * s).all(axis=-1)
    OPS += gh * gw * s * s
    return res


def _compute_sad(cur, prev, dx, dy):
    """Compute exact full-frame integer SAD between cur and prev shifted by (dx, dy).

    cur[y, x] corresponds to prev[y - dy, x - dx].
    Increments global OPS.
    Returns (sad_norm, overlap_area).
    """
    global OPS

    H, W = cur.shape
    cy0, cy1 = max(0, dy), min(H, H + dy)
    cx0, cx1 = max(0, dx), min(W, W + dx)

    if cy0 >= cy1 or cx0 >= cx1:
        return (1 << 60, 0)

    py0, py1 = cy0 - dy, cy1 - dy
    px0, px1 = cx0 - dx, cx1 - dx

    cur_sub = cur[cy0:cy1, cx0:cx1].astype(np.int64)
    prev_sub = prev[py0:py1, px0:px1].astype(np.int64)

    overlap_area = (cy1 - cy0) * (cx1 - cx0)
    total_area = H * W

    diff = np.abs(cur_sub - prev_sub)
    sad = np.sum(diff)

    OPS += overlap_area * 3  # diff, abs, sum

    # Integer normalized SAD scaled to full total_area
    sad_norm = (int(sad) * total_area) // overlap_area
    return (sad_norm, overlap_area)


def estimate_global_motion(prev_luma, cur_luma, max_d, *, eps=1e-12):
    """Estimate global motion vector (dx, dy, score) via Phase Correlation.

    Returns (dx, dy, score) as tuple of Python ints or None if ambiguous/flat/invalid.
    """
    global OPS

    prev = np.asarray(prev_luma, dtype=np.uint8)
    cur = np.asarray(cur_luma, dtype=np.uint8)

    H, W = cur.shape
    if H < 8 or W < 8:
        return None

    # Check for flat frame
    if np.min(cur) == np.max(cur) or np.min(prev) == np.max(prev):
        OPS += H * W * 2
        return None

    # Zero motion fast check with integer arithmetic
    sad_0, _ = _compute_sad(cur, prev, 0, 0)
    if sad_0 == 0:
        return (0, 0, 0)

    # Downsample by 2 if frame width > 640
    scale_factor = 1
    if W > 640:
        scale_factor = 2
        prev_ds = prev[::2, ::2]
        cur_ds = cur[::2, ::2]
        OPS += prev_ds.size + cur_ds.size
    else:
        prev_ds = prev
        cur_ds = cur

    h_ds, w_ds = cur_ds.shape

    # Mean-subtracted float luma for Phase Correlation
    f_prev = prev_ds.astype(np.float64)
    f_cur = cur_ds.astype(np.float64)
    f_prev -= np.mean(f_prev)
    f_cur -= np.mean(f_cur)
    OPS += f_prev.size * 2 + f_cur.size * 2

    # Phase correlation via FFT
    F_prev = np.fft.fft2(f_prev)
    F_cur = np.fft.fft2(f_cur)

    # Cross-power spectrum: F_cur * conj(F_prev)
    R = F_cur * np.conj(F_prev)
    R_mag = np.abs(R)
    R_norm = R / (R_mag + eps)

    # Inverse FFT to get phase correlation matrix
    r = np.fft.ifft2(R_norm).real

    # Count ops for FFT float ops
    OPS += h_ds * w_ds * 10

    # Find top 8 peak coordinates in r
    r_flat = r.ravel()
    top_indices = np.argpartition(r_flat, -8)[-8:]
    top_indices = top_indices[np.argsort(-r_flat[top_indices])]

    raw_candidates = []
    for idx in top_indices:
        ry, rx = np.unravel_index(idx, (h_ds, w_ds))
        peak_val = float(r_flat[idx])

        dy_opts = [ry, ry - h_ds] if ry > 0 else [0]
        dx_opts = [rx, rx - w_ds] if rx > 0 else [0]

        for dy_ds in dy_opts:
            for dx_ds in dx_opts:
                raw_candidates.append((dx_ds, dy_ds, peak_val))

    # Scale back to full resolution candidates
    candidates_to_test = set()
    for dx_ds, dy_ds, _ in raw_candidates:
        dx_full = dx_ds * scale_factor
        dy_full = dy_ds * scale_factor

        if scale_factor > 1:
            for ddy in range(-1, 2):
                for ddx in range(-1, 2):
                    candidates_to_test.add((dx_full + ddx, dy_full + ddy))
        else:
            candidates_to_test.add((dx_full, dy_full))

    valid_candidates = []
    for dx, dy in candidates_to_test:
        if max(abs(dx), abs(dy)) <= max_d:
            valid_candidates.append((dx, dy))

    if not valid_candidates:
        return None

    # Evaluate full-resolution integer SAD for candidates
    candidate_costs = []
    for dx, dy in valid_candidates:
        sad_norm, overlap = _compute_sad(cur, prev, dx, dy)
        if overlap > 0 and sad_norm < (1 << 50):
            candidate_costs.append((sad_norm, dx, dy))

    if not candidate_costs:
        return None

    candidate_costs.sort(key=lambda x: (x[0], abs(x[1]) + abs(x[2]), x[1], x[2]))

    best_sad, best_dx, best_dy = candidate_costs[0]

    # Refine best candidate in 3x3 local neighborhood
    refined_best = (best_sad, best_dx, best_dy)
    for ddy in range(-1, 2):
        for ddx in range(-1, 2):
            if ddx == 0 and ddy == 0:
                continue
            test_dx = best_dx + ddx
            test_dy = best_dy + ddy
            if max(abs(test_dx), abs(test_dy)) <= max_d:
                sad_norm, overlap = _compute_sad(cur, prev, test_dx, test_dy)
                if overlap > 0 and sad_norm < refined_best[0]:
                    refined_best = (sad_norm, test_dx, test_dy)

    best_sad, best_dx, best_dy = refined_best

    # Check zero-motion cost comparison
    if (best_dx, best_dy) != (0, 0) and sad_0 < best_sad:
        best_sad, best_dx, best_dy = sad_0, 0, 0

    # Filter spatially distinct candidates for alias rejection / ambiguity check
    # Vectors within 2px distance belong to the same peak cluster.
    distinct_candidates = []
    for sad, dx, dy in candidate_costs:
        if not any(max(abs(dx - adx), abs(dy - ady)) <= 2 for _, adx, ady in distinct_candidates):
            distinct_candidates.append((sad, dx, dy))

    distinct_candidates.sort(key=lambda x: x[0])

    max_frame_sad = H * W * 255
    if best_sad > (max_frame_sad * 4) // 10:  # > 40% max SAD
        return None

    if best_dx == 0 and best_dy == 0:
        return (0, 0, int(best_sad))

    if len(distinct_candidates) >= 2:
        second_sad = distinct_candidates[1][0]
        # Ambiguous / periodic if second best distinct peak cost is within 5% of best cost
        if best_sad * 105 >= second_sad * 100:
            return None

    return (int(best_dx), int(best_dy), int(best_sad))
