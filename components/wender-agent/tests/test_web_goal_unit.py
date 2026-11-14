# @module {"id":"wa.tests.web_goal_unit","layer":2,"owner":"LEARN","deps":["wa.learn.web_goal","wa.learn.cu_loop","wa.learn.model_client","wa.learn.script_live","wa.perceive.srf_identity","wa.tools.cu_web","wa.perceive.vision_bridge"],"book":"HB-WA-s4-learn"}
"""Public adapter predicates with independent transport/provider boundary fakes."""
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from tools.cu_web import main
from wender.learn import web_goal as g
from wender.learn.cu_loop import _encode_png
from wender.learn.model_client import MuseBackend, ModelTimeoutError
from wender.learn.script_live import LegacyDispatchOwner
from wender.perceive.srf_identity import GoalIdentitySurface, SurfaceError, SurfaceNotReady, SurfaceRefused
from wender.perceive.vision_bridge import DescribeResult, Mark


# @anchor:wa.tests.web_goal_unit._proposal
def _proposal(kind='done', **fields):
    return dict(schema='cu.action/v1', observation='PRIVATE_OBSERVATION',
                action=dict(kind=kind, **fields), expectation=dict(kind='state_change'), confidence=1)


# @anchor:wa.tests.web_goal_unit._element
def _element(name='Design', **changes):
    row = dict(id='PRIVATE_ID', name=name, role='textbox', box=[0, 0, 8, 8],
               actionable=True, region='main', container={'id': 'PRIVATE_CONTAINER'},
               states={'enabled': True, 'focused': True}, value='PRIVATE_VALUE')
    row.update(changes)
    return row


# @anchor:wa.tests.web_goal_unit._Clock
class _Clock:
    # @anchor:wa.tests.web_goal_unit._Clock.__init__
    def __init__(self):
        self.now = 1.0

    # @anchor:wa.tests.web_goal_unit._Clock.__call__
    def __call__(self):
        return self.now

    # @anchor:wa.tests.web_goal_unit._Clock.sleep
    def sleep(self, seconds):
        self.now += seconds


# @anchor:wa.tests.web_goal_unit.GoalUnit
class GoalUnit(unittest.TestCase):
    # @anchor:wa.tests.web_goal_unit.GoalUnit.setUp
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/dev/shm')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'frame.png'
        self.path.write_bytes(_encode_png(bytes([1, 2, 3])*256, 16, 16))
        self.clock = _Clock()
        self.rows = [_element()]
        self.calls, self.inputs, self.requests = [], [], []
        self.seq = 0
        self.frame_error = self.ax_error = self.input_error = None
        self.receipt = dict(focus_verified=True, input_mono_ns=100)
        self.capture_delay = self.vision_delay = self.model_delay = 0
        self.actions = [_proposal()]
        self.marks = []
        self.result_paths = []
        self.addCleanup(self._cleanup_results)

    # @anchor:wa.tests.web_goal_unit.GoalUnit._cleanup_results
    def _cleanup_results(self):
        for path in self.result_paths:
            shutil.rmtree(Path(path).parent, ignore_errors=True)

    # @anchor:wa.tests.web_goal_unit.GoalUnit._transport
    def _transport(self, op, payload):
        self.calls.append(op)
        if op == 'frame':
            self.clock.sleep(self.capture_delay)
            if self.frame_error:
                error = self.frame_error.pop(0) if isinstance(self.frame_error, list) else self.frame_error
                if error:
                    raise error
            return dict(path=str(self.path), seq=self.seq, width=16, height=16, generation=1,
                capture_mono_ns=10, sha256_rgb24=hashlib.sha256(bytes([1, 2, 3])*256).hexdigest(),
                window=dict(id='w1', w=16, h=16))
        if op == 'ax':
            if self.ax_error:
                error = self.ax_error.pop(0) if isinstance(self.ax_error, list) else self.ax_error
                if error:
                    raise error
            return dict(elements=copy.deepcopy(self.rows), frame_seq=self.seq, window=dict(id='w1', w=16, h=16))
        self.inputs.append((op, payload))
        self.seq += 1
        if self.input_error:
            raise self.input_error
        return self.receipt

    # @anchor:wa.tests.web_goal_unit.GoalUnit._vision
    def _vision(self, frame, prev=None, *, masks=(), marks_out=None):
        self.clock.sleep(self.vision_delay)
        marks_out.write_bytes(self.path.read_bytes())
        return DescribeResult('ve.cu_describe/v1', hashlib.sha256(bytes([1, 2, 3])*256).hexdigest(),
                              16, 16, None if prev is None else True, (), (), {},
                              tuple(Mark(**m) for m in self.marks), str(marks_out))

    # @anchor:wa.tests.web_goal_unit.GoalUnit._generate
    def _generate(self, request):
        self.requests.append(copy.deepcopy(request))
        self.clock.sleep(self.model_delay)
        value = self.actions.pop(0) if len(self.actions) > 1 else self.actions[0]
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(request)
        return copy.deepcopy(value)

    # @anchor:wa.tests.web_goal_unit.GoalUnit._run
    def _run(self, **kwargs):
        surface = GoalIdentitySurface(transport=self._transport)
        trace = io.StringIO()
        with patch.object(MuseBackend, 'generate', side_effect=self._generate), patch(
                'wender.perceive.vision_bridge.SubprocessVisionEngine.describe', new=self._vision_bound()):
            result = g.run_goal(surface, goal=kwargs.pop('goal', 'create a design'), trace=trace,
                max_steps=kwargs.pop('max_steps', 3), max_ms=kwargs.pop('max_ms', 10000),
                clock=self.clock, sleep=self.clock.sleep, ve_root=self.root, **kwargs)
        if result['final_frame']:
            self.result_paths.append(result['final_frame'])
        self.assertFalse(surface.started)
        self.assertIsNone(surface._goal_targets)
        return result, trace.getvalue()

    # @anchor:wa.tests.web_goal_unit.GoalUnit._vision_bound
    def _vision_bound(self):
        def describe(engine, *args, **kwargs):
            return self._vision(*args, **kwargs)
        return describe

    # @anchor:wa.tests.web_goal_unit.GoalUnit._capsule
    def _capsule(self, goal='create a design', owner=None):
        surface = GoalIdentitySurface(transport=self._transport)
        policy = g.GoalPolicy(goal, self.clock()+10, self.clock)
        metrics = g.GoalMetrics(self.clock)
        actual_owner = LegacyDispatchOwner(surface)
        surface.install_goal_session(owner=actual_owner, policy=policy, deadline=policy.deadline, clock=self.clock)
        cap = g.GoalCapsule(surface, owner=owner or actual_owner, policy=policy, metrics=metrics,
            deadline=policy.deadline, clock=self.clock, sleep=self.clock.sleep)
        return cap, surface, actual_owner

    # @anchor:wa.tests.web_goal_unit.GoalUnit._refuses
    def _refuses(self, cap, action, code):
        before = len(self.inputs)
        try:
            cap.input(action)
        except SurfaceError as caught:
            self.assertEqual(caught.code, code)
        except Exception as caught:
            self.fail('expected refusal '+code+', got '+type(caught).__name__)
        else:
            self.fail('missing refusal '+code)
        self.assertEqual(len(self.inputs), before)

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_clean_done_and_final_copy
    def test_clean_done_and_final_copy(self):
        destination = self.root / 'explicit.png'
        result, trace = self._run(save_frame=destination)
        self.assertTrue(result['goal_verified'], result)
        self.assertEqual(result['goal_verification_basis'], 'model_done')
        self.assertIsNone(result['independent_goal_verified'])
        self.assertEqual(result['model_calls'], 1)
        self.assertTrue(Path(result['final_frame']).is_relative_to('/dev/shm'))
        self.assertTrue(Path(result['final_frame']).exists())
        self.assertEqual(Path(result['final_frame']).read_bytes(), destination.read_bytes())
        self.assertEqual(self.inputs, [])
        self.assertNotIn('terminal_observation', result)

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_canvas_mark_id_and_coordinate_refusal
    def test_canvas_mark_id_and_coordinate_refusal(self):
        self.rows = []
        self.marks = [dict(id=1, x=8, y=8, w=4, h=4, kind='button', score=1)]
        self.actions = [_proposal('click', mark_id=1), _proposal()]
        result, _ = self._run()
        self.assertTrue(result['goal_verified'], result)
        self.assertEqual(self.inputs, [('click', {'point': '10,10'})])
        self.assertEqual(self.requests[0]['marks'][0]['id'], 1)
        self.inputs.clear()
        self.actions = [_proposal('click', x=1, y=1)]
        result, _ = self._run()
        self.assertFalse(result['goal_verified'])
        self.assertEqual(self.inputs, [])
        self.assertEqual(result['failure_code'], 'goal_loop_failure')

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_odd_centers_through_run_goal
    def test_odd_centers_through_run_goal(self):
        for kind in ('element_id', 'mark_id'):
            with self.subTest(kind=kind):
                self.inputs.clear()
                self.rows = [_element(box=[1, 2, 5, 7])] if kind == 'element_id' else []
                self.marks = [] if kind == 'element_id' else [dict(id=1, x=1, y=2, w=5, h=7, kind='button', score=1)]
                self.actions = [_proposal('click', **{kind: 'e1_0' if kind == 'element_id' else 1}), _proposal()]
                result, _ = self._run()
                self.assertTrue(result['goal_verified'], result)
                self.assertIsNone(result['failure_code'])
                self.assertEqual(self.inputs, [('click', {'point': '3,5'})])
        # An AX control covering the actuator point keeps its precedence.
        self.rows = [_element(box=[3, 5, 1, 1])]
        self.marks = [dict(id=1, x=1, y=2, w=5, h=7, kind='button', score=1)]
        self.inputs.clear()
        self.actions = [_proposal()]
        result, _ = self._run()
        self.assertTrue(result['goal_verified'], result)
        self.assertEqual(self.requests[-1].get('marks'), [])
        self.assertEqual(self.inputs, [])

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_projection_failure_through_run_goal
    def test_projection_failure_through_run_goal(self):
        for change in (dict(name=3), dict(role=3), dict(states=[]),
                       dict(box=[0, 0, 5.0, 7])):
            with self.subTest(change=change):
                self.rows = [_element(**change)]
                result, _ = self._run()
                self.assertFalse(result['goal_verified'])
                self.assertEqual(result['failure_code'], 'goal_projection', result)
                self.assertEqual(self.inputs, [])
        self.rows = [_element()]
        result, _ = self._run()
        self.assertTrue(result['goal_verified'], result)
        self.assertIsNone(result['failure_code'])

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_description_failures_through_run_goal
    def test_description_failures_through_run_goal(self):
        digest = hashlib.sha256(bytes([1, 2, 3])*256).hexdigest()
        base = dict(schema='ve.cu_describe/v1', width=16, height=16,
                    frame_sha256=digest, marks=[], marks_png=None)
        mark = dict(id=1, x=8, y=8, w=5, h=7)
        cases = [(dict(schema='wrong'), 'goal_vision'),
                 (dict(marks=[dict(mark, id=0)]), 'goal_marks'),
                 (dict(marks=[dict(mark, w=5.0)]), 'goal_marks'),
                 (dict(marks=[dict(mark, x=15)]), 'goal_marks'),
                 (dict(marks=[mark]), 'goal_overlay'),
                 (dict(marks=[mark], marks_png=str(self.root / 'missing.png')), 'goal_overlay')]
        for changes, code in cases:
            with self.subTest(changes=changes), patch('wender.learn.cu_loop.BridgeDescriber.describe', return_value=dict(base, **changes)):
                result, _ = self._run()
                self.assertFalse(result['goal_verified'])
                self.assertEqual(result['failure_code'], code, result)
                self.assertEqual(self.inputs, [])
        self.rows = []
        self.marks = [dict(mark, kind='button', score=1)]
        with patch.object(g, '_fixture_rgb', return_value=(b'', 1, 1)):
            result, _ = self._run()
        self.assertEqual(result['failure_code'], 'goal_overlay', result)
        self.assertEqual(self.inputs, [])
        result, _ = self._run()
        self.assertTrue(result['goal_verified'], result)
        self.assertIsNone(result['failure_code'])

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_failure_code_reset_on_new_acquisition
    def test_failure_code_reset_on_new_acquisition(self):
        cap, _, _ = self._capsule()
        cap.failure_code = 'goal_projection'
        cap.frame()
        self.assertIsNone(cap.failure_code)
        cap.failure_code = 'goal_marks'
        describer = g.GoalDescriber(self.root, capsule=cap, policy=cap.policy, metrics=cap.metrics)
        self.addCleanup(lambda: describer._marks_dir and describer._marks_dir.cleanup())
        with patch('wender.perceive.vision_bridge.SubprocessVisionEngine.describe', new=self._vision_bound()):
            describer.describe(cap.surface.last_frame)
        self.assertIsNone(cap.failure_code)

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_surface_integer_geometry
    def test_surface_integer_geometry(self):
        cap, surface, _ = self._capsule()
        frame = cap.frame()
        for width in (5.0, True):
            surface.publish_goal_targets(frame_seq=frame['seq'], generation=frame['generation'],
                frame_digest=frame['sha256_rgb24'], elements=cap.targets,
                marks={1: dict(x=8, y=8, w=width, h=7)})
            self._refuses(cap, dict(kind='click', mark_id=1), 'goal_grounding')
        surface.publish_goal_targets(frame_seq=frame['seq'], generation=frame['generation'],
            frame_digest=frame['sha256_rgb24'], elements=cap.targets,
            marks={1: dict(x=8, y=8, w=5, h=7)})
        cap.input(dict(kind='click', mark_id=1))
        self.assertEqual(self.inputs, [('click', {'point': '10,11'})])

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_projection_structure_and_duplicate
    def test_projection_structure_and_duplicate(self):
        cap, _, _ = self._capsule()
        frame = cap.frame(state=False)
        row = frame['lane']['elements'][0]
        self.assertEqual(row['role'], 'textbox')
        self.assertEqual(row['bbox'], dict(x=0, y=0, w=8, h=8))
        self.assertEqual(row['region'], 'main')
        self.assertEqual(row['container'], 'c0')
        self.assertTrue(row['actionable'])
        self.assertEqual(row['value_len'], 13)
        self.assertNotIn('PRIVATE_ID', json.dumps(frame))
        self.assertNotIn('PRIVATE_VALUE', json.dumps(frame))
        self.rows *= 2
        with self.assertRaises(SurfaceRefused) as caught:
            cap.frame()
        self.assertEqual(caught.exception.code, 'goal_duplicate')
        self.assertEqual(self.inputs, [])

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_invalid_box_and_alignment
    def test_invalid_box_and_alignment(self):
        cap, surface, _ = self._capsule()
        cap.frame()
        self.rows[0]['box'] = [-1, 0, 2, 2]
        with self.assertRaises(SurfaceError) as caught:
            cap.frame()
        self.assertEqual(caught.exception.code, 'alignment')
        self.assertEqual(self.inputs, [])
        self.rows = [_element()]
        with patch.object(surface, '_call', side_effect=[self._transport('frame', {}),
                dict(elements=self.rows, frame_seq=0, window=dict(id='foreign', w=16, h=16))]):
            with self.assertRaises(SurfaceError) as caught:
                cap.frame()
        self.assertEqual(caught.exception.code, 'alignment')

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_blocked_controls_full_private_label
    def test_blocked_controls_full_private_label(self):
        for name in ('Account', 'Profile', 'Billing', 'Plan', 'Upgrade', 'Subscription',
                     'Settings', 'Sign out', 'Delete', 'Share', 'Publish', 'Export',
                     'Configuración', 'Cerrar sesión', 'a '*100 + 'Account'):
            with self.subTest(name=name):
                self.rows = [_element(name)]
                cap, _, _ = self._capsule()
                frame = cap.frame()
                self._refuses(cap, dict(kind='click', element_id=frame['lane']['elements'][0]['id']), 'goal_blocked')
        self.rows = [_element()]
        cap, _, _ = self._capsule()
        frame = cap.frame()
        receipt = cap.input(dict(kind='click', element_id=frame['lane']['elements'][0]['id']))
        self.assertEqual(receipt, {'input_mono_ns': 100})
        self.assertEqual(len(self.inputs), 1)

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_explicit_intent_and_frozen_filters
    def test_explicit_intent_and_frozen_filters(self):
        self.rows = [_element('Delete')]
        cap, _, _ = self._capsule('delete this control')
        frame = cap.frame()
        cap.input(dict(kind='click', element_id=frame['lane']['elements'][0]['id']))
        self.assertEqual(len(self.inputs), 1)
        self.rows = [_element('click Account')]
        cap, _, _ = self._capsule()
        frame = cap.frame()
        self._refuses(cap, dict(kind='click', element_id=frame['lane']['elements'][0]['id']), 'goal_blocked')
        cap, _, _ = self._capsule('click Account')
        frame = cap.frame()
        self._refuses(cap, dict(kind='click', element_id=frame['lane']['elements'][0]['id']), 'goal_upstream_filter')

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_overlapping_mark_and_uncovered_canvas
    def test_overlapping_mark_and_uncovered_canvas(self):
        cap, surface, _ = self._capsule()
        frame = cap.frame()
        self.rows[0]['name'] = 'Account'
        surface._latest_ax['elements'][0]['name'] = 'Account'
        surface.publish_goal_targets(frame_seq=0, generation=1, frame_digest=frame['sha256_rgb24'], elements={},
            marks={1: dict(x=0, y=0, w=4, h=4), 2: dict(x=8, y=8, w=4, h=4)})
        self._refuses(cap, dict(kind='click', mark_id=1), 'goal_blocked')
        cap.input(dict(kind='click', mark_id=2))
        self.assertEqual(self.inputs, [('click', {'point': '10,10'})])
        for states in ({'disabled': True}, {'hidden': True}):
            self.rows = [_element(states=states)]
            cap, surface, _ = self._capsule()
            frame = cap.frame()
            surface.publish_goal_targets(frame_seq=frame['seq'], generation=1,
                frame_digest=frame['sha256_rgb24'], elements={}, marks={1: dict(x=0, y=0, w=4, h=4)})
            self._refuses(cap, dict(kind='click', mark_id=1), 'goal_ineligible')

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_focus_ascii_and_editing_keys
    def test_focus_ascii_and_editing_keys(self):
        cap, _, _ = self._capsule()
        cap.frame()
        self._refuses(cap, dict(kind='type', text='é'), 'goal_ascii')
        self._refuses(cap, dict(kind='key', keysym='Tab'), 'goal_key')
        cap.input(dict(kind='type', text='ASCII'))
        cap.input(dict(kind='key', keysym='Left'))
        self.assertEqual([x[0] for x in self.inputs], ['type', 'key'])
        for rows, code in (([_element('Account')], 'goal_blocked'),
                           ([_element(), _element(id='other')], 'goal_focus'),
                           ([_element(states={})], 'goal_focus')):
            self.rows = rows
            cap, _, _ = self._capsule()
            cap.frame(state=False)
            self._refuses(cap, dict(kind='type', text='ASCII'), code)

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_canvas_focus_loss
    def test_canvas_focus_loss(self):
        self.rows = []
        cap, surface, _ = self._capsule()
        frame = cap.frame()
        surface.publish_goal_targets(frame_seq=0, generation=1, frame_digest=frame['sha256_rgb24'],
            elements={}, marks={1: dict(x=8, y=8, w=4, h=4)})
        cap.input(dict(kind='click', mark_id=1))
        cap.input(dict(kind='type', text='ASCII'))
        surface.last_frame['generation'] = 2
        surface.publish_goal_targets(frame_seq=0, generation=2, frame_digest=frame['sha256_rgb24'], elements={}, marks={})
        self._refuses(cap, dict(kind='type', text='ASCII'), 'goal_focus')

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_grounding_centers_stale_and_fields
    def test_grounding_centers_stale_and_fields(self):
        cap, surface, _ = self._capsule()
        frame = cap.frame()
        target = frame['lane']['elements'][0]['id']
        for action, code in ((dict(kind='click', x=4, y=4), 'goal_grounding'),
                (dict(kind='click', element_id='unknown'), 'goal_grounding'),
                (dict(kind='click', element_id=target, mark_id=1), 'goal_grounding'),
                (dict(kind='click', element_id=target, x=5, y=4), 'goal_center'),
                (dict(kind='click', element_id=target, secret=True), 'goal_fields'),
                (dict(kind='wait', ms=1), 'goal_fields'), (dict(kind='done'), 'goal_fields')):
            self._refuses(cap, action, code)
        cap.input(dict(kind='click', element_id=target, x=4, y=4))
        surface.last_frame = dict(surface.last_frame, seq=999)
        self._refuses(cap, dict(kind='click', element_id=target), 'goal_stale')
        self._refuses(cap, dict(kind='click', mark_id=1), 'goal_stale')

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_steps_and_wall_deadlines
    def test_steps_and_wall_deadlines(self):
        self.actions = [_proposal('key', keysym='Left')]
        result, _ = self._run(max_steps=1)
        self.assertEqual(result['stop_reason'], 'max_steps')
        self.assertFalse(result['goal_verified'])
        self.assertEqual(len(self.inputs), 1)
        for stage in ('capture_delay', 'vision_delay', 'model_delay'):
            self.inputs.clear()
            setattr(self, stage, 11)
            result, _ = self._run()
            self.assertFalse(result['goal_verified'])
            self.assertEqual(result['stop_reason'], 'max_ms')
            self.assertEqual(self.inputs, [])
            setattr(self, stage, 0)

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_selective_read_retry
    def test_selective_read_retry(self):
        self.frame_error = [SurfaceNotReady('not_ready', 'frame'), None]
        self.ax_error = [SurfaceRefused('refused_focus', 'ax'), None]
        result, _ = self._run()
        self.assertTrue(result['goal_verified'], result)
        self.assertEqual(result['read_retries'], 2)
        self.assertEqual(result['acquisition_attempts'], 3)
        for error, attempts in ((SurfaceNotReady('not_ready', 'frame'), 15),
                (SurfaceRefused('refused_focus', 'ax'), 15),
                (SurfaceError('malformed_capture', 'frame'), 1)):
            self.calls.clear()
            self.frame_error = error
            result, _ = self._run()
            self.assertEqual(result['acquisition_attempts'], attempts)
            self.assertEqual(result['failure_code'], error.code)
            self.assertFalse(result['goal_verified'])
            self.assertEqual(self.inputs, [])

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_input_never_replayed_and_uncertain
    def test_input_never_replayed_and_uncertain(self):
        self.actions = [_proposal('key', keysym='Left')]
        for error, receipt, code in ((SurfaceNotReady('not_ready', 'key'), self.receipt, 'not_ready'),
                (SurfaceRefused('refused_focus', 'key'), self.receipt, 'refused_focus'),
                (None, {}, 'refused_focus'),
                (None, dict(focus_verified=False, input_mono_ns=100), 'refused_focus'),
                (None, dict(focus_verified=True, input_mono_ns='bad'), 'malformed_capture'),
                (None, dict(focus_verified=True, input_mono_ns=-1), 'malformed_capture')):
            self.inputs.clear()
            self.input_error, self.receipt = error, receipt
            result, _ = self._run()
            self.assertEqual(len(self.inputs), 1)
            self.assertTrue(result['input_effect_unknown'])
            self.assertEqual(result['failure_code'], code)
            self.assertFalse(result['goal_verified'])

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_generation_cannot_cancel
    def test_generation_cannot_cancel(self):
        self.rows = [_element(states=dict(focused=True, busy=True))]
        cap, _, _ = self._capsule()
        frame = cap.frame()
        self._refuses(cap, dict(kind='key', keysym='Escape'), 'generation_key')
        self.rows = [_element('Stop response')]
        cap, _, _ = self._capsule('Stop response')
        frame = cap.frame()
        self._refuses(cap, dict(kind='click', element_id=frame['lane']['elements'][0]['id']), 'generation_control')
        self.rows = [_element()]
        cap, _, _ = self._capsule()
        cap.frame()
        cap.input(dict(kind='key', keysym='Left'))
        self.assertEqual(len(self.inputs), 1)

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_provider_attempts_and_malformed_done
    def test_provider_attempts_and_malformed_done(self):
        self.actions = [ModelTimeoutError('PRIVATE_EXCEPTION'), _proposal()]
        result, _ = self._run()
        self.assertTrue(result['goal_verified'], result)
        self.assertEqual(result['model_calls'], 2)
        self.assertEqual(result['latency_us']['model']['count'], 2)
        self.actions = [dict(schema='cu.action/v1', observation='bad', action=dict(kind='done', text='bad'))]
        result, _ = self._run()
        self.assertFalse(result['goal_verified'])
        self.assertEqual(result['failure_code'], 'goal_loop_failure')
        self.assertEqual(self.inputs, [])

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_missing_final_frame_never_success
    def test_missing_final_frame_never_success(self):
        with patch.object(g, 'run_loop', return_value=dict(steps=0, stop_reason='done', failure=None, final_frame=None)):
            result, _ = self._run()
        self.assertFalse(result['goal_verified'])
        self.assertIsNone(result['final_frame'])
        result, _ = self._run()
        self.assertTrue(result['goal_verified'])

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_privacy_on_success_and_failure
    def test_privacy_on_success_and_failure(self):
        self.rows[0]['name'] = 'PRIVATE_LABEL'
        result, trace = self._run(goal='PRIVATE_GOAL')
        serialized = json.dumps(result) + trace
        for canary in ('PRIVATE_GOAL', 'PRIVATE_LABEL', 'PRIVATE_ID', 'PRIVATE_CONTAINER', 'PRIVATE_VALUE', 'PRIVATE_OBSERVATION'):
            self.assertNotIn(canary, serialized)
        self.actions = [RuntimeError('PRIVATE_EXCEPTION')]
        result, trace = self._run(goal='PRIVATE_GOAL')
        self.assertNotIn('PRIVATE_EXCEPTION', json.dumps(result)+trace)
        self.assertEqual(result['model_calls'], 1)
        self.assertIn('model', result['latency_us'])
        sink = io.StringIO()
        projection = g.GoalTrace(sink)
        projection.write(json.dumps(dict(event='PRIVATE_EVENT', step=1, goal='PRIVATE_GOAL',
            action=dict(text='PRIVATE_TEXT'), latency_ms=2, metadata='PRIVATE_META'))+'\n')
        self.assertEqual(json.loads(sink.getvalue()), dict(event='goal_step', step=1, latency_ms=2))

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_cli_validation_and_isolation
    def test_cli_validation_and_isolation(self):
        for argv in (['--goal', 'é'], ['--goal', ''], ['--goal', 'x', '--task', 'x'],
                ['--goal', 'x', '--scripts', 'on'], ['--goal', 'x', '--analyze'],
                ['--goal', 'x', '--dry-check'], ['--goal', 'x', '--max-steps', '0'],
                ['--goal', 'x', '--max-seconds', 'nan'], ['--goal', 'x', '--max-seconds', 'inf'],
                ['--goal', 'x', '--max-seconds', '0'], ['--save-frame', '/dev/shm/no.png'],
                ['--save-frame', '/dev/shm/no.png', '--task', 'x', '--executor', 'muse']):
            with self.subTest(argv=argv), patch('sys.stderr', new=io.StringIO()), patch('wender.perceive.srf_identity.GoalIdentitySurface') as surface, patch('tools.cu_web.SrfIdentitySurface') as legacy, patch('tools.cu_web.run_web_cycle', side_effect=AssertionError('legacy route reached')), patch('pathlib.Path.read_text', return_value='{}'), patch.object(g, 'run_goal', return_value=dict(goal_verified=False)) as run:
                try:
                    main(argv)
                except SystemExit:
                    pass
                except Exception as caught:
                    self.fail('expected CLI validation, got '+type(caught).__name__)
                else:
                    self.fail('missing CLI validation')
                surface.assert_not_called()
                legacy.assert_not_called()
                run.assert_not_called()
        with patch('wender.perceive.srf_identity.GoalIdentitySurface') as surface, patch.object(g, 'run_goal', return_value=dict(goal_verified=True)) as run, patch('tools.cu_web.record_workflow') as record, patch('pathlib.Path.read_text', side_effect=AssertionError('memory read')), patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(main(['--goal', 'design']), 0)
            self.assertEqual(run.call_args.kwargs['goal'], 'design')
            record.assert_not_called()
            surface.assert_called_once()

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_owner_closed_foreign_and_cleanup
    def test_owner_closed_foreign_and_cleanup(self):
        cap, surface, owner = self._capsule()
        frame = cap.frame()
        owner.close()
        self._refuses(cap, dict(kind='click', element_id=frame['lane']['elements'][0]['id']), 'goal_owner')
        foreign = LegacyDispatchOwner(GoalIdentitySurface(transport=self._transport))
        cap, surface, _ = self._capsule(owner=foreign)
        frame = cap.frame()
        with self.assertRaisesRegex(ValueError, 'DISPATCH_OWNER_UNAVAILABLE'):
            cap.input(dict(kind='click', element_id=frame['lane']['elements'][0]['id']))
        self.assertEqual(self.inputs, [])
        result, _ = self._run()
        self.assertTrue(result['goal_verified'])
        self.assertIsNone(surface._goal_focus)

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_vision_association_and_overlay
    def test_vision_association_and_overlay(self):
        cap, surface, _ = self._capsule()
        frame = cap.frame()
        describer = g.GoalDescriber(self.root, capsule=cap, policy=cap.policy, metrics=cap.metrics)
        self.addCleanup(lambda: describer._marks_dir and describer._marks_dir.cleanup())
        with patch('wender.perceive.vision_bridge.SubprocessVisionEngine.describe', new=self._vision_bound()):
            result = describer.describe(frame)
            self.assertEqual(result['schema'], 've.cu_describe/v1')
            with self.assertRaises(SurfaceRefused) as caught:
                describer.describe(dict(frame, sha256_rgb24='a'*64))
            self.assertEqual(caught.exception.code, 'goal_vision')
        self.assertEqual(self.inputs, [])

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_deadline_inside_dispatch_and_expired_owner
    def test_deadline_inside_dispatch_and_expired_owner(self):
        cap, surface, owner = self._capsule()
        cap.frame()
        self.clock.sleep(11)
        self._refuses(cap, dict(kind='type', text='ASCII'), 'goal_deadline')
        self.clock.now = 1
        cap, surface, owner = self._capsule()
        cap.frame()
        dispatch = owner.dispatch
        def delayed(surface, action, send):
            self.clock.sleep(11)
            return dispatch(surface, action, send)
        with patch.object(owner, 'dispatch', side_effect=delayed):
            self._refuses(cap, dict(kind='type', text='ASCII'), 'goal_deadline')
        self.assertFalse(owner.check_ready())

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_escape_requires_generation_evidence
    def test_escape_requires_generation_evidence(self):
        cap, _, _ = self._capsule()
        cap.frame()
        self._refuses(cap, dict(kind='key', keysym='Escape'), 'generation_key')
        self.rows = [_element(states=dict(focused=True, busy=False))]
        cap, _, _ = self._capsule()
        cap.frame()
        cap.input(dict(kind='key', keysym='Escape'))
        self.assertEqual(len(self.inputs), 1)

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_provider_ascii_type_through_real_loop
    def test_provider_ascii_type_through_real_loop(self):
        self.actions = [_proposal('type', text='ASCII'), _proposal()]
        result, _ = self._run()
        self.assertTrue(result['goal_verified'], result)
        self.assertEqual(self.inputs, [('type', {'text': 'ASCII'})])
        self.inputs.clear()
        self.actions = [_proposal('type', text='é')]
        result, _ = self._run()
        self.assertFalse(result['goal_verified'])
        self.assertEqual(result['failure_code'], 'goal_loop_failure')
        self.assertEqual(self.inputs, [])

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_ax_recovery_uses_replaced_capture
    def test_ax_recovery_uses_replaced_capture(self):
        cap, surface, _ = self._capsule()
        frame = self._transport('frame', {})
        window = frame['window']
        with patch.object(surface, '_call', side_effect=[frame,
            dict(elements=self.rows, frame_seq=1, window=window), dict(frame, seq=1),
            dict(elements=self.rows, frame_seq=1, window=window)]):
            acquired = cap.frame()
        self.assertEqual(acquired['seq'], 1)
        self.assertEqual(surface._goal_targets[0]['seq'], 1)
        with patch.object(surface, '_call', side_effect=[frame,
            dict(elements=self.rows, frame_seq=2, window=window), dict(frame, seq=1),
            dict(elements=self.rows, frame_seq=2, window=window), dict(frame, seq=1)]):
            with self.assertRaises(SurfaceError) as misaligned:
                cap.frame()
        self.assertEqual(misaligned.exception.code, 'alignment')
        with self.assertRaises(SurfaceRefused) as caught:
            surface.publish_goal_targets(frame_seq=0, generation=1,
                frame_digest=frame['sha256_rgb24'], elements={}, marks={})
        self.assertEqual(caught.exception.code, 'goal_stale')
        self.assertIsNone(surface._goal_targets)
        self.assertEqual(self.inputs, [])

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_absent_metadata_and_corrupt_png_refuse
    def test_absent_metadata_and_corrupt_png_refuse(self):
        for key in ('generation', 'capture_mono_ns', 'sha256_rgb24'):
            cap, surface, _ = self._capsule()
            frame = self._transport('frame', {})
            del frame[key]
            ax = dict(elements=self.rows, frame_seq=0, window=frame['window'])
            with patch.object(surface, '_call', side_effect=[frame, ax]):
                with self.assertRaises(SurfaceError) as caught:
                    cap.frame()
                self.assertEqual(caught.exception.code, 'malformed_capture')
        cap, _, _ = self._capsule()
        self.path.write_bytes(b'bad png')
        with self.assertRaises(SurfaceError) as caught:
            cap.frame()
        self.assertEqual(caught.exception.code, 'malformed_capture')
        self.assertEqual(self.inputs, [])

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_missing_vision_overlay_invalid_marks_and_ax_preference
    def test_missing_vision_overlay_invalid_marks_and_ax_preference(self):
        cap, surface, _ = self._capsule()
        frame = cap.frame()
        describer = g.GoalDescriber(self.root, capsule=cap, policy=cap.policy, metrics=cap.metrics)
        self.addCleanup(lambda: describer._marks_dir and describer._marks_dir.cleanup())
        digest = frame['sha256_rgb24']
        base = dict(schema='ve.cu_describe/v1', frame_sha256=digest, width=16, height=16,
                    changed=None, changed_regions=[], crops=[], timing_us={}, marks_png=None)
        for marks, code in (([dict(id=1, x=8, y=8, w=4, h=4)], 'goal_overlay'),
                            ([dict(id=1, x=15, y=8, w=4, h=4)], 'goal_marks'),
                            ([dict(id=0, x=8, y=8, w=4, h=4)], 'goal_marks')):
            with patch('wender.learn.cu_loop.BridgeDescriber.describe', return_value=dict(base, marks=marks)):
                with self.assertRaises(SurfaceRefused) as caught:
                    describer.describe(frame)
                self.assertEqual(caught.exception.code, code)
        self.marks = [dict(id=1, x=0, y=0, w=4, h=4, kind='button', score=1)]
        with patch('wender.perceive.vision_bridge.SubprocessVisionEngine.describe', new=self._vision_bound()):
            described = describer.describe(frame)
        self.assertEqual(described['marks'], [])
        self.assertEqual(self.inputs, [])

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_untrusted_error_codes_and_missing_stage_measurements
    def test_untrusted_error_codes_and_missing_stage_measurements(self):
        self.frame_error = SurfaceError('PRIVATE_ERROR_CODE', 'frame', 'PRIVATE_DETAIL')
        result, trace = self._run()
        self.assertEqual(result['failure_code'], 'goal_failure')
        self.assertNotIn('PRIVATE', json.dumps(result)+trace)
        self.assertIn('model', result['latency_missingness'])
        self.assertEqual(result['latency_us']['model']['count'], 0)
        self.assertEqual(result['model_calls'], 0)

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_real_cleanup_no_episode_directories
    def test_real_cleanup_no_episode_directories(self):
        ram = Path('/dev/shm/vqa-cu')
        before = {p for p in ram.iterdir()} if ram.exists() else set()
        retained = []
        original = g.GoalDescriber
        def construct(*args, **kwargs):
            describer = original(*args, **kwargs)
            retained.append(describer)
            return describer
        with patch.object(g, 'GoalDescriber', side_effect=construct):
            result, _ = self._run()
        after = {p for p in ram.iterdir()} if ram.exists() else set()
        self.assertTrue(result['goal_verified'])
        self.assertEqual(after, before)
        self.assertTrue(Path(result['final_frame']).exists())


    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_corrupt_png_refuses_publicly
    def test_corrupt_png_refuses_publicly(self):
        cap, _, _ = self._capsule()
        cap.frame()
        self.path.write_bytes(b'bad png')
        try:
            cap.frame()
        except SurfaceError as caught:
            self.assertEqual(caught.code, 'malformed_capture')
        else:
            self.fail('corrupt PNG acquired')
        self.assertEqual(self.inputs, [])


    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_overlay_directory_custody
    def test_overlay_directory_custody(self):
        cap, _, _ = self._capsule()
        frame = cap.frame()
        describer = g.GoalDescriber(self.root, capsule=cap, policy=cap.policy, metrics=cap.metrics)
        # A valid RAM PNG outside the bridge's owned overlay directory is not
        # sufficient. This fixture isolates custody from image validity.
        value = dict(schema='ve.cu_describe/v1', frame_sha256=frame['sha256_rgb24'],
            width=16, height=16, changed=None, changed_regions=[], crops=[], timing_us={},
            marks=[dict(id=1, x=8, y=8, w=4, h=4)], marks_png=str(self.path))
        with patch('wender.learn.cu_loop.BridgeDescriber.describe', return_value=value):
            with self.assertRaises(SurfaceRefused) as caught:
                describer.describe(frame)
        self.assertEqual(caught.exception.code, 'goal_overlay')
        self.assertEqual(self.inputs, [])


    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_input_owner_closes_on_failure
    def test_input_owner_closes_on_failure(self):
        cap, surface, owner = self._capsule()
        cap.frame()
        self.input_error = SurfaceNotReady('not_ready', 'type')
        with self.assertRaises(SurfaceNotReady):
            cap.input(dict(kind='type', text='ASCII'))
        self.assertEqual(len(self.inputs), 1)
        self.assertFalse(owner.check_ready())
        self.assertTrue(cap.input_effect_unknown)
        self._refuses(cap, dict(kind='type', text='ASCII'), 'goal_owner')
        before = len(self.calls)
        with self.assertRaises(SurfaceRefused) as caught:
            cap.frame()
        self.assertEqual(caught.exception.code, 'goal_owner')
        self.assertEqual(len(self.calls), before)

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_read_pacing_and_timeouts
    def test_read_pacing_and_timeouts(self):
        cap, surface, _ = self._capsule()
        self.frame_error = [SurfaceNotReady('not_ready', 'frame'), None]
        started = self.clock()
        cap.frame()
        self.assertAlmostEqual(self.clock()-started, .2)
        self.assertLessEqual(surface.timeout, 10-.2)
        self.assertEqual(cap.metrics.acquisition_attempts, 2)
        self.assertEqual(cap.metrics.read_retries, 1)
        self.assertEqual(cap.metrics.stages['acquisition']['count'], 1)
        self.assertGreaterEqual(cap.metrics.stages['acquisition']['total'], 199999)
        self.assertEqual(self.inputs, [])

    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_surface_rechecks_changed_ax_custody
    def test_surface_rechecks_changed_ax_custody(self):
        cap, surface, _ = self._capsule()
        frame = cap.frame()
        target = frame['lane']['elements'][0]['id']
        surface._latest_ax['elements'][0]['name'] = 'Account'
        self._refuses(cap, dict(kind='click', element_id=target), 'goal_stale')


    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_structural_ax_preserves_canvas_marks
    def test_structural_ax_preserves_canvas_marks(self):
        self.rows = [_element('Document', role='document', actionable=False,
                              box=[0, 0, 16, 16], states={})]
        self.marks = [dict(id=1, x=8, y=8, w=4, h=4, kind='button', score=1)]
        self.actions = [_proposal('click', mark_id=1), _proposal()]
        result, _ = self._run()
        self.assertTrue(result['goal_verified'], result)
        self.assertEqual(self.inputs, [('click', {'point': '10,10'})])
        self.inputs.clear()
        self.rows = [_element('Account', role='button', actionable=False,
                              box=[8, 8, 4, 4], states={'disabled': True})]
        self.actions = [_proposal('click', mark_id=1)]
        result, _ = self._run()
        self.assertFalse(result['goal_verified'])
        self.assertEqual(result['failure_code'], 'goal_loop_failure')
        self.assertEqual(self.inputs, [])


    # @anchor:wa.tests.web_goal_unit.GoalUnit.test_muse_configuration_and_direct_budgets
    def test_muse_configuration_and_direct_budgets(self):
        metrics = g.GoalMetrics(self.clock)
        backend = g.CountingMuseBackend(metrics=metrics)
        self.assertEqual(backend.effort_policy, 'auto')
        self.assertEqual(backend.timeout, 240)
        policy = g.GoalPolicy('design', self.clock()+10, self.clock)
        decider = g.GoalModelDecider(backend, policy=policy, metrics=metrics)
        self.addCleanup(decider.close)
        self.assertEqual(decider.images, 'marks')
        surface = GoalIdentitySurface(transport=self._transport)
        for steps, wall in ((0, 100), (1, 0), (True, 100)):
            with self.assertRaisesRegex(ValueError, 'GOAL_BUDGET'):
                g.run_goal(surface, goal='design', trace=io.StringIO(), max_steps=steps, max_ms=wall)
        self.assertEqual(self.calls, [])


# Each mutant weakens a specific check, and runs a previously passing public-API
# predicate. Source is compiled in an isolated interpreter, never edited on disk.
_MUTANTS = [
    ('surface_integer_geometry', 's', 'test_surface_integer_geometry',
     'all(type(v) is int for v in (x, y, w, h))',
     'all(type(v) in (int, float) for v in (x, y, w, h))'),
    ('reset_description_failure', 'g', 'test_failure_code_reset_on_new_acquisition',
     'self.capsule.failure_code = None', 'pass'),
    ('odd_surface_center', 's', 'test_odd_centers_through_run_goal',
     'cx, cy = x + w // 2, y + h // 2', 'cx, cy = x + w / 2, y + h / 2'),
    ('integer_ax_geometry', 'g', 'test_projection_failure_through_run_goal',
     'any(type(v) is not int for v in box)', 'any(type(v) not in (int, float) for v in box)'),
    ('integer_mark_geometry', 'g', 'test_description_failures_through_run_goal',
     "any(type(mark.get(k)) is not int for k in ('x', 'y', 'w', 'h'))",
     "any(type(mark.get(k)) not in (int, float) for k in ('x', 'y', 'w', 'h'))"),
    ('projection_failure_code', 'g', 'test_projection_failure_through_run_goal',
     "self.failure_code = 'goal_projection'", 'pass'),
    ('vision_failure_code', 'g', 'test_description_failures_through_run_goal',
     "self.capsule.failure_code = 'goal_vision'", 'pass'),
    ('marks_failure_code', 'g', 'test_description_failures_through_run_goal',
     "self.capsule.failure_code = 'goal_marks'", 'pass'),
    ('overlay_failure_code', 'g', 'test_description_failures_through_run_goal',
     "self.capsule.failure_code = 'goal_overlay'", 'pass'),
    ('reset_failure_code', 'g', 'test_failure_code_reset_on_new_acquisition',
     'self.failure_code = None', 'pass'),
    ('nonnegative_receipt', 's', 'test_input_never_replayed_and_uncertain',
     "if result['input_mono_ns'] < 0:", "if False:"),
    ('model_ascii', 'g', 'test_provider_ascii_type_through_real_loop',
     "if action['kind'] == 'type' and not action['text'].isascii():", "if False:"),

    ('legacy_workflow_isolation', 'c', 'test_cli_validation_and_isolation',
     "summary = run_goal(surface, goal=args.goal",
     "record_workflow(args.memory, {}, {'goal_verified': False})\n            summary = run_goal(surface, goal=args.goal"),
    ('exact_dispatch_owner', 'g', 'test_owner_closed_foreign_and_cleanup',
     "receipt = self.owner.dispatch(self.surface, action, send)", "receipt = send()"),
    ('normalized_input_receipt', 'g', 'test_blocked_controls_full_private_label',
     "return {'input_mono_ns': receipt['input_mono_ns']}", "return receipt"),

    ('ax_window_alignment', 's', 'test_invalid_box_and_alignment',
     "self._violation('window_mismatch')\n            raise SurfaceError('alignment', 'ax')",
     "self._violation('window_mismatch')"),
    ('ax_frame_alignment', 's', 'test_ax_recovery_uses_replaced_capture',
     "if attempt or not refetch:", "if False:"),

    ('cli_empty_goal', 'c', 'test_cli_validation_and_isolation',
     "not args.goal.strip() or ", ""),
    ('cli_task_exclusion', 'c', 'test_cli_validation_and_isolation',
     "args.task is not None or ", ""),
    ('cli_scripts_exclusion', 'c', 'test_cli_validation_and_isolation',
     "or args.scripts == 'on' ", ""),
    ('cli_analyze_exclusion', 'c', 'test_cli_validation_and_isolation',
     "or args.analyze ", ""),
    ('cli_dry_exclusion', 'c', 'test_cli_validation_and_isolation',
     "or args.dry_check:", ":"),
    ('cli_steps', 'c', 'test_cli_validation_and_isolation',
     "args.max_steps <= 0 or ", ""),
    ('auto_effort', 'g', 'test_muse_configuration_and_direct_budgets',
     "super().__init__(effort='auto', **kwargs)", "super().__init__(effort='low', **kwargs)"),
    ('model_images', 'g', 'test_muse_configuration_and_direct_budgets',
     "super().__init__(backend, images='marks')", "super().__init__(backend, images='full')"),

    ('structural_ax_canvas', 'g', 'test_structural_ax_preserves_canvas_marks',
     "return (target.get('actionable') is True or target.get('role', '').casefold() in",
     "return (True or target.get('role', '').casefold() in"),

    ('ax_box_alignment', 's', 'test_invalid_box_and_alignment',
     "raise SurfaceError('alignment', 'ax')", "pass"),
    ('dispatch_once', 'g', 'test_blocked_controls_full_private_label',
     "receipt = self.owner.dispatch(self.surface, action, send)",
     "receipt = self.owner.dispatch(self.surface, action, send)\n                receipt = self.owner.dispatch(self.surface, action, send)"),
    ('read_pacing', 'g', 'test_read_pacing_and_timeouts',
     "self.sleep(min(.2, max(0, self.deadline - self.clock())))",
     "self.sleep(0)"),
    ('read_deadline_clamp', 'g', 'test_read_pacing_and_timeouts',
     "self.surface.timeout = min(30, self.deadline - self.clock())",
     "self.surface.timeout = 30"),
    ('acquisition_owner', 'g', 'test_input_owner_closes_on_failure',
     "if not self.owner.check_ready():", "if False:"),
    ('current_ax_custody', 's', 'test_surface_rechecks_changed_ax_custody',
     "if sum(target == row for row in (self._latest_ax or {}).get('elements', [])) != 1:", "if False:"),

    ('canvas_grounding', 'g', 'test_canvas_mark_id_and_coordinate_refusal',
     "if action['kind'] == 'click' and sum(k in action for k in ('element_id', 'mark_id')) != 1:",
     "if False:"),
    ('duplicate_ax', 'g', 'test_projection_structure_and_duplicate',
     "or len(set(ids)) != len(ids)", "or False"),
    ('projection_structure', 'g', 'test_projection_structure_and_duplicate',
     "container=f'c{ordinal}'", "container='wrong'"),
    ('blocked_private_label', 'g', 'test_blocked_controls_full_private_label',
     "if any(_has(name, phrase) and not _has(self.goal, phrase) for phrase in _PHRASES):", "if False:"),
    ('narrow_intent', 'g', 'test_explicit_intent_and_frozen_filters',
     "and not _has(self.goal, phrase)", "and False"),
    ('frozen_filter', 's', 'test_explicit_intent_and_frozen_filters',
     "if not ax_options({'elements': [copy.deepcopy(target)]})[1]:", "if False:"),
    ('mark_block_overlap', 's', 'test_overlapping_mark_and_uncovered_canvas',
     "                        policy.check_target(row)\n                        raise SurfaceRefused('goal_ax_preferred', 'input')",
     "                        pass"),
    ('focus_custody', 's', 'test_focus_ascii_and_editing_keys',
     "if len(focused) != 1:", "if False:"),
    ('focus_missing', 's', 'test_focus_ascii_and_editing_keys',
     "elif self._goal_focus != (frame['generation'], json.dumps(frame['window'], sort_keys=True)):", "elif False:"),
    ('ascii_surface', 's', 'test_focus_ascii_and_editing_keys',
     "if not isinstance(action.get('text'), str) or not action['text'].isascii():", "if False:"),
    ('editing_keys', 's', 'test_focus_ascii_and_editing_keys',
     "if key not in ('Left', 'Right', 'Up', 'Down', 'Home', 'End', 'BackSpace', 'Delete', 'Return', 'space', 'Escape', 'Esc'):", "if False:"),
    ('canvas_focus_invalidation', 's', 'test_canvas_focus_loss',
     "if self._goal_focus != identity:\n            self._goal_focus = None", "if self._goal_focus is not None:\n            self._goal_focus = identity"),
    ('current_center', 's', 'test_grounding_centers_stale_and_fields',
     "if ('x' in action or 'y' in action) and (action.get('x'), action.get('y')) != (cx, cy):", "if False:"),
    ('exact_one_grounding', 's', 'test_grounding_centers_stale_and_fields',
     "if sum(k in action for k in ('element_id', 'mark_id')) != 1:", "if False:"),
    ('stale_publication', 's', 'test_grounding_centers_stale_and_fields',
     "if custody is None or custody[0] != self.last_frame:", "if custody is None:"),
    ('closed_input_fields', 's', 'test_grounding_centers_stale_and_fields',
     "if kind not in allowed or action.keys() - allowed[kind]:", "if kind not in allowed:"),
    ('step_budget', 'g', 'test_steps_and_wall_deadlines',
     "max_steps=max_steps, max_ms=remaining_ms", "max_steps=max_steps+1, max_ms=remaining_ms"),
    ('surface_deadline', 's', 'test_deadline_inside_dispatch_and_expired_owner',
     "if clock() >= deadline:\n            raise SurfaceRefused('goal_deadline', 'input')", "if False:\n            raise SurfaceRefused('goal_deadline', 'input')"),
    ('retry_bound', 'g', 'test_selective_read_retry', "range(15)", "range(14)"),
    ('selective_retry', 'g', 'test_selective_read_retry',
     "exc.code not in ('not_ready', 'refused_focus')", "False"),
    ('input_focus_receipt', 's', 'test_input_never_replayed_and_uncertain',
     "if not isinstance(result, dict) or result.get('focus_verified') is not True:", "if not isinstance(result, dict):"),
    ('generation_stop', 'g', 'test_generation_cannot_cancel',
     "if re.search(r'^(?:Stop|Detener)\\b', name, re.I):", "if False:"),
    ('generation_escape', 's', 'test_generation_cannot_cancel',
     "if key in ('Escape', 'Esc') and (not generation_known or self._latest_ax is None or _generating(self._latest_ax)):", "if False:"),
    ('missing_generation_evidence', 's', 'test_escape_requires_generation_evidence',
     "not generation_known or self._latest_ax is None or _generating(self._latest_ax)", "self._latest_ax is None or _generating(self._latest_ax)"),
    ('actual_provider_attempts', 'g', 'test_provider_attempts_and_malformed_done',
     "self.metrics.provider_attempts += 1", "self.metrics.provider_attempts += 0"),
    ('completion_final_frame', 'g', 'test_missing_final_frame_never_success',
     "and result['final_frame'] is not None", "and True"),
    ('completion_budget', 'g', 'test_steps_and_wall_deadlines',
     "raw['stop_reason'] == 'done'", "raw['stop_reason'] in ('done', 'max_steps')"),
    ('retained_screenshot', 'g', 'test_clean_done_and_final_copy',
     "result['final_frame'] = str(path)", "result['final_frame'] = str(path)\n                path.unlink()"),
    ('trace_projection', 'g', 'test_privacy_on_success_and_failure',
     "projected = {'event': 'goal_step'}", "projected = dict(raw)"),
    ('closed_failure_code', 'g', 'test_untrusted_error_codes_and_missing_stage_measurements',
     "and exc.code in _FAILURE_CODES", "and True"),
    ('cli_ascii', 'c', 'test_cli_validation_and_isolation',
     "if not args.goal.strip() or not args.goal.isascii():", "if not args.goal.strip():"),
    ('cli_conflicts', 'c', 'test_cli_validation_and_isolation',
     "if args.task is not None or args.scripts == 'on' or args.analyze or args.dry_check:", "if False:"),
    ('cli_wall', 'c', 'test_cli_validation_and_isolation',
     "not math.isfinite(args.max_seconds) or ", ""),
    ('cli_save_frame', 'c', 'test_cli_validation_and_isolation',
     "if args.save_frame is not None:", "if False:"),
    ('registered_owner', 's', 'test_owner_closed_foreign_and_cleanup',
     "if session is None or not session[0].check_ready():", "if session is None:"),
    ('vision_frame_association', 'g', 'test_vision_association_and_overlay',
     "(frame['width'], frame['height'], frame['sha256_rgb24'])):",
     "(result.get('width'), result.get('height'), result.get('frame_sha256'))):"),
    ('recovered_frame', 'g', 'test_ax_recovery_uses_replaced_capture',
     "        return frame", "        return dict(frame, seq=0)"),
    ('capture_validation', 'g', 'test_corrupt_png_refuses_publicly',
     "frame = _validate_frame(self.surface.last_frame)", "frame = dict(self.surface.last_frame)"),
    ('overlay_custody', 'g', 'test_overlay_directory_custody',
     "if marks and (not overlay or self._marks_dir is None or", "if False and (not overlay or self._marks_dir is None or"),
    ('mark_geometry', 'g', 'test_missing_vision_overlay_invalid_marks_and_ax_preference',
     "if min(x, y) < 0 or min(w, h) <= 0 or x+w > frame['width'] or y+h > frame['height']:", "if False:"),
    ('ax_preference', 'g', 'test_missing_vision_overlay_invalid_marks_and_ax_preference',
     "if not covered:", "if True:"),
    ('cleanup_marks', 'g', 'test_real_cleanup_no_episode_directories',
     "describer._marks_dir.cleanup()", "pass"),
]


# @anchor:wa.tests.web_goal_unit.GoalMutationAudit
class GoalMutationAudit(unittest.TestCase):
    # @anchor:wa.tests.web_goal_unit.GoalMutationAudit.test_spec_mutants
    def test_spec_mutants(self):
        import subprocess
        import sys
        # Import changed module before test module, so all consumers bind the
        # mutant definitions. A failing assertion (not import/setup error) kills it.
        program = '''
import importlib, io, sys, unittest
name, old, new, method = sys.argv[1:]
module = importlib.import_module(name)
source = open(module.__file__, encoding='utf-8').read()
assert old in source, 'mutation site absent'
mutated = new.join(source.rsplit(old, 1))
exec(compile(mutated, module.__file__, 'exec'), module.__dict__)
suite = unittest.defaultTestLoader.loadTestsFromName('tests.test_web_goal_unit.GoalUnit.' + method)
stream = io.StringIO()
result = unittest.TextTestRunner(stream=stream).run(suite)
if result.failures and not result.errors:
    sys.exit(0)
print(stream.getvalue())
sys.exit(1)
'''
        modules = dict(g='wender.learn.web_goal', s='wender.perceive.srf_identity', c='tools.cu_web')
        for name, module, method, old, new in _MUTANTS:
            with self.subTest(mutant=name):
                result = subprocess.run([sys.executable, '-B', '-c', program,
                    modules[module], old, new, method], text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, name+'\n'+result.stdout+result.stderr)
                print('WGOAL MUTANT '+name+' KILLED '+method+' [MEASURED]')
