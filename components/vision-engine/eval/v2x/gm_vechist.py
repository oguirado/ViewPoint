"""eval/v2x/gm_vechist.py - Global motion estimation via local block vector histogramming.

Design Goal:
  TARGET: At most 16.7 ms per 1920x1088 frame (1080p @ 60 FPS).
  Note: 16.7 ms is a design target, not a performance measurement.

Algorithm:
  1. Sample up to 128 textured 32x32 blocks on a 64-pixel lattice grid.
  2. For each block, search exact displacement over a candidate set formed by:
     - The 6 best 1D row projection shift candidates and top 6 1D column projection shifts.
     - A local 2D search window of radius 8 ([-8, 8] in dx and dy).
  3. Accept a block displacement vector only when the 32x32 block matches the previous
     frame exactly.
  4. Build a histogram of accepted block vectors.
  5. Select the mode vector if it accounts for at least 25% of accepted blocks and has
     at least twice the count of the runner-up vector.
  6. Verify candidate mode vector with full-frame integer SAD.

Work Accounting (OPS):
  Module-level integer OPS counts elementary pixel/element operations performed.
  Unit: One operation unit corresponds to one element processed in array comparisons,
  reductions, or transformations (e.g. element-wise subtraction, sum, abs, or equality check).
"""

import numpy as np

# Global operation counter for work accounting
OPS = 0


def reset_ops():
    """Reset the global OPS counter to 0."""
    global OPS
    OPS = 0
    return 0


def _add_ops(count):
    """Add count elementary operations to global OPS."""
    global OPS
    OPS += int(count)


def _compute_1d_projections(frame):
    """Compute 1D row sums and column sums of luma frame."""
    H, W = frame.shape
    row_sums = np.sum(frame, axis=1, dtype=np.int64)
    col_sums = np.sum(frame, axis=0, dtype=np.int64)
    _add_ops(H * W * 2)
    return row_sums, col_sums


def _search_1d_shifts(cur_proj, prev_proj, max_shift, top_k=6):
    """Search 1D projection shifts in [-max_shift, max_shift].

    Returns list of top K shift candidates sorted by normalized SAD.
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

        abs_diff = np.abs(c_sub - p_sub)
        sad = np.sum(abs_diff)
        _add_ops(2 * overlap)

        sad_norm = (int(sad) * N) // overlap
        candidates.append((sad_norm, abs(shift), shift))

    candidates.sort(key=lambda item: (item[0], item[1], item[2]))

    top_shifts = []
    seen = set()
    for _, _, shift in candidates:
        if shift not in seen:
            seen.add(shift)
            top_shifts.append(shift)
            if len(top_shifts) >= top_k:
                break

    return top_shifts


def estimate_global_motion(prev_luma, cur_luma, max_d):
    """Estimate global integer displacement (dx, dy) between prev_luma and cur_luma.

    Returns (dx, dy, score) or None if flat, periodic, or ambiguous.
    Uses local block vector histogramming on a 64-pixel lattice.
    """
    cur_arr = np.asarray(cur_luma, dtype=np.uint8)
    prev_arr = np.asarray(prev_luma, dtype=np.uint8)

    H, W = cur_arr.shape
    if H < 32 or W < 32:
        return None

    # Flat frame check
    c_min, c_max = np.min(cur_arr), np.max(cur_arr)
    p_min, p_max = np.min(prev_arr), np.max(prev_arr)
    _add_ops(2 * H * W + 2 * H * W)
    if c_min == c_max and p_min == p_max:
        return None

    # Compute 1D projections
    cur_rows, cur_cols = _compute_1d_projections(cur_arr)
    prev_rows, prev_cols = _compute_1d_projections(prev_arr)

    max_dy = min(max_d, H - 1)
    max_dx = min(max_d, W - 1)

    top_dys = _search_1d_shifts(cur_rows, prev_rows, max_dy, top_k=6)
    top_dxs = _search_1d_shifts(cur_cols, prev_cols, max_dx, top_k=6)

    # Candidate set of vectors: 1D projection top pairs + local window radius 8
    candidate_vectors = set()
    for dy in top_dys:
        for dx in top_dxs:
            if abs(dx) <= max_d and abs(dy) <= max_d:
                candidate_vectors.add((dx, dy))

    for r_dy in range(-8, 9):
        for r_dx in range(-8, 9):
            if abs(r_dx) <= max_d and abs(r_dy) <= max_d:
                candidate_vectors.add((r_dx, r_dy))

    # Sample up to 128 textured 32x32 blocks on a 64-pixel lattice
    block_size = 32
    lattice_step = 64

    candidate_blocks = []
    for by in range(0, H - block_size + 1, lattice_step):
        for bx in range(0, W - block_size + 1, lattice_step):
            c_block = cur_arr[by:by + block_size, bx:bx + block_size]
            b_min, b_max = int(np.min(c_block)), int(np.max(c_block))
            _add_ops(2 * block_size * block_size)
            texture_score = b_max - b_min
            if texture_score >= 12:
                candidate_blocks.append((texture_score, by, bx))

    if not candidate_blocks:
        return None

    # Subsample up to 128 textured blocks uniformly across spatial grid
    n_cand = len(candidate_blocks)
    if n_cand > 128:
        indices = np.linspace(0, n_cand - 1, 128, dtype=int)
        selected_blocks = [candidate_blocks[i] for i in indices]
    else:
        selected_blocks = candidate_blocks

    # Search block vector displacement matches
    vec_counts = {}
    accepted_blocks_count = 0

    for _, by, bx in selected_blocks:
        c_block = cur_arr[by:by + block_size, bx:bx + block_size]
        c00 = c_block[0, 0]
        c15 = c_block[15, 15]
        c31 = c_block[31, 31]

        matched_any = False

        for dx, dy in candidate_vectors:
            py0, py1 = by - dy, by - dy + block_size
            px0, px1 = bx - dx, bx - dx + block_size

            if py0 >= 0 and py1 <= H and px0 >= 0 and px1 <= W:
                # Early scalar corner check
                if (prev_arr[py0, px0] == c00 and
                        prev_arr[py0 + 15, px0 + 15] == c15 and
                        prev_arr[py0 + 31, px0 + 31] == c31):
                    _add_ops(3)
                    p_block = prev_arr[py0:py1, px0:px1]
                    _add_ops(block_size * block_size)
                    if np.array_equal(c_block, p_block):
                        matched_any = True
                        vec_counts[(dx, dy)] = vec_counts.get((dx, dy), 0) + 1
                else:
                    _add_ops(1)

        if matched_any:
            accepted_blocks_count += 1

    if accepted_blocks_count == 0 or not vec_counts:
        return None

    # Rank histogram candidate vectors
    sorted_vecs = sorted(
        vec_counts.items(),
        key=lambda item: (item[1], -(abs(item[0][0]) + abs(item[0][1]))),
        reverse=True
    )

    best_vec, best_count = sorted_vecs[0]
    runner_up_count = sorted_vecs[1][1] if len(sorted_vecs) > 1 else 0

    # Mode validation: >= 25% of accepted blocks and >= 2x runner-up
    if best_count * 4 < accepted_blocks_count:
        return None
    if best_count < 2 * runner_up_count:
        return None

    best_dx, best_dy = best_vec

    # Verify candidate vector with full-frame integer SAD
    cy0, cy1 = max(0, best_dy), min(H, H + best_dy)
    cx0, cx1 = max(0, best_dx), min(W, W + best_dx)

    overlap_area = (cy1 - cy0) * (cx1 - cx0)
    if overlap_area <= 0:
        return None

    py0, py1 = cy0 - best_dy, cy1 - best_dy
    px0, px1 = cx0 - best_dx, cx1 - best_dx

    c_sub = cur_arr[cy0:cy1, cx0:cx1].astype(np.int64)
    p_sub = prev_arr[py0:py1, px0:px1].astype(np.int64)

    diff = np.abs(c_sub - p_sub)
    sad = np.sum(diff)
    _add_ops(4 * overlap_area)

    sad_norm = (int(sad) * (H * W)) // overlap_area
    return (int(best_dx), int(best_dy), int(sad_norm))


def consensus_tiles(prev_luma, cur_luma, s, dx, dy):
    """Return a boolean (gh, gw) numpy array of consensus tiles.

    A tile is True iff the entire s x s destination tile equals the in-bounds
    translated previous pixels.
    Partial edge tiles and tiles whose source leaves the frame are False.
    Vectorized with NumPy.
    """
    cur_arr = np.asarray(cur_luma, dtype=np.uint8)
    prev_arr = np.asarray(prev_luma, dtype=np.uint8)

    H, W = cur_arr.shape
    gh = (H + s - 1) // s
    gw = (W + s - 1) // s

    mask = np.zeros((gh, gw), dtype=bool)

    full_gh = H // s
    full_gw = W // s

    if full_gh == 0 or full_gw == 0:
        _add_ops(gh * gw)
        return mask

    # Check tile source bounds in previous frame
    ty_indices = np.arange(full_gh)
    tx_indices = np.arange(full_gw)

    py0 = ty_indices * s - dy
    py1 = py0 + s
    px0 = tx_indices * s - dx
    px1 = px0 + s

    valid_ty = (py0 >= 0) & (py1 <= H)
    valid_tx = (px0 >= 0) & (px1 <= W)
    valid_mask = valid_ty[:, None] & valid_tx[None, :]

    _add_ops(full_gh + full_gw + full_gh * full_gw)

    # Pixel equality in overlapping region
    cy0, cy1 = max(0, dy), min(H, H + dy)
    cx0, cx1 = max(0, dx), min(W, W + dx)

    if cy0 < cy1 and cx0 < cx1:
        py_start, py_end = cy0 - dy, cy1 - dy
        px_start, px_end = cx0 - dx, cx1 - dx

        overlap_h = cy1 - cy0
        overlap_w = cx1 - cx0

        cur_sub = cur_arr[cy0:cy1, cx0:cx1]
        prev_sub = prev_arr[py_start:py_end, px_start:px_end]

        eq_sub = (cur_sub == prev_sub)
        _add_ops(overlap_h * overlap_w)

        equal_grid = np.zeros((H, W), dtype=bool)
        equal_grid[cy0:cy1, cx0:cx1] = eq_sub

        # Reshape full tiles to check all pixels in each s x s block
        cur_full_eq = equal_grid[:full_gh * s, :full_gw * s]
        tile_eq = cur_full_eq.reshape(full_gh, s, full_gw, s).all(axis=(1, 3))
        _add_ops(full_gh * full_gw * s * s)

        mask[:full_gh, :full_gw] = tile_eq & valid_mask
    else:
        _add_ops(full_gh * full_gw)

    return mask
