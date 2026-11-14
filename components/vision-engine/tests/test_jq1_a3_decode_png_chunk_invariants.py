"""Unit tests for visioncore.decode PNG chunk invariants (jq1-a3)."""
import struct
import unittest
import zlib

import numpy as np

from visioncore import decode
from visioncore.errors import VqaError

_SIG = b"\x89PNG\r\n\x1a\n"


def _make_chunk(tag: bytes, body: bytes) -> bytes:
    """Helper to build a PNG chunk with proper 4-byte big-endian length and CRC32."""
    return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)


class TestJq1A3DecodePngChunkInvariants(unittest.TestCase):
    def setUp(self):
        # Deterministic 4x4 3-channel uint8 buffer
        self.buf = np.arange(48, dtype=np.uint8).reshape(4, 4, 3)
        self.png_bytes = decode.encode_png(self.buf)

        # Extract IHDR and IDAT chunk bodies from encoded PNG bytes
        pos = 8
        self.chunks = []
        while pos < len(self.png_bytes):
            ln, typ = struct.unpack_from(">I4s", self.png_bytes, pos)
            body = self.png_bytes[pos + 8 : pos + 8 + ln]
            self.chunks.append((typ, body))
            pos += 12 + ln

        self.ihdr_valid = self.chunks[0][1]
        self.idat_body = self.chunks[1][1]

    @unittest.expectedFailure
    def test_b3_initial_invalid_ihdr_followed_by_valid_ihdr(self):
        """B3: Initial IHDR with dimensions 9999x9999 followed by valid 4x4 IHDR should raise E_VC_PNG."""
        ihdr_9999 = struct.pack(">IIBBBBB", 9999, 9999, 8, 2, 0, 0, 0)
        bad_png = (
            _SIG
            + _make_chunk(b"IHDR", ihdr_9999)
            + _make_chunk(b"IHDR", self.ihdr_valid)
            + _make_chunk(b"IDAT", self.idat_body)
            + _make_chunk(b"IEND", b"")
        )
        with self.assertRaises(VqaError) as cm:
            decode.png(bad_png)
        self.assertEqual(cm.exception.code, "E_VC_PNG")

    @unittest.expectedFailure
    def test_b3_valid_ihdr_duplicated_after_idat(self):
        """B3: Valid IHDR duplicated after IDAT but before IEND should raise E_VC_PNG."""
        bad_png = (
            _SIG
            + _make_chunk(b"IHDR", self.ihdr_valid)
            + _make_chunk(b"IDAT", self.idat_body)
            + _make_chunk(b"IHDR", self.ihdr_valid)
            + _make_chunk(b"IEND", b"")
        )
        with self.assertRaises(VqaError) as cm:
            decode.png(bad_png)
        self.assertEqual(cm.exception.code, "E_VC_PNG")

    @unittest.expectedFailure
    def test_b3b_appended_bytes_after_iend(self):
        """B3b: Sixteen non-PNG bytes appended after IEND should raise E_VC_PNG."""
        bad_png = self.png_bytes + b"0123456789abcdef"
        with self.assertRaises(VqaError) as cm:
            decode.png(bad_png)
        self.assertEqual(cm.exception.code, "E_VC_PNG")

    def test_passing_control_split_idat_chunks(self):
        """Passing control: Split IDAT body into 1-byte IDAT chunks with recomputed CRCs; decode should succeed."""
        idat_chunks = [
            _make_chunk(b"IDAT", self.idat_body[i : i + 1])
            for i in range(len(self.idat_body))
        ]
        valid_png = (
            _SIG
            + _make_chunk(b"IHDR", self.ihdr_valid)
            + b"".join(idat_chunks)
            + _make_chunk(b"IEND", b"")
        )
        decoded = decode.png(valid_png)
        self.assertTrue(np.array_equal(decoded, self.buf))

    @unittest.expectedFailure
    def test_b3_classifier_gap_simple_png_post_idat_duplicate_ihdr(self):
        """B3: Classifier gap pin: _simple_png should return False for post-IDAT duplicate IHDR header."""
        bad_png = (
            _SIG
            + _make_chunk(b"IHDR", self.ihdr_valid)
            + _make_chunk(b"IDAT", self.idat_body)
            + _make_chunk(b"IHDR", self.ihdr_valid)
            + _make_chunk(b"IEND", b"")
        )
        self.assertFalse(decode._simple_png(bad_png))


if __name__ == "__main__":
    unittest.main()
