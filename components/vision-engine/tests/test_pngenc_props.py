"""Fixed-seed property tests for synthetic PNG encoder vadapt/synth/pngenc.py."""
import struct
import unittest
import zlib

import numpy as np

from vadapt.synth import pngenc
from visioncore import decode
from visioncore.errors import VqaError


def _paeth_spec(a, b, c):
    """PNG specification Paeth predictor implementation for comparison."""
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    elif pb <= pc:
        return b
    else:
        return c


def _unfilter_row_in_test(ft, filtered_row, prev, bpp):
    """Unfilter a single row in test for verifying _filter_row property."""
    n = len(filtered_row)
    out = np.zeros(n, dtype=np.uint8)
    for i in range(n):
        a = int(out[i - bpp]) if i >= bpp else 0
        b = int(prev[i])
        c = int(prev[i - bpp]) if i >= bpp else 0
        if ft == 0:
            pred = 0
        elif ft == 1:
            pred = a
        elif ft == 2:
            pred = b
        elif ft == 3:
            pred = (a + b) // 2
        elif ft == 4:
            pred = _paeth_spec(a, b, c)
        else:
            raise ValueError(f"Invalid filter type {ft}")
        out[i] = (int(filtered_row[i]) + pred) & 0xFF
    return out


class TestPngEncProps(unittest.TestCase):

    def test_chunk_crc(self):
        tag = b"IHDR"
        data = struct.pack(">IIBBBBB", 10, 10, 8, 2, 0, 0, 0)
        chunk = pngenc._chunk(tag, data)

        length = struct.unpack(">I", chunk[:4])[0]
        self.assertEqual(length, len(data))
        self.assertEqual(chunk[4:8], tag)
        self.assertEqual(chunk[8:8 + length], data)

        expected_crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        actual_crc = struct.unpack(">I", chunk[8 + length:])[0]
        self.assertEqual(actual_crc, expected_crc)

    def test_chunk_negative(self):
        with self.assertRaises(TypeError):
            pngenc._chunk("IHDR", b"data")
        with self.assertRaises(TypeError):
            pngenc._chunk(b"IHDR", "data")

    def test_paeth_grid(self):
        grid_values = np.linspace(0, 255, 16, dtype=int)
        for a in grid_values:
            for b in grid_values:
                for c in grid_values:
                    actual = pngenc._paeth(int(a), int(b), int(c))
                    expected = _paeth_spec(int(a), int(b), int(c))
                    self.assertEqual(actual, expected)

    def test_paeth_negative(self):
        with self.assertRaises(TypeError):
            pngenc._paeth("a", 10, 20)
        with self.assertRaises(TypeError):
            pngenc._paeth(10, None, 20)

    def test_filter_row_identity(self):
        rng = np.random.default_rng(42)
        bpp = 3
        width = 17
        for ft in range(5):
            row = rng.integers(0, 256, size=width * bpp, dtype=np.uint8)
            prev = rng.integers(0, 256, size=width * bpp, dtype=np.uint8)
            filtered = pngenc._filter_row(ft, row, prev, bpp)
            unfiltered = _unfilter_row_in_test(ft, filtered, prev, bpp)
            np.testing.assert_array_equal(unfiltered, row)

    def test_filter_row_negative(self):
        row = np.array([10, 20, 30], dtype=np.uint8)
        prev = np.array([5, 15, 25], dtype=np.uint8)
        with self.assertRaises(IndexError):
            pngenc._filter_row(5, row, prev, 3)
        with self.assertRaises(IndexError):
            pngenc._filter_row(-6, row, prev, 3)

    def test_encode_png_roundtrip(self):
        for seed in range(10):
            rng = np.random.default_rng(seed)
            for h in range(1, 18):
                for w in range(1, 10):
                    rgb = rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)
                    alpha = rng.integers(0, 256, size=(h, w), dtype=np.uint8)
                    for filt in range(-1, 5):
                        enc_bytes = pngenc.encode_png(rgb, filt=filt)
                        decoded_rgb = decode.png(enc_bytes)
                        np.testing.assert_array_equal(decoded_rgb, rgb)

                        enc_rgba_bytes = pngenc.encode_png(rgb, filt=filt, alpha=alpha)
                        decoded_rgba = decode.png(enc_rgba_bytes)
                        np.testing.assert_array_equal(decoded_rgba, rgb)

    def test_encode_png_negative_filter(self):
        rgb = np.zeros((4, 4, 3), dtype=np.uint8)
        with self.assertRaises(IndexError):
            pngenc.encode_png(rgb, filt=5)

    def test_encode_png_negative_dtype(self):
        rgb_float = np.ones((4, 4, 3), dtype=np.float32)
        encoded_float = pngenc.encode_png(rgb_float, filt=0)
        with self.assertRaises(VqaError) as ctx:
            decode.png(encoded_float)
        self.assertEqual(ctx.exception.code, "E_VC_PNG")

    def test_encode_png_negative_alpha_shape(self):
        rgb = np.zeros((4, 4, 3), dtype=np.uint8)
        wrong_alpha = np.zeros((3, 4), dtype=np.uint8)
        with self.assertRaises(ValueError):
            pngenc.encode_png(rgb, alpha=wrong_alpha)


if __name__ == "__main__":
    unittest.main()
