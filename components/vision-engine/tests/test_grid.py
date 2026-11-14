"""S0 grid tests (SPEC_CORE §1): table, Morton order/contiguity, padding, ids, aligned_block."""
import unittest

import numpy as np

from visioncore import layout
from visioncore.errors import VqaError
from visioncore.grid import Grid, aligned_block, field_ids_of_rect

TABLE = ((1280, 720, 16, 80, 45), (1920, 1080, 32, 60, 34), (3840, 2160, 64, 60, 34),
         (1024, 1024, 16, 64, 64), (2560, 1440, 32, 80, 45), (8192, 8192, 128, 64, 64),
         (8192, 16, 64, 128, 1))


class TestGrid(unittest.TestCase):
    def test_table(self):
        for W, H, s, gw, gh in TABLE:
            g = Grid(W, H)
            self.assertEqual((g.s, g.gw, g.gh, g.N), (s, gw, gh, gw * gh), (W, H))
            self.assertEqual(g.ident, "grid:%dx%d:s%d" % (W, H, s))
            self.assertLessEqual(g.N, 4096)

    def test_bad_size(self):
        for W, H in ((0, 10), (8193, 10), (10, 0)):
            with self.assertRaises(VqaError) as c:
                Grid(W, H)
            self.assertEqual(c.exception.rc, 2)

    def test_morton_order_and_roundtrip(self):
        for W, H in ((1280, 720), (1920, 1080), (3840, 2160)):
            g = Grid(W, H)
            self.assertTrue(np.all(np.diff(g.mortons.astype(np.int64)) > 0))
            self.assertEqual(len(set(g.mortons.tolist())), g.N)
            for k in range(0, g.N, 37):
                self.assertEqual(layout.unmorton(int(g.mortons[k])), (int(g.tx[k]), int(g.ty[k])))
            self.assertTrue(np.all(g.order.reshape(-1)[g.flat] == np.arange(g.N)))
            self.assertTrue(np.all(g.order[g.ty, g.tx] == np.arange(g.N)))

    def test_block_contiguity(self):
        g = Grid(1024, 1024)  # full 64x64: every aligned 2^k block is a contiguous Morton range
        for k in (1, 2, 3):
            for ty0 in range(0, 64, 1 << k):
                for tx0 in range(0, 64, 1 << k):
                    ms = sorted(layout.morton(tx0 + dx, ty0 + dy) for dy in range(1 << k) for dx in range(1 << k))
                    self.assertEqual(ms, list(range(ms[0], ms[0] + 4 ** k)))

    def test_pad_edge_replication(self):
        g = Grid(1280, 720)  # 720 = 45*16 -> no pad; use odd size
        g = Grid(100, 70)
        rgb = np.arange(70 * 100 * 3, dtype=np.uint32).astype(np.uint8).reshape(70, 100, 3)
        p = g.pad(rgb)
        self.assertEqual(p.shape, (g.gh * g.s, g.gw * g.s, 3))
        self.assertTrue(np.array_equal(p[:70, :100], rgb))
        self.assertTrue(np.array_equal(p[70:, :100], np.broadcast_to(rgb[69], p[70:, :100].shape)))
        self.assertTrue(np.array_equal(p[:, 100:], np.broadcast_to(p[:, 99:100], p[:, 100:].shape)))
        pf = g.pad_flags()
        self.assertTrue(pf[g.tx == g.gw - 1].all() and pf[g.ty == g.gh - 1].all())
        self.assertFalse(pf[(g.tx == 0) & (g.ty == 0)].any())

    def test_field_ids_of_rect(self):
        g = Grid(1280, 720)
        self.assertEqual(field_ids_of_rect(7, (0, 0, 16, 16), g), ["f7:t0000"])
        self.assertEqual(len(field_ids_of_rect(7, (15, 15, 2, 2), g)), 4)  # 1x1 corner straddle
        self.assertEqual(field_ids_of_rect(7, (5, 5, 0, 3), g), [])
        with self.assertRaises(VqaError) as c:
            field_ids_of_rect(7, (5000, 5000, 10, 10), g)
        self.assertEqual(c.exception.code, "E_VC_ID_RANGE")

    def test_aligned_block(self):
        self.assertEqual(aligned_block((0, 0, 0, 0)), (0, 0, 0))
        self.assertEqual(aligned_block((2, 2, 3, 3)), (1, layout.morton(2, 2), layout.morton(2, 2) + 3))
        k, m0, m1 = aligned_block((1, 0, 2, 0))  # straddles the 2-boundary -> k=2
        self.assertEqual((k, m0, m1), (2, 0, 15))
        k, m0, m1 = aligned_block((3, 3, 4, 4))
        self.assertEqual((k, m0, m1), (3, 0, 63))


if __name__ == "__main__":
    unittest.main()
