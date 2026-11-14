"""JC-6: every node select() returns on a legal grid must format and parse through the id registry."""
import unittest

from tests.test_pyramid import make
from visioncore import ids
from visioncore.pyramid import Pyramid, id_level_cap, levels_for


class TestLegalGridIds(unittest.TestCase):
    def test_wide_grid_level8_nodes_have_ids(self):
        g, t, p = make(4080, 16, {(0, 0): 200, (254, 0): 90})
        self.assertEqual((g.gw, g.gh, p.L), (255, 1, 8))
        for B in (1, 4, 16, 100):
            for lvl, m0, mx, cnt in p.select(B, 0):
                nid = p.node_id(7, lvl, m0 >> (2 * lvl))
                self.assertEqual(ids.parse_any(nid)[0], "node")

    def test_full_dirty_wide_grid(self):
        _, _, p = make(4080, 16, {(x, 0): 100 for x in range(255)})
        for lvl, m0, _, _ in p.select(8, 0):
            p.node_id(0, lvl, m0 >> (2 * lvl))

    def test_selected_nodes_cover_dirty_tiles(self):
        _, _, p = make(4080, 16, {(5, 0): 200, (250, 0): 90})
        nodes = p.select(8, 0)
        for m in (5, 250):
            from visioncore import layout
            mm = layout.morton(m, 0)
            self.assertTrue(any(n[1] <= mm < n[1] + 4 ** n[0] for n in nodes), m)

    def test_id_cap_bounds(self):
        self.assertEqual(id_level_cap(3), 3)
        self.assertEqual(id_level_cap(levels_for(255, 1)), 8)  # JCODE-1: L8 legal (SPEC_INT R1)


if __name__ == "__main__":
    unittest.main()
