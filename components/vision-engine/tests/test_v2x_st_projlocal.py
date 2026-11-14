"""Unit tests for eval/v2x/st_projlocal.py sub-tile motion algorithm."""

import unittest
import numpy as np
import eval.v2x.st_projlocal as st_projlocal
from eval.v2x.st_projlocal import subtile_motion, reset_ops


class TestV2XStProjLocal(unittest.TestCase):

    def setUp(self):
        reset_ops()
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

    def test_caret_moving_right_inside_tile(self):
        """A 2x16 caret moving 12 px right inside a 32 px tile."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Caret 2x16 at x=8, y=8 in tile (0,0) [0..31 x 0..31]
        f1[8:24, 8:10] = self.fg_color
        # Moves 12 px right -> x=20, y=8
        f2[8:24, 20:22] = self.fg_color

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(0, 0), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (12, 0))
        self.assertGreaterEqual(n, 1)

    def test_caret_crossing_tile_border(self):
        """A caret crossing a tile border from tile (0,0) to tile (1,0)."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Caret at x=26, y=8 in tile (0,0)
        f1[8:24, 26:28] = self.fg_color
        # Moves 12 px right -> x=38, y=8 in tile (1,0)
        f2[8:24, 38:40] = self.fg_color

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(0, 0), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (12, 0))
        self.assertGreaterEqual(n, 1)

    def test_text_carrier_moving_5px(self):
        """A 3-tile wide text carrier moving 5 px."""
        s = 32
        W = 3 * s  # 96 px wide
        H = s      # 32 px high
        f1 = np.full((H, W), self.bg_color, dtype=np.uint8)
        f2 = np.full((H, W), self.bg_color, dtype=np.uint8)

        # Carrier block at x=10..70 (60 px wide)
        f1[8:24, 10:70] = self.fg_color
        # Textured details inside carrier
        f1[10:22, 15:25] = 100
        f1[10:22, 35:45] = 120
        f1[10:22, 50:60] = 140

        # Moves 5 px right -> x=15..75
        f2[8:24, 15:75] = self.fg_color
        f2[10:22, 20:30] = 100
        f2[10:22, 40:50] = 120
        f2[10:22, 55:65] = 140

        # Query middle tile (1,0) [32..63]
        res = subtile_motion(f1, f2, s=s, tile_xy=(1, 0), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (5, 0))
        self.assertGreaterEqual(n, 1)

    def test_window_drag_box(self):
        """A window-drag box moving 4 px right and 2 px down."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Window box at x=30..70, y=30..70
        f1[30:70, 30:70] = self.fg_color
        f1[32:68, 32:68] = 150  # window interior

        # Moves (dx=4, dy=2)
        f2[32:72, 34:74] = self.fg_color
        f2[34:70, 36:72] = 150

        # Tile (1,1) [32..63 x 32..63]
        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 1), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (4, 2))
        self.assertGreaterEqual(n, 1)

    def test_scroll_exposed_bottom_tile(self):
        """A scroll whose exposed bottom tile contains new content must return None for that tile."""
        s = 32
        W = 32
        H = 64  # 2 tiles high
        f1 = np.full((H, W), self.bg_color, dtype=np.uint8)
        f2 = np.full((H, W), self.bg_color, dtype=np.uint8)

        # Frame 1 has content in top tile
        f1[0:32, 0:32] = 100
        f1[10:20, 10:20] = 50

        # Frame 2 scrolls down by 8 px (dy = -8, content moves up)
        # Top 24 rows of f2 are bottom 24 rows of f1
        f2[0:24, 0:32] = f1[8:32, 0:32]
        # Exposed bottom rows (y=24..63) contain new brand-new content
        f2[24:64, 0:32] = 220

        # Query bottom tile (0, 1)
        res = subtile_motion(f1, f2, s=s, tile_xy=(0, 1), max_d=16)
        self.assertIsNone(res)

    def test_typing_step(self):
        """A typing step where a glyph appears where the caret was must return None."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Caret 2x16 at x=8, y=8
        f1[8:24, 8:10] = self.fg_color

        # Caret moves to x=20, y=8, and a new glyph appears at x=8..16, y=8..24
        f2[8:24, 20:22] = self.fg_color
        f2[8:24, 8:16] = 75  # new typed character

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(0, 0), max_d=16)
        self.assertIsNone(res)

    def test_flat_and_unchanged_tiles(self):
        """Flat tiles and unchanged tiles return None."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Unchanged
        res = subtile_motion(f1, f2, s=self.s, tile_xy=(0, 0), max_d=16)
        self.assertIsNone(res)

        # Both flat, but different flat colors
        f1_flat = np.full((self.H, self.W), 100, dtype=np.uint8)
        f2_flat = np.full((self.H, self.W), 150, dtype=np.uint8)
        # For a flat change, any translation gives the same constant background in bounds,
        # leading to ambiguous multiple valid translations, so return None.
        res_flat = subtile_motion(f1_flat, f2_flat, s=self.s, tile_xy=(0, 0), max_d=16)
        self.assertIsNone(res_flat)

    def test_non_multiple_of_8_height(self):
        """Frames whose height is not a multiple of 8 (e.g. 1920x1088 padded from 1080)."""
        W = 64
        H = 50  # Not a multiple of 8
        s = 16
        f1 = np.full((H, W), self.bg_color, dtype=np.uint8)
        f2 = np.full((H, W), self.bg_color, dtype=np.uint8)

        # Feature near the bottom edge
        f1[35:45, 10:20] = self.fg_color
        # Moves 3 px right
        f2[35:45, 13:23] = self.fg_color

        res = subtile_motion(f1, f2, s=s, tile_xy=(0, 2), max_d=8)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (3, 0))

    def test_max_d_boundary(self):
        """Displacement at max_d boundary and exceeding max_d."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Feature at x=10, y=10
        f1[10:14, 10:14] = self.fg_color

        # Move by max_d = 10
        f2[10:14, 20:24] = self.fg_color  # dx = 10
        res = subtile_motion(f1, f2, s=self.s, tile_xy=(0, 0), max_d=10)
        self.assertIsNotNone(res)
        self.assertEqual(res[:2], (10, 0))

        # Move by 11 (exceeds max_d = 10)
        f3 = self._make_frame()
        f3[10:14, 21:25] = self.fg_color  # dx = 11
        res_exceed = subtile_motion(f1, f3, s=self.s, tile_xy=(0, 0), max_d=10)
        self.assertIsNone(res_exceed)

    def test_ambiguous_flat_background(self):
        """Ambiguous flat-background cases follow documented rule (return None)."""
        f1 = self._make_frame()
        f2 = self._make_frame()

        # Horizontal stripe: changing dx gives identical projection/tile match in flat bg
        f1[16:20, :] = self.fg_color
        f2[18:22, :] = self.fg_color  # dy = 2, dx is ambiguous across all x

        res = subtile_motion(f1, f2, s=self.s, tile_xy=(1, 0), max_d=8)
        self.assertIsNone(res)

    def test_ops_counter(self):
        """OPS increases during calls and resets with reset_ops()."""
        reset_ops()
        self.assertEqual(st_projlocal.OPS, 0)

        f1 = self._make_frame()
        f2 = self._make_frame()
        f1[8:24, 8:10] = self.fg_color
        f2[8:24, 20:22] = self.fg_color

        subtile_motion(f1, f2, s=self.s, tile_xy=(0, 0), max_d=16)
        self.assertGreater(st_projlocal.OPS, 0)

        prev_ops = st_projlocal.OPS
        subtile_motion(f1, f2, s=self.s, tile_xy=(0, 0), max_d=16)
        self.assertGreater(st_projlocal.OPS, prev_ops)

        reset_ops()
        self.assertEqual(st_projlocal.OPS, 0)


if __name__ == "__main__":
    unittest.main()
