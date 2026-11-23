"""Rescore region noise after validating and recovering the exact persisted tile classes."""
import hashlib
import json
from pathlib import Path

import numpy as np

from . import calibrate, labels, noise_rescore, score, tile_classes
from visioncore.errors import VqaError

SCHEMA = noise_rescore.SCHEMA
EDGE_CEILING_PPM = noise_rescore.EDGE_CEILING_PPM
REPORT_NAME = noise_rescore.REPORT_NAME
ROW_COMPARE = noise_rescore.ROW_COMPARE
UPSTREAM_KEYS = noise_rescore.UPSTREAM_KEYS
NoiseRescoreError = noise_rescore.NoiseRescoreError

_fail = noise_rescore._fail
_int = noise_rescore._int
_finite = noise_rescore._finite
_alpha = noise_rescore._alpha
_seed_set = noise_rescore._seed_set
fit_noise = noise_rescore.fit_noise
should_demote = noise_rescore.should_demote
_json = noise_rescore._json
_jsonl = noise_rescore._jsonl
_inside = noise_rescore._inside
_grid = noise_rescore._grid
_hist = noise_rescore._hist
_unit_path = noise_rescore._unit_path
_confusion = noise_rescore._confusion
_project_confusion = noise_rescore._project_confusion
_score_unit = noise_rescore._score_unit
_checks = noise_rescore._checks
_recall_drops = noise_rescore._recall_drops
_inputs = noise_rescore._inputs
_absent_checks = noise_rescore._absent_checks


def fill_tile_classes(decoded, tc_rows):
    """Fill tiles that decode_regions marked -1 from tile_cls.jsonl rows after verifying grid, frames, and sha256."""
    if not isinstance(tc_rows, list) or not tc_rows:
        _fail("E_ABSENT", "ABSENT tile_cls.jsonl rows")
    frame_ids = decoded["frame_ids"]
    grid = decoded["grid"]
    if len(tc_rows) != len(frame_ids):
        _fail("E_ABSENT", "tile_cls.jsonl frame count mismatch")
    for f, (tc, frame_id) in enumerate(zip(tc_rows, frame_ids)):
        if not isinstance(tc, dict) or tc.get("schema") != tile_classes.SCHEMA:
            _fail("E_ABSENT", "tile_cls.jsonl schema mismatch")
        if tc.get("grid") != grid.ident:
            _fail("E_ABSENT", "tile_cls.jsonl grid mismatch")
        if isinstance(tc.get("frame"), bool) or tc.get("frame") != frame_id:
            _fail("E_ABSENT", "tile_cls.jsonl frame id mismatch")
        if isinstance(tc.get("n_tiles"), bool) or tc.get("n_tiles") != grid.N:
            _fail("E_ABSENT", "tile_cls.jsonl n_tiles mismatch")
        sha = tc.get("sha256")
        if not isinstance(sha, str) or len(sha) != 64:
            _fail("E_ABSENT", "ABSENT per-frame tile_cls.jsonl sha256")
        try:
            tc_cls = tile_classes.decode_rle(tc.get("rle"), n_tiles=grid.N)
        except VqaError as exc:
            _fail("E_ABSENT", "invalid tile_cls.jsonl rle: %s" % exc)
        actual_sha = hashlib.sha256(tc_cls.tobytes()).hexdigest()
        if actual_sha != sha:
            _fail("E_ABSENT", "tile_cls.jsonl per-frame sha256 mismatch", frame=frame_id)
        expected_hist = decoded["frame_rows"][f].get("n")
        if expected_hist is not None and np.bincount(tc_cls, minlength=7).tolist() != expected_hist:
            _fail("E_ABSENT", "tile_cls.jsonl histogram disagrees with frame row", frame=frame_id)
        cur = decoded["cls"][f]
        pure_dirty = cur >= score.DIRTY_MIN
        if not np.array_equal(tc_cls[pure_dirty], cur[pure_dirty]):
            _fail("E_ABSENT", "tile_cls.jsonl disagrees with pure region classes", frame=frame_id)
        clean_mask = cur == 0
        if (tc_cls[clean_mask] >= score.DIRTY_MIN).any():
            _fail("E_ABSENT", "tile_cls.jsonl marks uncovered tile dirty", frame=frame_id)
        for region, indices in decoded["entries"][f]:
            if np.bincount(tc_cls[indices], minlength=7).tolist() != region.get("cls_hist"):
                _fail("E_ABSENT", "tile_cls.jsonl disagrees with region cls_hist", frame=frame_id)
        neg = cur == -1
        if neg.any():
            cur[neg] = tc_cls[neg].astype(np.int8)
    absent = int((decoded["cls"] == -1).sum())
    decoded["mixed_class_tiles"] = absent
    decoded["class_absent_tiles"] = absent + decoded["clean_class_absent_tiles"]
    decoded["absent_tiles"] = absent
    return decoded


def decode_regions(frame_rows, region_rows, grid, tc_rows=None):
    """Decode engine regions and, when tc_rows are supplied, fill -1 mixed tiles from tile_cls.jsonl."""
    decoded = noise_rescore.decode_regions(frame_rows, region_rows, grid)
    if tc_rows is not None:
        return fill_tile_classes(decoded, tc_rows)
    decoded["absent_tiles"] = decoded["mixed_class_tiles"]
    return decoded


def _load_unit(run, row, metadata, fit_s):
    decoded, truth = noise_rescore._load_unit(run, row, metadata, fit_s)
    unit = _unit_path(run, row, fit_s)
    tc_rows = _jsonl(_inside(run, unit / "vision" / tile_classes.FILE_NAME))
    fill_tile_classes(decoded, tc_rows)
    return decoded, truth


def _baseline(run, rows, metadata, calibration, fit_s, gate_s):
    baseline = {
        "status": "PASS", "absent_tiles": 0, "class_absent_tiles": 0,
        "mixed_class_tiles": 0, "clean_class_absent_tiles": 0,
        "jitter_collapsed_tiles": 0, "units": [],
        "confusion_projection": "original columns UNCHANGED + JITTER -> UNCHANGED",
    }
    scored = []
    for original in rows:
        meta = metadata[(original["bench"], original["seed"])]
        decoded, truth = _load_unit(run, original, meta, fit_s)
        for key in ("absent_tiles", "class_absent_tiles", "mixed_class_tiles",
                    "clean_class_absent_tiles", "jitter_collapsed_tiles"):
            baseline[key] += decoded[key]
        item = {
            "bench": original["bench"], "seed": original["seed"], "status": "PASS",
            "absent_tiles": decoded["absent_tiles"],
            "class_absent_tiles": decoded["class_absent_tiles"],
        }
        if decoded["absent_tiles"]:
            item.update(status="ABSENT", reason="spatial classes within mixed dirty regions")
            baseline["status"] = "ABSENT"
            baseline["units"].append(item)
            continue
        expected = _project_confusion(meta.get("confusion"))
        if not np.array_equal(_confusion(truth["tl"], decoded["cls"]), expected):
            _fail("E_BASELINE", "decoded before confusion does not reproduce saved confusion",
                  bench=original["bench"], seed=original["seed"],
                  absent_tiles=baseline["absent_tiles"],
                  class_absent_tiles=baseline["class_absent_tiles"])
        regions = [[r for r, _ in entries] for entries in decoded["entries"]]
        actual = _score_unit(decoded, truth, original, decoded["cls"], regions)
        for key in ROW_COMPARE:
            if key in original and actual.get(key) != original[key]:
                _fail("E_BASELINE", "decoded before row differs: " + key,
                      bench=original["bench"], seed=original["seed"],
                      absent_tiles=baseline["absent_tiles"],
                      class_absent_tiles=baseline["class_absent_tiles"])
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
                      absent_tiles=baseline["absent_tiles"],
                      class_absent_tiles=baseline["class_absent_tiles"])
    return baseline, scored


def rescore(run, out, alphas, hard_ceiling=None, edge_ceiling_ppm=EDGE_CEILING_PPM):
    """Validate first (including tile_cls.jsonl), then sweep; never modify --run or an existing --out."""
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
    rows, metadata, calibration, fit_s, gate_s, model = _inputs(run)
    hard = float(model["R"]) if hard_ceiling is None else _finite(hard_ceiling, "hard ceiling", 0, noise_rescore.FIELD_SAT_DISPLAY)
    baseline, before_rows = _baseline(run, rows, metadata, calibration, fit_s, gate_s)
    before = _checks(before_rows, gate_s) if baseline["status"] == "PASS" else _absent_checks(gate_s)
    results, alpha_rows = [], [[] for _ in alphas]
    counts = [{"regions": 0, "tiles": 0, "gate_regions": 0, "gate_tiles": 0} for _ in alphas]
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
        "schema": SCHEMA, "run": str(run), "certifying": False,
        "absent_tiles": baseline["absent_tiles"], "model": model,
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
            "Mixed dirty spatial classes are recovered from tile_cls.jsonl; original clean U/J stays projected to UNCHANGED.",
            "C5 selection and raw residual evidence are carried from rows.jsonl, not recomputed after demotion.",
            "This alpha sweep is diagnostic and does not choose a winner using GATE or write an engine profile.",
        ],
    }
    out.mkdir(parents=True, exist_ok=False)
    (out / REPORT_NAME).write_text(json.dumps(doc, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (out / "NOISE_RESCORE.md").write_text(render_md(doc), encoding="utf-8")
    for result, output_rows in zip(results, alpha_rows):
        if result["rows_file"] is not None:
            (out / result["rows_file"]).write_text("".join(score.canon.dumps(r) for r in output_rows), encoding="utf-8")
    return doc


def render_md(doc):
    text = noise_rescore.render_md(doc)
    return "absent_tiles: %d\n" % doc.get("absent_tiles", doc["baseline"].get("absent_tiles", 0)) + text
