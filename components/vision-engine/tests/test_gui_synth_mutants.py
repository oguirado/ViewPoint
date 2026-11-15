"""P1 mutant kill table: each mutant in tests/mutants/gui_synth/manifest.json is a single exact-text replacement applied to a
scratch copy of the sources; the named killer test modules must be EXECUTED against it and fail (rc != 0). The unmutated copy
must pass the same modules (control). A mutant whose `old` text is absent or ambiguous is an error, never a silent pass."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAN = os.path.join(REPO, "tests", "mutants", "gui_synth", "manifest.json")
COPY = ("eval", "visioncore", "vadapt", "vmem", "tools", "tests")


def _copy(dst):
    for d in COPY:
        shutil.copytree(os.path.join(REPO, d), os.path.join(dst, d),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "results", "fixtures", "golden", "px_mutants"))
    os.makedirs(os.path.join(dst, "eval", "results"), exist_ok=True)


def _run(cwd, mods):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, "-m", "unittest"] + mods, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)
    return r.returncode, (r.stdout + r.stderr)[-600:]


def manifest():
    with open(MAN, encoding="utf-8") as f:
        return json.load(f)


class TestMutants(unittest.TestCase):
    def test_manifest_has_fifteen(self):
        m = manifest()
        self.assertEqual([x["id"] for x in m["mutants"]], ["M%d" % i for i in range(1, 16)])
        for x in m["mutants"]:
            for key in ("id", "file", "old", "new", "killers", "desc"):
                self.assertIn(key, x)
            self.assertNotEqual(x["old"], x["new"])

    def test_control_unmutated_passes(self):
        m = manifest()
        mods = sorted({k for x in m["mutants"] for k in x["killers"]})
        with tempfile.TemporaryDirectory(prefix="gs_ctl_", dir=_scratch()) as d:
            _copy(d)
            rc, out = _run(d, mods)
            self.assertEqual(rc, 0, out)

    def test_every_mutant_killed_by_executed_test(self):
        survivors = []
        for x in manifest()["mutants"]:
            with tempfile.TemporaryDirectory(prefix="gs_%s_" % x["id"], dir=_scratch()) as d:
                _copy(d)
                p = os.path.join(d, x["file"])
                src = open(p, encoding="utf-8").read()
                self.assertEqual(src.count(x["old"]), 1, "%s: pattern must occur exactly once in %s" % (x["id"], x["file"]))
                open(p, "w", encoding="utf-8").write(src.replace(x["old"], x["new"]))
                rc, out = _run(d, x["killers"])
                if rc == 0:
                    survivors.append(x["id"])
        self.assertEqual(survivors, [], "surviving mutants")


def _scratch():
    p = os.path.join(REPO, "build", "room_T", "mut")
    os.makedirs(p, exist_ok=True)
    return p


if __name__ == "__main__":
    unittest.main()
