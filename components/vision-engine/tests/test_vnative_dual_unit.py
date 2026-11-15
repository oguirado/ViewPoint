"""Unit tests for vnative/backend.py and vnative/dual.py."""
import tempfile
import unittest
from pathlib import Path

import numpy as np

from visioncore import desc
from vnative import backend, dual


class TestVNativeBackendUnit(unittest.TestCase):
    def test_resolve(self):
        # Explicit env dicts
        self.assertEqual(backend.resolve({}), "python")
        self.assertEqual(backend.resolve({"VISIONCORE_BACKEND": ""}), "python")
        self.assertEqual(backend.resolve({"VISIONCORE_BACKEND": "python"}), "python")
        self.assertEqual(backend.resolve({"VISIONCORE_BACKEND": "native"}), "native")
        self.assertEqual(backend.resolve({"VISIONCORE_BACKEND": "dual_verify"}), "dual_verify")

        # Negative case: invalid backend string
        with self.assertRaises(backend.VNativeError) as ctx:
            backend.resolve({"VISIONCORE_BACKEND": "invalid_mode"})
        self.assertEqual(ctx.exception.code, "E_VN_BACKEND")
        self.assertEqual(ctx.exception.detail, {"value": "invalid_mode"})

    def test_check_available(self):
        # Mode "python" available even if lib is None or missing
        self.assertIsNone(backend.check_available("python"))

        # Mode "native" with existing dummy lib file
        with tempfile.NamedTemporaryFile(suffix=".so") as tmp:
            self.assertIsNone(backend.check_available("native", lib=tmp.name))

        # Negative case 1: Mode "native" without existing library file
        with tempfile.TemporaryDirectory() as tmp_dir:
            non_existent_lib = Path(tmp_dir) / "missing_lib.so"
            with self.assertRaises(backend.VNativeError) as ctx:
                backend.check_available("native", lib=non_existent_lib)
            self.assertEqual(ctx.exception.code, "E_VN_NATIVE_MISSING")
            self.assertEqual(ctx.exception.detail, {"lib": str(non_existent_lib)})

        # Negative case 2: Mode "dual_verify" when KERNELS_REAL is False
        with tempfile.NamedTemporaryFile(suffix=".so") as tmp:
            with self.assertRaises(backend.VNativeError) as ctx:
                backend.check_available("dual_verify", lib=tmp.name)
            self.assertEqual(ctx.exception.code, "E_VN_NATIVE_MISSING")
            self.assertEqual(ctx.exception.detail, {"lib": str(tmp.name), "kernels_real": False})

    def test_originals(self):
        orig = backend.originals()
        self.assertEqual(set(orig.keys()), {"luma_of_padded", "_compute"})
        self.assertIs(orig["luma_of_padded"], desc.luma_of_padded)
        self.assertIs(orig["_compute"], desc._compute)

        # Negative case: mutating the returned dictionary does not mutate backend state
        orig["luma_of_padded"] = None
        orig["new_key"] = 123
        orig_fresh = backend.originals()
        self.assertIs(orig_fresh["luma_of_padded"], desc.luma_of_padded)
        self.assertNotIn("new_key", orig_fresh)

    def test_installed(self):
        orig_luma = desc.luma_of_padded
        orig_compute = desc._compute

        def dummy_luma(padded):
            return "dummy_luma"

        def dummy_compute(*args):
            return "dummy_compute"

        # Positive case: scope replaces functions and restores on exit
        with backend.installed({"luma_of_padded": dummy_luma, "_compute": dummy_compute}):
            self.assertIs(desc.luma_of_padded, dummy_luma)
            self.assertIs(desc._compute, dummy_compute)

        self.assertIs(desc.luma_of_padded, orig_luma)
        self.assertIs(desc._compute, orig_compute)

        # Negative case 1: exception raised inside installed scope restores original functions
        try:
            with backend.installed({"luma_of_padded": dummy_luma}):
                self.assertIs(desc.luma_of_padded, dummy_luma)
                raise ValueError("error in installed scope")
        except ValueError as err:
            self.assertEqual(str(err), "error in installed scope")

        self.assertIs(desc.luma_of_padded, orig_luma)
        self.assertIs(desc._compute, orig_compute)

        # Negative case 2: Non-LIFO exit raises E_VN_SCOPE
        m1 = backend.installed({"_compute": dummy_compute})
        m2 = backend.installed({"_compute": dummy_compute})
        m1.__enter__()
        m2.__enter__()
        with self.assertRaises(backend.VNativeError) as ctx:
            m1.__exit__(None, None, None)
        self.assertEqual(ctx.exception.code, "E_VN_SCOPE")
        self.assertEqual(ctx.exception.detail, {"reason": "non-LIFO exit"})
        m2.__exit__(None, None, None)
        self.assertIs(desc._compute, orig_compute)

    def test_reference(self):
        orig_luma = desc.luma_of_padded
        orig_compute = desc._compute

        def dummy_luma(padded):
            return "dummy"

        def dummy_compute(*args):
            return "dummy"

        # Positive case: inside installed(), reference() restores original functions in reference scope
        with backend.installed({"luma_of_padded": dummy_luma}):
            self.assertIs(desc.luma_of_padded, dummy_luma)
            with backend.reference():
                self.assertIs(desc.luma_of_padded, orig_luma)
                self.assertIs(desc._compute, orig_compute)
            self.assertIs(desc.luma_of_padded, dummy_luma)

        # Negative case: reference() when desc is patched outside installed() raises E_VN_SCOPE
        try:
            desc._compute = dummy_compute
            with self.assertRaises(backend.VNativeError) as ctx:
                with backend.reference():
                    pass
            self.assertEqual(ctx.exception.code, "E_VN_SCOPE")
            self.assertEqual(ctx.exception.detail, {"reason": "desc patched outside installed()"})
        finally:
            desc._compute = orig_compute


class TestVNativeDualUnit(unittest.TestCase):
    def test_assert_same(self):
        # Equal numpy arrays
        a = np.array([1, 2, 3], dtype=np.int32)
        b = np.array([1, 2, 3], dtype=np.int32)
        self.assertIsNone(dual.assert_same("arr", a, b))

        # Equal structured numpy arrays
        dtype_struct = [("id", np.int32), ("val", np.float64)]
        sa = np.array([(1, 2.5), (2, 3.5)], dtype=dtype_struct)
        sb = np.array([(1, 2.5), (2, 3.5)], dtype=dtype_struct)
        self.assertIsNone(dual.assert_same("struct_arr", sa, sb))

        # Equal bytes/bytearrays
        self.assertIsNone(dual.assert_same("bytes_test", b"hello", b"hello"))
        self.assertIsNone(dual.assert_same("bytearray_test", bytearray(b"abc"), bytearray(b"abc")))

        # Equal non-array values (tuples, lists, primitives)
        self.assertIsNone(dual.assert_same("tuple_test", (1, 2), (1, 2)))
        self.assertIsNone(dual.assert_same("list_test", [1, 2], [1, 2]))
        self.assertIsNone(dual.assert_same("val_test", 42, 42))

        # Negative cases for assert_same:

        # 1. Dtype mismatch
        a_int16 = np.array([1, 2, 3], dtype=np.int16)
        with self.assertRaises(backend.VNativeError) as ctx:
            dual.assert_same("arr_dtype", a, a_int16)
        self.assertEqual(ctx.exception.code, "E_VN_DIVERGE")
        self.assertEqual(ctx.exception.detail, {"name": "arr_dtype", "first_index": -1, "field": "dtype"})

        # 2. Shape mismatch
        a_2d = np.array([[1, 2, 3]], dtype=np.int32)
        with self.assertRaises(backend.VNativeError) as ctx:
            dual.assert_same("arr_shape", a, a_2d)
        self.assertEqual(ctx.exception.code, "E_VN_DIVERGE")
        self.assertEqual(ctx.exception.detail, {"name": "arr_shape", "first_index": -1, "field": "shape"})

        # 3. Unstructured numpy array byte/value mismatch
        c = np.array([1, 99, 3], dtype=np.int32)
        with self.assertRaises(backend.VNativeError) as ctx:
            dual.assert_same("arr_bytes", a, c)
        self.assertEqual(ctx.exception.code, "E_VN_DIVERGE")
        self.assertEqual(ctx.exception.detail, {"name": "arr_bytes", "first_index": 1, "field": "bytes"})

        # 4. Structured numpy array field mismatch
        sc = np.array([(1, 2.5), (2, 9.9)], dtype=dtype_struct)
        with self.assertRaises(backend.VNativeError) as ctx:
            dual.assert_same("struct_diff", sa, sc)
        self.assertEqual(ctx.exception.code, "E_VN_DIVERGE")
        self.assertEqual(ctx.exception.detail, {"name": "struct_diff", "first_index": 1, "field": "val"})

        # 5. Bytes mismatch
        with self.assertRaises(backend.VNativeError) as ctx:
            dual.assert_same("bytes_diff", b"hello", b"heLlo")
        self.assertEqual(ctx.exception.code, "E_VN_DIVERGE")
        self.assertEqual(ctx.exception.detail, {"name": "bytes_diff", "first_index": 2, "field": "bytes"})

        # 6. Non-array unequal values (tuples, lists, primitives)
        with self.assertRaises(backend.VNativeError) as ctx:
            dual.assert_same("tuple_diff", (1, 2), (1, 3))
        self.assertEqual(ctx.exception.code, "E_VN_DIVERGE")
        self.assertEqual(ctx.exception.detail, {"name": "tuple_diff", "first_index": -1, "field": "value"})

        with self.assertRaises(backend.VNativeError) as ctx:
            dual.assert_same("list_diff", [1, 2], [1, 3])
        self.assertEqual(ctx.exception.code, "E_VN_DIVERGE")
        self.assertEqual(ctx.exception.detail, {"name": "list_diff", "first_index": -1, "field": "value"})

    def test_assert_seq_same(self):
        # Equal cases: tuples, lists, numpy arrays as sequence elements
        self.assertIsNone(dual.assert_seq_same("seq_tuple", (10, 20, 30), (10, 20, 30)))
        self.assertIsNone(dual.assert_seq_same("seq_list", ["a", "b"], ["a", "b"]))
        self.assertIsNone(dual.assert_seq_same("seq_mixed", [1, (2, 3)], [1, (2, 3)]))

        # Negative cases for assert_seq_same:

        # 1. Element value mismatch in list/tuple
        with self.assertRaises(backend.VNativeError) as ctx:
            dual.assert_seq_same("seq_val_diff", [10, 20, 30], [10, 99, 30])
        self.assertEqual(ctx.exception.code, "E_VN_DIVERGE")
        self.assertEqual(ctx.exception.detail, {"name": "seq_val_diff", "first_index": 1, "field": "value"})

        with self.assertRaises(backend.VNativeError) as ctx:
            dual.assert_seq_same("tuple_val_diff", ("x", "y", "z"), ("x", "y", "W"))
        self.assertEqual(ctx.exception.code, "E_VN_DIVERGE")
        self.assertEqual(ctx.exception.detail, {"name": "tuple_val_diff", "first_index": 2, "field": "value"})

        # 2. Sequence length mismatch (a longer than b)
        with self.assertRaises(backend.VNativeError) as ctx:
            dual.assert_seq_same("seq_len_a_long", [1, 2, 3], [1, 2])
        self.assertEqual(ctx.exception.code, "E_VN_DIVERGE")
        self.assertEqual(ctx.exception.detail, {"name": "seq_len_a_long", "first_index": 2, "field": "length"})

        # 3. Sequence length mismatch (b longer than a)
        with self.assertRaises(backend.VNativeError) as ctx:
            dual.assert_seq_same("seq_len_b_long", (1, 2), (1, 2, 3, 4))
        self.assertEqual(ctx.exception.code, "E_VN_DIVERGE")
        self.assertEqual(ctx.exception.detail, {"name": "seq_len_b_long", "first_index": 2, "field": "length"})


if __name__ == "__main__":
    unittest.main()
