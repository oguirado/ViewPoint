"""Unit tests for tools.layer_check: layer rules, relative imports, and vision-engine check."""
import tempfile
import unittest
from pathlib import Path

from tools import layer_check

ROOT = Path(__file__).resolve().parents[1]


class TestLayerCheck(unittest.TestCase):
    def test_real_repo_clean(self):
        violations = layer_check.check(ROOT)
        self.assertEqual(violations, [])
        self.assertEqual(layer_check.main([]), 0)

    def test_synthetic_tree_violations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            pkg1 = tmp_path / "wender" / "pkg1"
            pkg2 = tmp_path / "wender" / "pkg2"
            pkg1.mkdir(parents=True)
            pkg2.mkdir(parents=True)

            (pkg2 / "l2_mod.py").write_text(
                '# @module {"id":"wa.pkg2.l2_mod","layer":2,"owner":"PERCEIVE","deps":[],"book":"HB-WA-s1-perceive"}\n'
                'def foo(): pass\n'
            )

            (pkg1 / "l0_mod.py").write_text(
                '# @module {"id":"wa.pkg1.l0_mod","layer":0,"owner":"PERCEIVE","deps":[],"book":"HB-WA-s1-perceive"}\n'
                'import wender.pkg2.l2_mod\n'
                'from ..pkg2.l2_mod import foo\n'
                'import visioncore\n'
            )

            violations = layer_check.check(tmp_path)
            self.assertEqual(len(violations), 3)

            v_abs_layer = [v for v in violations if v["line"] == 2]
            v_rel_layer = [v for v in violations if v["line"] == 3]
            v_vision = [v for v in violations if v["line"] == 4]

            self.assertEqual(len(v_abs_layer), 1)
            self.assertEqual(v_abs_layer[0]["type"], "layer_violation")
            self.assertEqual(v_abs_layer[0]["imported"], "wa.pkg2.l2_mod")

            self.assertEqual(len(v_rel_layer), 1)
            self.assertEqual(v_rel_layer[0]["type"], "layer_violation")
            self.assertEqual(v_rel_layer[0]["imported"], "wa.pkg2.l2_mod")

            self.assertEqual(len(v_vision), 1)
            self.assertEqual(v_vision[0]["type"], "vision_engine_import")
            self.assertEqual(v_vision[0]["imported"], "visioncore")


if __name__ == "__main__":
    unittest.main()
