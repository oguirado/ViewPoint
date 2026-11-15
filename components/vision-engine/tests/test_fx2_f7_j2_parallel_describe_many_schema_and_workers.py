"""Nonduplicate describe_many contracts for deferred fix fx2-f7-visioncore-decode-and-parallel-invariants."""
import multiprocessing as mp
import unittest
from unittest import mock

import numpy as np

from visioncore import desc, parallel
from visioncore.errors import VqaError
from visioncore.grid import Grid


class TestFx2F7J2ParallelDescribeManySchemaAndWorkers(unittest.TestCase):
    def setUp(self):
        self.grid = Grid(64, 48)
        rng = np.random.default_rng(seed=123)
        base = rng.integers(0, 256, (48, 64, 3), dtype=np.uint8)
        self.valid_f0 = base.copy()
        self.valid_f1 = base.copy()
        self.valid_f1[:8, :16] ^= np.uint8(255)
        self.valid_frames = np.stack([self.valid_f0, self.valid_f1, self.valid_f0, self.valid_f1])

    # --- Worker parameter refusal tests (fx2-f7-visioncore-decode-and-parallel-invariants) ---

    @unittest.expectedFailure
    def test_expected_failure_workers_float1_5_refused(self):
        # fx2-f7-visioncore-decode-and-parallel-invariants: float workers=1.5 should raise E_VC_PARAM_RANGE
        snapshots = [bytes(f) for f in self.valid_frames]
        try:
            with self.assertRaises(VqaError) as ctx:
                parallel.describe_many(self.valid_frames, self.grid, workers=1.5)
            self.assertEqual(ctx.exception.code, "E_VC_PARAM_RANGE")
        finally:
            current = [bytes(f) for f in self.valid_frames]
            self.assertEqual(current, snapshots)

    @unittest.expectedFailure
    def test_expected_failure_workers_float2_0_refused(self):
        # fx2-f7-visioncore-decode-and-parallel-invariants: float workers=2.0 should raise E_VC_PARAM_RANGE
        snapshots = [bytes(f) for f in self.valid_frames]
        try:
            with self.assertRaises(VqaError) as ctx:
                parallel.describe_many(self.valid_frames, self.grid, workers=2.0)
            self.assertEqual(ctx.exception.code, "E_VC_PARAM_RANGE")
        finally:
            current = [bytes(f) for f in self.valid_frames]
            self.assertEqual(current, snapshots)

    @unittest.expectedFailure
    def test_expected_failure_workers_numeric_string_refused(self):
        # fx2-f7-visioncore-decode-and-parallel-invariants: string workers="2" should raise E_VC_PARAM_RANGE
        snapshots = [bytes(f) for f in self.valid_frames]
        try:
            with self.assertRaises(VqaError) as ctx:
                parallel.describe_many(self.valid_frames, self.grid, workers="2")
            self.assertEqual(ctx.exception.code, "E_VC_PARAM_RANGE")
        finally:
            current = [bytes(f) for f in self.valid_frames]
            self.assertEqual(current, snapshots)

    @unittest.expectedFailure
    def test_expected_failure_workers_none_refused(self):
        # fx2-f7-visioncore-decode-and-parallel-invariants: workers=None should raise E_VC_PARAM_RANGE
        snapshots = [bytes(f) for f in self.valid_frames]
        try:
            with self.assertRaises(VqaError) as ctx:
                parallel.describe_many(self.valid_frames, self.grid, workers=None)
            self.assertEqual(ctx.exception.code, "E_VC_PARAM_RANGE")
        finally:
            current = [bytes(f) for f in self.valid_frames]
            self.assertEqual(current, snapshots)

    @unittest.expectedFailure
    def test_expected_failure_workers_true_refused(self):
        # fx2-f7-visioncore-decode-and-parallel-invariants: boolean workers=True should raise E_VC_PARAM_RANGE
        snapshots = [bytes(f) for f in self.valid_frames]
        try:
            with self.assertRaises(VqaError) as ctx:
                parallel.describe_many(self.valid_frames, self.grid, workers=True)
            self.assertEqual(ctx.exception.code, "E_VC_PARAM_RANGE")
        finally:
            current = [bytes(f) for f in self.valid_frames]
            self.assertEqual(current, snapshots)

    # --- Frame schema refusal tests (fx2-f7-visioncore-decode-and-parallel-invariants) ---

    @unittest.expectedFailure
    def test_expected_failure_ndarray_wrong_dtype_after_uint8_prefix_refused(self):
        # fx2-f7-visioncore-decode-and-parallel-invariants: wrong-dtype frame in ndarray source must raise E_VC_FRAME_SCHEMA before shm or process creation
        f_float32 = self.valid_f1.astype(np.float32)
        source = np.array([self.valid_f0, f_float32], dtype=object)
        snapshots = [bytes(f) for f in source]
        with mock.patch("visioncore.parallel.shared_memory.SharedMemory", wraps=parallel.shared_memory.SharedMemory) as spy_shm, \
             mock.patch("visioncore.parallel.mp.get_context", wraps=parallel.mp.get_context) as spy_ctx:
            try:
                with self.assertRaises(VqaError) as ctx:
                    parallel.describe_many(source, self.grid, workers=2, cpus=[])
                self.assertEqual(ctx.exception.code, "E_VC_FRAME_SCHEMA")
                self.assertFalse(spy_shm.called, "SharedMemory allocated before schema validation")
                self.assertFalse(spy_ctx.called, "Process context obtained before schema validation")
            finally:
                current = [bytes(f) for f in source]
                self.assertEqual(current, snapshots)

    @unittest.expectedFailure
    def test_expected_failure_list_wrong_dtype_after_uint8_prefix_refused(self):
        # fx2-f7-visioncore-decode-and-parallel-invariants: wrong-dtype frame in list source must raise E_VC_FRAME_SCHEMA before shm or process creation
        f_float32 = self.valid_f1.astype(np.float32)
        source = [self.valid_f0, f_float32]
        snapshots = [bytes(f) for f in source]
        with mock.patch("visioncore.parallel.shared_memory.SharedMemory", wraps=parallel.shared_memory.SharedMemory) as spy_shm, \
             mock.patch("visioncore.parallel.mp.get_context", wraps=parallel.mp.get_context) as spy_ctx:
            try:
                with self.assertRaises(VqaError) as ctx:
                    parallel.describe_many(source, self.grid, workers=2, cpus=[])
                self.assertEqual(ctx.exception.code, "E_VC_FRAME_SCHEMA")
                self.assertFalse(spy_shm.called, "SharedMemory allocated before schema validation")
                self.assertFalse(spy_ctx.called, "Process context obtained before schema validation")
            finally:
                current = [bytes(f) for f in source]
                self.assertEqual(current, snapshots)

    @unittest.expectedFailure
    def test_expected_failure_tuple_wrong_dtype_after_uint8_prefix_refused(self):
        # fx2-f7-visioncore-decode-and-parallel-invariants: wrong-dtype frame in tuple source must raise E_VC_FRAME_SCHEMA before shm or process creation
        f_float32 = self.valid_f1.astype(np.float32)
        source = (self.valid_f0, f_float32)
        snapshots = [bytes(f) for f in source]
        with mock.patch("visioncore.parallel.shared_memory.SharedMemory", wraps=parallel.shared_memory.SharedMemory) as spy_shm, \
             mock.patch("visioncore.parallel.mp.get_context", wraps=parallel.mp.get_context) as spy_ctx:
            try:
                with self.assertRaises(VqaError) as ctx:
                    parallel.describe_many(source, self.grid, workers=2, cpus=[])
                self.assertEqual(ctx.exception.code, "E_VC_FRAME_SCHEMA")
                self.assertFalse(spy_shm.called, "SharedMemory allocated before schema validation")
                self.assertFalse(spy_ctx.called, "Process context obtained before schema validation")
            finally:
                current = [bytes(f) for f in source]
                self.assertEqual(current, snapshots)

    # --- Passing existing-behaviour controls (undecorated) ---

    def test_control_wrong_channels_4ch_raises_frame_shape(self):
        # Deliberately malformed negative control: 4 channels (RGBA) must raise E_VC_FRAME_SHAPE
        f_4ch = np.zeros((48, 64, 4), dtype=np.uint8)
        source = [self.valid_f0, f_4ch]
        snapshots = [bytes(f) for f in source]
        try:
            with self.assertRaises(VqaError) as ctx:
                parallel.describe_many(source, self.grid, workers=2, cpus=[])
            self.assertEqual(ctx.exception.code, "E_VC_FRAME_SHAPE")
        finally:
            current = [bytes(f) for f in source]
            self.assertEqual(current, snapshots)

    def test_control_wrong_channels_1ch_raises_frame_shape(self):
        # Deliberately malformed negative control: 1 channel must raise E_VC_FRAME_SHAPE
        f_1ch = np.zeros((48, 64, 1), dtype=np.uint8)
        source = (self.valid_f0, f_1ch)
        snapshots = [bytes(f) for f in source]
        try:
            with self.assertRaises(VqaError) as ctx:
                parallel.describe_many(source, self.grid, workers=2, cpus=[])
            self.assertEqual(ctx.exception.code, "E_VC_FRAME_SHAPE")
        finally:
            current = [bytes(f) for f in source]
            self.assertEqual(current, snapshots)

    def test_control_four_worker_parity_and_return_s0_with_empty_cpus(self):
        # Extended parity control: 4-worker vs serial with cpus=[] and repeated frames spanning partition boundaries
        f0 = self.valid_f0
        f1 = self.valid_f1
        # 8 frames spanning 4-worker boundaries (2 frames per worker: [f0, f0], [f1, f1], [f0, f0], [f1, f1])
        frames = np.stack([f0, f0, f1, f1, f0, f0, f1, f1])

        tiles_s, pads_s, luma_s, words_s = parallel.describe_many(frames, self.grid, workers=1, return_s0=True)
        tiles_p, pads_p, luma_p, words_p = parallel.describe_many(frames, self.grid, workers=4, cpus=[], return_s0=True)

        self.assertEqual(tiles_s.dtype, desc.TILE_DTYPE)
        self.assertEqual(tiles_p.dtype, desc.TILE_DTYPE)
        self.assertTrue(np.array_equal(tiles_s, tiles_p))
        self.assertTrue(np.array_equal(words_s, words_p))
        self.assertEqual(len(pads_s), len(pads_p))
        self.assertEqual(len(luma_s), len(luma_p))
        for k in range(len(pads_s)):
            self.assertTrue(np.array_equal(pads_s[k], pads_p[k]))
            self.assertTrue(np.array_equal(luma_s[k], luma_p[k]))


if __name__ == "__main__":
    unittest.main()
