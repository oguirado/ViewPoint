"""tests/test_jq1_a1_flat_exact_tie_refusal.py - Unit test pinning exact-score tie refusal for non-adjacent vectors."""

import unittest
import numpy as np
from eval.v2x.gm_bands_hv import estimate_global_motion


class TestFlatExactTieRefusal(unittest.TestCase):

    def test_flat_exact_tie_refusal(self):
        """Pin non-adjacent exact-score tie refusal independently of identical-frame short circuit.

        Generates a 16-row by 256-col texture with default_rng(202), repeats vertically to (256, 256),
        and creates cur by a cyclic vertical shift of -3.
        Proves dy=-3 and dy=13 both score zero and differ by at least 4.
        Requires estimate_global_motion to return None.
        """
        rng = np.random.default_rng(202)
        texture_16 = rng.integers(0, 256, size=(16, 256), dtype=np.uint8)
        prev = np.tile(texture_16, (16, 1))  # shape (256, 256)
        cur = np.roll(prev, -3, axis=0)

        # 1. Frames must differ
        self.assertFalse(np.array_equal(prev, cur), "Frames must differ")

        # 2. Calculate integer overlap SAD independently for dy=-3 and dy=13 (dx=0)
        dy_candidates = [-3, 13]
        sads = {}
        for dy in dy_candidates:
            cy_start, cy_end = max(0, dy), min(256, 256 + dy)
            py_start, py_end = cy_start - dy, cy_end - dy
            c_sub = cur[cy_start:cy_end, :]
            p_sub = prev[py_start:py_end, :]
            sad = int(np.abs(c_sub.astype(int) - p_sub.astype(int)).sum())
            sads[dy] = sad

        # Prove dy=-3 and dy=13 both score zero
        self.assertEqual(sads[-3], 0, "dy=-3 must score zero")
        self.assertEqual(sads[13], 0, "dy=13 must score zero")

        # Prove vectors differ by at least 4
        vector_diff = abs(-3 - 13)
        self.assertGreaterEqual(vector_diff, 4, "Vectors must differ by at least 4")

        # 3. Call estimate_global_motion with max_d=64 and require None
        res = estimate_global_motion(prev, cur, max_d=64)
        self.assertIsNone(res, "estimate_global_motion must return None for non-adjacent exact-score tie")


if __name__ == "__main__":
    unittest.main()
