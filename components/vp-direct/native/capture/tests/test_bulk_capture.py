# SPDX-License-Identifier: AGPL-3.0-only
import ctypes as ct
from hashlib import sha256
from itertools import count
import os
from pathlib import Path
from time import monotonic_ns
import unittest
from unittest.mock import Mock, patch

from fixture import PrivateX, PythonCapture, SURFACE

import bulk_capture as bulk
from capture import FrameInfo

LIBRARY = Path(os.environ.get("VP_CAPTURE_TEST_LIBRARY", SURFACE / "target/release/libvp_capture.so"))
PAYLOAD = b"\x04\x05\x06" * 2


class SyntheticCapture:
    info = FrameInfo(2, 1, 6, 1, len(PAYLOAD))

    def __init__(self, _display, _server_pid, *, library):
        self.closed = False

    def next_frame(self, pixels):
        pixels[:] = PAYLOAD
        return sha256(PAYLOAD).digest()

    def close(self):
        self.closed = True


class CountedApi:
    def __init__(self, api, double_view=False):
        self.api = api
        self.calls = []
        self.double_view = double_view

    def __getattr__(self, name):
        if name == "context":
            return self.api.context

        def record(*args):
            operation = getattr(self.api, name)
            self.calls.append(name)
            if name == "cpu_view" and self.double_view:
                self.calls.append(name)
                operation(*args)
            return operation(*args)
        return record


def capture_options():
    return {"deadline_monotonic_ns": monotonic_ns() + 10_000_000_000, "priority": 0}


class BulkCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pin = sha256(LIBRARY.read_bytes()).hexdigest()
        cls.next_session = count(1000)

    def stage(self, display=93, pid=1):
        session = next(self.next_session)
        return bulk.CaptureStage(display, pid, library=LIBRARY, provider_sha256=self.pin,
                                 session_id=session, lane_id=2000, consumer_id=7,
                                 affinity_group=8, generation=1, epoch=1)

    def test_wrong_pin_refused_before_load_and_open(self):
        with patch.object(bulk.ct, "CDLL", side_effect=AssertionError) as load:
            with self.assertRaises(bulk.BulkError) as error:
                bulk._Provider(LIBRARY, "0" * 64)
        load.assert_not_called()
        self.assertEqual(error.exception.status, bulk._ABI_MISMATCH)

    def test_invalid_bootstrap_table_and_missing_symbol_fail_before_capture_opens(self):
        provider = bulk._load_provider(LIBRARY, self.pin)
        for invalid in ("abi_version", "struct_size", "context", *[n for n, _ in bulk._Api._fields_[3:]]):
            with self.subTest(invalid=invalid):
                def corrupt(_version, *_args):
                    target = ct.cast(_args[-1], ct.POINTER(bulk._Api)).contents
                    ct.memmove(ct.addressof(target), ct.addressof(provider.api), ct.sizeof(target))
                    for field, field_type in bulk._Api._fields_:
                        if field == invalid:
                            setattr(target, field, field_type() if issubclass(field_type, ct._CFuncPtr) else 0)
                    return bulk._OK

                fake = Mock(vp_bulk_get_api_v1=corrupt)
                with (patch.object(bulk, "_PROVIDERS", {}),
                      patch.object(bulk.ct, "CDLL", return_value=fake),
                      patch.object(bulk, "Capture", side_effect=AssertionError) as open_capture):
                    try:
                        with self.assertRaises(bulk.BulkError) as error:
                            self.stage()
                    finally:
                        for cached in bulk._PROVIDERS.values():
                            cached.image.close()
                open_capture.assert_not_called()
                self.assertEqual(error.exception.status, bulk._ABI_MISMATCH)
        with self.assertRaises(bulk.BulkError) as missing:
            bulk._negotiate(object())
        self.assertEqual(missing.exception.status, bulk._ABI_MISMATCH)

    def test_stage_settles_exact_custody_and_returns_only_verified_metadata(self):
        with patch.object(bulk, "Capture", SyntheticCapture), self.stage() as stage:
            result = stage.capture_and_verify(**capture_options())
            self.assertEqual(result, bulk.VerifiedFrame(SyntheticCapture.info, sha256(PAYLOAD).digest()))
            self.assertEqual(set(vars(result)), {"info", "sha256"})
            self.assertEqual(set(vars(result.info)), {"width", "height", "row_stride", "format_id", "byte_len"})
        with self.assertRaises(bulk.BulkError) as closed:
            stage.capture_and_verify(**capture_options())
        self.assertEqual(closed.exception.status, bulk._BROKEN)
        fd_paths = [entry.readlink().as_posix() for entry in Path("/proc/self/fd").iterdir() if not entry.name == "0"
                    and entry.is_symlink() and entry.exists()]
        self.assertFalse(any("vp-staged-rgb24" in path for path in fd_paths))
        self.assertNotIn("vp-staged-rgb24", Path("/proc/self/maps").read_text())

    def test_foreign_process_does_not_dispatch_capture_or_bulk(self):
        with patch.object(bulk, "Capture", SyntheticCapture), self.stage() as stage:
            api = stage._api
            stage._api = Mock()
            with patch.object(bulk.os, "getpid", return_value=os.getpid() + 1):
                with self.assertRaises(bulk.BulkError) as denied:
                    stage.capture_and_verify(**capture_options())
                with self.assertRaises(bulk.BulkError):
                    stage.close()
            self.assertEqual(denied.exception.status, bulk._BROKEN)
            self.assertEqual(stage._api.mock_calls, [])
            stage._api = api

    def test_empty_digest_and_nonempty_null_view_rejected(self):
        self.assertEqual(bulk._digest_view(bulk._View(None, 0), 0), sha256(b"").digest())
        with self.assertRaises(bulk.BulkError):
            bulk._digest_view(bulk._View(None, 1), 1)
        with self.assertRaises(bulk.BulkError):
            bulk._digest_view(bulk._View(1, 0), 1)

    def test_stage_seals_and_releases_on_verification_failure(self):
        with patch.object(bulk, "Capture", SyntheticCapture), self.stage() as stage:
            with patch.object(bulk, "_digest_view", return_value=b"wrong"):
                with self.assertRaises(bulk.BulkError) as error:
                    stage.capture_and_verify(**capture_options())
            self.assertEqual(error.exception.status, bulk._BROKEN)
            self.assertTrue(stage._closed)
            self.assertNotIn("vp-staged-rgb24", Path("/proc/self/maps").read_text())

    def test_per_element_crossing_mutant_fails_boundary_count(self):
        def crossing_oracle(stage):
            stage.capture_and_verify(**capture_options())
            self.assertEqual(stage._api.calls, ["publish_batch", "borrow_batch", "cpu_view", "release"])

        with patch.object(bulk, "Capture", SyntheticCapture):
            for mutant in (False, True):
                with self.stage() as stage:
                    stage._api = CountedApi(stage._api, double_view=mutant)
                    if mutant:
                        with self.assertRaisesRegex(AssertionError, "lists differ|Lists differ"):
                            crossing_oracle(stage)
                    else:
                        crossing_oracle(stage)

    def test_wrong_generation_mutant_is_rejected_before_reading_pixels(self):
        class SubstituteGeneration:
            def __init__(self, api):
                self.api = api

            def __getattr__(self, name):
                return getattr(self.api, name)

            def borrow_batch(self, context, lane, intent, delivery, out, capacity, produced):
                rc = self.api.borrow_batch(context, lane, intent, delivery, out, capacity, produced)
                block = ct.cast(out, ct.POINTER(bulk._Block)).contents
                self.exact = bulk._Lease.from_buffer_copy(block.lease)
                block.lease.generation += 1
                return rc

            def release(self, context, _loan):
                return self.api.release(context, ct.byref(self.exact))

        with patch.object(bulk, "Capture", SyntheticCapture), self.stage() as stage:
            stage._api = SubstituteGeneration(stage._api)
            with patch.object(bulk, "_digest_view", side_effect=AssertionError) as view:
                with self.assertRaises(bulk.BulkError) as error:
                    stage.capture_and_verify(**capture_options())
            self.assertEqual(error.exception.status, bulk._BROKEN)
            view.assert_not_called()
            self.assertTrue(stage._closed)

    def test_precommit_release_failure_retries_exact_lease_after_fence(self):
        class FailOnce:
            def __init__(self, api):
                self.api = api
                self.events = []
                self.leases = []

            def __getattr__(self, name):
                return getattr(self.api, name)

            def release(self, context, loan):
                self.events.append("release")
                self.leases.append(bytes(ct.cast(loan, ct.POINTER(bulk._Lease)).contents))
                return bulk._BROKEN if len(self.leases) == 1 else self.api.release(context, loan)

            def break_and_drain(self, context, session):
                self.events.append("break")
                return self.api.break_and_drain(context, session)

        with patch.object(bulk, "Capture", SyntheticCapture), self.stage() as stage:
            stage._api = FailOnce(stage._api)
            with self.assertRaises(bulk.BulkError) as error:
                stage.capture_and_verify(**capture_options())
            self.assertEqual(error.exception.status, bulk._BROKEN)
            self.assertEqual(stage._api.events, ["release", "break", "release", "break"])
            self.assertEqual(stage._api.leases[0], stage._api.leases[1])
            self.assertIsNone(stage._outstanding)
            self.assertTrue(stage._closed)

    def test_missing_release_mutant_is_detected_by_drain(self):
        class SuppressRelease:
            def __init__(self, api):
                self.api = api

            def __getattr__(self, name):
                return getattr(self.api, name)

            def release(self, _context, loan):
                self.lease = bulk._Lease.from_buffer_copy(ct.cast(loan, ct.POINTER(bulk._Lease)).contents)
                return bulk._OK

        with patch.object(bulk, "Capture", SyntheticCapture), self.stage() as stage:
            api = stage._api
            mutant = SuppressRelease(api)
            stage._api = mutant
            try:
                stage.capture_and_verify(**capture_options())
                with self.assertRaises(bulk.BulkError) as caught:
                    stage.close()
                self.assertEqual(caught.exception.status, 1)
            finally:
                stage._api = api
                self.assertEqual(api.release(api.context, ct.byref(mutant.lease)), bulk._OK)

    @unittest.skipUnless(os.environ.get("VP_CAPTURE_PRIVATE_X") == "1", "private Xvfb fixture required")
    def test_private_x11_parity_and_restart(self):
        with PrivateX() as x11, PythonCapture(x11) as existing:
            for _ in range(2):
                with self.stage(x11.display, x11.pid) as stage:
                    for profile in (0, 1):
                        x11.pattern(profile)
                        actual = stage.capture_and_verify(**capture_options())
                        _, expected = existing.next_frame()
                        self.assertEqual(actual.sha256, expected)
                        self.assertEqual((actual.info.width, actual.info.height), (x11.width, x11.height))


if __name__ == "__main__":
    unittest.main(verbosity=2)
