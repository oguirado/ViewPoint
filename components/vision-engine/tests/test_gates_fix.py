import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATES = ROOT / "tools" / "gates.py"
ROUTE = ROOT / "tools" / "route_code.py"


def run(*args):
    p = subprocess.run([sys.executable, "-I", str(GATES), *args], capture_output=True, text=True, timeout=300)
    return p.returncode, json.loads(p.stdout)


def route(q, root=ROOT):
    p = subprocess.run([sys.executable, "-I", str(ROUTE), "--q", q, "--root", str(root)], capture_output=True, text=True, timeout=60)
    return p.returncode, json.loads(p.stdout), len(p.stdout.encode())


def copy_tree(dst, names=("visioncore", "vadapt", "vmem", "tools", "catalogue", "tests/golden", "llms.txt", "AGENTS.md")):
    for n in names:
        src = ROOT / n
        out = Path(dst) / n
        out.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, out, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy(src, out)


class CatalogueAndRoutes(unittest.TestCase):
    def test_catalogue_is_fresh(self):  # RARCH-1
        rc, o = run("--gate", "g_catalogue")
        self.assertEqual((rc, o["failed"]), (0, []), o)

    def test_stale_catalogue_fails_gate(self):
        with tempfile.TemporaryDirectory() as d:
            copy_tree(d)
            with open(Path(d) / "visioncore" / "layout.py", "a", encoding="utf-8") as f:
                f.write("\n# @anchor:vc.layout.extra_unrecorded\n")
            rc, o = run("--gate", "g_catalogue", "--root", d)
        self.assertEqual((rc, o["failed"]), (5, ["g_catalogue"]))

    def test_route_to_missing_anchor_fails(self):  # RARCH-2
        with tempfile.TemporaryDirectory() as d:
            copy_tree(d)
            with open(Path(d) / "catalogue" / "routes.tsv", "a", encoding="utf-8") as f:
                f.write("nonexistent thing\tvc.s2.nope\tR3\n")
            rc, o = run("--gate", "g_anchor", "--root", d)
        self.assertEqual((rc, o["failed"]), (5, ["g_anchor"]))

    def test_every_route_target_has_anchor_or_is_pending(self):
        rc, o = run("--gate", "g_anchor")
        self.assertEqual(rc, 0, o)

    def test_route_budget_gate_passes_and_catches_wrong_anchor(self):  # RARCH-3
        self.assertEqual(run("--gate", "g_route_budget")[0], 0)
        with tempfile.TemporaryDirectory() as d:
            copy_tree(d)
            q = Path(d) / "catalogue" / "route_queries.tsv"
            q.write_text(q.read_text(encoding="utf-8").replace("\tvc.s0.grid\n", "\tvc.s0.nope\n"), encoding="utf-8")
            rc, o = run("--gate", "g_route_budget", "--root", d)
        self.assertEqual((rc, o["failed"]), (5, ["g_route_budget"]))

    def test_hint_neutral_reported_na(self):
        rc, o = run("--all")
        if rc == 0:
            self.assertIn("NA:g_hint_neutral", o["receipt"])

    def test_vmem_compaction_abstains_or_has_path(self):  # RARCH-4
        rc, o, n = route("where does vmem compaction keep FAIL evidence")
        if rc == 0:
            self.assertTrue(all(r[1] and r[2] > 0 for r in o["r"]), o)
        else:
            self.assertEqual((rc, o["code"]), (3, "E_NAV_ABSTAIN"))
        self.assertLessEqual(n, 2000)

    def test_router_never_returns_pathless_anchor(self):
        for q in ("pack evidence for the judge", "open the visual memory store", "classify tile change jitter", "sig64", "quantum teleportation"):
            rc, o, _ = route(q)
            self.assertTrue(rc in (0, 3), q)
            self.assertTrue(all(r[1] and r[2] > 0 for r in o["r"]), (q, o))

    def test_navigability_ten_questions(self):
        want = {"PNG unfilter paeth": "vc.decode.unfilter", "grid size s": "vc.s0.grid", "format field id": "vc.ids.field_id",
                "hysteresis band": "vc.p.HYST_BAND_PERMILLE", "sig64": "vc.s1.describe_padded", "xh64 BLAKE2b": "vc.s1.xh64_of",
                "error code to exit code": "vc.errors.vqaerror", "profile params_sha256": "vc.p.effective",
                "FROZEN decided": "vc.p.FROZEN_M"}
        for q, a in want.items():
            rc, o, _ = route(q)
            self.assertEqual(rc, 0, q)
            self.assertIn(a, [r[0] for r in o["r"]], q)


class GateHardening(unittest.TestCase):
    def probe(self, rel, text, gate):
        with tempfile.TemporaryDirectory() as d:
            copy_tree(d)
            p = Path(d) / rel
            p.write_text(p.read_text(encoding="utf-8") + "\n" + text, encoding="utf-8")
            return run("--gate", gate, "--root", d)

    def test_nofloat_bypasses_closed(self):  # RARCH-6
        for snippet in ("import math\nQ = math.sqrt(2)\n", "Q = 3 ** -1\n", "import operator\nQ = operator.truediv(3, 2)\n",
                        "import statistics\n", "import numpy as np\nQ = np.mean([1, 2])\n", "Q = [1, 2].mean()\n",
                        "from math import log\n"):
            rc, o = self.probe("visioncore/desc.py", snippet, "g_nofloat")
            self.assertEqual(rc, 5, snippet)

    def test_nofloat_allows_integer_math(self):
        rc, _ = self.probe("visioncore/desc.py", "import math\nQ = math.isqrt(9) + math.gcd(4, 6) + 2 ** 3\n", "g_nofloat")
        self.assertEqual(rc, 0)

    def test_ids_bypasses_closed(self):  # RARCH-7
        for rel, snippet in (("visioncore/desc.py", "Q = 'f{}:t{:04x}'.format(1, 2)\n"),
                             ("visioncore/desc.py", "Q = 'f' + str(1) + ':t' + 'a'\n"),
                             ("vmem/store.py", "Q = f'r{4}:{5:04x}'\n"),
                             ("vmem/store.py", "Q = 'r_%06d_%s' % (1, 'ab')\n"),
                             ("vmem/store.py", "Q = 'T:%s:%s' % (1, 2)\n")):
            rc, o = self.probe(rel, snippet, "g_ids")
            self.assertEqual(rc, 5, snippet)

    def test_pure_covers_all_tools(self):  # RARCH-8
        for snippet in ("import os\nQ = os.urandom(4)\n", "Q = __import__('time')\n", "import time\n"):
            rc, o = self.probe("tools/route_code.py", snippet, "g_pure")
            self.assertEqual(rc, 5, snippet)

    def test_untabled_constant_in_l2(self):  # RARCH-11
        rc, o = self.probe("visioncore/pyramid.py", "_T_SECRET = 37\n", "g_threshold")
        self.assertEqual(rc, 5)

    def test_gitignore_has_sp(self):
        self.assertIn(".sp.*.md", (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())


class Golden(unittest.TestCase):
    def test_golden_gate_passes_on_repo(self):
        rc, o = run("--gate", "g_golden")
        self.assertEqual((rc, o.get("findings")), (0, None))

    def test_golden_gate_catches_flipped_threshold(self):  # RARCH-3 mutant: flipped T0 / hysteresis constant
        with tempfile.TemporaryDirectory() as d:
            copy_tree(d)
            p = Path(d) / "visioncore" / "params.py"
            s = p.read_text(encoding="utf-8")
            self.assertIn('"HYST_BAND_PERMILLE": (100,', s)
            p.write_text(s.replace('"HYST_BAND_PERMILLE": (100,', '"HYST_BAND_PERMILLE": (400,'), encoding="utf-8")
            rc, o = run("--gate", "g_golden", "--root", d)
        self.assertEqual((rc, o["failed"]), (5, ["g_golden"]))


if __name__ == "__main__":
    unittest.main()
