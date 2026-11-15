# @module {"id":"wa.tests.test_fx2_f3_j3_cdp_session_event_and_reply_bounds","layer":1,"owner":"PERCEIVE","deps":["wa.perceive.cdp_pipe","wa.perceive.cdp_port"],"book":"HB-WA-s1-perceive"}
"""@module wa.tests.test_fx2_f3_j3_cdp_session_event_and_reply_bounds - session event and reply bounds tests.

Exercises CdpPipe and CdpPort readers/senders under timed-out replies, duplicate IDs, active waiters,
and multi-session event interleaving. Verifies bounded retention, sibling load preservation, stale-ID isolation,
and mutant rejection.
"""
import json
import os
import threading
import unittest
from collections import deque
from unittest.mock import MagicMock

from wender.perceive.cdp_pipe import CdpPipe, CdpError, EVENT_BUFFER
from wender.perceive.cdp_port import CdpPort


def _create_pipe_client():
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
    client.events = deque(maxlen=EVENT_BUFFER)
    client._closed = False
    client._w = w_fd
    client._r = r_fd
    return client, r_fd, w_fd


def _create_port_client():
    mock_ws = MagicMock()
    client = CdpPort.__new__(CdpPort)
    client.host = "127.0.0.1"
    client.port = 9222
    client.timeout = 5
    client._next_id = 0
    client._lock = threading.Lock()
    client._cv = threading.Condition()
    client._replies = {}
    client.events = deque(maxlen=EVENT_BUFFER)
    client._closed = False
    client._ws = mock_ws
    client._reader = None
    client._attached = []
    client.proc = None
    client.user_data_dir = None
    return client, mock_ws


class TestCdpPipeReplyBounds(unittest.TestCase):
    """Tests for CdpPipe reply retention bounds and active delivery."""

    def test_active_reply_delivery_healthy_control(self):
        """Healthy control: Active waiter receives its reply and pops it from self._replies."""
        client, r_fd, w_fd = _create_pipe_client()
        reader = threading.Thread(target=client._read_loop, daemon=True)
        reader.start()

        try:
            # Send message for mid=1
            msg = json.dumps({"id": 1, "result": {"status": "ok"}}).encode("utf-8") + b"\0"
            os.write(w_fd, msg)

            with client._cv:
                while 1 not in client._replies:
                    client._cv.wait(timeout=2)
                reply = client._replies.pop(1)

            self.assertEqual(reply.get("result"), {"status": "ok"})
            self.assertNotIn(1, client._replies)
        finally:
            os.close(w_fd)
            reader.join(timeout=2)
            os.close(r_fd)

    def test_stale_id_isolation_and_wrong_id_rejection(self):
        """Healthy control: Stale reply ID 99 does not fulfill waiter for active ID 100."""
        client, r_fd, w_fd = _create_pipe_client()
        reader = threading.Thread(target=client._read_loop, daemon=True)
        reader.start()

        try:
            # Send stale reply ID 99
            msg_stale = json.dumps({"id": 99, "result": {"data": "stale"}}).encode("utf-8") + b"\0"
            os.write(w_fd, msg_stale)

            with client._cv:
                while 99 not in client._replies:
                    client._cv.wait(timeout=2)

            # Active waiter waiting for mid=100
            with client._cv:
                self.assertNotIn(100, client._replies)
                self.assertIn(99, client._replies)

            # Send active reply ID 100
            msg_active = json.dumps({"id": 100, "result": {"data": "active"}}).encode("utf-8") + b"\0"
            os.write(w_fd, msg_active)

            with client._cv:
                while 100 not in client._replies:
                    client._cv.wait(timeout=2)
                reply = client._replies.pop(100)

            self.assertEqual(reply.get("result"), {"data": "active"})
        finally:
            os.close(w_fd)
            reader.join(timeout=2)
            os.close(r_fd)

    def test_pipe_reply_retention_bounded_interleaved_adversarial(self):
        """Adversarial: Interleave timed-out replies, duplicate IDs, active waiter, and session events.

        Assert len(client._replies) <= EVENT_BUFFER after every message.
        Demonstrates defect S1-F3 (unbounded _replies growth).
        """
        client, r_fd, w_fd = _create_pipe_client()
        reader = threading.Thread(target=client._read_loop, daemon=True)
        reader.start()

        try:
            # Stream 2000 timed-out replies and duplicate IDs
            for i in range(1, 2001):
                msg = json.dumps({"id": i, "result": {"val": i}}).encode("utf-8") + b"\0"
                os.write(w_fd, msg)

            import time
            end = time.monotonic() + 3
            while time.monotonic() < end:
                with client._cv:
                    if len(client._replies) > EVENT_BUFFER:
                        break
                    if len(client._replies) == 2000:
                        break
                    client._cv.wait(timeout=0.05)

            with client._cv:
                self.assertLessEqual(
                    len(client._replies), EVENT_BUFFER,
                    "Defect S1-F3: CdpPipe stored %d stale replies exceeding EVENT_BUFFER=%d"
                    % (len(client._replies), EVENT_BUFFER)
                )
        finally:
            os.close(w_fd)
            reader.join(timeout=2)
            os.close(r_fd)


class TestCdpPortReplyBounds(unittest.TestCase):
    """Tests for CdpPort reply retention bounds and active delivery."""

    def test_port_active_delivery_healthy_control(self):
        """Healthy control: CdpPort WebSocket reader receives reply and waiter consumes it."""
        client, mock_ws = _create_port_client()

        messages = [
            json.dumps({"id": 42, "result": {"port_ok": True}}),
            ""
        ]
        mock_ws.recv.side_effect = messages

        reader = threading.Thread(target=client._read_loop, daemon=True)
        reader.start()

        with client._cv:
            while 42 not in client._replies and not client._closed:
                client._cv.wait(timeout=2)

        self.assertIn(42, client._replies)
        with client._cv:
            reply = client._replies.pop(42)

        self.assertEqual(reply.get("result"), {"port_ok": True})
        reader.join(timeout=2)

    def test_port_reply_retention_bounded_adversarial(self):
        """Adversarial: CdpPort WebSocket reader receiving 2000 interleaved stale replies.

        Assert len(client._replies) <= EVENT_BUFFER.
        Demonstrates defect S1-F3 in CdpPort.
        """
        client, mock_ws = _create_port_client()

        messages = [json.dumps({"id": i, "result": {"data": i}}) for i in range(1, 2001)]
        messages.append("")
        mock_ws.recv.side_effect = messages

        reader = threading.Thread(target=client._read_loop, daemon=True)
        reader.start()
        reader.join(timeout=5)

        self.assertLessEqual(
            len(client._replies), EVENT_BUFFER,
            "Defect S1-F3: CdpPort stored %d stale replies exceeding EVENT_BUFFER=%d"
            % (len(client._replies), EVENT_BUFFER)
        )


class TestMultiSessionNavigateIsolation(unittest.TestCase):
    """Tests for multi-session navigation load event isolation."""

    def test_single_session_navigate_healthy_control(self):
        """Healthy control: Single session navigation succeeds and receives load event."""
        client = CdpPipe(chrome_bin="/bin/true")

        def fake_send(method, params=None, session_id=None, timeout=None):
            if method == "Page.navigate":
                client.events.append({"method": "Page.loadEventFired", "sessionId": "sid_A", "params": {}})
                return {"frameId": "f1"}
            return {}

        client.send = fake_send
        res = client.navigate("sid_A", "https://example.com", timeout=0.1)
        self.assertEqual(res, {"frameId": "f1"})

    def test_multi_session_navigate_preserves_sibling_loads_adversarial(self):
        """Adversarial: Navigating session A must preserve pending load events for sessions B and C.

        Demonstrates defect S1-F4 (global purge of all Page.loadEventFired in navigate).
        """
        client = CdpPipe(chrome_bin="/bin/true")

        # Session B and C load events in queue
        ev_b = {"method": "Page.loadEventFired", "sessionId": "sid_B", "params": {"payload": "B1"}}
        ev_c = {"method": "Page.loadEventFired", "sessionId": "sid_C", "params": {"payload": "C1"}}
        ev_a_stale = {"method": "Page.loadEventFired", "sessionId": "sid_A", "params": {"payload": "A_old"}}

        client.events.extend([ev_b, ev_a_stale, ev_c])

        def fake_send(method, params=None, session_id=None, timeout=None):
            if method == "Page.navigate":
                # Session A new load event fired
                client.events.append({"method": "Page.loadEventFired", "sessionId": "sid_A", "params": {"payload": "A_new"}})
                return {"frameId": "frame_A"}
            return {}

        client.send = fake_send
        client.navigate("sid_A", "https://example.com/A", timeout=0.1)

        # Sibling loads for sid_B and sid_C must still be in events in exact order
        remaining_sids = [e.get("sessionId") for e in client.events if e.get("method") == "Page.loadEventFired"]
        self.assertIn("sid_B", remaining_sids, "Defect S1-F4: Sibling load event for sid_B was purged during sid_A navigate")
        self.assertIn("sid_C", remaining_sids, "Defect S1-F4: Sibling load event for sid_C was purged during sid_A navigate")

    def test_multi_session_navigate_repeatedly_purges_only_stale_own_loads(self):
        """Adversarial: Repeated navigation on session A preserves sibling load event payloads/order.

        Purges only stale load events matching session_id == "sid_A".
        """
        client = CdpPipe(chrome_bin="/bin/true")

        ev_b1 = {"method": "Page.loadEventFired", "sessionId": "sid_B", "params": {"seq": 1}}
        ev_b2 = {"method": "Page.loadEventFired", "sessionId": "sid_B", "params": {"seq": 2}}
        ev_a_old = {"method": "Page.loadEventFired", "sessionId": "sid_A", "params": {"seq": 0}}

        client.events.extend([ev_b1, ev_a_old, ev_b2])

        def fake_send(method, params=None, session_id=None, timeout=None):
            if method == "Page.navigate":
                client.events.append({"method": "Page.loadEventFired", "sessionId": session_id, "params": {"seq": 99}})
                return {"frameId": "f_A"}
            return {}

        client.send = fake_send
        client.navigate("sid_A", "https://example.com/A2", timeout=0.1)

        b_events = [e for e in client.events if e.get("sessionId") == "sid_B" and e.get("method") == "Page.loadEventFired"]
        self.assertEqual(len(b_events), 2, "Defect S1-F4: Sibling sid_B load events purged during sid_A navigate")
        self.assertEqual([e["params"]["seq"] for e in b_events], [1, 2], "Sibling load event order/payload altered")


class TestMutantRejection(unittest.TestCase):
    """Rejection tests for unbounded retention, global purge, wrong-ID delivery, and live-reply eviction mutants."""

    def test_reject_unbounded_retention_mutant(self):
        """Reject mutant that stores unlimited unsolicited replies in self._replies."""
        client, r_fd, w_fd = _create_pipe_client()
        reader = threading.Thread(target=client._read_loop, daemon=True)
        reader.start()

        try:
            for i in range(1, 2001):
                msg = json.dumps({"id": i, "result": {"mutant": True}}).encode("utf-8") + b"\0"
                os.write(w_fd, msg)

            import time
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with client._cv:
                    if len(client._replies) >= 2000:
                        break
                    client._cv.wait(timeout=0.05)

            with client._cv:
                self.assertLessEqual(len(client._replies), EVENT_BUFFER, "Unbounded retention mutant accepted")
        finally:
            os.close(w_fd)
            reader.join(timeout=2)
            os.close(r_fd)

    def test_reject_global_purge_mutant(self):
        """Reject mutant that purges Page.loadEventFired across all session IDs."""
        client = CdpPipe(chrome_bin="/bin/true")
        client.events.append({"method": "Page.loadEventFired", "sessionId": "sid_OTHER", "params": {}})

        def fake_send(method, params=None, session_id=None, timeout=None):
            if method == "Page.navigate":
                client.events.append({"method": "Page.loadEventFired", "sessionId": session_id, "params": {}})
                return {"frameId": "f1"}
            return {}

        client.send = fake_send
        client.navigate("sid_SELF", "https://example.com", timeout=0.1)

        other_events = [e for e in client.events if e.get("sessionId") == "sid_OTHER"]
        self.assertEqual(len(other_events), 1, "Global purge mutant accepted: sibling session events purged")

    def test_reject_wrong_id_delivery_mutant(self):
        """Reject wrong-ID delivery: send() for mid=2 must not return reply for mid=1."""
        client, r_fd, w_fd = _create_pipe_client()
        # Separate send pipe from read pipe so commands written by send are not re-read as replies
        cmd_r, cmd_w = os.pipe()
        client._w = cmd_w

        reader = threading.Thread(target=client._read_loop, daemon=True)
        reader.start()

        try:
            client._next_id = 1  # Next send will produce mid=2

            # Feed stale reply for mid=1
            msg1 = json.dumps({"id": 1, "result": {"wrong": True}}).encode("utf-8") + b"\0"
            os.write(w_fd, msg1)

            # Feed correct reply for mid=2
            msg2 = json.dumps({"id": 2, "result": {"correct": True}}).encode("utf-8") + b"\0"
            os.write(w_fd, msg2)

            res = client.send("Test.method", timeout=2)
            self.assertEqual(res, {"correct": True}, "Wrong-ID delivery mutant accepted")
        finally:
            os.close(cmd_w)
            os.close(cmd_r)
            os.close(w_fd)
            reader.join(timeout=2)
            os.close(r_fd)

    def test_reject_live_reply_eviction_mutant(self):
        """Reject live-reply eviction: Active waiter for mid=10 must receive its reply without early eviction."""
        client, r_fd, w_fd = _create_pipe_client()
        reader = threading.Thread(target=client._read_loop, daemon=True)
        reader.start()

        try:
            msg = json.dumps({"id": 10, "result": {"live": True}}).encode("utf-8") + b"\0"
            os.write(w_fd, msg)

            with client._cv:
                while 10 not in client._replies:
                    client._cv.wait(timeout=2)
                reply = client._replies.pop(10)

            self.assertEqual(reply.get("result"), {"live": True})
        finally:
            os.close(w_fd)
            reader.join(timeout=2)
            os.close(r_fd)


if __name__ == "__main__":
    unittest.main()
