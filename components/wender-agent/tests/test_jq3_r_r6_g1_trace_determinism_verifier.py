# @module {"id":"tests.test_jq3_r_r6_g1_trace_determinism_verifier","layer":3,"owner":"TESTS","deps":["wa.learn.trace_determinism_verifier","wa.learn.multiwin_scorecard","wa.learn.replay","wa.perceive.session_registry","wa.state.schema"],"book":"HB-WA-s3-remember"}
"""Acceptance test suite for jq3-r-r6-g1-trace-determinism-verifier.

Covers:
1. Verifier-owned TraceEnvelope adaptation from landed WindowEvidence rows via
   frame_seq and event_index without diagnostic wall time.
2. Bit-exact complete-trace digest chain and cross-window bleed auditor:
   truncated/altered seal strings, reordered/duplicated steps, intact reordered
   evidence row lists under Counter membership, foreign observations, altered
   and duplicate checkpoints, complete reset evidence, and preservation of
   unavailable metric denominators.
3. Self-contained in-memory AST prove harness killing catalogue obligations:
   - M-wender-learn-multiwin-scorecard-1 (O at wender/learn/multiwin_scorecard.py:1109)
   - M-wender-learn-multiwin-scorecard-2 (B at wender/learn/multiwin_scorecard.py:434)
   plus verifier mutations across O, B (Compare and Constant), D (If and Assert),
   and S (Call), with separate dispositions for FAILED_CONTROL, MISSING_ANCHOR,
   COMPILE_OR_IMPORT_ERROR, TIMEOUT, SURVIVED, EQUIVALENT, and KILLED.
4. Seeded stdlib random property loops (64 valid cases per domain, <= 32 shrink
   steps with minimal fixture reporting).
5. Shared-state concurrency schedule tests (32 replayable schedules, <= 4 threads,
   Barrier/Event coordination with 2s waits, zero leaked threads).
"""
from __future__ import annotations

import ast
import copy
from dataclasses import dataclass, fields, replace
import hashlib
import importlib
import importlib.machinery
import json
from pathlib import Path
import random
import re
import sys
import threading
import time
import types
from types import SimpleNamespace
from typing import Callable
import unittest

import numpy as np

from wender.learn import multiwin_scorecard as mws
from wender.learn import replay as replay_mod
from wender.learn import trace_determinism_verifier as tdv
from wender.perceive import session_registry as sr
from wender.remember import skills
from wender.state import schema as state_schema


REPO_ROOT = Path(__file__).resolve().parents[1]
PROPERTY_CASES_TARGET = 64
MAX_SHRINK_ATTEMPTS = 32
CONCURRENCY_SCHEDULES_TARGET = 32
MAX_THREADS = 4
WAIT_TIMEOUT_S = 2.0


def _hkey(handle: object) -> tuple[str, str, int]:
    return handle.kind, handle.canonical_id, handle.generation


def _readonly_rgb(arr: np.ndarray) -> np.ndarray:
    return np.frombuffer(arr.tobytes(order="C"), dtype=np.uint8).reshape(arr.shape)


@dataclass
class _OfflineRegistry:
    adapter: "_OfflineAdapter"

    def resolve(self, handle: sr.Handle):
        a = self.adapter
        a.calls.append(("resolve", _hkey(handle)))
        return SimpleNamespace(
            handle=a.registry_handle or a.handle,
            epoch=a.epoch,
            closed=a.closed,
            transport=self,
            owned=True,
            session_id=a.handle.kind,
            attachment_owned=True,
        )


@dataclass
class _OfflineStream:
    adapter: "_OfflineAdapter"

    def deliver(self, frame: sr.FrameRef):
        a = self.adapter
        a.calls.append(("deliver", frame.frame_seq))
        if frame.frame_seq in a.drop_seqs:
            return None
        if a.stream_mode == "foreign":
            return SimpleNamespace(
                handle=sr.Handle("cdp_tab", "foreign-stream", 1),
                epoch=frame.epoch,
                frame_seq=frame.frame_seq,
                width=frame.width,
                height=frame.height,
                rgb=frame.rgb,
                digest=frame.digest,
            )
        if a.stream_mode == "torn":
            return SimpleNamespace(
                handle=frame.handle,
                epoch=frame.epoch,
                frame_seq=frame.frame_seq,
                width=frame.width,
                height=frame.height,
                rgb=frame.rgb,
                digest="0" * 64,
            )
        return frame


@dataclass
class _OfflinePerception:
    adapter: "_OfflineAdapter"

    def observe(self, frame: sr.FrameRef) -> sr.Observation:
        a = self.adapter
        a.calls.append(("observe", frame.frame_seq))
        state = {
            "cursor": a.memory.get("cursor", 0),
            "previous": a.memory.get("previous"),
            "rand": a.rng.getrandbits(64),
            "target": list(_hkey(a.handle)),
        }
        if a.extra_state is not None:
            state.update(a.extra_state())
        obs = sr.Observation(a.handle, frame.epoch, frame.frame_seq, a.dpr_ppm, state, frame)
        a.observations.append(obs)
        return obs

    def reduce(self, observation: sr.Observation) -> sr.ChangeResult:
        a = self.adapter
        a.calls.append(("reduce", observation.frame_seq))
        a.memory["cursor"] = a.memory.get("cursor", 0) + 1
        a.memory["previous"] = observation.frame.digest
        claims = observation.frame.rgb[:, :, 0] > 0
        ops = {} if a.empty_ops else {"pixels": int(claims.size), "reductions": 1}
        if a.operation_bleed:
            ops["reductions"] = a.memory["cursor"]
        return sr.ChangeResult(
            a.change_handle or a.handle,
            observation.epoch,
            a.settled,
            claims,
            ops,
        )


@dataclass
class _OfflineScheduler:
    adapter: "_OfflineAdapter"

    def admit(self, action: sr.ActionRequest, observation: sr.Observation, change: sr.ChangeResult, now_ns: int):
        a = self.adapter
        a.calls.append(("admit", action.action_id))
        telemetry = {
            "resource_key": a.resource_key,
            "active_resource": 1,
            "focus_authorized": True,
            "handle_fresh": True,
            "fence_clear": True,
            "guard_passed": True,
            "geometry_matches": True,
            "global_committed": a.committed,
            "global_reserved": action.cost,
            "global_cap": 10000,
            "target_committed": a.committed,
            "target_reserved": action.cost,
            "target_cap": 10000,
            "wait_opportunities": 0,
            "queue_depth": 1,
            "fairness_cost": 1,
            "quantum": 1,
            "urgent_cap": 1,
            "active_focus": 1,
        }
        telemetry.update(a.telemetry)
        return {
            "admitted": a.admit,
            "eligible": a.eligible,
            "reason": a.reason,
            "telemetry": telemetry,
        }


@dataclass
class _OfflineActor:
    adapter: "_OfflineAdapter"

    def execute(self, action: sr.ActionRequest, observation: sr.Observation, now_ns: int) -> sr.Receipt:
        a = self.adapter
        a.calls.append(("execute", action.action_id))
        a.committed += action.cost
        a.clock.advance(action.payload.get("advance_ns", 0))
        if action.payload.get("raise") == "timeout":
            raise TimeoutError("simulated timeout")
        if action.payload.get("raise") == "crash":
            raise ConnectionError("simulated crash")
        return sr.Receipt(
            action.action_id,
            a.receipt_handle or a.handle,
            action.payload.get("admission", "ADMITTED"),
            None,
        )


class _OfflineAdapter:
    offline = True

    def __init__(self, handle: sr.Handle, *, resource_key: str | None = None, shared_memory: dict | None = None):
        self.handle = handle
        self.resource_key = resource_key or f"{handle.kind}/{handle.canonical_id}"
        self.focus_domain = f"focus/{self.resource_key}"
        self.gates = {
            g: {"status": "PASS", "evidence": f"offline-fixture:{g}", "expected_failure": False}
            for g in mws.PREREQUISITE_GATES
        }
        self.registry = _OfflineRegistry(self)
        self.stream = _OfflineStream(self)
        self.perception = _OfflinePerception(self)
        self.scheduler = _OfflineScheduler(self)
        self.actor = _OfflineActor(self)
        self.calls: list[tuple[str, object]] = []
        self.seeds: list[int] = []
        self.observations: list[sr.Observation] = []
        self.memory = {} if shared_memory is None else shared_memory
        self.epoch = 0
        self.closed = False
        self.dpr_ppm = 1_000_000
        self.registry_handle = None
        self.receipt_handle = None
        self.change_handle = None
        self.stream_mode = None
        self.drop_seqs: set[int] = set()
        self.telemetry: dict[str, object] = {}
        self.admit = True
        self.eligible = True
        self.reason = None
        self.settled = True
        self.empty_ops = False
        self.operation_bleed = False
        self.fail_reset = False
        self.extra_state = None
        self.committed = 0

    def reset(self, *, seed: int, clock: mws.FakeClock) -> None:
        if self.fail_reset:
            raise RuntimeError("simulated reset failure")
        self.calls.append(("reset", seed))
        self.seeds.append(seed)
        self.rng = random.Random(seed)
        self.clock = clock
        self.memory.clear()
        self.committed = 0

    def terminal(self, action: sr.ActionRequest, receipt: sr.Receipt, now_ns: int) -> sr.Receipt:
        self.calls.append(("terminal", action.action_id))
        return sr.Receipt(
            action.action_id,
            self.handle,
            action.payload.get("terminal", "SUCCESS"),
            None,
        )


def _make_event(
    handle: sr.Handle,
    seq: int,
    at_ns: int,
    *,
    epoch: int = 0,
    scene: str = "scene-main",
    action_id: str | None = None,
    payload: dict | None = None,
    expected: str = "SUCCESS",
    with_action: bool = True,
):
    rgb = np.zeros((2, 3, 3), dtype=np.uint8)
    rgb[:, :, 0] = np.array([[1, 0, 1], [0, 1, 1]], dtype=np.uint8)
    rgb[:, :, 1] = (seq + 1) % 251
    truth = rgb[:, :, 0] > 0
    if with_action:
        aid = action_id or f"{handle.kind}:{handle.canonical_id}:{handle.generation}:{epoch}:{seq}"
        action = sr.ActionRequest(handle, aid, epoch, payload or {}, 1, 1_000_000)
        outcome = (aid, expected)
    else:
        action = None
        outcome = None
    event = {
        "handle": handle,
        "scene": scene,
        "epoch": epoch,
        "frame_seq": seq,
        "at_ns": at_ns,
        "rgb": rgb,
        "truth": truth,
        "action": action,
    }
    return event, outcome


def _build_scenario(
    kinds: tuple[str, ...] = ("cdp_tab", "godot_window"),
    frames_per_target: int = 2,
    *,
    generation: int = 1,
) -> tuple[mws.Scenario, dict[tuple[str, str, int], _OfflineAdapter]]:
    handles = tuple(sr.Handle(k, f"win-{idx}", generation) for idx, k in enumerate(kinds))
    adapters = {_hkey(h): _OfflineAdapter(h) for h in handles}
    events = []
    expected = {}
    for seq in range(frames_per_target):
        for h in handles:
            ev, out = _make_event(h, seq, 10 * (len(events) + 1))
            events.append(ev)
            if out is not None:
                expected[out[0]] = out[1]
    return mws.Scenario("offline-multiwin", handles, tuple(events), expected), adapters


def _build_schema_state(nodes: list[dict]) -> dict:
    pruned_nodes = []
    boxes = {}
    for i, n in enumerate(nodes):
        nid = n.get("id", f"{i + 1:016x}")
        pruned_nodes.append(
            {
                "id": nid,
                "role": n.get("role", "button"),
                "name": n.get("name", f"Node {i}"),
                "depth": 0,
                "ordinal": i,
                "attrs": [],
            }
        )
        boxes[nid] = [0, 0, 10, 10]
    return state_schema.build_state(
        pruned_nodes,
        kind="cdp",
        url="http://example.com",
        title="Test App",
        ax_sha256="0" * 64,
        ax_stable_sha256="0" * 64,
        prune={"name": "prune_ax_v1", "version": 1},
        raw_count=len(pruned_nodes),
        boxes=boxes,
        actionable_roles=frozenset({"button"}),
    )


class _OfflineReplayDriver:
    """Deterministic in-RAM driver for replay_and_admit and verify_replay_trace."""

    def __init__(self) -> None:
        self.reset_count = 0
        self.cursor = 0
        self._states = [
            _build_schema_state([{"id": "0000000000000001", "role": "button", "name": "Start"}]),
            _build_schema_state(
                [
                    {"id": "0000000000000001", "role": "button", "name": "Start"},
                    {"id": "0000000000000002", "role": "status", "name": "Step1 Ready"},
                ]
            ),
            _build_schema_state(
                [
                    {"id": "0000000000000001", "role": "button", "name": "Start"},
                    {"id": "0000000000000003", "role": "status", "name": "Step2 Complete"},
                ]
            ),
        ]

    def reset(self) -> None:
        self.reset_count += 1
        self.cursor = 0

    def observe(self) -> tuple[dict, str]:
        idx = min(self.cursor, len(self._states) - 1)
        return copy.deepcopy(self._states[idx]), f"{idx:016x}"

    def act(self, step: dict) -> None:
        self.cursor += 1


def _make_replay_draft() -> dict:
    return {
        "id": "skill-deterministic-1",
        "intent": "complete two-step workflow",
        "params": {"mode": "fast"},
        "steps": [
            {"path": "ax", "op": "press", "target": "0000000000000001"},
            {"path": "ax", "op": "press", "target": "0000000000000002"},
        ],
        "checkpoints": [
            {"after_step": 0, "expect": {"node_present": {"role": "status", "name": "Step1 Ready"}}},
            {"after_step": 1, "expect": {"text_contains": "Step2 Complete"}},
        ],
        "provenance": {"source": "demo:trace#1"},
        "evidence": {"reproduced": True, "verified_by": "bootstrap", "version": 1},
    }


DISPOSITION_KILLED = "KILLED"
DISPOSITION_SURVIVED = "SURVIVED"
DISPOSITION_EQUIVALENT = "EQUIVALENT"
DISPOSITION_FAILED_CONTROL = "FAILED_CONTROL"
DISPOSITION_MISSING_ANCHOR = "MISSING_ANCHOR"
DISPOSITION_COMPILE_OR_IMPORT_ERROR = "COMPILE_OR_IMPORT_ERROR"
DISPOSITION_TIMEOUT = "TIMEOUT"


class MutationAnchorError(LookupError):
    """Raised when a mutation anchor or structural selector is missing or ambiguous."""


@dataclass(frozen=True)
class MutationObligation:
    """Catalogue or verifier mutation specification."""

    obligation_id: str
    module_path: str
    module_name: str
    qualified_function: str
    lineno: int
    operator: str  # 'O', 'B', 'D', or 'S'
    original_text: str
    structural_selector: str | None = None
    probe: Callable[[types.ModuleType], None] | None = None
    is_equivalent: bool = False
    equivalence_proof: str | None = None
    timeout_s: float = 2.0


@dataclass(frozen=True)
class MutationProveResult:
    """Outcome record for a single in-memory AST mutation execution."""

    obligation_id: str
    disposition: str
    lineno: int
    original_source: str
    mutated_source: str
    detail: str


_COMPARE_OP_MAP: dict[type[ast.cmpop], type[ast.cmpop]] = {
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

_BOUNDARY_CMP_MAP: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
}

_BOOL_OP_MAP: dict[type[ast.boolop], type[ast.boolop]] = {
    ast.And: ast.Or,
    ast.Or: ast.And,
}

_BIN_OP_MAP: dict[type[ast.operator], type[ast.operator]] = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.FloorDiv,
    ast.FloorDiv: ast.Mult,
}


def _normalize_ws(text: str) -> str:
    return re.sub(r"\n[ \t]+", "\n ", text.strip())


def _resolve_qualified_function(tree: ast.Module, qualified_function: str) -> ast.AST:
    parts = [p for p in qualified_function.split(".") if p]
    if not parts:
        raise MutationAnchorError("empty qualified_function")

    def _find(body: list[ast.stmt], rem: list[str]) -> list[ast.AST]:
        head, tail = rem[0], rem[1:]
        found: list[ast.AST] = []
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and stmt.name == head:
                if not tail:
                    found.append(stmt)
                else:
                    found.extend(_find(stmt.body, tail))
        return found

    matches = _find(tree.body, parts)
    if len(matches) != 1:
        raise MutationAnchorError(
            f"qualified_function {qualified_function!r} matched {len(matches)} nodes"
        )
    return matches[0]


def _navigate_structural_path(root: ast.AST, selector: str) -> ast.AST:
    """Navigate a dot-separated structural path such as 'body element 13.body element 17.test'."""
    current: object = root
    steps = [s.strip() for s in selector.split(".") if s.strip()]
    if not steps:
        raise MutationAnchorError("empty structural_selector")
    for step in steps:
        if step == "self":
            continue
        m_elem = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s+element\s+(\d+)", step)
        if not m_elem:
            m_elem = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\[(\d+)\]", step)
        if m_elem:
            field_name, idx_str = m_elem.group(1), int(m_elem.group(2))
            if not isinstance(current, ast.AST) or not hasattr(current, field_name):
                raise MutationAnchorError(f"missing field {field_name!r} at step {step!r}")
            seq = getattr(current, field_name)
            if not isinstance(seq, list) or not (0 <= idx_str < len(seq)):
                raise MutationAnchorError(f"index {idx_str} out of range for {field_name!r}")
            current = seq[idx_str]
        else:
            if not isinstance(current, ast.AST) or not hasattr(current, step):
                raise MutationAnchorError(f"missing attribute {step!r}")
            current = getattr(current, step)
    if not isinstance(current, ast.AST):
        raise MutationAnchorError("structural selector did not resolve to an AST node")
    return current


def _apply_ast_operator(
    operator: str,
    target_node: ast.AST,
    *,
    expect_comparison_for_b: bool | None = None,
) -> ast.AST:
    """Apply O, B, D, or S to a deep-copied AST node according to catalogue rules."""
    mutated = copy.deepcopy(target_node)

    if operator == "O":
        if isinstance(mutated, ast.Compare):
            if not mutated.ops or any(type(op) not in _COMPARE_OP_MAP for op in mutated.ops):
                raise MutationAnchorError("Compare node contains unsupported comparison operator for O")
            mutated.ops = [_COMPARE_OP_MAP[type(op)]() for op in mutated.ops]
            return mutated
        if isinstance(mutated, ast.BoolOp):
            op_cls = _BOOL_OP_MAP.get(type(mutated.op))
            if op_cls is None:
                raise MutationAnchorError("unsupported BoolOp for O")
            mutated.op = op_cls()
            return mutated
        if isinstance(mutated, ast.BinOp):
            op_cls = _BIN_OP_MAP.get(type(mutated.op))
            if op_cls is None:
                raise MutationAnchorError("unsupported BinOp for O")
            mutated.op = op_cls()
            return mutated
        raise MutationAnchorError(f"operator O unsupported on {type(mutated).__name__}")

    if operator == "B":
        if expect_comparison_for_b is True and not isinstance(mutated, ast.Compare):
            raise MutationAnchorError("comparison selector resolved to non-Compare node for B")
        if expect_comparison_for_b is False and not isinstance(mutated, ast.Constant):
            raise MutationAnchorError("constant selector resolved to non-Constant node for B")

        if isinstance(mutated, ast.Compare):
            if not mutated.ops or any(type(op) not in _BOUNDARY_CMP_MAP for op in mutated.ops):
                raise MutationAnchorError("Compare node lacks Lt/LtE/Gt/GtE for B")
            mutated.ops = [_BOUNDARY_CMP_MAP[type(op)]() for op in mutated.ops]
            return mutated
        if isinstance(mutated, ast.Constant) and type(mutated.value) is int:
            return ast.Constant(value=mutated.value + 1)
        raise MutationAnchorError(
            f"operator B requires Compare(Lt/LtE/Gt/GtE) or exact int Constant, got {type(mutated).__name__}"
        )

    if operator == "D":
        if isinstance(mutated, ast.If):
            has_raise = any(isinstance(sub, ast.Raise) for stmt in mutated.body for sub in ast.walk(stmt))
            if not has_raise:
                raise MutationAnchorError("D on If requires a raising If body")
            mutated.test = ast.copy_location(ast.Constant(value=False), mutated.test)
            return mutated
        if isinstance(mutated, ast.Assert):
            mutated.test = ast.copy_location(ast.Constant(value=True), mutated.test)
            return mutated
        raise MutationAnchorError(f"operator D requires raising If or Assert, got {type(mutated).__name__}")

    if operator == "S":
        if not isinstance(mutated, ast.Call):
            raise MutationAnchorError(f"operator S requires Call node, got {type(mutated).__name__}")
        non_starred_indices = [
            idx for idx, arg in enumerate(mutated.args) if not isinstance(arg, ast.Starred)
        ]
        if len(non_starred_indices) < 2:
            raise MutationAnchorError("Call node has fewer than two non-starred positional arguments")
        first_idx = non_starred_indices[0]
        second_idx = None
        first_dump = ast.dump(mutated.args[first_idx])
        for cand_idx in non_starred_indices[1:]:
            if ast.dump(mutated.args[cand_idx]) != first_dump:
                second_idx = cand_idx
                break
        if second_idx is None:
            raise MutationAnchorError("Call node lacks two distinct non-starred positional arguments")
        mutated.args[first_idx], mutated.args[second_idx] = (
            mutated.args[second_idx],
            mutated.args[first_idx],
        )
        return mutated

    raise MutationAnchorError(f"unknown mutation operator {operator!r}")


def _replace_node_identity(root: ast.AST, old_node: ast.AST, new_node: ast.AST) -> int:
    count = 0
    for parent in ast.walk(root):
        for field_name, field_val in ast.iter_fields(parent):
            if field_val is old_node:
                setattr(parent, field_name, new_node)
                count += 1
            elif isinstance(field_val, list):
                for idx, item in enumerate(field_val):
                    if item is old_node:
                        field_val[idx] = new_node
                        count += 1
    return count


class InMemoryAstProveHarness:
    """Self-contained in-memory AST prove harness with strict anchor and cleanup guarantees."""

    _seq = 0

    @classmethod
    def prove(cls, obligation: MutationObligation) -> MutationProveResult:
        if obligation.probe is None:
            return MutationProveResult(
                obligation.obligation_id,
                DISPOSITION_MISSING_ANCHOR,
                obligation.lineno,
                obligation.original_text,
                "",
                "probe is None",
            )

        src_path = REPO_ROOT / obligation.module_path
        if not src_path.is_file():
            return MutationProveResult(
                obligation.obligation_id,
                DISPOSITION_MISSING_ANCHOR,
                obligation.lineno,
                obligation.original_text,
                "",
                f"missing file {obligation.module_path}",
            )

        source_text = src_path.read_text(encoding="utf-8")

        real_mod = importlib.import_module(obligation.module_name)
        try:
            obligation.probe(real_mod)
        except BaseException as exc:
            return MutationProveResult(
                obligation.obligation_id,
                DISPOSITION_FAILED_CONTROL,
                obligation.lineno,
                obligation.original_text,
                "",
                f"positive control failed: {type(exc).__name__}: {exc}",
            )

        try:
            tree = ast.parse(source_text, filename=obligation.module_path)
            fn_node = _resolve_qualified_function(tree, obligation.qualified_function)

            if obligation.structural_selector is not None:
                target_node = _navigate_structural_path(fn_node, obligation.structural_selector)
                seg = ast.get_source_segment(source_text, target_node) or ""
                if getattr(target_node, "lineno", None) != obligation.lineno:
                    raise MutationAnchorError(
                        f"lineno mismatch: expected {obligation.lineno}, got {getattr(target_node, 'lineno', None)}"
                    )
                if seg != obligation.original_text and _normalize_ws(seg) != _normalize_ws(obligation.original_text):
                    raise MutationAnchorError(
                        f"source text mismatch: expected {obligation.original_text!r}, got {seg!r}"
                    )
            else:
                candidates: list[ast.AST] = []
                for sub in ast.walk(fn_node):
                    if getattr(sub, "lineno", None) == obligation.lineno:
                        seg = ast.get_source_segment(source_text, sub) or ""
                        if seg == obligation.original_text or _normalize_ws(seg) == _normalize_ws(obligation.original_text):
                            candidates.append(sub)
                if len(candidates) != 1:
                    raise MutationAnchorError(
                        f"expected 1 matching node at line {obligation.lineno}, found {len(candidates)}"
                    )
                target_node = candidates[0]
                seg = ast.get_source_segment(source_text, target_node) or ""

            expect_cmp_b: bool | None = None
            if obligation.operator == "B":
                expect_cmp_b = not obligation.original_text.strip().lstrip("-").isdigit()

            replacement = _apply_ast_operator(
                obligation.operator,
                target_node,
                expect_comparison_for_b=expect_cmp_b,
            )
            ast.copy_location(replacement, target_node)
            replaced_count = _replace_node_identity(fn_node, target_node, replacement)
            if replaced_count != 1:
                raise MutationAnchorError(f"expected 1 AST replacement, got {replaced_count}")
            ast.fix_missing_locations(tree)
            mutated_src_str = ast.unparse(replacement)
        except MutationAnchorError as exc:
            return MutationProveResult(
                obligation.obligation_id,
                DISPOSITION_MISSING_ANCHOR,
                obligation.lineno,
                obligation.original_text,
                "",
                str(exc),
            )

        cls._seq += 1
        pkg_name = obligation.module_name.rsplit(".", 1)[0] if "." in obligation.module_name else ""
        private_mod_name = f"{pkg_name}._ast_mutant_{cls._seq}" if pkg_name else f"_ast_mutant_{cls._seq}"
        prev_bytecode_flag = sys.dont_write_bytecode
        saved_modules = dict(sys.modules)

        try:
            sys.dont_write_bytecode = True
            try:
                code_obj = compile(
                    tree,
                    filename=f"<inmem-ast:{obligation.obligation_id}:{obligation.module_path}>",
                    mode="exec",
                    dont_inherit=True,
                )
                private_mod = types.ModuleType(private_mod_name)
                private_mod.__file__ = str(src_path)
                private_mod.__package__ = pkg_name
                private_mod.__loader__ = None
                private_mod.__spec__ = importlib.machinery.ModuleSpec(
                    private_mod_name, None, origin=str(src_path)
                )
                sys.modules[private_mod_name] = private_mod
                exec(code_obj, private_mod.__dict__)
            except BaseException as exc:
                return MutationProveResult(
                    obligation.obligation_id,
                    DISPOSITION_COMPILE_OR_IMPORT_ERROR,
                    obligation.lineno,
                    seg,
                    mutated_src_str,
                    f"compile/import error: {type(exc).__name__}: {exc}",
                )

            outcome_box: dict[str, BaseException | None] = {"exc": None}

            def _run_probe() -> None:
                try:
                    obligation.probe(private_mod)
                except BaseException as exc:
                    outcome_box["exc"] = exc

            worker = threading.Thread(target=_run_probe, name=f"probe-{obligation.obligation_id}")
            worker.daemon = True
            worker.start()
            worker.join(timeout=obligation.timeout_s)
            if worker.is_alive():
                return MutationProveResult(
                    obligation.obligation_id,
                    DISPOSITION_TIMEOUT,
                    obligation.lineno,
                    seg,
                    mutated_src_str,
                    f"probe exceeded {obligation.timeout_s}s timeout",
                )

            probe_exc = outcome_box["exc"]
            if isinstance(probe_exc, AssertionError):
                return MutationProveResult(
                    obligation.obligation_id,
                    DISPOSITION_KILLED,
                    obligation.lineno,
                    seg,
                    mutated_src_str,
                    f"killed by AssertionError: {probe_exc}",
                )
            if probe_exc is not None:
                return MutationProveResult(
                    obligation.obligation_id,
                    DISPOSITION_COMPILE_OR_IMPORT_ERROR,
                    obligation.lineno,
                    seg,
                    mutated_src_str,
                    f"non-AssertionError exception: {type(probe_exc).__name__}: {probe_exc}",
                )
            if obligation.is_equivalent and obligation.equivalence_proof:
                return MutationProveResult(
                    obligation.obligation_id,
                    DISPOSITION_EQUIVALENT,
                    obligation.lineno,
                    seg,
                    mutated_src_str,
                    obligation.equivalence_proof,
                )
            return MutationProveResult(
                obligation.obligation_id,
                DISPOSITION_SURVIVED,
                obligation.lineno,
                seg,
                mutated_src_str,
                "probe completed without AssertionError",
            )
        finally:
            sys.dont_write_bytecode = prev_bytecode_flag
            for mod_key in list(sys.modules):
                if mod_key not in saved_modules:
                    sys.modules.pop(mod_key, None)
            for mod_key, mod_val in saved_modules.items():
                sys.modules[mod_key] = mod_val


def _probe_m_multiwin_scorecard_1(mod: types.ModuleType) -> None:
    """Kill M-wender-learn-multiwin-scorecard-1 (O at line 1109: And -> Or in score_windows)."""
    scenario, adapters = _build_scenario(frames_per_target=1)
    events = list(scenario.events)
    first_action = events[0]["action"]
    events[0] = {
        **events[0],
        "action": replace(first_action, payload={"terminal": "TIMEOUT"}),
    }
    expected = {**scenario.expected_outcomes, first_action.action_id: "TIMEOUT"}
    fault_scenario = replace(scenario, events=tuple(events), expected_outcomes=expected)

    report = mod.run_trial(fault_scenario, seed=11, clock=mod.FakeClock(), adapters=adapters)
    card = mod.score_windows(report.rows)
    if not card.passed:
        raise AssertionError(f"expected valid fault scenario to pass, got {card.violations}")
    throughput = card.metrics["terminal_completeness"]["simulated_throughput"]
    if throughput["numerator"] != 1:
        raise AssertionError(f"expected exactly 1 successful action in throughput, got {throughput}")


def _probe_m_multiwin_scorecard_2(mod: types.ModuleType) -> None:
    """Kill M-wender-learn-multiwin-scorecard-2 (B at line 434: rgb.shape[1] -> rgb.shape[2])."""
    scenario, adapters = _build_scenario(frames_per_target=1)
    zero_width_rgb = np.zeros((2, 0, 3), dtype=np.uint8)
    zero_width_truth = np.zeros((2, 0), dtype=np.bool_)
    bad_event = {**scenario.events[0], "rgb": zero_width_rgb, "truth": zero_width_truth}
    bad_scenario = replace(scenario, events=(bad_event,) + scenario.events[1:])
    try:
        mod._validate_scenario(bad_scenario, 7, mod.FakeClock(), adapters)
    except mod.EvidenceError as exc:
        if "rgb must be nonempty uint8 HxWx3" not in str(exc):
            raise AssertionError(f"unexpected EvidenceError message: {exc}") from exc
        return
    raise AssertionError("expected EvidenceError for zero-width (2, 0, 3) rgb array")


def _probe_verifier_seal_len_compare(mod: types.ModuleType) -> None:
    """Kill B/O on len(s) < 64 in _classify_seal."""
    scenario, adapters = _build_scenario(frames_per_target=1)
    report = mws.run_trial(scenario, seed=5, clock=mws.FakeClock(), adapters=adapters)
    audit = mod.audit_episode_log(report)
    if not audit.passed:
        raise AssertionError(f"healthy report failed audit: {audit.violations}")
    altered_hex = ("0" if report.rows[0].seal[0] != "0" else "1") + report.rows[0].seal[1:]
    altered = tuple(replace(r, seal=altered_hex) for r in report.rows)
    bad_audit = mod.audit_episode_log(altered)
    if "altered_seal" not in bad_audit.violations or "truncated_seal" in bad_audit.violations:
        raise AssertionError(
            f"expected altered_seal (not truncated_seal) for 64-char altered seal, got {bad_audit.violations}"
        )


def _probe_verifier_disable_state_digest_guard(mod: types.ModuleType) -> None:
    """Kill D on raising If 'if row.frame_supported and not dropped and not quarantined:' in adapt_window_row."""
    scenario, adapters = _build_scenario(frames_per_target=1)
    report = mws.run_trial(scenario, seed=5, clock=mws.FakeClock(), adapters=adapters)
    env = mod.adapt_window_row(report.rows[0])
    if env.step != 0:
        raise AssertionError("expected step 0")
    null_state_ops = {**report.rows[0].ops, "state_digest": None}
    bad_row = replace(report.rows[0], ops=null_state_ops)
    try:
        mod.adapt_window_row(bad_row)
    except mod.TraceDeterminismError:
        return
    raise AssertionError("expected TraceDeterminismError when supported active row lacks state_digest")


def _probe_verifier_swap_call_args(mod: types.ModuleType) -> None:
    """Kill S on _compute_link_digest(idx, prev_digest, env) in build_trace_chain."""
    scenario, adapters = _build_scenario(frames_per_target=1)
    report = mws.run_trial(scenario, seed=5, clock=mws.FakeClock(), adapters=adapters)
    chain = mod.build_trace_chain(report)
    ok, viol = mod.verify_envelope_chain(chain)
    if not ok:
        raise AssertionError(f"chain verification failed: {viol}")


CATALOGUE_OBLIGATIONS: tuple[MutationObligation, ...] = (
    MutationObligation(
        obligation_id="M-wender-learn-multiwin-scorecard-1",
        module_path="wender/learn/multiwin_scorecard.py",
        module_name="wender.learn.multiwin_scorecard",
        qualified_function="score_windows",
        lineno=1109,
        operator="O",
        original_text=(
            'r.ops.get("admitted") is True and _terminal(r.terminal) in _SUCCESS\n'
            " and r.terminal == r.expected_terminal"
        ),
        structural_selector="body element 47.value.args element 0.elt",
        probe=_probe_m_multiwin_scorecard_1,
    ),
    MutationObligation(
        obligation_id="M-wender-learn-multiwin-scorecard-2",
        module_path="wender/learn/multiwin_scorecard.py",
        module_name="wender.learn.multiwin_scorecard",
        qualified_function="_validate_scenario",
        lineno=434,
        operator="B",
        original_text="1",
        structural_selector="body element 13.body element 17.test.values element 5.operand.slice",
        probe=_probe_m_multiwin_scorecard_2,
    ),
)


class TestTraceDeterminismVerifier(unittest.TestCase):
    """Acceptance gate test suite for jq3-r-r6-g1-trace-determinism-verifier."""

    def test_envelope_adaptation_and_diagnostic_wall_time_exclusion(self) -> None:
        scenario, adapters = _build_scenario(
            kinds=("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"),
            frames_per_target=2,
        )
        report = mws.run_trial(scenario, seed=19, clock=mws.FakeClock(), adapters=adapters)
        self.assertEqual(len(report.rows), 10)

        self.assertEqual(
            [f.name for f in fields(tdv.TraceEnvelope)],
            ["target", "generation", "epoch", "step", "action", "outcome", "state_digest"],
        )

        first_row = report.rows[0]
        env = tdv.adapt_window_row(first_row)
        self.assertEqual(env.target, (first_row.handle.kind, first_row.handle.canonical_id))
        self.assertEqual(env.generation, first_row.handle.generation)
        self.assertEqual(env.epoch, first_row.epoch)
        self.assertEqual(env.step, first_row.ops["event_index"])
        self.assertEqual(env.frame_seq, first_row.frame_seq)
        self.assertEqual(env.event_index, first_row.ops["event_index"])
        self.assertEqual(len(env.state_digest), 64)

        noisy_ops = copy.deepcopy(first_row.ops)
        noisy_ops["wall_time_ns"] = 987654321
        noisy_ops["wall_clock_timestamp"] = "2026-10-20T00:00:00Z"
        noisy_ops["diagnostics"] = {"host": "worker-vm", "elapsed_wall_ns": 55555}
        noisy_row = replace(first_row, ops=noisy_ops)
        noisy_env = tdv.adapt_window_row(noisy_row)
        self.assertEqual(env.canonical_bytes(), noisy_env.canonical_bytes())
        self.assertEqual(env.digest(), noisy_env.digest())
        self.assertNotIn(b"wall_time_ns", noisy_env.canonical_bytes())
        self.assertNotIn(b"wall_clock_timestamp", noisy_env.canonical_bytes())

        no_index_ops = {k: v for k, v in first_row.ops.items() if k != "event_index"}
        no_index_ops["step"] = 0
        no_index_ops["checkpoint"] = 0
        with self.assertRaises(tdv.TraceDeterminismError):
            tdv.adapt_window_row(replace(first_row, ops=no_index_ops))

    def test_seal_truncation_alteration_and_unsealed_row_rejection(self) -> None:
        scenario, adapters = _build_scenario(frames_per_target=2)
        report = mws.run_trial(scenario, seed=23, clock=mws.FakeClock(), adapters=adapters)
        healthy = tdv.audit_episode_log(report)
        self.assertTrue(healthy.passed, healthy.violations)

        unsealed_rows = tuple(replace(r, seal="") for r in report.rows)
        unsealed_audit = tdv.audit_episode_log(unsealed_rows)
        self.assertFalse(unsealed_audit.passed)
        self.assertIn("empty_seal", unsealed_audit.violations)
        self.assertIn("missing_mixed_or_invalid_seal", unsealed_audit.violations)

        truncated_rows = (replace(report.rows[0], seal=report.rows[0].seal[:32]),) + report.rows[1:]
        truncated_audit = tdv.audit_episode_log(truncated_rows)
        self.assertFalse(truncated_audit.passed)
        self.assertIn("truncated_seal", truncated_audit.violations)

        malformed_rows = (replace(report.rows[0], seal="z" * 64),) + report.rows[1:]
        malformed_audit = tdv.audit_episode_log(malformed_rows)
        self.assertFalse(malformed_audit.passed)
        self.assertIn("malformed_seal", malformed_audit.violations)

        altered_hex = ("0" if report.rows[0].seal[0] != "0" else "1") + report.rows[0].seal[1:]
        altered_rows = tuple(replace(r, seal=altered_hex) for r in report.rows)
        altered_audit = tdv.audit_episode_log(altered_rows)
        self.assertFalse(altered_audit.passed)
        self.assertIn("altered_seal", altered_audit.violations)

    def test_intact_reordered_rows_accepted_while_reordered_or_duplicated_steps_fail(self) -> None:
        scenario, adapters = _build_scenario(frames_per_target=2)
        report = mws.run_trial(scenario, seed=29, clock=mws.FakeClock(), adapters=adapters)
        baseline_audit = tdv.audit_episode_log(report)
        self.assertTrue(baseline_audit.passed)

        reversed_intact_rows = tuple(reversed(report.rows))
        reordered_list_audit = tdv.audit_episode_log(reversed_intact_rows)
        self.assertTrue(reordered_list_audit.passed, reordered_list_audit.violations)
        self.assertEqual(
            reordered_list_audit.chain.complete_trace_digest,
            baseline_audit.chain.complete_trace_digest,
        )

        r0_ops = {**report.rows[0].ops, "event_index": report.rows[2].ops["event_index"]}
        r2_ops = {**report.rows[2].ops, "event_index": report.rows[0].ops["event_index"]}
        tampered_order_rows = (
            replace(report.rows[0], ops=r0_ops),
            report.rows[1],
            replace(report.rows[2], ops=r2_ops),
            report.rows[3],
        )
        tampered_order_audit = tdv.audit_episode_log(tampered_order_rows)
        self.assertFalse(tampered_order_audit.passed)
        self.assertIn("reordered_step", tampered_order_audit.violations)

        dup_rows = report.rows + (report.rows[0],)
        dup_audit = tdv.audit_episode_log(dup_rows)
        self.assertFalse(dup_audit.passed)
        self.assertIn("duplicate_step", dup_audit.violations)

        envs = list(baseline_audit.chain.envelopes)
        ok_rev, viol_rev = tdv.verify_envelope_chain([envs[1], envs[0], envs[2], envs[3]])
        self.assertFalse(ok_rev)
        self.assertIn("reordered_step", viol_rev)

        ok_dup, viol_dup = tdv.verify_envelope_chain([envs[0], envs[0], envs[2], envs[3]])
        self.assertFalse(ok_dup)
        self.assertIn("duplicate_step", viol_dup)

    def test_foreign_observations_and_cross_window_bleed_detection(self) -> None:
        scenario, adapters = _build_scenario(frames_per_target=2)
        report = mws.run_trial(scenario, seed=31, clock=mws.FakeClock(), adapters=adapters)

        valid_obs = [obs for a in adapters.values() for obs in a.observations[:2]]
        clean_audit = tdv.audit_episode_log(report, observations=valid_obs)
        self.assertTrue(clean_audit.passed, clean_audit.violations)

        foreign_handle = sr.Handle("cdp_tab", "foreign-tab", 1)
        rgb = np.zeros((2, 3, 3), dtype=np.uint8)
        foreign_frame = sr.FrameRef.from_rgb(foreign_handle, 0, 0, rgb)
        foreign_obs = sr.Observation(
            foreign_handle, 0, 0, 1_000_000, {"target": list(_hkey(foreign_handle))}, foreign_frame
        )
        foreign_audit = tdv.audit_episode_log(report, observations=valid_obs + [foreign_obs])
        self.assertFalse(foreign_audit.passed)
        self.assertIn("foreign_observation", foreign_audit.violations)
        self.assertFalse(foreign_audit.metrics["observation_provenance"]["passed"])

        h0 = scenario.targets[0]
        h1 = scenario.targets[1]
        f0 = sr.FrameRef.from_rgb(h0, 0, 0, rgb)
        bleed_obs = sr.Observation(h0, 0, 0, 1_000_000, {"target": list(_hkey(h1))}, f0)
        perc_result = sr.PerceptionResult(h0, bleed_obs, None)
        bleed_obs_audit = tdv.audit_episode_log(report, observations=[perc_result])
        self.assertFalse(bleed_obs_audit.passed)
        self.assertIn("foreign_observation", bleed_obs_audit.violations)
        self.assertIn("cross_window_bleed", bleed_obs_audit.violations)

        shared_scenario, shared_adapters = _build_scenario(frames_per_target=2)
        shared_mem: dict = {}
        for a in shared_adapters.values():
            a.memory = shared_mem
        bleed_report = mws.run_trial(shared_scenario, seed=31, clock=mws.FakeClock(), adapters=shared_adapters)
        bleed_audit = tdv.audit_episode_log(bleed_report)
        self.assertFalse(bleed_audit.passed)
        self.assertIn("cross_window_state_bleed", bleed_audit.violations)
        self.assertFalse(bleed_audit.metrics["cross_window_bleed_audit"]["passed"])
        self.assertGreater(len(bleed_audit.bleed_events), 0)

    def test_complete_reset_evidence_and_unavailable_denominators_preserved(self) -> None:
        scenario, adapters = _build_scenario(frames_per_target=1)
        adapters[_hkey(scenario.targets[0])].fail_reset = True
        reset_fail_report = mws.run_trial(scenario, seed=37, clock=mws.FakeClock(), adapters=adapters)
        self.assertTrue(
            any(f.get("stage") == "reset" and f.get("terminal") == "ERROR" for f in reset_fail_report.faults)
        )
        reset_audit = tdv.audit_episode_log(reset_fail_report)
        self.assertFalse(reset_audit.passed)
        self.assertIn("incomplete_reset_evidence", reset_audit.violations)
        self.assertFalse(reset_audit.reset_evidence["complete"])
        self.assertFalse(reset_audit.metrics["reset_completeness"]["passed"])

        healthy_scenario, healthy_adapters = _build_scenario(frames_per_target=1)
        healthy_report = mws.run_trial(
            healthy_scenario, seed=37, clock=mws.FakeClock(), adapters=healthy_adapters
        )
        partial_reset_map = {_hkey(healthy_scenario.targets[0]): {"reset_count": 3, "reset_ok": True}}
        partial_audit = tdv.audit_episode_log(healthy_report, reset_evidence=partial_reset_map)
        self.assertFalse(partial_audit.passed)
        self.assertIn("incomplete_reset_evidence", partial_audit.violations)

        full_reset_map = {
            _hkey(h): {"reset_count": len(healthy_adapters[_hkey(h)].seeds), "seeds": healthy_adapters[_hkey(h)].seeds, "reset_ok": True}
            for h in healthy_scenario.targets
        }
        full_audit = tdv.audit_episode_log(healthy_report, reset_evidence=full_reset_map)
        self.assertTrue(full_audit.passed, full_audit.violations)

        empty_audit = tdv.audit_episode_log(())
        self.assertFalse(empty_audit.passed)
        for metric_name, metric_val in empty_audit.metrics.items():
            with self.subTest(metric=metric_name):
                self.assertIsNone(metric_val["numerator"])
                self.assertIsNone(metric_val["denominator"])
                self.assertFalse(metric_val["passed"])

        no_ops_scenario, no_ops_adapters = _build_scenario(frames_per_target=1)
        for a in no_ops_adapters.values():
            a.empty_ops = True
        no_ops_report = mws.run_trial(no_ops_scenario, seed=41, clock=mws.FakeClock(), adapters=no_ops_adapters)
        no_ops_audit = tdv.audit_episode_log(no_ops_report)
        self.assertFalse(no_ops_audit.passed)
        self.assertIsNone(no_ops_audit.metrics["operation_totals"]["denominator"])
        self.assertIsNone(no_ops_audit.metrics["operation_totals"]["numerator"])
        self.assertFalse(no_ops_audit.metrics["operation_totals"]["passed"])

    def test_replay_verification_checkpoints_resets_versions_and_lazy_deliverable_block(self) -> None:
        draft = _make_replay_draft()
        driver = _OfflineReplayDriver()

        result = tdv.verify_replay_trace(
            draft,
            driver,
            "verifier_r6",
            runs=2,
            reference_draft=draft,
            known_versions={1},
        )
        self.assertEqual(driver.reset_count, 2)
        self.assertEqual(result["admitted"]["evidence"]["version"], 2)
        self.assertEqual(result["admitted"]["evidence"]["runs"], 2)
        self.assertEqual(len(result["chain"].links), 2)
        self.assertTrue(result["reset_evidence"]["complete"])

        class _ResetlessDriver:
            def observe(self):
                return driver.observe()

            def act(self, step):
                return driver.act(step)

        with self.assertRaises(tdv.TraceDeterminismError):
            tdv.verify_replay_trace(draft, _ResetlessDriver(), "verifier_r6", runs=2)

        dup_cp_draft = copy.deepcopy(draft)
        dup_cp_draft["checkpoints"].append(copy.deepcopy(draft["checkpoints"][0]))
        with self.assertRaises(tdv.TraceDeterminismError) as ctx_dup:
            tdv.verify_replay_trace(dup_cp_draft, _OfflineReplayDriver(), "verifier_r6", runs=2)
        self.assertIn("duplicate_checkpoint", str(ctx_dup.exception))

        altered_cp_draft = copy.deepcopy(draft)
        altered_cp_draft["checkpoints"][1] = {
            "after_step": 1,
            "expect": {"node_absent": {"role": "dialog", "name": "Error"}},
        }
        with self.assertRaises(tdv.TraceDeterminismError) as ctx_alt:
            tdv.verify_replay_trace(
                altered_cp_draft,
                _OfflineReplayDriver(),
                "verifier_r6",
                runs=2,
                reference_draft=draft,
            )
        self.assertIn("altered_checkpoint", str(ctx_alt.exception))

        reordered_steps_draft = copy.deepcopy(draft)
        reordered_steps_draft["steps"] = list(reversed(draft["steps"]))
        with self.assertRaises(tdv.TraceDeterminismError) as ctx_reord:
            tdv.verify_replay_trace(
                reordered_steps_draft,
                _OfflineReplayDriver(),
                "verifier_r6",
                runs=2,
                reference_draft=draft,
            )
        self.assertIn("reordered_step", str(ctx_reord.exception))

        unknown_ver_draft = copy.deepcopy(draft)
        unknown_ver_draft["evidence"]["version"] = 99
        with self.assertRaises(tdv.TraceDeterminismError) as ctx_ver:
            tdv.verify_replay_trace(
                unknown_ver_draft,
                _OfflineReplayDriver(),
                "verifier_r6",
                runs=2,
                known_versions={1, 2},
            )
        self.assertIn("unknown_prior_version", str(ctx_ver.exception))

        admitted_dict = copy.deepcopy(result["admitted"])
        admitted_dict["evidence"]["sha256"] = admitted_dict["evidence"]["sha256"][:20]
        with self.assertRaises(tdv.TraceDeterminismError) as ctx_trunc:
            tdv.verify_admitted_skill_record(json.dumps(admitted_dict))
        self.assertIn("truncated_seal", str(ctx_trunc.exception))

        with self.assertRaises(tdv.PrerequisiteAbsentError) as ctx_abs:
            tdv.load_sibling_deliverable("wender.learn.unmerged_future_owner_deliverable")
        self.assertIn("AUSENTE", str(ctx_abs.exception))

    def test_catalogue_ast_mutation_obligations_and_harness_dispositions(self) -> None:
        modules_before = dict(sys.modules)

        for obligation in CATALOGUE_OBLIGATIONS:
            with self.subTest(obligation=obligation.obligation_id):
                res = InMemoryAstProveHarness.prove(obligation)
                self.assertEqual(
                    res.disposition,
                    DISPOSITION_KILLED,
                    f"{obligation.obligation_id} was not killed: {res}",
                )

        verifier_obligations = (
            MutationObligation(
                obligation_id="M-verifier-seal-len-B",
                module_path="wender/learn/trace_determinism_verifier.py",
                module_name="wender.learn.trace_determinism_verifier",
                qualified_function="_classify_seal",
                lineno=645,
                operator="B",
                original_text="len(s) < 64",
                probe=_probe_verifier_seal_len_compare,
            ),
            MutationObligation(
                obligation_id="M-verifier-state-digest-D",
                module_path="wender/learn/trace_determinism_verifier.py",
                module_name="wender.learn.trace_determinism_verifier",
                qualified_function="adapt_window_row",
                lineno=350,
                operator="D",
                original_text='if row.frame_supported and not dropped and not quarantined:\n            raise TraceDeterminismError("supported active row lacks state_digest")',
                probe=_probe_verifier_disable_state_digest_guard,
            ),
            MutationObligation(
                obligation_id="M-verifier-link-digest-S",
                module_path="wender/learn/trace_determinism_verifier.py",
                module_name="wender.learn.trace_determinism_verifier",
                qualified_function="build_trace_chain",
                lineno=512,
                operator="S",
                original_text="_compute_link_digest(idx, prev_digest, env)",
                probe=_probe_verifier_swap_call_args,
            ),
        )
        for v_ob in verifier_obligations:
            with self.subTest(obligation=v_ob.obligation_id):
                res = InMemoryAstProveHarness.prove(v_ob)
                self.assertEqual(res.disposition, DISPOSITION_KILLED, res)

        failed_ctrl = replace(
            CATALOGUE_OBLIGATIONS[0],
            obligation_id="disp-failed-control",
            probe=lambda _mod: (_ for _ in ()).throw(AssertionError("control fails on real code")),
        )
        self.assertEqual(
            InMemoryAstProveHarness.prove(failed_ctrl).disposition,
            DISPOSITION_FAILED_CONTROL,
        )

        missing_anchor = replace(
            CATALOGUE_OBLIGATIONS[0],
            obligation_id="disp-missing-anchor",
            lineno=9999,
        )
        self.assertEqual(
            InMemoryAstProveHarness.prove(missing_anchor).disposition,
            DISPOSITION_MISSING_ANCHOR,
        )

        bad_b_kind = MutationObligation(
            obligation_id="disp-b-mismatch",
            module_path="wender/learn/multiwin_scorecard.py",
            module_name="wender.learn.multiwin_scorecard",
            qualified_function="_validate_scenario",
            lineno=434,
            operator="B",
            original_text="rgb.shape[2] != 3",
            probe=lambda _mod: None,
        )
        self.assertEqual(
            InMemoryAstProveHarness.prove(bad_b_kind).disposition,
            DISPOSITION_MISSING_ANCHOR,
        )

        survivor = replace(
            CATALOGUE_OBLIGATIONS[1],
            obligation_id="disp-survivor",
            probe=lambda _mod: None,
        )
        self.assertEqual(
            InMemoryAstProveHarness.prove(survivor).disposition,
            DISPOSITION_SURVIVED,
        )
        equivalent = replace(
            survivor,
            obligation_id="disp-equivalent",
            is_equivalent=True,
            equivalence_proof="no-op probe is invariant under shape index increment",
        )
        self.assertEqual(
            InMemoryAstProveHarness.prove(equivalent).disposition,
            DISPOSITION_EQUIVALENT,
        )

        runtime_err = replace(
            CATALOGUE_OBLIGATIONS[1],
            obligation_id="disp-runtime-err",
            probe=lambda mod: (_ for _ in ()).throw(KeyError("boom"))
            if mod.__name__ != "wender.learn.multiwin_scorecard"
            else None,
        )
        self.assertEqual(
            InMemoryAstProveHarness.prove(runtime_err).disposition,
            DISPOSITION_COMPILE_OR_IMPORT_ERROR,
        )

        self.assertEqual(set(sys.modules), set(modules_before))

    def test_seeded_random_property_loops_with_validity_preserving_shrinking(self) -> None:
        seed_domain_1 = 0x6A09E667
        rng1 = random.Random(seed_domain_1)
        valid_cases_1 = 0
        all_kinds = list(mws.KINDS)

        for case_idx in range(PROPERTY_CASES_TARGET):
            k_count = rng1.randint(2, 4)
            chosen_kinds = tuple(rng1.sample(all_kinds, k_count))
            frames_per = rng1.randint(1, 3)
            gen = rng1.randint(1, 50)
            scenario, adapters = _build_scenario(chosen_kinds, frames_per, generation=gen)
            trial_seed = rng1.randint(1, 10_000)
            report = mws.run_trial(scenario, seed=trial_seed, clock=mws.FakeClock(), adapters=adapters)
            audit = tdv.audit_episode_log(report)
            if not audit.passed:
                minimal = (chosen_kinds, frames_per, gen, trial_seed)
                shrinks = 0
                while shrinks < MAX_SHRINK_ATTEMPTS and (len(minimal[0]) > 2 or minimal[1] > 1):
                    shrinks += 1
                    cand_kinds = minimal[0][: max(2, len(minimal[0]) - 1)]
                    cand_frames = max(1, minimal[1] - 1)
                    sc_c, ad_c = _build_scenario(cand_kinds, cand_frames, generation=minimal[2])
                    rep_c = mws.run_trial(sc_c, seed=minimal[3], clock=mws.FakeClock(), adapters=ad_c)
                    if not tdv.audit_episode_log(rep_c).passed:
                        minimal = (cand_kinds, cand_frames, minimal[2], minimal[3])
                    else:
                        break
                self.fail(
                    f"Domain 1 failure at seed={seed_domain_1:#x} case={case_idx} minimal_fixture={minimal}"
                )

            shuffled_rows = list(report.rows)
            rng1.shuffle(shuffled_rows)
            shuffled_audit = tdv.audit_episode_log(shuffled_rows)
            self.assertTrue(
                shuffled_audit.passed,
                f"seed={seed_domain_1:#x} case={case_idx} shuffled_rows failed",
            )
            self.assertEqual(
                shuffled_audit.chain.complete_trace_digest,
                audit.chain.complete_trace_digest,
            )
            valid_cases_1 += 1

        self.assertEqual(valid_cases_1, PROPERTY_CASES_TARGET)

        seed_domain_2 = 0xBB67AE85
        rng2 = random.Random(seed_domain_2)
        valid_cases_2 = 0

        for case_idx in range(PROPERTY_CASES_TARGET):
            base_draft = _make_replay_draft()
            prior_ver = rng2.randint(1, 20)
            runs = rng2.randint(2, 4)
            base_draft["evidence"]["version"] = prior_ver
            base_draft["id"] = f"skill-{case_idx}-{prior_ver}"
            drv = _OfflineReplayDriver()
            res = tdv.verify_replay_trace(
                base_draft,
                drv,
                verified_by=f"verifier-{case_idx}",
                runs=runs,
                reference_draft=base_draft,
                known_versions={prior_ver},
            )
            self.assertEqual(res["admitted"]["evidence"]["version"], prior_ver + 1)
            self.assertEqual(drv.reset_count, runs)
            valid_cases_2 += 1

        self.assertEqual(valid_cases_2, PROPERTY_CASES_TARGET)

    def test_shared_state_concurrency_32_schedules_bounded_threads_zero_leaks(self) -> None:
        baseline_threads = {t.ident for t in threading.enumerate()}
        rng = random.Random(0x3C6EF372)
        completed_schedules = 0

        for sched_idx in range(CONCURRENCY_SCHEDULES_TARGET):
            worker_count = MAX_THREADS
            reg = sr.SessionRegistry(clock=mws.FakeClock(1000))
            barrier = threading.Barrier(worker_count, timeout=WAIT_TIMEOUT_S)
            start_event = threading.Event()
            order = list(range(worker_count))
            rng.shuffle(order)
            turn_events = [threading.Event() for _ in range(worker_count)]
            turn_events[0].set()

            results: dict[int, tuple[sr.Observation, str]] = {}
            errors: list[BaseException] = []

            def _worker(slot: int, rank: int) -> None:
                try:
                    barrier.wait(timeout=WAIT_TIMEOUT_S)
                    if not start_event.wait(timeout=WAIT_TIMEOUT_S):
                        raise TimeoutError("start_event wait timed out")
                    if not turn_events[rank].wait(timeout=WAIT_TIMEOUT_S):
                        raise TimeoutError("turn_event wait timed out")

                    h = reg.register("cdp_tab", f"tab-{slot}", object(), owned=True)
                    rgb = np.full((2, 2, 3), (slot + 1) * 17, dtype=np.uint8)
                    fref = reg.admit_frame(h, 0, rgb)
                    obs = reg.admit_observation(
                        h,
                        fref,
                        state={"slot": slot, "target": [h.kind, h.canonical_id, h.generation]},
                        dpr_ppm=1_000_000,
                    )
                    env = tdv.TraceEnvelope(
                        target=(h.kind, h.canonical_id),
                        generation=h.generation,
                        epoch=obs.epoch,
                        step=rank,
                        action={"action_id": f"act-{slot}"},
                        outcome={"frame_seq": obs.frame_seq, "event_index": rank, "slot": slot},
                        state_digest=hashlib.sha256(mws._json(obs.state)).hexdigest(),
                    )
                    results[rank] = (obs, env.digest())
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    if rank + 1 < worker_count:
                        turn_events[rank + 1].set()

            threads = [
                threading.Thread(
                    target=_worker,
                    args=(order[rank], rank),
                    name=f"sched-{sched_idx}-w-{rank}",
                )
                for rank in range(worker_count)
            ]
            self.assertLessEqual(len(threads), MAX_THREADS)
            for t in threads:
                t.start()
            start_event.set()
            for t in threads:
                t.join(timeout=WAIT_TIMEOUT_S)
                self.assertFalse(t.is_alive(), f"leaked thread {t.name} in schedule {sched_idx}")

            self.assertEqual(errors, [], f"schedule {sched_idx} raised {errors}")
            self.assertEqual(len(results), worker_count)
            self.assertEqual(
                [results[r][0].handle.generation for r in range(worker_count)],
                [1, 2, 3, 4],
            )
            completed_schedules += 1

        self.assertEqual(completed_schedules, CONCURRENCY_SCHEDULES_TARGET)
        after_threads = {t.ident for t in threading.enumerate()}
        self.assertEqual(after_threads - baseline_threads, set())

    def test_ast_operator_matrix_and_timeout_disposition(self) -> None:
        # 1. Verify O on all 10 Compare operators, BoolOp (And/Or), and BinOp (Add/Sub, Mult/FloorDiv)
        compare_cases = (
            ("x < y", "x > y"),
            ("x > y", "x < y"),
            ("x <= y", "x >= y"),
            ("x >= y", "x <= y"),
            ("x == y", "x != y"),
            ("x != y", "x == y"),
            ("x is y", "x is not y"),
            ("x is not y", "x is y"),
            ("x in y", "x not in y"),
            ("x not in y", "x in y"),
            ("x and y", "x or y"),
            ("x or y", "x and y"),
            ("x + y", "x - y"),
            ("x - y", "x + y"),
            ("x * y", "x // y"),
            ("x // y", "x * y"),
        )
        for src_expr, expected_mut in compare_cases:
            with self.subTest(op="O", expr=src_expr):
                node = ast.parse(src_expr, mode="eval").body
                mut = _apply_ast_operator("O", node)
                self.assertEqual(ast.unparse(mut), expected_mut)

        # 2. Verify B on Compare (Lt<->LtE, Gt<->GtE) and exact integer Constant (+1), rejecting bool
        boundary_cases = (
            ("x < y", True, "x <= y"),
            ("x <= y", True, "x < y"),
            ("x > y", True, "x >= y"),
            ("x >= y", True, "x > y"),
            ("1", False, "2"),
            ("0", False, "1"),
        )
        for src_expr, is_cmp, expected_mut in boundary_cases:
            with self.subTest(op="B", expr=src_expr):
                node = ast.parse(src_expr, mode="eval").body
                mut = _apply_ast_operator("B", node, expect_comparison_for_b=is_cmp)
                self.assertEqual(ast.unparse(mut), expected_mut)

        with self.assertRaises(MutationAnchorError):
            _apply_ast_operator("B", ast.parse("True", mode="eval").body, expect_comparison_for_b=False)
        with self.assertRaises(MutationAnchorError):
            _apply_ast_operator("B", ast.parse("1", mode="eval").body, expect_comparison_for_b=True)
        with self.assertRaises(MutationAnchorError):
            _apply_ast_operator("B", ast.parse("x < y", mode="eval").body, expect_comparison_for_b=False)

        # 3. Verify D on raising If (test -> False) and Assert (test -> True)
        if_node = ast.parse('if cond:\n    raise ValueError("bad")').body[0]
        mut_if = _apply_ast_operator("D", if_node)
        self.assertIsInstance(mut_if.test, ast.Constant)
        self.assertIs(mut_if.test.value, False)

        assert_node = ast.parse('assert cond, "msg"').body[0]
        mut_assert = _apply_ast_operator("D", assert_node)
        self.assertIsInstance(mut_assert.test, ast.Constant)
        self.assertIs(mut_assert.test.value, True)

        # 4. Verify S swaps the first two distinct non-starred positional Call arguments
        call_node = ast.parse("fn(*starred, a, a, b, c)").body[0].value
        mut_call = _apply_ast_operator("S", call_node)
        self.assertEqual(ast.unparse(mut_call), "fn(*starred, b, a, a, c)")

        # 5. Verify TIMEOUT disposition is separate from KILLED
        timeout_ev = threading.Event()
        try:
            timeout_ob = replace(
                CATALOGUE_OBLIGATIONS[1],
                obligation_id="disp-timeout",
                timeout_s=0.05,
                probe=lambda mod: timeout_ev.wait(0.5)
                if mod.__name__ != "wender.learn.multiwin_scorecard"
                else None,
            )
            res_timeout = InMemoryAstProveHarness.prove(timeout_ob)
            self.assertEqual(res_timeout.disposition, DISPOSITION_TIMEOUT)
        finally:
            timeout_ev.set()


if __name__ == "__main__":
    unittest.main()
