# @module tests.test_jq3_r_r1_j01_mutkill_godot_window_target
"""Test suite and AST mutation harness for GodotWindowTarget catalogue survivors."""

import ast
import copy
import hashlib
import random
import sys
import threading
import types
import unittest
import numpy as np

from wender.state import schema
from wender.perceive import godot_adapter as adapter
from wender.perceive.godot_window_target import (
    GodotWindowTarget, GodotProtocolError, GodotPrerequisiteError,
    Handle, FrameRef, Observation, _identity, _tick_from_state, _owned_frame,
)
from wender.act.godot_executor import GodotExecutor
from wender.perceive.session_registry import SessionRegistry
from tests.test_godot_t2_adapter import (
    _fixture_encode_batch, _fixture_decode_batches, _make_action_schemas,
    _make_valid_base_and_sidecar,
)


# @anchor wa.test.godot_window_target.make_target_helper
def make_target_helper(mod=None, *, name="window-1", registries=None, context=None, epoch=0):
    if mod is None:
        mod = sys.modules["wender.perceive.godot_window_target"]
    registry = SessionRegistry()
    handle = registry.register("godot_window", name, object(), owned=False)
    for _ in range(epoch):
        registry.advance_epoch(handle)
    chunks = []
    client = adapter.GodotT2Client(
        "/virtual/inbox", "/virtual/outbox", "fixture",
        decode_batches=_fixture_decode_batches,
        encode_batch=_fixture_encode_batch, sink=chunks.append)
    executor = GodotExecutor(
        {"principal": "offline", "channel": "t2", "slot_kind": "sandbox",
         "automation_active": False, **(context or {})},
        _make_action_schemas(), _fixture_encode_batch, chunks.append)
    regs = {"session_registry": registry, "epoch": epoch, "screens": {"portfolio_overview", "Holdings"},
            "cadences": {"tick", "quarterly"},
            "prerequisite_gate": lambda h, e: True}
    if registries:
        regs.update(registries)
    return mod.GodotWindowTarget.bind(handle, client, executor, registries=regs), chunks, client, executor


# @anchor wa.test.godot_window_target.valid_pair
def valid_pair(tick=12):
    base, sidecar = _make_valid_base_and_sidecar()
    base["nodes"][0]["states"]["tick"] = str(tick)
    sidecar["base_sha256"] = hashlib.sha256(schema.dumps(base).encode("utf-8")).hexdigest()
    return base, sidecar


# @anchor wa.test.godot_window_target.make_frame
def make_frame(mod, handle, epoch=0, frame_seq=1, width=10, height=10):
    rgb = np.zeros((height, width, 3), dtype=np.uint8)
    digest = hashlib.sha256(rgb.tobytes()).hexdigest()
    return mod.FrameRef(handle, epoch, frame_seq, width, height, rgb, digest)


# @anchor wa.test.godot_window_target.DummyFrame
class DummyFrame:
    def __init__(self, handle, height=0, width=10, epoch=0, frame_seq=1):
        self.handle = handle
        self.epoch = epoch
        self.frame_seq = frame_seq
        self.width = width
        self.height = height
        self.rgb = np.zeros((height, width, 3), dtype=np.uint8)
        self.digest = hashlib.sha256(self.rgb.tobytes()).hexdigest()


# @anchor wa.test.godot_window_target.DummyHandleGen0
class DummyHandleGen0:
    kind = "godot_window"
    canonical_id = "w8"
    generation = 0


# @anchor wa.test.godot_window_target.TestGodotWindowTargetControls
class TestGodotWindowTargetControls(unittest.TestCase):
    """Positive controls and property/concurrency tests for GodotWindowTarget."""

    def test_base_sidecar_pairing_committed_tick_and_basis(self):
        target, chunks, client, executor = make_target_helper()
        base, sidecar = valid_pair(15)
        f = make_frame(sys.modules["wender.perceive.godot_window_target"], target.handle, 0, 1)
        obs = target.perceive(_fixture_encode_batch([base, sidecar]), frame=f)
        self.assertEqual(obs.handle, target.handle)
        self.assertEqual(target.committed_tick, 15)
        self.assertEqual(target.basis_sha256, sidecar["base_sha256"])

    def test_absent_base_or_missing_dispatch_basis_fails_closed(self):
        target, _, _, _ = make_target_helper()
        with self.assertRaises(GodotPrerequisiteError):
            target.dispatch({"seq": target.executor.next_seq, "tick": 12}, action_id="act-1")

        target.committed_tick = 12
        target.basis_sha256 = None
        with self.assertRaises(GodotPrerequisiteError):
            target.dispatch({"seq": target.executor.next_seq, "tick": 12}, action_id="act-2")

    def test_compare_chosen_base_proof_with_committed_pair(self):
        base, sidecar = valid_pair(20)
        sha = sidecar["base_sha256"]
        mod = sys.modules["wender.perceive.godot_window_target"]
        target, _, _, _ = make_target_helper(registries={"frame_evidence": lambda f: (20, sha)})
        f = make_frame(mod, target.handle, 0, 1)
        target.perceive(_fixture_encode_batch([base, sidecar]), frame=f)
        self.assertTrue(target.visual_corroborated)

        target2, _, _, _ = make_target_helper(registries={"frame_evidence": lambda f: (19, sha)})
        f2 = make_frame(mod, target2.handle, 0, 1)
        target2.perceive(_fixture_encode_batch([base, sidecar]), frame=f2)
        self.assertFalse(target2.visual_corroborated)

    def test_fresh_bind_epoch_boundaries_and_immutable_owned_frames(self):
        target, _, _, _ = make_target_helper(epoch=3)
        self.assertEqual(target.epoch, 3)
        base, sidecar = valid_pair(10)
        mod = sys.modules["wender.perceive.godot_window_target"]
        f = make_frame(mod, target.handle, 3, 1, width=8, height=8)
        obs = target.perceive(_fixture_encode_batch([base, sidecar]), frame=f)
        self.assertFalse(obs.frame.rgb.flags.writeable)

    def test_seeded_stdlib_random_property_loops(self):
        seed = 42
        rng = random.Random(seed)
        mod = sys.modules["wender.perceive.godot_window_target"]
        cases_run = 0

        # Domain 1: FrameRef geometry and owned frame properties (64 valid cases)
        for i in range(64):
            width = rng.randint(1, 100)
            height = rng.randint(1, 100)
            seq = rng.randint(1, 1000)
            epoch = rng.randint(0, 10)
            handle = Handle("godot_window", f"win-{i}", rng.randint(1, 5))

            rgb = np.full((height, width, 3), rng.randint(0, 255), dtype=np.uint8)
            sha = hashlib.sha256(rgb.tobytes()).hexdigest()
            source = FrameRef(handle, epoch, seq, width, height, rgb, sha)

            owned = mod._owned_frame(source, handle, epoch, seq - 1)
            self.assertIsNotNone(owned)
            self.assertEqual(owned.width, width)
            self.assertEqual(owned.height, height)
            cases_run += 1

        # Domain 2: Ticks and state pairs (64 valid cases)
        for i in range(64):
            tick_val = rng.randint(0, 1000)
            base, sidecar = valid_pair(tick_val)
            extracted = mod._tick_from_state(base)
            self.assertEqual(extracted, tick_val)
            cases_run += 1

        self.assertEqual(cases_run, 128)

    def test_thread_shared_state_concurrency(self):
        target, chunks, client, executor = make_target_helper()
        base, sidecar = valid_pair(10)
        mod = sys.modules["wender.perceive.godot_window_target"]
        f = make_frame(mod, target.handle, 0, 1)
        target.perceive(_fixture_encode_batch([base, sidecar]), frame=f)

        barrier = threading.Barrier(4)
        errors = []

        def worker(thread_idx):
            try:
                barrier.wait(timeout=2.0)
                for _ in range(8):
                    seq = target.executor.next_seq
                    try:
                        target.dispatch({"seq": seq, "tick": 10}, action_id=f"t{thread_idx}")
                    except (GodotProtocolError, GodotPrerequisiteError):
                        pass
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()

        for t in threads:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "Leaked thread detected!")

        self.assertFalse(errors, f"Concurrency errors observed: {errors}")


# @anchor wa.test.godot_window_target.kill_M1
def kill_M1(mod):
    t, _, _, _ = make_target_helper(mod)
    try:
        t.perceive(b"")
    except mod.GodotPrerequisiteError:
        return
    except Exception as exc:
        raise AssertionError(f"M1 killed: perceive raised {type(exc).__name__} instead of GodotPrerequisiteError") from exc
    raise AssertionError("M1 killed: perceive did not raise GodotPrerequisiteError when _base is None")


# @anchor wa.test.godot_window_target.kill_M2
def kill_M2(mod):
    base, sidecar = valid_pair(10)
    t, _, _, _ = make_target_helper(mod, registries={
        "frame_evidence": lambda f: (10, sidecar["base_sha256"])})
    t.perceive(_fixture_encode_batch([base, sidecar]), frame=make_frame(mod, t.handle))
    t.basis_sha256 = None
    try:
        t.dispatch({"seq": t.executor.next_seq, "tick": 10}, action_id="a1")
    except mod.GodotPrerequisiteError as exc:
        if str(exc) != "target blocked by backlog or missing basis/tick":
            raise AssertionError(f"M2 wrong refusal: {exc}") from exc
        return
    except Exception as exc:
        raise AssertionError(f"M2 killed: dispatch raised {type(exc).__name__} instead of GodotPrerequisiteError") from exc
    raise AssertionError("M2 killed: dispatch did not raise GodotPrerequisiteError when basis_sha256 is None")


# @anchor wa.test.godot_window_target.kill_M3
def kill_M3(mod):
    base, sidecar = valid_pair(12)
    t, _, _, _ = make_target_helper(mod, registries={
        "frame_evidence": lambda f: (12, sidecar["base_sha256"])})
    frame = make_frame(mod, t.handle, 0, 1)
    t.perceive(_fixture_encode_batch([base, sidecar]), frame=frame)
    try:
        t.dispatch({"seq": t.executor.next_seq + 99, "tick": 12}, action_id="a1")
    except mod.GodotProtocolError:
        return
    except Exception as exc:
        raise AssertionError(f"M3 killed: dispatch raised {type(exc).__name__} instead of GodotProtocolError") from exc
    raise AssertionError("M3 killed: dispatch did not raise GodotProtocolError on stale sequence")


# @anchor wa.test.godot_window_target.kill_M4
def kill_M4(mod):
    base, sidecar = valid_pair(12)
    sha = sidecar["base_sha256"]
    t, _, _, _ = make_target_helper(mod, registries={"frame_evidence": lambda f: (12, sha)})
    f = make_frame(mod, t.handle, 0, 1)
    t.perceive(_fixture_encode_batch([base, sidecar]), frame=f)
    if not t.visual_corroborated:
        raise AssertionError("M4 killed: visual_corroborated is False")


# @anchor wa.test.godot_window_target.kill_M5
def kill_M5(mod):
    t, _, _, _ = make_target_helper(mod)
    dummy = DummyFrame(t.handle, height=0)
    try:
        mod._owned_frame(dummy, t.handle, 0, 0)
    except mod.GodotProtocolError:
        return
    except Exception as exc:
        raise AssertionError(f"M5 killed: _owned_frame raised {type(exc).__name__} instead of GodotProtocolError") from exc
    raise AssertionError("M5 killed: _owned_frame accepted height 0 without error")


# @anchor wa.test.godot_window_target.kill_M6
def kill_M6(mod):
    t, _, _, _ = make_target_helper(mod)
    t._inflight[1] = ("a1", 0, t.epoch, "sha")
    t.basis_sha256 = "sha"
    t._outcomes = [{"seq": 1, "requested_tick": 0, "applied_tick": 0, "state_sha256": "sha", "status": "accepted", "reason": None}]
    t._accept_outcomes()
    if t.committed_tick != 0:
        raise AssertionError(f"M6 killed: committed_tick is {t.committed_tick}, expected 0")


# @anchor wa.test.godot_window_target.kill_M7
def kill_M7(mod):
    registry = SessionRegistry()
    h = registry.register("godot_window", "w7", object(), owned=False)
    cl = adapter.GodotT2Client("/in", "/out", "f", decode_batches=_fixture_decode_batches, encode_batch=_fixture_encode_batch)
    ex = GodotExecutor({"principal": "off"}, _make_action_schemas(), _fixture_encode_batch, lambda x: None)
    try:
        t = mod.GodotWindowTarget.bind(h, cl, ex, registries={"session_registry": registry})
    except mod.GodotProtocolError as exc:
        raise AssertionError("M7 killed: valid default epoch refused") from exc
    if t.epoch != 0:
        raise AssertionError(f"M7 killed: default epoch is {t.epoch}, expected 0")


# @anchor wa.test.godot_window_target.kill_M8
def kill_M8(mod):
    dh = DummyHandleGen0()
    try:
        mod._identity(dh)
    except mod.GodotProtocolError:
        return
    raise AssertionError("M8 killed: generation zero was accepted")


# @anchor wa.test.godot_window_target.kill_M9
def kill_M9(mod):
    base, sidecar = valid_pair(12)
    bad_base = copy.deepcopy(base)
    bad_base["nodes"].append(copy.deepcopy(bad_base["nodes"][0]))
    bad_base["nodes"][1]["states"]["tick"] = "99"
    try:
        mod._tick_from_state(bad_base)
    except mod.GodotProtocolError:
        return
    except Exception as exc:
        raise AssertionError(f"M9 killed: _tick_from_state raised {type(exc).__name__} instead of GodotProtocolError") from exc
    raise AssertionError("M9 killed: _tick_from_state did not raise GodotProtocolError on conflicting ticks")


# @anchor wa.test.godot_window_target.kill_M10
def kill_M10(mod):
    registry = SessionRegistry()
    h = registry.register("godot_window", "w10", object(), owned=False)
    cl = adapter.GodotT2Client("/in", "/out", "f", decode_batches=_fixture_decode_batches, encode_batch=_fixture_encode_batch)
    ex = GodotExecutor({"principal": "off"}, _make_action_schemas(), _fixture_encode_batch, lambda x: None)
    try:
        mod.GodotWindowTarget.bind(h, cl, ex, registries={"session_registry": registry, "epoch": -1})
    except mod.GodotProtocolError:
        return
    except Exception as exc:
        raise AssertionError(f"M10 killed: bind raised {type(exc).__name__} instead of GodotProtocolError") from exc
    raise AssertionError("M10 killed: bind did not raise GodotProtocolError on negative epoch")


# @anchor wa.test.godot_window_target.kill_M11
def kill_M11(mod):
    base, sidecar = valid_pair(12)
    bad_schema_base = {"schema": "invalid_v99"}
    t, _, _, _ = make_target_helper(mod)
    try:
        t._verify_pair(bad_schema_base, sidecar)
    except mod.GodotProtocolError as exc:
        if not str(exc).startswith("base invalid:"):
            raise AssertionError(f"M11 killed: expected base invalid error, got {exc}") from exc
        return
    except Exception as exc:
        raise AssertionError(f"M11 killed: _verify_pair raised {type(exc).__name__}") from exc
    raise AssertionError("M11 killed: _verify_pair did not raise GodotProtocolError on invalid base schema")


# @anchor wa.test.godot_window_target.kill_M12
def kill_M12(mod):
    t, _, _, _ = make_target_helper(mod)
    try:
        t.perceive(b"")
    except mod.GodotPrerequisiteError:
        return
    except Exception as exc:
        raise AssertionError(f"M12 killed: perceive raised {type(exc).__name__} instead of GodotPrerequisiteError") from exc
    raise AssertionError("M12 killed: perceive did not raise GodotPrerequisiteError when _base is None")


# @anchor wa.test.godot_window_target.kill_M13
def kill_M13(mod):
    base, sidecar = valid_pair(12)
    t, _, _, _ = make_target_helper(mod)
    f = make_frame(mod, t.handle, 0, 1)
    try:
        t.perceive(_fixture_encode_batch([base, sidecar]), frame=f)
    except Exception as exc:
        raise AssertionError(f"M13 killed: perceive raised {type(exc).__name__}") from exc


# @anchor wa.test.godot_window_target.kill_M14
def kill_M14(mod):
    try:
        make_target_helper(mod)
    except Exception as exc:
        raise AssertionError(f"M14 killed: bind raised {type(exc).__name__}") from exc


# @anchor wa.test.godot_window_target.kill_M15
def kill_M15(mod):
    try:
        make_target_helper(mod)
    except Exception as exc:
        raise AssertionError(f"M15 killed: bind raised {type(exc).__name__}") from exc


# @anchor wa.test.godot_window_target.kill_M16
def kill_M16(mod):
    try:
        res = mod._identity(mod.Handle("godot_window", "w16", 1))
        if res != ("godot_window", "w16", 1):
            raise AssertionError("M16 killed: wrong identity")
    except Exception as exc:
        raise AssertionError(f"M16 killed: _identity raised {type(exc).__name__}") from exc


ALL_KILLS = {
    "M1": kill_M1, "M2": kill_M2, "M3": kill_M3, "M4": kill_M4,
    "M5": kill_M5, "M6": kill_M6, "M7": kill_M7, "M8": kill_M8,
    "M9": kill_M9, "M10": kill_M10, "M11": kill_M11, "M12": kill_M12,
    "M13": kill_M13, "M14": kill_M14, "M15": kill_M15, "M16": kill_M16,
}


# @anchor wa.test.godot_window_target.Mutator
class Mutator(ast.NodeTransformer):
    def __init__(self, mut_id):
        self.mut_id = mut_id
        self.applied = False

    def visit_Compare(self, node):
        self.generic_visit(node)
        lineno = getattr(node, "lineno", None)
        if self.mut_id == "M1" and lineno == 359 and isinstance(node.ops[0], ast.Is):
            node.ops[0] = ast.IsNot()
            self.applied = True
        elif self.mut_id == "M2" and lineno == 396 and isinstance(node.ops[0], ast.Is) and getattr(node.left, "attr", None) == "basis_sha256":
            node.ops[0] = ast.IsNot()
            self.applied = True
        elif self.mut_id == "M3" and lineno == 407 and isinstance(node.ops[0], ast.NotEq):
            node.ops[0] = ast.Eq()
            self.applied = True
        elif self.mut_id == "M4" and lineno == 365 and isinstance(node.ops[0], ast.Eq):
            node.ops[0] = ast.NotEq()
            self.applied = True
        elif self.mut_id == "M5" and lineno == 111 and isinstance(node.ops[0], ast.Gt) and getattr(node.left, "attr", None) == "height":
            node.ops[0] = ast.GtE()
            self.applied = True
        return node

    def visit_Constant(self, node):
        self.generic_visit(node)
        lineno = getattr(node, "lineno", None)
        if self.mut_id == "M6" and lineno == 308 and node.value == 0:
            node.value = 1
            self.applied = True
        elif self.mut_id == "M8" and lineno == 73 and node.value == 1:
            node.value = 0
            self.applied = True
        return node

    def visit_If(self, node):
        self.generic_visit(node)
        lineno = getattr(node, "lineno", None)
        if self.mut_id == "M9" and lineno == 97:
            node.test = ast.Constant(value=False)
            self.applied = True
        elif self.mut_id == "M10" and lineno == 155:
            node.test = ast.Constant(value=False)
            self.applied = True
        elif self.mut_id == "M11" and lineno == 85:
            node.test = ast.Constant(value=False)
            self.applied = True
        elif self.mut_id == "M12" and lineno == 359:
            node.test = ast.Constant(value=False)
            self.applied = True
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        lineno = getattr(node, "lineno", None)
        # The literal default 0 became active.epoch in the live-registration fence.
        # Keep M7's default-epoch corruption: increment that default by one.
        if (self.mut_id == "M7" and lineno == 154
                and getattr(node.func, "attr", None) == "get"
                and len(node.args) == 2
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "epoch"):
            node.args[1] = ast.BinOp(left=node.args[1], op=ast.Add(),
                                    right=ast.Constant(value=1))
            self.applied = True
        elif self.mut_id == "M13" and lineno == 318 and getattr(node.func, "id", None) == "isinstance":
            node.args[0], node.args[1] = node.args[1], node.args[0]
            self.applied = True
        elif self.mut_id == "M14" and lineno == 140 and getattr(node.func, "id", None) == "getattr":
            node.args[0], node.args[1] = node.args[1], node.args[0]
            self.applied = True
        elif self.mut_id == "M15" and lineno == 142 and getattr(node.func, "id", None) == "getattr":
            node.args[0], node.args[1] = node.args[1], node.args[0]
            self.applied = True
        elif self.mut_id == "M16" and lineno == 72 and getattr(node.func, "id", None) == "isinstance":
            node.args[0], node.args[1] = node.args[1], node.args[0]
            self.applied = True
        return node


# @anchor wa.test.godot_window_target.TestGodotWindowTargetMutationHarness
class TestGodotWindowTargetMutationHarness(unittest.TestCase):
    """AST prove harness executing and killing all assigned catalogue mutations."""

    def setUp(self):
        with open("wender/perceive/godot_window_target.py", "r", encoding="utf-8") as f:
            self.source = f.read()
        self.tree = ast.parse(self.source)
        self.real_mod = sys.modules["wender.perceive.godot_window_target"]

    def test_all_mutations_killed(self):
        # First verify positive control: all kill assertions must pass on unmutated real code
        for mut_id, kill_fn in ALL_KILLS.items():
            try:
                kill_fn(self.real_mod)
            except Exception as exc:
                self.fail(f"Baseline positive control for {mut_id} failed on real code: {exc}")

        # Execute in-memory AST mutations
        for mut_id, kill_fn in ALL_KILLS.items():
            mut_tree = copy.deepcopy(self.tree)
            mutator = Mutator(mut_id)
            mut_tree = mutator.visit(mut_tree)
            self.assertTrue(mutator.applied, f"Mutation anchor for {mut_id} missing or not applied!")
            ast.fix_missing_locations(mut_tree)
            code = compile(mut_tree, "wender/perceive/godot_window_target.py", "exec")

            mod_name = "wender.perceive.godot_window_target"
            saved_mod = sys.modules[mod_name]
            try:
                temp_mod = types.ModuleType(mod_name)
                sys.modules[mod_name] = temp_mod
                exec(code, temp_mod.__dict__)

                with self.assertRaises(AssertionError, msg=f"Mutation {mut_id} SURVIVED!"):
                    kill_fn(temp_mod)
            finally:
                sys.modules[mod_name] = saved_mod


if __name__ == "__main__":
    unittest.main()
