import copy
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from asset_factory import core
from tools.qualification import qualify, verify_receipt
from tools.qualification.harness import DEFAULT_LIMITS, combined_checks, measurements, run_offline
from tools.qualification.manifest import Pins, check_candidate
from tools.qualification.offline import establish_namespace


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "test_d18_qual_candidate.py"


def pin(path):
    path = Path(path).resolve(strict=True)
    return {"path": str(path), "sha256": core.sha256_file(path)}


class QualificationTests(unittest.TestCase):
    def setUp(self):
        saved = os.environ.get("AF_QUAL_FIXTURE_EVIDENCE")
        if saved and self._testMethodName in (
            "test_offline_cpu_run_and_denied_fetch_control", "test_swallowed_network_error_refuses_candidate",
            "test_invalid_clone_flags_are_denied_by_kernel", "test_candidate_cannot_write_outside_workdir",
            "test_pid1_streams_cannot_write_supervisor_logs",
        ):
            self.root = Path(saved) / (self._testMethodName + "-inputs")
            self.root.mkdir(mode=0o700)
        else:
            root = tempfile.TemporaryDirectory()
            self.addCleanup(root.cleanup)
            self.root = Path(root.name)
        self.fixture = self.root / "candidate.py"
        self.fixture.write_bytes(FIXTURE.read_bytes())
        weights = self.root / "weights.bin"
        weights.write_bytes(b"synthetic weights for the CPU qualification fixture\n")
        licence = self.root / "dependency.txt"
        licence.write_text("Synthetic licence fixture for a non-production test.\n")
        runtime = self.root / "runtime.json"
        runtime.write_text('{"fixture":true}\n')
        self.manifest = {
            "schema": "af.qualification-candidate/v1",
            "provider": "qual-cpu-fixture",
            "code": {"revision": core.sha256_file(self.fixture), "entrypoint": pin(self.fixture), "files": []},
            "weights": [pin(weights)],
            "conditioners": [],
            "dependencies": [{"name": "qualification-fixture", "licence": pin(licence), "files": [pin(runtime)]}],
            "runtime": {"interpreter": pin(sys.executable), "files": [pin(runtime)], "cuda": False},
            "request": self.write_json("request.json", {"try_fetch": False}),
            "input_schema": self.write_json("input-schema.json", self.schema("try_fetch")),
            "output_schema": self.write_json("output-schema.json", self.schema("fixture")),
            "outputs": ["artifact.bin", "result.json"],
        }
        self.lock_runtime()
        self.bind_rights()

    def lock_runtime(self, *import_roots):
        runtime = self.manifest["runtime"]
        runtime["lock"] = self.write_json("runtime-lock.json", {
            "schema": "af.qualification-python-runtime/v1",
            "interpreter_sha256": runtime["interpreter"]["sha256"],
            "files": runtime["files"],
            "import_roots": [str(path) for path in import_roots],
        })

    @staticmethod
    def schema(key):
        return {
            "type": "object", "required": [key],
            "properties": {key: {"type": "boolean"}}, "additionalProperties": False,
        }

    def write_json(self, name, value):
        path = self.root / name
        path.write_text(core.canonical_json(value))
        return pin(path)

    def bind_rights(self, verdict="PASS"):
        subject = {key: value for key, value in self.manifest.items() if key != "rights"}
        self.manifest["rights"] = self.write_json("rights.json", {
            "schema": "af.qualification-rights/v1", "provider": subject["provider"], "jurisdiction": "EU",
            "commercial_use": True, "verdict": verdict, "subject_sha256": core.sha256_json(subject),
        })

    def run_qualification(self, name, manifest=None, *, retain=False, timeout_s=15):
        saved = os.environ.get("AF_QUAL_FIXTURE_EVIDENCE") if retain else None
        evidence = (Path(saved) if saved else self.root) / name
        receipt = qualify(self.manifest if manifest is None else manifest, evidence, timeout_s=timeout_s)
        self.assertEqual(core.load_json(evidence / "qualification.json", max_bytes=1024*1024), receipt)
        return receipt

    def test_offline_cpu_run_and_denied_fetch_control(self):
        receipt = self.run_qualification("cpu-offline", retain=True)
        self.assertEqual(receipt["rc"], 0, receipt)
        self.assertTrue(receipt["review_required"])
        control, positive = receipt["runs"]
        self.assertEqual(control["role"], "denied-fetch")
        self.assertTrue(control["fetch_probe_passed"])
        self.assertEqual(control["network_attempts"], 1)
        self.assertEqual(positive["role"], "candidate")
        self.assertEqual(positive["network_attempts"], 0)
        self.assertNotEqual(positive["network_namespace"]["inode"], receipt["caller_netns_inode"])
        for run in receipt["runs"]:
            self.assertGreater(run["cgroup_peak_bytes"], 0)
            self.assertEqual(run["cgroup_peak_source"], "pidns_test_scope")
            self.assertGreater(run["rss_peak_bytes"], 0)
            self.assertGreater(run["wall_ns"], 0)
            self.assertTrue(run["cleanup_proven"])
        self.assertIsNone(positive["cuda_peak_allocated_bytes"])
        self.assertEqual(positive["outputs"]["artifact.bin"]["sha256"],
                         hashlib.sha256(b"QUAL CPU fixture\n").hexdigest())

    def test_swallowed_network_error_refuses_candidate(self):
        self.manifest["request"] = self.write_json("request.json", {"try_fetch": True})
        self.bind_rights()
        receipt = self.run_qualification("cpu-fetch-refused", retain=True)
        self.assertEqual(receipt["rc"], 5, receipt)
        self.assertIn("candidate_network_attempt", receipt["reason_codes"])
        candidate = receipt["runs"][1]
        self.assertEqual(candidate["returncode"], 0)
        self.assertEqual(candidate["network_attempts"], 1)
        self.assertTrue(candidate["cleanup_proven"])

    def test_missing_identity_and_dependency_pin_refuse_before_start(self):
        missing_licence = copy.deepcopy(self.manifest)
        missing_licence["dependencies"][0].pop("licence")
        missing_weight = copy.deepcopy(self.manifest)
        missing_weight["weights"][0].pop("sha256")
        missing_conditioner = copy.deepcopy(self.manifest)
        missing_conditioner["conditioners"] = [{"name": "fixture-encoder", "files": [{"path": str(self.fixture)}]}]
        directory = self.run_qualification("directory-only", {"provider": str(self.root)})
        self.assertEqual(directory["reason_codes"], ["candidate_identity_required"])
        self.assertEqual(directory["runs"], [])
        for index, (candidate, code) in enumerate((
                (missing_licence, "invalid_component_pin"),
                (missing_weight, "missing_or_invalid_file_pin"),
                (missing_conditioner, "missing_or_invalid_file_pin"),
        )):
            with self.subTest(index=index):
                self.manifest = candidate
                self.bind_rights()
                receipt = self.run_qualification(f"missing-{index}")
                self.assertEqual(receipt["reason_codes"], [code])
                self.assertEqual(receipt["runs"], [])

    def test_changed_weight_refuses_before_start(self):
        Path(self.manifest["weights"][0]["path"]).write_bytes(b"changed")
        receipt = self.run_qualification("stale")
        self.assertEqual(receipt["rc"], 4)
        self.assertIn("pin_digest_mismatch", receipt["reason_codes"])
        self.assertEqual(receipt["runs"], [])

    def test_declared_denial_and_rights_from_another_candidate_refuse(self):
        self.bind_rights(verdict="FAIL")
        denied = self.run_qualification("denied")
        self.assertEqual(denied["rc"], 5)
        self.manifest["provider"] = "another-fixture"
        unbound = self.run_qualification("unbound")
        self.assertEqual(unbound["rc"], 4)
        self.assertEqual(denied["runs"] + unbound["runs"], [])

    def test_excluded_provider_refuses_even_with_permissive_rights(self):
        self.manifest["provider"] = "tencent/Hunyuan3D-2.1"
        self.bind_rights()
        receipt = self.run_qualification("excluded")
        self.assertEqual(receipt["rc"], 5)
        self.assertEqual(receipt["reason_codes"], ["provider_excluded_eu"])
        self.assertEqual(receipt["runs"], [])

    def test_excluded_weights_under_an_alias_refuse(self):
        excluded = self.root / "Hunyuan3D-2.1"
        excluded.mkdir()
        weights = excluded / "weights.bin"
        weights.write_bytes(b"synthetic exclusion witness")
        alias = self.root / "alias.bin"
        alias.symlink_to(weights)
        self.manifest["weights"] = [{"path": str(alias), "sha256": core.sha256_file(weights)}]
        self.bind_rights()
        receipt = self.run_qualification("excluded-alias")
        self.assertEqual(receipt["reason_codes"], ["provider_excluded_eu"])
        self.assertEqual(receipt["runs"], [])

    def test_interpreter_uses_resolved_path(self):
        interpreter = self.root / "venv-python"
        interpreter.symlink_to(sys.executable)
        self.manifest["runtime"]["interpreter"]["path"] = str(interpreter)
        self.bind_rights()
        actual, _, _, _ = check_candidate(self.manifest, Pins())
        self.assertEqual(actual, interpreter.resolve())

    def test_broader_excluded_family_aliases(self):
        for index, name in enumerate(("tencent/Hunyuan3D-v2.1", "hy3dpaint")):
            with self.subTest(name=name):
                self.manifest["provider"] = name
                self.bind_rights()
                receipt = self.run_qualification(f"excluded-name-{index}")
                self.assertEqual(receipt["reason_codes"], ["provider_excluded_eu"])
                self.assertEqual(receipt["runs"], [])

    def test_excluded_digest_under_neutral_path(self):
        self.manifest["weights"][0]["sha256"] = (
            "6b519fc7242f78e9b5f47ea4d55668fe3d944a2d27332f4ca68d29a6ff603f5e"
        )
        self.bind_rights()
        receipt = self.run_qualification("excluded-digest")
        self.assertEqual(receipt["reason_codes"], ["provider_excluded_eu"])
        self.assertEqual(receipt["runs"], [])

    def test_required_runtime_lock(self):
        del self.manifest["runtime"]["lock"]
        self.bind_rights()
        receipt = self.run_qualification("missing-runtime-lock")
        self.assertEqual(receipt["reason_codes"], ["runtime_lock_required"])
        self.assertEqual(receipt["runs"], [])

    def test_receipt_verifier_rejects_forged_fields(self):
        evidence = self.root / "receipt-verifier"
        receipt = self.run_qualification(evidence.name)
        self.assertEqual(receipt["rc"], 0, receipt)
        trusted_digest = core.sha256_json(receipt)
        self.assertEqual(verify_receipt(evidence, expected_sha256=trusted_digest), receipt)
        for target, key, value, reason in (
            (None, "candidate_sha256", "0" * 64, "receipt_candidate_mismatch"),
            (1, "outputs", {}, "receipt_output_mismatch"),
            (1, "logs", {}, "receipt_log_mismatch"),
            (1, "wall_ns", 1, "receipt_supervision_mismatch"),
            (1, "rss_peak_bytes", 1, "receipt_measurement_mismatch"),
            (1, "network_attempts", False, "receipt_isolation_mismatch"),
        ):
            with self.subTest(key=key):
                changed = copy.deepcopy(receipt)
                record = changed if target is None else changed["runs"][target]
                record[key] = value
                (evidence / "qualification.json").write_text(core.canonical_json(changed))
                # A matching digest still requires semantic validation.
                with self.assertRaisesRegex(ValueError, reason):
                    verify_receipt(evidence, expected_sha256=core.sha256_json(changed))
                with self.assertRaisesRegex(ValueError, "receipt_digest_mismatch"):
                    verify_receipt(evidence, expected_sha256=trusted_digest)

    def test_receipt_verifier_rehashes_retained_bytes_and_pins(self):
        evidence = self.root / "receipt-retained"
        receipt = self.run_qualification(evidence.name)
        trusted_digest = core.sha256_json(receipt)
        self.assertEqual(verify_receipt(evidence, expected_sha256=trusted_digest), receipt)
        for path, code in (
            (evidence / "candidate" / "network.trace", "receipt_log_mismatch"),
            (evidence / "output" / "artifact.bin", "receipt_output_mismatch"),
            (Path(self.manifest["weights"][0]["path"]), "pin_digest_mismatch"),
        ):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b"changed")
                with self.assertRaisesRegex(ValueError, code):
                    verify_receipt(evidence, expected_sha256=trusted_digest)
                path.write_bytes(original)

    def test_receipt_verifier_refuses_consistent_forgery_without_trusted_digest(self):
        evidence = self.root / "consistent-forgery"
        receipt = self.run_qualification(evidence.name)
        self.assertEqual(receipt["rc"], 0, receipt)
        trusted_digest = core.sha256_json(receipt)
        self.assertEqual(verify_receipt(evidence, expected_sha256=trusted_digest), receipt)

        artifact = evidence / "output" / "artifact.bin"
        replacement = b"untrusted fixture replacement\n"
        artifact.write_bytes(replacement)
        receipt["runs"][1]["outputs"][artifact.name] = {
            "sha256": core.sha256_file(artifact), "bytes": len(replacement),
        }
        (evidence / "qualification.json").write_text(core.canonical_json(receipt))

        with self.subTest(digest="omitted"):
            with self.assertRaisesRegex(TypeError, "expected_sha256"):
                verify_receipt(evidence)
        with self.subTest(digest="null"):
            with self.assertRaisesRegex(ValueError, "receipt_digest_mismatch"):
                verify_receipt(evidence, expected_sha256=None)
        with self.subTest(digest="original"):
            with self.assertRaisesRegex(ValueError, "receipt_digest_mismatch"):
                verify_receipt(evidence, expected_sha256=trusted_digest)

    def test_runtime_lock_covers_imported_module(self):
        packages = self.root / "packages"
        packages.mkdir()
        module = packages / "qual_local_module.py"
        module.write_text("VALUE = 'pinned'\n")
        self.manifest["runtime"]["files"].append(pin(module))
        self.lock_runtime(packages)
        self.extend_fixture("    import qual_local_module\n    assert qual_local_module.VALUE == 'pinned'")
        self.bind_rights()
        receipt = self.run_qualification("runtime-imports")
        self.assertEqual(receipt["rc"], 0, receipt)
        self.manifest["runtime"]["files"].pop()
        self.bind_rights()
        receipt = self.run_qualification("runtime-lock-drift")
        self.assertEqual(receipt["reason_codes"], ["runtime_lock_identity_mismatch"])
        self.assertEqual(receipt["runs"], [])

    def test_interpreter_alias_swap_keeps_the_original_executable(self):
        alias = self.root / "python"
        alias.symlink_to(sys.executable)
        self.manifest["runtime"]["interpreter"]["path"] = str(alias)
        self.bind_rights()

        def swap_at_launch(*args, **kwargs):
            if args[1] == "candidate":
                alias.unlink()
                alias.symlink_to("/usr/bin/false")
            return run_offline(*args, **kwargs)

        with patch("tools.qualification.harness.run_offline", side_effect=swap_at_launch):
            receipt = self.run_qualification("interpreter-alias-swap")
        self.assertEqual(receipt["reason_codes"], ["pin_changed_during_run"])
        self.assertEqual(receipt["runs"][1]["returncode"], 0)

    def test_candidate_has_kernel_filter(self):
        self.extend_fixture(
            '    assert "Seccomp:\\t2" in Path("/proc/self/status").read_text()'
        )
        self.bind_rights()
        receipt = self.run_qualification("kernel-filter")
        self.assertEqual(receipt["rc"], 0, receipt)

    def test_invalid_clone_flags_are_denied_by_kernel(self):
        # CLONE_THREAD requires CLONE_SIGHAND. Without it the call cannot
        # create a child, even in the absence of the qualification filter.
        self.extend_fixture(
            "    import ctypes\n"
            "    lib = ctypes.CDLL('libseccomp.so.2')\n"
            "    clone = lib.seccomp_syscall_resolve_name(b'clone')\n"
            "    libc = ctypes.CDLL(None, use_errno=True)\n"
            "    assert clone >= 0\n"
            "    value = libc.syscall(clone, 0x00810000, 0, 0, 0, 0)\n"
            "    assert value == -1 and ctypes.get_errno() == errno.EACCES"
        )
        self.bind_rights()
        receipt = self.run_qualification("invalid-clone", retain=True)
        self.assertEqual(receipt["reason_codes"], ["candidate_containment_attempt"], receipt)
        self.assertEqual(receipt["runs"][1]["returncode"], 0)

    def test_candidate_has_read_only_runtime_mounts_and_no_supervisor_fds(self):
        self.extend_fixture(
            "    import os\n"
            "    for name in ('/', '/tmp', '/run', '/proc', '/dev'):\n"
            "        assert os.statvfs(name).f_flag & os.ST_RDONLY, name\n"
            "    assert not os.statvfs('/work').f_flag & os.ST_RDONLY\n"
            "    for name in os.listdir('/proc/self/fd'):\n"
            "        if name in ('0', '1', '2'):\n"
            "            continue\n"
            "        path = '/proc/self/fd/' + name\n"
            "        assert not os.path.lexists(path), os.readlink(path)"
        )
        self.bind_rights()
        receipt = self.run_qualification("readonly-mounts")
        stderr = self.root / "readonly-mounts" / "candidate" / "stderr"
        self.assertEqual(receipt["rc"], 0, stderr.read_text() if stderr.exists() else receipt)

    def test_candidate_cannot_write_outside_workdir(self):
        sentinel = self.root / "outside-work-marker"
        sentinel.write_bytes(b"original")
        self.extend_fixture(
            f"    try:\n        Path({str(sentinel)!r}).write_bytes(b'changed')\n"
            "    except OSError:\n        pass\n"
            "    else:\n        raise AssertionError('outside write allowed')"
        )
        self.bind_rights()
        receipt = self.run_qualification("read-only-outside-work", retain=True)
        self.assertEqual(receipt["rc"], 0, receipt)
        self.assertEqual(sentinel.read_bytes(), b"original")

    def test_pid1_streams_cannot_write_supervisor_logs(self):
        self.extend_fixture(
            "    import os\n"
            "    own_cmdline = Path('/proc/self/cmdline').read_bytes()\n"
            "    print('own PID1 command:', Path('/proc/1/cmdline').read_bytes() == own_cmdline)\n"
            "    print('PID1 streams:', *(os.readlink(f'/proc/1/fd/{fd}') for fd in (0, 1, 2)), flush=True)\n"
            "    for fd in (1, 2):\n"
            "        with open(f'/proc/1/fd/{fd}', 'ab') as stream:\n"
            "            stream.write(f'QUAL candidate PID1 stream {fd}\\n'.encode())"
        )
        self.bind_rights()
        name = "pid1-streams"
        receipt = self.run_qualification(name, retain=True)
        self.assertEqual(receipt["rc"], 0, receipt)
        evidence = Path(os.environ.get("AF_QUAL_FIXTURE_EVIDENCE") or self.root) / name / "candidate"
        for fd, name in ((1, "stdout"), (2, "stderr")):
            with self.subTest(stream=name):
                marker = f"QUAL candidate PID1 stream {fd}\n".encode()
                self.assertNotIn(marker, (evidence / ("supervisor." + name)).read_bytes())
                self.assertIn(marker, (evidence / name).read_bytes())
        stdout = (evidence / "stdout").read_bytes()
        self.assertIn(b"own PID1 command: True\n", stdout)
        self.assertIn(b"PID1 streams: /dev/null /work/.stdout /work/.stderr\n", stdout)

    def test_schema_failure_cannot_pass_on_zero_exit(self):
        self.manifest["output_schema"] = self.write_json("output-schema.json", self.schema("other"))
        self.bind_rights()
        receipt = self.run_qualification("wrong-output")
        self.assertEqual(receipt["rc"], 5, receipt)
        self.assertIn("candidate_output_contract", receipt["reason_codes"])
        self.assertEqual(receipt["runs"][1]["returncode"], 0)

    def test_current_namespace_cannot_witness_offline_isolation(self):
        caller = os.stat("/proc/self/ns/net").st_ino
        with self.assertRaisesRegex(RuntimeError, "offline_namespace_unproven"):
            establish_namespace(caller)

    def test_incomplete_run_or_exceeded_memory_refuses(self):
        run = {
            "process_refusal": "systemd_user_manager_unavailable",
            "cleanup_proven": False, "network_namespace": None, "network_attempts": None,
            "timed_out": False, "process_result": "success",
        }
        self.assertEqual(combined_checks(run, DEFAULT_LIMITS.maximum + 1, 1024, DEFAULT_LIMITS), [
            (3, "systemd_user_manager_unavailable"), (3, "cleanup_unproven"),
            (3, "offline_evidence_unproven"), (6, "memory_budget"),
        ])

    def test_missing_or_invalid_peaks_refuse(self):
        for cgroup, rss in ((None, 100), (100, None), (0, 100), (True, 100)):
            with self.subTest(cgroup=cgroup, rss=rss):
                _, errors = measurements(cgroup, rss, cuda=False, cuda_record=None)
                self.assertTrue(errors)
        _, errors = measurements(100, 50, cuda=True, cuda_record=None)
        self.assertIn("cuda_peak_unavailable", errors)
        _, errors = measurements(100, 50, cuda=True,
                                 cuda_record={"allocated_bytes": 51, "reserved_bytes": 50})
        self.assertIn("cuda_peak_unavailable", errors)

    def test_missing_cuda_bytes_refuse_cpu_fixture_with_cuda_contract(self):
        self.extend_fixture('    (output / "cuda-peaks.json").write_text("{}")')
        self.manifest["runtime"]["cuda"] = True
        self.manifest["outputs"].append("cuda-peaks.json")
        self.bind_rights()
        receipt = self.run_qualification("no-cuda-measurement")
        self.assertEqual(receipt["rc"], 3)
        self.assertEqual(receipt["reason_codes"], ["cuda_peak_unavailable"])
        self.assertEqual(receipt["runs"][1]["returncode"], 0)

    def test_undeclared_output_is_refused(self):
        self.extend_fixture('    (output / "extra.bin").write_bytes(b"undeclared")')
        self.bind_rights()
        receipt = self.run_qualification("undeclared-output")
        self.assertEqual(receipt["reason_codes"], ["candidate_output_contract"])
        self.assertEqual(receipt["runs"][1]["returncode"], 0)

    def test_candidate_cannot_change_pinned_weight(self):
        self.extend_fixture(
            '    try:\n        Path(manifest["weights"][0]["path"]).write_bytes(b"drift")\n'
            '    except OSError:\n        pass\n'
            '    else:\n        raise AssertionError("pinned file writable")'
        )
        self.bind_rights()
        receipt = self.run_qualification("changed-weight-during-run")
        self.assertEqual(receipt["rc"], 0, receipt)
        self.assertEqual(core.sha256_file(Path(self.manifest["weights"][0]["path"])),
                         self.manifest["weights"][0]["sha256"])

    def extend_fixture(self, line):
        code = FIXTURE.read_text().replace('\n\nif __name__', '\n' + line + '\n\n\nif __name__')
        self.fixture.write_text(code)
        self.manifest["code"]["entrypoint"] = pin(self.fixture)
        self.manifest["code"]["revision"] = core.sha256_file(self.fixture)

    def test_cleanup_after_runtime_limit(self):
        self.fixture.write_text("import time\ntime.sleep(30)\n")
        self.manifest["code"]["entrypoint"] = pin(self.fixture)
        self.manifest["code"]["revision"] = core.sha256_file(self.fixture)
        self.bind_rights()
        receipt = self.run_qualification("timeout", timeout_s=2)
        self.assertEqual(receipt["rc"], 6, receipt)
        self.assertIn("candidate_time_budget", receipt["reason_codes"])
        self.assertTrue(receipt["runs"][1]["cleanup_proven"])

    def test_existing_evidence_is_preserved(self):
        evidence = self.root / "existing"
        evidence.mkdir()
        sentinel = evidence / "qualification.json"
        sentinel.write_bytes(b"first attempt\n")
        with self.assertRaises(FileExistsError):
            qualify(self.manifest, evidence, timeout_s=15)
        self.assertEqual(sentinel.read_bytes(), b"first attempt\n")
