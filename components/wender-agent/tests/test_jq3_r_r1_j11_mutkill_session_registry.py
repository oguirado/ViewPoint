# @module test_jq3_r_r1_j11_mutkill_session_registry
# @anchor wa.package.unit.test_jq3_r_r1_j11_mutkill_session_registry
"""Unit tests and mutation kills for session_registry module."""

import ast
import copy
import hashlib
import os
import random
import sys
import threading
import types
import unittest
from unittest import mock

import numpy as np

import wender.perceive.session_registry as session_registry
from wender.perceive.session_registry import (
    ActionRequest,
    ActorLease,
    AliasConflictError,
    ChangeResult,
    FocusLease,
    FrameRef,
    FrameValidationError,
    Handle,
    Observation,
    PerceptionResult,
    QuarantinedTargetError,
    Receipt,
    RegistryError,
    SessionRegistry,
    StaleHandleError,
    StreamSpec,
    StreamToken,
    TargetRecord,
    Ticket,
    WindowDescription,
    WindowFrames,
)


class TestSessionRegistryMutkill(unittest.TestCase):
    # @anchor wa.package.unit.test_jq3_r_r1_j11_mutkill_session_registry.TestSessionRegistryMutkill.setUp
    def setUp(self) -> None:
        self.registry = SessionRegistry()
        self.transport = object()

    # @anchor wa.package.unit.test_jq3_r_r1_j11_mutkill_session_registry.TestSessionRegistryMutkill.test_canonical_session_registration
    def test_canonical_session_registration(self) -> None:
        h1 = self.registry.register("cdp_tab", "target-A", self.transport, owned=False)
        self.assertEqual(h1.kind, "cdp_tab")
        self.assertEqual(h1.canonical_id, "target-A")
        self.assertEqual(h1.generation, 1)

        rec = self.registry.resolve(h1)
        self.assertEqual(rec.handle, h1)
        self.assertIs(rec.transport, self.transport)
        self.assertFalse(rec.owned)
        self.assertEqual(rec.epoch, 0)
        self.assertIsNone(rec.session_id)

    # @anchor wa.package.unit.test_jq3_r_r1_j11_mutkill_session_registry.TestSessionRegistryMutkill.test_frameref_owned_rgb_bytes_and_digest
    def test_frameref_owned_rgb_bytes_and_digest(self) -> None:
        h = Handle("cdp_tab", "tab-1", 1)
        rgb = np.zeros((10, 20, 3), dtype=np.uint8)
        rgb[0, 0] = [255, 0, 0]

        fref = FrameRef.from_rgb(h, epoch=1, frame_seq=1, rgb=rgb)
        self.assertEqual(fref.width, 20)
        self.assertEqual(fref.height, 10)
        self.assertEqual(fref.rgb.shape, (10, 20, 3))
        self.assertFalse(fref.rgb.flags.writeable)

        # Digest mismatch must raise FrameValidationError
        bad_digest = "a" * 64
        with self.assertRaises(FrameValidationError):
            FrameRef(h, 1, 1, 20, 10, rgb, bad_digest)

    # @anchor wa.package.unit.test_jq3_r_r1_j11_mutkill_session_registry.TestSessionRegistryMutkill.test_attachment_updates
    def test_attachment_updates(self) -> None:
        h = self.registry.register("cdp_tab", "target-A", self.transport, owned=True)
        rec1 = self.registry.bind_attachment(h, "sess-1", owned=True)
        self.assertEqual(rec1.session_id, "sess-1")
        self.assertTrue(rec1.attachment_owned)

        # Idempotent re-binding with same params
        rec2 = self.registry.bind_attachment(h, "sess-1", owned=True)
        self.assertEqual(rec2.session_id, "sess-1")

        # Conflict on different session_id or owned flag
        with self.assertRaises(AliasConflictError):
            self.registry.bind_attachment(h, "sess-2", owned=True)

        with self.assertRaises(AliasConflictError):
            self.registry.bind_attachment(h, "sess-1", owned=False)

    # @anchor wa.package.unit.test_jq3_r_r1_j11_mutkill_session_registry.TestSessionRegistryMutkill.test_quarantined_reopen
    def test_quarantined_reopen(self) -> None:
        h1 = self.registry.register("cdp_tab", "target-A", self.transport, owned=True)
        self.registry.close(h1)

        # Re-register while quarantined must fail with QuarantinedTargetError
        with self.assertRaises(QuarantinedTargetError):
            self.registry.register("cdp_tab", "target-A", self.transport, owned=True)

        # Acknowledge cleanup removes from quarantine
        self.registry.acknowledge_cleanup(h1)
        h2 = self.registry.register("cdp_tab", "target-A", self.transport, owned=True)
        self.assertEqual(h2.generation, 2)

    # @anchor wa.package.unit.test_jq3_r_r1_j11_mutkill_session_registry.TestSessionRegistryMutkill.test_strict_action_request_payloads
    def test_strict_action_request_payloads(self) -> None:
        h = Handle("cdp_tab", "tab-1", 1)
        payload = {"key": "value", "nested": [1, 2, 3]}
        req = ActionRequest(h, "act-1", 1, payload, 10, 1000)

        # Deep-copy isolation check
        payload["key"] = "modified"
        self.assertEqual(req.payload["key"], "value")

        # Non-dict payload must raise RegistryError
        with self.assertRaises(RegistryError):
            ActionRequest(h, "act-1", 1, "not-a-dict", 10, 1000)

        # Invalid action_id string must raise RegistryError
        with self.assertRaises(RegistryError):
            ActionRequest(h, "", 1, {}, 10, 1000)

    # @anchor wa.package.unit.test_jq3_r_r1_j11_mutkill_session_registry.TestSessionRegistryMutkill.test_array_aliases_and_foreign_generations
    def test_array_aliases_and_foreign_generations(self) -> None:
        h1 = Handle("cdp_tab", "tab-1", 1)
        h_foreign = Handle("cdp_tab", "tab-1", 99)

        raw = np.array([[1, 2], [3, 4]], dtype=np.uint8)
        cres = ChangeResult(h1, 1, True, raw, {"op": 1})
        self.assertFalse(cres.claims.flags.writeable)
        raw[0, 0] = 99
        self.assertEqual(cres.claims[0, 0], 1)

        # Foreign handle lookup in active registry fails
        self.registry.register("cdp_tab", "tab-1", self.transport, owned=True)
        with self.assertRaises(StaleHandleError):
            self.registry.resolve(h_foreign)

    # @anchor wa.package.unit.test_jq3_r_r1_j11_mutkill_session_registry.TestSessionRegistryMutkill.test_streamspec_field_lookup
    def test_streamspec_field_lookup(self) -> None:
        spec = StreamSpec(width=100, height=200, slots=2, quota_bytes=1000000)
        self.assertEqual(spec.width, 100)
        self.assertEqual(spec.height, 200)

        with self.assertRaises(RegistryError):
            StreamSpec(width=0, height=200, slots=2, quota_bytes=1000000)

    # @anchor wa.package.unit.test_jq3_r_r1_j11_mutkill_session_registry.TestSessionRegistryMutkill.test_property_based_randomized_loops
    def test_property_based_randomized_loops(self) -> None:
        seed = 42
        rng = random.Random(seed)
        valid_kinds = list(session_registry.SUPPORTED_KINDS)

        for case_idx in range(64):
            kind = rng.choice(valid_kinds)
            canonical_id = f"target-{rng.randint(1, 10000)}"

            # Test handle creation
            h = Handle(kind, canonical_id, 1)
            self.assertEqual(h.kind, kind)
            self.assertEqual(h.canonical_id, canonical_id)

            # Test frame creation
            w = rng.randint(1, 32)
            h_dim = rng.randint(1, 32)
            rgb = np.zeros((h_dim, w, 3), dtype=np.uint8)
            fref = FrameRef.from_rgb(h, 1, case_idx + 1, rgb)
            self.assertEqual(fref.width, w)
            self.assertEqual(fref.height, h_dim)

    # @anchor wa.package.unit.test_jq3_r_r1_j11_mutkill_session_registry.TestSessionRegistryMutkill.test_multithreaded_concurrency
    def test_multithreaded_concurrency(self) -> None:
        barrier = threading.Barrier(4)
        start_event = threading.Event()
        errors = []

        def worker(worker_id: int):
            try:
                barrier.wait(timeout=2.0)
                start_event.wait(timeout=2.0)
                for i in range(10):
                    t = object()
                    h = self.registry.register("cdp_tab", f"t-{worker_id}-{i}", t, owned=True)
                    rec = self.registry.resolve(h)
                    self.assertIs(rec.transport, t)
                    self.registry.close(h)
                    self.registry.acknowledge_cleanup(h)
            except Exception as ex:
                errors.append(ex)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()

        start_event.set()
        for t in threads:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive())

        self.assertEqual(len(errors), 0, f"Thread errors: {errors}")


class TestMutationHarnessSessionRegistry(unittest.TestCase):
    # @anchor wa.package.unit.test_jq3_r_r1_j11_mutkill_session_registry.TestMutationHarnessSessionRegistry._apply_and_run_mutant
    def _apply_and_run_mutant(self, mutator, test_func, mod_name="wender.perceive.session_registry") -> None:
        source_path = mod_name.replace(".", "/") + ".py"
        with open(source_path, "r", encoding="utf-8") as f:
            source = f.read()

        tree = ast.parse(source)
        mutator(tree)
        ast.fix_missing_locations(tree)
        compiled_code = compile(tree, filename=source_path, mode="exec")

        old_mod = sys.modules.get(mod_name)
        try:
            mut_mod = types.ModuleType(mod_name)
            mut_mod.__file__ = os.path.abspath(source_path)
            mut_mod.__package__ = mod_name.rpartition(".")[0]
            sys.modules[mod_name] = mut_mod
            exec(compiled_code, mut_mod.__dict__)

            with self.assertRaises(AssertionError, msg="Mutant survived! Expected AssertionError kill."):
                test_func(mut_mod)
        finally:
            if old_mod is not None:
                sys.modules[mod_name] = old_mod
            else:
                sys.modules.pop(mod_name, None)

    # @anchor wa.package.unit.test_jq3_r_r1_j11_mutkill_session_registry.TestMutationHarnessSessionRegistry.test_m1_frameref_digest_kill
    def test_m1_frameref_digest_kill(self) -> None:
        # M-wender-perceive-session-registry-1 O at wender/state/handles.py:134
        def mutator(tree):
            class CompareTransformer(ast.NodeTransformer):
                def visit_Compare(self, node):
                    if getattr(node, "lineno", None) == 134 and isinstance(node.left, ast.Attribute) and node.left.attr == "digest":
                        node.ops = [ast.Eq() if isinstance(op, ast.NotEq) else op for op in node.ops]
                    return self.generic_visit(node)
            CompareTransformer().visit(tree)

        def runner(mod):
            h = mod.Handle("cdp_tab", "tab-1", 1)
            rgb = mod.np.zeros((10, 10, 3), dtype=mod.np.uint8)
            actual_digest = hashlib.sha256(rgb.tobytes()).hexdigest()
            try:
                mod.FrameRef(h, 1, 1, 10, 10, rgb, actual_digest)
            except mod.FrameValidationError:
                raise AssertionError("M1 kill: valid digest rejected under O Compare mutation")

        self._apply_and_run_mutant(mutator, runner, "wender.state.handles")

    # @anchor wa.package.unit.test_jq3_r_r1_j11_mutkill_session_registry.TestMutationHarnessSessionRegistry.test_m2_bind_attachment_kill
    def test_m2_bind_attachment_kill(self) -> None:
        # M-wender-perceive-session-registry-2 O at wender/perceive/session_registry.py:163
        def mutator(tree):
            for node in ast.walk(tree):
                if getattr(node, "lineno", None) == 163 and isinstance(node, ast.Compare) and isinstance(node.left, ast.Tuple):
                    node.ops[0] = ast.Eq()

        def runner(mod):
            reg = mod.SessionRegistry()
            h = reg.register("cdp_tab", "target-1", object(), owned=True)
            reg.bind_attachment(h, "sess-1", owned=True)
            try:
                reg.bind_attachment(h, "sess-1", owned=True)
            except mod.AliasConflictError:
                raise AssertionError("M2 kill: binding same attachment raised AliasConflictError under O Compare mutation")

        self._apply_and_run_mutant(mutator, runner)

    # @anchor wa.package.unit.test_jq3_r_r1_j11_mutkill_session_registry.TestMutationHarnessSessionRegistry.test_m3_from_rgb_validation_kill
    def test_m3_from_rgb_validation_kill(self) -> None:
        # M-wender-perceive-session-registry-3 D at wender/state/handles.py:149
        def mutator(tree):
            for node in ast.walk(tree):
                if getattr(node, "lineno", None) == 149 and isinstance(node, ast.If):
                    node.test = ast.Constant(value=False)

        def runner(mod):
            h = mod.Handle("cdp_tab", "tab-1", 1)
            bad_rgb = mod.np.zeros((10, 10, 4), dtype=mod.np.uint8)
            with mock.patch.object(mod, "_owned_array", side_effect=lambda x, **kw: x):
                try:
                    mod.FrameRef.from_rgb(h, 1, 1, bad_rgb)
                except mod.FrameValidationError:
                    return
            raise AssertionError("M3 kill: invalid RGB shape accepted by from_rgb under D If mutation")

        self._apply_and_run_mutant(mutator, runner, "wender.state.handles")

    # @anchor wa.package.unit.test_jq3_r_r1_j11_mutkill_session_registry.TestMutationHarnessSessionRegistry.test_m4_require_active_kill
    def test_m4_require_active_kill(self) -> None:
        # M-wender-perceive-session-registry-4 D at wender/perceive/session_registry.py:112
        def mutator(tree):
            for node in ast.walk(tree):
                if getattr(node, "lineno", None) == 112 and isinstance(node, ast.If):
                    node.test = ast.Constant(value=False)

        def runner(mod):
            reg = mod.SessionRegistry()
            bogus_h = mod.Handle("cdp_tab", "bogus", 1)
            try:
                reg.resolve(bogus_h)
            except mod.StaleHandleError:
                return
            raise AssertionError("M4 kill: resolve non-existent handle did not raise StaleHandleError under D If mutation")

        self._apply_and_run_mutant(mutator, runner)

    # @anchor wa.package.unit.test_jq3_r_r1_j11_mutkill_session_registry.TestMutationHarnessSessionRegistry.test_m5_register_quarantine_kill
    def test_m5_register_quarantine_kill(self) -> None:
        # M-wender-perceive-session-registry-5 D at wender/perceive/session_registry.py:63
        def mutator(tree):
            for node in ast.walk(tree):
                if getattr(node, "lineno", None) == 63 and isinstance(node, ast.If):
                    node.test = ast.Constant(value=False)

        def runner(mod):
            reg = mod.SessionRegistry()
            h = reg.register("cdp_tab", "target-1", object(), owned=True)
            reg.close(h)
            try:
                reg.register("cdp_tab", "target-1", object(), owned=True)
            except mod.QuarantinedTargetError:
                return
            raise AssertionError("M5 kill: register on quarantined target did not raise QuarantinedTargetError under D If mutation")

        self._apply_and_run_mutant(mutator, runner)

    # @anchor wa.package.unit.test_jq3_r_r1_j11_mutkill_session_registry.TestMutationHarnessSessionRegistry.test_m6_action_request_payload_kill
    def test_m6_action_request_payload_kill(self) -> None:
        # M-wender-perceive-session-registry-6 D at wender/state/handles.py:205
        def mutator(tree):
            for node in ast.walk(tree):
                if getattr(node, "lineno", None) == 205 and isinstance(node, ast.If):
                    node.test = ast.Constant(value=False)

        def runner(mod):
            h = mod.Handle("cdp_tab", "tab-1", 1)
            try:
                mod.ActionRequest(h, "act-1", 1, "not-a-dict", 1, 1000)
            except mod.RegistryError:
                return
            raise AssertionError("M6 kill: non-dict payload accepted under D If mutation")

        self._apply_and_run_mutant(mutator, runner, "wender.state.handles")

    # @anchor wa.package.unit.test_jq3_r_r1_j11_mutkill_session_registry.TestMutationHarnessSessionRegistry.test_m7_action_request_name_args_swap_kill
    def test_m7_action_request_name_args_swap_kill(self) -> None:
        # M-wender-perceive-session-registry-7 S at wender/state/handles.py:201
        def mutator(tree):
            for node in ast.walk(tree):
                if getattr(node, "lineno", None) == 201 and isinstance(node, ast.Call):
                    node.args[0], node.args[1] = node.args[1], node.args[0]

        def runner(mod):
            h = mod.Handle("cdp_tab", "tab-1", 1)
            try:
                mod.ActionRequest(h, "", 1, {}, 1, 1000)
            except mod.RegistryError:
                return
            raise AssertionError("M7 kill: empty action_id accepted under S Call args swap mutation")

        self._apply_and_run_mutant(mutator, runner, "wender.state.handles")

    # @anchor wa.package.unit.test_jq3_r_r1_j11_mutkill_session_registry.TestMutationHarnessSessionRegistry.test_m8_streamspec_getattr_args_swap_kill
    def test_m8_streamspec_getattr_args_swap_kill(self) -> None:
        # M-wender-perceive-session-registry-8 S at wender/state/handles.py:249
        def mutator(tree):
            for node in ast.walk(tree):
                if getattr(node, "lineno", None) == 249 and isinstance(node, ast.Call) and getattr(node.func, "id", None) == "getattr":
                    node.args[0], node.args[1] = node.args[1], node.args[0]

        def runner(mod):
            try:
                mod.StreamSpec(width=10, height=10, slots=1, quota_bytes=1000000)
            except (TypeError, mod.RegistryError):
                raise AssertionError("M8 kill: StreamSpec constructor failed under S getattr args swap mutation")

        self._apply_and_run_mutant(mutator, runner, "wender.state.handles")


if __name__ == "__main__":
    unittest.main()
