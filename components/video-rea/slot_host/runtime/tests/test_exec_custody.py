# SPDX-License-Identifier: AGPL-3.0-only
"""Executable custody, typed argument order, and environment admission."""
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from slot_host.runtime.binding import sha256_of_file
from slot_host.runtime.tools import _run_adapter
from slot_host.runtime.tests.binding_fixtures import (
    fixture_policy,
    fixture_retention,
    run_fixture as run_bound,
    FIXTURE_RESULT_PAYLOAD,
    FIXTURE_RESULT_SHA256,
    FixtureLaunch as _AllowLiveAuthority,
    verified_fixture_binding as verify_binding,
)

setUpModule = fixture_retention



_ADAPTER = "d" * 64
_PROVIDER_SHA = "a" * 64
_REQ_DIGEST = "b" * 64
_RES_DIGEST = "c" * 64
_GOOD_SHA = FIXTURE_RESULT_SHA256
_counter = [0]


def _fresh(tag):
    _counter[0] += 1
    return f"req-cust-{tag}-{_counter[0]:04d}", f"att-cust-{tag}-{_counter[0]:04d}"


def _manifest(impl_sha, *, typed_args=None):
    def _op(oid, role, targs):
        return {"id": oid, "role": role,
                "input_ref": f"urn:rea.slot_request:v2#slot={role}",
                "output_ref": f"urn:rea.slot_result:v2#slot={role}",
                "binding": {"kind": "cli_subcommand", "argv": [oid], "typed_args": targs},
                "required_effects": ["process.spawn:child"],
                "required_grants": ["grant.exec.external_process"],
                "limits": {"timeout_s": 5, "max_output_bytes": 4096},
                "retry_semantics": {"mode": "at_most_once", "on_interrupt": "UNCERTAIN_OUTCOME"}}

    rec_typed = list(typed_args) if typed_args is not None else []
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
            "operations": [_op("record", "replay", rec_typed), _op("replay", "replay", []), _op("compare", "verify", [])],
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


def _write_echo_fixture(path: Path, *, counter: Path | None = None) -> str:
    """Fixture emitting the valid object plus its own argv echo."""
    lines = [b"#!/usr/bin/env python3\n", b"# SPDX-License-Identifier: AGPL-3.0-only\n",
             b"import json, os, sys\n"]
    if counter is not None:
        lines.append(b"open(%r, 'a').write('x\\n')\n" % str(counter))
    inner = json.dumps(_valid_obj(argv_echo=None))
    lines.append(b"obj = json.loads(" + repr(inner).encode() + b")\n")
    lines.append(b"obj['argv_echo'] = sys.argv[1:]\n")
    lines.append(b"obj['env_echo'] = {name: os.environ.get(name) for name in ('PATH', 'HOST_ONLY', 'MANIFEST_ONLY')}\n")
    lines.append(b"sys.stdout.write(json.dumps(obj) + '\\n')\n")
    lines.append(b"sys.stdout.flush()\n")
    return _write_fixture(path, b"".join(lines))


class ExecCustodyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _binding(self, exe: Path, impl: str, *, typed_args=None):
        return verify_binding(_manifest(impl, typed_args=typed_args), str(exe),
                              operation_id="record", host_grants={"grant.exec.external_process"})

    def _run(self, binding, argv, **kwargs):
        req, att = _fresh("cust")
        return run_bound(binding, argv, timeout_s=5, cwd=str(self.dir),
                         request_id=req, attempt_id=att,
                         live_authority=_AllowLiveAuthority(), **kwargs)

    def test_symlink_executable_rejected_at_launch(self):
        """A symlink resolving to the pinned bytes must not launch.

        The pre-U08 runner opened the executable by path, followed the
        link, and spawned it.  O_NOFOLLOW custody refuses the link.
        """
        real = self.dir / "fixture.py"
        impl = _write_echo_fixture(real)
        link = self.dir / "linked.py"
        os.symlink(real, link)
        # Binding hashes through the link; the launch gate must refuse.
        binding = self._binding(link, impl)
        w = self._run(binding, [])
        self.assertIsNone(w["pid"], "symlinked executable must never spawn")
        self.assertFalse(w["evidence_accepted"])
        self.assertEqual(w["receipt"]["attempt"]["control_outcome"], "evidence_binding_rejected")
        self.assertNotIn("slot_result", w["receipt"]["attempt"])

    def test_typed_integer_rejects_non_integer(self):
        exe = self.dir / "typed.py"
        impl = _write_echo_fixture(exe)
        binding = self._binding(exe, impl, typed_args=[{"name": "count", "type": "integer", "position": 0}])
        w = self._run(binding, ["not-an-int"])
        self.assertIsNone(w["pid"], "integer-typed argv with non-integer must never spawn")
        self.assertFalse(w["evidence_accepted"])
        self.assertEqual(w["receipt"]["attempt"]["control_outcome"], "evidence_binding_rejected")
        self.assertNotIn("slot_result", w["receipt"]["attempt"])

    def test_typed_arity_rejects_extra_argv(self):
        exe = self.dir / "arity.py"
        impl = _write_echo_fixture(exe)
        binding = self._binding(exe, impl, typed_args=[{"name": "count", "type": "integer", "position": 0}])
        w = self._run(binding, ["1", "injected-extra"])
        self.assertIsNone(w["pid"], "raw extra argv beyond typed_args must never spawn")
        self.assertFalse(w["evidence_accepted"])
        self.assertEqual(w["receipt"]["attempt"]["control_outcome"], "evidence_binding_rejected")

    def test_typed_boolean_and_path_validation(self):
        cases = [
            ("bool-case", [{"name": "flag", "type": "boolean", "position": 0}], ["TRUE"]),
            ("path-empty", [{"name": "target", "type": "path", "position": 0}], [""]),
        ]
        for name, targs, bad_argv in cases:
            with self.subTest(case=name):
                exe = self.dir / f"typed-{name}.py"
                impl = _write_echo_fixture(exe)
                binding = self._binding(exe, impl, typed_args=targs)
                w = self._run(binding, bad_argv)
                self.assertIsNone(w["pid"], f"{name} must never spawn")
                self.assertFalse(w["evidence_accepted"])
                self.assertEqual(w["receipt"]["attempt"]["control_outcome"], "evidence_binding_rejected")

    def test_spawn_environment_is_intersection_of_verified_allowlists(self):
        # Distinct types make the positions observable even with a reversed declaration.
        targs = [{"name": "flag", "type": "boolean", "position": 1},
                 {"name": "count", "type": "integer", "position": 0}]
        exe = self.dir / "typed-order.py"
        counter = self.dir / "adapter-launches"
        impl = _write_echo_fixture(exe, counter=counter)
        ref = "installation:fixture-slot@adapter-custody"

        def run(argv, typed_args):
            manifest = _manifest(impl, typed_args=typed_args)
            manifest["transport"]["env_allowlist"].append("MANIFEST_ONLY")
            request_id, _ = _fresh("adapter")
            return _run_adapter(
                {"installation_ref": ref, "operation_id": "record", "argv": argv},
                request_id=request_id, host={ref: (manifest, str(exe))},
                host_policy=fixture_policy(manifest), live_authority=_AllowLiveAuthority(),
                host_env_allowlist=frozenset({"PATH", "HOST_ONLY"}),
            )

        with patch.dict(os.environ, {"PATH": "/usr/bin", "HOST_ONLY": "host", "MANIFEST_ONLY": "manifest"}):
            typed = run(["42", "true"], targs)
            zero = run([], [])
            for argv, declaration in ((["true", "42"], targs),
                                      (["42", "true", "--raw-extra-arg"], targs),
                                      (["--raw-extra-arg"], [])):
                with self.subTest(argv=argv):
                    refused = run(argv, declaration)
                    self.assertIsNone(refused["pid"])
                    self.assertFalse(refused["evidence_accepted"])
                    self.assertEqual(refused["receipt"]["attempt"]["control_outcome"], "evidence_binding_rejected")
                    self.assertNotIn("slot_result", refused["receipt"]["attempt"])
        for wrapper, expected in ((typed, ["record", "42", "true"]), (zero, ["record"])):
            self.assertEqual(wrapper["shared_records"]["terminal"]["artifact"]["sha256"], _GOOD_SHA)
            self.assertTrue(wrapper["evidence_accepted"])
            output = json.loads(wrapper["stdout"])
            self.assertEqual(output["argv_echo"], expected)
            self.assertEqual(output["env_echo"], {"PATH": "/usr/bin", "HOST_ONLY": None, "MANIFEST_ONLY": None})
        self.assertEqual(counter.read_text().splitlines(), ["x", "x"])

    def test_changed_launch_object_cannot_inherit_verified_digest(self):
        ctr = self.dir / "ctr-subst.txt"
        exe = self.dir / "fixture.py"
        impl = _write_echo_fixture(exe, counter=ctr)
        binding = self._binding(exe, impl)
        replacement = self.dir / "replacement.py"
        shutil.copy2(exe, replacement)
        original_inode = exe.stat().st_ino

        def replace_after_measurement(opened):
            if replacement.exists():
                self.assertIsNone(opened["attempt"].get("control_outcome"))
                replacement.replace(exe)

        w = self._run(binding, [], attempt_store=replace_after_measurement)
        self.assertNotEqual(exe.stat().st_ino, original_inode)
        self.assertIsNone(w["pid"])
        self.assertFalse(w["evidence_accepted"])
        self.assertEqual(w["receipt"]["attempt"]["control_outcome"], "evidence_binding_rejected")
        self.assertNotIn("slot_result", w["receipt"]["attempt"])
        self.assertFalse(ctr.exists(), "tampered bytes must never have executed")

    def test_run_scratch_cannot_redirect_executable_resolution(self):
        ctr = self.dir / "ctr-decoy.txt"
        installation = self.dir / "installation"
        installation.mkdir()
        exe = installation / "fixture.py"
        impl = _write_echo_fixture(exe, counter=ctr)
        registered = self.dir / "registered"
        registered.symlink_to(installation, target_is_directory=True)
        relative = Path(os.path.relpath(registered / exe.name))
        binding = self._binding(relative, impl)
        scratch = self.dir / "scratch"
        scratch.mkdir()
        decoy_ctr = self.dir / "decoy-counter.txt"
        _write_echo_fixture(scratch / exe.name, counter=decoy_ctr)
        registered.unlink()
        registered.symlink_to(scratch, target_is_directory=True)
        real_mkdtemp = tempfile.mkdtemp
        with patch.object(tempfile, "mkdtemp", side_effect=lambda *, prefix: (
            str(scratch) if prefix == "rea-runtime-" else real_mkdtemp(prefix=prefix)
        )):
            req, att = _fresh("decoy")
            w = run_bound(binding, [], request_id=req, attempt_id=att,
                          live_authority=_AllowLiveAuthority())
        self.assertEqual(w["shared_records"]["terminal"]["artifact"]["sha256"], _GOOD_SHA)
        self.assertTrue(w["evidence_accepted"], "pinned bytes must run")
        self.assertEqual(ctr.read_text().splitlines(), ["x"], "pinned object executed exactly once")
        self.assertFalse(decoy_ctr.exists(), "scratch decoy must never execute")
        self.assertFalse(scratch.exists(), "managed scratch must be removed")

    def test_launch_execs_open_snapshot_fd(self):
        """The child must be exec'd through the open snapshot fd."""
        import slot_host.runtime.runner as runner_mod
        exe = self.dir / "fd-exec.py"
        impl = _write_echo_fixture(exe)
        binding = self._binding(exe, impl)
        orig_popen = runner_mod.subprocess.Popen
        seen = {}

        def _spy(*args, **kwargs):
            seen.update(kwargs)
            return orig_popen(*args, **kwargs)

        runner_mod.subprocess.Popen = _spy
        try:
            w = self._run(binding, [])
        finally:
            runner_mod.subprocess.Popen = orig_popen
        self.assertTrue(w["evidence_accepted"], "fd-exec launch must still complete")
        exe_arg = seen.get("executable", "")
        self.assertTrue(exe_arg.startswith("/proc/self/fd/"),
                        f"child must exec via an open fd, got executable={exe_arg!r}")
        try:
            fd_num = int(exe_arg.rsplit("/", 1)[1])
        except (ValueError, IndexError):
            self.fail(f"could not parse fd from executable={exe_arg!r}")
        self.assertIn(fd_num, tuple(seen.get("pass_fds") or ()),
                      "the exec'd fd must be passed to the child")

    def test_filtered_env_intersection_unit(self):
        from slot_host.runtime import executable_custody

        with patch.dict(os.environ, {"PATH": "/usr/bin", "HOST_ONLY": "y"}):
            env = executable_custody.filtered_env(
                ["PATH", "EXCUST_TOKEN"], frozenset({"PATH", "HOST_ONLY"}),
                {"PATH": "/custom", "EXCUST_TOKEN": "x", "HOST_ONLY": "y"})
        self.assertEqual(env, {"PATH": "/custom"})

    def test_stage_verifies_digest(self):
        from slot_host.runtime import executable_custody

        exe = self.dir / "stage-src.py"
        impl = _write_echo_fixture(exe)
        stage = Path(tempfile.mkdtemp(prefix="rea-stage-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(stage, ignore_errors=True))
        snap = executable_custody.stage_verified_executable(str(exe), impl, stage, "fixture.py")
        self.assertEqual(sha256_of_file(snap), impl)
        with self.assertRaises(executable_custody.ExecutableCustodyError):
            executable_custody.stage_verified_executable(str(exe), "f" * 64, stage, "other.py")


if __name__ == "__main__":
    unittest.main()
