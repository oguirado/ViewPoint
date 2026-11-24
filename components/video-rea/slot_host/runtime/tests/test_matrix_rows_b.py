# SPDX-License-Identifier: AGPL-3.0-only
"""Acceptance-matrix rows 22-42 for external-binding (fresh runs only).

Every test uses a disposable fixture in a fresh temp dir, a fresh
request/attempt identity, and the production entry point named in the
matrix. No store, pool or exchange is faked beyond the injected vision
pool dependency (fake in tests per the lane rules) and fault-injection
callbacks (attempt_store, shutil patch) where the matrix requires an
interruption. Safe-PID rule: only PIDs this test started and recorded
are signalled, after verifying pid+starttime in /proc; never groups,
never pid<=1, no orphans, bounded reads.
"""

import copy
from dataclasses import replace
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from slot_host.compare.tests.test_comparators import behaviour_comparison, cases
from slot_host.runtime import runner as runner_mod
from slot_host.runtime.tests import test_owner_handoff as handoff
from slot_host.runtime.tests.test_owner_handoff import compare_fixture
from slot_host.projections import control_reply, json_invoke
from slot_host.projections import cli as projected_cli
from slot_host.projections.cli import MAX_ARGS_BYTES
from slot_host.runtime.binding import BindingRejected, sha256_of_file
from slot_host.runtime import tools as tools_mod
from slot_host.runtime.receipts import (
    build_attempt_receipt,
    evidence_accepted,
    finalize_receipt,
    receipt_digest,
    reconcile_run,
)
from slot_host.runtime.runner import _terminate_only, _kill_only
from slot_host.runtime.tools import register_installation, main as tools_main
from slot_host.runtime.tools import dispatch

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
    FIXTURE_RESULT_PAYLOAD,
    FIXTURE_RESULT_SHA256,
    FixtureLaunch as _AllowLiveAuthority,
    verified_fixture_binding as verify_binding,
    dispatch_fixture as _dispatch_orig,
)

setUpModule = fixture_retention
_GOOD_SHA = FIXTURE_RESULT_SHA256

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


_counter = [0]


def _fresh_req(tag: str) -> str:
    _counter[0] += 1
    s = f"req-m2-{tag}-{_counter[0]:04d}"
    # Must satisfy tools _REQUEST_ID_RE: 8-128 chars, first alnum.
    assert 8 <= len(s) <= 128
    return s


def _fresh_att(tag: str) -> str:
    _counter[0] += 1
    return f"att-m2-{tag}-{_counter[0]:04d}"


def _valid_obj(**overrides) -> dict:
    obj = {"schema": "rea.slot_result/v1", "status": "OK", "rc": 0, "sha256": _GOOD_SHA, "result_payload": FIXTURE_RESULT_PAYLOAD}
    obj.update(overrides)
    return obj


def _write_obj_fixture(path: Path, obj: dict, *, sleep_s: float = 0.0,
                       counter_file: str | None = None, kill_parent_after_s: float | None = None) -> str:
    """Write an executable fixture emitting exactly one JSON object.

    counter_file: absolute path appended with one line per launch.
    kill_parent_after_s: fixture sleeps then SIGINTs its parent (test proc).
    """
    inner = json.dumps(obj)
    lines = [
        b"#!/usr/bin/env python3\n",
        b"# SPDX-License-Identifier: AGPL-3.0-only\n",
        b"import json, sys, time, signal, os\n",
    ]
    if counter_file is not None:
        lines.append(
            b"open(" + repr(counter_file).encode() + b", 'a').write('x\\n')\n"
        )
    if sleep_s:
        lines.append(b"time.sleep(" + repr(float(sleep_s)).encode() + b")\n")
    if kill_parent_after_s is not None:
        lines.append(
            b"time.sleep(" + repr(float(kill_parent_after_s)).encode() + b")\n"
            b"os.kill(os.getppid(), signal.SIGINT)\n"
            b"time.sleep(30)\n"
        )
    lines.append(b"sys.stdout.write(json.dumps(" + inner.encode() + b")+'\\n')\n")
    lines.append(b"sys.stdout.flush()\n")
    path.write_bytes(b"".join(lines))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return sha256_of_file(path)


def _write_sleeper(path: Path, *, counter_file: str | None = None) -> str:
    lines = [
        b"#!/usr/bin/env python3\n",
        b"# SPDX-License-Identifier: AGPL-3.0-only\n",
        b"import time, sys\n",
    ]
    if counter_file is not None:
        lines.append(
            b"open(" + repr(counter_file).encode() + b", 'a').write('x\\n')\n"
        )
    lines.append(b"time.sleep(30)\n")
    path.write_bytes(b"".join(lines))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return sha256_of_file(path)


def _write_mixed_overflow(path: Path, stdout_bytes: bytes, stderr_bytes: bytes) -> str:
    script = (
        b"#!/usr/bin/env python3\n"
        b"# SPDX-License-Identifier: AGPL-3.0-only\n"
        b"import sys\n"
        b"sys.stdout.buffer.write(b'" + stdout_bytes.replace(b"\\", b"\\\\").replace(b"'", b"\\'") + b"')\n"
        b"sys.stdout.buffer.flush()\n"
        b"sys.stderr.buffer.write(b'" + stderr_bytes.replace(b"\\", b"\\\\").replace(b"'", b"\\'") + b"')\n"
        b"sys.stderr.buffer.flush()\n"
    )
    path.write_bytes(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return sha256_of_file(path)


def _write_scratch_saboteur(path: Path) -> str:
    script = (
        b"#!/usr/bin/env python3\n"
        b"# SPDX-License-Identifier: AGPL-3.0-only\n"
        b"import json, os, shutil, sys\n"
        b"cwd = os.getcwd()\n"
        b"shutil.rmtree(cwd, ignore_errors=True)\n"
        b"open(cwd, 'w').write('sabotaged')\n"
        b"sys.stdout.write(json.dumps({'schema':'rea.slot_result/v1','status':'OK','rc':0,'sha256':'"
        + _GOOD_SHA.encode()
        + b"'})+'\\n')\n"
        b"sys.stdout.flush()\n"
    )
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
    if type(pid) is not int or pid <= 1 or type(starttime) is not int:
        return
    if _proc_starttime(pid) != starttime:
        return
    try:
        os.kill(pid, 9)
    except (ProcessLookupError, PermissionError):
        pass


def _manifest(impl_sha: str, *, profile: str = "external_process",
              status: str = "registered", op_limits=None,
              perception_required: str = "none") -> dict:
    def _op(oid, role):
        return {
            "id": oid,
            "role": role,
            "input_ref": f"urn:rea.slot_request:v2#slot={role}",
            "output_ref": f"urn:rea.slot_result:v2#slot={role}",
            "binding": {"kind": "cli_subcommand", "argv": [oid], "typed_args": []},
            "required_effects": ["process.spawn:child"],
            "required_grants": ["grant.exec.external_process"],
            "limits": dict(op_limits) if op_limits else {"timeout_s": 5, "max_output_bytes": 4096},
            "retry_semantics": {"mode": "at_most_once", "on_interrupt": "UNCERTAIN_OUTCOME"},
        }
    if perception_required == "vision":
        perc = {"required_level": "vision", "optional_levels": [],
                "observation_sources": [{"id": "px", "level": "vision",
                                       "producer": "host_vision_engine",
                                       "outside_capsule": True,
                                       "vision_pool": {"required": True,
                                                     "owner": "ViewPoint_Tools",
                                                     "ticket_field": "vision_pool_ticket"}}]}
    else:
        perc = {"required_level": perception_required, "optional_levels": [],
                "observation_sources": [{"id": "tool-evidence",
                                       "level": perception_required,
                                       "producer": "tool_stdio",
                                       "outside_capsule": True}]}
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
            {"role": "replay",
             "request_schema_ref": "urn:rea.slot_request:v2",
             "request_digest": _REQ_DIGEST,
             "result_schema_ref": "urn:rea.slot_result:v2",
             "result_digest": _RES_DIGEST,
             "execution_profile": profile},
            {"role": "verify",
             "request_schema_ref": "urn:rea.slot_request:v2",
             "request_digest": _REQ_DIGEST,
             "result_schema_ref": "urn:rea.slot_result:v2",
             "result_digest": _RES_DIGEST,
             "execution_profile": profile},
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
        "operations": [_op("record", "replay"), _op("replay", "replay"), _op("compare", "verify")],
        "capabilities": {
            "target_kinds": ["fixture_target"],
            "platforms": ["linux"],
            "prerequisite_probes": [],
            "optional_features": [],
            "adapter_identity": {"adapter_id": "fixture.v1",
                                 "adapter_sha256": _ADAPTER,
                                 "provenance": "host_authored"},
        },
        "perception": perc,
        "record_replay": {"input_codec": "json", "output_codec": "json",
                          "observable_channels": ["error"],
                          "state_domain": "per_case",
                          "effects": ["process.spawn:child"],
                          "determinism_modes": ["recorded_inputs"],
                          "freshness_inputs": ["fixture"]},
        "comparison": {"comparator_id": "fixture.compare", "version": "1",
                       "policy_ref": "policy:fixture@1",
                       "coverage_requirements": {"channel": "error"},
                       "tolerance_units": "none",
                       "waiver_policy": {"source": "host_policy",
                                         "binds": ["input", "actual", "reason",
                                                    "scope", "policy_version"],
                                         "stale": "fail"}},
        "evidence": {"shared_schema_ref": "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2",
                     "required_rea_fields": ["episode", "observation", "slot"],
                     "artifact_roles": ["evidence_bundle"],
                     "retention": "evidence_store_attempt",
                     "visibility": "redacted_projection"},
        "conformance": {"fixture_refs": ["fixture:1"],
                        "negative_control_refs": ["control:1"],
                        "supported_entry_points": ["cli", "mcp", "json"],
                        "required_checks": ["admit-and-project",
                                            "outside-record-replay-compare",
                                            "perception-and-pool",
                                            "transport-parity-and-retry"]},
        "discovery": {"summary": "fixture", "search_terms": ["fixture"],
                      "operation_descriptions": {"record": "record",
                                                 "replay": "replay",
                                                 "compare": "compare"},
                      "examples": []},
    }


def _register(manifest, exe: Path, ref: str) -> str:
    register_installation(ref, manifest, str(exe))
    return ref


def _count_lines(path: Path) -> int:
    try:
        return len([l for l in path.read_text().splitlines() if l])
    except FileNotFoundError:
        return 0


def _clear_store() -> None:
    try:
        tools_mod._REQUEST_STORE.clear()
    except Exception:
        pass


class MatrixRowsBTest(unittest.TestCase):
    def setUp(self):
        _clear_store()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_pinned_provider_exchange_rejects_each_identity_substitution(self):
        # Positive: pinned exchange with matching identities is accepted.
        exe = self.dir / "valid.py"
        impl = _write_obj_fixture(exe, _valid_obj())
        manifest = _manifest(impl)
        binding = verify_binding(manifest, str(exe), operation_id="record",
                                 host_grants={"grant.exec.external_process"})
        req0 = _fresh_req("r22-ok")
        w0 = run_bound(binding, [], timeout_s=5, cwd=str(self.dir),
                       request_id=req0, attempt_id=_fresh_att("r22-ok"))
        self.assertTrue(w0["evidence_accepted"], "positive pinned exchange must accept")
        self.assertTrue(evidence_accepted(w0))
        self.assertIsNotNone(w0["receipt"]["attempt"].get("slot_result"))
        # Independently measured digest: custody result hash must equal the
        # host-measured provider bytes hash, not a child-asserted field.
        self.assertEqual(w0["custody"]["result_sha256"],
                         w0["receipt"]["attempt"]["slot_result"]["sha256"])
        # Each single-field substitution alone invalidates the exchange.
        cases = [
            ("provider_id", {"provider_id": "other-provider"}),
            ("provider_revision", {"provider_revision": "9.9.9"}),
            ("execution_profile", {"execution_profile": "offline_fixture"}),
            ("role", {"role": "verify"}),
            ("record_schema", {"record_schema": "rea.slot_result/v2"}),
            ("request_id", {"request_id": "req-foreign-9999"}),
        ]
        for name, forged in cases:
            with self.subTest(field=name):
                fpath = self.dir / f"forged_{name}.py"
                fimpl = _write_obj_fixture(fpath, _valid_obj(**forged))
                fman = _manifest(fimpl)
                fb = verify_binding(fman, str(fpath), operation_id="record",
                                    host_grants={"grant.exec.external_process"})
                req = _fresh_req(f"r22-{name}")
                w = run_bound(fb, [], timeout_s=5, cwd=str(self.dir),
                              request_id=req, attempt_id=_fresh_att(f"r22-{name}"))
                self.assertFalse(w["evidence_accepted"],
                                 f"substituted {name} must not accept")
                self.assertFalse(evidence_accepted(w))
                self.assertFalse(w["custody"]["producer_binding_verified"],
                                 f"substituted {name} must downgrade custody")
                self.assertFalse(w["receipt"]["observation"]["producer"]["process_bound"])
                self.assertIsNone(w["receipt"]["attempt"].get("slot_result"),
                                  f"substituted {name} must leave no attempt.slot_result")

    def test_os_exit_and_minimal_status_cannot_substitute_for_pinned_result(self):
        # Positive: full pinned result is accepted.
        exe = self.dir / "valid.py"
        impl = _write_obj_fixture(exe, _valid_obj())
        manifest = _manifest(impl)
        binding = verify_binding(manifest, str(exe), operation_id="record",
                                 host_grants={"grant.exec.external_process"})
        w0 = run_bound(binding, [], timeout_s=5, cwd=str(self.dir),
                       request_id=_fresh_req("r23-ok"), attempt_id=_fresh_att("r23-ok"))
        self.assertTrue(w0["evidence_accepted"])
        # Negative: minimal status/rc with successful exit is not a result.
        for name, obj in (
            ("bare", {"status": "OK", "rc": 0}),
            ("v1-no-sha", {"schema": "rea.slot_result/v1", "status": "OK", "rc": 0}),
        ):
            with self.subTest(minimal=name):
                fpath = self.dir / f"minimal_{name}.py"
                fimpl = _write_obj_fixture(fpath, obj)
                fman = _manifest(fimpl)
                fb = verify_binding(fman, str(fpath), operation_id="record",
                                    host_grants={"grant.exec.external_process"})
                w = run_bound(fb, [], timeout_s=5, cwd=str(self.dir),
                              request_id=_fresh_req(f"r23-{name}"),
                              attempt_id=_fresh_att(f"r23-{name}"))
                self.assertEqual(w["rc"], 0, "OS exit was successful yet must not substitute")
                self.assertFalse(w["evidence_accepted"])
                self.assertFalse(evidence_accepted(w))
                self.assertFalse(w["custody"]["producer_binding_verified"])
                sr = w["receipt"]["attempt"].get("slot_result")
                self.assertIsNone(sr, "minimal status must leave no attempt.slot_result")
                # No synthetic BUDGET_EXCEEDED/6 status anywhere.
                blob = json.dumps(w)
                self.assertNotIn("BUDGET_EXCEEDED", blob)

    def test_result_reference_digest_must_match_custody_witness(self):
        from slot_host.runtime.receipts import finalize_receipt

        exe = self.dir / "valid.py"
        impl = _write_obj_fixture(exe, _valid_obj())
        binding = verify_binding(_manifest(impl), str(exe), operation_id="record",
                                 host_grants={"grant.exec.external_process"})
        wrapper = run_bound(binding, [], timeout_s=5, cwd=str(self.dir),
                            request_id=_fresh_req("r24-ok"), attempt_id=_fresh_att("r24-ok"))
        custody, receipt = wrapper["custody"], wrapper["receipt"]
        result = receipt["attempt"]["slot_result"]
        self.assertEqual(result["sha256"], custody["result_sha256"])
        self.assertEqual(wrapper["shared_records"]["terminal"]["artifact"]["sha256"], _GOOD_SHA)
        self.assertTrue(evidence_accepted(wrapper))
        for bad_digest in (None, "f" * 64):
            with self.subTest(digest=bad_digest):
                bad_custody = dict(custody, result_sha256=bad_digest)
                finalized = finalize_receipt(receipt, result, custody=bad_custody)
                self.assertFalse(finalized["observation"]["producer"]["process_bound"])
                self.assertNotIn("slot_result", finalized["attempt"])
                self.assertFalse(evidence_accepted(dict(wrapper, custody=bad_custody)))
        substituted = copy.deepcopy(wrapper)
        substituted["receipt"]["attempt"]["slot_result"]["sha256"] = "a" * 64
        self.assertFalse(evidence_accepted(substituted))
        self.assertTrue(evidence_accepted(wrapper))

    def test_combined_output_overflow_is_bounded_and_is_not_provider_status(self):
        # Positive: small valid capture accepts with overflow False.
        exe = self.dir / "valid.py"
        impl = _write_obj_fixture(exe, _valid_obj())
        manifest = _manifest(impl)
        binding = verify_binding(manifest, str(exe), operation_id="record",
                                 host_grants={"grant.exec.external_process"})
        w0 = run_bound(binding, [], timeout_s=5, cwd=str(self.dir),
                       request_id=_fresh_req("r25-ok"), attempt_id=_fresh_att("r25-ok"))
        self.assertTrue(w0["evidence_accepted"])
        self.assertFalse(w0["custody"]["overflow"])
        # Negative: 3000+3000 across two streams exceeds the 4096 combined
        # cap while each stream alone stays below it.
        loud = self.dir / "loud.py"
        limpl = _write_mixed_overflow(loud, b"x" * 3000, b"y" * 3000)
        lman = _manifest(limpl)
        lb = verify_binding(lman, str(loud), operation_id="record",
                            host_grants={"grant.exec.external_process"})
        w = run_bound(lb, [], timeout_s=5, cwd=str(self.dir),
                      request_id=_fresh_req("r25-ov"), attempt_id=_fresh_att("r25-ov"))
        self.assertTrue(w["custody"]["overflow"])
        self.assertFalse(w["evidence_accepted"])
        self.assertFalse(evidence_accepted(w))
        self.assertIsNone(w["receipt"]["attempt"].get("slot_result"),
                          "overflow must leave no attempt.slot_result")
        retained = len((w["stdout"] or "").encode()) + len((w["stderr"] or "").encode())
        self.assertLessEqual(retained, 4096, "retained combined output must stay within cap")
        self.assertNotIn("BUDGET_EXCEEDED", json.dumps(w["receipt"]))

    # -- CLI cap / descriptor (rows 26-27): actual tools.main, no dispatch. --
    def _call_cli(self, argv, stdin_text: str | None = None):
        calls: list = []

        def fake_dispatch(tool, arguments, **kw):
            calls.append((tool, copy.deepcopy(arguments)))
            return {"ok": True, "request_id": "x", "result": {}}

        out = io.StringIO()
        err = io.StringIO()
        old_out, old_err, old_in = sys.stdout, sys.stderr, sys.stdin
        sys.stdout, sys.stderr = out, err
        if stdin_text is not None:
            sys.stdin = io.StringIO(stdin_text)
        try:
            rc = tools_main(argv, dispatch_fn=fake_dispatch)
        finally:
            sys.stdout, sys.stderr, sys.stdin = old_out, old_err, old_in
        return rc, calls, err.getvalue()

    def test_runtime_cli_rejects_oversized_input_before_decode(self):
        fixture = handoff.U12Handoff()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        request_id = _fresh_req("r26cli")
        document = json.dumps(fixture.args(request_id=request_id, dry_run=True, params={"seed": 7}))
        at_limit = document.ljust(MAX_ARGS_BYTES)
        oversized = at_limit + " "
        calls = []

        def dispatch_preview(envelope):
            normalized = json_invoke(fixture.manifest, fixture.bundle, envelope, **fixture.host())
            calls.append(envelope)
            return control_reply(request_id, "OK", ok=True, result={
                "kind": "dry_run", "dry_run": True, "outside_capsule": True,
                "normalized_request": normalized,
            })

        projector = fixture.projector(dispatch_preview)
        path = self.dir / "r26.json"
        argv = ["slot", "call", "rea", "record", "--request-id", request_id]
        path.write_text(at_limit, encoding="utf-8")
        code, stdout, _ = fixture.cli_run(argv + ["--args-file", str(path)], projector)
        self.assertEqual((code, json.loads(stdout)["code"]), (0, "OK"))
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0]["dry_run"], True)

        path.write_text(oversized, encoding="utf-8")
        for option, stdin in ((["--args-file", str(path)], None),
                              (["--args", oversized], None),
                              (["--args-file", "-"], oversized)):
            with self.subTest(source=option[0], stdin=stdin is not None):
                fixture.assert_schema_refusal(fixture.cli_run(argv + option, projector, stdin), request_id)
        self.assertEqual(len(calls), 1)

        class ShortReads(io.BytesIO):
            def read(self, size):
                return super().read(min(size, len(document)))

        stream = ShortReads(oversized.encode("utf-8"))
        with patch.object(sys, "stdin", stream):
            fixture.assert_schema_refusal(fixture.cli_run(argv + ["--args-file", "-"], projector),
                                          request_id)
        self.assertEqual(stream.tell(), MAX_ARGS_BYTES + 1)
        self.assertEqual(len(calls), 1)

        # A descriptor substitution is independently observable even when
        # the substituted source would otherwise yield a valid document.
        fifo = self.dir / "r26.fifo"
        os.mkfifo(fifo)
        real_open = os.open

        def open_substitute(name, flags, *args, **kwargs):
            if name == str(path):
                name = str(fifo)
            return real_open(name, flags, *args, **kwargs)

        with patch.object(projected_cli.os, "open", side_effect=open_substitute), \
                patch.object(projected_cli.os, "read", side_effect=[document.encode(), b""]) as read:
            reply = fixture.cli_run(argv + ["--args-file", str(path)], projector)
        fixture.assert_schema_refusal(reply, request_id)
        self.assertIn("regular opened descriptor", reply[2])
        read.assert_not_called()
        self.assertEqual(len(calls), 1)

    def test_runtime_cli_checks_opened_argument_descriptor(self):
        # Positive: regular file within cap dispatches.
        small_doc = json.dumps({"request_id": _fresh_req("r27small"),
                                "payload": {"installation_ref": "installation:x",
                                            "operation_id": "record"}})
        good = self.dir / "args.json"
        good.write_text(small_doc, encoding="utf-8")
        rc_ok, calls_ok, _ = self._call_cli(
            ["mini", "behaviour.record", "--args-file", str(good)])
        self.assertEqual(rc_ok, 0)
        self.assertEqual(len(calls_ok), 1)
        # Directory is nonregular: must fail before dispatch, no blocking.
        notdir = self.dir / "notregular-dir"
        notdir.mkdir()
        rc, calls, _ = self._call_cli(
            ["mini", "behaviour.record", "--args-file", str(notdir)])
        self.assertEqual(rc, 14)
        self.assertEqual(calls, [])
        # FIFO: must be rejected via opened-descriptor check without
        # blocking the host waiting for a writer.
        fifo = self.dir / "args.fifo"
        os.mkfifo(fifo)
        result: dict = {}

        def _run_fifo():
            try:
                rc2, calls2, _e = self._call_cli(
                    ["mini", "behaviour.record", "--args-file", str(fifo)])
                result["rc"] = rc2
                result["calls"] = calls2
            except BaseException as exc:  # noqa: BLE001 - record for assert
                result["exc"] = exc

        worker = threading.Thread(target=_run_fifo, daemon=True)
        worker.start()
        worker.join(timeout=2.0)
        if worker.is_alive():
            # Current code opened the FIFO for reading and blocks; unblock
            # by supplying the writer so the suite can finish, then fail.
            try:
                fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                os.write(fd, small_doc.encode())
                os.close(fd)
            except OSError:
                pass
            worker.join(timeout=2.0)
            self.fail("FIFO argument source blocked the host (no descriptor check)")
        self.assertIn("rc", result, f"fifo CLI raised {result.get('exc')!r}")
        self.assertEqual(result["rc"], 14)
        self.assertEqual(result["calls"], [])

    @unittest.skip("owner-pending: MAIN")
    def test_concurrent_identical_requests_share_at_most_once_claim(self):
        self.fail("MAIN must supply central cross-face claims and join semantics")

    def test_concurrent_offline_fixture_requests_share_local_claim(self):
        # Slow fixture widens the race window: concurrent duplicates must
        # still share one claim and launch exactly once.
        counter = self.dir / "launches.txt"
        counter.write_text("", encoding="utf-8")
        exe = self.dir / "slow.py"
        impl = _write_obj_fixture(exe, _valid_obj(),
                                  sleep_s=0.6, counter_file=str(counter))
        manifest = _manifest(impl)
        ref = _register(manifest, exe, f"installation:m2-conc-{_counter[0]}")
        req = _fresh_req("r28conc")
        payload = {"installation_ref": ref, "operation_id": "record",
                   "argv": [],
                   "host_grants": ["grant.exec.external_process"],
                   "attempt_id": _fresh_att("r28conc")}
        # Positive contrast: sequential identical retries share one launch.
        first = dispatch("behaviour.record",
                         {"request_id": req, "payload": copy.deepcopy(payload)},
                         host=None, host_policy={"grants": ["grant.exec.external_process"]})
        self.assertTrue(first["ok"])
        self.assertTrue(first["result"]["evidence_accepted"])
        self.assertEqual(_count_lines(counter), 1)
        second = dispatch("behaviour.record",
                          {"request_id": req, "payload": copy.deepcopy(payload)},
                          host=None, host_policy={"grants": ["grant.exec.external_process"]})
        # Same attempt_id keeps the payload identical; second joins claim.
        self.assertEqual(second, first)
        self.assertEqual(_count_lines(counter), 1, "sequential retry must not relaunch")
        # Concurrent identical requests under a fresh identity: one launch.
        _clear_store()
        counter.write_text("", encoding="utf-8")
        exe2 = self.dir / "slow2.py"
        impl2 = _write_obj_fixture(exe2, _valid_obj(),
                                   sleep_s=0.6, counter_file=str(counter))
        ref2 = _register(_manifest(impl2), exe2, f"installation:m2-conc2-{_counter[0]}")
        req2 = _fresh_req("r28conc2")
        att2 = _fresh_att("r28conc2")
        payload2 = {"installation_ref": ref2, "operation_id": "record",
                    "argv": [],
                    "host_grants": ["grant.exec.external_process"],
                    "attempt_id": att2}
        results: list = []
        errors: list = []
        barrier = threading.Barrier(6)

        def _worker():
            try:
                barrier.wait(timeout=5)
                results.append(dispatch("behaviour.record",
                                        {"request_id": req2,
                                         "payload": copy.deepcopy(payload2)},
                                        host=None, host_policy={"grants": ["grant.exec.external_process"]}))
            except BaseException as exc:  # noqa: BLE001 - collected for assert
                errors.append(exc)

        threads = [threading.Thread(target=_worker, daemon=True) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertFalse([t for t in threads if t.is_alive()],
                         "concurrent dispatch deadlocked")
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 6)
        self.assertEqual(_count_lines(counter), 1,
                         "concurrent identical requests must launch once")
        for r in results:
            self.assertTrue(r["ok"] and r["result"]["evidence_accepted"])

    @unittest.skip("owner-pending: MAIN")
    def test_dry_run_change_conflicts_in_shared_request_store(self):
        self.fail("MAIN must supply the central preview and live claim boundary")

    def test_offline_fixture_dry_run_change_conflicts(self):
        counter = self.dir / "dry-launches.txt"
        counter.write_text("", encoding="utf-8")
        exe = self.dir / "valid.py"
        impl = _write_obj_fixture(exe, _valid_obj(), counter_file=str(counter))
        ref = _register(_manifest(impl), exe, f"installation:m2-dry-{_counter[0]}")
        # Positive: identical dry_run retry returns preview, zero launches.
        req = _fresh_req("r29dry")
        payload = {"installation_ref": ref, "operation_id": "record",
                   "argv": [], "host_grants": ["grant.exec.external_process"]}
        r1 = dispatch("behaviour.record", {"request_id": req, "dry_run": True,
                                           "payload": copy.deepcopy(payload)},
                      host=None, host_policy={"grants": ["grant.exec.external_process"]})
        self.assertTrue(r1["ok"] and r1["result"]["planned"] is True)
        r2 = dispatch("behaviour.record", {"request_id": req, "dry_run": True,
                                           "payload": copy.deepcopy(payload)},
                      host=None, host_policy={"grants": ["grant.exec.external_process"]})
        self.assertEqual(r2, r1)
        self.assertEqual(_count_lines(counter), 0, "preview must not launch")
        # Preview-first: changing dry_run under same id conflicts.
        with self.assertRaises(Exception) as cm:
            dispatch("behaviour.record", {"request_id": req, "dry_run": False,
                                          "payload": copy.deepcopy(payload)},
                     host=None, host_policy={"grants": ["grant.exec.external_process"]})
        self.assertEqual(getattr(cm.exception, "code", None), "REQUEST_ID_CONFLICT")
        self.assertEqual(_count_lines(counter), 0,
                         "changed request must not launch")
        # Live-first: preview under same id conflicts; live stays claimed.
        _clear_store()
        counter.write_text("", encoding="utf-8")
        req2 = _fresh_req("r29live")
        live = dispatch("behaviour.record", {"request_id": req2, "dry_run": False,
                                              "payload": copy.deepcopy(payload)},
                        host=None, host_policy={"grants": ["grant.exec.external_process"]})
        self.assertTrue(live["ok"] and live["result"]["evidence_accepted"])
        self.assertEqual(_count_lines(counter), 1)
        with self.assertRaises(Exception) as cm2:
            dispatch("behaviour.record", {"request_id": req2, "dry_run": True,
                                          "payload": copy.deepcopy(payload)},
                     host=None, host_policy={"grants": ["grant.exec.external_process"]})
        self.assertEqual(getattr(cm2.exception, "code", None), "REQUEST_ID_CONFLICT")
        self.assertEqual(_count_lines(counter), 1, "preview must not launch again")
        # Preview cannot satisfy live: identical live retry reuses reply.
        again = dispatch("behaviour.record", {"request_id": req2, "dry_run": False,
                                               "payload": copy.deepcopy(payload)},
                         host=None, host_policy={"grants": ["grant.exec.external_process"]})
        self.assertEqual(again, live)

    @unittest.skip("owner-pending: MAIN")
    def test_authorized_preview_does_not_create_execution_evidence(self):
        self.fail("MAIN must supply the enrolled preview route")

    def test_offline_fixture_preview_does_not_create_execution_evidence(self):
        counter = self.dir / "prev-launches.txt"
        counter.write_text("", encoding="utf-8")
        exe = self.dir / "valid.py"
        impl = _write_obj_fixture(exe, _valid_obj(), counter_file=str(counter))
        ref = _register(_manifest(impl), exe, f"installation:m2-prev-{_counter[0]}")
        payload = {"installation_ref": ref, "operation_id": "record",
                   "argv": [], "host_grants": ["grant.exec.external_process"]}
        # Fresh admitted live request returns validated result + receipt.
        live = dispatch("behaviour.record",
                        {"request_id": _fresh_req("r30live"),
                         "payload": copy.deepcopy(payload)}, host=None, host_policy={"grants": ["grant.exec.external_process"]})
        self.assertTrue(live["ok"])
        self.assertTrue(live["result"]["evidence_accepted"])
        self.assertTrue(evidence_accepted(live["result"]))
        self.assertIn("receipt", live["result"])
        self.assertIsNotNone(live["result"]["receipt"]["attempt"].get("slot_result"))
        self.assertEqual(_count_lines(counter), 1)
        # Same intent as preview under fresh identity: planned reply only.
        preview = dispatch("behaviour.record",
                           {"request_id": _fresh_req("r30prev"), "dry_run": True,
                            "payload": copy.deepcopy(payload)}, host=None, host_policy={"grants": ["grant.exec.external_process"]})
        self.assertTrue(preview["ok"])
        self.assertTrue(preview["result"]["planned"] is True)
        self.assertNotIn("receipt", preview["result"])
        self.assertNotIn("slot_result", json.dumps(preview))
        self.assertNotIn("evidence_accepted", json.dumps(preview))
        self.assertEqual(_count_lines(counter), 1,
                         "preview must create no execution evidence")

    def test_timeout_reaps_bound_child_and_preserves_sibling(self):
        import subprocess
        marker = self.dir / "closed-pipes"
        delay = self.dir / "delay"
        sleeper = self.dir / "sleeper.py"
        sleeper.write_text(
            "#!/usr/bin/env python3\n"
            "# SPDX-License-Identifier: AGPL-3.0-only\n"
            "import os, pathlib, time\n"
            f"print({json.dumps(_valid_obj())!r}, flush=True)\n"
            "os.close(1)\n"
            "os.close(2)\n"
            f"pathlib.Path({str(marker)!r}).write_text(os.getcwd())\n"
            f"if pathlib.Path({str(delay)!r}).exists(): time.sleep(4)\n",
            encoding="utf-8",
        )
        sleeper.chmod(0o700)
        binding = verify_binding(_manifest(sha256_of_file(sleeper)), str(sleeper), operation_id="record",
                                 host_grants={"grant.exec.external_process"})
        sibling_script = self.dir / "sibling"
        _write_sleeper(sibling_script)
        sibling = subprocess.Popen([str(sibling_script)])
        sibling_start = _proc_starttime(sibling.pid)
        try:
            self.assertIsNotNone(sibling_start)
            good = run_bound(binding, [], timeout_s=0.5, request_id=_fresh_req("r31good"),
                             attempt_id=_fresh_att("r31good"))
            self.assertTrue(evidence_accepted(good))
            self.assertFalse(Path(marker.read_text(encoding="utf-8")).exists())
            self.assertIsNone(_proc_starttime(good["pid"]))
            marker.unlink()
            delay.touch()
            held = []

            def retain(receipt):
                if not held:
                    self.assertFalse(marker.exists())
                held.append(receipt)

            started = time.monotonic()
            wrapped = run_bound(binding, [], timeout_s=0.5, attempt_store=retain,
                                request_id=_fresh_req("r31"), attempt_id=_fresh_att("r31"))
            self.assertLess(time.monotonic() - started, 3)
            self.assertTrue(marker.is_file(), "child did not reach pipe closure")
            scratch = Path(marker.read_text(encoding="utf-8"))
            self.assertFalse(scratch.exists())
            self.assertTrue(wrapped["timed_out"])
            self.assertTrue(wrapped["custody"]["cleanup_observed"])
            self.assertFalse(wrapped["evidence_accepted"])
            self.assertFalse(evidence_accepted(wrapped))
            self.assertEqual(len(held), 2)
            self.assertTrue(held[0]["attempt"]["opened_before_dispatch"])
            self.assertEqual(held[-1], wrapped["receipt"])
            terminal = wrapped["receipt"]
            self.assertEqual(terminal["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
            self.assertFalse(terminal["observation"]["producer"]["process_bound"])
            self.assertNotIn("slot_result", terminal["attempt"])
            pid = wrapped["custody"]["bound_process_id"]
            self.assertGreater(pid, 1)
            self.assertIsNone(_proc_starttime(pid))
            self.assertIsNone(sibling.poll())
            self.assertEqual(_proc_starttime(sibling.pid), sibling_start)
            with patch.object(runner_mod.subprocess, "Popen",
                              side_effect=RuntimeError("invalid deadline reached spawn")) as spawn:
                with self.assertRaisesRegex(ValueError, "finite and positive"):
                    run_bound(binding, [], timeout_s=float("nan"))
                spawn.assert_not_called()
        finally:
            _safe_kill(sibling.pid, sibling_start)
            sibling.wait(timeout=5)

    def test_supervision_interrupt_retains_uncertain_terminal_attempt(self):
        exe = self.dir / "interrupt.py"
        exe.write_text("#!/usr/bin/env python3\n# SPDX-License-Identifier: AGPL-3.0-only\n"
                       "import sys\nsys.stdin.buffer.read(1)\nimport time\ntime.sleep(0.25)\n",
                       encoding="utf-8")
        exe.chmod(0o700)
        binding = verify_binding(_manifest(sha256_of_file(exe)), str(exe), operation_id="record",
                                 host_grants={"grant.exec.external_process"})

        for record_identity in (True, False):
            with self.subTest(record_identity=record_identity):
                held = []
                identity = []

                def interrupt(proc, *_):
                    identity[:] = [proc, _proc_starttime(proc.pid)]
                    raise KeyboardInterrupt("interrupted collection")

                read_identity = _proc_starttime if record_identity else lambda _: None
                with patch.object(runner_mod, "_proc_starttime", side_effect=read_identity), patch.object(
                    runner_mod, "_collect_output", side_effect=interrupt,
                ), patch.object(runner_mod, "_terminate_only", wraps=_terminate_only) as terminate, patch.object(
                    runner_mod, "_kill_only", wraps=_kill_only,
                ) as kill:
                    try:
                        with self.assertRaisesRegex(KeyboardInterrupt, "interrupted collection"):
                            run_bound(binding, [], timeout_s=5, cwd=self.dir, attempt_store=held.append,
                                      request_id=_fresh_req("r32int"), attempt_id=_fresh_att("r32int"))
                        proc, start = identity
                        self.assertIs(type(start), int)
                        self.assertIsNone(_proc_starttime(proc.pid))
                        self.assertIsNotNone(proc.returncode)
                        if not record_identity:
                            terminate.assert_not_called()
                            kill.assert_not_called()
                        opened, terminal = held
                        self.assertNotIn("slot_result", opened["attempt"])
                        self.assertEqual(terminal["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
                        self.assertIs(terminal["observation"]["producer"]["process_bound"], False)
                        self.assertNotIn("slot_result", terminal["attempt"])
                        self.assertFalse(evidence_accepted({"receipt": terminal, "custody": {}}))
                    finally:
                        if identity:
                            proc, start = identity
                            if proc.poll() is None:
                                _safe_kill(proc.pid, start)
                            proc.wait(timeout=2)

    def test_scratch_cleanup_interrupt_retains_uncertain_terminal_attempt(self):
        self.enterContext(patch.object(tempfile, "tempdir", str(self.dir)))
        # Primary fresh-run case: the fixture replaces its scratch dir with
        # a plain file, so verified removal cannot succeed. The host must
        # retain UNCERTAIN_OUTCOME with cleanup not certified.
        sab = self.dir / "sab.py"
        sim = _write_scratch_saboteur(sab)
        sb = verify_binding(_manifest(sim), str(sab),
                            operation_id="record",
                            host_grants={"grant.exec.external_process"})
        # cwd=None gives host-owned scratch that the fixture sabotages.
        # The sabotaged file itself is removed by the test cleanup below.
        held: list = []
        w = run_bound(sb, [], timeout_s=5, cwd=None,
                      attempt_store=held.append,
                      request_id=_fresh_req("r33sab"),
                      attempt_id=_fresh_att("r33sab"))
        self.assertEqual(w["receipt"]["attempt"]["control_outcome"],
                         "UNCERTAIN_OUTCOME")
        self.assertFalse(w["custody"]["cleanup_observed"])
        self.assertFalse(w["evidence_accepted"])
        self.assertFalse(evidence_accepted(w))
        self.assertIsNone(w["receipt"]["attempt"].get("slot_result"),
                          "scratch uncertainty must omit attempt.slot_result")
        self.assertGreaterEqual(len(held), 1)
        self.assertEqual(held[-1]["attempt"]["control_outcome"],
                         "UNCERTAIN_OUTCOME")
        # KeyboardInterrupt during rmtree itself: retained before re-raise.
        exe = self.dir / "valid.py"
        impl = _write_obj_fixture(exe, _valid_obj())
        binding = verify_binding(_manifest(impl), str(exe),
                                 operation_id="record",
                                 host_grants={"grant.exec.external_process"})
        held2: list = []
        import shutil as _shutil
        orig_rmtree = _shutil.rmtree
        state = {"armed": True}

        def _boom_rmtree(path, *a, **k):
            if state["armed"] and str(path).startswith("/tmp/"):
                state["armed"] = False
                raise KeyboardInterrupt("scratch interrupt")
            return orig_rmtree(path, *a, **k)

        _shutil.rmtree = _boom_rmtree
        try:
            with self.assertRaises(KeyboardInterrupt):
                run_bound(binding, [], timeout_s=5, cwd=None,
                          attempt_store=held2.append,
                          request_id=_fresh_req("r33ki"),
                          attempt_id=_fresh_att("r33ki"))
        finally:
            _shutil.rmtree = orig_rmtree
        self.assertGreaterEqual(len(held2), 2)
        terminal = held2[-1]
        self.assertEqual(terminal["attempt"]["control_outcome"],
                         "UNCERTAIN_OUTCOME")
        self.assertIsNone(terminal["attempt"].get("slot_result"))

        # An interruption at the supervisor's cleanup boundary must still
        # retain terminal uncertainty and must not be mistaken for removal.
        captured = []
        staged = []
        real_remove = runner_mod.process_lifecycle.destroy_run_scratch

        def remember_stage():
            stage, owned = create_stage()
            staged.append(stage)
            return stage, owned

        def interrupt_removal(path, managed):
            if path in staged:
                return real_remove(path, managed)
            self.assertTrue(managed)
            captured.append(path)
            raise KeyboardInterrupt("before managed scratch removal")

        create_stage = runner_mod.process_lifecycle.create_stage_dir
        held3 = []
        with patch.object(runner_mod.process_lifecycle, "create_stage_dir", side_effect=remember_stage), \
                patch.object(runner_mod.process_lifecycle, "destroy_run_scratch", side_effect=interrupt_removal):
            with self.assertRaisesRegex(KeyboardInterrupt, "before managed scratch removal"):
                run_bound(binding, [], timeout_s=5, attempt_store=held3.append,
                          request_id=_fresh_req("r33boundary"), attempt_id=_fresh_att("r33boundary"))
        try:
            self.assertEqual(len(held3), 2)
            terminal = held3[-1]
            self.assertEqual(terminal["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
            self.assertFalse(terminal["observation"]["producer"]["process_bound"])
            self.assertNotIn("slot_result", terminal["attempt"])
            self.assertEqual(len(captured), 1)
            self.assertTrue(captured[0].is_dir())
            self.assertEqual(len(staged), 1)
            self.assertFalse(staged[0].exists())
        finally:
            for path in (*captured, *staged):
                _shutil.rmtree(path, ignore_errors=True)

    def test_terminal_retention_failure_cannot_publish_accepted_observation(self):
        exe = self.dir / "valid.py"
        impl = _write_obj_fixture(exe, _valid_obj())
        binding = verify_binding(_manifest(impl), str(exe),
                                 operation_id="record",
                                 host_grants={"grant.exec.external_process"})
        # Positive: with a working store the observation is accepted.
        ok_held: list = []
        w_ok = run_bound(binding, [], timeout_s=5, cwd=str(self.dir),
                         attempt_store=ok_held.append,
                         request_id=_fresh_req("r34ok"),
                         attempt_id=_fresh_att("r34ok"))
        self.assertTrue(w_ok["evidence_accepted"])
        self.assertTrue(evidence_accepted(w_ok))
        self.assertGreaterEqual(len(ok_held), 2)
        # Negative: opened retention succeeds, terminal retention fails.
        held: list = []
        calls = {"n": 0}

        def _flaky(receipt):
            calls["n"] += 1
            if calls["n"] == 1:
                held.append(copy.deepcopy(receipt))
                return
            raise RuntimeError("terminal receipt retention unavailable")

        w = run_bound(binding, [], timeout_s=5, cwd=str(self.dir),
                      attempt_store=_flaky,
                      request_id=_fresh_req("r34fail"),
                      attempt_id=_fresh_att("r34fail"))
        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(held), 1,
                         "only the opened attempt was retained")
        self.assertFalse(w["evidence_accepted"],
                         "no accepted observation without terminal retention")
        self.assertFalse(evidence_accepted(w))
        self.assertEqual(w["receipt"]["attempt"]["control_outcome"],
                         "UNCERTAIN_OUTCOME")
        self.assertTrue(w["custody"].get("terminal_retention_failed") is True)
        # Matrix pins no accepted observation and UNCERTAIN_OUTCOME; the
        # terminal receipt was never advertised (only opened was retained).

    def test_identical_uncertain_retry_refuses_to_relaunch(self):
        # Positive contrast: completed request retry reuses the reply.
        counter = self.dir / "retry-launches.txt"
        counter.write_text("", encoding="utf-8")
        exe = self.dir / "valid.py"
        impl = _write_obj_fixture(exe, _valid_obj(), counter_file=str(counter))
        ref = _register(_manifest(impl), exe, f"installation:m2-retry-{_counter[0]}")
        payload = {"installation_ref": ref, "operation_id": "record",
                   "argv": [], "host_grants": ["grant.exec.external_process"]}
        req_ok = _fresh_req("r35ok")
        first = dispatch("behaviour.record",
                         {"request_id": req_ok, "payload": copy.deepcopy(payload)},
                         host=None, host_policy={"grants": ["grant.exec.external_process"]})
        self.assertTrue(first["ok"] and first["result"]["evidence_accepted"])
        self.assertEqual(_count_lines(counter), 1)
        again = dispatch("behaviour.record",
                         {"request_id": req_ok, "payload": copy.deepcopy(payload)},
                         host=None, host_policy={"grants": ["grant.exec.external_process"]})
        self.assertEqual(again, first)
        self.assertEqual(_count_lines(counter), 1)
        # Uncertain prior: identical retry stays UNCERTAIN_OUTCOME.
        _clear_store()
        counter.write_text("", encoding="utf-8")
        sleeper = self.dir / "sleeper.py"
        simpl = _write_sleeper(sleeper, counter_file=str(counter))
        sref = _register(_manifest(simpl), sleeper,
                         f"installation:m2-retry-unc-{_counter[0]}")
        spayload = {"installation_ref": sref, "operation_id": "record",
                    "argv": [], "host_grants": ["grant.exec.external_process"],
                    "timeout_s": 0.5}
        req_unc = _fresh_req("r35unc")
        reply = dispatch("behaviour.record",
                         {"request_id": req_unc, "payload": copy.deepcopy(spayload)},
                         host=None, host_policy={"grants": ["grant.exec.external_process"]})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["result"]["receipt"]["attempt"]["control_outcome"],
                         "UNCERTAIN_OUTCOME")
        self.assertEqual(_count_lines(counter), 1)
        with self.assertRaises(Exception) as cm:
            dispatch("behaviour.record",
                     {"request_id": req_unc, "payload": copy.deepcopy(spayload)},
                     host=None, host_policy={"grants": ["grant.exec.external_process"]})
        self.assertEqual(getattr(cm.exception, "code", None), "UNCERTAIN_OUTCOME")
        self.assertEqual(_count_lines(counter), 1,
                         "identical uncertain retry must not relaunch")

    def _uncertain_wrapper(self):
        sleeper = self.dir / f"sleeper-{_counter[0]}.py"
        impl = _write_sleeper(sleeper)
        binding = verify_binding(_manifest(impl), str(sleeper),
                                 operation_id="record",
                                 host_grants={"grant.exec.external_process"})
        w = run_bound(binding, [], timeout_s=0.5, cwd=str(self.dir),
                      request_id=_fresh_req("unc"),
                      attempt_id=_fresh_att("unc"))
        self.assertTrue(w["timed_out"])
        self.assertEqual(w["receipt"]["attempt"]["control_outcome"],
                         "UNCERTAIN_OUTCOME")
        self.assertTrue(w["custody"]["cleanup_observed"])
        return w

    def _clean_wrapper(self):
        exe = self.dir / f"clean-{_counter[0]}.py"
        impl = _write_obj_fixture(exe, _valid_obj())
        binding = verify_binding(_manifest(impl), str(exe),
                                 operation_id="record",
                                 host_grants={"grant.exec.external_process"})
        w = run_bound(binding, [], timeout_s=5, cwd=str(self.dir),
                      request_id=_fresh_req("clean"),
                      attempt_id=_fresh_att("clean"))
        self.assertTrue(w["evidence_accepted"])
        self.assertIsNone(w["receipt"]["attempt"].get("control_outcome"))
        self.assertTrue(w["custody"]["cleanup_observed"])
        return w

    def test_reconcile_rejects_clean_success(self):
        unc = self._uncertain_wrapper()
        rec = reconcile_run(unc, new_attempt_id=_fresh_att("r36ok"),
                            request_id=_fresh_req("r36ok"))
        self.assertEqual(rec["attempt"]["reconciles_attempt_id"],
                         unc["receipt"]["attempt"]["attempt_id"])
        self.assertEqual(rec["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
        self.assertFalse(evidence_accepted(rec))
        clean = self._clean_wrapper()
        with self.assertRaises(ValueError):
            reconcile_run(clean, new_attempt_id=_fresh_att("r36bad"),
                          request_id=_fresh_req("r36bad"))

    def test_reconcile_refuses_caller_asserted_cleanup(self):
        unc = self._uncertain_wrapper()
        # Positive: trusted identity-bound witness reconciles.
        rec = reconcile_run(unc, new_attempt_id=_fresh_att("r37ok"),
                            request_id=_fresh_req("r37ok"))
        self.assertEqual(rec["attempt"]["reconciles_attempt_id"],
                         unc["receipt"]["attempt"]["attempt_id"])
        # Same request and process, with a cleanup witness naming a foreign attempt.
        foreign = dict(unc["custody"], prior_attempt_id="att-r37-foreign")
        with self.assertRaisesRegex(ValueError, "prior attempt_id"):
            reconcile_run(unc, custody=foreign, new_attempt_id=_fresh_att("r37foreign"),
                          request_id=_fresh_req("r37foreign"))
        # Negative: public cleanup_observed=true without identity binding.
        asserted = {"receipt": copy.deepcopy(unc["receipt"]),
                    "custody": {"cleanup_observed": True,
                                "timed_out": True}}
        with self.assertRaises(ValueError):
            reconcile_run(asserted, new_attempt_id=_fresh_att("r37bad"),
                          request_id=_fresh_req("r37bad"))
        bare = copy.deepcopy(unc["receipt"])
        bare["custody"] = {"cleanup_observed": True}
        with self.assertRaises(ValueError):
            reconcile_run(bare, new_attempt_id=_fresh_att("r37bad2"),
                          request_id=_fresh_req("r37bad2"))

    def test_reconcile_requires_distinct_attempt_and_pins_actual_prior_digest(self):
        unc = self._uncertain_wrapper()
        unc["receipt"]["$comment"] = "caf\u00e9-\u00fc\u00f1\u00ed-0001"
        with self.assertRaisesRegex(ValueError, "exact prior receipt digest"):
            reconcile_run(unc, new_attempt_id=_fresh_att("r38tampered"),
                          request_id=_fresh_req("r38tampered"))
        expected = receipt_digest(unc["receipt"])
        canon = json.dumps(unc["receipt"], sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False,
                           allow_nan=False).encode("utf-8")
        self.assertEqual(expected, hashlib.sha256(canon).hexdigest())
        # The offline host fixture retains the Unicode-bearing record as new
        # immutable bytes and authorizes readback of that exact record.
        unc["custody"]["prior_receipt_sha256"] = expected
        prior_id = unc["receipt"]["attempt"]["attempt_id"]
        # Reusing the prior attempt_id as the new identity is refused.
        with self.assertRaises(ValueError):
            reconcile_run(unc, new_attempt_id=prior_id,
                          request_id=_fresh_req("r38bad"))
        # Prior receipt is untouched by the failed attempt.
        self.assertEqual(receipt_digest(unc["receipt"]), expected)
        self.assertEqual(unc["receipt"]["attempt"]["attempt_id"], prior_id)
        # Distinct identity succeeds and pins the actual prior digest.
        rec = reconcile_run(unc, new_attempt_id=_fresh_att("r38ok"),
                            request_id=_fresh_req("r38ok"))
        self.assertNotEqual(rec["attempt"]["attempt_id"], prior_id)
        self.assertEqual(rec["attempt"]["reconciles_attempt_id"], prior_id)
        self.assertEqual(rec["attempt"]["host_artifact_sha256"], expected)
        self.assertIsNone(rec["attempt"].get("slot_result"))

    def test_replay_receipt_rejects_unsupported_mode_and_incomplete_controls(self):
        sha = "a" * 64

        def _open_replay(replay, parent="ep-parent-r39"):
            return build_attempt_receipt(
                attempt_id=_fresh_att("r39"), request_id=_fresh_req("r39"),
                operation="replay", slot_role="replay", slot_id="fixture-slot",
                parent_episode_id=parent, replay=replay)

        with self.assertRaisesRegex(ValueError, "requires parent_episode_id"):
            _open_replay({"input_tape_sha256": sha, "determinism_mode": "seeded", "seed": 7},
                         parent=None)

        # Positive controls for each supported mode.
        self.assertIn("replay", _open_replay(
            {"input_tape_sha256": sha, "determinism_mode": "uncontrolled"}))
        self.assertIn("replay", _open_replay(
            {"input_tape_sha256": sha, "determinism_mode": "seeded", "seed": 7}))
        self.assertIn("replay", _open_replay(
            {"input_tape_sha256": sha, "determinism_mode": "distribution",
             "sample_count": 3}))
        self.assertIn("replay", _open_replay(
            {"input_tape_sha256": sha, "determinism_mode": "exact",
             "controls": {"clock": "controlled", "rng": "controlled",
                          "external_effects": "uncontrolled"}}))
        # Unsupported mode and incomplete controls are rejected outright.
        bad = [
            {"input_tape_sha256": sha, "determinism_mode": "bogus"},
            {"input_tape_sha256": sha, "determinism_mode": "seeded"},
            {"input_tape_sha256": sha, "determinism_mode": "distribution"},
            {"input_tape_sha256": sha, "determinism_mode": "exact"},
            {"input_tape_sha256": sha, "determinism_mode": "exact",
             "controls": {"clock": "controlled"}},
            {"input_tape_sha256": sha, "determinism_mode": "exact",
             "controls": {"clock": "uncontrolled", "rng": "uncontrolled",
                          "external_effects": "uncontrolled"}},
        ]
        for i, replay in enumerate(bad):
            with self.subTest(case=i):
                with self.assertRaises(ValueError):
                    _open_replay(replay)

    def test_decisive_comparison_requires_admitted_check(self):
        exe = self.dir / "cmp.py"
        impl = _write_obj_fixture(exe, _valid_obj())
        binding = verify_binding(_manifest(impl), str(exe), operation_id="compare",
                                 host_grants={"grant.exec.external_process"})
        request_id, attempt_id = _fresh_req("r40cmp"), _fresh_att("r40cmp")
        episode_ids = ("ep-r40-reference", "ep-r40-candidate")
        observed = run_bound(binding, [], timeout_s=5, cwd=str(self.dir),
                             request_id=request_id, attempt_id=attempt_id,
                             reference_episode_id=episode_ids[0], candidate_episode_id=episode_ids[1])
        self.assertTrue(observed["custody"]["offline_fixture"])
        self.assertTrue(observed["custody"]["artifact_digest_verified"])
        receipt, custody = observed["receipt"], observed["custody"]
        slot_result = receipt["attempt"]["slot_result"]
        self.assertEqual((slot_result["status"], slot_result["rc"]), ("OK", 0))
        self.assertEqual(receipt["verdict"]["result"], "abstain")
        self.assertFalse(evidence_accepted(observed))

        actual = json.loads(observed["stdout"])["result_payload"]
        self.assertEqual(hashlib.sha256(actual.encode()).hexdigest(), slot_result["sha256"])
        for expected, outcome in ((FIXTURE_RESULT_PAYLOAD, "match"), ("changed", "mismatch")):
            with self.subTest(outcome=outcome):
                comparison = behaviour_comparison(actual)
                comparison = replace(comparison, slot_result=slot_result,
                                     reference=replace(comparison.reference, behaviour=cases(expected)))
                comparison = replace(comparison, attempt_id=attempt_id)
                host, report = compare_fixture(comparison, request_id=request_id, episode_ids=episode_ids)
                verdict = report["verdict"]
                held = host.inspect(report["receipt_ref"], authorized=True)
                self.assertEqual(held.artifact_sha256, hashlib.sha256(host.artifacts[held.artifact_ref]).hexdigest())
                bound = dict(custody, comparison_report=host.comparison_witness)
                final = finalize_receipt(receipt, slot_result, custody=bound, verdict=verdict)
                self.assertEqual(final["verdict"], verdict)
                self.assertTrue(evidence_accepted({"receipt": final, "custody": bound}))
                foreign = copy.deepcopy(final)
                foreign["episode"]["candidate_episode_id"] = "ep-r40-foreign"
                self.assertFalse(evidence_accepted({"receipt": foreign, "custody": bound}))
                self.assertEqual(finalize_receipt(foreign, slot_result, custody=bound, verdict=verdict)
                                 ["verdict"]["abstain_reason"], "custody_unverified")
                self.assertEqual(verdict["result"], outcome)
                self.assertEqual(verdict["policy"], comparison.policy.pin())
                check, = verdict["checks"]
                self.assertEqual(check["outcome"], outcome)
                changed = [
                    dict(verdict, checks=[]),
                    dict(verdict, independent_qualification="true"),
                    dict(verdict, policy=dict(verdict["policy"], sha256="f" * 64)),
                ]
                self.assertEqual(
                    finalize_receipt(receipt, dict(slot_result, status="FAIL", rc=5), custody=bound,
                                     verdict=verdict)["verdict"]["result"],
                    "abstain",
                )
                if outcome == "mismatch":
                    self.assertEqual(check["first_divergence"]["case_id"], "press-1")
                    changed.append(dict(verdict, checks=[{k: v for k, v in check.items()
                                                         if k != "first_divergence"}]))
                else:
                    control, = verdict["negative_controls"]
                    changed.extend([
                        dict(verdict, negative_controls=[]),
                        dict(verdict, negative_controls=[dict(control, activated=False)]),
                        dict(verdict, negative_controls=[dict(control, outcome="survived")]),
                    ])
                for submitted, witness in [*((value, bound) for value in changed), (verdict, custody)]:
                    with self.subTest(verdict=submitted):
                        self.assertFalse(evidence_accepted({
                            "receipt": dict(receipt, verdict=submitted), "custody": witness,
                        }))
                        final = finalize_receipt(receipt, slot_result, custody=witness, verdict=submitted)
                        self.assertEqual(final["verdict"]["result"], "abstain")
                        self.assertFalse(evidence_accepted({"receipt": final, "custody": witness}))

    def test_fixture_cleanup_requires_recorded_identity_and_reap_witness(self):
        import subprocess

        script = self.dir / "cleanup-target.py"
        script.write_text(
            "#!/usr/bin/env python3\n# SPDX-License-Identifier: AGPL-3.0-only\n"
            "import select\nselect.select([0], [], [], 5)\n",
            encoding="utf-8",
        )
        script.chmod(0o700)
        proc = subprocess.Popen([str(script)], stdin=subprocess.PIPE)
        pid = proc.pid
        start = _proc_starttime(pid)
        try:
            self.assertGreater(pid, 1)
            self.assertIs(type(start), int)
            with patch.object(os, "kill") as signal, patch.object(proc, "terminate") as term, patch.object(
                proc, "kill",
            ) as kill:
                for identity in (None, True, start + 1):
                    with self.subTest(identity=identity):
                        _safe_kill(pid, identity)
                        self.assertFalse(_terminate_only(pid, proc, grace_s=0.2, bound_starttime=identity))
                        self.assertFalse(_kill_only(pid, proc, bound_starttime=identity))
                        self.assertIsNone(proc.poll())
                signal.assert_not_called()
                term.assert_not_called()
                kill.assert_not_called()
            self.assertTrue(_terminate_only(pid, proc, grace_s=2.0, bound_starttime=start))
            self.assertIsNotNone(proc.returncode)
            self.assertIsNone(_proc_starttime(pid))
        finally:
            proc.stdin.close()
            proc.wait(timeout=6)

    @unittest.skip("owner-pending: T06")
    def test_runtime_vision_requires_current_grant_and_witnessed_release(self):
        self.fail("Future vision requires a real grant and witnessed release, with wrong-owner, "
                  "expired, lease_mismatch and interrupted release contrasts")

    def test_slot_identity_strict(self):
        """ExpectedFailure: slot field must be validated against binding."""
        exe = self.dir / "valid.py"
        impl = _write_obj_fixture(exe, _valid_obj(slot="verify"))
        man = _manifest(impl)
        b = verify_binding(man, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        w = run_bound(b, [], timeout_s=5, cwd=str(self.dir), request_id=_fresh_req("r22-slot"), attempt_id=_fresh_att("r22-slot"))
        self.assertFalse(w["evidence_accepted"])
        self.assertIsNone(w["receipt"]["attempt"].get("slot_result"))

    def test_provider_digest_pin_strict(self):
        """ExpectedFailure: provider_digest must be checked against provider pin."""
        exe = self.dir / "valid.py"
        impl = _write_obj_fixture(exe, _valid_obj(provider_digest="f" * 64))
        man = _manifest(impl)
        b = verify_binding(man, str(exe), operation_id="record", host_grants={"grant.exec.external_process"})
        w = run_bound(b, [], timeout_s=5, cwd=str(self.dir), request_id=_fresh_req("r22-pdig"), attempt_id=_fresh_att("r22-pdig"))
        self.assertFalse(w["evidence_accepted"])

    def test_decisive_strict_schema(self):
        """ExpectedFailure: decisive mismatch requires check_id/kind/divergence."""
        exe = self.dir / "cmp.py"
        impl = _write_obj_fixture(exe, _valid_obj())
        binding = verify_binding(_manifest(impl), str(exe), operation_id="compare", host_grants={"grant.exec.external_process"})
        w = run_bound(binding, [], timeout_s=5, cwd=str(self.dir), request_id=_fresh_req("r40-strict"), attempt_id=_fresh_att("r40-strict"))
        # Malformed check (id only, no kind/divergence, independent_qualification true) must not qualify.
        import copy as _copy
        bad = _copy.deepcopy(w)
        bad["receipt"]["verdict"] = {"comparator_id": "host.compare", "comparator_version": "1", "result": "mismatch",
                                     "policy": _copy.deepcopy(w["receipt"]["verdict"]["policy"]),
                                     "checks": [{"id": "check-1", "outcome": "mismatch"}], "contrast_ids": [],
                                     "negative_controls": [], "oracle_independence": "independent",
                                     "independent_qualification": True}
        bad["custody"] = _copy.deepcopy(w["custody"])
        bad["custody"]["producer_binding_verified"] = True
        bad["receipt"]["observation"]["producer"]["process_bound"] = True
        bad["evidence_accepted"] = True
        self.assertFalse(evidence_accepted(bad))

    def test_cli_oversized_distinguishes_cap_from_syntax(self):
        """ExpectedFailure: oversized valid JSON must be rejected by cap, not syntax."""
        # Valid JSON padded to exceed cap must be SCHEMA_INVALID/14 even
        # though it is syntactically valid.
        import json as _json
        doc = _json.dumps({"request_id": _fresh_req("r26-pad"), "payload": {"installation_ref": "installation:x", "operation_id": "record"}})
        # Pad with spaces to exceed cap but stay valid JSON.
        padded = " " * 70000 + doc
        self.assertGreater(len(padded.encode()), 65536)
        # Current _call_cli will reject as invalid JSON vs cap; strict wire
        # path must distinguish. Marked expectedFailure until REA wire is wired.
        rc, calls, _ = self._call_cli(["mini", "behaviour.record", "--args", padded])
        self.assertEqual(rc, 14)
        self.assertEqual(calls, [])

if __name__ == "__main__":
    unittest.main()
