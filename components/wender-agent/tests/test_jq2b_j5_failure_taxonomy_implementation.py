"""Offline J5 taxonomy controls and adversarial episode battery mutations."""
import builtins
import copy
import unittest
from dataclasses import replace
from unittest.mock import patch

from wender.learn.failure_taxonomy import (
    T2_REASONS, HARNESS_CODES, EpisodeEvidence, EvidenceError,
    audit_episodes, classify_failure,
)


def holdings(status="accepted", reason=None):
    return {"seq": 1, "requested_tick": 2, "applied_tick": 2,
            "order_seq_id": 3, "status": status, "reason": reason,
            "state_sha256": "a" * 64}


def cdp(status="accepted", reason=None):
    return {"request_id": "browser-action-1", "trace_id": "tr-1",
            "status": status, "reason": reason}


def fixture():
    manifest = {
        "episodes": {
            "held-1": {"source_policy": "godot_t2", "cohort": "eligible",
                       "expected_status": "accepted", "expected_reason": None,
                       "postconditions": {"balance_updated": True}},
            "web-1": {"source_policy": "cdp", "cohort": "eligible",
                      "expected_status": "accepted", "expected_reason": None,
                      "postconditions": {"page_updated": True}},
            "held-refuse": {"source_policy": "godot_t2", "cohort": "eligible",
                            "expected_status": "rejected", "expected_reason": "INSUFFICIENT_FUNDS",
                            "postconditions": {"funds_preserved": True}},
            "excluded-1": {"source_policy": "cdp", "cohort": "excluded"},
            "unsupported-1": {"source_policy": "future_driver", "cohort": "unsupported"},
        },
        "denominators": {"completion": 3, "exactness": 3, "trail": 3, "outcome": 3},
        "thresholds": {k: {"numerator": 2 if k == "completion" else 3, "denominator": 3}
                       for k in ("completion", "exactness", "trail", "outcome")},
    }
    rows = [
        EpisodeEvidence("held-1", "godot_t2", holdings(), {"balance_updated": True},
                        True, True, True, False, 3),
        EpisodeEvidence("web-1", "cdp", cdp(), {"page_updated": True},
                        True, True, True, False, 8),
        EpisodeEvidence("held-refuse", "godot_t2", holdings("rejected", "INSUFFICIENT_FUNDS"),
                        {"funds_preserved": True}, True, True, True, False, 10),
        EpisodeEvidence("excluded-1", "cdp", None, {}, None, None, False, False),
        EpisodeEvidence("unsupported-1", "future_driver", None, {}, None, None, False, False),
    ]
    return rows, manifest


class TestFailureTaxonomy(unittest.TestCase):
    def test_exhaustive_contract_rejection_exact_spelling(self):
        self.assertEqual(len(T2_REASONS), 22)
        for reason in T2_REASONS:
            with self.subTest(reason=reason):
                f = classify_failure("engine", reason, status="rejected")
                self.assertEqual((f.code, f.plane, f.mapped, f.digest),
                                 ("T2/" + reason, "engine", True, None))
        self.assertEqual(len(HARNESS_CODES), 29)
        for code in HARNESS_CODES:
            f = classify_failure("harness", code)
            self.assertEqual(f.code, code)
            self.assertNotEqual(f.code, "T2/" + code)

    def test_contrasts_unknown_and_accepted_reason(self):
        for f in (classify_failure("engine", "schema", status="rejected"),
                  classify_failure("engine", "CAPTURE/DIGEST_MISMATCH"),
                  classify_failure("harness", "SCHEMA"),
                  classify_failure("engine", "SCHEMA", status="accepted"),
                  classify_failure("network", "credentials-and-bad-reason")):
            self.assertEqual(f.code, "EVIDENCE/UNMAPPED")
            self.assertFalse(f.mapped)
            self.assertRegex(f.digest, r"\A[0-9a-f]{16}\Z")
            self.assertNotIn("schema", repr(f).lower())
        self.assertIsNone(classify_failure("engine", None, status="accepted"))
        self.assertEqual(classify_failure("engine", None, status="rejected").code,
                         "EVIDENCE/AUSENTE")
        self.assertEqual(classify_failure("engine", "SCHEMA", status="proposed").code,
                         "EVIDENCE/UNMAPPED")

    def test_healthy_fixture_contract_and_descriptive_no_probability(self):
        rows, cfg = fixture()
        verdict = audit_episodes(rows, cfg)
        self.assertTrue(verdict.passed)
        self.assertEqual((verdict.eligible, verdict.completed, verdict.refused,
                          verdict.incomplete, verdict.excluded), (3, 2, 1, 0, 1))
        self.assertEqual(verdict.metrics["cohorts"]["unsupported"], 1)
        self.assertEqual(verdict.failures, {"T2/INSUFFICIENT_FUNDS": 1})
        self.assertEqual(verdict.metrics["completion"]["numerator"], 2)
        self.assertEqual(verdict.metrics["completion"]["denominator"], 3)
        self.assertEqual(verdict.metrics["outcome"]["numerator"], 3)
        self.assertEqual(verdict.metrics["attempts"], {"emitted": 3, "local_refused": 0})
        self.assertEqual(verdict.reward_status, "DESIGNING")
        self.assertIsNone(verdict.reward)
        self.assertEqual(verdict.confidence, {"status": "AUSENTE", "unit": "complete-episode",
                                              "method": None, "lower": None, "upper": None})

    def test_correlated_frames_do_not_change_trials(self):
        rows, cfg = fixture()
        v = audit_episodes(rows, cfg)
        replicated = [replace(r, frame_rows=r.frame_rows * 1000) for r in rows]
        w = audit_episodes(replicated, cfg)
        self.assertEqual((v.eligible, v.completed, v.refused, v.metrics["completion"]),
                         (w.eligible, w.completed, w.refused, w.metrics["completion"]))
        self.assertEqual(w.confidence["status"], "AUSENTE")
        self.assertNotEqual(v.metrics["evidence"]["correlated_frame_rows"],
                            w.metrics["evidence"]["correlated_frame_rows"])

    def test_unknown_rejection_mutant_fails_and_never_becomes_engine_reason(self):
        rows, cfg = fixture()
        rows[2] = replace(rows[2], outcome=holdings("rejected", "UNKNOWN_FUTURE"))
        v = audit_episodes(rows, cfg)
        self.assertFalse(v.passed)
        self.assertEqual(v.incomplete, 1)
        self.assertIn("EVIDENCE/UNMAPPED", v.failures)
        self.assertNotIn("T2/UNKNOWN_FUTURE", v.failures)

    def test_accepted_with_reason_mutant_fails(self):
        rows, cfg = fixture()
        rows[0] = replace(rows[0], outcome=holdings("accepted", "SCHEMA"))
        v = audit_episodes(rows, cfg)
        self.assertFalse(v.passed)
        self.assertIn("EVIDENCE/UNMAPPED", v.failures)

    def test_false_postcondition_cannot_complete(self):
        rows, cfg = fixture()
        rows[0] = replace(rows[0], postconditions={"balance_updated": False})
        v = audit_episodes(rows, cfg)
        self.assertFalse(v.passed)
        self.assertEqual(v.completed, 1)
        self.assertEqual(v.incomplete, 1)

    def test_trail_and_exactness_missing_never_perfect(self):
        rows, cfg = fixture()
        for attribute, key in (("exactness", "exactness"), ("trail", "trail")):
            broken = list(rows)
            broken[0] = replace(broken[0], **{attribute: None})
            v = audit_episodes(broken, cfg)
            self.assertEqual(v.metrics[key]["status"], "UNKNOWN")
            self.assertIsNone(v.metrics[key]["denominator"])
            self.assertFalse(v.passed)

    def test_declared_zero_or_missing_denominator_is_unknown(self):
        for n in (0, None):
            rows, cfg = fixture()
            cfg["denominators"]["outcome"] = n
            v = audit_episodes(rows, cfg)
            self.assertEqual(v.metrics["outcome"]["status"], "UNKNOWN")
            self.assertIsNone(v.metrics["outcome"]["numerator"])
            self.assertFalse(v.passed)

    def test_empty_battery_is_ausente_not_success(self):
        _, cfg = fixture()
        cfg["episodes"] = {}
        with self.assertRaisesRegex(EvidenceError, "AUSENTE"):
            audit_episodes([], cfg)

    def test_duplicate_foreign_and_missing_episodes_fail(self):
        rows, cfg = fixture()
        mutants = (rows[:-1], rows + [rows[0]],
                   [replace(rows[0], episode_id="foreign-id")] + rows[1:])
        for mutant in mutants:
            with self.subTest(ids=[r.episode_id for r in mutant]):
                v = audit_episodes(mutant, cfg)
                self.assertFalse(v.passed)
                self.assertIn("EVIDENCE/AUSENTE", v.failures)

    def test_local_refusal_not_an_emitted_completed_action(self):
        rows, cfg = fixture()
        rows[0] = replace(rows[0], outcome=None, emitted=False, local_refusal=True)
        v = audit_episodes(rows, cfg)
        self.assertFalse(v.passed)
        self.assertEqual(v.metrics["attempts"], {"emitted": 2, "local_refused": 1})
        self.assertEqual(v.completed, 1)
        self.assertEqual(v.incomplete, 1)

    def test_t2_outcome_fields_and_injected_source_validator(self):
        rows, cfg = fixture()
        gone = holdings()
        gone.pop("order_seq_id")
        rows[0] = replace(rows[0], outcome=gone)
        self.assertFalse(audit_episodes(rows, cfg).passed)
        rows, cfg = fixture()
        v = audit_episodes(rows, cfg, validators={"godot_t2": lambda record: False})
        self.assertFalse(v.passed)
        self.assertEqual(v.metrics["outcome"]["status"], "UNKNOWN")

    def test_source_policy_swap_is_not_relabelled(self):
        rows, cfg = fixture()
        rows[0] = replace(rows[0], source_policy="cdp")
        v = audit_episodes(rows, cfg)
        self.assertFalse(v.passed)
        self.assertEqual(v.incomplete, 1)

    def test_cdp_refusal_is_harness_not_t2_engine(self):
        rows, cfg = fixture()
        cfg["episodes"]["web-1"]["expected_status"] = "rejected"
        cfg["episodes"]["web-1"]["expected_reason"] = "SELECTOR/MISS"
        cfg["thresholds"]["completion"]["numerator"] = 1
        rows[1] = replace(rows[1], outcome=cdp("rejected", "SELECTOR/MISS"))
        v = audit_episodes(rows, cfg)
        self.assertTrue(v.passed)
        self.assertIn("SELECTOR/MISS", v.failures)
        self.assertNotIn("T2/SELECTOR/MISS", v.failures)
        self.assertEqual(v.refused, 2)
        cfg["episodes"]["web-1"]["expected_reason"] = "SCHEMA"
        with self.assertRaisesRegex(EvidenceError, "AUSENTE"):
            audit_episodes(rows, cfg)

    def test_fault_injected_is_failure_even_with_successful_outcome(self):
        rows, cfg = fixture()
        rows[1] = replace(rows[1], faults=(("harness", "CAPTURE/GEOMETRY_MISMATCH"),))
        v = audit_episodes(rows, cfg)
        self.assertFalse(v.passed)
        self.assertIn("CAPTURE/GEOMETRY_MISMATCH", v.failures)

    def test_scorecard_structural_compatibility_and_missing_support(self):
        from types import SimpleNamespace
        rows, cfg = fixture()
        support = {"frame_exactness": {"denominator": 1},
                   "terminal_completeness": {"denominator": 1}}
        self.assertTrue(audit_episodes(rows, cfg, scorecard=SimpleNamespace(
            passed=True, metrics=support, windows=tuple())).passed)
        self.assertFalse(audit_episodes(rows, cfg, scorecard=SimpleNamespace(
            passed=True, metrics={"frame_exactness": {"denominator": 0}},
            windows=tuple())).passed)

    def test_no_image_receipt_or_file_writes_during_audit(self):
        rows, cfg = fixture()
        # Author-generated RAM frame; pixel arrays are not accepted by the core.
        try:
            import numpy as np
            frame = np.zeros((2, 2, 3), dtype=np.uint8)
            self.assertEqual(frame.shape, (2, 2, 3))
        except ImportError:
            frame = None
        with patch.object(builtins, "open", side_effect=AssertionError("pixel/receipt leak")):
            self.assertTrue(audit_episodes(rows, cfg).passed)
        self.assertIsNone(audit_episodes(rows, cfg).reward)


if __name__ == "__main__":
    unittest.main()
