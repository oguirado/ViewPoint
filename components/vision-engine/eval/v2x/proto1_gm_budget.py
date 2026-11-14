"""Pre-admitted, full-search GM adapter for PerWindowDetector.

Only the operation accounting performed by gm_bands_hv is budgeted.  The host
CPU-time, peak RAM and transport/capture costs are not bounded by this object.
The producer-internal stage functions are deliberately pinned by the companion
contract; a parity gate must be rerun whenever the producer changes.
"""

from contextvars import ContextVar

import numpy as np

from . import gm_bands_hv as _gm
from .per_window_detector import DetectorInputError, MissingEvidenceError


# These are local, provisional refusal messages, not new upstream error exports.
_MISSING = "GM budget not configured"
_BAD_LIMIT = "GM budget must be an exact nonnegative integer"
_EXHAUSTED = "GM operation budget exhausted before {}"
_ACCOUNTING = "GM operation envelope violated in {}"


def _rank_ops(length, max_d):
    """Upper OPS for one raw projection scan (ignores minimum-overlap skips)."""
    q = min(length - 1, max_d)
    return 2 * ((2 * q + 1) * length - q * (q + 1))


def _axis_ops(length, max_d):
    cost = _rank_ops(length, max_d)
    k = _gm.PROJECTION_SMOOTH
    if length >= k:
        cost += 2 * k * (length - k + 1)
        cost += _rank_ops(length - k + 1, max_d)
    return cost


def _axis_count(length, max_d, keep):
    """Upper count from raw winners, zero and smoothed basin additions."""
    possible = 2 * min(length - 1, max_d) + 1
    return min(possible, keep + 1 + _gm.PROJECTION_COARSE_KEEP * _gm.PROJECTION_BASIN_KEEP)


def _projection_ops(height, width, max_d, keep):
    if min(height, width) < _gm.MIN_OVERLAP:
        return 0
    # Row+column projections, then at most ys row-shift-dependent x scans.
    ys = _axis_count(height, max_d, keep)
    return (2 * height * width + _axis_ops(height, max_d)
            + ys * (2 * height * width + _axis_ops(width, max_d)))


def _candidate_ops(height, width, max_d, full_pixels):
    """Bound every projection call, including up to eight extra row runs."""
    return (2 * full_pixels
            + _projection_ops(height, width, max_d, _gm.PROJECTION_RAW_KEEP)
            + _gm.PROJECTION_REGION_LIMIT
            * _projection_ops(height, width, max_d, _gm.PROJECTION_REGION_KEEP))


def _tile_crop_ops(top, left, height, width):
    tile = _gm.TILE
    rows = (top % tile + height + tile - 1) // tile * tile
    cols = (left % tile + width + tile - 1) // tile * tile
    return rows * cols


def _decision_ops(state, candidates, max_d):
    """Upper envelope for all full-search verifications and runner passes.

    The source verifier charges 10*A for informative masks, 3*A in its first
    reduction, at most 2*A for either dense or packed SAD, two tile crops and
    4*A+3*T for support.  We admit every eligible proposal, not a short list.
    """
    height, width = state.cur.shape
    pixels = height * width
    tile = _gm.TILE
    tile_count = ((height + tile - 1) // tile) * ((width + tile - 1) // tile)
    bh = state.band[1] - state.band[0]
    bw = state.band[3] - state.band[2]
    verify_total = 0
    max_verify = 0
    max_sparse = 0
    runners = 0
    eligible = False
    for dx, dy in set(candidates) | {(0, 0)}:
        if abs(dx) > max_d or abs(dy) > max_d:
            continue
        cy0, cy1, cx0, cx1 = _gm._overlap(state.band, dx, dy)
        hh, ww = cy1 - cy0, cx1 - cx0
        if hh < _gm.min_ov_y(bh) or ww < _gm.min_ov_y(bw):
            continue
        eligible = True
        area = hh * ww
        crop = _tile_crop_ops(cy0, cx0, hh, ww)
        verification = 19 * area + 2 * crop + 3 * tile_count
        verify_total += verification
        max_verify = max(max_verify, verification)
        max_sparse = max(max_sparse, 19 * area + crop + 2 * tile_count)
        runners += 4 * area + 2 * crop + 2 * tile_count
    if not eligible:
        return 0
    # One best re-verification; optional exact sparse certification; any other
    # eligible candidate can be a runner; fixed-chrome full viewport check.
    return verify_total + max_verify + max_sparse + runners + 3 * pixels


def estimate_op_envelope(height, width, max_d):
    """Conservative full-call OPS ceiling for capacity planning, not a default.

    Positive integer geometry and nonnegative integral max_d are required.
    This bound deliberately includes every possible projection proposal; the
    runtime uses tighter, state-dependent staged admission instead.
    """
    if any(type(x) is not int or x <= 0 for x in (height, width)):
        raise ValueError("GM envelope geometry must be positive exact integers")
    if type(max_d) is not int or max_d < 0:
        raise ValueError("GM envelope max_d must be a nonnegative exact integer")
    n = height * width
    primary_y = _axis_count(height, max_d, _gm.PROJECTION_RAW_KEEP)
    primary_x = _axis_count(width, max_d, min(_gm.PROJECTION_RAW_KEEP, _gm.PROJECTION_AXIS_KEEP))
    region_y = _axis_count(height, max_d, _gm.PROJECTION_REGION_KEEP)
    region_x = _axis_count(width, max_d, min(_gm.PROJECTION_REGION_KEEP, _gm.PROJECTION_AXIS_KEEP))
    max_candidates = (1 + primary_y * primary_x
                      + _gm.PROJECTION_REGION_LIMIT * region_y * region_x)
    tile = _gm.TILE
    t = ((height + tile - 1) // tile) * ((width + tile - 1) // tile)
    crop = (height + 2 * (tile - 1)) * (width + 2 * (tile - 1))
    verify = 19 * n + 2 * crop + 3 * t
    runner = 4 * n + 2 * crop + 2 * t
    sparse = 19 * n + crop + 2 * t
    return (5 * n + 17 * n + _candidate_ops(height, width, max_d, n)
            + (max_candidates + 1) * verify + max_candidates * runner
            + sparse + 3 * n)


def consensus_op_envelope(height, width, tile_side):
    """Worst counted work: two full masks, overlap, padded tile pass, change."""
    if any(type(x) is not int or x <= 0 for x in (height, width, tile_side)):
        raise ValueError("GM consensus envelope geometry must be positive exact integers")
    padded_h = (height + tile_side - 1) // tile_side * tile_side
    padded_w = (width + tile_side - 1) // tile_side * tile_side
    return 4 * height * width + padded_h * padded_w


class BudgetedGM:
    """Per-invocation OPS-cap adapter accepted by PerWindowDetector(gm=...).

    max_ops MUST be supplied explicitly by the caller's measured baseline
    configuration.  Two calls forming estimate + consensus share the cap.
    Every context (including independent detector lanes) owns its counters.
    """

    def __init__(self, max_ops=None):
        if max_ops is None:
            raise MissingEvidenceError(_MISSING)
        if type(max_ops) is not int or max_ops < 0:
            raise DetectorInputError(_BAD_LIMIT)
        self.max_ops = max_ops
        self._ops = ContextVar("proto1_gm_last_ops", default=0)
        self._detail = ContextVar("proto1_gm_last_detail", default=None)
        self._used = ContextVar("proto1_gm_previous_estimate_ops", default=0)
        self._pending = ContextVar("proto1_gm_previous_estimate_valid", default=False)

    @property
    def OPS(self):
        return self._ops.get()

    @property
    def OPS_DETAIL(self):
        return dict(self._detail.get() or {})

    def reset_ops(self):
        self._ops.set(0)
        self._detail.set({})
        self._used.set(0)
        self._pending.set(False)
        _gm.reset_ops()

    def _admit(self, phase, upper, preceding=0):
        current = _gm.OPS
        if current + preceding + upper > self.max_ops:
            raise MissingEvidenceError(_EXHAUSTED.format(phase))
        return current

    def _check(self, phase, old, upper):
        actual = _gm.OPS
        if type(actual) is not int or actual < old or actual - old > upper:
            raise MissingEvidenceError(_ACCOUNTING.format(phase))

    def _finish(self, spent):
        self._ops.set(spent)
        self._detail.set(dict(_gm.OPS_DETAIL))

    def estimate_global_motion(self, prev_luma, cur_luma, max_d):
        self.reset_ops()
        try:
            prev, cur, max_d = _gm._inputs(prev_luma, cur_luma, max_d)
            n = prev.size
            start = self._admit("estimate", 5 * n)
            _gm._add_ops(5 * n)
            self._check("estimate", start, 5 * n)
            if int(prev.min()) == int(prev.max()) and int(cur.min()) == int(cur.max()):
                decision = None
            elif np.array_equal(prev, cur):
                decision = (0, 0, 0)
            else:
                start = self._admit("prepare", 17 * n)
                state = _gm._prepare(prev, cur)
                self._check("prepare", start, 17 * n)
                if state is None:
                    decision = None
                else:
                    y0, y1, x0, x1 = state.band
                    upper = _candidate_ops(y1 - y0, x1 - x0, max_d, n)
                    start = self._admit("candidates", upper)
                    candidates = _gm.dynamic_band_candidates(state, max_d)
                    self._check("candidates", start, upper)
                    upper = _decision_ops(state, candidates, max_d)
                    start = self._admit("verification", upper)
                    decision = _gm._decide(state, candidates, max_d)
                    self._check("verification", start, upper)
            self._finish(int(_gm.OPS))
            self._used.set(self.OPS)
            self._pending.set(True)
            return decision
        except BaseException:
            self._ops.set(0)
            self._detail.set({})
            self._used.set(0)
            self._pending.set(False)
            raise

    def consensus_tiles(self, prev_luma, cur_luma, s, dx, dy):
        prior = self._used.get() if self._pending.get() else 0
        # The producer's public consensus call resets OPS and OPS_DETAIL even
        # after a preceding estimate; expose the same per-call behavior.
        self.reset_ops()
        try:
            if not all(isinstance(v, (int, np.integer)) and not isinstance(v, (bool, np.bool_))
                       for v in (dx, dy)):
                raise TypeError("displacements must be integers")
            if not isinstance(s, (int, np.integer)) or isinstance(s, (bool, np.bool_)) or s <= 0:
                raise ValueError("tile side must be a positive integer")
            prev, cur, _ = _gm._inputs(prev_luma, cur_luma, max(abs(dx), abs(dy)))
            upper = consensus_op_envelope(cur.shape[0], cur.shape[1], int(s))
            start = self._admit("consensus", upper, preceding=prior)
            result = _gm.consensus_tiles(prev, cur, s, dx, dy)
            self._check("consensus", start, upper)
            self._finish(int(_gm.OPS))
            return result
        except BaseException:
            self._ops.set(0)
            self._detail.set({})
            raise
