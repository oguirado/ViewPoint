"""Integer constellation proposals with the shared v2.1 motion verifier.

Features are exact 256-byte luma patches, not perceptual or collision-prone
hashes.  Their votes only propose displacements: informative residuals,
distributed changed support, aliases and competing motion are checked by
``gm_bands_hv`` against its common evidence band.  Public consensus is the
same changed-only, whole-real-tile correspondence rule as that module.

OPS is a per-call, context-local diagnostic work count.  It includes dense
gradient and summed-area operations, inspected origins and feature pairs,
plus the shared verifier's accounting.  OPS_DETAIL separates feature work,
shared work and the shared module's counters (with a ``bands_`` prefix).  These
are not machine-instruction or latency estimates.  Calling this plugin leaves
both bands diagnostic contexts intact.
"""

from collections import Counter, defaultdict
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps

import numpy as np

from . import gm_bands_hv as _bands


TILE = 16
FEATURE_SIZE = 16
FEATURE_CELL = 16
FEATURES_PER_CELL = 2
FEATURE_TOP_K = 256
FEATURE_MIN_GRADIENT = 256
HISTOGRAM_KEEP = 32
HISTOGRAM_HARD_LIMIT = 256
PAIR_PER_DEST_TILE_MODE_CAP = 1

_ops = ContextVar("constellation_ops", default=0)
_diagnostics = ContextVar("constellation_diagnostics", default=None)


def __getattr__(name):
    if name == "OPS":
        return _ops.get()
    if name == "OPS_DETAIL":
        return dict(_diagnostics.get() or {})
    raise AttributeError(name)


def reset_ops():
    """Reset this context's diagnostic count."""
    _ops.set(0)
    _diagnostics.set(None)


def _add_detail(key, count):
    details = dict(_diagnostics.get() or {})
    details[key] = details.get(key, 0) + int(count)
    _diagnostics.set(details)


def _add_ops(count):
    _ops.set(_ops.get() + int(count))
    _add_detail("feature_ops", count)


def _per_call(function):
    @wraps(function)
    def counted(*args, **kwargs):
        reset_ops()
        return function(*args, **kwargs)
    return counted


def _shared_call(function, *args):
    """Transfer shared work accounting without changing its caller's context."""
    token = _bands._ops.set(0)
    detail_token = _bands._diagnostics.set(None)
    try:
        return function(*args)
    finally:
        try:
            _ops.set(_ops.get() + int(_bands.OPS))
            _add_detail("shared_ops", _bands.OPS)
            for key, count in _bands.OPS_DETAIL.items():
                _add_detail("bands_" + key, count)
        finally:
            _bands._diagnostics.reset(detail_token)
            _bands._ops.reset(token)


def _window_sum(array, h, w):
    """Integer sums for every complete h-by-w window, in origin order."""
    integral = np.pad(array.astype(np.int64), ((1, 0), (1, 0)))
    integral = integral.cumsum(axis=0, dtype=np.int64).cumsum(axis=1, dtype=np.int64)
    result = (integral[h:, w:] - integral[:-h, w:]
              - integral[h:, :-w] + integral[:-h, :-w])
    _add_ops(2 * integral.size + 3 * result.size)
    return result


@dataclass(frozen=True)
class Feature:
    y: int
    x: int
    gradient: int
    signature: bytes


def select_features(frame, allowed=None):
    """Select exact patches from all integer origins before applying budgets.

    Internal horizontal and vertical absolute differences define the integer
    gradient.  Complete unmasked patches with gradient at least 256 qualify.
    Each fixed 16-by-16 origin cell retains two candidates, ordered by larger
    gradient then smaller row and column.  The same order selects the global
    best 256.  In particular, no origin is rounded onto the tile lattice.
    """
    frame = np.asarray(frame)
    if frame.dtype != np.uint8:
        raise TypeError("luma must be uint8")
    if frame.ndim != 2:
        raise ValueError("frame must have H by W shape")
    h, w = frame.shape
    if allowed is None:
        allowed = np.ones(frame.shape, dtype=bool)
    else:
        allowed = np.asarray(allowed)
        if allowed.dtype != np.bool_ or allowed.shape != frame.shape:
            raise ValueError("allowed must be a Boolean mask with the frame shape")
    if h < FEATURE_SIZE or w < FEATURE_SIZE:
        return ()

    luma = frame.astype(np.int64)
    gx = np.abs(luma[:, 1:] - luma[:, :-1])
    gy = np.abs(luma[1:] - luma[:-1])
    _add_ops(2 * (gx.size + gy.size))
    scores = (_window_sum(gx, FEATURE_SIZE, FEATURE_SIZE - 1)
              + _window_sum(gy, FEATURE_SIZE - 1, FEATURE_SIZE))
    valid = (_window_sum(allowed, FEATURE_SIZE, FEATURE_SIZE)
             == FEATURE_SIZE * FEATURE_SIZE)
    _add_ops(2 * scores.size)
    sh, sw = scores.shape
    candidates = []
    for ya in range(0, sh, FEATURE_CELL):
        for xa in range(0, sw, FEATURE_CELL):
            block = scores[ya:ya + FEATURE_CELL, xa:xa + FEATURE_CELL]
            eligible = valid[ya:ya + FEATURE_CELL, xa:xa + FEATURE_CELL]
            indices = np.flatnonzero(eligible.ravel()
                                     & (block.ravel() >= FEATURE_MIN_GRADIENT))
            _add_ops(2 * block.size)
            if not indices.size:
                continue
            # ravel order is (y,x); stable sorting preserves coordinate ties.
            order = indices[np.argsort(-block.ravel()[indices], kind="stable")
                            [:FEATURES_PER_CELL]]
            for index in order:
                yy, xx = divmod(int(index), block.shape[1])
                candidates.append((-int(block[yy, xx]), ya + yy, xa + xx))

    features = []
    for neg_gradient, y, x in sorted(candidates)[:FEATURE_TOP_K]:
        signature = frame[y:y + FEATURE_SIZE, x:x + FEATURE_SIZE].tobytes(order="C")
        features.append(Feature(y, x, -neg_gradient, signature))
        _add_ops(FEATURE_SIZE * FEATURE_SIZE)
    return tuple(features)


def constellation_histogram(previous_features, current_features, max_d):
    """Count exact-signature correspondences once per destination tile/mode."""
    if not isinstance(max_d, (int, np.integer)) or isinstance(max_d, (bool, np.bool_)):
        raise TypeError("max_d must be an integer")
    if max_d < 0:
        raise ValueError("max_d must be nonnegative")
    max_d = int(max_d)
    previous = defaultdict(list)
    for feature in previous_features:
        previous[feature.signature].append(feature)
        _add_ops(1)
    for features in previous.values():
        features.sort(key=lambda feature: (feature.y, feature.x))

    seen = set()
    histogram = Counter()
    for cur in sorted(current_features, key=lambda feature: (feature.y, feature.x)):
        _add_ops(1)
        for prev in previous.get(cur.signature, ()):
            dx, dy = int(cur.x) - int(prev.x), int(cur.y) - int(prev.y)
            _add_ops(1)
            if abs(dx) > max_d or abs(dy) > max_d:
                continue
            key = (int(cur.y) // TILE, int(cur.x) // TILE, dx, dy)
            if key in seen:
                continue
            seen.add(key)
            histogram[(dx, dy)] += PAIR_PER_DEST_TILE_MODE_CAP
    return histogram


def constellation_candidates(state, max_d):
    """Keep the top modes including every vote tie at the nominal cutoff.

    The mode budget is 32, extended through all ties.  If that set exceeds
    256, retain only zero so the verifier abstains rather than arbitrarily
    dropping aliases.  Empty histograms also retain only zero.
    """
    previous = select_features(state.prev, state.allowed)
    current = select_features(state.cur, state.allowed)
    histogram = constellation_histogram(previous, current, max_d)
    ranked = sorted(histogram, key=lambda vector: (
        -histogram[vector], abs(vector[0]) + abs(vector[1]), vector[0], vector[1]))
    if not ranked:
        return {(0, 0)}
    cutoff = histogram[ranked[min(HISTOGRAM_KEEP, len(ranked)) - 1]]
    retained = {vector for vector in ranked if histogram[vector] >= cutoff}
    if len(retained) > HISTOGRAM_HARD_LIMIT:
        return {(0, 0)}
    return retained | {(0, 0)}


@_per_call
def estimate_global_motion(prev_luma, cur_luma, max_d):
    """Return an integer (dx, dy, score) or refuse with None.

    Preserve the public flat-frame refusal and textured-identical (0,0,0)
    special case.  Every proposed nonzero displacement passes the shared
    v2.1 verifier; a histogram peak is never itself a motion claim.
    Score is the ceiling informative mean, with the shared exact fixed-chrome
    viewport compatibility rule documented in docs/spec/fx2_f5_gm_v2_note.md.
    """
    prev, cur, max_d = _shared_call(_bands._inputs, prev_luma, cur_luma, max_d)
    _add_ops(4 * prev.size)
    if (int(prev.min()) == int(prev.max())
            and int(cur.min()) == int(cur.max())):
        return None
    _add_ops(prev.size)
    if np.array_equal(prev, cur):
        return (0, 0, 0)
    state = _shared_call(_bands._prepare, prev, cur)
    if state is None:
        return None
    candidates = constellation_candidates(state, max_d)
    return _shared_call(_bands._decide, state, candidates, max_d)


@_per_call
def consensus_tiles(prev_luma, cur_luma, s, dx, dy):
    """Claim only changed whole real tiles with exact in-bounds sources."""
    return _shared_call(_bands.consensus_tiles, prev_luma, cur_luma, s, dx, dy)
