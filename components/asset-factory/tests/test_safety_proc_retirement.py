"""RP11: retirement failures are distinct from expired caller wait slices."""
import signal
import subprocess
import unittest
from unittest import mock

from asset_factory import proc
from providers.trellis2.build import process_guard
from systemd_fake import FakeRunner
from test_safety_proc import launch
from test_safety_proc_wait_slices import Clock


class RetirementRunner(FakeRunner):
    """The reviewer's Slow runner: data only, with bounded RPC elapsed time."""
    def __init__(self, clock, **delays):
        super().__init__()
        self.clock, self.delays, self.armed = clock, delays, False
        self.transactions = []

    def run(self, argv, *, timeout):
        if self.armed:
            action = argv[2]
            self.transactions.append((action, timeout))
            delay = self.delays.get(action, 0)
            delay = delay() if callable(delay) else delay
            self.clock.advance(min(delay, timeout))
            if delay > timeout:
                raise proc.SignalRefusal('systemd_user_manager_unavailable')
        return super().run(argv, timeout=timeout)


class Retirement(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        for patcher in (mock.patch.object(proc.time, 'monotonic', self.clock),
                        mock.patch.object(proc.time, 'sleep', self.clock.advance)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def job(self, **delays):
        runner = RetirementRunner(self.clock, **delays)
        job = launch(runner)
        def dispose():
            runner.armed, runner.fail_stop = False, False
            proc.reconcile_owned(runner=runner)
        self.addCleanup(dispose)
        runner.armed = True
        return job, runner

    def test_F10_1_wait_None_hung_stop_is_refusal(self):
        job, runner = self.job(stop=10)
        runner.finish(job, 0)
        started = self.clock()
        with self.assertRaises(proc.SignalRefusal) as caught:
            job.wait(None)
        self.assertIn(caught.exception.code,
                      ('systemd_user_manager_unavailable', 'group_drain_unproven'))
        self.assertEqual(self.clock() - started, 5)
        self.assertIsNone(job.result)
        self.assertIn(job, proc._LIVE_JOBS)
        self.assertNotIn('TimeoutExpired', job._cleanup_errors)

    def test_F10_1_final_retirement_show_manager_timeouts_are_refusals(self):
        for failure in (OSError('manager absent'), subprocess.TimeoutExpired('show', 1)):
            with self.subTest(failure=type(failure).__name__):
                job, runner = self.job()
                runner.finish(job, 0)
                original = runner.run
                def fail_witness(argv, *, timeout):
                    if '--property=LoadState' in argv:
                        raise failure
                    return original(argv, timeout=timeout)
                with mock.patch.object(runner, 'run', side_effect=fail_witness):
                    with self.assertRaises(proc.SignalRefusal) as caught:
                        job.wait(None)
                self.assertEqual(caught.exception.code, 'systemd_user_manager_unavailable')
                self.assertIsNone(job.result)

    def test_F10_1_guard_hung_first_stop_is_not_job_deadline(self):
        job, runner = self.job()
        runner.finish(job, 0)
        delays = iter((10, 0))
        runner.delays['stop'] = lambda: next(delays, 0)
        with mock.patch.object(process_guard.af_proc, 'popen', return_value=job):
            result = process_guard.supervise(['/usr/bin/true'], 300)
        self.assertEqual(result['rc'], 3, result)
        self.assertIn(result['reason'],
                      ('systemd_user_manager_unavailable', 'group_drain_unproven'))
        self.assertEqual(runner.units, {})

    def test_F10_2_stop_returns_published_result_after_slice_expiry(self):
        job, runner = self.job(show=.8, stop=.8, **{'reset-failed': .8, 'kill': .8})
        started = self.clock()
        self.assertEqual(job.stop(), -signal.SIGKILL)
        self.assertLessEqual(self.clock() - started, 5)
        self.assertTrue(job._retired)
        self.assertEqual(runner.units, {})

    def test_F10_2_cleanup_does_not_invent_a_drain_error(self):
        job, runner = self.job(show=.8, stop=.8, **{'reset-failed': .8, 'kill': .8})
        self.assertEqual(proc.cleanup(job), [])
        self.assertEqual(job.returncode, -signal.SIGKILL)
        self.assertEqual(runner.units, {})

    def test_F10_2_SIGTERM_total_includes_retirement(self):
        job, runner = self.job(show=.95, stop=.95, **{'reset-failed': .95, 'kill': .95})
        started = self.clock()
        try:
            code = job.stop(signal.SIGTERM)
        except proc.SignalRefusal as exc:
            self.assertIn(exc.code,
                          ('systemd_user_manager_unavailable', 'group_drain_unproven'))
        else:
            self.assertEqual(code, -signal.SIGTERM)
            self.assertTrue(job._retired)
            self.assertEqual(runner.units, {})
        self.assertLessEqual(self.clock() - started, 5)

    def test_F10_2_stop_observation_cannot_overrun_remaining_total(self):
        job, runner = self.job(show=.8, stop=.8, **{'reset-failed': .8, 'kill': .8})
        started = self.clock()
        with self.assertRaises(proc.SignalRefusal):
            job.stop(signal.SIGTERM, _deadline=started + .4)
        self.assertLessEqual(self.clock() - started, .4 + 1e-12)

    def test_F10_3_expired_pending_slice_admits_no_retirement(self):
        job, runner = self.job()
        runner.finish(job, 0)
        runner.fail_stop = True
        with self.assertRaises(proc.SignalRefusal):
            job.wait(1)
        self.assertIsNotNone(job._pending)
        for recovered in (False, True):
            runner.fail_stop = not recovered
            for timeout in (0, -1):
                with self.subTest(recovered=recovered, timeout=timeout):
                    before = list(runner.transactions)
                    started = self.clock()
                    with self.assertRaises(subprocess.TimeoutExpired):
                        job.wait(timeout)
                    self.assertEqual(runner.transactions, before)
                    self.assertEqual(self.clock(), started)
                    self.assertIsNone(job.result)
        self.assertEqual(job.wait(1), 0)

    def test_PROC_T_show_has_own_one_second_bound(self):
        job, runner = self.job(show=1.2)
        started = self.clock()
        with self.assertRaises(proc.SignalRefusal) as caught:
            job.wait(.05)
        self.assertEqual(caught.exception.code, 'systemd_user_manager_unavailable')
        self.assertEqual(self.clock() - started, 1)
        self.assertEqual(runner.transactions, [('show', 1)])


if __name__ == '__main__':
    unittest.main()
