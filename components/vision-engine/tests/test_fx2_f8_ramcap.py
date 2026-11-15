"""D11 RAM capture contract: offline CPU tests with tiny verified tmpfs rings.

No X server, encoded image, persistent frame file, e16 fixture, or large ring
benchmark is used here. The injected workspace deliberately avoids the real
workspace's host free-space floor, while preserving actual tmpfs storage.
"""
import gc
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from vadapt import ramws, rtring, xfb
from vadapt import x11_ramcap as ramcap
from tests.test_xfb import build_xwd, grad


ERRORS = (ramcap.CaptureError, xfb.XwdError, ramws.RamError)


class Clock:
    def __init__(self):
        self.value = 1000000
        self.waits = []

    def __call__(self):
        return self.value

    def advance(self, n):
        self.value += n

    def wait(self, event, timeout_ns):
        self.waits.append(timeout_ns)
        self.advance(timeout_ns)
        return event.is_set()


class TinyWorkspace:
    """A test-only logical quota adapter over an independently verified tmpfs."""

    def __init__(self, root, quota):
        self.root = Path(root)
        if ramws.is_ram_fs(self.root) not in ramws.RAM_FS:
            raise AssertionError("test workspace must be a verified RAM filesystem")
        self.quota, self.used = quota, 0
        self.closed = False
        self.reservations = []

    def path(self, rel):
        if self.closed:
            raise ramws.RamError("E_RAM_LOCKED", "test workspace closed")
        rel = Path(rel)
        if rel.is_absolute() or ".." in rel.parts:
            raise ramws.RamError("E_RAM_ESCAPE", "bad test path")
        path = self.root / rel
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ramws.RamError("E_RAM_ESCAPE", "test path escaped")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def reserve(self, n):
        if type(n) is not int or n < 0 or self.used + n > self.quota:
            raise ramws.RamError("E_RAM_QUOTA", "test quota")
        self.reservations.append(n)
        self.used += n

    def release(self, path):
        path = Path(path)
        if not path.is_absolute():
            path = self.root / path
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ramws.RamError("E_RAM_ESCAPE", "test release escaped")
        size = path.stat().st_size if path.exists() else 0
        if path.exists():
            path.unlink()
        self.used = max(0, self.used - size)
        return size

    def account(self):
        self.used = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        return self.used

    def close(self):
        shutil.rmtree(self.root, ignore_errors=False)
        self.closed = True


class RamCase(unittest.TestCase):
    WIDTH, HEIGHT, SLOTS = 4, 2, 2

    def setUp(self):
        self.assertIn(ramws.is_ram_fs("/dev/shm"), ramws.RAM_FS,
                      "offline capture tests require verified tmpfs; no disk fallback")
        self.base = Path(tempfile.mkdtemp(prefix="fx2_f8_", dir="/dev/shm"))
        self.source_root = self.base / "source"
        self.source_root.mkdir()
        self.source_file = self.source_root / "Xvfb_screen0"
        self.raw = build_xwd(self.WIDTH, self.HEIGHT, grad)
        self.source_file.write_bytes(self.raw)
        self.clock = Clock()
        self.workspaces = []
        self.captures = []
        self.readers = []
        self.source = self.config()
        self.need = rtring.ring_bytes(self.SLOTS, self.WIDTH, self.HEIGHT)

    def tearDown(self):
        gc.collect()
        for reader in self.readers:
            reader.close()
        for capture in reversed(self.captures):
            capture.close()
        # Never force-unlink a mapping retained for an outstanding borrowed view.
        retained = sum(capture.SizeBytes() for capture in self.captures)
        self.assertEqual(retained, 0, "test leaked a borrowed view or pending cleanup")
        shutil.rmtree(self.base)

    def config(self, **changes):
        values = dict(root=str(self.source_root), path="Xvfb_screen0",
                      width=self.WIDTH, height=self.HEIGHT,
                      nocursor_evidence="offline fixture; trusted launcher uses -nocursor",
                      kind="xvfb-xwd")
        values.update(changes)
        return ramcap.SourceConfig(**values)

    def workspace(self, session_id, size):
        root = Path(tempfile.mkdtemp(prefix="ws_", dir=self.base))
        ws = TinyWorkspace(root, size)
        self.workspaces.append(ws)
        return ws

    def capture(self, **changes):
        values = dict(max_frame_bytes=len(self.raw), max_session_bytes=self.need,
                      max_total_bytes=self.need * 3, max_sessions=3,
                      ring_slots=self.SLOTS, interval_ns=10, ttl_ns=1000,
                      tries=4, workspace_factory=self.workspace,
                      clock=self.clock, wait=self.clock.wait)
        values.update(changes)
        capture = ramcap.RamCapture({"screen": self.source}, **values)
        self.captures.append(capture)
        return capture

    def start(self, capture=None, session_id="s", **changes):
        capture = capture or self.capture()
        return capture, capture.Start(session_id, "screen", background=False, **changes)

    def reader(self, metadata):
        reader = rtring.RingReader(metadata["ring_path"], root=self.base)
        self.readers.append(reader)
        return reader

    def assertTyped(self, call, code=None):
        with self.assertRaises(ERRORS) as caught:
            call()
        self.assertTrue(caught.exception.code.startswith("E_"))
        if code is not None:
            self.assertEqual(caught.exception.code, code)
        return caught.exception


class TestTrustedSource(RamCase):
    def test_fbread_raw_exact_and_frame_cap_boundary(self):
        self.assertEqual(ramcap.fbread(self.source, max_bytes=len(self.raw)), self.raw)
        self.assertTyped(lambda: ramcap.fbread(self.source, max_bytes=len(self.raw) - 1),
                         "E_XWD_LIMIT")

    def test_unknown_or_lossy_kind_and_absent_nocursor_fail_before_open(self):
        cases = [dict(kind="xpra"), dict(kind="jpeg"), dict(kind="unknown"),
                 dict(nocursor_evidence=""), dict(nocursor_evidence=None),
                 dict(nocursor_evidence=True)]
        for changes in cases:
            with self.subTest(changes=changes), mock.patch.object(ramcap.os, "open") as opened:
                self.assertTyped(lambda: ramcap.fbread(self.config(**changes), max_bytes=len(self.raw)))
                opened.assert_not_called()

    def test_relative_escape_and_symlink_sources_refused(self):
        for path in ("../Xvfb_screen0", "/dev/shm/Xvfb_screen0", "a/../../x", "x\0y"):
            with self.subTest(path=path):
                self.assertTyped(lambda: ramcap.fbread(self.config(path=path), max_bytes=len(self.raw)))
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "frame").write_bytes(self.raw)
        (self.source_root / "leaf").symlink_to(outside / "frame")
        (self.source_root / "parent").symlink_to(outside, target_is_directory=True)
        for path in ("leaf", "parent/frame"):
            with self.subTest(path=path):
                self.assertTyped(lambda: ramcap.fbread(self.config(path=path), max_bytes=len(self.raw)))

    def test_non_ram_root_and_workspace_are_refused(self):
        # Read-only existing code path: no frame is ever written on persistent storage.
        code_path = Path(__file__).resolve()
        self.assertTyped(lambda: ramcap.fbread(
            self.config(root=str(code_path.parent), path=code_path.name), max_bytes=1 << 20))
        class DiskWorkspace:
            root = code_path.parent

            def path(self, rel):
                raise AssertionError("non-RAM workspace reached allocation")

            def close(self):
                pass
        cap = self.capture(workspace_factory=lambda name, n: DiskWorkspace())
        self.assertTyped(lambda: cap.Start("s", "screen", background=False))
        self.assertEqual(cap.SizeBytes(), 0)

    def test_unknown_alias_has_no_workspace_or_publication(self):
        cap = self.capture()
        self.assertTyped(lambda: cap.Start("s", "unknown", background=False))
        self.assertEqual(cap.SizeBytes(), 0)
        self.assertEqual(self.workspaces, [])

    def test_identity_replacement_is_rejected_without_publication(self):
        cap, metadata = self.start()
        reader = self.reader(metadata)
        replacement = self.source_root / "replacement"
        replacement.write_bytes(self.raw)
        os.replace(replacement, self.source_file)
        self.clock.advance(10)
        self.assertTyped(lambda: cap.capture_once("s"))
        self.assertEqual(reader.head(), 0)

    def test_geometry_or_byte_layout_change_is_rejected_without_publication(self):
        # Equal payload length and same inode isolate geometry/layout pinning.
        for raw in (build_xwd(2, 4, grad), build_xwd(4, 2, grad, byte_order=1)):
            with self.subTest(header=raw[:100]):
                self.source_file.write_bytes(self.raw)
                cap, metadata = self.start(session_id="layout%d" % len(self.captures))
                reader = rtring.RingReader(metadata["ring_path"], root=self.base)
                self.assertEqual(len(raw), len(self.raw))
                self.source_file.write_bytes(raw)
                self.clock.advance(10)
                try:
                    self.assertTyped(lambda: cap.capture_once(metadata["session_id"]))
                    self.assertEqual(reader.head(), 0)
                finally:
                    reader.close()
                    cap.close()

    def test_unstable_sample_never_advances_head_or_render_index(self):
        cap, metadata = self.start()
        reader = self.reader(metadata)
        self.clock.advance(10)
        with mock.patch.object(xfb, "read_screen_fd", side_effect=xfb.XwdError("E_TORN", "fixture")):
            self.assertTyped(lambda: cap.capture_once("s"), "E_TORN")
        self.assertEqual(reader.head(), 0)
        self.clock.advance(10)
        frame = cap.capture_once("s")
        self.assertEqual(frame.render_index, 1)
        del frame

    def test_layout_change_after_decode_prevents_publication(self):
        cap, metadata = self.start()
        reader = self.reader(metadata)
        convert = xfb.rgb24

        def changed_after_decode(*args, **kwargs):
            result = convert(*args, **kwargs)
            self.source_file.write_bytes(build_xwd(4, 2, grad, byte_order=1))
            return result

        self.clock.advance(10)
        with mock.patch.object(xfb, "rgb24", side_effect=changed_after_decode):
            self.assertTyped(lambda: cap.capture_once("s"))
        self.assertEqual(reader.head(), 0)

    def test_source_identity_initialization_failure_closes_opened_descriptor(self):
        cap = self.capture()
        opened_fds = []
        open_source = ramcap._open_source

        def remember(config):
            fd = open_source(config)
            opened_fds.append(fd)
            return fd

        with mock.patch.object(ramcap, "_open_source", side_effect=remember), \
                mock.patch.object(ramcap, "_identity", side_effect=OSError("injected initial fstat failure")):
            self.assertTyped(lambda: cap.Start("s", "screen", background=False), "E_CAPTURE_IO")
        self.assertEqual(len(opened_fds), 1)
        with self.assertRaises(OSError) as closed:
            os.fstat(opened_fds[0])
        self.assertEqual(closed.exception.errno, 9)  # EBADF: failed construction retained no descriptor.
        self.assertEqual(cap.SizeBytes(), 0)
        self.assertEqual(self.workspaces, [])


class TestLifecycleAndBudgets(RamCase):
    def test_start_capture_stop_retains_and_reap_releases(self):
        cap, metadata = self.start()
        self.assertEqual(metadata["frames"], 1)
        path = Path(metadata["ring_path"])
        self.assertEqual(path.stat().st_size, self.need)
        self.assertEqual(cap.SizeBytes(), self.need)
        self.assertIsNone(cap.capture_once("s"))
        self.clock.advance(10)
        frame = cap.capture_once("s")
        self.assertEqual(frame.render_index, 1)
        self.assertEqual(frame.view.shape, (self.HEIGHT, self.WIDTH, 3))
        self.assertEqual(frame.view.tobytes(), xfb.rgb24(self.raw))
        self.assertFalse(frame.view.flags.writeable)
        self.assertFalse(frame.view.flags.owndata)
        self.assertTrue(frame.valid())
        del frame
        self.assertTrue(cap.Stop("s"))
        self.assertTrue(cap.Stop("s"))
        self.assertTrue(path.exists())
        self.assertEqual(cap.SizeBytes("s"), self.need)
        self.assertTyped(lambda: cap.capture_once("s"))
        self.assertEqual(cap.Reap("s")["reaped"], ["s"])
        self.assertFalse(path.exists())
        self.assertEqual(cap.SizeBytes(), 0)
        self.assertEqual(cap.Reap("s")["reaped"], [])
        self.assertFalse(cap.Stop("missing"))
        self.assertEqual(cap.SizeBytes("missing"), 0)

    def test_idempotent_start_and_conflict_and_stopped_retention(self):
        cap, metadata = self.start()
        again = cap.Start("s", "screen", background=False)
        self.assertEqual(again, metadata)
        self.assertEqual(len(self.workspaces), 1)
        self.assertTyped(lambda: cap.Start("s", "screen", ttl_ns=999, background=False))
        cap.Stop("s")
        with mock.patch.object(xfb, "read_screen_fd") as read:
            stopped = cap.Start("s", "screen", background=False)
            read.assert_not_called()
        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(stopped["frames"], 1)
        self.assertEqual(cap.SizeBytes(), self.need)

    def test_exact_session_budget_boundary_and_header_accounting(self):
        cap = self.capture(max_session_bytes=self.need - 1)
        self.assertTyped(lambda: cap.Start("small", "screen", background=False))
        self.assertEqual(cap.SizeBytes(), 0)
        self.assertEqual(self.workspaces, [])
        cap = self.capture(max_session_bytes=self.need)
        cap.Start("exact", "screen", background=False)
        self.assertEqual(cap.SizeBytes(), self.need)
        self.assertGreater(self.need, self.SLOTS * self.WIDTH * self.HEIGHT * 3)
        self.assertEqual(self.workspaces[-1].reservations, [self.need])

    def test_exact_total_budget_counts_stopped_and_frees_only_after_reap(self):
        cap = self.capture(max_total_bytes=self.need)
        cap.Start("a", "screen", background=False)
        self.assertEqual(cap.SizeBytes(), self.need)
        self.assertTyped(lambda: cap.Start("b", "screen", background=False))
        cap.Stop("a")
        self.assertTyped(lambda: cap.Start("b", "screen", background=False))
        cap.Reap("a")
        cap.Start("b", "screen", background=False)
        self.assertEqual(cap.SizeBytes(), self.need)

    def test_max_sessions_counts_retained_sessions(self):
        cap = self.capture(max_sessions=1)
        cap.Start("a", "screen", background=False)
        cap.Stop("a")
        self.assertTyped(lambda: cap.Start("b", "screen", background=False))
        cap.Reap("a")
        cap.Start("b", "screen", background=False)
        self.assertEqual(cap.SizeBytes(), self.need)

    def test_configuration_domains_are_positive_exact_integers(self):
        names = ("max_frame_bytes", "max_session_bytes", "max_total_bytes", "max_sessions",
                 "ring_slots", "interval_ns", "ttl_ns")
        for name in names:
            for bad in (True, False, 0, -1, 1.0, "1", None):
                with self.subTest(name=name, bad=bad):
                    self.assertTyped(lambda: self.capture(**{name: bad}))
        for tries in (True, 0, 1, 2.0, "2"):
            with self.subTest(tries=tries):
                self.assertTyped(lambda: self.capture(tries=tries))
        self.assertEqual(self.workspaces, [])

    def test_start_override_domains_and_configured_bounds(self):
        cap = self.capture()
        for options in (dict(slots=True), dict(slots=0), dict(slots=3),
                        dict(interval_ns=1.0), dict(interval_ns=9),
                        dict(ttl_ns=False), dict(ttl_ns=1001)):
            with self.subTest(options=options):
                self.assertTyped(lambda: cap.Start("s", "screen", background=False, **options))
        self.assertEqual(cap.SizeBytes(), 0)
        self.assertEqual(self.workspaces, [])

    def test_expiry_boundary_retains_charge_until_reap(self):
        cap, metadata = self.start(ttl_ns=30)
        reader = self.reader(metadata)
        self.clock.advance(30)
        self.assertTyped(lambda: cap.capture_once("s"))
        self.assertEqual(reader.head(), 0)
        self.assertEqual(cap.SizeBytes(), self.need)
        self.assertEqual(cap.Reap()["reaped"], ["s"])
        self.assertEqual(cap.SizeBytes(), 0)

    def test_borrowed_derived_slice_defers_release_and_accounting(self):
        cap, metadata = self.start()
        self.clock.advance(10)
        frame = cap.capture_once("s")
        view = frame.view[:, :1, :]
        expected = view.tobytes()
        del frame
        cap.Stop("s")
        first = cap.Reap("s")
        self.assertEqual(first["reaped"], [])
        self.assertIn("s", first["retained"])
        self.assertEqual(cap.SizeBytes(), self.need)
        self.assertTrue(Path(metadata["ring_path"]).exists())
        self.assertEqual(view.tobytes(), expected)
        self.assertFalse(self.workspaces[-1].closed)
        del view
        gc.collect()
        self.assertEqual(cap.Reap("s")["reaped"], ["s"])
        self.assertEqual(cap.SizeBytes(), 0)

    def test_borrowed_frame_validity_tracks_slot_reuse(self):
        cap, _ = self.start()
        self.clock.advance(10)
        old = cap.capture_once("s")
        self.assertTrue(old.valid())
        for _ in range(self.SLOTS):
            self.clock.advance(10)
            frame = cap.capture_once("s")
            del frame
        self.assertFalse(old.valid())
        del old

    def test_release_failure_keeps_cleanup_owner_and_retries(self):
        cap, metadata = self.start()
        ws = self.workspaces[-1]
        release = ws.release
        calls = []

        def flaky(path):
            calls.append(path)
            if len(calls) == 1:
                raise OSError("injected release failure")
            return release(path)

        ws.release = flaky
        cap.Stop("s")
        self.assertEqual(cap.Reap("s")["reaped"], [])
        self.assertEqual(cap.SizeBytes(), self.need)
        self.assertTrue(Path(metadata["ring_path"]).exists())
        self.assertEqual(cap.Reap("s")["reaped"], ["s"])
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(cap.SizeBytes(), 0)

    def test_allocation_failure_rolls_back_ring_and_session(self):
        cap = self.capture()
        with mock.patch.object(rtring, "_map", side_effect=OSError("injected map failure")):
            self.assertTyped(lambda: cap.Start("s", "screen", background=False))
        self.assertEqual(cap.SizeBytes(), 0)
        self.assertTrue(all(not ws.root.exists() for ws in self.workspaces))
        cap.Start("s", "screen", background=False)
        self.assertEqual(cap.SizeBytes(), self.need)

    def test_failed_initialization_keeps_owner_when_rollback_needs_retry(self):
        close_calls = []

        def factory(session_id, size):
            ws = self.workspace(session_id, size)
            close = ws.close

            def close_once_failed():
                close_calls.append(1)
                if len(close_calls) == 1:
                    raise OSError("injected rollback close failure")
                return close()

            ws.close = close_once_failed
            return ws

        cap = self.capture(workspace_factory=factory)
        with mock.patch.object(rtring, "RingReader", side_effect=OSError("injected reader failure")):
            self.assertTyped(lambda: cap.Start("s", "screen", background=False))
        self.assertEqual(cap.SizeBytes(), self.need)
        self.assertTrue(self.workspaces[-1].root.exists())
        self.assertEqual(cap.Reap("s")["reaped"], ["s"])
        self.assertEqual(cap.SizeBytes(), 0)
        self.assertFalse(self.workspaces[-1].root.exists())

    def test_failed_thread_start_and_failed_rollback_remain_reapable(self):
        close_calls = []

        def factory(session_id, size):
            ws = self.workspace(session_id, size)
            close = ws.close

            def close_once_failed():
                close_calls.append(1)
                if len(close_calls) == 1:
                    raise OSError("injected workspace close failure")
                return close()

            ws.close = close_once_failed
            return ws

        cap = self.capture(workspace_factory=factory)
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("injected thread start failure")):
            self.assertTyped(lambda: cap.Start("s", "screen"), "E_CAPTURE_IO")
        self.assertEqual(cap.SizeBytes(), self.need)
        self.assertTrue(self.workspaces[-1].root.exists())
        # Reap must not attempt to join the thread whose start() failed.
        self.assertEqual(cap.Reap("s")["reaped"], ["s"])
        self.assertEqual(cap.SizeBytes(), 0)
        self.assertEqual(len(close_calls), 2)


class TestCadenceAndCancellation(RamCase):
    def test_no_catch_up_burst_after_late_capture(self):
        cap, _ = self.start()
        self.clock.advance(105)
        frame = cap.capture_once("s")
        self.assertEqual(frame.render_index, 1)
        del frame
        self.assertIsNone(cap.capture_once("s"))
        self.clock.advance(9)
        self.assertIsNone(cap.capture_once("s"))
        self.clock.advance(1)
        frame = cap.capture_once("s")
        self.assertEqual(frame.render_index, 2)
        del frame

    def test_cadence_is_from_completion_and_wait_is_injectable(self):
        cap, _ = self.start()
        self.clock.advance(10)
        read = xfb.read_screen_fd

        def slow(*args, **kwargs):
            value = read(*args, **kwargs)
            self.clock.advance(25)
            return value

        with mock.patch.object(xfb, "read_screen_fd", side_effect=slow):
            frame = cap.capture_once("s")
        del frame
        self.assertIsNone(cap.capture_once("s"))
        frame = cap.capture_once("s", wait=True)
        self.assertEqual(self.clock.waits[-1], 10)
        self.assertEqual(frame.render_index, 2)
        del frame

    def test_cancellation_after_sample_prevents_publication(self):
        cancel = threading.Event()
        cap = self.capture(cancel_factory=lambda: cancel)
        cap, metadata = self.start(cap)
        reader = self.reader(metadata)
        read = xfb.read_screen_fd

        def cancelled_after_read(*args, **kwargs):
            value = read(*args, **kwargs)
            cancel.set()
            return value

        self.clock.advance(10)
        with mock.patch.object(xfb, "read_screen_fd", side_effect=cancelled_after_read):
            self.assertTyped(lambda: cap.capture_once("s"))
        self.assertEqual(reader.head(), 0)

    def test_external_manual_cancellation_becomes_eligible_for_ordinary_reap(self):
        cancel = threading.Event()
        cap = self.capture(cancel_factory=lambda: cancel)
        cap.Start("s", "screen", background=False)
        cancel.set()
        self.assertTyped(lambda: cap.capture_once("s"), "E_CANCELLED")
        self.assertEqual(cap.SizeBytes(), self.need)
        self.assertEqual(cap.Reap()["reaped"], ["s"])
        self.assertEqual(cap.SizeBytes(), 0)

    def test_force_shutdown_works_with_backward_or_invalid_clock(self):
        for shutdown in ("close", "eof"):
            for value in (0, True, None, 1.5):
                with self.subTest(shutdown=shutdown, clock=value):
                    clock = Clock()
                    cap = self.capture(clock=clock, wait=clock.wait)
                    cap.Start("s", "screen", background=False)
                    clock.value = value
                    if shutdown == "close":
                        result = cap.close()
                    else:
                        output = io.StringIO()
                        result = ramcap.serve(cap, io.BytesIO(), output, max_control_line_bytes=256)
                        self.assertEqual(output.getvalue(), "")
                    self.assertEqual(result, {"reaped": ["s"], "retained": []})
                    self.assertEqual(cap.SizeBytes(), 0)

    def test_huge_integer_interval_is_clamped_before_wait_float_conversion(self):
        waits = []

        class CancelOnWait(threading.Event):
            def wait(self, timeout=None):
                waits.append(timeout)
                self.set()
                return True

        huge = 10 ** 400
        cap = self.capture(interval_ns=huge, ttl_ns=huge * 2,
                           wait=None, cancel_factory=CancelOnWait)
        cap.Start("s", "screen", background=False)
        self.assertTyped(lambda: cap.capture_once("s", wait=True), "E_CANCELLED")
        self.assertEqual(len(waits), 1)
        self.assertGreater(waits[0], 0)
        self.assertLessEqual(waits[0], threading.TIMEOUT_MAX)
        self.assertEqual(cap.Reap()["reaped"], ["s"])

    def test_eof_cancels_waiting_background_worker_and_reaps(self):
        waiting = threading.Event()
        cancel = threading.Event()

        def wait(event, timeout_ns):
            waiting.set()
            return event.wait(timeout_ns / 1000000000)

        # Real monotonic clock only for this bounded cancellation seam test.
        import time
        cap = self.capture(clock=time.monotonic_ns, wait=wait,
                           interval_ns=1000000000, ttl_ns=10000000000,
                           cancel_factory=lambda: cancel)
        cap.Start("s", "screen")
        self.assertTrue(waiting.wait(2), "capture worker did not reach its cancellable wait")
        out = io.StringIO()
        ramcap.serve(cap, io.BytesIO(b""), out, max_control_line_bytes=256)
        self.assertTrue(cancel.is_set())
        self.assertEqual(cap.SizeBytes(), 0)
        self.assertEqual(out.getvalue(), "")
        self.assertTrue(all(ws.closed for ws in self.workspaces))

    def test_idempotent_start_does_not_block_behind_background_wait(self):
        import time
        waiting, done = threading.Event(), threading.Event()

        def wait(event, timeout_ns):
            waiting.set()
            return event.wait(timeout_ns / 1000000000)

        cap = self.capture(clock=time.monotonic_ns, wait=wait,
                           interval_ns=1000000000, ttl_ns=10000000000)
        cap.Start("s", "screen")
        self.assertTrue(waiting.wait(2))
        answers, errors = [], []

        def again():
            try:
                answers.append(cap.Start("s", "screen"))
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()

        caller = threading.Thread(target=again)
        caller.start()
        try:
            completed_while_waiting = done.wait(1)
        finally:
            cap.Stop("s")
            caller.join(2)
        self.assertFalse(caller.is_alive())
        self.assertTrue(completed_while_waiting, "idempotent Start blocked behind capture cadence wait")
        self.assertEqual(errors, [])
        self.assertEqual(answers[0]["frames"], 1)


class RecordingCapture:
    def __init__(self):
        self.calls = []
        self.closed = False

    def Start(self, session_id, source, **kwargs):
        self.calls.append(("Start", session_id, source, kwargs))
        return {"session_id": session_id, "state": "running"}

    def Stop(self, session_id):
        self.calls.append(("Stop", session_id))
        return True

    def SizeBytes(self, session_id=None):
        self.calls.append(("SizeBytes", session_id))
        return 12

    def Reap(self, session_id=None, **kwargs):
        self.calls.append(("Reap", session_id, kwargs))
        return {"reaped": [], "retained": []}

    def close(self):
        self.closed = True
        return {"reaped": [], "retained": []}


class BoundedInput(io.BytesIO):
    def __init__(self, value, maximum):
        super().__init__(value)
        self.maximum, self.calls = maximum, []

    def _check(self, size):
        if type(size) is not int or not 0 < size <= self.maximum:
            raise AssertionError("control input attempted an unbounded or oversized read: %r" % size)
        self.calls.append(size)

    def readline(self, size=-1):
        self._check(size)
        return super().readline(size)

    def read(self, size=-1):
        self._check(size)
        return super().read(size)


class TestControlProtocol(unittest.TestCase):
    def serve(self, raw, limit=256, capture=None):
        capture = capture or RecordingCapture()
        input_stream = BoundedInput(raw, limit + 1)
        output = io.StringIO()
        ramcap.serve(capture, input_stream, output, max_control_line_bytes=limit)
        lines = output.getvalue().splitlines()
        replies = [json.loads(line) for line in lines]
        for line, reply in zip(lines, replies):
            self.assertEqual(set(reply), {"request_id", "op", "ok", "code", "result"})
            self.assertEqual(line, json.dumps(reply, sort_keys=True, separators=(",", ":")))
        self.assertTrue(capture.closed, "EOF must close the capture owner")
        return capture, replies

    def test_exact_operations_and_stable_reply_schema(self):
        requests = [
            {"op": "Start", "request_id": 1, "session_id": "s", "source": "screen",
             "slots": 2, "interval_ns": 10, "ttl_ns": 1000},
            {"op": "Stop", "request_id": 2, "session_id": "s"},
            {"op": "SizeBytes", "request_id": 3, "session_id": None},
            {"op": "Reap", "request_id": 4, "session_id": "s", "force": False},
        ]
        capture, replies = self.serve(b"".join(json.dumps(r).encode() + b"\n" for r in requests))
        self.assertEqual([r["request_id"] for r in replies], [1, 2, 3, 4])
        self.assertEqual([r["op"] for r in replies], [r["op"] for r in requests])
        self.assertTrue(all(r["ok"] is True and r["code"] == "OK" for r in replies))
        self.assertEqual([c[0] for c in capture.calls], ["Start", "Stop", "SizeBytes", "Reap"])
        self.assertEqual(replies[2]["result"], {"bytes": 12})

    def test_duplicate_unknown_missing_fields_and_invalid_json_are_rejected(self):
        invalid = [
            b'{"op":"Stop","op":"SizeBytes","request_id":1,"session_id":"s"}\n',
            b'{"op":"Stop","request_id":1,"session_id":"s","extra":0}\n',
            b'{"op":"Stop","request_id":1}\n',
            b'{"op":"Unknown","request_id":1,"session_id":"s"}\n',
            b'{"op":"SizeBytes","request_id":NaN,"session_id":null}\n',
            b'{"op":"SizeBytes","request_id":Infinity,"session_id":null}\n',
            b'{"op":"SizeBytes","request_id":1,"session_id":{"a":1,"a":2}}\n',
            b'[]\n', b'null\n', b'1\n', b'{\n', b'\xff\n', b'\n',
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                capture, replies = self.serve(raw)
                self.assertEqual(capture.calls, [])
                self.assertEqual(len(replies), 1)
                self.assertFalse(replies[0]["ok"])
                self.assertEqual(replies[0]["code"], "E_CONTROL_SCHEMA")

    def test_positive_integer_domains_and_exact_boolean_force(self):
        base = {"op": "Start", "request_id": 1, "session_id": "s", "source": "screen",
                "slots": 2, "interval_ns": 10, "ttl_ns": 1000}
        for field in ("request_id", "slots", "interval_ns", "ttl_ns"):
            for value in (0, -1, True, False, 1.0, "1", None):
                request = dict(base, **{field: value})
                with self.subTest(field=field, value=value):
                    capture, replies = self.serve(json.dumps(request).encode() + b"\n")
                    self.assertEqual(capture.calls, [])
                    self.assertEqual(replies[0]["code"], "E_CONTROL_SCHEMA")
        for value in (0, 1, "true", None):
            with self.subTest(force=value):
                request = {"op": "Reap", "request_id": 1, "session_id": None, "force": value}
                capture, replies = self.serve(json.dumps(request).encode() + b"\n")
                self.assertEqual(capture.calls, [])
                self.assertEqual(replies[0]["code"], "E_CONTROL_SCHEMA")

    def test_oversized_line_is_bounded_and_preserves_following_request(self):
        valid = b'{"op":"SizeBytes","request_id":7,"session_id":null}\n'
        capture, replies = self.serve(b"x" * 8192 + b"\n" + valid, limit=128)
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[0]["code"], "E_CONTROL_LINE")
        self.assertEqual(replies[1]["request_id"], 7)
        self.assertEqual(replies[1]["code"], "OK")
        self.assertEqual(capture.calls, [("SizeBytes", None)])

    def test_exact_line_byte_boundary_includes_newline(self):
        raw = b'{"op":"SizeBytes","request_id":1,"session_id":null}\n'
        _, exact = self.serve(raw, limit=len(raw))
        _, over = self.serve(raw, limit=len(raw) - 1)
        self.assertEqual(exact[0]["code"], "OK")
        self.assertEqual(over[0]["code"], "E_CONTROL_LINE")

    def test_oversized_unterminated_line_and_valid_final_line(self):
        _, over = self.serve(b"x" * 1024, limit=64)
        self.assertEqual([r["code"] for r in over], ["E_CONTROL_LINE"])
        raw = b'{"op":"SizeBytes","request_id":1,"session_id":null}'
        _, good = self.serve(raw, limit=len(raw))
        self.assertEqual(good[0]["code"], "OK")

    def test_control_line_limit_is_a_positive_exact_integer(self):
        for limit in (True, False, 0, -1, 1.0, "128", None):
            with self.subTest(limit=limit):
                stream = mock.Mock()
                with self.assertRaises(ERRORS):
                    ramcap.serve(RecordingCapture(), stream, io.StringIO(), max_control_line_bytes=limit)
                stream.readline.assert_not_called()
                stream.read.assert_not_called()

    def test_control_line_limit_cannot_overflow_stream_size(self):
        for limit in (sys.maxsize, sys.maxsize + 1, 10 ** 400):
            with self.subTest(limit=limit):
                stream = mock.Mock()
                with self.assertRaises(ramcap.CaptureError) as caught:
                    ramcap.serve(RecordingCapture(), stream, io.StringIO(), max_control_line_bytes=limit)
                self.assertEqual(caught.exception.code, "E_CONFIG")
                stream.readline.assert_not_called()
                stream.read.assert_not_called()


class TestCLI(RamCase):
    def argv(self, **changes):
        values = {"source": "screen", "source-root": str(self.source_root),
                  "source-path": "Xvfb_screen0", "width": str(self.WIDTH), "height": str(self.HEIGHT),
                  "nocursor-evidence": "trusted offline launcher -nocursor",
                  "max-frame-bytes": str(len(self.raw))}
        values.update(changes)
        args = ["fbread"]
        for key, value in values.items():
            args.extend(["--" + key, str(value)])
        return args

    def test_fbread_stdout_is_only_validated_raw_bytes(self):
        output, error = io.BytesIO(), io.StringIO()
        code = ramcap.main(self.argv(), stdout=output, stderr=error)
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), self.raw)
        self.assertEqual(error.getvalue(), "")

    def test_fbread_failure_has_typed_stderr_and_empty_stdout(self):
        bad = bytearray(self.raw)
        struct.pack_into(">I", bad, 7 * 4, 2)
        self.source_file.write_bytes(bad)
        output, error = io.BytesIO(), io.StringIO()
        code = ramcap.main(self.argv(), stdout=output, stderr=error)
        self.assertEqual(code, 5)
        self.assertEqual(output.getvalue(), b"")
        self.assertIn("E_XWD_BYTE_ORDER", error.getvalue())
        self.assertNotIn("Traceback", error.getvalue())

    def test_duplicate_cli_options_rejected_before_source_access(self):
        for suffix in (["--width", "4"], ["--width=4"], ["--source-root=" + str(self.source_root)]):
            with self.subTest(suffix=suffix):
                output, error = io.BytesIO(), io.StringIO()
                with mock.patch.object(ramcap.os, "open") as opened:
                    code = ramcap.main(self.argv() + suffix, stdout=output, stderr=error)
                    opened.assert_not_called()
                self.assertEqual(code, 2)
                self.assertEqual(output.getvalue(), b"")
                self.assertIn("E_USAGE", error.getvalue())

    def test_cli_rejects_missing_cap_unknown_options_and_numeric_coercions(self):
        cases = [self.argv()[:-2], self.argv() + ["--unknown", "1"],
                 self.argv(width="4.0"), self.argv(**{"max-frame-bytes": "0"}),
                 self.argv(tries="1")]
        for args in cases:
            with self.subTest(args=args):
                output, error = io.BytesIO(), io.StringIO()
                with mock.patch.object(ramcap.os, "open") as opened:
                    code = ramcap.main(args, stdout=output, stderr=error)
                    opened.assert_not_called()
                self.assertEqual(code, 2)
                self.assertEqual(output.getvalue(), b"")
                self.assertIn("E_USAGE", error.getvalue())


if __name__ == "__main__":
    unittest.main()
