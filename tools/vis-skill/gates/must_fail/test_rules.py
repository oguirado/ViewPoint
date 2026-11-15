#!/usr/bin/env python3
"""Each refusal compares the actual target counter, not only the CLI rc."""
import fcntl
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import *
loader = importlib.machinery.SourceFileLoader('vis_core', str(ROOT / 'vis'))
spec = importlib.util.spec_from_loader(loader.name, loader)
core = importlib.util.module_from_spec(spec)
loader.exec_module(core)

def check_noeffect(label, call, counter):
    before = counter.read_bytes()
    value = call()
    time.sleep(0.05)
    require(counter.read_bytes() == before, label + ' changed target')
    note(label + ': PASS; target event counter unchanged [MEASURED]')
    return value

def config_metadata():
    rows = []
    for base in (Path.home() / '.config/gedit', Path.home() / '.local/share/gedit'):
        if not base.exists():
            continue
        for parent, dirs, files in os.walk(base):
            for name in dirs + files:
                q = Path(parent) / name
                try:
                    st = q.lstat()
                    rows.append((str(q), st.st_mode, st.st_size, st.st_mtime_ns))
                except OSError:
                    pass
    return sorted(rows)

def main():
    skip_started_jobs()
    work = Path(tempfile.mkdtemp(prefix='visb-rules-', dir='/dev/shm/vqa-cu'))
    counter = work / 'counter.json'
    job = 'visb-rules-' + str(os.getpid())
    web = job + '-web'
    jobs = [job, web]
    try:
        vis('start', '--job', job, '--app', shlex.join([sys.executable, str(ROOT / 'gates/must_fail/event_target.py'), str(counter)]), '--size', '800x600')
        deadline = time.monotonic() + 4
        while not counter.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        require(counter.exists(), 'gate assertion failed')
        time.sleep(0.4)

        # Both readers must survive actual lifecycle contention; actor refusal
        # below remains the negative control. Keep frame publication serialized.
        readers = []
        try:
            with open(BASE / job / 'lock', 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                for _ in range(2):
                    readers.append(subprocess.Popen([str(VIS), 'look', '--job', job],
                                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE))
                time.sleep(0.3)
            for reader in readers:
                out, err = reader.communicate(timeout=60)
                result = json.loads(out)
                require(reader.returncode == 0 and result.get('rc') == 0 and result.get('ok') is True,
                        ('concurrent reader refused', reader.returncode, out, err))
                require(out.decode() == core.canonical(result) + '\n', 'reader protocol mismatch')
                require(result.get('verb') == 'look' and result.get('code') == 'OK', 'reader status mismatch')
                require(Path(result['frame']).is_file() and Path(result['marks_png']).is_file(),
                        'concurrent reader evidence was lost')
                note('Concurrent look: rc 0 [MEASURED]; canonical response and retained evidence')
        finally:
            for reader in readers:
                if reader.poll() is None:
                    reader.terminate()
                reader.communicate(timeout=10)

        def act(*extra, rc=5, env=None, step='refusal'):
            return vis('act', '--job', job, '--step', step, *extra, rc=rc, env=env)
        with tempfile.TemporaryDirectory(prefix='visb-ext4-') as disk:
            before = list(Path(disk).iterdir())

            def ram_guard():
                try:
                    core.tmpfs(disk)
                except core.Failure as e:
                    require(e.rc == 5, 'gate assertion failed')
                else:
                    raise AssertionError('disk filesystem passed tmpfs guard')
                with patch.object(core, 'BASE', Path(disk) / 'jobs'):
                    try:
                        core.jobpath('disk-job')
                    except core.Failure as e:
                        require(e.rc == 5, 'gate assertion failed')
                    else:
                        raise AssertionError('disk path accepted')
                require(list(Path(disk).iterdir()) == before, 'gate assertion failed')
            check_noeffect('Frames only in RAM', ram_guard, counter)
        check_noeffect('Not operator display', lambda: vis('act', '--step', 'x', '--type', 'X', '--expect-change', 'any', rc=5, env={'DISPLAY': ':1'}), counter)
        check_noeffect('Wrong adopt PID', lambda: vis('adopt', '--job', job + '-bad', '--display', state(job)['display'], '--xvfb-pid', str(os.getpid()), rc=5), counter)
        controlled = work / 'controlled-home'
        controlled.mkdir()
        sentinel = controlled / 'sentinel'
        sentinel.write_text('before')
        snap = sentinel.stat().st_mtime_ns
        sentinel.write_text('deliberate escape negative control')
        require(sentinel.stat().st_mtime_ns != snap, 'negative control did not detect a write')
        before = config_metadata()
        for i in range(2):
            name = job + '-private-' + str(i)
            jobs.append(name)
            vis('start', '--job', name, '--app', 'mkdir -p "$HOME/.vis-private"; printf private-session > "$HOME/.vis-private/marker"; exec gedit --standalone', '--size', '800x600')
            vis('act', '--job', name, '--step', 'write', '--type', 'Private session', '--expect-change', '0,50,800,500')
            private = BASE / name
            require((private / 'home/.vis-private/marker').read_text() == 'private-session', 'gate assertion failed')
            require(any((q.is_file() for q in (private / 'config').rglob('*'))), 'app did not produce private config files')
            vis('stop', '--job', name)
        after = config_metadata()
        diffs = sorted(set(before) ^ set(after))
        note('Application-owned real config metadata differences: ' + repr(diffs) + ' [MEASURED]')
        private_ok = after == before
        note('Private session gedit-owned paths and private-HOME marker gate: ' + ('PASS' if private_ok else 'BLOCKED by changes within gedit-owned paths') + ' [MEASURED]; files were not read')
        for i, text in enumerate(synthetic_keys()):
            check_noeffect('No credentials key shape ' + str(i), lambda t=text: act('--type=' + t, '--expect-change', 'any'), counter)
        for i, text in enumerate(ORDINARY_TEXTS):
            events = json.loads(counter.read_text())['events']
            act('--type=' + text, '--expect-change', 'any', rc=0, step='ordinary-' + str(i))
            require(json.loads(counter.read_text())['events'] > events, ('ordinary text did not reach input', text))
        note('Ordinary corpus reached input: ' + str(len(ORDINARY_TEXTS)) + ' strings [MEASURED]')
        check_noeffect('No secret file reads', lambda: act('--type-file', str(Path.home() / '.ssh/id_rsa'), '--expect-change', 'any'), counter)
        fixture = work / 'page.html'
        fixture.write_text('<body style="margin:0;background:white"><button style="margin:40px;width:140px;height:60px">No operation</button><input type="password" aria-label="Password"></body>')
        vis('start', '--job', web, '--web', fixture.as_uri())
        look = vis('look', '--job', web)
        password = next((n['id'] for n in look['state']['nodes'] if n['role'] == 'textbox'))
        button = next((n['id'] for n in look['state']['nodes'] if n['role'] == 'button'))
        before_frame = look['frame_sha256']
        vis('act', '--job', web, '--step', 'secret', '--ax', password, '--op', 'type', '--text', 'ordinary', '--expect-change', 'any', rc=5)
        require(vis('look', '--job', web)['frame_sha256'] == before_frame, 'gate assertion failed')
        note('Protected password field: PASS; no pixel change and no input receipt [MEASURED]')
        vis('act', '--job', web, '--step', 'focus-button', '--ax', button, '--op', 'focus', '--expect-change', 'any', rc=None)
        for i in range(3):
            before = vis('look', '--job', web)['frame_sha256']
            vis('act', '--job', web, '--step', 'no-op', '--ax', button, '--op', 'click', '--expect-change', 'any', rc=3)
            require(vis('look', '--job', web)['frame_sha256'] == before, 'gate assertion failed')
        baseline = vis('look', '--job', web)['frame_sha256']
        vis('act', '--job', web, '--step', 'no-op', '--ax', button, '--op', 'click', '--expect-change', 'any', rc=4)
        require(vis('look', '--job', web)['frame_sha256'] == baseline, 'gate assertion failed')
        note('Retries and no false green: PASS; unchanged pixels on every no-op and fourth refusal [MEASURED]')
        check_noeffect('MV_HOSTED default', lambda: act('--type', 'hosted', '--expect-change', 'any', env={'MV_HOSTED': '1'}), counter)
        meta = job + '-compat'
        jobs.append(meta)
        meta_counter = work / 'meta-counter.json'
        compat = compatibility_lock(work / 'compat')
        vis('start', '--job', meta, '--target-repo', str(compat), '--app', shlex.join([sys.executable, str(ROOT / 'gates/must_fail/event_target.py'), str(meta_counter)]), '--size', '800x600')
        deadline = time.monotonic() + 4
        while not meta_counter.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        require(meta_counter.exists(), 'gate assertion failed')
        require(state(meta)['engines']['wender_ref'].startswith('7648b258'), 'gate assertion failed')
        check_noeffect('MV_HOSTED old compatibility pinned job', lambda: vis('act', '--job', meta, '--step', 'hosted', '--type', 'hosted', '--expect-change', 'any', rc=5, env={'MV_HOSTED': '1'}), meta_counter)
        vis('stop', '--job', meta)
        # Every job on a display shares one display-wide actor lock.
        with open(BASE / ('.actor-' + hashlib.sha256(state(job)['display'].encode()).hexdigest()), 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            check_noeffect('One actor', lambda: act('--type', 'locked', '--expect-change', 'any'), counter)
        with open(BASE / job / 'lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            check_noeffect('Job lifecycle lock bounded wait', lambda: act('--type', 'locked', '--expect-change', 'any', rc=6), counter)
        # A stale identity for a live process this test spawned (never the runner) is not signalled.
        bystander = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'])
        try:
            check_noeffect('Kill only owned PID', lambda: core.terminate({'pid': bystander.pid, 'start': 'stale-start'}), counter)
            time.sleep(0.2)
            require(bystander.poll() is None, 'stale identity was signalled')
        finally:
            bystander.kill(); bystander.wait()
        j = state(job)
        if not_in_test_mode('started job scope MemoryMax=4G and MemorySwapMax=0 via systemctl'):
            require(j['scope'] is None and j['launch'] == 'test-pidns' and isinstance(j['memory_cap'], int), j)
            note('Test-mode job: no scope; inherited memory cap ' + str(j['memory_cap']) + ' bytes [MEASURED]')
        else:
            r = execute(['systemctl', '--user', 'show', j['scope'], '-p', 'MemoryMax', '-p', 'MemorySwapMax'])
            require(b'MemoryMax=4294967296' in r.stdout and b'MemorySwapMax=0' in r.stdout, 'gate assertion failed')
        huge = BASE / job / 'budget.bin'
        with open(huge, 'wb') as f:
            f.truncate(2 * 1024 ** 3 + 1)
        check_noeffect('Capped / RAM budget', lambda: act('--type', 'over-budget', '--expect-change', 'any', rc=6), counter)
        huge.unlink()
        hogjob = job + '-hog'
        hog = not not_in_test_mode('real memory hog hits its own 128M job scope (rc 6)')
        if hog:
            jobs.append(hogjob)
            trigger = work / 'hog-trigger'
            hogpid = work / 'hog-pid.json'
            vis('start', '--job', hogjob, '--app', shlex.join([sys.executable, str(ROOT / 'gates/must_fail/memory_target.py'), str(trigger), str(hogpid)]), '--size', '800x600', '--wait-title', 'VIS-B capped target', '--mem', '128M')
            hogstate = state(hogjob)
            cg = Path(hogstate['cgroup'])
            trigger.touch()
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                events = dict((line.split() for line in (cg / 'memory.events').read_text().splitlines())) if (cg / 'memory.events').exists() else {}
                if int(events.get('oom_kill', '0')):
                    break
                time.sleep(0.1)
            require(int(events.get('oom_kill', '0')) > 0, 'real hog did not hit the cap')
            check_noeffect('Scope OOM no input at sentinel', lambda: vis('act', '--job', hogjob, '--step', 'after-oom', '--type', 'not-sent', '--expect-change', 'any', rc=6), counter)
            require(core.identity(os.getpid()) and counter.exists(), 'gate assertion failed')
            note('Capped real memory hog: PASS; memory.events oom_kill positive inside 128M [MEASURED] scope, host sentinel alive; no host scope changed')
            vis('stop', '--job', hogjob)
        # Removed surfaces (F10): unknown flag or verb is rc 2 and creates no job dir.
        check_noeffect('Removed --untrusted', lambda: vis('start', '--job', job + '-untrusted', '--app', 'false', '--untrusted', rc=2), counter)
        require(not (BASE / (job + '-untrusted')).exists(), 'gate assertion failed')
        check_noeffect('Removed ask verb', lambda: vis('ask', '--job', job, '--seat', 'sol', '--q', 'Read', '--marks', rc=2), counter)
        check_noeffect('Removed --ask-file', lambda: vis('check', '--job', job, '--ask-file', rc=2), counter)
        check_noeffect('E_USAGE rc2', lambda: act('--type', 'usage', '--settle-ms', '40000', rc=2), counter)
        check_noeffect('E_ABSENT rc7', lambda: act('--ax', 'not-a-node', '--op', 'click', '--expect-change', 'any', rc=7), counter)
        p = BASE / job / 'job.json'
        old = p.read_bytes()
        p.write_text('{broken')
        try:
            check_noeffect('E_INTERNAL rc9', lambda: act('--type', 'broken', '--expect-change', 'any', rc=9), counter)
        finally:
            p.write_bytes(old)
        check_noeffect('OK rc0 read-only look', lambda: vis('look', '--job', job), counter)
        # The fixture blocks its Tk event loop; no process is signalled.
        Path(str(counter) + '.freeze').touch()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not Path(str(counter)+'.frozen').exists():
            time.sleep(0.05)
        require(Path(str(counter)+'.frozen').exists(), 'target did not block its event loop')
        check_noeffect('E_TARGET rc8', lambda: act('--type', 'frozen', '--expect-change', 'any', rc=8), counter)
        note('Must-fail: every Strategy section 5 rule represented; public rc 0,2,3,4,5,6,7,8,9 observed [MEASURED]')
        require(private_ok, 'gedit-owned host paths changed; private-session gate did not pass')
    finally:
        for name in reversed(jobs):
            cleanup(name)
        import shutil
        shutil.rmtree(work, ignore_errors=True)
if __name__ == '__main__':
    main()
