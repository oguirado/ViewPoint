"""Test for borderline competing motions in global motion estimation."""
import unittest
import numpy as np
from eval.v2x import gm_bands_hv as gm


class TestBorderlineCompetingMotions(unittest.TestCase):
    def test_borderline_competing_motions(self):
        # D-A1-F2-BORDERLINE: Pin D-A1-F2-BORDERLINE rather than equal-block case.
        # Secondary orthogonal motion must veto larger mode even though nominal object areas give >600 ppt support.
        prev = np.full((480, 640), 40, dtype=np.uint8)
        cur = prev.copy()

        rng = np.random.default_rng(912)
        block1 = rng.integers(0, 256, (100, 150), dtype=np.uint8)
        block2 = rng.integers(0, 256, (100, 80), dtype=np.uint8)

        prev[64:164, 64:214] = block1
        cur[64:164, 76:226] = block1

        prev[280:380, 380:460] = block2
        cur[270:370, 380:460] = block2

        # Require estimate_global_motion to return None when secondary orthogonal motion vetoes
        self.assertIsNone(gm.estimate_global_motion(prev, cur, 100))


if __name__ == '__main__':
    unittest.main()
