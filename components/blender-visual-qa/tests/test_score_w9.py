"""W9 S2: drift_duty_ppm, frozen_hit by coverage stratum, flicker_fp_tiles (IMPL-GAP-8)."""
import unittest

import numpy as np

from blender_visual_qa.truth import score
from blender_visual_qa.truth.score import canon


def temporal(F=6):
    tl = np.zeros((F, 4), np.uint8)
    cls = np.zeros((F, 4), np.uint8)
    mv = np.zeros((F, 4, 2), np.int8)
    tl[:, 0] = 11                     # DRIFT tile: detected from frame 2 on, missed once afterwards
    cls[2:, 0] = score.C_DRIFT
    cls[4, 0] = score.C_UNCHANGED
    tl[:, 1] = 10                     # FROZEN, cov 400k
    tl[:, 2] = 10                     # FROZEN, cov 20k
    cls[:, 1] = score.C_FROZEN
    tl[:, 3] = 0
    cov = np.zeros((F, 4), np.int32)
    cov[:, 1], cov[:, 2] = 400000, 20000
    return tl, cls, mv, cov


def srow(tl, cls, mv, bench="B4", **kw):
    return score.score_frames(tl, cls, mv, mv.copy(), bench, 0, 4, "g", 32, **kw)


class TemporalFieldTests(unittest.TestCase):
    def test_drift_duty_after_first_detection(self):
        tl, cls, mv, cov = temporal()
        r = srow(tl, cls, mv, cov=cov)
        self.assertEqual((r["drift_duty_n"], r["drift_duty_hit"]), (4, 3))   # frames 2..5, one miss
        self.assertEqual(r["drift_duty_ppm"], 750000)
        self.assertEqual(score.summarize([r])["drift_duty_ppm"], 750000)

    def test_drift_never_detected_is_zero_population(self):
        tl, cls, mv, cov = temporal()
        cls[:, 0] = 0
        r = srow(tl, cls, mv, cov=cov)
        self.assertEqual((r["drift_duty_n"], r["drift_duty_hit"], r["drift_duty_ppm"]), (0, 0, 0))

    def test_frozen_hit_by_cov_stratum(self):
        tl, cls, mv, cov = temporal()
        r = srow(tl, cls, mv, cov=cov)
        self.assertEqual(r["frozen_hit"]["ge300k"], {"n": 6, "hit": 6})
        self.assertEqual(r["frozen_hit"]["lt50k"], {"n": 6, "hit": 0})
        self.assertEqual(r["frozen_hit"]["50k_150k"], {"n": 0, "hit": 0})
        s = score.summarize([r])["frozen_hit"]
        self.assertEqual((s["ge300k"]["ppm"], s["lt50k"]["ppm"], s["50k_150k"]["ppm"]), (1000000, 0, None))

    def test_flicker_fp_only_counted_for_b4n(self):
        tl, cls, mv, cov = temporal()
        cls[:, 3] = score.C_FLICKER
        rn = srow(tl, cls, mv, bench="B4n", cov=cov)
        self.assertEqual(rn["flicker_fp_tiles"], 6)
        self.assertEqual(score.summarize([rn])["flicker_fp_tiles"], 6)
        self.assertIsNone(score.summarize([srow(tl, cls, mv, bench="B4", cov=cov)])["flicker_fp_tiles"])

    def test_row_stays_integer_canonical(self):
        tl, cls, mv, cov = temporal()
        canon.dumps(srow(tl, cls, mv, cov=cov, resid=np.zeros(tl.shape, np.int32)))

    def test_cov_shape_checked(self):
        tl, cls, mv, cov = temporal()
        with self.assertRaises(score.VqaError):
            srow(tl, cls, mv, cov=cov[:2])

    def test_fp_resid_by_tl(self):
        tl, cls, mv, cov = temporal()
        tl[:, 3] = 7
        res = np.zeros(tl.shape, np.int32)
        res[:, 3] = 77
        r = srow(tl, cls, mv, resid=res)
        self.assertEqual(r["fp_resid_by_tl"], {"7": 77})


if __name__ == "__main__":
    unittest.main()
