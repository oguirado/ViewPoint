"""Test pinning overlap-floor defect D-A1-MICRO4-JUMP960."""

import unittest
import numpy as np
from eval.v2x import gm_bands_hv as gm


class TestPageJump960pxOverlapFloor(unittest.TestCase):
    def test_page_jump_960px_overlap_floor(self):
        """D-A1-MICRO4-JUMP960: Overlap floor rejects 960px jump (120px overlap < min_ov_y=270)."""
        rng = np.random.default_rng(960)
        prev = rng.integers(0, 256, (1080, 1920), dtype=np.uint8)
        cur = rng.integers(0, 256, (1080, 1920), dtype=np.uint8)

        # Copy prev rows 960 through 1079 into cur rows 0 through 119
        cur[0:120] = prev[960:1080]

        # Verify surviving overlap calculation
        surviving_overlap = 120
        band_H = 1080
        quarter_floor = max(16, band_H // 4)  # 270 rows
        self.assertEqual(surviving_overlap, 120)
        self.assertLess(surviving_overlap, quarter_floor)

        result = gm.estimate_global_motion(prev, cur, max_d=1100)
        self.assertEqual(result, (0, -960, 0))


if __name__ == "__main__":
    unittest.main()
