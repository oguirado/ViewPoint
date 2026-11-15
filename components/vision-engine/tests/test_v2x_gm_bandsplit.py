import unittest
import numpy as np

from eval.v2x.gm_bandsplit import (
    estimate_global_motion,
    consensus_tiles,
    reset_ops,
    OPS,
)
import eval.v2x.gm_bandsplit as gm_bandsplit_mod


class TestGMBandsplit(unittest.TestCase):

    def setUp(self):
        reset_ops()

    def test_header_footer_static_scrolling_middle(self):
        # Header and footer static with scrolling middle at dy = 3 and dy = -11
        rng = np.random.default_rng(1001)
        H, W = 200, 300
        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)

        top_h = 30
        bot_h = 40

        for dy in [3, -11]:
            cur = prev.copy()
            # Scroll middle rows [30, 160]
            m_start, m_end = top_h, H - bot_h
            M = m_end - m_start
            mid_prev = prev[m_start:m_end, :]

            # Create scrolled middle for cur
            cur_mid = np.zeros_like(mid_prev)
            if dy > 0:
                cur_mid[dy:, :] = mid_prev[:-dy, :]
                cur_mid[:dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)
            else:
                abs_dy = abs(dy)
                cur_mid[:-abs_dy, :] = mid_prev[abs_dy:, :]
                cur_mid[-abs_dy:, :] = rng.integers(0, 256, size=(abs_dy, W), dtype=np.uint8)

            cur[m_start:m_end, :] = cur_mid

            res = estimate_global_motion(prev, cur, max_d=50)
            self.assertIsNotNone(res)
            dx_est, dy_est, score = res
            self.assertEqual((dx_est, dy_est), (0, dy))
            # Every row in middle that moved in bounds matched
            expected_matches = M - abs(dy)
            self.assertEqual(score, (expected_matches) * 1_000_000 // M)

            # Consensus tiles check
            s = 16
            mask = consensus_tiles(prev, cur, s, dx_est, dy_est)
            self.assertEqual(mask.dtype, bool)
            # Tiles overlapping top_h or bot_h should be False because they are outside middle band
            gh = (H + s - 1) // s
            gw = (W + s - 1) // s
            for ty in range(gh):
                r0 = ty * s
                r1 = (ty + 1) * s
                if r0 < top_h or r1 > (H - bot_h):
                    for tx in range(gw):
                        self.assertFalse(mask[ty, tx])

    def test_full_frame_scroll(self):
        # Full-frame scroll (empty static bands, middle is full frame)
        rng = np.random.default_rng(1002)
        H, W = 100, 150
        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)

        dy = 15
        cur = np.zeros_like(prev)
        cur[dy:, :] = prev[:-dy, :]
        cur[:dy, :] = rng.integers(0, 256, size=(dy, W), dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=50)
        self.assertIsNotNone(res)
        dx_est, dy_est, score = res
        self.assertEqual((dx_est, dy_est), (0, dy))
        self.assertEqual(score, (H - dy) * 1_000_000 // H)

    def test_static_pair(self):
        # Entire frame is static -> top band = H, middle band = 0 -> returns None
        rng = np.random.default_rng(1003)
        H, W = 100, 100
        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
        cur = prev.copy()

        res = estimate_global_motion(prev, cur, max_d=20)
        self.assertIsNone(res)

    def test_noise_change_returning_none(self):
        # Static top/bottom band, but middle band filled with pure noise (< 50% row matches)
        rng = np.random.default_rng(1004)
        H, W = 200, 200
        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
        cur = prev.copy()

        # Replace middle band [50, 150] with completely new random noise
        cur[50:150, :] = rng.integers(0, 256, size=(100, W), dtype=np.uint8)

        res = estimate_global_motion(prev, cur, max_d=30)
        self.assertIsNone(res)

    def test_horizontal_only_pan(self):
        # Horizontal-only pan (dy = 0 in middle band with static header/footer)
        rng = np.random.default_rng(1005)
        H, W = 200, 300
        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)

        top_h, bot_h = 40, 40
        m_start, m_end = top_h, H - bot_h

        cur = prev.copy()
        # In middle band, row m_start and row m_end-1 differ from prev to define middle band boundaries,
        # while inner rows remain identical (dy = 0) with 118/120 >= 50% matching rows.
        cur[m_start, :] = (prev[m_start, :] + np.uint8(1))
        cur[m_end - 1, :] = (prev[m_end - 1, :] + np.uint8(1))

        res = estimate_global_motion(prev, cur, max_d=50)
        self.assertIsNotNone(res)
        dx_est, dy_est, score = res
        self.assertEqual((dx_est, dy_est), (0, 0))
        self.assertEqual(score, (m_end - m_start - 2) * 1_000_000 // (m_end - m_start))

    def test_1x1_and_2xW_frames(self):
        # 1x1 frame
        prev_1x1 = np.array([[100]], dtype=np.uint8)
        cur_1x1 = np.array([[100]], dtype=np.uint8)
        self.assertIsNone(estimate_global_motion(prev_1x1, cur_1x1, max_d=5))

        cur_1x1_diff = np.array([[200]], dtype=np.uint8)
        self.assertIsNone(estimate_global_motion(prev_1x1, cur_1x1_diff, max_d=5))

        # 2 x W frame
        rng = np.random.default_rng(1006)
        W = 50
        prev_2xW = rng.integers(0, 256, size=(2, W), dtype=np.uint8)
        cur_2xW = np.zeros_like(prev_2xW)
        cur_2xW[1, :] = prev_2xW[0, :]  # dy = 1
        cur_2xW[0, :] = rng.integers(0, 256, size=(W,), dtype=np.uint8)

        res = estimate_global_motion(prev_2xW, cur_2xW, max_d=5)
        self.assertIsNotNone(res)
        dx_est, dy_est, score = res
        self.assertEqual((dx_est, dy_est), (0, 1))

    def test_ops_increment_and_reset(self):
        reset_ops()
        self.assertEqual(gm_bandsplit_mod.OPS, 0)

        rng = np.random.default_rng(1007)
        prev = rng.integers(0, 256, size=(40, 40), dtype=np.uint8)
        cur = prev.copy()
        cur[10:30, :] = rng.integers(0, 256, size=(20, 40), dtype=np.uint8)

        estimate_global_motion(prev, cur, max_d=10)
        self.assertGreater(gm_bandsplit_mod.OPS, 0)

        reset_ops()
        self.assertEqual(gm_bandsplit_mod.OPS, 0)


if __name__ == "__main__":
    unittest.main()
