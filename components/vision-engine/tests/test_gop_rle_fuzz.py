"""Fixed-seed fuzzing and boundary unit tests for S5 temporal codec primitives in visioncore/gop.py."""
import random
import struct
import unittest

from visioncore import gop
from visioncore.errors import VqaError


class TestGopRleFuzz(unittest.TestCase):
    def test_rle_fuzz_seeds_0_to_4(self):
        """Generate 200 random byte masks per seed for seeds 0..4, asserting identity and rle_bound."""
        total_runs = 0
        for seed in range(5):
            rng = random.Random(seed)
            for _ in range(200):
                # Varied lengths from 0 to 1000 bytes with different zero densities
                n = rng.randint(0, 1000)
                density = rng.choice([0.0, 0.01, 0.1, 0.5, 0.9, 1.0])
                raw_bytes = bytearray(n)
                for i in range(n):
                    if rng.random() >= density:
                        raw_bytes[i] = rng.randint(1, 255)
                mask = bytes(raw_bytes)

                bound = gop.rle_bound(n)
                encoded = gop.rle_encode(mask)
                self.assertLessEqual(len(encoded), bound)

                decoded = gop.rle_decode(encoded, n)
                self.assertEqual(decoded, mask)
                total_runs += 1

        self.assertEqual(total_runs, 1000)

    def test_rle_bound(self):
        """Exact value test and negative case for rle_bound."""
        self.assertEqual(gop.rle_bound(0), 2)
        self.assertEqual(gop.rle_bound(254), 256)
        self.assertEqual(gop.rle_bound(255), 259)
        self.assertEqual(gop.rle_bound(510), 516)
        self.assertEqual(gop.rle_bound(512), 518)

        # Negative case: invalid input type
        with self.assertRaises(TypeError):
            gop.rle_bound("invalid_length")

    def test_rle_encode(self):
        """Exact byte encoding test and negative case for rle_encode."""
        # Single byte non-zero: [0 zeros, 1 literal, 0x07] -> b"\x00\x01\x07"
        self.assertEqual(gop.rle_encode(b"\x07"), b"\x00\x01\x07")

        # Two zeros: [2 zeros, 0 literals] -> b"\x02\x00"
        self.assertEqual(gop.rle_encode(b"\x00\x00"), b"\x02\x00")

        # Negative case: non-iterable / wrong type input
        with self.assertRaises(TypeError):
            gop.rle_encode(None)

    def test_rle_decode(self):
        """Exact decoding tests and negative error cases for rle_decode."""
        # Exact valid decode
        encoded = b"\x00\x04\x01\x02\x03\x04"
        self.assertEqual(gop.rle_decode(encoded, 4), b"\x01\x02\x03\x04")

        # Negative case 1: Truncated token header
        with self.assertRaises(VqaError) as cm:
            gop.rle_decode(b"\x01", 1)
        self.assertEqual(cm.exception.code, "E_DELTA_TOKEN")

        # Negative case 2: Literal overrun
        with self.assertRaises(VqaError) as cm:
            gop.rle_decode(b"\x00\x05\x01\x02", 5)
        self.assertEqual(cm.exception.code, "E_DELTA_TOKEN")

        # Negative case 3: Decoded length mismatch
        with self.assertRaises(VqaError) as cm:
            gop.rle_decode(b"\x00\x01\x07", 2)
        self.assertEqual(cm.exception.code, "E_DELTA_LEN")

        # Negative case 4: Input length exceeds rle_bound
        bad_buf = b"\x00" * (gop.rle_bound(4) + 1)
        with self.assertRaises(VqaError) as cm:
            gop.rle_decode(bad_buf, 4)
        self.assertEqual(cm.exception.code, "E_DELTA_BOUND")

    def test_zlib_bound(self):
        """Exact value test and negative case for zlib_bound."""
        self.assertEqual(gop.zlib_bound(0), 13)
        self.assertEqual(gop.zlib_bound(4096), 4110)
        self.assertEqual(gop.zlib_bound(100000), 100043)

        # Negative case: invalid input type
        with self.assertRaises(TypeError):
            gop.zlib_bound(None)

    def test_max_gop_bytes(self):
        """Exact worst-case size calculation and negative case for max_gop_bytes."""
        # For gw=16, gh=10 (160 tiles), cells produce n_mb = 10 macroblocks.
        # N = 160, K = 20, gw = 16, gh = 10
        val = gop.max_gop_bytes(160, 20, 16, 10)
        self.assertEqual(val, 104324)

        # Negative case: invalid grid size causing indexing failure in _cells
        with self.assertRaises(IndexError):
            gop.max_gop_bytes(0, 10, 0, 0)

    def test_parse_header(self):
        """Header parsing with exact dict value verification and negative cases for corrupted headers."""
        # Construct valid 48-byte header
        valid_hdr = gop._pack_header(16, 10, 16, 20, 0, 5, 0, 100, 200, 0x12345678, 0x87654321)
        parsed = gop.parse_header(valid_hdr)
        self.assertEqual(parsed["magic"], b"VGOP")
        self.assertEqual(parsed["ver"], 1)
        self.assertEqual(parsed["hdr_len"], 48)
        self.assertEqual(parsed["gw"], 16)
        self.assertEqual(parsed["gh"], 10)
        self.assertEqual(parsed["tile_px"], 16)
        self.assertEqual(parsed["K"], 20)
        self.assertEqual(parsed["f0"], 0)
        self.assertEqual(parsed["n_frames"], 5)
        self.assertEqual(parsed["flags"], 0)
        self.assertEqual(parsed["izlen"], 100)
        self.assertEqual(parsed["body_len"], 200)
        self.assertEqual(parsed["key_sha8"], 0x12345678)
        self.assertEqual(parsed["body_sha8"], 0x87654321)

        # Negative case 1: Truncated header
        with self.assertRaises(VqaError) as cm:
            gop.parse_header(valid_hdr[:40])
        self.assertEqual(cm.exception.code, "E_GOP_MAGIC")

        # Negative case 2: Corrupted magic
        bad_magic = gop._pack_header(16, 10, 16, 20, 0, 5, 0, 100, 200, 0, 0)
        bad_magic = b"BADM" + bad_magic[4:]
        with self.assertRaises(VqaError) as cm:
            gop.parse_header(bad_magic)
        self.assertEqual(cm.exception.code, "E_GOP_MAGIC")

        # Negative case 3: Invalid version
        bad_ver = struct.pack("<4sH", b"VGOP", 2) + valid_hdr[6:]
        with self.assertRaises(VqaError) as cm:
            gop.parse_header(bad_ver)
        self.assertEqual(cm.exception.code, "E_GOP_VERSION")

        # Negative case 4: Invalid grid dimensions
        bad_grid = gop._pack_header(0, 10, 16, 20, 0, 5, 0, 100, 200, 0, 0)
        with self.assertRaises(VqaError) as cm:
            gop.parse_header(bad_grid)
        self.assertEqual(cm.exception.code, "E_GOP_GRID")

        # Negative case 5: Reserved flag bits set
        bad_flags = gop._pack_header(16, 10, 16, 20, 0, 5, 0x08, 100, 200, 0, 0)
        with self.assertRaises(VqaError) as cm:
            gop.parse_header(bad_flags)
        self.assertEqual(cm.exception.code, "E_GOP_PAD")

    def test_split_blobs(self):
        """Split blobs on valid GOP byte stream and negative case on truncated blob."""
        hdr1 = gop._pack_header(16, 10, 16, 20, 0, 5, 0, 10, 20, 0, 0)
        blob1 = hdr1 + b"x" * 30  # total len 78
        hdr2 = gop._pack_header(16, 10, 16, 20, 5, 5, 0, 15, 25, 0, 0)
        blob2 = hdr2 + b"y" * 40  # total len 88
        data = blob1 + blob2

        offsets = gop.split_blobs(data)
        self.assertEqual(offsets, [(0, 78), (78, 88)])

        # Negative case: Truncated blob data
        truncated_data = data[:-10]
        with self.assertRaises(VqaError) as cm:
            gop.split_blobs(truncated_data)
        self.assertEqual(cm.exception.code, "E_GOP_MAGIC")

    def test_gop_class(self):
        """Direct construction and attribute assertions for Gop class, plus negative attribute access."""
        instance = gop.Gop(g=1, f0=100, n_frames=20, flags=gop.F_CUT_START, blob=b"dummy_blob")
        self.assertEqual(instance.g, 1)
        self.assertEqual(instance.f0, 100)
        self.assertEqual(instance.n_frames, 20)
        self.assertEqual(instance.flags, 1)
        self.assertEqual(instance.blob, b"dummy_blob")

        # Negative case: Accessing an attribute that was not initialized
        with self.assertRaises(AttributeError):
            _ = instance.non_existent_attribute


if __name__ == "__main__":
    unittest.main()
