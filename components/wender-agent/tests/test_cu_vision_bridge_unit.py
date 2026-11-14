"""C2 subprocess tests: isolated executable stub, no vision-engine dependency."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from wender.perceive.vision_adapter import (
    BRIDGE_STATUS, FakeVisionEngine, Frame, PERSISTENT_MODE, VisionEngineAdapter,
)
from wender.perceive.vision_bridge import (
    ChangedRegion, Crop, DescribeResult, MAX_CHANGED_REGIONS, MAX_CROPS, MAX_MARKS,
    ONE_SHOT_LATENCY_NOTE, SubprocessVisionEngine, VisionBridgeError,
    changed_regions, screen_identity, select_vision_engine,
)


class TestCuVisionBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stub = self.root / 'stub-python'
        self.frame = Frame(4, 3, bytes(range(36)))
        self.payload = {
            'schema': 've.cu_describe/v1',
            'frame_sha256': hashlib.sha256(self.frame.data).hexdigest(),
            'width': 4, 'height': 3, 'changed': None,
            'changed_regions': [{'x': 1, 'y': 0, 'w': 2, 'h': 2, 'score': 0.75}],
            'crops': [{'x': 0, 'y': 0, 'w': 4, 'h': 3, 'reason': 'salient'}],
            'timing_us': 19,
        }
        self.write_stub()

    def write_stub(self, body=None):
        if body is None:
            body = """
import json, pathlib, struct, sys, zlib
args = sys.argv[1:]
assert args[:2] == ['-m', 'vadapt.cu_describe']
assert args[-1] == '--json'
pathlib.Path('args.json').write_text(json.dumps(args))
# Read the inherited anonymous PNGs while the child is alive.
for flag in ('--frame', '--prev'):
    if flag in args:
        p = args[args.index(flag) + 1]
        if p.startswith('/proc/self/fd/'):
            data = pathlib.Path(p).read_bytes()
            assert data[:8] == b'\\x89PNG\\r\\n\\x1a\\n'
            size = struct.unpack('>I', data[33:37])[0]
            raw = zlib.decompress(data[41:41+size])
            assert raw == b''.join(b'\\x00' + bytes(range(i, i+12)) for i in (0, 12, 24))
row = PAYLOAD
row['changed'] = True if '--prev' in args else None
print(json.dumps(row))
""".replace('PAYLOAD', repr(self.payload))
        self.stub.write_text('#!' + sys.executable + '\n' + body)
        self.stub.chmod(0o700)

    def engine(self, **kwargs):
        return SubprocessVisionEngine(self.root, python=self.stub, **kwargs)

    def test_parsing_typed_records_and_ram_frame(self):
        result = self.engine().describe(self.frame)
        self.assertIsInstance(result, DescribeResult)
        self.assertEqual(result.frame_sha256, self.payload['frame_sha256'])
        self.assertEqual((result.width, result.height, result.timing_us), (4, 3, 19))
        self.assertIsNone(result.changed)
        self.assertEqual(result.changed_regions, (ChangedRegion(1, 0, 2, 2, 0.75),))
        self.assertEqual(result.crops, (Crop(0, 0, 4, 3, 'salient'),))
        self.assertFalse(list(self.root.glob('*.png')))

    def test_default_python_invokes_checkout_module(self):
        package = self.root / 'vadapt'
        package.mkdir()
        (package / '__init__.py').write_text('')
        source = self.stub.read_text().split('\n', 1)[1]
        source = source.replace('args = sys.argv[1:]',
                                "args = ['-m', 'vadapt.cu_describe'] + sys.argv[1:]")
        (package / 'cu_describe.py').write_text(source)
        result = SubprocessVisionEngine(self.root).describe(self.frame)
        self.assertEqual(result.frame_sha256, self.payload['frame_sha256'])

    def test_response_must_match_request(self):
        for overrides in ({'changed': False}, {'width': 5}):
            with self.subTest(overrides=overrides):
                payload = dict(self.payload, **overrides)
                self.write_stub('print(' + repr(json.dumps(payload)) + ')\n')
                with self.assertRaises(VisionBridgeError):
                    self.engine().describe(self.frame)

    def test_existing_interface(self):
        engine = self.engine()
        self.assertIsInstance(engine, VisionEngineAdapter)
        self.assertEqual(screen_identity(self.frame, adapter=engine), self.payload['frame_sha256'][:16])
        self.assertEqual(changed_regions(self.frame, self.frame, adapter=engine),
                         [{'x': 1, 'y': 0, 'w': 2, 'h': 2, 'tiles': 1}])

    def test_masks_and_paths_passed_as_separate_args(self):
        self.engine().describe(self.root / 'frame with spaces.png',
                               prev=self.root / 'previous.png',
                               masks=[(1, 0, 2, 2), (0, 1, 4, 1)])
        args = json.loads((self.root / 'args.json').read_text())
        self.assertEqual(args, ['-m', 'vadapt.cu_describe', '--frame',
                               str(self.root / 'frame with spaces.png'), '--prev',
                               str(self.root / 'previous.png'), '--mask', '1,0,2,2',
                               '--mask', '0,1,4,1', '--json'])

    def test_timeout(self):
        self.write_stub('import time\ntime.sleep(5)\n')
        with self.assertRaisesRegex(VisionBridgeError, 'timed out'):
            self.engine(timeout=0.05).describe(self.frame)

    def test_bad_json(self):
        for output in ('not JSON', '[]', '{}', 'null', '{"schema": "wrong"}'):
            with self.subTest(output=output):
                self.write_stub('print(' + repr(output) + ')\n')
                with self.assertRaisesRegex(VisionBridgeError, 'invalid C2 JSON'):
                    self.engine().describe(self.frame)

    def test_invalid_contract_values(self):
        for key, value in (('width', True), ('height', 0), ('frame_sha256', 'invalid'),
                           ('changed', 'yes'), ('timing_us', -1),
                           ('changed_regions', [{'x': 0, 'y': 0, 'w': 1, 'h': 1, 'score': float('nan')}]),
                           ('crops', [{'x': 0, 'y': 0, 'w': 1, 'h': 1, 'reason': 'unknown'}])):
            with self.subTest(key=key):
                payload = dict(self.payload, **{key: value})
                self.write_stub('print(' + repr(json.dumps(payload)) + ')\n')
                with self.assertRaises(VisionBridgeError):
                    self.engine().describe(self.frame)

    def test_nonzero_exit(self):
        self.write_stub('import sys\nprint("bad frame", file=sys.stderr)\nsys.exit(2)\n')
        with self.assertRaisesRegex(VisionBridgeError, 'code 2: bad frame'):
            self.engine().describe(self.frame)

    def test_launch_error(self):
        self.stub.unlink()
        with self.assertRaisesRegex(VisionBridgeError, 'could not execute'):
            self.engine().describe(self.frame)

    def test_selector_and_configuration(self):
        self.assertIsInstance(select_vision_engine('fake'), FakeVisionEngine)
        with patch.dict(os.environ, {'VQA_VE_ROOT': str(self.root)}):
            engine = select_vision_engine('subprocess', python=self.stub)
            self.assertEqual(engine.ve_root, self.root)
            self.assertEqual(engine.timeout, 2.0)
            self.assertIsInstance(engine.describe(self.frame), DescribeResult)
            self.assertEqual(SubprocessVisionEngine(self.root, python=self.stub).ve_root, self.root)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(VisionBridgeError):
                select_vision_engine('subprocess')
        with self.assertRaises(VisionBridgeError):
            SubprocessVisionEngine(self.root / 'absent')
        with self.assertRaisesRegex(ValueError, 'AUSENTE'):
            select_vision_engine('persistent')
        with self.assertRaises(ValueError):
            select_vision_engine('unknown')
        self.assertEqual(BRIDGE_STATUS, 'real')
        self.assertEqual(
            (PERSISTENT_MODE, sys.modules['wender.perceive.vision_bridge'].PERSISTENT_MODE),
            ('vserve', 'vserve'),
        )
        self.assertEqual(VisionEngineAdapter.bridge_status, 'real')
        self.assertEqual(
            (VisionEngineAdapter.persistent_mode, sys.modules['wender.perceive.vision_bridge'].PERSISTENT_MODE),
            ('vserve', 'vserve'),
        )
        self.assertEqual(
            ONE_SHOT_LATENCY_NOTE,
            'persistent reuse AUSENTE in one-shot C2; subprocess spawn cost per step [TARGET]',
        )

    def test_c2_score_order(self):
        ordered = [
            {'x': 0, 'y': 0, 'w': 1, 'h': 1, 'score': 0.9},
            {'x': 1, 'y': 0, 'w': 1, 'h': 1, 'score': 0.75},
            {'x': 2, 'y': 0, 'w': 1, 'h': 1, 'score': 0.75},
            {'x': 3, 'y': 0, 'w': 1, 'h': 1, 'score': 0.1},
        ]
        payload = dict(self.payload, changed_regions=ordered)
        self.write_stub('print(' + repr(json.dumps(payload)) + ')\n')
        result = self.engine().describe(self.frame)
        self.assertEqual([r.score for r in result.changed_regions], [0.9, 0.75, 0.75, 0.1])

        unordered = [
            {'x': 0, 'y': 0, 'w': 1, 'h': 1, 'score': 0.4},
            {'x': 1, 'y': 0, 'w': 1, 'h': 1, 'score': 0.8},
        ]
        bad_payload = dict(self.payload, changed_regions=unordered)
        self.write_stub('print(' + repr(json.dumps(bad_payload)) + ')\n')
        with self.assertRaisesRegex(VisionBridgeError, 'invalid C2 JSON'):
            self.engine().describe(self.frame)

    def test_ram_destination(self):
        shm_root = Path('/dev/shm/vqa-cu')
        shm_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=shm_root) as ram_dir:
            ram_path = Path(ram_dir)
            for forbidden in ('/tmp/marks.png', '/dev/shm/outside-vqa-cu.png', self.root / 'marks.png'):
                with self.subTest(forbidden=forbidden):
                    with self.assertRaisesRegex(ValueError, 'marks path must stay under /dev/shm/vqa-cu'):
                        self.engine().describe(self.frame, marks_out=forbidden)
            escape = ram_path / 'escape_tmp'
            escape.symlink_to('/tmp', target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'marks path must stay under /dev/shm/vqa-cu'):
                self.engine().describe(self.frame, marks_out=escape / 'marks.png')

            bad_resp = dict(self.payload, marks=[], marks_png='/tmp/marks.png')
            self.write_stub('print(' + repr(json.dumps(bad_resp)) + ')\n')
            with self.assertRaisesRegex(VisionBridgeError, 'invalid C2 JSON'):
                self.engine().describe(self.frame)

            valid_marks = ram_path / 'marks.png'
            good_resp = dict(
                self.payload,
                marks=[{'id': 1, 'x': 0, 'y': 0, 'w': 2, 'h': 2, 'kind': 'button', 'score': 0.5}],
                marks_png=str(valid_marks),
            )
            self.write_stub(
                'import pathlib, sys\n'
                'args = sys.argv[1:]\n'
                'out = pathlib.Path(args[args.index("--marks-out") + 1])\n'
                'out.write_bytes(b"ram-marks")\n'
                'print(' + repr(json.dumps(good_resp)) + ')\n'
            )
            described = self.engine().describe(self.frame, marks_out=valid_marks)
            self.assertEqual(described.marks_png, str(valid_marks))
            self.assertEqual(len(described.marks), 1)

    def test_dim_mismatch(self):
        for bad_dims in ({'width': 5, 'height': 3}, {'width': 4, 'height': 4},
                         {'width': 3, 'height': 2, 'changed_regions': [], 'crops': []}):
            with self.subTest(bad_dims=bad_dims):
                payload = dict(self.payload, **bad_dims)
                self.write_stub('print(' + repr(json.dumps(payload)) + ')\n')
                with self.assertRaisesRegex(VisionBridgeError, 'frame dimensions disagree'):
                    self.engine().describe(self.frame)
        with self.assertRaisesRegex(ValueError, 'Frame dimensions do not match'):
            self.engine().describe(self.frame, prev=Frame(2, 3, bytes(18)))
        with self.assertRaisesRegex(ValueError, 'Frame dimensions do not match'):
            changed_regions(self.frame, Frame(4, 2, bytes(24)), adapter=self.engine())

    def test_scrubbed_env_and_strict_c2_caps(self):
        env_out = self.root / 'child_env.json'
        self.write_stub(
            'import json, os, pathlib\n'
            'pathlib.Path(' + repr(str(env_out)) + ').write_text(json.dumps(dict(os.environ)))\n'
            'print(' + repr(json.dumps(self.payload)) + ')\n'
        )
        with patch.dict(
            os.environ,
            {
                'MUSE_API_KEY': 'secret-muse',
                'BASETEN_API_KEY': 'secret-baseten',
                'PYTHONPATH': '/tmp/injected',
                'LD_PRELOAD': '/tmp/preload.so',
                'VQA_VE_ROOT': str(self.root),
            },
        ):
            self.engine().describe(self.frame)
        child_env = json.loads(env_out.read_text())
        for forbidden in ('MUSE_API_KEY', 'BASETEN_API_KEY', 'PYTHONPATH', 'LD_PRELOAD', 'VQA_VE_ROOT'):
            self.assertNotIn(forbidden, child_env)
        self.assertEqual(child_env.get('PYTHONDONTWRITEBYTECODE'), '1')
        self.assertTrue(child_env.get('PATH'))

        max_regions = [{'x': 0, 'y': 0, 'w': 1, 'h': 1, 'score': round(1.0 - i * 0.01, 4)}
                       for i in range(MAX_CHANGED_REGIONS)]
        max_crops = [{'x': 0, 'y': 0, 'w': 1, 'h': 1, 'reason': 'salient'}
                     for _ in range(MAX_CROPS)]
        ok_payload = dict(self.payload, changed_regions=max_regions, crops=max_crops)
        self.write_stub('print(' + repr(json.dumps(ok_payload)) + ')\n')
        res = self.engine().describe(self.frame)
        self.assertEqual(len(res.changed_regions), MAX_CHANGED_REGIONS)
        self.assertEqual(len(res.crops), MAX_CROPS)

        for bad_over in (
            {'changed_regions': max_regions + [{'x': 0, 'y': 0, 'w': 1, 'h': 1, 'score': 0.01}]},
            {'crops': max_crops + [{'x': 0, 'y': 0, 'w': 1, 'h': 1, 'reason': 'changed'}]},
        ):
            payload = dict(self.payload, **bad_over)
            self.write_stub('print(' + repr(json.dumps(payload)) + ')\n')
            with self.assertRaisesRegex(VisionBridgeError, 'invalid C2 JSON'):
                self.engine().describe(self.frame)


if __name__ == '__main__':
    unittest.main()
