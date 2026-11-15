"""Owned CDP gate: real merged records, offline structural authorities, in-memory proofs.

All timings printed by this module are VM measurements.  Structural collaborators
exercise the executor boundary, not the separately owned registry or fence APIs.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace
import ast
from contextlib import contextmanager
import hashlib
import itertools
import json
from pathlib import Path
import random
import signal
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from wender.act import cdp_executor as executor
from wender.act.focus_policy import FocusPolicy
from wender.perceive import ax
from wender.perceive.session_registry import ActorLease, FrameRef, Handle, Observation, TargetRecord


SOURCE = Path(executor.__file__).read_text(encoding="utf-8")
SOURCE_PATH = "wender/act/cdp_executor.py"
SEED = 0x3A7701
CASES = 64
SHRINK_LIMIT = 32
SCHEDULES = 32


class AnchorError(ValueError):
    pass


class ProofTimeout(TimeoutError):
    pass


@dataclass(frozen=True)
class Mutation:
    identifier: str
    qualified_function: str
    node_type: str
    original: str
    operator: str
    selector: str
    source_path: str = SOURCE_PATH
    original_line: int = 0


@dataclass(frozen=True)
class Proof:
    identifier: str
    disposition: str
    control: str
    detail: str = ""


def _function(tree, qualified_name):
    found = []

    def visit(node, prefix=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + child.name
                if name == qualified_name and not isinstance(child, ast.ClassDef):
                    found.append(child)
                visit(child, name + ".")
            else:
                visit(child, prefix)

    visit(tree)
    if len(found) != 1:
        raise AnchorError("qualified function %r matched %d definitions" % (qualified_name, len(found)))
    return found[0]


def _scoped_nodes(function):
    yield function
    for child in ast.iter_child_nodes(function):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield from _scoped_nodes(child)


def _selected(tree, source, spec):
    function = _function(tree, spec.qualified_function)
    matches = [node for node in _scoped_nodes(function)
               if type(node).__name__ == spec.node_type
               and ast.get_source_segment(source, node) == spec.original]
    if len(matches) != 1:
        raise AnchorError("%s exact %s anchor matched %d nodes: %r" %
                          (spec.qualified_function, spec.node_type, len(matches), spec.original))
    return matches[0]


_OPPOSITES = {ast.Lt: ast.Gt, ast.Gt: ast.Lt, ast.LtE: ast.GtE, ast.GtE: ast.LtE,
              ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
              ast.In: ast.NotIn, ast.NotIn: ast.In}
_BOUNDARIES = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt}
_BINARY = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.FloorDiv, ast.FloorDiv: ast.Mult}


def mutate(source, spec):
    """One exact AST mutation; missing/ambiguous/invalid selectors are hard errors."""
    tree = ast.parse(source, filename=spec.source_path)
    node = _selected(tree, source, spec)
    if spec.operator in ("O", "B") and isinstance(node, ast.Compare):
        if not spec.selector.startswith("ops[") or not spec.selector.endswith("]"):
            raise AnchorError("comparison requires an exact ops[index] selector")
        try:
            index = int(spec.selector[4:-1])
            if index < 0:
                raise IndexError
            original = type(node.ops[index])
            mapping = _OPPOSITES if spec.operator == "O" else _BOUNDARIES
            replacement = mapping[original]
        except (ValueError, IndexError, KeyError) as exc:
            raise AnchorError("comparison operator/selector is unavailable") from exc
        node.ops[index] = replacement()
    elif spec.operator == "O" and isinstance(node, ast.BoolOp) and spec.selector == "op":
        node.op = {ast.And: ast.Or, ast.Or: ast.And}[type(node.op)]()
    elif spec.operator == "O" and isinstance(node, ast.BinOp) and spec.selector == "op":
        try:
            node.op = _BINARY[type(node.op)]()
        except KeyError as exc:
            raise AnchorError("unsupported binary operator") from exc
    elif spec.operator == "B" and isinstance(node, ast.Constant) and spec.selector == "value":
        if type(node.value) is not int:
            raise AnchorError("B constant selector requires an exact integer, never bool")
        node.value += 1
    elif spec.operator == "D" and spec.selector == "test" and isinstance(node, ast.If):
        if not any(isinstance(n, ast.Raise) for item in node.body for n in ast.walk(item)):
            raise AnchorError("D If must be a raising guard")
        node.test = ast.copy_location(ast.Constant(False), node.test)
    elif spec.operator == "D" and spec.selector == "test" and isinstance(node, ast.Assert):
        node.test = ast.copy_location(ast.Constant(True), node.test)
    elif spec.operator == "S" and isinstance(node, ast.Call) and spec.selector == "first_two_distinct_nonstarred_args":
        ordinary = [(index, arg) for index, arg in enumerate(node.args) if not isinstance(arg, ast.Starred)]
        if not ordinary:
            raise AnchorError("S has no ordinary positional argument")
        first_index, first = ordinary[0]
        second = next((index for index, arg in ordinary[1:]
                       if ast.dump(arg, include_attributes=False) != ast.dump(first, include_attributes=False)), None)
        if second is None:
            raise AnchorError("S needs two distinct non-starred positional arguments")
        node.args[first_index], node.args[second] = node.args[second], node.args[first_index]
    else:
        raise AnchorError("operator %s does not apply to %s/%s" % (spec.operator, spec.node_type, spec.selector))
    return ast.fix_missing_locations(tree)


@contextmanager
def _deadline(seconds=2):
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("mutation proofs run on the main thread")
    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)

    def expired(signum, frame):
        raise ProofTimeout("in-memory proof deadline exceeded")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        signal.setitimer(signal.ITIMER_REAL, *old_timer)


_PRIVATE_IDS = itertools.count()


@contextmanager
def _private_module(code, source_path):
    """No mocked imports, persisted mutants or bytecode; dataclasses see registration."""
    previous = dict(sys.modules)
    old_bytecode = sys.dont_write_bytecode
    name = "wender.act._jq3_r7_prove_%d" % next(_PRIVATE_IDS)
    module = ModuleType(name)
    module.__file__ = source_path
    module.__package__ = "wender.act"
    sys.modules[name] = module
    sys.dont_write_bytecode = True
    try:
        exec(code, module.__dict__)
        yield module
    finally:
        for key in tuple(sys.modules):
            if key not in previous:
                del sys.modules[key]
        for key, value in previous.items():
            if sys.modules.get(key) is not value:
                sys.modules[key] = value
        sys.dont_write_bytecode = old_bytecode


def prove(source, spec, assertion, *, equivalent=None, timeout=2):
    """Only this same assertion's semantic AssertionError after a passing control kills."""
    try:
        real_code = compile(source, spec.source_path, "exec", dont_inherit=True)
    except (SyntaxError, ValueError, TypeError) as exc:
        return Proof(spec.identifier, "compile_error", "not_run", repr(exc))
    phase = "import"
    try:
        with _deadline(timeout), _private_module(real_code, spec.source_path) as module:
            phase = "assertion"
            assertion(module)
    except AssertionError as exc:
        disposition = "failed_control" if phase == "assertion" else "import_error"
        return Proof(spec.identifier, disposition, "failed", str(exc))
    except (ProofTimeout, TimeoutError) as exc:
        return Proof(spec.identifier, "timeout", "failed", str(exc))
    except ImportError as exc:
        return Proof(spec.identifier, "import_error", "failed", str(exc))
    except Exception as exc:
        return Proof(spec.identifier, "runtime_error", "failed", repr(exc))
    try:
        tree = mutate(source, spec)
        mutant_code = compile(tree, spec.source_path, "exec", dont_inherit=True)
    except AnchorError as exc:
        return Proof(spec.identifier, "anchor_error", "passed", str(exc))
    except (SyntaxError, ValueError, TypeError) as exc:
        return Proof(spec.identifier, "compile_error", "passed", repr(exc))
    phase = "import"
    try:
        with _deadline(timeout), _private_module(mutant_code, spec.source_path) as module:
            phase = "assertion"
            assertion(module)
    except AssertionError as exc:
        trace = exc.__traceback__
        while trace.tb_next is not None:
            trace = trace.tb_next
        semantic = trace.tb_frame.f_globals.get("__name__") in (__name__, "unittest.case")
        disposition = "kill" if phase == "assertion" and semantic else "import_error" if phase == "import" else "runtime_error"
        return Proof(spec.identifier, disposition, "passed", str(exc))
    except (ProofTimeout, TimeoutError) as exc:
        return Proof(spec.identifier, "timeout", "passed", str(exc))
    except ImportError as exc:
        return Proof(spec.identifier, "import_error", "passed", str(exc))
    except Exception as exc:
        return Proof(spec.identifier, "runtime_error", "passed", repr(exc))
    if equivalent is not None:
        try:
            proof = equivalent(source, tree)
        except Exception as exc:
            return Proof(spec.identifier, "equivalence_error", "passed", repr(exc))
        if isinstance(proof, str) and proof:
            return Proof(spec.identifier, "proven_equivalent", "passed", proof)
    return Proof(spec.identifier, "survivor", "passed")


CATALOGUE = (
    Mutation("M-wender-act-cdp-executor-1", "_run_authorized_ax_action", "BoolOp",
             "_verify_expect(post, target, expect)\n            and (expect is not None or before_sha != after_sha)",
             "O", "op", original_line=762),
    Mutation("M-wender-act-cdp-executor-2", "_authorized_input_events", "Compare",
             'obs_node.get("role") != node.get("role")', "O", "ops[0]", original_line=655),
)


class Registry:
    """Explicit executor protocol; not a stand-in SessionRegistry implementation."""
    def __init__(self, record, observation):
        self.record = record
        self.current = observation

    def get(self, handle):
        return self.record if self.record.handle == handle else None

    def validate_observation(self, observation):
        return observation is self.current


class Locks:
    def __init__(self, lease):
        self.current = lease

    def owns(self, lease, *, now_ns):
        return lease is self.current and now_ns < lease.expires_ns


class Fence:
    """Small synchronized acquire/release/quarantine collaborator only."""
    def __init__(self):
        self.mutex = threading.Lock()
        self.holder = None
        self.blocked = False
        self.acquisitions = self.releases = self.quarantines = 0

    def acquire(self, lease):
        with self.mutex:
            if self.holder is not None or self.blocked:
                return False
            self.holder = lease
            self.acquisitions += 1
            return True

    def release(self, lease):
        with self.mutex:
            if self.holder is not lease:
                raise RuntimeError("release without ownership")
            self.holder = None
            self.releases += 1

    def quarantine(self, lease):
        with self.mutex:
            self.blocked = True
            self.quarantines += 1


class CDP:
    def __init__(self, fail_method=None):
        self.calls = []
        self.fail_method = fail_method

    def send(self, method, params=None, session_id=None):
        self.calls.append((method, copy.deepcopy(params), session_id))
        if method == self.fail_method:
            raise RuntimeError("offline acknowledgement lost")
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "owned-object"}}
        return {}


def make_node(backend=41, *, role="button", attrs=(), box=None):
    node = {"id": "target", "backend_id": backend, "role": role,
            "name": "Submit", "depth": 0, "ancestors": (), "attrs": attrs,
            "ordinal": 0, "cardinality": 1,
            "box": [2, 2, 4, 4] if box is None else box}
    node["id"] = ax.stable_id(node)
    return node


class Case:
    def __init__(self, *, backend=41, epoch=2, sequence=7, width=16, height=12,
                 role="button", box=None):
        self.handle = Handle("cdp_tab", "owned-tab", 3)
        self.lease = ActorLease(self.handle, "executor-owner", 3, b"owned-nonce", 500)
        self.now = 100
        self.node = make_node(backend, role=role, box=box)
        self.after = dict(self.node, attrs=(("selected", "true"),))
        frame = FrameRef.from_rgb(self.handle, epoch, sequence,
                                  np.zeros((height, width, 3), dtype=np.uint8))
        state = {"handle": self.handle, "epoch": epoch, "frame_seq": sequence,
                 "digest": frame.digest, "nodes": [copy.deepcopy(self.node)],
                 "focused_node_id": self.node["id"],
                 "viewport": {"w": width, "h": height, "scroll_x": 0, "scroll_y": 0}}
        self.observation = Observation(self.handle, epoch, sequence, 1000000, state, frame)
        self.record = TargetRecord(self.handle, None, True, epoch, "s1", True, False)
        self.registry = Registry(self.record, self.observation)
        self.locks = Locks(self.lease)
        self.policy = FocusPolicy(self.registry, self.locks, clock=lambda: self.now)
        self.policy.set_focus(self.handle, domain="cdp:s1", epoch=epoch)
        self.focus = self.policy.grant(self.lease, domain="cdp:s1")
        self.fence = Fence()
        self.cdp = CDP()
        self.action = {"op": "click", "target": self.node["id"],
                       "expect": {"target_states": {"selected": "true"}}}

    def run(self, module=executor, *, action=None, reader=None, **overrides):
        snapshots = iter(([self.node], [self.after]))
        arguments = dict(lease=self.lease, focus=self.focus,
                         observation=self.observation, registry=self.registry,
                         focus_policy=self.policy, writer_fence=self.fence,
                         clock=lambda: self.now)
        arguments.update(overrides)
        return module.run_ax_action(
            self.cdp, "s1", [self.node], self.action if action is None else action,
            (lambda: next(snapshots)) if reader is None else reader,
            retries=3, wait_ms=0, sleep=lambda _: None, **arguments)


class TestAuthorizedFreshness(unittest.TestCase):
    def test_keyboard_focus_change_during_fresh_read_refuses_before_send(self):
        """Pinned pre-fix assertion: executor source SHA-256 eab2fa1ab7789e0b09f886d9de6d30c8870afc18ca75ad1601a2793f3a7bdcdc."""
        case = Case(role="textbox")
        action = dict(case.action, op="press", key="Enter")

        def reader():
            case.observation.state["focused_node_id"] = "sibling-element"
            return [case.node]

        refused = False
        try:
            case.run(action=action, reader=reader)
        except executor.ActionError:
            refused = True
        self.assertEqual(case.cdp.calls, [], "focus changed during fresh read; no CDP write is authorized")
        self.assertTrue(refused, "focus changed during fresh read must refuse")

    def assert_refused(self, case, *, action=None, reader=None, **changes):
        with self.assertRaises(executor.ActionError):
            case.run(action=action, reader=reader, **changes)
        self.assertEqual(case.cdp.calls, [], "a refused call must not reach even DOM scroll")

    def test_all_authorities_are_mandatory_and_structural_capabilities_checked(self):
        for name in ("lease", "focus", "observation", "registry", "focus_policy", "writer_fence"):
            with self.subTest(missing=name):
                self.assert_refused(Case(), **{name: None})
        for name, collaborator in (
                ("registry", SimpleNamespace(validate_observation=lambda _: True)),
                ("registry", SimpleNamespace(get=lambda _: None)),
                ("writer_fence", SimpleNamespace(acquire=lambda _: True, release=lambda _: None)),
                ("focus_policy", SimpleNamespace(validate=lambda *a, **k: None))):
            with self.subTest(incomplete=name):
                self.assert_refused(Case(), **{name: collaborator})

    def test_all_supported_operations_have_one_authorized_attempt(self):
        for op in executor.OPS:
            with self.subTest(op=op):
                role = "textbox" if op in ("type", "insert_text") else "combobox" if op == "select" else "button"
                case = Case(role=role)
                action = dict(case.action, op=op)
                if op in ("type", "insert_text"):
                    action["text"] = "offline text"
                elif op == "select":
                    action["value"] = "one"
                elif op == "press":
                    action["key"] = "Enter"
                elif op == "lpath_click":
                    action["origin"] = [0, 0]
                result = case.run(action=action)
                self.assertEqual((result["ok"], result["verified"], result["attempts"]), (1, 1, 1))
                self.assertTrue(case.cdp.calls)
                self.assertTrue(all(session == "s1" for _, _, session in case.cdp.calls))
                self.assertEqual((case.fence.acquisitions, case.fence.releases, case.fence.quarantines), (1, 1, 0))
                prior = list(case.cdp.calls)
                with self.assertRaises(executor.ActionError):
                    case.run(action=action)
                self.assertEqual(case.cdp.calls, prior, "one-use focus may never dispatch twice")

    def test_current_record_session_generation_epoch_and_observation_required(self):
        for kind in ("session", "missing_session", "closed", "epoch", "handle", "observation", "lease", "expiry"):
            case = Case()
            if kind == "session":
                case.registry.record = replace(case.record, session_id="foreign")
            elif kind == "missing_session":
                case.registry.record = replace(case.record, session_id=None, attachment_owned=False)
            elif kind == "closed":
                case.registry.record = replace(case.record, closed=True)
            elif kind == "epoch":
                case.registry.record = replace(case.record, epoch=case.record.epoch + 1)
            elif kind == "handle":
                case.registry.record = replace(case.record, handle=replace(case.handle, generation=4))
            elif kind == "observation":
                case.registry.current = None
            elif kind == "lease":
                case.locks.current = None
            else:
                case.now = case.lease.expires_ns
            with self.subTest(kind=kind):
                self.assert_refused(case)

    def test_tied_missing_unreadable_and_role_changed_fresh_targets_refuse(self):
        for kind in ("tie", "empty", "none", "unreadable", "role", "backend", "disabled"):
            case = Case()
            if kind == "tie":
                reader = lambda: [dict(case.node), dict(case.node)]
            elif kind == "empty":
                reader = lambda: []
            elif kind == "none":
                reader = lambda: None
            elif kind == "unreadable":
                reader = lambda: next(iter(()))
            elif kind == "role":
                reader = lambda: [dict(case.node, role="link")]
            elif kind == "backend":
                reader = lambda: [dict(case.node, backend_id=99)]
            else:
                reader = lambda: [dict(case.node, attrs=(("disabled", "true"),))]
            with self.subTest(kind=kind):
                self.assert_refused(case, reader=reader)

    def test_transport_acknowledgement_is_not_effect_verification(self):
        case = Case()
        result = case.run(reader=lambda: [case.node])
        self.assertEqual((result["ok"], result["verified"], result["attempts"]), (0, 0, 1))
        self.assertEqual(sum(method == "Runtime.callFunctionOn" for method, _, _ in case.cdp.calls), 1)
        no_expect = Case()
        result = no_expect.run(action={"op": "click", "target": no_expect.node["id"]},
                               reader=lambda: [no_expect.node])
        self.assertEqual((result["ok"], result["verified"]), (0, 0))

    def test_preexisting_change_in_fresh_snapshot_is_not_an_action_effect(self):
        case = Case()
        fresh = dict(case.node, attrs=(("selected", "true"),))
        # A concurrent pre-dispatch change must not be credited to our action.
        case.observation.state["nodes"] = [copy.deepcopy(fresh)]
        result = case.run(action={"op": "click", "target": case.node["id"]}, reader=lambda: [fresh])
        self.assertEqual((result["ok"], result["verified"]), (0, 0))
        self.assertEqual(result["pre_sha"], result["post_sha"])

    def test_ambiguous_transport_quarantines_without_retry_or_coordinate_fallback(self):
        for method in ("DOM.scrollIntoViewIfNeeded", "DOM.resolveNode", "Runtime.callFunctionOn"):
            case = Case()
            case.cdp.fail_method = method
            result = case.run(action=dict(case.action, irreversible=True), allow_coordinates=True)
            self.assertEqual((result["ok"], result["verified"], result["attempts"]), (0, 0, 1))
            self.assertEqual((case.fence.quarantines, case.fence.releases), (1, 0))
            self.assertFalse(any(name == "Input.dispatchMouseEvent" for name, _, _ in case.cdp.calls))
            self.assertEqual(sum(name == method for name, _, _ in case.cdp.calls), 1)

    def test_absent_merged_focus_or_frame_authority_refuses(self):
        for module_name in ("wender.act.focus_policy", "wender.perceive.session_registry"):
            case = Case()
            with self.subTest(module=module_name), patch.dict(sys.modules, {module_name: None}):
                self.assert_refused(case)

    def test_observed_identity_disagreement_and_duplicate_ids_refuse(self):
        for kind in ("duplicate", "name", "ordinal", "disabled", "role", "backend"):
            case = Case()
            observed = case.observation.state["nodes"][0]
            if kind == "duplicate":
                case.observation.state["nodes"].append(copy.deepcopy(observed))
            elif kind == "name":
                observed["name"] = "Different action"
            elif kind == "ordinal":
                observed["ordinal"] = 1
            elif kind == "disabled":
                observed["attrs"] = (("disabled", "true"),)
            elif kind == "role":
                observed["role"] = "link"
            else:
                observed["backend_id"] += 1
            with self.subTest(kind=kind):
                self.assert_refused(case)

    def test_reader_cannot_rewrite_validated_action_or_recorded_identity(self):
        case = Case()
        recorded = copy.deepcopy(case.node)
        wrong = dict(recorded, attrs=(("selected", "false"),))
        reads = 0

        def reader():
            nonlocal reads
            reads += 1
            case.action["op"] = "press"
            case.action["key"] = "Enter"
            case.action["expect"] = {"target_states": {"selected": "false"}}
            case.node["name"] = "Rewritten caller node"
            return [recorded if reads == 1 else wrong]

        result = case.run(reader=reader)
        self.assertEqual((result["ok"], result["verified"], result["attempts"]), (0, 0, 1))
        methods = [method for method, _, _ in case.cdp.calls]
        self.assertEqual(methods.count("Runtime.callFunctionOn"), 1)
        self.assertNotIn("Input.dispatchKeyEvent", methods)

    def test_backendless_legacy_press_retains_fresh_retry(self):
        node = make_node(backend=None)
        after = dict(node, attrs=(("selected", "true"),))
        snapshots = iter(([node], [node], [after]))
        cdp = CDP()
        result = executor.run_ax_action(
            cdp, "s1", [node], {"op": "press", "target": node["id"], "key": "Enter",
                                "expect": {"target_states": {"selected": "true"}}},
            lambda: next(snapshots), retries=2, wait_ms=0, sleep=lambda _: None)
        self.assertEqual((result["ok"], result["verified"], result["attempts"]), (1, 1, 2))
        self.assertEqual([params["type"] for method, params, _ in cdp.calls],
                         ["rawKeyDown", "keyUp", "rawKeyDown", "keyUp"])

    def test_legacy_reader_cannot_rewrite_retry_identity(self):
        node = make_node()
        cdp = CDP()
        action = {"op": "click", "target": node["id"],
                  "expect": {"target_states": {"selected": "true"}}}

        def reader():
            node["role"] = "link"
            node["name"] = "Different destination"
            return [node]

        refused = False
        try:
            executor.run_ax_action(cdp, "s1", [node], action, reader,
                                   retries=2, wait_ms=0, sleep=lambda _: None)
        except executor.ActionError:
            refused = True
        self.assertEqual(sum(method == "Runtime.callFunctionOn" for method, _, _ in cdp.calls), 1,
                         "a mutated caller node cannot authorize the second legacy dispatch")
        self.assertTrue(refused, "changed role and identity must refuse before retry")

    def test_frame_content_and_digest_are_revalidated_not_only_aliases(self):
        self.assertEqual(Case().run()["verified"], 1)
        for kind in ("digest", "shape", "dtype", "content"):
            case = Case()
            original = case.observation.frame
            # External structural data deliberately passes Observation's identity
            # checks; only reconstructing the merged FrameRef validates its RGB.
            frame = SimpleNamespace(handle=original.handle, epoch=original.epoch,
                                    frame_seq=original.frame_seq, width=original.width,
                                    height=original.height, rgb=original.rgb,
                                    digest=original.digest)
            if kind == "digest":
                frame.digest = "f" * 64
            elif kind == "shape":
                frame.rgb = np.zeros((1, 1, 3), dtype=np.uint8)
            elif kind == "dtype":
                frame.rgb = np.zeros(original.rgb.shape, dtype=np.float64)
            else:
                frame.rgb = np.ones(original.rgb.shape, dtype=np.uint8)
            state = copy.deepcopy(case.observation.state)
            state["digest"] = frame.digest  # do not kill only the alias check
            case.observation = Observation(case.handle, original.epoch, original.frame_seq,
                                           1000000, state, frame)
            case.registry.current = case.observation
            with self.subTest(kind=kind):
                self.assert_refused(case)

    def test_request_nonce_and_integer_alias_types_must_agree(self):
        for key in ("actor_lease", "epoch", "frame_seq"):
            case = Case(epoch=1, sequence=1)
            value = replace(case.lease, nonce=b"unissued-nonce") if key == "actor_lease" else True
            with self.subTest(key=key):
                self.assert_refused(case, action=dict(case.action, **{key: value}))
        case = Case()
        with self.assertRaises(executor.ActionError):
            executor.run_ax_action(case.cdp, "s1", [case.node],
                                   dict(case.action, digest=case.observation.frame.digest),
                                   lambda: [case.node], wait_ms=0, sleep=lambda _: None)
        self.assertEqual(case.cdp.calls, [], "digest-only request cannot enter legacy without authority")

    def test_busy_writer_fence_refuses_before_reader_or_transport(self):
        case = Case()
        self.assertTrue(case.fence.acquire(case.lease))
        reads = []

        def reader():
            reads.append(True)
            return [case.node]

        self.assert_refused(case, reader=reader)
        self.assertEqual(reads, [])
        self.assertEqual((case.fence.acquisitions, case.fence.releases, case.fence.quarantines), (1, 0, 0))


def _bounded_property(test, domain, seed, factory, check, valid):
    """64 valid fixtures; deterministic, validity-preserving, bounded failure reduction."""
    rng = random.Random(seed)
    started = time.monotonic()
    for index in range(CASES):
        fixture = factory(rng)
        if not valid(fixture):
            test.fail("incomplete generator: domain=%s seed=%d case=%d fixture=%r" % (domain, seed, index, fixture))
        try:
            check(fixture)
        except AssertionError as original:
            minimal = dict(fixture)
            attempts = 0
            exhausted = False
            while True:
                changed = False
                candidates = []
                for key in sorted(minimal):
                    if type(minimal[key]) is int:
                        for lower in (0, 1):
                            if lower < minimal[key]:
                                candidate = dict(minimal, **{key: lower})
                                if valid(candidate):
                                    candidates.append(candidate)
                for candidate in candidates:
                    if attempts >= SHRINK_LIMIT:
                        exhausted = True
                        break
                    attempts += 1
                    try:
                        check(candidate)
                    except AssertionError:
                        minimal = candidate
                        changed = True
                        break
                if exhausted or not changed:
                    break
            test.fail("%s seed=%d case=%d minimal_fixture=%s shrink_attempts=%d incomplete=%s original=%s" %
                      (domain, seed, index, json.dumps(minimal, sort_keys=True), attempts, exhausted, original))
    print("VM property " + json.dumps({"domain": domain, "seed": seed, "valid_cases": CASES,
                                       "shrink_attempts": 0, "minimal_fixture": None,
                                       "seconds": round(time.monotonic() - started, 6)}, sort_keys=True))


class TestSeededDomains(unittest.TestCase):
    def test_identity_and_digest_alias_domain(self):
        def check(values):
            for changed in (None, "handle", "epoch", "frame_seq", "digest"):
                case = Case(**values)
                aliases = {"handle": case.handle, "epoch": values["epoch"],
                           "frame_seq": values["sequence"], "digest": case.observation.frame.digest}
                if changed == "handle":
                    aliases[changed] = replace(case.handle, canonical_id="foreign-tab")
                elif changed in ("epoch", "frame_seq"):
                    aliases[changed] += 1
                elif changed == "digest":
                    aliases[changed] = "f" * 64
                action = dict(case.action, **aliases)
                if changed is None:
                    self.assertEqual(case.run(action=action)["verified"], 1)
                else:
                    with self.assertRaises(executor.ActionError):
                        case.run(action=action)
                    self.assertEqual(case.cdp.calls, [])
        _bounded_property(self, "identity_and_digest", SEED,
                          lambda rng: {"backend": rng.randint(1, 65535), "epoch": rng.randint(0, 100),
                                       "sequence": rng.randint(0, 1000)}, check,
                          lambda v: v["backend"] > 0 and v["epoch"] >= 0 and v["sequence"] >= 0)

    def test_focus_and_freshness_revalidation_domain(self):
        def check(values):
            for changed in ("focused_node_id", "epoch", "frame_seq", "digest", "current", "lease"):
                case = Case(**values, role="textbox")

                def reader():
                    if changed == "current":
                        case.registry.current = None
                    elif changed == "lease":
                        case.locks.current = None
                    elif changed == "focused_node_id":
                        case.observation.state[changed] = "sibling"
                    elif changed == "digest":
                        case.observation.state[changed] = "f" * 64
                    else:
                        case.observation.state[changed] += 1
                    return [case.node]

                with self.assertRaises(executor.ActionError):
                    case.run(action=dict(case.action, op="press", key="Enter"), reader=reader)
                self.assertEqual(case.cdp.calls, [])
                self.assertEqual((case.fence.releases, case.fence.quarantines), (1, 0))
        _bounded_property(self, "focus_and_freshness", SEED + 1,
                          lambda rng: {"backend": rng.randint(1, 65535), "epoch": rng.randint(0, 100),
                                       "sequence": rng.randint(0, 1000)}, check,
                          lambda v: v["backend"] > 0 and v["epoch"] >= 0 and v["sequence"] >= 0)

    def test_effect_verification_domain(self):
        def check(values):
            for satisfies in (False, True):
                case = Case(backend=values["backend"])
                post = dict(case.node, attrs=(("selected", "true" if satisfies else "false"),))
                reads = iter(([case.node], [post]))
                result = case.run(reader=lambda: next(reads))
                self.assertEqual((result["ok"], result["verified"], result["attempts"]),
                                 (int(satisfies), int(satisfies), 1))
                self.assertEqual(sum(name == "Runtime.callFunctionOn" for name, _, _ in case.cdp.calls), 1)
            fresh = [make_node(values["backend"], attrs=(("selected", "true"),))]
            target = make_node(values["backend"])
            self.assertTrue(executor._verify_expect(fresh, target, {"target_states": {"selected": "true"}}))
            self.assertFalse(executor._verify_expect(fresh, target, {"target_states": {"selected": "false"}}))
            self.assertFalse(executor._verify_expect([], target, None))
        _bounded_property(self, "effect_verification", SEED + 2,
                          lambda rng: {"backend": rng.randint(1, 65535)}, check, lambda v: v["backend"] > 0)

    def test_lpath_bounds_and_geometry_domain(self):
        def check(values):
            width, height = values["width"], values["height"]
            cx, cy = values["x"], values["y"]
            box = [cx, cy, 1, 1]
            for policy in ("horizontal_first", "vertical_first"):
                case = Case(width=width, height=height, box=box)
                action = dict(case.action, op="lpath_click", origin=[0, 0], waypoint_policy=policy)
                result = case.run(action=action)
                self.assertEqual(result["verified"], 1)
                events = [params for method, params, _ in case.cdp.calls if method == "Input.dispatchMouseEvent"]
                self.assertTrue(3 <= len(events) <= 4)
                self.assertEqual([event["type"] for event in events[-2:]], ["mousePressed", "mouseReleased"])
                self.assertTrue(all(0 <= e["x"] < width and 0 <= e["y"] < height for e in events))
                self.assertEqual([(e["x"], e["y"]) for e in events[-2:]], [(cx, cy), (cx, cy)])
                points = [(0, 0)] + [(e["x"], e["y"]) for e in events[:-2]]
                self.assertTrue(all(a[0] == b[0] or a[1] == b[1] for a, b in zip(points, points[1:])))
            for origin in ([width, 0], [0, height], [-1, 0], [0, -1]):
                case = Case(width=width, height=height, box=box)
                with self.assertRaises(executor.ActionError):
                    case.run(action=dict(case.action, op="lpath_click", origin=origin))
                self.assertEqual(case.cdp.calls, [])
        def factory(rng):
            width, height = rng.randint(2, 40), rng.randint(2, 40)
            return {"width": width, "height": height, "x": rng.randrange(width), "y": rng.randrange(height)}
        _bounded_property(self, "lpath_geometry", SEED + 3, factory, check,
                          lambda v: v["width"] > 0 and v["height"] > 0
                          and 0 <= v["x"] < v["width"] and 0 <= v["y"] < v["height"])

    def test_legacy_irreversible_retry_rechecks_fresh_gate_domain(self):
        def check(values):
            node = make_node(values["backend"])
            disabled = dict(node, attrs=(("disabled", "true"),))
            snapshots = iter(([node], [node], [disabled]))
            cdp = CDP()
            action = {"op": "click", "target": node["id"], "irreversible": True,
                      "expect": {"target_states": {"selected": "true"}}}
            with self.assertRaises(executor.ActionError):
                executor.run_ax_action(cdp, "s1", [node], action, lambda: next(snapshots),
                                       retries=3, wait_ms=0, sleep=lambda _: None, allow_coordinates=True)
            self.assertEqual(sum(name == "Runtime.callFunctionOn" for name, _, _ in cdp.calls), 1)
            self.assertFalse(any(name == "Input.dispatchMouseEvent" for name, _, _ in cdp.calls))
            # The reversible legacy retry must also rebind identity, not merely
            # trust a post-read replacement that shares the old id.
            node = make_node(values["backend"])
            changed = dict(node, role="link", name="Different destination")
            snapshots = iter(([changed], [changed]))
            cdp = CDP()
            action = {"op": "click", "target": node["id"],
                      "expect": {"target_states": {"selected": "true"}}}
            with self.assertRaises(executor.ActionError):
                executor.run_ax_action(cdp, "s1", [node], action, lambda: next(snapshots),
                                       retries=3, wait_ms=0, sleep=lambda _: None)
            self.assertEqual(sum(name == "Runtime.callFunctionOn" for name, _, _ in cdp.calls), 1)
        _bounded_property(self, "legacy_retry_freshness", SEED + 4,
                          lambda rng: {"backend": rng.randint(1, 65535)}, check, lambda v: v["backend"] > 0)


class TestControlledSchedules(unittest.TestCase):
    def test_revocation_during_fresh_read_32_replayable_schedules(self):
        rng = random.Random(SEED + 5)
        kinds = ["focus", "epoch", "current", "lease"] * 8
        rng.shuffle(kinds)
        for index, kind in enumerate(kinds):
            case = Case(role="textbox")
            rendezvous = threading.Barrier(2, timeout=2)
            changed = threading.Event()
            failures, outcomes = [], []

            def reader():
                rendezvous.wait()
                if not changed.wait(2):
                    raise TimeoutError("revoker did not signal")
                return [case.node]

            def actor():
                try:
                    case.run(action=dict(case.action, op="press", key="Enter"), reader=reader)
                    outcomes.append("sent")
                except executor.ActionError:
                    outcomes.append("refused")
                except BaseException as exc:
                    failures.append(repr(exc))

            def revoker():
                try:
                    rendezvous.wait()
                    if kind == "focus":
                        case.observation.state["focused_node_id"] = "sibling"
                    elif kind == "epoch":
                        case.registry.record = replace(case.record, epoch=case.record.epoch + 1)
                    elif kind == "current":
                        case.registry.current = None
                    else:
                        case.locks.current = None
                except BaseException as exc:
                    failures.append(repr(exc))
                finally:
                    changed.set()

            threads = [threading.Thread(target=actor, name="jq3-actor-%d" % index),
                       threading.Thread(target=revoker, name="jq3-revoker-%d" % index)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
            leaked = [thread.name for thread in threads if thread.is_alive()]
            self.assertEqual(leaked, [], "leaked threads: seed=%d schedule=%d kind=%s" % (SEED + 5, index, kind))
            self.assertEqual(failures, [], "incomplete schedule %d/%s" % (index, kind))
            self.assertEqual(outcomes, ["refused"], "seed=%d schedule=%d minimal_fixture=%s" % (SEED + 5, index, kind))
            self.assertEqual(case.cdp.calls, [], "refusal must precede all transport")
        self.assertEqual(len(kinds), SCHEDULES)
        print("VM schedules " + json.dumps({"seed": SEED + 5, "completed": len(kinds),
                                            "worker_threads": 2, "wait_seconds": 2,
                                            "leaked_threads": 0}, sort_keys=True))


class TestMutationProofs(unittest.TestCase):
    def test_catalogue_and_selected_boundary_guard_swap_mutants(self):
        def effect_assertion(module):
            case = Case()
            wrong = dict(case.node, attrs=(("selected", "false"),))
            snapshots = iter(([case.node], [wrong]))
            result = case.run(module, reader=lambda: next(snapshots))
            self.assertEqual((result["ok"], result["verified"]), (0, 0),
                             "successful transport and changed state cannot satisfy a false expectation")

        def role_assertion(module):
            case = Case()
            state = copy.deepcopy(case.observation.state)
            state["nodes"][0]["role"] = "link"
            refused = False
            try:
                module._authorized_input_events(case.action, case.node, state)
            except module.ActionError:
                refused = True
            self.assertTrue(refused, "pointer helper must reject observation/AX role disagreement")

        def one_retry_assertion(module):
            case = Case()
            accepted = True
            try:
                module._validate_inputs([case.node], case.action, lambda: [case.node], 1, 0, lambda _: None, False)
            except module.ActionError:
                accepted = False
            self.assertTrue(accepted, "the exact minimum retries=1 is valid")

        def modifier_assertion(module):
            case = Case()
            refused = False
            try:
                module._validate_inputs([case.node], {"op": "press", "target": case.node["id"],
                                                     "key": "Enter", "modifiers": 16},
                                        lambda: [case.node], 1, 0, lambda _: None, False)
            except module.ActionError:
                refused = True
            self.assertTrue(refused, "modifiers=16 must remain outside the integer [0,15] domain")

        def reader_assertion(module):
            case = Case()
            refused = False
            try:
                module._validate_inputs([case.node], case.action, None, 1, 0, lambda _: None, False)
            except module.ActionError:
                refused = True
            self.assertTrue(refused, "missing reader must fail before transport")

        def swap_assertion(module):
            node = make_node()
            self.assertTrue(module._verify_expect([node], node, None), "correct target lookup arguments must preserve identity")

        extras = (
            (Mutation("R7-B-comparison-retries", "_validate_inputs", "Compare", "retries < 1", "B", "ops[0]"), one_retry_assertion),
            (Mutation("R7-B-integer-modifier-limit", "_validate_inputs", "Constant", "15", "B", "value"), modifier_assertion),
            (Mutation("R7-D-reader-guard", "_validate_inputs", "If",
                      'if not callable(read_nodes):\n        raise ActionError("read_nodes must be callable")', "D", "test"), reader_assertion),
            (Mutation("R7-S-target-lookup", "_verify_expect", "Call", '_node_by_id(fresh_nodes, target_node["id"])',
                      "S", "first_two_distinct_nonstarred_args"), swap_assertion),
        )
        proofs = []
        for spec, assertion in ((CATALOGUE[0], effect_assertion), (CATALOGUE[1], role_assertion)) + extras:
            with self.subTest(mutation=spec.identifier):
                proof = prove(SOURCE, spec, assertion)
                proofs.append(proof)
                self.assertEqual(proof.control, "passed", repr(proof))
                self.assertEqual(proof.disposition, "kill", repr(proof))
        print("VM mutation " + json.dumps({"controls_passed": sum(p.control == "passed" for p in proofs),
                                          "kills": [p.identifier for p in proofs if p.disposition == "kill"],
                                          "survivors": [p.identifier for p in proofs if p.disposition == "survivor"],
                                          "proven_equivalents": [], "errors": [],
                                          "source_sha256": hashlib.sha256(SOURCE.encode()).hexdigest()}, sort_keys=True))

    def test_operator_mappings_and_structural_selectors(self):
        compare_spellings = {ast.Lt: "<", ast.Gt: ">", ast.LtE: "<=", ast.GtE: ">=", ast.Eq: "==",
                             ast.NotEq: "!=", ast.Is: "is", ast.IsNot: "is not", ast.In: "in", ast.NotIn: "not in"}
        for operator, replacements in (("O", _OPPOSITES), ("B", _BOUNDARIES)):
            for original, replacement in replacements.items():
                expression = "left %s right" % compare_spellings[original]
                source = "def probe(left, right):\n    return %s\n" % expression
                spec = Mutation("mapping", "probe", "Compare", expression, operator, "ops[0]")
                changed = mutate(source, spec)
                node = next(n for n in ast.walk(changed) if isinstance(n, ast.Compare))
                self.assertIs(type(node.ops[0]), replacement)
        for expression, node_type, expected in (
                ("left and right", "BoolOp", ast.Or), ("left or right", "BoolOp", ast.And),
                ("left + right", "BinOp", ast.Sub), ("left - right", "BinOp", ast.Add),
                ("left * right", "BinOp", ast.FloorDiv), ("left // right", "BinOp", ast.Mult)):
            tree = mutate("def probe(left, right):\n    return %s\n" % expression,
                          Mutation("mapping", "probe", node_type, expression, "O", "op"))
            selected = next(n for n in ast.walk(tree) if type(n).__name__ == node_type)
            self.assertIs(type(selected.op), expected)
        source = "def probe():\n    return 7\n"
        tree = mutate(source, Mutation("integer", "probe", "Constant", "7", "B", "value"))
        self.assertEqual(next(n for n in ast.walk(tree) if isinstance(n, ast.Constant)).value, 8)
        with self.assertRaises(AnchorError):
            mutate("def probe():\n    return True\n", Mutation("boolean", "probe", "Constant", "True", "B", "value"))
        with self.assertRaises(AnchorError):
            mutate("def probe(x):\n    return x < 7\n", Mutation("wrong-selector", "probe", "Compare", "x < 7", "B", "value"))
        source = "def probe(x):\n    assert x > 0\n"
        tree = mutate(source, Mutation("assert", "probe", "Assert", "assert x > 0", "D", "test"))
        self.assertIs(next(n for n in ast.walk(tree) if isinstance(n, ast.Assert)).test.value, True)
        source = "def probe(fn, x, y, rest):\n    return fn(*rest, x, x, y)\n"
        tree = mutate(source, Mutation("swap", "probe", "Call", "fn(*rest, x, x, y)", "S", "first_two_distinct_nonstarred_args"))
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
        self.assertIsInstance(call.args[0], ast.Starred)
        self.assertEqual([n.id for n in call.args[1:]], ["y", "x", "x"])

    def test_missing_ambiguous_and_wrong_qualified_anchors_are_not_kills(self):
        source = "def probe(x):\n    return x < 1\n"
        spec = Mutation("missing", "probe", "Compare", "x < 2", "O", "ops[0]")
        for source_text, selected in (
                (source, spec),
                (source, replace(spec, original="x < 1", qualified_function="other")),
                ("def probe(x):\n    return (x < 1) or (x < 1)\n", replace(spec, original="x < 1"))):
            result = prove(source_text, selected, lambda module: self.assertTrue(module.probe(0)))
            self.assertEqual((result.disposition, result.control), ("anchor_error", "passed"))
        nested = "def outer(x):\n    def probe():\n        return x < 1\n    return probe()\n"
        result = prove(nested, replace(spec, original="x < 1", qualified_function="outer.probe"),
                       lambda module: self.assertTrue(module.outer(0)))
        self.assertEqual(result.disposition, "kill")

    def test_dispositions_and_module_registration_restore_are_separate(self):
        source = "def probe(x):\n    return x < 1\n"
        spec = Mutation("dispositions", "probe", "Compare", "x < 1", "O", "ops[0]")
        passed = lambda module: self.assertIsInstance(module.probe(0), bool)
        survivor = prove(source, spec, passed)
        self.assertEqual(survivor.disposition, "survivor")
        control = prove(source, spec, lambda module: self.assertFalse(module.probe(0)))
        self.assertEqual(control.disposition, "failed_control")
        compile_error = prove("def broken(:\n", spec, passed)
        self.assertEqual(compile_error.disposition, "compile_error")
        import_error = prove("import _jq3_absent_dependency_7762a765\n" + source, spec, passed)
        self.assertEqual(import_error.disposition, "import_error")
        runtime = prove(source, spec, lambda module: 1 / 0)
        self.assertEqual(runtime.disposition, "runtime_error")
        timeout = prove(source, spec, lambda module: (_ for _ in ()).throw(ProofTimeout("bounded test")))
        self.assertEqual(timeout.disposition, "timeout")
        timeout_source = "def probe(x):\n    if x < 1:\n        return True\n    while True:\n        pass\n"
        timeout_mutant = prove(timeout_source, spec, lambda module: self.assertTrue(module.probe(0)), timeout=0.03)
        self.assertEqual((timeout_mutant.disposition, timeout_mutant.control), ("timeout", "passed"))
        import_source = "def probe(x):\n    if x < 1:\n        return True\n    import _jq3_absent_dependency_7762a765\n"
        import_mutant = prove(import_source, spec, lambda module: self.assertTrue(module.probe(0)))
        self.assertEqual((import_mutant.disposition, import_mutant.control), ("import_error", "passed"))
        # An AssertionError raised by module code is an execution error, not a semantic kill.
        source_with_assert = "def probe(x):\n    assert x < 1\n    return True\n"
        result = prove(source_with_assert, spec, lambda module: self.assertTrue(module.probe(0)))
        self.assertEqual(result.disposition, "runtime_error")
        equivalent_source = "def probe(x):\n    return (x or True) and True\n"
        equivalent_spec = Mutation("equivalent", "probe", "BoolOp", "(x or True) and True", "O", "op")

        def exhaustive_boolean_equivalence(original, mutated):
            # The declared domain is exactly booleans; enumerate it completely.
            with _private_module(compile(original, "equivalence", "exec"), "equivalence") as real:
                with _private_module(compile(mutated, "equivalence", "exec"), "equivalence") as alternate:
                    self.assertEqual([real.probe(x) for x in (False, True)],
                                     [alternate.probe(x) for x in (False, True)])
            return "Exhaustive domain {False, True}: (x or True) and/or True is True for both inputs."

        equivalent = prove(equivalent_source, equivalent_spec,
                           lambda module: self.assertTrue(all(module.probe(x) is True for x in (False, True))),
                           equivalent=exhaustive_boolean_equivalence)
        self.assertEqual(equivalent.disposition, "proven_equivalent")
        dataclass_source = ("from __future__ import annotations\nfrom dataclasses import dataclass\n"
                            "@dataclass(frozen=True)\nclass Value:\n    count: int\n"
                            "def probe(x):\n    return Value(x < 1)\n")
        before = dict(sys.modules)
        bytecode = sys.dont_write_bytecode
        registration = prove(dataclass_source, spec, lambda module: self.assertTrue(module.probe(0).count))
        self.assertEqual(registration.disposition, "kill")
        self.assertEqual(dict(sys.modules), before)
        self.assertEqual(sys.dont_write_bytecode, bytecode)
        self.assertEqual({survivor.disposition, control.disposition, compile_error.disposition,
                          import_error.disposition, runtime.disposition, timeout.disposition,
                          equivalent.disposition, registration.disposition},
                         {"survivor", "failed_control", "compile_error", "import_error", "runtime_error",
                          "timeout", "proven_equivalent", "kill"})


if __name__ == "__main__":
    unittest.main()
