"""Repair-round controls for the GPU handoff and the common CPU boundary."""

import errno
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from asset_factory import core, proc
from providers.trellis2 import admission
from providers.trellis2.qualification import supervisor
from systemd_fake import FakeRunner
from tools.qualification import gpu, offline, sandbox
from tools.qualification.harness import DEFAULT_LIMITS, infrastructure, run_offline
from tools.qualification.manifest import Pins, Refusal


UNIT = "af-job-" + "f" * 32 + ".service"


def scope(pid=20, ticks=43):
    return {"cgroup_path": "/app.slice/" + UNIT, "limits": gpu.SCOPE_LIMITS,
            "cpu_affinity": list(range(16)), "cpuset_cpus_effective": "0-15",
            "nice": 15, "pid": pid, "start_ticks": ticks}


class BridgeRepair(unittest.TestCase):
    def test_f1_peer_in_another_cgroup_is_refused(self):
        with patch.object(offline, "read_peer_identity", create=True) as observed:
            binding = scope()
            observed.return_value = (binding["cgroup_path"], binding["start_ticks"])
            self.assertTrue(supervisor.peer_matches(binding["pid"], binding))
            self.assertFalse(supervisor.peer_matches(binding["pid"] + 1, binding))
            observed.return_value = ("/elsewhere", binding["start_ticks"])
            self.assertFalse(supervisor.peer_matches(binding["pid"], binding))
            observed.return_value = (binding["cgroup_path"], binding["start_ticks"] + 1)
            self.assertFalse(supervisor.peer_matches(binding["pid"], binding))

    def test_f1_main_pid_and_cgroup_are_checked_on_the_owned_job(self):
        runner = FakeRunner()
        job = proc.popen(["/usr/bin/true"], start_new_session=True, runner=runner,
                         memory_limits=DEFAULT_LIMITS, env={})
        self.addCleanup(proc.cleanup, job)
        binding = {**scope(), "cgroup_path": "/app.slice/" + job.unit_name}
        state = runner.units[job.unit_name]
        state.update(MainPID=str(binding["pid"]), ControlGroup=binding["cgroup_path"])
        with patch.object(offline, "read_peer_identity",
                          return_value=(binding["cgroup_path"], binding["start_ticks"])):
            supervisor.verify_main_peer(job, binding, time.monotonic() + 5)
            for key, value in (("MainPID", "21"), ("ControlGroup", "/another"), ("SubState", "exited")):
                with self.subTest(key=key), patch.dict(state, {key: value}):
                    with self.assertRaisesRegex(Refusal, "runtime_peer_unproven"):
                        supervisor.verify_main_peer(job, binding, time.monotonic() + 5)

    def test_f1_authority_runtime_rejects_symlink_and_shared_permissions(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"XDG_RUNTIME_DIR": temp}):
            self.assertEqual(supervisor.private_runtime_dir(), Path(temp) / "af-native")
            private = Path(temp) / "af-native"
            private.chmod(0o750)
            with self.assertRaisesRegex(Refusal, "runtime_authority_path_untrusted"):
                supervisor.private_runtime_dir()
            private.rmdir()
            private.symlink_to(temp, target_is_directory=True)
            with self.assertRaisesRegex(Refusal, "runtime_authority_path_untrusted"):
                supervisor.private_runtime_dir()

    def test_f2_transient_unit_enforces_cpuset(self):
        runner = FakeRunner()
        job = proc.popen(["/usr/bin/true"], start_new_session=True, runner=runner,
                         memory_limits=DEFAULT_LIMITS, env={})
        self.addCleanup(proc.cleanup, job)
        launch = next(call for call in runner.calls if call[0] == "systemd-run")
        self.assertIn("--property=AllowedCPUs=0-15", launch)

    def test_f2_scope_refuses_a_widened_effective_cpuset(self):
        witness = scope()
        self.assertEqual(offline.validate_gpu_scope(witness, UNIT), witness)
        with self.assertRaisesRegex(RuntimeError, "gpu_scope_unproven"):
            offline.validate_gpu_scope({**witness, "cpuset_cpus_effective": "0-19"}, UNIT)

    def test_f2_candidate_cannot_change_its_affinity(self):
        # A request for the unchanged mask safely distinguishes the filter
        # before exercising a wider request on the repaired boundary.
        script = (
            "import os\n"
            "before = os.sched_getaffinity(0)\n"
            "for requested in (before, set(range(os.cpu_count()))):\n"
            " try:\n  os.sched_setaffinity(0, requested)\n"
            f" except OSError as exc:\n  assert exc.errno == {errno.EACCES}\n"
            " else:\n  raise AssertionError('affinity changes are permitted')\n"
            " assert os.sched_getaffinity(0) == before\n"
        )
        pins = Pins()
        tools, _ = infrastructure(pins)
        fixtures = {tools["supervisor_python"], tools["supervisor_script"], tools["sandbox_launcher"]}
        bindings = [[str(path), str(pins.files[path][1][0]), *pins.files[path][1][1:]]
                    for path in sorted(fixtures)]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run, _, _ = run_offline(
                root, "candidate", [sys.executable, "-I", "-B", "-c", script], tools, bindings,
                os.stat("/proc/self/ns/net").st_ino, os.stat("/proc/self/ns/mnt").st_ino,
                10, DEFAULT_LIMITS, cuda=False)
            stderr = root / "candidate" / "stderr"
            self.assertEqual(run["returncode"], 0, stderr.read_text() if stderr.exists() else run)
            self.assertTrue(run["cleanup_proven"])

    def test_f3_device_comparison_uses_both_independent_witnesses(self):
        expected = [{**sandbox.device_identity(Path("/dev/null")), "path": name}
                    for name in sandbox.GPU_DEVICE_PATHS]
        changed = [{**expected[0], "inode": expected[0]["inode"] + 1}, *expected[1:]]
        gpu.validate_device_witnesses(expected, expected, expected)
        for observed in ((changed, expected), (expected, changed)):
            with self.assertRaisesRegex(Refusal, "gpu_device_witness_changed"):
                gpu.validate_device_witnesses(expected, *observed)

    def test_f4_reobserved_scope_must_match_initial_scope(self):
        original = scope()
        self.assertEqual(gpu.validate_reobserved_scope(original, original, UNIT), original)
        with self.assertRaisesRegex(Refusal, "gpu_scope_changed"):
            gpu.validate_reobserved_scope(original, {**original, "start_ticks": 44}, UNIT)

    def test_f4_waiter_checks_scope_after_handoff_before_exec(self):
        original = scope()
        changed = {**original, "start_ticks": 44}
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            with (
                patch.object(offline, "gpu_scope", side_effect=[original, changed]),
                patch.object(offline, "redeem_authority"),
                patch.object(offline.os, "execv") as execute,
            ):
                core.atomic_write_json(folder / "gpu-challenge.json", {
                    "socket": "/fixture", "permit_sha256": "a" * 64,
                })
                core.atomic_write_json(folder / "gpu-authority.json", {"permit_sha256": "a" * 64})
                with self.assertRaisesRegex(RuntimeError, "gpu_scope_changed"):
                    offline.start_gpu_scope(folder, 5, ["/usr/bin/true"])
                execute.assert_not_called()
                self.assertFalse((folder / "gpu-scope-exec.json").exists())

    def test_f5_sandbox_exec_must_be_after_authorization_and_before_expiry(self):
        start = 100
        end = start + admission.MAX_ADMISSION_AGE_NS
        handoff = {"admitted_monotonic_ns": start, "authorized_monotonic_ns": start + 1,
                   "expires_monotonic_ns": end}
        gpu.validate_exec_freshness(handoff, start + 1)
        gpu.validate_exec_freshness(handoff, end)
        for observed in (True, start, end + 1):
            with self.assertRaisesRegex(Refusal, "gpu_exec_stale"):
                gpu.validate_exec_freshness(handoff, observed)

    def test_f5_expired_handoff_refuses_before_sandbox_exec(self):
        now = time.monotonic_ns()
        entry = ({"boot_id": "fixture"}, {"boot_id": "fixture",
                 "authorized_monotonic_ns": now - 2, "expires_monotonic_ns": now - 1})
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(sandbox, "WORK", Path(temp)), patch.object(sandbox.os, "execv") as execute:
                with self.assertRaisesRegex(RuntimeError, "gpu_exec_stale"):
                    sandbox.enter(["/usr/bin/true"], gpu_entry=entry)
                execute.assert_not_called()

    def test_reconciler_consumes_scope_device_and_exec_witnesses(self):
        devices = [{**sandbox.device_identity(Path("/dev/null")), "path": name}
                   for name in sandbox.GPU_DEVICE_PATHS]
        policy = {"devices": devices, "devices_sha256": core.sha256_json(devices), "gpu_uuid": "fixture"}
        candidate = "a" * 64
        start = 100
        permit = {"schema": "af.trellis-native-permit/v1", "input_sha256": "b" * 64,
                  "seed": 42, "pipeline_type": "512",
                  "scheduler": {"admitted_monotonic_ns": start, "boot_id": "fixture"}}
        handoff = {"schema": gpu.AUTHORITY_SCHEMA, "candidate_sha256": candidate, "unit": UNIT,
                   "permit_sha256": core.sha256_json(permit), "input_sha256": permit["input_sha256"],
                   "seed": permit["seed"], "admitted_monotonic_ns": start, "boot_id": "fixture",
                   "gpu_uuid": "fixture", "peer_pid": scope()["pid"], "peer_start_ticks": scope()["start_ticks"],
                   "authorized_monotonic_ns": start + 1, "expires_monotonic_ns": start + offline.MAX_HANDOFF_AGE_NS}
        entry = {"mounted_devices": devices, "authority_sha256": core.sha256_json(handoff),
                 "boot_id": "fixture", "executed_monotonic_ns": start + 2}
        run = {"unit_name": UNIT, "cgroup_peak_source": "systemd_job",
               "cleanup_proven": True, "cgroup_peak_bytes": 1}
        evidence = {
            "gpu-scope.json": scope(), "gpu-scope-exec.json": scope(), "gpu-devices.json": devices,
            "gpu-authority.json": handoff, "gpu-permit.json": permit, "supervisor.stdout": entry,
            "gpu-release.json": {"unit": UNIT, "permit_sha256": handoff["permit_sha256"],
                                 "fence_removed": True, "compute_idle": True, "gpu_uuid": "fixture"},
        }
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for name, value in evidence.items():
                core.atomic_write_json(folder / name, value)
            gpu.reconcile_gpu_evidence(folder, run, policy, candidate)
            for name, value, reason in (
                ("gpu-scope-exec.json", {**scope(), "start_ticks": 44}, "gpu_scope_changed"),
                ("supervisor.stdout", {**entry, "mounted_devices": []}, "gpu_device_witness_changed"),
                ("supervisor.stdout", {**entry, "boot_id": "another-boot"}, "gpu_authority_binding_changed"),
                ("supervisor.stdout", {**entry, "executed_monotonic_ns": handoff["expires_monotonic_ns"] + 1},
                 "gpu_exec_stale"),
            ):
                with self.subTest(reason=reason):
                    core.atomic_write_json(folder / name, value)
                    with self.assertRaisesRegex(Refusal, reason):
                        gpu.reconcile_gpu_evidence(folder, run, policy, candidate)
                    core.atomic_write_json(folder / name, evidence[name])
