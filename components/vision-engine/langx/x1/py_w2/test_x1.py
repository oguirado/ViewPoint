import json
import os
import random
import unittest
import numpy as np

from langx.x1.py_w2.x1 import (
    X1Error,
    repack_bgrx_to_rgb,
    damage_tiles,
    _damage_tiles_plain,
    _damage_tiles_numpy,
    splitmix64,
    fnv1a_64,
)


class TestX1HandComputed(unittest.TestCase):
    def test_2x2_repack_padded(self):
        # 2x2 image, src_stride=12 (4-byte pad per row), dst_stride=8 (2-byte pad per row)
        # BGRX pixel format: B, G, R, X
        # Row 0:
        # Pixel (0,0): B=10, G=20, R=30, X=99 -> RGB: 30, 20, 10
        # Pixel (1,0): B=40, G=50, R=60, X=99 -> RGB: 60, 50, 40
        # Row 1:
        # Pixel (0,1): B=70, G=80, R=90, X=99 -> RGB: 90, 80, 70
        # Pixel (1,1): B=100, G=110, R=120, X=99 -> RGB: 120, 110, 100
        src = bytearray(24)
        # Row 0
        src[0:4] = [10, 20, 30, 99]
        src[4:8] = [40, 50, 60, 99]
        src[8:12] = [255, 255, 255, 255]  # padding
        # Row 1
        src[12:16] = [70, 80, 90, 99]
        src[16:20] = [100, 110, 120, 99]
        src[20:24] = [255, 255, 255, 255]  # padding

        dst = bytearray([0xAA] * 16)
        repack_bgrx_to_rgb(src, 2, 2, 12, dst, 8)

        # Check RGB row 0
        self.assertEqual(list(dst[0:6]), [30, 20, 10, 60, 50, 40])
        # Check padding row 0 untouched
        self.assertEqual(list(dst[6:8]), [0xAA, 0xAA])
        # Check RGB row 1
        self.assertEqual(list(dst[8:14]), [90, 80, 70, 120, 110, 100])
        # Check padding row 1 untouched
        self.assertEqual(list(dst[14:16]), [0xAA, 0xAA])

    def test_repack_zero_dim(self):
        dst = bytearray(10)
        repack_bgrx_to_rgb(b"", 0, 10, 0, dst, 0)
        self.assertEqual(dst, bytearray(10))
        repack_bgrx_to_rgb(b"", 10, 0, 40, dst, 30)
        self.assertEqual(dst, bytearray(10))

    def test_repack_errors(self):
        dst = bytearray(100)
        # E_DIM
        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(b"", -1, 10, 0, dst, 0)
        self.assertEqual(cm.exception.code, "E_DIM")

        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(b"", 10, 16385, 40, dst, 30)
        self.assertEqual(cm.exception.code, "E_DIM")

        # E_STRIDE
        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(b"", 10, 10, 39, dst, 30)
        self.assertEqual(cm.exception.code, "E_STRIDE")

        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(b"", 10, 10, 40, dst, 29)
        self.assertEqual(cm.exception.code, "E_STRIDE")

        # E_SHORT
        src = b"\x00" * 30  # Needs (2-1)*40 + 40 = 80
        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(src, 10, 2, 40, dst, 30)
        self.assertEqual(cm.exception.code, "E_SHORT")

        src = b"\x00" * 80
        dst_short = bytearray(50)  # Needs (2-1)*30 + 30 = 60
        with self.assertRaises(X1Error) as cm:
            repack_bgrx_to_rgb(src, 10, 2, 40, dst_short, 30)
        self.assertEqual(cm.exception.code, "E_SHORT")

    def test_damage_worked_check(self):
        # Worked check from spec: W 1920, H 1080, s 32 gives gw 60, gh 34, 2040 tiles and 32 words;
        # a 192x32 rectangle at x 96, y 0 sets tiles 3 to 8 of row 0.
        words, popcount = damage_tiles(1920, 1080, 32, [(96, 0, 192, 32)])
        self.assertEqual(len(words), 32)
        self.assertEqual(popcount, 6)
        self.assertEqual(words[0], 0x1F8)  # bits 3, 4, 5, 6, 7, 8 set
        for w in words[1:]:
            self.assertEqual(w, 0)

    def test_damage_x33_w0(self):
        words, popcount = damage_tiles(1920, 1080, 32, [(33, 0, 0, 10)])
        self.assertEqual(popcount, 0)
        self.assertTrue(all(w == 0 for w in words))

    def test_damage_bottom_partial_row(self):
        # W 100, H 100, s 64 -> gw = 2, gh = 2 (tile 0: (0,0), tile 1: (1,0), tile 2: (0,1), tile 3: (1,1))
        # Bottom partial row: y=70, h=20 -> y range [70, 90], so ty in range [1, 2].
        words, popcount = damage_tiles(100, 100, 64, [(10, 70, 80, 20)])
        # x range [10, 90], tx in range [0, 2]. So tiles (0,1) and (1,1) -> indices 2 and 3.
        self.assertEqual(popcount, 2)
        self.assertEqual(words[0], 0x0C)  # bits 2 and 3 set

    def test_damage_errors(self):
        # E_DIM
        with self.assertRaises(X1Error) as cm:
            damage_tiles(0, 1080, 32, [])
        self.assertEqual(cm.exception.code, "E_DIM")

        with self.assertRaises(X1Error) as cm:
            damage_tiles(1920, 16385, 32, [])
        self.assertEqual(cm.exception.code, "E_DIM")

        # E_GRID
        with self.assertRaises(X1Error) as cm:
            damage_tiles(1920, 1080, 30, [])
        self.assertEqual(cm.exception.code, "E_GRID")

        with self.assertRaises(X1Error) as cm:
            damage_tiles(1920, 1080, 512, [])
        self.assertEqual(cm.exception.code, "E_GRID")

        # E_RECT
        with self.assertRaises(X1Error) as cm:
            damage_tiles(1920, 1080, 32, [(0, 0, -1, 10)])
        self.assertEqual(cm.exception.code, "E_RECT")

        with self.assertRaises(X1Error) as cm:
            damage_tiles(1920, 1080, 32, [(0, 0, 10, -5)])
        self.assertEqual(cm.exception.code, "E_RECT")

    def test_splitmix64_and_fnv1a_pinned(self):
        val = splitmix64(12345, 2)
        self.assertEqual(val, [0x22118258A9D111A0, 0x346EDCE5F713F8ED])

        buf = b"".join(v.to_bytes(8, "little") for v in val)[:12]
        h = fnv1a_64(buf)
        self.assertEqual(hex(h), "0x618596c5101f8baa")


class TestX1RandomEquivalence(unittest.TestCase):
    def test_numpy_vs_plain_random(self):
        rng = random.Random(0x12345678)
        w, h = 1920, 1080
        s = 32
        gw, gh = (w + s - 1) // s, (h + s - 1) // s

        for _ in range(200):
            n_rects = rng.randint(0, 50)
            rects = []
            for _ in range(n_rects):
                rx = rng.randint(-50, w + 50)
                ry = rng.randint(-50, h + 50)
                rw = rng.randint(0, 500)
                rh = rng.randint(0, 500)
                rects.append((rx, ry, rw, rh))

            words_plain, pop_plain = _damage_tiles_plain(w, h, s, rects, gw, gh)
            words_np, pop_np = _damage_tiles_numpy(w, h, s, rects, gw, gh)

            self.assertEqual(words_plain, words_np)
            self.assertEqual(pop_plain, pop_np)


class TestX1Fixtures(unittest.TestCase):
    def test_conformance_fixtures(self):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        fixtures_dir = os.path.join(base_dir, "..", "fixtures")

        repack_cases_path = os.path.join(fixtures_dir, "repack_cases.json")
        damage_cases_path = os.path.join(fixtures_dir, "damage_cases.json")

        if not os.path.exists(fixtures_dir) or not os.path.exists(repack_cases_path):
            self.skipTest("fixtures absent at langx/x1/fixtures")

        with open(repack_cases_path, "r") as f:
            repack_cases = json.load(f)

        repack_src_path = os.path.join(fixtures_dir, "repack_src.bin")
        repack_dst_path = os.path.join(fixtures_dir, "repack_dst.bin")

        src_bin = open(repack_src_path, "rb").read() if os.path.exists(repack_src_path) else None
        dst_bin = open(repack_dst_path, "rb").read() if os.path.exists(repack_dst_path) else None

        for case in repack_cases:
            w = case["w"]
            h = case["h"]
            src_stride = case["src_stride"]
            dst_stride = case["dst_stride"]

            if "src_off" in case:
                src = src_bin[case["src_off"]:case["src_off"] + case["src_len"]]
                dst = bytearray(dst_bin[case["dst_off"]:case["dst_off"] + case["dst_len"]])
                repack_bgrx_to_rgb(src, w, h, src_stride, dst, dst_stride)
                expected = dst_bin[case["expected_dst_off"]:case["expected_dst_off"] + case["dst_len"]]
                self.assertEqual(dst, expected)
            elif "seed" in case:
                # Large seed-based case
                seed = case["seed"]
                src_bytes_count = (h - 1) * src_stride + 4 * w if w > 0 and h > 0 else 0
                n_words = (src_bytes_count + 7) // 8
                src_words = splitmix64(seed, n_words)
                src = b"".join(v.to_bytes(8, "little") for v in src_words)[:src_bytes_count]

                dst_bytes_count = (h - 1) * dst_stride + 3 * w if w > 0 and h > 0 else 0
                dst = bytearray(dst_bytes_count)
                repack_bgrx_to_rgb(src, w, h, src_stride, dst, dst_stride)

                h_val = fnv1a_64(dst)
                expected_fnv = int(case["expected_fnv64"], 16) if isinstance(case["expected_fnv64"], str) else case["expected_fnv64"]
                self.assertEqual(h_val, expected_fnv)

        with open(damage_cases_path, "r") as f:
            damage_cases = json.load(f)

        for case in damage_cases:
            w = case["w"]
            h = case["h"]
            s = case["s"]
            rects = [tuple(r) for r in case["rects"]]

            words, popcount = damage_tiles(w, h, s, rects)
            expected_hex_words = case["expected_words"]
            expected_words = [int(hw, 16) for hw in expected_hex_words]

            self.assertEqual(words, expected_words)
            self.assertEqual(popcount, case["expected_popcount"])


if __name__ == "__main__":
    unittest.main()
