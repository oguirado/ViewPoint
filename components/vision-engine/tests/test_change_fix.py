"""RCORE-1/2/3/7/8 fixes in S2 change classification: drift threshold, island MOVED, SHADE_ONLY, boundaries."""
import unittest

import numpy as np

from visioncore import change as C, layout, params as P
from tests.test_change import mk, run


def grid_tiles(gw, gh, s, y):
    """Tile records (row-major order) with y_q4 from the padded luma plane y."""
    t = mk(gw * gh)
    m = y.astype(np.int64).reshape(gh, s, gw, s).sum(axis=(1, 3))
    for i in range(gw * gh):
        tx, ty = i % gw, i // gw
        t["morton"][i] = layout.morton(tx, ty)
        t["y_q4"][i] = (16 * m[ty, tx]) // (s * s)
    return t


class TestDriftTracksT0(unittest.TestCase):  # RCORE-1
    def test_thresholds_drift_follows_t_enter(self):
        p = P.effective({"t0_q4": 60})
        self.assertEqual(C.thresholds(p), (66, 54, 66))
        self.assertEqual(C.thresholds(), (35, 28, 35))

    def test_stationary_jitter_not_drift_with_raised_t0(self):
        p = P.effective({"t0_q4": 60})
        frames = []
        for i in range(20):
            t = mk(8)
            t["y_q4"] += np.uint16(48 if i % 2 else 0)  # 3 grey alternation inside the band
            frames.append(t)
        for o, _ in run(frames, params=p)[1:]:
            self.assertFalse((o["cls"] == C.C_DRIFT).any())
            self.assertFalse((o["cls"] >= C.C_CHANGED).any())

    def test_ramp_drifts_within_ceil_t_enter_over_v(self):
        p = P.effective({"t0_q4": 60})
        frames = []
        for i in range(14):
            t = mk(4)
            t["y_q4"] += np.uint16(8 * i)  # 0.5 grey/frame
            frames.append(t)
        res = run(frames, params=p)
        first = next(i for i, (o, _) in enumerate(res) if (o["cls"] == C.C_DRIFT).any())
        self.assertEqual(first, 9)  # ceil(66/8)=9


class TestIslandMoved(unittest.TestCase):  # RCORE-2
    def _scene(self, gw=12, gh=8, s=16, dx=4, dy=0):
        rng = np.random.RandomState(11)
        bg = np.full((gh * s, gw * s), 90, np.int16)
        spr = (rng.randint(0, 64, (3 * s, 3 * s)) + 3 * np.arange(3 * s)[None, :] + 60).astype(np.int16)
        prev, cur = bg.copy(), bg.copy()
        prev[2 * s:5 * s, 2 * s:5 * s] = spr
        cur[2 * s + dy:5 * s + dy, 2 * s + dx:5 * s + dx] = spr
        return (s, gw, gh), prev, cur

    def test_local_sprite_moved(self):
        grid, prev, cur = self._scene()
        s, gw, gh = grid
        t0, t1 = grid_tiles(gw, gh, s, prev), grid_tiles(gw, gh, s, cur)
        st = C.ClassState.init(t0)
        C.classify_first(st, t0, y_cur=prev)
        o, st, summ = C.classify_tiles(st, t1, np.zeros(len(t1), bool), y_cur=cur, grid=grid)
        mv = o["cls"] == C.C_MOVED
        self.assertGreaterEqual(int(mv.sum()), 4)
        self.assertTrue((o["mv_dx"][mv] == 4).all() and (o["mv_dy"][mv] == 0).all())
        self.assertLess(int(o["cls"][o["morton"] == layout.morton(0, 0)][0]), C.C_CHANGED)  # background

    def test_two_opposite_sprites_use_island_vectors(self):
        s, gw, gh = 16, 16, 8
        rng = np.random.RandomState(3)
        spr = lambda: (rng.randint(0, 64, (3 * s, 3 * s)) + 3 * np.arange(3 * s)[None, :] + 60).astype(np.int16)
        a, b = spr(), spr()
        prev = np.full((gh * s, gw * s), 90, np.int16)
        cur = prev.copy()
        prev[s:4 * s, s:4 * s] = a
        cur[s:4 * s, s + 4:4 * s + 4] = a
        prev[4 * s:7 * s, 9 * s:12 * s] = b
        cur[4 * s:7 * s, 9 * s - 4:12 * s - 4] = b
        self.assertEqual(C.global_vector(cur, prev, s), (0, 0))  # no single global explains both
        t0, t1 = grid_tiles(gw, gh, s, prev), grid_tiles(gw, gh, s, cur)
        st = C.ClassState.init(t0)
        C.classify_first(st, t0, y_cur=prev)
        o, st, summ = C.classify_tiles(st, t1, np.zeros(len(t1), bool), y_cur=cur, grid=(s, gw, gh))
        mv = o["cls"] == C.C_MOVED
        self.assertIsNone(summ["global_mv"])
        self.assertEqual({(int(x), int(y)) for x, y in zip(o["mv_dx"][mv], o["mv_dy"][mv])}, {(4, 0), (-4, 0)})
        self.assertFalse((o["flags"][mv] & C.FLAG_GLOBAL_MV).any())
        self.assertEqual(int(mv.sum()), int((o["cls"] >= C.C_CHANGED).sum()))

    def test_morton_xy_roundtrip(self):  # regression: in-place masking corrupted y
        for tx, ty in ((0, 0), (5, 2), (13, 7), (255, 255)):
            x, y = C._morton_xy(np.array([layout.morton(tx, ty)]))
            self.assertEqual((int(x[0]), int(y[0])), (tx, ty))

    def test_island_vector_direct(self):
        grid, prev, cur = self._scene(dx=0, dy=4)
        s, gw, gh = grid
        mask = np.zeros((gh, gw), bool)
        mask[2:6, 2:6] = True
        self.assertEqual(C.island_vector(mask, cur, prev, s), (0, 4))

    def test_islands_labelling_8_connected(self):
        m = np.zeros((4, 4), bool)
        m[0, 0] = m[1, 1] = True      # diagonal -> same island
        m[3, 3] = True
        lab, n = C._islands(m)
        self.assertEqual(n, 2)
        self.assertEqual(lab[0, 0], lab[1, 1])
        self.assertNotEqual(lab[0, 0], lab[3, 3])


class TestShadeOnly(unittest.TestCase):  # RCORE-3
    def _step(self, cur_plane):
        s, gw, gh = 16, 4, 2
        prev = np.full((gh * s, gw * s), 100, np.int16)
        t0, t1 = grid_tiles(gw, gh, s, prev), grid_tiles(gw, gh, s, cur_plane)
        st = C.ClassState.init(t0)
        C.classify_first(st, t0, y_cur=prev)
        return C.classify_tiles(st, t1, np.zeros(len(t1), bool), y_cur=cur_plane, grid=(s, gw, gh))

    def test_one_sided_delta_sets_flag(self):
        o, _, _ = self._step(np.full((32, 64), 112, np.int16))
        self.assertTrue((o["cls"] == C.C_CHANGED).all())
        self.assertTrue((o["flags"] & C.FLAG_SHADE_ONLY).all())

    def test_two_sided_delta_no_flag(self):
        cols = np.arange(64)[None, :] % 4 < 2
        o, _, _ = self._step(np.where(cols, 150, 90).astype(np.int16) * np.ones((32, 1), np.int16))
        chg = o["cls"] == C.C_CHANGED
        self.assertTrue(chg.any())
        self.assertFalse((o["flags"][chg] & C.FLAG_SHADE_ONLY).any())

    def test_flag_only_on_changed(self):
        o, _, _ = self._step(np.full((32, 64), 100, np.int16))
        self.assertFalse((o["flags"] & C.FLAG_SHADE_ONLY).any())


class TestParamsArgUsed(unittest.TestCase):  # RCORE-7
    def test_mv_ratio_from_passed_params(self):
        s, gw, gh = 16, 8, 4
        r = np.random.RandomState(5)
        big = (r.randint(0, 40, (gh * s, gw * s + 64)) + 2 * np.arange(gw * s + 64)[None, :]).astype(np.int16)
        prev = big[:, 32:32 + gw * s]
        cur = big[:, 28:28 + gw * s] + r.randint(-2, 3, (gh * s, gw * s)).astype(np.int16)
        t0, t1 = grid_tiles(gw, gh, s, prev), grid_tiles(gw, gh, s, cur)

        def n_moved(params):
            st = C.ClassState.init(t0)
            C.classify_first(st, t0, y_cur=prev)
            o, _, _ = C.classify_tiles(st, t1, np.zeros(len(t1), bool), y_cur=cur, grid=(s, gw, gh), params=params)
            return int((o["cls"] == C.C_MOVED).sum())
        p16 = dict(P.PARAMS)
        p16["MV_SAD_RATIO_DEN"] = 16
        self.assertGreater(n_moved(None), 0)
        self.assertEqual(n_moved(p16), 0)


class TestBoundaries(unittest.TestCase):  # RCORE-8
    def _state(self, n):
        t = mk(n)
        st = C.ClassState.init(t)
        C.classify_first(st, t)
        return t, st

    def test_frozen_cov_exactly_at_threshold(self):
        for n_act, expect in ((10, C.C_FROZEN), (9, C.C_UNCHANGED)):  # 10/1000 == 10000 ppm exactly
            t, st = self._state(1000)
            st.still_run[30] = 7
            st.pre_act[30] = 8
            t1 = t.copy()
            t1["y_q4"][:n_act] = 5000
            eq = np.ones(1000, bool)
            eq[:n_act] = False
            o, _, s = C.classify_tiles(st, t1, eq)
            self.assertEqual(int(o["cls"][30]), expect)

    def test_flicker_window_is_bits_0_to_3(self):
        t, st = self._state(2)
        st.hot[:] = 1
        st.hist16[:] = 0b1011   # new hist 0b10111: one flip in bits 0..3, a second only at bit3-4
        t1 = t.copy()
        t1["y_q4"] += 200
        st.prev2["y_q4"] += 400   # not A-B-A
        o, _, _ = C.classify_tiles(st, t1, np.zeros(2, bool))
        self.assertTrue((o["cls"] == C.C_CHANGED).all())

    def test_mv_texit_gate(self):
        s, gw, gh = 16, 8, 4
        rng = np.random.RandomState(5)
        big = rng.randint(0, 256, (gh * s, gw * s + 64)).astype(np.int16)
        prev = big[:, 32:32 + gw * s]
        cur = big[:, 28:28 + gw * s] + rng.choice([-9, 9], (gh * s, gw * s)).astype(np.int16)
        self.assertEqual(C.global_vector(cur, prev, s), (4, 0))  # vector found, but SAD/px ~9 >> T_exit/16
        t0, t1 = grid_tiles(gw, gh, s, prev), grid_tiles(gw, gh, s, cur)
        st = C.ClassState.init(t0)
        C.classify_first(st, t0, y_cur=prev)
        o, _, summ = C.classify_tiles(st, t1, np.zeros(len(t1), bool), y_cur=cur, grid=(s, gw, gh))
        self.assertFalse((o["cls"] == C.C_MOVED).any())
        self.assertIsNone(summ["global_mv"])

    def test_axis_shift_normalises_overlap(self):
        # true shift +2; without the W//(W-|d|) normalisation the tiny overlap at d=-14 wins
        prev = np.array([172, 47, 117, 192, 67, 195, 103, 9, 21, 36, 87, 70, 88, 140, 58, 193], np.int64)
        cur = np.array([61, 196, 169, 45, 119, 190, 69, 192, 101, 10, 21, 33, 87, 72, 91, 137], np.int64)
        self.assertEqual(C._axis_shift(cur, prev, 15), 2)


if __name__ == "__main__":
    unittest.main()
