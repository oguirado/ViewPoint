# @module {"id":"wa.tests.test_cu_loop_unit","layer":2,"owner":"LEARN","deps":["wa.learn.cu_loop","wa.learn.cu_tasks","wa.learn.model_client","wa.tests.test_cu_wiring_unit"],"book":"HB-WA-s4-learn"}
"""C1/C3/C4 runner tests. PNG fixtures exist only in /dev/shm."""
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import subprocess

from wender.learn.cu_loop import (FakeCapsule, ReplayDecider, SrfCapsule,
                                 CommandDescriber, main, run_loop, _encode_png)


class _TrustedDispatcher:
    """Explicit trusted test-only transport owner."""
    # @anchor:wa.tests.test_cu_loop_unit._TrustedDispatcher.dispatch
    def dispatch(self, surface, action, send):
        return send()


def proposal(kind='key', expectation=None, **fields):
    if kind == 'key':
        fields.setdefault('keysym', 'Return')
    return {'schema': 'cu.action/v1', 'observation': 'test',
            'action': {'kind': kind, **fields},
            'expectation': expectation or {'kind': 'state_change'}, 'confidence': 1}


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Describer:
    def __init__(self, regions=None):
        self.calls = []
        self.regions = regions if regions is not None else [{'x': 0, 'y': 0, 'w': 10, 'h': 10, 'score': 1}]

    def describe(self, frame, *, prev=None):
        self.calls.append((frame, prev))
        changed = None if prev is None else frame['sha256_rgb24'] != prev['sha256_rgb24']
        return {'changed': changed, 'changed_regions': self.regions if changed else []}


class TestCuLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/dev/shm')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = Clock()

    def capsule(self, states, colors=None, elements=None):
        rows = []
        for i, state in enumerate(states):
            name = f'f{i}.png'
            (self.root / name).write_bytes(_encode_png(bytes(colors[i] if colors else (i, 0, 0)) * 256, 16, 16))
            row = {'seq': i, 'png': name, 'tick': i}
            if state is not None:
                row['state_sha256'] = hashlib.sha256(state.encode()).hexdigest()
            if elements:
                row['elements'] = elements
            rows.append(row)
        (self.root / 'index.json').write_text(json.dumps(rows))
        return FakeCapsule(self.root)

    def run_case(self, capsule, actions, describer=None, **kwargs):
        trace = io.StringIO()
        summary = run_loop(capsule, describer or Describer(), ReplayDecider(actions),
                           goal='test', trace=trace, clock=self.clock,
                           sleep=self.clock.sleep, **kwargs)
        return summary, [json.loads(line) for line in trace.getvalue().splitlines()]

    def test_happy_path_three_then_done(self):
        cap = self.capsule(['a', 'b', 'c', 'd'])
        desc = Describer()
        summary, rows = self.run_case(cap, [proposal()] * 3 + [proposal('done')], desc)
        self.assertEqual({k: summary[k] for k in ('steps', 'verified_ok', 'failures', 'stop_reason')}, {'steps': 3, 'verified_ok': 3, 'failures': 0, 'stop_reason': 'done'})
        self.assertFalse(summary['goal_verified'])
        self.assertEqual(summary['terminal_observation'], 'test')
        self.assertEqual(len(cap.inputs), 3)
        self.assertEqual(len(rows), 4)
        self.assertEqual([r['step'] for r in rows], [1, 2, 3, 4])
        self.assertEqual(desc.calls[2][1]['seq'], 0)
        self.assertFalse(rows[-1]['verify']['ok'])
        self.assertIsNone(rows[-1]['input'])

    def test_failure_counted_despite_visual_change(self):
        cap = self.capsule(['a', 'a'])
        summary, rows = self.run_case(cap, [proposal(), proposal('done')])
        self.assertEqual(summary['failures'], 1)
        self.assertEqual(rows[0]['verify']['method'], 'state_sha256')
        self.assertFalse(rows[0]['verify']['ok'])

    def test_three_consecutive_failures_stop(self):
        cap = self.capsule(['a'] * 5, colors=[(0, 0, 0)] * 5)
        summary, rows = self.run_case(cap, [proposal()] * 5)
        self.assertEqual(summary['stop_reason'], 'verify_failures')
        self.assertEqual(summary['steps'], 3)
        self.assertEqual(len(rows), 3)
        self.assertGreaterEqual(self.clock.now, 4.5)

    def test_success_resets_failure_streak(self):
        cap = self.capsule(['a', 'a', 'b', 'b', 'b'])
        summary, _ = self.run_case(cap, [proposal()] * 4 + [proposal('done')])
        self.assertEqual(summary['stop_reason'], 'done')
        self.assertEqual(summary['failures'], 3)

    def test_step_budget(self):
        cap = self.capsule(['a', 'b', 'c'])
        summary, rows = self.run_case(cap, [proposal()] * 3, max_steps=2)
        self.assertEqual(summary['stop_reason'], 'max_steps')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]['budget']['steps_left'], 0)

    def test_time_budget_limits_poll(self):
        cap = self.capsule(['a'], colors=[(0, 0, 0)])
        summary, rows = self.run_case(cap, [proposal()], max_ms=70)
        self.assertEqual(summary['stop_reason'], 'max_ms')
        self.assertAlmostEqual(self.clock.now, .07)
        self.assertEqual(rows, [])
        self.assertEqual(summary['failure']['stage'], 'after_capture')

    def test_expired_decision_does_not_act(self):
        cap = self.capsule(['a', 'b'])
        clock = self.clock
        class Slow(ReplayDecider):
            def decide(self, **kwargs):
                clock.sleep(.1)
                return super().decide(**kwargs)
        result = run_loop(cap, Describer(), Slow([proposal()]), goal='x', trace=io.StringIO(),
                          max_ms=50, clock=clock, sleep=clock.sleep)
        self.assertEqual(result['stop_reason'], 'max_ms')
        self.assertEqual(cap.inputs, [])

    # @anchor:wa.tests.test_cu_loop_unit.TestCuLoop.test_trace_schema
    def test_trace_schema(self):
        summary, rows = self.run_case(self.capsule(['a', 'b']), [proposal()], max_steps=1)
        row = rows[0]
        self.assertEqual(set(row), {'schema', 'step', 'frame', 'describe', 'model', 'input', 'after', 'verify', 'budget',
                                             'predicate_before', 'predicate_after',
                                             'predicate_before_unknown', 'predicate_after_unknown',
                                             'status', 'status_before', 'progress', 'grounding'})
        for key, fields in {'frame': {'seq', 'sha256_rgb24', 'generation'},
                            'describe': {'changed', 'regions_n'},
                            'grounding': {'raw_coordinates', 'osc2_transform'},
                            'model': {'backend', 'latency_ms', 'action', 'drafted_ahead', 'corrections', 'correction_classes'},
                            'input': {'input_mono_ns'}, 'after': {'seq', 'sha256_rgb24'},
                            'verify': {'method', 'ok', 'detail'},
                            'budget': {'steps_left', 'ms_left'}}.items():
            self.assertEqual(set(row[key]), fields)
        self.assertEqual(row['model']['drafted_ahead'], 0)
        self.assertEqual(row['schema'], 'cu.trace/v1')
        self.assertEqual(len(row['frame']['sha256_rgb24']), 64)

    def test_trace_records_drafted_ahead(self):
        from wender.learn.model_client import parse_muse_output, validate_action
        first = proposal()
        event = {'payload_type': 'run.output.delta',
                 'payload': {'text': json.dumps(first) + json.dumps(proposal('wait', ms=800))}}
        parsed = validate_action(parse_muse_output(json.dumps(event)), 16, 16)
        summary, rows = self.run_case(self.capsule(['a', 'b']), [parsed], max_steps=1)
        self.assertEqual(rows[0]['model']['drafted_ahead'], 1)
        self.assertEqual(rows[0]['model']['action'], first['action'])
        self.assertEqual(summary['steps'], 1)

    def test_element_id_resolved(self):
        cap = self.capsule(['a', 'b'], elements=[{'id': 'save', 'bbox': {'x': 2, 'y': 4, 'w': 10, 'h': 8}}])
        self.run_case(cap, [proposal('click', element_id='save')], max_steps=1)
        self.assertEqual(cap.inputs[0], {'kind': 'click', 'element_id': 'save', 'x': 7, 'y': 8})

    def test_unknown_element_fails_without_input(self):
        cap = self.capsule(['a', 'b'])
        summary, rows = self.run_case(cap, [proposal('click', element_id='missing')] * 2)
        self.assertEqual(summary['failure']['stage'], 'validation')
        self.assertEqual(rows, [])
        self.assertEqual(cap.inputs, [])

    def test_region_overlap_and_miss(self):
        for region, expected in [({'x': 5, 'y': 5, 'w': 2, 'h': 2}, True),
                                 ({'x': 10, 'y': 10, 'w': 2, 'h': 2}, False)]:
            with self.subTest(region=region):
                cap = self.capsule([None, None])
                _, rows = self.run_case(cap, [proposal(expectation={'kind': 'changed_region', 'region': region})], max_steps=1, pixel_only=True)
                self.assertEqual(rows[0]['verify']['ok'], expected)
                self.assertEqual(rows[0]['verify']['method'], 'changed_region')

    def test_missing_state_fails_closed(self):
        cap = self.capsule([None, None])
        summary, rows = self.run_case(cap, [proposal()], max_steps=1)
        self.assertEqual(summary['failure']['stage'], 'capture')
        self.assertEqual(cap.inputs, [])
        self.assertEqual(rows, [])

    def test_explicit_pixel_only_mode(self):
        _, rows = self.run_case(self.capsule([None, None]), [proposal()], max_steps=1, pixel_only=True)
        self.assertTrue(rows[0]['verify']['ok'])
        self.assertEqual(rows[0]['verify']['method'], 'none')

    def test_delayed_frame_polls_without_repeating_input(self):
        inner = self.capsule(['a', 'b'])
        clock = self.clock

        class Delayed:
            def frame(self, *, state=True):
                if clock() < .06:
                    position = inner._position
                    inner._position = 0
                    frame = inner.frame(state=state)
                    inner._position = position
                    return frame
                return inner.frame(state=state)

            def input(self, action):
                return inner.input(action)

        summary, _ = self.run_case(Delayed(), [proposal()], max_steps=1)
        self.assertEqual(summary['verified_ok'], 1)
        self.assertEqual(len(inner.inputs), 1)
        self.assertAlmostEqual(clock(), .06)

    def test_state_only_change_is_sufficient(self):
        cap = self.capsule(['a', 'b'], colors=[(0, 0, 0), (0, 0, 0)])
        summary, rows = self.run_case(cap, [proposal()], max_steps=1)
        self.assertEqual(summary['verified_ok'], 1)
        self.assertEqual(rows[0]['frame']['sha256_rgb24'], rows[0]['after']['sha256_rgb24'])

    def test_wait_is_bounded_and_has_no_input(self):
        cap = self.capsule(['a'])
        summary, rows = self.run_case(cap, [proposal('wait', ms=1000)], max_ms=50)
        self.assertEqual(summary['stop_reason'], 'max_ms')
        self.assertAlmostEqual(self.clock(), .05)
        self.assertEqual(cap.inputs, [])
        self.assertEqual(rows, [])
        self.assertIsNone(summary['failure']['receipt'])
        self.assertEqual(summary['failure']['stage'], 'wait')
        self.assertEqual(summary['terminal_attempt']['stage'], 'wait')
        self.assertEqual(summary['final_frame']['seq'], 0)

    def test_type_is_available_to_capable_protocol_provider(self):
        cap = self.capsule(['a', 'b'])
        summary, _ = self.run_case(cap, [proposal('type', text='hello')], max_steps=1)
        self.assertEqual(summary['verified_ok'], 1)
        self.assertEqual(cap.inputs[0]['text'], 'hello')

    def test_invalid_confidence_fails_closed(self):
        action = proposal()
        action['confidence'] = float('nan')
        cap = self.capsule(['a', 'b'])
        summary, rows = self.run_case(cap, [action] * 2)
        self.assertEqual(summary['failure']['stage'], 'validation')
        self.assertEqual(rows, [])
        self.assertEqual(cap.inputs, [])

    def test_zero_budget(self):
        summary, rows = self.run_case(self.capsule(['a']), [], max_steps=0)
        self.assertEqual(summary['steps'], 0)
        self.assertEqual(rows, [])

    # @anchor:wa.tests.test_cu_loop_unit.TestCuLoop.test_srf_and_describer_commands
    @patch('wender.learn.cu_loop.subprocess.run')
    def test_srf_and_describer_commands(self, run):
        frame = self.capsule(['a']).frame()
        run.return_value.stdout = json.dumps(frame)
        cap = SrfCapsule('box', '/custom/capsule', dispatcher=_TrustedDispatcher())
        cap.frame()
        self.assertEqual(run.call_args.args[0], ['/custom/capsule', 'frame', '--container', 'box', '--state'])
        run.return_value.stdout = '{"input_mono_ns":1}'
        cap.input({'kind': 'click', 'x': 4, 'y': 8})
        self.assertIn('4,8', run.call_args.args[0])
        cap.input({'kind': 'key', 'keysym': 'Return'})
        self.assertEqual(run.call_args.args[0][-2:], ['--key', 'Return'])
        with self.assertRaises(ValueError):
            cap.input({'kind': 'type', 'text': 'hello'})
        CommandDescriber(['describe']).describe({'path': 'new'}, prev={'path': 'old'})
        self.assertEqual(run.call_args.args[0], ['describe', '--frame', 'new', '--prev', 'old', '--json'])
        cap.state_available = False
        run.return_value.stdout = json.dumps(frame)
        cap.frame()
        self.assertNotIn('--state', run.call_args.args[0])

    @patch('wender.learn.cu_loop.subprocess.run')
    def test_transport_error_is_not_retried(self, run):
        run.side_effect = subprocess.CalledProcessError(3, ['capsule'])
        with self.assertRaises(subprocess.CalledProcessError):
            SrfCapsule('box').frame()
        self.assertEqual(run.call_count, 1)

    def test_cli_replay_summary_and_trace(self):
        self.capsule(['a', 'b'])
        replay = self.root / 'replay.json'
        replay.write_text(json.dumps([proposal(), proposal('done')]))
        trace = self.root / 'trace.jsonl'
        from tests.test_cu_wiring_unit import stub_ve
        stub_ve(self.root)
        with patch('sys.stdout', new_callable=io.StringIO) as out:
            status = main(['--goal', 'test', '--capsule', 'fake:' + str(self.root),
                           '--decider', 'replay:' + str(replay), '--trace', str(trace),
                           '--ve-root', str(self.root)])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out.getvalue())['stop_reason'], 'done')
        self.assertEqual(len(trace.read_text().splitlines()), 2)

    def test_module_cli_subprocess(self):
        self.capsule(['a', 'b'])
        replay = self.root / 'cli-replay.json'
        replay.write_text(json.dumps([proposal(), proposal('done')]))
        from tests.test_cu_wiring_unit import stub_ve
        stub_ve(self.root)
        trace = self.root / 'cli-trace.jsonl'
        command = [sys.executable, '-B', '-m', 'wender.learn.cu_loop',
                   '--goal', 'test', '--capsule', 'fake:' + str(self.root),
                   '--decider', 'replay:' + str(replay), '--trace', str(trace),
                   '--ve-root', str(self.root)]
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=10)
        summary = json.loads(result.stdout)
        self.assertEqual({k: summary[k] for k in ('steps', 'verified_ok', 'failures', 'stop_reason')}, {'steps': 1, 'verified_ok': 1, 'failures': 0, 'stop_reason': 'done'})
        self.assertFalse(summary['goal_verified'])
        self.assertEqual(len(trace.read_text().splitlines()), 2)


class TestNoProgress(unittest.TestCase):
    setUp = TestCuLoop.setUp
    capsule = TestCuLoop.capsule
    run_case = TestCuLoop.run_case
    def trajectory(self, statuses, predicate, **kwargs):
        from wender.learn.cu_tasks import extract_status, evaluate_predicate, predicate_distance
        cap = self.capsule([str(i) for i in range(len(statuses))])
        original = cap.frame
        def frame(**kw):
            result = original(**kw)
            result['lane'].update(statuses[cap._position])
            return result
        cap.frame = frame
        return self.run_case(cap, [proposal()] * (len(statuses) - 1),
            max_steps=len(statuses)-1,
            goal_check=lambda f: evaluate_predicate(predicate, extract_status(f)),
            goal_distance=lambda f: predicate_distance(predicate, extract_status(f)), **kwargs)

    def test_detour_with_flat_or_worse_distance_does_not_stop(self):
        summary, rows = self.trajectory([
            {'route': 'home', 'inbox_open': 0, 'read': 0},
            {'route': 'inbox', 'inbox_open': 1, 'read': 0},
            {'route': 'inbox', 'inbox_open': 1, 'read': 1},
            {'route': 'home', 'inbox_open': 1, 'read': 1}],
            'route == "home" and inbox_open == 1')
        self.assertEqual(summary['stop_reason'], 'goal_reached')
        self.assertEqual(summary['steps'], 3)
        self.assertEqual([r['progress']['stagnant_steps'] for r in rows], [0, 0, 0])

    def test_changing_focus_revision_is_meaningful(self):
        summary, _ = self.trajectory([
            {'route': 'inbox', 'focus': str(i), 'revision': i}
            for i in range(6)], 'route == "home"')
        self.assertEqual(summary['stop_reason'], 'max_steps')

    def test_read_ack_noop_ignores_digest_churn(self):
        cap = self.capsule(['a', 'b', 'c', 'd'])
        original = cap.frame
        def frame(**kwargs):
            result = original(**kwargs)
            result['lane'].update(route='inbox', tick=0, revision=0,
                                  unread_count=0, read_count=1, acked_count=1)
            return result
        cap.frame = frame
        summary, rows = self.run_case(cap, [
            proposal('click', x=1, y=1), proposal('click', x=2, y=2),
            proposal('click', x=1, y=1)], max_steps=3)
        self.assertEqual(summary['stop_reason'], 'no_progress')
        self.assertEqual(summary['steps'], 2)
        self.assertTrue(all(r['verify']['ok'] for r in rows))

    def test_cycle_stops_on_repeated_seen_snapshots(self):
        summary, rows = self.trajectory([{'tick': tick, 'revision': 0}
            for tick in (0, 1, 0, 1, 0)], 'tick >= 3')
        self.assertEqual(summary['stop_reason'], 'no_progress')
        self.assertEqual(summary['steps'], 3)
        self.assertEqual([r['progress']['stagnant_steps'] for r in rows], [0, 1, 2])

    def test_new_snapshot_resets_repeat_streak(self):
        summary, rows = self.trajectory([{'tick': tick, 'revision': 0}
            for tick in (0, 0, 1, 1, 1)], 'tick >= 3')
        self.assertEqual(summary['stop_reason'], 'no_progress')
        self.assertEqual([r['progress']['stagnant_steps'] for r in rows], [1, 0, 1, 2])

    def test_refusal_repeats_for_same_action_without_verification(self):
        cap = self.capsule(['a'] * 4)
        original = cap.frame
        def frame(**kwargs):
            result = original(**kwargs)
            result['lane']['last_refusal_code'] = 'ILLEGAL_RULE' if cap._position else None
            return result
        cap.frame = frame
        summary, rows = self.run_case(cap, [proposal()] * 3, max_steps=3,
                                      no_progress_steps=10)
        self.assertEqual(summary['stop_reason'], 'no_progress')
        self.assertEqual(summary['steps'], 2)
        self.assertFalse(any(r['verify']['ok'] for r in rows))

    def test_refusal_on_different_actions_does_not_stop(self):
        summary, _ = self.trajectory([
            {'tick': i, 'last_refusal_code': 'ILLEGAL_RULE' if i else None}
            for i in range(2)], 'tick >= 3', no_progress_steps=10)
        self.assertEqual(summary['stop_reason'], 'max_steps')
        cap = self.capsule(['a', 'b', 'c'])
        original = cap.frame
        def frame(**kwargs):
            result = original(**kwargs)
            result['lane']['last_refusal_code'] = 'ILLEGAL_RULE'
            return result
        cap.frame = frame
        summary, _ = self.run_case(cap, [proposal('key', keysym='A'),
                                       proposal('key', keysym='B')], max_steps=2)
        self.assertEqual(summary['stop_reason'], 'max_steps')

    def test_goal_has_priority(self):
        summary, rows = self.trajectory([{'tick': 0}, {'tick': 1}],
            'tick >= 1', no_progress_steps=1)
        self.assertEqual(summary['stop_reason'], 'goal_reached')
        self.assertEqual(rows[-1]['progress']['distance_after'], 0)

    def test_missing_predicate_evidence_is_telemetry_only(self):
        summary, rows = self.trajectory([{}] * 4, 'inbox_open == 0')
        self.assertEqual(summary['stop_reason'], 'max_steps')
        self.assertIsNone(rows[-1]['progress']['distance_after'])
        self.assertFalse(summary['goal_verified'])

    def test_invalid_no_progress_limit(self):
        with self.assertRaises(ValueError):
            self.run_case(self.capsule(['a', 'b']), [proposal()], no_progress_steps=0)

    def test_inbox_state_change_is_meaningful_without_distance(self):
        cap = self.capsule(['a', 'b', 'c', 'd'])
        original = cap.frame
        def frame(**kwargs):
            result = original(**kwargs)
            result['lane'].update(tick=0, revision=0, route='inbox',
                                  read_count=cap._position)
            return result
        cap.frame = frame
        summary, rows = self.run_case(cap, [proposal()] * 3, max_steps=3)
        self.assertEqual(summary['stop_reason'], 'max_steps')
        self.assertTrue(all(r['progress']['stagnant_steps'] == 0 for r in rows))

    def test_absent_status_is_not_a_repeated_snapshot(self):
        cap = self.capsule([None] * 4)
        summary, _ = self.run_case(cap,
            [proposal(expectation={'kind': 'none'})] * 3,
            max_steps=3, pixel_only=True)
        self.assertEqual(summary['stop_reason'], 'max_steps')

    def test_unverified_step_does_not_count_as_snapshot_repeat(self):
        cap = self.capsule(['a', 'b', 'b', 'c', 'd'])
        original = cap.frame
        def frame(**kwargs):
            result = original(**kwargs)
            result['lane'].update(tick=0, revision=0)
            return result
        cap.frame = frame
        summary, rows = self.run_case(cap, [proposal()] * 4, max_steps=4)
        self.assertEqual(summary['stop_reason'], 'no_progress')
        self.assertEqual(summary['steps'], 4)
        self.assertEqual([r['progress']['stagnant_steps'] for r in rows], [1, 0, 1, 2])
