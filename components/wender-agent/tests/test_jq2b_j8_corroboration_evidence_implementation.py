# @module {"id":"tests.test_jq2b_j8_corroboration_evidence_implementation","layer":3,"owner":"TESTS","deps":["wa.perceive.corroboration_evidence","wa.perceive.parallel_perceive","wa.perceive.vision_adapter"]}
"""Substantive public-path unit tests for corroboration_evidence (jq2b-j8-corroboration-evidence).

Exercises the real public API of corroboration_evidence following INV J8 1 through INV J8 4.
Offline only: numpy frames in RAM, inline Holdings fixture records; no X server, Godot binary,
network, disk writes, or host timing measurements.
"""
from __future__ import annotations

import hashlib
import json
import unittest

import numpy as np

from wender.perceive import corroboration_evidence as ce
from wender.perceive.parallel_perceive import FrameRef, Handle


def make_frame_ref(
    handle: Handle,
    epoch: int = 1,
    frame_seq: int = 0,
    width: int = 32,
    height: int = 32,
    pixel_val: int = 128,
) -> FrameRef:
    """Create a validated FrameRef in RAM."""
    pixels = np.full((height, width, 3), pixel_val, dtype=np.uint8)
    digest = hashlib.sha256(pixels.tobytes(order="C")).hexdigest()
    return FrameRef(
        handle=handle,
        epoch=epoch,
        frame_seq=frame_seq,
        width=width,
        height=height,
        rgb=pixels,
        digest=digest,
    )


def make_tree_fixture(
    dpr_ppm: int = 1000000,
    nodes: list[dict] | None = None,
    query: str | None = "button#save",
    provenance: dict | None = None,
) -> dict:
    """Create an AX UI-tree fixture."""
    prov = provenance or {
        "source": "truthful_ui_tree_v1",
        "anchor_line": 509,
        "spec": "truthful UI-tree provenance",
    }
    return {
        "nodes": nodes or [
            {"id": "node_1", "role": "button", "box": [0, 0, 32, 32], "actionable": True},
            {"id": "node_2", "role": "text", "box": [0, 32, 32, 16], "actionable": False},
        ],
        "viewport": {"w": 800, "h": 600, "dpr_ppm": dpr_ppm},
        "query": query,
        "provenance": prov,
    }


def make_binding_fixture(
    handle: Handle,
    epoch: int = 1,
    frame_seq: int = 0,
    dpr_ppm: int = 1000000,
    source: ce.SourceDescriptor | dict | None = None,
    commit_ref: str = "order_seq_1:state_sha256_mock",
    screen_identity: str = "screen_id_alpha_16hex",
    query: str | None = "button#save",
    provenance: dict | None = None,
    tick: int = 10,
) -> dict:
    """Create a Holdings evidence binding fixture."""
    prov = provenance or {
        "source": "truthful_ui_tree_v1",
        "anchor_line": 509,
        "spec": "truthful UI-tree provenance",
    }
    src = source or ce.SourceDescriptor(
        kind="godot",
        session_id="godot_session_1",
        target_id=handle.canonical_id,
        has_actual_cdp_harvest=False,
    )
    return {
        "handle": handle,
        "epoch": epoch,
        "frame_seq": frame_seq,
        "dpr_ppm": dpr_ppm,
        "source": src,
        "screen_identity": screen_identity,
        "query": query,
        "commit_ref": commit_ref,
        "provenance": prov,
        "telemetry": {"tick": tick, "status": "accepted"},
    }


class TestCorroborationEvidenceImplementation(unittest.TestCase):
    """Substantive public API test suite for corroboration_evidence."""

    def setUp(self):
        self.handle_a = Handle(kind="godot_window", canonical_id="win_main", generation=1)
        self.handle_sibling = Handle(kind="godot_window", canonical_id="win_other", generation=1)
        self.handle_cdp = Handle(kind="cdp_tab", canonical_id="tab_app", generation=1)

    def test_normalize_evidence_positive_control(self):
        """Positive control: valid tree, FrameRef, and binding produce frozen NormalizedEvidence."""
        frame = make_frame_ref(self.handle_a, epoch=1, frame_seq=0)
        tree = make_tree_fixture()
        binding = make_binding_fixture(self.handle_a, epoch=1, frame_seq=0)

        evidence = ce.normalize_evidence(tree, frame, binding)
        self.assertIsInstance(evidence, ce.NormalizedEvidence)
        self.assertEqual(evidence.handle, self.handle_a)
        self.assertEqual(evidence.epoch, 1)
        self.assertEqual(evidence.frame_seq, 0)
        self.assertEqual(evidence.dpr_ppm, 1000000)
        self.assertEqual(evidence.structure_summary["nodes_count"], 2)

        # Raw pixels are not leaked into references
        self.assertNotIn("pixels", evidence.frame_ref)
        self.assertNotIn("rgb", evidence.tree_ref)
        self.assertTrue(evidence.frame_ref.startswith("frame:win_main:1:0:"))

    def test_inv_j8_1_missing_prerequisites_fail_as_ausente(self):
        """INV J8 1: Missing prerequisites or absent provenance fail loudly as AUSENTE."""
        frame = make_frame_ref(self.handle_a)
        tree = make_tree_fixture()
        binding = make_binding_fixture(self.handle_a)

        # Missing tree
        with self.assertRaises(ce.EvidenceError) as cm:
            ce.normalize_evidence(None, frame, binding)
        self.assertIn("AUSENTE", str(cm.exception))

        # Missing frame_ref
        with self.assertRaises(ce.EvidenceError) as cm:
            ce.normalize_evidence(tree, None, binding)
        self.assertIn("AUSENTE", str(cm.exception))

        # Absent provenance
        bad_binding = dict(binding)
        del bad_binding["provenance"]
        bad_tree = dict(tree)
        del bad_tree["provenance"]
        with self.assertRaises(ce.EvidenceError) as cm:
            ce.normalize_evidence(bad_tree, frame, bad_binding)
        self.assertIn("AUSENTE", str(cm.exception))
        self.assertIn("absent provenance", str(cm.exception))

    def test_inv_j8_1_cdp_harvest_policy_justification(self):
        """INV J8 1: Actual CDP harvest alone justifies source.kind cdp."""
        frame = make_frame_ref(self.handle_cdp)
        tree = make_tree_fixture()

        # CDP source with actual CDP harvest -> succeeds
        cdp_source_valid = ce.SourceDescriptor(
            kind="cdp",
            session_id="cdp_sess_1",
            target_id="tab_app",
            has_actual_cdp_harvest=True,
        )
        binding_valid = make_binding_fixture(self.handle_cdp, source=cdp_source_valid)
        ev = ce.normalize_evidence(tree, frame, binding_valid)
        self.assertEqual(ev.source.kind, "cdp")
        self.assertTrue(ev.source.has_actual_cdp_harvest)

        # CDP source without actual harvest -> rejected
        with self.assertRaises(ce.EvidenceError) as cm:
            ce.SourceDescriptor(
                kind="cdp",
                session_id="cdp_sess_1",
                target_id="tab_app",
                has_actual_cdp_harvest=False,
            )
        self.assertIn("Actual CDP harvest alone justifies source.kind cdp", str(cm.exception))

    def test_inv_j8_2_sibling_frame_rejected(self):
        """INV J8 2: Sibling frame from a different handle is rejected."""
        frame_sibling = make_frame_ref(self.handle_sibling, epoch=1, frame_seq=0)
        tree = make_tree_fixture()
        binding = make_binding_fixture(self.handle_a, epoch=1, frame_seq=0)

        with self.assertRaises(ce.EvidenceError) as cm:
            ce.normalize_evidence(tree, frame_sibling, binding)
        self.assertIn("SIBLING_FRAME", str(cm.exception))

    def test_inv_j8_2_same_epoch_resize_rejected(self):
        """INV J8 2: Same-epoch resize is rejected."""
        frame_resized = make_frame_ref(self.handle_a, epoch=1, frame_seq=1, width=64, height=32)
        tree = make_tree_fixture()
        binding = make_binding_fixture(self.handle_a, epoch=1, frame_seq=1)

        # Previous geometry in epoch 1 was (32, 32)
        with self.assertRaises(ce.EvidenceError) as cm:
            ce.normalize_evidence(
                tree,
                frame_resized,
                binding,
                prev_geometry=(32, 32),
            )
        self.assertIn("SAME_EPOCH_RESIZE", str(cm.exception))

    def test_inv_j8_2_wrong_dpr_and_mismatched_bindings_rejected(self):
        """INV J8 2: Wrong DPR and mismatched screen/query/commit are rejected."""
        frame = make_frame_ref(self.handle_a)
        tree_wrong_dpr = make_tree_fixture(dpr_ppm=2000000)
        binding = make_binding_fixture(self.handle_a, dpr_ppm=1000000)

        # DPR mismatch
        with self.assertRaises(ce.EvidenceError) as cm:
            ce.normalize_evidence(tree_wrong_dpr, frame, binding)
        self.assertIn("WRONG_DPR", str(cm.exception))

        # Query mismatch
        tree_diff_query = make_tree_fixture(query="button#cancel")
        with self.assertRaises(ce.EvidenceError) as cm:
            ce.normalize_evidence(tree_diff_query, frame, binding)
        self.assertIn("MISMATCHED_QUERY", str(cm.exception))

    def test_inv_j8_3_prohibition_of_last_good_fallback(self):
        """INV J8 3: Unknown geometry, stale or skipped evidence cannot use last-good fallback."""
        frame = make_frame_ref(self.handle_a)
        tree = make_tree_fixture()
        binding = make_binding_fixture(self.handle_a)
        ev = ce.normalize_evidence(tree, frame, binding)

        # Direct attempt to use last-good fallback returns MISMATCH
        result = ce.corroborate_evidence(ev, last_good_fallback=True)
        self.assertEqual(result.verdict, ce.Verdict.MISMATCH.value)
        self.assertEqual(result.failure_code, ce.FailureCode.LAST_GOOD_FALLBACK_FORBIDDEN.value)
        self.assertFalse(result.structure_checked)
        self.assertFalse(result.telemetry_checked)
        self.assertFalse(result.residency_checked)

    def test_inv_j8_3_independent_structure_telemetry_residency_checks(self):
        """INV J8 3: Evaluates independent structure, calibrated telemetry, and residency."""
        frame = make_frame_ref(self.handle_a)
        tree = make_tree_fixture()
        binding = make_binding_fixture(self.handle_a, tick=10)
        ev = ce.normalize_evidence(tree, frame, binding)

        # Corroborated when all 3 pillars pass
        result = ce.corroborate_evidence(
            ev,
            resident_handles=[self.handle_a],
            telemetry_sample={"tick": 10},
        )
        self.assertEqual(result.verdict, ce.Verdict.CORROBORATED.value)
        self.assertIsNone(result.failure_code)
        self.assertTrue(result.structure_checked)
        self.assertTrue(result.telemetry_checked)
        self.assertTrue(result.residency_checked)

        # Incomplete observer coverage results in ABSTAIN
        result_incomplete = ce.corroborate_evidence(
            ev,
            observer_coverage_pct=50,  # below default 90%
            resident_handles=[self.handle_a],
        )
        self.assertEqual(result_incomplete.verdict, ce.Verdict.ABSTAIN.value)
        self.assertEqual(result_incomplete.failure_code, ce.FailureCode.INCOMPLETE_OBSERVER_COVERAGE.value)
        self.assertFalse(result_incomplete.structure_checked)

        # Telemetry out of tolerance results in MISMATCH
        result_uncalibrated = ce.corroborate_evidence(
            ev,
            resident_handles=[self.handle_a],
            telemetry_sample={"tick": 20},  # delta 10 > tolerance 1
        )
        self.assertEqual(result_uncalibrated.verdict, ce.Verdict.MISMATCH.value)
        self.assertEqual(result_uncalibrated.failure_code, ce.FailureCode.UNCALIBRATED_TELEMETRY.value)
        self.assertFalse(result_uncalibrated.telemetry_checked)

    def test_inv_j8_4_result_format_clean_fields(self):
        """INV J8 4: Result has 8 clean fields and contains no pixels, crops, or model rationale."""
        frame = make_frame_ref(self.handle_a)
        tree = make_tree_fixture()
        binding = make_binding_fixture(self.handle_a)
        ev = ce.normalize_evidence(tree, frame, binding)

        result = ce.corroborate_evidence(ev)
        fields = (
            "verdict",
            "failure_code",
            "tree_ref",
            "frame_ref",
            "commit_ref",
            "structure_checked",
            "telemetry_checked",
            "residency_checked",
        )
        for f in fields:
            self.assertTrue(hasattr(result, f), f"missing field: {f}")

        # Ensure no pixel arrays or crops exist on the result
        self.assertFalse(hasattr(result, "pixels"))
        self.assertFalse(hasattr(result, "crops"))
        self.assertFalse(hasattr(result, "rationale"))

        # Describe result respects options and disables artifact output
        described = ce.describe_result(result)
        data = json.loads(described)
        self.assertEqual(data["verdict"], "CORROBORATED")
        self.assertNotIn("crops", data)
        self.assertNotIn("pixels", data)

    def test_both_source_policies_exercised(self):
        """Exercise both source policies (CDP and Godot) without relabeling."""
        # Policy 1: CDP Policy
        cdp_source = ce.SourceDescriptor(
            kind="cdp",
            session_id="cdp_sess_main",
            target_id="tab_app",
            has_actual_cdp_harvest=True,
        )
        frame_cdp = make_frame_ref(self.handle_cdp)
        binding_cdp = make_binding_fixture(self.handle_cdp, source=cdp_source)
        ev_cdp = ce.normalize_evidence(make_tree_fixture(), frame_cdp, binding_cdp)
        res_cdp = ce.corroborate_evidence(ev_cdp, resident_handles=[self.handle_cdp])
        self.assertEqual(ev_cdp.source.kind, "cdp")
        self.assertEqual(res_cdp.verdict, ce.Verdict.CORROBORATED.value)

        # Policy 2: Godot Policy
        godot_source = ce.SourceDescriptor(
            kind="godot",
            session_id="godot_sess_main",
            target_id="win_main",
            has_actual_cdp_harvest=False,
        )
        frame_godot = make_frame_ref(self.handle_a)
        binding_godot = make_binding_fixture(self.handle_a, source=godot_source)
        ev_godot = ce.normalize_evidence(make_tree_fixture(), frame_godot, binding_godot)
        res_godot = ce.corroborate_evidence(ev_godot, resident_handles=[self.handle_a])
        self.assertEqual(ev_godot.source.kind, "godot")
        self.assertEqual(res_godot.verdict, ce.Verdict.CORROBORATED.value)

        self.assertNotEqual(ev_cdp.source.kind, ev_godot.source.kind)

    def test_declared_tolerances_reject_zero_defaults(self):
        """Declared raw-unit tolerances must be positive numbers; zero defaults rejected."""
        tols = ce.DeclaredTolerances()
        self.assertGreater(tols.px_pos_tolerance, 0)
        self.assertGreater(tols.px_size_tolerance, 0)
        self.assertGreater(tols.telemetry_tick_tolerance, 0)
        self.assertGreater(tols.dpr_tolerance_ppm, 0)

        # Zero tolerance rejected
        with self.assertRaises(ce.EvidenceError):
            ce.DeclaredTolerances(px_pos_tolerance=0)
        with self.assertRaises(ce.EvidenceError):
            ce.DeclaredTolerances(telemetry_tick_tolerance=0)

    def test_temporal_lane_isolation_and_exception_reset(self):
        """Exceptions reset ONLY the affected temporal lane; other lanes remain intact."""
        corroborator = ce.Corroborator()

        # Step 1: establish initial geometry for lane A and lane B
        frame_a0 = make_frame_ref(self.handle_a, epoch=1, frame_seq=0, width=32, height=32)
        binding_a0 = make_binding_fixture(self.handle_a, epoch=1, frame_seq=0)
        corroborator.corroborate(make_tree_fixture(), frame_a0, binding_a0)

        frame_b0 = make_frame_ref(self.handle_sibling, epoch=1, frame_seq=0, width=48, height=48)
        binding_b0 = make_binding_fixture(self.handle_sibling, epoch=1, frame_seq=0)
        corroborator.corroborate(make_tree_fixture(), frame_b0, binding_b0)

        # Step 2: cause an exception in lane A (same-epoch resize: 32 -> 64)
        frame_a1_bad = make_frame_ref(self.handle_a, epoch=1, frame_seq=1, width=64, height=32)
        binding_a1 = make_binding_fixture(self.handle_a, epoch=1, frame_seq=1)
        with self.assertRaises(ce.EvidenceError):
            corroborator.corroborate(make_tree_fixture(), frame_a1_bad, binding_a1)

        # Lane A was reset: its geometry cache is now cleared
        key_a = (*ce._target_tuple(self.handle_a), 1)
        self.assertNotIn(key_a, corroborator._lane_geometry)

        # Lane B remains completely intact and undisturbed
        key_b = (*ce._target_tuple(self.handle_sibling), 1)
        self.assertIn(key_b, corroborator._lane_geometry)
        self.assertEqual(corroborator._lane_geometry[key_b], (48, 48))

    def test_write_spy_rejects_leakage(self):
        """WriteSpy rejects pixel frame or receipt leakage."""
        spy = ce.WriteSpy()
        spy.check_data("clean_hash_metadata", "metadata")

        # Raw PNG header
        with self.assertRaises(ce.EvidenceError) as cm:
            spy.check_data(b"\x89PNG\r\n\x1a\nfake_pixel_payload", "leaked.png")
        self.assertIn("raw pixel leakage", str(cm.exception))

        # Base64 PNG header
        with self.assertRaises(ce.EvidenceError) as cm:
            spy.check_data("data:image/png;base64,iVBORw0KGgoAAA", "leaked_base64")
        self.assertIn("base64 pixel leakage", str(cm.exception))

    def test_gate_g_j8_2_evidence_ownership_epochs(self):
        """Acceptance Gate G-J8-2: requires Evidence ownership/epochs, negative contrast & positive control."""
        corroborator = ce.Corroborator()

        # Positive control: same-window frozen evidence
        frame_pos = make_frame_ref(self.handle_a, epoch=1, frame_seq=0, width=32, height=32)
        binding_pos = make_binding_fixture(self.handle_a, epoch=1, frame_seq=0)
        tree_pos = make_tree_fixture()
        pos_inputs = (tree_pos, frame_pos, binding_pos)

        # Negative contrast 1: Sibling frame across targets
        frame_sib = make_frame_ref(self.handle_sibling, epoch=1, frame_seq=0, width=32, height=32)
        sib_inputs = (tree_pos, frame_sib, binding_pos)

        # Negative contrast 2: Same-epoch resize
        frame_res = make_frame_ref(self.handle_a, epoch=1, frame_seq=1, width=64, height=32)
        binding_res = make_binding_fixture(self.handle_a, epoch=1, frame_seq=1)
        res_inputs = (tree_pos, frame_res, binding_res)

        gate_result = ce.evaluate_gate_g_j8_2(
            corroborator,
            positive_control_inputs=pos_inputs,
            sibling_contrast_inputs=sib_inputs,
            resize_contrast_inputs=res_inputs,
        )

        self.assertEqual(gate_result["status"], "PASS")
        self.assertTrue(gate_result["passed"])
        self.assertTrue(gate_result["positive_control_passed"])
        self.assertTrue(gate_result["sibling_contrast_rejected"])
        self.assertTrue(gate_result["resize_contrast_rejected"])


if __name__ == "__main__":
    unittest.main()
