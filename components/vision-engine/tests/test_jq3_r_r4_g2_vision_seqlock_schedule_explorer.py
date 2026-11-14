"""Acceptance suite G-R4-2 for jq3-r-r4-g2-vision-seqlock-schedule-explorer.

Covers:
- Deterministic seqlock and ring-buffer schedule explorer in
  ``eval.v2eval.seqlock_schedule_explorer`` driving real ``vadapt.rtring``,
  ``vadapt.ramws``, ``vadapt.multi_x11_ramcap``, and ``vadapt.window_stream_bank`` APIs.
- Enumeration of writer preemption between ``begin``, ``payload``, ``slot_seal``,
  ``head_update``, and ``copied_read_revalidation`` via existing 1-arg
  ``before_copy`` / ``after_copy`` ``step_hook`` callbacks and local writer wrappers.
- Rejection of odd, unequal, or overwritten sequences and corrupt CRC while
  preserving the last valid immutable frame in explorer state.
- Canonical handle admission, stale-token refusal, exact reservation conservation,
  per-target reservation deltas, close/resize/retained-view accounting without
  any mapped pixel file, and factory/hook execution outside metadata locks.
- Embedded AST mutation harness killing named catalogue mutant
  ``M-vadapt-window-stream-bank-1`` (``vadapt/window_stream_bank.py:258``,
  anchor ``raw is None``) and structural seqlock/ring invariants with strict
  outcome bucket separation.
- Seeded 64-case fuzz/property loop with independent oracle, replay seed, and
  smallest counterexample shrinking metadata.
- 32-schedule concurrency suite with barriers, bounded joins, propagated
  exceptions, and thread leak detection.
"""

from __future__ import annotations

import ast
import builtins
import copy
from dataclasses import FrozenInstanceError, dataclass, fields
import hashlib
import inspect
import mmap
import os
import random
import socket
import struct
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch
import zlib

import numpy as np

import eval.v2eval.seqlock_schedule_explorer as explorer_mod
from eval.v2eval.seqlock_schedule_explorer import (
    AUSENTE,
    BoundedScheduleTrace,
    DEFAULT_SEED,
    DEFAULT_TRACE_CAPACITY,
    ExplorationReport,
    FAULT_MODES,
    FakeMmap,
    FakeRamWorkspace,
    FakeRingBacking,
    InMemoryRingSeam,
    LocalBankWriterWrapper,
    LocalRingWriterWrapper,
    PREEMPTION_STAGES,
    ReservationDelta,
    ScheduleStep,
    SeqlockScheduleExplorer,
    TIMING_ENV,
    VirtualRingPath,
    canonical_handle_key,
    make_deterministic_rgb,
    run_deterministic_exploration,
    shrink_schedule_counterexample,
    snapshot_valid_rtring_frame,
    verify_immutable_frame_ref,
)
from vadapt import multi_x11_ramcap, ramws, rtring
from vadapt.multi_x11_ramcap import (
    CaptureFailure,
    CropSpec,
    MemoryStreamBank,
    TrustedScreen,
    capture_many,
)
import vadapt.window_stream_bank as wsb_mod
from vadapt.window_stream_bank import (
    Drop,
    FrameRef,
    Handle,
    KINDS,
    QuotaError,
    StaleStreamError,
    StreamError,
    StreamSpec,
    StreamToken,
    WindowStreamBank,
    check_lock_order,
)

VM_BUDGET_SECONDS = 15.0
PROPERTY_TARGET_CASES = 64
PROPERTY_SEED = 0x5E010C42
CONCURRENCY_TARGET_SCHEDULES = 32
MAX_CONCURRENCY_THREADS = 4
SYNC_TIMEOUT_S = 2.0


class _ForbiddenLiveIOAndMappedPixelGuard:
    """Context manager forbidding disk writes, mapped pixel files, subprocesses, and sockets."""

    def __init__(self) -> None:
        self._patches: list[object] = []

    def __enter__(self) -> _ForbiddenLiveIOAndMappedPixelGuard:
        real_open = builtins.open

        def _guarded_open(file: object, mode: str = "r", *args: object, **kwargs: object):
            if any(ch in str(mode) for ch in ("w", "a", "x", "+")):
                raise AssertionError(f"forbidden file write attempted: {file!r} ({mode!r})")
            return real_open(file, mode, *args, **kwargs)

        def _forbidden_os_open(*args: object, **kwargs: object):
            raise AssertionError(f"forbidden os.open attempted: {args!r}")

        def _forbidden_mmap(*args: object, **kwargs: object):
            raise AssertionError(f"forbidden real mmap.mmap attempted: {args!r}")

        def _forbidden_proc(*args: object, **kwargs: object):
            raise AssertionError(f"forbidden subprocess launch attempted: {args!r}")

        def _forbidden_sock(*args: object, **kwargs: object):
            raise AssertionError("forbidden network socket attempted")

        self._patches = [
            patch.object(builtins, "open", side_effect=_guarded_open),
            patch.object(os, "open", side_effect=_forbidden_os_open),
            patch.object(mmap, "mmap", side_effect=_forbidden_mmap),
            patch.object(subprocess, "Popen", side_effect=_forbidden_proc),
            patch.object(subprocess, "run", side_effect=_forbidden_proc),
            patch.object(socket, "socket", side_effect=_forbidden_sock),
        ]
        for pt in self._patches:
            pt.start()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> bool:
        for pt in reversed(self._patches):
            pt.stop()
        self._patches = []
        return False


@dataclass(frozen=True)
class MutantOutcome:
    mutant_id: str
    qualified_scope: str
    status: str
    positive_passed: bool
    killed: bool
    detail: str


class _ScopedAstMutationHarness:
    """Embedded in-memory AST mutation harness with strict outcome separation.

    Selects sites by:
    1. ``qualified_scope`` (module + class/function/method path),
    2. ``containing_expr_predicate`` (must match a unique AST statement/expression
       within ``qualified_scope``), and
    3. ``structural_mutator`` (mutates the unique containing AST node).

    Runs the identical behavioral ``probe`` on the real module first. Only if the
    positive control passes does it compile the mutated AST into a fresh private
    module with ``sys.dont_write_bytecode = True`` and restore module/package
    state in ``finally``. Only ``AssertionError`` from ``probe(mutated_mod)``
    counts as ``KILLED``.
    """

    def __init__(self) -> None:
        self.outcomes: list[MutantOutcome] = []

    @property
    def killed_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "KILLED" and o.positive_passed and o.killed)

    def evaluate_mutant(
        self,
        *,
        mutant_id: str,
        module: types.ModuleType,
        qualified_scope: str,
        containing_expr_predicate,
        structural_mutator,
        probe,
        equivalent: bool = False,
        timeout_s: float = SYNC_TIMEOUT_S,
    ) -> MutantOutcome:
        orig_dont_write = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            # Step 1: Run identical assertion on the real unmutated module first.
            try:
                probe(module)
            except BaseException as exc:  # noqa: BLE001
                outcome = MutantOutcome(
                    mutant_id=mutant_id,
                    qualified_scope=qualified_scope,
                    status="FAILED_POSITIVE",
                    positive_passed=False,
                    killed=False,
                    detail=f"{type(exc).__name__}: {exc}",
                )
                self.outcomes.append(outcome)
                return outcome

            if equivalent:
                outcome = MutantOutcome(
                    mutant_id=mutant_id,
                    qualified_scope=qualified_scope,
                    status="EQUIVALENT",
                    positive_passed=True,
                    killed=False,
                    detail="marked equivalent",
                )
                self.outcomes.append(outcome)
                return outcome

            # Step 2: Locate unique qualified AST scope.
            source = inspect.getsource(module)
            tree = ast.parse(source, filename=module.__file__ or "<memory>")
            mod_parts = module.__name__.split(".")
            scope_parts = qualified_scope.split(".")
            if scope_parts[: len(mod_parts)] != mod_parts:
                outcome = MutantOutcome(
                    mutant_id=mutant_id,
                    qualified_scope=qualified_scope,
                    status="MISSING_ANCHOR",
                    positive_passed=True,
                    killed=False,
                    detail="scope module prefix mismatch",
                )
                self.outcomes.append(outcome)
                return outcome
            rel_parts = tuple(scope_parts[len(mod_parts) :])
            if not rel_parts:
                outcome = MutantOutcome(
                    mutant_id=mutant_id,
                    qualified_scope=qualified_scope,
                    status="MISSING_ANCHOR",
                    positive_passed=True,
                    killed=False,
                    detail="empty relative scope",
                )
                self.outcomes.append(outcome)
                return outcome

            matched_scopes: list[ast.AST] = []

            def _walk_scopes(node: ast.AST, path: tuple[str, ...]) -> None:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        next_path = path + (child.name,)
                        if next_path == rel_parts:
                            matched_scopes.append(child)
                        else:
                            _walk_scopes(child, next_path)

            _walk_scopes(tree, ())
            if len(matched_scopes) == 0:
                outcome = MutantOutcome(
                    mutant_id=mutant_id,
                    qualified_scope=qualified_scope,
                    status="MISSING_ANCHOR",
                    positive_passed=True,
                    killed=False,
                    detail="scope not found",
                )
                self.outcomes.append(outcome)
                return outcome
            if len(matched_scopes) > 1:
                outcome = MutantOutcome(
                    mutant_id=mutant_id,
                    qualified_scope=qualified_scope,
                    status="NONUNIQUE_ANCHOR",
                    positive_passed=True,
                    killed=False,
                    detail=f"found {len(matched_scopes)} scopes",
                )
                self.outcomes.append(outcome)
                return outcome

            # Step 3: Select unique containing expression within the qualified scope.
            scope_node = matched_scopes[0]
            containing_matches = [
                n for n in ast.walk(scope_node) if containing_expr_predicate(n)
            ]
            if len(containing_matches) == 0:
                outcome = MutantOutcome(
                    mutant_id=mutant_id,
                    qualified_scope=qualified_scope,
                    status="MISSING_ANCHOR",
                    positive_passed=True,
                    killed=False,
                    detail="containing expression not found",
                )
                self.outcomes.append(outcome)
                return outcome
            if len(containing_matches) > 1:
                outcome = MutantOutcome(
                    mutant_id=mutant_id,
                    qualified_scope=qualified_scope,
                    status="NONUNIQUE_ANCHOR",
                    positive_passed=True,
                    killed=False,
                    detail=f"found {len(containing_matches)} matching containing expressions",
                )
                self.outcomes.append(outcome)
                return outcome

            applied = structural_mutator(containing_matches[0])
            if applied != 1:
                status = "MISSING_ANCHOR" if applied == 0 else "NONUNIQUE_ANCHOR"
                outcome = MutantOutcome(
                    mutant_id=mutant_id,
                    qualified_scope=qualified_scope,
                    status=status,
                    positive_passed=True,
                    killed=False,
                    detail=f"structural_mutator applied {applied} edits",
                )
                self.outcomes.append(outcome)
                return outcome

            ast.fix_missing_locations(tree)

            # Step 4: Compile and execute in a fresh private module in memory.
            mutated_mod = types.ModuleType(module.__name__)
            mutated_mod.__file__ = module.__file__
            mutated_mod.__package__ = module.__package__
            mutated_mod.__loader__ = getattr(module, "__loader__", None)
            mutated_mod.__spec__ = getattr(module, "__spec__", None)

            orig_sys_mod = sys.modules.get(module.__name__)
            parent_pkg = sys.modules.get(module.__package__) if module.__package__ else None
            attr_name = module.__name__.rsplit(".", 1)[-1]
            orig_pkg_attr = getattr(parent_pkg, attr_name, None) if parent_pkg is not None else None

            try:
                try:
                    code_obj = compile(tree, filename="<ast-mutant-memory>", mode="exec")
                    exec(code_obj, mutated_mod.__dict__)
                except BaseException as exc:  # noqa: BLE001
                    outcome = MutantOutcome(
                        mutant_id=mutant_id,
                        qualified_scope=qualified_scope,
                        status="IMPORT_OR_INIT_ERROR",
                        positive_passed=True,
                        killed=False,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                    self.outcomes.append(outcome)
                    return outcome

                sys.modules[module.__name__] = mutated_mod
                if parent_pkg is not None:
                    setattr(parent_pkg, attr_name, mutated_mod)

                t_start = time.monotonic()
                try:
                    probe(mutated_mod)
                except TimeoutError as exc:
                    outcome = MutantOutcome(
                        mutant_id=mutant_id,
                        qualified_scope=qualified_scope,
                        status="TIMEOUT",
                        positive_passed=True,
                        killed=False,
                        detail=str(exc),
                    )
                    self.outcomes.append(outcome)
                    return outcome
                except AssertionError as exc:
                    if time.monotonic() - t_start > timeout_s:
                        outcome = MutantOutcome(
                            mutant_id=mutant_id,
                            qualified_scope=qualified_scope,
                            status="TIMEOUT",
                            positive_passed=True,
                            killed=False,
                            detail="exceeded timeout during assertion",
                        )
                        self.outcomes.append(outcome)
                        return outcome
                    outcome = MutantOutcome(
                        mutant_id=mutant_id,
                        qualified_scope=qualified_scope,
                        status="KILLED",
                        positive_passed=True,
                        killed=True,
                        detail=str(exc),
                    )
                    self.outcomes.append(outcome)
                    return outcome
                except BaseException as exc:  # noqa: BLE001
                    outcome = MutantOutcome(
                        mutant_id=mutant_id,
                        qualified_scope=qualified_scope,
                        status="UNRELATED_EXCEPTION",
                        positive_passed=True,
                        killed=False,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                    self.outcomes.append(outcome)
                    return outcome

                if time.monotonic() - t_start > timeout_s:
                    outcome = MutantOutcome(
                        mutant_id=mutant_id,
                        qualified_scope=qualified_scope,
                        status="TIMEOUT",
                        positive_passed=True,
                        killed=False,
                        detail="exceeded timeout without assertion failure",
                    )
                    self.outcomes.append(outcome)
                    return outcome

                outcome = MutantOutcome(
                    mutant_id=mutant_id,
                    qualified_scope=qualified_scope,
                    status="SURVIVED",
                    positive_passed=True,
                    killed=False,
                    detail="probe completed without AssertionError",
                )
                self.outcomes.append(outcome)
                return outcome
            finally:
                if orig_sys_mod is not None:
                    sys.modules[module.__name__] = orig_sys_mod
                if parent_pkg is not None and orig_pkg_attr is not None:
                    setattr(parent_pkg, attr_name, orig_pkg_attr)
        finally:
            sys.dont_write_bytecode = orig_dont_write


class _IndependentSeqlockOracle:
    """Independent reference oracle for stream seqlock schedules and reservation conservation."""

    def __init__(self, *, quota_bytes: int) -> None:
        self.quota_bytes = quota_bytes
        self.reserved_bytes = 0
        self.last_valid_seq: dict[tuple[str, str, int], int | None] = {}
        self.last_valid_digest: dict[tuple[str, str, int], str | None] = {}
        self.epochs: dict[tuple[str, str, int], int] = {}

    def open_target(self, handle: Handle, charge: int) -> None:
        key = canonical_handle_key(handle)
        if self.reserved_bytes + charge > self.quota_bytes:
            raise QuotaError("oracle quota exceeded")
        self.reserved_bytes += charge
        self.last_valid_seq[key] = None
        self.last_valid_digest[key] = None
        self.epochs[key] = 0

    def step(
        self,
        handle: Handle,
        rgb: np.ndarray,
        *,
        frame_seq: int,
        stage: str,
        fault: str,
    ) -> tuple[str, str | None, int | None, str | None]:
        key = canonical_handle_key(handle)
        if fault == "clean":
            digest = hashlib.sha256(rgb.tobytes()).hexdigest()
            self.last_valid_seq[key] = frame_seq
            self.last_valid_digest[key] = digest
            return ("accepted", None, frame_seq, digest)

        expected_drop = {
            ("begin", "odd_sequence"): "torn",
            ("payload", "payload_tear"): "torn",
            ("slot_seal", "unequal_sequence"): "torn",
            ("slot_seal", "overwritten_sequence"): "lapped",
            ("head_update", "unadvanced_head"): "absent",
            ("head_update", "head_regressed"): "torn",
            ("copied_read_revalidation", "corrupt_crc"): "torn",
            ("copied_read_revalidation", "stale_epoch_resize"): "stale_epoch",
        }[(stage, fault)]
        if fault == "stale_epoch_resize":
            self.epochs[key] += 1
        return (
            "dropped",
            expected_drop,
            self.last_valid_seq[key],
            self.last_valid_digest[key],
        )


class TestJq3R4G2VisionSeqlockScheduleExplorer(unittest.TestCase):
    """Acceptance gate G-R4-2 test suite."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._suite_start = time.monotonic()

    @classmethod
    def tearDownClass(cls) -> None:
        elapsed = time.monotonic() - cls._suite_start
        if elapsed > VM_BUDGET_SECONDS:
            raise AssertionError(
                f"VM suite budget exceeded: {elapsed:.3f}s VM > {VM_BUDGET_SECONDS:.1f}s VM"
            )

    def test_frozen_records_and_api_constants(self) -> None:
        self.assertEqual(AUSENTE, "AUSENTE")
        self.assertEqual(TIMING_ENV, "VM")
        self.assertEqual(
            PREEMPTION_STAGES,
            ("begin", "payload", "slot_seal", "head_update", "copied_read_revalidation"),
        )
        self.assertEqual(
            KINDS,
            frozenset({"cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"}),
        )
        self.assertEqual(
            tuple(f.name for f in fields(ReservationDelta)),
            (
                "handle",
                "epoch",
                "operation",
                "before_bytes",
                "after_bytes",
                "delta_bytes",
                "target_reserved_bytes",
                "retained_bytes",
            ),
        )
        self.assertEqual(
            tuple(f.name for f in fields(ScheduleStep)),
            (
                "step_index",
                "seed",
                "surface",
                "handle",
                "epoch",
                "stage",
                "fault",
                "hook_phases",
                "hook_outside_lock",
                "outcome",
                "drop_reason",
                "accepted_seq",
                "preserved_seq",
                "preserved_digest",
                "torn_observed",
                "reserved_bytes",
            ),
        )
        delta = ReservationDelta(
            Handle("cdp_tab", "tab/1", 0), 0, "open", 0, 4144, 4144, 4144, 0
        )
        with self.assertRaises(FrozenInstanceError):
            delta.delta_bytes = 0  # type: ignore[misc]

    def test_canonical_handle_admission_and_single_condition_negatives(self) -> None:
        with _ForbiddenLiveIOAndMappedPixelGuard():
            unit = rtring.ring_bytes(2, 4, 2)
            explorer = SeqlockScheduleExplorer(quota_bytes=6 * unit)
            spec = StreamSpec(4, 2, 2, unit)

            handles = [
                Handle("capsule", "cap/0", 1),
                Handle("cdp_tab", "tab/0", 1),
                Handle("godot_window", "godot/0", 1),
                Handle("x11_window", "win/0", 1),
                Handle("xvfb_screen", "screen/0", 1),
            ]
            self.assertTrue(explorer.bank.check_lock_order(handles))
            tokens = explorer.open_many([(h, spec) for h in handles])
            self.assertEqual(len(tokens), 5)
            self.assertEqual([t.epoch for t in tokens], [0, 0, 0, 0, 0])
            self.assertEqual(explorer.bank.reserved_bytes, 5 * unit)

            # Single-condition negative 1: unsupported kind
            with self.assertRaises(StreamError):
                Handle("wayland_surface", "wl/0", 0)
            # Single-condition negative 2: empty canonical_id
            with self.assertRaises(StreamError):
                Handle("cdp_tab", "", 0)
            # Single-condition negative 3: non-str canonical_id
            with self.assertRaises(StreamError):
                Handle("cdp_tab", 123, 0)  # type: ignore[arg-type]
            # Single-condition negative 4: negative generation
            with self.assertRaises(StreamError):
                Handle("cdp_tab", "tab/neg", -1)
            # Single-condition negative 5: bool generation
            with self.assertRaises(StreamError):
                Handle("cdp_tab", "tab/bool", True)  # type: ignore[arg-type]
            # Single-condition negative 6: reversed canonical order in open_many
            h_hi = Handle("xvfb_screen", "z-last", 1)
            h_lo = Handle("cdp_tab", "a-first", 1)
            with self.assertRaises(StreamError):
                check_lock_order((h_hi, h_lo))
            with self.assertRaises(StreamError):
                explorer.open_many(((h_hi, spec), (h_lo, spec)))
            # Single-condition negative 7: duplicate handle in check_lock_order
            with self.assertRaises(StreamError):
                check_lock_order((h_lo, h_lo))

            for tok in tokens:
                explorer.close_stream(tok)
            self.assertEqual(explorer.bank.reserved_bytes, 0)

            # Single-condition negative 8: reopening a closed incarnation is refused
            with self.assertRaises(StaleStreamError):
                explorer.open_stream(handles[0], spec)
            self.assertEqual(explorer.bank.reserved_bytes, 0)

    def test_stale_token_refusal_and_factory_hook_outside_metadata_locks(self) -> None:
        with _ForbiddenLiveIOAndMappedPixelGuard():
            unit = rtring.ring_bytes(2, 4, 2)
            explorer = SeqlockScheduleExplorer(quota_bytes=3 * unit)
            h = Handle("cdp_tab", "tab/stale", 1)
            spec = StreamSpec(4, 2, 2, unit)

            tok0 = explorer.open_stream(h, spec)
            self.assertEqual(tok0.epoch, 0)
            self.assertTrue(all(explorer.factory_lock_checks))

            f0 = make_deterministic_rgb(4, 2, frame_seq=0)
            ref0 = explorer.publish_valid_frame(tok0, f0, frame_seq=0)
            self.assertEqual(ref0.epoch, 0)

            # Resize advances epoch to 1 and invalidates tok0
            tok1 = explorer.resize_stream(tok0, spec)
            self.assertEqual(tok1.epoch, 1)
            self.assertEqual(explorer.bank.reserved_bytes, unit)

            for action in (
                lambda: explorer.bank.read(tok0),
                lambda: explorer.bank.publish(tok0, f0, frame_seq=1),
                lambda: explorer.bank.resize(tok0, spec),
                lambda: explorer.bank.close(tok0),
            ):
                with self.assertRaises(StaleStreamError) as ctx:
                    action()
                self.assertEqual(
                    str(ctx.exception), "closed generation or stale geometry epoch"
                )

            # Publish frame 1 on tok1 and run a step_hook that checks lock freedom
            step = explorer.explore_bank_preemption(
                tok1,
                make_deterministic_rgb(4, 2, frame_seq=1),
                frame_seq=1,
                stage="head_update",
                fault="clean",
            )
            self.assertEqual(step.outcome, "accepted")
            self.assertEqual(step.hook_phases, ("before_copy", "after_copy"))
            self.assertTrue(step.hook_outside_lock)
            self.assertTrue(all(explorer.hook_lock_checks))

            explorer.close_stream(tok1)
            with self.assertRaises(StaleStreamError) as ctx_closed:
                explorer.bank.read(tok1)
            self.assertEqual(
                str(ctx_closed.exception), "closed generation or stale geometry epoch"
            )
            self.assertEqual(explorer.bank.reserved_bytes, 0)

    def test_preemption_enumeration_rejects_faults_and_preserves_last_valid_frame(self) -> None:
        with _ForbiddenLiveIOAndMappedPixelGuard():
            unit = rtring.ring_bytes(2, 4, 2)
            explorer = SeqlockScheduleExplorer(quota_bytes=2 * unit, seed=0x123456)
            h = Handle("cdp_tab", "tab/preempt", 1)
            spec = StreamSpec(4, 2, 2, unit)
            key = canonical_handle_key(h)

            tok = explorer.open_stream(h, spec)

            # 1. Head not updated before any frame committed -> Drop("absent"), last_valid is None
            s_absent0 = explorer.explore_bank_preemption(
                tok,
                make_deterministic_rgb(4, 2, frame_seq=0),
                frame_seq=0,
                stage="head_update",
                fault="unadvanced_head",
            )
            self.assertEqual(s_absent0.outcome, "dropped")
            self.assertEqual(s_absent0.drop_reason, "absent")
            self.assertIsNone(explorer.last_valid_frames[key])

            # 2. Clean publication of frame 0 -> accepted and stored as last_valid_frame
            rgb0 = make_deterministic_rgb(4, 2, frame_seq=0, seed=0x123456)
            s_clean0 = explorer.explore_bank_preemption(
                tok,
                rgb0,
                frame_seq=0,
                stage="head_update",
                fault="clean",
            )
            self.assertEqual(s_clean0.outcome, "accepted")
            self.assertEqual(s_clean0.accepted_seq, 0)
            valid0 = explorer.last_valid_frames[key]
            self.assertIsNotNone(valid0)
            assert valid0 is not None
            self.assertTrue(
                verify_immutable_frame_ref(
                    valid0, expected_handle=h, expected_epoch=0, expected_seq=0, expected_rgb=rgb0
                )
            )
            with self.assertRaises(ValueError):
                valid0.rgb.setflags(write=True)

            # 3. Enumerate each faulty preemption stage; each must reject and preserve valid0
            cases = [
                ("begin", "odd_sequence", "torn"),
                ("payload", "payload_tear", "torn"),
                ("slot_seal", "unequal_sequence", "torn"),
                ("slot_seal", "overwritten_sequence", "lapped"),
                ("head_update", "unadvanced_head", "absent"),
                ("head_update", "head_regressed", "torn"),
                ("copied_read_revalidation", "corrupt_crc", "torn"),
            ]
            for seq_idx, (stage, fault, expected_reason) in enumerate(cases, start=1):
                with self.subTest(stage=stage, fault=fault):
                    cand = make_deterministic_rgb(4, 2, frame_seq=seq_idx, seed=0x999)
                    step = explorer.explore_bank_preemption(
                        tok,
                        cand,
                        frame_seq=seq_idx,
                        stage=stage,
                        fault=fault,
                    )
                    self.assertEqual(step.outcome, "dropped")
                    self.assertEqual(step.drop_reason, expected_reason)
                    self.assertEqual(step.torn_observed, 0)
                    self.assertEqual(step.preserved_seq, 0)
                    self.assertEqual(step.preserved_digest, valid0.digest)
                    self.assertIs(explorer.last_valid_frames[key], valid0)
                    np.testing.assert_array_equal(valid0.rgb, rgb0)

            # 4. Concurrent resize during copied_read_revalidation -> Drop("stale_epoch"), preserves valid0
            step_stale = explorer.explore_bank_preemption(
                tok,
                make_deterministic_rgb(4, 2, frame_seq=8),
                frame_seq=8,
                stage="copied_read_revalidation",
                fault="stale_epoch_resize",
                resize_spec=spec,
            )
            self.assertEqual(step_stale.outcome, "dropped")
            self.assertEqual(step_stale.drop_reason, "stale_epoch")
            self.assertIs(explorer.last_valid_frames[key], valid0)

            tok_new = explorer.active_tokens[key]
            self.assertEqual(tok_new.epoch, 1)
            rgb1 = make_deterministic_rgb(4, 2, frame_seq=1, seed=0x123456)
            step_clean1 = explorer.explore_bank_preemption(
                tok_new,
                rgb1,
                frame_seq=1,
                stage="head_update",
                fault="clean",
            )
            self.assertEqual(step_clean1.outcome, "accepted")
            self.assertEqual(step_clean1.accepted_seq, 1)
            self.assertEqual(explorer.last_valid_frames[key].frame_seq, 1)
            # Previously retained valid0 is still intact and immutable
            np.testing.assert_array_equal(valid0.rgb, rgb0)

            explorer.close_stream(tok_new)
            self.assertEqual(explorer.bank.reserved_bytes, 0)
            self.assertEqual(explorer.torn_reads_delivered, 0)

    def test_close_resize_and_retained_view_accounting_without_mapped_files(self) -> None:
        with _ForbiddenLiveIOAndMappedPixelGuard():
            unit_small = rtring.ring_bytes(2, 4, 2)
            unit_large = rtring.ring_bytes(3, 6, 4)
            explorer = SeqlockScheduleExplorer(
                quota_bytes=2 * unit_small + unit_large, seed=77
            )
            h = Handle("x11_window", "win/accounting", 1)
            key = canonical_handle_key(h)
            spec_small = StreamSpec(4, 2, 2, unit_small)
            spec_large = StreamSpec(6, 4, 3, unit_large)

            # 1. Open small stream
            tok0 = explorer.open_stream(h, spec_small)
            self.assertEqual(explorer.bank.reserved_bytes, unit_small)
            self.assertEqual(explorer.per_target_reserved[key], unit_small)
            self.assertEqual(explorer.per_target_retained[key], 0)

            # 2. Retain an aliasing numpy slot view on tok0's FakeRingBacking, then resize to spec_large
            backing0 = explorer.backings[(key, 0)]
            aliasing_view0 = backing0.retain_slot_view(0)
            tok1 = explorer.resize_stream(tok0, spec_large)
            self.assertEqual(tok1.epoch, 1)
            self.assertFalse(backing0._released)
            self.assertEqual(explorer.bank.reserved_bytes, unit_small + unit_large)
            self.assertEqual(explorer.per_target_reserved[key], unit_small + unit_large)
            self.assertEqual(explorer.per_target_retained[key], unit_small)
            self.assertEqual(explorer.retry_cleanup(), 0)

            # 3. Drop aliasing_view0 and run retry_cleanup -> unit_small is freed!
            del aliasing_view0
            freed = explorer.retry_cleanup()
            self.assertEqual(freed, 1)
            self.assertTrue(backing0._released)
            self.assertEqual(explorer.bank.reserved_bytes, unit_large)
            self.assertEqual(explorer.per_target_reserved[key], unit_large)
            self.assertEqual(explorer.per_target_retained[key], 0)

            # 4. Close tok1 while holding an aliasing numpy view on backing1
            backing1 = explorer.backings[(key, 1)]
            aliasing_view1 = backing1.retain_slot_view(1)
            explorer.close_stream(tok1)
            self.assertFalse(backing1._released)
            self.assertEqual(explorer.bank.reserved_bytes, unit_large)
            self.assertEqual(explorer.per_target_retained[key], unit_large)

            # 5. Drop aliasing_view1 and retry_cleanup -> reserved_bytes returns to 0
            del aliasing_view1
            self.assertEqual(explorer.retry_cleanup(), 1)
            self.assertTrue(backing1._released)
            self.assertEqual(explorer.bank.reserved_bytes, 0)
            self.assertEqual(explorer.per_target_reserved[key], 0)
            self.assertEqual(explorer.per_target_retained[key], 0)

            ops = [d.operation for d in explorer.per_target_deltas[key]]
            self.assertEqual(
                ops,
                [
                    "open",
                    "resize_retained",
                    "retry_cleanup",
                    "close_retained",
                    "retry_cleanup",
                ],
            )
            self.assertEqual(sum(d.delta_bytes for d in explorer.per_target_deltas[key]), 0)

    def test_file_free_rtring_seam_and_ramws_invariants(self) -> None:
        with _ForbiddenLiveIOAndMappedPixelGuard():
            with InMemoryRingSeam() as seam:
                ws = FakeRamWorkspace(seam, quota_bytes=3 * rtring.ring_bytes(2, 4, 2))

                # Odd _seq0 raises ValueError("seq0 must be even") at vadapt/rtring.py:88
                with self.assertRaises(ValueError) as ctx_odd:
                    rtring.RingWriter(ws, 2, 4, 2, rel="odd/frames.rgb24", _seq0=1)
                self.assertEqual(str(ctx_odd.exception), "seq0 must be even")
                self.assertEqual(ws.used, 0)

                # Path escape outside workspace root raises RamError("E_RAM_ESCAPE")
                for bad_rel in ("../escape.rgb24", "/abs/escape.rgb24", "a/../../b", "bad\0name"):
                    with self.assertRaises(ramws.RamError) as ctx_esc:
                        ws.path(bad_rel)
                    self.assertEqual(ctx_esc.exception.code, "E_RAM_ESCAPE")

                wr = rtring.RingWriter(ws, 2, 4, 2, rel="ring/frames.rgb24", _seq0=4)
                rd = rtring.RingReader(wr.path, root=ws.root, verify=True)
                wrapper = LocalRingWriterWrapper(wr)

                # O_EXCL collision before quota reservation
                before_collision = ws.used
                with self.assertRaises(FileExistsError):
                    rtring.RingWriter(ws, 2, 4, 2, rel="ring/frames.rgb24")
                self.assertEqual(ws.used, before_collision)

                # Stage-by-stage pause hooks via LocalRingWriterWrapper
                paused_observations: list[tuple[str, int, int, bool]] = []
                frame0 = make_deterministic_rgb(4, 2, frame_seq=0)

                def _on_pause(stage: str, k: int) -> None:
                    slot_seq = rtring._Q.unpack_from(wr._mm, rtring.HDR + k * rtring.SLOT_HDR)[0]
                    head_now = rd.head()
                    paused_observations.append((stage, slot_seq, head_now, bool(slot_seq & 1)))

                wrapper.publish_with_pause(
                    frame0, 0, pause_after="begin", on_pause=_on_pause
                )
                self.assertEqual(paused_observations[-1], ("begin", 5, -1, True))

                frame1 = make_deterministic_rgb(4, 2, frame_seq=1)
                wrapper.publish_with_pause(
                    frame1, 1, pause_after="slot_seal", on_pause=_on_pause
                )
                # At slot_seal, slot 1 sequence is even (6), but global head is still 0!
                self.assertEqual(paused_observations[-1], ("slot_seal", 6, 0, False))
                self.assertEqual(rd.head(), 1)

                # RingReader.read(fn) with post-use Frame.valid() check
                res = rd.read(lambda view: int(view[0, 0, 0]))
                self.assertEqual(res, (int(frame1[0, 0, 0]), 1))

                # Close reader then verify closed reader operations raise ValueError("mmap closed or invalid")
                rd.close()
                for op in (lambda: rd.poll(), lambda: rd.acquire(0), lambda: rd.head()):
                    with self.assertRaises(ValueError) as ctx_rd:
                        op()
                    self.assertEqual(str(ctx_rd.exception), "mmap closed or invalid")

                wr.close()
                self.assertTrue(wr._released)
                self.assertEqual(ws.used, 0)
                self.assertEqual(len(seam.files), 0)

    def test_bounded_schedule_trace_and_reproducible_seed_determinism(self) -> None:
        with _ForbiddenLiveIOAndMappedPixelGuard():
            with self.assertRaises(ValueError):
                BoundedScheduleTrace(capacity=0)
            with self.assertRaises(ValueError):
                BoundedScheduleTrace(capacity=True)  # type: ignore[arg-type]

            rep1 = run_deterministic_exploration(
                seed=0x5E010C42, trace_capacity=12, random_steps=20
            )
            rep2 = run_deterministic_exploration(
                seed=0x5E010C42, trace_capacity=12, random_steps=20
            )
            rep_diff = run_deterministic_exploration(
                seed=0xABCDEF01, trace_capacity=12, random_steps=20
            )

            self.assertEqual(rep1.seed, 0x5E010C42)
            self.assertEqual(rep1.torn_reads_delivered, 0)
            self.assertEqual(rep1.final_reserved_bytes, 0)
            self.assertEqual(rep1.timing_env, "VM")
            self.assertEqual(rep1.host_timing_ns, AUSENTE)
            self.assertEqual(rep1.trace_capacity, 12)
            self.assertEqual(len(rep1.trace), 12)
            self.assertEqual(rep1.trace_total_recorded, rep1.steps_executed)
            self.assertEqual(rep1.trace_evicted, rep1.steps_executed - 12)
            self.assertGreater(rep1.trace_evicted, 0)

            # Exact determinism across runs with identical seed
            self.assertEqual(rep1.trace, rep2.trace)
            self.assertEqual(rep1.accepted_count, rep2.accepted_count)
            self.assertEqual(rep1.rejected_count, rep2.rejected_count)
            self.assertEqual(rep1.per_target_deltas, rep2.per_target_deltas)
            self.assertEqual(rep1.per_target_net_bytes, rep2.per_target_net_bytes)
            for k in rep1.last_valid_frames:
                f1 = rep1.last_valid_frames[k]
                f2 = rep2.last_valid_frames[k]
                assert f1 is not None and f2 is not None
                self.assertEqual(f1.digest, f2.digest)
                self.assertEqual(f1.frame_seq, f2.frame_seq)

            # Different seed produces a distinct schedule trace
            self.assertNotEqual(
                [(s.stage, s.fault, s.handle) for s in rep1.trace],
                [(s.stage, s.fault, s.handle) for s in rep_diff.trace],
            )

    def test_kill_named_catalogue_mutant_m_vadapt_window_stream_bank_1_and_seqlock_mutants(self) -> None:
        harness = _ScopedAstMutationHarness()

        # 1. Named catalogue mutant M-vadapt-window-stream-bank-1:
        # drop check in WindowStreamBank._staging at vadapt/window_stream_bank.py:258; anchor `raw is None`
        def _is_raw_is_none_if(node: ast.AST) -> bool:
            return (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "raw"
                and len(node.test.ops) == 1
                and isinstance(node.test.ops[0], ast.Is)
                and len(node.test.comparators) == 1
                and isinstance(node.test.comparators[0], ast.Constant)
                and node.test.comparators[0].value is None
            )

        def _drop_raw_is_none_check(node: ast.If) -> int:
            node.test = ast.Constant(value=False)
            return 1

        def _invert_raw_is_none_check(node: ast.If) -> int:
            assert isinstance(node.test, ast.Compare)
            node.test.ops[0] = ast.IsNot()
            return 1

        def _probe_m_vadapt_wsb_1(mod: types.ModuleType) -> None:
            size = rtring.ring_bytes(2, 3, 2)
            h_ok = mod.Handle("cdp_tab", "tab/ok", 1)
            h_bad = mod.Handle("cdp_tab", "tab/null-buf", 1)
            spec = mod.StreamSpec(3, 2, 2, size)

            # Positive control: valid owner with writable bytearray buffer succeeds
            bank_ok = mod.WindowStreamBank(
                quota_bytes=size,
                ring_factory=lambda _h, _s: types.SimpleNamespace(
                    buffer=bytearray(size), close=lambda: True
                ),
            )
            try:
                tok = bank_ok.open(h_ok, spec)
            except BaseException as exc:  # noqa: BLE001
                self.fail(f"valid open unexpectedly failed with {type(exc).__name__}: {exc}")
            self.assertEqual(bank_ok.reserved_bytes, size)
            bank_ok.close(tok)
            self.assertEqual(bank_ok.reserved_bytes, 0)

            # Negative control: owner is not None, but owner.buffer is None -> raw is None
            bank_bad = mod.WindowStreamBank(
                quota_bytes=size,
                ring_factory=lambda _h, _s: types.SimpleNamespace(buffer=None),
            )
            try:
                bank_bad.open(h_bad, spec)
            except mod.StreamError as exc:
                self.assertEqual(str(exc), "ring_factory returned no writable buffer")
            except BaseException as exc:  # noqa: BLE001
                self.fail(
                    f"expected StreamError('ring_factory returned no writable buffer'), "
                    f"got {type(exc).__name__}: {exc}"
                )
            else:
                self.fail("expected StreamError('ring_factory returned no writable buffer')")
            self.assertEqual(bank_bad.reserved_bytes, 0)

        out_m1_drop = harness.evaluate_mutant(
            mutant_id="M-vadapt-window-stream-bank-1:drop",
            module=wsb_mod,
            qualified_scope="vadapt.window_stream_bank.WindowStreamBank._staging",
            containing_expr_predicate=_is_raw_is_none_if,
            structural_mutator=_drop_raw_is_none_check,
            probe=_probe_m_vadapt_wsb_1,
        )
        self.assertEqual(out_m1_drop.status, "KILLED", msg=out_m1_drop.detail)

        out_m1_inv = harness.evaluate_mutant(
            mutant_id="M-vadapt-window-stream-bank-1:invert",
            module=wsb_mod,
            qualified_scope="vadapt.window_stream_bank.WindowStreamBank._staging",
            containing_expr_predicate=_is_raw_is_none_if,
            structural_mutator=_invert_raw_is_none_check,
            probe=_probe_m_vadapt_wsb_1,
        )
        self.assertEqual(out_m1_inv.status, "KILLED", msg=out_m1_inv.detail)

        # 2. Kill odd-sequence check mutant in WindowStreamBank.read (`if seq1 & 1:`)
        def _is_seq1_odd_if(node: ast.AST) -> bool:
            return (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.BinOp)
                and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "seq1"
                and isinstance(node.test.op, ast.BitAnd)
            )

        def _disable_seq1_odd(node: ast.If) -> int:
            node.test = ast.Constant(value=False)
            return 1

        def _probe_wsb_odd_seq(mod: types.ModuleType) -> None:
            size = rtring.ring_bytes(2, 3, 2)
            ring = bytearray(size)
            bank = mod.WindowStreamBank(quota_bytes=size, ring_factory=lambda _h, _s: ring)
            h = mod.Handle("cdp_tab", "tab/odd", 1)
            s = mod.StreamSpec(3, 2, 2, size)
            tok = bank.open(h, s)
            pix = np.full((2, 3, 3), 19, dtype=np.uint8)
            bank.publish(tok, pix, frame_seq=0)
            self.assertIsInstance(bank.read(tok), mod.FrameRef)
            # Set slot 0 sequence to odd (3) prior to read; pre-copy guard must skip before step_hook
            rtring._Q.pack_into(ring, rtring.HDR, 3)
            hook_calls: list[str] = []
            got = bank.read(tok, step_hook=hook_calls.append)
            self.assertIsInstance(got, mod.Drop)
            self.assertEqual(got.reason, "torn")
            self.assertEqual(hook_calls, [])

        out_odd = harness.evaluate_mutant(
            mutant_id="M-wsb-read-odd-seq",
            module=wsb_mod,
            qualified_scope="vadapt.window_stream_bank.WindowStreamBank.read",
            containing_expr_predicate=_is_seq1_odd_if,
            structural_mutator=_disable_seq1_odd,
            probe=_probe_wsb_odd_seq,
        )
        self.assertEqual(out_odd.status, "KILLED", msg=out_odd.detail)

        # 3. Kill CRC revalidation mutant in WindowStreamBank.read (`zlib.crc32(raw) != crc1`)
        def _is_crc_compare(node: ast.AST) -> bool:
            return (
                isinstance(node, ast.Compare)
                and len(node.ops) == 1
                and isinstance(node.ops[0], ast.NotEq)
                and isinstance(node.left, ast.Call)
                and isinstance(node.left.func, ast.Attribute)
                and node.left.func.attr == "crc32"
                and len(node.comparators) == 1
                and isinstance(node.comparators[0], ast.Name)
                and node.comparators[0].id == "crc1"
            )

        def _disable_crc_compare(node: ast.Compare) -> int:
            node.ops[0] = ast.Eq()
            return 1

        def _probe_wsb_crc(mod: types.ModuleType) -> None:
            size = rtring.ring_bytes(2, 3, 2)
            ring = bytearray(size)
            bank = mod.WindowStreamBank(quota_bytes=size, ring_factory=lambda _h, _s: ring)
            h = mod.Handle("cdp_tab", "tab/crc", 1)
            s = mod.StreamSpec(3, 2, 2, size)
            tok = bank.open(h, s)
            pix = np.full((2, 3, 3), 55, dtype=np.uint8)
            bank.publish(tok, pix, frame_seq=0)
            self.assertIsInstance(bank.read(tok), mod.FrameRef)
            p_off = rtring.data_off(2)
            ring[p_off] ^= 0xFF
            got = bank.read(tok)
            self.assertIsInstance(got, mod.Drop)
            self.assertEqual(got.reason, "torn")

        out_crc = harness.evaluate_mutant(
            mutant_id="M-wsb-read-crc32",
            module=wsb_mod,
            qualified_scope="vadapt.window_stream_bank.WindowStreamBank.read",
            containing_expr_predicate=_is_crc_compare,
            structural_mutator=_disable_crc_compare,
            probe=_probe_wsb_crc,
        )
        self.assertEqual(out_crc.status, "KILLED", msg=out_crc.detail)

        # 4. Kill rtring Frame.valid mutant (`self._reader._seq(self._k) == self._seq`)
        def _is_frame_valid_return(node: ast.AST) -> bool:
            return isinstance(node, ast.Return) and isinstance(node.value, ast.Compare)

        def _invert_frame_valid(node: ast.Return) -> int:
            assert isinstance(node.value, ast.Compare)
            node.value.ops[0] = ast.NotEq()
            return 1

        def _probe_rtring_valid(mod: types.ModuleType) -> None:
            with InMemoryRingSeam() as seam:
                ws = FakeRamWorkspace(seam, quota_bytes=2 * mod.ring_bytes(2, 4, 2))
                wr = mod.RingWriter(ws, 2, 4, 2)
                rd = mod.RingReader(wr.path, root=ws.root)
                wr.publish(np.full((2, 4, 3), 9, dtype=np.uint8), 0)
                f = rd.poll()
                self.assertIsNotNone(f)
                self.assertTrue(f.valid())
                wr._begin(2)
                self.assertFalse(f.valid())
                del f
                rd.close()
                wr.close()

        out_rtring = harness.evaluate_mutant(
            mutant_id="M-rtring-frame-valid",
            module=rtring,
            qualified_scope="vadapt.rtring.Frame.valid",
            containing_expr_predicate=_is_frame_valid_return,
            structural_mutator=_invert_frame_valid,
            probe=_probe_rtring_valid,
        )
        self.assertEqual(out_rtring.status, "KILLED", msg=out_rtring.detail)

        # 5. Kill explorer digest verification mutant in verify_immutable_frame_ref
        def _is_digest_compare(node: ast.AST) -> bool:
            return (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare)
                and len(node.test.ops) == 1
                and isinstance(node.test.ops[0], ast.NotEq)
                and len(node.test.comparators) == 1
                and isinstance(node.test.comparators[0], ast.Attribute)
                and node.test.comparators[0].attr == "digest"
            )

        def _disable_digest_check(node: ast.If) -> int:
            node.test = ast.Constant(value=False)
            return 1

        def _probe_explorer_digest(mod: types.ModuleType) -> None:
            h = Handle("cdp_tab", "tab/d", 0)
            arr = make_deterministic_rgb(4, 2, frame_seq=0)
            raw = arr.tobytes()
            ro = np.frombuffer(raw, dtype=np.uint8).reshape(2, 4, 3)
            good_ref = FrameRef(h, 0, 0, 4, 2, ro, hashlib.sha256(raw).hexdigest())
            bad_ref = FrameRef(h, 0, 0, 4, 2, ro, "0" * 64)
            self.assertTrue(mod.verify_immutable_frame_ref(good_ref))
            self.assertFalse(mod.verify_immutable_frame_ref(bad_ref))

        out_exp = harness.evaluate_mutant(
            mutant_id="M-explorer-digest-check",
            module=explorer_mod,
            qualified_scope="eval.v2eval.seqlock_schedule_explorer.verify_immutable_frame_ref",
            containing_expr_predicate=_is_digest_compare,
            structural_mutator=_disable_digest_check,
            probe=_probe_explorer_digest,
        )
        self.assertEqual(out_exp.status, "KILLED", msg=out_exp.detail)
        self.assertEqual(harness.killed_count, 6)

    def test_mutation_harness_separates_non_kill_outcomes_from_killed_numerator(self) -> None:
        harness = _ScopedAstMutationHarness()

        # 1. Failed positive control never enters killed numerator
        out_fp = harness.evaluate_mutant(
            mutant_id="sep-failed-positive",
            module=wsb_mod,
            qualified_scope="vadapt.window_stream_bank.WindowStreamBank._staging",
            containing_expr_predicate=lambda n: False,
            structural_mutator=lambda n: 1,
            probe=lambda _mod: self.fail("intentional positive failure"),
        )
        self.assertEqual(out_fp.status, "FAILED_POSITIVE")
        self.assertFalse(out_fp.positive_passed)
        self.assertFalse(out_fp.killed)

        # 2. Missing anchor stays separate
        out_missing = harness.evaluate_mutant(
            mutant_id="sep-missing-anchor",
            module=wsb_mod,
            qualified_scope="vadapt.window_stream_bank.WindowStreamBank.nonexistent_method",
            containing_expr_predicate=lambda n: True,
            structural_mutator=lambda n: 1,
            probe=lambda _mod: None,
        )
        self.assertEqual(out_missing.status, "MISSING_ANCHOR")

        # 3. Nonunique anchor stays separate
        out_nonunique = harness.evaluate_mutant(
            mutant_id="sep-nonunique-anchor",
            module=wsb_mod,
            qualified_scope="vadapt.window_stream_bank.WindowStreamBank._staging",
            containing_expr_predicate=lambda n: isinstance(n, ast.Name),
            structural_mutator=lambda n: 1,
            probe=lambda _mod: None,
        )
        self.assertEqual(out_nonunique.status, "NONUNIQUE_ANCHOR")

        # 4. Unrelated exception stays separate from AssertionError kill
        out_unrel = harness.evaluate_mutant(
            mutant_id="sep-unrelated-exc",
            module=explorer_mod,
            qualified_scope="eval.v2eval.seqlock_schedule_explorer.canonical_handle_key",
            containing_expr_predicate=lambda n: isinstance(n, ast.Return),
            structural_mutator=lambda n: setattr(
                n,
                "value",
                ast.Call(func=ast.Name(id="RuntimeError", ctx=ast.Load()), args=[], keywords=[]),
            )
            or 1,
            probe=lambda mod: (
                self.assertEqual(
                    mod.canonical_handle_key(Handle("cdp_tab", "x", 0)),
                    ("cdp_tab", "x", 0),
                )
                if isinstance(
                    res := mod.canonical_handle_key(Handle("cdp_tab", "x", 0)), tuple
                )
                else (_ for _ in ()).throw(res)
            ),
        )
        self.assertEqual(out_unrel.status, "UNRELATED_EXCEPTION")

        # 5. Equivalent mutant stays separate
        out_equiv = harness.evaluate_mutant(
            mutant_id="sep-equivalent",
            module=explorer_mod,
            qualified_scope="eval.v2eval.seqlock_schedule_explorer.canonical_handle_key",
            containing_expr_predicate=lambda n: isinstance(n, ast.Return),
            structural_mutator=lambda n: 1,
            probe=lambda _mod: None,
            equivalent=True,
        )
        self.assertEqual(out_equiv.status, "EQUIVALENT")

        # 6. Survived mutant stays separate
        out_surv = harness.evaluate_mutant(
            mutant_id="sep-survived",
            module=explorer_mod,
            qualified_scope="eval.v2eval.seqlock_schedule_explorer.canonical_handle_key",
            containing_expr_predicate=lambda n: isinstance(n, ast.Return),
            structural_mutator=lambda n: 1,
            probe=lambda _mod: None,
        )
        self.assertEqual(out_surv.status, "SURVIVED")

        # None of the 6 non-kill outcomes entered killed_count!
        self.assertEqual(harness.killed_count, 0)

    def test_seeded_random_property_loop_64_cases_with_oracle_and_shrinker(self) -> None:
        with _ForbiddenLiveIOAndMappedPixelGuard():
            rng = random.Random(PROPERTY_SEED)
            completed_cases = 0

            for case_idx in range(PROPERTY_TARGET_CASES):
                case_seed = rng.randint(1, (1 << 31) - 1)
                case_rng = random.Random(case_seed)
                slots = case_rng.randint(1, 4)
                width = case_rng.randint(2, 8)
                height = case_rng.randint(2, 6)
                unit = rtring.ring_bytes(slots, width, height)

                explorer = SeqlockScheduleExplorer(
                    quota_bytes=3 * unit,
                    seed=case_seed,
                    trace_capacity=16,
                )
                oracle = _IndependentSeqlockOracle(quota_bytes=3 * unit)

                kind = case_rng.choice(sorted(KINDS))
                handle = Handle(kind, f"target/{case_idx}", case_rng.randint(1, 5))
                spec = StreamSpec(width, height, slots, unit)

                tok = explorer.open_stream(handle, spec)
                oracle.open_target(handle, unit)
                self.assertEqual(explorer.bank.reserved_bytes, oracle.reserved_bytes)

                # Publish initial frame 0 so slot 0 has a valid baseline
                rgb0 = make_deterministic_rgb(width, height, frame_seq=0, seed=case_seed)
                step0 = explorer.explore_bank_preemption(
                    tok, rgb0, frame_seq=0, stage="head_update", fault="clean"
                )
                o_out0, o_reason0, o_seq0, o_dig0 = oracle.step(
                    handle, rgb0, frame_seq=0, stage="head_update", fault="clean"
                )
                self.assertEqual(step0.outcome, o_out0)
                self.assertIsNone(o_reason0)
                self.assertEqual(step0.preserved_seq, o_seq0)
                self.assertEqual(step0.preserved_digest, o_dig0)

                next_seq = 1
                n_ops = case_rng.randint(4, 8)
                for op_idx in range(n_ops):
                    stage, fault = case_rng.choice(
                        (
                            ("head_update", "clean"),
                            ("begin", "odd_sequence"),
                            ("payload", "payload_tear"),
                            ("slot_seal", "unequal_sequence"),
                            ("slot_seal", "overwritten_sequence"),
                            ("head_update", "unadvanced_head"),
                            ("head_update", "head_regressed"),
                            ("copied_read_revalidation", "corrupt_crc"),
                        )
                    )
                    rgb = make_deterministic_rgb(
                        width, height, frame_seq=next_seq, seed=case_seed + op_idx
                    )
                    step = explorer.explore_bank_preemption(
                        tok,
                        rgb,
                        frame_seq=next_seq,
                        stage=stage,
                        fault=fault,
                    )
                    o_out, o_reason, o_seq, o_dig = oracle.step(
                        handle, rgb, frame_seq=next_seq, stage=stage, fault=fault
                    )
                    diag = (
                        f"replay_seed={case_seed:#x} case={case_idx} op={op_idx} "
                        f"stage={stage} fault={fault}"
                    )
                    self.assertEqual(step.outcome, o_out, msg=diag)
                    self.assertEqual(step.drop_reason, o_reason, msg=diag)
                    self.assertEqual(step.preserved_seq, o_seq, msg=diag)
                    self.assertEqual(step.preserved_digest, o_dig, msg=diag)
                    self.assertEqual(step.torn_observed, 0, msg=diag)
                    if step.outcome == "accepted":
                        next_seq += 1

                explorer.close_stream(tok)
                self.assertEqual(explorer.bank.reserved_bytes, 0)
                self.assertEqual(explorer.torn_reads_delivered, 0)
                completed_cases += 1

            self.assertEqual(completed_cases, PROPERTY_TARGET_CASES)

            # Verify smallest counterexample shrinking metadata on a synthetic failing predicate
            sample_schedule = [
                ("head_update", "clean"),
                ("begin", "odd_sequence"),
                ("payload", "payload_tear"),
                ("copied_read_revalidation", "corrupt_crc"),
                ("head_update", "clean"),
            ]
            shrunk = shrink_schedule_counterexample(
                sample_schedule,
                lambda seq: ("payload", "payload_tear") in seq,
                replay_seed=PROPERTY_SEED,
                case_index=7,
            )
            self.assertEqual(shrunk["replay_seed"], PROPERTY_SEED)
            self.assertEqual(shrunk["case_index"], 7)
            self.assertEqual(shrunk["original_length"], 5)
            self.assertEqual(shrunk["minimal_length"], 1)
            self.assertEqual(shrunk["minimal_schedule"], (("payload", "payload_tear"),))

    def test_concurrency_32_schedules_with_barriers_bounded_joins_and_leak_detection(self) -> None:
        baseline_threads = {t.ident for t in threading.enumerate()}
        completed_schedules: list[tuple[int, tuple[str, ...]]] = []

        for sched_idx in range(CONCURRENCY_TARGET_SCHEDULES):
            unit = rtring.ring_bytes(2, 4, 2)
            explorer = SeqlockScheduleExplorer(
                quota_bytes=4 * unit,
                seed=DEFAULT_SEED + sched_idx,
                trace_capacity=32,
            )
            h_a = Handle("cdp_tab", f"tab/conc-{sched_idx}-a", 1)
            h_b = Handle("x11_window", f"win/conc-{sched_idx}-b", 1)
            spec = StreamSpec(4, 2, 2, unit)
            tok_a, tok_b = explorer.open_many(((h_a, spec), (h_b, spec)))

            f0_a = make_deterministic_rgb(4, 2, frame_seq=0, seed=sched_idx)
            f0_b = make_deterministic_rgb(4, 2, frame_seq=0, seed=sched_idx + 100)
            explorer.publish_valid_frame(tok_a, f0_a, frame_seq=0)
            explorer.publish_valid_frame(tok_b, f0_b, frame_seq=0)

            mode = sched_idx % 4
            num_threads = 3 if mode in (0, 1) else MAX_CONCURRENCY_THREADS
            self.assertLessEqual(num_threads, MAX_CONCURRENCY_THREADS)

            start_barrier = threading.Barrier(num_threads, timeout=SYNC_TIMEOUT_S)
            hook_entered = threading.Event()
            writer_acted = threading.Event()
            schedule_events: list[str] = []
            ev_lock = threading.Lock()
            errors: list[BaseException] = []
            threads: list[threading.Thread] = []

            def _log(tag: str) -> None:
                with ev_lock:
                    schedule_events.append(tag)

            def _worker(tid: int, *, _mode: int = mode, _idx: int = sched_idx) -> None:
                try:
                    start_barrier.wait(timeout=SYNC_TIMEOUT_S)
                    if _mode == 0:
                        if tid == 0:
                            def _hook(phase: str) -> None:
                                if phase == "before_copy":
                                    _log("r0:before_copy")
                                    hook_entered.set()
                                    assert writer_acted.wait(timeout=SYNC_TIMEOUT_S)

                            got = explorer.bank.read(tok_a, step_hook=_hook)
                            assert isinstance(got, FrameRef) and verify_immutable_frame_ref(
                                got, expected_rgb=f0_a
                            )
                            _log("r0:accepted")
                        elif tid == 1:
                            assert hook_entered.wait(timeout=SYNC_TIMEOUT_S)
                            f1_b = make_deterministic_rgb(4, 2, frame_seq=1, seed=_idx + 200)
                            explorer.bank.publish(tok_b, f1_b, frame_seq=1)
                            _log("w1:published_b1")
                            writer_acted.set()
                        else:
                            assert writer_acted.wait(timeout=SYNC_TIMEOUT_S)
                            got_b = explorer.bank.read(tok_b)
                            assert isinstance(got_b, FrameRef) and got_b.frame_seq == 1
                            _log("r2:read_b1")

                    elif _mode == 1:
                        if tid == 0:
                            def _hook_resize(phase: str) -> None:
                                if phase == "after_copy":
                                    _log("r0:after_copy")
                                    hook_entered.set()
                                    assert writer_acted.wait(timeout=SYNC_TIMEOUT_S)

                            got = explorer.bank.read(tok_a, step_hook=_hook_resize)
                            assert isinstance(got, Drop) and got.reason == "stale_epoch"
                            _log("r0:dropped_stale_epoch")
                        elif tid == 1:
                            assert hook_entered.wait(timeout=SYNC_TIMEOUT_S)
                            explorer.resize_stream(tok_a, spec)
                            _log("w1:resized_a")
                            writer_acted.set()
                        else:
                            assert writer_acted.wait(timeout=SYNC_TIMEOUT_S)
                            with self.assertRaises(StaleStreamError):
                                explorer.bank.read(tok_a)
                            _log("r2:stale_token_refused")

                    elif _mode == 2:
                        backing_a = explorer.backings[(canonical_handle_key(h_a), tok_a.epoch)]
                        saved = bytes(backing_a.buffer)
                        if tid == 0:
                            calls = [0]

                            def _hook_tear(phase: str) -> None:
                                if phase == "before_copy":
                                    calls[0] += 1
                                    if calls[0] == 1:
                                        _log("r0:before_copy_tear")
                                        hook_entered.set()
                                        assert writer_acted.wait(timeout=SYNC_TIMEOUT_S)

                            got = explorer.bank.read(tok_a, step_hook=_hook_tear)
                            backing_a.buffer[:] = saved
                            assert isinstance(got, Drop) and got.reason == "torn"
                            _log("r0:dropped_torn")
                        elif tid == 1:
                            assert hook_entered.wait(timeout=SYNC_TIMEOUT_S)
                            p_off = rtring.data_off(spec.slots)
                            backing_a.buffer[p_off] ^= 0xFF
                            _log("w1:corrupted_payload")
                            writer_acted.set()
                        else:
                            assert writer_acted.wait(timeout=SYNC_TIMEOUT_S)
                            got_b = explorer.bank.read(tok_b)
                            assert isinstance(got_b, FrameRef) and verify_immutable_frame_ref(
                                got_b, expected_rgb=f0_b
                            )
                            _log(f"r{tid}:read_b0_clean")

                    else:
                        if tid == 0:
                            f1_a = make_deterministic_rgb(4, 2, frame_seq=1, seed=_idx + 300)
                            explorer.bank.publish(tok_a, f1_a, frame_seq=1)
                            _log("w0:published_a1")
                            hook_entered.set()
                            writer_acted.set()
                        else:
                            assert hook_entered.wait(timeout=SYNC_TIMEOUT_S)
                            got_a = explorer.bank.read(tok_a)
                            assert isinstance(got_a, FrameRef) and verify_immutable_frame_ref(got_a)
                            _log(f"r{tid}:verified_a")
                except BaseException as exc:  # noqa: BLE001
                    with ev_lock:
                        errors.append(exc)
                    hook_entered.set()
                    writer_acted.set()

            try:
                for tid in range(num_threads):
                    t = threading.Thread(
                        target=_worker,
                        args=(tid,),
                        name=f"jq3-r4-g2-sched-{sched_idx}-t-{tid}",
                        daemon=False,
                    )
                    threads.append(t)
                    t.start()
                for t in threads:
                    t.join(timeout=SYNC_TIMEOUT_S)
            finally:
                hook_entered.set()
                writer_acted.set()
                try:
                    start_barrier.abort()
                except Exception:
                    pass
                for t in threads:
                    if t.is_alive():
                        t.join(timeout=SYNC_TIMEOUT_S)
                    self.assertFalse(
                        t.is_alive(), f"leaked thread {t.name} in schedule {sched_idx}"
                    )

            if errors:
                raise AssertionError(
                    f"concurrent schedule {sched_idx} failed with errors: {errors!r}"
                )

            cur_tok_a = explorer.active_tokens[canonical_handle_key(h_a)]
            explorer.close_stream(cur_tok_a)
            explorer.close_stream(tok_b)
            self.assertEqual(explorer.bank.reserved_bytes, 0)
            expected_events = num_threads + (1 if mode in (0, 1, 2) else 0)
            self.assertEqual(len(schedule_events), expected_events)
            completed_schedules.append((sched_idx, tuple(schedule_events)))

        self.assertEqual(len(completed_schedules), CONCURRENCY_TARGET_SCHEDULES)
        after_threads = {t.ident for t in threading.enumerate()}
        self.assertTrue(after_threads.issubset(baseline_threads))


if __name__ == "__main__":
    unittest.main()
