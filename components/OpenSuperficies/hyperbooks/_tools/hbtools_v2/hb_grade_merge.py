#!/usr/bin/env python3
"""Pooled-grading merge (D-7): two seats of different families (gemini, haiku) grade pooled pairs 0-3, binarised >= 2; disagreements go
to an Opus seat; raw agreement and Cohen kappa are exact Fractions printed as decimals. Metrics are then recomputed 'adjudicated'
(hb_eval --labels) while the raw numbers (unjudged = 0) are kept.

hb_grade_merge.py --a eval/grades_gemini.json --b eval/grades_haiku.json [--third eval/grades_opus.json]
                  --key eval/pool_key.json --out eval/adjudicated.json [--requests eval/adjudication_requests.jsonl] [--pairs eval/pool_pairs.jsonl]
"""
import argparse, os, sys
from fractions import Fraction as F

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402


def binar(g):
    return 1 if g >= 2 else 0


def kappa(pairs):
    """Cohen kappa of two binary raters (exact Fraction) or None when undefined."""
    n = len(pairs)
    if not n:
        return None
    po = F(sum(1 for a, b in pairs if a == b), n)
    pa, pb = F(sum(a for a, _ in pairs), n), F(sum(b for _, b in pairs), n)
    pe = pa * pb + (1 - pa) * (1 - pb)
    return None if pe == 1 else (po - pe) / (1 - pe)


def merge(a, b, third=None, ids=None):
    """a, b, third: {pair_id: grade 0..3}. Returns (final {pair_id: 0|1|None}, stats). None = disagreement without a third grade."""
    ids = sorted(ids if ids is not None else set(a) & set(b))
    final, dis, both = {}, [], []
    for i in ids:
        ga, gb = a.get(i), b.get(i)
        if ga is None or gb is None:
            final[i] = None
            continue
        ba, bb = binar(ga), binar(gb)
        both.append((ba, bb))
        if ba == bb:
            final[i] = ba
        else:
            dis.append(i)
            final[i] = binar(third[i]) if third and i in third else None
    k = kappa(both)
    st = {"n": len(ids), "graded_by_both": len(both), "agree": sum(1 for x, y in both if x == y),
          "raw_agreement": L.fmt_dec(F(sum(1 for x, y in both if x == y), len(both))) if both else None,
          "kappa": L.fmt_dec(k) if k is not None else None, "disagreements": dis, "unresolved": [i for i in dis if final[i] is None]}
    return final, st


def adjudicated(final, key):
    """{qid: sorted symbols judged relevant}."""
    out = {}
    for pid, v in final.items():
        if v == 1 and pid in key:
            q, s = key[pid]
            out.setdefault(q, []).append(s)
    return {q: sorted(v) for q, v in sorted(out.items())}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True)
    ap.add_argument("--b", required=True)
    ap.add_argument("--third")
    ap.add_argument("--key", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--requests")
    ap.add_argument("--pairs")
    a = ap.parse_args(argv)
    ga, gb = L.jload(a.a), L.jload(a.b)
    g3 = L.jload(a.third) if a.third and os.path.isfile(a.third) else None
    ids = [p["pair_id"] for p in L.read_jsonl(a.pairs)] if a.pairs else None
    final, st = merge(ga, gb, g3, ids)
    L.write_json(a.out, adjudicated(final, L.jload(a.key)))
    if a.requests:
        pairs = {p["pair_id"]: p for p in L.read_jsonl(a.pairs)} if a.pairs else {}
        L.write_jsonl(a.requests, [pairs.get(i, {"pair_id": i}) for i in st["disagreements"] if not (g3 and i in g3)])
    L.write_json(os.path.splitext(a.out)[0] + ".stats.json", st)
    print(L.dumps({k: v for k, v in st.items() if k not in ("disagreements",)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
