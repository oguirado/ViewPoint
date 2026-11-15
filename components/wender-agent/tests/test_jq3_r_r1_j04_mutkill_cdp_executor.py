# @module {"id":"wa.test.jq3_r_r1_j04_mutkill_cdp_executor","layer":2,"owner":"ACT","deps":["wa.act.cdp_executor","wa.perceive.ax","wa.state.schema"],"book":"HB-WA-s2-act"}
# @layer:2
"""Mutation kill and contract unit tests for CDP executor observation binding, role agreement,

authorized focus, box selection, and L-path dispatch against fake CDP transport.
"""

import ast
import copy
import dataclasses
import random
import sys
import threading
import types
import unittest
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from wender.act import cdp_executor
from wender.perceive import ax


# Define fallback canonical frozen dataclasses for session_registry interfaces
@dataclasses.dataclass(frozen=True)
class Handle:
    kind: str
    canonical_id: str
    generation: int


@dataclasses.dataclass(frozen=True)
class FrameRef:
    handle: Handle
    epoch: int
    frame_seq: int
    width: int
    height: int
    rgb: Any
    digest: str


@dataclasses.dataclass(frozen=True)
class Observation:
    handle: Handle
    epoch: int
    frame_seq: int
    dpr_ppm: int
    state: Dict[str, Any]
    frame: FrameRef


@dataclasses.dataclass(frozen=True)
class ActorLease:
    handle: Handle
    owner: str
    generation: int
    nonce: bytes
    expires_ns: int


@dataclasses.dataclass(frozen=True)
class FocusLease:
    lease: ActorLease
    domain: str
    epoch: int


@dataclasses.dataclass(frozen=True)
class TargetRecord:
    handle: Handle
    closed: bool
    epoch: int
    session_id: str


class FakeRegistry:
    def __init__(self, record: TargetRecord):
        self.record = record

    def get(self, handle: Handle) -> TargetRecord:
        return self.record

    def validate_observation(self, obs: Observation) -> bool:
        return True


class FakeFocusPolicy:
    def classify(self, mode: str, op: str) -> str:
        return "exclusive"

    def validate(self, lease: ActorLease, focus: FocusLease, obs: Observation, domain: str, now_ns: int) -> None:
        pass

    def consume(self, focus: FocusLease) -> None:
        pass


class FakeWriterFence:
    def __init__(self):
        self.quarantined = False

    def acquire(self, lease: ActorLease) -> bool:
        return True

    def release(self, lease: ActorLease) -> None:
        pass

    def quarantine(self, lease: ActorLease) -> None:
        self.quarantined = True


class FakeCdp:
    """Fake CDP transport double for recording method calls and returning scripted replies."""

    def __init__(self):
        self.calls: List[Tuple[str, Optional[Dict[str, Any]], Optional[str]]] = []

    def send(self, method: str, params: Optional[Dict[str, Any]] = None, session_id: Optional[str] = None) -> Dict[str, Any]:
        self.calls.append((method, params, session_id))
        if method == "DOM.describeNode":
            backend_id = (params or {}).get("backendNodeId", 100)
            return {"node": {"backendNodeId": backend_id, "nodeName": "BUTTON"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": True}}
        if method == "DOM.getBoxModel":
            return {"model": {"border": [10, 20, 100, 20, 100, 50, 10, 50]}}
        return {}


def _create_test_env():
    raw_nodes = [
        {"nodeId": "1", "role": {"type": "role", "value": "RootWebArea"}, "name": {"type": "computedString", "value": "App"}, "childIds": ["2"], "ignored": False},
        {"nodeId": "2", "role": {"type": "role", "value": "button"}, "name": {"type": "computedString", "value": "Send"}, "childIds": [], "ignored": False, "backendDOMNodeId": 100}
    ]
    nodes = ax.prune(raw_nodes)
    btn = dict(nodes[1], box=[10, 20, 100, 50])
    nodes = [nodes[0], btn]

    handle = Handle("cdp_tab", "tab1", 1)
    lease = ActorLease(handle, "builder", 1, b"1234567890123456", 999999999999)
    focus = FocusLease(lease, "cdp:sess1", 1)
    from wender.perceive.session_registry import FrameRef as ValidFrameRef
    frame = ValidFrameRef.from_rgb(handle, 1, 10, np.zeros((800, 1000, 3), dtype=np.uint8))
    state = {
        "handle": handle,
        "epoch": 1,
        "frame_seq": 10,
        "nodes": nodes,
        "viewport": {"w": 1000, "h": 800},
        "focused_node_id": btn["id"],
    }
    obs = Observation(handle, 1, 10, 1, state, frame)
    registry = FakeRegistry(TargetRecord(handle, False, 1, "sess1"))
    focus_policy = FakeFocusPolicy()
    writer_fence = FakeWriterFence()
    cdp = FakeCdp()

    return btn, nodes, handle, lease, focus, obs, registry, focus_policy, writer_fence, cdp


# Embedded AST prove harness
with open("wender/act/cdp_executor.py", "r", encoding="utf-8") as _f:
    _SOURCE_TEXT = _f.read()

_PARSED_AST = ast.parse(_SOURCE_TEXT, filename="wender/act/cdp_executor.py")


def _apply_ast_mutation(source_ast: ast.AST, mutant_id: str) -> ast.AST:
    t = copy.deepcopy(source_ast)
    found = False
    for node in ast.walk(t):
        if isinstance(node, ast.FunctionDef):
            fn_name = node.name
            if mutant_id == "M1" and fn_name == "_run_authorized_ax_action":
                for child in ast.walk(node):
                    if isinstance(child, ast.BoolOp) and child.lineno == 854 and isinstance(child.op, ast.And):
                        child.op = ast.Or()
                        found = True
                        break
            elif mutant_id == "M2" and fn_name == "_authorized_input_events":
                for child in ast.walk(node):
                    if isinstance(child, ast.Compare) and child.lineno == 742 and isinstance(child.ops[0], ast.NotEq):
                        child.ops[0] = ast.Eq()
                        found = True
                        break
            elif mutant_id == "M3" and fn_name == "_run_authorized_ax_action":
                for child in ast.walk(node):
                    if isinstance(child, ast.Compare) and child.lineno == 851 and isinstance(child.ops[0], ast.Is) and ast.unparse(child) == "expect is None":
                        child.ops[0] = ast.IsNot()
                        found = True
                        break
            elif mutant_id == "M4" and fn_name == "_run_authorized_ax_action":
                for child in ast.walk(node):
                    if isinstance(child, ast.Compare) and child.lineno == 851 and isinstance(child.ops[0], ast.Is) and "node_by_id" in ast.unparse(child):
                        child.ops[0] = ast.IsNot()
                        found = True
                        break
            elif mutant_id == "M5" and fn_name == "_resolve_box":
                for child in ast.walk(node):
                    if (isinstance(child, ast.BoolOp) and child.lineno == 190
                            and isinstance(child.values[0], ast.Compare)
                            and isinstance(child.values[0].left, ast.Subscript)
                            and isinstance(child.values[0].left.slice, ast.Constant)):
                        comp0 = child.values[0]
                        comp0.left.slice.value += 1
                        found = True
                        break
            elif mutant_id == "M6" and fn_name == "_resolve_box":
                for child in ast.walk(node):
                    if isinstance(child, ast.Compare) and child.lineno == 208 and isinstance(child.comparators[0], ast.Constant) and child.comparators[0].value == 8:
                        child.comparators[0].value += 1
                        found = True
                        break
            elif mutant_id == "M7" and fn_name == "_dispatch_ax_op":
                for child in ast.walk(node):
                    if isinstance(child, ast.Call) and child.lineno == 280 and "check_viewport_point" in ast.unparse(child) and isinstance(child.args[0], ast.Subscript):
                        child.args[0].slice.value += 1
                        found = True
                        break
            elif mutant_id == "M8" and fn_name == "_validate_inputs":
                for child in ast.walk(node):
                    if isinstance(child, ast.Compare) and child.lineno == 124 and isinstance(child.comparators[0], ast.Constant) and child.comparators[0].value == 2:
                        child.comparators[0].value += 1
                        found = True
                        break
            elif mutant_id == "M9" and fn_name == "_validate_authorized_observation":
                for child in ast.walk(node):
                    if isinstance(child, ast.If) and child.lineno == 597:
                        child.test = ast.copy_location(ast.Constant(value=False), child.test)
                        found = True
                        break
            elif mutant_id == "M10" and fn_name in ("_run_authorized_ax_action", "_validate_authorized_target"):
                for child in ast.walk(node):
                    if isinstance(child, ast.If) and child.lineno in (690, 816):
                        child.test = ast.copy_location(ast.Constant(value=False), child.test)
                        found = True
                        break
            elif mutant_id == "M11" and fn_name == "_validate_authorized_observation":
                for child in ast.walk(node):
                    if isinstance(child, ast.Call) and child.lineno == 616 and "frame_seq" in ast.unparse(child):
                        child.args[0], child.args[1] = child.args[1], child.args[0]
                        found = True
                        break
            elif mutant_id == "M12" and fn_name == "_dispatch_ax_op":
                for child in ast.walk(node):
                    if isinstance(child, ast.Call) and child.lineno == 269:
                        child.args[0], child.args[1] = child.args[1], child.args[0]
                        found = True
                        break
    if not found:
        raise RuntimeError(f"Mutation obligation {mutant_id} anchor missing or ambiguous")
    return t


def _run_test_with_mutant(mutant_id: str, test_fn):
    # 1. Run baseline positive control on real unmutated code first
    test_fn(cdp_executor)

    # 2. Mutate in-memory AST
    mutated_ast = _apply_ast_mutation(_PARSED_AST, mutant_id)
    compiled_code = compile(mutated_ast, filename="wender/act/cdp_executor.py", mode="exec")

    # 3. Swap sys.modules safely
    old_mod = sys.modules.get("wender.act.cdp_executor")
    mutated_mod = types.ModuleType("wender.act.cdp_executor")
    mutated_mod.__file__ = "wender/act/cdp_executor.py"
    sys.modules["wender.act.cdp_executor"] = mutated_mod

    try:
        exec(compiled_code, mutated_mod.__dict__)
        try:
            test_fn(mutated_mod)
        except Exception as exc:
            # Successfully killed
            return exc
        raise AssertionError(f"Mutant {mutant_id} SURVIVED!")
    finally:
        if old_mod is not None:
            sys.modules["wender.act.cdp_executor"] = old_mod


# @anchor:wa.test.jq3_r_r1_j04_mutkill_cdp_executor.TestCdpExecutorMutkillContract
class TestCdpExecutorMutkillContract(unittest.TestCase):
    """Unit tests for CDP executor mutation obligations M1..M12 and baseline contract controls."""

    def test_m1_unverified_expect_kill(self):
        """M1: O at line 854 in _run_authorized_ax_action (And -> Or)."""
        def run_case(mod):
            btn, nodes, handle, lease, focus, obs, registry, focus_policy, writer_fence, cdp = _create_test_env()
            action = {"op": "click", "target": btn["id"], "expect": {"target_states": {"pressed": "true"}}}
            receipt = mod.run_ax_action(
                cdp, "sess1", nodes, action, read_nodes=lambda: nodes,
                lease=lease, focus=focus, observation=obs, registry=registry,
                focus_policy=focus_policy, writer_fence=writer_fence, sleep=lambda s: None
            )
            self.assertEqual(receipt["verified"], 0)

        _run_test_with_mutant("M1", run_case)

    def test_m2_observation_role_mismatch_kill(self):
        """M2: O at line 742 in _authorized_input_events (!= -> ==)."""
        def run_case(mod):
            btn, nodes, handle, lease, focus, obs, registry, focus_policy, writer_fence, cdp = _create_test_env()
            action = {"op": "click", "target": btn["id"], "path": "coordinates"}
            receipt = mod.run_ax_action(
                cdp, "sess1", nodes, action, read_nodes=lambda: nodes, allow_coordinates=True,
                lease=lease, focus=focus, observation=obs, registry=registry,
                focus_policy=focus_policy, writer_fence=writer_fence, sleep=lambda s: None
            )
            self.assertEqual(receipt["verified"], 0)

        _run_test_with_mutant("M2", run_case)

    def test_m3_navigated_expect_none_check_kill(self):
        """M3: O at line 851 in _run_authorized_ax_action (expect is None -> expect is not None)."""
        def run_case(mod):
            btn, nodes, handle, lease, focus, obs, registry, focus_policy, writer_fence, cdp = _create_test_env()
            post_nodes = [nodes[0]]
            call_count = 0

            def scripted_read():
                nonlocal call_count
                call_count += 1
                return nodes if call_count == 1 else post_nodes

            action = {"op": "click", "target": btn["id"]}
            receipt = mod.run_ax_action(
                cdp, "sess1", nodes, action, scripted_read,
                lease=lease, focus=focus, observation=obs, registry=registry,
                focus_policy=focus_policy, writer_fence=writer_fence, sleep=lambda s: None
            )
            self.assertEqual(receipt["verified"], 1)

        _run_test_with_mutant("M3", run_case)

    def test_m4_navigated_node_absent_check_kill(self):
        """M4: O at line 851 in _run_authorized_ax_action (_node_by_id is None -> _node_by_id is not None)."""
        def run_case(mod):
            btn, nodes, handle, lease, focus, obs, registry, focus_policy, writer_fence, cdp = _create_test_env()
            post_nodes = [nodes[0]]
            call_count = 0

            def scripted_read():
                nonlocal call_count
                call_count += 1
                return nodes if call_count == 1 else post_nodes

            action = {"op": "click", "target": btn["id"]}
            receipt = mod.run_ax_action(
                cdp, "sess1", nodes, action, scripted_read,
                lease=lease, focus=focus, observation=obs, registry=registry,
                focus_policy=focus_policy, writer_fence=writer_fence, sleep=lambda s: None
            )
            self.assertEqual(receipt["verified"], 1)

        _run_test_with_mutant("M4", run_case)

    def test_m5_box_positive_dimension_kill(self):
        """M5: B at line 190 in _resolve_box (cand[2] -> cand[3])."""
        def run_case(mod):
            btn, nodes, handle, lease, focus, obs, registry, focus_policy, writer_fence, cdp = _create_test_env()
            target_node = dict(btn, box=[10, 20, -5, 50])
            res = mod._resolve_box(cdp, "sess1", target_node, {"op": "click", "target": btn["id"]})
            self.assertEqual(res, [10, 20, 90, 30])

        _run_test_with_mutant("M5", run_case)

    def test_m6_box_quad_length_kill(self):
        """M6: B at line 208 in _resolve_box (len(quad) != 8 -> != 9)."""
        def run_case(mod):
            btn, nodes, handle, lease, focus, obs, registry, focus_policy, writer_fence, cdp = _create_test_env()
            target_node = dict(btn)
            del target_node["box"]
            res = mod._resolve_box(cdp, "sess1", target_node, {"op": "click", "target": btn["id"]})
            self.assertEqual(res, [10, 20, 90, 30])

        _run_test_with_mutant("M6", run_case)

    def test_m7_elbow_point_check_kill(self):
        """M7: B at line 280 in _dispatch_ax_op (elbow[0] -> elbow[1])."""
        def run_case(mod):
            btn, nodes, handle, lease, focus, obs, registry, focus_policy, writer_fence, cdp = _create_test_env()
            target_node = dict(btn, box=[450, 650, 100, 100])
            action = {
                "op": "lpath_click",
                "target": btn["id"],
                "origin": [100, 700],
                "waypoint_policy": "horizontal_first",
                "viewport": [600, 800],
            }
            mod._dispatch_ax_op(cdp, "sess1", target_node, action)

        _run_test_with_mutant("M7", run_case)

    def test_m8_viewport_length_kill(self):
        """M8: B at line 124 in _validate_inputs (len(vp) != 2 -> != 3)."""
        def run_case(mod):
            btn, nodes, handle, lease, focus, obs, registry, focus_policy, writer_fence, cdp = _create_test_env()
            action = {"op": "click", "target": btn["id"], "viewport": [1000, 800]}
            mod._validate_inputs(nodes, action, lambda: nodes, 3, 250, lambda s: None, False)

        _run_test_with_mutant("M8", run_case)

    def test_m9_lease_handle_kind_kill(self):
        """M9: D at line 597 in _validate_authorized_observation (getattr(handle, "kind") != "cdp_tab" -> False)."""
        def run_case(mod):
            btn, nodes, handle, lease, focus, obs, registry, focus_policy, writer_fence, cdp = _create_test_env()
            bad_handle = Handle("atspi_window", "tab1", 1)
            bad_lease = dataclasses.replace(lease, handle=bad_handle)
            # Keep deliberately invalid authority in the structural fixture so
            # the executor, rather than FrameRef construction, rejects it.
            bad_frame = FrameRef(bad_handle, obs.frame.epoch, obs.frame.frame_seq,
                                 obs.frame.width, obs.frame.height,
                                 obs.frame.rgb, obs.frame.digest)
            bad_obs = dataclasses.replace(obs, handle=bad_handle, frame=bad_frame, state=dict(obs.state, handle=bad_handle))
            with self.assertRaises(mod.ActionError) as cm:
                mod._validate_authorized_observation(registry, bad_lease, bad_obs, "sess1")
            self.assertIn("cdp_tab", str(cm.exception))

        _run_test_with_mutant("M9", run_case)

    def test_m10_keyboard_focus_check_kill(self):
        """M10: D at lines 690 and 816 in _validate_authorized_target and _run_authorized_ax_action (state.get("focused_node_id") != target -> False)."""
        def run_case(mod):
            btn, nodes, handle, lease, focus, obs, registry, focus_policy, writer_fence, cdp = _create_test_env()
            obs_unfocused = dataclasses.replace(
                obs,
                state=dict(obs.state, focused_node_id="other_node_id")
            )
            action = {"op": "press", "target": btn["id"], "key": "Enter"}
            with self.assertRaises(mod.ActionError) as cm:
                mod.run_ax_action(
                    cdp, "sess1", nodes, action, read_nodes=lambda: nodes,
                    lease=lease, focus=focus, observation=obs_unfocused, registry=registry,
                    focus_policy=focus_policy, writer_fence=writer_fence, sleep=lambda s: None
                )
            self.assertIn("focused element", str(cm.exception))

        _run_test_with_mutant("M10", run_case)

    def test_m11_frame_seq_swap_args_kill(self):
        """M11: S at line 616 in _validate_authorized_observation (getattr(observation, "frame_seq", None) args swapped)."""
        def run_case(mod):
            btn, nodes, handle, lease, focus, obs, registry, focus_policy, writer_fence, cdp = _create_test_env()
            st = mod._validate_authorized_observation(registry, lease, obs, "sess1")
            self.assertIsInstance(st, dict)

        _run_test_with_mutant("M11", run_case)

    def test_m12_resolve_box_swap_args_kill(self):
        """M12: S at line 269 in _dispatch_ax_op (_resolve_box args swapped)."""
        def run_case(mod):
            btn, nodes, handle, lease, focus, obs, registry, focus_policy, writer_fence, cdp = _create_test_env()
            target_node = dict(btn)
            del target_node["box"]
            action = {"op": "lpath_click", "target": btn["id"], "origin": [10, 10], "viewport": [1000, 800]}
            mod._dispatch_ax_op(cdp, "sess1", target_node, action)

        _run_test_with_mutant("M12", run_case)

    def test_seeded_property_loop_64_cases(self):
        """Seeded stdlib random property test with 64 valid cases and shrink reporting."""
        seed = 42
        rng = random.Random(seed)
        valid_count = 0

        for i in range(64):
            vw = rng.randint(200, 1920)
            vh = rng.randint(200, 1080)
            bw = rng.randint(10, 100)
            bh = rng.randint(10, 100)
            bx = rng.randint(0, vw - bw)
            by = rng.randint(0, vh - bh)

            btn, nodes, handle, lease, focus, obs, registry, focus_policy, writer_fence, cdp = _create_test_env()
            target_node = dict(btn, box=[bx, by, bw, bh])
            action = {
                "op": "lpath_click",
                "target": btn["id"],
                "origin": [rng.randint(0, vw - 1), rng.randint(0, vh - 1)],
                "waypoint_policy": rng.choice(["horizontal_first", "vertical_first"]),
                "viewport": [vw, vh],
            }

            try:
                cdp_executor._dispatch_ax_op(cdp, "sess1", target_node, action)
                valid_count += 1
            except Exception as exc:
                minimal_fixture = f"seed={seed}, case={i}, viewport=[{vw}, {vh}], box=[{bx}, {by}, {bw}, {bh}], action={action}"
                self.fail(f"Seeded property case failed: {exc}. Minimal fixture: {minimal_fixture}")

        self.assertEqual(valid_count, 64)

    def test_concurrency_writer_fence_schedules(self):
        """Shared-state surface concurrency test with controlled barriers and bounded joins."""
        btn, nodes, handle, lease, focus, obs, registry, focus_policy, writer_fence, cdp = _create_test_env()
        barrier = threading.Barrier(4)
        errors: List[Exception] = []
        threads: List[threading.Thread] = []

        def worker(worker_id: int):
            try:
                barrier.wait(timeout=2.0)
                action = {"op": "click", "target": btn["id"]}
                cdp_executor.run_ax_action(
                    cdp, "sess1", nodes, action, read_nodes=lambda: nodes,
                    lease=lease, focus=focus, observation=obs, registry=registry,
                    focus_policy=focus_policy, writer_fence=writer_fence, sleep=lambda s: None
                )
            except Exception as exc:
                errors.append(exc)

        for w_id in range(4):
            t = threading.Thread(target=worker, args=(w_id,))
            t.daemon = True
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "Leaked thread detected!")

        self.assertEqual(len(errors), 0, f"Thread workers encountered errors: {errors}")


if __name__ == "__main__":
    unittest.main()
