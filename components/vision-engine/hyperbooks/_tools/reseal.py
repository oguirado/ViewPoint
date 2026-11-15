"""Rerun every HB-VC receipt command and rewrite the receipt (JDOCS-2/3). Usage: python3 hyperbooks/_tools/reseal.py [id ...]
commit is set (tree_clean true) only when the whole tree is clean outside hyperbooks/ and build/; otherwise commit=null."""
import hashlib
import json
import glob
import os
import platform
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import admission  # noqa: E402

MAP = {"build/room_H4/sel.py": "hyperbooks/_tools/sel.py", "build/room_H6/rle.py": "hyperbooks/_tools/rle.py",
       "build/room_H2/mk.py": "hyperbooks/_tools/bench_desc.py"}
RUN = {"R-s0-grid-table": (["python3", "hyperbooks/_tools/grid_table.py"], "property"),
       "R-s6-z1-size": (["python3", "hyperbooks/_tools/z1_size.py"], "measurement"),
       "R-s6-anchors": (["python3", "hyperbooks/_tools/anchor_census.py", "visioncore/pack.py", "visioncore/cite.py"], "anchor-census"),
       "R-s7-vmem-inventory": (["python3", "hyperbooks/_tools/anchor_census.py", "vmem/*.py"], "measurement")}
EXTRA_SRC = {"R-s0-grid-table": ["visioncore/grid.py", "visioncore/layout.py"],
             "R-s6-z1-size": ["visioncore/pack.py", "visioncore/params.py"],
             "R-s6-anchors": [], "R-s7-vmem-inventory": []}


def main(ids):
    os.chdir(ROOT)
    try:
        os.sched_setaffinity(0, {5, 6, 7, 8, 9})
    except (AttributeError, OSError):
        pass
    dirty = admission._dirty(ROOT)
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    for f in sorted(glob.glob("hyperbooks/*/receipts/*.json")):
        rc = json.load(open(f))
        if ids and rc["id"] not in ids:
            continue
        argv, kind = RUN.get(rc["id"], ([MAP.get(a, a) for a in rc["argv"]], rc["kind"]))
        srcs = [p for p in admission._sources(rc) if p not in MAP]
        srcs += [MAP[p] for p in admission._sources(rc) if p in MAP]
        srcs += [a for a in argv if re.fullmatch(r"[\w./-]+\.py", a) and a not in srcs]
        srcs += [p for p in EXTRA_SRC.get(rc["id"], []) if p not in srcs]
        if rc["id"] == "R-s7-vmem-inventory":
            srcs = sorted(glob.glob("vmem/*.py")) + [p for p in srcs if p.startswith("tests/")] + ["hyperbooks/_tools/anchor_census.py"]
        if rc["id"] == "R-s6-anchors":
            srcs = sorted(set(srcs))
        srcs = sorted(set(srcs))
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=900)
        if "unittest" in argv:
            out = r.stderr + r.stdout
            n = int(re.search(r"Ran (\d+) test", out).group(1))
            res = "OK" if re.search(r"^OK", out, re.M) else "FAILED"
            fl = re.search(r"failures=(\d+)", out)
            er = re.search(r"errors=(\d+)", out)
            metrics = {"tests_run": n, "result": res, "failures": int(fl.group(1)) if fl else 0,
                       "errors": int(er.group(1)) if er else 0}
            if res != "OK":
                metrics["failing_tests"] = sorted(set(re.findall(r"^(?:ERROR|FAIL): (\S+)", out, re.M)))[:40]
            osha = hashlib.sha256(json.dumps(metrics, sort_keys=True).encode()).hexdigest()
        else:
            line = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "{}"
            metrics = json.loads(line)
            osha = hashlib.sha256(r.stdout.encode()).hexdigest()
        # tree_clean is about the whole tree: any dirty path outside hyperbooks/ and build/ makes the receipt unbound
        clean = not [d for d in dirty if not d.startswith(("hyperbooks/", "build/"))]
        new = {"schema": "hb.receipt.v1", "id": rc["id"], "kind": kind, "argv": argv, "cwd": ".",
               "commit": head if clean else None, "tree_clean": bool(clean),
               "sources": {p: admission.sha256_file(p) for p in srcs if os.path.isfile(p)},
               "exit_code": r.returncode, "skipped": 0, "output_sha256": osha, "metrics": metrics,
               "env": {"python": platform.python_version(), "platform": platform.platform(), "hashseed": "unset"}}
        with open(f, "w") as fh:
            json.dump(new, fh, indent=1, sort_keys=True)
        print(rc["id"], r.returncode, "clean" if clean else "unbound")


if __name__ == "__main__":
    main(set(sys.argv[1:]))
