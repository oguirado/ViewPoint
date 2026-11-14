"""tests/test_v2proto_global_motion.py - Unit tests for v2proto global motion estimation."""

import unittest
import numpy as np
from eval.v2proto_global_motion import (
    estimate_global_motion,
    consensus_tiles,
    reset_sad_eval_count,
    get_sad_eval_count,
)
from visioncore.layout import grid_params


def make_text_frame(width=1280, height=800, seed=42, header_height=0, footer_height=0):
    """Generate a synthetic text-like luma frame with text lines, optional header, and optional footer."""
    rng = np.random.RandomState(seed)
    # Background luma ~ 240 (light)
    frame = np.full((height, width), 240, dtype=np.uint8)

    if header_height > 0:
        # Header area: dark background ~ 40, distinct pattern
        frame[:header_height, :] = 40
        for y in range(10, header_height - 10, 15):
            for x in range(20, width - 20, 80):
                frame[y:y+8, x:x+50] = 220

    if footer_height > 0:
        # Footer area: dark background ~ 60, distinct pattern
        frame[height - footer_height:, :] = 60
        for y in range(height - footer_height + 10, height - 10, 15):
            for x in range(30, width - 30, 90):
                frame[y:y+8, x:x+50] = 200

    # Text lines in body
    start_y = max(header_height + 20, 40)
    end_y = min(height - footer_height - 30, height - 30)
    for y in range(start_y, end_y, 25):
        # Draw pseudo text glyphs (blocks of dark pixels)
        x = 50
        while x < width - 100:
            glyph_w = rng.randint(5, 25)
            glyph_h = rng.randint(10, 18)
            frame[y:y+glyph_h, x:x+glyph_w] = rng.randint(20, 60)
            x += glyph_w + rng.randint(4, 12)

    return frame


def make_repeated_text_frame(width=1280, height=800, line_height=30):
    """Generate a frame with strictly periodic repeated text lines."""
    frame = np.full((height, width), 240, dtype=np.uint8)
    line_pattern = np.full((12, width), 240, dtype=np.uint8)
    for x in range(50, width - 100, 40):
        line_pattern[2:10, x:x+20] = 30

    for y in range(0, height - line_height, line_height):
        frame[y:y+12, :] = line_pattern

    return frame


class TestV2ProtoGlobalMotion(unittest.TestCase):

    def test_vertical_scrolls(self):
        """Test vertical scrolls of 8, 64, 200, and 500 px at 1280x800."""
        W, H = 1280, 800
        big_canvas = make_text_frame(W, H + 1200, seed=123)

        for dy in (8, 64, 200, 500):
            # Content shifted by (0, dy): prev at offset y, cur at offset y - dy
            # So cur[y, x] = prev[y - dy, x]
            prev = big_canvas[600:600+H, :]
            cur = big_canvas[600-dy:600-dy+H, :]

            res = estimate_global_motion(prev, cur, max_d=600)
            self.assertIsNotNone(res, f"Failed for vertical scroll dy={dy}")
            est_dx, est_dy, score = res
            self.assertEqual((est_dx, est_dy), (0, dy), f"Expected (0, {dy}), got ({est_dx}, {est_dy})")
            self.assertEqual(score, 0, f"Expected perfect SAD score 0, got {score}")

    def test_large_vertical_and_horizontal_scrolls(self):
        """Test large vertical and horizontal scrolls up to half frame (e.g. 1920x1080 frame)."""
        W, H = 1920, 1080
        # Large vertical scroll (up to H/2 = 540 px)
        big_canvas_v = make_text_frame(W, H + 1200, seed=101)
        dy = 500
        prev_v = big_canvas_v[600:600+H, :]
        cur_v = big_canvas_v[600-dy:600-dy+H, :]

        res_v = estimate_global_motion(prev_v, cur_v, max_d=600)
        self.assertIsNotNone(res_v, f"Failed for 1080p vertical scroll dy={dy}")
        self.assertEqual((res_v[0], res_v[1]), (0, dy))

        # Large horizontal scroll (dx = 500 px)
        big_canvas_h = make_text_frame(W + 1500, H, seed=202)
        dx = 500
        prev_h = big_canvas_h[:, 600:600+W]
        cur_h = big_canvas_h[:, 600-dx:600-dx+W]

        res_h = estimate_global_motion(prev_h, cur_h, max_d=600)
        self.assertIsNotNone(res_h, f"Failed for 1080p horizontal scroll dx={dx}")
        self.assertEqual((res_h[0], res_h[1]), (dx, 0))

    def test_horizontal_scroll(self):
        """Test horizontal scroll of 150 px."""
        W, H = 1280, 800
        big_canvas = make_text_frame(W + 500, H, seed=456)

        dx = 150
        # Content shifted by (dx, 0): cur[y, x] = prev[y, x - dx]
        prev = big_canvas[:, 250:250+W]
        cur = big_canvas[:, 250-dx:250-dx+W]

        res = estimate_global_motion(prev, cur, max_d=300)
        self.assertIsNotNone(res, "Failed for horizontal scroll")
        est_dx, est_dy, score = res
        self.assertEqual((est_dx, est_dy), (dx, 0), f"Expected ({dx}, 0), got ({est_dx}, {est_dy})")
        self.assertEqual(score, 0)

    def test_scroll_with_fixed_header(self):
        """Test vertical scroll of 100 px with a fixed header band of 80 px."""
        W, H = 1280, 800
        header_h = 80
        big_body = make_text_frame(W, H + 500, seed=789)

        dy = 100
        # Body shifted by dy
        prev = big_body[200:200+H, :].copy()
        cur = big_body[200-dy:200-dy+H, :].copy()

        # Fixed header band on both
        header = make_text_frame(W, header_h, seed=999, header_height=header_h)[:header_h, :]
        prev[:header_h, :] = header
        cur[:header_h, :] = header

        res = estimate_global_motion(prev, cur, max_d=400)
        self.assertIsNotNone(res, "Failed for scroll with fixed header")
        est_dx, est_dy, _ = res
        self.assertEqual((est_dx, est_dy), (0, dy))

    def test_scroll_with_fixed_header_and_footer(self):
        """Test vertical scroll with both fixed header (80px) and footer (60px)."""
        W, H = 1280, 800
        header_h = 80
        footer_h = 60
        big_body = make_text_frame(W, H + 500, seed=333)

        dy = 120
        prev = big_body[200:200+H, :].copy()
        cur = big_body[200-dy:200-dy+H, :].copy()

        fixed_hf = make_text_frame(W, H, seed=444, header_height=header_h, footer_height=footer_h)
        prev[:header_h, :] = fixed_hf[:header_h, :]
        cur[:header_h, :] = fixed_hf[:header_h, :]
        prev[H - footer_h:, :] = fixed_hf[H - footer_h:, :]
        cur[H - footer_h:, :] = fixed_hf[H - footer_h:, :]

        res = estimate_global_motion(prev, cur, max_d=400)
        self.assertIsNotNone(res, "Failed for scroll with fixed header and footer")
        est_dx, est_dy, _ = res
        self.assertEqual((est_dx, est_dy), (0, dy))

    def test_repeated_text_periodic_content(self):
        """Repeated text periodic content must return None or the true vector, never an alias."""
        W, H = 1280, 800
        line_h = 30
        periodic_frame = make_repeated_text_frame(W, H, line_height=line_h)

        # Zero motion on periodic content -> true vector is (0, 0)
        res_zero = estimate_global_motion(periodic_frame, periodic_frame, max_d=300)
        if res_zero is not None:
            self.assertEqual((res_zero[0], res_zero[1]), (0, 0), "Periodic content returned wrong vector for zero shift")

        # Shift by multiple of period (60 px = 2 * line_h)
        dy = 60
        big_periodic = make_repeated_text_frame(W, H + 300, line_height=line_h)
        prev_p = big_periodic[100:100+H, :]
        cur_p = big_periodic[100-dy:100-dy+H, :]

        res_p = estimate_global_motion(prev_p, cur_p, max_d=300)
        if res_p is not None:
            # Must return true vector (0, dy) or None due to ambiguity, but never an alias like (0, 30) or (0, 90)
            self.assertIn((res_p[0], res_p[1]), [(0, dy), (0, 0)], "Returned alias instead of true vector or None")

    def test_flat_frame(self):
        """Flat frame should return None."""
        prev = np.full((800, 1280), 128, dtype=np.uint8)
        cur = np.full((800, 1280), 128, dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=400)
        self.assertIsNone(res, "Flat frame should return None")

    def test_periodic_stripes(self):
        """Periodic stripes should return None due to non-uniqueness."""
        W, H = 1280, 800
        y_coords, x_coords = np.indices((H, W))
        # 16-pixel periodic vertical stripes
        stripes = np.where((x_coords // 16) % 2 == 0, 200, 50).astype(np.uint8)

        prev = stripes
        cur = stripes

        res = estimate_global_motion(prev, cur, max_d=200)
        self.assertIsNone(res, "Periodic stripes should return None")

    def test_cost_regression_sad_eval_count(self):
        """Cost regression test counting SAD evaluations on a 1080p frame for a large scroll."""
        W, H = 1920, 1080
        big_canvas = make_text_frame(W, H + 1200, seed=555)

        dy = 400
        prev = big_canvas[600:600+H, :]
        cur = big_canvas[600-dy:600-dy+H, :]

        reset_sad_eval_count()
        res = estimate_global_motion(prev, cur, max_d=600)
        self.assertIsNotNone(res)

        eval_count = get_sad_eval_count()
        # Assert upper bound on SAD evaluations (< 4000 block/frame evaluations)
        upper_bound = 4000
        self.assertLessEqual(
            eval_count,
            upper_bound,
            f"SAD evaluation count {eval_count} exceeded upper bound {upper_bound}"
        )

    def test_consensus_tiles(self):
        """Test consensus_tiles mask calculation."""
        W, H = 1280, 800
        s, gw, gh = grid_params(W, H)

        big_canvas = make_text_frame(W, H + 200, seed=321)
        dy = 32
        prev = big_canvas[100:100+H, :]
        cur = big_canvas[100-dy:100-dy+H, :]

        mask = consensus_tiles(prev, cur, s, dx=0, dy=dy)
        self.assertEqual(mask.shape, (gh, gw))
        # With s=16 and dy=32, ty=2 is y=32 in cur, corresponding to y=0 in prev
        self.assertTrue(mask[2, 0], "Tile should match shifted previous frame content")


if __name__ == "__main__":
    unittest.main()
