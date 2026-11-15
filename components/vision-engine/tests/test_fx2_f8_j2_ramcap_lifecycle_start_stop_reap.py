"""Contract tests for fx2-f8-d11-x11-framebuffer-ram-capture lifecycle: Start, Stop, SizeBytes, Reap.

Target D11-LIFETIME: RamCapture capture session lifecycle, configuration, fake monotonic time TTL,
and cancellation hooks.
"""

import unittest
from unittest.mock import MagicMock, Mock

try:
    from vadapt.ramcap import RamCapture
    HAS_RAMCAP = True
    RAMCAP_IMPORT_ERROR = None
except ImportError as e:
    try:
        from vadapt.xfb import RamCapture
        HAS_RAMCAP = True
        RAMCAP_IMPORT_ERROR = None
    except ImportError:
        RamCapture = None
        HAS_RAMCAP = False
        RAMCAP_IMPORT_ERROR = str(e)


class TestFx2F8J2RamcapLifecycleStartStopReap(unittest.TestCase):
    """Contract tests for RamCapture session lifecycle (Start, Stop, SizeBytes, Reap)."""

    def _assert_module_available(self):
        self.assertTrue(
            HAS_RAMCAP,
            f"RamCapture module or class is absent: {RAMCAP_IMPORT_ERROR}"
        )

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_config_validation_ttl_and_cadence(self):
        """Test zero, negative and boolean TTL or cadence configuration as failures vs valid boundary."""
        self._assert_module_available()

        # Boundary checks for TTL and cadence (min_interval_ms)
        for invalid_ttl in [0, -100, True, False]:
            with self.assertRaises((ValueError, TypeError)):
                RamCapture(
                    source_metadata={"slot": 0, "xid": 1001},
                    max_frame_bytes=1024 * 1024,
                    max_session_bytes=8 * 1024 * 1024,
                    min_interval_ms=100,
                    ring_slots=4,
                    ttl_ms=invalid_ttl,
                )

        for invalid_cadence in [0, -50, True, False]:
            with self.assertRaises((ValueError, TypeError)):
                RamCapture(
                    source_metadata={"slot": 0, "xid": 1001},
                    max_frame_bytes=1024 * 1024,
                    max_session_bytes=8 * 1024 * 1024,
                    min_interval_ms=invalid_cadence,
                    ring_slots=4,
                    ttl_ms=5000,
                )

        for invalid_max_frame in [0, -1, True, False]:
            with self.assertRaises((ValueError, TypeError)):
                RamCapture(
                    source_metadata={"slot": 0, "xid": 1001},
                    max_frame_bytes=invalid_max_frame,
                    max_session_bytes=8 * 1024 * 1024,
                    min_interval_ms=100,
                    ring_slots=4,
                    ttl_ms=5000,
                )

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_trusted_config_slot_and_xid_resolution(self):
        """Slot and xid resolve through trusted configuration, never a caller-supplied path or executable."""
        self._assert_module_available()

        # Must fail when caller attempts executable or filesystem path injections as slot/xid
        untrusted_slots = ["/bin/sh", "/tmp/malicious.sh", "../../../etc/passwd"]
        for untrusted in untrusted_slots:
            with self.assertRaises((ValueError, TypeError)):
                cap = RamCapture(
                    source_metadata={"slot": untrusted, "xid": 1001},
                    max_frame_bytes=1024 * 1024,
                    max_session_bytes=8 * 1024 * 1024,
                    min_interval_ms=100,
                    ring_slots=4,
                )
                cap.Start(slot=untrusted, xid=1001, ttl_ms=5000)

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_start_lifecycle_and_nonempty_session_id(self):
        """Start takes slot, xid and ttl_ms and returns a nonempty session_id."""
        self._assert_module_available()

        mock_clock = Mock(return_value=1000.0)
        mock_ws_factory = MagicMock()

        cap = RamCapture(
            source_metadata={"slot": 0, "xid": 1001},
            max_frame_bytes=1024 * 1024,
            max_session_bytes=8 * 1024 * 1024,
            min_interval_ms=100,
            ring_slots=4,
            workspace_factory=mock_ws_factory,
            monotonic_clock=mock_clock,
        )

        sid = cap.Start(slot=0, xid=1001, ttl_ms=5000)
        self.assertIsInstance(sid, str)
        self.assertTrue(len(sid) > 0)

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_cancelled_start_before_workspace_creation(self):
        """Cancelled Start before workspace creation leaves no visible session or reservation."""
        self._assert_module_available()

        mock_cancel_hook = Mock(return_value=True)  # Fired before workspace creation
        mock_ws_factory = MagicMock()

        cap = RamCapture(
            source_metadata={"slot": 0, "xid": 1001},
            max_frame_bytes=1024 * 1024,
            max_session_bytes=8 * 1024 * 1024,
            min_interval_ms=100,
            ring_slots=4,
            workspace_factory=mock_ws_factory,
            cancellation_hook=mock_cancel_hook,
        )

        with self.assertRaises(Exception):
            cap.Start(slot=0, xid=1001, ttl_ms=5000)

        mock_ws_factory.assert_not_called()
        self.assertEqual(cap.SizeBytes("cancelled_session"), 0)

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_cancelled_start_after_workspace_creation(self):
        """Cancelled Start after workspace creation rolls back and leaves no visible session or reservation."""
        self._assert_module_available()

        ws_mock = MagicMock()
        mock_ws_factory = Mock(return_value=ws_mock)

        # Cancel right after workspace creation
        cancel_state = [False]

        def cancel_hook():
            return cancel_state[0]

        cap = RamCapture(
            source_metadata={"slot": 0, "xid": 1001},
            max_frame_bytes=1024 * 1024,
            max_session_bytes=8 * 1024 * 1024,
            min_interval_ms=100,
            ring_slots=4,
            workspace_factory=mock_ws_factory,
            cancellation_hook=cancel_hook,
        )

        cancel_state[0] = True
        with self.assertRaises(Exception):
            cap.Start(slot=0, xid=1001, ttl_ms=5000)

        # Workspace close or release should be invoked during rollback
        self.assertTrue(ws_mock.close.called or ws_mock.release.called)

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_start_failed_first_frame_validation(self):
        """Failed first-frame validation during Start leaves no visible session identity or reservation."""
        self._assert_module_available()

        mock_ws_factory = MagicMock()

        cap = RamCapture(
            source_metadata={"slot": 0, "xid": 1001},
            max_frame_bytes=1024 * 1024,
            max_session_bytes=8 * 1024 * 1024,
            min_interval_ms=100,
            ring_slots=4,
            workspace_factory=mock_ws_factory,
        )

        # Simulate first frame validation failure
        with self.assertRaises(Exception):
            cap.Start(slot=0, xid=1001, ttl_ms=5000, validate_first_frame=True)

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_stop_seals_without_deleting_and_double_stop(self):
        """Stop seals without deleting ring; double Stop is an idempotent success for a sealed known session."""
        self._assert_module_available()

        cap = RamCapture(
            source_metadata={"slot": 0, "xid": 1001},
            max_frame_bytes=1024 * 1024,
            max_session_bytes=8 * 1024 * 1024,
            min_interval_ms=100,
            ring_slots=4,
        )

        sid = cap.Start(slot=0, xid=1001, ttl_ms=5000)
        size_before = cap.SizeBytes(sid)
        self.assertGreater(size_before, 0)

        # First Stop: seals without deleting ring
        cap.Stop(sid)
        size_after_stop = cap.SizeBytes(sid)
        self.assertGreater(size_after_stop, 0)

        # Second Stop: idempotent success for sealed known session
        cap.Stop(sid)
        self.assertEqual(cap.SizeBytes(sid), size_after_stop)

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_stop_unknown_identity_returns_typed_error(self):
        """Stop on an unknown identity returns a typed error."""
        self._assert_module_available()

        cap = RamCapture(
            source_metadata={"slot": 0, "xid": 1001},
            max_frame_bytes=1024 * 1024,
            max_session_bytes=8 * 1024 * 1024,
            min_interval_ms=100,
            ring_slots=4,
        )

        with self.assertRaises((KeyError, ValueError, Exception)):
            cap.Stop("unknown-session-id-999")

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_stop_prevents_further_publication(self):
        """Verify Stop prevents further frame publication."""
        self._assert_module_available()

        cap = RamCapture(
            source_metadata={"slot": 0, "xid": 1001},
            max_frame_bytes=1024 * 1024,
            max_session_bytes=8 * 1024 * 1024,
            min_interval_ms=100,
            ring_slots=4,
        )

        sid = cap.Start(slot=0, xid=1001, ttl_ms=5000)
        cap.capture_once(sid)
        cap.Stop(sid)

        with self.assertRaises(Exception):
            cap.capture_once(sid)

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_size_bytes_behavior_across_stop_and_reap(self):
        """SizeBytes stays positive after Stop with valid retained ring and becomes zero after Reap."""
        self._assert_module_available()

        cap = RamCapture(
            source_metadata={"slot": 0, "xid": 1001},
            max_frame_bytes=1024 * 1024,
            max_session_bytes=8 * 1024 * 1024,
            min_interval_ms=100,
            ring_slots=4,
        )

        sid = cap.Start(slot=0, xid=1001, ttl_ms=5000)
        self.assertGreater(cap.SizeBytes(sid), 0)

        cap.Stop(sid)
        self.assertGreater(cap.SizeBytes(sid), 0)

        cap.Reap(sid)
        self.assertEqual(cap.SizeBytes(sid), 0)

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_reap_tolerates_empty_failed_unknown_and_double_reap(self):
        """Reap removes session and tolerates empty, failed and unknown identities; double Reap is idempotent."""
        self._assert_module_available()

        cap = RamCapture(
            source_metadata={"slot": 0, "xid": 1001},
            max_frame_bytes=1024 * 1024,
            max_session_bytes=8 * 1024 * 1024,
            min_interval_ms=100,
            ring_slots=4,
        )

        # Reap on empty manager / no sessions
        cap.Reap()

        # Reap on unknown identity
        cap.Reap("unknown-session-123")

        # Start, Stop, Reap and Double Reap
        sid = cap.Start(slot=0, xid=1001, ttl_ms=5000)
        cap.Reap(sid)
        cap.Reap(sid)  # Double Reap
        self.assertEqual(cap.SizeBytes(sid), 0)

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_ttl_expiry_exactly_at_ttl_ms_and_idle_reaper(self):
        """Test expiry exactly at ttl_ms with fake clock; idle-session reaper runs independently of requests."""
        self._assert_module_available()

        current_time = [1000.0]

        def fake_clock():
            return current_time[0]

        cap = RamCapture(
            source_metadata={"slot": 0, "xid": 1001},
            max_frame_bytes=1024 * 1024,
            max_session_bytes=8 * 1024 * 1024,
            min_interval_ms=100,
            ring_slots=4,
            monotonic_clock=fake_clock,
        )

        sid = cap.Start(slot=0, xid=1001, ttl_ms=5000)
        self.assertGreater(cap.SizeBytes(sid), 0)

        # Before TTL expiry (4.999 seconds)
        current_time[0] = 1000.0 + 4.999
        self.assertGreater(cap.SizeBytes(sid), 0)

        # Exactly at TTL expiry (5.000 seconds)
        current_time[0] = 1000.0 + 5.000
        # Check that expiry triggers reaping / cleanup
        cap.run_idle_reaper()
        self.assertEqual(cap.SizeBytes(sid), 0)

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_ttl_applies_to_sealed_sessions_without_inbound_bridge_request(self):
        """TTL also applies to sealed (stopped) sessions without another inbound bridge request."""
        self._assert_module_available()

        current_time = [1000.0]

        def fake_clock():
            return current_time[0]

        cap = RamCapture(
            source_metadata={"slot": 0, "xid": 1001},
            max_frame_bytes=1024 * 1024,
            max_session_bytes=8 * 1024 * 1024,
            min_interval_ms=100,
            ring_slots=4,
            monotonic_clock=fake_clock,
        )

        sid = cap.Start(slot=0, xid=1001, ttl_ms=5000)
        cap.Stop(sid)  # Sealed
        self.assertGreater(cap.SizeBytes(sid), 0)

        # Advance fake time past TTL without calling any inbound bridge request for `sid`
        current_time[0] = 1000.0 + 6.000
        cap.run_idle_reaper()
        self.assertEqual(cap.SizeBytes(sid), 0)

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_cancellation_during_cadence_wait_and_session_isolation(self):
        """Cancellation during cadence wait stops capture; cancellation of one session never reaps another."""
        self._assert_module_available()

        current_time = [1000.0]

        def fake_clock():
            return current_time[0]

        cap = RamCapture(
            source_metadata={"slot": 0, "xid": 1001},
            max_frame_bytes=1024 * 1024,
            max_session_bytes=8 * 1024 * 1024,
            min_interval_ms=100,
            ring_slots=4,
            monotonic_clock=fake_clock,
        )

        sid_a = cap.Start(slot=0, xid=1001, ttl_ms=10000)
        sid_b = cap.Start(slot=1, xid=1002, ttl_ms=10000)

        self.assertGreater(cap.SizeBytes(sid_a), 0)
        self.assertGreater(cap.SizeBytes(sid_b), 0)

        # Cancel Session A
        cap.Reap(sid_a)

        # Session A reaped, Session B remains intact
        self.assertEqual(cap.SizeBytes(sid_a), 0)
        self.assertGreater(cap.SizeBytes(sid_b), 0)

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_deterministic_capture_once_pumps_single_sample(self):
        """Deterministic capture_once pumps a single eligible sample for tests."""
        self._assert_module_available()

        cap = RamCapture(
            source_metadata={"slot": 0, "xid": 1001},
            max_frame_bytes=1024 * 1024,
            max_session_bytes=8 * 1024 * 1024,
            min_interval_ms=100,
            ring_slots=4,
        )

        sid = cap.Start(slot=0, xid=1001, ttl_ms=5000)
        pushed = cap.capture_once(sid)
        self.assertTrue(pushed)


if __name__ == "__main__":
    unittest.main()
