"""E4 residual gate bypasses found while re-verifying RARCH-6/7 at BASE_E (float.fromhex, bare `g%d` gop id)."""
import tempfile
import unittest
from pathlib import Path

from tests.test_gates_fix import copy_tree, run


def _overlay(gate, rel, add):
    with tempfile.TemporaryDirectory() as d:
        copy_tree(d)
        with open(Path(d) / rel, "a", encoding="utf-8") as f:
            f.write("\n" + add)
        return run("--gate", gate, "--root", d)


class ResidualBypasses(unittest.TestCase):
    def test_nofloat_float_fromhex(self):  # RARCH-6 residual
        rc, o = _overlay("g_nofloat", "visioncore/change.py", "x = float.fromhex('0x1')\n")
        self.assertEqual((rc, o["failed"]), (5, ["g_nofloat"]))

    def test_ids_bare_gop_percent_template(self):  # RARCH-7 residual
        rc, o = _overlay("g_ids", "visioncore/gop.py", 'x = "g%d" % 3\n')
        self.assertEqual((rc, o["failed"]), (5, ["g_ids"]))

    def test_ids_gop_format_template(self):  # RARCH-7 control
        rc, o = _overlay("g_ids", "visioncore/gop.py", 'x = "g{}".format(3)\n')
        self.assertEqual((rc, o["failed"]), (5, ["g_ids"]))

    def test_clean_tree_passes_both(self):
        for g in ("g_nofloat", "g_ids"):
            with tempfile.TemporaryDirectory() as d:
                copy_tree(d)
                rc, o = run("--gate", g, "--root", d)
            self.assertEqual((rc, o["failed"]), (0, []), (g, o))


if __name__ == "__main__":
    unittest.main()
