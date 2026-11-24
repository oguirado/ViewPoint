# SPDX-License-Identifier: AGPL-3.0-only
"""Live-execution park: gate, no-bypass, fixture, authority seam.

Covers the PARK_REVIEW admission and launch boundaries. Each test uses
its own disposable artifacts.
Fixture authority is scoped to a host-created capability, never
from tempfile.gettempdir(), TMPDIR/TEMP/TMP, or any env variable.
"""
import copy
import json
import os
import stat
import shutil
from functools import partial
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from slot_host.runtime.binding import sha256_of_file
from slot_host.runtime.live_park import (
    LIVE_EXECUTION_PARKED,
    DenyLiveAuthority,
    _DEFAULT_AUTHORITY,
    authorize_live,
)
from slot_host.runtime.runner import run_bound
from slot_host.runtime.tools import _ControlError, register_installation
from slot_host.runtime import live_park, tools as tools_mod
from slot_host.runtime.tests.binding_fixtures import (
    fixture_policy,
    fixture_retention,
    verified_fixture_binding as verify_binding,
    execute_fixture as execute,
    dispatch_fixture as dispatch,
)

setUpModule = fixture_retention

_ADAPTER = "d" * 64
_PROV = "a" * 64
_REQ = "b" * 64
_RES = "c" * 64
_SEQ = [0]

def _uid(prefix):
    _SEQ[0] += 1
    return "%s-%d-%d" % (prefix, os.getpid(), _SEQ[0])

def _manifest(impl_sha, profile="external_process"):
    def _op(oid, role):
        return {"id": oid, "role": role, "input_ref": f"urn:rea.slot_request:v2#slot={role}", "output_ref": f"urn:rea.slot_result:v2#slot={role}", "binding": {"kind": "cli_subcommand", "argv": [oid], "typed_args": []}, "required_effects": ["process.spawn:child"], "required_grants": ["grant.exec.external_process"], "limits": {"timeout_s": 5, "max_output_bytes": 4096}, "retry_semantics": {"mode": "at_most_once", "on_interrupt": "UNCERTAIN_OUTCOME"}}
    ops = [_op("record", "replay"), _op("replay", "replay"), _op("compare", "verify")]
    return {"schema": "rea.slot_manifest/v2", "slot_id": "fixture-slot", "status": "registered", "upstream": {"name": "fixture", "source_url": "https://example.com/fixture", "version": "1.0.0", "licence_id": "MIT", "licence_evidence_refs": ["fixture: licence"], "component_notices": [], "user_installed": True}, "provider_ref": {"schema": "rea.provider/v2", "provider_id": "fixture-slot", "provider_revision": "1.0.0", "provider_sha256": _PROV, "implementation_sha256": impl_sha}, "contract_refs": [{"role": "replay", "request_schema_ref": "urn:rea.slot_request:v2", "request_digest": _REQ, "result_schema_ref": "urn:rea.slot_result:v2", "result_digest": _RES, "execution_profile": profile}, {"role": "verify", "request_schema_ref": "urn:rea.slot_request:v2", "request_digest": _REQ, "result_schema_ref": "urn:rea.slot_result:v2", "result_digest": _RES, "execution_profile": profile}], "transport": {"kind": "cli_argv", "driver_location": "outside_capsule", "executable_binding": "user_selected", "argv_prefix": [], "framing": "none", "cwd_policy": "run_scratch", "env_allowlist": ["PATH"]}, "operations": ops, "capabilities": {"target_kinds": ["fixture_target"], "platforms": ["linux"], "prerequisite_probes": [], "optional_features": [], "adapter_identity": {"adapter_id": "fixture.v1", "adapter_sha256": _ADAPTER, "provenance": "host_authored"}}, "perception": {"required_level": "none", "optional_levels": [], "observation_sources": [{"id": "tool-evidence", "level": "none", "producer": "tool_stdio", "outside_capsule": True}]}, "record_replay": {"input_codec": "json", "output_codec": "json", "observable_channels": ["error"], "state_domain": "per_case", "effects": ["process.spawn:child"], "determinism_modes": ["recorded_inputs"], "freshness_inputs": ["fixture"]}, "comparison": {"comparator_id": "fixture.compare", "version": "1", "policy_ref": "policy:fixture@1", "coverage_requirements": {"channel": "error"}, "tolerance_units": "none", "waiver_policy": {"source": "host_policy", "binds": ["input", "actual", "reason", "scope", "policy_version"], "stale": "fail"}}, "evidence": {"shared_schema_ref": "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2", "required_rea_fields": ["episode", "observation", "slot"], "artifact_roles": ["evidence_bundle"], "retention": "evidence_store_attempt", "visibility": "redacted_projection"}, "conformance": {"fixture_refs": ["fixture:1"], "negative_control_refs": ["control:1"], "supported_entry_points": ["cli", "mcp", "json"], "required_checks": ["admit-and-project", "outside-record-replay-compare", "perception-and-pool", "transport-parity-and-retry"]}, "discovery": {"summary": "fixture", "search_terms": ["fixture"], "operation_descriptions": {"record": "record", "replay": "replay", "compare": "compare"}, "examples": []}}

def _write_fixture(path: Path, counter: str):
    ctr = repr(counter).encode()
    body = (b"#!/usr/bin/env python3\n# SPDX-License-Identifier: AGPL-3.0-only\nimport json, sys\nctr=" + ctr + b"\nopen(ctr,'a').write('1')\nobj={\"schema\":\"rea.slot_result/v1\",\"status\":\"OK\",\"rc\":0,\"sha256\":\"e\"+\"f\"*63}\nsys.stdout.write(json.dumps(obj)+'\\n')\nsys.stdout.flush()\n")
    path.write_bytes(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return sha256_of_file(path)

def _bind(manifest, exe: Path):
    return verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"}, allow_pending_evidence=True, requested_profile="external_process")

def _policy_for(manifest):
    return dict(fixture_policy(manifest), perception_ceiling="none")


def _fixture_capability(binding, request_id, attempt_id):
    capability = getattr(live_park, "_FixtureLiveAuthority", None)
    if capability is None:
        # Exercise the baseline's actual fixture mechanism in cross-revision tests.
        live_park.register_fixture(binding.executable_path)
        return None
    return capability(binding, request_id=request_id, attempt_id=attempt_id)


def _assert_no_temporary_directory_authority(case):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "inert"
        with patch.object(tempfile, "gettempdir", side_effect=AssertionError("unexpected temporary directory lookup")) as lookup:
            case.assertFalse(authorize_live(
                object(), path, live_authority=None, operation_id="record",
                request_id=_uid("req-park-temp"), attempt_id=_uid("att-park-temp"),
            ))
        lookup.assert_not_called()


class LivePark(unittest.TestCase):
    def test_a_live_launch_refused_with_parked_code_and_zero_spawns(self):
        """Unauthenticated external_process live launch parks (fails on 6b14e8ff where it spawned)."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            exe = td / "tool.py"
            ctr = td / "counter"
            impl = _write_fixture(exe, str(ctr))
            binding = _bind(_manifest(impl), exe)
            # No registry, no authority -> default deny
            w = run_bound(binding, [], cwd=str(td), request_id=_uid("req-park-0001"), attempt_id=_uid("att-park-0001"), shared_receipt_ref="shared:fix")
            self.assertEqual(w["receipt"]["attempt"]["control_outcome"], LIVE_EXECUTION_PARKED)
            self.assertIs(w["evidence_accepted"], False)
            self.assertIsNone(w["pid"])
            self.assertFalse(ctr.exists())
            # Paired check: registered fixture with same executable WOULD run — on old code both ran, so paired assertion distinguishes
            req, att = _uid("req-park-0001b"), _uid("att-park-0001b")
            capability = _fixture_capability(binding, req, att)
            w2 = run_bound(binding, [], cwd=str(td), request_id=req, attempt_id=att,
                           shared_receipt_ref="shared:fix", live_authority=capability)
            self.assertNotEqual(w2["receipt"]["attempt"].get("control_outcome"), LIVE_EXECUTION_PARKED)
            self.assertTrue(ctr.exists())
            self.assertFalse(authorize_live(binding, exe, live_authority=DenyLiveAuthority(),
                                            operation_id="record", request_id=req, attempt_id=att))

    def test_b_env_manifest_flag_attempts_still_refused(self):
        _assert_no_temporary_directory_authority(self)
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            exe = directory / "tool.py"
            counter = directory / "counter"
            binding = _bind(_manifest(_write_fixture(exe, str(counter))), exe)
            with patch.dict(os.environ, {"REA_ALLOW_LIVE": "1"}):
                result = run_bound(binding, [], cwd=td, request_id=_uid("req-park-b"),
                                   attempt_id=_uid("att-park-b"), env={"REA_ALLOW_LIVE": "1"})
            self.assertEqual(result["receipt"]["attempt"]["control_outcome"], LIVE_EXECUTION_PARKED)
            self.assertIsNone(result["pid"])
            with self.assertRaisesRegex(ValueError, "unknown payload fields"):
                dispatch("behaviour.record", {"request_id": _uid("req-park-b"),
                         "payload": {"installation_ref": "x", "operation_id": "record", "live_allowed": True}})
            self.assertFalse(counter.exists())

    def test_c_registered_fixture_still_runs_unregistered_temp_parks(self):
        """Host-registered fixture runs; an unregistered exe under temp dir parks (fails on old where both ran)."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            # Registered
            exe_ok = td / "ok.py"
            ctr_ok = td / "ctr_ok"
            impl_ok = _write_fixture(exe_ok, str(ctr_ok))
            binding_ok = _bind(_manifest(impl_ok), exe_ok)
            req, att = _uid("req-fx-0001"), _uid("att-fx-0001")
            capability = _fixture_capability(binding_ok, req, att)
            w = run_bound(binding_ok, [], cwd=str(td), request_id=req, attempt_id=att,
                          shared_receipt_ref="shared:fix", live_authority=capability)
            self.assertFalse(authorize_live(binding_ok, exe_ok, live_authority=capability,
                                            operation_id="record", request_id=req + "b", attempt_id=att))
            self.assertIsNotNone(w["pid"])
            self.assertTrue(ctr_ok.exists())
            self.assertNotEqual(w["receipt"]["attempt"].get("control_outcome"), LIVE_EXECUTION_PARKED)
            # Unregistered exe in same temp dir must still park (old code would have spawned)
            exe_bad = td / "bad.py"
            ctr_bad = td / "ctr_bad"
            impl_bad = _write_fixture(exe_bad, str(ctr_bad))
            binding_bad = _bind(_manifest(impl_bad), exe_bad)
            w2 = run_bound(binding_bad, [], cwd=str(td), request_id=_uid("req-fx-0002"), attempt_id=_uid("att-fx-0002"), shared_receipt_ref="shared:fix")
            self.assertEqual(w2["receipt"]["attempt"]["control_outcome"], LIVE_EXECUTION_PARKED)
            self.assertIsNone(w2["pid"])
            self.assertFalse(ctr_bad.exists())

    def test_d_authority_stub_opens_gate_only_in_test(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            exe = td / "tool.py"
            ctr = td / "counter"
            impl = _write_fixture(exe, str(ctr))
            binding = _bind(_manifest(impl), exe)
            class Stub:
                def verify(self, **kw):
                    return True
            w = run_bound(binding, [], cwd=str(td), request_id=_uid("req-park-0003"), attempt_id=_uid("att-park-0003"), shared_receipt_ref="shared:fix", live_authority=Stub())
            self.assertIsNotNone(w["pid"])
            self.assertTrue(ctr.exists())
            self.assertNotEqual(w["receipt"]["attempt"].get("control_outcome"), LIVE_EXECUTION_PARKED)
            # Same exe without authority must park — paired to make test fail on old
            exe2 = td / "tool2.py"
            ctr2 = td / "counter2"
            impl2 = _write_fixture(exe2, str(ctr2))
            binding2 = _bind(_manifest(impl2), exe2)
            w2 = run_bound(binding2, [], cwd=str(td), request_id=_uid("req-park-0003b"), attempt_id=_uid("att-park-0003b"), shared_receipt_ref="shared:fix")
            self.assertEqual(w2["receipt"]["attempt"]["control_outcome"], LIVE_EXECUTION_PARKED)
            self.assertFalse(ctr2.exists())

    def test_e_deny_missing_and_truthy_authorities_still_park(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            exe = td / "tool.py"
            ctr = td / "counter"
            impl = _write_fixture(exe, str(ctr))
            binding = _bind(_manifest(impl), exe)
            class Truthy:
                def verify(self, **kw):
                    return 1
            class Raising:
                def verify(self, **kw):
                    raise RuntimeError("boom")
            for label, auth in (("deny", DenyLiveAuthority()), ("truthy", Truthy()), ("raising", Raising())):
                w = run_bound(binding, [], cwd=str(td), request_id=_uid(f"req-park-e-{label}-0001"), attempt_id=_uid(f"att-park-e-{label}-0001"), shared_receipt_ref="shared:fix", live_authority=auth)
                self.assertEqual(w["receipt"]["attempt"]["control_outcome"], LIVE_EXECUTION_PARKED)
                self.assertIsNone(w["pid"])
                self.assertFalse(ctr.exists())
            w = run_bound(binding, [], cwd=str(td), request_id=_uid("req-park-e-missing-0001"), attempt_id=_uid("att-park-e-missing-0001"), shared_receipt_ref="shared:fix", live_authority=object())
            self.assertEqual(w["receipt"]["attempt"]["control_outcome"], LIVE_EXECUTION_PARKED)
            self.assertFalse(ctr.exists())
            # Default (None) also denies via _DEFAULT_AUTHORITY
            w2 = run_bound(binding, [], cwd=str(td), request_id=_uid("req-park-e-default-0001"), attempt_id=_uid("att-park-e-default-0001"), shared_receipt_ref="shared:fix", live_authority=None)
            self.assertEqual(w2["receipt"]["attempt"]["control_outcome"], LIVE_EXECUTION_PARKED)
            self.assertFalse(ctr.exists())

    def test_f_fixture_never_overrides_denying_authority(self):
        _assert_no_temporary_directory_authority(self)
        with tempfile.TemporaryDirectory() as td:
            exe, ctr = Path(td) / "tool.py", Path(td) / "counter"
            binding = _bind(_manifest(_write_fixture(exe, str(ctr))), exe)
            req, att = _uid("req-park-f-0001"), _uid("att-park-f-0001")
            capability = _fixture_capability(binding, req, att)
            calls = []
            class Recording:
                def verify(self, **kw):
                    calls.append(kw)
                    return False
            w = run_bound(binding, [], cwd=td, request_id=req, attempt_id=att,
                          shared_receipt_ref="shared:fix", live_authority=Recording())
            self.assertIsNone(w["pid"])
            self.assertFalse(ctr.exists())
            self.assertEqual(w["receipt"]["attempt"].get("control_outcome"), LIVE_EXECUTION_PARKED)
            self.assertEqual(len(calls), 1)
            self.assertTrue(authorize_live(binding, exe, live_authority=capability,
                                           operation_id="record", request_id=req, attempt_id=att))

    def test_g_verify_is_request_bound_via_run_bound(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            exe = td / "tool.py"
            ctr = td / "counter"
            impl = _write_fixture(exe, str(ctr))
            binding = _bind(_manifest(impl), exe)
            # Without authority, must park (fails on old where it spawned because path under temp)
            w0 = run_bound(binding, [], cwd=str(td), request_id=_uid("req-park-g-pre"), attempt_id=_uid("att-park-g-pre"), shared_receipt_ref="shared:fix")
            self.assertEqual(w0["receipt"]["attempt"]["control_outcome"], LIVE_EXECUTION_PARKED)
            calls = []
            class Recording:
                def verify(self, **kw):
                    calls.append(kw)
                    return True
            req, att = _uid("req-park-g-0001"), _uid("att-park-g-0001")
            w = run_bound(binding, [], cwd=str(td), request_id=req, attempt_id=att,
                          live_authority=Recording())
            self.assertIsNotNone(w["pid"])
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0], calls[1])
            self.assertIs(calls[0]["binding"], binding)
            self.assertEqual(calls[0]["operation_id"], "record")
            self.assertEqual(calls[0]["request_id"], req)
            self.assertEqual(calls[0]["attempt_id"], att)

    def test_h_public_grant_claim_does_not_open_live_gate(self):
        _assert_no_temporary_directory_authority(self)
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            exe = td / "tool.py"
            ctr = td / "counter"
            impl = _write_fixture(exe, str(ctr))
            ref = _uid("installation:fixture-slot@parkh")
            register_installation(ref, _manifest(impl), str(exe))
            payload = {"installation_ref": ref, "operation_id": "record", "argv": [], "host_grants": ["grant.exec.external_process"]}
            # Without authenticated host_policy, must be NO_LEASE, not a live spawn
            with self.assertRaises(_ControlError) as ctx:
                dispatch("behaviour.record", {"request_id": _uid("req-park-h-0001"), "payload": payload}, host=None)
            self.assertEqual(ctx.exception.code, "NO_LEASE")
            self.assertFalse(ctr.exists())

    def test_i_tmpdir_temp_must_not_bypass_even_when_writable(self):
        """TMPDIR/TEMP/TMP pointing at a real dir must not make a non-fixture executable park-exempt."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            exe = td / "tool.py"
            ctr = td / "counter"
            impl = _write_fixture(exe, str(ctr))
            binding = _bind(_manifest(impl), exe)
            # Set TMPDIR to the temp dir itself (would have previously made is_disposable_fixture True)
            old_tmp = {k: os.environ.get(k) for k in ("TMPDIR","TEMP","TMP")}
            for k in ("TMPDIR","TEMP","TMP"):
                os.environ[k] = str(td)
            try:
                w = run_bound(binding, [], cwd=str(td), request_id=_uid("req-park-i0001"), attempt_id=_uid("att-park-i0001"), shared_receipt_ref="shared:fix")
            finally:
                for k,val in old_tmp.items():
                    if val is None:
                        os.environ.pop(k,None)
                    else:
                        os.environ[k]=val
            self.assertEqual(w["receipt"]["attempt"]["control_outcome"], LIVE_EXECUTION_PARKED)
            self.assertFalse(ctr.exists())
            # authorize_live must not consult env
            self.assertFalse(authorize_live(binding, exe, live_authority=None, operation_id="record", request_id=_uid("req-i0002"), attempt_id=_uid("att-i0002")))

    def test_j_tempdir_module_mutation_must_not_bypass(self):
        _assert_no_temporary_directory_authority(self)

    def test_k_runner_execute_gate(self):
        """runner.execute without authority parks; with stub authority it runs (paired to fail on old)."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            exe = td / "tool.py"
            ctr = td / "counter"
            impl = _write_fixture(exe, str(ctr))
            manifest = _manifest(impl)
            req1 = _uid("req-ex-0001")
            w1 = execute(manifest, str(exe), [], operation_id="record", host_grants=["grant.exec.external_process"], allow_pending_evidence=True, request_id=req1, attempt_id=_uid("att-ex-0001"), cwd=str(td), shared_receipt_ref="shared:fix")
            self.assertEqual(w1["receipt"]["attempt"]["control_outcome"], LIVE_EXECUTION_PARKED)
            self.assertIsNone(w1["pid"])
            self.assertFalse(ctr.exists())
            class Stub:
                def verify(self, **kw): return True
            req2 = _uid("req-ex-0002")
            w2 = execute(manifest, str(exe), [], operation_id="record", host_grants=["grant.exec.external_process"], allow_pending_evidence=True, request_id=req2, attempt_id=_uid("att-ex-0002"), cwd=str(td), shared_receipt_ref="shared:fix", live_authority=Stub())
            self.assertNotEqual(w2["receipt"]["attempt"].get("control_outcome"), LIVE_EXECUTION_PARKED)
            self.assertIsNotNone(w2["pid"])
            self.assertTrue(ctr.exists())

    def test_l_dispatch_live_parked_raises_control_error(self):
        """tools.dispatch with policy but no authority raises _ControlError LIVE_EXECUTION_PARKED, not ok=True."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            exe = td / "tool.py"
            ctr = td / "counter"
            impl = _write_fixture(exe, str(ctr))
            manifest = _manifest(impl)
            ref = _uid("installation:fixture-slot@disp-park")
            register_installation(ref, manifest, str(exe))
            payload = {"installation_ref": ref, "operation_id": "record", "argv": []}
            policy = _policy_for(manifest)
            with self.assertRaises(_ControlError) as ctx:
                dispatch("behaviour.record", {"request_id": _uid("req-disp-0001"), "payload": payload}, host=None, host_policy=policy)
            self.assertEqual(ctx.exception.code, LIVE_EXECUTION_PARKED)
            self.assertFalse(ctr.exists())
            # With stub authority it succeeds
            class Stub:
                def verify(self, **kw): return True
            reply = dispatch("behaviour.record", {"request_id": _uid("req-disp-0002"), "payload": payload}, host=None, host_policy=policy, live_authority=Stub())
            self.assertIs(reply.get("ok"), True)
            result = reply.get("result") or {}
            self.assertNotEqual(result.get("receipt",{}).get("attempt",{}).get("control_outcome"), LIVE_EXECUTION_PARKED)
            self.assertTrue(ctr.exists())

    def test_m_dispatch_dry_run_parks_without_authority(self):
        """Dry-run with policy but no authority parks (was advertised as planned before)."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            exe = td / "tool.py"
            ctr = td / "counter"
            impl = _write_fixture(exe, str(ctr))
            manifest = _manifest(impl)
            ref = _uid("installation:fixture-slot@dry-park")
            register_installation(ref, manifest, str(exe))
            payload = {"installation_ref": ref, "operation_id": "record", "argv": []}
            policy = _policy_for(manifest)
            # Dry-run without authority must raise PARKED
            with self.assertRaises(_ControlError) as ctx:
                dispatch("behaviour.record", {"request_id": _uid("req-dry-0001"), "dry_run": True, "payload": payload}, host=None, host_policy=policy)
            self.assertEqual(ctx.exception.code, LIVE_EXECUTION_PARKED)
            self.assertFalse(ctr.exists())
            # Dry-run with stub authority previews
            class Stub:
                def verify(self, **kw): return True
            preview = dispatch("behaviour.record", {"request_id": _uid("req-dry-0002"), "dry_run": True, "payload": payload}, host=None, host_policy=policy, live_authority=Stub())
            self.assertIs(preview.get("dry_run"), True)
            self.assertFalse(ctr.exists())
            # Retry with different dry_run must conflict
            req = _uid("req-dry-conf-0001")
            preview2 = dispatch("behaviour.record", {"request_id": req, "dry_run": True, "payload": payload}, host=None, host_policy=policy, live_authority=Stub())
            self.assertIs(preview2.get("dry_run"), True)
            with self.assertRaises(_ControlError) as ctx2:
                dispatch("behaviour.record", {"request_id": req, "dry_run": False, "payload": payload}, host=None, host_policy=policy, live_authority=Stub())
            self.assertEqual(ctx2.exception.code, "REQUEST_ID_CONFLICT")

    def test_n_dispatch_retry_authority_change_conflicts(self):
        """Retry with same request_id but different live_authority status must conflict (B-04)."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            exe = td / "tool.py"
            ctr = td / "counter"
            impl = _write_fixture(exe, str(ctr))
            manifest = _manifest(impl)
            ref = _uid("installation:fixture-slot@retry-auth")
            register_installation(ref, manifest, str(exe))
            payload = {"installation_ref": ref, "operation_id": "record", "argv": []}
            policy = _policy_for(manifest)
            req = _uid("req-retry-0001")
            class Stub:
                def verify(self, **kw): return True
            # First run with authorized stub succeeds
            reply = dispatch("behaviour.record", {"request_id": req, "payload": payload}, host=None, host_policy=policy, live_authority=Stub())
            self.assertIs(reply.get("ok"), True)
            # Retry same id with revoked authority (None) must conflict, not return cached live result
            with self.assertRaises(_ControlError) as ctx:
                dispatch("behaviour.record", {"request_id": req, "payload": payload}, host=None, host_policy=policy, live_authority=None)
            self.assertEqual(ctx.exception.code, "REQUEST_ID_CONFLICT")

    def test_o_tools_main_exit_16_on_parked(self):
        """CLI main returns 16 when dispatch maps LIVE_EXECUTION_PARKED."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            exe = td / "tool.py"
            ctr = td / "counter"
            impl = _write_fixture(exe, str(ctr))
            manifest = _manifest(impl)
            ref = _uid("installation:fixture-slot@main-park")
            register_installation(ref, manifest, str(exe))
            real_dispatch = partial(tools_mod.dispatch, host_policy=_policy_for(manifest))
            arguments = {"request_id": _uid("req-main-0001"),
                         "payload": {"installation_ref": ref, "operation_id": "record"}}
            rc = tools_mod.main(["mini", "behaviour.record", "--args", json.dumps(arguments)],
                                dispatch_fn=real_dispatch)
            self.assertEqual(rc, 16)
            self.assertFalse(ctr.exists())

    def test_p_compare_refusal_abstain_is_control_refused(self):
        """Compare operation parked receipt has abstain_reason control_refused (not custody_unverified)."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            exe = td / "tool.py"
            ctr = td / "counter"
            impl = _write_fixture(exe, str(ctr))
            # Need a binding with operation compare (role verify)
            manifest = _manifest(impl)
            # Bind for compare operation
            binding = verify_binding(manifest, str(exe), operation_id="compare", host_grants={"grant.exec.external_process"}, allow_pending_evidence=True, requested_profile="external_process")
            w = run_bound(binding, [], cwd=str(td), request_id=_uid("req-cmp-0001"), attempt_id=_uid("att-cmp-0001"), shared_receipt_ref="shared:fix")
            self.assertEqual(w["receipt"]["attempt"]["control_outcome"], LIVE_EXECUTION_PARKED)
            verdict = w["receipt"].get("verdict")
            self.assertIsNotNone(verdict)
            self.assertEqual(verdict.get("result"), "abstain")
            self.assertEqual(verdict.get("abstain_reason"), "control_refused")
            self.assertIs(w["evidence_accepted"], False)
            self.assertIsNone(w["pid"])
            self.assertFalse(ctr.exists())
            # Paired: with stub authority, compare would run and not be parked
            class Stub:
                def verify(self, **kw): return True
            w2 = run_bound(binding, [], cwd=str(td), request_id=_uid("req-cmp-0002"), attempt_id=_uid("att-cmp-0002"), shared_receipt_ref="shared:fix", live_authority=Stub())
            self.assertNotEqual(w2["receipt"]["attempt"].get("control_outcome"), LIVE_EXECUTION_PARKED)

    def test_q_deny_authority_instance_park_and_default_authority_constant(self):
        """DenyLiveAuthority and _DEFAULT_AUTHORITY always deny; None default is instantiated."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            exe = td / "tool.py"
            ctr = td / "counter"
            impl = _write_fixture(exe, str(ctr))
            binding = _bind(_manifest(impl), exe)
            # _DEFAULT_AUTHORITY is an instance of DenyLiveAuthority and returns False
            self.assertIsInstance(_DEFAULT_AUTHORITY, DenyLiveAuthority)
            self.assertIs(authorize_live(binding, exe, live_authority=None, operation_id="record", request_id=_uid("req-q-0001"), attempt_id=_uid("att-q-0001")), False)
            self.assertIs(authorize_live(binding, exe, live_authority=DenyLiveAuthority(), operation_id="record", request_id=_uid("req-q-0002"), attempt_id=_uid("att-q-0002")), False)
            # run_bound with None defaults to deny (check that default is consulted)
            w = run_bound(binding, [], cwd=str(td), request_id=_uid("req-q-0003"), attempt_id=_uid("att-q-0003"), shared_receipt_ref="shared:fix")
            self.assertEqual(w["receipt"]["attempt"]["control_outcome"], LIVE_EXECUTION_PARKED)

class ParkBoundaryRegressions(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.exe = self.root / "tool.py"
        self.counter = self.root / "counter"
        self.manifest = _manifest(_write_fixture(self.exe, str(self.counter)))
        self.binding = _bind(self.manifest, self.exe)
        self.ref = "installation:fixture-slot@boundary"
        self.request = _uid("req-boundary-0001")
        self.attempt = f"att-{self.request}"
        self.payload = {"installation_ref": self.ref, "argv": []}
        self.policy = _policy_for(self.manifest)
        self.host = {self.ref: (self.manifest, str(self.exe))}

    def _dispatch(self, authority, *, payload=None, tool="behaviour.record"):
        return dispatch(tool, {"request_id": self.request, "payload": self.payload if payload is None else payload},
                        host=self.host, host_policy=self.policy, live_authority=authority)

    def test_cached_live_reply_denies_revoked_or_raising_verifier(self):
        class Authority:
            response = True

            def verify(self, **kwargs):
                if isinstance(self.response, Exception):
                    raise self.response
                return self.response

        authority = Authority()
        self.assertTrue(self._dispatch(authority)["ok"])
        self.assertEqual(self.counter.read_text(), "1")
        for response in (False, RuntimeError("owner unavailable")):
            authority.response = response
            with self.subTest(response=response):
                with self.assertRaises(_ControlError) as caught:
                    self._dispatch(authority)
                self.assertEqual(caught.exception.code, "REQUEST_ID_CONFLICT")
                self.assertEqual(self.counter.read_text(), "1")

    def test_cached_fixture_reply_denies_after_capability_withdrawal(self):
        authority = _fixture_capability(self.binding, self.request, self.attempt)
        self.assertTrue(self._dispatch(authority)["ok"])
        if authority is None:
            live_park.unregister_fixture(self.exe)
        else:
            authority.revoke()
        with self.assertRaises(_ControlError) as caught:
            self._dispatch(authority)
        self.assertEqual(caught.exception.code, "REQUEST_ID_CONFLICT")
        self.assertEqual(self.counter.read_text(), "1")

    def test_cached_reply_rechecks_access_after_initial_lookup(self):
        class Authority:
            calls = 0

            def verify(self, **kwargs):
                self.calls += 1
                return self.calls < 6

        authority = Authority()
        self.assertTrue(self._dispatch(authority)["ok"])
        # On retry the preliminary check succeeds and the next check denies.
        authority.calls = 4
        with self.assertRaises(_ControlError) as caught:
            self._dispatch(authority)
        self.assertEqual(caught.exception.code, "REQUEST_ID_CONFLICT")
        self.assertEqual(self.counter.read_text(), "1")

    def test_joined_request_rechecks_access_after_wait(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch

        class Authority:
            allowed = True

            def verify(self, **kwargs):
                return self.allowed

        authority = Authority()
        child_finished, retry_waiting, winner_may_return = (threading.Event() for _ in range(3))

        def wait_before_publishing(*args, **kwargs):
            result = run_bound(*args, **kwargs)
            child_finished.set()
            if not winner_may_return.wait(5):
                raise TimeoutError("test winner was not released")
            return result

        with patch.object(tools_mod, "run_bound", side_effect=wait_before_publishing), ThreadPoolExecutor(2) as pool:
            winner = pool.submit(self._dispatch, authority)
            try:
                self.assertTrue(child_finished.wait(5))
                completed = tools_mod._REQUEST_STORE[self.request]["done"]
                original_wait = completed.wait

                def observe_wait(*args, **kwargs):
                    retry_waiting.set()
                    return original_wait(*args, **kwargs)

                with patch.object(completed, "wait", side_effect=observe_wait):
                    retry = pool.submit(self._dispatch, authority)
                    self.assertTrue(retry_waiting.wait(5))
                    authority.allowed = False
                    winner_may_return.set()
                    self.assertTrue(winner.result(timeout=5)["ok"])
                    with self.assertRaises(_ControlError) as caught:
                        retry.result(timeout=5)
                    self.assertEqual(caught.exception.code, "REQUEST_ID_CONFLICT")
            finally:
                winner_may_return.set()
        self.assertEqual(self.counter.read_text(), "1")

    def test_unsigned_live_request_refused_before_attempt_store_and_handler(self):
        from unittest.mock import patch

        stored = []
        result = run_bound(self.binding, [], attempt_store=stored.append,
                           request_id=self.request, attempt_id=self.attempt)
        self.assertEqual(result["receipt"]["attempt"].get("control_outcome"), LIVE_EXECUTION_PARKED)
        self.assertFalse(stored, "a denial before launch must leave no durable attempt")
        with patch.object(tools_mod, "run_bound", wraps=run_bound) as runner:
            with self.assertRaises(_ControlError) as caught:
                self._dispatch(None)
            self.assertEqual(caught.exception.code, LIVE_EXECUTION_PARKED)
            runner.assert_not_called()
        self.assertFalse(self.counter.exists())

    def test_authority_rechecked_after_executable_staging(self):
        from unittest.mock import patch
        from slot_host.runtime import executable_custody

        class Authority:
            allowed = True

            def verify(self, **kwargs):
                return self.allowed

        authority = Authority()
        real_stage = executable_custody.stage_verified_executable
        paths = []

        def stage_and_revoke(*args, **kwargs):
            snapshot = real_stage(*args, **kwargs)
            paths.append(snapshot.parent)
            authority.allowed = False
            return snapshot

        retained = []
        with patch.object(executable_custody, "stage_verified_executable", side_effect=stage_and_revoke):
            result = run_bound(self.binding, [], request_id=self.request, attempt_id=self.attempt,
                               attempt_store=retained.append, live_authority=authority)
        self.assertEqual(result["receipt"]["attempt"].get("control_outcome"), LIVE_EXECUTION_PARKED)
        self.assertIsNone(result["pid"])
        self.assertTrue(paths)
        self.assertFalse(paths[0].exists())
        self.assertFalse(self.counter.exists())
        self.assertIsNone(retained[0]["attempt"].get("control_outcome"))
        self.assertEqual(retained[-1]["attempt"].get("control_outcome"), LIVE_EXECUTION_PARKED)

    def test_fixture_staging_rejects_same_bytes_from_different_inode(self):
        from unittest.mock import patch
        from slot_host.runtime import executable_custody

        capability = _fixture_capability(self.binding, self.request, self.attempt)
        saved = self.exe.with_name("saved.py")
        replacement = self.exe.with_name("replacement.py")
        shutil.copy2(self.exe, replacement)
        real_stage = executable_custody.stage_verified_executable

        def swap_during_staging(*args, **kwargs):
            self.exe.replace(saved)
            replacement.replace(self.exe)
            try:
                return real_stage(*args, **kwargs)
            finally:
                saved.replace(self.exe)

        with patch.object(executable_custody, "stage_verified_executable", side_effect=swap_during_staging):
            result = run_bound(self.binding, [], request_id=self.request, attempt_id=self.attempt,
                               live_authority=capability)
        self.assertEqual(result["receipt"]["attempt"].get("control_outcome"), "evidence_binding_rejected")
        self.assertIsNone(result["pid"])
        self.assertFalse(self.counter.exists())

    def test_public_cwd_and_env_rejected_before_runner(self):
        from unittest.mock import patch

        class Authority:
            def verify(self, **kwargs):
                return True

        for key, value in (("cwd", str(self.root / "caller-cwd")), ("env", {"PATH": str(self.root)})):
            with self.subTest(key=key), patch.object(tools_mod, "run_bound", wraps=run_bound) as runner:
                with self.assertRaisesRegex(ValueError, "unknown payload fields"):
                    self._dispatch(Authority(), payload=dict(self.payload, **{key: value}))
                runner.assert_not_called()
        self.assertFalse((self.root / "caller-cwd").exists())
        self.assertFalse(self.counter.exists())

    def test_legacy_aliases_park_direct_manifest_inputs(self):
        class Authority:
            def verify(self, **kwargs):
                return True

        payload = {"manifest": self.manifest, "executable_path": str(self.exe),
                   "operation_id": "record", "argv": []}
        for tool in ("runtime.bind", "runtime.execute"):
            with self.subTest(tool=tool):
                with self.assertRaises(_ControlError) as caught:
                    self._dispatch(Authority(), payload=payload, tool=tool)
                self.assertEqual(caught.exception.code, "AVAILABILITY_PARKED")
        self.assertFalse(self.counter.exists())


if __name__ == "__main__":
    unittest.main()
