"""Ordinary acceptance and implementation tests for `eval.v2eval.holdings_scorecard_bridge`.

Gate G J5 3 (`jq2b-j5-holdings-scorecard-bridge`):
- Lazily imports `eval.v2eval.multiwin_scorecard` inside an undecorated prerequisite test;
  absence fails loudly without `@unittest.skip` or `@unittest.expectedFailure`.
- Exercises the real public API `bridge_vision` and source-neutral boundary helpers with
  independent expected outputs across healthy controls and negative contrasts.
- Verifies INV-J5-1 (`eligible == completed + refused + incomplete`, exclusions separate),
  INV-J5-2 (matched outcomes and independent task postconditions; admission != economic
  acceptance; rejected attempts advance trusted bookkeeping without simulation mutation),
  INV-J5-3 (unequal denominators pooled by integer sums into rational `numerator`/`denominator`
  rates; absent/empty denominators stay `None` and `"UNKNOWN"`), and INV-J5-4 (correct safe
  refusal distinguished from economic completion).
- Verifies unsupported frames, mixed/missing seals, duplicate window identities, foreign
  generations, foreign episode bindings, empty battery, reward (`None` / `"DESIGNING"`),
  confidence theorem (`"AUSENTE"`), complete-episode uncertainty (`"UNSUPPORTED"`, no frame
  pseudoreplication), source-neutral Godot and fake-CDP-derived DOM/AX observations, inline
  Holdings T2 commands (`pause` and `new_game`), write spies rejecting pixel persistence, and
  `VM`-tagged diagnostics.

Spec citations:
- Section 10: $HB_DOCS/HYPERBOOKS_WEB_MGMT_v1.md:525
- Section 10: $HB_DOCS/HYPERBOOKS_WEB_MGMT_v1.md:529
- Section 10: $HB_DOCS/HYPERBOOKS_WEB_MGMT_v1.md:531
- Section 14: $HB_DOCS/HYPERBOOKS_WEB_MGMT_v1.md:764
"""
from __future__ import annotations

from dataclasses import replace
import copy
import hashlib
import importlib
import sys
import unittest
from typing import Any

import numpy as np

from eval.v2eval import holdings_scorecard_bridge as hsb
from eval.v2eval.holdings_scorecard_bridge import (
    AUSENTE,
    EXTERNAL_T2_FIELDS,
    ForeignEpisodeBindingError,
    GATE_G_J5_3,
    MEASURED_STATUS,
    NEW_GAME_ARG_FIELDS,
    PixelPersistenceError,
    REWARD_STATUS_DESIGNING,
    SPEC_CITATIONS,
    SourceKindBoundaryError,
    SourceNeutralBoundarySession,
    T2CommandContractError,
    TrustedEpisodeContext,
    UNCERTAINTY_STATUS_UNSUPPORTED,
    UNKNOWN_STATUS,
    VM_MEASUREMENT_TAG,
    WriteSpy,
    bridge_vision,
    make_t2_new_game,
    make_t2_pause,
    validate_external_t2_record,
    validate_procfs_cgroup_ram_fixtures,
    validate_source_neutral_observation,
    verify_dispatch_admission,
)


SEAL_VALID = "seal-j5-2026-8f03b9c41e72d50a69e4f12b38c07a91827364551029384756abcdef01234567"


def _make_ram_frame_ref(
    handle: Any,
    *,
    epoch: int = 0,
    frame_seq: int = 1,
    width: int = 16,
    height: int = 12,
    fill_value: int = 42,
) -> Any:
    mws = importlib.import_module("eval.v2eval.multiwin_scorecard")
    rgb = np.full((height, width, 3), fill_value, dtype=np.uint8)
    digest = hashlib.sha256(rgb.tobytes()).hexdigest()
    return mws.FrameRef(
        handle=handle,
        epoch=epoch,
        frame_seq=frame_seq,
        width=width,
        height=height,
        rgb=rgb,
        digest=digest,
    )


def _make_window_evidence(
    *,
    kind: str = "godot_window",
    canonical_id: str = "holdings-main",
    generation: int = 1,
    scene: str = "holdings_portfolio_scene",
    epoch: int = 0,
    frame_seq: int = 1,
    tp: int = 10,
    fp: int = 2,
    fn: int = 1,
    tn: int = 87,
    ops: dict[str, Any] | None = None,
    frame_supported: bool = True,
    dropped: bool = False,
    exact: bool | None = True,
    terminal: str | None = "COMPLETE",
    expected_terminal: str | None = "COMPLETE",
    seal: str = SEAL_VALID,
    violations: tuple[str, ...] = (),
) -> Any:
    mws = importlib.import_module("eval.v2eval.multiwin_scorecard")
    base_ops = {"decode": 1, "describe": 2, "change": 1, "gm": 1, "fl": 1,
                "isolated_ops": {"decode": 1, "describe": 2, "change": 1, "gm": 1, "fl": 1}}
    if ops is not None:
        base_ops.update(ops)
    return mws.WindowEvidence(
        handle=mws.Handle(kind=kind, canonical_id=canonical_id, generation=generation),
        scene=scene,
        epoch=epoch,
        frame_seq=frame_seq,
        tp=tp,
        fp=fp,
        fn=fn,
        tn=tn,
        ops=base_ops,
        frame_supported=frame_supported,
        dropped=dropped,
        exact=exact,
        terminal=terminal,
        expected_terminal=expected_terminal,
        seal=seal,
        violations=violations,
    )


def _seal_evidence(evidence, *, active_rows=None):
    """Seal the exact scored fixture batch; exclusions share its episode seal."""
    mws = importlib.import_module("eval.v2eval.multiwin_scorecard")
    rows = evidence["windows"] if isinstance(evidence, dict) else evidence
    scored_rows = tuple(rows) if active_rows is None else tuple(active_rows)
    report = mws.seal_trial(mws.TrialReport(rows=scored_rows, outcomes={}, faults=()))
    digest = report.rows[0].seal
    sealed_rows = tuple(replace(row, seal=digest) for row in rows)
    if isinstance(evidence, dict):
        return {**evidence, "windows": sealed_rows}, digest
    return sealed_rows, digest


class TestHoldingsScorecardBridgeImplementation(unittest.TestCase):
    """Gate G J5 3 test suite for `eval.v2eval.holdings_scorecard_bridge`."""

    def test_prerequisite_lazy_import_multiwin_scorecard_and_admission_guard(self) -> None:
        """Undecorated prerequisite test: lazily imports `eval.v2eval.multiwin_scorecard` and checks runtime identity."""
        mws = importlib.import_module("eval.v2eval.multiwin_scorecard")
        for required_symbol in (
            "score_windows",
            "canonical_bytes",
            "validate_scorecard",
            "WindowEvidence",
            "Scorecard",
            "Handle",
            "FrameRef",
            "Observation",
            "FakeClock",
        ):
            self.assertTrue(
                hasattr(mws, required_symbol),
                f"Missing required symbol {required_symbol!r} in eval.v2eval.multiwin_scorecard",
            )

        # Verify holdings_scorecard_bridge reuses exact runtime types from multiwin_scorecard (no private copies)
        self.assertIs(hsb.WindowEvidence, mws.WindowEvidence)
        self.assertIs(hsb.Scorecard, mws.Scorecard)
        self.assertIs(hsb.Handle, mws.Handle)
        self.assertIs(hsb.score_windows, mws.score_windows)
        self.assertIs(hsb.canonical_bytes, mws.canonical_bytes)

        # Verify no wender, Godot, or CDP adapter packages are imported in sys.modules by the core
        for mod_name in tuple(sys.modules.keys()):
            self.assertFalse(
                mod_name == "wender" or mod_name.startswith("wender."),
                f"Forbidden cross-repo wender import detected: {mod_name}",
            )

        # Verify dispatch admission guard holds when host/conductor receipts are AUSENTE vs admitted when proven
        hold_admission = verify_dispatch_admission(
            provider_revision=AUSENTE,
            gate_receipt=None,
            clone_revision=AUSENTE,
            conductor_binding=AUSENTE,
        )
        self.assertFalse(hold_admission["admitted"])
        self.assertEqual(hold_admission["dispatch_status"], "HOLD")
        self.assertEqual(hold_admission["provider_revision"], AUSENTE)
        self.assertEqual(hold_admission["gate_receipt"], AUSENTE)
        self.assertIn("provider_revision", hold_admission["missing_prerequisites"])

        proven_admission = verify_dispatch_admission(
            provider_revision="95ca4fbbbcfb5c0cb99331d271b72323ba66db3c",
            gate_receipt="jq2a-m7-g1-vision-scorecard:a1:PASS",
            clone_revision="e2346eb96916552dff79275d70e61a6dc9f484a5",
            conductor_binding="lease-bb53200393c88aee",
        )
        self.assertTrue(proven_admission["admitted"])
        self.assertEqual(proven_admission["dispatch_status"], "ADMITTED")
        self.assertEqual(proven_admission["missing_prerequisites"], ())
        self.assertNotEqual(hold_admission["admitted"], proven_admission["admitted"])

    def test_gate_g_j5_3_healthy_control_preserves_scorecard_and_places_episode_ref_outside(self) -> None:
        """Gate G J5 3: healthy independent control preserves Scorecard untouched and binds episode_ref outside."""
        mws = importlib.import_module("eval.v2eval.multiwin_scorecard")
        w1 = _make_window_evidence(
            kind="godot_window",
            canonical_id="holdings-win-1",
            generation=2,
            scene="scene_portfolio",
            epoch=0,
            frame_seq=1,
            tp=14,
            fp=2,
            fn=1,
            tn=83,
        )
        w2 = _make_window_evidence(
            kind="cdp_tab",
            canonical_id="cdp-tab-1",
            generation=2,
            scene="scene_news_feed",
            epoch=0,
            frame_seq=1,
            tp=26,
            fp=3,
            fn=4,
            tn=167,
        )
        sealed_rows, evidence_seal = _seal_evidence((w1, w2))
        original_scorecard = mws.score_windows(sealed_rows)
        bytes_before = mws.canonical_bytes(original_scorecard)
        pooled_keys_before = set(original_scorecard.pooled.keys())

        ep_ref = {
            "episode_id": "ep-holdings-2026-001",
            "seal": evidence_seal,
            "generation": 2,
            "trusted": True,
        }
        ep_ref_copy = copy.deepcopy(ep_ref)

        wrapped = bridge_vision(original_scorecard, episode_ref=ep_ref)

        # Check status, gate, citations, and identity preservation
        self.assertEqual(wrapped["gate"], GATE_G_J5_3)
        self.assertEqual(wrapped["status"], "PASSED")
        self.assertTrue(wrapped["passed"])
        self.assertFalse(wrapped["refused_before_aggregation"])
        self.assertIsNone(wrapped["error_code"])
        self.assertIs(wrapped["scorecard"], original_scorecard)
        self.assertEqual(mws.canonical_bytes(wrapped["scorecard"]), bytes_before)
        self.assertEqual(set(wrapped["scorecard"].pooled.keys()), pooled_keys_before)
        self.assertNotIn("episode_ref", wrapped["scorecard"].pooled)
        self.assertFalse(hasattr(wrapped["scorecard"], "episode_ref"))

        # Check outer episode_ref and immutable caller input
        self.assertEqual(wrapped["episode_id"], "ep-holdings-2026-001")
        self.assertEqual(wrapped["episode_seal"], evidence_seal)
        self.assertEqual(wrapped["episode_ref"]["episode_id"], "ep-holdings-2026-001")
        self.assertEqual(wrapped["episode_ref"]["seal"], evidence_seal)
        self.assertEqual(ep_ref, ep_ref_copy)
        self.assertEqual(wrapped["citations"], SPEC_CITATIONS)
        self.assertEqual(
            SPEC_CITATIONS,
            (
                "$HB_DOCS/HYPERBOOKS_WEB_MGMT_v1.md:525",
                "$HB_DOCS/HYPERBOOKS_WEB_MGMT_v1.md:529",
                "$HB_DOCS/HYPERBOOKS_WEB_MGMT_v1.md:531",
                "$HB_DOCS/HYPERBOOKS_WEB_MGMT_v1.md:764",
            ),
        )

        # Negative contrast: untrusted / missing episode_ref refuses before aggregation
        untrusted_wrapped = bridge_vision(
            original_scorecard,
            episode_ref={"episode_id": "ep-holdings-2026-001", "seal": evidence_seal, "trusted": False},
        )
        self.assertEqual(untrusted_wrapped["status"], "REFUSED")
        self.assertFalse(untrusted_wrapped["passed"])
        self.assertTrue(untrusted_wrapped["refused_before_aggregation"])
        self.assertEqual(untrusted_wrapped["error_code"], "E_FOREIGN_EPISODE_BINDING")
        self.assertNotEqual(wrapped["passed"], untrusted_wrapped["passed"])
        self.assertNotEqual(wrapped["status"], untrusted_wrapped["status"])

    def test_inv_j5_1_eligible_partition_and_separate_exclusions(self) -> None:
        """INV-J5-1: keeps eligible == completed + refused + incomplete, with exclusions separate."""
        w_completed = _make_window_evidence(
            canonical_id="win-comp",
            scene="scene_comp",
            frame_seq=1,
            terminal="COMPLETE",
            expected_terminal="COMPLETE",
        )
        w_refused = _make_window_evidence(
            canonical_id="win-ref",
            scene="scene_ref",
            frame_seq=1,
            terminal="REFUSED",
            expected_terminal="REFUSED",
        )
        w_incomplete = _make_window_evidence(
            canonical_id="win-inc",
            scene="scene_inc",
            frame_seq=1,
            terminal="TIMEOUT",
            expected_terminal="COMPLETE",
        )
        w_excluded_1 = _make_window_evidence(
            canonical_id="win-excl-1",
            scene="scene_excl_1",
            frame_seq=1,
            terminal="EXCLUDED",
            expected_terminal="EXCLUDED",
            ops={"excluded": True},
        )
        w_excluded_2 = _make_window_evidence(
            canonical_id="win-excl-2",
            scene="scene_excl_2",
            frame_seq=1,
            terminal="COMPLETE",
            expected_terminal="COMPLETE",
        )

        evidence = {
            "windows": (w_completed, w_refused, w_incomplete, w_excluded_1, w_excluded_2),
            "exclusions": ("scene_excl_2",),
            "tasks": (
                {
                    "task_id": "task-extra-completed",
                    "terminal": "COMPLETE",
                    "expected_terminal": "COMPLETE",
                    "postcondition_verified": True,
                },
                {
                    "task_id": "task-extra-excluded",
                    "terminal": "COMPLETE",
                    "expected_terminal": "COMPLETE",
                    "excluded": True,
                },
            ),
        }

        sealed_evidence, evidence_seal = _seal_evidence(evidence, active_rows=(w_completed, w_refused, w_incomplete))
        res = bridge_vision(
            sealed_evidence,
            episode_ref={"episode_id": "ep-j5-1", "seal": evidence_seal, "generation": 1},
        )
        counts = res["counts"]
        self.assertEqual(counts["completed"], 2)
        self.assertEqual(counts["refused"], 1)
        self.assertEqual(counts["incomplete"], 1)
        self.assertEqual(counts["eligible"], 4)
        self.assertEqual(
            counts["eligible"],
            counts["completed"] + counts["refused"] + counts["incomplete"],
        )
        self.assertEqual(counts["exclusions"], 3)
        self.assertEqual(counts["total"], counts["eligible"] + counts["exclusions"])
        self.assertFalse(res["passed"])
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("incomplete_items_present", res["violations"])

    def test_inv_j5_2_matched_outcomes_and_independent_postconditions_vs_admission(self) -> None:
        """INV-J5-2: demands matched outcomes and independent task postconditions; admission != completion."""
        w_ok = _make_window_evidence(
            canonical_id="win-1",
            scene="scene_postcond_check",
            frame_seq=1,
            terminal="COMPLETE",
            expected_terminal="COMPLETE",
        )

        # Healthy control: outcome matches AND independent postcondition is True
        sealed_evidence, evidence_seal = _seal_evidence({
                "windows": (w_ok,),
                "postconditions": {"scene_postcond_check": True},
                "expected_outcomes": {"scene_postcond_check": " ledger_balanced "},
            })
        healthy = bridge_vision(
            sealed_evidence,
            episode_ref={"episode_id": "ep-j5-2-ok", "seal": evidence_seal, "generation": 1},
        )
        self.assertTrue(healthy["passed"])
        self.assertEqual(healthy["status"], "PASSED")
        self.assertEqual(healthy["counts"]["completed"], 1)
        self.assertEqual(healthy["counts"]["incomplete"], 0)
        self.assertEqual(healthy["rates"]["economic_completion_rate"]["fraction"], "1/1")

        # Negative contrast 1: terminal says COMPLETE, but independent task postcondition is False
        sealed_evidence, evidence_seal = _seal_evidence({
                "windows": (w_ok,),
                "postconditions": {"scene_postcond_check": False},
            })
        failed_postcond = bridge_vision(
            sealed_evidence,
            episode_ref={"episode_id": "ep-j5-2-fail-pc", "seal": evidence_seal, "generation": 1},
        )
        self.assertFalse(failed_postcond["passed"])
        self.assertEqual(failed_postcond["status"], "FAILED")
        self.assertEqual(failed_postcond["counts"]["completed"], 0)
        self.assertEqual(failed_postcond["counts"]["incomplete"], 1)
        self.assertEqual(failed_postcond["counts"]["unverified_postconditions"], 1)
        self.assertIn("unverified_or_failed_postcondition", failed_postcond["violations"])
        self.assertEqual(failed_postcond["rates"]["economic_completion_rate"]["fraction"], "0/1")

        # Negative contrast 2: command was merely ADMITTED without verified completion postcondition
        w_admitted_only = _make_window_evidence(
            canonical_id="win-1",
            scene="scene_admitted_only",
            frame_seq=1,
            terminal="COMPLETE",
            expected_terminal="COMPLETE",
            ops={"admitted_only": 1, "isolated_ops": {
                "decode": 1, "describe": 2, "change": 1, "gm": 1, "fl": 1,
                "admitted_only": 1,
            }},
        )
        sealed_evidence, evidence_seal = _seal_evidence((w_admitted_only,))
        admitted_res = bridge_vision(
            sealed_evidence,
            episode_ref={"episode_id": "ep-j5-2-admitted", "seal": evidence_seal, "generation": 1},
        )
        self.assertFalse(admitted_res["passed"])
        self.assertEqual(admitted_res["counts"]["completed"], 0)
        self.assertEqual(admitted_res["counts"]["incomplete"], 1)
        self.assertEqual(admitted_res["counts"]["admitted_without_completion"], 1)
        self.assertIn("admission_without_economic_completion", admitted_res["violations"])
        self.assertNotEqual(healthy["passed"], failed_postcond["passed"])
        self.assertNotEqual(healthy["passed"], admitted_res["passed"])

    def test_inv_j5_3_unequal_denominators_and_absent_denominators_null_unknown(self) -> None:
        """INV-J5-3: unequal denominators pooled from integer sums; absent denominators stay None and UNKNOWN."""
        # Window 1 has small denominator: tp=1, fp=0, fn=0, tn=1 (window precision=1/1=1000 permille, recall=1/1=1000)
        w_small = _make_window_evidence(
            canonical_id="win-small",
            scene="scene_small",
            frame_seq=1,
            tp=1,
            fp=0,
            fn=0,
            tn=1,
        )
        # Window 2 has large denominator: tp=10, fp=10, fn=30, tn=50 (window precision=10/20=500 permille, recall=10/40=250)
        w_large = _make_window_evidence(
            canonical_id="win-large",
            scene="scene_large",
            frame_seq=1,
            tp=10,
            fp=10,
            fn=30,
            tn=50,
        )

        sealed_evidence, evidence_seal = _seal_evidence((w_small, w_large))
        res = bridge_vision(
            sealed_evidence,
            episode_ref={"episode_id": "ep-unequal-den", "seal": evidence_seal, "generation": 1},
        )
        # Pooled integer counts: tp=11, fp=10, fn=30, tn=51
        self.assertEqual(res["counts"]["tp"], 11)
        self.assertEqual(res["counts"]["fp"], 10)
        self.assertEqual(res["counts"]["fn"], 30)
        self.assertEqual(res["counts"]["tn"], 51)

        # True rational pooled precision = 11 / 21 (permille = 523), NOT average of window ratios (1000 + 500)//2 = 750
        prec = res["rates"]["precision"]
        self.assertEqual(prec["numerator"], 11)
        self.assertEqual(prec["denominator"], 21)
        self.assertEqual(prec["fraction"], "11/21")
        self.assertEqual(prec["permille"], (11 * 1000) // 21)
        self.assertNotEqual(prec["permille"], 750)
        self.assertEqual(prec["status"], MEASURED_STATUS)

        # True rational pooled recall = 11 / 41 (permille = 268), NOT average of window ratios (1000 + 250)//2 = 625
        rec = res["rates"]["recall"]
        self.assertEqual(rec["numerator"], 11)
        self.assertEqual(rec["denominator"], 41)
        self.assertEqual(rec["fraction"], "11/41")
        self.assertEqual(rec["permille"], (11 * 1000) // 41)
        self.assertNotEqual(rec["permille"], 625)
        self.assertEqual(rec["status"], MEASURED_STATUS)

        # Absent positive support denominator (tp=0, fp=0, fn=0, tn=50): precision & recall must be None and UNKNOWN
        w_zero_pos = _make_window_evidence(
            canonical_id="win-zero-pos",
            scene="scene_zero_pos",
            frame_seq=1,
            tp=0,
            fp=0,
            fn=0,
            tn=50,
        )
        sealed_evidence, evidence_seal = _seal_evidence((w_zero_pos,))
        res_zero = bridge_vision(
            sealed_evidence,
            episode_ref={"episode_id": "ep-zero-pos", "seal": evidence_seal, "generation": 1},
        )
        self.assertFalse(res_zero["passed"])
        for rate_key in ("precision", "recall", "f1"):
            rate_obj = res_zero["rates"][rate_key]
            self.assertEqual(rate_obj["numerator"], 0)
            self.assertIsNone(rate_obj["denominator"])
            self.assertIsNone(rate_obj["value"])
            self.assertIsNone(rate_obj["fraction"])
            self.assertIsNone(rate_obj["permille"])
            self.assertEqual(rate_obj["status"], UNKNOWN_STATUS)
            self.assertFalse(rate_obj["defined"])
            self.assertFalse(rate_obj["passed"])

    def test_inv_j5_4_refused_cases_distinguish_safe_refusal_from_economic_completion(self) -> None:
        """INV-J5-4: distinguishes correct safe refusal from economic completion and unexpected refusal."""
        w_safe_refusal = _make_window_evidence(
            canonical_id="win-safe-ref",
            scene="scene_unsafe_action",
            frame_seq=1,
            terminal="REFUSED",
            expected_terminal="REFUSED",
        )
        w_econ_complete = _make_window_evidence(
            canonical_id="win-econ-ok",
            scene="scene_nominal_action",
            frame_seq=1,
            terminal="COMPLETE",
            expected_terminal="COMPLETE",
        )

        # Episode with 1 economic completion and 1 expected safe refusal
        sealed_evidence, evidence_seal = _seal_evidence((w_safe_refusal, w_econ_complete))
        mixed_res = bridge_vision(
            sealed_evidence,
            episode_ref={"episode_id": "ep-j5-4-safe", "seal": evidence_seal, "generation": 1},
        )
        self.assertTrue(mixed_res["passed"])
        self.assertEqual(mixed_res["status"], "PASSED")
        self.assertEqual(mixed_res["counts"]["eligible"], 2)
        self.assertEqual(mixed_res["counts"]["completed"], 1)
        self.assertEqual(mixed_res["counts"]["refused"], 1)
        self.assertEqual(mixed_res["counts"]["correct_safe_refusals"], 1)
        self.assertEqual(mixed_res["counts"]["unexpected_refusals"], 0)
        self.assertEqual(mixed_res["counts"]["incomplete"], 0)

        # Economic completion rate is 1/2 (NOT 2/2!), while safe_refusal_accuracy is 1/1
        self.assertEqual(mixed_res["rates"]["economic_completion_rate"]["numerator"], 1)
        self.assertEqual(mixed_res["rates"]["economic_completion_rate"]["denominator"], 2)
        self.assertEqual(mixed_res["rates"]["economic_completion_rate"]["fraction"], "1/2")
        self.assertEqual(mixed_res["rates"]["refusal_rate"]["fraction"], "1/2")
        self.assertEqual(mixed_res["rates"]["safe_refusal_accuracy"]["fraction"], "1/1")

        # Negative contrast: unexpected refusal (when COMPLETE was expected) fails acceptance
        w_unexpected_refusal = _make_window_evidence(
            canonical_id="win-unexp-ref",
            scene="scene_nominal_refused",
            frame_seq=1,
            terminal="REFUSED",
            expected_terminal="COMPLETE",
        )
        sealed_evidence, evidence_seal = _seal_evidence((w_unexpected_refusal, w_econ_complete))
        unexp_res = bridge_vision(
            sealed_evidence,
            episode_ref={"episode_id": "ep-j5-4-unexp", "seal": evidence_seal, "generation": 1},
        )
        self.assertFalse(unexp_res["passed"])
        self.assertEqual(unexp_res["status"], "FAILED")
        self.assertEqual(unexp_res["counts"]["completed"], 1)
        self.assertEqual(unexp_res["counts"]["refused"], 1)
        self.assertEqual(unexp_res["counts"]["correct_safe_refusals"], 0)
        self.assertEqual(unexp_res["counts"]["unexpected_refusals"], 1)
        self.assertIn("unexpected_refusal", unexp_res["violations"])
        self.assertIsNone(unexp_res["rates"]["safe_refusal_accuracy"]["denominator"])
        self.assertEqual(unexp_res["rates"]["safe_refusal_accuracy"]["status"], UNKNOWN_STATUS)
        self.assertNotEqual(mixed_res["passed"], unexp_res["passed"])

    def test_unsupported_frames_reward_designing_and_episode_uncertainty_no_pseudoreplication(self) -> None:
        """Unsupported frames fail with UNSUPPORTED/UNKNOWN; reward stays null/DESIGNING; no frame pseudoreplication."""
        w_unsupported = _make_window_evidence(
            canonical_id="win-unsup",
            scene="scene_unsup",
            frame_seq=1,
            tp=0,
            fp=0,
            fn=0,
            tn=0,
            frame_supported=False,
            dropped=False,
            exact=None,
        )
        sealed_evidence, evidence_seal = _seal_evidence((w_unsupported,))
        res = bridge_vision(
            sealed_evidence,
            episode_ref={"episode_id": "ep-unsup", "seal": evidence_seal, "generation": 1},
        )
        self.assertFalse(res["passed"])
        self.assertEqual(res["status"], UNCERTAINTY_STATUS_UNSUPPORTED)
        self.assertEqual(res["counts"]["unsupported_frames"], 1)
        self.assertEqual(res["counts"]["supported_frames"], 0)
        self.assertIn("unsupported_frames_present", res["violations"])
        self.assertIsNone(res["rates"]["frame_exactness_rate"]["denominator"])
        self.assertEqual(res["rates"]["frame_exactness_rate"]["status"], UNKNOWN_STATUS)

        # Check reward and complete-episode uncertainty fields
        self.assertIsNone(res["reward"])
        self.assertEqual(res["reward_status"], REWARD_STATUS_DESIGNING)
        self.assertEqual(res["confidence_theorem"], AUSENTE)
        uncertainty = res["episode_uncertainty"]
        self.assertEqual(uncertainty["status"], UNCERTAINTY_STATUS_UNSUPPORTED)
        self.assertEqual(uncertainty["confidence_theorem"], AUSENTE)
        self.assertFalse(uncertainty["frame_pseudoreplication"])
        self.assertIsNone(uncertainty["ci_lower"])
        self.assertIsNone(uncertainty["ci_upper"])

        # Negative contrast: attempting frame pseudoreplication refuses before aggregation
        sealed_evidence, evidence_seal = _seal_evidence((_make_window_evidence(),))
        pseudo_res = bridge_vision(
            sealed_evidence,
            episode_ref={"episode_id": "ep-pseudo", "seal": evidence_seal, "generation": 1},
            use_frame_pseudoreplication=True,
        )
        self.assertEqual(pseudo_res["status"], "REFUSED")
        self.assertFalse(pseudo_res["passed"])
        self.assertTrue(pseudo_res["refused_before_aggregation"])
        self.assertEqual(pseudo_res["error_code"], "E_FRAME_PSEUDOREPLICATION_FORBIDDEN")

    def test_mixed_and_missing_seals_refuse_before_aggregation(self) -> None:
        """Mixed seals or missing seals must refuse before aggregation without calling the injected scorer."""
        scorer_calls = 0

        def spy_scorer(rows: Any) -> Any:
            nonlocal scorer_calls
            scorer_calls += 1
            return hsb.score_windows(rows)

        w_seal_a = _make_window_evidence(canonical_id="w1", scene="s1", seal=SEAL_VALID)
        w_seal_b = _make_window_evidence(canonical_id="w2", scene="s2", seal="seal-other-999")
        w_seal_empty = _make_window_evidence(canonical_id="w3", scene="s3", seal="")

        # 1. Mixed seals across windows
        res_mixed = bridge_vision(
            (w_seal_a, w_seal_b),
            episode_ref={"episode_id": "ep-mixed-seal", "seal": SEAL_VALID, "generation": 1},
            scorer=spy_scorer,
        )
        self.assertEqual(scorer_calls, 0)
        self.assertEqual(res_mixed["status"], "REFUSED")
        self.assertFalse(res_mixed["passed"])
        self.assertTrue(res_mixed["refused_before_aggregation"])
        self.assertFalse(res_mixed["scorer_invoked"])
        self.assertEqual(res_mixed["error_code"], "E_SEAL_MISMATCH")

        # 2. Missing seal on window
        res_missing = bridge_vision(
            (w_seal_empty,),
            episode_ref={"episode_id": "ep-missing-seal", "seal": SEAL_VALID, "generation": 1},
            scorer=spy_scorer,
        )
        self.assertEqual(scorer_calls, 0)
        self.assertEqual(res_missing["status"], "REFUSED")
        self.assertTrue(res_missing["refused_before_aggregation"])
        self.assertEqual(res_missing["error_code"], "E_SEAL_MISMATCH")

        # 3. Episode seal disagrees with window seal
        res_ep_mismatch = bridge_vision(
            (w_seal_a,),
            episode_ref={"episode_id": "ep-mismatch-seal", "seal": "seal-different", "generation": 1},
            scorer=spy_scorer,
        )
        self.assertEqual(scorer_calls, 0)
        self.assertEqual(res_ep_mismatch["status"], "REFUSED")
        self.assertTrue(res_ep_mismatch["refused_before_aggregation"])
        self.assertEqual(res_ep_mismatch["error_code"], "E_SEAL_MISMATCH")

    def test_duplicate_windows_refuse_before_aggregation(self) -> None:
        """Duplicate window identities refuse before aggregation without invoking scorer."""
        scorer_calls = 0

        def spy_scorer(rows: Any) -> Any:
            nonlocal scorer_calls
            scorer_calls += 1
            return hsb.score_windows(rows)

        w_dup_1 = _make_window_evidence(canonical_id="win-dup", scene="scene_a", epoch=2, frame_seq=7)
        w_dup_2 = _make_window_evidence(canonical_id="win-dup", scene="scene_a", epoch=2, frame_seq=7)

        res_dup = bridge_vision(
            (w_dup_1, w_dup_2),
            episode_ref={"episode_id": "ep-dup", "seal": SEAL_VALID, "generation": 1},
            scorer=spy_scorer,
        )
        self.assertEqual(scorer_calls, 0)
        self.assertEqual(res_dup["status"], "REFUSED")
        self.assertFalse(res_dup["passed"])
        self.assertTrue(res_dup["refused_before_aggregation"])
        self.assertFalse(res_dup["scorer_invoked"])
        self.assertEqual(res_dup["error_code"], "E_WINDOW_IDENTITY")

        # Verify raise_on_refusal=True raises WindowIdentityError
        with self.assertRaises(hsb.WindowIdentityError):
            bridge_vision(
                (w_dup_1, w_dup_2),
                episode_ref={"episode_id": "ep-dup", "seal": SEAL_VALID, "generation": 1},
                scorer=spy_scorer,
                raise_on_refusal=True,
            )
        self.assertEqual(scorer_calls, 0)

    def test_foreign_generation_and_foreign_episode_binding_refuse_before_aggregation(self) -> None:
        """Foreign handle generation or foreign episode binding refuses before aggregation."""
        scorer_calls = 0

        def spy_scorer(rows: Any) -> Any:
            nonlocal scorer_calls
            scorer_calls += 1
            return hsb.score_windows(rows)

        # Window has generation=2 while trusted episode_ref binds generation=1
        w_foreign_gen = _make_window_evidence(
            canonical_id="holdings-main",
            generation=2,
            scene="scene_a",
            frame_seq=1,
        )
        res_gen = bridge_vision(
            (w_foreign_gen,),
            episode_ref={"episode_id": "ep-gen-1", "seal": SEAL_VALID, "generation": 1},
            scorer=spy_scorer,
        )
        self.assertEqual(scorer_calls, 0)
        self.assertEqual(res_gen["status"], "REFUSED")
        self.assertFalse(res_gen["passed"])
        self.assertTrue(res_gen["refused_before_aggregation"])
        self.assertEqual(res_gen["error_code"], "E_FOREIGN_EPISODE_BINDING")

        # Window ops binds to a foreign episode_id
        w_foreign_ep = _make_window_evidence(
            canonical_id="holdings-main",
            generation=1,
            scene="scene_a",
            frame_seq=1,
            ops={"episode_id": "ep-foreign-999"},
        )
        res_ep = bridge_vision(
            (w_foreign_ep,),
            episode_ref={"episode_id": "ep-gen-1", "seal": SEAL_VALID, "generation": 1},
            scorer=spy_scorer,
        )
        self.assertEqual(scorer_calls, 0)
        self.assertEqual(res_ep["status"], "REFUSED")
        self.assertTrue(res_ep["refused_before_aggregation"])
        self.assertEqual(res_ep["error_code"], "E_FOREIGN_EPISODE_BINDING")

    def test_empty_battery_null_unknown_denominators_and_fails_acceptance(self) -> None:
        """Empty battery keeps all denominators None and UNKNOWN, with zero counts and passed=False."""
        res_empty = bridge_vision(
            (),
            episode_ref={"episode_id": "ep-empty-battery", "seal": SEAL_VALID, "generation": 1},
        )
        self.assertFalse(res_empty["passed"])
        self.assertEqual(res_empty["status"], "EMPTY_BATTERY")
        self.assertFalse(res_empty["refused_before_aggregation"])
        self.assertIn("empty_battery", res_empty["violations"])

        counts = res_empty["counts"]
        for k in (
            "total",
            "eligible",
            "completed",
            "refused",
            "incomplete",
            "exclusions",
            "tp",
            "fp",
            "fn",
            "tn",
        ):
            self.assertEqual(counts[k], 0)

        for rate_name, rate_obj in res_empty["rates"].items():
            self.assertIsNone(rate_obj["denominator"], f"{rate_name} denominator must be None on empty battery")
            self.assertIsNone(rate_obj["value"], f"{rate_name} value must be None on empty battery")
            self.assertIsNone(rate_obj["fraction"], f"{rate_name} fraction must be None on empty battery")
            self.assertEqual(rate_obj["status"], UNKNOWN_STATUS, f"{rate_name} status must be UNKNOWN")
            self.assertFalse(rate_obj["passed"], f"{rate_name} must not pass on empty battery")

    def test_source_neutral_godot_and_fake_cdp_boundary_and_t2_bookkeeping(self) -> None:
        """Source-neutral boundary: Godot and fake-CDP observations, relabeled Godot rejection, T2 commands & bookkeeping."""
        mws = importlib.import_module("eval.v2eval.multiwin_scorecard")
        h_godot = mws.Handle(kind="godot_window", canonical_id="holdings-godot-main", generation=1)
        h_cdp = mws.Handle(kind="cdp_tab", canonical_id="holdings-cdp-companion", generation=1)

        # 1. Valid Godot observation and valid fake-CDP-derived DOM/AX observation
        fr_godot = _make_ram_frame_ref(h_godot, epoch=0, frame_seq=1, fill_value=64)
        obs_godot = mws.Observation(
            handle=h_godot,
            epoch=0,
            frame_seq=1,
            dpr_ppm=1_000_000,
            state={
                "source": {"kind": "godot", "url": "godot://holdings/main", "title": "Holdings"},
                "ui_state": "paused",
                "nodes": [{"id": "0123456789abcdef", "role": "application", "name": "Holdings"}],
            },
            frame=fr_godot,
        )

        fr_cdp = _make_ram_frame_ref(h_cdp, epoch=0, frame_seq=1, fill_value=128)
        obs_cdp = mws.Observation(
            handle=h_cdp,
            epoch=0,
            frame_seq=1,
            dpr_ppm=1_000_000,
            state={
                "source": {"kind": "cdp", "url": "https://holdings.local/companion", "title": "Manual"},
                "dom_ax_tree": "cdp_dom_ax_v1",
                "nodes": [{"id": "dom-node-1", "role": "document", "name": "Manual"}],
            },
            frame=fr_cdp,
        )

        # 2. Construct inline Holdings T2 records using COMMANDS.md and CONTRACTS.md field names
        cmd_new_game = make_t2_new_game(
            tick=0,
            seq=1,
            country_id="ESP",
            difficulty="standard",
            opening_mode="historical",
            seed=20261020,
        )
        cmd_pause = make_t2_pause(tick=5, seq=2)
        self.assertEqual(tuple(cmd_new_game.keys()), EXTERNAL_T2_FIELDS)
        self.assertEqual(tuple(cmd_new_game["args"].keys()), NEW_GAME_ARG_FIELDS)
        self.assertEqual(cmd_pause["args"], {})

        # Reject extra authority fields inside external T2 payload
        with self.assertRaises(T2CommandContractError):
            validate_external_t2_record(
                {"tick": 1, "seq": 1, "cmd": "pause", "args": {}, "owner": "attacker"}
            )
        with self.assertRaises(T2CommandContractError):
            validate_external_t2_record(
                {"tick": 1, "seq": 1, "cmd": "pause", "args": {"unexpected": 1}}
            )
        with self.assertRaises(T2CommandContractError):
            validate_external_t2_record(
                {"tick": 1, "seq": 1, "cmd": "new_game", "args": {"seed": 1}}
            )

        # 3. Exercise SourceNeutralBoundarySession: admission vs economic completion, rejected attempts, reset
        ctx = TrustedEpisodeContext(
            episode_id="ep-boundary-001",
            seal=SEAL_VALID,
            allowed_handles=(h_godot, h_cdp),
        )
        session = SourceNeutralBoundarySession(trusted_context=ctx)
        self.assertEqual(session.observe(obs_godot)["source_kind"], "godot")
        self.assertEqual(session.observe(obs_cdp)["source_kind"], "cdp")

        # Submit new_game with independent postcondition verifying country_id and seed
        r_new_game = session.submit_t2(
            cmd_new_game,
            target_handle=h_godot,
            postcondition_fn=lambda st: st["country_id"] == "ESP" and st["seed"] == 20261020,
        )
        self.assertEqual(r_new_game["status"], "COMPLETED")
        self.assertTrue(r_new_game["admitted"])
        self.assertTrue(r_new_game["economic_completed"])
        self.assertTrue(r_new_game["sim_mutated"])

        # Rejected attempt: invalid/stale sequence number (seq=1 <= last_seq=1)
        # Must advance trusted bookkeeping without mutating simulation state!
        sim_before_reject = copy.deepcopy(session.sim_state)
        r_rejected = session.submit_t2(
            make_t2_pause(tick=5, seq=1),
            target_handle=h_godot,
        )
        self.assertEqual(r_rejected["status"], "REFUSED")
        self.assertFalse(r_rejected["admitted"])
        self.assertFalse(r_rejected["economic_completed"])
        self.assertFalse(r_rejected["sim_mutated"])
        self.assertEqual(session.sim_state, sim_before_reject)
        self.assertEqual(r_rejected["bookkeeping"]["attempted"], 2)
        self.assertEqual(r_rejected["bookkeeping"]["rejected"], 1)
        self.assertEqual(r_rejected["bookkeeping"]["admitted"], 1)

        # Admitted attempt whose independent postcondition fails: admitted=True, economic_completed=False
        r_admitted_only = session.submit_t2(
            cmd_pause,
            target_handle=h_godot,
            postcondition_fn=lambda _st: False,
        )
        self.assertEqual(r_admitted_only["status"], "ADMITTED")
        self.assertTrue(r_admitted_only["admitted"])
        self.assertFalse(r_admitted_only["economic_completed"])
        self.assertEqual(r_admitted_only["bookkeeping"]["admitted"], 2)
        self.assertEqual(r_admitted_only["bookkeeping"]["economically_completed"], 1)

        # Reset cleans up session state
        reset_bk = session.reset()
        self.assertTrue(session.cleaned_up)
        self.assertEqual(reset_bk["resets"], 1)
        self.assertEqual(session.sim_state["mutations"], 0)
        self.assertEqual(len(session.observations), 0)

        # 4. Bridge integration with both Godot and fake-CDP observations + T2 records
        w_godot = _make_window_evidence(kind="godot_window", canonical_id="holdings-godot-main", generation=1, scene="s_godot")
        w_cdp = _make_window_evidence(kind="cdp_tab", canonical_id="holdings-cdp-companion", generation=1, scene="s_cdp")
        sealed_evidence, evidence_seal = _seal_evidence((w_godot, w_cdp))
        bridge_ok = bridge_vision(
            sealed_evidence,
            episode_ref=replace(ctx, seal=evidence_seal),
            observations=(obs_godot, obs_cdp),
            t2_submissions=(cmd_new_game, cmd_pause),
        )
        self.assertTrue(bridge_ok["passed"])
        self.assertEqual(bridge_ok["source_boundary"]["electron_brokered_publisher"], AUSENTE)
        self.assertTrue(bridge_ok["source_boundary"]["cleanup_completed"])
        self.assertEqual(len(bridge_ok["source_boundary"]["observations"]), 2)
        self.assertEqual(len(bridge_ok["source_boundary"]["t2_submissions"]), 2)

        # 5. Negative contrast: relabeled Godot tree claiming `source.kind == "cdp"` must refuse!
        obs_relabeled_godot = {
            "handle": h_cdp,
            "epoch": 0,
            "frame_seq": 1,
            "dpr_ppm": 1_000_000,
            "state": {
                "source": {"kind": "cdp", "url": "godot://holdings/main", "title": "Relabeled"},
                "sidecar_schema": "holdings.godot.uitree/v1",
                "nodes": [{"id": "n1", "control_path": "Root.MainPanel"}],
            },
            "frame": fr_cdp,
        }
        sealed_evidence, evidence_seal = _seal_evidence((w_godot, w_cdp))
        bridge_relabeled = bridge_vision(
            sealed_evidence,
            episode_ref=replace(ctx, seal=evidence_seal),
            observations=(obs_relabeled_godot,),
        )
        self.assertEqual(bridge_relabeled["status"], "REFUSED")
        self.assertFalse(bridge_relabeled["passed"])
        self.assertTrue(bridge_relabeled["refused_before_aggregation"])
        self.assertEqual(bridge_relabeled["error_code"], "E_SOURCE_KIND_BOUNDARY")
        with self.assertRaises(SourceKindBoundaryError):
            validate_source_neutral_observation(obs_relabeled_godot)

    def test_write_spies_reject_pixel_persistence_and_fake_clock_diagnostics(self) -> None:
        """Write spies reject pixel persistence without executing a real write; FakeClock never claims host speed."""
        mws = importlib.import_module("eval.v2eval.multiwin_scorecard")
        spy = WriteSpy()
        dummy_frame = np.zeros((8, 8, 3), dtype=np.uint8)

        # Verify direct spy methods reject disk, swap, log, receipt, and screenshot persistence
        with self.assertRaises(PixelPersistenceError):
            spy.save_screenshot("/tmp/forbidden_screenshot.png", dummy_frame)
        with self.assertRaises(PixelPersistenceError):
            spy.write_frame_to_disk("/tmp/forbidden_frame.raw", dummy_frame)
        with self.assertRaises(PixelPersistenceError):
            spy.write_frame_to_swap("/dev/sda2", dummy_frame)
        with self.assertRaises(PixelPersistenceError):
            spy.write_frame_to_log("eval.log", dummy_frame)
        with self.assertRaises(PixelPersistenceError):
            spy.write_frame_to_receipt("receipt.json", dummy_frame)
        self.assertEqual(len(spy.blocked_attempts), 5)

        # Verify context-managed WriteSpy intercepts open(..., "wb") and np.save without touching disk
        with self.assertRaises(PixelPersistenceError):
            with spy:
                open("/tmp/never_created_pixel_dump.bin", "wb")
        with self.assertRaises(PixelPersistenceError):
            with spy:
                np.save("/tmp/never_created_array.npy", dummy_frame)

        # Verify procfs and cgroup text fixtures in RAM
        ram_ok = validate_procfs_cgroup_ram_fixtures(
            proc_status_text="Name:\tpython3\nVmRSS:\t32768 kB\nVmSwap:\t0 kB\n",
            cgroup_swap_text="0\n",
        )
        self.assertTrue(ram_ok["ram_only_verified"])
        self.assertEqual(ram_ok["vm_swap_kb"], 0)
        self.assertEqual(ram_ok["cgroup_swap_bytes"], 0)

        with self.assertRaises(PixelPersistenceError):
            validate_procfs_cgroup_ram_fixtures(
                proc_status_text="Name:\tpython3\nVmSwap:\t128 kB\n",
                cgroup_swap_text="0\n",
            )

        # Verify FakeClock and VM-labeled optional diagnostics
        clock = mws.FakeClock(now_ns=1_000_000)
        clock.advance(500_000)
        w = _make_window_evidence()
        sealed_evidence, evidence_seal = _seal_evidence((w,))
        res_diag = bridge_vision(
            sealed_evidence,
            episode_ref={"episode_id": "ep-diag", "seal": evidence_seal, "generation": 1},
            clock=clock,
            diagnostics={"latency_ns": 250_000, "duration_ns": 500_000},
            proc_status_text="VmSwap:\t0 kB\n",
            cgroup_swap_text="0\n",
            write_spy=spy,
        )
        self.assertTrue(res_diag["passed"])
        self.assertEqual(res_diag["diagnostics"]["measurement_tag"], VM_MEASUREMENT_TAG)
        self.assertEqual(res_diag["diagnostics"]["latency_ns"], 250_000)
        self.assertEqual(res_diag["diagnostics"]["throughput"]["fraction"], "1/500000")
        self.assertEqual(res_diag["diagnostics"]["fake_clock_now_ns"], 1_500_000)
        self.assertIsNone(res_diag["diagnostics"]["performance_evidence_from_fake_clock"])
        self.assertEqual(res_diag["diagnostics"]["measured_host_speed"], AUSENTE)
        self.assertEqual(res_diag["diagnostics"]["host_capacity"], AUSENTE)

        # Negative contrast: deriving performance evidence from FakeClock or claiming host speed refuses
        sealed_evidence, evidence_seal = _seal_evidence((w,))
        res_fake_perf = bridge_vision(
            sealed_evidence,
            episode_ref={"episode_id": "ep-diag", "seal": evidence_seal, "generation": 1},
            clock=clock,
            diagnostics={"derived_from_fake_clock": True, "latency_ns": 500_000},
        )
        self.assertEqual(res_fake_perf["status"], "REFUSED")
        self.assertFalse(res_fake_perf["passed"])
        self.assertEqual(res_fake_perf["error_code"], "E_FAKE_CLOCK_PERFORMANCE_CLAIM")

        sealed_evidence, evidence_seal = _seal_evidence((w,))
        res_host_claim = bridge_vision(
            sealed_evidence,
            episode_ref={"episode_id": "ep-diag", "seal": evidence_seal, "generation": 1},
            diagnostics={"claim_host_speed": True},
        )
        self.assertEqual(res_host_claim["status"], "REFUSED")
        self.assertFalse(res_host_claim["passed"])
        self.assertEqual(res_host_claim["error_code"], "E_HOST_SPEED_CLAIM_FORBIDDEN")


if __name__ == "__main__":
    unittest.main()
