"""Frame-parallel S1 (CDESIGN_CENG F3): describe_many is byte-identical to the serial path."""
import hashlib
import tempfile
import unittest
from pathlib import Path

import numpy as np

from visioncore import desc, parallel, pipeline
from visioncore.errors import VqaError
from visioncore.grid import Grid


def frames(n=6, W=96, H=64, seed=3):
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 256, (H, W, 3), dtype=np.uint8)
    out = []
    for k in range(n):
        f = base.copy()
        f[(k * 5) % H:(k * 5) % H + 8, 4:40] ^= np.uint8(37)
        out.append(f)
    return np.stack(out)


class TestDescribeMany(unittest.TestCase):
    def setUp(self):
        self.fr = frames()
        self.grid = Grid(96, 64)

    def serial_chain(self):
        prev = prev_xh = prev_t = None
        rows = []
        for f in self.fr:
            padded = self.grid.pad(f)
            t, _ = desc.describe_padded(padded, self.grid, prev, prev_xh, None, prev_t)
            rows.append(t)
            prev, prev_xh, prev_t = padded, t["xh64"], t
        return np.stack(rows)

    def test_workers_identical_and_match_incremental(self):
        ref = parallel.describe_many(self.fr, self.grid, 1)
        self.assertEqual(ref.tobytes(), self.serial_chain().tobytes())
        for w in (2, 4, 9):
            got = parallel.describe_many(self.fr, self.grid, w, cpus=[])
            self.assertEqual(hashlib.sha256(got.tobytes()).hexdigest(), hashlib.sha256(ref.tobytes()).hexdigest())

    def test_explicit_cpu_pinning_runs(self):
        import os
        cpu = sorted(os.sched_getaffinity(0))[0]
        got = parallel.describe_many(self.fr, self.grid, 2, cpus=[cpu])
        self.assertEqual(got.tobytes(), parallel.describe_many(self.fr, self.grid, 1).tobytes())

    def test_bad_shape_refused(self):
        with self.assertRaises(VqaError) as c:
            parallel.describe_many(self.fr[:, :32], self.grid, 2)
        self.assertEqual(c.exception.code, "E_VC_FRAME_SHAPE")

    def test_bad_cpu_fails_loudly(self):
        with self.assertRaises(VqaError) as c:
            parallel.describe_many(self.fr, self.grid, 2, cpus=[100000])
        self.assertEqual(c.exception.code, "E_IO")

    def test_empty(self):
        self.assertEqual(parallel.describe_many(self.fr[:0], self.grid, 3).shape, (0, self.grid.N))


class TestAnalyzeParallel(unittest.TestCase):
    def test_outputs_byte_identical(self):
        from vadapt.synth import SynthAdapter
        d = Path(tempfile.mkdtemp(prefix="par_"))
        SynthAdapter().write_run(str(d), "scroll", 1)
        shas = []
        for w in (1, 3):
            out = d / f"v{w}"
            pipeline.analyze(pipeline.ManifestSource(d / "frames.manifest.json"), None, None, out, workers=w, cpus=[])
            shas.append({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.iterdir())})
        self.assertEqual(shas[0], shas[1])
        self.assertIn("tiles.bin", shas[0])


if __name__ == "__main__":
    unittest.main()
