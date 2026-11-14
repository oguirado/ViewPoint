"""eval/v2x/gm_textline.py - Two-stage text-line alignment global motion estimator.

Design goal:
TARGET: At most 16.7 ms per 1920x1088 frame.

OPS Unit Description:
Operations count (OPS) tracks the number of elementary pixel or array element
operations (comparisons, additions, reductions, array indexing, slicing, bitwise
operations) performed during global motion estimation and consensus tile checking.
"""

from collections import defaultdict
import numpy as np

OPS = 0


def reset_ops():
    """Reset the global operations counter."""
    global OPS
    OPS = 0


def get_ops():
    """Get current global operations count."""
    global OPS
    return OPS


def _hash_line_64(line_edges):
    """Compute deterministic 64-bit integer hash of relative 1D line edge profile."""
    global OPS
    OPS += line_edges.size
    if len(line_edges) == 0:
        return 0

    total_edges = int(np.sum(line_edges))
    if total_edges <= 0:
        norm_profile = np.zeros_like(line_edges)
    else:
        norm_profile = (line_edges * 32) // total_edges

    data = norm_profile.astype(np.int64).tobytes()
    rem = len(data) % 8
    if rem != 0:
        data += b"\x00" * (8 - rem)
    u64_arr = np.frombuffer(data, dtype=np.uint64)
    h = np.uint64(14695981039346656037)
    prime = np.uint64(1099511628211)
    for val in u64_arr:
        h = np.uint64((int(h ^ val) * int(prime)) & 0xFFFFFFFFFFFFFFFF)
    return int(h)


class _TextLine:
    __slots__ = ("start_row", "height", "line_hash")

    def __init__(self, start_row, height, line_hash):
        self.start_row = start_row
        self.height = height
        self.line_hash = line_hash


def _segment_lines(img, edge_thresh=5):
    """Segment frame into horizontal text lines based on row edge profiles."""
    global OPS
    H, W = img.shape
    if W < 2 or H < 1:
        return []

    # Horizontal edge counts per row
    diff = np.abs(img[:, 1:].astype(np.int16) - img[:, :-1].astype(np.int16))
    row_edge_counts = np.sum(diff > edge_thresh, axis=1)

    max_edges = int(np.max(row_edge_counts)) if len(row_edge_counts) > 0 else 0
    if max_edges < 10:
        return []

    min_edges = max(15, max_edges // 3)
    row_has_edge = row_edge_counts >= min_edges
    OPS += H * (W - 1) + H

    lines = []
    in_line = False
    start_row = 0

    for r in range(H):
        if row_has_edge[r]:
            if not in_line:
                in_line = True
                start_row = r
        else:
            if in_line:
                in_line = False
                height = r - start_row
                line_edges = row_edge_counts[start_row:r]
                line_hash = _hash_line_64(line_edges)
                lines.append(_TextLine(start_row, height, line_hash))

    if in_line:
        height = H - start_row
        line_edges = row_edge_counts[start_row:H]
        line_hash = _hash_line_64(line_edges)
        lines.append(_TextLine(start_row, height, line_hash))

    return lines


def _compute_full_sad(cur_arr, prev_arr, dx, dy):
    """Compute exact full-frame normalized SAD between cur and prev shifted by (dx, dy)."""
    global OPS
    H, W = cur_arr.shape
    cy0, cy1 = max(0, dy), min(H, H + dy)
    cx0, cx1 = max(0, dx), min(W, W + dx)

    if cy0 >= cy1 or cx0 >= cx1:
        return 1 << 60

    py0, py1 = cy0 - dy, cy1 - dy
    px0, px1 = cx0 - dx, cx1 - dx

    overlap_area = (cy1 - cy0) * (cx1 - cx0)
    total_area = H * W

    cur_sub = cur_arr[cy0:cy1, cx0:cx1].astype(np.int64)
    prev_sub = prev_arr[py0:py1, px0:px1].astype(np.int64)

    sad = np.sum(np.abs(cur_sub - prev_sub))
    sad_norm = (sad * total_area) // overlap_area
    OPS += overlap_area * 2
    return int(sad_norm)


def estimate_global_motion(prev_luma, cur_luma, max_d):
    """Estimate global integer displacement (dx, dy) between prev_luma and cur_luma.

    Returns (dx, dy, score) or None if flat, periodic, or ambiguous.
    """
    global OPS
    prev_arr = np.asarray(prev_luma, dtype=np.uint8)
    cur_arr = np.asarray(cur_luma, dtype=np.uint8)

    H, W = cur_arr.shape
    OPS += H * W * 2

    # Flat frame check
    min_cur, max_cur = int(np.min(cur_arr)), int(np.max(cur_arr))
    min_prev, max_prev = int(np.min(prev_arr)), int(np.max(prev_arr))
    if min_cur == max_cur and min_prev == max_prev:
        return None

    # Stage 1: Segment frames into text lines
    cur_lines = _segment_lines(cur_arr)
    prev_lines = _segment_lines(prev_arr)

    # Align line sequences
    matches = defaultdict(list)
    for c_line in cur_lines:
        for p_line in prev_lines:
            if c_line.height == p_line.height and c_line.line_hash == p_line.line_hash:
                dy = c_line.start_row - p_line.start_row
                if abs(dy) <= max_d:
                    matches[dy].append((c_line, p_line))

    if not matches:
        return None

    max_matches = max(len(v) for v in matches.values())
    if max_matches < 3:
        return None

    top_dys = [dy for dy, lst in matches.items() if len(lst) == max_matches]

    # Check for distant tie / ambiguity in dy
    if len(top_dys) > 1:
        if max(abs(top_dys[0] - top_dys[i]) for i in range(1, len(top_dys))) >= 4:
            return None

    best_dy_init = top_dys[0]

    # Refine dy within +/- 2 px by 1D row SAD
    best_dy = best_dy_init
    best_row_sad = 1 << 60

    for dy_cand in range(best_dy_init - 2, best_dy_init + 3):
        if abs(dy_cand) > max_d:
            continue
        cy0, cy1 = max(0, dy_cand), min(H, H + dy_cand)
        py0, py1 = cy0 - dy_cand, cy1 - dy_cand
        if cy0 >= cy1:
            continue
        c_rows = cur_arr[cy0:cy1, :].sum(axis=1, dtype=np.int64)
        p_rows = prev_arr[py0:py1, :].sum(axis=1, dtype=np.int64)
        overlap_h = cy1 - cy0
        sad_1d = int(np.sum(np.abs(c_rows - p_rows)))
        sad_norm = (sad_1d * H) // overlap_h
        OPS += overlap_h * W + overlap_h
        if sad_norm < best_row_sad or (sad_norm == best_row_sad and abs(dy_cand) < abs(best_dy)):
            best_row_sad = sad_norm
            best_dy = dy_cand

    # Stage 2: Find dx on column profiles of aligned overlap
    cy0, cy1 = max(0, best_dy), min(H, H + best_dy)
    py0, py1 = cy0 - best_dy, cy1 - best_dy
    if cy0 >= cy1:
        return None

    c_cols = cur_arr[cy0:cy1, :].sum(axis=0, dtype=np.int64)
    p_cols = prev_arr[py0:py1, :].sum(axis=0, dtype=np.int64)
    OPS += (cy1 - cy0) * W * 2

    max_dx = min(max_d, W - 1)

    ds = 4
    if W >= 128:
        W_coarse = W // ds
        c_coarse = c_cols[:W_coarse * ds].reshape(W_coarse, ds).sum(axis=1)
        p_coarse = p_cols[:W_coarse * ds].reshape(W_coarse, ds).sum(axis=1)
        max_dx_coarse = max_dx // ds

        coarse_cands = []
        for dx_c in range(-max_dx_coarse, max_dx_coarse + 1):
            if dx_c >= 0:
                sub_c = c_coarse[dx_c:]
                sub_p = p_coarse[:W_coarse - dx_c]
            else:
                sub_c = c_coarse[:W_coarse + dx_c]
                sub_p = p_coarse[-dx_c:]
            overlap = len(sub_c)
            if overlap <= 0:
                continue
            sad = int(np.sum(np.abs(sub_c - sub_p)))
            sad_norm = (sad * W_coarse) // overlap
            OPS += overlap
            coarse_cands.append((sad_norm, abs(dx_c), dx_c))

        coarse_cands.sort(key=lambda item: (item[0], item[1], item[2]))
        top_coarse_dxs = [dx_c * ds for _, _, dx_c in coarse_cands[:8]]
    else:
        top_coarse_dxs = list(range(-max_dx, max_dx + 1))

    refine_dxs = set()
    for c_dx in top_coarse_dxs:
        for dx in range(c_dx - ds, c_dx + ds + 1):
            if abs(dx) <= max_dx:
                refine_dxs.add(dx)
    if 0 <= max_dx:
        refine_dxs.add(0)

    col_candidates = []
    for dx in refine_dxs:
        if dx >= 0:
            c_sub = c_cols[dx:]
            p_sub = p_cols[:W - dx]
        else:
            c_sub = c_cols[:W + dx]
            p_sub = p_cols[-dx:]

        overlap = len(c_sub)
        if overlap <= 0:
            continue

        sad = int(np.sum(np.abs(c_sub - p_sub)))
        sad_norm = (sad * W) // overlap
        OPS += overlap
        col_candidates.append((sad_norm, abs(dx), dx))

    if not col_candidates:
        return None

    col_candidates.sort(key=lambda item: (item[0], item[1], item[2]))

    # Evaluate top 2D candidates (dx, best_dy)
    top_dx_cands = [dx for _, _, dx in col_candidates[:8]]
    if 0 not in top_dx_cands:
        top_dx_cands.append(0)

    dx_scores = []
    for dx_cand in top_dx_cands:
        score = _compute_full_sad(cur_arr, prev_arr, dx_cand, best_dy)
        dx_scores.append((score, dx_cand, best_dy))

    dx_scores.sort(key=lambda item: (item[0], abs(item[1]) + abs(item[2]), item[1], item[2]))

    best_score, best_dx, best_dy = dx_scores[0]

    # Alias / Periodicity / Ambiguity check
    non_adj_scores = [
        item for item in dx_scores
        if max(abs(item[1] - best_dx), abs(item[2] - best_dy)) >= 4
    ]

    if non_adj_scores:
        second_best_score = non_adj_scores[0][0]
        if second_best_score == best_score:
            # If tie at 0 score, check if one of them is zero motion (0, 0)
            if best_score == 0 and (best_dx, best_dy) != (0, 0):
                # Check if (0, 0) is among perfect matches
                if any(s == 0 and (x, y) == (0, 0) for s, x, y in dx_scores):
                    return (0, 0, 0)
            return None
        if second_best_score - best_score < 100:
            return None

    return (int(best_dx), int(best_dy), int(best_score))


def consensus_tiles(prev, cur, s, dx, dy):
    """Return a boolean (gh, gw) numpy array of tiles whose content equals

    prev content shifted by (dx, dy).
    """
    global OPS
    prev_arr = np.asarray(prev, dtype=np.uint8)
    cur_arr = np.asarray(cur, dtype=np.uint8)

    H, W = cur_arr.shape
    gh = (H + s - 1) // s
    gw = (W + s - 1) // s

    OPS += H * W

    cy0_min = max(0, dy)
    cy1_max = min(H, H + dy)
    cx0_min = max(0, dx)
    cx1_max = min(W, W + dx)

    if cy0_min >= cy1_max or cx0_min >= cx1_max:
        return np.zeros((gh, gw), dtype=bool)

    match_plane = np.zeros((gh * s, gw * s), dtype=bool)
    match_plane[H:, :] = True
    match_plane[:, W:] = True

    cur_sub = cur_arr[cy0_min:cy1_max, cx0_min:cx1_max]
    prev_sub = prev_arr[cy0_min - dy:cy1_max - dy, cx0_min - dx:cx1_max - dx]

    match_plane[cy0_min:cy1_max, cx0_min:cx1_max] = (cur_sub == prev_sub)

    tile_mask = match_plane.reshape(gh, s, gw, s).all(axis=(1, 3))
    return tile_mask
