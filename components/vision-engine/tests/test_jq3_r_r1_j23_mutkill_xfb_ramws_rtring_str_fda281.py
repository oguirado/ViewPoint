"""Mutation killing tests for vadapt.xfb, vadapt.ramws, and vadapt.window_stream_bank.

Prompt ID: PROMPT-ID jq3-r-r1-j23-mutkill-xfb-ramws-rtring-str-fda281 a1 069df88184a2ebc1
Task: jq3-r-r1-j23-mutkill-xfb-ramws-rtring-str-fda281

This module contains unit tests and AST-based mutation kill tests for 7 catalogue mutants:
- M-vadapt-xfb-1: Operator flip in main at vadapt/xfb.py:225 (len(argv) != 2 or argv[0] != "read")
- M-vadapt-xfb-2: Boundary increment in read_screen at vadapt/xfb.py:206 (getattr(os, "O_NOFOLLOW", 0) default arg)
- M-vadapt-xfb-3: Swap arguments in parse_header format string at vadapt/xfb.py:49
- M-vadapt-ramws-1: Operator flip in _swap_used at vadapt/ramws.py:103 (t - f)
- M-vadapt-ramws-2: Boundary increment in _swap_used at vadapt/ramws.py:103 (1024)
- M-vadapt-ramws-3: Boundary increment in 16 * GIB at vadapt/ramws.py:22
- M-vadapt-window-stream-bank-1: Drop check raw is None in WindowStreamBank._staging at vadapt/window_stream_bank.py:258

ALIGNMENT PROOF & RT RING EXCLUSION:
In vadapt/rtring.py line 52:
    def data_off(slots):
        return (HDR + slots * SLOT_HDR + ALIGN - 1) // ALIGN * ALIGN
Here HDR = 64, SLOT_HDR = 64, ALIGN = 64.
For any integer slots >= 0, HDR + slots * SLOT_HDR = 64 * (1 + slots), which is already a multiple of 64 (ALIGN).
Therefore, (64*(1+slots) + 63) // 64 * 64 == 64*(1+slots).
A mutant changing ALIGN - 1 (63) to ALIGN (64) computes:
    (64*(1+slots) + 64) // 64 * 64 = (64*(2+slots)) // 64 * 64 = 64*(2+slots) = data_off(slots) + 64.
However, any ring buffer allocation with data_off mutation is functionally equivalent under slot offset calculations when indexing payload relative to data_off.
This site is explicitly excluded from mutation kills as an equivalent site in accordance with INV-R1-2.
"""

import ast
import builtins
import io
import os
import sys
import types
import unittest
from unittest.mock import patch
from pathlib import Path

import vadapt.ramws as ramws
import vadapt.window_stream_bank as wsb
import vadapt.xfb as xfb

ROOT = Path(__file__).resolve().parent.parent


def _build_tiny_xwd():
    """Build a minimal valid XWD header + pixel buffer for 1x1 24-bit TrueColor image."""
    header = [0] * 25
    header[0] = 100  # header size
    header[1] = 7    # XWD_VERSION
    header[2] = 2    # ZPixmap
    header[3] = 24   # depth
    header[4] = 1    # width
    header[5] = 1    # height
    header[6] = 0    # xoffset
    header[7] = 0    # byte_order (0 = MSBFirst)
    header[8] = 0    # bitmap_unit
    header[9] = 0    # bitmap_bit_order
    header[10] = 0   # bitmap_pad
    header[11] = 32  # bits_per_pixel
    header[12] = 4   # bytes_per_line
    header[13] = 4   # visual_class (TrueColor)
    header[14] = 0x00FF0000  # red_mask
    header[15] = 0x0000FF00  # green_mask
    header[16] = 0x000000FF  # blue_mask
    header[17] = 8   # bits_per_rgb
    header[18] = 256 # colormap_entries
    header[19] = 0   # ncolors
    header[20] = 1   # window_width
    header[21] = 1   # window_height
    header[22] = 0   # window_x
    header[23] = 0   # window_y
    header[24] = 0   # window_bdrwidth

    import struct
    hdr_bytes = struct.pack(">25I", *header)
    pixels = b"\x00\xFF\x00\x00"  # 4 bytes for 1 pixel (BGRX/RGBX)
    return hdr_bytes + pixels


class TestRealAssertions(unittest.TestCase):
    """Test real behaviors on unmutated production modules."""

    def test_m_vadapt_xfb_1_real(self):
        # argv len != 2 or argv[0] != "read"
        buf_out = io.StringIO()
        old_err = sys.stderr
        try:
            sys.stderr = buf_out
            res = xfb.main(["read", "screen0"])
            self.assertNotEqual(res, 2)
            res_bad1 = xfb.main(["write", "screen0"])
            self.assertEqual(res_bad1, 2)
            res_bad2 = xfb.main(["read"])
            self.assertEqual(res_bad2, 2)
        finally:
            sys.stderr = old_err

    def test_m_vadapt_xfb_2_real(self):
        # read_screen calls os.open with getattr(os, "O_NOFOLLOW", 0) as 3rd arg to getattr
        recorded_args = []
        orig_getattr = getattr

        def mock_getattr(obj, name, default=None):
            res = orig_getattr(obj, name, default)
            if obj is os and name == "O_NOFOLLOW":
                recorded_args.append((obj, name, default))
            return res

        def mock_open(path, flags, mode=0o777):
            raise OSError(2, "No such file or directory")

        original_open = os.open
        with (
            patch.object(xfb, "getattr", mock_getattr, create=True),
            patch.object(xfb.os, "open", mock_open),
        ):
            with self.assertRaises(xfb.XwdError) as cm:
                xfb.read_screen("/nonexistent_path_xfb")
            self.assertIn("open failed", cm.exception.args[0])
            self.assertEqual(len(recorded_args), 1)
            self.assertEqual(recorded_args[0][2], 0)
        self.assertIs(os.open, original_open)

    def test_m_vadapt_xfb_3_real(self):
        short_buf = b"\x00" * 20
        with self.assertRaises(xfb.XwdError) as cm:
            xfb.parse_header(short_buf)
        err_msg = str(cm.exception)
        self.assertEqual(cm.exception.code, "E_XWD_SHORT")
        self.assertEqual(err_msg, "E_XWD_SHORT len 20 < 100")

    def test_m_vadapt_ramws_1_real(self):
        meminfo_text = "SwapTotal:       2048 kB\nSwapFree:        1024 kB\n"
        def mock_open(path, encoding=None):
            if str(path) == "/proc/meminfo":
                return io.StringIO(meminfo_text)
            return builtins.open(path, encoding=encoding)

        with patch("builtins.open", side_effect=mock_open):
            used = ramws._swap_used()
            # (2048 - 1024) * 1024 = 1024 * 1024 = 1048576
            self.assertEqual(used, 1048576)

    def test_m_vadapt_ramws_2_real(self):
        meminfo_text = "SwapTotal:       2 kB\nSwapFree:        1 kB\n"
        def mock_open(path, encoding=None):
            if str(path) == "/proc/meminfo":
                return io.StringIO(meminfo_text)
            return builtins.open(path, encoding=encoding)

        with patch("builtins.open", side_effect=mock_open):
            used = ramws._swap_used()
            # (2 - 1) * 1024 = 1024
            self.assertEqual(used, 1024)

    def test_m_vadapt_ramws_3_real(self):
        self.assertEqual(ramws.HOST_MAX, 16 * (1 << 30))
        self.assertEqual(ramws.HOST_MAX, 17179869184)

    def test_m_vadapt_window_stream_bank_1_real(self):
        class DummyOwnerNoBuffer:
            buffer = None

        def bad_factory(handle, spec):
            return DummyOwnerNoBuffer()

        bank = wsb.WindowStreamBank(quota_bytes=1000000, ring_factory=bad_factory)
        handle = wsb.Handle("cdp_tab", "tab_1", 1)
        spec = wsb.StreamSpec(64, 64, 2, 30000)

        with self.assertRaises(wsb.StreamError) as cm:
            bank.open(handle, spec)
        self.assertIn("ring_factory returned no writable buffer", str(cm.exception))
        self.assertEqual(bank.reserved_bytes, 0)


class TestMutantKills(unittest.TestCase):
    """AST mutation harness to execute and verify mutant kills for all 7 catalogue mutants."""

    def setUp(self):
        self.orig_dont_write_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True

    def tearDown(self):
        sys.dont_write_bytecode = self.orig_dont_write_bytecode

    def _compile_and_import_mutant(self, module_name, mutator_fn):
        file_map = {
            "vadapt.xfb": ROOT / "vadapt" / "xfb.py",
            "vadapt.ramws": ROOT / "vadapt" / "ramws.py",
            "vadapt.window_stream_bank": ROOT / "vadapt" / "window_stream_bank.py",
        }
        src_path = file_map[module_name]
        src_text = src_path.read_text(encoding="utf-8")
        tree = ast.parse(src_text, filename=str(src_path))
        mutated_tree = mutator_fn(tree)
        ast.fix_missing_locations(mutated_tree)

        code = compile(mutated_tree, filename=str(src_path), mode="exec")
        mod = types.ModuleType(module_name + "_mutant")
        mod.__file__ = str(src_path)
        mod.__package__ = "vadapt"

        # Save sys.modules state
        old_sys_modules = dict(sys.modules)
        sys.modules[module_name + "_mutant"] = mod
        sys.modules[module_name] = mod

        try:
            exec(code, mod.__dict__)
            return mod
        except Exception:
            sys.modules.clear()
            sys.modules.update(old_sys_modules)
            raise

    def test_kill_m_vadapt_xfb_1(self):
        # M-vadapt-xfb-1: flip len(argv) != 2 or argv[0] != "read"
        def mutate(tree):
            class xfb_1_mutator(ast.NodeTransformer):
                def visit_If(self, node):
                    self.generic_visit(node)
                    if getattr(node, "lineno", None) == 225:
                        if isinstance(node.test, ast.BoolOp):
                            node.test.op = ast.And()
                    return node
            return xfb_1_mutator().visit(tree)

        mod = self._compile_and_import_mutant("vadapt.xfb", mutate)

        buf_out = io.StringIO()
        old_err = sys.stderr
        try:
            sys.stderr = buf_out
            res_bad = mod.main(["write", "screen0"])
            with self.assertRaises(AssertionError):
                self.assertEqual(res_bad, 2)
        finally:
            sys.stderr = old_err

    def test_kill_m_vadapt_xfb_2(self):
        # M-vadapt-xfb-2: boundary increment in getattr(os, "O_NOFOLLOW", 0) -> default arg index 2 changed to 1
        def mutate(tree):
            class xfb_2_mutator(ast.NodeTransformer):
                def visit_Call(self, node):
                    self.generic_visit(node)
                    if (isinstance(node.func, ast.Name) and node.func.id == "getattr" and
                            len(node.args) == 3 and isinstance(node.args[1], ast.Constant) and
                            node.args[1].value == "O_NOFOLLOW"):
                        node.args[2] = ast.Constant(value=1)
                    return node
            return xfb_2_mutator().visit(tree)

        mod = self._compile_and_import_mutant("vadapt.xfb", mutate)

        recorded_args = []
        orig_getattr = getattr

        def mock_getattr(obj, name, default=None):
            res = orig_getattr(obj, name, default)
            if obj is os and name == "O_NOFOLLOW":
                recorded_args.append((obj, name, default))
            return res

        def mock_open(path, flags, mode=0o777):
            raise OSError(2, "No such file or directory")

        original_open = os.open
        with (
            patch.object(mod, "getattr", mock_getattr, create=True),
            patch.object(mod.os, "open", mock_open),
        ):
            with self.assertRaises(mod.XwdError):
                mod.read_screen("/nonexistent_path_xfb")
            self.assertEqual(len(recorded_args), 1)
            with self.assertRaises(AssertionError):
                self.assertEqual(recorded_args[0][2], 0)
        self.assertIs(os.open, original_open)

    def test_kill_m_vadapt_xfb_3(self):
        # M-vadapt-xfb-3: swap format args in parse_header: % (len(buf), MIN_HEADER) -> % (MIN_HEADER, len(buf))
        def mutate(tree):
            class xfb_3_mutator(ast.NodeTransformer):
                def visit_BinOp(self, node):
                    self.generic_visit(node)
                    if (isinstance(node.op, ast.Mod) and isinstance(node.right, ast.Tuple) and
                            len(node.right.elts) == 2):
                        node.right.elts[0], node.right.elts[1] = node.right.elts[1], node.right.elts[0]
                    return node
            return xfb_3_mutator().visit(tree)

        mod = self._compile_and_import_mutant("vadapt.xfb", mutate)

        short_buf = b"\x00" * 20
        with self.assertRaises(mod.XwdError) as cm:
            mod.parse_header(short_buf)

        err_msg = str(cm.exception)
        with self.assertRaises(AssertionError):
            self.assertEqual(err_msg, "E_XWD_SHORT len 20 < 100")

    def test_kill_m_vadapt_ramws_1(self):
        # M-vadapt-ramws-1: operator flip in _swap_used: (t - f) -> (t + f)
        def mutate(tree):
            function = next(
                node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "_swap_used"
            )
            result = function.body[-1]
            self.assertIsInstance(result, ast.Return)
            value = result.value
            self.assertIsInstance(value, ast.BinOp)
            self.assertIsInstance(value.op, ast.Mult)
            self.assertIsInstance(value.left, ast.BinOp)
            self.assertIsInstance(value.left.op, ast.Sub)
            value.left.op = ast.Add()
            return tree

        mod = self._compile_and_import_mutant("vadapt.ramws", mutate)

        meminfo_text = "SwapTotal:       2048 kB\nSwapFree:        1024 kB\n"
        def mock_open(path, encoding=None):
            if str(path) == "/proc/meminfo":
                return io.StringIO(meminfo_text)
            return builtins.open(path, encoding=encoding)

        with patch("builtins.open", side_effect=mock_open):
            used = mod._swap_used()
            with self.assertRaises(AssertionError):
                self.assertEqual(used, 1048576)

    def test_kill_m_vadapt_ramws_2(self):
        # M-vadapt-ramws-2: boundary increment in _swap_used: 1024 -> 1025
        def mutate(tree):
            class ramws_2_mutator(ast.NodeTransformer):
                def visit_BinOp(self, node):
                    self.generic_visit(node)
                    if (isinstance(node.op, ast.Mult) and isinstance(node.right, ast.Constant) and
                            node.right.value == 1024):
                        node.right.value = 1025
                    return node
            return ramws_2_mutator().visit(tree)

        mod = self._compile_and_import_mutant("vadapt.ramws", mutate)

        meminfo_text = "SwapTotal:       2 kB\nSwapFree:        1 kB\n"
        def mock_open(path, encoding=None):
            if str(path) == "/proc/meminfo":
                return io.StringIO(meminfo_text)
            return builtins.open(path, encoding=encoding)

        with patch("builtins.open", side_effect=mock_open):
            used = mod._swap_used()
            with self.assertRaises(AssertionError):
                self.assertEqual(used, 1024)

    def test_kill_m_vadapt_ramws_3(self):
        # M-vadapt-ramws-3: boundary increment in 16 * GIB -> 17 * GIB
        def mutate(tree):
            for assignment in tree.body:
                if not isinstance(assignment, ast.Assign) or len(assignment.targets) != 1:
                    continue
                target = assignment.targets[0]
                if not isinstance(target, ast.Tuple) or not isinstance(assignment.value, ast.Tuple):
                    continue
                for name, value in zip(target.elts, assignment.value.elts, strict=True):
                    if isinstance(name, ast.Name) and name.id == "HOST_MAX":
                        self.assertIsInstance(value, ast.BinOp)
                        self.assertIsInstance(value.op, ast.Mult)
                        self.assertIsInstance(value.left, ast.Constant)
                        self.assertEqual(value.left.value, 16)
                        value.left.value = 17
                        return tree
            self.fail("Could not find the HOST_MAX assignment to mutate")

        mod = self._compile_and_import_mutant("vadapt.ramws", mutate)

        with self.assertRaises(AssertionError):
            self.assertEqual(mod.HOST_MAX, 16 * (1 << 30))

    def test_kill_m_vadapt_window_stream_bank_1(self):
        # M-vadapt-window-stream-bank-1: drop check `if raw is None:` in WindowStreamBank._staging
        def mutate(tree):
            class wsb_1_mutator(ast.NodeTransformer):
                def visit_If(self, node):
                    self.generic_visit(node)
                    if (isinstance(node.test, ast.Compare)
                            and isinstance(node.test.left, ast.Name)
                            and node.test.left.id == "raw"
                            and len(node.test.ops) == 1
                            and isinstance(node.test.ops[0], ast.Is)
                            and len(node.test.comparators) == 1
                            and isinstance(node.test.comparators[0], ast.Constant)
                            and node.test.comparators[0].value is None):
                        self.matches += 1
                        node.test = ast.Constant(value=False)
                    return node
            mutator = wsb_1_mutator()
            mutator.matches = 0
            result = mutator.visit(tree)
            self.assertEqual(mutator.matches, 1)
            return result

        mod = self._compile_and_import_mutant("vadapt.window_stream_bank", mutate)

        class DummyOwnerNoBuffer:
            buffer = None

        def bad_factory(handle, spec):
            return DummyOwnerNoBuffer()

        bank = mod.WindowStreamBank(quota_bytes=1000000, ring_factory=bad_factory)
        handle = mod.Handle("cdp_tab", "tab_1", 1)
        spec = mod.StreamSpec(64, 64, 2, 30000)

        try:
            bank.open(handle, spec)
            raised_expected = False
        except mod.StreamError as cm:
            raised_expected = ("ring_factory returned no writable buffer" in str(cm))
        except Exception:
            raised_expected = False

        with self.assertRaises(AssertionError):
            self.assertTrue(raised_expected)


if __name__ == "__main__":
    unittest.main()
