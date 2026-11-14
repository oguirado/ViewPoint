"""Acceptance and isolation verification suite for jq3-r-r4-g1-multiwin-isolation-verifier.

Covers:
- Action-isolation verifier over issued-lease metadata, focus ownership, epoch, and
  pre-dispatch frame digest for CDP (`wender.act.multiwin_isolation_verifier`).
- Godot T2 action-isolation verification over trusted-context mandates, command
  seq/tick/basis binding, and single-use confirmation proposals with in-RAM sink list.
- Strict separation of authorization, dispatch, and verified effect; stale or foreign
  bindings fail before the fake actor is invoked, and deterministic audit output
  identifies the target and violated predicate.
- Complete in-memory AST prove harness supporting operators O, B (comparison and
  exact integer Constant separately), D (raising If and Assert), and S (Call arg swap),
  preserving all catalogue source anchors and structural selectors, and killing
  M-wender-act-cdp-executor-1 and M-wender-act-cdp-executor-2.
- Seeded stdlib random property loops targeting 64 valid cases per domain with at most
  32 validity-preserving shrink attempts reporting seed, case, and minimal fixture.
- Shared-state concurrency suite across 32 replayable schedules with at most 4 threads,
  barriers, controlled Events, and 2-second waits, verifying zero leaked threads.
- All timings labelled VM; frames remain in RAM (numpy uint8 arrays).
"""
from __future__ import annotations

import ast
import copy
from dataclasses import dataclass
import hashlib
import inspect
import json
import random
import re
import sys
import threading
import time
import types
from types import SimpleNamespace
from typing import Any, Callable
import unittest

import numpy as np

import wender.act.cdp_executor as cdp_executor_mod
from wender.act.cdp_executor import ActionError, OPS, KEYS, _verify_expect, run_ax_action
import wender.act.focus_policy as focus_policy_mod
from wender.act.focus_policy import (
    ActorLease,
    FocusClass,
    FocusLease,
    FocusPolicy,
    FocusRefused,
    Handle,
    Observation,
    classify,
)
import wender.act.godot_executor as godot_executor_mod
from wender.act.godot_executor import (
    CONFIRM_BINDING_KEYS,
    IRREVERSIBLE_COMMANDS,
    OWNER_RESERVED_COMMANDS,
    STATUS_ADMITTED,
    STATUS_REJECTED,
    GodotExecutor,
    map_ui_action,
)
import wender.act.multiwin_isolation_verifier as verifier_mod
from wender.act.multiwin_isolation_verifier import (
    CDP_PREDICATES,
    GODOT_PREDICATES,
    STAGES,
    CdpIsolationVerifier,
    GodotIsolationVerifier,
    IsolationViolationError,
    SiblingDependencyAbsentError,
    StructuralLockOwnership,
    StructuralSessionRegistry,
    StructuralTargetRecord,
    StructuralWriterFence,
    assert_audit_target_predicate,
    authorize_cdp_action,
    authorize_godot_command,
    build_audit_record,
    format_audit_record,
    require_sibling_dependency,
    verify_cdp_isolation,
    verify_godot_isolation,
)
from wender.perceive import ax
from wender.perceive.godot import INBOX_RECORD_KEYS, read_outbox_batches
from wender.perceive.session_registry import (
    FrameRef,
    Handle as CanonicalHandle,
    Observation as CanonicalObservation,
    PerceptionResult,
)
from wender.state import schema

VM_SUITE_BUDGET_S = 20.0
PROPERTY_TARGET_CASES = 64
MAX_SHRINK_ATTEMPTS = 32
CDP_PROPERTY_SEED = 0x4A513352
GODOT_PROPERTY_SEED = 0x474F4454
CONCURRENCY_TARGET_SCHEDULES = 32
MAX_CONCURRENCY_THREADS = 4
SYNC_TIMEOUT_S = 2.0


class FakeMonotonicClock:
    """Deterministic monotonic nanosecond clock in RAM."""

    def __init__(self, initial_ns: int = 100) -> None:
        self.value = int(initial_ns)

    def __call__(self) -> int:
        return self.value

    def advance(self, delta_ns: int) -> int:
        self.value += int(delta_ns)
        return self.value


class FakeCDPActor:
    """Per-instance fake CDP transport recording calls in RAM."""

    def __init__(self, fail_method: str | None = None) -> None:
        self.calls: list[tuple[str, Any, str | None]] = []
        self.fail_method = fail_method

    def send(self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None) -> dict[str, Any]:
        self.calls.append((method, copy.deepcopy(params), session_id))
        if method == self.fail_method:
            raise RuntimeError("simulated transport ambiguity")
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "element-1"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"type": "undefined"}}
        if method == "DOM.getBoxModel":
            return {"model": {"border": [10, 20, 30, 20, 30, 40, 10, 40]}}
        return {}


def make_ax_node(
    *,
    ident: str = "n1",
    role: str = "button",
    name: str = "Do it",
    attrs: tuple[tuple[str, str], ...] = (),
    backend: int = 41,
    ordinal: int = 0,
    box: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "id": ident,
        "role": role,
        "name": name,
        "depth": 0,
        "attrs": attrs,
        "ancestors": (),
        "ordinal": ordinal,
        "cardinality": 1,
        "backend_id": backend,
        "box": [10, 20, 20, 20] if box is None else list(box),
    }


def make_ram_frame_ref(
    handle: CanonicalHandle,
    *,
    epoch: int = 2,
    frame_seq: int = 7,
    width: int = 16,
    height: int = 12,
    seed_byte: int = 42,
) -> FrameRef:
    rgb = np.full((height, width, 3), seed_byte % 256, dtype=np.uint8)
    rgb[0, 0, 0] = (seed_byte + epoch + frame_seq) % 256
    return FrameRef.from_rgb(handle, epoch, frame_seq, rgb)


def encode_godot_batch_ram(records: list[dict[str, Any]]) -> bytes:
    lines = [schema.dumps(r) for r in records]
    return ("\n".join(lines) + "\n").encode("utf-8")


def decode_godot_batch_ram(payload: bytes) -> list[dict[str, Any]]:
    text = payload.decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def make_godot_schemas() -> dict[str, Any]:
    schemas: dict[str, Any] = {
        "buy_asset": {
            "kind": "economic",
            "args": {"asset_type": "nonempty_str", "quantity": "pos_int"},
            "required": ["asset_type", "quantity"],
        },
        "start_process": {
            "kind": "economic",
            "args": {"recipe_id": "nonempty_str"},
            "required": ["recipe_id"],
        },
        "new_game": {
            "kind": "current",
            "boundary": "current",
            "economic": False,
            "args": {"seed": "nonneg_int"},
            "required": ["seed"],
        },
        "advance": {
            "args": {},
        },
        "pause": {
            "args": {},
        },
        "open_screen": {
            "args": {"screen_id": "nonempty_str"},
            "required": ["screen_id"],
        },
        "save": {
            "kind": "at_rest",
            "boundary": "current",
            "economic": False,
            "args": {"slot_name": "nonempty_str"},
            "required": ["slot_name"],
        },
    }
    for cmd in IRREVERSIBLE_COMMANDS:
        schemas[cmd] = {
            "kind": "economic",
            "irreversible": True,
            "args": {
                "target_id": "nonempty_str",
                "terms": "dict",
            },
            "required": ["target_id", "terms"],
        }
    return schemas


class _DriftingRoleNodeDict(dict):
    """Dict subclass simulating role drift between initial observation check and _authorized_input_events."""

    def __init__(self, base: dict[str, Any], *, drifted_role: str) -> None:
        super().__init__(base)
        self._drifted_role = drifted_role
        self._role_reads = 0

    def get(self, key: str, default: Any = None) -> Any:
        if key == "role":
            self._role_reads += 1
            if self._role_reads >= 2:
                return self._drifted_role
        return super().get(key, default)


# ============================================================================
# Embedded In-Memory AST Prove Harness
# ============================================================================

@dataclass(frozen=True)
class MutantSpec:
    """Catalogue or structural AST mutation obligation specification."""

    mutant_id: str
    operator: str  # 'O', 'B', 'D', 'S'
    file_path: str
    line: int
    qualname: str
    node_source: str
    selector: str | None = None


@dataclass(frozen=True)
class MutantResult:
    """Disposition record produced by InMemoryAstProveHarness."""

    mutant_id: str
    disposition: str
    detail: str


CATALOGUE_MUTATION_OBLIGATIONS: tuple[MutantSpec, ...] = (
    MutantSpec(
        mutant_id="M-wender-act-cdp-executor-1",
        operator="O",
        file_path="wender/act/cdp_executor.py",
        line=854,
        qualname="_run_authorized_ax_action",
        node_source="_verify_expect(post, target, expect)\n and (expect is not None or before_sha differs from after_sha)",
        selector=None,
    ),
    MutantSpec(
        mutant_id="M-wender-act-cdp-executor-2",
        operator="O",
        file_path="wender/act/cdp_executor.py",
        line=742,
        qualname="_authorized_input_events",
        node_source='obs_node.get("role") differs from node.get("role")',
        selector="ops element 0",
    ),
)


class InMemoryAstProveHarness:
    """Complete in-memory AST mutation prove harness with strict anchor and selector matching.

    Dispositions (mutually exclusive; only 'KILLED' is a kill):
      - 'KILLED': positive control passed on real code; mutated code raised semantic AssertionError.
      - 'FAILED_CONTROL': positive control failed on real unmutated code.
      - 'MISSING_ANCHOR': 0 or >1 AST nodes matched (file, line, qualname, node_source, selector)
        or operator/selector was incompatible with the matched AST node.
      - 'COMPILE_IMPORT_ERROR': compiling or executing the mutated module failed.
      - 'TIMEOUT': probe execution exceeded timeout_s.
      - 'RUNTIME_ERROR': mutated code raised a non-AssertionError exception.
      - 'PROVEN_EQUIVALENT': mutated code survived and equivalence_proof verified equivalence.
      - 'SURVIVED': mutated code completed without raising AssertionError.
    """

    _COMPARE_O_MAP: dict[type[ast.cmpop], type[ast.cmpop]] = {
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

    _COMPARE_B_MAP: dict[type[ast.cmpop], type[ast.cmpop]] = {
        ast.Lt: ast.LtE,
        ast.LtE: ast.Lt,
        ast.Gt: ast.GtE,
        ast.GtE: ast.Gt,
    }

    _BOOLOP_O_MAP: dict[type[ast.boolop], type[ast.boolop]] = {
        ast.And: ast.Or,
        ast.Or: ast.And,
    }

    _BINOP_O_MAP: dict[type[ast.operator], type[ast.operator]] = {
        ast.Add: ast.Sub,
        ast.Sub: ast.Add,
        ast.Mult: ast.FloorDiv,
        ast.FloorDiv: ast.Mult,
    }

    @staticmethod
    def _normalize_anchor_text(text: str) -> str:
        norm = text.replace(" differs from ", " != ")
        lines = [re.sub(r"[ \t]+", " ", line.strip()) for line in norm.strip().splitlines()]
        return "\n".join(line for line in lines if line)

    @staticmethod
    def _resolve_qualname_scopes(tree: ast.AST, module_name: str, qualname: str) -> list[ast.AST]:
        target_parts = qualname.split(".")
        mod_parts = module_name.split(".")
        if target_parts[: len(mod_parts)] == mod_parts:
            rel_parts = tuple(target_parts[len(mod_parts) :])
        else:
            rel_parts = tuple(target_parts)
        if not rel_parts:
            return []

        matched: list[ast.AST] = []

        def _walk_scope(node: ast.AST, path: tuple[str, ...]) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    next_path = path + (child.name,)
                    if next_path == rel_parts:
                        matched.append(child)
                    else:
                        _walk_scope(child, next_path)

        _walk_scope(tree, ())
        return matched

    @staticmethod
    def _parse_op_index(selector: str | None) -> int | None:
        if selector is None or selector in ("", "root", "compare", "boolop", "binop"):
            return 0
        m = re.fullmatch(r"ops(?:\s+element\s+|\s*\[\s*)(\d+)\s*\]?", selector.strip())
        if m:
            return int(m.group(1))
        return None

    @classmethod
    def _node_matches_operator_kind(cls, node: ast.AST, operator: str, selector: str | None) -> bool:
        sel = (selector or "").strip()
        if operator == "O":
            if isinstance(node, ast.Compare):
                return cls._parse_op_index(selector) is not None
            if isinstance(node, (ast.BoolOp, ast.BinOp)):
                return sel in ("", "root", "boolop", "binop")
            return False
        if operator == "B":
            if sel.startswith("ops") or sel in ("compare", "comparison"):
                return isinstance(node, ast.Compare)
            if sel in ("constant", "int_constant"):
                return isinstance(node, ast.Constant) and type(node.value) is int
            if sel == "":
                return isinstance(node, ast.Compare) or (isinstance(node, ast.Constant) and type(node.value) is int)
            return False
        if operator == "D":
            if sel == "assert_test":
                return isinstance(node, ast.Assert)
            if sel == "if_test":
                return isinstance(node, ast.If)
            if sel == "":
                return isinstance(node, (ast.If, ast.Assert))
            return False
        if operator == "S":
            return isinstance(node, ast.Call) and sel in ("", "call_args", "args[0:2]")
        return False

    @classmethod
    def _apply_operator_to_node(cls, node: ast.AST, operator: str, selector: str | None) -> bool:
        sel = (selector or "").strip()
        if operator == "O":
            if isinstance(node, ast.Compare):
                idx = cls._parse_op_index(selector)
                if idx is None or idx < 0 or idx >= len(node.ops):
                    return False
                replacement = cls._COMPARE_O_MAP.get(type(node.ops[idx]))
                if replacement is None:
                    return False
                node.ops[idx] = replacement()
                return True
            if isinstance(node, ast.BoolOp):
                replacement = cls._BOOLOP_O_MAP.get(type(node.op))
                if replacement is None:
                    return False
                node.op = replacement()
                return True
            if isinstance(node, ast.BinOp):
                replacement = cls._BINOP_O_MAP.get(type(node.op))
                if replacement is None:
                    return False
                node.op = replacement()
                return True
            return False

        if operator == "B":
            # Never substitute a constant increment for a comparison selector, or vice versa.
            if sel.startswith("ops") or sel in ("compare", "comparison"):
                if not isinstance(node, ast.Compare):
                    return False
                idx = cls._parse_op_index(selector) if sel.startswith("ops") else 0
                if idx is None or idx < 0 or idx >= len(node.ops):
                    return False
                replacement = cls._COMPARE_B_MAP.get(type(node.ops[idx]))
                if replacement is None:
                    return False
                node.ops[idx] = replacement()
                return True
            if sel in ("constant", "int_constant"):
                if not isinstance(node, ast.Constant) or type(node.value) is not int:
                    return False
                node.value = node.value + 1
                return True
            if sel == "":
                if isinstance(node, ast.Compare):
                    if len(node.ops) != 1:
                        return False
                    replacement = cls._COMPARE_B_MAP.get(type(node.ops[0]))
                    if replacement is None:
                        return False
                    node.ops[0] = replacement()
                    return True
                if isinstance(node, ast.Constant) and type(node.value) is int:
                    node.value = node.value + 1
                    return True
            return False

        if operator == "D":
            if isinstance(node, ast.If):
                if sel not in ("", "if_test"):
                    return False
                has_raise = any(isinstance(sub, ast.Raise) for sub in ast.walk(ast.Module(body=node.body, type_ignores=[])))
                if not has_raise:
                    return False
                node.test = ast.copy_location(ast.Constant(value=False), node.test)
                return True
            if isinstance(node, ast.Assert):
                if sel not in ("", "assert_test"):
                    return False
                node.test = ast.copy_location(ast.Constant(value=True), node.test)
                return True
            return False

        if operator == "S":
            if not isinstance(node, ast.Call) or sel not in ("", "call_args", "args[0:2]"):
                return False
            if len(node.args) < 2:
                return False
            a0, a1 = node.args[0], node.args[1]
            if isinstance(a0, ast.Starred) or isinstance(a1, ast.Starred):
                return False
            if ast.dump(a0) == ast.dump(a1):
                return False
            node.args[0], node.args[1] = a1, a0
            return True

        return False

    @classmethod
    def prove(
        cls,
        *,
        module: types.ModuleType,
        spec: MutantSpec,
        probe: Callable[[types.ModuleType], None],
        equivalence_proof: Callable[[types.ModuleType, types.ModuleType], bool] | None = None,
        timeout_s: float = 2.0,
        mutate_source_hook: Callable[[str], str] | None = None,
    ) -> MutantResult:
        # Step 1: Run the exact same assertion on the real unmutated code first.
        try:
            probe(module)
        except BaseException as exc:  # noqa: BLE001
            return MutantResult(
                spec.mutant_id,
                "FAILED_CONTROL",
                f"positive control failed on real code with {type(exc).__name__}: {exc}",
            )

        try:
            source = inspect.getsource(module)
        except Exception as exc:
            return MutantResult(spec.mutant_id, "MISSING_ANCHOR", f"unable to read module source: {exc}")

        if not spec.node_source or not isinstance(spec.node_source, str):
            return MutantResult(spec.mutant_id, "MISSING_ANCHOR", "empty node_source anchor")

        try:
            tree = ast.parse(source, filename=module.__file__ or "<memory>")
        except SyntaxError as exc:
            return MutantResult(spec.mutant_id, "COMPILE_IMPORT_ERROR", f"source parse failed: {exc}")

        scopes = cls._resolve_qualname_scopes(tree, module.__name__, spec.qualname)
        if len(scopes) != 1:
            return MutantResult(
                spec.mutant_id,
                "MISSING_ANCHOR",
                f"expected unique scope for {spec.qualname!r}, found {len(scopes)}",
            )

        want_norm = cls._normalize_anchor_text(spec.node_source)
        candidates: list[ast.AST] = []
        for cand in ast.walk(scopes[0]):
            lineno = getattr(cand, "lineno", None)
            if lineno != spec.line:
                continue
            if not cls._node_matches_operator_kind(cand, spec.operator, spec.selector):
                continue
            seg = ast.get_source_segment(source, cand)
            if seg is None:
                continue
            if cls._normalize_anchor_text(seg) == want_norm:
                candidates.append(cand)

        if len(candidates) != 1:
            return MutantResult(
                spec.mutant_id,
                "MISSING_ANCHOR",
                f"expected exactly 1 AST node at line {spec.line} matching {spec.node_source!r} "
                f"(selector={spec.selector!r}), found {len(candidates)}",
            )

        target_node = candidates[0]
        if not cls._apply_operator_to_node(target_node, spec.operator, spec.selector):
            return MutantResult(
                spec.mutant_id,
                "MISSING_ANCHOR",
                f"operator {spec.operator!r} with selector {spec.selector!r} incompatible with matched node",
            )

        ast.fix_missing_locations(tree)

        saved_modules = dict(sys.modules)
        parent_pkg = sys.modules.get(module.__package__) if module.__package__ else None
        attr_name = module.__name__.rsplit(".", 1)[-1]
        original_pkg_attr = getattr(parent_pkg, attr_name, None) if parent_pkg is not None else None
        had_dont_write = sys.dont_write_bytecode

        try:
            sys.dont_write_bytecode = True
            try:
                if mutate_source_hook is not None:
                    mutated_src = mutate_source_hook(ast.unparse(tree))
                    code_obj = compile(mutated_src, filename=module.__file__ or "<ast-mutant>", mode="exec")
                else:
                    code_obj = compile(tree, filename=module.__file__ or "<ast-mutant>", mode="exec")
                mutated_mod = types.ModuleType(module.__name__)
                mutated_mod.__file__ = module.__file__
                mutated_mod.__package__ = module.__package__
                mutated_mod.__loader__ = getattr(module, "__loader__", None)
                mutated_mod.__spec__ = getattr(module, "__spec__", None)
                # Register in sys.modules before exec so @dataclass and private-module lookups succeed.
                sys.modules[module.__name__] = mutated_mod
                if parent_pkg is not None:
                    setattr(parent_pkg, attr_name, mutated_mod)
                exec(code_obj, mutated_mod.__dict__)
            except BaseException as exc:  # noqa: BLE001
                return MutantResult(
                    spec.mutant_id,
                    "COMPILE_IMPORT_ERROR",
                    f"compile/import failed with {type(exc).__name__}: {exc}",
                )

            outcome_box: dict[str, Any] = {}

            def _run_probe() -> None:
                try:
                    probe(mutated_mod)
                    outcome_box["status"] = "SURVIVED"
                except AssertionError as exc:
                    outcome_box["status"] = "KILLED"
                    outcome_box["detail"] = str(exc)
                except BaseException as exc:  # noqa: BLE001
                    outcome_box["status"] = "RUNTIME_ERROR"
                    outcome_box["detail"] = f"{type(exc).__name__}: {exc}"

            if timeout_s < 0.25:
                worker = threading.Thread(target=_run_probe, name=f"ast-prove-{spec.mutant_id}", daemon=True)
                worker.start()
                worker.join(timeout=timeout_s)
                if worker.is_alive():
                    return MutantResult(spec.mutant_id, "TIMEOUT", f"probe exceeded {timeout_s}s timeout")
            else:
                t0 = time.monotonic()
                _run_probe()
                if time.monotonic() - t0 > timeout_s:
                    return MutantResult(spec.mutant_id, "TIMEOUT", f"probe exceeded {timeout_s}s timeout")

            status = outcome_box.get("status", "SURVIVED")
            if status == "KILLED":
                return MutantResult(spec.mutant_id, "KILLED", outcome_box.get("detail", "semantic AssertionError"))
            if status == "RUNTIME_ERROR":
                return MutantResult(spec.mutant_id, "RUNTIME_ERROR", outcome_box.get("detail", "runtime error"))
            if equivalence_proof is not None and equivalence_proof(module, mutated_mod):
                return MutantResult(spec.mutant_id, "PROVEN_EQUIVALENT", "verified by equivalence_proof")
            return MutantResult(spec.mutant_id, "SURVIVED", "mutant survived probe without AssertionError")
        finally:
            sys.dont_write_bytecode = had_dont_write
            sys.modules.clear()
            sys.modules.update(saved_modules)
            if parent_pkg is not None and original_pkg_attr is not None:
                setattr(parent_pkg, attr_name, original_pkg_attr)


# ============================================================================
# Validity-Preserving Shrinkers for Property Testing
# ============================================================================

def shrink_cdp_fixture(
    fixture: dict[str, Any],
    is_valid: Callable[[dict[str, Any]], bool],
    fails_property: Callable[[dict[str, Any]], bool],
    *,
    max_attempts: int = MAX_SHRINK_ATTEMPTS,
) -> tuple[dict[str, Any], int]:
    """Shrink a failing CDP fixture in at most max_attempts validity-preserving steps."""
    if max_attempts < 1 or max_attempts > MAX_SHRINK_ATTEMPTS:
        raise RuntimeError(f"shrink budget {max_attempts} out of bounds [1..{MAX_SHRINK_ATTEMPTS}]")
    current = copy.deepcopy(fixture)
    attempts = 0

    candidates_generators = [
        lambda fx: dict(fx, extra_nodes=fx["extra_nodes"][: len(fx["extra_nodes"]) // 2]) if len(fx.get("extra_nodes", ())) > 0 else None,
        lambda fx: dict(fx, extra_nodes=[]) if len(fx.get("extra_nodes", ())) > 0 else None,
        lambda fx: dict(fx, epoch=max(1, fx["epoch"] // 2)) if fx.get("epoch", 1) > 1 else None,
        lambda fx: dict(fx, frame_seq=max(0, fx["frame_seq"] // 2)) if fx.get("frame_seq", 0) > 0 else None,
        lambda fx: dict(fx, generation=max(1, fx["generation"] - 1)) if fx.get("generation", 1) > 1 else None,
        lambda fx: dict(fx, expires_ns=max(fx.get("now_ns", 10) + 1, fx["expires_ns"] // 2)) if fx.get("expires_ns", 100) > fx.get("now_ns", 10) + 1 else None,
    ]

    for gen in candidates_generators:
        while True:
            if attempts >= max_attempts:
                raise RuntimeError(f"shrink budget exhausted after {attempts} attempts")
            cand = gen(current)
            if cand is None or cand == current:
                break
            attempts += 1
            if is_valid(cand) and fails_property(cand):
                current = cand
            else:
                break
    return current, attempts


def shrink_godot_fixture(
    fixture: dict[str, Any],
    is_valid: Callable[[dict[str, Any]], bool],
    fails_property: Callable[[dict[str, Any]], bool],
    *,
    max_attempts: int = MAX_SHRINK_ATTEMPTS,
) -> tuple[dict[str, Any], int]:
    """Shrink a failing Godot fixture in at most max_attempts validity-preserving steps."""
    if max_attempts < 1 or max_attempts > MAX_SHRINK_ATTEMPTS:
        raise RuntimeError(f"shrink budget {max_attempts} out of bounds [1..{MAX_SHRINK_ATTEMPTS}]")
    current = copy.deepcopy(fixture)
    attempts = 0

    candidates_generators = [
        lambda fx: dict(fx, committed_tick=fx["committed_tick"] // 2) if fx.get("committed_tick", 0) > 0 else None,
        lambda fx: dict(fx, quantity=max(1, fx["quantity"] // 2)) if fx.get("quantity", 1) > 1 else None,
        lambda fx: dict(fx, terms={"amount": max(1, fx["terms"].get("amount", 1) // 2)}) if isinstance(fx.get("terms"), dict) and fx["terms"].get("amount", 1) > 1 else None,
    ]

    for gen in candidates_generators:
        while True:
            if attempts >= max_attempts:
                raise RuntimeError(f"shrink budget exhausted after {attempts} attempts")
            cand = gen(current)
            if cand is None or cand == current:
                break
            attempts += 1
            if is_valid(cand) and fails_property(cand):
                current = cand
            else:
                break
    return current, attempts


# ============================================================================
# Main Test Suite
# ============================================================================

class TestJq3R4G1MultiwinIsolationVerifier(unittest.TestCase):
    """Acceptance test suite for jq3-r-r4-g1-multiwin-isolation-verifier."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._vm_start = time.monotonic()

    @classmethod
    def tearDownClass(cls) -> None:
        elapsed_vm = time.monotonic() - cls._vm_start
        if elapsed_vm > VM_SUITE_BUDGET_S:
            raise AssertionError(f"VM suite budget exceeded: {elapsed_vm:.3f}s VM > {VM_SUITE_BUDGET_S:.1f}s VM")

    def _build_cdp_env(
        self,
        *,
        cdp_mod: types.ModuleType = cdp_executor_mod,
        epoch: int = 2,
        frame_seq: int = 7,
        generation: int = 3,
        now_ns: int = 100,
        expires_ns: int = 500,
    ) -> SimpleNamespace:
        clock = FakeMonotonicClock(now_ns)
        handle_a = CanonicalHandle("cdp_tab", "tab-a", generation)
        handle_b = CanonicalHandle("cdp_tab", "tab-b", generation)
        lease_a = ActorLease(handle_a, "alice", generation, b"nonce-a", expires_ns)
        lease_b = ActorLease(handle_b, "bob", generation, b"nonce-b", expires_ns)
        rec_a = StructuralTargetRecord(handle_a, epoch=epoch, session_id="s1", closed=False, owned=True)
        rec_b = StructuralTargetRecord(handle_b, epoch=epoch, session_id="s2", closed=False, owned=True)
        registry = StructuralSessionRegistry([rec_a, rec_b])
        locks = StructuralLockOwnership([lease_a, lease_b])
        policy = FocusPolicy(registry, locks, clock=clock)
        policy.set_focus(handle_a, domain="cdp:s1", epoch=epoch)
        focus_a = policy.grant(lease_a, domain="cdp:s1")
        fence = StructuralWriterFence()
        cdp = FakeCDPActor()

        original_node = make_ax_node(ident="n1", role="button", name="Do it", attrs=(), backend=41, box=[10, 20, 20, 20])
        after_node = make_ax_node(
            ident="n1",
            role="button",
            name="Do it",
            attrs=(("selected", "true"),),
            backend=41,
            box=[10, 20, 20, 20],
        )
        frame_a = make_ram_frame_ref(handle_a, epoch=epoch, frame_seq=frame_seq)
        obs_a = CanonicalObservation(
            handle_a,
            epoch,
            frame_seq,
            1_000_000,
            {
                "handle": handle_a,
                "epoch": epoch,
                "frame_seq": frame_seq,
                "viewport": {"w": 800, "h": 600, "scroll_x": 0, "scroll_y": 0},
                "nodes": [copy.deepcopy(original_node)],
                "focused_node_id": "n1",
            },
            frame_a,
        )
        perc_a = PerceptionResult(handle_a, obs_a, None)
        self.assertIs(perc_a.observation, obs_a)
        registry.set_current_observation(obs_a)
        action = {
            "op": "click",
            "target": "n1",
            "expect": {"target_states": {"selected": "true"}},
        }
        verifier = CdpIsolationVerifier(registry, locks, policy, fence, clock=clock)
        return SimpleNamespace(
            clock=clock,
            handle_a=handle_a,
            handle_b=handle_b,
            lease_a=lease_a,
            lease_b=lease_b,
            registry=registry,
            locks=locks,
            policy=policy,
            focus_a=focus_a,
            fence=fence,
            cdp=cdp,
            original_node=original_node,
            after_node=after_node,
            frame_a=frame_a,
            obs_a=obs_a,
            action=action,
            verifier=verifier,
            cdp_mod=cdp_mod,
        )

    # ------------------------------------------------------------------------
    # 1. CDP Action-Isolation Verifier Tests
    # ------------------------------------------------------------------------

    def test_cdp_positive_isolation_separates_authorization_dispatch_and_verified_effect(self) -> None:
        env = self._build_cdp_env()
        snapshots = iter(([env.original_node], [env.after_node]))
        audit = env.verifier.verify_and_run(
            env.cdp,
            "s1",
            [env.original_node],
            env.action,
            lambda: next(snapshots),
            lease=env.lease_a,
            focus=env.focus_a,
            observation=env.obs_a,
            expected_frame_digest=env.frame_a.digest,
            fail_closed=True,
        )
        self.assertEqual(audit["ok"], 1)
        self.assertEqual(audit["surface"], "cdp")
        self.assertEqual(audit["target"], "cdp_tab:tab-a@3")
        self.assertIsNone(audit["violated_predicate"])
        self.assertEqual(audit["stage"], "verified_effect")
        self.assertEqual((audit["authorized"], audit["dispatched"], audit["verified_effect"]), (1, 1, 1))
        self.assertEqual(audit["stages"]["authorization"], {"ok": 1, "predicate": None})
        self.assertEqual(audit["stages"]["dispatch"], {"ok": 1, "attempts": 1, "predicate": None})
        self.assertEqual(audit["stages"]["verified_effect"]["ok"], 1)
        self.assertEqual(audit["stages"]["verified_effect"]["pre_digest"], env.frame_a.digest)
        self.assertEqual(env.fence.releases, 1)
        self.assertEqual(len(env.cdp.calls), 3)
        self.assertEqual(json.loads(format_audit_record(audit)), audit)

        # Single-use focus grant is now consumed; replay must fail in authorization before fake CDP actor runs.
        env.cdp.calls.clear()
        with self.assertRaises(IsolationViolationError) as ctx:
            env.verifier.verify_and_run(
                env.cdp,
                "s1",
                [env.original_node],
                env.action,
                lambda: [env.original_node],
                lease=env.lease_a,
                focus=env.focus_a,
                observation=env.obs_a,
                expected_frame_digest=env.frame_a.digest,
                fail_closed=True,
            )
        self.assertEqual(env.cdp.calls, [])
        self.assertEqual(ctx.exception.stage, "authorization")
        self.assertEqual(ctx.exception.predicate, "focus_ownership")
        self.assertEqual(ctx.exception.target, "cdp_tab:tab-a@3")

    def test_cdp_stale_or_foreign_bindings_fail_before_fake_actor_with_deterministic_audit(self) -> None:
        # 1. Unowned / forged lease nonce
        env = self._build_cdp_env()
        forged_lease = ActorLease(env.handle_a, "alice", 3, b"forged-nonce", 500)
        audit_forged = env.verifier.verify_and_run(
            env.cdp,
            "s1",
            [env.original_node],
            env.action,
            lambda: [env.original_node],
            lease=forged_lease,
            focus=env.focus_a,
            observation=env.obs_a,
        )
        self.assertEqual(env.cdp.calls, [])
        self.assertEqual(audit_forged["authorized"], 0)
        self.assertEqual(audit_forged["dispatched"], 0)
        self.assertEqual(audit_forged["violated_predicate"], "lease_ownership")
        self.assertEqual(audit_forged["target"], "cdp_tab:tab-a@3")

        # 2. Expired lease
        env = self._build_cdp_env()
        env.clock.value = 500
        audit_exp = env.verifier.verify_and_run(
            env.cdp,
            "s1",
            [env.original_node],
            env.action,
            lambda: [env.original_node],
            lease=env.lease_a,
            focus=env.focus_a,
            observation=env.obs_a,
        )
        self.assertEqual(env.cdp.calls, [])
        self.assertEqual(audit_exp["violated_predicate"], "lease_expiry")
        self.assertEqual(audit_exp["actor_invocations"], 0)

        # 3. Stale lease generation
        env = self._build_cdp_env()
        stale_gen_lease = SimpleNamespace(
            handle=env.handle_a,
            owner="alice",
            generation=2,
            nonce=b"nonce-a",
            expires_ns=500,
        )
        audit_gen = env.verifier.verify_and_run(
            env.cdp,
            "s1",
            [env.original_node],
            env.action,
            lambda: [env.original_node],
            lease=stale_gen_lease,
            focus=env.focus_a,
            observation=env.obs_a,
        )
        self.assertEqual(env.cdp.calls, [])
        self.assertEqual(audit_gen["violated_predicate"], "lease_generation")

        # 4. Foreign focus grant (target B focus presented for target A)
        env = self._build_cdp_env()
        env.policy.set_focus(env.handle_b, domain="cdp:s2", epoch=2)
        focus_b = env.policy.grant(env.lease_b, domain="cdp:s2")
        audit_focus = env.verifier.verify_and_run(
            env.cdp,
            "s1",
            [env.original_node],
            env.action,
            lambda: [env.original_node],
            lease=env.lease_a,
            focus=focus_b,
            observation=env.obs_a,
        )
        self.assertEqual(env.cdp.calls, [])
        self.assertEqual(audit_focus["violated_predicate"], "focus_ownership")

        # 5. Modal revocation on domain
        env = self._build_cdp_env()
        env.policy.set_modal(env.handle_b, domain="cdp:s1")
        audit_modal = env.verifier.verify_and_run(
            env.cdp,
            "s1",
            [env.original_node],
            env.action,
            lambda: [env.original_node],
            lease=env.lease_a,
            focus=env.focus_a,
            observation=env.obs_a,
        )
        self.assertEqual(env.cdp.calls, [])
        self.assertEqual(audit_modal["violated_predicate"], "focus_ownership")

        # 6. Stale geometry epoch after registry advance_epoch
        env = self._build_cdp_env()
        env.registry.advance_epoch(env.handle_a)
        audit_epoch = env.verifier.verify_and_run(
            env.cdp,
            "s1",
            [env.original_node],
            env.action,
            lambda: [env.original_node],
            lease=env.lease_a,
            focus=env.focus_a,
            observation=env.obs_a,
        )
        self.assertEqual(env.cdp.calls, [])
        self.assertEqual(audit_epoch["violated_predicate"], "epoch_binding")

        # 7. Pre-dispatch frame digest mismatch
        env = self._build_cdp_env()
        audit_digest = env.verifier.verify_and_run(
            env.cdp,
            "s1",
            [env.original_node],
            env.action,
            lambda: [env.original_node],
            lease=env.lease_a,
            focus=env.focus_a,
            observation=env.obs_a,
            expected_frame_digest="0" * 64,
        )
        self.assertEqual(env.cdp.calls, [])
        self.assertEqual(audit_digest["violated_predicate"], "frame_digest")

        # 8. Busy writer fence
        env = self._build_cdp_env()
        env.fence.busy = True
        audit_fence = env.verifier.verify_and_run(
            env.cdp,
            "s1",
            [env.original_node],
            env.action,
            lambda: [env.original_node],
            lease=env.lease_a,
            focus=env.focus_a,
            observation=env.obs_a,
        )
        self.assertEqual(env.cdp.calls, [])
        self.assertEqual(audit_fence["violated_predicate"], "writer_fence")

        # 9. Dispatched action whose post-state expectation is not met separates dispatch from verified_effect
        env = self._build_cdp_env()
        unchanged_snapshots = iter(([env.original_node], [env.original_node]))
        audit_unverified = env.verifier.verify_and_run(
            env.cdp,
            "s1",
            [env.original_node],
            env.action,
            lambda: next(unchanged_snapshots),
            lease=env.lease_a,
            focus=env.focus_a,
            observation=env.obs_a,
        )
        self.assertEqual((audit_unverified["authorized"], audit_unverified["dispatched"], audit_unverified["verified_effect"]), (1, 1, 0))
        self.assertEqual(audit_unverified["stage"], "verified_effect")
        self.assertEqual(audit_unverified["violated_predicate"], "verified_effect")
        self.assertGreater(len(env.cdp.calls), 0)

    def test_sibling_dependency_lazy_import_blocks_on_absence(self) -> None:
        loaded = require_sibling_dependency("wender.act.focus_policy")
        self.assertIs(loaded, focus_policy_mod)
        with self.assertRaises(SiblingDependencyAbsentError) as ctx:
            require_sibling_dependency("wender.act.nonexistent_future_sibling_deliverable")
        self.assertIn("AUSENTE", str(ctx.exception))

    # ------------------------------------------------------------------------
    # 2. Godot Action-Isolation Verifier Tests
    # ------------------------------------------------------------------------

    def test_godot_mandates_seq_tick_basis_and_single_use_proposals_with_ram_sink(self) -> None:
        sink: list[bytes] = []
        schemas = make_godot_schemas()
        basis = "b" * 64
        trusted_ctx = {
            "principal": "op-1",
            "channel": "t2",
            "slot_kind": "sandbox",
            "automation_active": True,
            "mandates": {"owner": True},
        }
        ex = GodotExecutor(trusted_ctx, schemas, encode_godot_batch_ram, sink.append)
        verifier = GodotIsolationVerifier(ex, sink)

        # 1. Current-boundary command (advance) at committed_tick=4, seq=1 admits and writes once.
        rec_adv = {"tick": 4, "seq": 1, "cmd": "advance", "args": {}}
        self.assertEqual(set(rec_adv.keys()), set(INBOX_RECORD_KEYS))
        audit1 = verifier.verify_and_dispatch(rec_adv, committed_tick=4, basis_sha256=basis, fail_closed=True)
        self.assertEqual((audit1["ok"], audit1["authorized"], audit1["dispatched"], audit1["verified_effect"]), (1, 1, 1, 1))
        self.assertEqual(len(sink), 1)
        self.assertEqual(ex.next_seq, 2)

        # 2. Wrong tick window for economic command (buy_asset requires committed_tick + 1 = 5, not 4)
        bad_tick_rec = {
            "tick": 4,
            "seq": 2,
            "cmd": "buy_asset",
            "args": {"asset_type": "gpu", "quantity": 2},
        }
        audit_tick = verifier.verify_and_dispatch(bad_tick_rec, committed_tick=4, basis_sha256=basis)
        self.assertEqual(audit_tick["ok"], 0)
        self.assertEqual(audit_tick["violated_predicate"], "tick_window")
        self.assertEqual(len(sink), 1)
        self.assertEqual(ex.next_seq, 2)

        # 3. Sequence gap (seq=3 when next_seq=2) rejected without sink write or burning seq
        gap_rec = {
            "tick": 5,
            "seq": 3,
            "cmd": "buy_asset",
            "args": {"asset_type": "gpu", "quantity": 2},
        }
        audit_seq = verifier.verify_and_dispatch(gap_rec, committed_tick=4, basis_sha256=basis)
        self.assertEqual(audit_seq["violated_predicate"], "sequence_binding")
        self.assertEqual(len(sink), 1)
        self.assertEqual(ex.next_seq, 2)

        # 4. Irreversible owner-reserved command (sell_company) without confirmation creates proposal
        irrev_rec = {
            "tick": 5,
            "seq": 2,
            "cmd": "sell_company",
            "args": {"target_id": "comp_acme", "terms": {"price": 1000}},
        }
        audit_prop = verifier.verify_and_dispatch(irrev_rec, committed_tick=4, basis_sha256=basis)
        self.assertEqual(audit_prop["ok"], 0)
        self.assertEqual(audit_prop["violated_predicate"], "confirmation_binding")
        self.assertEqual(audit_prop["target"], "comp_acme")
        self.assertEqual(audit_prop["proposal_modal_id"], "modal_sell_company_2")
        self.assertEqual(len(sink), 1)
        self.assertEqual(ex.next_seq, 2)

        # 5. Stale basis_sha256 on confirmation fails with basis_binding before sink write
        modal_id = "modal_sell_company_2"
        stale_confirm_rec = {
            "tick": 5,
            "seq": 2,
            "cmd": "confirm",
            "args": {
                "modal_id": modal_id,
                "cmd": "sell_company",
                "target_id": "comp_acme",
                "terms": {"price": 1000},
                "basis_sha256": "c" * 64,
            },
        }
        audit_stale = verifier.verify_and_dispatch(stale_confirm_rec, committed_tick=4, basis_sha256=basis)
        self.assertEqual(audit_stale["violated_predicate"], "basis_binding")
        self.assertEqual(len(sink), 1)

        # 6. Target mismatch on confirmation fails with confirmation_binding before sink write
        wrong_target_confirm = {
            "tick": 5,
            "seq": 2,
            "cmd": "confirm",
            "args": {
                "modal_id": modal_id,
                "cmd": "sell_company",
                "target_id": "comp_other",
                "terms": {"price": 1000},
                "basis_sha256": basis,
            },
        }
        audit_tm = verifier.verify_and_dispatch(wrong_target_confirm, committed_tick=4, basis_sha256=basis)
        self.assertEqual(audit_tm["violated_predicate"], "confirmation_binding")
        self.assertEqual(audit_tm["target"], "comp_other")
        self.assertEqual(len(sink), 1)

        # 7. Valid confirmation with exact CONFIRM_BINDING_KEYS admits, writes once to sink, and consumes proposal
        valid_confirm = {
            "tick": 5,
            "seq": 2,
            "cmd": "confirm",
            "args": {
                "modal_id": modal_id,
                "cmd": "sell_company",
                "target_id": "comp_acme",
                "terms": {"price": 1000},
                "basis_sha256": basis,
            },
        }
        self.assertEqual(frozenset(valid_confirm["args"].keys()), CONFIRM_BINDING_KEYS)
        audit_conf_ok = verifier.verify_and_dispatch(valid_confirm, committed_tick=4, basis_sha256=basis, fail_closed=True)
        self.assertEqual((audit_conf_ok["ok"], audit_conf_ok["authorized"], audit_conf_ok["dispatched"], audit_conf_ok["verified_effect"]), (1, 1, 1, 1))
        self.assertEqual(len(sink), 2)
        self.assertEqual(ex.next_seq, 3)

        # 8. Re-using the same confirmation proposal fails with proposal_single_use (USED_CONFIRM)
        replay_confirm = dict(valid_confirm, seq=3)
        audit_replay = verifier.verify_and_dispatch(replay_confirm, committed_tick=4, basis_sha256=basis)
        self.assertEqual(audit_replay["violated_predicate"], "proposal_single_use")
        self.assertEqual(len(sink), 2)
        self.assertEqual(ex.next_seq, 3)

        # 9. Missing owner mandate refuses owner-reserved command with trusted_mandate
        unmandated_sink: list[bytes] = []
        ex_no_owner = GodotExecutor(
            {"principal": "viewer", "slot_kind": "sandbox", "mandates": {}},
            schemas,
            encode_godot_batch_ram,
            unmandated_sink.append,
        )
        ver_no_owner = GodotIsolationVerifier(ex_no_owner, unmandated_sink)
        audit_mandate = ver_no_owner.verify_and_dispatch(
            {"tick": 5, "seq": 1, "cmd": "ipo", "args": {"target_id": "comp_acme", "terms": {"shares": 10}}},
            committed_tick=4,
            basis_sha256=basis,
        )
        self.assertEqual(audit_mandate["violated_predicate"], "trusted_mandate")
        self.assertEqual(unmandated_sink, [])

        # 10. Automated main-save irreversible command refused with main_save_consent (CONSENT_MAIN_SAVE)
        main_sink: list[bytes] = []
        ex_main = GodotExecutor(
            {"principal": "op", "slot_kind": "main", "automation_active": True, "mandates": {"owner": True}},
            schemas,
            encode_godot_batch_ram,
            main_sink.append,
        )
        ver_main = GodotIsolationVerifier(ex_main, main_sink)
        audit_main = ver_main.verify_and_dispatch(
            {"tick": 5, "seq": 1, "cmd": "ipo", "args": {"target_id": "comp_acme", "terms": {"shares": 10}}},
            committed_tick=4,
            basis_sha256=basis,
        )
        self.assertEqual(audit_main["violated_predicate"], "main_save_consent")
        self.assertEqual(main_sink, [])

    # ------------------------------------------------------------------------
    # 3. Catalogue Mutation Obligations & AST Prove Harness Tests
    # ------------------------------------------------------------------------

    def test_catalogue_mutant_m_wender_act_cdp_executor_1_killed(self) -> None:
        spec = CATALOGUE_MUTATION_OBLIGATIONS[0]
        self.assertEqual(spec.mutant_id, "M-wender-act-cdp-executor-1")
        self.assertEqual(spec.operator, "O")
        self.assertEqual(spec.line, 854)
        self.assertEqual(spec.qualname, "_run_authorized_ax_action")

        def _probe_m1(mod: types.ModuleType) -> None:
            env = self._build_cdp_env(cdp_mod=mod)
            # Post-action snapshot changes digest (busy='true'), so before_sha != after_sha is True,
            # while _verify_expect(post, target, expect) is False (selected='true' is absent).
            # Original And returns False (verified=0); mutated Or returns True (verified=1).
            changed_unverified = make_ax_node(
                ident="n1",
                role="button",
                name="Do it",
                attrs=(("busy", "true"),),
                backend=41,
                box=[10, 20, 20, 20],
            )
            snapshots = iter(([env.original_node], [changed_unverified]))
            receipt = mod.run_ax_action(
                env.cdp,
                "s1",
                [env.original_node],
                env.action,
                lambda: next(snapshots),
                retries=1,
                wait_ms=0,
                sleep=lambda _: None,
                lease=env.lease_a,
                focus=env.focus_a,
                observation=env.obs_a,
                registry=env.registry,
                focus_policy=env.policy,
                writer_fence=env.fence,
                clock=env.clock,
            )
            self.assertNotEqual(receipt["pre_sha"], receipt["post_sha"])
            self.assertEqual(receipt["ok"], 0)
            self.assertEqual(receipt["verified"], 0)
            self.assertEqual(receipt["attempts"], 1)

        res = InMemoryAstProveHarness.prove(module=cdp_executor_mod, spec=spec, probe=_probe_m1)
        self.assertEqual(res.disposition, "KILLED", msg=res.detail)

    def test_catalogue_mutant_m_wender_act_cdp_executor_2_killed(self) -> None:
        spec = CATALOGUE_MUTATION_OBLIGATIONS[1]
        self.assertEqual(spec.mutant_id, "M-wender-act-cdp-executor-2")
        self.assertEqual(spec.operator, "O")
        self.assertEqual(spec.line, 742)
        self.assertEqual(spec.qualname, "_authorized_input_events")
        self.assertEqual(spec.selector, "ops element 0")

        def _probe_m2(mod: types.ModuleType) -> None:
            # 1. Matching roles in _authorized_input_events must succeed on hover.
            env_ok = self._build_cdp_env(cdp_mod=mod)
            hover_action = {
                "op": "hover",
                "target": "n1",
                "expect": {"target_states": {"selected": "true"}},
            }
            snapshots_ok = iter(([env_ok.original_node], [env_ok.after_node]))
            try:
                receipt = mod.run_ax_action(
                    env_ok.cdp,
                    "s1",
                    [env_ok.original_node],
                    hover_action,
                    lambda: next(snapshots_ok),
                    retries=1,
                    wait_ms=0,
                    sleep=lambda _: None,
                    lease=env_ok.lease_a,
                    focus=env_ok.focus_a,
                    observation=env_ok.obs_a,
                    registry=env_ok.registry,
                    focus_policy=env_ok.policy,
                    writer_fence=env_ok.fence,
                    clock=env_ok.clock,
                )
            except mod.ActionError as exc:
                self.fail(f"matching role in _authorized_input_events refused: {exc}")
            self.assertEqual(receipt["ok"], 1)
            self.assertEqual(len(env_ok.cdp.calls), 1)

            # 2. Drifted role at _authorized_input_events must raise ActionError before dispatch.
            env_bad = self._build_cdp_env(cdp_mod=mod)
            env_bad.obs_a.state["nodes"] = [_DriftingRoleNodeDict(env_bad.original_node, drifted_role="textbox")]
            snapshots_bad = iter(([env_bad.original_node], [env_bad.after_node]))
            with self.assertRaises(mod.ActionError):
                mod.run_ax_action(
                    env_bad.cdp,
                    "s1",
                    [env_bad.original_node],
                    hover_action,
                    lambda: next(snapshots_bad),
                    retries=1,
                    wait_ms=0,
                    sleep=lambda _: None,
                    lease=env_bad.lease_a,
                    focus=env_bad.focus_a,
                    observation=env_bad.obs_a,
                    registry=env_bad.registry,
                    focus_policy=env_bad.policy,
                    writer_fence=env_bad.fence,
                    clock=env_bad.clock,
                )
            self.assertEqual(env_bad.cdp.calls, [])

        res = InMemoryAstProveHarness.prove(module=cdp_executor_mod, spec=spec, probe=_probe_m2)
        self.assertEqual(res.disposition, "KILLED", msg=res.detail)

    def test_ast_prove_harness_operators_O_B_D_S_and_separate_dispositions(self) -> None:
        # 1. Operator B on Compare (toggling >= to > on now_ns >= expires_ns at boundary now_ns == expires_ns)
        lines = inspect.getsource(verifier_mod).splitlines()
        exp_line = next(i for i, l in enumerate(lines, 1) if "now_ns >= expires_ns" in l)
        spec_b_cmp = MutantSpec(
            mutant_id="M-verifier-B-compare-expiry",
            operator="B",
            file_path="wender/act/multiwin_isolation_verifier.py",
            line=exp_line,
            qualname="authorize_cdp_action",
            node_source="now_ns >= expires_ns",
            selector="ops element 0",
        )

        def _probe_b_cmp(mod: types.ModuleType) -> None:
            env = self._build_cdp_env(now_ns=100, expires_ns=500)
            env.clock.value = 500
            ok, pred, _, _ = mod.authorize_cdp_action(
                session_id="s1",
                nodes=[env.original_node],
                action=env.action,
                lease=env.lease_a,
                focus=env.focus_a,
                observation=env.obs_a,
                registry=env.registry,
                focus_policy=env.policy,
                writer_fence=env.fence,
                locks=env.locks,
                clock=env.clock,
            )
            self.assertFalse(ok)
            self.assertEqual(pred, "lease_expiry")

        res_b_cmp = InMemoryAstProveHarness.prove(module=verifier_mod, spec=spec_b_cmp, probe=_probe_b_cmp)
        self.assertEqual(res_b_cmp.disposition, "KILLED", msg=res_b_cmp.detail)

        # 2. Operator B on exact integer Constant (+1 on 1 in `initial_seq + 1`)
        seq_line = next(i for i, l in enumerate(lines, 1) if "seq_advanced = executor.next_seq == initial_seq + 1" in l)
        spec_b_const = MutantSpec(
            mutant_id="M-verifier-B-const-seq",
            operator="B",
            file_path="wender/act/multiwin_isolation_verifier.py",
            line=seq_line,
            qualname="verify_godot_isolation",
            node_source="1",
            selector="constant",
        )

        def _probe_b_const(mod: types.ModuleType) -> None:
            sink: list[bytes] = []
            ex = GodotExecutor({"principal": "p", "slot_kind": "sandbox"}, make_godot_schemas(), encode_godot_batch_ram, sink.append)
            audit = mod.verify_godot_isolation(
                ex,
                {"tick": 4, "seq": 1, "cmd": "advance", "args": {}},
                committed_tick=4,
                basis_sha256="a" * 64,
                sink_list=sink,
            )
            self.assertEqual(audit["ok"], 1)
            self.assertEqual(audit["verified_effect"], 1)

        res_b_const = InMemoryAstProveHarness.prove(module=verifier_mod, spec=spec_b_const, probe=_probe_b_const)
        self.assertEqual(res_b_const.disposition, "KILLED", msg=res_b_const.detail)

        # Negative control: B never substitutes a constant increment for a comparison selector
        spec_b_mismatch = MutantSpec(
            mutant_id="M-verifier-B-selector-mismatch",
            operator="B",
            file_path="wender/act/multiwin_isolation_verifier.py",
            line=seq_line,
            qualname="verify_godot_isolation",
            node_source="1",
            selector="ops element 0",
        )
        res_b_mismatch = InMemoryAstProveHarness.prove(module=verifier_mod, spec=spec_b_mismatch, probe=_probe_b_const)
        self.assertEqual(res_b_mismatch.disposition, "MISSING_ANCHOR")

        # 3. Operator O on BinOp (Add -> Sub on `initial_seq + 1`)
        spec_o_binop = MutantSpec(
            mutant_id="M-verifier-O-binop-seq",
            operator="O",
            file_path="wender/act/multiwin_isolation_verifier.py",
            line=seq_line,
            qualname="verify_godot_isolation",
            node_source="initial_seq + 1",
            selector="binop",
        )
        res_o_binop = InMemoryAstProveHarness.prove(module=verifier_mod, spec=spec_o_binop, probe=_probe_b_const)
        self.assertEqual(res_o_binop.disposition, "KILLED", msg=res_o_binop.detail)

        # 3b. Operator O on BinOp Mult <-> FloorDiv (`height * width`)
        mult_line = next(i for i, l in enumerate(lines, 1) if "expected_pixels = height * width" in l)
        spec_o_mult = MutantSpec(
            mutant_id="M-verifier-O-binop-mult",
            operator="O",
            file_path="wender/act/multiwin_isolation_verifier.py",
            line=mult_line,
            qualname="_verify_frame_digest",
            node_source="height * width",
            selector="binop",
        )
        res_o_mult = InMemoryAstProveHarness.prove(
            module=verifier_mod,
            spec=spec_o_mult,
            probe=lambda mod: self.assertTrue(mod._verify_frame_digest(self._build_cdp_env().frame_a, None)[0]),
        )
        self.assertEqual(res_o_mult.disposition, "KILLED", msg=res_o_mult.detail)

        # 4b. Operator D on Assert (`assert audit.get("target") == expected_target ...`)
        assert_line = next(i for i, l in enumerate(lines, 1) if 'assert audit.get("target") == expected_target' in l)
        assert_src = """assert audit.get("target") == expected_target and audit.get("violated_predicate") == expected_predicate, (
        f"expected target={expected_target!r} predicate={expected_predicate!r}, "
        f"got target={audit.get('target')!r} predicate={audit.get('violated_predicate')!r}"
    )"""
        spec_d_assert = MutantSpec(
            mutant_id="M-verifier-D-assert",
            operator="D",
            file_path="wender/act/multiwin_isolation_verifier.py",
            line=assert_line,
            qualname="assert_audit_target_predicate",
            node_source=assert_src,
            selector="assert_test",
        )

        def _probe_d_assert(mod: types.ModuleType) -> None:
            sample = {"target": "t1", "violated_predicate": "lease_ownership"}
            mod.assert_audit_target_predicate(sample, expected_target="t1", expected_predicate="lease_ownership")
            raised = False
            try:
                mod.assert_audit_target_predicate(sample, expected_target="t2", expected_predicate="lease_ownership")
            except AssertionError:
                raised = True
            self.assertTrue(raised, "expected AssertionError on mismatched target")

        res_d_assert = InMemoryAstProveHarness.prove(module=verifier_mod, spec=spec_d_assert, probe=_probe_d_assert)
        self.assertEqual(res_d_assert.disposition, "KILLED", msg=res_d_assert.detail)

        # 4. Operator D on raising If (`if surface not in ("cdp", "godot"): raise ValueError(...)`)
        surf_line = next(i for i, l in enumerate(lines, 1) if 'if surface not in ("cdp", "godot"):' in l)
        surf_if_src = 'if surface not in ("cdp", "godot"):\n        raise ValueError(f"unsupported surface: {surface!r}")'
        spec_d_if = MutantSpec(
            mutant_id="M-verifier-D-raising-if",
            operator="D",
            file_path="wender/act/multiwin_isolation_verifier.py",
            line=surf_line,
            qualname="build_audit_record",
            node_source=surf_if_src,
            selector="if_test",
        )

        def _probe_d_if(mod: types.ModuleType) -> None:
            with self.assertRaises(ValueError):
                mod.build_audit_record(
                    surface="invalid_surface",
                    target="t1",
                    stage="authorization",
                    violated_predicate="lease_ownership",
                    authorized=0,
                    dispatched=0,
                    verified_effect=0,
                    actor_invocations=0,
                    detail="bad",
                )

        res_d_if = InMemoryAstProveHarness.prove(module=verifier_mod, spec=spec_d_if, probe=_probe_d_if)
        self.assertEqual(res_d_if.disposition, "KILLED", msg=res_d_if.detail)

        # 5. Operator S on Call (swapping first two distinct positional arguments in `_verify_frame_digest(frame, expected_frame_digest)`)
        call_line = next(i for i, l in enumerate(lines, 1) if "digest_ok, digest_info = _verify_frame_digest(frame, expected_frame_digest)" in l)
        spec_s_call = MutantSpec(
            mutant_id="M-verifier-S-call-args",
            operator="S",
            file_path="wender/act/multiwin_isolation_verifier.py",
            line=call_line,
            qualname="authorize_cdp_action",
            node_source="_verify_frame_digest(frame, expected_frame_digest)",
            selector="call_args",
        )

        def _probe_s_call(mod: types.ModuleType) -> None:
            env = self._build_cdp_env()
            ok, pred, _, _ = mod.authorize_cdp_action(
                session_id="s1",
                nodes=[env.original_node],
                action=env.action,
                lease=env.lease_a,
                focus=env.focus_a,
                observation=env.obs_a,
                registry=env.registry,
                focus_policy=env.policy,
                writer_fence=env.fence,
                locks=env.locks,
                clock=env.clock,
                expected_frame_digest=env.frame_a.digest,
            )
            self.assertTrue(ok)
            self.assertIsNone(pred)

        res_s_call = InMemoryAstProveHarness.prove(module=verifier_mod, spec=spec_s_call, probe=_probe_s_call)
        self.assertEqual(res_s_call.disposition, "KILLED", msg=res_s_call.detail)

        # 6. Verify separate non-kill dispositions: FAILED_CONTROL, MISSING_ANCHOR, COMPILE_IMPORT_ERROR,
        # RUNTIME_ERROR, SURVIVED, PROVEN_EQUIVALENT, TIMEOUT.
        res_failed_ctrl = InMemoryAstProveHarness.prove(
            module=verifier_mod,
            spec=spec_s_call,
            probe=lambda _m: self.fail("intentional control failure"),
        )
        self.assertEqual(res_failed_ctrl.disposition, "FAILED_CONTROL")

        res_missing = InMemoryAstProveHarness.prove(
            module=verifier_mod,
            spec=MutantSpec("M-missing", "O", "wender/act/multiwin_isolation_verifier.py", 9999, "authorize_cdp_action", "x == y"),
            probe=_probe_s_call,
        )
        self.assertEqual(res_missing.disposition, "MISSING_ANCHOR")

        res_compile_err = InMemoryAstProveHarness.prove(
            module=verifier_mod,
            spec=spec_s_call,
            probe=lambda _m: None,
            mutate_source_hook=lambda _src: "def broken(:\n",
        )
        self.assertEqual(res_compile_err.disposition, "COMPILE_IMPORT_ERROR")

        res_runtime_err = InMemoryAstProveHarness.prove(
            module=verifier_mod,
            spec=spec_s_call,
            probe=lambda m: (_ for _ in ()).throw(KeyError("boom")) if m is not verifier_mod else None,
        )
        self.assertEqual(res_runtime_err.disposition, "RUNTIME_ERROR")

        res_survived = InMemoryAstProveHarness.prove(
            module=verifier_mod,
            spec=spec_s_call,
            probe=lambda _m: None,
        )
        self.assertEqual(res_survived.disposition, "SURVIVED")

        res_equiv = InMemoryAstProveHarness.prove(
            module=verifier_mod,
            spec=spec_s_call,
            probe=lambda _m: None,
            equivalence_proof=lambda orig, mut: orig.__name__ == mut.__name__,
        )
        self.assertEqual(res_equiv.disposition, "PROVEN_EQUIVALENT")

        res_timeout = InMemoryAstProveHarness.prove(
            module=verifier_mod,
            spec=spec_s_call,
            probe=lambda m: time.sleep(0.08) if m is not verifier_mod else None,
            timeout_s=0.01,
        )
        self.assertEqual(res_timeout.disposition, "TIMEOUT")

    # ------------------------------------------------------------------------
    # 4. Seeded Random Property Loops (64 Cases/Domain) & Shrinking (<= 32)
    # ------------------------------------------------------------------------

    def test_seeded_random_cdp_isolation_property_loop_64_cases_and_shrinking(self) -> None:
        rng = random.Random(CDP_PROPERTY_SEED)
        valid_cases_run = 0

        for case_idx in range(PROPERTY_TARGET_CASES):
            epoch = rng.randint(1, 8)
            frame_seq = rng.randint(0, 20)
            generation = rng.randint(1, 6)
            now_ns = rng.randint(50, 200)
            expires_ns = now_ns + rng.randint(50, 400)
            fault = rng.choice(
                (
                    "valid",
                    "forged_nonce",
                    "expired_lease",
                    "wrong_generation",
                    "wrong_focus_domain",
                    "modal_blocked",
                    "stale_epoch",
                    "bad_frame_digest",
                    "stale_observation",
                    "busy_fence",
                    "unverified_post",
                )
            )
            env = self._build_cdp_env(
                epoch=epoch,
                frame_seq=frame_seq,
                generation=generation,
                now_ns=now_ns,
                expires_ns=expires_ns,
            )

            lease = env.lease_a
            focus = env.focus_a
            obs = env.obs_a
            expected_digest = env.frame_a.digest
            post_node = env.after_node
            expected_predicate: str | None = None
            expected_stage = "verified_effect"
            expected_calls = 3

            if fault == "forged_nonce":
                lease = ActorLease(env.handle_a, "alice", generation, b"unissued-nonce", expires_ns)
                expected_predicate = "lease_ownership"
                expected_stage = "authorization"
                expected_calls = 0
            elif fault == "expired_lease":
                env.clock.value = expires_ns
                expected_predicate = "lease_expiry"
                expected_stage = "authorization"
                expected_calls = 0
            elif fault == "wrong_generation":
                lease = SimpleNamespace(
                    handle=env.handle_a,
                    owner="alice",
                    generation=generation + 1,
                    nonce=b"nonce-a",
                    expires_ns=expires_ns,
                )
                expected_predicate = "lease_generation"
                expected_stage = "authorization"
                expected_calls = 0
            elif fault == "wrong_focus_domain":
                focus = FocusLease(env.lease_a, "cdp:other_session", epoch)
                expected_predicate = "focus_ownership"
                expected_stage = "authorization"
                expected_calls = 0
            elif fault == "modal_blocked":
                env.policy.set_modal(env.handle_b, domain="cdp:s1")
                expected_predicate = "focus_ownership"
                expected_stage = "authorization"
                expected_calls = 0
            elif fault == "stale_epoch":
                env.registry.advance_epoch(env.handle_a)
                expected_predicate = "epoch_binding"
                expected_stage = "authorization"
                expected_calls = 0
            elif fault == "bad_frame_digest":
                expected_digest = "f" * 64 if env.frame_a.digest != "f" * 64 else "0" * 64
                expected_predicate = "frame_digest"
                expected_stage = "authorization"
                expected_calls = 0
            elif fault == "stale_observation":
                env.registry.clear_observations()
                expected_predicate = "observation_freshness"
                expected_stage = "authorization"
                expected_calls = 0
            elif fault == "busy_fence":
                env.fence.busy = True
                expected_predicate = "writer_fence"
                expected_stage = "authorization"
                expected_calls = 0
            elif fault == "unverified_post":
                post_node = env.original_node
                expected_predicate = "verified_effect"
                expected_stage = "verified_effect"
                expected_calls = 3

            snapshots = iter(([env.original_node], [post_node]))
            audit = env.verifier.verify_and_run(
                env.cdp,
                "s1",
                [env.original_node],
                env.action,
                lambda: next(snapshots),
                lease=lease,
                focus=focus,
                observation=obs,
                expected_frame_digest=expected_digest,
            )
            diag = f"seed=0x{CDP_PROPERTY_SEED:x} case={case_idx} minimal_fixture={{fault={fault!r}, epoch={epoch}, seq={frame_seq}, gen={generation}}}"
            self.assertEqual(audit["violated_predicate"], expected_predicate, msg=diag)
            self.assertEqual(audit["stage"], expected_stage, msg=diag)
            self.assertEqual(len(env.cdp.calls), expected_calls, msg=diag)
            valid_cases_run += 1

        self.assertEqual(valid_cases_run, PROPERTY_TARGET_CASES)

        # Verify validity-preserving shrinker within 32 attempts and budget-exhaustion guard
        raw_fixture = {
            "epoch": 16,
            "frame_seq": 24,
            "generation": 5,
            "now_ns": 100,
            "expires_ns": 900,
            "extra_nodes": ["n2", "n3", "n4", "n5"],
            "fault": "stale_epoch",
        }
        shrunk, used_attempts = shrink_cdp_fixture(
            raw_fixture,
            is_valid=lambda fx: fx["epoch"] >= 1 and fx["generation"] >= 1 and fx["expires_ns"] > fx["now_ns"],
            fails_property=lambda fx: fx["fault"] == "stale_epoch",
            max_attempts=MAX_SHRINK_ATTEMPTS,
        )
        self.assertLessEqual(used_attempts, MAX_SHRINK_ATTEMPTS)
        self.assertEqual(shrunk["epoch"], 1)
        self.assertEqual(shrunk["frame_seq"], 0)
        self.assertEqual(shrunk["generation"], 1)
        self.assertEqual(shrunk["extra_nodes"], [])
        with self.assertRaises(RuntimeError):
            shrink_cdp_fixture(
                raw_fixture,
                is_valid=lambda _fx: True,
                fails_property=lambda _fx: True,
                max_attempts=2,
            )

    def test_seeded_random_godot_isolation_property_loop_64_cases_and_shrinking(self) -> None:
        rng = random.Random(GODOT_PROPERTY_SEED)
        schemas = make_godot_schemas()
        valid_cases_run = 0

        for case_idx in range(PROPERTY_TARGET_CASES):
            committed_tick = rng.randint(0, 25)
            basis = hashlib.sha256(f"basis-{case_idx}".encode("utf-8")).hexdigest()
            scenario = rng.choice(
                (
                    "valid_current",
                    "valid_economic",
                    "wrong_tick_economic",
                    "seq_gap",
                    "duplicate_seq",
                    "missing_mandate",
                    "main_save_automated",
                    "irreversible_needs_confirm",
                    "confirm_admitted_then_used",
                    "confirm_stale_basis",
                    "confirm_target_mismatch",
                    "confirm_terms_mismatch",
                )
            )
            sink: list[bytes] = []
            mandates = {} if scenario == "missing_mandate" else {"owner": True}
            slot_kind = "main" if scenario == "main_save_automated" else "sandbox"
            ex = GodotExecutor(
                {
                    "principal": "prop_op",
                    "channel": "t2",
                    "slot_kind": slot_kind,
                    "automation_active": True,
                    "mandates": mandates,
                },
                schemas,
                encode_godot_batch_ram,
                sink.append,
            )
            verifier = GodotIsolationVerifier(ex, sink)
            diag = f"seed=0x{GODOT_PROPERTY_SEED:x} case={case_idx} minimal_fixture={{scenario={scenario!r}, tick={committed_tick}}}"

            if scenario == "valid_current":
                cmd = rng.choice(("advance", "pause"))
                audit = verifier.verify_and_dispatch(
                    {"tick": committed_tick, "seq": 1, "cmd": cmd, "args": {}},
                    committed_tick=committed_tick,
                    basis_sha256=basis,
                )
                self.assertEqual((audit["ok"], len(sink), ex.next_seq), (1, 1, 2), msg=diag)

            elif scenario == "valid_economic":
                audit = verifier.verify_and_dispatch(
                    {"tick": committed_tick + 1, "seq": 1, "cmd": "buy_asset", "args": {"asset_type": "gpu", "quantity": 3}},
                    committed_tick=committed_tick,
                    basis_sha256=basis,
                )
                self.assertEqual((audit["ok"], len(sink), ex.next_seq), (1, 1, 2), msg=diag)

            elif scenario == "wrong_tick_economic":
                audit = verifier.verify_and_dispatch(
                    {"tick": committed_tick, "seq": 1, "cmd": "buy_asset", "args": {"asset_type": "gpu", "quantity": 1}},
                    committed_tick=committed_tick,
                    basis_sha256=basis,
                )
                self.assertEqual((audit["ok"], audit["violated_predicate"], len(sink), ex.next_seq), (0, "tick_window", 0, 1), msg=diag)

            elif scenario == "seq_gap":
                audit = verifier.verify_and_dispatch(
                    {"tick": committed_tick, "seq": 2, "cmd": "advance", "args": {}},
                    committed_tick=committed_tick,
                    basis_sha256=basis,
                )
                self.assertEqual((audit["ok"], audit["violated_predicate"], len(sink), ex.next_seq), (0, "sequence_binding", 0, 1), msg=diag)

            elif scenario == "duplicate_seq":
                verifier.verify_and_dispatch(
                    {"tick": committed_tick, "seq": 1, "cmd": "advance", "args": {}},
                    committed_tick=committed_tick,
                    basis_sha256=basis,
                )
                audit_dup = verifier.verify_and_dispatch(
                    {"tick": committed_tick, "seq": 1, "cmd": "pause", "args": {}},
                    committed_tick=committed_tick,
                    basis_sha256=basis,
                )
                self.assertEqual((audit_dup["ok"], audit_dup["violated_predicate"], len(sink)), (0, "sequence_binding", 1), msg=diag)

            elif scenario == "missing_mandate":
                cmd = rng.choice(tuple(OWNER_RESERVED_COMMANDS))
                audit = verifier.verify_and_dispatch(
                    {"tick": committed_tick + 1, "seq": 1, "cmd": cmd, "args": {"target_id": "t_ent", "terms": {"amount": 5}}},
                    committed_tick=committed_tick,
                    basis_sha256=basis,
                )
                self.assertEqual((audit["ok"], audit["violated_predicate"], len(sink)), (0, "trusted_mandate", 0), msg=diag)

            elif scenario == "main_save_automated":
                cmd = rng.choice(tuple(IRREVERSIBLE_COMMANDS))
                audit = verifier.verify_and_dispatch(
                    {"tick": committed_tick + 1, "seq": 1, "cmd": cmd, "args": {"target_id": "t_ent", "terms": {"amount": 5}}},
                    committed_tick=committed_tick,
                    basis_sha256=basis,
                )
                self.assertEqual((audit["ok"], audit["violated_predicate"], len(sink)), (0, "main_save_consent", 0), msg=diag)

            else:
                cmd = rng.choice(tuple(IRREVERSIBLE_COMMANDS))
                prop_audit = verifier.verify_and_dispatch(
                    {"tick": committed_tick + 1, "seq": 1, "cmd": cmd, "args": {"target_id": "t_ent", "terms": {"amount": 5}}},
                    committed_tick=committed_tick,
                    basis_sha256=basis,
                )
                modal_id = prop_audit["proposal_modal_id"]
                self.assertIsNotNone(modal_id, msg=diag)
                self.assertEqual(len(sink), 0, msg=diag)

                if scenario == "irreversible_needs_confirm":
                    self.assertEqual(prop_audit["violated_predicate"], "confirmation_binding", msg=diag)
                elif scenario == "confirm_admitted_then_used":
                    conf_rec = {
                        "tick": committed_tick + 1,
                        "seq": 1,
                        "cmd": "confirm",
                        "args": {
                            "modal_id": modal_id,
                            "cmd": cmd,
                            "target_id": "t_ent",
                            "terms": {"amount": 5},
                            "basis_sha256": basis,
                        },
                    }
                    admitted = verifier.verify_and_dispatch(conf_rec, committed_tick=committed_tick, basis_sha256=basis)
                    self.assertEqual((admitted["ok"], len(sink), ex.next_seq), (1, 1, 2), msg=diag)
                    reused = verifier.verify_and_dispatch(dict(conf_rec, seq=2), committed_tick=committed_tick, basis_sha256=basis)
                    self.assertEqual((reused["ok"], reused["violated_predicate"], len(sink)), (0, "proposal_single_use", 1), msg=diag)
                elif scenario == "confirm_stale_basis":
                    conf_rec = {
                        "tick": committed_tick + 1,
                        "seq": 1,
                        "cmd": "confirm",
                        "args": {
                            "modal_id": modal_id,
                            "cmd": cmd,
                            "target_id": "t_ent",
                            "terms": {"amount": 5},
                            "basis_sha256": "0" * 64,
                        },
                    }
                    stale = verifier.verify_and_dispatch(conf_rec, committed_tick=committed_tick, basis_sha256=basis)
                    self.assertEqual((stale["ok"], stale["violated_predicate"], len(sink)), (0, "basis_binding", 0), msg=diag)
                elif scenario == "confirm_target_mismatch":
                    conf_rec = {
                        "tick": committed_tick + 1,
                        "seq": 1,
                        "cmd": "confirm",
                        "args": {
                            "modal_id": modal_id,
                            "cmd": cmd,
                            "target_id": "wrong_ent",
                            "terms": {"amount": 5},
                            "basis_sha256": basis,
                        },
                    }
                    tm = verifier.verify_and_dispatch(conf_rec, committed_tick=committed_tick, basis_sha256=basis)
                    self.assertEqual((tm["ok"], tm["violated_predicate"], len(sink)), (0, "confirmation_binding", 0), msg=diag)
                elif scenario == "confirm_terms_mismatch":
                    conf_rec = {
                        "tick": committed_tick + 1,
                        "seq": 1,
                        "cmd": "confirm",
                        "args": {
                            "modal_id": modal_id,
                            "cmd": cmd,
                            "target_id": "t_ent",
                            "terms": {"amount": 999},
                            "basis_sha256": basis,
                        },
                    }
                    trm = verifier.verify_and_dispatch(conf_rec, committed_tick=committed_tick, basis_sha256=basis)
                    self.assertEqual((trm["ok"], trm["violated_predicate"], len(sink)), (0, "confirmation_binding", 0), msg=diag)

            valid_cases_run += 1

        self.assertEqual(valid_cases_run, PROPERTY_TARGET_CASES)

        raw_godot_fx = {"committed_tick": 18, "quantity": 12, "terms": {"amount": 64}, "scenario": "confirm_stale_basis"}
        shrunk_g, g_attempts = shrink_godot_fixture(
            raw_godot_fx,
            is_valid=lambda fx: fx["committed_tick"] >= 0 and fx["quantity"] >= 1,
            fails_property=lambda fx: fx["scenario"] == "confirm_stale_basis",
            max_attempts=MAX_SHRINK_ATTEMPTS,
        )
        self.assertLessEqual(g_attempts, MAX_SHRINK_ATTEMPTS)
        self.assertEqual(shrunk_g["committed_tick"], 0)
        self.assertEqual(shrunk_g["quantity"], 1)
        self.assertEqual(shrunk_g["terms"], {"amount": 1})

    def test_pure_functions_deterministic_properties(self) -> None:
        # Pure functions: classify, map_ui_action, _verify_expect, format_audit_record
        for path in ("api", "ax", "dom", "coordinates", "invalid", None):
            for op in OPS + ("call", "invoke", "read", "unknown"):
                res1 = classify(path, op)  # type: ignore[arg-type]
                res2 = classify(path, op)  # type: ignore[arg-type]
                self.assertEqual(res1, res2)
                self.assertIn(res1, tuple(FocusClass))

        mapped_buy = map_ui_action({"tick": 1, "seq": 1, "cmd": "buy_compute", "args": {"asset_type": "h100", "quantity": 2}})
        self.assertEqual(mapped_buy, {"tick": 1, "seq": 1, "cmd": "buy_asset", "args": {"asset_type": "h100", "quantity": 2}})
        mapped_train = map_ui_action("start_training", {"recipe_id": "r_gpt"})
        self.assertEqual(mapped_train, ("start_process", {"recipe_id": "r_gpt"}))
        with self.assertRaises(ValueError):
            map_ui_action({"cmd": "buy_compute", "args": "not_a_dict"})
        with self.assertRaises(ValueError):
            map_ui_action("buy_compute", None)
        with self.assertRaises(ValueError):
            map_ui_action(12345)  # type: ignore[arg-type]

        target_node = make_ax_node(ident="n1", role="button", name="Send", attrs=(("pressed", "true"),), backend=99)
        self.assertTrue(_verify_expect([target_node], target_node, {"target_states": {"pressed": "true"}, "target_name": "Send"}))
        self.assertFalse(_verify_expect([target_node], target_node, {"target_states": {"pressed": "false"}}))

    # ------------------------------------------------------------------------
    # 5. Shared-State Concurrency Suite (32 Replayable Schedules, <= 4 Threads)
    # ------------------------------------------------------------------------

    def test_shared_state_concurrency_32_replayable_schedules_bounded_waits(self) -> None:
        baseline_threads = {t.ident for t in threading.enumerate()}
        recorded_schedules: list[tuple[int, tuple[str, ...]]] = []

        for sched_idx in range(CONCURRENCY_TARGET_SCHEDULES):
            mode = sched_idx % 4
            num_threads = 3 if mode in (0, 1) else MAX_CONCURRENCY_THREADS
            self.assertLessEqual(num_threads, MAX_CONCURRENCY_THREADS)

            start_barrier = threading.Barrier(num_threads, timeout=SYNC_TIMEOUT_S)
            step1_event = threading.Event()
            step2_event = threading.Event()
            step3_event = threading.Event()
            schedule_trace: list[str] = []
            trace_lock = threading.Lock()
            worker_errors: list[BaseException] = []
            threads: list[threading.Thread] = []

            def _record_step(label: str) -> None:
                with trace_lock:
                    schedule_trace.append(label)

            if mode == 0:
                # Concurrent contention on a single-use CDP focus grant: exactly 1 dispatches, others fail before fake actor.
                env = self._build_cdp_env(epoch=2 + sched_idx)
                results: list[dict[str, Any]] = []

                def _worker_m0(tid: int, *, _env: SimpleNamespace = env) -> None:
                    try:
                        start_barrier.wait(timeout=SYNC_TIMEOUT_S)
                        if tid == 0:
                            _record_step("t0:consume_and_dispatch")
                            snaps = iter(([_env.original_node], [_env.after_node]))
                            r = _env.verifier.verify_and_run(
                                _env.cdp,
                                "s1",
                                [_env.original_node],
                                _env.action,
                                lambda: next(snaps),
                                lease=_env.lease_a,
                                focus=_env.focus_a,
                                observation=_env.obs_a,
                            )
                            with trace_lock:
                                results.append(r)
                            step1_event.set()
                        elif tid == 1:
                            assert step1_event.wait(timeout=SYNC_TIMEOUT_S)
                            _record_step("t1:replay_grant_refused")
                            r = _env.verifier.verify_and_run(
                                _env.cdp,
                                "s1",
                                [_env.original_node],
                                _env.action,
                                lambda: [_env.original_node],
                                lease=_env.lease_a,
                                focus=_env.focus_a,
                                observation=_env.obs_a,
                            )
                            with trace_lock:
                                results.append(r)
                            step2_event.set()
                        else:
                            assert step2_event.wait(timeout=SYNC_TIMEOUT_S)
                            _record_step("t2:foreign_lease_refused")
                            r = _env.verifier.verify_and_run(
                                _env.cdp,
                                "s1",
                                [_env.original_node],
                                _env.action,
                                lambda: [_env.original_node],
                                lease=_env.lease_b,
                                focus=_env.focus_a,
                                observation=_env.obs_a,
                            )
                            with trace_lock:
                                results.append(r)
                    except BaseException as exc:  # noqa: BLE001
                        with trace_lock:
                            worker_errors.append(exc)
                        step1_event.set()
                        step2_event.set()

                worker_fn = _worker_m0
                post_check = lambda _env=env, _res=results: (
                    self.assertEqual([r["ok"] for r in _res], [1, 0, 0]),
                    self.assertEqual(len(_env.cdp.calls), 3),
                )

            elif mode == 1:
                # Modal preemption before dispatch: t0 sets modal on handle_b, t1 is refused before CDP actor, t2 clears modal & re-grants.
                env = self._build_cdp_env(epoch=3)
                results = []

                def _worker_m1(tid: int, *, _env: SimpleNamespace = env) -> None:
                    try:
                        start_barrier.wait(timeout=SYNC_TIMEOUT_S)
                        if tid == 0:
                            _record_step("t0:set_modal_b")
                            _env.policy.set_modal(_env.handle_b, domain="cdp:s1")
                            step1_event.set()
                        elif tid == 1:
                            assert step1_event.wait(timeout=SYNC_TIMEOUT_S)
                            _record_step("t1:blocked_by_modal")
                            r = _env.verifier.verify_and_run(
                                _env.cdp,
                                "s1",
                                [_env.original_node],
                                _env.action,
                                lambda: [_env.original_node],
                                lease=_env.lease_a,
                                focus=_env.focus_a,
                                observation=_env.obs_a,
                            )
                            with trace_lock:
                                results.append(r)
                            step2_event.set()
                        else:
                            assert step2_event.wait(timeout=SYNC_TIMEOUT_S)
                            _record_step("t2:clear_modal_and_dispatch")
                            _env.policy.set_modal(None, domain="cdp:s1")
                            _env.policy.set_focus(_env.handle_a, domain="cdp:s1", epoch=3)
                            fresh_focus = _env.policy.grant(_env.lease_a, domain="cdp:s1")
                            snaps = iter(([_env.original_node], [_env.after_node]))
                            r = _env.verifier.verify_and_run(
                                _env.cdp,
                                "s1",
                                [_env.original_node],
                                _env.action,
                                lambda: next(snaps),
                                lease=_env.lease_a,
                                focus=fresh_focus,
                                observation=_env.obs_a,
                            )
                            with trace_lock:
                                results.append(r)
                    except BaseException as exc:  # noqa: BLE001
                        with trace_lock:
                            worker_errors.append(exc)
                        step1_event.set()
                        step2_event.set()

                worker_fn = _worker_m1
                post_check = lambda _env=env, _res=results: (
                    self.assertEqual([r["ok"] for r in _res], [0, 1]),
                    self.assertEqual(_res[0]["violated_predicate"], "focus_ownership"),
                    self.assertEqual(len(_env.cdp.calls), 3),
                )

            elif mode == 2:
                # 4 threads on Godot single-use proposal confirmation: t0 proposes, t1 confirms, t2 & t3 fail on single-use / seq.
                sink: list[bytes] = []
                basis = "d" * 64
                ex = GodotExecutor(
                    {"principal": "op", "slot_kind": "sandbox", "mandates": {"owner": True}},
                    make_godot_schemas(),
                    encode_godot_batch_ram,
                    sink.append,
                )
                g_verifier = GodotIsolationVerifier(ex, sink)
                g_results: list[dict[str, Any]] = []

                def _worker_m2(tid: int, *, _ver: GodotIsolationVerifier = g_verifier, _basis: str = basis) -> None:
                    try:
                        start_barrier.wait(timeout=SYNC_TIMEOUT_S)
                        if tid == 0:
                            _record_step("t0:propose_ipo")
                            r = _ver.verify_and_dispatch(
                                {"tick": 5, "seq": 1, "cmd": "ipo", "args": {"target_id": "c1", "terms": {"shares": 100}}},
                                committed_tick=4,
                                basis_sha256=_basis,
                            )
                            with trace_lock:
                                g_results.append(r)
                            step1_event.set()
                        elif tid == 1:
                            assert step1_event.wait(timeout=SYNC_TIMEOUT_S)
                            _record_step("t1:confirm_ipo_admitted")
                            r = _ver.verify_and_dispatch(
                                {
                                    "tick": 5,
                                    "seq": 1,
                                    "cmd": "confirm",
                                    "args": {
                                        "modal_id": "modal_ipo_1",
                                        "cmd": "ipo",
                                        "target_id": "c1",
                                        "terms": {"shares": 100},
                                        "basis_sha256": _basis,
                                    },
                                },
                                committed_tick=4,
                                basis_sha256=_basis,
                            )
                            with trace_lock:
                                g_results.append(r)
                            step2_event.set()
                        elif tid == 2:
                            assert step2_event.wait(timeout=SYNC_TIMEOUT_S)
                            _record_step("t2:confirm_replay_rejected")
                            r = _ver.verify_and_dispatch(
                                {
                                    "tick": 5,
                                    "seq": 2,
                                    "cmd": "confirm",
                                    "args": {
                                        "modal_id": "modal_ipo_1",
                                        "cmd": "ipo",
                                        "target_id": "c1",
                                        "terms": {"shares": 100},
                                        "basis_sha256": _basis,
                                    },
                                },
                                committed_tick=4,
                                basis_sha256=_basis,
                            )
                            with trace_lock:
                                g_results.append(r)
                            step3_event.set()
                        else:
                            assert step3_event.wait(timeout=SYNC_TIMEOUT_S)
                            _record_step("t3:valid_next_seq")
                            r = _ver.verify_and_dispatch(
                                {"tick": 4, "seq": 2, "cmd": "advance", "args": {}},
                                committed_tick=4,
                                basis_sha256=_basis,
                            )
                            with trace_lock:
                                g_results.append(r)
                    except BaseException as exc:  # noqa: BLE001
                        with trace_lock:
                            worker_errors.append(exc)
                        step1_event.set()
                        step2_event.set()
                        step3_event.set()

                worker_fn = _worker_m2
                post_check = lambda _sink=sink, _res=g_results: (
                    self.assertEqual([r["ok"] for r in _res], [0, 1, 0, 1]),
                    self.assertEqual(_res[2]["violated_predicate"], "proposal_single_use"),
                    self.assertEqual(len(_sink), 2),
                )

            else:
                # 4 threads testing epoch advance fencing and writer fence quarantine isolation.
                env = self._build_cdp_env(epoch=5)
                results = []

                def _worker_m3(tid: int, *, _env: SimpleNamespace = env) -> None:
                    try:
                        start_barrier.wait(timeout=SYNC_TIMEOUT_S)
                        if tid == 0:
                            _record_step("t0:advance_epoch")
                            _env.registry.advance_epoch(_env.handle_a)
                            step1_event.set()
                        elif tid == 1:
                            assert step1_event.wait(timeout=SYNC_TIMEOUT_S)
                            _record_step("t1:stale_epoch_refused")
                            r = _env.verifier.verify_and_run(
                                _env.cdp,
                                "s1",
                                [_env.original_node],
                                _env.action,
                                lambda: [_env.original_node],
                                lease=_env.lease_a,
                                focus=_env.focus_a,
                                observation=_env.obs_a,
                            )
                            with trace_lock:
                                results.append(r)
                            step2_event.set()
                        elif tid == 2:
                            assert step2_event.wait(timeout=SYNC_TIMEOUT_S)
                            _record_step("t2:quarantine_fence")
                            _env.fence.quarantine(_env.lease_b)
                            step3_event.set()
                        else:
                            assert step3_event.wait(timeout=SYNC_TIMEOUT_S)
                            _record_step("t3:quarantined_fence_refused")
                            frame6 = make_ram_frame_ref(_env.handle_a, epoch=6, frame_seq=8)
                            obs6 = CanonicalObservation(
                                _env.handle_a,
                                6,
                                8,
                                1_000_000,
                                {
                                    "handle": _env.handle_a,
                                    "epoch": 6,
                                    "frame_seq": 8,
                                    "viewport": {"w": 800, "h": 600, "scroll_x": 0, "scroll_y": 0},
                                    "nodes": [copy.deepcopy(_env.original_node)],
                                    "focused_node_id": "n1",
                                },
                                frame6,
                            )
                            _env.registry.set_current_observation(obs6)
                            _env.policy.set_focus(_env.handle_a, domain="cdp:s1", epoch=6)
                            focus6 = _env.policy.grant(_env.lease_a, domain="cdp:s1")
                            r = _env.verifier.verify_and_run(
                                _env.cdp,
                                "s1",
                                [_env.original_node],
                                _env.action,
                                lambda: [_env.original_node],
                                lease=_env.lease_a,
                                focus=focus6,
                                observation=obs6,
                            )
                            with trace_lock:
                                results.append(r)
                    except BaseException as exc:  # noqa: BLE001
                        with trace_lock:
                            worker_errors.append(exc)
                        step1_event.set()
                        step2_event.set()
                        step3_event.set()

                worker_fn = _worker_m3
                post_check = lambda _env=env, _res=results: (
                    self.assertEqual([r["violated_predicate"] for r in _res], ["epoch_binding", "writer_fence"]),
                    self.assertEqual(_env.cdp.calls, []),
                )

            try:
                for tid in range(num_threads):
                    t = threading.Thread(
                        target=worker_fn,
                        args=(tid,),
                        name=f"jq3-r4-sched-{sched_idx}-t-{tid}",
                        daemon=False,
                    )
                    threads.append(t)
                    t.start()
                for t in threads:
                    t.join(timeout=SYNC_TIMEOUT_S)
            finally:
                step1_event.set()
                step2_event.set()
                step3_event.set()
                try:
                    start_barrier.abort()
                except Exception:
                    pass
                for t in threads:
                    if t.is_alive():
                        t.join(timeout=SYNC_TIMEOUT_S)
                    self.assertFalse(t.is_alive(), f"leaked thread {t.name} in schedule {sched_idx}")

            if worker_errors:
                raise AssertionError(f"schedule {sched_idx} worker errors: {worker_errors!r}")
            post_check()
            self.assertEqual(len(schedule_trace), num_threads)
            recorded_schedules.append((sched_idx, tuple(schedule_trace)))

        self.assertEqual(len(recorded_schedules), CONCURRENCY_TARGET_SCHEDULES)
        after_threads = {t.ident for t in threading.enumerate()}
        self.assertTrue(after_threads.issubset(baseline_threads))


if __name__ == "__main__":
    unittest.main()
