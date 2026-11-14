"""eval/v2x/gm_rowhash.py - Row-hash matching global motion estimator.

Design goal: TARGET execution time of at most 16.7 ms per 1920x1088 frame.

OPS definition:
Exposes a module-level integer `OPS` and a function `reset_ops()`. Every call
adds the number of elementary pixel or element operations it performs, counted as
the total element count (size) of every array compared, transformed, hashed, or
reduced during execution.
"""

import math
import numpy as np

OPS = 0


def reset_ops():
    """Reset the global OPS counter to 0."""
    global OPS
    OPS = 0


def _add_ops(count):
    global OPS
    OPS += int(count)


# Fixed 64-bit prime for 1D polynomial hash
_HASH_PRIME = np.uint64(0x9E3779B97F4A7C15)


def _hash_rows(arr):
    """Compute 64-bit polynomial hash for each row of a 2D uint8 array.

    arr: 2D uint8 array (H, W).
    Returns uint64 array of shape (H,).
    """
    H, W = arr.shape
    if H == 0 or W == 0:
        return np.empty(0, dtype=np.uint64)

    _add_ops(H * W * 2)

    powers = np.power(_HASH_PRIME, np.arange(1, W + 1, dtype=np.uint64), dtype=np.uint64)
    row_hashes = np.sum(arr.astype(np.uint64) * powers, axis=1, dtype=np.uint64)
    return row_hashes


def _hash_cols(arr):
    """Compute 64-bit polynomial hash for each column of a 2D uint8 array.

    arr: 2D uint8 array (H, W).
    Returns uint64 array of shape (W,).
    """
    H, W = arr.shape
    if H == 0 or W == 0:
        return np.empty(0, dtype=np.uint64)

    _add_ops(H * W * 2)
    powers = np.power(_HASH_PRIME, np.arange(1, H + 1, dtype=np.uint64), dtype=np.uint64)[:, None]
    col_hashes = np.sum(arr.astype(np.uint64) * powers, axis=0, dtype=np.uint64)
    return col_hashes


def _vote_1d(hashes_cur, hashes_prev, max_shift):
    """Vote 1D displacement between hashes_cur and hashes_prev.

    cur[i] corresponds to prev[i - shift].
    Only considers unique hashes in prev and unique hashes in cur.
    Returns (winner_shift, best_votes) or (None, 0).
    Condition: min 8 votes, margin of twice runner-up.
    """
    if len(hashes_cur) == 0 or len(hashes_prev) == 0:
        return None, 0

    _add_ops(len(hashes_cur) + len(hashes_prev))

    cur_vals, cur_counts = np.unique(hashes_cur, return_counts=True)
    prev_vals, prev_counts = np.unique(hashes_prev, return_counts=True)

    unique_cur_hashes = set(cur_vals[cur_counts == 1])
    prev_counts_dict = dict(zip(prev_vals, prev_counts))
    prev_idx_map = {}
    for idx, val in enumerate(hashes_prev):
        if prev_counts_dict[val] == 1:
            prev_idx_map[val] = idx

    votes = {}
    for cur_idx, val in enumerate(hashes_cur):
        if val in unique_cur_hashes and val in prev_idx_map:
            prev_idx = prev_idx_map[val]
            shift = cur_idx - prev_idx
            if abs(shift) <= max_shift:
                votes[shift] = votes.get(shift, 0) + 1

    if not votes:
        return None, 0

    sorted_votes = sorted(votes.items(), key=lambda x: x[1], reverse=True)
    top_shift, top_count = sorted_votes[0]

    runner_up_count = sorted_votes[1][1] if len(sorted_votes) > 1 else 0

    if top_count >= 8 and top_count >= 2 * runner_up_count:
        return top_shift, top_count

    return None, 0


def _get_top_shifts_from_votes(hashes_cur, hashes_prev, max_shift, top_k=8):
    """Return top_k candidate shifts ranked by vote counts."""
    if len(hashes_cur) == 0 or len(hashes_prev) == 0:
        return []

    _add_ops(len(hashes_cur) + len(hashes_prev))

    cur_vals, cur_counts = np.unique(hashes_cur, return_counts=True)
    prev_vals, prev_counts = np.unique(hashes_prev, return_counts=True)

    unique_cur_hashes = set(cur_vals[cur_counts == 1])
    prev_counts_dict = dict(zip(prev_vals, prev_counts))
    prev_idx_map = {}
    for idx, val in enumerate(hashes_prev):
        if prev_counts_dict[val] == 1:
            prev_idx_map[val] = idx

    votes = {}
    for cur_idx, val in enumerate(hashes_cur):
        if val in unique_cur_hashes and val in prev_idx_map:
            prev_idx = prev_idx_map[val]
            shift = cur_idx - prev_idx
            if abs(shift) <= max_shift:
                votes[shift] = votes.get(shift, 0) + 1

    if not votes:
        return []

    sorted_votes = sorted(votes.items(), key=lambda x: x[1], reverse=True)
    return [s for s, c in sorted_votes[:top_k] if c > 0]


def _get_top_dx_from_col_projections(cur_arr, prev_arr, max_dx, top_k=8):
    """Compute 1D column sums and rank dx by normalized SAD."""
    H, W = cur_arr.shape
    _add_ops(H * W * 2)

    col_cur = np.sum(cur_arr.astype(np.int64), axis=0)
    col_prev = np.sum(prev_arr.astype(np.int64), axis=0)

    candidates = []
    for dx in range(-max_dx, max_dx + 1):
        cx0, cx1 = max(0, dx), min(W, W + dx)
        px0, px1 = cx0 - dx, cx1 - dx
        overlap = cx1 - cx0
        if overlap <= 0:
            continue
        c_sub = col_cur[cx0:cx1]
        p_sub = col_prev[px0:px1]
        sad = np.sum(np.abs(c_sub - p_sub))
        norm_sad = (sad * W) // overlap
        candidates.append((norm_sad, dx))

    _add_ops(len(candidates) * 4)
    candidates.sort(key=lambda x: (x[0], abs(x[1]), x[1]))
    return [dx for _, dx in candidates[:top_k]]


def _verify_vector(prev_arr, cur_arr, dx, dy):
    """Verify displacement (dx, dy) by counting exact matching pixels in overlap.

    cur[y, x] == prev[y - dy, x - dx].
    Returns (sad_score, overlap_area) or None if no valid overlap.
    """
    H, W = cur_arr.shape
    cy0, cy1 = max(0, dy), min(H, H + dy)
    cx0, cx1 = max(0, dx), min(W, W + dx)

    if cy0 >= cy1 or cx0 >= cx1:
        return None

    py0, py1 = cy0 - dy, cy1 - dy
    px0, px1 = cx0 - dx, cx1 - dx

    cur_sub = cur_arr[cy0:cy1, cx0:cx1]
    prev_sub = prev_arr[py0:py1, px0:px1]

    overlap_area = (cy1 - cy0) * (cx1 - cx0)
    _add_ops(overlap_area * 2)

    diff = np.abs(cur_sub.astype(np.int64) - prev_sub.astype(np.int64))
    sad = int(np.sum(diff))

    return sad, overlap_area


def estimate_global_motion(prev_luma, cur_luma, max_d, **kwargs):
    """Estimate global motion (dx, dy, score) between prev_luma and cur_luma.

    Returns tuple (dx, dy, score) of Python ints or None if flat, periodic,
    or ambiguous.
    """
    prev_arr = np.asarray(prev_luma, dtype=np.uint8)
    cur_arr = np.asarray(cur_luma, dtype=np.uint8)

    H, W = cur_arr.shape
    _add_ops(H * W * 2)

    # Flat frame check
    if np.max(cur_arr) == np.min(cur_arr) and np.max(prev_arr) == np.min(prev_arr):
        return None

    max_dy = min(max_d, H - 1)
    max_dx = min(max_d, W - 1)

    # 1. Row hashes of full frame
    hashes_cur_rows = _hash_rows(cur_arr)
    hashes_prev_rows = _hash_rows(prev_arr)
    dy_win, dy_votes = _vote_1d(hashes_cur_rows, hashes_prev_rows, max_dy)

    # 2. Column hashes of full frame
    hashes_cur_cols = _hash_cols(cur_arr)
    hashes_prev_cols = _hash_cols(prev_arr)
    dx_win, dx_votes = _vote_1d(hashes_cur_cols, hashes_prev_cols, max_dx)

    candidates = set()

    if dy_win is not None and dx_win is not None:
        candidates.add((dx_win, dy_win))
    if dy_win is not None:
        candidates.add((0, dy_win))
    if dx_win is not None:
        candidates.add((dx_win, 0))

    # Collect candidate dx shifts
    top_dx_votes = _get_top_shifts_from_votes(hashes_cur_cols, hashes_prev_cols, max_dx, top_k=5)
    top_dx_proj = _get_top_dx_from_col_projections(cur_arr, prev_arr, max_dx, top_k=5)

    dx_candidates = list(dict.fromkeys(top_dx_votes + top_dx_proj + [0]))

    # Collect candidate dy shifts from full row votes
    top_dy_votes = _get_top_shifts_from_votes(hashes_cur_rows, hashes_prev_rows, max_dy, top_k=5)
    for dy in top_dy_votes:
        candidates.add((0, dy))

    # For combined move, compute row hashes restricted to column overlap of top dx candidates
    for cand_dx in dx_candidates:
        if abs(cand_dx) > max_d:
            continue
        cx0, cx1 = max(0, cand_dx), min(W, W + cand_dx)
        px0, px1 = cx0 - cand_dx, cx1 - cand_dx
        if cx1 <= cx0:
            continue

        cur_sub_cols = cur_arr[:, cx0:cx1]
        prev_sub_cols = prev_arr[:, px0:px1]

        h_cur_sub = _hash_rows(cur_sub_cols)
        h_prev_sub = _hash_rows(prev_sub_cols)

        cand_dy, votes_dy_sub = _vote_1d(h_cur_sub, h_prev_sub, max_dy)
        if cand_dy is not None:
            candidates.add((cand_dx, cand_dy))

        top_dys_sub = _get_top_shifts_from_votes(h_cur_sub, h_prev_sub, max_dy, top_k=3)
        for dy_sub in top_dys_sub:
            candidates.add((cand_dx, dy_sub))

    # Evaluate candidate vectors except (0,0) unless (0,0) is uniquely voted
    verified = []
    for dx, dy in candidates:
        if abs(dx) > max_d or abs(dy) > max_d:
            continue
        res = _verify_vector(prev_arr, cur_arr, dx, dy)
        if res is not None:
            sad, overlap = res
            verified.append((sad, overlap, dx, dy))

    if not verified:
        return None

    # Filter out candidate (0, 0) if non-zero candidate matches better or equal normalized SAD
    nonzero = [v for v in verified if (v[2], v[3]) != (0, 0)]
    if nonzero:
        # Sort by (sad / overlap)
        nonzero.sort(key=lambda x: (x[0] / float(x[1]), x[0], -x[1]))
        best_nz = nonzero[0]

        zero = [v for v in verified if (v[2], v[3]) == (0, 0)]
        if zero:
            zero_sad, zero_overlap, _, _ = zero[0]
            # If nonzero vector has lower or comparable normalized SAD density, prefer nonzero
            if (best_nz[0] / float(best_nz[1])) <= (zero_sad / float(zero_overlap)):
                verified = nonzero

    # Sort candidates by (sad, -overlap, abs(dx)+abs(dy))
    verified.sort(key=lambda x: (x[0], -x[1], abs(x[2]) + abs(x[3])))

    best_sad, best_overlap, best_dx, best_dy = verified[0]

    # Periodic/alias check
    exact_matches = [item for item in verified if item[0] == 0]
    if len(exact_matches) > 1:
        for _, _, edx, edy in exact_matches:
            if (edx, edy) != (best_dx, best_dy):
                if abs(edx - best_dx) >= 4 or abs(edy - best_dy) >= 4:
                    return None

    if best_sad > best_overlap * 255 * 0.05:
        return None

    return (int(best_dx), int(best_dy), int(best_sad))


def consensus_tiles(prev, cur, s, dx, dy, **kwargs):
    """Return numpy bool array of shape (gh, gw) indicating tiles where

    whole destination tile equals in-bounds translated previous pixels.
    gh = ceil(H / s), gw = ceil(W / s).
    """
    prev_arr = np.asarray(prev, dtype=np.uint8)
    cur_arr = np.asarray(cur, dtype=np.uint8)

    H, W = cur_arr.shape
    gh = math.ceil(H / s)
    gw = math.ceil(W / s)

    _add_ops(H * W * 2)

    y_coords, x_coords = np.ogrid[:H, :W]
    tile_y = y_coords // s
    tile_x = x_coords // s

    py = y_coords - dy
    px = x_coords - dx

    in_bounds = (py >= 0) & (py < H) & (px >= 0) & (px < W)

    py_clamped = np.clip(py, 0, H - 1)
    px_clamped = np.clip(px, 0, W - 1)

    prev_shifted = prev_arr[py_clamped, px_clamped]
    pixel_match = in_bounds & (cur_arr == prev_shifted)

    _add_ops(H * W * 3)

    has_mismatch = np.zeros((gh, gw), dtype=bool)
    np.logical_or.at(has_mismatch, (tile_y, tile_x), ~pixel_match)

    tile_mask = ~has_mismatch
    return tile_mask
