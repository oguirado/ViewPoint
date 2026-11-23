"""Offline noise-rule regressions; all fixtures are synthesized in TemporaryDirectory.

Only JSON records and labels.pack_truth_tiles() label blobs are written. Tile
predictions below are tiny in-memory metadata tables, never rendered frames,
luma planes, pixel buffers, or committed binary fixtures.
"""
import contextlib
import hashlib
import io
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.dont_write_bytecode = True
from blender_visual_qa.truth import calibrate, labels, score
from tools import noise_rescore as noise
from visioncore import grid, regions


FIT_SEED, GATE_SEED = 1, 101
FIT_CAPTURE = hashlib.sha256(b"synthetic:fit:1:B6").hexdigest()


def _json(path, obj):
    path.write_text(json.dumps(obj, sort_keys=True) + "\n", encoding="utf-8")


def _jsonl(path, rows):
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")


def _tree_bytes(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _still(samples, seeds=None, captures=None):
    return {"samples_q4": samples, "still_meta": {
        "seeds": [FIT_SEED] if seeds is None else seeds,
        "capture_sha256": [FIT_CAPTURE] if captures is None else captures}}


def _fit_rows():
    return [{"bench": "B6", "seed": FIT_SEED, "capture_sha256": FIT_CAPTURE}]


def _rule_region(n=100, mean=6, peak=None, cls="CHANGED", edge=0):
    return {"cls": cls, "n_tiles": n, "r_sum": mean * n,
            "r_max": mean if peak is None else peak, "edge_ppm": edge}


class _PoisonSamples(list):
    """Any sample access proves that a rejected partition was opened too early."""

    def __iter__(self):
        raise AssertionError("GATE residual samples must not be read for fitting")

    def __getitem__(self, key):
        raise AssertionError("GATE residual samples must not be indexed for fitting")

    def __len__(self):
        raise AssertionError("GATE residual samples must not be counted for fitting")


class _Run:
    """A persisted FIT/GATE run with real wheel-generated pure-class regions.

    B1 has two four-tile noise islands (STATIC and SHADE_WEAK), four real
    change islands, and clean gaps between them. Its RIGID island has exact
    (3, -2) motion. Two frames make S4 and C1b evidence independently scorable.
    This deliberately small run lacks the temporal/C5 acceptance populations;
    the CLI must retain that refusal even when all geometric recalls survive.
    """

    def __init__(self, root, rigid_resid=80, jitter=False, t0=32):
        self.path = Path(root) / "run"
        self.path.mkdir()
        self.g = grid.Grid(128, 128)
        self.rows, self.benches = [], []
        self._unit("fit", FIT_SEED, "B6", 32)
        self._unit("gate", GATE_SEED, "B1", t0, changed=True,
                   rigid_resid=rigid_resid, jitter=jitter)
        self._unit("gate", GATE_SEED, "B6", t0)
        self.still = _still([8] * 128)
        gate_rows = [r for r in self.rows if r["seed"] == GATE_SEED]
        summary = score.summarize(gate_rows)
        self.report = {"schema": "bvqa.run-report/v1", "grid": self.g.ident,
                       "spp": 4, "frames": 2, "renderer": "synthetic-json-only",
                       "seeds": {"fit": [FIT_SEED], "gate": [GATE_SEED]},
                       "frozen_t0_q4": t0, "benches": self.benches,
                       "calibration": {"summary": summary,
                                       "checks": calibrate.check_targets(summary, True, gate_rows, [GATE_SEED])}}
        self.save()

    def save(self):
        _jsonl(self.path / "rows.jsonl", self.rows)
        _json(self.path / "still_manifest.json", self.still)
        _json(self.path / "calib_report.json", self.report)

    def gate_unit(self, bench="B1"):
        return self.path / "gate" / ("seed_%06d" % GATE_SEED) / bench

    def _unit(self, arm, seed, bench, t0, changed=False, rigid_resid=80, jitter=False):
        g, nf = self.g, 2
        tl = np.zeros((nf, g.N), dtype=np.uint8)
        flags = np.zeros_like(tl)
        truth_mv = np.zeros(tl.shape + (2,), dtype=np.int8)
        ob = np.zeros(tl.shape, dtype=np.uint16)
        # Metadata-only fields consumed by regions_of; no frame/pixel fields.
        dtype = [("morton", "<u2"), ("cls", "u1"), ("flags", "u1"),
                 ("resid", "u1"), ("edge", "u1"), ("mv_dx", "i1"), ("mv_dy", "i1")]
        tables = [np.zeros(g.N, dtype=dtype) for _ in range(nf)]
        for tab in tables:
            tab["morton"] = g.mortons
        if changed:
            # tl, engine cls, top-left tile x/y, residual. Islands have gaps,
            # so the engine never combines CHANGED with another dirty class.
            islands = ((0, 2, 0, 0, 2), (7, 2, 6, 0, 2),
                       (3, 3, 0, 6, rigid_resid), (4, 2, 0, 3, 80),
                       (5, 2, 6, 3, 80), (6, 2, 3, 6, 80))
            for oid, (truth, cls, x, y, residual) in enumerate(islands, 1):
                idx = np.flatnonzero((g.tx >= x) & (g.tx < x + 2) & (g.ty >= y) & (g.ty < y + 2))
                tl[:, idx] = truth
                ob[:, idx] = oid
                for tab in tables:
                    tab["cls"][idx] = cls
                    tab["resid"][idx] = residual
                if truth == 3:
                    truth_mv[:, idx] = (3, -2)
                    for tab in tables:
                        tab["mv_dx"][idx], tab["mv_dy"][idx] = 3, -2
            if jitter:
                clean = np.flatnonzero(tables[0]["cls"] == 0)[:3]
                for tab in tables:
                    tab["cls"][clean] = score.C_JITTER
        cls = np.stack([tab["cls"] for tab in tables])
        pred_mv = np.stack([np.stack((tab["mv_dx"], tab["mv_dy"]), axis=-1) for tab in tables])
        labs = [{"tl": tl[f], "flags": flags[f], "mv": truth_mv[f],
                 "ob_major": ob[f], "occ_permille": np.zeros(g.N, dtype=np.uint16)} for f in range(nf)]
        cov = np.asarray([[labels.frame_meta(f, lab)["cov_ppm"]] * g.N for f, lab in enumerate(labs)])
        regseq = [regions.regions_of(tab, g.W, g.H, f) for f, tab in enumerate(tables)]
        frame_rows = [{"schema": "vqa.core-frame/v1", "frame": f, "grid": g.ident,
                       "n": np.bincount(cls[f], minlength=7).tolist(),
                       "regions": len(regseq[f]), "global_mv": [0, 0]} for f in range(nf)]
        unit = self.path / arm / ("seed_%06d" % seed) / bench
        label_dir = unit / ("labels" if t0 == 32 else "labels_t0_%d" % t0)
        label_dir.mkdir(parents=True)
        (unit / "vision").mkdir()
        blob = labels.pack_truth_tiles(g, labs)
        (label_dir / "tiles_truth.bin").write_bytes(blob)
        _jsonl(unit / "vision" / "frames.jsonl", frame_rows)
        _jsonl(unit / "vision" / "regions.jsonl", [r for regs in regseq for r in regs])
        row = score.score_frames(tl, cls, truth_mv, pred_mv, bench, seed, 4, g.ident, t0, cov=cov, flags=flags)
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
        self.benches.append({"bench": bench, "seed": seed, "seed_group": arm,
                             "grid": g.ident, "frames": nf, "tiles": g.N,
                             "labels_sha256": hashlib.sha256(blob).hexdigest(),
                             "confusion": conf.tolist()})


class NoiseFitTests(unittest.TestCase):
    def test_supplied_sample_digest_is_verified(self):
        still = _still([8, 12, 16])
        still["sha256"] = score.canon.sha256_bytes(score.canon.dumps({"s": still["samples_q4"]}).encode())
        model = noise.fit_noise(still, [FIT_SEED], _fit_rows())
        self.assertEqual(model["sample_digest_status"], "PASS")
        self.assertEqual(model["mu"], 3)
        still["sha256"] = "0" * 64
        with self.assertRaises(noise.NoiseRescoreError):
            noise.fit_noise(still, [FIT_SEED], _fit_rows())

    def test_p999_rank_and_persisted_residual_units(self):
        # For M=2 rank is zero, not max; floor the display value before averaging.
        model = noise.fit_noise(_still([40, 8]), [FIT_SEED], _fit_rows())
        self.assertEqual(model["R_q4"], 8)
        self.assertEqual(model["R"], 2)
        self.assertEqual(model["mu"], 6)
        self.assertEqual(model["sample_count"], 2)
        self.assertEqual(model["tail_samples"], 1)
        model = noise.fit_noise(_still([4] * 999 + [2048]), [FIT_SEED], _fit_rows())
        self.assertEqual(model["R_q4"], 4)
        self.assertEqual(model["R"], 1)
        self.assertAlmostEqual(model["mu"], (999 + 255) / 1000)
        self.assertEqual(model["tail_samples"], 1)
        self.assertEqual(calibrate.t0_still([4] * 999 + [2048]), 32)

    def test_conversion_truncates_each_sample_and_saturates(self):
        model = noise.fit_noise(_still([8, 11, 12, 1020, 2048]), [FIT_SEED], _fit_rows())
        self.assertEqual(model["R_q4"], 1020)
        self.assertEqual(model["R"], 255)
        self.assertAlmostEqual(model["mu"], (2 + 2 + 3 + 255 + 255) / 5)

    def test_gate_metadata_rejected_before_accessing_any_samples(self):
        for seeds in ([GATE_SEED], [FIT_SEED, GATE_SEED]):
            with self.subTest(seeds=seeds), self.assertRaises(noise.NoiseRescoreError):
                noise.fit_noise(_still(_PoisonSamples([999999]), seeds=seeds), [FIT_SEED], _fit_rows())

    def test_foreign_capture_rejected_before_accessing_samples(self):
        with self.assertRaises(noise.NoiseRescoreError):
            noise.fit_noise(_still(_PoisonSamples([999999]), captures=["gate-capture"]),
                            [FIT_SEED], _fit_rows())

    def test_gate_row_rejected_before_accessing_samples(self):
        rows = _fit_rows() + [{"bench": "B6", "seed": GATE_SEED, "capture_sha256": "gate-capture"}]
        with self.assertRaises(noise.NoiseRescoreError):
            noise.fit_noise(_still(_PoisonSamples([999999])), [FIT_SEED], rows)

    def test_unrelated_gate_samples_cannot_change_fit(self):
        still = _still([8, 12, 16])
        expected = noise.fit_noise(still, [FIT_SEED], _fit_rows())
        still["gate_samples_q4"] = _PoisonSamples([999999])
        self.assertEqual(noise.fit_noise(still, [FIT_SEED], _fit_rows()), expected)


class NoiseRuleTests(unittest.TestCase):
    def test_alpha_monotonicity_has_a_real_transition(self):
        model = {"mu": 1.0, "R": 10.0}
        reg = _rule_region()
        alphas = [0.5, 0.05, 0.001, 0.0001]
        decisions = [noise.should_demote(reg, model, a) for a in alphas]
        self.assertFalse(decisions[0])
        self.assertTrue(decisions[-1])
        self.assertEqual(decisions, sorted(decisions))

    def test_hoeffding_uses_region_mean_and_tile_count(self):
        model = {"mu": 1.0, "R": 10.0}
        self.assertTrue(noise.should_demote(_rule_region(n=100, mean=4), model, 0.05))
        self.assertFalse(noise.should_demote(_rule_region(n=400, mean=4), model, 0.05))
        eps = 2 * math.sqrt(math.log(2 / 0.05) / 100)
        self.assertLess(4 / 10, 1 / 10 + eps)

    def test_hard_and_edge_ceilings_are_inclusive(self):
        model = {"mu": 2.0, "R": 10.0}
        reg = _rule_region(mean=2, peak=10, edge=50000)
        self.assertTrue(noise.should_demote(reg, model, 0.05))
        self.assertFalse(noise.should_demote(dict(reg, r_max=11), model, 0.05))
        self.assertFalse(noise.should_demote(dict(reg, edge_ppm=50001), model, 0.05))
        self.assertTrue(noise.should_demote(dict(reg, r_max=11), model, 0.05, hard_ceiling=11))
        self.assertTrue(noise.should_demote(dict(reg, edge_ppm=50001), model, 0.05, edge_ceiling_ppm=50001))

    def test_clean_classes_never_demote_and_integer_dirty_class_works(self):
        model = {"mu": 2.0, "R": 2.0}
        for cls in (0, 1, "UNCHANGED", "JITTER"):
            with self.subTest(cls=cls):
                self.assertFalse(noise.should_demote(_rule_region(mean=2, cls=cls), model, 0.05))
        self.assertTrue(noise.should_demote(_rule_region(mean=2, cls=2), model, 0.05))

    def test_invalid_alpha_is_not_a_numeric_verdict(self):
        for alpha in (0, -0.1, 1, 2, float("nan"), float("inf")):
            with self.subTest(alpha=alpha), self.assertRaises(noise.NoiseRescoreError):
                noise.should_demote(_rule_region(), {"mu": 1.0, "R": 10.0}, alpha)


class NoiseDecodeTests(unittest.TestCase):
    @staticmethod
    def _rectangular_evidence():
        g = grid.Grid(48, 32)
        # Six existing cells have Morton codes 0,1,2,3,4,6: code 6 has rank 5.
        tab = np.zeros(g.N, dtype=[("morton", "<u2"), ("cls", "u1"), ("flags", "u1"),
                                  ("resid", "u1"), ("edge", "u1"), ("mv_dx", "i1"), ("mv_dy", "i1")])
        tab["morton"] = g.mortons
        tab["cls"][-1], tab["resid"][-1] = score.C_MOVED, 80
        tab["mv_dx"][-1], tab["mv_dy"][-1] = 3, -2
        regs = regions.regions_of(tab, g.W, g.H, 0)
        frames = [{"schema": "vqa.core-frame/v1", "frame": 0, "grid": g.ident,
                   "n": [5, 0, 0, 1, 0, 0, 0], "regions": 1, "global_mv": [0, 0]}]
        return g, frames, regs

    def test_rectangular_morton_gap_maps_to_compact_array_rank(self):
        g, frames, regs = self._rectangular_evidence()
        self.assertEqual(g.mortons.tolist(), [0, 1, 2, 3, 4, 6])
        decoded = noise.decode_regions(frames, regs, g)
        self.assertEqual(decoded["cls"].tolist(), [[0, 0, 0, 0, 0, score.C_MOVED]])
        self.assertEqual(decoded["mv"][0, 5].tolist(), [3, -2])
        self.assertEqual(decoded["entries"][0][0][1].tolist(), [5])

    def test_span_with_valid_endpoints_and_missing_interior_morton_is_rejected(self):
        g, frames, regs = self._rectangular_evidence()
        regs[0].update(spans=[["0004", "0006"]], n_tiles=3,
                       cls_hist=[0, 0, 0, 3, 0, 0, 0], r_sum=240)
        frames[0]["n"] = [3, 0, 0, 3, 0, 0, 0]
        with self.assertRaises(noise.NoiseRescoreError):
            noise.decode_regions(frames, regs, g)


class NoiseRecallTests(unittest.TestCase):
    def test_exact_count_losses_are_refused_when_ppm_does_not_change(self):
        # Hand-built counts exercise ppm rounding without allocating huge arrays.
        n, hit = 2000000, 1999999
        old = {"seed": GATE_SEED,
               "tl": {tl: {"n": n, "dirty": hit, "cls_ok": hit} for tl in ("3", "9", "10", "11")},
               "mv_exact": hit, "drift_duty_n": n, "drift_duty_hit": hit,
               "frozen_hit": {"ge300k": {"n": n, "hit": hit}}}
        new = json.loads(json.dumps(old))
        for counts in new["tl"].values():
            counts["dirty"] -= 1
            counts["cls_ok"] -= 1
        new["mv_exact"] -= 1
        new["drift_duty_hit"] -= 1
        new["frozen_hit"]["ge300k"]["hit"] -= 1
        ppm = score.ppm(hit, n)
        self.assertEqual(ppm, score.ppm(hit - 1, n))
        checks = {name: {"op": ">=", "value": ppm}
                  for name in ("rigid_dirty_recall", "moved_class", "mv_exact", "drift_duty_ppm")}
        drops = noise._recall_drops([old], [new], checks, checks, {GATE_SEED})
        self.assertEqual(drops, sorted(["rigid_dirty_recall", "moved_class", "mv_exact", "drift_duty_ppm",
                                       "flicker_t_recall", "frozen_t_recall", "drift_t_recall",
                                       "frozen_ge300k_recall"]))

    def test_raw_rigid_recall_loss_cannot_hide_behind_unobservable_flag(self):
        tl = np.full((1, 2), 3, dtype=np.uint8)
        flags = np.array([[0, labels.FLAG_UNOBSERVABLE]], dtype=np.uint8)
        mv = np.array([[[3, -2], [3, -2]]], dtype=np.int8)
        predicted = np.full(tl.shape, score.C_MOVED, dtype=np.uint8)
        old = score.score_frames(tl, predicted, mv, mv, "B1", GATE_SEED, 4, "grid:32x16:s16", 32, flags=flags)
        predicted[0, 1] = score.C_UNCHANGED
        new = score.score_frames(tl, predicted, mv, mv, "B1", GATE_SEED, 4, "grid:32x16:s16", 32, flags=flags)
        before, after = noise._checks([old], {GATE_SEED}), noise._checks([new], {GATE_SEED})
        for name in ("rigid_dirty_recall", "moved_class", "mv_exact"):
            self.assertEqual(before[name]["value"], 1000000)
            self.assertEqual(after[name]["value"], 1000000)
        for name in ("moved_recall_raw_ppm", "mv_exact_raw_ppm"):
            self.assertEqual(score.summarize([old])[name], 1000000)
            self.assertEqual(score.summarize([new])[name], 500000)
        drops = noise._recall_drops([old], [new], before, after, {GATE_SEED})
        self.assertIn("moved_recall_raw_ppm", drops)
        self.assertIn("mv_exact_raw_ppm", drops)


class NoiseRescoreTests(unittest.TestCase):
    def test_bad_sample_digest_refuses_before_creating_output(self):
        with tempfile.TemporaryDirectory() as d:
            fixture = _Run(d)
            fixture.still["sha256"] = "0" * 64
            fixture.save()
            before, out = _tree_bytes(fixture.path), Path(d) / "out"
            with self.assertRaises(noise.NoiseRescoreError):
                noise.rescore(fixture.path, out, [0.05])
            self.assertFalse(out.exists())
            self.assertEqual(_tree_bytes(fixture.path), before)

    def test_manifest_binding_verifies_exact_bytes_not_reserialized_json(self):
        with tempfile.TemporaryDirectory() as d:
            fixture = _Run(d)
            manifest = fixture.path / "still_manifest.json"
            fixture.report["calibration"]["source_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
            fixture.save()
            report = noise.rescore(fixture.path, Path(d) / "valid", [0.05])
            self.assertEqual(report["model"]["manifest_digest_status"], "PASS")
            # Parsed data is identical; the original byte binding is now stale.
            manifest.write_bytes(manifest.read_bytes() + b"\n")
            self.assertEqual(json.loads(manifest.read_text()), fixture.still)
            out = Path(d) / "stale"
            with self.assertRaises(noise.NoiseRescoreError):
                noise.rescore(fixture.path, out, [0.05])
            self.assertFalse(out.exists())

    def test_missing_optional_digests_are_supported_but_reported_absent(self):
        with tempfile.TemporaryDirectory() as d:
            fixture = _Run(d)
            report = noise.rescore(fixture.path, Path(d) / "out", [0.05])
            self.assertEqual(report["model"]["sample_digest_status"], "ABSENT")
            self.assertEqual(report["model"]["manifest_digest_status"], "ABSENT")
            self.assertEqual(report["results"][0]["after"]["static_fp"]["value"], 0)

    def test_noise_demoted_real_rigid_kept_and_run_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            fixture = _Run(d)
            before = _tree_bytes(fixture.path)
            out = Path(d) / "rescore"
            report = noise.rescore(fixture.path, out, [0.05, 0.001])
            self.assertEqual(_tree_bytes(fixture.path), before)
            self.assertEqual(report["baseline"]["class_absent_tiles"], 0)
            self.assertEqual(report["baseline"]["mixed_class_tiles"], 0)
            self.assertEqual(len(report["results"]), 2)
            for result in report["results"]:
                self.assertEqual(result["demoted_regions"], 4)
                self.assertEqual(result["demoted_tiles"], 16)
                self.assertEqual(result["before"]["static_fp"]["value"], 8)
                self.assertEqual(result["after"]["static_fp"]["value"], 0)
                self.assertEqual(result["before"]["shade_weak_fp_ppm"]["value"], 1000000)
                self.assertEqual(result["after"]["shade_weak_fp_ppm"]["value"], 0)
                for name in ("rigid_dirty_recall", "moved_class", "occlude_recall",
                             "deform_recall", "shade_strong_recall", "mv_exact"):
                    self.assertEqual(result["before"][name]["value"], 1000000)
                    self.assertEqual(result["after"][name]["value"], 1000000)
                self.assertFalse(result["recall_drop_refused"])
                self.assertEqual(result["recall_drops"], [])
                # Missing temporal/selector populations remain a refusal.
                self.assertTrue(result["refused"])
            self.assertEqual(json.loads((out / "noise_rescore_report.json").read_text()), report)
            self.assertTrue((out / "NOISE_RESCORE.md").is_file())
            self.assertTrue(all(p.suffix in (".json", ".jsonl", ".md") for p in out.iterdir()))

    def test_recall_loss_is_reported_and_refused(self):
        with tempfile.TemporaryDirectory() as d:
            fixture = _Run(d, rigid_resid=2)
            result = noise.rescore(fixture.path, Path(d) / "out", [0.05])["results"][0]
            self.assertEqual(result["before"]["rigid_dirty_recall"]["value"], 1000000)
            self.assertEqual(result["after"]["rigid_dirty_recall"]["value"], 0)
            self.assertEqual(result["after"]["moved_class"]["value"], 0)
            self.assertTrue(result["recall_drop_refused"])
            self.assertTrue(result["recall_drops"])
            self.assertTrue(result["refused"])

    def test_rescore_supplies_only_fit_rows_to_fitter(self):
        with tempfile.TemporaryDirectory() as d:
            fixture = _Run(d)
            original = noise.fit_noise

            def guarded(still, fit_seeds, fit_rows):
                self.assertEqual(set(fit_seeds), {FIT_SEED})
                self.assertEqual({r["seed"] for r in fit_rows}, {FIT_SEED})
                return original(still, fit_seeds, fit_rows)

            with mock.patch.object(noise, "fit_noise", side_effect=guarded) as fitting:
                noise.rescore(fixture.path, Path(d) / "out", [0.05])
            fitting.assert_called_once()

    def test_mixed_region_has_absent_classes_and_no_guessed_after(self):
        with tempfile.TemporaryDirectory() as d:
            fixture = _Run(d)
            unit = fixture.gate_unit()
            rp, fp = unit / "vision" / "regions.jsonl", unit / "vision" / "frames.jsonl"
            regs = [json.loads(line) for line in rp.read_text().splitlines()]
            frames = [json.loads(line) for line in fp.read_text().splitlines()]
            reg = next(r for r in regs if r["cls"] == "CHANGED" and r["frame"] == 0)
            self.assertEqual(reg["n_tiles"], 4)
            reg["cls_hist"][2] -= 1
            reg["cls_hist"][4] += 1
            frames[0]["n"][2] -= 1
            frames[0]["n"][4] += 1
            _jsonl(rp, regs)
            _jsonl(fp, frames)
            report = noise.rescore(fixture.path, Path(d) / "out", [0.05])
            self.assertEqual(report["baseline"]["status"], "ABSENT")
            self.assertEqual(report["baseline"]["mixed_class_tiles"], 4)
            self.assertGreaterEqual(report["baseline"]["class_absent_tiles"], 4)
            result = report["results"][0]
            self.assertTrue(result["refused"])
            self.assertTrue(all(c["value"] is None for c in result["after"].values()))
            self.assertIn("ABSENT", (Path(d) / "out" / "NOISE_RESCORE.md").read_text())

    def test_jitter_count_is_declared_while_clean_scoring_reproduces(self):
        with tempfile.TemporaryDirectory() as d:
            fixture = _Run(d, jitter=True)
            report = noise.rescore(fixture.path, Path(d) / "out", [0.05])
            self.assertEqual(report["baseline"]["jitter_collapsed_tiles"], 6)
            self.assertEqual(report["baseline"]["mixed_class_tiles"], 0)
            self.assertEqual(report["results"][0]["before"]["static_fp"]["value"], 8)
            self.assertEqual(report["results"][0]["after"]["static_fp"]["value"], 0)

    def test_confusion_mismatch_refuses_before_creating_output(self):
        with tempfile.TemporaryDirectory() as d:
            fixture = _Run(d)
            bench = next(b for b in fixture.report["benches"] if b["bench"] == "B1")
            bench["confusion"][0][2] += 1
            fixture.save()
            out = Path(d) / "out"
            with self.assertRaises(noise.NoiseRescoreError):
                noise.rescore(fixture.path, out, [0.05])
            self.assertFalse(out.exists())

    def test_summary_mismatch_is_not_hidden_by_matching_confusion(self):
        with tempfile.TemporaryDirectory() as d:
            fixture = _Run(d)
            fixture.report["calibration"]["summary"]["tl"]["0"]["fp"] += 1
            fixture.save()
            with self.assertRaises(noise.NoiseRescoreError):
                noise.rescore(fixture.path, Path(d) / "out", [0.05])

    def test_nondefault_t0_uses_matching_label_variant(self):
        with tempfile.TemporaryDirectory() as d:
            fixture = _Run(d, t0=64)
            self.assertFalse((fixture.gate_unit() / "labels" / "tiles_truth.bin").exists())
            report = noise.rescore(fixture.path, Path(d) / "out", [0.05])
            self.assertEqual(report["results"][0]["after"]["static_fp"]["value"], 0)

    def test_existing_nested_and_parent_outputs_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            fixture = _Run(d)
            existing = Path(d) / "existing"
            existing.mkdir()
            before = _tree_bytes(fixture.path)
            for out in (existing, fixture.path, fixture.path / "nested", Path(d)):
                with self.subTest(out=str(out)), self.assertRaises(noise.NoiseRescoreError):
                    noise.rescore(fixture.path, out, [0.05])
            self.assertEqual(_tree_bytes(fixture.path), before)
            self.assertFalse((fixture.path / "nested").exists())
            self.assertEqual(list(existing.iterdir()), [])

    def test_cli_refusal_writes_report_and_invalid_alpha_does_not(self):
        with tempfile.TemporaryDirectory() as d:
            fixture = _Run(d)
            before = _tree_bytes(fixture.path)
            out = Path(d) / "cli"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = noise.main(["--run", str(fixture.path), "--alpha", "0.05", "0.001", "--out", str(out)])
            self.assertEqual(rc, 5)
            self.assertEqual(len(json.loads((out / "noise_rescore_report.json").read_text())["results"]), 2)
            bad = Path(d) / "invalid"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = noise.main(["--run", str(fixture.path), "--alpha", "0", "--out", str(bad)])
            self.assertEqual(rc, 2)
            self.assertFalse(bad.exists())
            self.assertEqual(_tree_bytes(fixture.path), before)


if __name__ == "__main__":
    unittest.main()
