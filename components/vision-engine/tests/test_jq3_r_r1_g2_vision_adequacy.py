"""Focused unit tests for eval/v2eval/ast_mutation_adequacy_gate.py.

Verifies module/category reductions, rejection of duplicate anchors, changed
digests, unknown exclusions, and failed positives, preservation of measured
denominators, deterministic input-order-independent reductions, and isolation
of foreign scheduler provenance fixtures from vision-engine local source kills.
"""
from __future__ import annotations

from pathlib import Path
import random
import threading
import unittest

import numpy as np

from eval.v2eval.ast_mutation_adequacy_gate import (
    AdequacyGateError,
    ChangedDigestError,
    DenominatorTamperError,
    DuplicateAnchorError,
    FailedPositiveControlError,
    ForeignReceiptError,
    ForeignSchedulerReceipt,
    UnknownExclusionError,
    evaluate_ast_mutation_adequacy,
    reduce_adequacy_receipts,
)
from eval.v2eval.jq3_mutation_receipts import (
    OUTCOME_EQUIVALENT,
    OUTCOME_INFRASTRUCTURE,
    OUTCOME_KILLED,
    OUTCOME_MISSING_ANCHOR,
    OUTCOME_SURVIVED,
    VisionMutationReceipt,
    source_digest_for_path,
    synthetic_rgb_digest,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
POSTPASS_PATH = "eval/v2eval/postpass.py"
DETECTOR_PATH = "eval/v2x/per_window_detector.py"
ADEQUACY_REPLAY_SEED = 0x5A8E12E5


def _make_rgb_fixture(seed_val: int) -> np.ndarray:
    arr = np.full((16, 16, 3), int(seed_val) & 0xFF, dtype=np.uint8)
    arr.flags.writeable = False
    return arr


class TestJQ3VisionAdequacyGate(unittest.TestCase):
    """Focused test suite for AST mutation adequacy gate reduction and invariants."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.postpass_digest = source_digest_for_path(REPO_ROOT / POSTPASS_PATH)
        cls.detector_digest = source_digest_for_path(REPO_ROOT / DETECTOR_PATH)
        cls.expected_sources = {
            POSTPASS_PATH: cls.postpass_digest,
            DETECTOR_PATH: cls.detector_digest,
        }
        cls.rgb_a = synthetic_rgb_digest((_make_rgb_fixture(17),))
        cls.rgb_b = synthetic_rgb_digest((_make_rgb_fixture(89),))
        cls.bound_rgb = {cls.rgb_a, cls.rgb_b}

    def _sample_local_receipts(self) -> list[VisionMutationReceipt]:
        return [
            VisionMutationReceipt(
                mutant_id="M-eval-v2eval-postpass-1",
                module_path=POSTPASS_PATH,
                category="operator_flip",
                source_digest=self.postpass_digest,
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
                positive_control_passed=True,
                synthetic_rgb_digest=self.rgb_a,
                outcome=OUTCOME_KILLED,
                lineno=87,
            ),
            VisionMutationReceipt(
                mutant_id="M-eval-v2eval-postpass-2",
                module_path=POSTPASS_PATH,
                category="boundary_increment",
                source_digest=self.postpass_digest,
                qualified_ast_scope="BaselineFlickerVeto.update",
                containing_expression="old is not None and (current['cls'] == 4).any()",
                original_expression="4",
                structural_selector="values[1]:Call/func:Attribute/value:Compare/comparators[0]:Constant",
                transformation="Constant.value: 4 -> 5",
                positive_control_passed=True,
                synthetic_rgb_digest=self.rgb_b,
                outcome=OUTCOME_KILLED,
                lineno=121,
            ),
            VisionMutationReceipt(
                mutant_id="M-eval-v2eval-postpass-equiv-1",
                module_path=POSTPASS_PATH,
                category="equivalent_cast",
                source_digest=self.postpass_digest,
                qualified_ast_scope="BaselineFlickerVeto.update",
                containing_expression="int(frame_id)",
                original_expression="int(frame_id)",
                structural_selector="self:Call",
                transformation="Call: int(frame_id) -> int(int(frame_id))",
                positive_control_passed=True,
                synthetic_rgb_digest=self.rgb_a,
                outcome=OUTCOME_EQUIVALENT,
                equivalent_justification="idempotent int conversion after integer guard",
                lineno=90,
            ),
            VisionMutationReceipt(
                mutant_id="M-eval-v2x-detector-1",
                module_path=DETECTOR_PATH,
                category="operator_flip",
                source_digest=self.detector_digest,
                qualified_ast_scope="PerWindowDetector._step",
                containing_expression="st.prev_y is not None and (not np.array_equal(st.prev_y, y))",
                original_expression="st.prev_y is not None",
                structural_selector="values[0]:Compare",
                transformation="Compare.ops[0]: IsNot -> Is",
                positive_control_passed=True,
                synthetic_rgb_digest=self.rgb_b,
                outcome=OUTCOME_KILLED,
                lineno=231,
            ),
            VisionMutationReceipt(
                mutant_id="M-eval-v2x-detector-survived-1",
                module_path=DETECTOR_PATH,
                category="boundary_increment",
                source_digest=self.detector_digest,
                qualified_ast_scope="PerWindowDetector._step",
                containing_expression="len(st.history) > 2",
                original_expression="2",
                structural_selector="comparators[0]:Constant",
                transformation="Constant.value: 2 -> 3",
                positive_control_passed=True,
                synthetic_rgb_digest=self.rgb_a,
                outcome=OUTCOME_SURVIVED,
                lineno=292,
            ),
        ]

    def test_module_and_category_reduction_preserves_measured_denominators(self) -> None:
        """Verify module, category, and (module, category) buckets preserve measured denominators."""
        receipts = self._sample_local_receipts()
        exclusions = {
            "M-eval-v2eval-postpass-equiv-1": "idempotent int conversion after integer guard"
        }
        report = reduce_adequacy_receipts(
            receipts,
            expected_source_digests=self.expected_sources,
            bound_rgb_digests=self.bound_rgb,
            known_exclusions=exclusions,
            expected_measured_denominator=4,
            expected_module_denominators={POSTPASS_PATH: 2, DETECTOR_PATH: 2},
            min_adequacy_ratio=0.75,
        )

        # Overall checks: 5 total local sites, 1 equivalent -> measured_denominator = 4, killed = 3
        self.assertEqual(report.total_local_sites, 5)
        self.assertEqual(report.equivalent, 1)
        self.assertEqual(report.measured_denominator, 4)
        self.assertEqual(report.killed_numerator, 3)
        self.assertEqual(report.killed, 3)
        self.assertEqual(report.survived, 1)
        self.assertEqual(report.missing_anchor, 0)
        self.assertEqual(report.infrastructure, 0)
        self.assertAlmostEqual(report.adequacy_ratio, 3 / 4)
        self.assertFalse(report.gate_passed)  # 1 survived site keeps gate_passed False

        # Module buckets
        post_bucket = report.by_module[POSTPASS_PATH]
        self.assertEqual(post_bucket.total_sites, 3)
        self.assertEqual(post_bucket.equivalent, 1)
        self.assertEqual(post_bucket.measured_denominator, 2)
        self.assertEqual(post_bucket.killed_numerator, 2)
        self.assertAlmostEqual(post_bucket.adequacy_ratio, 1.0)

        det_bucket = report.by_module[DETECTOR_PATH]
        self.assertEqual(det_bucket.total_sites, 2)
        self.assertEqual(det_bucket.equivalent, 0)
        self.assertEqual(det_bucket.measured_denominator, 2)
        self.assertEqual(det_bucket.killed_numerator, 1)
        self.assertEqual(det_bucket.survived, 1)
        self.assertAlmostEqual(det_bucket.adequacy_ratio, 0.5)

        # Category buckets
        op_bucket = report.by_category["operator_flip"]
        self.assertEqual(op_bucket.measured_denominator, 2)
        self.assertEqual(op_bucket.killed_numerator, 2)

        bound_bucket = report.by_category["boundary_increment"]
        self.assertEqual(bound_bucket.measured_denominator, 2)
        self.assertEqual(bound_bucket.killed_numerator, 1)
        self.assertEqual(bound_bucket.survived, 1)

        eq_bucket = report.by_category["equivalent_cast"]
        self.assertEqual(eq_bucket.total_sites, 1)
        self.assertEqual(eq_bucket.equivalent, 1)
        self.assertEqual(eq_bucket.measured_denominator, 0)
        self.assertEqual(eq_bucket.killed_numerator, 0)

    def test_reject_duplicate_anchors_changed_digests_unknown_exclusions_and_failed_positives(
        self,
    ) -> None:
        """Single-condition negative tests for every adequacy gate rejection rule."""
        base = self._sample_local_receipts()[:2]

        # 1. Duplicate anchor (different mutant_id, identical AST anchor) -> DuplicateAnchorError
        dup_anchor = VisionMutationReceipt(
            mutant_id="M-eval-v2eval-postpass-1-clone",
            module_path=POSTPASS_PATH,
            category="operator_flip",
            source_digest=self.postpass_digest,
            qualified_ast_scope="BaselineFlickerVeto.update",
            containing_expression=base[0].containing_expression,
            original_expression=base[0].original_expression,
            structural_selector=base[0].structural_selector,
            transformation="BoolOp.op: Or -> And (duplicate)",
            positive_control_passed=True,
            synthetic_rgb_digest=self.rgb_a,
            outcome=OUTCOME_KILLED,
        )
        with self.assertRaises(DuplicateAnchorError):
            reduce_adequacy_receipts(base + [dup_anchor], expected_source_digests=self.expected_sources)

        # 2. Changed source digest -> ChangedDigestError
        changed_src = VisionMutationReceipt(
            mutant_id="M-eval-v2eval-postpass-changed-digest",
            module_path=POSTPASS_PATH,
            category="operator_flip",
            source_digest="e" * 64,
            qualified_ast_scope="PostPass.update",
            containing_expression="self.prev is not None",
            original_expression="self.prev is not None",
            structural_selector="self:Compare",
            transformation="Compare.ops[0]: IsNot -> Is",
            positive_control_passed=True,
            synthetic_rgb_digest=self.rgb_a,
            outcome=OUTCOME_KILLED,
        )
        with self.assertRaises(ChangedDigestError):
            reduce_adequacy_receipts(
                base + [changed_src], expected_source_digests=self.expected_sources
            )
        # Also rejected even without expected_source_digests because postpass.py digest is inconsistent in batch
        with self.assertRaises(ChangedDigestError):
            reduce_adequacy_receipts(base + [changed_src])

        # 3. Unbound synthetic RGB digest -> ChangedDigestError
        with self.assertRaises(ChangedDigestError):
            reduce_adequacy_receipts(
                base,
                expected_source_digests=self.expected_sources,
                bound_rgb_digests={"c" * 64},
            )

        # 4. Unknown exclusions -> UnknownExclusionError
        all_five = self._sample_local_receipts()
        # Missing known_exclusions entry for equivalent receipt
        with self.assertRaises(UnknownExclusionError):
            reduce_adequacy_receipts(all_five, expected_source_digests=self.expected_sources)
        # Mismatched justification text
        with self.assertRaises(UnknownExclusionError):
            reduce_adequacy_receipts(
                all_five,
                expected_source_digests=self.expected_sources,
                known_exclusions={"M-eval-v2eval-postpass-equiv-1": "wrong reason"},
            )
        # Exclusion referencing unknown mutant ID
        with self.assertRaises(UnknownExclusionError):
            reduce_adequacy_receipts(
                base,
                expected_source_digests=self.expected_sources,
                known_exclusions={"M-nonexistent-id": "some reason"},
            )
        # Exclusion referencing a non-equivalent (killed) mutant ID
        with self.assertRaises(UnknownExclusionError):
            reduce_adequacy_receipts(
                base,
                expected_source_digests=self.expected_sources,
                known_exclusions={"M-eval-v2eval-postpass-1": "cannot exclude a kill"},
            )

        # 5. Failed positive control -> FailedPositiveControlError
        failed_pos = VisionMutationReceipt(
            mutant_id="M-eval-v2eval-postpass-failed-pos",
            module_path=POSTPASS_PATH,
            category="operator_flip",
            source_digest=self.postpass_digest,
            qualified_ast_scope="PostPass.update",
            containing_expression="self.veto is not None",
            original_expression="self.veto is not None",
            structural_selector="self:Compare",
            transformation="Compare.ops[0]: IsNot -> Is",
            positive_control_passed=False,
            synthetic_rgb_digest=self.rgb_a,
            outcome=OUTCOME_INFRASTRUCTURE,
            failure_detail="positive_control_failed",
        )
        with self.assertRaises(FailedPositiveControlError):
            reduce_adequacy_receipts(
                base + [failed_pos], expected_source_digests=self.expected_sources
            )

        # 6. Denominator tampering -> DenominatorTamperError
        with self.assertRaises(DenominatorTamperError):
            reduce_adequacy_receipts(
                base,
                expected_source_digests=self.expected_sources,
                expected_measured_denominator=99,
            )

    def test_foreign_scheduler_receipts_are_provenance_fixtures_only(self) -> None:
        """Prove foreign scheduler receipts never enter vision-engine local source kills."""
        local_kills = self._sample_local_receipts()[:2]
        foreign_sched_1 = ForeignSchedulerReceipt(
            mutant_id="M-wender-scheduler-lease-1",
            repository="wender-scheduler",
            module_path="wender/scheduler/lease_queue.py",
            category="scheduler_timeout",
            source_digest="1" * 64,
            outcome=OUTCOME_KILLED,
            scheduler_id="wavemind-scheduler-r1",
        )
        foreign_sched_2 = VisionMutationReceipt(
            mutant_id="M-wender-scheduler-lease-2",
            module_path="wender/scheduler/dispatch.py",
            category="scheduler_dispatch",
            source_digest="2" * 64,
            qualified_ast_scope="Dispatcher.poll",
            containing_expression="seq >= 0",
            original_expression="seq >= 0",
            structural_selector="self:Compare",
            transformation="Compare.ops[0]: GtE -> Gt",
            positive_control_passed=True,
            synthetic_rgb_digest=self.rgb_a,
            outcome=OUTCOME_KILLED,
            repository="wender-scheduler",
        )

        report = evaluate_ast_mutation_adequacy(
            local_kills + [foreign_sched_1],
            foreign_receipts=[foreign_sched_2],
            expected_source_digests=self.expected_sources,
            bound_rgb_digests=self.bound_rgb,
        )

        # Local numerator and denominator reflect ONLY the 2 vision-engine local receipts
        self.assertEqual(report.killed_numerator, 2)
        self.assertEqual(report.measured_denominator, 2)
        self.assertEqual(report.total_local_sites, 2)
        self.assertTrue(report.gate_passed)
        self.assertEqual(set(report.by_module.keys()), {POSTPASS_PATH})
        self.assertNotIn("wender/scheduler/lease_queue.py", report.by_module)
        self.assertNotIn("wender/scheduler/dispatch.py", report.by_module)
        self.assertNotIn("scheduler_timeout", report.by_category)
        self.assertNotIn("scheduler_dispatch", report.by_category)

        # Foreign receipts are preserved as provenance fixtures
        self.assertEqual(report.foreign_provenance_count, 2)
        self.assertEqual(
            [f.mutant_id for f in report.foreign_provenance_receipts],
            ["M-wender-scheduler-lease-2", "M-wender-scheduler-lease-1"],
        )

        # Disguising a foreign scheduler module path as repository='vision-engine' is rejected
        disguised_foreign = VisionMutationReceipt(
            mutant_id="M-disguised-scheduler-kill",
            module_path="wender/scheduler/lease_queue.py",
            category="operator_flip",
            source_digest=self.postpass_digest,
            qualified_ast_scope="LeaseQueue.pop",
            containing_expression="ready > 0",
            original_expression="ready > 0",
            structural_selector="self:Compare",
            transformation="Compare.ops[0]: Gt -> GtE",
            positive_control_passed=True,
            synthetic_rgb_digest=self.rgb_a,
            outcome=OUTCOME_KILLED,
            repository="vision-engine",
        )
        with self.assertRaises(ForeignReceiptError):
            evaluate_ast_mutation_adequacy(local_kills + [disguised_foreign])

        # Strict mode rejects foreign receipts mixed into the local stream
        with self.assertRaises(ForeignReceiptError):
            evaluate_ast_mutation_adequacy(
                local_kills + [foreign_sched_1],
                reject_foreign_in_local_stream=True,
            )

    def test_deterministic_input_order_independence_and_multithreaded_reduction(self) -> None:
        """Verify input-order independence across seeded permutations and concurrent threads."""
        rng = random.Random(ADEQUACY_REPLAY_SEED)
        receipts = self._sample_local_receipts()
        exclusions = {
            "M-eval-v2eval-postpass-equiv-1": "idempotent int conversion after integer guard"
        }
        foreign = [
            ForeignSchedulerReceipt(
                mutant_id=f"M-foreign-{i}",
                repository="wender-scheduler",
                module_path=f"wender/scheduler/mod_{i}.py",
                category="scheduler_cat",
                source_digest=f"{i + 1}" * 64,
                outcome=OUTCOME_KILLED,
            )
            for i in range(3)
        ]

        baseline_report = reduce_adequacy_receipts(
            receipts,
            foreign_receipts=foreign,
            expected_source_digests=self.expected_sources,
            bound_rgb_digests=self.bound_rgb,
            known_exclusions=exclusions,
        )

        n_threads = 6
        barrier = threading.Barrier(n_threads, timeout=5.0)
        reports: list[object] = [None] * n_threads
        errors: list[BaseException] = []
        err_lock = threading.Lock()
        threads_before = {t.ident for t in threading.enumerate()}

        PermutedInputs = []
        for _ in range(n_threads):
            r_copy = list(receipts) + list(foreign)
            rng.shuffle(r_copy)
            PermutedInputs.append(r_copy)

        def _worker(idx: int, permuted: list[VisionMutationReceipt | ForeignSchedulerReceipt]) -> None:
            try:
                barrier.wait(timeout=5.0)
                rep = reduce_adequacy_receipts(
                    permuted,
                    expected_source_digests=self.expected_sources,
                    bound_rgb_digests=self.bound_rgb,
                    known_exclusions=exclusions,
                )
                barrier.wait(timeout=5.0)
                reports[idx] = rep
            except BaseException as exc:  # pragma: no cover
                with err_lock:
                    errors.append(exc)

        workers = [
            threading.Thread(
                target=_worker,
                args=(i, PermutedInputs[i]),
                name=f"adequacy-worker-{i}",
                daemon=True,
            )
            for i in range(n_threads)
        ]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=5.0)
            self.assertFalse(w.is_alive(), f"thread {w.name} did not join within timeout")

        if errors:
            raise errors[0]

        threads_after = {t.ident for t in threading.enumerate()}
        self.assertEqual(threads_before, threads_after)

        for rep in reports:
            self.assertIsNotNone(rep)
            assert isinstance(rep, type(baseline_report))
            self.assertEqual(rep.report_digest, baseline_report.report_digest)
            self.assertEqual(rep.local_receipts, baseline_report.local_receipts)
            self.assertEqual(
                rep.foreign_provenance_receipts, baseline_report.foreign_provenance_receipts
            )
            self.assertEqual(rep.by_module, baseline_report.by_module)
            self.assertEqual(rep.by_category, baseline_report.by_category)


if __name__ == "__main__":
    unittest.main()
