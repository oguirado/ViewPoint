#!/usr/bin/env python3
"""Generate catalogue/*.tsv|json|jsonl from catalogue/*.src and the code anchors (stdlib only).

Usage: build_catalogue.py [--root DIR] [--check]
Prints one canonical JSON object {"code","rc",...}. rc 0 OK, 2 bad input/usage, 4 stale (--check).
Outputs are staged in memory and written via <file>.tmp + os.replace only when every input is valid.
"""
import json
import os
import re
import sys

ANCHOR_RE = re.compile(r"(?:^|\s)# @anchor:((?:vc|vm|va|tools)(?:\.[A-Za-z0-9_]+){2,})\s*$")
MODULE_RE = re.compile(r"^# @module (\{.*\})\s*$")
STATUS = ("proved", "measured", "assumed")
PKG_DIRS = ("visioncore", "vmem", "vadapt", "tools")
TSV = {"routes": 3, "thresholds": 8, "failures": 6}
JSON_SRC = ("layers", "subjects", "measurements")


class BuildError(Exception):
    def __init__(self, code, rc, msg):
        super().__init__(msg)
        self.code, self.rc, self.msg = code, rc, msg


def canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def read_tsv(path, ncols):
    rows = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) != ncols:
                raise BuildError("E_SCHEMA", 2, "%s:%d expected %d columns, got %d" % (path, n, ncols, len(cols)))
            rows.append(cols)
    return rows


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except ValueError as e:
        raise BuildError("E_SCHEMA", 2, "%s: %s" % (path, e))


def scan_symbols(root, layers):
    mods = layers.get("modules", {})
    rows, seen = [], {}
    for pkg in PKG_DIRS:
        base = os.path.join(root, pkg)
        for dp, dns, fns in os.walk(base):
            dns[:] = sorted(d for d in dns if d != "__pycache__")
            for fn in sorted(fns):
                if not fn.endswith(".py"):
                    continue
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                with open(full, encoding="utf-8") as f:
                    lines = f.read().split("\n")
                hdr = {}
                for ln in lines[:40]:
                    m = MODULE_RE.match(ln)
                    if m:
                        try:
                            hdr = json.loads(m.group(1))
                        except ValueError:
                            hdr = {}
                        break
                stem = fn[:-3]
                layer = hdr.get("layer")
                if layer is None and pkg == "visioncore":
                    layer = mods.get(stem, {}).get("layer")
                for i, ln in enumerate(lines, 1):
                    m = ANCHOR_RE.search(ln)
                    if not m:
                        continue
                    a = m.group(1)
                    if a in seen:
                        raise BuildError("E_ANCHOR", 3, "duplicate anchor %s (%s:%d, %s)" % (a, rel, i, seen[a]))
                    seen[a] = "%s:%d" % (rel, i)
                    tests = [hdr["oracle"]] if hdr.get("oracle") else []
                    rows.append({"anchor": a, "path": rel, "line": i, "symbol": a.rsplit(".", 1)[1],
                                 "layer": layer, "owner": hdr.get("owner"),
                                 "tests": tests, "mutants": list(hdr.get("mutants", []))})
    rows.sort(key=lambda r: r["anchor"])
    return rows


def build(root):
    cat = os.path.join(root, "catalogue")
    out = {}
    src = {}
    for name, n in TSV.items():
        p = os.path.join(cat, name + ".src")
        if not os.path.isfile(p):
            raise BuildError("E_IO", 2, "missing " + p)
        src[name] = read_tsv(p, n)
    for name in JSON_SRC:
        p = os.path.join(cat, name + ".src")
        if not os.path.isfile(p):
            raise BuildError("E_IO", 2, "missing " + p)
        src[name] = read_json(p)
    meas = src["measurements"]
    ids = set()
    for r in src["thresholds"]:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,40}", r[0]):
            raise BuildError("E_SCHEMA", 2, "bad threshold id " + r[0])
        if r[0] in ids:
            raise BuildError("E_SCHEMA", 2, "duplicate threshold " + r[0])
        ids.add(r[0])
        if not re.fullmatch(r"-?[0-9]+", r[1]):
            raise BuildError("E_FLOAT", 2, "non-integer value for " + r[0])
        if r[7] not in STATUS:
            raise BuildError("E_SCHEMA", 2, "bad status for %s: %s" % (r[0], r[7]))
        if r[7] == "measured" and r[6] not in meas:
            raise BuildError("E_SCHEMA", 2, "measured threshold %s has no measurements row %s" % (r[0], r[6]))
    for r in src["failures"]:
        if not re.fullmatch(r"[0-9]", r[1]) or r[1] in ("0", "1"):
            raise BuildError("E_SCHEMA", 2, "bad rc for " + r[0])
    for r in src["routes"]:
        if r[2] not in ("R2", "R3", "R4", "R5"):
            raise BuildError("E_SCHEMA", 2, "bad rung for " + r[0])
    for name in TSV:
        rows = sorted(src[name], key=lambda c: c)
        out[name + ".tsv"] = "".join("\t".join(c) + "\n" for c in rows)
    out["layers.json"] = canon(src["layers"])
    out["subjects.json"] = canon(src["subjects"])
    out["measurements.json"] = canon(meas)
    sym = scan_symbols(root, src["layers"])
    out["symbols.jsonl"] = "".join(canon(r) for r in sym)
    known = {r["anchor"] for r in sym}
    unresolved = sorted({r[1] for r in src["routes"]} - known)
    return out, {"files": len(out), "symbols": len(sym), "routes_unresolved": len(unresolved)}


def main(argv):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    check = False
    args = list(argv)
    while args:
        a = args.pop(0)
        if a == "--check":
            check = True
        elif a == "--root" and args:
            root = os.path.abspath(args.pop(0))
        else:
            sys.stdout.write(canon({"code": "E_USAGE", "rc": 2, "detail": {"arg": a}}))
            return 2
    try:
        out, info = build(root)
        cat = os.path.join(root, "catalogue")
        if check:
            stale = []
            for name, text in sorted(out.items()):
                p = os.path.join(cat, name)
                cur = open(p, encoding="utf-8").read() if os.path.isfile(p) else None
                if cur != text:
                    stale.append(name)
            if stale:
                sys.stdout.write(canon({"code": "E_STALE", "rc": 4, "detail": {"stale": stale}}))
                return 4
        else:
            tmps = []
            for name, text in sorted(out.items()):
                p = os.path.join(cat, name)
                with open(p + ".tmp", "w", encoding="utf-8", newline="\n") as f:
                    f.write(text)
                tmps.append(p)
            for p in tmps:
                os.replace(p + ".tmp", p)
        sys.stdout.write(canon({"code": "OK", "rc": 0, "detail": info}))
        return 0
    except BuildError as e:
        sys.stdout.write(canon({"code": e.code, "rc": e.rc, "detail": {"msg": e.msg}}))
        return e.rc


# @anchor:tools.buildcatalogue.main
if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
