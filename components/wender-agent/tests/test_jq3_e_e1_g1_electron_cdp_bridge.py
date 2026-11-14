"""Offline public-path CDP bridge gate; no Electron, websocket or network use.

Synthetic scripts assert each send(method, params, session_id) triple. Mutants
are compiled in memory under a package-qualified name and never written out.
"""

import ast
from collections import deque
import copy
import inspect
import random
import sys
import threading
import time
import types
import unittest
from unittest import mock

from wender.perceive import cdp_pipe
from wender.perceive.electron_cdp_bridge import (
    BridgeError, ElectronCdpBridge, PageTarget, StaleTargetError, TargetBinding,
)
from wender.perceive.session_registry import Handle
from wender.state import schema


SEED = 0xE1C0D3
PROPERTY_CASES = 64  # [TARGET], deterministic with replayable seed/case
CONCURRENCY_SCHEDULES = 32  # [TARGET], bounded joins and no leaked threads
BUDGET_S = 15  # [TARGET] VM test budget, not a measurement
DOM = {"root": {"nodeId": 1, "nodeType": 9, "nodeName": "#document",
                "documentURL": "app://demo/page"}}
AX = {"nodes": [
    {"nodeId": "ax-root", "ignored": False,
     "role": {"type": "role", "value": "RootWebArea"},
     "name": {"type": "computedString", "value": "Test window"},
     "childIds": ["ax-button"]},
    {"nodeId": "ax-button", "ignored": False,
     "role": {"type": "role", "value": "button"},
     "name": {"type": "computedString", "value": "Apply"},
     "properties": [{"name": "disabled", "value": {"type": "boolean", "value": False}}]},
]}


def page(tid="t1", url="app://demo/page", *, other=False):
    infos = [{"targetId": tid, "type": "page", "url": url,
              "title": "Test window", "openerId": "UNTRUSTED"}]
    if other:
        infos.append({"targetId": "worker", "type": "worker", "url": url})
    return {"targetInfos": infos}


def attach_script(tid="t1", sid="s1", *, url="app://demo/page"):
    return [
        ("Target.getTargets", {}, None, page(tid, url, other=True)),
        ("Target.attachToTarget", {"targetId": tid, "flatten": True}, None,
         {"sessionId": sid}),
    ]


def snapshot_script(sid="s1", dom=None, ax=None):
    return [
        ("DOM.getDocument", {"depth": 0}, sid, copy.deepcopy(DOM if dom is None else dom)),
        ("Accessibility.enable", {}, sid, {}),
        ("Accessibility.getFullAXTree", {}, sid, copy.deepcopy(AX if ax is None else ax)),
        ("Accessibility.disable", {}, sid, {}),
    ]


class Transcript:
    """Every method, params AND flattened session must match; no stray calls."""

    def __init__(self, calls):
        self.calls = deque(calls)
        self.observed = []

    def send(self, method, params=None, session_id=None, timeout=None):
        triple = (method, params, session_id)
        self.observed.append(triple)
        if not self.calls:
            raise AssertionError("unexpected CDP call %r" % (triple,))
        want_m, want_p, want_s, result = self.calls.popleft()
        if triple != (want_m, want_p, want_s):
            raise AssertionError("CDP signature mismatch: %r != %r" %
                                 (triple, (want_m, want_p, want_s)))
        return copy.deepcopy(result)

    def complete(self, case=""):
        if self.calls:
            raise AssertionError("unconsumed transcript %s: %r" % (case, list(self.calls)))


class FakePort(Transcript):
    def __init__(self, script, tid="port-1", sid="p-1"):
        super().__init__(script)
        self.tid, self.sid, self.port_calls = tid, sid, []

    def ensure_tab(self, url_prefix, open_url, selected_target=None, timeout=None):
        self.port_calls.append((url_prefix, open_url, selected_target, timeout))
        return types.SimpleNamespace(target_id=self.tid, session_id=self.sid,
                                     session_owned=True, target_owned=False)


class TestElectronCdpBridge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.started = time.perf_counter()

    @classmethod
    def tearDownClass(cls):
        elapsed = time.perf_counter() - cls.started
        print("VM bridge gate: %.3fs elapsed; TARGET %ds" % (elapsed, BUDGET_S))
        if elapsed >= BUDGET_S:
            raise AssertionError("VM bridge gate exceeded 15-second target")

    def test_current_pipe_dom_ax_exact_transcript_and_closed_schema(self):
        script = attach_script() + snapshot_script()
        cdp = Transcript(script)
        bridge = ElectronCdpBridge.from_pipe(cdp,
            parent_by_target={"t1": "electron-window-7"},
            business={"app_id": "org.example.demo", "workspace": "QA"})
        binding = bridge.attach_target("t1")
        self.assertEqual(binding.generation, 1)
        self.assertEqual(binding.handle, Handle("cdp_tab", "t1", 1))
        self.assertEqual(binding.parent_id, "electron-window-7")
        descriptor = bridge.target_descriptor(binding)
        self.assertEqual(schema.validate_target_descriptor(descriptor), [])
        self.assertEqual(set(descriptor), set(schema.TARGET_DESCRIPTOR_KEYS))
        state = descriptor["state"]
        self.assertEqual(set(state), set(schema.TOP_KEYS))
        self.assertEqual(state["source"]["kind"], "cdp")
        self.assertEqual(state["source"]["url"], "app://demo/page")
        self.assertEqual([n["role"] for n in state["nodes"]], ["RootWebArea", "button"])
        self.assertEqual(state["counts"]["raw"], 2)
        self.assertEqual(state["counts"]["actionable"], 1)
        self.assertEqual(state["actionable"], [state["nodes"][1]["id"]])
        cdp.complete("positive pipe")
        self.assertEqual([x[0] for x in cdp.observed],
            ["Target.getTargets", "Target.attachToTarget", "DOM.getDocument",
             "Accessibility.enable", "Accessibility.getFullAXTree", "Accessibility.disable"])

    def test_port_attach_injected_no_endpoint_and_ownership(self):
        port = FakePort(snapshot_script("p-1"))
        bridge = ElectronCdpBridge.from_port(port,
            parent_by_target={"port-1": "parent-2"})
        binding = bridge.attach_port_tab("https://example.test/ui", "https://example.test/ui/home",
                                         selected_target="port-1", timeout=1)
        self.assertEqual(binding.generation, 1)
        self.assertEqual(binding.session_id, "p-1")
        self.assertEqual(binding.parent_id, "parent-2")
        self.assertFalse(binding.target_owned)
        self.assertEqual(port.port_calls,
                         [("https://example.test/ui", "https://example.test/ui/home", "port-1", 1)])
        self.assertEqual(bridge.read_state(binding)["source"]["kind"], "cdp")
        port.complete("port")

    def test_detach_crash_and_reattach_fence_generation(self):
        cdp = Transcript(attach_script("t1", "s1") + attach_script("t1", "s2") +
                         attach_script("t1", "s3"))
        b = ElectronCdpBridge(cdp)
        first = b.attach_target("t1")
        self.assertEqual(first.generation, 1)
        self.assertFalse(b.accept_event({"method": "Target.detachedFromTarget",
            "params": {"sessionId": "foreign", "targetId": "t1"}}))
        self.assertTrue(b.current(first))
        self.assertTrue(b.accept_event({"method": "Target.detachedFromTarget",
            "params": {"sessionId": "s1", "targetId": "t1"}}))
        self.assertFalse(b.current(first))
        with self.assertRaises(StaleTargetError):
            b.read_state(first)
        second = b.attach_target("t1")
        self.assertEqual(second.generation, 2)
        self.assertNotEqual(first.handle, second.handle)
        self.assertFalse(b.accept_event({"method": "Target.targetCrashed",
            "sessionId": "s1", "params": {"targetId": "t1"}}))
        self.assertTrue(b.current(second))
        self.assertTrue(b.accept_event({"method": "Target.targetCrashed",
            "sessionId": "s2", "params": {"targetId": "t1"}}))
        third = b.attach_target("t1")
        self.assertEqual(third.generation, 3)
        self.assertTrue(b.current(third))
        self.assertFalse(b.current(second))
        cdp.complete("three generations")

    def test_recycled_session_rejected_and_remains_fenced(self):
        cdp = Transcript(attach_script("t1", "s1") + attach_script("t1", "s1") +
                         attach_script("t1", "s2"))
        bridge = ElectronCdpBridge(cdp)
        old = bridge.attach_target("t1")
        self.assertTrue(bridge.invalidate_target("t1"))
        with self.assertRaisesRegex(BridgeError, "retired"):
            bridge.attach_target("t1")
        new = bridge.attach_target("t1")
        self.assertEqual((old.generation, new.generation), (1, 2))
        self.assertFalse(bridge.current(old))
        cdp.complete("reused session")

    def test_no_foreign_session_evidence_and_bad_dom_ax_rejected(self):
        cdp = Transcript(attach_script() + snapshot_script("s1", dom={"root": {"nodeId": 1}}))
        b = ElectronCdpBridge(cdp)
        binding = b.attach_target("t1")
        with self.assertRaisesRegex(BridgeError, "DOM.getDocument"):
            b.read_state(binding)
        # Invalid DOM stops before Accessibility.enable; strict fake notices no extra calls.
        self.assertEqual(len(cdp.calls), 3)
        # Foreign target/session data cannot be substituted into a current binding.
        other = TargetBinding("t1", "foreign", 1, Handle("cdp_tab", "t1", 1),
                              "app://demo/page", "Test window", None)
        with self.assertRaises(StaleTargetError):
            b.read_state(other)
        with self.assertRaisesRegex(BridgeError, "port transport"):
            ElectronCdpBridge(Transcript(attach_script())).attach_port_tab("a", "b")

    def test_foreign_session_result_even_with_correct_send_is_rejected(self):
        wrong_ax = copy.deepcopy(AX)
        wrong_ax["sessionId"] = "foreign"
        transcript = Transcript(attach_script() + snapshot_script("s1", ax=wrong_ax))
        bridge = ElectronCdpBridge(transcript)
        binding = bridge.attach_target("t1")
        with self.assertRaisesRegex(StaleTargetError, "foreign-session"):
            bridge.read_state(binding)
        transcript.complete("foreign-result provenance")

    def test_explicit_detach_acknowledgement_and_local_fence(self):
        script = attach_script() + [
            ("Target.detachFromTarget", {"sessionId": "s1"}, None, {})]
        transcript = Transcript(script)
        bridge = ElectronCdpBridge(transcript)
        binding = bridge.attach_target("t1")
        bridge.detach(binding)
        self.assertFalse(bridge.current(binding))
        with self.assertRaises(StaleTargetError):
            bridge.target_descriptor(binding)
        transcript.complete("explicit detach")

    def test_empty_ax_and_source_provenance_fail_closed(self):
        transcript = Transcript(attach_script() + snapshot_script("s1", ax={"nodes": []}))
        bridge = ElectronCdpBridge(transcript)
        binding = bridge.attach_target("t1")
        with self.assertRaisesRegex(BridgeError, "AX nodes"):
            bridge.read_state(binding)
        transcript.complete("empty AX")
        with self.assertRaises(BridgeError):
            TargetBinding("t", "s", 0, Handle("cdp_tab", "t", 1), "", "", None)
        with self.assertRaises(ValueError):
            Handle("cdp_tab", "t", 0)

    def test_business_is_separate_versioned_and_explicit_parent_only(self):
        details = {"vendor": "UserApp", "nonce": "not-in-state"}
        transcript = Transcript(attach_script() + snapshot_script())
        bridge = ElectronCdpBridge(transcript, business=details)
        details["vendor"] = "changed after injection"
        current = bridge.attach_target("t1")
        state = bridge.read_state(current)
        identity = bridge.business_envelope(current)
        self.assertEqual(identity["schema"], "wender.electron-business/v1")
        self.assertIsNone(identity["target"]["parent_id"])
        self.assertEqual(identity["business"]["vendor"], "UserApp")
        self.assertNotIn("business", state)
        self.assertNotIn("parent", state["source"])
        self.assertNotIn("openerId", repr(identity))
        identity["business"]["vendor"] = "another mutation"
        self.assertEqual(bridge.business_envelope(current)["business"]["vendor"], "UserApp")
        transcript.complete("identity split")

    def test_reattach_event_and_connection_reset(self):
        cdp = Transcript(attach_script("t1", "s1") + attach_script("t1", "s2") +
                         attach_script("t1", "s3"))
        bridge = ElectronCdpBridge(cdp)
        first = bridge.attach_target("t1")
        self.assertFalse(bridge.accept_event({"method": "Target.attachedToTarget",
            "params": {"sessionId": "s1", "targetInfo": {"targetId": "t1"}}}))
        self.assertTrue(bridge.accept_event({"method": "Target.attachedToTarget",
            "params": {"sessionId": "s2", "targetInfo": {"targetId": "t1"}}}))
        second = bridge.attach_target("t1")
        self.assertEqual(second.generation, 2)
        bridge.invalidate_all()
        self.assertFalse(bridge.current(second))
        third = bridge.attach_target("t1")
        self.assertEqual(third.generation, 3)
        cdp.complete("connection reset")

    def test_property_seeded_64_generation_and_parent_oracles(self):
        rng = random.Random(SEED)
        completed = 0
        for case in range(PROPERTY_CASES):
            tid = "T%d" % case
            attach_n = rng.randint(1, 4)
            parent = "W%d" % rng.randrange(25)
            scripts = sum((attach_script(tid, "S%d-%d" % (case, j))
                           for j in range(attach_n)), [])
            transcript = Transcript(scripts)
            bridge = ElectronCdpBridge(transcript, parent_by_target={tid: parent})
            old = None
            with self.subTest(seed=SEED, case=case, attachments=attach_n):
                for j in range(attach_n):
                    binding = bridge.attach_target(tid)
                    oracle_generation = j + 1  # independent, one-based attach count
                    self.assertEqual(binding.generation, oracle_generation,
                        "seed=%d case=%d attach=%d" % (SEED, case, j))
                    self.assertEqual(binding.handle.generation, oracle_generation)
                    self.assertEqual(binding.parent_id, parent)
                    if old is not None:
                        self.assertFalse(bridge.current(old))
                        self.assertNotEqual(old.handle, binding.handle)
                    old = binding
                    if j % 2 == 1:
                        self.assertFalse(bridge.accept_event({"method": "Target.detachedFromTarget",
                            "params": {"sessionId": "unknown"}}))
                self.assertTrue(bridge.current(old))
                self.assertTrue(bridge.invalidate_target(tid))
                self.assertFalse(bridge.current(old))
                transcript.complete("seed=%d case=%d" % (SEED, case))
                completed += 1
        self.assertEqual(completed, PROPERTY_CASES, "property case budget exhausted")

    def test_concurrency_32_logical_schedules_no_leaks(self):
        completed = 0
        for case in range(CONCURRENCY_SCHEDULES):
            tid = "parallel-%d" % case
            barrier = threading.Barrier(3)
            ready, release = threading.Event(), threading.Event()
            schedule = []
            schedule_lock = threading.Lock()

            class BlockingTransport:
                def send(self, method, params=None, session_id=None, timeout=None):
                    if method == "Target.getTargets":
                        return page(tid)
                    if method == "Target.attachToTarget":
                        return {"sessionId": "ss-%d" % case}
                    if method == "DOM.getDocument":
                        return copy.deepcopy(DOM)
                    if method == "Accessibility.enable":
                        return {}
                    if method == "Accessibility.getFullAXTree":
                        with schedule_lock:
                            schedule.append("AX in flight")
                        ready.set()
                        barrier.wait(timeout=2)
                        if not release.wait(timeout=2):
                            raise RuntimeError("incomplete scheduled detach")
                        return copy.deepcopy(AX)
                    if method == "Accessibility.disable":
                        return {}
                    raise AssertionError("unexpected method %r" % method)

            bridge = ElectronCdpBridge(BlockingTransport())
            binding = bridge.attach_target(tid)
            outcomes = []

            def reader():
                try:
                    bridge.read_state(binding)
                    outcomes.append("WRONG:accepted stale evidence")
                except StaleTargetError:
                    with schedule_lock:
                        schedule.append("reader fenced")
                    outcomes.append("stale")
                except BaseException as exc:
                    outcomes.append("UNEXPECTED:" + repr(exc))

            def invalidator():
                try:
                    if not ready.wait(timeout=2):
                        raise RuntimeError("reader did not reach AX")
                    barrier.wait(timeout=2)
                    if case % 2:
                        bridge.accept_event({"method": "Target.targetCrashed",
                            "sessionId": binding.session_id, "params": {"targetId": tid}})
                    else:
                        bridge.accept_event({"method": "Target.detachedFromTarget",
                            "params": {"sessionId": binding.session_id, "targetId": tid}})
                    with schedule_lock:
                        schedule.append("generation retired")
                except BaseException as exc:
                    outcomes.append("UNEXPECTED:" + repr(exc))
                finally:
                    release.set()

            threads = [threading.Thread(target=reader, name="e1-reader-%d" % case),
                       threading.Thread(target=invalidator, name="e1-invalid-%d" % case)]
            try:
                for thread in threads:
                    thread.start()
                self.assertTrue(ready.wait(timeout=2), "no in-flight AX: case %d" % case)
                barrier.wait(timeout=2)
            finally:
                release.set() if not ready.is_set() else None
                for thread in threads:
                    thread.join(timeout=2)
                if any(t.is_alive() for t in threads):
                    barrier.abort()
                    release.set()
                    for thread in threads:
                        thread.join(timeout=2)
            with self.subTest(case=case):
                self.assertFalse(any(t.is_alive() for t in threads),
                                 "leaked threads in schedule %d" % case)
                self.assertEqual(outcomes, ["stale"], "case=%d log=%r" % (case, schedule))
                self.assertIn("AX in flight", schedule)
                self.assertIn("generation retired", schedule)
                self.assertIn("reader fenced", schedule)
                self.assertLess(schedule.index("generation retired"),
                                schedule.index("reader fenced"))
                completed += 1
        self.assertEqual(completed, CONCURRENCY_SCHEDULES,
                         "concurrency schedule budget exhausted")


class _AbortOnWait:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def wait(self, seconds):
        raise AssertionError("semantic mutant: waited after deadline")


class TestCatalogMutants(unittest.TestCase):
    @staticmethod
    def _mutant(method, mutate):
        """Unique qualified AST anchors; compilation under a real package name."""
        source = inspect.getsource(cdp_pipe)
        tree = ast.parse(source, filename=cdp_pipe.__file__)
        classes = [x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == "CdpPipe"]
        if len(classes) != 1:
            raise AssertionError("CdpPipe anchor not unique")
        funcs = [x for x in classes[0].body if isinstance(x, ast.FunctionDef) and x.name == method]
        if len(funcs) != 1:
            raise AssertionError("CdpPipe.%s anchor not unique" % method)
        mutate(funcs[0])
        ast.fix_missing_locations(tree)
        name = "wender.perceive._cdp_pipe_e1_mutant_" + method
        module = types.ModuleType(name)
        module.__file__ = cdp_pipe.__file__
        module.__package__ = "wender.perceive"
        if name in sys.modules:
            raise AssertionError("mutant module leaked into sys.modules")
        try:
            sys.modules[name] = module
            exec(compile(tree, cdp_pipe.__file__, "exec"), module.__dict__)
            yield module
        finally:
            sys.modules.pop(name, None)

    @staticmethod
    def _deadline_oracle(module, *, mutant):
        # Minimal __new__ client, equivalent to offline transport fixtures.
        c = module.CdpPipe.__new__(module.CdpPipe)
        c.timeout = 0
        c.clock = lambda: 5_000_000_000
        c._cv = _AbortOnWait()
        c._replies = {}
        c._pending = set()
        c._request_info = {}
        c._tombstones = {}
        c._events_by_session = {}
        c._lane_errors = {}
        c._session_incarnations = {}
        c._target_sessions = {}
        c._connection_incarnation = 0
        c.events = deque()
        c._closed = False
        if mutant:
            # Only AssertionError from the semantic oracle counts as a kill.
            with unittest.TestCase().assertRaisesRegex(AssertionError, "waited after deadline"):
                c.wait_event("NoEvent", session_id="a", timeout=0)
        else:
            with unittest.TestCase().assertRaisesRegex(module.CdpError,
                                                        "timeout waiting for event NoEvent"):
                c.wait_event("NoEvent", session_id="a", timeout=0)

    @staticmethod
    def _fd_oracle(module):
        events = []
        closes = []

        class Proc:
            def poll(self):
                return None

        class NoThread:
            def __init__(self, *args, **kwargs):
                pass
            def start(self):
                pass
            def is_alive(self):
                return False

        def fcntl_oracle(fd, operation, lowest):
            events.append(("fcntl", fd, operation, lowest))
            return 31 if fd == 21 else 32

        def dup2_oracle(source, dest):
            events.append(("dup2", source, dest))

        def fake_spawn(_args, **kwargs):
            kwargs["preexec_fn"]()
            return Proc()

        with (mock.patch.object(module.tempfile, "mkdtemp", return_value="/tmp/e1-fd-fixture"),
              mock.patch.object(module.os, "pipe", side_effect=[(21, 22), (23, 24)]),
              mock.patch.object(module.os, "close", side_effect=lambda fd: closes.append(fd)),
              mock.patch.object(module.os, "dup2", side_effect=dup2_oracle),
              mock.patch.object(module.fcntl, "fcntl", side_effect=fcntl_oracle),
              mock.patch.object(module.subprocess, "Popen", side_effect=fake_spawn),
              mock.patch.object(module.threading, "Thread", NoThread)):
            client = module.CdpPipe(chrome_bin="/stub-chrome", timeout=0.1)
            client.start()
        expected = [
            ("fcntl", 21, module.fcntl.F_DUPFD, 10),
            ("fcntl", 24, module.fcntl.F_DUPFD, 10),
            ("dup2", 31, 3),
            ("dup2", 32, 4),
        ]
        assert events == expected, "semantic FD 3/4 map mismatch: %r != %r" % (events, expected)
        assert closes == [21, 24], "incorrect parent FD cleanup: %r" % closes

    def test_catalog_deadline_addition_mutant_semantically_killed(self):
        self._deadline_oracle(cdp_pipe, mutant=False)  # real-module positive control

        def mutate(fn):
            matches = [n for n in ast.walk(fn)
                       if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Sub)
                       and isinstance(n.left, ast.Name) and n.left.id == "deadline_ns"
                       and isinstance(n.right, ast.Call)
                       and isinstance(n.right.func, ast.Attribute)
                       and n.right.func.attr == "_now_ns"]
            if len(matches) != 1:
                raise AssertionError("wait_event deadline-minus-clock anchor not unique")
            matches[0].op = ast.Add()

        for module in self._mutant("wait_event", mutate):
            self._deadline_oracle(module, mutant=True)

    def test_catalog_fdupfd_boundary_mutant_semantically_killed(self):
        self._fd_oracle(cdp_pipe)  # real-module positive control (no Chrome launched)

        def mutate(fn):
            children = [n for n in ast.walk(fn)
                        if isinstance(n, ast.FunctionDef) and n.name == "_child_fds"]
            if len(children) != 1:
                raise AssertionError("CdpPipe.start._child_fds anchor not unique")
            matches = [n for n in ast.walk(children[0])
                       if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                       and isinstance(n.func.value, ast.Name)
                       and (n.func.value.id, n.func.attr) == ("fcntl", "fcntl")
                       and len(n.args) == 3 and isinstance(n.args[0], ast.Name)
                       and n.args[0].id == "cmd_r"
                       and isinstance(n.args[1], ast.Attribute)
                       and n.args[1].attr == "F_DUPFD"
                       and isinstance(n.args[2], ast.Constant)
                       and n.args[2].value == 10]
            if len(matches) != 1:
                raise AssertionError("cmd_r F_DUPFD=10 anchor not unique")
            matches[0].args[2] = ast.Constant(value=11)

        for module in self._mutant("start", mutate):
            with self.assertRaisesRegex(AssertionError, "semantic FD 3/4 map mismatch"):
                self._fd_oracle(module)


if __name__ == "__main__":
    unittest.main()
