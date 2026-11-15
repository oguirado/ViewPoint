"""Target Lock Registry and CDP Pipe Mutation Testing and Verification Suite.

Tests target-lock integer validation, nonce widths, active record lookup, quarantine
reconciliation, invalid nonce types, CDP deframing, reply errors, closure, and navigation
errors. Includes seeded property loops, multi-threaded concurrency checks, and an in-memory
AST mutation prove harness for catalogue mutation obligations.
"""
import ast
import json
import os
import random
import secrets
import sys
import tempfile
import threading
import time
import types
import unittest
import unittest.mock

import wender.act.target_lock_registry as tlr_mod
from wender.act.target_lock_registry import (
    ActorLease,
    Handle,
    PrerequisiteAbsent,
    TargetBusy,
    TargetLockRefused,
    TargetLockRegistry,
    _integer,
)
import wender.perceive.cdp_pipe as cdp_mod
from wender.perceive.cdp_pipe import CdpError, CdpPipe, Deframer, frame


class _FakeRecord:
    def __init__(self, handle, closed=False):
        self.handle = handle
        self.closed = closed


class _FakeRegistry:
    def __init__(self, alias_map=None):
        self.records = {}
        self.alias_map = alias_map or {}

    def register(self, handle, closed=False):
        key = (handle.kind, handle.canonical_id)
        self.records[key] = _FakeRecord(handle, closed=closed)

    def get(self, handle):
        key = (handle.kind, handle.canonical_id)
        return self.records.get(key)

    def canonical_resource(self, handle):
        key = (handle.kind, handle.canonical_id)
        return self.alias_map.get(key, handle.canonical_id)


class TestTargetLockRegistry(unittest.TestCase):
    def test_integer_validation(self):
        self.assertEqual(_integer(0, "test_field"), 0)
        self.assertEqual(_integer(100, "test_field"), 100)

        for invalid in ["0", 1.5, None, [10], {"a": 1}]:
            with self.assertRaises(TargetLockRefused) as cm:
                _integer(invalid, "test_field")
            self.assertIn("must be a nonnegative integer", str(cm.exception))

        with self.assertRaises(TargetLockRefused) as cm:
            _integer(-1, "test_field")
        self.assertIn("must be a nonnegative integer", str(cm.exception))

    def test_nonce_widths_and_generation(self):
        reg = _FakeRegistry()
        h1 = Handle("cdp_tab", "tab_1", 1)
        reg.register(h1)
        registry_inst = TargetLockRegistry(reg, clock=lambda: 100)

        lease1 = registry_inst.acquire(h1, "owner1", expires_ns=200)
        self.assertEqual(len(lease1.nonce), 24)
        self.assertIsInstance(lease1.nonce, bytes)

        # Test token_bytes collision loop
        h2 = Handle("cdp_tab", "tab_2", 1)
        reg.register(h2)
        call_count = [0]
        orig_token_bytes = secrets.token_bytes

        def collision_token_bytes(n):
            call_count[0] += 1
            if call_count[0] == 1:
                return lease1.nonce  # collision on 1st attempt
            return orig_token_bytes(n)

        with unittest.mock.patch("secrets.token_bytes", side_effect=collision_token_bytes):
            lease2 = registry_inst.acquire(h2, "owner2", expires_ns=200)
            self.assertEqual(len(lease2.nonce), 24)
            self.assertNotEqual(lease1.nonce, lease2.nonce)
            self.assertGreaterEqual(call_count[0], 2)

    def test_active_record_lookup(self):
        reg = _FakeRegistry()
        h_unreg = Handle("cdp_tab", "unregistered_tab", 1)
        registry_inst = TargetLockRegistry(reg, clock=lambda: 100)

        with self.assertRaises(PrerequisiteAbsent) as cm:
            registry_inst.acquire(h_unreg, "owner1", expires_ns=200)
        self.assertIn("target has no current registry record", str(cm.exception))

        h_closed = Handle("cdp_tab", "closed_tab", 1)
        reg.register(h_closed, closed=True)
        with self.assertRaises(PrerequisiteAbsent) as cm:
            registry_inst.acquire(h_closed, "owner1", expires_ns=200)
        self.assertIn("target is closed", str(cm.exception))

    def test_quarantine_reconciliation(self):
        reg = _FakeRegistry()
        h1 = Handle("cdp_tab", "tab_1", 1)
        reg.register(h1)
        registry_inst = TargetLockRegistry(reg, clock=lambda: 100)

        lease1 = registry_inst.acquire(h1, "owner1", expires_ns=200)

        # Reconcile when not inflight must raise TargetLockRefused
        with self.assertRaises(TargetLockRefused) as cm:
            registry_inst.reconcile_quarantine(h1, acknowledged=False)
        self.assertIn("unsent lease must use nonce-fenced release", str(cm.exception))

        # Mark inflight
        registry_inst.mark_inflight(lease1)

        # Reconcile quarantine with acknowledged=False retains quarantine and returns None
        res_quarantine = registry_inst.reconcile_quarantine(h1, acknowledged=False)
        self.assertIsNone(res_quarantine)

        # Attempt to acquire quarantined resource fails with TargetBusy
        with self.assertRaises(TargetBusy):
            registry_inst.acquire(h1, "owner2", expires_ns=300)

    def test_invalid_nonce_types(self):
        reg = _FakeRegistry()
        h1 = Handle("cdp_tab", "tab_1", 1)
        reg.register(h1)
        registry_inst = TargetLockRegistry(reg, clock=lambda: 100)

        lease1 = registry_inst.acquire(h1, "owner1", expires_ns=200)

        object.__setattr__(lease1, "nonce", "not_bytes_type")
        fake_lease = lease1

        with self.assertRaises(TargetLockRefused) as cm:
            registry_inst.release(fake_lease)
        self.assertIn("missing or invalid issued nonce", str(cm.exception))

    def test_malformed_grant_does_not_unlock_sibling(self):
        reg = _FakeRegistry(alias_map={
            ("cdp_tab", "tab_a"): "shared_canonical_res",
            ("cdp_tab", "tab_b"): "shared_canonical_res",
        })
        ha = Handle("cdp_tab", "tab_a", 1)
        hb = Handle("cdp_tab", "tab_b", 1)
        reg.register(ha)
        reg.register(hb)

        registry_inst = TargetLockRegistry(reg, clock=lambda: 100)
        lease_a = registry_inst.acquire(ha, "owner_a", expires_ns=500)

        malformed_lease = types.SimpleNamespace(
            nonce=b"invalid_24_bytes_nonce_____",
            handle=ha,
            owner="owner_a",
            generation=1,
            expires_ns=500,
        )

        with self.assertRaises(TargetLockRefused):
            registry_inst.release(malformed_lease)

        # Sibling handle hb must STILL be locked by shared_canonical_res
        with self.assertRaises(TargetBusy) as cm:
            registry_inst.acquire(hb, "owner_b", expires_ns=500)
        self.assertIn("shared_canonical_res", str(cm.exception))


class TestCdpPipe(unittest.TestCase):
    def test_deframer_feed(self):
        deframer = Deframer()

        # Simple json frame
        frames = deframer.feed(b'{"id":1}\x00')
        self.assertEqual(frames, [{"id": 1}])

        # NUL byte prefix
        frames2 = deframer.feed(b'\x00{"id":2}\x00')
        self.assertEqual(frames2, [{"id": 2}])

        # Chunked feeding
        chunked = b'{"id":3,"method":"Page.enable"}\x00{"id":4}\x00'
        out = []
        for i in range(len(chunked)):
            out.extend(deframer.feed(chunked[i:i + 1]))
        self.assertEqual(out, [{"id": 3, "method": "Page.enable"}, {"id": 4}])

    def test_cdp_reply_errors(self):
        pipe = CdpPipe(chrome_bin="/bin/true")
        pipe._closed = False
        pipe._w = 5

        def fake_write_error(fd, data):
            msg = {"id": pipe._next_id, "error": {"message": "invalid command params"}}
            with pipe._cv:
                pipe._store_reply_locked(msg)
            return len(data)

        with unittest.mock.patch("os.write", side_effect=fake_write_error):
            with self.assertRaises(CdpError) as cm:
                pipe.send("Target.attach", timeout=1.0)
            self.assertIn("Target.attach: invalid command params", str(cm.exception))

    def test_cdp_pipe_closed(self):
        pipe = CdpPipe(chrome_bin="/bin/true")
        pipe._closed = True
        pipe._w = 5

        with unittest.mock.patch("os.write", return_value=10):
            with self.assertRaises(CdpError) as cm:
                pipe.send("Target.attach", timeout=1.0)
            self.assertIn("CDP connection is closed", str(cm.exception))

            with self.assertRaises(CdpError) as cm:
                pipe.wait_event("Page.loadEventFired", timeout=1.0)
            self.assertIn("browser closed transport waiting for Page.loadEventFired", str(cm.exception))

    def test_cdp_navigation_errors(self):
        pipe = CdpPipe(chrome_bin="/bin/true")

        def fake_send_error(method, params=None, session_id=None, timeout=None):
            if method == "Page.navigate":
                return {"errorText": "net::ERR_NAME_NOT_RESOLVED"}
            return {}

        pipe.send = fake_send_error

        with self.assertRaises(CdpError) as cm:
            pipe.navigate("session_1", "http://invalid.domain.test")
        self.assertIn("navigate failed: net::ERR_NAME_NOT_RESOLVED", str(cm.exception))

    def test_child_fds_descriptor_constants(self):
        pipe = CdpPipe(chrome_bin="/bin/true")
        pipe.running = False
        fcntl_calls = []

        def fake_fcntl(fd, cmd, arg):
            fcntl_calls.append((fd, cmd, arg))
            return fd + 100

        class FakePopen:
            def __init__(self, *args, **kwargs):
                self.pid = 9999
                if "preexec_fn" in kwargs and callable(kwargs["preexec_fn"]):
                    kwargs["preexec_fn"]()

        with unittest.mock.patch("os.pipe", side_effect=[(3, 4), (5, 6)]), \
             unittest.mock.patch("fcntl.fcntl", side_effect=fake_fcntl), \
             unittest.mock.patch("os.dup2"), \
             unittest.mock.patch("os.close"), \
             unittest.mock.patch("tempfile.mkdtemp", return_value="/tmp/test_dir"), \
             unittest.mock.patch("subprocess.Popen", side_effect=FakePopen), \
             unittest.mock.patch("threading.Thread"):
            pipe.start()
            descriptor_constants = [call[2] for call in fcntl_calls]
            self.assertEqual(descriptor_constants, [10, 10])


class TestPropertyAndConcurrency(unittest.TestCase):
    def test_seeded_random_properties(self):
        seed = 42981
        rng = random.Random(seed)
        cases_tested = 0

        for i in range(64):
            val = rng.randint(0, 1000000)
            res = _integer(val, f"field_{i}")
            self.assertEqual(res, val)

            # Test framing roundtrip
            msg = {"id": val, "key": f"val_{i}"}
            framed = frame(msg)
            self.assertTrue(framed.endswith(b"\0"))
            deframer = Deframer()
            parsed = deframer.feed(framed)
            self.assertEqual(parsed, [msg])
            cases_tested += 1

        self.assertEqual(cases_tested, 64)

    def test_concurrent_lock_acquires(self):
        reg = _FakeRegistry()
        handles = [Handle("cdp_tab", f"tab_thread_{i}", 1) for i in range(4)]
        for h in handles:
            reg.register(h)

        registry_inst = TargetLockRegistry(reg, clock=time.monotonic_ns)
        barrier = threading.Barrier(4)
        acquired_leases = []
        lock = threading.Lock()
        threads = []

        def worker(h, index):
            barrier.wait()
            lease = registry_inst.acquire(h, f"owner_{index}", expires_ns=time.monotonic_ns() + 100000000)
            with lock:
                acquired_leases.append(lease)

        for i in range(4):
            t = threading.Thread(target=worker, args=(handles[i], i))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "Leaked thread detected!")

        self.assertEqual(len(acquired_leases), 4)


class TestAstMutationKillHarness(unittest.TestCase):
    """In-memory AST prove harness for catalogue mutation obligations."""

    @classmethod
    def setUpClass(cls):
        with open(tlr_mod.__file__, "r", encoding="utf-8") as f:
            cls.tlr_source = f.read()
        with open(cdp_mod.__file__, "r", encoding="utf-8") as f:
            cls.cdp_source = f.read()

    def _apply_mutation(self, file_kind, transformer_func):
        source = self.tlr_source if file_kind == "tlr" else self.cdp_source
        file_path = tlr_mod.__file__ if file_kind == "tlr" else cdp_mod.__file__
        mod_name = tlr_mod.__name__ if file_kind == "tlr" else cdp_mod.__name__

        tree = ast.parse(source, file_path)
        transformer_func(tree)
        ast.fix_missing_locations(tree)
        code = compile(tree, file_path, "exec")

        orig_mod = sys.modules[mod_name]
        mut_mod = types.ModuleType(mod_name)
        mut_mod.__file__ = file_path

        sys.modules[mod_name] = mut_mod
        try:
            exec(code, mut_mod.__dict__)
            return mut_mod, orig_mod, mod_name
        except Exception:
            sys.modules[mod_name] = orig_mod
            raise

    def _run_mutation_test(self, file_kind, transformer_func, assertion_func):
        # 1. Run baseline positive control assertion on real unmutated module
        real_mod = tlr_mod if file_kind == "tlr" else cdp_mod
        assertion_func(real_mod)

        # 2. Run assertion against mutated module
        mut_mod, orig_mod, mod_name = self._apply_mutation(file_kind, transformer_func)
        try:
            try:
                assertion_func(mut_mod)
                killed = False
            except AssertionError:
                killed = True
            except Exception as e:
                raise AssertionError(f"Mutation raised non-AssertionError exception: {e}") from e

            self.assertTrue(killed, "Mutation survived assertion test!")
        finally:
            sys.modules[mod_name] = orig_mod

    # --- Mutation Transformers and Assertions ---

    # M1: M-wender-act-target-lock-registry-1
    # B at wender/act/target_lock_registry.py:71, _integer, node 'value is less than 0', selector ops element 0
    def test_kill_M_wender_act_target_lock_registry_1(self):
        def mutate(tree):
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "_integer":
                    for item in ast.walk(node):
                        if isinstance(item, ast.Compare) and isinstance(item.ops[0], ast.Lt):
                            item.ops[0] = ast.LtE()

        def assert_fn(mod):
            try:
                res = mod._integer(0, "test_field")
            except Exception as e:
                raise AssertionError(f"Expected 0, got exception: {e}") from e
            if res != 0:
                raise AssertionError(f"Expected 0, got {res}")

        self._run_mutation_test("tlr", mutate, assert_fn)

    # M2: M-wender-act-target-lock-registry-2
    # B at wender/act/target_lock_registry.py:174, TargetLockRegistry.acquire, unique secrets.token_bytes(24) argument (collision retry)
    def test_kill_M_wender_act_target_lock_registry_2(self):
        def mutate(tree):
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == "TargetLockRegistry":
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) and item.name == "acquire":
                            sites = [n for n in ast.walk(item)
                                     if isinstance(n, ast.Call)
                                     and isinstance(n.func, ast.Attribute)
                                     and isinstance(n.func.value, ast.Name)
                                     and n.func.value.id == "secrets"
                                     and n.func.attr == "token_bytes"
                                     and len(n.args) == 1
                                     and isinstance(n.args[0], ast.Constant)
                                     and n.args[0].value == 24]
                            assert len(sites) == 1, "unique token_bytes(24) anchor required"
                            c_node = sites[0].args[0]
                            c_node.value += 1

        def assert_fn(mod):
            reg = _FakeRegistry()
            h1 = mod.Handle("cdp_tab", "t1", 1)
            reg.register(h1)
            r1 = mod.TargetLockRegistry(reg, clock=lambda: 100)
            l1 = r1.acquire(h1, "o1", expires_ns=200)

            h2 = mod.Handle("cdp_tab", "t2", 1)
            reg.register(h2)
            called = [0]
            orig_tb = secrets.token_bytes

            def fake_tb(n):
                called[0] += 1
                if called[0] == 1:
                    return l1.nonce
                return orig_tb(n)

            with unittest.mock.patch("secrets.token_bytes", side_effect=fake_tb):
                l2 = r1.acquire(h2, "o2", expires_ns=200)
                if len(l2.nonce) != 24:
                    raise AssertionError(f"Expected nonce length 24, got {len(l2.nonce)}")

        self._run_mutation_test("tlr", mutate, assert_fn)

    # M3: M-wender-act-target-lock-registry-3
    # B at wender/act/target_lock_registry.py:174, TargetLockRegistry.acquire, unique secrets.token_bytes(24) argument (initial mint)
    def test_kill_M_wender_act_target_lock_registry_3(self):
        def mutate(tree):
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == "TargetLockRegistry":
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) and item.name == "acquire":
                            sites = [n for n in ast.walk(item)
                                     if isinstance(n, ast.Call)
                                     and isinstance(n.func, ast.Attribute)
                                     and isinstance(n.func.value, ast.Name)
                                     and n.func.value.id == "secrets"
                                     and n.func.attr == "token_bytes"
                                     and len(n.args) == 1
                                     and isinstance(n.args[0], ast.Constant)
                                     and n.args[0].value == 24]
                            assert len(sites) == 1, "unique token_bytes(24) anchor required"
                            c_node = sites[0].args[0]
                            c_node.value += 1

        def assert_fn(mod):
            reg = _FakeRegistry()
            h1 = mod.Handle("cdp_tab", "t1", 1)
            reg.register(h1)
            r1 = mod.TargetLockRegistry(reg, clock=lambda: 100)
            l1 = r1.acquire(h1, "o1", expires_ns=200)
            if len(l1.nonce) != 24:
                raise AssertionError(f"Expected nonce length 24, got {len(l1.nonce)}")

        self._run_mutation_test("tlr", mutate, assert_fn)

    # M4: M-wender-act-target-lock-registry-4
    # B at wender/act/target_lock_registry.py:71, _integer, node '0'
    def test_kill_M_wender_act_target_lock_registry_4(self):
        def mutate(tree):
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "_integer":
                    for item in ast.walk(node):
                        if isinstance(item, ast.Compare) and isinstance(item.ops[0], ast.Lt):
                            if isinstance(item.comparators[0], ast.Constant) and item.comparators[0].value == 0:
                                item.comparators[0].value += 1

        def assert_fn(mod):
            try:
                res = mod._integer(0, "test_field")
            except Exception as e:
                raise AssertionError(f"Expected 0, got exception: {e}") from e
            if res != 0:
                raise AssertionError(f"Expected 0, got {res}")

        self._run_mutation_test("tlr", mutate, assert_fn)

    # M5: M-wender-act-target-lock-registry-5
    # D at wender/act/target_lock_registry.py:119, TargetLockRegistry._record, node 'record is None'
    def test_kill_M_wender_act_target_lock_registry_5(self):
        def mutate(tree):
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == "TargetLockRegistry":
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) and item.name == "_record":
                            for stmt in ast.walk(item):
                                if isinstance(stmt, ast.If) and isinstance(stmt.test, ast.Compare):
                                    if getattr(stmt.test.left, "id", None) == "record":
                                        stmt.test = ast.Constant(value=False)

        def assert_fn(mod):
            reg = _FakeRegistry()
            h_unregistered = mod.Handle("cdp_tab", "t_unregistered", 1)
            tlr_inst = mod.TargetLockRegistry(reg, clock=lambda: 100)
            try:
                tlr_inst._record(h_unregistered)
                raise AssertionError("Expected PrerequisiteAbsent")
            except mod.PrerequisiteAbsent as e:
                if "target has no current registry record" not in str(e):
                    raise AssertionError(f"Expected 'target has no current registry record', got: {e}") from e

        self._run_mutation_test("tlr", mutate, assert_fn)

    # M6: M-wender-act-target-lock-registry-6
    # D at wender/act/target_lock_registry.py:220, TargetLockRegistry.reconcile_quarantine, node 'not claim.inflight'
    def test_kill_M_wender_act_target_lock_registry_6(self):
        def mutate(tree):
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == "TargetLockRegistry":
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) and item.name == "reconcile_quarantine":
                            for stmt in ast.walk(item):
                                if isinstance(stmt, ast.If) and isinstance(stmt.test, ast.UnaryOp) and isinstance(stmt.test.op, ast.Not):
                                    if isinstance(stmt.test.operand, ast.Attribute) and stmt.test.operand.attr == "inflight":
                                        stmt.test = ast.Constant(value=False)

        def assert_fn(mod):
            reg = _FakeRegistry()
            h1 = mod.Handle("cdp_tab", "t1", 1)
            reg.register(h1)
            r1 = mod.TargetLockRegistry(reg, clock=lambda: 100)
            r1.acquire(h1, "o1", expires_ns=200)

            try:
                r1.reconcile_quarantine(h1, acknowledged=False)
                raise AssertionError("Expected TargetLockRefused for unsent lease")
            except mod.TargetLockRefused as e:
                if "unsent lease must use nonce-fenced release" not in str(e):
                    raise AssertionError(f"Expected 'unsent lease must use nonce-fenced release', got {e}") from e

        self._run_mutation_test("tlr", mutate, assert_fn)

    # M7: M-wender-act-target-lock-registry-7
    # D at wender/act/target_lock_registry.py:201, TargetLockRegistry._minted_claim, node 'type(nonce) is not bytes'
    def test_kill_M_wender_act_target_lock_registry_7(self):
        def mutate(tree):
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == "TargetLockRegistry":
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) and item.name == "_minted_claim":
                            sites = [stmt for stmt in ast.walk(item)
                                     if isinstance(stmt, ast.If)
                                     and ast.unparse(stmt.test) == "type(nonce) is not bytes"]
                            assert len(sites) == 1, "unique nonce-type gate required"
                            sites[0].test = ast.Constant(value=False)

        def assert_fn(mod):
            reg = _FakeRegistry()
            h1 = mod.Handle("cdp_tab", "t1", 1)
            reg.register(h1)
            r1 = mod.TargetLockRegistry(reg, clock=lambda: 100)
            l1 = r1.acquire(h1, "o1", expires_ns=200)

            object.__setattr__(l1, "nonce", "not_bytes")
            fake_l = l1
            try:
                r1.release(fake_l)
                raise AssertionError("Expected TargetLockRefused for non-bytes nonce")
            except mod.TargetLockRefused as e:
                if "missing or invalid issued nonce" not in str(e):
                    raise AssertionError(f"Expected 'missing or invalid issued nonce', got {e}") from e

        self._run_mutation_test("tlr", mutate, assert_fn)

    # M8: M-wender-perceive-cdp-pipe-1
    # O at wender/perceive/cdp_pipe.py:631, CdpPipe.wait_event, node 'deadline_ns - self._now_ns()'
    def test_kill_M_wender_perceive_cdp_pipe_1(self):
        def mutate(tree):
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == "CdpPipe":
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) and item.name == "wait_event":
                            for stmt in ast.walk(item):
                                if isinstance(stmt, ast.BinOp) and isinstance(stmt.op, ast.Sub):
                                    if isinstance(stmt.left, ast.Name) and stmt.left.id == "deadline_ns":
                                        stmt.op = ast.Add()

        def assert_fn(mod):
            now_val = [100_000_000_000]

            def fake_clock():
                v = now_val[0]
                now_val[0] += 100_000_000_000
                return v

            pipe = mod.CdpPipe(chrome_bin="/bin/true", clock=fake_clock)
            pipe._closed = False
            pipe._cv.wait = unittest.mock.MagicMock(side_effect=AssertionError("_cv.wait was called unexpectedly"))

            try:
                pipe.wait_event("test_ev", timeout=5.0)
                raise AssertionError("Expected CdpError timeout")
            except mod.CdpError as e:
                if "timeout waiting for event" not in str(e):
                    raise AssertionError(f"Expected 'timeout waiting for event', got {e}") from e

        self._run_mutation_test("cdp", mutate, assert_fn)

    # M9: M-wender-perceive-cdp-pipe-2
    # B at wender/perceive/cdp_pipe.py:173, CdpPipe.start._child_fds, node '10', unique path body element 0.value.args element 2
    def test_kill_M_wender_perceive_cdp_pipe_2(self):
        def mutate(tree):
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "_child_fds":
                    c_node = node.body[0].value.args[2]
                    c_node.value += 1

        def assert_fn(mod):
            pipe = mod.CdpPipe(chrome_bin="/bin/true")
            pipe.running = False
            fcntl_args = []

            def fake_fcntl(fd, cmd, arg):
                fcntl_args.append((fd, cmd, arg))
                return fd + 100

            class FakePopen:
                def __init__(self, *args, **kwargs):
                    self.pid = 1234
                    if "preexec_fn" in kwargs and callable(kwargs["preexec_fn"]):
                        kwargs["preexec_fn"]()

            with unittest.mock.patch("os.pipe", side_effect=[(3, 4), (5, 6)]), \
                 unittest.mock.patch("fcntl.fcntl", side_effect=fake_fcntl), \
                 unittest.mock.patch("os.dup2"), \
                 unittest.mock.patch("os.close"), \
                 unittest.mock.patch("tempfile.mkdtemp", return_value="/tmp/test_dir"), \
                 unittest.mock.patch("subprocess.Popen", side_effect=FakePopen), \
                 unittest.mock.patch("threading.Thread"):
                pipe.start()
                arg2s = [a[2] for a in fcntl_args]
                if arg2s != [10, 10]:
                    raise AssertionError(f"Expected fcntl dup args [10, 10], got {arg2s}")

        self._run_mutation_test("cdp", mutate, assert_fn)

    # M10: M-wender-perceive-cdp-pipe-3
    # B at wender/perceive/cdp_pipe.py:44, Deframer.feed, node 'i is less than 0', selector ops element 0
    def test_kill_M_wender_perceive_cdp_pipe_3(self):
        def mutate(tree):
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == "Deframer":
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) and item.name == "feed":
                            for stmt in ast.walk(item):
                                if isinstance(stmt, ast.If) and isinstance(stmt.test, ast.Compare):
                                    if isinstance(stmt.test.left, ast.Name) and stmt.test.left.id == "i":
                                        stmt.test.ops[0] = ast.LtE()

        def assert_fn(mod):
            deframer = mod.Deframer()
            res = deframer.feed(b"\0{\"id\":1}\0")
            if res != [{"id": 1}]:
                raise AssertionError(f"Expected [{{\"id\": 1}}], got {res}")

        self._run_mutation_test("cdp", mutate, assert_fn)

    # M11: M-wender-perceive-cdp-pipe-4
    # D at wender/perceive/cdp_pipe.py:349, CdpPipe.send, node '"error" in reply'
    def test_kill_M_wender_perceive_cdp_pipe_4(self):
        def mutate(tree):
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == "CdpPipe":
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) and item.name == "send":
                            for stmt in ast.walk(item):
                                if isinstance(stmt, ast.If) and isinstance(stmt.test, ast.Compare):
                                    if isinstance(stmt.test.ops[0], ast.In):
                                        stmt.test = ast.Constant(value=False)

        def assert_fn(mod):
            pipe = mod.CdpPipe(chrome_bin="/bin/true")
            pipe._closed = False
            pipe._w = 5

            def fake_write(fd, data):
                msg = {"id": pipe._next_id, "error": {"message": "cmd failed"}}
                with pipe._cv:
                    pipe._store_reply_locked(msg)
                return len(data)

            with unittest.mock.patch("os.write", side_effect=fake_write):
                try:
                    pipe.send("TestMethod", timeout=1.0)
                    raise AssertionError("Expected CdpError for error reply")
                except mod.CdpError as e:
                    if "cmd failed" not in str(e):
                        raise AssertionError(f"Expected 'cmd failed' in exception, got {e}") from e

        self._run_mutation_test("cdp", mutate, assert_fn)

    # M12: M-wender-perceive-cdp-pipe-5
    # D at wender/perceive/cdp_pipe.py:339, CdpPipe.send, node 'self._closed'
    def test_kill_M_wender_perceive_cdp_pipe_5(self):
        def mutate(tree):
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == "CdpPipe":
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) and item.name == "send":
                            for stmt in ast.walk(item):
                                if isinstance(stmt, ast.If) and isinstance(stmt.test, ast.Attribute) and stmt.test.attr == "_closed":
                                    stmt.test = ast.Constant(value=False)

        def assert_fn(mod):
            pipe = mod.CdpPipe(chrome_bin="/bin/true")
            pipe._closed = True
            pipe._w = 5

            with unittest.mock.patch("os.write", return_value=10):
                try:
                    pipe.send("TestMethod", timeout=1.0)
                    raise AssertionError("Expected CdpError for closed pipe")
                except mod.CdpError as e:
                    if "CDP connection is closed" not in str(e):
                        raise AssertionError(f"Expected 'CDP connection is closed', got {e}") from e

        self._run_mutation_test("cdp", mutate, assert_fn)

    # M13: M-wender-perceive-cdp-pipe-6
    # D at wender/perceive/cdp_pipe.py:402, CdpPipe.navigate, node 'res.get("errorText")'
    def test_kill_M_wender_perceive_cdp_pipe_6(self):
        def mutate(tree):
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == "CdpPipe":
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) and item.name == "navigate":
                            for stmt in ast.walk(item):
                                if isinstance(stmt, ast.If) and isinstance(stmt.test, ast.Call):
                                    if isinstance(stmt.test.func, ast.Attribute) and stmt.test.func.attr == "get":
                                        stmt.test = ast.Constant(value=False)

        def assert_fn(mod):
            pipe = mod.CdpPipe(chrome_bin="/bin/true")
            pipe.send = lambda method, params=None, session_id=None, timeout=None: {"errorText": "net::ERR_FAILED"}

            try:
                pipe.navigate("session_1", "http://example.com")
                raise AssertionError("Expected CdpError for navigation error")
            except mod.CdpError as e:
                if "navigate failed" not in str(e):
                    raise AssertionError(f"Expected 'navigate failed', got {e}") from e

        self._run_mutation_test("cdp", mutate, assert_fn)


if __name__ == "__main__":
    unittest.main()
