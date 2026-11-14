#!/usr/bin/env python3
"""HB2-09: span splicer. Edits {path, start, end, text} (byte offsets, half-open [start, end)); per file the spans must be pairwise
disjoint (E_OVERLAP); applied in descending offset on a pre-image copy; post-check = every edited Python file re-parses and every
untouched anchor is still SAME or SHIFT; undo = pre-image.

hb_splice.py --root R --edits edits.json [--anchors anchors.jsonl] [--undo-dir D]    (rc 0 ok, 3 E_OVERLAP, 4 post-check failed)
"""
import argparse, ast, difflib, os, shutil, sys, tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402
import hb_extract2 as X  # noqa: E402
import hb_anchor2 as A  # noqa: E402


def overlaps(spans):
    """True iff two half-open spans intersect. spans: [(start, end)]."""
    s = sorted(spans)
    return any(s[i][1] > s[i + 1][0] for i in range(len(s) - 1)) or any(a == b for a, b in zip(s, s[1:]) if a[0] == a[1] and a == b)


def spans_from_diff(base, new):
    """Changed byte spans in `base` (bytes) for a new version, from a line diff (half-open, pure insertions are zero-width at the line)."""
    bl, nl = base.splitlines(keepends=True), new.splitlines(keepends=True)
    offs = [0]
    for l in bl:
        offs.append(offs[-1] + len(l))
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=bl, b=nl, autojunk=False).get_opcodes():
        if tag != "equal":
            out.append((offs[i1], offs[i2]))
    return out


def branches_overlap(base, versions):
    """versions: {branch: bytes}. -> sorted list of (branchA, branchB) whose changed spans intersect or touch on the same lines."""
    sp = {b: spans_from_diff(base, v) for b, v in versions.items()}
    bad = []
    names = sorted(sp)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            for (s1, e1) in sp[a]:
                for (s2, e2) in sp[b]:
                    if s1 < e2 and s2 < e1 or (s1 == e1 and s2 <= s1 < e2) or (s2 == e2 and s1 <= s2 < e1) or (s1 == e1 == s2 == e2):
                        bad.append((a, b))
    return sorted(set(bad))


def splice(root, edits, anchors=None, apply=True):
    """-> {"ok", "code", "preimages": {path: bytes}, "detail"}. Does not touch the disk when apply is False or on E_OVERLAP."""
    by = {}
    for e in edits:
        by.setdefault(e["path"], []).append(e)
    for p, es in by.items():
        if overlaps([(e["start"], e["end"]) for e in es]):
            return {"ok": False, "code": "E_OVERLAP", "preimages": {}, "detail": p}
    pre = {p: L.readb(os.path.join(root, p)) for p in by}
    new = {}
    for p, es in by.items():
        data = pre[p]
        for e in sorted(es, key=lambda e: (e["start"], e["end"]), reverse=True):
            if not (0 <= e["start"] <= e["end"] <= len(data)):
                return {"ok": False, "code": "E_RANGE", "preimages": {}, "detail": p}
            data = data[:e["start"]] + e["text"].encode("utf-8") + data[e["end"]:]
        new[p] = data
    for p, data in new.items():
        if p.endswith(".py"):
            try:
                ast.parse(data.decode("utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                return {"ok": False, "code": "E_SYNTAX", "preimages": {}, "detail": p}
    if anchors is not None:
        tmp = tempfile.mkdtemp(prefix="hbsplice")
        try:
            for dp, dn, fn in os.walk(root):
                dn[:] = [d for d in dn if d not in (".git", "__pycache__")]
                for f in fn:
                    s = os.path.join(dp, f)
                    d = os.path.join(tmp, os.path.relpath(s, root))
                    os.makedirs(os.path.dirname(d), exist_ok=True)
                    shutil.copy2(s, d)
            for p, data in new.items():
                open(os.path.join(tmp, p), "wb").write(data)
            touched = {p: [(e["start"], e["end"]) for e in by[p]] for p in by}
            untouched = [a for a in anchors if a.get("kind") != "module" and (a["path"] not in touched or
                         not any(a["start_byte"] < max(e, s + 1) and s < a["end_byte"] for s, e in touched[a["path"]]))]
            res = A.classify(untouched, X.Repo(tmp), {}, None)
            bad = [r["id"] + ":" + r["class"] for r in res if r["class"] not in ("SAME", "SHIFT", "CHILD")]
            if bad:
                return {"ok": False, "code": "E_ANCHOR", "preimages": {}, "detail": ",".join(bad[:5])}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if apply:
        for p, data in new.items():
            with open(os.path.join(root, p), "wb") as f:
                f.write(data)
    return {"ok": True, "code": "OK", "preimages": pre, "detail": ""}


def undo(root, preimages):
    for p, data in preimages.items():
        with open(os.path.join(root, p), "wb") as f:
            f.write(data)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--edits", required=True)
    ap.add_argument("--anchors")
    ap.add_argument("--undo-dir")
    a = ap.parse_args(argv)
    anchors = L.read_jsonl(a.anchors) if a.anchors else None
    r = splice(a.root, L.jload(a.edits), anchors)
    if r["ok"] and a.undo_dir:
        for p, data in r["preimages"].items():
            d = os.path.join(a.undo_dir, p)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            open(d, "wb").write(data)
    sys.stdout.write(L.dumps({"ok": r["ok"], "code": r["code"], "detail": r["detail"]}) + "\n")
    return {"OK": 0, "E_OVERLAP": 3}.get(r["code"], 4)


if __name__ == "__main__":
    sys.exit(main())
