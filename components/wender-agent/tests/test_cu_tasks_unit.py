# @module {"id":"wa.tests.cu_tasks_unit","layer":2,"owner":"ARCH","deps":["wa.learn.cu_tasks","wa.learn.cu_loop"],"book":"HB-WA-s4-learn"}
"""Safe grammar, recorded status evidence and authoritative goal termination."""
import io
import json
from pathlib import Path
import tempfile
import unittest

from tests import test_cu_loop_unit as loop_fixtures
from tests.test_cu_loop_unit import Clock, Describer, proposal
from wender.learn.cu_loop import ReplayDecider, run_loop
from wender.learn.cu_tasks import (DEFAULT_TASKS_FILE, evaluate_predicate,
    extract_status, load_task, parse_predicate, render_predicate)


class TestPredicates(unittest.TestCase):
    def test_precedence_grouping_and_nested_not(self):
        env = {'tick': 1, 'revision': 2}
        for source, expected in [
                ('tick == 1 or tick == 2 and revision == 9', True),
                ('(tick == 1 or tick == 2) and revision == 9', False),
                ('not tick == 1 or revision == 2', True),
                ('not (tick == 1 or revision == 2)', False),
                ('not not tick == 1', True),
                ('(tick) == (1)', True)]:
            with self.subTest(source=source):
                self.assertIs(evaluate_predicate(source, env), expected)

    def test_every_operator(self):
        for op, yes, no in [('==', 2, 3), ('!=', 3, 2), ('<', 1, 2),
                            ('<=', 2, 3), ('>', 3, 2), ('>=', 2, 1)]:
            with self.subTest(op=op):
                self.assertIs(evaluate_predicate('tick ' + op + ' 2', {'tick': yes}), True)
                self.assertIs(evaluate_predicate('tick ' + op + ' 2', {'tick': no}), False)
        self.assertTrue(evaluate_predicate('route != "home"', {'route': 'inbox'}))
        self.assertTrue(evaluate_predicate('tick == -1', {'tick': -1}))

    def test_strings_and_escapes(self):
        self.assertTrue(evaluate_predicate(r'''focus == 'a\n\r\t\b\f\/\\\"\'\u0041' ''',
                                           {'focus': 'a\n\r\t\b\f/\\"\'A'}))
        self.assertTrue(evaluate_predicate('"home" == route', {'route': 'home'}))

    def test_unknown_is_never_true_even_under_negation_or_or(self):
        for src in ('tick == 0', 'not tick == 0', 'tick != 0',
                    'tick == 0 or route == "home"'):
            self.assertIsNone(evaluate_predicate(src, {'route': 'home'}))
        for item in (None, True, '0', 0.0, 9007199254740992):
            self.assertIsNone(evaluate_predicate('tick == 0', {'tick': item}))

    def test_malformed_and_type_errors(self):
        for src in ('', 'unknown == 1', '__import__("os")', 'tick', '1', 'true',
                    'tick = 1', 'tick === 1', 'tick ! 1', 'tick == 1.0',
                    'tick == 1e2', 'tick == 9007199254740992',
                    'tick < 1 < 2', 'tick == 1 junk', '(tick == 1',
                    'tick == 1)', 'tick ==', 'tick == "1"', 'route < "home"',
                    'tick and revision', 'not tick', 'tick == 1 && tick == 2',
                    'tick == 1 | tick == 2', 'route == "unterminated',
                    r'route == "bad\q"', r'route == "bad\u123"',
                    '(tick == 1) == (tick == 1)'):
            with self.subTest(src=src), self.assertRaises(ValueError):
                parse_predicate(src)

    def test_plain_language_preserves_grouping(self):
        self.assertEqual(render_predicate('route == "inbox"'), 'success means: (route is inbox)')
        text = render_predicate('not (tick < 1 or revision >= 2) and inbox_open != 0')
        self.assertEqual(text, 'success means: (not (((tick is less than 1) or (revision is at least 2))) and (inbox_open is not 0))')


class TestRecordedStatus(unittest.TestCase):
    def setUp(self):
        self.frame = json.loads((Path(__file__).parent / 'fixtures' /
            'real_capsule_frame_holdings_08d409a.json').read_text())['frame']

    def test_real_frame_and_normalized_lane(self):
        expected = dict(tick=0, revision=0, cash_minor=5000000, employed=0,
                        inbox_open=1, route='home')
        self.assertEqual(extract_status(self.frame), expected)
        normalized = {'lane': self.frame['state']}
        self.assertEqual(extract_status(normalized), expected)
        self.assertTrue(evaluate_predicate('route == "home" and cash_minor == 5000000', expected))
        self.assertFalse(evaluate_predicate('route == "inbox"', expected))
        self.assertIsNone(evaluate_predicate('focus == ""', expected))
        if not Path(DEFAULT_TASKS_FILE).is_file():
            self.skipTest('external Holdings task corpus not present; set WENDER_CU_TASKS_FILE')
        self.assertIsNone(evaluate_predicate(load_task('hb_tf_a-01-v1')['success_predicate'], expected))

    @unittest.skipUnless(Path(DEFAULT_TASKS_FILE).is_file(), 'external Holdings task corpus not present; set WENDER_CU_TASKS_FILE')
    def test_focus_uses_current_node_anchor(self):
        lane = self.frame['state']
        lane['state']['nodes'][2]['states']['focused'] = 'true'
        self.assertEqual(extract_status(self.frame)['focus'], '/holdings/h3-nav-home')
        self.assertTrue(evaluate_predicate(load_task('hb_tf_a-01-v1')['success_predicate'],
                                           extract_status(self.frame)))
        lane['state']['nodes'][3]['states']['focused'] = 'true'
        self.assertNotIn('focus', extract_status(self.frame))

    def test_missing_and_conflicting_evidence(self):
        lane = self.frame['state']
        lane['state']['nodes'][-1]['states'].pop('cash_minor')
        self.assertNotIn('cash_minor', extract_status(self.frame))
        lane['tick'] = 1
        self.assertNotIn('tick', extract_status(self.frame))
        lane['state']['source'].pop('title')
        self.assertNotIn('route', extract_status(self.frame))
        self.assertEqual(extract_status({}), {})
        self.assertIsNone(evaluate_predicate('inbox_open == 0', {}))


class TestTaskLoading(unittest.TestCase):
    @unittest.skipUnless(Path(DEFAULT_TASKS_FILE).is_file(), 'external Holdings task corpus not present; set WENDER_CU_TASKS_FILE')
    def test_corpus_and_inbox_driven_tasks(self):
        rows = [json.loads(line) for line in Path(DEFAULT_TASKS_FILE).read_text().splitlines()]
        self.assertEqual(len(rows), 87)
        inbox = [row for row in rows if row['scenario'] == 'inbox-driven']
        self.assertTrue(inbox)
        for row in rows:
            with self.subTest(task=row['task_id']):
                self.assertEqual(load_task(row['task_id']), row)
                parse_predicate(row['success_predicate'])
        self.assertEqual(load_task('hb_tf_c-pace-step1-v1')['seed'], 1001)
        self.assertEqual(load_task('hb_tf_c-pace-step1-v1')['max_actions'], 40)
        with self.assertRaises(ValueError):
            load_task('no-such-task')

    @unittest.skipUnless(Path(DEFAULT_TASKS_FILE).is_file(), 'external Holdings task corpus not present; set WENDER_CU_TASKS_FILE')
    def test_duplicate_id_and_invalid_task_rejected(self):
        row = load_task('hb_tf_a-02-v1')
        with tempfile.TemporaryDirectory(dir='/dev/shm') as tmp:
            path = Path(tmp) / 'tasks.jsonl'
            for content in (json.dumps(row) + '\n' + json.dumps(row),
                            json.dumps(dict(row, max_actions=0)),
                            json.dumps(dict(row, success_predicate='route == bad'))):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    load_task(row['task_id'], path)


class TestGoalLoop(unittest.TestCase):
    def test_changed_people_does_not_satisfy_inbox_then_inbox_stops(self):
        # Reuse the RAM fixture builder without inheriting/discovering its tests.
        helper = loop_fixtures.TestCuLoop()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        cap = helper.capsule(['home', 'people', 'inbox', 'other'])
        original = cap.frame
        def frame(**kwargs):
            result = original(**kwargs)
            result['lane']['route'] = ('home', 'people', 'inbox', 'other')[cap._position]
            return result
        cap.frame = frame
        trace = io.StringIO()
        clock = Clock()
        seen_goals = []
        class Decider(ReplayDecider):
            def decide(self, **kwargs):
                seen_goals.append(kwargs['goal'])
                return super().decide(**kwargs)
        summary = run_loop(cap, Describer(), Decider([proposal()] * 3),
            goal='Open inbox. ' + render_predicate('route == "inbox"'), trace=trace,
            goal_check=lambda f: evaluate_predicate('route == "inbox"', extract_status(f)),
            clock=clock, sleep=clock.sleep)
        self.assertEqual(summary['stop_reason'], 'goal_reached')
        self.assertTrue(summary['goal_verified'])
        self.assertEqual(summary['goal_evidence'], 'verified')
        self.assertEqual(len(cap.inputs), 2)
        rows = [json.loads(line) for line in trace.getvalue().splitlines()]
        self.assertTrue(rows[0]['verify']['ok'])  # Generic state change alone is insufficient.
        self.assertEqual([(r['predicate_before'], r['predicate_after']) for r in rows],
                         [(False, False), (False, True)])
        self.assertFalse(rows[0]['predicate_after_unknown'])
        self.assertTrue(all('success means: (route is inbox)' in goal for goal in seen_goals))

    def test_unknown_does_not_verify_goal(self):
        helper = loop_fixtures.TestCuLoop()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        cap = helper.capsule(['a', 'b'])
        clock = Clock()
        trace = io.StringIO()
        result = run_loop(cap, Describer(), ReplayDecider([proposal()]), goal='test',
            trace=trace, max_steps=1, clock=clock, sleep=clock.sleep,
            goal_check=lambda f: evaluate_predicate('not route == "inbox"', extract_status(f)))
        self.assertEqual(result['stop_reason'], 'max_steps')
        self.assertFalse(result['goal_verified'])
        self.assertEqual(result['goal_evidence'], 'not_reached')
        row = json.loads(trace.getvalue())
        self.assertFalse(row['predicate_after'])
        self.assertTrue(row['predicate_after_unknown'])


class TestPredicateDistance(unittest.TestCase):
    def test_typed_distance_and_boolean_composition(self):
        from wender.learn.cu_tasks import predicate_distance as distance
        self.assertEqual(distance('tick >= 3', {'tick': 1}), 2)
        self.assertEqual(distance('tick > 3', {'tick': 1}), 3)
        self.assertEqual(distance('3 > tick', {'tick': 5}), 3)
        self.assertEqual(distance('tick <= 3', {'tick': 5}), 2)
        self.assertEqual(distance('tick == 3', {'tick': 5}), 2)
        self.assertEqual(distance('tick != 3', {'tick': 3}), 1)
        self.assertEqual(distance('not tick < 3', {'tick': 1}), 2)
        self.assertEqual(distance('not (tick >= 3 and revision >= 2)', {'tick': 3, 'revision': 4}), 1)
        self.assertEqual(distance('route == "home" and inbox_open == 0', {'route': 'inbox', 'inbox_open': 1}), 2)
        self.assertEqual(distance('route == "home" or inbox_open == 0', {'route': 'inbox', 'inbox_open': 0}), 0)
        self.assertIsNone(distance('route == "home" or inbox_open == 0', {'route': 'home'}))
        self.assertIsNone(distance('tick == 0', {'tick': False}))

    def test_task_family_hint_is_scoped(self):
        from wender.learn.cu_tasks import task_family_hint
        hint = task_family_hint('hb_tf_a-07-v1')
        self.assertLess(hint.index('Mark read'), hint.index('Acknowledge'))
        self.assertIn("opening a message is 'Mark read'", hint)
        self.assertIn('only allowed after Mark read', hint)
        self.assertIn('only do it when the goal asks to acknowledge/confirm/clear/handle', hint)
        self.assertIn('row only moves focus', hint)
        self.assertEqual(task_family_hint('unrelated'), '')
        self.assertEqual(task_family_hint(None), '')

    def test_snapshot_preserves_absence_and_projects_extras(self):
        from wender.learn.cu_tasks import observation_status
        status = observation_status({'lane': {'inbox_open': 0, 'unread_count': '0',
            'last_refusal_code': 'ILLEGAL_RULE', 'raw_html': 'excluded'}})
        self.assertEqual(status['inbox_open'], 0)
        self.assertEqual(status['unread_count'], 0)
        self.assertEqual(status['last_refusal_code'], 'ILLEGAL_RULE')
        self.assertIsNone(status['read_count'])
        self.assertIsNone(status['acked_count'])
        self.assertIsNone(status['focus'])
        self.assertNotIn('raw_html', status)
        self.assertIsNone(observation_status({})['unread_count'])
        inbox = observation_status({'lane': {'read_count': '2', 'acked_count': 1,
                                            'read': True, 'acked': False}})
        self.assertEqual(inbox['read_count'], 2)
        self.assertEqual(inbox['acked_count'], 1)
        self.assertIs(inbox['read'], True)
        self.assertIs(inbox['acked'], False)


    def test_rendered_refusal_is_in_observation(self):
        from wender.learn.cu_tasks import observation_status
        lane = {'state': {'nodes': [
            {'role': 'status', 'name': 'Last refusal: ILLEGAL_RULE', 'states': {}},
            {'role': 'status', 'name': 'Inbox', 'states': {'unread_count': '1'}}]}}
        status = observation_status({'lane': lane})
        self.assertEqual(status['last_refusal_code'], 'ILLEGAL_RULE')
        self.assertEqual(status['unread_count'], 1)
        self.assertIn('Last refusal: ILLEGAL_RULE', status['status_text'])
        self.assertIsNone(observation_status({'lane': {'state': None}})['inbox_open'])
        self.assertIsNone(observation_status(None)['last_refusal_code'])
