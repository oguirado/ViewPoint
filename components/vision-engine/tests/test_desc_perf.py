"""S1 performance rewrite tests (RPERF-1): the tile-major describe_padded (+ prev_tiles fast path) is byte-identical to
the pre-rewrite implementation kept below as _describe_reference, on every synthetic scenario; budget check (VQA_PERF=0 skips)."""
import hashlib
import os
import statistics
import time
import unittest

import numpy as np

from visioncore import desc
from visioncore.desc import F_EDGE, F_PAD, F_SAT, F_TEXTURE, TILE_DTYPE, isqrt, xh64_of, ycocg
from visioncore.errors import VqaError
from visioncore.grid import Grid
from visioncore.params import PARAMS
from vadapt.synth.synthetic import SCENARIOS, render_scenario


def _describe_reference(padded, grid, words_prev=None, prev_xh64=None, p=None):
    """padded = grid.pad(frame). words_prev = previous padded frame (None: every tile counts as differing).
    prev_xh64 = previous tiles['xh64'] (None: hash every tile). cls/resid/mv fields are left 0 for S2."""
    p = PARAMS if p is None else p
    s, gw, gh, N = grid.s, grid.gw, grid.gh, grid.N
    n = s * s
    if padded.shape != (gh * s, gw * s, 3):
        raise VqaError("E_VC_FRAME_SHAPE", {"shape": list(padded.shape), "grid": grid.ident})
    flat = grid.flat
    P = padded.reshape(gh, s, gw, s, 3)
    y, co, cg = ycocg(P)
    y64 = y.astype(np.int64)
    sum_y = y64.sum(axis=(1, 3))
    sum_y2 = (y64 * y64).sum(axis=(1, 3))
    sum_co = co.sum(axis=(1, 3), dtype=np.int64)
    sum_cg = cg.sum(axis=(1, 3), dtype=np.int64)

    gx = np.zeros(y.shape, dtype=np.int16)
    gy = np.zeros(y.shape, dtype=np.int16)
    gx[:, :, :, :-1] = np.abs(y[:, :, :, 1:] - y[:, :, :, :-1])
    gy[:, :-1, :, :] = np.abs(y[:, 1:, :, :] - y[:, :-1, :, :])
    gsum = gx + gy
    grad_sum = gsum.sum(axis=(1, 3), dtype=np.int64)
    e = (gsum > p["TAU_EDGE"]).sum(axis=(1, 3), dtype=np.int64)
    sat = ((P == 0) | (P == 255)).any(axis=4).sum(axis=(1, 3), dtype=np.int64)

    b = s // 8
    sub = y64.reshape(gh, 8, b, gw, 8, b).sum(axis=(2, 5)).transpose(0, 2, 1, 3)  # (gh,gw,8,8)
    bits = (sub * n > sum_y[:, :, None, None] * (b * b)).reshape(gh, gw, 64)
    weights = np.uint64(1) << np.arange(64, dtype=np.uint64)
    sig64 = np.bitwise_or.reduce(bits.astype(np.uint64) * weights, axis=2)

    out = np.zeros(N, dtype=TILE_DTYPE)
    out["morton"] = grid.mortons
    flags = np.zeros(N, dtype=np.uint8)
    e_f = e.reshape(-1)[flat]
    flags |= np.where((2 * e_f >= s) & (e_f < 4 * s), F_EDGE, 0).astype(np.uint8)
    flags |= np.where(e_f >= 4 * s, F_TEXTURE, 0).astype(np.uint8)
    flags |= np.where(4 * sat.reshape(-1)[flat] >= n, F_SAT, 0).astype(np.uint8)
    flags |= np.where(grid.pad_flags(), F_PAD, 0).astype(np.uint8)
    out["flags"] = flags
    out["y_q4"] = (16 * sum_y // n).reshape(-1)[flat]
    out["co_h"] = np.clip(sum_co // (2 * n), -128, 127).reshape(-1)[flat]
    out["cg_h"] = np.clip(sum_cg // (2 * n), -128, 127).reshape(-1)[flat]
    sigma = isqrt(n * sum_y2 - sum_y * sum_y)
    out["sig_q4"] = (16 * sigma // n).reshape(-1)[flat]
    out["grad_q6"] = (64 * grad_sum // n).reshape(-1)[flat]
    out["edge"] = (255 * e // n).reshape(-1)[flat]
    out["sig64"] = sig64.reshape(-1)[flat]

    if words_prev is None:
        words_eq = np.zeros(N, dtype=bool)
    else:
        eq = (padded == words_prev).reshape(gh, s, gw, s, 3).all(axis=(1, 3, 4))
        words_eq = eq.reshape(-1)[flat]
    xh = np.zeros(N, dtype=np.uint64)
    if prev_xh64 is not None:
        xh[words_eq] = np.asarray(prev_xh64, dtype=np.uint64)[words_eq]
    for k in np.nonzero(~words_eq if prev_xh64 is not None else np.ones(N, dtype=bool))[0]:
        ty, tx = int(grid.ty[k]), int(grid.tx[k])
        xh[k] = xh64_of(padded[ty * s:(ty + 1) * s, tx * s:(tx + 1) * s])
    out["xh64"] = xh
    return out, words_eq


def _run(frames, grid, fast, prev_xh_mode):
    pp = px = pt = None
    outs = []
    for f in frames:
        p = grid.pad(f)
        if fast:
            t, eq = desc.describe_padded(p, grid, pp, px, None, pt)
        else:
            t, eq = desc.describe_padded(p, grid, pp, px)
        outs.append((t.tobytes(), eq.tobytes()))
        pp, pt = p, t
        px = t["xh64"] if prev_xh_mode else None
    return outs


def _ref(frames, grid):
    pp = px = None
    outs = []
    for f in frames:
        p = grid.pad(f)
        t, eq = _describe_reference(p, grid, pp, px)
        outs.append((t.tobytes(), eq.tobytes()))
        pp, px = p, t["xh64"]
    return outs


class TestDescPerfEquiv(unittest.TestCase):
    def _check(self, W, H, n):
        g = Grid(W, H)
        for name in SCENARIOS:
            fr, _, _ = render_scenario(name, 1, W, H, n)
            ref = _ref(fr, g)
            self.assertEqual(_run(fr, g, False, True), ref, name)
            self.assertEqual(_run(fr, g, True, True), ref, name + " prev_tiles")

    def test_equiv_small(self):
        self._check(256, 160, 24)

    def test_equiv_720p(self):
        self._check(1280, 720, 16)

    def test_equiv_padded_grid(self):
        g = Grid(300, 170)  # edge tiles are replicated
        rs = np.random.RandomState(4)
        a = rs.randint(0, 256, (170, 300, 3)).astype(np.uint8)
        b = a.copy()
        b[40:70, 100:140] ^= 255
        pa, pb = g.pad(a), g.pad(b)
        ta, _ = desc.describe_padded(pa, g)
        r, _ = _describe_reference(pb, g, pa, ta["xh64"])
        f, eq = desc.describe_padded(pb, g, pa, None, None, ta)
        self.assertEqual(r.tobytes(), f.tobytes())
        self.assertTrue(eq.any() and not eq.all())

    def test_prev_tiles_drops_s2_fields(self):
        g = Grid(256, 160)
        a = np.random.RandomState(1).randint(0, 256, (160, 256, 3)).astype(np.uint8)
        pa = g.pad(a)
        t, _ = desc.describe_padded(pa, g)
        t2 = t.copy()
        t2["cls"] = 3
        t2["resid"] = 9
        t2["flags"] |= 0x04  # SHADE_ONLY set by S2 must not leak into a copied descriptor
        f, eq = desc.describe_padded(pa, g, pa, None, None, t2)
        self.assertTrue(eq.all())
        self.assertEqual(f.tobytes(), t.tobytes())

    def test_sat_boundaries(self):
        g = Grid(256, 160)
        a = np.full((160, 256, 3), 128, np.uint8)
        a[0, 0] = (0, 128, 128)
        a[0, 1] = (128, 255, 128)
        a[0, 2] = (1, 254, 128)  # not saturated
        t, _ = desc.describe_padded(g.pad(a), g)
        r, _ = _describe_reference(g.pad(a), g)
        self.assertEqual(t.tobytes(), r.tobytes())


@unittest.skipIf(os.environ.get("VQA_PERF") == "0", "VQA_PERF=0")
class TestDescBudget(unittest.TestCase):
    def test_clean_static_720p_median(self):  # SPEC_INT §6 budget is S0-S4 median <= 15 ms @720p; S1 alone must fit
        W, H = 1280, 720
        fr, _, _ = render_scenario("clean_static", 1, W, H, 16)
        g = Grid(W, H)
        pp = pt = None
        ts = []
        for f in fr:
            p = g.pad(f)
            t0 = time.perf_counter()
            t, _ = desc.describe_padded(p, g, pp, None, None, pt)
            ts.append(time.perf_counter() - t0)
            pp, pt = p, t
        self.assertLessEqual(statistics.median(ts[1:]) * 1e3, 15.0)


if __name__ == "__main__":
    unittest.main()
