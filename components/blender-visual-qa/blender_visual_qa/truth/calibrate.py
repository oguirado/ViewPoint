"""bvqa.calib-row/v1 rows + B6 still probe -> vqa.profile/v1 (T0_Q4 only) + calib_report.json (BDESIGN_TRUTH 2.7/2.8/5).

Assumptions (declared): dirty iff S1 residual >= T0, so the least T0 with zero false positives is max_resid + 1;
still quantity q = max of the B6 still-probe residual samples (q4); T0_still = max(32, (5q+3)//4) (SPEC_CORE 6.1).
Only P rows change; DRIFT_C_Q4/DRIFT_U_Q4 are left at the defaults in v1. Non-P findings go to proposals[].
"""
import glob
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
from . import score  # noqa: E402
from .score import canon, VqaError  # noqa: E402
from visioncore import params  # noqa: E402

FIELD_SAT_Q4 = 1020  # engine display field is min(255, r>>2): 255 = saturated

# (name, tl, metric key, op, ppm bound)  op ">=": value must be >= bound; "<=": value must be <= bound
TARGETS = (
    ("static_fp", "0", "fp", "<=", 0),
    ("rigid_dirty_recall", "3", "dirty_ppm", ">=", 990000),
    ("moved_class", "3", "cls_ok_ppm", ">=", 900000),
    ("occlude_recall", "4", "dirty_ppm", ">=", 980000),
    ("deform_recall", "5", "dirty_ppm", ">=", 980000),
    ("shade_strong_recall", "6", "dirty_ppm", ">=", 980000),
    ("shade_weak_fp_ppm", "7", "dirty_ppm", "<=", 10000),
)


def t0_still(still_q4):
    """T0 from the B6 still probe samples (q4 residuals): q = p99.9 rank sorted(r1)[999*(M-1)//1000],
    T0_still = max(32, (5q+3)//4) (BTRUTH C1, IMPL-GAP-5)."""
    if not still_q4:
        raise VqaError("E_SCHEMA", {"reason": "empty still probe"})
    xs = sorted(int(x) for x in still_q4)
    q = xs[999 * (len(xs) - 1) // 1000]
    return max(32, (5 * q + 3) // 4)


def band_ppm(hyst_permille=None):
    if hyst_permille is None:
        hyst_permille = params.PARAMS["HYST_BAND_PERMILLE"]
    return int(hyst_permille) * 1000


def t0_fp(rows, band=None):
    """Least T0 with T0*(10^6+BAND)//10^6 >= fp_resid_max (engine: dirty iff r1 > T_enter) (BTRUTH C2, IMPL-GAP-6)."""
    band = band_ppm() if band is None else band
    mx = max((r.get("fp_resid_max_q4", 0) for r in rows), default=0)
    if mx <= 0:
        return 1
    k = 1000000 + band
    return max(1, (mx * 1000000 + k - 1) // k)


def parse_seeds(spec):
    """'fit:0-9,gate:10-39' -> {'fit': [...], 'gate': [...]}"""
    out = {"fit": [], "gate": []}
    for part in spec.split(","):
        role, _, rng = part.partition(":")
        if role not in out or not rng:
            raise VqaError("E_SCHEMA", {"reason": "bad seeds spec", "spec": spec})
        a, _, b = rng.partition("-")
        out[role].extend(range(int(a), int(b or a) + 1))
    return out


def fp_attribution(rows, floor):
    """Which (bench, tile class) pairs carry FP evidence: dirty verdicts on clean tiles or residuals >= floor."""
    grp = {}
    for r in rows:
        for t in ("0", "7"):
            d = r.get("tl", {}).get(t, {}).get("dirty", 0)
            rm = r.get("fp_resid_by_tl", {}).get(t, r.get("fp_resid_max_q4", 0) if not r.get("fp_resid_by_tl") else 0)
            if d or (floor and rm >= floor):
                g = grp.setdefault((r.get("bench", "?"), score.TL_NAMES[int(t)]), [0, 0])
                g[0] += d
                g[1] = max(g[1], rm)
    return [{"bench": b, "tl": n, "fp_tiles": v[0], "fp_resid_max_q4": v[1]} for (b, n), v in sorted(grp.items())]


REFUSAL_SPLIT = "split_leak"
IOU_MIN_PPM = 800000
LAT_MAX_FRAMES = 4
DRIFT_DUTY_MIN_PPM = 500000


def split_violations(rows, seeds, still_meta):
    """-> list of reasons the FIT/GATE partition is unusable (JW9-1). Empty list = frozen, nonempty, disjoint partition.

    Needs seeds {'fit','gate'} nonempty and disjoint; every row seed in exactly one arm; every row a capture_sha256;
    capture shas disjoint across arms (same capture under different seeds leaks); still probe (B6) bound to FIT seeds."""
    bad = []
    if not isinstance(seeds, dict) or not seeds.get("fit") or not seeds.get("gate"):
        return ["absent_or_empty_partition"]
    fit_s, gate_s = set(seeds["fit"]), set(seeds["gate"])
    if fit_s & gate_s:
        bad.append("seed_overlap")
    caps = {"fit": set(), "gate": set()}
    for r in rows:
        arms = [k for k, v in (("fit", fit_s), ("gate", gate_s)) if r.get("seed") in v]
        if not arms:
            bad.append("row_seed_outside_partition")
            continue
        cs = r.get("capture_sha256")
        if not cs:
            bad.append("row_without_capture_sha256")
            continue
        for k in arms:
            caps[k].add(cs)
    if caps["fit"] & caps["gate"]:
        bad.append("capture_sha_overlap")
    if not any(r.get("seed") in fit_s for r in rows) or not any(r.get("seed") in gate_s for r in rows):
        bad.append("arm_without_rows")
    have = {r.get("seed") for r in rows}
    if not (fit_s | gate_s) <= have:  # ADV-W9-02: every declared seed of both arms supplies rows
        bad.append("arm_seed_without_rows")
    if sum(r.get("fp_pop_n", 0) for r in rows if r.get("seed") in fit_s) <= 0:  # T0_fp fitted on an empty FP population
        bad.append("fit_fp_pop_empty")
    keys = [(r.get("bench"), r.get("seed")) for r in rows if r.get("seed") in gate_s]
    if len(keys) != len(set(keys)):  # ADV-W9-03: one GATE row per (bench, seed); a sweep cannot stack certified evidence
        bad.append("duplicate_bench_seed")  # (FIT duplicates are T0-neutral: T0_fp and contamination are maxima)
    if not isinstance(still_meta, dict) or not still_meta.get("seeds"):
        bad.append("still_probe_unbound")
    else:
        if not set(still_meta["seeds"]) <= fit_s:
            bad.append("still_probe_not_fit")
        sc = still_meta.get("capture_sha256")
        if set(sc or ()) & caps["gate"]:
            bad.append("still_probe_capture_in_gate")
        bad += still_membership(rows, fit_s, still_meta["seeds"], sc)
    return sorted(set(bad))


def split_detail(rows, seeds):
    """W10X: which (bench, capture_sha256) groups make the arms overlap -> [{bench, capture_sha256, fit, gate}].
    A seed that does not change a bench's capture (noise-free render, seed-independent scene) shows up here."""
    if not isinstance(seeds, dict) or not seeds.get("fit") or not seeds.get("gate"):
        return []
    fit_s, gate_s = set(seeds["fit"]), set(seeds["gate"])
    grp = {}
    for r in rows:
        if r.get("capture_sha256"):
            g = grp.setdefault((r.get("bench"), r["capture_sha256"]), {"fit": set(), "gate": set()})
            g["fit" if r.get("seed") in fit_s else "gate"].add(r.get("seed"))
    return [{"bench": b, "capture_sha256": c, "fit": sorted(v["fit"]), "gate": sorted(v["gate"])}
            for (b, c), v in sorted(grp.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])) if v["fit"] and v["gate"]]


STILL_BENCH = "B6"


def still_membership(rows, fit_s, still_seeds, still_caps):
    """JW9b-1: the still probe identities must be nonempty capture shas, each the capture of a FIT row of the still
    bench (B6) at a declared still seed, and every declared still seed must contribute one. -> list of reasons."""
    if not isinstance(still_caps, list) or not still_caps or any(not isinstance(c, str) or not c for c in still_caps):
        return ["still_probe_capture_absent"]
    own = {}
    for r in rows:
        if r.get("bench") == STILL_BENCH and r.get("seed") in fit_s and r.get("seed") in set(still_seeds) and r.get("capture_sha256"):
            own[r["capture_sha256"]] = r["seed"]
    bad = []
    if not set(still_caps) <= set(own):
        bad.append("still_probe_capture_unbound")
    if {own[c] for c in still_caps if c in own} != set(still_seeds):
        bad.append("still_probe_seed_unbound")
    return bad


def _seed_success(gate_rows, gate_seeds, pred):
    """Seed-level 30/30 style observation: seeds in gate_seeds whose rows ALL satisfy pred (at least one row),
    over len(gate_seeds) (ADV-W9-04: one passing bench must not mask a failing one of the same seed)."""
    by = {}
    for r in gate_rows:
        by.setdefault(r["seed"], []).append(r)
    ok = {s for s, rs in by.items() if all(pred(r) for r in rs)}
    return len(ok & set(gate_seeds)), len(set(gate_seeds))


def _tl_all_ok(r, t):
    d = r.get("tl", {}).get(t)
    return bool(d) and d["n"] > 0 and d["cls_ok"] == d["n"]


def _sum_key(rows, key, field):
    vals = [r[key][field] for r in rows if isinstance(r.get(key), dict) and field in r[key]]
    return sum(vals) if vals else None


# JW9c-3: S4 acceptance population is B1 only (BTRUTH §5.2 "S4 IoU median >= 0.8 on B1"). B2 IoU (C6) stays a per-row
# diagnostic: it never substitutes absent B1 evidence and is never pooled into the acceptance median.
S4_BENCHES = ("B1",)
# JW9b-2: metric -> (benches whose rows must carry it, row predicate). Every declared GATE seed needs one such row.
POPULATION = (
    ("b4n_temporal_fp_tiles", ("B4n",), lambda r: True),
    ("c1b_class_flips", ("B6",), lambda r: "c1b_flips" in r),
    ("c5_select_ppm", None, lambda r: isinstance(r.get("c5_select"), dict) and r["c5_select"].get("n", 0) > 0),
    ("b5o_global_mv_ppm", ("B5o",), lambda r: isinstance(r.get("b5o_global_mv"), dict) and r["b5o_global_mv"].get("n", 0) > 0),
    ("s4_iou_median_ppm", S4_BENCHES, lambda r: "s4_iou_ppm" in r),
)
CLEAN_BENCHES = ("B3", "B5")


def missing_population(gate_rows, gate_seeds):
    """-> {metric: [GATE seeds without evidence]} (only metrics with a gap)."""
    out = {}
    for name, benches, pred in POPULATION:
        have = {r.get("seed") for r in gate_rows if (benches is None or r.get("bench") in benches) and pred(r)}
        miss = sorted(set(gate_seeds) - have)
        if miss:
            out[name] = miss
    return out


def required_checks(summary, gate_rows, gate_seeds):
    """Mandatory evidence beyond TARGETS (JW9-2). Each -> (name, value, op, bound, den); value None = ABSENT = refuse.
    A metric whose evidence is missing for any declared GATE seed is ABSENT too (JW9b-2: no partial populations)."""
    out = []
    t = [r for r in gate_rows if str(r.get("bench", "")).startswith("B4") and not str(r["bench"]).startswith("B4n")]
    # per seed, EVERY row carrying the stratum must pass (B4 and B4f of one seed are judged together, ADV-W9-04)
    fl = [r for r in t if r.get("tl", {}).get("9", {}).get("n", 0) > 0]
    dr = [r for r in t if r.get("tl", {}).get("11", {}).get("n", 0) > 0]
    n_f, d_f = _seed_success(fl, gate_seeds, lambda r: _tl_all_ok(r, "9")) if fl else (None, len(gate_seeds))
    n_d, d_d = _seed_success(dr, gate_seeds, lambda r: _tl_all_ok(r, "11") and r.get("drift_duty_ppm", 0) >= DRIFT_DUTY_MIN_PPM) \
        if dr else (None, len(gate_seeds))
    fz_rows = [r for r in t if r.get("frozen_hit", {}).get("ge300k", {}).get("n", 0) > 0]
    n_z, d_z = _seed_success(fz_rows, gate_seeds, lambda r: r["frozen_hit"]["ge300k"]["hit"] == r["frozen_hit"]["ge300k"]["n"]) if fz_rows else (None, len(gate_seeds))
    out += [("flicker_seed_success", n_f, ">=", d_f, d_f), ("drift_seed_success", n_d, ">=", d_d, d_d),
            ("frozen_ge300k_seed_success", n_z, ">=", d_z, d_z)]
    lat = [r["latency_max"] for r in t if isinstance(r.get("latency_max"), dict) and "9" in r["latency_max"]]
    out.append(("flicker_latency_max_frames", max((x["9"] for x in lat), default=None), "<=", LAT_MAX_FRAMES, None))
    b4n = [r for r in gate_rows if str(r.get("bench", "")).startswith("B4n")]
    out.append(("b4n_temporal_fp_tiles", sum(r.get("flicker_fp_tiles", 0) + r.get("drift_fp_tiles", 0) for r in b4n) if b4n else None, "<=", 0, None))
    b6 = [r for r in gate_rows if r.get("bench") == "B6"]
    c1b = [r["c1b_flips"] for r in b6 if "c1b_flips" in r]
    out.append(("c1b_class_flips", sum(c1b) if c1b else None, "<=", 0, None))
    c5n, c5h = _sum_key(gate_rows, "c5_select", "n"), _sum_key(gate_rows, "c5_select", "hit")
    out.append(("c5_select_ppm", None if not c5n else c5h * 1000000 // c5n, ">=", 1000000, c5n))
    b5o = [r for r in gate_rows if r.get("bench") == "B5o"]
    b5n, b5e = _sum_key(b5o, "b5o_global_mv", "n"), _sum_key(b5o, "b5o_global_mv", "exact")
    out.append(("b5o_global_mv_ppm", None if not b5n else b5e * 1000000 // b5n, ">=", 1000000, b5n))
    iou = sorted(r["s4_iou_ppm"] for r in gate_rows if r.get("bench") in S4_BENCHES and "s4_iou_ppm" in r)
    out.append(("s4_iou_median_ppm", iou[(len(iou) - 1) // 2] if iou else None, ">=", IOU_MIN_PPM, len(iou) or None))
    gaps = missing_population(gate_rows, gate_seeds)
    return [(n, None if n in gaps else v, op, b, d) for n, v, op, b, d in out]


def check_targets(summary, strict=False, gate_rows=(), gate_seeds=()):
    """-> list of {name, value, op, bound, status in PASS|FAIL}. A mandatory metric with no value is FAIL (no profile);
    `strict` is kept for call compatibility (absence is refused either way)."""
    out = []
    for name, tl, key, op, bound in TARGETS:
        m = summary["tl"].get(tl)
        out.append(_chk(name, None if m is None else m.get(key), op, bound, True, None if m is None else m["n"]))
    out.append(_chk("mv_exact", summary["mv_exact_ppm"], ">=", 950000, True, summary.get("mv_n")))
    out.append(_chk("mv_err_p95_px", summary["mv_err_p95_px"], "<=", 1, True, summary.get("mv_moved_n")))
    out.append(_chk("drift_duty_ppm", summary.get("drift_duty_ppm"), ">=", DRIFT_DUTY_MIN_PPM, True, None))
    out.append(_chk("flicker_fp_tiles", summary.get("flicker_fp_tiles"), "<=", 0, True, None))
    for name, v, op, bound, den in required_checks(summary, list(gate_rows), list(gate_seeds)):
        out.append(_chk(name, v, op, bound, True, den))
    gaps = missing_population(list(gate_rows), list(gate_seeds))
    for c in out:
        if c["name"] in gaps:
            c["missing_seeds"] = gaps[c["name"]]
    return out


def clean_evidence_missing(rows):
    """JW9b-3: B3/B5 rows whose truth was labelled without an independent clean residual -> [(bench, seed)]."""
    return sorted((r.get("bench"), r.get("seed")) for r in rows if r.get("bench") in CLEAN_BENCHES and r.get("clean_resid") is not True)


def _chk(name, v, op, bound, strict, den=None):
    st = "FAIL" if v is None else ("PASS" if (v >= bound if op == ">=" else v <= bound) else "FAIL")
    c = {"name": name, "value": v, "op": op, "bound": bound, "status": st}
    if v is None:
        c["absent"] = True
    if den is not None:
        c["den"] = den
    return c


def fit_t0(fit_rows, still_q4):
    """FIT-only T0 fit -> {T0_still, T0_fp, T0_Q4, fp_resid_max_q4, contaminated}; never sees GATE rows (R-lane interface)."""
    fit_rows = list(fit_rows)
    a, b = t0_still(still_q4), t0_fp(fit_rows)
    mx = max((r.get("fp_resid_max_q4", 0) for r in fit_rows), default=0)
    return {"T0_still": a, "T0_fp": b, "T0_Q4": max(a, b), "fp_resid_max_q4": mx,
            "contaminated": mx >= FIELD_SAT_Q4 or b > 4 * a}


def calibrate(rows, still_q4, adapter, renderer, grid, source_sha256, strict=False, seeds=None, still_meta=None):
    """-> (profile or None, report). profile None means refused (rc 5 E_GATE reason truth_targets / split_leak).

    seeds {'fit': [...], 'gate': [...]} is MANDATORY: T0 is fitted on FIT rows only, targets are checked on GATE rows only.
    Absent/empty arm, seed overlap, capture-sha overlap or an unbound still probe refuses with `split_leak` (bt.samedata.01).
    still_meta {'seeds': [...], 'capture_sha256': [...]} binds the B6 still probe to FIT."""
    rows = list(rows)
    if not rows:
        raise VqaError("E_SCHEMA", {"reason": "no rows"})
    leaks = split_violations(rows, seeds, still_meta)
    if isinstance(seeds, dict) and still_q4 and not any(int(x) for x in still_q4):
        leaks.append("still_probe_degenerate")  # W10S: an all-zero still probe carries no noise distribution to fit T0
    overlap = sorted(set(seeds["fit"]) & set(seeds["gate"])) if isinstance(seeds, dict) and seeds.get("fit") and seeds.get("gate") else []
    if leaks:
        fit_rows = gate_rows = []
    else:
        fit_s, gate_s = set(seeds["fit"]), set(seeds["gate"])
        fit_rows = [r for r in rows if r.get("seed") in fit_s]
        gate_rows = [r for r in rows if r.get("seed") in gate_s]
    fit = fit_t0(fit_rows, still_q4) if fit_rows else {"T0_still": t0_still(still_q4), "T0_fp": 0, "T0_Q4": t0_still(still_q4),
                                                      "fp_resid_max_q4": 0, "contaminated": False}
    a, b, mx_fp, contaminated, t0 = fit["T0_still"], fit["T0_fp"], fit["fp_resid_max_q4"], fit["contaminated"], fit["T0_Q4"]
    hi = params.BOUNDS["T0_Q4"][1]
    ok_range = t0 <= hi
    t0 = min(t0, hi)
    # ADV-W9-03: GATE rows must have been labelled/scored at the T0 frozen from FIT, never at another threshold.
    off_t0 = sorted((r.get("bench"), r.get("seed")) for r in gate_rows if r.get("t0_q4") != t0)
    summary = score.summarize(gate_rows) if gate_rows else {"tl": {}, "mv_n": 0, "mv_exact_ppm": None, "mv_err_p95_px": None,
                                                           "drift_duty_ppm": None, "flicker_fp_tiles": None, "frozen_hit": {},
                                                           "fp_resid_max_q4": 0, "fp_pop_n": 0, "mv_moved_n": 0}
    gate_seeds = sorted(seeds["gate"]) if isinstance(seeds, dict) and seeds.get("gate") else []
    checks = check_targets(summary, strict, gate_rows, gate_seeds)
    no_clean = clean_evidence_missing(rows)
    checks.append(_chk("clean_evidence_missing_rows", len(no_clean), "<=", 0, True, None))
    passed = ok_range and not contaminated and not leaks and not off_t0 and not any(c["status"] == "FAIL" for c in checks)
    proposals = []
    wrong_mv = sum(sum(r.get("mv_err_hist", [0])[1:]) for r in gate_rows)
    if summary["mv_exact_ppm"] is not None and summary["mv_exact_ppm"] < 950000:
        if wrong_mv > 0:
            proposals.append({"param": "MV_SAD_RATIO_DEN", "reason": "wrong motion vectors present (%d); review MOVED rule" % wrong_mv})
        else:
            proposals.append({"param": None, "reason": "MOVED loss = unobservable tl3 + abs SAD gate vs render noise, "
                              "not vector error (wrong mv = 0); see DIAG_DMOV RC1/RC2"})
    refusal = REFUSAL_SPLIT if leaks else ("fp_pop_contaminated" if contaminated else
                                           ("t0_not_frozen" if off_t0 else ("clean_evidence_absent" if no_clean else None)))
    detail = None
    if contaminated:
        detail = fp_attribution(fit_rows, mx_fp if mx_fp >= FIELD_SAT_Q4 else 4 * a)
    elif not passed and not leaks and any(c["name"] == "static_fp" and c["status"] == "FAIL" for c in checks):
        detail = fp_attribution(gate_rows, 0)
    table = params.effective({"T0_Q4": t0})
    split = None if not isinstance(seeds, dict) else {"fit": list(seeds.get("fit") or []), "gate": list(seeds.get("gate") or [])}
    report = {
        "schema": "bvqa.calib-report/v1", "adapter": adapter, "renderer": renderer, "grid": grid,
        "source_sha256": source_sha256, "T0_still": a, "T0_fp": b, "T0_Q4": t0, "t0_in_bounds": ok_range,
        "rows": len(rows), "seeds": split, "split_violations": leaks + (["gate_row_t0_not_frozen"] if off_t0 else []),
        "split_sha256": None if leaks else canon.sha256_bytes(canon.dumps({"seeds": split, "still": still_meta}).encode()),
        "seeds_overlap": overlap, "fp_attribution": detail, "summary": summary, "checks": checks, "passed": passed, "strict": bool(strict),
        "proposals": proposals, "refusal": refusal,
        "params_sha256": params.params_sha256(table),
    }
    if leaks:  # W10X: name the overlapping (bench, capture) groups; absent on a clean partition (golden bytes unchanged)
        report["split_detail"] = split_detail(rows, seeds)
    profile = None
    if passed:
        profile = {"schema": "vqa.profile/v1", "adapter": adapter, "renderer": renderer, "grid": grid,
                   "source_sha256": source_sha256, "T0_Q4": t0}
    return profile, report


def load_rows(patterns):
    rows = []
    for pat in patterns:
        for p in sorted(glob.glob(pat)):
            for ln in Path(p).read_text(encoding="utf-8").splitlines():
                if ln.strip():
                    rows.append(canon.loads(ln))
    return rows


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="blender_visual_qa.truth.calibrate")
    ap.add_argument("--rows", nargs="+", required=True)
    ap.add_argument("--still", required=True, help="B6 still manifest json: {\"samples_q4\": [...], \"sha256\": hex}")
    ap.add_argument("--out", required=True, help="profile.json (written only on pass)")
    ap.add_argument("--report", default=None, help="calib_report.json (default: next to --out)")
    ap.add_argument("--renderer", default="cycles-cpu-16spp")
    ap.add_argument("--grid", required=True)
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--seeds", default=None, help="fit:<a-b>,gate:<c-d>; FIT ∩ GATE ≠ ∅ refuses (rc 5)")
    a = ap.parse_args(argv)
    try:
        st = json.loads(Path(a.still).read_text(encoding="utf-8"))
        src = canon.sha256_bytes(Path(a.still).read_bytes())
        profile, report = calibrate(load_rows(a.rows), st["samples_q4"], "blender", a.renderer, a.grid, src, a.strict,
                                       None if a.seeds is None else parse_seeds(a.seeds),
                                       st.get("still_meta"))
        rp = Path(a.report) if a.report else Path(a.out).with_name("calib_report.json")
        canon.write_atomic(str(rp), canon.dumps(report).encode())
        if profile is None:
            raise VqaError("E_GATE", {"reason": report["refusal"] or "truth_targets"})
        canon.write_atomic(a.out, canon.dumps(profile).encode())
        return 0
    except (VqaError, KeyError, ValueError) as e:
        if not isinstance(e, VqaError):
            e = VqaError("E_SCHEMA", {"reason": repr(e)})
        sys.stdout.write(json.dumps(e.to_obj(), sort_keys=True) + "\n")
        return e.rc


if __name__ == "__main__":
    sys.exit(main())
