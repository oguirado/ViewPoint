"""tests/test_v2x_gm_census.py - Unit tests for Census rank-transform global motion estimation."""

import unittest
import numpy as np
from eval.v2x.gm_census import (
    estimate_global_motion,
    consensus_tiles,
    reset_ops,
    OPS,
)
import eval.v2x.gm_census as gm_census


def make_textured_frame(shape, seed=42, block_size=4):
    """Generate a textured luma frame with block structure suitable for coarse pyramid levels."""
    rng = np.random.default_rng(seed)
    H, W = shape
    h_small = (H + block_size - 1) // block_size
    w_small = (W + block_size - 1) // block_size
    small = rng.integers(0, 256, (h_small, w_small), dtype=np.uint8)
    big = np.repeat(np.repeat(small, block_size, axis=0), block_size, axis=1)[:H, :W]
    return big


class TestV2XGMCensus(unittest.TestCase):

    def test_textured_scrolls(self):
        """Test vertical scrolls (1, 7, 33, 200 px) and horizontal scrolls (5, 64 px) with exposed band filled."""
        H, W = 320, 480

        # Vertical scrolls
        for dy in (1, 7, 33, 200):
            prev = make_textured_frame((H, W), seed=100 + dy, block_size=4)
            cur = np.zeros((H, W), dtype=np.uint8)
            if dy > 0:
                cur[dy:, :] = prev[:H - dy, :]
                cur[:dy, :] = make_textured_frame((dy, W), seed=200 + dy, block_size=4)

            res = estimate_global_motion(prev, cur, max_d=250)
            self.assertIsNotNone(res, f"Failed for dy={dy}")
            dx_est, dy_est, score = res
            self.assertEqual((dx_est, dy_est), (0, dy), f"Expected (0, {dy}), got ({dx_est}, {dy_est})")

        # Horizontal scrolls
        for dx in (5, 64):
            prev = make_textured_frame((H, W), seed=300 + dx, block_size=4)
            cur = np.zeros((H, W), dtype=np.uint8)
            if dx > 0:
                cur[:, dx:] = prev[:, :W - dx]
                cur[:, :dx] = make_textured_frame((H, dx), seed=400 + dx, block_size=4)

            res = estimate_global_motion(prev, cur, max_d=250)
            self.assertIsNotNone(res, f"Failed for dx={dx}")
            dx_est, dy_est, score = res
            self.assertEqual((dx_est, dy_est), (dx, 0), f"Expected ({dx}, 0), got ({dx_est}, {dy_est})")

    def test_1088p_large_scroll(self):
        """Test 1920x1088 frame scrolled by 300 px vertically."""
        H, W = 1088, 1920
        dy = 300

        prev = make_textured_frame((H, W), seed=123, block_size=4)
        cur = np.zeros((H, W), dtype=np.uint8)
        cur[dy:, :] = prev[:H - dy, :]
        cur[:dy, :] = make_textured_frame((dy, W), seed=456, block_size=4)

        res = estimate_global_motion(prev, cur, max_d=500)
        self.assertIsNotNone(res, "Failed for 1088p scroll dy=300")
        dx_est, dy_est, score = res
        self.assertEqual((dx_est, dy_est), (0, dy))

    def test_static_header_footer_scroll(self):
        """Test static header and footer bands with scrolling middle."""
        H, W = 600, 800
        header_h, footer_h = 60, 50
        dy = 40

        prev = make_textured_frame((H, W), seed=789, block_size=4)
        cur = prev.copy()

        # Middle scrolling section
        mid_h = H - header_h - footer_h
        mid_prev = prev[header_h:H - footer_h, :]
        cur[header_h + dy:H - footer_h, :] = mid_prev[:mid_h - dy, :]
        cur[header_h:header_h + dy, :] = make_textured_frame((dy, W), seed=888, block_size=4)

        res = estimate_global_motion(prev, cur, max_d=200)
        self.assertIsNotNone(res, "Failed for static header/footer with scrolling middle")
        dx_est, dy_est, _ = res
        self.assertEqual((dx_est, dy_est), (0, dy))

    def test_diagonal_window_drag(self):
        """Test diagonal window-drag-like move."""
        H, W = 400, 600
        dx, dy = 16, 12

        prev = make_textured_frame((H, W), seed=999, block_size=4)
        cur = np.zeros((H, W), dtype=np.uint8)
        cur[dy:, dx:] = prev[:H - dy, :W - dx]
        cur[:dy, :] = make_textured_frame((dy, W), seed=111, block_size=4)
        cur[:, :dx] = make_textured_frame((H, dx), seed=222, block_size=4)

        res = estimate_global_motion(prev, cur, max_d=200)
        self.assertIsNotNone(res, "Failed for diagonal move")
        dx_est, dy_est, _ = res
        self.assertEqual((dx_est, dy_est), (dx, dy))

    def test_flat_frame(self):
        """Flat frame should return None."""
        prev = np.full((300, 400), 100, dtype=np.uint8)
        cur = np.full((300, 400), 100, dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=100)
        self.assertIsNone(res)

    def test_periodic_stripe_pattern(self):
        """Periodic stripe pattern of period 8 returns either None or true vector, never an alias."""
        H, W = 320, 480
        x_coords = np.arange(W)
        stripes = np.where((x_coords // 4) % 2 == 0, 220, 30).astype(np.uint8)
        prev = np.tile(stripes, (H, 1))

        # Test shift by period (dx = 8) -> true motion dx=8, dy=0
        dx = 8
        cur = np.zeros((H, W), dtype=np.uint8)
        cur[:, dx:] = prev[:, :W - dx]
        cur[:, :dx] = prev[:, :dx]

        res = estimate_global_motion(prev, cur, max_d=50)
        if res is not None:
            self.assertEqual((res[0], res[1]), (dx, 0), f"Expected true vector ({dx}, 0) or None, got ({res[0]}, {res[1]})")

    def test_consensus_tiles(self):
        """Test consensus_tiles masks where exposed and partial tiles are False."""
        H, W = 320, 480
        s = 16
        gh, gw = (H + s - 1) // s, (W + s - 1) // s

        rng = np.random.default_rng(42)
        prev = rng.integers(0, 256, (H, W), dtype=np.uint8)
        dx, dy = 16, 32
        cur = np.zeros((H, W), dtype=np.uint8)
        cur[dy:, dx:] = prev[:H - dy, :W - dx]

        mask = consensus_tiles(prev, cur, s, dx, dy)
        self.assertEqual(mask.shape, (gh, gw))

        # Exposed tiles (top 2 tile rows and left 1 tile col) must be False
        self.assertFalse(np.any(mask[:2, :]))
        self.assertFalse(np.any(mask[:, :1]))
        # Interior matching tiles must be True
        self.assertTrue(mask[2, 1])

    def test_ops_tracking_and_reset(self):
        """Test OPS increases after a call and resets with reset_ops."""
        reset_ops()
        self.assertEqual(gm_census.OPS, 0)

        rng = np.random.default_rng(111)
        prev = rng.integers(0, 256, (100, 100), dtype=np.uint8)
        cur = prev.copy()

        estimate_global_motion(prev, cur, max_d=20)
        ops_after_est = gm_census.OPS
        self.assertGreater(ops_after_est, 0)

        consensus_tiles(prev, cur, 16, 0, 0)
        self.assertGreater(gm_census.OPS, ops_after_est)

        reset_ops()
        self.assertEqual(gm_census.OPS, 0)

    def test_max_d_smaller_than_true_shift(self):
        """max_d smaller than true shift returns None or a vector within max_d."""
        rng = np.random.default_rng(222)
        H, W = 320, 480
        dy_true = 100

        prev = rng.integers(0, 256, (H, W), dtype=np.uint8)
        cur = np.zeros((H, W), dtype=np.uint8)
        cur[dy_true:, :] = prev[:H - dy_true, :]
        cur[:dy_true, :] = rng.integers(0, 256, (dy_true, W), dtype=np.uint8)

        max_d_small = 50
        res = estimate_global_motion(prev, cur, max_d=max_d_small)
        if res is not None:
            dx_est, dy_est, _ = res
            self.assertLessEqual(abs(dx_est), max_d_small)
            self.assertLessEqual(abs(dy_est), max_d_small)


if __name__ == "__main__":
    unittest.main()
