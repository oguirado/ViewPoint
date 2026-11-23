#!/usr/bin/env python3
"""W10X offline rescore: night-1 rows/evidence -> NEW out dir (never the night-1 run dir). No render, no refit.

Writes <out>/rescore_report.json and <out>/RESCORE.md:
  official   calibrate.calibrate() on the frozen rows exactly as run_bench calls it (reproduces the refusal);
  diagnostic GATE-only check values: score.summarize(GATE rows) + calibrate.check_targets at the thresholds in code and the
             T0 the GATE rows were labelled at (run_bench frozen_t0_q4). DIAGNOSTIC ONLY: never a profile, never SEAL-2;
  seeds      per check, GATE seeds whose own rows meet the bound (k/n) + Clopper-Pearson one-sided 95 % lower bound;
  independence per bench, distinct capture_sha256 per arm and FIT/GATE overlap (seed-independence of the evidence).
Assumption (declared): the per-seed success of a pooled ppm target is the same target evaluated on that seed's rows only.
"""
import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blender_visual_qa.truth import calibrate, score  # noqa: E402

ALPHA = 0.05


def cp_lower(k, n, alpha=ALPHA):
    """Clopper-Pearson one-sided (1-alpha) lower bound for k successes in n: p with P[X >= k | p] = alpha."""
    if n <= 0 or k <= 0:
        return 0.0
    if k == n:
        return alpha ** (1.0 / n)
    from math import comb

    def tail(p):
        return sum(comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1))
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if tail(mid) < alpha else (lo, mid)
    return lo


def independence(rows, fit_s, gate_s):
    out = {}
    for b in sorted({r["bench"] for r in rows}):
        f = {r["capture_sha256"] for r in rows if r["bench"] == b and r["seed"] in fit_s}
        g = {r["capture_sha256"] for r in rows if r["bench"] == b and r["seed"] in gate_s}
        out[b] = {"fit_distinct": len(f), "gate_distinct": len(g), "overlap": len(f & g)}
    return out


def gate_checks(gate_rows, gate_seeds):
    return calibrate.check_targets(score.summarize(gate_rows), True, gate_rows, gate_seeds)


def seed_success(gate_rows, gate_seeds):
    """{check name: [k, n, cp_lb]} for checks that are PASS/FAIL-evaluable on one seed's rows."""
    per = {}
    for s in gate_seeds:
        rs = [r for r in gate_rows if r["seed"] == s]
        for c in (gate_checks(rs, [s]) if rs else []):
            if c["value"] is not None:
                k = per.setdefault(c["name"], [0, 0])
                k[0] += c["status"] == "PASS"
                k[1] += 1
    return {n: [k, m, round(cp_lower(k, m), 4)] for n, (k, m) in sorted(per.items())}


def rescore(run, out):
    run, out = Path(run).resolve(), Path(out).resolve()
    if out == run or run in out.parents or out in run.parents:
        raise SystemExit("E_ARGS: --out must be a new dir outside the night-1 run dir")
    if out.exists() and any(out.iterdir()):
        raise SystemExit("E_ARGS: --out exists and is not empty (never overwrite)")
    rows = [json.loads(ln) for ln in (run / "rows.jsonl").read_text().splitlines() if ln.strip()]
    rep = json.loads((run / "calib_report.json").read_text())
    still = json.loads((run / "still_manifest.json").read_text())
    seeds = rep["seeds"]
    fit_s, gate_s = set(seeds["fit"]), set(seeds["gate"])
    _, official = calibrate.calibrate(rows, still["samples_q4"], "blender", rep["renderer"], rep["grid"],
                                      score.canon.sha256_bytes((run / "still_manifest.json").read_bytes()), True,
                                      seeds, still.get("still_meta"))
    gate_rows = [r for r in rows if r["seed"] in gate_s]
    diag = gate_checks(gate_rows, sorted(gate_s))
    doc = {"schema": "bvqa.w10x-rescore/v1", "run": str(run), "rows": len(rows),
           "rows_sha256": score.canon.sha256_bytes((run / "rows.jsonl").read_bytes()),
           "frozen_t0_q4": rep.get("frozen_t0_q4"), "gate_t0_q4": sorted({r.get("t0_q4") for r in gate_rows}),
           "official": {k: official.get(k) for k in ("passed", "refusal", "split_violations", "split_detail", "T0_Q4")},
           "diagnostic_gate_only": diag, "seed_success_cp95": seed_success(gate_rows, sorted(gate_s)),
           "independence": independence(rows, fit_s, gate_s), "certifying": False}
    out.mkdir(parents=True, exist_ok=True)
    (out / "rescore_report.json").write_text(json.dumps(doc, sort_keys=True, indent=1) + "\n")
    (out / "RESCORE.md").write_text(render_md(doc))
    return doc


def render_md(doc):
    L = ["# W10X offline rescore (DIAGNOSTIC, not certifying)", "",
         "official: passed %s, refusal %s, split_violations %s" % (doc["official"]["passed"], doc["official"]["refusal"],
                                                                  doc["official"]["split_violations"]),
         "GATE rows labelled at T0_Q4 %s (frozen_t0_q4 %s)" % (doc["gate_t0_q4"], doc["frozen_t0_q4"]), "",
         "| check | op | bound | GATE value | den | status | seeds k/n | CP95 LB |", "|---|---|---|---|---|---|---|---|"]
    ss = doc["seed_success_cp95"]
    for c in doc["diagnostic_gate_only"]:
        k = ss.get(c["name"])
        L.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (c["name"], c["op"], c["bound"], c["value"], c.get("den", "-"),
                                                           c["status"], "%d/%d" % tuple(k[:2]) if k else "-", k[2] if k else "-"))
    L += ["", "| bench | FIT distinct captures | GATE distinct | FIT/GATE overlap |", "|---|---|---|---|"]
    L += ["| %s | %d | %d | %d |" % (b, v["fit_distinct"], v["gate_distinct"], v["overlap"]) for b, v in doc["independence"].items()]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rescore_w10")
    ap.add_argument("--run", required=True, help="night-1 run dir (read only)")
    ap.add_argument("--out", required=True, help="NEW output dir")
    a = ap.parse_args(argv)
    doc = rescore(a.run, a.out)
    print(json.dumps({"code": "OK", "official_refusal": doc["official"]["refusal"], "out": str(Path(a.out).resolve())}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
