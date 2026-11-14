"""Tests for eval.native_parity_v2k host instrument."""

import importlib
import os
import sys
import tempfile
import unittest

from eval.native_parity_v2k import check


class TestNativeParityV2k(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        sys.path.insert(0, self.temp_dir.name)

    def tearDown(self):
        if self.temp_dir.name in sys.path:
            sys.path.remove(self.temp_dir.name)
        self.temp_dir.cleanup()

    def _write_mutant(self, module_name, code):
        filepath = os.path.join(self.temp_dir.name, f"{module_name}.py")
        with open(filepath, "w") as f:
            f.write(code)
        importlib.invalidate_caches()
        return module_name

    def test_reference_against_itself_subtile(self):
        report = check("eval.v2proto_subtile", "st", "eval.v2proto_subtile", resolutions=[(64, 48)])
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["mismatches_count"], 0)

    def test_reference_against_itself_st_block4(self):
        report = check("eval.v2x.st_block4", "st", "eval.v2x.st_block4", resolutions=[(64, 48)])
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["mismatches_count"], 0)

    def test_reference_against_itself_global_motion(self):
        report = check("eval.v2proto_global_motion", "gm", "eval.v2proto_global_motion")
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["mismatches_count"], 0)

    def test_detects_mutant_cache_keyed_on_id_only(self):
        code = """
import eval.v2proto_subtile as ref

_CACHE = {}

def subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d):
    key = (id(prev_luma), id(cur_luma), s, tile_xy, max_d)
    if key in _CACHE:
        return _CACHE[key]
    res = ref.subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d)
    _CACHE[key] = res
    return res
"""
        mod = self._write_mutant("mutant_cache_id", code)
        report = check(mod, "st", "eval.v2proto_subtile", resolutions=[(64, 48)])
        self.assertEqual(report["status"], "FAIL")
        self.assertGreater(report["mismatches_count"], 0)

    def test_detects_mutant_swapped_dx_dy(self):
        code = """
import eval.v2proto_global_motion as ref

def estimate_global_motion(prev_luma, cur_luma, max_d):
    res = ref.estimate_global_motion(prev_luma, cur_luma, max_d)
    if res is None:
        return None
    dx, dy, score = res
    return (dy, dx, score)

def consensus_tiles(prev, cur, s, dx, dy):
    return ref.consensus_tiles(prev, cur, s, dx, dy)
"""
        mod = self._write_mutant("mutant_swapped_dx_dy", code)
        report = check(mod, "gm", "eval.v2proto_global_motion")
        self.assertEqual(report["status"], "FAIL")
        self.assertGreater(report["mismatches_count"], 0)

    def test_detects_mutant_consensus_mask_transposed(self):
        code = """
import eval.v2proto_global_motion as ref

def estimate_global_motion(prev_luma, cur_luma, max_d):
    return ref.estimate_global_motion(prev_luma, cur_luma, max_d)

def consensus_tiles(prev, cur, s, dx, dy):
    mask = ref.consensus_tiles(prev, cur, s, dx, dy)
    return mask.T
"""
        mod = self._write_mutant("mutant_mask_transposed", code)
        report = check(mod, "gm", "eval.v2proto_global_motion")
        self.assertEqual(report["status"], "FAIL")
        self.assertGreater(report["mismatches_count"], 0)

    def test_detects_mutant_score_off_by_one(self):
        code = """
import eval.v2proto_global_motion as ref

def estimate_global_motion(prev_luma, cur_luma, max_d):
    res = ref.estimate_global_motion(prev_luma, cur_luma, max_d)
    if res is None:
        return None
    dx, dy, score = res
    return (dx, dy, score + 1)

def consensus_tiles(prev, cur, s, dx, dy):
    return ref.consensus_tiles(prev, cur, s, dx, dy)
"""
        mod = self._write_mutant("mutant_score_off_by_one", code)
        report = check(mod, "gm", "eval.v2proto_global_motion")
        self.assertEqual(report["status"], "FAIL")
        self.assertGreater(report["mismatches_count"], 0)


if __name__ == "__main__":
    unittest.main()
