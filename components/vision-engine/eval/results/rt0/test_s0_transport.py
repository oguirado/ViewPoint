"""Executed regression for RT0D01: S0 is produced once in workers, including padding."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, os.environ.get('RT0_SOURCE_ROOT', str(ROOT)))
import numpy as np
from visioncore import desc, frame, pipeline
from visioncore.grid import Grid


class S0Transport(unittest.TestCase):
    def test_no_parent_s0_redo(self):
        rng = np.random.default_rng(91)
        images = rng.integers(0, 256, (7, 63, 95, 3), dtype=np.uint8)
        images[3] = images[2]
        src = frame.MemoryFrameSource([frame.Frame(i, i * 33333, i, rgb) for i, rgb in enumerate(images)],
                                      {'width': 95, 'height': 63, 'fps_num': 30, 'fps_den': 1})
        expected = pipeline.analyze(src, workers=1)._blobs()
        parent = os.getpid()
        pad, luma = Grid.pad, desc.luma_of_padded

        def checked_pad(*a, **kw):
            if os.getpid() == parent:
                raise AssertionError('padding repeated in consumer')
            return pad(*a, **kw)

        def checked_luma(*a, **kw):
            if os.getpid() == parent:
                raise AssertionError('luma repeated in consumer')
            return luma(*a, **kw)

        with patch.object(Grid, 'pad', checked_pad), patch.object(desc, 'luma_of_padded', checked_luma):
            got = pipeline.analyze(src, workers=3, cpus=[])._blobs()
        self.assertEqual(got, expected)


if __name__ == '__main__':
    unittest.main()
