"""S1 descriptor tests (SPEC_CORE §2): dtype, isqrt, YCoCg, hand-computed tiles, determinism, xh64 carry-forward."""
import hashlib
import math
import unittest

import numpy as np

from visioncore import layout
from visioncore.desc import TILE_DTYPE, describe, describe_padded, isqrt, xh64_of, ycocg
from visioncore.errors import VqaError
from visioncore.grid import Grid


def noise(W, H, seed):
    return np.random.RandomState(seed).randint(0, 256, (H, W, 3)).astype(np.uint8)


def ref_tile(rgb, tx, ty, s, W, H, tau=48):
    """Independent pure-Python reference for one tile (edge-replicated), straight from SPEC_CORE §2."""
    px = [[tuple(int(c) for c in rgb[min(ty * s + y, H - 1), min(tx * s + x, W - 1)]) for x in range(s)] for y in range(s)]
    Y = [[0] * s for _ in range(s)]
    sco = scg = sy = sy2 = 0
    for y in range(s):
        for x in range(s):
            r, g, b = px[y][x]
            co = r - b
            t = b + (co >> 1)
            cg = g - t
            yy = t + (cg >> 1)
            Y[y][x] = yy
            sco += co
            scg += cg
            sy += yy
            sy2 += yy * yy
    n = s * s
    G = e = sat = 0
    for y in range(s):
        for x in range(s):
            d = (abs(Y[y][x + 1] - Y[y][x]) if x + 1 < s else 0) + (abs(Y[y + 1][x] - Y[y][x]) if y + 1 < s else 0)
            G += d
            e += d > tau
            sat += any(c in (0, 255) for c in px[y][x])
    b = s // 8
    sig = 0
    for i in range(8):
        for j in range(8):
            sub = sum(Y[i * b + yy][j * b + xx] for yy in range(b) for xx in range(b))
            if sub * n > sy * b * b:
                sig |= 1 << (8 * i + j)
    flags = (1 if 2 * e >= s and e < 4 * s else 0) | (2 if e >= 4 * s else 0) | (8 if 4 * sat >= n else 0) | \
        (128 if (tx + 1) * s > W or (ty + 1) * s > H else 0)
    blob = bytes(v for y in range(s) for x in range(s) for v in px[y][x])
    return dict(flags=flags, y_q4=16 * sy // n, co_h=max(-128, min(127, sco // (2 * n))),
                cg_h=max(-128, min(127, scg // (2 * n))), sig_q4=16 * math.isqrt(n * sy2 - sy * sy) // n,
                grad_q6=64 * G // n, edge=255 * e // n, sig64=sig,
                xh64=int.from_bytes(hashlib.blake2b(blob, digest_size=8, person=b"vqa-tile").digest(), "little"))


class TestDesc(unittest.TestCase):
    def test_dtype_layout(self):
        self.assertEqual(TILE_DTYPE.itemsize, 32)
        self.assertEqual([TILE_DTYPE.fields[n][1] for n in layout.TILE_FIELDS], list(layout.TILE_OFFSETS))
        rec = np.zeros(1, dtype=TILE_DTYPE)
        rec["morton"], rec["cls"], rec["co_h"], rec["sig64"], rec["xh64"] = 0x1234, 3, -5, 2 ** 63 + 1, 2 ** 64 - 1
        self.assertEqual(layout.unpack_tile(rec.tobytes()), {
            **dict.fromkeys(layout.TILE_FIELDS, 0), "morton": 0x1234, "cls": 3, "co_h": -5,
            "sig64": 2 ** 63 + 1, "xh64": 2 ** 64 - 1})

    def test_isqrt_exact(self):
        rs = np.random.RandomState(1)
        v = np.concatenate([rs.randint(0, 2 ** 52, 100000, dtype=np.int64)])
        self.assertEqual(isqrt(v).tolist(), [math.isqrt(int(x)) for x in v])
        edges = []
        for k in (1, 2, 1000, 12345, 2 ** 25, 2 ** 26 - 1):
            edges += [k * k - 1, k * k, k * k + 1]
        edges = [e for e in edges if 0 <= e < 2 ** 52]
        self.assertEqual(isqrt(np.array(edges, dtype=np.int64)).tolist(), [math.isqrt(e) for e in edges])

    def test_ycocg_lossless(self):
        rs = np.random.RandomState(2)
        rgb = rs.randint(0, 256, (1000, 1000, 3)).astype(np.uint8)
        y, co, cg = ycocg(rgb)
        t = y - (cg >> 1)
        g = cg + t
        b = t - (co >> 1)
        r = b + co
        self.assertTrue(np.array_equal(np.stack([r, g, b], -1), rgb.astype(np.int16)))
        self.assertTrue(((y >= 0) & (y <= 255)).all())

    def test_hand_computed_flat_tile(self):
        g = Grid(32, 32)  # s=16, 2x2
        rgb = np.zeros((32, 32, 3), np.uint8)
        rgb[:] = (200, 100, 50)
        t, eq = describe(rgb, g)
        y, co, cg = ycocg(np.array([[[200, 100, 50]]], np.uint8))
        self.assertEqual(t.shape, (4,))
        self.assertTrue((t["y_q4"] == 16 * int(y[0, 0])).all())
        self.assertTrue((t["co_h"] == int(co[0, 0]) // 2).all())
        self.assertTrue((t["cg_h"] == int(cg[0, 0]) // 2).all())
        for f in ("sig_q4", "grad_q6", "edge", "sig64", "cls", "resid"):
            self.assertTrue((t[f] == 0).all(), f)
        self.assertEqual(t["flags"].tolist(), [0] * 4)
        self.assertFalse(eq.any())
        self.assertEqual(int(t["xh64"][0]), xh64_of(rgb[:16, :16]))

    def test_negative_chroma_floor(self):
        g = Grid(16, 16)
        rgb = np.zeros((16, 16, 3), np.uint8)
        rgb[:] = (0, 0, 255)  # Co = -255 -> floor(-255/2) = -128 (clamp), not trunc -127
        t, _ = describe(rgb, g)
        self.assertEqual(int(t["co_h"][0]), -128)
        rgb[:] = (0, 0, 3)  # Co=-3 -> -3//2 = -2 (floor); truncation would give -1
        t, _ = describe(rgb, g)
        self.assertEqual(int(t["co_h"][0]), -2)

    def test_sigma_and_sig64_checker(self):
        g = Grid(16, 16)
        rgb = np.zeros((16, 16, 3), np.uint8)
        rgb[:, 8:] = 255  # left half black, right half white: sigma_Y = 127.5
        t, _ = describe(rgb, g)
        self.assertEqual(int(t["sig_q4"][0]), 2040)  # 16*isqrt(n*S2 - S^2)//n = 16*127.5
        # sub-blocks 2x2 px; bit 8i+j set iff block brighter than mean -> right 4 columns of each row
        want = sum(1 << (8 * i + j) for i in range(8) for j in range(4, 8))
        self.assertEqual(int(t["sig64"][0]), want)
        self.assertEqual(int(t["edge"][0]), 255 * 16 // 256)  # one jump column x 16 rows
        self.assertTrue(int(t["flags"][0]) & 1)  # EDGE: s/2 <= e < 4s (16 px)

    def test_flags_texture_sat_pad(self):
        g = Grid(36, 36)  # s=16, 3x3 cells; only cells with tx==2 or ty==2 are padded
        rgb = noise(36, 36, 3)
        rgb[:16, :16] = 255  # saturated tile 0
        t, _ = describe(rgb, g)
        f = {(int(x), int(y)): int(v) for x, y, v in zip(g.tx, g.ty, t["flags"])}
        self.assertTrue(f[(0, 0)] & 8)  # SAT
        self.assertFalse(f[(0, 0)] & 128)
        self.assertTrue(f[(1, 1)] & 2)  # TEXTURE (noise)
        self.assertFalse(f[(1, 1)] & 128)
        for k, v in f.items():
            self.assertEqual(bool(v & 128), 2 in k, k)

    def test_unch_exact_one_lsb(self):
        g = Grid(160, 96)
        a = noise(160, 96, 4)
        b = a.copy()
        b[40, 70, 1] ^= 1
        pa = g.pad(a)
        ta, _ = describe_padded(pa, g)
        tb, eq = describe_padded(g.pad(b), g, pa, ta["xh64"])
        self.assertEqual(int((~eq).sum()), 1)
        k = int(np.nonzero(~eq)[0][0])
        self.assertEqual((int(g.tx[k]), int(g.ty[k])), (70 // 16, 40 // 16))
        self.assertNotEqual(int(tb["xh64"][k]), int(ta["xh64"][k]))

    def test_xh64_carry_forward_valid_all_frames(self):
        g = Grid(160, 96)
        a = noise(160, 96, 5)
        b = a.copy()
        b[0:20, 0:20] = noise(20, 20, 6)
        pa, pb = g.pad(a), g.pad(b)
        ta, _ = describe_padded(pa, g)
        tb, eq = describe_padded(pb, g, pa, ta["xh64"])
        full, _ = describe_padded(pb, g)  # recomputed from bytes, no carry
        self.assertTrue(np.array_equal(tb["xh64"], full["xh64"]))
        self.assertTrue(np.array_equal(tb["xh64"][eq], ta["xh64"][eq]))
        self.assertTrue((tb["xh64"] != 0).all())

    def test_hash_is_blake2b(self):
        blk = noise(16, 16, 7)
        want = int.from_bytes(hashlib.blake2b(blk.tobytes(), digest_size=8, person=b"vqa-tile").digest(), "little")
        self.assertEqual(xh64_of(blk), want)

    def test_matches_pure_python_reference(self):
        for W, H, seed in ((45, 37, 11), (64, 48, 12)):
            rgb = noise(W, H, seed)
            rgb[:16, :16] = 0  # smooth + saturated corner
            rgb[16:32, :16, :] = (rgb[16:32, :16, :] // 64) * 64
            g = Grid(W, H)
            t, _ = describe(rgb, g)
            for k in range(g.N):
                want = ref_tile(rgb, int(g.tx[k]), int(g.ty[k]), g.s, W, H)
                for f, v in want.items():
                    self.assertEqual(int(t[f][k]), v, (W, H, k, f))

    def test_determinism_and_golden(self):
        g = Grid(200, 120)
        a = noise(200, 120, 8)
        runs = [describe(a, g)[0].tobytes() for _ in range(3)]
        self.assertEqual(runs[0], runs[1])
        self.assertEqual(runs[1], runs[2])
        self.assertEqual(hashlib.sha256(runs[0]).hexdigest(), GOLDEN_SHA)

    def test_bad_frame(self):
        g = Grid(64, 64)
        for bad in (np.zeros((64, 63, 3), np.uint8), np.zeros((64, 64, 3), np.int16)):
            with self.assertRaises(VqaError) as c:
                describe(bad, g)
            self.assertEqual(c.exception.code, "E_VC_FRAME_SHAPE")


GOLDEN_SHA = "2d25eecf22ed79e4e9e26f404abea4b4492b7601aa9d1a6810c96869e4de2833"

if __name__ == "__main__":
    unittest.main()
