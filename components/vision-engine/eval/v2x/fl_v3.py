"""Production FL v3: a baseline-only veto and the existing blink prior.

``veto_flicker`` implements the P1/P2/F6 contract documented in
``docs/spec/jq1_a2_s2_flicker_veto.md``. It has no test-module dependency and
never mutates inputs, changes labels, advances a tracker, or assumes that an
emitted/lazy hash is an exact-content hash. Callers must supply explicit
continuity and hash-validity evidence and apply the result to baseline S2
labels before positive tracker claims.

``FlickerTracker`` deliberately reuses ``fl_blinkprior.FlickerTracker``. Its
three-toggle warm-up, minimum half-run, symmetry tolerance, held-frame
behavior, third-state reset, and stalled-run reentry are unchanged. Its
positive declarations are independent of the veto's lag-two condition.
``OPS`` and ``reset_ops`` refer to that shared tracker implementation's
operation counter; the stateless veto does not increment this counter.
"""
from enum import IntEnum
from typing import NamedTuple

import numpy as np

from visioncore import change, desc, params as core_params
from . import fl_blinkprior as _blinkprior
from .fl_blinkprior import (
    C_UNCHANGED, C_JITTER, C_CHANGED, C_MOVED, C_FLICKER, C_FROZEN, C_DRIFT,
    UNCHANGED, JITTER, CHANGED, MOVED, FLICKER, FROZEN, DRIFT,
    FlickerTracker, reset_ops, ops_scope,
)


def __getattr__(name):
    # A copied integer would become stale when the shared tracker increments it.
    if name == "OPS":
        return _blinkprior.OPS
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def update_with_ops(tracker, hashes):
    """Run a tracker update in an independent ContextVar scope.

    The returned count is from the production FL source, not a guessed
    post-hoc copy of a shared module global. The enclosing copied context
    isolates other plugins' diagnostic variables as well.
    """
    return _blinkprior.update_with_ops(tracker, hashes)


class Reason(IntEnum):
    NOT_BASELINE_FLICKER = 0
    WORDS_EQUAL = 1
    MISSING_HISTORY = 2
    INVALID_HISTORY = 3
    UNTRUSTED_HISTORY = 4
    UNTRUSTED_HASH = 5
    EXACT_HASH_RETURN = 6
    MISSING_LUMA = 7
    INVALID_LUMA = 8
    PADDED_TILE = 9
    INCONSISTENT_GM = 10
    NO_GM_CONSENSUS = 11
    PARTIAL_WINDOW = 12
    NO_INWARD_SUPPORT = 13
    NOT_THREE_DISTINCT = 14
    PIXEL_RETURN_OR_SMALL_CHANGE = 15
    NOT_DESCRIPTOR_RETURN = 16
    VETO_P2 = 17
    VETO_F6 = 18


class VetoDecision(NamedTuple):
    mask: np.ndarray       # fresh bool[N], in the input descriptor order
    reasons: np.ndarray    # fresh uint8[N], values from Reason


_THRESHOLDS = (
    "T0_Q4", "HYST_BAND_PERMILLE", "H_DEAD", "SIG_DEAD_Q4",
    "MV_SAD_RATIO_DEN", "GLOBAL_SHARE_PPM",
)


def _integer(value):
    return isinstance(value, (int, np.integer)) and not isinstance(value, (bool, np.bool_))


def _window_sads(cur, prev, vector, s, tx, ty):
    """Pixel-aligned clipped windows, widened subtraction, int64 sums/counts.

    A count of zero is never an explanation. Coordinates are nonnegative grid
    coordinates validated by the caller; unlike native indexing, no wraparound
    is allowed. No padding, synthetic pixels, or normalized float means.
    """
    dx, dy = (int(v) for v in vector)
    h, w = cur.shape
    sums = np.zeros(len(tx), dtype=np.int64)
    counts = np.zeros(len(tx), dtype=np.int64)
    for i, (x, y) in enumerate(zip(tx, ty)):
        x0, y0 = int(x) * s, int(y) * s
        left, right = max(x0, dx), min(x0 + s, w + dx)
        top, bottom = max(y0, dy), min(y0 + s, h + dy)
        if right <= left or bottom <= top:
            continue
        a = cur[top:bottom, left:right].astype(np.int64)
        b = prev[top-dy:bottom-dy, left-dx:right-dx].astype(np.int64)
        sums[i] = np.abs(a - b).sum(dtype=np.int64)
        counts[i] = (right - left) * (bottom - top)
    return sums, counts


def veto_flicker(s2_tiles, prev2_tiles, prev1_tiles, words_eq, grid,
                 gm_vector=None, y_prev2=None, y_prev1=None, y_cur=None,
                 params=None):
    """Return VetoDecision(mask bool[N], reasons uint8[N]); never mutate inputs.

    grid is the actual S2 tuple (s, gw, gh), not a router d48 object. Current
    tiles must be a 1-D change.TILE_DT array containing FINAL BASELINE S2 labels.
    Histories must have identical shape/dtype/Morton order, oldest first.
    words_eq is bool[N]. Luma must be integer[gh*s,gw*s] in [0,255].

    params is an eval evidence envelope, not visioncore.params.PARAMS:
      {"thresholds": {optional integer threshold overrides},
       "evidence": {"frame_ids": (t-2,t-1,t),
                    "history_valid": bool[N],
                    "hash_valid": bool[3,N]}}
    history_valid attests same stream/epoch/grid and owned immutable snapshots
    for all three frames. hash_valid attests each RGB hash was actually computed
    or validly inherited through exact words equality; zero is a possible value,
    never a sentinel. The helper cannot verify a caller's provenance assertion.
    Missing evidence defaults to abstention, not implied eager-hash validity.

    Malformed current arrays/grid/parameter envelopes raise TypeError/ValueError.
    Missing, malformed, discontinuous, or invalid history/luma/GM evidence yields
    false candidate bits and explicit reasons. Exact trusted lag-two hash returns
    are preserved before evaluating negative evidence. P2 is independent of GM
    when GM is absent; an explicitly inconsistent/nonconsensual GM request causes
    conservative abstention, including from P2. No ClassState/tracker is advanced.
    """
    if not isinstance(s2_tiles, np.ndarray) or s2_tiles.dtype != change.TILE_DT or s2_tiles.ndim != 1:
        raise TypeError("s2_tiles must be one-dimensional TILE_DT")
    if not isinstance(grid, (tuple, list)) or len(grid) != 3 or not all(_integer(v) for v in grid):
        raise TypeError("grid must be integer (s, gw, gh)")
    s, gw, gh = (int(v) for v in grid)
    if not (16 <= s <= 128 and s % 8 == 0 and 1 <= gw <= 255 and 1 <= gh <= 255):
        raise ValueError("unsupported grid; s is a multiple of 8 in [16,128]")
    n, nn = len(s2_tiles), s * s
    if n != gw * gh:
        raise ValueError("grid and tile count disagree")
    if not isinstance(words_eq, np.ndarray) or words_eq.dtype != np.bool_ or words_eq.shape != (n,):
        raise TypeError("words_eq must be bool[N]")
    tx, ty = change._morton_xy(s2_tiles["morton"])
    if (tx >= gw).any() or (ty >= gh).any() or len(np.unique(s2_tiles["morton"])) != n:
        raise ValueError("Morton coordinates must cover the grid exactly once")
    if params is None:
        params = {}
    if not isinstance(params, dict) or set(params) - {"thresholds", "evidence"}:
        raise ValueError("eval envelope accepts only thresholds and evidence")
    overrides = params.get("thresholds", {})
    if not isinstance(overrides, dict) or set(overrides) - set(_THRESHOLDS):
        raise ValueError("unknown veto threshold")
    p = {key: core_params.PARAMS[key] for key in _THRESHOLDS}
    p.update(overrides)
    for key, value in p.items():
        lo, hi = core_params.BOUNDS[key]
        if not _integer(value) or not lo <= int(value) <= hi:
            raise ValueError("threshold must be an integer within core bounds: " + key)
        p[key] = int(value)
    enter = p["T0_Q4"] * (1000 + p["HYST_BAND_PERMILLE"]) // 1000
    exit_ = p["T0_Q4"] * (1000 - p["HYST_BAND_PERMILLE"]) // 1000

    # PASS 1: gather immutable evidence. No label arbitration or offer/best state.
    candidate = (s2_tiles["cls"] == change.C_FLICKER) & ~words_eq
    mask = np.zeros(n, dtype=np.bool_)
    reasons = np.full(n, Reason.NOT_BASELINE_FLICKER, dtype=np.uint8)
    reasons[(s2_tiles["cls"] == change.C_FLICKER) & words_eq] = Reason.WORDS_EQUAL

    def abstain(which, reason):
        reasons[which] = reason
        return VetoDecision(mask, reasons)

    if not candidate.any():
        return VetoDecision(mask, reasons)
    if prev2_tiles is None or prev1_tiles is None:
        return abstain(candidate, Reason.MISSING_HISTORY)
    for previous in (prev2_tiles, prev1_tiles):
        if (not isinstance(previous, np.ndarray) or previous.dtype != change.TILE_DT
                or previous.shape != s2_tiles.shape
                or not np.array_equal(previous["morton"], s2_tiles["morton"])):
            return abstain(candidate, Reason.INVALID_HISTORY)
    ev = params.get("evidence", {})
    if not isinstance(ev, dict):
        return abstain(candidate, Reason.UNTRUSTED_HISTORY)
    ids = ev.get("frame_ids")
    hv = ev.get("history_valid")
    hashes_valid = ev.get("hash_valid")
    if (not isinstance(ids, (tuple, list)) or len(ids) != 3
            or not all(_integer(v) and int(v) >= 0 for v in ids)
            or tuple(int(v) for v in ids) != tuple(range(int(ids[0]), int(ids[0]) + 3))
            or not isinstance(hv, np.ndarray) or hv.dtype != np.bool_ or hv.shape != (n,)):
        return abstain(candidate, Reason.UNTRUSTED_HISTORY)
    reasons[candidate] = Reason.UNTRUSTED_HISTORY
    ready = candidate & hv
    if (not isinstance(hashes_valid, np.ndarray) or hashes_valid.dtype != np.bool_
            or hashes_valid.shape != (3, n)):
        return abstain(ready, Reason.UNTRUSTED_HASH)
    reasons[ready] = Reason.UNTRUSTED_HASH
    trusted = hv & hashes_valid.all(axis=0)
    ready &= trusted
    padded = ((s2_tiles["flags"] | prev1_tiles["flags"] | prev2_tiles["flags"])
              & desc.F_PAD) != 0
    reasons[ready & padded] = Reason.PADDED_TILE
    ready &= ~padded
    exact_return = ready & (s2_tiles["xh64"] == prev2_tiles["xh64"])
    reasons[exact_return] = Reason.EXACT_HASH_RETURN
    ready &= ~exact_return
    if not ready.any():
        return VetoDecision(mask, reasons)
    distinct = ((s2_tiles["xh64"] != prev1_tiles["xh64"])
                & (prev1_tiles["xh64"] != prev2_tiles["xh64"])
                & (s2_tiles["xh64"] != prev2_tiles["xh64"]))
    reasons[ready & ~distinct] = Reason.NOT_THREE_DISTINCT
    ready &= distinct
    planes = (y_prev2, y_prev1, y_cur)
    if any(y is None for y in planes):
        return abstain(ready, Reason.MISSING_LUMA)
    if any(not isinstance(y, np.ndarray) or y.dtype.kind not in "iu"
           or y.shape != (gh * s, gw * s) or (y < 0).any() or (y > 255).any()
           for y in planes):
        return abstain(ready, Reason.INVALID_LUMA)
    # Values were range-checked before conversion. In particular uint64 + int32
    # would promote inside native sig_conf; canonical int16 keeps all arithmetic
    # integral and also supports readonly signed/unsigned luma without mutation.
    planes = tuple(y.astype(np.int16, copy=False) for y in planes)
    y_prev2, y_prev1, y_cur = planes
    conf = [change.sig_conf(y, s, gw, gh, s2_tiles["morton"], p["SIG_DEAD_Q4"])
            for y in planes]
    r1 = change.residual(s2_tiles, prev1_tiles, p["H_DEAD"], conf[2] | conf[1])
    r2 = change.residual(s2_tiles, prev2_tiles, p["H_DEAD"], conf[2] | conf[0])
    descriptor_return = (r1 > enter) & (r2 <= exit_)
    reasons[ready & ~descriptor_return] = Reason.NOT_DESCRIPTOR_RETURN
    ready &= descriptor_return
    sad1, full1 = _window_sads(y_cur, y_prev1, (0, 0), s, tx, ty)
    sad2, full2 = _window_sads(y_cur, y_prev2, (0, 0), s, tx, ty)
    sad01, full01 = _window_sads(y_prev1, y_prev2, (0, 0), s, tx, ty)
    p2 = ((full1 == nn) & (full2 == nn) & (full01 == nn)
          & (16 * sad2 > nn * exit_)
          & (16 * sad1 > nn * enter) & (16 * sad01 > nn * enter))
    reasons[ready] = Reason.PIXEL_RETURN_OR_SMALL_CHANGE
    f6 = np.zeros(n, dtype=np.bool_)
    has_gm = gm_vector is not None
    if has_gm:
        if (not isinstance(gm_vector, (tuple, list)) or len(gm_vector) != 2
                or not all(_integer(v) for v in gm_vector)):
            return abstain(ready, Reason.INCONSISTENT_GM)
        gm_vector = tuple(int(v) for v in gm_vector)
        has_gm = gm_vector != (0, 0)
    if has_gm:
        if (max(abs(v) for v in gm_vector) > min(s, change.MOTION_D_MAX)
                or change.global_vector(y_cur, y_prev1, s) != gm_vector):
            return abstain(ready, Reason.INCONSISTENT_GM)
        shifted, counts = _window_sads(y_cur, y_prev1, gm_vector, s, tx, ty)
        informative = trusted & ~padded & (counts == nn) & (sad1 > 0)
        support = (informative & (shifted == 0)
                   & (p["MV_SAD_RATIO_DEN"] * shifted < sad1)
                   & (16 * shifted <= nn * exit_))
        # All labels can establish evidence, including provisional/exact-return
        # FLICKER. This does not reuse native offer()'s mutable winner mask.
        count_support, count_info = int(support.sum()), int(informative.sum())
        if (count_support < 3 or count_info == 0
                or count_support * 1000000 < p["GLOBAL_SHARE_PPM"] * count_info):
            return abstain(ready, Reason.NO_GM_CONSENSUS)
        lookup = {(int(x), int(y)): i for i, (x, y) in enumerate(zip(tx, ty))}
        dx, dy = gm_vector
        for i in np.flatnonzero(ready):
            if counts[i] == nn:
                continue
            reasons[i] = Reason.PARTIAL_WINDOW
            if not (0 < counts[i] < nn and 2 * counts[i] >= nn and shifted[i] == 0):
                continue
            if p["MV_SAD_RATIO_DEN"] * shifted[i] * nn >= sad1[i] * counts[i]:
                continue
            x, y = int(tx[i]), int(ty[i])
            sx, sy = x * s - dx, y * s - dy
            inward = []
            if sx < 0:
                inward.append((x + 1, y))
            if sx + s > gw * s:
                inward.append((x - 1, y))
            if sy < 0:
                inward.append((x, y + 1))
            if sy + s > gh * s:
                inward.append((x, y - 1))
            reasons[i] = Reason.NO_INWARD_SUPPORT
            f6[i] = any(xy in lookup and support[lookup[xy]] for xy in inward)

    # PASS 2: decide only on the frozen baseline candidate set. P1 already kept
    # exact returns. F6 wins the reason-code tie with P2; both demote to CHANGED.
    mask[:] = ready & (p2 | f6)
    reasons[ready & p2] = Reason.VETO_P2
    reasons[ready & f6] = Reason.VETO_F6
    assert not (mask & ~candidate).any()
    return VetoDecision(mask, reasons)

