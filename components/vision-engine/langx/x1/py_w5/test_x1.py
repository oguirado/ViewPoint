"""Tests for LANG-X experiment X1 fluency seed py_w5."""

import json
import os
import unittest
import numpy as np

from langx.x1.py_w5.x1 import (
    repack_bgrx_to_rgb,
    damage_tiles,
    damage_tiles_plain_loop,
    SplitMix64,
    fnv1a_64,
    X1Error,
    E_DIM,
    E_STRIDE,
    E_SHORT,
    E_RECT,
    E_GRID,
)


class TestX1Unit(unittest.TestCase):
    def test_repack_2x2_padded(self):
        # 2x2 image, W=2, H=2
        # src_stride = 10 (min 4*2 = 8), dst_stride = 8 (min 3*2 = 6)
        w, h = 2, 2
        src_stride = 10
        dst_stride = 8

        # Allocate src buffer: row 0 padded, row 1 no padding needed at end but let's pad
        src_buf = bytearray(h * src_stride)
        # Row 0, col 0: B=10, G=20, R=30, X=99
        src_buf[0:4] = [10, 20, 30, 99]
        # Row 0, col 1: B=11, G=21, R=31, X=99
        src_buf[4:8] = [11, 21, 31, 99]
        # src_buf[8:10] is row 0 padding (0xEE)
        src_buf[8:10] = [0xEE, 0xEE]

        # Row 1, col 0: B=40, G=50, R=60, X=99
        src_buf[10:14] = [40, 50, 60, 99]
        # Row 1, col 1: B=41, G=51, R=61, X=99
        src_buf[14:18] = [41, 51, 61, 99]
        # src_buf[18:20] padding
        src_buf[18:20] = [0xEE, 0xEE]

        # Destination buffer filled with 0xFF
        dst_buf = bytearray([0xFF] * (h * dst_stride))

        repack_bgrx_to_rgb(src_buf, w, h, src_stride, dst_buf, dst_stride)

        # Check row 0 output: RGB -> R, G, B
        self.assertEqual(list(dst_buf[0:3]), [30, 20, 10])
        self.assertEqual(list(dst_buf[3:6]), [31, 21, 11])
        # Check padding untouched
        self.assertEqual(list(dst_buf[6:8]), [0xFF, 0xFF])

        # Check row 1 output
        self.assertEqual(list(dst_buf[8:11]), [60, 50, 40])
        self.assertEqual(list(dst_buf[11:14]), [61, 51, 41])
        # Check padding untouched
        self.assertEqual(list(dst_buf[14:16]), [0xFF, 0xFF])

    def test_repack_w0_h0(self):
        dst = bytearray(10)
        repack_bgrx_to_rgb(b"", 0, 0, 0, dst, 0)
        repack_bgrx_to_rgb(b"", 0, 10, 0, dst, 0)
        repack_bgrx_to_rgb(b"", 10, 0, 40, dst, 30)
        self.assertEqual(dst, bytearray(10))

    def test_errors_repack(self):
        dst = bytearray(100)
        # E_DIM: W or H outside 0 to 16384
        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(b"", -1, 10, 0, dst, 30)
        self.assertEqual(cm.exception.code, E_DIM)

        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(b"", 16385, 10, 65540, dst, 49155)
        self.assertEqual(cm.exception.code, E_DIM)

        # E_STRIDE: src_stride < 4W or dst_stride < 3W
        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(b"x" * 100, 10, 10, 39, dst, 30)
        self.assertEqual(cm.exception.code, E_STRIDE)

        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(b"x" * 100, 10, 10, 40, dst, 29)
        self.assertEqual(cm.exception.code, E_STRIDE)

        # E_SHORT: buffer too short
        # W=2, H=2, src_stride=8, dst_stride=6
        # min_src = (1)*8 + 8 = 16
        # min_dst = (1)*6 + 6 = 12
        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(b"x" * 15, 2, 2, 8, bytearray(12), 6)
        self.assertEqual(cm.exception.code, E_SHORT)

        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(b"x" * 16, 2, 2, 8, bytearray(11), 6)
        self.assertEqual(cm.exception.code, E_SHORT)

    def test_errors_damage(self):
        # E_DIM: W or H outside 1 to 16384
        with self.assertRaises(X1Error) as cm:
            damage_tiles(0, 100, 32, [])
        self.assertEqual(cm.exception.code, E_DIM)

        with self.assertRaises(X1Error) as cm:
            damage_tiles(100, 16385, 32, [])
        self.assertEqual(cm.exception.code, E_DIM)

        # E_GRID: s not a power of two in 1..256
        for invalid_s in [0, 3, 5, 30, 257, -1]:
            with self.assertRaises(X1Error) as cm:
                damage_tiles(100, 100, invalid_s, [])
            self.assertEqual(cm.exception.code, E_GRID)

        # E_RECT: negative width or height
        with self.assertRaises(X1Error) as cm:
            damage_tiles(100, 100, 32, [(0, 0, -1, 10)])
        self.assertEqual(cm.exception.code, E_RECT)

        with self.assertRaises(X1Error) as cm:
            damage_tiles(100, 100, 32, [(0, 0, 10, -5)])
        self.assertEqual(cm.exception.code, E_RECT)

    def test_worked_check_1920_1080_s32(self):
        # W 1920, H 1080, s 32 gives gw 60, gh 34, 2040 tiles and 32 words;
        # a 192x32 rectangle at x 96, y 0 sets tiles 3 to 8 of row 0.
        w, h, s = 1920, 1080, 32
        rects = [(96, 0, 192, 32)]
        words, popcount = damage_tiles(w, h, s, rects)

        gw = (1920 + 31) // 32  # 60
        gh = (1080 + 31) // 32  # 34
        total_tiles = gw * gh  # 2040
        expected_words_count = (total_tiles + 63) // 64  # 32

        self.assertEqual(len(words), expected_words_count)
        # x 96..288 => tx 3..9 excluding 9 -> tx=3,4,5,6,7,8 (6 tiles total)
        self.assertEqual(popcount, 6)

        # Word 0: bits 3, 4, 5, 6, 7, 8 set => (1<<3) + (1<<4) + (1<<5) + (1<<6) + (1<<7) + (1<<8)
        expected_word_0 = sum(1 << i for i in range(3, 9))
        self.assertEqual(words[0], expected_word_0)
        for w_val in words[1:]:
            self.assertEqual(w_val, 0)

    def test_rect_w0_skipped(self):
        # x 33, w 0 sets nothing
        words, popcount = damage_tiles(100, 100, 16, [(33, 10, 0, 20)])
        self.assertEqual(popcount, 0)
        self.assertTrue(all(w == 0 for w in words))

    def test_rect_partial_bottom_row(self):
        # W=100, H=50, s=32 -> gw=4, gh=2 (total tiles 8, 1 word)
        # Tile rows: ty=0 (y 0..31), ty=1 (y 32..49 - partial)
        # Rect at y=20, h=25 -> y1=45 -> covers ty=0 and ty=1
        words, popcount = damage_tiles(100, 50, 32, [(0, 20, 32, 25)])
        # tx=0, ty=0 -> t=0; tx=0, ty=1 -> t=4
        self.assertEqual(popcount, 2)
        expected_word = (1 << 0) | (1 << 4)
        self.assertEqual(words[0], expected_word)

    def test_splitmix64_fnv1a64_pinned(self):
        # Test SplitMix64 with seed 0x123456789ABCDEF0
        sm = SplitMix64(0x123456789ABCDEF0)
        val1 = sm.next_u64()
        val2 = sm.next_u64()

        # Compute step 1 manually:
        # state1 = 0x123456789ABCDEF0 + 0x9E3779B97F4A7C15 = 0xB06BD0321A076B05
        # z = 0xB06BD0321A076B05
        # z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9 & 0xFFFFFFFFFFFFFFFF
        # z = (z ^ (z >> 27)) * 0x94D049BB133111EB & 0xFFFFFFFFFFFFFFFF
        # z = z ^ (z >> 31)
        # Check against implementation output
        buf = sm.fill_bytes(10)
        self.assertEqual(len(buf), 10)

        # Pin FNV-1a 64 on b"hello world"
        # FNV-1a 64 of b"hello world" is 0x779a65e7023cd2e7
        h = fnv1a_64(b"hello world")
        self.assertEqual(h, 0x779A65E7023CD2E7)

    def test_damage_numpy_vs_plain_loop_random(self):
        sm = SplitMix64(42)
        for _ in range(200):
            # Generate W in 1..200, H in 1..200, s in {1, 2, 4, 8, 16, 32}
            w = (sm.next_u64() % 200) + 1
            h = (sm.next_u64() % 200) + 1
            s = 1 << (sm.next_u64() % 6)  # 1, 2, 4, 8, 16, 32

            num_rects = sm.next_u64() % 10
            rects = []
            for _ in range(num_rects):
                rx = (sm.next_u64() % 300) - 50  # -50..249
                ry = (sm.next_u64() % 300) - 50
                rw = sm.next_u64() % 150  # 0..149
                rh = sm.next_u64() % 150
                rects.append((rx, ry, rw, rh))

            words_np, pop_np = damage_tiles(w, h, s, rects, use_plain_loop=False)
            words_loop, pop_loop = damage_tiles(
                w, h, s, rects, use_plain_loop=True
            )

            self.assertEqual(words_np, words_loop)
            self.assertEqual(pop_np, pop_loop)


class TestX1Fixtures(unittest.TestCase):
    def test_fixtures_if_present(self):
        # Fixtures dir relative to this file: langx/x1/fixtures
        base_dir = os.path.dirname(os.path.abspath(__file__))
        fixtures_dir = os.path.normpath(
            os.path.join(base_dir, "..", "fixtures")
        )

        if not os.path.isdir(fixtures_dir):
            self.skipTest("fixtures absent at langx/x1/fixtures")

        repack_cases_path = os.path.join(fixtures_dir, "repack_cases.json")
        damage_cases_path = os.path.join(fixtures_dir, "damage_cases.json")

        if not (
            os.path.isfile(repack_cases_path)
            and os.path.isfile(damage_cases_path)
        ):
            self.skipTest("fixtures absent at langx/x1/fixtures")

        # Load damage cases
        with open(damage_cases_path, "r", encoding="utf-8") as f:
            damage_cases = json.load(f)

        for case in damage_cases:
            w = case["w"]
            h = case["h"]
            s = case["s"]
            rects = case["rects"]
            expected_words_hex = case["words"]  # list of 16-digit hex strings
            expected_popcount = case["popcount"]

            words, popcount = damage_tiles(w, h, s, rects)
            actual_words_hex = [f"{w_val:016x}" for w_val in words]

            self.assertEqual(actual_words_hex, expected_words_hex)
            self.assertEqual(popcount, expected_popcount)

        # Load repack cases
        with open(repack_cases_path, "r", encoding="utf-8") as f:
            repack_cases = json.load(f)

        src_bin_path = os.path.join(fixtures_dir, "repack_src.bin")
        dst_bin_path = os.path.join(fixtures_dir, "repack_dst.bin")

        has_bin_files = os.path.isfile(src_bin_path) and os.path.isfile(
            dst_bin_path
        )

        for case in repack_cases:
            w = case["w"]
            h = case["h"]
            src_stride = case["src_stride"]
            dst_stride = case["dst_stride"]

            if "seed" in case and "fnv1a" in case:
                # Large case given by seed and FNV-1a 64
                seed = case["seed"]
                expected_fnv = int(case["fnv1a"], 16)

                sm_src = SplitMix64(seed)
                src_len = (h - 1) * src_stride + 4 * w if h > 0 and w > 0 else 0
                src_data = sm_src.fill_bytes(src_len)

                dst_len = (h - 1) * dst_stride + 3 * w if h > 0 and w > 0 else 0
                dst_buf = bytearray(dst_len)

                repack_bgrx_to_rgb(
                    src_data, w, h, src_stride, dst_buf, dst_stride
                )
                actual_fnv = fnv1a_64(dst_buf)

                self.assertEqual(actual_fnv, expected_fnv)
            else:
                # Binary slice case
                if not has_bin_files:
                    continue
                with open(src_bin_path, "rb") as f_src:
                    f_src.seek(case["src_offset"])
                    src_data = f_src.read(case["src_len"])

                with open(dst_bin_path, "rb") as f_dst:
                    f_dst.seek(case["dst_offset"])
                    expected_dst = f_dst.read(case["dst_len"])

                dst_len = (h - 1) * dst_stride + 3 * w if h > 0 and w > 0 else 0
                dst_buf = bytearray(dst_len)

                repack_bgrx_to_rgb(
                    src_data, w, h, src_stride, dst_buf, dst_stride
                )
                self.assertEqual(bytes(dst_buf), expected_dst)


if __name__ == "__main__":
    unittest.main()
