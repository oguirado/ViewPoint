#!/usr/bin/env python3
"""Synthetic static false-positive cause fixture generator.

Generates a persisted run tree layout readable by tools/noise_rescore.py
and blender_visual_qa.truth.score, plus expected_causes.json pinning
static false-positive cause claims (SHADE, EDGE_BLEED, GLOBAL_EXPOSURE,
AA_JITTER, UNATTRIBUTED).
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from blender_visual_qa.truth import calibrate, labels, score  # noqa: E402
from visioncore import grid as vgrid  # noqa: E402


SCHEMA_EXPECTED = "bvqa.fp-cause-expected/v1"
FP_POP = (0, 7)  # STATIC (0), SHADE_WEAK (7)
DIRTY_TL = (3, 4, 5, 6, 9, 10, 11)


def classify_fp_tile(frame_idx, tile_idx, grid, tl_matrix, cls_matrix):
    """Determine the FP cause for a static tile at (frame_idx, tile_idx).

    Priority:
    1. GLOBAL_EXPOSURE: all static tiles in this frame are predicted dirty
    2. SHADE: truth tl == 7 (SHADE_WEAK)
    3. EDGE_BLEED: 2D grid neighbour of a truth-dirty tile
    4. AA_JITTER: class verdict flips on consecutive frames (f >= 1)
    5. UNATTRIBUTED: none of the above
    """
    nf, N = tl_matrix.shape
    tl_val = int(tl_matrix[frame_idx, tile_idx])
    if tl_val not in FP_POP or int(cls_matrix[frame_idx, tile_idx]) < score.DIRTY_MIN:
        return None

    # Check GLOBAL_EXPOSURE: every static tile in frame_idx is predicted dirty
    static_indices = [i for i in range(N) if int(tl_matrix[frame_idx, i]) in FP_POP]
    if static_indices and all(int(cls_matrix[frame_idx, i]) >= score.DIRTY_MIN for i in static_indices):
        return "GLOBAL_EXPOSURE"

    if tl_val == 7:
        return "SHADE"

    # Check EDGE_BLEED: 2D neighbour of a truth-dirty tile in frame_idx
    tx0, ty0 = int(grid.tx[tile_idx]), int(grid.ty[tile_idx])
    has_dirty_neighbour = False
    for i in range(N):
        if int(tl_matrix[frame_idx, i]) in DIRTY_TL:
            tx1, ty1 = int(grid.tx[i]), int(grid.ty[i])
            if abs(tx0 - tx1) <= 1 and abs(ty0 - ty1) <= 1 and (tx0, ty0) != (tx1, ty1):
                has_dirty_neighbour = True
                break
    if has_dirty_neighbour:
        return "EDGE_BLEED"

    # Check AA_JITTER: verdict flips between consecutive frames (evaluating frames >= 1)
    curr_dirty = int(cls_matrix[frame_idx, tile_idx]) >= score.DIRTY_MIN
    flips = False
    if frame_idx >= 2:
        prev_dirty = int(cls_matrix[frame_idx - 1, tile_idx]) >= score.DIRTY_MIN
        if curr_dirty != prev_dirty:
            flips = True
    if frame_idx + 1 < nf:
        next_dirty = int(cls_matrix[frame_idx + 1, tile_idx]) >= score.DIRTY_MIN
        if curr_dirty != next_dirty:
            flips = True
    if flips:
        return "AA_JITTER"

    return "UNATTRIBUTED"


def generate_fixtures(out_dir, seed=42):
    """Generate synthetic run tree and expected_causes.json in out_dir."""
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    fit_seed = int(seed)
    gate_seed = int(seed) + 100
    fit_cap = hashlib.sha256(("fp_cause:fit:%d" % fit_seed).encode("utf-8")).hexdigest()
    gate_cap = hashlib.sha256(("fp_cause:gate:%d" % gate_seed).encode("utf-8")).hexdigest()

    g = vgrid.Grid(64, 64)  # 4x4 tiles = 16 tiles
    nf = 4

    # Setup ground truth labels
    tl = np.zeros((nf, g.N), dtype=np.uint8)
    flags = np.zeros_like(tl)
    truth_mv = np.zeros(tl.shape + (2,), dtype=np.int8)
    ob = np.zeros(tl.shape, dtype=np.uint16)

    # Tile assignments:
    # Tile 0 (tx=0, ty=0): SHADE_WEAK (tl = 7)
    tl[:, 0] = 7
    # Tile 1 (tx=1, ty=0): RIGID (tl = 3, truth dirty)
    tl[:, 1] = 3
    truth_mv[:, 1] = (3, -2)
    # Tile 4 (tx=2, ty=0): STATIC (tl = 0), neighbour of Tile 1
    # Tile 12 (tx=2, ty=2): STATIC (tl = 0), isolated (UNATTRIBUTED)
    # Tile 15 (tx=3, ty=3): STATIC (tl = 0), isolated (AA_JITTER)

    # Predicted classes
    cls_pred = np.zeros((nf, g.N), dtype=np.uint8)

    # Frame 0: all clean (t=0)
    # Frame 1 (t=1):
    cls_pred[1, 0] = 2   # SHADE (tl=7)
    cls_pred[1, 4] = 2   # EDGE_BLEED (tl=0, neighbour of tile 1 RIGID)
    cls_pred[1, 12] = 2  # UNATTRIBUTED (tl=0, isolated, dirty in f=1 and f=2)
    cls_pred[1, 15] = 2  # AA_JITTER (flips between f=1 dirty and f=2 clean)

    # Frame 2 (t=2):
    cls_pred[2, 0] = 2   # SHADE
    cls_pred[2, 4] = 2   # EDGE_BLEED
    cls_pred[2, 12] = 2  # UNATTRIBUTED (dirty in f=1 and f=2)
    cls_pred[2, 15] = 0  # AA_JITTER clean in frame 2

    # Frame 3 (t=3):
    # Whole-frame static exposure: all static tiles in frame 3 are dirty
    for i in range(g.N):
        if int(tl[3, i]) in FP_POP:
            cls_pred[3, i] = 2  # GLOBAL_EXPOSURE

    # Build GATE unit (B1)
    gate_unit = out_dir / "gate" / ("seed_%06d" % gate_seed) / "B1"
    (gate_unit / "vision").mkdir(parents=True, exist_ok=True)
    (gate_unit / "labels").mkdir(parents=True, exist_ok=True)

    labs = [
        {
            "tl": tl[f],
            "flags": flags[f],
            "mv": truth_mv[f],
            "ob_major": ob[f],
            "occ_permille": np.zeros(g.N, dtype=np.uint16),
        }
        for f in range(nf)
    ]
    blob = labels.pack_truth_tiles(g, labs)
    (gate_unit / "labels" / "tiles_truth.bin").write_bytes(blob)

    frame_rows = []
    region_rows = []
    for f in range(nf):
        f_regs = []
        for i in range(g.N):
            c = int(cls_pred[f, i])
            if c >= score.DIRTY_MIN:
                m_hex = "%04x" % int(g.mortons[i])
                r = {
                    "schema": "vqa.core-region/v1",
                    "frame": f,
                    "grid": g.ident,
                    "cls": "CHANGED",
                    "n_tiles": 1,
                    "r_sum": 100,
                    "r_max": 100,
                    "edge_ppm": 0,
                    "cls_hist": [0, 0, 1, 0, 0, 0, 0],
                    "group": "CHG",
                    "spans": [[m_hex, m_hex]],
                }
                f_regs.append(r)
                region_rows.append(r)
        frame_rows.append({
            "schema": "vqa.core-frame/v1",
            "frame": f,
            "grid": g.ident,
            "n": np.bincount(cls_pred[f], minlength=7).tolist(),
            "regions": len(f_regs),
            "global_mv": [0, 0],
        })

    with open(gate_unit / "vision" / "frames.jsonl", "w", encoding="utf-8") as fh:
        for fr in frame_rows:
            fh.write(json.dumps(fr, sort_keys=True) + "\n")
    with open(gate_unit / "vision" / "regions.jsonl", "w", encoding="utf-8") as fh:
        for rr in region_rows:
            fh.write(json.dumps(rr, sort_keys=True) + "\n")

    # Score GATE unit
    cov = np.zeros((nf, g.N), dtype=np.int64)
    pred_mv = np.zeros((nf, g.N, 2), dtype=np.int64)
    gate_row = score.score_frames(tl, cls_pred, truth_mv, pred_mv, "B1", gate_seed, 4, g.ident, 32, cov=cov, flags=flags)
    gate_row["capture_sha256"] = gate_cap

    conf = np.zeros((12, 7), dtype=np.int64)
    keep = ~np.isin(tl, score.EXCLUDED)
    np.add.at(conf, (tl[keep].astype(np.int64), cls_pred[keep].astype(np.int64)), 1)

    benches = [{
        "bench": "B1",
        "seed": gate_seed,
        "seed_group": "gate",
        "grid": g.ident,
        "frames": nf,
        "tiles": g.N,
        "labels_sha256": hashlib.sha256(blob).hexdigest(),
        "confusion": conf.tolist(),
    }]

    # Build FIT unit (B6)
    fit_unit = out_dir / "fit" / ("seed_%06d" % fit_seed) / "B6"
    (fit_unit / "vision").mkdir(parents=True, exist_ok=True)
    (fit_unit / "labels").mkdir(parents=True, exist_ok=True)

    fit_tl = np.zeros((nf, g.N), dtype=np.uint8)
    fit_cls = np.zeros((nf, g.N), dtype=np.uint8)
    fit_labs = [
        {
            "tl": fit_tl[f],
            "flags": flags[f],
            "mv": truth_mv[f],
            "ob_major": ob[f],
            "occ_permille": np.zeros(g.N, dtype=np.uint16),
        }
        for f in range(nf)
    ]
    fit_blob = labels.pack_truth_tiles(g, fit_labs)
    (fit_unit / "labels" / "tiles_truth.bin").write_bytes(fit_blob)

    fit_frame_rows = [
        {
            "schema": "vqa.core-frame/v1",
            "frame": f,
            "grid": g.ident,
            "n": [g.N, 0, 0, 0, 0, 0, 0],
            "regions": 0,
            "global_mv": [0, 0],
        }
        for f in range(nf)
    ]
    with open(fit_unit / "vision" / "frames.jsonl", "w", encoding="utf-8") as fh:
        for fr in fit_frame_rows:
            fh.write(json.dumps(fr, sort_keys=True) + "\n")
    with open(fit_unit / "vision" / "regions.jsonl", "w", encoding="utf-8") as fh:
        pass  # empty

    fit_row = score.score_frames(fit_tl, fit_cls, truth_mv, pred_mv, "B6", fit_seed, 4, g.ident, 32, cov=cov, flags=flags)
    fit_row["capture_sha256"] = fit_cap

    fit_conf = np.zeros((12, 7), dtype=np.int64)
    keep_fit = ~np.isin(fit_tl, score.EXCLUDED)
    np.add.at(fit_conf, (fit_tl[keep_fit].astype(np.int64), fit_cls[keep_fit].astype(np.int64)), 1)

    benches.append({
        "bench": "B6",
        "seed": fit_seed,
        "seed_group": "fit",
        "grid": g.ident,
        "frames": nf,
        "tiles": g.N,
        "labels_sha256": hashlib.sha256(fit_blob).hexdigest(),
        "confusion": fit_conf.tolist(),
    })

    rows = [fit_row, gate_row]
    summary = score.summarize([gate_row])

    report = {
        "schema": "bvqa.run-report/v1",
        "grid": g.ident,
        "spp": 4,
        "frames": nf,
        "renderer": "synthetic-json-only",
        "seeds": {"fit": [fit_seed], "gate": [gate_seed]},
        "frozen_t0_q4": 32,
        "benches": benches,
        "calibration": {
            "summary": summary,
            "checks": calibrate.check_targets(summary, True, [gate_row], [gate_seed]),
        },
    }

    still = {
        "samples_q4": [8] * g.N,
        "still_meta": {"seeds": [fit_seed], "capture_sha256": [fit_cap]},
    }

    with open(out_dir / "rows.jsonl", "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(score.canon.dumps(r))
    with open(out_dir / "still_manifest.json", "w", encoding="utf-8") as fh:
        fh.write(json.dumps(still, sort_keys=True, indent=2) + "\n")
    with open(out_dir / "calib_report.json", "w", encoding="utf-8") as fh:
        fh.write(json.dumps(report, sort_keys=True, indent=2) + "\n")

    # Analyze and collect expected causes on injected tiles
    counts = {"AA_JITTER": 0, "EDGE_BLEED": 0, "GLOBAL_EXPOSURE": 0, "SHADE": 0, "UNATTRIBUTED": 0}
    injected_tiles = []
    for f in range(nf):
        for i in range(g.N):
            cause = classify_fp_tile(f, i, g, tl, cls_pred)
            if cause is not None:
                counts[cause] += 1
                injected_tiles.append({
                    "bench": "B1",
                    "cause": cause,
                    "frame": f,
                    "pred_cls": int(cls_pred[f, i]),
                    "seed": gate_seed,
                    "tile": i,
                    "truth_tl": int(tl[f, i]),
                    "tx": int(g.tx[i]),
                    "ty": int(g.ty[i]),
                })

    expected_doc = {
        "schema": SCHEMA_EXPECTED,
        "seed": int(seed),
        "counts": counts,
        "total_injected_tiles": len(injected_tiles),
        "injected_tiles": injected_tiles,
        "timings": {
            "generation": "VM",
        },
    }

    with open(out_dir / "expected_causes.json", "w", encoding="utf-8") as fh:
        fh.write(json.dumps(expected_doc, sort_keys=True, indent=2) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="output directory for synthetic run tree")
    parser.add_argument("--seed", type=int, default=42, help="random seed / run seed")
    args = parser.parse_args(argv)

    generate_fixtures(args.out, args.seed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
