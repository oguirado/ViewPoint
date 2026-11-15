"""tests/test_v2x_gm_corners.py - Tests for eval.v2x.gm_corners global motion estimator."""

import unittest
import numpy as np

from eval.v2x.gm_corners import (
    estimate_global_motion,
    consensus_tiles,
    reset_ops,
    OPS,
)
import eval.v2x.gm_corners as gm_mod


class TestV2XGMCorners(unittest.TestCase):

    def setUp(self):
        reset_ops()

    def test_textured_random_scrolls(self):
        """Test vertical and horizontal scrolls on textured random frames using default_rng."""
        H, W = 360, 640
        rng = np.random.default_rng(seed=42)

        shifts = [
            (0, 1),
            (0, 7),
            (0, 33),
            (0, 200),
            (5, 0),
            (64, 0),
        ]

        for dx, dy in shifts:
            reset_ops()
            prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
            cur = np.zeros((H, W), dtype=np.uint8)

            cy0, cy1 = max(0, dy), min(H, H + dy)
            cx0, cx1 = max(0, dx), min(W, W + dx)
            py0, py1 = cy0 - dy, cy1 - dy
            px0, px1 = cx0 - dx, cx1 - dx

            cur[cy0:cy1, cx0:cx1] = prev[py0:py1, px0:px1]

            if dy > 0:
                cur[:dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)
            elif dy < 0:
                cur[H + dy:, :] = rng.integers(0, 256, size=(-dy, W), dtype=np.uint8)

            if dx > 0:
                cur[:, :dx] = rng.integers(0, 256, size=(H, dx), dtype=np.uint8)
            elif dx < 0:
                cur[:, W + dx:] = rng.integers(0, 256, size=(H, -dx), dtype=np.uint8)

            res = estimate_global_motion(prev, cur, max_d=max(H, W) - 1)
            self.assertIsNotNone(res, f"Failed to estimate shift ({dx}, {dy})")
            est_dx, est_dy, score = res
            self.assertEqual((est_dx, est_dy), (dx, dy), f"Expected ({dx}, {dy}), got ({est_dx}, {est_dy})")

    def test_1080p_large_scroll(self):
        """Test 1920x1088 frame scrolled vertically by 300 px."""
        H, W = 1088, 1920
        rng = np.random.default_rng(seed=101)

        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
        dy = 300
        dx = 0

        cur = np.zeros((H, W), dtype=np.uint8)
        cur[dy:, :] = prev[:H - dy, :]
        cur[:dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=max(H, W) - 1)
        self.assertIsNotNone(res, "Failed to estimate 1080p 300px scroll")
        self.assertEqual((res[0], res[1]), (dx, dy))

    def test_static_header_footer_scrolling_middle(self):
        """Test vertical scroll with static header band and footer band."""
        H, W = 600, 800
        header_h = 60
        footer_h = 50
        dy = 40

        rng = np.random.default_rng(seed=202)
        base = rng.integers(0, 256, size=(H + 100, W), dtype=np.uint8)

        # cur[y, x] = prev[y - dy, x]
        # prev at offset 100, cur at offset 100 - dy
        prev = base[100:100 + H, :].copy()
        cur = base[100 - dy:100 - dy + H, :].copy()

        header = rng.integers(0, 256, size=(header_h, W), dtype=np.uint8)
        footer = rng.integers(0, 256, size=(footer_h, W), dtype=np.uint8)

        prev[:header_h, :] = header
        cur[:header_h, :] = header
        prev[H - footer_h:, :] = footer
        cur[H - footer_h:, :] = footer

        res = estimate_global_motion(prev, cur, max_d=300)
        self.assertIsNotNone(res, "Failed for static header/footer with scrolling middle")
        self.assertEqual((res[0], res[1]), (0, dy))

    def test_diagonal_window_drag(self):
        """Test diagonal window-drag-like move (dx=15, dy=25)."""
        H, W = 480, 640
        dx, dy = 15, 25
        rng = np.random.default_rng(seed=303)

        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
        cur = np.zeros((H, W), dtype=np.uint8)

        cur[dy:, dx:] = prev[:H - dy, :W - dx]
        cur[:dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)
        cur[:, :dx] = rng.integers(0, 256, size=(H, dx), dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=300)
        self.assertIsNotNone(res, "Failed for diagonal move")
        self.assertEqual((res[0], res[1]), (dx, dy))

    def test_flat_frames(self):
        """Flat frames must return None."""
        prev = np.full((180, 320), 128, dtype=np.uint8)
        cur = np.full((180, 320), 128, dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=200)
        self.assertIsNone(res, "Flat frames should return None")

    def test_periodic_stripe_pattern(self):
        """Periodic stripe pattern of period 8 returns None or true vector, never an alias."""
        H, W = 200, 320
        period = 8

        x_coords = np.arange(W)
        stripes = np.where((x_coords // (period // 2)) % 2 == 0, 220, 30).astype(np.uint8)
        pattern = np.tile(stripes, (H, 1))

        res_zero = estimate_global_motion(pattern, pattern, max_d=100)
        if res_zero is not None:
            self.assertEqual((res_zero[0], res_zero[1]), (0, 0), "Periodic pattern returned wrong vector for zero shift")

        dx = 16
        prev_f = np.tile(stripes[:W], (H, 1))
        cur_f = np.roll(prev_f, dx, axis=1)

        res_p = estimate_global_motion(prev_f, cur_f, max_d=100)
        if res_p is not None:
            self.assertIn((res_p[0], res_p[1]), [(dx, 0), (0, 0)], "Returned alias instead of true vector or None")

    def test_consensus_tiles_mask(self):
        """Test consensus_tiles mask where exposed and partial tiles are False."""
        H, W = 180, 320
        s = 16
        dx, dy = 10, 20

        rng = np.random.default_rng(seed=404)
        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
        cur = np.zeros((H, W), dtype=np.uint8)

        cur[dy:, dx:] = prev[:H - dy, :W - dx]
        cur[:dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)
        cur[:, :dx] = rng.integers(0, 256, size=(H, dx), dtype=np.uint8)

        mask = consensus_tiles(prev, cur, s, dx, dy)
        self.assertEqual(mask.shape, (12, 20))

        self.assertFalse(mask[0, :].any(), "Exposed top tiles must be False")
        self.assertFalse(mask[:, 0].any(), "Exposed left tiles must be False")

        self.assertTrue(mask[2, 2], "In-bounds fully shifted interior tile must be True")

    def test_ops_increment_and_reset(self):
        """OPS increases after a call and resets with reset_ops."""
        H, W = 180, 320
        rng = np.random.default_rng(seed=505)

        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
        cur = prev.copy()

        reset_ops()
        self.assertEqual(gm_mod.OPS, 0)

        consensus_tiles(prev, cur, 16, 0, 0)
        ops_after_consensus = gm_mod.OPS
        self.assertGreater(ops_after_consensus, 0, "OPS must increase after consensus_tiles")

        estimate_global_motion(prev, cur, max_d=100)
        ops_after_estimate = gm_mod.OPS
        self.assertGreater(ops_after_estimate, ops_after_consensus, "OPS must increase after estimate_global_motion")

        reset_ops()
        self.assertEqual(gm_mod.OPS, 0, "OPS must reset to 0")

    def test_max_d_limitation(self):
        """max_d smaller than true shift returns None or a vector within max_d."""
        H, W = 360, 640
        dx, dy = 0, 150
        rng = np.random.default_rng(seed=606)

        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
        cur = np.zeros((H, W), dtype=np.uint8)
        cur[dy:, :] = prev[:H - dy, :]

        res = estimate_global_motion(prev, cur, max_d=50)
        if res is not None:
            self.assertLessEqual(abs(res[0]), 50)
            self.assertLessEqual(abs(res[1]), 50)


if __name__ == "__main__":
    unittest.main()
