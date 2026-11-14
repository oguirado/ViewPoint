"""tests/test_v2x_gm_textline.py - Unit tests for eval.v2x.gm_textline global motion estimator."""

import unittest
import numpy as np
from eval.v2x.gm_textline import (
    estimate_global_motion,
    consensus_tiles,
    reset_ops,
    get_ops,
)


def make_text_frame(rng, width=1280, height=800, num_lines=15):
    """Generate synthetic luma frame with horizontal text lines separated by empty rows."""
    frame = np.full((height, width), 240, dtype=np.uint8)
    if height < 30:
        return frame
    line_ys = np.linspace(10, height - 20, max(1, num_lines), dtype=int)

    for y in line_ys:
        line_h = rng.integers(10, 18)
        if y + line_h >= height:
            continue
        x = 40
        while x < width - 60:
            gw = rng.integers(6, 25)
            gh = min(rng.integers(6, line_h), height - y)
            if gh > 0:
                frame[y:y + gh, x:x + gw] = rng.integers(20, 80, size=(gh, gw), dtype=np.uint8)
            x += gw + rng.integers(6, 16)

    return frame


class TestV2XGMTextline(unittest.TestCase):

    def test_vertical_and_horizontal_scrolls_with_exposed_band(self):
        """Test vertical scrolls (1, 7, 33, 200 px) and horizontal scrolls (5, 64 px)

        with exposed bands filled with new random content.
        """
        W, H = 1280, 800

        # Vertical scrolls: 1, 7, 33, 200 px
        for dy in (1, 7, 33, 200):
            rng = np.random.default_rng(1000 + dy)
            big = make_text_frame(rng, W, H + 600, num_lines=25)
            prev = big[300:300 + H, :]
            cur = big[300 - dy:300 - dy + H, :]

            res = estimate_global_motion(prev, cur, max_d=400)
            self.assertIsNotNone(res, f"Failed for vertical scroll dy={dy}")
            est_dx, est_dy, score = res
            self.assertEqual((est_dx, est_dy), (0, dy), f"Expected (0, {dy}), got ({est_dx}, {est_dy})")
            self.assertEqual(score, 0, f"Expected perfect SAD score 0, got {score}")

        # Horizontal scrolls: 5, 64 px
        for dx in (5, 64):
            rng = np.random.default_rng(2000 + dx)
            big = make_text_frame(rng, W + 600, H, num_lines=20)
            prev = big[:, 300:300 + W]
            cur = big[:, 300 - dx:300 - dx + W]

            res = estimate_global_motion(prev, cur, max_d=400)
            self.assertIsNotNone(res, f"Failed for horizontal scroll dx={dx}")
            est_dx, est_dy, score = res
            self.assertEqual((est_dx, est_dy), (dx, 0), f"Expected ({dx}, 0), got ({est_dx}, {est_dy})")
            self.assertEqual(score, 0, f"Expected perfect SAD score 0, got {score}")

    def test_1080p_large_vertical_scroll(self):
        """Test one 1920x1088 frame scrolled vertically by 300 px."""
        W, H = 1920, 1088
        rng = np.random.default_rng(3001)

        dy = 300
        big = make_text_frame(rng, W, H + 800, num_lines=30)
        prev = big[400:400 + H, :]
        cur = big[400 - dy:400 - dy + H, :]

        res = estimate_global_motion(prev, cur, max_d=600)
        self.assertIsNotNone(res, "Failed for 1920x1088 300px scroll")
        self.assertEqual((res[0], res[1]), (0, dy), f"Expected (0, {dy}), got ({res[0]}, {res[1]})")
        self.assertEqual(res[2], 0)

    def test_static_header_and_footer_bands(self):
        """Test vertical scroll with static header band and footer band."""
        W, H = 1280, 800
        rng = np.random.default_rng(4001)

        header_h = 60
        footer_h = 50
        dy = 80

        big_body = make_text_frame(rng, W, H + 500, num_lines=25)
        prev = big_body[200:200 + H, :].copy()
        cur = big_body[200 - dy:200 - dy + H, :].copy()

        # Fixed header and footer band
        header = make_text_frame(rng, W, header_h, num_lines=2)[:header_h, :]
        footer = make_text_frame(rng, W, footer_h, num_lines=2)[:footer_h, :]

        prev[:header_h, :] = header
        cur[:header_h, :] = header
        prev[H - footer_h:, :] = footer
        cur[H - footer_h:, :] = footer

        res = estimate_global_motion(prev, cur, max_d=400)
        self.assertIsNotNone(res, "Failed for static header/footer with scrolling middle")
        self.assertEqual((res[0], res[1]), (0, dy))

    def test_diagonal_window_drag(self):
        """Test diagonal window-drag-like move (dx=25, dy=15)."""
        W, H = 1280, 800
        rng = np.random.default_rng(5001)

        dx, dy = 25, 15
        big = make_text_frame(rng, W + 500, H + 500, num_lines=25)
        prev = big[200:200 + H, 200:200 + W]
        cur = big[200 - dy:200 - dy + H, 200 - dx:200 - dx + W]

        res = estimate_global_motion(prev, cur, max_d=400)
        self.assertIsNotNone(res, "Failed for diagonal window-drag move")
        self.assertEqual((res[0], res[1]), (dx, dy))

    def test_flat_frames(self):
        """Flat frame should return None."""
        prev = np.full((800, 1280), 128, dtype=np.uint8)
        cur = np.full((800, 1280), 128, dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=400)
        self.assertIsNone(res, "Flat frame must return None")

    def test_periodic_stripe_pattern(self):
        """Periodic stripe pattern of period 8 must return None or true vector, never an alias."""
        W, H = 1280, 800
        y_coords, x_coords = np.indices((H, W))
        stripes = np.where((x_coords // 8) % 2 == 0, 200, 50).astype(np.uint8)

        prev = stripes
        cur = stripes  # Zero motion, true vector is (0, 0)

        res = estimate_global_motion(prev, cur, max_d=200)
        if res is not None:
            self.assertEqual((res[0], res[1]), (0, 0), "Periodic stripes returned alias instead of (0,0) or None")

    def test_consensus_tiles(self):
        """Test consensus_tiles masks where exposed and partial out-of-bounds tiles are False."""
        W, H = 1280, 800
        s = 16
        gh = (H + s - 1) // s
        gw = (W + s - 1) // s

        rng = np.random.default_rng(6001)
        big = make_text_frame(rng, W, H + 200, num_lines=20)
        dy = 32
        prev = big[100:100 + H, :]
        cur = big[100 - dy:100 - dy + H, :]

        mask = consensus_tiles(prev, cur, s, dx=0, dy=dy)
        self.assertEqual(mask.shape, (gh, gw))

        # Exposed top 2 tile rows (ty=0: y=0..16, ty=1: y=16..32) must be False
        self.assertFalse(mask[0, 0], "Exposed top tile must be False")
        self.assertFalse(mask[1, 0], "Exposed top tile must be False")

        # Unexposed interior matching tile (ty=2: y=32..48) should be True
        self.assertTrue(mask[2, 0], "Interior matching tile should be True")

    def test_ops_counter_and_reset(self):
        """Test OPS counter increases after a call and resets with reset_ops."""
        W, H = 1280, 800
        rng = np.random.default_rng(7001)

        prev = make_text_frame(rng, W, H, num_lines=10)
        cur = make_text_frame(rng, W, H, num_lines=10)

        reset_ops()
        self.assertEqual(get_ops(), 0, "OPS should be 0 after reset_ops()")

        estimate_global_motion(prev, cur, max_d=200)
        ops_after_est = get_ops()
        self.assertGreater(ops_after_est, 0, "OPS must increase after estimate_global_motion call")

        reset_ops()
        self.assertEqual(get_ops(), 0, "OPS should be 0")

        consensus_tiles(prev, cur, 16, 0, 16)
        ops_after_ct = get_ops()
        self.assertGreater(ops_after_ct, 0, "OPS must increase after consensus_tiles call")

    def test_max_d_constraint(self):
        """Test max_d smaller than true shift returns None or a vector within max_d."""
        W, H = 1280, 800
        rng = np.random.default_rng(8001)

        dy = 150
        big = make_text_frame(rng, W, H + 400, num_lines=20)
        prev = big[200:200 + H, :]
        cur = big[200 - dy:200 - dy + H, :]

        max_d_small = 50
        res = estimate_global_motion(prev, cur, max_d=max_d_small)

        if res is not None:
            est_dx, est_dy, _ = res
            self.assertLessEqual(abs(est_dx), max_d_small, f"est_dx {est_dx} exceeded max_d {max_d_small}")
            self.assertLessEqual(abs(est_dy), max_d_small, f"est_dy {est_dy} exceeded max_d {max_d_small}")


if __name__ == "__main__":
    unittest.main()
