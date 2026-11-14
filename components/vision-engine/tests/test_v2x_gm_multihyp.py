"""tests/test_v2x_gm_multihyp.py - Comprehensive unit tests for eval.v2x.gm_multihyp."""

import unittest
import numpy as np

from eval.v2x.gm_multihyp import estimate_global_motion, consensus_tiles, reset_ops, OPS
import eval.v2x.gm_multihyp as gm_mod
from visioncore.layout import grid_params


def make_random_scrolled_frame(width=320, height=180, dx=0, dy=0, seed=123):
    """Generate prev and cur luma frames with numpy default_rng fixed seed.

    Content is scrolled by (dx, dy), and exposed bands are filled with new random pixels.
    """
    rng = np.random.default_rng(seed)
    prev = rng.integers(0, 256, size=(height, width), dtype=np.uint8)
    cur = np.zeros((height, width), dtype=np.uint8)

    # Overlap regions
    cy0, cy1 = max(0, dy), min(height, height + dy)
    cx0, cx1 = max(0, dx), min(width, width + dx)

    if cy0 < cy1 and cx0 < cx1:
        py0, py1 = cy0 - dy, cy1 - dy
        px0, px1 = cx0 - dx, cx1 - dx
        cur[cy0:cy1, cx0:cx1] = prev[py0:py1, px0:px1]

    # Exposed regions filled with new random noise
    exposed_mask = np.ones((height, width), dtype=bool)
    if cy0 < cy1 and cx0 < cx1:
        exposed_mask[cy0:cy1, cx0:cx1] = False

    num_exposed = np.sum(exposed_mask)
    if num_exposed > 0:
        cur[exposed_mask] = rng.integers(0, 256, size=num_exposed, dtype=np.uint8)

    return prev, cur


class TestV2XGMMultiHyp(unittest.TestCase):

    def setUp(self):
        reset_ops()

    def test_vertical_scrolls(self):
        """Test vertical scrolls of 1, 7, 33, 200 px."""
        for dy in (1, 7, 33, 200):
            prev, cur = make_random_scrolled_frame(width=640, height=400, dx=0, dy=dy, seed=42 + dy)
            max_d = max(640, 400) - 1
            res = estimate_global_motion(prev, cur, max_d)
            self.assertIsNotNone(res, f"Failed for vertical scroll dy={dy}")
            est_dx, est_dy, score = res
            self.assertEqual((est_dx, est_dy), (0, dy), f"Expected (0, {dy}), got ({est_dx}, {est_dy})")
            self.assertEqual(type(est_dx), int)
            self.assertEqual(type(est_dy), int)
            self.assertEqual(type(score), int)

    def test_horizontal_scrolls(self):
        """Test horizontal scrolls of 5 and 64 px."""
        for dx in (5, 64):
            prev, cur = make_random_scrolled_frame(width=640, height=400, dx=dx, dy=0, seed=100 + dx)
            max_d = max(640, 400) - 1
            res = estimate_global_motion(prev, cur, max_d)
            self.assertIsNotNone(res, f"Failed for horizontal scroll dx={dx}")
            est_dx, est_dy, score = res
            self.assertEqual((est_dx, est_dy), (dx, 0), f"Expected ({dx}, 0), got ({est_dx}, {est_dy})")

    def test_1080p_large_scroll(self):
        """Test one 1920x1088 frame scrolled by 300 px vertically."""
        prev, cur = make_random_scrolled_frame(width=1920, height=1088, dx=0, dy=300, seed=1088)
        max_d = max(1920, 1088) - 1
        res = estimate_global_motion(prev, cur, max_d)
        self.assertIsNotNone(res, "Failed for 1920x1088 scroll dy=300")
        self.assertEqual((res[0], res[1]), (0, 300))

    def test_static_header_footer_scrolling_middle(self):
        """Test a static header band (40px) and footer band (30px) with a scrolling middle."""
        W, H = 640, 400
        header_h, footer_h = 40, 30
        dy = 25
        rng = np.random.default_rng(2024)

        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
        cur = np.zeros((H, W), dtype=np.uint8)

        # Header and footer static content
        header = rng.integers(0, 256, size=(header_h, W), dtype=np.uint8)
        footer = rng.integers(0, 256, size=(footer_h, W), dtype=np.uint8)

        prev[:header_h] = header
        cur[:header_h] = header
        prev[H - footer_h:] = footer
        cur[H - footer_h:] = footer

        # Middle scrolling
        mid_prev = prev[header_h:H - footer_h]
        mid_h = H - header_h - footer_h
        cur[header_h + dy:H - footer_h] = mid_prev[:mid_h - dy]
        cur[header_h:header_h + dy] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=max(W, H) - 1)
        self.assertIsNotNone(res)
        self.assertEqual((res[0], res[1]), (0, dy))

    def test_diagonal_window_drag(self):
        """Test diagonal window-drag-like move (dx=20, dy=15)."""
        prev, cur = make_random_scrolled_frame(width=640, height=400, dx=20, dy=15, seed=555)
        res = estimate_global_motion(prev, cur, max_d=max(640, 400) - 1)
        self.assertIsNotNone(res)
        self.assertEqual((res[0], res[1]), (20, 15))

    def test_flat_frame(self):
        """Flat frame must return None."""
        prev = np.full((180, 320), 128, dtype=np.uint8)
        cur = np.full((180, 320), 128, dtype=np.uint8)
        res = estimate_global_motion(prev, cur, max_d=319)
        self.assertIsNone(res)

    def test_periodic_stripe_pattern(self):
        """Periodic stripe pattern of period 8 must return either None or the true vector, never an alias."""
        W, H = 320, 180
        x = np.arange(W)[None, :]
        stripes = ((x // 8) % 2 * 200 + 20).astype(np.uint8)
        prev = np.broadcast_to(stripes, (H, W)).copy()

        # Zero shift case
        cur_zero = prev.copy()
        res_zero = estimate_global_motion(prev, cur_zero, max_d=319)
        if res_zero is not None:
            self.assertEqual((res_zero[0], res_zero[1]), (0, 0))

        # Shift by multiple of period (e.g., dx=16)
        dx = 16
        cur_shifted = np.zeros_like(prev)
        cur_shifted[:, dx:] = prev[:, :-dx]
        cur_shifted[:, :dx] = 128

        res_shifted = estimate_global_motion(prev, cur_shifted, max_d=319)
        if res_shifted is not None:
            self.assertIn((res_shifted[0], res_shifted[1]), [(dx, 0), (0, 0)])

    def test_consensus_tiles(self):
        """Test consensus_tiles masks where exposed and partial tiles are False."""
        W, H = 320, 192
        s, gw, gh = grid_params(W, H)
        dx, dy = 0, 16
        prev, cur = make_random_scrolled_frame(width=W, height=H, dx=dx, dy=dy, seed=777)

        mask = consensus_tiles(prev, cur, s, dx, dy)
        self.assertEqual(mask.shape, (gh, gw))
        self.assertIsInstance(mask, np.ndarray)
        self.assertEqual(mask.dtype, bool)

        # Top row (ty=0) in cur corresponds to py < 0 in prev (out of bounds) -> False
        self.assertFalse(mask[0, 0])
        # Second row (ty=1, row y=16 in cur) corresponds to row y=0 in prev -> True
        self.assertTrue(mask[1, 0])

    def test_ops_counter(self):
        """Test that OPS increases after a call and resets with reset_ops."""
        prev, cur = make_random_scrolled_frame(width=160, height=120, dx=0, dy=10, seed=888)
        reset_ops()
        self.assertEqual(gm_mod.OPS, 0)

        estimate_global_motion(prev, cur, max_d=159)
        ops_after_estimate = gm_mod.OPS
        self.assertGreater(ops_after_estimate, 0)

        reset_ops()
        self.assertEqual(gm_mod.OPS, 0)

    def test_max_d_smaller_than_true_shift(self):
        """max_d smaller than true shift returns None or a vector within max_d."""
        true_dy = 100
        prev, cur = make_random_scrolled_frame(width=320, height=240, dx=0, dy=true_dy, seed=999)
        max_d_small = 50

        res = estimate_global_motion(prev, cur, max_d=max_d_small)
        if res is not None:
            est_dx, est_dy, _ = res
            self.assertLessEqual(abs(est_dx), max_d_small)
            self.assertLessEqual(abs(est_dy), max_d_small)


if __name__ == "__main__":
    unittest.main()
