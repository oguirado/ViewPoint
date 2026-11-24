# SPDX-License-Identifier: AGPL-3.0-only
"""Runner fixes: one test per triage_runtime runner fix_list item."""
import copy
import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from slot_host.runtime.binding import sha256_of_file
from slot_host.runtime.runner import _kill_only, _proc_starttime, _terminate_only, run_bound
from slot_host.runtime.receipts import evidence_accepted


from slot_host.runtime.tests.binding_fixtures import (
    fixture_retention,
    run_fixture as _fixture_run_bound,
    FIXTURE_RESULT_PAYLOAD,
    FIXTURE_RESULT_SHA256,
    FixtureLaunch as _AllowLiveAuthority,
    verified_fixture_binding as verify_binding,
)

setUpModule = fixture_retention
def run_bound(*_args, **_kwargs):
    if "live_authority" not in _kwargs or _kwargs["live_authority"] is None:
        _kwargs = dict(_kwargs)
        _kwargs["live_authority"] = _AllowLiveAuthority()
    return _fixture_run_bound(*_args, **_kwargs)


_ADAPTER = "d" * 64
_PROVIDER_SHA = "a" * 64
_REQ_DIGEST = "b" * 64
_RES_DIGEST = "c" * 64
_GOOD_SHA = FIXTURE_RESULT_SHA256
_counter = [0]

def _fresh(tag):
    _counter[0] += 1
    return f"req-fix-{tag}-{_counter[0]:04d}", f"att-fix-{tag}-{_counter[0]:04d}"

def _manifest(impl_sha, *, perception_required="none"):
    if perception_required == "vision":
        perc_sources = [{"id": "px", "level": "vision", "producer": "host_vision_engine", "outside_capsule": True, "vision_pool": {"required": True, "owner": "ViewPoint_Tools", "ticket_field": "vision_pool_ticket"}}]
    else:
        perc_sources = [{"id": "tool-evidence", "level": perception_required, "producer": "tool_stdio", "outside_capsule": True}]
    def _op(oid, role):
        return {"id": oid, "role": role,
                "input_ref": f"urn:rea.slot_request:v2#slot={role}",
                "output_ref": f"urn:rea.slot_result:v2#slot={role}",
                "binding": {"kind": "cli_subcommand", "argv": [oid], "typed_args": []},
                "required_effects": ["process.spawn:child"],
                "required_grants": ["grant.exec.external_process"],
                "limits": {"timeout_s": 5, "max_output_bytes": 4096},
                "retry_semantics": {"mode": "at_most_once", "on_interrupt": "UNCERTAIN_OUTCOME"}}
    return {"schema": "rea.slot_manifest/v2", "slot_id": "fixture-slot", "status": "registered",
            "upstream": {"name": "fixture", "source_url": "https://example.com/fixture", "version": "1.0.0",
                         "licence_id": "MIT", "licence_evidence_refs": ["fixture: licence"],
                         "component_notices": [], "user_installed": True},
            "provider_ref": {"schema": "rea.provider/v2", "provider_id": "fixture-slot",
                             "provider_revision": "1.0.0", "provider_sha256": _PROVIDER_SHA,
                             "implementation_sha256": impl_sha},
            "contract_refs": [
                {"role": "replay", "request_schema_ref": "urn:rea.slot_request:v2", "request_digest": _REQ_DIGEST,
                 "result_schema_ref": "urn:rea.slot_result:v2", "result_digest": _RES_DIGEST, "execution_profile": "external_process"},
                {"role": "verify", "request_schema_ref": "urn:rea.slot_request:v2", "request_digest": _REQ_DIGEST,
                 "result_schema_ref": "urn:rea.slot_result:v2", "result_digest": _RES_DIGEST, "execution_profile": "external_process"}],
            "transport": {"kind": "cli_argv", "driver_location": "outside_capsule", "executable_binding": "user_selected",
                          "argv_prefix": [], "framing": "none", "cwd_policy": "run_scratch", "env_allowlist": ["PATH"]},
            "operations": [_op("record", "replay"), _op("replay", "replay"), _op("compare", "verify")],
            "capabilities": {"target_kinds": ["fixture_target"], "platforms": ["linux"], "prerequisite_probes": [],
                             "optional_features": [], "adapter_identity": {"adapter_id": "fixture.v1", "adapter_sha256": _ADAPTER, "provenance": "host_authored"}},
            "perception": {"required_level": perception_required, "optional_levels": [], "observation_sources": perc_sources},
            "record_replay": {"input_codec": "json", "output_codec": "json", "observable_channels": ["error"],
                              "state_domain": "per_case", "effects": ["process.spawn:child"],
                              "determinism_modes": ["recorded_inputs"], "freshness_inputs": ["fixture"]},
            "comparison": {"comparator_id": "fixture.compare", "version": "1", "policy_ref": "policy:fixture@1",
                           "coverage_requirements": {"channel": "error"}, "tolerance_units": "none",
                           "waiver_policy": {"source": "host_policy", "binds": ["input", "actual", "reason", "scope", "policy_version"], "stale": "fail"}},
            "evidence": {"shared_schema_ref": "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2", "required_rea_fields": ["episode", "observation", "slot"],
                         "artifact_roles": ["evidence_bundle"], "retention": "evidence_store_attempt", "visibility": "redacted_projection"},
            "conformance": {"fixture_refs": ["fixture:1"], "negative_control_refs": ["control:1"], "supported_entry_points": ["cli", "mcp", "json"],
                            "required_checks": ["admit-and-project"]},
            "discovery": {"summary": "fixture", "search_terms": ["fixture"],
                          "operation_descriptions": {"record": "record", "replay": "replay", "compare": "compare"}, "examples": []}}

def _write_fixture(path: Path, body: bytes) -> str:
    path.write_bytes(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return sha256_of_file(path)

def _valid_obj(**overrides):
    obj = {"schema": "rea.slot_result/v1", "status": "OK", "rc": 0, "sha256": _GOOD_SHA, "result_payload": FIXTURE_RESULT_PAYLOAD}
    obj.update(overrides)
    return obj

def _write_obj(path: Path, obj: dict) -> str:
    inner = json.dumps(obj)
    lines = [b"#!/usr/bin/env python3\n", b"# SPDX-License-Identifier: AGPL-3.0-only\n",
             b"import json, sys\n"]
    lines.append(b"sys.stdout.write(json.dumps(" + inner.encode() + b")+'\\n')\n")
    lines.append(b"sys.stdout.flush()\n")
    return _write_fixture(path, b"".join(lines))

def _write_raw(path: Path, stdout_text: str, *, stderr_text: str = "") -> str:
    lines = [b"#!/usr/bin/env python3\n", b"# SPDX-License-Identifier: AGPL-3.0-only\n",
             b"import sys\n"]
    if stdout_text:
        lines.append(b"sys.stdout.write(" + repr(stdout_text).encode() + b")\n")
    if stderr_text:
        lines.append(b"sys.stderr.write(" + repr(stderr_text).encode() + b")\n")
    lines.append(b"sys.stdout.flush()\n")
    return _write_fixture(path, b"".join(lines))

class _FakePool:
    def __init__(self, lease=None, acquire_returns=None):
        self._lease = lease
        self._returns = acquire_returns
        self.acquires = 0
        self.releases = 0
    def acquire(self, front, pid, timeout=60, ttl=300):
        self.acquires += 1
        if self._returns is not None:
            return self._returns
        return dict(self._lease)
    def release(self, front, pid, lease_id):
        self.releases += 1
        return True

class RunnerFixTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _binding(self, exe: Path, impl: str, *, operation_id="record", perception_required="none", ceiling="vision"):
        return verify_binding(_manifest(impl, perception_required=perception_required), str(exe),
                              operation_id=operation_id, host_grants={"grant.exec.external_process"},
                              capsule_perception_ceiling=ceiling)

    def test_snapshot_outside_scratch_decoy_ignored(self):
        exe = self.dir / "fixture.py"
        impl = _write_obj(exe, _valid_obj())
        binding = self._binding(exe, impl)
        scratch = self.dir / "scratch"
        scratch.mkdir()
        # Same-named decoy inside scratch that would mark if executed.
        decoy_body = (b"#!/usr/bin/env python3\nimport pathlib; pathlib.Path('decoy-executed').write_text('x')\n")
        decoy = scratch / "fixture.py"
        _write_fixture(decoy, decoy_body)
        req, att = _fresh("snap")
        w = run_bound(binding, [], timeout_s=5, cwd=str(scratch), request_id=req, attempt_id=att, shared_receipt_ref="shared:fix")
        self.assertTrue(w["evidence_accepted"])
        self.assertFalse((scratch / "decoy-executed").exists(), "scratch decoy must not execute (launch target must be outside scratch)")
        self.assertFalse((scratch / "_rea-snapshot").exists(), "snapshot must live outside run_scratch")

    def test_strict_stdout_rejects_stderr_duplicate_nonfinite_boolrc(self):
        cases = [
            ("stderr-only", "", json.dumps(_valid_obj())),
            ("dup-key", '{"schema":"rea.slot_result/v1","schema":"rea.slot_result/v1","status":"OK","rc":0,"sha256":"' + _GOOD_SHA + '"}', ""),
            ("nonfinite", '{"schema":"rea.slot_result/v1","status":"OK","rc":0,"sha256":"' + _GOOD_SHA + '","extra":Infinity}', ""),
            ("bool-rc", '{"schema":"rea.slot_result/v1","status":"OK","rc":false,"sha256":"' + _GOOD_SHA + '"}', ""),
            ("two-lines", json.dumps(_valid_obj()) + "\n" + json.dumps(_valid_obj()) + "\n", ""),
            ("extra-noise", "noise-not-json\n" + json.dumps(_valid_obj()) + "\n", ""),
        ]
        for name, stdout, stderr in cases:
            with self.subTest(case=name):
                exe = self.dir / f"strict-{name}.py"
                impl = _write_raw(exe, stdout + ("\n" if stdout else ""), stderr_text=stderr)
                binding = self._binding(exe, impl)
                req, att = _fresh(f"strict-{name}"[:20].replace("-",""))
                w = run_bound(binding, [], timeout_s=5, cwd=str(self.dir), request_id=req, attempt_id=att, shared_receipt_ref="shared:fix")
                self.assertFalse(w["evidence_accepted"], f"{name} must not accept")
                self.assertIsNone(w["receipt"]["attempt"].get("slot_result"), f"{name} must leave no slot_result")

    def test_tape_digest_independent_of_result_digest(self):
        exe = self.dir / "indep.py"
        other = hashlib.sha256(b"result independent of tape").hexdigest()
        impl = _write_obj(exe, _valid_obj(sha256=other, result_payload="result independent of tape"))
        binding = self._binding(exe, impl)
        tape = hashlib.sha256(b"tape-input-xyz").hexdigest()
        self.assertNotEqual(tape, other)
        req, att = _fresh("indep")
        w = run_bound(binding, [], timeout_s=5, cwd=str(self.dir), request_id=req, attempt_id=att, tape_sha256=tape, shared_receipt_ref="shared:fix")
        self.assertTrue(w["evidence_accepted"], "distinct tape/result digests must still accept")
        self.assertEqual(w["receipt"]["attempt"]["slot_result"]["sha256"], other)
        self.assertEqual(w["custody"]["input_tape_sha256"], tape)
        self.assertEqual(w["shared_records"]["terminal"]["artifact"]["sha256"], other)

    def test_identity_null_coercion_and_slot_rejected(self):
        cases = [
            ("null-rev", _valid_obj(provider_revision=None)),
            ("int-rev", _valid_obj(provider_revision=100)),
            ("slot-forge", _valid_obj(slot="verify")),
            ("pd-forge", _valid_obj(provider_digest="e" + "f" * 63)),
            ("pid-forge", _valid_obj(provider_id="other-provider")),
        ]
        for name, obj in cases:
            with self.subTest(case=name):
                exe = self.dir / f"id-{name}.py"
                impl = _write_obj(exe, obj)
                binding = self._binding(exe, impl)
                req, att = _fresh(f"id{name}"[:20].replace("-",""))
                w = run_bound(binding, [], timeout_s=5, cwd=str(self.dir), request_id=req, attempt_id=att, shared_receipt_ref="shared:fix")
                self.assertFalse(w["evidence_accepted"], f"{name} must not accept")
                self.assertIsNone(w["receipt"]["attempt"].get("slot_result"), f"{name} must leave no slot_result")

    def test_terminate_requires_recorded_starttime(self):
        import subprocess as sp, time as _t
        script = self.dir / "sleeper.py"
        _write_fixture(script, b"#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n")
        proc = sp.Popen([str(script)])
        try:
            _t.sleep(0.3)
            self.assertIsNone(proc.poll())
            _terminate_only(proc.pid, proc, grace_s=0.2, bound_starttime=None)
            self.assertIsNone(proc.poll(), "no recorded starttime must not signal")
            _kill_only(proc.pid, proc, bound_starttime=None)
            self.assertIsNone(proc.poll(), "no recorded starttime must not kill")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_interruption_and_terminal_downgrade(self):
        import slot_host.runtime.executable_custody as custody_mod
        exe = self.dir / "valid.py"
        impl = _write_obj(exe, _valid_obj())
        binding = self._binding(exe, impl)
        # Snapshot custody is owned by executable_custody (U08): patch
        # its stage seam (same intent as the former shutil.copyfile seam).
        orig_stage = custody_mod.stage_verified_executable
        def _boom(*args, **kwargs):
            raise KeyboardInterrupt("snapshot interrupted")
        held = []
        custody_mod.stage_verified_executable = _boom
        try:
            with self.assertRaises(KeyboardInterrupt):
                run_bound(binding, [], timeout_s=5, cwd=None,
                          request_id="req-fix-interrupt-0001", attempt_id="att-fix-interrupt-0001",
                          attempt_store=held.append, shared_receipt_ref="shared:fix")
        finally:
            custody_mod.stage_verified_executable = orig_stage
        terminal = held[-1] if held else {}
        self.assertEqual(terminal.get("attempt", {}).get("control_outcome"), "UNCERTAIN_OUTCOME",
                         "snapshot interrupt must retain UNCERTAIN_OUTCOME before propagation")
        # Terminal retention failure must downgrade process binding and drop the result.
        calls = {"n": 0}
        def _flaky(receipt):
            calls["n"] += 1
            if calls["n"] == 1:
                return
            raise RuntimeError("terminal retention unavailable")
        req, att = _fresh("term")
        w = run_bound(binding, [], timeout_s=5, cwd=str(self.dir), attempt_store=_flaky,
                      request_id=req, attempt_id=att, shared_receipt_ref="shared:fix")
        self.assertFalse(w["evidence_accepted"])
        self.assertEqual(w["receipt"]["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
        self.assertIsNone(w["receipt"]["attempt"].get("slot_result"), "terminal failure must drop slot_result")
        self.assertFalse(w["receipt"]["observation"]["producer"]["process_bound"], "terminal failure must clear process_bound")

    def test_vision_grant_validation(self):
        from unittest.mock import patch
        from slot_host.runtime import runner
        from slot_host.runtime.binding import BindingRejected

        exe = self.dir / "vision.py"
        impl = _write_obj(exe, _valid_obj())
        binding = self._binding(exe, impl, perception_required="vision")
        for pool in (_FakePool(acquire_returns="lease-plain-string"),
                     _FakePool(lease={"expires_at_monotonic": True}),
                     _FakePool(lease={"front": "WrongFront"})):
            req, att = _fresh("vision")
            with (patch.object(runner.subprocess, "Popen", wraps=runner.subprocess.Popen) as spawn,
                  self.assertRaises(BindingRejected) as refused):
                run_bound(binding, [], cwd=self.dir, vision_pool=pool,
                          perception_level="vision", request_id=req, attempt_id=att)
            self.assertEqual(refused.exception.reason, "availability_parked")
            self.assertEqual(pool.acquires, 0)
            spawn.assert_not_called()

    def test_replay_profile_threaded_through_runner(self):
        exe = self.dir / "replay.py"
        impl = _write_obj(exe, _valid_obj())
        binding = self._binding(exe, impl, operation_id="replay")
        tape = "a" * 64
        parent = "ep-fix-replay-parent"
        replay = {"input_tape_sha256": tape, "determinism_mode": "seeded", "seed": 7}
        retained = {(parent, tape): {"parent_episode_id": parent, "input_tape_sha256": tape}}
        with self.assertRaisesRegex(ValueError, "requires parent_episode_id"):
            run_bound(binding, [], cwd=str(self.dir), replay=replay)
        req, att = _fresh("replay")
        w = run_bound(binding, [], timeout_s=5, cwd=str(self.dir), request_id=req, attempt_id=att,
                      parent_episode_id=parent, tape_resolver=lambda episode, sha: retained.get((episode, sha)),
                      replay=replay, shared_receipt_ref="shared:fix")
        self.assertTrue(w["evidence_accepted"])
        self.assertEqual(w["receipt"].get("replay", {}).get("determinism_mode"), "seeded")
        self.assertEqual(w["receipt"].get("replay", {}).get("seed"), 7)
        self.assertEqual(w["receipt"].get("replay", {}).get("input_tape_sha256"), tape)

if __name__ == "__main__":
    unittest.main()
