#!/usr/bin/env python3
"""D-6 tuning, DEV only, five stages in order, each frozen before the next:
 (1) tau_rel on the DEV drift suite: largest of the grid with 0 false rebinds;
 (2) FIND ranking, 36 configs, J = mean (RR@20 + recall@5)/2 on BUDGETED answers of answerable DEV rows, parsimony (delta = 1/(2n), fewest
     non-default switches), promotion over the all-default config only if dJ > 0;
 (3) abstention fit (HB2-03, `hb_query.fit_x2`);
 (4) IMPACT, 27 configs, objective mean over DEV IMPACT rows of (symbol recall@5, test recall@3)/2, ties -> smaller k_e, D_max;
 (5) visual delta_px / rho on the DEV scene mutants + clean reruns (reads out/visual_DEV.json).
Grids come from standards/grid_hb.json. g_blind: any path containing hb_test / hb_bench / a P1 bench dir, or any row with split TEST, is refused.

hb_tune.py --dev bench/hb_dev.jsonl --dev-aug eval/dev_aug.jsonl --out params.json [--report out/tune_report.json]
"""
import argparse, itertools, json, os, sys
from fractions import Fraction as F

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402
import hb_eval as E  # noqa: E402
import hb_query as Q  # noqa: E402
import hb_impact as H  # noqa: E402
import hb_anchor2 as A  # noqa: E402


class BlindError(Exception):
    code = "E_BLIND"


def guard(path):
    ap = os.path.abspath(path)
    base = os.path.basename(ap)
    if "hb_test" in base or "hb_bench" in base or (os.sep + "P1" + os.sep + "bench") in ap or ("hb_test" in ap):
        raise BlindError("E_BLIND: " + path)


def read_dev(paths):
    rows = []
    for p in paths:
        if not p or not os.path.isfile(p):
            continue
        guard(p)
        for l in open(p, encoding="utf-8"):
            if l.strip():
                r = json.loads(l)
                if r.get("split") == "TEST":
                    raise BlindError("E_BLIND: TEST row in " + p)
                rows.append(r)
    return rows


def switches(cfg, defaults):
    return sum(1 for k, d in defaults.items() if cfg[k] != d)


def configs(spec):
    vals = [spec["values"][k] for k in spec["order"]]
    return [dict(zip(spec["order"], c)) for c in itertools.product(*vals)]


def jq(rows, gold):
    """(RR@20 + recall@5)/2 as an exact Fraction."""
    g = set(gold)
    rr = next((F(1, i + 1) for i, x in enumerate(rows[:20]) if x in g), F(0))
    return (rr + F(len(set(rows[:5]) & g), len(g))) / 2


def select(table, defaults, order, delta):
    """table: [(cfg, J)] in grid order. Parsimony then promotion over the default (router v2 6.2). Returns (cfg, report)."""
    best = max(j for _, j in table)
    cand = [i for i, (c, j) in enumerate(table) if j >= best - delta]
    cand.sort(key=lambda i: (switches(table[i][0], defaults), i))
    dflt = next(i for i, (c, _) in enumerate(table) if c == defaults)
    trail = []
    for i in cand:
        if i == dflt:
            return table[i][0], {"candidates": len(cand), "promotion": "default_is_best_parsimonious", "trail": trail}
        dj = table[i][1] - table[dflt][1]
        trail.append({"cfg": table[i][0], "dJ": E.dec(dj, 6)})
        if dj > 0:
            return table[i][0], {"candidates": len(cand), "promotion": "accepted", "trail": trail}
    return defaults, {"candidates": len(cand), "promotion": "none_accepted_keep_default", "trail": trail}


def stage1_tau(subject, grid):
    import hb_drift2 as D
    res = []
    for tau in grid["anchor"]["tau_rel_ppm"]:
        r = D.run(subject, None, tau)
        res.append({"tau_rel": tau, "false_rebinds": r["summary"]["false_rebinds"], "correct": r["summary"]["all_correct"], "n": r["summary"]["all_n"]})
    ok = [x for x in res if x["false_rebinds"] == 0]
    chosen = max(x["tau_rel"] for x in ok) if ok else min(x["tau_rel"] for x in res)
    return chosen, res


def stage2_find(h, rows, grid):
    spec = grid["find"]
    ans = [r for r in rows if E.row_class(r) == "FIND"]
    table = []
    for cfg in configs(spec):
        h.find = dict(Q.I.DEFAULT_FIND, **cfg)
        h.abstain = None
        js = []
        for r in ans:
            a = Q.answer(h, r["text"], "FIND")
            js.append(jq([x[0] for x in a["r"]], r["gold"]))
        table.append((cfg, sum(js, F(0)) / len(js)))
    delta = F(1, 2 * len(ans))
    chosen, prom = select(table, spec["defaults"], spec["order"], delta)
    return chosen, prom, table, len(ans), delta


def feature_rows(h, rows, cfg):
    out = []
    for r in rows:
        c = E.row_class(r)
        if c not in ("FIND", "ABSENT"):
            continue
        qt = L.toks(r["text"])
        S = h.ix.scores(qt, cfg)
        order = h.ix.order(S)
        m, cov, oov = h.ix.features(qt, order, cfg)
        out.append((not qt or not order, m, cov, oov, c == "FIND"))
    return out


def stage3_abstain(h, rows, cfg):
    feats = feature_rows(h, rows, cfg)
    fit = Q.fit_x2(feats)
    thin = sum(1 for f in feats if f[4]) < 20 or sum(1 for f in feats if not f[4]) < 20
    return fit, feats, thin


def stage4_impact(h, rows, grid, find_cfg):
    spec = grid["impact"]
    imp = [r for r in rows if E.row_class(r) == "IMPACT"]
    h.find = dict(Q.I.DEFAULT_FIND, **find_cfg)
    table = []
    for cfg in configs(spec):
        h.impact = {"profile": cfg["profile"], "k_e": cfg["k_e"], "d_max": cfg["d_max"], "w_body": spec["w_body"]}
        vals = []
        for r in imp:
            core = H.impact_core(h, r["text"])
            chg = core["chg"] if core else []
            tests = core["tests"] if core else []
            sr = F(len(set(chg[:5]) & set(r["gold"])), len(r["gold"]))
            gt = set(r.get("gold_tests", []))
            tr = F(len(set(tests[:3]) & gt), len(gt)) if gt else sr
            vals.append((sr + tr) / 2)
        table.append((cfg, sum(vals, F(0)) / len(vals)))
    best = max(j for _, j in table)
    cand = [(c, j) for c, j in table if j == best]
    cand.sort(key=lambda cj: (cj[0]["k_e"], cj[0]["d_max"], spec["values"]["profile"].index(cj[0]["profile"])))
    return cand[0][0], table, len(imp)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", required=True)
    ap.add_argument("--dev-aug")
    ap.add_argument("--out", default=os.path.join(L.HB, "params.json"))
    ap.add_argument("--report", default=os.path.join(L.HB, "out", "tune_report.json"))
    ap.add_argument("--grid", default=os.path.join(L.HB, "standards", "grid_hb.json"))
    ap.add_argument("--root", default=L.HB)
    ap.add_argument("--visual", default=os.path.join(L.HB, "out", "visual_DEV.json"))
    ap.add_argument("--skip-drift", action="store_true", help="keep tau_rel at the default (tests only)")
    try:
        a = ap.parse_args(argv)
        for p in (a.dev, a.dev_aug, a.grid):
            if p:
                guard(p)
        rows = read_dev([a.dev, a.dev_aug])
    except BlindError as e:
        sys.stderr.write(str(e) + "\n")
        return 4
    grid = L.jload(a.grid)
    root = a.root
    tau, tau_res = (A.TAU_REL, []) if a.skip_drift else stage1_tau(L.subject_of(root), grid)
    h = Q.Hyper(root, params_path=os.path.join(root, "out", "no_params_for_tuning.json"))      # clean slate: no previous params
    find_cfg, prom, table, n_ans, delta = stage2_find(h, rows, grid)
    find_cfg = dict(Q.I.DEFAULT_FIND, **find_cfg)
    fit, feats, thin_abs = stage3_abstain(h, rows, find_cfg)
    imp_cfg, imp_table, n_imp = stage4_impact(h, rows, grid, find_cfg)
    vis = None
    if os.path.isfile(a.visual):
        vd = L.jload(a.visual)
        vis = vd.get("thresholds_dev")
    dev_sha = L.sha_file(a.dev)
    params = {"schema": "hb.params.v2", "dev_sha256": dev_sha, "find": find_cfg,
              "impact": {"profile": imp_cfg["profile"], "k_e": imp_cfg["k_e"], "d_max": imp_cfg["d_max"], "w_body": grid["impact"]["w_body"]},
              "abstain": None if fit is None else {"m_min": fit["m_min"], "tau_cov": fit["tau_cov"], "tau_oov": fit["tau_oov"]},
              "anchors": {"tau_rel_ppm": tau, "margin_ppm": grid["anchor"]["margin_ppm"], "min_tokens": A.MIN_TOK},
              "grid_sha256": L.sha_file(a.grid)}
    if fit is None:
        params["abstain_reason"] = "uncalibrated"
    if vis:
        params["visual"] = {"delta_px": vis["delta_px"], "rho_ppm": vis["rho_ppm"]}
    if a.dev_aug and os.path.isfile(a.dev_aug):
        params["dev_aug_sha256"] = L.sha_file(a.dev_aug)
    L.write_json(a.out, params)
    report = {"schema": "hb.tune-report.v2", "stage1_tau_rel": tau_res, "tau_rel": tau,
              "stage2": {"configs": len(table), "n_answerable": n_ans, "delta": "%d/%d" % (delta.numerator, delta.denominator), "chosen": find_cfg,
                         "promotion": prom, "J": [{"cfg": c, "J": E.dec(j, 6)} for c, j in table],
                         "J_default": E.dec(next(j for c, j in table if c == grid["find"]["defaults"]), 6), "J_best": E.dec(max(j for _, j in table), 6)},
              "stage3": {"fit": None if fit is None else {k: (E.dec(v, 6) if isinstance(v, F) else v) for k, v in fit.items()}, "rows": len(feats),
                         "answerable": sum(1 for f in feats if f[4]), "absent": sum(1 for f in feats if not f[4])},
              "stage4": {"configs": len(imp_table), "n_impact": n_imp, "chosen": imp_cfg, "J": [{"cfg": c, "J": E.dec(j, 6)} for c, j in imp_table]},
              "stage5": vis, "thin_dev": thin_abs or n_ans < 20, "dev_rows": len(rows), "dev_sha256": dev_sha}
    L.write_json(a.report, report)
    print(L.dumps({"find": find_cfg, "impact": params["impact"], "abstain": params["abstain"], "tau_rel": tau, "visual": params.get("visual"),
                   "thin_dev": report["thin_dev"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
