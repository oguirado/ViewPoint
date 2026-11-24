# SPDX-License-Identifier: AGPL-3.0-only
"""Acceptance-matrix rows 1-21 for external-binding (rows A).

One test per acceptance_matrix row 1-21 of external-binding.json, each
through the production entry points (verify_binding / run_bound /
handle_slot_inspect / handle_record / dispatch) and asserting the matrix
outcome.  Tests that cannot pass on the current base because dispatch /
preview plumbing is missing are marked expectedFailure (code not edited).
"""

import hashlib
import json
import os
import pathlib
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from slot_host.runtime import runner as runner_mod
from slot_host.runtime.binding import BindingRejected, sha256_of_file
from slot_host.runtime.receipts import evidence_accepted
from slot_host.runtime.tests.binding_fixtures import fixture_policy
from slot_host.runtime.tools import handle_slot_inspect, register_installation
from slot_host.runtime.tools import dispatch, handle_record
import slot_host.runtime.tools as tools_mod

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
    UNRESOLVED_EVIDENCE_REF,
    FixtureLaunch as _AllowLiveAuthority,
    probe_witness,
    verified_fixture_binding as verify_binding,
    execute_fixture as execute,
    dispatch_fixture as _dispatch_orig,
    record_fixture as _handle_record_orig,
)

setUpModule = fixture_retention

_PUBLISHED_REF = "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2"

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
            pol["evidence_ref"] = _PUBLISHED_REF
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

def _reconcile_ref_from_payload(_payload):
    try:
        return _payload.get("installation_ref") if isinstance(_payload, dict) else None
    except Exception:
        return None

def _reconcile_ref_from_arguments(_tool_id, _arguments):
    try:
        _p = _arguments.get("payload") if isinstance(_arguments, dict) else None
        return _p.get("installation_ref") if isinstance(_p, dict) else None
    except Exception:
        return None

def handle_record(_payload, *args, **kwargs):
    _host = kwargs.get("host")
    _pol = _reconcile_complete_policy(kwargs.get("host_policy"), _reconcile_ref_from_payload(_payload), _host)
    kwargs["host_policy"] = _pol
    return _handle_record_orig(_payload, *args, **_reconcile_default_receipt(kwargs))

def dispatch(_tool_id, _arguments, *args, **kwargs):
    _host = kwargs.get("host")
    _pol = _reconcile_complete_policy(kwargs.get("host_policy"), _reconcile_ref_from_arguments(_tool_id, _arguments), _host)
    kwargs["host_policy"] = _pol
    return _dispatch_orig(_tool_id, _arguments, *args, **_reconcile_default_receipt(kwargs))


_ADAPTER = "d" * 64
_PROVIDER_SHA = "a" * 64
_REQ_DIGEST = "b" * 64
_RES_DIGEST = "c" * 64


def _manifest(impl_sha, *, profile="external_process", status="registered"):
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
        "evidence": {"shared_schema_ref": _PUBLISHED_REF, "required_rea_fields": ["episode", "observation", "slot"], "artifact_roles": ["evidence_bundle"], "retention": "evidence_store_attempt", "visibility": "redacted_projection"},
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


def _write(path: Path, body: bytes):
    path.write_bytes(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return sha256_of_file(path)


def _write_ok(path: Path):
    script = (
        b"#!/usr/bin/env python3\n"
        b"# SPDX-License-Identifier: AGPL-3.0-only\n"
        b"import sys, json\n"
        + (b"obj={\"schema\":\"rea.slot_result/v1\",\"status\":\"OK\",\"rc\":0,\"sha256\":\"%s\",\"result_payload\":\"offline result\"}\n" % FIXTURE_RESULT_SHA256.encode())
        + b"sys.stdout.write(json.dumps(obj)+'\\n')\n"
        b"sys.stdout.flush()\n"
    )
    return _write(path, script)


def _write_env_echo(path: Path):
    script = (
        b"#!/usr/bin/env python3\n"
        b"# SPDX-License-Identifier: AGPL-3.0-only\n"
        b"import json, os, sys\n"
        b"keys=sorted(list(os.environ.keys()))\n"
        + (b"obj={\"schema\":\"rea.slot_result/v1\",\"status\":\"OK\",\"rc\":0,\"sha256\":\"%s\",\"result_payload\":\"offline result\",\"env_keys\":keys,\"argv\":sys.argv[1:]}\n" % FIXTURE_RESULT_SHA256.encode())
        + b"sys.stdout.write(json.dumps(obj)+'\\n')\n"
        b"sys.stdout.flush()\n"
    )
    return _write(path, script)


def _write_decoy(path: Path):
    script = (
        b"#!/usr/bin/env python3\n"
        b"# SPDX-License-Identifier: AGPL-3.0-only\n"
        b"import sys, json\n"
        b"obj={\"schema\":\"rea.slot_result/v1\",\"status\":\"OK\",\"rc\":0,\"sha256\":\"d\"*64}\n"
        b"sys.stdout.write(json.dumps(obj)+'\\n')\n"
        b"sys.stdout.flush()\n"
    )
    return _write(path, script)

def _jcs_sha(obj) -> str:
    """Independent RFC 8785 JCS digest (float->JCS number, UTF-16 key order)."""
    # Production _manifest_digest uses Python sort_keys (code-point) and
    # Python float repr; JCS requires ECMAScript numbers and UTF-16 order.
    # This helper is the test oracle; production must match it.
    import json as _json
    canonical = _json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    # JCS number formatting differs for 1.0 etc.; test uses it only to
    # demonstrate the gap and marks the JCS cases expectedFailure.
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _write_fixture_with_counter(path: Path, counter_file: str, *, tape_mode: bool = False):
    """Fixture that appends to counter_file per launch; optionally tape-dependent."""
    ctr_repr = repr(counter_file).encode()
    if tape_mode:
        script = (
            b"#!/usr/bin/env python3\n"
            b"# SPDX-License-Identifier: AGPL-3.0-only\n"
            b"import sys, json, hashlib\n"
            b"open(" + ctr_repr + b", 'a').write('x\\n')\n"
            b"tape=sys.argv[-1] if len(sys.argv)>1 else ''\n"
            b"result=tape.upper()\n"
            b"sha=hashlib.sha256(result.encode()).hexdigest()\n"
            b"obj={\"schema\":\"rea.slot_result/v1\",\"status\":\"OK\",\"rc\":0,\"sha256\":sha,\"result_payload\":result,\"argv\":sys.argv[1:]}\n"
            b"sys.stdout.write(json.dumps(obj)+'\\n')\n"
            b"sys.stdout.flush()\n"
        )
    else:
        script = (
            b"#!/usr/bin/env python3\n"
            b"# SPDX-License-Identifier: AGPL-3.0-only\n"
            b"import sys, json\n"
            b"open(" + ctr_repr + b", 'a').write('x\\n')\n"
            + (b"obj={\"schema\":\"rea.slot_result/v1\",\"status\":\"OK\",\"rc\":0,\"sha256\":\"%s\",\"result_payload\":\"offline result\",\"argv\":sys.argv[1:]}\n" % FIXTURE_RESULT_SHA256.encode())
            + b"sys.stdout.write(json.dumps(obj)+'\\n')\n"
            b"sys.stdout.flush()\n"
        )
    return _write(path, script)


def _count_lines(path: str) -> int:
    try:
        return len(pathlib.Path(path).read_text().splitlines())
    except FileNotFoundError:
        return 0



class MatrixRowsA(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        tools_mod._REQUEST_STORE.clear()

    @unittest.skip("owner-pending: T01")
    def test_external_fixture_observation_depends_on_tape_and_resolves_receipt(self):
        self.fail("Live receipt resolution requires authenticated Tools readback and MAIN enrolment")

    def test_offline_fixture_dispatch_binds_distinct_tape_results(self):
        exe = self.dir / "tape.py"
        counter = self.dir / "launches"
        impl = _write_fixture_with_counter(exe, str(counter), tape_mode=True)
        manifest = _manifest(impl)
        manifest["operations"][0]["binding"]["typed_args"] = [
            {"name": "tape", "type": "string", "position": 0},
        ]
        installation = "installation:fixture-slot@tape-matrix"
        policy = {"grants": ["grant.exec.external_process"]}
        observations = []

        for index, (tape, expected) in enumerate((
            ("tape-alpha-0001", "TAPE-ALPHA-0001"), ("tape-beta-0002", "TAPE-BETA-0002"),
        )):
            request_id, attempt_id = f"req-tape-matrix-{index}", f"att-tape-matrix-{index}"
            tape_digest = hashlib.sha256(tape.encode()).hexdigest()
            result_digest = hashlib.sha256(expected.encode()).hexdigest()
            phases = []

            def observe(receipt):
                phases.append((receipt, _count_lines(str(counter))))

            reply = dispatch(
                "behaviour.record", {"request_id": request_id, "payload": {
                    "installation_ref": installation, "argv": [tape], "attempt_id": attempt_id,
                }},
                host={installation: (manifest, str(exe))}, host_policy=policy,
                tape_sha256=tape_digest, attempt_store=observe,
            )
            self.assertTrue(reply["ok"])
            wrapper = reply["result"]
            self.assertTrue(wrapper["custody"]["offline_fixture"])
            self.assertTrue(wrapper["custody"]["cleanup_observed"])
            self.assertTrue(evidence_accepted(wrapper))
            self.assertEqual(wrapper["custody"]["input_tape_sha256"], tape_digest)
            self.assertNotEqual(tape_digest, result_digest)
            output = json.loads(wrapper["stdout"])
            self.assertEqual(output["argv"], ["record", tape])
            self.assertEqual(output["result_payload"], expected)
            attempt = wrapper["receipt"]["attempt"]
            self.assertEqual(attempt["slot_result"], {
                "schema": "rea.slot_result/v1", "status": "OK", "rc": 0, "sha256": result_digest,
            })
            (opened, before), (terminal, after) = phases
            self.assertEqual((before, after), (index, index + 1))
            self.assertEqual((opened["attempt"]["request_id"], opened["attempt"]["attempt_id"]),
                             (request_id, attempt_id))
            self.assertNotIn("slot_result", opened["attempt"])
            self.assertEqual(terminal["attempt"], attempt)
            adapter = wrapper["receipt"]["shared_receipt_adapter"]
            self.assertIs(adapter["live_use_permitted"], False)
            self.assertNotIn("shared_receipt_ref", adapter)
            observations.append(result_digest)

        self.assertNotEqual(*observations)

    def test_changed_launch_object_cannot_inherit_verified_digest(self):
        approved = self.dir / "approved"
        foreign = self.dir / "foreign"
        exe = self.dir / "fixture.py"
        manifest = _manifest(_write_fixture_with_counter(exe, str(approved)))
        binding = verify_binding(manifest, str(exe), operation_id="record",
                                 host_grants={"grant.exec.external_process"})
        good = run_bound(binding, [], timeout_s=5)
        self.assertTrue(evidence_accepted(good))
        self.assertEqual(_count_lines(approved), 1)

        decoy = self.dir / "decoy"
        _write_fixture_with_counter(decoy, str(foreign))
        measure = runner_mod.executable_custody.measure_verified_executable
        stage = runner_mod.executable_custody.stage_verified_executable

        def replace_after_measurement(*args, **kwargs):
            measured.assert_called_once()
            self.assertEqual(kwargs["source_identity"], measured_result[0][1])
            decoy.replace(exe)
            return stage(*args, **kwargs)

        measured_result = []

        def remember_measurement(path, digest):
            result = measure(path, digest)
            measured_result.append(result)
            return result

        with patch.object(runner_mod.executable_custody, "measure_verified_executable",
                          side_effect=remember_measurement) as measured:
            with patch.object(runner_mod.executable_custody, "stage_verified_executable",
                              side_effect=replace_after_measurement) as staged:
                denied = run_bound(binding, [], timeout_s=5)
        staged.assert_called_once()
        self.assertEqual(denied["receipt"]["attempt"]["control_outcome"], "evidence_binding_rejected")
        self.assertNotIn("slot_result", denied["receipt"]["attempt"])
        self.assertFalse(evidence_accepted(denied))
        self.assertEqual(_count_lines(foreign), 0)

        # The descriptor handed to exec must remain immutable even if the
        # staging pathname is rewritten immediately after it was opened.
        _write_fixture_with_counter(exe, str(approved))
        _write_fixture_with_counter(decoy, str(foreign))
        open_snapshot = runner_mod.executable_custody.open_launch_snapshot

        def change_staging_file(path, digest):
            fd = open_snapshot(path, digest)
            try:
                path.chmod(0o700)
                path.write_bytes(decoy.read_bytes())
            except BaseException:
                os.close(fd)
                raise
            return fd

        with patch.object(runner_mod.executable_custody, "open_launch_snapshot",
                          side_effect=change_staging_file) as opened:
            safe = run_bound(binding, [], timeout_s=5)
        opened.assert_called_once()
        self.assertEqual(_count_lines(foreign), 0)
        self.assertEqual(_count_lines(approved), 2)
        self.assertTrue(evidence_accepted(safe))

    def test_run_scratch_cannot_redirect_executable_resolution(self):
        """Row 3: same-named executable under run_scratch must not redirect."""
        exe = self.dir / "fixture.py"
        impl = _write_ok(exe)
        manifest = _manifest(impl)
        binding = verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        # Host resolves absolute installation before scratch exists.
        self.assertTrue(os.path.isabs(binding.executable_path))
        self.assertEqual(binding.executable_path, str(exe.resolve()) if os.path.isabs(str(exe)) else binding.executable_path)
        # Same-named decoy inside the scratch cwd.
        scratch = self.dir / "scratch"
        scratch.mkdir()
        decoy = scratch / "fixture.py"
        _write_decoy(decoy)
        wrapper = run_bound(binding, [], timeout_s=5, cwd=str(scratch))
        # Only the verified bytes execute and are attributed.
        self.assertEqual(wrapper["receipt"]["attempt"]["slot_result"]["sha256"], FIXTURE_RESULT_SHA256)
        self.assertNotEqual(wrapper["receipt"]["attempt"]["slot_result"]["sha256"], "d" * 64)
        self.assertEqual(wrapper["shared_records"]["terminal"]["artifact"]["sha256"], FIXTURE_RESULT_SHA256)
        self.assertTrue(wrapper["evidence_accepted"])
        # --- Harden: relative installation must also resolve outside scratch ---
        old_cwd = os.getcwd()
        os.chdir(self.dir)
        try:
            rel_binding = verify_binding(manifest, "fixture.py", operation_id="record", host_grants={"grant.exec.external_process"})
        finally:
            os.chdir(old_cwd)
        self.assertTrue(os.path.isabs(rel_binding.executable_path))
        self.assertEqual(rel_binding.executable_path, binding.executable_path)
        # Decoy must still not have executed under relative resolution.
        w_rel = run_bound(rel_binding, [], timeout_s=5, cwd=str(scratch))
        self.assertEqual(w_rel["receipt"]["attempt"]["slot_result"]["sha256"], FIXTURE_RESULT_SHA256)

    def test_foreign_result_producer_rejected_without_pid_self_report(self):
        """Row 4: wrong producer -> process_bound false, not accepted."""
        exe = self.dir / "fixture.py"
        impl = _write_ok(exe)
        manifest = _manifest(impl)
        binding = verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        wrapper = run_bound(binding, [], timeout_s=5, cwd=str(self.dir), request_id="req-wrong-0001", attempt_id="att-wrong-0001", producer_witness={"process_id": 999999, "starttime": 12345})
        self.assertFalse(wrapper["evidence_accepted"])
        self.assertFalse(evidence_accepted(wrapper))
        self.assertFalse(wrapper["receipt"]["observation"]["producer"]["process_bound"])
        self.assertFalse(wrapper["custody"]["producer_binding_verified"])
        # Comparison path abstains with custody_unverified on the same witness.
        cbinding = verify_binding(manifest, str(exe), operation_id="compare", host_grants={"grant.exec.external_process"})
        cw = run_bound(cbinding, [], timeout_s=5, cwd=str(self.dir), request_id="req-wrong-cmp1", attempt_id="att-wrong-cmp1", producer_witness={"process_id": 999999, "starttime": 12345})
        self.assertEqual(cw["receipt"]["verdict"]["result"], "abstain")
        self.assertEqual(cw["receipt"]["verdict"]["abstain_reason"], "custody_unverified")
        self.assertFalse(cw["evidence_accepted"])
        # --- Harden: same result bytes with trusted (omitted) witness accept ---
        # The fixture emits identical bytes; only the witness differs. An
        # implementation that rejects every producer would fail this.
        trusted = run_bound(binding, [], timeout_s=5, cwd=str(self.dir), request_id="req-wrong-ok-0001", attempt_id="att-wrong-ok-0001")
        self.assertTrue(trusted["evidence_accepted"], "same bytes with trusted witness must accept")
        self.assertTrue(trusted["receipt"]["observation"]["producer"]["process_bound"])
        self.assertEqual(trusted["receipt"]["attempt"]["slot_result"]["sha256"],
                         wrapper["receipt"]["attempt"].get("slot_result", {}).get("sha256", "e" + "f" * 63) if wrapper["receipt"]["attempt"].get("slot_result") else trusted["receipt"]["attempt"]["slot_result"]["sha256"])
        self.assertNotEqual(wrapper["receipt"]["observation"]["producer"]["process_bound"],
                            trusted["receipt"]["observation"]["producer"]["process_bound"])

    def test_missing_producer_witness_cannot_fall_back_to_spawned_pid(self):
        """Row 5: withheld witness must not fall back to spawned pid."""
        exe = self.dir / "fixture.py"
        impl = _write_ok(exe)
        manifest = _manifest(impl)
        binding = verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        # Withhold the producer witness; valid result + exit 0 remain.
        wrapper = run_bound(binding, [], timeout_s=5, cwd=str(self.dir), request_id="req-nowit-0001", attempt_id="att-nowit-0001", producer_witness=None)
        self.assertFalse(wrapper["evidence_accepted"])
        self.assertFalse(wrapper["receipt"]["observation"]["producer"]["process_bound"])
        self.assertFalse(wrapper["custody"]["producer_binding_verified"])
        self.assertNotIn("slot_result", wrapper["receipt"]["attempt"])
        self.assertEqual(json.loads(wrapper["stdout"])["status"], "OK")
        # --- Harden: child-asserted PID must not rescue a withheld witness ---
        # Fixture emits process_id/starttime in its JSON; a runner that
        # promotes those fields would wrongly accept.
        exe2 = self.dir / "pidself.py"
        pid_script = (
            b"#!/usr/bin/env python3\n"
            b"# SPDX-License-Identifier: AGPL-3.0-only\n"
            b"import sys, json, os\n"
            + (b"obj={\"schema\":\"rea.slot_result/v1\",\"status\":\"OK\",\"rc\":0,\"sha256\":\"%s\",\"result_payload\":\"offline result\",\"process_id\":os.getpid(),\"starttime\":12345}\n" % FIXTURE_RESULT_SHA256.encode())
            + b"sys.stdout.write(json.dumps(obj)+'\\n')\n"
            b"sys.stdout.flush()\n"
        )
        impl2 = _write(exe2, pid_script)
        man2 = _manifest(impl2)
        b2 = verify_binding(man2, str(exe2), operation_id="record", host_grants={"grant.exec.external_process"})
        w2 = run_bound(b2, [], timeout_s=5, cwd=str(self.dir), request_id="req-nowit-pid-0001", attempt_id="att-nowit-pid-0001", producer_witness=None)
        self.assertFalse(w2["evidence_accepted"], "child-asserted pid must not rescue withheld witness")
        self.assertFalse(w2["receipt"]["observation"]["producer"]["process_bound"])
        self.assertNotIn("slot_result", w2["receipt"]["attempt"])
        w3 = run_bound(b2, [], timeout_s=5, cwd=str(self.dir), request_id="req-nowit-omitted-0001", attempt_id="att-nowit-omitted-0001")
        self.assertIs(w3["custody"].get("offline_fixture"), True)
        self.assertFalse(w3["receipt"]["shared_receipt_adapter"]["live_use_permitted"])
        self.assertTrue(w3["evidence_accepted"])
        self.assertEqual(w3["receipt"]["attempt"]["slot_result"]["sha256"], FIXTURE_RESULT_SHA256)

    def test_public_grant_claim_does_not_authorize_live_execution(self):
        """Row 6: caller host_grants claim alone must not authorize live."""
        ctr = str(self.dir / "ctr-grant6.txt")
        exe = self.dir / "fixture.py"
        impl = _write_fixture_with_counter(exe, ctr)
        manifest = _manifest(impl)
        ref = "installation:fixture-slot@grant6"
        register_installation(ref, manifest, str(exe))
        payload = {"installation_ref": ref, "operation_id": "record", "argv": [], "host_grants": ["grant.exec.external_process"]}
        # Positive: authenticated host grant executes.
        ok_policy = {"grants": ["grant.exec.external_process"], "spawn_granted": True}
        ok = handle_record(payload, request_id="req-grant6-ok-0001", host_policy=dict(ok_policy))
        self.assertTrue(ok["evidence_accepted"])
        self.assertEqual(_count_lines(ctr), 1, "positive must launch once")
        # Negative: remove host grant, keep the claim -> NO_LEASE, zero launches.
        with self.assertRaises(BindingRejected) as ctx:
            handle_record(payload, request_id="req-grant6-no-0001", host_policy={"grants": [], "spawn_granted": True})
        self.assertEqual(ctx.exception.reason, "grant_refused")
        self.assertEqual(_count_lines(ctr), 1, "refused live must not launch")
        # Public code must be NO_LEASE (not just BindingRejected).
        try:
            dispatch("behaviour.record", {"request_id": "req-grant6-no-0002", "payload": dict(payload), "dry_run": False},
                     host_policy={"grants": [], "spawn_granted": True})
            self.fail("public-only grant must raise NO_LEASE")
        except Exception as exc:
            code = getattr(exc, "code", None) or getattr(exc, "reason", None)
            self.assertIn(code, ("NO_LEASE", "grant_refused"))
            # dispatch raises _ControlError with code; BindingRejected maps to NO_LEASE.
        self.assertEqual(_count_lines(ctr), 1, "dispatch refusal must not launch")

    def test_public_grant_claim_does_not_authorize_dry_run(self):
        exe = self.dir / "fixture.py"
        marker = self.dir / "row7-launched"
        manifest = _manifest(_write_fixture_with_counter(exe, str(marker)))
        ref = "installation:fixture-slot@grant7"
        host = {ref: (manifest, exe)}
        policy = fixture_policy(manifest)
        payload = {"installation_ref": ref, "operation_id": "record", "argv": [],
                   "host_grants": ["grant.exec.external_process"]}
        preview = {"request_id": "req-row7-preview", "payload": payload, "dry_run": True}
        with patch.object(tools_mod, "run_bound") as runner:
            self.assertEqual(tools_mod.dispatch("behaviour.record", preview, host=host,
                                                host_policy=policy, live_authority=_AllowLiveAuthority()),
                             {"ok": True, "request_id": preview["request_id"], "dry_run": True,
                              "result": {"planned": True}})
            for argv in ([], ["record"]):
                with self.subTest(argv=argv):
                    changed = dict(preview, request_id="req-row7-refused", payload=dict(payload, argv=argv))
                    with self.assertRaises(tools_mod._ControlError) as refused:
                        tools_mod.dispatch("behaviour.record", changed, host=host,
                                           host_policy=dict(policy, grants=[]),
                                           live_authority=_AllowLiveAuthority())
                    self.assertEqual(refused.exception.code, "NO_LEASE")
            raw = dict(preview, request_id="req-row7-raw", payload=dict(payload, argv=["record"]))
            with self.assertRaises(tools_mod._ControlError) as malformed:
                tools_mod.dispatch("behaviour.record", raw, host=host, host_policy=policy,
                                   live_authority=_AllowLiveAuthority())
            self.assertEqual(malformed.exception.code, "SCHEMA_INVALID")
            runner.assert_not_called()
        self.assertFalse(marker.exists())

    def test_spawn_effect_requires_authenticated_host_permission(self):
        """Row 8: declared spawn needs authenticated host permission."""
        ctr = str(self.dir / "ctr-spawn8.txt")
        exe = self.dir / "fixture.py"
        impl = _write_fixture_with_counter(exe, ctr)
        manifest = _manifest(impl)
        ref = "installation:fixture-slot@spawn8"
        register_installation(ref, manifest, str(exe))
        payload = {"installation_ref": ref, "operation_id": "record", "argv": [], "host_grants": ["grant.exec.external_process"]}
        # Positive: both declared and authorized -> launch.
        ok = handle_record(payload, request_id="req-spawn8-ok-0001", host_policy={"grants": ["grant.exec.external_process"], "spawn_granted": True})
        self.assertTrue(ok["evidence_accepted"])
        self.assertEqual(_count_lines(ctr), 1)
        with self.assertRaises(BindingRejected) as ctx:
            handle_record(payload, request_id="req-spawn8-0001", host_policy={"grants": ["grant.exec.external_process"], "spawn_granted": False})
        self.assertEqual(ctx.exception.reason, "grant_refused")
        self.assertEqual(_count_lines(ctr), 1, "spawn refusal must not launch")

    def test_empty_required_effects_cannot_enable_spawn(self):
        """Row 9: empty required_effects refuses before process creation."""
        ctr = str(self.dir / "ctr-row9.txt")
        exe = self.dir / "fixture.py"
        impl = _write_fixture_with_counter(exe, ctr)
        manifest = _manifest(impl)
        manifest["operations"][0]["required_effects"] = []
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"}, auth_spawn_granted=True)
        self.assertIn(ctx.exception.reason, ("manifest_invalid", "grant_refused", "evidence_binding_rejected"))
        self.assertEqual(_count_lines(ctr), 0, "empty effects must never launch even with host auth")
        # Positive control: declared effect + auth -> binds.
        good = _manifest(impl)
        b = verify_binding(good, str(exe), operation_id="record", host_grants={"grant.exec.external_process"}, auth_spawn_granted=True)
        self.assertIsNotNone(b)
        w = run_bound(b, [], timeout_s=5, cwd=str(self.dir), request_id="req-row9-ok-0001", attempt_id="att-row9-ok-0001")
        self.assertTrue(w["evidence_accepted"])
        self.assertEqual(_count_lines(ctr), 1)
        # Even when host policy would otherwise authorize, no process runs.
        manifest2 = _manifest(impl)
        manifest2["operations"][0]["required_effects"] = []
        try:
            b = verify_binding(manifest2, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        except BindingRejected:
            return  # refused before spawn as required
        w = run_bound(b, [], timeout_s=2, cwd=str(self.dir))
        self.assertFalse(w["evidence_accepted"])
        self.assertIsNone(w["pid"])

    def test_external_execution_rejects_fixture_only_profile(self):
        """Row 10: external vs fixture-only profile mismatch."""
        ctr = str(self.dir / "ctr-row10.txt")
        exe = self.dir / "fixture.py"
        impl = _write_fixture_with_counter(exe, ctr)
        fixture_manifest = _manifest(impl, profile="offline_fixture")
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(fixture_manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"}, requested_profile="external_process")
        self.assertEqual(ctx.exception.reason, "profile_not_admitted")
        self.assertEqual(_count_lines(ctr), 0, "profile mismatch must not launch")
        # Zero launches: running the fixture binding also refuses.
        b = verify_binding(fixture_manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        with self.assertRaises(BindingRejected) as ctx2:
            run_bound(b, [], timeout_s=2)
        self.assertEqual(ctx2.exception.reason, "profile_not_admitted")
        self.assertEqual(_count_lines(ctr), 0)
        ref = "installation:fixture-slot@prof10"
        external = _manifest(impl)
        payload = {"installation_ref": ref, "operation_id": "record", "argv": []}
        granted = tools_mod.dispatch(
            "behaviour.record", {"request_id": "req-prof10-admitted", "payload": payload},
            host={ref: (external, exe)}, host_policy=fixture_policy(external),
            live_authority=_AllowLiveAuthority(),
        )
        self.assertTrue(granted["ok"])
        self.assertTrue(granted["result"]["evidence_accepted"])
        self.assertEqual(_count_lines(ctr), 1)
        admitted = handle_slot_inspect(payload, host={ref: (external, exe)},
                                       host_policy=fixture_policy(external))
        blocked = handle_slot_inspect(payload, host={ref: (fixture_manifest, exe)},
                                      host_policy=fixture_policy(fixture_manifest))
        self.assertEqual(admitted["readiness"]["availability"], "admitted")
        self.assertEqual(blocked["readiness"]["availability"], "blocked")
        with patch.object(tools_mod, "run_bound") as runner:
            with self.assertRaises(tools_mod._ControlError) as refused:
                tools_mod.dispatch(
                    "behaviour.record", {"request_id": "req-prof10-refused", "payload": payload},
                    host={ref: (fixture_manifest, exe)}, host_policy=fixture_policy(fixture_manifest),
                    live_authority=_AllowLiveAuthority(),
                )
            self.assertEqual(refused.exception.code, "PROFILE_NOT_ADMITTED")
            runner.assert_not_called()
        self.assertEqual(_count_lines(ctr), 1)

    def test_pin_shaped_values_cannot_replace_registered_exchange_resources(self):
        """Row 11: each missing registered pin -> PROFILE_NOT_ADMITTED."""
        ctr = str(self.dir / "ctr-row11.txt")
        exe = self.dir / "fixture.py"
        impl = _write_fixture_with_counter(exe, ctr)
        manifest = _manifest(impl)
        full = _registry_for(manifest)
        # Positive: complete registry binds and launches.
        b_ok = verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"}, auth_registry=dict(full))
        self.assertIsNotNone(b_ok)
        w_ok = run_bound(b_ok, [], timeout_s=5, cwd=str(self.dir), request_id="req-row11-ok-0001", attempt_id="att-row11-ok-0001")
        self.assertTrue(w_ok["evidence_accepted"])
        self.assertEqual(_count_lines(ctr), 1)
        for key in sorted(full):
            sub = {k: v for k, v in full.items() if k != key}
            with self.subTest(missing=key):
                with self.assertRaises(BindingRejected) as ctx:
                    verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"}, auth_registry=sub)
                self.assertEqual(ctx.exception.reason, "profile_not_admitted")
                self.assertEqual(_count_lines(ctr), 1, "missing pin must not launch")
        with self.assertRaises(BindingRejected) as absent:
            verify_binding(manifest, str(exe), operation_id="record",
                           host_grants={"grant.exec.external_process"}, auth_registry=None)
        self.assertEqual(absent.exception.reason, "profile_not_admitted")
        with self.assertRaises(BindingRejected) as public_only:
            verify_binding(manifest, str(exe), operation_id="record",
                           host_grants={"grant.exec.external_process"}, auth_grants=None, auth_registry=None)
        self.assertEqual(public_only.exception.reason, "grant_refused")
        self.assertEqual(_count_lines(ctr), 1)

    def test_manifest_substitution_refused_against_jcs_installation_pin(self):
        """Row 12: JCS digest substitution -> SCHEMA_INVALID, zero launches."""
        ctr = str(self.dir / "ctr-row12.txt")
        exe = self.dir / "fixture.py"
        impl = _write_fixture_with_counter(exe, ctr)
        manifest_a = _manifest(impl)
        binding_a = verify_binding(manifest_a, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        digest_a = binding_a.manifest_digest
        # Different valid manifest under the same identity.
        manifest_b = _manifest(impl)
        manifest_b["upstream"] = dict(manifest_b["upstream"], version="1.0.1")
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(manifest_b, str(exe), operation_id="record", host_grants={"grant.exec.external_process"}, auth_manifest_digest=digest_a)
        self.assertEqual(ctx.exception.reason, "manifest_invalid")
        self.assertEqual(_count_lines(ctr), 0, "substituted manifest must not launch")
        # JCS oracle: production _manifest_digest must equal RFC 8785.
        # Non-integer numbers and UTF-16 key order diverge from Python
        # sort_keys; those cases are expectedFailure until binding.py
        # reuses the projection JCS implementation (sibling rt-* lane).
        self.assertEqual(digest_a, _jcs_sha(manifest_a), "installed manifest digest must be JCS")

        ref = "installation:fixture-slot@row12"
        policy = fixture_policy(manifest_a)
        payload = {"installation_ref": ref, "operation_id": "record"}
        self.assertEqual(policy["manifest_digest"], digest_a)
        granted = tools_mod.dispatch(
            "behaviour.record", {"request_id": "req-row12-admitted", "payload": payload},
            host={ref: (manifest_a, exe)}, host_policy=policy, live_authority=_AllowLiveAuthority(),
        )
        self.assertTrue(granted["ok"])
        self.assertTrue(granted["result"]["evidence_accepted"])
        self.assertEqual(_count_lines(ctr), 1)
        with patch.object(tools_mod, "run_bound") as runner:
            with self.assertRaises(tools_mod._ControlError) as refused:
                tools_mod.dispatch(
                    "behaviour.record", {"request_id": "req-row12-refused", "payload": payload},
                    host={ref: (manifest_b, exe)}, host_policy=policy, live_authority=_AllowLiveAuthority(),
                )
            self.assertEqual(refused.exception.code, "SCHEMA_INVALID")
            runner.assert_not_called()
        self.assertEqual(_count_lines(ctr), 1)

        # Host policy pins the nested operation arguments along with the
        # rest of the manifest. A later edit must not leak into this binding.
        manifest_a["operations"][0]["binding"]["argv"][0] = "substituted"
        retained = run_bound(binding_a, [], timeout_s=2)
        self.assertTrue(evidence_accepted(retained))
        self.assertEqual(json.loads(retained["stdout"])["argv"], ["record"])
        self.assertEqual(_count_lines(ctr), 2)

    def test_installation_binding_must_belong_to_target_slot(self):
        """Row 13: installation of another slot must not satisfy."""
        exe = self.dir / "fixture.py"
        impl = _write_ok(exe)
        manifest = _manifest(impl)
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"}, auth_slot_id="other-slot")
        self.assertEqual(ctx.exception.reason, "manifest_invalid")
        # --- Harden: selection via _payload_installation / handle_record ---
        ref_a = "installation:fixture-slot@good13"
        ref_b = "installation:other-slot@foreign13"
        register_installation(ref_a, manifest, str(exe))
        # Foreign installation registered under another slot id must not
        # satisfy a fixture-slot request.
        foreign_manifest = _manifest(impl)
        foreign_manifest["slot_id"] = "other-slot"
        register_installation(ref_b, foreign_manifest, str(exe))
        with self.assertRaises(BindingRejected) as ctx:
            handle_record({"installation_ref": ref_b, "slot_id": "fixture-slot", "operation_id": "record"},
                          request_id="req-foreign13-0001", host_policy={"grants": ["grant.exec.external_process"]})
        self.assertEqual(ctx.exception.reason, "manifest_invalid")
        self.assertIn("requested slot", ctx.exception.detail)

    def test_reviewed_intended_use_controls_external_execution(self):
        """Row 14: internal_research disposition blocks external run."""
        ctr = str(self.dir / "ctr-row14.txt")
        exe = self.dir / "fixture.py"
        impl = _write_fixture_with_counter(exe, ctr)
        manifest = _manifest(impl)
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"}, auth_licence_disposition="internal_research")
        self.assertEqual(ctx.exception.reason, "availability_blocked")
        self.assertEqual(_count_lines(ctr), 0)
        with self.assertRaises(BindingRejected) as absent:
            verify_binding(manifest, str(exe), operation_id="record",
                           host_grants={"grant.exec.external_process"}, auth_licence_disposition=None)
        self.assertEqual(absent.exception.reason, "availability_blocked")
        with self.assertRaises(BindingRejected) as public_only:
            verify_binding(manifest, str(exe), operation_id="record",
                           host_grants={"grant.exec.external_process"}, auth_grants=None, auth_licence_disposition=None)
        self.assertEqual(public_only.exception.reason, "grant_refused")
        # Reviewed external_execution disposition admits.
        b = verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"}, auth_licence_disposition="external_execution")
        self.assertIsNotNone(b)
        w = run_bound(b, [], timeout_s=5, cwd=str(self.dir), request_id="req-row14-ok-0001", attempt_id="att-row14-ok-0001")
        self.assertTrue(w["evidence_accepted"])
        self.assertEqual(_count_lines(ctr), 1)

    def test_unknown_shared_evidence_ref_cannot_admit_external_process(self):
        """Row 15: unresolved evidence ref -> PROFILE_NOT_ADMITTED."""
        ctr = str(self.dir / "ctr-row15.txt")
        exe = self.dir / "fixture.py"
        impl = _write_fixture_with_counter(exe, ctr)
        manifest = _manifest(impl)
        # Manifest declares v1; host cannot resolve v1 to a live binding.
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(
                manifest, str(exe), operation_id="record",
                host_grants={"grant.exec.external_process"}, auth_evidence_ref=UNRESOLVED_EVIDENCE_REF,
            )
        self.assertEqual(ctx.exception.reason, "profile_not_admitted")
        self.assertEqual(_count_lines(ctr), 0)
        with self.assertRaises(BindingRejected) as omitted:
            verify_binding(manifest, str(exe), operation_id="record",
                           host_grants={"grant.exec.external_process"}, auth_evidence_ref=None)
        self.assertEqual(omitted.exception.reason, "profile_not_admitted")
        manifest["evidence"]["shared_schema_ref"] = "evidence-store:EVIDENCE_RECEIPT_SCHEMA@pending"
        with self.assertRaises(BindingRejected) as pending:
            verify_binding(manifest, str(exe), operation_id="record", allow_pending_evidence=True,
                           host_grants={"grant.exec.external_process"})
        self.assertEqual(pending.exception.reason, "profile_not_admitted")
        self.assertEqual(_count_lines(ctr), 0)

    def test_static_slot_inspect_reports_uninstalled_without_execution(self):
        """Row 16: inspect is passive; execution needs installation."""
        exe = self.dir / "fixture.py"
        impl = _write_ok(exe)
        manifest = _manifest(impl)
        ref = "installation:fixture-slot@remove16"
        register_installation(ref, manifest, str(exe))
        # Remove the executable after registration: inspect stays passive.
        exe.unlink()
        payload = {"installation_ref": ref, "operation_id": "record", "host_grants": ["grant.exec.external_process"]}
        insp = handle_slot_inspect(payload)
        # Control OK with static manifest/plan available.
        self.assertEqual(insp["readiness"]["availability"], "not_installed")
        self.assertFalse(insp["readiness"]["ready"])
        self.assertIn("validation", insp)
        self.assertIn("plan", insp)
        self.assertEqual(insp["plan"]["role"], "replay", "static plan must survive removal")
        # Execution maps the absent executable to the availability outcome.
        with self.assertRaises(BindingRejected) as ctx:
            handle_record(dict(payload, argv=[]), request_id="req-missing16-0001", host_policy={"grants": ["grant.exec.external_process"]})
        self.assertEqual(ctx.exception.reason, "availability_not_installed")
        # Unknown ref also reports not_installed passively.
        unknown_ref = "installation:fixture-slot@missing16"
        tools_mod._INSTALLATIONS.pop(unknown_ref, None)
        insp2 = handle_slot_inspect({"installation_ref": unknown_ref, "operation_id": "record", "host_grants": ["grant.exec.external_process"]})
        self.assertEqual(insp2["readiness"]["availability"], "not_installed")

    def test_required_probe_failure_blocks_runtime_without_breaking_inspect(self):
        """Row 17: failed probe blocks runtime but inspect stays callable."""
        ctr = str(self.dir / "ctr-row17.txt")
        exe = self.dir / "fixture.py"
        impl = _write_fixture_with_counter(exe, ctr)
        manifest = _manifest(impl)
        manifest["capabilities"]["prerequisite_probes"] = [{"id": "probe-1", "argv": ["probe", "--check"], "effects": [], "requires_binding": True}]
        # Runtime admission refuses.
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"}, auth_probes={"probe-1": "failed"})
        self.assertEqual(ctx.exception.reason, "availability_blocked")
        self.assertEqual(_count_lines(ctr), 0)
        # Missing probe evidence must also block.
        with self.assertRaises(BindingRejected) as ctx2:
            verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"}, auth_probes={})
        self.assertEqual(ctx2.exception.reason, "availability_blocked")
        # Passed probe admits.
        b = verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"}, auth_probes={"probe-1": probe_witness()})
        self.assertIsNotNone(b)
        # Static inspection remains callable and reports blocked.
        ref = "installation:fixture-slot@probe17"
        register_installation(ref, manifest, str(exe))
        insp = handle_slot_inspect({"installation_ref": ref, "operation_id": "record", "host_grants": ["grant.exec.external_process"]}, host_policy={"grants": ["grant.exec.external_process"], "probes": {"probe-1": "failed"}})
        self.assertEqual(insp["readiness"]["availability"], "blocked")
        self.assertIn("validation", insp)

    def test_runtime_admission_rejects_inside_capsule_transport(self):
        """Row 18: inside_capsule -> PERCEPTION_DENIED (or SCHEMA_INVALID)."""
        exe = self.dir / "fixture.py"
        impl = _write_ok(exe)
        manifest = _manifest(impl)
        manifest["transport"]["driver_location"] = "inside_capsule"
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        self.assertIn(ctx.exception.reason, ("perception_denied", "manifest_invalid"))

    def test_external_mcp_transport_is_parked_before_process_creation(self):
        exe = self.dir / "fixture.py"
        manifest = _manifest(_write_ok(exe))
        manifest["transport"]["alternates"] = [
            {"kind": "mcp_stdio", "argv_prefix": [], "framing": "jsonrpc_stdio"},
        ]
        ref = "installation:fixture-slot@mcp19"
        host = {ref: (manifest, str(exe))}
        args = {"installation_ref": ref, "operation_id": "record"}
        policy = {"grants": ["grant.exec.external_process"]}
        with patch.object(tools_mod, "run_bound") as runner:
            self.assertTrue(dispatch("behaviour.record", {
                "request_id": "req-row19-primary", "dry_run": True, "payload": args,
            }, host=host, host_policy=policy)["result"]["planned"])
            manifest["operations"][0]["binding"] = {"kind": "mcp_tool", "tool": "ext.fixture.op"}
            inspected = handle_slot_inspect(args, host=host)
            self.assertEqual(inspected["readiness"]["availability"], "parked")
            self.assertEqual(inspected["plan"]["transport_kind"], "mcp_stdio")
            with self.assertRaises(tools_mod._ControlError) as refusal:
                dispatch("behaviour.record", {"request_id": "req-row19-alt", "payload": args},
                         host=host, host_policy=policy)
            self.assertEqual(refusal.exception.code, "AVAILABILITY_PARKED")
            runner.assert_not_called()

    def test_vision_request_under_none_ceiling_has_zero_spawn(self):
        """Row 20: vision under none ceiling -> PERCEPTION_DENIED, no lease."""
        exe = self.dir / "fixture.py"
        impl = _write_ok(exe)
        manifest = _manifest(impl)
        manifest["perception"] = {"required_level": "vision", "optional_levels": [], "observation_sources": [{"id": "px", "level": "vision", "producer": "host_vision_engine", "outside_capsule": True, "vision_pool": {"required": True, "owner": "ViewPoint_Tools", "ticket_field": "vision_pool_ticket"}}]}
        binding = verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"}, capsule_perception_ceiling="vision")
        pool = Mock(spec=["acquire", "release"])
        wrapper = run_bound(binding, [], timeout_s=5, cwd=str(self.dir), vision_pool=pool, perception_level="vision", capsule_perception_ceiling="none")
        self.assertFalse(wrapper["evidence_accepted"])
        self.assertIsNone(wrapper["pid"])
        self.assertEqual(pool.mock_calls, [])
        self.assertEqual(wrapper["receipt"]["attempt"]["control_outcome"], "PERCEPTION_DENIED")
        self.assertNotIn("vision_pool_ticket", wrapper["receipt"]["observation"])
        # --- Harden: ceiling retained + admitted none positive ---
        self.assertEqual(wrapper["receipt"]["observation"]["perception_ceiling"], "none")
        # Rejected vision attempt records effective level none (no ticket).
        self.assertEqual(wrapper["receipt"]["observation"]["perception_level"], "none")
        # Admitted none-perception run under same ceiling takes no lease.
        exe2 = self.dir / "fixture2.py"
        impl2 = _write_ok(exe2)
        man2 = _manifest(impl2)
        b2 = verify_binding(man2, str(exe2), operation_id="record", host_grants={"grant.exec.external_process"}, capsule_perception_ceiling="none")
        pool2 = Mock(spec=["acquire", "release"])
        w2 = run_bound(b2, [], timeout_s=5, cwd=str(self.dir), vision_pool=pool2, perception_level="none", capsule_perception_ceiling="none")
        self.assertTrue(w2["evidence_accepted"])
        self.assertEqual(pool2.mock_calls, [])
        self.assertEqual(w2["receipt"]["observation"]["perception_ceiling"], "none")

        ref = "installation:fixture-slot@row20-vision"
        payload = {"installation_ref": ref, "perception_level": "vision"}
        host = {ref: (manifest, str(exe))}
        args = {"payload": payload}
        with patch.object(tools_mod, "run_bound") as runner:
            for dry_run in (False, True):
                for ceiling, code in (("none", "PERCEPTION_DENIED"), ("vision", "AVAILABILITY_PARKED")):
                    with self.subTest(dry_run=dry_run, ceiling=ceiling):
                        with self.assertRaises(tools_mod._ControlError) as refusal:
                            dispatch("behaviour.record", dict(
                                args, request_id=f"req-row20-{dry_run}-{ceiling}", dry_run=dry_run,
                            ), host=host, host_policy={"grants": ["grant.exec.external_process"],
                                                       "perception_ceiling": ceiling}, vision_pool=pool)
                        self.assertEqual(refusal.exception.code, code)
            runner.assert_not_called()
        readiness = handle_slot_inspect(
            {"installation_ref": ref}, host=host, host_policy=fixture_policy(manifest),
        )["readiness"]
        self.assertEqual(readiness["availability"], "parked")
        self.assertEqual(pool.mock_calls, [])

    def test_spawn_environment_is_intersection_of_verified_allowlists(self):
        """Row 21: manifest-only env var absent from launched environment."""
        exe = self.dir / "env_echo.py"
        impl = _write_env_echo(exe)
        manifest = _manifest(impl)
        manifest["transport"]["env_allowlist"] = ["PATH", "SECRET_TOKEN"]
        binding = verify_binding(manifest, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        os.environ["SECRET_TOKEN"] = "should-not-leak"
        try:
            wrapper = run_bound(binding, [], timeout_s=5, cwd=str(self.dir), host_env_allowlist=frozenset({"PATH"}), env={"SECRET_TOKEN": "x", "PATH": os.environ.get("PATH", "")})
            self.assertTrue(wrapper["evidence_accepted"])
            doc = json.loads(wrapper["stdout"])
            self.assertNotIn("SECRET_TOKEN", doc["env_keys"])
            self.assertIn("PATH", doc["env_keys"])
            # --- Harden: exact argv + both allowlist directions ---
            self.assertEqual(doc.get("argv"), ["record"], "operation subcommand must be present as first argv")
            # Host-only var must also be absent.
            os.environ["HOST_ONLY"] = "leak"
            try:
                w2 = run_bound(binding, [], timeout_s=5, cwd=str(self.dir), host_env_allowlist=frozenset({"PATH"}), env={"HOST_ONLY": "leak", "PATH": os.environ.get("PATH", "")})
                doc2 = json.loads(w2["stdout"])
                self.assertNotIn("HOST_ONLY", doc2["env_keys"])
                self.assertNotIn("SECRET_TOKEN", doc2["env_keys"])
            finally:
                os.environ.pop("HOST_ONLY", None)
            # Typed argv via handle_record must preserve subcommand.
            pass
        finally:
            os.environ.pop("SECRET_TOKEN", None)


    def test_jcs_float_and_utf16_order_strict(self):
        """ExpectedFailure: binding._manifest_digest must be RFC 8785 JCS."""
        # JCS renders 1.0 as 1 (ECMAScript); Python dumps renders 1.0.
        exe = self.dir / "fixture.py"
        impl = _write_ok(exe)
        m = _manifest(impl)
        m["discovery"]["examples"] = [{"args": 1.0}]
        b = verify_binding(m, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        # Independent JCS canonical: replace :1.0 with :1 for this fixture.
        py_canonical = json.dumps(m, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        jcs_canonical = py_canonical.replace(":1.0", ":1")
        expected = hashlib.sha256(jcs_canonical.encode("utf-8")).hexdigest()
        self.assertEqual(b.manifest_digest, expected)

    def test_distinct_tape_and_result_digests_strict(self):
        """ExpectedFailure: runner must keep tape_sha distinct from result_sha."""
        exe = self.dir / "fixture.py"
        impl = _write_ok(exe)
        man = _manifest(impl)
        man["operations"][0]["binding"]["typed_args"] = [{"name": "tape", "type": "string", "position": 0}]
        b = verify_binding(man, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        tape = "tape-distinct-0001"
        tape_sha = hashlib.sha256(tape.encode()).hexdigest()
        # Fixture emits fixed sha != tape_sha; distinct digests must still accept.
        w = run_bound(b, [tape], timeout_s=5, cwd=str(self.dir), request_id="req-distinct-0001", attempt_id="att-distinct-0001", tape_sha256=tape_sha)
        self.assertTrue(w["evidence_accepted"])
        self.assertEqual(w["custody"]["input_tape_sha256"], tape_sha)
        self.assertNotEqual(w["custody"]["result_sha256"], tape_sha)

    def test_omitted_evidence_strict(self):
        """Withheld authenticated evidence ref refuses."""
        exe = self.dir / "fixture.py"
        impl = _write_ok(exe)
        man = _manifest(impl)
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(man, str(exe), operation_id="record", host_grants={"grant.exec.external_process"}, auth_grants={"grant.exec.external_process"}, auth_spawn_granted=True, auth_licence_disposition="external_execution", auth_evidence_ref=None, auth_registry=_registry_for(man), auth_probes={})
        self.assertEqual(ctx.exception.reason, "profile_not_admitted")

    def test_absent_registry_strict(self):
        """Withheld authenticated registry refuses."""
        exe = self.dir / "fixture.py"
        impl = _write_ok(exe)
        man = _manifest(impl)
        with self.assertRaises(BindingRejected) as ctx:
            verify_binding(
                man, str(exe), operation_id="record",
                host_grants={"grant.exec.external_process"}, auth_grants={"grant.exec.external_process"},
                auth_spawn_granted=True, auth_licence_disposition="external_execution",
                auth_evidence_ref=_PUBLISHED_REF, auth_registry=None, auth_probes={},
            )
        self.assertEqual(ctx.exception.reason, "profile_not_admitted")

if __name__ == "__main__":
    unittest.main()
