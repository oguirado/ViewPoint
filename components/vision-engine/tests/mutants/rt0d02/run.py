"""RT0 D02 mutants M1-M5: apply each manifest edit to a COPY of the production file, run the executed tests against that copy.

Usage: PYTHONDONTWRITEBYTECODE=1 python3 tests/mutants/rt0d02/run.py
M1-M3 mutate a copy of visioncore/change.py, M4-M5 a copy of tools/gate_checks.py, both under build/room_T2/. The tests load the copies
through RT0D02_CHANGE_PATH / RT0D02_GATE_PATH. Production sources are never written; every child is timeout bounded; the unmutated
copies must be fully green first (control). Killed = oracle loaded, ran >0 checks, 0 errors and >=1 assertion failure
(errors such as IndexError or syntax errors never count).
"""
import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
SCRATCH = ROOT / "build" / "room_T2"
SOURCES = {"change": ROOT / "visioncore" / "change.py", "gate": ROOT / "tools" / "gate_checks.py"}
ORACLE = '''import json,sys,unittest
sys.path.insert(0,sys.argv[1])
suite=unittest.defaultTestLoader.loadTestsFromNames(sys.argv[2:])
def valid(s):
 return all(valid(x) if isinstance(x,unittest.TestSuite) else x.__class__.__name__ != '_FailedTest' for x in s)
loaded=valid(suite)
r=unittest.TestResult()
suite.run(r)
print(json.dumps({'loaded':loaded,'checks':r.testsRun,'failures':len(r.failures),'errors':len(r.errors),
 'details':[(str(t),s[-200:]) for t,s in r.failures+r.errors]}))
sys.exit(1 if r.failures or r.errors else 0)
'''
CONTROL = ["tests.test_rt0_d02"]


def oracle(env_paths, tests, tmp):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(tmp), **env_paths)
    p = subprocess.run([sys.executable, "-B", "-c", ORACLE, str(ROOT), *tests], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=900)
    try:
        data = json.loads(p.stdout.splitlines()[-1])
    except (ValueError, IndexError):
        data = {"loaded": False, "checks": 0, "failures": 0, "errors": 1, "details": p.stderr[-300:]}
    return p.returncode, data


def main():
    rows = json.loads((HERE / "manifest.json").read_text())["mutants"]
    SCRATCH.mkdir(parents=True, exist_ok=True)
    base = {k: v.read_text() for k, v in SOURCES.items()}
    with tempfile.TemporaryDirectory(prefix="run_", dir=SCRATCH) as tmp:
        tmp = Path(tmp)
        paths = {"change": tmp / "visioncore" / "change.py", "gate": tmp / "tools" / "gate_checks.py"}
        for k, pth in paths.items():
            pth.parent.mkdir()
            pth.write_text(base[k])
        scratch_tmp = tmp / "tmp"
        scratch_tmp.mkdir()
        env = {"RT0D02_CHANGE_PATH": str(paths["change"]), "RT0D02_GATE_PATH": str(paths["gate"])}
        rc, control = oracle(env, CONTROL, scratch_tmp)
        if rc or not control["loaded"] or control["checks"] < 1 or control["failures"] or control["errors"]:
            print(json.dumps({"status": "CONTROL_FAILED", "rc": rc, "oracle": control}))
            return 2
        results = []
        for row in rows:
            original = base[row["target"]]
            changed = original
            for e in row["edits"]:
                if changed.count(e["before"]) != 1:
                    raise ValueError("nonunique mutation anchor: " + row["id"])
                changed = changed.replace(e["before"], e["after"], 1)
            if changed == original:
                raise ValueError("inert mutant: " + row["id"])
            ast.parse(changed)
            paths[row["target"]].write_text(changed)
            try:
                rc, res = oracle(env, row["tests"], scratch_tmp)
            finally:
                paths[row["target"]].write_text(original)
            killed = rc == 1 and res["loaded"] and res["checks"] > 0 and res["errors"] == 0 and res["failures"] > 0
            results.append({"id": row["id"], "desc": row["desc"], "killed": killed, **res})
    receipt = {"schema": "rt0d02.mutants/v2", "control_checks": control["checks"], "total": len(results),
               "killed": sum(r["killed"] for r in results), "results": results}
    out = SCRATCH / f"receipt_{os.getpid()}.json"
    out.write_text(json.dumps(receipt, indent=1) + "\n")
    print(json.dumps({k: v for k, v in receipt.items() if k != "results"}, sort_keys=True), str(out))
    return 0 if receipt["killed"] == receipt["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
