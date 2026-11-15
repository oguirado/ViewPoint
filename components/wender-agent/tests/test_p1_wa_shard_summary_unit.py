"""Independent, RAM-only controls for PROTO1 Area A gates."""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import random
import threading
import unittest


SOURCE_PIN = "9c418c4ab2cb4cc43af302a4c4d62639e4ce9bf7"
REPO = "example-owner/wender-agent"


def independent_bytes(value):
    # Independent expected-byte policy; do not call the production serializer.
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def independent_sha(value):
    return hashlib.sha256(independent_bytes(value)).hexdigest()


def draft():
    return {
        "schema": "proto1.shard-manifest/v1", "repo": REPO,
        "source_commit": SOURCE_PIN,
        "shards": [
            {"shard_id": "shard.a", "sequence": 0,
             "selected_case_ids": ["case.alpha", "case.beta"]},
            {"shard_id": "shard.b", "sequence": 1,
             "selected_case_ids": ["case.gamma"]},
        ],
        "obligations": [
            {"obligation_id": "obligation.x", "case_ids": ["case.alpha", "case.gamma"]},
            {"obligation_id": "obligation.y", "case_ids": ["case.beta"]},
        ],
        "allowed_skip_case_ids": [],
    }


def receipt_for(manifest, shard_id, **updates):
    shard = next(s for s in manifest["shards"] if s["shard_id"] == shard_id)
    cases = list(shard["selected_case_ids"])
    stdout = ("actual-stdout:" + shard_id).encode("ascii")
    record = {
        "schema": "proto1.shard-receipt/v1", "repo": REPO,
        "source_commit": SOURCE_PIN, "manifest_sha256": manifest["manifest_sha256"],
        "shard_id": shard_id, "attempt_id": "a1", "selected_case_ids": cases,
        "executed_case_ids": list(cases), "tests_run": len(cases),
        "failures": 0, "errors": 0, "skips": 0, "unexpected_successes": 0,
        "gate_exit_code": 0,
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "started_mono_ns": 11, "finished_mono_ns": 19, "cpu_ns": 5,
        "cleanup": {"known": False, "verified": False,
                    "remaining_bytes": None, "remaining_children": None},
        "host_metric": {"name": "memory_peak", "value": None, "unit": "bytes",
                        "evidence": "AUSENTE: no host process measurement", "status": "AUSENTE"},
        "reason": "AUSENTE: host performance measurement not available",
    }
    record.update(updates)
    return record


def attest(module, receipt, *, known=True, verified=True, remaining_bytes=0,
           remaining_children=0, evidence="host:cgroup-process-reconciliation",
           host_metric=None):
    # Hash is derived wholly independently of the tested module.
    return module.HostCleanup(
        shard_id=receipt["shard_id"], attempt_id=receipt["attempt_id"],
        receipt_sha256=independent_sha(receipt), known=known, verified=verified,
        remaining_bytes=remaining_bytes if known else None,
        remaining_children=remaining_children if known else None,
        evidence=evidence, host_metric=host_metric)


class ShardSummaryUnit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Import explicitly and fail loudly if the new producer is missing.
        cls.p = importlib.import_module("wender.learn.proto1_shard_summary")

    def setUp(self):
        self.m = self.p.freeze_manifest(draft())
        self.receipts = [receipt_for(self.m, "shard.a"),
                         receipt_for(self.m, "shard.b")]
        self.hosts = {r["shard_id"]: attest(self.p, r) for r in self.receipts}

    def refuse(self, code, receipts=None, manifest=None, hosts=None):
        if receipts is None:
            receipts = self.receipts
        if manifest is None:
            manifest = self.m
        if hosts is None:
            hosts = self.hosts
        with self.assertRaises(self.p.GateRefusal) as ctx:
            self.p.reduce_receipts(manifest, receipts, host_cleanups=hosts)
        self.assertEqual(ctx.exception.exit_code, code)
        with self.assertRaises(self.p.GateRefusal):
            self.p.completion_certificate(manifest, receipts, host_cleanups=hosts)
        return ctx.exception.reason

    def no_completion(self, code, receipts=None, hosts=None):
        receipts = receipts if receipts is not None else self.receipts
        hosts = self.hosts if hosts is None else hosts
        summary = self.p.reduce_receipts(self.m, receipts, host_cleanups=hosts)
        self.assertFalse(summary.passed)
        self.assertEqual(summary.exit_code, code)
        with self.assertRaises(self.p.GateRefusal) as ctx:
            self.p.completion_certificate(self.m, receipts, host_cleanups=hosts)
        self.assertEqual(ctx.exception.exit_code, code)
        return summary

    def test_manifest_digest_is_independent_exact_bytes(self):
        expected_body = draft()
        expected_body["obligations"].sort(key=lambda a: a["obligation_id"])
        self.assertEqual(self.m["manifest_sha256"], independent_sha(expected_body))
        self.assertEqual(self.p.canonical_bytes(expected_body), independent_bytes(expected_body))
        self.assertEqual(self.p.validate_manifest(self.m), self.m)
        self.assertEqual(self.m["source_commit"], SOURCE_PIN)

    def test_positive_disjoint_shards_exact_totals_and_certificate(self):
        summary = self.p.reduce_receipts(self.m, self.receipts, host_cleanups=self.hosts)
        self.assertEqual(summary.exit_code, 0)
        self.assertTrue(summary.accounting_closed and summary.passed)
        self.assertEqual(summary.totals, {"tests_run": 3, "failures": 0, "errors": 0,
                                         "skips": 0, "unexpected_successes": 0,
                                         "selected_cases": 3, "executed_cases": 3})
        self.assertEqual(summary.obligations_covered, ("obligation.x", "obligation.y"))
        self.assertEqual(summary.retained_shards, ())
        sealed = {"manifest_sha256": self.m["manifest_sha256"],
                  "receipts": sorted(self.receipts, key=lambda r: (r["shard_id"], r["attempt_id"]))}
        self.assertEqual(summary.receipt_set_sha256, independent_sha(sealed))
        cert = self.p.completion_certificate(self.m, self.receipts, host_cleanups=self.hosts)
        self.assertEqual(cert["schema"], "proto1.completion/v1")
        self.assertEqual(cert["source_commit"], SOURCE_PIN)
        self.assertEqual(cert["tests_run"], 3)
        self.assertEqual(cert["obligation_ids"], ["obligation.x", "obligation.y"])
        self.assertEqual(cert["certificate_sha256"], independent_sha({k: v for k, v in cert.items()
                                                                      if k != "certificate_sha256"}))
        self.assertEqual(cert, self.p.completion_certificate(self.m, list(reversed(self.receipts)),
                                                             host_cleanups=self.hosts))

    def test_positive_guest_cleanup_false_but_independent_host_clean(self):
        # Guest can neither confer nor revoke host authority.
        summary = self.p.reduce_receipts(self.m, self.receipts, host_cleanups=self.hosts)
        self.assertTrue(summary.passed)
        self.assertFalse(summary.raw_shard_status[0]["guest_cleanup"]["known"])

    def test_duplicate_receipt_refusal(self):
        reason = self.refuse(5, self.receipts + [copy.deepcopy(self.receipts[0])])
        self.assertEqual(reason, "duplicate_attempt_replay")

    def test_duplicate_same_shard_other_attempt(self):
        other = copy.deepcopy(self.receipts[0]); other["attempt_id"] = "a2"
        self.assertEqual(self.refuse(5, self.receipts + [other]), "duplicate_shard_receipt")

    def test_replayed_attempt_with_altered_content_is_fenced(self):
        ledger = self.p.ReceiptLedger()
        self.assertEqual(ledger.claim(self.m, self.receipts[0]), "NEW")
        altered = copy.deepcopy(self.receipts[0]); altered["stdout_sha256"] = "f" * 64
        with self.assertRaises(self.p.GateRefusal) as ctx:
            ledger.claim(self.m, altered)
        self.assertEqual((ctx.exception.exit_code, ctx.exception.reason), (5, "altered_attempt_replay"))
        self.assertEqual(ledger.count(), 1)
        self.assertEqual(ledger.claim(self.m, copy.deepcopy(self.receipts[0])), "IDENTICAL_REPLAY")
        self.assertEqual(ledger.count(), 1)

    def test_linearizable_first_claim_under_forced_barrier(self):
        ledger = self.p.ReceiptLedger()
        barrier = threading.Barrier(8)
        result = []
        write_lock = threading.Lock()
        seed = random.Random(20261021)
        variants = [copy.deepcopy(self.receipts[0]) for _ in range(8)]
        for i in seed.sample(list(range(8)), 4):
            variants[i]["stdout_sha256"] = "a" * 64
        def compete(value):
            barrier.wait()
            try:
                claim = ledger.claim(self.m, value)
            except self.p.GateRefusal as exc:
                claim = f"refused:{exc.exit_code}"
            with write_lock:
                result.append(claim)
        threads = [threading.Thread(target=compete, args=(r,)) for r in variants]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(result), 8)
        self.assertEqual(result.count("NEW"), 1)
        self.assertEqual(ledger.count(), 1)
        self.assertEqual(result.count("IDENTICAL_REPLAY"), 3)
        self.assertEqual(result.count("refused:5"), 4)

    def test_foreign_repo_rejected(self):
        bad = copy.deepcopy(self.receipts); bad[0]["repo"] = "other/wender-agent"
        self.assertEqual(self.refuse(5, bad), "foreign_repo")

    def test_foreign_source_pin_rejected(self):
        bad = copy.deepcopy(self.receipts); bad[0]["source_commit"] = "a" * 40
        self.assertEqual(self.refuse(5, bad), "foreign_source_pin")

    def test_foreign_manifest_digest_rejected(self):
        bad = copy.deepcopy(self.receipts); bad[0]["manifest_sha256"] = "e" * 64
        self.assertEqual(self.refuse(5, bad), "foreign_manifest_digest")

    def test_unplanned_selected_case_rejected(self):
        bad = copy.deepcopy(self.receipts)
        bad[0]["selected_case_ids"].append("case.unplanned")
        self.assertEqual(self.refuse(5, bad), "selected_case_sequence_mismatch")

    def test_unplanned_executed_case_rejected(self):
        bad = copy.deepcopy(self.receipts)
        bad[0]["executed_case_ids"][-1] = "case.unplanned"
        self.assertEqual(self.refuse(5, bad), "missing_or_unplanned_executed_case")

    def test_omitted_case_rejected(self):
        bad = copy.deepcopy(self.receipts)
        bad[0]["executed_case_ids"].pop()
        bad[0]["tests_run"] = 1
        self.assertEqual(self.refuse(5, bad), "missing_or_unplanned_executed_case")

    def test_missing_shard_sequence_rejected(self):
        self.assertEqual(self.refuse(5, self.receipts[:1]), "missing_shard_sequence")

    def test_missing_manifest_sequence_rejected(self):
        d = draft(); d["shards"][1]["sequence"] = 3
        with self.assertRaises(self.p.GateRefusal) as ctx:
            self.p.freeze_manifest(d)
        self.assertEqual((ctx.exception.exit_code, ctx.exception.reason), (5, "missing_sequence"))

    def test_forged_frozen_manifest_digest_rejected(self):
        bad = copy.deepcopy(self.m); bad["obligations"][0]["case_ids"].append("case.beta")
        self.assertEqual(self.refuse(5, self.receipts, bad), "manifest_digest_mismatch")

    def test_incomplete_obligation_set_rejected(self):
        d = draft(); d["obligations"] = d["obligations"][:1]
        with self.assertRaises(self.p.GateRefusal) as ctx:
            self.p.freeze_manifest(d)
        self.assertEqual((ctx.exception.exit_code, ctx.exception.reason), (5, "incomplete_obligation_set"))

    def test_false_success_with_zero_tests_rejected(self):
        bad = copy.deepcopy(self.receipts)
        bad[0]["tests_run"] = 0
        self.assertEqual(self.refuse(3, bad), "claimed_execution_zero_tests")

    def test_exit_zero_with_failure_rejected(self):
        bad = copy.deepcopy(self.receipts); bad[0]["failures"] = 1
        self.assertEqual(self.refuse(5, bad), "false_success_status")

    def test_exit_zero_with_error_rejected(self):
        bad = copy.deepcopy(self.receipts); bad[0]["errors"] = 1
        self.assertEqual(self.refuse(5, bad), "false_success_status")

    def test_exit_zero_with_unexpected_success_rejected(self):
        bad = copy.deepcopy(self.receipts); bad[0]["unexpected_successes"] = 1
        self.assertEqual(self.refuse(5, bad), "false_success_status")

    def test_exit_zero_with_skip_rejected(self):
        bad = copy.deepcopy(self.receipts); bad[0]["skips"] = 1
        self.assertEqual(self.refuse(5, bad), "false_success_status")

    def test_nonzero_failure_closes_accounting_not_success(self):
        bad = copy.deepcopy(self.receipts); bad[0]["failures"] = 1
        bad[0]["gate_exit_code"] = 5; bad[0]["reason"] = "one real test failed"
        hosts = {r["shard_id"]: attest(self.p, r) for r in bad}
        summary = self.no_completion(5, bad, hosts)
        self.assertTrue(summary.accounting_closed)
        self.assertEqual(summary.totals["failures"], 1)
        self.assertEqual(summary.raw_shard_status[0]["gate_exit_code"], 5)
        self.assertEqual(summary.raw_shard_status[0]["failures"], 1)

    def test_nonzero_error_closes_accounting_not_success(self):
        bad = copy.deepcopy(self.receipts); bad[0]["errors"] = 1
        bad[0]["gate_exit_code"] = 5; bad[0]["reason"] = "one test errored"
        hosts = {r["shard_id"]: attest(self.p, r) for r in bad}
        summary = self.no_completion(5, bad, hosts)
        self.assertTrue(summary.accounting_closed)
        self.assertEqual(summary.totals["errors"], 1)

    def test_nonzero_unexpected_success_closes_not_success(self):
        bad = copy.deepcopy(self.receipts); bad[0]["unexpected_successes"] = 1
        bad[0]["gate_exit_code"] = 5; bad[0]["reason"] = "unexpected success"
        hosts = {r["shard_id"]: attest(self.p, r) for r in bad}
        summary = self.no_completion(5, bad, hosts)
        self.assertEqual(summary.totals["unexpected_successes"], 1)
        self.assertTrue(summary.accounting_closed)

    def test_disallowed_skip_rejected_even_for_nonzero_gate(self):
        bad = copy.deepcopy(self.receipts); bad[0]["skips"] = 1
        bad[0]["gate_exit_code"] = 5
        self.assertEqual(self.refuse(5, bad), "disallowed_skip")

    def test_authorized_skip_counts_but_never_passes(self):
        d = draft(); d["allowed_skip_case_ids"] = ["case.alpha", "case.beta"]
        m = self.p.freeze_manifest(d)
        records = [receipt_for(m, "shard.a", skips=1, gate_exit_code=5),
                   receipt_for(m, "shard.b")]
        hosts = {r["shard_id"]: attest(self.p, r) for r in records}
        summary = self.p.reduce_receipts(m, records, host_cleanups=hosts)
        self.assertFalse(summary.passed)
        self.assertEqual(summary.exit_code, 5)
        self.assertEqual(summary.totals["skips"], 1)
        with self.assertRaises(self.p.GateRefusal):
            self.p.completion_certificate(m, records, host_cleanups=hosts)

    def test_unknown_guest_cleanup_with_no_host_evidence_retains_resources(self):
        summary = self.no_completion(6, hosts={})
        self.assertFalse(summary.accounting_closed)
        self.assertEqual(summary.retained_shards, ("shard.a", "shard.b"))

    def test_guest_optimistic_cleanup_is_not_host_authority(self):
        bad = copy.deepcopy(self.receipts)
        for r in bad:
            r["cleanup"] = {"known": True, "verified": True,
                            "remaining_bytes": 0, "remaining_children": 0}
        summary = self.no_completion(6, bad, hosts={})
        self.assertEqual(summary.retained_shards, ("shard.a", "shard.b"))

    def test_unknown_host_cleanup_retains_resources(self):
        hosts = copy.deepcopy(self.hosts)
        hosts["shard.a"] = attest(self.p, self.receipts[0], known=False, verified=False)
        summary = self.no_completion(6, hosts=hosts)
        self.assertIn("shard.a", summary.retained_shards)
        self.assertFalse(summary.accounting_closed)

    def test_known_unverified_host_cleanup_retains_resources(self):
        hosts = copy.deepcopy(self.hosts)
        hosts["shard.a"] = attest(self.p, self.receipts[0], known=True, verified=False)
        summary = self.no_completion(6, hosts=hosts)
        self.assertIn("shard.a", summary.retained_shards)

    def test_remaining_host_children_blocks_completion(self):
        hosts = copy.deepcopy(self.hosts)
        hosts["shard.a"] = attest(self.p, self.receipts[0], remaining_children=1)
        summary = self.no_completion(5, hosts=hosts)
        self.assertIn("shard.a", summary.retained_shards)

    def test_wrong_host_attempt_and_receipt_digest_rejected(self):
        hosts = copy.deepcopy(self.hosts)
        hosts["shard.a"] = self.p.HostCleanup("shard.a", "a2", "0" * 64,
                                               True, True, 0, 0, "independent host")
        self.assertEqual(self.refuse(5, hosts=hosts), "foreign_host_cleanup_identity")
        hosts["shard.a"] = self.p.HostCleanup("shard.a", "a1", "0" * 64,
                                               True, True, 0, 0, "independent host")
        self.assertEqual(self.refuse(5, hosts=hosts), "unbound_host_cleanup")

    def test_host_metric_absent_is_null_with_reason(self):
        summary = self.p.reduce_receipts(self.m, self.receipts, host_cleanups=self.hosts)
        for metric in summary.host_metrics:
            self.assertIsNone(metric["value"])
            self.assertEqual(metric["status"], "AUSENTE")
            self.assertIn("AUSENTE", metric["reason"])

    def test_guest_claim_of_measured_metric_not_promoted(self):
        altered = copy.deepcopy(self.receipts)
        altered[0]["host_metric"] = {"name": "memory_peak", "value": 900,
                                     "unit": "bytes", "evidence": "guest-only-claim",
                                     "status": "MEASURED"}
        hosts = {r["shard_id"]: attest(self.p, r) for r in altered}
        summary = self.p.reduce_receipts(self.m, altered, host_cleanups=hosts)
        self.assertIsNone(summary.host_metrics[0]["value"])

    def test_host_measured_metric_only_from_separate_host_evidence(self):
        hosts = copy.deepcopy(self.hosts)
        metric = {"name": "memory_peak", "value": 900, "unit": "bytes",
                  "evidence": "host-cgroup-observation", "status": "MEASURED"}
        hosts["shard.a"] = attest(self.p, self.receipts[0], host_metric=metric)
        summary = self.p.reduce_receipts(self.m, self.receipts, host_cleanups=hosts)
        self.assertEqual(summary.host_metrics[0]["value"], 900)
        self.assertIsNone(summary.host_metrics[1]["value"])

    def test_missing_absent_metric_reason_rejected(self):
        bad = copy.deepcopy(self.receipts)
        bad[0]["host_metric"] = None
        bad[0]["reason"] = ""
        self.assertEqual(self.refuse(2, bad), "absent_metric_needs_reason")

    def test_float_metric_and_boolean_counts_rejected(self):
        bad = copy.deepcopy(self.receipts); bad[0]["tests_run"] = True
        self.assertEqual(self.refuse(2, bad), "invalid_tests_run")
        bad = copy.deepcopy(self.receipts); bad[0]["host_metric"]["value"] = 1.5
        bad[0]["host_metric"]["status"] = "MEASURED"
        self.assertEqual(self.refuse(2, bad), "invalid_metric_value")

    def test_invalid_receipt_additional_field_rejected(self):
        bad = copy.deepcopy(self.receipts); bad[0]["untrusted_flag"] = True
        self.assertEqual(self.refuse(2, bad), "unexpected_receipt_fields")

    def test_runner_unavailable_capability_never_calls_executor(self):
        seen = []
        result = self.p.run_shard(self.m, "shard.a", "a1",
                    capability=self.p.CapabilityEvidence(False, "AUSENTE", ()),
                    executor=lambda cases: seen.append(cases))
        self.assertEqual(result.exit_code, 4)
        self.assertIsNone(result.receipt)
        self.assertEqual(seen, [])

    def test_runner_operator_hold_never_calls_executor(self):
        seen = []
        result = self.p.run_shard(self.m, "shard.a", "a1",
                    capability=self.p.CapabilityEvidence(True, "operator:fenced",
                        ("offline", "test_execution", "exact_case_identity"), operator_hold=True),
                    executor=lambda cases: seen.append(cases))
        self.assertEqual(result.exit_code, 7)
        self.assertIsNone(result.receipt)
        self.assertEqual(seen, [])

    def test_runner_missing_required_capability_never_calls_executor(self):
        seen = []
        result = self.p.run_shard(self.m, "shard.a", "a1",
                    capability=self.p.CapabilityEvidence(True, "host:partial-capability",
                        ("offline", "test_execution")),
                    executor=lambda cases: seen.append(cases))
        self.assertEqual(result.exit_code, 4)
        self.assertIsNone(result.receipt)
        self.assertEqual(seen, [])

    def test_runner_preflight_foreign_shard_never_calls_executor(self):
        seen = []
        with self.assertRaises(self.p.GateRefusal) as ctx:
            self.p.run_shard(self.m, "shard.foreign", "a1",
                capability=self.p.CapabilityEvidence(True, "host:preflight", ("offline", "test_execution", "exact_case_identity")),
                executor=lambda cases: seen.append(cases))
        self.assertEqual(ctx.exception.exit_code, 2)
        self.assertEqual(seen, [])

    def runner(self, observation, *, host_probe=None):
        seen = []
        cap = self.p.CapabilityEvidence(True, "independent:offline-executor-preflight",
                          ("offline", "test_execution", "exact_case_identity"))
        def exec_once(cases):
            seen.append(cases)
            return observation
        ticks = iter((101, 112))
        cpu = iter((31, 37))
        result = self.p.run_shard(self.m, "shard.a", "a1", capability=cap,
                                  executor=exec_once, host_probe=host_probe,
                                  monotonic_ns=lambda: next(ticks),
                                  cpu_clock_ns=lambda: next(cpu))
        self.assertEqual(seen, [("case.alpha", "case.beta")])
        return result

    def observation(self, **updates):
        base = dict(executed_case_ids=("case.alpha", "case.beta"), tests_run=2,
                    failures=0, errors=0, skips=0, unexpected_successes=0,
                    driver_exit_code=0, stdout=b"verifiable-output",
                    guest_cleanup={"known": True, "verified": True,
                                   "remaining_bytes": 0, "remaining_children": 0})
        base.update(updates)
        return self.p.ExecutionObservation(**base)

    def test_runner_positive_requires_independent_host_probe(self):
        result = self.runner(self.observation(), host_probe=lambda r: attest(self.p, r))
        self.assertEqual(result.exit_code, 0)
        self.assertIsNotNone(result.receipt)
        self.assertEqual(result.receipt["gate_exit_code"], 0)
        self.assertEqual(result.receipt["cpu_ns"], 6)
        self.assertEqual(result.receipt["stdout_sha256"], hashlib.sha256(b"verifiable-output").hexdigest())
        self.assertEqual(self.p.validate_receipt(self.m, result.receipt), result.receipt)
        self.assertIsNotNone(result.host_cleanup)

    def test_runner_zero_tests_never_claims_success_from_driver_exit(self):
        result = self.runner(self.observation(tests_run=0))
        self.assertEqual(result.exit_code, 3)
        self.assertIsNone(result.receipt)

    def test_runner_driver_exit_zero_but_test_failure_does_not_pass(self):
        result = self.runner(self.observation(failures=1), host_probe=lambda r: attest(self.p, r))
        self.assertEqual(result.exit_code, 5)
        self.assertEqual(result.receipt["failures"], 1)
        self.assertEqual(result.receipt["gate_exit_code"], 5)

    def test_runner_known_test_failure_preserved_under_unknown_cleanup(self):
        result = self.runner(self.observation(failures=1))
        self.assertEqual(result.exit_code, 6)
        self.assertIsNotNone(result.receipt)
        self.assertEqual(result.receipt["gate_exit_code"], 5)
        self.assertEqual(result.receipt["failures"], 1)
        self.assertIn("host_cleanup_probe_absent", result.receipt["reason"])

    def test_runner_driver_nonzero_with_zero_failures_is_failure(self):
        result = self.runner(self.observation(driver_exit_code=1),
                             host_probe=lambda r: attest(self.p, r))
        self.assertEqual(result.exit_code, 5)
        self.assertEqual(result.receipt["gate_exit_code"], 5)

    def test_runner_without_host_probe_retains_unknown(self):
        result = self.runner(self.observation())
        self.assertEqual(result.exit_code, 6)
        self.assertEqual(result.receipt["gate_exit_code"], 6)
        self.assertIsNone(result.host_cleanup)

    def test_runner_executor_sequence_mismatch_cannot_publish_receipt(self):
        result = self.runner(self.observation(executed_case_ids=("case.beta", "case.alpha")))
        self.assertEqual(result.exit_code, 5)
        self.assertIsNone(result.receipt)

    def test_runner_executor_exception_is_indeterminate(self):
        called = []
        def crash(cases):
            called.append(cases)
            raise RuntimeError("child outcome not known")
        result = self.p.run_shard(self.m, "shard.b", "a1",
            capability=self.p.CapabilityEvidence(True, "host-preflight",
                         ("offline", "test_execution", "exact_case_identity")),
            executor=crash)
        self.assertEqual(result.exit_code, 6)
        self.assertIsNone(result.receipt)
        self.assertEqual(called, [("case.gamma",)])

    def test_noncanonical_json_rejected(self):
        with self.assertRaises(self.p.GateRefusal) as ctx:
            self.p.canonical_bytes({"x": float("nan")})
        self.assertEqual(ctx.exception.exit_code, 2)
        with self.assertRaises(self.p.GateRefusal) as ctx:
            self.p.canonical_bytes({0: "not-string-key"})
        self.assertEqual(ctx.exception.exit_code, 2)


if __name__ == "__main__":
    unittest.main()
