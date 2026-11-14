"""RT0 D01 mutants M1-M4: apply each manifest edit to a scratch copy, run the bound tests, require a real failure.

Usage: PYTHONDONTWRITEBYTECODE=1 python3 tests/mutants/rt0/run.py
Production sources are never written; every child is timeout bounded; the unmutated copy must be green first.
A mutant counts as killed only when the oracle loaded, ran >0 checks and reported >0 failures (syntax errors do not count).
"""
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
SCRATCH = ROOT / "build" / "room_T" / "mutants"
ORACLE = '''import json,sys,unittest
sys.path.insert(0,sys.argv[1])
suite=unittest.defaultTestLoader.loadTestsFromNames(sys.argv[2:])
def valid(s):
 return all(valid(x) if isinstance(x,unittest.TestSuite) else x.__class__.__name__ != '_FailedTest' for x in s)
loaded=valid(suite)
r=unittest.TestResult()
suite.run(r)
fails=len(r.failures)+len(r.errors)
print(json.dumps({'loaded':loaded,'checks':r.testsRun,'fails':fails,'details':[(str(t),s[-300:]) for t,s in r.failures+r.errors]}))
sys.exit(1 if fails else 0)
'''


def oracle(work, tests):
    p = subprocess.run([sys.executable, "-B", "-c", ORACLE, str(work), *tests], cwd=work,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), capture_output=True, text=True, timeout=300)
    try:
        data = json.loads(p.stdout.splitlines()[-1])
    except (ValueError, IndexError):
        data = {"loaded": False, "checks": 0, "fails": 0, "details": p.stderr[-300:]}
    return p.returncode, data


def main():
    rows = json.loads((HERE / "manifest.json").read_text())["mutants"]
    SCRATCH.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="run_", dir=SCRATCH) as tmp:
        work = Path(tmp) / "repo"
        work.mkdir()
        for name in ("visioncore", "tools", "tests", "vadapt"):
            shutil.copytree(ROOT / name, work / name, ignore=shutil.ignore_patterns("__pycache__", "mutants"))
        (work / "build").mkdir()
        rc, control = oracle(work, sorted({r["test"] for r in rows}))
        if rc or not control["loaded"] or control["checks"] < 1:
            print(json.dumps({"status": "CONTROL_FAILED", "rc": rc, "oracle": control}))
            return 2
        results = []
        for row in rows:
            path = work / row["file"]
            original = path.read_text()
            changed = original
            for e in row["edits"]:
                if changed.count(e["before"]) != 1:
                    raise ValueError("nonunique mutation anchor: " + row["id"])
                changed = changed.replace(e["before"], e["after"], 1)
            if changed == original:
                raise ValueError("inert mutant: " + row["id"])
            ast.parse(changed)
            try:
                path.write_text(changed)
                rc, res = oracle(work, [row["test"]])
            finally:
                path.write_text(original)
            killed = rc == 1 and res["loaded"] and res["checks"] > 0 and res["fails"] > 0
            results.append({"id": row["id"], "desc": row["desc"], "killed": killed, **res})
    receipt = {"schema": "rt0.mutants/v1", "control_checks": control["checks"], "total": len(results),
               "killed": sum(r["killed"] for r in results), "results": results}
    out = SCRATCH / f"receipt_{os.getpid()}.json"
    out.write_text(json.dumps(receipt, indent=1) + "\n")
    print(json.dumps({k: v for k, v in receipt.items() if k != "results"}, sort_keys=True), str(out))
    return 0 if receipt["killed"] == receipt["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
