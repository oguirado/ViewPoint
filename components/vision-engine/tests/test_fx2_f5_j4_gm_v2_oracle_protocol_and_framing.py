"""tests/test_fx2_f5_j4_gm_v2_oracle_protocol_and_framing.py - Native stdin framing rejection gap tests for rust/vqa_k_gm_v2.

Exercises native stdin process receiving malformed requests (unknown opcodes,
partial op words, header strict prefixes, incomplete frame payloads, zero dimensions/s)
as well as a valid small interleaved op sequence as negative contrast.
Preserves bare little-endian op contract per gm_v2_oracle.py and prototype README.
"""

import io
import os
import struct
import subprocess
import unittest
import numpy as np


class TestFX2F5J4GMV2OracleProtocolAndFraming(unittest.TestCase):
    """Tests for native process stdin framing rejection and negative contrast sequence."""

    def _run_subproc_with_input(self, bin_path: str, input_bytes: bytes, timeout: float = 5.0):
        """Spawn native binary in a fresh bounded subprocess, write input_bytes, and reap."""
        proc = subprocess.Popen(
            [bin_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout_data, stderr_data = proc.communicate(input_bytes, timeout=timeout)
            return proc.returncode, stdout_data, stderr_data
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def _assert_malformed_rejection(self, bin_path: str, input_bytes: bytes, msg: str = ""):
        """Assert prompt nonzero exit termination, no panic text, and no valid status=0 success response."""
        returncode, stdout_data, stderr_data = self._run_subproc_with_input(bin_path, input_bytes)

        # 1. Require prompt nonzero exit / process termination
        self.assertNotEqual(
            returncode, 0,
            f"Malformed input must trigger nonzero process termination, got returncode 0. {msg}"
        )

        # 2. Require no Rust panic text in stderr
        stderr_lower = stderr_data.lower()
        self.assertNotIn(
            b"panicked", stderr_lower,
            f"Stderr must not contain panic stacktrace: {stderr_data!r}. {msg}"
        )
        self.assertNotIn(
            b"panic", stderr_lower,
            f"Stderr must not contain panic text: {stderr_data!r}. {msg}"
        )

        # 3. Require no valid success status (status == 0) in stdout
        if len(stdout_data) >= 4:
            status = struct.unpack("<i", stdout_data[:4])[0]
            self.assertNotEqual(
                status, 0,
                f"Stdout must not return success status 0 for malformed input. {msg}"
            )

    # fx2-f5-rust-vqa-k-gm-v2-conformance
    def test_unknown_opcode_rejection(self):
        """Native process must promptly terminate with nonzero code for unknown opcodes."""
        from tests.test_jq1_a4_rust_gm_v2_conformance_gate import resolve_v2_binary

        bin_path = resolve_v2_binary()
        self.assertTrue(os.path.exists(bin_path), f"Native binary missing at {bin_path}")
        self.assertTrue(os.access(bin_path, os.X_OK), f"Native binary not executable at {bin_path}")

        # Unknown opcodes: integer opcodes 99, -1, 255, and byte-swapped opcode struct.pack('>i', 1)
        unknown_ops = [
            struct.pack("<i", 99),
            struct.pack("<i", -1),
            struct.pack("<i", 255),
            struct.pack(">i", 1),  # Byte-swapped 1 = 0x01000000 = 16777216 in LE
        ]

        for op_bytes in unknown_ops:
            self._assert_malformed_rejection(
                bin_path,
                op_bytes,
                msg=f"for opcode bytes {op_bytes!r}"
            )

    # fx2-f5-rust-vqa-k-gm-v2-conformance
    def test_partial_op_word_rejection(self):
        """Native process must promptly terminate with nonzero code for truncated op words."""
        from tests.test_jq1_a4_rust_gm_v2_conformance_gate import resolve_v2_binary

        bin_path = resolve_v2_binary()
        self.assertTrue(os.path.exists(bin_path), f"Native binary missing at {bin_path}")
        self.assertTrue(os.access(bin_path, os.X_OK), f"Native binary not executable at {bin_path}")

        # Partial op words: 1, 2, and 3 bytes
        partial_words = [b"\x01", b"\x01\x00", b"\x01\x00\x00"]

        for partial in partial_words:
            self._assert_malformed_rejection(
                bin_path,
                partial,
                msg=f"for partial op word {partial!r}"
            )

    # fx2-f5-rust-vqa-k-gm-v2-conformance
    def test_op1_and_op2_header_strict_prefix_rejection(self):
        """Native process must promptly terminate with nonzero code for every strict prefix of op 1 and op 2 headers."""
        from tests.test_jq1_a4_rust_gm_v2_conformance_gate import resolve_v2_binary

        bin_path = resolve_v2_binary()
        self.assertTrue(os.path.exists(bin_path), f"Native binary missing at {bin_path}")
        self.assertTrue(os.access(bin_path, os.X_OK), f"Native binary not executable at {bin_path}")

        # Op 1 request header: op=1 (i32), h=16 (u32), w=16 (u32), max_d=8 (i32), k_top=8 (u32), s=16 (u32) -> 24 bytes
        op1_hdr = struct.pack("<iIIiII", 1, 16, 16, 8, 8, 16)
        self.assertEqual(len(op1_hdr), 24)

        for k in range(1, len(op1_hdr)):
            prefix = op1_hdr[:k]
            self._assert_malformed_rejection(
                bin_path,
                prefix,
                msg=f"for op 1 header strict prefix length {k}"
            )

        # Op 2 request header: op=2 (i32), h=16 (u32), w=16 (u32), s=16 (u32), dx=-2 (i32), dy=3 (i32) -> 24 bytes
        op2_hdr = struct.pack("<iIIIii", 2, 16, 16, 16, -2, 3)
        self.assertEqual(len(op2_hdr), 24)

        for k in range(1, len(op2_hdr)):
            prefix = op2_hdr[:k]
            self._assert_malformed_rejection(
                bin_path,
                prefix,
                msg=f"for op 2 header strict prefix length {k}"
            )

    # fx2-f5-rust-vqa-k-gm-v2-conformance
    def test_complete_prev_incomplete_cur_frame_rejection(self):
        """Native process must terminate with nonzero code when cur frame payload is truncated."""
        from tests.test_jq1_a4_rust_gm_v2_conformance_gate import resolve_v2_binary

        bin_path = resolve_v2_binary()
        self.assertTrue(os.path.exists(bin_path), f"Native binary missing at {bin_path}")
        self.assertTrue(os.access(bin_path, os.X_OK), f"Native binary not executable at {bin_path}")

        h, w = 16, 16
        prev_bytes = bytes(h * w)

        # Op 1 with complete prev frame (256 bytes) and incomplete cur frame (128 bytes)
        op1_hdr = struct.pack("<iIIiII", 1, h, w, 8, 8, 16)
        op1_truncated = op1_hdr + prev_bytes + bytes(64)
        self._assert_malformed_rejection(
            bin_path,
            op1_truncated,
            msg="for op 1 complete prev frame but truncated cur frame"
        )

        # Op 2 with complete prev frame (256 bytes) and incomplete cur frame (10 bytes)
        op2_hdr = struct.pack("<iIIIii", 2, h, w, 16, -2, 3)
        op2_truncated = op2_hdr + prev_bytes + bytes(10)
        self._assert_malformed_rejection(
            bin_path,
            op2_truncated,
            msg="for op 2 complete prev frame but truncated cur frame"
        )

    # fx2-f5-rust-vqa-k-gm-v2-conformance
    def test_zero_dimensions_or_tile_size_rejection(self):
        """Native process must promptly reject requests with zero height, zero width, or zero tile size."""
        from tests.test_jq1_a4_rust_gm_v2_conformance_gate import resolve_v2_binary

        bin_path = resolve_v2_binary()
        self.assertTrue(os.path.exists(bin_path), f"Native binary missing at {bin_path}")
        self.assertTrue(os.access(bin_path, os.X_OK), f"Native binary not executable at {bin_path}")

        # Zero height: h=0, w=16, s=16
        req_zero_h = struct.pack("<iIIiII", 1, 0, 16, 8, 8, 16)
        self._assert_malformed_rejection(
            bin_path,
            req_zero_h,
            msg="for zero height h=0"
        )

        # Zero width: h=16, w=0, s=16
        req_zero_w = struct.pack("<iIIiII", 1, 16, 0, 8, 8, 16)
        self._assert_malformed_rejection(
            bin_path,
            req_zero_w,
            msg="for zero width w=0"
        )

        # Zero tile size: h=16, w=16, s=0
        req_zero_s = struct.pack("<iIIiII", 1, 16, 16, 8, 8, 0) + bytes(16 * 16) + bytes(16 * 16)
        self._assert_malformed_rejection(
            bin_path,
            req_zero_s,
            msg="for zero tile size s=0"
        )

    # fx2-f5-rust-vqa-k-gm-v2-conformance
    def test_valid_interleaved_op_sequence_negative_contrast(self):
        """Valid small interleaved op sequence (op 1 -> op 2 -> op 1 -> op 0) must succeed cleanly."""
        from eval.v2x import gm_bands_hv
        from eval.v2x import gm_v2_oracle as oracle
        from tests.test_jq1_a4_rust_gm_v2_conformance_gate import resolve_v2_binary

        bin_path = resolve_v2_binary()
        self.assertTrue(os.path.exists(bin_path), f"Native binary missing at {bin_path}")
        self.assertTrue(os.access(bin_path, os.X_OK), f"Native binary not executable at {bin_path}")

        h, w, s = 32, 32, 16
        gh_exp, gw_exp = (h + s - 1) // s, (w + s - 1) // s

        rng = np.random.default_rng(1001)
        prev = rng.integers(0, 256, (h, w), dtype=np.uint8)
        cur = rng.integers(0, 256, (h, w), dtype=np.uint8)

        case1 = {"h": h, "w": w, "s": s, "max_d": 8, "prev": prev, "cur": cur}
        req1 = oracle.encode_request(case1, op=1)

        # Op 2 with signed displacement dx=-4, dy=5
        case2 = {"h": h, "w": w, "s": s, "dx": -4, "dy": 5, "prev": prev, "cur": cur}
        req2 = oracle.encode_request(case2, op=2)

        # Op 0 quit
        req0 = oracle.encode_request(case1, op=0)

        interleaved_payload = req1 + req2 + req1 + req0

        returncode, stdout_data, stderr_data = self._run_subproc_with_input(bin_path, interleaved_payload)

        # Clean exit returncode 0
        self.assertEqual(returncode, 0, f"Valid sequence must exit returncode 0, got {returncode}. Stderr: {stderr_data!r}")
        self.assertNotIn(b"panicked", stderr_data.lower())
        self.assertNotIn(b"panic", stderr_data.lower())

        stream = io.BytesIO(stdout_data)

        # Decode response 1 (Op 1)
        resp1 = oracle.decode_response(stream, op=1)
        self.assertEqual(resp1["status"], 0)
        self.assertEqual(resp1["gh"], gh_exp)
        self.assertEqual(resp1["gw"], gw_exp)
        self.assertEqual(resp1["mask"].shape, (gh_exp, gw_exp))

        # Decode response 2 (Op 2 explicit consensus with signed displacement)
        resp2 = oracle.decode_response(stream, op=2)
        self.assertEqual(resp2["status"], 0)
        self.assertEqual(resp2["gh"], gh_exp)
        self.assertEqual(resp2["gw"], gw_exp)
        self.assertEqual(resp2["mask"].shape, (gh_exp, gw_exp))
        exp_mask2 = gm_bands_hv.consensus_tiles(prev, cur, s, dx=-4, dy=5)
        np.testing.assert_array_equal(resp2["mask"], exp_mask2)

        # Decode response 3 (Second Op 1)
        resp3 = oracle.decode_response(stream, op=1)
        self.assertEqual(resp3["status"], 0)
        self.assertEqual(resp3["gh"], gh_exp)
        self.assertEqual(resp3["gw"], gw_exp)


if __name__ == "__main__":
    unittest.main()
