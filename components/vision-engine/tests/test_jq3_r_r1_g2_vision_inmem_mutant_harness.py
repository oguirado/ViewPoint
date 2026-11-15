"""In-memory AST mutation harness and receipt validation tests (G-R1-2).

Owns its private in-memory AST mutation harness, synthetic RGB fixtures, and
module/context cleanup (INV-R1-3). Exercises valid postpass/veto/detector
controls and malformed receipts without importing any sibling test harness.
"""
from __future__ import annotations

import ast
from contextvars import copy_context
import copy
from dataclasses import dataclass
import importlib
import importlib.machinery
from pathlib import Path
import random
import sys
import threading
import types
from typing import Callable
import unittest
from unittest import mock

import numpy as np

from eval.v2eval import postpass as real_postpass
from eval.v2eval.jq3_mutation_receipts import (
    CANONICAL_OUTCOMES,
    DuplicateMutationSiteError,
    InvalidKillReceiptError,
    MutationReceiptError,
    OUTCOME_EQUIVALENT,
    OUTCOME_INFRASTRUCTURE,
    OUTCOME_KILLED,
    OUTCOME_MISSING_ANCHOR,
    OUTCOME_SURVIVED,
    UnboundDigestError,
    VisionMutationReceipt,
    classify_trial_outcome,
    reduce_vision_receipts,
    source_digest_for_path,
    synthetic_rgb_digest,
)
from eval.v2proto_flicker import FlickerTracker
from eval.v2x import fl_blinkprior, fl_v3, per_window_detector as real_pwd
from visioncore import desc
from visioncore.grid import Grid


REPO_ROOT = Path(__file__).resolve().parents[1]
POSTPASS_REL_PATH = "eval/v2eval/postpass.py"
DETECTOR_REL_PATH = "eval/v2x/per_window_detector.py"
REPLAY_SEED = 0xE58C6B85


def _make_uniform_rgb(value: int, width: int = 16, height: int = 16) -> np.ndarray:
    arr = np.full((height, width, 3), int(value) & 0xFF, dtype=np.uint8)
    arr.flags.writeable = False
    return arr


def _make_baseline_tiles(rgb: np.ndarray, cls: int | np.ndarray = 0) -> np.ndarray:
    grid = Grid(rgb.shape[1], rgb.shape[0])
    tiles, _ = desc.describe_padded(grid.pad(rgb), grid, lazy_xh=True)
    tiles = tiles.copy()
    tiles["cls"] = cls
    tiles["mv_dx"] = 0
    tiles["mv_dy"] = 0
    tiles["xh64"] = 0
    return tiles


def _make_canvas_noise_rgb(seed: int) -> np.ndarray:
    x = np.arange(16, dtype=np.uint32)[None, :]
    y = np.arange(16, dtype=np.uint32)[:, None]
    z = (x * np.uint32(374761393)) ^ (y * np.uint32(668265263)) ^ np.uint32(seed)
    z = (z ^ (z >> np.uint32(13))) * np.uint32(1274126177)
    z ^= z >> np.uint32(16)
    grey = (z & np.uint32(31)).astype(np.uint8)
    bands = ((x // 8) % 3).astype(np.uint8) + ((y // 13) % 3).astype(np.uint8)
    np.subtract(bands, 3, out=bands, where=bands >= 3)
    grey += bands * np.uint8(23)
    grey += np.uint8(45)
    rgb = np.empty((16, 16, 3), dtype=np.uint8)
    rgb[:, :, 0] = grey
    rgb[:, :, 1] = grey + np.uint8(12)
    rgb[:, :, 2] = grey + np.uint8(24)
    rgb.flags.writeable = False
    return rgb


def _make_f6_scroll_frames() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build a 3-frame 64x16 uint8 RGB sequence exercising old-history GM VETO_F6 at line 316."""
    rng = np.random.default_rng(7)
    base_pat = rng.integers(10, 240, size=(16, 64), dtype=np.uint8)
    f1 = np.repeat(base_pat[:, :, None], 3, axis=2)

    f2 = np.zeros_like(f1)
    f2[:, 4:64, :] = f1[:, 0:60, :]
    f2[:, 0:4, :] = 120

    f0 = f2.copy()
    f0[0, 0, :] = 121

    for arr in (f0, f1, f2):
        arr.flags.writeable = False
    return f0, f1, f2


def _make_chain_veto_fixture() -> dict[str, object]:
    """Build chain/veto baseline & claim mappings with cls, mv_dx, mv_dy, mask and FlickerTracker(4)."""
    baseline = {
        "cls": np.array([4, 4, 2, 0], dtype=np.int64),
        "mv_dx": np.array([0, 0, 0, 0], dtype=np.int64),
        "mv_dy": np.array([0, 0, 0, 0], dtype=np.int64),
    }
    tracker = FlickerTracker(4)
    hashes = np.array([0x1111, 0x2222, 0x3333, 0x4444], dtype=np.uint64)
    states, _ = tracker.update(hashes)
    fl_claim = {
        "mask": states == 4,
        "cls": 4,
        "mv_dx": np.zeros(4, dtype=np.int64),
        "mv_dy": np.zeros(4, dtype=np.int64),
    }
    gm_claim = {
        "mask": np.array([False, False, True, False], dtype=np.bool_),
        "cls": 3,
        "mv_dx": np.array([0, 0, 5, 0], dtype=np.int64),
        "mv_dy": np.array([0, 0, 0, 0], dtype=np.int64),
    }
    veto = np.array([True, True, False, False], dtype=np.bool_)
    return {
        "baseline": baseline,
        "tracker": tracker,
        "fl_claim": fl_claim,
        "gm_claim": gm_claim,
        "veto": veto,
    }


@dataclass(frozen=True)
class InMemoryMutantSpec:
    """Specification of an AST mutation site and its behavioral assertion."""

    mutant_id: str
    module_path: str
    package_name: str
    real_module_name: str
    category: str
    qualified_ast_scope: str
    containing_expression: str
    original_expression: str
    structural_selector: str
    transformation: str
    expected_lineno: int | None
    mutator: Callable[[ast.AST], ast.AST]
    frames: tuple[np.ndarray, ...]
    assertion: Callable[[types.ModuleType, tuple[np.ndarray, ...]], None]
    is_equivalent: bool = False
    equivalent_justification: str | None = None


def _find_qualified_scope(tree: ast.Module, qualified_scope: str) -> list[ast.AST]:
    """Resolve a dotted qualified AST scope (e.g., 'BaselineFlickerVeto.update') without ordinals."""
    parts = [p for p in qualified_scope.split(".") if p]
    if not parts:
        return []

    def _search(body: list[ast.stmt], remaining: list[str]) -> list[ast.AST]:
        head = remaining[0]
        rest = remaining[1:]
        matches: list[ast.AST] = []
        for stmt in body:
            if isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == head:
                if not rest:
                    matches.append(stmt)
                else:
                    matches.extend(_search(stmt.body, rest))
        return matches

    return _search(tree.body, parts)


def _iter_scope_nodes(scope_node: ast.AST) -> list[ast.AST]:
    """Yield AST nodes inside scope_node without descending into nested class/function scopes."""
    out: list[ast.AST] = []

    def _walk(node: ast.AST, is_root: bool = False) -> None:
        if not is_root and isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            return
        out.append(node)
        for child in ast.iter_child_nodes(node):
            _walk(child, is_root=False)

    _walk(scope_node, is_root=True)
    return out


def _resolve_structural_selector(root_expr: ast.AST, selector: str) -> list[ast.AST]:
    """Navigate from a unique containing AST node to target subnode(s) via a structural path.

    Supported step forms separated by '/':
    - 'self' (the root containing expression itself)
    - '<field_name>' (e.g., 'test', 'func', 'value', 'left', 'right')
    - '<field_name>[<index>]' (e.g., 'values[1]', 'comparators[0]', 'ops[0]', 'args[0]')
    - Optional ':NodeType' assertion suffix on any step (e.g. 'values[2]:Compare', 'self:BoolOp')
    """
    steps = [s.strip() for s in selector.split("/") if s.strip()]
    if not steps:
        return []

    current: list[ast.AST] = [root_expr]
    for step in steps:
        if ":" in step:
            nav_part, type_name = step.split(":", 1)
        else:
            nav_part, type_name = step, None

        next_nodes: list[ast.AST] = []
        for node in current:
            if nav_part == "self":
                cand = [node]
            elif "[" in nav_part and nav_part.endswith("]"):
                field_name, idx_str = nav_part[:-1].split("[", 1)
                if not idx_str.isdigit():
                    return []
                idx = int(idx_str)
                seq_val = getattr(node, field_name, None)
                if isinstance(seq_val, list) and 0 <= idx < len(seq_val):
                    item = seq_val[idx]
                    cand = [item] if isinstance(item, ast.AST) else []
                else:
                    cand = []
            else:
                val = getattr(node, nav_part, None)
                cand = [val] if isinstance(val, ast.AST) else []

            for c in cand:
                if type_name is None or type(c).__name__ == type_name:
                    next_nodes.append(c)
        current = next_nodes
        if not current:
            return []
    return current


def _replace_ast_node_in_place(tree: ast.AST, target: ast.AST, replacement: ast.AST) -> bool:
    """Replace the exact object identity `target` inside `tree` with `replacement`."""
    for parent in ast.walk(tree):
        for field_name, field_val in ast.iter_fields(parent):
            if field_val is target:
                setattr(parent, field_name, replacement)
                return True
            if isinstance(field_val, list):
                for idx, elem in enumerate(field_val):
                    if elem is target:
                        field_val[idx] = replacement
                        return True
    return False


class _PrivateInMemoryHarness:
    """Private in-memory AST mutation execution harness owned exclusively by this test module."""

    _trial_lock = threading.Lock()
    _counter = 0
    _lock = threading.Lock()

    @classmethod
    def _next_uid(cls) -> int:
        with cls._lock:
            cls._counter += 1
            return cls._counter

    @classmethod
    def run_spec(cls, spec: InMemoryMutantSpec) -> VisionMutationReceipt:
        # sys.modules and dont_write_bytecode belong to the process, not a thread.
        with cls._trial_lock:
            return cls._run_spec_locked(spec)

    @classmethod
    def _run_spec_locked(cls, spec: InMemoryMutantSpec) -> VisionMutationReceipt:
        source_path = REPO_ROOT / spec.module_path
        raw_bytes = source_path.read_bytes()
        source_digest = source_digest_for_path(source_path)
        rgb_digest = synthetic_rgb_digest(spec.frames)

        # Snapshot bytecode flag, module table, and OPS counter before positive control & mutant run
        prev_dont_write = sys.dont_write_bytecode
        modules_before = dict(sys.modules)
        ops_before = fl_blinkprior.OPS
        pyc_before = set(REPO_ROOT.rglob("*.pyc"))

        private_mod_name = (
            f"{spec.package_name}._inmem_mutant_{spec.mutant_id.replace('-', '_')}_{cls._next_uid()}"
        )

        positive_control_passed = False
        anchor_matched = False
        matched_lineno = 0
        mutated_expr_str = ""
        compile_or_import_exc: BaseException | None = None
        mutant_exc: BaseException | None = None
        failure_detail: str | None = None

        try:
            sys.dont_write_bytecode = True

            # 1. Run identical assertion on the real module first (INV-R1-1)
            real_mod = importlib.import_module(spec.real_module_name)
            try:
                copy_context().run(
                    spec.assertion,
                    real_mod,
                    tuple(f.copy() for f in spec.frames),
                )
                positive_control_passed = True
            except BaseException as exc:
                positive_control_passed = False
                failure_detail = f"positive_control_failed:{type(exc).__name__}:{exc}"

            # 2. Resolve AST site by qualified scope, unique containing expression, and structural selector
            tree = ast.parse(raw_bytes.decode("utf-8"), filename=spec.module_path)
            scope_matches = _find_qualified_scope(tree, spec.qualified_ast_scope)
            if len(scope_matches) == 1:
                scope_node = scope_matches[0]
                containing_matches = [
                    n
                    for n in _iter_scope_nodes(scope_node)
                    if ast.unparse(n) == spec.containing_expression
                ]
                if len(containing_matches) == 1:
                    containing_node = containing_matches[0]
                    target_matches = _resolve_structural_selector(
                        containing_node, spec.structural_selector
                    )
                    if (
                        len(target_matches) == 1
                        and ast.unparse(target_matches[0]) == spec.original_expression
                        and (
                            spec.expected_lineno is None
                            or getattr(target_matches[0], "lineno", None) == spec.expected_lineno
                        )
                    ):
                        anchor_matched = True
                        target_node = target_matches[0]
                        matched_lineno = int(getattr(target_node, "lineno", 0) or 0)

                        # Mutate AST in memory
                        replacement = spec.mutator(copy.deepcopy(target_node))
                        ast.copy_location(replacement, target_node)
                        replaced = _replace_ast_node_in_place(tree, target_node, replacement)
                        if not replaced:
                            anchor_matched = False
                        else:
                            ast.fix_missing_locations(tree)
                            mutated_expr_str = ast.unparse(replacement)

            # 3. Compile and execute mutated AST only when positive control and anchor succeeded
            if positive_control_passed and anchor_matched:
                try:
                    code_obj = compile(
                        tree,
                        filename=f"<inmem-mutant:{spec.mutant_id}:{spec.module_path}>",
                        mode="exec",
                        dont_inherit=True,
                    )
                    private_mod = types.ModuleType(private_mod_name)
                    private_mod.__file__ = f"<inmem:{spec.module_path}>"
                    private_mod.__package__ = spec.package_name
                    private_mod.__loader__ = None
                    private_mod.__spec__ = importlib.machinery.ModuleSpec(
                        private_mod_name, None, origin=private_mod.__file__
                    )
                    sys.modules[private_mod_name] = private_mod
                    exec(code_obj, private_mod.__dict__)
                except BaseException as exc:
                    compile_or_import_exc = exc
                    failure_detail = f"compile_or_import_error:{type(exc).__name__}:{exc}"

                if compile_or_import_exc is None:
                    try:
                        copy_context().run(
                            spec.assertion,
                            private_mod,
                            tuple(f.copy() for f in spec.frames),
                        )
                    except BaseException as exc:
                        mutant_exc = exc
                        failure_detail = f"mutant_raised:{type(exc).__name__}:{exc}"
            elif not anchor_matched and failure_detail is None:
                failure_detail = "missing_or_nonunique_ast_anchor"

            # Verify synthetic RGB digest invariance after trial
            if synthetic_rgb_digest(spec.frames) != rgb_digest:
                raise UnboundDigestError("synthetic RGB frames were mutated during trial")

            outcome = classify_trial_outcome(
                positive_control_passed=positive_control_passed,
                anchor_matched=anchor_matched,
                mutant_exception=mutant_exc,
                is_equivalent=spec.is_equivalent,
                compile_or_import_error=compile_or_import_exc,
            )

            return VisionMutationReceipt(
                mutant_id=spec.mutant_id,
                module_path=spec.module_path,
                category=spec.category,
                source_digest=source_digest,
                qualified_ast_scope=spec.qualified_ast_scope,
                containing_expression=spec.containing_expression,
                original_expression=spec.original_expression,
                structural_selector=spec.structural_selector,
                transformation=spec.transformation,
                positive_control_passed=positive_control_passed,
                synthetic_rgb_digest=rgb_digest,
                outcome=outcome,
                anchor_matched=anchor_matched,
                mutated_expression=mutated_expr_str,
                lineno=matched_lineno,
                equivalent_justification=spec.equivalent_justification,
                failure_detail=failure_detail,
            )
        finally:
            # Restore sys.modules, OPS counter, and bytecode flag (INV-R1-3)
            sys.modules.pop(private_mod_name, None)
            for mod_key in list(sys.modules):
                if mod_key not in modules_before:
                    sys.modules.pop(mod_key, None)
            for mod_key, mod_val in modules_before.items():
                sys.modules[mod_key] = mod_val
            fl_blinkprior._ops.set(ops_before)
            sys.dont_write_bytecode = prev_dont_write
            pyc_after = set(REPO_ROOT.rglob("*.pyc"))
            if pyc_after != pyc_before:
                for extra_pyc in pyc_after - pyc_before:
                    extra_pyc.unlink(missing_ok=True)
                raise AssertionError("in-memory mutant harness leaked bytecode files to disk")


# --- Behavioral Assertions Built from Chain/Veto and Detector Context Fixtures ---


def _assert_postpass_1_frame_id_guard(
    mod: types.ModuleType, frames: tuple[np.ndarray, ...]
) -> None:
    """Assert BaselineFlickerVeto.update accepts frame_id=0 and rejects bool, non-int, and negative."""
    rgb = frames[0]
    grid = Grid(rgb.shape[1], rgb.shape[0])
    tiles, _ = desc.describe_padded(grid.pad(rgb), grid)
    veto = mod.BaselineFlickerVeto(grid)
    try:
        dec0 = veto.update(tiles, rgb, frame_id=0)
    except ValueError as exc:
        raise AssertionError(f"frame_id=0 must be accepted without ValueError: {exc}") from exc
    assert isinstance(dec0.mask, np.ndarray) and dec0.mask.shape == (grid.N,)
    assert veto.last_frame_id == 0

    for bad_id in (True, np.bool_(False), -1, 1.5, "0"):
        raised = False
        try:
            veto.update(tiles, rgb, frame_id=bad_id)
        except ValueError as exc:
            if "frame_id must be a nonnegative integer" in str(exc):
                raised = True
            else:
                raise AssertionError(f"unexpected ValueError message: {exc}") from exc
        if not raised:
            raise AssertionError(f"BaselineFlickerVeto.update accepted invalid frame_id={bad_id!r}")


def _assert_postpass_2_class_four_old_history_gm_augmentation(
    mod: types.ModuleType, frames: tuple[np.ndarray, ...]
) -> None:
    """Assert BaselineFlickerVeto.update at line 316 augments class-4 tiles with VETO_F6 when old is present."""
    f0, f1, f2 = frames
    grid = Grid(f0.shape[1], f0.shape[0])
    veto = mod.BaselineFlickerVeto(grid)
    for idx, rgb in enumerate((f0, f1, f2)):
        tiles, _ = desc.describe_padded(grid.pad(rgb), grid)
        tiles = tiles.copy()
        tiles["cls"] = (
            np.array([4, 3, 3, 3], dtype=np.uint8)
            if idx == 2
            else np.array([2, 2, 2, 2], dtype=np.uint8)
        )
        decision = veto.update(tiles, rgb, frame_id=idx)
    assert len(veto.history) == 2, "two-frame history window must remain intact"
    assert bool(decision.mask[0]) is True, "scroll-exposed class-4 tile 0 must be vetoed via F6"
    assert int(decision.reasons[0]) == int(fl_v3.Reason.VETO_F6), (
        f"expected Reason.VETO_F6 ({int(fl_v3.Reason.VETO_F6)}), got {int(decision.reasons[0])}"
    )
    assert not bool(decision.mask[1:].any()), "non-class-4 tiles must not be vetoed"


def _assert_postpass_apply_claims_and_combined_priority(
    mod: types.ModuleType, frames: tuple[np.ndarray, ...]
) -> None:
    """Exercise chain/veto apply_claims demotion and PostPass(baseline_flicker_veto=True) GM priority."""
    fixture = _make_chain_veto_fixture()
    demoted = mod.apply_claims(
        fixture["baseline"],
        [fixture["gm_claim"], fixture["fl_claim"]],
        flicker_veto=fixture["veto"],
    )
    if not np.array_equal(demoted["cls"], np.array([2, 2, 3, 0], dtype=np.int64)):
        raise AssertionError(f"apply_claims veto demotion mismatch: {demoted['cls'].tolist()}")

    a, b = frames[0], frames[1]
    grid = Grid(a.shape[1], a.shape[0])
    tiles, _ = desc.describe_padded(grid.pad(a), grid)
    tiles = tiles.copy()
    tiles["cls"] = 4

    post = mod.PostPass(grid, accelerated=False, baseline_flicker_veto=True)
    post.update(tiles, a, frame_id=0)
    with mock.patch.object(
        post.tracker, "update", return_value=(np.array([4]), np.array([8]))
    ), mock.patch.object(
        mod, "estimate_global_motion", return_value=(1, 0, 0)
    ), mock.patch.object(
        mod, "consensus_tiles", return_value=np.ones((1, 1), dtype=np.bool_)
    ):
        output, claims = post.update(
            tiles, b, frame_id=1, flicker_veto=np.ones(1, dtype=np.bool_)
        )
    assert int(output["global_motion"]["cls"][0]) == 3
    assert int(output["combined"]["cls"][0]) == 3, "opt-in combined path must give GM priority over FL"
    assert int(output["combined"]["mv_dx"][0]) == 1
    assert bool(claims["flicker"]["veto_mask"][0]) is True


def _assert_detector_canvas_veto_and_gm_priority(
    mod: types.ModuleType, frames: tuple[np.ndarray, ...]
) -> None:
    """Exercise PerWindowDetector with Handle('cdp_tab','tab:10',0) and Handle('godot_window','arena:2',2**85)."""
    handle_a = mod.Handle("cdp_tab", "tab:10", 0)
    handle_b = mod.Handle("godot_window", "arena:2", 2**85)
    detector = mod.PerWindowDetector()

    for idx, rgb in enumerate(frames[:3]):
        res_a = detector.update(
            handle_a,
            _make_baseline_tiles(rgb, 4 if idx == 2 else 2),
            rgb,
            epoch=1,
            frame_seq=idx,
        )
        res_b = detector.update(
            handle_b,
            _make_baseline_tiles(frames[0], 0),
            frames[0],
            epoch=7,
            frame_seq=idx,
        )
    assert int(res_a.claims["cls"][0]) == 2, "canvas nonreturn veto must demote baseline FLICKER to 2"
    assert not res_a.settled
    assert res_b.handle == handle_b and res_b.epoch == 7


class TestJQ3VisionInMemMutantHarness(unittest.TestCase):
    """Comprehensive tests for the in-memory vision mutation harness and pure receipt reducer."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.f_uniform_0 = _make_uniform_rgb(0)
        cls.f_uniform_200 = _make_uniform_rgb(200)
        cls.f_scroll_0, cls.f_scroll_1, cls.f_scroll_2 = _make_f6_scroll_frames()
        cls.f_canvas_0 = _make_canvas_noise_rgb(0)
        cls.f_canvas_1 = _make_canvas_noise_rgb(1)
        cls.f_canvas_2 = _make_canvas_noise_rgb(2)

        cls.postpass_digest = source_digest_for_path(REPO_ROOT / POSTPASS_REL_PATH)
        cls.detector_digest = source_digest_for_path(REPO_ROOT / DETECTOR_REL_PATH)
        cls.expected_source_digests = {
            POSTPASS_REL_PATH: cls.postpass_digest,
            DETECTOR_REL_PATH: cls.detector_digest,
        }

        # Catalogue contrast 1: M-eval-v2eval-postpass-1 at eval/v2eval/postpass.py:289
        cls.spec_m1 = InMemoryMutantSpec(
            mutant_id="M-eval-v2eval-postpass-1",
            module_path=POSTPASS_REL_PATH,
            package_name="eval.v2eval",
            real_module_name="eval.v2eval.postpass",
            category="operator_flip",
            qualified_ast_scope="BaselineFlickerVeto.update",
            containing_expression=(
                "isinstance(frame_id, (bool, np.bool_)) "
                "or not isinstance(frame_id, (int, np.integer)) "
                "or frame_id < 0"
            ),
            original_expression=(
                "isinstance(frame_id, (bool, np.bool_)) "
                "or not isinstance(frame_id, (int, np.integer)) "
                "or frame_id < 0"
            ),
            structural_selector="self:BoolOp",
            transformation="BoolOp.op: Or -> And",
            expected_lineno=289,
            mutator=lambda node: ast.BoolOp(op=ast.And(), values=node.values),
            frames=(cls.f_uniform_0,),
            assertion=_assert_postpass_1_frame_id_guard,
        )

        # Catalogue contrast 2: M-eval-v2eval-postpass-2 at eval/v2eval/postpass.py:316
        cls.spec_m2 = InMemoryMutantSpec(
            mutant_id="M-eval-v2eval-postpass-2",
            module_path=POSTPASS_REL_PATH,
            package_name="eval.v2eval",
            real_module_name="eval.v2eval.postpass",
            category="boundary_increment",
            qualified_ast_scope="BaselineFlickerVeto.update",
            containing_expression="old is not None and (current['cls'] == 4).any()",
            original_expression="4",
            structural_selector=(
                "values[1]:Call/func:Attribute/value:Compare/comparators[0]:Constant"
            ),
            transformation="Constant.value: 4 -> 5",
            expected_lineno=316,
            mutator=lambda node: ast.Constant(value=5),
            frames=(cls.f_scroll_0, cls.f_scroll_1, cls.f_scroll_2),
            assertion=_assert_postpass_2_class_four_old_history_gm_augmentation,
        )

        # Additional postpass site: apply_claims class-4 veto demotion target
        cls.spec_postpass_combined = InMemoryMutantSpec(
            mutant_id="M-eval-v2eval-postpass-3",
            module_path=POSTPASS_REL_PATH,
            package_name="eval.v2eval",
            real_module_name="eval.v2eval.postpass",
            category="veto_demotion_target",
            qualified_ast_scope="apply_claims",
            containing_expression="flicker_veto & (baseline['cls'] == 4)",
            original_expression="4",
            structural_selector="right:Compare/comparators[0]:Constant",
            transformation="Constant.value: 4 -> 3",
            expected_lineno=233,
            mutator=lambda node: ast.Constant(value=3),
            frames=(cls.f_uniform_0, cls.f_uniform_200),
            assertion=_assert_postpass_apply_claims_and_combined_priority,
        )

        # PerWindowDetector site: baseline FLICKER demotion in _step
        cls.spec_detector_veto = InMemoryMutantSpec(
            mutant_id="M-eval-v2x-per-window-detector-1",
            module_path=DETECTOR_REL_PATH,
            package_name="eval.v2x",
            real_module_name="eval.v2x.per_window_detector",
            category="detector_veto_demotion",
            qualified_ast_scope="PerWindowDetector._step",
            containing_expression="output['cls'][mask] = 2",
            original_expression="2",
            structural_selector="value:Constant",
            transformation="Constant.value: 2 -> 4",
            expected_lineno=270,
            mutator=lambda node: ast.Constant(value=4),
            frames=(cls.f_canvas_0, cls.f_canvas_1, cls.f_canvas_2),
            assertion=_assert_detector_canvas_veto_and_gm_priority,
        )

    def test_catalogue_contrasts_m1_and_m2_are_killed_with_exact_bindings(self) -> None:
        """Verify M-eval-v2eval-postpass-1 (line 289) and M-eval-v2eval-postpass-2 (line 316) kills."""
        r1 = _PrivateInMemoryHarness.run_spec(self.spec_m1)
        r2 = _PrivateInMemoryHarness.run_spec(self.spec_m2)

        self.assertEqual(r1.mutant_id, "M-eval-v2eval-postpass-1")
        self.assertEqual(r1.lineno, 289)
        self.assertEqual(r1.qualified_ast_scope, "BaselineFlickerVeto.update")
        self.assertEqual(
            r1.original_expression,
            "isinstance(frame_id, (bool, np.bool_)) or not isinstance(frame_id, (int, np.integer)) or frame_id < 0",
        )
        self.assertTrue(r1.positive_control_passed)
        self.assertTrue(r1.anchor_matched)
        self.assertEqual(r1.outcome, OUTCOME_KILLED)
        self.assertEqual(r1.source_digest, self.postpass_digest)
        self.assertEqual(r1.synthetic_rgb_digest, synthetic_rgb_digest((self.f_uniform_0,)))

        self.assertEqual(r2.mutant_id, "M-eval-v2eval-postpass-2")
        self.assertEqual(r2.lineno, 316)
        self.assertEqual(r2.qualified_ast_scope, "BaselineFlickerVeto.update")
        self.assertEqual(
            r2.containing_expression,
            "old is not None and (current['cls'] == 4).any()",
        )
        self.assertEqual(r2.original_expression, "4")
        self.assertEqual(r2.mutated_expression, "5")
        self.assertTrue(r2.positive_control_passed)
        self.assertTrue(r2.anchor_matched)
        self.assertEqual(r2.outcome, OUTCOME_KILLED)
        self.assertEqual(r2.source_digest, self.postpass_digest)
        self.assertEqual(
            r2.synthetic_rgb_digest,
            synthetic_rgb_digest((self.f_scroll_0, self.f_scroll_1, self.f_scroll_2)),
        )

    def test_valid_postpass_and_detector_controls_and_distinct_outcomes(self) -> None:
        """Exercise killed, survived, equivalent, missing-anchor, and infrastructure outcomes."""
        r_m1 = _PrivateInMemoryHarness.run_spec(self.spec_m1)
        r_m2 = _PrivateInMemoryHarness.run_spec(self.spec_m2)
        r_m3 = _PrivateInMemoryHarness.run_spec(self.spec_postpass_combined)
        r_det = _PrivateInMemoryHarness.run_spec(self.spec_detector_veto)

        # Survived mutant: mutate PostPass.cache_size default check or diagnostic wall_seconds
        spec_survived = InMemoryMutantSpec(
            mutant_id="M-eval-v2eval-postpass-survived",
            module_path=POSTPASS_REL_PATH,
            package_name="eval.v2eval",
            real_module_name="eval.v2eval.postpass",
            category="diagnostic_counter",
            qualified_ast_scope="PostPass.update",
            containing_expression="self.calls['flicker'] += 1",
            original_expression="1",
            structural_selector="value:Constant",
            transformation="Constant.value: 1 -> 2",
            expected_lineno=464,
            mutator=lambda node: ast.Constant(value=2),
            frames=(self.f_uniform_0, self.f_uniform_200),
            assertion=_assert_postpass_apply_claims_and_combined_priority,
        )
        r_survived = _PrivateInMemoryHarness.run_spec(spec_survived)
        self.assertEqual(r_survived.outcome, OUTCOME_SURVIVED)
        self.assertFalse(r_survived.is_killed)

        # Equivalent mutant: frame_id = int(frame_id) -> int(int(frame_id)) after int validation
        spec_equivalent = InMemoryMutantSpec(
            mutant_id="M-eval-v2eval-postpass-equiv",
            module_path=POSTPASS_REL_PATH,
            package_name="eval.v2eval",
            real_module_name="eval.v2eval.postpass",
            category="equivalent_cast",
            qualified_ast_scope="BaselineFlickerVeto.update",
            containing_expression="int(frame_id)",
            original_expression="int(frame_id)",
            structural_selector="self:Call",
            transformation="Call: int(frame_id) -> int(int(frame_id))",
            expected_lineno=291,
            mutator=lambda node: ast.Call(
                func=ast.Name(id="int", ctx=ast.Load()), args=[node], keywords=[]
            ),
            frames=(self.f_uniform_0,),
            assertion=_assert_postpass_1_frame_id_guard,
            is_equivalent=True,
            equivalent_justification="int() is idempotent on validated integer frame_id",
        )
        r_equiv = _PrivateInMemoryHarness.run_spec(spec_equivalent)
        self.assertEqual(r_equiv.outcome, OUTCOME_EQUIVALENT)
        self.assertFalse(r_equiv.is_killed)

        # Missing anchor: non-existent containing expression in BaselineFlickerVeto.update
        spec_missing = InMemoryMutantSpec(
            mutant_id="M-eval-v2eval-postpass-missing-anchor",
            module_path=POSTPASS_REL_PATH,
            package_name="eval.v2eval",
            real_module_name="eval.v2eval.postpass",
            category="missing_anchor_probe",
            qualified_ast_scope="BaselineFlickerVeto.update",
            containing_expression="nonexistent_anchor_expression == 999",
            original_expression="999",
            structural_selector="comparators[0]:Constant",
            transformation="Constant.value: 999 -> 1000",
            expected_lineno=None,
            mutator=lambda node: ast.Constant(value=1000),
            frames=(self.f_uniform_0,),
            assertion=_assert_postpass_1_frame_id_guard,
        )
        r_missing = _PrivateInMemoryHarness.run_spec(spec_missing)
        self.assertEqual(r_missing.outcome, OUTCOME_MISSING_ANCHOR)
        self.assertFalse(r_missing.anchor_matched)
        self.assertFalse(r_missing.is_killed)

        # Infrastructure error: mutant raises RuntimeError (not AssertionError)
        def _raise_runtime_error(_node: ast.AST) -> ast.AST:
            return ast.parse("(_ for _ in ()).throw(RuntimeError('mutant_infra_boom'))", mode="eval").body

        spec_infra = InMemoryMutantSpec(
            mutant_id="M-eval-v2eval-postpass-infra-exc",
            module_path=POSTPASS_REL_PATH,
            package_name="eval.v2eval",
            real_module_name="eval.v2eval.postpass",
            category="infrastructure_probe",
            qualified_ast_scope="BaselineFlickerVeto.reset",
            containing_expression="[]",
            original_expression="[]",
            structural_selector="self:List",
            transformation="List -> RuntimeError throw",
            expected_lineno=256,
            mutator=_raise_runtime_error,
            frames=(self.f_uniform_0,),
            assertion=_assert_postpass_1_frame_id_guard,
        )
        r_infra = _PrivateInMemoryHarness.run_spec(spec_infra)
        self.assertEqual(r_infra.outcome, OUTCOME_INFRASTRUCTURE)
        self.assertFalse(r_infra.is_killed)

        bound_rgb = {
            r.synthetic_rgb_digest
            for r in (r_m1, r_m2, r_m3, r_det, r_survived, r_equiv, r_missing, r_infra)
        }
        report = reduce_vision_receipts(
            [r_m1, r_m2, r_m3, r_det, r_survived, r_equiv, r_missing, r_infra],
            expected_source_digests=self.expected_source_digests,
            bound_rgb_digests=bound_rgb,
        )

        self.assertEqual(report.killed, 4)
        self.assertEqual(report.killed_numerator, 4)
        self.assertEqual(report.survived, 1)
        self.assertEqual(report.equivalent, 1)
        self.assertEqual(report.missing_anchor, 1)
        self.assertEqual(report.infrastructure, 1)
        self.assertEqual(report.total_receipts, 8)
        # Equivalent site is excluded from declared_non_equivalent_denominator (8 - 1 = 7)
        self.assertEqual(report.declared_non_equivalent_denominator, 7)
        self.assertEqual(report.evaluated_denominator, 5)
        self.assertAlmostEqual(report.mutation_score, 4 / 7)
        self.assertEqual(set(CANONICAL_OUTCOMES), {
            OUTCOME_KILLED,
            OUTCOME_SURVIVED,
            OUTCOME_EQUIVALENT,
            OUTCOME_MISSING_ANCHOR,
            OUTCOME_INFRASTRUCTURE,
        })

    def test_failed_positive_control_never_raises_killed_numerator(self) -> None:
        """Prove INV-R1-1 & INV-R1-2: a failed positive control never raises killed_numerator."""
        r_valid = _PrivateInMemoryHarness.run_spec(self.spec_m1)
        baseline_report = reduce_vision_receipts(
            [r_valid],
            expected_source_digests=self.expected_source_digests,
            bound_rgb_digests={r_valid.synthetic_rgb_digest},
        )
        self.assertEqual(baseline_report.killed_numerator, 1)

        # Spec whose assertion fails on the unmutated original module first
        def _broken_positive_assertion(mod: types.ModuleType, frames: tuple[np.ndarray, ...]) -> None:
            _assert_postpass_2_class_four_old_history_gm_augmentation(mod, frames)
            raise AssertionError("deliberate broken positive control")

        spec_failed_pos = InMemoryMutantSpec(
            mutant_id="M-eval-v2eval-postpass-failed-positive",
            module_path=POSTPASS_REL_PATH,
            package_name="eval.v2eval",
            real_module_name="eval.v2eval.postpass",
            category="failed_positive_control",
            qualified_ast_scope="BaselineFlickerVeto.update",
            containing_expression="old is not None and (current['cls'] == 4).any()",
            original_expression="4",
            structural_selector=(
                "values[1]:Call/func:Attribute/value:Compare/comparators[0]:Constant"
            ),
            transformation="Constant.value: 4 -> 5 (under broken positive control)",
            expected_lineno=316,
            mutator=lambda node: ast.Constant(value=5),
            frames=(self.f_scroll_0, self.f_scroll_1, self.f_scroll_2),
            assertion=_broken_positive_assertion,
        )
        r_failed_pos = _PrivateInMemoryHarness.run_spec(spec_failed_pos)
        self.assertFalse(r_failed_pos.positive_control_passed)
        self.assertEqual(r_failed_pos.outcome, OUTCOME_INFRASTRUCTURE)
        self.assertFalse(r_failed_pos.is_killed)

        combined_report = reduce_vision_receipts(
            [r_valid, r_failed_pos],
            expected_source_digests=self.expected_source_digests,
            bound_rgb_digests={r_valid.synthetic_rgb_digest, r_failed_pos.synthetic_rgb_digest},
        )
        # Killed numerator remains 1 while denominator rises from 1 to 2
        self.assertEqual(combined_report.killed_numerator, baseline_report.killed_numerator)
        self.assertEqual(combined_report.killed_numerator, 1)
        self.assertEqual(combined_report.infrastructure, 1)
        self.assertEqual(combined_report.declared_non_equivalent_denominator, 2)

        # Direct construction of a killed receipt with positive_control_passed=False is rejected
        with self.assertRaises(InvalidKillReceiptError):
            VisionMutationReceipt(
                mutant_id="M-forged-kill-no-pos",
                module_path=POSTPASS_REL_PATH,
                category="forged",
                source_digest=self.postpass_digest,
                qualified_ast_scope="BaselineFlickerVeto.update",
                original_expression="4",
                structural_selector="comparators[0]:Constant",
                transformation="Constant.value: 4 -> 5",
                positive_control_passed=False,
                synthetic_rgb_digest=r_valid.synthetic_rgb_digest,
                outcome=OUTCOME_KILLED,
            )

    def test_single_condition_negatives_for_receipt_schema_and_reducer(self) -> None:
        """Test single-condition negatives for every receipt invariant and binding rule."""
        base_rgb_digest = synthetic_rgb_digest((self.f_uniform_0,))
        valid_kwargs = dict(
            mutant_id="M-valid-1",
            module_path=POSTPASS_REL_PATH,
            category="operator_flip",
            source_digest=self.postpass_digest,
            qualified_ast_scope="BaselineFlickerVeto.update",
            containing_expression="frame_id < 0",
            original_expression="frame_id < 0",
            structural_selector="self:Compare",
            transformation="Compare.ops[0]: Lt -> LtE",
            positive_control_passed=True,
            synthetic_rgb_digest=base_rgb_digest,
            outcome=OUTCOME_KILLED,
            anchor_matched=True,
        )
        valid_receipt = VisionMutationReceipt(**valid_kwargs)
        self.assertTrue(valid_receipt.is_killed)

        # 1. Unmatched anchor cannot be killed (INV-R1-1)
        with self.assertRaises(InvalidKillReceiptError):
            VisionMutationReceipt(**{**valid_kwargs, "anchor_matched": False})

        # 2. Bare ordinal in structural_selector or qualified_ast_scope is rejected
        with self.assertRaises(MutationReceiptError):
            VisionMutationReceipt(**{**valid_kwargs, "structural_selector": "0"})
        with self.assertRaises(MutationReceiptError):
            VisionMutationReceipt(**{**valid_kwargs, "qualified_ast_scope": "12"})

        # 3. Malformed source_digest or synthetic_rgb_digest is rejected
        with self.assertRaises(UnboundDigestError):
            VisionMutationReceipt(**{**valid_kwargs, "source_digest": "bad_digest"})
        with self.assertRaises(UnboundDigestError):
            VisionMutationReceipt(**{**valid_kwargs, "synthetic_rgb_digest": "A" * 64})

        # 4. Non-bool positive_control_passed (e.g., int 1) is rejected
        with self.assertRaises(MutationReceiptError):
            VisionMutationReceipt(**{**valid_kwargs, "positive_control_passed": 1})  # type: ignore[arg-type]

        # 5. Equivalent outcome without justification is rejected
        with self.assertRaises(MutationReceiptError):
            VisionMutationReceipt(
                **{
                    **valid_kwargs,
                    "outcome": OUTCOME_EQUIVALENT,
                    "equivalent_justification": "",
                }
            )

        # 6. Duplicate mutant_id or duplicate AST site is rejected before aggregation
        dup_id = VisionMutationReceipt(
            **{
                **valid_kwargs,
                "structural_selector": "values[2]:Compare",
            }
        )
        with self.assertRaises(DuplicateMutationSiteError):
            reduce_vision_receipts([valid_receipt, dup_id])

        dup_site = VisionMutationReceipt(
            **{
                **valid_kwargs,
                "mutant_id": "M-valid-2-same-site",
            }
        )
        with self.assertRaises(DuplicateMutationSiteError):
            reduce_vision_receipts([valid_receipt, dup_site])

        # 7. Unbound source digest or unbound RGB digest is rejected before aggregation
        with self.assertRaises(UnboundDigestError):
            reduce_vision_receipts(
                [valid_receipt],
                expected_source_digests={POSTPASS_REL_PATH: "0" * 64},
            )
        with self.assertRaises(UnboundDigestError):
            reduce_vision_receipts(
                [valid_receipt],
                bound_rgb_digests={"f" * 64},
            )

        # 8. Undeclared site is rejected
        undeclared = VisionMutationReceipt(**{**valid_kwargs, "mutant_id": "M-undecl", "declared": False})
        with self.assertRaises(MutationReceiptError):
            reduce_vision_receipts([undeclared])

    def test_seeded_property_fuzz_loop_and_counterexample_shrinking(self) -> None:
        """Seeded property loop verifying INV-R1-1/INV-R1-2 and minimal counterexample shrinking."""
        rng = random.Random(REPLAY_SEED)
        rgb_digest = synthetic_rgb_digest((self.f_uniform_0,))

        for trial_idx in range(25):
            n_sites = rng.randint(4, 12)
            batch: list[VisionMutationReceipt] = []
            expected_kills = 0
            expected_equiv = 0
            for site_idx in range(n_sites):
                pos_ok = rng.choice([True, True, False])
                anc_ok = rng.choice([True, True, False])
                is_eq = pos_ok and anc_ok and rng.choice([False, False, True])
                exc_kind = rng.choice(["assert", "none", "runtime", "import"])
                exc: BaseException | None
                if exc_kind == "assert":
                    exc = AssertionError("behavioral failure")
                elif exc_kind == "runtime":
                    exc = RuntimeError("crash")
                elif exc_kind == "import":
                    exc = ImportError("bad import")
                else:
                    exc = None

                outcome = classify_trial_outcome(
                    positive_control_passed=pos_ok,
                    anchor_matched=anc_ok,
                    mutant_exception=exc,
                    is_equivalent=is_eq,
                )
                if outcome == OUTCOME_KILLED:
                    self.assertTrue(pos_ok)
                    self.assertTrue(anc_ok)
                    self.assertIsInstance(exc, AssertionError)
                    expected_kills += 1
                if outcome == OUTCOME_EQUIVALENT:
                    expected_equiv += 1

                batch.append(
                    VisionMutationReceipt(
                        mutant_id=f"M-fuzz-{trial_idx}-{site_idx}",
                        module_path=POSTPASS_REL_PATH,
                        category=f"cat_{site_idx % 3}",
                        source_digest=self.postpass_digest,
                        qualified_ast_scope="BaselineFlickerVeto.update",
                        containing_expression=f"expr_{site_idx}",
                        original_expression=f"expr_{site_idx}",
                        structural_selector=f"values[{site_idx}]:Compare",
                        transformation=f"op_flip_{site_idx}",
                        positive_control_passed=pos_ok,
                        synthetic_rgb_digest=rgb_digest,
                        outcome=outcome,
                        anchor_matched=anc_ok,
                        equivalent_justification="idempotent" if outcome == OUTCOME_EQUIVALENT else None,
                    )
                )

            shuffled = list(batch)
            rng.shuffle(shuffled)
            rep_a = reduce_vision_receipts(
                batch,
                expected_source_digests={POSTPASS_REL_PATH: self.postpass_digest},
                bound_rgb_digests={rgb_digest},
            )
            rep_b = reduce_vision_receipts(
                shuffled,
                expected_source_digests={POSTPASS_REL_PATH: self.postpass_digest},
                bound_rgb_digests={rgb_digest},
            )
            self.assertEqual(rep_a.killed_numerator, expected_kills)
            self.assertEqual(
                rep_a.declared_non_equivalent_denominator, n_sites - expected_equiv
            )
            self.assertEqual(rep_a.receipts, rep_b.receipts)

        # Deterministic counterexample shrinker metadata verification
        bad_stream = list(batch) + [batch[0]]  # inject duplicate at end

        def _fails_reduction(candidate: list[VisionMutationReceipt]) -> bool:
            try:
                reduce_vision_receipts(candidate)
                return False
            except DuplicateMutationSiteError:
                return True

        # Shrink to smallest counterexample preserving the duplicate-site failure
        shrunk = list(bad_stream)
        i = 0
        while i < len(shrunk):
            without_i = shrunk[:i] + shrunk[i + 1 :]
            if _fails_reduction(without_i):
                shrunk = without_i
            else:
                i += 1
        counterexample_meta = {
            "replay_seed": REPLAY_SEED,
            "smallest_counterexample_len": len(shrunk),
            "mutant_ids": [r.mutant_id for r in shrunk],
        }
        self.assertEqual(counterexample_meta["replay_seed"], REPLAY_SEED)
        self.assertEqual(len(shrunk), 2)
        self.assertEqual(shrunk[0].mutant_id, shrunk[1].mutant_id)

    def test_multithreaded_harness_isolation_with_barrier_and_leak_detection(self) -> None:
        """Run concurrent in-memory mutant trials across threads with Barrier, bounded joins, and leak checks."""
        specs = (
            self.spec_m1,
            self.spec_m2,
            self.spec_postpass_combined,
            self.spec_detector_veto,
        )
        n_workers = len(specs)
        barrier = threading.Barrier(n_workers, timeout=5.0)
        results: list[VisionMutationReceipt | None] = [None] * n_workers
        errors: list[BaseException] = []
        errors_lock = threading.Lock()

        threads_before = {t.ident for t in threading.enumerate()}
        modules_before = set(sys.modules.keys())

        def _worker(idx: int, spec: InMemoryMutantSpec) -> None:
            try:
                barrier.wait(timeout=5.0)
                rec = _PrivateInMemoryHarness.run_spec(spec)
                barrier.wait(timeout=5.0)
                results[idx] = rec
            except BaseException as exc:  # pragma: no cover
                with errors_lock:
                    errors.append(exc)

        workers = [
            threading.Thread(
                target=_worker,
                args=(i, spec),
                name=f"jq3-mutant-worker-{i}",
                daemon=True,
            )
            for i, spec in enumerate(specs)
        ]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=5.0)
            self.assertFalse(w.is_alive(), f"worker thread {w.name} timed out")

        if errors:
            raise errors[0]

        threads_after = {t.ident for t in threading.enumerate()}
        self.assertEqual(threads_before, threads_after, "leaked worker threads detected")
        self.assertEqual(set(sys.modules.keys()), modules_before, "leaked private modules in sys.modules")

        validated = [r for r in results if r is not None]
        self.assertEqual(len(validated), n_workers)
        report = reduce_vision_receipts(
            validated,
            expected_source_digests=self.expected_source_digests,
            bound_rgb_digests={r.synthetic_rgb_digest for r in validated},
        )
        self.assertEqual(report.killed_numerator, 4)
        self.assertEqual(report.declared_non_equivalent_denominator, 4)

    def test_api_contracts_and_class_four_gm_target_specificity(self) -> None:
        """Verify merged API contracts on PostPass, BaselineFlickerVeto, and PerWindowDetector."""
        self.assertEqual(
            real_postpass.VARIANTS,
            ("baseline", "global_motion", "flicker", "subtile", "combined"),
        )
        grid = Grid(16, 16)
        rgb = _make_uniform_rgb(42)
        tiles, _ = desc.describe_padded(grid.pad(rgb), grid)
        arr_map = real_postpass.arrays(tiles)
        for key in ("cls", "mv_dx", "mv_dy"):
            self.assertEqual(arr_map[key].dtype, np.int64)

        veto = real_postpass.BaselineFlickerVeto(grid)
        with self.assertRaisesRegex(ValueError, "RAM RGB uint8 frame must match grid"):
            veto.update(tiles, np.zeros((8, 8, 3), dtype=np.uint8))
        with self.assertRaisesRegex(ValueError, "baseline tiles must match the grid's TILE_DTYPE order"):
            veto.update(np.zeros(1, dtype=np.int32), rgb)

        post = real_postpass.PostPass(grid, accelerated=False, baseline_flicker_veto=True)
        with self.assertRaisesRegex(ValueError, "RAM RGB uint8 frame must match grid"):
            post.update(tiles, None)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "RAM RGB uint8 frame must match grid"):
            post.update(tiles, np.zeros((16, 16, 3), dtype=np.int16))

        # PerWindowDetector contracts
        self.assertTrue(issubclass(real_pwd.MissingEvidenceError, real_pwd.DetectorInputError))
        for kind in ("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"):
            h = real_pwd.Handle(kind, f"{kind}:0", 0)
            self.assertEqual(h.kind, kind)
        with self.assertRaisesRegex(
            real_pwd.DetectorInputError, "veto must be an independent per-stream factory"
        ):
            real_pwd.PerWindowDetector(veto="not_callable")  # type: ignore[arg-type]

        det = real_pwd.PerWindowDetector()
        h_tab = real_pwd.Handle("cdp_tab", "tab:10", 0)
        with self.assertRaisesRegex(
            real_pwd.DetectorInputError, "tiles must match the exact S2 grid, dtype and Morton order"
        ):
            det.update(h_tab, np.zeros(2, dtype=desc.TILE_DTYPE), rgb, epoch=1, frame_seq=0)

        class _BadConsensusGM:
            @staticmethod
            def estimate_global_motion(prev_y, cur_y, max_d):
                assert max_d == max(grid.W, grid.H) - 1
                return (4, 0, 0)

            @staticmethod
            def consensus_tiles(prev_y, cur_y, side, dx, dy):
                return np.ones((2, 2), dtype=np.bool_)

        det_bad_gm = real_pwd.PerWindowDetector(gm=_BadConsensusGM())
        det_bad_gm.update(h_tab, _make_baseline_tiles(rgb), rgb, epoch=1, frame_seq=0)
        rgb2 = _make_uniform_rgb(99)
        with self.assertRaisesRegex(real_pwd.MissingEvidenceError, "GM consensus must match grid"):
            det_bad_gm.update(h_tab, _make_baseline_tiles(rgb2), rgb2, epoch=1, frame_seq=1)
        det_bad_gm.reset_window(h_tab)


if __name__ == "__main__":
    unittest.main()
