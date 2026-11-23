#!/usr/bin/env python3
"""Router bench: recall@5 / MRR on a frozen query split (integer ppm, stdlib).

Usage: bench.py [--split FIT|DEV|TEST|ALL] [--freeze]
Stdout: one canonical JSON line. rc 0 at target, 5 below target (or a negative answered), 4 frozen-split sha mismatch, 2 usage.
Positives score the first accepted id in the router top-5 (accept entries ending in `*` match by prefix);
negatives must abstain (router rc 3). TEST thresholds: recall@5 >= 700000 ppm, MRR >= 500000 ppm [TARGET].
--freeze rewrites FROZEN.json (test_sha256 = sha256 of the TEST rows of queries.tsv); deliberate, never run by tests.
"""
import hashlib
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
K = 5
TARGET = {"recall5_ppm": 700000, "mrr_ppm": 500000}


def canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def load_router():
    spec = importlib.util.spec_from_file_location("route_code", os.path.join(ROOT, "tools", "route_code.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def read_queries(path=None):
    with open(path or os.path.join(HERE, "queries.tsv"), encoding="utf-8") as f:
        lines = [ln.rstrip("\n").split("\t") for ln in f if ln.strip()]
    assert lines[0] == ["qid", "split", "kind", "query", "accept"], lines[0]
    return [dict(zip(lines[0], r)) for r in lines[1:]]


def test_digest(queries):
    return hashlib.sha256("".join("\t".join(q[c] for c in ("qid", "split", "kind", "query", "accept")) + "\n"
                                  for q in queries if q["split"] == "TEST").encode()).hexdigest()


def hit(accept, anchor):
    return any(anchor.startswith(a[:-1]) if a.endswith("*") else anchor == a for a in accept)


def run(queries, route_root=None, router=None):
    rt = router or load_router()
    root = route_root or os.path.join(ROOT, "catalogue", "router")
    pos = [q for q in queries if q["kind"] == "pos"]
    neg = [q for q in queries if q["kind"] == "neg"]
    rr_sum, hits, rows = 0, 0, []
    for q in pos:
        res = rt.route(q["query"], K, root)
        ids = [r[0] for r in res["r"]][:K]
        acc = q["accept"].split(",")
        rank = next((i + 1 for i, a in enumerate(ids) if hit(acc, a)), 0)
        hits += 1 if rank else 0
        rr_sum += 1000000 // rank if rank else 0
        rows.append({"qid": q["qid"], "rank": rank, "abstain": res["abstain"]["flag"]})
    bad_neg = []
    for q in neg:
        if not rt.route(q["query"], K, root)["abstain"]["flag"]:
            bad_neg.append(q["qid"])
    n = max(1, len(pos))
    return {"n_pos": len(pos), "n_neg": len(neg), "recall5_ppm": hits * 1000000 // n, "mrr_ppm": rr_sum // n,
            "neg_answered": bad_neg, "misses": [r["qid"] for r in rows if not r["rank"]]}


def main(argv):
    split, freeze, args = "TEST", False, list(argv)
    while args:
        a = args.pop(0)
        if a == "--split" and args:
            split = args.pop(0)
        elif a == "--freeze":
            freeze = True
        else:
            sys.stdout.write(canon({"code": "E_USAGE", "rc": 2, "detail": {"arg": a}}))
            return 2
    if split not in ("FIT", "DEV", "TEST", "ALL"):
        sys.stdout.write(canon({"code": "E_USAGE", "rc": 2, "detail": {"split": split}}))
        return 2
    allq = read_queries()
    digest = test_digest(allq)
    fz = os.path.join(HERE, "FROZEN.json")
    if freeze:
        counts = {s: sum(1 for q in allq if q["split"] == s) for s in ("FIT", "DEV", "TEST")}
        with open(fz, "w", encoding="utf-8") as f:
            f.write(canon({"schema": "bvqa.router-bench-frozen/v1", "test_sha256": digest, "counts": counts, "target": TARGET, "k": K}))
    with open(fz, encoding="utf-8") as f:
        frozen = json.load(f)
    if frozen["test_sha256"] != digest:
        sys.stdout.write(canon({"code": "E_STALE", "rc": 4, "detail": {"reason": "test_split_changed"}}))
        return 4
    qs = allq if split == "ALL" else [q for q in allq if q["split"] == split]
    res = run(qs)
    ok = res["recall5_ppm"] >= TARGET["recall5_ppm"] and res["mrr_ppm"] >= TARGET["mrr_ppm"] and not res["neg_answered"]
    res.update({"code": "OK" if ok else "E_GATE", "rc": 0 if ok else 5, "split": split})
    sys.stdout.write(canon(res))
    return 0 if ok else 5


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
