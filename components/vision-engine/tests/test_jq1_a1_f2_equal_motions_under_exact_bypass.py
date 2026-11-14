"""tests/test_jq1_a1_f2_equal_motions_under_exact_bypass.py - Safety test suite for exact-match bypass under competing motions."""

import unittest
import numpy as np
from eval.v2x import adv_gm
from eval.v2x import gm_bands_hv


class TestEqualMotionsUnderExactBypass(unittest.TestCase):
    """Verify that independent competing motions are refused by the global motion estimator."""

    def test_two_motions_cases_refused_at_dual_radii(self):
        """Require None at both max_d=100 and max_d=199 for all two_motions cases."""
        all_cases = adv_gm.cases(seed=0x5EED, include_1080p=False)
        two_motions_cases = [c for c in all_cases if c["kind"] == "two_motions"]
        self.assertGreater(len(two_motions_cases), 0, "No two_motions cases found in adv_gm.cases")

        for c in two_motions_cases:
            cid = c["case_id"]
            f0, f1 = c["frames"][0], c["frames"][1]
            self.assertEqual(f0.shape, (160, 200), f"Case {cid} expected shape (160, 200)")
            self.assertEqual(f1.shape, (160, 200), f"Case {cid} expected shape (160, 200)")

            for max_d in (100, 199):
                with self.subTest(case_id=cid, max_d=max_d):
                    res = gm_bands_hv.estimate_global_motion(f0, f1, max_d=max_d)
                    self.assertIsNone(
                        res,
                        f"Case {cid} with max_d={max_d} returned {res} instead of None"
                    )

    def test_two_motions_0_positive_residual_calculation(self):
        """Compute joint pixel SAD within valid overlap for (12, 0) and (0, -10) on two_motions_0, requiring SAD > 0."""
        all_cases = adv_gm.cases(seed=0x5EED, include_1080p=False)
        tm0 = [c for c in all_cases if c["case_id"] == "two_motions_0"][0]
        f0, f1 = tm0["frames"][0], tm0["frames"][1]
        H, W = f0.shape
        self.assertEqual((H, W), (160, 200))

        candidates = [(12, 0), (0, -10)]
        for dx, dy in candidates:
            with self.subTest(dx=dx, dy=dy):
                cy0, cy1 = max(0, dy), min(H, H + dy)
                cx0, cx1 = max(0, dx), min(W, W + dx)
                py0, py1 = cy0 - dy, cy1 - dy
                px0, px1 = cx0 - dx, cx1 - dx

                cur_sub = f1[cy0:cy1, cx0:cx1].astype(np.int64)
                prev_sub = f0[py0:py1, px0:px1].astype(np.int64)

                diff = cur_sub - prev_sub
                sad = int(np.abs(diff).sum())
                self.assertGreater(
                    sad, 0,
                    f"Expected strictly positive residual SAD for vector ({dx}, {dy}), got {sad}"
                )


if __name__ == "__main__":
    unittest.main()
