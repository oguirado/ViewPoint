"""tests/test_jq1_a1_changed_tile_600ppt_boundary.py - Boundary tests for changed-tile 600-ppt consensus gate.

Pin the nonzero-SAD 600-ppt changed-tile consensus gate with explicit 800-ppt acceptance and 400-ppt rejection controls.
Note: These test cases straddle the 600-ppt threshold rather than measuring exact equality at 600 ppt.
"""

import unittest
import numpy as np
from eval.v2x.gm_bands_hv import estimate_global_motion


class TestChangedTile600pptBoundary(unittest.TestCase):

    def test_changed_tile_600ppt_boundary(self):
        """Test 800-ppt acceptance and 400-ppt rejection controls straddling 600-ppt threshold."""
        rng = np.random.default_rng(1515)
        prev = rng.integers(0, 256, size=(160, 160), dtype=np.uint8)

        # cur shifted down 16 rows with independently filled exposed top 16 rows
        cur_base = np.empty_like(prev)
        cur_base[16:, :] = prev[:-16, :]
        cur_base[:16, :] = rng.integers(0, 256, size=(16, 160), dtype=np.uint8)

        # Destination overlap has ninety 16x16 tiles (rows 16..160, cols 0..160 -> 9 rows * 10 cols = 90 tiles)
        # Acceptance: flip exactly 1 pixel in each of 18 distinct overlap tiles (row-major order, xor 1)
        cur_acc = cur_base.copy()
        for k in range(18):
            r = k // 10
            c = k % 10
            y = 16 + r * 16
            x = c * 16
            cur_acc[y, x] ^= 1

        # Rejection: flip exactly 1 pixel in each of 54 distinct overlap tiles (row-major order, xor 1)
        cur_rej = cur_base.copy()
        for k in range(54):
            r = k // 10
            c = k % 10
            y = 16 + r * 16
            x = c * 16
            cur_rej[y, x] ^= 1

        # Independently count exact translated tile pixels to prove matched support is respectively 72 of 90 and 36 of 90
        def count_tile_support(cur_frame):
            matched_tiles = 0
            total_tiles = 0
            for y in range(16, 160, 16):
                for x in range(0, 160, 16):
                    total_tiles += 1
                    c_tile = cur_frame[y:y+16, x:x+16]
                    p_tile = prev[y:y+16, x:x+16]
                    # Tile changed in place
                    if np.array_equal(c_tile, p_tile):
                        continue
                    # Tile matches translated source (prev shifted by dy=16)
                    source_tile = prev[y-16:y, x:x+16]
                    if np.array_equal(c_tile, source_tile):
                        matched_tiles += 1
            return matched_tiles, total_tiles

        matched_acc, total_acc = count_tile_support(cur_acc)
        self.assertEqual(total_acc, 90)
        self.assertEqual(matched_acc, 72)  # 72 of 90 matched tiles -> 800 ppt support

        matched_rej, total_rej = count_tile_support(cur_rej)
        self.assertEqual(total_rej, 90)
        self.assertEqual(matched_rej, 36)  # 36 of 90 matched tiles -> 400 ppt support

        # Test estimate_global_motion at max_d=48
        res_acc = estimate_global_motion(prev, cur_acc, max_d=48)
        self.assertIsNotNone(res_acc)
        dx_acc, dy_acc, score_acc = res_acc
        self.assertEqual(dx_acc, 0)
        self.assertEqual(dy_acc, 16)
        self.assertIsInstance(score_acc, (int, np.integer))
        self.assertGreater(score_acc, 0)

        res_rej = estimate_global_motion(prev, cur_rej, max_d=48)
        self.assertIsNone(res_rej)


if __name__ == "__main__":
    unittest.main()
