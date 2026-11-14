import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATES = ROOT / "tools" / "gates.py"


def run(*args):
    p = subprocess.run([sys.executable, "-I", str(GATES), *args], capture_output=True, text=True, timeout=120)
    return p.returncode, json.loads(p.stdout)


class TestGates(unittest.TestCase):
    def test_mutants_all_killed(self):
        rc, o = run("--mutants", "--json")
        self.assertEqual(rc, 0, o)
        self.assertEqual(o["code"], "OK")
        self.assertGreaterEqual(o["killed"], 16)

    def test_every_gate_has_a_mutant(self):
        names = {p.name.split("__")[0][2:] for p in (ROOT / "tests/mutants/gate").glob("m_*.py")}
        for g in ("g_tier1", "g_anchor", "g_header", "g_layer", "g_threshold", "g_nofloat",
                  "g_engine_free", "g_ids", "g_schema", "g_size", "g_canon", "g_pure"):
            self.assertIn(g, names)

    def test_usage_rc2(self):
        rc, o = run("--bogus")
        self.assertEqual((rc, o["code"]), (2, "E_USAGE"))

    def test_empty_root_fails_closed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            rc, o = run("--all", "--root", d)
        self.assertEqual(rc, 5)
        self.assertTrue(o["receipt"].startswith("VG:FAIL:"))

    def test_deterministic_all(self):
        a = run("--all")
        self.assertEqual(a, run("--all"))
        self.assertEqual(a, run("--all"))


if __name__ == "__main__":
    unittest.main()
