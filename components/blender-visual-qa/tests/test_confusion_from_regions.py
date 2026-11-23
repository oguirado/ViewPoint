"""tests/test_confusion_from_regions.py: unit tests for tools/confusion_from_regions.py."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

from blender_visual_qa.truth import labels, pin as _pin, run_bench, score
from tools import confusion_from_regions

_pin.pin()
from visioncore import grid as vgrid


def hash_dir(d):
    """Compute SHA-256 for every file in directory d."""
    res = {}
    p = Path(d)
    for f in sorted(p.rglob("*")):
        if f.is_file():
            rel = f.relative_to(p).as_posix()
            res[rel] = hashlib.sha256(f.read_bytes()).hexdigest()
    return res


def make_synthetic_unit(
    d,
    n_frames=2,
    grid_w=32,
    grid_h=16,
    bench="B1",
    seed=0,
    mixed_region=False,
    frame_mismatch=False,
    empty_regions=False,
):
    """Build a synthetic unit directory for testing in d."""
    run_dir = Path(d) / "run"
    unit_dir = run_dir / bench
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "labels").mkdir(exist_ok=True)
    (unit_dir / "vision").mkdir(exist_ok=True)

    g = vgrid.Grid(grid_w, grid_h)  # s=16, gw=2, gh=1, N=2

    # Create truth labels
    truth_labs = []
    for f in range(n_frames):
        tl_arr = np.zeros(g.N, dtype=np.uint8)  # STATIC
        fl_arr = np.zeros(g.N, dtype=np.uint8)
        mv_arr = np.zeros((g.N, 2), dtype=np.int8)
        ob_arr = np.ones(g.N, dtype=np.uint16)
        oc_arr = np.zeros(g.N, dtype=np.uint16)
        truth_labs.append({
            "tl": tl_arr,
            "flags": fl_arr,
            "mv": mv_arr,
            "ob_major": ob_arr,
            "occ_permille": oc_arr,
        })

    blob = labels.pack_truth_tiles(g, truth_labs, f0=0)
    (unit_dir / "labels" / "tiles_truth.bin").write_bytes(blob)

    # Write frames.jsonl
    frames_jsonl_count = n_frames - 1 if frame_mismatch else n_frames
    frames_lines = []
    for f in range(frames_jsonl_count):
        rec = {
            "schema": "vqa.core-frame/v1",
            "frame": f,
            "grid": g.ident,
            "n": [2 if not mixed_region else 1, 0, 0 if not mixed_region else 0, 0, 0, 0, 0],
            "cov_ppm": 0,
            "global_mv": [0, 0],
            "regions": 0 if empty_regions else 1,
            "tiles_sha256": "0" * 64,
        }
        frames_lines.append(json.dumps(rec))

    (unit_dir / "vision" / "frames.jsonl").write_text("\n".join(frames_lines) + "\n", encoding="utf-8")

    # Write regions.jsonl
    regions_lines = []
    if not empty_regions:
        for f in range(frames_jsonl_count):
            cls_hist = [0, 5, 5, 0, 0, 0, 0] if mixed_region else [2, 0, 0, 0, 0, 0, 0]
            rec = {
                "schema": "vqa.core-region/v1",
                "id": f"r:{f:04x}",
                "frame": f,
                "grid": g.ident,
                "cls": "MIXED" if mixed_region else "UNCHANGED",
                "n_tiles": 2,
                "bbox_t": [0, 0, 1, 0],
                "bbox_px": [0, 0, 32, 16],
                "mv": [0, 0],
                "r_max": 0,
                "r_sum": 0,
                "edge_ppm": 0,
                "kind": "SMOOTH",
                "shade_only": False,
                "cls_hist": cls_hist,
                "spans": [["0000", "0001"]],
            }
            regions_lines.append(json.dumps(rec))

    (unit_dir / "vision" / "regions.jsonl").write_text("\n".join(regions_lines) + "\n", encoding="utf-8")

    # Reconstruct confusion matrix for calib_report.json
    pred_cls = np.zeros((n_frames, g.N), dtype=np.int64)
    if mixed_region:
        unrec_mask = np.ones((n_frames, g.N), dtype=bool)
        keep = ~np.isin(truth_labs[0]["tl"], score.EXCLUDED) & ~unrec_mask[0]
        m = np.zeros((score.TL_MAX + 1, 7), dtype=np.int64)
        conf_list = m.tolist()
    else:
        tt_tl = np.stack([l["tl"] for l in truth_labs])
        conf = run_bench.confusion(tt_tl, pred_cls)
        conf_list = conf.tolist()

    calib_report = {
        "benches": [
            {
                "bench": bench,
                "seed": seed,
                "confusion": conf_list,
            }
        ]
    }
    (run_dir / "calib_report.json").write_text(json.dumps(calib_report, indent=2) + "\n", encoding="utf-8")

    return run_dir


class TestConfusionFromRegions(unittest.TestCase):
    def test_exact_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = make_synthetic_unit(tmpdir, n_frames=2, mixed_region=False)
            out_file = Path(tmpdir) / "out" / "result.json"

            rc = confusion_from_regions.main(["--run", str(run_dir), "--out", str(out_file)])
            self.assertEqual(rc, 0)
            self.assertTrue(out_file.is_file())

            report = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertTrue(report["exact_match"])
            self.assertEqual(report["mismatch_cells"], 0)
            self.assertEqual(report["unrecoverable_tiles"], 0)

    def test_mixed_class_region_unrecoverable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = make_synthetic_unit(tmpdir, n_frames=2, mixed_region=True)
            out_file = Path(tmpdir) / "out" / "result.json"

            rc = confusion_from_regions.main(["--run", str(run_dir), "--out", str(out_file)])
            self.assertEqual(rc, 0)
            self.assertTrue(out_file.is_file())

            report = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertEqual(report["unrecoverable_tiles"], 4)  # 2 frames x 2 tiles

    def test_frame_count_mismatch_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = make_synthetic_unit(tmpdir, n_frames=3, frame_mismatch=True)
            out_file = Path(tmpdir) / "out" / "result.json"

            with self.assertRaises(ValueError):
                confusion_from_regions.main(["--run", str(run_dir), "--out", str(out_file)])

    def test_empty_regions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = make_synthetic_unit(tmpdir, n_frames=2, empty_regions=True)
            out_file = Path(tmpdir) / "out" / "result.json"

            rc = confusion_from_regions.main(["--run", str(run_dir), "--out", str(out_file)])
            self.assertEqual(rc, 0)
            self.assertTrue(out_file.is_file())

            report = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertTrue(report["exact_match"])
            self.assertEqual(report["unrecoverable_tiles"], 0)

    def test_run_dir_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = make_synthetic_unit(tmpdir, n_frames=2)
            out_file = Path(tmpdir) / "out" / "result.json"

            before_hashes = hash_dir(run_dir)
            rc = confusion_from_regions.main(["--run", str(run_dir), "--out", str(out_file)])
            self.assertEqual(rc, 0)
            after_hashes = hash_dir(run_dir)

            self.assertEqual(before_hashes, after_hashes)


if __name__ == "__main__":
    unittest.main()
