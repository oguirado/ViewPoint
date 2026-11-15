# @module {"id":"wa.tests.test_jq3_r_r5_g1_multiwin_perception_audit","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.multiwin_perception_audit","wa.perceive.parallel_perceive","wa.perceive.session_registry","wa.perceive.vision_adapter","wa.state.schema"],"book":"HB-WA-s1-perceive"}
"""Tests for multi-window perception consistency auditing, AST mutation proofs,
seeded property loops with validity-preserving shrinking, and bounded concurrency schedules.
"""

from __future__ import annotations

import ast
import builtins
from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import dataclass, replace
import hashlib
import importlib
from pathlib import Path
import random
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

import numpy as np

from wender.perceive import multiwin_perception_audit as audit_mod
from wender.perceive.multiwin_perception_audit import (
    AuditConfigError,
    BatchAuditReport,
    DIAG_DISAGREEING,
    DIAG_FOREIGN,
    DIAG_MISSING,
    DIAG_STALE,
    DIAGNOSTIC_CATEGORIES,
    EvidenceIssue,
    MultiWindowPerceptionAuditor,
    TargetAuditDiagnostic,
    VERDICT_AUTHENTIC,
    VERDICT_REJECTED,
    audit_multiwin_perception,
    canonical_window_identity,
    perceive_and_audit,
    require_collaborator,
    verify_immutable_digest,
)
from wender.perceive import parallel_perceive as pp_mod
from wender.perceive.parallel_perceive import (
    ObservationError,
    _TARGET_KINDS,
    _describe_group,
    _handle_key,
    _prepare,
    perceive_many,
)
from wender.perceive.session_registry import (
    FrameRef,
    FrameValidationError,
    Handle,
    Observation,
    PerceptionResult,
    RegistryError,
    SessionRegistry,
)
from wender.perceive import vision_adapter as va_mod
from wender.perceive.vision_adapter import (
    FakeVisionEngine,
    Frame,
    VisionEngineAdapter,
    fuse_regions,
)
from wender.state import schema


REPO_ROOT = Path(__file__).resolve().parents[1]

# Separate dispositions for the in-memory AST prove harness; ONLY DISP_KILLED is a kill.
DISP_KILLED = "KILLED"
DISP_SURVIVED = "SURVIVED"
DISP_EQUIVALENT = "EQUIVALENT"
DISP_FAILED_CONTROL = "FAILED_CONTROL"
DISP_MISSING_ANCHOR = "MISSING_ANCHOR"
DISP_AMBIGUOUS_ANCHOR = "AMBIGUOUS_ANCHOR"
DISP_COMPILE_IMPORT_ERROR = "COMPILE_IMPORT_ERROR"
DISP_TIMEOUT = "TIMEOUT"

NON_KILL_DISPOSITIONS = frozenset({
    DISP_SURVIVED,
    DISP_EQUIVALENT,
    DISP_FAILED_CONTROL,
    DISP_MISSING_ANCHOR,
    DISP_AMBIGUOUS_ANCHOR,
    DISP_COMPILE_IMPORT_ERROR,
    DISP_TIMEOUT,
})


class AnchorResolutionError(LookupError):
    """Raised when an AST mutation anchor is missing, ambiguous, or structurally invalid."""


@dataclass(frozen=True)
class MutationSpec:
    """Complete specification for a single in-memory AST mutation."""

    mutation_id: str
    operator: str
    module_name: str
    file_path: str
    lineno: int
    qualname: str
    source_text: str
    selector: str | tuple | None = None
    catalogue_node: str | None = None
    catalogue_selector: str | None = None


@dataclass(frozen=True)
class MutationProofResult:
    """Outcome record for a single AST mutation proof execution."""

    mutation_id: str
    disposition: str
    killed: bool
    control_passed: bool
    detail: str
    elapsed_vm_ms: int


# Catalogue mutation obligations preserved with exact source text and catalogue descriptors.
CATALOGUE_MUTATIONS = (
    MutationSpec(
        mutation_id="M-wender-perceive-parallel-perceive-1",
        operator="O",
        module_name="wender.perceive.parallel_perceive",
        file_path="wender/perceive/parallel_perceive.py",
        lineno=140,
        qualname="_prepare",
        source_text=(
            "not isinstance(digest, str) or len(digest) != 64\n"
            "            or digest != actual_digest"
        ),
        selector="op",
        catalogue_node=(
            "not isinstance(digest, str) or len(digest) differs from 64\n"
            " or digest differs from actual_digest"
        ),
        catalogue_selector="op",
    ),
    MutationSpec(
        mutation_id="M-wender-perceive-parallel-perceive-2",
        operator="O",
        module_name="wender.perceive.parallel_perceive",
        file_path="wender/perceive/parallel_perceive.py",
        lineno=110,
        qualname="_prepare",
        source_text='_handle_key(_structural_handle(state["handle"])) != key',
        selector="ops element 0",
        catalogue_node=(
            '_handle_key(_structural_handle(state left square bracket "handle" '
            "right square bracket )) differs from key"
        ),
        catalogue_selector="ops element 0",
    ),
)


_COMPARE_O_MAP = {
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

_COMPARE_B_MAP = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
}

_BOOLOP_O_MAP = {
    ast.And: ast.Or,
    ast.Or: ast.And,
}

_BINOP_O_MAP = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.FloorDiv,
    ast.FloorDiv: ast.Mult,
}


def _catalogue_sanitize_text(raw: str) -> str:
    """Convert raw Python source text into the prompt's sanitized catalogue notation."""
    text = (
        raw.replace("!=", "differs from")
        .replace("[", " left square bracket ")
        .replace("]", " right square bracket ")
    )
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n ".join(lines) if len(lines) > 1 else (lines[0] if lines else "")


def _parse_ops_index(selector: str | tuple | None, ops_len: int) -> int:
    if selector is None or selector in ("op", "compare", "Compare.ops[0]"):
        if ops_len != 1:
            raise AnchorResolutionError(
                f"ambiguous Compare with {ops_len} ops requires explicit index selector"
            )
        return 0
    if isinstance(selector, tuple) and len(selector) == 2 and selector[0] == "ops":
        idx = selector[1]
        if type(idx) is int and 0 <= idx < ops_len:
            return idx
        raise AnchorResolutionError(f"Compare ops index {idx!r} out of range (0..{ops_len - 1})")
    if isinstance(selector, str):
        s = selector.strip()
        if s.startswith("ops element "):
            tail = s[len("ops element "):].strip()
            if tail.isdigit():
                idx = int(tail)
                if 0 <= idx < ops_len:
                    return idx
        if s.startswith("ops[") and s.endswith("]"):
            tail = s[4:-1].strip()
            if tail.isdigit():
                idx = int(tail)
                if 0 <= idx < ops_len:
                    return idx
    raise AnchorResolutionError(f"invalid Compare selector: {selector!r}")


def _is_comparison_selector(selector: str | tuple | None) -> bool:
    if selector is None:
        return False
    if isinstance(selector, tuple) and len(selector) == 2 and selector[0] == "ops":
        return True
    if isinstance(selector, str):
        s = selector.strip()
        return (
            s == "compare"
            or s.startswith("ops[")
            or s.startswith("ops element ")
            or s.startswith("Compare.")
        )
    return False


def _is_constant_selector(selector: str | tuple | None) -> bool:
    if selector is None:
        return False
    if isinstance(selector, str):
        return selector.strip() in ("Constant", "constant", "int_constant", "value")
    return False


def _node_matches_operator_and_selector(
    node: ast.AST,
    operator: str,
    selector: str | tuple | None,
) -> bool:
    if operator == "O":
        if isinstance(node, ast.Compare):
            try:
                idx = _parse_ops_index(selector, len(node.ops))
            except AnchorResolutionError:
                return False
            return type(node.ops[idx]) in _COMPARE_O_MAP
        if isinstance(node, ast.BoolOp):
            if selector not in (None, "op", "BoolOp.op"):
                return False
            return type(node.op) in _BOOLOP_O_MAP
        if isinstance(node, ast.BinOp):
            if selector not in (None, "op", "BinOp.op"):
                return False
            return type(node.op) in _BINOP_O_MAP
        return False

    if operator == "B":
        if _is_comparison_selector(selector):
            if not isinstance(node, ast.Compare):
                return False
            try:
                idx = _parse_ops_index(selector, len(node.ops))
            except AnchorResolutionError:
                return False
            return type(node.ops[idx]) in _COMPARE_B_MAP
        if _is_constant_selector(selector):
            return isinstance(node, ast.Constant) and type(node.value) is int
        if isinstance(node, ast.Compare):
            try:
                idx = _parse_ops_index(selector, len(node.ops))
            except AnchorResolutionError:
                return False
            return type(node.ops[idx]) in _COMPARE_B_MAP
        if isinstance(node, ast.Constant) and type(node.value) is int:
            return True
        return False

    if operator == "D":
        if isinstance(node, ast.If):
            if selector not in (None, "test", "If.test", "raising_if"):
                return False
            return any(isinstance(stmt, ast.Raise) for stmt in ast.walk(ast.Module(body=node.body, type_ignores=[])))
        if isinstance(node, ast.Assert):
            return selector in (None, "test", "Assert.test", "assert")
        return False

    if operator == "S":
        if not isinstance(node, ast.Call):
            return False
        if selector not in (None, "args", "Call.args", "args[0,1]"):
            return False
        if len(node.args) < 2:
            return False
        a0, a1 = node.args[0], node.args[1]
        if isinstance(a0, ast.Starred) or isinstance(a1, ast.Starred):
            return False
        return ast.dump(a0) != ast.dump(a1)

    return False


def _apply_mutation_to_node(
    node: ast.AST,
    operator: str,
    selector: str | tuple | None,
) -> ast.AST:
    if operator == "O":
        if isinstance(node, ast.Compare):
            idx = _parse_ops_index(selector, len(node.ops))
            op_cls = type(node.ops[idx])
            if op_cls not in _COMPARE_O_MAP:
                raise AnchorResolutionError(f"unsupported Compare op for O: {op_cls.__name__}")
            node.ops[idx] = _COMPARE_O_MAP[op_cls]()
            return node
        if isinstance(node, ast.BoolOp):
            if selector not in (None, "op", "BoolOp.op"):
                raise AnchorResolutionError(f"invalid BoolOp selector: {selector!r}")
            op_cls = type(node.op)
            if op_cls not in _BOOLOP_O_MAP:
                raise AnchorResolutionError(f"unsupported BoolOp for O: {op_cls.__name__}")
            node.op = _BOOLOP_O_MAP[op_cls]()
            return node
        if isinstance(node, ast.BinOp):
            if selector not in (None, "op", "BinOp.op"):
                raise AnchorResolutionError(f"invalid BinOp selector: {selector!r}")
            op_cls = type(node.op)
            if op_cls not in _BINOP_O_MAP:
                raise AnchorResolutionError(f"unsupported BinOp for O: {op_cls.__name__}")
            node.op = _BINOP_O_MAP[op_cls]()
            return node
        raise AnchorResolutionError(f"O unsupported on AST node {type(node).__name__}")

    if operator == "B":
        if _is_comparison_selector(selector):
            if not isinstance(node, ast.Compare):
                raise AnchorResolutionError(
                    "comparison selector in B must target ast.Compare, never Constant"
                )
            idx = _parse_ops_index(selector, len(node.ops))
            op_cls = type(node.ops[idx])
            if op_cls not in _COMPARE_B_MAP:
                raise AnchorResolutionError(
                    f"B on Compare requires Lt/LtE/Gt/GtE, got {op_cls.__name__}"
                )
            node.ops[idx] = _COMPARE_B_MAP[op_cls]()
            return node
        if _is_constant_selector(selector):
            if not isinstance(node, ast.Constant) or type(node.value) is not int:
                raise AnchorResolutionError(
                    "Constant selector in B requires exact integer ast.Constant"
                )
            node.value = node.value + 1
            return node
        if isinstance(node, ast.Compare):
            idx = _parse_ops_index(selector, len(node.ops))
            op_cls = type(node.ops[idx])
            if op_cls not in _COMPARE_B_MAP:
                raise AnchorResolutionError(
                    f"B on Compare requires Lt/LtE/Gt/GtE, got {op_cls.__name__}"
                )
            node.ops[idx] = _COMPARE_B_MAP[op_cls]()
            return node
        if isinstance(node, ast.Constant) and type(node.value) is int:
            node.value = node.value + 1
            return node
        raise AnchorResolutionError(f"B unsupported on AST node {type(node).__name__}")

    if operator == "D":
        if isinstance(node, ast.If):
            has_raise = any(
                isinstance(stmt, ast.Raise)
                for stmt in ast.walk(ast.Module(body=node.body, type_ignores=[]))
            )
            if not has_raise:
                raise AnchorResolutionError("D on If requires a raising If branch")
            node.test = ast.copy_location(ast.Constant(value=False), node.test)
            return node
        if isinstance(node, ast.Assert):
            node.test = ast.copy_location(ast.Constant(value=True), node.test)
            return node
        raise AnchorResolutionError(f"D unsupported on AST node {type(node).__name__}")

    if operator == "S":
        if not isinstance(node, ast.Call):
            raise AnchorResolutionError(f"S requires ast.Call, got {type(node).__name__}")
        if len(node.args) < 2:
            raise AnchorResolutionError("S requires at least two positional Call arguments")
        a0, a1 = node.args[0], node.args[1]
        if isinstance(a0, ast.Starred) or isinstance(a1, ast.Starred):
            raise AnchorResolutionError("S requires non-starred first two positional arguments")
        if ast.dump(a0) == ast.dump(a1):
            raise AnchorResolutionError("S requires distinct first two positional arguments")
        node.args[0], node.args[1] = node.args[1], node.args[0]
        return node

    raise AnchorResolutionError(f"unknown mutation operator: {operator!r}")


def _find_qualified_functions(tree: ast.Module) -> dict[str, list[ast.AST]]:
    scopes: dict[str, list[ast.AST]] = {}

    def visit_block(body: list[ast.stmt], prefix: str = "") -> None:
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qname = f"{prefix}.{stmt.name}" if prefix else stmt.name
                scopes.setdefault(qname, []).append(stmt)
                visit_block(stmt.body, qname)
            elif isinstance(stmt, ast.ClassDef):
                cname = f"{prefix}.{stmt.name}" if prefix else stmt.name
                visit_block(stmt.body, cname)

    visit_block(tree.body)
    return scopes


def _build_mutated_ast(source: str, spec: MutationSpec) -> ast.Module:
    tree = ast.parse(source, filename=spec.file_path)
    scopes = _find_qualified_functions(tree)
    func_nodes = scopes.get(spec.qualname, [])
    if len(func_nodes) == 0:
        raise AnchorResolutionError(
            f"missing qualified function {spec.qualname!r} in {spec.file_path}"
        )
    if len(func_nodes) > 1:
        raise AnchorResolutionError(
            f"ambiguous qualified function {spec.qualname!r} in {spec.file_path}"
        )
    target_func = func_nodes[0]

    candidates = []
    for sub in ast.walk(target_func):
        if getattr(sub, "lineno", None) != spec.lineno:
            continue
        seg = ast.get_source_segment(source, sub)
        if seg != spec.source_text:
            continue
        if spec.catalogue_node is not None:
            if _catalogue_sanitize_text(seg) != " ".join(spec.catalogue_node.split()).replace(
                " or digest differs from actual_digest", "\n or digest differs from actual_digest"
            ) and _catalogue_sanitize_text(seg).replace("\n ", " ") != " ".join(
                spec.catalogue_node.split()
            ):
                raise AnchorResolutionError(
                    f"catalogue anchor mismatch for {spec.mutation_id}"
                )
        if _node_matches_operator_and_selector(sub, spec.operator, spec.selector):
            candidates.append(sub)

    if len(candidates) == 0:
        raise AnchorResolutionError(
            f"missing AST anchor for {spec.mutation_id} at {spec.file_path}:{spec.lineno} "
            f"in {spec.qualname} ({spec.source_text!r}, selector={spec.selector!r})"
        )
    if len(candidates) > 1:
        raise AnchorResolutionError(
            f"ambiguous AST anchor for {spec.mutation_id}: {len(candidates)} matches"
        )

    _apply_mutation_to_node(candidates[0], spec.operator, spec.selector)
    ast.fix_missing_locations(tree)
    return tree


def _run_with_timeout(fn, arg, timeout_s: float):
    outcome = {"status": "timeout", "exc": None, "value": None}
    done = threading.Event()

    def target():
        try:
            outcome["value"] = fn(arg)
            outcome["status"] = "ok"
        except BaseException as exc:
            outcome["exc"] = exc
            outcome["status"] = "exc"
        finally:
            done.set()

    worker = threading.Thread(target=target, name="ast-prove-worker", daemon=True)
    worker.start()
    finished = done.wait(timeout=timeout_s)
    if not finished:
        return "timeout", None
    worker.join(timeout=timeout_s)
    return outcome["status"], outcome["exc"] if outcome["status"] == "exc" else outcome["value"]


def prove_mutation(
    spec: MutationSpec,
    assertion_fn,
    *,
    equivalence_fn=None,
    timeout_s: float = 2.0,
) -> MutationProofResult:
    """Execute a complete in-memory AST mutation proof without touching disk or bytecode."""
    t0 = time.monotonic_ns()
    full_path = REPO_ROOT / spec.file_path
    if not full_path.is_file():
        elapsed = (time.monotonic_ns() - t0) // 1_000_000
        return MutationProofResult(
            mutation_id=spec.mutation_id,
            disposition=DISP_MISSING_ANCHOR,
            killed=False,
            control_passed=False,
            detail=f"source file not found: {spec.file_path}",
            elapsed_vm_ms=elapsed,
        )

    source = full_path.read_text(encoding="utf-8")

    # 1. Resolve anchor and build mutated AST before running control/mutant
    try:
        tree = ast.parse(source, filename=spec.file_path)
        scopes = _find_qualified_functions(tree)
        func_nodes = scopes.get(spec.qualname, [])
        if len(func_nodes) == 0:
            elapsed = (time.monotonic_ns() - t0) // 1_000_000
            return MutationProofResult(
                mutation_id=spec.mutation_id,
                disposition=DISP_MISSING_ANCHOR,
                killed=False,
                control_passed=False,
                detail=f"missing function {spec.qualname!r}",
                elapsed_vm_ms=elapsed,
            )
        if len(func_nodes) > 1:
            elapsed = (time.monotonic_ns() - t0) // 1_000_000
            return MutationProofResult(
                mutation_id=spec.mutation_id,
                disposition=DISP_AMBIGUOUS_ANCHOR,
                killed=False,
                control_passed=False,
                detail=f"ambiguous function {spec.qualname!r}",
                elapsed_vm_ms=elapsed,
            )

        matched_by_text = [
            sub
            for sub in ast.walk(func_nodes[0])
            if getattr(sub, "lineno", None) == spec.lineno
            and ast.get_source_segment(source, sub) == spec.source_text
            and _node_matches_operator_and_selector(sub, spec.operator, spec.selector)
        ]
        if len(matched_by_text) == 0:
            elapsed = (time.monotonic_ns() - t0) // 1_000_000
            return MutationProofResult(
                mutation_id=spec.mutation_id,
                disposition=DISP_MISSING_ANCHOR,
                killed=False,
                control_passed=False,
                detail=f"no matching AST anchor at {spec.file_path}:{spec.lineno}",
                elapsed_vm_ms=elapsed,
            )
        if len(matched_by_text) > 1:
            elapsed = (time.monotonic_ns() - t0) // 1_000_000
            return MutationProofResult(
                mutation_id=spec.mutation_id,
                disposition=DISP_AMBIGUOUS_ANCHOR,
                killed=False,
                control_passed=False,
                detail=f"ambiguous AST anchor: {len(matched_by_text)} matches",
                elapsed_vm_ms=elapsed,
            )
        mutated_tree = _build_mutated_ast(source, spec)
    except AnchorResolutionError as exc:
        elapsed = (time.monotonic_ns() - t0) // 1_000_000
        disp = DISP_AMBIGUOUS_ANCHOR if "ambiguous" in str(exc) else DISP_MISSING_ANCHOR
        return MutationProofResult(
            mutation_id=spec.mutation_id,
            disposition=disp,
            killed=False,
            control_passed=False,
            detail=str(exc),
            elapsed_vm_ms=elapsed,
        )

    # 2. Run the exact assertion on the real unmutated module first
    try:
        real_mod = importlib.import_module(spec.module_name)
    except Exception as exc:
        elapsed = (time.monotonic_ns() - t0) // 1_000_000
        return MutationProofResult(
            mutation_id=spec.mutation_id,
            disposition=DISP_COMPILE_IMPORT_ERROR,
            killed=False,
            control_passed=False,
            detail=f"real module import failed: {exc}",
            elapsed_vm_ms=elapsed,
        )

    ctrl_status, ctrl_payload = _run_with_timeout(assertion_fn, real_mod, timeout_s)
    if ctrl_status != "ok":
        elapsed = (time.monotonic_ns() - t0) // 1_000_000
        return MutationProofResult(
            mutation_id=spec.mutation_id,
            disposition=DISP_FAILED_CONTROL,
            killed=False,
            control_passed=False,
            detail=f"control failed on real code: {ctrl_status} ({ctrl_payload!r})",
            elapsed_vm_ms=elapsed,
        )

    # 3. Compile and execute mutated AST in memory, preserving dataclass sys.modules registration
    saved_modules = dict(sys.modules)
    saved_dont_write = sys.dont_write_bytecode
    private_name = f"{spec.module_name}__mutant_{spec.mutation_id.replace('-', '_')}"
    try:
        sys.dont_write_bytecode = True
        try:
            code_obj = compile(mutated_tree, filename=f"<memory:{spec.file_path}>", mode="exec")
            mutant_mod = types.ModuleType(spec.module_name)
            mutant_mod.__file__ = getattr(real_mod, "__file__", spec.file_path)
            mutant_mod.__package__ = getattr(real_mod, "__package__", spec.module_name.rpartition(".")[0])
            mutant_mod.__loader__ = getattr(real_mod, "__loader__", None)
            mutant_mod.__spec__ = getattr(real_mod, "__spec__", None)
            # Register both canonical and private module names in sys.modules before exec
            # so @dataclass inspection of sys.modules[cls.__module__] succeeds in memory.
            sys.modules[spec.module_name] = mutant_mod
            sys.modules[private_name] = mutant_mod
            exec(code_obj, mutant_mod.__dict__)
        except Exception as exc:
            elapsed = (time.monotonic_ns() - t0) // 1_000_000
            return MutationProofResult(
                mutation_id=spec.mutation_id,
                disposition=DISP_COMPILE_IMPORT_ERROR,
                killed=False,
                control_passed=True,
                detail=f"in-memory compile/exec error: {type(exc).__name__}: {exc}",
                elapsed_vm_ms=elapsed,
            )

        mut_status, mut_payload = _run_with_timeout(assertion_fn, mutant_mod, timeout_s)
        elapsed = (time.monotonic_ns() - t0) // 1_000_000

        if mut_status == "timeout":
            return MutationProofResult(
                mutation_id=spec.mutation_id,
                disposition=DISP_TIMEOUT,
                killed=False,
                control_passed=True,
                detail=f"mutant timed out after {timeout_s}s [VM]",
                elapsed_vm_ms=elapsed,
            )

        if mut_status == "exc":
            if isinstance(mut_payload, AssertionError):
                return MutationProofResult(
                    mutation_id=spec.mutation_id,
                    disposition=DISP_KILLED,
                    killed=True,
                    control_passed=True,
                    detail=f"killed by semantic AssertionError: {mut_payload}",
                    elapsed_vm_ms=elapsed,
                )
            return MutationProofResult(
                mutation_id=spec.mutation_id,
                disposition=DISP_COMPILE_IMPORT_ERROR,
                killed=False,
                control_passed=True,
                detail=f"non-AssertionError runtime failure: {type(mut_payload).__name__}: {mut_payload}",
                elapsed_vm_ms=elapsed,
            )

        # Mutant did not fail assertion: check if proven equivalent
        if equivalence_fn is not None and equivalence_fn(real_mod, mutant_mod):
            return MutationProofResult(
                mutation_id=spec.mutation_id,
                disposition=DISP_EQUIVALENT,
                killed=False,
                control_passed=True,
                detail="proven semantic equivalent",
                elapsed_vm_ms=elapsed,
            )

        return MutationProofResult(
            mutation_id=spec.mutation_id,
            disposition=DISP_SURVIVED,
            killed=False,
            control_passed=True,
            detail="mutant survived assertion without equivalence proof",
            elapsed_vm_ms=elapsed,
        )
    finally:
        sys.modules.clear()
        sys.modules.update(saved_modules)
        sys.dont_write_bytecode = saved_dont_write


# --- Strict fixture builders and structural envelopes for negative tests ---


@dataclass(frozen=True)
class RawFrameEnvelope:
    """Structural FrameRef collaborator for testing invalid frame inputs without weakening FrameRef."""

    handle: object
    epoch: object
    frame_seq: object
    width: object
    height: object
    rgb: object
    digest: object


@dataclass(frozen=True)
class RawObservationEnvelope:
    """Structural Observation collaborator for testing invalid observation inputs without weakening Observation."""

    handle: object
    epoch: object
    frame_seq: object
    dpr_ppm: object
    state: object
    frame: object


class FingerprintVisionEngine(FakeVisionEngine):
    """Offline deterministic VisionEngineAdapter with optional screen_fingerprint returning bytes."""

    def screen_fingerprint(self, frame: Frame) -> bytes:
        return hashlib.sha256(frame.data).digest()


class OrderControllingRunner:
    """Deterministic runner.map implementation supporting reverse or custom delivery schedules."""

    def __init__(self, permutation=None, fail_after: int | None = None):
        self.permutation = permutation
        self.fail_after = fail_after

    def map(self, fn, jobs):
        job_list = list(jobs)
        if self.permutation is not None:
            ordered = [job_list[i] for i in self.permutation if i < len(job_list)]
        else:
            ordered = list(reversed(job_list))
        for idx, job in enumerate(ordered):
            if self.fail_after is not None and idx >= self.fail_after:
                raise RuntimeError("injected runner failure after partial delivery")
            yield fn(job)


def make_rgb_array(width: int = 16, height: int = 16, fill: int = 0, patch_rect=None) -> np.ndarray:
    arr = np.full((height, width, 3), fill, dtype=np.uint8)
    if patch_rect is not None:
        px, py, pw, ph, val = patch_rect
        arr[py:py + ph, px:px + pw, :] = val
    return arr


def make_valid_state(
    node_id: str = "0000000000000001",
    box=(0, 0, 16, 16),
    dpr_ppm: int = 1000000,
    handle: Handle | dict | None = None,
    epoch: int | None = None,
    actionable: bool = True,
) -> dict:
    node = {
        "id": node_id,
        "role": "button" if actionable else "heading",
        "name": f"Node-{node_id[-4:]}",
        "depth": 0,
        "parent": None,
        "ordinal": 0,
        "states": {},
        "actionable": actionable,
        "box": list(box) if box is not None else None,
    }
    state = {
        "nodes": [node],
        "viewport": {
            "w": 64,
            "h": 64,
            "scroll_x": 0,
            "scroll_y": 0,
            "doc_w": 64,
            "doc_h": 64,
            "dpr_ppm": dpr_ppm,
        },
    }
    if handle is not None:
        if isinstance(handle, dict):
            state["handle"] = dict(handle)
        else:
            state["handle"] = {
                "kind": handle.kind,
                "canonical_id": handle.canonical_id,
                "generation": handle.generation,
            }
    if epoch is not None:
        state["epoch"] = epoch
    return state


def make_strict_observation(
    canonical_id: str = "win_a",
    seq: int = 1,
    fill: int = 10,
    *,
    kind: str = "cdp_tab",
    generation: int = 1,
    epoch: int = 2,
    dpr_ppm: int = 1000000,
    width: int = 16,
    height: int = 16,
    node_id: str = "0000000000000001",
    box=(0, 0, 16, 16),
    patch_rect=None,
    include_state_binding: bool = True,
) -> Observation:
    handle = Handle(kind, canonical_id, generation)
    rgb = make_rgb_array(width=width, height=height, fill=fill, patch_rect=patch_rect)
    frame_ref = FrameRef.from_rgb(handle, epoch, seq, rgb)
    state = make_valid_state(
        node_id=node_id,
        box=box,
        dpr_ppm=dpr_ppm,
        handle=handle if include_state_binding else None,
        epoch=epoch if include_state_binding else None,
    )
    return Observation(handle, epoch, seq, dpr_ppm, state, frame_ref)


# --- Seeded Property Loop and Validity-Preserving Shrinker ---


@dataclass(frozen=True)
class PropertyRunSummary:
    domain: str
    seed: int
    valid_cases: int
    shrink_attempts: int
    minimal_fixture: object | None


def run_seeded_property_domain(
    domain_name: str,
    *,
    seed: int,
    generator_fn,
    is_valid_fn,
    property_fn,
    shrink_candidates_fn,
    target_valid_cases: int = 64,
    max_shrink_steps: int = 32,
    max_generation_attempts: int = 512,
) -> PropertyRunSummary:
    """Run a seeded stdlib random property loop targeting 64 valid cases and <=32 validity-preserving shrinks."""
    rng = random.Random(seed)
    valid_count = 0
    attempts = 0
    while valid_count < target_valid_cases and attempts < max_generation_attempts:
        attempts += 1
        candidate = generator_fn(rng, valid_count)
        if not is_valid_fn(candidate):
            continue
        valid_count += 1
        try:
            property_fn(candidate)
        except AssertionError as exc:
            current = candidate
            shrink_steps = 0
            while shrink_steps < max_shrink_steps:
                progressed = False
                for smaller in shrink_candidates_fn(current):
                    if not is_valid_fn(smaller):
                        continue
                    shrink_steps += 1
                    try:
                        property_fn(smaller)
                    except AssertionError:
                        current = smaller
                        progressed = True
                        break
                    if shrink_steps >= max_shrink_steps:
                        break
                if not progressed:
                    break
            raise AssertionError(
                f"Property failure in domain={domain_name!r} seed={seed} "
                f"case={valid_count} shrink_steps={shrink_steps} "
                f"minimal_fixture={current!r}: {exc}"
            ) from exc

    if valid_count < target_valid_cases:
        raise AssertionError(
            f"Budget exhaustion in domain={domain_name!r} seed={seed}: "
            f"generated {valid_count}/{target_valid_cases} valid cases in {attempts} attempts"
        )
    return PropertyRunSummary(
        domain=domain_name,
        seed=seed,
        valid_cases=valid_count,
        shrink_attempts=0,
        minimal_fixture=None,
    )


class TestAstProveHarnessAndCatalogueObligations(unittest.TestCase):
    """In-memory AST prove harness verification and catalogue mutation kills."""

    def test_catalogue_m_wender_perceive_parallel_perceive_1_killed(self):
        """Kill M-wender-perceive-parallel-perceive-1 (O on BoolOp at parallel_perceive.py:140 in _prepare)."""
        spec = CATALOGUE_MUTATIONS[0]
        self.assertEqual(spec.mutation_id, "M-wender-perceive-parallel-perceive-1")
        self.assertEqual(spec.lineno, 140)
        self.assertEqual(spec.qualname, "_prepare")

        def assertion(mod):
            valid_obs = make_strict_observation("win_1", seq=1, fill=20)
            res_valid = mod.perceive_many([valid_obs], adapter=FakeVisionEngine())
            self.assertIsNone(res_valid[0].error)
            self.assertEqual(res_valid[0].observation.frame.digest, valid_obs.frame.digest)

            # Construct observation with valid 64-char str digest that differs from actual_digest
            forged_frame = RawFrameEnvelope(
                handle=valid_obs.handle,
                epoch=valid_obs.epoch,
                frame_seq=valid_obs.frame_seq,
                width=valid_obs.frame.width,
                height=valid_obs.frame.height,
                rgb=valid_obs.frame.rgb,
                digest="0" * 64,
            )
            forged_obs = RawObservationEnvelope(
                handle=valid_obs.handle,
                epoch=valid_obs.epoch,
                frame_seq=valid_obs.frame_seq,
                dpr_ppm=valid_obs.dpr_ppm,
                state=valid_obs.state,
                frame=forged_frame,
            )
            try:
                mod._prepare(forged_obs)
            except mod.ObservationError as exc:
                self.assertIn("frame content SHA-256 mismatch", str(exc))
            except Exception as exc:
                self.fail(f"unexpected exception from _prepare: {exc}")
            else:
                self.fail("_prepare accepted forged 64-char SHA-256 digest")

            batch_res = mod.perceive_many([forged_obs], adapter=FakeVisionEngine())
            self.assertEqual(len(batch_res), 1)
            self.assertIsNone(batch_res[0].observation)
            self.assertIsNotNone(batch_res[0].error)
            self.assertTrue(batch_res[0].error.startswith("VALIDATION_ERROR:ObservationError:"))

        proof = prove_mutation(spec, assertion)
        self.assertTrue(proof.control_passed)
        self.assertEqual(proof.disposition, DISP_KILLED, proof.detail)
        self.assertTrue(proof.killed)

    def test_catalogue_m_wender_perceive_parallel_perceive_2_killed(self):
        """Kill M-wender-perceive-parallel-perceive-2 (O on Compare at parallel_perceive.py:110 in _prepare)."""
        spec = CATALOGUE_MUTATIONS[1]
        self.assertEqual(spec.mutation_id, "M-wender-perceive-parallel-perceive-2")
        self.assertEqual(spec.lineno, 110)
        self.assertEqual(spec.qualname, "_prepare")
        self.assertEqual(spec.catalogue_selector, "ops element 0")

        def assertion(mod):
            # Matching state["handle"] must succeed without raising ObservationError
            matching_obs = make_strict_observation("win_match", seq=1, fill=40, include_state_binding=True)
            res_ok = mod.perceive_many([matching_obs], adapter=FakeVisionEngine())
            self.assertIsNone(res_ok[0].error, f"matching state['handle'] rejected: {res_ok[0].error}")
            self.assertIsNotNone(res_ok[0].observation)

            try:
                prepared = mod._prepare(matching_obs)
            except Exception as exc:
                self.fail(f"_prepare rejected matching state['handle']: {exc}")
            self.assertEqual(prepared.handle, matching_obs.handle)

            # Mismatched state["handle"] must raise ObservationError("state target handle mismatch")
            foreign_handle = Handle("cdp_tab", "win_foreign", 1)
            mismatched_state = make_valid_state(
                dpr_ppm=1000000,
                handle=foreign_handle,
                epoch=matching_obs.epoch,
            )
            mismatched_obs = Observation(
                matching_obs.handle,
                matching_obs.epoch,
                matching_obs.frame_seq,
                matching_obs.dpr_ppm,
                mismatched_state,
                matching_obs.frame,
            )
            res_bad = mod.perceive_many([mismatched_obs], adapter=FakeVisionEngine())
            self.assertIsNone(res_bad[0].observation)
            self.assertIsNotNone(res_bad[0].error)
            self.assertTrue(res_bad[0].error.startswith("VALIDATION_ERROR:ObservationError:"))

        proof = prove_mutation(spec, assertion)
        self.assertTrue(proof.control_passed)
        self.assertEqual(proof.disposition, DISP_KILLED, proof.detail)
        self.assertTrue(proof.killed)

    def test_ast_prove_operators_o_b_d_s_and_separate_dispositions(self):
        """Verify O, B (Compare & Constant), D (If & Assert), S, and all non-kill dispositions."""
        # 1. Operator B on Compare (dpr <= 0 -> dpr < 0 at parallel_perceive.py:102)
        spec_b_cmp = MutationSpec(
            mutation_id="M-parallel-perceive-b-cmp-dpr",
            operator="B",
            module_name="wender.perceive.parallel_perceive",
            file_path="wender/perceive/parallel_perceive.py",
            lineno=102,
            qualname="_prepare",
            source_text="dpr <= 0",
            selector="ops[0]",
        )

        def assert_dpr_boundary(mod):
            valid = make_strict_observation("w", seq=1, fill=10, dpr_ppm=1)
            res_v = mod.perceive_many([valid], adapter=FakeVisionEngine())
            self.assertIsNone(res_v[0].error)
            zero_dpr = RawObservationEnvelope(
                handle=valid.handle,
                epoch=valid.epoch,
                frame_seq=valid.frame_seq,
                dpr_ppm=0,
                state={"nodes": []},
                frame=valid.frame,
            )
            res_z = mod.perceive_many([zero_dpr], adapter=FakeVisionEngine())
            self.assertIsNotNone(res_z[0].error)
            self.assertIn("dpr_ppm must be a positive integer", res_z[0].error)

        res_b_cmp = prove_mutation(spec_b_cmp, assert_dpr_boundary)
        self.assertEqual(res_b_cmp.disposition, DISP_KILLED, res_b_cmp.detail)
        self.assertTrue(res_b_cmp.killed)

        # 2. Operator B on exact integer Constant (64 -> 65 at parallel_perceive.py:140)
        spec_b_const = MutationSpec(
            mutation_id="M-parallel-perceive-b-const-64",
            operator="B",
            module_name="wender.perceive.parallel_perceive",
            file_path="wender/perceive/parallel_perceive.py",
            lineno=140,
            qualname="_prepare",
            source_text="64",
            selector="Constant",
        )

        def assert_digest_len_64(mod):
            obs = make_strict_observation("w", seq=1, fill=5)
            out = mod.perceive_many([obs], adapter=FakeVisionEngine())
            self.assertIsNone(out[0].error, f"valid 64-hex digest rejected: {out[0].error}")

        res_b_const = prove_mutation(spec_b_const, assert_digest_len_64)
        self.assertEqual(res_b_const.disposition, DISP_KILLED, res_b_const.detail)
        self.assertTrue(res_b_const.killed)

        # Verify B never substitutes a constant increment for a comparison selector
        spec_b_invalid_sub = MutationSpec(
            mutation_id="M-invalid-b-selector",
            operator="B",
            module_name="wender.perceive.parallel_perceive",
            file_path="wender/perceive/parallel_perceive.py",
            lineno=140,
            qualname="_prepare",
            source_text="len(digest) != 64",
            selector="ops[0]",
        )
        res_b_invalid = prove_mutation(spec_b_invalid_sub, lambda mod: None)
        self.assertEqual(res_b_invalid.disposition, DISP_MISSING_ANCHOR)
        self.assertFalse(res_b_invalid.killed)

        # 3. Operator D on raising If (parallel_perceive.py:222 geometry change check)
        spec_d_if = MutationSpec(
            mutation_id="M-parallel-perceive-d-geometry-if",
            operator="D",
            module_name="wender.perceive.parallel_perceive",
            file_path="wender/perceive/parallel_perceive.py",
            lineno=222,
            qualname="perceive_many",
            source_text=(
                "if key in geometry and geometry[key] != dimensions:\n"
                '                raise ObservationError("geometry changed without changing epoch")'
            ),
            selector="If.test",
        )

        def assert_geom_change_rejected(mod):
            o1 = make_strict_observation("g", seq=1, fill=10, epoch=1, width=16, height=16)
            o2 = make_strict_observation("g", seq=2, fill=20, epoch=1, width=32, height=16)
            out = mod.perceive_many([o1, o2], adapter=FakeVisionEngine())
            self.assertIsNone(out[0].error)
            self.assertIsNotNone(out[1].error)
            self.assertIn("geometry changed without changing epoch", out[1].error)

        res_d_if = prove_mutation(spec_d_if, assert_geom_change_rejected)
        self.assertEqual(res_d_if.disposition, DISP_KILLED, res_d_if.detail)
        self.assertTrue(res_d_if.killed)

        # 4. Operator S on Call (swapping first two distinct non-starred positional args)
        spec_s_call = MutationSpec(
            mutation_id="M-parallel-perceive-s-exact-nonneg",
            operator="S",
            module_name="wender.perceive.parallel_perceive",
            file_path="wender/perceive/parallel_perceive.py",
            lineno=95,
            qualname="_prepare",
            source_text='_exact_nonnegative("epoch", observation.epoch)',
            selector="Call.args",
        )

        def assert_epoch_nonneg_call(mod):
            obs = make_strict_observation("s", seq=1, fill=10)
            out = mod.perceive_many([obs], adapter=FakeVisionEngine())
            self.assertIsNone(out[0].error, f"swapped _exact_nonnegative args failed: {out[0].error}")

        res_s_call = prove_mutation(spec_s_call, assert_epoch_nonneg_call)
        self.assertEqual(res_s_call.disposition, DISP_KILLED, res_s_call.detail)
        self.assertTrue(res_s_call.killed)

        # 5. Proven Equivalent disposition: S on zip(prev_data[start:end], cur_data[start:end])
        # at wender/perceive/vision_adapter.py:122 (pixel difference abs(a - b) is symmetric!)
        spec_equiv = MutationSpec(
            mutation_id="M-vision-adapter-s-zip-symmetric-diff",
            operator="S",
            module_name="wender.perceive.vision_adapter",
            file_path="wender/perceive/vision_adapter.py",
            lineno=122,
            qualname="FakeVisionEngine.changed_regions",
            source_text="zip(prev_data[start:end], cur_data[start:end])",
            selector="Call.args",
        )

        def assert_changed_regions(mod):
            eng = mod.FakeVisionEngine(tile=16, threshold=8)
            f_prev = mod.Frame(32, 32, bytes([10] * (32 * 32 * 3)))
            buf = bytearray([10] * (32 * 32 * 3))
            for idx in range(0, 16 * 3):
                buf[idx] = 200
            f_cur = mod.Frame(32, 32, bytes(buf))
            regs = eng.changed_regions(f_prev, f_cur)
            self.assertEqual(regs, [{"x": 0, "y": 0, "w": 16, "h": 16, "tiles": 1}])

        def prove_zip_symmetry_equivalence(real_m, mut_m) -> bool:
            rng = random.Random(20261020)
            r_eng = real_m.FakeVisionEngine(tile=8, threshold=5)
            m_eng = mut_m.FakeVisionEngine(tile=8, threshold=5)
            for _ in range(16):
                w = rng.choice((8, 16, 20))
                h = rng.choice((8, 16, 18))
                b1 = rng.randbytes(w * h * 3)
                b2 = rng.randbytes(w * h * 3)
                rf1, rf2 = real_m.Frame(w, h, b1), real_m.Frame(w, h, b2)
                mf1, mf2 = mut_m.Frame(w, h, b1), mut_m.Frame(w, h, b2)
                if r_eng.changed_regions(rf1, rf2) != m_eng.changed_regions(mf1, mf2):
                    return False
                if r_eng.changed_regions(rf1, rf2) != r_eng.changed_regions(rf2, rf1):
                    return False
            return True

        res_equiv = prove_mutation(
            spec_equiv,
            assert_changed_regions,
            equivalence_fn=prove_zip_symmetry_equivalence,
        )
        self.assertEqual(res_equiv.disposition, DISP_EQUIVALENT)
        self.assertFalse(res_equiv.killed)
        self.assertIn(res_equiv.disposition, NON_KILL_DISPOSITIONS)

        # 6. Survivor disposition (assertion does not exercise the mutated branch)
        res_survived = prove_mutation(spec_b_cmp, lambda mod: self.assertTrue(hasattr(mod, "perceive_many")))
        self.assertEqual(res_survived.disposition, DISP_SURVIVED)
        self.assertFalse(res_survived.killed)

        # 7. Failed control disposition (assertion fails on unmutated real code)
        res_failed_ctrl = prove_mutation(
            spec_b_cmp,
            lambda mod: self.assertEqual(1, 2, "intentional control failure"),
        )
        self.assertEqual(res_failed_ctrl.disposition, DISP_FAILED_CONTROL)
        self.assertFalse(res_failed_ctrl.killed)
        self.assertFalse(res_failed_ctrl.control_passed)

        # 8. Missing anchor disposition
        spec_missing = MutationSpec(
            mutation_id="M-missing-anchor",
            operator="O",
            module_name="wender.perceive.parallel_perceive",
            file_path="wender/perceive/parallel_perceive.py",
            lineno=9999,
            qualname="_prepare",
            source_text="nonexistent_var != 0",
            selector="ops[0]",
        )
        res_missing = prove_mutation(spec_missing, lambda mod: None)
        self.assertEqual(res_missing.disposition, DISP_MISSING_ANCHOR)
        self.assertFalse(res_missing.killed)

        # 9. Non-AssertionError runtime exception on mutant -> COMPILE_IMPORT_ERROR disposition
        res_runtime_err = prove_mutation(
            spec_s_call,
            lambda mod: mod._prepare(make_strict_observation("s", seq=1, fill=10)),
        )
        self.assertEqual(res_runtime_err.disposition, DISP_COMPILE_IMPORT_ERROR)
        self.assertFalse(res_runtime_err.killed)

    def test_ast_operator_mapping_tables_complete(self):
        """Verify all O, B, D, and S AST transformations directly in memory."""
        # O on Compare: Lt<->Gt, LtE<->GtE, Eq<->NotEq, Is<->IsNot, In<->NotIn
        compare_pairs = (
            ("a < b", ast.Gt),
            ("a > b", ast.Lt),
            ("a <= b", ast.GtE),
            ("a >= b", ast.LtE),
            ("a == b", ast.NotEq),
            ("a != b", ast.Eq),
            ("a is b", ast.IsNot),
            ("a is not b", ast.Is),
            ("a in b", ast.NotIn),
            ("a not in b", ast.In),
        )
        for expr, expected_cls in compare_pairs:
            node = ast.parse(expr, mode="eval").body
            mut = _apply_mutation_to_node(node, "O", "ops[0]")
            self.assertIsInstance(mut.ops[0], expected_cls, expr)

        # O on BoolOp: And<->Or
        for expr, expected_cls in (("a and b", ast.Or), ("a or b", ast.And)):
            node = ast.parse(expr, mode="eval").body
            mut = _apply_mutation_to_node(node, "O", "op")
            self.assertIsInstance(mut.op, expected_cls, expr)

        # O on BinOp: Add<->Sub, Mult<->FloorDiv
        for expr, expected_cls in (
            ("a + b", ast.Sub),
            ("a - b", ast.Add),
            ("a * b", ast.FloorDiv),
            ("a // b", ast.Mult),
        ):
            node = ast.parse(expr, mode="eval").body
            mut = _apply_mutation_to_node(node, "O", "op")
            self.assertIsInstance(mut.op, expected_cls, expr)

        # B on Compare: Lt<->LtE, Gt<->GtE
        for expr, expected_cls in (
            ("a < b", ast.LtE),
            ("a <= b", ast.Lt),
            ("a > b", ast.GtE),
            ("a >= b", ast.Gt),
        ):
            node = ast.parse(expr, mode="eval").body
            mut = _apply_mutation_to_node(node, "B", "ops[0]")
            self.assertIsInstance(mut.ops[0], expected_cls, expr)

        # B on Constant increments exact int by 1 and rejects bool
        int_node = ast.Constant(value=41)
        _apply_mutation_to_node(int_node, "B", "Constant")
        self.assertEqual(int_node.value, 42)
        with self.assertRaises(AnchorResolutionError):
            _apply_mutation_to_node(ast.Constant(value=True), "B", "Constant")
        with self.assertRaises(AnchorResolutionError):
            _apply_mutation_to_node(ast.Constant(value=41), "B", "ops[0]")

        # D on raising If -> False, and D on Assert -> True
        if_node = ast.parse("if cond:\n    raise ValueError('bad')\n").body[0]
        _apply_mutation_to_node(if_node, "D", "If.test")
        self.assertIsInstance(if_node.test, ast.Constant)
        self.assertIs(if_node.test.value, False)

        assert_node = ast.parse("assert x > 0").body[0]
        _apply_mutation_to_node(assert_node, "D", "Assert.test")
        self.assertIsInstance(assert_node.test, ast.Constant)
        self.assertIs(assert_node.test.value, True)

        # S on Call swaps first two distinct non-starred positional arguments
        call_node = ast.parse("fn(first_arg, second_arg, third_arg, k=1)", mode="eval").body
        _apply_mutation_to_node(call_node, "S", "Call.args")
        self.assertEqual([a.id for a in call_node.args], ["second_arg", "first_arg", "third_arg"])
        with self.assertRaises(AnchorResolutionError):
            _apply_mutation_to_node(ast.parse("fn(same, same)", mode="eval").body, "S", "Call.args")
        with self.assertRaises(AnchorResolutionError):
            _apply_mutation_to_node(ast.parse("fn(*starred, second)", mode="eval").body, "S", "Call.args")

    def test_multiwin_auditor_mutations_killed(self):
        """Kill AST mutations on wender/perceive/multiwin_perception_audit.py."""
        src_lines = (REPO_ROOT / "wender/perceive/multiwin_perception_audit.py").read_text(encoding="utf-8").splitlines()
        gen_zero_line = next(
            i for i, line in enumerate(src_lines, 1) if line.strip() == "if generation == 0:"
        )
        spec_audit_o = MutationSpec(
            mutation_id="M-audit-canonical-identity-gen-zero",
            operator="O",
            module_name="wender.perceive.multiwin_perception_audit",
            file_path="wender/perceive/multiwin_perception_audit.py",
            lineno=gen_zero_line,
            qualname="canonical_window_identity",
            source_text="generation == 0",
            selector="ops[0]",
        )

        def assert_canonical_identity(mod):
            h = Handle("cdp_tab", "win_ok", 1)
            try:
                got = mod.canonical_window_identity(h)
            except Exception as exc:
                self.fail(f"canonical_window_identity rejected valid handle: {exc}")
            self.assertEqual(got, ("cdp_tab", "win_ok", 1))
            try:
                mod.canonical_window_identity({"kind": "cdp_tab", "canonical_id": "win_ok", "generation": 0})
            except ObservationError:
                pass
            except Exception as exc:
                self.fail(f"unexpected exception for generation=0: {exc}")
            else:
                self.fail("generation=0 was not rejected")

        proof_o = prove_mutation(spec_audit_o, assert_canonical_identity)
        self.assertEqual(proof_o.disposition, DISP_KILLED, proof_o.detail)
        self.assertTrue(proof_o.killed)

        # Mutation 2: D on raising If `if generation == 0:` in canonical_window_identity
        spec_audit_d = MutationSpec(
            mutation_id="M-audit-canonical-identity-drop-gen-zero-if",
            operator="D",
            module_name="wender.perceive.multiwin_perception_audit",
            file_path="wender/perceive/multiwin_perception_audit.py",
            lineno=gen_zero_line,
            qualname="canonical_window_identity",
            source_text='if generation == 0:\n        raise ObservationError("generation must be a positive integer")',
            selector="If.test",
        )
        proof_d = prove_mutation(spec_audit_d, assert_canonical_identity)
        self.assertEqual(proof_d.disposition, DISP_KILLED, proof_d.detail)
        self.assertTrue(proof_d.killed)


class TestMultiWindowPerceptionAuditor(unittest.TestCase):
    """Deterministic unit tests for stale, foreign, missing, and disagreeing diagnostics."""

    def test_authentic_multiwindow_batch_and_reverse_delivery(self):
        """Multi-window batch with distinct DPRs, epochs, and changed regions audits as AUTHENTIC."""
        obs_a1 = make_strict_observation(
            "win_a", seq=1, fill=10, kind="cdp_tab", generation=1, epoch=2, dpr_ppm=1000000, box=(0, 0, 16, 16)
        )
        obs_b1 = make_strict_observation(
            "win_b", seq=1, fill=30, kind="x11_window", generation=2, epoch=5, dpr_ppm=2000000, box=(0, 0, 8, 8)
        )
        obs_a2 = make_strict_observation(
            "win_a", seq=2, fill=200, kind="cdp_tab", generation=1, epoch=2, dpr_ppm=1000000, box=(0, 0, 16, 16)
        )
        obs_b2 = make_strict_observation(
            "win_b", seq=2, fill=220, kind="x11_window", generation=2, epoch=5, dpr_ppm=2000000, box=(0, 0, 8, 8)
        )
        items = [obs_a1, obs_b1, obs_a2, obs_b2]
        adapter = FingerprintVisionEngine(tile=16, threshold=8)
        runner = OrderControllingRunner()

        results, report = perceive_and_audit(
            items,
            adapter=adapter,
            runner=runner,
            expected_targets=[obs_a1.handle, obs_b1.handle],
            expected_epochs={"win_a": 2, "win_b": 5},
            expected_dpr_ppm={"win_a": 1000000, "win_b": 2000000},
        )
        self.assertTrue(report.authentic)
        self.assertTrue(report.ok)
        self.assertEqual(report.verdict, VERDICT_AUTHENTIC)
        self.assertEqual(report.authentic_count, 2)
        self.assertEqual(report.rejected_count, 0)
        self.assertEqual(len(report.targets), 2)

        diag_a = report.for_target(obs_a1.handle)
        diag_b = report.for_target("win_b")
        self.assertIsNotNone(diag_a)
        self.assertIsNotNone(diag_b)
        self.assertTrue(diag_a.authentic)
        self.assertTrue(diag_b.authentic)
        self.assertEqual(diag_a.categories, ())
        self.assertEqual(diag_b.categories, ())
        self.assertEqual(diag_a.observed_sequences, (1, 2))
        self.assertEqual(diag_b.observed_sequences, (1, 2))
        self.assertEqual(diag_a.observed_dpr_ppm, 1000000)
        self.assertEqual(diag_b.observed_dpr_ppm, 2000000)
        self.assertEqual(diag_a.latest_digest, obs_a2.frame.digest)
        self.assertEqual(diag_b.latest_digest, obs_b2.frame.digest)

        # Verify canonical JSON serialization contains no floats
        dumped = report.to_json()
        self.assertTrue(dumped.startswith('{"authentic":true,'))

    def test_corrected_parallel_perceive_controls_with_strict_constructors(self):
        """Exercise RAM copy ownership before runner.map and typed partial error receipts without weakening constructors."""
        valid_obs = make_strict_observation("ok_win", seq=1, fill=0)
        writable_pixels = np.zeros((16, 16, 3), dtype=np.uint8)
        writable_digest = hashlib.sha256(writable_pixels.tobytes()).hexdigest()
        mutable_input_obs = RawObservationEnvelope(
            handle=valid_obs.handle,
            epoch=valid_obs.epoch,
            frame_seq=2,
            dpr_ppm=valid_obs.dpr_ppm,
            state=valid_obs.state,
            frame=RawFrameEnvelope(
                handle=valid_obs.handle,
                epoch=valid_obs.epoch,
                frame_seq=2,
                width=16,
                height=16,
                rgb=writable_pixels,
                digest=writable_digest,
            ),
        )

        class BufferMutatingRunner:
            def map(self, fn, jobs):
                writable_pixels.fill(250)
                return (fn(job) for job in jobs)

        owned_res = perceive_many([mutable_input_obs], adapter=FakeVisionEngine(), runner=BufferMutatingRunner())[0]
        self.assertIsNone(owned_res.error)
        self.assertEqual(owned_res.observation.frame.rgb.tobytes(), bytes(16 * 16 * 3))
        self.assertFalse(owned_res.observation.frame.rgb.flags.writeable)

        # Typed partial validation errors alongside valid observation
        wrong_dpr_obs = RawObservationEnvelope(
            handle=Handle("cdp_tab", "bad_dpr", 1),
            epoch=1,
            frame_seq=1,
            dpr_ppm=0,
            state={"nodes": []},
            frame=valid_obs.frame,
        )
        noncontig_arr = np.zeros((16, 32, 3), dtype=np.uint8)[:, ::2, :]
        noncontig_obs = RawObservationEnvelope(
            handle=Handle("cdp_tab", "bad_nc", 1),
            epoch=1,
            frame_seq=1,
            dpr_ppm=1000000,
            state={"nodes": []},
            frame=RawFrameEnvelope(
                handle=Handle("cdp_tab", "bad_nc", 1),
                epoch=1,
                frame_seq=1,
                width=16,
                height=16,
                rgb=noncontig_arr,
                digest=hashlib.sha256(np.ascontiguousarray(noncontig_arr).tobytes()).hexdigest(),
            ),
        )
        bad_digest_obs = RawObservationEnvelope(
            handle=Handle("cdp_tab", "bad_dig", 1),
            epoch=1,
            frame_seq=1,
            dpr_ppm=1000000,
            state={"nodes": []},
            frame=RawFrameEnvelope(
                handle=Handle("cdp_tab", "bad_dig", 1),
                epoch=1,
                frame_seq=1,
                width=16,
                height=16,
                rgb=make_rgb_array(16, 16, fill=9),
                digest="0" * 64,
            ),
        )
        out_of_order_obs = make_strict_observation("ok_win", seq=1, fill=255)

        batch_items = [valid_obs, wrong_dpr_obs, noncontig_obs, bad_digest_obs, out_of_order_obs]
        batch_results = perceive_many(
            batch_items,
            adapter=FakeVisionEngine(),
            runner=OrderControllingRunner(),
        )
        self.assertIsNone(batch_results[0].error)
        self.assertTrue(all(r.error.startswith("VALIDATION_ERROR:") for r in batch_results[1:]))
        self.assertTrue(all(r.observation is None for r in batch_results[1:]))

        report = audit_multiwin_perception(batch_items, batch_results)
        self.assertFalse(report.authentic)
        self.assertEqual(report.authentic_count, 0)

    def test_sibling_success_never_corroborates_failed_or_unverified_target(self):
        """A valid sibling window never corroborates a failed, missing, or unperceived sibling."""
        obs_a = make_strict_observation("win_a", seq=1, fill=15, dpr_ppm=1000000)
        obs_b = make_strict_observation("win_b", seq=1, fill=15, dpr_ppm=1000000)
        # Runner fails after delivering only the first group (win_a)
        runner = OrderControllingRunner(permutation=[0, 1], fail_after=1)
        results, report = perceive_and_audit(
            [obs_a, obs_b],
            adapter=FakeVisionEngine(),
            runner=runner,
        )
        self.assertFalse(report.authentic)
        self.assertEqual(report.verdict, VERDICT_REJECTED)
        self.assertEqual(report.authentic_count, 1)
        self.assertEqual(report.rejected_count, 1)

        diag_a = report.for_target("win_a")
        diag_b = report.for_target("win_b")
        self.assertTrue(diag_a.authentic)
        self.assertFalse(diag_b.authentic)
        self.assertIn("result_error_receipt", diag_b.missing)
        self.assertEqual(diag_b.categories, (DIAG_MISSING,))

    def test_structural_validity_is_never_treated_as_authenticity(self):
        """Structurally valid Observation and PerceptionResult without _perception or readonly rgb are rejected."""
        obs = make_strict_observation("win_struct", seq=1, fill=50)
        # 1. Raw Observation passed directly without running perceive_many
        report_raw = audit_multiwin_perception([obs])
        self.assertFalse(report_raw.authentic)
        diag_raw = report_raw.for_target("win_struct")
        self.assertFalse(diag_raw.authentic)
        self.assertIn("missing_perception_metadata", diag_raw.missing)

        # 2. PerceptionResult wrapping raw Observation (error=None, valid dataclasses, no _perception)
        unperceived_res = PerceptionResult(obs.handle, obs, None)
        report_wrapped = audit_multiwin_perception([obs], [unperceived_res])
        self.assertFalse(report_wrapped.authentic)
        diag_wrapped = report_wrapped.for_target("win_struct")
        self.assertFalse(diag_wrapped.authentic)
        self.assertIn("missing_perception_metadata", diag_wrapped.missing)

        # 3. Perceived result whose frame.rgb buffer is mutable (writeable=True)
        real_res = perceive_many([obs], adapter=FakeVisionEngine())[0]
        mutable_rgb = np.array(real_res.observation.frame.rgb, dtype=np.uint8, copy=True)
        mutable_rgb.flags.writeable = True
        mutable_frame = RawFrameEnvelope(
            handle=real_res.observation.handle,
            epoch=real_res.observation.epoch,
            frame_seq=real_res.observation.frame_seq,
            width=real_res.observation.frame.width,
            height=real_res.observation.frame.height,
            rgb=mutable_rgb,
            digest=real_res.observation.frame.digest,
        )
        mutable_obs = RawObservationEnvelope(
            handle=real_res.observation.handle,
            epoch=real_res.observation.epoch,
            frame_seq=real_res.observation.frame_seq,
            dpr_ppm=real_res.observation.dpr_ppm,
            state=real_res.observation.state,
            frame=mutable_frame,
        )
        mutable_res = PerceptionResult(obs.handle, None, None)
        object.__setattr__(mutable_res, "observation", mutable_obs)
        report_mut = audit_multiwin_perception([obs], [mutable_res])
        self.assertFalse(report_mut.authentic)
        diag_mut = report_mut.for_target("win_struct")
        self.assertFalse(diag_mut.authentic)
        self.assertIn("mutable_frame_buffer", diag_mut.disagreeing)

    def test_stale_evidence_diagnostics_across_generation_epoch_and_sequence(self):
        """Detect stale handle generation, tombstoned handle, stale epoch, and regressed sequence."""
        reg = SessionRegistry()
        h_v1 = reg.register("cdp_tab", "tab_main", object(), owned=True)
        reg.close(h_v1)
        reg.acknowledge_cleanup(h_v1)
        h_v2 = reg.register("cdp_tab", "tab_main", object(), owned=True)
        reg.advance_epoch(h_v2)  # active epoch is now 1
        reg.advance_epoch(h_v2)  # active epoch is now 2
        rgb_latest = make_rgb_array(16, 16, fill=90)
        reg.admit_frame(h_v2, 5, rgb_latest, epoch=2)

        # Target 1: tombstoned generation 1
        obs_stale_gen = make_strict_observation(
            "tab_main", seq=1, fill=10, generation=h_v1.generation, epoch=0
        )
        # Target 2: active generation 2, but stale epoch 1 (< active epoch 2) and stale seq 3 (< 5)
        obs_stale_ep_seq = make_strict_observation(
            "tab_main", seq=3, fill=90, generation=h_v2.generation, epoch=1
        )
        results = perceive_many([obs_stale_gen, obs_stale_ep_seq], adapter=FakeVisionEngine())
        report = audit_multiwin_perception(
            [obs_stale_gen, obs_stale_ep_seq],
            results,
            registry=reg,
            expected_sequences={h_v2: 5},
        )
        self.assertFalse(report.authentic)

        diag_v1 = report.for_target(h_v1)
        self.assertFalse(diag_v1.authentic)
        self.assertIn("stale_tombstoned_handle", diag_v1.stale)
        self.assertIn("stale_generation", diag_v1.stale)

        diag_v2 = report.for_target(h_v2)
        self.assertFalse(diag_v2.authentic)
        self.assertIn("stale_epoch", diag_v2.stale)
        self.assertIn("stale_sequence", diag_v2.stale)

    def test_foreign_evidence_diagnostics_across_handles_nodes_and_temporal_bleed(self):
        """Detect foreign handle swap, foreign AX node bleed, foreign perception handle, and temporal bleed."""
        obs_a = make_strict_observation("win_a", seq=1, fill=10, node_id="000000000000000a")
        obs_b = make_strict_observation("win_b", seq=1, fill=240, node_id="000000000000000b")
        res_a, res_b = perceive_many([obs_a, obs_b], adapter=FakeVisionEngine())

        # Poison res_b with foreign AX node from win_a, foreign _perception.handle, and non-empty initial regions
        poisoned_state = copy.deepcopy(res_b.observation.state)
        poisoned_state["nodes"][0]["handle"] = {
            "kind": obs_a.handle.kind,
            "canonical_id": obs_a.handle.canonical_id,
            "generation": obs_a.handle.generation,
        }
        poisoned_state["_perception"]["handle"] = {
            "kind": obs_a.handle.kind,
            "canonical_id": obs_a.handle.canonical_id,
            "generation": obs_a.handle.generation,
        }
        poisoned_state["_perception"]["regions"] = [{"x": 0, "y": 0, "w": 16, "h": 16, "tiles": 1}]
        poisoned_obs_b = Observation(
            obs_b.handle,
            obs_b.epoch,
            obs_b.frame_seq,
            obs_b.dpr_ppm,
            poisoned_state,
            res_b.observation.frame,
        )
        poisoned_res_b = PerceptionResult(obs_b.handle, poisoned_obs_b, None)

        report = audit_multiwin_perception(
            [obs_a, obs_b],
            [res_a, poisoned_res_b],
            expected_targets=[obs_a.handle, obs_b.handle],
        )
        self.assertFalse(report.authentic)
        diag_a = report.for_target(obs_a.handle)
        diag_b = report.for_target(obs_b.handle)
        self.assertTrue(diag_a.authentic)
        self.assertFalse(diag_b.authentic)
        self.assertIn("foreign_ax_node", diag_b.foreign)
        self.assertIn("foreign_perception_handle", diag_b.foreign)
        self.assertIn("foreign_temporal_predecessor", diag_b.foreign)
        self.assertIn("regions_mismatch", diag_b.disagreeing)

    def test_disagreeing_evidence_dpr_digest_and_fusion_misalignment(self):
        """Detect DPR disagreement, forged fusion hits, and frame content SHA-256 digest mismatch."""
        obs_a = make_strict_observation("win_dpr_a", seq=1, fill=20, dpr_ppm=1000000, box=(0, 0, 16, 16))
        obs_b = make_strict_observation("win_dpr_b", seq=1, fill=20, dpr_ppm=2000000, box=(0, 0, 8, 8))
        obs_b2 = make_strict_observation("win_dpr_b", seq=2, fill=220, dpr_ppm=2000000, box=(0, 0, 8, 8))
        res_a, res_b1, res_b2 = perceive_many([obs_a, obs_b, obs_b2], adapter=FakeVisionEngine())

        # Tamper res_b2: replace _perception.fusion with fusion computed at wrong DPR (1.0x instead of 2.0x)
        # and tamper _perception.dpr_ppm
        bad_state = copy.deepcopy(res_b2.observation.state)
        bad_state["_perception"]["dpr_ppm"] = 1000000
        bad_state["_perception"]["fusion"] = {
            "hits": [{"region": 0, "node": "0000000000000001", "overlap": 256, "actionable": True}],
            "unmatched_regions": [],
        }
        bad_obs_b2 = Observation(
            obs_b2.handle,
            obs_b2.epoch,
            obs_b2.frame_seq,
            obs_b2.dpr_ppm,
            bad_state,
            res_b2.observation.frame,
        )
        bad_res_b2 = PerceptionResult(obs_b2.handle, bad_obs_b2, None)

        auditor = MultiWindowPerceptionAuditor(
            adapter=FakeVisionEngine(),
            expected_dpr_ppm={"win_dpr_a": 1000000, "win_dpr_b": 2000000},
        )
        report = auditor.audit([obs_a, obs_b, obs_b2], [res_a, res_b1, bad_res_b2])
        self.assertFalse(report.authentic)
        self.assertTrue(report.for_target("win_dpr_a").authentic)
        diag_b = report.for_target("win_dpr_b")
        self.assertFalse(diag_b.authentic)
        self.assertIn("perception_dpr_mismatch", diag_b.disagreeing)
        self.assertIn("fusion_mismatch", diag_b.disagreeing)

    def test_lazy_collaborator_blocks_on_absence(self):
        """Unmerged future sibling modules block on lazy import rather than fabricating APIs."""
        mod = require_collaborator("wender.perceive.vision_adapter")
        self.assertIs(mod, va_mod)
        fn = require_collaborator("wender.perceive.parallel_perceive", "perceive_many")
        self.assertIs(fn, perceive_many)

        with self.assertRaises(AuditConfigError) as ctx_mod:
            require_collaborator("wender.perceive.ausente_future_sibling_deliverable")
        self.assertIn("AUSENTE", str(ctx_mod.exception))

        with self.assertRaises(AuditConfigError) as ctx_sym:
            require_collaborator("wender.perceive.parallel_perceive", "describe_windows")
        self.assertIn("AUSENTE", str(ctx_sym.exception))

    def test_no_disk_io_during_perceive_and_audit(self):
        """Verify frames remain strictly in RAM and open() is never invoked."""
        items = [
            make_strict_observation("ram_a", seq=1, fill=12),
            make_strict_observation("ram_b", seq=1, fill=80),
            make_strict_observation("ram_a", seq=2, fill=240),
        ]
        with patch.object(builtins, "open", side_effect=AssertionError("disk I/O forbidden")):
            _, report = perceive_and_audit(
                items,
                adapter=FingerprintVisionEngine(),
                runner=OrderControllingRunner(),
            )
        self.assertTrue(report.authentic)


class TestSeededPropertyLoopsAndShrinking(unittest.TestCase):
    """Seeded stdlib random property loops (64 valid cases per domain, <=32 shrink steps)."""

    def test_domain_1_parallel_perceive_64_valid_cases(self):
        """64 seeded multi-window batches verify order preservation, isolation, and RAM immutability."""
        kinds = sorted(_TARGET_KINDS)

        def gen_batch(rng: random.Random, case_idx: int):
            n_windows = rng.randint(2, 4)
            specs = []
            for w_idx in range(n_windows):
                kind = kinds[(case_idx + w_idx) % len(kinds)]
                cid = f"win_{w_idx}_{rng.randint(1, 9)}"
                gen = rng.randint(1, 4)
                epoch = rng.randint(0, 6)
                dpr = rng.choice((1000000, 1500000, 2000000))
                n_frames = rng.randint(1, 3)
                seq_start = rng.randint(0, 5)
                fills = [rng.randint(0, 250) for _ in range(n_frames)]
                specs.append((kind, cid, gen, epoch, dpr, seq_start, fills))
            return specs

        def is_valid_batch(specs) -> bool:
            if not specs:
                return False
            seen_dest = set()
            for kind, cid, gen, epoch, dpr, seq_start, fills in specs:
                if kind not in _TARGET_KINDS or not cid or gen < 1 or epoch < 0 or dpr <= 0 or seq_start < 0:
                    return False
                if not fills or (kind, cid) in seen_dest:
                    return False
                seen_dest.add((kind, cid))
            return True

        def shrink_batch(specs):
            if len(specs) > 1:
                for i in range(len(specs)):
                    yield specs[:i] + specs[i + 1:]
            for i, (kind, cid, gen, epoch, dpr, seq_start, fills) in enumerate(specs):
                if len(fills) > 1:
                    updated = list(specs)
                    updated[i] = (kind, cid, 1, 0, 1000000, 0, fills[:1])
                    yield updated

        def check_property(specs):
            observations = []
            for kind, cid, gen, epoch, dpr, seq_start, fills in specs:
                for offset, fill_val in enumerate(fills):
                    observations.append(
                        make_strict_observation(
                            cid,
                            seq=seq_start + offset,
                            fill=fill_val,
                            kind=kind,
                            generation=gen,
                            epoch=epoch,
                            dpr_ppm=dpr,
                            box=(0, 0, 8, 8),
                        )
                    )
            res_rev = perceive_many(
                observations,
                adapter=FingerprintVisionEngine(),
                runner=OrderControllingRunner(),
            )
            self.assertEqual(len(res_rev), len(observations))
            for obs, res in zip(observations, res_rev):
                self.assertIsNone(res.error)
                self.assertEqual(res.handle, obs.handle)
                self.assertFalse(res.observation.frame.rgb.flags.writeable)
                self.assertEqual(res.observation.frame.digest, obs.frame.digest)

            report = audit_multiwin_perception(
                observations,
                res_rev,
                adapter=FingerprintVisionEngine(),
            )
            self.assertTrue(report.authentic)

        summary = run_seeded_property_domain(
            "parallel_perceive_multiwin_batch",
            seed=2026102001,
            generator_fn=gen_batch,
            is_valid_fn=is_valid_batch,
            property_fn=check_property,
            shrink_candidates_fn=shrink_batch,
            target_valid_cases=64,
            max_shrink_steps=32,
        )
        self.assertEqual(summary.valid_cases, 64)

    def test_domain_2_vision_adapter_fusion_64_valid_cases(self):
        """64 seeded cases for FakeVisionEngine and target-bound fuse_regions DPR/IoU invariants."""
        kinds = sorted(_TARGET_KINDS)

        def gen_fusion_case(rng: random.Random, case_idx: int):
            kind = kinds[case_idx % len(kinds)]
            handle = Handle(kind, f"target_{case_idx % 8}", rng.randint(1, 5))
            epoch = rng.randint(0, 10)
            dpr_ppm = rng.choice((1000000, 1250000, 1500000, 2000000))
            rx = rng.randint(0, 32)
            ry = rng.randint(0, 32)
            rw = rng.randint(8, 32)
            rh = rng.randint(8, 32)
            nx = (rx * 1000000) // dpr_ppm
            ny = (ry * 1000000) // dpr_ppm
            nw = max(4, ((rx + rw) * 1000000) // dpr_ppm - nx)
            nh = max(4, ((ry + rh) * 1000000) // dpr_ppm - ny)
            return (handle, epoch, dpr_ppm, (rx, ry, rw, rh), (nx, ny, nw, nh))

        def is_valid_fusion_case(case) -> bool:
            handle, epoch, dpr_ppm, reg, box = case
            return (
                handle.kind in _TARGET_KINDS
                and handle.generation >= 1
                and epoch >= 0
                and dpr_ppm > 0
                and reg[2] > 0
                and reg[3] > 0
                and box[2] > 0
                and box[3] > 0
            )

        def shrink_fusion_case(case):
            handle, epoch, dpr_ppm, (rx, ry, rw, rh), (nx, ny, nw, nh) = case
            if rx > 0 or ry > 0 or epoch > 0:
                yield (Handle(handle.kind, "t", 1), 0, 1000000, (0, 0, 8, 8), (0, 0, 8, 8))

        def check_fusion_property(case):
            handle, epoch, dpr_ppm, (rx, ry, rw, rh), (nx, ny, nw, nh) = case
            state = make_valid_state(
                node_id="0000000000000001",
                box=(nx, ny, nw, nh),
                dpr_ppm=dpr_ppm,
            )
            envelope = {
                "state": state,
                "handle": handle,
                "epoch": epoch,
                "dpr_ppm": dpr_ppm,
            }
            regions = [{"x": rx, "y": ry, "w": rw, "h": rh, "tiles": 1}]
            fused = fuse_regions(
                envelope,
                regions,
                dpr_ppm=dpr_ppm,
                iou_num=1,
                iou_den=2,
                handle=handle,
                epoch=epoch,
            )
            self.assertEqual(len(fused["hits"]), 1)
            self.assertEqual(fused["hits"][0]["node"], "0000000000000001")
            self.assertEqual(fused["unmatched_regions"], [])

            # Sibling handle or wrong epoch must fail closed
            sibling = Handle(handle.kind, handle.canonical_id + "_sib", handle.generation)
            with self.assertRaises(ValueError):
                fuse_regions(envelope, regions, dpr_ppm=dpr_ppm, handle=sibling, epoch=epoch)
            with self.assertRaises(ValueError):
                fuse_regions(envelope, regions, dpr_ppm=dpr_ppm, handle=handle, epoch=epoch + 1)

        summary = run_seeded_property_domain(
            "vision_adapter_target_bound_fusion",
            seed=2026102002,
            generator_fn=gen_fusion_case,
            is_valid_fn=is_valid_fusion_case,
            property_fn=check_fusion_property,
            shrink_candidates_fn=shrink_fusion_case,
            target_valid_cases=64,
            max_shrink_steps=32,
        )
        self.assertEqual(summary.valid_cases, 64)

    def test_domain_3_multiwin_auditor_fault_classification_64_valid_cases(self):
        """64 seeded fault-injection cases verify deterministic per-target diagnostics."""
        fault_kinds = ("none", "stale_epoch", "foreign_handle", "missing_meta", "bad_dpr")

        def gen_audit_case(rng: random.Random, case_idx: int):
            fault = fault_kinds[case_idx % len(fault_kinds)]
            epoch_a = rng.randint(1, 6)
            epoch_b = rng.randint(1, 6)
            dpr_a = rng.choice((1000000, 2000000))
            dpr_b = rng.choice((1000000, 2000000))
            fill_a = rng.randint(0, 200)
            fill_b = rng.randint(0, 200)
            return (fault, epoch_a, epoch_b, dpr_a, dpr_b, fill_a, fill_b)

        def is_valid_audit_case(case) -> bool:
            fault, epoch_a, epoch_b, dpr_a, dpr_b, fill_a, fill_b = case
            return (
                fault in fault_kinds
                and epoch_a >= 1
                and epoch_b >= 1
                and dpr_a > 0
                and dpr_b > 0
                and 0 <= fill_a <= 255
                and 0 <= fill_b <= 255
            )

        def shrink_audit_case(case):
            fault, epoch_a, epoch_b, dpr_a, dpr_b, fill_a, fill_b = case
            if epoch_a > 1 or epoch_b > 1 or fill_a > 0 or fill_b > 0:
                yield (fault, 1, 1, 1000000, 1000000, 0, 0)

        def check_audit_property(case):
            fault, epoch_a, epoch_b, dpr_a, dpr_b, fill_a, fill_b = case
            obs_a = make_strict_observation("win_a", seq=1, fill=fill_a, epoch=epoch_a, dpr_ppm=dpr_a)
            obs_b = make_strict_observation("win_b", seq=1, fill=fill_b, epoch=epoch_b, dpr_ppm=dpr_b)
            res_a, res_b = perceive_many([obs_a, obs_b], adapter=FakeVisionEngine())
            exp_epochs = {"win_a": epoch_a, "win_b": epoch_b}

            if fault == "stale_epoch":
                exp_epochs["win_b"] = epoch_b + 1
            elif fault == "foreign_handle":
                st = copy.deepcopy(res_b.observation.state)
                st["_perception"]["handle"] = {
                    "kind": obs_a.handle.kind,
                    "canonical_id": obs_a.handle.canonical_id,
                    "generation": obs_a.handle.generation,
                }
                res_b = PerceptionResult(
                    obs_b.handle,
                    Observation(obs_b.handle, obs_b.epoch, obs_b.frame_seq, obs_b.dpr_ppm, st, res_b.observation.frame),
                    None,
                )
            elif fault == "missing_meta":
                res_b = PerceptionResult(obs_b.handle, obs_b, None)
            elif fault == "bad_dpr":
                st = copy.deepcopy(res_b.observation.state)
                st["_perception"]["dpr_ppm"] = dpr_b + 500000
                res_b = PerceptionResult(
                    obs_b.handle,
                    Observation(obs_b.handle, obs_b.epoch, obs_b.frame_seq, obs_b.dpr_ppm, st, res_b.observation.frame),
                    None,
                )

            report = audit_multiwin_perception(
                [obs_a, obs_b],
                [res_a, res_b],
                expected_epochs=exp_epochs,
            )
            diag_a = report.for_target("win_a")
            diag_b = report.for_target("win_b")
            self.assertTrue(diag_a.authentic)

            if fault == "none":
                self.assertTrue(diag_b.authentic)
                self.assertTrue(report.authentic)
            else:
                # Sibling win_a success never corroborates faulted win_b
                self.assertFalse(diag_b.authentic)
                self.assertFalse(report.authentic)
                if fault == "stale_epoch":
                    self.assertIn(DIAG_STALE, diag_b.categories)
                elif fault == "foreign_handle":
                    self.assertIn(DIAG_FOREIGN, diag_b.categories)
                elif fault == "missing_meta":
                    self.assertIn(DIAG_MISSING, diag_b.categories)
                elif fault == "bad_dpr":
                    self.assertIn(DIAG_DISAGREEING, diag_b.categories)

        summary = run_seeded_property_domain(
            "multiwin_auditor_fault_classification",
            seed=2026102003,
            generator_fn=gen_audit_case,
            is_valid_fn=is_valid_audit_case,
            property_fn=check_audit_property,
            shrink_candidates_fn=shrink_audit_case,
            target_valid_cases=64,
            max_shrink_steps=32,
        )
        self.assertEqual(summary.valid_cases, 64)

    def test_validity_preserving_shrinker_reports_seed_case_and_minimal_fixture(self):
        """Verify shrinker preserves validity, respects <=32 shrink budget, and reports minimal fixture."""
        def gen_int_list(rng: random.Random, idx: int):
            return [rng.randint(10, 50) for _ in range(rng.randint(3, 6))]

        def is_valid_nonempty_pos(items) -> bool:
            return isinstance(items, list) and len(items) >= 1 and all(x >= 5 for x in items)

        def shrink_list(items):
            if len(items) > 1:
                yield items[:1]
            if items[0] > 5:
                yield [max(5, items[0] // 2)]

        with self.assertRaises(AssertionError) as ctx:
            run_seeded_property_domain(
                "shrinker_verification_domain",
                seed=77,
                generator_fn=gen_int_list,
                is_valid_fn=is_valid_nonempty_pos,
                property_fn=lambda items: self.assertLess(sum(items), 5),
                shrink_candidates_fn=shrink_list,
                target_valid_cases=64,
                max_shrink_steps=32,
            )
        msg = str(ctx.exception)
        self.assertIn("seed=77", msg)
        self.assertIn("case=1", msg)
        self.assertIn("minimal_fixture=[5]", msg)


class TestBoundedConcurrencySchedules(unittest.TestCase):
    """32 replayable multi-thread schedules (<=4 threads, Barriers, Events, 2s bounded joins)."""

    def test_32_replayable_concurrent_multiwindow_schedules_no_leaked_threads(self):
        """Run 32 deterministic schedules across 4 worker threads with barriers and controlled Events."""
        baseline_threads = {t.ident for t in threading.enumerate()}
        adapter = FingerprintVisionEngine(tile=16, threshold=8)

        for schedule_idx in range(32):
            rng = random.Random(9000 + schedule_idx)
            reg = SessionRegistry()
            handles = [
                reg.register("cdp_tab", f"conc_win_{i}", object(), owned=True)
                for i in range(4)
            ]
            # Advance epochs deterministically according to schedule
            expected_epochs = {}
            expected_dprs = {}
            for w_idx, h in enumerate(handles):
                steps = (schedule_idx + w_idx) % 3
                ep = 0
                for _ in range(steps):
                    ep = reg.advance_epoch(h)
                expected_epochs[h] = ep
                expected_dprs[h] = 1000000 if (w_idx % 2 == 0) else 2000000

            start_barrier = threading.Barrier(4, timeout=2.0)
            release_events = [threading.Event() for _ in range(4)]
            delivery_order = [0, 1, 2, 3]
            rng.shuffle(delivery_order)

            admitted_obs = [None] * 4
            worker_errors = []
            lock = threading.Lock()

            def worker_task(slot: int):
                try:
                    start_barrier.wait(timeout=2.0)
                    if not release_events[slot].wait(timeout=2.0):
                        raise TimeoutError(f"worker {slot} event wait timed out")
                    h = handles[slot]
                    ep = expected_epochs[h]
                    dpr = expected_dprs[h]
                    rgb = make_rgb_array(16, 16, fill=(slot + 1) * 25 + schedule_idx)
                    fref = reg.admit_frame(h, schedule_idx + 1, rgb, epoch=ep)
                    state = make_valid_state(
                        node_id=f"000000000000000{slot + 1}",
                        box=(0, 0, 8, 8),
                        dpr_ppm=dpr,
                        handle=h,
                        epoch=ep,
                    )
                    obs = reg.admit_observation(h, fref, state=state, dpr_ppm=dpr)
                    with lock:
                        admitted_obs[slot] = obs
                except BaseException as exc:
                    with lock:
                        worker_errors.append(exc)

            threads = [
                threading.Thread(
                    target=worker_task,
                    args=(slot,),
                    name=f"sched-{schedule_idx}-w-{slot}",
                    daemon=True,
                )
                for slot in range(4)
            ]
            self.assertLessEqual(len(threads), 4)
            for t in threads:
                t.start()

            # Release workers in the replayable schedule's permutation order
            for slot in delivery_order:
                release_events[slot].set()

            for t in threads:
                t.join(timeout=2.0)
                self.assertFalse(t.is_alive(), f"leaked thread {t.name} in schedule {schedule_idx}")

            self.assertEqual(worker_errors, [])
            self.assertTrue(all(o is not None for o in admitted_obs))

            # Execute perceive_many with controlled runner delivery order and audit against SessionRegistry
            runner = OrderControllingRunner(permutation=delivery_order)
            results, report = perceive_and_audit(
                admitted_obs,
                adapter=adapter,
                runner=runner,
                registry=reg,
                expected_targets=handles,
                expected_epochs=expected_epochs,
                expected_dpr_ppm=expected_dprs,
            )
            self.assertTrue(report.authentic, f"schedule {schedule_idx} failed audit: {report.to_dict()}")
            self.assertEqual(report.authentic_count, 4)

        current_threads = {
            t.ident
            for t in threading.enumerate()
            if not t.name.startswith("perceive_")
        }
        leaked = current_threads - baseline_threads
        self.assertEqual(leaked, set(), f"leaked threads detected: {leaked}")


if __name__ == "__main__":
    unittest.main()
