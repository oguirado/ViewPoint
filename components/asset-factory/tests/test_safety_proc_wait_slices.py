"""PROC-T: caller slices and manager transactions are independent budgets."""
import subprocess
import unittest
from unittest import mock

from asset_factory import proc
from systemd_fake import FakeRunner
from test_safety_proc import launch


class Clock:
    def __init__(self):
        self.now = 100.

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TimedRunner(FakeRunner):
    """No processes: model SystemdRunner's bounded subprocess transaction."""
    def __init__(self, clock, *, finish_after=3, show_delay=.08, stop_delay=0):
        super().__init__()
        self.clock, self.finish_after = clock, finish_after
        self.show_delay, self.stop_delay = show_delay, stop_delay
        self.armed, self.observations, self.budgets = False, 0, []

    def run(self, argv, *, timeout):
        if self.armed:
            action = argv[2]
            self.budgets.append((action, timeout))
            delay = self.show_delay if action == 'show' else self.stop_delay
            self.clock.advance(min(delay, timeout))
            if delay > timeout:
                # Exactly the production translation, including its cause.
                try:
                    raise subprocess.TimeoutExpired(argv, timeout)
                except subprocess.TimeoutExpired as exc:
                    raise proc.SignalRefusal('systemd_user_manager_unavailable') from exc
            if action == 'show' and '--property='+proc._PROPERTIES in argv:
                self.observations += 1
                if self.finish_after is not None and self.observations >= self.finish_after:
                    self.finish(argv[-1])
        return super().run(argv, timeout=timeout)


class WaitSlices(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.patchers = (mock.patch.object(proc.time, 'monotonic', self.clock),
                         mock.patch.object(proc.time, 'sleep', self.clock.advance))
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def job(self, **kwargs):
        runner = TimedRunner(self.clock, **kwargs)
        job = launch(runner)
        def dispose():
            runner.armed = False
            job.stop()
        self.addCleanup(dispose)
        runner.armed = True
        return job, runner

    def test_slow_show_slices_timeout_then_observe_exit(self):
        job, runner = self.job()
        expiries = 0
        for _ in range(8):
            try:
                code = proc.wait_exit(job, timeout=.05)
            except subprocess.TimeoutExpired:
                expiries += 1
            else:
                self.assertEqual(code, 0)
                break
        else:
            self.fail('short slices lost terminal evidence')
        self.assertEqual(expiries, 4)
        self.assertEqual(runner.observations, 3)
        self.assertEqual(runner.units, {})
        self.assertNotIn(job, proc._LIVE_JOBS)

    def test_terminal_observation_survives_expired_slice(self):
        job, runner = self.job(finish_after=1)
        with self.assertRaises(subprocess.TimeoutExpired):
            job.wait(.05)
        self.assertIsNotNone(job._pending)
        self.assertIsNone(job.returncode)
        self.assertIn(job.unit_name, runner.units)

    def test_running_observation_checks_caller_deadline(self):
        job, runner = self.job(finish_after=None)
        with self.assertRaises(subprocess.TimeoutExpired):
            job.wait(.05)
        self.assertEqual(runner.observations, 1)
        self.assertIsNone(job._pending)

    def test_retirement_finishes_before_slice_timeout_is_reported(self):
        job, runner = self.job(finish_after=1, show_delay=.01, stop_delay=.08)
        with self.assertRaises(subprocess.TimeoutExpired):
            job.wait(.05)
        self.assertEqual(runner.units, {})
        self.assertTrue(job._retired)
        self.assertEqual(job.wait(.05), 0)
        mutations = [budget for action, budget in runner.budgets if action != 'show']
        self.assertEqual(len(mutations), 2)
        self.assertTrue(all(.08 < budget <= 5 for budget in mutations), mutations)

    def test_manager_transaction_over_own_bound_remains_refusal(self):
        job, runner = self.job(show_delay=10.)
        with self.assertRaises(proc.SignalRefusal) as caught:
            job.wait(.05)
        self.assertEqual(caught.exception.code, 'systemd_user_manager_unavailable')
        self.assertIsInstance(caught.exception.__cause__, subprocess.TimeoutExpired)
        self.assertIsNone(job._pending)
        self.assertIn(job.unit_name, runner.units)

    def test_nonzero_manager_reply_remains_refusal(self):
        job, runner = self.job(show_delay=0)
        runner.available = False
        with self.assertRaises(proc.SignalRefusal) as caught:
            job.wait(.05)
        self.assertEqual(caught.exception.code, 'systemd_user_manager_unavailable')
        runner.available = True

    def test_zero_slice_does_not_start_a_transaction(self):
        job, runner = self.job()
        with self.assertRaises(subprocess.TimeoutExpired):
            job.wait(0)
        self.assertEqual(runner.budgets, [])

    def test_read_only_poll_uses_bounded_transaction_without_slice(self):
        job, runner = self.job(finish_after=1)
        self.assertEqual(proc.poll_exit(job), 0)
        self.assertTrue(all(0 < budget <= 5 for _, budget in runner.budgets))
        self.assertEqual(runner.units, {})

    def test_expired_retirement_keeps_typed_failure_for_next_slice(self):
        job, runner = self.job(finish_after=1, show_delay=.01, stop_delay=.08)
        runner.finish(job, 0)
        runner.units[job.unit_name]['ExecMainCode'] = '0'
        runner.finish_after = None
        with self.assertRaises(subprocess.TimeoutExpired):
            job.wait(.05)
        with self.assertRaises(proc.SignalRefusal) as caught:
            job.wait(.05)
        self.assertEqual(caught.exception.code, 'job_result_unproven')
        self.assertEqual(runner.units, {})

    def test_real_runner_oserror_and_own_timeout_are_manager_failures(self):
        for failure in (OSError('manager absent'), subprocess.TimeoutExpired('show', 1)):
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch.object(proc.Path, 'read_text', return_value='systemd'), \
                    mock.patch.object(proc.subprocess, 'run', side_effect=failure):
                with self.assertRaises(proc.SignalRefusal) as caught:
                    proc.SystemdRunner().run(['systemctl', '--user', 'show'], timeout=1)
                self.assertEqual(caught.exception.code, 'systemd_user_manager_unavailable')


if __name__ == '__main__':
    unittest.main()
