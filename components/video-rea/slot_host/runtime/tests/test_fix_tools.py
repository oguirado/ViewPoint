# SPDX-License-Identifier: AGPL-3.0-only
"""Fix regressions for slot_host/runtime/tools.py (triage_runtime tools items).

Each test fails on the base behaviour and passes after the fix.
"""
import copy
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from slot_host.runtime.binding import BindingRejected, sha256_of_file
from slot_host.runtime import tools as tools_mod
from slot_host.runtime.tools import (
    _ControlError,
    handle_bind,
    handle_reconcile,
    handle_replay,
    handle_slot_inspect,
    register_installation,
    main as tools_main,
    ROUTER_ENTRIES,
)
from slot_host.runtime.tests.binding_fixtures import (
    dispatch_fixture as _dispatch_orig,
    fixture_retention,
)

setUpModule = fixture_retention


class _AllowLiveAuthority:
    """Test-only authority that permits live launch (bypasses LIVE_EXECUTION_PARKED)."""
    def verify(self, *, binding, operation_id: str, request_id: str, attempt_id: str) -> bool:
        return True

def dispatch(*_args, **_kwargs):
    # Inject test stub authority so fixtures can run without LIVE_EXECUTION_PARKED
    if "live_authority" not in _kwargs or _kwargs["live_authority"] is None:
        _kwargs = dict(_kwargs)
        _kwargs["live_authority"] = _AllowLiveAuthority()
    return _dispatch_orig(*_args, **_kwargs)



_ADAPTER = "d" * 64
_PROVIDER_SHA = "a" * 64
_REQ_DIGEST = "b" * 64
_RES_DIGEST = "c" * 64


def _manifest(impl_sha, *, profile="external_process"):
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
        "upstream": {"name": "fixture", "source_url": "https://example.com/fixture", "version": "1.0.0", "licence_id": "MIT", "licence_evidence_refs": ["fixture: licence"], "component_notices": [], "user_installed": True},
        "provider_ref": {"schema": "rea.provider/v2", "provider_id": "fixture-slot", "provider_revision": "1.0.0", "provider_sha256": _PROVIDER_SHA, "implementation_sha256": impl_sha},
        "contract_refs": [
            {"role": "replay", "request_schema_ref": "urn:rea.slot_request:v2", "request_digest": _REQ_DIGEST, "result_schema_ref": "urn:rea.slot_result:v2", "result_digest": _RES_DIGEST, "execution_profile": profile},
            {"role": "verify", "request_schema_ref": "urn:rea.slot_request:v2", "request_digest": _REQ_DIGEST, "result_schema_ref": "urn:rea.slot_result:v2", "result_digest": _RES_DIGEST, "execution_profile": profile},
        ],
        "transport": {"kind": "cli_argv", "driver_location": "outside_capsule", "executable_binding": "user_selected", "argv_prefix": [], "framing": "none", "cwd_policy": "run_scratch", "env_allowlist": ["PATH"]},
        "operations": ops,
        "capabilities": {"target_kinds": ["fixture_target"], "platforms": ["linux"], "prerequisite_probes": [], "optional_features": [], "adapter_identity": {"adapter_id": "fixture.v1", "adapter_sha256": _ADAPTER, "provenance": "host_authored"}},
        "perception": {"required_level": "none", "optional_levels": [], "observation_sources": [{"id": "tool-evidence", "level": "none", "producer": "tool_stdio", "outside_capsule": True}]},
        "record_replay": {"input_codec": "json", "output_codec": "json", "observable_channels": ["error"], "state_domain": "per_case", "effects": ["process.spawn:child"], "determinism_modes": ["recorded_inputs"], "freshness_inputs": ["fixture"]},
        "comparison": {"comparator_id": "fixture.compare", "version": "1", "policy_ref": "policy:fixture@1", "coverage_requirements": {"channel": "error"}, "tolerance_units": "none", "waiver_policy": {"source": "host_policy", "binds": ["input", "actual", "reason", "scope", "policy_version"], "stale": "fail"}},
        "evidence": {"shared_schema_ref": "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2", "required_rea_fields": ["episode", "observation", "slot"], "artifact_roles": ["evidence_bundle"], "retention": "evidence_store_attempt", "visibility": "redacted_projection"},
        "conformance": {"fixture_refs": ["fixture:1"], "negative_control_refs": ["control:1"], "supported_entry_points": ["cli", "mcp", "json"], "required_checks": ["admit-and-project", "outside-record-replay-compare", "perception-and-pool", "transport-parity-and-retry"]},
        "discovery": {"summary": "fixture", "search_terms": ["fixture"], "operation_descriptions": {"record": "record", "replay": "replay", "compare": "compare"}, "examples": []},
    }


def _write(path: Path, body: bytes):
    path.write_bytes(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return sha256_of_file(path)


def _write_ok(path: Path):
    script = (
        b"#!/usr/bin/env python3\n"
        b"# SPDX-License-Identifier: AGPL-3.0-only\n"
        b"import sys, json\n"
        b"obj={\"schema\":\"rea.slot_result/v1\",\"status\":\"OK\",\"rc\":0,\"sha256\":\"e\"+\"f\"*63}\n"
        b"sys.stdout.write(json.dumps(obj)+'\\n')\n"
        b"sys.stdout.flush()\n"
    )
    return _write(path, script)


def _write_marker(path: Path, marker: Path):
    script = (
        b"#!/usr/bin/env python3\n"
        b"# SPDX-License-Identifier: AGPL-3.0-only\n"
        b"import sys, json, pathlib\n"
        b"pathlib.Path(" + repr(str(marker)).encode() + b").write_text('ran')\n"
        b"obj={\"schema\":\"rea.slot_result/v1\",\"status\":\"OK\",\"rc\":0,\"sha256\":\"e\"+\"f\"*63}\n"
        b"sys.stdout.write(json.dumps(obj)+'\\n')\n"
        b"sys.stdout.flush()\n"
    )
    return _write(path, script)


def _vision_manifest(impl_sha):
    m = _manifest(impl_sha)
    m["perception"] = {
        "required_level": "vision", "optional_levels": [],
        "observation_sources":[{"id": "vsrc", "level": "vision", "producer": "tool_stdio", "outside_capsule": True, "vision_pool": {"required": True, "owner": "ViewPoint_Tools", "ticket_field": "vision_pool_ticket"}}],
    }
    return m


class FixToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _register_ok(self, ref_suffix="fix"):
        exe = self.dir / f"fixture-{ref_suffix}.py"
        impl = _write_ok(exe)
        ref = f"installation:fixture-slot@{ref_suffix}"
        register_installation(ref, _manifest(impl), str(exe))
        return ref, exe

    def _policy(self, grants=None, **extra):
        pol = {"grants": list(grants) if grants is not None else ["grant.exec.external_process"]}
        # Supply complete authenticated host records so that exactly one
        # varied factor drives refusal; otherwise the live binding would
        # reject with PROFILE_NOT_ADMITTED before the tested gate.
        # Build registry from the latest registered manifest.
        if "registry" not in extra and "evidence_ref" not in extra:
            try:
                from slot_host.runtime.tools import _INSTALLATIONS
                last = list(_INSTALLATIONS.values())[-1]["manifest"]
                def _reg(m):
                    c = next(r for r in m["contract_refs"] if r["role"] == "replay")
                    pr = m["provider_ref"]
                    ad = m["capabilities"]["adapter_identity"]["adapter_sha256"]
                    return {"provider_sha256": pr["provider_sha256"], "provider_revision": pr["provider_revision"], "implementation_sha256": pr["implementation_sha256"], "adapter_sha256": ad, "request_digest": c["request_digest"], "result_digest": c["result_digest"], "request_schema_ref": c["request_schema_ref"], "result_schema_ref": c["result_schema_ref"], "execution_profile": c["execution_profile"]}
                pol["registry"] = _reg(last)
                pol["evidence_ref"] = "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2"
                pol["spawn_granted"] = True
                pol["licence_disposition"] = "external_execution"
                from slot_host.projections.schema import digest as _jcs_digest
                pol["manifest_digest"] = _jcs_digest(last)
                pol["slot_id"] = "fixture-slot"
                pol["probes"] = {}
            except Exception:
                pass
        pol.update(extra)
        return pol

    def test_live_public_claim_does_not_authorize(self):
        ref, _ = self._register_ok("live1")
        payload = {"installation_ref": ref, "operation_id": "record", "argv": ["record"], "host_grants": ["grant.exec.external_process"]}
        with self.assertRaises(_ControlError) as ctx:
            dispatch("behaviour.record", {"request_id": "req-fix-live-0001", "payload": payload}, host=None)
        self.assertEqual(ctx.exception.code, "NO_LEASE")

    def test_dry_run_public_claim_refused_with_nonempty_argv(self):
        ref, _ = self._register_ok("dry1")
        payload = {"installation_ref": ref, "operation_id": "record", "argv": ["record"], "host_grants": ["grant.exec.external_process"]}
        with self.assertRaises(_ControlError) as ctx:
            dispatch("behaviour.record", {"request_id": "req-fix-dry-0001", "dry_run": True, "payload": payload}, host=None, host_policy={"grants": []})
        self.assertEqual(ctx.exception.code, "NO_LEASE")
        with self.assertRaises(_ControlError) as ctx2:
            dispatch("behaviour.record", {"request_id": "req-fix-dry-0002", "dry_run": True, "payload": payload}, host=None)
        self.assertEqual(ctx2.exception.code, "NO_LEASE")

    def test_handle_bind_requires_authenticated_policy(self):
        ref, _ = self._register_ok("bind1")
        payload = {"installation_ref": ref, "operation_id": "record", "host_grants": ["grant.exec.external_process"]}
        with self.assertRaises(BindingRejected) as ctx:
            handle_bind(payload, host=None)
        self.assertEqual(ctx.exception.reason, "grant_refused")
        with self.assertRaises(BindingRejected) as parked:
            handle_bind(payload, host=None, host_policy=self._policy())
        self.assertEqual(parked.exception.reason, "availability_parked")

    def test_claim_binds_policy_and_isolates_mutation(self):
        ref, _ = self._register_ok("claim1")
        payload = {"installation_ref": ref, "operation_id": "record", "argv": [], "host_grants": ["grant.exec.external_process"]}
        r1 = dispatch("behaviour.record", {"request_id": "req-fix-claim-0001", "payload": payload}, host=None, host_policy=self._policy())
        r1["result"]["mutated"] = True
        r2 = dispatch("behaviour.record", {"request_id": "req-fix-claim-0001", "payload": payload}, host=None, host_policy=self._policy())
        self.assertNotIn("mutated", r2.get("result", {}))
        with self.assertRaises(_ControlError) as ctx:
            dispatch("behaviour.record", {"request_id": "req-fix-claim-0001", "payload": payload}, host=None, host_policy={"grants": []})
        self.assertEqual(ctx.exception.code, "REQUEST_ID_CONFLICT")

    def test_inspect_effective_grants_from_policy(self):
        ref, _ = self._register_ok("insp1")
        payload = {"installation_ref": ref, "operation_id": "record", "host_grants": ["grant.exec.external_process"]}
        insp = handle_slot_inspect(payload, host_policy={"grants": []})
        self.assertEqual(insp["effective_grants"], [])
        self.assertIn("grant.exec.external_process", insp["required_grants"])
        insp2 = handle_slot_inspect(payload, host_policy=self._policy())
        self.assertEqual(insp2["effective_grants"], ["grant.exec.external_process"])

    def test_dry_run_rejects_fixture_only_profile(self):
        exe = self.dir / "fixture-offline.py"
        impl = _write_ok(exe)
        ref = "installation:fixture-slot@offline1"
        register_installation(ref, _manifest(impl, profile="offline_fixture"), str(exe))
        payload = {"installation_ref": ref, "operation_id": "record", "argv": ["record"], "host_grants": ["grant.exec.external_process"]}
        with self.assertRaises(_ControlError) as ctx:
            dispatch("behaviour.record", {"request_id": "req-fix-prof-0001", "dry_run": True, "payload": payload}, host=None, host_policy=self._policy())
        self.assertEqual(ctx.exception.code, "PROFILE_NOT_ADMITTED")

    def test_dry_run_parked_transport(self):
        exe = self.dir / "fixture-mcp.py"
        impl = _write_ok(exe)
        m = _manifest(impl)
        m["transport"] = {"kind": "mcp_stdio", "driver_location": "outside_capsule", "executable_binding": "user_selected", "argv_prefix": [], "framing": "jsonrpc_stdio", "cwd_policy": "run_scratch", "env_allowlist": ["PATH"]}
        for op in m["operations"]:
            op["binding"] = {"kind": "mcp_tool", "tool": "ext.fixture.op"}
        ref = "installation:fixture-slot@parked1"
        register_installation(ref, m, str(exe))
        payload = {"installation_ref": ref, "operation_id": "record", "argv": ["record"], "host_grants": ["grant.exec.external_process"]}
        with self.assertRaises(_ControlError) as ctx:
            dispatch("behaviour.record", {"request_id": "req-fix-park-0001", "dry_run": True, "payload": payload}, host=None, host_policy=self._policy())
        self.assertEqual(ctx.exception.code, "AVAILABILITY_PARKED")

    def test_dry_run_respects_none_ceiling(self):
        exe = self.dir / "fixture-vision.py"
        impl = _write_ok(exe)
        ref = "installation:fixture-slot@vision1"
        register_installation(ref, _vision_manifest(impl), str(exe))
        payload = {"installation_ref": ref, "operation_id": "record", "argv": ["record"], "host_grants": ["grant.exec.external_process"]}
        with self.assertRaises(_ControlError) as ctx:
            dispatch("behaviour.record", {"request_id": "req-fix-ceil-0001", "dry_run": True, "payload": payload}, host=None, host_policy=self._policy(perception_ceiling="none"))
        self.assertEqual(ctx.exception.code, "PERCEPTION_DENIED")
        with self.assertRaises(_ControlError) as parked:
            dispatch("behaviour.record", {"request_id": "req-fix-ceil-0002", "dry_run": True, "payload": payload},
                     host=None, host_policy=self._policy(perception_ceiling="vision"))
        self.assertEqual(parked.exception.code, "AVAILABILITY_PARKED")

    def test_live_zero_spawn_under_none_ceiling(self):
        marker = self.dir / "launched.txt"
        exe = self.dir / "fixture-zero.py"
        impl = _write_marker(exe, marker)
        ref = "installation:fixture-slot@zero1"
        register_installation(ref, _vision_manifest(impl), str(exe))
        payload = {"installation_ref": ref, "operation_id": "record", "argv": ["record"], "host_grants": ["grant.exec.external_process"], "perception_level": "vision"}
        with self.assertRaises(_ControlError) as ctx:
            dispatch("behaviour.record", {"request_id": "req-fix-zero-0001", "payload": payload}, host=None, host_policy=self._policy(["grant.exec.external_process"], perception_ceiling="none"))
        self.assertEqual(ctx.exception.code, "PERCEPTION_DENIED")
        self.assertFalse(marker.exists())

    def test_public_reconcile_is_parked(self):
        with self.assertRaises(BindingRejected) as ctx:
            handle_reconcile({"interrupted_receipt": {}, "new_attempt_id": "att-x-0001"})
        self.assertEqual(ctx.exception.reason, "availability_parked")

    def test_replay_requires_replay_operation(self):
        ref, _ = self._register_ok("replay1")
        with self.assertRaises(ValueError):
            handle_replay({"installation_ref": ref, "operation_id": "record"}, request_id="req-fix-rp-0001", host_policy=self._policy())
        with self.assertRaises(ValueError):
            dispatch("behaviour.replay", {"request_id": "req-fix-rp-0002", "payload": {"installation_ref": ref, "operation_id": "record", "argv": ["replay"]}}, host=None, host_policy=self._policy())

    def test_main_maps_grant_refusal_to_no_lease(self):
        import io, sys
        old = sys.stderr
        sys.stderr = io.StringIO()
        try:
            def boom(*a, **k):
                raise BindingRejected("grant_refused", "missing")
            rc = tools_main(["mini", "behaviour.record", "--args", '{"request_id":"req-fix-main-0001","payload":{"installation_ref":"x","operation_id":"record"}}'], dispatch_fn=boom)
            self.assertEqual(rc, 11)
        finally:
            sys.stderr = old

    def test_main_maps_plain_value_error_to_schema_invalid(self):
        import io, sys
        old = sys.stderr
        sys.stderr = io.StringIO()
        try:
            def boom(*a, **k):
                raise ValueError("bad id")
            rc = tools_main(["mini", "behaviour.record", "--args", '{"request_id":"req-fix-main-0002","payload":{}}'], dispatch_fn=boom)
            self.assertEqual(rc, 14)
        finally:
            sys.stderr = old

    def test_slot_inspect_canonical_shape(self):
        self.assertEqual(ROUTER_ENTRIES["host.slot.inspect"]["cli_argv_shape"], ["slot", "inspect", "<slot_id>"])
        ref, _ = self._register_ok("canon1")
        insp = handle_slot_inspect({"slot_id": "fixture-slot", "operation_id": "record"}, host_policy=self._policy())
        self.assertEqual(insp["plan"]["operation_id"], "record")

    def test_interrupt_retains_uncertain_retry(self):
        # The start is retained before scratch acquisition. An interruption
        # there must preserve uncertainty for an identical retry.
        marker = self.dir / "launched2.txt"
        exe = self.dir / "fixture-int.py"
        impl = _write_marker(exe, marker)
        ref = "installation:fixture-slot@int1"
        register_installation(ref, _manifest(impl), str(exe))
        request = {"request_id": "req-fix-int-0001", "payload": {"installation_ref": ref, "argv": []}}
        policy = self._policy()
        retained = []
        with patch.object(tempfile, "mkdtemp", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            dispatch("behaviour.record", request, host_policy=policy, attempt_store=retained.append)
        self.assertEqual(len(retained), 2)
        self.assertEqual(retained[-1]["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
        with self.assertRaises(_ControlError) as ctx:
            dispatch("behaviour.record", request, host_policy=policy, attempt_store=retained.append)
        self.assertEqual(ctx.exception.code, "UNCERTAIN_OUTCOME")
        self.assertEqual(len(retained), 2)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
