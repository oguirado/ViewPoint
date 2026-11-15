"""E1 C3: RPERF-2 (synthetic BAND coverage at every size) and RPERF-3b (still_probe jitter density)."""
import tempfile
import unittest

import numpy as np

from vadapt.synth import synthetic as S
from visioncore.grid import Grid
from visioncore.params import PARAMS

SIZES = ((256, 160), (1280, 720), (1920, 1080), (3840, 2160), (1024, 1024))


def active_ppm(W, H, a=12, b=11):
    fr, _, _ = S.render_scenario("frozen_subregion", 1, W, H, 16)
    g = Grid(W, H)
    d = (g.pad(fr[a]) != g.pad(fr[b])).reshape(g.gh, g.s, g.gw, g.s, 3).any(axis=(1, 3, 4))
    return int(d.sum()) * 10 ** 6 // (g.gw * g.gh)


class TestBand(unittest.TestCase):
    def test_active_coverage_every_size(self):
        for W, H in SIZES[1:]:  # BASE_E: 12222 / 5882 / 2941 / 10742 ppm
            self.assertGreaterEqual(active_ppm(W, H), S.BAND_COV_PPM, (W, H))

    def test_default_frame_unchanged_and_above_gate(self):
        self.assertEqual(S.band_rect(256, 160), S.BAND)
        self.assertGreaterEqual(active_ppm(256, 160), PARAMS["FROZEN_COV_PPM"])

    def test_band_inside_frame_and_clear_of_frozen_rect(self):
        for W, H in SIZES:
            x, y, w, h = S.band_rect(W, H)
            self.assertTrue(x >= 0 and y >= 0 and x + w <= W and y + h <= H and w > 0 and h > 0, (W, H))
            fx, fy, fw, fh = S.FREEZE_F
            self.assertGreaterEqual(y, fy + fh, (W, H))

    def test_frozen_rect_static_after_freeze_frame_every_size(self):
        for W, H in SIZES:
            fr, t, _ = S.render_scenario("frozen_subregion", 1, W, H, 24)
            x, y, w, h = t["rect"]
            ref = fr[9][y:y + h, x:x + w]
            for f in range(9, 24):
                self.assertTrue(np.array_equal(fr[f][y:y + h, x:x + w], ref), (W, H, f))

    def test_frozen_class_reaches_truth_tiles_at_720p(self):
        from tests.test_desc_classes import run, truth_cells
        g, truth, cls, _ = run("frozen_subregion", 1280, 720, 24)
        late = [(f, r) for f, r in truth_cells(g, truth) if f >= 16]
        self.assertTrue(late and all(cls[f][r] == 5 for f, r in late))


class TestStillProbe(unittest.TestCase):
    def test_probe_jitter_density_matches_gpu_jitter(self):
        W, H = 256, 160
        base = S.base_scene(1, W, H)
        with tempfile.TemporaryDirectory() as d:
            S.SynthAdapter().write_run(d, "gpu_jitter", 1, W, H, 16)
            probe = S.SynthAdapter().still_probe(d)
            pf = [f.rgb for f in probe]
        jit, _, _ = S.render_scenario("gpu_jitter", 1, W, H, 16)
        dens = lambda fs: sum(int((f != base).any(axis=2).sum()) for f in fs) / (len(fs) * W * H)
        pd, jd = dens(pf), dens(jit)
        self.assertGreater(jd, 0)
        self.assertLess(abs(pd - jd) / jd, 0.15, (pd, jd))  # BASE_E: probe is 1/8, control 1/4 -> ratio 0.5


if __name__ == "__main__":
    unittest.main()
