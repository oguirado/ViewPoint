import ctypes as ct
import gc
import hashlib
import mmap
import os
import subprocess
from pathlib import Path
from time import monotonic, sleep
import unittest

from fixture import PrivateX, PythonCapture, SURFACE
from capture import Capture, CaptureError, Status, _FrameInfo, _load

LIBRARY = os.environ.get("VP_CAPTURE_TEST_LIBRARY")
ABI_PROBE = os.environ.get("VP_CAPTURE_TEST_ABI_PROBE", str(SURFACE / "target" / "abi_probe"))


def open_capture(display, pid):
    return Capture(display, pid, library=LIBRARY)


def resources():
    fds = {}
    for fd in Path("/proc/self/fd").iterdir():
        try:
            fds[fd.name] = os.readlink(fd)
        except FileNotFoundError:
            pass  # the temporary directory-enumeration fd has closed
    tasks = frozenset(task.name for task in Path("/proc/self/task").iterdir())
    shared = frozenset(
        line for line in Path("/proc/self/maps").read_text().splitlines()
        if line.split()[1].endswith("s")
    )
    return fds, tasks, shared


class CaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = _load(LIBRARY)

    def assert_reclaimed(self, before):
        # pthread_join has returned, but the kernel can still expose the exiting
        # TID in /proc briefly. Descriptor and mapping identity must match now.
        expected_fds, expected_tasks, expected_maps = before
        fds, tasks, maps = resources()
        self.assertEqual(fds, expected_fds, "descriptor leaked")
        self.assertEqual(maps, expected_maps, "shared mapping leaked")
        deadline = monotonic() + 0.25
        while tasks != expected_tasks and monotonic() < deadline:
            sleep(0.005)
            tasks = frozenset(task.name for task in Path("/proc/self/task").iterdir())
        self.assertEqual(tasks, expected_tasks, "worker thread leaked")

    def test_c_header_and_exported_abi(self):
        with PrivateX() as x:
            subprocess.run([ABI_PROBE, str(x.display), str(x.pid)],
                           check=True, timeout=5)

    def assert_output(self, actual, digest, oracle):
        pixels, expected_hash = oracle
        self.assertEqual(bytes(actual), pixels)
        self.assertEqual(digest, expected_hash)
        self.assertEqual(digest, hashlib.sha256(actual).digest())

    def test_start_stop_reclaims_thread_descriptors_and_mapping(self):
        with PrivateX() as x:
            before = resources()
            for _ in range(8):
                with open_capture(x.display, x.pid) as capture:
                    live = resources()
                    self.assertGreater(len(live[0]), len(before[0]))
                    self.assertEqual(len(live[1]), len(before[1]) + 1)
                    self.assertGreater(len(live[2]), len(before[2]))
                    buffer = bytearray(capture.info.byte_len)
                    self.assertEqual(capture.next_frame(buffer), hashlib.sha256(buffer).digest())
                capture.close()
                with self.assertRaises(CaptureError) as error:
                    capture.next_frame(buffer)
                self.assertEqual(error.exception.status, Status.CLOSED)
                self.assert_reclaimed(before)

    def test_mutant_missing_stop_is_detected(self):
        with PrivateX() as x:
            before = resources()
            capture = open_capture(x.display, x.pid)
            try:
                with self.assertRaisesRegex(AssertionError, "leaked"):
                    self.assert_reclaimed(before)
            finally:
                capture.close()
            self.assert_reclaimed(before)

    def test_unclosed_python_owner_is_finalized(self):
        with PrivateX() as x:
            before = resources()
            capture = open_capture(x.display, x.pid)
            del capture
            gc.collect()
            self.assert_reclaimed(before)

    def test_bad_display_range_missing_socket_and_wrong_owner_refused(self):
        with PrivateX() as x:
            before = resources()
            for display, pid in ((0, x.pid), (89, x.pid), (65536, x.pid),
                                 (65535, x.pid), (x.display, os.getpid())):
                with self.subTest(display=display, pid=pid):
                    out, info = ct.c_void_p(), _FrameInfo()
                    status = self.lib.vp_capture_start(display, pid, ct.byref(out), ct.byref(info))
                    self.assertEqual(status, Status.BAD_DISPLAY)
                    self.assertFalse(out)
                    self.assertEqual(info.byte_len, 0)
                    self.assert_reclaimed(before)
            with PrivateX(display=94) as other:
                with self.assertRaises(CaptureError) as error:
                    open_capture(x.display, other.pid)
                self.assertEqual(error.exception.status, Status.BAD_DISPLAY)

    def test_different_visual_refused_and_reclaimed(self):
        with PrivateX(depth=16) as x:
            before = resources()
            with self.assertRaises(CaptureError) as error:
                open_capture(x.display, x.pid)
            self.assertEqual(error.exception.status, Status.UNSUPPORTED_FORMAT)
            self.assert_reclaimed(before)

    def test_mutant_bypassing_owner_gate_is_detected(self):
        with PrivateX() as x, PrivateX(display=94) as other:
            def require_wrong_owner_refused(start):
                with self.assertRaises(CaptureError) as error:
                    with start(other.pid):
                        pass
                self.assertEqual(error.exception.status, Status.BAD_DISPLAY)

            require_wrong_owner_refused(lambda pid: open_capture(x.display, pid))
            with self.assertRaisesRegex(AssertionError, "CaptureError not raised"):
                require_wrong_owner_refused(lambda _pid: open_capture(x.display, x.pid))

    def test_rgb_geometry_digest_and_changed_pixels_match_python(self):
        with PrivateX(95, 63) as x, open_capture(x.display, x.pid) as capture, PythonCapture(x) as baseline:
            self.assertEqual(
                (capture.info.width, capture.info.height, capture.info.row_stride, capture.info.format_id),
                (95, 63, 285, 1),
            )
            buffer = bytearray(capture.info.byte_len + 17)
            buffer[-17:] = b"!" * 17
            seen = set()
            for pattern in (0, 1, 0):
                x.pattern(pattern)
                digest = capture.next_frame(buffer)
                with memoryview(buffer)[:capture.info.byte_len] as pixels:
                    self.assert_output(pixels, digest, baseline.next_frame())
                self.assertEqual(buffer[-17:], b"!" * 17)
                seen.add(digest)
            self.assertEqual(len(seen), 2)

    def test_mutant_channel_swap_and_stale_frame_are_detected(self):
        with PrivateX() as x, open_capture(x.display, x.pid) as capture, PythonCapture(x) as baseline:
            x.pattern(0)
            first = bytearray(capture.info.byte_len)
            first_digest = capture.next_frame(first)
            self.assert_output(first, first_digest, baseline.next_frame())
            swapped = first.copy()
            swapped[0::3], swapped[2::3] = first[2::3], first[0::3]
            with self.assertRaises(AssertionError):
                self.assert_output(swapped, hashlib.sha256(swapped).digest(), baseline.next_frame())
            x.pattern(1)
            with self.assertRaises(AssertionError):
                self.assert_output(first, first_digest, baseline.next_frame())

    def test_python_mmap_loan_and_noncontiguous_refusal(self):
        with PrivateX() as x, open_capture(x.display, x.pid) as capture:
            x.pattern(0)
            size = capture.info.byte_len
            with mmap.mmap(-1, size + 2) as mapped, memoryview(mapped) as entire:
                entire[0], entire[-1] = 11, 23
                with entire[1:-1] as pixels, entire[::2] as strided:
                    with self.assertRaisesRegex(ValueError, "C-contiguous"):
                        capture.next_frame(strided)
                    digest = capture.next_frame(pixels)
                    self.assertEqual(digest, hashlib.sha256(pixels).digest())
                self.assertEqual((entire[0], entire[-1]), (11, 23))

    def test_buffer_errors_do_not_touch_outputs_or_consume_session(self):
        with PrivateX() as x, open_capture(x.display, x.pid) as capture:
            size = capture.info.byte_len
            dst = (ct.c_uint8 * size).from_buffer_copy(b"x" * size)
            digest = (ct.c_uint8 * 32).from_buffer_copy(b"y" * 32)
            next_frame = self.lib.vp_capture_next_frame
            self.assertEqual(next_frame(capture._handle, dst, size - 1, digest), Status.BUFFER_TOO_SMALL)
            self.assertEqual(next_frame(capture._handle, dst, size, ct.byref(dst, size - 8)),
                             Status.INVALID_ARGUMENT)
            self.assertEqual(bytes(dst), b"x" * size)
            self.assertEqual(bytes(digest), b"y" * 32)
            self.assertEqual(next_frame(capture._handle, dst, size, digest), Status.OK)
            self.assertEqual(bytes(digest), hashlib.sha256(dst).digest())

    def test_server_exit_breaks_session_and_reclaims_worker(self):
        with PrivateX() as x:
            before = resources()
            with open_capture(x.display, x.pid) as capture:
                x.stop()
                dst = bytearray(capture.info.byte_len)
                with self.assertRaises(CaptureError) as error:
                    capture.next_frame(dst)
                self.assertEqual(error.exception.status, Status.X11_ERROR)
                with self.assertRaises(CaptureError) as error:
                    capture.next_frame(dst)
                self.assertEqual(error.exception.status, Status.CLOSED)
                self.assert_reclaimed(before)
            self.assert_reclaimed(before)

    def test_stalled_start_times_out_and_reclaims_resources(self):
        with PrivateX() as x:
            before = resources()
            with x.stall():
                with self.assertRaises(CaptureError) as error:
                    open_capture(x.display, x.pid)
                self.assertEqual(error.exception.status, Status.TIMED_OUT)
                self.assert_reclaimed(before)

    def test_stalled_frame_times_out_before_returning_buffer_loan(self):
        with PrivateX() as x:
            before = resources()
            with open_capture(x.display, x.pid) as capture:
                dst = bytearray(b"z" * capture.info.byte_len)
                with x.stall():
                    with self.assertRaises(CaptureError) as error:
                        capture.next_frame(dst)
                    self.assertEqual(error.exception.status, Status.TIMED_OUT)
                    self.assert_reclaimed(before)
                self.assertEqual(dst, b"z" * capture.info.byte_len)
