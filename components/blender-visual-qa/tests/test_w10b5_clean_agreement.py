"""W10B5: clean-image agreement must survive descriptor quantization boundaries."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from blender_visual_qa.truth import run_bench as rb

SCRATCH = rb.REPO / 'build/room_B'


class CleanAgreementTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name)
        self.table = rb.vparams.effective(None)

    def pair(self, delta, base=lambda f: 100):
        """Independent captures with a supplied image-space perturbation."""
        def render(bench, run, w, h, nf, spp, which):
            clean = Path(run) / ('clean_%d' % which)
            (clean / 'render').mkdir(parents=True, exist_ok=True)
            for f in range(1, nf + 1):
                rgb = np.full((h, w, 3), base(f), np.uint8)
                if which:
                    delta(rgb, f)
                (clean / 'render' / ('frame_%04d.png' % f)).write_bytes(rb.decode.encode_png(rgb))
            return clean
        return render

    def test_single_code_pixel_boundary_is_not_nonconvergence(self):
        # Each tile has one red code lowered by one: co_h floors to -1,
        # contributing 8 q4 despite only 1/256 code of pixel disagreement.
        def perturb(rgb, f):
            rgb[0, ::16, 0] -= 1
        with patch.object(rb, 'render_clean', side_effect=self.pair(perturb)):
            residual, spp = rb.clean_resid(self.run, 'B5', self.table, 32, 16, 2, spp0=4096)
        self.assertEqual(spp, 4096)
        np.testing.assert_array_equal(residual, np.zeros((2, 2), np.int64))
        receipt = json.loads((self.run / 'clean_agreement.json').read_text())
        self.assertEqual(receipt['metric'], 'rgb-max-mean-q4/v1')
        self.assertEqual((receipt['population'], receipt['bad_tiles'], receipt['s1_bad_tiles']), (4, 0, 4))

    def test_genuine_photometric_disagreement_is_refused_at_cap(self):
        def perturb(rgb, f):
            rgb[:] += 10
        with patch.object(rb, 'render_clean', side_effect=self.pair(perturb)):
            with self.assertRaises(rb.BenchError) as raised:
                rb.clean_resid(self.run, 'B5', self.table, 32, 16, 2, spp0=4096)
        self.assertEqual((raised.exception.code, raised.exception.rc), ('E_BUDGET', 6))

    def test_subbin_chroma_change_is_not_hidden_by_s1_rounding(self):
        # Uniform +1 red has identical floored S1 fields but differs by
        # 16 q4 in image space, beyond the 7 q4 clean tolerance.
        def perturb(rgb, f):
            rgb[:, :, 0] += 1
        with patch.object(rb, 'render_clean', side_effect=self.pair(perturb)):
            with self.assertRaises(rb.BenchError) as raised:
                rb.clean_resid(self.run, 'B5', self.table, 32, 16, 2, spp0=4096)
        self.assertEqual(raised.exception.rc, 6)

    def test_temporal_s1_truth_is_preserved(self):
        with patch.object(rb, 'render_clean', side_effect=self.pair(lambda rgb, f: None, base=lambda f: 100 + 10 * f)):
            residual, _ = rb.clean_resid(self.run, 'B5', self.table, 32, 16, 2, spp0=4096)
        np.testing.assert_array_equal(residual, [[0, 0], [160, 160]])

    def test_missing_clean_frame_is_not_silently_omitted(self):
        render = self.pair(lambda rgb, f: None)
        def incomplete(*args):
            clean = render(*args)
            if args[-1] == 1:
                (clean / 'render/frame_0002.png').unlink()
            return clean
        with patch.object(rb, 'render_clean', side_effect=incomplete):
            with self.assertRaises(rb.BenchError) as raised:
                rb.clean_resid(self.run, 'B5', self.table, 32, 16, 2, spp0=4096)
        self.assertEqual(raised.exception.code, 'E_CLEAN_PAIR')

    def test_over_cap_exclusion_remains_refused(self):
        # No exclusion policy is introduced. Two failures of one bench
        # (the night-3 case) must remain an incomplete population.
        failures = [dict(bench='B5', seed=k, seed_group='fit', rc=6,
                         error='E_BUDGET: planted nonconvergence') for k in (0, 6)]
        report = dict(grid='grid:320x180:s16', spp=4, frames=60, renderer='cycles-cpu-4spp-box', profile=None)
        rc = rb._incomplete(self.run, report, [], failures)
        stored = json.loads((self.run / 'calib_report.json').read_text())
        self.assertEqual(rc, 3)
        self.assertEqual(stored['calibration']['refusal'], 'incomplete_population')
        self.assertEqual(stored['failures'], failures)
        self.assertIsNone(stored['profile'])
        self.assertFalse((self.run / 'profile.json').exists())


if __name__ == '__main__':
    unittest.main()
