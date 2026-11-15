"""eval/v2x/gm_ransac_blocks.py - Block-matching consensus global motion with deterministic RANSAC.

Design Goal: At most 16.7 ms per 1920x1088 frame as TARGET.

OPS Unit:
The OPS counter tracks the total number of elementary pixel or element operations performed,
counted as the size (number of elements) of every array compared, transformed, or reduced.
"""

import numpy as np
from visioncore.layout import grid_params

# Module-level integer OPS counter
OPS = 0


def reset_ops():
    """Reset the global OPS counter to 0."""
    global OPS
    OPS = 0


def _add_ops(n):
    """Increment OPS by n elementary element operations."""
    global OPS
    OPS += int(n)


def _compute_full_sad(cur, prev, dx, dy):
    """Compute exact full-frame normalized SAD between cur and prev shifted by (dx, dy).

    cur[y, x] corresponds to prev[y - dy, x - dx].
    Increments OPS by overlap_area.
    Returns normalized SAD scaled to total frame area (H * W), or 1 << 60 if out of bounds.
    """
    H, W = cur.shape
    cy0, cy1 = max(0, dy), min(H, H + dy)
    cx0, cx1 = max(0, dx), min(W, W + dx)

    if cy0 >= cy1 or cx0 >= cx1:
        return 1 << 60

    py0, py1 = cy0 - dy, cy1 - dy
    px0, px1 = cx0 - dx, cx1 - dx

    overlap_area = (cy1 - cy0) * (cx1 - cx0)
    if overlap_area <= 0:
        return 1 << 60

    cur_sub = cur[cy0:cy1, cx0:cx1].astype(np.int64)
    prev_sub = prev[py0:py1, px0:px1].astype(np.int64)

    sad = np.sum(np.abs(cur_sub - prev_sub))
    _add_ops(overlap_area)

    sad_norm = (int(sad) * (H * W)) // overlap_area
    return int(sad_norm)


def estimate_global_motion(prev_luma, cur_luma, max_d):
    """Estimate global integer displacement (dx, dy) between prev_luma and cur_luma.

    Returns (dx, dy, score) as Python ints, or None if flat, periodic, or ambiguous.
    Uses deterministic 64x64 block matching with RANSAC consensus.
    """
    prev_arr = np.asarray(prev_luma, dtype=np.uint8)
    cur_arr = np.asarray(cur_luma, dtype=np.uint8)
    _add_ops(prev_arr.size + cur_arr.size)

    H, W = cur_arr.shape

    # Flat frame check
    if np.max(cur_arr) == np.min(cur_arr) and np.max(prev_arr) == np.min(prev_arr):
        return None

    block_size = 64
    if H < block_size or W < block_size:
        block_size = max(8, min(32, H, W))

    n_y = H // block_size
    n_x = W // block_size

    if n_y == 0 or n_x == 0:
        return None

    # Step 1: Divide frame into block_size x block_size non-overlapping blocks
    # Compute integer variance for each block
    block_info = []
    for iy in range(n_y):
        by = iy * block_size
        for ix in range(n_x):
            bx = ix * block_size
            blk = cur_arr[by:by + block_size, bx:bx + block_size].astype(np.int64)
            size = blk.size
            blk_sum = np.sum(blk)
            mean = blk_sum // size
            var = np.sum((blk - mean) ** 2) // size
            _add_ops(size * 3)
            if var > 0:
                block_info.append((int(var), by, bx))

    if not block_info:
        return None

    # Sort blocks by variance descending, tie-breaking by (by, bx) ascending
    block_info.sort(key=lambda b: (-b[0], b[1], b[2]))

    # Keep up to 48 most textured blocks
    selected_blocks = block_info[:48]
    num_selected = len(selected_blocks)

    max_dy = min(max_d, H - 1)
    max_dx = min(max_d, W - 1)

    prev_int = prev_arr.astype(np.int64)
    cur_int = cur_arr.astype(np.int64)

    # Precompute 1D row sums and column sums
    prev_all_rows = np.sum(prev_int, axis=1)  # shape (H,)
    cur_all_rows = np.sum(cur_int, axis=1)    # shape (H,)
    prev_all_cols = np.sum(prev_int, axis=0)  # shape (W,)
    cur_all_cols = np.sum(cur_int, axis=0)    # shape (W,)
    _add_ops(prev_int.size * 2 + cur_int.size * 2)

    # Precompute sliding window 2D views for prev row/col sums
    prev_row_windows = np.lib.stride_tricks.sliding_window_view(prev_all_rows, block_size)  # shape (H - block_size + 1, block_size)
    prev_col_windows = np.lib.stride_tricks.sliding_window_view(prev_all_cols, block_size)  # shape (W - block_size + 1, block_size)

    candidate_vectors = set()

    for var, by, bx in selected_blocks:
        # 1-D row projection SAD over horizontal strip
        cur_r_strip = cur_all_rows[by:by + block_size]

        # Valid py0 range: 0 <= py0 <= H - block_size
        py0_indices = np.arange(H - block_size + 1)
        dys = by - py0_indices
        valid_mask_y = np.abs(dys) <= max_dy

        if np.any(valid_mask_y):
            valid_windows_y = prev_row_windows[valid_mask_y]
            valid_dys = dys[valid_mask_y]
            sads_y = np.sum(np.abs(valid_windows_y - cur_r_strip), axis=1)
            _add_ops(valid_windows_y.size)

            min_sad_y = np.min(sads_y)
            min_matches = valid_dys[sads_y == min_sad_y]
            best_dy = int(min(min_matches, key=lambda d: (abs(d), d)))
        else:
            best_dy = 0

        # 1-D col projection SAD over vertical strip
        cur_c_strip = cur_all_cols[bx:bx + block_size]

        px0_indices = np.arange(W - block_size + 1)
        dxs = bx - px0_indices
        valid_mask_x = np.abs(dxs) <= max_dx

        if np.any(valid_mask_x):
            valid_windows_x = prev_col_windows[valid_mask_x]
            valid_dxs = dxs[valid_mask_x]
            sads_x = np.sum(np.abs(valid_windows_x - cur_c_strip), axis=1)
            _add_ops(valid_windows_x.size)

            min_sad_x = np.min(sads_x)
            min_matches = valid_dxs[sads_x == min_sad_x]
            best_dx = int(min(min_matches, key=lambda d: (abs(d), d)))
        else:
            best_dx = 0

        candidate_vectors.add((best_dx, best_dy))

    # Always test (0, 0)
    candidate_vectors.add((0, 0))

    # Sort hypotheses deterministically
    sorted_hypotheses = sorted(candidate_vectors, key=lambda v: (abs(v[0]) + abs(v[1]), abs(v[0]), v[0], v[1]))

    # Evaluate each hypothesis with integer block SAD
    evaluations = []

    for dx, dy in sorted_hypotheses:
        inliers = 0
        for _, by, bx in selected_blocks:
            py0 = by - dy
            py1 = py0 + block_size
            px0 = bx - dx
            px1 = px0 + block_size

            if 0 <= py0 and py1 <= H and 0 <= px0 and px1 <= W:
                c_blk = cur_int[by:by + block_size, bx:bx + block_size]
                p_blk = prev_int[py0:py1, px0:px1]
                blk_sad = int(np.sum(np.abs(c_blk - p_blk)))
                _add_ops(block_size * block_size)
                # Inlier threshold: SAD <= 1 per pixel (i.e. <= block_size * block_size)
                if blk_sad <= block_size * block_size:
                    inliers += 1

        evaluations.append((inliers, dx, dy))

    # Require at least 30 percent of selected textured blocks
    # Integer arithmetic: (inliers * 100) >= (30 * num_selected)
    valid_evals = [item for item in evaluations if item[0] * 100 >= 30 * num_selected]

    if not valid_evals:
        return None

    # Pick hypothesis with most inliers, ties broken by smaller magnitude
    max_inliers = max(item[0] for item in valid_evals)
    top_inlier_candidates = [item for item in valid_evals if item[0] == max_inliers]

    # Verify candidates with full-frame integer SAD
    scored_candidates = []
    for inliers, dx, dy in top_inlier_candidates:
        sad_norm = _compute_full_sad(cur_arr, prev_arr, dx, dy)
        scored_candidates.append((sad_norm, abs(dx) + abs(dy), dx, dy, inliers))

    scored_candidates.sort(key=lambda item: (item[0], item[1], abs(item[2]), item[2], item[3]))

    best_score, best_mag, best_dx, best_dy, best_inliers = scored_candidates[0]

    if best_score >= (1 << 50):
        return None

    # Check for periodic / ambiguous frame aliases
    if np.array_equal(cur_arr, prev_arr):
        return (0, 0, 0)

    # If there are multiple distinct top candidate vectors with identical score and inliers
    tied_top = [c for c in scored_candidates if c[0] == best_score and c[4] == best_inliers]
    if len(tied_top) > 1:
        # Ambiguous periodic frame
        return None

    return (int(best_dx), int(best_dy), int(best_score))


def consensus_tiles(prev, cur, s, dx, dy):
    """Return a numpy bool array of shape (gh, gw) indicating tiles where entire destination tile

    equals translated previous pixels in-bounds (EDGE_RULE).
    Tiles whose source leaves the frame are False.
    Vectorized with numpy.
    """
    prev_arr = np.asarray(prev, dtype=np.uint8)
    cur_arr = np.asarray(cur, dtype=np.uint8)
    _add_ops(prev_arr.size + cur_arr.size)

    H, W = cur_arr.shape
    grid_s, gw, gh = grid_params(W, H)

    # Compute in-bounds validity for source tile coordinates
    ty_indices = np.arange(gh)
    tx_indices = np.arange(gw)

    cy0 = ty_indices * s
    cy1 = np.minimum((ty_indices + 1) * s, H)

    cx0 = tx_indices * s
    cx1 = np.minimum((tx_indices + 1) * s, W)

    py0 = cy0 - dy
    py1 = cy1 - dy

    px0 = cx0 - dx
    px1 = cx1 - dx

    valid_y = (py0 >= 0) & (py1 <= H)  # shape (gh,)
    valid_x = (px0 >= 0) & (px1 <= W)  # shape (gw,)
    valid_tiles = valid_y[:, None] & valid_x[None, :]  # shape (gh, gw)
    _add_ops(gh * gw)

    if not np.any(valid_tiles):
        return np.zeros((gh, gw), dtype=bool)

    # Efficient vectorized tile comparison
    if H % s == 0 and W % s == 0:
        prev_shifted = np.zeros((H, W), dtype=prev_arr.dtype)

        overlap_cy0, overlap_cy1 = max(0, dy), min(H, H + dy)
        overlap_cx0, overlap_cx1 = max(0, dx), min(W, W + dx)

        if overlap_cy0 < overlap_cy1 and overlap_cx0 < overlap_cx1:
            py0_ov = overlap_cy0 - dy
            py1_ov = overlap_cy1 - dy
            px0_ov = overlap_cx0 - dx
            px1_ov = overlap_cx1 - dx
            prev_shifted[overlap_cy0:overlap_cy1, overlap_cx0:overlap_cx1] = prev_arr[py0_ov:py1_ov, px0_ov:px1_ov]

        eq_pixels = (cur_arr == prev_shifted)
        _add_ops(cur_arr.size)

        tile_equal = np.all(eq_pixels.reshape(gh, s, gw, s), axis=(1, 3))
        _add_ops(cur_arr.size)

        return valid_tiles & tile_equal
    else:
        # Fallback for unpadded frame dimensions
        mask = np.zeros((gh, gw), dtype=bool)
        for ty in range(gh):
            if not valid_y[ty]:
                continue
            cy_s, cy_e = cy0[ty], cy1[ty]
            py_s, py_e = py0[ty], py1[ty]
            for tx in range(gw):
                if not valid_x[tx]:
                    continue
                cx_s, cx_e = cx0[tx], cx1[tx]
                px_s, px_e = px0[tx], px1[tx]

                cur_tile = cur_arr[cy_s:cy_e, cx_s:cx_e]
                prev_tile = prev_arr[py_s:py_e, px_s:px_e]
                _add_ops(cur_tile.size)
                if cur_tile.shape == prev_tile.shape and np.array_equal(cur_tile, prev_tile):
                    mask[ty, tx] = True
        return mask
