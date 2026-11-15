"""Unit tests for eval.v2eval.multiwin_scorecard (jq2a-m7-g1-vision-scorecard).

Exercises production public paths for score_windows, canonical_bytes, run_trial,
WindowEvidence, Scorecard, FakeClock, and all canonical frozen records across
healthy controls and named mutant distinctions (G-M7-1 through G-M7-5).
"""
from __future__ import annotations

import builtins
import copy
from dataclasses import FrozenInstanceError, fields
import hashlib
import json
import socket
import subprocess
import unittest
from unittest.mock import patch

import numpy as np

from eval.gui_synth.score import ScoreError
from eval.v2eval import multiwin_scorecard as mws
from eval.v2eval.multiwin_scorecard import (
    AUSENTE,
    ActionRequest,
    ActorLease,
    ChangeResult,
    FakeClock,
    FocusLease,
    FrameRef,
    FrameSequenceError,
    Handle,
    MANDATORY_METRIC_KEYS,
    METADATA_LOCK_ORDER,
    Observation,
    PerceptionResult,
    Receipt,
    SUPPORTED_TARGET_KINDS,
    Scenario,
    Scorecard,
    ScorecardValidationError,
    SealMismatchError,
    StreamSpec,
    StreamToken,
    TargetRecord,
    TerminalValidationError,
    Ticket,
    TrialReport,
    WindowDescription,
    WindowEvidence,
    WindowFrames,
    WindowIdentityError,
    canonical_actuation_resource,
    canonical_bytes,
    canonical_handle_key,
    run_trial,
    seal_trial,
    score_sealed_window_scenes,
    score_windows,
    validate_lock_order,
    validate_scorecard,
)


class _ForbiddenLiveIOGuard:
    """Context manager that fails any attempt to launch processes, open sockets, or write disk files."""

    def __init__(self) -> None:
        self._patches = []

    def __enter__(self):
        real_open = builtins.open

        def guarded_open(file, mode="r", *args, **kwargs):
            if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
                raise AssertionError(f"forbidden disk write attempted: {file!r} ({mode!r})")
            return real_open(file, mode, *args, **kwargs)

        def forbidden_popen(*args, **kwargs):
            raise AssertionError(f"forbidden process launch attempted: {args!r}")

        def forbidden_socket(*args, **kwargs):
            raise AssertionError("forbidden network socket attempted")

        self._patches = [
            patch.object(builtins, "open", side_effect=guarded_open),
            patch.object(subprocess, "Popen", side_effect=forbidden_popen),
            patch.object(subprocess, "run", side_effect=forbidden_popen),
            patch.object(socket, "socket", side_effect=forbidden_socket),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for p in reversed(self._patches):
            p.stop()
        return False


def _make_evidence(
    *,
    kind: str = "cdp_tab",
    canonical_id: str = "tab-1",
    generation: int = 1,
    scene: str = "DEV:scroll_small:0:4470e5acd95e3f29",
    epoch: int = 0,
    frame_seq: int = 0,
    tp: int = 12,
    fp: int = 2,
    fn: int = 3,
    tn: int = 25,
    ops: dict | None = None,
    frame_supported: bool = True,
    dropped: bool = False,
    exact: bool | None = True,
    terminal: str | None = "OK",
    expected_terminal: str | None = "OK",
    seal: str = "",
    violations: tuple = (),
) -> WindowEvidence:
    default_ops = {"decode": 1, "describe": 2, "change": 2, "fl": 4, "gm": 3,
                   "isolated_ops": {"decode": 1, "describe": 2, "change": 2, "fl": 4, "gm": 3}}
    return WindowEvidence(
        handle=Handle(kind, canonical_id, generation),
        scene=scene,
        epoch=epoch,
        frame_seq=frame_seq,
        tp=tp,
        fp=fp,
        fn=fn,
        tn=tn,
        ops=dict(default_ops if ops is None else ops),
        frame_supported=frame_supported,
        dropped=dropped,
        exact=exact,
        terminal=terminal,
        expected_terminal=expected_terminal,
        seal=seal,
        violations=violations,
    )


class TestMultiwinScorecardImplementation(unittest.TestCase):
    """Comprehensive tests for eval.v2eval.multiwin_scorecard."""

    def test_frozen_record_field_orders_and_immutability(self) -> None:
        expected_orders = {
            Scenario: ("name", "targets", "events", "expected_outcomes"),
            WindowEvidence: (
                "handle",
                "scene",
                "epoch",
                "frame_seq",
                "tp",
                "fp",
                "fn",
                "tn",
                "ops",
                "frame_supported",
                "dropped",
                "exact",
                "terminal",
                "expected_terminal",
                "seal",
                "violations",
            ),
            TrialReport: ("rows", "outcomes", "faults"),
            Scorecard: ("windows", "pooled", "metrics", "violations", "passed"),
            Handle: ("kind", "canonical_id", "generation"),
            TargetRecord: (
                "handle",
                "transport",
                "owned",
                "epoch",
                "session_id",
                "attachment_owned",
                "closed",
            ),
            FrameRef: ("handle", "epoch", "frame_seq", "width", "height", "rgb", "digest"),
            Observation: ("handle", "epoch", "frame_seq", "dpr_ppm", "state", "frame"),
            Receipt: ("action_id", "handle", "status", "reason"),
            ActionRequest: ("handle", "action_id", "epoch", "payload", "cost", "deadline_ns"),
            ActorLease: ("handle", "owner", "generation", "nonce", "expires_ns"),
            FocusLease: ("lease", "domain", "epoch"),
            StreamSpec: ("width", "height", "slots", "quota_bytes"),
            StreamToken: ("handle", "epoch"),
            Ticket: ("action_id", "handle"),
            ChangeResult: ("handle", "epoch", "settled", "claims", "ops"),
            PerceptionResult: ("handle", "observation", "error"),
            WindowFrames: ("handle", "epoch", "frames", "frame_seqs", "dpr_ppm"),
            WindowDescription: (
                "handle",
                "epoch",
                "frame_seqs",
                "descriptors",
                "regions",
                "error",
            ),
        }
        for cls, exp_names in expected_orders.items():
            with self.subTest(cls=cls.__name__):
                actual_names = tuple(f.name for f in fields(cls))
                self.assertEqual(actual_names, exp_names)

        h = Handle("cdp_tab", "tab-main", 1)
        with self.assertRaises(FrozenInstanceError):
            h.generation = 2  # type: ignore[misc]

        ev = _make_evidence()
        with self.assertRaises(FrozenInstanceError):
            ev.tp = 99  # type: ignore[misc]

    def test_fake_clock_monotonic_and_refuses_negative_increments(self) -> None:
        clock = FakeClock()
        self.assertEqual(clock.now_ns, 0)
        self.assertEqual(clock(), 0)
        self.assertEqual(clock.advance(250_000), 250_000)
        self.assertEqual(clock(), 250_000)
        self.assertEqual(clock.advance(0), 250_000)
        with self.assertRaises(ValueError):
            clock.advance(-1)
        with self.assertRaises(ValueError):
            clock.advance(True)  # type: ignore[arg-type]

    def test_gate_g_m7_4_positive_control_pooled_confusion_and_ratio_mutants(self) -> None:
        """G-M7-4: Pooled confusion equals sum, rejects mutant average ratios, allows same scene across windows."""
        seal = "seal-known-count-v1"
        shared_scene = "DEV:scroll_small:0:4470e5acd95e3f29"
        # Imbalanced per-window counts so pooled integer ratio != average of per-window ratios:
        # w1: recall = 1/4 (250 permille), precision = 1/2 (500 permille)
        # w2: recall = 19/20 (950 permille), precision = 19/20 (950 permille)
        # w3: recall = 10/12 (833 permille), precision = 10/13 (769 permille)
        # Macro-averaged recall permille = (250 + 950 + 833) // 3 = 677
        # True pooled recall = (1+19+10) / (4+20+12) = 30/36 -> 833 permille
        # Macro-averaged precision permille = (500 + 950 + 769) // 3 = 739
        # True pooled precision = 30 / (2+20+13) = 30/35 -> 857 permille
        w1 = _make_evidence(
            kind="cdp_tab",
            canonical_id="tab-1",
            scene=shared_scene,
            epoch=0,
            frame_seq=0,
            tp=1,
            fp=1,
            fn=3,
            tn=5,
            ops={"fl": 4, "gm": 2, "isolated_ops": {"fl": 4, "gm": 2}},
            seal=seal,
        )
        w2 = _make_evidence(
            kind="x11_window",
            canonical_id="win-1",
            scene=shared_scene,
            epoch=0,
            frame_seq=0,
            tp=19,
            fp=1,
            fn=1,
            tn=15,
            ops={"fl": 6, "gm": 3, "isolated_ops": {"fl": 6, "gm": 3}},
            seal=seal,
        )
        w3 = _make_evidence(
            kind="godot_window",
            canonical_id="gd-1",
            scene="DEV:caret_blink:0:9a8b7c6d5e4f3a2b",
            epoch=0,
            frame_seq=0,
            tp=10,
            fp=3,
            fn=2,
            tn=10,
            ops={"fl": 5, "gm": 4, "isolated_ops": {"fl": 5, "gm": 4}},
            seal=seal,
        )

        with _ForbiddenLiveIOGuard():
            card = score_windows(seal_trial(TrialReport(rows=tuple((w1, w2, w3)), outcomes={}, faults=())))
            validate_scorecard(card)

        self.assertTrue(card.passed, f"unexpected violations: {card.violations}")
        self.assertEqual(card.violations, ())
        self.assertEqual(set(card.metrics.keys()), set(MANDATORY_METRIC_KEYS))

        # Scene identities must never be renamed to work around duplicate-scene aggregation
        self.assertEqual(card.windows[0].scene, shared_scene)
        self.assertEqual(card.windows[1].scene, shared_scene)

        # Elementwise sum of confusion counts
        self.assertEqual(card.pooled["tp"], 30)
        self.assertEqual(card.pooled["fp"], 5)
        self.assertEqual(card.pooled["fn"], 6)
        self.assertEqual(card.pooled["tn"], 30)
        self.assertEqual(card.pooled["total"], 71)
        self.assertEqual(card.pooled["support"], 36)

        pc = card.metrics["pooled_confusion"]
        self.assertEqual(pc["numerator"], 30)
        self.assertEqual(pc["denominator"], 36)
        self.assertEqual(pc["recall"]["numerator"], 30)
        self.assertEqual(pc["recall"]["denominator"], 36)
        self.assertEqual(pc["recall"]["fraction"], "30/36")
        self.assertEqual(pc["recall"]["permille"], 833)
        self.assertNotEqual(pc["recall"]["permille"], 677)

        self.assertEqual(pc["precision"]["numerator"], 30)
        self.assertEqual(pc["precision"]["denominator"], 35)
        self.assertEqual(pc["precision"]["fraction"], "30/35")
        self.assertEqual(pc["precision"]["permille"], 857)
        self.assertNotEqual(pc["precision"]["permille"], 739)

        self.assertEqual(pc["f1"]["numerator"], 60)
        self.assertEqual(pc["f1"]["denominator"], 71)
        self.assertEqual(pc["accuracy"]["numerator"], 60)
        self.assertEqual(pc["accuracy"]["denominator"], 71)

        # Verify validate_scorecard rejects a macro-averaged ratio mutant
        mutant_metrics = copy.deepcopy(card.metrics)
        mutant_metrics["pooled_confusion"]["recall"]["permille"] = 677
        mutant_card = Scorecard(
            windows=card.windows,
            pooled=card.pooled,
            metrics=mutant_metrics,
            violations=card.violations,
            passed=True,
        )
        with self.assertRaises(ScorecardValidationError):
            validate_scorecard(mutant_card)

    def test_gate_g_m7_4_rejects_missing_and_mixed_seals(self) -> None:
        """G-M7-4 & INV-M7-2: Missing or mixed seals fail validation."""
        for bad_seal in ("", "   ", None):
            with self.subTest(bad_seal=bad_seal):
                with self.assertRaises(SealMismatchError):
                    w_bad = _make_evidence(seal=bad_seal)  # type: ignore[arg-type]
                    score_windows([w_bad])

        w_a = _make_evidence(canonical_id="tab-a", seal="seal-alpha")
        w_b = _make_evidence(canonical_id="tab-b", seal="seal-beta")
        with self.assertRaises(SealMismatchError):
            score_windows([w_a, w_b])

    def test_gate_g_m7_4_absent_denominators_are_none_and_fail_mandatory_gates(self) -> None:
        """G-M7-4 & INV-M7-3: Absent denominator is None (never 0) and fails mandatory metric & scorecard."""
        # 1. Zero positive support (tp + fn == 0)
        w_no_support = _make_evidence(tp=0, fp=3, fn=0, tn=10)
        card_no_support = score_windows(seal_trial(TrialReport(rows=tuple([w_no_support]), outcomes={}, faults=())))
        self.assertFalse(card_no_support.passed)
        self.assertIn("missing_denominator:pooled_confusion", card_no_support.violations)
        pc = card_no_support.metrics["pooled_confusion"]
        self.assertIsNone(pc["denominator"])
        self.assertFalse(pc["defined"])
        self.assertEqual(pc["status"], AUSENTE)
        self.assertFalse(pc["passed"])
        self.assertIsNone(pc["recall"]["denominator"])
        self.assertIsNone(pc["recall"]["permille"])
        self.assertIsNone(pc["recall"]["fraction"])
        self.assertIsNone(card_no_support.pooled["support"])

        # 2. Zero predicted positives (tp + fp == 0)
        w_no_pred = _make_evidence(tp=0, fp=0, fn=4, tn=10)
        card_no_pred = score_windows(seal_trial(TrialReport(rows=tuple([w_no_pred]), outcomes={}, faults=())))
        self.assertFalse(card_no_pred.passed)
        self.assertIsNone(card_no_pred.metrics["pooled_confusion"]["precision"]["denominator"])
        self.assertIsNone(card_no_pred.metrics["pooled_confusion"]["precision"]["permille"])
        self.assertFalse(card_no_pred.metrics["pooled_confusion"]["passed"])

        # 3. Zero delivered frames (all dropped or unsupported)
        w_dropped = _make_evidence(frame_supported=True, dropped=True, exact=None)
        card_dropped = score_windows(seal_trial(TrialReport(rows=tuple([w_dropped]), outcomes={}, faults=())))
        self.assertFalse(card_dropped.passed)
        self.assertIsNone(card_dropped.metrics["frame_exactness"]["denominator"])
        self.assertIsNone(card_dropped.metrics["frame_exactness"]["permille"])
        self.assertEqual(card_dropped.metrics["frame_exactness"]["status"], AUSENTE)
        self.assertFalse(card_dropped.metrics["frame_exactness"]["passed"])

        # 4. Zero supported frames
        w_unsup = _make_evidence(frame_supported=False, dropped=False, exact=None)
        card_unsup = score_windows(seal_trial(TrialReport(rows=tuple([w_unsup]), outcomes={}, faults=())))
        self.assertFalse(card_unsup.passed)
        self.assertIsNone(card_unsup.metrics["frame_drops"]["denominator"])
        self.assertIsNone(card_unsup.metrics["frame_exactness"]["denominator"])

        # 5. Empty window sequence
        card_empty = score_windows(seal_trial(TrialReport(rows=tuple(()), outcomes={}, faults=())))
        self.assertFalse(card_empty.passed)
        for key in MANDATORY_METRIC_KEYS:
            self.assertIsNone(card_empty.metrics[key]["denominator"])
            self.assertFalse(card_empty.metrics[key]["passed"])

        # 6. Verify validate_scorecard rejects a replace-null-with-zero mutant
        zero_mutant_metrics = copy.deepcopy(card_no_support.metrics)
        zero_mutant_metrics["pooled_confusion"]["recall"]["denominator"] = 0
        zero_mutant_metrics["pooled_confusion"]["recall"]["permille"] = 0
        zero_mutant_card = Scorecard(
            windows=card_no_support.windows,
            pooled=card_no_support.pooled,
            metrics=zero_mutant_metrics,
            violations=card_no_support.violations,
            passed=False,
        )
        with self.assertRaises(ScorecardValidationError):
            validate_scorecard(zero_mutant_card)

    def test_gate_g_m7_1_canonical_bytes_determinism_and_wall_time_exclusion(self) -> None:
        """G-M7-1 & INV-M7-3: Same seed and trace give identical canonical bytes; wall-clock timing excluded."""
        targets = (
            Handle("cdp_tab", "tab-1", 1),
            Handle("godot_window", "gd-1", 1),
        )
        events_run1 = (
            {
                "handle": targets[0],
                "scene": "DEV:scroll_h:0:1111",
                "tp": 14,
                "fp": 1,
                "fn": 2,
                "tn": 30,
                "ops": {"fl": 5, "gm": 3, "isolated_ops": {"fl": 5, "gm": 3}, "wall_ms": 12.34, "diagnostic_seconds": 0.99, "host": "node-a"},
            },
            {
                "handle": targets[1],
                "scene": "DEV:caret_move:0:2222",
                "tp": 8,
                "fp": 0,
                "fn": 1,
                "tn": 20,
                "ops": {"fl": 2, "gm": 4, "isolated_ops": {"fl": 2, "gm": 4}, "wall_ms": 98.76, "pid": 12345},
            },
        )
        events_run2 = (
            {
                "handle": targets[0],
                "scene": "DEV:scroll_h:0:1111",
                "tp": 14,
                "fp": 1,
                "fn": 2,
                "tn": 30,
                "ops": {"fl": 5, "gm": 3, "isolated_ops": {"fl": 5, "gm": 3}, "wall_ms": 999.99, "diagnostic_seconds": 42.0, "host": "node-b"},
            },
            {
                "handle": targets[1],
                "scene": "DEV:caret_move:0:2222",
                "tp": 8,
                "fp": 0,
                "fn": 1,
                "tn": 20,
                "ops": {"fl": 2, "gm": 4, "isolated_ops": {"fl": 2, "gm": 4}, "wall_ms": 0.01, "pid": 99999},
            },
        )

        scen1 = Scenario("det-trial", targets, events_run1, {"default_terminal": "OK", "seal": "seal-det"})
        scen2 = Scenario("det-trial", targets, events_run2, {"default_terminal": "OK", "seal": "seal-det"})

        with _ForbiddenLiveIOGuard():
            rep1 = run_trial(scen1, seed=77, clock=FakeClock(0), adapters={})
            rep2 = run_trial(scen2, seed=77, clock=FakeClock(0), adapters={})
            bytes_rep1 = canonical_bytes(rep1)
            bytes_rep2 = canonical_bytes(rep2)
            self.assertEqual(bytes_rep1, bytes_rep2)

            card1 = score_windows(rep1)
            card2 = score_windows(TrialReport(rows=tuple(reversed(rep2.rows)), outcomes=rep2.outcomes, faults=rep2.faults))
            bytes_card1 = canonical_bytes(card1)
            bytes_card2 = canonical_bytes(card2)
            self.assertEqual(bytes_card1, bytes_card2)

        decoded = json.loads(bytes_card1.decode("utf-8"))
        serialized_str = bytes_card1.decode("utf-8")
        for forbidden_key in ("wall_ms", "diagnostic_seconds", "pid", "node-a", "node-b"):
            self.assertNotIn(forbidden_key, serialized_str)
        self.assertTrue(decoded["passed"])

        # Different evidence changes canonical bytes
        scen_diff = Scenario(
            "det-trial",
            targets,
            (
                {**events_run1[0], "tp": 15},
                events_run1[1],
            ),
            {"default_terminal": "OK", "seal": "seal-det"},
        )
        rep_diff = run_trial(scen_diff, seed=77, clock=FakeClock(0), adapters={})
        self.assertNotEqual(bytes_rep1, canonical_bytes(rep_diff))

    def test_gate_g_m7_2_injected_cross_target_state_bleed_fails_scorecard(self) -> None:
        """G-M7-2: Injected cross-target state bleed fails scorecard."""
        w_clean = _make_evidence(canonical_id="win-clean")
        w_bleed = _make_evidence(
            canonical_id="win-bleed",
            violations=("cross_window_bleed",),
            ops={"decode": 1, "describe": 2, "cross_window_bleed": 1, "isolated_ops": {"decode": 1, "describe": 2}},
        )
        card = score_windows(seal_trial(TrialReport(rows=tuple([w_clean, w_bleed]), outcomes={}, faults=())))
        self.assertFalse(card.passed)
        self.assertIn("cross_window_bleed", card.violations)
        self.assertFalse(card.metrics["cross_window_bleed"]["passed"])
        self.assertGreater(card.metrics["cross_window_bleed"]["numerator"], 0)

        # Also test isolated replay operation count mismatch
        w_op_bleed = _make_evidence(
            canonical_id="win-op-mismatch",
            ops={"fl": 9, "gm": 3, "isolated_ops": {"fl": 4, "gm": 3}},
        )
        card_op = score_windows(seal_trial(TrialReport(rows=tuple([w_clean, w_op_bleed]), outcomes={}, faults=())))
        self.assertFalse(card_op.passed)
        self.assertIn("operation_totals_mismatch", card_op.violations)
        self.assertFalse(card_op.metrics["operation_totals"]["passed"])

    def test_gate_g_m7_3_heterogeneous_trial_covers_every_target_kind_and_isolates_cursors(self) -> None:
        """G-M7-3 & G-M8-2: Heterogeneous trial reports all 5 target kinds; Godot and CDP keep separate cursors."""
        targets = (
            Handle("cdp_tab", "shared-id", 1),
            Handle("godot_window", "shared-id", 1),
            Handle("x11_window", "desk-0/win-1", 1),
            Handle("xvfb_screen", "desk-0", 1),
            Handle("capsule", "desk-0", 1),
        )
        events = tuple(
            {
                "handle": h,
                "scene": f"DEV:scroll_small:{idx}:abcd",
                "cursor": (10 * (idx + 1), 20 * (idx + 1)),
                "ops": {"decode": 1, "isolated_ops": {"decode": 1}},
                "tp": 10 + idx,
                "fp": 1,
                "fn": 1,
                "tn": 20,
            }
            for idx, h in enumerate(targets)
        )
        scenario = Scenario(
            name="heterogeneous-m7",
            targets=targets,
            events=events,
            expected_outcomes={"default_terminal": "OK", "seal": "seal-het-v1"},
        )
        clock = FakeClock(1_000)
        adapters = {
            canonical_handle_key(h): (lambda ev, clock, rng, kind=h.kind: {"adapter_kind": kind})
            for h in targets
        }

        with _ForbiddenLiveIOGuard():
            report = run_trial(scenario, seed=42, clock=clock, adapters=adapters)
            card = score_windows(report, require_all_target_kinds=True)

        self.assertTrue(card.passed, f"unexpected violations: {card.violations}")
        self.assertEqual(card.pooled["target_kinds"], tuple(sorted(SUPPORTED_TARGET_KINDS)))
        self.assertTrue(card.pooled["all_target_kinds_present"])

        # Godot and CDP with identical canonical_id="shared-id" keep separate keys and cursors
        cursors = report.outcomes["cursors_by_target"]
        self.assertEqual(cursors["cdp_tab:9:shared-id:1"], (10, 20))
        self.assertEqual(cursors["godot_window:9:shared-id:1"], (20, 40))

        # Capsule and child xvfb_screen share the same actuation resource when addressing desk-0
        self.assertEqual(
            canonical_actuation_resource(targets[3]),
            canonical_actuation_resource(targets[4]),
        )
        self.assertNotEqual(
            canonical_actuation_resource(targets[0]),
            canonical_actuation_resource(targets[1]),
        )

        # Dropping a target kind fails when require_all_target_kinds=True
        partial_card = score_windows(seal_trial(TrialReport(rows=tuple(report.rows[:-1]), outcomes={}, faults=())), require_all_target_kinds=True)
        self.assertFalse(partial_card.passed)
        self.assertTrue(any(v.startswith("missing_target_kinds:") for v in partial_card.violations))

    def test_gate_g_m7_5_crash_timeout_and_unknown_terminals_without_false_success(self) -> None:
        """G-M7-5 & INV-M7-3: Crash/timeout counted without false success; UNKNOWN never counts as complete."""
        # 1. Unexpected crash and timeout fail nominal completion
        w_ok = _make_evidence(canonical_id="tab-ok", terminal="OK", expected_terminal="OK")
        w_crash = _make_evidence(canonical_id="tab-crash", terminal="CRASH", expected_terminal="OK")
        w_timeout = _make_evidence(canonical_id="tab-timeout", terminal="TIMEOUT", expected_terminal="OK")

        card_fault = score_windows(seal_trial(TrialReport(rows=tuple([w_ok, w_crash, w_timeout]), outcomes={}, faults=())))
        self.assertFalse(card_fault.passed)
        tc = card_fault.metrics["terminal_completeness"]
        self.assertFalse(tc["passed"])
        self.assertEqual(tc["numerator"], 1)
        self.assertEqual(tc["denominator"], 3)
        self.assertEqual(tc["crashes"], 1)
        self.assertEqual(tc["timeouts"], 1)
        self.assertEqual(tc["unexpected_faults"], 2)
        self.assertEqual(card_fault.pooled["crashes"], 1)
        self.assertEqual(card_fault.pooled["timeouts"], 1)

        # 2. Explicit expected fault trace matches fault expectation without counting as nominal success
        w_expected_timeout = _make_evidence(
            canonical_id="tab-fault-trace",
            terminal="TIMEOUT",
            expected_terminal="TIMEOUT",
        )
        card_expected_fault = score_windows(seal_trial(TrialReport(rows=tuple([w_ok, w_expected_timeout]), outcomes={}, faults=())))
        self.assertTrue(card_expected_fault.passed, f"violations: {card_expected_fault.violations}")
        tc_exp = card_expected_fault.metrics["terminal_completeness"]
        self.assertEqual(tc_exp["numerator"], 2)
        self.assertEqual(tc_exp["denominator"], 2)
        self.assertEqual(tc_exp["nominal_completed"], 1)
        self.assertEqual(tc_exp["nominal_required"], 1)
        self.assertEqual(tc_exp["fault_expected"], 1)
        self.assertEqual(tc_exp["fault_matched"], 1)
        self.assertEqual(tc_exp["timeouts"], 1)

        # 3. UNKNOWN terminal never counts as complete even if expected_terminal="UNKNOWN"
        for exp_t in ("OK", "UNKNOWN"):
            with self.subTest(expected_terminal=exp_t):
                w_unk = _make_evidence(
                    canonical_id="tab-unk",
                    terminal="UNKNOWN",
                    expected_terminal=exp_t,
                )
                card_unk = score_windows(seal_trial(TrialReport(rows=tuple([w_ok, w_unk]), outcomes={}, faults=())))
                self.assertFalse(card_unk.passed)
                self.assertIn("unknown_terminal", card_unk.violations)
                self.assertFalse(card_unk.metrics["terminal_completeness"]["passed"])

        # 4. Missing terminal fails validation
        w_missing_term = _make_evidence(canonical_id="tab-missing", terminal=None, expected_terminal=None)
        with self.assertRaises(TerminalValidationError):
            score_windows(seal_trial(TrialReport(rows=tuple([w_missing_term]), outcomes={}, faults=())))

    def test_duplicate_and_foreign_rows_and_frame_admission_validation(self) -> None:
        """INV-M7-2: Duplicate/foreign rows fail validation; WindowFrames & FrameRef enforce sequence and owned RGB."""
        w1 = _make_evidence(canonical_id="tab-1", epoch=0, frame_seq=0)
        w1_dup = _make_evidence(canonical_id="tab-1", epoch=0, frame_seq=0)
        with self.assertRaises(WindowIdentityError):
            score_windows(seal_trial(TrialReport(rows=tuple([w1, w1_dup]), outcomes={}, faults=())))

        # Foreign handle rejected when allowed_handles is provided
        allowed = {Handle("cdp_tab", "tab-1", 1)}
        w_foreign = _make_evidence(canonical_id="tab-foreign", epoch=0, frame_seq=0)
        with self.assertRaises(WindowIdentityError):
            score_windows(seal_trial(TrialReport(rows=tuple([w1, w_foreign]), outcomes={}, faults=())), allowed_handles=allowed)

        # Non-increasing frame_seq within the same (handle, scene, epoch) stream rejected
        w_seq1 = _make_evidence(canonical_id="tab-1", epoch=0, frame_seq=2)
        w_seq0 = _make_evidence(canonical_id="tab-1", epoch=0, frame_seq=1)
        with self.assertRaises(FrameSequenceError):
            score_windows(seal_trial(TrialReport(rows=tuple([w_seq1, w_seq0]), outcomes={}, faults=())))

        # FrameRef copies RGB into owned read-only contiguous uint8 array
        h = Handle("x11_window", "win-rgb", 1)
        raw_rgb = np.zeros((4, 6, 3), dtype=np.uint8)
        raw_rgb[1, 2] = [10, 20, 30]
        digest = hashlib.sha256(raw_rgb.tobytes()).hexdigest()
        fref = FrameRef(h, 0, 0, 6, 4, raw_rgb, digest)
        self.assertFalse(fref.rgb.flags.writeable)
        raw_rgb[1, 2] = [255, 255, 255]
        self.assertEqual(list(fref.rgb[1, 2]), [10, 20, 30])

        # WindowFrames validates strictly increasing sequences and handle/epoch binding
        fref_next = FrameRef(h, 0, 1, 6, 4, np.ones((4, 6, 3), dtype=np.uint8), hashlib.sha256(bytes([1]) * 72).hexdigest())
        wf = WindowFrames(h, 0, (fref, fref_next), (0, 1), 1_000_000)
        self.assertEqual(wf.frame_seqs, (0, 1))

        with self.assertRaises(FrameSequenceError):
            WindowFrames(h, 0, (fref, fref_next), (1, 1), 1_000_000)

        foreign_fref = FrameRef(Handle("cdp_tab", "other", 1), 0, 1, 6, 4, np.ones((4, 6, 3), dtype=np.uint8), hashlib.sha256(bytes([1]) * 72).hexdigest())
        with self.assertRaises(WindowIdentityError):
            WindowFrames(h, 0, (fref, foreign_fref), (0, 1), 1_000_000)

    def test_safety_stalls_starvation_lock_order_and_absent_prerequisites(self) -> None:
        """Scorecard pass contract: safety, budget caps, fairness bound 2*N, lock order, and AUSENTE prerequisites."""
        # Lock order validation
        self.assertTrue(validate_lock_order(METADATA_LOCK_ORDER))
        with self.assertRaises(ScorecardValidationError):
            validate_lock_order(("registry", "scheduler_or_fence"))

        # Starvation (> 2*N dispatch opportunities) and budget cap overflow fail scorecard
        w_starved = _make_evidence(
            canonical_id="tab-starved",
            ops={
                "decode": 1,
                "wait_dispatches": 5,
                "committed_charge": 60,
                "reserved_charge": 50,
                "target_cap": 100,
                "isolated_ops": {"decode": 1},
            },
        )
        card_starved = score_windows(seal_trial(TrialReport(rows=tuple([w_starved]), outcomes={}, faults=())))
        self.assertFalse(card_starved.passed)
        self.assertFalse(card_starved.metrics["starvation"]["passed"])
        self.assertFalse(card_starved.metrics["safety_violations"]["passed"])
        self.assertIn("starvation_bound_exceeded", card_starved.violations)
        self.assertIn("target_cap_exceeded", card_starved.violations)

        # Absent prerequisites in run_trial report AUSENTE and never pass scorecard
        scen_blocked = Scenario(
            name="blocked-prereq",
            targets=(Handle("cdp_tab", "tab-1", 1),),
            events=({"handle": Handle("cdp_tab", "tab-1", 1), "tp": 5, "fp": 0, "fn": 0, "tn": 5},),
            expected_outcomes={"prerequisites": {"identity_and_streams": True, "readiness": False}},
        )
        rep_blocked = run_trial(scen_blocked, seed=1, clock=FakeClock(0), adapters={})
        self.assertEqual(rep_blocked.outcomes["status"], AUSENTE)
        self.assertTrue(rep_blocked.outcomes["prerequisite_blocked"])
        card_blocked = score_windows(rep_blocked)
        self.assertFalse(card_blocked.passed)

        # Sealed duplicate-scene contract helper still rejects duplicate scenes when called directly
        fake_scene_res = {
            "prereg_sha": "abc",
            "scene_units": {"DEV:static_clean:0:123": {"kind": "static_clean", "param_bucket": {}, "metrics": {}}},
        }
        with self.assertRaises(ScoreError):
            score_sealed_window_scenes([fake_scene_res, fake_scene_res])


if __name__ == "__main__":
    unittest.main()
