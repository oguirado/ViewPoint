"""Unit tests for D01 parallel descriptor fan-out helper functions in visioncore/parallel.py."""
import mmap
import unittest
from unittest import mock

import numpy as np

from visioncore import desc, parallel
from visioncore.errors import VqaError
from visioncore.grid import Grid


class DummyFrame:
    def __init__(self, rgb):
        self.rgb = rgb


class DummySourceWithRaw:
    def __init__(self, raw):
        self.raw_array = raw


class TestFramesArray(unittest.TestCase):
    def test_returns_memmap_when_source_carries_raw_array(self):
        raw = np.zeros((3, 16, 16, 3), dtype=np.uint8)
        source = DummySourceWithRaw(raw)
        res = parallel.frames_array(source)
        self.assertIs(res, raw)

    def test_returns_stacked_uint8_array_otherwise(self):
        f1 = DummyFrame(np.ones((8, 8, 3), dtype=np.uint8) * 10)
        f2 = DummyFrame(np.ones((8, 8, 3), dtype=np.uint8) * 20)
        res = parallel.frames_array([f1, f2])
        self.assertEqual(res.shape, (2, 8, 8, 3))
        self.assertEqual(res.dtype, np.uint8)
        np.testing.assert_array_equal(res[0], f1.rgb)
        np.testing.assert_array_equal(res[1], f2.rgb)

    def test_negative_invalid_source_element(self):
        # Element lacking .rgb attribute
        with self.assertRaises(AttributeError):
            parallel.frames_array([object()])


class TestDefaultCpus(unittest.TestCase):
    def test_returns_big_cpus_from_patched_topology(self):
        with mock.patch.object(parallel.topology, "big_cpus", return_value=[5, 6, 7, 8, 9]):
            self.assertEqual(parallel.default_cpus(), [5, 6, 7, 8, 9])

    def test_returns_empty_list_when_topology_is_none(self):
        with mock.patch.object(parallel, "topology", None):
            self.assertEqual(parallel.default_cpus(), [])

    def test_negative_returns_empty_list_on_topology_exception(self):
        with mock.patch.object(parallel.topology, "big_cpus", side_effect=OSError("unreadable proc")):
            self.assertEqual(parallel.default_cpus(), [])


class TestSharedArray(unittest.TestCase):
    def test_returns_zero_filled_array_backed_by_mmap(self):
        shape = (2, 4)
        arr = parallel._shared_array(shape, np.uint8)
        self.assertEqual(arr.shape, shape)
        self.assertEqual(arr.dtype, np.uint8)
        self.assertIsInstance(arr.base, mmap.mmap)
        self.assertTrue(np.all(arr == 0))

    def test_negative_invalid_dtype(self):
        with self.assertRaises((TypeError, ValueError)):
            parallel._shared_array((2, 2), "invalid_dtype_spec")


class TestWithS0(unittest.TestCase):
    def test_keeps_repeated_frames_sharing_same_immutable_planes(self):
        out = np.zeros((3, 12), dtype=desc.TILE_DTYPE)
        pad0 = np.full((16, 16, 3), 10, dtype=np.uint8)
        pad1 = np.full((16, 16, 3), 20, dtype=np.uint8)
        luma0 = np.full((16, 16), 10, dtype=np.int16)
        luma1 = np.full((16, 16), 20, dtype=np.int16)
        words = np.zeros((3, 12), dtype=bool)

        pads = [pad0, pad1]
        luma = [luma0, luma1]
        indices = [0, 0, 1]  # frames 0 and 1 share plane index 0

        s0 = (pads, luma, words, indices)
        ret_out, ret_pads, ret_luma, ret_words = parallel._with_s0(out, s0)

        self.assertIs(ret_out, out)
        self.assertIs(ret_words, words)
        self.assertEqual(len(ret_pads), 3)
        self.assertEqual(len(ret_luma), 3)
        self.assertIs(ret_pads[0], pad0)
        self.assertIs(ret_pads[1], pad0)  # plane index 0 shared
        self.assertIs(ret_pads[2], pad1)
        self.assertIs(ret_luma[0], luma0)
        self.assertIs(ret_luma[1], luma0)  # plane index 0 shared
        self.assertIs(ret_luma[2], luma1)

    def test_negative_index_out_of_bounds(self):
        out = np.zeros((1, 12), dtype=desc.TILE_DTYPE)
        s0 = ([np.zeros((8, 8, 3))], [np.zeros((8, 8))], np.zeros((1, 12)), [5])
        with self.assertRaises(IndexError):
            parallel._with_s0(out, s0)


class TestDescribeManyUnit(unittest.TestCase):
    def setUp(self):
        self.grid = Grid(64, 48)
        rng = np.random.default_rng(42)
        base = rng.integers(0, 256, (48, 64, 3), dtype=np.uint8)
        frames = []
        for k in range(4):
            f = base.copy()
            f[k * 8 : (k + 1) * 8, :16] ^= np.uint8(127)
            frames.append(f)
        self.frames = np.stack(frames)

    def test_workers1_and_workers2_return_byte_identical_rows(self):
        out_w1 = parallel.describe_many(self.frames, self.grid, workers=1)
        out_w2 = parallel.describe_many(self.frames, self.grid, workers=2, cpus=[])

        self.assertEqual(out_w1.dtype, desc.TILE_DTYPE)
        self.assertEqual(out_w2.dtype, desc.TILE_DTYPE)
        self.assertEqual(out_w1.shape, (4, self.grid.N))
        self.assertEqual(out_w2.shape, (4, self.grid.N))
        self.assertEqual(out_w1.tobytes(), out_w2.tobytes())

    def test_negative_wrong_shape_raises_vqaerror(self):
        wrong_shape_frames = self.frames[:, :32]  # height 32 instead of 48
        with self.assertRaises(VqaError) as ctx:
            parallel.describe_many(wrong_shape_frames, self.grid, workers=1)
        self.assertEqual(ctx.exception.code, "E_VC_FRAME_SHAPE")

    @unittest.expectedFailure
    def test_negative_wrong_dtype_raises_vqaerror(self):
        # BUG: describe_many does not validate frame dtype with VqaError,
        # letting numpy raise UFuncTypeError when computing luma differences.
        wrong_dtype_frames = self.frames.astype(np.float32)
        with self.assertRaises(VqaError) as ctx:
            parallel.describe_many(wrong_dtype_frames, self.grid, workers=1)
        self.assertEqual(ctx.exception.code, "E_VC_FRAME_SCHEMA")

    @unittest.expectedFailure
    def test_negative_workers_zero_raises_vqaerror(self):
        # BUG: describe_many clamps workers=0 to 1 via max(1, min(...))
        # instead of raising VqaError for invalid workers <= 0.
        with self.assertRaises(VqaError) as ctx:
            parallel.describe_many(self.frames, self.grid, workers=0)
        self.assertEqual(ctx.exception.code, "E_VC_PARAM_RANGE")


if __name__ == "__main__":
    unittest.main()
