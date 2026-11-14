"""Rejected model evidence and bounded same-frame correction regressions."""
import copy
import io
import json
import unittest

from tests import test_cu_loop_unit as fixtures
from wender.learn.cu_loop import ModelDecider, run_loop
from wender.learn.model_client import BackendError, ModelTimeoutError, _prompt


class TestRepair(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TestCuLoop()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.clock = fixtures.Clock()
        self.cap = self.fixture.capsule(['a', 'b'])
        self.requests = []
        self.trace = io.StringIO()

    def run_actions(self, actions, *, max_steps=2, max_ms=30000, delay=0):
        owner = self
        class Backend:
            def generate(self, request):
                owner.requests.append(copy.deepcopy(request))
                owner.clock.sleep(delay)
                result = actions[len(owner.requests) - 1]
                if isinstance(result, Exception):
                    raise result
                return result
        class Describer(fixtures.Describer):
            def describe(self, frame, **kwargs):
                return {**super().describe(frame, **kwargs),
                        'schema': 've.cu_describe/v1', 'width': 16, 'height': 16,
                        'crops': [{'x': 0, 'y': 0, 'w': 4, 'h': 4}]}
        return run_loop(self.cap, Describer(), ModelDecider(Backend()), goal='test',
                        trace=self.trace, max_steps=max_steps, max_ms=max_ms,
                        clock=self.clock, sleep=self.clock.sleep)

    def test_invalid_then_valid_same_frame_crops_and_feedback(self):
        bad = fixtures.proposal('click', x=900, y=0)
        result = self.run_actions([bad, fixtures.proposal()], delay=.01)
        self.assertEqual(result['steps'], 1)
        self.assertEqual(result['verified_ok'], 1)
        self.assertEqual(len(self.cap.inputs), 1)
        row = json.loads(self.trace.getvalue())
        self.assertEqual(row['model']['corrections'], 1)
        self.assertAlmostEqual(row['model']['latency_ms'], 20)
        self.assertEqual(row['budget']['steps_left'], 0)
        first, second = self.requests
        self.assertEqual(first['frame_path'], second['frame_path'])
        self.assertEqual(first['crop_paths'], second['crop_paths'])
        self.assertEqual(first['describe'], second['describe'])
        self.assertEqual(json.loads(second['feedback']['rejected_action']), bad['action'])
        self.assertIn('Your previous action was rejected: coordinates outside frame. Return one corrected action.', _prompt(second))
        self.assertEqual(json.loads(result['attempts'][0]['raw_output']), bad)
        self.assertAlmostEqual(result['attempts'][0]['latency_ms'], 10)

    def test_feedback_keeps_action_when_observation_is_truncated(self):
        bad = {**fixtures.proposal('click', x=900, y=0), 'observation': 'x' * 4000}
        result = self.run_actions([bad, fixtures.proposal()])
        self.assertEqual(result['steps'], 1)
        self.assertEqual(len(result['attempts'][0]['raw_output']), 2000)
        self.assertEqual(json.loads(self.requests[1]['feedback']['rejected_action']), bad['action'])

    def test_invalid_twice_records_both_and_stops(self):
        bad = [fixtures.proposal('click', x=900, y=i) for i in (0, 1)]
        result = self.run_actions(bad, max_steps=5)
        self.assertEqual(result['stop_reason'], 'failure')
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(result['attempts']), 2)
        self.assertEqual([json.loads(a['raw_output']) for a in result['attempts']], bad)
        self.assertEqual(result['failure']['raw_output'], result['attempts'][1]['raw_output'])
        self.assertEqual(result['terminal_attempt']['raw_output'], result['failure']['raw_output'])
        self.assertEqual(result['failure']['corrections'], 1)
        self.assertEqual(self.cap.inputs, [])

    def test_truncation_and_redaction(self):
        bad = {**fixtures.proposal(), 'secret': 'private-value',
               'image_bytes': 'raw-image-value', 'observation': 'x' * 4000}
        result = self.run_actions([bad], max_steps=1)
        evidence = result['failure']['raw_output']
        self.assertEqual(len(evidence), 2000)
        self.assertNotIn('private-value', json.dumps(result))
        self.assertNotIn('raw-image-value', json.dumps(result))
        self.assertEqual(len(result['attempts']), 1)

    def test_decision_error_message_bounded_and_no_retry(self):
        result = self.run_actions([BackendError('sk-private token=private ' + 'x' * 500)])
        failure = result['failure']
        self.assertEqual(len(failure['validator_message']), 300)
        self.assertNotIn('sk-private', json.dumps(result))
        self.assertNotIn('token=private', json.dumps(result))
        self.assertEqual(failure['raw_output'], 'null')
        self.assertEqual(len(result['attempts']), 1)
        self.assertEqual(len(self.requests), 1)

    def test_no_retry_when_step_budget_spent(self):
        result = self.run_actions([fixtures.proposal('click', x=900, y=0)], max_steps=1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result['failure']['corrections'], 0)
        self.assertEqual(self.cap.inputs, [])

    def test_deadline_during_correction_prevents_input(self):
        result = self.run_actions([fixtures.proposal('click', x=900, y=0), fixtures.proposal()],
                                  delay=.03, max_ms=50)
        self.assertEqual(result['stop_reason'], 'max_ms')
        self.assertEqual(len(result['attempts']), 2)
        self.assertEqual(self.cap.inputs, [])

    def test_deadline_before_correction_prevents_retry(self):
        result = self.run_actions([fixtures.proposal('click', x=900, y=0)], delay=.06, max_ms=50)
        self.assertEqual(result['stop_reason'], 'max_ms')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.cap.inputs, [])


    def test_retryable_model_failure_reuses_observation_and_budget(self):
        for error in (BackendError('model did not reach a terminal state within 1 step(s)'),
                      ModelTimeoutError('model command timed out')):
            with self.subTest(error=type(error).__name__):
                self.setUp()
                result = self.run_actions([error, fixtures.proposal()])
                self.assertEqual(result['steps'], 1)
                self.assertEqual(len(self.requests), 2)
                first, second = self.requests
                self.assertEqual({k: v for k, v in second.items() if k != 'feedback'}, first)
                self.assertEqual(second['feedback']['error_class'], type(error).__name__)
                row = json.loads(self.trace.getvalue())
                self.assertEqual(row['model']['correction_classes'], [type(error).__name__])
                self.assertEqual(row['budget']['steps_left'], 0)
                self.assertEqual(result['attempts'][0]['correction_class'], type(error).__name__)
                self.assertEqual(len(self.cap.inputs), 1)

    def test_second_model_failure_stops_without_input(self):
        for error in (BackendError('did not reach a terminal state'), ModelTimeoutError('timeout')):
            with self.subTest(error=type(error).__name__):
                self.setUp()
                result = self.run_actions([error, type(error)(str(error))], max_steps=5)
                self.assertEqual(result['stop_reason'], 'timeout' if isinstance(error, ModelTimeoutError) else 'failure')
                self.assertEqual(len(self.requests), 2)
                self.assertEqual(result['failure']['corrections'], 1)
                self.assertEqual(self.cap.inputs, [])

    def test_model_retry_requires_budget_and_deadline(self):
        for kwargs in ({'max_steps': 1}, {'max_ms': 50, 'delay': .06}):
            with self.subTest(kwargs=kwargs):
                self.setUp()
                result = self.run_actions([ModelTimeoutError('timeout')], **kwargs)
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(result['failure']['corrections'], 0)
                self.assertEqual(self.cap.inputs, [])

    def test_retry_allowance_resets_for_next_decision(self):
        result = self.run_actions([ModelTimeoutError('timeout'), fixtures.proposal(),
                                   ModelTimeoutError('timeout'), fixtures.proposal('done')], max_steps=4)
        self.assertEqual(result['stop_reason'], 'done')
        self.assertEqual(len(self.requests), 4)
        self.assertEqual([a['correction_class'] for a in result['attempts'] if 'correction_class' in a],
                         ['ModelTimeoutError', 'ModelTimeoutError'])

    def test_lane_status_and_refusal_in_text_context(self):
        original = self.cap.frame
        self.cap.frame = lambda **kwargs: dict(original(**kwargs), lane=dict(
            original(**kwargs)['lane'], route='inbox', inbox_open=1,
            unread_count=1, last_refusal_code='ILLEGAL_RULE'))
        self.run_actions([fixtures.proposal('done')])
        prompt = _prompt(self.requests[0])
        self.assertIn('"lane_status":', prompt)
        self.assertIn('"route": "inbox"', prompt)
        self.assertIn('"last_refusal_code": "ILLEGAL_RULE"', prompt)
        self.assertIn('"unread_count": 1', prompt)
