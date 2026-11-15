#!/usr/bin/env python3
"""T01: census C-1..C-16 of D-1 over the frozen subject. Values are measured; a mismatch with D-1 is reported, never fixed.

Definitions (declared assumptions where D-1 is silent):
  symbol bytes = UTF-8 bytes of the lines from the first decorator to end_lineno, each line with its LF;
  median = upper median (sorted[n // 2]); C-6 call edges = distinct CALLS pairs between function/method symbols of tools/;
  C-11/C-12 B1 = router v2 integer BM25F, weights 1000, lcp5 on, over name(qualname + module stem) + docstring + signature
  (args + returns) of function/method symbols of tools/; rank = position in the full (-S, id) order.
"""
import argparse, ast, json, os, sys
from fractions import Fraction as F

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402
import hb_extract2 as X  # noqa: E402

D1 = {"C-1": [25, 4240, 184725], "C-2": [172, 128, 94], "C-3": [29, 172], "C-4": [318, 6003], "C-5": [15, 11, 3], "C-6": [262, 33],
      "C-7": [110], "C-9": [2, 16, 20], "C-10": [1], "C-11": ["0.529", "0.833"], "C-12": [132, 1, 3, 23, 1], "C-13": [1, 1, 2, 10, 1],
      "C-14": [1, 5, 35], "C-16": [4, 4]}


def dev_rows(path):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def tool_callables(repo):
    out = []
    for m in repo.mods:
        if m.path.startswith("tools/"):
            for s in m.syms:
                if s["kind"] in ("function", "method"):
                    n = s["node"]
                    sig = ast.unparse(n.args) + ((" " + ast.unparse(n.returns)) if n.returns is not None else "")
                    out.append(dict(id=s["id"], path=m.path, stem=m.stem, qual=s["qual"], doc=ast.get_docstring(n) or "", sig=sig,
                                    line=s["start_line"], node=n))
    return out


def b1_rank(repo, cands, queries):
    docs = [{"name": L.toks(c["qual"] + " " + c["stem"]), "doc": L.toks(c["doc"]), "sig": L.toks(c["sig"])} for c in cands]
    ix = L.bm25f_index(docs, ["name", "doc", "sig"])
    keys = [c["id"].encode("utf-8") for c in cands]
    ranks = {}
    for q in queries:
        S = L.bm25f_scores(ix, L.toks(q["text"]), {"name": 1000, "doc": 1000, "sig": 1000}, True)
        full = sorted(range(len(cands)), key=lambda d: (-S.get(d, 0), keys[d]))
        ids = [cands[i]["id"] for i in full]
        ranks[q["qid"]] = min([ids.index(g) + 1 for g in q["gold"] if g in ids] or [0])
    return ranks, ix


def b2_hits(cands, queries):
    """B2: all symbols of tool files whose stem tokens meet the query tokens (D-5). Returns per-query (hit, n_returned)."""
    stems = sorted({c["path"] for c in cands})
    out = {}
    for q in queries:
        qt = set(L.toks(q["text"]))
        q5 = {t[:5] for t in qt}
        files = [p for p in stems if q5 & {t[:5] for t in os.path.splitext(os.path.basename(p))[0].split("_")}]
        syms = [c["id"] for c in sorted(cands, key=lambda c: (c["path"], c["line"])) if c["path"] in files]
        out[q["qid"]] = (int(any(g in syms for g in q["gold"])), len(syms))
    return out


def census(subject, dev_path, trace_path=None):
    repo = X.Repo(subject)
    x = X.extract(subject)
    c = {}
    paths = repo.files
    c["C-1"] = [len(paths), sum(m.nlines for m in repo.mods), sum(len(m.data) for m in repo.mods)]
    defs = lambda pre: [s for m in repo.mods if m.path.startswith(pre) for s in m.syms if s["kind"] in ("function", "method", "class", "test")]
    td, ts = defs("tools/"), defs("tests/")
    c["C-2"] = [len(td), len(ts), sum(1 for s in ts if s["kind"] == "test")]
    ndoc = sum(1 for s in td if ast.get_docstring(s["node"]))
    c["C-3"] = [ndoc, len(td)]
    sizes = [s["end_byte"] - s["start_byte"] for s in td]
    c["C-4"] = [L.median_up(sizes), max(sizes)]
    big = max(td, key=lambda s: s["end_byte"] - s["start_byte"])
    c["C-5"] = [sum(1 for v in sizes if v > 1800), sum(1 for v in sizes if v > 2000), sum(1 for v in sizes if v > 4000)]
    k = x["kinds"]
    tt = {(e[0], e[1]) for e in x["call_edges"] if e[0].startswith("tools/") and e[1].startswith("tools/")
          and k[e[0]] in ("function", "method") and k[e[1]] in ("function", "method")}
    imp = set()   # direct import statements between in-scope modules (star propagation excluded)
    for m in repo.mods:
        for n in m.import_nodes():
            names = [a.name for a in n.names] if isinstance(n, ast.Import) else ([n.module or ""] if not n.level else [])
            for nm in names:
                if nm in repo.stem and repo.stem[nm] != m.path:
                    imp.add((m.path, repo.stem[nm]))
    c["C-6"] = [len(tt), len(imp)]
    c["C-7"] = [len(x["static_test_edges"])]
    # C-8 from the trace receipt
    if trace_path and os.path.isfile(trace_path):
        tr = L.jload(trace_path)
        c["C-8"] = [tr["metrics"]["tests"], tr["metrics"]["pass_tests"], tr["metrics"]["failures"] + tr["metrics"]["errors"]]
    else:
        c["C-8"] = None
    # C-9 reverse call closure (transitive callers over CALLS among tool function/method symbols)
    ids = sorted({a for a, b in tt} | {b for a, b in tt} | {s["id"] for s in td if s["kind"] in ("function", "method")})
    ix = {s: i for i, s in enumerate(ids)}
    rev = [0] * len(ids)
    for a, b in tt:
        rev[ix[b]] |= 1 << ix[a]
    sizes9 = []
    for i in range(len(ids)):
        seen, fr = 1 << i, 1 << i
        while fr:
            nx = 0
            j = 0
            f = fr
            while f:
                if f & 1:
                    nx |= rev[j]
                f >>= 1
                j += 1
            nx &= ~seen
            seen |= nx
            fr = nx
        sizes9.append(bin(seen).count("1") - 1)
    c["C-9"] = [L.median_up(sizes9), L.pctl(sizes9, 90), max(sizes9)]
    n10 = 0
    for m in repo.mods:
        if m.path.startswith("tools/"):
            for n in ast.walk(m.tree):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("getattr", "__import__"):
                    n10 += 1
    c["C-10"] = [n10]
    dev = dev_rows(dev_path)
    cands = tool_callables(repo)
    find = [q for q in dev if q["class"] == "FIND"]
    imp_q = [q for q in dev if q["class"] == "IMPACT"]
    ranks, ixb = b1_rank(repo, cands, find + imp_q)
    rr = [F(1, ranks[q["qid"]]) if ranks[q["qid"]] else F(0) for q in find]
    c["C-11"] = [L.fmt_dec(sum(rr) / len(rr)), L.fmt_dec(F(sum(1 for q in find if 0 < ranks[q["qid"]] <= 5), len(find)))]
    c["C-12"] = [ranks[q["qid"]] for q in imp_q]
    # C-13 gold test among trace-covering tests
    c13 = None
    if trace_path and os.path.isfile(trace_path):
        tr = L.jload(trace_path)
        cov = {}
        for t, s in tr["edges"]:
            cov.setdefault(s, set()).add(t)
        size = tr["coverage"]
        c13 = []
        for q in imp_q:
            tgt = q["gold"][0]
            tname = set(L.toks(tgt.split("::")[1].replace("_", " ")))
            qt = set(L.toks(q["text"])) | tname
            tests = sorted(cov.get(tgt, ()), key=lambda t: (-len(set(L.toks(t.split("::")[1].replace("_", " "))) & qt), size.get(t, 0), t))
            g = q["gold_tests"][0] if q["gold_tests"] else None
            c13.append(tests.index(g) + 1 if g in tests else 0)
    c["C-13"] = c13
    b2 = b2_hits(cands, imp_q)
    c["C-14"] = [sum(v[0] for v in b2.values()), len(imp_q), max((v[1] for q in imp_q for v in [b2[q["qid"]]] if v[0]), default=0)]
    # C-15 query-term OOV share
    field_terms = ixb
    oov = {}
    for cl in ("FIND", "IMPACT", "ABSENT"):
        rows = []
        for q in dev:
            if q["class"] == cl:
                ts = sorted(set(L.toks(q["text"])))
                rows.append("%d/%d" % (sum(1 for t in ts if L.bm25f_vposting(field_terms, t, True)[1] == 0), len(ts)))
        oov[cl] = rows
    c["C-15"] = oov
    # C-16 receipt keys
    ok = []
    for q in dev:
        if q["class"] == "CLAIM" and q.get("receipt"):
            f, key = q["receipt"].split(":", 1)
            try:
                v = L.jload(os.path.join(subject, f))
                for tok in key.split("."):
                    v = v[int(tok)] if isinstance(v, list) else v[tok]
                ok.append(1)
            except (OSError, KeyError, ValueError, IndexError):
                ok.append(0)
    c["C-16"] = [sum(ok), len(ok)]
    meta = dict(largest_symbol=big["id"])
    return c, meta


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", default=L.SUBJECT)
    ap.add_argument("--dev", required=True)
    ap.add_argument("--out", default=os.path.join(L.HB, "out", "census.json"))
    ap.add_argument("--trace", default=None)
    a = ap.parse_args(argv)
    tp = a.trace or os.path.join(os.path.dirname(a.out), "trace.json")
    c, meta = census(a.subject, a.dev, tp)
    cmp = {}
    for k, v in D1.items():
        cmp[k] = {"design": v, "measured": c.get(k), "match": c.get(k) == v}
    doc = {"schema": "hb.receipt.v2", "kind": "build", "tool": "census2.py/2.0", "census": c, "design": D1, "compare": cmp, "meta": meta,
           "mismatch": sorted(k for k, v in cmp.items() if not v["match"]), "metrics": {"mismatch_count": sum(1 for v in cmp.values() if not v["match"])}}
    L.write_json(a.out, doc)
    print(L.dumps({"mismatch": doc["mismatch"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
