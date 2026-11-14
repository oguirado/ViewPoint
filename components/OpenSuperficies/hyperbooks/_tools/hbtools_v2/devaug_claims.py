#!/usr/bin/env python3
"""DEV-AUG CLAIM rows built mechanically from subject receipts (test_eval/ and out/): 10 rows, 5 true and 5 perturbed by
+-(1 + 10u) % with u in [0, 1) from a seeded generator (integer thousandths), nudged by one unit in the last stated digit when rounding
would hide the change. Statement text carries no digits other than the claimed number. Gold verdict is mechanical."""
import json, os, random, re, sys
from decimal import Decimal
from fractions import Fraction as F

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402

FILES = ["test_eval/agreement.json", "test_eval/cold_latency_TEST.json", "test_eval/gates_TEST.json", "test_eval/DELTA_v2.0.1.json",
         "out/cold_latency.json", "out/census.json", "out/null_edges.json", "out/T2.json", "out/T0.json", "out/ablation.json"]
UNIT_BY_KEY = [(re.compile(r"bytes?|size", re.I), ("B", "bytes")), (re.compile(r"_s($|\.)", re.I), ("s", "seconds")),
               (re.compile(r"(^|[._])(ms|lat|latency|p50|p90|p99|p95)($|[._])|_ms($|\.)", re.I), ("ms", "milliseconds"))]


def leaves(doc, path=()):
    if isinstance(doc, dict):
        for k in sorted(doc):
            yield from leaves(doc[k], path + (k,))
    elif isinstance(doc, list):
        for i, v in enumerate(doc[:8]):
            yield from leaves(v, path + (str(i),))
    elif isinstance(doc, (int, Decimal)) and not isinstance(doc, bool):
        yield ".".join(path), doc


def decimals(v):
    s = format(v, "f") if isinstance(v, Decimal) else str(v)
    return len(s.split(".")[1]) if "." in s else 0


def words(f, key):
    toks = re.findall(r"[A-Za-z]+", os.path.splitext(os.path.basename(f))[0].replace("_", " ")) + re.findall(r"[A-Za-z]+", " ".join(key.split(".")[-2:]).replace("_", " "))
    out = []
    for t in toks:
        if t.lower() not in out and t.upper() != "TEST":
            out.append(t.lower())
    return " ".join(out[:6])


def candidates(subject):
    out = []
    for f in FILES:
        p = os.path.join(subject, f)
        if not os.path.isfile(p):
            continue
        for key, v in leaves(json.load(open(p), parse_float=Decimal)):
            if v >= 2 and re.search(r"[A-Za-z]", key) and decimals(v) <= 4 and len(key) < 60:
                out.append((f, key, v))
    return out


def fmt(v, p):
    return L.fmt_dec(F(v), p) if p > 0 else "%d" % int(v)


def make_row(i, f, key, v, perturb, rnd, seed):
    p = decimals(v)
    val = F(v)
    unit_word = ""
    for rx, (sym, w) in UNIT_BY_KEY:
        if rx.search(key):
            unit_word = w
            break
    u = F(rnd.randint(0, 999), 1000)
    claimed, verdict = val, "supported"
    if perturb:
        pct = (1 + 10 * u) / 100
        sign = 1 if rnd.random() < 0.5 else -1
        claimed = val * (1 + sign * pct)
        q = F(1, 10 ** p)
        claimed = F(round(claimed / q)) * q
        if abs(claimed - val) <= q / 2:                       # rounding hid the change: nudge one unit of the last digit
            claimed = val + sign * q
        verdict = "contradicted"
    text = "The recorded %s was %s%s." % (words(f, key), fmt(claimed, p), (" " + unit_word) if unit_word else "")
    return {"class": "CLAIM", "gold": [], "gold_tests": [], "intent": "check_claim", "notes": "true=%s" % fmt(val, p), "qid": "HB-AC%02d" % i,
            "receipt": "%s:%s" % (f, key), "source_event": "devaug_claims.py seed %d" % seed, "split": "DEV", "text": text, "verdict": verdict}


def build(subject, seed=11, n=10):
    """Rows whose mechanical gold verdict is re-checked with hb_claim (a row the checker cannot decide is replaced by the next candidate)."""
    import hb_claim as C
    cands = candidates(subject)
    rnd = random.Random(seed)
    rnd.shuffle(cands)
    flags = [False] * (n // 2) + [True] * (n - n // 2)
    rnd.shuffle(flags)
    rows, seen_files = [], {}
    for f, key, v in cands:
        if len(rows) == n:
            break
        if seen_files.get(f, 0) >= 2:
            continue
        r = make_row(len(rows) + 1, f, key, v, flags[len(rows)], rnd, seed)
        c = C.check_prose(r["text"], r["receipt"], subject)
        if c is None or c["verdict"] != r["verdict"]:
            continue
        seen_files[f] = seen_files.get(f, 0) + 1
        rows.append(r)
    return rows


if __name__ == "__main__":
    for r in build(sys.argv[1] if len(sys.argv) > 1 else L.SUBJECT):
        print(r["verdict"], r["text"], r["receipt"])
