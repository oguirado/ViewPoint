"""Fit and apply the offline region-noise rule against persisted truth-bench evidence."""
import hashlib
import json
import math
import re
from pathlib import Path

import numpy as np

from . import calibrate, labels, score
from visioncore import layout, regions as vreg
from visioncore.grid import Grid

SCHEMA = "bvqa.noise-rescore/v1"
EDGE_CEILING_PPM = 50000
REPORT_NAME = "noise_rescore_report.json"
# The pinned engine stores q4 residuals as saturated display bytes.
FIELD_SAT_DISPLAY = calibrate.FIELD_SAT_Q4 >> 2
ROW_COMPARE = (
    "frames", "tiles", "excluded", "tl", "mv_n", "mv_exact", "mv_err_hist",
    "mv_n_unobs", "mv_n_raw", "mv_exact_raw", "moved_raw", "drift_duty_n",
    "drift_duty_hit", "drift_duty_ppm", "frozen_hit", "flicker_fp_tiles",
    "drift_fp_tiles", "latency_max", "c1b_flips", "b5o_global_mv",
    "s4_iou_ppm", "s4_iou_n",
)
UPSTREAM_KEYS = (
    "capture_sha256", "clean_resid", "fp_pop_n", "fp_resid_max_q4",
    "fp_resid_by_tl", "c5_select",
)


class NoiseRescoreError(ValueError):
    def __init__(self, code, message, **detail):
        super().__init__(message)
        self.code, self.detail = code, detail


def _fail(code, message, **detail):
    raise NoiseRescoreError(code, message, **detail)


def _int(value, name, lo=0, hi=None):
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        _fail("E_SCHEMA", "%s must be an integer" % name)
    value = int(value)
    if value < lo or (hi is not None and value > hi):
        _fail("E_SCHEMA", "%s out of range" % name)
    return value


def _finite(value, name, lo, hi):
    if isinstance(value, bool):
        _fail("E_ARGS", "%s must be numeric" % name)
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        _fail("E_ARGS", "%s must be numeric" % name)
    if not math.isfinite(value) or not lo <= value <= hi:
        _fail("E_ARGS", "%s out of range" % name)
    return value


def _alpha(value):
    value = _finite(value, "alpha", 0, 1)
    if value in (0, 1):
        _fail("E_ARGS", "alpha must satisfy 0 < alpha < 1")
    return value


def _seed_set(values, name):
    if not isinstance(values, (list, tuple, set)) or not values:
        _fail("E_SPLIT", "ABSENT nonempty %s seeds" % name)
    parsed = [_int(s, name + " seed") for s in values]
    if len(set(parsed)) != len(parsed):
        _fail("E_SPLIT", "duplicate %s seeds" % name)
    return set(parsed)


def fit_noise(still, fit_seeds, fit_rows):
    """Fit solely from B6 FIT samples; validate provenance BEFORE accessing samples.

    A pooled manifest involving GATE cannot be separated, so it is refused. The
    caller must supply FIT rows only; no unit files or truth labels are read here.
    """
    fit_s = _seed_set(fit_seeds, "FIT")
    if not isinstance(still, dict) or not isinstance(still.get("still_meta"), dict):
        _fail("E_SPLIT", "ABSENT still_meta binding")
    meta = still["still_meta"]
    still_s = _seed_set(meta.get("seeds"), "still")
    if not still_s <= fit_s:
        _fail("E_SPLIT", "still samples include non-FIT seeds")
    fit_rows = list(fit_rows)
    if any(r.get("seed") not in fit_s for r in fit_rows):
        _fail("E_SPLIT", "fit_noise accepts FIT rows only")
    bad = calibrate.still_membership(fit_rows, fit_s, sorted(still_s), meta.get("capture_sha256"))
    if bad:
        _fail("E_SPLIT", "; ".join(bad))
    samples = still.get("samples_q4")
    if not isinstance(samples, (list, tuple)) or not samples:
        _fail("E_ABSENT", "ABSENT nonempty samples_q4")
    raw = [_int(x, "still q4 residual") for x in samples]
    sample_digest_status = "ABSENT"
    if "sha256" in still:
        digest = score.canon.sha256_bytes(score.canon.dumps({"s": raw}).encode("utf-8"))
        if still["sha256"] != digest:
            _fail("E_BASELINE", "still sample digest mismatch")
        sample_digest_status = "PASS"
    rank = 999 * (len(raw) - 1) // 1000  # exactly calibrate.t0_still's quantile rank
    r_q4 = sorted(raw)[rank]
    display = [min(FIELD_SAT_DISPLAY, x >> 2) for x in raw]
    ceiling = min(FIELD_SAT_DISPLAY, r_q4 >> 2)
    if ceiling == 0:
        _fail("E_ABSENT", "ABSENT positive fitted display ceiling R (degenerate probe)")
    tails = sum(x > ceiling for x in display)
    return {
        "sample_count": len(raw), "rank": rank, "R_q4": r_q4,
        "mu_q4": sum(raw) / len(raw), "R": ceiling, "mu": sum(display) / len(display),
        "units": "display: min(255, q4 >> 2)", "fit_seeds": sorted(still_s),
        "tail_samples": tails, "raw_tail_samples": sum(x > r_q4 for x in raw),
        "bounded_fit_samples": tails == 0, "samples_clipped_at_R": False,
        "saturated_samples": sum(x >= calibrate.FIELD_SAT_Q4 for x in raw),
        "independent_samples_verified": False, "mean_estimation_error_accounted": False,
        "sample_digest_status": sample_digest_status,
    }


def _class(value):
    if isinstance(value, str):
        if value not in layout.CLS:
            _fail("E_SCHEMA", "unknown region cls: %s" % value)
        return layout.CLS.index(value)
    return _int(value, "cls", 0, score.C_DRIFT)


def _region_stats(region):
    n = _int(region.get("n_tiles"), "n_tiles", 1)
    rmax = _int(region.get("r_max"), "r_max (display units)", 0, FIELD_SAT_DISPLAY)
    rsum = _int(region.get("r_sum"), "r_sum (display units)", 0, FIELD_SAT_DISPLAY * n)
    edge = _int(region.get("edge_ppm"), "edge_ppm", 0, 1000000)
    if not rmax <= rsum <= n * rmax:
        _fail("E_SCHEMA", "inconsistent residual sum/max")
    return n, rmax, rsum, edge


def should_demote(region, model, alpha, hard_ceiling=None, edge_ceiling_ppm=EDGE_CEILING_PPM):
    """Truth-blind whole-region rule. Smaller alpha permits at least as many demotions.

    Hard/edge equality is allowed: only EXCEEDING a declared ceiling vetoes a
    demotion. Saturated display values are kept because their actual upper bound
    is lost. alpha's logarithm is evaluated without overflowing 2/alpha.
    """
    alpha = _alpha(alpha)
    r = _finite(model["R"], "R", 0, FIELD_SAT_DISPLAY)
    if r == 0:
        _fail("E_ARGS", "R must be positive")
    mu = _finite(model["mu"], "mu", 0, FIELD_SAT_DISPLAY)
    hard = r if hard_ceiling is None else _finite(hard_ceiling, "hard ceiling", 0, FIELD_SAT_DISPLAY)
    edge_limit = _int(edge_ceiling_ppm, "edge ceiling ppm", 0, 1000000)
    n, rmax, rsum, edge = _region_stats(region)
    if _class(region.get("cls")) < score.DIRTY_MIN or rmax > hard or edge > edge_limit or rmax == FIELD_SAT_DISPLAY:
        return False
    eps = 2 * math.sqrt((math.log(2) - math.log(alpha)) / n)
    return (rsum / n) / r <= mu / r + eps


def _json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _fail("E_ABSENT", "ABSENT readable JSON %s: %s" % (path, exc))


def _jsonl(path):
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        _fail("E_ABSENT", "ABSENT readable JSONL %s: %s" % (path, exc))


def _inside(run, path):
    resolved = path.resolve()
    if run != resolved and run not in resolved.parents:
        _fail("E_ARGS", "input path escapes --run: %s" % path)
    return resolved


def _grid(ident):
    match = re.fullmatch(r"grid:(\d+)x(\d+):s(\d+)", str(ident))
    if not match:
        _fail("E_SCHEMA", "invalid grid identity")
    grid = Grid(int(match[1]), int(match[2]))
    if grid.ident != ident:
        _fail("E_SCHEMA", "grid identity disagrees with pinned grid rules")
    return grid


def _hist(value, name, count):
    if not isinstance(value, list) or len(value) != 7:
        _fail("E_SCHEMA", "ABSENT seven-bin %s" % name)
    hist = [_int(x, name) for x in value]
    if sum(hist) != count:
        _fail("E_SCHEMA", "%s count mismatch" % name)
    return hist


def decode_regions(frame_rows, region_rows, grid):
    """Decode engine evidence only. -1 denotes unrecoverable mixed dirty classes."""
    if not frame_rows:
        _fail("E_ABSENT", "ABSENT engine frames")
    frame_ids = [_int(f.get("frame"), "frame") for f in frame_rows]
    if frame_ids != list(range(frame_ids[0], frame_ids[0] + len(frame_ids))):
        _fail("E_SCHEMA", "engine frames must be ordered and consecutive")
    by_frame = {f: [] for f in frame_ids}
    for region in region_rows:
        if region.get("schema") != "vqa.core-region/v1" or region.get("grid") != grid.ident:
            _fail("E_SCHEMA", "region schema/grid mismatch")
        frame = _int(region.get("frame"), "region frame")
        if frame not in by_frame:
            _fail("E_SCHEMA", "region references an absent frame")
        by_frame[frame].append(region)
    cls = np.zeros((len(frame_ids), grid.N), np.int8)
    mv = np.zeros(cls.shape + (2,), np.int64)
    rank = {int(m): i for i, m in enumerate(grid.mortons)}
    entries, mixed, clean_absent, jitter = [], 0, 0, 0
    for f, metadata in enumerate(frame_rows):
        if metadata.get("schema") != "vqa.core-frame/v1" or metadata.get("grid") != grid.ident:
            _fail("E_SCHEMA", "frame schema/grid mismatch")
        expected = _hist(metadata.get("n"), "frame histogram", grid.N)
        frame_regs = by_frame[frame_ids[f]]
        if _int(metadata.get("regions"), "frame regions") != len(frame_regs):
            _fail("E_SCHEMA", "frame region count mismatch")
        occupied = np.zeros(grid.N, bool)
        actual = np.zeros(7, np.int64)
        frame_entries = []
        for region in frame_regs:
            n, _, _, _ = _region_stats(region)
            hist = _hist(region.get("cls_hist"), "region histogram", n)
            c = _class(region.get("cls"))
            if c != hist.index(max(hist)) or hist[0] or hist[1]:
                _fail("E_SCHEMA", "region modal cls/dirty histogram mismatch")
            active = {i for i, count in enumerate(hist) if count}
            group = "MOV" if active == {3} else "FRZ" if active == {5} else "CHG"
            if (group == "CHG" and not active <= {2, 4, 6}) or region.get("group", group) != group:
                _fail("E_SCHEMA", "invalid engine region group")
            spans = region.get("spans")
            if not isinstance(spans, list) or not spans:
                _fail("E_SCHEMA", "ABSENT region Morton spans")
            span_n = 0
            for span in spans:
                if not isinstance(span, list) or len(span) != 2 or any(
                        not isinstance(x, str) or not re.fullmatch(r"[0-9a-fA-F]{1,4}", x) for x in span):
                    _fail("E_SCHEMA", "invalid Morton span")
                a, b = (int(x, 16) for x in span)
                if a > b:
                    _fail("E_SCHEMA", "reversed Morton span")
                span_n += b - a + 1
            if span_n != n or span_n > grid.N:
                _fail("E_SCHEMA", "Morton span count differs from n_tiles")
            mortons = vreg.region_tiles(region)  # same expansion as score.s4_iou
            if len(set(mortons)) != n or any(m not in rank for m in mortons):
                _fail("E_SCHEMA", "duplicate/out-of-grid Morton tiles")
            indices = np.asarray([rank[m] for m in mortons], np.int64)
            if occupied[indices].any():
                _fail("E_SCHEMA", "overlapping regions")
            occupied[indices] = True
            actual += hist
            if len(active) == 1:
                cls[f, indices] = c
            else:
                cls[f, indices] = -1
                mixed += n
            if group == "MOV":
                vector = region.get("mv")
                if not isinstance(vector, list) or len(vector) != 2:
                    _fail("E_SCHEMA", "ABSENT MOV region vector")
                mv[f, indices] = [_int(x, "MOVED vector", -128, 127) for x in vector]
            frame_entries.append((region, indices))
        uncovered = grid.N - int(occupied.sum())
        if list(actual[2:]) != expected[2:] or uncovered != sum(expected[:2]):
            _fail("E_SCHEMA", "frame histogram disagrees with decoded regions")
        jitter += expected[1]
        if 0 < expected[1] < uncovered:
            clean_absent += uncovered
        entries.append(frame_entries)
    return {
        "cls": cls, "mv": mv, "frame_ids": frame_ids, "frame_rows": frame_rows,
        "entries": entries, "grid": grid, "mixed_class_tiles": mixed,
        "clean_class_absent_tiles": clean_absent, "class_absent_tiles": mixed + clean_absent,
        "jitter_collapsed_tiles": jitter,
    }


def _unit_path(run, row, fit_s):
    bench = row.get("bench")
    if not isinstance(bench, str) or bench in ("", ".", "..") or Path(bench).name != bench or "\\" in bench:
        _fail("E_SCHEMA", "invalid bench path component")
    arm = "fit" if row["seed"] in fit_s else "gate"
    return _inside(run, run / arm / ("seed_%06d" % row["seed"]) / bench)


def _load_unit(run, row, metadata, fit_s):
    unit = _unit_path(run, row, fit_s)
    grid = _grid(row["grid"])
    decoded = decode_regions(_jsonl(_inside(run, unit / "vision/frames.jsonl")),
                             _jsonl(_inside(run, unit / "vision/regions.jsonl")), grid)
    if row.get("frames") != len(decoded["frame_ids"]) or row.get("tiles") != grid.N:
        _fail("E_BASELINE", "row dimensions disagree with engine evidence", bench=row["bench"], seed=row["seed"])
    # Prefer the actual frozen threshold's labels. A legacy labels/ path is usable
    # only if it is the sole candidate or matches the runner's recorded digest.
    t0 = _int(row.get("t0_q4"), "t0_q4", 1)
    name = "labels" if t0 == int(calibrate.params.effective(None)["T0_Q4"]) else "labels_t0_%d" % t0
    preferred = _inside(run, unit / name / "tiles_truth.bin")
    legacy = _inside(run, unit / "labels/tiles_truth.bin")
    choices = list(dict.fromkeys(p for p in (preferred, legacy) if p.is_file()))
    want = metadata.get("labels_sha256")
    if want:
        choices = [p for p in choices if hashlib.sha256(p.read_bytes()).hexdigest() == want]
    if preferred in choices:
        truth_path = preferred
    elif len(choices) == 1:
        truth_path = choices[0]
    else:
        _fail("E_ABSENT", "ABSENT uniquely bound truth tiles", bench=row["bench"], seed=row["seed"])
    truth = score.read_truth_tiles(truth_path, (grid.s, grid.W, grid.H, grid.gw, grid.gh, grid.N))
    if truth["hdr"]["n_frames"] != len(decoded["frame_ids"]) or truth["hdr"]["f0"] != decoded["frame_ids"][0]:
        _fail("E_BASELINE", "truth/engine frame alignment mismatch")
    return decoded, truth


def _confusion(truth, cls):
    keep = ~np.isin(truth, score.EXCLUDED)
    return np.bincount((truth[keep].astype(np.int64) * 7 + cls[keep]), minlength=12 * 7).reshape(12, 7)


def _project_confusion(value):
    if not isinstance(value, list) or len(value) != 12 or any(not isinstance(r, list) or len(r) != 7 for r in value):
        _fail("E_ABSENT", "ABSENT 12x7 saved confusion")
    conf = np.asarray([[_int(v, "confusion cell") for v in row] for row in value], np.int64)
    conf[:, 0] += conf[:, 1]
    conf[:, 1] = 0
    return conf


def _score_unit(decoded, truth, original, cls, region_seq):
    """Scoring boundary: this function alone combines engine predictions with truth."""
    cov = np.asarray([np.full(decoded["grid"].N, labels.frame_meta(frame, {
        "tl": truth["tl"][f], "flags": truth["flags"][f]})["cov_ppm"], np.int64)
        for f, frame in enumerate(decoded["frame_ids"])])
    row = score.score_frames(
        truth["tl"], cls, truth["mv"], decoded["mv"], original["bench"], original["seed"],
        original["spp"], original["grid"], original["t0_q4"], cov=cov,
        flags=truth["flags"] if "mv_n_unobs" in original else None,
    )
    # Original C5/residual evidence belongs to the unchanged upstream engine run.
    # Its per-tile inputs are not reconstructed or silently manufactured here.
    for key in UPSTREAM_KEYS:
        if key in original:
            row[key] = original[key]
    bench = original["bench"]
    if bench == "B6":
        row["c1b_flips"] = score.c1b_flips(truth["tl"], cls)
    if bench == "B5o":
        if all("global_mv" in f for f in decoded["frame_rows"]):
            global_mv = [f["global_mv"] or [0, 0] for f in decoded["frame_rows"]]
            if any(not isinstance(v, list) or len(v) != 2 for v in global_mv):
                _fail("E_SCHEMA", "invalid global motion vector")
            global_mv = [[_int(x, "global vector", -128, 127) for x in v] for v in global_mv]
            row["b5o_global_mv"] = score.b5o_global_mv(truth["tl"], truth["mv"], global_mv)
    if bench in ("B1", "B2"):
        ious = score.s4_iou(truth["tl"], truth["ob"], decoded["grid"].mortons, region_seq)
        if ious:
            row["s4_iou_ppm"], row["s4_iou_n"] = ious[(len(ious) - 1) // 2], len(ious)
    return row


def _checks(rows, gate_seeds):
    gate = [r for r in rows if r["seed"] in gate_seeds]
    return {c["name"]: c for c in calibrate.check_targets(score.summarize(gate), True, gate, sorted(gate_seeds))}


def _recall_drops(before_rows, after_rows, before, after, gate_seeds):
    """Compare exact per-unit counts too, so ppm rounding cannot conceal a loss."""
    drops = set()
    for old, new in zip(before_rows, after_rows):
        if old["seed"] not in gate_seeds:
            continue
        for name, tl, field, op, _ in calibrate.TARGETS:
            if op != ">=":
                continue
            key = "dirty" if field == "dirty_ppm" else "cls_ok"
            a, b = old["tl"].get(tl), new["tl"].get(tl)
            if a and (not b or b["n"] != a["n"] or b[key] < a[key]):
                drops.add(name)
        for tl in ("9", "10", "11"):
            a, b = old["tl"].get(tl), new["tl"].get(tl)
            if a and (not b or b["n"] != a["n"] or b["cls_ok"] < a["cls_ok"] or b["dirty"] < a["dirty"]):
                drops.add(score.TL_NAMES[int(tl)].lower() + "_recall")
        if new["mv_exact"] < old["mv_exact"]:
            drops.add("mv_exact")
        if "mv_exact_raw" in old and new.get("mv_exact_raw", -1) < old["mv_exact_raw"]:
            drops.add("mv_exact_raw_ppm")
        if "moved_raw" in old:
            a, b = old["moved_raw"], new.get("moved_raw", {})
            if b.get("n") != a["n"] or b.get("cls_ok", -1) < a["cls_ok"]:
                drops.add("moved_recall_raw_ppm")
        if old["drift_duty_n"] and (not new["drift_duty_n"] or
                new["drift_duty_hit"] * old["drift_duty_n"] < old["drift_duty_hit"] * new["drift_duty_n"]):
            drops.add("drift_duty_ppm")
        for name, a in old["frozen_hit"].items():
            b = new["frozen_hit"][name]
            if b["n"] != a["n"] or b["hit"] < a["hit"]:
                drops.add("frozen_" + name + "_recall")
    for name, a in before.items():
        b = after[name]
        if a["op"] == ">=" and a["value"] is not None and (b["value"] is None or b["value"] < a["value"]):
            drops.add(name)
    return sorted(drops)


def _inputs(run, *, fitter=fit_noise):
    report = _json(_inside(run, run / "calib_report.json"))
    still_path = _inside(run, run / "still_manifest.json")
    still = _json(still_path)
    rows = _jsonl(_inside(run, run / "rows.jsonl"))
    calibration = report.get("calibration", report)
    if report.get("seeds") is not None and calibration.get("seeds") is not None and report["seeds"] != calibration["seeds"]:
        _fail("E_SPLIT", "run/calibration seed declarations disagree")
    seeds = report.get("seeds") or calibration.get("seeds")
    if not isinstance(seeds, dict):
        _fail("E_SPLIT", "ABSENT declared FIT/GATE split")
    fit_s, gate_s = _seed_set(seeds.get("fit"), "FIT"), _seed_set(seeds.get("gate"), "GATE")
    if fit_s & gate_s:
        _fail("E_SPLIT", "FIT/GATE seed overlap")
    if not rows:
        _fail("E_ABSENT", "ABSENT scoring rows")
    keys, captures, present = set(), {"fit": set(), "gate": set()}, set()
    for row in rows:
        if row.get("schema") != score.SCHEMA:
            _fail("E_SCHEMA", "not a calibration row")
        seed = _int(row.get("seed"), "row seed")
        if seed not in fit_s | gate_s:
            _fail("E_SPLIT", "row outside declared split")
        key = (row.get("bench"), seed)
        if key in keys:
            _fail("E_SPLIT", "duplicate bench/seed row")
        keys.add(key)
        present.add(seed)
        cap = row.get("capture_sha256")
        if not isinstance(cap, str) or not cap:
            _fail("E_SPLIT", "ABSENT row capture identity")
        captures["fit" if seed in fit_s else "gate"].add(cap)
    if present != fit_s | gate_s or captures["fit"] & captures["gate"]:
        _fail("E_SPLIT", "missing seed evidence or FIT/GATE capture overlap")
    benches = report.get("benches", [])
    metadata = {}
    for b in benches:
        key = (b.get("bench"), b.get("seed"))
        if key in metadata:
            _fail("E_SCHEMA", "duplicate saved bench metadata")
        metadata[key] = b
    if set(metadata) != keys:
        _fail("E_ABSENT", "ABSENT exactly one saved confusion per row")
    for (bench, seed), meta in metadata.items():
        expected_arm = "fit" if seed in fit_s else "gate"
        if meta.get("seed_group", expected_arm) != expected_arm:
            _fail("E_SPLIT", "saved bench seed_group disagrees with partition", bench=bench, seed=seed)
    manifest_digest_status = "ABSENT"
    if "source_sha256" in calibration:
        if calibration["source_sha256"] != hashlib.sha256(still_path.read_bytes()).hexdigest():
            _fail("E_BASELINE", "calibration still-manifest source digest mismatch")
        manifest_digest_status = "PASS"
    model = fitter(still, sorted(fit_s), [r for r in rows if r["seed"] in fit_s])
    model["manifest_digest_status"] = manifest_digest_status
    return rows, metadata, calibration, fit_s, gate_s, model


def _baseline(run, rows, metadata, calibration, fit_s, gate_s):
    baseline = {"status": "PASS", "class_absent_tiles": 0, "mixed_class_tiles": 0,
                "clean_class_absent_tiles": 0, "jitter_collapsed_tiles": 0, "units": [],
                "confusion_projection": "original columns UNCHANGED + JITTER -> UNCHANGED"}
    scored = []
    for original in rows:
        meta = metadata[(original["bench"], original["seed"])]
        decoded, truth = _load_unit(run, original, meta, fit_s)
        for key in ("class_absent_tiles", "mixed_class_tiles", "clean_class_absent_tiles", "jitter_collapsed_tiles"):
            baseline[key] += decoded[key]
        item = {"bench": original["bench"], "seed": original["seed"], "status": "PASS",
                "class_absent_tiles": decoded["class_absent_tiles"]}
        if decoded["mixed_class_tiles"]:
            item.update(status="ABSENT", reason="spatial classes within mixed dirty regions")
            baseline["status"] = "ABSENT"
            baseline["units"].append(item)
            continue
        expected = _project_confusion(meta.get("confusion"))
        if not np.array_equal(_confusion(truth["tl"], decoded["cls"]), expected):
            _fail("E_BASELINE", "decoded before confusion does not reproduce saved confusion",
                  bench=original["bench"], seed=original["seed"], class_absent_tiles=baseline["class_absent_tiles"])
        regions = [[r for r, _ in entries] for entries in decoded["entries"]]
        actual = _score_unit(decoded, truth, original, decoded["cls"], regions)
        for key in ROW_COMPARE:
            if key in original and actual.get(key) != original[key]:
                _fail("E_BASELINE", "decoded before row differs: " + key,
                      bench=original["bench"], seed=original["seed"], class_absent_tiles=baseline["class_absent_tiles"])
        scored.append(actual)
        baseline["units"].append(item)
    if baseline["status"] == "PASS":
        expected = calibration.get("summary")
        if not isinstance(expected, dict):
            _fail("E_ABSENT", "ABSENT calibration summary")
        actual = score.summarize([r for r in scored if r["seed"] in gate_s])
        for key, value in actual.items():
            if key not in expected or value != expected[key]:
                _fail("E_BASELINE", "decoded before GATE summary differs: " + key,
                      class_absent_tiles=baseline["class_absent_tiles"])
    return baseline, scored


def _absent_checks(gate_s):
    checks = _checks([], gate_s)
    for check in checks.values():
        check.update(value=None, status="FAIL", absent=True, reason="baseline spatial classes ABSENT")
    return checks


def _rescore(run, out, alphas, hard_ceiling, edge_ceiling_ppm, *, fitter):
    """Validate first, then sweep; never change a byte under run or an existing out."""
    if Path(out).is_symlink():
        _fail("E_ARGS", "--out must be a new directory, not a symlink")
    run, out = Path(run).resolve(), Path(out).resolve()
    if not run.is_dir():
        _fail("E_ARGS", "--run must be a directory")
    if out == run or run in out.parents or out in run.parents or out.exists():
        _fail("E_ARGS", "--out must be a NEW directory outside --run (never overwrite)")
    alphas = [_alpha(a) for a in alphas]
    if not alphas or len(set(alphas)) != len(alphas):
        _fail("E_ARGS", "supply a nonempty list of distinct alpha values")
    edge_ceiling_ppm = _int(edge_ceiling_ppm, "edge ceiling ppm", 0, 1000000)
    rows, metadata, calibration, fit_s, gate_s, model = _inputs(run, fitter=fitter)
    hard = float(model["R"]) if hard_ceiling is None else _finite(hard_ceiling, "hard ceiling", 0, FIELD_SAT_DISPLAY)
    baseline, before_rows = _baseline(run, rows, metadata, calibration, fit_s, gate_s)
    before = _checks(before_rows, gate_s) if baseline["status"] == "PASS" else _absent_checks(gate_s)
    results, alpha_rows = [], [[] for _ in alphas]
    counts = [{"regions": 0, "tiles": 0, "gate_regions": 0, "gate_tiles": 0} for _ in alphas]
    # All units and the GATE aggregate have passed the baseline assertion before
    # the first call to should_demote. Only one unit's arrays live in RAM at once.
    if baseline["status"] == "PASS":
        for original in rows:
            decoded, truth = _load_unit(run, original, metadata[(original["bench"], original["seed"])], fit_s)
            for i, alpha in enumerate(alphas):
                cls, retained = decoded["cls"].copy(), []
                for f, entries in enumerate(decoded["entries"]):
                    keep = []
                    for region, indices in entries:
                        if should_demote(region, model, alpha, hard, edge_ceiling_ppm):
                            cls[f, indices] = score.C_UNCHANGED
                            counts[i]["regions"] += 1
                            counts[i]["tiles"] += len(indices)
                            if original["seed"] in gate_s:
                                counts[i]["gate_regions"] += 1
                                counts[i]["gate_tiles"] += len(indices)
                        else:
                            keep.append(region)
                    retained.append(keep)
                alpha_rows[i].append(_score_unit(decoded, truth, original, cls, retained))
    for i, alpha in enumerate(alphas):
        after = _checks(alpha_rows[i], gate_s) if baseline["status"] == "PASS" else _absent_checks(gate_s)
        drops = _recall_drops(before_rows, alpha_rows[i], before, after, gate_s) if baseline["status"] == "PASS" else []
        reasons = (["baseline_classes_absent"] if baseline["status"] != "PASS" else [])
        if drops:
            reasons.append("recall_drop")
        if any(c["status"] == "FAIL" for c in after.values()):
            reasons.append("truth_targets_failed_or_absent")
        results.append({
            "alpha": alpha, "before": before, "after": after, "recall_drops": drops,
            "recall_drop_refused": bool(drops), "refused": bool(reasons), "refusal_reasons": reasons,
            "recall_comparison": "COMPLETE" if baseline["status"] == "PASS" else "ABSENT",
            "demoted_regions": counts[i]["regions"], "demoted_tiles": counts[i]["tiles"],
            "gate_demoted_regions": counts[i]["gate_regions"], "gate_demoted_tiles": counts[i]["gate_tiles"],
            "rows_file": "rows_alpha_%03d.jsonl" % i if baseline["status"] == "PASS" else None,
        })
    doc = {
        "schema": SCHEMA, "run": str(run), "certifying": False, "model": model,
        "rule": {"epsilon": "2*sqrt((ln(2)-ln(alpha))/n_tiles)",
                 "comparison": "(r_sum/n_tiles)/R <= mu/R + epsilon", "hard_ceiling": hard,
                 "edge_ceiling_ppm": edge_ceiling_ppm, "saturated_regions_kept": True,
                 "candidate_bound_at_most_R": hard <= model["R"]},
        "baseline": baseline, "results": results, "refused": any(r["refused"] for r in results),
        "scoring_population": "GATE only; FIT rows decoded for original-run baseline verification",
        "upstream_evidence_carried": ["c5_select", "fp_pop_n", "fp_resid_max_q4", "fp_resid_by_tl"],
        "region_policy": "demoted regions are removed from post-rule S4 IoU; upstream engine unchanged",
        "limitations": [
            "p99.9 is fitted, not a proven support bound; samples above R are counted and never clipped at R.",
            "Neighboring residual independence and the estimated FIT mean's error are not established.",
            "Still q4 uses unmasked previous-frame residuals; engine masking and DRIFT reference residuals differ.",
            "Original clean U/J locations and mixed dirty spatial classes may be ABSENT; counts are reported.",
            "C5 selection and raw residual evidence are carried from rows.jsonl, not recomputed after demotion.",
            "This alpha sweep is diagnostic and does not choose a winner using GATE or write an engine profile.",
        ],
    }
    # Fail if out appeared during the read-only computation. No exist_ok, no overwrite.
    out.mkdir(parents=True, exist_ok=False)
    (out / REPORT_NAME).write_text(json.dumps(doc, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (out / "NOISE_RESCORE.md").write_text(render_md(doc), encoding="utf-8")
    for result, output_rows in zip(results, alpha_rows):
        if result["rows_file"] is not None:
            (out / result["rows_file"]).write_text("".join(score.canon.dumps(r) for r in output_rows), encoding="utf-8")
    return doc


def render_md(doc):
    b, m = doc["baseline"], doc["model"]
    lines = ["# P5 offline noise rescore", "", "Diagnostic only; no profile or acceptance is issued.", "",
             "Baseline: **%s**. ABSENT exact per-tile classes: **%d** (%d mixed dirty, %d ambiguous clean)." % (
                 b["status"], b["class_absent_tiles"], b["mixed_class_tiles"], b["clean_class_absent_tiles"]),
             "Original JITTER tiles projected to UNCHANGED: %d." % b["jitter_collapsed_tiles"], "",
             "FIT samples: %d; rank: %d; R_q4: %d; display R: %s; display mu: %s; samples above R: %d." % (
                 m["sample_count"], m["rank"], m["R_q4"], m["R"], m["mu"], m["tail_samples"]),
             "Units: `%s`. Hard ceiling: %s; edge ceiling: %d ppm." % (
                 m["units"], doc["rule"]["hard_ceiling"], doc["rule"]["edge_ceiling_ppm"]),
             "Supplied sample digest: %s; supplied manifest digest: %s." % (
                 m["sample_digest_status"], m["manifest_digest_status"]), ""]
    for result in doc["results"]:
        lines += ["## Alpha %s" % result["alpha"], "",
                  "Demoted regions/tiles: %d/%d (GATE: %d/%d). Refused: **%s**. Recall loss: %s." % (
                      result["demoted_regions"], result["demoted_tiles"], result["gate_demoted_regions"],
                      result["gate_demoted_tiles"], result["refused"],
                      "ABSENT" if result["recall_comparison"] == "ABSENT" else ", ".join(result["recall_drops"]) or "none observed"), "",
                  "| Target | Bound | Before | After | After status |",
                  "|---|---|---|---|---|"]
        for name, before in result["before"].items():
            after = result["after"][name]
            show = lambda check: "ABSENT" if check["value"] is None else str(check["value"])
            lines.append("| %s | %s %s | %s | %s | %s |" % (
                name, before["op"], before["bound"], show(before), show(after), after["status"]))
        lines.append("")
    lines += ["## Limits and evidence", ""] + ["- " + value for value in doc["limitations"]]
    return "\n".join(lines) + "\n"
