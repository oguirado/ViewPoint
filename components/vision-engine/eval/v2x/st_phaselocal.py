"""Local Phase Correlation Sub-Tile Motion Variant for V2 Claim-Only Post-Pass.

Algorithm:
    Computes local phase correlation using 2D FFT on mean-subtracted 3x3
    neighbourhood tile windows around the requested tile.

    1. Extracts 3x3 neighbourhood tile windows around tile_xy = (tx, ty)
       for cur_luma and prev_luma.
    2. Subtracts window means from both windows.
    3. Computes 2D FFT phase correlation matrix R = IFFT2( FA * conj(FB) / |FA * conj(FB)| ).
    4. Finds the top integer peaks in R.
    5. For up to the 4 highest integer peaks in descending order of correlation magnitude:
       - Evaluates both signed displacement interpretations (+v, -v) within max_d.
       - Tests each against the hard rule (EDGE_RULE): the whole destination tile pixels
         (clipped to frame boundaries) must equal the previous frame translated by that vector
         with the whole source inside the frame, and the tile is not equal to the previous frame at zero shift.
       - Returns the first unique vector that passes the hard rule.
       - If multiple distinct vectors at the same peak pass or evidence cannot separate them,
         returns None.

API:
    subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d) -> tuple (dx, dy, n) or None
    n reports the evidence count: the number of changed pixels in the 3x3 neighbourhood window.

Work Counting (OPS):
    OPS is a module-level integer tracking the number of elementary element operations performed
    (array element comparisons, additions, multiplications, FFT transforms, and slice reductions).
    Call reset_ops() to reset OPS to 0.
"""

import numpy as np
from visioncore.layout import grid_params

OPS = 0


def reset_ops():
    """Reset the global OPS work counter to zero."""
    global OPS
    OPS = 0


def subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d):
    """Find sub-tile integer translation (dx, dy) for tile_xy using local phase correlation.

    Args:
        prev_luma (np.ndarray or array-like): 2D array (H, W) of previous frame luma values.
        cur_luma (np.ndarray or array-like): 2D array (H, W) of current frame luma values.
        s (int or None): Tile size in pixels (e.g. 16, 32).
        tile_xy (tuple): (tx, ty) tile grid index.
        max_d (int): Maximum search displacement in pixels (abs(dx) <= max_d, abs(dy) <= max_d).

    Returns:
        tuple (dx, dy, n) or None:
            dx, dy: Integer pixel translation from prev to cur frame.
            n: Number of changed pixels in the 3x3 neighbourhood window.
            Returns None if zero shift holds, windows are flat, or no candidate passes hard rule.
    """
    global OPS
    prev_luma = np.asarray(prev_luma)
    cur_luma = np.asarray(cur_luma)
    H, W = cur_luma.shape

    s_grid, gw, gh = grid_params(W, H)
    if s is None:
        s = s_grid

    tx, ty = tile_xy
    if not (0 <= tx < gw and 0 <= ty < gh):
        return None

    # Destination tile pixel bounds clipped at frame edges
    tx0 = tx * s
    tx1 = min(W, (tx + 1) * s)
    ty0 = ty * s
    ty1 = min(H, (ty + 1) * s)

    cur_tile = cur_luma[ty0:ty1, tx0:tx1]
    prev_tile = prev_luma[ty0:ty1, tx0:tx1]

    # Zero shift check
    OPS += cur_tile.size
    if np.array_equal(cur_tile, prev_tile):
        return None

    # 3x3 tile neighbourhood bounds in tile grid
    tx_min = max(0, tx - 1)
    tx_max = min(gw - 1, tx + 1)
    ty_min = max(0, ty - 1)
    ty_max = min(gh - 1, ty + 1)

    # 3x3 neighbourhood pixel bounds
    x0 = tx_min * s
    x1 = min(W, (tx_max + 1) * s)
    y0 = ty_min * s
    y1 = min(H, (ty_max + 1) * s)

    w_cur = cur_luma[y0:y1, x0:x1]
    w_prev = prev_luma[y0:y1, x0:x1]

    OPS += w_cur.size + w_prev.size

    # Evidence count n: changed pixels in 3x3 neighbourhood
    diff = w_cur != w_prev
    OPS += diff.size
    n = int(np.count_nonzero(diff))
    if n == 0:
        return None

    # Mean subtraction
    OPS += w_cur.size + w_prev.size
    w_cur_f = w_cur.astype(np.float64)
    w_prev_f = w_prev.astype(np.float64)

    mean_c = np.mean(w_cur_f)
    mean_p = np.mean(w_prev_f)
    OPS += w_cur_f.size + w_prev_f.size

    A = w_cur_f - mean_c
    B = w_prev_f - mean_p
    OPS += A.size + B.size

    # Check for flat / zero variance windows
    std_A = np.std(A)
    std_B = np.std(B)
    OPS += A.size + B.size
    if std_A < 1e-9 or std_B < 1e-9:
        return None

    # 2D FFT Phase Correlation
    Ny, Nx = A.shape
    FA = np.fft.fft2(A)
    FB = np.fft.fft2(B)
    OPS += A.size + B.size

    cross = FA * np.conj(FB)
    OPS += cross.size

    denom = np.abs(cross)
    OPS += cross.size

    eps = 1e-12
    denom_safe = np.where(denom > eps, denom, 1.0)
    R = cross / denom_safe
    OPS += R.size * 2

    r = np.real(np.fft.ifft2(R))
    OPS += r.size

    # Sort all integer locations (iy, ix) in r by r[iy, ix] descending
    flat_indices = np.argsort(-r.ravel())
    OPS += r.size

    tested_peaks = 0
    i = 0
    total_elements = r.size
    seen_vectors = set()

    while i < total_elements and tested_peaks < 4:
        iy = int(flat_indices[i] // Nx)
        ix = int(flat_indices[i] % Nx)

        # Raw displacement corresponding to FFT indices (iy, ix)
        dx_raw = ix if ix <= Nx // 2 else ix - Nx
        dy_raw = iy if iy <= Ny // 2 else iy - Ny

        i += 1

        # Signed interpretations
        interpretations = [(dx_raw, dy_raw), (-dx_raw, -dy_raw)]

        # Filter valid candidates: inside max_d, non-zero, and not previously evaluated across peaks
        valid_candidates = []
        for cdx, cdy in interpretations:
            if cdx == 0 and cdy == 0:
                continue
            if abs(cdx) <= max_d and abs(cdy) <= max_d:
                if (cdx, cdy) not in seen_vectors:
                    seen_vectors.add((cdx, cdy))
                    valid_candidates.append((cdx, cdy))

        if not valid_candidates:
            continue

        tested_peaks += 1

        # Evaluate Hard Rule for valid candidates of this peak
        passing = []
        for cdx, cdy in valid_candidates:
            # Source tile bounds in prev_luma:
            # Destination tile: cur_luma[ty0:ty1, tx0:tx1]
            # Since cur[y, x] = prev[y - dy, x - dx],
            # prev y range: [ty0 - cdy, ty1 - cdy]
            # prev x range: [tx0 - cdx, tx1 - cdx]
            py0 = ty0 - cdy
            py1 = ty1 - cdy
            px0 = tx0 - cdx
            px1 = tx1 - cdx

            # Check whole source inside frame
            if px0 < 0 or px1 > W or py0 < 0 or py1 > H:
                continue

            prev_trans = prev_luma[py0:py1, px0:px1]
            OPS += cur_tile.size
            if np.array_equal(cur_tile, prev_trans):
                passing.append((cdx, cdy))

        if len(passing) == 1:
            return (passing[0][0], passing[0][1], n)
        elif len(passing) > 1:
            # Multiple distinct vectors at this peak pass; evidence cannot separate them
            return None

    return None
