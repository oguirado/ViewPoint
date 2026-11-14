"""A4 integer-contract tests for gm_bands_hv chrome trimming, cutoff work accounting, and rejection gates."""

import unittest
import numpy as np
from eval.v2x import gm_bands_hv as gm


class TestJQ1A4BandsTrimCutoffAndGates(unittest.TestCase):

    def setUp(self):
        gm.reset_ops()

    def test_left_right_chrome_trim(self):
        """Test static left/right chrome trimming with scrolling middle columns."""
        H, W = 480, 640
        rng = np.random.default_rng(407)
        prev = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
        cur = prev.copy()

        # Keep columns 0..47 and 592..639 identical in cur.
        # Shift middle columns 48..591 down by 12 and fill top 12 rows with fresh noise.
        cur[12:, 48:592] = prev[:-12, 48:592]
        cur[:12, 48:592] = rng.integers(0, 256, size=(12, 544), dtype=np.uint8)

        res = gm.estimate_global_motion(prev, cur, max_d=64)
        self.assertEqual(res, (0, 12, 13))

    def test_joint_sad_cutoff_and_ops_accounting(self):
        """Test _joint_sad cutoff behavior, OPS context accounting, and strictly greater boundary condition."""
        cur = np.full((128, 64), 200, dtype=np.uint8)
        prev = np.zeros((128, 64), dtype=np.uint8)

        # 1. cutoff=100 stops after first 32-row block (2048 elements * 200 = 409600 > 100).
        gm.reset_ops()
        sad_cutoff = gm._joint_sad(cur, prev, cutoff=100)
        self.assertEqual(sad_cutoff, 409600)
        self.assertEqual(gm.OPS, 4096)

        # 2. cutoff=None evaluates all 4 blocks (128 rows = 4 * 32 rows).
        gm.reset_ops()
        sad_full = gm._joint_sad(cur, prev, cutoff=None)
        self.assertEqual(sad_full, 1638400)
        self.assertEqual(gm.OPS, 16384)

        # 3. cutoff=409600 (equality with first block SAD): total > cutoff is strictly greater,
        # so block 1 (total=409600) does not break, but block 2 (total=819200) breaks.
        gm.reset_ops()
        sad_boundary = gm._joint_sad(cur, prev, cutoff=409600)
        self.assertEqual(sad_boundary, 819200)
        self.assertEqual(gm.OPS, 8192)

    def test_alias_margin_perturbed_near_tie_rejection(self):
        """Test that a deterministic single-pixel perturbation on a periodic image triggers alias margin rejection."""
        H, W = 64, 64
        x = np.arange(W)
        stripes = np.where((x // 4) % 2 == 0, 200, 50).astype(np.uint8)
        prev = np.tile(stripes, (H, 1))

        # Horizontal roll by eight on period-8 image
        cur = np.roll(prev, shift=8, axis=1)

        # Unperturbed case returns (0, 0, 0) due to static chrome / exact tie
        res_unperturbed = gm.estimate_global_motion(prev, cur, max_d=16)
        self.assertEqual(res_unperturbed, (0, 0, 0))

        # Single-pixel perturbation breaking whole-frame identity and creating near-tie
        cur_perturbed = cur.copy()
        cur_perturbed[32, 30] = 200

        res_perturbed = gm.estimate_global_motion(prev, cur_perturbed, max_d=16)
        self.assertIsNone(
            res_perturbed,
            "Perturbed near-tie must be rejected by alias margin gate (None)",
        )

    def test_changed_tile_support_rejection_gate(self):
        """Test changed-tile support gate and verify actual block tile areas."""
        H, W = 160, 160
        rng = np.random.default_rng(999)

        prev = np.full((H, W), 40, dtype=np.uint8)
        cur = prev.copy()

        # 64 by 64 textured region translated 16 columns
        texture = rng.integers(0, 256, (64, 64), dtype=np.uint8)
        prev[16:80, 16:80] = texture
        cur[16:80, 32:96] = texture

        # Disjoint 96 by 64 region replaced by independent noise
        prev[64:160, 96:160] = rng.integers(0, 256, (96, 64), dtype=np.uint8)
        cur[64:160, 96:160] = rng.integers(0, 256, (96, 64), dtype=np.uint8)

        # Confirm exact changed and matched tile counting in 16x16 blocks
        best_dx, best_dy = 16, 0
        cy0, cy1 = max(0, best_dy), min(H, H + best_dy)
        cx0, cx1 = max(0, best_dx), min(W, W + best_dx)
        matched_area = changed_area = 0
        changed_tiles = matched_tiles = 0

        for y in range(cy0, cy1, 16):
            for x in range(cx0, cx1, 16):
                ye, xe = min(y + 16, cy1), min(x + 16, cx1)
                c = cur[y:ye, x:xe]
                p = prev[y:ye, x:xe]
                if np.array_equal(c, p):
                    continue
                changed_area += c.size
                changed_tiles += 1
                source = prev[y - best_dy:ye - best_dy, x - best_dx:xe - best_dx]
                if np.array_equal(c, source):
                    matched_area += c.size
                    matched_tiles += 1

        self.assertEqual(changed_tiles, 44)
        self.assertEqual(matched_tiles, 20)
        self.assertEqual(changed_area, 11264)
        self.assertEqual(matched_area, 5120)

        # Support ratio is 5120 / 11264 = 454.54 per 1000, which is below 600 per 1000
        self.assertTrue(matched_area * 1000 < changed_area * 600)

        res = gm.estimate_global_motion(prev, cur, max_d=32)
        self.assertIsNone(
            res,
            "Insufficient changed-tile support must return None",
        )


if __name__ == "__main__":
    unittest.main()
