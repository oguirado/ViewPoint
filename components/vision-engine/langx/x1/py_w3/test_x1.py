"""Unit and fixture tests for LANG-X X1 py_w3 seed."""

import json
import os
import random
import unittest
import numpy as np

from langx.x1.py_w3.x1 import (
    X1Error,
    E_DIM,
    E_STRIDE,
    E_SHORT,
    E_RECT,
    E_GRID,
    repack_bgrx_to_rgb,
    damage_tiles,
    _damage_tiles_plain,
    _damage_tiles_numpy,
    splitmix64,
    fnv1a64,
)


class TestX1AlwaysOn(unittest.TestCase):
    """Hand-computed unit tests that always run independently of shared fixtures."""

    def test_repack_2x2_padded(self):
        """2x2 repack with padded strides and untouched destination padding."""
        w, h = 2, 2
        src_stride = 10  # 4*2 + 2 padding bytes
        dst_stride = 8   # 3*2 + 2 padding bytes

        src = bytearray(20)
        # Row 0: pixel (0,0) BGRX=(1,2,3,4), pixel (1,0) BGRX=(5,6,7,8), pad=(9,10)
        src[0:4] = [1, 2, 3, 4]
        src[4:8] = [5, 6, 7, 8]
        src[8:10] = [9, 10]
        # Row 1: pixel (0,1) BGRX=(11,12,13,14), pixel (1,1) BGRX=(15,16,17,18), pad=(19,20)
        src[10:14] = [11, 12, 13, 14]
        src[14:18] = [15, 16, 17, 18]
        src[18:20] = [19, 20]

        dst = bytearray(b"\xff" * 16)

        repack_bgrx_to_rgb(src, w, h, src_stride, dst, dst_stride)

        # Expected dst layout:
        # Row 0: pixel (0,0) RGB=(3,2,1), pixel (1,0) RGB=(7,6,5), pad=(255,255)
        # Row 1: pixel (0,1) RGB=(13,12,11), pixel (1,1) RGB=(17,16,15), pad=(255,255)
        expected = bytearray([
            3, 2, 1, 7, 6, 5, 255, 255,
            13, 12, 11, 17, 16, 15, 255, 255
        ])
        self.assertEqual(dst, expected)

    def test_repack_numpy_dst(self):
        """Repack with writable numpy uint8 array destination."""
        w, h = 2, 2
        src_stride = 8
        dst_stride = 6
        src = bytearray([
            10, 20, 30, 0, 40, 50, 60, 0,
            70, 80, 90, 0, 100, 110, 120, 0
        ])
        dst = np.zeros(12, dtype=np.uint8)
        repack_bgrx_to_rgb(src, w, h, src_stride, dst, dst_stride)
        expected = np.array([
            30, 20, 10, 60, 50, 40,
            90, 80, 70, 120, 110, 100
        ], dtype=np.uint8)
        np.testing.assert_array_equal(dst, expected)

    def test_errors_repack(self):
        """Test all error conditions for repack_bgrx_to_rgb."""
        src = bytearray(100)
        dst = bytearray(100)

        # E_DIM
        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(src, -1, 10, 40, dst, 30)
        self.assertEqual(cm.exception.code, E_DIM)

        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(src, 10, 16385, 40, dst, 30)
        self.assertEqual(cm.exception.code, E_DIM)

        # E_STRIDE
        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(src, 10, 10, 39, dst, 30)
        self.assertEqual(cm.exception.code, E_STRIDE)

        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(src, 10, 10, 40, dst, 29)
        self.assertEqual(cm.exception.code, E_STRIDE)

        # E_SHORT
        # w=10, h=2, src_stride=40, dst_stride=30
        # min src len = (2-1)*40 + 4*10 = 80
        # min dst len = (2-1)*30 + 3*10 = 60
        short_src = bytearray(79)
        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(short_src, 10, 2, 40, dst, 30)
        self.assertEqual(cm.exception.code, E_SHORT)

        short_dst = bytearray(59)
        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(src, 10, 2, 40, short_dst, 30)
        self.assertEqual(cm.exception.code, E_SHORT)

    def test_errors_damage(self):
        """Test all error conditions for damage_tiles."""
        rects = [(0, 0, 10, 10)]

        # E_DIM
        with self.assertRaises(X1Error) as cm:
            damage_tiles(0, 100, 16, rects)
        self.assertEqual(cm.exception.code, E_DIM)

        with self.assertRaises(X1Error) as cm:
            damage_tiles(16385, 100, 16, rects)
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
        bad_rects = [(0, 0, -1, 10)]
        with self.assertRaises(X1Error) as cm:
            damage_tiles(100, 100, 16, bad_rects)
        self.assertEqual(cm.exception.code, E_RECT)

        bad_rects2 = [(0, 0, 10, -5)]
        with self.assertRaises(X1Error) as cm:
            damage_tiles(100, 100, 16, bad_rects2)
        self.assertEqual(cm.exception.code, E_RECT)

    def test_worked_check_1920_1080_s32(self):
        """Worked check: W 1920, H 1080, s 32 gives gw 60, gh 34, 2040 tiles and 32 words; a 192x32 rectangle at x 96, y 0 sets tiles 3 to 8 of row 0."""
        w, h, s = 1920, 1080, 32
        rects = [(96, 0, 192, 32)]
        words, popcount = damage_tiles(w, h, s, rects)

        self.assertEqual(len(words), 32)
        # Tiles set: tx from 96//32=3 to ceil(288/32)=9, so tx in {3, 4, 5, 6, 7, 8}
        # Bits 3, 4, 5, 6, 7, 8 in word 0 -> 1<<3 | 1<<4 | 1<<5 | 1<<6 | 1<<7 | 1<<8 = 0x1F8 = 504
        self.assertEqual(words[0], 0x1F8)
        self.assertEqual(popcount, 6)
        self.assertEqual(sum(words[1:]), 0)

    def test_zero_width_rect_and_partial_bottom_row(self):
        """x=33 with w=0 sets no bit, and a rectangle crossing partial bottom row works correctly."""
        # x 33, w 0 sets nothing
        words, popcount = damage_tiles(100, 100, 16, [(33, 10, 0, 20)])
        self.assertEqual(popcount, 0)
        self.assertTrue(all(w == 0 for w in words))

        # Partial bottom row check: W=100, H=100, s=32 -> gw=4, gh=4. Total tiles=16.
        # Tile rows: y=0..31 -> ty=0; y=32..63 -> ty=1; y=64..95 -> ty=2; y=96..99 -> ty=3 (partial row, H=100).
        # Rect at x=0, y=90, w=32, h=10 -> spans y=90..100 (clipped to H=100).
        # y0=90 (ty=2), y1=100 (ceil(100/32)=4 -> ty=2, 3). So ty in {2, 3}.
        words2, popcount2 = damage_tiles(100, 100, 32, [(0, 90, 32, 10)])
        # tx = 0. Tiles set: ty=2, tx=0 -> t = 2*4 + 0 = 8; ty=3, tx=0 -> t = 3*4 + 0 = 12.
        # Bits 8 and 12 set in word 0.
        expected_word0 = (1 << 8) | (1 << 12)
        self.assertEqual(words2[0], expected_word0)
        self.assertEqual(popcount2, 2)

    def test_splitmix64_and_fnv1a64_pinned(self):
        """SplitMix64 and FNV-1a 64 test vectors pinned in the test."""
        # Pin SplitMix64 seed 0, length 16
        sm_out = splitmix64(0, 16)
        expected_sm = bytes([
            0xaf, 0xcd, 0x1d, 0x7b, 0x39, 0xa8, 0x20, 0xe2,
            0xf4, 0x65, 0xb9, 0xa1, 0x6a, 0x9e, 0x78, 0x6e
        ])
        self.assertEqual(sm_out, expected_sm)

        # Pin FNV-1a 64 over sm_out[:10]
        h_val = fnv1a64(sm_out[:10])
        self.assertEqual(h_val, 0xA7018E0DB35C88BD)

    def test_random_rectangles_plain_vs_numpy_equivalence(self):
        """Compare numpy path with plain-loop path on 200 random rectangle lists from a fixed seed."""
        rng = random.Random(0x12345678)
        for _ in range(200):
            w = rng.randint(1, 1000)
            h = rng.randint(1, 1000)
            s_power = rng.randint(0, 8)  # 2^0 to 2^8 -> 1 to 256
            s = 1 << s_power
            num_rects = rng.randint(0, 30)
            rects = []
            for _ in range(num_rects):
                rx = rng.randint(-100, w + 100)
                ry = rng.randint(-100, h + 100)
                rw = rng.randint(0, 200)
                rh = rng.randint(0, 200)
                rects.append((rx, ry, rw, rh))

            words_plain, pop_plain = _damage_tiles_plain(w, h, s, rects)
            words_numpy, pop_numpy = _damage_tiles_numpy(w, h, s, rects)

            self.assertEqual(words_plain, words_numpy)
            self.assertEqual(pop_plain, pop_numpy)


class TestX1Fixtures(unittest.TestCase):
    """Fixture tests reading shared conformance fixtures if present."""

    def test_conformance_fixtures(self):
        """Run shared fixtures if present; skip if absent."""
        fixture_dir = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "fixtures")
        )

        repack_cases_path = os.path.join(fixture_dir, "repack_cases.json")
        repack_src_path = os.path.join(fixture_dir, "repack_src.bin")
        repack_dst_path = os.path.join(fixture_dir, "repack_dst.bin")
        damage_cases_path = os.path.join(fixture_dir, "damage_cases.json")

        required_files = [
            repack_cases_path,
            repack_src_path,
            repack_dst_path,
            damage_cases_path,
        ]

        if not os.path.isdir(fixture_dir) or any(not os.path.isfile(f) for f in required_files):
            self.skipTest(f"fixtures absent at langx/x1/fixtures")

        # Load damage cases
        with open(damage_cases_path, "r", encoding="utf-8") as f:
            damage_cases = json.load(f)

        for case in damage_cases:
            w = case["w"]
            h = case["h"]
            s = case["s"]
            rects = [tuple(r) for r in case["rects"]]
            expected_words_hex = case["words"]  # 16-digit lowercase hex strings
            expected_popcount = case["popcount"]

            words, popcount = damage_tiles(w, h, s, rects)
            words_hex = [f"{word:016x}" for word in words]

            self.assertEqual(words_hex, expected_words_hex, f"Damage case {case.get('id')} words mismatch")
            self.assertEqual(popcount, expected_popcount, f"Damage case {case.get('id')} popcount mismatch")

        # Load repack cases
        with open(repack_cases_path, "r", encoding="utf-8") as f:
            repack_cases = json.load(f)

        with open(repack_src_path, "rb") as f:
            repack_src_buf = f.read()

        with open(repack_dst_path, "rb") as f:
            repack_dst_buf = f.read()

        for case in repack_cases:
            w = case["w"]
            h = case["h"]
            src_stride = case["src_stride"]
            dst_stride = case["dst_stride"]

            if "src_offset" in case:
                src_offset = case["src_offset"]
                src_len = case["src_len"]
                dst_offset = case["dst_offset"]
                dst_len = case["dst_len"]
                expected_fnv = case["dst_fnv1a64"]

                src_data = bytearray(repack_src_buf[src_offset : src_offset + src_len])
                dst_data = bytearray(repack_dst_buf[dst_offset : dst_offset + dst_len])

                repack_bgrx_to_rgb(src_data, w, h, src_stride, dst_data, dst_stride)
                out_fnv = fnv1a64(dst_data)
                self.assertEqual(out_fnv, expected_fnv, f"Repack case {case.get('id')} FNV-1a 64 mismatch")
            elif "seed" in case:
                seed = case["seed"]
                src_len = case["src_len"]
                dst_len = case["dst_len"]
                expected_fnv = case["dst_fnv1a64"]

                src_data = splitmix64(seed, src_len)
                dst_data = bytearray(dst_len)

                repack_bgrx_to_rgb(src_data, w, h, src_stride, dst_data, dst_stride)
                out_fnv = fnv1a64(dst_data)
                self.assertEqual(out_fnv, expected_fnv, f"Large repack case {case.get('id')} FNV-1a 64 mismatch")


if __name__ == "__main__":
    unittest.main()
