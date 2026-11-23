"""Explicit real-manager controls, excluded from unittest discovery.

Run only in a dedicated transient scope on the host. Every touched payload unit
is created here and recorded in AF_PROC_REAL_UNIT_LOG. No numeric delivery.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from asset_factory import proc

UNITS = []
JOBS = []
LIMITS = proc.MemoryLimits(6*2**30, 8*2**30, 0)

def start(argv, **kwargs):
    job = proc.popen(argv, start_new_session=True, memory_limits=LIMITS, **kwargs)
    UNITS.append(job.unit_name)
    JOBS.append(job)
    return job

class RealSystemd(unittest.TestCase):
    def lost_start(self, timeout_reply):
        class LostReply(proc.SystemdRunner):
            def run(self, argv, *, timeout):
                reply = super().run(argv, timeout=timeout)
                if argv[0] == 'systemd-run':
                    unit = next(x.split('=',1)[1] for x in argv if x.startswith('--unit='))
                    UNITS.append(unit)
                    if timeout_reply:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    raise proc.SignalRefusal('systemd_user_manager_unavailable')
                return reply
        with self.assertRaises(proc.SignalRefusal) as caught:
            start([sys.executable, '-c', 'import time;time.sleep(60)'], runner=LostReply())
        self.assertEqual(caught.exception.code, 'systemd_user_manager_unavailable')
        self.assertEqual(caught.exception.cleanup_errors, [])
        unit = caught.exception.unit_name
        reply = proc._unit_call(proc.SystemdRunner(), 'show', unit, '--property=LoadState')
        self.assertIn('LoadState=not-found', reply.stdout)

    def test_accepted_start_with_lost_reply_is_retired(self):
        self.lost_start(False)

    def test_accepted_start_with_timeout_reply_is_retired(self):
        self.lost_start(True)

    def test_pre_payload_failure_is_typed_and_retired(self):
        class PreStartFailure(proc.SystemdRunner):
            def run(self, argv, *, timeout):
                if argv[0] == 'systemd-run':
                    index = argv.index('--')
                    argv = [*argv[:index], '--property=ExecStartPre=/usr/bin/false', *argv[index:]]
                return super().run(argv, timeout=timeout)
        job = start(['/usr/bin/true'], runner=PreStartFailure())
        try:
            self.assertNotEqual(job.wait(10), 0)
            self.assertIsNone(job.result.main_returncode)
            self.assertEqual(job.result.reason, 'job_exit_code')
            self.assertEqual(job.result.systemd_result, 'exit-code')
        finally:
            job.stop()

    def test_runtime_timeout_is_failure(self):
        job = start([sys.executable, '-c', 'import time;time.sleep(60)'], runtime_s=1)
        try:
            self.assertNotEqual(job.wait(10), 0)
            self.assertEqual(job.result.reason, 'job_timeout')
        finally:
            job.stop()

    def test_valid_shaped_retarget_preserves_other_owned_job(self):
        first = start([sys.executable, '-c', 'import time;time.sleep(60)'])
        other = start([sys.executable, '-c', 'import time;time.sleep(60)'])
        original = first.unit_name
        try:
            first._unit_name = other.unit_name
            with self.assertRaises(proc.SignalRefusal):
                first.stop()
            self.assertIsNone(proc.poll_exit(other))
        finally:
            first._unit_name = original
            first.stop()
            other.stop()

    def test_exit_status_output_and_memory_properties(self):
        job = start([sys.executable, '-c', 'import sys;print("receipt");sys.exit(7)'])
        try:
            raw = proc._unit_call(proc.SystemdRunner(), 'show', job.unit_name,
                '--property=MemoryHigh,MemoryMax,MemorySwapMax,Nice,CPUAffinity').stdout
            fields = dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
            self.assertEqual(fields['MemoryHigh'], str(6*2**30))
            self.assertEqual(fields['MemoryMax'], str(8*2**30))
            self.assertEqual(fields['MemorySwapMax'], '0')
            self.assertEqual(fields['Nice'], str(os.nice(0)))
            self.assertEqual(fields['CPUAffinity'], '0-15')
            self.assertEqual(job.wait(10), 7)
            self.assertEqual(job.stdout_path.read_bytes(), b'receipt\n')
            self.assertEqual(job.result.systemd_result, 'exit-code')
        finally:
            job.stop()

    def descendants(self, leader_exits):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'child'
            script = ('import os,subprocess,sys,time; '
                'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(300)"],start_new_session=True); '
                'open(sys.argv[1],"w").write(str(p.pid)); ' + ('sys.exit(0)' if leader_exits else 'time.sleep(300)'))
            job = start([sys.executable, '-c', script, str(path)])
            try:
                deadline = time.monotonic()+10
                while not path.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(path.exists(), job.stderr_path.read_text())
                child = int(path.read_text())
                self.assertGreater(child, 1)
                if leader_exits:
                    self.assertEqual(job.wait(10), 0)
                else:
                    self.assertEqual(job.stop(), -signal.SIGKILL)
                deadline = time.monotonic()+3
                while time.monotonic() < deadline:
                    try:
                        state = Path(f'/proc/{child}/stat').read_text().rsplit(')',1)[1].split()[0]
                    except FileNotFoundError:
                        break
                    if state in ('Z','X'):
                        break
                    time.sleep(.01)
                else:
                    self.fail('owned descendant still executes after service disposal')
            finally:
                job.stop()

    def test_stop_reaches_independent_session_descendant(self):
        self.descendants(False)

    def test_wait_disposes_descendants_after_main_exit(self):
        self.descendants(True)

    def test_stopping_one_unit_preserves_another(self):
        first = start([sys.executable, '-c', 'import time;time.sleep(300)'])
        other = start([sys.executable, '-c', 'import time;time.sleep(300)'])
        try:
            self.assertEqual(first.stop(), -signal.SIGKILL)
            self.assertIsNone(proc.poll_exit(other))
        finally:
            first.stop()
            other.stop()

if __name__ == '__main__':
    assert Path('/proc/1/comm').read_text().strip() == 'systemd'
    scope = Path('/proc/self/cgroup').read_text().strip()
    assert scope.endswith('.scope') and '/run-' in scope, 'dedicated transient scope required'
    # A cooperative sentinel exits on EOF, so teardown requires no signal.
    sentinel = subprocess.Popen([sys.executable, '-c', 'import sys;sys.stdin.read()'], stdin=subprocess.PIPE)
    def identity():
        fields = Path(f'/proc/{sentinel.pid}/stat').read_text().rsplit(')',1)[1].split()
        return {'pid':sentinel.pid,'state':fields[0],'start_ticks':fields[19]}
    before = identity()
    def inventory():
        reply = subprocess.run(['systemctl', '--user', 'list-units', '--all', '--plain',
            '--no-legend', 'af-job-*.service'], capture_output=True, text=True, timeout=5)
        if reply.returncode:
            raise RuntimeError('unit_inventory_unproven')
        return reply.stdout.splitlines()
    units_before = inventory()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RealSystemd))
    after = identity()
    cleanup_errors=[]
    for job in JOBS:
        try:
            job.stop()
        except Exception as exc:
            cleanup_errors.append({'unit':job.unit_name,'error':str(exc)})
    remaining=[]
    for unit in UNITS:
        reply = proc._unit_call(proc.SystemdRunner(), 'show', unit, '--property=LoadState')
        if 'LoadState=not-found' not in reply.stdout:
            remaining.append(unit)
    record={'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
        'created_units':UNITS,'remaining_units':remaining,'cleanup_errors':cleanup_errors,
        'sentinel_before':before,'sentinel_after':after,
        'sentinel_alive':before['start_ticks']==after['start_ticks'] and after['state'] not in ('Z','X'),
        'scope':scope,'all_af_units_before':units_before,'all_af_units_after':inventory(),
        'jobs':[{'unit':j.unit_name,'result':None if j.result is None else j.result.__dict__} for j in JOBS]}
    Path(os.environ['AF_PROC_REAL_UNIT_LOG']).write_text(json.dumps(record,sort_keys=True,indent=2)+'\n')
    sentinel.stdin.close()
    sentinel.wait(5)
    print(json.dumps(record,sort_keys=True))
    raise SystemExit(0 if result.wasSuccessful() and not remaining and not cleanup_errors and record['sentinel_alive'] else 5)
