"""P8 bounded lifecycle regressions. Manager doubles never deliver signals."""
import gc
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time
import unittest
import uuid
from unittest import mock
from asset_factory import proc
from systemd_fake import FakeRunner
from test_safety_proc import launch

def gate_module():
    # Mesh tests also expose the sibling's tools namespace; pin this component.
    spec=importlib.util.spec_from_file_location('af_round9_gates',Path(proc.__file__).resolve().parents[1]/'tools/gates.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module

class AcceptedThenLost(FakeRunner):
    def run(self, argv, *, timeout):
        reply=super().run(argv,timeout=timeout)
        if argv[0]=='systemd-run':
            raise proc.SignalRefusal('systemd_user_manager_unavailable')
        return reply

class UnknownExit(FakeRunner):
    def run(self, argv, *, timeout):
        reply=super().run(argv,timeout=timeout)
        if argv[0]=='systemd-run':
            for state in self.units.values(): state['ExecMainCode']='0'
        return reply

class Round9(unittest.TestCase):
    def test_P8_START_accepted_reply_lost_is_owned_before_request_and_retired(self):
        observed=[]
        class Inspect(AcceptedThenLost):
            def run(self, argv, *, timeout):
                if argv[0]=='systemd-run':
                    unit=next(a.split('=',1)[1] for a in argv if a.startswith('--unit='))
                    jobs=[j for j,r in proc._CREATIONS.items() if r.unit==unit and r.active]
                    observed.append(bool(jobs) and Path(jobs[0]._scratch.name,'creation.json').is_file())
                return super().run(argv,timeout=timeout)
        runner=Inspect()
        for _ in range(4):
            with self.assertRaises(proc.SignalRefusal): launch(runner)
        self.assertEqual(observed,[True]*4)
        self.assertEqual(runner.units,{})
        self.assertEqual(sum(c[2]=='stop' for c in runner.calls if c[0]=='systemctl'),4)

    def test_P8_START_failed_cleanup_retains_transaction_and_reconciles(self):
        runner=AcceptedThenLost(); runner.fail_stop=True
        with self.assertRaises(proc.SignalRefusal) as caught: launch(runner)
        self.assertTrue(caught.exception.cleanup_errors)
        gc.collect()
        owned=[j for j,r in proc._CREATIONS.items() if r.runner is runner and r.active]
        self.assertEqual(len(owned),1)
        self.assertTrue(Path(owned[0]._scratch.name,'creation.json').is_file())
        runner.fail_stop=False
        self.assertEqual(proc.reconcile_owned(runner=runner),[])
        self.assertEqual(runner.units,{})

    def test_P8_START_abandoned_completed_handle_keeps_owner_until_reconcile(self):
        runner=FakeRunner(auto_exit=0)
        launch(runner); gc.collect()
        self.assertEqual(sum(r.runner is runner and r.active for r in proc._CREATIONS.values()),1)
        self.assertEqual(proc.reconcile_owned(runner=runner),[])
        self.assertEqual(runner.units,{})

    def test_P8_REFUSAL_guard_keeps_incomplete_record(self):
        from providers.trellis2.build import process_guard
        runner=UnknownExit(auto_exit=0)
        with proc.using_runner(runner): result=process_guard.supervise(['/usr/bin/true'],1)
        self.assertEqual((result['rc'],result['reason']),(3,'job_result_unproven'))
        self.assertEqual(runner.units,{})

    def test_P8_REFUSAL_gate_keeps_incomplete_record(self):
        gates=gate_module()
        runner=UnknownExit(auto_exit=0)
        with proc.using_runner(runner): result=gates.run_leg('F0-U1',['/usr/bin/true'],1)
        self.assertEqual((result['rc'],result['failures']),(3,['job_result_unproven']))
        self.assertEqual(runner.units,{})

    def test_P8_REFUSAL_capture_translates_incomplete_to_domain_error(self):
        from tools.blender import glb_capture
        from tools.blender.helpers_contracts import GlbError
        runner=UnknownExit(auto_exit=0)
        with proc.using_runner(runner), self.assertRaises(GlbError) as caught:
            glb_capture.run_owned(['/usr/bin/true'],timeout_s=1,cwd=Path.cwd())
        self.assertEqual((caught.exception.rc,caught.exception.code),(3,'job_result_unproven'))
        self.assertEqual(runner.units,{})

    def primary_cleanup_case(self, name):
        from providers.trellis2.build import process_guard
        from tools.blender import glb_capture
        from tools.blender.helpers_contracts import GlbError
        for error, code in ((proc.SignalRefusal('cleanup'), 'cleanup'),
                            (RuntimeError('cleanup_fault'), 'RuntimeError')):
            with self.subTest(caller=name, cleanup=code):
                runner=FakeRunner()
                with proc.using_runner(runner), mock.patch.object(proc.Job,'stop',side_effect=error):
                    if name=='capture':
                        with mock.patch.object(proc.Job,'wait',side_effect=proc.SignalRefusal('primary')), self.assertRaises(GlbError) as caught:
                            glb_capture.run_owned(['/usr/bin/true'],timeout_s=1,cwd=Path.cwd())
                        self.assertEqual(caught.exception.code,'primary')
                        errors=caught.exception.cleanup_errors
                        self.assertIn(code,json.loads(caught.exception.diagnostics)['cleanup_errors'])
                    elif name=='guard':
                        with mock.patch.object(proc.Job,'wait',side_effect=proc.SignalRefusal('primary')):
                            result=process_guard.supervise(['/usr/bin/true'],1)
                        self.assertEqual(result['reason'],'primary')
                        errors=result['cleanup_errors']
                    else:
                        with mock.patch.object(proc,'wait_exit',side_effect=proc.SignalRefusal('primary')):
                            result=gate_module().run_leg('F0-U1',['/usr/bin/true'],1)
                        self.assertEqual((result['rc'],result['failures']),(3,['primary']))
                        errors=result['evidence']['cleanup_errors']
                self.assertIn(code,errors)
                proc.reconcile_owned(runner=runner)

    def test_P8_REFUSAL_cleanup_error_does_not_replace_primary_guard_result(self):
        self.primary_cleanup_case('guard')

    def test_P8_REFUSAL_cleanup_error_does_not_replace_primary_capture_error(self):
        self.primary_cleanup_case('capture')

    def test_P8_REFUSAL_cleanup_error_does_not_replace_primary_gate_result(self):
        self.primary_cleanup_case('gate')

    def test_P8_START_timeout_after_acceptance_is_typed_and_retired(self):
        class TimedOut(FakeRunner):
            def run(self, argv, *, timeout):
                answer=super().run(argv,timeout=timeout)
                if argv[0]=='systemd-run': raise subprocess.TimeoutExpired(argv,timeout)
                return answer
        runner=TimedOut()
        with self.assertRaises(proc.SignalRefusal) as caught: launch(runner)
        self.assertEqual(caught.exception.code,'systemd_user_manager_unavailable')
        self.assertEqual(runner.units,{})

    def test_P8_START_explicit_rejection_attempts_owned_retirement(self):
        runner=FakeRunner(); runner.fail_start=True
        with self.assertRaises(proc.SignalRefusal): launch(runner)
        self.assertEqual([c[2] for c in runner.calls if c[0]=='systemctl' and c[2] in ('stop','reset-failed')],['stop','reset-failed'])
        self.assertEqual(runner.units,{})

    def test_P8_WAIT_every_manager_call_gets_remaining_budget(self):
        budgets=[]
        class Clocked(FakeRunner):
            def run(self, argv, *, timeout):
                if budgets is not None: budgets.append(timeout)
                answer=super().run(argv,timeout=timeout)
                time.sleep(.002)
                return answer
        runner=Clocked(auto_exit=0); job=launch(runner); budgets.clear()
        self.assertEqual(job.wait(.2),0)
        self.assertEqual(len(budgets),4)
        self.assertTrue(all(0<b<=.2 for b in budgets),budgets)
        self.assertTrue(all(a>b for a,b in zip(budgets,budgets[1:])),budgets)

    def test_P8_WAIT_late_manager_completion_never_returns_success(self):
        runner=FakeRunner(auto_exit=0); job=launch(runner)
        runner.show_hook=lambda unit:time.sleep(.025)
        try:
            with self.assertRaises(subprocess.TimeoutExpired): job.wait(.01)
        finally:
            runner.show_hook=None; job.stop()

    def test_P8_WAIT_restored_total_probe_duration_oracle(self):
        from test_f2u1_review import ReviewTests
        runner=FakeRunner(auto_exit=0,output=b'[2,{}]\n')
        runner.show_hook=lambda unit:time.sleep(1.1)
        with proc.using_runner(runner), self.assertRaises(AssertionError):
            ReviewTests()._bounded_probe('fifo_compare',2)
        proc.reconcile_owned(runner=runner)

    def test_P8_REAP_external_reap_retires_fd_but_keeps_unknown_status(self):
        for _ in range(4):
            job=proc.popen(['/usr/bin/true'],start_new_session=True)
            child=job._backend._child
            self.assertTrue(type(child.pid) is int and child.pid>1)
            self.assertEqual(os.waitpid(child.pid,0)[0],child.pid)
            self.assertTrue(job._backend._receipt.is_file())
            with self.assertRaises(proc.SignalRefusal) as caught: job.wait(1)
            self.assertEqual(caught.exception.code,'job_result_unproven')
            self.assertIsNone(job._backend._fd)
            self.assertIsNone(job.stop())
            with self.assertRaises(proc.SignalRefusal): job.wait(1)

    def test_P8_REAP_automatic_reaper_is_also_unknown_not_success(self):
        job=proc.popen(['/usr/bin/true'],start_new_session=True)
        # Popen's native waiter consumes the same owned supervisor status.
        job._backend._child.wait(5)
        with self.assertRaises(proc.SignalRefusal) as caught: job.wait(1)
        self.assertEqual(caught.exception.code,'job_result_unproven')
        self.assertIsNone(job._backend._fd)
        job.stop()

    def test_P8_REAP_reentrant_external_reap_closes_only_after_delivery(self):
        from asset_factory import proc_pidns
        import sys
        job=proc.popen([sys.executable,'-c','import time;time.sleep(.1)'],start_new_session=True)
        seen={}
        def final_call(fd,sig,*args):
            job._backend._child.wait(5)
            with self.assertRaises(proc.SignalRefusal) as caught: job.wait(1)
            self.assertEqual(caught.exception.code,'job_result_unproven')
            seen['retained']=job._backend._fd is not None
            # The disputed delivery is intercepted, never sent.
        with mock.patch.object(proc_pidns.signal,'pidfd_send_signal',side_effect=final_call):
            with self.assertRaises(proc.SignalRefusal) as caught: job.stop()
        self.assertEqual(caught.exception.code,'job_result_unproven')
        self.assertTrue(seen['retained'])
        self.assertIsNone(job._backend._fd)
        self.assertIsNone(job.stop())

    def test_P8_NAME_unit_name_is_uuid4_derived(self):
        value=uuid.uuid4(); runner=FakeRunner(auto_exit=0)
        with mock.patch.object(proc.uuid,'uuid4',return_value=value) as generate:
            job=launch(runner)
        generate.assert_called_once_with()
        self.assertEqual(job.unit_name,'af-job-'+value.hex+'.service')
        self.assertEqual(uuid.UUID(value.hex).version,4)
        job.wait(1)
