"""Pure-numpy tests of bpy_side/decode_exr.py quantisers (no Blender needed)."""
import importlib.util
import os
import unittest

import numpy as np

_p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bpy_side", "decode_exr.py")
_spec = importlib.util.spec_from_file_location("bvqa_decode_exr", _p)
dx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dx)


def rgba(h=2, w=3, **ch):
    a = np.zeros((h, w, 4), np.float32)
    for i, v in ch.items():
        a[..., int(i[1:])] = v
    return a


class QuantTests(unittest.TestCase):
    def test_yflip_index(self):
        a = rgba()
        a[0, 0, 0] = 3  # bottom-left in Blender buffer
        q = dx.quant_index(a, {3}, "IndexOB")
        self.assertEqual(q.dtype, np.uint16)
        self.assertEqual(int(q[-1, 0]), 3)  # flipped to last row
        self.assertEqual(int(q[0, 0]), 0)

    def test_index_not_in_manifest_rejected(self):
        a = rgba()
        a[0, 0, 0] = 9
        with self.assertRaises(dx.TruthError) as cm:
            dx.quant_index(a, {1, 2}, "IndexOB")
        self.assertEqual((cm.exception.name, cm.exception.rc), ("E_TRUTH_QUANT", 2))

    def test_index_zero_always_allowed_and_rounds(self):
        a = rgba()
        a[0, 1, 0] = 2.0004
        q = dx.quant_index(a, {2}, "IndexOB")
        self.assertEqual(int(q[-1, 1]), 2)

    def test_index_negative_or_nan_rejected(self):
        for bad in (-1.0, float("nan"), 70000.0):
            a = rgba()
            a[0, 0, 0] = bad
            with self.assertRaises(dx.TruthError):
                dx.quant_index(a, {1}, "IndexOB")

    def test_motion_sign_and_q4(self):
        a = rgba()
        a[..., 0] = -3.0  # to-prev x = -3 px => object moved +3 px right
        a[..., 1] = 2.0   # to-prev y(up) = +2 => moved down in image? y-up: moved -2 up-axis => +2 rows
        mv = dx.quant_motion(a)
        self.assertEqual(mv.dtype, np.int16)
        self.assertEqual(mv.shape, (2, 3, 2))
        self.assertEqual(int(mv[0, 0, 0]), 48)
        self.assertEqual(int(mv[0, 0, 1]), 32)

    def test_motion_half_away_from_zero(self):
        a = rgba()
        a[..., 0] = 0.5 / 16   # -V0*16 = -0.5 -> -1
        a[..., 1] = 0.5 / 16   # +0.5 -> 1
        mv = dx.quant_motion(a)
        self.assertEqual(int(mv[0, 0, 0]), -1)
        self.assertEqual(int(mv[0, 0, 1]), 1)

    def test_depth_mm_and_background(self):
        a = rgba()
        a[..., 0] = 9.5
        a[0, 0, 0] = 1e10
        z = dx.quant_depth(a)
        self.assertEqual(z.dtype, np.uint32)
        self.assertEqual(int(z[-1, 0]), 0xFFFFFFFF)
        self.assertEqual(int(z[0, 0]), 9500)

    def test_depth_nan_rejected_and_far_clamped(self):
        a = rgba()
        a[0, 0, 0] = float("nan")
        with self.assertRaises(dx.TruthError):
            dx.quant_depth(a)
        a = rgba()
        a[..., 0] = 5e8
        self.assertEqual(int(dx.quant_depth(a)[0, 0]), 2 ** 32 - 2)

    def test_normal_and_uv(self):
        a = rgba()
        a[..., 2] = 1.0
        a[..., 0] = -1.0
        n = dx.quant_normal(a)
        self.assertEqual(n.dtype, np.int8)
        self.assertEqual(tuple(n[0, 0]), (-127, 0, 127))
        u = rgba()
        u[..., 0], u[..., 1] = 1.0, 0.5
        q = dx.quant_uv(u)
        self.assertEqual(q.dtype, np.uint16)
        self.assertEqual((int(q[0, 0, 0]), int(q[0, 0, 1])), (65535, 32768))

    def test_srgb_load_refused(self):
        with self.assertRaises(dx.TruthError) as cm:
            dx.load_exr_rgba("/nonexistent.exr", colorspace="sRGB")
        self.assertEqual(cm.exception.name, "E_TRUTH_QUANT")

    def test_write_frame_atomic_and_sha(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            arr = {"ob": np.arange(6, dtype=np.uint16).reshape(2, 3)}
            s1 = dx.write_frame(arr, d, 7)
            s2 = dx.write_frame(arr, d, 7)
            self.assertEqual(s1, s2)
            self.assertTrue(os.path.exists(os.path.join(d, "f000007.ob.npy")))
            self.assertEqual(os.listdir(d), ["f000007.ob.npy"])
            np.testing.assert_array_equal(np.load(os.path.join(d, "f000007.ob.npy")), arr["ob"])

    def test_arrays_equal_negative(self):
        a = {"ob": np.zeros((2, 2), np.uint16)}
        b = {"ob": np.zeros((2, 2), np.uint16)}
        self.assertTrue(dx.arrays_equal(a, b))
        b["ob"][0, 0] = 1
        self.assertFalse(dx.arrays_equal(a, b))


if __name__ == "__main__":
    unittest.main()
