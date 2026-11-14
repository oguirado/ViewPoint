"""Global motion from independently verified runs of non-static rows.

All luma and intermediate pixel arrays stay in RAM; inputs are never modified.
Positive dx/dy moves right/down: cur[y, x] == prev[y - dy, x - dx].

OPS counts array elements cast, compared, transformed, or reduced, following
the other v2x variants. It is an integer work counter, not a timing estimate.
"""

import operator

import numpy as np

OPS = 0


def reset_ops():
    """Reset the module-level integer operation counter."""
    global OPS
    OPS = 0


def _add_ops(count):
    global OPS
    OPS += int(count)


def _frames(prev_luma, cur_luma):
    prev = np.asarray(prev_luma)
    cur = np.asarray(cur_luma)
    if (prev.ndim != 2 or cur.ndim != 2 or prev.shape != cur.shape
            or prev.dtype != np.uint8 or cur.dtype != np.uint8):
        raise ValueError("luma must be matching uint8 (H, W) arrays")
    return prev, cur


def _row_signatures(frame):
    """Sum absolute horizontal gradients without uint8 subtraction wraparound."""
    wide = frame.astype(np.int64)
    gradients = wide[:, 1:] - wide[:, :-1]
    signatures = np.abs(gradients).sum(axis=1, dtype=np.int64)
    _add_ops(frame.size + 3 * gradients.size)
    return signatures


def _moving_runs(prev, cur):
    """Return maximal non-static [start, stop) row runs of length at least 8."""
    same_signature = _row_signatures(prev) == _row_signatures(cur)
    same_pixels = np.all(prev == cur, axis=1)
    static = same_signature & same_pixels
    moving = np.concatenate(([False], ~static, [False]))
    boundaries = np.flatnonzero(moving[1:] != moving[:-1])
    _add_ops(2 * prev.size + 3 * len(static) + 2 * (len(static) + 1))
    return [(int(start), int(stop))
            for start, stop in boundaries.reshape(-1, 2)
            if stop - start >= 8]


def _projection_shift(prev_profile, cur_profile, max_d):
    """Minimise profile MAD with >= half overlap and deterministic ties.

    Profiles contain int64 sums, proportional to their means by the same
    constant for every candidate. Compare SAD / overlap by cross multiplication
    with Python ints, so neither float rounding nor integer division creates
    artificial ties. Ties prefer smaller absolute shift, then smaller shift.
    """
    length = len(cur_profile)
    if not length:
        return None
    radius = min(max_d, length // 2)
    best_shift = None
    best_sad, best_overlap = 0, 1
    for shift in range(-radius, radius + 1):
        start, stop = max(0, shift), min(length, length + shift)
        overlap = stop - start
        if 2 * overlap < length:
            continue
        diff = cur_profile[start:stop] - prev_profile[start - shift:stop - shift]
        sad = int(np.abs(diff).sum(dtype=np.int64))
        _add_ops(3 * overlap)
        lhs, rhs = sad * best_overlap, best_sad * overlap
        if (best_shift is None or lhs < rhs
                or (lhs == rhs and (abs(shift), shift)
                    < (abs(best_shift), best_shift))):
            best_shift, best_sad, best_overlap = shift, sad, overlap
    return best_shift


def _verify_run(prev, cur, start, stop, dx, dy):
    """Count pixels in fully equal overlap rows; require 90% of the whole run.

    Both current and shifted previous rows belong to the same run. Exposed
    rows do not verify. Horizontal exposure is excluded from each row's pixel
    comparison, and at least half the row width must remain in bounds.
    """
    width = cur.shape[1]
    cy0, cy1 = max(start, start + dy), min(stop, stop + dy)
    cx0, cx1 = max(0, dx), min(width, width + dx)
    if (cy0 >= cy1 or cx0 >= cx1 or 2 * (cy1 - cy0) < stop - start
            or 2 * (cx1 - cx0) < width):
        return None
    current = cur[cy0:cy1, cx0:cx1]
    previous = prev[cy0 - dy:cy1 - dy, cx0 - dx:cx1 - dx]
    equal_rows = np.all(current == previous, axis=1)
    verified_rows = int(np.count_nonzero(equal_rows))
    _add_ops(2 * current.size + equal_rows.size)
    if 10 * verified_rows < 9 * (stop - start):
        return None
    return verified_rows * (cx1 - cx0), cy0, cy1, cx0, cx1


def estimate_global_motion(prev_luma, cur_luma, max_d):
    """Return (int dx, int dy, verified pixel share) or None.

    Each non-static run supplies one projection hypothesis. Select the model
    with the most pixels in fully verified rows, breaking equal support by
    the first run in frame order. The score is that support in integer ppm of H * W.
    Verification needs at least 90% of the original run height, including
    exposure; a scroll exposing more than 10% of a run therefore abstains.
    """
    prev, cur = _frames(prev_luma, cur_luma)
    max_d = operator.index(max_d)
    if max_d < 0:
        raise ValueError("max_d must be nonnegative")
    height, width = cur.shape
    if not height or not width:
        return None

    best = None
    best_pixels = 0
    for start, stop in _moving_runs(prev, cur):
        prev_band, cur_band = prev[start:stop], cur[start:stop]
        prev_rows = prev_band.sum(axis=1, dtype=np.int64)
        cur_rows = cur_band.sum(axis=1, dtype=np.int64)
        _add_ops(prev_band.size + cur_band.size)
        dy = _projection_shift(prev_rows, cur_rows, max_d)

        # Restrict both column profiles to vertically corresponding run rows.
        cy0, cy1 = max(start, start + dy), min(stop, stop + dy)
        current = cur[cy0:cy1]
        previous = prev[cy0 - dy:cy1 - dy]
        prev_cols = previous.sum(axis=0, dtype=np.int64)
        cur_cols = current.sum(axis=0, dtype=np.int64)
        _add_ops(previous.size + current.size)
        dx = _projection_shift(prev_cols, cur_cols, max_d)

        verified = _verify_run(prev, cur, start, stop, dx, dy)
        if verified is not None and verified[0] > best_pixels:
            best_pixels = verified[0]
            best = (int(dx), int(dy), (best_pixels * 1_000_000) // (height * width))
    return best


def consensus_tiles(prev_luma, cur_luma, s, dx, dy):
    """Return a row-major bool mask for the best run verifying (dx, dy).

    This is stateless: no cached frame or prior estimate is required. Select
    by verified pixel support, keeping the first run on ties. Only complete
    s-by-s tiles inside that run's shifted overlap can be claimed. Each tile
    must itself match exactly, including rows with mismatches in other tiles.
    Partial tiles on the right or bottom frame edge are not claimed.
    """
    prev, cur = _frames(prev_luma, cur_luma)
    s, dx, dy = operator.index(s), operator.index(dx), operator.index(dy)
    if s <= 0:
        raise ValueError("s must be positive")
    height, width = cur.shape
    mask = np.zeros(((height + s - 1) // s, (width + s - 1) // s), dtype=bool)
    if not height or not width:
        return mask

    best = None
    for start, stop in _moving_runs(prev, cur):
        verified = _verify_run(prev, cur, start, stop, dx, dy)
        if verified is not None and (best is None or verified[0] > best[0]):
            best = verified
    if best is None:
        return mask

    _, cy0, cy1, cx0, cx1 = best
    ty0, ty1 = (cy0 + s - 1) // s, cy1 // s
    tx0, tx1 = (cx0 + s - 1) // s, cx1 // s
    if ty0 >= ty1 or tx0 >= tx1:
        return mask
    y0, y1, x0, x1 = ty0 * s, ty1 * s, tx0 * s, tx1 * s
    equal = (cur[y0:y1, x0:x1]
             == prev[y0 - dy:y1 - dy, x0 - dx:x1 - dx])
    mask[ty0:ty1, tx0:tx1] = equal.reshape(ty1 - ty0, s, tx1 - tx0, s).all(axis=(1, 3))
    _add_ops(2 * equal.size)
    return mask
