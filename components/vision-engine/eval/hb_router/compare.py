#!/usr/bin/env python3
"""Paired sign-flip p on per-query RR of one system, run_before vs run_after (exact Fractions, no floats).
usage: compare.py run_before.json run_after.json [system=v2]"""
import itertools, json, random, sys
from fractions import Fraction as F


def sign_flip_p(deltas):
    d = [x for x in deltas if x != 0]
    if not d:
        return F(1)
    obs = abs(sum(d))
    n = len(d)
    if n <= 20:
        return F(sum(abs(sum(s * x for s, x in zip(sg, d))) >= obs for sg in itertools.product((1, -1), repeat=n)), 2 ** n)
    rnd = random.Random(0)
    ge = sum(abs(sum(x if rnd.random() < 0.5 else -x for x in d)) >= obs for _ in range(100000))
    return F(ge + 1, 100001)


def main(argv):
    if len(argv) < 3:
        sys.stderr.write(__doc__)
        return 2
    a, b = (json.load(open(p, encoding="utf-8")) for p in argv[1:3])
    sysn = argv[3] if len(argv) > 3 else "v2"
    ra, rb = a["rr"][sysn], b["rr"][sysn]
    qs = sorted(set(ra) & set(rb))
    deltas = [F(rb[q]) - F(ra[q]) for q in qs]
    p = sign_flip_p(deltas)
    print(json.dumps({"system": sysn, "n": len(qs), "nonzero": sum(1 for x in deltas if x != 0),
                      "sum_delta_rr": "%d/%d" % (sum(deltas, F(0)).numerator, sum(deltas, F(0)).denominator),
                      "p": "%.4f" % (p.numerator * 10000 // p.denominator / 10000), "p_exact": "%d/%d" % (p.numerator, p.denominator)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
