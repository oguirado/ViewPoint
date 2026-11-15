"""ADV-S1-2 (Argon ADV_SEAL1 §2): the aperture rule must not hide a real FROZEN/DRIFT HUD (2A) and the clipped-window
FLICKER drop must not hide a real interior A-B-A blink (2B) during a published global pan. 1280x720, s16, gray frames
(YCoCg-R luma = grey value). Pan: random texture moving +4 px/frame -> gv = (4, 0). HUD rows y 16..31 (ty = 1)."""
import unittest

import numpy as np

from visioncore import change as C, desc, params as P
from visioncore.grid import Grid

W, H, DX = 1280, 720, 4
FROZEN, DRIFT, FLICKER, MOVED = 5, 6, 4, 3


def _bg(f, seed=11):
    rng = np.random.RandomState(seed)
    base = rng.randint(30, 220, (H, W + DX * 64)).astype(np.uint8)
    x0 = DX * 64 - DX * f
    return base[:, x0:x0 + W].copy()  # cur[x] = prev[x - 4]


def _run(frames, params=None):
    g = Grid(W, H)
    idx = {int(m): k for k, m in enumerate(g.mortons)}
    pw = px = st = None
    cls, gmv = [], []
    for f in frames:
        rgb = np.repeat(f[..., None], 3, axis=2)
        pad = g.pad(rgb)
        t, eq = desc.describe_padded(pad, g, pw, px)
        y = C.luma_padded(rgb, g.s, g.gw, g.gh)
        if st is None:
            st = C.ClassState.init(t)
            o, _, s = C.classify_first(st, t, y)
        else:
            o, st, s = C.classify_tiles(st, t, eq, y, (g.s, g.gw, g.gh), params)
        pw, px = pad, t["xh64"]
        cls.append(o["cls"].copy())
        gmv.append(s["global_mv"])
    return np.array(cls), gmv, lambda tx, ty: idx[_morton(tx, ty)]


def _morton(tx, ty):
    m = 0
    for b in range(8):
        m |= ((tx >> b) & 1) << (2 * b) | ((ty >> b) & 1) << (2 * b + 1)
    return m


def _hud_bar(fill, border=220):
    """Advocate 2A bar: tx 2..10 (x 32..175), ty 1; 2-px borders Y=220, fill Y=fill (row-periodic, x-invariant)."""
    bar = np.full((16, 9 * 16), fill, np.int16)
    bar[:2] = bar[-2:] = border
    return bar


def _frames_2a(n=26, freeze=11, motion_history=False, jitter=False, drift=False):
    out = []
    for f in range(n):
        fr = _bg(f).astype(np.int16)
        fill = 140 + 6 * max(0, freeze - f)  # animates (not motion-explained) until t = freeze, then frozen
        bar = _hud_bar(fill if not drift else 140)
        if drift and f >= freeze:
            bar[2:-2] = 140 + (f - freeze)  # slow sub-threshold ramp of a frozen bar (DRIFT variant)
        if motion_history and f < freeze:
            bar = _hud_bar(140)
            xs = (np.arange(bar.shape[1]) + 32 - DX * f) % 48 < 12  # a vertical edge carried by the pan
            bar[:, xs] = 255 - bar[:, xs]
        if jitter and f >= freeze:
            bar[2:-2] += f & 1  # +-1 LSB GPU jitter after the stop (non-active, words_eq False)
        fr[16:32, 32:176] = bar
        out.append(np.clip(fr, 0, 255).astype(np.uint8))
    return out


def _frames_2b(n=14, a_hi=200, b_lo=100):
    """Advocate 2B: tile (5,1) blinks A-B-A; A: x 0..3 = 40, x 4..15 = a_hi; B: x 0..3 = 200, x 4..15 = b_lo.
    Neighbour (4,1) is constant 40 (its right 4 cols feed the gv window)."""
    out = []
    for f in range(n):
        fr = _bg(f)
        fr[16:32, 64:80] = 40
        if f % 2 == 0:
            fr[16:32, 80:84], fr[16:32, 84:96] = 40, a_hi
        else:
            fr[16:32, 80:84], fr[16:32, 84:96] = 200, b_lo
        out.append(fr)
    return out


def _sads(frames, t):
    g = Grid(W, H)
    yc, yp = (C.luma_padded(np.repeat(frames[i][..., None], 3, axis=2), g.s, g.gw, g.gh) for i in (t, t - 1))
    m = np.array([_morton(5, 1)])
    tx, ty = C._morton_xy(m)
    s0, _ = C._tile_sad_clip(yc, yp, (0, 0), g.s, g.gh, g.gw, tx, ty)
    sg, nv = C._tile_sad_clip(yc, yp, (DX, 0), g.s, g.gh, g.gw, tx, ty)
    return int(s0[0]), int(sg[0]), int(nv[0])


class AdvNumbers(unittest.TestCase):
    def test_advocate_sads(self):
        self.assertEqual(_sads(_frames_2a(), 20), (0, 0, 256))           # 2A: s0 = sg = 0, nv = 256
        self.assertEqual(_sads(_frames_2b(), 6), (29440, 12800, 256))    # 2B: s0c, sgc, nvc
        self.assertEqual(_sads(_frames_2b(b_lo=170), 6), (16000, 3840, 256))


ADV_PARAMS = dict(P.PARAMS, MV_SAD_RATIO_DEN=2, FROZEN_M=12)  # constants quoted by the advocate


class Adv2AFrozenHud(unittest.TestCase):
    def _frozen_tiles(self, frames, params=None):
        cls, gmv, at = _run(frames, params)
        self.assertTrue(all(g == [DX, 0] for g in gmv[12:]), gmv)
        return [tx for tx in range(3, 11) if (cls[1:, at(tx, 1)] == FROZEN).any()], cls, at

    def test_2a_exact_frozen_bar_during_pan_is_frozen(self):
        got, cls, at = self._frozen_tiles(_frames_2a())
        self.assertIn(5, got, cls[:, at(5, 1)])  # BASE_E: tile (5,1) masked to UNCHANGED (s0 = sg = 0)
        self.assertEqual(got, list(range(3, 11)))

    def test_2a_advocate_constants(self):
        got, _, _ = self._frozen_tiles(_frames_2a(), ADV_PARAMS)
        self.assertIn(5, got)

    def test_2a_jitter_variant_frozen(self):
        got, _, _ = self._frozen_tiles(_frames_2a(jitter=True))
        self.assertIn(5, got)

    def test_2a_drift_variant_not_masked(self):
        cls, _, at = _run(_frames_2a(drift=True))
        self.assertTrue((cls[1:, at(5, 1)] == DRIFT).any(), cls[:, at(5, 1)])

    def test_2a_motion_explained_history_stays_ambiguous(self):  # X4 scroll guard: the pan itself animated the bar
        cls, _, at = _run(_frames_2a(motion_history=True))
        self.assertTrue((cls[1:11, at(5, 1)] == MOVED).any(), cls[:, at(5, 1)])
        self.assertFalse(np.isin(cls[1:, [at(tx, 1) for tx in range(3, 11)]], (FROZEN, DRIFT)).any())


class Adv2BBlinkHud(unittest.TestCase):
    def _blink(self, frames, params=None):
        cls, gmv, at = _run(frames, params)
        self.assertTrue(all(g == [DX, 0] for g in gmv[2:]), gmv)
        return cls[2:, at(5, 1)]

    def test_2b_advocate_exact_numbers(self):  # sgc 12,800, s0c 29,440, den 2: BASE_E drops FLICKER -> CHANGED
        c = self._blink(_frames_2b(), ADV_PARAMS)
        self.assertTrue((c == FLICKER).all(), c)

    def test_2b_default_params_variant(self):  # B fill 170: sgc 3,840, s0c 16,000, den 3 -> 3*3840 < 16000
        c = self._blink(_frames_2b(b_lo=170))
        self.assertTrue((c == FLICKER).all(), c)

    def test_2b_default_params_exact_numbers(self):
        c = self._blink(_frames_2b())
        self.assertTrue((c == FLICKER).all(), c)


if __name__ == "__main__":
    unittest.main()
