"""RT1-L2: dual.assert_same and dual_verify divergence detection."""
import contextlib
import copy
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

from tests.test_e1_change_pipeline import BASE_E_SHA, blob_sha, source
from visioncore import desc
from vnative import backend, dual, run as vrun


@contextlib.contextmanager
def real_kernels(kernels=None):
    """Pretend RT2 landed: KERNELS_REAL True, a lib file present, optional replacement KERNELS."""
    old = backend.KERNELS_REAL, backend.KERNELS
    backend.KERNELS_REAL = True
    if kernels is not None:
        backend.KERNELS = kernels
    with tempfile.NamedTemporaryFile(suffix=".so") as f:
        try:
            yield f.name
        finally:
            backend.KERNELS_REAL, backend.KERNELS = old


class TestDual(unittest.TestCase):
    def test_assert_same_fields(self):
        a = np.zeros(8, dtype=desc.TILE_DTYPE)
        dual.assert_same("t", a, a.copy())
        b = a.copy()
        b["sig64"][5] ^= np.uint64(1)
        with self.assertRaises(backend.VNativeError) as c:
            dual.assert_same("t", a, b)
        self.assertEqual((c.exception.code, c.exception.detail["first_index"], c.exception.detail["field"]),
                         ("E_VN_DIVERGE", 5, "sig64"))
        with self.assertRaises(backend.VNativeError) as c:
            dual.assert_same("t", a, a.astype(a.dtype).reshape(2, 4))
        self.assertEqual(c.exception.detail["field"], "shape")
        with self.assertRaises(backend.VNativeError) as c:
            dual.assert_same("x", np.zeros(2, np.int16), np.zeros(2, np.int32))
        self.assertEqual(c.exception.detail["field"], "dtype")
        with self.assertRaises(backend.VNativeError) as c:
            dual.assert_same("g", b"abc", b"abd")
        self.assertEqual(c.exception.detail["first_index"], 2)

    def test_dual_identity_shim_ok_and_returns_python_run(self):
        with real_kernels() as lib:
            r = vrun.run(source("scroll"), mode="dual_verify", lib=lib)
        self.assertEqual(blob_sha(r), BASE_E_SHA["scroll"])

    def test_dual_refuses_shim_or_missing_lib(self):  # adv F-04: python-vs-own-shim must not PASS
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(backend.VNativeError) as c:  # KERNELS_REAL False, lib present
                with tempfile.NamedTemporaryFile(dir=t, suffix=".so") as f:
                    vrun.run(source("clean_static"), mode="dual_verify", lib=f.name)
            self.assertEqual(c.exception.code, "E_VN_NATIVE_MISSING")
            with real_kernels():
                with self.assertRaises(backend.VNativeError) as c:  # KERNELS_REAL True, no lib
                    vrun.run(source("clean_static"), mode="dual_verify", lib=Path(t) / "none.so")
            self.assertEqual(c.exception.code, "E_VN_NATIVE_MISSING")

    def test_workers_refused_native_and_dual(self):  # adv F-01
        with real_kernels() as lib:
            for m in ("native", "dual_verify"):
                with self.assertRaises(backend.VNativeError) as c:
                    vrun.run(source("clean_static"), mode=m, workers=2, lib=lib)
                self.assertEqual(c.exception.code, "E_VN_WORKERS")

    def test_rt2_slots_listed(self):
        for k in backend.RT2_SLOTS:
            self.assertIn(k, backend.KERNELS)
        self.assertEqual(set(backend.RT2_SLOTS), {"grid.pad", "words_eq"})

    def test_dual_honours_out_dir(self):
        with real_kernels() as lib, tempfile.TemporaryDirectory() as t:
            out = Path(t) / "run"
            vrun.run(source("clean_static"), mode="dual_verify", lib=lib, out_dir=out)
            self.assertTrue((out / "tiles.bin").is_file())

    def test_dual_detects_sig64_flip(self):
        orig = backend.originals()

        def bad(out, idx, *a):
            orig["_compute"](out, idx, *a)
            out["sig64"][idx[5]] ^= np.uint64(1)
        k = {"luma_of_padded": orig["luma_of_padded"], "_compute": bad}
        with real_kernels(k) as lib:
            with self.assertRaises(backend.VNativeError) as c:
                vrun.run(source("clean_static"), mode="dual_verify", lib=lib)
        d = c.exception.detail
        self.assertEqual(c.exception.code, "E_VN_DIVERGE")
        self.assertEqual((d["name"], d["field"]), ("tiles[0]", "sig64"))
        self.assertEqual(d["first_index"], 5)  # F2: exact planted position (idx[5] is the 6th computed tile)

    def test_reference_leg_ignores_concurrent_patch(self):  # judge F1: lock + original kernels in the reference leg
        orig = backend.originals()
        entered, release, hits = threading.Event(), threading.Event(), []

        def flip(out, idx, *a):
            hits.append(1)
            orig["_compute"](out, idx, *a)
            out["sig64"][idx[0]] ^= np.uint64(1)

        def holder():
            with backend.installed({"_compute": flip}):
                entered.set()
                release.wait(20)
        res = {}

        def dual_run():
            try:
                res["r"] = vrun.run(source("clean_static"), mode="dual_verify", lib=lib)
            except Exception as e:  # noqa: BLE001
                res["e"] = e
        with real_kernels() as lib:
            a = threading.Thread(target=holder)
            a.start()
            self.assertTrue(entered.wait(10))
            b = threading.Thread(target=dual_run)
            b.start()
            b.join(1.0)
            self.assertTrue(b.is_alive(), "dual_verify ran while another thread held installed()")
            release.set()
            a.join(10)
            b.join(60)
        self.assertEqual(hits, [])
        self.assertNotIn("e", res)
        self.assertEqual(blob_sha(res["r"]), BASE_E_SHA["clean_static"])
        self.assertIs(desc._compute, orig["_compute"])

    def test_reference_runs_original_kernels_when_globals_patched_in_scope(self):
        orig = backend.originals()
        seen = []

        def spy(*a):
            seen.append(1)
            return orig["_compute"](*a)
        with real_kernels({"luma_of_padded": orig["luma_of_padded"], "_compute": spy}) as lib:
            with backend.installed({"_compute": spy}):  # caller already inside a patched scope
                vrun.run(source("clean_static"), mode="dual_verify", lib=lib)
        # only the native leg may call the spy; the reference leg is on originals
        with real_kernels({"luma_of_padded": orig["luma_of_padded"], "_compute": spy}) as lib:
            seen.clear()
            vrun.run(source("clean_static"), mode="dual_verify", lib=lib)
            n_native = len(seen)
        self.assertGreater(n_native, 0)
        with real_kernels({"luma_of_padded": orig["luma_of_padded"], "_compute": spy}) as lib:
            seen.clear()
            with backend.installed({"_compute": spy}):
                vrun.run(source("clean_static"), mode="dual_verify", lib=lib)
        self.assertEqual(len(seen), n_native)  # ref leg added no spy calls despite the enclosing patch

    def test_reference_refuses_foreign_patch(self):
        orig = desc._compute
        desc._compute = lambda *a: orig(*a)
        try:
            with self.assertRaises(backend.VNativeError) as c:
                with backend.reference():
                    pass
            self.assertEqual(c.exception.code, "E_VN_SCOPE")
        finally:
            desc._compute = orig

    def test_non_lifo_exit_detected_and_restored(self):
        l0, c0 = desc.luma_of_padded, desc._compute
        m1, m2 = backend.installed({"_compute": lambda *a: 0}), backend.installed({"_compute": lambda *a: 1})
        m1.__enter__()
        m2.__enter__()
        with self.assertRaises(backend.VNativeError) as c:
            m1.__exit__(None, None, None)  # outer leaves first
        self.assertEqual(c.exception.code, "E_VN_SCOPE")
        m2.__exit__(None, None, None)
        self.assertIs(desc._compute, c0)
        self.assertIs(desc.luma_of_padded, l0)


def _planted(fn):
    """Run clean python twice (scroll: has regions), let fn plant one divergence in b, return compare_runs' error."""
    from visioncore import pipeline
    a = pipeline.analyze(source("scroll"))
    b = pipeline.analyze(source("scroll"))
    fn(b)
    try:
        vrun.compare_runs(a, b)
    except backend.VNativeError as e:
        return e
    return None


class TestPlantedFamilies(unittest.TestCase):
    """judge F2: one planted divergence per compared family, exact first_index."""

    def test_clean_pair_compares_equal(self):
        self.assertIsNone(_planted(lambda b: None))

    def test_tile(self):
        def plant(b):
            b.tiles[3]["sig64"][7] ^= np.uint64(1)
        e = _planted(plant)
        self.assertEqual((e.detail["name"], e.detail["first_index"], e.detail["field"]), ("tiles[3]", 7, "sig64"))

    def test_region(self):
        def plant(b):
            b.regions[1][0]["r_max"] += 1
        e = _planted(plant)
        self.assertEqual(e.code, "E_VN_DIVERGE")
        self.assertEqual(e.detail["name"], "regions[1]")
        self.assertEqual(e.detail["field"], "bytes")
        self.assertGreater(e.detail["first_index"], 0)

    def test_link(self):
        def plant(b):
            b.links[2]["frame"] = 99
        e = _planted(plant)
        self.assertEqual(e.detail["name"], "links[2]")

    def test_global_mv(self):
        def plant(b):
            b.global_mv[5] = (b.global_mv[5][0] + 1, b.global_mv[5][1])
        e = _planted(plant)
        self.assertEqual((e.detail["name"], e.detail["first_index"]), ("global_mv", 5))

    def test_frame_row(self):
        def plant(b):
            b.frame_rows[6] = dict(b.frame_rows[6], cov_ppm=b.frame_rows[6]["cov_ppm"] + 1)
        e = _planted(plant)
        self.assertEqual((e.detail["name"], e.detail["first_index"]), ("frame_rows", 6))

    def test_gop_byte(self):
        def plant(b):
            g = bytearray(b.gop_files["gops.bin"])
            g[123] ^= 1
            b.gop_files["gops.bin"] = bytes(g)
        e = _planted(plant)
        self.assertEqual((e.detail["name"], e.detail["first_index"]), ("gop:gops.bin", 123))

    def test_frames_sha_and_stages(self):
        e = _planted(lambda b: setattr(b, "frames_sha256", "0" * 64))
        self.assertEqual(e.detail["name"], "frames_sha256")
        e = _planted(lambda b: b.stages.__setitem__("s5", "absent"))
        self.assertEqual(e.detail["name"], "stages")


if __name__ == "__main__":
    unittest.main()
