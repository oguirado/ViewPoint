"""eval/v2x/gm_bandsplit.py - Deterministic global motion bandsplit variant.

OPS definition:
Exposes a module-level integer `OPS` and a function `reset_ops()`.
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


_FNV_OFFSET = np.uint64(0xCBF29CE484222325)
_FNV_PRIME = np.uint64(0x100000001B3)


def _hash_rows_fnv1a(arr):
    """Compute 64-bit FNV-1a hash for each row of a 2D uint8 array."""
    H, W = arr.shape
    if H == 0 or W == 0:
        return np.empty(H, dtype=np.uint64)

    _add_ops(H * W * 2)
    hashes = np.full(H, _FNV_OFFSET, dtype=np.uint64)
    for c in range(W):
        hashes = (hashes ^ arr[:, c].astype(np.uint64)) * _FNV_PRIME
    return hashes


def _hash_cols_fnv1a(arr):
    """Compute 64-bit FNV-1a hash for each column of a 2D uint8 array."""
    M, W = arr.shape
    if M == 0 or W == 0:
        return np.empty(W, dtype=np.uint64)

    _add_ops(M * W * 2)
    hashes = np.full(W, _FNV_OFFSET, dtype=np.uint64)
    for r in range(M):
        hashes = (hashes ^ arr[r, :].astype(np.uint64)) * _FNV_PRIME
    return hashes


def estimate_global_motion(prev_luma, cur_luma, max_d, **kwargs):
    """Estimate global motion (dx, dy, score) between prev_luma and cur_luma using bandsplit.

    Returns (dx, dy, score) with score an integer ppm of matched middle rows, or None if middle band is empty or fewer than half rows match.
    """
    prev_arr = np.asarray(prev_luma, dtype=np.uint8)
    cur_arr = np.asarray(cur_luma, dtype=np.uint8)

    H, W = cur_arr.shape
    _add_ops(H * W * 2)

    if H == 0 or W == 0:
        return None

    prev_row_hashes = _hash_rows_fnv1a(prev_arr)
    cur_row_hashes = _hash_rows_fnv1a(cur_arr)

    # Static top band: longest prefix of equal hashes
    top_y = 0
    while top_y < H and cur_row_hashes[top_y] == prev_row_hashes[top_y]:
        top_y += 1

    # Static bottom band: longest suffix of equal hashes
    bottom = 0
    while bottom < (H - top_y) and cur_row_hashes[H - 1 - bottom] == prev_row_hashes[H - 1 - bottom]:
        bottom += 1

    bottom_y = H - bottom
    M = bottom_y - top_y

    if M <= 0:
        return None

    max_dy = min(max_d, H - 1)
    max_dx = min(max_d, W - 1)

    cur_m_indices = np.arange(top_y, bottom_y)
    cur_m_hashes = cur_row_hashes[cur_m_indices]

    best_dy = None
    best_count = -1

    for dy in range(-max_dy, max_dy + 1):
        prev_indices = cur_m_indices - dy
        valid = (prev_indices >= 0) & (prev_indices < H)
        if not np.any(valid):
            count = 0
        else:
            match = cur_m_hashes[valid] == prev_row_hashes[prev_indices[valid]]
            count = int(np.sum(match))

        _add_ops(M)

        if count > best_count:
            best_count = count
            best_dy = dy
        elif count == best_count and best_dy is not None:
            if abs(dy) < abs(best_dy):
                best_dy = dy
            elif abs(dy) == abs(best_dy) and dy < best_dy:
                best_dy = dy

    min_required = math.ceil(M / 2.0)
    if best_dy is None or best_count < min_required:
        return None

    cur_mid = cur_arr[top_y:bottom_y, :]
    prev_mid = prev_arr[top_y:bottom_y, :]

    cur_col_hashes = _hash_cols_fnv1a(cur_mid)
    prev_col_hashes = _hash_cols_fnv1a(prev_mid)

    cand_dx = None
    best_col_count = -1
    col_indices = np.arange(W)

    for dx in range(-max_dx, max_dx + 1):
        prev_c_indices = col_indices - dx
        valid_c = (prev_c_indices >= 0) & (prev_c_indices < W)
        if not np.any(valid_c):
            col_count = 0
        else:
            col_match = cur_col_hashes[valid_c] == prev_col_hashes[prev_c_indices[valid_c]]
            col_count = int(np.sum(col_match))

        _add_ops(W)

        if col_count > best_col_count:
            best_col_count = col_count
            cand_dx = dx
        elif col_count == best_col_count and cand_dx is not None:
            if abs(dx) < abs(cand_dx):
                cand_dx = dx
            elif abs(dx) == abs(cand_dx) and dx < cand_dx:
                cand_dx = dx

    dx_win = 0
    if cand_dx is not None and cand_dx != 0:
        prev_m_indices = cur_m_indices - best_dy
        valid_rows = (prev_m_indices >= 0) & (prev_m_indices < H)
        matched_mask = valid_rows.copy()
        matched_mask[valid_rows] = (cur_m_hashes[valid_rows] == prev_row_hashes[prev_m_indices[valid_rows]])

        matched_cur_y = cur_m_indices[matched_mask]
        matched_prev_y = prev_m_indices[matched_mask]

        cx0, cx1 = max(0, cand_dx), min(W, W + cand_dx)
        px0, px1 = cx0 - cand_dx, cx1 - cand_dx

        if cx1 > cx0:
            all_match = True
            for cy, py in zip(matched_cur_y, matched_prev_y):
                _add_ops(cx1 - cx0)
                if not np.array_equal(cur_arr[cy, cx0:cx1], prev_arr[py, px0:px1]):
                    all_match = False
                    break
            if all_match:
                dx_win = cand_dx

    score = (best_count * 1_000_000) // M   # integer ppm of matched middle rows (engine law: integers only)
    return (int(dx_win), int(best_dy), score)


def consensus_tiles(prev, cur, s, dx, dy, **kwargs):
    """Return numpy bool array of shape (gh, gw) indicating tiles of size s

    fully inside the middle band whose pixels equal prev shifted by dx dy.
    gh = ceil(H / s), gw = ceil(W / s).
    """
    prev_arr = np.asarray(prev, dtype=np.uint8)
    cur_arr = np.asarray(cur, dtype=np.uint8)

    H, W = cur_arr.shape
    gh = math.ceil(H / s)
    gw = math.ceil(W / s)

    _add_ops(H * W * 2)

    if H == 0 or W == 0:
        return np.zeros((gh, gw), dtype=bool)

    prev_row_hashes = _hash_rows_fnv1a(prev_arr)
    cur_row_hashes = _hash_rows_fnv1a(cur_arr)

    top_y = 0
    while top_y < H and cur_row_hashes[top_y] == prev_row_hashes[top_y]:
        top_y += 1

    bottom = 0
    while bottom < (H - top_y) and cur_row_hashes[H - 1 - bottom] == prev_row_hashes[H - 1 - bottom]:
        bottom += 1

    bottom_y = H - bottom
    M = bottom_y - top_y

    mask = np.zeros((gh, gw), dtype=bool)
    if M <= 0:
        return mask

    for ty in range(gh):
        cy0 = ty * s
        cy1 = min((ty + 1) * s, H)
        if cy0 < top_y or cy1 > bottom_y:
            continue

        for tx in range(gw):
            cx0 = tx * s
            cx1 = min((tx + 1) * s, W)

            py0, py1 = cy0 - dy, cy1 - dy
            px0, px1 = cx0 - dx, cx1 - dx

            if py0 < 0 or py1 > H or px0 < 0 or px1 > W:
                continue

            _add_ops((cy1 - cy0) * (cx1 - cx0) * 2)
            if np.array_equal(cur_arr[cy0:cy1, cx0:cx1], prev_arr[py0:py1, px0:px1]):
                mask[ty, tx] = True

    return mask
