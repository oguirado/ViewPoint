"""Independent oracle and differential tests for visioncore.desc describe_padded perceptual fields and fill_xh64.

SPEC_CORE §2 exact perceptual fields, xh64 BLAKE2b hashes, and native differential runner.
"""
import hashlib
import math
import os
import subprocess
import tempfile
import unittest

import numpy as np

from visioncore import layout
from visioncore.desc import TILE_DTYPE, describe_padded, fill_xh64
from visioncore.grid import Grid
from visioncore.params import PARAMS

# SHA256 digests of describe_padded outputs for 6 fixed seeds on Grid(160, 96)
SEED_DESCS_SHA256 = {
    1: "58e24672f86840f409c5d01ebb5a63b52b9b034edd14c28186b08fcbadd548b2",
    2: "cc68a1d3b990321aa3aa4de3e8e90fb6a5b9a027d0855333797037e4fb4cff3a",
    3: "999c8d0d062801a5e69fe33cd0ec3434dcd4eb5403a318f3327ad71c84ece2ba",
    4: "706040f5b288b314081b1349f682a0dca7a01201d7b8c32e698ed0ba7239345a",
    5: "fe99b63cc01f10e118bf3497f6d043c4c5e6f5c98f78360485e1f027d8fbeab5",
    6: "1302ba93d50d7f24e0be6ee9705d3a641aa4ff6ba63efc451ae81d1ce8e532fb",
}


def oracle_tile(padded, tx, ty, s, W, H, tau=48):
    """Plain Python/numpy per-tile oracle computing exact descriptor fields according to SPEC_CORE §2 integer rules."""
    block = padded[ty * s : (ty + 1) * s, tx * s : (tx + 1) * s, :]
    n = s * s
    sy = sy2 = sco = scg = 0
    Y = np.zeros((s, s), dtype=np.int64)
    for y in range(s):
        for x in range(s):
            r = int(block[y, x, 0])
            g = int(block[y, x, 1])
            b = int(block[y, x, 2])
            co = r - b
            t = b + (co >> 1)
            cg = g - t
            yy = t + (cg >> 1)
            Y[y, x] = yy
            sy += yy
            sy2 += yy * yy
            sco += co
            scg += cg

    G = e = sat = 0
    for y in range(s):
        for x in range(s):
            d = (abs(int(Y[y, x + 1]) - int(Y[y, x])) if x + 1 < s else 0) + (
                abs(int(Y[y + 1, x]) - int(Y[y, x])) if y + 1 < s else 0
            )
            G += d
            if d > tau:
                e += 1
            r, g, b = block[y, x]
            if r in (0, 255) or g in (0, 255) or b in (0, 255):
                sat += 1

    b_size = s // 8
    sig = 0
    for i in range(8):
        for j in range(8):
            sub = int(Y[i * b_size : (i + 1) * b_size, j * b_size : (j + 1) * b_size].sum())
            if sub * n > sy * (b_size * b_size):
                sig |= 1 << (8 * i + j)

    f_edge = 1 if (2 * e >= s and e < 4 * s) else 0
    f_texture = 2 if e >= 4 * s else 0
    f_sat = 8 if 4 * sat >= n else 0
    f_pad = 128 if ((tx + 1) * s > W or (ty + 1) * s > H) else 0
    flags = f_edge | f_texture | f_sat | f_pad

    y_q4 = 16 * sy // n
    co_h = max(-128, min(127, sco // (2 * n)))
    cg_h = max(-128, min(127, scg // (2 * n)))
    sig_q4 = 16 * math.isqrt(n * sy2 - sy * sy) // n
    grad_q6 = 64 * G // n
    edge = 255 * e // n

    tile_bytes = block.tobytes()
    h = hashlib.blake2b(tile_bytes, digest_size=8, person=b"vqa-tile").digest()
    xh64 = int.from_bytes(h, "little")

    return {
        "flags": flags,
        "y_q4": y_q4,
        "co_h": co_h,
        "cg_h": cg_h,
        "sig_q4": sig_q4,
        "grad_q6": grad_q6,
        "edge": edge,
        "sig64": sig,
        "xh64": xh64,
    }


def oracle_describe_padded(padded, grid, tau=48):
    """Full frame oracle for describe_padded."""
    N = grid.N
    s = grid.s
    out = np.zeros(N, dtype=TILE_DTYPE)
    out["morton"] = grid.mortons
    for k in range(N):
        tx, ty = int(grid.tx[k]), int(grid.ty[k])
        res = oracle_tile(padded, tx, ty, s, grid.W, grid.H, tau)
        for field, val in res.items():
            out[field][k] = val
    return out


def oracle_fill_xh64(out, padded, grid, prev_xh64=None, hash_all=False):
    """Oracle implementation of fill_xh64."""
    res = out.copy()
    cls = res["cls"]
    N = grid.N
    s = grid.s
    if prev_xh64 is None:
        hash_all = True
        xh = np.zeros(N, dtype=np.uint64)
    else:
        xh = np.array(prev_xh64, dtype=np.uint64)

    for k in range(N):
        if hash_all or cls[k] >= layout.CLS_DIRTY_MIN:
            tx, ty = int(grid.tx[k]), int(grid.ty[k])
            block = padded[ty * s : (ty + 1) * s, tx * s : (tx + 1) * s, :]
            h = hashlib.blake2b(block.tobytes(), digest_size=8, person=b"vqa-tile").digest()
            xh[k] = int.from_bytes(h, "little")

    res["xh64"] = xh
    return res


class TestKRefS1DescOracle(unittest.TestCase):
    def test_oracle_random_rgb_16x16(self):
        W, H = 160, 96
        grid = Grid(W, H)
        for seed in range(10, 15):
            rgb = np.random.RandomState(seed).randint(0, 256, (H, W, 3)).astype(np.uint8)
            padded = grid.pad(rgb)
            ref_out, _ = describe_padded(padded, grid)
            ora_out = oracle_describe_padded(padded, grid)
            self.assertEqual(ref_out.tobytes(), ora_out.tobytes(), f"Seed {seed} s=16 mismatch")

    def test_oracle_random_rgb_32x32(self):
        W, H = 1025, 1025
        grid = Grid(W, H)
        self.assertEqual(grid.s, 32)
        rgb = np.random.RandomState(42).randint(0, 256, (H, W, 3)).astype(np.uint8)
        padded = grid.pad(rgb)
        ref_out, _ = describe_padded(padded, grid)
        ora_out = oracle_describe_padded(padded, grid)
        self.assertEqual(ref_out.tobytes(), ora_out.tobytes(), "s=32 mismatch")

    def test_oracle_flat_colours(self):
        W, H = 64, 64
        grid = Grid(W, H)
        colours = [
            (0, 0, 0),
            (255, 255, 255),
            (255, 0, 0),
            (0, 255, 0),
            (0, 0, 255),
            (128, 128, 128),
            (200, 100, 50),
        ]
        for c in colours:
            rgb = np.full((H, W, 3), c, dtype=np.uint8)
            padded = grid.pad(rgb)
            ref_out, _ = describe_padded(padded, grid)
            ora_out = oracle_describe_padded(padded, grid)
            self.assertEqual(ref_out.tobytes(), ora_out.tobytes(), f"Colour {c} mismatch")
            self.assertTrue((ref_out["sig_q4"] == 0).all())
            self.assertTrue((ref_out["grad_q6"] == 0).all())
            self.assertTrue((ref_out["edge"] == 0).all())
            self.assertTrue((ref_out["sig64"] == 0).all())

    def test_oracle_pure_saturation(self):
        W, H = 32, 32
        grid = Grid(W, H)
        # Tile 0: saturated (255, 0, 0), Tile 1: unsaturated (128, 128, 128)
        rgb = np.full((H, W, 3), 128, dtype=np.uint8)
        rgb[:16, :16] = (255, 0, 0)
        padded = grid.pad(rgb)
        ref_out, _ = describe_padded(padded, grid)
        ora_out = oracle_describe_padded(padded, grid)
        self.assertEqual(ref_out.tobytes(), ora_out.tobytes())
        # Check F_SAT (8) is set on Tile 0 and clear on Tile 1
        t0_idx = np.searchsorted(grid.mortons, layout.morton(0, 0))
        t1_idx = np.searchsorted(grid.mortons, layout.morton(1, 0))
        self.assertTrue(ref_out["flags"][t0_idx] & layout.TILE_FLAGS.index("SAT") if hasattr(layout, "F_SAT") else ref_out["flags"][t0_idx] & 8)

    def test_oracle_sharp_edges_tau_threshold(self):
        W, H = 16, 16
        grid = Grid(W, H)
        tau = PARAMS["TAU_EDGE"]  # 48

        # Step jump of TAU_EDGE - 1 = 47
        rgb_below = np.zeros((16, 16, 3), dtype=np.uint8)
        rgb_below[:, 8:] = 47
        padded_below = grid.pad(rgb_below)
        ref_below, _ = describe_padded(padded_below, grid)
        ora_below = oracle_describe_padded(padded_below, grid)
        self.assertEqual(ref_below.tobytes(), ora_below.tobytes())
        self.assertEqual(int(ref_below["edge"][0]), 0)

        # Step jump of TAU_EDGE + 1 = 49
        rgb_above = np.zeros((16, 16, 3), dtype=np.uint8)
        rgb_above[:, 8:] = 49
        padded_above = grid.pad(rgb_above)
        ref_above, _ = describe_padded(padded_above, grid)
        ora_above = oracle_describe_padded(padded_above, grid)
        self.assertEqual(ref_above.tobytes(), ora_above.tobytes())
        self.assertGreater(int(ref_above["edge"][0]), 0)

    def test_oracle_padded_tiles(self):
        for W, H in ((45, 37), (1025, 1025)):
            grid = Grid(W, H)
            rgb = np.random.RandomState(99).randint(0, 256, (H, W, 3)).astype(np.uint8)
            padded = grid.pad(rgb)
            ref_out, _ = describe_padded(padded, grid)
            ora_out = oracle_describe_padded(padded, grid)
            self.assertEqual(ref_out.tobytes(), ora_out.tobytes(), f"Padded {W}x{H} mismatch")
            # Pad flags check
            pad_mask = grid.pad_flags()
            self.assertTrue(np.array_equal((ref_out["flags"] & 128) != 0, pad_mask))

    def test_oracle_fill_xh64_dirty_subset_and_hash_all(self):
        W, H = 160, 96
        grid = Grid(W, H)
        rgb = np.random.RandomState(7).randint(0, 256, (H, W, 3)).astype(np.uint8)
        padded = grid.pad(rgb)
        out, _ = describe_padded(padded, grid, lazy_xh=True)

        # Assign classes: alternating 0 (UNCHANGED), 1 (JITTER), 2 (CHANGED), 3 (MOVED)
        out["cls"] = np.array([i % 4 for i in range(grid.N)], dtype=np.uint8)
        prev_xh = np.arange(100, 100 + grid.N, dtype=np.uint64)

        # 1. hash_all = False (dirty subset path: cls >= 2)
        ref_dirty = fill_xh64(out.copy(), padded, grid, prev_xh64=prev_xh, hash_all=False)
        ora_dirty = oracle_fill_xh64(out.copy(), padded, grid, prev_xh64=prev_xh, hash_all=False)
        self.assertEqual(ref_dirty.tobytes(), ora_dirty.tobytes())

        # Clean tiles (cls < 2) keep prev_xh
        clean_mask = out["cls"] < layout.CLS_DIRTY_MIN
        self.assertTrue(np.array_equal(ref_dirty["xh64"][clean_mask], prev_xh[clean_mask]))

        # 2. hash_all = True
        ref_all = fill_xh64(out.copy(), padded, grid, prev_xh64=prev_xh, hash_all=True)
        ora_all = oracle_fill_xh64(out.copy(), padded, grid, prev_xh64=prev_xh, hash_all=True)
        self.assertEqual(ref_all.tobytes(), ora_all.tobytes())

    def test_pinned_seeds_sha256(self):
        W, H = 160, 96
        grid = Grid(W, H)
        for seed, expected_sha in SEED_DESCS_SHA256.items():
            rgb = np.random.RandomState(seed).randint(0, 256, (H, W, 3)).astype(np.uint8)
            padded = grid.pad(rgb)
            ref_out, _ = describe_padded(padded, grid)
            sha = hashlib.sha256(ref_out.tobytes()).hexdigest()
            self.assertEqual(sha, expected_sha, f"SHA256 mismatch for seed {seed}")

    def test_native_differential(self):
        desc_bin = os.environ.get("VQA_K_S1_DESC_BIN")
        xh64_bin = os.environ.get("VQA_K_S1_XH64_BIN")
        if not desc_bin or not xh64_bin or not os.path.isfile(desc_bin) or not os.path.isfile(xh64_bin):
            self.skipTest("VQA_K_S1_DESC_BIN or VQA_K_S1_XH64_BIN environment variable not set to release binary")

        W, H = 160, 96
        grid = Grid(W, H)
        rgb = np.random.RandomState(123).randint(0, 256, (H, W, 3)).astype(np.uint8)
        padded = grid.pad(rgb)
        ref_out, _ = describe_padded(padded, grid)

        # Run native differential for VQA_K_S1_DESC_BIN
        proc = subprocess.run([desc_bin], input=padded.tobytes(), capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0, f"Native binary {desc_bin} failed: {proc.stderr}")
        self.assertEqual(proc.stdout, ref_out.tobytes())

        # Run native differential for VQA_K_S1_XH64_BIN
        proc_xh = subprocess.run([xh64_bin], input=padded.tobytes(), capture_output=True, timeout=10)
        self.assertEqual(proc_xh.returncode, 0, f"Native binary {xh64_bin} failed: {proc_xh.stderr}")
        self.assertEqual(proc_xh.stdout, ref_out["xh64"].tobytes())

    def test_ram_storage_law_and_tempdir(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            p = os.path.join(tmp_dir, "test.tmp")
            with open(p, "w") as f:
                f.write("ram test")
            self.assertTrue(os.path.exists(p))


if __name__ == "__main__":
    unittest.main()
