"""Tests for eval/v2x/st_pixtrans.py sub-tile motion algorithm."""

import unittest
import numpy as np
import eval.v2x.st_pixtrans as st_pixtrans
from eval.v2x.st_pixtrans import subtile_motion


class TestV2XStPixTrans(unittest.TestCase):

    def setUp(self):
        st_pixtrans.reset_ops()
        self.W = 128
        self.H = 128
        self.s = 32
        self.bg_color = 200
        self.fg_color = 50

    def _make_frame(self, h=None, w=None):
        height = self.H if h is None else h
        width = self.W if w is None else w
        return np.full((height, width), self.bg_color, dtype=np.uint8)

    def test_caret_moving_right(self):
        """2x16 caret moving 12 px right inside a 32 px tile."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Place caret inside tile (1,1) [x=32..63, y=32..63]
        # In frame 1: caret 2x16 at x=40..41, y=40..55
        f1[40:56, 40:42] = self.fg_color
        # In frame 2: moves 12 px right -> x=52..53, y=40..55
        f2[40:56, 52:54] = self.fg_color

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=31)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (12, 0))
        self.assertGreaterEqual(n, 1)

    def test_caret_crossing_tile_border(self):
        """Caret crossing a tile border from tile (1,1) into tile (2,1)."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Caret 2x16 at x=58..59 in tile (1,1) [x=32..63]
        f1[40:56, 58:60] = self.fg_color
        # Moves 12 px right -> x=70..71 in tile (2,1) [x=64..95]
        f2[40:56, 70:72] = self.fg_color

        # Check tile (1,1)
        res1 = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=31)
        self.assertIsNotNone(res1)
        self.assertEqual((res1[0], res1[1]), (12, 0))

        # Check tile (2,1)
        res2 = subtile_motion(f1, f2, s=self.s, tile_xy=(2, 1), max_d=31)
        self.assertIsNotNone(res2)
        self.assertEqual((res2[0], res2[1]), (12, 0))

    def test_text_carrier_moving(self):
        """3-tile wide text carrier moving 5 px."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Text carrier spanning 3 tiles: (1,1), (2,1), (3,1) -> x=32..127, y=32..63
        # Add text pattern on carrier
        f1[40:56, 35:120] = 100
        f1[42:50, 40:115:3] = 30  # text pattern

        # Move 5 px right
        f2[40:56, 40:125] = 100
        f2[42:50, 45:120:3] = 30

        for tx in (1, 2, 3):
            res = subtile_motion(f1, f2, s=self.s, tile_xy=(tx, 1), max_d=31)
            self.assertIsNotNone(res)
            self.assertEqual((res[0], res[1]), (5, 0))

    def test_window_drag_box(self):
        """Window-drag box moving 4 px right and 2 px down."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # 40x40 window box in tile (1,1)
        f1[35:75, 35:75] = 150
        f1[37:40, 37:73] = 80  # title bar

        # Moves (dx=4, dy=2)
        f2[37:77, 39:79] = 150
        f2[39:42, 41:77] = 80

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=31)
        self.assertIsNotNone(res)
        self.assertEqual((res[0], res[1]), (4, 2))

    def test_scroll_exposed_bottom_tile(self):
        """Scroll whose exposed bottom tile contains new content must return None."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Scroll upwards by 4 px: dy = -4
        f1[32:96, 32:64] = 120  # content
        f2[28:92, 32:64] = 120  # moved up

        # Exposed bottom tile (tile (1,3) -> y=96..127) gets new content in f2
        f2[96:128, 32:64] = 77  # new content not present in f1

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 3), max_d=31)
        self.assertIsNone(res)

    def test_typing_step_with_new_glyph(self):
        """Typing step where a glyph appears where caret was must return None."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Caret in f1 at x=40..41, y=40..55 in tile (1,1)
        f1[40:56, 40:42] = self.fg_color

        # Caret moves to x=52..53 in f2
        f2[40:56, 52:54] = self.fg_color
        # A new glyph appears at x=38..43, y=42..50 where caret was
        f2[42:51, 38:44] = 10

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=31)
        self.assertIsNone(res)

    def test_flat_and_unchanged_tiles(self):
        """Flat tiles and unchanged tiles return None."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Unchanged frame
        res_unchanged = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=31)
        self.assertIsNone(res_unchanged)

        # Flat tile color shift (e.g., solid 200 changes to solid 150)
        f2_flat = self._make_frame()
        f2_flat[32:64, 32:64] = 150
        res_flat = subtile_motion(f1, f2_flat, s=self.s, tile_xy=(1, 1), max_d=31)
        self.assertIsNone(res_flat)

    def test_frame_height_not_multiple_of_8(self):
        """Frames whose height is not a multiple of 8 (e.g. 180 or 1088)."""
        W, H, s = 320, 180, 16
        f1 = np.full((H, W), self.bg_color, dtype=np.uint8)
        f2 = np.full((H, W), self.bg_color, dtype=np.uint8)

        # Bottom edge tile: ty = 11 (y=176..179, height 4 px, clipped at H=180)
        f1[176:180, 32:48] = 40
        f2[176:180, 34:50] = 40  # moves 2 px right

        res = subtile_motion(f1, f2, s=s, tile_xy=(2, 11), max_d=15)
        self.assertIsNotNone(res)
        self.assertEqual((res[0], res[1]), (2, 0))

    def test_max_d_boundary(self):
        """Max_d boundary: motion <= max_d succeeds, > max_d fails."""
        f1 = self._make_frame()
        f2_ok = self._make_frame()
        f2_far = self._make_frame()

        # Caret at x=40..41
        f1[40:56, 40:42] = self.fg_color

        # Moves 15 px right (max_d = 15) -> x=55..56
        f2_ok[40:56, 55:57] = self.fg_color
        res_ok = subtile_motion(f1, f2_ok, s=self.s, tile_xy=(1, 1), max_d=15)
        self.assertIsNotNone(res_ok)
        self.assertEqual((res_ok[0], res_ok[1]), (15, 0))

        # Moves 16 px right (with max_d = 15) -> x=56..57
        f2_far[40:56, 56:58] = self.fg_color
        res_far = subtile_motion(f1, f2_far, s=self.s, tile_xy=(1, 1), max_d=15)
        self.assertIsNone(res_far)

    def test_ambiguous_flat_background_cases(self):
        """Ambiguous cases with multiple satisfying vectors return None."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # A long uniform horizontal line spanning the full width of the frame
        # moving 4 px down. Any dx produces an identical match.
        f1[40:42, 0:128] = 10
        f2[44:46, 0:128] = 10  # dy = 4, but dx is completely ambiguous

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=15)
        self.assertIsNone(res)

    def test_ops_counter(self):
        """OPS increases during subtile_motion and resets with reset_ops."""
        st_pixtrans.reset_ops()
        self.assertEqual(st_pixtrans.OPS, 0)

        f1 = self._make_frame()
        f2 = self._make_frame()
        f1[40:56, 40:42] = self.fg_color
        f2[40:56, 52:54] = self.fg_color

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=31)
        self.assertIsNotNone(res)
        self.assertGreater(st_pixtrans.OPS, 0)

        st_pixtrans.reset_ops()
        self.assertEqual(st_pixtrans.OPS, 0)


if __name__ == "__main__":
    unittest.main()
