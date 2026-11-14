"""STORAGE LAW V1: g_sealed_untouched, g_disk_frames and the g_layer rule for vadapt/ramws.py (each fails before the gate exists)."""
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location("_t_" + name, ROOT / "tools" / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def git(d, *a):
    subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True, timeout=60)


class TestLawGates(unittest.TestCase):
    def test_layer_rule_ramws_stdlib_only(self):
        g = load("gates")
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as t:
            r = Path(t)
            (r / "vadapt").mkdir()
            for src, bad in (("import os\nimport json\n", False), ("from visioncore import ids\n", True),
                             ("import numpy\n", True), ("from vmem import x\n", True)):
                (r / "vadapt" / "ramws.py").write_text(src)
                self.assertEqual(bool(g.g_layer(r)), bad, src)
            (r / "vadapt" / "other.py").write_text("import numpy\n")
            (r / "vadapt" / "ramws.py").write_text("import os\n")
            self.assertEqual(g.g_layer(r), [])

    def test_sealed_untouched(self):
        c = load("gate_checks")
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as t:
            r = Path(t)
            self.assertEqual(c.sealed_untouched(r), [])  # no .git -> NA
            git(r, "init", "-q")
            (r / "visioncore").mkdir()
            (r / "visioncore" / "a.py").write_text("X = 1\n")
            (r / "vmem").mkdir()
            (r / "vmem" / "b.py").write_text("Y = 1\n")
            git(r, "add", "-A")
            git(r, "commit", "-qm", "x")
            self.assertEqual([x[0] for x in c.sealed_untouched(r)], ["E_GATE_SEALED"])  # tag missing
            git(r, "tag", "ve-sealed-1")
            self.assertEqual(c.sealed_untouched(r), [])
            (r / "vmem" / "b.py").write_text("Y = 2\n")
            self.assertEqual(len(c.sealed_untouched(r)), 1)
            git(r, "checkout", "--", "vmem")
            (r / "visioncore" / "new.py").write_text("")
            self.assertEqual(len(c.sealed_untouched(r)), 1)
            (r / "visioncore" / "new.py").unlink()
            self.assertEqual(c.sealed_untouched(r), [])

    def test_real_tree_sealed_and_gates_registered(self):
        g, c = load("gates"), load("gate_checks")
        self.assertEqual(c.sealed_untouched(ROOT), [])
        names = [n for n, _ in g.GATES]
        self.assertIn("g_sealed_untouched", names)
        self.assertIn("g_disk_frames", names)

    def test_disk_frames_na_without_eval_and_passes_here(self):
        c = load("gate_checks")
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as t:
            self.assertEqual(c.disk_frames(Path(t)), [])
        self.assertEqual(c.disk_frames(ROOT), [])


if __name__ == "__main__":
    unittest.main()
