"""Explicit namespace admission and retained-descriptor lifetime controls."""
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


class NamespaceAdmission(unittest.TestCase):
    def test_host_request_refuses_before_spawn(self):
        with mock.patch.dict(os.environ, {'AF_PROC_BACKEND': 'pidns'}), \
             mock.patch.object(proc_pidns, '_namespace_pids', return_value=(123,)), \
             mock.patch.object(subprocess, 'Popen') as spawn:
            with self.assertRaises(proc.SignalRefusal) as error:
                proc.popen(['/usr/bin/true'], start_new_session=True)
        self.assertEqual(error.exception.code, 'pidns_namespace_unproven')
        spawn.assert_not_called()

    def test_unknown_backend_refuses_without_fallback(self):
        with mock.patch.dict(os.environ, {'AF_PROC_BACKEND': 'invalid'}):
            with self.assertRaises(proc.SignalRefusal) as error:
                proc.popen(['/usr/bin/true'], start_new_session=True)
        self.assertEqual(error.exception.code, 'proc_backend_invalid')

    def test_namespace_without_opt_in_keeps_systemd(self):
        with mock.patch.dict(os.environ, {'AF_PROC_BACKEND': 'systemd'}), \
             mock.patch.object(proc.SystemdRunner, 'run', side_effect=proc.SignalRefusal('systemd_user_manager_unavailable')):
            with self.assertRaises(proc.SignalRefusal) as error:
                proc.popen(['/usr/bin/true'], start_new_session=True)
        self.assertEqual(error.exception.code, 'systemd_user_manager_unavailable')

    def test_missing_and_forged_outer_procfs_are_not_proof(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'self').mkdir()
            (root/'self/status').write_text('NSpid:\t123\t456\n')
            for value in (temp, str(root/'missing')):
                with mock.patch.dict(os.environ, {'AF_PROC_PIDNS_PROC': value}), \
                     mock.patch.object(proc_pidns, '_local_pids', return_value=(os.getpid(),)):
                    with self.assertRaises(proc.SignalRefusal):
                        proc_pidns.require_namespace()

    def test_mocked_spawn_never_acquires_a_descriptor_and_leaks_no_fds(self):
        before = len(list(Path('/proc/self/fd').iterdir()))
        with mock.patch.object(subprocess, 'Popen', return_value=mock.MagicMock()), \
             mock.patch.object(os, 'pidfd_open') as acquire:
            for _ in range(8):
                with self.assertRaises(proc.SignalRefusal) as error:
                    proc.popen(['/usr/bin/true'], start_new_session=True)
                self.assertEqual(error.exception.code, 'signal_target_not_integer')
        acquire.assert_not_called()
        self.assertEqual(len(list(Path('/proc/self/fd').iterdir())), before)


class NamespaceLifetime(unittest.TestCase):
    def start(self, code, *args, **options):
        job = proc.popen([sys.executable, '-B', '-c', code, *args],
                         start_new_session=True, **options)
        self.addCleanup(job.stop)
        return job

    def test_real_kernel_proof(self):
        pids = proc_pidns.require_namespace()
        self.assertGreaterEqual(len(pids), 2)
        self.assertEqual(pids[-1], os.getpid())

    def test_output_input_exit_and_composition(self):
        job = self.start('import sys;print(sys.stdin.read());sys.exit(7)', input=b'request')
        self.assertEqual(proc.communicate(job, timeout=5)[0], b'request\n')
        self.assertEqual(job.returncode, 7)
        for name in ('pid', 'kill', 'terminate', 'send_signal', '_popen'):
            self.assertFalse(hasattr(job, name), name)
        self.assertIsNotNone(job.result)

    def test_stop_is_idempotent_and_preserves_sibling(self):
        first = self.start('import time;time.sleep(60)')
        sibling = self.start('import time;time.sleep(60)')
        self.assertEqual(first.stop(), -signal.SIGKILL)
        self.assertEqual(first.wait(0), -signal.SIGKILL)
        self.assertEqual(first.stop(), -signal.SIGKILL)
        self.assertIsNone(proc.poll_exit(sibling))

    def descendants(self, leader_exits):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'child'
            code = ('import subprocess,sys,time;from pathlib import Path;'
                    'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"],start_new_session=True);'
                    'Path(sys.argv[1]).write_text(str(child.pid));' +
                    ('sys.exit(0)' if leader_exits else 'time.sleep(60)'))
            job = self.start(code, str(path))
            deadline = time.monotonic()+5
            while not path.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(path.exists())
            pid = int(path.read_text())
            self.assertGreater(pid, 1)
            self.assertEqual(job.wait(5) if leader_exits else job.stop(),
                             0 if leader_exits else -signal.SIGKILL)
            self.assertFalse(Path(f'/proc/{pid}').exists(), 'descendant not reaped')

    def test_independent_session_descendant_is_reaped_on_stop(self):
        self.descendants(False)

    def test_leader_exit_drains_adopted_descendants(self):
        self.descendants(True)

    def test_timeout_leaves_no_success_result(self):
        job = self.start('import time;time.sleep(60)')
        with self.assertRaises(subprocess.TimeoutExpired):
            job.wait(.001)
        self.assertIsNone(job.result)
        self.assertEqual(job.stop(), -signal.SIGKILL)

    def test_fork_inherited_handle_is_refused(self):
        job = self.start('import time;time.sleep(60)')
        with mock.patch.object(proc.os, 'getpid', return_value=os.getpid()+100000):
            with self.assertRaises(proc.SignalRefusal) as error:
                job.stop()
        self.assertEqual(error.exception.code, 'group_not_owned')

    def test_descriptor_delivery_failure_refuses_without_fallback(self):
        job = self.start('import time;time.sleep(60)')
        with mock.patch.object(proc_pidns.signal, 'pidfd_send_signal', side_effect=OSError('closed descriptor')):
            with self.assertRaises(proc.SignalRefusal) as error:
                job.stop()
        self.assertEqual(error.exception.code, 'group_drain_unproven')
        self.assertIsNone(job.result)
