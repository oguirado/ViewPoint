"""tests/test_v2x_gm_vechist.py - Unit tests for global motion vechist variant."""

import unittest
import numpy as np
from eval.v2x.gm_vechist import (
    estimate_global_motion,
    consensus_tiles,
    reset_ops,
    OPS,
)
import eval.v2x.gm_vechist as gm_vechist


class TestGMVechist(unittest.TestCase):

    def setUp(self):
        reset_ops()

    def test_textured_random_scrolls(self):
        """Test textured random frames scrolled vertically (1, 7, 33, 200) and horizontally (5, 64)."""
        W, H = 640, 480
        rng = np.random.default_rng(seed=12345)

        # Vertical scrolls: 1, 7, 33, 200 px
        for dy in (1, 7, 33, 200):
            prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
            cur = np.zeros((H, W), dtype=np.uint8)
            cur[dy:, :] = prev[:H - dy, :]
            cur[:dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)

            res = estimate_global_motion(prev, cur, max_d=400)
            self.assertIsNotNone(res, f"Failed for vertical scroll dy={dy}")
            est_dx, est_dy, score = res
            self.assertEqual((est_dx, est_dy), (0, dy), f"Expected (0, {dy}), got ({est_dx}, {est_dy})")

        # Horizontal scrolls: 5, 64 px
        for dx in (5, 64):
            prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
            cur = np.zeros((H, W), dtype=np.uint8)
            cur[:, dx:] = prev[:, :W - dx]
            cur[:, :dx] = rng.integers(0, 256, size=(H, dx), dtype=np.uint8)

            res = estimate_global_motion(prev, cur, max_d=400)
            self.assertIsNotNone(res, f"Failed for horizontal scroll dx={dx}")
            est_dx, est_dy, score = res
            self.assertEqual((est_dx, est_dy), (dx, 0), f"Expected ({dx}, 0), got ({est_dx}, {est_dy})")

    def test_1080p_scrolled_300px(self):
        """Test one 1920x1088 frame scrolled vertically by 300 px."""
        W, H = 1920, 1088
        rng = np.random.default_rng(seed=999)
        dy = 300

        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
        cur = np.zeros((H, W), dtype=np.uint8)
        cur[dy:, :] = prev[:H - dy, :]
        cur[:dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=600)
        self.assertIsNotNone(res, "Failed for 1920x1088 scroll 300px")
        self.assertEqual((res[0], res[1]), (0, dy))

    def test_static_header_and_footer_scrolling_middle(self):
        """Test a static header band and footer band with a scrolling middle."""
        W, H = 640, 480
        header_h, footer_h = 64, 64
        rng = np.random.default_rng(seed=777)
        dy = 24

        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
        cur = prev.copy()

        # Scroll middle area
        mid_h = H - header_h - footer_h
        cur[header_h + dy:H - footer_h, :] = prev[header_h:H - footer_h - dy, :]
        cur[header_h:header_h + dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=200)
        self.assertIsNotNone(res, "Failed for static header/footer with scrolling middle")
        self.assertEqual((res[0], res[1]), (0, dy))

    def test_diagonal_window_drag(self):
        """Test a diagonal window-drag-like move (dx=12, dy=16)."""
        W, H = 640, 480
        rng = np.random.default_rng(seed=4321)
        dx, dy = 12, 16

        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
        cur = np.zeros((H, W), dtype=np.uint8)
        cur[dy:, dx:] = prev[:H - dy, :W - dx]

        # Fill exposed bands
        cur[:dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)
        cur[:, :dx] = rng.integers(0, 256, size=(H, dx), dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=200)
        self.assertIsNotNone(res, "Failed for diagonal move")
        self.assertEqual((res[0], res[1]), (dx, dy))

    def test_flat_frames(self):
        """Flat frames must return None."""
        prev = np.full((480, 640), 100, dtype=np.uint8)
        cur = np.full((480, 640), 100, dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=200)
        self.assertIsNone(res, "Flat frame should return None")

    def test_periodic_stripes_period_8(self):
        """A periodic stripe pattern of period 8 returns either None or true vector, never an alias."""
        W, H = 640, 480
        x_coords = np.arange(W)
        stripes = np.where((x_coords // 4) % 2 == 0, 220, 20).astype(np.uint8)
        prev = np.tile(stripes, (H, 1))

        # Shift by 16 px (2 full periods -> true dx = 16)
        dx = 16
        cur = np.roll(prev, dx, axis=1)

        res = estimate_global_motion(prev, cur, max_d=200)
        if res is not None:
            self.assertIn((res[0], res[1]), [(dx, 0), (0, 0)], f"Returned alias {res[:2]} instead of true or None")

    def test_consensus_tiles_masks(self):
        """Check consensus_tiles masks where exposed and partial tiles are False."""
        W, H = 320, 180
        s = 16
        gh = (H + s - 1) // s  # ceil(180/16) = 12
        gw = (W + s - 1) // s  # ceil(320/16) = 20

        rng = np.random.default_rng(seed=555)
        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
        dy = 16
        cur = np.zeros((H, W), dtype=np.uint8)
        cur[dy:, :] = prev[:H - dy, :]
        cur[:dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)

        mask = consensus_tiles(prev, cur, s, dx=0, dy=dy)
        self.assertEqual(mask.shape, (gh, gw))

        # Row 0 in cur corresponds to y=0..15 in cur, source y=-16..-1 in prev -> exposed -> False
        self.assertFalse(mask[0, 0])

        # Partial bottom tile row ty=11 (y=176..179) must be False
        self.assertFalse(mask[gh - 1, 0])

        # Internal tile ty=2 (y=32..47 in cur, source y=16..31 in prev) -> True
        self.assertTrue(mask[2, 0])

    def test_ops_accounting(self):
        """Test OPS increases after a call and resets with reset_ops."""
        reset_ops()
        self.assertEqual(gm_vechist.OPS, 0)

        W, H = 320, 180
        rng = np.random.default_rng(seed=100)
        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
        cur = prev.copy()

        res = estimate_global_motion(prev, cur, max_d=50)
        self.assertGreater(gm_vechist.OPS, 0, "OPS should increase after estimate_global_motion call")

        ops_before = gm_vechist.OPS
        consensus_tiles(prev, cur, 16, 0, 0)
        self.assertGreater(gm_vechist.OPS, ops_before, "OPS should increase after consensus_tiles call")

        reset_ops()
        self.assertEqual(gm_vechist.OPS, 0, "reset_ops should reset OPS to 0")

    def test_max_d_constraint(self):
        """max_d smaller than the true shift returns None or a vector within max_d."""
        W, H = 640, 480
        rng = np.random.default_rng(seed=888)
        dy = 100

        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
        cur = np.zeros((H, W), dtype=np.uint8)
        cur[dy:, :] = prev[:H - dy, :]
        cur[:dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)

        # Pass max_d = 50 (smaller than true shift 100)
        res = estimate_global_motion(prev, cur, max_d=50)
        if res is not None:
            self.assertLessEqual(abs(res[0]), 50)
            self.assertLessEqual(abs(res[1]), 50)


if __name__ == "__main__":
    unittest.main()
