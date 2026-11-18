"""Focused controls for worker authority and custody."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
import time
from unittest.mock import patch

from asset_factory import core
from test_st_lease import OfflineAuthority, ROOT, deadline, dummy_argv, launch_tripwire, supervisor
import test_st_lease_isolation as fixture
from tools.workers.lease import LeaseError, Residency, ReuseQualification


def pack(evidence):
    receipt = core.canonical_json(evidence).encode()
    return ReuseQualification(sha256(receipt).hexdigest(), receipt)


def synthetic_evidence(scope):
    # Isolates receipt validation in tests. Live admission tests below mint
    # their evidence from two jobs and the exact process retirement witness.
    clean = dict.fromkeys(scope.probes, True)
    return {
        "schema": "af.worker-isolation/v1",
        "adapter_id": scope.adapter_id,
        "adapter_sha256": scope.adapter_sha256,
        "workload_kind": scope.workload_kind,
        "identity_a": [10, 11],
        "identity_b": [10, 11],
        "before": clean.copy(),
        "perturbed": dict.fromkeys(scope.probes, False),
        "post_a": clean.copy(),
        "job_b": clean.copy(),
        "post_b": clean.copy(),
    }


class LeaseAuthorityTests(unittest.TestCase):
    worker = fixture.LeaseRepairTests.worker
    target = fixture.LeaseRepairTests.target
    load = fixture.LeaseRepairTests.load
    refuse = fixture.LeaseRepairTests.refuse
    test_second_job_in_same_unqualified_lease_is_refused = (
        fixture.LeaseRepairTests.test_second_job_in_same_unqualified_lease_is_refused
    )

    def qualified_worker(self, qualification, scope, *, blender=False, attest_scope=True):
        authority = OfflineAuthority()
        authority.binding = replace(authority.binding, reuse=qualification)
        worker = supervisor(authority, fixture.qualified_argv(blender),
                            adapter_scope=scope if attest_scope else None)
        self.addCleanup(worker.recover, deadline_ns=deadline(120))
        return worker, authority

    def test_caller_cannot_self_qualify(self):
        authority = OfflineAuthority()
        with self.assertRaisesRegex(TypeError, "reuse_safe"):
            Residency("lease-test", dummy_argv, authorize=authority.authorize,
                      consume_permit=authority.consume, confirm_retired=authority.retired,
                      runtime_s=120, cwd=ROOT, env={}, reuse_safe=True)

    def test_retirement_deadline_leaves_controller_unfenced(self):
        worker, authority = self.worker()
        authority.issue("first", self.target(worker))
        lease = self.load(worker, "first")
        end = deadline()
        clock = SimpleNamespace(monotonic_ns=time.monotonic_ns, sleep=time.sleep)
        original = worker._close

        def close_at_deadline(*args):
            result = original(*args)
            clock.monotonic_ns = lambda: end
            return result

        with patch("tools.workers.lease.time", clock), patch.object(worker, "_close", close_at_deadline):
            self.refuse("request_deadline", worker.unload, lease, deadline_ns=end)
        self.assertEqual(worker.last_terminal.state, "released")
        self.assertFalse(worker._fenced)
        authority.issue("next", self.target(worker))
        self.assertGreater(self.load(worker, "next").generation, lease.generation)

    def test_permit_replay_refused_even_if_external_consumer_accepts(self):
        worker, authority = self.worker()
        authority.issue("once", self.target(worker))
        lease = self.load(worker, "once")
        worker.unload(lease, deadline_ns=deadline())
        with patch.object(worker, "_consume", return_value=True) as consume, launch_tripwire() as starts:
            self.refuse("permit_replayed_or_invalid", self.load, worker, "once")
        consume.assert_not_called()
        self.assertEqual(starts, [])

    def test_permit_history_cap_preserves_consumed_id(self):
        worker, authority = self.worker()
        with patch("tools.workers.lease._MAX_CONSUMED_PERMITS", 1):
            authority.issue("once", self.target(worker))
            lease = self.load(worker, "once")
            worker.unload(lease, deadline_ns=deadline())
            authority.issue("new", self.target(worker))
            with launch_tripwire() as starts:
                self.refuse("permit_history_full", self.load, worker, "new")
                self.refuse("permit_replayed_or_invalid", self.load, worker, "once")
            self.assertEqual(starts, [])
            self.assertEqual(authority.used, {"once"})

    def test_malformed_permit_never_reaches_authority(self):
        worker, _ = self.worker()
        with patch.object(worker, "_consume", return_value=True) as consume, launch_tripwire() as starts:
            for invalid in (True, "", "x" * 257):
                with self.subTest(invalid=invalid):
                    self.refuse("permit_replayed_or_invalid", self.load, worker, invalid)
        consume.assert_not_called()
        self.assertEqual(starts, [])

    def test_uncertain_permit_remains_blocked_after_recovery(self):
        worker, _ = self.worker()
        with patch.object(worker, "_consume", side_effect=RuntimeError("authority disconnected")):
            with self.assertRaisesRegex(RuntimeError, "authority disconnected"):
                self.load(worker, "uncertain")
        worker.recover(deadline_ns=deadline())
        with patch.object(worker, "_consume", return_value=True) as consume:
            self.refuse("permit_replayed_or_invalid", self.load, worker, "uncertain")
        consume.assert_not_called()

    def test_unqualified_release_retires_owned_worker(self):
        worker, authority = self.worker()
        authority.issue("first", self.target(worker))
        first = self.load(worker, "first")
        observation = worker.query(first, {}, deadline_ns=deadline())
        worker.release(first, deadline_ns=deadline())
        self.assertFalse(fixture.identity_present(observation["identity"]))
        self.refuse("permit_required", self.load, worker)

    def test_drain_resets_dispatch_for_next_generation(self):
        worker, authority = self.worker()
        authority.issue("first", self.target(worker))
        first = self.load(worker, "first")
        a = worker.query(first, {}, deadline_ns=deadline())
        worker.release(first, deadline_ns=deadline())
        self.assertFalse(fixture.identity_present(a["identity"]))
        authority.issue("next", self.target(worker))
        second = self.load(worker, "next")
        self.assertGreater(second.generation, first.generation)
        try:
            b = worker.query(second, {}, deadline_ns=deadline())
        except LeaseError as exc:
            self.fail(f"fresh generation rejected its first job: {exc.code}")
        self.assertNotEqual(a["identity"], b["identity"])

    def refuses_receipt(self, code, qualification, scope, *, attest_scope=True):
        worker, authority = self.qualified_worker(qualification, scope, attest_scope=attest_scope)
        authority.issue("first", self.target(worker))
        with launch_tripwire() as starts:
            self.refuse(code, self.load, worker, "first")
        self.assertEqual(starts, [])
        self.assertEqual(authority.used, set())

    def test_authority_receipt_digest_mismatch_refused(self):
        scope = fixture.adapter_scope(False)
        valid = pack(synthetic_evidence(scope))
        self.refuses_receipt("worker_isolation_receipt", replace(valid, receipt_sha256="f" * 64), scope)

    def test_receipt_requires_installed_scope(self):
        scope = fixture.adapter_scope(False)
        self.refuses_receipt("worker_isolation_receipt", pack(synthetic_evidence(scope)),
                             scope, attest_scope=False)

    def test_receipt_for_another_adapter_refused(self):
        scope = fixture.adapter_scope(False)
        evidence = synthetic_evidence(scope)
        self.refuses_receipt("worker_isolation_scope", pack({**evidence, "adapter_id": "another"}), scope)

    def test_stale_adapter_digest_refused(self):
        scope = fixture.adapter_scope(False)
        evidence = synthetic_evidence(scope)
        self.refuses_receipt("worker_isolation_scope", pack({**evidence, "adapter_sha256": "f" * 64}), scope)

    def test_receipt_for_another_workload_refused(self):
        scope = fixture.adapter_scope(False)
        evidence = synthetic_evidence(scope)
        self.refuses_receipt("worker_isolation_scope", pack({**evidence, "workload_kind": "another"}), scope)

    def test_malformed_receipt_refused(self):
        scope = fixture.adapter_scope(False)
        valid = pack(synthetic_evidence(scope)).receipt
        candidates = (valid + b" ", b"{}", b'{"bad":1.5}', b"\xff",
                      pack({**synthetic_evidence(scope), "schema": "unknown"}).receipt,
                      pack({**synthetic_evidence(scope), "padding": "x" * 65536}).receipt)
        for receipt in candidates:
            with self.subTest(receipt=receipt[:60]):
                self.refuses_receipt("worker_isolation_receipt",
                                     ReuseQualification(sha256(receipt).hexdigest(), receipt), scope)
        with self.subTest(receipt="untyped"):
            self.refuses_receipt("worker_isolation_receipt", True, scope)
        with self.subTest(receipt="mutable bytes"):
            self.refuses_receipt("worker_isolation_receipt",
                                 ReuseQualification(sha256(valid).hexdigest(), bytearray(valid)), scope)

    def test_receipt_requires_same_live_process(self):
        scope = fixture.adapter_scope(False)
        evidence = synthetic_evidence(scope)
        for a, b in (([10, 11], [10, 12]), ([1, 11], [1, 11]), ([10, 1], [10, True])):
            with self.subTest(a=a, b=b):
                invalid = {**evidence, "identity_a": a, "identity_b": b}
                self.refuses_receipt("worker_isolation_evidence", pack(invalid), scope)

    def test_receipt_requires_observed_perturbation_and_all_clean_states(self):
        scope = fixture.adapter_scope(False)
        evidence = synthetic_evidence(scope)
        for stage, state in (("perturbed", evidence["before"]), ("before", {}),
                             ("post_a", {"rng": False}), ("job_b", {"rng": 1}),
                             ("post_b", None)):
            with self.subTest(stage=stage):
                self.refuses_receipt("worker_isolation_evidence", pack({**evidence, stage: state}), scope)

    def test_probe_requires_exact_boolean_and_coverage(self):
        scope = fixture.adapter_scope(False)
        clean = dict.fromkeys(scope.probes, True)
        for sample in ({**clean, "rng": 1}, {**clean, "extra": True}, {"rng": True}):
            with self.subTest(sample=sample):
                bad = {**synthetic_evidence(scope), "post_b": sample}
                self.refuses_receipt("worker_isolation_evidence", pack(bad), scope)

    def test_empty_probe_set_cannot_qualify(self):
        scope = replace(fixture.adapter_scope(False), probes=())
        self.refuses_receipt("worker_isolation_evidence", pack(synthetic_evidence(scope)), scope)

    def test_qualification_requires_retirement_witness(self):
        with patch.object(fixture, "identity_present", return_value=True):
            self.refuse("release_unproven", fixture.qualification_run, False)

    def test_qualification_requires_owned_live_worker_identity(self):
        read = fixture.core.load_json

        def wrong_identity(path, **kwargs):
            reply = read(path, **kwargs)
            if path.name == "reply.json":
                reply["value"]["identity"] = [2147483647, 1]
            return reply

        with patch.object(fixture.core, "load_json", side_effect=wrong_identity):
            self.refuse("qualification_worker_unproven", fixture.qualification_run, False)

    def test_qualification_cannot_label_dummy_as_blender(self):
        scope = replace(fixture.adapter_scope(False), adapter_id="fixture-blender-cpu")
        with self.assertRaisesRegex(TypeError, "unexpected keyword argument 'scope'"):
            fixture.qualification_run(False, scope=scope)

    def test_forged_lease_cannot_remove_authority_reuse_binding(self):
        scope = fixture.adapter_scope(False)
        worker, authority = self.qualified_worker(pack(synthetic_evidence(scope)), scope)
        authority.issue("first", self.target(worker))
        lease = self.load(worker, "first")
        forged = replace(lease, binding=replace(lease.binding, reuse=None))
        self.refuse("lease_identity", worker.query, forged, {}, deadline_ns=deadline())
        self.assertEqual(worker.query(lease, {}, deadline_ns=deadline()), "fixture".encode().hex())

    def test_reuse_revocation_drains_before_warm_load(self):
        scope = fixture.adapter_scope(False)
        worker, authority = self.qualified_worker(pack(synthetic_evidence(scope)), scope)
        authority.issue("first", self.target(worker))
        lease = self.load(worker, "first")
        observation = worker.query(lease, {"op": "observe"}, deadline_ns=deadline())
        worker.release(lease, deadline_ns=deadline())
        authority.binding = replace(authority.binding, reuse=None)
        with launch_tripwire() as starts:
            self.refuse("lease_identity", self.load, worker)
        self.assertEqual(starts, [])
        self.assertFalse(fixture.identity_present(observation["identity"]))

    def test_post_job_probe_fails_closed(self):
        scope = fixture.adapter_scope(False)
        for fault in ("absent", "dirty"):
            with self.subTest(fault=fault):
                worker, authority = self.qualified_worker(pack(synthetic_evidence(scope)), scope)
                authority.issue("first", self.target(worker))
                lease = self.load(worker, "first")
                pid = worker._job._backend._child.pid
                owned = (pid, fixture.starttime(pid))
                original = worker._await

                def bad_reply(name, bound):
                    reply = original(name, bound)
                    if fault == "absent":
                        del reply["post_job_state"]
                    else:
                        reply["post_job_state"]["rng"] = False
                    return reply

                with patch.object(worker, "_await", side_effect=bad_reply):
                    self.refuse("worker_isolation_unproven", worker.query, lease, {}, deadline_ns=deadline())
                self.assertEqual(worker.last_terminal.state, "invalidated")
                self.assertEqual(worker.last_terminal.close_reason, "worker_isolation_unproven")
                self.assertFalse(fixture.identity_present(owned))

    def warm_isolation(self, blender):
        scope = fixture.adapter_scope(blender)
        try:
            qualification = fixture.qualification_run(blender)
        except LeaseError as exc:
            self.fail(f"same-resident qualification failed: {exc.code}")
        worker, authority = self.qualified_worker(qualification, scope, blender=blender)
        self.assertEqual(qualification.receipt_sha256, sha256(qualification.receipt).hexdigest())
        evidence = json.loads(qualification.receipt)
        self.assertEqual(evidence["identity_a"], evidence["identity_b"])
        clean = dict.fromkeys(scope.probes, True)
        dirty = dict.fromkeys(scope.probes, False)
        with TemporaryDirectory() as scratch, launch_tripwire() as starts:
            authority.issue("first", self.target(worker))
            first = self.load(worker, "first")
            a = worker.query(first, {"op": "poison", "output": str(Path(scratch) / "a")},
                             deadline_ns=deadline(20))
            self.assertEqual(a["state"], dirty)
            worker.release(first, deadline_ns=deadline())
            self.refuse("lease_already_closed", worker.release, first, deadline_ns=deadline())
            self.assertTrue(fixture.identity_present(a["identity"]))
            second = self.load(worker)
            self.assertEqual(first.generation, second.generation)
            self.assertGreater(second.lease_epoch, first.lease_epoch)
            self.assertEqual(second.binding.reuse, qualification)
            b = worker.query(second, {"op": "observe", "output": str(Path(scratch) / "b")},
                             deadline_ns=deadline(20))
            self.assertEqual(b["identity"], a["identity"])
            # Observe the handoff before Blender can change legitimate state.
            # query() independently requires the post-reset probe to pass.
            self.assertEqual(b["before"], clean)
            worker.unload(second, deadline_ns=deadline())
            self.assertFalse(fixture.identity_present(b["identity"]))
            self.assertEqual(len(starts), 1)

    def test_warm_dummy_isolation(self):
        self.warm_isolation(False)

    def test_warm_blender_cpu_isolation(self):
        self.warm_isolation(True)
