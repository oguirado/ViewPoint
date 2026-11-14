#!/usr/bin/env python3
"""An app that appears under the wrong title must leave no processes on failure."""
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import BASE, ROOT, PIDNS, execute, note, vis, require, skip_started_jobs
skip_started_jobs()
work = Path(tempfile.mkdtemp(prefix='visb-timeout-', dir='/dev/shm/vqa-cu'))
job = 'visb-timeout-' + str(os.getpid())
counter = work / 'counter.json'
try:
    vis('start', '--job', job, '--app', shlex.join([sys.executable, str(ROOT / 'gates/must_fail/event_target.py'), str(counter)]), '--wait-title', 'THIS_TITLE_IS_INTENTIONALLY_ABSENT', '--timeout', '1', rc=8)
    require(counter.exists(), 'negative test did not start a real app')
    require(not (BASE / job).exists(), 'gate assertion failed')
    if PIDNS:
        # Test mode has no scope: the real app must be gone. Its PID is only read here, never signalled.
        pid = __import__('json').loads(counter.read_text())['pid']
        try: state = Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()[0]
        except OSError: state = None
        require(state is None, ('failed start left the app process', pid, state))
        note('Failed-start cleanup (test mode): PASS; real target appeared, impossible title gave E_TARGET, no job directory or app process survived [MEASURED].')
    else:
        r = execute(['systemctl', '--user', 'list-units', '--all', '--type=scope', '--no-legend', '--plain', 'vis-' + job + '-*'])
        require(not r.stdout.strip(), 'gate assertion failed')
        note('Failed-start cleanup: PASS; real target appeared, impossible title gave E_TARGET, no job directory or scope survived [MEASURED].')
finally:
    shutil.rmtree(work)
