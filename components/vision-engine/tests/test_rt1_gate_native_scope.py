"""RT1-L2: g_native_scope catches planted mentions/objects in a temp copy, passes on the real tree."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("_t_gates", ROOT / "tools" / "gates.py")
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)


class TestNativeScope(unittest.TestCase):
    def test_registered_and_real_tree_clean(self):
        self.assertIn("g_native_scope", [n for n, _ in G.GATES])
        self.assertEqual(G.g_native_scope(ROOT), [])

    def test_planted(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as t:
            r = Path(t)
            for d in ("visioncore", "vmem", "vnative"):
                (r / d).mkdir()
            (r / "visioncore" / "a.py").write_text("X = 1\n")
            self.assertEqual(G.g_native_scope(r), [])
            cases = [("visioncore/a.py", "import vnative\n"), ("vmem/b.py", "# VISIONCORE_BACKEND\n"),
                     ("visioncore/data.txt", "see vnative\n")]
            for rel_, txt in cases:
                p = r / rel_
                old = p.read_text() if p.exists() else None
                p.write_text(txt)
                self.assertEqual(len(G.g_native_scope(r)), 1, rel_)
                p.unlink() if old is None else p.write_text(old)
            for d in ("visioncore", "vmem", "vnative"):
                (r / d / "x.so").write_bytes(b"\x7fELF")
                self.assertEqual(len(G.g_native_scope(r)), 1, d)
                (r / d / "x.so").unlink()
            (r / "vnative" / "ok.py").write_text("import vnative\nVISIONCORE_BACKEND = 1\n")  # allowed inside vnative
            self.assertEqual(G.g_native_scope(r), [])


class TestVnativeCoverage(unittest.TestCase):  # adv F-05
    def setUp(self):
        self._t = tempfile.TemporaryDirectory(dir=ROOT / "build")
        self.r = Path(self._t.name)
        for d in ("vnative", "vadapt", "tools", "tests", "build/native"):
            (self.r / d).mkdir(parents=True)
        (self.r / "tools" / "gates.py").write_text("X = 1\n")

    def tearDown(self):
        self._t.cleanup()

    def test_src_pkgs_includes_vnative(self):
        self.assertIn("vnative", G.SRC_PKGS)

    def test_size_layer_pure_cover_vnative(self):
        (self.r / "vnative" / "big.py").write_text("x = 1\n" * (G.MAX_LINES + 1))
        self.assertEqual([c for c, _ in G.g_size(self.r)], ["E_GATE_SIZE"])
        (self.r / "vnative" / "big.py").unlink()
        for src in ("import vmem\n", "import requests\n", "from vadapt import ramws\n"):
            (self.r / "vnative" / "m.py").write_text(src)
            self.assertEqual([c for c, _ in G.g_layer(self.r)], ["E_GATE_LAYER_PKG"], src)
        (self.r / "vnative" / "m.py").write_text("import numpy\nimport threading\nfrom visioncore import desc\nfrom . import backend\n")
        self.assertEqual(G.g_layer(self.r), [])
        self.assertEqual(G.g_pure(self.r), [])
        (self.r / "vnative" / "m.py").write_text("import time\n")
        self.assertEqual([c for c, _ in G.g_pure(self.r)], ["E_GATE_PURE"])

    def test_anchor_grammar_covers_vnative(self):
        (self.r / "vnative" / "m.py").write_text("x = 1  # @anchor:bogus\n")
        self.assertEqual([c for c, _ in G.g_anchor(self.r)], ["E_GATE_ANCHOR"])

    def test_dlopen_and_so_outside_vnative_are_banned(self):
        cases = [("vadapt/a.py", "import ctypes\nctypes.CDLL('x')\n"), ("tools/a.py", "from ctypes import cdll\n"),
                 ("tests/a.py", "from ctypes import CDLL as L\n"), ("tools/b.py", "import ctypes as c\nc.cdll.LoadLibrary('x')\n"),
                 ("vadapt/a.so", "\x7fELF"), ("tools/lib.so.1", "\x7fELF"), ("rust/x.so", "\x7fELF")]
        for rel_, txt in cases:
            p = self.r / rel_
            p.parent.mkdir(exist_ok=True)
            p.write_text(txt)
            self.assertGreaterEqual(len(G.g_native_scope(self.r)), 1, rel_)
            p.unlink()
            self.assertEqual(G.g_native_scope(self.r), [], rel_)

    def test_allowed_places(self):
        (self.r / "vnative" / "ld.py").write_text("import ctypes\nctypes.CDLL('x')\n")
        (self.r / "build/native/visioncore_native.abi3.so").write_bytes(b"\x7fELF")
        (self.r / "tools" / "ok.py").write_text("import ctypes\nprint(ctypes.sizeof(ctypes.c_int))\n")  # ctypes alone is not dlopen
        self.assertEqual(G.g_native_scope(self.r), [])


if __name__ == "__main__":
    unittest.main()
