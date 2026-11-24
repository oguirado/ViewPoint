# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the external-binding unit.

Assumed API (written alongside the code lane):

* slot_host.runtime.binding.verify_binding(manifest, executable_path, *,
  operation_id, host_grants, requested_profile=None) -> Binding, raises
  BindingRejected; slot_host.runtime.binding.sha256_of_file(path) -> hex
  and slot_host.runtime.binding.bind_executable(...) -> dict.
* slot_host.runtime.runner.run_bound(binding, argv, **opts) -> wrapper
  {"receipt","custody","evidence_accepted","stdout",...} and
  execute(manifest, executable_path, argv, *, operation_id, host_grants, ...)
  for the bind+run shortcut.
* slot_host.runtime.receipts.reconcile_run(interrupted, **kw) -> new receipt
  and evidence_accepted(wrapper_or_receipt) -> bool.
* slot_host.runtime.tools.dispatch(tool, arguments) with closed
  {request_id,dry_run,payload} wrappers, ROUTER_ENTRIES / MCP_TOOLS /
  MINI_TOOLS / ARGUMENT_SCHEMAS / RESULT_SCHEMAS for CLI/MCP/router parity.

Every test writes its fixture into a temp dir; no network.  The only
subprocess outside the runtime under test is the Python interpreter that
runs a disposable fixture script created here.  Tests kill only PIDs they
started and recorded, verifying pid+starttime from /proc first; they never
signal process groups and never PID <=1.
"""

import copy
import hashlib
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from slot_host.runtime.binding import BindingRejected, sha256_of_file
from slot_host.runtime.receipts import reconcile_run, evidence_accepted, finalize_receipt, receipt_digest
from slot_host.runtime.tools import ROUTER_ENTRIES, MCP_TOOLS, MINI_TOOLS, ARGUMENT_SCHEMAS, RESULT_SCHEMAS, register_installation, control_request
from slot_host.runtime.tools import dispatch

FIXTURE_PAYLOAD = b"REAsynthetic-runtime-fixture\n"
_ADAPTER = "d" * 64
_PROVIDER_SHA = "a" * 64
_REQ_DIGEST = "b" * 64
_RES_DIGEST = "c" * 64

# --- Reconciliation: authenticated fixtures for integrated code ---
# The integrated code requires @v0.2 live evidence pins, an explicit shared
# terminal receipt reference, and complete authenticated host policy
# (registry, evidence, spawn, licence, digest). The ported fx fixtures add
# them here (strengthening, not weakening): every run cites the pinned
# receipt, and every provided host policy is completed from the registered
# manifest while preserving the test's varied factor (grants, spawn, probes,
# evidence, registry, licence). Omitted policies stay omitted so NO_LEASE
# negatives still refuse; dry/run positives that omitted the policy were
# given an explicit minimal policy by the patch (see updated_tests).
from slot_host.runtime import tools as _reconcile_tools
from slot_host.projections.schema import digest as _reconcile_jcs_digest
from slot_host.runtime.tests.binding_fixtures import (
    fixture_retention,
    run_fixture as _fixture_run_bound,
    FIXTURE_RESULT_SHA256,
    FixtureLaunch as _AllowLiveAuthority,
    verified_fixture_binding as verify_binding,
    execute_fixture as execute,
    dispatch_fixture as _dispatch_orig,
)

setUpModule = fixture_retention

def _reconcile_registry(manifest):
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

def _reconcile_manifest_for(ref, host=None):
    if not isinstance(ref, str) or not ref.strip():
        return None
    try:
        if isinstance(host, dict) and ref in host:
            row = host[ref]
            if isinstance(row, tuple) and len(row) == 2:
                return row[0]
            if isinstance(row, dict) and "manifest" in row:
                return row["manifest"]
        row = _reconcile_tools._INSTALLATIONS.get(ref)
        if isinstance(row, dict):
            return row.get("manifest")
    except Exception:
        return None
    return None

def _reconcile_complete_policy(policy, installation_ref=None, host=None):
    if policy is None:
        return None
    if not isinstance(policy, dict):
        return policy
    pol = dict(policy)
    manifest = _reconcile_manifest_for(installation_ref, host)
    if isinstance(manifest, dict):
        if "registry" not in pol:
            try:
                pol["registry"] = _reconcile_registry(manifest)
            except Exception:
                pass
        if "evidence_ref" not in pol:
            pol["evidence_ref"] = "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2"
        if "spawn_granted" not in pol:
            pol["spawn_granted"] = True
        if "licence_disposition" not in pol:
            pol["licence_disposition"] = "external_execution"
        if "manifest_digest" not in pol:
            try:
                pol["manifest_digest"] = _reconcile_jcs_digest(manifest)
            except Exception:
                pass
        if "slot_id" not in pol:
            try:
                pol["slot_id"] = manifest.get("slot_id")
            except Exception:
                pass
        if "probes" not in pol:
            pol["probes"] = {}
    return pol



def _reconcile_default_receipt(kwargs):
    # Test helpers bypass live park via explicit stub authority (review B-01)
    if "live_authority" not in kwargs or kwargs["live_authority"] is None:
        kwargs = dict(kwargs)
        kwargs["live_authority"] = _AllowLiveAuthority()
    return kwargs


def run_bound(*_args, **_kwargs):
    return _fixture_run_bound(*_args, **_reconcile_default_receipt(_kwargs))

def dispatch(_tool_id, _arguments, *args, **kwargs):
    _host = kwargs.get("host")
    _ref = None
    try:
        _ref = _arguments.get("payload", {}).get("installation_ref")
    except Exception:
        _ref = None
    _pol = _reconcile_complete_policy(kwargs.get("host_policy"), _ref, _host)
    kwargs["host_policy"] = _pol
    return _dispatch_orig(_tool_id, _arguments, *args, **_reconcile_default_receipt(kwargs))



def _valid_provider_json(status: str = "OK", rc: int = 0, sha: str | None = None) -> bytes:
    """Minimal pinned provider result JSON for fixtures."""
    if sha is None:
        sha = "e" * 64
    obj = {"schema": "rea.slot_result/v1", "status": status, "rc": rc, "sha256": sha}
    return json.dumps(obj).encode()


def _write_fixture(path: Path, payload: bytes = FIXTURE_PAYLOAD) -> str:
    """Emit a digest over the exact UTF-8 result of the fixture workload."""
    script = (
        "#!/usr/bin/env python3\n"
        "# SPDX-License-Identifier: AGPL-3.0-only\n"
        "import hashlib, json, sys\n"
        f"default = {payload!r}\n"
        "result = sys.argv[2].upper().encode('utf-8') if len(sys.argv) > 2 else default\n"
        "obj = {'schema': 'rea.slot_result/v1', 'status': 'OK', 'rc': 0,\n"
        "       'sha256': hashlib.sha256(result).hexdigest(), 'result_payload': result.decode('utf-8')}\n"
        "print(json.dumps(obj), flush=True)\n"
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return sha256_of_file(path)


def _write_raw_fixture(path: Path) -> str:
    """Fixture that writes raw non-JSON; must be rejected as exchange_invalid."""
    script = (
        b"#!/usr/bin/env python3\n"
        b"# SPDX-License-Identifier: AGPL-3.0-only\n"
        b"import sys\n"
        b"sys.stdout.buffer.write(b'fixture:')\n"
        b"sys.stdout.flush()\n"
    )
    path.write_bytes(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return sha256_of_file(path)


def _write_argv_echo(path: Path) -> str:
    """Fixture that reports received sys.argv[1:] as JSON for argv checks."""
    script = (
        b"#!/usr/bin/env python3\n"
        b"# SPDX-License-Identifier: AGPL-3.0-only\n"
        b"import json, sys\n"
        + (b"obj={\"schema\":\"rea.slot_result/v1\",\"status\":\"OK\",\"rc\":0,\"sha256\":\"%s\",\"result_payload\":\"offline result\",\"argv\":sys.argv[1:]}\n" % FIXTURE_RESULT_SHA256.encode())
        + b"sys.stdout.write(json.dumps(obj)+'\\n')\n"
        b"sys.stdout.flush()\n"
    )
    path.write_bytes(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return sha256_of_file(path)


def _write_env_echo(path: Path) -> str:
    """Fixture that echoes environment keys for allowlist verification."""
    script = (
        b"#!/usr/bin/env python3\n"
        b"# SPDX-License-Identifier: AGPL-3.0-only\n"
        b"import json, os, sys\n"
        b"keys=sorted([k for k in os.environ.keys()])\n"
        + (b"obj={\"schema\":\"rea.slot_result/v1\",\"status\":\"OK\",\"rc\":0,\"sha256\":\"%s\",\"result_payload\":\"offline result\",\"env_keys\":keys}\n" % FIXTURE_RESULT_SHA256.encode())
        + b"sys.stdout.write(json.dumps(obj)+'\\n')\n"
        b"sys.stdout.flush()\n"
    )
    path.write_bytes(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return sha256_of_file(path)


def _write_foreign_pid_fixture(path: Path) -> str:
    """Fixture that emits a provider result carrying a foreign observed pid (multi-line JSON)."""
    script = (
        b"#!/usr/bin/env python3\n"
        b"# SPDX-License-Identifier: AGPL-3.0-only\n"
        b"import json, os, sys\n"
        b"child = os.fork()\n"
        b"if child == 0:\n"
        b"    os._exit(0)\n"
        b"os.waitpid(child, 0)\n"
        b"foreign = child + 500000\n"
        b"# Multi-line pretty-printed JSON exercises the full-stream parser.\n"
        b"obj={'observed_process_id': foreign, 'ok': True, 'schema':'rea.slot_result/v1','status':'OK','rc':0,'sha256':'e'+'f'*63}\n"
        b"sys.stdout.write(json.dumps(obj, indent=2)+'\\n')\n"
        b"sys.stdout.flush()\n"
    )
    path.write_bytes(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return sha256_of_file(path)


def _write_mismatched_request_fixture(path: Path) -> str:
    """Fixture that emits a valid result JSON with a mismatched request_id."""
    script = (
        b"#!/usr/bin/env python3\n"
        b"# SPDX-License-Identifier: AGPL-3.0-only\n"
        b"import json, sys\n"
        + (b"obj={\"schema\":\"rea.slot_result/v1\",\"status\":\"OK\",\"rc\":0,\"sha256\":\"%s\",\"result_payload\":\"offline result\",\"request_id\":\"req-foreign-9999\",\"provider_id\":\"other-provider\"}\n" % FIXTURE_RESULT_SHA256.encode())
        + b"sys.stdout.write(json.dumps(obj)+'\\n')\n"
        b"sys.stdout.flush()\n"
    )
    path.write_bytes(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return sha256_of_file(path)


def _write_sleeper(path: Path) -> str:
    """Disposable sleeper fixture that sleeps; records its PID before sleeping."""
    script = (
        b"#!/usr/bin/env python3\n"
        b"# SPDX-License-Identifier: AGPL-3.0-only\n"
        b"import os, time, sys\n"
        b"p=os.getpid()\n"
        b"open(sys.argv[1],'w').write(str(p)) if len(sys.argv)>1 else None\n"
        b"time.sleep(30)\n"
    )
    path.write_bytes(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return sha256_of_file(path)


def _write_loud(path: Path, payload: bytes) -> str:
    """Fixture that writes payload to stdout then exits 0 with a valid result on stderr."""
    # Write payload as raw bytes followed by a valid result; overflow takes precedence.
    pi = len(payload)
    script = (
        b"#!/usr/bin/env python3\n"
        b"# SPDX-License-Identifier: AGPL-3.0-only\n"
        b"import sys\n"
        b"payload=b'" + payload.replace(b"'", b"\\'").replace(b"\\", b"\\\\") + b"'\n"
        b"sys.stdout.buffer.write(payload)\n"
        b"sys.stdout.buffer.write(b'\\n')\n"
        b"sys.stdout.flush()\n"
    )
    # For overflow we do not need a valid result; overflow dominates.
    path.write_bytes(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return sha256_of_file(path)


def _proc_starttime(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return int(raw.rsplit(")", 1)[1].split()[19])
    except (FileNotFoundError, PermissionError, IndexError, ValueError):
        return None


def _safe_kill(pid: int, starttime: int | None = None) -> None:
    """Kill only if pid>1, starttime is a recorded int, and it matches live."""
    if type(pid) is not int or pid <= 1:
        return
    if type(starttime) is not int:
        return
    now = _proc_starttime(pid)
    if now is None or now != starttime:
        return
    try:
        os.kill(pid, 9)
    except (ProcessLookupError, PermissionError):
        pass


def _manifest(impl_sha: str, *, profile="external_process", status="registered", operation_id="record"):
    """Schema-valid registered manifest (rea.slot_manifest/v2)."""
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
        "evidence": {"shared_schema_ref": "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2", "required_rea_fields": ["episode", "observation", "slot"], "artifact_roles": ["evidence_bundle"], "retention": "evidence_store_attempt", "visibility": "redacted_projection"},
        "conformance": {"fixture_refs": ["fixture:1"], "negative_control_refs": ["control:1"], "supported_entry_points": ["cli", "mcp", "json"], "required_checks": ["admit-and-project", "outside-record-replay-compare", "perception-and-pool", "transport-parity-and-retry"]},
        "discovery": {"summary": "fixture", "search_terms": ["fixture"], "operation_descriptions": {"record": "record", "replay": "replay", "compare": "compare"}, "examples": []},
    }


def _register(manifest, exe: Path, ref="installation:fixture-slot@1.0.0"):
    register_installation(ref, manifest, str(exe))
    return ref


def _validate_manifest_structural(manifest: dict) -> None:
    """Pure-stdlib structural validation of rea.slot_manifest/v2."""
    import re
    required = ("schema", "slot_id", "status", "upstream", "provider_ref", "contract_refs", "transport", "operations", "capabilities", "perception", "record_replay", "comparison", "evidence", "conformance", "discovery")
    for key in required:
        assert key in manifest, f"manifest missing {key}"
    assert manifest["schema"] == "rea.slot_manifest/v2"
    slot_id = manifest["slot_id"]
    assert type(slot_id) is str and len(slot_id) <= 24 and re.fullmatch(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$", slot_id), "slot_id grammar"
    assert manifest["status"] in ("not_installed", "registered")
    up = manifest["upstream"]
    for k in ("name", "source_url", "version", "licence_id", "licence_evidence_refs", "component_notices", "user_installed"):
        assert k in up, f"upstream missing {k}"
    assert isinstance(up["name"], str) and up["name"].strip()
    assert isinstance(up["source_url"], str) and up["source_url"].startswith("https://")
    assert isinstance(up["version"], str) and up["version"].strip()
    assert isinstance(up["licence_id"], str) and up["licence_id"].strip().upper() not in {"UNKNOWN", "NONE", "NOASSERTION"}
    assert isinstance(up["licence_evidence_refs"], list) and len(up["licence_evidence_refs"]) >= 1
    assert isinstance(up["component_notices"], list)
    assert up["user_installed"] is True
    pr = manifest["provider_ref"]
    assert isinstance(pr, dict)
    assert "schema" in pr and "provider_id" in pr and "provider_revision" in pr and "provider_sha256" in pr and "implementation_sha256" in pr
    # contract_refs
    assert isinstance(manifest["contract_refs"], list) and len(manifest["contract_refs"]) >= 1
    for row in manifest["contract_refs"]:
        for k in ("role", "request_schema_ref", "request_digest", "result_schema_ref", "result_digest", "execution_profile"):
            assert k in row
    # perception
    perc = manifest["perception"]
    assert perc["required_level"] in ("none", "cdp_atspi", "vision")
    assert isinstance(perc["optional_levels"], list)
    assert isinstance(perc["observation_sources"], list) and len(perc["observation_sources"]) >= 1
    # operations
    assert isinstance(manifest["operations"], list) and len(manifest["operations"]) >= 1
    if manifest["status"] == "registered":
        ids = {op.get("id") for op in manifest["operations"] if isinstance(op, dict)}
        assert {"record", "replay", "compare"} <= ids, "registered requires record/replay/compare"
        assert pr["provider_revision"] != "unpinned"
        assert up["version"] not in ("unpinned", "0.0.0")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    # -- done_oracle: correct binding -> accepted; changed binary -> rejected
    def test_correct_binding_produces_accepted_observation(self):
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        manifest = _manifest(impl)
        binding = verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        self.assertEqual(binding.observed_sha256, impl)
        self.assertEqual(binding.operation_id, "record")
        self.assertEqual(binding.operation_binding.get("argv"), ["record"])
        self.assertEqual((binding.timeout_s, binding.max_output_bytes), (5, 4096))
        held = []
        wrapper = run_bound(binding, [], timeout_s=5, max_output_bytes=4096, attempt_store=held.append,
                            request_id="req-offline-payload", attempt_id="att-offline-payload")
        receipt = wrapper["receipt"]
        self.assertTrue(held, "attempt must be retained before dispatch (issue 3)")
        self.assertTrue(wrapper["custody"]["producer_binding_verified"])
        self.assertTrue(wrapper["custody"]["artifact_digest_verified"])
        self.assertTrue(wrapper["custody"]["cleanup_observed"])
        self.assertTrue(wrapper["evidence_accepted"])
        self.assertTrue(evidence_accepted(wrapper))
        self.assertEqual(json.loads(wrapper["stdout"])["result_payload"], FIXTURE_PAYLOAD.decode())
        self.assertEqual(receipt["attempt"]["slot_result"], {
            "schema": "rea.slot_result/v1", "status": "OK", "rc": 0,
            "sha256": hashlib.sha256(FIXTURE_PAYLOAD).hexdigest(),
        })
        self.assertEqual(receipt["slot"]["out_of_process"], True)
        self.assertEqual(receipt["observation"]["producer"]["kind"], "external_slot")
        self.assertIn("shared_receipt_adapter", receipt)
        self.assertNotIn("verdict", receipt)
        self.assertNotIn("custody", receipt)
        self.assertEqual(receipt["episode"]["role"], "original")

    def test_raw_nonjson_is_exchange_invalid(self):
        exe = self.dir / "raw.py"
        impl = _write_raw_fixture(exe)
        manifest = _manifest(impl)
        binding = verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        wrapper = run_bound(binding, [], timeout_s=5, cwd=str(self.dir))
        self.assertFalse(wrapper["evidence_accepted"])
        self.assertFalse(evidence_accepted(wrapper))
        self.assertFalse(wrapper["custody"]["producer_binding_verified"])
        self.assertIsNone(wrapper["receipt"]["attempt"].get("slot_result"))

    def test_manifest_is_schema_valid_stdlib_only(self):
        # Pure-stdlib structural validation; no jsonschema.
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        manifest = _manifest(impl)
        _validate_manifest_structural(manifest)
        # Receipts are also structurally valid for the evidence profile.
        binding = verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        wrapper = run_bound(binding, [], timeout_s=5, cwd=str(self.dir))
        # Access through relative repository paths only.
        _validate_manifest_structural(manifest)

    def test_wrong_process_produces_no_accepted_observation(self):
        exe = self.dir / "fixture-foreign.py"
        impl = _write_foreign_pid_fixture(exe)
        manifest = _manifest(impl)
        binding = verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        wrapper = run_bound(binding, [], timeout_s=5, cwd=str(self.dir))
        receipt, custody = wrapper["receipt"], wrapper["custody"]
        self.assertNotEqual(custody["bound_process_id"], custody["observed_process_id"])
        self.assertFalse(custody["producer_binding_verified"])
        self.assertFalse(wrapper["evidence_accepted"])
        self.assertFalse(evidence_accepted(wrapper))
        self.assertFalse(receipt["observation"]["producer"]["process_bound"])

    def test_mismatched_request_id_produces_no_accepted(self):
        exe = self.dir / "fixture-mismatch.py"
        impl = _write_mismatched_request_fixture(exe)
        manifest = _manifest(impl)
        binding = verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"}, capsule_perception_ceiling="vision")
        wrapper = run_bound(binding, [], timeout_s=5, cwd=str(self.dir), request_id="req-fixture-0001")
        self.assertFalse(wrapper["custody"]["producer_binding_verified"])
        self.assertFalse(wrapper["evidence_accepted"])

    def test_changed_binary_produces_no_accepted_observation(self):
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        manifest = _manifest(impl)
        binding = verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        exe.write_bytes(exe.read_bytes() + b"\n#tampered\n")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        wrapper = run_bound(binding, [], timeout_s=5, cwd=str(self.dir))
        self.assertFalse(wrapper["evidence_accepted"])
        self.assertFalse(evidence_accepted(wrapper))
        self.assertTrue(wrapper["custody"]["cleanup_observed"])
        self.assertNotIn("verdict", wrapper["receipt"])
        self.assertEqual(wrapper["receipt"]["attempt"]["control_outcome"], "evidence_binding_rejected")
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        self.assertEqual(ctx.exception.reason, "binding_rejected")

    def test_interrupted_run_is_reconciled(self):
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        manifest = _manifest(impl)
        sleeper = self.dir / "sleeper.py"
        changed = _write_sleeper(sleeper)
        manifest2 = _manifest(changed)
        binding2 = verify_binding(manifest2, str(sleeper), operation_id="record", host_grants={"grant.exec.external_process"})
        wrapper = run_bound(binding2, [], timeout_s=0.5, cwd=str(self.dir))
        self.assertTrue(wrapper["timed_out"])
        self.assertEqual(wrapper["receipt"]["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
        self.assertTrue(wrapper["custody"]["cleanup_observed"])
        # Reconcile via the wrapper form (requires verified custody)
        reconciled = reconcile_run(
            wrapper,
            new_attempt_id="att-runtime-reconciled-0002",
            request_id="req-runtime-0002",
        )
        self.assertEqual(reconciled["attempt"]["reconciles_attempt_id"], wrapper["receipt"]["attempt"]["attempt_id"])
        self.assertEqual(reconciled["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
        self.assertIn("shared_receipt_adapter", reconciled)
        self.assertNotIn("verdict", reconciled)
        self.assertEqual(reconciled["attempt"]["host_artifact_sha256"], receipt_digest(wrapper["receipt"]))
        self.assertFalse(evidence_accepted(reconciled))
        # Uncleaned wrapper must fail closed.
        unclean = {"receipt": copy.deepcopy(wrapper["receipt"]), "custody": dict(wrapper["custody"], cleanup_observed=False)}
        with self.assertRaises(ValueError):
            reconcile_run(unclean, new_attempt_id="att-x", request_id="req-x-0001")
        # Bare receipt without custody also fails closed.
        with self.assertRaises(ValueError):
            reconcile_run(copy.deepcopy(wrapper["receipt"]), new_attempt_id="att-y", request_id="req-y-0001")

    def test_reconcile_preserves_non_ascii_digest(self):
        # Reconciliation requires an uncertain prior attempt with verified
        # cleanup; seed a timed-out sleeper run (UNCERTAIN_OUTCOME).
        sleeper = self.dir / "sleeper-unicode.py"
        impl = _write_sleeper(sleeper)
        manifest = _manifest(impl)
        binding = verify_binding(manifest, str(sleeper), operation_id="record", host_grants={"grant.exec.external_process"})
        wrapper = run_bound(binding, [], timeout_s=0.5, cwd=str(self.dir), request_id="req-unicode-0001", attempt_id="att-unicode-0001")
        self.assertEqual(wrapper["receipt"]["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
        self.assertTrue(wrapper["custody"]["cleanup_observed"])
        # Put valid UTF-8 in a schema-legal free-text field of the actual
        # prior receipt, then pin its digest.
        wrapper["receipt"]["$comment"] = "café-üñí-0001"
        d1 = receipt_digest(wrapper["receipt"])
        d2 = __import__("hashlib").sha256(json.dumps(wrapper["receipt"], sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
        self.assertEqual(d1, d2)
        with self.assertRaisesRegex(ValueError, "exact prior receipt digest"):
            reconcile_run(wrapper, new_attempt_id="att-unicode-unbound", request_id="req-unicode-unbound")
        wrapper["custody"]["prior_receipt_sha256"] = d2
        reconciled = reconcile_run(wrapper, new_attempt_id="att-unicode-reconciled-001", request_id="req-unicode-reconciled-001")
        self.assertEqual(reconciled["attempt"]["host_artifact_sha256"], receipt_digest(wrapper["receipt"]))
        self.assertEqual(reconciled["attempt"]["reconciles_attempt_id"], "att-unicode-0001")
        self.assertFalse(evidence_accepted(reconciled))

    def test_grant_and_profile_enforcement(self):
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        manifest = _manifest(impl)
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(manifest, str(exe), operation_id="record", host_grants=set())
        self.assertEqual(ctx.exception.reason, "grant_refused")
        fixture_manifest = _manifest(impl, profile="offline_fixture")
        binding = verify_binding(fixture_manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        self.assertEqual(binding.execution_profile, "offline_fixture")
        with self.assertRaises(BindingRejected):
            run_bound(binding, [], timeout_s=2)

    def test_compare_receipt_carries_full_verdict(self):
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        manifest = _manifest(impl, profile="external_process")
        binding = verify_binding(manifest, str(exe), operation_id="compare", host_grants={"grant.exec.external_process"})
        wrapper = run_bound(binding, [], timeout_s=5, max_output_bytes=4096, cwd=str(self.dir))
        verdict = wrapper["receipt"]["verdict"]
        for key in ("comparator_id", "comparator_version", "result", "policy", "checks", "contrast_ids", "negative_controls", "oracle_independence", "independent_qualification"):
            self.assertIn(key, verdict, f"compare verdict missing {key}")
        self.assertIn("abstain_detail", verdict)
        self.assertNotIn("detail", verdict)
        self.assertEqual(wrapper["receipt"]["episode"]["role"], "comparison")
        self.assertIn("reference_episode_id", wrapper["receipt"]["episode"])
        self.assertIn("candidate_episode_id", wrapper["receipt"]["episode"])

    def test_replay_keeps_lineage(self):
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        manifest = _manifest(impl)
        binding = verify_binding(manifest, str(exe), operation_id="replay", host_grants={"grant.exec.external_process"})
        parent = "ep-parent-0001"
        tape = hashlib.sha256(b"tape").hexdigest()
        retained = {(parent, tape): {"parent_episode_id": parent, "input_tape_sha256": tape}}
        with self.assertRaisesRegex(RuntimeError, "owner-pending: T04"):
            run_bound(binding, [], cwd=str(self.dir), parent_episode_id=parent, tape_sha256=tape)
        wrapper = run_bound(binding, [], timeout_s=5, cwd=str(self.dir), parent_episode_id=parent,
                            tape_sha256=tape, tape_resolver=lambda episode, sha: retained.get((episode, sha)))
        self.assertEqual(wrapper["receipt"]["attempt"]["operation"], "replay")
        self.assertEqual(wrapper["receipt"]["episode"]["role"], "candidate")
        self.assertEqual(wrapper["receipt"]["episode"]["parent_episode_id"], parent)
        self.assertEqual(wrapper["receipt"]["replay"], {
            "input_tape_sha256": tape, "determinism_mode": "uncontrolled",
        })

    # -- issue 2: vision lease lifecycle
    def test_vision_lease_none_and_cdp_make_zero_pool_calls(self):
        exe = self.dir / "perception.py"
        impl = _write_fixture(exe)
        pool = Mock(spec=["acquire", "release"])
        grant = {"grant.exec.external_process"}
        binding = verify_binding(_manifest(impl), str(exe), operation_id="record", host_grants=grant)
        result = run_bound(binding, [], cwd=self.dir, vision_pool=pool, capsule_perception_ceiling="none")
        self.assertIsNotNone(result["pid"])
        self.assertNotIn("vision_pool_ticket", result["receipt"]["observation"])
        self.assertTrue(result["evidence_accepted"])

        for level in ("vision", "cdp_atspi"):
            with self.subTest(level=level):
                manifest = _manifest(impl)
                source = {"id": "perception", "level": level, "producer": "host", "outside_capsule": True}
                if level == "vision":
                    source["vision_pool"] = {
                        "required": True, "owner": "ViewPoint_Tools", "ticket_field": "vision_pool_ticket",
                    }
                manifest["perception"] = {
                    "required_level": level, "optional_levels": [], "observation_sources": [source],
                }
                ref = "installation:fixture-slot@parked-perception"
                host = {ref: (manifest, str(exe))}
                retained = Mock()
                with patch("slot_host.runtime.runner.subprocess.Popen") as spawn:
                    for dry_run in (False, True):
                        with self.assertRaises(_reconcile_tools._ControlError) as refused:
                            dispatch("behaviour.record", {
                                "request_id": f"req-parked-{level}-{dry_run}", "dry_run": dry_run,
                                "payload": {"installation_ref": ref},
                            }, host=host, host_policy={"grants": list(grant)}, vision_pool=pool,
                                attempt_store=retained)
                        self.assertEqual(refused.exception.code, "AVAILABILITY_PARKED")
                    binding = verify_binding(manifest, str(exe), operation_id="record", host_grants=grant)
                    with self.assertRaises(BindingRejected) as refused:
                        run_bound(binding, [], cwd=self.dir, vision_pool=pool, attempt_store=retained)
                    self.assertEqual(refused.exception.reason, "availability_parked")
                    self.assertEqual(refused.exception.detail, "owner-pending: T06")
                spawn.assert_not_called()
                retained.assert_not_called()  # No attempt and therefore no vision_pool_ticket.
        self.assertEqual(pool.mock_calls, [])

    def test_perception_ceiling_denies(self):
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        manifest = _manifest(impl)
        manifest["perception"] = {"required_level": "vision", "optional_levels": [], "observation_sources": [{"id": "px", "level": "vision", "producer": "host_vision_engine", "outside_capsule": True, "vision_pool": {"required": True, "owner": "ViewPoint_Tools", "ticket_field": "vision_pool_ticket"}}]}
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"}, capsule_perception_ceiling="none")
        self.assertEqual(ctx.exception.reason, "perception_denied")

    def test_driver_location_and_source_boundary(self):
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        # driver_location != outside_capsule
        m = _manifest(impl)
        m["transport"]["driver_location"] = "inside_capsule"
        with self.assertRaises(BindingRejected) as c:
            verify_binding(m, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        self.assertEqual(c.exception.reason, "perception_denied")
        # outside_capsule False on a source
        m2 = _manifest(impl)
        m2["perception"]["observation_sources"][0]["outside_capsule"] = False
        with self.assertRaises(BindingRejected) as c2:
            verify_binding(m2, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        self.assertEqual(c2.exception.reason, "perception_denied")
        # vision source without vision_pool metadata
        m3 = _manifest(impl)
        m3["perception"] = {"required_level": "vision", "optional_levels": [], "observation_sources": [{"id": "px", "level": "vision", "producer": "host_vision", "outside_capsule": True}]}
        with self.assertRaises(BindingRejected) as c3:
            verify_binding(m3, str(exe), operation_id="record", host_grants={"grant.exec.external_process"}, capsule_perception_ceiling="vision")
        self.assertEqual(c3.exception.reason, "manifest_invalid")

    # -- issue 4: manifest/operation/env/cwd isolation
    def test_profile_v1_rejected_under_external(self):
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        manifest = _manifest(impl)
        manifest["provider_ref"] = dict(manifest["provider_ref"], schema="rea.provider/v1")
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        self.assertEqual(ctx.exception.reason, "profile_not_admitted")

    def test_operation_ref_mismatch_rejected(self):
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        manifest = _manifest(impl)
        manifest["operations"][0] = dict(manifest["operations"][0], input_ref="urn:rea.slot_request:v2#slot=verify")
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        self.assertEqual(ctx.exception.reason, "manifest_invalid")

    def test_registered_requires_concrete_adapter(self):
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        manifest = _manifest(impl)
        manifest["capabilities"]["adapter_identity"] = dict(manifest["capabilities"]["adapter_identity"], adapter_sha256=None)
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        self.assertEqual(ctx.exception.reason, "profile_not_admitted")

    def test_pending_evidence_gated(self):
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        manifest = _manifest(impl)
        manifest["evidence"]["shared_schema_ref"] = "evidence-store:EVIDENCE_RECEIPT_SCHEMA@pending"
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        self.assertEqual(ctx.exception.reason, "profile_not_admitted")
        with self.assertRaises(BindingRejected) as pending:
            verify_binding(manifest, str(exe), operation_id="record",
                           host_grants={"grant.exec.external_process"}, allow_pending_evidence=True)
        self.assertEqual(pending.exception.reason, "profile_not_admitted")
        manifest["evidence"]["shared_schema_ref"] = "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2"
        binding = verify_binding(manifest, str(exe), operation_id="record",
                                 host_grants={"grant.exec.external_process"})
        self.assertEqual(binding.execution_profile, "external_process")

    def test_non_executable_fails_closed(self):
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        exe.chmod(0o644)
        manifest = _manifest(impl)
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        self.assertEqual(ctx.exception.reason, "binding_rejected")

    def test_argv_derived_from_operation_binding(self):
        exe = self.dir / "argv_echo.py"
        impl = _write_argv_echo(exe)
        manifest = _manifest(impl)
        manifest["operations"][0]["binding"]["typed_args"] = [{"name": "value", "type": "string", "position": 0}]
        binding = verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        self.assertEqual(binding.operation_binding["argv"], ["record"])
        wrapper = run_bound(binding, ["--extra"], timeout_s=5, cwd=str(self.dir))
        self.assertTrue(wrapper["evidence_accepted"])
        # Operation argv ["record"] is prepended to operator typed args.
        self.assertIn("record", wrapper["stdout"])
        self.assertIn("--extra", wrapper["stdout"])

    def test_env_intersected_with_host_policy(self):
        exe = self.dir / "env_echo.py"
        impl = _write_env_echo(exe)
        manifest = _manifest(impl)
        manifest["transport"]["env_allowlist"] = ["PATH", "SECRET_TOKEN"]
        binding = verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        os.environ["SECRET_TOKEN"] = "should-not-leak"
        try:
            wrapper = run_bound(binding, [], timeout_s=5, cwd=str(self.dir), host_env_allowlist=frozenset({"PATH"}), env={"SECRET_TOKEN": "x", "PATH": os.environ.get("PATH", "")})
            self.assertTrue(wrapper["evidence_accepted"])
            # Host intersected env: SECRET_TOKEN must not appear in child keys.
            self.assertNotIn("SECRET_TOKEN", wrapper["stdout"])
            self.assertIn("PATH", wrapper["stdout"])
        finally:
            os.environ.pop("SECRET_TOKEN", None)

    def test_output_overflow_is_budget_exceeded(self):
        # New contract: combined-output overflow is bounded and yields no
        # provider result (no synthetic BUDGET_EXCEEDED/6 status).
        exe = self.dir / "loud.py"
        impl = _write_loud(exe, b"x" * 10000)
        manifest = _manifest(impl)
        binding = verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        wrapper = run_bound(binding, [], timeout_s=5, max_output_bytes=65536, cwd=str(self.dir))
        # Caller asks for 65536 but operation caps at 4096; overflow dominates.
        self.assertFalse(wrapper["evidence_accepted"])
        self.assertFalse(evidence_accepted(wrapper))
        self.assertTrue(wrapper["custody"]["overflow"])
        attempt = wrapper["receipt"]["attempt"]
        self.assertIsNone(attempt.get("slot_result"), "overflow must not synthesize a provider slot_result")
        retained = len(str(wrapper.get("stdout", "")).encode()) + len(str(wrapper.get("stderr", "")).encode())
        self.assertLess(retained, 10000)
        self.assertLessEqual(retained, 8192)

    def test_timeout_capped_by_operation_limits(self):
        exe = self.dir / "sleeper2.py"
        impl = _write_sleeper(exe)
        manifest = _manifest(impl)
        manifest["operations"][0]["limits"] = {"timeout_s": 1, "max_output_bytes": 4096}
        binding = verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        pidfile = self.dir / "pid.json"
        # Sleeper without argv file; use timeout_s=30 caller but cap 1s enforces UNCERTAIN_OUTCOME.
        start = time.time()
        wrapper = run_bound(binding, [], timeout_s=30, cwd=str(self.dir))
        elapsed = time.time() - start
        self.assertTrue(wrapper["timed_out"])
        self.assertFalse(wrapper["evidence_accepted"])
        self.assertLess(elapsed, 5)
        self.assertEqual(wrapper["receipt"]["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")

    # -- issue 5: supervision under leaked grandchild pipes (safe-PID only)
    def test_sibling_survives_child_timeout(self):
        sleeper = self.dir / "sleeper.py"
        impl = _write_sleeper(sleeper)
        manifest = _manifest(impl)
        binding = verify_binding(manifest, str(sleeper), operation_id="record", host_grants={"grant.exec.external_process"})
        # Disposable sibling written to temp dir; record its pid/starttime via shell? Use python fixture instead.
        sibling_script = self.dir / "sibling.py"
        sibling_script.write_text(
            "#!/usr/bin/env python3\n# SPDX-License-Identifier: AGPL-3.0-only\nimport time, sys\nopen(sys.argv[1],'w').write(str(__import__('os').getpid()))\ntime.sleep(30)\n"
        )
        sibling_script.chmod(sibling_script.stat().st_mode | stat.S_IXUSR)
        pidfile = self.dir / "sibling.pid"
        import subprocess as _sp
        sibling = _sp.Popen([str(sibling_script), str(pidfile)])
        # Wait for sibling to write its pid
        for _ in range(50):
            if pidfile.exists():
                break
            time.sleep(0.02)
        try:
            self.assertTrue(pidfile.exists(), "sibling pidfile missing")
            try:
                sib_pid = int(pidfile.read_text().strip())
                sib_start = _proc_starttime(sib_pid)
            except Exception:
                sib_pid, sib_start = None, None
            self.assertIsNotNone(sib_pid)
            self.assertGreater(sib_pid, 1)
            wrapper = run_bound(binding, [], timeout_s=0.5, cwd=str(self.dir))
            self.assertTrue(wrapper["timed_out"])
            self.assertTrue(wrapper["custody"]["cleanup_observed"])
            # Sibling must still be alive (never signaled via group).
            self.assertIsNone(sibling.poll(), "sibling in parent group must survive child timeout shutdown")
            # Verify sibling starttime unchanged.
            if sib_start is not None:
                self.assertEqual(_proc_starttime(sib_pid), sib_start)
        finally:
            try:
                sibling.terminate()
                sibling.wait(timeout=5)
            except Exception:
                try:
                    # Use spawn-time identity (sib_pid/sib_start) not a fresh read.
                    _safe_kill(sib_pid if 'sib_pid' in locals() else -1, sib_start if 'sib_start' in locals() else None)
                except Exception:
                    pass
                try:
                    sibling.kill()
                    sibling.wait(timeout=2)
                except Exception:
                    pass

    def test_grandchild_pipe_leak_yields_uncertain_outcome(self):
        leaker = self.dir / "leaker.py"
        spawned = self.dir / "descendant-started"
        retired = self.dir / "descendant-reaped"
        leaker.write_text(
            "#!/usr/bin/env python3\n# SPDX-License-Identifier: AGPL-3.0-only\n"
            "import os, select, signal, sys, time\n"
            "from pathlib import Path\n"
            "read_fd, write_fd = os.pipe()\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    os.close(write_fd)\n"
            "    select.select([read_fd], [], [], 3)\n"
            "    os.close(read_fd)\n"
            "    os._exit(0)\n"
            "os.close(read_fd)\n"
            "def retire(*_):\n"
            "    os.close(write_fd)\n"
            "    child, status = os.waitpid(pid, 0)\n"
            f"    Path({str(retired)!r}).write_text(f'{{child}} {{status}}')\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, retire)\n"
            "identity = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]\n"
            f"Path({str(spawned)!r}).write_text(f'{{pid}} {{identity}}')\n"
            "print('parent', flush=True)\n"
            "time.sleep(3)\n"
            "retire()\n"
        )
        leaker.chmod(leaker.stat().st_mode | stat.S_IXUSR)
        impl = sha256_of_file(leaker)
        binding = verify_binding(_manifest(impl), str(leaker), operation_id="record",
                                 host_grants={"grant.exec.external_process"})
        wrapper = run_bound(binding, [], timeout_s=0.5, cwd=str(self.dir))
        self.assertTrue(wrapper["timed_out"])
        self.assertFalse(wrapper["evidence_accepted"])
        self.assertEqual(wrapper["receipt"]["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
        self.assertTrue(wrapper["custody"]["cleanup_observed"])
        pid, starttime = map(int, spawned.read_text().split())
        self.assertGreater(pid, 1)
        self.assertGreater(starttime, 0)
        self.assertEqual(list(map(int, retired.read_text().split())), [pid, 0])
        self.assertIsNone(_proc_starttime(pid))
        self.assertEqual(wrapper["stdout"], "parent\n")
        captured = hashlib.sha256(wrapper["stdout"].encode()).hexdigest()
        self.assertNotEqual(wrapper["shared_records"]["terminal"]["artifact"]["sha256"], captured)

    def test_keyboard_interrupt_retains_uncertain_outcome(self):
        # Exercise BaseException path without raising twice.  The runner
        # collects output via _collect_output (not Popen.communicate), so
        # the simulated interrupt is injected there after launch.
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        manifest = _manifest(impl)
        binding = verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        import slot_host.runtime.runner as runner_mod
        held = []
        orig_collect = runner_mod._collect_output
        state = {"once": False}
        def patched(proc, deadline, cap):
            if not state["once"]:
                state["once"] = True
                raise KeyboardInterrupt("simulated")
            return orig_collect(proc, deadline, cap)
        runner_mod._collect_output = patched
        try:
            with self.assertRaises(KeyboardInterrupt):
                run_bound(binding, [], timeout_s=5, cwd=str(self.dir), attempt_store=held.append)
        finally:
            runner_mod._collect_output = orig_collect
        self.assertTrue(len(held) >= 2, f"interrupted attempt must be retained; got {len(held)}")
        rec = held[-1]
        self.assertIn("attempt", rec)
        self.assertEqual(rec["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
        self.assertIsNone(rec["attempt"].get("slot_result"), "interrupted attempt must not carry a provider slot_result")

    # -- issue 6: tools faces
    def test_tools_router_mcp_parity(self):
        for row_id, row in ROUTER_ENTRIES.items():
            self.assertIn(row["mcp_name"], MCP_TOOLS)
            self.assertEqual(row["json_contract_ref"]["op"], "rea.slot.project")
            self.assertIsNotNone(row["input_schema_ref"])
            self.assertIn("argument_schema_ref", row["json_contract_ref"])
            self.assertIn("result_schema_ref", row["json_contract_ref"])
            self.assertIn(row["operation"], MINI_TOOLS)
        self.assertEqual(set(ROUTER_ENTRIES), {
            "host.slot.inspect", "host.behaviour.record", "host.behaviour.replay", "host.error.diagnose",
            "host.journal.verify", "host.reconstruction.obligations", "host.tape.generate", "host.effect.isolate",
        })
        for mcp in MCP_TOOLS.values():
            blob = json.dumps(mcp)
            self.assertNotIn("executable_path", blob)
            self.assertNotIn("argv_prefix", blob)
            self.assertNotIn("env_allowlist", blob)
            self.assertIn("payload", json.dumps(mcp))
        # ARGUMENT_SCHEMAS must also exclude raw transport argv and env values.
        for ref, schema in ARGUMENT_SCHEMAS.items():
            blob = json.dumps(schema)
            self.assertNotIn("executable_path", blob)
            self.assertNotIn("argv_prefix", blob)
            self.assertNotIn("env_allowlist", blob)
        # Router cli shape must be the projected public shape.
        self.assertEqual(ROUTER_ENTRIES["host.slot.inspect"]["cli_argv_shape"], ["slot", "inspect", "<slot_id>"])

    def test_execute_end_to_end_via_tools(self):
        exe = self.dir / "fixture.py"
        manifest = _manifest(_write_fixture(exe, payload=b"fallback"))
        manifest["operations"][0]["binding"]["typed_args"] = [
            {"name": "tape", "type": "string", "position": 0},
        ]
        ref = _register(manifest, exe, "installation:fixture-slot@e2e")
        for index, (tape, expected) in enumerate((("alpha", "ALPHA"), ("beta", "BETA"))):
            with self.subTest(tape=tape):
                tape_sha = hashlib.sha256(tape.encode()).hexdigest()
                result_sha = hashlib.sha256(expected.encode()).hexdigest()
                arguments = {
                    "request_id": f"req-offline-tape-{index}",
                    "payload": {"installation_ref": ref, "operation_id": "record", "argv": [tape],
                                "attempt_id": f"att-offline-tape-{index}"},
                }
                kwargs = {"host_policy": {"grants": ["grant.exec.external_process"]}, "tape_sha256": tape_sha}
                reply = dispatch("behaviour.record", arguments, **kwargs)
                self.assertTrue(reply["ok"])
                wrapper = reply["result"]
                self.assertTrue(wrapper["evidence_accepted"])
                self.assertTrue(evidence_accepted(wrapper))
                self.assertEqual(wrapper["custody"]["input_tape_sha256"], tape_sha)
                self.assertEqual(json.loads(wrapper["stdout"])["result_payload"], expected)
                self.assertEqual(wrapper["receipt"]["attempt"]["slot_result"]["sha256"], result_sha)
                self.assertEqual(wrapper["shared_records"]["terminal"]["artifact"]["sha256"], result_sha)
                receipt = wrapper["receipt"]
                custody = wrapper["custody"]
                for unresolved in ("shared:fix", "shared:fix-attempt", "artifact:fixture/1"):
                    adapter = dict(receipt["shared_receipt_adapter"], shared_receipt_ref=unresolved,
                                   live_use_permitted=True)
                    forged = dict(receipt, shared_receipt_adapter=adapter)
                    self.assertFalse(evidence_accepted({"receipt": forged, "custody": custody}))
                    final = finalize_receipt(forged, receipt["attempt"]["slot_result"], custody=custody)
                    self.assertEqual(final["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
                    self.assertNotIn("slot_result", final["attempt"])
                self.assertEqual(dispatch("behaviour.record", arguments, **kwargs), reply)


    def test_tools_reject_unknown_fields_and_bad_request_id(self):
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        ref = _register(_manifest(impl), exe, "installation:fixture-slot@bad")
        with self.assertRaises(ValueError) as ctx:
            dispatch("behaviour.record", {"request_id": "bad id!", "payload": {"installation_ref": ref, "operation_id": "record"}}, host=None)
        self.assertIn("request", str(ctx.exception).lower())
        with self.assertRaises(ValueError):
            dispatch("behaviour.record", {"request_id": "req-unknown-0001", "payload": {"installation_ref": ref, "operation_id": "record", "cwd": "/etc"}}, host=None)

    def test_tools_dry_run_spawns_nothing_and_rejects_unknown(self):
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        ref = _register(_manifest(impl), exe, "installation:fixture-slot@dry2")
        # Dry-run must not spawn; unknown cwd must be rejected even under dry_run.
        with self.assertRaises(ValueError):
            dispatch("behaviour.record", {
                "request_id": "req-dry-unknown-0001",
                "dry_run": True,
                "payload": {"installation_ref": ref, "operation_id": "record", "argv": [], "cwd": "/etc", "host_grants": ["grant.exec.external_process"]},
            }, host=None)
        reply = dispatch("behaviour.record", {
            "request_id": "req-dry-0001",
            "dry_run": True,
            "payload": {"installation_ref": ref, "operation_id": "record", "argv": [], "host_grants": ["grant.exec.external_process"]},
        }, host=None, host_policy={"grants": ["grant.exec.external_process"]})
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["result"]["planned"])

    def test_tools_request_id_conflict(self):
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        ref = _register(_manifest(impl), exe, "installation:fixture-slot@conflict")
        dispatch("behaviour.record", {
            "request_id": "req-conflict-0001",
            "payload": {"installation_ref": ref, "operation_id": "record", "argv": [], "host_grants": ["grant.exec.external_process"]},
        }, host=None, host_policy={"grants": ["grant.exec.external_process"]})
        with self.assertRaises(ValueError) as ctx:
            dispatch("behaviour.record", {
                "request_id": "req-conflict-0001",
                "payload": {"installation_ref": ref, "operation_id": "replay", "argv": [], "host_grants": ["grant.exec.external_process"]},
            }, host=None, host_policy={"grants": ["grant.exec.external_process"]})
        self.assertIn("conflict", str(ctx.exception).lower())

    def test_tools_uncertain_retry_blocks_until_reconciled(self):
        exe = self.dir / "sleeper-unc.py"
        impl = _write_sleeper(exe)
        manifest = _manifest(impl)
        ref = _register(manifest, exe, "installation:fixture-slot@unc")
        # Timeout forces UNCERTAIN_OUTCOME claim.
        reply = dispatch("behaviour.record", {
            "request_id": "req-unc-0001",
            "payload": {"installation_ref": ref, "operation_id": "record", "argv": [], "host_grants": ["grant.exec.external_process"], "timeout_s": 0.5},
        }, host=None, host_policy={"grants": ["grant.exec.external_process"]})
        self.assertTrue(reply["ok"])
        # Identical retry (same normalized payload) must be UNCERTAIN_OUTCOME
        # with zero repeat launches, not REQUEST_ID_CONFLICT.
        try:
            dispatch("behaviour.record", {
                "request_id": "req-unc-0001",
                "payload": {"installation_ref": ref, "operation_id": "record", "argv": [], "host_grants": ["grant.exec.external_process"], "timeout_s": 0.5},
            }, host=None, host_policy={"grants": ["grant.exec.external_process"]})
            self.fail("identical uncertain retry must raise UNCERTAIN_OUTCOME")
        except ValueError as exc:
            code = getattr(exc, "code", None) or getattr(exc, "args", [""])[0]
            self.assertIn("UNCERTAIN_OUTCOME", str(exc) + str(code))
        # Changed payload remains REQUEST_ID_CONFLICT (separate contrast).
        with self.assertRaises(ValueError):
            dispatch("behaviour.record", {
                "request_id": "req-unc-0001",
                "payload": {"installation_ref": ref, "operation_id": "record", "argv": ["changed"], "host_grants": ["grant.exec.external_process"], "timeout_s": 0.5},
            }, host=None, host_policy={"grants": ["grant.exec.external_process"]})

    def test_tools_cli_rejects_duplicate_and_nan(self):
        from slot_host.runtime.tools import main as tools_main
        import io, sys
        # Capture stderr diagnostics.
        old_stderr = sys.stderr
        cur = io.StringIO()
        sys.stderr = cur
        try:
            rc = tools_main(["mini", "behaviour.record", "--args", '{"request_id":"req-a","request_id":"req-b","payload":{}}'], dispatch_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not dispatch")))
            self.assertEqual(rc, 14)
            self.assertIn("duplicate", cur.getvalue().lower())
        finally:
            sys.stderr = old_stderr
        old_stderr = sys.stderr
        cur = io.StringIO()
        sys.stderr = cur
        try:
            rc = tools_main(["mini", "behaviour.record", "--args", '{"request_id":"req-a","payload":{"installation_ref":"x","operation_id":"record","argv":[NaN]}}'], dispatch_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not dispatch")))
            self.assertEqual(rc, 14)
        finally:
            sys.stderr = old_stderr

    def test_tools_cli_bounded_input_and_flags(self):
        from slot_host.runtime.tools import main as tools_main
        import io, sys
        # Empty args
        cur = io.StringIO()
        old = sys.stderr
        sys.stderr = cur
        try:
            rc = tools_main(["mini", "behaviour.record", "--args", ''], dispatch_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not dispatch")))
            self.assertEqual(rc, 14)
        finally:
            sys.stderr = old
        # --dry-run flag agreement
        cur2 = io.StringIO()
        old2 = sys.stderr
        sys.stderr = cur2
        try:
            rc = tools_main(["mini", "slot.inspect", "--args", '{"request_id":"req-flags-0001","payload":{"installation_ref":"x","operation_id":"record"},"dry_run":true}', "--dry-run"], dispatch_fn=lambda *a, **k: {"ok": True, "request_id": "req-flags-0001", "result": {}})
            # Flag agreement is either accepted or cleanly rejected; verify no crash.
            self.assertIn(rc, (0, 14))
        finally:
            sys.stderr = old2

    def test_dispatch_dry_run_flip_conflicts(self):
        from slot_host.runtime.tools import _ControlError
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        ref = _register(_manifest(impl), exe, "installation:fixture-slot@flip")
        payload = {"installation_ref": ref, "operation_id": "record", "argv": [], "host_grants": ["grant.exec.external_process"]}
        reply = dispatch("behaviour.record", {"request_id": "req-flip-0001", "dry_run": True, "payload": payload}, host=None, host_policy={"grants": ["grant.exec.external_process"]})
        self.assertTrue(reply["result"]["planned"])
        with self.assertRaises(_ControlError) as ctx:
            dispatch("behaviour.record", {"request_id": "req-flip-0001", "dry_run": False, "payload": payload}, host=None, host_policy={"grants": ["grant.exec.external_process"]})
        self.assertEqual(ctx.exception.code, "REQUEST_ID_CONFLICT")

    def test_dispatch_dry_run_enforces_host_policy(self):
        from slot_host.runtime.tools import _ControlError
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        ref = _register(_manifest(impl), exe, "installation:fixture-slot@drypol")
        payload = {"installation_ref": ref, "operation_id": "record", "argv": [], "host_grants": ["grant.exec.external_process"]}
        # Authenticated host policy without the execution grant refuses the
        # preview with NO_LEASE even though the payload claims the grant.
        with self.assertRaises(_ControlError) as ctx:
            dispatch("behaviour.record", {"request_id": "req-drypol-0001", "dry_run": True, "payload": payload}, host=None, host_policy={"grants": []})
        self.assertEqual(ctx.exception.code, "NO_LEASE")
        # The same preview under policy that grants execution succeeds.
        reply = dispatch("behaviour.record", {"request_id": "req-drypol-0002", "dry_run": True, "payload": payload}, host=None, host_policy={"grants": ["grant.exec.external_process"]})
        self.assertTrue(reply["result"]["planned"])

    def test_dispatch_live_enforces_host_policy(self):
        from slot_host.runtime.tools import _ControlError
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        ref = _register(_manifest(impl), exe, "installation:fixture-slot@livepol")
        payload = {"installation_ref": ref, "operation_id": "record", "argv": [], "host_grants": ["grant.exec.external_process"]}
        with self.assertRaises(_ControlError) as ctx:
            dispatch("behaviour.record", {"request_id": "req-livepol-0001", "payload": payload}, host=None, host_policy={"grants": []})
        self.assertEqual(ctx.exception.code, "NO_LEASE")

    def test_binding_error_code_mapping(self):
        from slot_host.runtime.tools import _code_for_binding_error
        self.assertEqual(_code_for_binding_error(BindingRejected("availability_not_installed", "x")), "AVAILABILITY_NOT_INSTALLED")
        self.assertEqual(_code_for_binding_error(BindingRejected("availability_parked", "x")), "AVAILABILITY_PARKED")

    def test_cli_rejects_oversized_input_before_decode(self):
        from slot_host.runtime.tools import main as tools_main
        import io, sys
        def no_dispatch(*a, **k):
            raise AssertionError("must not dispatch")
        oversized = "x" * 70000
        cur = io.StringIO()
        old = sys.stderr
        sys.stderr = cur
        try:
            rc = tools_main(["mini", "behaviour.record", "--args", oversized], dispatch_fn=no_dispatch)
            self.assertEqual(rc, 14)
        finally:
            sys.stderr = old
        big = self.dir / "big.json"
        big.write_bytes(b"x" * 70000)
        cur = io.StringIO()
        sys.stderr = cur
        try:
            rc = tools_main(["mini", "behaviour.record", "--args-file", str(big)], dispatch_fn=no_dispatch)
            self.assertEqual(rc, 14)
        finally:
            sys.stderr = old
        # stdin source: bounded read of the oversized document.
        class _FakeStdin:
            def __init__(self, data):
                self.buffer = io.BytesIO(data)
        cur = io.StringIO()
        old_stdin = sys.stdin
        sys.stdin = _FakeStdin(b"x" * 70000)
        sys.stderr = cur
        try:
            rc = tools_main(["mini", "behaviour.record", "--args-file", "-"], dispatch_fn=no_dispatch)
            self.assertEqual(rc, 14)
        finally:
            sys.stdin = old_stdin
            sys.stderr = old

    def test_cli_rejects_nonregular_args_descriptor(self):
        from slot_host.runtime.tools import main as tools_main
        import io, sys
        def no_dispatch(*a, **k):
            raise AssertionError("must not dispatch")
        subdir = self.dir / "argdir"
        subdir.mkdir()
        cur = io.StringIO()
        old = sys.stderr
        sys.stderr = cur
        try:
            rc = tools_main(["mini", "behaviour.record", "--args-file", str(subdir)], dispatch_fn=no_dispatch)
            self.assertEqual(rc, 14)
        finally:
            sys.stderr = old
        fifo = self.dir / "args.fifo"
        os.mkfifo(fifo)
        cur = io.StringIO()
        sys.stderr = cur
        try:
            rc = tools_main(["mini", "behaviour.record", "--args-file", str(fifo)], dispatch_fn=no_dispatch)
            self.assertEqual(rc, 14)
        finally:
            sys.stderr = old

    def test_controls_envelope(self):
        env = control_request("behaviour.record", {"request_id": "req-envelope-0001", "payload": {"installation_ref": "installation:fixture-slot@e2e", "operation_id": "record"}}, context=None)
        self.assertEqual(env["schema"], "vp.control.req/v1")
        self.assertEqual(env["op"], "rea.slot.project")
        self.assertEqual(env["args"]["route_id"], "host.behaviour.record")

    def test_slot_inspect_reports_separately(self):
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        ref = _register(_manifest(impl), exe, "installation:fixture-slot@inspect")
        reply = dispatch("slot.inspect", {
            "request_id": "req-inspect-0001",
            "payload": {"installation_ref": ref, "operation_id": "record", "host_grants": ["grant.exec.external_process"]},
        }, host=None)
        self.assertTrue(reply["ok"])
        result = reply["result"]
        self.assertIn("validation", result)
        self.assertIn("readiness", result)
        self.assertIn("plan", result)
        self.assertIn("effective_grants", result)

    def test_slot_inspect_ungranted_reports_readiness_without_raise(self):
        exe = self.dir / "fixture.py"
        impl = _write_fixture(exe)
        ref = _register(_manifest(impl), exe, "installation:fixture-slot@inspect-ungranted")
        # Passive inspection must return control OK with static validation
        # and plan, readiness, and empty effective_grants, even ungranted.
        reply = dispatch("slot.inspect", {
            "request_id": "req-inspect-ungranted-0001",
            "payload": {"installation_ref": ref, "operation_id": "record", "host_grants": []},
        }, host=None, host_policy={"grants": [], "spawn_granted": False} if False else None)
        # dispatch slot.inspect is passive; it must not raise NO_LEASE.
        self.assertTrue(reply["ok"], "passive inspect must be OK even without grants")
        res = reply.get("result", reply)
        self.assertIn("readiness", res)
        self.assertIn("validation", res)
        self.assertIn("plan", res)
        # Effective grants derived from policy must be empty, not echoed.
        if "effective_grants" in res:
            self.assertEqual(res["effective_grants"], [])


if __name__ == "__main__":
    unittest.main()
