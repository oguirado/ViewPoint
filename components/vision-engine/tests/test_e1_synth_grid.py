"""E1 C3: SQ-ID at the Grid level (T-R1 / T-R1-neg) at 720p, 1080p, 4K and 1024x1024."""
import unittest

import numpy as np

from visioncore import ids, layout
from visioncore.errors import VqaError
from visioncore.grid import Grid, aligned_block, field_ids_of_rect

RES = ((1280, 720), (1920, 1080), (3840, 2160), (1024, 1024))


class TestGridIds(unittest.TestCase):
    def test_mortons_ascending_and_rank_ids_roundtrip(self):
        for W, H in RES:
            g = Grid(W, H)
            self.assertTrue(np.all(np.diff(g.mortons.astype(np.int64)) > 0), (W, H))
            for r in (0, 1, g.N // 2, g.N - 1):
                fid = ids.field_id(7, int(g.mortons[r]), W, H)
                kind, d = ids.parse_any(fid, W, H)
                self.assertEqual((kind, d["frame"], d["morton"]), ("field", 7, int(g.mortons[r])))
                self.assertEqual(ids.field_id(d["frame"], d["morton"], W, H), fid)
                self.assertEqual(layout.morton(int(g.tx[r]), int(g.ty[r])), int(g.mortons[r]))

    def test_rect_ids_cover_exactly_the_tiles(self):
        for W, H in RES:
            g = Grid(W, H)
            rect = (g.s + 1, g.s * 2 + 3, g.s * 2, g.s)  # straddles tile borders
            got = {ids.parse_any(i, W, H)[1]["morton"] for i in field_ids_of_rect(4, rect, g)}
            x, y, w, h = rect
            want = {layout.morton(tx, ty) for tx in range(x // g.s, (x + w - 1) // g.s + 1)
                    for ty in range(y // g.s, (y + h - 1) // g.s + 1)}
            self.assertEqual(got, want, (W, H))

    def test_last_cell_id_valid_and_one_past_rejected(self):
        for W, H in RES:
            g = Grid(W, H)
            ids.parse_any(ids.field_id(0, layout.morton(g.gw - 1, g.gh - 1), W, H), W, H)
            for tx, ty in ((g.gw, 0), (0, g.gh)):
                with self.assertRaises(VqaError) as c:
                    ids.parse_any("f0:t%04x" % layout.morton(tx, ty), W, H)
                self.assertEqual(c.exception.code, "E_VC_ID_RANGE", (W, H, tx, ty))

    def test_syntax_negatives(self):
        for bad in ("f84:t0a4", "f084:t0a40", "f84:t0A40"):
            with self.assertRaises(VqaError) as c:
                ids.parse_any(bad, 1280, 720)
            self.assertEqual(c.exception.code, "E_VC_ID_SYNTAX", bad)

    def test_pad_shape_and_edge_replication(self):
        for W, H in RES:
            g = Grid(W, H)
            rgb = (np.arange(H * W * 3, dtype=np.uint32) % 251).astype(np.uint8).reshape(H, W, 3)
            p = g.pad(rgb)
            self.assertEqual(p.shape, (g.gh * g.s, g.gw * g.s, 3), (W, H))
            self.assertTrue(np.array_equal(p[:H, :W], rgb))
            if p.shape[0] > H:
                self.assertTrue(np.array_equal(p[-1, :W], rgb[-1]))
            if p.shape[1] > W:
                self.assertTrue(np.array_equal(p[:H, -1], rgb[:, -1]))
            self.assertEqual(int(g.pad_flags().sum()) > 0, p.shape[:2] != (H, W))

    def test_aligned_block_contains_bbox(self):
        for W, H in RES:
            g = Grid(W, H)
            k, m0, m1 = aligned_block((1, 1, g.gw - 1, g.gh - 1))
            self.assertLess(m0, m1)
            self.assertEqual(m1 - m0 + 1, 4 ** k)
            self.assertLessEqual(m0, layout.morton(1, 1))
            self.assertGreaterEqual(m1, layout.morton(g.gw - 1, g.gh - 1))

    def test_grid_refusals(self):
        for W, H in ((1, 70000), (70000, 1)):
            with self.assertRaises(VqaError) as c:
                Grid(W, H)
            self.assertEqual(c.exception.code, "E_GRID")
        g = Grid(4080, 256)
        self.assertLessEqual(max(g.gw, g.gh), 255)


if __name__ == "__main__":
    unittest.main()
