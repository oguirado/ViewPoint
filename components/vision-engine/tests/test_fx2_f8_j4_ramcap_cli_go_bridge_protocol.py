"""Tests for fx2-f8-j4-ramcap-cli-go-bridge-protocol Python CLI contract.

Tests the Python CLI through in-process main calls, RAM streams, fake sessions,
and patched acquisition.
"""

import io
import json
import os
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

from vadapt import xfb


def build_synthetic_xwd(w=8, h=4):
    """Build a valid synthetic 24/32-bit TrueColor ZPixmap XWD buffer."""
    hs = xfb.MIN_HEADER + len(b"Xvfb main window\x00")
    bpl = w * 4
    head = struct.pack(">25I", hs, xfb.XWD_VERSION, xfb.ZPIXMAP, 24, w, h, 0, 0, 32, 0, 32, 32, bpl, xfb.TRUECOLOR,
                       0xFF0000, 0xFF00, 0xFF, 8, 0, 0, w, h, 0, 0, 0) + b"Xvfb main window\x00"
    body = bytearray(w * h * 4)
    for y in range(h):
        for x in range(w):
            off = (y * w + x) * 4
            body[off:off + 4] = bytes((10, 20, 30, 0))  # B, G, R, X
    return head + bytes(body)


class TestFx2F8J4RamcapCliGoBridgeProtocol(unittest.TestCase):
    """Unit tests for Python CLI contracts for fx2-f8-d11-x11-framebuffer-ram-capture."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.addCleanup(self.tmp_dir.cleanup)

    def test_legacy_read_success(self):
        """Healthy control: xfb.main(["read", path]) outputs exact XWD buffer on success."""
        buf = build_synthetic_xwd(8, 4)
        screen_path = os.path.join(self.tmp_dir.name, "Xvfb_screen0")
        with open(screen_path, "wb") as f:
            f.write(buf)

        stdout_buf = io.BytesIO()
        stderr_buf = io.StringIO()

        with patch("sys.stdout", type("Stdout", (), {"buffer": stdout_buf})()), \
             patch("sys.stderr", stderr_buf):
            rc = xfb.main(["read", screen_path])

        self.assertEqual(rc, 0)
        self.assertEqual(stdout_buf.getvalue(), buf)
        self.assertEqual(stderr_buf.getvalue(), "")

    def test_legacy_read_invalid_xwd(self):
        """Healthy control: xfb.main(["read", path]) prints error code to stderr and returns 5 on corrupt frame."""
        bad_buf = b"SHORT_HEADER"
        screen_path = os.path.join(self.tmp_dir.name, "Xvfb_screen0")
        with open(screen_path, "wb") as f:
            f.write(bad_buf)

        stdout_buf = io.BytesIO()
        stderr_buf = io.StringIO()

        with patch("sys.stdout", type("Stdout", (), {"buffer": stdout_buf})()), \
             patch("sys.stderr", stderr_buf):
            rc = xfb.main(["read", screen_path])

        self.assertEqual(rc, 5)
        self.assertIn("E_XWD_SHORT", stderr_buf.getvalue())
        self.assertEqual(stdout_buf.getvalue(), b"")

    def test_legacy_read_usage_error(self):
        """Healthy control: xfb.main with incorrect arguments returns exit code 2."""
        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            rc = xfb.main(["invalid_cmd"])

        self.assertEqual(rc, 2)
        self.assertIn("usage:", stderr_buf.getvalue())

    @unittest.expectedFailure
    def test_cli_repeated_option_rejection_before_source_access(self):
        """Demonstrates missing behavior: repeated options should be rejected with exit code 2 before accessing source."""
        screen_path = os.path.join(self.tmp_dir.name, "Xvfb_screen0")
        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            rc = xfb.main(["read", "--tries=4", "--tries=8", screen_path])
        self.assertEqual(rc, 2)
        self.assertIn("E_USAGE", stderr_buf.getvalue())

    @unittest.expectedFailure
    def test_cli_fbread_stdout_exactness(self):
        """Demonstrates missing behavior: proposed fbread command writes exact frame payload to stdout."""
        buf = build_synthetic_xwd(8, 4)
        screen_path = os.path.join(self.tmp_dir.name, "Xvfb_screen0")
        with open(screen_path, "wb") as f:
            f.write(buf)

        stdout_buf = io.BytesIO()
        with patch("sys.stdout", type("Stdout", (), {"buffer": stdout_buf})()):
            rc = xfb.main(["fbread", screen_path])
        self.assertEqual(rc, 0)
        self.assertEqual(stdout_buf.getvalue(), buf)

    @unittest.expectedFailure
    def test_cli_fbread_typed_failure_no_partial_output(self):
        """Demonstrates missing behavior: fbread on error returns typed exit code/error without partial stdout data."""
        bad_buf = b"CORRUPT"
        screen_path = os.path.join(self.tmp_dir.name, "Xvfb_screen0")
        with open(screen_path, "wb") as f:
            f.write(bad_buf)

        stdout_buf = io.BytesIO()
        stderr_buf = io.StringIO()
        with patch("sys.stdout", type("Stdout", (), {"buffer": stdout_buf})()), \
             patch("sys.stderr", stderr_buf):
            rc = xfb.main(["fbread", screen_path])
        self.assertEqual(rc, 5)
        self.assertEqual(stdout_buf.getvalue(), b"")
        self.assertIn("E_XWD_SHORT", stderr_buf.getvalue())

    @unittest.expectedFailure
    def test_cli_serve_exact_request_fields(self):
        """Demonstrates missing behavior: proposed serve command handles exact request fields."""
        req = json.dumps({"id": "req-1", "op": "ping", "session": "s1"}).encode("utf-8") + b"\n"
        stdin_buf = io.BytesIO(req)
        stdout_buf = io.BytesIO()

        with patch("sys.stdin", type("Stdin", (), {"buffer": stdin_buf, "readline": stdin_buf.readline})()), \
             patch("sys.stdout", type("Stdout", (), {"buffer": stdout_buf})()):
            rc = xfb.main(["serve", "--max-control-line-bytes=1024"])

        self.assertEqual(rc, 0)
        res = json.loads(stdout_buf.getvalue().decode("utf-8").strip())
        self.assertEqual(res.get("id"), "req-1")
        self.assertEqual(res.get("code"), "OK")

    @unittest.expectedFailure
    def test_cli_serve_duplicate_json_keys(self):
        """Demonstrates missing behavior: serve command rejects JSON requests with duplicate keys."""
        req = b'{"id": "req-1", "id": "req-2", "op": "ping"}\n'
        stdin_buf = io.BytesIO(req)
        stdout_buf = io.BytesIO()

        with patch("sys.stdin", type("Stdin", (), {"buffer": stdin_buf, "readline": stdin_buf.readline})()), \
             patch("sys.stdout", type("Stdout", (), {"buffer": stdout_buf})()):
            rc = xfb.main(["serve"])

        res = json.loads(stdout_buf.getvalue().decode("utf-8").strip())
        self.assertEqual(res.get("code"), "E_DUPLICATE_KEY")

    @unittest.expectedFailure
    def test_cli_serve_invalid_integer_domains(self):
        """Demonstrates missing behavior: serve command rejects out-of-domain integer parameters."""
        req = json.dumps({"id": "req-1", "op": "capture", "width": -50}).encode("utf-8") + b"\n"
        stdin_buf = io.BytesIO(req)
        stdout_buf = io.BytesIO()

        with patch("sys.stdin", type("Stdin", (), {"buffer": stdin_buf, "readline": stdin_buf.readline})()), \
             patch("sys.stdout", type("Stdout", (), {"buffer": stdout_buf})()):
            rc = xfb.main(["serve"])

        res = json.loads(stdout_buf.getvalue().decode("utf-8").strip())
        self.assertEqual(res.get("code"), "E_INVALID_DOMAIN")

    @unittest.expectedFailure
    def test_cli_serve_unknown_sessions_and_operations(self):
        """Demonstrates missing behavior: serve command returns typed errors for unknown sessions or operations."""
        req1 = json.dumps({"id": "req-1", "op": "unknown_op", "session": "s1"}).encode("utf-8") + b"\n"
        req2 = json.dumps({"id": "req-2", "op": "capture", "session": "unknown_session"}).encode("utf-8") + b"\n"
        stdin_buf = io.BytesIO(req1 + req2)
        stdout_buf = io.BytesIO()

        with patch("sys.stdin", type("Stdin", (), {"buffer": stdin_buf, "readline": stdin_buf.readline})()), \
             patch("sys.stdout", type("Stdout", (), {"buffer": stdout_buf})()):
            rc = xfb.main(["serve"])

        lines = [json.loads(line) for line in stdout_buf.getvalue().decode("utf-8").strip().splitlines() if line]
        self.assertEqual(lines[0].get("code"), "E_UNKNOWN_OP")
        self.assertEqual(lines[1].get("code"), "E_UNKNOWN_SESSION")

    @unittest.expectedFailure
    def test_cli_serve_malformed_input_recovery(self):
        """Demonstrates missing behavior: serve command reports error for malformed line and recovers for valid line."""
        line1 = b"{malformed_json\n"
        line2 = json.dumps({"id": "req-2", "op": "ping"}).encode("utf-8") + b"\n"
        stdin_buf = io.BytesIO(line1 + line2)
        stdout_buf = io.BytesIO()

        with patch("sys.stdin", type("Stdin", (), {"buffer": stdin_buf, "readline": stdin_buf.readline})()), \
             patch("sys.stdout", type("Stdout", (), {"buffer": stdout_buf})()):
            rc = xfb.main(["serve"])

        lines = [json.loads(line) for line in stdout_buf.getvalue().decode("utf-8").strip().splitlines() if line]
        self.assertEqual(lines[0].get("code"), "E_MALFORMED_JSON")
        self.assertEqual(lines[1].get("id"), "req-2")
        self.assertEqual(lines[1].get("code"), "OK")

    @unittest.expectedFailure
    def test_cli_serve_max_control_line_bytes(self):
        """Demonstrates missing behavior: serve mode configured with --max-control-line-bytes admits up to limit,
        rejects over-limit lines without unbounded reading, and recovers for following line."""
        max_bytes = 64
        # Line 1: Exact limit line (64 bytes including newline)
        raw_req1 = json.dumps({"id": "r1", "op": "ping", "p": "a" * 35}).encode("utf-8")
        self.assertLessEqual(len(raw_req1) + 1, max_bytes)
        line1 = raw_req1.ljust(max_bytes - 1, b" ") + b"\n"
        self.assertEqual(len(line1), max_bytes)

        # Line 2: Over-limit line (> 64 bytes)
        line2 = b'{"id": "r2", "op": "ping", "p": "' + b"x" * 100 + b'"}\n'
        self.assertGreater(len(line2), max_bytes)

        # Line 3: Normal valid line following over-limit
        line3 = json.dumps({"id": "r3", "op": "ping"}).encode("utf-8") + b"\n"

        stdin_buf = io.BytesIO(line1 + line2 + line3)
        stdout_buf = io.BytesIO()

        with patch("sys.stdin", type("Stdin", (), {"buffer": stdin_buf, "readline": stdin_buf.readline})()), \
             patch("sys.stdout", type("Stdout", (), {"buffer": stdout_buf})()):
            rc = xfb.main(["serve", f"--max-control-line-bytes={max_bytes}"])

        lines = [json.loads(line) for line in stdout_buf.getvalue().decode("utf-8").strip().splitlines() if line]
        self.assertEqual(lines[0].get("id"), "r1")
        self.assertEqual(lines[0].get("code"), "OK")
        self.assertEqual(lines[1].get("code"), "E_LINE_TOO_LONG")
        self.assertEqual(lines[2].get("id"), "r3")
        self.assertEqual(lines[2].get("code"), "OK")

    @unittest.expectedFailure
    def test_cli_serve_cancellation_and_eof_cleanup(self):
        """Demonstrates missing behavior: serve command cleans up sessions on cancellation or stdin EOF."""
        req = json.dumps({"id": "req-cancel", "op": "cancel", "session": "s1"}).encode("utf-8") + b"\n"
        stdin_buf = io.BytesIO(req)
        stdout_buf = io.BytesIO()

        with patch("sys.stdin", type("Stdin", (), {"buffer": stdin_buf, "readline": stdin_buf.readline})()), \
             patch("sys.stdout", type("Stdout", (), {"buffer": stdout_buf})()):
            rc = xfb.main(["serve"])

        self.assertEqual(rc, 0)
        res = json.loads(stdout_buf.getvalue().decode("utf-8").strip())
        self.assertEqual(res.get("id"), "req-cancel")
        self.assertEqual(res.get("code"), "OK")

    @unittest.expectedFailure
    def test_cli_serve_stable_response_bytes_deterministic_ids(self):
        """Demonstrates missing behavior: serve command responses contain stable response bytes with deterministic request IDs."""
        req1 = json.dumps({"id": "det-1", "op": "ping"}).encode("utf-8") + b"\n"
        req2 = json.dumps({"id": "det-2", "op": "ping"}).encode("utf-8") + b"\n"
        stdin_buf = io.BytesIO(req1 + req2)
        stdout_buf = io.BytesIO()

        with patch("sys.stdin", type("Stdin", (), {"buffer": stdin_buf, "readline": stdin_buf.readline})()), \
             patch("sys.stdout", type("Stdout", (), {"buffer": stdout_buf})()):
            rc = xfb.main(["serve"])

        responses = [json.loads(line) for line in stdout_buf.getvalue().decode("utf-8").strip().splitlines() if line]
        self.assertEqual(responses[0].get("id"), "det-1")
        self.assertEqual(responses[1].get("id"), "det-2")


if __name__ == "__main__":
    unittest.main()
