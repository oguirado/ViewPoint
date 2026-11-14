"""Unit tests for eval/v2x/st_block2hash.py (2x2 block hash lookup subtile motion variant)."""

import unittest
import numpy as np
import eval.v2x.st_block2hash as st_mod
from eval.v2x.st_block2hash import subtile_motion, reset_ops


class TestV2XSTBlock2Hash(unittest.TestCase):

    def setUp(self):
        reset_ops()
        self.bg_color = 200
        self.fg_color = 50

    def _make_frame(self, W=320, H=180, fill=None):
        if fill is None:
            fill = self.bg_color
        return np.full((H, W), fill, dtype=np.uint8)

    def test_caret_moving_right_inside_tile(self):
        """A 2x16 caret moving 12 px right inside tile (1, 0)."""
        W, H, s = 128, 128, 32
        f1 = self._make_frame(W, H)
        f2 = self._make_frame(W, H)

        # Caret 2x16 at x=40, y=8 in tile (1, 0) [x=32..63]
        f1[8:24, 40:42] = self.fg_color
        # Moves 12 px right -> x=52, y=8
        f2[8:24, 52:54] = self.fg_color

        res = subtile_motion(f1, f2, s=s, tile_xy=(1, 0), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (12, 0))
        self.assertGreaterEqual(n, 1)

    def test_caret_crossing_tile_border(self):
        """A caret crossing a tile border from tile (1,0) into tile (2,0)."""
        W, H, s = 128, 128, 32
        f1 = self._make_frame(W, H)
        f2 = self._make_frame(W, H)

        # Caret at x=58, y=8 in tile (1, 0)
        f1[8:24, 58:60] = self.fg_color
        # Moves 12 px right -> x=70, y=8 in destination tile (2, 0)
        f2[8:24, 70:72] = self.fg_color

        res = subtile_motion(f1, f2, s=s, tile_xy=(2, 0), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (12, 0))
        self.assertGreaterEqual(n, 1)

    def test_text_carrier_moving_5px(self):
        """A 3-tile wide text carrier moving 5 px right."""
        W, H, s = 160, 64, 32
        f1 = self._make_frame(W, H)
        f2 = self._make_frame(W, H)

        # Non-periodic textured text carrier spanning x=10..82, y=10..20
        np.random.seed(42)
        rand_pattern = np.random.randint(0, 255, (10, 72), dtype=np.uint8)
        f1[10:20, 10:82] = rand_pattern
        # Moves 5 px right -> x=15..87
        f2[10:20, 15:87] = rand_pattern

        res = subtile_motion(f1, f2, s=s, tile_xy=(1, 0), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (5, 0))
        self.assertGreaterEqual(n, 1)

    def test_window_drag_box_moving(self):
        """A window-drag box moving (dx=4, dy=2)."""
        W, H, s = 128, 128, 32
        f1 = self._make_frame(W, H)
        f2 = self._make_frame(W, H)

        # 20x20 box in tile (1, 1) at x=40, y=40
        f1[40:60, 40:60] = 50
        # Moves 4 px right, 2 px down -> x=44, y=42
        f2[42:62, 44:64] = 50

        res = subtile_motion(f1, f2, s=s, tile_xy=(1, 1), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (4, 2))
        self.assertGreaterEqual(n, 1)

    def test_scroll_exposed_bottom_tile_returns_none(self):
        """A scroll whose exposed bottom tile contains new content must return None for that tile."""
        W, H, s = 64, 64, 32
        f1 = self._make_frame(W, H)
        f2 = self._make_frame(W, H)

        # f1 has content in top tile (0, 0)
        f1[5:25, 5:25] = 100

        # Entire screen scrolls UP by 10 px
        f2[0:15, 5:25] = 100
        # Exposed bottom tile (0, 1) gets brand new content not in prev
        f2[40:55, 5:25] = 150

        # Subtile motion query for bottom tile (0, 1)
        res = subtile_motion(f1, f2, s=s, tile_xy=(0, 1), max_d=16)
        self.assertIsNone(res)

    def test_typing_step_returns_none(self):
        """A typing step where a new glyph appears where caret was must return None."""
        W, H, s = 64, 64, 32
        f1 = self._make_frame(W, H)
        f2 = self._make_frame(W, H)

        # f1 has caret at x=10, y=10
        f1[10:26, 10:12] = 0

        # f2 has glyph 'A' at x=10, y=10, and caret moved to x=18, y=10
        f2[10:26, 10:16] = 120
        f2[10:26, 18:20] = 0

        res = subtile_motion(f1, f2, s=s, tile_xy=(0, 0), max_d=16)
        self.assertIsNone(res)

    def test_flat_and_unchanged_tiles_return_none(self):
        """Flat tiles and unchanged tiles return None."""
        W, H, s = 64, 64, 32
        f1 = self._make_frame(W, H, fill=100)
        f2 = self._make_frame(W, H, fill=100)

        # Unchanged flat frame
        res = subtile_motion(f1, f2, s=s, tile_xy=(0, 0), max_d=16)
        self.assertIsNone(res)

        # Changed flat frame (global illumination change)
        f2_brighter = self._make_frame(W, H, fill=110)
        res = subtile_motion(f1, f2_brighter, s=s, tile_xy=(0, 0), max_d=16)
        self.assertIsNone(res)

    def test_height_not_multiple_of_8(self):
        """Frames whose height is not a multiple of 8 (e.g., 1920x1088 padded from 1080)."""
        W, H, s = 128, 1088, 32
        f1 = self._make_frame(W, H)
        f2 = self._make_frame(W, H)

        # Caret near bottom edge y=1065 in last row of tiles, tx=1
        f1[1065:1080, 40:42] = self.fg_color
        f2[1065:1080, 50:52] = self.fg_color

        ty = (1088 + s - 1) // s - 1  # Last tile row
        res = subtile_motion(f1, f2, s=s, tile_xy=(1, ty), max_d=16)
        self.assertIsNotNone(res)
        dx, dy, n = res
        self.assertEqual((dx, dy), (10, 0))

    def test_max_d_boundary(self):
        """Test max_d boundary limits."""
        W, H, s = 128, 128, 32
        f1 = self._make_frame(W, H)
        f2 = self._make_frame(W, H)

        # Caret moving exactly max_d = 16 px inside tile (1, 0)
        f1[8:24, 40:42] = self.fg_color
        f2[8:24, 56:58] = self.fg_color

        res16 = subtile_motion(f1, f2, s=s, tile_xy=(1, 0), max_d=16)
        self.assertIsNotNone(res16)
        self.assertEqual(res16[:2], (16, 0))

        # Caret moving 17 px with max_d = 16 (exceeds max_d) -> None
        f3 = self._make_frame(W, H)
        f3[8:24, 57:59] = self.fg_color
        res17 = subtile_motion(f1, f3, s=s, tile_xy=(1, 0), max_d=16)
        self.assertIsNone(res17)

    def test_ambiguous_flat_background_cases(self):
        """Ambiguous cases where multiple vectors tie should return None."""
        W, H, s = 128, 128, 32
        f1 = self._make_frame(W, H)
        f2 = self._make_frame(W, H)

        # Periodic dot pattern giving identical vote counts for multiple displacements
        for y in range(8, 28, 4):
            for x in range(8, 28, 4):
                f1[y:y+2, x:x+2] = 10
                f2[y:y+2, x:x+2] = 10

        # Introduce a uniform shift to part of tile
        f2[8:28, 8:28] = f1[8:28, 8:28]

        res = subtile_motion(f1, f2, s=s, tile_xy=(0, 0), max_d=16)
        # Should be None since f1 and f2 are identical in tile (0,0) or ambiguous
        self.assertIsNone(res)

    def test_ops_increases_and_resets(self):
        """OPS increases during calls and resets with reset_ops()."""
        reset_ops()
        self.assertEqual(st_mod.OPS, 0)

        W, H, s = 64, 64, 32
        f1 = self._make_frame(W, H)
        f2 = self._make_frame(W, H)
        f1[8:24, 40:42] = self.fg_color
        f2[8:24, 52:54] = self.fg_color

        subtile_motion(f1, f2, s=s, tile_xy=(1, 0), max_d=16)
        self.assertGreater(st_mod.OPS, 0)

        ops_before = st_mod.OPS
        reset_ops()
        self.assertEqual(st_mod.OPS, 0)


if __name__ == "__main__":
    unittest.main()
