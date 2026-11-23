"""Direct production gate for fx2-f3-tab-manager-harvest-and-cdp-isolation.

Exercises production classes in wender.perceive.cdp_port, wender.perceive.cdp_pipe,
and wender.perceive.harvest with deterministic offline fakes and injected clocks.
"""
from collections import deque
from copy import deepcopy
import itertools
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
from unittest.mock import MagicMock, Mock, call

from wender.perceive.cdp_pipe import CdpError, CdpPipe, EVENT_BUFFER, frame
from wender.perceive.cdp_port import CdpPort, CdpTabError, TabHandle, normalize_https_origin
from wender.perceive.harvest import (
    CallerTab,
    FencedError,
    HarvestBroker,
    HarvestJob,
    HarvestSite,
    LeaseBusy,
    LeaseError,
    LeaseToken,
    SessionQueues,
    SharedLeaseAuthority,
    TransientReadError,
    make_event,
    normalize_origin,
)


SITES = (
    HarvestSite(
        "perplexity",
        "https://www.perplexity.ai/",
        "https://www.perplexity.ai/fixture-route?q=offline",
    ),
    HarvestSite(
        "chatgpt",
        "https://chatgpt.com/",
        "https://chatgpt.com/fixture-route?q=offline",
    ),
    HarvestSite(
        "gemini",
        "https://gemini.google.com/",
        "https://gemini.google.com/fixture-route?q=offline",
    ),
)
INDEPENDENT = HarvestSite(
    "independent",
    "https://www.google.com/",
    "https://www.google.com/search?fixture=offline",
)
SORRY_URL = "https://www.google.com/sorry/index"


def _node(nid, role, name="", children=()):
    return {
        "nodeId": nid,
        "role": {"value": role},
        "name": {"value": name},
        "ignored": False,
        "childIds": list(children),
    }


def _answer_ax(text, complete=True, user_only=False):
    children = ["user", "user-copy"]
    nodes = [
        _node("user", "heading", "User fixture prompt"),
        _node("user-copy", "button", "Copy fixture answer"),
    ]
    if not user_only:
        children += ["assistant", "answer", "answer-copy", "footer"]
        nodes += [
            _node("assistant", "heading", "Assistant fixture response"),
            _node("answer", "article", "Answer region", ["paragraph"]),
            _node("paragraph", "paragraph", children=["text"]),
            _node("text", "StaticText", text),
            _node("answer-copy", "button", "Copy fixture answer"),
            _node("footer", "StaticText", "UI trailer must not be extracted"),
        ]
    if not complete:
        children.append("generating")
        nodes.append(_node("generating", "button", "Stop generating"))
    return [_node("root", "RootWebArea", "Fixture", children)] + nodes


def _default_targets():
    return [
        {"targetId": "t-" + s.name, "type": "page", "url": s.prefix}
        for s in SITES
    ] + [{"targetId": "t-independent", "type": "page", "url": INDEPENDENT.open_url}]


class _SecondsClock:
    def __init__(self, initial=1000.0):
        self.now = initial

    def __call__(self):
        return self.now

    def advance(self, delta):
        self.now += delta
        return self.now


class _NanosecondClock:
    """CDP clock with a bounded wait callback; wait arguments are seconds."""

    def __init__(self):
        self.now_ns = 100_000_000_000
        self.wait_calls = 0

    def __call__(self):
        return self.now_ns

    def wait_once(self, seconds):
        self.wait_calls += 1
        if self.wait_calls > 1:
            raise AssertionError("mocked CDP wait failed to reach its deadline")
        self.now_ns += int(seconds * 1_000_000_000) + 1


class _ProtocolFakeCDP:
    """Deterministic fake CDP transport supplying external protocol effects only."""

    def __init__(self, targets=None, port=None, auto_load_on_port=False):
        self.targets = deepcopy(_default_targets() if targets is None else targets)
        self.sessions = {}
        self.created = 0
        self.attached = 0
        self.failures = {}
        self.port = port
        self.auto_load_on_port = auto_load_on_port
        self.send = Mock(side_effect=self._send)

    def _send(self, method, params=None, session_id=None, timeout=None):
        params = params or {}
        failure = self.failures.get((method, session_id), self.failures.get(method))
        if failure is not None:
            if isinstance(failure, Exception):
                raise failure
            if callable(failure):
                return failure(method, params, session_id, timeout)
            return deepcopy(failure)
        if method == "Target.getTargets":
            return {"targetInfos": deepcopy(self.targets)}
        if method == "Target.createTarget":
            self.created += 1
            tid = "created-%d" % self.created
            self.targets.append({"targetId": tid, "type": "page", "url": params["url"]})
            return {"targetId": tid}
        if method == "Target.attachToTarget":
            assert params.get("flatten") is True
            assert any(t["targetId"] == params.get("targetId") for t in self.targets)
            self.attached += 1
            sid = "session-%d" % self.attached
            self.sessions[sid] = params["targetId"]
            return {"sessionId": sid}
        if method == "Target.detachFromTarget":
            assert params.get("sessionId") in self.sessions
            del self.sessions[params["sessionId"]]
            return {}
        if method == "Target.closeTarget":
            assert any(t["targetId"] == params.get("targetId") for t in self.targets)
            self.targets = [t for t in self.targets if t["targetId"] != params["targetId"]]
            return {"success": True}
        if method in ("Page.enable", "Accessibility.enable", "Accessibility.disable"):
            assert session_id in self.sessions
            return {}
        if method == "Accessibility.getFullAXTree":
            assert session_id in self.sessions
            return {"nodes": _answer_ax("offline raw AX")}
        if method == "Page.navigate":
            assert session_id in self.sessions
            tid = self.sessions[session_id]
            next(t for t in self.targets if t["targetId"] == tid)["url"] = params["url"]
            if self.port is not None and self.auto_load_on_port:
                self.port.events.append({"method": "Page.loadEventFired", "sessionId": session_id})
            return {"frameId": "frame-" + tid}
        raise AssertionError("unexpected CDP command: " + method)

    def calls_for(self, method):
        return [c for c in self.send.call_args_list if c.args[0] == method]


class TestProductionEnsureTabAndPortIsolation(unittest.TestCase):
    """Direct production tests for CdpPort.ensure_tab, CdpPort.close, and CdpPipe.navigate."""

    def test_ensure_tab_reuses_unique_matching_page_and_navigates_session(self):
        port = CdpPort()
        fake = _ProtocolFakeCDP(
            targets=[
                {"targetId": "t-worker", "type": "worker", "url": "https://chatgpt.com/worker.js"},
                {"targetId": "t-deceptive", "type": "page", "url": "https://chatgpt.com.evil.invalid/"},
                {"targetId": "t-wrong-port", "type": "page", "url": "https://chatgpt.com:8443/"},
                {"targetId": "t-chat", "type": "page", "url": "https://ChatGPT.com:443/c/existing"},
            ],
            port=port,
            auto_load_on_port=True,
        )
        port.send = fake.send

        handle = port.ensure_tab("https://chatgpt.com/", "https://chatgpt.com/fixture-route?q=1")
        self.assertIsInstance(handle, TabHandle)
        self.assertEqual(handle.target_id, "t-chat")
        self.assertEqual(handle["target_id"], "t-chat")
        self.assertEqual(handle.session_id, "session-1")
        self.assertFalse(handle.target_owned)
        self.assertFalse(handle.tab_owned)
        self.assertTrue(handle.session_owned)
        self.assertEqual(handle.target_ownership, "caller")
        self.assertEqual(handle.session_ownership, "broker")
        self.assertEqual(fake.calls_for("Target.createTarget"), [])
        self.assertEqual(
            [c.args[0] for c in fake.send.call_args_list],
            ["Target.getTargets", "Target.attachToTarget", "Page.enable", "Page.navigate"],
        )
        navigation = fake.calls_for("Page.navigate")
        self.assertEqual(len(navigation), 1)
        timeout = navigation[0].kwargs["timeout"]
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 15)
        self.assertEqual(
            navigation,
            [call("Page.navigate", {"url": "https://chatgpt.com/fixture-route?q=1"},
                  session_id="session-1", timeout=timeout)],
        )

    def test_ensure_tab_creates_once_on_true_miss_and_records_ownership(self):
        port = CdpPort()
        fake = _ProtocolFakeCDP(
            targets=[
                {"targetId": "t-worker", "type": "worker", "url": SITES[0].prefix},
                {"targetId": "t-deceptive", "type": "page", "url": "https://www.perplexity.ai.attacker.example/"},
            ],
            port=port,
            auto_load_on_port=True,
        )
        port.send = fake.send

        handle = port.ensure_tab(SITES[0].prefix, SITES[0].open_url)
        self.assertEqual(handle.target_id, "created-1")
        self.assertTrue(handle.target_owned)
        self.assertTrue(handle.tab_owned)
        self.assertTrue(handle.session_owned)
        self.assertEqual(handle.target_ownership, "broker")
        self.assertEqual(handle.session_ownership, "broker")
        self.assertEqual(len(fake.calls_for("Target.createTarget")), 1)
        self.assertEqual(
            fake.calls_for("Target.createTarget"),
            [call("Target.createTarget", {"url": SITES[0].open_url})],
        )

    def test_negative_control_rejects_first_match_ambiguity_under_permutations_and_invalid_hint(self):
        dup_targets = [
            {"targetId": "t-1", "type": "page", "url": "https://www.perplexity.ai/a"},
            {"targetId": "t-2", "type": "page", "url": "https://www.perplexity.ai/b"},
            {"targetId": "t-other", "type": "page", "url": "https://chatgpt.com/"},
        ]
        for perm in itertools.permutations(dup_targets):
            port = CdpPort()
            fake = _ProtocolFakeCDP(targets=list(perm), port=port, auto_load_on_port=True)
            port.send = fake.send
            with self.assertRaisesRegex(CdpError, "AMBIGUOUS_TARGET"):
                port.ensure_tab(SITES[0].prefix, SITES[0].open_url)
            self.assertEqual([c.args[0] for c in fake.send.call_args_list], ["Target.getTargets"])
            self.assertEqual(fake.calls_for("Target.createTarget"), [])
            self.assertEqual(fake.calls_for("Target.attachToTarget"), [])

        # Explicit valid selection resolves ambiguity without creating
        port = CdpPort()
        fake = _ProtocolFakeCDP(targets=dup_targets, port=port, auto_load_on_port=True)
        port.send = fake.send
        handle = port.ensure_tab(SITES[0].prefix, SITES[0].open_url, selected_target="t-2")
        self.assertEqual(handle.target_id, "t-2")
        self.assertFalse(handle.target_owned)
        self.assertEqual(fake.calls_for("Target.createTarget"), [])

        # Invalid explicit hint fails without fallback creation
        with self.assertRaises(CdpTabError):
            port.ensure_tab(SITES[0].prefix, SITES[0].open_url, selected_target="t-nonexistent")
        self.assertEqual(fake.calls_for("Target.createTarget"), [])

    def test_ensure_tab_validates_https_origin_credentials_and_same_origin_route_before_commands(self):
        port = CdpPort()
        fake = _ProtocolFakeCDP(targets=[], port=port, auto_load_on_port=True)
        port.send = fake.send

        invalid_pairs = [
            ("http://chatgpt.com/", "https://chatgpt.com/route"),
            ("https://chatgpt.com/", "http://chatgpt.com/route"),
            ("https://user:secret" "@chatgpt.com/", "https://chatgpt.com/route"),
            ("https://chatgpt.com/", "https://user:secret" "@chatgpt.com/route"),
            ("https://chatgpt.com/", "https://other.example.com/route"),
            ("https://chatgpt.com:443/", "https://chatgpt.com:8443/route"),
            ("https:///missing-host", "https://chatgpt.com/route"),
        ]
        for prefix, open_url in invalid_pairs:
            with self.subTest(prefix=prefix, open_url=open_url):
                with self.assertRaises((CdpError, ValueError)):
                    port.ensure_tab(prefix, open_url)
        fake.send.assert_not_called()

    def test_port_close_and_handle_close_are_idempotent_preserve_caller_pages_and_never_browser_close(self):
        port = CdpPort()
        fake = _ProtocolFakeCDP(port=port, auto_load_on_port=True)
        port.send = fake.send

        h1 = port.ensure_tab(SITES[0].prefix, SITES[0].open_url)
        h2 = port.ensure_tab(SITES[1].prefix, SITES[1].open_url)
        fake.sessions["caller-sid"] = "t-gemini"
        port._caller_sessions.add("caller-sid")

        # Closing h1 detaches only h1's session, preserving h2 and caller-sid
        h1.close()
        h1.close()  # idempotent
        self.assertNotIn(h1.session_id, fake.sessions)
        self.assertIn(h2.session_id, fake.sessions)
        self.assertIn("caller-sid", fake.sessions)

        # Port close detaches remaining owned session h2, never caller-sid, never closes caller tabs or Browser.close
        port.close()
        port.close()  # idempotent
        self.assertNotIn(h2.session_id, fake.sessions)
        self.assertIn("caller-sid", fake.sessions)
        self.assertEqual(fake.calls_for("Target.closeTarget"), [])
        self.assertEqual(fake.calls_for("Browser.close"), [])
        self.assertEqual(len(fake.calls_for("Target.detachFromTarget")), 2)

    def test_negative_control_navigate_rejects_global_load_purge_and_preserves_sibling_order(self):
        port = CdpPort()
        stale_a1 = {"method": "Page.loadEventFired", "sessionId": "sid-A", "params": {"seq": 0}}
        sib_b1 = {"method": "Page.loadEventFired", "sessionId": "sid-B", "params": {"seq": 1}}
        sib_c1 = {"method": "Network.dataReceived", "sessionId": "sid-A", "params": {"seq": 2}}
        sib_b2 = {"method": "Page.loadEventFired", "sessionId": "sid-B", "params": {"seq": 3}}
        port.events.extend([stale_a1, sib_b1, sib_c1, sib_b2])

        def on_send(method, params=None, session_id=None, timeout=None):
            if method == "Page.navigate":
                port.events.append(
                    {"method": "Page.loadEventFired", "sessionId": session_id, "params": {"seq": 99}}
                )
                return {"frameId": "f-" + session_id}
            return {}

        port.send = Mock(side_effect=on_send)
        with self.assertRaises(CdpError):
            port.navigate("", "https://chatgpt.com/")

        res = port.navigate("sid-A", "https://chatgpt.com/")
        self.assertEqual(res, {"frameId": "f-sid-A"})
        self.assertEqual(list(port.events), [sib_b1, sib_c1, sib_b2])


class TestProductionReplyRetentionAndCapacityBounds(unittest.TestCase):
    """Direct production tests for bounded reply retention, active waiter protection, and 5,000 timed-out commands."""

    def test_five_thousand_timed_out_commands_verify_bound_after_each_message_deterministically(self):
        clock = _NanosecondClock()
        pipe = CdpPipe(chrome_bin="/bin/true", timeout=1.0, clock=clock)
        pipe._w = 999
        # Issued IDs are permanently fenced on pipe; exercise diagnostic FIFO
        # on a separate connection where these IDs have never been issued.
        diagnostics = CdpPipe(chrome_bin="/bin/true", clock=clock)

        with mock.patch("os.write", return_value=32):
            for i in range(1, 5001):
                clock.wait_calls = 0
                with mock.patch.object(pipe._cv, "wait", new=clock.wait_once):
                    with self.assertRaises(CdpError):
                        pipe.send("Test.cmd", {"i": i}, timeout=0.5)
                self.assertEqual(clock.wait_calls, 1)
                self.assertEqual(len(pipe._pending), 0)
                with pipe._cv:
                    pipe._store_reply_locked({"id": i, "result": {"i": i}})
                self.assertEqual(pipe._replies, {})
                with diagnostics._cv:
                    diagnostics._store_reply_locked({"id": i, "result": {"i": i}})
                self.assertLessEqual(len(pipe._replies), EVENT_BUFFER)
                self.assertLessEqual(len(diagnostics._replies), EVENT_BUFFER)

        pipe = diagnostics
        self.assertEqual(len(pipe._replies), EVENT_BUFFER)
        # Oldest 3976 replies were evicted in FIFO order; newest 1024 (3977..5000) remain
        self.assertNotIn(1, pipe._replies)
        self.assertNotIn(3976, pipe._replies)
        self.assertIn(3977, pipe._replies)
        self.assertIn(5000, pipe._replies)

    def test_mocked_wait_guard_rejects_nonprogressing_loop(self):
        clock = _NanosecondClock()
        clock.wait_once(0.5)
        with self.assertRaisesRegex(AssertionError, "failed to reach its deadline"):
            clock.wait_once(0.5)

    def test_seconds_clock_rejected_before_transport_or_wait(self):
        for client_type in (CdpPipe, CdpPort):
            for invalid in (100.0, True, "100", None):
                with self.subTest(client=client_type.__name__, invalid=invalid):
                    kwargs = {"chrome_bin": "/bin/true"} if client_type is CdpPipe else {}
                    client = client_type(clock=lambda: invalid, **kwargs)
                    with mock.patch.object(client, "_write_command") as write:
                        with mock.patch.object(client._cv, "wait") as wait:
                            with self.assertRaisesRegex(TypeError, "monotonic nanoseconds as an integer"):
                                client.send("Test.cmd", timeout=0.5)
                    write.assert_not_called()
                    wait.assert_not_called()

    def test_negative_control_live_waiter_protected_from_eviction_duplicate_id_and_stale_pre_reply(self):
        port = CdpPort()
        port._ws = MagicMock()

        # Pre-seed an unsolicited reply with id=1 before command 1 is ever sent
        with port._cv:
            port._store_reply_locked({"id": 1, "result": {"pre_seeded_stale": True}})
        self.assertIn(1, port._replies)

        def on_ws_send(payload):
            msg = json.loads(payload)
            mid = msg["id"]
            # Pre-seeded stale reply for mid must have been cleared on registration before write
            self.assertNotIn(mid, port._replies)
            self.assertIn(mid, port._pending)
            with port._cv:
                # Feed 1200 unsolicited replies before the live reply arrives
                for u in range(10000, 11200):
                    port._store_reply_locked({"id": u, "result": {"unsolicited": u}})
                    self.assertLessEqual(len(port._replies), EVENT_BUFFER)
                # Feed the genuine response for active command mid
                port._store_reply_locked({"id": mid, "result": {"genuine": mid}})
                self.assertLessEqual(len(port._replies), EVENT_BUFFER)
                # Feed a duplicate reply with the same id while mid is still pending
                port._store_reply_locked({"id": mid, "result": {"duplicate_overwrite": True}})
                self.assertLessEqual(len(port._replies), EVENT_BUFFER)
                # Feed another 1200 unsolicited replies while mid is sitting in _replies waiting to be popped
                for u in range(20000, 21200):
                    port._store_reply_locked({"id": u, "result": {"unsolicited": u}})
                    self.assertLessEqual(len(port._replies), EVENT_BUFFER)
                    self.assertIn(mid, port._replies)
                port._cv.notify_all()

        port._ws.send.side_effect = on_ws_send
        res = port.send("Runtime.evaluate", {"expression": "1+1"})
        self.assertEqual(res, {"genuine": 1})
        self.assertNotIn(1, port._pending)
        self.assertNotIn(1, port._replies)
        self.assertLessEqual(len(port._replies), EVENT_BUFFER)

    def test_active_capacity_exhaustion_refuses_admission_before_write_and_write_failure_cleans_pending(self):
        pipe = CdpPipe(chrome_bin="/bin/true")
        pipe._w = 888
        pipe._pending = set(range(1, EVENT_BUFFER + 1))

        with mock.patch("os.write") as mock_write:
            with self.assertRaisesRegex(CdpError, "capacity exhausted"):
                pipe.send("Page.enable")
            mock_write.assert_not_called()
        self.assertEqual(pipe._next_id, 0)

        # Free capacity and verify write failure removes pending state
        pipe._pending.clear()
        with mock.patch("os.write", side_effect=OSError("broken pipe")):
            with self.assertRaisesRegex(CdpError, "^CDP write failed: broken pipe$"):
                pipe.send("Page.enable")
        self.assertEqual(pipe._pending, set())
        self.assertEqual(pipe._replies, {})


class TestProductionPipeStartupAndTeardownFaults(unittest.TestCase):
    """Direct production tests for CdpPipe.start fault rollback and CdpPipe.close safety."""

    def test_negative_control_partial_start_rollback_across_all_allocation_stages(self):
        # 1. mkdtemp failure
        pipe = CdpPipe(chrome_bin="/bin/false")
        with mock.patch("tempfile.mkdtemp", side_effect=OSError("mkdtemp failed")),              mock.patch("os.pipe") as mock_pipe:
            with self.assertRaisesRegex(OSError, "mkdtemp failed"):
                pipe.start()
            mock_pipe.assert_not_called()
        self.assertIsNone(pipe.user_data_dir)

        # 2. First pipe failure cleans mkdtemp directory
        created_dirs = []
        orig_mkdtemp = tempfile.mkdtemp

        def track_mkdtemp(*a, **kw):
            d = orig_mkdtemp(*a, **kw)
            created_dirs.append(d)
            return d

        pipe = CdpPipe(chrome_bin="/bin/false")
        with mock.patch("tempfile.mkdtemp", side_effect=track_mkdtemp),              mock.patch("os.pipe", side_effect=OSError("first pipe failed")):
            with self.assertRaisesRegex(OSError, "first pipe failed"):
                pipe.start()
        self.assertEqual(len(created_dirs), 1)
        self.assertFalse(os.path.exists(created_dirs[0]))
        self.assertIsNone(pipe.user_data_dir)

        # 3. Second pipe failure closes first pipe fds once and removes directory
        closed_fds = []
        pipe = CdpPipe(chrome_bin="/bin/false")
        with mock.patch("tempfile.mkdtemp", side_effect=track_mkdtemp),              mock.patch("os.pipe", side_effect=[(101, 102), OSError("second pipe failed")]),              mock.patch("os.close", side_effect=lambda fd: closed_fds.append(fd)):
            with self.assertRaisesRegex(OSError, "second pipe failed"):
                pipe.start()
        self.assertEqual(closed_fds, [101, 102])
        self.assertFalse(os.path.exists(created_dirs[-1]))
        self.assertIsNone(pipe.user_data_dir)

        # 4. Reader thread startup failure closes remaining fds once, reaps acknowledged child, removes dir
        closed_fds = []
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        fake_thread = MagicMock()
        fake_thread.start.side_effect = RuntimeError("reader thread failed")

        pipe = CdpPipe(chrome_bin="/bin/false")
        with mock.patch("tempfile.mkdtemp", side_effect=track_mkdtemp),              mock.patch("os.pipe", side_effect=[(201, 202), (203, 204)]),              mock.patch("os.close", side_effect=lambda fd: closed_fds.append(fd)),              mock.patch("subprocess.Popen", return_value=fake_proc),              mock.patch("threading.Thread", return_value=fake_thread):
            with self.assertRaisesRegex(RuntimeError, "reader thread failed"):
                pipe.start()

        self.assertEqual(closed_fds, [201, 204, 202, 203])
        fake_proc.kill.assert_called_once_with()
        fake_proc.wait.assert_called_once_with(timeout=5)
        self.assertFalse(os.path.exists(created_dirs[-1]))
        self.assertIsNone(pipe.proc)
        self.assertIsNone(pipe._w)
        self.assertIsNone(pipe._r)
        self.assertIsNone(pipe._reader)
        self.assertIsNone(pipe.user_data_dir)

        # Subsequent close() after partial start must not close any descriptor numbers again
        with mock.patch("os.close") as after_close:
            pipe.close()
            after_close.assert_not_called()

    def test_repeat_start_rejected_before_allocation_and_close_idempotent_under_send_and_wait_faults(self):
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        fake_proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="chrome", timeout=5),
            subprocess.TimeoutExpired(cmd="chrome", timeout=5),
        ]
        fake_thread = MagicMock()
        fake_thread.is_alive.return_value = True

        pipe = CdpPipe(chrome_bin="/bin/false")
        closed_fds = []
        with mock.patch("os.pipe", side_effect=[(301, 302), (303, 304)]),              mock.patch("os.close", side_effect=lambda fd: closed_fds.append(fd)),              mock.patch("subprocess.Popen", return_value=fake_proc),              mock.patch("threading.Thread", return_value=fake_thread):
            pipe.start()
            with mock.patch("tempfile.mkdtemp") as repeat_mkdtemp,                  mock.patch("os.pipe") as repeat_pipe:
                with self.assertRaises(CdpError):
                    pipe.start()
                repeat_mkdtemp.assert_not_called()
                repeat_pipe.assert_not_called()

        udd = pipe.user_data_dir
        self.assertTrue(os.path.exists(udd))
        pipe.send = Mock(side_effect=RuntimeError("send failed during close"))
        with mock.patch("os.close", side_effect=lambda fd: closed_fds.append(fd)):
            pipe.close()
            # Second close must not close descriptors 302/303 again even if OS reuses those numbers
            pipe.close()

        self.assertEqual(closed_fds, [301, 304, 302, 303])
        fake_proc.kill.assert_called_once_with()
        self.assertEqual(fake_proc.wait.call_count, 2)
        self.assertFalse(os.path.exists(udd))
        self.assertIsNone(pipe._w)
        self.assertIsNone(pipe._r)
        self.assertIsNone(pipe.user_data_dir)


class TestProductionHarvestBrokerAndSharedLeases(unittest.TestCase):
    """Direct production gate for HarvestBroker and SharedLeaseAuthority."""

    def setUp(self):
        self.clock = _SecondsClock(1000.0)
        self.authority = SharedLeaseAuthority(initial_epoch=1)
        self.fake = _ProtocolFakeCDP()
        self.broker = HarvestBroker(
            self.fake,
            lease_authority=self.authority,
            holder="broker-1",
            epoch=1,
            clock=self.clock,
        )

    def _submitted(self, broker=None, site=SITES[0], **kwargs):
        b = broker or self.broker
        job = b.start(site, **kwargs)
        self.assertIsNone(job.receipt)
        b.feed(make_event(job, "Page.loadEventFired"))
        b.step(job)
        b.step(job)
        self.assertEqual(job.phase, "WAITING")
        return job

    def _finish_answer(self, job, text=None, broker=None):
        b = broker or self.broker
        text = text if text is not None else ("answer from " + job.site.name)
        b.feed(make_event(job, "fixture.ax", nodes=_answer_ax(text)))
        b.step(job)
        self.assertEqual(job.completion, "complete")
        b.step(job)
        self.assertEqual(job.receipt["status"], "SUCCESS")
        return job.receipt

    def test_shared_authority_contention_across_broker_instances_and_negative_stale_credential_controls(self):
        fake2 = _ProtocolFakeCDP()
        broker2 = HarvestBroker(
            fake2,
            lease_authority=self.authority,
            holder="broker-2",
            epoch=1,
            clock=self.clock,
        )

        # Broker 1 acquires perplexity
        job1 = self.broker.start(SITES[0], question_id="q-1")
        self.assertIsNone(job1.receipt)
        self.assertEqual(job1.token.generation, 1)

        # Broker 2 contends on same normalized origin (with uppercase host and :443)
        alias_site = HarvestSite(
            "perplexity-alias",
            "https://WWW.PERPLEXITY.AI:443/",
            "https://www.perplexity.ai/fixture-route?q=contend",
        )
        contended = broker2.start(alias_site, question_id="q-2")
        self.assertEqual(contended.receipt["status"], "LEASE_BUSY")
        fake2.send.assert_not_called()
        self.assertIs(self.authority.active[SITES[0].lease_key], job1.token)

        # Broker 2 can progress independently on sibling origin (chatgpt)
        sibling = self._submitted(broker=broker2, site=SITES[1], question_id="q-sib")
        sib_receipt = self._finish_answer(sibling, "sibling chatgpt answer", broker=broker2)
        self.assertEqual(sib_receipt["answer"], "sibling chatgpt answer")

        # Complete job1 and start successor job2 on Broker 2
        self.broker.feed(make_event(job1, "Page.loadEventFired"))
        self.broker.step(job1)
        self.broker.step(job1)
        self._finish_answer(job1, "first perplexity answer")

        job2 = broker2.start(SITES[0], question_id="q-2", token="tok-successor")
        self.assertIsNone(job2.receipt)
        self.assertEqual(job2.token.generation, 2)

        # Negative controls: stale token, wrong holder, stale epoch, or forged copy cannot release job2
        self.assertFalse(self.authority.release(job1.token))
        self.assertFalse(self.authority.release(job2.token, holder="wrong-holder"))
        self.assertFalse(self.authority.release(job2.token, epoch=999))
        self.assertFalse(self.authority.release(job2.token, release_token="wrong-secret"))
        forged = LeaseToken(
            site=job2.token.site,
            question_id=job2.token.question_id,
            generation=job2.token.generation,
            holder=job2.token.holder,
            epoch=job2.token.epoch,
            token=job2.token.token,
        )
        self.assertFalse(self.authority.release(forged))
        self.assertIs(self.authority.active[SITES[0].lease_key], job2.token)

        # Revoking or advancing epoch fences job2 before its next effective operation
        self.authority.advance_epoch(SITES[0].lease_key)
        broker2.feed(make_event(job2, "Page.loadEventFired"))
        calls_before = len(fake2.send.call_args_list)
        broker2.step(job2)
        self.assertEqual(job2.receipt["status"], "CANCELLED")
        self.assertEqual(job2.receipt["cleanup_status"], "deferred")
        self.assertEqual(len(fake2.send.call_args_list), calls_before)
        broker2.submit.assert_called_once()  # only sibling called submit

    def test_shuffled_answers_provenance_cross_site_substitution_and_duplicate_terminals_rejected(self):
        jobs = [self.broker.start(s, question_id="q-fanout") for s in SITES]
        stopped = self.broker.start(INDEPENDENT, question_id="q-fanout")

        # Independent tab hits /sorry/ wall
        self.broker.feed(
            {
                "method": "Page.frameNavigated",
                "sessionId": stopped.session_id,
                "params": {"frame": {"url": SORRY_URL}},
            }
        )
        self.assertEqual(stopped.receipt["status"], "STOP")
        self.assertEqual(stopped.receipt["cleanup_status"], "deferred")

        # Native session-only load without question provenance must be rejected
        self.broker.feed({"method": "Page.loadEventFired", "sessionId": jobs[0].session_id})
        self.broker.step(jobs[0])
        self.assertEqual(jobs[0].phase, "NAVIGATING")

        # Deliver correlated loads in reverse order and advance all three healthy jobs to WAITING
        for job in reversed(jobs):
            self.broker.feed(make_event(job, "Page.loadEventFired"))
        self.broker.step()
        self.broker.step()
        self.assertTrue(all(j.phase == "WAITING" for j in jobs))
        self.assertEqual(self.broker.submit.call_count, 3)

        # Negative control: cross-site answer substitution in event envelope is rejected
        cross_site_ev = make_event(jobs[0], "fixture.ax", nodes=_answer_ax("spoofed from chatgpt"))
        cross_site_ev["site"] = "chatgpt"
        self.broker.feed(cross_site_ev)
        # Mismatched target_id envelope is rejected
        wrong_target_ev = make_event(jobs[0], "fixture.ax", nodes=_answer_ax("wrong target"))
        wrong_target_ev["target_id"] = "t-chatgpt"
        self.broker.feed(wrong_target_ev)
        self.broker.step(jobs[0])
        self.assertEqual(jobs[0].completion, "pending")

        # Deliver valid answers in shuffled order
        for job in (jobs[2], jobs[0], jobs[1]):
            self.broker.feed(make_event(job, "fixture.ax", nodes=_answer_ax("verified " + job.site.name)))
        for job in (jobs[1], jobs[2], jobs[0]):
            self.broker.step(job)
        for job in (jobs[2], jobs[1], jobs[0]):
            self.broker.step(job)
            self.assertEqual(job.receipt["status"], "SUCCESS")
            self.assertEqual(job.receipt["answer"], "verified " + job.site.name)
            self.assertEqual(job.receipt["requested_route"], job.site.open_url)

        # Duplicate terminal events cannot emit a second receipt or mutate the terminal receipt
        receipt_count = len(self.broker.collected_receipts())
        self.broker.feed(make_event(jobs[0], "fixture.ax", nodes=_answer_ax("duplicate answer")))
        self.broker.feed(
            {
                "method": "Page.frameNavigated",
                "sessionId": jobs[0].session_id,
                "params": {"frame": {"url": SORRY_URL}},
            }
        )
        self.broker.step(jobs[0])
        self.assertEqual(len(self.broker.collected_receipts()), receipt_count)
        self.assertEqual(jobs[0].receipt["answer"], "verified perplexity")

    def test_early_stop_during_attach_and_post_stop_zero_dispatch_negative_control(self):
        orig_send = self.fake.send.side_effect

        def inject_early_stop(method, params=None, session_id=None, timeout=None):
            res = orig_send(method, params, session_id, timeout)
            if method == "Target.attachToTarget":
                # Unrelated session event must be ignored; acknowledged session STOP must replay before enable
                self.broker.feed(
                    {
                        "method": "Page.frameNavigated",
                        "sessionId": "unrelated-session",
                        "params": {"frame": {"url": SORRY_URL}},
                    }
                )
                self.broker.feed(
                    {
                        "method": "Page.frameNavigated",
                        "sessionId": res["sessionId"],
                        "params": {"frame": {"url": SORRY_URL}},
                    }
                )
            return res

        self.fake.send.side_effect = inject_early_stop
        job = self.broker.start(SITES[0])
        self.assertEqual(job.receipt["status"], "STOP")
        self.assertEqual(job.receipt["cleanup_status"], "deferred")
        self.assertEqual(self.fake.calls_for("Page.enable"), [])
        self.assertEqual(self.fake.calls_for("Page.navigate"), [])
        self.assertEqual(self.fake.calls_for("Target.detachFromTarget"), [])
        self.assertIn(SITES[0].lease_key, self.broker.tombstones)

        # Subsequent start on tombstoned site returns REVIEW_REQUIRED with zero CDP calls
        before_calls = list(self.fake.send.call_args_list)
        blocked = self.broker.start(SITES[0], question_id="q-after-stop")
        self.assertEqual(blocked.receipt["status"], "REVIEW_REQUIRED")
        self.assertEqual(self.fake.send.call_args_list, before_calls)

    def test_local_and_global_cancellation_and_late_create_attach_acknowledgements(self):
        # 1. Late attach acknowledgement after cancellation during Target.attachToTarget
        orig_send = self.fake.send.side_effect

        def cancel_on_attach(method, params=None, session_id=None, timeout=None):
            res = orig_send(method, params, session_id, timeout)
            if method == "Target.attachToTarget":
                self.broker.cancel(self.broker.jobs[-1])
            return res

        self.fake.send.side_effect = cancel_on_attach
        late_job = self.broker.start(SITES[0])
        self.assertEqual(late_job.receipt["status"], "CANCELLED")
        self.assertEqual(late_job.receipt["session_id"], late_job.session_id)
        self.assertEqual(late_job.receipt["session_ownership"], "broker")
        self.assertEqual(late_job.receipt["cleanup_status"], "deferred")
        self.assertEqual(self.fake.calls_for("Page.enable"), [])

        # Restore normal send and test local vs global cancellation on remaining sites
        self.fake.send.side_effect = orig_send
        j_chat = self._submitted(site=SITES[1], question_id="q-chat")
        j_gem = self._submitted(site=SITES[2], question_id="q-gem")

        # Local cancel on j_chat preserves j_gem
        self.broker.cancel(j_chat)
        self.assertEqual(j_chat.receipt["status"], "CANCELLED")
        self.assertIsNone(j_gem.receipt)

        # Global cancel fences j_gem and blocks future admission
        self.broker.cancel()
        self.assertEqual(j_gem.receipt["status"], "CANCELLED")
        self.assertEqual(self.authority.active, {})
        before_calls = list(self.fake.send.call_args_list)
        after_global = self.broker.start(INDEPENDENT, question_id="q-blocked")
        self.assertEqual(after_global.receipt["status"], "REVIEW_REQUIRED")
        self.assertEqual(self.fake.send.call_args_list, before_calls)

    def test_whole_job_injected_clock_deadline_across_admission_setup_waiting_and_extraction(self):
        # 1. Deadline expired during setup (Target.getTargets)
        orig_send = self.fake.send.side_effect

        def slow_get_targets(method, params=None, session_id=None, timeout=None):
            res = orig_send(method, params, session_id, timeout)
            if method == "Target.getTargets":
                self.clock.advance(10.0)
            return res

        self.fake.send.side_effect = slow_get_targets
        setup_expired = self.broker.start(SITES[0], timeout=5.0)
        self.assertEqual(setup_expired.receipt["status"], "TIMEOUT")
        self.assertEqual(setup_expired.receipt["cleanup_status"], "deferred")
        self.assertEqual(self.fake.calls_for("Target.attachToTarget"), [])
        self.assertEqual(self.authority.active, {})

        # 2. Deadline expired during extraction blocks answer even if extractor returns text
        self.fake.send.side_effect = orig_send
        job = self._submitted(site=SITES[1], timeout=20.0)
        self.broker.feed(make_event(job, "fixture.ax", nodes=_answer_ax("late answer after deadline")))
        self.broker.step(job)
        self.assertEqual(job.phase, "EXTRACT")

        def slow_extract(snapshot):
            self.clock.advance(25.0)
            return "late answer after deadline"

        self.broker.extract = Mock(side_effect=slow_extract)
        self.broker.step(job)
        self.assertEqual(job.receipt["status"], "TIMEOUT")
        self.assertEqual(job.receipt["completion_status"], "complete")
        self.assertEqual(job.receipt["extraction_status"], "blocked")
        self.assertIsNone(job.receipt["answer"])
        self.assertEqual(job.receipt["cleanup_status"], "deferred")

    def test_session_queue_overflow_cleanup_failure_and_tombstone_reconciliation(self):
        self.broker.queues.limit = 2
        j1 = self.broker.start(SITES[0])
        j2 = self.broker.start(SITES[1])

        sib_load = make_event(j2, "Page.loadEventFired")
        self.broker.feed(sib_load)
        for _ in range(3):
            self.broker.feed(make_event(j1, "fixture.other"))

        self.assertEqual(j1.receipt["status"], "EVENT_OVERFLOW")
        self.assertEqual(j1.receipt["cleanup_status"], "deferred")
        self.assertEqual(list(self.broker.queues.by_session[j2.session_id]), [sib_load])

        # Reconcile SITES[0] tombstone explicitly and verify readmission succeeds
        self.assertIn(SITES[0].lease_key, self.broker.tombstones)
        self.authority.reconcile(SITES[0])
        self.assertNotIn(SITES[0].lease_key, self.broker.tombstones)

        # Now test cleanup failure on SITES[2]: primary failure preserved, lease released, tombstone set
        self.fake.failures["Page.enable"] = CdpError("enable primary failure")
        self.fake.failures["Target.detachFromTarget"] = CdpError("detach cleanup failure")
        j3 = self.broker.start(SITES[2])
        self.assertEqual(j3.receipt["status"], "SETUP_ERROR")
        self.assertEqual(j3.receipt["error"], "enable primary failure")
        self.assertEqual(j3.receipt["cleanup_status"], "failed")
        self.assertEqual(j3.receipt["cleanup_errors"], ["detach cleanup failure"])
        self.assertNotIn(SITES[2].lease_key, self.authority.active)
        self.assertIn(SITES[2].lease_key, self.broker.tombstones)


if __name__ == "__main__":
    unittest.main()
