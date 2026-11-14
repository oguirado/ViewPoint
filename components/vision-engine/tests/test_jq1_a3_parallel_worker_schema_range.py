"""Adversarial unit tests for visioncore.parallel.describe_many schema validation and worker parameter range checking."""
import unittest
import numpy as np

from visioncore import desc, parallel
from visioncore.errors import VqaError
from visioncore.grid import Grid


class TestParallelWorkerSchemaRange(unittest.TestCase):
    def setUp(self):
        self.grid = Grid(64, 48)
        rng = np.random.default_rng(seed=7)
        self.frames = rng.integers(0, 256, (4, 48, 64, 3), dtype=np.uint8)

    def test_valid_uint8_two_worker_control(self):
        out = parallel.describe_many(self.frames, self.grid, workers=2, cpus=[])
        self.assertEqual(out.dtype, desc.TILE_DTYPE)
        self.assertEqual(out.shape, (4, self.grid.N))

    def test_passing_negative_control_four_channel_shape(self):
        rng = np.random.default_rng(seed=7)
        f_4ch_list = [rng.integers(0, 256, (48, 64, 4), dtype=np.uint8) for _ in range(4)]
        with self.assertRaises(VqaError) as ctx:
            parallel.describe_many(f_4ch_list, self.grid)
        self.assertEqual(ctx.exception.code, "E_VC_FRAME_SHAPE")

    @unittest.expectedFailure
    def test_expected_failure_float32_ndarray_workers2_b1(self):
        # B1: float32 ndarray with workers=2 should raise E_VC_FRAME_SCHEMA rather than E_IO
        f_f32 = self.frames.astype(np.float32)
        with self.assertRaises(VqaError) as ctx:
            parallel.describe_many(f_f32, self.grid, workers=2, cpus=[])
        self.assertEqual(ctx.exception.code, "E_VC_FRAME_SCHEMA")

    @unittest.expectedFailure
    def test_expected_failure_int16_list_workers2_b1(self):
        # B1: int16 list of frames with workers=2 should raise E_VC_FRAME_SCHEMA rather than E_IO
        f_i16_list = [f.astype(np.int16) for f in self.frames]
        with self.assertRaises(VqaError) as ctx:
            parallel.describe_many(f_i16_list, self.grid, workers=2, cpus=[])
        self.assertEqual(ctx.exception.code, "E_VC_FRAME_SCHEMA")

    @unittest.expectedFailure
    def test_expected_failure_workers_negative_three_b2(self):
        # B2: workers=-3 should raise E_VC_PARAM_RANGE
        with self.assertRaises(VqaError) as ctx:
            parallel.describe_many(self.frames, self.grid, workers=-3)
        self.assertEqual(ctx.exception.code, "E_VC_PARAM_RANGE")

    @unittest.expectedFailure
    def test_expected_failure_workers_false_b2(self):
        # B2: workers=False should raise E_VC_PARAM_RANGE
        with self.assertRaises(VqaError) as ctx:
            parallel.describe_many(self.frames, self.grid, workers=False)
        self.assertEqual(ctx.exception.code, "E_VC_PARAM_RANGE")


if __name__ == "__main__":
    unittest.main()
