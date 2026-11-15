"""STORAGE LAW V1: the 7 ram.* mutants of vadapt/ramws.py are killed by oracle execution (rc 5 = killed).
Each mutant is a unique source edit applied to a scratch copy (build/room_V1/mut/); the green control runs first.
Usage: python3 -B tests/test_e15_ram_mutants.py  -> one JSON line per mutant, rc 5 when every mutant is killed (rc 0 = a survivor)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
T = "tests.test_ramws."
MUTANTS = [
    ("ram.tmpfs.01", '    return fst if fst in RAM_FS else None', '    return fst or "tmpfs"',
     [T + "TestLocation.test_non_tmpfs_refused", T + "TestLocation.test_is_ram_fs_longest_prefix"]),
    ("ram.quota.02", 'if _host_used(self._host, skip=self.root) + self.used + n > HOST_MAX:', 'if False:',
     [T + "TestQuota.test_host_sum"]),
    ("ram.release.03", 'self.used = max(0, self.used - freed)', 'self.used = self.used',
     [T + "TestPathRelease.test_release_accounting"]),
    ("ram.escape.04", 'if real != self._real and not real.startswith(self._real + os.sep):', 'if False:',
     [T + "TestPathRelease.test_escape"]),
    ("ram.janitor.05", 'if fd is None and not stale_boot:\n                continue', 'if False:\n                continue',
     [T + "TestJanitor.test_stale_vs_live_distinct_runs"]),
    ("ram.janitor.06", '    gone = []\n    for camp in', '    gone = []\n    return gone\n    for camp in',
     [T + "TestJanitor.test_stale_vs_live_distinct_runs", T + "TestJanitor.test_sigkilled_owner_swept_by_cli"]),
    ("ram.close.07", '        shutil.rmtree(self.root, ignore_errors=True)\n        for d in', '        for d in',
     [T + "TestPathRelease.test_close_idempotent", T + "TestPathRelease.test_context_manager"]),
]


def oracle(work, tests):
    p = subprocess.run([sys.executable, "-B", "-W", "ignore", "-m", "unittest", *tests], cwd=work, capture_output=True, text=True,
                       timeout=120, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    return p.returncode, p.stderr[-600:]


def run_all():
    scratch = ROOT / "build" / "room_V1" / "mut"
    scratch.mkdir(parents=True, exist_ok=True)
    out = []
    with tempfile.TemporaryDirectory(dir=scratch) as t:
        work = Path(t)
        (work / "vadapt").mkdir()
        (work / "tests").mkdir()
        (work / "build").mkdir()
        shutil.copy(ROOT / "vadapt" / "__init__.py", work / "vadapt")
        shutil.copy(ROOT / "tests" / "__init__.py", work / "tests")
        shutil.copy(ROOT / "tests" / "test_ramws.py", work / "tests")
        src = (ROOT / "vadapt" / "ramws.py").read_text(encoding="utf-8")
        tgt = work / "vadapt" / "ramws.py"
        tgt.write_text(src, encoding="utf-8")
        all_tests = sorted({x for m in MUTANTS for x in m[3]})
        rc, err = oracle(work, all_tests)
        out.append({"id": "control", "rc": rc, "green": rc == 0, "err": err if rc else ""})
        for mid, before, after, tests in MUTANTS:
            n = src.count(before)
            if n != 1:
                out.append({"id": mid, "rc": 2, "killed": False, "err": "anchor count %d" % n})
                continue
            tgt.write_text(src.replace(before, after, 1), encoding="utf-8")
            rc, err = oracle(work, tests)
            out.append({"id": mid, "rc": 5 if rc == 1 else rc, "killed": rc == 1, "err": ""})
        tgt.write_text(src, encoding="utf-8")
    return out


class TestRamMutants(unittest.TestCase):
    def test_all_killed_rc5(self):
        res = run_all()
        self.assertTrue(res[0]["green"], res[0])
        self.assertEqual([r["id"] for r in res[1:]], ["ram.%s.%02d" % (n, i) for i, n in enumerate(
            ("tmpfs", "quota", "release", "escape", "janitor", "janitor", "close"), 1)])
        for r in res[1:]:
            self.assertEqual((r["rc"], r["killed"]), (5, True), r)


if __name__ == "__main__":
    res = run_all()
    for r in res:
        print(json.dumps(r, sort_keys=True))
    sys.exit(5 if res[0]["green"] and all(r.get("killed") for r in res[1:]) else 0)
