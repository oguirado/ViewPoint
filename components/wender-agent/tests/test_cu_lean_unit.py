# @module {"id":"wa.tests.test_cu_lean_unit","layer":2,"owner":"LEARN","deps":["wa.learn.cu_loop","wa.learn.model_client","wa.tests.test_cu_loop_unit","wa.tests.test_cu_model_client_unit"],"book":"HB-WA-s4-learn"}
"""Lean Muse episode lifecycle, telemetry and adaptive effort contracts."""
import io
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from tests import test_cu_loop_unit as loops
from tests.test_cu_model_client_unit import action, jsonl, record
from wender.learn.cu_loop import ModelDecider, run_loop
from wender.learn.model_client import (
    ActionValidationError, ModelClient, MuseBackend, SCHEMA_PATH,
    parse_muse_output, validate_action,
)


class Description(loops.Describer):
    def describe(self, frame, *, prev=None):
        return dict(super().describe(frame, prev=prev), schema='ve.cu_describe/v1',
                    width=16, height=16, crops=[])


class TestLean(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/dev/shm')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = loops.Clock()

    def episode(self, decider, actions, states=('a', 'b', 'c', 'd'), *, fail=False,
                marks_only=False):
        elements = [] if marks_only else [dict(id='a', bbox=dict(x=0, y=0, w=2, h=2))]
        cap = loops.TestCuLoop.capsule(self, states, elements=elements)
        captured = []
        choices = iter(actions)
        usage = dict(input_tokens=45, cached_tokens=40, reasoning_tokens=2, output_tokens=5)
        def invoke(argv, *args, **kwargs):
            workspace = Path(argv[argv.index('--workspace') + 1])
            self.assertEqual(list(workspace.iterdir()), [])
            self.assertTrue(workspace.is_relative_to('/dev/shm/vqa-cu'))
            self.assertEqual(workspace.name, 'musews')
            captured.append(argv)
            if fail:
                raise RuntimeError('transport unavailable')
            return jsonl(next(choices)) + '\n' + json.dumps(record('run.usage', models=[usage]))
        trace = io.StringIO()
        class MarksDescription(Description):
            def describe(inner, frame, *, prev=None):
                return dict(super().describe(frame, prev=prev),
                            marks=[dict(id=1, x=0, y=0, w=2, h=2)],
                            marks_png=str(decider.crops_dir / 'marks.png'))
        with patch('wender.learn.model_client._run', side_effect=invoke):
            summary = run_loop(cap, MarksDescription() if marks_only else Description(),
                               decider, goal='test', trace=trace, pixel_only=marks_only,
                               max_steps=10, clock=self.clock, sleep=self.clock.sleep)
        return summary, [json.loads(line) for line in trace.getvalue().splitlines()], captured

    def test_episode_session_workspace_usage_and_effort(self):
        backend = MuseBackend('/ignored/repository')
        decider = ModelDecider(backend)
        self.addCleanup(decider.close)
        actions = [action('key', keysym='Return'),
                   dict(action('key', keysym='Return'), need_reasoning=True),
                   action('key', keysym='Return'), action('done')]
        summary, rows, calls = self.episode(decider, actions)
        self.assertEqual(summary['stop_reason'], 'done')
        sessions = [argv[argv.index('--session-id') + 1] for argv in calls]
        self.assertEqual(len(set(sessions)), 1)
        uuid.UUID(sessions[0])
        self.assertEqual([r['model']['effort'] for r in rows], ['medium', 'low', 'medium', 'low'])
        self.assertEqual([r['model']['effort_reason'] for r in rows],
                         ['first_step', 'auto_low', 'need_reasoning', 'auto_low'])
        self.assertEqual([r['model']['need_reasoning'] for r in rows], [None, True, None, None])
        self.assertEqual([r['model']['effort_need_reasoning'] for r in rows],
                         [False, False, True, False])
        self.assertEqual([argv[argv.index('--reasoning-effort') + 1] for argv in calls],
                         ['medium', 'low', 'medium', 'low'])
        for row in rows:
            self.assertEqual(row['model']['session_id'], sessions[0])
            self.assertEqual(row['model']['usage'], dict(input_tokens=45, cached_tokens=40,
                                                       reasoning_tokens=2, output_tokens=5))
            self.assertEqual(row['model']['calls'][0]['usage'], row['model']['usage'])
            for field in ('effort', 'effort_reason', 'need_reasoning', 'effort_need_reasoning'):
                self.assertEqual(row['model']['calls'][0][field], row['model'][field])
        self.assertFalse(Path(backend.workspace).exists())
        _, _, calls2 = self.episode(decider, [action('done')])
        self.assertNotEqual(calls2[0][calls2[0].index('--session-id') + 1], sessions[0])
        self.assertFalse(Path(backend.workspace).exists())

    def test_failed_verification_returns_to_medium(self):
        decider = ModelDecider(MuseBackend())
        self.addCleanup(decider.close)
        _, rows, _ = self.episode(decider, [dict(action('key', keysym='Return'), expectation={'kind': 'state_change'})] * 3 +
                                 [action('done')], states=('a', 'b', 'b', 'c'))
        self.assertEqual([r['model']['effort'] for r in rows], ['medium', 'low', 'medium', 'low'])
        self.assertFalse(rows[1]['verify']['ok'])
        self.assertEqual(rows[2]['model']['effort_reason'], 'previous_not_ok')

    def test_marks_only_offline_reasoning_request_controls_next_call(self):
        for need in (False, None, True):
            with self.subTest(need=need):
                decider = ModelDecider(MuseBackend())
                self.addCleanup(decider.close)
                _, rows, calls = self.episode(decider, [
                    dict(action('key', keysym='Return'), need_reasoning=need),
                    action('done')], marks_only=True)
                self.assertTrue(rows[0]['verify']['ok'])
                expected = ['medium', 'medium' if need is True else 'low']
                self.assertEqual([r['model']['effort'] for r in rows], expected)
                self.assertEqual([argv[argv.index('--reasoning-effort') + 1] for argv in calls], expected)
                self.assertEqual(rows[0]['model']['need_reasoning'], need)
                self.assertEqual(rows[1]['model']['effort_reason'],
                                 'need_reasoning' if need is True else 'auto_low')

    def test_feedback_retry_trace(self):
        decider = ModelDecider(MuseBackend())
        self.addCleanup(decider.close)
        _, rows, _ = self.episode(decider, [dict(action('click', x=100, y=100), need_reasoning=True),
                                          action('key', keysym='Return'), action('done')])
        self.assertEqual(rows[0]['model']['corrections'], 1)
        self.assertEqual([call['effort_reason'] for call in rows[0]['model']['calls']],
                         ['first_step', 'feedback_retry'])
        self.assertEqual(rows[0]['model']['effort'], 'medium')
        self.assertIs(rows[0]['model']['calls'][0]['need_reasoning'], True)

    def test_auto_effort_policy_table(self):
        decider = ModelDecider(MuseBackend())
        self.addCleanup(decider.close)
        for first in (False, True):
            for retry in (False, True):
                for ok in (False, True):
                    for need in (False, None, True):
                        for grounding in ('none', 'marks', 'elements'):
                            frame = {'lane': {'elements': [{}]}} if grounding == 'elements' else {}
                            desc = {'marks': [{'id': 1}]} if grounding == 'marks' else {}
                            reason = ('feedback_retry' if retry else 'first_step' if first else
                                      'need_reasoning' if need is True else 'previous_not_ok' if not ok else
                                      'ungrounded' if grounding == 'none' else 'auto_low')
                            with self.subTest(first=first, retry=retry, ok=ok, need=need, grounding=grounding):
                                decider.set_effort(frame, desc, ok, need,
                                                   first_step=first, feedback_retry=retry)
                                self.assertEqual(decider.effort_context['effort_reason'], reason)
                                self.assertEqual(decider.client.backend.effort,
                                                 'low' if reason == 'auto_low' else 'medium')

    def test_prompt_limits_reasoning_to_content_not_visible_as_marks(self):
        from wender.learn.model_client import _prompt
        prompt = _prompt(dict(goal='test', describe={}, elements=[], trace_steps=[]))
        self.assertIn('true ONLY when the NEXT decision', prompt)
        self.assertIn('not visible as marks', prompt)
        self.assertIn('set it to false or null', prompt)

    def test_workspace_removed_on_transport_failure_even_when_crops_retained(self):
        decider = ModelDecider(MuseBackend(), keep_crops=True)
        self.addCleanup(__import__('shutil').rmtree, decider.crops_dir.parent)
        summary, _, _ = self.episode(decider, [], fail=True)
        self.assertEqual(summary['stop_reason'], 'failure')
        self.assertFalse(Path(decider.client.backend.workspace).exists())
        self.assertTrue(decider.crops_dir.exists())

    def test_effort_grounding_and_overrides(self):
        backend = MuseBackend()
        decider = ModelDecider(backend)
        self.addCleanup(decider.close)
        cases = [({}, {}, True, False, 'medium'),
                 ({}, {'marks': [{'id': 1}]}, True, False, 'low'),
                 ({}, {'marks': [{'id': 1}]}, False, False, 'medium'),
                 ({'lane': {'elements': [{}]}}, {}, True, False, 'low'),
                 ({'lane': {'elements': [{}]}}, {}, True, True, 'medium')]
        for frame, desc, ok, need, expected in cases:
            decider.set_effort(frame, desc, ok, need)
            self.assertEqual(backend.effort, expected)
        for policy in ('low', 'medium', 'xhigh'):
            backend.effort_policy = policy
            decider.set_effort({}, {}, False, False)
            self.assertEqual(backend.effort, policy)
            self.assertEqual(decider.effort_context['effort_reason'], 'fixed_policy')
            decider.set_effort({}, {}, False, True)
            self.assertEqual(backend.effort, 'medium' if policy == 'low' else policy)

    def test_marks_only_does_not_decode_or_generate_crops(self):
        class Backend:
            def generate(inner, request):
                self.assertEqual(request['crop_paths'], [])
                return action('done')
        decider = ModelDecider(Backend())
        self.addCleanup(decider.close)
        decider.decide(goal='test', frame={'path': '/absent/frame.png'},
                       description=dict(schema='ve.cu_describe/v1', width=16, height=16,
                                        marks_png='/dev/shm/vqa-cu/marks.png',
                                        marks=[dict(id=1, x=0, y=0, w=1, h=1)],
                                        crops=[dict(x=0, y=0, w=1, h=1)]))
        self.assertEqual(list(decider.crops_dir.iterdir()), [])

    def test_need_reasoning_nullable_strict_wire_and_legacy_replay(self):
        schema = json.loads(SCHEMA_PATH.read_text())
        self.assertEqual(schema['properties']['need_reasoning']['type'], ['boolean', 'null'])
        self.assertIn('need_reasoning', schema['required'])
        for value in (True, False, None):
            result = validate_action(dict(action('done'), need_reasoning=value), 16, 16)
            self.assertEqual(result.get('need_reasoning'), value)
        self.assertNotIn('need_reasoning', validate_action(action('done'), 16, 16))
        for value in (0, 1, 'true', [], {}):
            with self.subTest(value=value), self.assertRaises(ActionValidationError):
                validate_action(dict(action('done'), need_reasoning=value), 16, 16)

    def test_usage_real_fixtures_and_missing_evidence(self):
        fixtures = Path(__file__).with_name('fixtures')
        for name, expected in (
            ('ok', dict(input_tokens=41419, cached_tokens=0, reasoning_tokens=2090, output_tokens=2231)),
            ('multi', dict(input_tokens=87892, cached_tokens=44401, reasoning_tokens=1831, output_tokens=2099)),
        ):
            parsed = parse_muse_output((fixtures / ('real_muse_output_' + name + '.jsonl')).read_text())
            self.assertEqual(parsed.usage, expected)
            client = ModelClient(MuseBackend('.'))
            with patch.object(client.backend, 'generate', return_value=parsed):
                if name == 'ok':  # This older capture predates the complete C3 envelope.
                    with self.assertRaises(ActionValidationError):
                        client.act('test', 'frame.png', dict(schema='ve.cu_describe/v1', width=1024, height=768))
                else:
                    client.act('test', 'frame.png', dict(schema='ve.cu_describe/v1', width=1024, height=768))
            self.assertEqual(client.last_model['usage'], expected)
        self.assertIsNone(parse_muse_output(jsonl(action('done'))).usage)
        empty = parse_muse_output(jsonl(action('done')) + '\n' +
                                  json.dumps(record('run.usage', models=[])))
        self.assertTrue(all(value is None for value in empty.usage.values()))
