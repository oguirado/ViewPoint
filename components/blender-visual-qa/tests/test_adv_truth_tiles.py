"""Adversarial test suite for truth tile codec (score.read_truth_tiles, labels.pack_truth_tiles,
labels.parse_truth_tiles, labels.mark_unobservable).

All fixtures generated in RAM from fixed seeds.
Storage law: No binary fixture files or frames written to disk (except temporary files for score.read_truth_tiles).
"""
import pathlib
import struct
import tempfile
import unittest
from types import SimpleNamespace

import numpy as np

from blender_visual_qa.truth import labels, score
from blender_visual_qa.truth.score import VqaError

# Table of malformed/boundary cases that are accepted by codec functions without error.
KNOWN_ACCEPTS = {
    "flags_unknown_bits": "Flags byte with unknown bits (e.g. 0xF0) is accepted without masking or validation.",
    "mv_boundary_i8": "Motion vector components dx and dy at int8 boundaries (-128, 127) are accepted.",
    "zero_frames": "Header with n_frames=0 (64-byte file) is accepted and yields empty array payload.",
    "sha16_nonzero": "Header with non-zero 16-byte SHA16 payload identifier is accepted.",
    "header_N_mismatch_no_grid": "Header with N != gw * gh is accepted when g=None and expect_grid=None.",
}


def make_valid_blob(gw=20, gh=12, s=16, W=320, H=192, n_frames=3, f0=0, sha16=b"\x00" * 16, seed=42):
    """Generate a valid truth tile blob in RAM for testing."""
    N = gw * gh
    rng = np.random.default_rng(seed)
    lab_list = []
    for f in range(n_frames):
        lab_list.append({
            "tl": rng.integers(0, labels.TL_MAX + 1, size=N, dtype=np.uint8),
            "flags": rng.integers(0, 16, size=N, dtype=np.uint8),
            "mv": rng.integers(-127, 128, size=(N, 2), dtype=np.int8),
            "ob_major": rng.integers(0, 65536, size=N, dtype=np.uint16),
            "occ_permille": rng.integers(0, 1001, size=N, dtype=np.uint16),
        })
    g = SimpleNamespace(s=s, W=W, H=H, gw=gw, gh=gh, N=N, ident=f"grid:{W}x{H}:s{s}")
    blob = labels.pack_truth_tiles(g, lab_list, f0=f0, sha16=sha16)
    return blob, g, lab_list


class AdvTruthTilesTests(unittest.TestCase):
    def setUp(self):
        self.valid_blob, self.g, self.lab_list = make_valid_blob()
        self.expect_grid_tuple = (self.g.s, self.g.W, self.g.H, self.g.gw, self.g.gh, self.g.N)

    def test_known_accepts_table(self):
        """Assert exact keys present in KNOWN_ACCEPTS table."""
        expected_keys = {
            "flags_unknown_bits",
            "mv_boundary_i8",
            "zero_frames",
            "sha16_nonzero",
            "header_N_mismatch_no_grid",
        }
        self.assertEqual(set(KNOWN_ACCEPTS.keys()), expected_keys)
        for key, desc in KNOWN_ACCEPTS.items():
            self.assertIsInstance(desc, str)
            self.assertGreater(len(desc), 0)

    def test_wrong_magic(self):
        """1. Wrong magic raises E_SCHEMA (rc 2)."""
        b = bytearray(self.valid_blob)
        b[:8] = b"BADMAGIC"
        blob = bytes(b)

        with self.assertRaises(VqaError) as cm:
            labels.parse_truth_tiles(blob, self.g)
        self.assertEqual((cm.exception.code, cm.exception.rc), ("E_SCHEMA", 2))

        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "t.bin"
            p.write_bytes(blob)
            with self.assertRaises(VqaError) as cm:
                score.read_truth_tiles(p, expect_grid=self.expect_grid_tuple)
            self.assertEqual((cm.exception.code, cm.exception.rc), ("E_SCHEMA", 2))

        with self.assertRaises(VqaError) as cm:
            labels.mark_unobservable(blob, np.zeros((3, self.g.N), bool))
        self.assertEqual((cm.exception.code, cm.exception.rc), ("E_SCHEMA", 2))

    def test_version_zero_and_65535(self):
        """2. Version 0 and 65535 raise E_SCHEMA (rc 2)."""
        for ver in (0, 65535):
            b = bytearray(self.valid_blob)
            struct.pack_into("<H", b, 8, ver)
            blob = bytes(b)

            with self.assertRaises(VqaError) as cm:
                labels.parse_truth_tiles(blob, self.g)
            self.assertEqual((cm.exception.code, cm.exception.rc), ("E_SCHEMA", 2))

            with tempfile.TemporaryDirectory() as td:
                p = pathlib.Path(td) / "t.bin"
                p.write_bytes(blob)
                with self.assertRaises(VqaError) as cm:
                    score.read_truth_tiles(p, expect_grid=self.expect_grid_tuple)
                self.assertEqual((cm.exception.code, cm.exception.rc), ("E_SCHEMA", 2))

            with self.assertRaises(VqaError) as cm:
                labels.mark_unobservable(blob, np.zeros((3, self.g.N), bool))
            self.assertEqual((cm.exception.code, cm.exception.rc), ("E_SCHEMA", 2))

    def test_header_truncated_below_64(self):
        """3. Header truncated at every byte below 64 raises E_SCHEMA (rc 2)."""
        for length in range(64):
            blob = self.valid_blob[:length]

            with self.assertRaises(VqaError) as cm:
                labels.parse_truth_tiles(blob, self.g)
            self.assertEqual((cm.exception.code, cm.exception.rc), ("E_SCHEMA", 2))

            with tempfile.TemporaryDirectory() as td:
                p = pathlib.Path(td) / "t.bin"
                p.write_bytes(blob)
                with self.assertRaises(VqaError) as cm:
                    score.read_truth_tiles(p, expect_grid=self.expect_grid_tuple)
                self.assertEqual((cm.exception.code, cm.exception.rc), ("E_SCHEMA", 2))

            with self.assertRaises(VqaError) as cm:
                labels.mark_unobservable(blob, np.zeros((3, self.g.N), bool))
            self.assertEqual((cm.exception.code, cm.exception.rc), ("E_SCHEMA", 2))

    def test_record_count_mismatch(self):
        """4. Record count not matching gw * gh * n_frames raises E_SCHEMA or E_STALE."""
        # Case 4a: Payload size truncated / altered
        b = self.valid_blob[:-8] # 1 tile missing
        with self.assertRaises(VqaError) as cm:
            labels.parse_truth_tiles(b, self.g)
        self.assertEqual((cm.exception.code, cm.exception.rc), ("E_SCHEMA", 2))

        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "t.bin"
            p.write_bytes(b)
            with self.assertRaises(VqaError) as cm:
                score.read_truth_tiles(p, expect_grid=self.expect_grid_tuple)
            self.assertEqual((cm.exception.code, cm.exception.rc), ("E_SCHEMA", 2))

        with self.assertRaises(VqaError) as cm:
            labels.mark_unobservable(b, np.zeros((3, self.g.N), bool))
        self.assertEqual((cm.exception.code, cm.exception.rc), ("E_SCHEMA", 2))

        # Case 4b: Header N set to 200 (gw=20, gh=12 -> gw*gh=240 != N=200) with matching payload 200*3*8
        b4_hdr = bytearray(self.valid_blob[:64])
        struct.pack_into("<H", b4_hdr, 20, 200) # N=200 at offset 20
        b4_blob = bytes(b4_hdr) + bytes(200 * 3 * 8)

        # When Grid/expect_grid is provided, it raises E_STALE (rc 4) because N=200 != 240
        with self.assertRaises(VqaError) as cm:
            labels.parse_truth_tiles(b4_blob, self.g)
        self.assertEqual((cm.exception.code, cm.exception.rc), ("E_STALE", 4))

        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "t.bin"
            p.write_bytes(b4_blob)
            with self.assertRaises(VqaError) as cm:
                score.read_truth_tiles(p, expect_grid=self.expect_grid_tuple)
            self.assertEqual((cm.exception.code, cm.exception.rc), ("E_STALE", 4))

    def test_header_N_mismatch_without_grid(self):
        """4c. Header N != gw * gh without grid check is accepted (recorded in KNOWN_ACCEPTS)."""
        b4_hdr = bytearray(self.valid_blob[:64])
        struct.pack_into("<H", b4_hdr, 20, 200) # N=200 at offset 20
        b4_blob = bytes(b4_hdr) + bytes(200 * 3 * 8)

        # When g=None / expect_grid=None, codec does not check gw * gh == N
        h, arrs = labels.parse_truth_tiles(b4_blob, g=None)
        self.assertEqual(h["N"], 200)
        self.assertEqual(len(arrs), 3)

        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "t.bin"
            p.write_bytes(b4_blob)
            res = score.read_truth_tiles(p, expect_grid=None)
            self.assertEqual(res["hdr"]["N"], 200)

        unobs_res = labels.mark_unobservable(b4_blob, np.zeros((3, 200), bool))
        self.assertEqual(len(unobs_res), len(b4_blob))
        self.assertIn("header_N_mismatch_no_grid", KNOWN_ACCEPTS)

    def test_trailing_bytes(self):
        """5. Trailing bytes after payload raise E_SCHEMA (rc 2)."""
        b = self.valid_blob + b"\x00\x00\x00\x00"

        with self.assertRaises(VqaError) as cm:
            labels.parse_truth_tiles(b, self.g)
        self.assertEqual((cm.exception.code, cm.exception.rc), ("E_SCHEMA", 2))

        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "t.bin"
            p.write_bytes(b)
            with self.assertRaises(VqaError) as cm:
                score.read_truth_tiles(p, expect_grid=self.expect_grid_tuple)
            self.assertEqual((cm.exception.code, cm.exception.rc), ("E_SCHEMA", 2))

        with self.assertRaises(VqaError) as cm:
            labels.mark_unobservable(b, np.zeros((3, self.g.N), bool))
        self.assertEqual((cm.exception.code, cm.exception.rc), ("E_SCHEMA", 2))

    def test_grid_mismatch(self):
        """6. Grid mismatch with expect_grid / g raises E_STALE (rc 4)."""
        bad_g = SimpleNamespace(s=16, W=160, H=96, gw=10, gh=6, N=60, ident="grid:160x96:s16")
        bad_tuple = (16, 160, 96, 10, 6, 60)

        with self.assertRaises(VqaError) as cm:
            labels.parse_truth_tiles(self.valid_blob, bad_g)
        self.assertEqual((cm.exception.code, cm.exception.rc), ("E_STALE", 4))

        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "t.bin"
            p.write_bytes(self.valid_blob)
            with self.assertRaises(VqaError) as cm:
                score.read_truth_tiles(p, expect_grid=bad_tuple)
            self.assertEqual((cm.exception.code, cm.exception.rc), ("E_STALE", 4))

    def test_tl_above_TL_MAX(self):
        """7. tl values above TL_MAX (11) raise E_SCHEMA (rc 2)."""
        b = bytearray(self.valid_blob)
        b[64] = 12 # First record's tl byte
        blob = bytes(b)

        with self.assertRaises(VqaError) as cm:
            labels.parse_truth_tiles(blob, self.g)
        self.assertEqual((cm.exception.code, cm.exception.rc), ("E_SCHEMA", 2))

        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "t.bin"
            p.write_bytes(blob)
            with self.assertRaises(VqaError) as cm:
                score.read_truth_tiles(p, expect_grid=self.expect_grid_tuple)
            self.assertEqual((cm.exception.code, cm.exception.rc), ("E_SCHEMA", 2))

        with self.assertRaises(VqaError) as cm:
            labels.mark_unobservable(blob, np.zeros((3, self.g.N), bool))
        self.assertEqual((cm.exception.code, cm.exception.rc), ("E_SCHEMA", 2))

    def test_flags_unknown_bits(self):
        """8. Flags with unknown bits (e.g. 0xF0) are accepted (recorded in KNOWN_ACCEPTS)."""
        b = bytearray(self.valid_blob)
        b[65] = 0xF0 # Record 0 flags
        blob = bytes(b)

        h, arrs = labels.parse_truth_tiles(blob, self.g)
        self.assertEqual(arrs[0]["fl"][0], 0xF0)

        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "t.bin"
            p.write_bytes(blob)
            res = score.read_truth_tiles(p, expect_grid=self.expect_grid_tuple)
            self.assertEqual(res["flags"][0, 0], 0xF0)

        marked = labels.mark_unobservable(blob, np.zeros((3, self.g.N), bool))
        self.assertEqual(len(marked), len(blob))
        self.assertIn("flags_unknown_bits", KNOWN_ACCEPTS)

    def test_mv_dx_dy_at_minus_128_and_127(self):
        """9. Motion vector values dx=-128 and dy=127 are accepted (recorded in KNOWN_ACCEPTS)."""
        b = bytearray(self.valid_blob)
        # Record 0 at offset 64: tl (1B), fl (1B), dx (1B), dy (1B)
        b[66] = 0x80 # dx = -128 in int8
        b[67] = 0x7F # dy = 127 in int8
        blob = bytes(b)

        h, arrs = labels.parse_truth_tiles(blob, self.g)
        self.assertEqual(arrs[0]["dx"][0], -128)
        self.assertEqual(arrs[0]["dy"][0], 127)

        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "t.bin"
            p.write_bytes(blob)
            res = score.read_truth_tiles(p, expect_grid=self.expect_grid_tuple)
            self.assertEqual(res["mv"][0, 0, 0], -128)
            self.assertEqual(res["mv"][0, 0, 1], 127)

        self.assertIn("mv_boundary_i8", KNOWN_ACCEPTS)

    def test_zero_frames(self):
        """10. Zero frames (n_frames=0) is accepted (recorded in KNOWN_ACCEPTS)."""
        blob, zero_g, _ = make_valid_blob(n_frames=0)
        self.assertEqual(len(blob), 64)

        h, arrs = labels.parse_truth_tiles(blob, zero_g)
        self.assertEqual(h["n_frames"], 0)
        self.assertEqual(len(arrs), 0)

        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "t.bin"
            p.write_bytes(blob)
            res = score.read_truth_tiles(p, expect_grid=(zero_g.s, zero_g.W, zero_g.H, zero_g.gw, zero_g.gh, zero_g.N))
            self.assertEqual(res["hdr"]["n_frames"], 0)
            self.assertEqual(res["tl"].shape, (0, zero_g.N))

        unobs_res = labels.mark_unobservable(blob, np.zeros((0, zero_g.N), bool))
        self.assertEqual(unobs_res, blob)
        self.assertIn("zero_frames", KNOWN_ACCEPTS)

    def test_sha16_nonzero(self):
        """11. sha16 nonzero in header is accepted (recorded in KNOWN_ACCEPTS)."""
        sha = b"0123456789abcdef"
        blob, sha_g, _ = make_valid_blob(sha16=sha)

        h, arrs = labels.parse_truth_tiles(blob, sha_g)
        self.assertEqual(h["sha16"], sha.hex())

        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "t.bin"
            p.write_bytes(blob)
            res = score.read_truth_tiles(p, expect_grid=(sha_g.s, sha_g.W, sha_g.H, sha_g.gw, sha_g.gh, sha_g.N))
            self.assertEqual(res["hdr"]["n_frames"], 3)

        self.assertIn("sha16_nonzero", KNOWN_ACCEPTS)

    def test_pack_parse_roundtrip_seeded_random(self):
        """12. Pack then parse roundtrip on seeded random labels for 1x1, 20x12, and 120x68 grids."""
        grids_specs = [
            ("1x1", 1, 1, 16, 16, 16, 100),
            ("20x12", 20, 12, 16, 320, 192, 101),
            ("120x68", 120, 68, 16, 1920, 1088, 102),
        ]

        for name, gw, gh, s, W, H, seed in grids_specs:
            with self.subTest(grid=name):
                N = gw * gh
                rng = np.random.default_rng(seed)
                F = 4
                f0 = 12
                sha16 = rng.bytes(16)
                lab_list = []
                for f in range(F):
                    lab_list.append({
                        "tl": rng.integers(0, labels.TL_MAX + 1, size=N, dtype=np.uint8),
                        "flags": rng.integers(0, 256, size=N, dtype=np.uint8),
                        "mv": rng.integers(-128, 128, size=(N, 2), dtype=np.int8),
                        "ob_major": rng.integers(0, 65536, size=N, dtype=np.uint16),
                        "occ_permille": rng.integers(0, 1001, size=N, dtype=np.uint16),
                    })
                g = SimpleNamespace(s=s, W=W, H=H, gw=gw, gh=gh, N=N, ident=f"grid:{W}x{H}:s{s}")

                # Pack
                packed = labels.pack_truth_tiles(g, lab_list, f0=f0, sha16=sha16)
                self.assertEqual(len(packed), 64 + F * N * 8)

                # Parse
                h, arrs = labels.parse_truth_tiles(packed, g)
                self.assertEqual((h["s"], h["W"], h["H"], h["gw"], h["gh"], h["N"]), (s, W, H, gw, gh, N))
                self.assertEqual(h["n_frames"], F)
                self.assertEqual(h["f0"], f0)
                self.assertEqual(h["sha16"], sha16.hex())

                for f in range(F):
                    np.testing.assert_array_equal(arrs[f]["tl"], lab_list[f]["tl"])
                    np.testing.assert_array_equal(arrs[f]["fl"], lab_list[f]["flags"])
                    np.testing.assert_array_equal(arrs[f]["dx"], lab_list[f]["mv"][:, 0])
                    np.testing.assert_array_equal(arrs[f]["dy"], lab_list[f]["mv"][:, 1])
                    np.testing.assert_array_equal(arrs[f]["ob"], lab_list[f]["ob_major"])
                    np.testing.assert_array_equal(arrs[f]["oc"], lab_list[f]["occ_permille"])

                # score.read_truth_tiles
                with tempfile.TemporaryDirectory() as td:
                    p = pathlib.Path(td) / "truth.bin"
                    p.write_bytes(packed)
                    r = score.read_truth_tiles(p, expect_grid=(s, W, H, gw, gh, N))
                    np.testing.assert_array_equal(r["tl"], np.stack([lab["tl"] for lab in lab_list]))
                    np.testing.assert_array_equal(r["flags"], np.stack([lab["flags"] for lab in lab_list]))
                    np.testing.assert_array_equal(r["mv"], np.stack([lab["mv"] for lab in lab_list]))
                    np.testing.assert_array_equal(r["ob"], np.stack([lab["ob_major"] for lab in lab_list]))
                    np.testing.assert_array_equal(r["occ"], np.stack([lab["occ_permille"] for lab in lab_list]))

                # mark_unobservable
                unobs = rng.choice([True, False], size=(F, N))
                marked = labels.mark_unobservable(packed, unobs)
                h_m, arrs_m = labels.parse_truth_tiles(marked, g)
                for f in range(F):
                    expected_fl = lab_list[f]["flags"].copy()
                    expected_fl[unobs[f]] |= labels.FLAG_UNOBSERVABLE
                    np.testing.assert_array_equal(arrs_m[f]["fl"], expected_fl)


if __name__ == "__main__":
    unittest.main()
