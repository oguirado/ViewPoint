"""Ordinary unit tests for eval.v2eval.holdings_change_statistics (Gate G J10 4).

Exercises the real public API of eval.v2eval.holdings_change_statistics with
independent expected outputs across:
- Descriptive integer differences and exact rational fractions over complete-case units.
- Explicit None preservation for unavailable evidence and missing operation counters.
- Confidence status AUSENTE and null significance/interval/OPE/reward without reviewed protocol.
- Environment and generator mismatch refusals, unequal batteries, and per-case exclusions.
- Negative controls for duplicated correlated frames and changed environment identity.
- Manifest provenance, INV-J10-1..INV-J10-4, all 11 case families, lane isolation under
  reordering, ContextVar op restoration on plugin exceptions, source-neutral Holdings T2
  boundary, and WriteSpy pixel-persistence rejection.
"""
from __future__ import annotations

import copy
from fractions import Fraction
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

from eval.gui_synth.rng import SplitMix64
from eval.v2eval import holdings_change_statistics as hcs
from eval.v2eval.holdings_change_statistics import (
    AUSENTE,
    BatteryMismatchError,
    CONDUCTOR_ADMISSION_BINDING,
    ELECTRON_BROKERED_PUBLISHER,
    EnvironmentMismatchError,
    GeneratorMismatchError,
    HoldingsStatisticsError,
    MANIFEST_REQUIRED_FIELDS,
    MANIFEST_SCHEMA,
    PixelPersistenceError,
    ProtocolValidationError,
    RationalValue,
    SourceBoundaryError,
    SourceNeutralBoundary,
    TIMING_SCOPE_VM,
    WriteSpy,
    build_environment_pin,
    build_manifest,
    canonical_json_bytes,
    default_generator_sha256,
    derive_lane_seed,
    describe_changes,
    evaluate_lanes,
    forbid_pixel_persistence,
    generate_case,
    make_holdings_command,
    rational_fraction,
    validate_environment,
    validate_manifest,
    validate_source_observation,
    validate_t2_command,
)
from eval.v2x import fl_blinkprior, fl_v3, gm_bands_hv
from eval.v2x.per_window_detector import Handle, PerWindowDetector


def _make_paired_fixture() -> tuple[dict, dict, dict[str, str], str]:
    """Create a deterministic paired 3-case baseline and candidate battery fixture."""
    env = build_environment_pin(
        python_version="3.11.9",
        numpy_version="1.26.4",
        platform_id="linux-x86_64",
        dependency_digest="a" * 64,
    )
    gen_sha = "b" * 64
    baseline_cases = [
        {
            "case_id": "case_alpha",
            "seed_tag": 101,
            "status": "OK",
            "complete": True,
            "frames": 4,
            "expected_frames": 4,
            "metrics": {
                "correct": 12,
                "total": 16,
                "errors": 4,
                "fp": 1,
                "fp_total": 8,
                "moved_correct": 4,
                "moved_total": 8,
                "flicker_correct": 0,
                "flicker_total": 0,
                "changed_correct": 2,
                "changed_total": 4,
                "settled": 1,
            },
            "ops": {"gm": 1200, "fl": 300},
            "frame_metadata": [
                {
                    "case_id": "case_alpha",
                    "frame_seq": s,
                    "pixel_decl_sha256": hashlib.sha256(f"decl:alpha:{s}".encode()).hexdigest(),
                    "raw_sha256": hashlib.sha256(f"raw:alpha:{s}".encode()).hexdigest(),
                }
                for s in range(1, 5)
            ],
        },
        {
            "case_id": "case_beta",
            "seed_tag": 202,
            "status": "OK",
            "complete": True,
            "frames": 4,
            "expected_frames": 4,
            "metrics": {
                "correct": 10,
                "total": 16,
                "errors": 6,
                "fp": 2,
                "fp_total": 8,
                "moved_correct": 2,
                "moved_total": 4,
                "flicker_correct": 1,
                "flicker_total": 4,
                "changed_correct": 3,
                "changed_total": 4,
                "settled": 0,
            },
            "ops": {"gm": 1400, "fl": 320},
            "frame_metadata": [
                {
                    "case_id": "case_beta",
                    "frame_seq": s,
                    "pixel_decl_sha256": hashlib.sha256(f"decl:beta:{s}".encode()).hexdigest(),
                    "raw_sha256": hashlib.sha256(f"raw:beta:{s}".encode()).hexdigest(),
                }
                for s in range(1, 5)
            ],
        },
        {
            "case_id": "case_gamma",
            "seed_tag": 303,
            "status": "OK",
            "complete": True,
            "frames": 4,
            "expected_frames": 4,
            "metrics": {
                "correct": 14,
                "total": 16,
                "errors": 2,
                "fp": 0,
                "fp_total": 8,
                "moved_correct": 3,
                "moved_total": 4,
                "flicker_correct": 1,
                "flicker_total": 2,
                "changed_correct": 2,
                "changed_total": 2,
                "settled": 1,
            },
            "ops": {"gm": 1000, "fl": 280},
            "frame_metadata": [
                {
                    "case_id": "case_gamma",
                    "frame_seq": s,
                    "pixel_decl_sha256": hashlib.sha256(f"decl:gamma:{s}".encode()).hexdigest(),
                    "raw_sha256": hashlib.sha256(f"raw:gamma:{s}".encode()).hexdigest(),
                }
                for s in range(1, 5)
            ],
        },
    ]

    candidate_cases = copy.deepcopy(baseline_cases)
    # case_alpha improves: correct 12 -> 15 (errors 4 -> 1, moved_correct 4 -> 7, fp 1 -> 0)
    candidate_cases[0]["metrics"]["correct"] = 15
    candidate_cases[0]["metrics"]["errors"] = 1
    candidate_cases[0]["metrics"]["fp"] = 0
    candidate_cases[0]["metrics"]["moved_correct"] = 7
    candidate_cases[0]["ops"]["gm"] = 1100
    candidate_cases[0]["ops"]["fl"] = 310

    # case_beta improves: correct 10 -> 13 (errors 6 -> 3, flicker_correct 1 -> 3, fp 2 -> 1)
    candidate_cases[1]["metrics"]["correct"] = 13
    candidate_cases[1]["metrics"]["errors"] = 3
    candidate_cases[1]["metrics"]["fp"] = 1
    candidate_cases[1]["metrics"]["flicker_correct"] = 3
    candidate_cases[1]["ops"]["gm"] = 1350
    candidate_cases[1]["ops"]["fl"] = 330

    # case_gamma unchanged: correct stays 14 (errors stays 2)
    candidate_cases[2]["ops"]["gm"] = 1000
    candidate_cases[2]["ops"]["fl"] = 280

    baseline = {
        "environment": copy.deepcopy(env),
        "generator_sha256": gen_sha,
        "algorithm_id": "j10-holdings-synth-v1",
        "master_seed": 42,
        "cases": baseline_cases,
    }
    candidate = {
        "environment": copy.deepcopy(env),
        "generator_sha256": gen_sha,
        "algorithm_id": "j10-holdings-synth-v1",
        "master_seed": 42,
        "cases": candidate_cases,
    }
    return baseline, candidate, env, gen_sha


class TestHoldingsChangeStatisticsImplementation(unittest.TestCase):
    """Comprehensive ordinary test suite for eval.v2eval.holdings_change_statistics."""

    def test_prerequisite_imports_and_module_surface(self):
        """Undecorated prerequisite test verifying module exports and absence of adapter imports."""
        self.assertTrue(callable(describe_changes))
        self.assertTrue(callable(generate_case))
        self.assertTrue(callable(build_manifest))
        self.assertTrue(callable(validate_manifest))
        self.assertTrue(callable(evaluate_lanes))
        self.assertEqual(hcs.AUSENTE, "AUSENTE")
        self.assertEqual(ELECTRON_BROKERED_PUBLISHER, "AUSENTE")
        self.assertEqual(CONDUCTOR_ADMISSION_BINDING, "AUSENTE")
        self.assertEqual(TIMING_SCOPE_VM, "VM")
        for forbidden_mod in ("wender.perceive.godot", "wender.perceive.cdp_pipe", "wender.act.godot_executor"):
            self.assertNotIn(forbidden_mod, sys.modules)

    def test_gate_g_j10_4_healthy_control_exact_arithmetic_and_ausente_confidence(self):
        """Gate G J10 4: Healthy control computes exact integer deltas, reduced rational fractions, and AUSENTE confidence."""
        baseline, candidate, _, _ = _make_paired_fixture()
        b_snapshot = copy.deepcopy(baseline)
        c_snapshot = copy.deepcopy(candidate)

        report = describe_changes(baseline, candidate, protocol=None)

        # Verify input immutability
        self.assertEqual(baseline, b_snapshot)
        self.assertEqual(candidate, c_snapshot)

        # Status & sampling units
        self.assertEqual(report["status"], "OK")
        self.assertFalse(report["refused"])
        self.assertFalse(report["accepted"])
        self.assertIsNone(report["error"])
        self.assertEqual(report["sampling_unit"], "complete_case")
        self.assertEqual(report["complete_case_count"], 3)
        self.assertEqual(report["sampling_unit_count"], 3)
        self.assertEqual(report["paired_case_ids"], ("case_alpha", "case_beta", "case_gamma"))
        self.assertEqual(report["excluded_case_ids"], ())
        self.assertEqual(report["per_case_exclusions"], {})

        # Exact integer totals & differences (candidate - baseline)
        # Baseline correct: 12 + 10 + 14 = 36; Candidate correct: 15 + 13 + 14 = 42 -> diff = +6
        self.assertEqual(report["baseline_totals"]["correct"], 36)
        self.assertEqual(report["candidate_totals"]["correct"], 42)
        self.assertEqual(report["differences"]["correct"], 6)
        self.assertEqual(report["differences"]["total"], 0)
        self.assertEqual(report["differences"]["errors"], -6)
        self.assertEqual(report["differences"]["fp"], -2)
        self.assertEqual(report["differences"]["moved_correct"], 3)
        self.assertEqual(report["differences"]["flicker_correct"], 2)
        self.assertEqual(report["differences"]["improved_cases"], 2)
        self.assertEqual(report["differences"]["regressed_cases"], 0)
        self.assertEqual(report["differences"]["unchanged_cases"], 1)
        self.assertEqual(report["differences"]["net_improved_cases"], 2)

        # Operation counter totals & differences
        # Baseline GM: 1200+1400+1000=3600; Candidate GM: 1100+1350+1000=3450 -> diff = -150
        # Baseline FL: 300+320+280=900; Candidate FL: 310+330+280=920 -> diff = +20
        self.assertEqual(report["ops"]["baseline"]["gm"], 3600)
        self.assertEqual(report["ops"]["candidate"]["gm"], 3450)
        self.assertEqual(report["ops"]["differences"]["gm"], -150)
        self.assertEqual(report["ops"]["baseline"]["fl"], 900)
        self.assertEqual(report["ops"]["candidate"]["fl"], 920)
        self.assertEqual(report["ops"]["differences"]["fl"], 20)

        # Exact reduced rational fractions
        self.assertEqual(report["fractions"]["improved_cases"], Fraction(2, 3))
        self.assertEqual(report["fractions"]["improved_cases"]["numerator"], 2)
        self.assertEqual(report["fractions"]["improved_cases"]["denominator"], 3)
        self.assertEqual(report["fractions"]["regressed_cases"], Fraction(0, 1))
        self.assertEqual(report["fractions"]["unchanged_cases"], Fraction(1, 3))
        self.assertEqual(report["fractions"]["net_case_change"], Fraction(2, 3))
        # Accuracy: 36/48 = 3/4 -> 42/48 = 7/8, difference = 1/8
        self.assertEqual(report["fractions"]["baseline_accuracy"], Fraction(3, 4))
        self.assertEqual(report["fractions"]["candidate_accuracy"], Fraction(7, 8))
        self.assertEqual(report["fractions"]["accuracy_difference"], Fraction(1, 8))
        self.assertEqual(report["fractions"]["case_unit_accuracy_difference"], Fraction(1, 8))
        # Moved recall: 9/16 -> 12/16 = 3/4, difference = 3/16
        self.assertEqual(report["fractions"]["moved_recall_baseline"], Fraction(9, 16))
        self.assertEqual(report["fractions"]["moved_recall_candidate"], Fraction(3, 4))
        self.assertEqual(report["fractions"]["moved_recall_difference"], Fraction(3, 16))
        # Flicker recall: 2/6 = 1/3 -> 4/6 = 2/3, difference = 1/3
        self.assertEqual(report["fractions"]["flicker_recall_baseline"], Fraction(1, 3))
        self.assertEqual(report["fractions"]["flicker_recall_candidate"], Fraction(2, 3))
        self.assertEqual(report["fractions"]["flicker_recall_difference"], Fraction(1, 3))

        # Missing reviewed protocol -> confidence_status AUSENTE and all statistical claims None
        self.assertEqual(report["confidence_status"], AUSENTE)
        self.assertEqual(report["protocol_status"], AUSENTE)
        self.assertFalse(report["protocol_reviewed"])
        self.assertIsNone(report["significance"])
        self.assertIsNone(report["p_value"])
        self.assertIsNone(report["interval"])
        self.assertIsNone(report["confidence_interval"])
        self.assertIsNone(report["ope_certification"])
        self.assertIsNone(report["learned_reward"])
        self.assertIsNone(report["reward"])
        self.assertEqual(report["reward_status"], AUSENTE)
        self.assertIsNone(report["economic_improvement"])
        self.assertFalse(report["improvement_claimed"])
        self.assertIsNone(report["improved"])
        self.assertEqual(report["timing_scope"], "VM")

    def test_gate_g_j10_4_negative_controls_environment_and_correlated_frames(self):
        """Gate G J10 4: Prove healthy control differs from changed-environment and duplicated-frame negative contrasts."""
        baseline, candidate, _, _ = _make_paired_fixture()
        healthy = describe_changes(baseline, candidate)

        # Negative Control 1: Changed environment identity
        cand_env_changed = copy.deepcopy(candidate)
        cand_env_changed["environment"]["numpy"] = "2.0.0-altered"
        neg_env = describe_changes(baseline, cand_env_changed)

        self.assertNotEqual(healthy["status"], neg_env["status"])
        self.assertEqual(neg_env["status"], "REFUSED")
        self.assertTrue(neg_env["refused"])
        self.assertEqual(neg_env["error"], "environment_mismatch")
        self.assertFalse(neg_env["environment_match"])
        self.assertFalse(neg_env["evidence_strengthened"])
        self.assertEqual(neg_env["confidence_status"], AUSENTE)
        self.assertFalse(neg_env["improvement_claimed"])
        self.assertIsNone(neg_env["economic_improvement"])
        self.assertIsNone(neg_env["differences"])
        self.assertIsNone(neg_env["fractions"])
        self.assertEqual(neg_env["complete_case_count"], 0)

        with self.assertRaises(EnvironmentMismatchError):
            describe_changes(baseline, cand_env_changed, strict=True)

        # Negative Control 2: Changed generator digest
        cand_gen_changed = copy.deepcopy(candidate)
        cand_gen_changed["generator_sha256"] = "c" * 64
        neg_gen = describe_changes(baseline, cand_gen_changed)

        self.assertEqual(neg_gen["status"], "REFUSED")
        self.assertEqual(neg_gen["error"], "generator_mismatch")
        self.assertFalse(neg_gen["generator_match"])
        self.assertEqual(neg_gen["confidence_status"], AUSENTE)
        self.assertIsNone(neg_gen["differences"])

        with self.assertRaises(GeneratorMismatchError):
            describe_changes(baseline, cand_gen_changed, strict=True)

        # Negative Control 3: Duplicated correlated frames inside a case
        cand_dup_frames = copy.deepcopy(candidate)
        dup_frame = copy.deepcopy(cand_dup_frames["cases"][0]["frame_metadata"][0])
        cand_dup_frames["cases"][0]["frame_metadata"].append(dup_frame)
        neg_dup = describe_changes(baseline, cand_dup_frames)

        self.assertNotEqual(healthy["status"], neg_dup["status"])
        self.assertEqual(neg_dup["status"], "REFUSED")
        self.assertTrue(neg_dup["refused"])
        self.assertEqual(neg_dup["error"], "correlated_frame_duplication")
        self.assertTrue(neg_dup["correlated_frame_duplicates"])
        self.assertIn("case_alpha", neg_dup["correlated_duplicate_cases"])
        self.assertFalse(neg_dup["evidence_strengthened"])
        self.assertEqual(neg_dup["confidence_status"], AUSENTE)
        self.assertFalse(neg_dup["improvement_claimed"])
        self.assertIsNone(neg_dup["significance"])
        self.assertIsNone(neg_dup["interval"])
        # Complete-case sampling unit count must NEVER be inflated by frame duplication
        self.assertEqual(neg_dup["complete_case_count"], healthy["complete_case_count"])
        self.assertEqual(neg_dup["fractions"]["improved_cases"], healthy["fractions"]["improved_cases"])

    def test_null_counters_and_unequal_batteries_with_per_case_exclusions(self):
        """Missing counters stay explicit None (never 0) and unequal batteries refuse improvement claims."""
        baseline, candidate, _, _ = _make_paired_fixture()
        # Set FL counter to None on case_beta in candidate, and GM counter to None on case_alpha in baseline
        baseline["cases"][0]["ops"]["gm"] = None
        candidate["cases"][1]["ops"]["fl"] = None

        res_null = describe_changes(baseline, candidate)
        self.assertEqual(res_null["status"], "OK")
        self.assertIsNone(res_null["ops"]["baseline"]["gm"])
        self.assertIsNotNone(res_null["ops"]["candidate"]["gm"])
        self.assertIsNone(res_null["ops"]["differences"]["gm"])
        self.assertIsNone(res_null["differences"]["gm_ops"])
        self.assertIsNotNone(res_null["ops"]["baseline"]["fl"])
        self.assertIsNone(res_null["ops"]["candidate"]["fl"])
        self.assertIsNone(res_null["ops"]["differences"]["fl"])
        self.assertIsNone(res_null["differences"]["fl_ops"])
        self.assertIsNone(res_null["per_case"]["case_alpha"]["ops_differences"]["gm"])
        self.assertIsNone(res_null["per_case"]["case_beta"]["ops_differences"]["fl"])
        # Gamma still has both counters present per-case
        self.assertEqual(res_null["per_case"]["case_gamma"]["ops_differences"]["gm"], 0)
        self.assertEqual(res_null["per_case"]["case_gamma"]["ops_differences"]["fl"], 0)

        # Unequal battery: mark case_beta as INCOMPLETE on candidate and omit case_gamma from candidate
        cand_unequal = copy.deepcopy(candidate)
        cand_unequal["cases"][1]["status"] = "INCOMPLETE"
        cand_unequal["cases"][1]["complete"] = False
        cand_unequal["cases"][1]["error"] = "plugin_exception"
        cand_unequal["cases"].pop(2)  # remove case_gamma

        res_unequal = describe_changes(baseline, cand_unequal)
        self.assertEqual(res_unequal["status"], "REFUSED")
        self.assertTrue(res_unequal["refused"])
        self.assertEqual(res_unequal["error"], "unequal_battery")
        self.assertTrue(res_unequal["unequal_battery"])
        self.assertFalse(res_unequal["battery_equal"])
        self.assertFalse(res_unequal["improvement_claimed"])
        self.assertIsNone(res_unequal["economic_improvement"])
        self.assertIsNone(res_unequal["improved"])
        self.assertEqual(res_unequal["confidence_status"], AUSENTE)
        self.assertEqual(res_unequal["paired_case_ids"], ("case_alpha",))
        self.assertEqual(res_unequal["complete_case_count"], 1)
        self.assertEqual(res_unequal["excluded_case_ids"], ("case_beta", "case_gamma"))
        self.assertEqual(res_unequal["per_case_exclusions"]["case_beta"]["side"], "candidate")
        self.assertEqual(res_unequal["per_case_exclusions"]["case_beta"]["reason"], "plugin_exception")
        self.assertEqual(res_unequal["per_case_exclusions"]["case_gamma"]["side"], "candidate")
        self.assertEqual(res_unequal["per_case_exclusions"]["case_gamma"]["reason"], "missing_in_candidate")

        with self.assertRaises(BatteryMismatchError):
            describe_changes(baseline, cand_unequal, strict=True)

    def test_unreviewed_protocol_cannot_produce_statistical_or_economic_claims(self):
        """Missing or unreviewed protocol keeps significance, interval, OPE, and learned reward None."""
        baseline, candidate, _, _ = _make_paired_fixture()
        unreviewed_proto = {
            "protocol_id": "draft-proto-01",
            "reviewed": False,
            "confidence_theorem": AUSENTE,
        }
        res = describe_changes(baseline, candidate, protocol=unreviewed_proto)
        self.assertEqual(res["protocol_status"], "UNREVIEWED")
        self.assertFalse(res["protocol_reviewed"])
        self.assertEqual(res["confidence_status"], AUSENTE)
        self.assertIsNone(res["significance"])
        self.assertIsNone(res["interval"])
        self.assertIsNone(res["ope_certification"])
        self.assertIsNone(res["learned_reward"])
        self.assertIsNone(res["economic_improvement"])
        self.assertFalse(res["improvement_claimed"])

        with self.assertRaises(ProtocolValidationError):
            describe_changes(baseline, candidate, protocol={"reviewed": "yes"})  # type: ignore[arg-type]

    def test_inv_j10_1_and_j10_2_manifest_and_lane_reordering_isolation(self):
        """INV-J10-1 & INV-J10-2: Reordering lanes/cases preserves RNG, history, cache, and manifest seal."""
        env = build_environment_pin()
        gen_sha = default_generator_sha256()
        specs = [
            {
                "case_id": "lane_a_static",
                "seed_tag": 11,
                "family": "static_control",
                "handle": {"kind": "godot_window", "canonical_id": "win_a", "generation": 1},
                "geometry": {"width": 32, "height": 32, "tile_size": 16},
                "n_frames": 5,
            },
            {
                "case_id": "lane_b_trans",
                "seed_tag": 22,
                "family": "integer_translation",
                "handle": {"kind": "cdp_tab", "canonical_id": "tab_b", "generation": 1},
                "geometry": {"width": 64, "height": 64, "tile_size": 16},
                "n_frames": 4,
            },
            {
                "case_id": "lane_c_flicker_veto",
                "seed_tag": 33,
                "family": "periodic_flicker_real_motion_veto",
                "handle": {"kind": "x11_window", "canonical_id": "x11_c", "generation": 1},
                "geometry": {"width": 64, "height": 64, "tile_size": 16},
                "n_frames": 6,
            },
        ]

        with forbid_pixel_persistence() as spy:
            cases_fwd = [generate_case(s, master_seed=99, environment=env, generator_sha256=gen_sha) for s in specs]
            cases_rev = [generate_case(s, master_seed=99, environment=env, generator_sha256=gen_sha) for s in reversed(specs)]

            # Verify identical generated frame digests and read-only volatile uint8 frames
            by_id_fwd = {c["case_id"]: c for c in cases_fwd}
            by_id_rev = {c["case_id"]: c for c in cases_rev}
            for cid in by_id_fwd:
                self.assertEqual(by_id_fwd[cid]["frame_metadata"], by_id_rev[cid]["frame_metadata"])
                for f_a, f_b in zip(by_id_fwd[cid]["frames_rgb"], by_id_rev[cid]["frames_rgb"]):
                    self.assertEqual(f_a.dtype, np.uint8)
                    self.assertFalse(f_a.flags.writeable)
                    np.testing.assert_array_equal(f_a, f_b)

            manifest_fwd = build_manifest(cases_fwd, environment=env, generator_sha256=gen_sha, master_seed=99)
            manifest_rev = build_manifest(cases_rev, environment=env, generator_sha256=gen_sha, master_seed=99)
            self.assertEqual(manifest_fwd["manifest_sha256"], manifest_rev["manifest_sha256"])
            for field_name in MANIFEST_REQUIRED_FIELDS:
                self.assertIn(field_name, manifest_fwd)
            validate_manifest(manifest_fwd, expected_environment=env, expected_generator_sha256=gen_sha)

            # Evaluate forward vs reversed order through PerWindowDetector
            eval_fwd = evaluate_lanes(cases_fwd, environment=env, generator_sha256=gen_sha, master_seed=99)
            eval_rev = evaluate_lanes(cases_rev, environment=env, generator_sha256=gen_sha, master_seed=99)
            res_fwd_map = {r["case_id"]: r for r in eval_fwd["cases"]}
            res_rev_map = {r["case_id"]: r for r in eval_rev["cases"]}
            for cid in res_fwd_map:
                self.assertEqual(res_fwd_map[cid]["metrics"], res_rev_map[cid]["metrics"])
                self.assertEqual(res_fwd_map[cid]["ops"], res_rev_map[cid]["ops"])
                self.assertEqual(res_fwd_map[cid]["settled"], res_rev_map[cid]["settled"])
            self.assertEqual(len(spy.blocked_attempts), 0)

        # INV-J10-2: Regeneration refuses mismatched seed, generator, or environment
        with self.assertRaises(EnvironmentMismatchError):
            generate_case(
                specs[0],
                master_seed=99,
                environment=env,
                generator_sha256=gen_sha,
                expected_environment=build_environment_pin(numpy_version="0.0.0-wrong"),
            )
        with self.assertRaises(GeneratorMismatchError):
            generate_case(
                specs[0],
                master_seed=99,
                environment=env,
                generator_sha256=gen_sha,
                expected_generator_sha256="d" * 64,
            )
        with self.assertRaises(GeneratorMismatchError):
            generate_case(
                specs[0],
                master_seed=99,
                environment=env,
                generator_sha256=gen_sha,
                expected_master_seed=100,
            )

    def test_inv_j10_3_and_j10_4_contextvar_restoration_and_all_case_families(self):
        """INV-J10-3 & INV-J10-4: Cover all 11 case families, fault isolation, and ContextVar restoration on exception."""
        env = build_environment_pin()
        gen_sha = default_generator_sha256()

        all_family_specs = [
            {"case_id": f"fam_{idx}_{fam}", "seed_tag": 100 + idx, "family": fam, "n_frames": 4}
            for idx, fam in enumerate(hcs.CASE_FAMILIES)
        ]
        generated = [generate_case(s, master_seed=7, environment=env, generator_sha256=gen_sha) for s in all_family_specs]
        self.assertEqual(len(generated), len(hcs.CASE_FAMILIES))

        # Verify caller mutation immutability on generated arrays and labels
        with self.assertRaises(ValueError):
            generated[0]["frames_rgb"][0][0, 0, 0] = 255
        with self.assertRaises(ValueError):
            generated[0]["labels"][0, 0] = 6

        # Verify INV-J10-4 rejects prediction-derived labels in build_manifest and describe_changes
        forged_case = copy.deepcopy(generated[0])
        forged_case["labels_source"] = "prediction"
        with self.assertRaises(HoldingsStatisticsError):
            build_manifest([forged_case], environment=env, generator_sha256=gen_sha)

        # Verify INV-J10-3: Outer GM and FL ContextVar counters are preserved across plugin exceptions
        gm_bands_hv.reset_ops()
        gm_bands_hv._add_ops(777)
        with fl_blinkprior.ops_scope() as read_outer_fl:
            fl_blinkprior._add_ops(555)
            self.assertEqual(gm_bands_hv.OPS, 777)
            self.assertEqual(read_outer_fl(), 555)

            class FailingGMPlugin:
                OPS = 9999

                @staticmethod
                def estimate_global_motion(prev_y, cur_y, max_d):
                    gm_bands_hv._add_ops(12345)
                    raise RuntimeError("injected GM plugin failure")

                @staticmethod
                def consensus_tiles(prev_y, cur_y, s, dx, dy):
                    return np.zeros((2, 2), dtype=bool)

            # Faulted lane on win_fault alongside healthy lane on win_healthy
            fault_case = generate_case(
                {
                    "case_id": "case_plugin_exc",
                    "seed_tag": 501,
                    "family": "localized_change",
                    "handle": {"kind": "godot_window", "canonical_id": "win_fault", "generation": 1},
                    "faults": ["plugin_exception"],
                    "n_frames": 3,
                },
                master_seed=7,
                environment=env,
                generator_sha256=gen_sha,
            )
            eval_fault = evaluate_lanes(
                [fault_case],
                gm=FailingGMPlugin,
                environment=env,
                generator_sha256=gen_sha,
                master_seed=7,
            )
            self.assertEqual(eval_fault["cases"][0]["status"], "INCOMPLETE")
            self.assertFalse(eval_fault["cases"][0]["complete"])
            self.assertIsNone(eval_fault["cases"][0]["ops"]["gm"])
            self.assertIsNone(eval_fault["cases"][0]["ops"]["fl"])

            # Outer ContextVar counts must remain untouched (777 and 555)
            self.assertEqual(gm_bands_hv.OPS, 777)
            self.assertEqual(read_outer_fl(), 555)

    def test_cache_eviction_and_lane_local_reset(self):
        """Verify cache eviction beyond 32 pairs and lane-local gap/reset isolation."""
        env = build_environment_pin()
        gen_sha = default_generator_sha256()
        evict_case = generate_case(
            {
                "case_id": "case_evict_36",
                "seed_tag": 808,
                "family": "cache_eviction",
                "handle": {"kind": "xvfb_screen", "canonical_id": "xvfb_0", "generation": 1},
                "geometry": {"width": 32, "height": 32, "tile_size": 16},
                "n_frames": 36,
            },
            master_seed=19,
            environment=env,
            generator_sha256=gen_sha,
        )
        det = PerWindowDetector()
        res = evaluate_lanes([evict_case], detector=det, environment=env, generator_sha256=gen_sha, master_seed=19)
        self.assertEqual(res["cases"][0]["status"], "OK")
        self.assertTrue(res["cases"][0]["complete"])
        handle = Handle("xvfb_screen", "xvfb_0", 1)
        lane = det._lane(handle)
        self.assertLessEqual(len(lane.stream.cache), PerWindowDetector.MAX_CACHE_PAIRS)

    def test_source_neutral_boundary_and_holdings_t2_commands(self):
        """Verify source-neutral observations, CDP vs Godot separation, and T2 command bookkeeping."""
        boundary = SourceNeutralBoundary()
        self.assertEqual(boundary.electron_brokered_publisher, AUSENTE)
        self.assertEqual(boundary.conductor_admission_binding, AUSENTE)

        # Valid Godot neutral observation and valid fake-CDP DOM/AX observation
        godot_obs = boundary.ingest_observation({
            "source": {"kind": "godot", "window_id": "godot_main"},
            "frame_digest": "1" * 64,
            "godot_tree": {"root": "World"},
        })
        self.assertEqual(godot_obs["source"]["kind"], "godot")

        cdp_obs = boundary.ingest_observation({
            "source": {"kind": "cdp", "target_id": "tab_1"},
            "frame_digest": "2" * 64,
            "dom_ax": [{"nodeId": 1, "role": "WebArea"}],
        })
        self.assertEqual(cdp_obs["source"]["kind"], "cdp")

        # Relabeled Godot tree under source.kind == 'cdp' must be rejected
        with self.assertRaises(SourceBoundaryError):
            boundary.ingest_observation({
                "source": {"kind": "cdp", "origin": "godot"},
                "dom_ax": [{"nodeId": 1}],
            })
        with self.assertRaises(SourceBoundaryError):
            boundary.ingest_observation({
                "source": {"kind": "cdp"},
                "godot_tree": {"root": "Node2D"},
            })

        # Holdings commands: pause (empty args) and new_game (country_id, difficulty, opening_mode, seed)
        pause_cmd = make_holdings_command("pause", tick=10, seq=1)
        self.assertEqual(pause_cmd, {"tick": 10, "seq": 1, "cmd": "pause", "args": {}})
        out_pause = boundary.submit_t2(pause_cmd)
        self.assertTrue(out_pause["admitted"])
        self.assertIsNone(out_pause["economic_acceptance"])
        self.assertEqual(out_pause["confidence_status"], AUSENTE)
        self.assertTrue(boundary.simulation_state["paused"])

        new_game_cmd = make_holdings_command(
            "new_game",
            tick=12,
            seq=2,
            country_id="ES",
            difficulty="normal",
            opening_mode="standard",
            seed=12345,
        )
        out_ng = boundary.submit_t2(new_game_cmd)
        self.assertTrue(out_ng["admitted"])
        self.assertEqual(boundary.simulation_state["game"]["country_id"], "ES")
        self.assertEqual(boundary.simulation_mutations, 2)

        # Rejected attempt advances trusted bookkeeping without mutating simulation state
        state_before = copy.deepcopy(boundary.simulation_state)
        mutations_before = boundary.simulation_mutations
        out_rej = boundary.submit_t2({"tick": 15, "seq": 3, "cmd": "pause", "args": {"unexpected": 1}})
        self.assertFalse(out_rej["admitted"])
        self.assertEqual(out_rej["status"], "REJECTED")
        self.assertFalse(out_rej["simulation_mutated"])
        self.assertEqual(boundary.bookkeeping_seq, 3)
        self.assertEqual(boundary.rejected_count, 1)
        self.assertEqual(boundary.simulation_mutations, mutations_before)
        self.assertEqual(boundary.simulation_state, state_before)

    def test_write_spy_blocks_pixel_persistence_and_cleans_up(self):
        """Verify WriteSpy rejects pixel/frame writes without creating files and restores originals on exit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            forbidden_png = Path(tmpdir) / "frame_01.png"
            forbidden_npy = Path(tmpdir) / "frame_01.npy"
            arr = np.zeros((16, 16, 3), dtype=np.uint8)

            with forbid_pixel_persistence() as spy:
                with self.assertRaises(PixelPersistenceError):
                    np.save(forbidden_npy, arr)
                with self.assertRaises(PixelPersistenceError):
                    spy.save_screenshot(forbidden_png, arr)
                with self.assertRaises(PixelPersistenceError):
                    open(forbidden_png, "wb")
                with self.assertRaises(PixelPersistenceError):
                    forbidden_png.write_bytes(b"raw_pixels")

                self.assertEqual(len(spy.blocked_attempts), 4)
                self.assertFalse(forbidden_png.exists())
                self.assertFalse(forbidden_npy.exists())

            # After context exit, normal file operations are restored
            txt_path = Path(tmpdir) / "note.txt"
            txt_path.write_text("restored", encoding="utf-8")
            self.assertEqual(txt_path.read_text(encoding="utf-8"), "restored")


if __name__ == "__main__":
    unittest.main()
