"""W10B3: clean capture twin validation for bench B3."""
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np

from blender_visual_qa.truth import run_bench as rb

SCRATCH = rb.REPO / 'build/room_B'


class B3CleanPairTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name)
        self.table = rb.vparams.effective(None)

    def pair(self, delta=lambda rgb, f: None, base=lambda f: 100, w=32, h=16, nf=2):
        """Independent captures with a supplied image-space perturbation."""
        def render(bench, run, w_arg, h_arg, nf_arg, spp, which):
            clean = Path(run) / ('clean_%d' % which)
            (clean / 'render').mkdir(parents=True, exist_ok=True)
            for f in range(1, nf + 1):
                rgb = np.full((h, w, 3), base(f), np.uint8)
                if which:
                    delta(rgb, f)
                (clean / 'render' / ('frame_%04d.png' % f)).write_bytes(rb.decode.encode_png(rgb))
            return clean
        return render

    def test_b3_missing_clean_frame_raises_e_clean_pair(self):
        render = self.pair()
        def incomplete(*args):
            clean = render(*args)
            if args[-1] == 1:  # second clean capture
                (clean / 'render/frame_0002.png').unlink()
            return clean
        with patch.object(rb, 'render_clean', side_effect=incomplete):
            with self.assertRaises(rb.BenchError) as raised:
                rb.clean_resid(self.run, 'B3', self.table, 32, 16, 2, spp0=1024)
        self.assertEqual(raised.exception.code, 'E_CLEAN_PAIR')

    def test_b3_frame_identity_mismatch_raises_e_clean_pair(self):
        render = self.pair()
        def mismatched(*args):
            clean = render(*args)
            if args[-1] == 1:
                # Rename frame_0002.png to frame_0003.png so frame identity / render_index disagrees
                (clean / 'render/frame_0002.png').rename(clean / 'render/frame_0003.png')
            return clean
        with patch.object(rb, 'render_clean', side_effect=mismatched):
            with self.assertRaises(rb.BenchError) as raised:
                rb.clean_resid(self.run, 'B3', self.table, 32, 16, 2, spp0=1024)
        self.assertEqual(raised.exception.code, 'E_CLEAN_PAIR')

    def test_b3_dimension_mismatch_raises_e_clean_pair(self):
        def render(bench, run, w_arg, h_arg, nf_arg, spp, which):
            clean = Path(run) / ('clean_%d' % which)
            (clean / 'render').mkdir(parents=True, exist_ok=True)
            # Use different width for second clean capture
            width = 32 if which == 0 else 16
            for f in range(1, nf_arg + 1):
                rgb = np.full((h_arg, width, 3), 100, np.uint8)
                (clean / 'render' / ('frame_%04d.png' % f)).write_bytes(rb.decode.encode_png(rgb))
            return clean

        with patch.object(rb, 'render_clean', side_effect=render):
            with self.assertRaises(rb.BenchError) as raised:
                rb.clean_resid(self.run, 'B3', self.table, 32, 16, 2, spp0=1024)
        self.assertEqual(raised.exception.code, 'E_CLEAN_PAIR')

    def test_b3_passing_well_formed_pair(self):
        with patch.object(rb, 'render_clean', side_effect=self.pair()):
            residual, spp = rb.clean_resid(self.run, 'B3', self.table, 32, 16, 2, spp0=1024)
        self.assertEqual(spp, 1024)
        self.assertEqual(residual.shape, (2, 2))


if __name__ == '__main__':
    unittest.main()
