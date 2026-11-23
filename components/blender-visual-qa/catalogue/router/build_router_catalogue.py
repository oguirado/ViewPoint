#!/usr/bin/env python3
"""Regenerate the router catalogue and hyperbooks/nodes.jsonl from code anchors, symbols and book sections.

Usage: build_router_catalogue.py [--root DIR] [--check]
Stdlib only, deterministic (sorted, canonical JSON). Reads the repo read-only; writes only
hyperbooks/nodes.jsonl and catalogue/router/catalogue/{symbols.jsonl,routes.tsv,thresholds.tsv,failures.tsv}.
rc 0 written/fresh, 4 stale (--check), 2 usage/bad input. Stdout: one canonical JSON line.
Node ids: code anchors keep their `# @anchor:` id; other public top-level defs get `<module id>.<name>`
(kind "symbol"); book sections are `hb:<HB_ID>#hb-sN` (kind "section"). Every node has a real path and line >= 1 (O-B11).
"""
import ast
import json
import os
import re
import sys

PKGS = ("blender_visual_qa", "bpy_side", "tools")
ANCHOR_LINE = re.compile(r"# @anchor:((?:bv|bpy|tools)(?:\.[A-Za-z0-9_]+){2,})\s*$")
H1 = re.compile(r"^# (HB-BVQA-[A-Za-z0-9-]+)")
SEC = re.compile(r'^##\s+(?:<a id="(hb-s[0-9]+)"></a>)?(?:<a id="[^"]+"></a>)*\s*(.*\S)\s*$')
CITE = re.compile(r"(?:bv|bpy|tools)(?:\.[A-Za-z0-9_]+){2,}")
OWNER_ANCHOR = {"BORACLES": "bv.oracles.scene.evaluate", "BDRIVE": "bv.journal.cycles_seed", "BTRUTH": "bpy.canon.cycles_seed",
                "BINT": "tools.gates.main"}
TOK = re.compile(r"[a-z0-9]+")
# Ordinary-English words that must not be the only reason a code symbol answers a query: an undocumented public def whose
# name contains one is plumbing, not a routing target (it made the off-domain query "quantum teleportation protocol" answer).
GENERIC_WORDS = frozenset({"protocol"})


def canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def variants(words):
    """Doc-side vocabulary expansion (the router stems nothing): each token plus its plural/singular and -ing/-ed stems."""
    out = []
    for w in words:
        out.append(w)
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            out.append(w[:-1])
        elif len(w) > 2 and w.isalpha():
            out.append(w + "s")
        for suf in ("ing", "ed"):
            if len(w) > len(suf) + 3 and w.endswith(suf):
                out.append(w[: -len(suf)])
    return out


def words(text):
    return TOK.findall(text.lower())


def module_id(rel):
    parts = rel[:-3].split("/")
    head = {"blender_visual_qa": "bv", "bpy_side": "bpy", "tools": "tools"}[parts[0]]
    rest = [p for p in parts[1:] if p != "__init__"]
    return ".".join([head] + rest)


def scan_code(root):
    nodes = {}
    for pkg in PKGS:
        base = os.path.join(root, pkg)
        for dp, dn, fn in os.walk(base):
            dn[:] = sorted(d for d in dn if d != "__pycache__")
            for f in sorted(fn):
                if not f.endswith(".py"):
                    continue
                rel = os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/")
                with open(os.path.join(root, rel), encoding="utf-8") as fh:
                    src = fh.read()
                lines = src.splitlines()
                try:
                    tree = ast.parse(src)
                except SyntaxError:
                    continue
                mid = module_id(rel)
                mdoc = (ast.get_docstring(tree) or "").strip().splitlines()
                mdoc = mdoc[0] if mdoc else ""
                defs = {}
                for n in tree.body:
                    if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                        doc = (ast.get_docstring(n) or "").strip().splitlines()
                        defs[n.lineno] = (n.name, doc[0] if doc else "")
                anchored = {}
                for i, ln in enumerate(lines, 1):
                    m = ANCHOR_LINE.search(ln)
                    if m:
                        tgt = i if i in defs else (i + 1 if i + 1 in defs else None)
                        anchored[m.group(1)] = tgt or i
                by_line = {}
                for aid, ln in anchored.items():
                    name, doc = defs.get(ln, (aid.rsplit(".", 1)[-1], ""))
                    nodes[aid] = {"id": aid, "kind": "anchor", "path": rel, "line": ln, "title": name, "purpose": doc, "cites": [], "module": mdoc}
                    by_line[ln] = aid
                for ln, (name, doc) in defs.items():
                    if name.startswith("_") or ln in by_line:
                        continue
                    if not doc and GENERIC_WORDS & set(words(name)):
                        continue
                    sid = mid + "." + name
                    if re.fullmatch(r"(?:bv|bpy|tools)(?:\.[A-Za-z0-9_]+){2,}", sid) and sid not in nodes:
                        nodes[sid] = {"id": sid, "kind": "symbol", "path": rel, "line": ln, "title": name, "purpose": doc, "cites": [], "module": mdoc}
    return nodes


def scan_books(root, known):
    out = {}
    d = os.path.join(root, "hyperbooks")
    if not os.path.isdir(d):
        return out
    for f in sorted(os.listdir(d)):
        if not (f.startswith("HB-BVQA-") and f.endswith(".md")):
            continue
        rel = "hyperbooks/" + f
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        hb = f[:-3]
        for ln in lines:
            m = H1.match(ln)
            if m:
                hb = m.group(1)
                break
        cur = None
        for i, ln in enumerate(lines, 1):
            m = SEC.match(ln)
            if m and not ln.startswith("###"):
                num = m.group(1) or "hb-l%d" % i
                cur = {"id": "hb:%s#%s" % (hb, num), "kind": "section", "path": rel, "line": i,
                       "title": m.group(2), "purpose": "", "cites": []}
                out[cur["id"]] = cur
            elif cur is not None:
                for c in CITE.findall(ln):
                    if c in known and c not in cur["cites"]:
                        cur["cites"].append(c)
                if not cur["purpose"] and ln.strip() and not ln.lstrip().startswith(("|", "```")):
                    cur["purpose"] = ln.strip()[:240]
    return out


def read_rows(path, ncols):
    rows = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.rstrip("\n")
                if ln.strip() and not ln.startswith("#"):
                    c = ln.split("\t")
                    if len(c) == ncols:
                        rows.append(c)
    return rows[1:] if rows and rows[0][0] in ("id", "code", "subject") else rows


def build(root):
    code = scan_code(root)
    books = scan_books(root, set(code))
    nodes = dict(code)
    nodes.update(books)
    for n in nodes.values():
        n["cites"] = sorted(n["cites"])
    ordered = [nodes[k] for k in sorted(nodes)]
    out = {"hyperbooks/nodes.jsonl": "".join(canon({k: v for k, v in n.items() if k != "module"}) for n in ordered)}
    sym = [{"anchor": n["id"], "layer": 4, "line": n["line"], "mutants": [], "owner": "BINT", "path": n["path"],
            "symbol": n["title"], "tests": []} for n in ordered]
    out["catalogue/router/catalogue/symbols.jsonl"] = "".join(canon(s) for s in sym)
    routes = set()
    for n in ordered:
        text = " ".join(variants(words(n["title"] + " " + n["purpose"] + " " + n.get("module", "") + " " + n["path"])))
        if text.strip():
            routes.add((text, n["id"], "R2"))
        if n["kind"] == "section":
            for c in n["cites"]:
                routes.add((" ".join(variants(words(n["title"]))), c, "R2"))
    for r in read_rows(os.path.join(root, "catalogue/routes.tsv"), 3):
        if r[1] in nodes:
            routes.add(tuple(r))
    out["catalogue/router/catalogue/routes.tsv"] = "".join("\t".join(r) + "\n" for r in sorted(routes))
    thr = []
    for r in read_rows(os.path.join(root, "catalogue/thresholds.tsv"), 6):
        anchor = OWNER_ANCHOR.get(r[3], "tools.gates.main")
        if anchor in nodes:
            thr.append(r[:3] + [anchor] + r[3:] + ["assumed"])
    out["catalogue/router/catalogue/thresholds.tsv"] = "".join("\t".join(r) + "\n" for r in sorted(thr))
    fail = []
    for r in read_rows(os.path.join(root, "catalogue/failures.tsv"), 6):
        fail.append(r[:3] + ["tools.gates.main"] + r[4:])
    out["catalogue/router/catalogue/failures.tsv"] = "".join("\t".join(r) + "\n" for r in sorted(fail))
    return out


def main(argv):
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    check, args = False, list(argv)
    while args:
        a = args.pop(0)
        if a == "--check":
            check = True
        elif a == "--root" and args:
            root = os.path.abspath(args.pop(0))
        else:
            sys.stdout.write(canon({"code": "E_USAGE", "rc": 2, "detail": {"arg": a}}))
            return 2
    files = build(root)
    stale = []
    for rel, data in sorted(files.items()):
        p = os.path.join(root, rel)
        cur = open(p, encoding="utf-8").read() if os.path.isfile(p) else None
        if cur != data:
            stale.append(rel)
            if not check:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p + ".tmp", "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(p + ".tmp", p)
    if check and stale:
        sys.stdout.write(canon({"code": "E_STALE", "rc": 4, "detail": {"stale": stale}}))
        return 4
    sys.stdout.write(canon({"code": "OK", "rc": 0, "written": [] if check else stale, "nodes": files["hyperbooks/nodes.jsonl"].count("\n")}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
