#!/usr/bin/env python3
"""D-6 freeze: out/freeze.json = sha256 of hbtools_v2/*.py, params.json, cards, book (+ derived + index), standards, fixtures, Godot fixture and sealed
mutant manifests, DEV files, grid, oracle extras, tests and the sealed visual TEST manifest. After the freeze nothing in that set may change (g_freeze).

hb_freeze.py --write [--root HB]      write out/freeze.json
hb_freeze.py --verify [--root HB]     rc 0 iff every recorded hash still matches (and no file was added to a frozen directory)
"""
import argparse, os, sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402

FROZEN_DIRS = ["hbtools_v2", "book", "standards", "fixtures", "godot_fixture", "oracle_extra", "tests", "eval"]
FROZEN_FILES = ["params.json", "out/subject.sha256.json", "out/visual_TEST_manifest.sha256", "out/visual_DEV_manifest.sha256", "out/cards.sha256"]
SKIP = ("__pycache__", ".git")
DEV_BENCH = os.path.join(os.path.dirname(L.HB), "bench", "hb_dev.jsonl")


def collect(root):
    files = {}
    for d in FROZEN_DIRS:
        base = os.path.join(root, d)
        for dp, dn, fn in os.walk(base):
            dn[:] = sorted(x for x in dn if x not in SKIP)
            for f in sorted(fn):
                if f.endswith(".pyc") or f.startswith(("pool_", "grades_", "adjudicat")):
                    continue
                p = os.path.join(dp, f)
                files[os.path.relpath(p, root).replace(os.sep, "/")] = L.sha_file(p)
    for f in FROZEN_FILES:
        p = os.path.join(root, f)
        if os.path.isfile(p):
            files[f] = L.sha_file(p)
    return dict(sorted(files.items()))


def write(root):
    files = collect(root)
    doc = {"schema": "hb.freeze.v2", "files": files, "dev_bench_sha256": L.sha_file(DEV_BENCH) if os.path.isfile(DEV_BENCH) else None,
           "files_sha256": L.sha256b(L.canon(files)), "n_files": len(files)}
    L.write_json(os.path.join(root, "out", "freeze.json"), doc)
    return doc


def verify(path, root=None):
    """-> (ok, reason). Recomputes the frozen set; a changed, missing or extra file is a failure."""
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(path)))
    if not os.path.isfile(path):
        return False, "no freeze file"
    doc = L.jload(path)
    now = collect(root)
    old = doc["files"]
    bad = sorted(k for k in set(old) | set(now) if old.get(k) != now.get(k))
    if bad:
        return False, "changed after freeze: " + ",".join(bad[:6])
    if doc.get("dev_bench_sha256") and os.path.isfile(DEV_BENCH) and L.sha_file(DEV_BENCH) != doc["dev_bench_sha256"]:
        return False, "DEV bench changed"
    return True, "ok"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--root", default=L.HB)
    a = ap.parse_args(argv)
    if a.write:
        d = write(a.root)
        print(L.dumps({"n_files": d["n_files"], "freeze_sha256": L.sha_file(os.path.join(a.root, "out", "freeze.json"))}))
        return 0
    if a.verify:
        ok, why = verify(os.path.join(a.root, "out", "freeze.json"), a.root)
        print(why)
        return 0 if ok else 5
    return 2


if __name__ == "__main__":
    sys.exit(main())
