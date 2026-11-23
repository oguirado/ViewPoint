"""W10F lane A: truth amendment FLAG_UNOBSERVABLE (reports/W10D_MOVED_DIAG.md "Truth-label amendment").

Bit 8 of the bvqa.truth-tile/v1 flags byte marks RIGID tiles of diag_moved cause a/b; it is set from beauty + truth
only and score drops those tiles from the MOVED denominators while reporting the unmasked counts alongside."""
import hashlib
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from blender_visual_qa.truth import diag_moved as dm
from blender_visual_qa.truth import labels, run_bench as rb, score
from visioncore import canon, pipeline
from visioncore.errors import VqaError
from visioncore.grid import Grid

ROOT = Path(__file__).resolve().parents[1]
W = H = 128
BG = 40
PRE_W10F = "1f2fad3"  # last commit before the amendment
# sha256 of the canon rows of _golden_rows() under score.py at PRE_W10F (computed from `git show 1f2fad3:...`)
GOLDEN_ROWS_SHA = "ace9f6ea42b740d7e9439b4c14e34ebadeaa27d6cbcbdc8ffb43e32aaa7b8951"


def texture(kind, w, h):
    if kind == "flat":
        return np.full((h, w), 200, np.uint8)
    return np.random.default_rng(7).integers(0, 256, (h, w), dtype=np.uint8)


def sequence(kind, dx, n=4, x0=8, y0=24, sw=80, sh=80):
    """Square (attached texture) moving dx px/frame over a flat background, with Blender-style truth."""
    tex = texture(kind, sw, sh)
    rgbs, truths, boxes = [], [], []
    for f in range(n):
        x = x0 + dx * f
        lum = np.full((H, W), BG, np.uint8)
        ob = np.zeros((H, W), np.uint16)
        mv = np.zeros((H, W, 2), np.int16)
        z = np.full((H, W), labels.BG_Z, np.uint32)
        xa, xb = max(0, x), min(W, x + sw)
        lum[y0:y0 + sh, xa:xb] = tex[:, xa - x:xb - x]
        ob[y0:y0 + sh, xa:xb] = 1
        mv[y0:y0 + sh, xa:xb, 0] = 16 * dx
        z[y0:y0 + sh, xa:xb] = 1000
        rgbs.append(np.repeat(lum[..., None], 3, axis=2))
        truths.append({"ob": ob, "mv": mv, "z": z})
        boxes.append((x, y0, x + sw, y0 + sh))
    return rgbs, truths, boxes


def interior_tiles(g, boxes, dx, s=16, margin=2):
    """(frame, Morton tile) whose block (+margin) is inside the square at t and whose source block is inside at t-1."""
    out = set()
    for f in range(1, len(boxes)):
        x0, y0, x1, y1 = boxes[f]
        px0, _, px1, _ = boxes[f - 1]
        for i in range(g.N):
            bx0, by0 = int(g.tx[i]) * s - margin, int(g.ty[i]) * s - margin
            bx1, by1 = bx0 + s + 2 * margin, by0 + s + 2 * margin
            if x0 <= bx0 and bx1 <= x1 and y0 <= by0 and by1 <= y1 and px0 <= bx0 - dx and bx1 - dx <= px1:
                out.add((f, i))
    return out


def setup(kind, dx=3):
    rgbs, truths, boxes = sequence(kind, dx)
    g = Grid(W, H)
    labs = labels.label_sequence(truths, g)
    return rgbs, truths, boxes, g, labs


def blob_of(g, labs):
    return labels.pack_truth_tiles(g, labs)


def _golden_rows(score_frames):
    rng = np.random.default_rng(10)
    rows = []
    for k in range(3):
        F, N = 5, 64
        tl = rng.integers(0, 12, (F, N)).astype(np.uint8)
        cls = rng.integers(0, 7, (F, N)).astype(np.int64)
        mvt = rng.integers(-3, 4, (F, N, 2)).astype(np.int8)
        mvp = np.where(rng.integers(0, 2, (F, N, 2)) == 1, mvt, rng.integers(-3, 4, (F, N, 2))).astype(np.int64)
        resid = rng.integers(0, 400, (F, N)).astype(np.int64)
        cov = rng.integers(0, 1000001, (F, N)).astype(np.int64)
        rows.append(score_frames(tl, cls, mvt, mvp, "B%d" % k, k, 4, "s16-128x128", 64, resid=resid, cov=cov))
    return b"".join(canon.dumps(r).encode() for r in rows)


def mask_engine_free(fn, rgbs, truths, labs, g):
    """True iff fn(rgbs, truths, labs, g) never runs the engine (pipeline.analyze / diag_moved.engine_run raise) and its
    output is unchanged when the diag tiles' engine fields claim a perfect or a blind engine."""
    def boom(*a, **k):
        raise AssertionError("engine called by the truth mask")
    try:
        with mock.patch.object(pipeline, "analyze", boom), mock.patch.object(dm, "engine_run", boom):
            base = fn(rgbs, truths, labs, g)
            real_diag = dm.diagnose_sequence
            for verdict in ("blind", "perfect"):
                def diag(r, t, cls, mv, gg, tag=None, label_list=None, _v=verdict):
                    c = np.zeros_like(cls) if _v == "blind" else np.full_like(cls, 3)
                    m = np.stack([np.asarray(lb["mv"]) for lb in label_list]).astype(np.int64) if _v == "perfect" \
                        else np.zeros_like(mv)
                    return real_diag(r, t, c, m, gg, tag, label_list)
                with mock.patch.object(dm, "diagnose_sequence", diag):
                    if not np.array_equal(fn(rgbs, truths, labs, g), base):
                        return False
    except AssertionError:
        return False
    return True


def mutant_engine_verdict(rgbs, truths, labs, g):
    """Mutant 1: unobservable = RIGID tile the engine does not call MOVED (mask from the engine verdict)."""
    _, cls, _ = dm.engine_run(rgbs)
    out = np.zeros((len(rgbs), g.N), bool)
    for f in range(1, len(rgbs)):
        tl, mv = np.asarray(labs[f]["tl"]), np.asarray(labs[f]["mv"])
        out[f] = (tl == 3) & (np.abs(mv).max(axis=-1) > 0) & (cls[f] != 3)
    return out


def mutant_hit_mask(rgbs, truths, labs, g):
    """Mutant 2: diag tiles fed with the caller's verdict, and engine misses folded into the mask."""
    F, N = len(rgbs), g.N
    tiles = dm.diagnose_sequence(rgbs, truths, np.zeros((F, N), np.int64), np.zeros((F, N, 2), np.int64), g,
                                 label_list=labs)
    out = np.zeros((F, N), bool)
    for t in tiles:
        if t["cause"] in ("a", "b") or not t["hit"]:
            out[t["frame"], t["tile"]] = True
    return out


class TestFlagUnobservable(unittest.TestCase):
    def test_flag_value_is_free_bit(self):
        self.assertEqual(labels.FLAG_UNOBSERVABLE, 8)
        self.assertEqual(score.FLAG_UNOBSERVABLE, labels.FLAG_UNOBSERVABLE)
        self.assertEqual(labels.FLAG_UNOBSERVABLE & (labels.FLAG_EDGE_ONLY | labels.FLAG_BG | labels.FLAG_CAM_MOTION), 0)

    def test_flat_square_interior_flagged_and_excluded(self):
        rgbs, truths, boxes, g, labs = setup("flat")
        unobs = rb.unobservable(rgbs, truths, labs, g)
        inner = interior_tiles(g, boxes, 3)
        self.assertGreaterEqual(len(inner), 6)
        self.assertTrue(all(unobs[f, i] for f, i in inner))
        self.assertFalse(unobs[0].any())  # frame 0 has no t-1
        _, arrs = labels.parse_truth_tiles(labels.mark_unobservable(blob_of(g, labs), unobs), g)
        fl = np.stack([a["fl"] for a in arrs])
        self.assertTrue(all(fl[f, i] & 8 for f, i in inner))
        tl = np.stack([lb["tl"] for lb in labs])
        mvt = np.stack([lb["mv"] for lb in labs])
        cls = np.full(tl.shape, 3, np.int64)  # engine calls everything MOVED with the true vector
        raw = score.score_frames(tl, cls, mvt, mvt.astype(np.int64), "B1", 0, 4, g.ident, 64)
        row = score.score_frames(tl, cls, mvt, mvt.astype(np.int64), "B1", 0, 4, g.ident, 64, flags=fl)
        n_un = int(unobs.sum())
        self.assertEqual(row["mv_n_unobs"], n_un)
        self.assertEqual(row["mv_n"], raw["mv_n"] - n_un)
        self.assertEqual(row["mv_n_raw"], raw["mv_n"])
        self.assertEqual(row["mv_exact_raw"], raw["mv_exact"])
        self.assertEqual(row["tl"].get("3", {"n": 0})["n"], raw["tl"]["3"]["n"] - n_un)
        self.assertEqual(row["moved_raw"], {"n": raw["tl"]["3"]["n"], "cls_ok": raw["tl"]["3"]["cls_ok"]})
        summ = score.summarize([row])
        self.assertEqual(summ["mv_n_unobs"], n_un)
        self.assertEqual(summ["moved_recall_obs_ppm"], (summ["tl"].get("3") or {}).get("cls_ok_ppm"))
        self.assertIn("moved_recall_raw_ppm", summ)
        self.assertIn("mv_exact_raw_ppm", summ)

    def test_flat_square_engine_miss_leaves_observable_recall(self):
        rgbs, truths, boxes, g, labs = setup("flat")
        g2, cls, mv = dm.engine_run(rgbs)
        self.assertEqual(g2.ident, g.ident)
        unobs = rb.unobservable(rgbs, truths, labs, g)
        tl = np.stack([lb["tl"] for lb in labs])
        mvt = np.stack([lb["mv"] for lb in labs])
        fl = np.where(unobs, labels.FLAG_UNOBSERVABLE, 0).astype(np.uint8)
        row = score.score_frames(tl, cls, mvt, mv, "B1", 0, 4, g.ident, 64, flags=fl)
        raw = score.score_frames(tl, cls, mvt, mv, "B1", 0, 4, g.ident, 64)
        self.assertGreater(row["mv_n_unobs"], 0)
        # the flagged interior tiles are engine misses: dropping them cannot lower the observable recall
        rgbs2, truths2, _, _, labs2 = setup("noise")
        _, cls2, mv2 = dm.engine_run(rgbs2)
        tl2 = np.stack([lb["tl"] for lb in labs2])
        mvt2 = np.stack([lb["mv"] for lb in labs2])
        fl2 = np.where(rb.unobservable(rgbs2, truths2, labs2, g), 8, 0).astype(np.uint8)
        noise = score.score_frames(tl2, cls2, mvt2, mv2, "B2", 0, 4, g.ident, 64, flags=fl2)
        noise_raw = score.score_frames(tl2, cls2, mvt2, mv2, "B2", 0, 4, g.ident, 64)
        r_obs = score.summarize([row, noise])
        r_raw = score.summarize([raw, noise_raw])
        self.assertGreater(r_obs["moved_recall_obs_ppm"], r_obs["moved_recall_raw_ppm"])
        self.assertEqual(r_obs["moved_recall_raw_ppm"], r_raw["tl"]["3"]["cls_ok_ppm"])

    def test_textured_square_never_flagged(self):
        rgbs, truths, _, g, labs = setup("noise")
        unobs = rb.unobservable(rgbs, truths, labs, g)
        self.assertEqual(int(unobs.sum()), 0)

    def test_flags_byte_compatible(self):
        rgbs, truths, _, g, labs = setup("flat")
        b0 = blob_of(g, labs)
        unobs = rb.unobservable(rgbs, truths, labs, g)
        b1 = labels.mark_unobservable(b0, unobs)
        self.assertEqual(len(b1), len(b0))
        self.assertEqual(b1[:labels.TRUTH_HDR_SIZE], b0[:labels.TRUTH_HDR_SIZE])
        diff = [k for k in range(len(b0)) if b0[k] != b1[k]]
        self.assertTrue(diff)
        self.assertTrue(all((k - labels.TRUTH_HDR_SIZE) % labels.TRUTH_REC_SIZE == 1 for k in diff))  # flags byte only
        self.assertTrue(all(b1[k] ^ b0[k] == 8 for k in diff))
        h0, a0 = labels.parse_truth_tiles(b0, g)
        h1, a1 = labels.parse_truth_tiles(b1, g)
        self.assertEqual(h0, h1)
        for x, y in zip(a0, a1):
            for k in ("tl", "dx", "dy", "ob", "oc"):
                self.assertTrue(np.array_equal(x[k], y[k]))
            self.assertTrue(np.array_equal(x["fl"] & 7, y["fl"] & 7))
        self.assertEqual(labels.mark_unobservable(b1, unobs), b1)  # idempotent
        with self.assertRaises(Exception):
            labels.mark_unobservable(b0, unobs[:, :-1])

    def test_v1_reader_ignores_bit8(self):
        rgbs, truths, _, g, labs = setup("flat")
        b0 = blob_of(g, labs)
        b1 = labels.mark_unobservable(b0, rb.unobservable(rgbs, truths, labs, g))
        d = Path(self._tmp())
        (d / "a.bin").write_bytes(b0)
        (d / "b.bin").write_bytes(b1)
        r0, r1 = score.read_truth_tiles(d / "a.bin"), score.read_truth_tiles(d / "b.bin")
        for k in ("tl", "mv", "ob", "occ"):
            self.assertTrue(np.array_equal(r0[k], r1[k]))
        self.assertEqual(r0["hdr"], r1["hdr"])
        tl, mvt = r1["tl"], r1["mv"]
        cls = np.zeros(tl.shape, np.int64)
        self.assertEqual(score.score_frames(tl, cls, mvt, mvt.astype(np.int64), "B1", 0, 4, g.ident, 64),
                         score.score_frames(r0["tl"], cls, r0["mv"], mvt.astype(np.int64), "B1", 0, 4, g.ident, 64))

    def _tmp(self):
        d = tempfile.mkdtemp(prefix="w10f_")
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def test_flags_none_rows_byte_identical(self):
        b = _golden_rows(score.score_frames)
        self.assertEqual(hashlib.sha256(b).hexdigest(), GOLDEN_ROWS_SHA)
        self.assertEqual(_golden_rows(lambda *a, **k: score.score_frames(*a, flags=None, **k)), b)

    def test_flags_none_matches_pre_w10f_scorer(self):
        try:
            src = subprocess.run(["git", "-C", str(ROOT), "show", PRE_W10F + ":blender_visual_qa/truth/score.py"],
                                 capture_output=True, timeout=30, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            self.skipTest("git object %s unavailable" % PRE_W10F)
        spec = importlib.util.spec_from_loader("blender_visual_qa.truth._score_pre_w10f", loader=None)
        old = importlib.util.module_from_spec(spec)
        old.__package__ = "blender_visual_qa.truth"
        exec(compile(src, "score_pre_w10f.py", "exec"), old.__dict__)
        self.assertEqual(_golden_rows(old.score_frames), _golden_rows(score.score_frames))

    def test_flags_shape_refused(self):
        tl = np.zeros((2, 4), np.uint8)
        mv = np.zeros((2, 4, 2), np.int8)
        with self.assertRaises(VqaError):
            score.score_frames(tl, tl.astype(np.int64), mv, mv.astype(np.int64), "B1", 0, 4, "g", 64,
                               flags=np.zeros((2, 3), np.uint8))


class TestMaskEngineFree(unittest.TestCase):
    def test_real_mask_is_engine_free(self):
        rgbs, truths, boxes, g, labs = setup("noise")
        self.assertTrue(mask_engine_free(rb.unobservable, rgbs, truths, labs, g))
        rgbs, truths, boxes, g, labs = setup("flat")
        self.assertTrue(mask_engine_free(rb.unobservable, rgbs, truths, labs, g))

    def test_mutant_engine_verdict_killed(self):
        rgbs, truths, _, g, labs = setup("noise")
        self.assertFalse(mask_engine_free(mutant_engine_verdict, rgbs, truths, labs, g))

    def test_mutant_hit_mask_killed(self):
        rgbs, truths, _, g, labs = setup("noise")
        self.assertFalse(mask_engine_free(mutant_hit_mask, rgbs, truths, labs, g))


def fake_run(d, kind, dx=3, beauty=True):
    """Run dir with truth/ (frames.jsonl + f<RI6>.{ob,mv,z}.npy) and, when beauty, a decoded-beauty manifest whose
    frames are served by a patched pipeline.ManifestSource."""
    rgbs, truths, boxes = sequence(kind, dx)
    g = Grid(W, H)
    tr = Path(d) / "truth"
    tr.mkdir(parents=True)
    rows = []
    for ri, t in enumerate(truths):
        for k in ("ob", "mv", "z"):
            np.save(tr / ("f%06d.%s.npy" % (ri, k)), t[k])
        rows.append(json.dumps({"render_index": ri, "width": W, "height": H, "grid": g.ident, "motion_blur": False}))
    (tr / "frames.jsonl").write_text("\n".join(rows) + "\n")
    if beauty:
        (Path(d) / "vision_in").mkdir()
        (Path(d) / "vision_in" / "frames.manifest.json").write_text("{}")
    return rgbs, boxes, g


class TestRunBenchFlags(unittest.TestCase):
    def _labels(self, kind, beauty=True):
        d = tempfile.mkdtemp(prefix="w10f_run_")
        self.addCleanup(shutil.rmtree, d, True)
        rgbs, boxes, g = fake_run(d, kind, beauty=beauty)
        src = [SimpleNamespace(rgb=r) for r in rgbs]
        with mock.patch.object(rb.pipeline, "ManifestSource", lambda man: iter(src)):
            tt, res, out = rb.truth_labels(d)
        return tt, res, out, boxes, g

    def test_truth_labels_flag_flat_interior(self):
        tt, res, out, boxes, g = self._labels("flat")
        self.assertTrue(res["unobservable_flagged"])
        inner = interior_tiles(g, boxes, 3)
        self.assertTrue(inner and all(tt["flags"][f, i] & 8 for f, i in inner))
        disk = score.read_truth_tiles(Path(out) / "tiles_truth.bin")
        self.assertTrue(np.array_equal(disk["flags"], tt["flags"]))
        self.assertEqual(res["sha256"], hashlib.sha256((Path(out) / "tiles_truth.bin").read_bytes()).hexdigest())

    def test_truth_labels_textured_unflagged(self):
        tt, res, _, _, _ = self._labels("noise")
        self.assertTrue(res["unobservable_flagged"])
        self.assertFalse((tt["flags"] & 8).any())

    def test_truth_labels_without_beauty_unflagged(self):
        tt, res, _, _, _ = self._labels("flat", beauty=False)
        self.assertFalse(res["unobservable_flagged"])
        self.assertFalse((tt["flags"] & 8).any())


class TestRunBenchCpu(unittest.TestCase):
    def test_cpu_defaults_15_19(self):
        self.assertEqual(rb.CPUS, "15-19")
        self.assertEqual(rb.CPU_PLANS["w9"], ("15-19", "0-4", "10-13"))
        self.assertEqual(rb.CPU_PLANS["w10"], ("5-9", "15-19", "0-4", "10-14"))
        self.assertEqual(rb.JOB_CPUS, rb.CPU_PLANS["w9"])
        self.assertNotIn("14", rb.CPUS.split("-"))


if __name__ == "__main__":
    unittest.main()
