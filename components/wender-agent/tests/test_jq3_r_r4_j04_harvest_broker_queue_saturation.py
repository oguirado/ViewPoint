import sys
import os
import ast
import random
import threading
import time
import unittest
from typing import Dict, Any, List, Optional

from wender.perceive.harvest_broker import (
    HarvestBroker,
    Handle,
    BrokerError,
    StoppedError,
    FencedLaneError,
)


class FakeYieldingTransport:
    """Fake transport that can hold in send until a yield event is set."""

    def __init__(self):
        self.in_send_event = threading.Event()
        self.yield_event = threading.Event()
        self.reentrant_broker: Optional[HarvestBroker] = None
        self.reentrant_handle: Optional[Handle] = None
        self.lock_held_during_reentrant: Optional[bool] = None

    def send(self, method: str, params: Optional[dict] = None, session_id: Optional[str] = None) -> dict:
        self.in_send_event.set()
        if not self.yield_event.is_set():
            self.yield_event.wait(timeout=2.0)

        if self.reentrant_broker and self.reentrant_handle:
            # Check if registry lock is held by current thread during transport call
            self.lock_held_during_reentrant = self.reentrant_broker._registry_lock._is_owned()

        return {"result": f"ok_{method}"}


class TestHarvestBrokerQueueSaturation(unittest.TestCase):
    """Unit tests for HarvestBroker ingress and command queue saturation."""

    def test_hold_fake_transport_and_saturate_command_queue(self):
        broker = HarvestBroker(lane_capacity=2)
        transport1 = FakeYieldingTransport()
        handle1 = broker.register("cdp_tab", "tab-canon-1", transport1, owned=True)

        res_thread1 = []
        res_thread2 = []

        def worker1():
            try:
                r = broker.command(handle1, "eval1")
                res_thread1.append(r)
            except Exception as e:
                res_thread1.append(e)

        def worker2():
            try:
                r = broker.command(handle1, "eval2")
                res_thread2.append(r)
            except Exception as e:
                res_thread2.append(e)

        t1 = threading.Thread(target=worker1)
        t2 = threading.Thread(target=worker2)

        t1.start()
        self.assertTrue(transport1.in_send_event.wait(timeout=2.0))

        # First command is blocked in send. Launch second command in thread to fill inflight capacity (2)
        t2.start()
        time.sleep(0.05)

        # 3rd command should overflow and fence lane immediately without calling transport send
        with self.assertRaises(FencedLaneError):
            broker.command(handle1, "eval3")

        # Unblock transport1 and join threads
        transport1.yield_event.set()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())

    def test_exact_lane_limit_ingress_events_and_overflow(self):
        broker = HarvestBroker(lane_capacity=3)
        transport = FakeYieldingTransport()
        transport.yield_event.set()
        handle = broker.register("cdp_tab", "tab-canon-1", transport, owned=True)

        # Inject exact lane-limit events (3 events)
        broker.route_ingress_event(handle, {"evt": 1})
        broker.route_ingress_event(handle, {"evt": 2})
        broker.route_ingress_event(handle, {"evt": 3})

        # 4th event should raise FencedLaneError and fence the lane
        with self.assertRaises(FencedLaneError):
            broker.route_ingress_event(handle, {"evt": 4})

        # Further ingress events on fenced lane keep raising
        with self.assertRaises(FencedLaneError):
            broker.route_ingress_event(handle, {"evt": 5})

    def test_close_sibling_and_drain_deterministically(self):
        broker = HarvestBroker(lane_capacity=5)
        t1 = FakeYieldingTransport()
        t2 = FakeYieldingTransport()
        t1.yield_event.set()
        t2.yield_event.set()

        h1 = broker.register("cdp_tab", "tab-canon-1", t1, owned=True)
        h2 = broker.register("cdp_tab", "tab-canon-2", t2, owned=True)

        broker.route_ingress_event(h1, {"e": 1})
        broker.route_ingress_event(h2, {"e": 2})

        # Close sibling h2
        broker.close(h2)

        # Ingress to retired handle h2 raises
        with self.assertRaises(StoppedError):
            broker.route_ingress_event(h2, {"e": 3})

        # Commands on closed handle h2 raise StoppedError
        with self.assertRaises(StoppedError):
            broker.command(h2, "ping")

        # h1 remains healthy and unaffected
        res = broker.command(h1, "ping")
        self.assertEqual(res, {"result": "ok_ping", "broker_request_id": "req-1-2"})

    def test_registry_metadata_not_held_across_reentrant_transport(self):
        broker = HarvestBroker(lane_capacity=5)
        t1 = FakeYieldingTransport()
        t1.yield_event.set()
        h1 = broker.register("cdp_tab", "tab-canon-1", t1, owned=True)

        t1.reentrant_broker = broker
        t1.reentrant_handle = h1

        broker.command(h1, "reentrant_test")

        self.assertIsNotNone(t1.lock_held_during_reentrant)
        self.assertFalse(t1.lock_held_during_reentrant, "Registry lock must not be held across transport I/O")

    def test_one_full_lane_cannot_corrupt_another_target(self):
        broker = HarvestBroker(lane_capacity=2)
        t1 = FakeYieldingTransport()
        t2 = FakeYieldingTransport()
        t1.yield_event.set()
        t2.yield_event.set()

        h1 = broker.register("cdp_tab", "tab-canon-1", t1, owned=True)
        h2 = broker.register("cdp_tab", "tab-canon-2", t2, owned=True)

        # Overflow h1 ingress lane
        broker.route_ingress_event(h1, {"e": 1})
        broker.route_ingress_event(h1, {"e": 2})
        with self.assertRaises(FencedLaneError):
            broker.route_ingress_event(h1, {"e": 3})

        # h1 command should fail because it's fenced
        with self.assertRaises(FencedLaneError):
            broker.command(h1, "test")

        # h2 is completely unaffected
        broker.route_ingress_event(h2, {"e": 1})
        res = broker.command(h2, "test")
        self.assertEqual(res, {"result": "ok_test", "broker_request_id": "req-2-1"})

    def test_unknown_or_closed_ingress_ignored(self):
        broker = HarvestBroker(lane_capacity=5)
        unknown_handle = Handle(kind="cdp_tab", canonical_id="unknown", generation=1)

        # Ingress to unknown handle is rejected without creating state
        with self.assertRaises(ValueError):
            broker.route_ingress_event(unknown_handle, {"evt": "ignored"})
        self.assertIsNone(broker.get_target_record(unknown_handle))

    def test_seeded_property_loops(self):
        """Property tests: 64 valid cases per domain with seed reporting."""
        seed_val = 1337
        rng = random.Random(seed_val)

        # Domain 1: Random ingress sequences with random limits
        cases_run = 0
        for i in range(64):
            capacity = rng.randint(1, 10)
            events_count = rng.randint(0, 15)
            broker = HarvestBroker(lane_capacity=capacity)
            t = FakeYieldingTransport()
            t.yield_event.set()
            h = broker.register("cdp_tab", f"canon-{i}", t, owned=True)

            overflow_occurred = False
            for e_idx in range(events_count):
                try:
                    broker.route_ingress_event(h, {"id": e_idx})
                except FencedLaneError:
                    overflow_occurred = True
                    break

            if events_count > capacity:
                self.assertTrue(overflow_occurred, f"Seed {seed_val}, case {i}: expected overflow for capacity {capacity}, events {events_count}")
            else:
                self.assertFalse(overflow_occurred, f"Seed {seed_val}, case {i}: unexpected overflow for capacity {capacity}, events {events_count}")
            cases_run += 1

        self.assertEqual(cases_run, 64)

    def test_multithreaded_schedules(self):
        """Multi-threaded schedule verification with barriers and controlled joins."""
        for schedule_id in range(32):
            broker = HarvestBroker(lane_capacity=4)
            t1 = FakeYieldingTransport()
            t1.yield_event.set()
            h1 = broker.register("cdp_tab", f"canon-mt-{schedule_id}", t1, owned=True)

            barrier = threading.Barrier(4)
            errors = []

            def worker_ingress(worker_id):
                try:
                    barrier.wait(timeout=2.0)
                    for k in range(2):
                        try:
                            broker.route_ingress_event(h1, {"w": worker_id, "k": k})
                        except FencedLaneError:
                            pass
                except Exception as ex:
                    errors.append(ex)

            threads = [threading.Thread(target=worker_ingress, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=2.0)
                self.assertFalse(t.is_alive(), f"Thread leaked in schedule {schedule_id}")

            self.assertEqual(len(errors), 0)


class ASTProveHarness(unittest.TestCase):
    """In-memory AST prove harness for catalogue mutation obligations."""

    @classmethod
    def setUpClass(cls):
        src_path = "wender/perceive/harvest_broker.py"
        with open(src_path, "r", encoding="utf-8") as f:
            cls.original_code = f.read()
        cls.original_ast = ast.parse(cls.original_code)

    def _apply_compare_O(self, node: ast.Compare, op_idx: int = 0) -> None:
        """O operator on Compare ops element."""
        mapping = {
            ast.Lt: ast.Gt,
            ast.Gt: ast.Lt,
            ast.LtE: ast.GtE,
            ast.GtE: ast.LtE,
            ast.Eq: ast.NotEq,
            ast.NotEq: ast.Eq,
            ast.Is: ast.IsNot,
            ast.IsNot: ast.Is,
            ast.In: ast.NotIn,
            ast.NotIn: ast.In,
        }
        current_op = type(node.ops[op_idx])
        if current_op in mapping:
            node.ops[op_idx] = mapping[current_op]()

    def _apply_constant_B(self, node: ast.Constant) -> None:
        """B operator on exact integer Constant: increments by 1."""
        if isinstance(node.value, int) and not isinstance(node.value, bool):
            node.value = node.value + 1

    def test_m_wender_perceive_harvest_broker_1_kill(self):
        """M-wender-perceive-harvest-broker-1 O at wender/perceive/harvest_broker.py:363,

        HarvestBroker.route_ingress_event, node 'len(state.ingress_events) is at least state.lane_limit', selector ops element 0.
        """
        # 1. Run positive control on real unmutated code
        broker = HarvestBroker(lane_capacity=2)
        t = FakeYieldingTransport()
        t.yield_event.set()
        h = broker.register("cdp_tab", "tab-1", t, owned=True)
        # Event 1 (len=0 < 2): succeeds
        broker.route_ingress_event(h, {"a": 1})
        # Event 2 (len=1 < 2): succeeds
        broker.route_ingress_event(h, {"a": 2})
        # Event 3 (len=2 >= 2): raises FencedLaneError
        with self.assertRaises(FencedLaneError):
            broker.route_ingress_event(h, {"a": 3})

        # 2. Apply mutation in-memory
        tree = ast.parse(self.original_code)
        target_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "route_ingress_event":
                for child in ast.walk(node):
                    if isinstance(child, ast.Compare) and getattr(child, "lineno", None) == 363:
                        target_node = child
                        break

        self.assertIsNotNone(target_node, "Missing anchor for M-wender-perceive-harvest-broker-1")
        self._apply_compare_O(target_node, 0)

        # Compile and execute mutated module in temporary namespace
        compiled = compile(tree, "<string>", "exec")
        mod_globals = {}
        exec(compiled, mod_globals)

        MutatedBroker = mod_globals["HarvestBroker"]
        MutatedFencedLaneError = mod_globals["FencedLaneError"]

        # 3. Assert kill on mutated code:
        # Mutated condition GtE -> LtE (<=).
        # On Event 1: len=0 <= 2 is True -> raises MutatedFencedLaneError on 1st event!
        # Healthy behavior expects Event 1 to succeed without error.
        m_broker = MutatedBroker(lane_capacity=2)
        m_t = FakeYieldingTransport()
        m_t.yield_event.set()
        m_h = m_broker.register("cdp_tab", "tab-1", m_t, owned=True)

        killed = False
        try:
            m_broker.route_ingress_event(m_h, {"a": 1})
        except MutatedFencedLaneError:
            killed = True

        self.assertTrue(killed, "M-wender-perceive-harvest-broker-1 mutation was not killed!")

    def test_m_wender_perceive_harvest_broker_2_kill(self):
        """M-wender-perceive-harvest-broker-2 B at wender/perceive/harvest_broker.py:178,

        HarvestBroker._resolve_canonical, node '1', unique structural path body element 2.body element 0.value.args element 1.
        """
        # 1. Run positive control on real unmutated code
        broker = HarvestBroker()
        res = broker._resolve_canonical("cdp_tab", "capsule:screen-1:extra")
        # split(":", 1) gives ["capsule", "screen-1:extra"], prefix="capsule", dest="screen-1:extra"
        self.assertEqual(res, "screen-1:extra")

        # 2. Mutate Constant(value=1) in _resolve_canonical split(":", 1) -> split(":", 2)
        tree = ast.parse(self.original_code)
        target_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_resolve_canonical":
                if len(node.body) > 2 and isinstance(node.body[2], ast.If):
                    if_node = node.body[2]
                    if len(if_node.body) > 0 and isinstance(if_node.body[0], ast.Assign):
                        assign_node = if_node.body[0]
                        if isinstance(assign_node.value, ast.Call) and len(assign_node.value.args) > 1:
                            target_node = assign_node.value.args[1]

        self.assertIsNotNone(target_node, "Missing anchor for M-wender-perceive-harvest-broker-2")
        self.assertIsInstance(target_node, ast.Constant)
        self.assertEqual(target_node.value, 1)

        self._apply_constant_B(target_node)
        self.assertEqual(target_node.value, 2)

        # Compile and test mutated code
        compiled = compile(tree, "<string>", "exec")
        mod_globals = {}
        exec(compiled, mod_globals)

        MutatedBroker = mod_globals["HarvestBroker"]
        m_broker = MutatedBroker()

        # With split(":", 2) on "capsule:screen-1:extra", parts = ["capsule", "screen-1", "extra"]
        # Dest assignment: prefix, dest = parts[0], parts[1] => prefix="capsule", dest="screen-1" (drops ":extra"!)
        m_res = m_broker._resolve_canonical("cdp_tab", "capsule:screen-1:extra")

        killed = (m_res != "screen-1:extra")
        self.assertTrue(killed, "M-wender-perceive-harvest-broker-2 mutation was not killed!")


if __name__ == "__main__":
    unittest.main()
