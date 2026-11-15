#!/usr/bin/env python3
"""Mutant runner. Copies the needed tree into build/gate_disk/mut_<pid>/ (never mutates the tracked tree), applies each
mutant from mutants.json in turn, checks it still compiles (precheck rc 0), runs its oracle and requires rc != 0 (KILLED).
  run_mutants.py [--only ID]    rc 0 all killed, 2 usage, 5 survivor / invalid mutant. Never 1.
"""
import json
import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
from vision import pin  # noqa: E402  (ADV-08: pin first)

pin.activate()

COPY = ["src", "cmd", "go.mod", "go.sum", "vision", "config", "docs", "testdata/cu", "scripts/cu", "capsule", "bench", "docker", ".gitignore", "hyperbooks", "contracts", "eval"]
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def make_copy(dst):
    for rel in COPY:
        s, d = os.path.join(REPO, rel), os.path.join(dst, rel)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        if os.path.isdir(s):
            shutil.copytree(s, d, ignore=IGNORE)
        else:
            shutil.copy2(s, d)
    os.makedirs(os.path.join(dst, "build", "wheels"), exist_ok=True)
    for w in os.listdir(os.path.join(REPO, "build", "wheels")):
        shutil.copy2(os.path.join(REPO, "build", "wheels", w), os.path.join(dst, "build", "wheels", w))


def sh(cmd, cwd):
    env = dict(os.environ, PATH=os.environ.get("PATH", "") + ":/usr/local/go/bin", CGO_ENABLED="0", PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run(["taskset", "-c", os.environ.get("CU_CPUSET", "10-14"), "timeout", "280", "bash", "-c", cmd], cwd=cwd, env=env, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr)[-300:]


def main(argv):
    only = None
    if argv:
        if len(argv) == 2 and argv[0] == "--only":
            only = argv[1]
        else:
            sys.stderr.write("usage: run_mutants.py [--only ID]\n")
            return 2
    spec = json.load(open(os.path.join(REPO, "scripts", "cu", "mutants.json")))["mutants"]
    if only:
        spec = [m for m in spec if m["id"] == only]
        if not spec:
            return 2
    dst = os.path.join(REPO, "build", "gate_disk", "mut_%d" % os.getpid())
    results = []
    try:
        make_copy(dst)
        for m in spec:
            path = os.path.join(dst, m["file"])
            orig = open(path, encoding="utf-8").read()
            if orig.count(m["find"]) != 1:
                results.append((m["id"], "INVALID", "find matched %d times" % orig.count(m["find"])))
                continue
            try:
                open(path, "w", encoding="utf-8").write(orig.replace(m["find"], m["replace"]))
                prc, pout = sh(m["precheck"], dst)
                if prc:
                    results.append((m["id"], "INVALID", "precheck rc %d: %s" % (prc, pout)))
                    continue
                orc, oout = sh(m["oracle"], dst)
                results.append((m["id"], "KILLED" if orc else "SURVIVED", "oracle rc %d" % orc))
            finally:
                open(path, "w", encoding="utf-8").write(orig)
    except Exception as e:
        results.append(("runner", "INVALID", repr(e)))
    finally:
        shutil.rmtree(dst, ignore_errors=True)
    killed = sum(1 for _, s, _ in results if s == "KILLED")
    for r in results:
        print("%-28s %-9s %s" % r)
    print("%d/%d killed" % (killed, len(spec)))
    return 0 if killed == len(spec) and results else 5


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
