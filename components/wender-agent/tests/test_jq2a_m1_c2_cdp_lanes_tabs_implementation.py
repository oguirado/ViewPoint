"""Offline production-path acceptance tests for JQ2-A M1 C2 (no browser/network)."""
import json
import socket
import subprocess
import threading
import unittest
import urllib.request
from unittest.mock import patch

from wender.perceive.cdp_pipe import CdpError, CdpPipe, WriterFence, EVENT_BUFFER
from wender.perceive.cdp_port import CdpPort, CdpTabError
from wender.perceive.session_registry import Handle, SessionRegistry, StaleHandleError, QuarantinedTargetError
from wender.perceive.tab_manager import TabManager


class FakeClock:
    def __init__(self):
        self.value = 1_000_000_000

    def __call__(self):
        return self.value

    def advance(self, nanoseconds):
        self.value += nanoseconds


class FakeCDP:
    """Synthetic CDP responses; there is no socket, process or file backend."""

    def __init__(self, targets=(), *, duplicate_session=False):
        self.targets = list(targets)
        self.calls = []
        self.next_target = 0
        self.next_session = 0
        self.duplicate_session = duplicate_session

    def send(self, method, params=None, session_id=None, timeout=None):
        params = params or {}
        self.calls.append((method, dict(params), session_id))
        if method == "Target.getTargets":
            return {"targetInfos": list(self.targets)}
        if method == "Target.createTarget":
            self.next_target += 1
            tid = "created-%d" % self.next_target
            self.targets.append({"targetId": tid, "type": "page", "url": params["url"]})
            return {"targetId": tid}
        if method == "Target.attachToTarget":
            self.next_session += 1
            return {"sessionId": "shared" if self.duplicate_session else "sid-%d" % self.next_session}
        if method == "Page.enable":
            return {}
        if method in ("Target.detachFromTarget", "Target.closeTarget"):
            return {"success": True}
        if method == "Page.navigate":
            return {"frameId": "fake-frame"}
        raise AssertionError("unhandled fake CDP method: " + method)

    def navigate(self, session_id, url, timeout=None):
        return self.send("Page.navigate", {"url": url}, session_id=session_id, timeout=timeout)

    def count(self, method):
        return sum(1 for call in self.calls if call[0] == method)


class OfflineOnly(unittest.TestCase):
    def setUp(self):
        # These are forbidden even if an implementation accidentally attempts
        # to launch a browser, open a socket, access a URL or write a disk file.
        for target in ("socket.socket", "subprocess.Popen", "urllib.request.urlopen",
                       "builtins.open"):
            p = patch(target, side_effect=AssertionError("forbidden offline I/O: " + target))
            p.start()
            self.addCleanup(p.stop)


class TabManagerProductionTests(OfflineOnly):
    def _manager(self, targets=(), selected_fence=None):
        fake = FakeCDP(targets)
        clock = FakeClock()
        registry = SessionRegistry(clock=clock)
        return fake, registry, TabManager(fake, registry, clock=clock, writer_fence=selected_fence)

    def test_mutant_first_prefix_match_rejected_in_both_orders(self):
        targets = [
            {"targetId": "a", "type": "page", "url": "https://example.com/app/a"},
            {"targetId": "b", "type": "page", "url": "https://example.com/app/b"},
        ]
        for rows in (targets, list(reversed(targets))):
            cdp, _, manager = self._manager(rows)
            with self.assertRaisesRegex(CdpTabError, "AMBIGUOUS_TARGET"):
                manager.ensure_tab("https://example.com/app", "https://example.com/app/a")
            self.assertEqual(cdp.count("Target.getTargets"), 1)
            self.assertEqual(cdp.count("Target.attachToTarget"), 0)
            self.assertEqual(cdp.count("Target.createTarget"), 0)

    def test_positive_selected_target_binds_canonical_handle_and_epoch(self):
        fake, registry, manager = self._manager([
            {"targetId": "a", "type": "page", "url": "https://example.com/app/a"},
            {"targetId": "b", "type": "page", "url": "https://EXAMPLE.COM:443/app/b"},
        ])
        handle = manager.ensure_tab("https://example.com/app",
                                    "https://example.com/app/final", selected_target="b")
        self.assertIsInstance(handle, Handle)
        self.assertEqual((handle.kind, handle.canonical_id), ("cdp_tab", "b"))
        record = registry.resolve(handle)
        self.assertEqual(record.epoch, 1)
        self.assertFalse(record.owned)
        self.assertTrue(record.attachment_owned)
        self.assertTrue(record.session_id)
        self.assertEqual(fake.count("Target.createTarget"), 0)
        self.assertEqual(fake.count("Target.attachToTarget"), 1)
        self.assertEqual(fake.count("Page.navigate"), 1)
        self.assertLess(
            next(i for i, c in enumerate(fake.calls) if c[0] == "Page.enable"),
            next(i for i, c in enumerate(fake.calls) if c[0] == "Page.navigate"),
        )

    def test_miss_creates_once_and_close_is_idempotent(self):
        fake, registry, manager = self._manager()
        h = manager.ensure_tab("https://example.com/", "https://example.com/new")
        self.assertEqual(fake.count("Target.createTarget"), 1)
        self.assertEqual(fake.count("Target.attachToTarget"), 1)
        self.assertEqual(registry.resolve(h).epoch, 1)
        receipt = manager.close(h)
        self.assertEqual(receipt.status, "closed")
        self.assertEqual(receipt.handle, h)
        self.assertEqual(fake.count("Target.detachFromTarget"), 1)
        self.assertEqual(fake.count("Target.closeTarget"), 1)
        self.assertEqual(manager.close(h).status, "already_closed")
        self.assertEqual(fake.count("Target.closeTarget"), 1)

    def test_reused_caller_target_never_closed(self):
        fake, registry, manager = self._manager([
            {"targetId": "caller", "type": "page", "url": "https://example.com/app"}
        ])
        h = manager.ensure_tab("https://example.com/", "https://example.com/app")
        self.assertEqual(manager.close(h).status, "closed")
        self.assertEqual(fake.count("Target.detachFromTarget"), 1)
        self.assertEqual(fake.count("Target.closeTarget"), 0)
        with self.assertRaises(StaleHandleError):
            registry.resolve(h)
        next_h = manager.ensure_tab("https://example.com/", "https://example.com/app")
        self.assertGreater(next_h.generation, h.generation)

    def test_normalized_origin_invalid_hints_and_deceptive_host(self):
        fake, _, manager = self._manager([
            {"targetId": "bad", "type": "page", "url": "https://example.com.evil.invalid/a"},
            {"targetId": "worker", "type": "service_worker", "url": "https://example.com/a"},
            {"targetId": "good", "type": "page", "url": "https://EXAMPLE.COM:443/a"},
        ])
        for route in ("https://elsewhere.invalid/a", "https://user:pass@example.com/a"):
            with self.assertRaises(CdpTabError):
                manager.ensure_tab("https://example.com/", route)
        self.assertEqual(fake.calls, [])
        with self.assertRaises(CdpTabError):
            manager.ensure_tab("https://example.com/", "https://example.com/a",
                               selected_target="bad")
        self.assertEqual(fake.count("Target.attachToTarget"), 0)
        h = manager.ensure_tab("https://example.com/", "https://example.com/a")
        self.assertEqual(h.canonical_id, "good")
        self.assertEqual(fake.count("Target.createTarget"), 0)

    def test_stop_defers_cleanup_retains_quarantine(self):
        fake, registry, manager = self._manager([
            {"targetId": "caller", "type": "page", "url": "https://example.com/a"}
        ])
        h = manager.ensure_tab("https://example.com/", "https://example.com/a")
        before = len(fake.calls)
        result = manager.stop(h)
        self.assertEqual(result.status, "stopped")
        self.assertEqual(len(fake.calls), before)
        self.assertTrue(registry.tombstone(h)[2])
        self.assertEqual(manager.close(h).status, "already_closed")
        self.assertEqual(len(fake.calls), before)
        with self.assertRaises(QuarantinedTargetError):
            manager.ensure_tab("https://example.com/", "https://example.com/a")
        self.assertEqual(fake.count("Target.detachFromTarget"), 0)

    def test_writer_fence_rejects_new_commands(self):
        fence = WriterFence()
        fake, registry, manager = self._manager(selected_fence=fence)
        fence.stop()
        with self.assertRaisesRegex(CdpError, "STOP"):
            manager.ensure_tab("https://example.com/", "https://example.com/a")
        self.assertEqual(fake.calls, [])


class PipeProductionTests(OfflineOnly):
    def _pipe(self, clock=None):
        return CdpPipe(chrome_bin="/bin/true", timeout=1, clock=clock)

    def test_connection_identities_correlate_interleaved_waiters(self):
        pipe = self._pipe()
        written = []
        written_cv = threading.Condition()

        def write(msg):
            with written_cv:
                written.append(msg)
                written_cv.notify_all()
        pipe._write_command = write
        answers, failures = {}, []
        def worker(label):
            try:
                answers[label] = pipe.send("Synthetic." + label, session_id=label, timeout=2)
            except Exception as exc:
                failures.append(exc)
        first = threading.Thread(target=worker, args=("A",))
        second = threading.Thread(target=worker, args=("B",))
        first.start()
        second.start()
        with written_cv:
            self.assertTrue(written_cv.wait_for(lambda: len(written) == 2, timeout=1))
        mids = {m["sessionId"]: m["id"] for m in written}
        self.assertEqual(len(set(mids.values())), 2)
        with pipe._cv:
            # Inverse-order replies must not fulfill another session's waiter.
            pipe._store_reply_locked({"id": mids["B"], "sessionId": "B", "result": {"who": "B"}})
            pipe._store_reply_locked({"id": mids["A"], "sessionId": "A", "result": {"who": "A"}})
            pipe._cv.notify_all()
        first.join(2)
        second.join(2)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(answers, {"A": {"who": "A"}, "B": {"who": "B"}})

    def test_timeout_tombstone_rejects_expired_and_late_reply(self):
        clock = FakeClock()
        pipe = self._pipe(clock=clock)
        sent = []
        pipe._write_command = lambda message: sent.append(message)
        with self.assertRaisesRegex(CdpError, "timeout"):
            pipe.send("Silent", session_id="A", timeout=0)
        issued = sent[0]["id"]
        self.assertIn(issued, pipe._tombstones)
        with pipe._cv:
            pipe._store_reply_locked({"id": issued, "sessionId": "A", "result": {"stale": True}})
            self.assertNotIn(issued, pipe._replies)
        clock.advance(70_000_000_000)
        with pipe._cv:
            pipe._store_reply_locked({"id": issued, "sessionId": "A", "result": {"stale": True}})
            self.assertNotIn(issued, pipe._replies)

    def test_active_reply_not_evicted_by_unsolicited_flood(self):
        pipe = self._pipe()
        with pipe._cv:
            pipe._pending_set().add(50_000)
            pipe._store_reply_locked({"id": 50_000, "result": {"live": True}})
            for i in range(EVENT_BUFFER + 100):
                pipe._store_reply_locked({"id": 60_000 + i, "result": {"noise": i}})
            self.assertEqual(pipe._replies[50_000]["result"], {"live": True})
            self.assertLessEqual(len(pipe._replies), EVENT_BUFFER)

    def test_navigation_preserves_sibling_events_in_order(self):
        pipe = self._pipe()
        with pipe._cv:
            pipe._store_event_locked({"method": "Page.loadEventFired", "sessionId": "B", "params": {"seq": 1}})
            pipe._store_event_locked({"method": "Page.loadEventFired", "sessionId": "A", "params": {"seq": 0}})
            pipe._store_event_locked({"method": "Page.loadEventFired", "sessionId": "B", "params": {"seq": 2}})
        def fake_send(method, params=None, session_id=None, timeout=None):
            self.assertEqual((method, session_id), ("Page.navigate", "A"))
            with pipe._cv:
                pipe._store_event_locked({"method": "Page.loadEventFired", "sessionId": "A",
                                          "params": {"seq": 7}})
                pipe._cv.notify_all()
            return {"frameId": "frame-a"}
        pipe.send = fake_send
        self.assertEqual(pipe.navigate("A", "https://example.com/a", timeout=1)["frameId"], "frame-a")
        self.assertEqual([
            pipe.wait_event("Page.loadEventFired", session_id="B", timeout=0.1)["params"]["seq"]
            for _ in range(2)
        ], [1, 2])
        with self.assertRaises(CdpError):
            pipe.wait_event("Page.loadEventFired", session_id="A", timeout=0)

    def test_any_session_scan_is_deterministic(self):
        pipe = self._pipe()
        with pipe._cv:
            pipe._store_event_locked({"method": "Ev.test", "sessionId": "B"})
            pipe._store_event_locked({"method": "Ev.test", "sessionId": "A"})
        self.assertEqual(pipe.wait_event("Ev.test", timeout=0)["sessionId"], "B")
        self.assertEqual(pipe.wait_event("Ev.test", timeout=0)["sessionId"], "A")

    def test_crash_fences_only_affected_session(self):
        pipe = self._pipe()
        with pipe._cv:
            pipe._store_event_locked({"method": "Ev.healthy", "sessionId": "B"})
            pipe._store_event_locked({"method": "Target.detachedFromTarget",
                                      "params": {"sessionId": "A"}})
        with self.assertRaisesRegex(CdpError, "fenced"):
            pipe.wait_event("Ev.healthy", session_id="A", timeout=0)
        self.assertEqual(pipe.wait_event("Ev.healthy", session_id="B", timeout=0)["sessionId"], "B")

    def test_overflow_fences_flooded_lane_but_preserves_sibling(self):
        pipe = self._pipe()
        with pipe._cv:
            pipe._store_event_locked({"method": "Ev.ok", "sessionId": "B"})
            for i in range(EVENT_BUFFER):
                pipe._store_event_locked({"method": "Ev.spam", "sessionId": "A", "params": {"i": i}})
        with self.assertRaisesRegex(CdpError, "overflow"):
            pipe.wait_event("Ev.spam", session_id="A", timeout=0)
        self.assertEqual(pipe.wait_event("Ev.ok", session_id="B", timeout=0)["sessionId"], "B")

    def test_writer_fence_blocks_pipe_dispatch_after_stop(self):
        fence = WriterFence()
        pipe = CdpPipe(chrome_bin="/bin/true", writer_fence=fence)
        writes = []
        pipe._write_command = lambda msg: writes.append(msg)
        fence.stop()
        with self.assertRaisesRegex(CdpError, "STOP"):
            pipe.send("Page.enable", session_id="A", timeout=0)
        self.assertEqual(writes, [])


class PortProductionTests(OfflineOnly):
    def test_port_uses_same_identity_and_writer_fence(self):
        fence = WriterFence()
        port = CdpPort(writer_fence=fence)

        class MemorySocket:
            def __init__(self):
                self.messages = []
                self.closed = False

            def send(self, data):
                msg = json.loads(data)
                self.messages.append(msg)
                with port._cv:
                    port._store_reply_locked({"id": msg["id"], "result": {"accepted": True}})
                    port._cv.notify_all()

            def close(self):
                self.closed = True

        ws = MemorySocket()
        port._ws = ws
        self.assertEqual(port.send("Page.enable", session_id="s"), {"accepted": True})
        self.assertEqual(port.send("Page.enable", session_id="s"), {"accepted": True})
        self.assertEqual([m["id"] for m in ws.messages], [1, 2])
        fence.stop()
        with self.assertRaisesRegex(CdpError, "STOP"):
            port.send("Page.enable", session_id="s")
        port._attached.append("owned-s")
        port.close()
        self.assertFalse(ws.closed)  # post-STOP cleanup deferred
        self.assertEqual(len(ws.messages), 2)
        self.assertEqual(port._attached, ["owned-s"])


if __name__ == "__main__":
    unittest.main()
