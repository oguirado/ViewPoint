# @module {"id":"tests.test_jq2b_j5_episode_metrics_implementation","layer":3,"owner":"TESTS","deps":["wa.learn.episode_metrics","wa.learn.multiwin_scorecard","wa.perceive.session_registry"]}
"""Substantive public-path unit tests for wender.learn.episode_metrics (jq2b-j5-episode-metrics).

Exercises real public APIs of episode_metrics following INV J5 1 through INV J5 4:
- INV J5 1: eligible == completed + refused + incomplete; excluded and unsupported separately visible.
- INV J5 2: Completion requires matched engine outcomes and independent task postconditions.
- INV J5 3: Nonvacuous rational metrics; null/UNKNOWN for empty denominators, never perfection.
- INV J5 4: Safe refusal is correctness evidence without economic completion; confidence AUSENTE.
All fixtures are RAM-only and offline; stdlib plus numpy only.
"""
from __future__ import annotations

from contextlib import ExitStack
import copy
from dataclasses import fields, replace
from fractions import Fraction
import hashlib
import json
import unittest
from unittest.mock import patch

import numpy as np

from wender.learn import episode_metrics as em
from wender.learn.multiwin_scorecard import FakeClock, Handle, Scorecard, WindowEvidence
from wender.perceive.session_registry import RegistryError, SessionRegistry


def _make_dummy_handle(kind: str = "godot_window", cid: str = "win_0", gen: int = 1) -> Handle:
    """Create a valid session_registry.Handle (generation must be an integer >= 1)."""
    return Handle(kind=kind, canonical_id=cid, generation=gen)


def _make_window_evidence(
    scene: str = "test_scene",
    exact: bool = True,
    terminal: str | None = "goal",
    ops: dict | None = None,
    handle: Handle | None = None,
    epoch: int = 1,
    frame_seq: int = 0,
) -> WindowEvidence:
    h = handle or _make_dummy_handle()
    return WindowEvidence(
        handle=h,
        scene=scene,
        epoch=epoch,
        frame_seq=frame_seq,
        tp=100,
        fp=0,
        fn=0,
        tn=0,
        ops=ops or {},
        frame_supported=True,
        dropped=False,
        exact=exact,
        terminal=terminal,
        expected_terminal="goal",
        seal="0" * 64,
        violations=(),
    )


class TestEpisodeMetricsImplementation(unittest.TestCase):
    """Substantive test suite verifying wender.learn.episode_metrics production contract."""

    def test_inv_j5_1_eligible_equals_completed_plus_refused_plus_incomplete(self):
        """INV J5 1: eligible equals completed + refused + incomplete; excluded and unsupported stay separate."""
        rows = [
            em.EpisodeRow(
                episode_id="ep_comp",
                status="completed",
                terminal="goal",
                expected_terminal="goal",
                engine_outcomes={"status": "accepted"},
                expected_outcomes={"status": "accepted"},
                postconditions_met=True,
            ),
            em.EpisodeRow(
                episode_id="ep_ref",
                status="refused",
                terminal="safe_refusal",
                is_safe_refusal=True,
                is_local_refusal=True,
            ),
            em.EpisodeRow(
                episode_id="ep_inc",
                status="incomplete",
                terminal="timeout",
            ),
            em.EpisodeRow(
                episode_id="ep_exc",
                status="excluded",
            ),
        ]
        verdict = em.reduce_episode(rows)

        self.assertEqual(
            tuple(f.name for f in fields(em.EpisodeVerdict)),
            (
                "eligible",
                "completed",
                "refused",
                "incomplete",
                "excluded",
                "failures",
                "metrics",
                "reward",
                "reward_status",
                "confidence",
                "passed",
            ),
        )
        self.assertEqual(verdict.completed, 1)
        self.assertEqual(verdict.refused, 1)
        self.assertEqual(verdict.incomplete, 1)
        self.assertEqual(verdict.excluded, 1)
        self.assertEqual(verdict.eligible, 3)
        self.assertEqual(verdict.eligible, verdict.completed + verdict.refused + verdict.incomplete)
        self.assertTrue(verdict.passed)
        self.assertIsNone(verdict.reward)
        self.assertEqual(verdict.reward_status, "DESIGNING")

        # Unsupported cases stay separately visible and do not inflate eligible
        rows_with_unsupported = rows + [
            em.EpisodeRow(episode_id="ep_unsup", status="unsupported", supported=False)
        ]
        v_unsup = em.reduce_episode(rows_with_unsupported)
        self.assertEqual(v_unsup.eligible, 3)
        self.assertEqual(v_unsup.excluded, 2)
        self.assertEqual(v_unsup.metrics["counts"]["unsupported"], 1)
        self.assertFalse(v_unsup.passed)
        self.assertIn("unsupported_episode:ep_unsup", v_unsup.failures)

    def test_inv_j5_2_completion_requires_matched_outcomes_and_postconditions(self):
        """INV J5 2: Completion requires matched engine outcomes AND independent postconditions."""
        row_healthy = em.EpisodeRow(
            episode_id="ep_1",
            status="completed",
            engine_outcomes={"balance": 1000, "status": "settled"},
            expected_outcomes={"balance": 1000, "status": "settled"},
            postconditions_met=True,
        )
        v1 = em.reduce_episode([row_healthy])
        self.assertEqual(v1.completed, 1)
        self.assertEqual(v1.incomplete, 0)
        self.assertTrue(v1.passed)

        # Outcome mismatch -> demoted to incomplete, failure recorded
        row_bad_outcome = em.EpisodeRow(
            episode_id="ep_2",
            status="completed",
            engine_outcomes={"balance": 900, "status": "settled"},
            expected_outcomes={"balance": 1000, "status": "settled"},
            postconditions_met=True,
        )
        v2 = em.reduce_episode([row_bad_outcome])
        self.assertEqual(v2.completed, 0)
        self.assertEqual(v2.incomplete, 1)
        self.assertFalse(v2.passed)
        self.assertIn("unmatched_engine_outcomes:ep_2", v2.failures)

        # Postcondition unmet -> demoted to incomplete, failure recorded
        row_unmet_post = em.EpisodeRow(
            episode_id="ep_3",
            status="completed",
            engine_outcomes={"status": "settled"},
            expected_outcomes={"status": "settled"},
            postconditions_met=False,
        )
        v3 = em.reduce_episode([row_unmet_post])
        self.assertEqual(v3.completed, 0)
        self.assertEqual(v3.incomplete, 1)
        self.assertFalse(v3.passed)
        self.assertIn("unmet_postconditions:ep_3", v3.failures)

    def test_inv_j5_3_rational_metrics_and_null_unknown_for_empty_denominators(self):
        """INV J5 3: Nonvacuous rational metrics; null/UNKNOWN for empty denominators, never perfection."""
        r_active = em.make_rational_rate(7, 10)
        self.assertEqual(r_active["numerator"], 7)
        self.assertEqual(r_active["denominator"], 10)
        self.assertEqual(r_active["fraction"], "7/10")
        self.assertEqual(r_active["rate"], 0.7)
        self.assertEqual(r_active["status"], "MEASURED")
        self.assertTrue(r_active["passed"])

        # Zero numerator with positive denominator is MEASURED 0/10, distinct from UNKNOWN
        r_zero_num = em.make_rational_rate(0, 10)
        self.assertEqual(r_zero_num["numerator"], 0)
        self.assertEqual(r_zero_num["denominator"], 10)
        self.assertEqual(r_zero_num["rate"], 0.0)
        self.assertEqual(r_zero_num["fraction"], "0/10")
        self.assertEqual(r_zero_num["status"], "MEASURED")

        # Vacuous / zero denominator -> null and UNKNOWN, never perfection
        r_zero = em.make_rational_rate(0, 0)
        self.assertIsNone(r_zero["numerator"])
        self.assertIsNone(r_zero["denominator"])
        self.assertIsNone(r_zero["rate"])
        self.assertIsNone(r_zero["fraction"])
        self.assertEqual(r_zero["status"], "UNKNOWN")
        self.assertFalse(r_zero["passed"])

        # None denominator -> null and UNKNOWN
        r_none = em.make_rational_rate(5, None)
        self.assertIsNone(r_none["numerator"])
        self.assertIsNone(r_none["denominator"])
        self.assertEqual(r_none["status"], "UNKNOWN")
        self.assertFalse(r_none["passed"])

    def test_inv_j5_4_safe_refusal_and_emitted_attempt_distinction(self):
        """INV J5 4: Safe refusal is correctness without completion; emitted attempts distinct from local refusals."""
        row_refusal = em.EpisodeRow(
            episode_id="ep_refusal",
            status="refused",
            terminal="safe_refusal",
            is_safe_refusal=True,
            is_emitted_attempt=False,
            is_local_refusal=True,
        )
        v = em.reduce_episode([row_refusal])
        self.assertEqual(v.completed, 0)
        self.assertEqual(v.refused, 1)
        self.assertEqual(v.eligible, 1)
        self.assertTrue(v.passed)
        self.assertEqual(v.metrics["refusal"]["numerator"], 1)
        self.assertEqual(v.metrics["refusal"]["denominator"], 1)
        self.assertEqual(v.metrics["completion"]["numerator"], 0)
        self.assertEqual(v.metrics["completion"]["denominator"], 1)
        self.assertEqual(v.metrics["counts"]["local_refusals"], 1)
        self.assertEqual(v.metrics["counts"]["emitted_attempts"], 0)

        # Emitted attempt masquerading as local refusal fails loudly and is demoted to incomplete
        row_emitted_masquerade = em.EpisodeRow(
            episode_id="ep_emitted_refusal",
            status="refused",
            terminal="safe_refusal",
            is_safe_refusal=True,
            is_emitted_attempt=True,
            is_local_refusal=True,
        )
        v_bad = em.reduce_episode([row_emitted_masquerade])
        self.assertEqual(v_bad.refused, 0)
        self.assertEqual(v_bad.incomplete, 1)
        self.assertFalse(v_bad.passed)
        self.assertIn("emitted_attempt_not_local_refusal:ep_emitted_refusal", v_bad.failures)

    def test_gate_g_j5_2_nonvacuous_denominator_accounting_and_mutants(self):
        """Gate G-J5-2: Nonvacuous denominator accounting positive control and negative contrast."""
        rows = [
            em.EpisodeRow(
                episode_id="ep_1",
                status="completed",
                postconditions_met=True,
                supported_frames=2,
                exact_frames=2,
                trail_stamps=2,
                matched_stamps=2,
                engine_outcomes={"status": "accepted"},
                expected_outcomes={"status": "accepted"},
            ),
            em.EpisodeRow(
                episode_id="ep_2",
                status="refused",
                is_safe_refusal=True,
                is_local_refusal=True,
                supported_frames=1,
                exact_frames=1,
                trail_stamps=1,
                matched_stamps=1,
            ),
        ]
        manifest = {
            "battery": ["ep_1", "ep_2"],
            "denominators": {
                "eligible": 2,
                "supported_exactness": 3,
                "trail": 3,
                "outcomes": 1,
            },
            "thresholds": {"completion": Fraction(1, 2)},
        }
        v_valid = em.reduce_episode(rows, manifest=manifest)
        self.assertTrue(v_valid.passed)
        gate_res = em.evaluate_gate_g_j5_2(v_valid)
        self.assertTrue(gate_res["passed"])
        self.assertEqual(gate_res["status"], "PASS")
        self.assertTrue(gate_res["nonvacuous"])

        # Negative contrast: empty battery fails gate and verdict
        v_empty = em.reduce_episode([], manifest={"battery": []})
        self.assertFalse(v_empty.passed)
        self.assertIn("empty_battery", v_empty.failures)
        gate_empty = em.evaluate_gate_g_j5_2(v_empty)
        self.assertFalse(gate_empty["passed"])
        self.assertEqual(gate_empty["status"], "FAIL")
        self.assertFalse(gate_empty["nonvacuous"])

        # Negative contrast mutant: empty battery claiming a perfect score (rate=1.0, passed=True)
        mutant_empty_perfect = {
            "eligible": 0,
            "completed": 0,
            "refused": 0,
            "incomplete": 0,
            "excluded": 0,
            "metrics": {"completion": {"numerator": 0, "denominator": 0, "rate": 1.0, "status": "MEASURED", "passed": True}},
            "passed": True,
        }
        gate_mutant = em.evaluate_gate_g_j5_2(mutant_empty_perfect)
        self.assertFalse(gate_mutant["passed"])
        self.assertEqual(gate_mutant["status"], "FAIL")

    def test_gate_g_j5_4_rejects_invented_bounds_and_frame_pseudoreplication(self):
        """Gate G-J5-4: Descriptive confidence AUSENTE; rejects invented bounds and frame trials."""
        rows = [
            em.EpisodeRow(episode_id="ep_1", status="completed", postconditions_met=True),
            em.EpisodeRow(episode_id="ep_2", status="refused", is_safe_refusal=True, is_local_refusal=True),
            em.EpisodeRow(episode_id="ep_3", status="incomplete"),
        ]
        unc = em.report_uncertainty(rows, protocol={"name": "offline_j5_battery"})
        self.assertEqual(unc["confidence"]["status"], "AUSENTE")
        self.assertEqual(unc["confidence"]["unit"], "complete-episode")
        self.assertIsNone(unc["confidence"]["method"])
        self.assertIsNone(unc["confidence"]["lower"])
        self.assertIsNone(unc["confidence"]["upper"])
        self.assertEqual(unc["rational"]["completed"]["fraction"], "1/3")
        self.assertEqual(unc["rational"]["refused"]["fraction"], "1/3")
        self.assertEqual(unc["rational"]["incomplete"]["fraction"], "1/3")

        gate_pass = em.evaluate_gate_g_j5_4(unc)
        self.assertTrue(gate_pass["passed"])
        self.assertEqual(gate_pass["status"], "PASS")

        # Mutant 1: inventing confidence interval bounds
        unc_mutant_bounds = copy.deepcopy(unc)
        unc_mutant_bounds["confidence"]["lower"] = 0.15
        unc_mutant_bounds["confidence"]["upper"] = 0.65
        unc_mutant_bounds["confidence"]["method"] = "wilson_score"
        gate_fail_bounds = em.evaluate_gate_g_j5_4(unc_mutant_bounds)
        self.assertFalse(gate_fail_bounds["passed"])
        self.assertEqual(gate_fail_bounds["status"], "FAIL")

        # Mutant 2: treating each frame as an independent trial
        unc_mutant_frames = copy.deepcopy(unc)
        unc_mutant_frames["counts"]["eligible"] = 30
        unc_mutant_frames["counts"]["total_episodes"] = 3
        gate_fail_frames = em.evaluate_gate_g_j5_4(unc_mutant_frames)
        self.assertFalse(gate_fail_frames["passed"])
        self.assertEqual(gate_fail_frames["status"], "FAIL")

    def test_report_uncertainty_and_reduce_episode_kill_frame_pseudoreplication(self):
        """Multiple correlated frames for an episode must not increase episode count or create confidence."""
        frame_samples = [
            {
                "episode_id": "ep_single",
                "frame_seq": i,
                "status": "completed",
                "postconditions_met": True,
                "supported_frames": 1,
                "exact_frames": 1,
            }
            for i in range(10)
        ]
        unc = em.report_uncertainty(frame_samples)
        self.assertEqual(unc["counts"]["total_episodes"], 1)
        self.assertEqual(unc["counts"]["eligible"], 1)
        self.assertEqual(unc["counts"]["completed"], 1)
        self.assertEqual(unc["counts"]["raw_samples"], 10)
        self.assertEqual(unc["fractions"]["completed"], 1.0)
        self.assertEqual(unc["confidence"]["status"], "AUSENTE")
        self.assertIsNone(unc["confidence"]["lower"])

        verdict = em.reduce_episode(frame_samples)
        self.assertEqual(verdict.eligible, 1)
        self.assertEqual(verdict.completed, 1)
        self.assertEqual(verdict.metrics["counts"]["supported_frames"], 10)
        self.assertEqual(verdict.metrics["counts"]["exact_frames"], 10)
        self.assertTrue(verdict.passed)

    def test_battery_membership_deduplication_and_denominator_mismatches(self):
        """Battery membership checks foreign, duplicate, missing episodes and denominator mismatches."""
        manifest = {
            "battery": ["ep_a", "ep_b", "ep_c"],
            "denominators": {"eligible": 3, "supported_exactness": 5},
        }
        rows = [
            em.EpisodeRow(episode_id="ep_a", status="completed", postconditions_met=True, supported_frames=1, exact_frames=1),
            em.EpisodeRow(episode_id="ep_b", status="completed", postconditions_met=True, supported_frames=1, exact_frames=1),
            em.EpisodeRow(episode_id="ep_b", status="completed", postconditions_met=True, supported_frames=1, exact_frames=1),  # duplicate
            em.EpisodeRow(episode_id="ep_d", status="completed", postconditions_met=True, supported_frames=1, exact_frames=1),  # foreign
        ]
        verdict = em.reduce_episode(rows, manifest=manifest)
        self.assertFalse(verdict.passed)
        self.assertIn("duplicate_episode_identity:ep_b", verdict.failures)
        self.assertIn("foreign_battery_membership:ep_d", verdict.failures)
        self.assertIn("missing_battery_membership:ep_c", verdict.failures)
        self.assertTrue(any("denominator_mismatch:supported_exactness" in f for f in verdict.failures))
        # Foreign episode ep_d and duplicate ep_b must not inflate completed count
        self.assertEqual(verdict.completed, 1)
        self.assertEqual(verdict.incomplete, 2)
        self.assertEqual(verdict.eligible, 3)

    def test_expected_terminal_counts_validation(self):
        """Expected terminal counts match actual terminal counts."""
        manifest = {"expected_terminals": {"goal": 2, "safe_refusal": 1}}
        rows_ok = [
            em.EpisodeRow(episode_id="ep_1", status="completed", terminal="goal", postconditions_met=True),
            em.EpisodeRow(episode_id="ep_2", status="completed", terminal="goal", postconditions_met=True),
            em.EpisodeRow(episode_id="ep_3", status="refused", terminal="safe_refusal", is_safe_refusal=True),
        ]
        v_ok = em.reduce_episode(rows_ok, manifest=manifest)
        self.assertTrue(v_ok.passed)

        rows_mismatch = [
            em.EpisodeRow(episode_id="ep_1", status="completed", terminal="goal", postconditions_met=True),
            em.EpisodeRow(episode_id="ep_2", status="incomplete", terminal="timeout"),
            em.EpisodeRow(episode_id="ep_3", status="refused", terminal="safe_refusal", is_safe_refusal=True),
        ]
        v_mismatch = em.reduce_episode(rows_mismatch, manifest=manifest)
        self.assertFalse(v_mismatch.passed)
        self.assertTrue(any("terminal_count_mismatch:goal" in f for f in v_mismatch.failures))

    def test_declared_completion_thresholds_enforced(self):
        """Declared completion thresholds are checked; empty thresholds dict fails; no universal threshold invented."""
        rows = [
            em.EpisodeRow(episode_id="ep_1", status="completed", postconditions_met=True),
            em.EpisodeRow(episode_id="ep_2", status="incomplete"),
        ]
        # 1. Threshold 0.8 declared, achieved 0.5 -> fails threshold
        v_thresh = em.reduce_episode(rows, manifest={"thresholds": {"completion_rate": 0.8}})
        self.assertFalse(v_thresh.passed)
        self.assertIn("threshold_not_met:completion", v_thresh.failures)

        # 2. Declared threshold 0.5 -> passes because no higher universal threshold is invented
        v_half = em.reduce_episode(rows, manifest={"thresholds": {"completion_rate": 0.5}})
        self.assertTrue(v_half.passed)

        # 3. Empty thresholds dict when thresholds section is provided -> fails
        v_empty_thresh = em.reduce_episode(rows, manifest={"thresholds": {}})
        self.assertFalse(v_empty_thresh.passed)
        self.assertIn("missing_declared_completion_threshold", v_empty_thresh.failures)

    def test_holdings_offline_fixtures_pause_and_outcome(self):
        """Offline Holdings pause command and outcome record constructors and validators."""
        pause = em.make_holdings_pause(tick=10, seq=2)
        self.assertEqual(pause, {"tick": 10, "seq": 2, "cmd": "pause", "args": {}})
        validated_pause = em.validate_holdings_pause(pause)
        self.assertEqual(validated_pause["tick"], 10)

        with self.assertRaises(em.EpisodeError):
            em.validate_holdings_pause({"tick": 1, "seq": 1, "cmd": "pause", "args": {"bad": 1}})

        dummy_sha = hashlib.sha256(b"state").hexdigest()
        outcome = em.make_holdings_outcome(
            seq=2,
            requested_tick=10,
            applied_tick=10,
            order_seq_id=5,
            status="accepted",
            reason=None,
            state_sha256=dummy_sha,
        )
        self.assertEqual(outcome["status"], "accepted")
        validated_outcome = em.validate_holdings_outcome(outcome)
        self.assertEqual(validated_outcome["order_seq_id"], 5)

        with self.assertRaises(em.EpisodeError):
            em.make_holdings_outcome(
                seq=1, requested_tick=1, applied_tick=1, order_seq_id=1, status="accepted", state_sha256="bad_sha"
            )

    def test_source_agnostic_policies_godot_and_fake_cdp_without_relabeling(self):
        """Exercise both Godot and fake CDP source policies through fixtures without relabeling."""
        godot_row = em.EpisodeRow(
            episode_id="godot_sim_0",
            status="completed",
            terminal="goal",
            engine_outcomes={"source": "godot", "status": "accepted"},
            expected_outcomes={"source": "godot", "status": "accepted"},
            postconditions_met=True,
            metadata={"source_kind": "godot", "handle_kind": "godot_window"},
        )
        cdp_row = em.EpisodeRow(
            episode_id="cdp_sim_0",
            status="completed",
            terminal="goal",
            engine_outcomes={"source": "cdp", "status": "accepted"},
            expected_outcomes={"source": "cdp", "status": "accepted"},
            postconditions_met=True,
            metadata={"source_kind": "cdp", "handle_kind": "cdp_tab"},
        )
        verdict = em.reduce_episode([godot_row, cdp_row])
        self.assertEqual(verdict.completed, 2)
        self.assertEqual(verdict.eligible, 2)
        self.assertTrue(verdict.passed)

        # Relabeling Godot as CDP fails loudly
        relabeled = replace(godot_row, episode_id="relabeled_ep", metadata={"source_kind": "cdp", "handle_kind": "godot_window"})
        v_relabel = em.reduce_episode([relabeled])
        self.assertFalse(v_relabel.passed)
        self.assertTrue(any("source_policy_relabel" in f for f in v_relabel.failures))

    def test_prerequisites_vision_scorecard_and_optional_training_failures(self):
        """Missing prerequisites or expectedFailure fail as AUSENTE; optional training never blocks disabled retail migration."""
        row = em.EpisodeRow(episode_id="ep_1", status="completed", postconditions_met=True)

        # Requesting vision composition without merged JQ2-A vision scorecard receipt fails as AUSENTE
        v_vis_missing = em.reduce_episode([row], manifest={"vision_composition": True})
        self.assertFalse(v_vis_missing.passed)
        self.assertIn("prerequisite_AUSENTE:jq2a_vision_scorecard", v_vis_missing.failures)

        # Expected failure is a pending contract, not accepted implementation evidence
        v_exp_fail = em.reduce_episode(
            [row],
            manifest={
                "prerequisites": {
                    "G-M1-1": {"status": "PASS", "evidence": "receipt:1", "expected_failure": True}
                }
            },
        )
        self.assertFalse(v_exp_fail.passed)
        self.assertIn("expected_failure_not_accepted:G-M1-1", v_exp_fail.failures)

        # Optional training failures never block disabled retail Holdings migration
        v_mig = em.reduce_episode(
            [row],
            manifest={
                "retail_holdings_migration": {"enabled": False},
                "optional_training_failures": ["train_step_timeout"],
            },
        )
        self.assertTrue(v_mig.passed)
        self.assertFalse(v_mig.metrics["retail_holdings_migration_blocked"])

    def test_write_spy_rejects_pixel_and_receipt_leakage_in_ram_only_mode(self):
        """WriteSpy rejects pixel bytes, numpy arrays, PNG signatures, and unapproved raw receipts."""
        spy = em.WriteSpy()
        spy.persist({"episode_id": "ep_1", "status": "completed", "score": 100})
        self.assertEqual(len(spy.retained_records), 1)

        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
        with self.assertRaises(em.PixelPersistenceError):
            spy.persist({"frame_bytes": png_bytes})
        self.assertTrue(spy.leakage_detected)

        with self.assertRaises(em.PixelPersistenceError):
            spy.persist({"image_data": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA"})

        with self.assertRaises(em.PixelPersistenceError):
            spy.persist({"raw_receipt": {"trace": "godot_dump"}})

        with self.assertRaises(em.PixelPersistenceError):
            spy.persist({"frame": np.zeros((4, 4, 3), dtype=np.uint8)})

    def test_window_evidence_scorecard_and_session_registry_compatibility(self):
        """Reuses existing SessionRegistry, WindowEvidence, and Scorecard with real Handle generation >= 1."""
        clock = FakeClock(1000)
        registry = SessionRegistry(clock=clock)
        godot_handle = registry.register("godot_window", "holdings_main", object(), owned=False)
        cdp_handle = registry.register("cdp_tab", "holdings_docs", object(), owned=False)

        self.assertGreaterEqual(godot_handle.generation, 1)
        self.assertGreaterEqual(cdp_handle.generation, 1)

        # Invalid Handle generation < 1 or undeclared kind raises RegistryError
        with self.assertRaises(RegistryError):
            Handle("godot_window", "bad_gen", 0)
        with self.assertRaises(RegistryError):
            Handle("unknown_kind", "bad_kind", 1)

        rgb = np.zeros((2, 2, 3), dtype=np.uint8)
        frame_godot = registry.admit_frame(godot_handle, 0, rgb)
        obs_godot = registry.admit_observation(godot_handle, frame_godot, state={"tick": 1}, dpr_ppm=1000000)
        self.assertEqual(obs_godot.handle, godot_handle)

        ev1 = _make_window_evidence(
            handle=godot_handle,
            scene="scene_1",
            epoch=0,
            frame_seq=0,
            exact=True,
            terminal="goal",
            ops={
                "episode_id": "win_ep_1",
                "postconditions_met": True,
                "engine_outcomes": {"status": "accepted", "source": "godot"},
                "source_kind": "godot",
            },
        )
        ev2 = _make_window_evidence(
            handle=cdp_handle,
            scene="scene_2",
            epoch=0,
            frame_seq=0,
            exact=True,
            terminal="safe_refusal",
            ops={
                "episode_id": "win_ep_2",
                "is_safe_refusal": True,
                "is_local_refusal": True,
                "source_kind": "cdp",
            },
        )
        with ExitStack() as stack:
            for target in ("builtins.open", "io.open", "socket.socket", "subprocess.Popen", "numpy.save"):
                stack.enter_context(patch(target, side_effect=AssertionError("forbidden I/O")))
            verdict = em.reduce_episode([ev1, ev2])

        self.assertEqual(verdict.eligible, 2)
        self.assertEqual(verdict.completed, 1)
        self.assertEqual(verdict.refused, 1)
        self.assertTrue(verdict.passed)

        sc = Scorecard(
            windows=(ev1, ev2),
            pooled={"tp": 200, "fp": 0, "fn": 0, "tn": 0},
            metrics={"frame_exactness": {"numerator": 2, "denominator": 2, "passed": True}},
            violations=(),
            passed=True,
        )
        self.assertTrue(sc.passed)
        self.assertEqual(len(sc.windows), 2)


if __name__ == "__main__":
    unittest.main()
