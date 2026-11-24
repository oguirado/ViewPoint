# SPDX-License-Identifier: AGPL-3.0-only
"""Regressions for triage_runtime fix_list (binding.py lane).

Every test fails on the pre-fix base and passes after the fix.
Authenticated positives supply the complete host record set so that
exactly one varied factor drives the refusal.
"""

import hashlib
import stat
import tempfile
import unittest
from pathlib import Path

from slot_host.runtime.binding import BindingRejected, sha256_of_file
from slot_host.projections.schema import digest as jcs_digest
from slot_host.runtime.tests.binding_fixtures import (
    fixture_retention,
    UNRESOLVED_EVIDENCE_REF,
    probe_witness,
    verified_fixture_binding as verify_binding,
)

setUpModule = fixture_retention

_ADAPTER = "d" * 64
_PROVIDER_SHA = "a" * 64
_REQ_DIGEST = "b" * 64
_RES_DIGEST = "c" * 64
_PUBLISHED_REF = "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2"
_PUBLISHED_ID = "https://metaviewer.local/contracts/v0.2/attempt_evidence_record.schema.json"


def _write(path: Path, body: bytes) -> str:
    path.write_bytes(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return sha256_of_file(path)


def _manifest(impl_sha: str, *, profile="external_process", status="registered", shared_ref=None, licence_id="MIT", upstream_name="fixture") -> dict:
    shared_ref = shared_ref or _PUBLISHED_REF
    def _op(oid, role):
        b = {"kind": "cli_subcommand", "argv": [oid], "typed_args": []}
        return {
            "id": oid,
            "role": role,
            "input_ref": f"urn:rea.slot_request:v2#slot={role}",
            "output_ref": f"urn:rea.slot_result:v2#slot={role}",
            "binding": b,
            "required_effects": ["process.spawn:child"],
            "required_grants": ["grant.exec.external_process"],
            "limits": {"timeout_s": 5, "max_output_bytes": 4096},
            "retry_semantics": {"mode": "at_most_once", "on_interrupt": "UNCERTAIN_OUTCOME"},
        }
    ops = [_op("record", "replay"), _op("replay", "replay"), _op("compare", "verify")]
    return {
        "schema": "rea.slot_manifest/v2",
        "slot_id": "fixture-slot",
        "status": status,
        "upstream": {
            "name": upstream_name,
            "source_url": "https://example.com/fixture",
            "version": "1.0.0",
            "licence_id": licence_id,
            "licence_evidence_refs": ["fixture: licence"],
            "component_notices": [],
            "user_installed": True,
        },
        "provider_ref": {
            "schema": "rea.provider/v2",
            "provider_id": "fixture-slot",
            "provider_revision": "1.0.0",
            "provider_sha256": _PROVIDER_SHA,
            "implementation_sha256": impl_sha,
        },
        "contract_refs": [
            {
                "role": "replay",
                "request_schema_ref": "urn:rea.slot_request:v2",
                "request_digest": _REQ_DIGEST,
                "result_schema_ref": "urn:rea.slot_result:v2",
                "result_digest": _RES_DIGEST,
                "execution_profile": profile,
            },
            {
                "role": "verify",
                "request_schema_ref": "urn:rea.slot_request:v2",
                "request_digest": _REQ_DIGEST,
                "result_schema_ref": "urn:rea.slot_result:v2",
                "result_digest": _RES_DIGEST,
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
        "perception": {"required_level": "none", "optional_levels": [], "observation_sources": [{"id": "tool-evidence", "level": "none", "producer": "tool_stdio", "outside_capsule": True}]},
        "record_replay": {"input_codec": "json", "output_codec": "json", "observable_channels": ["error"], "state_domain": "per_case", "effects": ["process.spawn:child"], "determinism_modes": ["recorded_inputs"], "freshness_inputs": ["fixture"]},
        "comparison": {"comparator_id": "fixture.compare", "version": "1", "policy_ref": "policy:fixture@1", "coverage_requirements": {"channel": "error"}, "tolerance_units": "none", "waiver_policy": {"source": "host_policy", "binds": ["input", "actual", "reason", "scope", "policy_version"], "stale": "fail"}},
        "evidence": {"shared_schema_ref": shared_ref, "required_rea_fields": ["episode", "observation", "slot"], "artifact_roles": ["evidence_bundle"], "retention": "evidence_store_attempt", "visibility": "redacted_projection"},
        "conformance": {"fixture_refs": ["fixture:1"], "negative_control_refs": ["control:1"], "supported_entry_points": ["cli", "mcp", "json"], "required_checks": ["admit-and-project", "outside-record-replay-compare", "perception-and-pool", "transport-parity-and-retry"]},
        "discovery": {"summary": "fixture", "search_terms": ["fixture"], "operation_descriptions": {"record": "record", "replay": "replay", "compare": "compare"}, "examples": []},
    }


def _registry_for(manifest):
    contract = next(r for r in manifest["contract_refs"] if r["role"] == "replay")
    provider = manifest["provider_ref"]
    adapter = manifest["capabilities"]["adapter_identity"]["adapter_sha256"]
    return {
        "provider_sha256": provider["provider_sha256"],
        "provider_revision": provider["provider_revision"],
        "implementation_sha256": provider["implementation_sha256"],
        "adapter_sha256": adapter,
        "request_digest": contract["request_digest"],
        "result_digest": contract["result_digest"],
        "request_schema_ref": contract["request_schema_ref"],
        "result_schema_ref": contract["result_schema_ref"],
        "execution_profile": contract["execution_profile"],
    }


def _auth(manifest, *, registry=True, probes=None):
    # probes=None means declare no probes; pass explicit dict for probe cases.
    kwargs = {
        "auth_grants": {"grant.exec.external_process"},
        "auth_spawn_granted": True,
        "auth_licence_disposition": "external_execution",
        "auth_evidence_ref": _PUBLISHED_REF,
        "auth_registry": _registry_for(manifest) if registry else None,
        "auth_probes": probes,
        "auth_manifest_digest": jcs_digest(manifest),
        "auth_slot_id": "fixture-slot",
    }
    # registry=False withholds the registry entirely (authenticated absence).
    # To simulate "supplied but incomplete", callers pass registry dicts
    # directly; absence uses auth_registry=None via explicit override.
    return kwargs


class FixBinding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.exe = self.dir / "fixture.py"
        body = (
            b"#!/usr/bin/env python3\n"
            b"# SPDX-License-Identifier: AGPL-3.0-only\n"
            b"import sys, json\n"
            b"obj={\"schema\":\"rea.slot_result/v1\",\"status\":\"OK\",\"rc\":0,\"sha256\":\"e\"+\"f\"*63}\n"
            b"sys.stdout.write(json.dumps(obj)+'\\n')\n"
        )
        self.impl = _write(self.exe, body)
        self.manifest = _manifest(self.impl)

    # Fix 1: JCS digest -------------------------------------------------
    def test_jcs_digest_accepts_noninteger_and_utf16_ordering(self):
        # Manifest object whose Python sorted-JSON digest differs from JCS:
        # 1.0 normalizes to 1, -0.0 to 0, and keys sort by UTF-16.
        probe = dict(self.manifest)
        # Embed the discriminating values in discovery examples.
        probe["discovery"] = dict(probe["discovery"])
        probe["discovery"]["examples"] = [{"arg": 1.0, "negzero": -0.0, "z": 1}]
        # A key pair whose Python order differs from UTF-16 order.
        probe["discovery"]["examples"] = [{"a": 1.0, "B": 2, "z": 3}]
        independent = jcs_digest(probe)
        legacy = hashlib.sha256(
            __import__("json").dumps(probe, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        ).hexdigest()
        self.assertNotEqual(independent, legacy)
        # Authenticated binding with the independent JCS pin must admit.
        b = verify_binding(probe, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.external_process"},
                           auth_grants={"grant.exec.external_process"},
                           auth_spawn_granted=True,
                           auth_licence_disposition="external_execution",
                           auth_evidence_ref=_PUBLISHED_REF,
                           auth_registry=_registry_for(probe),
                           auth_probes={},
                           auth_manifest_digest=independent,
                           auth_slot_id="fixture-slot")
        self.assertEqual(b.manifest_digest, independent)
        # A one-field substitution must refuse (no pin inheritance).
        probe2 = dict(probe)
        probe2["upstream"] = dict(probe2["upstream"], version="1.0.1")
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(probe2, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.external_process"},
                           auth_grants={"grant.exec.external_process"},
                           auth_spawn_granted=True,
                           auth_licence_disposition="external_execution",
                           auth_evidence_ref=_PUBLISHED_REF,
                           auth_registry=_registry_for(probe2),
                           auth_probes={},
                           auth_manifest_digest=independent,
                           auth_slot_id="fixture-slot")
        self.assertEqual(ctx.exception.reason, "manifest_invalid")

    # Fix 2: grants + spawn --------------------------------------------
    def test_empty_required_grants_cannot_authorize_external(self):
        m = _manifest(self.impl)
        m["operations"][0]["required_grants"] = []
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(m, str(self.exe), operation_id="record",
                           host_grants=set(), auth_grants=set(),
                           auth_spawn_granted=True,
                           auth_licence_disposition="external_execution",
                           auth_evidence_ref=_PUBLISHED_REF,
                           auth_registry=_registry_for(m),
                           auth_probes={})
        self.assertEqual(ctx.exception.reason, "grant_refused")

    def test_undeclared_external_grant_still_required(self):
        m = _manifest(self.impl)
        m["operations"][0]["required_grants"] = []
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(m, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.other"}, auth_grants={"grant.exec.other"},
                           auth_spawn_granted=True,
                           auth_licence_disposition="external_execution",
                           auth_evidence_ref=_PUBLISHED_REF,
                           auth_registry=_registry_for(m),
                           auth_probes={})
        self.assertEqual(ctx.exception.reason, "grant_refused")

    def test_spawn_requires_authenticated_permission(self):
        # Complete positive admits.
        b = verify_binding(self.manifest, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.external_process"},
                           auth_grants={"grant.exec.external_process"},
                           auth_spawn_granted=True,
                           auth_licence_disposition="external_execution",
                           auth_evidence_ref=_PUBLISHED_REF,
                           auth_registry=_registry_for(self.manifest),
                           auth_probes={})
        self.assertEqual(b.execution_profile, "external_process")
        # Declared spawn without authenticated permission refuses.
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(self.manifest, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.external_process"},
                           auth_grants={"grant.exec.external_process"},
                           auth_spawn_granted=False,
                           auth_licence_disposition="external_execution",
                           auth_evidence_ref=_PUBLISHED_REF,
                           auth_registry=_registry_for(self.manifest),
                           auth_probes={})
        self.assertEqual(ctx.exception.reason, "grant_refused")
        # Authenticated None (withheld permission) also refuses.
        with self.assertRaises(BindingRejected) as ctx2:
            verify_binding(self.manifest, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.external_process"},
                           auth_grants={"grant.exec.external_process"},
                           auth_spawn_granted=None,
                           auth_licence_disposition="external_execution",
                           auth_evidence_ref=_PUBLISHED_REF,
                           auth_registry=_registry_for(self.manifest),
                           auth_probes={})
        self.assertEqual(ctx2.exception.reason, "grant_refused")

    def test_empty_required_effects_cannot_enable_spawn_even_when_authorized(self):
        m = _manifest(self.impl)
        m["operations"][0]["required_effects"] = []
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(m, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.external_process"},
                           auth_grants={"grant.exec.external_process"},
                           auth_spawn_granted=True,
                           auth_licence_disposition="external_execution",
                           auth_evidence_ref=_PUBLISHED_REF,
                           auth_registry=_registry_for(m),
                           auth_probes={})
        self.assertIn(ctx.exception.reason, ("manifest_invalid", "grant_refused"))

    # Fix 3: provider pins ----------------------------------------------
    def test_provider_revision_null_refused(self):
        m = _manifest(self.impl)
        m["provider_ref"]["provider_revision"] = None
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(m, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.external_process"})
        self.assertIn(ctx.exception.reason, ("manifest_invalid", "profile_not_admitted"))

    def test_provider_v1_with_external_refused(self):
        m = _manifest(self.impl)
        m["provider_ref"]["schema"] = "rea.provider/v1"
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(m, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.external_process"})
        self.assertEqual(ctx.exception.reason, "profile_not_admitted")

    def test_provider_identity_fields_required(self):
        for missing in ("provider_id", "schema", "provider_revision"):
            m = _manifest(self.impl)
            del m["provider_ref"][missing]
            with self.subTest(missing=missing):
                with self.assertRaises(BindingRejected) as ctx:
                    verify_binding(m, str(self.exe), operation_id="record",
                                   host_grants={"grant.exec.external_process"})
                self.assertIn(ctx.exception.reason, ("manifest_invalid", "profile_not_admitted"))

    def test_v1_exchange_with_external_refused(self):
        m = _manifest(self.impl)
        for row in m["contract_refs"]:
            row["request_schema_ref"] = "urn:rea.slot_request:v1"
            row["result_schema_ref"] = "urn:rea.slot_result:v1"
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(m, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.external_process"})
        self.assertEqual(ctx.exception.reason, "profile_not_admitted")

    def test_authenticated_registry_absent_refused(self):
        # Complete authenticated positive admits.
        b = verify_binding(self.manifest, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.external_process"},
                           auth_grants={"grant.exec.external_process"},
                           auth_spawn_granted=True,
                           auth_licence_disposition="external_execution",
                           auth_evidence_ref=_PUBLISHED_REF,
                           auth_registry=_registry_for(self.manifest),
                           auth_probes={})
        self.assertEqual(b.provider_revision, "1.0.0")
        # Withheld authenticated registry refuses.
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(self.manifest, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.external_process"},
                           auth_grants={"grant.exec.external_process"},
                           auth_spawn_granted=True,
                           auth_licence_disposition="external_execution",
                           auth_evidence_ref=_PUBLISHED_REF,
                           auth_registry=None,
                           auth_probes={})
        self.assertEqual(ctx.exception.reason, "profile_not_admitted")
        # One missing registered resource refuses.
        reg = _registry_for(self.manifest)
        del reg["provider_sha256"]
        with self.assertRaises(BindingRejected) as ctx2:
            verify_binding(self.manifest, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.external_process"},
                           auth_grants={"grant.exec.external_process"},
                           auth_spawn_granted=True,
                           auth_licence_disposition="external_execution",
                           auth_evidence_ref=_PUBLISHED_REF,
                           auth_registry=reg,
                           auth_probes={})
        self.assertEqual(ctx2.exception.reason, "profile_not_admitted")

    # Fix 4: licence disposition ----------------------------------------
    def test_compound_fsl_licence_blocks_external(self):
        m = _manifest(self.impl, licence_id="MIT AND FSL-1.1-ALv2", upstream_name="raea-renderer")
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(m, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.external_process"},
                           auth_grants={"grant.exec.external_process"},
                           auth_spawn_granted=True,
                           auth_licence_disposition="external_execution",
                           auth_evidence_ref=_PUBLISHED_REF,
                           auth_registry=_registry_for(m),
                           auth_probes={})
        self.assertEqual(ctx.exception.reason, "availability_blocked")

    def test_authenticated_disposition_absent_blocks_external(self):
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(self.manifest, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.external_process"},
                           auth_grants={"grant.exec.external_process"},
                           auth_spawn_granted=True,
                           auth_licence_disposition=None,
                           auth_evidence_ref=_PUBLISHED_REF,
                           auth_registry=_registry_for(self.manifest),
                           auth_probes={})
        self.assertEqual(ctx.exception.reason, "availability_blocked")

    def test_internal_research_disposition_blocks_external(self):
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(self.manifest, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.external_process"},
                           auth_grants={"grant.exec.external_process"},
                           auth_spawn_granted=True,
                           auth_licence_disposition="internal_research",
                           auth_evidence_ref=_PUBLISHED_REF,
                           auth_registry=_registry_for(self.manifest),
                           auth_probes={})
        self.assertEqual(ctx.exception.reason, "availability_blocked")

    # Fix 5: probes -----------------------------------------------------
    def test_declared_probe_without_authenticated_result_blocks(self):
        m = _manifest(self.impl)
        m["capabilities"]["prerequisite_probes"] = [{"id": "probe-1", "argv": ["--probe"], "effects": ["process.spawn:child"], "requires_binding": True}]
        # Withheld authenticated probe results refuse.
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(m, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.external_process"},
                           auth_grants={"grant.exec.external_process"},
                           auth_spawn_granted=True,
                           auth_licence_disposition="external_execution",
                           auth_evidence_ref=_PUBLISHED_REF,
                           auth_registry=_registry_for(m),
                           auth_probes=None)
        self.assertEqual(ctx.exception.reason, "availability_blocked")
        # Failed result refuses; passed admits (positive contrast).
        with self.assertRaises(BindingRejected) as ctx2:
            verify_binding(m, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.external_process"},
                           auth_grants={"grant.exec.external_process"},
                           auth_spawn_granted=True,
                           auth_licence_disposition="external_execution",
                           auth_evidence_ref=_PUBLISHED_REF,
                           auth_registry=_registry_for(m),
                           auth_probes={"probe-1": "failed"})
        self.assertEqual(ctx2.exception.reason, "availability_blocked")
        b = verify_binding(m, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.external_process"},
                           auth_grants={"grant.exec.external_process"},
                           auth_spawn_granted=True,
                           auth_licence_disposition="external_execution",
                           auth_evidence_ref=_PUBLISHED_REF,
                           auth_registry=_registry_for(m),
                           auth_probes={"probe-1": probe_witness()})
        self.assertEqual(b.execution_profile, "external_process")

    def test_probe_declaration_must_be_list(self):
        m = _manifest(self.impl)
        m["capabilities"]["prerequisite_probes"] = {"id": "probe-1"}
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(m, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.external_process"},
                           auth_grants={"grant.exec.external_process"},
                           auth_spawn_granted=True,
                           auth_licence_disposition="external_execution",
                           auth_evidence_ref=_PUBLISHED_REF,
                           auth_registry=_registry_for(m),
                           auth_probes={})
        self.assertEqual(ctx.exception.reason, "manifest_invalid")

    # Fix 6: live evidence binding --------------------------------------
    def test_authenticated_external_requires_verified_live_evidence(self):
        # Positive: pinned v0.2 establishes the live binding.
        b = verify_binding(self.manifest, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.external_process"},
                           auth_grants={"grant.exec.external_process"},
                           auth_spawn_granted=True,
                           auth_licence_disposition="external_execution",
                           auth_evidence_ref=_PUBLISHED_REF,
                           auth_registry=_registry_for(self.manifest),
                           auth_probes={})
        self.assertEqual(b.shared_schema_ref, _PUBLISHED_REF)
        # Withheld evidence reference refuses.
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(self.manifest, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.external_process"},
                           auth_grants={"grant.exec.external_process"},
                           auth_spawn_granted=True,
                           auth_licence_disposition="external_execution",
                           auth_evidence_ref=None,
                           auth_registry=_registry_for(self.manifest),
                           auth_probes={})
        self.assertEqual(ctx.exception.reason, "profile_not_admitted")
        # Unresolved v1 reference refuses as an authenticated binding.
        with self.assertRaises(BindingRejected) as ctx2:
            verify_binding(self.manifest, str(self.exe), operation_id="record",
                           host_grants={"grant.exec.external_process"},
                           auth_grants={"grant.exec.external_process"},
                           auth_spawn_granted=True,
                           auth_licence_disposition="external_execution",
                           auth_evidence_ref=UNRESOLVED_EVIDENCE_REF,
                           auth_registry=_registry_for(self.manifest),
                           auth_probes={})
        self.assertEqual(ctx2.exception.reason, "profile_not_admitted")


if __name__ == "__main__":
    unittest.main()
