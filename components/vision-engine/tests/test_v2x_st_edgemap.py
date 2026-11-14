"""Tests for edge-map matching sub-tile motion algorithm eval/v2x/st_edgemap.py."""

import unittest
import numpy as np
import eval.v2x.st_edgemap as st_edgemap
from eval.v2x.st_edgemap import subtile_motion, OPS, reset_ops


class TestV2XSTEdgemap(unittest.TestCase):

    def setUp(self):
        reset_ops()
        self.W = 128
        self.H = 128
        self.s = 32
        self.bg_color = 200
        self.fg_color = 50

    def _make_frame(self, h=None, w=None):
        height = h if h is not None else self.H
        width = w if w is not None else self.W
        return np.full((height, width), self.bg_color, dtype=np.uint8)

    def test_caret_moving_right(self):
        """1. A 2x16 caret moving 12 px right inside a 32 px tile."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Caret inside tile (1, 0) at x=40, y=8 (tile 1,0 spans x=32..63, y=0..31)
        f1[8:24, 40:42] = self.fg_color
        # Moves 12 px right -> x=52, y=8 in frame 2
        f2[8:24, 52:54] = self.fg_color

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 0), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (12, 0))
        self.assertGreaterEqual(n, 1)
        self.assertIsInstance(dx, int)
        self.assertIsInstance(dy, int)
        self.assertIsInstance(n, int)

    def test_caret_crossing_tile_border(self):
        """2. A caret crossing a tile border (moves from tile 0,0 to tile 1,0)."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Caret 2x16 at x=26, y=8 in tile (0,0)
        f1[8:24, 26:28] = self.fg_color
        # Moves 12 px right -> x=38, y=8 in tile (1,0)
        f2[8:24, 38:40] = self.fg_color

        # Tile (1,0) (spanning x=32..63) receives caret from tile (0,0)
        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 0), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (12, 0))
        self.assertGreaterEqual(n, 1)

    def test_text_carrier_moving(self):
        """3. A 3-tile wide text carrier moving 5 px."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Non-periodic text carrier with distinctive asymmetric letter features
        # Tile (1,0) spans x=32..63
        f1[10:22, 35:37] = self.fg_color
        f1[10:22, 43:47] = self.fg_color
        f1[10:22, 55:58] = self.fg_color

        # Shifted by dx=5, dy=0
        f2[10:22, 40:42] = self.fg_color
        f2[10:22, 48:52] = self.fg_color
        f2[10:22, 60:63] = self.fg_color

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 0), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (5, 0))
        self.assertGreaterEqual(n, 1)

    def test_window_drag_box(self):
        """4. A window-drag box moving 4 and 2 px (dx=4, dy=2)."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Box inside tile (1,1) (x=32..63, y=32..63)
        f1[36:56, 36:56] = self.fg_color
        # Box moved dx=4, dy=2 in frame 2
        f2[38:58, 40:60] = self.fg_color

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (4, 2))
        self.assertGreaterEqual(n, 1)

    def test_scroll_exposed_bottom_tile_returns_none(self):
        """5. A scroll whose exposed bottom tile contains new content must return None."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Content moves up by 8 px
        f1[20:100, 10:50] = self.fg_color
        f2[12:92, 10:50] = self.fg_color
        # Exposed bottom tile gets new unique content
        f2[96:120, 10:50] = 120

        # Query exposed bottom tile (tile (0, 3) spanning y=96..127)
        res = subtile_motion(f1, f2, s=self.s, tile_xy=(0, 3), max_d=16)
        self.assertIsNone(res)

    def test_typing_step_glyph_appears(self):
        """6. A typing step where a glyph appears where the caret was must return None."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Caret in frame 1 at tile (1, 0) (x=40..42, y=8..24)
        f1[8:24, 40:42] = self.fg_color
        # Caret moves right AND new glyph 'A' appears at old caret position
        f2[8:24, 52:54] = self.fg_color
        f2[8:24, 40:46] = 80  # new glyph

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 0), max_d=16)
        self.assertIsNone(res)

    def test_flat_and_unchanged_tiles_return_none(self):
        """7. Flat tiles and unchanged tiles return None."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Unchanged flat frame
        res_unchanged = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=16)
        self.assertIsNone(res_unchanged)

        # Flat tile modified to another flat color
        f3 = self._make_frame()
        f3[32:64, 32:64] = 150
        res_flat = subtile_motion(f1, f3, s=self.s, tile_xy=(1, 1), max_d=16)
        self.assertIsNone(res_flat)

    def test_frame_height_not_multiple_of_8(self):
        """8. Frames whose height is not a multiple of 8 (e.g., H=102, grid-padded 102x128)."""
        H, W = 102, 128
        f1 = self._make_frame(h=H, w=W)
        f2 = self._make_frame(h=H, w=W)

        # Tile (1, 4) spans x=16..31, y=64..79 (with s=16)
        f1[68:76, 20:22] = self.fg_color
        f2[68:76, 24:26] = self.fg_color

        res = subtile_motion(f1, f2, s=16, tile_xy=(1, 4), max_d=15)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (4, 0))
        self.assertGreaterEqual(n, 1)

    def test_max_d_boundary(self):
        """9. Displacement equal to max_d boundary and exceeding max_d."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Caret inside tile (1, 1) at x=40, y=40
        f1[40:56, 40:42] = self.fg_color
        # Move by max_d = 15 -> x=55, y=40
        f2[40:56, 55:57] = self.fg_color

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=15)
        self.assertIsNotNone(res)
        self.assertEqual(res[:2], (15, 0))

        # Move by 16 (> max_d=15) -> should return None
        f3 = self._make_frame()
        f3[40:56, 56:58] = self.fg_color
        res_exceed = subtile_motion(f1, f3, s=self.s, tile_xy=(1, 1), max_d=15)
        self.assertIsNone(res_exceed)

    def test_ambiguous_flat_background_cases(self):
        """10. Ambiguous cases (multiple distinct vectors with equal top score or zero edge support)."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Symmetric pattern inside tile (1,1) that produces multiple identical translation matches
        f1[40:50, 40:50] = self.fg_color
        f2[40:50, 40:50] = self.fg_color
        f1[40:50, 52:62] = self.fg_color
        f2[40:50, 52:62] = self.fg_color

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=16)
        self.assertIsNone(res)

    def test_ops_increases_and_resets(self):
        """11. OPS increases during subtile_motion call and resets with reset_ops."""
        reset_ops()
        self.assertEqual(st_edgemap.OPS, 0)

        f1 = self._make_frame()
        f2 = self._make_frame()
        f1[8:24, 40:42] = self.fg_color
        f2[8:24, 52:54] = self.fg_color

        subtile_motion(f1, f2, s=self.s, tile_xy=(1, 0), max_d=16)
        ops_after = st_edgemap.OPS
        self.assertGreater(ops_after, 0)

        reset_ops()
        self.assertEqual(st_edgemap.OPS, 0)


if __name__ == "__main__":
    unittest.main()
