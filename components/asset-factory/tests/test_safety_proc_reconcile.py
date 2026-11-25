"""Retained entry points accept only service handles; no process retrofit."""
import signal
import subprocess
import unittest
from unittest import mock
from asset_factory import proc
from systemd_fake import FakeRunner
from test_safety_proc import launch

class Reconcile(unittest.TestCase):
    def test_old_registration_and_signalling_names_accept_job_only(self):
        runner = FakeRunner()
        job = launch(runner)
        self.assertIs(proc.register_group(job, start_new_session=True), job)
        self.assertIs(proc.register_pid(job), job)
        proc.signal_pid(job, 0)
        self.assertEqual(proc.signal_group(job, signal.SIGKILL), -signal.SIGKILL)
        with self.assertRaises(ProcessLookupError):
            proc.signal_pid(job, 0)

    def test_multiprocessing_and_bare_child_adoption_are_refused(self):
        for method in (proc.start_process, proc.register_child, proc.reap_child):
            with self.assertRaises(proc.SignalRefusal):
                method(mock.MagicMock())

    def test_explicit_context_injection_does_not_leak(self):
        fake = FakeRunner(auto_exit=0)
        with proc.using_runner(fake):
            job = proc.popen(['/usr/bin/true'], start_new_session=True,
                            memory_limits=proc.MemoryLimits(1024, 2048, 0))
            self.assertEqual(job.wait(1), 0)
        self.assertIsNone(proc._RUNNER.get())

    def test_memory_scope_becomes_service_properties(self):
        fake = FakeRunner(auto_exit=0)
        job = proc.popen(['systemd-run','--user','--scope','-q','-p','MemoryHigh=6G',
            '-p','MemoryMax=8G','-p','MemorySwapMax=0','--','/usr/bin/true'],
            start_new_session=True, runner=fake)
        self.assertEqual(job.wait(1), 0)
        command = next(c for c in fake.calls if c[0] == 'systemd-run')
        self.assertIn('--property=MemoryMax=8589934592', command)
        self.assertEqual(command.count('systemd-run'), 1)

    def test_unsupported_spawn_features_refuse_before_launch(self):
        for options in ({'pass_fds':(123,)}, {'preexec_fn':lambda: None}, {'shell':True}):
            fake = FakeRunner()
            with self.assertRaises(proc.SignalRefusal):
                proc.popen(['/usr/bin/true'], start_new_session=True, runner=fake, **options)
            self.assertEqual(fake.calls, [])

    def test_memory_is_required_not_defaulted(self):
        for values in ((0,2048,0),(True,2048,0),(4096,2048,0),(1024,2048,1)):
            with self.assertRaises(proc.SignalRefusal):
                proc.MemoryLimits(*values)

class ExternalSupervisor(unittest.TestCase):
    def test_adopted_adapter_descendant_is_refused_and_reaped(self):
        import json
        from pathlib import Path
        import sys
        import tempfile
        import time

        component = Path(proc.__file__).resolve().parents[1]
        supervisor = component / 'viewpoint/external_control/supervisor.py'
        adapter = (
            'from pathlib import Path; import subprocess, sys; '
            'sys.stdin.read(); '
            'child = subprocess.Popen([sys.executable, "-I", "-c", '
            '"import time; time.sleep(30)"], start_new_session=True); '
            'Path("descendant.pid").write_text(str(child.pid)); print("{}")'
        )
        with tempfile.TemporaryDirectory() as directory:
            request = {'argv': [sys.executable, '-I', '-c', adapter], 'message': {},
                       'expires_ns': time.monotonic_ns() + 5_000_000_000}
            response = subprocess.run([sys.executable, '-I', '-B', str(supervisor)],
                                      input=json.dumps(request), text=True, capture_output=True,
                                      cwd=directory, timeout=8, check=True)
            self.assertEqual(json.loads(response.stdout), {'error': 'ADAPTER_DESCENDANTS'})
            pid = int((Path(directory) / 'descendant.pid').read_text())
            self.assertFalse(Path(f'/proc/{pid}').exists(), response.stderr)


class PinBinding(unittest.TestCase):
    def test_proc_source_is_in_texture_revision(self):
        from pathlib import Path
        from asset_factory import core
        from tools.blender import texture_project as texture
        original = texture.read_bytes
        before = texture.tool_snapshot()
        guard = Path(proc.__file__)
        self.assertIn({'name': 'proc.py', 'sha256': core.sha256_file(guard)}, before)
        def changed(path):
            data = original(path)
            return data + b'\n# pin mutation\n' if Path(path) == guard else data
        with mock.patch.object(texture, 'read_bytes', side_effect=changed):
            after = texture.tool_snapshot()
        self.assertNotEqual(core.sha256_json(before), core.sha256_json(after))

    def test_namespace_backend_source_is_in_texture_revision(self):
        from pathlib import Path
        from asset_factory import core, proc_pidns
        from tools.blender import texture_project as texture
        original = texture.read_bytes
        before = texture.tool_snapshot()
        guard = Path(proc_pidns.__file__)
        self.assertIn({'name': 'proc_pidns.py', 'sha256': core.sha256_file(guard)}, before)
        def changed(path):
            return original(path) + (b'\n# pin mutation\n' if Path(path) == guard else b'')
        with mock.patch.object(texture, 'read_bytes', side_effect=changed):
            self.assertNotEqual(core.sha256_json(before), core.sha256_json(texture.tool_snapshot()))

class CallerBoundary(unittest.TestCase):
    def test_build_and_topology_use_only_fake_services(self):
        from providers.trellis2.build import build
        from tools.blender import topology
        fake = FakeRunner(auto_exit=0, output=b'fixture\n')
        with proc.using_runner(fake):
            self.assertEqual(build.run_owned(['/usr/bin/true']), b'fixture\n')
            self.assertEqual(topology.default_runner(['/usr/bin/true'], 1), (0, 'fixture\n'))
        self.assertEqual(fake.units, {})

    def test_gate_preserves_declared_rc_translation(self):
        from tools import gates
        for exit_code, text, expected in ((0, b'Ran 3 tests in 0.001s\nOK\n', 0),
                (1, b'Ran 3 tests in 0.001s\nFAILED (failures=1)\n', 5)):
            fake = FakeRunner(auto_exit=exit_code, error=text)
            with proc.using_runner(fake):
                row = gates.run_leg('F0-U1', ['/usr/bin/true'], 1)
            self.assertEqual(row['rc'], expected, row)
            self.assertEqual(row['checks_evaluated'], 3)
            self.assertEqual(fake.units, {})

    def test_missing_manager_is_cannot_see_in_gate_not_pass(self):
        from tools import gates
        fake = FakeRunner(available=False)
        with proc.using_runner(fake):
            row = gates.run_leg('F0-U1', ['/usr/bin/true'], 1)
        self.assertEqual(row['rc'], 3)
        self.assertEqual(row['checks_evaluated'], 0)
        self.assertEqual(row['failures'], ['systemd_user_manager_unavailable'])

    def test_texture_and_physics_use_service_disposal(self):
        from pathlib import Path
        import tempfile
        from tools.blender import texture_project as texture
        from tools.physics import api as physics
        fake = FakeRunner(auto_exit=0, output=b'fixture')
        with tempfile.TemporaryDirectory() as d, proc.using_runner(fake):
            self.assertEqual(texture.scoped_run(['/usr/bin/true'], 1, Path(d)/'log'), 0)
            self.assertEqual(physics.run_child(['/usr/bin/true'], timeout_s=1, cwd=d), (0, b'fixture'))
        self.assertEqual(fake.units, {})

    def test_input_gate_uses_snapshot_input_and_result_status(self):
        from gates import input_gate
        fake = FakeRunner(auto_exit=0, output=b'{}\n')
        with proc.using_runner(fake):
            result = input_gate._run_owned_worker({'test':'snapshot'}, 1)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '{}\n')
        self.assertEqual(fake.units, {})

    def test_internal_comparator_deadline_does_not_require_a_process(self):
        import time
        from tools.blender import topology
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            topology._bounded_compare(lambda *a, **k: time.sleep(10), None, None, {}, .02)
        self.assertLess(time.monotonic()-started, 1)
