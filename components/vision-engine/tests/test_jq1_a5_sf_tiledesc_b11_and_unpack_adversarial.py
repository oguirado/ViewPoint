"""Adversarial trust boundary unit tests for sf_tiledesc binary fingerprint decoding.

Exercises `_unpack` and `fingerprint_distance` in `eval.v2x.sf_tiledesc` against:
- Non-bytes inputs (None, int, bytearray)
- Header and layout truncation at every offset
- Hash payload truncation
- Corrupted magic header tags
- Zero width, height, or tile_size fields
- Zero tile count (wire contract requires count >= 1 non-empty bag)
- Tile count inconsistent with binary payload length
- Appended trailing bytes
- Corrupt-equal-pair negative control preventing validation bypass via equality
"""

import unittest
import numpy as np

from eval.v2x import sf_tiledesc as sf


class TestSfTiledescAdversarialUnpack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Generate valid control fingerprints from small RAM uint8 frames.
        cls.rng = np.random.RandomState(1337)
        cls.frame_a = cls.rng.randint(0, 256, (32, 32, 3), dtype=np.uint8)
        cls.frame_a.flags.writeable = False
        cls.fp_a = sf.screen_fingerprint(cls.frame_a)

        cls.frame_b = cls.rng.randint(0, 256, (32, 32, 3), dtype=np.uint8)
        cls.frame_b.flags.writeable = False
        cls.fp_b = sf.screen_fingerprint(cls.frame_b)

        # Derive header, layout, and payload offsets dynamically.
        cls.header_size = sf._HEADER.size
        cls.layout_bytes = sf._LAYOUT_BYTES
        cls.offset = cls.header_size + cls.layout_bytes

        cls.magic, cls.width, cls.height, cls.tile_size, cls.count = sf._HEADER.unpack_from(cls.fp_a)

    def test_valid_fingerprint_controls(self):
        """Positive control: valid fingerprints unpack cleanly and compute correct distance."""
        shape, layout, hashes = sf._unpack(self.fp_a)
        self.assertEqual(shape, (self.width, self.height, self.tile_size))
        self.assertIsInstance(layout, int)
        self.assertIsInstance(hashes, np.ndarray)
        self.assertEqual(len(hashes), self.count)

        # Self-distance control
        self.assertEqual(sf.fingerprint_distance(self.fp_a, self.fp_a), 0)

        # Cross-distance control
        dist = sf.fingerprint_distance(self.fp_a, self.fp_b)
        self.assertIsInstance(dist, int)
        self.assertGreaterEqual(dist, 0)

    def test_non_bytes_input_rejected(self):
        """Negative control: non-bytes inputs must raise TypeError in _unpack and fingerprint_distance."""
        bad_inputs = [None, 12345, bytearray(self.fp_a), [1, 2, 3], "string_fingerprint"]

        for bad in bad_inputs:
            with self.subTest(bad_type=type(bad).__name__):
                with self.assertRaises(TypeError):
                    sf._unpack(bad)

                # Direct & equal pair negative controls for fingerprint_distance
                with self.assertRaises(TypeError):
                    sf.fingerprint_distance(bad, self.fp_a)

                with self.assertRaises(TypeError):
                    sf.fingerprint_distance(self.fp_a, bad)

                with self.assertRaises(TypeError):
                    sf.fingerprint_distance(bad, bad)

    def test_header_and_layout_truncation_offsets(self):
        """Exhaustively test every byte offset from 0 to header_size + layout_bytes - 1."""
        for offset_k in range(0, self.offset):
            truncated = self.fp_a[:offset_k]
            with self.subTest(trunc_offset=offset_k):
                with self.assertRaises(ValueError):
                    sf._unpack(truncated)

                # Corrupt-equal-pair negative control: distance must not bypass via equality
                with self.assertRaises(ValueError):
                    sf.fingerprint_distance(truncated, truncated)

    def test_hash_payload_truncation_offsets(self):
        """Exhaustively test every truncated payload length from offset to len(fp) - 1."""
        for trunc_len in range(self.offset, len(self.fp_a) - 1):
            truncated = self.fp_a[:trunc_len]
            with self.subTest(trunc_len=trunc_len):
                with self.assertRaises(ValueError):
                    sf._unpack(truncated)

                with self.assertRaises(ValueError):
                    sf.fingerprint_distance(truncated, truncated)

    def test_corrupted_magic_header_rejected(self):
        """Corrupted magic header tag must be rejected with ValueError."""
        bad_magics = [b"BADMAGIC", b"SFTD002\0", b"SFTD001\x01", b"\x00" * 8]

        for bad_mag in bad_magics:
            corrupt = sf._HEADER.pack(bad_mag, self.width, self.height, self.tile_size, self.count) + self.fp_a[self.header_size:]
            with self.subTest(magic=bad_mag):
                with self.assertRaises(ValueError):
                    sf._unpack(corrupt)

                with self.assertRaises(ValueError):
                    sf.fingerprint_distance(corrupt, corrupt)

    def test_zero_dimensions_rejected(self):
        """Width, height, or tile_size set to 0 must be rejected with ValueError."""
        cases = [
            ("zero_width", 0, self.height, self.tile_size),
            ("zero_height", self.width, 0, self.tile_size),
            ("zero_tile_size", self.width, self.height, 0),
        ]

        for name, zw, zh, zt in cases:
            corrupt = sf._HEADER.pack(self.magic, zw, zh, zt, self.count) + self.fp_a[self.header_size:]
            with self.subTest(dimension=name):
                with self.assertRaises(ValueError):
                    sf._unpack(corrupt)

                with self.assertRaises(ValueError):
                    sf.fingerprint_distance(corrupt, corrupt)

    def test_count_zero_rejected_by_wire_contract(self):
        """Count zero must be rejected with ValueError.

        The sf_tiledesc wire contract requires non-empty tile bags (count >= 1).
        The _unpack validator enforces `min(width, height, tile_size, count) < 1`,
        so a zero hash count payload is explicitly rejected.
        """
        corrupt_zero_count = sf._HEADER.pack(self.magic, self.width, self.height, self.tile_size, 0) + self.fp_a[self.header_size:self.offset]
        with self.assertRaises(ValueError):
            sf._unpack(corrupt_zero_count)

        with self.assertRaises(ValueError):
            sf.fingerprint_distance(corrupt_zero_count, corrupt_zero_count)

    def test_count_inconsistent_with_payload_length_rejected(self):
        """Header tile count inconsistent with actual byte length must be rejected."""
        mismatched_counts = [self.count + 1, self.count - 1 if self.count > 1 else 99, self.count + 100]

        for bad_count in mismatched_counts:
            corrupt = sf._HEADER.pack(self.magic, self.width, self.height, self.tile_size, bad_count) + self.fp_a[self.header_size:]
            with self.subTest(bad_count=bad_count):
                with self.assertRaises(ValueError):
                    sf._unpack(corrupt)

                with self.assertRaises(ValueError):
                    sf.fingerprint_distance(corrupt, corrupt)

    def test_appended_trailing_bytes_rejected(self):
        """Trailing bytes beyond the expected header + layout + 8*count payload must be rejected."""
        trailing_payloads = [b"\x00", b"EXTRA_TRAILING_BYTES", b"\xff" * 8]

        for trailing in trailing_payloads:
            corrupt = self.fp_a + trailing
            with self.subTest(trailing_len=len(trailing)):
                with self.assertRaises(ValueError):
                    sf._unpack(corrupt)

                with self.assertRaises(ValueError):
                    sf.fingerprint_distance(corrupt, corrupt)


if __name__ == "__main__":
    unittest.main()
