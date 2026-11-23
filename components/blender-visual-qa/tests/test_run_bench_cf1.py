import unittest

import numpy as np

from blender_visual_qa.truth import calibrate, run_bench as rb
from visioncore import params as vparams
from visioncore.change import TILE_DT


class FakeRun:
    class grid:
        N = 3

    def __init__(self, y0, y1):
        a, b = (np.zeros(3, TILE_DT) for _ in range(2))
        a["y_q4"], b["y_q4"] = y0, y1
        self.tiles = [a, b]


class RawResidTests(unittest.TestCase):
    def test_raw_q4_not_display_field(self):
        r = rb.raw_resid(FakeRun(0, 103), vparams.effective(None))
        self.assertEqual(r.shape, (2, 3))
        self.assertEqual(int(r[0].max()), 0)
        self.assertEqual(int(r[1].max()), 103)  # display field would be 103 >> 2 = 25
        self.assertEqual(calibrate.t0_still(r[1:].reshape(-1).tolist()), 129)

    def test_renderer_id_carries_box(self):
        import inspect
        self.assertIn("-box", inspect.getsource(rb.main))


if __name__ == "__main__":
    unittest.main()
