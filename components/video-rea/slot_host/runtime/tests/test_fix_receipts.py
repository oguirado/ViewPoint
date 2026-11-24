# SPDX-License-Identifier: AGPL-3.0-only
"""Fixes for rt-receipts (triage_runtime.json receipts.py items).

Each test covers one fix_list item and fails on the pre-fix receipts.py while
passing after: custody digest gating, decisive-verdict admission,
perception/replay/episode validation, and trusted reconciliation.
"""

import copy
from dataclasses import replace
import unittest

from slot_host.compare.tests.test_comparators import behaviour_comparison
from slot_host.runtime.tests.test_owner_handoff import compare_fixture
from slot_host.runtime.receipts import (
    build_attempt_receipt,
    evidence_accepted,
    finalize_receipt,
    receipt_digest,
    reconcile_run,
)

_IMPL = "a" * 64
_RESULT = "b" * 64
_TAPE = "c" * 64
_PUBLISHED_REF = "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2"
_PUBLISHED_SHA = "b3bfd6715abfa60a0866b655d23088d8bee09b0cf8e0a3b44c156e5692c19d8c"


def _good_result(status="OK", rc=0, sha=_RESULT):
    return {"schema": "rea.slot_result/v1", "status": status, "rc": rc, "sha256": sha}


def _good_custody(request_id="req-fix-0001", provider_id="fixture-slot", impl=_IMPL, result_sha=_RESULT):
    return {
        "offline_fixture": True,
        "bound_process_id": 12345,
        "observed_process_id": 12345,
        "bound_starttime": 67890,
        "observed_starttime": 67890,
        "cleanup_observed": True,
        "timed_out": False,
        "producer_binding_verified": True,
        "artifact_digest_verified": True,
        "provider_id": provider_id,
        "request_id": request_id,
        "observed_sha256": impl,
        "implementation_sha256": impl,
        "result_sha256": result_sha,
        "overflow": False,
        "stdout_truncated": False,
    }


def _offline_schema_pin():
    return {
        "shared_schema_ref": _PUBLISHED_REF,
        "shared_schema_sha256": _PUBLISHED_SHA,
    }


class FixReceiptsTest(unittest.TestCase):
    def test_result_digest_must_match_custody_witness_via_finalize(self):
        # Fix item 1: finalize must downgrade process_bound when the held
        # result digest is omitted, null, or different.
        opened = build_attempt_receipt(
            attempt_id="att-fix-0001", request_id="req-fix-0001", operation="record",
            slot_role="verify", slot_id="fixture-slot",
            provider_id="fixture-slot", implementation_sha256=_IMPL,
            **_offline_schema_pin(),
        )
        custody = _good_custody()
        good = finalize_receipt(opened, _good_result(), custody=custody)
        self.assertTrue(good["observation"]["producer"]["process_bound"])
        self.assertTrue(evidence_accepted({"receipt": good, "custody": copy.deepcopy(custody), "evidence_accepted": True}))

        for name, mutate in (
            ("omitted", lambda c: c.pop("result_sha256")),
            ("null", lambda c: c.update(result_sha256=None)),
            ("different", lambda c: c.update(result_sha256="f" * 64)),
        ):
            with self.subTest(case=name):
                bad_custody = _good_custody()
                mutate(bad_custody)
                finalized = finalize_receipt(opened, _good_result(), custody=bad_custody)
                self.assertFalse(finalized["observation"]["producer"]["process_bound"],
                                 f"{name} digest must not bind")
                wrapper = {"receipt": finalized, "custody": copy.deepcopy(bad_custody), "evidence_accepted": True}
                self.assertFalse(evidence_accepted(wrapper), f"{name} digest must not accept")
                self.assertNotIn("slot_result", finalized["attempt"])

        # Shared-receipt gate: a pending/unresolved adapter never accepts live use.
        pending = build_attempt_receipt(
            attempt_id="att-fix-pending", request_id="req-fix-0001", operation="record",
            slot_role="verify", slot_id="fixture-slot",
            provider_id="fixture-slot", implementation_sha256=_IMPL,
        )
        c2 = _good_custody()
        finalized_pending = finalize_receipt(pending, _good_result(), custody=c2)
        wrapper_pending = {"receipt": finalized_pending, "custody": copy.deepcopy(c2), "evidence_accepted": True}
        self.assertFalse(evidence_accepted(wrapper_pending), "pending adapter must not accept")

    def _compare_receipt(self, verdict):
        opened = build_attempt_receipt(
            attempt_id="att-fix-compare", request_id="req-fix-0001", operation="compare",
            slot_role="verify", slot_id="fixture-slot",
            provider_id="fixture-slot", implementation_sha256=_IMPL,
            reference_episode_id="ep-ref-0001", candidate_episode_id="ep-cand-0001",
            **_offline_schema_pin(),
        )
        custody = _good_custody()
        finalized = finalize_receipt(opened, _good_result(), custody=custody, verdict=verdict)
        return opened, finalized, custody

    def test_decisive_comparison_requires_admitted_check_strict(self):
        # Malformed and unbound decisive verdicts both downgrade to abstain.
        malformed = {
            "comparator_id": "host.compare", "comparator_version": "1", "result": "mismatch",
            "policy": {"id": "policy:fixture@1", "version": "1", "sha256": "0" * 64},
            "checks": [{"id": "check-1", "outcome": "mismatch", "detail": "no bound divergence"}],
            "contrast_ids": [], "negative_controls": [],
            "oracle_independence": "independent", "independent_qualification": True,
        }
        _, finalized, custody = self._compare_receipt(copy.deepcopy(malformed))
        self.assertEqual(finalized["verdict"]["result"], "abstain")
        self.assertEqual(finalized["verdict"]["checks"], [])
        self.assertFalse(evidence_accepted({"receipt": finalized, "custody": copy.deepcopy(custody), "evidence_accepted": True}))

        opened, _, custody_ok = self._compare_receipt(malformed)
        comparison = replace(behaviour_comparison("off"), attempt_id="att-fix-compare",
                             slot_result=_good_result())
        host, report = compare_fixture(comparison, request_id="req-fix-0001",
                                       episode_ids=("ep-ref-0001", "ep-cand-0001"))
        admitted = report["verdict"]
        self.assertEqual(admitted["result"], "mismatch")
        self.assertTrue(admitted["checks"][0]["first_divergence"])
        bound = dict(custody_ok, comparison_report=host.comparison_witness)
        finalized = finalize_receipt(opened, _good_result(), custody=bound, verdict=admitted)
        self.assertEqual(finalized["verdict"], admitted)
        self.assertTrue(evidence_accepted({"receipt": finalized, "custody": bound}))
        no_checks = finalize_receipt(opened, _good_result(), custody=bound, verdict=dict(admitted, checks=[]))
        self.assertEqual(no_checks["verdict"]["result"], "abstain")
        self.assertFalse(evidence_accepted({"receipt": no_checks, "custody": bound}))
        finalized_unbound = finalize_receipt(opened, _good_result(), custody=custody_ok, verdict=admitted)
        self.assertEqual(finalized_unbound["verdict"]["result"], "abstain")
        self.assertEqual(finalized_unbound["verdict"]["abstain_reason"], "custody_unverified")
        self.assertFalse(evidence_accepted({"receipt": finalized_unbound, "custody": custody_ok}))

        # Custody refusal downgrades an otherwise admitted decisive verdict.
        foreign = _good_custody(request_id="req-foreign-9999")
        opened_ok = build_attempt_receipt(
            attempt_id="att-fix-compare2", request_id="req-fix-0001", operation="compare",
            slot_role="verify", slot_id="fixture-slot",
            provider_id="fixture-slot", implementation_sha256=_IMPL,
            reference_episode_id="ep-ref-0001", candidate_episode_id="ep-cand-0001",
            **_offline_schema_pin(),
        )
        downgraded = finalize_receipt(opened_ok, _good_result(), custody=foreign, verdict=copy.deepcopy(admitted))
        self.assertEqual(downgraded["verdict"]["result"], "abstain")
        self.assertEqual(downgraded["verdict"]["abstain_reason"], "custody_unverified")
        self.assertFalse(downgraded["observation"]["producer"]["process_bound"])

        # Control refusal downgrades even with a valid OK result and checks.
        refused = build_attempt_receipt(
            attempt_id="att-fix-compare3", request_id="req-fix-0001", operation="compare",
            slot_role="verify", slot_id="fixture-slot",
            provider_id="fixture-slot", implementation_sha256=_IMPL,
            reference_episode_id="ep-ref-0001", candidate_episode_id="ep-cand-0001",
            control_outcome="NO_LEASE", **_offline_schema_pin(),
        )
        downgraded2 = finalize_receipt(refused, _good_result(), custody=_good_custody(), verdict=copy.deepcopy(admitted))
        self.assertEqual(downgraded2["verdict"]["result"], "abstain")
        self.assertEqual(downgraded2["verdict"]["abstain_reason"], "control_refused")

    def test_perception_ceiling_retained_and_replay_episode_validated(self):
        # Fix item 3: the ceiling is recorded verbatim; replay needs a
        # witnessed tape; episode lineage must be a valid identifier.
        capped = build_attempt_receipt(
            attempt_id="att-fix-cap", request_id="req-fix-0001", operation="record",
            perception_level="cdp_atspi", perception_ceiling="none",
            **_offline_schema_pin(),
        )
        self.assertEqual(capped["observation"]["perception_ceiling"], "none")
        self.assertEqual(capped["observation"]["perception_level"], "none")

        # Empty and tape-less replay objects are rejected.
        for name, replay in (
            ("empty", {}),
            ("missing-tape", {"determinism_mode": "uncontrolled"}),
            ("missing-mode", {"input_tape_sha256": _TAPE}),
            ("seeded-partial-controls", {"input_tape_sha256": _TAPE, "determinism_mode": "seeded",
                                          "seed": 7, "controls": {"clock": "controlled"}}),
        ):
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    build_attempt_receipt(
                        attempt_id=f"att-fix-replay-{name}", request_id="req-fix-0001",
                        operation="replay", slot_role="replay", parent_episode_id="ep-parent-0001",
                        replay=replay, **_offline_schema_pin(),
                    )
        with self.assertRaises(ValueError):
            build_attempt_receipt(
                attempt_id="att-fix-bad-ep", request_id="req-fix-0001", operation="replay",
                slot_role="replay", parent_episode_id="bad id",
                replay={"input_tape_sha256": _TAPE, "determinism_mode": "uncontrolled"},
                **_offline_schema_pin(),
            )

        good = build_attempt_receipt(
            attempt_id="att-fix-replay-ok", request_id="req-fix-0001", operation="replay",
            slot_role="replay", parent_episode_id="ep-parent-0001",
            replay={"input_tape_sha256": _TAPE, "determinism_mode": "uncontrolled"},
            **_offline_schema_pin(),
        )
        self.assertEqual(good["replay"]["input_tape_sha256"], _TAPE)

    def test_reconcile_requires_trusted_uncertain_and_omits_slot_result(self):
        # Fix item 4: only a trusted UNCERTAIN_OUTCOME prior with a bound
        # cleanup witness reconciles; the new receipt omits slot_result and
        # pins the exact prior digest.
        prior = build_attempt_receipt(
            attempt_id="att-fix-prior", request_id="req-fix-prior", operation="record",
            slot_role="verify", slot_id="fixture-slot",
            provider_id="fixture-slot", implementation_sha256=_IMPL,
            slot_result=_good_result(), control_outcome="UNCERTAIN_OUTCOME",
            **_offline_schema_pin(),
        )
        witness = dict(_good_custody(request_id="req-fix-prior"),
                       prior_attempt_id="att-fix-prior", prior_receipt_sha256=receipt_digest(prior))
        wrapper = {"receipt": copy.deepcopy(prior), "custody": copy.deepcopy(witness)}
        expected = receipt_digest(prior)
        rec = reconcile_run(wrapper, new_attempt_id="att-fix-rec", request_id="req-fix-new")
        self.assertEqual(rec["attempt"]["reconciles_attempt_id"], "att-fix-prior")
        self.assertEqual(rec["attempt"]["host_artifact_sha256"], expected)
        self.assertNotIn("slot_result", rec["attempt"])
        self.assertFalse(evidence_accepted(rec))
        self.assertEqual(receipt_digest(prior), expected)

        with self.assertRaisesRegex(ValueError, "exact prior receipt digest"):
            reconcile_run(wrapper, custody=dict(witness, prior_receipt_sha256="f" * 64),
                          new_attempt_id="att-fix-unbound", request_id="req-fix-new")

        # Clean success with a timeout flag is not uncertain.
        clean = build_attempt_receipt(
            attempt_id="att-fix-clean", request_id="req-fix-prior", operation="record",
            slot_role="verify", slot_id="fixture-slot",
            provider_id="fixture-slot", implementation_sha256=_IMPL,
            slot_result=_good_result(),
            **_offline_schema_pin(),
        )
        clean_witness = _good_custody(request_id="req-fix-prior")
        clean_witness["timed_out"] = True
        with self.assertRaises(ValueError):
            reconcile_run({"receipt": clean, "custody": clean_witness},
                          new_attempt_id="att-fix-bad1", request_id="req-fix-new")

        # Caller-asserted cleanup cannot override a false trusted witness.
        unclean_witness = dict(witness)
        unclean_witness["cleanup_observed"] = False
        with self.assertRaises(ValueError):
            reconcile_run({"receipt": copy.deepcopy(prior), "custody": unclean_witness},
                          new_attempt_id="att-fix-bad2", request_id="req-fix-new",
                          cleanup_observed=True)

        # Foreign request custody does not bind the retained receipt.
        foreign = _good_custody(request_id="req-foreign-9999")
        with self.assertRaises(ValueError):
            reconcile_run({"receipt": copy.deepcopy(prior), "custody": foreign},
                          new_attempt_id="att-fix-bad3", request_id="req-fix-new")

        # An embedded receipt custody dict is not a trusted witness.
        bare = copy.deepcopy(prior)
        bare["custody"] = _good_custody(request_id="req-fix-prior")
        with self.assertRaises(ValueError):
            reconcile_run(bare, new_attempt_id="att-fix-bad4", request_id="req-fix-new")


if __name__ == "__main__":
    unittest.main()
