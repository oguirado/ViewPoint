"""PROC-C/D/E/F: no reapable job child or process-control descriptor exists."""
import gc
import os
from pathlib import Path
import signal
import subprocess
import unittest
from unittest import mock
from asset_factory import proc
from systemd_fake import FakeRunner
from test_safety_proc import launch

class GroupLifetime(unittest.TestCase):
    def test_PROC_F_returncode_unset_until_disposal_is_proven(self):
        runner = FakeRunner(auto_exit=0)
        child = launch(runner)
        def disposing(unit):
            self.assertEqual(unit, child.unit_name)
            self.assertIsNone(child.returncode)  # Restored stronger postcondition.
            self.assertIsNone(child.result)
        runner.stop_hook = disposing
        self.assertEqual(child.wait(1), 0)
        self.assertEqual(child.returncode, 0)

    def test_PROC_F_premature_returncode_mutant_is_red(self):
        runner = FakeRunner(auto_exit=0)
        child = launch(runner)
        # A mutant publishing its result before stop violates the same oracle.
        with mock.patch.object(proc.Job, 'returncode', property(lambda self: 77)):
            runner.stop_hook = lambda unit: self.assertIsNone(child.returncode)
            with self.assertRaises(AssertionError):
                child.wait(1)
        runner.stop_hook = None
        child.wait(1)

    def test_failed_disposal_never_publishes_returncode(self):
        runner = FakeRunner(auto_exit=0)
        child = launch(runner)
        runner.fail_stop = True
        with self.assertRaises(proc.SignalRefusal) as error:
            child.wait(1)
        self.assertEqual(error.exception.code, 'group_drain_unproven')
        self.assertIsNone(child.returncode)
        runner.fail_stop = False
        self.assertEqual(child.wait(1), 0)

    def test_PROC_E_factory_reaping_is_irrelevant_to_unit_result(self):
        runner = FakeRunner(auto_exit=7)
        child = launch(runner)
        with mock.patch.object(os, 'waitpid', side_effect=ChildProcessError), \
             mock.patch.object(os, 'waitid', side_effect=ChildProcessError):
            self.assertEqual(child.wait(1), 7)
        self.assertEqual(runner.units, {})

    def test_PROC_D_repeated_cleanup_cannot_rebind_a_process_sink(self):
        runner = FakeRunner()
        a, b = launch(runner), launch(runner)
        a.stop()
        before = list(runner.calls)
        for _ in range(5):
            self.assertEqual(a.stop(), -signal.SIGKILL)
            self.assertEqual(a.wait(0), -signal.SIGKILL)
        self.assertEqual(before, runner.calls)
        self.assertIn(b.unit_name, runner.units)
        b.stop()

    def test_PROC_C_repeated_waits_leave_no_units_or_descriptors(self):
        runner = FakeRunner(auto_exit=0)
        gc.collect()
        before = len(list(Path('/proc/self/fd').iterdir()))
        for _ in range(32):
            child = launch(runner)
            self.assertEqual(child.wait(1), 0)
            self.assertEqual(child.wait(1), 0)
        del child
        gc.collect()
        self.assertEqual(runner.units, {})
        self.assertEqual(len(list(Path('/proc/self/fd').iterdir())), before)

    def test_unknown_exit_evidence_does_not_become_zero(self):
        runner = FakeRunner()
        child = launch(runner)
        runner.units.pop(child.unit_name)
        with self.assertRaises(proc.SignalRefusal):
            child.wait(1)
        self.assertIsNone(child.returncode)

    def test_wait_timeout_does_not_claim_completion(self):
        child = launch()
        with self.assertRaises(subprocess.TimeoutExpired):
            child.wait(.001)
        self.assertIsNone(child.returncode)
        self.assertEqual(child.stop(), -signal.SIGKILL)
