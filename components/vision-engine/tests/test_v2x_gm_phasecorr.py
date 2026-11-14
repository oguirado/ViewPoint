"""tests/test_v2x_gm_phasecorr.py - Unit tests for global motion phase correlation estimator.
"""

import unittest
import numpy as np

import eval.v2x.gm_phasecorr as gm


def _make_scrolled_frame(rng, H, W, dx, dy):
    """Generate prev and cur luma frames shifted by (dx, dy).

    cur[y, x] = prev[y - dy, x - dx]. Exposed band filled with new random content.
    """
    prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
    cur = rng.integers(0, 256, size=(H, W), dtype=np.uint8)

    cy0, cy1 = max(0, dy), min(H, H + dy)
    cx0, cx1 = max(0, dx), min(W, W + dx)
    py0, py1 = cy0 - dy, cy1 - dy
    px0, px1 = cx0 - dx, cx1 - dx

    if cy0 < cy1 and cx0 < cx1:
        cur[cy0:cy1, cx0:cx1] = prev[py0:py1, px0:px1]

    return prev, cur


class TestGMPhaseCorr(unittest.TestCase):

    def setUp(self):
        gm.reset_ops()

    def test_vertical_scrolls(self):
        """Textured random frames scrolled vertically by 1, 7, 33 and 200 px."""
        rng = np.random.default_rng(12345)
        H, W = 320, 320
        max_d = max(H, W) - 1

        for shift in (1, 7, 33, 200):
            prev, cur = _make_scrolled_frame(rng, H, W, 0, shift)
            res = gm.estimate_global_motion(prev, cur, max_d)
            self.assertIsNotNone(res)
            dx, dy, score = res
            self.assertEqual((dx, dy), (0, shift))
            self.assertIsInstance(dx, int)
            self.assertIsInstance(dy, int)
            self.assertIsInstance(score, int)

    def test_horizontal_scrolls(self):
        """Textured random frames scrolled horizontally by 5 and 64 px."""
        rng = np.random.default_rng(54321)
        H, W = 320, 320
        max_d = max(H, W) - 1

        for shift in (5, 64):
            prev, cur = _make_scrolled_frame(rng, H, W, shift, 0)
            res = gm.estimate_global_motion(prev, cur, max_d)
            self.assertIsNotNone(res)
            dx, dy, score = res
            self.assertEqual((dx, dy), (shift, 0))

    def test_1080p_large_scroll(self):
        """One 1920x1088 frame scrolled by 300 px."""
        rng = np.random.default_rng(999)
        H, W = 1088, 1920
        max_d = max(H, W) - 1

        prev, cur = _make_scrolled_frame(rng, H, W, 0, 300)
        res = gm.estimate_global_motion(prev, cur, max_d)
        self.assertIsNotNone(res)
        dx, dy, score = res
        self.assertEqual((dx, dy), (0, 300))

    def test_static_header_footer_scrolling_middle(self):
        """Static header band and footer band with a scrolling middle."""
        rng = np.random.default_rng(2024)
        H, W = 240, 320
        max_d = max(H, W) - 1

        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
        cur = prev.copy()

        # Middle section (rows 40..200) scrolled down by 10 px
        mid_prev = prev[40:190, :]
        cur[50:200, :] = mid_prev
        cur[40:50, :] = rng.integers(0, 256, size=(10, W), dtype=np.uint8)

        # Global motion estimator should return None or (0, 0) or middle scroll depending on SAD ratio
        res = gm.estimate_global_motion(prev, cur, max_d)
        if res is not None:
            dx, dy, _ = res
            self.assertIn((dx, dy), [(0, 0), (0, 10)])

    def test_diagonal_window_drag(self):
        """A diagonal window-drag-like move."""
        rng = np.random.default_rng(777)
        H, W = 240, 320
        max_d = max(H, W) - 1

        prev, cur = _make_scrolled_frame(rng, H, W, 12, 18)
        res = gm.estimate_global_motion(prev, cur, max_d)
        self.assertIsNotNone(res)
        dx, dy, _ = res
        self.assertEqual((dx, dy), (12, 18))

    def test_flat_frames_return_none(self):
        """Flat frames returning None."""
        prev = np.full((100, 100), 128, dtype=np.uint8)
        cur = np.full((100, 100), 128, dtype=np.uint8)

        res = gm.estimate_global_motion(prev, cur, 99)
        self.assertIsNone(res)

    def test_periodic_stripe_pattern(self):
        """A periodic stripe pattern of period 8 returns either None or the true vector, never an alias."""
        H, W = 120, 120
        # Stripe pattern of period 8
        stripe = np.tile([0, 0, 0, 0, 255, 255, 255, 255], W // 8)
        prev = np.tile(stripe, (H, 1)).astype(np.uint8)

        # Shift by true shift (e.g. 2 px horizontally)
        cur = np.roll(prev, shift=2, axis=1)

        res = gm.estimate_global_motion(prev, cur, 50)
        if res is not None:
            dx, dy, _ = res
            self.assertEqual((dx, dy), (2, 0))

    def test_consensus_tiles_masks(self):
        """consensus_tiles masks where exposed and partial tiles are False."""
        rng = np.random.default_rng(101)
        H, W = 64, 64
        s = 16
        dx, dy = 8, 0

        prev, cur = _make_scrolled_frame(rng, H, W, dx, dy)
        mask = gm.consensus_tiles(prev, cur, s, dx, dy)

        self.assertEqual(mask.shape, (4, 4))
        # Exposed left tiles (column 0) cannot be equal because source left the frame
        self.assertFalse(mask[:, 0].any())

    def test_ops_counter_and_reset(self):
        """OPS increases after a call and resets with reset_ops."""
        gm.reset_ops()
        self.assertEqual(gm.OPS, 0)

        rng = np.random.default_rng(42)
        prev, cur = _make_scrolled_frame(rng, 64, 64, 2, 2)
        _ = gm.estimate_global_motion(prev, cur, 63)

        self.assertGreater(gm.OPS, 0)

        gm.reset_ops()
        self.assertEqual(gm.OPS, 0)

    def test_max_d_constraint(self):
        """max_d smaller than the true shift returns None or a vector within max_d."""
        rng = np.random.default_rng(33)
        H, W = 200, 200
        true_shift = 50

        prev, cur = _make_scrolled_frame(rng, H, W, 0, true_shift)

        # Call with max_d smaller than true shift
        res = gm.estimate_global_motion(prev, cur, max_d=20)
        if res is not None:
            dx, dy, _ = res
            self.assertLessEqual(max(abs(dx), abs(dy)), 20)


if __name__ == "__main__":
    unittest.main()
