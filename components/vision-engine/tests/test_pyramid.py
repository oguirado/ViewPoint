"""S3 pyramid tests (SPEC_CORE §4): build, bound, AABB query, bounded covering select, zero-subtree skip."""
import unittest

import numpy as np

from visioncore import layout
from visioncore.errors import VqaError
from visioncore.grid import Grid
from visioncore.pyramid import Pyramid, levels_for, r_sel

DT = np.dtype([("morton", "<u2"), ("cls", "u1"), ("resid", "u1")])


def make(W, H, dirty):
    """dirty: {(tx,ty): resid}; returns (grid, tiles, pyramid)."""
    g = Grid(W, H)
    t = np.zeros(g.N, dtype=DT)
    t["morton"] = g.mortons
    for (tx, ty), r in dirty.items():
        i = int(g.order[ty, tx])
        t["resid"][i] = r
        t["cls"][i] = 2
    return g, t, Pyramid(t, g)


def leaves_under(p, level, i):
    lo, hi = i << (2 * level), (i + 1) << (2 * level)
    return lo, hi


class TestPyramid(unittest.TestCase):
    def test_levels(self):
        self.assertEqual([levels_for(*a) for a in ((80, 45), (60, 34), (64, 64), (1, 1), (2, 1))], [7, 6, 6, 1, 1])
        self.assertEqual(Pyramid(make(1280, 720, {})[1], Grid(1280, 720)).L, 7)

    def test_empty_select(self):
        _, _, p = make(1280, 720, {})
        self.assertEqual(p.select(16), [])
        self.assertEqual(p.last_visits, 0)

    def test_max_is_exact_per_node(self):
        rng = np.random.default_rng(1)
        d = {(int(rng.integers(0, 80)), int(rng.integers(0, 45))): int(rng.integers(1, 255)) for _ in range(300)}
        g, t, p = make(1280, 720, d)
        leaf = p.mx[0]
        for k in range(p.L + 1):
            blk = leaf.reshape(-1, 4 ** k).max(axis=1)
            self.assertTrue(np.array_equal(blk, p.mx[k]))
        self.assertEqual(int(p.mx[p.L][0]), max(d.values()))
        self.assertEqual(int(p.cnt[p.L][0]), len(d))
        self.assertEqual(int(p.real[p.L][0]), g.N)

    def test_absent_cells_zero(self):
        g, _, p = make(1280, 720, {(79, 44): 200})
        self.assertEqual(int(p.real[0].sum()), g.N)
        self.assertEqual(int(p.mx[0][layout.morton(79, 44)]), 200)

    def test_query_aabb(self):
        g, _, p = make(1280, 720, {(5, 5): 90, (6, 5): 40})
        n = p.query_aabb(5 * 16, 5 * 16, 16, 16)  # one tile
        self.assertEqual(n, [(0, layout.morton(5, 5), 90, 1)])
        n = p.query_aabb(5 * 16, 5 * 16, 40, 16)  # tiles 5..7
        self.assertLessEqual(len(n), 4)
        self.assertGreaterEqual(max(x[2] for x in n), 90)
        self.assertEqual(p.max_aabb(0, 0, 1280, 720), 90)
        self.assertEqual(p.max_aabb(0, 0, 16, 16), 0)

    def test_query_aabb_bound_and_size(self):
        rng = np.random.default_rng(7)
        d = {(int(rng.integers(0, 80)), int(rng.integers(0, 45))): int(rng.integers(1, 255)) for _ in range(200)}
        g, t, p = make(1280, 720, d)
        for _ in range(200):
            x, y = int(rng.integers(0, 1280)), int(rng.integers(0, 720))
            w, h = int(rng.integers(1, 400)), int(rng.integers(1, 400))
            nodes = p.query_aabb(x, y, w, h)
            self.assertLessEqual(len(nodes), 4)
            tx0, ty0 = x // 16, y // 16
            tx1, ty1 = (min(x + w, 1280) - 1) // 16, (min(y + h, 720) - 1) // 16
            truth = max([r for (tx, ty), r in d.items() if tx0 <= tx <= tx1 and ty0 <= ty <= ty1], default=0)
            self.assertGreaterEqual(p.max_aabb(x, y, w, h), truth)

    def test_query_outside_empty(self):
        _, _, p = make(1280, 720, {(0, 0): 99})
        self.assertEqual(p.query_aabb(2000, 0, 10, 10), [])
        self.assertEqual(p.query_aabb(0, 0, 0, 5), [])

    def test_select_cover_and_bound(self):
        rng = np.random.default_rng(3)
        rs = r_sel()
        for trial in range(30):
            d = {(int(rng.integers(0, 60)), int(rng.integers(0, 34))): int(rng.integers(1, 255))
                 for _ in range(int(rng.integers(1, 400)))}
            g, t, p = make(1920, 1080, d)
            for B in (1, 3, 8, 40, 500):
                out = p.select(B)
                self.assertLessEqual(len(out), B)
                spans = [(m0, m0 + 4 ** lv) for lv, m0, _, _ in out]
                for (tx, ty), r in d.items():
                    if r > rs:
                        m = layout.morton(tx, ty)
                        self.assertTrue(any(a <= m < b for a, b in spans), (B, tx, ty))
                spans.sort()
                for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
                    self.assertLessEqual(a1, b0)  # disjoint

    def test_select_exact_when_budget_large(self):
        g, _, p = make(1280, 720, {(1, 1): 90, (70, 40): 120})
        out = p.select(64)
        self.assertEqual(sorted(o[:1] + o[2:] for o in out), [(0, 90, 1), (0, 120, 1)])

    def test_zero_subtree_skip(self):
        g, _, p = make(1280, 720, {(1, 1): 90})
        p.select(64)
        self.assertLessEqual(p.last_visits, p.L + 1)  # only the path to one leaf

    def test_below_threshold_skipped(self):
        _, _, p = make(1280, 720, {(3, 3): r_sel()})  # resid == R_SEL is not > R_SEL
        self.assertEqual(p.select(8), [])

    def test_full_dirty_emits_root_early(self):
        g = Grid(1024, 1024)
        t = np.zeros(g.N, dtype=DT)
        t["morton"], t["cls"], t["resid"] = g.mortons, 2, 50
        p = Pyramid(t, g)
        out = p.select(100)
        self.assertEqual(out, [(6, 0, 50, 4096)])

    def test_deterministic(self):
        d = {(i % 60, i % 34): 10 + i % 200 for i in range(100)}
        a = make(1920, 1080, d)[2].select(20)
        b = make(1920, 1080, d)[2].select(20)
        self.assertEqual(a, b)

    def test_bad_budget(self):
        _, _, p = make(1280, 720, {})
        for B in (0, -1, 2.5):
            with self.assertRaises(VqaError) as c:
                p.select(B)
            self.assertEqual(c.exception.rc, 2)

    def test_node_id(self):
        _, _, p = make(1280, 720, {})
        self.assertEqual(p.node_id(42, 2, 0x68 >> 4), "p42:L2:%04x" % ((0x68 >> 4) << 4))


if __name__ == "__main__":
    unittest.main()
