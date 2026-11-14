"""Tests for eval/v2x/st_ccdisp.py connected-component displacement algorithm."""

import unittest
import numpy as np
import eval.v2x.st_ccdisp as st


class TestV2XStCCDisp(unittest.TestCase):

    def setUp(self):
        st.reset_ops()
        self.W = 128
        self.H = 128
        self.s = 32
        self.bg_color = 200
        self.fg_color = 50

    def _make_frame(self, h=None, w=None):
        if h is None:
            h = self.H
        if w is None:
            w = self.W
        return np.full((h, w), self.bg_color, dtype=np.uint8)

    def test_caret_moving_right(self):
        """2x16 caret moving 12 px right inside a 32 px tile."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Caret 2x16 inside tile (1,1) [x:32..64, y:32..64]
        # Frame 1: x=42..44, y=40..56
        f1[40:56, 42:44] = self.fg_color
        # Frame 2: moves 12 px right -> x=54..56, y=40..56 (entirely in tile (1,1))
        f2[40:56, 54:56] = self.fg_color

        res = st.subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (12, 0))
        self.assertGreaterEqual(n, 1)

    def test_caret_crossing_tile_border(self):
        """Caret crossing a tile border (moves from tile 0,0 into tile 1,0)."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Caret 2x16 at x=20, y=8 in tile (0,0)
        f1[8:24, 20:22] = self.fg_color
        # Moves 12 px right -> x=32, y=8 in tile (1,0)
        f2[8:24, 32:34] = self.fg_color

        res1 = st.subtile_motion(f1, f2, s=self.s, tile_xy=(1, 0), max_d=16)
        self.assertIsNotNone(res1)
        self.assertEqual(res1[:2], (12, 0))

    def test_text_carrier_moving(self):
        """A text carrier moving 5 px."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Carrier 48 px wide in tile (1,1) [x:32..64, y:32..64], neighbourhood x:0..96
        # f1: x=40..88, y=40..56
        pattern = np.full((16, 48), self.fg_color, dtype=np.uint8)
        f1[40:56, 40:88] = pattern
        # Moves 5 px right -> x=45..93
        f2[40:56, 45:93] = pattern

        res = st.subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (5, 0))
        self.assertGreaterEqual(n, 1)

    def test_window_drag_box(self):
        """Window-drag box moving 4 px right and 2 px down."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Box 20x20 in tile (1,1) [x:32..64, y:32..64]
        # f1: x=36..56, y=36..56
        f1[36:56, 36:56] = self.fg_color
        # Moves dx=4, dy=2 -> x=40..60, y=38..58 (entirely inside tile (1,1))
        f2[38:58, 40:60] = self.fg_color

        res = st.subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (4, 2))
        self.assertGreaterEqual(n, 1)

    def test_scroll_exposed_bottom_tile(self):
        """Scroll whose exposed bottom tile contains new content must return None."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Entire frame scrolls up by 8 px (dy = -8)
        f2[0:120, :] = f1[8:128, :]
        # Exposed bottom tile (y:96..128) in f2 gets new content not in f1
        f2[96:128, :] = 120

        # Exposed bottom tile (0, 3) spanning y:96..128
        res = st.subtile_motion(f1, f2, s=self.s, tile_xy=(0, 3), max_d=16)
        self.assertIsNone(res)

    def test_typing_step_glyph_appears(self):
        """A typing step where a glyph appears where the caret was must return None."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Frame 1: Caret at x=42, y=40 (2x16) in tile (1,1)
        f1[40:56, 42:44] = self.fg_color

        # Frame 2: Glyph 'A' appears at x=42, y=40 (8x16), caret moves to x=54, y=40
        f2[40:56, 42:50] = 75
        f2[40:56, 54:56] = self.fg_color

        # For tile (1,1), new glyph content appeared so tile is modified non-pure translation
        res = st.subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=16)
        self.assertIsNone(res)

    def test_flat_and_unchanged_tiles(self):
        """Flat tiles and unchanged tiles return None."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Unchanged
        res = st.subtile_motion(f1, f2, s=self.s, tile_xy=(0, 0), max_d=16)
        self.assertIsNone(res)

        # Flat tile modified in f2 to flat color 100
        f2[0:32, 0:32] = 100
        res_flat = st.subtile_motion(f1, f2, s=self.s, tile_xy=(0, 0), max_d=16)
        self.assertIsNone(res_flat)

    def test_non_multiple_of_8_height(self):
        """Frames whose height is not a multiple of 8 (e.g., 100 px)."""
        h = 100  # Not a multiple of 8
        w = 128
        f1 = self._make_frame(h=h, w=w)
        f2 = self._make_frame(h=h, w=w)

        # Caret in tile (1, 2) moving 12 px right
        f1[68:84, 42:44] = self.fg_color
        f2[68:84, 54:56] = self.fg_color

        res = st.subtile_motion(f1, f2, s=32, tile_xy=(1, 2), max_d=16)
        self.assertIsNotNone(res)
        self.assertEqual(res[:2], (12, 0))

    def test_max_d_boundary(self):
        """Motion larger than max_d returns None."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        f1[40:56, 42:44] = self.fg_color
        # Shifted by 20 px right (max_d = 16)
        f2[40:56, 62:64] = self.fg_color

        res = st.subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=16)
        self.assertIsNone(res)

    def test_ambiguous_flat_background_cases(self):
        """Ambiguous cases with multiple candidate components return None."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Two identical carets moving in opposite directions in tile (1,1)
        f1[40:56, 36:38] = self.fg_color
        f1[40:56, 58:60] = self.fg_color

        f2[40:56, 40:42] = self.fg_color  # one moved right 4px
        f2[40:56, 54:56] = self.fg_color  # one moved left 4px

        res = st.subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=16)
        self.assertIsNone(res)

    def test_ops_increases_and_resets(self):
        """OPS increases with calls and resets with reset_ops()."""
        st.reset_ops()
        self.assertEqual(st.OPS, 0)

        f1 = self._make_frame()
        f2 = self._make_frame()
        f1[40:56, 42:44] = self.fg_color
        f2[40:56, 54:56] = self.fg_color

        st.subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=16)
        ops_after_call = st.OPS
        self.assertGreater(ops_after_call, 0)

        st.reset_ops()
        self.assertEqual(st.OPS, 0)


if __name__ == "__main__":
    unittest.main()
