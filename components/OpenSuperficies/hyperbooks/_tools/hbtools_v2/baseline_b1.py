#!/usr/bin/env python3
"""B1 (D-5): router v2 integer BM25F, all weights 1000, B_f 750,000, over name + docstring + signature of non-test symbols; no abstention.

Deviation (DEVIATIONS.md): lcp5 is ON by default. D-5 says off, but the census value C-11 (recall@5 0.833) is only reproduced with prefix
classes, and a baseline without them is much weaker (DEV MRR 0.114); the stronger baseline is the conservative choice. `--lcp5 0` gives the D-5 text.
`--candidates census` uses the census candidate set (function/method symbols of tools/, signature = args + returns) for the C-11 reproduction.
baseline_b1.py --query TEXT [--class FIND|IMPACT|ABSENT|CLAIM] [--lcp5 0|1] [--candidates all|census] [--k 20]
"""
import argparse, os, sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402
import hb_index as I  # noqa: E402
import census2 as CE  # noqa: E402
import hb_extract2 as X  # noqa: E402

W = {"name": 1000, "sig": 1000, "doc": 1000}


def rank_all(root, text, lcp5=True, k=20):
    ix = I.Index(os.path.join(root, L.BOOK_SUB, "index"))
    S = L.bm25f_scores(ix.bm, L.toks(text), W, lcp5)
    order = ix.order(S)
    return [(ix.ids[d], S[d]) for d in order[:k]], ix


_CENSUS = {}


def rank_census(subject, text, lcp5=True, k=20, full=False):
    key = os.path.abspath(subject)
    if key not in _CENSUS:
        repo = X.Repo(subject)
        cands = CE.tool_callables(repo)
        docs = [{"name": L.toks(c["qual"] + " " + c["stem"]), "doc": L.toks(c["doc"]), "sig": L.toks(c["sig"])} for c in cands]
        _CENSUS[key] = (cands, L.bm25f_index(docs, ["name", "doc", "sig"]))
    cands, ix = _CENSUS[key]
    S = L.bm25f_scores(ix, L.toks(text), W, lcp5)
    keys = [c["id"].encode("utf-8") for c in cands]
    order = sorted(range(len(cands)), key=lambda d: (-S.get(d, 0), keys[d])) if full else L.rank_order(S, keys)
    return [(cands[d]["id"], S.get(d, 0)) for d in (order if full else order[:k])]


def answer(root, subject, text, cls, lcp5=True, candidates="all", k=20):
    if cls == "CLAIM":
        return {"system": "B1", "class": cls, "rows": [], "abstain": False, "claim": None, "na": True, "bytes": 0}
    rows = rank_census(subject, text, lcp5, k) if candidates == "census" else rank_all(root, text, lcp5, k)[0]
    out = {"system": "B1", "class": cls, "rows": [r[0] for r in rows], "scores": [r[1] for r in rows], "abstain": False, "claim": None, "na": False}
    out["bytes"] = len(L.canon(out))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    ap.add_argument("--class", dest="cls", default="FIND")
    ap.add_argument("--lcp5", type=int, default=1)
    ap.add_argument("--candidates", default="all", choices=["all", "census"])
    ap.add_argument("--root", default=L.HB)
    ap.add_argument("--k", type=int, default=20)
    a = ap.parse_args(argv)
    sys.stdout.write(L.dumps(answer(a.root, os.path.join(a.root, "subject"), a.query, a.cls, bool(a.lcp5), a.candidates, a.k)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
