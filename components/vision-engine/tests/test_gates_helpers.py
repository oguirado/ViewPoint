import ast
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATES_PY = ROOT / "tools" / "gates.py"

spec = importlib.util.spec_from_file_location("gates", GATES_PY)
gates = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gates)


class TestGatesHelpers(unittest.TestCase):

    def test_pyfiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg_dir = root / "visioncore"
            pkg_dir.mkdir()
            (pkg_dir / "a.py").write_text("x = 1\n", encoding="utf-8")
            sub_dir = pkg_dir / "sub"
            sub_dir.mkdir()
            (sub_dir / "b.py").write_text("y = 2\n", encoding="utf-8")
            cache_dir = pkg_dir / "__pycache__"
            cache_dir.mkdir()
            (cache_dir / "c.py").write_text("z = 3\n", encoding="utf-8")

            # Positive case: retrieves sorted .py files excluding __pycache__
            res = gates.pyfiles(root, "visioncore")
            expected = [pkg_dir / "a.py", sub_dir / "b.py"]
            self.assertEqual(res, expected)

            # Negative case: returns [] for non-existent package directory
            res_missing = gates.pyfiles(root, "nonexistent")
            self.assertEqual(res_missing, [])

    def test_stem_of(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vc_file = root / "visioncore" / "grid.py"

            # Positive case: extracts stem relative to visioncore
            self.assertEqual(gates.stem_of(root, vc_file), "grid")

            # Negative case: raises ValueError if file is not under visioncore directory
            other_file = root / "vmem" / "grid.py"
            with self.assertRaises(ValueError):
                gates.stem_of(root, other_file)

    def test_imports_of(self):
        code = (
            "import sys\n"
            "import numpy as np\n"
            "import visioncore.grid\n"
            "from visioncore import params, grid\n"
            "from . import internal\n"
            "from .sub import helper\n"
            "from vmem import layout\n"
        )
        tree = ast.parse(code)

        # Positive case: yields expected imports tuple (top, sub, lineno)
        imports = list(gates.imports_of(tree, level0_pkg="visioncore"))
        expected = [
            ("sys", None, 1),
            ("numpy", None, 2),
            ("visioncore", "grid", 3),
            ("visioncore", "params", 4),
            ("visioncore", "grid", 4),
            ("visioncore", "internal", 5),
            ("visioncore", "sub", 6),
            ("vmem", None, 7),
        ]
        self.assertEqual(imports, expected)

        # Negative case: AST without import statements yields []
        empty_tree = ast.parse("x = 10\ny = 20\n")
        self.assertEqual(list(gates.imports_of(empty_tree)), [])

    def test_is_stdlib(self):
        # Positive case: stdlib modules return True
        self.assertTrue(gates.is_stdlib("sys"))
        self.assertTrue(gates.is_stdlib("ast"))
        self.assertTrue(gates.is_stdlib("json"))

        # Negative case: non-stdlib or unknown modules return False
        self.assertFalse(gates.is_stdlib("numpy"))
        self.assertFalse(gates.is_stdlib("nonexistent_module_xyz"))

    def test_anchors_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vc_dir = root / "visioncore"
            vc_dir.mkdir()

            # Positive case: valid anchor
            (vc_dir / "a.py").write_text("# @anchor:vc.s0.grid\n", encoding="utf-8")
            found, errs = gates.anchors_in(root)
            self.assertEqual(found, {"vc.s0.grid": "visioncore/a.py:1"})
            self.assertEqual(errs, [])

            # Negative case 1: bad anchor grammar error code E_GATE_ANCHOR
            (vc_dir / "bad.py").write_text("# @anchor:bad_grammar\n", encoding="utf-8")
            found_bad, errs_bad = gates.anchors_in(root)
            self.assertIn(("E_GATE_ANCHOR", "visioncore/bad.py:1 bad anchor grammar"), errs_bad)

            # Negative case 2: duplicate anchor error code E_GATE_ANCHOR
            (vc_dir / "dup.py").write_text("# @anchor:vc.s0.grid\n", encoding="utf-8")
            found_dup, errs_dup = gates.anchors_in(root)
            self.assertTrue(any(code == "E_GATE_ANCHOR" and "duplicate anchor" in msg for code, msg in errs_dup))

    def test_header_of(self):
        # Positive case: multi-line # @module header parsed buffer
        code = (
            "# @module {\"layer\": 0,\n"
            "#   \"deps\": [\"vc.ids\"]}\n"
            "x = 1\n"
        )
        hdr = gates.header_of(code)
        self.assertEqual(hdr, '{"layer": 0,"deps": ["vc.ids"]}')

        # Negative case: returns None when header is missing
        code_no_hdr = "x = 1\n# Just a comment\n"
        self.assertIsNone(gates.header_of(code_no_hdr))

    def test_exempt_ranges(self):
        code = (
            "def normal():\n"
            "    pass\n"
            "\n"
            "# @anchor:vc.s1.isqrt\n"
            "def exempt_fn():\n"
            "    pass\n"
        )
        tree = ast.parse(code)

        # Positive case: detects line range for function with exempt anchor
        rngs = gates.exempt_ranges(code, tree)
        self.assertEqual(len(rngs), 1)
        self.assertEqual(rngs[0], (5, 6))

        # Negative case: returns [] when exempt anchor is absent
        code_no_anchor = "def foo():\n    pass\n"
        tree_no_anchor = ast.parse(code_no_anchor)
        self.assertEqual(gates.exempt_ranges(code_no_anchor, tree_no_anchor), [])

    def test_load_params(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vc_dir = root / "visioncore"
            vc_dir.mkdir()

            # Positive case: valid PARAMS dictionary loaded via AST
            params_py = vc_dir / "params.py"
            params_py.write_text("PARAMS = {'P_GRID_SIZE': 64}\n", encoding="utf-8")
            p_dict, err = gates.load_params(root)
            self.assertEqual(p_dict, {"P_GRID_SIZE": 64})
            self.assertIsNone(err)

            # Negative case 1: params.py missing
            params_py.unlink()
            p_dict_missing, err_missing = gates.load_params(root)
            self.assertIsNone(p_dict_missing)
            self.assertEqual(err_missing, "params.py missing")

            # Negative case 2: params.py not AST-evaluable and raising exception during module import
            params_py.write_text("PARAMS = dict()\nraise RuntimeError('fail import')\n", encoding="utf-8")
            p_dict_bad, err_bad = gates.load_params(root)
            self.assertIsNone(p_dict_bad)
            self.assertTrue(err_bad.startswith("params.py not loadable: fail import"))

    def test_run_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vc_dir = root / "visioncore"
            vc_dir.mkdir()
            (vc_dir / "a.py").write_text("x = 1\n", encoding="utf-8")
            tools_dir = root / "tools"
            tools_dir.mkdir()
            (tools_dir / "gates.py").touch()

            # Positive case: cheap gates pass on fake tree
            res = gates.run_gates(root, names={"g_canon", "g_pure"})
            self.assertIn("g_canon", res)
            self.assertIn("g_pure", res)
            self.assertEqual(res["g_canon"], [])
            self.assertEqual(res["g_pure"], [])

            # Negative case: running gate that finds failure (e.g., g_pure on impure import)
            (tools_dir / "impure.py").write_text("import random\n", encoding="utf-8")
            res_fail = gates.run_gates(root, names={"g_pure"})
            self.assertEqual(res_fail["g_pure"], [("E_GATE_PURE", "tools/impure.py:1 imports random")])

    def test_receipt(self):
        # Positive case: all gates pass
        res_pass = {"g_pure": [], "g_hint_neutral": []}
        failed, rc_str = gates.receipt(res_pass)
        self.assertEqual(failed, [])
        self.assertEqual(rc_str, "VG:PASS NA:g_hint_neutral")

        # Negative case: one or more gates fail
        res_fail = {"g_pure": [("E_GATE_PURE", "error")], "g_canon": []}
        failed, rc_str = gates.receipt(res_fail)
        self.assertEqual(failed, ["g_pure"])
        self.assertEqual(rc_str, "VG:FAIL:g_pure")

    def test_canon(self):
        # Positive case: canonical json output with trailing newline
        self.assertEqual(gates.canon({"b": 1, "a": 2}), '{"a":2,"b":1}\n')

        # Negative case: non-serializable object raises TypeError
        with self.assertRaises(TypeError):
            gates.canon(object())

    def test_rel(self):
        root = Path("/tmp/fake_root")
        p = root / "visioncore" / "grid.py"

        # Positive case: relative path string
        self.assertEqual(gates.rel(root, p), "visioncore/grid.py")

        # Negative case: path outside root raises ValueError
        with self.assertRaises(ValueError):
            gates.rel(root, Path("/other/dir/file.py"))

    def test_load_py(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "mod.py"
            tmp_path.write_text("VAL = 42\n", encoding="utf-8")

            # Positive case: loads module from file path
            mod = gates.load_py(tmp_path, "fake_mod")
            self.assertEqual(mod.VAL, 42)

            # Negative case: non-existent module raises FileNotFoundError or Exception
            with self.assertRaises(Exception):
                gates.load_py(Path(tmp) / "nonexistent.py", "nonexistent_mod")


if __name__ == "__main__":
    unittest.main()
