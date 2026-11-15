#!/usr/bin/env python3
"""Paired before/after comparison of the HB-OS pilot (primary endpoint: v2 reciprocal rank on TEST FIND), recomputed from the receipts.

usage: compare.py [--runs DIR] [--before LABEL] [--after LABEL] [--json]
Reads DIR/run_test_<label>.json (default eval/hb_os/runs, labels before/after), refuses (rc 5) when the frozen-protocol stamps differ,
then prints n, wins/losses, mean delta RR, exact sign-flip p (hb_eval.sign_flip_p, the pre-registered test) and the ABSENT F1 guardrail.
rc 0 computed, 2 usage, 5 stamp mismatch or unreadable receipt. Never 1."""
import json
import os
import sys
from fractions import Fraction as F

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
PARITY = ("params_sha256", "test_queries_sha256", "dev_queries_sha256", "driver_sha256", "protocol_sha256")


def load_hb_eval():
    sys.path.insert(0, os.path.join(REPO, "hyperbooks", "_tools", "hbtools_v2"))
    import hb_eval
    return hb_eval


def compare(runs, before="before", after="after"):
    b, a = [json.load(open(os.path.join(runs, "run_test_%s.json" % l))) for l in (before, after)]
    for k in PARITY:
        if a["stamp"].get(k) != b["stamp"].get(k):
            raise ValueError("stamp mismatch " + k)
    H = load_hb_eval()
    rb, ra = b["rr"]["v2"], a["rr"]["v2"]
    qs = sorted(q for q in ra if q in rb)
    d = [F(ra[q]) - F(rb[q]) for q in qs]
    nz = [x for x in d if x != 0]
    f1 = lambda r: float(r["metrics"]["v2"]["ABSENT"]["f1"])
    p = float(H.sign_flip_p(d))
    out = {"n": len(d), "nonzero": len(nz), "wins": sum(1 for x in nz if x > 0), "losses": sum(1 for x in nz if x < 0),
           "mean_delta_rr": float(sum(d) / len(d)), "mrr_before": float(sum(F(rb[q]) for q in qs) / len(qs)),
           "mrr_after": float(sum(F(ra[q]) for q in qs) / len(qs)), "p": p,
           "p_exact_fraction": "%d/%d" % (F(p).numerator, F(p).denominator) if len(nz) <= 20 else None,
           "absent_f1_before": f1(b), "absent_f1_after": f1(a)}
    smallest = min((x for x in nz if x > 0), default=None)
    if smallest is not None:
        d2 = list(d)
        d2.remove(smallest)
        out["p_without_smallest_win"] = float(H.sign_flip_p(d2))
    out["met_threshold"] = bool(p < 0.05 and out["mean_delta_rr"] > 0 and out["absent_f1_after"] >= out["absent_f1_before"] - 0.05)
    return out


def main(argv):
    opts = {"--runs": os.path.join(HERE, "runs"), "--before": "before", "--after": "after"}
    i = 0
    while i < len(argv):
        if argv[i] in opts and i + 1 < len(argv):
            opts[argv[i]] = argv[i + 1]
            i += 2
        else:
            print("usage: compare.py [--runs DIR] [--before LABEL] [--after LABEL]", file=sys.stderr)
            return 2
    try:
        res = compare(opts["--runs"], opts["--before"], opts["--after"])
    except (OSError, ValueError, KeyError) as e:
        print("E_COMPARE %s" % e, file=sys.stderr)
        return 5
    print(json.dumps(res, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
