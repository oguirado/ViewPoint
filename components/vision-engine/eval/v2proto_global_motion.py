"""eval/v2proto_global_motion.py - Research prototype for vision v2 global motion estimation.

Provides coarse-to-fine 1D projection + 2D block-sampled SAD global motion estimation.
Integer arithmetic only, deterministic.
"""

import numpy as np
from visioncore.layout import grid_params

# Global SAD evaluation counter for cost regression tracking
_SAD_EVAL_COUNT = 0


def reset_sad_eval_count():
    """Reset the global SAD evaluation counter."""
    global _SAD_EVAL_COUNT
    _SAD_EVAL_COUNT = 0


def get_sad_eval_count():
    """Get the global SAD evaluation counter."""
    global _SAD_EVAL_COUNT
    return _SAD_EVAL_COUNT


def _compute_1d_projections(frame):
    """Compute 1D row sums and column sums of luma frame.

    Returns (row_sums, col_sums) as int64 arrays.
    """
    arr = np.asarray(frame, dtype=np.int64)
    row_sums = np.sum(arr, axis=1)  # shape (H,)
    col_sums = np.sum(arr, axis=0)  # shape (W,)
    return row_sums, col_sums


def _search_1d_shifts(cur_proj, prev_proj, max_shift, top_k=8):
    """Search for 1D shifts between cur_proj and prev_proj in range [-max_shift, max_shift].

    cur_proj[i] ~ prev_proj[i - shift].
    Normalized SAD = sum(|cur[i] - prev[i - shift]|) * N // overlap_length.
    Returns list of candidate shifts sorted by (normalized_sad, abs(shift), shift).
    """
    N = len(cur_proj)
    candidates = []

    for shift in range(-max_shift, max_shift + 1):
        if abs(shift) >= N:
            continue
        if shift >= 0:
            c_sub = cur_proj[shift:]
            p_sub = prev_proj[:N - shift]
        else:
            c_sub = cur_proj[:N + shift]
            p_sub = prev_proj[-shift:]

        overlap = len(c_sub)
        if overlap <= 0:
            continue

        sad = np.sum(np.abs(c_sub - p_sub))
        sad_norm = (sad * N) // overlap
        candidates.append((int(sad_norm), shift))

    candidates.sort(key=lambda item: (item[0], abs(item[1]), item[1]))

    # Return top K distinct shift values
    top_shifts = []
    seen = set()
    for _, shift in candidates:
        if shift not in seen:
            seen.add(shift)
            top_shifts.append(shift)
            if len(top_shifts) >= top_k:
                break

    return top_shifts, candidates


def _compute_sparse_grid_sad(cur, prev, dx, dy, sample_coords, block_size=16):
    """Compute SAD over a sparse fixed grid of sample blocks.

    cur[y, x] corresponds to prev[y - dy, x - dx].
    sample_coords is a list of (y, x) block top-left coordinates.
    Increments global _SAD_EVAL_COUNT per block checked.
    Returns normalized SAD scaled to full frame area (or 1 << 60 if out of bounds / no valid blocks).
    """
    global _SAD_EVAL_COUNT

    H, W = cur.shape
    total_sad = 0
    valid_blocks = 0
    total_block_pixels = 0

    for cy0, cx0 in sample_coords:
        cy1, cx1 = cy0 + block_size, cx0 + block_size
        py0, py1 = cy0 - dy, cy1 - dy
        px0, px1 = cx0 - dx, cx1 - dx

        if cy0 >= 0 and cy1 <= H and cx0 >= 0 and cx1 <= W and \
           py0 >= 0 and py1 <= H and px0 >= 0 and px1 <= W:
            c_block = cur[cy0:cy1, cx0:cx1].astype(np.int64)
            p_block = prev[py0:py1, px0:px1].astype(np.int64)
            total_sad += np.sum(np.abs(c_block - p_block))
            valid_blocks += 1
            total_block_pixels += (block_size * block_size)
            _SAD_EVAL_COUNT += 1

    if valid_blocks == 0 or total_block_pixels == 0:
        return 1 << 60

    # Scale total_sad to full frame area H * W
    sad_norm = (total_sad * (H * W)) // total_block_pixels
    return int(sad_norm)


def _compute_full_sad(cur, prev, dx, dy):
    """Compute exact full-frame SAD between cur and prev shifted by (dx, dy).

    cur[y, x] corresponds to prev[y - dy, x - dx].
    Increments global _SAD_EVAL_COUNT.
    """
    global _SAD_EVAL_COUNT

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

    sad = np.sum(np.abs(cur_sub - prev_sub))
    sad_norm = (sad * total_area) // overlap_area
    _SAD_EVAL_COUNT += 1
    return int(sad_norm)


def estimate_global_motion(prev_luma, cur_luma, max_d, k_top=16):
    """Estimate global integer displacement (dx, dy) between prev_luma and cur_luma.

    Returns (dx, dy, score) or None if flat, periodic, or ambiguous.
    Uses 1D row/column projections to filter translation candidates,
    followed by sparse 2D block verification and local Level-0 refinement.
    """
    prev_arr = np.asarray(prev_luma, dtype=np.uint8)
    cur_arr = np.asarray(cur_luma, dtype=np.uint8)

    H, W = cur_arr.shape

    # Flat frame check
    if np.max(cur_arr) == np.min(cur_arr) and np.max(prev_arr) == np.min(prev_arr):
        return None

    # 1D Row & Column projections
    cur_rows, cur_cols = _compute_1d_projections(cur_arr)
    prev_rows, prev_cols = _compute_1d_projections(prev_arr)

    # 1D shift search over full max_d range
    max_dy = min(max_d, H - 1)
    max_dx = min(max_d, W - 1)

    top_dys, row_candidates_all = _search_1d_shifts(cur_rows, prev_rows, max_dy, top_k=8)
    top_dxs, col_candidates_all = _search_1d_shifts(cur_cols, prev_cols, max_dx, top_k=8)

    # Always include 0 in candidate shifts if not present
    if 0 not in top_dys:
        top_dys.append(0)
    if 0 not in top_dxs:
        top_dxs.append(0)

    # Generate 2D candidate pairs (dx, dy)
    candidate_vectors = set()
    for dy in top_dys:
        for dx in top_dxs:
            if abs(dx) <= max_d and abs(dy) <= max_d:
                candidate_vectors.add((dx, dy))

    # Define fixed grid of sample blocks for sparse 2D SAD evaluation
    block_size = 16
    grid_rows = 8
    grid_cols = 8

    step_y = max(block_size, (H - block_size) // max(1, grid_rows - 1))
    step_x = max(block_size, (W - block_size) // max(1, grid_cols - 1))

    sample_coords = []
    for gy in range(grid_rows):
        by = min(gy * step_y, H - block_size)
        if by < 0:
            continue
        for gx in range(grid_cols):
            bx = min(gx * step_x, W - block_size)
            if bx < 0:
                continue
            sample_coords.append((by, bx))

    # Evaluate sparse SAD for candidate vectors
    sparse_scores = []
    for dx, dy in candidate_vectors:
        score = _compute_sparse_grid_sad(cur_arr, prev_arr, dx, dy, sample_coords, block_size)
        sparse_scores.append((score, dx, dy))

    sparse_scores.sort(key=lambda item: (item[0], abs(item[1]) + abs(item[2]), item[1], item[2]))

    # Keep top coarse candidate vectors without pruning true vectors
    top_coarse = [(dx, dy) for _, dx, dy in sparse_scores[:k_top]]

    # Local refinement & full SAD scoring at Level 0
    lvl0_scores = []
    seen_lvl0 = set()

    for c_dx, c_dy in top_coarse:
        # Evaluate local neighborhood around top coarse candidates
        r = 2  # local search radius around coarse estimate
        for dy in range(c_dy - r, c_dy + r + 1):
            for dx in range(c_dx - r, c_dx + r + 1):
                if abs(dx) <= max_d and abs(dy) <= max_d and (dx, dy) not in seen_lvl0:
                    seen_lvl0.add((dx, dy))
                    score = _compute_full_sad(cur_arr, prev_arr, dx, dy)
                    lvl0_scores.append((score, dx, dy))

    lvl0_scores.sort(key=lambda item: (item[0], abs(item[1]) + abs(item[2]), item[1], item[2]))

    if not lvl0_scores:
        return None

    best_score, best_dx, best_dy = lvl0_scores[0]

    # Uniqueness test: check non-adjacent displacements
    # Include distant alias rejection
    non_adj_scores = [
        item for item in lvl0_scores
        if max(abs(item[1] - best_dx), abs(item[2] - best_dy)) >= 4
    ]

    if non_adj_scores:
        second_best_score = non_adj_scores[0][0]
        if second_best_score == best_score:
            return None
        if second_best_score - best_score < 100:
            return None

    # Check 1D projection alias ambiguity (e.g. periodic stripes or repeated text without unique 2D match)
    # If 1D row projection has identical minimum score at multiple distant shifts AND full SAD score is high
    if best_score > 0:
        best_row_sad = row_candidates_all[0][0]
        row_aliases = [s for sad, s in row_candidates_all if sad == best_row_sad and abs(s - best_dy) >= 8]
        if row_aliases:
            # Check if any alias has almost identical full SAD
            for alias_dy in row_aliases:
                alias_score = _compute_full_sad(cur_arr, prev_arr, best_dx, alias_dy)
                if abs(alias_score - best_score) < 100:
                    return None

        best_col_sad = col_candidates_all[0][0]
        col_aliases = [s for sad, s in col_candidates_all if sad == best_col_sad and abs(s - best_dx) >= 8]
        if col_aliases:
            for alias_dx in col_aliases:
                alias_score = _compute_full_sad(cur_arr, prev_arr, alias_dx, best_dy)
                if abs(alias_score - best_score) < 100:
                    return None

    return (int(best_dx), int(best_dy), int(best_score))


def consensus_tiles(prev, cur, s, dx, dy):
    """Return a boolean (gh, gw) numpy array of tiles whose content equals

    prev content shifted by (dx, dy). Uses visioncore.layout.grid_params.
    """
    prev_arr = np.asarray(prev, dtype=np.uint8)
    cur_arr = np.asarray(cur, dtype=np.uint8)

    H, W = cur_arr.shape
    grid_s, gw, gh = grid_params(W, H)

    mask = np.zeros((gh, gw), dtype=bool)

    for ty in range(gh):
        for tx in range(gw):
            cy0, cy1 = ty * s, min((ty + 1) * s, H)
            cx0, cx1 = tx * s, min((tx + 1) * s, W)

            py0, py1 = cy0 - dy, cy1 - dy
            px0, px1 = cx0 - dx, cx1 - dx

            if py0 < 0 or py1 > H or px0 < 0 or px1 > W:
                mask[ty, tx] = False
                continue

            cur_tile = cur_arr[cy0:cy1, cx0:cx1]
            prev_tile = prev_arr[py0:py1, px0:px1]

            if cur_tile.shape == prev_tile.shape and np.array_equal(cur_tile, prev_tile):
                mask[ty, tx] = True
            else:
                mask[ty, tx] = False

    return mask
