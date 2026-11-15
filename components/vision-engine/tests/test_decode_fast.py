"""decode.png_fast (CDESIGN_CENG F1): bit-equality with the canonical decoder, refusal parity, guard behaviour."""
import os
import struct
import time
import unittest
import zlib
from unittest import mock

import numpy as np

from visioncore import decode
from visioncore.errors import VqaError

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None


def chunk(t, b):
    return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)


def image(seed, H=48, W=64):
    r = np.random.RandomState(seed)
    base = (np.add.outer(np.arange(H), np.arange(W))[:, :, None] * 3 + r.randint(0, 20, (H, W, 3))) % 256
    return base.astype(np.uint8)


def cv_png(rgb, level=3, strategy=None):
    params = [cv2.IMWRITE_PNG_COMPRESSION, level]
    if strategy is not None:
        params += [cv2.IMWRITE_PNG_STRATEGY, strategy]
    ok, buf = cv2.imencode(".png", rgb[:, :, ::-1], params)
    assert ok
    return buf.tobytes()


def fixtures():
    out = [decode.encode_png(image(1)), decode.encode_png(image(2, 17, 33)), decode.encode_png(image(3, 1, 1))]
    if cv2 is not None:
        for strat in (None, cv2.IMWRITE_PNG_STRATEGY_FILTERED, cv2.IMWRITE_PNG_STRATEGY_HUFFMAN_ONLY):
            out.append(cv_png(image(4), 6, strat))
    return out


class TestFast(unittest.TestCase):
    def setUp(self):
        decode.reset_guard()

    def test_equals_canonical(self):
        for data in fixtures():
            decode.reset_guard()
            ref = decode.png(data)
            for _ in range(3):                       # 1st call = guard, 2nd/3rd = steady state
                got = decode.png_fast(data)
                self.assertTrue(np.array_equal(got, ref))
                self.assertEqual(got.dtype, np.uint8)
                self.assertTrue(got.flags["C_CONTIGUOUS"])

    @unittest.skipIf(cv2 is None, "cv2 absent")
    def test_guard_turns_on(self):
        decode.png_fast(decode.encode_png(image(5)))
        self.assertEqual(decode.fast_state(), "on")

    @unittest.skipIf(cv2 is None, "cv2 absent")
    def test_all_filter_types_with_cv2_encoder(self):
        data = cv_png(image(6, 64, 64), 9)
        self.assertGreater(len(set(zlib.decompress(self._idat(data))[i] for i in range(0, 64 * 193, 193))), 1)
        decode.reset_guard()
        self.assertTrue(np.array_equal(decode.png_fast(data), decode.png(data)))
        self.assertTrue(np.array_equal(decode.png_fast(data), decode.png(data)))

    @staticmethod
    def _idat(data):
        pos, out = 8, b""
        while pos < len(data):
            ln, typ = struct.unpack_from(">I4s", data, pos)
            if typ == b"IDAT":
                out += data[pos + 8:pos + 8 + ln]
            pos += 12 + ln
        return out

    def test_rgba_alpha_dropped(self):
        rgb = image(7)
        rgba = np.concatenate([rgb, np.full(rgb.shape[:2] + (1,), 77, np.uint8)], axis=2)
        H, W = rgb.shape[:2]
        raw = np.concatenate([np.zeros((H, 1), np.uint8), rgba.reshape(H, W * 4)], axis=1).tobytes()
        data = decode._SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0)) + \
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
        self.assertTrue(np.array_equal(decode.png_fast(data), rgb))
        self.assertTrue(np.array_equal(decode.png_fast(data), rgb))

    def test_path_input(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.png")
            data = decode.encode_png(image(8))
            with open(p, "wb") as fh:
                fh.write(data)
            self.assertTrue(np.array_equal(decode.png_fast(p), decode.png(p)))
            with self.assertRaises(VqaError) as cm:
                decode.png_fast(os.path.join(d, "missing.png"))
            self.assertEqual(cm.exception.code, "E_IO")

    def test_refusal_parity(self):
        good = decode.encode_png(image(9))
        ihdr16 = struct.pack(">IIBBBBB", 4, 4, 16, 2, 0, 0, 0)
        ihdr_il = struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 1)
        bad = {
            "notpng": b"hello world",
            "truncated": good[:-20],
            "crc": good[:30] + bytes([good[30] ^ 1]) + good[31:],
            "noiend": good[:-12],
            "depth16": decode._SIG + chunk(b"IHDR", ihdr16) + chunk(b"IDAT", zlib.compress(b"\0" * 100)) + chunk(b"IEND", b""),
            "interlaced": decode._SIG + chunk(b"IHDR", ihdr_il) + chunk(b"IDAT", zlib.compress(b"\0" * 100)) + chunk(b"IEND", b""),
            "size": decode._SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0)) +
                    chunk(b"IDAT", zlib.compress(b"\0" * 10)) + chunk(b"IEND", b""),
        }
        for name, data in bad.items():
            decode.reset_guard()
            with self.assertRaises(VqaError, msg=name) as c1:
                decode.png(data)
            for _ in range(2):
                with self.assertRaises(VqaError, msg=name) as c2:
                    decode.png_fast(data)
                self.assertEqual(c2.exception.code, c1.exception.code)

    def test_ancillary_chunk_uses_canonical(self):
        good = decode.encode_png(image(10))
        # inject gAMA after IHDR (offset 8 + 25)
        data = good[:33] + chunk(b"gAMA", struct.pack(">I", 45455)) + good[33:]
        self.assertFalse(decode._simple_png(data))
        decode.reset_guard()
        self.assertTrue(np.array_equal(decode.png_fast(data), decode.png(data)))
        self.assertEqual(decode.fast_state(), "unchecked")

    def test_cv2_missing_falls_back(self):
        data = decode.encode_png(image(11))
        with mock.patch.object(decode, "_CV2", None):
            decode.reset_guard()
            self.assertTrue(np.array_equal(decode.png_fast(data), decode.png(data)))
            self.assertEqual(decode.fast_state(), "off")

    @unittest.skipIf(cv2 is None, "cv2 absent")
    def test_guard_mismatch_disables(self):
        data = decode.encode_png(image(12))
        wrong = decode.png(data).copy()
        wrong[0, 0, 0] ^= 1
        with mock.patch.object(decode, "_cv2_decode", return_value=wrong):
            decode.reset_guard()
            ref = decode.png(data)
            self.assertTrue(np.array_equal(decode.png_fast(data), ref))      # canonical result returned
            self.assertEqual(decode.fast_state(), "off")
            self.assertTrue(np.array_equal(decode.png_fast(data), ref))      # stays off

    @unittest.skipIf(cv2 is None or os.environ.get("VQA_PERF") == "0", "perf skipped")
    def test_faster_720p(self):
        rgb = image(13, 720, 1280)
        data = cv_png(rgb, 3)
        decode.reset_guard()
        decode.png_fast(data)

        def best(fn, n=3):
            ts = []
            for _ in range(n):
                t = time.perf_counter()
                fn()
                ts.append(time.perf_counter() - t)
            return min(ts)
        slow, fast = best(lambda: decode.png(data)), best(lambda: decode.png_fast(data))
        print("decode 720p ms: png=%.1f png_fast=%.1f" % (slow * 1e3, fast * 1e3))
        self.assertLess(fast, slow)


if __name__ == "__main__":
    unittest.main()
