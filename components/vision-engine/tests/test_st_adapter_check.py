"""Tests for eval/v2eval/st_adapter_check.py."""

import importlib
import subprocess
import sys
import unittest
import numpy as np
import eval.v2proto_subtile as ref_proto


class IdOnlyCachingAdapter:
    """Mutant adapter that caches result based on id(prev) and id(cur) only."""
    _cache = {}

    @classmethod
    def reset(cls):
        cls._cache.clear()

    @staticmethod
    def subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d):
        key = (id(prev_luma), id(cur_luma), s, tile_xy, max_d)
        if key in IdOnlyCachingAdapter._cache:
            return IdOnlyCachingAdapter._cache[key]
        res = ref_proto.subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d)
        IdOnlyCachingAdapter._cache[key] = res
        return res


class IgnoreMaxDAdapter:
    """Mutant adapter that ignores max_d parameter and always uses max_d=1."""
    @staticmethod
    def subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d):
        return ref_proto.subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d=1)


class IgnoreSAdapter:
    """Mutant adapter that ignores s parameter and always uses s=16."""
    @staticmethod
    def subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d):
        return ref_proto.subtile_motion(prev_luma, cur_luma, s=16, tile_xy=tile_xy, max_d=max_d)


class TestStAdapterCheck(unittest.TestCase):

    def test_references_pass_against_themselves(self):
        """Test that eval.v2proto_subtile and eval.v2x.st_block4 pass when checked against themselves."""
        for mod in ["eval.v2proto_subtile", "eval.v2x.st_block4"]:
            cmd = [
                sys.executable,
                "eval/v2eval/st_adapter_check.py",
                "--candidate",
                mod,
                "--reference",
                mod,
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"Expected 0 for {mod} vs itself, got stderr: {res.stderr}")
            self.assertIn('"passed": true', res.stdout)

    def test_id_only_cache_mutant_caught(self):
        """Test that an adapter caching on array IDs alone is caught by attack 3 (in-place modified buffer)."""
        import eval.v2eval.st_adapter_check as checker
        IdOnlyCachingAdapter.reset()

        report = checker.run_attack_3_in_place_modified_buffer(IdOnlyCachingAdapter, ref_proto)
        self.assertFalse(report["passed"], "Id-only caching adapter should fail in-place modification attack")

    def test_ignore_max_d_mutant_caught(self):
        """Test that an adapter ignoring max_d is caught by attack 5."""
        import eval.v2eval.st_adapter_check as checker

        report = checker.run_attack_5_different_max_d(IgnoreMaxDAdapter, ref_proto)
        self.assertFalse(report["passed"], "Ignore-max_d adapter should fail different_max_d attack")

    def test_ignore_s_mutant_caught(self):
        """Test that an adapter ignoring s is caught by attack 6."""
        import eval.v2eval.st_adapter_check as checker

        report = checker.run_attack_6_different_s(IgnoreSAdapter, ref_proto)
        self.assertFalse(report["passed"], "Ignore-s adapter should fail different_s attack")


if __name__ == "__main__":
    unittest.main()
