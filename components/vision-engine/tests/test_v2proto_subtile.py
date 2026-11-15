"""Tests for eval/v2proto_subtile.py sub-tile motion algorithm."""

import unittest
import numpy as np
from eval.v2proto_subtile import subtile_motion


class TestV2ProtoSubtile(unittest.TestCase):

    def setUp(self):
        self.W = 128
        self.H = 128
        self.s = 32
        self.bg_color = 200
        self.fg_color = 50

    def _make_frame(self):
        return np.full((self.H, self.W), self.bg_color, dtype=np.uint8)

    def test_caret_moving_right(self):
        """2x16 caret moving 12 px right inside one 32 px tile."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Caret 2x16 at x=8, y=8 in frame 1 (tile 0,0 spans 0..31 x 0..31)
        f1[8:24, 8:10] = self.fg_color

        # Moves 12 px right -> x=20, y=8 in frame 2
        f2[8:24, 20:22] = self.fg_color

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(0, 0), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n_blocks = res
        self.assertEqual((dx, dy), (12, 0))
        self.assertGreater(n_blocks, 0)

    def test_word_moving_down(self):
        """Word moving 5 px down."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Word (24x10 block) at x=40, y=40 in tile (1,1)
        f1[40:50, 40:64] = self.fg_color

        # Moves 5 px down -> x=40, y=45
        f2[45:55, 40:64] = self.fg_color

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n_blocks = res
        self.assertEqual((dx, dy), (0, 5))
        self.assertGreater(n_blocks, 0)

    def test_glyph_shape_change(self):
        """Glyph changes shape without pure translation (must return None)."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Glyph in frame 1 at tile (0,0)
        f1[8:24, 8:24] = self.fg_color
        # Changed shape in frame 2
        f2[8:24, 8:24] = self.fg_color
        f2[12:20, 12:20] = 100  # inner modification

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(0, 0), max_d=16)
        self.assertIsNone(res)

    def test_move_crossing_tile_boundary(self):
        """Move crossing a tile boundary (moves from tile (0,0) to tile (1,0))."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Caret 2x16 at x=26, y=8 in tile (0,0)
        f1[8:24, 26:28] = self.fg_color

        # Moves 12 px right -> x=38, y=8 in tile (1,0)
        f2[8:24, 38:40] = self.fg_color

        # Tile (1,0) receives the moved caret from tile (0,0)
        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 0), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n_blocks = res
        self.assertEqual((dx, dy), (12, 0))
        self.assertGreater(n_blocks, 0)

    def test_noise_free_background(self):
        """Noise-free background (no change anywhere)."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(0, 0), max_d=16)
        self.assertIsNone(res)

    def test_regression_exposed_bottom_tile(self):
        """Exposed bottom tile at image edge after a move must stay CHANGED (return None)."""
        # Frame size with partial 8x8 block at bottom: H=36, W=32
        W, H = 32, 36
        s = 32
        f1 = np.full((H, W), self.bg_color, dtype=np.uint8)
        f2 = np.full((H, W), self.bg_color, dtype=np.uint8)

        # Move an element from top tile (0,0) into bottom tile (0,1)
        f1[16:32, 4:20] = self.fg_color
        f2[24:36, 4:20] = self.fg_color  # Moved 8 px down into partial bottom tile

        res = subtile_motion(f1, f2, s=s, tile_xy=(0, 1), max_d=16)
        self.assertIsNotNone(res)
        self.assertEqual((res[0], res[1]), (0, 8))

        # Now test exposed tile (0,0) after element moves away (uncovered background)
        f1_exp = np.full((H, W), self.bg_color, dtype=np.uint8)
        f2_exp = np.full((H, W), self.bg_color, dtype=np.uint8)
        f1_exp[4:20, 4:20] = self.fg_color
        res_exp = subtile_motion(f1_exp, f2_exp, s=s, tile_xy=(0, 0), max_d=16)
        self.assertIsNone(res_exp)

    def test_regression_uncovered_background_behind_moved_element(self):
        """Uncovered background behind a moved element must not be claimed as MOVED."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Element A at x=8..24, y=8..24 in tile (0,0)
        f1[8:24, 8:24] = self.fg_color
        # Element A moves to x=40..56 in tile (1,0)
        f2[8:24, 40:56] = self.fg_color

        # Tile (0,0) is now pure background in f2.
        res = subtile_motion(f1, f2, s=self.s, tile_xy=(0, 0), max_d=16)
        self.assertIsNone(res)

    def test_regression_narrow_caret_next_to_text(self):
        """A 1-2 px wide caret with <8 changed pixels next to text is rejected (returns None)."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Text block at x=8..24, y=8..24
        f1[8:24, 8:24] = self.fg_color
        f2[8:24, 8:24] = self.fg_color

        # Caret 1x4 at x=26, y=10..14 (4 changed pixels total) in f1, disappears/moves in f2
        f1[10:14, 26:27] = 30
        f2[10:14, 28:29] = 30

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(0, 0), max_d=16)
        self.assertIsNone(res)

    def test_regression_glyph_change_similar_shape(self):
        """A glyph change of similar shape (e.g. 'e' -> 'o') is rejected (returns None)."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # 'e'-like shape at x=10..22, y=10..22
        f1[10:22, 10:22] = self.fg_color
        f1[14:18, 12:20] = 100

        # 'o'-like shape at x=10..22, y=10..22
        f2[10:22, 10:22] = self.fg_color
        f2[14:18, 14:18] = 100

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(0, 0), max_d=16)
        self.assertIsNone(res)


if __name__ == "__main__":
    unittest.main()
