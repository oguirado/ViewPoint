"""Unit and CLI tests for vadapt.vserve persistent C2 JSON-lines server."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zlib

from vadapt import cu_describe, ramws, rtring, vserve
from visioncore.errors import RC_CLASSES, VqaError


def make_png(width=96, height=80, rect=None, color=b"\xff\xff\xff", rects=()):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    pixels = bytearray(width * height * 3)
    for x, y, w, h in ((rect,) if rect else ()) + tuple(rects):
        for row in range(y, y + h):
            offset = (row * width + x) * 3
            pixels[offset:offset + w * 3] = color * w
    raw = b"".join(b"\0" + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))
    encoded = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(raw))
               + chunk(b"IEND", b""))
    return encoded, bytes(pixels)


@contextlib.contextmanager
def ram_png(data):
    fd = os.memfd_create("vserve-test-png")
    try:
        os.write(fd, data)
        yield fd, "/proc/self/fd/%d" % fd
    finally:
        os.close(fd)


class BoundedInput(io.BytesIO):
    """Stream wrapper verifying that no read/readline exceeds the configured bound."""

    def __init__(self, value, maximum):
        super().__init__(value)
        self.maximum = maximum
        self.calls = []

    def _check(self, size):
        if type(size) is not int or not 0 < size <= self.maximum:
            raise AssertionError("control input attempted an unbounded or oversized read: %r" % (size,))
        self.calls.append(size)

    def readline(self, size=-1):
        self._check(size)
        return super().readline(size)

    def read(self, size=-1):
        self._check(size)
        return super().read(size)


class TestVServeUnit(unittest.TestCase):
    def setUp(self):
        self.assertIn(ramws.is_ram_fs("/dev/shm"), ramws.RAM_FS)
        self.shm_dir = Path(tempfile.mkdtemp(prefix="vserve_ut_", dir="/dev/shm"))
        self.cu_root = Path("/dev/shm/vqa-cu")
        self.cu_root.mkdir(exist_ok=True)
        self.cu_dir = Path(tempfile.mkdtemp(prefix="vserve_cu_", dir=self.cu_root))

    def tearDown(self):
        shutil.rmtree(self.shm_dir, ignore_errors=True)
        shutil.rmtree(self.cu_dir, ignore_errors=True)

    def run_serve(self, raw_bytes, *, max_control_line_bytes=512, max_frame_bytes=1 << 20, max_sessions=4):
        engine = vserve.VServeEngine(
            max_control_line_bytes=max_control_line_bytes,
            max_frame_bytes=max_frame_bytes,
            max_sessions=max_sessions,
        )
        bound = min(max_control_line_bytes + 1, 65536)
        stream = BoundedInput(raw_bytes, max_control_line_bytes + 1)
        out = io.StringIO()
        cleanup = vserve.serve(engine, stream, out, max_control_line_bytes=max_control_line_bytes)
        lines = out.getvalue().splitlines()
        replies = [json.loads(line) for line in lines]
        for line, reply in zip(lines, replies):
            self.assertEqual(
                set(reply),
                {"code", "detail", "ok", "op", "rc", "request_id", "result", "schema"},
            )
            self.assertEqual(reply["schema"], "ve.vserve.reply/v1")
            self.assertEqual(line, json.dumps(reply, sort_keys=True, separators=(",", ":"), allow_nan=False))
            self.assertIn(reply["rc"], RC_CLASSES)
            self.assertNotEqual(reply["rc"], 1)
        self.assertTrue(all(c <= bound or c == max_control_line_bytes + 1 for c in stream.calls))
        self.assertTrue(engine.closed)
        return engine, replies, cleanup

    def test_oversize_line_refused(self):
        """M1 target: oversize control lines are drained in bounded chunks and refused with E_BUDGET/rc 6."""
        valid = json.dumps({"request_id": 9, "op": "hello"}, separators=(",", ":")).encode("utf-8") + b"\n"
        oversized = b'{"request_id":1,"op":"hello","pad":"' + (b"x" * 4096) + b'"}\n'
        _, replies, _ = self.run_serve(oversized + valid, max_control_line_bytes=128)
        self.assertEqual(len(replies), 2)
        self.assertIs(replies[0]["ok"], False)
        self.assertEqual(replies[0]["code"], "E_BUDGET")
        self.assertEqual(replies[0]["rc"], 6)
        self.assertIsNone(replies[0]["request_id"])
        self.assertEqual(replies[0]["detail"]["cap"], "max_control_line_bytes")
        self.assertIs(replies[1]["ok"], True)
        self.assertEqual(replies[1]["code"], "OK")
        self.assertEqual(replies[1]["rc"], 0)
        self.assertEqual(replies[1]["request_id"], 9)

        # Exact byte boundary (includes trailing LF).
        _, exact_replies, _ = self.run_serve(valid, max_control_line_bytes=len(valid))
        _, over_replies, _ = self.run_serve(valid, max_control_line_bytes=len(valid) - 1)
        self.assertEqual(exact_replies[0]["code"], "OK")
        self.assertEqual(exact_replies[0]["rc"], 0)
        self.assertEqual(over_replies[0]["code"], "E_BUDGET")
        self.assertEqual(over_replies[0]["rc"], 6)

        # Oversized unterminated final line is also refused with E_BUDGET/rc 6.
        _, unterm_replies, _ = self.run_serve(b"a" * 300, max_control_line_bytes=64)
        self.assertEqual(len(unterm_replies), 1)
        self.assertEqual(unterm_replies[0]["code"], "E_BUDGET")
        self.assertEqual(unterm_replies[0]["rc"], 6)

    def test_never_rc1(self):
        """M2 target: bad control lines, bad frames, and bad CLI flags never produce rc 1."""
        frame_png, _ = make_png(96, 80)
        diff_png, _ = make_png(48, 40)
        bad_lines = [
            b"\xff\xfe\n",
            b"{\n",
            b"[]\n",
            b"null\n",
            b"123\n",
            b"\n",
            b'{"request_id":1,"request_id":2,"op":"hello"}\n',
            b'{"request_id":NaN,"op":"hello"}\n',
            b'{"request_id":Infinity,"op":"hello"}\n',
            b'{"request_id":0,"op":"hello"}\n',
            b'{"request_id":-5,"op":"hello"}\n',
            b'{"request_id":true,"op":"hello"}\n',
            b'{"request_id":"","op":"hello"}\n',
            b'{"op":"hello"}\n',
            b'{"request_id":1,"op":"unknown_op"}\n',
            b'{"request_id":1,"op":"hello","extra":1}\n',
            b'{"request_id":1,"op":"describe"}\n',
            b'{"request_id":1,"op":"describe","frame":"/proc/self/fd/999999"}\n',
            b'{"request_id":1,"op":"describe","frame_raw":"/tmp/not_shm.rgb,16,16"}\n',
            b'{"request_id":1,"op":"describe","frame_raw":"/dev/shm/../tmp/escape.rgb,16,16"}\n',
            b'{"request_id":1,"op":"open_session","session_id":"../bad"}\n',
        ]
        with ram_png(b"not a png") as (_, bad_png_path), \
                ram_png(frame_png) as (_, f_path), \
                ram_png(diff_png) as (_, d_path):
            bad_lines.append(
                json.dumps({"request_id": 20, "op": "describe", "frame": bad_png_path}).encode("utf-8") + b"\n"
            )
            bad_lines.append(
                json.dumps({"request_id": 21, "op": "describe", "frame": f_path, "prev": d_path}).encode("utf-8") + b"\n"
            )
            _, replies, _ = self.run_serve(b"".join(bad_lines), max_control_line_bytes=512)

        self.assertEqual(len(replies), len(bad_lines))
        for reply in replies:
            self.assertIs(reply["ok"], False, reply)
            self.assertNotEqual(reply["code"], "OK")
            self.assertIn(reply["rc"], (2, 3, 4, 5, 6), reply)
            self.assertNotEqual(reply["rc"], 1, reply)

        # CLI bad options must return 2 (never 1) and emit canonical JSON error.
        bad_Read_argvs = [
            ["--unknown", "1"],
            ["--max-control-line-bytes", "0"],
            ["--max-frame-bytes", "-4"],
            ["--max-sessions", "1.5"],
            ["--max-sessions"],
            ["--max-sessions", "2", "--max-sessions", "3"],
            ["positional"],
        ]
        for argv in bad_Read_argvs:
            with self.subTest(argv=argv):
                out, err = io.StringIO(), io.StringIO()
                rc = vserve.main(argv, stdin=io.BytesIO(b""), stdout=out, stderr=err)
                self.assertEqual(rc, 2)
                self.assertNotEqual(rc, 1)
                self.assertEqual(out.getvalue(), "")
                err_obj = json.loads(err.getvalue())
                self.assertEqual(err_obj["code"], "E_USAGE")
                self.assertEqual(err_obj["rc"], 2)

        # Subprocess CLI verification for bad flag and clean session.
        proc = subprocess.run(
            [sys.executable, "-m", "vadapt.vserve", "--max-frame-bytes", "0"],
            input=b"", capture_output=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertNotEqual(proc.returncode, 1)

    def test_reply_echoes_request_id(self):
        """M3 target: every reply echoes request_id and C2 describe output matches cu_describe."""
        base_png, base_rgb = make_png(96, 80)
        moved_png, moved_rgb = make_png(96, 80, rect=(24, 24, 24, 16))
        raw_path = self.shm_dir / "moved.rgb"
        raw_path.write_bytes(moved_rgb)

        with ram_png(base_png) as (fd_base, path_base), ram_png(moved_png) as (fd_moved, path_moved):
            reqs = [
                {"request_id": 101, "op": "hello"},
                {"request_id": "req-describe-1", "op": "describe", "frame": path_moved, "prev": path_base},
                {"request_id": 202, "frame_raw": f"{raw_path},96,80", "prev": path_base},
                {"request_id": "req-err-404", "op": "describe", "frame": "/proc/self/fd/999999"},
                {"request_id": 303, "op": "status"},
            ]
            payload = b"".join(json.dumps(r, separators=(",", ":")).encode("utf-8") + b"\n" for r in reqs)
            _, replies, _ = self.run_serve(payload, max_control_line_bytes=1024, max_frame_bytes=1 << 20)

            self.assertEqual([r["request_id"] for r in replies], [101, "req-describe-1", 202, "req-err-404", 303])
            self.assertTrue(replies[0]["ok"])
            self.assertIn("frame_raw", replies[0]["result"]["capabilities"])

            direct = cu_describe.describe(path_moved, path_base)
            served_png = dict(replies[1]["result"])
            served_raw = dict(replies[2]["result"])
            for d in (direct, served_png, served_raw):
                self.assertIsInstance(d.pop("timing_us"), int)
            self.assertEqual(served_png, direct)
            self.assertEqual(served_raw, direct)
            self.assertEqual(served_raw["frame_sha256"], hashlib.sha256(moved_rgb).hexdigest())

            # Error reply on valid envelope still echoes request_id.
            self.assertFalse(replies[3]["ok"])
            self.assertEqual(replies[3]["request_id"], "req-err-404")
            self.assertEqual(replies[3]["code"], "E_IO")
            self.assertEqual(replies[3]["rc"], 2)

            # Subprocess end-to-end check with pass_fds.
            proc = subprocess.run(
                [sys.executable, "-m", "vadapt.vserve",
                 "--max-control-line-bytes", "1024",
                 "--max-frame-bytes", str(1 << 20),
                 "--max-sessions", "4"],
                input=json.dumps({"request_id": 777, "op": "describe", "frame": path_moved, "prev": path_base}).encode("utf-8") + b"\n",
                capture_output=True,
                pass_fds=(fd_base, fd_moved),
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            sub_reply = json.loads(proc.stdout.decode("utf-8").strip())
            self.assertEqual(sub_reply["request_id"], 777)
            self.assertTrue(sub_reply["ok"])
            self.assertEqual(sub_reply["result"]["frame_sha256"], direct["frame_sha256"])

    def test_frame_bytes_cap_refused(self):
        """M4 target: frames exceeding max_frame_bytes are refused with E_BUDGET/rc 6 and preserve caller outputs."""
        small_png, small_rgb = make_png(32, 32)
        large_png, large_rgb = make_png(96, 80)
        raw_large = self.shm_dir / "large.rgb"
        raw_large.write_bytes(large_rgb)
        padded_png = small_png + (b"\x00" * 4096)
        marks_sentinel = self.cu_dir / "marks.png"
        marks_sentinel.write_bytes(b"sentinel-bytes")

        cap = len(small_rgb)  # 32 * 32 * 3 = 3072 bytes; 96 * 80 * 3 = 23040 > cap
        with ram_png(small_png) as (_, small_path), \
                ram_png(large_png) as (_, large_path), \
                ram_png(padded_png) as (_, padded_path):
            reqs = [
                {"request_id": 1, "op": "describe", "frame": small_path},
                {"request_id": 2, "op": "describe", "frame": large_path, "marks_out": str(marks_sentinel)},
                {"request_id": 3, "op": "describe", "frame": small_path, "prev": large_path},
                {"request_id": 4, "op": "describe", "frame_raw": f"{raw_large},96,80"},
                {"request_id": 5, "op": "describe", "frame": padded_path},
            ]
            payload = b"".join(json.dumps(r, separators=(",", ":")).encode("utf-8") + b"\n" for r in reqs)
            _, replies, _ = self.run_serve(payload, max_control_line_bytes=1024, max_frame_bytes=cap)

        self.assertTrue(replies[0]["ok"])
        self.assertEqual(replies[0]["rc"], 0)
        for idx in (1, 2, 3, 4):
            self.assertFalse(replies[idx]["ok"], replies[idx])
            self.assertEqual(replies[idx]["code"], "E_BUDGET")
            self.assertEqual(replies[idx]["rc"], 6)
            self.assertEqual(replies[idx]["detail"]["cap"], "max_frame_bytes")
        # Caller-owned marks_out file must be byte-identical after refusal.
        self.assertEqual(marks_sentinel.read_bytes(), b"sentinel-bytes")

    def test_max_sessions_cap_and_lifecycle(self):
        """M5 target: max_sessions refuses excess sessions with E_BUDGET/rc 6 and supports session prev/reap."""
        f0_png, _ = make_png(64, 64)
        f1_png, _ = make_png(64, 64, rect=(16, 16, 16, 16))
        with ram_png(f0_png) as (_, p0), ram_png(f1_png) as (_, p1):
            reqs = [
                {"request_id": 1, "op": "describe", "session_id": "s1", "frame": p0},
                {"request_id": 2, "op": "describe", "session_id": "s1", "frame": p1, "use_session_prev": True},
                {"request_id": 3, "op": "open_session", "session_id": "s2"},
                {"request_id": 4, "op": "open_session", "session_id": "s3"},
                {"request_id": 5, "op": "describe", "session_id": "s3", "frame": p0},
                {"request_id": 6, "op": "close_session", "session_id": "s2"},
                {"request_id": 7, "op": "open_session", "session_id": "s3"},
                {"request_id": 8, "op": "reap"},
                {"request_id": 9, "op": "status"},
            ]
            payload = b"".join(json.dumps(r, separators=(",", ":")).encode("utf-8") + b"\n" for r in reqs)
            engine, replies, cleanup = self.run_serve(
                payload, max_control_line_bytes=1024, max_frame_bytes=1 << 20, max_sessions=2
            )

        self.assertTrue(replies[0]["ok"])
        self.assertIsNone(replies[0]["result"]["changed"])
        self.assertTrue(replies[1]["ok"])
        self.assertIs(replies[1]["result"]["changed"], True)
        self.assertTrue(replies[2]["ok"])
        self.assertEqual(replies[2]["result"]["active_sessions"], 2)

        # 3rd session (requests 4 and 5) refused with E_BUDGET / rc 6.
        self.assertFalse(replies[3]["ok"])
        self.assertEqual(replies[3]["code"], "E_BUDGET")
        self.assertEqual(replies[3]["rc"], 6)
        self.assertEqual(replies[3]["detail"]["cap"], "max_sessions")
        self.assertFalse(replies[4]["ok"])
        self.assertEqual(replies[4]["code"], "E_BUDGET")
        self.assertEqual(replies[4]["rc"], 6)

        # Closing s2 frees a slot so s3 succeeds; reap clears all sessions.
        self.assertTrue(replies[5]["ok"])
        self.assertTrue(replies[5]["result"]["closed"])
        self.assertTrue(replies[6]["ok"])
        self.assertEqual(replies[6]["result"]["active_sessions"], 2)
        self.assertTrue(replies[7]["ok"])
        self.assertEqual(replies[7]["result"]["reaped"], ["s1", "s3"])
        self.assertEqual(replies[8]["result"]["active_sessions"], 0)
        self.assertEqual(replies[8]["result"]["session_bytes"], 0)
        self.assertEqual(engine.session_bytes(), 0)
        self.assertEqual(cleanup["active_sessions"], 0)


if __name__ == "__main__":
    unittest.main()
