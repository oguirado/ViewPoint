"""G-E2 fix (DIAG_X4): H_DEAD 2 -> 4 fitted on calibration seeds 30..39 only (never the 0..29 test split).
SPEC_CORE §8 n_iid_noise: +-1 LSB iid noise -> 0 tiles cls >= 2."""
import unittest

from eval import x4_fp
from tests.px_mutants import driver as D
from visioncore import params as P

CAL_SEEDS = range(30, 40)


class IidNoiseCalibration(unittest.TestCase):
    def test_h_dead_registry_value(self):
        self.assertEqual(P.PARAMS["H_DEAD"], 2)  # NR: SIG_DEAD_Q4 24 + H_DEAD 2 (tests/golden/nr_sigdead_fit.json)

    def test_cal_seeds_zero_fp_tiles(self):
        for s in CAL_SEEDS:
            with self.subTest(seed=s):
                r = x4_fp.run_trial("iid_noise", s)
                self.assertEqual(r["split"], "cal")
                self.assertEqual(r["fp_tiles"], 0, r["classes"])

    def test_n_iid_noise_cal_stream_no_dirty(self):
        frames = x4_fp.build_control("iid_noise", 30)
        _, cls, _, _ = D.classify_frames(frames)
        self.assertEqual(int((cls[1:] >= 2).sum()), 0)


if __name__ == "__main__":
    unittest.main()
