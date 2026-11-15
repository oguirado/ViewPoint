# @module {"id":"vc.s1","layer":1,"stage":"S1","owner":"CORE","deps":["vc.p","vc.layout","vc.errors"],"book":"HB-VC-s1-desc"}
"""@module vc.s1 L1 - S1 tile descriptors: exact (xh64) and perceptual (y/co/cg/sigma/grad/edge/sig64) keys (SPEC_CORE §2)."""
import hashlib

import numpy as np

from . import layout
from .errors import VqaError
from .params import PARAMS

TILE_DTYPE = np.dtype({
    "names": list(layout.TILE_FIELDS),
    "formats": ["<u2", "u1", "u1", "<u2", "i1", "i1", "<u2", "<u2", "u1", "u1", "i1", "i1", "<u8", "<u8"],
    "offsets": list(layout.TILE_OFFSETS),
    "itemsize": layout.TILE_SIZE,
})
assert TILE_DTYPE.itemsize == layout.TILE_SIZE

F_EDGE, F_TEXTURE, F_SHADE_ONLY, F_SAT, F_UI_OCC, F_ACTOR_OCC, F_GLOBAL_MV, F_PAD = (1 << i for i in range(8))


# @anchor:vc.s1.isqrt
def isqrt(v):
    """Exact floor sqrt of int64 array v, 0 <= v < 2**52 (float64 seed + one integer correction each way)."""
    v = np.asarray(v, dtype=np.int64)
    r = np.floor(np.sqrt(v.astype(np.float64))).astype(np.int64)
    r -= (r * r > v)
    r += ((r + 1) * (r + 1) <= v)
    return r


def ycocg(rgb):  # @anchor:vc.s1.ycocg
    """YCoCg-R lossless lifting in int16 (VG-0040). -> Y, Co, Cg."""
    r = rgb[..., 0].astype(np.int16)
    g = rgb[..., 1].astype(np.int16)
    b = rgb[..., 2].astype(np.int16)
    co = r - b
    t = b + (co >> 1)
    cg = g - t
    y = t + (cg >> 1)
    return y, co, cg


def luma_of_padded(padded):  # @anchor:vc.s1.luma_of_padded
    """YCoCg-R luma plane (int16) of a whole padded frame; equals change.luma_padded (edge pad commutes with the lift).
    Pass it as describe_padded(y_plane=...) and as classify_tiles(y_cur=...) so luma is computed once per frame."""
    r = padded[..., 0].astype(np.int16)
    g = padded[..., 1].astype(np.int16)
    b = padded[..., 2].astype(np.int16)
    t = b + ((r - b) >> 1)
    return t + ((g - t) >> 1)


def xh64_of(block):  # @anchor:vc.s1.xh64_of
    """BLAKE2b-64 (person vqa-tile) of a tile's padded RGB bytes, row-major, as unsigned LE integer."""
    h = hashlib.blake2b(np.ascontiguousarray(block).tobytes(), digest_size=8, person=b"vqa-tile").digest()
    return int.from_bytes(h, "little")


def describe(frame, grid, words_prev=None, prev_xh64=None, p=None):  # @anchor:vc.s1.describe
    """frame uint8[H,W,3] -> (tiles TILE_DTYPE[N], words_eq bool[N]). See describe_padded."""
    if getattr(frame, "shape", None) != (grid.H, grid.W, 3) or frame.dtype != np.uint8:
        raise VqaError("E_VC_FRAME_SHAPE", {"shape": list(getattr(frame, "shape", ())), "grid": grid.ident})
    return describe_padded(grid.pad(frame), grid, words_prev, prev_xh64, p)


def describe_padded(padded, grid, words_prev=None, prev_xh64=None, p=None, prev_tiles=None, y_plane=None, lazy_xh=False):  # @anchor:vc.s1.describe_padded
    """padded = grid.pad(frame). words_prev = previous padded frame (None: every tile counts as differing).
    prev_xh64 = previous tiles['xh64'] (None: hash every tile). prev_tiles = previous describe output: with
    words_prev, byte-equal tiles copy their descriptor verbatim (exact: equal bytes give equal descriptors) and only
    the other tiles are computed. y_plane = luma_of_padded(padded) when the caller already has it (else computed once
    here, only if some tile needs computing). lazy_xh=True (SPEC_INT I2) leaves xh64 = 0: the caller runs
    fill_xh64 after S2. cls/resid/mv fields are left 0 for S2. Tile-major: each tile is one contiguous block."""
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
        for f in _COPY_FIELDS:
            out[f][keep] = prev_tiles[f][keep]
        out["flags"][keep] = prev_tiles["flags"][keep] & _DESC_FLAGS
    if len(idx):
        if y_plane is None:
            y_plane = luma_of_padded(padded)
        _compute(out, idx, padded, grid, p, y_plane)
    if lazy_xh:
        return out, words_eq
    xh = np.zeros(N, dtype=np.uint64)
    if prev_xh64 is not None:
        xh[words_eq] = np.asarray(prev_xh64, dtype=np.uint64)[words_eq]
        hidx = np.nonzero(~words_eq)[0]
    else:
        hidx = np.arange(N)
    if len(hidx):
        xh[hidx] = _hash_tiles(padded, grid, hidx)
    out["xh64"] = xh
    return out, words_eq


def fill_xh64(out, padded, grid, prev_xh64=None, hash_all=False):  # @anchor:vc.s1.fill_xh64
    """Lazy xh64 (SPEC_INT I2), run after S2 on the classified tiles `out` (mutated in place). BLAKE2b-64 only for
    tiles with cls >= 2 (CLS_DIRTY_MIN) and, when hash_all (keyframe / no previous frame), for every tile; every other
    tile copies prev_xh64 (exact for UNCHANGED; a label only for JITTER, which TIME's period test must mask)."""
    cls = out["cls"]
    if prev_xh64 is None:
        hash_all = True
        xh = np.zeros(grid.N, dtype=np.uint64)
    else:
        xh = np.array(prev_xh64, dtype=np.uint64)
    idx = np.arange(grid.N) if hash_all else np.nonzero(cls >= layout.CLS_DIRTY_MIN)[0]
    if len(idx):
        xh[idx] = _hash_tiles(padded, grid, idx)
    out["xh64"] = xh
    return out


_COPY_FIELDS = ("y_q4", "co_h", "cg_h", "sig_q4", "grad_q6", "edge", "sig64")
_DESC_FLAGS = F_EDGE | F_TEXTURE | F_SAT | F_PAD


def _gather(padded, grid, idx):
    """-> uint8[M, s, s, 3], contiguous tile blocks for Morton ranks idx."""
    s = grid.s
    P = padded.reshape(grid.gh, s, grid.gw, s, 3)
    return P[grid.ty[idx], :, grid.tx[idx], :, :]


def _hash_tiles(padded, grid, idx):
    T = _gather(padded, grid, idx)
    rows = T.reshape(len(idx), -1)
    dig = b"".join(hashlib.blake2b(rows[k], digest_size=8, person=b"vqa-tile").digest() for k in range(len(idx)))
    return np.frombuffer(dig, dtype="<u8")


def _compute(out, idx, padded, grid, p, y_plane):
    """Fill the perceptual fields of out[idx] (int32 accumulators while 2**14 * 255**2 fits: s <= 128)."""
    s = grid.s
    n = s * s
    M = len(idx)
    acc = np.int32 if s <= 128 else np.int64
    T = _gather(padded, grid, idx)
    y = y_plane.reshape(grid.gh, s, grid.gw, s)[grid.ty[idx], :, grid.tx[idx], :]    # (M, s, s) int16
    b = T[..., 2].astype(np.int16)
    co = np.subtract(T[..., 0], b, dtype=np.int16)
    cg = np.subtract(T[..., 1], b + (co >> 1), dtype=np.int16)
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
    d = T - np.uint8(1)                                   # uint8 wrap: {0, 255} -> {255, 254}
    sm = np.maximum(np.maximum(d[..., 0], d[..., 1]), d[..., 2]) >= 254
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
    sig64 = np.packbits(bits, axis=1, bitorder="little").view("<u8")[:, 0].astype(np.uint64)

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
