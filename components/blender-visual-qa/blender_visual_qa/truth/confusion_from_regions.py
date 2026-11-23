"""Reconstruct truth-bench confusion matrices from persisted region records."""
import json
from pathlib import Path

import numpy as np

from blender_visual_qa.truth import pin as _pin

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


def _confusion_mismatch_cells(original, reconstructed):
    orig_conf = np.array(original, dtype=np.int64)
    recon_conf = np.array(reconstructed, dtype=np.int64)
    return int((orig_conf != recon_conf).sum())
