"""P7 reliability controls; all disputed delivery is intercepted before syscall."""
import gc
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from asset_factory import proc, proc_pidns
from systemd_fake import FakeRunner
from test_safety_proc import launch


class Reliability(unittest.TestCase):
    def test_P7_OWN_valid_shaped_retarget_never_controls_another_unit(self):
        runner = FakeRunner()
        job = launch(runner)
        original = job.unit_name
        other = 'af-job-' + 'c'*32 + '.service'
        runner.units[other] = dict(runner.units[original])
        job._unit_name = other
        before = len(runner.calls)
        try:
            with self.assertRaises(proc.SignalRefusal):
                job.stop()
            self.assertEqual(len(runner.calls), before)
            self.assertIn(original, runner.units)
            self.assertIn(other, runner.units)
        finally:
            job._unit_name = original
            job.stop()

    def test_P7_OWN_controls_require_a_created_unit_not_just_a_name(self):
        runner = FakeRunner()
        for action in ('kill', 'stop', 'reset-failed'):
            with self.subTest(action=action):
                with self.assertRaises(proc.SignalRefusal):
                    proc._unit_call(runner, action, 'af-job-'+'d'*32+'.service')
        self.assertEqual(runner.calls, [])

    def test_P7_RESULT_service_failure_never_becomes_zero(self):
        for value in ('oom-kill', 'resources', 'timeout', 'signal', 'core-dump', 'exit-code'):
            with self.subTest(result=value):
                runner = FakeRunner(auto_exit=0)
                job = launch(runner)
                runner.units[job.unit_name]['Result'] = value
                try:
                    self.assertNotEqual(job.wait(1), 0)
                    self.assertEqual(job.result.main_returncode, 0)
                    self.assertEqual(job.result.reason, 'job_'+value.replace('-', '_'))
                    self.assertEqual(job.result.systemd_result, value)
                finally:
                    job.stop()
                self.assertEqual(runner.units, {})

    def test_P7_RESULT_inconsistent_success_is_failure(self):
        runner = FakeRunner(auto_exit=7)
        job = launch(runner)
        runner.units[job.unit_name]['Result'] = 'success'
        self.assertNotEqual(job.wait(1), 0)
        self.assertEqual(job.result.reason, 'job_result_inconsistent')
        self.assertEqual(job.result.main_returncode, 7)

    def test_P7_RESULT_returncode_only_callers_receive_service_failure(self):
        from tools.blender import texture_project
        from providers.trellis2.build import process_guard
        class Failed(FakeRunner):
            def run(self, argv, *, timeout):
                reply = super().run(argv, timeout=timeout)
                if argv[0] == 'systemd-run':
                    for state in self.units.values():
                        state['Result'] = 'oom-kill'
                return reply
        import tempfile
        with tempfile.TemporaryDirectory() as temp, proc.using_runner(Failed(auto_exit=0)):
            self.assertNotEqual(texture_project.scoped_run(['/usr/bin/true'], 1,
                                Path(temp)/'log'), 0)
            observation = process_guard.supervise(['/usr/bin/true'], 1)
            self.assertNotEqual(observation['returncode'], 0)
            self.assertEqual(observation['rc'], 6)
            self.assertEqual(observation['reason'], 'job_oom_kill')

    def test_P7_RESULT_glb_capture_receives_typed_service_failure(self):
        from tools.blender import glb_capture
        from tools.blender.helpers_contracts import GlbError
        class OomRunner(FakeRunner):
            def run(self, argv, *, timeout):
                reply = super().run(argv, timeout=timeout)
                if argv[0] == 'systemd-run':
                    for state in self.units.values():
                        state['Result'] = 'oom-kill'
                return reply
        runner = OomRunner(auto_exit=0)
        with proc.using_runner(runner):
            with self.assertRaises(GlbError) as caught:
                glb_capture.run_owned(['/usr/bin/true'], timeout_s=1, cwd=Path.cwd())
        self.assertEqual(caught.exception.code, 'job_oom_kill')
        self.assertEqual(runner.units, {})

    def test_P7_FAILED_pre_payload_failures_are_retired(self):
        runner = FakeRunner()
        for _ in range(32):
            job = launch(runner)
            runner.units[job.unit_name].update(ActiveState='failed', SubState='failed',
                Result='resources', ExecMainCode='0', ExecMainStatus='0')
            self.assertNotEqual(job.wait(1), 0)
            self.assertIsNone(job.result.main_returncode)
            self.assertEqual(job.result.reason, 'job_resources')
            self.assertEqual(job.stop(), job.returncode)
        self.assertEqual(runner.units, {})

    def test_P7_FAILED_unknown_exit_still_retires_and_keeps_refusal(self):
        runner = FakeRunner(auto_exit=0)
        job = launch(runner)
        runner.units[job.unit_name]['ExecMainCode'] = '0'
        for _ in range(2):
            with self.assertRaises(proc.SignalRefusal) as caught:
                job.wait(1)
            self.assertEqual(caught.exception.code, 'job_result_unproven')
            self.assertIsNone(job.returncode)
            self.assertEqual(runner.units, {})

    def test_P7_FORK_child_finalizers_preserve_parent_evidence(self):
        job = launch(FakeRunner(auto_exit=0, output=b'parent evidence'))
        scratch = Path(job._scratch.name)
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            try:
                try:
                    job.stop()
                except proc.SignalRefusal as exc:
                    reason = exc.code
                else:
                    reason = 'unexpected_success'
                # Exercise both the explicit destructor and TemporaryDirectory's
                # inherited weak finalizer; the child owns neither file lifetime.
                job.__del__()
                job._scratch.cleanup()
                del job
                gc.collect()
                os.write(write_fd, reason.encode())
            finally:
                os.close(write_fd)
                os._exit(0)
        os.close(write_fd)
        try:
            reason = os.read(read_fd, 256).decode()
            self.assertGreater(child, 1)
            self.assertEqual(os.waitpid(child, 0)[1], 0)
            self.assertEqual(reason, 'group_not_owned')
            self.assertTrue(scratch.exists())
            self.assertEqual(proc.communicate(job, timeout=1)[0], b'parent evidence')
        finally:
            os.close(read_fd)
            job.stop()

    def test_P7_REENTER_final_call_cannot_rebind_selected_descriptor(self):
        sentinel = subprocess.Popen([sys.executable, '-c', 'import sys;sys.stdin.read()'],
                                    stdin=subprocess.PIPE)
        job = proc.popen([sys.executable, '-c', 'import time;time.sleep(.15)'],
                         start_new_session=True)
        retained = []
        observed = {}
        def final_call(fd, sig, *args):
            observed['before'] = os.fstat(fd).st_ino
            observed['nested'] = job.wait(5)
            # A retired descriptor would be reused for this independent sentinel.
            for _ in range(fd+2):
                retained.append(os.pidfd_open(sentinel.pid, 0))
            observed['after'] = os.fstat(fd).st_ino
            observed['reused'] = fd in retained
            # Deliberately do not deliver the disputed signal.
        try:
            with mock.patch.object(proc_pidns.signal, 'pidfd_send_signal', side_effect=final_call):
                self.assertEqual(job.stop(), 0)
            self.assertEqual(observed['nested'], 0)
            self.assertEqual(observed['before'], observed['after'])
            self.assertFalse(observed['reused'])
            self.assertIsNone(job._backend._fd)
            self.assertIsNone(sentinel.poll())
        finally:
            for fd in retained:
                os.close(fd)
            job.stop()
            sentinel.stdin.close()
            sentinel.wait(5)

    def test_pidns_completed_supervisor_cannot_enter_signal_unsafe_teardown(self):
        # Reproduce the observed -SIGUSR1 completion: the payload and drain
        # receipt are complete while Python teardown restores signal defaults.
        with tempfile.TemporaryDirectory() as temp:
            wrapper = Path(temp)/'supervisor.py'
            marker = Path(temp)/'teardown'
            wrapper.write_text('import atexit,runpy,signal,time\nfrom pathlib import Path\n'
                'def teardown():\n signal.signal(signal.SIGUSR1,signal.SIG_DFL)\n'
                f' Path({str(marker)!r}).touch()\n time.sleep(10)\n'
                'atexit.register(teardown)\n'
                f'runpy.run_path({str(Path(proc_pidns.__file__).resolve())!r},run_name="__main__")\n')
            original = subprocess.Popen
            def spawn(argv, **options):
                if len(argv) > 3 and argv[2] == str(Path(proc_pidns.__file__).resolve()):
                    argv = [*argv[:2], str(wrapper), *argv[3:]]
                return original(argv, **options)
            with mock.patch.object(proc_pidns.subprocess, 'Popen', side_effect=spawn):
                job = proc.popen(['/usr/bin/true'], start_new_session=True)
            deadline = time.monotonic()+3
            while not marker.exists() and proc.poll_exit(job) is None and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertEqual(job.stop(), 0)
            self.assertEqual(job.result.main_returncode, 0)

    def test_P7_TEST_probe_has_independent_owned_deadline(self):
        from test_f2u1_review import ReviewTests
        started = time.monotonic()
        with self.assertRaisesRegex(AssertionError, 'probe_deadline'):
            ReviewTests()._bounded_probe('hang', 0)
        self.assertLess(time.monotonic()-started, 4.5)

    def test_test_environment_has_explicit_refusal_and_no_backend_fallback(self):
        with mock.patch.dict(os.environ, {'AF_PROC_BACKEND': 'systemd'}):
            with self.assertRaises(proc.SignalRefusal) as caught:
                proc.check_test_environment()
        self.assertEqual(caught.exception.code, 'test_backend_environment')
        for value in ('AF_PROC_BACKEND=pidns', 'AF_PROC_PIDNS_PROC', 'PYTHONPATH=.:tests'):
            self.assertIn(value, proc.TEST_ENVIRONMENT)
        proc.check_test_environment()
