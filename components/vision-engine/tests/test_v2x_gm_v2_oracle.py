"""tests/test_v2x_gm_v2_oracle.py - Unit tests for eval/v2x/gm_v2_oracle.py."""

import os
import stat
import struct
import sys
import tempfile
import unittest
import numpy as np

from eval.v2x import gm_v2_oracle as oracle


class TestGMV2Oracle(unittest.TestCase):

    def test_cases_deterministic(self):
        """conformance_cases returns bit-exact identical cases across calls."""
        c1 = oracle.conformance_cases(seed=0x5EED)
        c2 = oracle.conformance_cases(seed=0x5EED)

        self.assertEqual(len(c1), len(c2))
        self.assertEqual(len(c1), 71)  # 7 card cases + 64 random cases

        for item1, item2 in zip(c1, c2):
            self.assertEqual(item1["case_id"], item2["case_id"])
            self.assertEqual(item1["h"], item2["h"])
            self.assertEqual(item1["w"], item2["w"])
            self.assertEqual(item1["s"], item2["s"])
            self.assertEqual(item1["max_d"], item2["max_d"])
            np.testing.assert_array_equal(item1["prev"], item2["prev"])
            np.testing.assert_array_equal(item1["cur"], item2["cur"])

    def test_encode_decode_round_trip(self):
        """encode_request framing and decode_response round-trip synthetic buffers."""
        case = {
            "h": 32,
            "w": 32,
            "s": 16,
            "max_d": 10,
            "prev": np.full((32, 32), 50, dtype=np.uint8),
            "cur": np.full((32, 32), 60, dtype=np.uint8),
        }

        req1 = oracle.encode_request(case, op=1)
        self.assertEqual(len(req1), 24 + 32 * 32 * 2)

        # Build synthetic response for op 1
        gh, gw = 2, 2
        mask_raw = bytes([1, 0, 0, 1])
        resp_hdr = struct.pack("<iBiiqII", 0, 1, 3, -2, 12345, gh, gw)
        resp_bytes = resp_hdr + mask_raw

        decoded = oracle.decode_response(resp_bytes, op=1)
        self.assertEqual(decoded["status"], 0)
        self.assertEqual(decoded["has_gm"], 1)
        self.assertEqual(decoded["dx"], 3)
        self.assertEqual(decoded["dy"], -2)
        self.assertEqual(decoded["score"], 12345)
        self.assertEqual(decoded["gh"], 2)
        self.assertEqual(decoded["gw"], 2)
        np.testing.assert_array_equal(decoded["mask"], [[1, 0], [0, 1]])

    def test_request_bytes_literal_hand_computed(self):
        """encode_request on a tiny 2x2 case matches literal byte string."""
        case = {
            "h": 2,
            "w": 2,
            "s": 2,
            "max_d": 1,
            "prev": np.array([[10, 20], [30, 40]], dtype=np.uint8),
            "cur": np.array([[11, 21], [31, 41]], dtype=np.uint8),
        }

        # Header: op=1 (i32), h=2 (u32), w=2 (u32), max_d=1 (i32), k_top=8 (u32), s=2 (u32)
        # Prev bytes: \x0a\x14\x1e\x28
        # Cur bytes:  \x0b\x15\x1f\x29
        literal_expected = (
            b"\x01\x00\x00\x00"  # op = 1
            b"\x02\x00\x00\x00"  # h = 2
            b"\x02\x00\x00\x00"  # w = 2
            b"\x01\x00\x00\x00"  # max_d = 1
            b"\x08\x00\x00\x00"  # k_top = 8
            b"\x02\x00\x00\x00"  # s = 2
            b"\x0a\x14\x1e\x28"  # prev
            b"\x0b\x15\x1f\x29"  # cur
        )

        encoded = oracle.encode_request(case, op=1)
        self.assertEqual(encoded, literal_expected)

    def test_check_binary_fake_binary(self):
        """check_binary against fake Python binary (passing and failing mutant)."""
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        fake_bin_code = f"""#!/usr/bin/env python3
import sys, os, struct, importlib
sys.path.insert(0, {repr(repo_root)})
import numpy as np

mod = importlib.import_module("eval.v2x.gm_bands_hv")
stdin = sys.stdin.buffer
stdout = sys.stdout.buffer

mutate = ("--mutate" in sys.argv)
case_count = 0

while True:
    op_raw = stdin.read(4)
    if not op_raw or len(op_raw) < 4:
        break
    op = struct.unpack("<i", op_raw)[0]
    if op == 0:
        break
    elif op == 1:
        hdr = stdin.read(20) # h, w, max_d, k_top, s
        h, w, max_d, k_top, s = struct.unpack("<IIiII", hdr)
        prev_bytes = stdin.read(h * w)
        cur_bytes = stdin.read(h * w)
        prev = np.frombuffer(prev_bytes, dtype=np.uint8).reshape((h, w))
        cur = np.frombuffer(cur_bytes, dtype=np.uint8).reshape((h, w))

        res = mod.estimate_global_motion(prev, cur, max_d)
        if res is not None:
            has_gm, dx, dy, score = 1, int(res[0]), int(res[1]), int(res[2])
        else:
            has_gm, dx, dy, score = 0, 0, 0, 0

        mask = mod.consensus_tiles(prev, cur, s, dx, dy)
        gh, gw = mask.shape[0], mask.shape[1]
        mask_bytes = bytearray(np.ascontiguousarray(mask, dtype=np.uint8).tobytes())

        if mutate and case_count == 0:
            mask_bytes[0] ^= 1  # flip first byte for first case

        case_count += 1
        resp_hdr = struct.pack("<iBiiqII", 0, has_gm, dx, dy, score, gh, gw)
        stdout.write(resp_hdr)
        stdout.write(mask_bytes)
        stdout.flush()
"""

        with tempfile.TemporaryDirectory() as tmpdir:
            bin_good = os.path.join(tmpdir, "fake_bin_good.py")
            with open(bin_good, "w") as f:
                f.write(fake_bin_code)
            os.chmod(bin_good, os.stat(bin_good).st_mode | stat.S_IEXEC)

            # Test good binary
            report_good = oracle.check_binary(bin_good, "eval.v2x.gm_bands_hv")
            self.assertTrue(report_good["all_pass"])
            self.assertEqual(report_good["passed_cases"], report_good["total_cases"])

            # Test mutant binary
            bin_mutant = os.path.join(tmpdir, "fake_bin_mutant.py")
            with open(bin_mutant, "w") as f:
                f.write(fake_bin_code)
            os.chmod(bin_mutant, os.stat(bin_mutant).st_mode | stat.S_IEXEC)

            report_mutant = oracle.check_binary(f"{sys.executable} {bin_mutant} --mutate", "eval.v2x.gm_bands_hv")
            self.assertFalse(report_mutant["all_pass"])
            self.assertFalse(report_mutant["cases"][0]["pass"])
            self.assertEqual(report_mutant["cases"][0]["first_mismatch"], "mask")

    def test_digest_stable(self):
        """digest is stable across multiple calls and returns 64-character sha256 hex."""
        d1 = oracle.digest("eval.v2x.gm_bands_hv")
        d2 = oracle.digest("eval.v2x.gm_bands_hv")

        self.assertEqual(d1, d2)
        self.assertEqual(len(d1), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in d1))

    def test_integer_only(self):
        """Expected output fields, cases and reports contain only strict integers or booleans."""
        cases = oracle.conformance_cases()
        for case in cases[:5]:
            ref_tuple, mask = oracle.expected("eval.v2x.gm_bands_hv", case)
            has_gm, dx, dy, score, gh, gw = ref_tuple

            for val in (has_gm, dx, dy, score, gh, gw):
                self.assertIsInstance(val, (int, np.integer))
                self.assertNotIsInstance(val, float)

            self.assertIn(mask.dtype, (np.uint8, bool, np.bool_))

        d = oracle.digest("eval.v2x.gm_bands_hv")
        self.assertIsInstance(d, str)


if __name__ == "__main__":
    unittest.main()
