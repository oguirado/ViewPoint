"""Manager exit evidence replaces the removed kernel-descriptor machinery."""
import dataclasses
import signal
import subprocess
import unittest
from asset_factory import proc
from systemd_fake import FakeRunner
from test_safety_proc import launch

class UnitResults(unittest.TestCase):
    def test_nonzero_exit_and_signal_are_not_unconditional_success(self):
        for code in (0, 2, 3, 4, 5, 6, 77, -signal.SIGTERM, -signal.SIGKILL):
            runner = FakeRunner(auto_exit=code)
            job = launch(runner)
            self.assertEqual(job.wait(1), code)
            self.assertEqual(job.result.returncode, code)
            with self.assertRaises(dataclasses.FrozenInstanceError):
                job.result.returncode = 99
            self.assertEqual(runner.units, {})

    def test_output_and_input_snapshots_are_files(self):
        runner = FakeRunner(auto_exit=0, output=b'hello', error=b'error')
        job = launch(runner, input=b'request')
        self.assertEqual(proc.communicate(job, timeout=1), (b'hello', b'error'))
        self.assertEqual((job.stdout_path.parent/'stdin').read_bytes(), b'request')
        self.assertFalse(hasattr(job, 'stdin'))

    def test_input_cannot_change_after_launch(self):
        job = launch(FakeRunner(auto_exit=0))
        with self.assertRaises(proc.SignalRefusal) as error:
            proc.communicate(job, b'changed', timeout=1)
        self.assertEqual(error.exception.code, 'job_input_after_start')
        job.wait(1)

    def test_manager_loss_has_no_direct_fallback(self):
        runner = FakeRunner()
        job = launch(runner)
        runner.available = False
        with self.assertRaises(proc.SignalRefusal):
            job.stop()
        self.assertIsNone(job.returncode)
        runner.available = True
        job.stop()

    def test_result_missing_exit_code_refuses(self):
        runner = FakeRunner(auto_exit=0)
        job = launch(runner)
        runner.units[job.unit_name]['ExecMainCode'] = '0'
        with self.assertRaises(proc.SignalRefusal) as error:
            job.wait(1)
        self.assertEqual(error.exception.code, 'job_result_unproven')
        self.assertIsNone(job.returncode)
        self.assertEqual(runner.units, {})
        with self.assertRaises(proc.SignalRefusal) as repeated:
            job.wait(1)
        self.assertEqual(repeated.exception.code, 'job_result_unproven')
