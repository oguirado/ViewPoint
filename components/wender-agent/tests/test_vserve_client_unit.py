# @module {"id":"wa.perceive.vserve_client_tests","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.vserve_client"],"book":"HB-WA-s1-perceive"}
"""Hosted protocol fixtures and negative controls; no external engine required."""
import hashlib
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from wender.perceive.vision_adapter import Frame
from wender.perceive.vserve_client import VServeClient, VServeError


STUB = r'''
import hashlib, json, os, pathlib, sys, time
root = pathlib.Path.cwd()
mode = (root / 'mode').read_text()
with (root / 'starts').open('a') as f: f.write(str(os.getpid()) + '\n')
(root / 'environment').write_text(json.dumps(dict(os.environ)))
hello = False
for line in sys.stdin:
    obj = json.loads(line)
    with (root / 'requests').open('a') as f: f.write(json.dumps(obj) + '\n')
    op = obj['op']
    if mode == 'eof' or mode == 'restart' and len((root / 'starts').read_text().splitlines()) == 1:
        sys.exit(0)
    if mode == 'timeout': time.sleep(60)
    if op == 'hello':
        hello = True
        result = dict(schema='ve.vserve.hello/v1', capabilities=['describe', 'frame_raw'],
                      raw_shm=True, frame_raw=mode != 'badhello', ring=False,
                      max_control_line_bytes=65536, max_frame_bytes=33554432, max_sessions=16)
    else:
        assert hello, 'MUT-HELLO-SKIP'
        assert 'frame_raw' in obj and 'frame' not in obj, 'MUT-PNG-REGRESS'
        path, w, h = obj['frame_raw'].rsplit(',', 2)
        p = pathlib.Path(path)
        assert p.parent == pathlib.Path('/dev/shm')
        assert p.stat().st_mode & 0o777 == 0o600
        data = p.read_bytes()
        assert len(data) == int(w)*int(h)*3
        (root / 'pixels').write_bytes(data)
        if 'prev_raw' in obj:
            pp, pw, ph = obj['prev_raw'].rsplit(',', 2)
            assert pathlib.Path(pp).read_bytes() == data
        result = dict(schema='ve.cu_describe/v1', frame_sha256=hashlib.sha256(data).hexdigest(),
                      width=int(w), height=int(h), changed=False if 'prev_raw' in obj else None,
                      changed_regions=[], crops=[], timing_us=1)
        if mode == 'baddigest': result['frame_sha256'] = '0'*64
        if mode == 'badorder': result['changed_regions'] = [dict(x=0,y=0,w=1,h=1,score=s) for s in (0.1,0.9)]
        if mode == 'caps': result['crops'] = [dict(x=0,y=0,w=1,h=1,reason='salient')]*9
        if 'marks_out' in obj:
            pathlib.Path(obj['marks_out']).write_bytes(b'png')
            result.update(marks=[], marks_png=obj['marks_out'])
    row = dict(schema='ve.vserve.reply/v1', request_id=obj['request_id'], op=op,
               ok=True, code='OK', rc=0, detail={}, result=result)
    if mode == 'reqid': row['request_id'] += 10
    if mode == 'badrc': row['rc'] = True
    if mode in ('stale', 'unavailable', 'budget'):
        rc, code = {'stale': (4, 'E_STALE'), 'unavailable': (5, 'E_UNAVAILABLE'), 'budget': (6, 'E_BUDGET')}[mode]
        row.update(ok=False, rc=rc, code=code, result=None, detail={'reason': mode})
    wire = json.dumps(row) + '\n'
    # Deliberately split replies to exercise sticky-buffer reads.
    sys.stdout.write(wire[:7]); sys.stdout.flush()
    sys.stdout.write(wire[7:]); sys.stdout.flush()
'''


class ServerFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'vadapt').mkdir()
        (self.root / 'vadapt/__init__.py').write_text('')
        (self.root / 'vadapt/vserve.py').write_text(STUB)
        self.mode('ok')
        self.frame = Frame(2, 2, bytes(range(12)))

    def mode(self, value):
        (self.root / 'mode').write_text(value)

    def client(self, **kwargs):
        kwargs.setdefault("python", sys.executable)
        client = VServeClient(self.root, **kwargs)
        self.addCleanup(client.close)
        return client

    def requests(self):
        return [json.loads(s) for s in (self.root / 'requests').read_text().splitlines()]


class VServeClientTests(ServerFixture):
    def test_hello_required(self):
        client = self.client()
        client.describe(self.frame)
        self.assertEqual([r['op'] for r in self.requests()], ['hello', 'describe'])
        self.mode('badhello')
        other = self.client()
        with self.assertRaisesRegex(VServeError, 'hello'):
            other.describe(self.frame)
        self.assertIsNone(other._proc)

    def test_frame_uses_raw_shm(self):
        client = self.client()
        self.assertIsNone(client._proc)
        for _ in range(2):
            result = client.describe(self.frame, self.frame)
            self.assertEqual(result['frame_sha256'], hashlib.sha256(self.frame.data).hexdigest())
        self.assertEqual((self.root / 'pixels').read_bytes(), self.frame.data)
        requests = self.requests()
        self.assertEqual(len((self.root / 'starts').read_text().splitlines()), 1)
        for request in requests[1:]:
            self.assertNotIn('frame', request)
            self.assertFalse(Path(request['frame_raw'].rsplit(',', 2)[0]).exists())
            self.assertIn('prev_raw', request)
        child = client._proc
        client.close()
        self.assertIsNotNone(child.returncode)
        with self.assertRaisesRegex(VServeError, 'closed'):
            client.describe(self.frame)

    def test_request_id_must_match(self):
        self.mode('reqid')
        client = self.client()
        with self.assertRaisesRegex(VServeError, 'reply'):
            client.describe(self.frame)
        self.assertIsNone(client._proc)

    def test_timeout_kills_and_reaps(self):
        self.mode('timeout')
        client = self.client(timeout=0.3)
        children = []
        from wender.perceive import vserve_client as module
        original = module.subprocess.Popen
        def capture(*a, **kw):
            child = original(*a, **kw)
            children.append(child)
            return child
        with patch.object(module.subprocess, 'Popen', side_effect=capture):
            started = time.monotonic()
            with self.assertRaisesRegex(VServeError, 'timed out'):
                client.describe(self.frame)
        self.assertLess(time.monotonic() - started, 2)
        child = children[0]
        self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait()))
        self.assertIsNotNone(child.returncode, 'MUT-NO-REAP')
        with self.assertRaises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)
        self.assertIsNone(client._proc)
        self.assertEqual(client.restarts, 0)

    def test_environment_is_scrubbed(self):
        with patch.dict(os.environ, {'WA_SECRET_SENTINEL': 'secret', 'PYTHONINSPECT': '1', 'PYTHONPATH': '/bad'}):
            self.client().describe(self.frame)
        env = json.loads((self.root / 'environment').read_text())
        self.assertNotIn('WA_SECRET_SENTINEL', env, 'MUT-ENV-INHERIT')
        self.assertNotIn('PYTHONINSPECT', env)
        self.assertEqual(env['PYTHONPATH'], str(self.root))
        self.assertEqual(env['PYTHONNOUSERSITE'], '1')
        self.assertEqual(env['PYTHONSAFEPATH'], '1')

    def test_bounded_restart_fresh_ids(self):
        self.mode('restart')
        client = self.client()
        client.describe(self.frame)
        ids = [r['request_id'] for r in self.requests()]
        self.assertEqual(ids, sorted(set(ids)))
        self.assertEqual(client.restarts, 1)
        self.mode('eof')
        client._stop()
        with self.assertRaises(VServeError):
            client.describe(self.frame)
        self.assertEqual(client.restarts, 1)

    def test_typed_refusals_and_retry_policy(self):
        for mode, code, attempts in (('stale', 'E_STALE', 1), ('unavailable', 'E_UNAVAILABLE', 2),
                                     ('budget', 'E_BUDGET', 1)):
            with self.subTest(mode=mode):
                self.mode(mode)
                client = self.client()
                before = len(self.requests()) if (self.root / 'requests').exists() else 0
                with self.assertRaisesRegex(VServeError, code):
                    client.describe(self.frame)
                self.assertEqual(len(self.requests()) - before, attempts)
                self.assertEqual(client.restarts, attempts - 1)
                self.assertIsNone(client._proc)

    def test_serialized_concurrent_calls(self):
        from concurrent.futures import ThreadPoolExecutor
        client = self.client()
        with ThreadPoolExecutor(max_workers=3) as pool:
            rows = list(pool.map(lambda _: client.describe(self.frame), range(6)))
        self.assertTrue(all(row['frame_sha256'] == hashlib.sha256(self.frame.data).hexdigest() for row in rows))
        self.assertEqual([r['request_id'] for r in self.requests()], list(range(1, 8)))
        self.assertEqual(len((self.root / 'starts').read_text().splitlines()), 1)

    def test_configuration_and_envelope_refused(self):
        for kwargs in ({'python': 'python3 -I'}, {'max_restarts': True}, {'timeout': float('nan')}):
            with self.assertRaises(ValueError):
                self.client(**kwargs)
        configured = self.client()
        (self.root / 'sitecustomize.py').write_text('raise RuntimeError()')
        with self.assertRaisesRegex(ValueError, 'hook'):
            self.client()
        with self.assertRaisesRegex(ValueError, 'hook'):
            configured.describe(self.frame)
        self.assertIsNone(configured._proc)
        (self.root / 'sitecustomize.py').unlink()
        self.mode('badrc')
        with self.assertRaises(VServeError):
            self.client().describe(self.frame)

    def test_hook_package_dir_refused(self):
        """MUT-HOOK-PKG: exact-name startup packages must be refused at spawn too."""
        for name in ('sitecustomize', 'usercustomize'):
            with self.subTest(name=name):
                client = self.client()
                package = self.root / name
                package.mkdir()
                (package / '__init__.py').write_text('raise RuntimeError("hook executed")')
                try:
                    with self.assertRaisesRegex(ValueError, 'hook'):
                        self.client()
                    with self.assertRaisesRegex(ValueError, 'hook'):
                        client.describe(self.frame)
                    self.assertIsNone(client._proc)
                finally:
                    (package / '__init__.py').unlink()
                    package.rmdir()

    def test_shm_oserror_is_unavailable(self):
        """MUT-SHM-RAW-OSERROR: allocation and write errors share transport policy."""
        for target in ('tempfile.mkstemp', 'os.fchmod'):
            with self.subTest(target=target):
                client = self.client()
                with patch('wender.perceive.vserve_client.' + target, side_effect=OSError('shm full')) as fail:
                    with self.assertRaises(VServeError) as caught:
                        client.describe(self.frame)
                self.assertTrue(caught.exception.unavailable)
                self.assertIsInstance(caught.exception.__cause__, OSError)
                self.assertEqual(fail.call_count, 2)
                self.assertEqual(client.restarts, 1)
                self.assertIsNone(client._proc)

    def test_shm_write_oserror_is_unavailable(self):
        client = self.client(max_restarts=0)
        from wender.perceive import vserve_client as module
        original = module.os.fdopen
        opened = []
        class FailingWrite:
            def __init__(self, fd, mode):
                self.stream = original(fd, mode)
                opened.append(self.stream)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def fileno(self):
                return self.stream.fileno()
            def write(self, data):
                raise OSError('shm write failed')
        with patch.object(module.os, 'fdopen', side_effect=FailingWrite):
            with self.assertRaises(VServeError) as caught:
                client.describe(self.frame)
        self.assertTrue(caught.exception.unavailable)
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertTrue(all(stream.closed for stream in opened))

    def test_missing_shm_cleanup_preserves_result(self):
        client = self.client()
        original = client._roundtrip
        def consume(op, fields, deadline):
            result = original(op, fields, deadline)
            if op == 'describe':
                os.unlink(fields['frame_raw'].rsplit(',', 2)[0])
            return result
        with patch.object(client, '_roundtrip', side_effect=consume):
            self.assertEqual(client.describe(self.frame)['width'], 2)

    def test_stop_bounds_every_reap_and_closes_streams_first(self):
        """MUT-UNBOUNDED-REAP: a wedged child cannot hold the lock forever."""
        client = self.client()
        child = Mock()
        child.poll.return_value = None
        streams = (child.stdin, child.stdout, child.stderr)
        timeouts = []
        def wait(*, timeout):
            self.assertTrue(all(stream.close.called for stream in streams))
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 0.5)
            timeouts.append(timeout)
            raise subprocess.TimeoutExpired('stub', timeout)
        child.wait.side_effect = wait
        client._proc = child
        with self.assertRaises(subprocess.TimeoutExpired):
            client.close()
        self.assertEqual(timeouts, [0.5, 0.2])
        child.terminate.assert_called_once_with()
        child.kill.assert_called_once_with()
        self.assertIs(client._proc, child)
        with self.assertRaisesRegex(VServeError, 'closed'):
            client.describe(self.frame)
        self.assertTrue(client._lock.acquire(blocking=False))
        client._lock.release()
        child.wait.side_effect = None
        child.poll.return_value = -9
        client.close()
        self.assertIsNone(client._proc)
