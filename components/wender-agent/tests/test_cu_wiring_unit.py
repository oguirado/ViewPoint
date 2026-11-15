"""Real CLI wiring with isolated C2 and Muse subprocess stubs; images stay in RAM."""
import hashlib
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wender.learn.cu_loop import (BridgeDescriber, FakeCapsule, ModelDecider,
                                  _decode_png, _encode_png, main, run_loop)
from wender.learn.model_client import MuseBackend, ReplayBackend
from tests.test_cu_loop_unit import proposal


def stub_ve(root):
    package = root / 'vadapt'
    package.mkdir()
    (package / '__init__.py').write_text('')
    (package / 'cu_describe.py').write_text('''import argparse, json, os, struct
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('--marks-out'); p.add_argument('--frame'); p.add_argument('--prev'); p.add_argument('--mask', action='append'); p.add_argument('--json', action='store_true')
a = p.parse_args()
if os.environ.get('CU_STUB_LOG'):
    with open(os.environ['CU_STUB_LOG'], 'a') as f: f.write(json.dumps(vars(a)) + '\\n')
if a.marks_out: Path(a.marks_out).write_bytes(Path(a.frame).read_bytes())
w,h=struct.unpack('>II',Path(a.frame).read_bytes()[16:24])
print(json.dumps(dict(schema='ve.cu_describe/v1', frame_sha256='a'*64,
    width=w, height=h, changed=None if a.prev is None else True,
    changed_regions=[], **(dict(marks=[], marks_png=a.marks_out) if a.marks_out else {}),
    crops=[dict(x=1 if w>2 else 0,y=1 if h>2 else 0,w=min(2,w),h=min(2,h),reason='salient')]*5, timing_us=1)))
''')


class TestWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/dev/shm')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rgb = bytes(range(36))
        for i in range(5):
            (self.root / f'{i}.png').write_bytes(_encode_png(self.rgb, 4, 3))
        (self.root / 'index.json').write_text(json.dumps([
            dict(seq=i, png=f'{i}.png', state_sha256=hashlib.sha256(str(i).encode()).hexdigest(), tick=i,
                 elements=[dict(id='a', role='button', name='Alpha',
                                bbox=dict(x=0,y=0,w=2,h=2), raw_ax='excluded')]) for i in range(5)]))
        stub_ve(self.root)

    def test_crop_pixels_request_history_and_cleanup(self):
        class Capture:
            def __init__(self):
                self.requests = []
            def generate(inner, request):
                inner.requests.append(request)
                return proposal() if len(inner.requests) < 5 else proposal('done')
        backend = Capture()
        decider = ModelDecider(backend, images='full')
        with patch.dict(os.environ, CU_STUB_LOG=str(self.root / 'log')):
            summary = run_loop(FakeCapsule(self.root), BridgeDescriber(self.root), decider,
                               goal='test goal', trace=io.StringIO(), max_ms=30000)
        self.assertEqual(summary['verified_ok'], 4)
        self.assertFalse(decider.crops_dir.exists())
        request = backend.requests[-1]
        self.assertEqual(request['goal'], 'test goal')
        self.assertEqual(request['frame_path'], str(self.root / '4.png'))
        self.assertEqual(len(request['crop_paths']), 4)
        self.assertEqual(len(request['trace_steps']), 3)
        self.assertEqual([r['step'] for r in request['trace_steps']], [2,3,4])
        self.assertEqual(set(request['elements'][0]), {'id','role','name','bbox'})
        self.assertEqual(request['describe']['schema'], 've.cu_describe/v1')
        self.assertTrue(all(Path(p).is_relative_to('/dev/shm/vqa-cu') for p in request['crop_paths']))
        # Retain one run to compare every cropped RGB pixel against the source.
        decider = ModelDecider(Capture(), keep_crops=True, images='full')
        self.addCleanup(lambda: __import__('shutil').rmtree(decider.crops_dir.parent))
        frame = FakeCapsule(self.root).frame()
        decider.decide(goal='pixels', frame=frame, description=BridgeDescriber(self.root).describe(frame))
        pixels, width, height = _decode_png(next(decider.crops_dir.glob('*.png')))
        self.assertEqual((width, height), (2,2))
        self.assertEqual(pixels, self.rgb[15:21] + self.rgb[27:33])
        log = [json.loads(line) for line in (self.root / 'log').read_text().splitlines()]
        self.assertIsNone(log[0]['mask'])
        self.assertEqual(log[1]['prev'], str(self.root / '0.png'))

    def test_cli_muse_executable_and_replay_selection(self):
        # /dev/shm is noexec on some hosts: only executable text lives in the worktree.
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as bindir:
            executable = Path(bindir) / 'muse'
            executable.write_text('#!/usr/bin/env python3\nimport sys,json,os\nfrom pathlib import Path\na=sys.argv[1:]\np=Path(a[a.index("--prompt-file")+1]).read_text()\nPath(os.environ["CU_MUSE_LOG"]).write_text(json.dumps(dict(argv=a,prompt=p)))\nprint(json.dumps(dict(payload_type="run.output.delta",payload=dict(text='+repr(json.dumps(proposal('done')))+'))))\n')
            executable.chmod(0o700)
            with patch.dict(os.environ, PATH=bindir + os.pathsep + os.environ['PATH'],
                            CU_MUSE_LOG=str(self.root / 'muse.json'),
                            CU_STUB_LOG=str(self.root / 've.jsonl')):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    status = main(['--goal','finish','--capsule','fake:'+str(self.root),
                                   '--decider','muse','--ve-root',str(self.root),
                                   '--mask','0,0,1,1','--effort','medium',
                                   '--muse-workspace',str(self.root), '--trace',str(self.root/'trace')])
                self.assertEqual(status, 0)
                self.assertEqual(json.loads(output.getvalue())['stop_reason'], 'done')
                call = json.loads((self.root / 'muse.json').read_text())
                self.assertIn('muse-spark-1.3', call['argv'])
                self.assertIn('medium', call['argv'])
                self.assertEqual(call['argv'].count('--image'), 5)
                self.assertIn('finish', call['prompt'])
                self.assertNotIn('raw_ax', call['prompt'])
                self.assertEqual(json.loads((self.root/'ve.jsonl').read_text())['mask'],
                                 ['0,0,1,1'])
        replay = self.root / 'replay.json'
        replay.write_text(json.dumps([proposal('done')]))
        with patch('wender.learn.cu_loop.ReplayBackend', wraps=ReplayBackend) as backend:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--goal','replay','--capsule','fake:'+str(self.root),
                                       '--decider','replay:'+str(replay),'--ve-root',str(self.root),
                                       '--trace',str(self.root/'trace')]), 0)
            backend.assert_called_once_with(str(replay))

    def test_failure_cleans_crops_without_input(self):
        replay = self.root / 'replay.json'
        replay.write_text('[]')
        decider = ModelDecider(ReplayBackend(replay))
        capsule = FakeCapsule(self.root)
        summary = run_loop(capsule, BridgeDescriber(self.root), decider, goal='x', trace=io.StringIO())
        self.assertEqual(summary['failure']['stage'], 'decision')
        self.assertEqual(summary['failure']['error_class'], 'BackendError')
        self.assertFalse(decider.crops_dir.exists())
        self.assertEqual(capsule.inputs, [])

    def test_live_start_array_stop_finally_and_summary(self):
        from tools import cu_live
        import subprocess
        app = self.root / 'app'
        app.mkdir()
        (app / 'holdings-h2l').touch()
        container = 'srf-ng-hcap-hc-012345abcdef-1'
        slot = dict(container=container, slot=1, run_id='hc-012345abcdef',
                    display=':100', width=1280, height=900, lane_socket='/run/lane',
                    frames_dir=str(self.root / 'frames'), ready_ms=1,
                    ready_line='HOLDINGS_READY 7 0')
        terminal = dict(stop_reason='done', outcome='model_claimed_done',
                        goal_verified=False, goal_evidence='not_reached',
                        terminal_observation='The inbox shows the first item: Alpha',
                        final_frame=dict(path=str(self.root / '0.png'), seq=0,
                                         sha256_rgb24=hashlib.sha256(self.rgb).hexdigest(),
                                         width=4, height=3, generation='fixture', capture_mono_ns=1))
        def arguments(directory):
            return ['--app', str(app), '--scenario', 'inbox-driven', '--seed', '7',
                    '--goal', 'open inbox and read first item', '--ve-root', str(self.root),
                    '--muse-workspace', str(self.root), '--run-dir', str(directory)]
        def transport_response(argv, **kwargs):
            self.assertFalse(kwargs['check'])
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps([slot])
                                               if argv[1] == 'start' else '{}')
        def loop(argv):
            self.assertIn('srf:' + container, argv)  # Container came from the start array.
            self.assertIn('muse', argv)
            self.assertIn(str(self.root), argv)
            self.assertNotIn('--mask', argv)
            print(json.dumps(terminal))
            return 0
        with patch.object(cu_live.subprocess, 'run', side_effect=transport_response) as transport:
            directory = self.root / 'live'
            with patch.object(cu_live, 'loop_main', side_effect=loop) as loops:
                with contextlib.redirect_stdout(io.StringIO()):
                    result = cu_live.main(arguments(directory))
            self.assertEqual(result, 0)
            loops.assert_called_once()
            self.assertEqual(transport.call_args_list[0].args[0][1:],
                             ['start', '--slots', '1', '--app', str(app),
                              '--scenario', 'inbox-driven', '--seed', '7'])
            self.assertEqual(transport.call_count, 2)
            self.assertEqual(transport.call_args.args[0][1:], ['stop', '--container', container])
            summary = json.loads((directory / 'summary.json').read_text())
            self.assertEqual(summary['terminal_summary'], terminal)
            self.assertEqual(summary['requested']['goal'], 'open inbox and read first item')
            self.assertEqual(summary['requested']['scenario'], 'inbox-driven')
            self.assertEqual(summary['requested']['seed'], 7)
            self.assertTrue(summary['cleanup']['ok'])
            self.assertTrue(summary['transport_completed'])
            self.assertTrue(summary['model_claimed_done'])
            self.assertEqual(summary['goal_evidence'], 'not_reached')
            # Reuse cannot append a second episode or change the original evidence.
            previous = (directory / 'summary.json').read_bytes()
            transport.reset_mock()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertNotEqual(cu_live.main(arguments(directory)), 0)
            transport.assert_not_called()
            self.assertEqual((directory / 'summary.json').read_bytes(), previous)
            # A fresh failing episode must stop in finally and still save its summary.
            failed = self.root / 'live-failed'
            with patch.object(cu_live, 'loop_main', side_effect=RuntimeError('failure')) as loops:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertNotEqual(cu_live.main(arguments(failed)), 0)
            loops.assert_called_once()
            self.assertEqual(transport.call_count, 2)
            self.assertEqual(transport.call_args.args[0][1:], ['stop', '--container', container])
            summary = json.loads((failed / 'summary.json').read_text())
            self.assertEqual(summary['primary_outcome']['stage'], 'loop')
            self.assertEqual(summary['primary_outcome']['error_class'], 'RuntimeError')
            self.assertTrue(summary['cleanup']['ok'])
            self.assertIsNone(summary['terminal_summary'])
