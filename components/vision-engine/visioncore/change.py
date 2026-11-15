# @module {"id":"vc.s2","layer":2,"stage":"S2","owner":"CORE","deps":["vc.layout","vc.p","vc.errors"],"inputs":"tile records t, ClassState, words_eq[N]","outputs":"tile records with cls/resid/mv, ClassState, summary","invariants":["integer-only","deterministic","pure-step"],"anti_uses":["float thresholds","per-engine branches"],"oracle":"tests/test_change.py","book":"HB-VC-s2-change"}
"""@module vc.s2 L2 - S2 change classification (SPEC_CORE §3): UNCHANGED/JITTER/CHANGED/MOVED/FLICKER/FROZEN/DRIFT.

Operates on structured tile arrays whose field names are layout.TILE_FIELDS (desc.TILE_DTYPE is compatible).
Integer only; floor semantics for every division/shift (I4); no clock, no RNG (I2).
"""
import numpy as np

from . import layout, params as P
from .errors import VqaError

_POP8 = np.array([bin(i).count("1") for i in range(256)], np.uint8)


def _popcount(a):
    """Per-element popcount; numpy >= 2.0 has bitwise_count, older numpy falls back to a byte table (same values)."""
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(a)
    a = np.ascontiguousarray(a)
    return _POP8[a.view(np.uint8)].reshape(a.shape + (a.dtype.itemsize,)).sum(axis=-1, dtype=np.uint8)


TILE_DT = np.dtype([(n, f) for n, f in zip(
    layout.TILE_FIELDS,
    ("<u2", "u1", "u1", "<u2", "i1", "i1", "<u2", "<u2", "u1", "u1", "i1", "i1", "<u8", "<u8"))])
assert TILE_DT.itemsize == layout.TILE_SIZE

# weights of the §3.1 residual and the freeze latch (SPEC_CORE §3.1/§3.4/§7; not profile-overridable)
W_CHROMA = 8
W_HAM = 8
FROZEN_PRE_ACT = 8    # active in >= 8 of the 16 frames before the stop (DESIGN_CHOICE, CAL-3)
DRIFT_STALL = P.PARAMS["DRIFT_STALL"]  # default alias (tests); classify_tiles reads p["DRIFT_STALL"]
MOTION_D_MAX = 64
SHADE_DEN = 64        # SPEC_CORE §3.5 (single-sign delta share); structural, not in params.py (outside lane surface)
SHADE_EDGE_MAX = 8
ISLAND_MAX = 16       # bound on island vectors tried per frame (largest first)
FLAG_SHADE_ONLY = 1 << 2
FLAG_GLOBAL_MV = 1 << 6
C_UNCHANGED, C_JITTER, C_CHANGED, C_MOVED, C_FLICKER, C_FROZEN, C_DRIFT = range(7)


def thresholds(params=None):  # @anchor:vc.s2.drift
    """-> (T_enter, T_exit, drift_u) in q4 from the effective parameter table.

    drift_u = max(T_enter, DRIFT_U_Q4): DRIFT never fires inside the hysteresis band, so a profile that raises
    T0_Q4 raises the DRIFT threshold with it (RCORE-1; DRIFT FP = jitter FP at T_enter, SPEC_CORE §3.4).
    """
    p = P.PARAMS if params is None else params
    t0, band = p["T0_Q4"], p["HYST_BAND_PERMILLE"]
    t_enter = t0 * (1000 + band) // 1000
    return t_enter, t0 * (1000 - band) // 1000, max(t_enter, p["DRIFT_U_Q4"])


def residual(a, b, h_dead=None, mask=None):  # @anchor:vc.s2.residual
    """r(a,b) in q4 (SPEC_CORE §3.1), vectorised over tile arrays; int64 result.
    mask: optional uint64[N] of sig64 bits allowed to count (sig_conf of a | sig_conf of b, AMEND_NR); None = all 64."""
    hd = P.PARAMS["H_DEAD"] if h_dead is None else h_dead
    d = lambda f: np.abs(a[f].astype(np.int64) - b[f].astype(np.int64))
    x = a["sig64"] ^ b["sig64"]
    ham = _popcount(x if mask is None else x & mask).astype(np.int64)
    return d("y_q4") + W_CHROMA * (d("co_h") + d("cg_h")) + d("sig_q4") + W_HAM * np.maximum(0, ham - hd)


_ALL64 = np.uint64(0xFFFFFFFFFFFFFFFF)


def sig_conf(y_plane, s, gw, gh, mortons, sig_dead_q4):  # @anchor:vc.s2.sig_conf
    """Noise dead band of the sig64 Hamming term (SPEC_CORE §3.1, AMEND_NR). y_plane = padded luma (gh*s, gw*s).
    -> uint64[N] in the order of `mortons`: bit k set iff the mean of sub-block k (same 8x8 layout and bit order as
    desc sig64) is more than sig_dead_q4/16 grey from the tile mean. A sig64 bit that flips while it is inside the band
    in BOTH frames is not counted: +-1 LSB iid noise moves a sub-block mean by <= 2 grey frame to frame, so it can only
    flip bits that sit inside a band >= ~2 grey; a 1 px shift of real content moves the flipped sub-blocks far out.
    Integer only: 16*|sub*n - sum*b*b| > sig_dead_q4*n*b*b with n = s*s, b = s//8. sig_dead_q4 = 0 -> all bits."""
    tx, ty = _morton_xy(np.asarray(mortons))
    if sig_dead_q4 <= 0:
        return np.full(len(tx), _ALL64, np.uint64)
    b = s // 8
    n = s * s
    y = np.asarray(y_plane)
    c = y[:, 0::b].astype(np.int32)                       # sums of b px along x (strided views, no reshape copy)
    for k in range(1, b):
        c += y[:, k::b]
    sub = c[0::b]
    for k in range(1, b):
        sub = sub + c[k::b]                               # (gh*8, gw*8): sub-block sums, |.| <= 255*b*b
    sub = sub.reshape(gh, 8, gw, 8)
    tot = sub.sum(axis=(1, 3), dtype=np.int32)            # tile sums (gh, gw)
    if s > 64:                                            # int32 holds 16*|dev| <= 16*255*n*b*b only up to s = 64
        sub, tot = sub.astype(np.int64), tot.astype(np.int64)
    dev = sub * n - tot[:, None, :, None] * (b * b)
    bits = (16 * np.abs(dev) > sig_dead_q4 * n * b * b).transpose(0, 2, 1, 3)[ty, tx].reshape(len(tx), 64)
    return np.packbits(bits, axis=1, bitorder="little").view("<u8")[:, 0].astype(np.uint64)


def _conf_of(y_cur, grid, mortons, sig_dead_q4):
    """sig_conf of the current frame; grid (s, gw, gh) or None (derived from the Morton codes and the plane shape)."""
    if y_cur is None:
        return np.full(len(mortons), _ALL64, np.uint64)
    if grid is None:
        tx, ty = _morton_xy(np.asarray(mortons))
        gw, gh = int(tx.max()) + 1, int(ty.max()) + 1
        grid = (np.asarray(y_cur).shape[1] // gw, gw, gh)
    return sig_conf(y_cur, grid[0], grid[1], grid[2], mortons, sig_dead_q4)


def _as_tiles(tiles):
    out = np.empty(len(tiles), dtype=TILE_DT)
    for n in layout.TILE_FIELDS:
        out[n] = tiles[n]
    return out


class ClassState:
    """Per-tile state (SPEC_CORE §3.2). Arrays are length N; None until init."""

    def __init__(self):
        self.n = 0
        self.frame = -1

    @classmethod
    def init(cls, tiles):
        st = cls()
        t = _as_tiles(tiles)
        st.n = len(t)
        st.frame = 0
        st.hot = np.zeros(st.n, np.uint8)
        st.hist16 = np.zeros(st.n, np.uint16)
        st.still_run = np.zeros(st.n, np.uint8)
        st.pre_act = np.zeros(st.n, np.uint8)
        st.ref_max = np.zeros(st.n, np.uint16)
        st.stall, st.mvh16, st.pre_mov = np.zeros(st.n, np.uint8), np.zeros(st.n, np.uint16), np.zeros(st.n, np.uint8)
        st.ref, st.prev1, st.prev2 = t.copy(), t.copy(), t.copy()
        st.cf_ref = np.full(st.n, _ALL64, np.uint64)  # sig_conf of ref / prev1 / prev2 (all bits until a luma plane is seen)
        st.cf1, st.cf2 = st.cf_ref.copy(), st.cf_ref.copy()
        st.y_prev = None
        return st

    def digest_bytes(self):
        parts = [self.hot, self.hist16, self.still_run, self.pre_act, self.ref_max, self.stall, self.ref, self.prev1, self.prev2, self.mvh16, self.pre_mov,
                 self.cf_ref, self.cf1, self.cf2]
        return b"".join(np.ascontiguousarray(a).tobytes() for a in parts)


def luma_padded(rgb, s, gw, gh):
    """YCoCg-R luma plane (int16) padded by edge replication to (gh*s, gw*s)."""
    r = rgb[..., 0].astype(np.int16)
    g = rgb[..., 1].astype(np.int16)
    b = rgb[..., 2].astype(np.int16)
    co = r - b
    t = b + (co >> 1)
    cg = g - t
    y = t + (cg >> 1)
    H, W = y.shape
    return np.pad(y, ((0, gh * s - H), (0, gw * s - W)), mode="edge")


def _axis_shift(cur, prev, dmax):
    """Best integer shift d (cur[x] ~ prev[x-d]) of two 1-D projections; None if no clear winner."""
    n = len(cur)
    best, cost0 = None, None
    for d in range(-dmax, dmax + 1):
        if abs(d) >= n:
            continue
        if d >= 0:
            sad = int(np.abs(cur[d:] - prev[:n - d]).sum())
        else:
            sad = int(np.abs(cur[:n + d] - prev[-d:]).sum())
        cost = sad * n // (n - abs(d))
        if d == 0:
            cost0 = cost
        key = (cost, abs(d), d)
        if best is None or key < best:
            best = key
    if best is None or best[2] == 0 or not 3 * best[0] < cost0:
        return 0
    return best[2]


def global_vector(y_cur, y_prev, s):  # @anchor:vc.s2.motion
    """(gdx, gdy) from row/column projections (SPEC_CORE §3.5); (0,0) when nothing explains the frame."""
    # search range is the tile size: motion > s px/frame is unreachable (returns (0,0)); MOTION_D_MAX only caps large s
    dmax = min(s, MOTION_D_MAX)
    # Integer luma: widen the reduction, without copying both full planes.
    c = y_cur if np.issubdtype(y_cur.dtype, np.integer) else y_cur.astype(np.int64)
    p = y_prev if np.issubdtype(y_prev.dtype, np.integer) else y_prev.astype(np.int64)
    return (_axis_shift(c.sum(axis=0, dtype=np.int64), p.sum(axis=0, dtype=np.int64), dmax),
            _axis_shift(c.sum(axis=1, dtype=np.int64), p.sum(axis=1, dtype=np.int64), dmax))


def _tile_sums(absdiff, s, gh, gw):
    return absdiff.reshape(gh, s, gw, s).sum(axis=(1, 3))


def _morton_xy(m):
    m = m.astype(np.int64)

    def comp(v):
        v = v & 0x5555
        v = (v | (v >> 1)) & 0x3333
        v = (v | (v >> 2)) & 0x0F0F
        return (v | (v >> 4)) & 0x00FF
    return comp(m), comp(m >> 1)


def _tile_sad_windows(y_cur, y_prev, c, s, tx, ty):
    """Requested windows only; bounded batches keep widened pixels in cache.

    Pixels outside the shifted source contribute neither SAD nor count. SAD
    accumulates in int64; integer luma subtraction widens before taking abs.
    """
    dx, dy = c
    h, w = y_cur.shape
    tx, ty = np.broadcast_arrays(np.asarray(tx), np.asarray(ty))
    shape = tx.shape
    # Validate before casting: oversized unsigned indices must not wrap into
    # valid negative indices. All source offsets then use signed arithmetic.
    if (not np.issubdtype(tx.dtype, np.integer) or
            not np.issubdtype(ty.dtype, np.integer) or
            ((tx < -(w // s)) | (tx >= w // s) |
             (ty < -(h // s)) | (ty >= h // s)).any()):
        raise IndexError("tile index out of bounds")
    tx = tx.astype(np.int64, copy=False).ravel()
    ty = ty.astype(np.int64, copy=False).ravel()
    # Match NumPy's tile indexing (including valid negative tile indices).
    tx = np.where(tx < 0, tx + w // s, tx)
    ty = np.where(ty < 0, ty + h // s, ty)
    sad = np.zeros(tx.size, np.int64)
    count = np.zeros(tx.size, np.int64)
    cur_windows = np.lib.stride_tricks.sliding_window_view(y_cur, (s, s))
    prev_windows = np.lib.stride_tricks.sliding_window_view(y_prev, (s, s))
    # int16 luma differences fit int32; wider/noninteger inputs retain sealed casts.
    narrow = all(np.issubdtype(y.dtype, np.integer) and y.dtype.itemsize <= 2
                 for y in (y_cur, y_prev))
    for lo in range(0, tx.size, 128):
        hi = min(lo + 128, tx.size)
        yy, xx = ty[lo:hi] * s, tx[lo:hi] * s
        sy, sx = yy - dy, xx - dx
        ny = np.maximum(0, np.minimum(sy + s, h) - np.maximum(sy, 0))
        nx = np.maximum(0, np.minimum(sx + s, w) - np.maximum(sx, 0))
        count[lo:hi] = ny * nx
        full = count[lo:hi] == s * s
        if full.any():
            cur = cur_windows[yy[full], xx[full]]
            src = prev_windows[sy[full], sx[full]]
            if narrow:
                diff = np.subtract(cur, src, dtype=np.int32)
            else:
                diff = cur.astype(np.int64) - src.astype(np.int64)
            np.abs(diff, out=diff)
            sad[lo + np.flatnonzero(full)] = diff.sum(axis=(1, 2), dtype=np.int64)
        # Only boundary windows need clipping. Their reductions are independent;
        # candidate offers and all classification state transitions remain ordered.
        for j in np.flatnonzero((count[lo:hi] > 0) & ~full):
            y0, y1 = max(int(yy[j]), dy), min(int(yy[j]) + s, h + dy)
            x0, x1 = max(int(xx[j]), dx), min(int(xx[j]) + s, w + dx)
            cur = y_cur[y0:y1, x0:x1].astype(np.int64)
            src = y_prev[y0 - dy:y1 - dy, x0 - dx:x1 - dx].astype(np.int64)
            sad[lo + j] = np.abs(cur - src).sum(dtype=np.int64)
    return sad.reshape(shape)[()], count.reshape(shape)[()]


def _tile_sad(y_cur, y_prev, c, s, gh, gw, tx, ty):
    """Per-tile SAD and full s*s source-window validity, in requested order."""
    sad, count = _tile_sad_windows(y_cur, y_prev, c, s, tx, ty)
    return sad, count == s * s


def _tile_sad_clip(y_cur, y_prev, c, s, gh, gw, tx, ty):
    """SAD over the in-bounds pixels of the shifted window and their count."""
    return _tile_sad_windows(y_cur, y_prev, c, s, tx, ty)


def _islands(mask):
    """8-connected labelling of a bool (gh, gw) mask; labels 1.. in raster order of first tile, 0 = background."""
    gh, gw = mask.shape
    lab = np.zeros((gh, gw), np.int32)
    n = 0
    for y0, x0 in zip(*np.nonzero(mask)):
        if lab[y0, x0]:
            continue
        n += 1
        lab[y0, x0] = n
        stack = [(int(y0), int(x0))]
        while stack:
            y, x = stack.pop()
            for yy in range(max(0, y - 1), min(gh, y + 2)):
                for xx in range(max(0, x - 1), min(gw, x + 2)):
                    if mask[yy, xx] and not lab[yy, xx]:
                        lab[yy, xx] = n
                        stack.append((yy, xx))
    return lab, n


def island_vector(lab_mask, y_cur, y_prev, s):  # @anchor:vc.s2.island_vector
    """(dx, dy) of one island (bool (gh, gw) tile mask): projections over its bbox + s margin (SPEC_CORE §3.5)."""
    ys, xs = np.nonzero(lab_mask)
    Hh, Ww = y_cur.shape
    r0, r1 = max(0, (int(ys.min()) - 1) * s), min(Hh, (int(ys.max()) + 2) * s)
    c0, c1 = max(0, (int(xs.min()) - 1) * s), min(Ww, (int(xs.max()) + 2) * s)
    return global_vector(y_cur[r0:r1, c0:c1], y_prev[r0:r1, c0:c1], s)


def classify_tiles(state, tiles, words_eq, y_cur=None, grid=None, params=None):  # @anchor:vc.s2.classify
    """One step of S2. tiles: descriptors of frame t (cls ignored); words_eq: bool[N] exact pixel equality with t-1.

    y_cur: optional padded luma plane (see luma_padded) enabling MOVED; grid: (s, gw, gh), needed with y_cur.
    Returns (tiles_out, state, summary); `state` is updated in place and returned.
    """
    t = _as_tiles(tiles)
    n = state.n
    if len(t) != n or len(words_eq) != n:
        raise VqaError("E_VC_GRID_MISMATCH", {"state_n": n, "tiles_n": len(t), "words_eq_n": len(words_eq)})
    words_eq = np.asarray(words_eq, bool)
    p = P.PARAMS if params is None else params
    t_enter, t_exit, drift_u = thresholds(p)
    out = t.copy()
    out["cls"] = C_UNCHANGED
    out["resid"] = 0
    out["mv_dx"] = 0
    out["mv_dy"] = 0
    out["flags"] = t["flags"] & ~np.uint8(FLAG_GLOBAL_MV | FLAG_SHADE_ONLY)
    summary = {"first": False, "n": [0] * 7, "cov_ppm": 0, "global_mv": None}
    state.frame += 1

    cf = _conf_of(y_cur, grid, t["morton"], p["SIG_DEAD_Q4"])
    r1 = residual(t, state.prev1, p["H_DEAD"], cf | state.cf1)
    r1[words_eq] = 0
    hot = state.hot.astype(bool)
    active = np.where(hot, r1 > t_exit, r1 > t_enter) & ~words_eq
    hist_before = state.hist16
    prev_active = (hist_before & 1).astype(bool)

    # stillness bookkeeping (§3.4)
    trans = ~active & prev_active
    state.pre_act = np.where(trans, _popcount(hist_before), state.pre_act).astype(np.uint8)
    state.pre_mov = np.where(trans, _popcount(state.mvh16), state.pre_mov).astype(np.uint8)
    run = np.where(trans, 1, np.minimum(state.still_run.astype(np.int64) + 1, 255))
    state.still_run = np.where(active, 0, run).astype(np.uint8)
    hist = ((hist_before << np.uint16(1)) | active.astype(np.uint16)).astype(np.uint16)
    cls = np.zeros(n, np.uint8)
    cls[~words_eq & ~active] = C_JITTER

    # FLICKER (rule 3): A-B-A at descriptor level; no transition counter (A->B->B->C is not FLICKER)
    r2 = residual(t, state.prev2, p["H_DEAD"], cf | state.cf2)
    flicker = active & (r2 <= t_exit) & (r1 > t_enter)

    # Reuse each vector's sums/counts only within this frame. Offers still execute
    # in sealed order: strict < best makes first-match tie breaking observable.
    sad_cache = {}

    def sad_for(vec):
        if vec not in sad_cache:
            sad_cache[vec] = _tile_sad_clip(y_cur, state.y_prev, vec, s, gh, gw, tx, ty)
        return sad_cache[vec]

    # MOVED (rule 4): candidates in order [global, island, (0,0)]
    moved, mexp = np.zeros(n, bool), np.zeros(n, bool)  # mexp: edge tiles explained by gv on the clipped window
    if y_cur is not None and state.y_prev is not None and grid is not None and active.any():
        s, gw, gh = grid
        tx, ty = _morton_xy(t["morton"])
        nn = s * s
        den = p["MV_SAD_RATIO_DEN"]
        sad_0, _ = sad_for((0, 0))
        best = np.full(n, np.iinfo(np.int64).max, np.int64)
        mvx = np.zeros(n, np.int64)
        mvy = np.zeros(n, np.int64)
        is_glob = np.zeros(n, bool)

        def offer(vec, member, glob):
            sad_c, count_c = sad_for(vec)
            ok_c = count_c == nn
            expl = member & ok_c & (den * sad_c < sad_0) & (16 * sad_c <= nn * t_exit) & (sad_c < best)
            best[expl] = sad_c[expl]
            mvx[expl], mvy[expl] = vec
            is_glob[expl] = glob
            return expl

        cand_base = active & ~flicker
        gv = global_vector(y_cur, state.y_prev, s)
        published = None
        if gv != (0, 0):
            expl_g = offer(gv, cand_base, True)
            if int(expl_g.sum()) * 1000000 >= p["GLOBAL_SHARE_PPM"] * int(active.sum()):
                summary["global_mv"] = [gv[0], gv[1]]
                published = gv
                flicker &= ~offer(gv, flicker, True)  # global motion explains an A-B-A tile -> MOVED (DIAG_X4)
                sgc, nvc = sad_for(gv)
                s0c = sad_0
                mexp = (nvc < nn) & (2 * nvc >= nn) & (den * sgc * nn < s0c * nvc) & (16 * sgc <= nvc * t_exit)
                flicker &= ~mexp  # edge tiles only, under offer()'s residual ceiling (ADV 2B); interior blinks stay
            else:  # not published: global candidate withdrawn
                best[:] = np.iinfo(np.int64).max
                is_glob[:] = False
                mvx[:] = 0
                mvy[:] = 0
        amask = np.zeros((gh, gw), bool)
        amask[ty[cand_base], tx[cand_base]] = True
        lab, nl = _islands(amask)
        if nl:
            sizes = np.bincount(lab.ravel(), minlength=nl + 1)[1:]
            order = sorted(range(1, nl + 1), key=lambda k: (-int(sizes[k - 1]), k))[:ISLAND_MAX]
            by_vec = {}
            for k in order:
                v = island_vector(lab == k, y_cur, state.y_prev, s)
                if v != (0, 0) and v != published:
                    by_vec.setdefault(v, []).append(k)
            tile_lab = lab[ty, tx]
            for v in sorted(by_vec):
                offer(v, np.isin(tile_lab, by_vec[v]) & cand_base, False)
            for v in sorted(by_vec):  # pool: every island vector is a candidate for every active tile (mixed islands)
                offer(v, cand_base, False)
        moved = best != np.iinfo(np.int64).max
        out["mv_dx"][moved] = mvx[moved]
        out["mv_dy"][moved] = mvy[moved]
        out["flags"][moved & is_glob] |= np.uint8(FLAG_GLOBAL_MV)
    cls[active] = C_CHANGED
    cls[moved] = C_MOVED
    cls[flicker] = C_FLICKER

    # post-rules on non-active tiles: FROZEN then DRIFT
    n_active = int(active.sum())
    alive = summary["global_mv"] is not None or n_active * 1000000 // n >= p["FROZEN_COV_PPM"]
    if alive:
        frozen = ~active & (state.still_run >= p["FROZEN_M"]) & (state.pre_act >= FROZEN_PRE_ACT)
    else:
        frozen = np.zeros(n, bool)
    # aperture rule (DIAG_X4): a non-active tile that the published global vector explains at least as well as
    # zero motion is ambiguous, not a stop/drift - unless its history is (ADV-S1-2, SPEC_CORE §3.3/§3.4)
    amb = amb_lt = np.zeros(n, bool)
    if summary["global_mv"] is not None and y_cur is not None and state.y_prev is not None and grid is not None:
        s_, gw_, gh_ = grid
        tx_, ty_ = _morton_xy(t["morton"])
        s0, _ = sad_for((0, 0))
        sg, nv = sad_for(tuple(summary["global_mv"]))
        amb = ~active & (2 * nv >= s_ * s_) & (sg * (s_ * s_) <= s0 * nv)
        amb_lt = amb & (sg * (s_ * s_) < s0 * nv)
    frozen &= ~(amb & (state.pre_act.astype(np.int64) - state.pre_mov < FROZEN_PRE_ACT))  # non-motion activity latches
    cls[frozen] = C_FROZEN
    # DRIFT latch (§3.4): holds while the residual vs ref keeps setting new maxima; ref is NOT reset on DRIFT
    cand = ~active & ~frozen
    rr_all = residual(t, state.ref, p["H_DEAD"], cf | state.cf_ref)
    grew = cand & (rr_all > state.ref_max)
    ref_max = np.where(grew, np.minimum(rr_all, 65535), state.ref_max).astype(np.uint16)
    stall = np.where(grew, 0, np.where(cand, np.minimum(state.stall.astype(np.int64) + 1, 255), state.stall)).astype(np.uint8)
    drift = cand & (rr_all > drift_u) & (stall < p["DRIFT_STALL"]) & ~(amb_lt | (amb & (state.pre_mov > 0)))
    settle = cand & (stall >= p["DRIFT_STALL"])
    cls[drift] = C_DRIFT
    out["cls"] = cls

    # SHADE_ONLY (b2) on CHANGED tiles: one-sided luma delta and stable edge energy (§3.5)
    chg = cls == C_CHANGED
    if y_cur is not None and state.y_prev is not None and grid is not None and chg.any():
        s, gw, gh = grid
        tx, ty = _morton_xy(t["morton"])
        dy_ = y_cur.astype(np.int64) - state.y_prev.astype(np.int64)
        n_pos = _tile_sums((dy_ > 0).astype(np.int64), s, gh, gw)[ty, tx]
        n_neg = _tile_sums((dy_ < 0).astype(np.int64), s, gh, gw)[ty, tx]
        d_edge = np.abs(t["edge"].astype(np.int64) - state.prev1["edge"].astype(np.int64))
        shade = chg & (np.minimum(n_pos, n_neg) <= (s * s) // SHADE_DEN) & (d_edge <= SHADE_EDGE_MAX)
        out["flags"][shade] |= np.uint8(FLAG_SHADE_ONLY)
    # resid = min(255, r >> 2) with r vs the previous frame (drift tiles: vs reference).
    # UNITS: the field is r/4 (display units, saturates at 255 = 1020 q4) and 0 for UNCHANGED; it is NOT q4. Consumers
    # that fit thresholds in q4 (T0_Q4) must recompute change.residual(tile, prev, H_DEAD) instead of using this field.
    r = np.where(drift, rr_all, r1)
    out["resid"] = np.minimum(255, r >> 2).astype(np.uint8)
    unch_like = (cls == C_UNCHANGED)
    out["resid"][unch_like] = 0
    # state update
    state.hot = active.astype(np.uint8)
    state.hist16 = hist
    state.mvh16 = ((state.mvh16 << np.uint16(1)) | ((cls == C_MOVED) | (mexp & active)).astype(np.uint16)).astype(np.uint16)
    upd = (cls == C_CHANGED) | (cls == C_MOVED) | (cls == C_FLICKER) | settle
    state.ref_max, state.stall = ref_max, stall
    state.ref[upd] = t[upd]
    state.cf_ref[upd] = cf[upd]
    state.ref_max[upd] = 0
    state.stall[upd] = 0
    state.prev2 = state.prev1
    state.prev1 = t.copy()
    state.cf2, state.cf1 = state.cf1, cf
    if y_cur is not None:
        state.y_prev = y_cur

    counts = np.bincount(cls, minlength=7)
    summary["n"] = [int(c) for c in counts[:7]]
    summary["cov_ppm"] = int(counts[2:7].sum()) * 1000000 // n  # I6: dirty = cls >= 2
    return out, state, summary


def classify_first(state, tiles, y_cur=None):  # @anchor:vc.s2.classify_first
    """Frame 0: all UNCHANGED; ref = prev1 = prev2 = desc_0 (set by ClassState.init)."""
    out = _as_tiles(tiles)
    out["cls"] = C_UNCHANGED
    out["resid"] = 0
    out["mv_dx"] = 0
    out["mv_dy"] = 0
    if y_cur is not None:
        state.y_prev = y_cur
        cf = _conf_of(y_cur, None, out["morton"], P.PARAMS["SIG_DEAD_Q4"])
        state.cf_ref, state.cf1, state.cf2 = cf, cf.copy(), cf.copy()
    n = state.n
    return out, state, {"first": True, "n": [n, 0, 0, 0, 0, 0, 0], "cov_ppm": 0, "global_mv": None}
