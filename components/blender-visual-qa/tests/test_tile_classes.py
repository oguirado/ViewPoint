"""Tests for per-tile class RLE persistence (tile_classes, run_bench_tc, noise_rescore_exact).

All fixtures are synthesized in TemporaryDirectory with metadata JSON records,
TILE_DTYPE buffers in temporary directories, and packed truth-tile label blobs;
never rendered frames or image files.
"""
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.dont_write_bytecode = True
from blender_visual_qa.truth import calibrate, labels, run_bench, run_bench_tc, score, tile_classes
from tools import noise_rescore, noise_rescore_exact
from vadapt import ramws
from visioncore import canon, desc, grid, layout, regions
from visioncore.errors import VqaError

FIT_SEED, GATE_SEED = 1, 101
FIT_CAPTURE = hashlib.sha256(b"synthetic:fit:1:B6").hexdigest()
API_VER = "1.1.0"
PARAMS_HEX = "ab" * 32


def _json(path, obj):
    path.write_text(json.dumps(obj, sort_keys=True) + "\n", encoding="utf-8")


def _jsonl(path, rows):
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")


def _build_tiles_bin(g, cls_frames, f0=0, resid_frames=None, mv_frames=None):
    """Build a synthetic tiles.bin bytes buffer with layout.pack_tile_header and desc.TILE_DTYPE."""
    cls_arr = np.asarray(cls_frames, dtype=np.uint8)
    nf, n_tiles = cls_arr.shape
    assert n_tiles == g.N
    hdr = layout.pack_tile_header(g.s, g.W, g.H, g.gw, g.gh, g.N, nf, f0, PARAMS_HEX, API_VER)
    rec = np.zeros((nf, g.N), dtype=desc.TILE_DTYPE)
    for f in range(nf):
        rec[f]["morton"] = g.mortons
        rec[f]["cls"] = cls_arr[f]
        if resid_frames is not None:
            rec[f]["resid"] = resid_frames[f]
        if mv_frames is not None:
            rec[f]["mv_dx"] = mv_frames[f, :, 0]
            rec[f]["mv_dy"] = mv_frames[f, :, 1]
    return hdr + rec.tobytes(), rec


class _MixedRun:
    """Synthetic FIT/GATE run built like tests/test_noise_rescore.py _Run, with a mixed CHG island.

    In B1, the 2x2 dirty island at (3, 6) contains mixed classes (CHANGED=2, FLICKER=4, DRIFT=6)
    within a single 8-connected CHG island so regions_of emits one region with cls_hist having
    multiple active dirty classes. Both tiles_truth.bin and tile_cls.jsonl (written via
    write_tile_classes from a synthetic TILE_DTYPE tiles.bin, then removing tiles.bin as
    persist_unit does) are stored per unit.
    """

    def __init__(self, root, mixed=True, t0=32):
        self.path = Path(root) / "run"
        self.path.mkdir()
        self.g = grid.Grid(128, 128)
        self.rows, self.benches = [], []
        self._unit("fit", FIT_SEED, "B6", 32, mixed=False)
        self._unit("gate", GATE_SEED, "B1", t0, changed=True, mixed=mixed)
        self._unit("gate", GATE_SEED, "B6", t0, mixed=False)
        self.still = {
            "samples_q4": [8] * 128,
            "still_meta": {"seeds": [FIT_SEED], "capture_sha256": [FIT_CAPTURE]},
        }
        gate_rows = [r for r in self.rows if r["seed"] == GATE_SEED]
        summary = score.summarize(gate_rows)
        self.report = {
            "schema": "bvqa.run-report/v1", "grid": self.g.ident,
            "spp": 4, "frames": 2, "renderer": "synthetic-json-only",
            "seeds": {"fit": [FIT_SEED], "gate": [GATE_SEED]},
            "frozen_t0_q4": t0, "benches": self.benches,
            "calibration": {
                "summary": summary,
                "checks": calibrate.check_targets(summary, True, gate_rows, [GATE_SEED]),
            },
        }
        self.save()

    def save(self):
        _jsonl(self.path / "rows.jsonl", self.rows)
        _json(self.path / "still_manifest.json", self.still)
        _json(self.path / "calib_report.json", self.report)

    def gate_unit(self, bench="B1"):
        return self.path / "gate" / ("seed_%06d" % GATE_SEED) / bench

    def _unit(self, arm, seed, bench, t0, changed=False, mixed=False):
        g, nf = self.g, 2
        tl = np.zeros((nf, g.N), dtype=np.uint8)
        flags = np.zeros_like(tl)
        truth_mv = np.zeros(tl.shape + (2,), dtype=np.int8)
        ob = np.zeros(tl.shape, dtype=np.uint16)
        cls = np.zeros((nf, g.N), dtype=np.uint8)
        resid = np.zeros((nf, g.N), dtype=np.uint8)
        pred_mv = np.zeros((nf, g.N, 2), dtype=np.int8)
        if changed:
            islands = (
                (0, 2, 0, 0, 2),
                (7, 2, 6, 0, 2),
                (3, 3, 0, 6, 80),
                (4, 2, 0, 3, 80),
                (5, 2, 6, 3, 80),
                (6, 2, 3, 6, 80),
            )
            for oid, (t_label, c_pred, x, y, r_val) in enumerate(islands, 1):
                idx = np.flatnonzero((g.tx >= x) & (g.tx < x + 2) & (g.ty >= y) & (g.ty < y + 2))
                tl[:, idx] = t_label
                ob[:, idx] = oid
                cls[:, idx] = c_pred
                resid[:, idx] = r_val
                if t_label == 3:
                    truth_mv[:, idx] = (3, -2)
                    pred_mv[:, idx] = (3, -2)
                if mixed and oid == 6:
                    # Make the 4-tile CHG island at (3, 6) a mixed CHG island:
                    # 2 tiles CHANGED (2), 1 tile FLICKER (4), 1 tile DRIFT (6).
                    tl[:, idx[2]] = score.TL_FLICKER
                    cls[:, idx[2]] = score.C_FLICKER
                    tl[:, idx[3]] = score.TL_DRIFT
                    cls[:, idx[3]] = score.C_DRIFT
        raw_tiles, tables = _build_tiles_bin(g, cls, f0=0, resid_frames=resid, mv_frames=pred_mv)
        regseq = [regions.regions_of(tables[f], g.W, g.H, f) for f in range(nf)]
        frame_rows = [{
            "schema": "vqa.core-frame/v1", "frame": f, "grid": g.ident,
            "n": np.bincount(cls[f], minlength=7).tolist(),
            "regions": len(regseq[f]), "global_mv": [0, 0],
        } for f in range(nf)]
        unit = self.path / arm / ("seed_%06d" % seed) / bench
        label_dir = unit / ("labels" if t0 == 32 else "labels_t0_%d" % t0)
        label_dir.mkdir(parents=True)
        vdir = unit / "vision"
        vdir.mkdir()
        labs = [{
            "tl": tl[f], "flags": flags[f], "mv": truth_mv[f],
            "ob_major": ob[f], "occ_permille": np.zeros(g.N, dtype=np.uint16),
        } for f in range(nf)]
        cov = np.asarray([[labels.frame_meta(f, lab)["cov_ppm"]] * g.N for f, lab in enumerate(labs)])
        blob = labels.pack_truth_tiles(g, labs)
        (label_dir / "tiles_truth.bin").write_bytes(blob)
        _jsonl(vdir / "frames.jsonl", frame_rows)
        _jsonl(vdir / "regions.jsonl", [r for regs in regseq for r in regs])
        # Build tile_cls.jsonl via write_tile_classes from synthetic tiles.bin, then remove tiles.bin.
        (vdir / "tiles.bin").write_bytes(raw_tiles)
        tile_classes.write_tile_classes(vdir)
        (vdir / "tiles.bin").unlink()
        row = score.score_frames(
            tl, cls.astype(np.int64), truth_mv, pred_mv.astype(np.int64),
            bench, seed, 4, g.ident, t0, cov=cov, flags=flags,
        )
        row["capture_sha256"] = hashlib.sha256(("synthetic:%s:%d:%s" % (arm, seed, bench)).encode()).hexdigest()
        if bench == "B6":
            row["c1b_flips"] = score.c1b_flips(tl, cls)
        if bench == "B1":
            ious = score.s4_iou(tl, ob, g.mortons, regseq)
            row["s4_iou_ppm"], row["s4_iou_n"] = ious[(len(ious) - 1) // 2], len(ious)
        conf = np.zeros((12, 7), dtype=np.int64)
        keep = ~np.isin(tl, score.EXCLUDED)
        np.add.at(conf, (tl[keep].astype(np.int64), cls[keep].astype(np.int64)), 1)
        self.rows.append(row)
        self.benches.append({
            "bench": bench, "seed": seed, "seed_group": arm,
            "grid": g.ident, "frames": nf, "tiles": g.N,
            "labels_sha256": hashlib.sha256(blob).hexdigest(),
            "confusion": conf.tolist(),
        })


class TileClassesRleTests(unittest.TestCase):
    def test_round_trip_random_class_arrays(self):
        rng = np.random.default_rng(20261019)
        for length in (0, 1, 7, 16, 64, 225, 1024, 4096):
            for _ in range(10):
                arr = rng.integers(0, 7, size=length, dtype=np.uint8)
                rle = tile_classes.encode_rle(arr)
                dec = tile_classes.decode_rle(rle, n_tiles=length)
                self.assertEqual(dec.dtype, np.uint8)
                self.assertTrue(np.array_equal(arr, dec))
        for c in range(7):
            arr = np.full(128, c, dtype=np.uint8)
            rle = tile_classes.encode_rle(arr)
            self.assertEqual(rle, "%d:128" % c)
            self.assertTrue(np.array_equal(tile_classes.decode_rle(rle, 128), arr))

    def test_invalid_rle_and_out_of_range_classes_rejected(self):
        with self.assertRaises(VqaError):
            tile_classes.encode_rle(np.array([0, 7], dtype=np.uint8))
        with self.assertRaises(VqaError):
            tile_classes.decode_rle("0:4,7:1")
        with self.assertRaises(VqaError):
            tile_classes.decode_rle("0:4", n_tiles=5)


class TileClassesWriteTests(unittest.TestCase):
    def test_synthetic_tiles_bin_with_tile_dtype(self):
        with tempfile.TemporaryDirectory() as d:
            vdir = Path(d) / "vision"
            vdir.mkdir()
            g = grid.Grid(64, 48)
            rng = np.random.default_rng(42)
            cls_frames = rng.integers(0, 7, size=(3, g.N), dtype=np.uint8)
            raw_tiles, _ = _build_tiles_bin(g, cls_frames, f0=1)
            (vdir / "tiles.bin").write_bytes(raw_tiles)
            frame_rows = [{
                "schema": "vqa.core-frame/v1", "frame": 1 + f, "grid": g.ident,
                "n": np.bincount(cls_frames[f], minlength=7).tolist(),
                "regions": 0, "global_mv": [0, 0],
            } for f in range(3)]
            (vdir / "frames.jsonl").write_text("".join(canon.dumps(r) for r in frame_rows), encoding="utf-8")
            out_path = tile_classes.write_tile_classes(vdir)
            self.assertEqual(out_path, vdir / "tile_cls.jsonl")
            lines = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(lines), 3)
            for f, row in enumerate(lines):
                self.assertEqual(row["schema"], "bvqa.tile-cls/v1")
                self.assertEqual(row["frame"], 1 + f)
                self.assertEqual(row["grid"], g.ident)
                self.assertEqual(row["n_tiles"], g.N)
                expected_sha = hashlib.sha256(cls_frames[f].tobytes()).hexdigest()
                self.assertEqual(row["sha256"], expected_sha)
                self.assertTrue(np.array_equal(tile_classes.decode_rle(row["rle"], g.N), cls_frames[f]))
            read_back = tile_classes.read_tile_classes(vdir, grid=g, frame_ids=[1, 2, 3])
            self.assertTrue(np.array_equal(read_back, cls_frames))


class PersistWrapperTests(unittest.TestCase):
    def test_persist_wrapper_on_fake_unit_dir(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "ws_unit"
            dst = Path(d) / "out_unit"
            g = grid.Grid(48, 32)
            cls_main = np.array([[0, 2, 4, 6, 0, 3], [0, 0, 2, 4, 6, 0]], dtype=np.uint8)
            cls_re = np.array([[0, 0, 2, 2, 4, 0], [0, 1, 2, 4, 6, 5]], dtype=np.uint8)
            for rel, cls_arr in (("vision", cls_main), ("vision_reanalysis/" + ("c" * 64), cls_re)):
                vdir = src / rel
                vdir.mkdir(parents=True)
                raw_tiles, _ = _build_tiles_bin(g, cls_arr, f0=0)
                (vdir / "tiles.bin").write_bytes(raw_tiles)
                frows = [{
                    "schema": "vqa.core-frame/v1", "frame": f, "grid": g.ident,
                    "n": np.bincount(cls_arr[f], minlength=7).tolist(),
                    "regions": 0, "global_mv": [0, 0],
                } for f in range(2)]
                (vdir / "frames.jsonl").write_text("".join(canon.dumps(r) for r in frows), encoding="utf-8")
            (src / "render").mkdir()
            (src / "render" / "frame_0001.png").write_bytes(b"not-copied")
            self.assertIs(run_bench.persist_unit, run_bench_tc.persist_unit)
            written = run_bench.persist_unit(src, dst)
            self.assertGreater(written, 0)
            for rel, cls_arr in (("vision", cls_main), ("vision_reanalysis/" + ("c" * 64), cls_re)):
                tc_file = dst / rel / "tile_cls.jsonl"
                self.assertTrue(tc_file.is_file())
                self.assertFalse((dst / rel / "tiles.bin").exists())
                recovered = tile_classes.read_tile_classes(tc_file, grid=g, frame_ids=[0, 1])
                self.assertTrue(np.array_equal(recovered, cls_arr))
            self.assertEqual(ramws.check_disk(dst), [])
            buf = io.StringIO()
            with mock.patch("os.path.exists", return_value=True), contextlib.redirect_stdout(buf):
                rc = run_bench_tc.main(["--dry-run", "--out", str(Path(d) / "dry")])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue())["code"], "DRY_RUN")


class NoiseRescoreExactTests(unittest.TestCase):
    def test_mixed_run_reports_absent_in_noise_rescore_and_zero_absent_in_exact(self):
        with tempfile.TemporaryDirectory() as d:
            fixture = _MixedRun(d, mixed=True)
            out_plain = Path(d) / "out_plain"
            out_exact = Path(d) / "out_exact"
            rep_plain = noise_rescore.rescore(fixture.path, out_plain, [0.05])
            rep_exact = noise_rescore_exact.rescore(fixture.path, out_exact, [0.05])

            self.assertEqual(rep_plain["baseline"]["status"], "ABSENT")
            self.assertEqual(rep_plain["baseline"]["mixed_class_tiles"], 8)
            self.assertEqual(rep_plain["baseline"]["class_absent_tiles"], 8)

            self.assertEqual(rep_exact["absent_tiles"], 0)
            self.assertEqual(rep_exact["baseline"]["absent_tiles"], 0)
            self.assertEqual(rep_exact["baseline"]["mixed_class_tiles"], 0)
            self.assertEqual(rep_exact["baseline"]["class_absent_tiles"], 0)
            self.assertEqual(rep_exact["baseline"]["status"], "PASS")
            self.assertEqual(rep_exact["results"][0]["before"]["static_fp"]["value"], 8)
            self.assertEqual(rep_exact["results"][0]["after"]["static_fp"]["value"], 0)
            self.assertFalse(rep_exact["results"][0]["recall_drop_refused"])

            for meta in fixture.report["benches"]:
                row = next(r for r in fixture.rows if (r["bench"], r["seed"]) == (meta["bench"], meta["seed"]))
                dec_plain, truth = noise_rescore._load_unit(fixture.path, row, meta, {FIT_SEED})
                dec_exact, _ = noise_rescore_exact._load_unit(fixture.path, row, meta, {FIT_SEED})
                non_mixed = (dec_plain["cls"] >= 0) & ~np.isin(truth["tl"], score.EXCLUDED)
                conf_plain = np.bincount(
                    truth["tl"][non_mixed].astype(np.int64) * 7 + dec_plain["cls"][non_mixed],
                    minlength=12 * 7,
                ).reshape(12, 7)
                conf_exact_non_mixed = np.bincount(
                    truth["tl"][non_mixed].astype(np.int64) * 7 + dec_exact["cls"][non_mixed],
                    minlength=12 * 7,
                ).reshape(12, 7)
                self.assertTrue(np.array_equal(conf_plain, conf_exact_non_mixed))
                expected_full = noise_rescore._project_confusion(meta["confusion"])
                actual_full = noise_rescore._confusion(truth["tl"], dec_exact["cls"])
                self.assertTrue(np.array_equal(actual_full, expected_full))

    def test_pure_run_produces_identical_results_and_confusions(self):
        with tempfile.TemporaryDirectory() as d:
            fixture = _MixedRun(d, mixed=False)
            rep_plain = noise_rescore.rescore(fixture.path, Path(d) / "plain", [0.05, 0.001])
            rep_exact = noise_rescore_exact.rescore(fixture.path, Path(d) / "exact", [0.05, 0.001])
            self.assertEqual(rep_exact["absent_tiles"], 0)
            self.assertEqual(rep_plain["results"], rep_exact["results"])

    def test_missing_or_mismatched_tile_cls_fails_with_e_absent(self):
        with tempfile.TemporaryDirectory() as d:
            fixture = _MixedRun(d, mixed=True)
            tc_path = fixture.gate_unit("B1") / "vision" / "tile_cls.jsonl"
            tc_path.unlink()
            with self.assertRaises(noise_rescore_exact.NoiseRescoreError) as ctx:
                noise_rescore_exact.rescore(fixture.path, Path(d) / "out_missing", [0.05])
            self.assertEqual(ctx.exception.code, "E_ABSENT")

        with tempfile.TemporaryDirectory() as d:
            fixture = _MixedRun(d, mixed=True)
            tc_path = fixture.gate_unit("B1") / "vision" / "tile_cls.jsonl"
            rows = [json.loads(line) for line in tc_path.read_text().splitlines()]
            rows[0]["grid"] = "grid:64x64:s16"
            _jsonl(tc_path, rows)
            with self.assertRaises(noise_rescore_exact.NoiseRescoreError) as ctx:
                noise_rescore_exact.rescore(fixture.path, Path(d) / "out_grid", [0.05])
            self.assertEqual(ctx.exception.code, "E_ABSENT")

        with tempfile.TemporaryDirectory() as d:
            fixture = _MixedRun(d, mixed=True)
            tc_path = fixture.gate_unit("B1") / "vision" / "tile_cls.jsonl"
            rows = [json.loads(line) for line in tc_path.read_text().splitlines()]
            rows[1]["frame"] = 5
            _jsonl(tc_path, rows)
            with self.assertRaises(noise_rescore_exact.NoiseRescoreError) as ctx:
                noise_rescore_exact.rescore(fixture.path, Path(d) / "out_frame", [0.05])
            self.assertEqual(ctx.exception.code, "E_ABSENT")

        with tempfile.TemporaryDirectory() as d:
            fixture = _MixedRun(d, mixed=True)
            tc_path = fixture.gate_unit("B1") / "vision" / "tile_cls.jsonl"
            rows = [json.loads(line) for line in tc_path.read_text().splitlines()]
            rows[0]["sha256"] = "0" * 64
            _jsonl(tc_path, rows)
            with self.assertRaises(noise_rescore_exact.NoiseRescoreError) as ctx:
                noise_rescore_exact.rescore(fixture.path, Path(d) / "out_sha", [0.05])
            self.assertEqual(ctx.exception.code, "E_ABSENT")

    def test_cli_reports_absent_tiles_zero_and_e_absent_exit_code_5(self):
        with tempfile.TemporaryDirectory() as d:
            fixture = _MixedRun(d, mixed=True)
            out = Path(d) / "cli_ok"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                rc = noise_rescore_exact.main(["--run", str(fixture.path), "--alpha", "0.05", "--out", str(out)])
            self.assertEqual(rc, 5)
            self.assertIn("absent_tiles=0", buf.getvalue())
            tc_path = fixture.gate_unit("B1") / "vision" / "tile_cls.jsonl"
            rows = [json.loads(line) for line in tc_path.read_text().splitlines()]
            rows[0]["sha256"] = "f" * 64
            _jsonl(tc_path, rows)
            err_buf = io.StringIO()
            with contextlib.redirect_stdout(err_buf), contextlib.redirect_stderr(io.StringIO()):
                rc2 = noise_rescore_exact.main(["--run", str(fixture.path), "--alpha", "0.05", "--out", str(Path(d) / "cli_bad")])
            self.assertEqual(rc2, 5)
            self.assertEqual(json.loads(err_buf.getvalue())["code"], "E_ABSENT")


if __name__ == "__main__":
    unittest.main()
