"""Contract tests for D11-RAM-PUBLISH and D11-SOURCE-TRUST in capture_once integration.

Task: fx2-f8-j3-ramcap-rtring-publish-and-storage-law
Fix Task ID: fx2-f8-d11-x11-framebuffer-ram-capture
"""
import inspect
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from vadapt import ramws, rtring, xfb

try:
    from vadapt import ramcap
    RamCapture = getattr(ramcap, "RamCapture", None)
    RamCaptureError = getattr(ramcap, "RamCaptureError", Exception)
except ImportError:
    RamCapture = None
    RamCaptureError = Exception


class TestRamCapRtRingPublishAndStorageLaw(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="vqa_test_ramcap_")
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def _check_module(self):
        self.assertIsNotNone(RamCapture, "RamCapture implementation module (vadapt.ramcap) is not available")

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_ramcap_lifecycle_contract(self):
        """RamCapture configured with source metadata, frame/session limits, min_interval_ms, and hooks.
        Start, Stop, SizeBytes, Reap contracts including double-Stop and post-Reap SizeBytes."""
        self._check_module()
        fake_time = 1000.0
        clock = lambda: fake_time

        cap = RamCapture(
            source_meta={"type": "x11", "nocursor": True},
            max_frame_bytes=1024 * 1024,
            max_session_bytes=rtring.ring_bytes(4, 64, 48),
            min_interval_ms=100,
            ring_slots=4,
            clock=clock,
        )

        sid = cap.Start(slot=0, xid=12345, ttl_ms=5000)
        self.assertTrue(isinstance(sid, str) and len(sid) > 0)

        sz = cap.SizeBytes(sid)
        self.assertTrue(isinstance(sz, int) and sz > 0)

        # Stop seals without deleting ring; double stop is idempotent
        cap.Stop(sid)
        self.assertEqual(cap.SizeBytes(sid), sz)
        cap.Stop(sid)  # Idempotent success

        # Unknown Stop returns typed error
        with self.assertRaises(RamCaptureError):
            cap.Stop("non_existent_session_id")

        # Reap removes session and ring; SizeBytes after Reap is 0
        cap.Reap(sid)
        self.assertEqual(cap.SizeBytes(sid), 0)

        # Reap tolerates empty, failed, and unknown identities
        cap.Reap("non_existent_session_id")
        cap.Reap("")

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_source_resolution_trust_boundary(self):
        """Slot and xid resolve through trusted configuration, never caller-supplied executable or filesystem path."""
        self._check_module()
        cap = RamCapture(
            source_meta={"type": "x11", "nocursor": True},
            max_frame_bytes=1024 * 1024,
            max_session_bytes=10 * 1024 * 1024,
            min_interval_ms=50,
            ring_slots=2,
        )

        # Valid boundary: integer slot and xid
        sid = cap.Start(slot=0, xid=100, ttl_ms=1000)
        self.assertTrue(sid)
        cap.Reap(sid)

        # Must-fail counterpart: passing path strings or executable commands as slot/xid
        for bad_slot in ["/bin/sh", "../tmp", "../../etc/passwd", "/dev/shm/eval.sh"]:
            with self.assertRaises((TypeError, ValueError, RamCaptureError)):
                cap.Start(slot=bad_slot, xid=100, ttl_ms=1000)

        for bad_xid in ["/bin/bash -c rm", "../escape", "/tmp/xid"]:
            with self.assertRaises((TypeError, ValueError, RamCaptureError)):
                cap.Start(slot=0, xid=bad_xid, ttl_ms=1000)

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_publish_order_retention_and_reap(self):
        """Canonical xfb RGB is published in order via RingWriter/RingReader over tiny /dev/shm RamWorkspace,
        retained across Stop, and removed only after Reap."""
        self._check_module()
        ws_dir = tempfile.mkdtemp(prefix="vqa_ramws_", dir="/dev/shm")
        self.addCleanup(shutil.rmtree, ws_dir, ignore_errors=True)

        # Injected free-memory probe
        def mock_statvfs(path):
            st = os.statvfs("/")
            # Statvfs proxy returning ample free memory
            return st

        ws = ramws.open_run("test_campaign", "run_01", quota_bytes=2 * 1024 * 1024, root=ws_dir, tool="test_tool", statvfs=mock_statvfs)
        self.addCleanup(ws.close)

        cap = RamCapture(
            source_meta={"type": "x11", "nocursor": True},
            max_frame_bytes=100000,
            max_session_bytes=rtring.ring_bytes(4, 64, 48),
            min_interval_ms=10,
            ring_slots=4,
            workspace=ws,
        )

        sid = cap.Start(slot=0, xid=1, ttl_ms=10000)
        # Pump deterministic captures
        f1 = np.full((48, 64, 3), 10, dtype=np.uint8)
        f2 = np.full((48, 64, 3), 20, dtype=np.uint8)

        cap.capture_once(frame_data=f1)
        cap.capture_once(frame_data=f2)

        # Check retention across Stop
        cap.Stop(sid)
        reader = cap.get_reader(sid)
        self.assertIsNotNone(reader)

        frame1 = reader.poll()
        self.assertIsNotNone(frame1)
        self.assertTrue(np.array_equal(frame1.view, f1))

        frame2 = reader.poll()
        self.assertIsNotNone(frame2)
        self.assertTrue(np.array_equal(frame2.view, f2))

        reader.close()

        # Removal only after Reap
        cap.Reap(sid)
        self.assertEqual(cap.SizeBytes(sid), 0)

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_max_session_bytes_admission_boundary(self):
        """Set max_session_bytes to rtring.ring_bytes for exact admission, then one byte less for refusal with unchanged reservation."""
        self._check_module()
        slots, w, h = 4, 64, 48
        exact_bytes = rtring.ring_bytes(slots, w, h)

        # Exact admission: succeeds
        cap_ok = RamCapture(
            source_meta={"type": "x11", "nocursor": True},
            max_frame_bytes=w * h * 3,
            max_session_bytes=exact_bytes,
            min_interval_ms=10,
            ring_slots=slots,
        )
        sid = cap_ok.Start(slot=0, xid=1, ttl_ms=1000)
        self.assertTrue(sid)
        self.assertGreater(cap_ok.SizeBytes(sid), 0)
        cap_ok.Reap(sid)

        # Must-fail counterpart: 1 byte less refusal
        cap_fail = RamCapture(
            source_meta={"type": "x11", "nocursor": True},
            max_frame_bytes=w * h * 3,
            max_session_bytes=exact_bytes - 1,
            min_interval_ms=10,
            ring_slots=slots,
        )
        with self.assertRaises(RamCaptureError):
            cap_fail.Start(slot=0, xid=1, ttl_ms=1000)

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_cadence_fake_clock_and_no_burst(self):
        """Fake clock proves no read, publication or allocation before min_interval_ms,
        one publication at deadline, and no catch-up burst when time advances far ahead."""
        self._check_module()
        t0 = 1000.0
        current_time = t0
        clock = lambda: current_time

        cap = RamCapture(
            source_meta={"type": "x11", "nocursor": True},
            max_frame_bytes=100000,
            max_session_bytes=rtring.ring_bytes(4, 64, 48),
            min_interval_ms=100,
            ring_slots=4,
            clock=clock,
        )
        sid = cap.Start(slot=0, xid=1, ttl_ms=10000)

        # Before min_interval_ms (e.g. +50 ms): no publication
        current_time = t0 + 0.050
        res = cap.capture_once()
        self.assertFalse(res)

        # At deadline (e.g. +100 ms): exactly one publication
        current_time = t0 + 0.100
        res = cap.capture_once()
        self.assertTrue(res)

        # Jump clock far ahead (+5000 ms): single publication, no catch-up burst
        current_time = t0 + 5.100
        published_count = 0
        for _ in range(10):
            if cap.capture_once():
                published_count += 1

        self.assertEqual(published_count, 1, "Must publish exactly once and avoid catch-up burst")
        cap.Reap(sid)

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_negative_source_fixtures_rejection(self):
        """Reject persistent filesystem, escaped symlink, unknown nocursor, cursor-enabled, xpra/encoded payload, changing FB geometry."""
        self._check_module()

        # 1. Persistent filesystem path source
        with self.assertRaises(RamCaptureError):
            RamCapture(source_meta={"type": "x11", "path": "/var/tmp/frame.png"})

        # 2. Escaped symlink in source
        with self.assertRaises(RamCaptureError):
            RamCapture(source_meta={"type": "x11", "path": "/dev/shm/../../etc/passwd"})

        # 3. Missing/unknown nocursor evidence
        with self.assertRaises(RamCaptureError):
            RamCapture(source_meta={"type": "x11"})

        # 4. Known cursor-enabled source
        with self.assertRaises(RamCaptureError):
            RamCapture(source_meta={"type": "x11", "nocursor": False})

        # 5. Xpra or encoded payload
        with self.assertRaises(RamCaptureError):
            RamCapture(source_meta={"type": "xpra", "format": "h264"})

        # 6. Changing FB identity or geometry during capture
        cap = RamCapture(
            source_meta={"type": "x11", "nocursor": True},
            max_frame_bytes=100000,
            max_session_bytes=rtring.ring_bytes(4, 64, 48),
            min_interval_ms=10,
            ring_slots=4,
        )
        sid = cap.Start(slot=0, xid=1, ttl_ms=1000)

        # Inject frame with modified geometry (e.g. 100x100 instead of 64x48)
        bad_geo_frame = np.zeros((100, 100, 3), dtype=np.uint8)
        with self.assertRaises(RamCaptureError):
            cap.capture_once(frame_data=bad_geo_frame)

        cap.Reap(sid)

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_tearing_and_stability_no_head_advancement(self):
        """Inject bounded sequence with no consecutive identical snapshots; assert no ring head advancement."""
        self._check_module()
        cap = RamCapture(
            source_meta={"type": "x11", "nocursor": True},
            max_frame_bytes=100000,
            max_session_bytes=rtring.ring_bytes(4, 64, 48),
            min_interval_ms=10,
            ring_slots=4,
        )
        sid = cap.Start(slot=0, xid=1, ttl_ms=1000)

        # Bounded sequence of alternating/changing non-identical reads (torn)
        torn_sequence = [
            np.full((48, 64, 3), i, dtype=np.uint8) for i in range(10)
        ]

        # capture_once using torn_sequence should fail consistent_read and NOT advance ring head
        res = cap.capture_once(raw_reads=torn_sequence)
        self.assertFalse(res, "Capture should fail under non-consecutive torn reads")

        reader = cap.get_reader(sid)
        self.assertEqual(reader.head(), -1, "Ring head must not advance when reads are torn")
        reader.close()

        # Valid counterpart: two consecutive identical snapshots
        stable_sequence = [
            np.full((48, 64, 3), 1, dtype=np.uint8),
            np.full((48, 64, 3), 2, dtype=np.uint8),
            np.full((48, 64, 3), 2, dtype=np.uint8),  # Consecutive identical pair
        ]
        res_ok = cap.capture_once(raw_reads=stable_sequence)
        self.assertTrue(res_ok, "Capture should succeed on consecutive identical reads")

        reader = cap.get_reader(sid)
        self.assertEqual(reader.head(), 0, "Ring head should advance by 1")
        reader.close()

        cap.Reap(sid)

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_audit_filesystem_writes_tmpfs_root_only(self):
        """Audit attempted filesystem writes with spies that reject frame destination outside verified tmpfs root.
        Demonstrate audit rejects synthetic attempted persistent write without creating it."""
        self._check_module()

        tmpfs_root = "/dev/shm"

        def write_audit_spy(path, *args, **kwargs):
            real_p = os.path.realpath(str(path))
            if not real_p.startswith(tmpfs_root + os.sep) and real_p != tmpfs_root:
                raise PermissionError("E_RAM_STORAGE_LAW: Attempted frame write outside verified tmpfs root: %s" % real_p)

        # Demonstrate spy rejects synthetic persistent write outside tmpfs root
        with self.assertRaises(PermissionError) as ctx:
            write_audit_spy("/tmp/persistent_frame.raw")
        self.assertIn("outside verified tmpfs root", str(ctx.exception))
        self.assertFalse(os.path.exists("/tmp/persistent_frame.raw"))

        # Valid boundary inside tmpfs root passes audit
        try:
            write_audit_spy("/dev/shm/vqa_ramws/valid_frame.raw")
        except PermissionError:
            self.fail("Write spy should allow paths inside tmpfs root")

        # RamCapture integrated write spy test
        cap = RamCapture(
            source_meta={"type": "x11", "nocursor": True},
            max_frame_bytes=100000,
            max_session_bytes=rtring.ring_bytes(4, 64, 48),
            min_interval_ms=10,
            ring_slots=4,
            write_audit_spy=write_audit_spy,
        )

        sid = cap.Start(slot=0, xid=1, ttl_ms=1000)
        # Attempting capture with destination outside tmpfs root must fail
        with patch.object(cap, "_get_frame_dest", return_value="/var/tmp/frame.raw", create=True):
            with self.assertRaises(PermissionError):
                cap.capture_once()

        cap.Reap(sid)

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_failure_cleanup_and_reader_view_drop(self):
        """Test cleanup after failed publication and drop reader views before Reap."""
        self._check_module()
        cap = RamCapture(
            source_meta={"type": "x11", "nocursor": True},
            max_frame_bytes=100000,
            max_session_bytes=rtring.ring_bytes(4, 64, 48),
            min_interval_ms=10,
            ring_slots=4,
        )
        sid = cap.Start(slot=0, xid=1, ttl_ms=1000)

        # Acquire reader view
        f = np.full((48, 64, 3), 42, dtype=np.uint8)
        cap.capture_once(frame_data=f)

        reader = cap.get_reader(sid)
        frame = reader.poll()
        self.assertIsNotNone(frame)
        view = frame.view

        # Attempting Reap while active view exists must safely drop/unmap view or close cleanly
        reader.close()
        del view
        del frame

        cap.Reap(sid)
        self.assertEqual(cap.SizeBytes(sid), 0)


if __name__ == "__main__":
    unittest.main()
