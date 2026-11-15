"""tests/test_p5_ycocg_clip.py - Unit tests for P5 integer statistical history clipping."""

import unittest
import numpy as np

from eval.p5_ycocg_clip import (
    rgb_to_ycocg,
    ycocg_to_rgb,
    clip_history,
    clip_frame_ycocg,
    clip_frame_luma,
    clipped_residual,
    raw_residual,
    compute_box_3x3,
)


class TestP5YCoCgClip(unittest.TestCase):
    """Test suite for eval/p5_ycocg_clip.py."""

    def test_lossless_roundtrip(self):
        """Pin lossless round trip on all 16777216 RGB values sampled by stride 4099 plus edges."""
        r = np.arange(256, dtype=np.uint8)
        g = np.arange(256, dtype=np.uint8)
        b = np.arange(256, dtype=np.uint8)
        grid = np.stack(np.meshgrid(r, g, b, indexing="ij"), axis=-1).reshape(-1, 3)

        sampled = grid[::4099]

        # Edge cases: boundaries, zeros, max uint8, extremes
        edge_vals = [0, 1, 127, 128, 254, 255]
        edge_grid = np.array(
            [[er, eg, eb] for er in edge_vals for eg in edge_vals for eb in edge_vals],
            dtype=np.uint8,
        )

        all_rgb = np.vstack([sampled, edge_grid])

        ycocg = rgb_to_ycocg(all_rgb)
        rec_rgb = ycocg_to_rgb(ycocg)

        self.assertTrue(
            np.array_equal(all_rgb, rec_rgb),
            "Lossless round-trip failed on sampled RGB values!",
        )

    def test_interior_points_untouched(self):
        """Pin that interior points inside the neighbourhood box are left untouched."""
        c = np.array([100, 50, -20])
        e = np.array([10, 5, 15])

        # Exact center
        self.assertTrue(np.array_equal(clip_history(c, c, e), c))

        # Points strictly inside
        h_inside = np.array([105, 52, -10])
        self.assertTrue(np.array_equal(clip_history(h_inside, c, e), h_inside))

        # Points on boundary
        h_boundary = np.array([110, 45, -35])
        self.assertTrue(np.array_equal(clip_history(h_boundary, c, e), h_boundary))

        # Scalar version
        self.assertEqual(clip_history(100, 100, 10), 100)
        self.assertEqual(clip_history(105, 100, 10), 105)

    def test_exterior_points_land_inside(self):
        """Pin that exterior points outside the neighbourhood box land inside or on the boundary."""
        c = np.array([100, 50, -20])
        e = np.array([10, 5, 15])

        h_ext1 = np.array([150, 50, -20])
        clipped1 = clip_history(h_ext1, c, e)
        self.assertTrue(np.all(c - e <= clipped1) and np.all(clipped1 <= c + e))
        self.assertEqual(clipped1[0], 110)  # boundary on dim 0

        h_ext2 = np.array([140, 80, -60])
        clipped2 = clip_history(h_ext2, c, e)
        self.assertTrue(np.all(c - e <= clipped2) and np.all(clipped2 <= c + e))

    def test_clipping_idempotence(self):
        """Pin that clipping is idempotent (clip(clip(h)) == clip(h))."""
        c = np.array([100, 50, -20])
        e = np.array([10, 5, 15])

        h_ext = np.array([160, 90, -80])
        clipped_once = clip_history(h_ext, c, e)
        clipped_twice = clip_history(clipped_once, c, e)

        self.assertTrue(
            np.array_equal(clipped_once, clipped_twice),
            "Clipping is not idempotent for history vector!",
        )

        # Full frame idempotence
        rng = np.random.RandomState(42)
        cur_rgb = rng.randint(0, 256, size=(16, 16, 3), dtype=np.uint8)
        prev_rgb = rng.randint(0, 256, size=(16, 16, 3), dtype=np.uint8)

        clip1 = clip_frame_ycocg(cur_rgb, prev_rgb)
        clip2 = clip_frame_ycocg(cur_rgb, clip1)

        self.assertTrue(
            np.array_equal(clip1, clip2),
            "RGB frame clipping is not idempotent!",
        )

        cur_luma = rng.randint(0, 256, size=(16, 16), dtype=np.uint8)
        prev_luma = rng.randint(0, 256, size=(16, 16), dtype=np.uint8)

        luma1 = clip_frame_luma(cur_luma, prev_luma)
        luma2 = clip_frame_luma(cur_luma, luma1)

        self.assertTrue(
            np.array_equal(luma1, luma2),
            "Luma frame clipping is not idempotent!",
        )

    def test_ram_frames_seeded_noise_and_moved_edge(self):
        """Pin that on RAM frames with seeded additive noise of amplitude 8:

        - clipped residual of static tiles is lower than raw residual
        - moved edge keeps a residual above static tile clipped residual.
        """
        rng = np.random.RandomState(999)
        H, W = 16, 32

        # Base frames in RAM
        base_prev = np.zeros((H, W), dtype=np.uint8) + 120
        base_cur = np.zeros((H, W), dtype=np.uint8) + 120

        # Static region in tile (0, 0) [y: 0..8, x: 0..8] - flat 120
        # Moved edge region in tile (0, 2) [y: 0..8, x: 16..24]
        # In prev, edge is at x=20. In cur, edge moves to x=22.
        base_prev[:, 16:20] = 40
        base_prev[:, 20:24] = 200

        base_cur[:, 16:22] = 40
        base_cur[:, 22:24] = 200

        # Additive noise of amplitude 8 in RAM
        noise_prev = rng.randint(-8, 9, size=(H, W))
        noise_cur = rng.randint(-8, 9, size=(H, W))

        prev_luma = np.clip(base_prev.astype(int) + noise_prev, 0, 255).astype(np.uint8)
        cur_luma = np.clip(base_cur.astype(int) + noise_cur, 0, 255).astype(np.uint8)

        c_res = clipped_residual(cur_luma, prev_luma, tile_size=8)
        r_res = raw_residual(cur_luma, prev_luma, tile_size=8)

        # Static tile at (0, 0)
        static_clipped = c_res[0, 0]
        static_raw = r_res[0, 0]

        # Moved edge tile at (0, 2)
        moved_edge_clipped = c_res[0, 2]

        self.assertLess(
            static_clipped,
            static_raw,
            f"Static tile clipped residual ({static_clipped}) should be lower than raw residual ({static_raw})",
        )
        self.assertGreater(
            moved_edge_clipped,
            static_clipped,
            f"Moved edge tile clipped residual ({moved_edge_clipped}) should be higher than static tile ({static_clipped})",
        )

        # Repeat for RGB frames
        prev_rgb = np.stack([prev_luma, prev_luma, prev_luma], axis=-1)
        cur_rgb = np.stack([cur_luma, cur_luma, cur_luma], axis=-1)

        c_res_rgb = clipped_residual(cur_rgb, prev_rgb, tile_size=8)
        r_res_rgb = raw_residual(cur_rgb, prev_rgb, tile_size=8)

        self.assertLess(c_res_rgb[0, 0], r_res_rgb[0, 0])
        self.assertGreater(c_res_rgb[0, 2], c_res_rgb[0, 0])


if __name__ == "__main__":
    unittest.main()
