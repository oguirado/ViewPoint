"""Tests for jq3-r-r4-g1-wender-pct-interleaving-explorer.

Includes:
- Embedded in-memory AST mutation prove harness with exact anchor/selector matching
  and separate dispositions (KILLED, SURVIVED, EQUIVALENT, FAILED_CONTROL,
  MISSING_ANCHOR, AMBIGUOUS_ANCHOR, COMPILE_IMPORT_ERROR, TIMEOUT, RUNTIME_ERROR).
- Catalogue mutation obligations M-wender-act-target-lock-registry-1 and
  M-wender-act-target-lock-registry-2, plus operator coverage for O, B, D, and S.
- Deterministic Kahn and Priority-Concurrency (PCT) explorer validation, bounded
  schedule enumeration, controlled yield hooks, and multi-threaded replay (<= 4
  threads, <= 32 schedules, 2.0s bounded joins, zero leaked threads).
- Real TargetLockRegistry, SessionRegistry, and perceive_many integration tests
  with corrected strict fixtures.
- Seeded stdlib random property loops (64 valid cases per domain) and bounded
  validity-preserving shrinking (<= 32 attempts).
"""

from __future__ import annotations

import ast
import copy
from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import random
import re
import secrets
import sys
import threading
import time
import types
from typing import Any, Callable, Sequence
import unittest
from unittest.mock import patch

import numpy as np

from wender.act.target_lock_registry import (
    ActorLease,
    PrerequisiteAbsent,
    TargetBusy,
    TargetLockRefused,
    TargetLockRegistry,
)
from wender.learn.interleaving_explorer import (
    ControlledOrderRunner,
    ControlledYieldBarrier,
    CyclicDependencyError,
    DEFAULT_JOIN_TIMEOUT_S,
    DeterministicVisionAdapter,
    DuplicateStepError,
    ExplorerStep,
    InlineSessionRegistryCollaborator,
    InterleavingExplorerError,
    MAX_BOUNDED_SCHEDULES,
    MAX_EXPLORER_THREADS,
    MissingPredecessorError,
    PendingDependencyAbsentError,
    ScheduleReplayError,
    ScheduleTrace,
    StepExecutionContext,
    TraceEvent,
    build_valid_observation,
    enumerate_kahn_schedules,
    explore_pct_schedules,
    explore_perceive_many_interleavings,
    explore_target_lock_interleavings,
    kahn_topological_schedule,
    pct_schedule,
    replay_schedule,
    require_merged_dependency,
    validate_step_dag,
)
from wender.perceive.parallel_perceive import ObservationError, _describe_group, _prepare, perceive_many
from wender.perceive.session_registry import (
    FrameRef,
    FrameValidationError,
    Handle,
    Observation,
    PerceptionResult,
    SessionRegistry,
    StaleHandleError,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


# ============================================================================
# Embedded In-Memory AST Mutation Prove Harness (No Shared Test Harness)
# ============================================================================

DISPOSITION_KILLED = "KILLED"
DISPOSITION_SURVIVED = "SURVIVED"
DISPOSITION_EQUIVALENT = "EQUIVALENT"
DISPOSITION_FAILED_CONTROL = "FAILED_CONTROL"
DISPOSITION_MISSING_ANCHOR = "MISSING_ANCHOR"
DISPOSITION_AMBIGUOUS_ANCHOR = "AMBIGUOUS_ANCHOR"
DISPOSITION_COMPILE_IMPORT_ERROR = "COMPILE_IMPORT_ERROR"
DISPOSITION_TIMEOUT = "TIMEOUT"
DISPOSITION_RUNTIME_ERROR = "RUNTIME_ERROR"


@dataclass(frozen=True)
class MutationObligation:
    """Catalogue or unit mutation specification with exact source anchor and selector."""

    mutation_id: str
    operator: str  # "O", "B", "D", "S"
    file_path: str
    lineno: int
    qualified_function: str
    node: str
    selector: str = ""
    structural_path: str = ""
    expected_source_line: str | None = None


@dataclass(frozen=True)
class MutationProofResult:
    """Outcome of running positive control + in-memory AST mutation proof."""

    mutation_id: str
    disposition: str
    killed: bool
    detail: str
    elapsed_vm_ms: int


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


def _normalize_node_description(text: str) -> str:
    """Normalize verbalized catalogue node descriptions to canonical ast.unparse strings."""
    s = text.strip()
    replacements = (
        (" is less than or equal to ", " <= "),
        (" is greater than or equal to ", " >= "),
        (" is less than ", " < "),
        (" is greater than ", " > "),
        (" is not ", " is not "),
        (" equals ", " == "),
        (" does not equal ", " != "),
    )
    for verbal, symbolic in replacements:
        if verbal in s and symbolic.strip() not in s:
            s = s.replace(verbal, symbolic)
    return re.sub(r"\s+", " ", s)


def _parse_selector_steps(selector_str: str) -> list[tuple[str, int | None]]:
    """Parse structural selector like 'body element 9.body element 2' or 'ops[0]'."""
    if not selector_str or not selector_str.strip():
        return []
    steps: list[tuple[str, int | None]] = []
    for part in selector_str.strip().split("."):
        part = part.strip()
        if not part:
            continue
        m_elem = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s+element\s+(\d+)", part)
        if m_elem:
            steps.append((m_elem.group(1), int(m_elem.group(2))))
            continue
        m_idx = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\[(\d+)\]", part)
        if m_idx:
            steps.append((m_idx.group(1), int(m_idx.group(2))))
            continue
        m_attr = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)", part)
        if m_attr:
            steps.append((m_attr.group(1), None))
            continue
        raise ValueError(f"invalid selector component: {part!r}")
    return steps


def _resolve_qualified_function(tree: ast.Module, qualified_name: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    parts = [p for p in qualified_name.split(".") if p]
    if not parts:
        return []

    def _search(body: Sequence[ast.stmt], remaining: Sequence[str]) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
        head = remaining[0]
        tail = remaining[1:]
        matches: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        for stmt in body:
            if not tail and isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == head:
                matches.append(stmt)
            elif tail and isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == head:
                matches.extend(_search(stmt.body, tail))
        return matches

    return _search(tree.body, parts)


def _navigate_structural_path(root: ast.AST, steps: Sequence[tuple[str, int | None]]) -> Any:
    current: Any = root
    for attr, idx in steps:
        if not hasattr(current, attr):
            raise KeyError(f"node {type(current).__name__} lacks attribute {attr!r}")
        current = getattr(current, attr)
        if idx is not None:
            if not isinstance(current, (list, tuple)) or idx < 0 or idx >= len(current):
                raise IndexError(f"attribute {attr!r} index {idx} out of range")
            current = current[idx]
    return current


def _apply_ast_mutation(
    tree: ast.Module,
    obligation: MutationObligation,
    source_lines: Sequence[str],
) -> tuple[str, str]:
    """Locate the unique target AST node inside tree and mutate it in place."""
    if obligation.lineno < 1 or obligation.lineno > len(source_lines):
        return DISPOSITION_MISSING_ANCHOR, f"line {obligation.lineno} out of range (1..{len(source_lines)})"

    if obligation.expected_source_line is not None:
        actual_line = source_lines[obligation.lineno - 1]
        if actual_line != obligation.expected_source_line:
            return (
                DISPOSITION_MISSING_ANCHOR,
                f"source line mismatch at {obligation.lineno}: {actual_line!r} != {obligation.expected_source_line!r}",
            )

    func_matches = _resolve_qualified_function(tree, obligation.qualified_function)
    if not func_matches:
        return DISPOSITION_MISSING_ANCHOR, f"qualified function {obligation.qualified_function!r} not found"
    if len(func_matches) > 1:
        return DISPOSITION_AMBIGUOUS_ANCHOR, f"qualified function {obligation.qualified_function!r} is ambiguous"
    func_node = func_matches[0]

    want_norm = _normalize_node_description(obligation.node)
    target_node: Any = None

    if obligation.structural_path:
        try:
            path_steps = _parse_selector_steps(obligation.structural_path)
            target_node = _navigate_structural_path(func_node, path_steps)
        except (ValueError, KeyError, IndexError) as exc:
            return DISPOSITION_MISSING_ANCHOR, f"structural_path resolution failed: {exc}"
        if not isinstance(target_node, ast.AST):
            return DISPOSITION_MISSING_ANCHOR, "structural_path did not resolve to an AST node"
        if getattr(target_node, "lineno", None) != obligation.lineno:
            return (
                DISPOSITION_MISSING_ANCHOR,
                f"structural_path node lineno {getattr(target_node, 'lineno', None)} != {obligation.lineno}",
            )
        unparsed_norm = _normalize_node_description(ast.unparse(target_node))
        if unparsed_norm != want_norm and obligation.node.strip() != ast.unparse(target_node).strip():
            return (
                DISPOSITION_MISSING_ANCHOR,
                f"structural_path node text {ast.unparse(target_node)!r} does not match {obligation.node!r}",
            )
    else:
        candidates: list[ast.AST] = []
        for sub in ast.walk(func_node):
            if isinstance(sub, ast.Expr):
                continue
            if getattr(sub, "lineno", None) != obligation.lineno:
                continue
            try:
                unparsed = ast.unparse(sub)
            except Exception:
                continue
            if _normalize_node_description(unparsed) == want_norm or unparsed.strip() == obligation.node.strip():
                candidates.append(sub)
        if not candidates:
            return (
                DISPOSITION_MISSING_ANCHOR,
                f"no AST node matching {obligation.node!r} at line {obligation.lineno} in {obligation.qualified_function}",
            )
        if len(candidates) > 1:
            return (
                DISPOSITION_AMBIGUOUS_ANCHOR,
                f"found {len(candidates)} ambiguous AST nodes matching {obligation.node!r} at line {obligation.lineno}",
            )
        target_node = candidates[0]

    selector_steps: list[tuple[str, int | None]] = []
    if obligation.selector:
        try:
            selector_steps = _parse_selector_steps(obligation.selector)
        except ValueError as exc:
            return DISPOSITION_MISSING_ANCHOR, str(exc)

    op = obligation.operator

    if op == "O":
        if isinstance(target_node, ast.Compare):
            op_idx = 0
            if selector_steps:
                if len(selector_steps) != 1 or selector_steps[0][0] != "ops" or selector_steps[0][1] is None:
                    return DISPOSITION_MISSING_ANCHOR, f"unsupported Compare selector {obligation.selector!r}"
                op_idx = selector_steps[0][1]
            elif len(target_node.ops) != 1:
                return DISPOSITION_AMBIGUOUS_ANCHOR, "Compare with multiple ops requires explicit ops selector"
            if op_idx < 0 or op_idx >= len(target_node.ops):
                return DISPOSITION_MISSING_ANCHOR, f"Compare ops index {op_idx} out of range"
            old_cls = type(target_node.ops[op_idx])
            new_cls = _COMPARE_O_MAP.get(old_cls)
            if new_cls is None:
                return DISPOSITION_MISSING_ANCHOR, f"unsupported Compare op for O: {old_cls.__name__}"
            target_node.ops[op_idx] = new_cls()
            return "OK", f"O Compare {old_cls.__name__} -> {new_cls.__name__}"

        if isinstance(target_node, ast.BoolOp):
            old_cls = type(target_node.op)
            new_cls = _BOOLOP_O_MAP.get(old_cls)
            if new_cls is None:
                return DISPOSITION_MISSING_ANCHOR, f"unsupported BoolOp for O: {old_cls.__name__}"
            target_node.op = new_cls()
            return "OK", f"O BoolOp {old_cls.__name__} -> {new_cls.__name__}"

        if isinstance(target_node, ast.BinOp):
            old_cls = type(target_node.op)
            new_cls = _BINOP_O_MAP.get(old_cls)
            if new_cls is None:
                return DISPOSITION_MISSING_ANCHOR, f"unsupported BinOp for O: {old_cls.__name__}"
            target_node.op = new_cls()
            return "OK", f"O BinOp {old_cls.__name__} -> {new_cls.__name__}"

        return DISPOSITION_MISSING_ANCHOR, f"operator O not applicable to {type(target_node).__name__}"

    if op == "B":
        # Strictly separate comparison boundary toggle from exact integer Constant increment.
        if isinstance(target_node, ast.Compare):
            op_idx = 0
            if selector_steps:
                if len(selector_steps) != 1 or selector_steps[0][0] != "ops" or selector_steps[0][1] is None:
                    return DISPOSITION_MISSING_ANCHOR, f"Compare B selector must target ops element, got {obligation.selector!r}"
                op_idx = selector_steps[0][1]
            elif len(target_node.ops) != 1:
                return DISPOSITION_AMBIGUOUS_ANCHOR, "Compare with multiple ops requires explicit ops selector"
            if op_idx < 0 or op_idx >= len(target_node.ops):
                return DISPOSITION_MISSING_ANCHOR, f"Compare ops index {op_idx} out of range"
            old_cls = type(target_node.ops[op_idx])
            new_cls = _COMPARE_B_MAP.get(old_cls)
            if new_cls is None:
                return DISPOSITION_MISSING_ANCHOR, f"Compare op {old_cls.__name__} cannot be boundary-toggled by B"
            target_node.ops[op_idx] = new_cls()
            return "OK", f"B Compare {old_cls.__name__} -> {new_cls.__name__}"

        if isinstance(target_node, ast.Constant) and type(target_node.value) is int:
            if selector_steps:
                return DISPOSITION_MISSING_ANCHOR, f"Constant B must not specify comparison selector {obligation.selector!r}"
            old_val = target_node.value
            target_node.value = old_val + 1
            return "OK", f"B Constant {old_val} -> {old_val + 1}"

        return (
            DISPOSITION_MISSING_ANCHOR,
            f"operator B requires Compare or exact int Constant, got {type(target_node).__name__}",
        )

    if op == "D":
        if isinstance(target_node, ast.If):
            has_raise = any(isinstance(n, ast.Raise) for n in ast.walk(ast.Module(body=target_node.body, type_ignores=[])))
            if not has_raise:
                return DISPOSITION_MISSING_ANCHOR, "D on If requires a raising body"
            target_node.test = ast.copy_location(ast.Constant(value=False), target_node.test)
            return "OK", "D If.test -> False"
        if isinstance(target_node, ast.Assert):
            target_node.test = ast.copy_location(ast.Constant(value=True), target_node.test)
            return "OK", "D Assert.test -> True"
        return DISPOSITION_MISSING_ANCHOR, f"operator D requires raising If or Assert, got {type(target_node).__name__}"

    if op == "S":
        if not isinstance(target_node, ast.Call):
            return DISPOSITION_MISSING_ANCHOR, f"operator S requires Call node, got {type(target_node).__name__}"
        non_starred = [i for i, arg in enumerate(target_node.args) if not isinstance(arg, ast.Starred)]
        if len(non_starred) < 2:
            return DISPOSITION_MISSING_ANCHOR, "Call has fewer than two non-starred positional arguments"
        i0, i1 = non_starred[0], non_starred[1]
        a0, a1 = target_node.args[i0], target_node.args[i1]
        if ast.unparse(a0) == ast.unparse(a1):
            return DISPOSITION_MISSING_ANCHOR, "first two non-starred positional Call arguments are identical"
        target_node.args[i0], target_node.args[i1] = a1, a0
        return "OK", f"S swapped Call args {ast.unparse(a0)!r} and {ast.unparse(a1)!r}"

    return DISPOSITION_MISSING_ANCHOR, f"unknown mutation operator {op!r}"


def _module_name_for_path(file_path: str) -> str:
    rel = Path(file_path)
    if rel.is_absolute():
        rel = rel.relative_to(REPO_ROOT)
    return ".".join(rel.with_suffix("").parts)


def prove_mutation(
    obligation: MutationObligation,
    check_fn: Callable[[types.ModuleType], None],
    *,
    equivalence_proof: Callable[[types.ModuleType, types.ModuleType], bool] | None = None,
    timeout_s: float = DEFAULT_JOIN_TIMEOUT_S,
    inject_syntax_error: bool = False,
) -> MutationProofResult:
    """Execute positive control on real module, then run check_fn against in-memory mutated AST."""
    start_ns = time.monotonic_ns()
    abs_path = REPO_ROOT / obligation.file_path
    if not abs_path.is_file():
        elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
        return MutationProofResult(
            obligation.mutation_id,
            DISPOSITION_MISSING_ANCHOR,
            False,
            f"source file not found: {obligation.file_path}",
            elapsed_ms,
        )

    module_name = _module_name_for_path(obligation.file_path)
    real_module = sys.modules.get(module_name)
    if real_module is None:
        real_module = importlib.import_module(module_name)

    # Step 1: Run the exact same assertion on the unmutated real module first.
    try:
        check_fn(real_module)
    except BaseException as exc:
        elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
        return MutationProofResult(
            obligation.mutation_id,
            DISPOSITION_FAILED_CONTROL,
            False,
            f"positive control failed on real module: {type(exc).__name__}: {exc}",
            elapsed_ms,
        )

    # Step 2: Parse and mutate AST in memory.
    source_text = abs_path.read_text(encoding="utf-8")
    source_lines = source_text.splitlines()
    tree = ast.parse(source_text, filename=str(abs_path))
    status, detail = _apply_ast_mutation(tree, obligation, source_lines)
    if status != "OK":
        elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
        return MutationProofResult(
            obligation.mutation_id,
            status,
            False,
            detail,
            elapsed_ms,
        )

    ast.fix_missing_locations(tree)

    # Step 3: Compile and exec inside an isolated module with sys.modules restored in finally.
    saved_modules = dict(sys.modules)
    saved_dont_write = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        try:
            if inject_syntax_error:
                code_obj = compile("def broken(:\n", filename=f"<mutant:{obligation.mutation_id}>", mode="exec")
            else:
                code_obj = compile(tree, filename=f"<mutant:{obligation.mutation_id}>", mode="exec")
            mutated_mod = types.ModuleType(module_name)
            mutated_mod.__file__ = getattr(real_module, "__file__", str(abs_path))
            mutated_mod.__package__ = getattr(real_module, "__package__", module_name.rpartition(".")[0])
            mutated_mod.__loader__ = getattr(real_module, "__loader__", None)
            mutated_mod.__spec__ = getattr(real_module, "__spec__", None)
            # Register in sys.modules BEFORE exec so @dataclass private-module lookup succeeds.
            sys.modules[module_name] = mutated_mod
            exec(code_obj, mutated_mod.__dict__)
        except BaseException as exc:
            elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
            return MutationProofResult(
                obligation.mutation_id,
                DISPOSITION_COMPILE_IMPORT_ERROR,
                False,
                f"compile/import failed: {type(exc).__name__}: {exc}",
                elapsed_ms,
            )

        outcome_box: list[tuple[str, str]] = []

        def _run_mutant() -> None:
            try:
                check_fn(mutated_mod)
                outcome_box.append(("PASSED", "mutated code satisfied check_fn without AssertionError"))
            except AssertionError as exc:
                outcome_box.append((DISPOSITION_KILLED, f"AssertionError: {exc}"))
            except BaseException as exc:
                outcome_box.append((DISPOSITION_RUNTIME_ERROR, f"{type(exc).__name__}: {exc}"))

        worker = threading.Thread(target=_run_mutant, name=f"mutant-{obligation.mutation_id}", daemon=True)
        worker.start()
        worker.join(timeout=timeout_s)
        if worker.is_alive():
            elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
            return MutationProofResult(
                obligation.mutation_id,
                DISPOSITION_TIMEOUT,
                False,
                f"mutant execution exceeded timeout {timeout_s}s",
                elapsed_ms,
            )

        tag, msg = outcome_box[0]
        elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
        if tag == DISPOSITION_KILLED:
            return MutationProofResult(obligation.mutation_id, DISPOSITION_KILLED, True, msg, elapsed_ms)
        if tag == DISPOSITION_RUNTIME_ERROR:
            return MutationProofResult(obligation.mutation_id, DISPOSITION_RUNTIME_ERROR, False, msg, elapsed_ms)

        if equivalence_proof is not None and equivalence_proof(real_module, mutated_mod):
            return MutationProofResult(
                obligation.mutation_id,
                DISPOSITION_EQUIVALENT,
                False,
                "semantic equivalence verified across input domain",
                elapsed_ms,
            )
        return MutationProofResult(obligation.mutation_id, DISPOSITION_SURVIVED, False, msg, elapsed_ms)
    finally:
        sys.modules.clear()
        sys.modules.update(saved_modules)
        sys.dont_write_bytecode = saved_dont_write


# ============================================================================
# Seeded Random Property Runner & Validity-Preserving Shrinker
# ============================================================================

@dataclass(frozen=True)
class PropertyReport:
    """Summary of a 64-case seeded domain loop or shrink outcome."""

    domain: str
    seed: int
    valid_cases: int
    failed_case_index: int | None
    minimal_fixture: Any
    shrink_attempts: int
    elapsed_vm_ms: int


def run_seeded_domain_loop(
    domain: str,
    *,
    seed: int,
    generator: Callable[[random.Random, int], Any],
    is_valid: Callable[[Any], bool],
    property_fn: Callable[[Any], bool],
    shrink_candidates: Callable[[Any], Sequence[Any]],
    target_valid_cases: int = 64,
    max_generation_attempts: int = 512,
    max_shrink_attempts: int = 32,
) -> PropertyReport:
    """Run ``target_valid_cases`` valid cases; shrink any counterexample in <= ``max_shrink_attempts``."""
    start_ns = time.monotonic_ns()
    rng = random.Random(seed)
    valid_count = 0
    attempts = 0

    while valid_count < target_valid_cases:
        if attempts >= max_generation_attempts:
            raise RuntimeError(
                f"budget exhaustion in domain {domain!r} (seed={seed}): "
                f"only {valid_count}/{target_valid_cases} valid cases after {attempts} attempts"
            )
        attempts += 1
        candidate = generator(rng, valid_count)
        if not is_valid(candidate):
            continue
        case_index = valid_count
        valid_count += 1
        if not property_fn(candidate):
            current = candidate
            shrink_steps = 0
            while shrink_steps < max_shrink_attempts:
                progressed = False
                for smaller in shrink_candidates(current):
                    if shrink_steps >= max_shrink_attempts:
                        break
                    shrink_steps += 1
                    if not is_valid(smaller):
                        continue
                    if not property_fn(smaller):
                        current = smaller
                        progressed = True
                        break
                if not progressed:
                    break
            elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
            return PropertyReport(
                domain=domain,
                seed=seed,
                valid_cases=valid_count,
                failed_case_index=case_index,
                minimal_fixture=current,
                shrink_attempts=shrink_steps,
                elapsed_vm_ms=elapsed_ms,
            )

    elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
    return PropertyReport(
        domain=domain,
        seed=seed,
        valid_cases=valid_count,
        failed_case_index=None,
        minimal_fixture=None,
        shrink_attempts=0,
        elapsed_vm_ms=elapsed_ms,
    )


# ============================================================================
# Catalogue Obligations
# ============================================================================

CATALOGUE_M1 = MutationObligation(
    mutation_id="M-wender-act-target-lock-registry-1",
    operator="B",
    file_path="wender/act/target_lock_registry.py",
    lineno=80,
    qualified_function="_integer",
    node="value is less than 0",
    selector="ops element 0",
    expected_source_line="    if type(value) is not int or value < 0:",
)

CATALOGUE_M2 = MutationObligation(
    mutation_id="M-wender-act-target-lock-registry-2",
    operator="B",
    file_path="wender/act/target_lock_registry.py",
    lineno=174,
    qualified_function="TargetLockRegistry.acquire",
    node="24",
    structural_path="body element 11.body element 0.value.args element 0",
    expected_source_line="            nonce = secrets.token_bytes(24)",
)


class TestCatalogueMutationsAndAstProveHarness(unittest.TestCase):
    """Verify the in-memory AST prove harness and kill both catalogue obligations."""

    def test_kill_catalogue_m1_target_lock_registry_integer_zero_boundary(self) -> None:
        """Kill M-wender-act-target-lock-registry-1 (B at target_lock_registry.py:80, _integer)."""

        def _check(mod: types.ModuleType) -> None:
            try:
                got = mod._integer(0, "now_ns")
            except Exception as exc:  # pragma: no cover - triggered on mutant
                raise AssertionError(f"_integer(0) raised {type(exc).__name__}: {exc}") from exc
            self.assertEqual(got, 0)

            collab = InlineSessionRegistryCollaborator(clock=lambda: 0)
            handle = collab.register_handle("cdp_tab", "tab-zero")
            registry = mod.TargetLockRegistry(collab, clock=lambda: 0)
            try:
                lease = registry.acquire(handle, "owner-zero", expires_ns=10, now_ns=0)
            except Exception as exc:
                raise AssertionError(f"acquire with now_ns=0 raised {type(exc).__name__}: {exc}") from exc
            self.assertEqual(lease.owner, "owner-zero")
            registry.release(lease)

        sys_before = dict(sys.modules)
        res = prove_mutation(CATALOGUE_M1, _check)
        self.assertEqual(sys.modules, sys_before)
        self.assertEqual(res.disposition, DISPOSITION_KILLED, res.detail)
        self.assertTrue(res.killed)

    def test_kill_catalogue_m2_target_lock_registry_acquire_retry_nonce_length(self) -> None:
        """Kill M-wender-act-target-lock-registry-2 (B at target_lock_registry.py:174, Constant 24)."""

        def _check(mod: types.ModuleType) -> None:
            collab = InlineSessionRegistryCollaborator(clock=lambda: 100)
            h1 = collab.register_handle("cdp_tab", "tab-1", actuation_resource="res-1")
            h2 = collab.register_handle("x11_window", "win-2", actuation_resource="res-2")
            reg = mod.TargetLockRegistry(collab, clock=lambda: 100)

            calls: list[int] = []

            def _fake_token_bytes(n: int) -> bytes:
                calls.append(n)
                # First two calls return the same 24-byte token, forcing the while-loop retry at line 174.
                if len(calls) <= 2:
                    return b"\xaa" * n
                return b"\xbb" * n

            with patch.object(mod.secrets, "token_bytes", side_effect=_fake_token_bytes):
                lease1 = reg.acquire(h1, "owner-1", expires_ns=500, now_ns=100)
                lease2 = reg.acquire(h2, "owner-2", expires_ns=500, now_ns=100)
                try:
                    self.assertEqual(len(lease1.nonce), 24)
                    self.assertEqual(len(lease2.nonce), 24)
                    self.assertEqual(calls, [24, 24, 24])
                finally:
                    reg.release(lease2)
                    reg.release(lease1)

        sys_before = dict(sys.modules)
        res = prove_mutation(CATALOGUE_M2, _check)
        self.assertEqual(sys.modules, sys_before)
        self.assertEqual(res.disposition, DISPOSITION_KILLED, res.detail)
        self.assertTrue(res.killed)

    def test_ast_prove_harness_operators_o_b_d_s_and_separate_dispositions(self) -> None:
        """Verify O (Compare/BoolOp/BinOp), B (Compare/Constant), D (If/Assert), S (Call), and all dispositions."""
        # 1. Operator O on Compare (Lt -> Gt) in target_lock_registry._integer
        m_o_cmp = MutationObligation(
            mutation_id="unit-O-compare",
            operator="O",
            file_path="wender/act/target_lock_registry.py",
            lineno=80,
            qualified_function="_integer",
            node="value < 0",
            selector="ops[0]",
        )
        res_o_cmp = prove_mutation(
            m_o_cmp,
            lambda mod: self.assertEqual(mod._integer(5, "x"), 5),
        )
        # Without catching TargetLockRefused inside check_fn, a raised TargetLockRefused is RUNTIME_ERROR (not a kill!).
        self.assertEqual(res_o_cmp.disposition, DISPOSITION_RUNTIME_ERROR)
        self.assertFalse(res_o_cmp.killed)

        def _semantic_check_int(mod: types.ModuleType) -> None:
            try:
                val = mod._integer(5, "x")
            except Exception as exc:
                raise AssertionError(f"unexpected exception: {exc}") from exc
            self.assertEqual(val, 5)

        res_o_cmp_killed = prove_mutation(m_o_cmp, _semantic_check_int)
        self.assertEqual(res_o_cmp_killed.disposition, DISPOSITION_KILLED)
        self.assertTrue(res_o_cmp_killed.killed)

        # 2. Operator O on BoolOp (Or -> And) at target_lock_registry.py:80
        m_o_bool = MutationObligation(
            mutation_id="unit-O-boolop",
            operator="O",
            file_path="wender/act/target_lock_registry.py",
            lineno=80,
            qualified_function="_integer",
            node="type(value) is not int or value < 0",
        )

        def _check_boolop(mod: types.ModuleType) -> None:
            try:
                mod._integer(-1, "neg")
            except mod.TargetLockRefused:
                return
            raise AssertionError("expected TargetLockRefused for -1")

        res_o_bool = prove_mutation(m_o_bool, _check_boolop)
        self.assertEqual(res_o_bool.disposition, DISPOSITION_KILLED)
        self.assertTrue(res_o_bool.killed)

        # 3. Operator O on BinOp (Add -> Sub) in interleaving_explorer
        m_o_bin = MutationObligation(
            mutation_id="unit-O-binop",
            operator="O",
            file_path="wender/learn/interleaving_explorer.py",
            lineno=195,
            qualified_function="validate_step_dag",
            node="-by_id[k].priority",
        )
        res_unary = prove_mutation(m_o_bin, lambda mod: None)
        self.assertEqual(res_unary.disposition, DISPOSITION_MISSING_ANCHOR)
        self.assertFalse(res_unary.killed)

        with open(REPO_ROOT / "wender/learn/interleaving_explorer.py", encoding="utf-8") as f:
            ie_lines = f.read().splitlines()
        seed_add_lineno = next(
            i + 1 for i, line in enumerate(ie_lines) if "seed=seed + attempt," in line
        )
        m_binop_add = MutationObligation(
            mutation_id="unit-O-binop-add",
            operator="O",
            file_path="wender/learn/interleaving_explorer.py",
            lineno=seed_add_lineno,
            qualified_function="explore_pct_schedules",
            node="seed + attempt",
        )

        def _check_pct_seed_progression(mod: types.ModuleType) -> None:
            steps = [
                mod.ExplorerStep(f"s{i}", f"a{i}", priority=1)
                for i in range(6)
            ]
            expected_second = mod.pct_schedule(steps, seed=11, depth=2, schedule_id=1)
            traces = mod.explore_pct_schedules(steps, seed=10, depth=2, max_schedules=2)
            self.assertEqual(traces[1].step_ids, expected_second.step_ids)

        res_binop = prove_mutation(m_binop_add, _check_pct_seed_progression)
        self.assertEqual(res_binop.disposition, DISPOSITION_KILLED)

        # 4. Operator D on raising If in target_lock_registry.py:110 (if registry is None:)
        m_d_if = MutationObligation(
            mutation_id="unit-D-if",
            operator="D",
            file_path="wender/act/target_lock_registry.py",
            lineno=110,
            qualified_function="TargetLockRegistry.__init__",
            node="if registry is None:\n    raise PrerequisiteAbsent('session registry required')",
        )

        def _check_none_registry(mod: types.ModuleType) -> None:
            with self.assertRaises(mod.PrerequisiteAbsent):
                mod.TargetLockRegistry(None)

        res_d_if = prove_mutation(m_d_if, _check_none_registry)
        self.assertEqual(res_d_if.disposition, DISPOSITION_KILLED)

        # 5. Operator S on Call positional arguments in target_lock_registry.py:94 (_integer(generation, "handle generation"))
        m_s_call = MutationObligation(
            mutation_id="unit-S-call",
            operator="S",
            file_path="wender/act/target_lock_registry.py",
            lineno=94,
            qualified_function="_identity",
            node="_integer(generation, 'handle generation')",
        )

        def _check_identity_call(mod: types.ModuleType) -> None:
            h = Handle("cdp_tab", "tab-1", 1)
            try:
                ident = mod._identity(h)
            except Exception as exc:
                raise AssertionError(f"_identity failed: {exc}") from exc
            self.assertEqual(ident, ("cdp_tab", "tab-1", 1))

        res_s_call = prove_mutation(m_s_call, _check_identity_call)
        self.assertEqual(res_s_call.disposition, DISPOSITION_KILLED)

        # 6. Separate dispositions: FAILED_CONTROL, SURVIVED, EQUIVALENT, COMPILE_IMPORT_ERROR, TIMEOUT
        res_failed_ctrl = prove_mutation(
            CATALOGUE_M1,
            lambda _mod: self.fail("intentional control failure"),
        )
        self.assertEqual(res_failed_ctrl.disposition, DISPOSITION_FAILED_CONTROL)
        self.assertFalse(res_failed_ctrl.killed)

        res_survived = prove_mutation(
            CATALOGUE_M1,
            lambda mod: self.assertEqual(mod._integer(10, "positive"), 10),
        )
        self.assertEqual(res_survived.disposition, DISPOSITION_SURVIVED)
        self.assertFalse(res_survived.killed)

        res_equiv = prove_mutation(
            CATALOGUE_M1,
            lambda mod: self.assertEqual(mod._integer(10, "positive"), 10),
            equivalence_proof=lambda real_m, mut_m: all(
                real_m._integer(v, "f") == mut_m._integer(v, "f") for v in range(1, 8)
            ),
        )
        self.assertEqual(res_equiv.disposition, DISPOSITION_EQUIVALENT)
        self.assertFalse(res_equiv.killed)

        res_compile_err = prove_mutation(
            CATALOGUE_M1,
            lambda mod: self.assertEqual(mod._integer(1, "x"), 1),
            inject_syntax_error=True,
        )
        self.assertEqual(res_compile_err.disposition, DISPOSITION_COMPILE_IMPORT_ERROR)
        self.assertFalse(res_compile_err.killed)

        real_tlr_mod = sys.modules["wender.act.target_lock_registry"]
        release_slow_mutant = threading.Event()
        try:
            res_timeout = prove_mutation(
                CATALOGUE_M1,
                lambda mod: release_slow_mutant.wait(timeout=0.5) if mod is not real_tlr_mod else None,
                timeout_s=0.01,
            )
        finally:
            release_slow_mutant.set()
        self.assertEqual(res_timeout.disposition, DISPOSITION_TIMEOUT)
        self.assertFalse(res_timeout.killed)


class TestExplorerValidationAndSchedules(unittest.TestCase):
    """Test DAG validation, Kahn & PCT schedule enumeration, lazy import guards, and controlled replay."""

    def test_validate_dag_rejects_duplicate_missing_and_cycles(self) -> None:
        s1 = ExplorerStep("step-1", "actor-a", priority=2)
        s1_dup = ExplorerStep("step-1", "actor-b", priority=1)
        with self.assertRaises(DuplicateStepError):
            validate_step_dag([s1, s1_dup])

        s_missing = ExplorerStep("step-2", "actor-b", predecessors=("ghost-step",))
        with self.assertRaises(MissingPredecessorError):
            validate_step_dag([s1, s_missing])

        s_self = ExplorerStep("self-loop", "actor-a", predecessors=("self-loop",))
        with self.assertRaises(CyclicDependencyError):
            validate_step_dag([s_self])

        c1 = ExplorerStep("c1", "actor-a", predecessors=("c3",))
        c2 = ExplorerStep("c2", "actor-b", predecessors=("c1",))
        c3 = ExplorerStep("c3", "actor-c", predecessors=("c2",))
        with self.assertRaises(CyclicDependencyError):
            validate_step_dag([c1, c2, c3])

        with self.assertRaises(InterleavingExplorerError):
            ExplorerStep("bad-preds", "actor-a", predecessors="not-a-tuple")  # type: ignore[arg-type]

        with self.assertRaises(InterleavingExplorerError):
            ExplorerStep("bad-prio", "actor-a", priority=True)  # type: ignore[arg-type]

    def test_lazy_import_guard_blocks_on_absent_sibling(self) -> None:
        mod = require_merged_dependency("wender.act.target_lock_registry")
        self.assertIs(mod.TargetLockRegistry, TargetLockRegistry)
        sym = require_merged_dependency("wender.perceive.parallel_perceive", "perceive_many")
        self.assertIs(sym, perceive_many)

        with self.assertRaisesRegex(PendingDependencyAbsentError, "AUSENTE"):
            require_merged_dependency("wender.learn.nonexistent_future_deliverable")

        with self.assertRaisesRegex(PendingDependencyAbsentError, "AUSENTE"):
            require_merged_dependency("wender.act.target_lock_registry", "nonexistent_future_symbol")

    def test_kahn_and_pct_schedule_enumeration_and_multithreaded_replay(self) -> None:
        log: list[str] = []
        lock = threading.Lock()

        def _make_action(tag: str) -> Callable[[StepExecutionContext], str]:
            def _run(ctx: StepExecutionContext) -> str:
                ctx.yield_control(f"enter:{tag}")
                with lock:
                    log.append(tag)
                ctx.yield_control(f"exit:{tag}")
                return f"done:{tag}"

            return _run

        steps = [
            ExplorerStep("init_a", "actor-a", priority=5, action=_make_action("init_a")),
            ExplorerStep("init_b", "actor-b", priority=3, action=_make_action("init_b")),
            ExplorerStep("use_a", "actor-a", priority=4, predecessors=("init_a",), action=_make_action("use_a")),
            ExplorerStep("use_b", "actor-b", priority=2, predecessors=("init_b",), action=_make_action("use_b")),
            ExplorerStep("join_ab", "actor-c", priority=1, predecessors=("use_a", "use_b"), action=_make_action("join_ab")),
        ]

        canonical = kahn_topological_schedule(steps)
        self.assertEqual(canonical.step_ids, ("init_a", "use_a", "init_b", "use_b", "join_ab"))

        all_kahn = enumerate_kahn_schedules(steps, max_schedules=MAX_BOUNDED_SCHEDULES)
        self.assertEqual(len(all_kahn), 6)
        self.assertEqual(len({t.step_ids for t in all_kahn}), 6)

        pct_traces = explore_pct_schedules(steps, seed=42, depth=2, max_schedules=6)
        self.assertEqual(len(pct_traces), 6)

        threads_before = {t.ident for t in threading.enumerate()}
        for trace in pct_traces:
            log.clear()
            replayed, outcomes, yields = replay_schedule(
                steps,
                trace,
                use_threads=True,
                max_threads=MAX_EXPLORER_THREADS,
                timeout_s=DEFAULT_JOIN_TIMEOUT_S,
            )
            self.assertEqual(tuple(log), trace.step_ids)
            self.assertEqual(replayed.step_ids, trace.step_ids)
            self.assertEqual(len(outcomes), 5)
            self.assertEqual(len(yields), 10)
            pos = {sid: idx for idx, sid in enumerate(log)}
            self.assertLess(pos["init_a"], pos["use_a"])
            self.assertLess(pos["init_b"], pos["use_b"])
            self.assertLess(pos["use_a"], pos["join_ab"])
            self.assertLess(pos["use_b"], pos["join_ab"])

        threads_after = {t.ident for t in threading.enumerate()}
        self.assertEqual(threads_after - threads_before, set())

        with self.assertRaises(ScheduleReplayError):
            replay_schedule(steps, ("use_a", "init_a", "init_b", "use_b", "join_ab"))


class TestTargetLockAndPerceiveManyCollaborators(unittest.TestCase):
    """Exercise real TargetLockRegistry, SessionRegistry, and perceive_many with corrected fixtures."""

    def test_target_lock_registry_real_session_registry_and_quarantine_semantics(self) -> None:
        now_box = [100]
        real_sr = SessionRegistry(clock=lambda: now_box[0])
        h_live = real_sr.register("cdp_tab", "tab-main", object(), owned=False)
        h_stale = Handle("cdp_tab", "tab-main", h_live.generation + 1)

        self.assertTrue(callable(real_sr.canonical_resource))
        self.assertEqual(real_sr.canonical_resource(h_live), "cdp_tab:tab-main")
        with self.assertRaises(StaleHandleError):
            real_sr.resolve(h_stale)

        lock_reg = TargetLockRegistry(real_sr, clock=lambda: now_box[0])
        lease = lock_reg.acquire(h_live, "actor-1", expires_ns=300, now_ns=100)
        self.assertIsInstance(lease, ActorLease)
        self.assertEqual(len(lease.nonce), 24)

        now_box[0] = 400
        with self.assertRaises(TargetBusy):
            lock_reg.acquire(h_live, "actor-2", expires_ns=800, now_ns=400)
        with self.assertRaises(TargetLockRefused):
            lock_reg.release(lease)

        now_box[0] = 200
        lock_reg.mark_inflight(lease)
        with self.assertRaises(TargetLockRefused):
            lock_reg.release(lease)

        ret = lock_reg.reconcile_quarantine(h_live, acknowledged=False)
        self.assertIsNone(ret)
        with self.assertRaises(TargetBusy):
            lock_reg.acquire(h_live, "actor-2", expires_ns=800, now_ns=200)

        ret_ack = lock_reg.reconcile_quarantine(h_live, acknowledged=True)
        self.assertIsNone(ret_ack)
        lease2 = lock_reg.acquire(h_live, "actor-2", expires_ns=800, now_ns=200)
        self.assertEqual(lease2.owner, "actor-2")
        lock_reg.release(lease2)

    def test_explorer_target_lock_interleavings_single_winner_per_schedule(self) -> None:
        traces = explore_target_lock_interleavings(("alpha", "beta", "gamma"), seed=7, max_schedules=6)
        self.assertEqual(len(traces), 6)
        for trace in traces:
            statuses = [ev.outcome["status"] for ev in trace.events]
            self.assertEqual(statuses.count("acquired"), 1)
            self.assertEqual(statuses.count("busy"), 2)
            self.assertEqual(trace.events[0].outcome["status"], "acquired")
            self.assertEqual(trace.events[0].outcome["owner"], trace.actors[0])
            self.assertEqual(trace.events[0].outcome["nonce_len"], 24)

    def test_perceive_many_strict_fixtures_validation_and_runner_error_receipts(self) -> None:
        h1 = Handle("x11_window", "win-1", 1)
        h2 = Handle("cdp_tab", "tab-2", 1)
        o1_f0 = build_valid_observation(h1, epoch=0, frame_seq=0, dpr_ppm=1000000, fill_byte=10)
        o1_f1 = build_valid_observation(h1, epoch=0, frame_seq=1, dpr_ppm=1000000, fill_byte=20)
        o2_f0 = build_valid_observation(h2, epoch=0, frame_seq=0, dpr_ppm=2000000, fill_byte=30)

        runs = explore_perceive_many_interleavings([o1_f0, o2_f0, o1_f1], max_schedules=2)
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0][0], (0, 1))
        self.assertEqual(runs[1][0], (1, 0))

        for _, results in runs:
            self.assertEqual(len(results), 3)
            self.assertEqual(results[0].handle, h1)
            self.assertEqual(results[1].handle, h2)
            self.assertEqual(results[2].handle, h1)
            for r in results:
                self.assertIsNone(r.error)
                self.assertIsNotNone(r.observation)
                perception_meta = r.observation.state["_perception"]
                self.assertEqual(len(perception_meta["screen_identity"]), 16)
                self.assertEqual(len(perception_meta["descriptor_hex"]), 16)
            self.assertEqual(results[0].observation.state["_perception"]["regions"], [])
            self.assertEqual(len(results[2].observation.state["_perception"]["regions"]), 1)

        bad_obs = types.SimpleNamespace(
            handle=h1,
            epoch=0,
            frame_seq=2,
            dpr_ppm=0,
            state={"nodes": []},
            frame=o1_f0.frame,
        )
        mixed_results = perceive_many(
            [o1_f0, bad_obs],
            adapter=DeterministicVisionAdapter(),
            runner=ControlledOrderRunner([0]),
        )
        self.assertIsNone(mixed_results[0].error)
        self.assertIsNone(mixed_results[1].observation)
        self.assertTrue(mixed_results[1].error.startswith("VALIDATION_ERROR:ObservationError:"))

        failing_runner = ControlledOrderRunner([0, 1], fail_after=0)
        runner_err_results = perceive_many(
            [o1_f0, o2_f0],
            adapter=DeterministicVisionAdapter(),
            runner=failing_runner,
        )
        self.assertEqual(len(runner_err_results), 2)
        for r in runner_err_results:
            self.assertIsNone(r.observation)
            self.assertTrue(r.error.startswith("RUNNER_ERROR:RuntimeError:injected runner failure"))


class TestSeededRandomPropertyLoopsAndShrinker(unittest.TestCase):
    """Run 64 valid seeded random cases per domain and verify <= 32 validity-preserving shrink attempts."""

    @staticmethod
    def _generate_random_dag(rng: random.Random, case_idx: int) -> tuple[ExplorerStep, ...]:
        n = rng.randint(3, 7)
        actors = ("actor-a", "actor-b", "actor-c", "actor-d")
        steps: list[ExplorerStep] = []
        for i in range(n):
            sid = f"s{i}"
            actor = actors[(i + case_idx) % len(actors)]
            prio = rng.randint(-5, 10)
            preds = tuple(f"s{j}" for j in range(i) if rng.random() < 0.35)
            steps.append(ExplorerStep(step_id=sid, actor=actor, priority=prio, predecessors=preds))
        rng.shuffle(steps)
        return tuple(steps)

    @staticmethod
    def _shrink_dag(steps: tuple[ExplorerStep, ...]) -> list[tuple[ExplorerStep, ...]]:
        candidates: list[tuple[ExplorerStep, ...]] = []
        for i in range(len(steps)):
            if len(steps) <= 1:
                break
            removed_id = steps[i].step_id
            reduced = [
                replace(st, predecessors=tuple(p for p in st.predecessors if p != removed_id))
                for j, st in enumerate(steps)
                if j != i
            ]
            candidates.append(tuple(reduced))
        for i, st in enumerate(steps):
            for p in st.predecessors:
                updated = list(steps)
                updated[i] = replace(st, predecessors=tuple(x for x in st.predecessors if x != p))
                candidates.append(tuple(updated))
        return candidates

    @staticmethod
    def _is_valid_dag(steps: tuple[ExplorerStep, ...]) -> bool:
        try:
            validate_step_dag(steps)
            return len(steps) >= 1
        except InterleavingExplorerError:
            return False

    def test_domain_1_64_valid_kahn_dag_schedules_and_replay(self) -> None:
        def _prop(steps: tuple[ExplorerStep, ...]) -> bool:
            trace = kahn_topological_schedule(steps)
            schedules = enumerate_kahn_schedules(steps, max_schedules=8)
            if not schedules or schedules[0].step_ids != trace.step_ids:
                return False
            by_id = {st.step_id: st for st in steps}
            for sched in schedules:
                if len(sched.step_ids) != len(steps):
                    return False
                pos = {sid: idx for idx, sid in enumerate(sched.step_ids)}
                for st in steps:
                    if any(pos[p] >= pos[st.step_id] for p in by_id[st.step_id].predecessors):
                        return False
            replayed, _, _ = replay_schedule(steps, trace)
            return replayed.step_ids == trace.step_ids

        report = run_seeded_domain_loop(
            "kahn_dag_enumeration",
            seed=20261020,
            generator=self._generate_random_dag,
            is_valid=self._is_valid_dag,
            property_fn=_prop,
            shrink_candidates=self._shrink_dag,
            target_valid_cases=64,
            max_shrink_attempts=32,
        )
        self.assertEqual(report.valid_cases, 64)
        self.assertIsNone(report.failed_case_index)

    def test_domain_2_64_valid_pct_schedules_determinism(self) -> None:
        def _prop(steps: tuple[ExplorerStep, ...]) -> bool:
            t1 = pct_schedule(steps, seed=17, depth=2)
            t2 = pct_schedule(steps, seed=17, depth=2)
            if t1 != t2:
                return False
            replayed, _, _ = replay_schedule(steps, t1)
            return replayed.step_ids == t1.step_ids

        report = run_seeded_domain_loop(
            "pct_schedule_determinism",
            seed=20261021,
            generator=self._generate_random_dag,
            is_valid=self._is_valid_dag,
            property_fn=_prop,
            shrink_candidates=self._shrink_dag,
            target_valid_cases=64,
            max_shrink_attempts=32,
        )
        self.assertEqual(report.valid_cases, 64)
        self.assertIsNone(report.failed_case_index)

    def test_domain_3_64_valid_target_lock_lifecycle_cases(self) -> None:
        kinds = ("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window")

        def _gen(rng: random.Random, idx: int) -> dict[str, Any]:
            return {
                "kind": kinds[idx % len(kinds)],
                "canonical_id": f"target-{idx}-{rng.randint(1, 999)}",
                "now_ns": rng.randint(0, 5000),
                "ttl_ns": rng.randint(1, 5000),
                "inflight": rng.choice((False, True)),
                "ack": rng.choice((False, True)),
            }

        def _valid(case: dict[str, Any]) -> bool:
            return case["kind"] in kinds and case["now_ns"] >= 0 and case["ttl_ns"] >= 1

        def _prop(case: dict[str, Any]) -> bool:
            now_box = [case["now_ns"]]
            collab = InlineSessionRegistryCollaborator(clock=lambda: now_box[0])
            h = collab.register_handle(case["kind"], case["canonical_id"])
            reg = TargetLockRegistry(collab, clock=lambda: now_box[0])
            lease = reg.acquire(h, "owner-1", expires_ns=case["now_ns"] + case["ttl_ns"], now_ns=case["now_ns"])
            if len(lease.nonce) != 24:
                return False
            if not case["inflight"]:
                reg.release(lease)
                lease_again = reg.acquire(h, "owner-2", expires_ns=case["now_ns"] + case["ttl_ns"], now_ns=case["now_ns"])
                reg.release(lease_again)
                return True
            reg.mark_inflight(lease)
            reg.reconcile_quarantine(h, acknowledged=case["ack"])
            if not case["ack"]:
                try:
                    reg.acquire(h, "owner-2", expires_ns=case["now_ns"] + case["ttl_ns"], now_ns=case["now_ns"])
                    return False
                except TargetBusy:
                    reg.reconcile_quarantine(h, acknowledged=True)
            lease_after = reg.acquire(h, "owner-2", expires_ns=case["now_ns"] + case["ttl_ns"], now_ns=case["now_ns"])
            reg.release(lease_after)
            return True

        report = run_seeded_domain_loop(
            "target_lock_lifecycle",
            seed=20261022,
            generator=_gen,
            is_valid=_valid,
            property_fn=_prop,
            shrink_candidates=lambda _: (),
            target_valid_cases=64,
            max_shrink_attempts=32,
        )
        self.assertEqual(report.valid_cases, 64)
        self.assertIsNone(report.failed_case_index)

    def test_domain_4_64_valid_perceive_many_batches(self) -> None:
        kinds = ("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window")

        def _gen(rng: random.Random, idx: int) -> list[Observation]:
            count = rng.randint(1, 4)
            batch: list[Observation] = []
            for j in range(count):
                h = Handle(kinds[(idx + j) % len(kinds)], f"win-{j}", 1)
                batch.append(
                    build_valid_observation(
                        h,
                        epoch=0,
                        frame_seq=j,
                        dpr_ppm=1000000,
                        width=4,
                        height=4,
                        fill_byte=(idx + j * 17) & 0xFF,
                    )
                )
            return batch

        def _prop(batch: list[Observation]) -> bool:
            runner = ControlledOrderRunner()
            res = perceive_many(batch, adapter=DeterministicVisionAdapter(), runner=runner)
            if len(res) != len(batch):
                return False
            return all(r.error is None and r.handle == obs.handle for r, obs in zip(res, batch))

        report = run_seeded_domain_loop(
            "perceive_many_batches",
            seed=20261023,
            generator=_gen,
            is_valid=lambda b: len(b) >= 1,
            property_fn=_prop,
            shrink_candidates=lambda b: [b[:-1]] if len(b) > 1 else [],
            target_valid_cases=64,
            max_shrink_attempts=32,
        )
        self.assertEqual(report.valid_cases, 64)
        self.assertIsNone(report.failed_case_index)

    def test_validity_preserving_shrinker_reports_minimal_fixture_and_budget_exhaustion(self) -> None:
        report = run_seeded_domain_loop(
            "synthetic_failing_dag",
            seed=99,
            generator=self._generate_random_dag,
            is_valid=self._is_valid_dag,
            property_fn=lambda steps: all(len(st.predecessors) < 2 for st in steps),
            shrink_candidates=self._shrink_dag,
            target_valid_cases=64,
            max_shrink_attempts=32,
        )
        self.assertIsNotNone(report.failed_case_index)
        self.assertIsNotNone(report.minimal_fixture)
        self.assertLessEqual(report.shrink_attempts, 32)
        self.assertTrue(self._is_valid_dag(report.minimal_fixture))
        self.assertFalse(all(len(st.predecessors) < 2 for st in report.minimal_fixture))

        with self.assertRaisesRegex(RuntimeError, "budget exhaustion"):
            run_seeded_domain_loop(
                "impossible_valid_domain",
                seed=1,
                generator=self._generate_random_dag,
                is_valid=lambda _: False,
                property_fn=lambda _: True,
                shrink_candidates=lambda _: (),
                target_valid_cases=64,
                max_generation_attempts=16,
            )


if __name__ == "__main__":
    unittest.main()
