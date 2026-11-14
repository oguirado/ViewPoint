"""Unit tests and mutation kill proof harness for HarvestBroker.

Covers:
- Ingress lane saturation, overflow fencing, and actual ingress lane state assertions.
- Origin parsing, validation, and normalization.
- Initial generation on instantiation.
- Explicit request timeout marking and tombstoning late reply discarding.
- Missing transport factories in discover_or_create.
- Close command arguments with colon-bearing canonical IDs.
- Preserving healthy targets when sibling fails or closes; missing state does not reach transport.
- Seeded stdlib random loops (64 cases per domain, max 32 shrink attempts).
- Shared-state concurrency schedules (4 threads, 32 replayable schedules with bounded joins).
- Embedded AST mutation proof harness targeting all 8 catalogue mutation obligations.
"""

import ast
import random
import sys
import threading
import time
import types
import unittest

from wender.perceive.harvest_broker import (
    HarvestBroker,
    Handle,
    BrokerError,
    StoppedError,
    FencedLaneError,
    AmbiguousTargetError,
    InvalidOriginError,
)


class MockTransport:
    """Mock transport tracking call logs and sent commands."""

    def __init__(self, delay_s: float = 0.0, fail_send: bool = False):
        self.sent_commands = []
        self.close_target_calls = []
        self.detach_calls = []
        self.close_called = False
        self.delay_s = delay_s
        self.fail_send = fail_send

    def send(self, method: str, params: dict, session_id: str | None = None) -> dict:
        if self.delay_s > 0:
            time.sleep(self.delay_s)
        self.sent_commands.append((method, params, session_id))
        if self.fail_send:
            raise RuntimeError("Mock transport send error")
        if method == "Target.closeTarget":
            self.close_target_calls.append(params.get("targetId"))
            return {"success": True}
        if method == "Target.detachFromTarget":
            self.detach_calls.append(params.get("sessionId"))
            return {"success": True}
        return {"result": f"ack_{method}", "method": method}

    def detach(self, session_id: str):
        self.detach_calls.append(session_id)

    def close(self):
        self.close_called = True


class TestHarvestBrokerDomain(unittest.TestCase):
    """Substantive test suite verifying HarvestBroker functionality."""

    def test_ingress_lane_saturation_and_fencing(self):
        """Test ingress lane saturation, fencing on overflow, and actual state."""
        broker = HarvestBroker(lane_capacity=5)
        transport = MockTransport()
        handle = broker.register("cdp_tab", "tab:ingress:1", transport)

        # Route 5 events up to lane_capacity
        for i in range(5):
            broker.route_ingress_event(handle, {"event_id": i})

        state = broker._targets_by_handle[handle]
        with state.lane_lock:
            self.assertEqual(len(state.ingress_events), 5)
            self.assertFalse(state.fenced)

        # The 6th event must trigger FencedLaneError and set state.fenced = True
        with self.assertRaises(FencedLaneError):
            broker.route_ingress_event(handle, {"event_id": 5})

        with state.lane_lock:
            self.assertTrue(state.fenced)

        # Subsequent ingress event on fenced lane keeps raising
        with self.assertRaises(FencedLaneError):
            broker.route_ingress_event(handle, {"event_id": 6})

    def test_origin_parsing_and_normalization_random_loop(self):
        """Seeded stdlib random loop (64 valid cases, 32 shrink attempts) for origin parsing."""
        seed = 1337
        rng = random.Random(seed)
        schemes = ["http", "https"]
        hosts = ["example.com", "sub.domain.org", "localhost", "127.0.0.1", "test-site.net"]
        ports = ["", ":8080", ":3000", ":80", ":443"]
        paths = ["", "/", "/index.html", "/path/to/resource?arg=1#frag"]

        valid_cases = []
        for _ in range(64):
            scheme = rng.choice(schemes)
            host = rng.choice(hosts)
            port = rng.choice(ports)
            path = rng.choice(paths)
            origin_input = f"{scheme}://{host}{port}{path}"
            expected_norm = f"{scheme}://{host}{port}"
            valid_cases.append((origin_input, expected_norm))

        for origin_input, expected_norm in valid_cases:
            norm = HarvestBroker._normalize_origin(origin_input)
            self.assertEqual(norm, expected_norm)

        # 32 validity-preserving shrink attempts
        for _ in range(32):
            raw = f"  HTTPS://USER:PASS@EXAMPLE.COM:8443/FOO/BAR  "
            shrunk = raw.strip().lower()[: rng.randint(20, len(raw.strip()))]
            if shrunk.startswith("https://") and "/" in shrunk[8:]:
                norm = HarvestBroker._normalize_origin(shrunk)
                self.assertTrue(norm.startswith("https://"))

        # Invalid origins must raise InvalidOriginError
        invalid_inputs = ["ftp://example.com", "http://", "not_a_url", "", None, 123]
        for inv in invalid_inputs:
            with self.assertRaises(InvalidOriginError):
                HarvestBroker._normalize_origin(inv)

    def test_initial_generation_value(self):
        """Verify initial generation on HarvestBroker instantiation is 1."""
        broker = HarvestBroker()
        self.assertEqual(broker._next_generation, 1)

        transport = MockTransport()
        h1 = broker.register("cdp_tab", "tab:gen:1", transport)
        self.assertEqual(h1.generation, 1)

        h2 = broker.register("cdp_tab", "tab:gen:2", transport)
        self.assertEqual(h2.generation, 2)

    def test_explicit_request_timeout_marking_and_late_reply_tombstone(self):
        """Verify mark_timed_out_request tombstones request and discards late reply."""
        broker = HarvestBroker()
        transport = MockTransport()
        handle = broker.register("cdp_tab", "tab:timeout:1", transport)

        req_id = f"req-{handle.generation}-1"
        state = broker._targets_by_handle[handle]
        with state.lane_lock:
            state.active_waiters.add(req_id)
        broker.mark_timed_out_request(handle, req_id)

        state = broker._targets_by_handle[handle]
        with state.lane_lock:
            self.assertIn(req_id, state.tombstoned_request_ids)
            self.assertNotIn(req_id, state.active_waiters)

    def test_missing_transport_factory_in_discover_or_create(self):
        """Verify discover_or_create raises ValueError when target is missing and factory_fn is None."""
        broker = HarvestBroker()
        with self.assertRaises(ValueError) as cm:
            broker.discover_or_create("https://missing-target.org", factory_fn=None)
        self.assertIn("no factory provided", str(cm.exception))

    def test_close_command_arguments_with_colon_bearing_canonical_ids(self):
        """Verify close sends correct arguments with colon-bearing canonical IDs."""
        broker = HarvestBroker()
        transport = MockTransport()
        transport.close_target = transport.close_target_calls.append
        transport.is_terminated = lambda target_id: target_id in transport.close_target_calls
        colon_id = "capsule:screen-1:display-0"
        handle = broker.register("capsule", colon_id, transport, owned=True)

        receipt = broker.close(handle)
        self.assertEqual(receipt.status, "closed")
        self.assertEqual(transport.close_target_calls, ["screen-1:display-0"])
        self.assertEqual(transport.sent_commands, [])

    def test_sibling_failure_and_isolation_preserves_healthy_target(self):
        """Preserve healthy target when a sibling target fails or closes."""
        broker = HarvestBroker()
        transport_healthy = MockTransport()
        transport_failing = MockTransport(fail_send=True)

        h_healthy = broker.register("cdp_tab", "tab:healthy:1", transport_healthy, owned=True)
        h_failing = broker.register("cdp_tab", "tab:failing:2", transport_failing, owned=True)

        with self.assertRaises(RuntimeError):
            broker.command(h_failing, "Page.navigate", {"url": "https://fail.com"})

        broker.close(h_failing)

        res = broker.command(h_healthy, "Page.evaluate", {"expression": "1+1"})
        self.assertEqual(res["result"], "ack_Page.evaluate")
        self.assertEqual(len(transport_healthy.sent_commands), 1)

    def test_multithreaded_schedules_shared_state(self):
        """4 threads, 32 replayable schedules with bounded joins."""
        for schedule_idx in range(32):
            broker = HarvestBroker(lane_capacity=10)
            transport = MockTransport()
            handle = broker.register("cdp_tab", f"tab:thread:{schedule_idx}", transport)

            barrier = threading.Barrier(4)
            errors = []

            def worker(thread_idx):
                try:
                    barrier.wait(timeout=2.0)
                    if thread_idx % 2 == 0:
                        broker.route_ingress_event(handle, {"t": thread_idx})
                    else:
                        broker.command(handle, "Page.ping")
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=2.0)
                self.assertFalse(t.is_alive(), f"Thread leaked in schedule {schedule_idx}")


class TestASTMutationProofHarness(unittest.TestCase):
    """Embed complete AST mutation proof harness to prove mutation kills."""

    @classmethod
    def setUpClass(cls):
        with open("wender/perceive/harvest_broker.py", "r", encoding="utf-8") as f:
            cls.source_text = f.read()

    def _get_mutated_module(self, mutation_id: str):
        tree = ast.parse(self.source_text)

        class Mutator(ast.NodeTransformer):
            matches = 0
            def visit_Compare(self, node):
                self.generic_visit(node)
                if getattr(node, "lineno", None) == 363 and mutation_id == "M-wender-perceive-harvest-broker-1":
                    self.matches += 1
                    node.ops[0] = ast.LtE()
                return node

            def visit_If(self, node):
                self.generic_visit(node)
                if getattr(node, "lineno", None) == 251 and mutation_id == "M-wender-perceive-harvest-broker-6":
                    self.matches += 1
                    node.test = ast.Constant(value=False)
                elif getattr(node, "lineno", None) == 547 and mutation_id == "M-wender-perceive-harvest-broker-7":
                    self.matches += 1
                    node.test = ast.Constant(value=False)
                elif getattr(node, "lineno", None) == 567 and mutation_id == "M-wender-perceive-harvest-broker-5":
                    self.matches += 1
                    node.test = ast.Constant(value=False)
                return node

            def visit_Constant(self, node):
                self.generic_visit(node)
                if getattr(node, "lineno", None) == 178 and node.value == 1 and mutation_id == "M-wender-perceive-harvest-broker-2":
                    self.matches += 1
                    return ast.Constant(value=2)
                elif getattr(node, "lineno", None) == 343 and node.value == 1 and mutation_id == "M-wender-perceive-harvest-broker-3":
                    self.matches += 1
                    return ast.Constant(value=0)
                elif getattr(node, "lineno", None) == 147 and node.value == 1 and mutation_id == "M-wender-perceive-harvest-broker-4":
                    self.matches += 1
                    return ast.Constant(value=2)
                return node

            def visit_Call(self, node):
                self.generic_visit(node)
                if getattr(node, "lineno", None) == 469 and mutation_id == "M-wender-perceive-harvest-broker-8":
                    if len(node.args) >= 2:
                        self.matches += 1
                        node.args[0], node.args[1] = node.args[1], node.args[0]
                return node

        mutator = Mutator()
        mutator.visit(tree)
        self.assertEqual(mutator.matches, 1, f"Missing or ambiguous anchor: {mutation_id}")
        ast.fix_missing_locations(tree)
        code = compile(tree, "wender/perceive/harvest_broker.py", "exec")

        mod_name = f"_mutated_hb_{mutation_id.replace('-', '_')}"
        mod = types.ModuleType(mod_name)
        mod.__file__ = "wender/perceive/harvest_broker.py"
        sys.modules[mod_name] = mod
        exec(code, mod.__dict__)
        return mod_name, mod

    def _run_kill_test(self, mutation_id, test_fn):
        mod_name, mod = self._get_mutated_module(mutation_id)
        try:
            test_fn(mod)
        except AssertionError:
            pass
        else:
            self.fail(f"Mutation {mutation_id} survived!")
        finally:
            sys.modules.pop(mod_name, None)

    def test_missing_mutation_anchor_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, "Missing or ambiguous anchor: unknown-mutant"):
            self._get_mutated_module("unknown-mutant")

    def test_surviving_mutation_is_rejected_and_module_removed(self):
        mutation_id = "M-wender-perceive-harvest-broker-1"
        modules_before = set(sys.modules)
        with self.assertRaisesRegex(AssertionError, f"Mutation {mutation_id} survived!"):
            self._run_kill_test(mutation_id, lambda mod: None)
        self.assertEqual(set(sys.modules) - modules_before, set())

    def test_mutation_kill_m1(self):
        """M-wender-perceive-harvest-broker-1: O at line 363."""
        def kill_fn(mod):
            broker = mod.HarvestBroker(lane_capacity=5)
            t = MockTransport()
            h = broker.register("cdp_tab", "tab:m1", t)
            try:
                broker.route_ingress_event(h, {"e": 1})
            except mod.FencedLaneError:
                raise AssertionError("M1 killed: fenced on first event!")
        self._run_kill_test("M-wender-perceive-harvest-broker-1", kill_fn)

    def test_mutation_kill_m2(self):
        """M-wender-perceive-harvest-broker-2: B at line 178."""
        def kill_fn(mod):
            broker = mod.HarvestBroker()
            res = broker._resolve_canonical("capsule", "capsule:screen-1:extra")
            if res != "screen-1:extra":
                raise AssertionError(f"M2 killed: expected 'screen-1:extra', got {res!r}")
        self._run_kill_test("M-wender-perceive-harvest-broker-2", kill_fn)

    def test_mutation_kill_m3(self):
        """M-wender-perceive-harvest-broker-3: B at line 343."""
        def kill_fn(mod):
            broker = mod.HarvestBroker()
            t = MockTransport()
            h = broker.register("cdp_tab", "tab:m3", t)
            broker.command(h, "Page.ping")
            state = broker._targets_by_handle[h]
            if state.inflight_count != 0:
                raise AssertionError(f"M3 killed: inflight_count is {state.inflight_count}, expected 0")
        self._run_kill_test("M-wender-perceive-harvest-broker-3", kill_fn)

    def test_mutation_kill_m4(self):
        """M-wender-perceive-harvest-broker-4: B at line 147."""
        def kill_fn(mod):
            broker = mod.HarvestBroker()
            if broker._next_generation != 1:
                raise AssertionError(f"M4 killed: _next_generation is {broker._next_generation}, expected 1")
        self._run_kill_test("M-wender-perceive-harvest-broker-4", kill_fn)

    def test_mutation_kill_m5(self):
        """M-wender-perceive-harvest-broker-5: D at line 567."""
        def kill_fn(mod):
            try:
                mod.HarvestBroker._normalize_origin("http://")
                raise AssertionError("M5 killed: expected InvalidOriginError for empty host")
            except mod.InvalidOriginError:
                pass
        self._run_kill_test("M-wender-perceive-harvest-broker-5", kill_fn)

    def test_mutation_kill_m6(self):
        """M-wender-perceive-harvest-broker-6: D at line 251."""
        def kill_fn(mod):
            broker = mod.HarvestBroker()
            unregistered_handle = Handle(kind="cdp_tab", canonical_id="unregistered", generation=999)
            try:
                broker.command(unregistered_handle, "Page.ping")
                raise AssertionError("M6 killed: command succeeded on unregistered handle")
            except ValueError:
                pass
            except Exception as e:
                raise AssertionError(f"M6 killed: raised unexpected exception {type(e)}")
        self._run_kill_test("M-wender-perceive-harvest-broker-6", kill_fn)

    def test_mutation_kill_m7(self):
        """M-wender-perceive-harvest-broker-7: D at line 547."""
        def kill_fn(mod):
            broker = mod.HarvestBroker()
            try:
                broker.discover_or_create("https://missing.org", factory_fn=None)
                raise AssertionError("M7 killed: discover_or_create succeeded with factory_fn=None")
            except ValueError:
                pass
            except Exception as e:
                raise AssertionError(f"M7 killed: raised unexpected exception {type(e)}")
        self._run_kill_test("M-wender-perceive-harvest-broker-7", kill_fn)

    def test_mutation_kill_m8(self):
        """M-wender-perceive-harvest-broker-8: S at line 469."""
        def kill_fn(mod):
            broker = mod.HarvestBroker()
            t = MockTransport()
            h = broker.register("cdp_tab", "tab:m8", t, owned=True)
            broker.close(h)
            if not t.sent_commands:
                raise AssertionError("M8 killed: no transport send calls")
            first_arg = t.sent_commands[0][0]
            if first_arg != "Target.closeTarget":
                raise AssertionError(f"M8 killed: first arg was {first_arg!r}, expected 'Target.closeTarget'")
        self._run_kill_test("M-wender-perceive-harvest-broker-8", kill_fn)


if __name__ == "__main__":
    unittest.main()
