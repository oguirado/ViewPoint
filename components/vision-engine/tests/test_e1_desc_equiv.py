"""E1 desc equivalence (RPERF-1/6, W2 lane C1): the BASE_E (fc899e4) describe_padded/_compute are kept below as the
reference; the current desc.py must give byte-equal tiles on every synth scenario, with and without the luma plane and
the prev_tiles fast path; luma_of_padded must equal change.luma_padded. Perf is logged only (VQA_PERF_LOG=1), never asserted."""
import hashlib
import os
import statistics
import time
import unittest

import numpy as np

from visioncore import change, desc
from visioncore.desc import F_EDGE, F_PAD, F_SAT, F_TEXTURE, TILE_DTYPE, isqrt, ycocg
from visioncore.errors import VqaError
from visioncore.grid import Grid
from visioncore.params import PARAMS
from vadapt.synth.synthetic import SCENARIOS, render_scenario


def ref_describe_padded(padded, grid, words_prev=None, prev_xh64=None, p=None, prev_tiles=None):  
    """padded = grid.pad(frame). words_prev = previous padded frame (None: every tile counts as differing).
    prev_xh64 = previous tiles['xh64'] (None: hash every tile). prev_tiles = previous describe output: with
    words_prev, byte-equal tiles copy their descriptor verbatim (exact: equal bytes give equal descriptors) and only
    the other tiles are computed. cls/resid/mv fields are left 0 for S2. Tile-major: each tile is one contiguous block."""
    p = PARAMS if p is None else p
    s, gw, gh, N = grid.s, grid.gw, grid.gh, grid.N
    n = s * s
    if padded.shape != (gh * s, gw * s, 3):
        raise VqaError("E_VC_FRAME_SHAPE", {"shape": list(padded.shape), "grid": grid.ident})
    out = np.zeros(N, dtype=TILE_DTYPE)
    out["morton"] = grid.mortons
    if words_prev is None:
        words_eq = np.zeros(N, dtype=bool)
    else:
        eq = (padded == words_prev).reshape(gh, s, gw, s * 3).all(axis=(1, 3))
        words_eq = eq.reshape(-1)[grid.flat]
    reuse = prev_tiles is not None and words_prev is not None
    if prev_xh64 is None and reuse:
        prev_xh64 = prev_tiles["xh64"]
    idx = np.nonzero(~words_eq)[0] if reuse else np.arange(N)
    if reuse:
        keep = np.nonzero(words_eq)[0]
        for f in desc._COPY_FIELDS:
            out[f][keep] = prev_tiles[f][keep]
        out["flags"][keep] = prev_tiles["flags"][keep] & desc._DESC_FLAGS
    if len(idx):
        _ref_compute(out, idx, padded, grid, p)
    xh = np.zeros(N, dtype=np.uint64)
    if prev_xh64 is not None:
        xh[words_eq] = np.asarray(prev_xh64, dtype=np.uint64)[words_eq]
        hidx = np.nonzero(~words_eq)[0]
    else:
        hidx = np.arange(N)
    if len(hidx):
        xh[hidx] = _ref_hash_tiles(padded, grid, hidx)
    out["xh64"] = xh
    return out, words_eq



def _ref_gather(padded, grid, idx):
    """-> uint8[M, s, s, 3], contiguous tile blocks for Morton ranks idx."""
    s = grid.s
    P = padded.reshape(grid.gh, s, grid.gw, s, 3)
    return P[grid.ty[idx], :, grid.tx[idx], :, :]


def _ref_hash_tiles(padded, grid, idx):
    T = _ref_gather(padded, grid, idx)
    rows = T.reshape(len(idx), -1)
    dig = b"".join(hashlib.blake2b(rows[k], digest_size=8, person=b"vqa-tile").digest() for k in range(len(idx)))
    return np.frombuffer(dig, dtype="<u8")


def _ref_compute(out, idx, padded, grid, p):
    """Fill the perceptual fields of out[idx] (int32 accumulators while 2**14 * 255**2 fits: s <= 128)."""
    s = grid.s
    n = s * s
    M = len(idx)
    acc = np.int32 if s <= 128 else np.int64
    T = _ref_gather(padded, grid, idx)
    r8, g8, b8 = T[..., 0], T[..., 1], T[..., 2]
    y, co, cg = ycocg(T)                                  # (M, s, s) int16
    sum_y = y.reshape(M, n).sum(axis=1, dtype=acc).astype(np.int64)
    y32 = y.astype(acc)
    sum_y2 = np.einsum("mi,mi->m", y32.reshape(M, n), y32.reshape(M, n)).astype(np.int64)
    sum_co = co.reshape(M, n).sum(axis=1, dtype=acc).astype(np.int64)
    sum_cg = cg.reshape(M, n).sum(axis=1, dtype=acc).astype(np.int64)

    gs = np.zeros((M, s, s), dtype=np.int16)
    gs[:, :, :-1] = np.abs(y[:, :, 1:] - y[:, :, :-1])
    gs[:, :-1, :] += np.abs(y[:, 1:, :] - y[:, :-1, :])
    gs = gs.reshape(M, n)
    grad_sum = gs.sum(axis=1, dtype=acc).astype(np.int64)
    e = np.count_nonzero(gs > p["TAU_EDGE"], axis=1).astype(np.int64)
    r8, g8, b8 = (np.ascontiguousarray(c) - np.uint8(1) for c in (r8, g8, b8))   # uint8 wrap: {0, 255} -> {255, 254}
    sm = (r8 >= 254) | (g8 >= 254) | (b8 >= 254)
    sat = np.count_nonzero(sm.reshape(M, n), axis=1).astype(np.int64)

    b = s // 8
    y4 = y.reshape(M, s, 8, b)
    cs = y4[..., 0].astype(acc)
    for k in range(1, b):
        cs += y4[..., k]
    cs = cs.reshape(M, 8, b, 8)
    sub = cs[:, :, 0].astype(np.int64)
    for k in range(1, b):
        sub += cs[:, :, k]
    bits = (sub * n > sum_y[:, None, None] * (b * b)).reshape(M, 64)
    sig64 = np.bitwise_or.reduce(bits.astype(np.uint64) << np.arange(64, dtype=np.uint64), axis=1)

    flags = np.zeros(M, dtype=np.uint8)
    flags |= np.where((2 * e >= s) & (e < 4 * s), F_EDGE, 0).astype(np.uint8)
    flags |= np.where(e >= 4 * s, F_TEXTURE, 0).astype(np.uint8)
    flags |= np.where(4 * sat >= n, F_SAT, 0).astype(np.uint8)
    flags |= np.where(grid.pad_flags()[idx], F_PAD, 0).astype(np.uint8)
    out["flags"][idx] = flags
    out["y_q4"][idx] = 16 * sum_y // n
    out["co_h"][idx] = np.clip(sum_co // (2 * n), -128, 127)
    out["cg_h"][idx] = np.clip(sum_cg // (2 * n), -128, 127)
    sigma = isqrt(n * sum_y2 - sum_y * sum_y)
    out["sig_q4"][idx] = 16 * sigma // n
    out["grad_q6"][idx] = 64 * grad_sum // n
    out["edge"][idx] = 255 * e // n
    out["sig64"][idx] = sig64


def _sha(a):
    return hashlib.sha256(np.ascontiguousarray(a).tobytes()).hexdigest()


def _frames(name, W, H, n=16):
    return render_scenario(name, 1, W, H, n)[0]


class DescEquivTest(unittest.TestCase):
    def _check(self, W, H, n):
        g = Grid(W, H)
        for name in SCENARIOS:
            fr = _frames(name, W, H, n)
            prev_p = prev_t = prev_ref = None
            for i in range(min(n, 6)):
                p = g.pad(fr[i])
                y = desc.luma_of_padded(p)
                ref, ref_eq = ref_describe_padded(p, g, prev_p, None if prev_ref is None else prev_ref["xh64"])
                cases = {
                    "plain": desc.describe_padded(p, g, prev_p, None if prev_ref is None else prev_ref["xh64"]),
                    "yplane": desc.describe_padded(p, g, prev_p, None if prev_ref is None else prev_ref["xh64"], y_plane=y),
                    "reuse": desc.describe_padded(p, g, prev_p, None, prev_tiles=prev_t, y_plane=y),
                }
                for k, (t, eq) in cases.items():
                    if k == "reuse" and prev_t is None:
                        continue
                    self.assertEqual(_sha(t), _sha(ref), (name, i, k))
                    self.assertTrue(np.array_equal(eq, ref_eq), (name, i, k))
                prev_p, prev_t, prev_ref = p, cases["plain"][0], ref

    def test_scenarios_256x160(self):
        self._check(256, 160, 16)

    def test_scenarios_720p(self):
        self._check(1280, 720, 16)

    def test_luma_of_padded_equals_change_luma_padded(self):
        for W, H in ((1280, 720), (1920, 1080), (1000, 600), (256, 160)):
            g = Grid(W, H)
            rgb = np.random.RandomState(W).randint(0, 256, (H, W, 3)).astype(np.uint8)
            rgb[0, :5] = 0
            rgb[1, :5] = 255
            y = desc.luma_of_padded(g.pad(rgb))
            self.assertEqual(y.dtype, np.int16)
            self.assertTrue(np.array_equal(y, change.luma_padded(rgb, g.s, g.gw, g.gh)), (W, H))
            self.assertTrue(np.array_equal(y, ycocg(g.pad(rgb))[0]))

    def test_bad_y_plane_changes_output(self):
        """Non-vacuity: the y plane is really consumed (a wrong plane must change the tiles)."""
        g = Grid(256, 160)
        p = g.pad(_frames("gpu_jitter", 256, 160)[1])
        good = desc.describe_padded(p, g)[0]
        bad = desc.describe_padded(p, g, y_plane=desc.luma_of_padded(p) + 1)[0]
        self.assertNotEqual(_sha(good), _sha(bad))

    def test_perf_log(self):
        if os.environ.get("VQA_PERF_LOG") != "1":
            self.skipTest("VQA_PERF_LOG=1 to log")
        g = Grid(1280, 720)
        fr = _frames("gpu_jitter", 1280, 720)
        p, pp = g.pad(fr[1]), g.pad(fr[0])
        ts = []
        for _ in range(15):
            t = time.perf_counter()
            desc.describe_padded(p, g, pp, y_plane=desc.luma_of_padded(p))
            ts.append((time.perf_counter() - t) * 1e3)
        print("\n[perf] describe+luma 720p median %.2f ms min %.2f ms" % (statistics.median(ts), min(ts)))


if __name__ == "__main__":
    unittest.main()
