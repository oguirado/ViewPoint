"""RT0 D02 lane T: S2 classify_tiles speed-up must stay byte-identical to sealed v1 (ve-sealed-1), and the sealed-tree gate must
admit the union of per-card receipts (RT0D01 + RT0D02).

Oracles: the sealed change.py (git show ve-sealed-1:visioncore/change.py, loaded as a temp module), random planes, scenario sequences.
Env overrides (used by tests/mutants/rt0d02/run.py only): RT0D02_CHANGE_PATH, RT0D02_GATE_PATH, RT0D02_SEALED_SRC."""
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import tracemalloc
import unittest
from pathlib import Path

import numpy as np

from visioncore import desc, layout
from visioncore.grid import Grid

ROOT = Path(__file__).resolve().parent.parent


def load_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def load_sealed():
    src = os.environ.get("RT0D02_SEALED_SRC")
    if src:
        text = Path(src).read_text()
    else:
        text = subprocess.run(["git", "-C", str(ROOT), "show", "ve-sealed-1:visioncore/change.py"], check=True,
                              capture_output=True, text=True, timeout=60).stdout
    with tempfile.TemporaryDirectory(prefix="sealed_rt0d02_") as d:    # honours TMPDIR; removed once the module is executed
        p = Path(d) / "sealed_change.py"
        p.write_text(text)
        return load_path("visioncore._sealed_change_rt0d02", p)   # package visioncore: relative imports resolve to the live layout/params


S = load_sealed()
L = load_path("visioncore._live_change_rt0d02", os.environ.get("RT0D02_CHANGE_PATH", ROOT / "visioncore" / "change.py"))


# ---------------------------------------------------------------- _tile_sad / _tile_sad_clip

def planes(rng, s, gh, gw, kind):
    shape = (gh * s, gw * s)
    if kind == "noise":
        return rng.integers(0, 256, shape).astype(np.int16)
    if kind == "low":      # many equal costs
        return rng.integers(0, 3, shape).astype(np.int16)
    return rng.choice(np.array([0, 255], np.int16), shape)   # extremes


def vectors(rng, s):
    r = s + 2
    allv = [(dx, dy) for dy in range(-r, r + 1) for dx in range(-r, r + 1)]
    if s < 64:
        return allv
    ring = [v for v in allv if max(abs(v[0]), abs(v[1])) >= s - 2]   # every out-of-image / edge-touching shift
    pick = rng.choice(len(allv), 300, replace=False)
    return ring + [allv[i] for i in pick] + [(0, 0)]


def requests(rng, gh, gw):
    ty, tx = np.divmod(np.arange(gh * gw), gw)
    sub = rng.integers(0, gh * gw, max(1, gh * gw // 2))   # unordered, with duplicates
    return [(tx.astype(np.int64), ty.astype(np.int64)), (tx[sub].astype(np.int64), ty[sub].astype(np.int64))]


class TestTileSadEquivalence(unittest.TestCase):
    CASES = [(16, 1, 1), (16, 3, 5), (16, 4, 6), (32, 2, 3), (32, 1, 2), (64, 2, 3), (64, 1, 1)]

    def check(self, fn, s, gh, gw, seed):
        rng = np.random.default_rng(seed)
        bad = []
        for kind in ("noise", "low", "ext"):
            cur, prev = planes(rng, s, gh, gw, kind), planes(rng, s, gh, gw, kind)
            reqs = requests(rng, gh, gw)
            for v in vectors(rng, s):
                for tx, ty in reqs:
                    a = getattr(S, fn)(cur, prev, v, s, gh, gw, tx, ty)
                    b = getattr(L, fn)(cur, prev, v, s, gh, gw, tx, ty)
                    for x, y in zip(a, b):
                        if x.dtype != y.dtype or x.shape != y.shape or not np.array_equal(x, y):
                            bad.append((kind, v, len(tx)))
                            break
                    if len(bad) > 3:
                        return bad
        return bad

    def test_tile_sad_equals_sealed(self):
        for i, (s, gh, gw) in enumerate(self.CASES):
            self.assertEqual(self.check("_tile_sad", s, gh, gw, 100 + i), [], (s, gh, gw))

    def test_tile_sad_clip_equals_sealed(self):
        for i, (s, gh, gw) in enumerate(self.CASES):
            self.assertEqual(self.check("_tile_sad_clip", s, gh, gw, 200 + i), [], (s, gh, gw))

    def test_validity_is_full_window_not_clipped(self):
        """An edge tile shifted partly out of the image has a clipped SAD but is NOT valid; clip count < s*s."""
        s, gh, gw = 16, 2, 2
        rng = np.random.default_rng(3)
        cur, prev = planes(rng, s, gh, gw, "noise"), planes(rng, s, gh, gw, "noise")
        tx, ty = np.array([0, 1, 0, 1], np.int64), np.array([0, 0, 1, 1], np.int64)
        _, valid = L._tile_sad(cur, prev, (5, 0), s, gh, gw, tx, ty)
        _, cnt = L._tile_sad_clip(cur, prev, (5, 0), s, gh, gw, tx, ty)
        self.assertEqual(valid.tolist(), [False, True, False, True])    # tile column 0 reads x-5 < 0
        self.assertEqual(cnt.tolist(), [(s - 5) * s, s * s, (s - 5) * s, s * s])

    def test_requested_tiles_only_memory(self):
        """Failing-first: 8 requested tiles of a 4K plane must not build full-frame int64 temporaries (sealed peak > 200 MB).
        Reading of the card goal 'only over the requested tiles' windows': peak < 2 bytes/pixel of the frame."""
        s, gh, gw = 64, 34, 60
        rng = np.random.default_rng(9)
        cur = rng.integers(0, 256, (gh * s, gw * s)).astype(np.int16)
        prev = rng.integers(0, 256, (gh * s, gw * s)).astype(np.int16)
        tx, ty = rng.integers(0, gw, 8).astype(np.int64), rng.integers(0, gh, 8).astype(np.int64)
        for fn in ("_tile_sad", "_tile_sad_clip"):
            tracemalloc.start()
            try:
                getattr(L, fn)(cur, prev, (-7, 11), s, gh, gw, tx, ty)
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            self.assertLess(peak, 2 * cur.size, (fn, peak))


# ---------------------------------------------------------------- classify_tiles on scenario sequences

def texture(rng, h, w):
    blocks = rng.integers(0, 256, (h // 4 + 2, w // 4 + 2, 3))
    big = np.repeat(np.repeat(blocks, 4, 0), 4, 1)[:h, :w]
    return np.clip(big + rng.integers(-20, 21, (h, w, 3)), 0, 255).astype(np.uint8)


def stripes(h, w, period=8):
    x = (np.arange(w) // (period // 2)) % 2
    row = np.where(x, 200, 40).astype(np.uint8)
    return np.broadcast_to(row[None, :, None], (h, w, 3)).copy()


def scenario(name, rng, W, H, nf=10):
    pad = 24 * nf
    frames = []
    if name.startswith("scroll"):
        vx, vy = {"scroll_h": (3, 0), "scroll_v": (0, -4), "scroll_diag": (5, 2), "scroll_large": (19, 0),
                  "scroll_large_v": (0, 18), "scroll_neg": (-6, 0)}[name]
        big = texture(rng, H + pad * 2, W + pad * 2)
        for k in range(nf):
            y0, x0 = pad + k * vy, pad + k * vx
            frames.append(big[y0:y0 + H, x0:x0 + W].copy())
    elif name == "periodic_tie":
        big = stripes(H, W + pad * 2)
        for k in range(nf):
            x0 = pad + k * 4
            frames.append(big[:, x0:x0 + W].copy())
    elif name == "flicker":
        a = texture(rng, H, W)
        b = a.copy()
        b[10:40, 20:60] = np.clip(b[10:40, 20:60].astype(np.int16) + 70, 0, 255).astype(np.uint8)
        frames = [a if k % 2 == 0 else b for k in range(nf)]
    elif name == "drift":
        a = texture(rng, H, W)
        for k in range(nf):
            f = a.copy()
            f[8:48, 8:64] = np.clip(f[8:48, 8:64].astype(np.int16) + k * 2, 0, 255).astype(np.uint8)
            frames.append(f)
    elif name == "frozen":     # patch A active 10 frames then still >= FROZEN_M; patch B keeps the frame alive (coverage)
        a = texture(rng, H, W)
        last = None
        for k in range(nf + 12):
            f = a.copy()
            f[40:56, 64:80] = texture(rng, 16, 16)            # B: always changing
            if k < nf:
                f[0:16, 0:16] = texture(rng, 16, 16)           # A: active
                last = f[0:16, 0:16].copy()
            else:
                f[0:16, 0:16] = last
            frames.append(f)
    elif name == "island":
        bg = texture(rng, H, W)
        patch = texture(rng, 24, 24)
        for k in range(nf):
            f = bg.copy()
            x0 = 6 + k * 3
            f[20:44, x0:x0 + 24] = patch
            frames.append(f)
    elif name == "island_over_scroll":
        big = texture(rng, H + pad * 2, W + pad * 2)
        patch = texture(rng, 20, 20)
        for k in range(nf):
            f = big[pad:pad + H, pad + k * 2:pad + k * 2 + W].copy()
            f[16:36, 10 + k * 4:30 + k * 4] = patch
            frames.append(f)
    else:   # noise: sparse pixel flips on a static texture
        a = texture(rng, H, W)
        for k in range(nf):
            f = a.copy()
            idx = rng.integers(0, H * W, 40)
            f.reshape(-1, 3)[idx] = rng.integers(0, 256, (40, 3))
            frames.append(f)
    return frames


NAMES = ["scroll_h", "scroll_v", "scroll_diag", "scroll_large", "scroll_large_v", "scroll_neg", "periodic_tie", "flicker",
         "drift", "frozen", "island", "island_over_scroll", "noise"]
SIZES = [(95, 63), (160, 96)]


def inputs(frames, W, H):
    grid = Grid(W, H)
    shapes = (grid.s, grid.gw, grid.gh)
    prev_padded = prev_raw = None
    rows = []
    for f in frames:
        padded = grid.pad(f)
        y = desc.luma_of_padded(padded)
        tiles, words_eq = desc.describe_padded(padded, grid, prev_padded, None, None, prev_raw, y_plane=y, lazy_xh=True)
        rows.append((tiles.copy(), words_eq.copy(), y))
        prev_padded, prev_raw = padded, tiles
    return rows, shapes


def run_module(M, rows, shapes):
    out = []
    st = None
    for tiles, words_eq, y in rows:
        if st is None:
            st = M.ClassState.init(tiles)
            o, st, summ = M.classify_first(st, tiles, y_cur=y)
        else:
            o, st, summ = M.classify_tiles(st, tiles, words_eq, y_cur=y, grid=shapes)
        out.append((o.tobytes(), json.dumps(summ, sort_keys=True, default=str), st.digest_bytes(), np.asarray(st.y_prev).tobytes(),
                    np.asarray(o["cls"]).copy()))
    return out


class TestClassifyEquivalence(unittest.TestCase):
    def test_sequences_equal_sealed(self):
        classes = {}
        for si, (W, H) in enumerate(SIZES):
            for ni, name in enumerate(NAMES):
                for seed in (1, 2):
                    rng = np.random.default_rng(1000 * si + 10 * ni + seed)
                    rows, shapes = inputs(scenario(name, rng, W, H), W, H)
                    a, b = run_module(S, rows, shapes), run_module(L, rows, shapes)
                    for k, (x, y) in enumerate(zip(a, b)):
                        self.assertEqual(x[:4], y[:4], (name, W, H, seed, k))
                        classes.setdefault(name, set()).update(np.unique(x[4]).tolist())
        # the sequences must really reach the classes under test (non-vacuous): MOVED (rule 4), FLICKER, FROZEN, DRIFT
        moved = {S.C_MOVED}
        for n in ("scroll_h", "scroll_v", "scroll_diag", "scroll_neg", "island_over_scroll", "periodic_tie"):
            self.assertTrue(moved & classes[n], n)
        self.assertIn(S.C_FLICKER, classes["flicker"])
        self.assertIn(S.C_FROZEN, classes["frozen"])
        self.assertTrue({S.C_DRIFT, S.C_MOVED, S.C_CHANGED} & classes["drift"])

    def test_global_vector_ties_equal_sealed(self):
        """Tie-break (cost, |d|, d): periodic stripes make +d and -d cost the same; ties must resolve exactly as sealed."""
        rng = np.random.default_rng(7)
        for period in (4, 8, 16):
            big = stripes(48, 160, period)[..., 0].astype(np.int16)
            for sh in range(-9, 10):
                cur = big[:, 40:104]
                prev = big[:, 40 - sh:104 - sh]
                for s in (16, 32):
                    self.assertEqual(L.global_vector(cur, prev, s), S.global_vector(cur, prev, s), (period, sh, s))
        cur = rng.integers(0, 4, (48, 64)).astype(np.int16)
        prev = rng.integers(0, 4, (48, 64)).astype(np.int16)
        self.assertEqual(L.global_vector(cur, prev, 16), S.global_vector(cur, prev, 16))

    def test_island_vector_equal_sealed(self):
        rng = np.random.default_rng(8)
        s, gh, gw = 16, 4, 6
        for _ in range(20):
            cur = rng.integers(0, 256, (gh * s, gw * s)).astype(np.int16)
            prev = np.roll(cur, int(rng.integers(-5, 6)), axis=1)
            m = rng.random((gh, gw)) < 0.3
            m[rng.integers(0, gh), rng.integers(0, gw)] = True
            self.assertEqual(L.island_vector(m, cur, prev, s), S.island_vector(m, cur, prev, s))


# ---------------------------------------------------------------- receipt union (tools/gate_checks.sealed_untouched)

def load_gate_checks():
    return load_path("_t_rt0d02_gate_checks", os.environ.get("RT0D02_GATE_PATH", ROOT / "tools" / "gate_checks.py"))


def git(d, *a):
    return subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True,
                          capture_output=True, text=True, timeout=60)


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def write_receipt(r, card, entries, *, proof_sha_in_entry=None, proof_sha_in_receipt=None, entry_card=None, verdict="PASS"):
    """card RT0D01 -> eval/results/rt0/, RT0D02 -> eval/results/rt0d02/ (vqa.rt0.equiv/v1 + PROOF_SHA_TABLE.json)."""
    sub = "rt0" if card == "RT0D01" else "rt0d02"
    d = r / "eval" / "results" / sub
    d.mkdir(parents=True, exist_ok=True)
    proof = json.dumps({"verdict": verdict, "card": card}).encode()
    (d / "PROOF_SHA_TABLE.json").write_bytes(proof)
    psha = hashlib.sha256(proof).hexdigest()
    (d / "EQUIV_RECEIPT.json").write_text(json.dumps({
        "schema": "vqa.rt0.equiv/v1", "card_id": card, "proof_table": f"eval/results/{sub}/PROOF_SHA_TABLE.json",
        "proof_table_sha256": proof_sha_in_receipt or psha,
        "files": [{"path": p, "old_sha256": o, "new_sha256": n, "card_id": entry_card or card,
                   "proof_table_sha256": proof_sha_in_entry or psha} for p, o, n in entries]}))


class TestReceiptUnion(unittest.TestCase):
    """a.py plays the RT0D01 file, b.py the RT0D02 file (change.py)."""

    def setUp(self):
        self.c = load_gate_checks()
        self.td = tempfile.TemporaryDirectory(prefix="rt0d02_gate_")
        self.r = Path(self.td.name)
        git(self.r, "init", "-q")
        (self.r / "visioncore").mkdir()
        for n in ("a", "b", "c"):
            (self.r / "visioncore" / f"{n}.py").write_text(f"{n.upper()} = 1\n")
        git(self.r, "add", "-A")
        git(self.r, "commit", "-qm", "x")
        git(self.r, "tag", "ve-sealed-1")
        self.old = {n: sha(self.r / "visioncore" / f"{n}.py") for n in "abc"}

    def tearDown(self):
        self.td.cleanup()

    def edit(self, n, text):
        (self.r / "visioncore" / f"{n}.py").write_text(text)
        return sha(self.r / "visioncore" / f"{n}.py")

    def both(self, **kw2):
        na, nb = self.edit("a", "A = 2\n"), self.edit("b", "B = 2\n")
        write_receipt(self.r, "RT0D01", [("visioncore/a.py", self.old["a"], na)])
        write_receipt(self.r, "RT0D02", [("visioncore/b.py", self.old["b"], nb)], **kw2)

    def msgs(self):
        return [m for _, m in self.c.sealed_untouched(self.r)]

    def test_union_admits_both_receipts(self):       # M5 kills a union that drops RT0D01
        self.both()
        self.assertEqual(self.c.sealed_untouched(self.r), [])

    def test_d01_alone_still_valid(self):
        na = self.edit("a", "A = 2\n")
        write_receipt(self.r, "RT0D01", [("visioncore/a.py", self.old["a"], na)])
        self.assertEqual(self.c.sealed_untouched(self.r), [])

    def test_d02_alone_valid(self):
        nb = self.edit("b", "B = 2\n")
        write_receipt(self.r, "RT0D02", [("visioncore/b.py", self.old["b"], nb)])
        self.assertEqual(self.c.sealed_untouched(self.r), [])

    def test_d01_file_without_its_receipt_rejected(self):   # D01 receipt missing: a.py is not covered by the D02 receipt
        self.both()
        (self.r / "eval/results/rt0/EQUIV_RECEIPT.json").unlink()
        ms = self.msgs()
        self.assertTrue(any("a.py" in m for m in ms), ms)

    def test_d02_wrong_proof_sha_in_entry_rejected(self):   # M4
        self.both(proof_sha_in_entry="0" * 64)
        ms = self.msgs()
        self.assertTrue(ms and any("b.py" in m for m in ms), ms)

    def test_d02_wrong_proof_sha_in_receipt_rejected(self):  # M4 (receipt level)
        self.both(proof_sha_in_receipt="1" * 64)
        ms = self.msgs()
        self.assertTrue(ms and any("b.py" in m for m in ms), ms)

    def test_d02_failed_verdict_rejected(self):
        self.both(verdict="FAIL")
        ms = self.msgs()
        self.assertTrue(ms and any("b.py" in m for m in ms), ms)

    def test_d02_entry_with_other_card_id_rejected(self):
        self.both(entry_card="RT0D01")
        ms = self.msgs()
        self.assertTrue(ms and any("b.py" in m for m in ms), ms)

    def test_d02_new_sha_mismatch_rejected(self):
        self.both()
        self.edit("b", "B = 3\n")          # edited again after the receipt
        ms = self.msgs()
        self.assertTrue(any("b.py" in m and "differs" in m for m in ms), ms)

    def test_d02_old_sha_mismatch_rejected(self):
        nb = self.edit("b", "B = 2\n")
        write_receipt(self.r, "RT0D02", [("visioncore/b.py", "f" * 64, nb)])
        ms = self.msgs()
        self.assertTrue(ms and any("b.py" in m for m in ms), ms)

    def test_unlisted_file_rejected_with_both_receipts(self):
        self.both()
        self.edit("c", "C = 2\n")
        ms = self.msgs()
        self.assertTrue(any("c.py" in m for m in ms), ms)
        self.assertFalse(any("a.py" in m or "b.py" in m for m in ms), ms)    # listed files stay accepted

    def test_untracked_unlisted_rejected_with_both_receipts(self):
        self.both()
        self.edit("z", "")
        ms = self.msgs()
        self.assertTrue(any("z.py" in m for m in ms), ms)

    def test_file_not_accepted_through_the_wrong_receipt(self):  # c.py listed only by neither; d02 cannot vouch for a.py's edit
        na = self.edit("a", "A = 2\n")
        nb = self.edit("b", "B = 2\n")
        write_receipt(self.r, "RT0D02", [("visioncore/b.py", self.old["b"], nb)])   # no RT0D01 receipt at all
        ms = self.msgs()
        self.assertTrue(any("a.py" in m for m in ms), ms)
        self.assertEqual(na, sha(self.r / "visioncore" / "a.py"))

    def test_real_tree_clean(self):
        self.assertEqual(self.c.sealed_untouched(ROOT), [])


if __name__ == "__main__":
    unittest.main()
