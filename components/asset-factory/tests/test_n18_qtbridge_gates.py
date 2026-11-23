"""Focused controls for native GPU capability and job evidence."""

from pathlib import Path
import os
import tempfile
import unittest

from tools.qualification import offline, sandbox
from tools.qualification.manifest import Refusal
from tools.qualification import gpu


class GpuBridgeTests(unittest.TestCase):
    def test_device_node_refuses_regular_file_and_substituted_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'nvidia0'
            path.write_bytes(b'ordinary file')
            with self.assertRaisesRegex(OSError, 'gpu_device_not_character'):
                sandbox.device_identity(path)
        node = Path('/dev/null')
        expected = sandbox.device_identity(node)
        self.assertEqual(expected['major'], os.major(node.stat().st_rdev))
        expected['inode'] += 1
        with self.assertRaisesRegex(OSError, 'gpu_device_changed'):
            sandbox.open_device(node, expected)

    def test_device_policy_rejects_extra_device_and_duplicate(self):
        devices = [{**sandbox.device_identity(Path('/dev/null')), 'path': name}
                   for name in sandbox.GPU_DEVICE_PATHS]
        self.assertEqual(sandbox.validate_devices(devices), devices)
        for forged in (devices[:-1], [*devices, devices[0]],
                       [*devices[:-1], {**devices[-1], 'path': '/dev/nvidia-modeset'}]):
            with self.assertRaisesRegex(OSError, 'gpu_device_policy_invalid'):
                sandbox.validate_devices(forged)

    def test_scope_rejects_wrong_unit_caps_affinity_or_nice(self):
        unit = 'af-job-' + 'f' * 32 + '.service'
        scope = {'cgroup_path': '/user.slice/user-1000.slice/user@1000.service/app.slice/' + unit,
                 'limits': gpu.SCOPE_LIMITS, 'cpu_affinity': list(range(16)), 'nice': 15,
                 'cpuset_cpus_effective': '0-15', 'pid': 20, 'start_ticks': 100}
        self.assertEqual(offline.validate_gpu_scope(scope, unit), scope)
        for changed in ({'cgroup_path': '/app.slice/' + unit + '-other'},
                        {'limits': {**gpu.SCOPE_LIMITS, 'swap': 1}},
                        {'limits': {**gpu.SCOPE_LIMITS, 'maximum': 23 * 1024**3}},
                        {'cpu_affinity': [0]}, {'nice': 14}):
            with self.assertRaisesRegex(RuntimeError, 'gpu_scope_unproven'):
                offline.validate_gpu_scope({**scope, **changed}, unit)

    def test_gpu_receipt_refuses_cpu_peak_scope_or_stale_attestation(self):
        unit = 'af-job-' + 'f' * 32 + '.service'
        run = {'unit_name': unit, 'cgroup_peak_source': 'systemd_job', 'cleanup_proven': True,
               'cgroup_peak_bytes': 12 * 1024**3}
        scope = {'cgroup_path': '/app.slice/' + unit, 'limits': gpu.SCOPE_LIMITS,
                 'cpu_affinity': list(range(16)), 'nice': 15,
                 'cpuset_cpus_effective': '0-15', 'pid': 20, 'start_ticks': 100}
        candidate = 'a' * 64
        handoff = {'schema': gpu.AUTHORITY_SCHEMA, 'candidate_sha256': candidate, 'unit': unit,
                    'permit_sha256': 'b' * 64, 'input_sha256': 'c' * 64, 'seed': 42,
                    'admitted_monotonic_ns': 1, 'boot_id': 'fixture', 'gpu_uuid': 'fixture',
                    'authorized_monotonic_ns': 2, 'expires_monotonic_ns': 1 + offline.MAX_HANDOFF_AGE_NS,
                    'peer_pid': 20, 'peer_start_ticks': 100}
        self.assertEqual(gpu.validate_handoff(handoff, scope, unit, candidate), handoff)
        gpu.validate_job_peak(run, scope)
        with self.assertRaisesRegex(Refusal, 'gpu_authority_binding_changed'):
            gpu.validate_handoff({**handoff, 'candidate_sha256': 'f' * 64}, scope, unit, candidate)
        for altered in ({'cleanup_proven': False}, {'cgroup_peak_source': 'pidns_test_scope'},
                        {'cgroup_peak_bytes': 25 * 1024**3}):
            with self.assertRaisesRegex(Refusal, 'gpu_job_supervision_unproven'):
                gpu.validate_job_peak({**run, **altered}, scope)

    def test_authority_release_requires_idle_gpu_and_exact_permit(self):
        unit = 'af-job-' + 'f' * 32 + '.service'
        handoff = {'permit_sha256': 'b' * 64, 'gpu_uuid': 'fixture'}
        release = {'unit': unit, 'permit_sha256': 'b' * 64,
                   'compute_idle': True, 'fence_removed': True, 'gpu_uuid': 'fixture'}
        gpu.validate_release(release, unit, handoff)
        for changed in ({'permit_sha256': 'a' * 64}, {'compute_idle': False}, {'fence_removed': False}):
            with self.assertRaisesRegex(Refusal, 'gpu_release_unproven'):
                gpu.validate_release({**release, **changed}, unit, handoff)

    @unittest.skipUnless(all(Path(path).is_char_device() for path in sandbox.GPU_DEVICE_PATHS),
                         "NVIDIA device metadata unavailable")
    def test_device_and_token_mounts_are_exact_without_starting_cuda(self):
        import json
        import shutil
        import sys
        import time

        from asset_factory import core, proc
        from providers.trellis2 import admission
        from tools.qualification.harness import DEFAULT_LIMITS

        expected = [sandbox.device_identity(Path(path)) for path in sandbox.GPU_DEVICE_PATHS]
        destination = Path(sandbox.__file__)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            work = folder / 'work'
            work.mkdir()
            token = folder / 'authority'
            now = time.monotonic_ns()
            handoff = {'boot_id': admission.boot_id(), 'authorized_monotonic_ns': now,
                       'expires_monotonic_ns': now + offline.MAX_HANDOFF_AGE_NS}
            core.atomic_write_json(token, handoff)
            metadata = destination.stat()
            source = (
                "import json, os, pathlib; "
                "assert sorted(p.name for p in pathlib.Path('/dev').glob('nvidia*')) == "
                "['nvidia-uvm', 'nvidia0', 'nvidiactl']; "
                "p = pathlib.Path('/run/af-native/qualification.json'); "
                f"assert json.loads(p.read_bytes()) == {handoff!r}; "
                "assert os.statvfs(p).f_flag & os.ST_RDONLY; "
                "assert sorted(os.listdir('/proc/self/fd')) == ['0', '1', '2', '3']; "
                "assert not p.with_name('authority.sock').exists(); print('device-metadata-and-token-only')"
            )
            config = folder / 'launcher.json'
            command = [sys.executable, '-I', '-B', '-c', source]
            config.write_text(json.dumps({
                'work': str(work), 'mount_info': str(folder / 'mount.json'), 'runtime_dir': None,
                'bindings': [[str(destination), str(destination), metadata.st_dev, metadata.st_ino,
                              metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns]],
                'command': command, 'bwrap': shutil.which('bwrap'), 'python': sys.executable,
                'launcher': str(destination), 'seccomp_library': str(sandbox.seccomp_library_path()),
                'gpu_devices': expected, 'gpu_witness': str(folder / 'gpu-devices.json'),
                'gpu_authority': str(token),
            }))
            job = proc.popen([sys.executable, '-I', '-S', '-B', destination, config],
                             start_new_session=True, memory_limits=DEFAULT_LIMITS, runtime_s=15)
            try:
                rc = job.wait(timeout=15)
                candidate_stderr = work / '.stderr'
                self.assertEqual(
                    rc, 0, job.stderr_path.read_text() +
                    (candidate_stderr.read_text() if candidate_stderr.is_file() else ''))
                self.assertEqual((work / '.stdout').read_text(), 'device-metadata-and-token-only\n')
                self.assertEqual(json.loads((folder / 'gpu-devices.json').read_text()), expected)
                witness = json.loads(job.stdout_path.read_text())
                self.assertEqual(witness['mounted_devices'], expected)
                self.assertEqual(witness['authority_sha256'], core.sha256_file(token))
                self.assertLessEqual(now, witness['executed_monotonic_ns'])
                self.assertLessEqual(witness['executed_monotonic_ns'], handoff['expires_monotonic_ns'])
            finally:
                self.assertEqual(proc.cleanup(job), [])

    def test_permit_evidence_refuses_substituted_input_or_scheduler_timestamp(self):
        from asset_factory import core

        permit = {'schema': 'af.trellis-native-permit/v1', 'input_sha256': 'c' * 64,
                  'seed': 42, 'pipeline_type': '512',
                  'scheduler': {'admitted_monotonic_ns': 1, 'boot_id': 'fixture'}}
        handoff = {'permit_sha256': core.sha256_json(permit), 'input_sha256': 'c' * 64,
                   'seed': 42, 'admitted_monotonic_ns': 1, 'boot_id': 'fixture'}
        gpu.validate_permit_evidence(permit, handoff)
        for changed in ({'input_sha256': 'e' * 64}, {'admitted_monotonic_ns': 2}, {'seed': 43}):
            with self.assertRaisesRegex(Refusal, 'gpu_permit_binding_changed'):
                gpu.validate_permit_evidence(permit, {**handoff, **changed})

    def test_authority_redeems_only_the_owned_peer_and_refuses_replay(self):
        import sys
        import time

        from asset_factory import core, proc
        from providers.trellis2 import admission
        from providers.trellis2.qualification import supervisor
        from tools.qualification.harness import DEFAULT_LIMITS

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with admission.Lease(root / 'lock') as lease:
                permit = {'scheduler': {'lease': lease.evidence(), 'boot_id': admission.boot_id(),
                                        'admitted_monotonic_ns': time.monotonic_ns(),
                                        'measured_peak_bytes': 1, 'min_free_bytes': 1}}
                digest = core.sha256_json(permit)
                script = """
import os, sys, time
from pathlib import Path
from tools.qualification import offline
root, digest = Path(sys.argv[1]), sys.argv[2]
deadline = time.monotonic() + 10
with open(root / 'ready.pending', 'x') as stream:
    stream.write(str(os.getpid()))
(root / 'ready.pending').replace(root / 'ready')
offline.wait_json(root / 'go.json', deadline)
offline.redeem_authority(str(root / 'authority.sock'), digest, deadline)
try:
    offline.redeem_authority(str(root / 'authority.sock'), digest, deadline)
except RuntimeError as exc:
    assert str(exc) == 'permit_replayed', str(exc)
else:
    raise AssertionError('redemption replay accepted')
"""
                job = proc.popen([sys.executable, '-B', '-c', script, root, digest],
                                 start_new_session=True, memory_limits=DEFAULT_LIMITS, runtime_s=15)
                try:
                    deadline = time.monotonic() + 10
                    pid = offline.wait_json(root / 'ready', deadline)
                    cgroup, ticks = offline.read_peer_identity(pid)
                    binding = {'pid': pid, 'cgroup_path': cgroup, 'start_ticks': ticks}
                    with admission.Authority(root, permit, lease, lambda p: supervisor.peer_matches(p, binding)) as issuer:
                        with self.assertRaisesRegex(RuntimeError, 'runtime_peer_unproven'):
                            offline.redeem_authority(str(issuer.path), digest, deadline)
                        self.assertFalse(issuer.used)
                        core.atomic_write_json(root / 'go.json', {})
                        self.assertEqual(job.wait(timeout=10), 0, job.stderr_path.read_text())
                        self.assertTrue(issuer.used)
                finally:
                    self.assertEqual(proc.cleanup(job), [])
                self.assertFalse((root / 'authority.sock').exists())

    def test_gpu_wait_does_not_leave_a_job_running_after_fetch_attempt(self):
        import sys
        import time

        from asset_factory import proc
        from tools.qualification.harness import DEFAULT_LIMITS, wait_gpu

        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / 'trace'
            trace.write_text('12 execve(0x0, 0x0, 0x0) = 0\n12 connect(0x3, 0x0, 0x0) = -1 EACCES\n')
            job = proc.popen([sys.executable, '-c', 'import time; time.sleep(8)'],
                             start_new_session=True, memory_limits=DEFAULT_LIMITS, runtime_s=10)
            try:
                with self.assertRaisesRegex(Refusal, 'candidate_network_attempt'):
                    wait_gpu(job, trace, time.monotonic() + 5, 5)
            finally:
                self.assertEqual(proc.cleanup(job), [])
            self.assertIsNotNone(job.result)
            self.assertNotEqual(job.returncode, 0)
