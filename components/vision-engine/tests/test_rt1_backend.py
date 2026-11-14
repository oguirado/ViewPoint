"""RT1-L2: backend resolution, scoped kernel install/restore, python-mode identity on the 7 goldens."""
import tempfile
import unittest
from pathlib import Path

from tests.test_e1_change_pipeline import BASE_E_SHA, blob_sha, source
from visioncore import desc, pipeline
from vnative import backend, run as vrun


class TestBackend(unittest.TestCase):
    def test_resolve_default_and_modes(self):
        self.assertEqual(backend.resolve({}), "python")
        self.assertEqual(backend.resolve({"VISIONCORE_BACKEND": ""}), "python")
        for m in backend.MODES:
            self.assertEqual(backend.resolve({"VISIONCORE_BACKEND": m}), m)

    def test_unknown_backend(self):
        with self.assertRaises(backend.VNativeError) as c:
            backend.resolve({"VISIONCORE_BACKEND": "gpu"})
        self.assertEqual(c.exception.code, "E_VN_BACKEND")
        with self.assertRaises(backend.VNativeError) as c:
            vrun.run(source("clean_static"), mode="bogus")
        self.assertEqual(c.exception.code, "E_VN_BACKEND")

    def test_native_without_lib(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(backend.VNativeError) as c:
                vrun.run(source("clean_static"), mode="native", lib=Path(t) / "none.so")
            self.assertEqual(c.exception.code, "E_VN_NATIVE_MISSING")
            with tempfile.NamedTemporaryFile(dir=t, suffix=".so") as f:  # lib present -> identity shim runs
                r = vrun.run(source("clean_static"), mode="native", lib=f.name)
                self.assertEqual(blob_sha(r), BASE_E_SHA["clean_static"])

    def test_originals_restored_after_raising_kernel(self):
        l0, c0 = desc.luma_of_padded, desc._compute

        def boom(*a, **k):
            raise RuntimeError("kernel")
        with self.assertRaises(RuntimeError):
            with backend.installed({"luma_of_padded": desc.luma_of_padded, "_compute": boom}):
                self.assertIs(desc._compute, boom)
                pipeline.analyze(source("clean_static"))
        self.assertIs(desc.luma_of_padded, l0)
        self.assertIs(desc._compute, c0)

    def test_scoped_patch_reaches_pipeline(self):
        calls = []
        orig = backend.originals()

        def lum(p):
            calls.append("l")
            return orig["luma_of_padded"](p)

        def comp(*a):
            calls.append("c")
            return orig["_compute"](*a)
        with backend.installed({"luma_of_padded": lum, "_compute": comp}):
            pipeline.analyze(source("clean_static"))
        self.assertIn("l", calls)
        self.assertIn("c", calls)

    def test_python_mode_identical_on_goldens(self):
        for sc, sha in sorted(BASE_E_SHA.items()):
            a = pipeline.analyze(source(sc))
            b = vrun.run(source(sc), mode="python")
            self.assertEqual(blob_sha(a), sha, sc)
            self.assertEqual(blob_sha(b), sha, sc)

    def test_python_mode_is_the_sealed_call(self):
        sentinel = object()
        orig = pipeline.analyze
        pipeline.analyze = lambda *a, **k: sentinel
        try:
            self.assertIs(vrun.run(None, mode="python"), sentinel)
        finally:
            pipeline.analyze = orig


if __name__ == "__main__":
    unittest.main()
