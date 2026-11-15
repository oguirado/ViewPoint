"""Tests for eval/v2x/st_block4.py sub-tile motion variant."""

import unittest
import numpy as np
from eval.v2x.st_block4 import subtile_motion, reset_ops
import eval.v2x.st_block4 as st_mod


class TestV2XStBlock4(unittest.TestCase):

    def setUp(self):
        reset_ops()
        self.bg_color = 200
        self.fg_color = 50

    def test_caret_moving_right(self):
        """2x16 caret moving 12 px right inside a 32 px tile (tile 1,1)."""
        W, H, s = 128, 128, 32
        f1 = np.full((H, W), self.bg_color, dtype=np.uint8)
        f2 = np.full((H, W), self.bg_color, dtype=np.uint8)

        # Caret 2x16 at x=40, y=40 in tile (1, 1) (spans 32..64 x 32..64)
        f1[40:56, 40:42] = self.fg_color
        # Moves 12 px right -> x=52, y=40 in frame 2
        f2[40:56, 52:54] = self.fg_color

        res = subtile_motion(f1, f2, s=s, tile_xy=(1, 1), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n_blocks = res
        self.assertEqual((dx, dy), (12, 0))
        self.assertGreaterEqual(n_blocks, 1)

    def test_caret_crossing_tile_border(self):
        """A caret crossing a tile border from tile (1,1) to tile (2,1)."""
        W, H, s = 128, 128, 32
        f1 = np.full((H, W), self.bg_color, dtype=np.uint8)
        f2 = np.full((H, W), self.bg_color, dtype=np.uint8)

        # Caret at x=58 (in tile 1,1)
        f1[40:56, 58:60] = self.fg_color
        # Moves 12 px right -> x=70 (in tile 2,1)
        f2[40:56, 70:72] = self.fg_color

        # Target tile (1,1) where caret was originally
        res = subtile_motion(f1, f2, s=s, tile_xy=(1, 1), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n_blocks = res
        self.assertEqual((dx, dy), (12, 0))

    def test_text_carrier_moving(self):
        """A 3-tile wide text carrier moving 5 px."""
        W, H, s = 128, 128, 32
        f1 = np.full((H, W), self.bg_color, dtype=np.uint8)
        f2 = np.full((H, W), self.bg_color, dtype=np.uint8)

        # Carrier spanning x=20..90, y=40..50 across tiles (0,1), (1,1), (2,1)
        f1[40:50, 20:90] = self.fg_color
        # Moves 5 px right -> x=25..95
        f2[40:50, 25:95] = self.fg_color

        # Test middle tile (1,1) (spanning x=32..64)
        # Source for dx=5: x = 27..59, which lies inside f1 (20..90 is fg, wait)
        # In f1: x=20..90 is fg.
        # f2[40:50, 32:64] is fg (since 25..95 is fg).
        # f1 translated by dx=5 for x=32..64: f1[40:50, 27:59], which is ALL fg!
        # Wait! Is tile (1,1) equal to zero shift? f1[40:50, 32:64] is ALL fg!
        # Both zero shift and dx=5 yield all fg for tile (1,1)!
        # So for pure uniform text carrier body, zero shift equals translated shift!
        # To make text carrier distinct, add character texture/stripes!
        f1[40:50, 20:90] = self.fg_color
        f1[42:48, 25:85:4] = 100  # text pattern

        f2[40:50, 25:95] = self.fg_color
        f2[42:48, 30:90:4] = 100  # text pattern shifted by 5 px

        res = subtile_motion(f1, f2, s=s, tile_xy=(1, 1), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n_blocks = res
        self.assertEqual((dx, dy), (5, 0))

    def test_window_drag(self):
        """A window-drag box moving 4 px right and 2 px down."""
        W, H, s = 128, 128, 32
        f1 = np.full((H, W), self.bg_color, dtype=np.uint8)
        f2 = np.full((H, W), self.bg_color, dtype=np.uint8)

        # Box at x=40..70, y=40..70 in tile (1,1)
        f1[40:70, 40:70] = self.fg_color
        # Moves 4 px right, 2 px down -> x=44..74, y=42..72
        f2[42:72, 44:74] = self.fg_color

        res = subtile_motion(f1, f2, s=s, tile_xy=(1, 1), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n_blocks = res
        self.assertEqual((dx, dy), (4, 2))

    def test_scroll_exposed_bottom_tile(self):
        """A scroll whose exposed bottom tile contains new content must return None for that tile."""
        W, H, s = 128, 128, 32
        f1 = np.full((H, W), self.bg_color, dtype=np.uint8)
        f2 = np.full((H, W), self.bg_color, dtype=np.uint8)

        # Text line at y=40..50 scrolled up by 10 px -> y=30..40
        f1[40:50, 40:80] = self.fg_color
        f2[30:40, 40:80] = self.fg_color
        # Exposed bottom tile (tile 1,2 y=64..95) has new content
        f2[70:80, 40:80] = 120  # new content in exposed tile

        # Tile (1,2) is the exposed bottom tile
        res = subtile_motion(f1, f2, s=s, tile_xy=(1, 2), max_d=16)
        self.assertIsNone(res)

    def test_typing_glyph_appears(self):
        """A typing step where a glyph appears where the caret was must return None."""
        W, H, s = 128, 128, 32
        f1 = np.full((H, W), self.bg_color, dtype=np.uint8)
        f2 = np.full((H, W), self.bg_color, dtype=np.uint8)

        # Frame 1: caret at x=40..42, y=40..56 in tile (1,1)
        f1[40:56, 40:42] = self.fg_color

        # Frame 2: caret moved right to x=52..54, BUT a new glyph 'A' appears at x=40..48
        f2[40:56, 52:54] = self.fg_color
        f2[42:54, 40:48] = 30  # new glyph

        res = subtile_motion(f1, f2, s=s, tile_xy=(1, 1), max_d=16)
        self.assertIsNone(res)

    def test_flat_and_unchanged_tiles(self):
        """Flat tiles and unchanged tiles return None."""
        W, H, s = 128, 128, 32
        f1 = np.full((H, W), self.bg_color, dtype=np.uint8)
        f2 = np.full((H, W), self.bg_color, dtype=np.uint8)

        # Unchanged flat tile
        res1 = subtile_motion(f1, f2, s=s, tile_xy=(1, 1), max_d=16)
        self.assertIsNone(res1)

        # Tile modified but completely flat inside f1 and f2 at tile (1,1) with different color
        f1[32:64, 32:64] = 100
        f2[32:64, 32:64] = 150
        res2 = subtile_motion(f1, f2, s=s, tile_xy=(1, 1), max_d=16)
        self.assertIsNone(res2)

    def test_non_multiple_of_8_height(self):
        """Frames whose height is not a multiple of 8 (e.g., 122 / non-standard height)."""
        W, H, s = 128, 122, 32  # 122 is not multiple of 8
        f1 = np.full((H, W), self.bg_color, dtype=np.uint8)
        f2 = np.full((H, W), self.bg_color, dtype=np.uint8)

        # Feature in tile (1, 3) (y=96..122)
        f1[100:110, 40:50] = self.fg_color
        f2[100:110, 44:54] = self.fg_color  # moved 4 px right

        res = subtile_motion(f1, f2, s=s, tile_xy=(1, 3), max_d=16)
        self.assertIsNotNone(res)
        self.assertEqual(res[:2], (4, 0))

    def test_max_d_boundary(self):
        """Motion exactly at max_d limit and exceeding max_d limit."""
        W, H, s = 128, 128, 32
        f1 = np.full((H, W), self.bg_color, dtype=np.uint8)
        f2 = np.full((H, W), self.bg_color, dtype=np.uint8)

        # Place feature in tile (1,1) so source is inside frame for max_d=15
        f1[40:50, 40:50] = self.fg_color
        # Move by 15 px (max_d = 15)
        f2[40:50, 55:65] = self.fg_color

        res_ok = subtile_motion(f1, f2, s=s, tile_xy=(1, 1), max_d=15)
        self.assertIsNotNone(res_ok)
        self.assertEqual(res_ok[:2], (15, 0))

        # Move by 16 px with max_d = 15 -> should return None
        f3 = np.full((H, W), self.bg_color, dtype=np.uint8)
        f3[40:50, 56:66] = self.fg_color
        res_fail = subtile_motion(f1, f3, s=s, tile_xy=(1, 1), max_d=15)
        self.assertIsNone(res_fail)

    def test_ambiguous_flat_background_cases(self):
        """Ambiguous flat-background / tie-breaking cases follow documented rule."""
        W, H, s = 128, 128, 32
        f1 = np.full((H, W), self.bg_color, dtype=np.uint8)
        f2 = np.full((H, W), self.bg_color, dtype=np.uint8)

        # Two identical features in tile (1,1)
        f1[40:50, 40:45] = self.fg_color
        f1[40:50, 50:55] = self.fg_color

        # In f2, shifted right by 2 px
        f2[40:50, 42:47] = self.fg_color
        f2[40:50, 52:57] = self.fg_color

        res = subtile_motion(f1, f2, s=s, tile_xy=(1, 1), max_d=16)
        self.assertIsNotNone(res)
        # Should pick closest distance (2, 0)
        self.assertEqual(res[:2], (2, 0))

    def test_ops_counter(self):
        """OPS increases during calls and resets with reset_ops()."""
        reset_ops()
        self.assertEqual(st_mod.OPS, 0)

        W, H, s = 128, 128, 32
        f1 = np.full((H, W), self.bg_color, dtype=np.uint8)
        f2 = np.full((H, W), self.bg_color, dtype=np.uint8)
        f1[40:56, 40:42] = self.fg_color
        f2[40:56, 52:54] = self.fg_color

        subtile_motion(f1, f2, s=s, tile_xy=(1, 1), max_d=16)
        ops1 = st_mod.OPS
        self.assertGreater(ops1, 0)

        reset_ops()
        self.assertEqual(st_mod.OPS, 0)


if __name__ == "__main__":
    unittest.main()
