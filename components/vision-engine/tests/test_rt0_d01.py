"""RT0 D01 (lane T): parallel analyze/describe_many is byte-identical to serial, S1 reuse really happens in the
workers (describe calls without prev <= workers), and the sealed gate accepts only files listed in
eval/results/rt0/EQUIV_RECEIPT.json with a matching new_sha256. Mutants: tests/mutants/rt0/run.py."""
import hashlib
import importlib.util
import json
import multiprocessing as mp
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from visioncore import desc, frame as fr, parallel, pipeline
from visioncore.grid import Grid

ROOT = Path(__file__).resolve().parent.parent
W, H = 96, 64


def make_frames(n, seed=5):
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 256, (H, W, 3), dtype=np.uint8)
    out = []
    for k in range(n):
        f = base.copy()
        f[(k * 7) % H:(k * 7) % H + 8, 4:40] ^= np.uint8(k + 1)   # moving patch: every frame differs from the last
        if k % 4 == 3:
            f = out[-1].copy()                                     # exact repeat: all tiles reusable
        out.append(f)
    return out


def source(n, seed=5):
    return fr.MemoryFrameSource([fr.Frame(i, i * 33333, i, np.ascontiguousarray(f)) for i, f in enumerate(make_frames(n, seed))],
                                {"width": W, "height": H, "fps_num": 30, "fps_den": 1})


def run_digest(run):
    h = hashlib.sha256()
    for t in run.tiles:
        h.update(t.tobytes())
    h.update(json.dumps([run.regions, run.links, run.global_mv, run.frame_rows, run.gops()], sort_keys=True,
                        default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o)).encode())
    h.update(json.dumps(run.vision_json(), sort_keys=True).encode())
    return h.hexdigest(), {k: hashlib.sha256(v).hexdigest() for k, v in sorted(run.gop_files.items())}


def serial_chain(frames, grid):
    prev = prev_xh = prev_t = None
    rows = []
    for f in frames:
        padded = grid.pad(f)
        t, _ = desc.describe_padded(padded, grid, prev, prev_xh, None, prev_t, lazy_xh=True)
        rows.append(t)
        prev, prev_xh, prev_t = padded, None, t
    return np.stack(rows) if rows else np.zeros((0, grid.N), desc.TILE_DTYPE)


class TestSerialEquality(unittest.TestCase):
    def test_describe_many_boundaries(self):
        grid = Grid(W, H)
        for n in (1, 2, 3, 5, 7, 13):
            fs = np.stack(make_frames(n))
            ref = parallel.describe_many(fs, grid, 1, lazy_xh=True)
            self.assertEqual(ref.tobytes(), serial_chain(fs, grid).tobytes(), n)
            for w in (2, 3, 5, 8, 20):   # includes workers > frames and ranges of unequal length
                got = parallel.describe_many(fs, grid, w, cpus=[], lazy_xh=True)
                self.assertEqual(got.tobytes(), ref.tobytes(), (n, w))

    def test_describe_many_full_hash(self):
        grid = Grid(W, H)
        fs = np.stack(make_frames(9))
        self.assertEqual(parallel.describe_many(fs, grid, 4, cpus=[]).tobytes(), parallel.describe_many(fs, grid, 1).tobytes())

    def test_analyze_outputs_identical(self):
        for n in (1, 2, 3, 6, 11):
            ref = run_digest(pipeline.analyze(source(n), workers=1))
            for w in (2, 5, 16):
                self.assertEqual(run_digest(pipeline.analyze(source(n), workers=w, cpus=[])), ref, (n, w))

    def test_analyze_odd_size_padding_identical(self):
        # JUDGE_D01 F3: 95x63 pads both axes, so the worker's in-place edge replication (rows and columns) is executed
        rng = np.random.default_rng(91)
        images = rng.integers(0, 256, (13, 63, 95, 3), dtype=np.uint8)
        images[3] = images[2]
        images[9] = images[8]
        meta = {"width": 95, "height": 63, "fps_num": 30, "fps_den": 1}

        def src():
            return fr.MemoryFrameSource([fr.Frame(i, i * 33333, i, np.ascontiguousarray(f)) for i, f in enumerate(images)], meta)
        ref = run_digest(pipeline.analyze(src(), workers=1))
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as t:
            shas = []
            for w in (1, 2, 5):
                self.assertEqual(run_digest(pipeline.analyze(src(), workers=w, cpus=[])), ref, w)
                out = Path(t) / f"w{w}"
                pipeline.analyze(src(), None, None, out, workers=w, cpus=[])
                shas.append({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.iterdir())})
            self.assertEqual(shas[1], shas[0])
            self.assertEqual(shas[2], shas[0])

    def test_analyze_written_files_identical(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as t:
            shas = []
            for w in (1, 2, 5):
                out = Path(t) / f"w{w}"
                pipeline.analyze(source(7), None, None, out, workers=w, cpus=[])
                shas.append({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.iterdir())})
            self.assertEqual(shas[0], shas[1])
            self.assertEqual(shas[0], shas[2])
            self.assertIn("tiles.bin", shas[0])


class TestReuseHappens(unittest.TestCase):
    """The parallel path used to describe every frame with words_prev=None (0 % reuse): count those calls."""

    def counted(self, fn):
        return self.counted2(fn)[0]

    def counted2(self, fn):
        """(calls with words_prev None, calls with prev_tiles None); counters are fork-shared with the workers."""
        ctx = mp.get_context("fork")
        no_prev, no_tiles = ctx.Value("i", 0), ctx.Value("i", 0)
        real = desc.describe_padded

        def spy(padded, grid, words_prev=None, prev_xh64=None, p=None, prev_tiles=None, *a, **k):
            for c, miss in ((no_prev, words_prev is None), (no_tiles, prev_tiles is None)):
                if miss:
                    with c.get_lock():
                        c.value += 1
            return real(padded, grid, words_prev, prev_xh64, p, prev_tiles, *a, **k)

        with mock.patch.object(desc, "describe_padded", spy):
            fn()
        return no_prev.value, no_tiles.value

    def test_serial_baseline_one_cold_call(self):
        self.assertEqual(self.counted(lambda: pipeline.analyze(source(9), workers=1)), 1)

    def test_describe_many_cold_calls_le_workers(self):
        grid = Grid(W, H)
        fs = np.stack(make_frames(12))
        for w in (2, 3, 5):
            cold = self.counted(lambda: parallel.describe_many(fs, grid, w, cpus=[], lazy_xh=True))
            self.assertLessEqual(cold, w, w)
            self.assertGreaterEqual(cold, 1)

    def test_no_prev_tiles_calls_le_workers(self):
        grid = Grid(W, H)
        fs = np.stack(make_frames(12))
        for w in (2, 3, 5):
            _, no_tiles = self.counted2(lambda: parallel.describe_many(fs, grid, w, cpus=[], lazy_xh=True))
            self.assertLessEqual(no_tiles, w, w)   # only each range's first frame may lack the previous descriptors
        _, no_tiles = self.counted2(lambda: pipeline.analyze(source(12), workers=3, cpus=[]))
        self.assertLessEqual(no_tiles, 3)

    def test_analyze_cold_calls_le_workers(self):
        for w in (2, 5):
            cold = self.counted(lambda: pipeline.analyze(source(12), workers=w, cpus=[]))
            self.assertLessEqual(cold, w, w)   # main-thread reconciliation included


def load_gate_checks():
    spec = importlib.util.spec_from_file_location("_t_rt0_gate_checks", ROOT / "tools" / "gate_checks.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def git(d, *a):
    return subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True,
                          capture_output=True, text=True, timeout=60)


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def write_receipt(r, entries):
    """EQUIV_RECEIPT.json (vqa.rt0.equiv/v1) + PROOF_SHA_TABLE.json as tools/gate_checks.sealed_untouched reads them;
    entries = [(path, old_sha, new_sha)]."""
    d = r / "eval" / "results" / "rt0"
    d.mkdir(parents=True, exist_ok=True)
    proof = json.dumps({"verdict": "PASS"}).encode()
    (d / "PROOF_SHA_TABLE.json").write_bytes(proof)
    psha = hashlib.sha256(proof).hexdigest()
    (d / "EQUIV_RECEIPT.json").write_text(json.dumps({
        "schema": "vqa.rt0.equiv/v1", "card_id": "RT0D01", "proof_table": "eval/results/rt0/PROOF_SHA_TABLE.json",
        "proof_table_sha256": psha,
        "files": [{"path": p, "old_sha256": o, "new_sha256": n, "card_id": "RT0D01", "proof_table_sha256": psha}
                  for p, o, n in entries]}))


class TestSealedReceipt(unittest.TestCase):
    def setUp(self):
        self.c = load_gate_checks()
        self.td = tempfile.TemporaryDirectory(dir=ROOT / "build")
        self.r = Path(self.td.name)
        git(self.r, "init", "-q")
        (self.r / "visioncore").mkdir()
        (self.r / "visioncore" / "a.py").write_text("A = 1\n")
        (self.r / "visioncore" / "b.py").write_text("B = 1\n")
        git(self.r, "add", "-A")
        git(self.r, "commit", "-qm", "x")
        git(self.r, "tag", "ve-sealed-1")
        self.old_a, self.old_b = sha(self.r / "visioncore" / "a.py"), sha(self.r / "visioncore" / "b.py")

    def tearDown(self):
        self.td.cleanup()

    def edit(self, name, text):
        (self.r / "visioncore" / name).write_text(text)
        return sha(self.r / "visioncore" / name)

    def test_untouched_ok(self):
        self.assertEqual(self.c.sealed_untouched(self.r), [])

    def test_listed_with_matching_sha_accepted(self):
        new = self.edit("a.py", "A = 2\n")
        write_receipt(self.r, [("visioncore/a.py", self.old_a, new)])
        self.assertEqual(self.c.sealed_untouched(self.r), [])

    def test_unlisted_file_rejected(self):      # M3
        new = self.edit("a.py", "A = 2\n")
        self.edit("b.py", "B = 2\n")
        write_receipt(self.r, [("visioncore/a.py", self.old_a, new)])
        msgs = [m for _, m in self.c.sealed_untouched(self.r)]
        self.assertTrue(any("b.py" in m for m in msgs), msgs)
        self.assertFalse(any("a.py" in m for m in msgs), msgs)   # the listed file stays accepted

    def test_diff_without_receipt_rejected(self):
        self.edit("a.py", "A = 2\n")
        self.assertEqual(len(self.c.sealed_untouched(self.r)), 1)

    def test_listed_but_sha_differs_rejected(self):   # M4
        self.edit("a.py", "A = 2\n")
        write_receipt(self.r, [("visioncore/a.py", self.old_a, sha(self.r / "visioncore" / "b.py"))])
        msgs = [m for _, m in self.c.sealed_untouched(self.r)]
        self.assertTrue(msgs and all("a.py" in m for m in msgs), msgs)
        self.assertTrue(any("differs" in m for m in msgs), msgs)   # the file stays rejected, not just flagged

    def test_edited_again_after_receipt_rejected(self):
        new = self.edit("a.py", "A = 2\n")
        write_receipt(self.r, [("visioncore/a.py", self.old_a, new)])
        self.edit("a.py", "A = 3\n")
        msgs = [m for _, m in self.c.sealed_untouched(self.r)]
        self.assertTrue(msgs and any("differs" in m for m in msgs), msgs)

    def test_untracked_unlisted_rejected(self):
        new = self.edit("a.py", "A = 2\n")
        write_receipt(self.r, [("visioncore/a.py", self.old_a, new)])
        self.edit("c.py", "")
        msgs = [m for _, m in self.c.sealed_untouched(self.r)]
        self.assertTrue(any("c.py" in m for m in msgs), msgs)
        self.assertFalse(any("a.py" in m for m in msgs), msgs)

    def test_real_tree_clean(self):
        self.assertEqual(self.c.sealed_untouched(ROOT), [])


if __name__ == "__main__":
    unittest.main()
