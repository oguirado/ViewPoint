import unittest

import numpy as np

from blender_visual_qa.truth import score
from tests.test_score import synth, row


class FpPopTests(unittest.TestCase):
    def test_subpix_not_fp_population(self):
        self.assertEqual(score.FP_POP, (0, 7))
        tl, cls, mv = synth()
        tl[:, 9] = 2
        res = np.zeros(tl.shape, np.int32)
        res[tl == 2] = 1000  # real sub-pixel motion: must not drive the FP T0
        self.assertEqual(row(tl, cls, mv, mv, resid=res)["fp_resid_max_q4"], 0)

    def test_shade_weak_still_counts(self):
        tl, cls, mv = synth()
        res = np.zeros(tl.shape, np.int32)
        res[tl == 7] = 20
        self.assertEqual(row(tl, cls, mv, mv, resid=res)["fp_resid_max_q4"], 20)


if __name__ == "__main__":
    unittest.main()
