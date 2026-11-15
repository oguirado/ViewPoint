import unittest

import numpy as np

from blender_visual_qa.truth import calibrate
from tests.test_calibrate import good_rows, calib


class RefusalTests(unittest.TestCase):
    def test_saturated_field_refused(self):
        p, rep = calib(good_rows(fp_resid=1049), [4], "blender", "r", "g", "ab")
        self.assertIsNone(p)
        self.assertEqual(rep["refusal"], "fp_pop_contaminated")

    def test_fp_far_above_still_refused(self):
        p, rep = calib(good_rows(fp_resid=300), [4], "blender", "r", "g", "ab")  # 301 > 4*32
        self.assertIsNone(p)
        self.assertEqual(rep["refusal"], "fp_pop_contaminated")

    def test_clean_rows_accepted(self):
        p, rep = calib(good_rows(fp_resid=10), [4], "blender", "r", "g", "ab")
        self.assertIsNotNone(p)
        self.assertIsNone(rep["refusal"])

    def test_mv_proposal_names_param_only_with_wrong_mv(self):
        rows = good_rows()
        for r in rows:
            r["mv_exact"] = 0
            r["mv_err_hist"] = [r["mv_n"]] + [0] * 16
        _, rep = calib(rows, [4], "blender", "r", "g", "ab")
        self.assertTrue(rep["proposals"])
        self.assertTrue(all(p["param"] is None for p in rep["proposals"]))
        for r in rows:
            r["mv_err_hist"] = [0, r["mv_n"]] + [0] * 15
        _, rep = calib(rows, [4], "blender", "r", "g", "ab")
        self.assertEqual([p["param"] for p in rep["proposals"]], ["MV_SAD_RATIO_DEN"])


if __name__ == "__main__":
    unittest.main()
