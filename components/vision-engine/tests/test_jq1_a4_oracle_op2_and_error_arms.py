"""tests/test_jq1_a4_oracle_op2_and_error_arms.py - Unit tests for A4 oracle op 2 wire contract and error arms."""

import io
import struct
import unittest
import numpy as np

from eval.v2x import gm_v2_oracle as oracle


class LimitedStream(io.BytesIO):
    """Custom stream reader limiting reads to max_chunk bytes per call."""

    def __init__(self, initial_bytes: bytes, max_chunk: int = 3):
        super().__init__(initial_bytes)
        self.max_chunk = max_chunk

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self.max_chunk
        else:
            size = min(size, self.max_chunk)
        return super().read(size)


class TestA4OracleOp2AndErrorArms(unittest.TestCase):

    def test_encode_request_op2_explicit_and_default(self):
        """op 2 request encoding header, payload size, row-major arrays, and default dx, dy."""
        rng = np.random.default_rng(401)
        prev = rng.integers(0, 256, (32, 48), dtype=np.uint8)
        cur = rng.integers(0, 256, (32, 48), dtype=np.uint8)

        case_explicit = {
            "h": 32,
            "w": 48,
            "s": 16,
            "max_d": 12,
            "dx": -5,
            "dy": 7,
            "prev": prev,
            "cur": cur,
        }

        req_op2 = oracle.encode_request(case_explicit, op=2)
        self.assertEqual(len(req_op2), 3096)

        hdr_unpacked = struct.unpack("<iIIIii", req_op2[:24])
        self.assertEqual(hdr_unpacked, (2, 32, 48, 16, -5, 7))

        prev_payload = req_op2[24 : 24 + 32 * 48]
        cur_payload = req_op2[24 + 32 * 48 :]

        self.assertEqual(prev_payload, prev.tobytes())
        self.assertEqual(cur_payload, cur.tobytes())

        # Test omitting dx and dy
        case_default = {
            "h": 32,
            "w": 48,
            "s": 16,
            "max_d": 12,
            "prev": prev,
            "cur": cur,
        }

        req_default = oracle.encode_request(case_default, op=2)
        hdr_default = struct.unpack("<iIIIii", req_default[:24])
        self.assertEqual(hdr_default, (2, 32, 48, 16, 0, 0))

    def test_encode_request_op0(self):
        """op 0 request encodes four-byte little-endian zero."""
        case = {"h": 32, "w": 48, "prev": np.zeros((32, 48), dtype=np.uint8), "cur": np.zeros((32, 48), dtype=np.uint8)}
        req_op0 = oracle.encode_request(case, op=0)
        self.assertEqual(req_op0, struct.pack("<i", 0))
        self.assertEqual(req_op0, b"\x00\x00\x00\x00")

    def test_decode_response_op2_formats_and_chunked_stream(self):
        """op 2 response decoding from bytes, bytearray, and limited stream (max 3 bytes/read)."""
        status, gh, gw = 0, 2, 3
        hdr = struct.pack("<iII", status, gh, gw)
        mask_raw = bytes([1, 0, 1, 0, 1, 0])
        resp_bytes = hdr + mask_raw

        expected_mask = np.array([[1, 0, 1], [0, 1, 0]], dtype=np.uint8)

        for input_data in [resp_bytes, bytearray(resp_bytes), LimitedStream(resp_bytes, max_chunk=3)]:
            decoded = oracle.decode_response(input_data, op=2)
            self.assertEqual(decoded["status"], 0)
            self.assertEqual(decoded["gh"], 2)
            self.assertEqual(decoded["gw"], 3)
            self.assertEqual(decoded["mask_bytes"], mask_raw)
            self.assertEqual(decoded["mask"].dtype, np.uint8)
            np.testing.assert_array_equal(decoded["mask"], expected_mask)

    def test_truncation_errors_op1_and_op2(self):
        """Truncated headers and masks for op 1 and op 2 raise RuntimeError with premature end message."""
        premature_msg = "Premature end of stream while reading response"

        # Op 1 response (29-byte header + gh*gw mask bytes)
        op1_hdr = struct.pack("<iBiiqII", 0, 1, 2, -3, 1000, 2, 2)
        op1_valid = op1_hdr + bytes([1, 0, 0, 1])

        # Truncate op 1 header
        for i in [0, 5, 15, 28]:
            with self.assertRaises(RuntimeError) as cm:
                oracle.decode_response(op1_hdr[:i], op=1)
            self.assertIn(premature_msg, str(cm.exception))

        # Truncate op 1 mask
        for i in [0, 1, 2, 3]:
            with self.assertRaises(RuntimeError) as cm:
                oracle.decode_response(op1_hdr + bytes([1, 0, 0, 1])[:i], op=1)
            self.assertIn(premature_msg, str(cm.exception))

        # Op 2 response (12-byte header + gh*gw mask bytes)
        op2_hdr = struct.pack("<iII", 0, 2, 2)
        op2_valid = op2_hdr + bytes([1, 0, 0, 1])

        # Truncate op 2 header
        for i in [0, 4, 8, 11]:
            with self.assertRaises(RuntimeError) as cm:
                oracle.decode_response(op2_hdr[:i], op=2)
            self.assertIn(premature_msg, str(cm.exception))

        # Truncate op 2 mask
        for i in [0, 1, 2, 3]:
            with self.assertRaises(RuntimeError) as cm:
                oracle.decode_response(op2_hdr + bytes([1, 0, 0, 1])[:i], op=2)
            self.assertIn(premature_msg, str(cm.exception))

    def test_unsupported_ops(self):
        """Unsupported ops 3, 7, -1 raise ValueError in both encoders and decoders."""
        case = {"h": 16, "w": 16, "prev": np.zeros((16, 16), dtype=np.uint8), "cur": np.zeros((16, 16), dtype=np.uint8)}
        dummy_data = b"\x00" * 64

        for unsupported_op in [3, 7, -1]:
            with self.assertRaises(ValueError):
                oracle.encode_request(case, op=unsupported_op)

            with self.assertRaises(ValueError):
                oracle.decode_response(dummy_data, op=unsupported_op)


if __name__ == "__main__":
    unittest.main()
