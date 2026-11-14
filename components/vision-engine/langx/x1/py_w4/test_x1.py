"""Unit tests and fixture conformance tests for LANG-X Experiment X1 (py_w4)."""

import json
import os
import random
import unittest

import numpy as np

from langx.x1.py_w4.x1 import (
    X1Error,
    damage_tiles,
    damage_tiles_numpy,
    damage_tiles_plain,
    repack_bgrx_to_rgb,
)


def splitmix64(seed: int, num_bytes: int) -> bytes:
    state = seed & 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    MASK = 0xFFFFFFFFFFFFFFFF
    while len(out) < num_bytes:
        state = (state + 0x9E3779B97F4A7C15) & MASK
        z = state
        z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9 & MASK
        z = (z ^ (z >> 27)) * 0x94D049BB133111EB & MASK
        z = (z ^ (z >> 31)) & MASK
        out.extend(z.to_bytes(8, "little"))
    return bytes(out[:num_bytes])


def fnv1a64(data: bytes) -> int:
    hash_val = 0xCBF29CE484222325
    prime = 0x100000001B3
    MASK = 0xFFFFFFFFFFFFFFFF
    for b in data:
        hash_val = ((hash_val ^ b) * prime) & MASK
    return hash_val


class TestX1HandComputed(unittest.TestCase):
    """Hand-computed unit tests that always run."""

    def test_splitmix64_and_fnv1a64_pinned(self):
        """Test SplitMix64 and FNV-1a 64 against pinned values."""
        sm_bytes = splitmix64(12345, 16)
        expected_hex = "a011d1a958821122edf813f7e5dc6e34"
        self.assertEqual(sm_bytes.hex(), expected_hex)

        hash_val = fnv1a64(sm_bytes)
        expected_hash = 0x1C9D6F44D1E669C9
        self.assertEqual(hash_val, expected_hash)

    def test_repack_2x2_padded(self):
        """2x2 repack with padded strides and untouched destination padding."""
        # W=2, H=2. src_stride=12 (4*2 + 4 padding), dst_stride=8 (3*2 + 2 padding)
        # Pixel (0,0): B=10, G=20, R=30, X=99 -> RGB: 30, 20, 10
        # Pixel (0,1): B=40, G=50, R=60, X=99 -> RGB: 60, 50, 40
        # Row 0 padding in src: bytes 8, 9, 10, 11
        # Row 0 padding in dst: bytes 6, 7 (should remain untouched 0xFF)
        src = bytearray([
            10, 20, 30, 99, 40, 50, 60, 99, 0xAA, 0xAA, 0xAA, 0xAA,
            11, 21, 31, 99, 41, 51, 61, 99, 0xBB, 0xBB, 0xBB, 0xBB,
        ])
        dst = bytearray([0xFF] * 16)

        repack_bgrx_to_rgb(src, 2, 2, 12, dst, 8)

        # Expected dst:
        # Row 0: 30, 20, 10, 60, 50, 40, 0xFF, 0xFF
        # Row 1: 31, 21, 11, 61, 51, 41, 0xFF, 0xFF
        expected_dst = bytearray([
            30, 20, 10, 60, 50, 40, 0xFF, 0xFF,
            31, 21, 11, 61, 51, 41, 0xFF, 0xFF,
        ])
        self.assertEqual(dst, expected_dst)

    def test_repack_zero_dim(self):
        """W or H equal to 0 writes nothing."""
        dst = bytearray([0xEE] * 10)
        repack_bgrx_to_rgb(b"", 0, 10, 0, dst, 0)
        self.assertEqual(dst, bytearray([0xEE] * 10))

        repack_bgrx_to_rgb(b"", 10, 0, 40, dst, 30)
        self.assertEqual(dst, bytearray([0xEE] * 10))

    def test_errors_e_dim(self):
        """Test E_DIM error code."""
        dst = bytearray(100)
        # K1 E_DIM: W or H outside 0 to 16384
        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(b"", 16385, 1, 16385 * 4, dst, 16385 * 3)
        self.assertEqual(cm.exception.code, "E_DIM")

        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(b"", -1, 1, 4, dst, 3)
        self.assertEqual(cm.exception.code, "E_DIM")

        # K2 E_DIM: W or H outside 1 to 16384
        with self.assertRaises(X1Error) as cm:
            damage_tiles(0, 100, 32, [])
        self.assertEqual(cm.exception.code, "E_DIM")

        with self.assertRaises(X1Error) as cm:
            damage_tiles(100, 16385, 32, [])
        self.assertEqual(cm.exception.code, "E_DIM")

    def test_errors_e_stride(self):
        """Test E_STRIDE error code."""
        dst = bytearray(100)
        # src_stride < 4W
        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(bytes(16), 2, 2, 7, dst, 6)
        self.assertEqual(cm.exception.code, "E_STRIDE")

        # dst_stride < 3W
        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(bytes(16), 2, 2, 8, dst, 5)
        self.assertEqual(cm.exception.code, "E_STRIDE")

    def test_errors_e_short(self):
        """Test E_SHORT error code."""
        dst = bytearray(10)
        # Short src buffer (need (2-1)*8 + 8 = 16 bytes)
        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(bytes(15), 2, 2, 8, dst, 8)
        self.assertEqual(cm.exception.code, "E_SHORT")

        # Short dst buffer (need (2-1)*6 + 6 = 12 bytes)
        src = bytes(16)
        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(src, 2, 2, 8, dst, 6)
        self.assertEqual(cm.exception.code, "E_SHORT")

    def test_errors_e_rect(self):
        """Test E_RECT error code in damage_tiles."""
        with self.assertRaises(X1Error) as cm:
            damage_tiles(100, 100, 32, [(0, 0, -1, 10)])
        self.assertEqual(cm.exception.code, "E_RECT")

        with self.assertRaises(X1Error) as cm:
            damage_tiles(100, 100, 32, [(0, 0, 10, -5)])
        self.assertEqual(cm.exception.code, "E_RECT")

    def test_errors_e_grid(self):
        """Test E_GRID error code in damage_tiles."""
        # s not a power of 2 or not in 1..256
        with self.assertRaises(X1Error) as cm:
            damage_tiles(100, 100, 3, [])
        self.assertEqual(cm.exception.code, "E_GRID")

        with self.assertRaises(X1Error) as cm:
            damage_tiles(100, 100, 0, [])
        self.assertEqual(cm.exception.code, "E_GRID")

        with self.assertRaises(X1Error) as cm:
            damage_tiles(100, 100, 512, [])
        self.assertEqual(cm.exception.code, "E_GRID")

    def test_worked_check_1920_1080_s32(self):
        """Worked check from spec: W 1920, H 1080, s 32, rect 192x32 at x 96, y 0."""
        # W 1920, H 1080, s 32 gives gw 60, gh 34, 2040 tiles and 32 words
        # 192x32 rectangle at x 96, y 0 sets tiles 3 to 8 of row 0
        rects = [(96, 0, 192, 32)]
        words, popcount = damage_tiles(1920, 1080, 32, rects)

        self.assertEqual(len(words), 32)
        self.assertEqual(popcount, 6)
        # Tiles 3, 4, 5, 6, 7, 8 in row 0 -> bits 3,4,5,6,7,8 = (1<<3)|(1<<4)|(1<<5)|(1<<6)|(1<<7)|(1<<8) = 504
        self.assertEqual(words[0], 504)
        for w in words[1:]:
            self.assertEqual(w, 0)

    def test_x33_w0_setting_no_bit(self):
        """Rectangle x 33, w 0 sets no bit."""
        words, popcount = damage_tiles(100, 100, 32, [(33, 10, 0, 20)])
        self.assertEqual(popcount, 0)
        self.assertTrue(all(w == 0 for w in words))

    def test_rectangle_crossing_partial_bottom_row(self):
        """A rectangle crossing into the partial bottom/right edge tile."""
        # W=100, H=100, s=64 -> gw=2 (tile 0: 0..63, tile 1: 64..99), gh=2 (tile 0: 0..63, tile 1: 64..99)
        # Rect at x=50, y=50, w=30, h=30 -> x0=50, x1=80 (tx0=0, tx1=2); y0=50, y1=80 (ty0=0, ty1=2)
        # Sets tiles: (ty=0, tx=0)->t=0, (ty=0, tx=1)->t=1, (ty=1, tx=0)->t=2, (ty=1, tx=1)->t=3.
        words, popcount = damage_tiles(100, 100, 64, [(50, 50, 30, 30)])
        self.assertEqual(popcount, 4)
        self.assertEqual(words[0], 0b1111)


class TestX1Comparison(unittest.TestCase):
    """Compare numpy path with plain loop path on 200 random rectangle lists."""

    def test_numpy_vs_plain_loop_random(self):
        rng = random.Random(0x12345678)
        s_choices = [1, 2, 4, 8, 16, 32, 64, 128, 256]

        for case_idx in range(200):
            w = rng.randint(1, 1000)
            h = rng.randint(1, 1000)
            s = rng.choice(s_choices)
            num_rects = rng.randint(0, 15)

            rects = []
            for _ in range(num_rects):
                rx = rng.randint(-50, w + 50)
                ry = rng.randint(-50, h + 50)
                rw = rng.randint(0, 200)
                rh = rng.randint(0, 200)
                rects.append((rx, ry, rw, rh))

            words_plain, pop_plain = damage_tiles_plain(w, h, s, rects)
            words_numpy, pop_numpy = damage_tiles_numpy(w, h, s, rects)

            self.assertEqual(
                words_plain,
                words_numpy,
                f"Mismatch in case {case_idx}: w={w}, h={h}, s={s}",
            )
            self.assertEqual(
                pop_plain,
                pop_numpy,
                f"Popcount mismatch in case {case_idx}: w={w}, h={h}, s={s}",
            )


class TestX1ConformanceFixtures(unittest.TestCase):
    """Conformance tests against fixtures in langx/x1/fixtures if present."""

    def setUp(self):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.fixtures_dir = os.path.abspath(os.path.join(base_dir, "..", "fixtures"))

        repack_cases_file = os.path.join(self.fixtures_dir, "repack_cases.json")
        repack_src_file = os.path.join(self.fixtures_dir, "repack_src.bin")
        repack_dst_file = os.path.join(self.fixtures_dir, "repack_dst.bin")
        damage_cases_file = os.path.join(self.fixtures_dir, "damage_cases.json")

        required_files = [
            repack_cases_file,
            repack_src_file,
            repack_dst_file,
            damage_cases_file,
        ]

        if not os.path.isdir(self.fixtures_dir) or not all(
            os.path.isfile(f) for f in required_files
        ):
            raise unittest.SkipTest("fixtures absent at langx/x1/fixtures")

    def test_repack_fixtures(self):
        repack_cases_file = os.path.join(self.fixtures_dir, "repack_cases.json")
        repack_src_file = os.path.join(self.fixtures_dir, "repack_src.bin")
        repack_dst_file = os.path.join(self.fixtures_dir, "repack_dst.bin")

        with open(repack_cases_file, "r") as f:
            cases = json.load(f)

        with open(repack_src_file, "rb") as f:
            src_bytes = f.read()

        with open(repack_dst_file, "rb") as f:
            expected_dst_bytes = f.read()

        for case in cases:
            w = case["w"]
            h = case["h"]
            src_stride = case["src_stride"]
            dst_stride = case["dst_stride"]

            if "seed" in case:
                # Large case given by seed and FNV-1a 64
                seed = case["seed"]
                expected_fnv = case["fnv64"]
                if isinstance(expected_fnv, str):
                    expected_fnv = int(expected_fnv, 16)

                min_src_len = (h - 1) * src_stride + 4 * w if (w and h) else 0
                min_dst_len = (h - 1) * dst_stride + 3 * w if (w and h) else 0

                src_buf = splitmix64(seed, min_src_len)
                dst_buf = bytearray(min_dst_len)

                repack_bgrx_to_rgb(src_buf, w, h, src_stride, dst_buf, dst_stride)
                out_fnv = fnv1a64(dst_buf)
                self.assertEqual(out_fnv, expected_fnv)
            else:
                src_off = case["src_offset"]
                dst_off = case["dst_offset"]
                dst_len = case["dst_len"]

                src_sub = src_bytes[src_off:]
                dst_buf = bytearray(dst_len)

                repack_bgrx_to_rgb(src_sub, w, h, src_stride, dst_buf, dst_stride)
                expected_sub = expected_dst_bytes[dst_off : dst_off + dst_len]
                self.assertEqual(dst_buf, expected_sub)

    def test_damage_fixtures(self):
        damage_cases_file = os.path.join(self.fixtures_dir, "damage_cases.json")
        with open(damage_cases_file, "r") as f:
            cases = json.load(f)

        for case in cases:
            w = case["w"]
            h = case["h"]
            s = case["s"]
            rects = case["rects"]
            expected_words_hex = case["words"]
            expected_popcount = case["popcount"]

            words, popcount = damage_tiles(w, h, s, rects)
            words_hex = [f"{word:016x}" for word in words]

            self.assertEqual(words_hex, expected_words_hex)
            self.assertEqual(popcount, expected_popcount)


if __name__ == "__main__":
    unittest.main()
