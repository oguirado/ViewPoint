"""G-E2 fix (DIAG_X4): aperture rule + FLICKER->MOVED for motion-explainable tiles when global_mv is published.
Calibration seeds 30..39 only (never the 0..29 test split)."""
import unittest

import numpy as np

from eval import x4_fp
from tests.px_mutants import driver as D

CAL_SEEDS = range(30, 40)
W, H, N, DX = 256, 160, 40, 4
FROZEN, DRIFT, FLICKER = 5, 6, 4


def _pan_frames(patch_mode):
    """Random texture panning DX px/frame. patch_mode 'uniform': row-constant HUD band (ty=1) that a horizontal pan
    cannot change (after an initial passing edge gives pre_act >= 8); 'textured': textured patch that pans for 12 frames then freezes while the scene keeps panning."""
    rng = np.random.RandomState(7)
    base = rng.randint(30, 220, (H, W * 2, 3)).astype(np.uint8)
    rows = rng.randint(30, 220, (H, 1, 3)).astype(np.uint8)
    hold = None
    out = []
    for f in range(N):
        fr = base[:, DX * f:DX * f + W].copy()
        if patch_mode == "uniform":
            band = np.broadcast_to(rows[16:32], (16, W, 3)).copy()
            if f < 12:  # a vertical edge passes through the band while it is alive, then it is row-uniform
                xs = (np.arange(W) + DX * f) % 96 < 24
                band[:, xs] = 255 - band[:, xs]
            fr[16:32] = band
        else:
            if f < 12:
                hold = fr[64:96, 64:128].copy()
            fr[64:96, 64:128] = hold
        out.append(fr)
    return out


class ScrollAperture(unittest.TestCase):
    def test_cal_seeds_scroll_zero_fault_tiles(self):
        for s in CAL_SEEDS:
            with self.subTest(seed=s):
                r = x4_fp.run_trial("scroll", s)
                self.assertEqual(r["split"], "cal")
                self.assertEqual(r["fault_tiles"], 0, r["classes"])

    def test_row_uniform_patch_in_pan_not_frozen(self):
        _, cls, gmv, _ = D.classify_frames(_pan_frames("uniform"))
        self.assertTrue(any(g is not None for g in gmv[1:]))
        c = cls[1:]
        self.assertEqual(int(np.isin(c, (FROZEN, DRIFT)).sum()), 0)

    def test_textured_patch_frozen_during_pan_still_frozen(self):  # t_frozen_pan kept
        _, cls, gmv, _ = D.classify_frames(_pan_frames("textured"))
        self.assertTrue(any(g is not None for g in gmv[1:]))
        self.assertGreater(int((cls[1:] == FROZEN).sum()), 0)


if __name__ == "__main__":
    unittest.main()
