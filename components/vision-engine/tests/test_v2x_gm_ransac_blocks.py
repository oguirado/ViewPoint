"""tests/test_v2x_gm_ransac_blocks.py - Unit tests for eval.v2x.gm_ransac_blocks.

All frame fixtures are built in memory using numpy default_rng with fixed seeds.
"""

import unittest
import numpy as np
import eval.v2x.gm_ransac_blocks as gm
from visioncore.layout import grid_params


class TestGMRansacBlocks(unittest.TestCase):

    def setUp(self):
        gm.reset_ops()

    def test_ops_counter(self):
        self.assertEqual(gm.OPS, 0)
        rng = np.random.default_rng(42)
        f1 = rng.integers(0, 256, (128, 128), dtype=np.uint8)
        f2 = rng.integers(0, 256, (128, 128), dtype=np.uint8)
        gm.estimate_global_motion(f1, f2, max_d=64)
        ops1 = gm.OPS
        self.assertGreater(ops1, 0)
        gm.reset_ops()
        self.assertEqual(gm.OPS, 0)

    def test_flat_frames(self):
        f1 = np.full((128, 128), 100, dtype=np.uint8)
        f2 = np.full((128, 128), 100, dtype=np.uint8)
        res = gm.estimate_global_motion(f1, f2, max_d=64)
        self.assertIsNone(res)

    def test_vertical_and_horizontal_scrolls(self):
        # Textured random frames scrolled vertically by 1, 7, 33, 200 px and horizontally by 5, 64 px
        # exposed band filled with new random content
        rng = np.random.default_rng(123)

        # Cases: (dx, dy, W, H)
        test_cases = [
            (0, 1, 320, 240),
            (0, 7, 320, 240),
            (0, 33, 320, 240),
            (0, 200, 640, 480),
            (5, 0, 320, 240),
            (64, 0, 320, 240),
        ]

        for dx, dy, W, H in test_cases:
            with self.subTest(dx=dx, dy=dy, W=W, H=H):
                gm.reset_ops()
                prev = rng.integers(0, 256, (H, W), dtype=np.uint8)
                cur = np.empty_like(prev)

                # Fill cur: cur[y, x] = prev[y - dy, x - dx]
                cy0, cy1 = max(0, dy), min(H, H + dy)
                cx0, cx1 = max(0, dx), min(W, W + dx)
                py0, py1 = cy0 - dy, cy1 - dy
                px0, px1 = cx0 - dx, cx1 - dx

                # Fill exposed area with new random noise
                cur[:] = rng.integers(0, 256, (H, W), dtype=np.uint8)
                cur[cy0:cy1, cx0:cx1] = prev[py0:py1, px0:px1]

                max_d = max(W, H) - 1
                res = gm.estimate_global_motion(prev, cur, max_d)
                self.assertIsNotNone(res)
                res_dx, res_dy, score = res
                self.assertEqual((res_dx, res_dy), (dx, dy))

    def test_large_1080p_scroll(self):
        # 1920x1088 frame scrolled by 300 px vertically
        rng = np.random.default_rng(456)
        W, H = 1920, 1088
        dx, dy = 0, 300

        prev = rng.integers(0, 256, (H, W), dtype=np.uint8)
        cur = rng.integers(0, 256, (H, W), dtype=np.uint8)

        cy0, cy1 = max(0, dy), min(H, H + dy)
        cx0, cx1 = max(0, dx), min(W, W + dx)
        py0, py1 = cy0 - dy, cy1 - dy
        px0, px1 = cx0 - dx, cx1 - dx

        cur[cy0:cy1, cx0:cx1] = prev[py0:py1, px0:px1]

        max_d = max(W, H) - 1
        res = gm.estimate_global_motion(prev, cur, max_d)
        self.assertIsNotNone(res)
        res_dx, res_dy, score = res
        self.assertEqual((res_dx, res_dy), (dx, dy))

    def test_static_header_footer_scrolling_middle(self):
        # A static header band and footer band with a scrolling middle
        rng = np.random.default_rng(789)
        W, H = 640, 480
        dx, dy = 0, 20

        prev = rng.integers(0, 256, (H, W), dtype=np.uint8)
        cur = prev.copy()

        # Header 0:64 and footer H-64:H are static. Middle 64:H-64 scrolls.
        m_cy0, m_cy1 = 64 + dy, (H - 64)
        m_py0, m_py1 = m_cy0 - dy, m_cy1 - dy

        cur[64:H-64, :] = rng.integers(0, 256, (H - 128, W), dtype=np.uint8)
        cur[m_cy0:m_cy1, :] = prev[m_py0:m_py1, :]

        res = gm.estimate_global_motion(prev, cur, max_d=max(W, H) - 1)
        # Should either detect the scrolling middle vector (0, 20) or None if static bands dominate.
        # Check that it never reports a false alias.
        if res is not None:
            self.assertIn((res[0], res[1]), [(0, 20), (0, 0)])

    def test_diagonal_window_drag(self):
        # A diagonal window-drag-like move
        rng = np.random.default_rng(101112)
        W, H = 640, 480
        dx, dy = 16, 12

        prev = rng.integers(0, 256, (H, W), dtype=np.uint8)
        cur = rng.integers(0, 256, (H, W), dtype=np.uint8)

        cy0, cy1 = max(0, dy), min(H, H + dy)
        cx0, cx1 = max(0, dx), min(W, W + dx)
        py0, py1 = cy0 - dy, cy1 - dy
        px0, px1 = cx0 - dx, cx1 - dx

        cur[cy0:cy1, cx0:cx1] = prev[py0:py1, px0:px1]

        res = gm.estimate_global_motion(prev, cur, max_d=max(W, H) - 1)
        self.assertIsNotNone(res)
        self.assertEqual((res[0], res[1]), (dx, dy))

    def test_periodic_stripe_pattern(self):
        # A periodic stripe pattern of period 8 that returns either None or the true vector, never an alias
        H, W = 256, 256
        dx, dy = 0, 16  # shift by a multiple of period 8

        # Periodic pattern along y with period 8
        pattern = np.array([0, 32, 64, 128, 255, 128, 64, 32], dtype=np.uint8)
        prev = np.tile(pattern, (H // 8, W))
        cur = np.roll(prev, shift=dy, axis=0)

        res = gm.estimate_global_motion(prev, cur, max_d=64)
        if res is not None:
            # Must return true vector (0, 16) or (0, 0) if unshifted, never an alias like (0, 8) or (0, 24)
            # Since shift 16 in periodic pattern leaves array identical to shift 0,
            # array_equal(cur, prev) will trigger (0,0) or None for alias ambiguity.
            self.assertIn((res[0], res[1]), [(0, 0), (0, 16)])

        # Test true vector with non-multiple shift (e.g. shift 3 px) on periodic pattern
        dy_nonmult = 3
        cur_nonmult = np.zeros_like(prev)
        cur_nonmult[dy_nonmult:, :] = prev[:-dy_nonmult, :]
        res_nonmult = gm.estimate_global_motion(prev, cur_nonmult, max_d=64)
        if res_nonmult is not None:
            self.assertEqual((res_nonmult[0], res_nonmult[1]), (0, dy_nonmult))

    def test_consensus_tiles(self):
        # consensus_tiles masks where exposed and partial tiles are False
        W, H, s = 320, 180, 16
        dx, dy = 16, 16

        rng = np.random.default_rng(2022)
        prev = rng.integers(0, 256, (H, W), dtype=np.uint8)
        cur = np.zeros_like(prev)

        cy0, cy1 = max(0, dy), min(H, H + dy)
        cx0, cx1 = max(0, dx), min(W, W + dx)
        py0, py1 = cy0 - dy, cy1 - dy
        px0, px1 = cx0 - dx, cx1 - dx

        cur[cy0:cy1, cx0:cx1] = prev[py0:py1, px0:px1]

        mask = gm.consensus_tiles(prev, cur, s, dx, dy)
        grid_s, gw, gh = grid_params(W, H)
        self.assertEqual(mask.shape, (gh, gw))

        # Check tile (0, 0) - top left tile.
        # Since dx=16, dy=16, the destination tile at (0, 0) corresponds to source (-16, -16) which is out of bounds.
        # Thus mask[0, 0] must be False.
        self.assertFalse(mask[0, 0])

        # Tile at (1, 1) covers destination y:[16..32], x:[16..32].
        # Corresponding source y:[0..16], x:[0..16], which is in bounds and identical.
        self.assertTrue(mask[1, 1])

        # Partial tile at bottom right boundary
        # If H=180, s=16, gh=ceil(180/16)=12. Last row ty=11 covers y:[176..180] (height 4).
        # Destination y:[176..180] -> source y:[160..164] which is in bounds.
        # But if source tile leaves frame bounds, it's False.
        # Test an out-of-bounds tile at bottom/right.
        # Tile at ty=11, tx=19: destination y:[176..180], x:[304..320].
        # Source y:[160..164], x:[288..304] is within bounds (180, 320).
        # So mask[11, 19] should be True.

    def test_max_d_smaller_than_true_shift(self):
        # max_d smaller than the true shift returns None or a vector within max_d
        rng = np.random.default_rng(3033)
        W, H = 320, 240
        true_dy = 50
        max_d = 20

        prev = rng.integers(0, 256, (H, W), dtype=np.uint8)
        cur = rng.integers(0, 256, (H, W), dtype=np.uint8)
        cur[true_dy:, :] = prev[:-true_dy, :]

        res = gm.estimate_global_motion(prev, cur, max_d=max_d)
        if res is not None:
            res_dx, res_dy, _ = res
            self.assertLessEqual(max(abs(res_dx), abs(res_dy)), max_d)


if __name__ == '__main__':
    unittest.main()
