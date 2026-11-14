"""Executable, test-local proposals for router C1; frames remain read-only RAM.

Neither estimator imports or replaces a production estimator.  Every tuning
constant below is a PROPOSED CHOICE, not a measured host optimum.  The companion
specification defines the public score, safety gates and unverified live target.
"""

import ast
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import cmp_to_key
from pathlib import Path
import sys
import unittest

import numpy as np

from eval.v2x import adv_gm


# PROPOSED CHOICES, all integers.  No implicit floating-point arithmetic is used.
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
FEATURE_SIZE = 16
FEATURE_CELL = 16
FEATURES_PER_CELL = 2
FEATURE_TOP_K = 256
FEATURE_MIN_GRADIENT = 256
HISTOGRAM_KEEP = 32
HISTOGRAM_HARD_LIMIT = 256
PAIR_PER_DEST_TILE_MODE_CAP = 1


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


def _verify(state, dx, dy, keep_pixels=False):
    """Rational residual plus capped exact support on eligible tile fragments.

    Scoring fragments are intersections of fixed global tiles and the band
    overlap.  They can contribute only their real informative changed pixels,
    capped at 128 per tile.  Final consensus below has the stronger whole-real-
    destination-tile condition and never promotes a partly exposed tile.
    """
    cy0, cy1, cx0, cx1 = _overlap(state.band, dx, dy)
    bh, bw = state.band[1] - state.band[0], state.band[3] - state.band[2]
    if cy1 - cy0 < min_ov_y(bh) or cx1 - cx0 < min_ov_y(bw):
        return None
    csl = np.s_[cy0:cy1, cx0:cx1]
    psl = np.s_[cy0-dy:cy1-dy, cx0-dx:cx1-dx]
    valid_sub = state.allowed[csl] & state.allowed[psl]
    # Gradient endpoints must both belong to this candidate's eligible
    # overlap; an exposed pixel cannot alter an adjacent evidence weight.
    informative = (informative_pixels(state.cur[csl], valid_sub)
                   | informative_pixels(state.prev[psl], valid_sub))
    N_dyn = int(np.count_nonzero(informative))
    if N_dyn == 0:
        return None
    difference = np.abs(state.cur[csl].astype(np.int64) - state.prev[psl].astype(np.int64))
    raw = int(difference[informative].sum(dtype=np.int64))
    valid = np.zeros(state.cur.shape, dtype=bool)
    valid[csl] = valid_sub
    mismatch = np.zeros(state.cur.shape, dtype=bool)
    mismatch[csl] = (difference != 0) & valid_sub
    changed_info = np.zeros(state.cur.shape, dtype=bool)
    changed_info[csl] = (state.cur[csl] != state.prev[csl]) & informative
    weights = np.minimum(_tile_sums(changed_info), TILE_WEIGHT_CAP)
    matched = (_tile_sums(mismatch) == 0) & (weights > 0)
    return Evidence(int(dx), int(dy), raw, N_dyn, (raw + N_dyn - 1) // N_dyn,
                    int(weights.sum()), int(weights[matched].sum()), weights,
                    valid if keep_pixels else None, matched,
                    changed_info if keep_pixels else None)


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
    # Recount on the primary's changed informative support.  Pixels beyond
    # either candidate's valid overlap are unavailable, never negative votes.
    common = primary.valid & runner.valid
    cy0, cy1, cx0, cx1 = _overlap(state.band, runner.dx, runner.dy)
    mismatch = np.zeros(state.cur.shape, dtype=bool)
    c = state.cur[cy0:cy1, cx0:cx1]
    p = state.prev[cy0-runner.dy:cy1-runner.dy, cx0-runner.dx:cx1-runner.dx]
    mismatch[cy0:cy1, cx0:cx1] = (c != p)
    match_tiles = _tile_sums(mismatch & common) == 0
    weights = np.minimum(_tile_sums(primary.changed_info & common), TILE_WEIGHT_CAP)
    return int(weights[match_tiles].sum())


def _decide(state, candidates, max_d):
    evidence = []
    for dx, dy in sorted(set(candidates) | {(0, 0)}):
        if abs(dx) <= max_d and abs(dy) <= max_d:
            item = _verify(state, int(dx), int(dy))
            if item is not None:
                evidence.append(item)
    if not evidence:
        return None
    evidence.sort(key=cmp_to_key(_evidence_compare))
    best = evidence[0]
    # Zero displacement competes in residual ranking, retaining the veto from
    # stationary texture.  It cannot emit spatial claims through this router.
    if (best.dx, best.dy) == (0, 0):
        return None
    if not alias_is_unique(best, evidence[1:]):
        return None
    yy, xx = np.nonzero(best.matched)
    if (best.support_weight < MIN_SUPPORT_WEIGHT or yy.size < MIN_SUPPORT_TILES
            or np.unique(yy).size < MIN_SUPPORT_ROWS or np.unique(xx).size < MIN_SUPPORT_COLS
            or best.changed_weight == 0
            or best.support_weight * PPT < PRIMARY_MIN_PPT * best.changed_weight):
        return None
    best = _verify(state, best.dx, best.dy, keep_pixels=True)
    for other in evidence[1:]:
        if (other.dx, other.dy) == (0, 0):
            continue
        runner_item = _verify(state, other.dx, other.dy, keep_pixels=True)
        runner = _runner_support(state, best, runner_item)
        if mode_veto(best.support_weight, runner, best.changed_weight):
            return None
    return int(best.dx), int(best.dy), int(best.score)


def _projection_rank(cur, prev, max_d, minimum):
    length = len(cur)
    records = []
    for shift in range(-min(max_d, length - 1), min(max_d, length - 1) + 1):
        lo, hi = max(0, shift), min(length, length + shift)
        if hi - lo >= minimum:
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
    cr, pr = cur.sum(axis=1, dtype=np.int64), prev.sum(axis=1, dtype=np.int64)
    dys = retained_axis_candidates(cr, pr, max_d, min_ov_y(h), keep)
    pairs = set()
    for dy in dys:
        lo, hi = max(0, dy), min(h, h + dy)
        if hi - lo < min_ov_y(h):
            continue
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


def estimate_dynamic_band_v21(prev_luma, cur_luma, max_d):
    prev, cur, max_d = _inputs(prev_luma, cur_luma, max_d)
    if np.array_equal(prev, cur):
        return None
    state = _prepare(prev, cur)
    if state is None:
        return None
    return _decide(state, dynamic_band_candidates(state, max_d), max_d)


def _window_sum(array, h, w):
    integral = np.pad(array.astype(np.int64), ((1, 0), (1, 0))).cumsum(axis=0).cumsum(axis=1)
    return integral[h:, w:] - integral[:-h, w:] - integral[h:, :-w] + integral[:-h, :-w]


@dataclass(frozen=True)
class Feature:
    y: int
    x: int
    gradient: int
    signature: bytes


def select_features(frame, allowed=None):
    """Top-K exact 16-square luma signatures; (-gradient,y,x) resolves ties.

    All integer patch origins are scored, then at most two survive each fixed
    16-square origin cell.  The global top K are retained.  This avoids the
    grid-to-grid-only matching error for shifts that are not multiples of 16.
    """
    h, w = frame.shape
    if h < FEATURE_SIZE or w < FEATURE_SIZE:
        return ()
    if allowed is None:
        allowed = np.ones(frame.shape, dtype=bool)
    a = frame.astype(np.int64)
    gx, gy = np.abs(a[:, 1:] - a[:, :-1]), np.abs(a[1:] - a[:-1])
    scores = (_window_sum(gx, FEATURE_SIZE, FEATURE_SIZE - 1)
              + _window_sum(gy, FEATURE_SIZE - 1, FEATURE_SIZE))
    valid = _window_sum(allowed, FEATURE_SIZE, FEATURE_SIZE) == FEATURE_SIZE * FEATURE_SIZE
    sh, sw = scores.shape
    candidates = []
    for ya in range(0, sh, FEATURE_CELL):
        for xa in range(0, sw, FEATURE_CELL):
            block = scores[ya:ya+FEATURE_CELL, xa:xa+FEATURE_CELL]
            eligible = valid[ya:ya+FEATURE_CELL, xa:xa+FEATURE_CELL]
            indices = np.flatnonzero(eligible.ravel() & (block.ravel() >= FEATURE_MIN_GRADIENT))
            if not indices.size:
                continue
            order = indices[np.argsort(-block.ravel()[indices], kind="stable")[:FEATURES_PER_CELL]]
            for index in order:
                yy, xx = divmod(int(index), block.shape[1])
                candidates.append((-int(block[yy, xx]), ya + yy, xa + xx))
    features = []
    for neg_gradient, y, x in sorted(candidates)[:FEATURE_TOP_K]:
        signature = frame[y:y+FEATURE_SIZE, x:x+FEATURE_SIZE].tobytes(order="C")
        features.append(Feature(y, x, -neg_gradient, signature))
    return tuple(features)


def constellation_histogram(previous_features, current_features, max_d):
    """Exact luma-byte pairing and integer offset voting, with tile vote caps."""
    previous = defaultdict(list)
    for feature in previous_features:
        previous[feature.signature].append(feature)
    seen = set()
    histogram = Counter()
    for cur in sorted(current_features, key=lambda f: (f.y, f.x)):
        for prev in sorted(previous.get(cur.signature, ()), key=lambda f: (f.y, f.x)):
            dx, dy = cur.x - prev.x, cur.y - prev.y
            if abs(dx) > max_d or abs(dy) > max_d:
                continue
            key = (cur.y // TILE, cur.x // TILE, dx, dy)
            if key in seen:
                continue
            seen.add(key)
            histogram[(dx, dy)] += PAIR_PER_DEST_TILE_MODE_CAP
    return histogram


def constellation_candidates(state, max_d):
    previous = select_features(state.prev, state.allowed)
    current = select_features(state.cur, state.allowed)
    histogram = constellation_histogram(previous, current, max_d)
    ranked = sorted(histogram, key=lambda d: (-histogram[d], abs(d[0]) + abs(d[1]), d[0], d[1]))
    if not ranked:
        return {(0, 0)}
    cutoff = histogram[ranked[min(HISTOGRAM_KEEP, len(ranked)) - 1]]
    retained = {vector for vector in ranked if histogram[vector] >= cutoff}
    # An equal-vote ambiguity must not be resolved by dropping aliases at the
    # K boundary.  Saturation abstains instead of silently cutting that tie.
    if len(retained) > HISTOGRAM_HARD_LIMIT:
        return {(0, 0)}
    return retained | {(0, 0)}


def estimate_constellation(prev_luma, cur_luma, max_d):
    prev, cur, max_d = _inputs(prev_luma, cur_luma, max_d)
    if np.array_equal(prev, cur):
        return None
    state = _prepare(prev, cur)
    if state is None:
        return None
    return _decide(state, constellation_candidates(state, max_d), max_d)


def consensus_tiles(prev_luma, cur_luma, s, dx, dy):
    """Exact changed-only whole-real-tile claims; exposed real pixels veto."""
    prev, cur, _ = _inputs(prev_luma, cur_luma, max(abs(dx), abs(dy)))
    if not isinstance(s, (int, np.integer)) or isinstance(s, (bool, np.bool_)) or s <= 0:
        raise ValueError("tile side must be a positive integer")
    if not all(isinstance(v, (int, np.integer)) and not isinstance(v, (bool, np.bool_)) for v in (dx, dy)):
        raise TypeError("displacements must be integers")
    h, w = cur.shape
    mismatch = np.ones(cur.shape, dtype=bool)
    y0, y1, x0, x1 = _overlap((0, h, 0, w), int(dx), int(dy))
    if y0 < y1 and x0 < x1:
        mismatch[y0:y1, x0:x1] = cur[y0:y1, x0:x1] != prev[y0-dy:y1-dy, x0-dx:x1-dx]
    changed = _tile_sums(prev != cur, int(s)) > 0
    return changed & (_tile_sums(mismatch, int(s)) == 0)


ESTIMATORS = (estimate_dynamic_band_v21, estimate_constellation)


def _texture_fixture(dy):
    rng = np.random.default_rng(101)
    prev = np.full((480, 640), 250, dtype=np.uint8)
    cur = prev.copy()
    block = rng.integers(0, 256, (96, 128), dtype=np.uint8)
    prev[180:276, 256:384] = block
    cur[180+dy:276+dy, 256:384] = block
    return prev, cur


def _competing_fixture():
    rng = np.random.default_rng(912)
    prev = np.full((480, 640), 40, dtype=np.uint8)
    cur = prev.copy()
    for h, w, y, x, dx, dy in ((100, 150, 64, 64, 12, 0), (100, 80, 280, 380, 0, -10)):
        block = rng.integers(0, 256, (h, w), dtype=np.uint8)
        prev[y:y+h, x:x+w] = block
        cur[y+dy:y+dy+h, x+dx:x+dx+w] = block
    return prev, cur


class TestDynamicBandAndConstellationSpec(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = adv_gm.cases(seed=0x5EED, include_1080p=True)

    def test_document_exists_and_is_nonempty(self):
        path = Path(__file__).resolve().parents[1].joinpath("docs/spec/jq1_a1_gm_v2_dynamic_band_and_constellation_spec.md")
        self.assertTrue(path.is_file())
        self.assertTrue(path.read_text(encoding="utf-8").strip())

    def test_adversarial_corpus_and_real_static_tiles(self):
        counts = Counter(case["kind"] for case in self.cases)
        pairs = sum(len(case["truth_vectors"]) for case in self.cases)
        self.assertEqual(counts, {"flat_window_drag": 4, "two_motions": 3, "chrome_clock": 3,
                                  "periodic_text": 4, "bare_caret": 3, "big_scroll_1080p": 4})
        self.assertEqual((len(self.cases), pairs), (21, 21))
        for estimate in ESTIMATORS:
            totals = Counter(pairs=0, exact=0, silent=0, wrong_vectors=0, static_relabels=0)
            for case in self.cases:
                for i, truth in enumerate(case["truth_vectors"]):
                    with self.subTest(estimator=estimate.__name__, case=case["case_id"], pair=i):
                        prev, cur = case["frames"][i:i+2]
                        result = estimate(prev, cur, max(case["h"], case["w"]) - 1)
                        vector = None if result is None else result[:2]
                        totals["pairs"] += 1
                        if vector == truth:
                            totals["exact"] += 1
                        elif vector is None:
                            totals["silent"] += 1
                        else:
                            totals["wrong_vectors"] += 1
                        self.assertTrue(vector is None or vector == truth, (vector, truth))
                        if case["kind"] == "chrome_clock":
                            self.assertEqual(vector, (0, -24))
                        if case["kind"] in ("two_motions", "periodic_text", "bare_caret"):
                            self.assertIsNone(result)
                        if case["kind"] in ("flat_window_drag", "big_scroll_1080p"):
                            self.assertEqual(vector, truth)
                        if result is not None:
                            self.assertTrue(all(type(value) is int for value in result))
                            self.assertGreaterEqual(result[2], 0)
                            mask = consensus_tiles(prev, cur, case["s"], *vector)
                        else:
                            mask = np.zeros(case["static_masks"][i].shape, dtype=bool)
                        static = _tile_sums(prev != cur, case["s"]) == 0
                        totals["static_relabels"] += int(np.count_nonzero(mask & static))
                        self.assertFalse(np.any(mask & static))
            self.assertEqual((totals["pairs"], totals["exact"], totals["silent"],
                              totals["wrong_vectors"], totals["static_relabels"]), (21, 18, 3, 0, 0))
            print("REFERENCE_SYNTHETIC", estimate.__name__, dict(sorted(totals.items())))

    def test_unequal_competing_blocks_are_refused(self):
        prev, cur = _competing_fixture()
        for estimate in ESTIMATORS:
            with self.subTest(estimator=estimate.__name__):
                self.assertIsNone(estimate(prev, cur, 100))

    def test_sparse_texture_and_micro_scroll_are_exact_zero(self):
        for dy in (-24, -4):
            prev, cur = _texture_fixture(dy)
            for estimate in ESTIMATORS:
                with self.subTest(estimator=estimate.__name__, dy=dy):
                    self.assertEqual(estimate(prev, cur, 100), (0, dy, 0))

    def test_independent_1080p_page_jump_has_exact_zero_score(self):
        rng = np.random.default_rng(960)
        prev = rng.integers(0, 256, (1080, 1920), dtype=np.uint8)
        cur = rng.integers(0, 256, (1080, 1920), dtype=np.uint8)
        cur[:120] = prev[-120:]
        for estimate in ESTIMATORS:
            with self.subTest(estimator=estimate.__name__):
                self.assertEqual(estimate(prev, cur, 1100), (0, -960, 0))

    def test_raw_candidate_retention_is_independent_of_smoothing(self):
        for size, shift in ((128, -4), (1080, -960)):
            rng = np.random.default_rng(700 + size)
            prev = rng.integers(1, 500, size, dtype=np.int64)
            cur = rng.integers(1, 500, size, dtype=np.int64)
            cur[:size+shift] = prev[-shift:]
            self.assertIn(shift, retained_axis_candidates(cur, prev, 1100, min_ov_y(size)))
        self.assertEqual([min_ov_y(x) for x in (16, 64, 128, 256, 1080)], [16, 16, 32, 64, 64])

    def test_exact_nonadjacent_ties_are_refused(self):
        rng = np.random.default_rng(32)
        unit = rng.integers(0, 256, (16, 128), dtype=np.uint8)
        prev = np.tile(unit, (10, 1))
        cur = np.roll(prev, -3, axis=0)
        for estimate in ESTIMATORS:
            self.assertIsNone(estimate(prev, cur, 80))

    def test_unique_zero_beats_sub_luma_alias_but_exact_tie_does_not(self):
        def record(dx, raw, n):
            return Evidence(dx, 0, raw, n, (raw+n-1)//n, 0, 0,
                            np.empty((0, 0), dtype=np.int64), np.empty((0, 0), dtype=bool),
                            np.empty((0, 0), dtype=bool), np.empty((0, 0), dtype=bool))
        best = record(5, 0, 100)
        self.assertTrue(alias_is_unique(best, [record(10, 1, 1000)]))
        self.assertFalse(alias_is_unique(best, [record(10, 0, 1000)]))
        self.assertFalse(alias_is_unique(record(5, 10, 100), [record(10, 11, 100)]))

    def test_multimode_threshold_boundaries_and_adjacent_modes(self):
        self.assertTrue(mode_veto(750, 250, 1000))
        self.assertFalse(mode_veto(750, 249, 1000))
        self.assertTrue(mode_veto(299, 100, 1000))
        self.assertFalse(mode_veto(300, 100, 1000))
        rng = np.random.default_rng(913)
        prev = np.full((256, 320), 40, dtype=np.uint8)
        cur = prev.copy()
        for y, x, dx in ((32, 32, 12), (144, 192, 13)):
            block = rng.integers(0, 256, (64, 64), dtype=np.uint8)
            prev[y:y+64, x:x+64] = block
            cur[y:y+64, x+dx:x+dx+64] = block
        for estimate in ESTIMATORS:
            self.assertIsNone(estimate(prev, cur, 48))

    def test_flat_support_abstains_and_text_flat_disagreement_is_penalized(self):
        for a, b in ((40, 40), (40, 250)):
            prev = np.full((128, 160), a, dtype=np.uint8)
            cur = np.full(prev.shape, b, dtype=np.uint8)
            for estimate in ESTIMATORS:
                self.assertIsNone(estimate(prev, cur, 32))
            # Zero numerical evidence never authorizes nonexact or static
            # whole-tile correspondence claims.
            self.assertFalse(consensus_tiles(prev, cur, 16, 4, 0).any())
        prev, cur = _texture_fixture(-4)
        state = _prepare(prev, cur)
        correct, wrong = _verify(state, 0, -4), _verify(state, 32, 0)
        self.assertEqual(correct.raw_sad, 0)
        self.assertGreater(correct.N_dyn, 0)
        self.assertGreater(wrong.raw_sad, 0)
        self.assertGreater(wrong.N_dyn, 0)
        texture = np.random.default_rng(71).integers(0, 256, (96, 128), dtype=np.uint8)
        for estimate in ESTIMATORS:
            self.assertIsNone(estimate(texture, texture.copy(), 100))

    def test_stationary_texture_cannot_be_cropped_or_masked_away(self):
        rng = np.random.default_rng(123)
        prev = rng.integers(0, 256, (256, 320), dtype=np.uint8)
        cur = prev.copy()
        block = rng.integers(0, 256, (64, 64), dtype=np.uint8)
        prev[96:160, 112:176] = block
        cur[96:160, 112:176] = rng.integers(0, 256, (64, 64), dtype=np.uint8)
        cur[96:160, 120:184] = block
        state = _prepare(prev, cur)
        self.assertEqual(state.band, (0, 256, 0, 320))
        self.assertTrue(state.allowed.all())
        for estimate in ESTIMATORS:
            self.assertIsNone(estimate(prev, cur, 48))

    def test_compact_chrome_span_cap_and_stationary_remainder(self):
        masks = {}
        eligible_changed = 0
        for case in self.cases:
            if case["kind"] == "chrome_clock":
                mask = compact_chrome_mask(*case["frames"])
                masks[case["w"]] = int(np.count_nonzero(mask))
                if case["w"] == 320:
                    prev, cur = case["frames"]
                    eligible_changed = int(np.count_nonzero(prev[10:30, 10:50] != cur[10:30, 10:50]))
                    self.assertFalse(np.any(mask & (prev == cur)))
                    self.assertEqual(int(np.count_nonzero(mask[10:30, 10:50])), masks[320])
        self.assertEqual((masks[256], masks[300]), (0, 0))
        self.assertGreater(masks[320], 0)
        # An unchanged textured endpoint can make a row's outside-span
        # remainder nonflat.  Such a row is deliberately not mask-eligible.
        self.assertLessEqual(masks[320], eligible_changed)
        rng = np.random.default_rng(81)
        prev = rng.integers(0, 256, (240, 320), dtype=np.uint8)
        cur = prev.copy()
        cur[10:30, 10:40] ^= 255
        cur[100:180] ^= 255
        self.assertFalse(compact_chrome_mask(prev, cur).any())
        prev = np.full((240, 320), 60, dtype=np.uint8)
        cur = prev.copy()
        prev[10:30, 10:50] = rng.integers(0, 256, (20, 40), dtype=np.uint8)
        cur[10:30, 10:50] = rng.integers(0, 256, (20, 40), dtype=np.uint8)
        island = rng.integers(0, 256, (8, 8), dtype=np.uint8)
        prev[16:24, 24:32] = island
        cur[16:24, 24:32] = island
        prev[80:200] = rng.integers(0, 256, (120, 320), dtype=np.uint8)
        cur[80:176] = prev[104:200]
        cur[176:200] = rng.integers(0, 256, (24, 320), dtype=np.uint8)
        mask = compact_chrome_mask(prev, cur)
        self.assertFalse(mask[16:24, 24:32].any())
        self.assertTrue(informative_pixels(prev, ~mask)[17:23, 25:31].all())

    def test_distributed_support_and_localized_vote_cap(self):
        rng = np.random.default_rng(88)
        prev = np.full((128, 160), 100, dtype=np.uint8)
        cur = prev.copy()
        block = rng.integers(0, 256, (16, 16), dtype=np.uint8)
        prev[48:64, 48:64] = block
        cur[48:64, 56:72] = block
        for estimate in ESTIMATORS:
            self.assertIsNone(estimate(prev, cur, 48))
        signature = bytes(range(256))
        previous = [Feature(0, 0, 1000, signature)]
        current = [Feature(4, 4, 1000, signature), Feature(4, 4, 999, signature)]
        self.assertEqual(constellation_histogram(previous, current, 48), {(4, 4): 1})

    def test_border_exclusion_and_partial_real_static_tiles(self):
        rng = np.random.default_rng(7)
        prev = rng.integers(0, 256, (35, 39), dtype=np.uint8)
        cur = rng.integers(0, 256, prev.shape, dtype=np.uint8)
        cur[:-4] = prev[4:]
        mask = consensus_tiles(prev, cur, 16, 0, -4)
        self.assertEqual(mask.shape, (3, 3))
        self.assertTrue(mask[0].all())
        self.assertFalse(mask[1:].any())
        self.assertFalse(consensus_tiles(prev, prev, 16, 0, 0).any())
        self.assertFalse(consensus_tiles(prev, cur, 16, 0, 100).any())
        state = _prepare(prev, cur)
        evidence = _verify(state, 0, -4, keep_pixels=True)
        self.assertEqual(evidence.raw_sad, 0)
        self.assertFalse(evidence.valid[-4:].any())
        self.assertLessEqual(evidence.N_dyn, 31 * 39)
        # Altered pixels outside the already-defined candidate overlap must
        # not manufacture informative evidence at its boundary.
        prev = np.full((64, 80), 100, dtype=np.uint8)
        cur = prev.copy()
        prev[:4] = rng.integers(0, 256, (4, 80), dtype=np.uint8)
        cur[-4:] = rng.integers(0, 256, (4, 80), dtype=np.uint8)
        state = _prepare(prev, cur)
        self.assertIsNone(_verify(state, 0, -4))

    def test_feature_order_is_deterministic_and_uses_luma_bytes(self):
        rng = np.random.default_rng(19)
        frame = rng.integers(0, 256, (80, 96), dtype=np.uint8)
        first = select_features(frame)
        self.assertEqual(first, select_features(frame))
        self.assertLessEqual(len(first), FEATURE_TOP_K)
        self.assertEqual(list(first), sorted(first, key=lambda f: (-f.gradient, f.y, f.x)))
        cells = Counter((f.y // FEATURE_CELL, f.x // FEATURE_CELL) for f in first)
        self.assertLessEqual(max(cells.values()), FEATURES_PER_CELL)
        for f in first:
            self.assertEqual(len(f.signature), FEATURE_SIZE * FEATURE_SIZE)
            self.assertEqual(f.signature, frame[f.y:f.y+16, f.x:f.x+16].tobytes())

    def test_inputs_are_read_only_and_invalid_float_inputs_are_rejected(self):
        prev, cur = _texture_fixture(-4)
        pbytes, cbytes = prev.tobytes(), cur.tobytes()
        prev.flags.writeable = False
        cur.flags.writeable = False
        for estimate in ESTIMATORS:
            self.assertEqual(estimate(prev, cur, 100), (0, -4, 0))
            with self.assertRaises(TypeError):
                estimate(prev.astype(np.float64), cur, 100)
            with self.assertRaises(TypeError):
                estimate(prev, cur, "100")
        self.assertEqual(prev.tobytes(), pbytes)
        self.assertEqual(cur.tobytes(), cbytes)

    def test_integer_only_arithmetic_and_no_expected_failures(self):
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            self.assertFalse(isinstance(node, ast.Constant) and isinstance(node.value, float))
            self.assertFalse(isinstance(node, ast.Div))
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, ("expectedFailure", "fft", "ifft", "mean", "true_divide"))
        forbidden = "gm_" + "constellation"
        self.assertFalse(any(name.rsplit(".", 1)[-1] == forbidden for name in sys.modules))


if __name__ == "__main__":
    unittest.main()
