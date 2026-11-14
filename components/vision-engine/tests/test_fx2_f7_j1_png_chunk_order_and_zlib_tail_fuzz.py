"""Contract tests for fx2-f7-visioncore-decode-and-parallel-invariants:
PNG chunk ordering, IDAT split boundary matrix, ancillary chunk placement,
and zlib stream tail / concatenated zlib stream fuzzing.
"""

import struct
import unittest
import zlib

import numpy as np

from visioncore import decode
from visioncore.errors import VqaError

_SIG = b"\x89PNG\r\n\x1a\n"


def _make_chunk(tag: bytes, body: bytes) -> bytes:
    """Pack a PNG chunk: 4-byte big-endian length + 4-byte tag + body + 4-byte CRC32."""
    return (
        struct.pack(">I", len(body))
        + tag
        + body
        + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
    )


def _make_frame(W: int = 8, H: int = 8):
    """Build a deterministic RGB frame and reference PNG components in RAM."""
    np_rnd = np.random.RandomState(42)
    rgb = np_rnd.randint(0, 256, (H, W, 3), dtype=np.uint8)
    raw_scanlines = b"".join(b"\x00" + rgb[y].tobytes() for y in range(H))
    comp1 = zlib.compress(raw_scanlines)
    ihdr = _make_chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
    iend = _make_chunk(b"IEND", b"")
    text = _make_chunk(b"tEXt", b"Comment\x00Ancillary metadata payload")
    return rgb, comp1, ihdr, iend, text


class TestFx2F7J1PngChunkOrderAndZlibTailFuzz(unittest.TestCase):
    def setUp(self):
        self.rgb, self.comp1, self.ihdr, self.iend, self.text = _make_frame(8, 8)
        self.comp2 = zlib.compress(b"second complete hidden zlib stream payload 0123456789")

    # -------------------------------------------------------------------------
    # Passing Controls: Valid IDAT splits, Ancillary placement, Fast-path agreement
    # -------------------------------------------------------------------------

    def test_adjacent_idat_split_matrix_passing_controls(self):
        """Passing control: Valid adjacent IDAT split matrix decodes to identical pixels."""
        split_positions = [1, 5, len(self.comp1) // 3, len(self.comp1) // 2, len(self.comp1) - 5, len(self.comp1) - 1]
        for k in split_positions:
            idat1 = _make_chunk(b"IDAT", self.comp1[:k])
            idat2 = _make_chunk(b"IDAT", self.comp1[k:])
            png_bytes = _SIG + self.ihdr + idat1 + idat2 + self.iend

            # Canonical decode, simple check, and fast path agreement
            decoded = decode.png(png_bytes)
            self.assertTrue(np.array_equal(decoded, self.rgb))
            self.assertTrue(decode._simple_png(png_bytes))
            self.assertTrue(np.array_equal(decode.png_fast(png_bytes), self.rgb))

        # 3-way split
        k1, k2 = len(self.comp1) // 3, (2 * len(self.comp1)) // 3
        idat31 = _make_chunk(b"IDAT", self.comp1[:k1])
        idat32 = _make_chunk(b"IDAT", self.comp1[k1:k2])
        idat33 = _make_chunk(b"IDAT", self.comp1[k2:])
        png_3way = _SIG + self.ihdr + idat31 + idat32 + idat33 + self.iend

        self.assertTrue(np.array_equal(decode.png(png_3way), self.rgb))
        self.assertTrue(decode._simple_png(png_3way))
        self.assertTrue(np.array_equal(decode.png_fast(png_3way), self.rgb))

        # Negative control check: corrupt CRC on split chunk MUST be rejected
        bad_crc_idat = struct.pack(">I", len(self.comp1[k1:k2])) + b"IDAT" + self.comp1[k1:k2] + struct.pack(">I", 0xDEADBEEF)
        bad_png = _SIG + self.ihdr + idat31 + bad_crc_idat + idat33 + self.iend
        with self.assertRaises(VqaError) as cm:
            decode.png(bad_png)
        self.assertEqual(cm.exception.code, "E_VC_PNG")
        self.assertEqual(cm.exception.detail["reason"], "bad chunk crc/length")

    def test_ancillary_text_before_and_after_idat_run_passing_controls(self):
        """Passing control: tEXt before or after IDAT run decodes correctly; _simple_png returns False."""
        single_idat = _make_chunk(b"IDAT", self.comp1)

        # Control A: tEXt before IDAT run
        png_before = _SIG + self.ihdr + self.text + single_idat + self.iend
        self.assertTrue(np.array_equal(decode.png(png_before), self.rgb))
        self.assertFalse(decode._simple_png(png_before))
        self.assertTrue(np.array_equal(decode.png_fast(png_before), self.rgb))

        # Control B: tEXt after IDAT run
        png_after = _SIG + self.ihdr + single_idat + self.text + self.iend
        self.assertTrue(np.array_equal(decode.png(png_after), self.rgb))
        self.assertFalse(decode._simple_png(png_after))
        self.assertTrue(np.array_equal(decode.png_fast(png_after), self.rgb))

        # Control C: tEXt both before and after IDAT run
        text2 = _make_chunk(b"tEXt", b"Author\x00VisionEngine")
        png_both = _SIG + self.ihdr + self.text + single_idat + text2 + self.iend
        self.assertTrue(np.array_equal(decode.png(png_both), self.rgb))
        self.assertFalse(decode._simple_png(png_both))
        self.assertTrue(np.array_equal(decode.png_fast(png_both), self.rgb))

        # Negative control check: corrupt CRC on ancillary chunk MUST be rejected by _chunks()
        bad_text_crc = struct.pack(">I", len(b"Comment\x00Ancillary metadata payload")) + b"tEXt" + b"Comment\x00Ancillary metadata payload" + struct.pack(">I", 0xBAD00C32)
        bad_ancillary_png = _SIG + self.ihdr + bad_text_crc + single_idat + self.iend
        with self.assertRaises(VqaError) as cm:
            decode.png(bad_ancillary_png)
        self.assertEqual(cm.exception.code, "E_VC_PNG")
        self.assertEqual(cm.exception.detail["reason"], "bad chunk crc/length")

    def test_fast_path_eligibility_agreement(self):
        """Agreement check: png and _simple_png contract alignment on fast-path eligibility."""
        single_idat = _make_chunk(b"IDAT", self.comp1)
        plain_png = _SIG + self.ihdr + single_idat + self.iend
        ancillary_png = _SIG + self.ihdr + self.text + single_idat + self.iend

        # Plain PNG is fast-path eligible
        self.assertTrue(decode._simple_png(plain_png))
        self.assertTrue(np.array_equal(decode.png(plain_png), decode.png_fast(plain_png)))

        # Ancillary PNG is ineligible for fast path, falls back safely to png
        self.assertFalse(decode._simple_png(ancillary_png))
        self.assertTrue(np.array_equal(decode.png(ancillary_png), decode.png_fast(ancillary_png)))

    # -------------------------------------------------------------------------
    # Expected Failure Tests (B3 Contract / Deferred Fix fx2-f7-visioncore-decode-and-parallel-invariants)
    # -------------------------------------------------------------------------

    @unittest.expectedFailure
    def test_resumed_idat_run_after_ancillary_text_expected_failure(self):
        """fx2-f7-visioncore-decode-and-parallel-invariants: Resumed IDAT run after ancillary tEXt termination must raise E_VC_PNG."""
        k = len(self.comp1) // 2
        idat1 = _make_chunk(b"IDAT", self.comp1[:k])
        idat2 = _make_chunk(b"IDAT", self.comp1[k:])
        png_resumed = _SIG + self.ihdr + idat1 + self.text + idat2 + self.iend

        with self.assertRaises(VqaError) as cm:
            decode.png(png_resumed)
        self.assertEqual(cm.exception.code, "E_VC_PNG")

    @unittest.expectedFailure
    def test_second_zlib_stream_tail_hidden_in_single_idat_expected_failure(self):
        """fx2-f7-visioncore-decode-and-parallel-invariants: Second complete zlib stream appended in single IDAT payload must raise E_VC_PNG."""
        concat_idat = _make_chunk(b"IDAT", self.comp1 + self.comp2)
        png_concat = _SIG + self.ihdr + concat_idat + self.iend

        with self.assertRaises(VqaError) as cm:
            decode.png(png_concat)
        self.assertEqual(cm.exception.code, "E_VC_PNG")

    @unittest.expectedFailure
    def test_second_zlib_stream_at_adjacent_idat_split_boundaries_expected_failure(self):
        """fx2-f7-visioncore-decode-and-parallel-invariants: Second complete zlib stream hidden across adjacent IDAT split boundaries must raise E_VC_PNG."""
        split_positions = [1, 5, len(self.comp1) // 2, len(self.comp1) - 1]
        for k in split_positions:
            idat1 = _make_chunk(b"IDAT", self.comp1[:k])
            idat2 = _make_chunk(b"IDAT", self.comp1[k:] + self.comp2)
            png_split_zlib = _SIG + self.ihdr + idat1 + idat2 + self.iend

            with self.assertRaises(VqaError) as cm:
                decode.png(png_split_zlib)
            self.assertEqual(cm.exception.code, "E_VC_PNG")

    @unittest.expectedFailure
    def test_second_zlib_stream_in_separate_idat_chunk_expected_failure(self):
        """fx2-f7-visioncore-decode-and-parallel-invariants: Second complete zlib stream in a separate IDAT chunk must raise E_VC_PNG."""
        idat1 = _make_chunk(b"IDAT", self.comp1)
        idat2 = _make_chunk(b"IDAT", self.comp2)
        png_separate = _SIG + self.ihdr + idat1 + idat2 + self.iend

        with self.assertRaises(VqaError) as cm:
            decode.png(png_separate)
        self.assertEqual(cm.exception.code, "E_VC_PNG")

    @unittest.expectedFailure
    def test_second_zlib_stream_interleaved_with_ancillary_text_expected_failure(self):
        """fx2-f7-visioncore-decode-and-parallel-invariants: Second complete zlib stream combined with ancillary tEXt chunk must raise E_VC_PNG."""
        idat1 = _make_chunk(b"IDAT", self.comp1)
        idat2 = _make_chunk(b"IDAT", self.comp2)
        png_interleaved = _SIG + self.ihdr + idat1 + self.text + idat2 + self.iend

        with self.assertRaises(VqaError) as cm:
            decode.png(png_interleaved)
        self.assertEqual(cm.exception.code, "E_VC_PNG")


if __name__ == "__main__":
    unittest.main()
