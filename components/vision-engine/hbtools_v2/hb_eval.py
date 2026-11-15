#!/usr/bin/env python3
"""D-7 evaluation harness. Every system answers every query in a fresh process (its CLI); metrics are exact Fractions printed as
3-decimal strings (nDCG uses `decimal` logarithms); no floats reach an artefact.

hb_eval.py --split DEV|TEST --systems v2,B0,B1,B2[,v2-nocard,v2-nosec,v2-b1fields] [--lock] [--dev-aug eval/dev_aug.jsonl]
           [--labels eval/adjudicated.json] [--out out/run_<split>.json] [--jobs 8]

The TEST file is never opened unless `--lock` is given and `out/freeze.json` verifies (hb_freeze.verify); DEV runs refuse paths under bench/hb_test*.
Class of a query comes from its `intent` (find_code -> FIND/ABSENT, impact -> IMPACT, check_claim -> CLAIM). Abstention is scored on FIND and ABSENT rows.
"""
import argparse, concurrent.futures as cf, itertools, json, os, random, subprocess, sys
from decimal import Decimal, getcontext
from fractions import Fraction as F

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402

getcontext().prec = 40
BENCH_DIR = os.path.abspath(os.environ["HBV2_BENCH"]) if os.environ.get("HBV2_BENCH") else os.path.join(os.path.dirname(L.HB), "bench")
DEV_PATH = os.path.join(BENCH_DIR, "hb_dev.jsonl")
TEST_PATH = os.path.join(BENCH_DIR, "hb_test.jsonl")
CLI = {"v2": ("hb_query.py", []), "v2-nocard": ("hb_query.py", ["--ablate", "nocard"]), "v2-nosec": ("hb_query.py", ["--ablate", "nosec"]),
       "v2-b1fields": ("hb_query.py", ["--ablate", "b1fields"]), "B0": ("baseline_b0.py", []), "B1": ("baseline_b1.py", []), "B2": ("baseline_b2.py", [])}
INTENT_CLASS = {"find_code": "FIND", "impact": "IMPACT", "check_claim": "CLAIM"}
BUDGET = 2000


class EvalError(Exception):
    def __init__(self, code, detail=""):
        super().__init__("%s %s" % (code, detail))
        self.code = code


# ---- reading the bench with the blind guard --------------------------------------------------------------
def guard_path(path, lock_ok):
    base = os.path.basename(path)
    if base.startswith("hb_test") or "hb_bench" in base or os.path.abspath(path) == TEST_PATH:
        if not lock_ok:
            raise EvalError("E_LOCKED", "TEST requires --lock and a verified freeze")
    if os.sep + "P1" + os.sep + "bench" in os.path.abspath(path):
        raise EvalError("E_BLIND", "P1 bench")


def read_rows(path, split, lock_ok=False):
    guard_path(path, lock_ok)
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    if split == "DEV" and any(r.get("split") == "TEST" for r in rows):
        raise EvalError("E_BLIND", "TEST rows in DEV file")
    return rows


def row_class(r):
    """Evaluation class of a bench row."""
    if r["class"] in ("FIND", "ABSENT", "IMPACT", "CLAIM"):
        return r["class"]
    return INTENT_CLASS.get(r.get("intent"), "FIND")


def query_text(r):
    if r["class"] == "CLAIM" and r.get("receipt"):
        return r["text"] + " " + r["receipt"]
    return r["text"]


def system_class(r):
    c = row_class(r)
    return "FIND" if c == "ABSENT" else c


# ---- running systems ---------------------------------------------------------------------------------------
def run_system(system, r, root=None):
    tool, extra = CLI[system]
    cmd = [sys.executable, os.path.join(L.HERE, tool), "--class", system_class(r)] + extra
    if tool == "hb_query.py":
        cmd += ["--root", root or L.HB, query_text(r)]
    else:
        cmd += ["--query", query_text(r)]
    p = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    out = p.stdout
    try:
        o = json.loads(out)
    except ValueError:
        return {"system": system, "rows": [], "abstain": False, "claim": None, "na": True, "bytes": len(out.encode("utf-8")), "error": (p.stderr or "")[-160:],
                "rc": p.returncode}
    return normalise(system, o, len(out.encode("utf-8")), p.returncode)


def normalise(system, o, nbytes, rc):
    if o.get("schema") == "hb.answer.v2":
        imp = o.get("impact")
        res = {"system": system, "rows": [x[0] for x in o["r"]], "abstain": bool(o["abstain"]["flag"]), "claim": (o.get("claim") or {}).get("verdict"),
               "na": False, "bytes": nbytes, "rc": rc, "class": o["class"]}
        if imp:
            res["chg"] = imp["chg"]
            res["tests"] = imp["tests"]
        return res
    o = dict(o)
    o["bytes"] = o.get("bytes") or nbytes
    o["rc"] = rc
    return o


# ---- metrics ---------------------------------------------------------------------------------------------
def dcg(rel_flags):
    tot = Decimal(0)
    for i, f in enumerate(rel_flags, 1):
        if f:
            tot += Decimal(1) / (Decimal(i + 1).ln() / Decimal(2).ln())
    return tot


def find_metrics(rows, gold, extra=None):
    """rows: ranked symbol ids; gold: authored gold list; extra: judged-relevant ids (adjudicated mode). -> dict of Fractions + nDCG Decimal."""
    extra = set(extra or ())
    g = set(gold)
    rel = g | extra
    first = next((i for i, x in enumerate(rows, 1) if x in rel), None)
    rr = F(1, first) if first else F(0)

    def rec(k):
        top = set(rows[:k])
        return min(F(1), F(len(top & rel), max(1, len(g))))
    flags = [x in rel for x in rows[:5]]
    ideal = dcg([True] * min(5, max(1, len(g))))
    nd = min(Decimal(1), dcg(flags) / ideal) if ideal else Decimal(0)
    return {"rr": rr, "r1": rec(1), "r5": rec(5), "ndcg5": nd}


def impact_metrics(chg, tests, gold, gold_tests, extra=None, cut=5, cut_t=3):
    extra = set(extra or ())
    g = set(gold)
    top = chg[:cut] if cut else chg
    hit = F(len((set(top) & (g | extra))), max(1, len(g)))
    prec = F(len([x for x in top if x in g | extra]), max(1, len(top))) if top else F(0)
    gt = set(gold_tests)
    trec = F(len(set(tests[:cut_t]) & gt), len(gt)) if gt else None
    return {"srec": min(F(1), hit), "prec": prec, "trec": trec}


def mean(vals):
    vals = list(vals)
    return sum(vals, F(0)) / len(vals) if vals else None


def dec(x, p=3):
    if x is None:
        return None
    return L.fmt_dec(F(x) if not isinstance(x, Decimal) else F(x), p)


def decd(x, p=3):
    """Decimal -> string at p places (round half up)."""
    return L.fmt_dec(F(str(x)), p) if x is not None else None


def abstention_scores(pairs):
    """pairs: [(abstained, is_absent)] -> precision, recall, F1 as Fractions (positive = ABSENT); None when undefined."""
    tp = sum(1 for a, p in pairs if a and p)
    fp = sum(1 for a, p in pairs if a and not p)
    fn = sum(1 for a, p in pairs if not a and p)
    prec = F(tp, tp + fp) if tp + fp else None
    rec = F(tp, tp + fn) if tp + fn else None
    f1 = F(2 * tp, 2 * tp + fp + fn) if (2 * tp + fp + fn) else None
    return {"p": prec, "r": rec, "f1": f1, "tp": tp, "fp": fp, "fn": fn}


def sign_flip_p(deltas):
    """Two-sided exact sign-flip permutation p on the sum of paired deltas (Fractions): 2^n flips if n <= 20, else 100000 from Random(0)."""
    d = [F(x) for x in deltas]
    d = [x for x in d if x != 0]
    if not d:
        return F(1)
    obs = abs(sum(d))
    n = len(d)
    if n <= 20:
        ge = 0
        for signs in itertools.product((1, -1), repeat=n):
            ge += abs(sum(s * x for s, x in zip(signs, d))) >= obs
        return F(ge, 2 ** n)
    rnd = random.Random(0)
    ge = 0
    for _ in range(100000):
        ge += abs(sum(x if rnd.random() < 0.5 else -x for x in d)) >= obs
    return F(ge + 1, 100001)


def byte_stats(sizes):
    s = sorted(sizes)
    if not s:
        return {"n": 0}
    return {"n": len(s), "max": s[-1], "median": s[len(s) // 2], "within": sum(1 for x in s if x <= BUDGET), "share_within": dec(F(sum(1 for x in s if x <= BUDGET), len(s)))}


# ---- scoring a whole run ---------------------------------------------------------------------------------
def score(rows, results, judged=None):
    """rows: bench rows; results: {system: {qid: normalised result}}; judged: {qid: set(symbols)} adjudicated relevant extras."""
    judged = judged or {}
    out, rrs = {}, {}
    for sysname, res in results.items():
        m = {"counts": {}, "FIND": None, "IMPACT": None, "CLAIM": None, "ABSENT": None}
        find_raw, find_adj, imp_raw, imp_adj, claim, absent_pairs, sizes = [], [], [], [], [], [], []
        rr_q = {}
        for r in rows:
            c = row_class(r)
            m["counts"][c] = m["counts"].get(c, 0) + 1
            x = res.get(r["qid"])
            if x is None:
                continue
            sizes.append(x.get("bytes", 0))
            if c == "FIND" and not x.get("na"):
                fm = find_metrics(x["rows"], r["gold"])
                fa = find_metrics(x["rows"], r["gold"], judged.get(r["qid"]))
                find_raw.append(fm)
                find_adj.append(fa)
                rr_q[r["qid"]] = fa["rr"]
            if c in ("FIND", "ABSENT"):
                absent_pairs.append((bool(x.get("abstain")) and not x.get("na"), c == "ABSENT"))
            if c == "IMPACT":
                if x.get("na"):
                    continue
                chg = x.get("chg")
                tests = x.get("tests") or []
                if chg is None:                              # baselines: ranked rows
                    chg = x["rows"]
                    cut = 0 if sysname == "B2" else 5       # B2 returns a set; D-7 A2 compares against its whole returned set
                else:
                    cut = 5
                imp_raw.append(impact_metrics(chg, tests, r["gold"], r.get("gold_tests", []), None, cut))
                imp_adj.append(impact_metrics(chg, tests, r["gold"], r.get("gold_tests", []), judged.get(r["qid"]), cut))
                rr_q.setdefault(r["qid"], impact_metrics(chg, tests, r["gold"], r.get("gold_tests", []), judged.get(r["qid"]), cut)["srec"])
            if c == "CLAIM" and not x.get("na"):
                claim.append((r.get("verdict"), x.get("claim")))
        if find_raw:
            m["FIND"] = {"n": len(find_raw), "raw": fmt_find(find_raw), "adjudicated": fmt_find(find_adj)}
        if imp_raw:
            m["IMPACT"] = {"n": len(imp_raw), "raw": fmt_imp(imp_raw), "adjudicated": fmt_imp(imp_adj)}
        if claim:
            contra = [(g, p) for g, p in claim if g == "contradicted"]
            m["CLAIM"] = {"n": len(claim), "accuracy": dec(F(sum(1 for g, p in claim if g == p), len(claim))),
                          "contradicted_n": len(contra), "contradicted_caught": dec(F(sum(1 for g, p in contra if p == "contradicted"), len(contra))) if contra else None}
        if absent_pairs:
            a = abstention_scores(absent_pairs)
            m["ABSENT"] = {"n": len(absent_pairs), "absent_n": sum(1 for _, p in absent_pairs if p), "precision": dec(a["p"]), "recall": dec(a["r"]), "f1": dec(a["f1"]) if a["f1"] is not None else "0.000",
                           "tp": a["tp"], "fp": a["fp"], "fn": a["fn"]}
        m["bytes"] = byte_stats(sizes)
        out[sysname] = m
        rrs[sysname] = {q: "%d/%d" % (v.numerator, v.denominator) for q, v in rr_q.items()}
    return out, rrs


def fmt_find(ms):
    return {"recall_at_1": dec(mean(m["r1"] for m in ms)), "recall_at_5": dec(mean(m["r5"] for m in ms)), "mrr": dec(mean(m["rr"] for m in ms)),
            "ndcg_at_5": decd(sum((m["ndcg5"] for m in ms), Decimal(0)) / len(ms))}


def fmt_imp(ms):
    tr = [m["trec"] for m in ms if m["trec"] is not None]
    return {"symbol_recall": dec(mean(m["srec"] for m in ms)), "precision_chg": dec(mean(m["prec"] for m in ms)), "test_recall_at_3": dec(mean(tr)) if tr else None}


def paired_p(rows, results, a, b):
    """Exact sign-flip p of per-query FIND RR (a minus b), over rows both systems answered."""
    deltas = []
    for r in rows:
        if row_class(r) != "FIND":
            continue
        xa, xb = results[a].get(r["qid"]), results[b].get(r["qid"])
        if not xa or not xb or xa.get("na") or xb.get("na"):
            continue
        deltas.append(find_metrics(xa["rows"], r["gold"])["rr"] - find_metrics(xb["rows"], r["gold"])["rr"])
    return {"n": len(deltas), "p": dec(sign_flip_p(deltas)) if deltas else None}


# ---- pooled pairs ------------------------------------------------------------------------------------------
def pool_pairs(rows, results, symbols, key=None):
    """Top-5 FIND/IMPACT symbols outside gold from any system -> deduplicated, system-blind, shuffled (seed 7) pair records."""
    seen, pairs = {}, []
    for r in rows:
        c = row_class(r)
        if c not in ("FIND", "IMPACT"):
            continue
        gold = set(r["gold"])
        for sysname in sorted(results):
            x = results[sysname].get(r["qid"])
            if not x or x.get("na"):
                continue
            ranked = x.get("chg") if x.get("chg") is not None else x["rows"]
            for sid in ranked[:5]:
                if sid in gold or sid not in symbols or (r["qid"], sid) in seen:
                    continue
                s = symbols[sid]
                seen[(r["qid"], sid)] = 1
                if key is not None:
                    key[L.sha16(("%s|%s" % (r["qid"], sid)).encode())[:12]] = [r["qid"], sid]
                pairs.append({"pair_id": L.sha16(("%s|%s" % (r["qid"], sid)).encode())[:12], "qid_hash": L.sha16(r["qid"].encode())[:12], "text": r["text"], "symbol": sid,
                              "card": (s.get("card") or {}).get("purpose", ""), "signature": s["sig"]})
    pairs.sort(key=lambda p: p["pair_id"])
    random.Random(7).shuffle(pairs)
    return pairs


def load_judged(path):
    """eval/adjudicated.json {qid: [symbols]} (final grade >= 2) -> dict qid -> set."""
    if not path or not os.path.isfile(path):
        return {}
    return {q: set(v) for q, v in L.jload(path).items()}


# ---- orchestration -----------------------------------------------------------------------------------------
def run(split, systems, lock=False, dev_aug=None, labels=None, jobs=8, root=None, out=None, bench=None):
    if split == "TEST":
        if not lock:
            raise EvalError("E_LOCKED", "TEST requires --lock")
        import hb_freeze
        ok, why = hb_freeze.verify(os.path.join(L.HB, "out", "freeze.json"))
        if not ok:
            raise EvalError("E_NO_FREEZE", why)
        path = TEST_PATH
    else:
        path = bench or DEV_PATH
    rows = read_rows(path, split, lock_ok=(split == "TEST" and lock))
    if split == "DEV" and dev_aug and os.path.isfile(dev_aug):
        rows += read_rows(dev_aug, "DEV")
    symbols = {s["id"]: s for s in L.read_jsonl(os.path.join(root or L.HB, L.BOOK_SUB, "symbols.jsonl"))}
    jobs_list = [(s, r) for s in systems for r in rows]
    results = {s: {} for s in systems}
    with cf.ThreadPoolExecutor(jobs) as ex:
        for (s, r), res in zip(jobs_list, ex.map(lambda sr: run_system(sr[0], sr[1], root), jobs_list)):
            results[s][r["qid"]] = res
    judged = load_judged(labels)
    metrics, rrs = score(rows, results, judged)
    base = [s for s in systems if s in ("B0", "B1")]
    perm = {}
    if "v2" in systems:
        for b in base:
            perm["v2_vs_" + b] = paired_p(rows, results, "v2", b)
    key = {}
    pairs = pool_pairs(rows, results, symbols, key)
    doc = {"schema": "hb.run.v2", "split": split, "n_rows": len(rows), "systems": systems, "metrics": metrics, "perm_p": perm, "rr": rrs,
           "pool_pairs": len(pairs), "labels": bool(judged), "thin_dev": thin(rows) if split == "DEV" else None,
           "per_query": {s: {q: {"rows": x.get("rows", [])[:8], "abstain": x.get("abstain"), "claim": x.get("claim"), "chg": x.get("chg"), "tests": x.get("tests"),
                                  "bytes": x.get("bytes")} for q, x in results[s].items()} for s in systems}}
    doc["_key"] = key
    return doc, pairs, results


def thin(rows):
    answerable = sum(1 for r in rows if row_class(r) in ("FIND", "IMPACT"))
    absent = sum(1 for r in rows if row_class(r) == "ABSENT")
    return answerable < 20 or absent < 20


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", required=True, choices=["DEV", "TEST"])
    ap.add_argument("--systems", default="v2,B0,B1,B2")
    ap.add_argument("--lock", action="store_true")
    ap.add_argument("--dev-aug", default=os.path.join(L.HB, "eval", "dev_aug.jsonl"))
    ap.add_argument("--labels")
    ap.add_argument("--out")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--root", default=L.HB)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    out = a.out or os.path.join(L.HB, "out", "run_%s.json" % a.split)
    try:
        doc, pairs, _ = run(a.split, a.systems.split(","), a.lock, a.dev_aug, a.labels, a.jobs, a.root)
    except EvalError as e:
        sys.stderr.write("%s\n" % e)
        return 4
    key = doc.pop("_key")
    L.write_json(out, doc)
    suffix = "" if a.split == "DEV" else "_TEST"
    L.write_jsonl(os.path.join(L.HB, "eval", "pool_pairs%s.jsonl" % suffix), pairs)
    L.write_json(os.path.join(L.HB, "eval", "pool_key%s.json" % suffix), key)
    print(L.dumps({s: {k: v for k, v in m.items() if k in ("FIND", "IMPACT", "CLAIM", "ABSENT")} for s, m in doc["metrics"].items() if s == "v2"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
