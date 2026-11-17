"""PROC-A/B: service ownership and cgroup policy, without numeric selectors."""
import signal
import unittest
from unittest import mock
from asset_factory import proc
from systemd_fake import FakeRunner
from test_safety_proc import launch

class Containment(unittest.TestCase):
    def test_payload_is_service_with_full_tree_cleanup_and_declared_bounds(self):
        runner = FakeRunner()
        job = launch(runner)
        command = next(c for c in runner.calls if c[0] == 'systemd-run')
        for value in ('--property=KillMode=control-group', '--property=SendSIGKILL=yes',
                      '--property=MemoryHigh=1073741824', '--property=MemoryMax=2147483648',
                      '--property=MemorySwapMax=0', '--property=RemainAfterExit=yes'):
            self.assertIn(value, command)
        self.assertNotIn('--scope', command)
        self.assertNotIn('--collect', command)  # Retain short-lived failure evidence.
        job.stop()
        self.assertEqual(runner.units, {})
        kill = next(c for c in runner.calls if c[2] == 'kill')
        self.assertEqual(kill[-2:], ('--', job.unit_name))
        self.assertIn('--kill-whom=all', kill)

    def test_PROC_B_deliberate_escape_is_explicitly_out_of_scope(self):
        self.assertIn('Deliberate cgroup escape', proc.__doc__)
        self.assertIn('outside the contract', proc.__doc__)

    def test_PROC_A_mocked_Popen_cannot_create_stop_methods(self):
        runner = FakeRunner()
        with mock.patch.object(proc.subprocess, 'Popen') as launch_process:
            job = launch(runner)
            job.stop()
        launch_process.assert_not_called()
        for name in ('kill', 'terminate', 'send_signal'):
            self.assertFalse(hasattr(job, name))

    def test_declined_start_never_stops_potentially_existing_unit(self):
        runner = FakeRunner()
        from uuid import UUID
        value = UUID('12345678-1234-4234-8234-123456789abc')
        unit = 'af-job-'+value.hex+'.service'
        runner.units[unit] = {'LoadState': 'loaded', 'owner': 'other'}
        with mock.patch.object(proc.uuid, 'uuid4', return_value=value), self.assertRaises(proc.SignalRefusal):
            launch(runner)
        self.assertFalse(any(c[0] == 'systemctl' and c[2] in ('stop', 'kill', 'reset-failed')
                             for c in runner.calls))

    def test_unit_names_are_unique_and_stop_cannot_reach_another_job(self):
        runner = FakeRunner()
        jobs = [launch(runner) for _ in range(8)]
        self.assertEqual(len({j.unit_name for j in jobs}), 8)
        for job in jobs:
            job.stop()
        self.assertEqual(runner.units, {})

    def test_caller_name_tampering_is_rejected(self):
        runner = FakeRunner()
        job = launch(runner)
        original = job.unit_name
        job._unit_name = 'unrelated.service'
        before = len(runner.calls)
        with self.assertRaises(proc.SignalRefusal):
            job.stop()
        self.assertEqual(len(runner.calls), before)
        job._unit_name = original
        job.stop()
