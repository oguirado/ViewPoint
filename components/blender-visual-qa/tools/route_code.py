#!/usr/bin/env python3
"""Code router: map a task text to catalogue anchors (stdlib, integer BM25, no index file).

Usage: route_code.py --q "<text>" [--k 3] [--json]
Stdout: one canonical JSON line <= 2000 B:
  {"abstain":{"flag":bool,"reason":str|null},"code":..,"overflow":int,"r":[[anchor,path,line,score_ppm]..],"rc":int}
rc 0 answered, 3 abstained, 2 usage.
"""
import json
import os
import re
import sys

MAX_B = 2000
K1_MILLI, B_MILLI = 1200, 750
ZONE_W = {"anchor": 3, "symbol": 2, "purpose": 2, "text": 1}
STOP = frozenset("a an and are do does for how i in is it of on or the to what where which with".split())
FIELD_RE = re.compile(r"f(0|[1-9][0-9]{0,9}):t[0-9a-f]{4}|f(0|[1-9][0-9]{0,9}):all|r(0|[1-9][0-9]{0,9}):[0-9a-f]{4}")
ANCHOR_RE = re.compile(r"(bv|bpy|tools)(\.[A-Za-z0-9_]+){2,}")
NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{2,40}")


def canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def toks(s):
    return [t for t in re.findall(r"[a-z0-9]+", s.lower()) if t not in STOP]


def lg_milli(x_milli):
    """Monotone integer log2 of a milli-scaled value >= 1000 (piecewise-linear between powers of two)."""
    k = x_milli.bit_length() - 1
    return 1000 * k + (x_milli - (1 << k)) * 1000 // (1 << k)


def load(root):
    cat = os.path.join(root, "catalogue")
    docs = {}

    def doc(a):
        return docs.setdefault(a, {"path": "", "line": 0, "anchor": [], "symbol": [], "purpose": [], "text": []})

    def rows(name):
        p = os.path.join(cat, name)
        if not os.path.isfile(p):
            return []
        with open(p, encoding="utf-8") as f:
            return [ln.rstrip("\n").split("\t") for ln in f if ln.strip()]
    p = os.path.join(cat, "symbols.jsonl")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            for ln in f:
                if ln.strip():
                    r = json.loads(ln)
                    d = doc(r["anchor"])
                    d["path"], d["line"] = r["path"], r["line"]
                    d["symbol"] += toks(r["symbol"])
    for r in rows("routes.tsv"):
        doc(r[1])["purpose"] += toks(r[0])
    thr, fail = {}, {}
    for r in rows("thresholds.tsv"):
        thr[r[0]] = r[3]
        doc(r[3])["text"] += toks(" ".join([r[0], r[2], r[4], r[6]]))
    for r in rows("failures.tsv"):
        fail[r[0]] = r[3]
        doc(r[3])["text"] += toks(" ".join([r[0], r[4]]))
    for a, d in docs.items():
        d["anchor"] = toks(a)
    return docs, thr, fail


def exact(q, docs, thr, fail):
    s = q.strip()
    if FIELD_RE.fullmatch(s):
        a = "vc.ids.parse"
    elif ANCHOR_RE.fullmatch(s) and s in docs:
        a = s
    elif NAME_RE.fullmatch(s) and (s in thr or s in fail):
        a = thr.get(s) or fail.get(s)
    else:
        return None
    d = docs.get(a, {"path": "", "line": 0})
    return [[a, d["path"], d["line"], 1000000]]


def score(qt, docs):
    n = len(docs)
    lens = {}
    tfw = {}
    for a, d in docs.items():
        w = {}
        dl = 0
        for z, wz in ZONE_W.items():
            for t in d[z]:
                w[t] = w.get(t, 0) + wz
                dl += wz
        tfw[a], lens[a] = w, dl
    avg = max(1, sum(lens.values()) // max(1, n))
    out = {}
    for t in sorted(set(qt)):
        hit = [a for a in docs if t in tfw[a]]
        if not hit:
            continue
        df = len(hit)
        idf = lg_milli((2 * (n - df) + 1) * 1000 // (2 * df + 1) + 1000)
        for a in hit:
            tf = tfw[a][t]
            norm = 1000 - B_MILLI + B_MILLI * (lens[a] * 1000 // avg) // 1000
            den = tf * 1000 + K1_MILLI * norm // 1000
            frac = tf * (K1_MILLI + 1000) * 1000 // den
            out[a] = out.get(a, 0) + idf * frac
    return out


def route(q, k, root):
    docs, thr, fail = load(root)
    ex = exact(q, docs, thr, fail)
    if ex is not None and ex[0][1]:
        return {"abstain": {"flag": False, "reason": None}, "r": ex, "overflow": 0}
    qt = toks(q)
    sc = score(qt, docs)
    # only candidates that resolve to code (path:line) may be answered; the rest are future/unbuilt anchors
    ranked = sorted(((a, s_) for a, s_ in sc.items() if docs[a]["path"]), key=lambda kv: (-kv[1], kv[0]))
    if not ranked:
        why = "no query term hits a catalogue zone" if not sc else "no matching anchor has a code path"
        return {"abstain": {"flag": True, "reason": why}, "r": [], "overflow": 0}
    top = ranked[0][0]
    need = (len(set(qt)) + 2) // 3  # the best anchor must explain at least a third of the query terms
    got = sum(1 for t in set(qt) if any(t in docs[top][z] for z in ZONE_W))
    if got < need:
        return {"abstain": {"flag": True, "reason": "best anchor matches %d of %d query terms" % (got, len(set(qt)))},
                "r": [], "overflow": 0}
    res = [[a, docs[a]["path"], docs[a]["line"], s_] for a, s_ in ranked]
    return {"abstain": {"flag": False, "reason": None}, "r": res[:k], "overflow": max(0, len(res) - k)}


# @anchor:tools.routecode.main
def main(argv):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    q, k, args = None, 3, list(argv)
    try:
        while args:
            a = args.pop(0)
            if a == "--q":
                q = args.pop(0)
            elif a == "--k":
                k = int(args.pop(0))
            elif a == "--json":
                pass
            elif a == "--root":
                root = os.path.abspath(args.pop(0))
            else:
                raise ValueError(a)
        if q is None or not q.strip() or not 1 <= k <= 20:
            raise ValueError("--q")
    except (ValueError, IndexError) as e:
        sys.stdout.write(canon({"code": "E_USAGE", "rc": 2, "detail": {"arg": str(e)}}))
        return 2
    res = route(q, k, root)
    rc = 3 if res["abstain"]["flag"] else 0
    res["code"], res["rc"] = ("E_NAV_ABSTAIN" if rc else "OK"), rc
    while len(canon(res).encode("utf-8")) > MAX_B and res["r"]:
        res["r"].pop()
        res["overflow"] += 1
    sys.stdout.write(canon(res))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
