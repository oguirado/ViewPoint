"""Adversarial tests for malformed PNG chunk families in visioncore/decode.py."""
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

import numpy as np

from visioncore import decode
from visioncore.errors import VqaError


def chunk(t, b):
    """Pack a PNG chunk: 4-byte big-endian length + 4-byte type + body + 4-byte CRC32."""
    return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)


_SIG = b"\x89PNG\r\n\x1a\n"


def make_valid_png(W=2, H=2):
    """Build a valid 8-bit RGB non-interlaced PNG in RAM using struct and zlib."""
    ihdr_body = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)
    ihdr = chunk(b"IHDR", ihdr_body)
    raw_scanlines = bytes([0] + [0] * (W * 3)) * H
    idat = chunk(b"IDAT", zlib.compress(raw_scanlines))
    iend = chunk(b"IEND", b"")
    return _SIG + ihdr + idat + iend


class TestAdvPngChunkFamilies(unittest.TestCase):
    # -------------------------------------------------------------------------
    # Bug Family B3: Accepted malformed PNGs (recorded as @unittest.expectedFailure)
    # -------------------------------------------------------------------------

    @unittest.expectedFailure
    def test_ihdr_not_first(self):
        """B3: png() accepts malformed PNG where IHDR is not the first chunk."""
        W, H = 2, 2
        ihdr_chunk = chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
        gama_chunk = chunk(b"gAMA", struct.pack(">I", 45455))
        raw_scanlines = bytes([0] + [0] * (W * 3)) * H
        idat_chunk = chunk(b"IDAT", zlib.compress(raw_scanlines))
        iend_chunk = chunk(b"IEND", b"")

        png_bytes = _SIG + gama_chunk + ihdr_chunk + idat_chunk + iend_chunk
        with self.assertRaises(VqaError):
            decode.png(png_bytes)

    @unittest.expectedFailure
    def test_chunks_after_iend(self):
        """B3: png() accepts malformed PNG with trailing chunks after IEND."""
        valid_png = make_valid_png(2, 2)
        text_chunk = chunk(b"tEXt", b"Comment\x00Trailing chunk")
        png_bytes = valid_png + text_chunk
        with self.assertRaises(VqaError):
            decode.png(png_bytes)

    @unittest.expectedFailure
    def test_duplicate_iend(self):
        """B3: png() accepts malformed PNG with duplicate IEND chunks."""
        valid_png = make_valid_png(2, 2)
        iend_chunk = chunk(b"IEND", b"")
        png_bytes = valid_png + iend_chunk
        with self.assertRaises(VqaError):
            decode.png(png_bytes)

    @unittest.expectedFailure
    def test_non_contiguous_idat_runs(self):
        """B3: png() accepts non-contiguous IDAT chunks separated by ancillary chunks."""
        W, H = 2, 2
        ihdr_chunk = chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
        raw_scanlines = bytes([0] + [0] * (W * 3)) * H
        comp = zlib.compress(raw_scanlines)
        mid = len(comp) // 2
        idat1 = chunk(b"IDAT", comp[:mid])
        gama_chunk = chunk(b"gAMA", struct.pack(">I", 45455))
        idat2 = chunk(b"IDAT", comp[mid:])
        iend_chunk = chunk(b"IEND", b"")

        png_bytes = _SIG + ihdr_chunk + idat1 + gama_chunk + idat2 + iend_chunk
        with self.assertRaises(VqaError):
            decode.png(png_bytes)

    @unittest.expectedFailure
    def test_unknown_critical_chunk_type(self):
        """B3: png() accepts malformed PNG with an unknown critical chunk type."""
        W, H = 2, 2
        ihdr_chunk = chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
        unknown_crit = chunk(b"CXXX", b"Unknown critical chunk payload")
        raw_scanlines = bytes([0] + [0] * (W * 3)) * H
        idat_chunk = chunk(b"IDAT", zlib.compress(raw_scanlines))
        iend_chunk = chunk(b"IEND", b"")

        png_bytes = _SIG + ihdr_chunk + unknown_crit + idat_chunk + iend_chunk
        with self.assertRaises(VqaError):
            decode.png(png_bytes)

    @unittest.expectedFailure
    def test_zlib_trailing_garbage(self):
        """B3: png() accepts IDAT payload with trailing garbage after zlib stream end."""
        W, H = 2, 2
        ihdr_chunk = chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
        raw_scanlines = bytes([0] + [0] * (W * 3)) * H
        comp = zlib.compress(raw_scanlines)
        idat_chunk = chunk(b"IDAT", comp + b"\x12\x34\x56\x78ExtraGarbageData")
        iend_chunk = chunk(b"IEND", b"")

        png_bytes = _SIG + ihdr_chunk + idat_chunk + iend_chunk
        with self.assertRaises(VqaError):
            decode.png(png_bytes)

    # -------------------------------------------------------------------------
    # Guarantees: Rejected malformed PNGs (plain passing tests)
    # -------------------------------------------------------------------------

    def test_iend_missing(self):
        """Guarantee: png() rejects PNG missing an IEND chunk."""
        W, H = 2, 2
        ihdr_chunk = chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
        raw_scanlines = bytes([0] + [0] * (W * 3)) * H
        idat_chunk = chunk(b"IDAT", zlib.compress(raw_scanlines))
        text_chunk = chunk(b"tEXt", b"Comment\x00No IEND here")

        png_bytes = _SIG + ihdr_chunk + idat_chunk + text_chunk
        with self.assertRaises(VqaError) as cm:
            decode.png(png_bytes)
        self.assertEqual(cm.exception.code, "E_VC_PNG")
        self.assertEqual(cm.exception.detail["reason"], "missing IEND")

        # Path input variant
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "missing_iend.png"
            p.write_bytes(png_bytes)
            with self.assertRaises(VqaError) as cm_path:
                decode.png(p)
            self.assertEqual(cm_path.exception.code, "E_VC_PNG")
            self.assertEqual(cm_path.exception.detail["reason"], "missing IEND")

    def test_zero_length_idat_only(self):
        """Guarantee: png() rejects PNG containing only zero-length IDAT chunks."""
        W, H = 2, 2
        ihdr_chunk = chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
        idat_empty = chunk(b"IDAT", b"")
        iend_chunk = chunk(b"IEND", b"")

        png_bytes = _SIG + ihdr_chunk + idat_empty + iend_chunk
        with self.assertRaises(VqaError) as cm:
            decode.png(png_bytes)
        self.assertEqual(cm.exception.code, "E_VC_PNG")
        self.assertEqual(cm.exception.detail["reason"], "inflated size mismatch")

    def test_crc_mismatch_critical_and_ancillary(self):
        """Guarantee: png() and _chunks() reject CRC mismatch in critical or ancillary chunks."""
        W, H = 2, 2
        ihdr_bytes = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)
        raw_scanlines = bytes([0] + [0] * (W * 3)) * H
        idat_chunk = chunk(b"IDAT", zlib.compress(raw_scanlines))
        iend_chunk = chunk(b"IEND", b"")

        # 1. Critical chunk CRC corrupt (IHDR)
        bad_crc_ihdr = struct.pack(">I", len(ihdr_bytes)) + b"IHDR" + ihdr_bytes + struct.pack(">I", 0xDEADBEEF)
        png_bad_crit = _SIG + bad_crc_ihdr + idat_chunk + iend_chunk
        with self.assertRaises(VqaError) as cm1:
            decode.png(png_bad_crit)
        self.assertEqual(cm1.exception.code, "E_VC_PNG")
        self.assertEqual(cm1.exception.detail["reason"], "bad chunk crc/length")

        # 2. Ancillary chunk CRC corrupt (gAMA)
        gama_body = struct.pack(">I", 45455)
        bad_crc_gama = struct.pack(">I", len(gama_body)) + b"gAMA" + gama_body + struct.pack(">I", 0xDEADBEEF)
        png_bad_anc = _SIG + chunk(b"IHDR", ihdr_bytes) + bad_crc_gama + idat_chunk + iend_chunk
        with self.assertRaises(VqaError) as cm2:
            decode.png(png_bad_anc)
        self.assertEqual(cm2.exception.code, "E_VC_PNG")
        self.assertEqual(cm2.exception.detail["reason"], "bad chunk crc/length")

    def test_length_field_larger_than_remaining_bytes(self):
        """Guarantee: _chunks() rejects chunk length exceeding remaining input length."""
        W, H = 2, 2
        ihdr_chunk = chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
        truncated_chunk = struct.pack(">I", 1000) + b"tEXt" + b"short payload"
        png_bytes = _SIG + ihdr_chunk + truncated_chunk

        with self.assertRaises(VqaError) as cm:
            decode.png(png_bytes)
        self.assertEqual(cm.exception.code, "E_VC_PNG")
        self.assertEqual(cm.exception.detail["reason"], "bad chunk crc/length")

    def test_length_2_to_31(self):
        """Guarantee: _chunks() rejects chunk length of 2^31."""
        W, H = 2, 2
        ihdr_chunk = chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
        len_2_31_chunk = struct.pack(">I", 2147483648) + b"tEXt" + b"data"
        png_bytes = _SIG + ihdr_chunk + len_2_31_chunk

        with self.assertRaises(VqaError) as cm:
            decode.png(png_bytes)
        self.assertEqual(cm.exception.code, "E_VC_PNG")
        self.assertEqual(cm.exception.detail["reason"], "bad chunk crc/length")

    def test_invalid_bit_depth_and_colour_type_pairs(self):
        """Guarantee: png() rejects invalid bit depth and colour type combinations."""
        W, H = 2, 2
        raw_scanlines = bytes([0] + [0] * (W * 3)) * H
        idat_chunk = chunk(b"IDAT", zlib.compress(raw_scanlines))
        iend_chunk = chunk(b"IEND", b"")

        invalid_pairs = [
            (1, 2),   # 1-bit RGB (invalid, RGB must be 8 or 16)
            (16, 2),  # 16-bit RGB (unsupported by vc.decode)
            (8, 0),   # 8-bit Greyscale (unsupported by vc.decode)
            (8, 3),   # 8-bit Indexed Palette (unsupported by vc.decode)
            (2, 6),   # 2-bit RGBA (invalid)
        ]
        for depth, ctype in invalid_pairs:
            ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, depth, ctype, 0, 0, 0))
            png_bytes = _SIG + ihdr + idat_chunk + iend_chunk
            with self.assertRaises(VqaError) as cm:
                decode.png(png_bytes)
            self.assertEqual(cm.exception.code, "E_VC_PNG")
            self.assertIn("unsupported depth=", cm.exception.detail["reason"])

    def test_interlace_1(self):
        """Guarantee: png() rejects interlaced PNGs (interlace method 1)."""
        W, H = 2, 2
        ihdr_chunk = chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 1))
        raw_scanlines = bytes([0] + [0] * (W * 3)) * H
        idat_chunk = chunk(b"IDAT", zlib.compress(raw_scanlines))
        iend_chunk = chunk(b"IEND", b"")

        png_bytes = _SIG + ihdr_chunk + idat_chunk + iend_chunk
        with self.assertRaises(VqaError) as cm:
            decode.png(png_bytes)
        self.assertEqual(cm.exception.code, "E_VC_PNG")
        self.assertIn("interlace=1", cm.exception.detail["reason"])

    def test_filter_type_5_in_scanline(self):
        """Guarantee: png() rejects scanline filter type 5."""
        W, H = 2, 2
        ihdr_chunk = chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
        # Scanlines with invalid filter byte 5
        raw_scanlines = bytes([5] + [0] * (W * 3)) * H
        idat_chunk = chunk(b"IDAT", zlib.compress(raw_scanlines))
        iend_chunk = chunk(b"IEND", b"")

        png_bytes = _SIG + ihdr_chunk + idat_chunk + iend_chunk
        with self.assertRaises(VqaError) as cm:
            decode.png(png_bytes)
        self.assertEqual(cm.exception.code, "E_VC_PNG")
        self.assertEqual(cm.exception.detail["reason"], "filter type 5")

    def test_zlib_stream_truncated(self):
        """Guarantee: png() rejects truncated zlib stream inside IDAT."""
        W, H = 2, 2
        ihdr_chunk = chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
        raw_scanlines = bytes([0] + [0] * (W * 3)) * H
        comp = zlib.compress(raw_scanlines)
        idat_chunk = chunk(b"IDAT", comp[:len(comp) // 2])
        iend_chunk = chunk(b"IEND", b"")

        png_bytes = _SIG + ihdr_chunk + idat_chunk + iend_chunk
        with self.assertRaises(VqaError) as cm:
            decode.png(png_bytes)
        self.assertEqual(cm.exception.code, "E_VC_PNG")
        self.assertIn(cm.exception.detail["reason"], ("inflated size mismatch", "zlib:"))

    def test_width_or_height_zero(self):
        """Guarantee: png() rejects PNG with width or height equal to zero."""
        idat_chunk = chunk(b"IDAT", zlib.compress(b""))
        iend_chunk = chunk(b"IEND", b"")

        zero_dims = [(0, 2), (2, 0), (0, 0)]
        for W, H in zero_dims:
            ihdr_chunk = chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
            png_bytes = _SIG + ihdr_chunk + idat_chunk + iend_chunk
            with self.assertRaises(VqaError) as cm:
                decode.png(png_bytes)
            self.assertEqual(cm.exception.code, "E_VC_PNG")
            self.assertIn("dimensions ", cm.exception.detail["reason"])

    def test_width_times_height_overflow(self):
        """Guarantee: png() rejects PNG with dimensions exceeding maximum limits or overflowing."""
        idat_chunk = chunk(b"IDAT", zlib.compress(b""))
        iend_chunk = chunk(b"IEND", b"")

        overflow_dims = [(8193, 2), (2, 8193), (65536, 65536)]
        for W, H in overflow_dims:
            ihdr_chunk = chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
            png_bytes = _SIG + ihdr_chunk + idat_chunk + iend_chunk
            with self.assertRaises(VqaError) as cm:
                decode.png(png_bytes)
            self.assertEqual(cm.exception.code, "E_VC_PNG")
            self.assertIn("dimensions ", cm.exception.detail["reason"])


if __name__ == "__main__":
    unittest.main()
