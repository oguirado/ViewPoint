"""FX round 1: score rows carry the temporal evidence calibrate requires (drift FP tiles, flicker onset latency)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from blender_visual_qa.truth import score as S  # noqa: E402


class TemporalRowTests(unittest.TestCase):
    def row(self, tl, cls):
        tl, cls = np.array(tl), np.array(cls)
        mv = np.zeros(tl.shape + (2,), np.int64)
        return S.score_frames(tl, cls, mv, mv, "B4", 100, 4, "g", 32)

    def test_flicker_latency_counts_frames_from_onset(self):
        tl = [[0, 0], [9, 0], [9, 0], [9, 0], [9, 0]]
        cls = [[0, 0], [0, 0], [0, 0], [4, 0], [4, 0]]
        self.assertEqual(self.row(tl, cls)["latency_max"], {"9": 2})

    def test_never_detected_flicker_is_worst_case_and_absent_without_truth(self):
        self.assertEqual(self.row([[9], [9], [9]], [[0], [0], [0]])["latency_max"], {"9": 3})
        self.assertNotIn("latency_max", self.row([[0], [0]], [[0], [0]]))

    def test_drift_fp_tiles(self):
        r = self.row([[0, 11], [0, 11]], [[6, 6], [0, 6]])
        self.assertEqual(r["drift_fp_tiles"], 1)



class ArgonTests(unittest.TestCase):  # v12ar ADV_W9
    def test_temporal_fp_ignores_excluded(self):  # bt.argon.10
        tl = np.array([[1, 2, 8], [1, 2, 8]], np.uint8)  # all EXCLUDED (INVISIBLE/JITTER/SHADE_BAND): never temporal FP
        cls = np.array([[4, 6, 4], [4, 6, 4]], np.uint8)
        mv = np.zeros((2, 3, 2), np.int64)
        r = S.score_frames(tl, cls, mv, mv.copy(), "B4n", 0, 16, "grid:640x360:s16", 32)
        self.assertEqual((r["flicker_fp_tiles"], r["drift_fp_tiles"]), (0, 0))

if __name__ == "__main__":
    unittest.main()
