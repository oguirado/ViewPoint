"""Deterministic v2.1 integer global motion and whole-real-tile consensus.

See docs/spec/fx2_f5_gm_v2_note.md for the evidence contract and operation
accounting. All proposals share one evidence band, including stationary texture.
OPS is context-local work accounting, not a latency or hardware instruction count.
OPS_DETAIL separates bounded dense SAD work from the expanded informative verifier.
"""

from contextvars import ContextVar
from dataclasses import dataclass
from functools import cmp_to_key, wraps
import numpy as np

_ops = ContextVar("bands_ops", default=0)
_diagnostics = ContextVar("bands_diagnostics", default=None)


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
    counters = dict(_diagnostics.get() or {})
    counters[name] = counters.get(name, 0) + int(count)
    _diagnostics.set(counters)


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
    total = 0
    for row in range(0, cur.shape[0], 32):
        c = cur[row:row + 32].astype(np.int64)
        p = prev[row:row + 32].astype(np.int64)
        total += int(np.abs(c - p).sum(dtype=np.int64))
        _add_ops(c.size * 2)
        _record("dense_sad_pixels", c.size)
        if cutoff is not None and total > cutoff:
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


def compact_chrome_mask(prev, cur):
    """Conservative row-span proposal, independent of any winning displacement.

    Only an edge row with a compact changed span, a flat identical remainder,
    and a separate interior broad-change row can have its changed pixels masked.  A forty-pixel
    clock is therefore ineligible at widths 256 and 300.  Unchanged textured
    remainders never qualify; the span is never split to satisfy the width cap.
    """
    h, w = prev.shape
    _add_ops(h * w * 4)
    changed = prev != cur
    active = changed.any(axis=1)
    first = np.argmax(changed, axis=1)
    last = w - np.argmax(changed[:, ::-1], axis=1)
    span = np.where(active, last - first, 0)
    edge = min(CHROME_EDGE_CAP, h // CHROME_EDGE_DIVISOR)
    broad = np.flatnonzero((span > w // CHROME_SPAN_DIVISOR)
                           & (np.arange(h) >= edge)
                           & (np.arange(h) < h - edge))
    mask = np.zeros(prev.shape, dtype=bool)
    if broad.size == 0:
        return mask
    for y in np.flatnonzero(active & (span <= w // CHROME_SPAN_DIVISOR)):
        y = int(y)
        separated = ((y < edge and y + CHROME_SEPARATION < int(broad[0]))
                     or (y >= h - edge and y - CHROME_SEPARATION > int(broad[-1])))
        if not separated:
            continue
        x0, x1 = int(first[y]), int(last[y])
        remainder_p = np.concatenate((prev[y, :x0], prev[y, x1:]))
        remainder_c = np.concatenate((cur[y, :x0], cur[y, x1:]))
        if (remainder_p.size and np.array_equal(remainder_p, remainder_c)
                and int(remainder_p.min()) == int(remainder_p.max())):
            # The whole first-to-last span was used for eligibility.  Mask
            # only actual changes: unchanged textured islands at every size
            # and all their observable internal adjacencies remain evidence.
            mask[y, x0:x1] = changed[y, x0:x1]
    return mask


def informative_pixels(frame, allowed=None):
    """Nonzero four-neighbour variation; endpoints must both be observable.

    This Boolean integer predicate gives flat-flat comparisons zero weight,
    including differently coloured flat interiors.  A textured endpoint
    remains informative when the endpoint in the other frame is flat.
    """
    _add_ops(frame.size * 5)
    if allowed is None:
        allowed = np.ones(frame.shape, dtype=bool)
    info = np.zeros(frame.shape, dtype=bool)
    horizontal = ((frame[:, 1:] != frame[:, :-1])
                  & allowed[:, 1:] & allowed[:, :-1])
    vertical = ((frame[1:] != frame[:-1]) & allowed[1:] & allowed[:-1])
    info[:, 1:] |= horizontal
    info[:, :-1] |= horizontal
    info[1:] |= vertical
    info[:-1] |= vertical
    return info & allowed


@dataclass
class Prepared:
    prev: np.ndarray
    cur: np.ndarray
    allowed: np.ndarray
    prev_info: np.ndarray
    cur_info: np.ndarray
    band: tuple


def _prepare(prev, cur):
    _add_ops(prev.size * 3)
    allowed = ~compact_chrome_mask(prev, cur)
    pi = informative_pixels(prev, allowed)
    ci = informative_pixels(cur, allowed)
    # Stationary texture participates in the bounding rectangle.  Cropping by
    # temporal change alone would silently discard essential counterevidence.
    active = allowed & ((prev != cur) | pi | ci)
    rows, cols = np.flatnonzero(active.any(axis=1)), np.flatnonzero(active.any(axis=0))
    if not rows.size or not cols.size:
        return None
    band = (int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1)
    return Prepared(prev, cur, allowed, pi, ci, band)


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
    padded = np.pad(mask, ((top, gh * TILE - top - h),
                           (left, gw * TILE - left - w)), constant_values=0)
    local = padded.reshape(gh, TILE, gw, TILE).sum(axis=(1, 3), dtype=np.int64)
    result = np.zeros(((shape[0] + TILE - 1) // TILE,
                       (shape[1] + TILE - 1) // TILE), dtype=np.int64)
    result[y0 // TILE:y0 // TILE + gh, x0 // TILE:x0 // TILE + gw] = local
    _add_ops(padded.size)
    return result


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
    csl = np.s_[cy0:cy1, cx0:cx1]
    psl = np.s_[cy0-dy:cy1-dy, cx0-dx:cx1-dx]
    c, p = state.cur[csl], state.prev[psl]
    valid_sub = state.allowed[csl] & state.allowed[psl]
    # Both aligned four-neighbour predicates are recomputed within overlap.
    informative = informative_pixels(c, valid_sub) | informative_pixels(p, valid_sub)
    n = int(np.count_nonzero(informative))
    _add_ops(c.size * 3)
    if n == 0:
        return None
    if _dense_budget is not None and _dense_budget[0] > 0 and n == c.size:
        _dense_budget[0] -= 1
        raw = _joint_sad(c, p, None)
    else:
        difference = c[informative].astype(np.int16) - p[informative].astype(np.int16)
        raw = int(np.abs(difference).sum(dtype=np.int64))
        _add_ops(n * 2)
        _record("packed_informative_comparisons", n)
    mismatch = (c != p) & valid_sub
    changed_sub = (c != state.prev[csl]) & informative
    weights = np.minimum(_tile_sums_crop(changed_sub, cy0, cx0, state.cur.shape),
                         TILE_WEIGHT_CAP)
    matched = (_tile_sums_crop(mismatch, cy0, cx0, state.cur.shape) == 0) & (weights > 0)
    _add_ops(c.size * 4 + weights.size * 3)
    valid = changed_info = None
    if keep_pixels:
        valid = np.zeros(state.cur.shape, dtype=bool)
        valid[csl] = valid_sub
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



def _runner_support(state, primary, runner):
    """Recount every runner on the primary's common changed information."""
    y0, y1, x0, x1 = _overlap(state.band, runner.dx, runner.dy)
    csl = np.s_[y0:y1, x0:x1]
    psl = np.s_[y0-runner.dy:y1-runner.dy, x0-runner.dx:x1-runner.dx]
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
    psl = np.s_[y0-best.dy:y1-best.dy, x0-best.dx:x1-best.dx]
    c, p = state.cur[csl], state.prev[psl]
    valid, changed = best.valid[csl], best.changed_info[csl]
    _add_ops(c.size * 2)
    _record("sparse_exact_comparisons", int(np.count_nonzero(valid)))
    # Exact SAD alone does not certify uninformative flat-flat comparisons.
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
    changed = state.prev != state.cur
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
    p = state.prev[y0-best.dy:y1-best.dy, x0-best.dx:x1-best.dx]
    _add_ops(c.size)
    _record("viewport_exact_comparisons", c.size)
    return bool(np.array_equal(c, p))


def _decide(state, candidates, max_d):
    evidence = []
    proposals = sorted(set(candidates) | {(0, 0)})
    _record("candidate_hypotheses", len(proposals))
    dense_budget = [DENSE_SAD_BUDGET]
    for dx, dy in proposals:
        if abs(dx) <= max_d and abs(dy) <= max_d:
            item = _verify(state, int(dx), int(dy), _dense_budget=dense_budget)
            if item is not None:
                evidence.append(item)
    if not evidence:
        return None
    evidence.sort(key=cmp_to_key(_evidence_compare))
    best = evidence[0]
    if (best.dx, best.dy) == (0, 0) or not alias_is_unique(best, evidence[1:]):
        return None
    yy, xx = np.nonzero(best.matched)
    if (yy.size < MIN_SUPPORT_TILES
            or np.unique(yy).size < MIN_SUPPORT_ROWS or np.unique(xx).size < MIN_SUPPORT_COLS
            or best.changed_weight == 0
            or best.support_weight * PPT < PRIMARY_MIN_PPT * best.changed_weight):
        return None
    # Only the primary's pixel masks persist through the runner pass. A runner
    # requires its geometry and exact equality, not another residual reduction.
    best = _verify(state, best.dx, best.dy, keep_pixels=True)
    sparse = best.support_weight < MIN_SUPPORT_WEIGHT
    if sparse and not _sparse_exact_support(state, best, evidence[1:]):
        return None
    for other in evidence[1:]:
        if (other.dx, other.dy) == (0, 0):
            continue
        support = _runner_support(state, best, other)
        if mode_veto(best.support_weight, support, best.changed_weight):
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


def _projection_rank(cur, prev, max_d, minimum):
    length = len(cur)
    records = []
    for shift in range(-min(max_d, length - 1), min(max_d, length - 1) + 1):
        lo, hi = max(0, shift), min(length, length + shift)
        if hi - lo >= minimum:
            _add_ops((hi - lo) * 2)
            raw = int(np.abs(cur[lo:hi] - prev[lo-shift:hi-shift]).sum(dtype=np.int64))
            records.append((raw * length // (hi - lo), abs(shift), shift))
    return sorted(records)


def retained_axis_candidates(cur, prev, max_d, minimum, keep=PROJECTION_RAW_KEEP):
    """Keep raw all-integer-offset winners, independent of smoothed coarse bins.

    No padding, FFT, interpolation or subpixel estimation is used.  The raw
    route preserves a -4 micro-scroll and a -960 shift with 120 observable rows.
    """
    raw = _projection_rank(cur, prev, max_d, minimum)
    selected = {r[2] for r in raw[:keep]}
    selected.add(0)
    if len(cur) >= PROJECTION_SMOOTH and len(prev) >= PROJECTION_SMOOTH:
        _add_ops((len(cur) - PROJECTION_SMOOTH + 1) * PROJECTION_SMOOTH * 2)
        kernel = np.ones(PROJECTION_SMOOTH, dtype=np.int64)
        cs = np.convolve(cur, kernel, mode="valid")
        ps = np.convolve(prev, kernel, mode="valid")
        smooth = _projection_rank(cs, ps, max_d, max(1, minimum - PROJECTION_SMOOTH + 1))
        coarse = [r for r in smooth if r[2] % PROJECTION_SMOOTH == 0][:PROJECTION_COARSE_KEEP]
        # Smoothed-only basins are additional proposals; raw winners can never
        # be displaced by them when the retained set is formed.
        for center in coarse:
            basin = [r for r in raw if abs(r[2] - center[2]) < PROJECTION_SMOOTH]
            selected.update(r[2] for r in basin[:PROJECTION_BASIN_KEEP])
    return tuple(sorted(selected))


def _projection_pairs(prev, cur, max_d, keep):
    h, w = prev.shape
    if h < MIN_OVERLAP or w < MIN_OVERLAP:
        return set()
    _add_ops(h * w * 2)
    cr, pr = cur.sum(axis=1, dtype=np.int64), prev.sum(axis=1, dtype=np.int64)
    dys = retained_axis_candidates(cr, pr, max_d, min_ov_y(h), keep)
    pairs = set()
    for dy in dys:
        lo, hi = max(0, dy), min(h, h + dy)
        if hi - lo < min_ov_y(h):
            continue
        _add_ops((hi - lo) * w * 2)
        cc = cur[lo:hi].sum(axis=0, dtype=np.int64)
        pc = prev[lo-dy:hi-dy].sum(axis=0, dtype=np.int64)
        for dx in retained_axis_candidates(cc, pc, max_d, min_ov_y(w), min(keep, PROJECTION_AXIS_KEEP)):
            pairs.add((dx, dy))
    return pairs


def dynamic_band_candidates(state, max_d):
    y0, y1, x0, x1 = state.band
    p, c = state.prev[y0:y1, x0:x1], state.cur[y0:y1, x0:x1]
    pairs = _projection_pairs(p, c, max_d, PROJECTION_RAW_KEEP)
    # Local row runs propose independently moving components; they do not
    # remove any pixel from the final verifier or its stationary counterevidence.
    _add_ops(state.cur.size * 2)
    changed = (state.prev != state.cur) & state.allowed
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
            pairs.update(_projection_pairs(state.prev[ya:yb, xa:xb], state.cur[ya:yb, xa:xb],
                                           max_d, PROJECTION_REGION_KEEP))
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

    # Valid in-bounds region in cur for displacement (dx, dy)
    cy0_v = max(0, dy)
    cy1_v = min(H, H + dy)
    cx0_v = max(0, dx)
    cx1_v = min(W, W + dx)

    # Initialize diff array with True (True = mismatch or out of bounds)
    diff = np.ones((H, W), dtype=bool)

    if cy0_v < cy1_v and cx0_v < cx1_v:
        py0_v = cy0_v - dy
        py1_v = cy1_v - dy
        px0_v = cx0_v - dx
        px1_v = cx1_v - dx

        cur_overlap = cur[cy0_v:cy1_v, cx0_v:cx1_v]
        prev_overlap = prev[py0_v:py1_v, px0_v:px1_v]
        _add_ops(cur_overlap.size)

        diff[cy0_v:cy1_v, cx0_v:cx1_v] = (cur_overlap != prev_overlap)

    # Pad diff to (gh * s, gw * s) with False so padded pixels outside (H, W) are ignored
    pad_h = gh * s - H
    pad_w = gw * s - W

    if pad_h > 0 or pad_w > 0:
        diff_padded = np.pad(diff, ((0, pad_h), (0, pad_w)), mode='constant', constant_values=False)
    else:
        diff_padded = diff

    # Reshape and check if any mismatch exists in tile
    diff_reshaped = diff_padded.reshape(gh, s, gw, s).swapaxes(1, 2)
    has_mismatch = np.any(diff_reshaped, axis=(2, 3))
    _add_ops(diff_padded.size)

    changed = cur != prev
    if pad_h > 0 or pad_w > 0:
        changed = np.pad(changed, ((0, pad_h), (0, pad_w)), constant_values=False)
    changed_tiles = changed.reshape(gh, s, gw, s).any(axis=(1, 3))
    _add_ops(H * W)
    return ~has_mismatch & changed_tiles
