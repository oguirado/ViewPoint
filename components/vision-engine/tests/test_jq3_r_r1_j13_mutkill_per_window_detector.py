"""Offline unit and mutation tests for PerWindowDetector (Task jq3-r-r1-j13-mutkill-per-window-detector)."""

import ast
from concurrent.futures import ThreadPoolExecutor
import importlib
import os
import sys
import types
import unittest

import numpy as np

from visioncore import desc
from visioncore.grid import Grid
import eval.v2x.per_window_detector as pwd_real
from eval.v2x.per_window_detector import (
    ChangeResult, DetectorInputError, Handle, MissingEvidenceError,
    PerWindowDetector,
)


def frame(value, size=16):
    return np.full((size, size, 3), value, dtype=np.uint8)


def baseline(rgb, cls=0):
    g = Grid(rgb.shape[1], rgb.shape[0])
    tiles, _ = desc.describe_padded(g.pad(rgb), g, lazy_xh=True)
    tiles = tiles.copy()
    tiles["cls"] = cls
    tiles["mv_dx"] = 0
    tiles["mv_dy"] = 0
    tiles["xh64"] = 0
    return tiles


def load_source():
    src_path = os.path.join(os.path.dirname(__file__), "..", "eval", "v2x", "per_window_detector.py")
    with open(src_path, "r", encoding="utf-8") as f:
        return f.read()


SOURCE_TEXT = load_source()


def compile_mutant(tree, name):
    ast.fix_missing_locations(tree)
    old_bytecode_flag = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        code = compile(tree, f"<mutant_{name}>", "exec")
        mod_name = f"pwd_mutant_{name}"
        mod = types.ModuleType(mod_name)
        mod.__file__ = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "eval", "v2x", "per_window_detector.py")
        )
        mod.__package__ = "eval.v2x"
        sys.modules[mod_name] = mod
        exec(code, mod.__dict__)
        return mod
    finally:
        sys.dont_write_bytecode = old_bytecode_flag


def cleanup_mutant(mod):
    if mod is not None and hasattr(mod, "__name__") and mod.__name__ in sys.modules:
        del sys.modules[mod.__name__]


# --- AST Transformers for M1..M7 ---

class M1Transformer(ast.NodeTransformer):
    """M1: operator flip in PerWindowDetector._step nested gm_call (ops + second -> ops - second)."""
    def __init__(self):
        self.count = 0

    def visit_BinOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Add) and isinstance(node.left, ast.Name) and node.left.id == "ops":
            if isinstance(node.right, ast.Name) and node.right.id == "second":
                self.count += 1
                return ast.BinOp(left=node.left, op=ast.Sub(), right=node.right)
        return node


class M2Transformer(ast.NodeTransformer):
    """M2: operator flip in PerWindowDetector.update at line 154 (or -> and)."""
    def __init__(self):
        self.count = 0

    def visit_If(self, node):
        self.generic_visit(node)
        if isinstance(node.test, ast.BoolOp) and isinstance(node.test.op, ast.Or):
            str_test = ast.unparse(node.test)
            if "TILE_DTYPE" in str_test and "morton" in str_test:
                self.count += 1
                return ast.If(
                    test=ast.BoolOp(op=ast.And(), values=node.test.values),
                    body=node.body,
                    orelse=node.orelse,
                )
        return node


class M3Transformer(ast.NodeTransformer):
    """M3: boundary increment in PerWindowDetector._step (output['mv_dx'][fm] = 0 -> = 1)."""
    def __init__(self):
        self.count = 0

    def visit_Assign(self, node):
        self.generic_visit(node)
        str_target = ast.unparse(node.targets[0]) if node.targets else ""
        if str_target == "output['mv_dx'][fm]" and isinstance(node.value, ast.Constant) and node.value.value == 0:
            self.count += 1
            return ast.Assign(targets=node.targets, value=ast.Constant(value=1))
        return node


class M4Transformer(ast.NodeTransformer):
    """M4: boundary increment in PerWindowDetector._step (st.clean_run + 1 if clean else 0 -> else 1)."""
    def __init__(self):
        self.count = 0

    def visit_IfExp(self, node):
        self.generic_visit(node)
        str_test = ast.unparse(node.test)
        if str_test == "clean" and isinstance(node.orelse, ast.Constant) and node.orelse.value == 0:
            self.count += 1
            return ast.IfExp(test=node.test, body=node.body, orelse=ast.Constant(value=1))
        return node


class M5Transformer(ast.NodeTransformer):
    """M5: drop check in PerWindowDetector.__init__ (veto is not None and not callable(veto))."""
    def __init__(self):
        self.count = 0

    def visit_If(self, node):
        self.generic_visit(node)
        str_test = ast.unparse(node.test)
        if "veto is not None" in str_test and "callable(veto)" in str_test:
            self.count += 1
            return ast.If(test=ast.Constant(value=False), body=node.body, orelse=node.orelse)
        return node


class M6Transformer(ast.NodeTransformer):
    """M6: drop check in PerWindowDetector._step nested gm_call (consensus shape match)."""
    def __init__(self):
        self.count = 0

    def visit_If(self, node):
        self.generic_visit(node)
        str_test = ast.unparse(node.test)
        if "grid.gh" in str_test and "grid.gw" in str_test and "np.bool_" in str_test:
            self.count += 1
            return ast.If(test=ast.Constant(value=False), body=node.body, orelse=node.orelse)
        return node


class M7Transformer(ast.NodeTransformer):
    """M7: drop check in PerWindowDetector.update (tiles validation)."""
    def __init__(self):
        self.count = 0

    def visit_If(self, node):
        self.generic_visit(node)
        if isinstance(node.test, ast.BoolOp) and isinstance(node.test.op, ast.Or):
            str_test = ast.unparse(node.test)
            if "TILE_DTYPE" in str_test and "morton" in str_test:
                self.count += 1
                return ast.If(test=ast.Constant(value=False), body=node.body, orelse=node.orelse)
        return node


# --- Test Fixture Seams ---

class CountGM:
    def __init__(self):
        self.OPS = 0

    def estimate_global_motion(self, prev, cur, max_d):
        self.OPS += 10
        return (1, 0, 0)

    def consensus_tiles(self, prev, cur, side, dx, dy):
        self.OPS += 5
        gh, gw = cur.shape[0] // side, cur.shape[1] // side
        return np.zeros((gh, gw), dtype=np.bool_)


class AllFlickerTracker:
    def __init__(self, n_tiles, max_half=64):
        self.n = n_tiles

    def update(self, hashes):
        return np.full(self.n, 4, dtype=np.int64), np.zeros(self.n, dtype=np.int64)


class FlFixture:
    FlickerTracker = AllFlickerTracker


class MalformedConsensusGM:
    def estimate_global_motion(self, prev, cur, max_d):
        return (1, 0, 0)

    def consensus_tiles(self, prev, cur, side, dx, dy):
        return np.zeros((2, 2), dtype=np.bool_)


class TestJq3MutkillPerWindowDetector(unittest.TestCase):
    def setUp(self):
        self.handle_a = Handle("cdp_tab", "tab:10", 0)
        self.handle_b = Handle("godot_window", "arena:2", 2**85)

    def test_real_additive_operation_counts_and_isolation(self):
        d = PerWindowDetector(gm=CountGM())
        f0, f1 = frame(10), frame(20)
        d.update(self.handle_a, baseline(f0), f0, epoch=1, frame_seq=0)
        res = d.update(self.handle_a, baseline(f1), f1, epoch=1, frame_seq=1)
        self.assertEqual(res.ops["gm"], 25)
        self.assertGreater(res.ops["fl"], 0)

    def test_real_invalid_tiles_and_consensus_shape_rejection(self):
        d = PerWindowDetector(gm=MalformedConsensusGM())
        f0, f1 = frame(10), frame(20)
        b0 = baseline(f0)
        b0["morton"] = b0["morton"] + 1
        with self.assertRaises(DetectorInputError) as cm_tiles:
            d.update(self.handle_a, b0, f0, epoch=1, frame_seq=0)
        self.assertEqual(str(cm_tiles.exception), "tiles must match the exact S2 grid, dtype and Morton order")

        d2 = PerWindowDetector(gm=MalformedConsensusGM())
        d2.update(self.handle_a, baseline(f0), f0, epoch=1, frame_seq=0)
        with self.assertRaises(MissingEvidenceError) as cm_cons:
            d2.update(self.handle_a, baseline(f1), f1, epoch=1, frame_seq=1)
        self.assertEqual(str(cm_cons.exception), "GM consensus must match grid")

    def test_real_veto_factory_validation(self):
        with self.assertRaises(DetectorInputError) as cm:
            PerWindowDetector(veto="not_a_callable")
        self.assertEqual(str(cm.exception), "veto must be an independent per-stream factory")

    def test_real_motion_zero_projection(self):
        d = PerWindowDetector(fl=FlFixture)
        f0, f1 = frame(10), frame(20)
        d.update(self.handle_a, baseline(f0), f0, epoch=1, frame_seq=0)
        res = d.update(self.handle_a, baseline(f1), f1, epoch=1, frame_seq=1)
        self.assertTrue(np.all(res.claims["mv_dx"] == 0))
        self.assertTrue(np.all(res.claims["mv_dy"] == 0))

    def test_real_clean_run_reset_and_settle(self):
        d = PerWindowDetector()
        f0 = frame(10)
        d.update(self.handle_a, baseline(f0), f0, epoch=1, frame_seq=0)
        d.update(self.handle_a, baseline(f0), f0, epoch=1, frame_seq=1)
        res = d.update(self.handle_a, baseline(f0), f0, epoch=1, frame_seq=2)
        self.assertFalse(res.settled)
        res_settled = d.update(self.handle_a, baseline(f0), f0, epoch=1, frame_seq=3)
        self.assertTrue(res_settled.settled)

    def test_real_interleaved_identities_isolation(self):
        frames_a = [frame(10 + i) for i in range(5)]
        frames_b = [frame(50 + i * 2) for i in range(5)]

        solo_a_det = PerWindowDetector()
        solo_b_det = PerWindowDetector()
        solo_a = [solo_a_det.update(self.handle_a, baseline(f), f, epoch=1, frame_seq=i) for i, f in enumerate(frames_a)]
        solo_b = [solo_b_det.update(self.handle_b, baseline(f), f, epoch=2, frame_seq=i) for i, f in enumerate(frames_b)]

        shared = PerWindowDetector()
        interleaved_a, interleaved_b = [], []
        for i, (fa, fb) in enumerate(zip(frames_a, frames_b)):
            interleaved_a.append(shared.update(self.handle_a, baseline(fa), fa, epoch=1, frame_seq=i))
            interleaved_b.append(shared.update(self.handle_b, baseline(fb), fb, epoch=2, frame_seq=i))

        for sa, ia in zip(solo_a, interleaved_a):
            np.testing.assert_array_equal(sa.claims, ia.claims)
            self.assertEqual(sa.ops, ia.ops)
            self.assertEqual(sa.settled, ia.settled)

        for sb, ib in zip(solo_b, interleaved_b):
            np.testing.assert_array_equal(sb.claims, ib.claims)
            self.assertEqual(sb.ops, ib.ops)
            self.assertEqual(sb.settled, ib.settled)

    def test_concurrent_multi_target_isolation(self):
        d = PerWindowDetector()
        frames = [frame(100 + i) for i in range(6)]

        def run_stream(handle, epoch):
            results = []
            for i, f in enumerate(frames):
                results.append(d.update(handle, baseline(f), f, epoch=epoch, frame_seq=i))
            return results

        with ThreadPoolExecutor(max_workers=2) as executor:
            fut_a = executor.submit(run_stream, self.handle_a, 1)
            fut_b = executor.submit(run_stream, self.handle_b, 2)
            res_a = fut_a.result(timeout=10)
            res_b = fut_b.result(timeout=10)

        self.assertEqual(len(res_a), 6)
        self.assertEqual(len(res_b), 6)
        self.assertEqual(res_a[-1].handle, self.handle_a)
        self.assertEqual(res_b[-1].handle, self.handle_b)

    # --- Mutation Kill Tests (M1..M7) ---

    def _eval_site_m1(self, mod):
        d = mod.PerWindowDetector(gm=CountGM())
        f0, f1 = frame(10), frame(20)
        d.update(self.handle_a, baseline(f0), f0, epoch=1, frame_seq=0)
        res = d.update(self.handle_a, baseline(f1), f1, epoch=1, frame_seq=1)
        if res.ops["gm"] != 25:
            raise AssertionError(f"Expected ops['gm'] == 25, got {res.ops}")

    def test_kill_m1_gm_ops_additive_operator_flip(self):
        # 1. Real passes assertion
        self._eval_site_m1(pwd_real)

        # 2. Mutant compilation and kill
        tree = ast.parse(SOURCE_TEXT)
        transformer = M1Transformer()
        tree = transformer.visit(tree)
        self.assertEqual(transformer.count, 1, "M1 anchor must match exactly once")
        mut_mod = compile_mutant(tree, "m1")
        try:
            with self.assertRaises(AssertionError):
                self._eval_site_m1(mut_mod)
        finally:
            cleanup_mutant(mut_mod)

    def _eval_site_m2(self, mod):
        d = mod.PerWindowDetector()
        f0 = frame(10)
        b0 = baseline(f0)
        b0["morton"] = b0["morton"] + 1
        try:
            d.update(self.handle_a, b0, f0, epoch=1, frame_seq=0)
            exc = None
        except Exception as e:
            exc = e
        if str(exc) != "tiles must match the exact S2 grid, dtype and Morton order":
            raise AssertionError(f"Expected exact tiles error, got {type(exc).__name__}: {exc}")

    def test_kill_m2_tiles_validation_operator_flip(self):
        self._eval_site_m2(pwd_real)

        tree = ast.parse(SOURCE_TEXT)
        transformer = M2Transformer()
        tree = transformer.visit(tree)
        self.assertEqual(transformer.count, 1, "M2 anchor must match exactly once")
        mut_mod = compile_mutant(tree, "m2")
        try:
            with self.assertRaises(AssertionError):
                self._eval_site_m2(mut_mod)
        finally:
            cleanup_mutant(mut_mod)

    def _eval_site_m3(self, mod):
        d = mod.PerWindowDetector(fl=FlFixture)
        f0, f1 = frame(10), frame(20)
        d.update(self.handle_a, baseline(f0), f0, epoch=1, frame_seq=0)
        res = d.update(self.handle_a, baseline(f1), f1, epoch=1, frame_seq=1)
        if not np.all(res.claims["mv_dx"] == 0):
            raise AssertionError(f"Expected claims['mv_dx'] == 0, got {res.claims['mv_dx']}")

    def test_kill_m3_flicker_motion_zero_boundary_increment(self):
        self._eval_site_m3(pwd_real)

        tree = ast.parse(SOURCE_TEXT)
        transformer = M3Transformer()
        tree = transformer.visit(tree)
        self.assertEqual(transformer.count, 1, "M3 anchor must match exactly once")
        mut_mod = compile_mutant(tree, "m3")
        try:
            with self.assertRaises(AssertionError):
                self._eval_site_m3(mut_mod)
        finally:
            cleanup_mutant(mut_mod)

    def _eval_site_m4(self, mod):
        d = mod.PerWindowDetector()
        f0 = frame(10)
        d.update(self.handle_a, baseline(f0), f0, epoch=1, frame_seq=0)
        d.update(self.handle_a, baseline(f0), f0, epoch=1, frame_seq=1)
        res = d.update(self.handle_a, baseline(f0), f0, epoch=1, frame_seq=2)
        if res.settled:
            raise AssertionError(f"Expected settled == False on seq 2, got {res.settled}")

    def test_kill_m4_clean_run_else_branch_boundary_increment(self):
        self._eval_site_m4(pwd_real)

        tree = ast.parse(SOURCE_TEXT)
        transformer = M4Transformer()
        tree = transformer.visit(tree)
        self.assertEqual(transformer.count, 1, "M4 anchor must match exactly once")
        mut_mod = compile_mutant(tree, "m4")
        try:
            with self.assertRaises(AssertionError):
                self._eval_site_m4(mut_mod)
        finally:
            cleanup_mutant(mut_mod)

    def _eval_site_m5(self, mod):
        try:
            mod.PerWindowDetector(veto="not_a_callable")
            exc = None
        except Exception as e:
            exc = e
        if str(exc) != "veto must be an independent per-stream factory":
            raise AssertionError(f"Expected exact veto error, got {type(exc).__name__}: {exc}")

    def test_kill_m5_veto_callable_check_dropped(self):
        self._eval_site_m5(pwd_real)

        tree = ast.parse(SOURCE_TEXT)
        transformer = M5Transformer()
        tree = transformer.visit(tree)
        self.assertEqual(transformer.count, 1, "M5 anchor must match exactly once")
        mut_mod = compile_mutant(tree, "m5")
        try:
            with self.assertRaises(AssertionError):
                self._eval_site_m5(mut_mod)
        finally:
            cleanup_mutant(mut_mod)

    def _eval_site_m6(self, mod):
        d = mod.PerWindowDetector(gm=MalformedConsensusGM())
        f0, f1 = frame(10), frame(20)
        d.update(self.handle_a, baseline(f0), f0, epoch=1, frame_seq=0)
        try:
            d.update(self.handle_a, baseline(f1), f1, epoch=1, frame_seq=1)
            exc = None
        except Exception as e:
            exc = e
        if str(exc) != "GM consensus must match grid":
            raise AssertionError(f"Expected exact consensus error, got {type(exc).__name__}: {exc}")

    def test_kill_m6_consensus_shape_check_dropped(self):
        self._eval_site_m6(pwd_real)

        tree = ast.parse(SOURCE_TEXT)
        transformer = M6Transformer()
        tree = transformer.visit(tree)
        self.assertEqual(transformer.count, 1, "M6 anchor must match exactly once")
        mut_mod = compile_mutant(tree, "m6")
        try:
            with self.assertRaises(AssertionError):
                self._eval_site_m6(mut_mod)
        finally:
            cleanup_mutant(mut_mod)

    def _eval_site_m7(self, mod):
        d = mod.PerWindowDetector()
        f0 = frame(10)
        invalid_tiles = np.zeros(16, dtype=desc.TILE_DTYPE)
        try:
            d.update(self.handle_a, invalid_tiles, f0, epoch=1, frame_seq=0)
            exc = None
        except Exception as e:
            exc = e
        if str(exc) != "tiles must match the exact S2 grid, dtype and Morton order":
            raise AssertionError(f"Expected exact tiles error, got {type(exc).__name__}: {exc}")

    def test_kill_m7_tiles_validation_check_dropped(self):
        self._eval_site_m7(pwd_real)

        tree = ast.parse(SOURCE_TEXT)
        transformer = M7Transformer()
        tree = transformer.visit(tree)
        self.assertEqual(transformer.count, 1, "M7 anchor must match exactly once")
        mut_mod = compile_mutant(tree, "m7")
        try:
            with self.assertRaises(AssertionError):
                self._eval_site_m7(mut_mod)
        finally:
            cleanup_mutant(mut_mod)


if __name__ == "__main__":
    unittest.main()
