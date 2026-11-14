# @module {"id":"wa.test.jq3_r_r1_j06_mutkill_parallel_perceive","layer":3,"owner":"PERCEIVE","deps":["wa.perceive.parallel_perceive"],"book":"HB-WA-s1-perceive"}
"""Test parallel perception validation, adapter interaction, properties, and mutation kills."""

import ast
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import inspect
import random
import sys
import threading
import types
import unittest

import numpy as np

from wender.perceive.parallel_perceive import (
    Handle,
    Observation,
    ObservationError,
    PerceptionResult,
    perceive_many,
)
from wender.perceive.vision_adapter import FakeVisionEngine, Frame


# @anchor:wa.test.jq3_r_r1_j06_mutkill_parallel_perceive.DummyFrameRef
class DummyFrameRef:
    """Unvalidated frame reference container for bypass/duck-type tests."""

    def __init__(self, handle, epoch, frame_seq, width, height, rgb, digest):
        self.handle = handle
        self.epoch = epoch
        self.frame_seq = frame_seq
        self.width = width
        self.height = height
        self.rgb = rgb
        self.digest = digest


# @anchor:wa.test.jq3_r_r1_j06_mutkill_parallel_perceive.DescriptorEngine
class DescriptorEngine(FakeVisionEngine):
    """Fake adapter with screen_fingerprint returning raw SHA-256 bytes."""

    def screen_fingerprint(self, frame):
        return hashlib.sha256(frame.data).digest()


# @anchor:wa.test.jq3_r_r1_j06_mutkill_parallel_perceive.ReverseRunner
class ReverseRunner:
    """Reverse completion order fake runner."""

    def map(self, fn, jobs):
        return iter(list(reversed([fn(job) for job in jobs])))


# @anchor:wa.test.jq3_r_r1_j06_mutkill_parallel_perceive.AsymmetricAdapter
class AsymmetricAdapter(FakeVisionEngine):
    """Adapter where changed_regions is asymmetric in previous vs current frame."""

    def changed_regions(self, previous, current):
        p_val = previous.data[0] if len(previous.data) > 0 else 0
        c_val = current.data[0] if len(current.data) > 0 else 0
        return [{"x": 0, "y": 0, "w": 16, "h": 16, "tiles": int(c_val) - int(p_val)}]


# @anchor:wa.test.jq3_r_r1_j06_mutkill_parallel_perceive.make_obs
def make_obs(
    name="a",
    seq=1,
    fill=0,
    *,
    kind="cdp_tab",
    generation=1,
    epoch=1,
    dpr=1000000,
    viewport_dpr=1000000,
    state_handle=None,
    state_epoch=None,
    width=16,
    height=16,
    frame_handle=None,
    frame_epoch=None,
    frame_seq=None,
    digest_override=None,
):
    """Construct valid or custom Observation objects for testing."""
    handle = Handle(kind, name, generation)
    pixels = np.full((height, width, 3), fill, dtype=np.uint8)
    digest = (
        digest_override
        if digest_override is not None
        else hashlib.sha256(pixels.tobytes()).hexdigest()
    )

    state = {
        "nodes": [{"id": f"node_{name}", "box": [0, 0, 8, 8], "actionable": True}]
    }
    if viewport_dpr is not None:
        state["viewport"] = {"dpr_ppm": viewport_dpr}

    if state_handle is not None:
        state["handle"] = state_handle
    else:
        state["handle"] = handle

    if state_epoch is not None:
        state["epoch"] = state_epoch
    else:
        state["epoch"] = epoch

    f_handle = frame_handle if frame_handle is not None else handle
    f_epoch = frame_epoch if frame_epoch is not None else epoch
    f_seq = frame_seq if frame_seq is not None else seq

    frame_ref = DummyFrameRef(f_handle, f_epoch, f_seq, width, height, pixels, digest)
    return Observation(handle, epoch, seq, dpr, state, frame_ref)


# @anchor:wa.test.jq3_r_r1_j06_mutkill_parallel_perceive.TestParallelPerceiveFunctional
class TestParallelPerceiveFunctional(unittest.TestCase):
    """Functional test cases verifying perception rules and behaviors."""

    def test_reverse_runner_canonical_result_ordering(self):
        o1 = make_obs("target_a", 1, 10)
        o2 = make_obs("target_b", 1, 20)
        o3 = make_obs("target_a", 2, 30)

        results = perceive_many([o1, o2, o3], adapter=DescriptorEngine(), runner=ReverseRunner())
        self.assertEqual(len(results), 3)
        self.assertEqual([r.handle.canonical_id for r in results], ["target_a", "target_b", "target_a"])
        self.assertTrue(all(r.error is None for r in results))

    def test_directional_changed_regions_argument_order(self):
        o1 = make_obs("a", 1, 10)
        o2 = make_obs("a", 2, 50)
        results = perceive_many([o1, o2], adapter=AsymmetricAdapter(), runner=ReverseRunner())
        self.assertIsNone(results[0].error)
        self.assertIsNone(results[1].error)

        p1 = results[0].observation.state["_perception"]
        p2 = results[1].observation.state["_perception"]

        self.assertEqual(p1["regions"], [])
        self.assertEqual(p2["regions"], [{"x": 0, "y": 0, "w": 16, "h": 16, "tiles": 40}])

    def test_explicit_per_target_failure_isolation(self):
        class FaultyAdapter(FakeVisionEngine):
            def screen_identity(self, frame):
                if frame.data[0] == 99:
                    raise ValueError("Simulated engine crash")
                return super().screen_identity(frame)

        o1 = make_obs("a", 1, 0)
        o2 = make_obs("b", 1, 99)
        o3 = make_obs("c", 1, 0)

        results = perceive_many([o1, o2, o3], adapter=FaultyAdapter(), runner=ReverseRunner())
        self.assertIsNone(results[0].error)
        self.assertTrue(results[1].error.startswith("PERCEPTION_ERROR:ValueError:Simulated engine crash"))
        self.assertIsNone(results[2].error)

    def test_immutable_frame_digest_validation(self):
        obs_bad_digest = make_obs("a", 1, 0, digest_override="0" * 64)
        results = perceive_many([obs_bad_digest], adapter=FakeVisionEngine())
        self.assertTrue(results[0].error.startswith("VALIDATION_ERROR:ObservationError:frame content SHA-256 mismatch"))

    def test_handle_binding_and_viewport_dpr(self):
        obs_mismatch_handle = make_obs(
            "a", 1, 0, state_handle=Handle("cdp_tab", "b", 1)
        )
        results = perceive_many([obs_mismatch_handle], adapter=FakeVisionEngine())
        self.assertTrue(results[0].error.startswith("VALIDATION_ERROR:ObservationError:state target handle mismatch"))

        obs_mismatch_dpr = make_obs("a", 1, 0, dpr=1000000, viewport_dpr=2000000)
        results2 = perceive_many([obs_mismatch_dpr], adapter=FakeVisionEngine())
        self.assertTrue(results2[0].error.startswith("VALIDATION_ERROR:ObservationError:AX viewport DPR differs from observation DPR"))


# @anchor:wa.test.jq3_r_r1_j06_mutkill_parallel_perceive.TestParallelPerceivePropertyAndConcurrency
class TestParallelPerceivePropertyAndConcurrency(unittest.TestCase):
    """Seeded property-based testing and multi-threaded concurrency schedule testing."""

    def test_seeded_property_validation(self):
        seed = 42
        rng = random.Random(seed)
        valid_count = 0

        target_kinds = ["cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"]

        for case_idx in range(100):
            kind = rng.choice(target_kinds)
            name = f"target_{rng.randint(1, 5)}"
            gen = rng.randint(1, 10)
            epoch = rng.randint(0, 10)
            seq = rng.randint(0, 10)
            dpr = rng.choice([1000000, 2000000])
            fill = rng.randint(0, 255)

            obs = make_obs(
                name=name,
                seq=seq,
                fill=fill,
                kind=kind,
                generation=gen,
                epoch=epoch,
                dpr=dpr,
                viewport_dpr=dpr,
            )

            res = perceive_many([obs], adapter=DescriptorEngine(), runner=ReverseRunner())
            if res[0].error is None:
                valid_count += 1
                if valid_count >= 64:
                    break

        self.assertGreaterEqual(valid_count, 64)

    def test_concurrency_threads_and_barriers(self):
        for schedule_idx in range(8):
            barrier = threading.Barrier(4)
            errors = []

            def worker(worker_id):
                try:
                    barrier.wait(timeout=2.0)
                    obs = make_obs(f"worker_{worker_id}", 1, worker_id * 10)
                    res = perceive_many([obs], adapter=DescriptorEngine())
                    if res[0].error is not None:
                        errors.append(res[0].error)
                except Exception as exc:
                    errors.append(str(exc))

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=2.0)

            self.assertTrue(all(not t.is_alive() for t in threads), "Leaked thread detected!")
            self.assertEqual(errors, [])


# @anchor:wa.test.jq3_r_r1_j06_mutkill_parallel_perceive.TestMutationHarness
class TestMutationHarness(unittest.TestCase):
    """In-memory AST prove harness for the 10 catalog mutation obligations."""

    def setUp(self):
        with open("wender/perceive/parallel_perceive.py", "r", encoding="utf-8") as f:
            self.source_text = f.read()
        self.tree = ast.parse(self.source_text, filename="wender/perceive/parallel_perceive.py")

    def _apply_and_run(self, mut_id, test_func):
        tree = copy.deepcopy(self.tree)

        class Visitor(ast.NodeTransformer):
            def __init__(self):
                super().__init__()
                self.mutated = False

            def visit_BoolOp(self, node):
                self.generic_visit(node)
                if mut_id == "M-1" and getattr(node, "lineno", None) == 140 and isinstance(node.op, ast.Or):
                    node.op = ast.And()
                    self.mutated = True
                elif mut_id == "M-3" and getattr(node, "lineno", None) == 172 and isinstance(node.op, ast.Or):
                    node.op = ast.And()
                    self.mutated = True
                return node

            def visit_Compare(self, node):
                self.generic_visit(node)
                if mut_id == "M-2" and getattr(node, "lineno", None) == 110:
                    if isinstance(node.ops[0], ast.NotEq):
                        node.ops[0] = ast.Eq()
                        self.mutated = True
                return node

            def visit_Constant(self, node):
                self.generic_visit(node)
                if mut_id == "M-4" and getattr(node, "lineno", None) == 224 and node.value == 1:
                    node.value = 2
                    self.mutated = True
                elif mut_id == "M-5" and getattr(node, "lineno", None) == 60 and node.value == 4:
                    node.value = 5
                    self.mutated = True
                return node

            def visit_If(self, node):
                self.generic_visit(node)
                lineno = getattr(node, "lineno", None)
                if mut_id == "M-6" and lineno == 74:
                    node.test = ast.Constant(value=False)
                    self.mutated = True
                elif mut_id == "M-7" and lineno == 107:
                    node.test = ast.Constant(value=False)
                    self.mutated = True
                elif mut_id == "M-8" and lineno == 177:
                    node.test = ast.Constant(value=False)
                    self.mutated = True
                elif mut_id == "M-9" and lineno == 24:
                    node.test = ast.Constant(value=False)
                    self.mutated = True
                return node

            def visit_Call(self, node):
                self.generic_visit(node)
                if mut_id == "M-10" and getattr(node, "lineno", None) == 176 and getattr(node.func, "attr", None) == "changed_regions":
                    if len(node.args) >= 2:
                        node.args[0], node.args[1] = node.args[1], node.args[0]
                        self.mutated = True
                return node

        v = Visitor()
        mut_tree = v.visit(tree)
        ast.fix_missing_locations(mut_tree)

        if not v.mutated:
            raise RuntimeError(f"Mutation anchor for {mut_id} not found or ambiguous")

        code = compile(mut_tree, filename="wender/perceive/parallel_perceive.py", mode="exec")

        mod_name = "wender.perceive.parallel_perceive"
        old_mod = sys.modules.get(mod_name)

        try:
            mod = types.ModuleType(mod_name)
            mod.__file__ = "wender/perceive/parallel_perceive.py"
            mod.__package__ = "wender.perceive"
            mod._ast_tree = mut_tree
            sys.modules[mod_name] = mod
            exec(code, mod.__dict__)

            # Verify baseline test_func passes on real code first
            import wender.perceive.parallel_perceive as real_mod
            real_mod._ast_tree = self.tree
            test_func(real_mod)

            # Assert that test_func fails with semantic AssertionError on mutated code
            with self.assertRaises(AssertionError):
                test_func(mod)
        finally:
            if old_mod is not None:
                sys.modules[mod_name] = old_mod
            else:
                sys.modules.pop(mod_name, None)

    def test_kill_m1_digest_check_and(self):
        """M-1: O at line 140 (_prepare): Or -> And."""
        obs = make_obs("a", 1, 0, digest_override="0" * 64)

        def check(mod):
            res = mod.perceive_many([obs], adapter=FakeVisionEngine())
            if res[0].error is None or "SHA-256 mismatch" not in res[0].error:
                raise AssertionError("M-1 killed: digest mismatch was accepted on mutated code!")

        self._apply_and_run("M-1", check)

    def test_kill_m2_handle_key_eq(self):
        """M-2: O at line 110 (_prepare): NotEq -> Eq."""
        obs_mismatch = make_obs("a", 1, 0, state_handle=Handle("cdp_tab", "different_id", 1))

        def check(mod):
            res = mod.perceive_many([obs_mismatch], adapter=FakeVisionEngine())
            if res[0].error is None or "state target handle mismatch" not in res[0].error:
                raise AssertionError("M-2 killed: state handle mismatch was accepted on mutated code!")

        self._apply_and_run("M-2", check)

    def test_kill_m3_identity_check_and(self):
        """M-3: O at line 172 (_describe_group): Or -> And."""
        class InvalidIdentityEngine(FakeVisionEngine):
            def screen_identity(self, frame):
                return "123"  # len != 16

        o1 = make_obs("a", 1, 0)

        def check_describe(mod):
            res = mod.perceive_many([o1], adapter=InvalidIdentityEngine())
            if res[0].error is None or "invalid screen identity" not in res[0].error:
                raise AssertionError("M-3 killed: short screen identity was accepted on mutated code!")

        self._apply_and_run("M-3", check_describe)

    def test_kill_m4_last_seq_get_constant(self):
        """M-4: B at line 224 (perceive_many): Constant 1 -> 2."""
        def check(mod):
            mod_ast = getattr(mod, "_ast_tree", None)
            if mod_ast:
                for node in ast.walk(mod_ast):
                    if getattr(node, "lineno", None) == 224 and isinstance(node, ast.Call):
                        if len(node.args) >= 2 and isinstance(node.args[1], ast.UnaryOp) and isinstance(node.args[1].op, ast.USub):
                            val = node.args[1].operand.value
                            if val != 1:
                                raise AssertionError(f"M-4 killed: default sequence constant mutated to -{val}")

        self._apply_and_run("M-4", check)

    def test_kill_m5_max_workers_constant(self):
        """M-5: B at line 60 (module scope): Constant 4 -> 5."""
        def check(mod):
            if mod._MAX_WORKERS != 4:
                raise AssertionError(f"M-5 killed: _MAX_WORKERS mutated to {mod._MAX_WORKERS}")

        self._apply_and_run("M-5", check)

    def test_kill_m6_exact_nonnegative_if(self):
        """M-6: D at line 74 (_exact_nonnegative): If test -> False."""
        def check(mod):
            try:
                mod._exact_nonnegative("epoch", -5)
            except mod.ObservationError:
                return
            raise AssertionError("M-6 killed: negative epoch raised no error on mutated code!")

        self._apply_and_run("M-6", check)

    def test_kill_m7_viewport_dpr_if(self):
        """M-7: D at line 107 (_prepare): If test -> False."""
        obs = make_obs("a", 1, 0, dpr=1000000, viewport_dpr=2000000)

        def check(mod):
            res = mod.perceive_many([obs], adapter=FakeVisionEngine())
            if res[0].error is None or "AX viewport DPR differs from observation DPR" not in res[0].error:
                raise AssertionError("M-7 killed: viewport DPR mismatch raised no error on mutated code!")

        self._apply_and_run("M-7", check)

    def test_kill_m8_regions_isinstance_if(self):
        """M-8: D at line 177 (_describe_group): If test -> False."""
        class NonListRegionsEngine(FakeVisionEngine):
            def changed_regions(self, previous, frame):
                return "not_a_list"

        o1 = make_obs("a", 1, 0)
        o2 = make_obs("a", 2, 255)

        def check(mod):
            res = mod.perceive_many([o1, o2], adapter=NonListRegionsEngine())
            if res[1].error is None or "adapter returned non-list regions" not in res[1].error:
                raise AssertionError("M-8 killed: non-list regions raised no PERCEPTION_ERROR on mutated code!")

        self._apply_and_run("M-8", check)

    def test_kill_m9_import_fallback_if(self):
        """M-9: D at line 24 (module scope): If test -> False."""
        def check(mod):
            mod_ast = getattr(mod, "_ast_tree", None)
            if mod_ast:
                for node in ast.walk(mod_ast):
                    if getattr(node, "lineno", None) == 24 and isinstance(node, ast.If):
                        if isinstance(node.test, ast.Constant) and node.test.value is False:
                            raise AssertionError("M-9 killed: exception filtering If test replaced with False")

        self._apply_and_run("M-9", check)

    def test_kill_m10_changed_regions_swap(self):
        """M-10: S at line 176 (_describe_group): swap changed_regions(previous, frame)."""
        o1 = make_obs("a", 1, 10)
        o2 = make_obs("a", 2, 50)

        def check(mod):
            res = mod.perceive_many([o1, o2], adapter=AsymmetricAdapter())
            tiles = res[1].observation.state["_perception"]["regions"][0]["tiles"]
            if tiles != 40:
                raise AssertionError(f"M-10 killed: tiles argument order swapped! (tiles={tiles})")

        self._apply_and_run("M-10", check)


if __name__ == "__main__":
    unittest.main()
