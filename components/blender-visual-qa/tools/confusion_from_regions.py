#!/usr/bin/env python3
"""tools/confusion_from_regions.py: offline reconstructor proving whether persisted unit artefacts suffice to re-score without frames.

Inputs per unit directory:
  - labels/tiles_truth.bin (read with score.read_truth_tiles)
  - vision/frames.jsonl (records with schema vqa.core-frame/v1 and per-class counts n)
  - vision/regions.jsonl (records with schema vqa.core-region/v1 carrying frame, cls, cls_hist, n_tiles and Morton spans)

CLI:
  python3 tools/confusion_from_regions.py --run DIR --out FILE
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from blender_visual_qa.truth import pin as _pin  # noqa: E402

_pin.pin()

from blender_visual_qa.truth import run_bench, score  # noqa: E402
from visioncore import regions as vreg  # noqa: E402


def find_truth_tiles_file(unit_dir):
    """Find tiles_truth.bin inside unit_dir."""
    unit_dir = Path(unit_dir)
    p1 = unit_dir / "labels" / "tiles_truth.bin"
    if p1.is_file():
        return p1
    p2 = unit_dir / "tiles_truth.bin"
    if p2.is_file():
        return p2
    for p in unit_dir.glob("labels*/tiles_truth.bin"):
        if p.is_file():
            return p
    return None


def process_unit_directory(unit_dir):
    """Process one unit directory and reconstruct tile predictions and confusion matrix."""
    unit_dir = Path(unit_dir)
    truth_bin_path = find_truth_tiles_file(unit_dir)
    if truth_bin_path is None:
        raise FileNotFoundError(f"Missing truth tiles in {unit_dir}")

    frames_jsonl_path = unit_dir / "vision" / "frames.jsonl"
    regions_jsonl_path = unit_dir / "vision" / "regions.jsonl"

    if not frames_jsonl_path.is_file():
        raise FileNotFoundError(f"Missing {frames_jsonl_path}")
    if not regions_jsonl_path.is_file():
        raise FileNotFoundError(f"Missing {regions_jsonl_path}")

    tt = score.read_truth_tiles(truth_bin_path)
    truth_tl = tt["tl"]  # shape (F, N)
    F, N = truth_tl.shape

    # Read frames.jsonl
    frame_records = []
    with open(frames_jsonl_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rec = json.loads(line)
                if rec.get("schema") != "vqa.core-frame/v1":
                    raise ValueError(f"Invalid schema in {frames_jsonl_path}: {rec.get('schema')}")
                frame_records.append(rec)

    if len(frame_records) != F:
        raise ValueError(
            f"Frame count mismatch: tiles_truth has {F} frames, frames.jsonl has {len(frame_records)}"
        )

    # Read regions.jsonl
    regions_by_frame = {f: [] for f in range(F)}
    with open(regions_jsonl_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rec = json.loads(line)
                if rec.get("schema") != "vqa.core-region/v1":
                    raise ValueError(f"Invalid schema in {regions_jsonl_path}: {rec.get('schema')}")
                f_idx = rec["frame"]
                if 0 <= f_idx < F:
                    regions_by_frame[f_idx].append(rec)

    pred_cls = np.zeros((F, N), dtype=np.int64)
    unrecoverable_mask = np.zeros((F, N), dtype=bool)

    for f in range(F):
        f_rec = frame_records[f]
        expected_n = f_rec.get("n", [])

        for r in regions_by_frame[f]:
            morts = vreg.region_tiles(r)
            cls_hist = r.get("cls_hist", [])
            if isinstance(cls_hist, dict):
                nonzero_classes = [int(k) for k, v in cls_hist.items() if v > 0]
            else:
                nonzero_classes = [i for i, v in enumerate(cls_hist) if v > 0]

            if len(nonzero_classes) == 1:
                c = nonzero_classes[0]
                for m in morts:
                    if 0 <= m < N:
                        pred_cls[f, m] = c
            else:
                for m in morts:
                    if 0 <= m < N:
                        unrecoverable_mask[f, m] = True

        # Cross-check per-frame class counts against frames.jsonl n
        if expected_n:
            if not unrecoverable_mask[f].any():
                actual_n = np.bincount(pred_cls[f], minlength=len(expected_n)).tolist()
                if actual_n[: len(expected_n)] != expected_n:
                    raise ValueError(
                        f"Frame {f} class count mismatch: expected {expected_n}, got {actual_n[:len(expected_n)]}"
                    )
            else:
                rec_cls = pred_cls[f][~unrecoverable_mask[f]]
                if rec_cls.size > 0:
                    counts = np.bincount(rec_cls, minlength=len(expected_n))
                    for c_idx, count in enumerate(counts[: len(expected_n)]):
                        if count > expected_n[c_idx]:
                            raise ValueError(
                                f"Frame {f} class {c_idx} recoverable count {count} exceeds expected {expected_n[c_idx]}"
                            )

    # Build confusion matrix
    conf_matrix = run_bench.confusion(truth_tl, pred_cls)
    unrecoverable_tiles = int(unrecoverable_mask.sum())

    return {
        "confusion": conf_matrix.tolist(),
        "unrecoverable_tiles": unrecoverable_tiles,
        "frames": F,
        "tiles": N,
    }


def locate_unit_directories(run_dir):
    """Locate unit directories in run_dir and match them with calib_report.json."""
    run_dir = Path(run_dir)
    calib_report_path = run_dir / "calib_report.json"
    calib_report = None
    if calib_report_path.is_file():
        calib_report = json.loads(calib_report_path.read_text(encoding="utf-8"))

    candidates = []
    if find_truth_tiles_file(run_dir) and (run_dir / "vision" / "frames.jsonl").is_file():
        candidates.append(run_dir)
    for sub in run_dir.rglob("*"):
        if sub.is_dir() and find_truth_tiles_file(sub) and (sub / "vision" / "frames.jsonl").is_file():
            candidates.append(sub)

    units = {}
    if calib_report and "benches" in calib_report:
        for item in calib_report["benches"]:
            b = item.get("bench")
            s = item.get("seed")
            target_key = (b, s)

            matched = None
            for cand in candidates:
                if cand == run_dir and len(calib_report["benches"]) == 1:
                    matched = cand
                    break
                cand_rel = cand.relative_to(run_dir).as_posix()
                if (
                    cand_rel == str(b)
                    or cand_rel == f"{b}_s{s}"
                    or cand_rel == f"{b}/{s}"
                    or cand_rel == f"{b}_seed{s}"
                    or cand.name == str(b)
                    or cand.name == f"{b}_s{s}"
                ):
                    matched = cand
                    break

            if matched is None and len(candidates) == 1 and len(calib_report["benches"]) == 1:
                matched = candidates[0]

            if matched is not None:
                units[target_key] = matched
    else:
        for cand in candidates:
            units[(cand.name, 0)] = cand

    return units, calib_report


def main(argv=None):
    ap = argparse.ArgumentParser(prog="confusion_from_regions")
    ap.add_argument("--run", required=True, help="Run directory containing unit artifacts")
    ap.add_argument("--out", required=True, help="Output file for reconstruction report")
    args = ap.parse_args(argv)

    run_dir = Path(args.run).resolve()
    out_file = Path(args.out).resolve()

    if not run_dir.is_dir():
        raise SystemExit(f"Error: --run directory {run_dir} does not exist")

    units, calib_report = locate_unit_directories(run_dir)
    if not units:
        raise SystemExit(f"Error: no unit directories found in {run_dir}")

    total_unrecoverable = 0
    total_mismatch_cells = 0
    exact_match_all = True
    unit_reports = []

    if calib_report and "benches" in calib_report:
        for item in calib_report["benches"]:
            b = item.get("bench")
            s = item.get("seed")
            key = (b, s)
            unit_dir = units.get(key)
            if unit_dir is None:
                raise SystemExit(f"Error: unit directory for bench {b} seed {s} not found in {run_dir}")

            u_res = process_unit_directory(unit_dir)
            orig_conf = np.array(item["confusion"], dtype=np.int64)
            recon_conf = np.array(u_res["confusion"], dtype=np.int64)

            mismatch_count = int((orig_conf != recon_conf).sum())
            unrec_count = u_res["unrecoverable_tiles"]
            exact_match = (mismatch_count == 0) and (unrec_count == 0)

            total_unrecoverable += unrec_count
            total_mismatch_cells += mismatch_count
            if not exact_match:
                exact_match_all = False

            unit_reports.append({
                "bench": b,
                "seed": s,
                "exact_match": exact_match,
                "mismatch_cells": mismatch_count,
                "unrecoverable_tiles": unrec_count,
                "confusion_reconstructed": u_res["confusion"],
                "confusion_original": item["confusion"],
            })
    else:
        for (b, s), unit_dir in units.items():
            u_res = process_unit_directory(unit_dir)
            unrec_count = u_res["unrecoverable_tiles"]
            total_unrecoverable += unrec_count
            unit_reports.append({
                "bench": b,
                "seed": s,
                "unrecoverable_tiles": unrec_count,
                "confusion_reconstructed": u_res["confusion"],
            })

    report_doc = {
        "exact_match": exact_match_all if calib_report else None,
        "mismatch_cells": total_mismatch_cells,
        "unrecoverable_tiles": total_unrecoverable,
        "units": unit_reports,
    }

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(report_doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    print(f"Exact match: {exact_match_all if calib_report else 'N/A'}")
    print(f"Mismatch cells: {total_mismatch_cells}")
    print(f"Unrecoverable tiles: {total_unrecoverable}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
