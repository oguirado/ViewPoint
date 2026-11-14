"""decode.py performance rewrite + hardening (RCORE-4, RCORE-6): wavefront unfilter parity with an independent
scalar reference, IEND required, bounded inflate, budget ceiling (VQA_PERF=0 skips)."""
import os
import statistics
import struct
import time
import unittest
import zlib

import numpy as np

from visioncore import decode
from visioncore.errors import VqaError


def chunk(t, b):
    return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)


def png_from_raw(raw, W, H, ctype=2, iend=True, idat=None):
    ihdr = struct.pack(">IIBBBBB", W, H, 8, ctype, 0, 0, 0)
    out = decode._SIG + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw) if idat is None else idat)
    return out + (chunk(b"IEND", b"") if iend else b"")


def scalar_unfilter(raw, H, stride, bpp):
    """Independent byte-by-byte reference straight from the PNG spec."""
    out = [[0] * stride for _ in range(H)]
    for y in range(H):
        ft = raw[y * (stride + 1)]
        row = raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)]
        for x in range(stride):
            a = out[y][x - bpp] if x >= bpp else 0
            b = out[y - 1][x] if y else 0
            c = out[y - 1][x - bpp] if y and x >= bpp else 0
            if ft == 0:
                p = 0
            elif ft == 1:
                p = a
            elif ft == 2:
                p = b
            elif ft == 3:
                p = (a + b) >> 1
            else:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                p = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
            out[y][x] = (row[x] + p) & 255
    return np.array(out, dtype=np.uint8)


class TestWavefront(unittest.TestCase):
    def test_parity_with_scalar_reference(self):
        rs = np.random.RandomState(11)
        for W in (1, 2, 3, 8, 21):
            for H in (1, 2, 5, 13):
                for bpp in (3, 4):
                    ft = rs.randint(0, 5, H).astype(np.uint8)
                    raw = np.concatenate([ft[:, None], rs.randint(0, 256, (H, W * bpp)).astype(np.uint8)], 1).tobytes()
                    got = decode.unfilter(raw, H, W * bpp, bpp)
                    self.assertTrue(np.array_equal(got, scalar_unfilter(raw, H, W * bpp, bpp)), (W, H, bpp))

    def test_each_filter_alone_and_pairs(self):
        rs = np.random.RandomState(12)
        H, W, bpp = 9, 14, 3
        body = rs.randint(0, 256, (H, W * bpp)).astype(np.uint8)
        for fts in ([0] * H, [3] * H, [4] * H, [1, 4] * 5, [2, 3] * 5, [4, 4, 0, 4, 1, 3, 2, 4, 4]):
            ft = np.array(fts[:H], np.uint8)
            raw = np.concatenate([ft[:, None], body], 1).tobytes()
            self.assertTrue(np.array_equal(decode.unfilter(raw, H, W * bpp, bpp), scalar_unfilter(raw, H, W * bpp, bpp)), fts)

    def test_bad_filter_type_rejected(self):
        raw = bytes([7]) + bytes(3)
        with self.assertRaises(VqaError):
            decode.unfilter(raw, 1, 3, 3)
        raw = bytes([5]) + bytes(3) + bytes([4]) + bytes(3)
        with self.assertRaises(VqaError):
            decode.unfilter(raw, 2, 3, 3)


class TestHardening(unittest.TestCase):
    def test_missing_iend_rejected(self):
        raw = bytes([0]) + bytes(3)
        decode.png(png_from_raw(raw, 1, 1))
        with self.assertRaises(VqaError) as cm:
            decode.png(png_from_raw(raw, 1, 1, iend=False))
        self.assertEqual(cm.exception.code, "E_VC_PNG")

    def test_inflate_bomb_rejected(self):
        bomb = zlib.compress(bytes(20 * 1024 * 1024), 9)  # inflates to 20 MiB, header says 1x1
        with self.assertRaises(VqaError):
            decode.png(png_from_raw(b"", 1, 1, idat=bomb))

    def test_short_and_trailing_data_rejected(self):
        with self.assertRaises(VqaError):
            decode.png(png_from_raw(bytes(3), 1, 1))           # 3 < 4 bytes
        with self.assertRaises(VqaError):
            decode.png(png_from_raw(bytes(5), 1, 1))           # 5 > 4 bytes

    def test_truncated_stream_rejected(self):
        raw = bytes([0]) + bytes(300)
        z = zlib.compress(raw)[:-6]
        with self.assertRaises(VqaError):
            decode.png(png_from_raw(b"", 100, 1, idat=z))


@unittest.skipIf(os.environ.get("VQA_PERF") == "0", "VQA_PERF=0")
class TestDecodeBudget(unittest.TestCase):
    def test_decode_budget(self):
        # 720p all-Paeth: was ~437 ms (scalar loop). Wavefront ~50 ms [MEASURED]; the 15 ms ARCH §4.4 figure stays a
        # [TARGET] (needs a K-tier kernel), so this asserts a regression ceiling, not the target.
        H, W = 720, 1280
        rs = np.random.RandomState(3)
        raw = np.concatenate([np.full((H, 1), 4, np.uint8), rs.randint(0, 256, (H, W * 3)).astype(np.uint8)], 1).tobytes()
        ts = []
        for _ in range(3):
            t0 = time.perf_counter()
            decode.unfilter(raw, H, W * 3, 3)
            ts.append(time.perf_counter() - t0)
        self.assertLess(statistics.median(ts) * 1e3, 200.0)


if __name__ == "__main__":
    unittest.main()
