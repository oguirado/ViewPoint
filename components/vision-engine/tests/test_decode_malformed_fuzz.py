"""Fixed-seed fuzz and negative-path tests for visioncore/decode.py."""
import random
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

import numpy as np

from visioncore import decode
from visioncore.errors import VqaError


class TestDecodeMalformedFuzz(unittest.TestCase):
    def test_bad_function(self):
        """Test _bad at line 14."""
        err = decode._bad("test_reason_123")
        self.assertIsInstance(err, VqaError)
        self.assertEqual(err.code, "E_VC_PNG")
        self.assertEqual(err.detail, {"reason": "test_reason_123"})
        self.assertEqual(err.rc, 3)

        # Negative check: confirm error details and code are specific
        self.assertNotEqual(err.code, "OK")
        self.assertIn("reason", err.detail)

    def test_chunks_function(self):
        """Test _chunks at line 18 with valid and malformed inputs."""
        rgb = np.arange(12, dtype=np.uint8).reshape(2, 2, 3)
        data = decode.encode_png(rgb)

        # Yields exact chunk list of a valid file
        chunks = list(decode._chunks(data))
        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0][0], b"IHDR")
        self.assertEqual(chunks[1][0], b"IDAT")
        self.assertEqual(chunks[2][0], b"IEND")
        self.assertEqual(len(chunks[0][1]), 13)

        # Negative path 1: bad chunk CRC
        corrupt_crc = bytearray(data)
        corrupt_crc[20] ^= 0xFF
        with self.assertRaises(VqaError) as cm:
            list(decode._chunks(bytes(corrupt_crc)))
        self.assertEqual(cm.exception.code, "E_VC_PNG")
        self.assertEqual(cm.exception.detail["reason"], "bad chunk crc/length")

        # Negative path 2: data ending before PNG header signature completes
        short_data = data[:12]
        self.assertEqual(list(decode._chunks(short_data)), [])

    def test_wavefront_and_unfilter(self):
        """Test _wavefront at line 32 and unfilter at line 68."""
        H, W, bpp = 5, 4, 3
        # Create random filtered rows using all 5 filter types (0, 1, 2, 3, 4)
        rng = np.random.RandomState(42)
        ft_col = np.array([0, 1, 2, 3, 4], dtype=np.uint8).reshape(5, 1)
        data_cols = rng.randint(0, 256, (5, W * bpp), dtype=np.uint8)
        a = np.hstack([ft_col, data_cols])

        wf_res = decode._wavefront(a, H, W, bpp)
        raw_bytes = a.tobytes()
        uf_res = decode.unfilter(raw_bytes, H, W * bpp, bpp).reshape(H, W, bpp)

        # Assert _wavefront gives the same result as unfilter
        self.assertTrue(np.array_equal(wf_res, uf_res))
        self.assertEqual(wf_res.shape, (H, W, bpp))
        self.assertEqual(wf_res.dtype, np.uint8)

        # Negative path for _wavefront: invalid filter type (> 4)
        a_bad = a.copy()
        a_bad[0, 0] = 5
        with self.assertRaises(VqaError) as cm:
            decode._wavefront(a_bad, H, W, bpp)
        self.assertEqual(cm.exception.code, "E_VC_PNG")
        self.assertEqual(cm.exception.detail["reason"], "filter type 5")

        # Negative path for unfilter: invalid filter type (> 4) when _wavefront is not triggered
        a_bad_uf = np.hstack([np.array([0, 1, 2, 5], dtype=np.uint8).reshape(4, 1), rng.randint(0, 256, (4, W * bpp), dtype=np.uint8)])
        with self.assertRaises(VqaError) as cm:
            decode.unfilter(a_bad_uf.tobytes(), 4, W * bpp, bpp)
        self.assertEqual(cm.exception.code, "E_VC_PNG")
        self.assertEqual(cm.exception.detail["reason"], "filter type 5")

    def test_png_fuzz_mutations(self):
        """Test png at line 93 with random fixed seeds 0 to 4 for truncation, single byte flip, drop IEND, corrupt CRC & depth/color."""
        for seed in range(5):
            rnd = random.Random(seed)
            np_rnd = np.random.RandomState(seed)

            H, W = rnd.randint(1, 8), rnd.randint(1, 8)
            rgb = np_rnd.randint(0, 256, (H, W, 3), dtype=np.uint8)
            valid_png = decode.encode_png(rgb)

            # Sanity check: valid png decodes correctly
            decoded = decode.png(valid_png)
            self.assertTrue(np.array_equal(decoded, rgb))

            # 1. Truncate
            trunc_len = rnd.randint(1, len(valid_png) - 1)
            mut_trunc = valid_png[:trunc_len]
            with self.assertRaises(VqaError) as cm:
                res = decode.png(mut_trunc)
                self.assertFalse(np.array_equal(res, rgb))
            self.assertEqual(cm.exception.code, "E_VC_PNG")
            self.assertIn(cm.exception.detail["reason"], (
                "not a png", "missing IHDR/IDAT", "missing IEND", "bad chunk crc/length", "inflated size mismatch"
            ))

            # 2. Flip one byte
            mut_flip = bytearray(valid_png)
            idx = rnd.randint(0, len(mut_flip) - 1)
            mut_flip[idx] ^= 0xFF
            mut_flip = bytes(mut_flip)
            with self.assertRaises(VqaError) as cm:
                res = decode.png(mut_flip)
                self.assertFalse(np.array_equal(res, rgb))
            self.assertEqual(cm.exception.code, "E_VC_PNG")

            # 3. Drop IEND
            mut_drop_iend = valid_png[:-12]
            with self.assertRaises(VqaError) as cm:
                res = decode.png(mut_drop_iend)
                self.assertFalse(np.array_equal(res, rgb))
            self.assertEqual(cm.exception.code, "E_VC_PNG")
            self.assertEqual(cm.exception.detail["reason"], "missing IEND")

            # 4. Corrupt chunk CRC and change bit depth or colour type
            mut_corrupt = bytearray(valid_png)
            mut_corrupt[24] = 16  # change bit depth to 16
            mut_corrupt[25] = 0   # change colour type to 0
            mut_corrupt[29] ^= 0xFF  # corrupt chunk CRC of IHDR
            mut_corrupt = bytes(mut_corrupt)
            with self.assertRaises(VqaError) as cm:
                res = decode.png(mut_corrupt)
                self.assertFalse(np.array_equal(res, rgb))
            self.assertEqual(cm.exception.code, "E_VC_PNG")
            self.assertEqual(cm.exception.detail["reason"], "bad chunk crc/length")

    @unittest.expectedFailure
    def test_png_fuzz_duplicate_ihdr_bug(self):
        """Duplicate IHDR mutation exposes a bug: png() accepts duplicate IHDR without raising VqaError."""
        for seed in range(5):
            rnd = random.Random(seed)
            np_rnd = np.random.RandomState(seed)
            H, W = rnd.randint(1, 8), rnd.randint(1, 8)
            rgb = np_rnd.randint(0, 256, (H, W, 3), dtype=np.uint8)
            valid_png = decode.encode_png(rgb)

            ihdr_chunk = valid_png[8:33]
            mut_dup_ihdr = valid_png[:33] + ihdr_chunk + valid_png[33:]
            with self.assertRaises(VqaError):
                decode.png(mut_dup_ihdr)

    def test_png_negative_paths(self):
        """Negative paths for png() function."""
        # Non-existent file path
        with tempfile.TemporaryDirectory() as d:
            missing_path = Path(d) / "does_not_exist.png"
            with self.assertRaises(VqaError) as cm:
                decode.png(missing_path)
            self.assertEqual(cm.exception.code, "E_IO")

        # Not a PNG header
        with self.assertRaises(VqaError) as cm:
            decode.png(b"12345678NOTPNG")
        self.assertEqual(cm.exception.code, "E_VC_PNG")
        self.assertEqual(cm.exception.detail["reason"], "not a png")

        # Missing IHDR or IDAT
        header_only = decode._SIG + struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND"))
        with self.assertRaises(VqaError) as cm:
            decode.png(header_only)
        self.assertEqual(cm.exception.code, "E_VC_PNG")
        self.assertEqual(cm.exception.detail["reason"], "missing IHDR/IDAT")


if __name__ == "__main__":
    unittest.main()
