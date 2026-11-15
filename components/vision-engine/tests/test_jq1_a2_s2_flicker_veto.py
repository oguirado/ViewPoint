"""Offline S2 veto reference and regression witnesses; no production wiring.

The reference implements P1 preservation, P2 confirmed pixel nonreturn, and
F6 exposed-window evidence. D1/D2/D3 production alternatives are specified in
docs/spec/jq1_a2_s2_flicker_veto.md. Frames exist only in RAM. All arithmetic in
the reference is integer arithmetic; real S1/S2 are used to make the fixtures.
"""
from collections import Counter
from enum import IntEnum
from pathlib import Path
from typing import NamedTuple
import unittest

import numpy as np

from eval.gui_synth import raster, scenes
from eval.v2eval.postpass import apply_claims
from eval.v2x.fl_blinkprior import FlickerTracker
from visioncore import change, desc, params as core_params
from visioncore.grid import Grid


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

    params is a REFERENCE-ONLY envelope, not visioncore.params.PARAMS:
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
        raise ValueError("reference envelope accepts only thresholds and evidence")
    overrides = params.get("thresholds", {})
    if not isinstance(overrides, dict) or set(overrides) - set(_THRESHOLDS):
        raise ValueError("unknown reference threshold")
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


def _reference_params(n, frame_index):
    return {"evidence": {
        "frame_ids": (frame_index - 2, frame_index - 1, frame_index),
        "history_valid": np.ones(n, dtype=np.bool_),
        "hash_valid": np.ones((3, n), dtype=np.bool_),
    }}


def _records(frames, grid):
    """Real eager S1 + real S2; no changed class/hash fields, no stored images."""
    state = None
    prev_padded = None
    records = []
    for rgb in frames:
        padded = grid.pad(rgb)
        y = desc.luma_of_padded(padded)
        raw, equal = desc.describe_padded(padded, grid, words_prev=prev_padded,
                                          y_plane=y, lazy_xh=False)
        assert raw.dtype == change.TILE_DT
        if state is None:
            state = change.ClassState.init(raw)
            baseline, state, summary = change.classify_first(state, raw, y_cur=y)
        else:
            baseline, state, summary = change.classify_tiles(
                state, raw, equal, y_cur=y, grid=(grid.s, grid.gw, grid.gh))
        records.append({"raw": raw, "s2": baseline, "equal": equal,
                        "y": y, "summary": summary})
        prev_padded = padded
    return records


def _scroll_frames(n=48, step=4, cycle=7):
    """One fixed world sampled at x+4*t: physical displacement is (-4,0).

    Periodic interior deliberately triggers the D1 publication gap. The world
    beyond x=160 breaks exact return at newly exposed right-edge pixels.
    """
    for i in range(n):
        x = np.arange(160, dtype=np.int64)[None, :] + step * i
        y = np.arange(96, dtype=np.int64)[:, None]
        grey = (60 + 80 * ((x % (2 * step)) >= step) + y // 16
                + (x >= 160) * ((x // step) % cycle))
        yield np.repeat(grey[:, :, None], 3, axis=2).astype(np.uint8)


def _call(records, i, grid, gm=None, params=None):
    r = records[i]
    return veto_flicker(
        r["s2"], records[i-2]["raw"] if i >= 2 else None,
        records[i-1]["raw"] if i >= 1 else None, r["equal"],
        (grid.s, grid.gw, grid.gh), gm_vector=gm,
        y_prev2=records[i-2]["y"] if i >= 2 else None,
        y_prev1=records[i-1]["y"] if i >= 1 else None, y_cur=r["y"],
        params=_reference_params(grid.N, i) if params is None else params)


def _totals(records):
    return np.bincount(np.concatenate([r["s2"]["cls"] for r in records]), minlength=7).tolist()


class S2VetoReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.grid = Grid(160, 96)
        cls.scroll = _records(_scroll_frames(), cls.grid)
        cls.canvas_scene = {"kind": "canvas_anim", "w": 160, "h": 96,
                            "seed": 1, "boxes": [[0, 0, 160, 96]],
                            "glyph_runs": [], "scale": 1}
        cls.canvas = _records((raster.frame(cls.canvas_scene, i) for i in range(48)), cls.grid)
        cls.caret_scene = scenes.build("caret_blink", 0, w=160, h=96, fps=32, n=80)
        cls.caret = _records((raster.frame(cls.caret_scene, i) for i in range(80)), cls.grid)
        cls.aba = _records((raster.frame(cls.caret_scene, i) for i in (0, 8, 0)), cls.grid)

    def test_defaults_and_real_dtype(self):
        self.assertEqual(change.thresholds()[:2], (35, 28))
        self.assertEqual([core_params.PARAMS[k] for k in _THRESHOLDS[:4]], [32, 100, 2, 24])
        self.assertEqual(self.grid.s, 16)
        for records in (self.scroll, self.canvas, self.caret, self.aba):
            for r in records:
                self.assertEqual(r["s2"].dtype, change.TILE_DT)
                self.assertEqual(r["raw"].dtype, change.TILE_DT)
                self.assertEqual(r["y"].dtype, np.int16)

    def test_scroll_baseline_defect_and_f6_preconditions(self):
        self.assertEqual(_totals(self.scroll), [60, 0, 204, 1236, 1380, 0, 0])
        r = self.scroll[4]
        self.assertTrue(np.all(r["s2"]["cls"] == change.C_FLICKER))
        self.assertIsNone(r["summary"]["global_mv"])
        self.assertEqual(change.global_vector(r["y"], self.scroll[3]["y"], 16), (-4, 0))
        j = 49
        self.assertEqual((int(self.grid.tx[j]), int(self.grid.ty[j])), (9, 0))
        self.assertEqual([int(self.scroll[i]["raw"]["xh64"][j]) for i in (2, 3, 4)],
                         [11770060860217519983, 3944673549284345041, 14867038441355757329])
        cf = [change.sig_conf(self.scroll[i]["y"], 16, 10, 6, self.grid.mortons, 24)
              for i in (2, 3, 4)]
        self.assertEqual(int(change.residual(r["raw"], self.scroll[3]["raw"], 2, cf[2] | cf[1])[j]), 504)
        self.assertEqual(int(change.residual(r["raw"], self.scroll[2]["raw"], 2, cf[2] | cf[0])[j]), 8)
        sums, counts = _window_sads(r["y"], self.scroll[3]["y"], (-4, 0), 16,
                                     self.grid.tx, self.grid.ty)
        self.assertEqual((int(sums[j]), int(counts[j])), (0, 192))
        full = counts == 256
        self.assertEqual(int(full.sum()), 54)
        self.assertTrue(np.all(sums[full] == 0))
        # Preconditions and the actual baseline defect are pinned BEFORE veto.
        result = _call(self.scroll, 4, self.grid, (-4, 0))
        self.assertTrue(result.mask[j])
        self.assertEqual(result.reasons[j], Reason.VETO_F6)
        changed = r["s2"].copy()
        changed["cls"][result.mask] = change.C_CHANGED
        self.assertEqual(changed["cls"][j], change.C_CHANGED)

    def test_scroll_exact_counts_and_protected_returns(self):
        self.assertEqual(_totals(self.scroll), [60, 0, 204, 1236, 1380, 0, 0])
        after = np.zeros(7, dtype=np.int64)
        reasons = Counter()
        exact, exposed, vetoes = 0, 0, 0
        for i, r in enumerate(self.scroll):
            gm = None if i == 0 else change.global_vector(r["y"], self.scroll[i-1]["y"], 16)
            if i >= 2:
                candidate = (r["s2"]["cls"] == 4) & ~r["equal"]
                keep = candidate & (r["raw"]["xh64"] == self.scroll[i-2]["raw"]["xh64"])
                exact += int(keep.sum())
            result = _call(self.scroll, i, self.grid, gm)
            if i >= 2:
                self.assertFalse(result.mask[keep].any())
            out = r["s2"].copy()
            out["cls"][result.mask] = change.C_CHANGED
            self.assertTrue(np.array_equal(out[~result.mask], r["s2"][~result.mask]))
            after += np.bincount(out["cls"], minlength=7)
            vetoes += int(result.mask.sum())
            exposed += int(np.count_nonzero(result.reasons == Reason.VETO_F6))
            reasons.update(int(x) for x in result.reasons[result.mask])
        self.assertEqual(exact, 1008)
        self.assertEqual(vetoes, 372)
        self.assertEqual(exposed, 78)
        self.assertEqual(reasons, {Reason.VETO_P2: 294, Reason.VETO_F6: 78})
        self.assertEqual(after.tolist(), [60, 0, 576, 1236, 1008, 0, 0])

    def test_canvas_baseline_distinct_hashes_and_exact_counts(self):
        self.assertEqual(_totals(self.canvas), [60, 1178, 1306, 0, 169, 13, 154])
        hashes = np.stack([r["raw"]["xh64"] for r in self.canvas])
        for j in range(self.grid.N):
            self.assertEqual(len(np.unique(hashes[:, j])), 48)
        # Confirm actual renderer seed expression against real texture bytes.
        for i in (0, 1, 2, 47):
            self.assertTrue(np.array_equal(raster.frame(self.canvas_scene, i),
                                           raster.texture(160, 96, 1 ^ (i * 7919))))
        after = np.zeros(7, dtype=np.int64)
        vetoes = 0
        for i, r in enumerate(self.canvas):
            result = _call(self.canvas, i, self.grid)
            out = r["s2"].copy()
            out["cls"][result.mask] = change.C_CHANGED
            self.assertTrue(np.array_equal(out[~result.mask], r["s2"][~result.mask]))
            after += np.bincount(out["cls"], minlength=7)
            vetoes += int(result.mask.sum())
            self.assertTrue(np.all(result.reasons[result.mask] == Reason.VETO_P2))
        self.assertEqual(vetoes, 169)
        self.assertEqual(after.tolist(), [60, 1178, 1475, 0, 0, 13, 154])

    def test_exact_aba_remains_flicker(self):
        self.assertEqual([np.bincount(r["s2"]["cls"], minlength=7).tolist() for r in self.aba],
                         [[60, 0, 0, 0, 0, 0, 0], [59, 0, 1, 0, 0, 0, 0], [59, 0, 0, 0, 1, 0, 0]])
        self.assertEqual(np.flatnonzero(self.aba[2]["s2"]["cls"] == 4).tolist(), [15])
        self.assertEqual(self.aba[2]["raw"]["xh64"][15], self.aba[0]["raw"]["xh64"][15])
        result = _call(self.aba, 2, self.grid)
        self.assertFalse(result.mask.any())
        self.assertEqual(result.reasons[15], Reason.EXACT_HASH_RETURN)

    def test_all_caret_tracker_emissions_survive_arbitration(self):
        self.assertEqual(self.caret_scene["period"], 16)
        self.assertEqual(_totals(self.caret), [4791, 0, 9, 0, 0, 0, 0])
        tracker = FlickerTracker(self.grid.N)
        observed = []
        for i, r in enumerate(self.caret):
            states, halves = tracker.update(r["raw"]["xh64"])
            claim = states == change.C_FLICKER
            result = _call(self.caret, i, self.grid)
            self.assertFalse(result.mask.any())
            base = {k: r["s2"][k].astype(np.int64).copy() for k in ("cls", "mv_dx", "mv_dy")}
            base["cls"][result.mask] = change.C_CHANGED
            zeros = np.zeros(self.grid.N, dtype=np.int64)
            output = apply_claims(base, [{"mask": claim, "cls": change.C_FLICKER,
                                          "mv_dx": zeros, "mv_dy": zeros}])
            self.assertTrue(np.all(output["cls"][claim] == change.C_FLICKER))
            for j in np.flatnonzero(claim):
                self.assertEqual(r["s2"]["cls"][j], change.C_CHANGED)
                self.assertFalse(r["equal"][j])
                self.assertNotEqual(r["raw"]["xh64"][j], self.caret[i-2]["raw"]["xh64"][j])
                self.assertEqual(int(halves[j]), 8)
                observed.append((i, int(j)))
        self.assertEqual(observed, [(i, 15) for i in (24, 32, 40, 48, 56, 64, 72)])
        self.assertEqual(self.caret[24]["raw"]["xh64"][15], self.caret[8]["raw"]["xh64"][15])
        self.assertEqual(self.caret[22]["raw"]["xh64"][15], self.caret[0]["raw"]["xh64"][15])

    def test_apply_claims_order_proof_with_overlap(self):
        # Exhaust the two Boolean masks: a later FL declaration always wins.
        # This exercises the actual function, including deliberately overlapping
        # claims; the current chain's first-claim-wins masking is a different API.
        for veto in (False, True):
            for tracker in (False, True):
                with self.subTest(veto=veto, tracker=tracker):
                    base = {"cls": np.array([4], np.int64),
                            "mv_dx": np.array([0], np.int64), "mv_dy": np.array([0], np.int64)}
                    corrected = {k: v.copy() for k, v in base.items()}
                    corrected["cls"][np.array([veto])] = 2
                    claim = {"mask": np.array([tracker]), "cls": 4,
                             "mv_dx": np.array([0]), "mv_dy": np.array([0])}
                    out = apply_claims(corrected, [claim])
                    self.assertEqual(int(out["cls"][0]), 4 if tracker or not veto else 2)
                    self.assertEqual(int(base["cls"][0]), 4)

    def test_selection_mask_never_changes_other_classes_or_words_equal(self):
        r = self.scroll[4]
        self.assertTrue(np.all(r["s2"]["cls"] == 4))
        baseline = r["s2"].copy()
        baseline["cls"][:7] = np.arange(7, dtype=np.uint8)
        equal = r["equal"].copy()
        equal[49] = True
        result = veto_flicker(baseline, self.scroll[2]["raw"], self.scroll[3]["raw"], equal,
                              (16, 10, 6), (-4, 0), self.scroll[2]["y"], self.scroll[3]["y"],
                              r["y"], _reference_params(60, 4))
        eligible = (baseline["cls"] == 4) & ~equal
        self.assertFalse(result.mask[~eligible].any())
        self.assertEqual(result.reasons[49], Reason.WORDS_EQUAL)
        output = baseline.copy()
        output["cls"][result.mask] = change.C_CHANGED
        self.assertTrue(np.array_equal(output[~eligible], baseline[~eligible]))
        self.assertTrue(result.mask.any())

    def test_absent_history_and_unattested_arrays_abstain(self):
        r = self.scroll[4]
        args = (r["s2"], self.scroll[2]["raw"], self.scroll[3]["raw"], r["equal"], (16, 10, 6))
        result = veto_flicker(*args)
        self.assertFalse(result.mask.any())
        self.assertTrue(np.all(result.reasons == Reason.UNTRUSTED_HISTORY))
        for missing in (1, 2):
            a = list(args)
            a[missing] = None
            result = veto_flicker(*a, params=_reference_params(60, 4))
            self.assertFalse(result.mask.any())
            self.assertTrue(np.all(result.reasons == Reason.MISSING_HISTORY))

    def test_history_gaps_grid_mismatch_and_per_tile_invalidity(self):
        p = _reference_params(60, 4)
        p["evidence"]["frame_ids"] = (1, 3, 4)
        result = _call(self.scroll, 4, self.grid, (-4, 0), p)
        self.assertFalse(result.mask.any())
        self.assertTrue(np.all(result.reasons == Reason.UNTRUSTED_HISTORY))
        p = _reference_params(60, 4)
        p["evidence"]["history_valid"][49] = False
        result = _call(self.scroll, 4, self.grid, (-4, 0), p)
        self.assertFalse(result.mask[49])
        self.assertEqual(result.reasons[49], Reason.UNTRUSTED_HISTORY)
        r = self.scroll[4]
        result = veto_flicker(r["s2"], self.scroll[2]["raw"][::-1], self.scroll[3]["raw"],
                              r["equal"], (16, 10, 6), params=_reference_params(60, 4))
        self.assertFalse(result.mask.any())
        self.assertTrue(np.all(result.reasons == Reason.INVALID_HISTORY))

    def test_placeholder_zero_and_nonzero_hashes_need_validity(self):
        r = self.scroll[4]
        for value in (0, 123):
            with self.subTest(placeholder=value):
                tiles = [self.scroll[i]["raw"].copy() for i in (2, 3)] + [r["s2"].copy()]
                for tile in tiles:
                    tile["xh64"] = value
                p = _reference_params(60, 4)
                p["evidence"]["hash_valid"][:] = False
                result = veto_flicker(tiles[2], tiles[0], tiles[1], r["equal"], (16, 10, 6),
                                      (-4, 0), self.scroll[2]["y"], self.scroll[3]["y"], r["y"], p)
                self.assertFalse(result.mask.any())
                self.assertTrue(np.all(result.reasons == Reason.UNTRUSTED_HASH))
        # Adversarial protocol-value test, NOT a claim to have found a zero-valued
        # BLAKE2 hash: validity, not hash != 0, controls trust in the return value.
        tiles[0]["xh64"][:] = 0
        tiles[2]["xh64"][:] = 0
        result = veto_flicker(tiles[2], tiles[0], tiles[1], r["equal"], (16, 10, 6),
                              params=_reference_params(60, 4))
        self.assertFalse(result.mask.any())
        self.assertTrue(np.all(result.reasons == Reason.EXACT_HASH_RETURN))

    def test_lazy_classstate_and_stale_jitter_hash_defect_witness(self):
        grid = Grid(32, 32)
        prev_padded = prev_raw = prev_hash = state = None
        outputs, eager = [], []
        for value in (64, 65):
            rgb = np.full((32, 32, 3), value, dtype=np.uint8)
            padded = grid.pad(rgb)
            y = desc.luma_of_padded(padded)
            raw, equal = desc.describe_padded(padded, grid, prev_padded, prev_hash,
                                              prev_tiles=prev_raw, y_plane=y, lazy_xh=True)
            self.assertTrue(np.all(raw["xh64"] == 0))
            if state is None:
                state = change.ClassState.init(raw)
                out, state, _ = change.classify_first(state, raw, y_cur=y)
            else:
                out, state, _ = change.classify_tiles(state, raw, equal, y_cur=y, grid=(16, 2, 2))
            out = out.copy()
            desc.fill_xh64(out, padded, grid, prev_hash, hash_all=prev_hash is None)
            eager.append(desc.describe_padded(padded, grid, lazy_xh=False)[0])
            outputs.append(out)
            prev_padded, prev_raw, prev_hash = padded, raw, out["xh64"]
        self.assertTrue(np.all(outputs[1]["cls"] == change.C_JITTER))
        self.assertTrue(np.all(eager[1]["xh64"] != eager[0]["xh64"]))
        self.assertTrue(np.array_equal(outputs[1]["xh64"], outputs[0]["xh64"]))
        self.assertTrue(np.all(state.prev1["xh64"] == 0))
        self.assertTrue(np.all(state.prev2["xh64"] == 0))

    def test_missing_and_malformed_luma(self):
        r = self.scroll[4]
        args = (r["s2"], self.scroll[2]["raw"], self.scroll[3]["raw"], r["equal"], (16, 10, 6))
        nonreturn = r["raw"]["xh64"] != self.scroll[2]["raw"]["xh64"]
        for plane, reason in ((None, Reason.MISSING_LUMA),
                              (self.scroll[2]["y"][:-1], Reason.INVALID_LUMA),
                              (self.scroll[2]["y"].astype(np.float64), Reason.INVALID_LUMA)):
            result = veto_flicker(*args, y_prev2=plane, y_prev1=self.scroll[3]["y"],
                                  y_cur=r["y"], params=_reference_params(60, 4))
            self.assertFalse(result.mask.any())
            self.assertTrue(np.all(result.reasons[nonreturn] == reason))

    def test_inconsistent_gm_abstains_even_when_p2_would_fire(self):
        r = self.scroll[4]
        nonreturn = r["raw"]["xh64"] != self.scroll[2]["raw"]["xh64"]
        for gm in ((0, 4), (-17, 0), (False, 0), (1,)):
            result = _call(self.scroll, 4, self.grid, gm)
            self.assertFalse(result.mask.any())
            self.assertTrue(np.all(result.reasons[nonreturn] == Reason.INCONSISTENT_GM))

    def test_padded_tiles_abstain(self):
        r = self.scroll[4]
        tiles = r["s2"].copy()
        tiles["flags"][49] |= desc.F_PAD
        result = veto_flicker(tiles, self.scroll[2]["raw"], self.scroll[3]["raw"], r["equal"],
                              (16, 10, 6), (-4, 0), self.scroll[2]["y"], self.scroll[3]["y"],
                              r["y"], _reference_params(60, 4))
        self.assertFalse(result.mask[49])
        self.assertEqual(result.reasons[49], Reason.PADDED_TILE)

    def test_partial_windows_have_real_counts_and_no_wrapping(self):
        y = np.arange(32 * 32, dtype=np.int64).reshape(32, 32) % 256
        for vector, expected in (((-4, 0), 192), ((-12, 0), 64), ((-16, 0), 0), ((-32, 0), 0)):
            sums, counts = _window_sads(y, y, vector, 16, np.array([1]), np.array([0]))
            self.assertEqual(int(counts[0]), expected)
            if expected == 0:
                self.assertEqual(int(sums[0]), 0)
                self.assertFalse(0 < counts[0] < 256 and 2 * counts[0] >= 256)

    def test_f6_independent_of_p2_and_requires_inward_support(self):
        records = _records(_scroll_frames(n=5, cycle=3), self.grid)
        r, j = records[4], 49
        self.assertTrue(np.all(r["s2"]["cls"] == change.C_FLICKER))
        self.assertEqual(len({int(records[i]["raw"]["xh64"][j]) for i in (2, 3, 4)}), 3)
        sad2, _ = _window_sads(r["y"], records[2]["y"], (0, 0), 16,
                              self.grid.tx, self.grid.ty)
        self.assertEqual(16 * int(sad2[j]), 256 * 20)
        self.assertLessEqual(16 * int(sad2[j]), 256 * 28)
        without_gm = _call(records, 4, self.grid)
        self.assertFalse(without_gm.mask[j])
        self.assertEqual(without_gm.reasons[j], Reason.PIXEL_RETURN_OR_SMALL_CHANGE)
        with_gm = _call(records, 4, self.grid, (-4, 0))
        self.assertTrue(with_gm.mask[j])
        self.assertEqual(with_gm.reasons[j], Reason.VETO_F6)
        p = _reference_params(60, 4)
        self.assertEqual((int(self.grid.tx[48]), int(self.grid.ty[48])), (8, 0))
        p["evidence"]["history_valid"][48] = False
        unsupported = _call(records, 4, self.grid, (-4, 0), p)
        self.assertFalse(unsupported.mask[j])
        self.assertEqual(unsupported.reasons[j], Reason.NO_INWARD_SUPPORT)
        frames = list(_scroll_frames(n=5, cycle=3))
        frames[4][0, 145, :] += np.uint8(1)
        noisy = _records(frames, self.grid)
        self.assertEqual(noisy[4]["s2"]["cls"][j], change.C_FLICKER)
        self.assertEqual(change.global_vector(noisy[4]["y"], noisy[3]["y"], 16), (-4, 0))
        shifted, counts = _window_sads(noisy[4]["y"], noisy[3]["y"], (-4, 0),
                                       16, self.grid.tx, self.grid.ty)
        self.assertEqual((int(shifted[j]), int(counts[j])), (1, 192))
        clipped_mismatch = _call(noisy, 4, self.grid, (-4, 0))
        self.assertFalse(clipped_mismatch.mask[j])
        self.assertEqual(clipped_mismatch.reasons[j], Reason.PARTIAL_WINDOW)

    def test_real_partial_window_below_half_abstains(self):
        records = _records(_scroll_frames(n=3, step=12), self.grid)
        r, j = records[2], 49
        self.assertEqual(np.bincount(r["s2"]["cls"], minlength=7).tolist(), [0, 0, 0, 6, 54, 0, 0])
        self.assertEqual(r["s2"]["cls"][j], change.C_FLICKER)
        self.assertEqual(len({int(q["raw"]["xh64"][j]) for q in records}), 3)
        self.assertEqual(change.global_vector(r["y"], records[1]["y"], 16), (-12, 0))
        sad, count = _window_sads(r["y"], records[1]["y"], (-12, 0), 16,
                                  self.grid.tx, self.grid.ty)
        self.assertEqual((int(sad[j]), int(count[j])), (0, 64))
        result = _call(records, 2, self.grid, (-12, 0))
        self.assertFalse(result.mask.any())
        self.assertEqual(result.reasons[j], Reason.PARTIAL_WINDOW)

    def test_too_few_trusted_gm_supporters_abstain(self):
        p = _reference_params(60, 4)
        p["evidence"]["history_valid"][:] = False
        p["evidence"]["history_valid"][[0, 1, 49]] = True
        result = _call(self.scroll, 4, self.grid, (-4, 0), p)
        self.assertFalse(result.mask.any())
        self.assertEqual(result.reasons[49], Reason.NO_GM_CONSENSUS)

    def test_hash_inequality_alone_does_not_authorize_veto(self):
        grid = Grid(32, 32)
        frames = [np.full((32, 32, 3), 64, np.uint8),
                  np.full((32, 32, 3), 200, np.uint8),
                  np.broadcast_to(np.array([65, 64, 63], np.uint8), (32, 32, 3)).copy()]
        records = _records(frames, grid)
        self.assertTrue(np.all(records[2]["s2"]["cls"] == change.C_FLICKER))
        self.assertTrue(np.all(records[2]["raw"]["xh64"] != records[0]["raw"]["xh64"]))
        self.assertTrue(np.array_equal(records[2]["y"], records[0]["y"]))
        result = _call(records, 2, grid)
        self.assertFalse(result.mask.any())
        self.assertTrue(np.all(result.reasons == Reason.PIXEL_RETURN_OR_SMALL_CHANGE))

    def test_all_integer_luma_dtypes_match_without_mutation(self):
        r = self.scroll[4]
        expected = _call(self.scroll, 4, self.grid, (-4, 0))
        for dtype in (np.uint8, np.int8, np.uint16, np.int16, np.uint32, np.int32, np.uint64, np.int64):
            # int8 cannot hold this fixture's full [0,255] range: its negative
            # wrapped values must abstain. All other listed dtypes represent it.
            with self.subTest(dtype=dtype):
                planes = [self.scroll[i]["y"].astype(dtype) for i in (2, 3, 4)]
                before = [y.tobytes() for y in planes]
                result = veto_flicker(r["s2"], self.scroll[2]["raw"], self.scroll[3]["raw"],
                                      r["equal"], (16, 10, 6), (-4, 0), *planes,
                                      _reference_params(60, 4))
                if dtype == np.int8:
                    self.assertFalse(result.mask.any())
                    self.assertEqual(result.reasons[49], Reason.INVALID_LUMA)
                else:
                    self.assertTrue(np.array_equal(result.mask, expected.mask))
                    self.assertTrue(np.array_equal(result.reasons, expected.reasons))
                self.assertEqual(before, [y.tobytes() for y in planes])

    def test_no_mutation_readonly_views_and_repeatability(self):
        r = self.scroll[4]
        original = [r["s2"], self.scroll[2]["raw"], self.scroll[3]["raw"], r["equal"],
                    self.scroll[2]["y"], self.scroll[3]["y"], r["y"]]
        arrays = [a.copy() for a in original]
        before = [a.tobytes() for a in arrays]
        for a in arrays:
            a.flags.writeable = False
        p = _reference_params(60, 4)
        ev_before = [p["evidence"][k].tobytes() for k in ("history_valid", "hash_valid")]
        for key in ("history_valid", "hash_valid"):
            p["evidence"][key].flags.writeable = False
        results = [veto_flicker(*arrays[:4], (16, 10, 6), (-4, 0), *arrays[4:], p) for _ in range(2)]
        self.assertEqual(results[0].mask.dtype, np.bool_)
        self.assertEqual(results[0].reasons.dtype, np.uint8)
        self.assertEqual(results[0].mask.shape, (60,))
        self.assertEqual(results[0].reasons.shape, (60,))
        self.assertTrue(np.array_equal(results[0].mask, results[1].mask))
        self.assertTrue(np.array_equal(results[0].reasons, results[1].reasons))
        self.assertFalse(np.shares_memory(results[0].mask, results[1].mask))
        self.assertEqual(before, [a.tobytes() for a in arrays])
        self.assertEqual(ev_before, [p["evidence"][k].tobytes() for k in ("history_valid", "hash_valid")])

    def test_current_contract_rejects_bad_types_and_float_thresholds(self):
        r = self.scroll[4]
        args = [r["s2"], self.scroll[2]["raw"], self.scroll[3]["raw"], r["equal"], (16, 10, 6)]
        with self.assertRaises(TypeError):
            veto_flicker(r["s2"].tolist(), *args[1:])
        with self.assertRaises(TypeError):
            veto_flicker(*args[:3], r["equal"].astype(np.uint8), args[4])
        with self.assertRaises(ValueError):
            veto_flicker(*args, params={"thresholds": {"T0_Q4": 32.0}})
        with self.assertRaises(ValueError):
            veto_flicker(*args, params=core_params.PARAMS)

    def test_specification_exists_and_is_nonempty(self):
        path = Path(__file__).resolve().parents[1] / "docs/spec/jq1_a2_s2_flicker_veto.md"
        self.assertTrue(path.is_file(), str(path))
        self.assertTrue(path.read_text(encoding="utf-8").strip())


if __name__ == "__main__":
    unittest.main()
