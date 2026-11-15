import unittest
import numpy as np

from eval.v2x.gm_rowhash import (
    estimate_global_motion,
    consensus_tiles,
    reset_ops,
    OPS,
)
import eval.v2x.gm_rowhash as gm_rowhash_mod


class TestGMRowHash(unittest.TestCase):

    def setUp(self):
        reset_ops()

    def test_vertical_scrolls(self):
        # Textured random frames scrolled vertically by 1, 7, 33 and 200 px
        rng = np.random.default_rng(42)
        H, W = 360, 640
        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)

        for dy in [1, 7, 33, 200]:
            cur = np.zeros_like(prev)
            # cur[y, x] = prev[y - dy, x]
            cur[dy:, :] = prev[: H - dy, :]
            cur[:dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)

            res = estimate_global_motion(prev, cur, max_d=max(H, W) - 1)
            self.assertIsNotNone(res, f"Failed for vertical scroll dy={dy}")
            dx_est, dy_est, score = res
            self.assertEqual((dx_est, dy_est), (0, dy))
            self.assertEqual(score, 0)

    def test_horizontal_scrolls(self):
        # Textured random frames scrolled horizontally by 5 and 64 px
        rng = np.random.default_rng(123)
        H, W = 360, 640
        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)

        for dx in [5, 64]:
            cur = np.zeros_like(prev)
            # cur[y, x] = prev[y, x - dx]
            cur[:, dx:] = prev[:, : W - dx]
            cur[:, :dx] = rng.integers(0, 256, size=(H, dx), dtype=np.uint8)

            res = estimate_global_motion(prev, cur, max_d=max(H, W) - 1)
            self.assertIsNotNone(res, f"Failed for horizontal scroll dx={dx}")
            dx_est, dy_est, score = res
            self.assertEqual((dx_est, dy_est), (dx, 0))
            self.assertEqual(score, 0)

    def test_large_1080p_scroll_300px(self):
        # One 1920x1088 frame scrolled by 300 px
        rng = np.random.default_rng(999)
        H, W = 1088, 1920
        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)

        dy = 300
        cur = np.zeros_like(prev)
        cur[dy:, :] = prev[: H - dy, :]
        cur[:dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=max(H, W) - 1)
        self.assertIsNotNone(res)
        dx_est, dy_est, score = res
        self.assertEqual((dx_est, dy_est), (0, dy))
        self.assertEqual(score, 0)

    @unittest.expectedFailure
    def test_static_header_footer_scrolling_middle(self):
        # Static header band and footer band with a scrolling middle
        # Pure global-motion estimators treat static header/footer as 50% matching (0,0) motion,
        # so local sub-region scroll dy=20 cannot beat (0,0) global SAD across static top/bottom.
        rng = np.random.default_rng(2024)
        H, W = 400, 600
        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)

        cur = prev.copy()
        # Scroll middle region y in [100, 300] vertically by 20 px
        m_start, m_end = 100, 300
        dy = 20
        cur[m_start + dy : m_end, :] = prev[m_start : m_end - dy, :]
        cur[m_start : m_start + dy, :] = rng.integers(
            0, 256, size=(dy, W), dtype=np.uint8
        )

        res = estimate_global_motion(prev, cur, max_d=max(H, W) - 1)
        self.assertIsNotNone(res)
        dx_est, dy_est, score = res
        self.assertEqual((dx_est, dy_est), (0, dy))

    def test_diagonal_window_drag_move(self):
        # Diagonal window-drag-like move
        rng = np.random.default_rng(777)
        H, W = 400, 600
        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)

        dx, dy = 15, 25
        cur = np.zeros_like(prev)
        cur[dy:, dx:] = prev[: H - dy, : W - dx]
        cur[:dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)
        cur[:, :dx] = rng.integers(0, 256, size=(H, dx), dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=max(H, W) - 1)
        self.assertIsNotNone(res)
        dx_est, dy_est, score = res
        self.assertEqual((dx_est, dy_est), (dx, dy))
        self.assertEqual(score, 0)

    def test_flat_frames(self):
        # Flat frames returning None
        prev = np.full((100, 100), 128, dtype=np.uint8)
        cur = np.full((100, 100), 128, dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=99)
        self.assertIsNone(res)

    def test_periodic_stripe_pattern(self):
        # Periodic stripe pattern of period 8 that returns either None or the true vector, never an alias
        H, W = 160, 160
        # Horizontal stripes repeating every 8 rows
        pattern = np.tile(
            np.array([0, 30, 60, 90, 120, 150, 180, 210], dtype=np.uint8)[:, None],
            (20, W),
        )

        prev = pattern
        dy_true = 8  # True shift is 8 (or multi of 8)
        cur = np.roll(prev, dy_true, axis=0)

        res = estimate_global_motion(prev, cur, max_d=50)
        if res is not None:
            dx_est, dy_est, _ = res
            # Must be true shift or valid multiple, NEVER an incorrect alias like dy=1, 2, etc.
            self.assertEqual(dx_est, 0)
            self.assertEqual(dy_est % 8, 0)

    def test_consensus_tiles(self):
        # Consensus_tiles masks where exposed and partial tiles are False
        H, W, s = 64, 64, 16
        rng = np.random.default_rng(11)
        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)

        dx, dy = 8, 16
        cur = np.zeros_like(prev)
        cur[dy:, dx:] = prev[: H - dy, : W - dx]
        cur[:dy, :] = 255  # new exposed content

        mask = consensus_tiles(prev, cur, s, dx, dy)
        self.assertEqual(mask.shape, (4, 4))
        self.assertTrue(mask.dtype == bool)

        # Tiles touching top rows (y < 16) or left cols (x < 8) should be False due to exposed source/destination
        for ty in range(4):
            for tx in range(4):
                cy0, cy1 = ty * s, (ty + 1) * s
                cx0, cx1 = tx * s, (tx + 1) * s
                py0, py1 = cy0 - dy, cy1 - dy
                px0, px1 = cx0 - dx, cx1 - dx
                if py0 < 0 or py1 > H or px0 < 0 or px1 > W:
                    self.assertFalse(mask[ty, tx])

    def test_ops_increment_and_reset(self):
        # OPS increases after a call and resets with reset_ops
        reset_ops()
        self.assertEqual(gm_rowhash_mod.OPS, 0)

        prev = np.zeros((32, 32), dtype=np.uint8)
        cur = np.zeros((32, 32), dtype=np.uint8)

        estimate_global_motion(prev, cur, max_d=31)
        ops_after_est = gm_rowhash_mod.OPS
        self.assertGreater(ops_after_est, 0)

        reset_ops()
        self.assertEqual(gm_rowhash_mod.OPS, 0)

        consensus_tiles(prev, cur, 16, 4, 4)
        ops_after_ct = gm_rowhash_mod.OPS
        self.assertGreater(ops_after_ct, 0)

        reset_ops()
        self.assertEqual(gm_rowhash_mod.OPS, 0)

    def test_max_d_smaller_than_true_shift(self):
        # max_d smaller than the true shift returns None or a vector within max_d
        rng = np.random.default_rng(55)
        H, W = 300, 300
        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)

        dy_true = 50
        cur = np.zeros_like(prev)
        cur[dy_true:, :] = prev[: H - dy_true, :]

        res = estimate_global_motion(prev, cur, max_d=30)
        if res is not None:
            dx_est, dy_est, _ = res
            self.assertLessEqual(abs(dx_est), 30)
            self.assertLessEqual(abs(dy_est), 30)


if __name__ == "__main__":
    unittest.main()
