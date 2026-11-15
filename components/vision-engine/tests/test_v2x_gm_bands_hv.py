"""tests/test_v2x_gm_bands_hv.py - Unit tests for eval.v2x.gm_bands_hv."""

import unittest
import numpy as np
from eval.v2x.gm_bands_hv import estimate_global_motion, consensus_tiles, OPS, reset_ops


class TestV2XGMBandsHV(unittest.TestCase):

    def setUp(self):
        reset_ops()

    def test_textured_random_vertical_scrolls(self):
        """Test textured random frames scrolled vertically by 1, 7, 33, and 200 px with exposed band filled with new random content."""
        W, H = 640, 480
        s = 16
        for dy in (1, 7, 33, 200):
            reset_ops()
            rng = np.random.default_rng(1000 + dy)
            prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
            cur = np.empty_like(prev)
            if dy > 0:
                cur[dy:, :] = prev[:-dy, :]
                cur[:dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)
            else:
                cur[:H+dy, :] = prev[-dy:, :]
                cur[H+dy:, :] = rng.integers(0, 256, size=(-dy, W), dtype=np.uint8)

            res = estimate_global_motion(prev, cur, max_d=300)
            self.assertIsNotNone(res, f"Failed for vertical scroll dy={dy}")
            est_dx, est_dy, score = res
            self.assertEqual((est_dx, est_dy), (0, dy))
            self.assertEqual(score, 0)

            # Test consensus_tiles mask
            mask = consensus_tiles(prev, cur, s, 0, dy)
            gh, gw = (H + s - 1) // s, (W + s - 1) // s
            self.assertEqual(mask.shape, (gh, gw))

            # Tile ty is True iff all rows in ty*s..(ty+1)*s are >= dy
            for ty in range(gh):
                r0 = ty * s
                r1 = min((ty + 1) * s, H)
                expected_true = (r0 >= dy) and (r1 - dy <= H - dy)
                self.assertEqual(mask[ty, 0], expected_true, f"Mismatch at ty={ty} for dy={dy}")

    def test_textured_random_horizontal_scrolls(self):
        """Test textured random frames scrolled horizontally by 5 and 64 px."""
        W, H = 640, 480
        s = 16
        for dx in (5, 64):
            reset_ops()
            rng = np.random.default_rng(2000 + dx)
            prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
            cur = np.empty_like(prev)
            if dx > 0:
                cur[:, dx:] = prev[:, :-dx]
                cur[:, :dx] = rng.integers(0, 256, size=(H, dx), dtype=np.uint8)
            else:
                cur[:, :W+dx] = prev[:, -dx:]
                cur[:, W+dx:] = rng.integers(0, 256, size=(H, -dx), dtype=np.uint8)

            res = estimate_global_motion(prev, cur, max_d=300)
            self.assertIsNotNone(res, f"Failed for horizontal scroll dx={dx}")
            est_dx, est_dy, score = res
            self.assertEqual((est_dx, est_dy), (dx, 0))
            self.assertEqual(score, 0)

            mask = consensus_tiles(prev, cur, s, dx, 0)
            gh, gw = (H + s - 1) // s, (W + s - 1) // s
            self.assertEqual(mask.shape, (gh, gw))

            for tx in range(gw):
                c0 = tx * s
                expected_true = (c0 >= dx)
                self.assertEqual(mask[0, tx], expected_true, f"Mismatch at tx={tx} for dx={dx}")

    def test_1080p_large_scroll_300px(self):
        """Test one 1920x1088 frame scrolled by 300 px."""
        W, H = 1920, 1088
        dy = 300
        rng = np.random.default_rng(3000)
        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
        cur = np.empty_like(prev)
        cur[dy:, :] = prev[:-dy, :]
        cur[:dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=600)
        self.assertIsNotNone(res)
        self.assertEqual((res[0], res[1]), (0, dy))
        self.assertEqual(res[2], 0)

    def test_static_header_footer_with_scrolling_middle(self):
        """Test a frame with static header (50px) and footer (40px) bands and scrolling middle."""
        W, H = 640, 480
        header_h = 50
        footer_h = 40
        dy = 30
        rng = np.random.default_rng(4000)

        # Static chrome
        header = rng.integers(0, 256, size=(header_h, W), dtype=np.uint8)
        footer = rng.integers(0, 256, size=(footer_h, W), dtype=np.uint8)

        # Scrolling middle
        mid_h = H - header_h - footer_h
        prev_mid = rng.integers(0, 256, size=(mid_h, W), dtype=np.uint8)
        cur_mid = np.empty_like(prev_mid)
        cur_mid[dy:, :] = prev_mid[:-dy, :]
        cur_mid[:dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)

        prev = np.empty((H, W), dtype=np.uint8)
        cur = np.empty((H, W), dtype=np.uint8)

        prev[:header_h, :] = header
        cur[:header_h, :] = header

        prev[header_h:H-footer_h, :] = prev_mid
        cur[header_h:H-footer_h, :] = cur_mid

        prev[H-footer_h:, :] = footer
        cur[H-footer_h:, :] = footer

        res = estimate_global_motion(prev, cur, max_d=200)
        self.assertIsNotNone(res)
        self.assertEqual((res[0], res[1]), (0, dy))
        self.assertEqual(res[2], 0)

    def test_diagonal_window_drag_move(self):
        """Test a diagonal move dx=16, dy=24."""
        W, H = 640, 480
        dx, dy = 16, 24
        rng = np.random.default_rng(5000)

        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
        cur = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
        cur[dy:, dx:] = prev[:-dy, :-dx]

        res = estimate_global_motion(prev, cur, max_d=200)
        self.assertIsNotNone(res)
        self.assertEqual((res[0], res[1]), (dx, dy))
        self.assertEqual(res[2], 0)

    def test_flat_frames(self):
        """Flat frames should return None."""
        prev = np.full((100, 100), 128, dtype=np.uint8)
        cur = np.full((100, 100), 128, dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=50)
        self.assertIsNone(res)

    def test_periodic_stripe_pattern(self):
        """A periodic stripe pattern of period 8 must return None or the true vector, never an alias."""
        W, H = 640, 480
        x_coords = np.arange(W)
        # Period 8 vertical stripe pattern
        stripes = np.where((x_coords // 4) % 2 == 0, 200, 50).astype(np.uint8)
        prev = np.tile(stripes, (H, 1))

        # Shift by 8 px
        dx = 8
        cur = np.roll(prev, shift=dx, axis=1)

        res = estimate_global_motion(prev, cur, max_d=100)
        if res is not None:
            self.assertIn((res[0], res[1]), [(dx, 0), (0, 0)], "Returned alias instead of true vector or None")

    def test_consensus_tiles_exposed_and_partial_tiles(self):
        """Test consensus_tiles masks where exposed and partial tiles are False."""
        W, H = 320, 180  # s=16 -> gh=12, gw=20 (partial tiles at bottom edge: 180 = 11*16 + 4)
        s = 16
        dx, dy = 0, 16
        rng = np.random.default_rng(6000)

        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
        cur = np.empty_like(prev)
        cur[dy:, :] = prev[:-dy, :]
        cur[:dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)

        mask = consensus_tiles(prev, cur, s, dx, dy)
        self.assertFalse(mask[0, 0], "Top exposed tile must be False")
        self.assertTrue(mask[1, 0], "Interior tile must be True")

        # Test partial edge tiles leaving frame
        dy_large = 170
        cur_l = np.empty_like(prev)
        cur_l[dy_large:, :] = prev[:-dy_large, :]
        cur_l[:dy_large, :] = rng.integers(0, 256, size=(dy_large, W), dtype=np.uint8)

        mask_l = consensus_tiles(prev, cur_l, s, dx, dy_large)
        self.assertFalse(mask_l[0, 0])

    def test_ops_counter_and_reset_ops(self):
        """OPS reports the latest call and resets with reset_ops."""
        reset_ops()
        from eval.v2x.gm_bands_hv import OPS as ops1
        self.assertEqual(ops1, 0)

        W, H = 100, 100
        rng = np.random.default_rng(7000)
        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
        cur = rng.integers(0, 256, size=(H, W), dtype=np.uint8)

        estimate_global_motion(prev, cur, max_d=20)
        from eval.v2x.gm_bands_hv import OPS as ops2
        self.assertGreater(ops2, 0)

        consensus_tiles(prev, cur, 16, 0, 0)
        from eval.v2x.gm_bands_hv import OPS as ops3
        self.assertGreater(ops3, 0)
        consensus_tiles(prev, cur, 16, 0, 0)
        from eval.v2x.gm_bands_hv import OPS as ops_repeat
        self.assertEqual(ops_repeat, ops3)

        reset_ops()
        from eval.v2x.gm_bands_hv import OPS as ops4
        self.assertEqual(ops4, 0)

    def test_max_d_smaller_than_true_shift(self):
        """max_d smaller than the true shift returns None or a vector within max_d."""
        W, H = 640, 480
        true_dy = 100
        max_d = 40
        rng = np.random.default_rng(8000)

        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
        cur = np.empty_like(prev)
        cur[true_dy:, :] = prev[:-true_dy, :]
        cur[:true_dy, :] = rng.integers(0, 256, size=(true_dy, W), dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=max_d)
        if res is not None:
            self.assertLessEqual(abs(res[0]), max_d)
            self.assertLessEqual(abs(res[1]), max_d)


if __name__ == "__main__":
    unittest.main()
