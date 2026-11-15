"""tests/test_v2x_gm_gradproj.py - Unit tests for gradient projection global motion estimation."""

import unittest
import numpy as np
from eval.v2x.gm_gradproj import (
    estimate_global_motion,
    consensus_tiles,
    OPS,
    reset_ops,
)


class TestV2XGMGradProj(unittest.TestCase):

    def setUp(self):
        reset_ops()

    def test_textured_scrolls(self):
        """Textured random frames scrolled vertically by 1, 7, 33, 200 px and horizontally by 5, 64 px."""
        rng = np.random.default_rng(seed=1001)
        W, H = 640, 480

        # Vertical scrolls: 1, 7, 33, 200 px
        for dy in (1, 7, 33, 200):
            reset_ops()
            canvas = rng.integers(0, 256, size=(H + 1000, W), dtype=np.uint8)
            prev = canvas[500:500 + H, :].copy()
            cur = canvas[500 - dy:500 - dy + H, :].copy()
            if dy > 0:
                cur[:dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)

            res = estimate_global_motion(prev, cur, max_d=300)
            self.assertIsNotNone(res, f"Failed for vertical scroll dy={dy}")
            est_dx, est_dy, score = res
            self.assertEqual((est_dx, est_dy), (0, dy), f"Expected (0, {dy}), got ({est_dx}, {est_dy})")

        # Horizontal scrolls: 5, 64 px
        for dx in (5, 64):
            reset_ops()
            canvas = rng.integers(0, 256, size=(H, W + 1000), dtype=np.uint8)
            prev = canvas[:, 500:500 + W].copy()
            cur = canvas[:, 500 - dx:500 - dx + W].copy()
            if dx > 0:
                cur[:, :dx] = rng.integers(0, 256, size=(H, dx), dtype=np.uint8)

            res = estimate_global_motion(prev, cur, max_d=300)
            self.assertIsNotNone(res, f"Failed for horizontal scroll dx={dx}")
            est_dx, est_dy, score = res
            self.assertEqual((est_dx, est_dy), (dx, 0), f"Expected ({dx}, 0), got ({est_dx}, {est_dy})")

    def test_1080p_scroll_300px(self):
        """One 1920x1088 frame scrolled by 300 px."""
        rng = np.random.default_rng(seed=2002)
        W, H = 1920, 1088
        dy = 300

        canvas = rng.integers(0, 256, size=(H + 1200, W), dtype=np.uint8)
        prev = canvas[600:600 + H, :].copy()
        cur = canvas[600 - dy:600 - dy + H, :].copy()
        cur[:dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=500)
        self.assertIsNotNone(res, "Failed for 1088p 300px vertical scroll")
        self.assertEqual((res[0], res[1]), (0, dy))

    def test_static_header_and_footer_scrolling_middle(self):
        """Static header band and footer band with a scrolling middle."""
        rng = np.random.default_rng(seed=3003)
        W, H = 1280, 800
        header_h, footer_h = 100, 100
        dy = 50

        middle_h = H - header_h - footer_h
        canvas = rng.integers(0, 256, size=(middle_h + 1000, W), dtype=np.uint8)

        prev_mid = canvas[500:500 + middle_h, :]
        cur_mid = canvas[500 - dy:500 - dy + middle_h, :]

        header = rng.integers(0, 256, size=(header_h, W), dtype=np.uint8)
        footer = rng.integers(0, 256, size=(footer_h, W), dtype=np.uint8)

        prev = np.vstack([header, prev_mid, footer])
        cur = np.vstack([header, cur_mid, footer])

        res = estimate_global_motion(prev, cur, max_d=300)
        self.assertIsNotNone(res, "Failed for static header/footer with scrolling middle")
        self.assertEqual((res[0], res[1]), (0, dy))

    def test_diagonal_window_drag(self):
        """A diagonal window-drag-like move."""
        rng = np.random.default_rng(seed=4004)
        W, H = 1280, 800
        dx, dy = 18, 24

        canvas = rng.integers(0, 256, size=(H + 1000, W + 1000), dtype=np.uint8)
        prev = canvas[500:500 + H, 500:500 + W].copy()
        cur = canvas[500 - dy:500 - dy + H, 500 - dx:500 - dx + W].copy()

        # Fill exposed bands
        cur[:dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)
        cur[:, :dx] = rng.integers(0, 256, size=(H, dx), dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=200)
        self.assertIsNotNone(res, "Failed for diagonal window drag move")
        self.assertEqual((res[0], res[1]), (dx, dy))

    def test_flat_frames(self):
        """Flat frames should return None."""
        prev = np.full((800, 1280), 128, dtype=np.uint8)
        cur = np.full((800, 1280), 128, dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=200)
        self.assertIsNone(res, "Flat frame should return None")

    def test_periodic_stripe_pattern(self):
        """Periodic stripe pattern of period 8 returns either None or the true vector, never an alias."""
        W, H = 640, 480
        y_indices = np.arange(H)[:, None]
        pattern = ((y_indices // 4) % 2) * 200 + 20
        stripes = np.tile(pattern.astype(np.uint8), (1, W))

        dy = 16
        prev = stripes
        cur = stripes

        res = estimate_global_motion(prev, cur, max_d=100)
        if res is not None:
            self.assertIn((res[0], res[1]), [(0, 0), (0, dy)], f"Returned alias vector {res[:2]}")

    def test_consensus_tiles_masks(self):
        """consensus_tiles masks where exposed and partial tiles are False."""
        rng = np.random.default_rng(seed=5005)
        W, H = 320, 192
        s = 16
        dx, dy = 0, 32

        canvas = rng.integers(0, 256, size=(H + 1000, W), dtype=np.uint8)
        prev = canvas[500:500 + H, :].copy()
        cur = canvas[500 - dy:500 - dy + H, :].copy()

        mask = consensus_tiles(prev, cur, s, dx, dy)
        gh = (H + s - 1) // s
        gw = (W + s - 1) // s

        self.assertEqual(mask.shape, (gh, gw))
        # Top 2 rows (dy=32, s=16 -> ty=0 and ty=1) have source before prev frame boundary -> False
        self.assertFalse(mask[0, 0], "Exposed top tile must be False")
        self.assertFalse(mask[1, 0], "Exposed top tile must be False")
        # In-bounds matching tiles should be True
        self.assertTrue(mask[2, 0], "In-bounds translated tile must be True")

    def test_ops_increment_and_reset(self):
        """OPS increases after a call and resets with reset_ops."""
        rng = np.random.default_rng(seed=6006)
        prev = rng.integers(0, 256, size=(100, 100), dtype=np.uint8)
        cur = rng.integers(0, 256, size=(100, 100), dtype=np.uint8)

        reset_ops()
        import eval.v2x.gm_gradproj as gm
        self.assertEqual(gm.OPS, 0)

        estimate_global_motion(prev, cur, max_d=20)
        ops_after_est = gm.OPS
        self.assertGreater(ops_after_est, 0, "OPS should increase after estimate_global_motion")

        consensus_tiles(prev, cur, s=16, dx=0, dy=16)
        self.assertGreater(gm.OPS, ops_after_est, "OPS should increase after consensus_tiles")

        reset_ops()
        self.assertEqual(gm.OPS, 0, "reset_ops() should reset OPS to 0")

    def test_max_d_smaller_than_true_shift(self):
        """max_d smaller than the true shift returns None or a vector within max_d."""
        rng = np.random.default_rng(seed=7007)
        W, H = 640, 480
        dy = 100

        canvas = rng.integers(0, 256, size=(H + 1000, W), dtype=np.uint8)
        prev = canvas[500:500 + H, :].copy()
        cur = canvas[500 - dy:500 - dy + H, :].copy()
        cur[:dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=50)
        if res is not None:
            est_dx, est_dy, _ = res
            self.assertLessEqual(abs(est_dx), 50, f"Returned dx {est_dx} exceeds max_d=50")
            self.assertLessEqual(abs(est_dy), 50, f"Returned dy {est_dy} exceeds max_d=50")


if __name__ == "__main__":
    unittest.main()
