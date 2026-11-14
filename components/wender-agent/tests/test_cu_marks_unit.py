"""C2.1/C3.1 grounding boundaries; all generated images remain in RAM."""
import copy
import contextlib
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wender.learn.cu_loop import BridgeDescriber, ModelDecider, _action, main, run_loop
from tests import test_cu_loop_unit as loop_tests
from wender.learn.model_client import (
    ActionValidationError, CommandBackend, ModelClient, MuseBackend, SCHEMA_PATH,
    validate_action,
)
from wender.perceive.vision_bridge import VisionBridgeError, _parse_description
from tests.test_cu_model_client_unit import action, jsonl
from tests import test_cu_vision_bridge_unit as bridge_tests


class TestMarkGrounding(unittest.TestCase):
    def setUp(self):
        self.mark = dict(id=1, x=10, y=20, w=11, h=9, kind='tab', score=.9)
        self.desc = dict(schema='ve.cu_describe/v1', width=100, height=80,
                         crops=[], marks=[self.mark], marks_png='/dev/shm/vqa-cu/test-marks.png')

    def test_strict_schema_includes_required_nullable_mark(self):
        schema = json.loads(SCHEMA_PATH.read_text())
        def check(node):
            if isinstance(node, dict):
                if 'properties' in node:
                    self.assertEqual(set(node['required']), set(node['properties']))
                    self.assertIs(node['additionalProperties'], False)
                for child in node.values():
                    check(child)
            elif isinstance(node, list):
                for child in node:
                    check(child)
        check(schema)
        self.assertEqual(schema['properties']['action']['properties']['mark_id']['type'],
                         ['integer', 'null'])

    def test_resolution_and_detachment(self):
        value = action(mark_id=1)
        original = copy.deepcopy(value)
        result = validate_action(value, 100, 80, marks=[self.mark])
        self.assertEqual(result['action'], dict(kind='click', mark_id=1, x=15, y=24))
        self.assertEqual(value, original)

    def test_unknown_duplicate_and_invalid_marks_rejected(self):
        for marks in ([], [dict(self.mark, id=2)], [self.mark, self.mark],
                      [dict(self.mark, w=0)], [dict(self.mark, x=99)]):
            with self.subTest(marks=marks), self.assertRaises(ActionValidationError):
                validate_action(action(mark_id=1), 100, 80, marks=marks)
        with self.assertRaises(ActionValidationError):
            validate_action(action(mark_id=True), 100, 80, marks=[self.mark])

    def test_multiple_groundings_and_partial_coordinates_rejected(self):
        for fields in (dict(x=1, y=2, mark_id=1), dict(element_id='a', mark_id=1),
                       dict(x=1, y=2, element_id='a'), dict(x=1, mark_id=1),
                       dict(y=2, mark_id=1), dict(x=1, y=2, element_id='a', mark_id=1)):
            with self.subTest(fields=fields), self.assertRaises(ActionValidationError):
                validate_action(action(**fields), 100, 80, marks=[self.mark])

    def test_images_marks_frame_crops_and_prompt_for_both_backends(self):
        for images in ('marks', 'full'):
            for backend in (MuseBackend('.'), CommandBackend(['stub', '{images}', '{prompt_file}'], '.')):
                captured = {}
                def run(argv, *args, **kwargs):
                    captured['images'] = [argv[i+1] for i, arg in enumerate(argv) if arg == '--image']
                    prompt_path = argv[argv.index('--prompt-file')+1] if '--prompt-file' in argv else argv[-1]
                    captured['prompt'] = Path(prompt_path).read_text()
                    return jsonl(action(mark_id=1)) if isinstance(backend, MuseBackend) else json.dumps(action(mark_id=1))
                with patch('wender.learn.model_client._run', side_effect=run):
                    result = ModelClient(backend).act('Inbox', '/dev/shm/frame.png', self.desc,
                                                     crop_paths=['/dev/shm/crop.png'], images=images)
                self.assertEqual(captured['images'], [self.desc['marks_png']] +
                                 (['/dev/shm/frame.png', '/dev/shm/crop.png'] if images == 'full' else []))
                self.assertIn('Prefer mark_id for clicks', captured['prompt'])
                self.assertIn('"id": 1', captured['prompt'])
                self.assertEqual(result['action']['x'], 15)


    def test_backend_cannot_mutate_grounding_evidence(self):
        class Backend:
            def generate(inner, request):
                request['marks'][0]['x'] = 50
                request['describe']['marks'][0]['x'] = 60
                return action(mark_id=1)
        result = ModelClient(Backend()).act('Inbox', '/dev/shm/frame.png', self.desc)
        self.assertEqual(result['action']['x'], 15)

    def test_loop_decision_revalidation_retains_mark_id(self):
        class Backend:
            def generate(inner, request):
                return action(mark_id=1)
        decider = ModelDecider(Backend())
        self.addCleanup(decider.close)
        frame = dict(path='/dev/shm/frame.png', width=100, height=80)
        for feedback in (None, {'validator_message': 'retry'}):
            result = decider.decide(goal='Inbox', frame=frame, description=self.desc, feedback=feedback)
            self.assertNotIn('x', result['action'])
            self.assertEqual(_action(result, frame, self.desc['marks']),
                             dict(kind='click', mark_id=1, x=15, y=24))

    def test_live_cli_marks_default_and_opt_out(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm') as ram:
            for flags, enabled in (([], True), (['--no-marks'], False)):
                with patch('wender.learn.cu_loop.BridgeDescriber') as bridge, \
                     patch('wender.learn.cu_loop.ReplayBackend'), \
                     patch('wender.learn.cu_loop.run_loop', return_value={'stop_reason': 'done'}), \
                     contextlib.redirect_stdout(io.StringIO()):
                    status = main(['--goal', 'Inbox', '--capsule', 'srf:box',
                                   '--decider', 'replay:unused', '--trace', ram+'/trace', *flags])
                self.assertEqual(status, 0)
                self.assertIs(bridge.call_args.kwargs['marks'], enabled)

    def test_full_loop_trace_keeps_resolved_mark(self):
        fixture = loop_tests.TestCuLoop()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        capsule = fixture.capsule(['before', 'after'])
        mark = dict(self.mark, x=2, y=3, w=5, h=7)
        description = dict(self.desc, width=16, height=16, marks=[mark],
                           changed=None, changed_regions=[])
        class Backend:
            def generate(inner, request):
                return action(mark_id=1)
        class Describer:
            def describe(inner, frame, *, prev=None):
                return dict(description, changed=None if prev is None else True)
        trace = io.StringIO()
        summary = run_loop(capsule, Describer(), ModelDecider(Backend()), goal='Inbox',
                           trace=trace, max_steps=1, clock=fixture.clock,
                           sleep=fixture.clock.sleep)
        self.assertEqual(summary['verified_ok'], 1)
        expected = dict(kind='click', mark_id=1, x=4, y=6)
        self.assertEqual(capsule.inputs, [expected])
        self.assertEqual(json.loads(trace.getvalue())['model']['action'], expected)


class TestMarksBridge(unittest.TestCase):
    def test_stub_describe_parses_marks_and_ram_output(self):
        # Reuse the executable C2 stub fixture without inheriting its test cases.
        fixture = bridge_tests.TestCuVisionBridge()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = Path('/dev/shm/vqa-cu')
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as ram:
            target = Path(ram) / 'marks.png'
            payload = dict(fixture.payload, marks=[dict(id=1, x=1, y=0, w=2, h=2,
                                                       kind='tab', score=.8)], marks_png=str(target))
            fixture.write_stub("import pathlib,sys\na=sys.argv\np=pathlib.Path(a[a.index('--marks-out')+1])\n"
                               "p.write_bytes(b'annotated stub')\nprint(" + repr(json.dumps(payload)) + ")\n")
            result = fixture.engine().describe(fixture.frame, marks_out=target)
            self.assertEqual(result.marks[0].id, 1)
            self.assertEqual((result.marks[0].x, result.marks[0].w), (1, 2))
            self.assertEqual(result.marks_png, str(target))
            self.assertTrue(target.is_file())
            with self.assertRaises(ValueError):
                fixture.engine().describe(fixture.frame, marks_out=fixture.root / 'disk.png')

    def test_malformed_mark_response_rejected(self):
        fixture = bridge_tests.TestCuVisionBridge()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        mark = dict(id=1, x=1, y=0, w=2, h=2, kind='tab', score=.8)
        payload = dict(fixture.payload, marks=[mark], marks_png='/dev/shm/vqa-cu/marks.png')
        for overrides in ({'id': True}, {'id': 2}, {'kind': 'bad'}, {'x': 4}, {'score': float('nan')}):
            with self.subTest(overrides=overrides), self.assertRaises(VisionBridgeError):
                _parse_description(json.dumps(dict(payload, marks=[dict(mark, **overrides)])))
        for overrides in ({'marks_png': '/tmp/disk.png'}, {'marks': None}, {'marks': [mark, mark]}):
            with self.subTest(overrides=overrides), self.assertRaises(VisionBridgeError):
                _parse_description(json.dumps(dict(payload, **overrides)))

    def test_default_enabled_and_disabled_describe_options(self):
        with patch('wender.learn.cu_loop.SubprocessVisionEngine') as engine:
            engine.return_value.describe.return_value = __import__(
                'wender.perceive.vision_bridge', fromlist=['DescribeResult']).DescribeResult(
                    've.cu_describe/v1', 'a'*64, 4, 3, None, (), (), 1)
            for enabled in (True, False):
                describer = BridgeDescriber('.', marks=enabled)
                describer.describe({'path': '/dev/shm/frame.png'})
                options = engine.return_value.describe.call_args.kwargs
                self.assertEqual('marks_out' in options, enabled)
                if enabled:
                    self.assertTrue(options['marks_out'].is_relative_to('/dev/shm/vqa-cu'))
                del describer
