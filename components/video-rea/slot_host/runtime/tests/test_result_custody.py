# SPDX-License-Identifier: AGPL-3.0-only
"""Result custody and process lifecycle at the offline runner boundary."""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from slot_host.runtime import process_lifecycle, runner
from slot_host.runtime.binding import BindingRejected, sha256_of_file
from slot_host.runtime.live_park import _FixtureLiveAuthority
from slot_host.runtime.receipts import _shared_adapter, finalize_receipt
from slot_host.runtime.runner import run_bound
from slot_host.runtime.tools import _run_adapter
from slot_host.runtime.tests.binding_fixtures import (
    fixture_policy,
    fixture_retention,
    FIXTURE_RESULT_PAYLOAD,
    FIXTURE_RESULT_SHA256,
    verified_fixture_binding as verify_binding,
)

setUpModule = fixture_retention


class _AllowLiveAuthority:
    """Test-only authority that permits live launch (bypasses LIVE_EXECUTION_PARKED)."""

    def verify(self, *, binding, operation_id: str, request_id: str, attempt_id: str) -> bool:
        return True


_ADAPTER = "d" * 64
_PROVIDER_SHA = "a" * 64
_REQ_DIGEST = "b" * 64
_RES_DIGEST = "c" * 64
_GOOD_SHA = FIXTURE_RESULT_SHA256
_counter = [0]


def _fresh(tag):
    _counter[0] += 1
    return f"req-res-{tag}-{_counter[0]:04d}", f"att-res-{tag}-{_counter[0]:04d}"


def _manifest(impl_sha, *, op_limits=None):
    def _op(oid, role):
        return {"id": oid, "role": role,
                "input_ref": f"urn:rea.slot_request:v2#slot={role}",
                "output_ref": f"urn:rea.slot_result:v2#slot={role}",
                "binding": {"kind": "cli_subcommand", "argv": [oid], "typed_args": []},
                "required_effects": ["process.spawn:child"],
                "required_grants": ["grant.exec.external_process"],
                "limits": dict(op_limits) if op_limits is not None else {"timeout_s": 5, "max_output_bytes": 4096},
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
            "perception": {"required_level": "none", "optional_levels": [],
                           "observation_sources": [{"id": "tool-evidence", "level": "none", "producer": "tool_stdio", "outside_capsule": True}]},
            "record_replay": {"input_codec": "json", "output_codec": "json", "observable_channels": ["error"],
                              "state_domain": "per_case", "effects": ["process.spawn:child"],
                              "determinism_modes": ["recorded_inputs"], "freshness_inputs": ["fixture"]},
            "comparison": {"comparator_id": "fixture.compare", "version": "1", "policy_ref": "policy:fixture@1",
                           "coverage_requirements": {"channel": "error"}, "tolerance_units": "none",
                           "waiver_policy": {"source": "host_policy", "binds": ["input", "actual", "reason", "scope", "policy_version"], "stale": "fail"}},
            "evidence": {"shared_schema_ref": "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2", "required_rea_fields": ["episode", "observation", "slot"],
                         "artifact_roles": ["evidence_bundle"], "retention": "evidence_store_attempt", "visibility": "redacted_projection"},
            "conformance": {"fixture_refs": ["fixture:1"], "negative_control_refs": ["control:1"], "supported_entry_points": ["cli", "mcp", "json"],
                             "required_checks": ["admit-and-project", "outside-record-replay-compare",
                                                 "perception-and-pool", "transport-parity-and-retry"]},
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


def _write_obj(path: Path, obj: dict, *, counter: Path | None = None) -> str:
    inner = json.dumps(obj)
    lines = [b"#!/usr/bin/env python3\n", b"# SPDX-License-Identifier: AGPL-3.0-only\n",
             b"import json, sys\n"]
    if counter is not None:
        lines.append(b"open(%r, 'a').write('x\\n')\n" % str(counter))
    lines.append(b"sys.stdout.write(" + repr(inner).encode() + b"+'\\n')\n")
    lines.append(b"sys.stdout.flush()\n")
    return _write_fixture(path, b"".join(lines))


def _write_raw(path: Path, stdout_text: str, *, stderr_text: str = "", counter: Path | None = None) -> str:
    lines = [b"#!/usr/bin/env python3\n", b"# SPDX-License-Identifier: AGPL-3.0-only\n",
             b"import sys\n"]
    if counter is not None:
        lines.append(b"open(%r, 'a').write('x\\n')\n" % str(counter))
    if stdout_text:
        lines.append(b"sys.stdout.write(" + repr(stdout_text).encode() + b")\n")
    if stderr_text:
        lines.append(b"sys.stderr.write(" + repr(stderr_text).encode() + b")\n")
    lines.append(b"sys.stdout.flush()\n")
    return _write_fixture(path, b"".join(lines))


class ResultCustodyUnitTest(unittest.TestCase):
    def test_decode_output_bounds_malformed_utf8_at_cap(self):
        from slot_host.runtime import result_custody

        data = b"\xff\xfe" + b"a" * 100
        text, truncated = result_custody.decode_output(data, 10)
        self.assertTrue(truncated)
        self.assertLessEqual(len(text.encode()), 10)
        text2, trunc2 = result_custody.decode_output(b"\xff" * 64, 64)
        self.assertTrue(trunc2)
        self.assertLessEqual(len(text2.encode()), 64)

    def test_placeholder_shared_refs_never_authorize_receipt(self):
        for reference in ("shared:fix", "shared:fix-attempt"):
            with self.subTest(reference=reference):
                adapter = _shared_adapter(
                    "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2", shared_receipt_ref=reference,
                )
                self.assertFalse(adapter["live_use_permitted"])
                self.assertNotIn("shared_receipt_ref", adapter)

    def test_resolve_withheld_witness_never_accepts(self):
        from slot_host.runtime import result_custody

        stdout = (json.dumps(_valid_obj()) + "\n").encode()
        pid, st, bound = result_custody.resolve_producer_origin(stdout, b"", 999, 123, None)
        self.assertEqual(pid, 0)
        self.assertIsNone(st)
        self.assertFalse(bound)

    def test_resolve_foreign_witness_downgrades(self):
        from slot_host.runtime import result_custody

        stdout = (json.dumps(_valid_obj()) + "\n").encode()
        foreign = {"process_id": 999, "starttime": 123}
        pid, st, bound = result_custody.resolve_producer_origin(stdout, b"", 555, 999, foreign)
        self.assertEqual(pid, 999)
        self.assertFalse(bound)

    def test_build_slot_result_rejects_overflow_and_mismatch(self):
        from slot_host.runtime import result_custody

        good = _valid_obj()
        self.assertIsNotNone(result_custody.build_slot_result(good, 0, timed_out=False, overflow=False, interrupted=False, mismatch=False))
        self.assertIsNone(result_custody.build_slot_result(good, 0, timed_out=False, overflow=True, interrupted=False, mismatch=False))
        self.assertIsNone(result_custody.build_slot_result(good, 0, timed_out=False, overflow=False, interrupted=False, mismatch=True))
        # OS rc disagreement yields no result (never synthesized).
        self.assertIsNone(result_custody.build_slot_result(good, 2, timed_out=False, overflow=False, interrupted=False, mismatch=False))

    def test_exchange_identity_exact_types(self):
        from slot_host.runtime import result_custody

        base = {"request_id": "req-x", "provider_id": "p", "provider_revision": "1",
                "execution_profile": "external_process", "implementation_sha256": "a" * 64,
                "role": "replay", "rc": 0}
        self.assertFalse(result_custody.exchange_identity_mismatch({"request_id": "req-x"}, **base))
        self.assertTrue(result_custody.exchange_identity_mismatch({"request_id": None}, **base))
        self.assertTrue(result_custody.exchange_identity_mismatch({"request_id": "req-y"}, **base))
        self.assertTrue(result_custody.exchange_identity_mismatch({"provider_digest": "e" + "f" * 63}, **base))

    def test_collect_output_combined_cap_unit(self):
        from slot_host.runtime import process_lifecycle

        script_lines = [b"#!/usr/bin/env python3\n", b"import sys\n",
                        b"sys.stdout.write('x'*800)\n", b"sys.stdout.flush()\n",
                        b"sys.stderr.write('y'*800)\n", b"sys.stderr.flush()\n",
                        b"sys.stdout.write('z'*800)\n", b"sys.stdout.flush()\n"]
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "loud.py"
            script.write_bytes(b"".join(script_lines))
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            proc = subprocess.Popen([str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False)
            identity = process_lifecycle.proc_starttime(proc.pid)
            try:
                deadline = process_lifecycle.effective_deadline(5.0)
                with patch.object(os, "read", wraps=os.read) as read:
                    out, err, timed_out, overflow = process_lifecycle.collect_output(proc, deadline, 1000)
                proc.wait(timeout=5)
            finally:
                process_lifecycle.close_pipes(proc)
                if proc.poll() is None:
                    process_lifecycle.safe_kill(proc.pid, identity)
                    proc.wait(timeout=5)
            self.assertTrue(overflow)
            self.assertFalse(timed_out)
            self.assertLessEqual(len(out) + len(err), 1000)
            self.assertLessEqual(max(call.args[1] for call in read.call_args_list), 1001)


class ResultCustodyIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _binding(self, exe: Path, impl: str, *, op_limits=None):
        return verify_binding(_manifest(impl, op_limits=op_limits), str(exe),
                              operation_id="record", host_grants={"grant.exec.external_process"})

    def _run(self, binding, *, registered=True, **kwargs):
        req, att = _fresh("res")
        authority = (
            _FixtureLiveAuthority(binding, request_id=req, attempt_id=att)
            if registered else _AllowLiveAuthority()
        )
        return run_bound(
            binding, [], request_id=req, attempt_id=att, live_authority=authority,
            **({"timeout_s": 5, "cwd": str(self.dir)} | kwargs),
        )

    def test_unregistered_producer_cannot_use_omitted_or_self_supplied_witness(self):
        exe = self.dir / "unregistered.py"
        marker = self.dir / "launches"
        impl = _write_obj(exe, _valid_obj(), counter=marker)
        binding = self._binding(exe, impl)
        trusted = self._run(binding)
        witness = {"host_result_sha256": trusted["custody"]["host_result_sha256"]}
        collect = runner._collect_output

        def identify(proc, *args):
            witness.update(process_id=proc.pid, starttime=process_lifecycle.proc_starttime(proc.pid))
            return collect(proc, *args)

        with patch.object(runner, "_collect_output", side_effect=identify):
            omitted = self._run(binding, registered=False, shared_receipt_ref="shared:fix")
            asserted = self._run(binding, registered=False, producer_witness=witness,
                                 shared_receipt_ref="shared:fix-attempt")
        self.assertEqual(marker.read_text().splitlines(), ["x", "x", "x"])
        for wrapper in (omitted, asserted):
            self.assertEqual(wrapper["stdout"], trusted["stdout"])
            self.assertFalse(wrapper["evidence_accepted"])
            self.assertFalse(wrapper["receipt"]["observation"]["producer"]["process_bound"])
            self.assertNotIn("slot_result", wrapper["receipt"]["attempt"])
            self.assertNotIn("result_sha256", wrapper["custody"])
            self.assertEqual(wrapper["custody"]["producer_custody_reason"], "owner-pending: T03")
            adapter = wrapper["receipt"]["shared_receipt_adapter"]
            self.assertFalse(adapter["live_use_permitted"])
            self.assertNotIn("shared_receipt_ref", adapter)
        self.assertTrue(trusted["evidence_accepted"])

    @unittest.skip("owner-pending: T03")
    def test_owner_authenticated_producer_attributes_exact_result_bytes(self):
        """Requires the owner-authenticated producer and retained artifact."""

    def test_producer_witness_binds_the_exact_stdout_bytes(self):
        exe = self.dir / "valid.py"
        impl = _write_obj(exe, _valid_obj())
        binding = self._binding(exe, impl)
        expected = hashlib.sha256((json.dumps(_valid_obj()) + "\n").encode()).hexdigest()
        collect = runner._collect_output

        def run(witness_digest):
            witness = {"host_result_sha256": witness_digest}

            def observe(proc, *args):
                witness.update(process_id=proc.pid, starttime=runner._proc_starttime(proc.pid))
                return collect(proc, *args)

            with patch.object(runner, "_collect_output", side_effect=observe):
                return self._run(binding, producer_witness=witness)

        accepted = run(expected)
        refused = run(hashlib.sha256(b"different result").hexdigest())
        self.assertTrue(accepted["evidence_accepted"])
        self.assertEqual(accepted["custody"]["host_result_sha256"], expected)
        self.assertEqual(accepted["stdout"], refused["stdout"])
        self.assertFalse(refused["evidence_accepted"])
        self.assertNotIn("slot_result", refused["receipt"]["attempt"])

    def test_foreign_result_producer_rejected_without_pid_self_report(self):
        """C10: same valid bytes with a foreign witness yields no acceptance."""
        ctr = self.dir / "ctr-foreign.txt"
        exe = self.dir / "foreign.py"
        impl = _write_obj(exe, _valid_obj(), counter=ctr)
        binding = self._binding(exe, impl)
        baseline = self._run(binding)
        self.assertTrue(baseline["evidence_accepted"])
        foreign = {"process_id": 424242, "starttime": 1,
                   "host_result_sha256": baseline["custody"]["host_result_sha256"]}
        w = self._run(binding, producer_witness=foreign)
        self.assertEqual(w["stdout"], baseline["stdout"])
        self.assertEqual(ctr.read_text().splitlines(), ["x", "x"], "both fixtures must have launched")
        self.assertFalse(w["evidence_accepted"])
        self.assertFalse(w["receipt"]["observation"]["producer"]["process_bound"])
        self.assertFalse(w["custody"]["producer_binding_verified"])
        self.assertNotIn("slot_result", w["receipt"]["attempt"])

    def test_missing_producer_witness_cannot_fall_back_to_spawned_pid(self):
        ctr = self.dir / "ctr-withheld.txt"
        exe = self.dir / "withheld.py"
        impl = _write_obj(exe, _valid_obj(), counter=ctr)
        binding = self._binding(exe, impl)
        w = self._run(binding, producer_witness=None)
        self.assertEqual(ctr.read_text().splitlines(), ["x"])
        self.assertFalse(w["evidence_accepted"])
        self.assertFalse(w["receipt"]["observation"]["producer"]["process_bound"])
        self.assertFalse(w["custody"]["producer_binding_verified"])
        self.assertNotIn("slot_result", w["receipt"]["attempt"])

    def test_combined_output_overflow_is_bounded_and_is_not_provider_status(self):
        """C21: each stream below cap but combined above -> overflow, bounded, no 6."""
        script = self.dir / "mixed.py"
        body = (b"#!/usr/bin/env python3\n# SPDX-License-Identifier: AGPL-3.0-only\n"
                b"import sys; sys.stdout.write('x'*1500); sys.stdout.flush(); "
                b"sys.stderr.write('y'*1500); sys.stderr.flush(); "
                b"sys.stdout.write('z'*1500); sys.stdout.flush()\n")
        impl = _write_fixture(script, body)
        binding = self._binding(script, impl)
        req, att = _fresh("overflow")
        w = run_bound(binding, [], timeout_s=5, max_output_bytes=4096, cwd=str(self.dir),
                      request_id=req, attempt_id=att,
                      live_authority=_AllowLiveAuthority())
        self.assertTrue(w["custody"]["overflow"])
        self.assertTrue(w["stdout_truncated"])
        self.assertTrue(w["custody"]["stdout_truncated"])
        self.assertFalse(w["evidence_accepted"])
        self.assertIsNone(w["receipt"]["attempt"].get("slot_result"),
                          "overflow must not synthesize a provider slot_result (never BUDGET_EXCEEDED/6)")
        retained = len(w.get("stdout", "").encode()) + len(w.get("stderr", "").encode())
        self.assertLessEqual(retained, 4096)
        captured = hashlib.sha256(w["stdout"].encode()).hexdigest()
        self.assertNotEqual(w["shared_records"]["terminal"]["artifact"]["sha256"], captured)

    def test_malformed_utf8_at_cap_bounded(self):
        """Malformed UTF-8 in capture never crashes and stays bounded."""
        script = self.dir / "badutf.py"
        body = (b"#!/usr/bin/env python3\n# SPDX-License-Identifier: AGPL-3.0-only\n"
                 b"import os; os.write(1, b'\\xff'*2048); os.write(2, b'\\xfe'*2048)\n")
        impl = _write_fixture(script, body)
        binding = self._binding(script, impl)
        w = self._run(binding)
        self.assertFalse(w["evidence_accepted"])
        self.assertIsNone(w["receipt"]["attempt"].get("slot_result"))
        retained = len(w.get("stdout", "").encode()) + len(w.get("stderr", "").encode())
        self.assertFalse(w["custody"]["overflow"], "raw output is exactly at the cap")
        self.assertTrue(w["stdout_truncated"])
        self.assertLessEqual(retained, 4096)

    def test_split_utf8_and_stderr_expansion_share_the_retained_byte_cap(self):
        exe = self.dir / "split-utf8.py"
        body = (
            b"#!/usr/bin/env python3\nimport os\n"
            b"os.write(1, b'a' * 4093); os.write(2, b'b\\xff\\xe2')\n"
        )
        impl = _write_fixture(exe, body)
        wrapper = self._run(self._binding(exe, impl))
        self.assertFalse(wrapper["custody"]["overflow"])
        self.assertEqual(wrapper["stdout"], "a" * 4093)
        self.assertEqual(wrapper["stderr"], "b")
        self.assertLessEqual(len((wrapper["stdout"] + wrapper["stderr"]).encode()), 4096)
        self.assertTrue(wrapper["stdout_truncated"])
        self.assertTrue(wrapper["custody"]["stdout_truncated"])
        self.assertNotIn("slot_result", wrapper["receipt"]["attempt"])
        self.assertEqual(runner._decode_output(b"a" * 4095 + "€".encode(), 4096),
                         ("a" * 4095, True))

    def test_pinned_provider_exchange_rejects_each_identity_substitution(self):
        identities = {
            "provider_id": "fixture-slot",
            "provider_revision": "1.0.0",
            "provider_sha256": _PROVIDER_SHA,
            "execution_profile": "external_process",
            "role": "replay",
            "record_schema": "rea.slot_result/v1",
            "request_id": "req-identity-fixture",
        }
        exe = self.dir / "identity.py"
        for field in (None, *identities):
            with self.subTest(field=field):
                substitute = "f" * 64 if field == "provider_sha256" else "substituted"
                values = identities if field is None else identities | {field: substitute}
                impl = _write_obj(exe, _valid_obj(**values))
                binding = self._binding(exe, impl)
                rid, aid = identities["request_id"], "att-identity-fixture"
                authority = _FixtureLiveAuthority(binding, request_id=rid, attempt_id=aid)
                wrapper = run_bound(binding, [], request_id=rid, attempt_id=aid, live_authority=authority)
                self.assertEqual(wrapper["rc"], 0)
                if field is None:
                    self.assertIn("slot_result", wrapper["receipt"]["attempt"])
                    self.assertEqual(wrapper["shared_records"]["terminal"]["artifact"]["sha256"], _GOOD_SHA)
                    self.assertTrue(wrapper["evidence_accepted"])
                else:
                    self.assertFalse(wrapper["evidence_accepted"])
                    self.assertFalse(wrapper["receipt"]["observation"]["producer"]["process_bound"])
                    self.assertNotIn("slot_result", wrapper["receipt"]["attempt"])

    def test_os_exit_and_minimal_status_cannot_substitute_for_pinned_result(self):
        exe = self.dir / "minimal.py"
        for result in ({"status": "OK", "rc": 0},
                       {"schema": "rea.slot_result/v1", "status": "OK", "rc": 0}):
            with self.subTest(result=result):
                impl = _write_obj(exe, result)
                wrapper = self._run(self._binding(exe, impl))
                self.assertEqual(wrapper["rc"], 0)
                self.assertEqual(json.loads(wrapper["stdout"]), result)
                self.assertFalse(wrapper["evidence_accepted"])
                self.assertNotIn("slot_result", wrapper["receipt"]["attempt"])
                digest = hashlib.sha256(wrapper["stdout"].encode()).hexdigest()
                artifact = wrapper["shared_records"]["terminal"]["artifact"]
                self.assertNotEqual(artifact["sha256"], digest)

    def test_result_reference_digest_must_match_custody_witness(self):
        exe = self.dir / "digest.py"
        payload = "résultat 🌱"
        raw = payload.encode()
        digest = hashlib.sha256(raw).hexdigest()
        valid = _valid_obj(result_payload=payload, sha256=digest)
        impl = _write_obj(exe, valid)
        wrapper = self._run(self._binding(exe, impl))
        receipt, custody = wrapper["receipt"], wrapper["custody"]
        result = receipt["attempt"]["slot_result"]
        self.assertEqual(result["sha256"], digest)
        self.assertEqual(custody["result_sha256"], digest)
        finalized = finalize_receipt(receipt, result | {"sha256": "0" * 64}, custody=custody)
        self.assertFalse(finalized["observation"]["producer"]["process_bound"])
        self.assertNotIn("slot_result", finalized["attempt"])

        forged = valid | {"sha256": "0" * 64}
        impl = _write_obj(exe, forged)
        refused = self._run(self._binding(exe, impl))
        self.assertEqual(json.loads(refused["stdout"]), forged)
        self.assertEqual(refused["rc"], 0)
        self.assertFalse(refused["evidence_accepted"])
        self.assertNotIn("slot_result", refused["receipt"]["attempt"])
        self.assertNotIn("result_sha256", refused["custody"])

        artifact = wrapper["shared_records"]["terminal"]["artifact"]
        self.assertEqual(artifact["sha256"], digest)
        self.assertEqual(artifact["bytes"], len(raw))
        self.assertNotEqual(digest, custody["host_result_sha256"])
        self.assertTrue(wrapper["evidence_accepted"])

    def test_fixture_cleanup_requires_recorded_identity_and_reap_witness(self):
        from slot_host.runtime import process_lifecycle

        script = self.dir / "teardown.py"
        _write_fixture(script, b"#!/usr/bin/env python3\nimport time; time.sleep(4)\n")
        child = subprocess.Popen([str(script)])
        starttime = process_lifecycle.proc_starttime(child.pid)
        try:
            self.assertGreater(child.pid, 1)
            self.assertIs(type(starttime), int)
            for missing in (None, True, starttime + 1):
                with self.subTest(starttime=missing):
                    self.assertFalse(runner._terminate_only(child.pid, child, 0.2, bound_starttime=missing))
                    self.assertFalse(runner._kill_only(child.pid, child, bound_starttime=missing))
                    self.assertFalse(process_lifecycle.safe_kill(child.pid, missing))
                    self.assertEqual(process_lifecycle.proc_starttime(child.pid), starttime)
                    self.assertIsNone(child.poll())
        finally:
            if child.poll() is None:
                process_lifecycle.safe_kill(child.pid, starttime)
                child.wait(timeout=5)

        exe = self.dir / "no-launch-identity.py"
        impl = _write_obj(exe, _valid_obj())
        with patch.object(runner, "_proc_starttime", return_value=None):
            wrapper = self._run(self._binding(exe, impl), cwd=None)
        self.assertEqual(wrapper["rc"], 0)
        self.assertFalse(wrapper["custody"]["cleanup_observed"])
        self.assertFalse(wrapper["evidence_accepted"])
        self.assertNotIn("slot_result", wrapper["receipt"]["attempt"])

    def test_timeout_reaps_bound_child_and_preserves_sibling(self):
        from slot_host.runtime import process_lifecycle

        child = self.dir / "close-pipes.py"
        marker = self.dir / "started.json"
        body = (
            "#!/usr/bin/env python3\n"
            "import json, os, time\n"
            f"open({str(marker)!r}, 'w').write(json.dumps([os.getpid(), os.getcwd()]))\n"
            "os.close(1)\nos.close(2)\ntime.sleep(0.50)\n"
        ).encode()
        impl = _write_fixture(child, body)
        sibling_script = self.dir / "sibling.py"
        _write_fixture(sibling_script, b"#!/usr/bin/env python3\nimport time; time.sleep(4)\n")
        sibling = subprocess.Popen([str(sibling_script)])
        sibling_start = process_lifecycle.proc_starttime(sibling.pid)
        try:
            self.assertIs(type(sibling_start), int)
            binding = self._binding(child, impl)
            start = time.monotonic()
            wrapper = self._run(binding, timeout_s=0.30, cwd=None)
            self.assertLess(time.monotonic() - start, 2)
            self.assertTrue(wrapper["timed_out"])
            self.assertFalse(wrapper["evidence_accepted"])
            self.assertEqual(wrapper["receipt"]["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
            self.assertTrue(wrapper["custody"]["cleanup_observed"])
            self.assertNotIn("slot_result", wrapper["receipt"]["attempt"])
            child_pid, scratch = json.loads(marker.read_text())
            self.assertEqual(child_pid, wrapper["pid"])
            self.assertFalse(Path(f"/proc/{child_pid}").exists(), "bound child is reaped")
            self.assertFalse(Path(scratch).exists(), "managed scratch is removed")
            self.assertEqual(process_lifecycle.proc_starttime(sibling.pid), sibling_start)
            self.assertIsNone(sibling.poll())
        finally:
            if sibling.poll() is None:
                process_lifecycle.safe_kill(sibling.pid, sibling_start)
                sibling.wait(timeout=5)

    def test_pipe_closed_child_observed_after_deadline_cannot_supply_result(self):
        exe = self.dir / "late-exit.py"
        obj = _valid_obj()
        document = json.dumps(obj).encode()
        body = (
            b"#!/usr/bin/env python3\nimport os, time\n"
            + ("os.write(1, %r)\n" % document).encode()
            + b"os.close(1); os.close(2); time.sleep(1.2)\n"
        )
        impl = _write_fixture(exe, body)
        binding = self._binding(exe, impl)
        success = self._run(binding, cwd=None, timeout_s=2.5)
        self.assertFalse(success["timed_out"])
        self.assertIn("slot_result", success["receipt"]["attempt"])
        collect = runner._collect_output

        def delay_observation(proc, deadline, cap):
            output = collect(proc, deadline, cap)
            # Introduce host-side delay after both pipes closed. The process
            # is already reaped when the supervisor resumes past its budget.
            proc.wait(timeout=2)
            return output

        with patch.object(runner, "_collect_output", side_effect=delay_observation):
            expired = self._run(binding, cwd=None, timeout_s=0.4)
        self.assertTrue(expired["timed_out"])
        self.assertEqual(expired["receipt"]["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
        self.assertNotIn("slot_result", expired["receipt"]["attempt"])
        self.assertFalse(expired["evidence_accepted"])
        self.assertFalse(Path(f"/proc/{expired['pid']}").exists())
        self.assertTrue(success["evidence_accepted"])

    def test_scratch_acquisition_error_retains_uncertainty_without_launch(self):
        exe = self.dir / "scratch-acquire.py"
        impl = _write_obj(exe, _valid_obj())
        retained = []
        binding = self._binding(exe, impl)
        with (patch.object(tempfile, "mkdtemp", side_effect=OSError("scratch acquisition")),
              patch.object(runner.subprocess, "Popen", wraps=runner.subprocess.Popen) as spawn):
            wrapper = self._run(binding, cwd=None, attempt_store=retained.append)
        spawn.assert_not_called()
        self.assertEqual(len(retained), 2)
        self.assertEqual(retained[-1]["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
        self.assertFalse(wrapper["custody"]["cleanup_observed"])
        self.assertFalse(wrapper["evidence_accepted"])
        self.assertNotIn("slot_result", retained[-1]["attempt"])

    def test_staging_acquisition_error_retains_uncertainty_and_removes_scratch(self):
        exe = self.dir / "acquire.py"
        marker = self.dir / "launches"
        impl = _write_obj(exe, _valid_obj(), counter=marker)
        scratch = self.dir / "managed"
        scratch.mkdir()
        retained = []
        with (patch.object(process_lifecycle, "create_run_scratch", return_value=(scratch, True)),
              patch.object(process_lifecycle, "create_stage_dir", side_effect=OSError("stage acquisition"))):
            wrapper = self._run(self._binding(exe, impl), cwd=None, attempt_store=retained.append)
        self.assertFalse(marker.exists())
        self.assertFalse(scratch.exists())
        self.assertEqual(len(retained), 2)
        self.assertEqual(retained[-1]["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
        self.assertFalse(wrapper["evidence_accepted"])
        self.assertNotIn("slot_result", retained[-1]["attempt"])

    def test_stage_removal_interrupt_retains_uncertain_terminal_attempt(self):
        exe = self.dir / "stage-cleanup.py"
        impl = _write_obj(exe, _valid_obj())
        stage = self.dir / "stage"
        stage.mkdir()
        retained = []
        interrupt = KeyboardInterrupt("stage removal")
        remove = shutil.rmtree

        def remove_stage(path, *args, **kwargs):
            if Path(path) == stage:
                raise interrupt
            return remove(path, *args, **kwargs)

        bound = self._binding(exe, impl)
        with (patch.object(process_lifecycle, "create_stage_dir", return_value=(stage, True)),
              patch.object(shutil, "rmtree", side_effect=remove_stage),
              self.assertRaises(KeyboardInterrupt) as caught):
            self._run(bound, cwd=None, attempt_store=retained.append)
        self.assertIs(caught.exception, interrupt)
        self.assertEqual(len(retained), 2)
        self.assertEqual(retained[-1]["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
        self.assertFalse(retained[-1]["observation"]["producer"]["process_bound"])
        self.assertNotIn("slot_result", retained[-1]["attempt"])

    def test_pipe_read_error_cannot_be_treated_as_eof(self):
        exe = self.dir / "read-error.py"
        obj = _valid_obj()
        _write_obj(exe, obj)
        with exe.open("ab") as fixture:
            fixture.write(b"sys.stderr.write('unread diagnostics'); sys.stderr.flush()\n")
        binding = self._binding(exe, sha256_of_file(exe))
        collect = runner._collect_output
        read = os.read

        def fault_in_capture(proc, *args):
            err_fd = proc.stderr.fileno()

            def read_pipe(fd, size):
                if fd == err_fd:
                    raise OSError("unread stderr")
                return read(fd, size)

            with patch.object(os, "read", side_effect=read_pipe):
                return collect(proc, *args)

        retained = []
        with patch.object(runner, "_collect_output", side_effect=fault_in_capture):
            wrapper = self._run(binding, cwd=None, attempt_store=retained.append)
        self.assertEqual(len(retained), 2)
        self.assertEqual(retained[-1]["attempt"].get("control_outcome"), "UNCERTAIN_OUTCOME")
        self.assertFalse(wrapper["evidence_accepted"])
        self.assertNotIn("slot_result", retained[-1]["attempt"])
        self.assertFalse(Path(f"/proc/{wrapper['pid']}").exists())

    def test_supervision_interrupt_survives_teardown_interrupt(self):
        exe = self.dir / "supervision.py"
        impl = _write_fixture(exe, b"#!/usr/bin/env python3\nimport time; time.sleep(3)\n")
        bound = self._binding(exe, impl)
        retained = []
        primary = KeyboardInterrupt("capture interrupted")
        shutdown = KeyboardInterrupt("termination interrupted")
        observed = {}

        def interrupt_capture(proc, *args):
            observed.update(pid=proc.pid, starttime=process_lifecycle.proc_starttime(proc.pid), proc=proc)
            raise primary

        try:
            with (patch.object(runner, "_collect_output", side_effect=interrupt_capture),
                  patch.object(runner, "_terminate_only", side_effect=shutdown),
                  self.assertRaises(KeyboardInterrupt) as caught):
                self._run(bound, cwd=None, attempt_store=retained.append)
            self.assertIs(caught.exception, primary)
            self.assertEqual(len(retained), 2)
            self.assertEqual(retained[-1]["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
            self.assertNotIn("slot_result", retained[-1]["attempt"])
            self.assertFalse(Path(f"/proc/{observed['pid']}").exists())
        finally:
            proc = observed.get("proc")
            if proc is not None and proc.poll() is None:
                process_lifecycle.safe_kill(observed["pid"], observed["starttime"])
                proc.wait(timeout=5)

    def test_supervision_interrupt_retains_uncertain_terminal_attempt(self):
        exe = self.dir / "interrupt.py"
        impl = _write_obj(exe, _valid_obj())
        binding = self._binding(exe, impl)
        for phase in ("scratch-acquisition", "supervision"):
            with self.subTest(phase=phase):
                retained = []
                interrupt = KeyboardInterrupt(phase)
                if phase == "scratch-acquisition":
                    fault = patch.object(tempfile, "mkdtemp", side_effect=interrupt)
                else:
                    fault = patch.object(runner, "_collect_output", side_effect=interrupt)
                with (fault,
                      patch.object(runner, "_attach_shared_records", wraps=runner._attach_shared_records) as project,
                      self.assertRaises(KeyboardInterrupt) as raised):
                    self._run(binding, cwd=None, attempt_store=retained.append)
                self.assertIs(raised.exception, interrupt)
                self.assertEqual(len(retained), 2, "retain start and terminal uncertainty")
                terminal = retained[-1]
                self.assertEqual(terminal["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
                self.assertFalse(terminal["observation"]["producer"]["process_bound"])
                self.assertNotIn("slot_result", terminal["attempt"])
                expected = "confirmed_retired" if phase == "supervision" else "unknown"
                self.assertEqual(project.call_args.kwargs["cleanup_state"], expected)

    def test_scratch_cleanup_interrupt_retains_uncertain_terminal_attempt(self):
        exe = self.dir / "remove.py"
        impl = _write_obj(exe, _valid_obj())
        retained = []
        scratch = self.dir / "managed"
        scratch.mkdir()
        mkdtemp = tempfile.mkdtemp
        rmtree = shutil.rmtree

        def allocate(*, prefix):
            return str(scratch) if prefix == "rea-runtime-" else mkdtemp(prefix=prefix)

        def remove(path, *args, **kwargs):
            if Path(path) == scratch:
                raise KeyboardInterrupt("scratch removal")
            return rmtree(path, *args, **kwargs)

        with (patch.object(tempfile, "mkdtemp", side_effect=allocate),
              patch.object(shutil, "rmtree", side_effect=remove),
              patch.object(runner, "_attach_shared_records", wraps=runner._attach_shared_records) as project,
              self.assertRaisesRegex(KeyboardInterrupt, "scratch removal")):
            self._run(self._binding(exe, impl), cwd=None, attempt_store=retained.append)
        self.assertTrue(scratch.exists())
        self.assertEqual(len(retained), 2)
        terminal = retained[-1]
        self.assertEqual(terminal["attempt"]["control_outcome"], "UNCERTAIN_OUTCOME")
        self.assertFalse(terminal["observation"]["producer"]["process_bound"])
        self.assertNotIn("slot_result", terminal["attempt"])
        self.assertFalse(project.call_args.args[0]["custody"]["cleanup_observed"])
        self.assertEqual(project.call_args.kwargs["cleanup_state"], "unknown")
        self.assertIsNone(project.call_args.kwargs["cleanup_retirement_sha256"])

    def test_terminal_retention_failure_cannot_publish_accepted_observation(self):
        exe = self.dir / "retain.py"
        marker = self.dir / "launches"
        impl = _write_obj(exe, _valid_obj(), counter=marker)
        retained = []

        def retain(receipt):
            if not retained:
                self.assertFalse(marker.exists(), "durable start precedes launch")
                retained.append(receipt)
                return True
            self.assertTrue(marker.exists(), "terminal publication follows the effect")
            return False

        wrapper = self._run(self._binding(exe, impl), attempt_store=retain)
        self.assertEqual(marker.read_text().splitlines(), ["x"])
        self.assertEqual(wrapper["receipt"]["attempt"].get("control_outcome"), "UNCERTAIN_OUTCOME")
        self.assertFalse(wrapper["evidence_accepted"])
        self.assertFalse(wrapper["receipt"]["observation"]["producer"]["process_bound"])
        self.assertNotIn("slot_result", wrapper["receipt"]["attempt"])
        self.assertIsNone(wrapper["shared_records"])
        adapter = wrapper["receipt"]["shared_receipt_adapter"]
        self.assertNotIn("shared_receipt_ref", adapter)
        self.assertFalse(adapter["live_use_permitted"])

    def test_vision_request_under_none_ceiling_has_zero_spawn(self):
        self._assert_perception_refusal("vision", "none")
        self._assert_perception_refusal("vision", "vision")

    def test_optional_runtime_perception_is_parked_before_launch(self):
        for level in ("cdp_atspi", "vision"):
            with self.subTest(level=level):
                self._assert_perception_refusal(level, "vision")

    @unittest.skip("owner-pending: T06")
    def test_runtime_vision_requires_current_grant_and_witnessed_release(self):
        """Requires current owner-bound grant, work, and witnessed release."""

    def _assert_perception_refusal(self, level, ceiling):
        exe = self.dir / "perception.py"
        marker = self.dir / "perception-launch"
        impl = _write_obj(exe, _valid_obj(), counter=marker)
        manifest = _manifest(impl)
        manifest["perception"]["optional_levels"] = ["cdp_atspi", "vision"]
        manifest["perception"]["observation_sources"].append({
            "id": "fixture-cdp", "level": "cdp_atspi", "producer": "fixture",
            "outside_capsule": True,
        })
        manifest["perception"]["observation_sources"].append({
            "id": "fixture-vision", "level": "vision", "producer": "fixture",
            "outside_capsule": True, "vision_pool": {
                "required": True, "owner": "ViewPoint_Tools", "ticket_field": "vision_pool_ticket",
            },
        })
        ref = "installation:fixture-slot@perception-custody"
        policy = fixture_policy(manifest) | {"perception_ceiling": ceiling}
        request_id, _ = _fresh("perception")
        pool = Mock()
        with self.assertRaises(BindingRejected) as raised:
            _run_adapter(
                {"installation_ref": ref, "perception_level": level},
                request_id=request_id, host={ref: (manifest, str(exe))},
                host_policy=policy, vision_pool=pool, live_authority=_AllowLiveAuthority(),
            )
        self.assertEqual(raised.exception.reason, "perception_denied" if ceiling == "none" else "availability_parked")
        if ceiling != "none":
            self.assertEqual(raised.exception.detail, "owner-pending: T06")
        self.assertFalse(marker.exists())
        pool.acquire.assert_not_called()


if __name__ == "__main__":
    unittest.main()
