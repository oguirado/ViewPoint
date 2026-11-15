"""Adversarial claim-pinning tests for CDP reply retention limits.

Claim: In CdpPipe (wender/perceive/cdp_pipe.py:89,131,191) and CdpPort (wender/perceive/cdp_port.py:31,61,90),
late replies arriving after a send() timeout or unsolicited reply id messages must not grow self._replies without bound.
Known defect: Defect S1-F3 stores every "id" message in self._replies indefinitely.
"""
import json
import os
import threading
import unittest
from unittest.mock import MagicMock

from wender.perceive.cdp_pipe import CdpPipe, EVENT_BUFFER


class TestReplyRetentionHealthyControls(unittest.TestCase):
    """Healthy control tests that must pass without expectedFailure."""

    def test_normal_reply_consumed_and_cleared(self):
        """Verify that normal send-reply lifecycle pops the reply from self._replies."""
        r_fd, w_fd = os.pipe()
        client = CdpPipe.__new__(CdpPipe)
        client.chrome_bin = "fake"
        client.headless = True
        client.timeout = 5
        client.proc = None
        client.user_data_dir = None
        client._next_id = 0
        client._lock = threading.Lock()
        client._cv = threading.Condition()
        client._replies = {}
        client.events = []
        client._closed = False
        client._w = w_fd
        client._r = r_fd

        # Simulate reader receiving reply for ID 1
        reader_thread = threading.Thread(target=client._read_loop, daemon=True)
        reader_thread.start()

        msg = json.dumps({"id": 1, "result": {"ok": True}}).encode("utf-8") + b"\0"
        os.write(w_fd, msg)

        # Wait until reply arrives in _replies
        with client._cv:
            while 1 not in client._replies:
                client._cv.wait(timeout=2)

        self.assertIn(1, client._replies)

        # Simulate send popping the reply
        with client._cv:
            reply = client._replies.pop(1)

        self.assertEqual(reply.get("result"), {"ok": True})
        self.assertNotIn(1, client._replies)

        os.close(w_fd)
        reader_thread.join(timeout=2)
        os.close(r_fd)

    def test_event_buffer_bounded_for_unsolicited_events(self):
        """Verify that events without 'id' are bounded by EVENT_BUFFER = 1024."""
        r_fd, w_fd = os.pipe()
        client = CdpPipe.__new__(CdpPipe)
        client.chrome_bin = "fake"
        client.headless = True
        client.timeout = 5
        client.proc = None
        client.user_data_dir = None
        client._next_id = 0
        client._lock = threading.Lock()
        client._cv = threading.Condition()
        client._replies = {}
        from collections import deque
        client.events = deque(maxlen=EVENT_BUFFER)
        client._closed = False
        client._w = w_fd
        client._r = r_fd

        reader_thread = threading.Thread(target=client._read_loop, daemon=True)
        reader_thread.start()

        # Send 2000 event messages (no "id")
        for i in range(2000):
            msg = json.dumps({"method": "Page.loadEventFired", "params": {"i": i}}).encode("utf-8") + b"\0"
            os.write(w_fd, msg)

        # Give reader thread time to process all
        with client._cv:
            while len(client.events) < EVENT_BUFFER:
                client._cv.wait(timeout=2)

        os.close(w_fd)
        reader_thread.join(timeout=2)
        os.close(r_fd)

        self.assertLessEqual(len(client.events), EVENT_BUFFER)


class TestReplyRetentionAdversarial(unittest.TestCase):
    """Adversarial claim-pinning tests asserting bounded reply storage under defect S1-F3."""

    def test_cdp_pipe_unsolicited_reply_retention_bounded(self):
        """Defect S1-F3: CdpPipe bounds unsolicited/timed-out reply storage in self._replies.

        Expected behavior: len(client._replies) is bounded (at most EVENT_BUFFER = 1024),
        never retaining all 2,000 stale replies.
        """
        r_fd, w_fd = os.pipe()
        client = CdpPipe.__new__(CdpPipe)
        client.chrome_bin = "fake"
        client.headless = True
        client.timeout = 5
        client.proc = None
        client.user_data_dir = None
        client._next_id = 0
        client._lock = threading.Lock()
        client._cv = threading.Condition()
        client._replies = {}
        from collections import deque
        client.events = deque(maxlen=EVENT_BUFFER)
        client._closed = False
        client._w = w_fd
        client._r = r_fd

        reader_thread = threading.Thread(target=client._read_loop, daemon=True)
        reader_thread.start()

        # Simulate 2,000 timed-out or unsolicited reply messages "id": i, "result": ...
        for i in range(1, 2001):
            msg = json.dumps({"id": i, "result": {"data": i}}).encode("utf-8") + b"\0"
            os.write(w_fd, msg)

        # Wait until the 2,000th message is processed by the reader thread
        import time
        deadline = time.monotonic() + 5
        with client._cv:
            while 2000 not in client._replies and time.monotonic() < deadline:
                client._cv.wait(timeout=0.1)

        os.close(w_fd)
        reader_thread.join(timeout=2)
        os.close(r_fd)

        # Claim assertion: self._replies must be bounded (<= EVENT_BUFFER)
        self.assertLessEqual(len(client._replies), EVENT_BUFFER,
                             "Defect S1-F3: CdpPipe retained %d stale replies without bound" % len(client._replies))

    def test_cdp_port_unsolicited_reply_retention_bounded(self):
        """Defect S1-F3: CdpPort bounds unsolicited/timed-out reply storage in self._replies.

        Expected behavior: len(client._replies) is bounded (at most EVENT_BUFFER = 1024),
        never retaining all 2,000 stale replies.
        """
        from wender.perceive.cdp_port import CdpPort

        mock_ws = MagicMock()
        client = CdpPort.__new__(CdpPort)
        client.host = "127.0.0.1"
        client.port = 9222
        client.timeout = 5
        client._next_id = 0
        client._lock = threading.Lock()
        client._cv = threading.Condition()
        client._replies = {}
        from collections import deque
        client.events = deque(maxlen=EVENT_BUFFER)
        client._closed = False
        client._ws = mock_ws
        client._reader = None
        client._attached = []
        client.proc = None
        client.user_data_dir = None

        # Prepare mock ws.recv() to stream 2,000 reply messages then return None / raise
        messages = [json.dumps({"id": i, "result": {"data": i}}) for i in range(1, 2001)]
        messages.append("")  # string return causes recv loop to break

        mock_ws.recv.side_effect = messages

        # Run _read_loop directly in worker thread
        reader_thread = threading.Thread(target=client._read_loop, daemon=True)
        reader_thread.start()
        reader_thread.join(timeout=5)

        # Claim assertion: self._replies must be bounded (<= EVENT_BUFFER)
        self.assertLessEqual(len(client._replies), EVENT_BUFFER,
                             "Defect S1-F3: CdpPort retained %d stale replies without bound" % len(client._replies))


if __name__ == "__main__":
    unittest.main()
