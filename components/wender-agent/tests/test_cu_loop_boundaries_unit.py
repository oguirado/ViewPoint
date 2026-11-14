# @module {"id":"wa.tests.test_cu_loop_boundaries_unit","layer":2,"owner":"LEARN","deps":["wa.learn.cu_loop","wa.learn.model_client","wa.perceive.vision_bridge","wa.tests.test_cu_loop_unit"],"book":"HB-WA-s4-learn"}
"""F-LOOP regression gates: real C1 shapes, passive evidence, stage failures."""
import io
import json
import subprocess
import unittest
from unittest.mock import patch

from tests import test_cu_loop_unit as fixtures
from tests.test_cu_loop_unit import Clock, Describer, proposal
from wender.learn.cu_loop import (SrfCapsule, ReplayDecider, ModelDecider,
                                  _validate_frame, run_loop, main)
from wender.learn.model_client import ModelTimeoutError, MuseBackend
from wender.perceive.vision_bridge import VisionBridgeError


# @anchor:wa.tests.test_cu_loop_boundaries_unit.TestLoopBoundaries
class TestLoopBoundaries(unittest.TestCase):
    # @anchor:wa.tests.test_cu_loop_boundaries_unit.TestLoopBoundaries.setUp
    def setUp(self):
        self.__dict__.pop("transport", None)
        self.fixture = fixtures.TestCuLoop()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.inner = self.fixture.capsule(['a', 'b'], colors=[(0, 0, 0)] * 2,
            elements=[{'id': 'open', 'bbox': {'x': 0, 'y': 0, 'w': 2, 'h': 2}}])
        self.trace = io.StringIO()
        self.clock = Clock()
        from tests.test_cu_loop_unit import _TrustedDispatcher
        self.cap = SrfCapsule('box', dispatcher=_TrustedDispatcher())
        self.inputs = []

    def transport(self, args, **kwargs):
        if args[1] == 'input':
            self.inputs.append(args)
            return self.inner.input({'kind': 'key', 'keysym': 'Return'})
        frame = self.inner.frame()
        frame['state'] = frame.pop('lane')
        frame['generation'] = 'go-generation'
        return frame

    def run_case(self, actions=None, cap=None, desc=None, decider=None, **kwargs):
        with patch('wender.learn.cu_loop._json_command', side_effect=self.transport):
            return run_loop(cap or self.cap, desc or Describer(), decider or ReplayDecider(actions or [proposal()]),
                goal='read first item', trace=self.trace, clock=self.clock, sleep=self.clock.sleep,
                max_steps=kwargs.pop('max_steps', 1), **kwargs)

    def test_go_state_grounding_and_state_only_verification(self):
        summary = self.run_case([proposal('click', element_id='open')])
        row = json.loads(self.trace.getvalue())
        self.assertEqual(row['model']['action']['x'], 1)
        self.assertEqual(row['verify']['method'], 'state_sha256')
        self.assertTrue(row['verify']['ok'])
        self.assertEqual(summary['verified_ok'], 1)
        self.assertEqual(len(self.inputs), 1)

    def test_unchanged_state_changing_pixels_is_not_state_evidence(self):
        self.inner = self.fixture.capsule(['a', 'a'])
        summary = self.run_case()
        self.assertEqual(summary['verified_ok'], 0)
        self.assertFalse(json.loads(self.trace.getvalue())['verify']['ok'])

    def test_invalid_proposals_never_inject(self):
        for action in [proposal('type', text='hello'), proposal('key', keysym='Return;bad'),
                       proposal('key', keysym='a'*33), proposal('click', x=16, y=0),
                       proposal(expectation={'kind': 'changed_region'}),
                       {**proposal(), 'unknown': True}]:
            with self.subTest(action=action):
                summary = self.run_case([action])
                self.assertEqual(summary['failure']['stage'], 'validation')
                self.assertEqual(self.inputs, [])
                self.assertEqual(self.trace.getvalue(), '')

    def test_missing_state_before_input(self):
        self.inner = self.fixture.capsule([None, None])
        summary = self.run_case(cap=self.inner)
        self.assertEqual(summary['failure']['stage'], 'capture')
        self.assertEqual(self.inner.inputs, [])

    def test_invalid_frame_evidence(self):
        frame = self.inner.frame()
        for changes in [{'state': []}, {'state': {**frame['lane'], 'tick': True}},
                        {'sha256_rgb24': 'a'*64}, {'width': 17},
                        {'lane': {**frame['lane'], 'revision': None}},
                        {'state': {**frame['lane'], 'state_sha256': 'f'*64}}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                _validate_frame({**frame, **changes})

    def test_legacy_fixture_revision_defaults_to_tick(self):
        self.assertNotIn('revision', self.inner.index[0])
        self.assertEqual(_validate_frame(self.inner.frame())['lane']['revision'], 0)
        self.inner.input({'kind': 'key', 'keysym': 'Return'})
        self.assertEqual(_validate_frame(self.inner.frame())['lane']['revision'], 1)
        self.inner.index[1]['revision'] = 7
        self.assertEqual(_validate_frame(self.inner.frame())['lane']['revision'], 7)

    def test_live_missing_revision_is_rejected(self):
        original = self.transport
        def legacy_live(args, **kwargs):
            frame = original(args, **kwargs)
            frame['state'].pop('revision')
            return frame
        self.transport = legacy_live
        summary = self.run_case()
        self.assertEqual(summary['failure']['stage'], 'capture')
        self.assertEqual(self.inputs, [])
        self.assertEqual(self.trace.getvalue(), '')

    def test_offscreen_elements_only_fail_when_targeted(self):
        for bbox in [dict(x=15, y=0, w=2, h=2), dict(x=-2, y=0, w=2, h=2)]:
            with self.subTest(bbox=bbox):
                self.setUp()
                for row in self.inner.index:
                    row['elements'] = row['elements'] + [{'id': 'offscreen', 'bbox': bbox}]
                summary = self.run_case([proposal('click', element_id='open')])
                self.assertEqual(summary['verified_ok'], 1)
                self.assertEqual(len(self.inputs), 1)
                self.trace = io.StringIO()
                summary = self.run_case([proposal('click', element_id='offscreen')])
                self.assertEqual(summary['failure']['stage'], 'validation')
                self.assertEqual(len(self.inputs), 1)
                self.assertEqual(self.trace.getvalue(), '')

    def test_live_capabilities_through_real_model_client(self):
        class Capture:
            response = proposal('done')
            def generate(inner, request):
                inner.request = request
                return inner.response
        backend = Capture()
        desc = Describer()
        describe = desc.describe
        def c2(frame, **kwargs):
            return {**describe(frame, **kwargs), 'schema': 've.cu_describe/v1',
                    'width': frame['width'], 'height': frame['height']}
        desc.describe = c2
        summary = self.run_case(decider=ModelDecider(backend), desc=desc)
        self.assertEqual(summary['stop_reason'], 'done')
        self.assertEqual(backend.request['capabilities'],
                         ['click', 'key', 'wait', 'done'])
        self.assertEqual(self.inputs, [])
        backend.response = proposal('type', text='unsupported')
        self.trace = io.StringIO()
        summary = self.run_case(decider=ModelDecider(backend), desc=desc)
        self.assertEqual(summary['failure']['stage'], 'decision')
        self.assertEqual(summary['failure']['error_class'], 'ActionValidationError')
        self.assertEqual(self.inputs, [])
        self.assertEqual(self.trace.getvalue(), '')

    def test_corrupt_receipt_stops_after_single_input(self):
        original = self.transport
        def corrupt(args, **kwargs):
            value = original(args, **kwargs)
            return {'input_mono_ns': 'bad'} if args[1] == 'input' else value
        self.transport = corrupt
        summary = self.run_case(max_steps=3)
        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(summary['failure']['stage'], 'input')
        self.assertTrue(summary['failure']['input_effect_unknown'])
        self.assertEqual(self.trace.getvalue(), '')
        self.assertEqual(summary['final_frame']['seq'], 0)

    def test_timeout_stages_raw_and_wrapped(self):
        for stage in ['describe', 'decision', 'input', 'after_capture', 'after_describe']:
            for wrapped in [False, True]:
                with self.subTest(stage=stage, wrapped=wrapped):
                    self.setUp()
                    desc = Describer()
                    decider = ReplayDecider([proposal()])
                    raw = subprocess.TimeoutExpired(['provider'], 1)
                    error = ModelTimeoutError('timeout') if stage == 'decision' else VisionBridgeError('timeout')
                    error.__cause__ = raw
                    error = error if wrapped else raw
                    original = self.transport
                    def injected(args, **kwargs):
                        if (stage == 'input' and args[1] == 'input') or (stage == 'after_capture' and self.inputs and args[1] == 'frame'):
                            if stage == 'input':
                                self.inputs.append(args)
                            raise error
                        return original(args, **kwargs)
                    self.transport = injected
                    if stage == 'describe':
                        desc.describe = lambda *a, **k: (_ for _ in ()).throw(error)
                    if stage == 'after_describe':
                        before_desc = desc.describe
                        def after_desc(*a, **k):
                            if self.inputs:
                                raise error
                            return before_desc(*a, **k)
                        desc.describe = after_desc
                    if stage == 'decision':
                        decider.decide = lambda **k: (_ for _ in ()).throw(error)
                    summary = self.run_case(desc=desc, decider=decider)
                    self.assertEqual(summary['stop_reason'], 'timeout')
                    self.assertEqual(summary['failure']['stage'], stage)
                    self.assertEqual(summary['failure']['input_effect_unknown'], stage in ['input', 'after_capture', 'after_describe'])
                    self.assertEqual(self.trace.getvalue(), '')
                    self.assertLessEqual(len(self.inputs), 1)

    def test_valid_prefix_and_failed_attempt_expectation(self):
        summary = self.run_case([proposal(), proposal('type', text='bad'), proposal('type', text='bad')], max_steps=3)
        self.assertEqual(len(self.trace.getvalue().splitlines()), 1)
        self.assertEqual(summary['failure']['stage'], 'validation')
        self.assertEqual(summary['failure']['expectation'], {'kind': 'state_change'})
        self.assertEqual(summary['attempts'][0]['expectation'], {'kind': 'state_change'})
        self.assertEqual(len(self.inputs), 1)

    def test_false_done_is_only_a_claim_with_final_observation(self):
        summary = self.run_case([proposal('done')])
        self.assertEqual(summary['outcome'], 'model_claimed_done')
        self.assertFalse(summary['goal_verified'])
        self.assertEqual(summary['terminal_observation'], 'test')
        self.assertEqual(summary['final_frame']['seq'], 0)
        self.assertEqual(self.inputs, [])

    def test_live_capabilities_passed_to_model_client(self):
        decider = ModelDecider(object())
        with patch.object(decider.client, 'act', return_value=proposal('done')) as act:
            summary = self.run_case(decider=decider)
        self.assertEqual(summary['stop_reason'], 'done')
        self.assertEqual(act.call_args.kwargs['capabilities'], ['click', 'key', 'wait', 'done'])

    def test_muse_timeout_clamped_to_remaining_episode_budget(self):
        for timeout in (240, .01):
            with self.subTest(timeout=timeout):
                backend = MuseBackend(self.fixture.root, timeout=timeout)
                decider = ModelDecider(backend)
                self.addCleanup(decider.close)
                def generate(request):
                    self.assertAlmostEqual(backend.timeout, min(timeout, .05))
                    self.clock.sleep(.06)
                    return proposal('key', keysym='Return')
                desc = Describer()
                with patch.object(backend, 'generate', side_effect=generate), \
                     patch.object(desc, 'describe', return_value={
                         'schema': 've.cu_describe/v1', 'width': 16, 'height': 16,
                         'changed': None, 'changed_regions': [], 'crops': []}):
                    summary = self.run_case(decider=decider, desc=desc, max_ms=50)
                self.assertEqual(summary['stop_reason'], 'max_ms')
                self.assertEqual(backend.timeout, timeout)
                self.assertEqual(self.inputs, [])

    def test_cli_muse_timeout_reaches_backend(self):
        for flags, expected in [([], 240), (['--muse-timeout', '175.5'], 175.5)]:
            with self.subTest(flags=flags), \
                 patch('wender.learn.cu_loop.BridgeDescriber'), \
                 patch('wender.learn.cu_loop.run_loop', return_value={'stop_reason': 'done'}) as run, \
                 patch('sys.stdout', new_callable=io.StringIO):
                status = main(['--goal', 'x', '--capsule', 'fake:'+str(self.fixture.root),
                               '--decider', 'muse', '--muse-workspace', str(self.fixture.root),
                               '--trace', str(self.fixture.root/'trace')] + flags)
            self.assertEqual(status, 0)
            self.assertEqual(run.call_args.args[2].client.backend.timeout, expected)

    def test_last_observation_survives_next_decision_failure(self):
        summary = self.run_case([proposal()], max_steps=3)
        self.assertEqual(summary['failure']['stage'], 'decision')
        self.assertEqual(summary['terminal_observation'], 'test')
        self.assertEqual(len(self.trace.getvalue().splitlines()), 1)

    def test_expired_capture_never_describes(self):
        original = self.transport
        def slow(args, **kwargs):
            result = original(args, **kwargs)
            self.clock.sleep(.1)
            return result
        self.transport = slow
        desc = Describer()
        summary = self.run_case(desc=desc, max_ms=50)
        self.assertEqual(summary['stop_reason'], 'max_ms')
        self.assertEqual(desc.calls, [])
        self.assertEqual(self.inputs, [])

    def test_after_input_budget_does_not_forge_after_frame(self):
        original = self.transport
        def slow(args, **kwargs):
            result = original(args, **kwargs)
            if args[1] == 'input':
                self.clock.sleep(.1)
            return result
        self.transport = slow
        summary = self.run_case(max_ms=50)
        self.assertEqual(summary['stop_reason'], 'max_ms')
        self.assertEqual(summary['final_frame']['seq'], 0)
        self.assertIsNotNone(summary['failure']['receipt'])
        self.assertTrue(summary['failure']['input_effect_unknown'])
        self.assertEqual(self.trace.getvalue(), '')
        self.assertEqual(len(self.inputs), 1)

    def test_missing_after_state_stops_without_repeated_input(self):
        original = self.transport
        def missing(args, **kwargs):
            result = original(args, **kwargs)
            if args[1] == 'frame' and self.inputs:
                result.pop('state')
            return result
        self.transport = missing
        summary = self.run_case(max_steps=3)
        self.assertEqual(summary['failure']['stage'], 'after_capture')
        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(self.trace.getvalue(), '')

    def test_nonfinite_rejected_proposal_has_strict_json_summary(self):
        summary = self.run_case([{**proposal(), 'confidence': float('nan')}])
        json.dumps(summary, allow_nan=False)
        self.assertEqual(summary['failure']['stage'], 'validation')
        self.assertEqual(self.inputs, [])

    def test_cli_budget_stop_non_success_parseable(self):
        with patch('wender.learn.cu_loop.BridgeDescriber', wraps=__import__('wender.learn.cu_loop', fromlist=['BridgeDescriber']).BridgeDescriber):
            # Zero action budget still exercises the actual CLI summary path.
            with patch('wender.learn.cu_loop.ReplayBackend', return_value=object()), patch('sys.stdout', new_callable=io.StringIO) as out:
                status = main(['--goal', 'x', '--capsule', 'fake:'+str(self.fixture.root),
                               '--decider', 'replay:unused', '--ve-root', str(self.fixture.root),
                               '--max-steps', '0', '--trace', str(self.fixture.root/'trace')])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(out.getvalue())['stop_reason'], 'max_steps')
