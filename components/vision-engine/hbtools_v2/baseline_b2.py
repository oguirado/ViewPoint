#!/usr/bin/env python3
"""B2 (D-5): naive IMPACT baseline. All function/method symbols of the tool files whose stem tokens (split on `_`) meet the query tokens on
the lcp5 class, file then line order; tests = all test methods of tests/test_* files sharing a stem token with those files (reproduces C-14).
baseline_b2.py --query TEXT"""
import argparse, os, sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402


def stems(path):
    return [t for t in os.path.splitext(os.path.basename(path))[0].split("_") if t]


def impact(root, text):
    syms = L.read_jsonl(os.path.join(root, L.BOOK_SUB, "symbols.jsonl"))
    q5 = {t[:5] for t in L.toks(text)}
    tool_files = sorted({s["path"] for s in syms if s["path"].startswith("tools/")})
    hit = [p for p in tool_files if q5 & {t[:5] for t in stems(p)}]
    rows = [s["id"] for p in hit for s in sorted((s for s in syms if s["path"] == p and s["kind"] in ("function", "method")), key=lambda s: s["span"]["start_line"])]
    toks = {t for p in hit for t in stems(p)}
    test_files = sorted({s["path"] for s in syms if s["path"].startswith("tests/test_")})
    tests = [s["id"] for p in test_files if toks & set(stems(p)) for s in sorted((s for s in syms if s["path"] == p and s["kind"] == "test"), key=lambda s: s["span"]["start_line"])]
    return rows, tests


def answer(root, text, cls):
    if cls != "IMPACT":
        return {"system": "B2", "class": cls, "rows": [], "abstain": False, "claim": None, "na": True, "bytes": 0}
    rows, tests = impact(root, text)
    out = {"system": "B2", "class": cls, "rows": rows, "tests": tests, "abstain": False, "claim": None, "na": False}
    out["bytes"] = len(L.canon(out))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    ap.add_argument("--class", dest="cls", default="IMPACT")
    ap.add_argument("--root", default=L.HB)
    a = ap.parse_args(argv)
    sys.stdout.write(L.dumps(answer(a.root, a.query, a.cls)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
