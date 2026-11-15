"""Offline static false-positive cause attribution tests on synthetic run trees.

All fixtures are synthesized in TemporaryDirectory using only JSON records and
labels.pack_truth_tiles() label blobs. No frames or images are ever written to disk.
"""
import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.dont_write_bytecode = True
from blender_visual_qa.truth import calibrate, labels, score
from tools import static_fp_cause
from visioncore import layout
from visioncore.grid import Grid


def _write_truth_bin(path, grid, tl_by_frame, f0=0):
    tl = np.asarray(tl_by_frame, dtype=np.uint8)
    n_frames, n_tiles = tl.shape
    assert n_tiles == grid.N
    labs = [
        {
            "tl": tl[f],
            "flags": np.zeros(n_tiles, dtype=np.uint8),
            "mv": np.zeros((n_tiles, 2), dtype=np.int8),
            "ob_major": np.zeros(n_tiles, dtype=np.uint16),
            "occ_permille": np.zeros(n_tiles, dtype=np.uint16),
        }
        for f in range(n_frames)
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = labels.pack_truth_tiles(grid, labs, f0=f0)
    path.write_bytes(blob)
    return hashlib.sha256(blob).hexdigest()


def _write_regions_and_frames(
    unit_dir,
    grid,
    cls_by_frame,
    f0=0,
    extra_frame_fields=None,
    override_regions=None,
):
    """Write vision/frames.jsonl and vision/regions.jsonl from per-frame, per-Morton-rank classes."""
    vision_dir = unit_dir / "vision"
    vision_dir.mkdir(parents=True, exist_ok=True)
    cls_arr = np.asarray(cls_by_frame, dtype=np.int8)
    n_frames, n_tiles = cls_arr.shape
    assert n_tiles == grid.N

    frame_lines = []
    region_lines = []
    for f_idx in range(n_frames):
        f_num = f0 + f_idx
        hist = [0] * 7

        if override_regions and f_idx in override_regions:
            f_regs = override_regions[f_idx]
            covered = 0
            for r in f_regs:
                for c_i in range(7):
                    hist[c_i] += int(r["cls_hist"][c_i])
                covered += int(r["n_tiles"])
                region_lines.append(json.dumps(r, sort_keys=True))
            hist[0] = n_tiles - covered
        else:
            f_regs = []
            reg_id = 0
            for r_idx in range(n_tiles):
                c = int(cls_arr[f_idx, r_idx])
                assert 0 <= c <= 6
                hist[c] += 1
                if c >= score.DIRTY_MIN:
                    m_val = int(grid.mortons[r_idx])
                    m_hex = "%04x" % m_val
                    r_hist = [0] * 7
                    r_hist[c] = 1
                    group = "MOV" if c == score.C_MOVED else "FRZ" if c == score.C_FROZEN else "CHG"
                    reg = {
                        "schema": "vqa.core-region/v1",
                        "grid": grid.ident,
                        "frame": f_num,
                        "region_id": reg_id,
                        "cls": layout.CLS[c],
                        "group": group,
                        "n_tiles": 1,
                        "cls_hist": r_hist,
                        "spans": [[m_hex, m_hex]],
                        "r_sum": 80,
                        "r_max": 80,
                        "edge_ppm": 0,
                    }
                    if group == "MOV":
                        reg["mv"] = [2, -1]
                    f_regs.append(reg)
                    region_lines.append(json.dumps(reg, sort_keys=True))
                    reg_id += 1

        dirty_count = sum(hist[score.DIRTY_MIN:])
        f_obj = {
            "schema": "vqa.core-frame/v1",
            "frame": f_num,
            "grid": grid.ident,
            "n": hist,
            "cov_ppm": int((dirty_count * 1000000) // n_tiles),
            "global_mv": [0, 0],
            "regions": len(f_regs),
            "tiles_sha256": "0" * 64,
        }
        if extra_frame_fields and f_idx in extra_frame_fields:
            f_obj.update(extra_frame_fields[f_idx])
        frame_lines.append(json.dumps(f_obj, sort_keys=True))

    (vision_dir / "frames.jsonl").write_text("\n".join(frame_lines) + "\n", encoding="utf-8")
    (vision_dir / "regions.jsonl").write_text(
        ("\n".join(region_lines) + "\n") if region_lines else "",
        encoding="utf-8",
    )


def _make_row(bench, seed, grid, n_frames, t0_q4=None, clean_resid=True):
    if t0_q4 is None:
        t0_q4 = int(calibrate.params.effective(None)["T0_Q4"])
    return {
        "schema": score.SCHEMA,
        "bench": bench,
        "seed": seed,
        "frames": n_frames,
        "tiles": grid.N,
        "grid": grid.ident,
        "t0_q4": t0_q4,
        "clean_resid": clean_resid,
    }


def _snapshot_tree(root):
    snap = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(root))
            snap[rel] = (
                hashlib.sha256(p.read_bytes()).hexdigest(),
                p.stat().st_mtime_ns,
            )
    return snap


class TestStaticFpCause(unittest.TestCase):
    def setUp(self):
        self.grid = Grid(64, 64)  # 4x4 = 16 tiles, s=16
        self.rank_by_xy = {}
        for r_idx in range(self.grid.N):
            self.rank_by_xy[(int(self.grid.tx[r_idx]), int(self.grid.ty[r_idx]))] = r_idx

    def _r(self, tx, ty):
        return self.rank_by_xy[(tx, ty)]

    def test_each_cause_injected_and_exact_counts(self):
        """Inject each of the 6 causes on known tiles across 4 frames and verify exact counts & ppm."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            out_dir = root / "out"
            unit_dir = run_dir / "fit" / "seed_001001" / "B1_still"

            # 4 frames (f=0..3), 16 tiles per frame (64 total tile-frames)
            tl = np.zeros((4, self.grid.N), dtype=np.uint8)
            cls = np.zeros((4, self.grid.N), dtype=np.int8)

            # Frame 0:
            # - Truth dirty tile at (0, 0): RIGID (3), not in FP_POP
            tl[0, self._r(0, 0)] = labels.TL_ID["RIGID"]
            cls[0, self._r(0, 0)] = score.C_MOVED
            # - SHADE: tile (3, 3) has tl=SHADE_WEAK (7) and predicted dirty (C_CHANGED=2)
            tl[0, self._r(3, 3)] = labels.TL_ID["SHADE_WEAK"]
            cls[0, self._r(3, 3)] = score.C_CHANGED
            # - EDGE_BLEED: tile (1, 0) is Chebyshev distance 1 from (0, 0), tl=STATIC, predicted dirty
            cls[0, self._r(1, 0)] = score.C_CHANGED
            # - AA_JITTER: tile (3, 0) is far from (0, 0), tl=STATIC in all frames,
            #   predicted dirty (C_FLICKER=4) in frame 0 and UNCHANGED (0) in frame 1
            cls[0, self._r(3, 0)] = score.C_FLICKER
            # - UNATTRIBUTED: tile (0, 3) is far from (0, 0), tl=STATIC in all frames,
            #   predicted dirty with the exact same class (C_CHANGED=2) across all 4 frames
            for f_i in range(4):
                cls[f_i, self._r(0, 3)] = score.C_CHANGED

            # Frame 1: GLOBAL_EXPOSURE frame
            # Need > 20% of static tiles dirty (> 3.2 tiles -> 4 tiles dirty) + drift signal (C_DRIFT=6)
            # Tile (0, 3) is already C_CHANGED (2). Add 3 more static tiles with C_DRIFT (6):
            cls[1, self._r(1, 2)] = score.C_DRIFT
            cls[1, self._r(2, 2)] = score.C_DRIFT
            cls[1, self._r(3, 2)] = score.C_DRIFT

            # Frame 2: LIGHT_CHANGE frame
            # journal.json has animated light change on frame 2.
            # Dirty static tiles in Frame 2: tile (0, 3) (C_CHANGED) and tile (2, 1) (C_CHANGED).
            # 2 / 16 = 125,000 ppm <= 200,000 ppm (so GLOBAL_EXPOSURE does not fire).
            cls[2, self._r(2, 1)] = score.C_CHANGED

            # Frame 3: only tile (0, 3) is dirty (C_CHANGED=2, same as Frame 2).
            # Attributed to UNATTRIBUTED.

            _write_truth_bin(unit_dir / "labels" / "tiles_truth.bin", self.grid, tl, f0=0)
            _write_regions_and_frames(unit_dir, self.grid, cls, f0=0)
            (unit_dir / "journal.json").write_text(
                json.dumps(
                    {
                        "bench": "B1_still",
                        "seed": 1001,
                        "animated": [{"frame": 2, "kind": "light", "name": "Key"}],
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            row = _make_row("B1_still", 1001, self.grid, 4)
            (run_dir / "rows.jsonl").write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")

            before_snap = _snapshot_tree(run_dir)
            report = static_fp_cause.diagnose(run_dir, out_dir)
            after_snap = _snapshot_tree(run_dir)
            self.assertEqual(before_snap, after_snap, "Input run directory was modified")

            self.assertEqual(report["fp_pop_n"], 63)
            self.assertEqual(report["fp_tiles"], 11)
            self.assertEqual(report["absent_tiles"], 0)

            causes = report["causes"]
            self.assertEqual(causes["SHADE"]["count"], 1)
            self.assertEqual(causes["EDGE_BLEED"]["count"], 1)
            self.assertEqual(causes["GLOBAL_EXPOSURE"]["count"], 4)
            self.assertEqual(causes["LIGHT_CHANGE"]["count"], 2)
            self.assertEqual(causes["AA_JITTER"]["count"], 1)
            self.assertEqual(causes["UNATTRIBUTED"]["count"], 2)
            self.assertEqual(causes["ABSENT"]["count"], 0)

            self.assertEqual(causes["SHADE"]["ppm"], score.ppm(1, 63))
            self.assertEqual(causes["GLOBAL_EXPOSURE"]["ppm"], score.ppm(4, 63))
            self.assertEqual(causes["LIGHT_CHANGE"]["ppm"], score.ppm(2, 63))
            self.assertEqual(causes["UNATTRIBUTED"]["ppm"], score.ppm(2, 63))

            self.assertTrue((out_dir / "static_fp_cause.json").is_file())
            self.assertTrue((out_dir / "SUMMARY.md").is_file())
            md = (out_dir / "SUMMARY.md").read_text(encoding="utf-8")
            self.assertIn("B1_still", md)
            self.assertIn("GLOBAL_EXPOSURE", md)

            self.assertEqual(report["top_frames"][0]["frame"], 0)
            self.assertEqual(report["top_frames"][0]["fp_tiles"], 4)
            self.assertEqual(report["top_frames"][1]["frame"], 1)
            self.assertEqual(report["top_frames"][1]["fp_tiles"], 4)

    def test_strict_rule_precedence(self):
        """When tiles satisfy multiple rules simultaneously, strict precedence picks the earliest rule."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            out_dir = root / "out"
            unit_dir = run_dir / "fit" / "seed_000007" / "B6_glossy"

            tl = np.zeros((2, self.grid.N), dtype=np.uint8)
            cls = np.zeros((2, self.grid.N), dtype=np.int8)

            # Frame 0 satisfies BOTH GLOBAL_EXPOSURE and LIGHT_CHANGE.
            tl[0, self._r(0, 0)] = labels.TL_ID["RIGID"]
            cls[0, self._r(0, 0)] = score.C_MOVED

            # Tile A at (1, 0): SHADE > EDGE_BLEED > GLOBAL_EXPOSURE > LIGHT_CHANGE > AA_JITTER
            tl[0, self._r(1, 0)] = labels.TL_ID["SHADE_WEAK"]
            cls[0, self._r(1, 0)] = score.C_DRIFT

            # Tile B at (0, 1): EDGE_BLEED > GLOBAL_EXPOSURE > LIGHT_CHANGE > AA_JITTER
            cls[0, self._r(0, 1)] = score.C_DRIFT

            # Tiles C1..C3 at (3, 3), (3, 2), (2, 3): GLOBAL_EXPOSURE > LIGHT_CHANGE > AA_JITTER
            cls[0, self._r(3, 3)] = score.C_DRIFT
            cls[0, self._r(3, 2)] = score.C_DRIFT
            cls[0, self._r(2, 3)] = score.C_DRIFT

            # Frame 1: LIGHT_CHANGE > AA_JITTER
            cls[1, self._r(3, 0)] = score.C_CHANGED

            _write_truth_bin(unit_dir / "labels" / "tiles_truth.bin", self.grid, tl, f0=0)
            _write_regions_and_frames(unit_dir, self.grid, cls, f0=0)
            (unit_dir / "journal.json").write_text(
                json.dumps(
                    {
                        "bench": "B6_glossy",
                        "seed": 7,
                        "animated": [
                            {"frame": 0, "kind": "light", "name": "Sun"},
                            {"frame": 1, "kind": "light", "name": "Sun"},
                        ],
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            row = _make_row("B6_glossy", 7, self.grid, 2)
            (run_dir / "rows.jsonl").write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")

            report = static_fp_cause.diagnose(run_dir, out_dir)
            causes = report["causes"]
            self.assertEqual(causes["SHADE"]["count"], 1)
            self.assertEqual(causes["EDGE_BLEED"]["count"], 1)
            self.assertEqual(causes["GLOBAL_EXPOSURE"]["count"], 3)
            self.assertEqual(causes["LIGHT_CHANGE"]["count"], 1)
            self.assertEqual(causes["AA_JITTER"]["count"], 0)
            self.assertEqual(causes["UNATTRIBUTED"]["count"], 0)

    def test_tile_cls_jsonl_and_unknown_tiles_counted_absent(self):
        """When vision/tile_cls.jsonl is present, load from it and count unknown (-1 / ABSENT) tiles as ABSENT."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            out_dir = root / "out"
            unit_dir = run_dir / "fit" / "seed_000001" / "B1_still"

            tl = np.zeros((2, self.grid.N), dtype=np.uint8)
            _write_truth_bin(unit_dir / "labels" / "tiles_truth.bin", self.grid, tl, f0=0)

            vision_dir = unit_dir / "vision"
            vision_dir.mkdir(parents=True, exist_ok=True)
            row0_cls = [0] * self.grid.N
            row0_cls[0] = "ABSENT"
            row0_cls[1] = -1
            row0_cls[2] = score.C_CHANGED
            row1_cls = [0] * self.grid.N

            tile_cls_lines = [
                json.dumps({"frame": 0, "grid": self.grid.ident, "cls": row0_cls}),
                json.dumps({"frame": 1, "grid": self.grid.ident, "cls": row1_cls}),
            ]
            (vision_dir / "tile_cls.jsonl").write_text("\n".join(tile_cls_lines) + "\n", encoding="utf-8")

            row = _make_row("B1_still", 1, self.grid, 2)
            (run_dir / "rows.jsonl").write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")

            report = static_fp_cause.diagnose(run_dir, out_dir)
            self.assertEqual(report["units"][0]["cls_source"], "tile_cls.jsonl")
            self.assertEqual(report["absent_tiles"], 2)
            self.assertEqual(report["fp_absent_tiles"], 2)
            self.assertEqual(report["causes"]["ABSENT"]["count"], 2)
            self.assertEqual(report["causes"]["ABSENT"]["status"], "ABSENT")
            self.assertEqual(report["causes"]["AA_JITTER"]["count"], 1)

    def test_absent_reporting_when_fields_missing(self):
        """Missing persisted fields report status=ABSENT and never guess attributions."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            out_dir = root / "out"
            unit_dir = run_dir / "fit" / "seed_000002" / "B2_anim"

            tl = np.zeros((1, self.grid.N), dtype=np.uint8)
            cls = np.zeros((1, self.grid.N), dtype=np.int8)

            m0 = "%04x" % int(self.grid.mortons[0])
            m5 = "%04x" % int(self.grid.mortons[5])
            m6 = "%04x" % int(self.grid.mortons[6])
            m8 = "%04x" % int(self.grid.mortons[8])
            override = {
                0: [
                    {
                        "schema": "vqa.core-region/v1",
                        "grid": self.grid.ident,
                        "frame": 0,
                        "region_id": 0,
                        "cls": "CHANGED",
                        "group": "CHG",
                        "n_tiles": 6,
                        "cls_hist": [0, 0, 6, 0, 0, 0, 0],
                        "spans": [[m0, m5]],
                        "r_sum": 480,
                        "r_max": 80,
                        "edge_ppm": 0,
                    },
                    {
                        "schema": "vqa.core-region/v1",
                        "grid": self.grid.ident,
                        "frame": 0,
                        "region_id": 1,
                        "cls": "CHANGED",
                        "group": "CHG",
                        "n_tiles": 3,
                        "cls_hist": [0, 0, 2, 0, 1, 0, 0],
                        "spans": [[m6, m8]],
                        "r_sum": 240,
                        "r_max": 80,
                        "edge_ppm": 0,
                    },
                ]
            }

            _write_truth_bin(unit_dir / "labels" / "tiles_truth.bin", self.grid, tl, f0=0)
            _write_regions_and_frames(unit_dir, self.grid, cls, f0=0, override_regions=override)

            row = _make_row("B2_anim", 2, self.grid, 1, clean_resid=False)
            (run_dir / "rows.jsonl").write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")

            report = static_fp_cause.diagnose(run_dir, out_dir)
            causes = report["causes"]

            self.assertEqual(causes["SHADE"]["status"], "ABSENT")
            self.assertIn("ABSENT", causes["SHADE"]["reason"])

            self.assertEqual(causes["GLOBAL_EXPOSURE"]["status"], "ABSENT")
            self.assertEqual(causes["GLOBAL_EXPOSURE"]["count"], 0)
            self.assertIn("ABSENT", causes["GLOBAL_EXPOSURE"]["reason"])

            self.assertEqual(causes["LIGHT_CHANGE"]["status"], "ABSENT")
            self.assertEqual(causes["LIGHT_CHANGE"]["count"], 0)
            self.assertIn("ABSENT", causes["LIGHT_CHANGE"]["reason"])

            self.assertEqual(causes["AA_JITTER"]["status"], "ABSENT")
            self.assertEqual(causes["AA_JITTER"]["count"], 0)
            self.assertIn("ABSENT", causes["AA_JITTER"]["reason"])

            self.assertEqual(causes["ABSENT"]["count"], 3)
            self.assertEqual(causes["ABSENT"]["status"], "ABSENT")
            self.assertEqual(causes["UNATTRIBUTED"]["count"], 6)

    def test_cli_refuses_existing_or_nested_out_and_preserves_input(self):
        """CLI returns rc=2 if --out exists or is inside --run, and never modifies --run."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            unit_dir = run_dir / "fit" / "seed_000001" / "B1_still"
            tl = np.zeros((2, self.grid.N), dtype=np.uint8)
            cls = np.zeros((2, self.grid.N), dtype=np.int8)
            _write_truth_bin(unit_dir / "labels" / "tiles_truth.bin", self.grid, tl, f0=0)
            _write_regions_and_frames(unit_dir, self.grid, cls, f0=0)
            row = _make_row("B1_still", 1, self.grid, 2)
            (run_dir / "rows.jsonl").write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")

            before_snap = _snapshot_tree(run_dir)

            with contextlib.redirect_stdout(io.StringIO()):
                rc_nested = static_fp_cause.main(["--run", str(run_dir), "--out", str(run_dir / "sub_out")])
            self.assertEqual(rc_nested, 2)

            existing_out = root / "existing_out"
            existing_out.mkdir()
            with contextlib.redirect_stdout(io.StringIO()):
                rc_existing = static_fp_cause.main(["--run", str(run_dir), "--out", str(existing_out)])
            self.assertEqual(rc_existing, 2)

            valid_out = root / "valid_out"
            proc = subprocess.run(
                [sys.executable, str(Path(static_fp_cause.__file__).resolve()), "--run", str(run_dir), "--out", str(valid_out)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue((valid_out / "static_fp_cause.json").is_file())
            self.assertTrue((valid_out / "SUMMARY.md").is_file())

            after_snap = _snapshot_tree(run_dir)
            self.assertEqual(before_snap, after_snap)


if __name__ == "__main__":
    unittest.main()
