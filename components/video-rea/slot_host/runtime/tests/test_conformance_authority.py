# SPDX-License-Identifier: AGPL-3.0-only
"""Default-on conformance predicate with host-created fixture receipts.

Covers U05 gate: the predicate turns true automatically after
qualification and denies once the receipt or prerequisite becomes
stale.  Owner seams (MAIN enrolment, Tools retention) deny by default;
missing enrolment, missing retention and stale receipts each deny and
keep the live gate at ``LIVE_EXECUTION_PARKED`` with zero spawns.

All keys are throwaway fixture keys in the test temp dir; no key
outside that dir is ever read.
"""
import datetime
import dataclasses
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from slot_host.runtime.binding import Binding, _PUBLISHED_SHA, binding_conformance_identity, sha256_of_file, verify_conformance_for_binding
from slot_host.runtime.conformance_authority import (
    ConformanceLiveAuthority,
    ReceiptStore,
    create_fixture_receipt_for_binding,
    policy_covers,
    receipt_matches_binding,
    evaluate_default_on,
    is_live_enabled,
    main_enrolment_available,
    tools_retention_available,
)
from slot_host.runtime.live_park import LIVE_EXECUTION_PARKED, authorize_live, make_conformance_authority
from slot_host.runtime.runner import run_bound
from slot_host.runtime import policy_activation
from slot_host.runtime.tests.test_policy_activation import _derive_key_id, _ed_keys, _make_envelope
from slot_host.runtime.tests.binding_fixtures import (
    fixture_retention,
    verified_fixture_binding as verify_binding,
)

setUpModule = fixture_retention

_ADAPTER = "d" * 64
_PROV = "a" * 64
_REQ = "b" * 64
_RES = "c" * 64
_KIT = "c" * 64
_REGISTRY_GEN = 1
_UTC = datetime.timezone.utc
_NOW = datetime.datetime(2026, 6, 1, 0, 0, 0, tzinfo=_UTC)
_LATER = datetime.datetime(2026, 6, 1, 1, 0, 0, tzinfo=_UTC)
_STALE_NOW = _LATER


def _manifest(impl_sha, profile="external_process"):
    def _op(oid, role):
        return {
            "id": oid,
            "role": role,
            "input_ref": f"urn:rea.slot_request:v2#slot={role}",
            "output_ref": f"urn:rea.slot_result:v2#slot={role}",
            "binding": {"kind": "cli_subcommand", "argv": [oid], "typed_args": []},
            "required_effects": ["process.spawn:child"],
            "required_grants": ["grant.exec.external_process"],
            "limits": {"timeout_s": 5, "max_output_bytes": 4096},
            "retry_semantics": {"mode": "at_most_once", "on_interrupt": "UNCERTAIN_OUTCOME"},
        }

    ops = [_op("record", "replay"), _op("replay", "replay"), _op("compare", "verify")]
    return {
        "schema": "rea.slot_manifest/v2",
        "slot_id": "fixture-slot",
        "status": "registered",
        "upstream": {
            "name": "fixture",
            "source_url": "https://example.com/fixture",
            "version": "1.0.0",
            "licence_id": "MIT",
            "licence_evidence_refs": ["fixture: licence"],
            "component_notices": [],
            "user_installed": True,
        },
        "provider_ref": {
            "schema": "rea.provider/v2",
            "provider_id": "fixture-slot",
            "provider_revision": "1.0.0",
            "provider_sha256": _PROV,
            "implementation_sha256": impl_sha,
        },
        "contract_refs": [
            {
                "role": "replay",
                "request_schema_ref": "urn:rea.slot_request:v2",
                "request_digest": _REQ,
                "result_schema_ref": "urn:rea.slot_result:v2",
                "result_digest": _RES,
                "execution_profile": profile,
            },
            {
                "role": "verify",
                "request_schema_ref": "urn:rea.slot_request:v2",
                "request_digest": _REQ,
                "result_schema_ref": "urn:rea.slot_result:v2",
                "result_digest": _RES,
                "execution_profile": profile,
            },
        ],
        "transport": {
            "kind": "cli_argv",
            "driver_location": "outside_capsule",
            "executable_binding": "user_selected",
            "argv_prefix": [],
            "framing": "none",
            "cwd_policy": "run_scratch",
            "env_allowlist": ["PATH"],
        },
        "operations": ops,
        "capabilities": {
            "target_kinds": ["fixture_target"],
            "platforms": ["linux"],
            "prerequisite_probes": [],
            "optional_features": [],
            "adapter_identity": {"adapter_id": "fixture.v1", "adapter_sha256": _ADAPTER, "provenance": "host_authored"},
        },
        "perception": {
            "required_level": "none",
            "optional_levels": [],
            "observation_sources": [
                {"id": "tool-evidence", "level": "none", "producer": "tool_stdio", "outside_capsule": True}
            ],
        },
        "record_replay": {
            "input_codec": "json",
            "output_codec": "json",
            "observable_channels": ["error"],
            "state_domain": "per_case",
            "effects": ["process.spawn:child"],
            "determinism_modes": ["recorded_inputs"],
            "freshness_inputs": ["fixture"],
        },
        "comparison": {
            "comparator_id": "fixture.compare",
            "version": "1",
            "policy_ref": "policy:fixture@1",
            "coverage_requirements": {"channel": "error"},
            "tolerance_units": "none",
            "waiver_policy": {"source": "host_policy", "binds": ["input", "actual", "reason", "scope", "policy_version"], "stale": "fail"},
        },
        "evidence": {
            "shared_schema_ref": "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2",
            "required_rea_fields": ["episode", "observation", "slot"],
            "artifact_roles": ["evidence_bundle"],
            "retention": "evidence_store_attempt",
            "visibility": "redacted_projection",
        },
        "conformance": {
            "fixture_refs": ["fixture:1"],
            "negative_control_refs": ["control:1"],
            "supported_entry_points": ["cli", "mcp", "json"],
            "required_checks": ["admit-and-project", "outside-record-replay-compare", "perception-and-pool", "transport-parity-and-retry"],
        },
        "discovery": {
            "summary": "fixture",
            "search_terms": ["fixture"],
            "operation_descriptions": {"record": "record", "replay": "replay", "compare": "compare"},
            "examples": [],
        },
    }


def _write_fixture(path: Path, counter: str):
    ctr = repr(counter).encode()
    body = (
        b"#!/usr/bin/env python3\n"
        b"# SPDX-License-Identifier: AGPL-3.0-only\n"
        b"import json, sys\n"
        b"ctr=" + ctr + b"\n"
        b"open(ctr,'a').write('1')\n"
        b'obj={"schema":"rea.slot_result/v1","status":"OK","rc":0,"sha256":"e"+"f"*63}\n'
        b"sys.stdout.write(json.dumps(obj)+'\\n')\n"
        b"sys.stdout.flush()\n"
    )
    path.write_bytes(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return sha256_of_file(path)


def _bind(manifest, exe: Path):
    return verify_binding(
        manifest,
        str(exe),
        operation_id="record",
        host_grants={"grant.exec.external_process"},
        allow_pending_evidence=True,
        requested_profile="external_process",
        **({"installation_ref": "installation:fixture-slot@1"} if hasattr(Binding, "installation_ref") else {}),
    )


def _policy(*, kit=_KIT, registry_gen=_REGISTRY_GEN, slot_id="fixture-slot", operations=None, generation=1, **over):
    ops = operations if operations is not None else ["record", "replay", "compare"]
    pol = {
        "schema": "rea.live_policy/v1",
        "purpose": "viewpoint.rea.external_execution",
        "policy_id": "live-policy-2026-001",
        "issuer": "operator",
        "key_id": "e" * 64,
        "audience": "viewpoint-rea-host",
        "generation": generation,
        "issued_at": "2026-01-01T00:00:00Z",
        "not_before": "2026-01-01T00:00:00Z",
        "expires_at": "2027-01-01T00:00:00Z",
        "baseline": {
            "mode": "live_after_conformance",
            "allowed_profiles": ["external_process"],
            "allowed_transports": ["cli_subcommand", "cli_argv"],
            "required_grants": ["grant.exec.external_process"],
            "perception_ceiling": "none",
            "shared_evidence_ref": "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2",
            "shared_evidence_sha256": _PUBLISHED_SHA,
            "conformance_kit_sha256": kit,
            "registry_generation": registry_gen,
            "enrollment_ref": "enrollment:fixture@1",
        },
        "scope": [{"slot_id": slot_id, "operation_ids": list(ops)}],
        "restrictions": [],
        "revocation": {
            "slot_ids": [],
            "operation_ids": [],
            "installation_refs": [],
            "implementation_sha256s": [],
            "revoked_generations": [],
        },
    }
    for k, v in over.items():
        pol[k] = v
    return pol



def _commit_policy(td, policy, *, now=_NOW, baseline_raw=False):
    td = tempfile.mkdtemp(prefix="policy-", dir=td)
    private, public = _ed_keys(td)
    key_id = _derive_key_id(public)
    state = Path(td) / "activation.json"
    policy = dict(policy, key_id=key_id)
    envelope = _make_envelope(private, policy, td)
    policy_activation.initialize_activation_state(state, audience=policy["audience"], key_id=key_id, now=now)
    policy_activation.activate(envelope, public_key_path=public, expected_audience=policy["audience"],
                               expected_key_id=key_id, state_path=state, now=now)
    source = getattr(policy_activation, "SignedPolicy", None)
    if source is None and baseline_raw:
        return policy
    if source is None:
        source = SimpleNamespace
    return source(envelope=envelope, public_key_path=public,
                                         expected_audience=policy["audience"], expected_key_id=key_id,
                                         state_path=state)

def _enrolled(*, binding, operation_id, policy=None):
    return True


def _retained(*, binding, operation_id, policy=None):
    return True


def _denied_enrolled(*, binding, operation_id, policy=None):
    return False


def _denied_retained(*, binding, operation_id, policy=None):
    return False


class PredicateTests(unittest.TestCase):

    def _binding_and_policy(self, td):
        exe = Path(td) / "tool.py"
        ctr = Path(td) / "counter"
        impl = _write_fixture(exe, str(ctr))
        manifest = _manifest(impl)
        binding = _bind(manifest, exe)
        policy = _commit_policy(td, _policy())
        receipt = create_fixture_receipt_for_binding(
            binding, "record", conformance_kit_sha256=_KIT, registry_generation=_REGISTRY_GEN, now=_NOW, valid_for_seconds=3600
        )
        return exe, ctr, binding, policy, receipt

    def test_predicate_true_only_with_all_fixture_prerequisites(self):
        with tempfile.TemporaryDirectory() as td:
            _exe, _ctr, binding, policy, receipt = self._binding_and_policy(td)
            decision = evaluate_default_on(
                binding=binding,
                operation_id="record",
                policy=policy,
                receipt=receipt,
                enrolment_check=_enrolled,
                retention_check=_retained,
                now=_NOW,
            )
            self.assertTrue(decision.allowed, decision.reason)
            self.assertEqual(decision.reason, "allowed")
            self.assertTrue(
                is_live_enabled(
                    binding=binding,
                    operation_id="record",
                    policy=policy,
                    receipt=receipt,
                    enrolment_check=_enrolled,
                    retention_check=_retained,
                    now=_NOW,
                )
            )

    def test_stale_receipt_denies(self):
        with tempfile.TemporaryDirectory() as td:
            _exe, _ctr, binding, policy, receipt = self._binding_and_policy(td)
            # Receipt issued at _NOW with 3600s validity is stale at _STALE_NOW.
            decision = evaluate_default_on(
                binding=binding,
                operation_id="record",
                policy=policy,
                receipt=receipt,
                enrolment_check=_enrolled,
                retention_check=_retained,
                now=_STALE_NOW,
            )
            self.assertFalse(decision.allowed)
            self.assertIn("expired", decision.reason)
            # Also with a receipt explicitly expired by valid_for_seconds=0.
            stale = create_fixture_receipt_for_binding(
                binding, "record", conformance_kit_sha256=_KIT, registry_generation=_REGISTRY_GEN, now=_NOW, valid_for_seconds=0
            )
            # valid_for_seconds=0 yields expires == issued, which is not > issued, so our factory still
            # creates expires == issued; predicate denies as expired/not yet valid.
            decision2 = evaluate_default_on(
                binding=binding,
                operation_id="record",
                policy=policy,
                receipt=stale,
                enrolment_check=_enrolled,
                retention_check=_retained,
                now=_NOW,
            )
            self.assertFalse(decision2.allowed)

    def test_missing_enrolment_denies(self):
        with tempfile.TemporaryDirectory() as td:
            _exe, _ctr, binding, policy, receipt = self._binding_and_policy(td)
            # Default seam denies.
            self.assertFalse(main_enrolment_available(binding=binding, operation_id="record", policy=policy))
            decision = evaluate_default_on(
                binding=binding,
                operation_id="record",
                policy=policy,
                receipt=receipt,
                enrolment_check=_denied_enrolled,
                retention_check=_retained,
                now=_NOW,
            )
            self.assertFalse(decision.allowed)
            self.assertIn("main_not_enrolled", decision.reason)
            # None enrolment also denies.
            decision2 = evaluate_default_on(
                binding=binding,
                operation_id="record",
                policy=policy,
                receipt=receipt,
                enrolment_check=None,
                retention_check=_retained,
                now=_NOW,
            )
            self.assertFalse(decision2.allowed)
            self.assertIn("main_not_enrolled", decision2.reason)

    def test_missing_retention_denies(self):
        with tempfile.TemporaryDirectory() as td:
            _exe, _ctr, binding, policy, receipt = self._binding_and_policy(td)
            self.assertFalse(tools_retention_available(binding=binding, operation_id="record", policy=policy))
            decision = evaluate_default_on(
                binding=binding,
                operation_id="record",
                policy=policy,
                receipt=receipt,
                enrolment_check=_enrolled,
                retention_check=_denied_retained,
                now=_NOW,
            )
            self.assertFalse(decision.allowed)
            self.assertIn("tools_retention_unavailable", decision.reason)
            decision2 = evaluate_default_on(
                binding=binding,
                operation_id="record",
                policy=policy,
                receipt=receipt,
                enrolment_check=_enrolled,
                retention_check=None,
                now=_NOW,
            )
            self.assertFalse(decision2.allowed)
            self.assertIn("tools_retention_unavailable", decision2.reason)

    def test_receipt_mismatch_denies(self):
        with tempfile.TemporaryDirectory() as td:
            _exe, _ctr, binding, policy, receipt = self._binding_and_policy(td)
            # Receipt for a different operation.
            other = create_fixture_receipt_for_binding(
                binding, "replay", conformance_kit_sha256=_KIT, registry_generation=_REGISTRY_GEN, now=_NOW, valid_for_seconds=3600
            )
            decision = evaluate_default_on(
                binding=binding,
                operation_id="record",
                policy=policy,
                receipt=other,
                enrolment_check=_enrolled,
                retention_check=_retained,
                now=_NOW,
            )
            self.assertFalse(decision.allowed)
            self.assertIn("mismatch", decision.reason)
            # Receipt for a different implementation (mismatched fixture cannot qualify installer).
            with tempfile.TemporaryDirectory() as td2:
                exe2 = Path(td2) / "other.py"
                ctr2 = Path(td2) / "counter2"
                impl2 = _write_fixture(exe2, str(ctr2))
                binding2 = _bind(_manifest(impl2), exe2)
                wrong_receipt = create_fixture_receipt_for_binding(
                    binding2, "record", conformance_kit_sha256=_KIT, registry_generation=_REGISTRY_GEN, now=_NOW, valid_for_seconds=3600
                )
                decision2 = evaluate_default_on(
                    binding=binding,
                    operation_id="record",
                    policy=policy,
                    receipt=wrong_receipt,
                    enrolment_check=_enrolled,
                    retention_check=_retained,
                    now=_NOW,
                )
                self.assertFalse(decision2.allowed)
                self.assertIn("mismatch", decision2.reason)

    def test_policy_scope_not_covered_denies(self):
        with tempfile.TemporaryDirectory() as td:
            _exe, _ctr, binding, _policy_ok, receipt = self._binding_and_policy(td)
            policy = _commit_policy(td, _policy(slot_id="other-slot"))
            decision = evaluate_default_on(
                binding=binding,
                operation_id="record",
                policy=policy,
                receipt=receipt,
                enrolment_check=_enrolled,
                retention_check=_retained,
                now=_NOW,
            )
            self.assertFalse(decision.allowed)
            self.assertIn("scope_not_covered", decision.reason)

    def test_revocation_denies(self):
        with tempfile.TemporaryDirectory() as td:
            _exe, _ctr, binding, _ok, receipt = self._binding_and_policy(td)
            pol = _policy()
            pol["revocation"]["slot_ids"] = ["fixture-slot"]
            decision = evaluate_default_on(
                binding=binding,
                operation_id="record",
                policy=_commit_policy(td, pol),
                receipt=receipt,
                enrolment_check=_enrolled,
                retention_check=_retained,
                now=_NOW,
            )
            self.assertFalse(decision.allowed)
            self.assertIn("revoked", decision.reason)

    def test_restriction_denies(self):
        with tempfile.TemporaryDirectory() as td:
            _exe, _ctr, binding, _ok, receipt = self._binding_and_policy(td)
            pol = _policy()
            pol["restrictions"] = [{"kind": "disable", "target": "fixture-slot"}]
            decision = evaluate_default_on(
                binding=binding,
                operation_id="record",
                policy=_commit_policy(td, pol),
                receipt=receipt,
                enrolment_check=_enrolled,
                retention_check=_retained,
                now=_NOW,
            )
            self.assertFalse(decision.allowed)
            self.assertIn("restricted", decision.reason)

    def test_truthy_non_true_seams_deny(self):
        with tempfile.TemporaryDirectory() as td:
            _exe, _ctr, binding, policy, receipt = self._binding_and_policy(td)
            def _truthy_enrolled(*, binding, operation_id, policy=None):
                return 1

            def _truthy_retained(*, binding, operation_id, policy=None):
                return "true"

            decision = evaluate_default_on(
                binding=binding,
                operation_id="record",
                policy=policy,
                receipt=receipt,
                enrolment_check=_truthy_enrolled,
                retention_check=_retained,
                now=_NOW,
            )
            self.assertFalse(decision.allowed)
            self.assertIn("main_not_enrolled", decision.reason)
            decision2 = evaluate_default_on(
                binding=binding,
                operation_id="record",
                policy=policy,
                receipt=receipt,
                enrolment_check=_enrolled,
                retention_check=_truthy_retained,
                now=_NOW,
            )
            self.assertFalse(decision2.allowed)
            self.assertIn("tools_retention_unavailable", decision2.reason)

    def test_binding_identity_helper(self):
        with tempfile.TemporaryDirectory() as td:
            _exe, _ctr, binding, _policy_ok, _receipt = self._binding_and_policy(td)
            ident = binding_conformance_identity(binding)
            self.assertEqual(ident["slot_id"], "fixture-slot")
            self.assertEqual(ident["operation_id"], "record")
            self.assertEqual(ident["implementation_sha256"], binding.implementation_sha256)
            self.assertEqual(ident["manifest_digest"], binding.manifest_digest)

    def test_verify_conformance_for_binding_helper(self):
        with tempfile.TemporaryDirectory() as td:
            _exe, _ctr, binding, policy, receipt = self._binding_and_policy(td)
            self.assertTrue(
                verify_conformance_for_binding(
                    binding, "record", policy=policy, receipt=receipt, enrolment_check=_enrolled, retention_check=_retained, now=_NOW
                )
            )
            self.assertFalse(
                verify_conformance_for_binding(
                    binding, "record", policy=policy, receipt=receipt, enrolment_check=_denied_enrolled, retention_check=_retained, now=_NOW
                )
            )


class LiveGateIntegrationTests(unittest.TestCase):

    def _fixture(self, td):
        exe = Path(td) / "tool.py"
        ctr = Path(td) / "counter"
        impl = _write_fixture(exe, str(ctr))
        manifest = _manifest(impl)
        binding = _bind(manifest, exe)
        return exe, ctr, manifest, binding

    def test_authorize_live_with_conformance_allows_only_when_all_prerequisites_present(self):
        with tempfile.TemporaryDirectory() as td:
            exe, ctr, _m, binding = self._fixture(td)
            # Do NOT register fixture: must go through conformance authority.
            policy = _commit_policy(td, _policy())
            receipt = create_fixture_receipt_for_binding(
                binding, "record", conformance_kit_sha256=_KIT, registry_generation=_REGISTRY_GEN, now=_NOW, valid_for_seconds=3600
            )
            authority = make_conformance_authority(
                policy=policy, receipts=[receipt], enrolment_check=_enrolled, retention_check=_retained, now=_NOW
            )
            self.assertTrue(
                authorize_live(binding, exe, live_authority=authority, operation_id="record", request_id="req-conf-0001", attempt_id="att-conf-0001")
            )

    def test_authorize_live_stale_receipt_parks(self):
        with tempfile.TemporaryDirectory() as td:
            exe, _ctr, _m, binding = self._fixture(td)
            policy = _commit_policy(td, _policy())
            receipt = create_fixture_receipt_for_binding(
                binding, "record", conformance_kit_sha256=_KIT, registry_generation=_REGISTRY_GEN, now=_NOW, valid_for_seconds=3600
            )
            authority = make_conformance_authority(
                policy=policy, receipts=[receipt], enrolment_check=_enrolled, retention_check=_retained, now=_STALE_NOW
            )
            self.assertFalse(
                authorize_live(binding, exe, live_authority=authority, operation_id="record", request_id="req-conf-stale-0001", attempt_id="att-conf-stale-0001")
            )
            self.assertIn("receipt_expired", authority.last_reason)


    def test_authorize_live_missing_enrolment_parks(self):
        with tempfile.TemporaryDirectory() as td:
            exe, _ctr, _m, binding = self._fixture(td)
            policy = _commit_policy(td, _policy())
            receipt = create_fixture_receipt_for_binding(
                binding, "record", conformance_kit_sha256=_KIT, registry_generation=_REGISTRY_GEN, now=_NOW, valid_for_seconds=3600
            )
            authority = make_conformance_authority(
                policy=policy, receipts=[receipt], enrolment_check=_denied_enrolled, retention_check=_retained, now=_NOW
            )
            self.assertFalse(
                authorize_live(binding, exe, live_authority=authority, operation_id="record", request_id="req-conf-enr-0001", attempt_id="att-conf-enr-0001")
            )
            self.assertIn("main_not_enrolled", authority.last_reason)


    def test_authorize_live_missing_retention_parks(self):
        with tempfile.TemporaryDirectory() as td:
            exe, _ctr, _m, binding = self._fixture(td)
            policy = _commit_policy(td, _policy())
            receipt = create_fixture_receipt_for_binding(
                binding, "record", conformance_kit_sha256=_KIT, registry_generation=_REGISTRY_GEN, now=_NOW, valid_for_seconds=3600
            )
            authority = make_conformance_authority(
                policy=policy, receipts=[receipt], enrolment_check=_enrolled, retention_check=_denied_retained, now=_NOW
            )
            self.assertFalse(
                authorize_live(binding, exe, live_authority=authority, operation_id="record", request_id="req-conf-ret-0001", attempt_id="att-conf-ret-0001")
            )
            self.assertIn("tools_retention_unavailable", authority.last_reason)


    def test_run_bound_with_conformance_parks_when_prerequisite_absent(self):
        with tempfile.TemporaryDirectory() as td:
            exe, ctr, _m, binding = self._fixture(td)
            policy = _commit_policy(td, _policy())
            # No receipt at all: deny.
            authority = make_conformance_authority(
                policy=policy, receipts=[], enrolment_check=_enrolled, retention_check=_retained, now=_NOW
            )
            w = run_bound(
                binding, [], cwd=td, request_id="req-conf-run-0001", attempt_id="att-conf-run-0001", shared_receipt_ref="shared:fix", live_authority=authority
            )
            self.assertEqual(w["receipt"]["attempt"]["control_outcome"], LIVE_EXECUTION_PARKED)
            self.assertIs(w["evidence_accepted"], False)
            self.assertIsNone(w["pid"])
            self.assertFalse(ctr.exists())
            self.assertEqual(authority.last_reason, "receipt_missing")

    def test_run_bound_with_conformance_allows_when_qualified(self):
        with tempfile.TemporaryDirectory() as td:
            exe, ctr, _m, binding = self._fixture(td)
            policy = _commit_policy(td, _policy())
            receipt = create_fixture_receipt_for_binding(
                binding, "record", conformance_kit_sha256=_KIT, registry_generation=_REGISTRY_GEN, now=_NOW, valid_for_seconds=3600
            )
            authority = make_conformance_authority(
                policy=policy, receipts=[receipt], enrolment_check=_enrolled, retention_check=_retained, now=_NOW
            )
            w = run_bound(
                binding, [], cwd=td, request_id="req-conf-run-ok-0001", attempt_id="att-conf-run-ok-0001", shared_receipt_ref="shared:fix", live_authority=authority,
                attempt_store=lambda _record: True
            )
            self.assertNotEqual(w["receipt"]["attempt"].get("control_outcome"), LIVE_EXECUTION_PARKED)
            self.assertIsNotNone(w["pid"])
            self.assertTrue(ctr.exists())

    def test_conformance_authority_verify_is_request_bound_and_strict(self):
        with tempfile.TemporaryDirectory() as td:
            _exe, _ctr, _m, binding = self._fixture(td)
            policy = _commit_policy(td, _policy())
            receipt = create_fixture_receipt_for_binding(
                binding, "record", conformance_kit_sha256=_KIT, registry_generation=_REGISTRY_GEN, now=_NOW, valid_for_seconds=3600
            )
            store = ReceiptStore()
            store.put(receipt)
            authority = ConformanceLiveAuthority(
                policy=policy, receipt_store=store, enrolment_check=_enrolled, retention_check=_retained, now=_NOW
            )
            # True for correct operation.
            self.assertTrue(authority.verify(binding=binding, operation_id="record", request_id="req-conf-0002", attempt_id="att-conf-0002"))
            # Different operation (no receipt) denies.
            self.assertFalse(authority.verify(binding=binding, operation_id="replay", request_id="req-conf-0002", attempt_id="att-conf-0002"))
            # Authority is request-bound in the sense that verify receives request_id/attempt_id and does not cache across requests.
            self.assertTrue(authority.verify(binding=binding, operation_id="record", request_id="req-conf-0003", attempt_id="att-conf-0003"))


class ParkConformanceRegressions(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.root = Path(tempdir.name)
        self.exe = self.root / "tool.py"
        self.counter = self.root / "launches"
        self.manifest = _manifest(_write_fixture(self.exe, str(self.counter)))
        self.binding = _bind(self.manifest, self.exe)
        self.receipt = create_fixture_receipt_for_binding(
            self.binding, "record", conformance_kit_sha256=_KIT,
            registry_generation=_REGISTRY_GEN, now=_NOW, valid_for_seconds=7200,
        )

    def _evaluate(self, policy, *, binding=None, **options):
        return evaluate_default_on(
            binding=self.binding if binding is None else binding, operation_id="record",
            policy=policy, receipt=self.receipt, enrolment_check=_enrolled,
            retention_check=_retained, now=_NOW, **options,
        )

    def test_unsigned_and_expired_policy_cannot_authorize(self):
        good = _commit_policy(self.root, _policy(), baseline_raw=True)
        decision = self._evaluate(good)
        self.assertTrue(decision.allowed, decision.reason)
        for policy in (_policy(), _policy(expires_at="2026-05-30T00:00:00Z"),
                       _policy(not_before="2026-06-02T00:00:00Z")):
            with self.subTest(policy=policy):
                self.assertFalse(self._evaluate(policy).allowed)
        # Commit an otherwise valid, signed policy before its expiry, then
        # use its expired envelope with a receipt which is still fresh.
        earlier = _NOW - datetime.timedelta(days=2)
        expired = _commit_policy(
            self.root, _policy(expires_at="2026-05-31T00:00:00Z"),
            now=earlier, baseline_raw=True,
        )
        self.assertFalse(self._evaluate(expired).allowed)

    def test_activation_clock_rollback_denies_with_fresh_receipt(self):
        future = _NOW + datetime.timedelta(minutes=1)
        signed = _commit_policy(self.root, _policy(), now=future, baseline_raw=True)
        kwargs = {}
        if isinstance(signed, dict):
            kwargs["activation_state"] = {
                "generation": signed["generation"], "not_before": signed["not_before"],
                "expires_at": signed["expires_at"], "max_clock": "2026-06-01T00:01:00Z",
            }
        denied = self._evaluate(signed, **kwargs)
        self.assertFalse(denied.allowed)
        self.assertIn("clock_rollback", denied.reason)

    def test_assert_active_rejects_disagreement_with_committed_state(self):
        initial = _policy()
        initial["revocation"]["revoked_generations"] = [2]
        signed = _commit_policy(self.root, initial)
        later = _policy(generation=3)
        later["key_id"] = signed.expected_key_id
        key_dir = signed.public_key_path.parent
        later_envelope = _make_envelope(key_dir / "ed-priv.pem", later, str(key_dir), name="later")
        committed = policy_activation.activate(
            later_envelope, public_key_path=signed.public_key_path,
            expected_audience=signed.expected_audience, expected_key_id=signed.expected_key_id,
            state_path=signed.state_path, now=_NOW,
        )
        self.assertEqual(committed["revoked_generations"], [2])
        self.assertEqual(policy_activation.assert_active(
            later_envelope, public_key_path=signed.public_key_path,
            expected_audience=signed.expected_audience, expected_key_id=signed.expected_key_id,
            state_path=signed.state_path, now=_NOW,
        ), committed)

        state = signed.state_path
        original = json.loads(state.read_bytes())
        for name, value in (
            ("key_id", "f" * 64), ("audience", "another-host"),
            ("revoked_generations", [3]), ("expires_at", "2026-12-31T00:00:00Z"),
        ):
            with self.subTest(name=name):
                state.write_text(json.dumps(dict(original, **{name: value})), encoding="utf-8")
                with self.assertRaises(policy_activation.PolicyActivationRejected):
                    policy_activation.assert_active(
                        later_envelope, public_key_path=signed.public_key_path,
                        expected_audience=signed.expected_audience, expected_key_id=signed.expected_key_id,
                        state_path=state, now=_NOW,
                    )

    def test_receipt_and_scope_require_all_binding_pins(self):
        self.assertTrue(receipt_matches_binding(self.receipt, self.binding, "record"))
        for key, value in (("manifest_digest", None), ("provider_revision", ""),
                           ("adapter_sha256", None)):
            with self.subTest(pin=key):
                missing = dataclasses.replace(self.binding, **{key: value})
                self.assertFalse(receipt_matches_binding(self.receipt, missing, "record"))
        policy = _policy()
        policy["scope"][0].update(manifest_sha256=self.binding.manifest_digest,
                                  provider_revision=self.binding.provider_revision,
                                  adapter_sha256=self.binding.adapter_sha256)
        self.assertTrue(policy_covers(policy, self.binding, "record")[0])
        for key in ("manifest_digest", "provider_revision", "adapter_sha256"):
            with self.subTest(scope_pin=key):
                self.assertFalse(policy_covers(
                    policy, dataclasses.replace(self.binding, **{key: None}), "record"
                )[0])

    def test_signed_baseline_checks_each_execution_bound(self):
        binding = self.binding
        if not hasattr(binding, "shared_schema_sha256"):
            binding = SimpleNamespace(**vars(binding), shared_schema_sha256=_PUBLISHED_SHA)
        for invalid_generation in (True, "1", None):
            with self.subTest(generation=invalid_generation):
                self.assertFalse(policy_covers(_policy(generation=invalid_generation), binding, "record")[0])
        for key, value in (
            ("execution_profile", "offline_fixture"), ("transport_kind", "mcp_stdio"),
            ("operation_binding", {"kind": "mcp_tool"}), ("grants", ()),
            ("perception_optional", ("vision",)), ("shared_schema_ref", "unresolved"),
            ("shared_schema_sha256", "f" * 64),
        ):
            with self.subTest(key=key):
                self.assertTrue(policy_covers(_policy(), binding, "record")[0])
                changed = SimpleNamespace(**(vars(binding) | {key: value}))
                self.assertFalse(policy_covers(_policy(), changed, "record")[0])

    def test_policy_applies_live_restrictions_and_installation_revocation(self):
        binding = SimpleNamespace(**(vars(self.binding) | {
            "installation_ref": "installation:fixture-slot@1",
            "shared_schema_sha256": _PUBLISHED_SHA, "env_allowlist": ("PATH",),
        }))
        for restriction in (
            {"kind": "preview_only"}, {"kind": "require_existing_approval"},
            {"kind": "limit_principal", "subject": "enrolled-principal"},
            {"kind": "limit_timeout", "timeout_s": 1},
            {"kind": "limit_output_bytes", "max_output_bytes": 1},
            {"kind": "remove_grant", "grant_id": "grant.exec.external_process"},
            {"kind": "remove_env", "env_name": "PATH"},
            {"kind": "shorten_qualification_validity"},
        ):
            with self.subTest(restriction=restriction):
                policy = _policy(restrictions=[dict(restriction, target="fixture-slot")])
                self.assertFalse(policy_covers(policy, binding, "record")[0])
        lower = _policy(restrictions=[{"kind": "lower_perception_ceiling", "target": "record",
                                      "perception_ceiling": "none"}])
        lower["baseline"]["perception_ceiling"] = "vision"
        self.assertTrue(policy_covers(lower, binding, "record")[0])
        visible = SimpleNamespace(**(vars(binding) | {"perception_optional": ("vision",)}))
        self.assertFalse(policy_covers(lower, visible, "record")[0])
        revoked = _policy()
        revoked["revocation"]["installation_refs"] = [binding.installation_ref]
        self.assertFalse(policy_covers(revoked, binding, "record")[0])
        revoked["revocation"]["installation_refs"] = ["installation:another-slot@1"]
        self.assertTrue(policy_covers(revoked, binding, "record")[0])
        missing = SimpleNamespace(**(vars(binding) | {"installation_ref": None}))
        self.assertFalse(policy_covers(revoked, missing, "record")[0])

    def test_conformance_requires_confirmed_durable_start_before_launch(self):
        authority = make_conformance_authority(
            policy=_commit_policy(self.root, _policy(), baseline_raw=True), receipts=[self.receipt],
            enrolment_check=_enrolled, retention_check=_retained, now=_NOW,
        )
        for store, code in ((None, LIVE_EXECUTION_PARKED), (lambda _: False, "UNCERTAIN_OUTCOME")):
            with self.subTest(code=code):
                result = run_bound(self.binding, [], request_id="req-no-start-1",
                                   attempt_id="att-no-start-1", live_authority=authority, attempt_store=store)
                self.assertEqual(result["receipt"]["attempt"].get("control_outcome"), code)
                self.assertIsNone(result["pid"])
                self.assertFalse(self.counter.exists())

    def test_spawn_refuses_while_activation_boundary_is_locked(self):
        import fcntl
        import os
        from unittest.mock import patch
        from slot_host.runtime import executable_custody

        payload = _policy()
        signed = _commit_policy(self.root, payload)
        policy = signed if hasattr(policy_activation, "SignedPolicy") else payload
        authority = make_conformance_authority(
            policy=policy, receipts=[self.receipt], enrolment_check=_enrolled,
            retention_check=_retained, now=_NOW,
        )
        lock = signed.state_path.with_name(f".{signed.state_path.name}.lock")
        fd = os.open(lock, os.O_RDONLY | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        real_stage = executable_custody.stage_verified_executable

        def stage_then_lock(*args, **kwargs):
            staged = real_stage(*args, **kwargs)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return staged

        retained = []
        def start(record):
            retained.append(record)
            return True

        with patch.object(executable_custody, "stage_verified_executable", side_effect=stage_then_lock):
            result = run_bound(self.binding, [], request_id="req-lock-001", attempt_id="att-lock-001",
                               live_authority=authority, attempt_store=start)
        self.assertTrue(retained)
        self.assertEqual(result["receipt"]["attempt"].get("control_outcome"), LIVE_EXECUTION_PARKED)
        self.assertIsNone(result["pid"])
        self.assertFalse(self.counter.exists())

    def test_cached_conformance_reply_denies_after_expiry_or_revocation(self):
        from slot_host.runtime.tests.test_live_park import _policy_for, _uid
        from slot_host.runtime.tools import _ControlError, dispatch

        ref = "installation:fixture-slot@1"
        host = {ref: (self.manifest, str(self.exe))}
        for cause in ("expiry", "revocation", "missing MAIN", "missing Tools"):
            with self.subTest(cause=cause):
                payload = _policy()
                signed = _commit_policy(self.root, payload, baseline_raw=True)
                authority = make_conformance_authority(
                    policy=signed, receipts=[self.receipt], enrolment_check=_enrolled,
                    retention_check=_retained, now=_NOW,
                )
                arguments = {"request_id": _uid("req-conformance-cached"), "payload": {
                    "installation_ref": ref, "argv": [],
                }}
                options = {"host": host, "host_policy": _policy_for(self.manifest), "live_authority": authority}
                if hasattr(policy_activation, "SignedPolicy"):
                    options["attempt_store"] = lambda _: True
                self.assertTrue(dispatch("behaviour.record", arguments, **options)["ok"])
                launches = self.counter.read_text()
                if cause == "expiry":
                    authority._now = self.receipt.expires_at
                elif cause == "missing MAIN":
                    authority._enrolment_check = _denied_enrolled
                elif cause == "missing Tools":
                    authority._retention_check = _denied_retained
                elif isinstance(signed, dict):
                    signed["revocation"]["slot_ids"].append(self.binding.slot_id)
                else:
                    payload.update(key_id=signed.expected_key_id, generation=2)
                    payload["revocation"]["slot_ids"].append(self.binding.slot_id)
                    key_dir = signed.public_key_path.parent
                    envelope = _make_envelope(key_dir / "ed-priv.pem", payload, str(key_dir), name="revoked")
                    policy_activation.activate(
                        envelope, public_key_path=signed.public_key_path,
                        expected_audience=signed.expected_audience, expected_key_id=signed.expected_key_id,
                        state_path=signed.state_path, now=_NOW,
                    )
                with self.assertRaises(_ControlError) as caught:
                    dispatch("behaviour.record", arguments, **options)
                self.assertEqual(caught.exception.code, "REQUEST_ID_CONFLICT")
                self.assertEqual(self.counter.read_text(), launches)


if __name__ == "__main__":
    unittest.main()
