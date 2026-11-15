"""Unit tests for LANG-X Experiment X1 py_w1 implementation."""

import json
import os
import random
import unittest
import numpy as np

from langx.x1.py_w1.x1 import (
    repack_bgrx_to_rgb,
    damage_tiles,
    X1Error,
    E_DIM,
    E_STRIDE,
    E_SHORT,
    E_RECT,
    E_GRID,
)


def splitmix64(seed: int):
    """Generator implementing SplitMix64 pseudo-random generator."""
    state = seed & 0xFFFFFFFFFFFFFFFF
    while True:
        state = (state + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
        z = state
        z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9 & 0xFFFFFFFFFFFFFFFF
        z = (z ^ (z >> 27)) * 0x94D049BB133111EB & 0xFFFFFFFFFFFFFFFF
        z = (z ^ (z >> 31)) & 0xFFFFFFFFFFFFFFFF
        yield z


def splitmix64_bytes(seed: int, length: int) -> bytes:
    """Generate pseudo-random buffer bytes using SplitMix64."""
    gen = splitmix64(seed)
    out = bytearray()
    while len(out) < length:
        val = next(gen)
        out.extend(val.to_bytes(8, "little"))
    return bytes(out[:length])


def fnv1a_64(data: bytes) -> int:
    """Compute FNV-1a 64 hash over data bytes."""
    h = 0xCBF29CE484222325
    prime = 0x100000001B3
    for b in data:
        h = h ^ b
        h = (h * prime) & 0xFFFFFFFFFFFFFFFF
    return h


class TestX1HandComputed(unittest.TestCase):
    """Hand-computed unit tests that always run."""

    def test_repack_2x2_padded(self):
        """2x2 repack with padded strides and untouched destination padding."""
        w, h = 2, 2
        src_stride = 12  # 4 * 2 + 4 padding
        dst_stride = 10  # 3 * 2 + 4 padding

        # Row 0: Pixel (0,0)=B1,G1,R1,X1; Pixel (0,1)=B2,G2,R2,X2; Pad=FF,FF,FF,FF
        # Row 1: Pixel (1,0)=B3,G3,R3,X3; Pixel (1,1)=B4,G4,R4,X4; Pad=EE,EE,EE,EE
        src = bytearray([
            10, 20, 30, 255,  40, 50, 60, 255,  0xFF, 0xFF, 0xFF, 0xFF,
            70, 80, 90, 255,  11, 22, 33, 255,  0xEE, 0xEE, 0xEE, 0xEE,
        ])

        # Destination buffer initialized with sentinel value 0xAA
        dst = bytearray([0xAA] * (h * dst_stride))

        repack_bgrx_to_rgb(src, w, h, src_stride, dst, dst_stride)

        # Expected Row 0: R1,G1,B1 (30,20,10), R2,G2,B2 (60,50,40), Pad (AA,AA,AA,AA)
        expected_row0 = [30, 20, 10, 60, 50, 40, 0xAA, 0xAA, 0xAA, 0xAA]
        # Expected Row 1: R3,G3,B3 (90,80,70), R4,G4,B4 (33,22,11), Pad (AA,AA,AA,AA)
        expected_row1 = [90, 80, 70, 33, 22, 11, 0xAA, 0xAA, 0xAA, 0xAA]

        self.assertEqual(list(dst[:dst_stride]), expected_row0)
        self.assertEqual(list(dst[dst_stride:2 * dst_stride]), expected_row1)

    def test_repack_numpy_dst(self):
        """Test repack_bgrx_to_rgb where dst is a numpy array."""
        w, h = 2, 1
        src = bytearray([1, 2, 3, 0, 4, 5, 6, 0])
        dst = np.zeros((1, 8), dtype=np.uint8)
        repack_bgrx_to_rgb(src, w, h, 8, dst, 8)
        self.assertEqual(list(dst[0, :6]), [3, 2, 1, 6, 5, 4])
        self.assertEqual(list(dst[0, 6:]), [0, 0])

    def test_repack_zero_dimensions(self):
        """W or H equal to 0 is valid and writes nothing."""
        dst = bytearray([0xAA] * 10)
        repack_bgrx_to_rgb(b"", 0, 10, 0, dst, 10)
        self.assertEqual(list(dst), [0xAA] * 10)

        repack_bgrx_to_rgb(b"", 10, 0, 40, dst, 30)
        self.assertEqual(list(dst), [0xAA] * 10)

    def test_errors_repack(self):
        """Verify error codes for repack_bgrx_to_rgb."""
        dst = bytearray(100)

        # E_DIM
        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(b"", -1, 10, 40, dst, 30)
        self.assertEqual(cm.exception.code, E_DIM)

        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(b"", 10, 16385, 40, dst, 30)
        self.assertEqual(cm.exception.code, E_DIM)

        # E_STRIDE
        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(b"", 10, 10, 39, dst, 30)
        self.assertEqual(cm.exception.code, E_STRIDE)

        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(b"", 10, 10, 40, dst, 29)
        self.assertEqual(cm.exception.code, E_STRIDE)

        # E_SHORT
        src_short = bytes(39)  # Needed: (1-1)*40 + 4*10 = 40
        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(src_short, 10, 1, 40, dst, 30)
        self.assertEqual(cm.exception.code, E_SHORT)

        dst_short = bytearray(29)  # Needed: (1-1)*30 + 3*10 = 30
        src_ok = bytes(40)
        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(src_ok, 10, 1, 40, dst_short, 30)
        self.assertEqual(cm.exception.code, E_SHORT)

    def test_errors_damage(self):
        """Verify error codes for damage_tiles."""
        rects = [(0, 0, 10, 10)]

        # E_DIM
        with self.assertRaises(X1Error) as cm:
            damage_tiles(0, 100, 16, rects)
        self.assertEqual(cm.exception.code, E_DIM)

        with self.assertRaises(X1Error) as cm:
            damage_tiles(100, 16385, 16, rects)
        self.assertEqual(cm.exception.code, E_DIM)

        # E_GRID
        with self.assertRaises(X1Error) as cm:
            damage_tiles(100, 100, 0, rects)
        self.assertEqual(cm.exception.code, E_GRID)

        with self.assertRaises(X1Error) as cm:
            damage_tiles(100, 100, 3, rects)
        self.assertEqual(cm.exception.code, E_GRID)

        with self.assertRaises(X1Error) as cm:
            damage_tiles(100, 100, 512, rects)
        self.assertEqual(cm.exception.code, E_GRID)

        # E_RECT
        invalid_rects = [(0, 0, -1, 10)]
        with self.assertRaises(X1Error) as cm:
            damage_tiles(100, 100, 16, invalid_rects)
        self.assertEqual(cm.exception.code, E_RECT)

    def test_damage_worked_check(self):
        """Worked check: W 1920, H 1080, s 32 gives gw 60, gh 34, 2040 tiles and 32 words;

        a 192x32 rectangle at x 96, y 0 sets tiles 3 to 8 of row 0.
        """
        words, popcount = damage_tiles(1920, 1080, 32, [(96, 0, 192, 32)])
        self.assertEqual(len(words), 32)
        self.assertEqual(popcount, 6)

        # Bits set: 3, 4, 5, 6, 7, 8 in word 0 -> 0x1F8
        self.assertEqual(words[0], 0x1F8)
        for w in words[1:]:
            self.assertEqual(w, 0)

    def test_damage_zero_width_rect(self):
        """A rectangle with clipped x1 <= x0 or y1 <= y0 is skipped and sets no bit."""
        words, popcount = damage_tiles(100, 100, 16, [(33, 10, 0, 10)])
        self.assertEqual(popcount, 0)
        self.assertTrue(all(w == 0 for w in words))

    def test_damage_partial_bottom_row(self):
        """A rectangle crossing the partial bottom row."""
        # W 100, H 50, s 32 -> gw = ceil(100/32)=4, gh = ceil(50/32)=2.
        # Partial bottom row is row 1 (y from 32 to 50).
        # Rect at x=0, y=40, w=32, h=10 -> x0=0, y0=40, x1=32, y1=50.
        # tx: 0..1, ty: 1..2. Tile index t = 1*4 + 0 = 4.
        words, popcount = damage_tiles(100, 50, 32, [(0, 40, 32, 10)])
        self.assertEqual(popcount, 1)
        self.assertEqual(words[0], 1 << 4)

    def test_splitmix64_and_fnv1a_pinned(self):
        """Test SplitMix64 and FNV-1a 64 against pinned values."""
        gen = splitmix64(12345)
        v1 = next(gen)
        v2 = next(gen)
        self.assertEqual(v1, 0x22118258A9D111A0)
        self.assertEqual(v2, 0x346EDCE5F713F8ED)

        buf = splitmix64_bytes(12345, 16)
        h = fnv1a_64(buf)
        self.assertEqual(h, 0x1C9D6F44D1E669C9)

    def test_damage_numpy_vs_loop_comparison(self):
        """Compare numpy path vs plain-loop path on 200 random rectangle lists from a fixed seed."""
        rng = random.Random(0xDEADBEEF)

        for _ in range(200):
            w = rng.randint(1, 1920)
            h = rng.randint(1, 1080)
            s = 1 << rng.randint(0, 8)  # 1, 2, 4, 8, 16, 32, 64, 128, 256
            num_rects = rng.randint(0, 20)
            rects = []
            for _ in range(num_rects):
                rx = rng.randint(-100, w + 100)
                ry = rng.randint(-100, h + 100)
                rw = rng.randint(0, 500)
                rh = rng.randint(0, 500)
                rects.append((rx, ry, rw, rh))

            res_np, pop_np = damage_tiles(w, h, s, rects, use_numpy_path=True)
            res_loop, pop_loop = damage_tiles(w, h, s, rects, use_numpy_path=False)

            self.assertEqual(res_np, res_loop)
            self.assertEqual(pop_np, pop_loop)


class TestX1Fixtures(unittest.TestCase):
    """Fixture tests reading langx/x1/fixtures if available."""

    def test_fixtures(self):
        fixtures_dir = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "fixtures")
        )

        repack_cases_path = os.path.join(fixtures_dir, "repack_cases.json")
        damage_cases_path = os.path.join(fixtures_dir, "damage_cases.json")

        if not os.path.isdir(fixtures_dir) or not os.path.exists(repack_cases_path):
            self.skipTest("fixtures absent at langx/x1/fixtures")

        # Test repack fixtures
        with open(repack_cases_path, "r", encoding="utf-8") as f:
            repack_cases = json.load(f)

        repack_src_path = os.path.join(fixtures_dir, "repack_src.bin")
        repack_dst_path = os.path.join(fixtures_dir, "repack_dst.bin")

        has_bin_files = os.path.exists(repack_src_path) and os.path.exists(repack_dst_path)
        if has_bin_files:
            with open(repack_src_path, "rb") as f:
                src_bin = f.read()
            with open(repack_dst_path, "rb") as f:
                dst_bin = f.read()

        for case in repack_cases:
            w = case["w"]
            h = case["h"]
            src_stride = case["src_stride"]
            dst_stride = case["dst_stride"]

            if "seed" in case and "fnv1a" in case:
                seed = case["seed"]
                expected_fnv = int(case["fnv1a"], 16)
                src_len = (h - 1) * src_stride + 4 * w if h > 0 else 0
                dst_len = (h - 1) * dst_stride + 3 * w if h > 0 else 0

                src_data = splitmix64_bytes(seed, src_len)
                dst_buf = bytearray(dst_len)
                repack_bgrx_to_rgb(src_data, w, h, src_stride, dst_buf, dst_stride)
                act_fnv = fnv1a_64(bytes(dst_buf))
                self.assertEqual(act_fnv, expected_fnv)
            elif has_bin_files and "src_offset" in case:
                src_offset = case["src_offset"]
                src_len = case["src_len"]
                dst_offset = case["dst_offset"]
                dst_len = case["dst_len"]

                src_sub = src_bin[src_offset : src_offset + src_len]
                dst_buf = bytearray(dst_len)
                repack_bgrx_to_rgb(src_sub, w, h, src_stride, dst_buf, dst_stride)

                expected_dst = dst_bin[dst_offset : dst_offset + dst_len]
                self.assertEqual(bytes(dst_buf), expected_dst)

        # Test damage fixtures
        if os.path.exists(damage_cases_path):
            with open(damage_cases_path, "r", encoding="utf-8") as f:
                damage_cases = json.load(f)

            for case in damage_cases:
                w = case["w"]
                h = case["h"]
                s = case["s"]
                rects = case["rects"]
                expected_words_hex = case["words"]  # list of 16-digit lowercase hex strings
                expected_popcount = case["popcount"]

                expected_words = [int(x, 16) for x in expected_words_hex]

                words, popcount = damage_tiles(w, h, s, rects)
                self.assertEqual(words, expected_words)
                self.assertEqual(popcount, expected_popcount)


if __name__ == "__main__":
    unittest.main()
