"""Deterministic v2.1 integer global motion fast kernel and whole-real-tile consensus.

Preserves bit-exact parity with eval.v2x.gm_bands_hv on motion tuples, abstentions,
consensus-mask bytes, and context-local OPS / OPS_DETAIL evidence while accelerating
evaluation via precomputed integral projections, 2D uninformative-pixel integral tables,
stride-cached 1D projection windows, and lazy/probed tile reductions.
"""

from contextvars import ContextVar
from dataclasses import dataclass
from functools import cmp_to_key, wraps
import threading
import numpy as np

_ops = ContextVar("bands_ops", default=0)
_diagnostics = ContextVar("bands_diagnostics", default=None)

_U_MAX = np.maximum
_U_MIN = np.minimum
_U_SUB = np.subtract
_U_ABS = np.abs
_U_MUL = np.multiply
_U_XOR = np.bitwise_xor
_U_OR = np.bitwise_or

_TLS = threading.local()


def __getattr__(name):
    if name == "OPS":
        return _ops.get()
    if name == "OPS_DETAIL":
        return dict(_diagnostics.get() or {})
    raise AttributeError(name)


def reset_ops():
    """Reset this context's per-call operation and verifier diagnostics."""
    _ops.set(0)
    _diagnostics.set({})


def _add_ops(count):
    _ops.set(_ops.get() + int(count))


def _record(name, count=1):
    counters = _diagnostics.get()
    if counters is None:
        counters = {}
        _diagnostics.set(counters)
    counters[name] = counters.get(name, 0) + int(count)


def _per_call(function):
    @wraps(function)
    def counted(*args, **kwargs):
        reset_ops()
        return function(*args, **kwargs)
    return counted


def _joint_sad(cur, prev, cutoff):
    """Exact dense SAD, or a strict lower bound after exceeding cutoff.

    A caller needing the exact rational residual must supply cutoff=None.
    Returning early is safe only for a consumer which can already reject that
    lower bound. The v2.1 verifier always requests the complete residual.
    """
    _record("dense_sad_calls")
    if cutoff is None:
        c = cur.astype(np.int16)
        p = prev.astype(np.int16)
        total = int(np.abs(c - p).sum(dtype=np.int64))
        _add_ops(cur.size * 2)
        _record("dense_sad_pixels", cur.size)
        return total
    total = 0
    for row in range(0, cur.shape[0], 32):
        c = cur[row:row + 32].astype(np.int64)
        p = prev[row:row + 32].astype(np.int64)
        total += int(np.abs(c - p).sum(dtype=np.int64))
        _add_ops(c.size * 2)
        _record("dense_sad_pixels", c.size)
        if total > cutoff:
            break
    return total


# The dense kernel budget bounds only full-rectangle SAD calls. Every remaining
# proposal still receives exact, counted informative-pixel residual verification.
DENSE_SAD_BUDGET = 9

TILE = 16
CHROME_EDGE_CAP = 64
CHROME_EDGE_DIVISOR = 4
CHROME_SPAN_DIVISOR = 8
CHROME_SEPARATION = 16
PROJECTION_SMOOTH = 8
PROJECTION_COARSE_KEEP = 8
PROJECTION_RAW_KEEP = 8
PROJECTION_AXIS_KEEP = 4
PROJECTION_BASIN_KEEP = 1
PROJECTION_REGION_KEEP = 3
PROJECTION_REGION_LIMIT = 8
MIN_OVERLAP = 16
MAX_MIN_OVERLAP = 64
ALIAS_DISTANCE = 4
ALIAS_MIN_LUMA = 1
PPT = 1000
PRIMARY_MIN_PPT = 600
SECONDARY_VETO_PPT = 250
PRIMARY_RUNNER_RATIO = 3
TILE_WEIGHT_CAP = 128
MIN_SUPPORT_WEIGHT = 512
MIN_SUPPORT_TILES = 4
MIN_SUPPORT_ROWS = 2
MIN_SUPPORT_COLS = 2

_SMOOTH_KERNEL = np.ones(PROJECTION_SMOOTH, dtype=np.int64)


# ---------------------------------------------------------------------------
# Precomputed Integral-Projection & Stride-Cached Helpers + Structural Seams
# ---------------------------------------------------------------------------


def _build_col_block_integral(frame):
    """Precompute 16-row block vertical prefix sums (H//16 + 1, W) in int32 for L1-resident column projections."""
    h, w = frame.shape
    nb = h >> 4
    b_int = np.empty((nb + 1, w), dtype=np.int32)
    b_int[0] = 0
    if nb > 0:
        bsums = frame[:nb << 4].reshape(nb, 16, w).sum(axis=1, dtype=np.uint16)
        np.cumsum(bsums, axis=0, dtype=np.int32, out=b_int[1:])
    return (frame, b_int)


def _query_col_block_integral(pack, lo, hi):
    """Query exact column sums on [lo:hi] using 16-row block integral table plus <=15 remainder rows."""
    frame, b_int = pack
    b0 = (lo + 15) >> 4
    b1 = hi >> 4
    if b0 > b1:
        return frame[lo:hi].sum(axis=0, dtype=np.uint32).astype(np.int32)
    res = b_int[b1] - b_int[b0]
    r0 = b0 << 4
    r1 = b1 << 4
    if lo < r0:
        res = res + frame[lo:r0].sum(axis=0, dtype=np.uint16)
    if r1 < hi:
        res = res + frame[r1:hi].sum(axis=0, dtype=np.uint16)
    return res


def _col_integral_projection(frame):
    """Precompute vertical prefix sums (H+1, W) in int32 for O(W) band column projections."""
    h, w = frame.shape
    out = np.empty((h + 1, w), dtype=np.int32)
    out[0] = 0
    np.cumsum(frame, axis=0, dtype=np.int32, out=out[1:])
    return out


def _row_integral_projection(frame):
    """Precompute horizontal prefix sums (H, W+1) in int32 for O(H) band row projections."""
    h, w = frame.shape
    out = np.empty((h, w + 1), dtype=np.int32)
    out[:, 0] = 0
    np.cumsum(frame, axis=1, dtype=np.int32, out=out[:, 1:])
    return out


def _rect_integral_2d(mask):
    """Precompute 2D integral table (H+1, W+1) in int32 for O(1) rectangle count queries."""
    h, w = mask.shape
    out = np.zeros((h + 1, w + 1), dtype=np.int32)
    np.cumsum(mask.view(np.uint8), axis=0, dtype=np.int32, out=out[1:, 1:])
    np.cumsum(out[1:, 1:], axis=1, dtype=np.int32, out=out[1:, 1:])
    return out


def _rect_sum_2d(integral, y0, y1, x0, x1):
    """Query exact count inside [y0:y1, x0:x1] from a 2D integral table in O(1)."""
    if y0 >= y1 or x0 >= x1:
        return 0
    return int(integral[y1, x1] - integral[y0, x1] - integral[y1, x0] + integral[y0, x0])


def _get_stride_workspace(length, max_shift):
    """Return thread-local stride-cached buffers and overlap denominators for 1D ranking."""
    cache = getattr(_TLS, "stride_cache", None)
    if cache is None:
        cache = {}
        _TLS.stride_cache = cache
    key = (int(length), int(max_shift))
    entry = cache.get(key)
    if entry is None:
        c_pad = np.zeros(length + max_shift, dtype=np.int32)
        p_pad = np.zeros(length + max_shift, dtype=np.int32)
        buf = np.empty((max_shift, length), dtype=np.int32)
        c_win = np.lib.stride_tricks.as_strided(
            c_pad[1:], shape=(max_shift, length), strides=(4, 4)
        )
        p_win = np.lib.stride_tricks.as_strided(
            p_pad[1:], shape=(max_shift, length), strides=(4, 4)
        )
        sh_arange = np.arange(1, max_shift + 1, dtype=np.int64)
        ov = length - sh_arange
        pos_tie = (sh_arange << 14) | (8192 + sh_arange)
        neg_tie = (sh_arange << 14) | (8192 - sh_arange)
        all_keys = np.empty(2 * max_shift + 1, dtype=np.int64)
        shifts = list(range(1, max_shift + 1))
        neg_shifts = list(range(-1, -max_shift - 1, -1))
        ops = 2 * (length + 2 * max_shift * length - max_shift * (max_shift + 1))
        entry = (c_pad, p_pad, buf, c_win, p_win, ov, pos_tie, neg_tie, all_keys, shifts, neg_shifts, int(ops))
        if len(cache) > 64:
            cache.clear()
        cache[key] = entry
    return entry


def _get_coarse_stride_workspace(length, max_shift, step=PROJECTION_SMOOTH):
    """Return thread-local stride-cached buffers for step-aligned coarse 1D ranking."""
    cache = getattr(_TLS, "coarse_stride_cache", None)
    if cache is None:
        cache = {}
        _TLS.coarse_stride_cache = cache
    n_coarse = max_shift // step
    key = (int(length), int(max_shift), int(step))
    entry = cache.get(key)
    if entry is None:
        max_c_shift = n_coarse * step
        c_pad = np.zeros(length + max_c_shift, dtype=np.int32)
        p_pad = np.zeros(length + max_c_shift, dtype=np.int32)
        buf = np.empty((n_coarse, length), dtype=np.int32)
        c_win = np.lib.stride_tricks.as_strided(
            c_pad[step:], shape=(n_coarse, length), strides=(4 * step, 4)
        )
        p_win = np.lib.stride_tricks.as_strided(
            p_pad[step:], shape=(n_coarse, length), strides=(4 * step, 4)
        )
        c_shifts_arr = np.arange(step, max_c_shift + 1, step, dtype=np.int64)
        ov = length - c_shifts_arr
        tail_idx = c_shifts_arr - 1
        pos_tie = (c_shifts_arr << 14) | (8192 + c_shifts_arr)
        neg_tie = (c_shifts_arr << 14) | (8192 - c_shifts_arr)
        all_keys = np.empty(2 * n_coarse + 1, dtype=np.int64)
        shifts = c_shifts_arr.tolist()
        neg_shifts = (-c_shifts_arr).tolist()
        ops = 2 * (length + 2 * max_shift * length - max_shift * (max_shift + 1))
        entry = (c_pad, p_pad, buf, c_win, p_win, ov, tail_idx, pos_tie, neg_tie, all_keys, shifts, neg_shifts, int(ops))
        if len(cache) > 64:
            cache.clear()
        cache[key] = entry
    return entry


def _get_u8_buffers(shape):
    """Return thread-local reusable uint8 buffers sliced from a flat arena (zero reallocations)."""
    h, w = shape
    need = h * w
    arena = getattr(_TLS, "u8_arena", None)
    if arena is None or arena[0].size < need:
        cap = max(need, 1088 * 1920)
        arena = (np.empty(cap, dtype=np.uint8), np.empty(cap, dtype=np.uint8))
        _TLS.u8_arena = arena
    return arena[0][:need].reshape(h, w), arena[1][:need].reshape(h, w)


def _inputs(prev_luma, cur_luma, max_d):
    """Validate rather than silently quantizing a float or a malformed frame."""
    prev, cur = np.asarray(prev_luma), np.asarray(cur_luma)
    if prev.dtype != np.uint8 or cur.dtype != np.uint8:
        raise TypeError("luma must be uint8")
    if prev.ndim != 2 or prev.shape != cur.shape or min(prev.shape) == 0:
        raise ValueError("frames must have the same nonempty H by W shape")
    if not isinstance(max_d, (int, np.integer)) or isinstance(max_d, (bool, np.bool_)):
        raise TypeError("max_d must be an integer")
    if max_d < 0:
        raise ValueError("max_d must be nonnegative")
    return prev, cur, int(max_d)


def compact_chrome_mask(prev, cur, _changed=None):
    """Conservative row-span proposal, independent of any winning displacement.

    Only an edge row with a compact changed span, a flat identical remainder,
    and a separate interior broad-change row can have its changed pixels masked.  A forty-pixel
    clock is therefore ineligible at widths 256 and 300.  Unchanged textured
    remainders never qualify; the span is never split to satisfy the width cap.
    """
    h, w = prev.shape
    _add_ops(h * w * 4)
    changed = (prev != cur) if _changed is None else _changed
    active = changed.any(axis=1)
    first = np.argmax(changed, axis=1)
    last = w - np.argmax(changed[:, ::-1], axis=1)
    span = np.where(active, last - first, 0)
    edge = min(CHROME_EDGE_CAP, h // CHROME_EDGE_DIVISOR)
    rows_idx = np.arange(h)
    broad = np.flatnonzero((span > w // CHROME_SPAN_DIVISOR)
                           & (rows_idx >= edge)
                           & (rows_idx < h - edge))
    mask = np.zeros(prev.shape, dtype=bool)
    if broad.size == 0:
        return mask
    b0, b_last = int(broad[0]), int(broad[-1])
    for y in np.flatnonzero(active & (span <= w // CHROME_SPAN_DIVISOR)):
        y = int(y)
        separated = ((y < edge and y + CHROME_SEPARATION < b0)
                     or (y >= h - edge and y - CHROME_SEPARATION > b_last))
        if not separated:
            continue
        x0, x1 = int(first[y]), int(last[y])
        remainder_p = np.concatenate((prev[y, :x0], prev[y, x1:]))
        remainder_c = np.concatenate((cur[y, :x0], cur[y, x1:]))
        if (remainder_p.size and np.array_equal(remainder_p, remainder_c)
                and int(remainder_p.min()) == int(remainder_p.max())):
            mask[y, x0:x1] = changed[y, x0:x1]
    return mask


def _informative_with_edges(frame, allowed=None):
    """Compute informative_pixels along with horizontal and vertical edge masks."""
    _add_ops(frame.size * 5)
    if allowed is None:
        horizontal = frame[:, 1:] != frame[:, :-1]
        vertical = frame[1:] != frame[:-1]
    else:
        horizontal = ((frame[:, 1:] != frame[:, :-1])
                      & allowed[:, 1:] & allowed[:, :-1])
        vertical = ((frame[1:] != frame[:-1]) & allowed[1:] & allowed[:-1])
    h_info = np.zeros(frame.shape, dtype=bool)
    h_info[:, 1:] |= horizontal
    h_info[:, :-1] |= horizontal
    v_info = np.zeros(frame.shape, dtype=bool)
    v_info[1:, :] |= vertical
    v_info[:-1, :] |= vertical
    info = h_info | v_info
    if allowed is not None:
        info &= allowed
    return info, horizontal, vertical, h_info, v_info


def informative_pixels(frame, allowed=None):
    """Nonzero four-neighbour variation; endpoints must both be observable.

    This Boolean integer predicate gives flat-flat comparisons zero weight,
    including differently coloured flat interiors.  A textured endpoint
    remains informative when the endpoint in the other frame is flat.
    """
    info, _, _, _, _ = _informative_with_edges(frame, allowed)
    return info


@dataclass
class Prepared:
    prev: np.ndarray
    cur: np.ndarray
    allowed: np.ndarray
    prev_info: np.ndarray
    cur_info: np.ndarray
    band: tuple
    changed: np.ndarray | None = None
    allowed_all: bool | None = None
    prev_h: np.ndarray | None = None
    prev_v: np.ndarray | None = None
    cur_h: np.ndarray | None = None
    cur_v: np.ndarray | None = None
    prev_hi: np.ndarray | None = None
    prev_vi: np.ndarray | None = None
    cur_hi: np.ndarray | None = None
    cur_vi: np.ndarray | None = None
    prev_inner_all_info: bool | None = None
    cur_inner_all_info: bool | None = None
    mostly_uninfo: bool | None = None
    cur_top_ok: np.ndarray | None = None
    cur_bot_ok: np.ndarray | None = None
    cur_left_ok: np.ndarray | None = None
    cur_right_ok: np.ndarray | None = None
    prev_top_ok: np.ndarray | None = None
    prev_bot_ok: np.ndarray | None = None
    prev_left_ok: np.ndarray | None = None
    prev_right_ok: np.ndarray | None = None
    uninfo_prev_int: np.ndarray | None = None
    uninfo_cur_int: np.ndarray | None = None


def _compute_border_ok_1d(h_edge, v_edge, h_info, v_info):
    """Precompute 1D per-row/per-col boolean flags indicating zero False pixels along interior border."""
    h, w = h_info.shape
    if h <= 2 or w <= 2:
        z_h = np.zeros(max(1, h - 1), dtype=bool)
        z_w = np.zeros(max(1, w - 1), dtype=bool)
        return z_h, z_h, z_w, z_w
    top_ok = (v_edge[:, 1:-1] | h_info[:-1, 1:-1]).sum(axis=1, dtype=np.int32) == (w - 2)
    bot_ok = (v_edge[:, 1:-1] | h_info[1:, 1:-1]).sum(axis=1, dtype=np.int32) == (w - 2)
    left_ok = (h_edge[1:-1, :] | v_info[1:-1, :-1]).sum(axis=0, dtype=np.int32) == (h - 2)
    right_ok = (h_edge[1:-1, :] | v_info[1:-1, 1:]).sum(axis=0, dtype=np.int32) == (h - 2)
    return top_ok, bot_ok, left_ok, right_ok


def _ensure_prepared_caches(state):
    """Populate fast caches lazily if Prepared was constructed directly by a caller."""
    if state.allowed_all is None:
        state.allowed_all = bool(np.all(state.allowed))
    if state.changed is None:
        state.changed = state.prev != state.cur
    if state.prev_hi is None or state.cur_hi is None:
        saved = _ops.get()
        _, ph, pv, phi, pvi = _informative_with_edges(
            state.prev, None if state.allowed_all else state.allowed
        )
        _, ch, cv, chi, cvi = _informative_with_edges(
            state.cur, None if state.allowed_all else state.allowed
        )
        _ops.set(saved)
        state.prev_h, state.prev_v, state.prev_hi, state.prev_vi = ph, pv, phi, pvi
        state.cur_h, state.cur_v, state.cur_hi, state.cur_vi = ch, cv, chi, cvi
    if state.cur_top_ok is None:
        state.cur_top_ok, state.cur_bot_ok, state.cur_left_ok, state.cur_right_ok = _compute_border_ok_1d(
            state.cur_h, state.cur_v, state.cur_hi, state.cur_vi
        )
        state.prev_top_ok, state.prev_bot_ok, state.prev_left_ok, state.prev_right_ok = _compute_border_ok_1d(
            state.prev_h, state.prev_v, state.prev_hi, state.prev_vi
        )
    if state.cur_inner_all_info is None:
        h, w = state.cur.shape
        if h > 2 and w > 2:
            c_nz = int(np.count_nonzero(state.cur_info[1:h - 1, 1:w - 1]))
            inner_area = (h - 2) * (w - 2)
            state.cur_inner_all_info = c_nz == inner_area
            state.mostly_uninfo = c_nz * 2 < inner_area
        else:
            state.cur_inner_all_info = False
            state.mostly_uninfo = True
    if state.prev_inner_all_info is None:
        h, w = state.prev.shape
        state.prev_inner_all_info = True if state.cur_inner_all_info else (
            (int(np.count_nonzero(state.prev_info[1:h - 1, 1:w - 1])) == (h - 2) * (w - 2))
            if (h > 2 and w > 2 and not state.mostly_uninfo) else False
        )


def _prepare(prev, cur):
    _add_ops(prev.size * 3)
    changed = prev != cur
    chrome = compact_chrome_mask(prev, cur, _changed=changed)
    allowed_all = int(np.count_nonzero(chrome)) == 0
    allowed = ~chrome
    if allowed_all:
        pi, ph, pv, phi, pvi = _informative_with_edges(prev, None)
        ci, ch, cv, chi, cvi = _informative_with_edges(cur, None)
        active = changed | pi | ci
    else:
        pi, ph, pv, phi, pvi = _informative_with_edges(prev, allowed)
        ci, ch, cv, chi, cvi = _informative_with_edges(cur, allowed)
        active = allowed & (changed | pi | ci)
    rows, cols = np.flatnonzero(active.any(axis=1)), np.flatnonzero(active.any(axis=0))
    if not rows.size or not cols.size:
        return None
    band = (int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1)
    h, w = cur.shape
    if h > 2 and w > 2:
        inner_area = (h - 2) * (w - 2)
        c_nz = int(np.count_nonzero(ci[1:h - 1, 1:w - 1]))
        c_all = c_nz == inner_area
        mostly_uninfo = c_nz * 2 < inner_area
        p_all = True if c_all else (
            (int(np.count_nonzero(pi[1:h - 1, 1:w - 1])) == inner_area)
            if not mostly_uninfo else False
        )
    else:
        c_all = p_all = False
        mostly_uninfo = True
    ct_ok, cb_ok, cl_ok, cr_ok = _compute_border_ok_1d(ch, cv, chi, cvi)
    pt_ok, pb_ok, pl_ok, pr_ok = _compute_border_ok_1d(ph, pv, phi, pvi)
    return Prepared(
        prev, cur, allowed, pi, ci, band,
        changed=changed,
        allowed_all=allowed_all,
        prev_h=ph,
        prev_v=pv,
        cur_h=ch,
        cur_v=cv,
        prev_hi=phi,
        prev_vi=pvi,
        cur_hi=chi,
        cur_vi=cvi,
        prev_inner_all_info=p_all,
        cur_inner_all_info=c_all,
        mostly_uninfo=mostly_uninfo,
        cur_top_ok=ct_ok,
        cur_bot_ok=cb_ok,
        cur_left_ok=cl_ok,
        cur_right_ok=cr_ok,
        prev_top_ok=pt_ok,
        prev_bot_ok=pb_ok,
        prev_left_ok=pl_ok,
        prev_right_ok=pr_ok,
    )


def min_ov_y(band_h):
    return max(MIN_OVERLAP, min(MAX_MIN_OVERLAP, int(band_h) // 4))


def _overlap(band, dx, dy):
    y0, y1, x0, x1 = band
    return max(y0, y0 + dy), min(y1, y1 + dy), max(x0, x0 + dx), min(x1, x1 + dx)


def _tile_sums(mask, s=TILE):
    h, w = mask.shape
    _add_ops(mask.size)
    gh, gw = (h + s - 1) // s, (w + s - 1) // s
    padded = np.pad(mask, ((0, gh * s - h), (0, gw * s - w)), constant_values=0)
    return padded.reshape(gh, s, gw, s).sum(axis=(1, 3), dtype=np.int64)


@dataclass
class Evidence:
    dx: int
    dy: int
    raw_sad: int
    N_dyn: int
    score: int
    changed_weight: int
    support_weight: int
    weights: np.ndarray
    valid: np.ndarray
    matched: np.ndarray
    changed_info: np.ndarray


def _tile_sums_crop(mask, y0, x0, shape):
    """Reduce a crop on the original global grid without frame-sized padding."""
    h, w = mask.shape
    top, left = y0 % TILE, x0 % TILE
    gh, gw = (top + h + TILE - 1) // TILE, (left + w + TILE - 1) // TILE
    padded_size = gh * TILE * gw * TILE
    _add_ops(padded_size)

    u8 = mask.view(np.uint8) if mask.dtype == np.bool_ else mask.astype(np.uint8, copy=False)
    r0 = (TILE - top) % TILE
    if r0 > h:
        r0 = h
    full_r = (h - r0) // TILE
    r1 = r0 + full_r * TILE
    row_sums = np.empty((gh, w), dtype=np.uint8)
    idx = 0
    if r0 > 0:
        row_sums[0] = u8[:r0].sum(axis=0, dtype=np.uint8)
        idx = 1
    if full_r > 0:
        row_sums[idx:idx + full_r] = u8[r0:r1].reshape(full_r, TILE, w).sum(axis=1, dtype=np.uint8)
        idx += full_r
    if r1 < h:
        row_sums[idx] = u8[r1:].sum(axis=0, dtype=np.uint8)

    c0 = (TILE - left) % TILE
    if c0 > w:
        c0 = w
    full_c = (w - c0) // TILE
    c1 = c0 + full_c * TILE
    result = np.zeros(((shape[0] + TILE - 1) // TILE,
                       (shape[1] + TILE - 1) // TILE), dtype=np.int64)
    ty0, tx0 = y0 // TILE, x0 // TILE
    local = result[ty0:ty0 + gh, tx0:tx0 + gw]
    cidx = 0
    if c0 > 0:
        local[:, 0] = row_sums[:, :c0].sum(axis=1, dtype=np.uint16)
        cidx = 1
    if full_c > 0:
        local[:, cidx:cidx + full_c] = row_sums[:, c0:c1].reshape(gh, full_c, TILE).sum(axis=2, dtype=np.uint16)
        cidx += full_c
    if c1 < w:
        local[:, cidx] = row_sums[:, c1:].sum(axis=1, dtype=np.uint16)
    return result


def _border_pair_1d(state, cy0, cy1, cx0, cx1, py0, py1, px0, px1):
    """Compute combined (c | p) 1D informative masks for the 4 crop borders using precomputed h_info/v_info."""
    ch, cv, chi, cvi = state.cur_h, state.cur_v, state.cur_hi, state.cur_vi
    ph, pv, phi, pvi = state.prev_h, state.prev_v, state.prev_hi, state.prev_vi
    top_i = cv[cy0, cx0:cx1] | pv[py0, px0:px1] | chi[cy0, cx0:cx1] | phi[py0, px0:px1]
    top_i[0] = bool(ch[cy0, cx0] or cv[cy0, cx0] or ph[py0, px0] or pv[py0, px0])
    top_i[-1] = bool(ch[cy0, cx1 - 2] or cv[cy0, cx1 - 1] or ph[py0, px1 - 2] or pv[py0, px1 - 1])

    bot_i = cv[cy1 - 2, cx0:cx1] | pv[py1 - 2, px0:px1] | chi[cy1 - 1, cx0:cx1] | phi[py1 - 1, px0:px1]
    bot_i[0] = bool(ch[cy1 - 1, cx0] or cv[cy1 - 2, cx0] or ph[py1 - 1, px0] or pv[py1 - 2, px0])
    bot_i[-1] = bool(ch[cy1 - 1, cx1 - 2] or cv[cy1 - 2, cx1 - 1] or ph[py1 - 1, px1 - 2] or pv[py1 - 2, px1 - 1])

    left_i = ch[cy0:cy1, cx0] | ph[py0:py1, px0] | cvi[cy0:cy1, cx0] | pvi[py0:py1, px0]
    left_i[0] = top_i[0]
    left_i[-1] = bot_i[0]

    right_i = ch[cy0:cy1, cx1 - 2] | ph[py0:py1, px1 - 2] | cvi[cy0:cy1, cx1 - 1] | pvi[py0:py1, px1 - 1]
    right_i[0] = top_i[-1]
    right_i[-1] = bot_i[-1]
    return top_i, bot_i, left_i, right_i


def _crop_informative_pair_fast(state, cy0, cy1, cx0, cx1, py0, py1, px0, px1):
    """Compute informative = informative_pixels(c, valid_sub) | informative_pixels(p, valid_sub)."""
    _ensure_prepared_caches(state)
    csl = np.s_[cy0:cy1, cx0:cx1]
    psl = np.s_[py0:py1, px0:px1]
    h, w = cy1 - cy0, cx1 - cx0
    if not state.allowed_all or h < 2 or w < 2:
        c, p = state.cur[csl], state.prev[psl]
        valid_sub = state.allowed[csl] & state.allowed[psl]
        if not bool(np.all(valid_sub)) or h < 2 or w < 2:
            info = informative_pixels(c, valid_sub) | informative_pixels(p, valid_sub)
            return info, valid_sub
    _add_ops(h * w * 10)
    info = state.cur_info[csl] | state.prev_info[psl]
    top_i, bot_i, left_i, right_i = _border_pair_1d(
        state, cy0, cy1, cx0, cx1, py0, py1, px0, px1
    )
    info[0, :] = top_i
    info[-1, :] = bot_i
    info[:, 0] = left_i
    info[:, -1] = right_i
    return info, None


def _interior_is_all_informative(state, cy0, cy1, cx0, cx1, py0, py1, px0, px1):
    """Return True if every pixel in [1:-1, 1:-1] of the overlap is informative."""
    if state.cur_inner_all_info or state.prev_inner_all_info:
        return True
    if state.mostly_uninfo:
        return False
    if state.uninfo_cur_int is None:
        state.uninfo_cur_int = _rect_integral_2d(~state.cur_info)
    if _rect_sum_2d(state.uninfo_cur_int, cy0 + 1, cy1 - 1, cx0 + 1, cx1 - 1) == 0:
        return True
    if state.uninfo_prev_int is None:
        state.uninfo_prev_int = _rect_integral_2d(~state.prev_info)
    return _rect_sum_2d(state.uninfo_prev_int, py0 + 1, py1 - 1, px0 + 1, px1 - 1) == 0


def _fast_candidate_residual(state, dx, dy, _dense_budget, _best_bound=None):
    """Compute (dx, dy, raw_sad, N_dyn, is_lower_bound) and record exact OPS/OPS_DETAIL."""
    _record("verified_candidates")
    cy0, cy1, cx0, cx1 = _overlap(state.band, dx, dy)
    bh, bw = state.band[1] - state.band[0], state.band[3] - state.band[2]
    if cy1 - cy0 < min_ov_y(bh) or cx1 - cx0 < min_ov_y(bw):
        return None
    _ensure_prepared_caches(state)
    py0, py1, px0, px1 = cy0 - dy, cy1 - dy, cx0 - dx, cx1 - dx
    h, w = cy1 - cy0, cx1 - cx0
    c_size = h * w
    c = state.cur[cy0:cy1, cx0:cx1]
    p = state.prev[py0:py1, px0:px1]

    top, left = cy0 % TILE, cx0 % TILE
    gh, gw = (top + h + TILE - 1) // TILE, (left + w + TILE - 1) // TILE
    padded_size = gh * TILE * gw * TILE
    H, W = state.cur.shape
    weights_size = ((H + TILE - 1) // TILE) * ((W + TILE - 1) // TILE)
    tail_ops = 2 * padded_size + c_size * 4 + weights_size * 3

    if state.allowed_all and h > 2 and w > 2 and _interior_is_all_informative(
        state, cy0, cy1, cx0, cx1, py0, py1, px0, px1
    ):
        _add_ops(c_size * 13)
        ch, cv, ph, pv = state.cur_h, state.cur_v, state.prev_h, state.prev_v
        c00 = bool(ch[cy0, cx0] or cv[cy0, cx0] or ph[py0, px0] or pv[py0, px0])
        c01 = bool(ch[cy0, cx1 - 2] or cv[cy0, cx1 - 1] or ph[py0, px1 - 2] or pv[py0, px1 - 1])
        c10 = bool(ch[cy1 - 1, cx0] or cv[cy1 - 2, cx0] or ph[py1 - 1, px0] or pv[py1 - 2, px0])
        c11 = bool(ch[cy1 - 1, cx1 - 2] or cv[cy1 - 2, cx1 - 1] or ph[py1 - 1, px1 - 2] or pv[py1 - 2, px1 - 1])
        if (
            c00 and c01 and c10 and c11
            and (state.cur_top_ok[cy0] or state.prev_top_ok[py0])
            and (state.cur_bot_ok[cy1 - 2] or state.prev_bot_ok[py1 - 2])
            and (state.cur_left_ok[cx0] or state.prev_left_ok[px0])
            and (state.cur_right_ok[cx1 - 2] or state.prev_right_ok[px1 - 2])
        ):
            border_false = 0
            top_i = bot_i = l_mid = r_mid = None
            f_top = f_bot = f_left = f_right = 0
        else:
            top_i, bot_i, left_i, right_i = _border_pair_1d(
                state, cy0, cy1, cx0, cx1, py0, py1, px0, px1
            )
            f_top = w - int(np.count_nonzero(top_i))
            f_bot = w - int(np.count_nonzero(bot_i))
            l_mid = left_i[1:-1]
            r_mid = right_i[1:-1]
            f_left = (h - 2) - int(np.count_nonzero(l_mid))
            f_right = (h - 2) - int(np.count_nonzero(r_mid))
            border_false = f_top + f_bot + f_left + f_right
        n = c_size - border_false
        if n == 0:
            return None
        if _dense_budget is not None and _dense_budget[0] > 0 and n == c_size:
            _dense_budget[0] -= 1
            raw = _joint_sad(c, p, None)
            _add_ops(tail_ops)
            return (int(dx), int(dy), raw, n, False)

        b1, b2 = _get_u8_buffers((h, w))
        r_done = 0
        if _best_bound is not None and h >= 14:
            b_raw, b_n = _best_bound
            cutoff = ((b_raw + ALIAS_MIN_LUMA * b_n) * n) // b_n
            est_rows = max(8, (cutoff // (56 * max(1, w - 2))) + 4)
            if est_rows < h - 2:
                r1 = 1 + est_rows
                c_top, p_top = c[:r1], p[:r1]
                b1_top, b2_top = b1[:r1], b2[:r1]
                _U_MAX(c_top, p_top, out=b1_top)
                _U_MIN(c_top, p_top, out=b2_top)
                _U_SUB(b1_top, b2_top, out=b1_top)
                strip_sad = int(b1_top[1:r1, 1:-1].sum(dtype=np.uint32))
                if strip_sad > cutoff:
                    _add_ops(n * 2 + tail_ops)
                    _record("packed_informative_comparisons", n)
                    return (int(dx), int(dy), strip_sad, n, True)
                r_done = r1

        if r_done < h:
            c_rem, p_rem = c[r_done:], p[r_done:]
            b1_rem, b2_rem = b1[r_done:], b2[r_done:]
            _U_MAX(c_rem, p_rem, out=b1_rem)
            _U_MIN(c_rem, p_rem, out=b2_rem)
            _U_SUB(b1_rem, b2_rem, out=b1_rem)

        total_sad = int(b1.sum(axis=1, dtype=np.uint32).sum(dtype=np.int64))
        if border_false > 0:
            uninfo_sad = 0
            if f_top > 0:
                uninfo_sad += int(b1[0, ~top_i].sum(dtype=np.int64))
            if f_bot > 0:
                uninfo_sad += int(b1[-1, ~bot_i].sum(dtype=np.int64))
            if f_left > 0:
                uninfo_sad += int(b1[1:-1, 0][~l_mid].sum(dtype=np.int64))
            if f_right > 0:
                uninfo_sad += int(b1[1:-1, -1][~r_mid].sum(dtype=np.int64))
            raw = total_sad - uninfo_sad
        else:
            raw = total_sad
        _add_ops(n * 2 + tail_ops)
        _record("packed_informative_comparisons", n)
        return (int(dx), int(dy), raw, n, False)

    informative, _ = _crop_informative_pair_fast(state, cy0, cy1, cx0, cx1, py0, py1, px0, px1)
    n = int(np.count_nonzero(informative))
    _add_ops(c_size * 3)
    if n == 0:
        return None
    if _dense_budget is not None and _dense_budget[0] > 0 and n == c_size:
        _dense_budget[0] -= 1
        raw = _joint_sad(c, p, None)
    else:
        if _best_bound is not None and h >= 20:
            b_raw, b_n = _best_bound
            cutoff = ((b_raw + ALIAS_MIN_LUMA * b_n) * n) // b_n
            rm0 = max(1, (h // 2) - 8)
            rm1 = min(h - 1, rm0 + 16)
            c_s = c[rm0:rm1]
            p_s = p[rm0:rm1]
            i_s = informative[rm0:rm1]
            s1, s2 = _get_u8_buffers(c_s.shape)
            _U_MAX(c_s, p_s, out=s1)
            _U_MIN(c_s, p_s, out=s2)
            _U_SUB(s1, s2, out=s1)
            _U_MUL(s1, i_s.view(np.uint8), out=s1)
            strip_sad = int(s1.sum(dtype=np.uint32))
            if strip_sad > cutoff:
                _add_ops(n * 2 + tail_ops)
                _record("packed_informative_comparisons", n)
                return (int(dx), int(dy), strip_sad, n, True)
        b1, b2 = _get_u8_buffers((h, w))
        _U_MAX(c, p, out=b1)
        _U_MIN(c, p, out=b2)
        _U_SUB(b1, b2, out=b1)
        if n < c_size:
            _U_MUL(b1, informative.view(np.uint8), out=b1)
        raw = int(b1.sum(axis=1, dtype=np.uint32).sum(dtype=np.int64))
        _add_ops(n * 2)
        _record("packed_informative_comparisons", n)
    _add_ops(tail_ops)
    return (int(dx), int(dy), raw, n, False)


def _exact_raw_sad_only(state, dx, dy):
    """Compute exact (raw_sad, N_dyn) without changing OPS or OPS_DETAIL."""
    saved_ops = _ops.get()
    saved_diag = dict(_diagnostics.get() or {})
    try:
        res = _fast_candidate_residual(state, dx, dy, None, _best_bound=None)
        if res is None:
            return None
        return res[2], res[3]
    finally:
        _ops.set(saved_ops)
        _diagnostics.set(saved_diag)


def _verify(state, dx, dy, keep_pixels=False, _dense_budget=None):
    """Exact informative residual and capped changed support for one proposal.

    Dense SAD is used only if every pixel in the eligible rectangle is
    informative. Sparse comparisons and all proposals beyond the dense budget
    use a packed informative vector, with equally exact integer arithmetic.
    The budget changes the reduction kernel, never the evidence or hypotheses.
    """
    _record("verified_candidates")
    cy0, cy1, cx0, cx1 = _overlap(state.band, dx, dy)
    bh, bw = state.band[1] - state.band[0], state.band[3] - state.band[2]
    if cy1 - cy0 < min_ov_y(bh) or cx1 - cx0 < min_ov_y(bw):
        return None
    py0, py1, px0, px1 = cy0 - dy, cy1 - dy, cx0 - dx, cx1 - dx
    csl = np.s_[cy0:cy1, cx0:cx1]
    psl = np.s_[py0:py1, px0:px1]
    c, p = state.cur[csl], state.prev[psl]
    informative, valid_sub = _crop_informative_pair_fast(state, cy0, cy1, cx0, cx1, py0, py1, px0, px1)
    n = int(np.count_nonzero(informative))
    _add_ops(c.size * 3)
    if n == 0:
        return None
    if _dense_budget is not None and _dense_budget[0] > 0 and n == c.size:
        _dense_budget[0] -= 1
        raw = _joint_sad(c, p, None)
    else:
        b1, b2 = _get_u8_buffers(c.shape)
        _U_MAX(c, p, out=b1)
        _U_MIN(c, p, out=b2)
        _U_SUB(b1, b2, out=b1)
        if n < c.size:
            _U_MUL(b1, informative.view(np.uint8), out=b1)
        raw = int(b1.sum(axis=1, dtype=np.uint32).sum(dtype=np.int64))
        _add_ops(n * 2)
        _record("packed_informative_comparisons", n)
    if valid_sub is None:
        mismatch = c != p
    else:
        mismatch = (c != p) & valid_sub
    if state.changed is not None:
        changed_sub = state.changed[csl] & informative
    else:
        changed_sub = (c != state.prev[csl]) & informative
    weights = np.minimum(_tile_sums_crop(changed_sub, cy0, cx0, state.cur.shape),
                         TILE_WEIGHT_CAP)
    matched = (_tile_sums_crop(mismatch, cy0, cx0, state.cur.shape) == 0) & (weights > 0)
    _add_ops(c.size * 4 + weights.size * 3)
    valid = changed_info = None
    if keep_pixels:
        valid = np.zeros(state.cur.shape, dtype=bool)
        valid[csl] = True if valid_sub is None else valid_sub
        changed_info = np.zeros(state.cur.shape, dtype=bool)
        changed_info[csl] = changed_sub
    return Evidence(int(dx), int(dy), raw, n, (raw + n - 1) // n,
                    int(weights.sum()), int(weights[matched].sum()), weights,
                    valid, matched, changed_info)


def _evidence_compare(a, b):
    cross = a.raw_sad * b.N_dyn - b.raw_sad * a.N_dyn
    if cross:
        return -1 if cross < 0 else 1
    ak = (abs(a.dx) + abs(a.dy), a.dx, a.dy)
    bk = (abs(b.dx) + abs(b.dy), b.dx, b.dy)
    return (ak > bk) - (ak < bk)


def _tuple_evidence_compare(a, b):
    cross = a[2] * b[3] - b[2] * a[3]
    if cross:
        return -1 if cross < 0 else 1
    ak = (abs(a[0]) + abs(a[1]), a[0], a[1])
    bk = (abs(b[0]) + abs(b[1]), b[0], b[1])
    return (ak > bk) - (ak < bk)


def alias_is_unique(best, others):
    """Exact rational comparison; unique exact zero bypasses positive margin."""
    for other in others:
        if max(abs(other.dx - best.dx), abs(other.dy - best.dy)) < ALIAS_DISTANCE:
            continue
        improvement = other.raw_sad * best.N_dyn - best.raw_sad * other.N_dyn
        if improvement == 0:
            return False
        if best.raw_sad != 0 and improvement < ALIAS_MIN_LUMA * best.N_dyn * other.N_dyn:
            return False
    return True


def mode_veto(primary_support, runner_support, changed_support):
    """Secondary modes need not be non-adjacent; every distinct vector counts."""
    return (runner_support * PPT >= SECONDARY_VETO_PPT * changed_support
            or primary_support < PRIMARY_RUNNER_RATIO * runner_support)


def _probe_tiles_strided(cur, prev, p_pos, iy0, iy1, ix0, ix1, dy, dx, ty_start, ty_end, tx_start, tx_end):
    """Return boolean mask (ty_end - ty_start, tx_end - tx_start) of active tiles passing a zero-copy strided probe."""
    nty = ty_end - ty_start
    ntx = tx_end - tx_start
    cand = p_pos[ty_start:ty_end, tx_start:tx_end].copy()
    r0_in = 1 if (ty_start * TILE < iy0) else 0
    c0_in = 1 if (tx_start * TILE < ix0) else 0
    r1_in = (nty - 1) if (ty_end * TILE > iy1) else nty
    c1_in = (ntx - 1) if (tx_end * TILE > ix1) else ntx
    if r0_in < r1_in and c0_in < c1_in:
        y_a = (ty_start + r0_in) * TILE
        y_b = (ty_start + r1_in) * TILE
        x_a = (tx_start + c0_in) * TILE
        x_b = (tx_start + c1_in) * TILE
        eq_in = (
            (cur[y_a:y_b:TILE, x_a:x_b:TILE] == prev[y_a - dy:y_b - dy:TILE, x_a - dx:x_b - dx:TILE])
            & (cur[y_a + 15:y_b + 15:TILE, x_a + 15:x_b + 15:TILE] == prev[y_a + 15 - dy:y_b + 15 - dy:TILE, x_a + 15 - dx:x_b + 15 - dx:TILE])
        )
        cand[r0_in:r1_in, c0_in:c1_in] &= eq_in
    if r0_in == 1 and c0_in < c1_in:
        x_a = (tx_start + c0_in) * TILE
        x_b = (tx_start + c1_in) * TILE
        ty_in = min(iy1 - 1, (ty_start + 1) * TILE - 1)
        cand[0, c0_in:c1_in] &= (
            (cur[ty_in, x_a:x_b:TILE] == prev[ty_in - dy, x_a - dx:x_b - dx:TILE])
            & (cur[ty_in, x_a + 15:x_b + 15:TILE] == prev[ty_in - dy, x_a + 15 - dx:x_b + 15 - dx:TILE])
        )
    if r1_in < nty and c0_in < c1_in:
        x_a = (tx_start + c0_in) * TILE
        x_b = (tx_start + c1_in) * TILE
        ry = max(iy0, (ty_end - 1) * TILE)
        cand[-1, c0_in:c1_in] &= (
            (cur[ry, x_a:x_b:TILE] == prev[ry - dy, x_a - dx:x_b - dx:TILE])
            & (cur[ry, x_a + 15:x_b + 15:TILE] == prev[ry - dy, x_a + 15 - dx:x_b + 15 - dx:TILE])
        )
    if c0_in == 1 and r0_in < r1_in:
        y_a = (ty_start + r0_in) * TILE
        y_b = (ty_start + r1_in) * TILE
        tx_in = min(ix1 - 1, (tx_start + 1) * TILE - 1)
        cand[r0_in:r1_in, 0] &= (
            (cur[y_a:y_b:TILE, tx_in] == prev[y_a - dy:y_b - dy:TILE, tx_in - dx])
            & (cur[y_a + 15:y_b + 15:TILE, tx_in] == prev[y_a + 15 - dy:y_b + 15 - dy:TILE, tx_in - dx])
        )
    if c1_in < ntx and r0_in < r1_in:
        y_a = (ty_start + r0_in) * TILE
        y_b = (ty_start + r1_in) * TILE
        rx = max(ix0, (tx_end - 1) * TILE)
        cand[r0_in:r1_in, -1] &= (
            (cur[y_a:y_b:TILE, rx] == prev[y_a - dy:y_b - dy:TILE, rx - dx])
            & (cur[y_a + 15:y_b + 15:TILE, rx] == prev[y_a + 15 - dy:y_b + 15 - dy:TILE, rx - dx])
        )
    if r0_in == 1 and c0_in == 1:
        ty_in = min(iy1 - 1, (ty_start + 1) * TILE - 1)
        tx_in = min(ix1 - 1, (tx_start + 1) * TILE - 1)
        cand[0, 0] &= bool(cur[ty_in, tx_in] == prev[ty_in - dy, tx_in - dx])
    if r0_in == 1 and c1_in < ntx:
        ty_in = min(iy1 - 1, (ty_start + 1) * TILE - 1)
        rx = max(ix0, (tx_end - 1) * TILE)
        cand[0, -1] &= bool(cur[ty_in, rx] == prev[ty_in - dy, rx - dx])
    if r1_in < nty and c0_in == 1:
        ry = max(iy0, (ty_end - 1) * TILE)
        tx_in = min(ix1 - 1, (tx_start + 1) * TILE - 1)
        cand[-1, 0] &= bool(cur[ry, tx_in] == prev[ry - dy, tx_in - dx])
    if r1_in < nty and c1_in < ntx:
        ry = max(iy0, (ty_end - 1) * TILE)
        rx = max(ix0, (tx_end - 1) * TILE)
        cand[-1, -1] &= bool(cur[ry, rx] == prev[ry - dy, rx - dx])
    return cand


def _runner_support(state, primary, runner, _fast_ctx=None):
    """Recount every runner on the primary's common changed information."""
    y0, y1, x0, x1 = _overlap(state.band, runner.dx, runner.dy)
    if _fast_ctx is not None:
        by0, by1, bx0, bx1, weights_size, H, W, p_pos, p_ty0, p_ty1, p_tx0, p_tx1 = _fast_ctx
        h, w = y1 - y0, x1 - x0
        top, left = y0 % TILE, x0 % TILE
        gh, gw = (top + h + TILE - 1) // TILE, (left + w + TILE - 1) // TILE
        padded_size = gh * TILE * gw * TILE
        iy0, iy1 = max(y0, by0), min(y1, by1)
        ix0, ix1 = max(x0, bx0), min(x1, bx1)
        ih, iw = max(0, iy1 - iy0), max(0, ix1 - ix0)
        common_nz = ih * iw
        _add_ops(2 * padded_size + (h * w) * 4 + weights_size * 2)
        _record("runner_candidates")
        _record("runner_comparisons", common_nz)
        if common_nz == 0:
            return 0
        ty_start = max(p_ty0, iy0 // TILE)
        ty_end = min(p_ty1, (iy1 + TILE - 1) // TILE)
        tx_start = max(p_tx0, ix0 // TILE)
        tx_end = min(p_tx1, (ix1 + TILE - 1) // TILE)
        if ty_start >= ty_end or tx_start >= tx_end:
            return 0
        dy, dx = runner.dy, runner.dx
        cand_mask = _probe_tiles_strided(
            state.cur, state.prev, p_pos,
            iy0, iy1, ix0, ix1, dy, dx,
            ty_start, ty_end, tx_start, tx_end,
        )
        if int(np.count_nonzero(cand_mask)) == 0:
            return 0
        total_support = 0
        for r_idx, c_idx in zip(*np.nonzero(cand_mask)):
            ty = int(ty_start + r_idx)
            tx = int(tx_start + c_idx)
            ry0, ry1 = max(ty * TILE, iy0), min((ty + 1) * TILE, iy1)
            rx0, rx1 = max(tx * TILE, ix0), min((tx + 1) * TILE, ix1)
            if np.array_equal(state.cur[ry0:ry1, rx0:rx1], state.prev[ry0 - dy:ry1 - dy, rx0 - dx:rx1 - dx]):
                if (ry0 == ty * TILE and ry1 == min((ty + 1) * TILE, H)
                        and rx0 == tx * TILE and rx1 == min((tx + 1) * TILE, W)
                        and ry0 >= by0 and ry1 <= by1 and rx0 >= bx0 and rx1 <= bx1):
                    total_support += int(primary.weights[ty, tx])
                else:
                    w_tile = int(np.count_nonzero(primary.changed_info[ry0:ry1, rx0:rx1]))
                    total_support += min(w_tile, TILE_WEIGHT_CAP)
        return total_support

    csl = np.s_[y0:y1, x0:x1]
    psl = np.s_[y0 - runner.dy:y1 - runner.dy, x0 - runner.dx:x1 - runner.dx]
    common = primary.valid[csl] & state.allowed[psl]
    mismatch = (state.cur[csl] != state.prev[psl]) & common
    match_tiles = _tile_sums_crop(mismatch, y0, x0, state.cur.shape) == 0
    weights = np.minimum(_tile_sums_crop(primary.changed_info[csl] & common,
                                         y0, x0, state.cur.shape), TILE_WEIGHT_CAP)
    _add_ops(common.size * 4 + weights.size * 2)
    _record("runner_candidates")
    _record("runner_comparisons", int(np.count_nonzero(common)))
    return int(weights[match_tiles].sum())


def _sparse_exact_support(state, best, others):
    """Certify sparse exact outlines without inflating changed-support weights.

    Only unchanged informative four-neighbours of changed information count,
    and only inside already exact tiles with positive changed support. These
    additional correspondences cannot inflate P or the runner denominators.
    See the design note for the compatibility extension to spec section 6.1.
    """
    if (best.raw_sad != 0 or best.support_weight != best.changed_weight
            or best.changed_weight == 0 or any(other.raw_sad == 0 for other in others)):
        return False
    y0, y1, x0, x1 = _overlap(state.band, best.dx, best.dy)
    csl = np.s_[y0:y1, x0:x1]
    psl = np.s_[y0 - best.dy:y1 - best.dy, x0 - best.dx:x1 - best.dx]
    c, p = state.cur[csl], state.prev[psl]
    valid, changed = best.valid[csl], best.changed_info[csl]
    _add_ops(c.size * 2)
    _record("sparse_exact_comparisons", int(np.count_nonzero(valid)))
    if np.any((c != p) & valid):
        return False
    informative = informative_pixels(c, valid) | informative_pixels(p, valid)
    adjacent = np.zeros(changed.shape, dtype=bool)
    adjacent[1:] |= changed[:-1]
    adjacent[:-1] |= changed[1:]
    adjacent[:, 1:] |= changed[:, :-1]
    adjacent[:, :-1] |= changed[:, 1:]
    boundary = informative & ~changed & adjacent
    weights = np.minimum(_tile_sums_crop(boundary, y0, x0, state.cur.shape),
                         TILE_WEIGHT_CAP)
    support = int(weights[best.matched].sum(dtype=np.int64))
    _add_ops(c.size * 7 + weights.size * 2)
    _record("sparse_exact_boundary_weight", support)
    return support >= MIN_SUPPORT_WEIGHT


def _fixed_chrome_exact_viewport(state, best):
    """Certify the legacy zero score, after full-band motion acceptance.

    Raw temporal changes independently bound a full-width (or full-height)
    viewport between two unchanged outer strips. Every observable translated
    viewport byte must match, including flat pixels and unmasked clocks.
    Stationary texture remains in all preceding motion and runner decisions.
    """
    if (best.dx == 0) == (best.dy == 0):
        return False
    h, w = state.cur.shape
    changed = (state.prev != state.cur) if state.changed is None else state.changed
    _add_ops(changed.size * 2)
    if best.dx == 0:
        active = np.flatnonzero(changed.any(axis=1))
        if not active.size or int(active[0]) == 0 or int(active[-1]) == h - 1:
            return False
        band = (int(active[0]), int(active[-1]) + 1, 0, w)
    else:
        active = np.flatnonzero(changed.any(axis=0))
        if not active.size or int(active[0]) == 0 or int(active[-1]) == w - 1:
            return False
        band = (0, h, int(active[0]), int(active[-1]) + 1)
    y0, y1, x0, x1 = _overlap(band, best.dx, best.dy)
    if (y1 - y0 < min_ov_y(band[1] - band[0])
            or x1 - x0 < min_ov_y(band[3] - band[2])):
        return False
    c = state.cur[y0:y1, x0:x1]
    p = state.prev[y0 - best.dy:y1 - best.dy, x0 - best.dx:x1 - best.dx]
    _add_ops(c.size)
    _record("viewport_exact_comparisons", c.size)
    return bool(np.array_equal(c, p))


def _seed_best_bound(state, proposals, max_d):
    """Quickly probe proposals on 1 interior row to find an initial tight upper bound (raw_sad, N_dyn)."""
    bh, bw = state.band[1] - state.band[0], state.band[3] - state.band[2]
    min_y, min_x = min_ov_y(bh), min_ov_y(bw)
    best_bound = None
    for dx, dy in proposals:
        if abs(dx) <= max_d and abs(dy) <= max_d:
            cy0, cy1, cx0, cx1 = _overlap(state.band, dx, dy)
            if cy1 - cy0 >= min_y and cx1 - cx0 >= min_x:
                ym = (cy0 + cy1) // 2
                if np.array_equal(state.cur[ym, cx0:cx1], state.prev[ym - dy, cx0 - dx:cx1 - dx]):
                    ex = _exact_raw_sad_only(state, int(dx), int(dy))
                    if ex is not None:
                        best_bound = ex
                        if ex[0] == 0:
                            return best_bound
                        break
    if best_bound is None and (0, 0) in proposals:
        best_bound = _exact_raw_sad_only(state, 0, 0)
    return best_bound


def _decide(state, candidates, max_d):
    _ensure_prepared_caches(state)
    proposals = sorted(set(candidates) | {(0, 0)})
    _record("candidate_hypotheses", len(proposals))
    dense_budget = [DENSE_SAD_BUDGET]
    best_bound = _seed_best_bound(state, proposals, max_d) if len(proposals) > DENSE_SAD_BUDGET else None
    raw_items = []
    for dx, dy in proposals:
        if abs(dx) <= max_d and abs(dy) <= max_d:
            item = _fast_candidate_residual(state, int(dx), int(dy), dense_budget, _best_bound=best_bound)
            if item is not None:
                raw_items.append(item)
                if not item[4]:
                    if best_bound is None or item[2] * best_bound[1] < best_bound[0] * item[3]:
                        best_bound = (item[2], item[3])
    if not raw_items:
        return None

    best_exact = None
    for it in raw_items:
        if not it[4]:
            if best_exact is None or _tuple_evidence_compare(it, best_exact) < 0:
                best_exact = it
    best_dx, best_dy, best_raw, best_n, _ = best_exact

    resolved_items = []
    for dx, dy, raw, n, is_lb in raw_items:
        if is_lb and (
            max(abs(dx - best_dx), abs(dy - best_dy)) < ALIAS_DISTANCE
            or raw * best_n <= (best_raw + ALIAS_MIN_LUMA * best_n) * n
        ):
            raw, n = _exact_raw_sad_only(state, dx, dy)
            is_lb = False
        resolved_items.append((dx, dy, raw, n, is_lb))

    best_resolved = resolved_items[0]
    for it in resolved_items[1:]:
        if _tuple_evidence_compare(it, best_resolved) < 0:
            best_resolved = it
    best_dx, best_dy, best_raw, best_n, _ = best_resolved
    if (best_dx, best_dy) == (0, 0):
        return None

    resolved_items.sort(key=cmp_to_key(_tuple_evidence_compare))
    best_stub = Evidence(
        best_dx, best_dy, best_raw, best_n, (best_raw + best_n - 1) // best_n,
        0, 0, None, None, None, None
    )
    other_stubs = [
        Evidence(dx, dy, raw, n, (raw + n - 1) // n, 0, 0, None, None, None, None)
        for dx, dy, raw, n, _ in resolved_items[1:]
    ]
    if not alias_is_unique(best_stub, other_stubs):
        return None

    saved_ops = _ops.get()
    saved_diag = dict(_diagnostics.get() or {})
    best_full = _verify(state, best_dx, best_dy, keep_pixels=True)
    verify_ops_delta = _ops.get() - saved_ops
    verify_diag_after = dict(_diagnostics.get() or {})

    _ops.set(saved_ops)
    _diagnostics.set(saved_diag)

    yy, xx = np.nonzero(best_full.matched)
    if (yy.size < MIN_SUPPORT_TILES
            or np.unique(yy).size < MIN_SUPPORT_ROWS or np.unique(xx).size < MIN_SUPPORT_COLS
            or best_full.changed_weight == 0
            or best_full.support_weight * PPT < PRIMARY_MIN_PPT * best_full.changed_weight):
        return None

    _ops.set(saved_ops + verify_ops_delta)
    _diagnostics.set(verify_diag_after)
    best = best_full

    sparse = best.support_weight < MIN_SUPPORT_WEIGHT
    if sparse and not _sparse_exact_support(state, best, other_stubs):
        return None

    fast_ctx = None
    if state.allowed_all:
        by0, by1, bx0, bx1 = _overlap(state.band, best.dx, best.dy)
        H, W = state.cur.shape
        weights_size = ((H + TILE - 1) // TILE) * ((W + TILE - 1) // TILE)
        p_pos = best.weights > 0
        wy, wx = np.nonzero(p_pos)
        if wy.size:
            p_ty0, p_ty1 = int(wy[0]), int(wy[-1]) + 1
            p_tx0, p_tx1 = int(wx.min()), int(wx.max()) + 1
        else:
            p_ty0 = p_ty1 = p_tx0 = p_tx1 = 0
        fast_ctx = (by0, by1, bx0, bx1, weights_size, H, W, p_pos, p_ty0, p_ty1, p_tx0, p_tx1)

    pre_runner_ops = _ops.get()
    pre_runner_diag = dict(_diagnostics.get() or {})
    has_lb = any(it[4] for it in resolved_items[1:])

    for other in other_stubs:
        if (other.dx, other.dy) == (0, 0):
            continue
        support = _runner_support(state, best, other, _fast_ctx=fast_ctx)
        if mode_veto(best.support_weight, support, best.changed_weight):
            if has_lb:
                _ops.set(pre_runner_ops)
                _diagnostics.set(pre_runner_diag)
                exact_others = []
                for dx, dy, raw, n, is_lb in resolved_items[1:]:
                    if is_lb:
                        raw, n = _exact_raw_sad_only(state, dx, dy)
                    exact_others.append(
                        Evidence(dx, dy, raw, n, (raw + n - 1) // n, 0, 0, None, None, None, None)
                    )
                exact_others.sort(key=cmp_to_key(_evidence_compare))
                for ex_other in exact_others:
                    if (ex_other.dx, ex_other.dy) == (0, 0):
                        continue
                    ex_sup = _runner_support(state, best, ex_other, _fast_ctx=fast_ctx)
                    if mode_veto(best.support_weight, ex_sup, best.changed_weight):
                        return None
            return None

    _record("decision_raw_sad", best.raw_sad)
    _record("decision_informative_pixels", best.N_dyn)
    _record("decision_score", best.score)
    if sparse:
        _record("sparse_exact_accepted")
    score = best.score
    if score > 0 and _fixed_chrome_exact_viewport(state, best):
        score = 0
        _record("exact_viewport_score_zero")
    return int(best.dx), int(best.dy), int(score)


def _projection_rank_keys(cur, prev, max_d, minimum):
    """Return (all_keys_ indexed_by_shift_plus_max_shift, max_shift) in int64."""
    length = len(cur)
    limit = min(int(max_d), length - 1)
    max_shift = min(limit, length - int(minimum))
    if max_shift < 0 or length < int(minimum) or length == 0:
        return None, -1
    c32 = np.asarray(cur, dtype=np.int32)
    p32 = np.asarray(prev, dtype=np.int32)
    raw0 = int(_U_ABS(c32 - p32).sum(dtype=np.int64))
    if max_shift == 0:
        _add_ops(length * 2)
        out = np.empty(1, dtype=np.int64)
        out[0] = (raw0 << 28) | 8192
        return out, 0
    c_pad, p_pad, buf, c_win, p_win, ov, pos_tie, neg_tie, all_keys, _, _, ops = _get_stride_workspace(
        length, max_shift
    )
    _add_ops(ops)
    c_pad[:length] = c32
    c_pad[length:] = 0
    p_pad[:length] = p32
    p_pad[length:] = 0
    p_tail = np.cumsum(p32[::-1], dtype=np.int64)[:max_shift]
    c_tail = np.cumsum(c32[::-1], dtype=np.int64)[:max_shift]
    _U_SUB(c_win, p32, out=buf)
    _U_ABS(buf, out=buf)
    pos_raw = buf.sum(axis=1, dtype=np.int32).astype(np.int64) - p_tail
    _U_SUB(c32, p_win, out=buf)
    _U_ABS(buf, out=buf)
    neg_raw = buf.sum(axis=1, dtype=np.int32).astype(np.int64) - c_tail
    pos_scores = (pos_raw * length) // ov
    neg_scores = (neg_raw * length) // ov
    all_keys[max_shift] = (raw0 << 28) | 8192
    all_keys[max_shift + 1:] = (pos_scores << 28) | pos_tie
    all_keys[max_shift - 1::-1] = (neg_scores << 28) | neg_tie
    return all_keys, max_shift


def _projection_rank(cur, prev, max_d, minimum):
    """Rank all 1D integer shifts within max_d meeting minimum overlap using stride-cached windows."""
    all_keys, max_shift = _projection_rank_keys(cur, prev, max_d, minimum)
    if all_keys is None:
        return []
    order = np.argsort(all_keys, kind="mergesort")
    shifts = (order - max_shift).tolist()
    keys_list = all_keys[order].tolist()
    return [(k >> 28, abs(s), s) for k, s in zip(keys_list, shifts)]


def _coarse_smooth_shifts(cs, ps, max_d, minimum):
    """Return top PROJECTION_COARSE_KEEP coarse shifts at PROJECTION_SMOOTH multiples."""
    length = len(cs)
    limit = min(int(max_d), length - 1)
    max_shift = min(limit, length - int(minimum))
    if max_shift < 0 or length < int(minimum) or length == 0:
        return ()
    cs32 = cs.astype(np.int32, copy=False)
    ps32 = ps.astype(np.int32, copy=False)
    raw0 = int(_U_ABS(cs32 - ps32).sum(dtype=np.int64))
    n_coarse = max_shift // PROJECTION_SMOOTH
    if n_coarse == 0:
        ops = 2 * (length + 2 * max_shift * length - max_shift * (max_shift + 1))
        _add_ops(ops)
        return (0,)
    c_pad, p_pad, buf, c_win, p_win, ov, tail_idx, pos_tie, neg_tie, all_keys, _, _, ops = _get_coarse_stride_workspace(
        length, max_shift, PROJECTION_SMOOTH
    )
    _add_ops(ops)
    c_pad[:length] = cs32
    c_pad[length:] = 0
    p_pad[:length] = ps32
    p_pad[length:] = 0
    p_tail = np.cumsum(ps32[::-1], dtype=np.int64)[tail_idx]
    c_tail = np.cumsum(cs32[::-1], dtype=np.int64)[tail_idx]
    _U_SUB(c_win, ps32, out=buf)
    _U_ABS(buf, out=buf)
    pos_raw = buf.sum(axis=1, dtype=np.int64) - p_tail
    _U_SUB(cs32, p_win, out=buf)
    _U_ABS(buf, out=buf)
    neg_raw = buf.sum(axis=1, dtype=np.int64) - c_tail
    pos_scores = (pos_raw * length) // ov
    neg_scores = (neg_raw * length) // ov
    all_keys[0] = (raw0 << 28) | 8192
    all_keys[1 : 1 + n_coarse] = (pos_scores << 28) | pos_tie
    all_keys[1 + n_coarse :] = (neg_scores << 28) | neg_tie
    k = min(PROJECTION_COARSE_KEEP, all_keys.size)
    if k < all_keys.size:
        top_idx = np.argpartition(all_keys, k - 1)[:k]
    else:
        top_idx = np.arange(all_keys.size)
    return ((all_keys[top_idx] & 16383) - 8192).tolist()


def retained_axis_candidates(cur, prev, max_d, minimum, keep=PROJECTION_RAW_KEEP):
    """Keep raw all-integer-offset winners, independent of smoothed coarse bins.

    No padding, FFT, interpolation or subpixel estimation is used.  The raw
    route preserves a -4 micro-scroll and a -960 shift with 120 observable rows.
    """
    all_keys, max_shift = _projection_rank_keys(cur, prev, max_d, minimum)
    if all_keys is None:
        return (0,)
    n_total = all_keys.size
    k = min(int(keep), n_total)
    if k <= 0:
        selected = {0}
    elif k < n_total:
        top_idx = np.argpartition(all_keys, k - 1)[:k]
        selected = set((top_idx - max_shift).tolist())
        selected.add(0)
    else:
        selected = set(range(-max_shift, max_shift + 1))
        selected.add(0)
    if len(cur) >= PROJECTION_SMOOTH and len(prev) >= PROJECTION_SMOOTH:
        _add_ops((len(cur) - PROJECTION_SMOOTH + 1) * PROJECTION_SMOOTH * 2)
        cs = np.convolve(cur, _SMOOTH_KERNEL, mode="valid")
        ps = np.convolve(prev, _SMOOTH_KERNEL, mode="valid")
        smooth_min = max(1, minimum - PROJECTION_SMOOTH + 1)
        coarse_shifts = _coarse_smooth_shifts(cs, ps, max_d, smooth_min)
        if coarse_shifts and n_total > 0:
            for c_shift in coarse_shifts:
                lo_s = max(-max_shift, c_shift - (PROJECTION_SMOOTH - 1))
                hi_s = min(max_shift, c_shift + (PROJECTION_SMOOTH - 1))
                if lo_s <= hi_s:
                    w_slice = all_keys[lo_s + max_shift : hi_s + max_shift + 1]
                    best_local = lo_s + int(np.argmin(w_slice))
                    selected.add(best_local)
    return tuple(sorted(selected))


def _projection_pairs(prev, cur, max_d, keep):
    h, w = prev.shape
    if h < MIN_OVERLAP or w < MIN_OVERLAP:
        return set()
    _add_ops(h * w * 2)
    cr = cur.sum(axis=1, dtype=np.uint32).astype(np.int64)
    pr = prev.sum(axis=1, dtype=np.uint32).astype(np.int64)
    dys = retained_axis_candidates(cr, pr, max_d, min_ov_y(h), keep)
    c_blk = _build_col_block_integral(cur)
    p_blk = _build_col_block_integral(prev)
    pairs = set()
    for dy in dys:
        lo, hi = max(0, dy), min(h, h + dy)
        if hi - lo < min_ov_y(h):
            continue
        _add_ops((hi - lo) * w * 2)
        cc = _query_col_block_integral(c_blk, lo, hi)
        pc = _query_col_block_integral(p_blk, lo - dy, hi - dy)
        for dx in retained_axis_candidates(cc, pc, max_d, min_ov_y(w), min(keep, PROJECTION_AXIS_KEEP)):
            pairs.add((dx, dy))
    return pairs


def dynamic_band_candidates(state, max_d):
    y0, y1, x0, x1 = state.band
    p, c = state.prev[y0:y1, x0:x1], state.cur[y0:y1, x0:x1]
    pairs = _projection_pairs(p, c, max_d, PROJECTION_RAW_KEEP)
    _add_ops(state.cur.size * 2)
    base_changed = (state.prev != state.cur) if state.changed is None else state.changed
    changed = base_changed if state.allowed_all else (base_changed & state.allowed)
    row_active = changed.any(axis=1)
    edges = np.diff(np.pad(row_active.astype(np.int8), (1, 1)))
    runs = []
    for ya, yb in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
        cols = np.flatnonzero(changed[ya:yb].any(axis=0))
        if cols.size:
            xa, xb = int(cols[0]), int(cols[-1]) + 1
            runs.append((-int(np.count_nonzero(changed[ya:yb, xa:xb])), int(ya), int(yb), xa, xb))
    for _, ya, yb, xa, xb in sorted(runs)[:PROJECTION_REGION_LIMIT]:
        if (ya, yb, xa, xb) != state.band:
            pairs.update(_projection_pairs(
                state.prev[ya:yb, xa:xb], state.cur[ya:yb, xa:xb],
                max_d, PROJECTION_REGION_KEEP,
            ))
    return pairs | {(0, 0)}


@_per_call
def estimate_global_motion(prev_luma, cur_luma, max_d):
    """Return integer (dx, dy, score) or abstain.

    Identical textured inputs retain the production (0, 0, 0) special case;
    two spatially flat inputs abstain, including different constant lumas.
    Score is the ceiling informative mean, except for the independently exact
    fixed-chrome viewport compatibility case documented in the design note.
    """
    prev, cur, max_d = _inputs(prev_luma, cur_luma, max_d)
    _add_ops(prev.size * 5)
    if int(prev.min()) == int(prev.max()) and int(cur.min()) == int(cur.max()):
        return None
    if np.array_equal(prev, cur):
        return (0, 0, 0)
    state = _prepare(prev, cur)
    if state is None:
        return None
    return _decide(state, dynamic_band_candidates(state, max_d), max_d)


@_per_call
def consensus_tiles(prev_luma, cur_luma, s, dx, dy):
    """Return a boolean (gh, gw) numpy array of tiles whose whole destination tile

    differs in place and equals the in-bounds translated previous pixels.
    """
    if not all(isinstance(v, (int, np.integer)) and not isinstance(v, (bool, np.bool_))
               for v in (dx, dy)):
        raise TypeError("displacements must be integers")
    if not isinstance(s, (int, np.integer)) or isinstance(s, (bool, np.bool_)) or s <= 0:
        raise ValueError("tile side must be a positive integer")
    dx, dy, s = int(dx), int(dy), int(s)
    prev, cur, _ = _inputs(prev_luma, cur_luma, max(abs(dx), abs(dy)))

    H, W = cur.shape
    _add_ops(H * W * 2)

    gh = (H + s - 1) // s
    gw = (W + s - 1) // s

    cy0_v = max(0, dy)
    cy1_v = min(H, H + dy)
    cx0_v = max(0, dx)
    cx1_v = min(W, W + dx)

    if cy0_v < cy1_v and cx0_v < cx1_v:
        _add_ops((cy1_v - cy0_v) * (cx1_v - cx0_v))
    _add_ops(gh * s * gw * s)
    _add_ops(H * W)

    out = np.zeros((gh, gw), dtype=bool)
    if cy0_v >= cy1_v or cx0_v >= cx1_v:
        return out

    ty0 = (cy0_v + s - 1) // s
    full_gh = H // s
    full_gw = W // s
    ty1_full = min(full_gh, cy1_v // s)
    tx0 = (cx0_v + s - 1) // s
    tx1_full = min(full_gw, cx1_v // s)

    if ty0 < ty1_full and tx0 < tx1_full:
        y_a, y_b = ty0 * s, ty1_full * s
        x_a, x_b = tx0 * s, tx1_full * s
        nty = ty1_full - ty0
        ntx = tx1_full - tx0
        c_sub = cur[y_a:y_b, x_a:x_b]
        p_shift = prev[y_a - dy:y_b - dy, x_a - dx:x_b - dx]
        p_same = prev[y_a:y_b, x_a:x_b]
        if s == 16:
            x1 = _U_XOR(c_sub, p_shift, order="C").view(np.uint64).reshape(nty, 16, ntx, 2)
            no_mis = _U_OR(x1[..., 0], x1[..., 1]).max(axis=1) == 0
            if bool(np.any(no_mis)):
                x2 = _U_XOR(c_sub, p_same, order="C").view(np.uint64).reshape(nty, 16, ntx, 2)
                ch = _U_OR(x2[..., 0], x2[..., 1]).max(axis=1) != 0
                out[ty0:ty1_full, tx0:tx1_full] = no_mis & ch
        elif s % 8 == 0:
            words = s // 8
            x1 = _U_XOR(c_sub, p_shift, order="C").view(np.uint64).reshape(nty, s, ntx, words)
            no_mis = x1.max(axis=3).max(axis=1) == 0
            if bool(np.any(no_mis)):
                x2 = _U_XOR(c_sub, p_same, order="C").view(np.uint64).reshape(nty, s, ntx, words)
                ch = x2.max(axis=3).max(axis=1) != 0
                out[ty0:ty1_full, tx0:tx1_full] = no_mis & ch
        else:
            no_mis = ~(c_sub != p_shift).reshape(nty, s, ntx, s).any(axis=3).any(axis=1)
            if bool(np.any(no_mis)):
                ch = (c_sub != p_same).reshape(nty, s, ntx, s).any(axis=3).any(axis=1)
                out[ty0:ty1_full, tx0:tx1_full] = no_mis & ch

    has_partial_row = (H % s != 0) and ((gh - 1) * s >= cy0_v) and (H <= cy1_v)
    has_partial_col = (W % s != 0) and ((gw - 1) * s >= cx0_v) and (W <= cx1_v)

    if has_partial_row and tx0 < tx1_full:
        ty = gh - 1
        y_a, y_b = ty * s, H
        x_a, x_b = tx0 * s, tx1_full * s
        ntx = tx1_full - tx0
        c_sub = cur[y_a:y_b, x_a:x_b]
        p_shift = prev[y_a - dy:y_b - dy, x_a - dx:x_b - dx]
        p_same = prev[y_a:y_b, x_a:x_b]
        no_mis = ~(c_sub != p_shift).reshape(y_b - y_a, ntx, s).any(axis=2).any(axis=0)
        ch = (c_sub != p_same).reshape(y_b - y_a, ntx, s).any(axis=2).any(axis=0)
        out[ty, tx0:tx1_full] = no_mis & ch

    if has_partial_col and ty0 < ty1_full:
        tx = gw - 1
        x_a, x_b = tx * s, W
        y_a, y_b = ty0 * s, ty1_full * s
        nty = ty1_full - ty0
        c_sub = cur[y_a:y_b, x_a:x_b]
        p_shift = prev[y_a - dy:y_b - dy, x_a - dx:x_b - dx]
        p_same = prev[y_a:y_b, x_a:x_b]
        no_mis = ~(c_sub != p_shift).reshape(nty, s, x_b - x_a).any(axis=2).any(axis=1)
        ch = (c_sub != p_same).reshape(nty, s, x_b - x_a).any(axis=2).any(axis=1)
        out[ty0:ty1_full, tx] = no_mis & ch

    if has_partial_row and has_partial_col:
        ty, tx = gh - 1, gw - 1
        y_a, y_b = ty * s, H
        x_a, x_b = tx * s, W
        c_sub = cur[y_a:y_b, x_a:x_b]
        p_shift = prev[y_a - dy:y_b - dy, x_a - dx:x_b - dx]
        p_same = prev[y_a:y_b, x_a:x_b]
        out[ty, tx] = bool(np.array_equal(c_sub, p_shift) and not np.array_equal(c_sub, p_same))

    return out
