"""Mutation testing harness and mutant kill suite for multi_describe and fl_v3.

Covers named catalogue contrasts:
- M-vadapt-multi-describe-1: boundary increment in _run_batch at vadapt/multi_describe.py:198
- M-vadapt-multi-describe-2: drop check in _identity at vadapt/multi_describe.py:90
- M-vadapt-multi-describe-3: drop check in describe_windows at vadapt/multi_describe.py:260
- M-vadapt-multi-describe-4: drop check in describe_windows at vadapt/multi_describe.py:248
- M-vadapt-multi-describe-6: swap arguments in _admit at vadapt/multi_describe.py:136
- M-eval-v2x-fl-v3-1: boundary increment in veto_flicker at eval/v2x/fl_v3.py:250
- M-eval-v2x-fl-v3-2: boundary increment in veto_flicker at eval/v2x/fl_v3.py:267
- M-eval-v2x-fl-v3-3: drop check in veto_flicker at eval/v2x/fl_v3.py:139
- M-eval-v2x-fl-v3-4: drop check in veto_flicker at eval/v2x/fl_v3.py:142
- M-eval-v2x-fl-v3-5: drop check in veto_flicker at eval/v2x/fl_v3.py:137
- M-eval-v2x-fl-v3-6: drop check in veto_flicker at eval/v2x/fl_v3.py:154

Every mutant is executed against a fresh, dynamically compiled private module.
INV-R1-1: Original passing assertion precedes every kill.
INV-R1-2: Only AssertionError on mutant counts as a kill.
INV-R1-3: Clean teardown of private modules.
"""
import importlib
import importlib.util
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import numpy as np

from visioncore import change
import vadapt.multi_describe as real_md
import eval.v2x.fl_v3 as real_fl

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent


class ReverseCompletionRunner:
    def map(self, fn, items):
        return reversed([fn(item) for item in items])


def _make_window(canonical_id="win_a", seed=42, kind="cdp_tab", generation=0, epoch=0,
                 size=(48, 64), seqs=(10, 11, 12), dpr_ppm=1_000_000):
    h, w = size
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
    frames = (base.copy(), base.copy(), np.bitwise_xor(base, np.uint8(129)))[:len(seqs)]
    for f in frames:
        f.setflags(write=True)
    return real_md.WindowFrames(
        real_md.Handle(kind, canonical_id, generation),
        epoch,
        frames,
        tuple(seqs),
        dpr_ppm
    )


def _make_flicker_fixture(s=16, gw=2, gh=2):
    n = gw * gh
    s2_tiles = np.zeros(n, dtype=change.TILE_DT)
    s2_tiles["morton"] = np.array(range(n), dtype=np.uint16)
    s2_tiles["cls"] = change.C_FLICKER
    s2_tiles["xh64"] = np.array([300 + i for i in range(n)], dtype=np.uint64)
    s2_tiles["y_q4"] = 50

    prev1_tiles = np.zeros(n, dtype=change.TILE_DT)
    prev1_tiles["morton"] = np.array(range(n), dtype=np.uint16)
    prev1_tiles["cls"] = change.C_FLICKER
    prev1_tiles["xh64"] = np.array([200 + i for i in range(n)], dtype=np.uint64)
    prev1_tiles["y_q4"] = 0

    prev2_tiles = np.zeros(n, dtype=change.TILE_DT)
    prev2_tiles["morton"] = np.array(range(n), dtype=np.uint16)
    prev2_tiles["cls"] = change.C_FLICKER
    prev2_tiles["xh64"] = np.array([100 + i for i in range(n)], dtype=np.uint64)
    prev2_tiles["y_q4"] = 50

    words_eq = np.zeros(n, dtype=np.bool_)

    h, w = gh * s, gw * s
    y_prev2 = np.zeros((h, w), dtype=np.uint8)
    y_prev1 = np.ones((h, w), dtype=np.uint8) * 50
    y_cur = np.zeros((h, w), dtype=np.uint8)

    params = {
        "evidence": {
            "frame_ids": (0, 1, 2),
            "history_valid": np.ones(n, dtype=np.bool_),
            "hash_valid": np.ones((3, n), dtype=np.bool_),
        }
    }
    return s2_tiles, prev2_tiles, prev1_tiles, words_eq, (s, gw, gh), y_prev2, y_prev1, y_cur, params


class TestMutkillMultiDescribeAndFLV3(unittest.TestCase):

    def _compile_and_kill(self, rel_path, package, anchor, replacement, assertion_func):
        source_file = ROOT / rel_path
        src = source_file.read_text(encoding="utf-8")

        count = src.count(anchor)
        self.assertEqual(count, 1, f"Anchor {anchor!r} count is {count}, expected exactly 1")

        # INV-R1-1: Run passing assertion on real module first
        real_mod = importlib.import_module(rel_path.replace("/", ".").removesuffix(".py"))
        assertion_func(self, real_mod)

        # Prepare mutated module
        mod_name = f"_mut_{hash(anchor) & 0xffffffff:08x}"
        spec = importlib.util.spec_from_loader(mod_name, loader=None)
        mut_mod = importlib.util.module_from_spec(spec)
        mut_mod.__file__ = str(source_file)
        mut_mod.__package__ = package

        mutated_src = src.replace(anchor, replacement, 1)
        code = compile(mutated_src, mut_mod.__file__, "exec")

        sys.modules[mod_name] = mut_mod
        try:
            exec(code, mut_mod.__dict__)
            # Execute assertion on mutant - MUST raise AssertionError
            with self.assertRaises(AssertionError, msg=f"Mutant survived for anchor {anchor!r}"):
                assertion_func(self, mut_mod)
        finally:
            sys.modules.pop(mod_name, None)

    # --- M-vadapt-multi-describe tests ---

    def test_kill_m_vadapt_multi_describe_1(self):
        """M-vadapt-multi-describe-1: boundary increment in _run_batch at vadapt/multi_describe.py:198."""
        anchor = '"channels": 3, "pixel_format": "rgb24", "storage": "rgb24_raw"'
        replacement = '"channels": 4, "pixel_format": "rgb24", "storage": "rgb24_raw"'

        def assert_channel_manifest(tc, mod):
            captured_channels = []
            real_analyze = mod.pipeline.analyze

            def spy_analyze(source, **kwargs):
                captured_channels.append(source.meta.get("channels"))
                return real_analyze(source, **kwargs)

            with mock.patch.object(mod.pipeline, "analyze", side_effect=spy_analyze):
                item = _make_window()
                res = mod.describe_windows([item], runner=ReverseCompletionRunner())
                tc.assertEqual(len(res), 1)
                tc.assertIsNone(res[0].error)
                tc.assertEqual(captured_channels[0], 3)

        self._compile_and_kill("vadapt/multi_describe.py", "vadapt", anchor, replacement, assert_channel_manifest)

    def test_kill_m_vadapt_multi_describe_2(self):
        """M-vadapt-multi-describe-2: drop check in _identity at vadapt/multi_describe.py:90."""
        anchor = ("not isinstance(kind, str) or kind not in _SUPPORTED_KINDS\n"
                  "            or not isinstance(canonical_id, str) or not canonical_id")
        replacement = "False"

        def assert_identity_check(tc, mod):
            bad_item = _make_window(kind="invalid_kind")
            res = mod.describe_windows([bad_item])
            tc.assertEqual(len(res), 1)
            tc.assertIsNotNone(res[0].error)
            tc.assertTrue(res[0].error.startswith("E_M3_HANDLE"))

        self._compile_and_kill("vadapt/multi_describe.py", "vadapt", anchor, replacement, assert_identity_check)

    def test_kill_m_vadapt_multi_describe_3(self):
        """M-vadapt-multi-describe-3: drop check in describe_windows at vadapt/multi_describe.py:260."""
        anchor = 'not all(hasattr(item, attr) for attr in ("handle", "epoch", "frame_seqs"))'
        replacement = "False"

        @dataclass
        class IncompleteItem:
            handle: object
            epoch: int = 0
            dpr_ppm: int = 0

        def assert_required_attrs_check(tc, mod):
            bad_item = IncompleteItem(handle=mod.Handle("cdp_tab", "win_a", 0))
            with tc.assertRaises(mod.WindowInputError):
                mod.describe_windows([bad_item])

        self._compile_and_kill("vadapt/multi_describe.py", "vadapt", anchor, replacement, assert_required_attrs_check)

    def test_kill_m_vadapt_multi_describe_4(self):
        """M-vadapt-multi-describe-4: drop check in describe_windows at vadapt/multi_describe.py:248."""
        anchor = "params is not None and not isinstance(params, dict)"
        replacement = "False"

        def assert_invalid_params_check(tc, mod):
            item = _make_window()
            with tc.assertRaises(mod.WindowInputError) as cm:
                mod.describe_windows([item], params="invalid_string_params")
            tc.assertEqual(cm.exception.code, "E_M3_PARAMS")

        self._compile_and_kill("vadapt/multi_describe.py", "vadapt", anchor, replacement, assert_invalid_params_check)

    def test_kill_m_vadapt_multi_describe_6(self):
        """M-vadapt-multi-describe-6: swap arguments in _admit at vadapt/multi_describe.py:136."""
        anchor = 'WindowInputError("E_M3_GEOMETRY", "a geometry epoch cannot contain different frame sizes")'
        replacement = 'WindowInputError("a geometry epoch cannot contain different frame sizes", "E_M3_GEOMETRY")'

        def assert_geometry_error_code(tc, mod):
            f1 = np.zeros((48, 64, 3), dtype=np.uint8)
            f2 = np.zeros((64, 80, 3), dtype=np.uint8)
            bad_geom = mod.WindowFrames(mod.Handle("cdp_tab", "win_a", 0), 0, (f1, f2), (10, 11), 1_000_000)
            res = mod.describe_windows([bad_geom])
            tc.assertEqual(len(res), 1)
            tc.assertIsNotNone(res[0].error)
            tc.assertTrue(res[0].error.startswith("E_M3_GEOMETRY"))

        self._compile_and_kill("vadapt/multi_describe.py", "vadapt", anchor, replacement, assert_geometry_error_code)

    # --- M-eval-v2x-fl-v3 tests ---

    def test_kill_m_eval_v2x_fl_v3_1(self):
        """M-eval-v2x-fl-v3-1: boundary increment in veto_flicker at eval/v2x/fl_v3.py:250."""
        anchor = "len(gm_vector) != 2"
        replacement = "len(gm_vector) != 3"

        def assert_vector_arity_check(tc, mod):
            s2_tiles, prev2, prev1, words_eq, grid, y_p2, y_p1, y_c, params = _make_flicker_fixture()
            dec = mod.veto_flicker(s2_tiles, prev2, prev1, words_eq, grid, gm_vector=(0, 0),
                                  y_prev2=y_p2, y_prev1=y_p1, y_cur=y_c, params=params)
            tc.assertNotIn(mod.Reason.INCONSISTENT_GM, dec.reasons)

        self._compile_and_kill("eval/v2x/fl_v3.py", "eval.v2x", anchor, replacement, assert_vector_arity_check)

    def test_kill_m_eval_v2x_fl_v3_2(self):
        """M-eval-v2x-fl-v3-2: boundary increment in veto_flicker at eval/v2x/fl_v3.py:267."""
        anchor = "count_info == 0"
        replacement = "count_info != 0"

        def assert_count_info_check(tc, mod):
            s = 16
            gw, gh = 4, 2
            n = gw * gh
            h, w = gh * s, gw * s

            y_c = np.zeros((h, w), dtype=np.uint8)
            for y in range(h):
                for x in range(w):
                    y_c[y, x] = (x * 7 + y * 13) % 200

            y_p1 = np.zeros((h, w), dtype=np.uint8)
            for y in range(h):
                for x in range(w):
                    y_p1[y, x] = ((x + 1) * 7 + y * 13) % 200

            y_p2 = y_p1.copy()

            s2_tiles, prev2, prev1, words_eq, grid, _, _, _, params = _make_flicker_fixture(s, gw, gh)
            dec = mod.veto_flicker(s2_tiles, prev2, prev1, words_eq, grid, gm_vector=(1, 0),
                                  y_prev2=y_p2, y_prev1=y_p1, y_cur=y_c, params=params)
            tc.assertNotIn(mod.Reason.NO_GM_CONSENSUS, dec.reasons)

        self._compile_and_kill("eval/v2x/fl_v3.py", "eval.v2x", anchor, replacement, assert_count_info_check)

    def test_kill_m_eval_v2x_fl_v3_3(self):
        """M-eval-v2x-fl-v3-3: drop check in veto_flicker at eval/v2x/fl_v3.py:139."""
        anchor = "not isinstance(grid, (tuple, list)) or len(grid) != 3 or not all(_integer(v) for v in grid)"
        replacement = "False"

        def assert_malformed_grid_check(tc, mod):
            s2_tiles, prev2, prev1, words_eq, _, _, _, _, _ = _make_flicker_fixture()
            with tc.assertRaises(TypeError) as cm:
                mod.veto_flicker(s2_tiles, prev2, prev1, words_eq, grid=(16.5, 2, 2))
            tc.assertEqual(str(cm.exception), "grid must be integer (s, gw, gh)")

        self._compile_and_kill("eval/v2x/fl_v3.py", "eval.v2x", anchor, replacement, assert_malformed_grid_check)

    def test_kill_m_eval_v2x_fl_v3_4(self):
        """M-eval-v2x-fl-v3-4: drop check in veto_flicker at eval/v2x/fl_v3.py:142."""
        anchor = "not (16 <= s <= 128 and s % 8 == 0 and 1 <= gw <= 255 and 1 <= gh <= 255)"
        replacement = "False"

        def assert_grid_bounds_check(tc, mod):
            s2_tiles, prev2, prev1, words_eq, _, _, _, _, _ = _make_flicker_fixture()
            with tc.assertRaises(ValueError) as cm:
                mod.veto_flicker(s2_tiles, prev2, prev1, words_eq, grid=(10, 2, 2))
            tc.assertEqual(str(cm.exception), "unsupported grid; s is a multiple of 8 in [16,128]")

        self._compile_and_kill("eval/v2x/fl_v3.py", "eval.v2x", anchor, replacement, assert_grid_bounds_check)

    def test_kill_m_eval_v2x_fl_v3_5(self):
        """M-eval-v2x-fl-v3-5: drop check in veto_flicker at eval/v2x/fl_v3.py:137."""
        anchor = "not isinstance(s2_tiles, np.ndarray) or s2_tiles.dtype != change.TILE_DT or s2_tiles.ndim != 1"
        replacement = "False"

        def assert_tile_dtype_check(tc, mod):
            _, prev2, prev1, words_eq, grid, _, _, _, _ = _make_flicker_fixture()
            bad_dt = np.dtype([("morton", "<u2"), ("cls", "<u1"), ("flags", "<u1"), ("xh64", "<u8"), ("y_q4", "<u1")])
            bad_s2 = np.zeros(4, dtype=bad_dt)
            bad_s2["morton"] = np.array([0, 1, 2, 3], dtype=np.uint16)
            with tc.assertRaises(TypeError) as cm:
                mod.veto_flicker(bad_s2, prev2, prev1, words_eq, grid)
            tc.assertEqual(str(cm.exception), "s2_tiles must be one-dimensional TILE_DT")

        self._compile_and_kill("eval/v2x/fl_v3.py", "eval.v2x", anchor, replacement, assert_tile_dtype_check)

    def test_kill_m_eval_v2x_fl_v3_6(self):
        """M-eval-v2x-fl-v3-6: drop check in veto_flicker at eval/v2x/fl_v3.py:154."""
        anchor = 'not isinstance(params, dict) or set(params) - {"thresholds", "evidence"}'
        replacement = "False"

        def assert_unexpected_params_check(tc, mod):
            s2_tiles, prev2, prev1, words_eq, grid, _, _, _, _ = _make_flicker_fixture()
            with tc.assertRaises(ValueError) as cm:
                mod.veto_flicker(s2_tiles, prev2, prev1, words_eq, grid, params={"unexpected_key": 123})
            tc.assertEqual(str(cm.exception), "eval envelope accepts only thresholds and evidence")

        self._compile_and_kill("eval/v2x/fl_v3.py", "eval.v2x", anchor, replacement, assert_unexpected_params_check)


if __name__ == "__main__":
    unittest.main()
