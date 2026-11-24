# SPDX-License-Identifier: AGPL-3.0-only
"""Owner-authority freeze (U01): authority seam, live-park gate, crypto contract.

Covers VERIFY units_plan.now[0] offline rows. Live execution stays parked:
without an authenticated authority the gate denies (fail closed) with zero
spawns. Owner-dependent positives (MAIN enrolment, Tools retention/custody)
are explicit seams and are skipped with reason "owner-pending: <T0x|MAIN>".

Offline rows are real: each fails if the park is removed, if bypass via
environment/manifest/payload/host_grants is reintroduced, if the authority
is honoured as truthy rather than strictly True, or if the OpenSSL
Ed25519 verify contract (fixed argv, shell=False, deadline, bounded
inputs, throwaway keys in the test temp dir only) is violated.
"""
import base64
import hashlib
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from slot_host.runtime import binding as runtime_binding, policy_signature
from slot_host.runtime.binding import BindingRejected, _retention_available, sha256_of_file
from slot_host.runtime.evidence_projection import V03_REF
from slot_host.runtime.live_park import (
    LIVE_EXECUTION_PARKED,
    DenyLiveAuthority,
    authorize_live,
    _DEFAULT_AUTHORITY,
)
from slot_host.runtime.runner import run_bound
from slot_host.runtime.tools import _ControlError, register_installation
from slot_host.runtime.tests.test_live_park import _fixture_capability
from slot_host.runtime.tests.binding_fixtures import (
    fixture_retention,
    FIXTURE_RESULT_SHA256,
    verified_fixture_binding as verify_binding,
    execute_fixture as execute,
    dispatch_fixture as dispatch,
)

from slot_host.runtime.tests.test_policy_signature import (
    _AUDIENCE, _NOW, _derive_key_id, _ed_keys, _payload_bytes, _policy, _sign,
)
from slot_host.runtime.tests.test_v03_projection import FIXTURES, validate

setUpModule = fixture_retention

_ADAPTER = "d" * 64
_PROV = "a" * 64
_REQ = "b" * 64
_RES = "c" * 64

# Unique ids avoid cross-test RequestStore collisions (module-global store).
_SEQ = [0]


def _uid(prefix):
    _SEQ[0] += 1
    return "%s-%d-%d" % (prefix, os.getpid(), _SEQ[0])


def _manifest(impl_sha, profile="external_process"):
    def _op(oid, role):
        return {
            "id": oid,
            "role": role,
            "input_ref": "urn:rea.slot_request:v2#slot=%s" % role,
            "output_ref": "urn:rea.slot_result:v2#slot=%s" % role,
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
            "observation_sources": [{"id": "tool-evidence", "level": "none", "producer": "tool_stdio", "outside_capsule": True}],
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


def _write_fixture(path, counter):
    ctr = repr(counter).encode()
    body = (
        b"#!/usr/bin/env python3\n"
        b"# SPDX-License-Identifier: AGPL-3.0-only\n"
        b"import json, sys\n"
        b"ctr=" + ctr + b"\n"
        b"open(ctr,'a').write('1')\n"
        + (b'obj={"schema":"rea.slot_result/v1","status":"OK","rc":0,"sha256":"%s","result_payload":"offline result"}\n'
           % FIXTURE_RESULT_SHA256.encode())
        +
        b"sys.stdout.write(json.dumps(obj)+'\\n')\n"
        b"sys.stdout.flush()\n"
    )
    path.write_bytes(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return sha256_of_file(path)


def _bind(manifest, exe):
    return verify_binding(
        manifest,
        str(exe),
        operation_id="record",
        host_grants={"grant.exec.external_process"},
        allow_pending_evidence=True,
        requested_profile="external_process",
    )


def _policy_for(manifest):
    from slot_host.projections.schema import digest as _jcs_digest

    contract = next(r for r in manifest["contract_refs"] if r["role"] == "replay")
    provider = manifest["provider_ref"]
    adapter_sha = manifest["capabilities"]["adapter_identity"]["adapter_sha256"]
    return {
        "grants": ["grant.exec.external_process"],
        "registry": {
            "provider_sha256": provider["provider_sha256"],
            "provider_revision": provider["provider_revision"],
            "implementation_sha256": provider["implementation_sha256"],
            "adapter_sha256": adapter_sha,
            "request_digest": contract["request_digest"],
            "result_digest": contract["result_digest"],
            "request_schema_ref": contract["request_schema_ref"],
            "result_schema_ref": contract["result_schema_ref"],
            "execution_profile": contract["execution_profile"],
        },
        "evidence_ref": "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2",
        "spawn_granted": True,
        "licence_disposition": "external_execution",
        "manifest_digest": _jcs_digest(manifest),
        "slot_id": "fixture-slot",
        "probes": {},
        "perception_ceiling": "none",
    }


class _RecordingStub:
    def __init__(self, value=True):
        self.value = value
        self.calls = []

    def verify(self, **kwargs):
        self.calls.append(kwargs)
        return self.value


class _RaisingStub:
    def verify(self, **kwargs):
        raise RuntimeError("authority boom")


def _make_live_dir(name):
    import tempfile as _tf
    d = Path(_tf.mkdtemp(prefix=f".auth-{name}-"))
    return d


class AuthoritySeamTests(unittest.TestCase):
    def test_none_authority_denies_non_fixture(self):
        d = _make_live_dir("seam-none")
        self.addCleanup(shutil.rmtree, d, True)
        exe = d / "tool.py"
        ctr = d / "counter"
        impl = _write_fixture(exe, str(ctr))
        binding = _bind(_manifest(impl), exe)
        self.assertFalse(
            authorize_live(binding, exe, live_authority=None, operation_id="record", request_id="req-seam-0001", attempt_id="att-seam-0001")
        )

    def test_deny_authority_denies_non_fixture(self):
        d = _make_live_dir("seam-deny")
        self.addCleanup(shutil.rmtree, d, True)
        exe = d / "tool.py"
        ctr = d / "counter"
        impl = _write_fixture(exe, str(ctr))
        binding = _bind(_manifest(impl), exe)
        self.assertFalse(
            authorize_live(
                binding, exe, live_authority=DenyLiveAuthority(), operation_id="record", request_id="req-seam-0002", attempt_id="att-seam-0002")
        )

    def test_missing_verify_method_denies(self):
        d = _make_live_dir("seam-missing")
        self.addCleanup(shutil.rmtree, d, True)
        exe = d / "tool.py"
        ctr = d / "counter"
        impl = _write_fixture(exe, str(ctr))
        binding = _bind(_manifest(impl), exe)
        for impostor in (object(), "authority", 42, object()):
            self.assertFalse(
                authorize_live(binding, exe, live_authority=impostor, operation_id="record", request_id="req-seam-0003", attempt_id="att-seam-0003")
            )

    def test_exception_in_verify_denies(self):
        d = _make_live_dir("seam-exc")
        self.addCleanup(shutil.rmtree, d, True)
        exe = d / "tool.py"
        ctr = d / "counter"
        impl = _write_fixture(exe, str(ctr))
        binding = _bind(_manifest(impl), exe)
        self.assertFalse(
            authorize_live(binding, exe, live_authority=_RaisingStub(), operation_id="record", request_id="req-seam-0004", attempt_id="att-seam-0004")
        )

    def test_truthy_non_true_denies(self):
        d = _make_live_dir("seam-truthy")
        self.addCleanup(shutil.rmtree, d, True)
        exe = d / "tool.py"
        ctr = d / "counter"
        impl = _write_fixture(exe, str(ctr))
        binding = _bind(_manifest(impl), exe)
        for value in (1, "true", [], {}, object(), False, None, 0):
            stub = _RecordingStub(value)
            with self.subTest(value=repr(value)[:20]):
                self.assertFalse(
                    authorize_live(binding, exe, live_authority=stub, operation_id="record", request_id="req-seam-0005", attempt_id="att-seam-0005")
                )

    def test_strict_true_opens_non_fixture(self):
        d = _make_live_dir("seam-true")
        self.addCleanup(shutil.rmtree, d, True)
        exe = d / "tool.py"
        ctr = d / "counter"
        impl = _write_fixture(exe, str(ctr))
        binding = _bind(_manifest(impl), exe)
        stub = _RecordingStub(True)
        self.assertTrue(
            authorize_live(binding, exe, live_authority=stub, operation_id="record", request_id="req-seam-0006", attempt_id="att-seam-0006")
        )
        self.assertEqual(len(stub.calls), 1)

    def test_verify_receives_exact_request_bound_kwargs(self):
        d = _make_live_dir("seam-kwargs")
        self.addCleanup(shutil.rmtree, d, True)
        exe = d / "tool.py"
        ctr = d / "counter"
        impl = _write_fixture(exe, str(ctr))
        binding = _bind(_manifest(impl), exe)
        stub = _RecordingStub(True)
        authorize_live(binding, exe, live_authority=stub, operation_id="record", request_id="req-seam-kw-01", attempt_id="att-seam-kw-01")
        self.assertEqual(len(stub.calls), 1)
        kw = stub.calls[0]
        self.assertIs(kw["binding"], binding)
        self.assertEqual(kw["operation_id"], "record")
        self.assertEqual(kw["request_id"], "req-seam-kw-01")
        self.assertEqual(kw["attempt_id"], "att-seam-kw-01")
        # Request-bound: a different request is seen as different.
        stub2 = _RecordingStub(True)
        authorize_live(binding, exe, live_authority=stub2, operation_id="record", request_id="req-seam-kw-02", attempt_id="att-seam-kw-02")
        self.assertNotEqual(stub.calls[0]["request_id"], stub2.calls[0]["request_id"])

    def test_fixture_authority_checks_operation_and_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            exe, ctr = Path(td) / "tool.py", Path(td) / "counter"
            binding = _bind(_manifest(_write_fixture(exe, str(ctr))), exe)
            req, att = "req-seam-fx-0001", "att-seam-fx-0001"
            capability = _fixture_capability(binding, req, att)
            for op, attempt, expected in (("record", att, True), ("replay", att, False),
                                           ("record", att + "b", False)):
                self.assertIs(authorize_live(binding, exe, live_authority=capability,
                                             operation_id=op, request_id=req, attempt_id=attempt), expected)

    def test_fixture_cannot_override_deny_or_raising_authority(self):
        with tempfile.TemporaryDirectory() as td:
            exe, ctr = Path(td) / "tool.py", Path(td) / "counter"
            binding = _bind(_manifest(_write_fixture(exe, str(ctr))), exe)
            req, att = "req-seam-fx-0002", "att-seam-fx-0002"
            capability = _fixture_capability(binding, req, att)
            self.assertTrue(authorize_live(binding, exe, live_authority=capability,
                                           operation_id="record", request_id=req, attempt_id=att))
            for authority in (DenyLiveAuthority(), _RaisingStub()):
                self.assertFalse(authorize_live(binding, exe, live_authority=authority,
                                                operation_id="record", request_id=req, attempt_id=att))
            self.assertFalse(authorize_live(binding, exe, live_authority=None,
                                            operation_id="record", request_id=req, attempt_id=att))

    def test_fixture_authority_rejects_symlink_and_identical_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            exe, ctr = Path(td) / "tool.py", Path(td) / "counter"
            binding = _bind(_manifest(_write_fixture(exe, str(ctr))), exe)
            req, att = "req-seam-fx-0003", "att-seam-fx-0003"
            capability = _fixture_capability(binding, req, att)
            kwargs = dict(live_authority=capability, operation_id="record", request_id=req, attempt_id=att)
            self.assertTrue(authorize_live(binding, exe, **kwargs))
            link = exe.with_name("link.py")
            link.symlink_to(exe)
            self.assertFalse(authorize_live(binding, link, **kwargs))
            replacement = exe.with_name("replacement.py")
            shutil.copy2(exe, replacement)
            replacement.replace(exe)
            self.assertFalse(authorize_live(binding, exe, **kwargs))


class LiveGateTests(unittest.TestCase):
    def test_run_bound_without_authority_parks_live_zero_spawn(self):
        d = _make_live_dir("gate-default")
        self.addCleanup(shutil.rmtree, d, True)
        exe = d / "tool.py"
        ctr = d / "counter"
        impl = _write_fixture(exe, str(ctr))
        binding = _bind(_manifest(impl), exe)
        w = run_bound(binding, [], cwd=str(d), request_id=_uid("req-gate-0001"), attempt_id=_uid("att-gate-0001"), shared_receipt_ref="shared:fix")
        self.assertEqual(w["receipt"]["attempt"]["control_outcome"], LIVE_EXECUTION_PARKED)
        self.assertIs(w["evidence_accepted"], False)
        self.assertIsNone(w["pid"])
        self.assertFalse(ctr.exists())

    def test_run_bound_deny_authority_parks_live_zero_spawn(self):
        d = _make_live_dir("gate-deny")
        self.addCleanup(shutil.rmtree, d, True)
        exe = d / "tool.py"
        ctr = d / "counter"
        impl = _write_fixture(exe, str(ctr))
        binding = _bind(_manifest(impl), exe)
        w = run_bound(
            binding, [], cwd=str(d), request_id=_uid("req-gate-0002"), attempt_id=_uid("att-gate-0002"), shared_receipt_ref="shared:fix", live_authority=DenyLiveAuthority()
        )
        self.assertEqual(w["receipt"]["attempt"]["control_outcome"], LIVE_EXECUTION_PARKED)
        self.assertIs(w["evidence_accepted"], False)
        self.assertIsNone(w["pid"])
        self.assertFalse(ctr.exists())

    def test_run_bound_stub_true_opens_live_request_bound(self):
        d = _make_live_dir("gate-stub")
        self.addCleanup(shutil.rmtree, d, True)
        exe = d / "tool.py"
        ctr = d / "counter"
        impl = _write_fixture(exe, str(ctr))
        binding = _bind(_manifest(impl), exe)
        stub = _RecordingStub(True)
        req = _uid("req-gate-0003")
        att = _uid("att-gate-0003")
        w = run_bound(binding, [], cwd=str(d), request_id=req, attempt_id=att, shared_receipt_ref="shared:fix", live_authority=stub)
        self.assertNotEqual(w["receipt"]["attempt"].get("control_outcome"), LIVE_EXECUTION_PARKED)
        self.assertIsNotNone(w["pid"])
        self.assertTrue(ctr.exists())
        self.assertIs(w["evidence_accepted"], False)
        self.assertNotIn("slot_result", w["receipt"]["attempt"])
        self.assertEqual(w["custody"]["producer_custody_reason"], "owner-pending: T03")
        self.assertEqual(len(stub.calls), 2)
        self.assertEqual(stub.calls[0], stub.calls[1])
        self.assertEqual(stub.calls[0]["request_id"], req)
        self.assertEqual(stub.calls[0]["attempt_id"], att)
        self.assertEqual(stub.calls[0]["operation_id"], "record")
        self.assertIs(stub.calls[0]["binding"], binding)

    def test_run_bound_truthy_stub_still_parks(self):
        d = _make_live_dir("gate-truthy")
        self.addCleanup(shutil.rmtree, d, True)
        exe = d / "tool.py"
        ctr = d / "counter"
        impl = _write_fixture(exe, str(ctr))
        binding = _bind(_manifest(impl), exe)
        for value in (1, "true", object()):
            w = run_bound(
                binding, [], cwd=str(d), request_id=_uid("req-gate-0004"), attempt_id=_uid("att-gate-0004"), shared_receipt_ref="shared:fix", live_authority=_RecordingStub(value)
            )
            self.assertEqual(w["receipt"]["attempt"]["control_outcome"], LIVE_EXECUTION_PARKED)
            self.assertIsNone(w["pid"])
        self.assertFalse(ctr.exists())

    def test_run_bound_raising_stub_still_parks(self):
        d = _make_live_dir("gate-raise")
        self.addCleanup(shutil.rmtree, d, True)
        exe = d / "tool.py"
        ctr = d / "counter"
        impl = _write_fixture(exe, str(ctr))
        binding = _bind(_manifest(impl), exe)
        w = run_bound(
            binding, [], cwd=str(d), request_id=_uid("req-gate-0005"), attempt_id=_uid("att-gate-0005"), shared_receipt_ref="shared:fix", live_authority=_RaisingStub()
        )
        self.assertEqual(w["receipt"]["attempt"]["control_outcome"], LIVE_EXECUTION_PARKED)
        self.assertIsNone(w["pid"])
        self.assertFalse(ctr.exists())

    def test_run_bound_env_flag_does_not_open_gate(self):
        d = _make_live_dir("gate-env")
        self.addCleanup(shutil.rmtree, d, True)
        exe = d / "tool.py"
        ctr = d / "counter"
        impl = _write_fixture(exe, str(ctr))
        binding = _bind(_manifest(impl), exe)
        old = os.environ.get("REA_ALLOW_LIVE")
        os.environ["REA_ALLOW_LIVE"] = "1"
        try:
            w = run_bound(
                binding, [], cwd=str(d), request_id=_uid("req-gate-0006"), attempt_id=_uid("att-gate-0006"), env={"REA_ALLOW_LIVE": "1"}, shared_receipt_ref="shared:fix"
            )
        finally:
            if old is None:
                os.environ.pop("REA_ALLOW_LIVE", None)
            else:
                os.environ["REA_ALLOW_LIVE"] = old
        self.assertEqual(w["receipt"]["attempt"]["control_outcome"], LIVE_EXECUTION_PARKED)
        self.assertIsNone(w["pid"])
        self.assertFalse(ctr.exists())

    def test_run_bound_fixture_requires_its_capability(self):
        with tempfile.TemporaryDirectory() as td:
            exe, ctr = Path(td) / "tool.py", Path(td) / "counter"
            binding = _bind(_manifest(_write_fixture(exe, str(ctr))), exe)
            req, att = _uid("req-gate-fx-0001"), _uid("att-gate-fx-0001")
            capability = _fixture_capability(binding, req, att)
            args = dict(cwd=td, request_id=req, attempt_id=att, shared_receipt_ref="shared:fix")
            self.assertFalse(authorize_live(binding, exe, live_authority=None,
                                            operation_id="record", request_id=req, attempt_id=att))
            denied = run_bound(binding, [], live_authority=DenyLiveAuthority(), **args)
            self.assertEqual(denied["receipt"]["attempt"].get("control_outcome"), LIVE_EXECUTION_PARKED)
            self.assertIsNone(denied["pid"])
            self.assertFalse(ctr.exists())
            w = run_bound(binding, [], live_authority=capability, **args)
            self.assertIsNotNone(w["pid"])
            self.assertTrue(ctr.exists())
            self.assertNotEqual(w["receipt"]["attempt"].get("control_outcome"), LIVE_EXECUTION_PARKED)
            self.assertIs(w["evidence_accepted"], True)
            self.assertFalse(w["receipt"]["shared_receipt_adapter"]["live_use_permitted"])
            self.assertNotIn("shared_receipt_ref", w["receipt"]["shared_receipt_adapter"])

    def test_execute_forwards_live_authority(self):
        d = _make_live_dir("gate-execute")
        self.addCleanup(shutil.rmtree, d, True)
        exe = d / "tool.py"
        ctr = d / "counter"
        impl = _write_fixture(exe, str(ctr))
        manifest = _manifest(impl)
        req1 = _uid("req-ex-0001")
        w1 = execute(
            manifest, str(exe), [], operation_id="record", host_grants=["grant.exec.external_process"], allow_pending_evidence=True, request_id=req1, attempt_id=_uid("att-ex-0001"), cwd=str(d), shared_receipt_ref="shared:fix"
        )
        self.assertEqual(w1["receipt"]["attempt"]["control_outcome"], LIVE_EXECUTION_PARKED)
        self.assertIsNone(w1["pid"])
        self.assertFalse(ctr.exists())
        stub = _RecordingStub(True)
        req2 = _uid("req-ex-0002")
        att2 = _uid("att-ex-0002")
        w2 = execute(
            manifest, str(exe), [], operation_id="record", host_grants=["grant.exec.external_process"], allow_pending_evidence=True, request_id=req2, attempt_id=att2, cwd=str(d), shared_receipt_ref="shared:fix", live_authority=stub
        )
        self.assertNotEqual(w2["receipt"]["attempt"].get("control_outcome"), LIVE_EXECUTION_PARKED)
        self.assertIsNotNone(w2["pid"])
        self.assertTrue(ctr.exists())
        self.assertEqual(stub.calls[0]["request_id"], req2)
        self.assertEqual(stub.calls[0]["attempt_id"], att2)


def _register_live(name):
    d = _make_live_dir(name)
    exe = d / "tool.py"
    ctr = d / "counter"
    impl = _write_fixture(exe, str(ctr))
    manifest = _manifest(impl)
    ref = "installation:fixture-slot@%s" % _uid("auth")
    register_installation(ref, manifest, str(exe))
    return d, exe, ctr, manifest, ref


class ToolsGateTests(unittest.TestCase):
    def test_dispatch_without_policy_no_lease_zero_spawn(self):
        d, exe, ctr, manifest, ref = _register_live("tools-nopol")
        self.addCleanup(shutil.rmtree, d, True)
        payload = {"installation_ref": ref, "operation_id": "record", "argv": [], "host_grants": ["grant.exec.external_process"]}
        with self.assertRaises(_ControlError) as ctx:
            dispatch("behaviour.record", {"request_id": _uid("req-tools-0001"), "payload": payload}, host=None)
        self.assertEqual(ctx.exception.code, "NO_LEASE")
        binding = _bind(manifest, exe)
        self.assertFalse(authorize_live(binding, exe, live_authority=None, operation_id="record",
                                        request_id="req-tools-nopol", attempt_id="att-tools-nopol"))
        self.assertFalse(ctr.exists())

    def test_dispatch_with_policy_no_authority_parks_zero_spawn(self):
        d, _exe, ctr, manifest, ref = _register_live("tools-park")
        self.addCleanup(shutil.rmtree, d, True)
        payload = {"installation_ref": ref, "operation_id": "record", "argv": []}
        # B-05: parked dispatch now raises _ControlError LIVE_EXECUTION_PARKED, not ok=True
        with self.assertRaises(_ControlError) as ctx:
            dispatch("behaviour.record", {"request_id": _uid("req-tools-0002"), "payload": payload}, host=None, host_policy=_policy_for(manifest))
        self.assertEqual(ctx.exception.code, LIVE_EXECUTION_PARKED)
        self.assertFalse(ctr.exists())

    def test_dispatch_with_policy_stub_opens_and_forwards_request(self):
        d, _exe, ctr, manifest, ref = _register_live("tools-stub")
        self.addCleanup(shutil.rmtree, d, True)
        payload = {"installation_ref": ref, "operation_id": "record", "argv": []}
        req = _uid("req-tools-0003")
        stub = _RecordingStub(True)
        reply = dispatch("behaviour.record", {"request_id": req, "payload": payload}, host=None, host_policy=_policy_for(manifest), live_authority=stub)
        self.assertIs(reply.get("ok"), True)
        result = reply.get("result") or {}
        self.assertNotEqual(result.get("receipt", {}).get("attempt", {}).get("control_outcome"), LIVE_EXECUTION_PARKED)
        self.assertIsNotNone(result.get("pid"))
        self.assertTrue(ctr.exists())
        self.assertEqual(len(stub.calls), 4)
        self.assertTrue(all(call == stub.calls[0] for call in stub.calls))
        self.assertEqual(stub.calls[0]["request_id"], req)
        fixture_attempt = "fixture-" + hashlib.sha256(req.encode()).hexdigest()
        self.assertEqual(stub.calls[0]["attempt_id"], fixture_attempt)
        self.assertEqual(result["receipt"]["attempt"]["attempt_id"], fixture_attempt)

    def test_dispatch_dry_run_preview_zero_spawn_and_conflict(self):
        d, _exe, ctr, manifest, ref = _register_live("tools-dry")
        self.addCleanup(shutil.rmtree, d, True)
        payload = {"installation_ref": ref, "operation_id": "record", "argv": []}
        req = _uid("req-tools-dry-0001")
        policy = _policy_for(manifest)
        # B-04: dry-run now requires authority; without it parks
        with self.assertRaises(_ControlError) as ctx:
            dispatch("behaviour.record", {"request_id": req, "dry_run": True, "payload": payload}, host=None, host_policy=policy)
        self.assertEqual(ctx.exception.code, LIVE_EXECUTION_PARKED)
        self.assertFalse(ctr.exists())
        # With stub authority dry-run succeeds
        class _DryStub:
            def verify(self, **kw): return True
        preview = dispatch("behaviour.record", {"request_id": req, "dry_run": True, "payload": payload}, host=None, host_policy=policy, live_authority=_DryStub())
        self.assertIs(preview.get("dry_run"), True)
        self.assertFalse(ctr.exists())
        with self.assertRaises(_ControlError) as ctx:
            dispatch("behaviour.record", {"request_id": req, "dry_run": False, "payload": payload}, host=None, host_policy=policy)
        self.assertEqual(ctx.exception.code, "REQUEST_ID_CONFLICT")
        self.assertFalse(ctr.exists())

    def test_dispatch_unknown_payload_field_never_reaches_spawn(self):
        d, exe, ctr, manifest, ref = _register_live("tools-unknown")
        self.addCleanup(shutil.rmtree, d, True)
        payload = {"installation_ref": ref, "operation_id": "record", "argv": [], "live_allowed": True}
        with self.assertRaises(ValueError):
            dispatch("behaviour.record", {"request_id": _uid("req-tools-0004"), "payload": payload}, host=None, host_policy=_policy_for(manifest))
        binding = _bind(manifest, exe)
        self.assertFalse(authorize_live(binding, exe, live_authority=None, operation_id="record",
                                        request_id="req-tools-unknown", attempt_id="att-tools-unknown"))
        self.assertFalse(ctr.exists())


class CryptoContractTests(unittest.TestCase):
    """Exercise OpenSSL signing with disposable keys and production verification."""

    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        priv, self.public = _ed_keys(self.directory)
        self.policy = _policy(_derive_key_id(self.public))
        self.payload = _payload_bytes(self.policy)
        _, signature = _sign(priv, self.payload, self.directory)
        self.signature = signature.read_bytes()

    def _verify(self, *, payload=None, signature=None, public=None):
        # Build the untrusted envelope independently, including invalid input
        # that the production encoder itself would reject.
        envelope = {
            "payload_b64": base64.b64encode(self.payload if payload is None else payload).decode(),
            "signature_b64": base64.b64encode(self.signature if signature is None else signature).decode(),
        }
        return policy_signature.verify_and_parse_envelope(
            envelope, public_key_path=self.public if public is None else public,
            expected_audience=_AUDIENCE, now=_NOW,
        )

    def assert_denied(self, reason, **overrides):
        with self.assertRaises(policy_signature.PolicySignatureRejected) as rejected:
            self._verify(**overrides)
        self.assertEqual(rejected.exception.reason, reason)

    def test_sign_verify_roundtrip_on_exact_bytes(self):
        self.assertEqual(self._verify(), self.policy)

    def test_one_byte_payload_flip_fails_verify(self):
        altered = self.payload.replace(b'"generation":1', b'"generation":2', 1)
        self.assertNotEqual(self.payload, altered)
        self.assert_denied("signature_invalid", payload=altered)

    def test_trailing_newline_is_part_of_signed_bytes(self):
        self.assertTrue(self.payload.endswith(b"\n"))
        self.assert_denied("signature_invalid", payload=self.payload[:-1])

    def test_wrong_key_fails_verify(self):
        _, other = _ed_keys(self.directory, "other")
        self.assert_denied("signature_invalid", public=other)

    def test_truncated_signature_fails_verify(self):
        self.assert_denied("envelope_invalid", signature=self.signature[:-1])

    def test_missing_and_oversized_inputs_fail_closed(self):
        self.assert_denied("envelope_invalid", payload=b"x" * (policy_signature.MAX_PAYLOAD_BYTES + 1))
        self.assert_denied("envelope_invalid", signature=b"")
        self.assert_denied("public_key_invalid", public=Path(self.directory) / "missing.pem")


class OwnerPendingTests(unittest.TestCase):
    """Owner-dependent positives: explicit seams that must deny today."""

    @unittest.skip("owner-pending: MAIN")
    def test_production_signed_policy_authorizes_live(self):
        raise AssertionError("MAIN must enrol the policy-authorized route")

    @unittest.skip("owner-pending: MAIN")
    def test_main_enrolled_live_call_via_shared_requeststore(self):
        raise AssertionError("MAIN RequestStore enrolment is owner-pending")

    @unittest.skip("owner-pending: T01")
    def test_tools_durable_start_and_terminal_readback_enables_live(self):
        raise AssertionError("ViewPoint_Tools authenticated retention is owner-pending")

    def test_tools_external_process_attempt_mapping_and_allocation(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / "fixture.py"
            counter = Path(directory) / "launches"
            manifest = _manifest(_write_fixture(exe, str(counter)))
            manifest["evidence"]["shared_schema_ref"] = V03_REF
            manifest["upstream"]["source_url"] = "https://vendor.example/path.invalid/tool"

            with patch.object(runtime_binding, "_retention_available", _retention_available):
                with self.assertRaisesRegex(BindingRejected, "owner-pending: T01") as refusal:
                    _bind(manifest, exe)
                self.assertEqual(refusal.exception.reason, "profile_not_admitted")
            self.assertFalse(counter.exists())

            binding = _bind(manifest, exe)
            request_id, attempt_id = "req-v03-mapping", "att-v03-mapping"
            wrapper = run_bound(binding, [], cwd=directory, request_id=request_id, attempt_id=attempt_id,
                                live_authority=_fixture_capability(binding, request_id, attempt_id))
            bundle = wrapper["shared_records"]
            expected = hashlib.sha256(b"rea/attempt/v0.3:att-v03-mapping").hexdigest()[:32]
            records = json.loads((FIXTURES / "attempt_evidence_record.v0.3.schema.json").read_bytes())
            index = json.loads((FIXTURES / "rea_retention_index.v1.schema.json").read_bytes())
            for field, schema in (("started", records), ("terminal", records), ("retention_index", index)):
                self.assertTrue(validate(bundle[field], schema))
                self.assertEqual(bundle[field]["attempt_id"], expected)
            self.assertEqual(bundle["terminal"]["artifact"]["sha256"], FIXTURE_RESULT_SHA256)
            self.assertEqual(counter.read_text(), "1")

            for source in (
                "https://fixture@vendor.example.org/tool",
                "https://bad_host/tool",
                "https://vendor.example:123456/tool",
            ):
                manifest["upstream"]["source_url"] = source
                with self.subTest(source=source), self.assertRaisesRegex(BindingRejected, "upstream.source_url"):
                    _bind(manifest, exe)

    @unittest.skip("owner-pending: T03")
    def test_tools_custody_readback_rejects_foreign_producer(self):
        raise AssertionError("ViewPoint_Tools producer-attribution witness is owner-pending")

    @unittest.skip("owner-pending: T04")
    def test_tools_bulk_retention_and_acl_readback(self):
        raise AssertionError("ViewPoint_Tools retention is owner-pending")

    @unittest.skip("owner-pending: T05")
    def test_tools_recovery_of_started_only_attempt(self):
        raise AssertionError("ViewPoint_Tools recovery is owner-pending")

    @unittest.skip("owner-pending: T06")
    def test_live_v2_exchange_and_unseeded_replay_mapping(self):
        raise AssertionError("ViewPoint_Tools live mapping approval is owner-pending")


if __name__ == "__main__":
    unittest.main()
