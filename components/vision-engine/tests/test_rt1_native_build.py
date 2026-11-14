"""RT1-L3: crate skeleton + tools/build_native.py. Native-.so tests skip ONLY if build/native/receipt.json says the toolchain is AUSENTE."""
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import build_native  # noqa: E402
from visioncore import desc  # noqa: E402

RECEIPT = ROOT / "build" / "native" / "receipt.json"
LIBSRC = (ROOT / "rust/visioncore_native/src/lib.rs").read_text()
PYSRC = (ROOT / "rust/visioncore_native/src/py.rs").read_text()


def rust_fields(src=LIBSRC):
    rows = re.findall(r"\(\"(\w+)\",\s*(\d+),\s*(\d+),\s*'([ui])'\)", src.split("TILE_FIELDS")[1].split("];")[0])
    return [(n, int(o), int(s), k) for n, o, s, k in rows]


def py_fields(dt):
    return [(n, dt.fields[n][1], dt.fields[n][0].itemsize, dt.fields[n][0].kind) for n in dt.names]


def descr(dt):
    return {"itemsize": dt.itemsize, "fields": py_fields(dt)}


def unguarded_pyfunctions(src):
    """names of #[pyfunction] fns whose body is not exactly one guard("name", ...) call (guard wraps catch_unwind)."""
    bad = []
    for m in re.finditer(r"#\[pyfunction\](?:\s*#\[[^\]]*\])*\s*fn\s+(\w+)[^{]*\{", src):
        i, depth = m.end(), 1
        while depth:
            depth += {"{": 1, "}": -1}.get(src[i], 0)
            i += 1
        body = src[m.end():i - 1].strip()
        if not (body.startswith('guard("%s",' % m.group(1)) and body.endswith(")")):
            bad.append(m.group(1))
    return bad


def sign_flipped(dt, name="co_h"):
    kinds = {n: dt.fields[n][0] for n in dt.names}
    kinds[name] = np.dtype("<u1")
    return np.dtype({"names": list(dt.names), "formats": [kinds[n] for n in dt.names],
                     "offsets": [dt.fields[n][1] for n in dt.names], "itemsize": dt.itemsize})


class StaticLayout(unittest.TestCase):
    def test_lib_rs_mirrors_tile_dtype(self):
        self.assertEqual(rust_fields(), py_fields(desc.TILE_DTYPE))
        self.assertIn("TILE_SIZE: usize = %d" % desc.TILE_DTYPE.itemsize, LIBSRC)

    def test_mutated_dtype_is_detected(self):
        bad = np.dtype({"names": list(desc.TILE_DTYPE.names), "formats": [desc.TILE_DTYPE.fields[n][0] for n in desc.TILE_DTYPE.names],
                        "offsets": [o + (1 if n == "sig64" else 0) for n, o in zip(desc.TILE_DTYPE.names, [desc.TILE_DTYPE.fields[n][1] for n in desc.TILE_DTYPE.names])],
                        "itemsize": 40})
        self.assertNotEqual(rust_fields(), py_fields(bad))

    def test_signedness_is_part_of_the_mirror(self):  # judge F8
        self.assertEqual({n: k for n, _, _, k in rust_fields()}["co_h"], "i")
        self.assertEqual(py_fields(desc.TILE_DTYPE)[4][3], "i")
        self.assertNotEqual(rust_fields(), py_fields(sign_flipped(desc.TILE_DTYPE)))
        self.assertNotEqual(rust_fields(LIBSRC.replace("(\"co_h\", 6, 1, 'i')", "(\"co_h\", 6, 1, 'u')")), rust_fields())

    def test_every_pyfunction_is_guarded(self):  # judge F9 / adv F-08
        self.assertEqual(unguarded_pyfunctions(PYSRC), [])
        self.assertIn("catch_unwind", PYSRC)
        self.assertGreaterEqual(len(re.findall(r"#\[pyfunction\]", PYSRC)), 3)
        naked = PYSRC.replace('guard("abi_version", || Ok(ABI_VERSION))', "Ok(ABI_VERSION)")
        self.assertEqual(unguarded_pyfunctions(naked), ["abi_version"])
        self.assertEqual(unguarded_pyfunctions(PYSRC.replace('guard("layout_check", ||', "(|| ")), ["layout_check"])

    def test_crate_layout_and_config(self):
        c = ROOT / "rust/visioncore_native"
        for f in ("Cargo.toml", ".cargo/config.toml", "src/lib.rs", "src/s0.rs", "src/s1.rs", "src/isqrt.rs", "src/par.rs", "src/py.rs"):
            self.assertTrue((c / f).is_file(), f)
        cfg = (c / ".cargo/config.toml").read_text()
        self.assertNotIn("target-cpu", cfg.replace("No target-cpu=native", ""))
        self.assertIn("+neon", cfg)
        self.assertIn("abi3-py312", (c / "Cargo.toml").read_text())
        py = (c / "src/py.rs").read_text()
        for name in ("abi_version", "layout_check", "panic_probe", "catch_unwind", "dtype_descr"):
            self.assertIn(name, py)


class BuildTool(unittest.TestCase):
    def _crate_copy(self, t, lock):
        dst = Path(t) / "rust" / "visioncore_native"
        shutil.copytree(ROOT / "rust/visioncore_native", dst, ignore=shutil.ignore_patterns("Cargo.lock", "target"))
        if lock:
            (dst / "Cargo.lock").write_text("# placeholder\n")
        return dst

    def test_missing_lock_is_reported_distinctly(self):  # judge F3
        if not build_native.find_cargo():
            self.skipTest("cargo AUSENTE on this host")
        with tempfile.TemporaryDirectory() as t:
            crate = self._crate_copy(t, lock=False)
            rc, rec = build_native.build(root=t, out=Path(t) / "o")
            self.assertEqual(rc, 3)
            self.assertEqual(rec["status"], "AUSENTE")
            self.assertTrue(rec["reason"].startswith("lock AUSENTE"), rec["reason"])
            self.assertNotIn("crates AUSENTE", rec["reason"])
            self.assertFalse((crate / "Cargo.lock").exists())  # the tool must not write into the source tree

    def test_with_lock_the_failure_is_not_lock_ausente(self):
        if not build_native.find_cargo():
            self.skipTest("cargo AUSENTE on this host")
        with tempfile.TemporaryDirectory() as t:
            self._crate_copy(t, lock=True)
            rc, rec = build_native.build(root=t, out=Path(t) / "o")
            self.assertNotIn("lock AUSENTE", rec.get("reason") or "")

    def test_lock_required_once_crates_present(self):  # judge F3: crates in the registry => the repo must carry Cargo.lock
        cargo = build_native.find_cargo()
        if not cargo:
            self.skipTest("cargo AUSENTE on this host")
        with tempfile.TemporaryDirectory() as t:
            crate = self._crate_copy(t, lock=False)
            r = subprocess.run([cargo, "metadata", "--offline", "--format-version", "1", "--manifest-path", str(crate / "Cargo.toml")],
                               capture_output=True, text=True, cwd=t)
        if r.returncode != 0:
            self.skipTest("crates AUSENTE offline (OPERATOR cargo fetch pending): no lock can exist yet")
        self.assertTrue((ROOT / "rust/visioncore_native/Cargo.lock").is_file(), "crates present but Cargo.lock not committed")

    def test_no_cargo_gives_ausente_rc3_and_operator_line(self):
        with tempfile.TemporaryDirectory() as t:
            rc, rec = build_native.build(out=Path(t) / "o", env={"PATH": t, "HOME": t})
            self.assertEqual(rc, 3)
            self.assertEqual(rec["status"], "AUSENTE")
            self.assertIn("cargo AUSENTE", rec["reason"])
            self.assertIn(".cargo/bin", rec["operator"])
            self.assertEqual(json.loads((Path(t) / "o/receipt.json").read_text())["status"], "AUSENTE")

    def test_output_only_under_out_and_receipt_consistent(self):
        before = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "rust").rglob("*"))
        with tempfile.TemporaryDirectory() as t:
            rc, rec = build_native.build(out=Path(t) / "o")
            self.assertIn(rc, (0, 3))
            self.assertEqual(rc == 0, rec["status"] == "OK")
            if rc == 0:
                self.assertEqual(len(rec["sha256"]), 64)
            else:
                self.assertIn("AUSENTE", rec["reason"])
        after = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "rust").rglob("*"))
        self.assertEqual(before, after)

    def test_main_rc3_prints_operator(self):
        with tempfile.TemporaryDirectory() as t:
            import contextlib
            import io
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = build_native.main(["--out", t])
            if rc == 3:
                self.assertIn("OPERATOR:", err.getvalue())


def _toolchain_ok():
    if not RECEIPT.is_file():
        return False, "no receipt (run tools/build_native.py): %s" % RECEIPT
    rec = json.loads(RECEIPT.read_text())
    return rec.get("status") == "OK", "toolchain AUSENTE per receipt %s: %s" % (RECEIPT, rec.get("reason"))


def _load():
    spec = importlib.util.spec_from_file_location("visioncore_native", ROOT / "build/native" / build_native.LIB)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class NativeModule(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ok, why = _toolchain_ok()
        if not ok:
            raise unittest.SkipTest(why)
        cls.m = _load()

    def test_layout_check_matches_python(self):
        lc = self.m.layout_check()
        self.assertEqual(lc["itemsize"], desc.TILE_DTYPE.itemsize)
        self.assertEqual([tuple(x) for x in lc["fields"]], py_fields(desc.TILE_DTYPE))
        self.m.layout_check(descr(desc.TILE_DTYPE))
        bad = descr(desc.TILE_DTYPE)
        bad["fields"][-1] = (bad["fields"][-1][0], bad["fields"][-1][1] + 1) + tuple(bad["fields"][-1][2:])
        with self.assertRaises(ValueError):
            self.m.layout_check(bad)

    def test_layout_check_rejects_sign_flip(self):  # judge F8
        with self.assertRaises(ValueError):
            self.m.layout_check(descr(sign_flipped(desc.TILE_DTYPE)))

    def test_panic_probe_raises(self):
        with self.assertRaises(RuntimeError):
            self.m.panic_probe()

    def test_abi_version_crossing(self):
        self.assertEqual(self.m.abi_version(), 1)
        f = self.m.abi_version
        t = []
        for _ in range(2000):
            a = time.perf_counter_ns()
            f()
            t.append(time.perf_counter_ns() - a)
        t.sort()
        self.assertLess(t[len(t) // 2], 1000)  # median < 1 us

    def test_two_builds_same_sha(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            ra = build_native.build(out=Path(a))[1]
            rb = build_native.build(out=Path(b))[1]
            self.assertEqual(ra["sha256"], rb["sha256"])


if __name__ == "__main__":
    unittest.main()
