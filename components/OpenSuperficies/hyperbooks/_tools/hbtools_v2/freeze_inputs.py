#!/usr/bin/env python3
"""T00: write out/subject.sha256.json (sorted relpath -> sha256) over the frozen subject tree."""
import argparse, hashlib, json, os, sys

SKIP_DIRS = {"__pycache__", ".git"}


def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for ch in iter(lambda: f.read(1 << 20), b""):
            h.update(ch)
    return h.hexdigest()


def walk(root):
    out = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in SKIP_DIRS)
        for f in sorted(fn):
            if f.endswith(".pyc"):
                continue
            p = os.path.join(dp, f)
            out[os.path.relpath(p, root).replace(os.sep, "/")] = sha_file(p)
    return dict(sorted(out.items()))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", default="subject")
    ap.add_argument("--out", default="out/subject.sha256.json")
    a = ap.parse_args(argv)
    m = walk(a.subject)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(m, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")
    print(len(m), "files")


if __name__ == "__main__":
    main()
