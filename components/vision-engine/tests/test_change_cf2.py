"""CF2 (v12cf): mixed-motion island pool, search range = tile size, resid field units."""
import unittest

import numpy as np

from visioncore import change as C
from tests.test_change_fix import grid_tiles


def _step(prev, cur, grid):
    s, gw, gh = grid
    t0, t1 = grid_tiles(gw, gh, s, prev), grid_tiles(gw, gh, s, cur)
    st = C.ClassState.init(t0)
    C.classify_first(st, t0, y_cur=prev)
    o, _, summ = C.classify_tiles(st, t1, np.zeros(len(t1), bool), y_cur=cur, grid=grid)
    return o, summ, t0, t1


class TestMixedIslandPool(unittest.TestCase):
    def test_touching_opposite_sprites_each_get_own_vector(self):
        s, gw, gh = 16, 20, 6
        rng = np.random.RandomState(7)
        spr = lambda: (rng.randint(0, 64, (3 * s, 3 * s)) + 3 * np.arange(3 * s)[None, :] + 60).astype(np.int16)
        a, b = spr(), spr()
        prev = np.full((gh * s, gw * s), 90, np.int16)
        cur = prev.copy()
        prev[s:4 * s, s:4 * s] = a
        cur[s:4 * s, s + 4:4 * s + 4] = a
        prev[s:4 * s, 5 * s:8 * s] = b  # 8-connected with a via edge tiles: one island
        cur[s:4 * s, 5 * s - 4:8 * s - 4] = b
        # isolated sprites supply the +4 / -4 island vectors; the merged pair's own island vector cancels to (0,0)
        c_, d_ = spr(), spr()
        prev[s:4 * s, 10 * s:13 * s] = c_
        cur[s:4 * s, 10 * s + 4:13 * s + 4] = c_
        prev[s:4 * s, 16 * s:19 * s] = d_
        cur[s:4 * s, 16 * s - 4:19 * s - 4] = d_
        o, _, _, _ = _step(prev, cur, (s, gw, gh))
        mv = o["cls"] == C.C_MOVED
        mt = {int(m): k for k, m in enumerate(o["morton"])}
        for (x, y), v in {(2, 2): 4, (3, 3): 4, (6, 2): -4, (7, 3): -4}.items():  # interior tiles of the merged pair
            k = mt[C.layout.morton(x, y)]
            self.assertEqual((int(o["cls"][k]), int(o["mv_dx"][k]), int(o["mv_dy"][k])), (C.C_MOVED, v, 0), (x, y))
        vecs = {(int(x), int(y)) for x, y in zip(o["mv_dx"][mv], o["mv_dy"][mv])}
        self.assertEqual(vecs, {(4, 0), (-4, 0)})
        self.assertFalse((o["mv_dy"][mv] != 0).any())


class TestSearchRange(unittest.TestCase):
    def test_shift_beyond_tile_size_unreachable(self):
        s = 16
        rng = np.random.RandomState(2)
        big = rng.randint(0, 256, (48, 128 + 64)).astype(np.int16)
        prev = big[:, 64:64 + 128]
        self.assertEqual(C.global_vector(big[:, 44:44 + 128], prev, s), (0, 0))  # 20 px > s
        self.assertEqual(C.global_vector(big[:, 56:56 + 128], prev, s), (8, 0))  # 8 px <= s


class TestResidUnits(unittest.TestCase):
    def test_resid_is_r_over_4_and_saturates(self):
        s, gw, gh = 16, 2, 1
        prev = np.full((s, 2 * s), 100, np.int16)
        cur = prev.copy()
        cur[:, :s] += 40
        cur[:, s:] += 120
        o, _, t0, t1 = _step(prev, cur, (s, gw, gh))
        for i in range(2):
            r = int(C.residual(t1, t0)[i])
            self.assertEqual(int(o["resid"][i]), min(255, r >> 2))
        self.assertEqual(int(o["resid"].max()), 255)  # saturated field != q4


if __name__ == "__main__":
    unittest.main()
