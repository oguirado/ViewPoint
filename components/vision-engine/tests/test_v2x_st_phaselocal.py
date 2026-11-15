"""Tests for eval/v2x/st_phaselocal.py sub-tile motion algorithm."""

import unittest
import numpy as np
import eval.v2x.st_phaselocal as st_phaselocal
from eval.v2x.st_phaselocal import subtile_motion, OPS, reset_ops


class TestV2XStPhaselocal(unittest.TestCase):

    def setUp(self):
        reset_ops()
        self.W = 128
        self.H = 128
        self.s = 32
        self.bg_color = 200
        self.fg_color = 50

    def _make_frame(self, W=None, H=None, bg=None):
        w = W if W is not None else self.W
        h = H if H is not None else self.H
        b = bg if bg is not None else self.bg_color
        return np.full((h, w), b, dtype=np.uint8)

    def test_caret_moving_right(self):
        """2x16 caret moving 12 px right inside a 32 px tile."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Caret 2x16 at x=40, y=40 in tile (1,1) (tile 1,1 spans x=32..63, y=32..63)
        f1[40:56, 40:42] = self.fg_color
        # Moves 12 px right -> x=52, y=40 in frame 2 (still inside tile (1,1))
        f2[40:56, 52:54] = self.fg_color

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (12, 0))
        self.assertGreaterEqual(n, 1)

    def test_caret_crossing_tile_border(self):
        """Caret crossing a tile border (moves from tile (1,1) to tile (2,1))."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Caret 2x16 at x=58, y=40 in tile (1,1)
        f1[40:56, 58:60] = self.fg_color
        # Moves 12 px right -> x=70, y=40 in tile (2,1)
        f2[40:56, 70:72] = self.fg_color

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(2, 1), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (12, 0))
        self.assertGreaterEqual(n, 1)

    def test_text_carrier_moving(self):
        """3-tile wide text carrier moving 5 px."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Text carrier 3 tiles wide (96 px) x 16 px high at x=10, y=40
        np.random.seed(42)
        pattern = np.random.randint(0, 255, size=(16, 96), dtype=np.uint8)

        f1[40:56, 10:106] = pattern
        # Moves 5 px right -> x=15, y=40
        f2[40:56, 15:111] = pattern

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (5, 0))
        self.assertGreaterEqual(n, 1)

    def test_window_drag_box(self):
        """Window-drag box moving 4 px right and 2 px down."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Box 40x40 with interior pattern at x=20, y=20
        np.random.seed(123)
        box_pattern = np.random.randint(0, 255, size=(40, 40), dtype=np.uint8)

        f1[20:60, 20:60] = box_pattern
        # Moves 4 px right, 2 px down -> x=24, y=22
        f2[22:62, 24:64] = box_pattern

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (4, 2))
        self.assertGreaterEqual(n, 1)

    def test_scroll_exposed_bottom_tile(self):
        """A scroll whose exposed bottom tile contains new content must return None."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Content at bottom tile (1, 3) in frame 2 is completely new pixels
        np.random.seed(999)
        f2[96:128, 32:64] = np.random.randint(0, 255, size=(32, 32), dtype=np.uint8)

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 3), max_d=16)
        self.assertIsNone(res)

    def test_typing_step_glyph_appears(self):
        """A typing step where a new glyph appears where caret was must return None."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Caret in frame 1 in tile (1,1)
        f1[40:56, 40:42] = self.fg_color

        # Caret moves and new glyph 'A' appears at (40:56, 40:48)
        f2[40:56, 40:48] = 100  # new glyph
        f2[40:56, 52:54] = self.fg_color  # caret moved

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=16)
        self.assertIsNone(res)

    def test_flat_and_unchanged_tiles(self):
        """Flat tiles and unchanged tiles return None."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # 1. Unchanged tile
        f1[40:56, 40:42] = self.fg_color
        f2[40:56, 40:42] = self.fg_color
        res_unchanged = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=16)
        self.assertIsNone(res_unchanged)

        # 2. Flat tile (all pixels identical in both frames)
        f3 = self._make_frame(bg=128)
        f4 = self._make_frame(bg=128)
        res_flat = subtile_motion(f3, f4, s=self.s, tile_xy=(1, 1), max_d=16)
        self.assertIsNone(res_flat)

    def test_non_multiple_of_8_height(self):
        """Frame height not a multiple of 8 (e.g. W=320, H=180, s=16)."""
        W, H, s = 320, 180, 16
        f1 = self._make_frame(W=W, H=H)
        f2 = self._make_frame(W=W, H=H)

        # Pattern in tile row ty=10 (y=160..176) moving 3 px right
        np.random.seed(55)
        pat = np.random.randint(0, 255, size=(12, 16), dtype=np.uint8)
        f1[160:172, 16:32] = pat
        f2[160:172, 19:35] = pat

        res = subtile_motion(f1, f2, s=s, tile_xy=(1, 10), max_d=15)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (3, 0))

    def test_max_d_boundary(self):
        """max_d boundary tests."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        np.random.seed(77)
        pat = np.random.randint(0, 255, size=(16, 16), dtype=np.uint8)
        f1[40:56, 40:56] = pat

        # Motion is 15 px right
        f2[40:56, 55:71] = pat

        # When max_d = 15, should find (15, 0)
        res15 = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=15)
        self.assertIsNotNone(res15)
        self.assertEqual(res15[:2], (15, 0))

        # When max_d = 10, shift 15 is outside max_d, should return None
        res10 = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=10)
        self.assertIsNone(res10)

    def test_ambiguous_flat_background_rule(self):
        """Ambiguous cases return None when evidence cannot separate vectors."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=16)
        self.assertIsNone(res)

    def test_ops_counter(self):
        """OPS increases during calls and resets with reset_ops()."""
        reset_ops()
        self.assertEqual(st_phaselocal.OPS, 0)

        f1 = self._make_frame()
        f2 = self._make_frame()
        f1[40:56, 40:42] = self.fg_color
        f2[40:56, 52:54] = self.fg_color

        subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=16)
        first_ops = st_phaselocal.OPS
        self.assertGreater(first_ops, 0)

        subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=16)
        second_ops = st_phaselocal.OPS
        self.assertGreater(second_ops, first_ops)

        reset_ops()
        self.assertEqual(st_phaselocal.OPS, 0)


if __name__ == "__main__":
    unittest.main()
