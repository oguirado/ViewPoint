# @module {"id":"wa.tests.cu_loop_progress","layer":2,"owner":"LEARN","deps":["wa.learn.cu_loop","wa.learn.cu_memory","wa.learn.dream_journal","wa.learn.model_client","wa.tests.cu_web_unit","wa.tests.test_cu_loop_unit","wa.tests.test_cu_repair_unit"],"book":"HB-WA-s4-learn"}
"""W7 predicates, deliberately invalid fixtures, and positive controls; RAM-only."""
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from tests import test_cu_loop_unit as fixtures
from tests import test_cu_repair_unit as repair
from tests import test_cu_web_unit as web
from wender.learn import cu_memory as mem
from wender.learn import dream_journal as dream
from wender.learn.cu_loop import (ModelDecider, ReplayDecider, _verify, run_loop, run_web_cycle,
                                  _enrich_retry_feedback, _compact_state_delta)
from wender.learn.model_client import _prompt


def _form(length=0, *, enabled=True, focused=True, busy=False):
    rows = [dict(id='input', role='textbox', value_len=length, focused=focused),
            dict(id='btn-submit', role='button', name='Submit', states={'enabled': enabled})]
    if busy:
        rows.append(dict(id='stop', role='button', name='Detener', states={'busy': True}))
    return dict(lane={'elements': rows, 'state_sha256': ('a' if length == 0 else 'b') * 64},
                sha256_rgb24='a' * 64)


def _receipt(step, **kw):
    return mem.Receipt(step, 'click', 'click:element:button:-', 'sealed', 'value_delta',
                       'a' * 16, ok=True, **kw)


# @anchor:wa.tests.cu_loop_progress.ProgressTests
class ProgressTests(unittest.TestCase):
    # @anchor:wa.tests.cu_loop_progress.ProgressTests.setUp
    def setUp(self):
        self.fixture = fixtures.TestCuLoop()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.clock = self.fixture.clock

    def _run(self, cap, actions, **kwargs):
        # @anchor:wa.tests.cu_loop_progress.ProgressTests.Description
        class Description(fixtures.Describer):
            # @anchor:wa.tests.cu_loop_progress.ProgressTests.Description.describe
            def describe(self, frame, *, prev=None):
                return dict(super().describe(frame, prev=prev), schema='ve.cu_describe/v1',
                            width=frame['width'], height=frame['height'], crops=[])
        trace = io.StringIO()
        summary = run_loop(cap, Description(), ReplayDecider(actions), goal='test', trace=trace,
                           clock=self.clock, sleep=self.clock.sleep, **kwargs)
        return summary, [json.loads(line) for line in trace.getvalue().splitlines()]

    def _form_cap(self, lengths):
        cap = self.fixture.capsule([str(i) for i in range(len(lengths))])
        original = cap.frame
        # @anchor:wa.tests.cu_loop_progress.ProgressTests.frame
        def frame(**kwargs):
            result = original(**kwargs)
            result['lane']['elements'] = _form(lengths[cap._position])['lane']['elements']
            return result
        cap.frame = frame
        return cap

    # @anchor:wa.tests.cu_loop_progress.ProgressTests.test_fixture_type_action_accepted_as_verified_when_focused_and_value_len_unchanged
    def test_fixture_type_action_accepted_as_verified_when_focused_and_value_len_unchanged(self):
        action = {'kind': 'type', 'text': 'Canberra'}
        before, after = _form(), _form()
        after['lane']['state_sha256'] = 'b' * 64
        self.assertEqual(_verify(before, after, {}, {'kind': 'state_change'}, action=action)['method'], 'value_delta')
        self.assertFalse(_verify(before, after, {}, {'kind': 'state_change'}, action=action)['ok'])
        self.assertTrue(_verify(before, _form(8), {}, {'kind': 'state_change'}, action=action)['ok'])
        for length, expected in ((0, 0), (8, 1)):
            cap = self._form_cap([0, length])
            summary, rows = self._run(cap, [fixtures.proposal('type', text='Canberra')], max_steps=1)
            self.assertEqual(summary['verified_ok'], expected)
            self.assertEqual(summary['failures'], 1 - expected)
            self.assertNotIn('Canberra', json.dumps(rows))

    # @anchor:wa.tests.cu_loop_progress.ProgressTests.test_fixture_submit_click_accepted_when_button_stays_enabled_and_input_not_cleared
    def test_fixture_submit_click_accepted_when_button_stays_enabled_and_input_not_cleared(self):
        action = dict(kind='click', element_id='btn-submit', x=5, y=5)
        before, after = _form(12), _form(12)
        after['lane']['state_sha256'] = 'c' * 64
        result = _verify(before, after, {}, {'kind': 'state_change'}, action=action)
        self.assertEqual(result['method'], 'submit_seen')
        self.assertFalse(result['ok'])
        for positive in (_form(0), _form(12, enabled=False), _form(12, busy=True)):
            self.assertTrue(_verify(before, positive, {}, {'kind': 'state_change'}, action=action)['ok'])
        self.assertFalse(mem.verify_action_postcondition({'kind': 'key', 'keysym': 'Return'}, before, after)['ok'])
        self.assertTrue(mem.verify_action_postcondition({'kind': 'key', 'keysym': 'Return'}, before, _form(0))['ok'])

    # @anchor:wa.tests.cu_loop_progress.ProgressTests.test_fixture_repeated_identical_action_target_state_hash_pair_executes_third_time
    def test_fixture_repeated_identical_action_target_state_hash_pair_executes_third_time(self):
        action = dict(kind='click', element_id='input')
        tracker = mem.NoProgressTracker()
        for _ in range(2):
            self.assertEqual(tracker.can_execute(action, 'a'), (True, None))
            tracker.record_step(action, 'a', 'a', outcome='wrong_target_noop')
        self.assertEqual(tracker.can_execute(action, 'a'), (False, 'repeated_action_target_state_pair'))
        self.assertEqual(tracker.can_execute(dict(action, element_id='other'), 'a'), (True, None))
        self.assertEqual(tracker.can_execute(action, 'b'), (True, None))
        cap = self._form_cap([0] * 4)
        original = cap.frame
        # @anchor:wa.tests.cu_loop_progress.ProgressTests.constant
        def constant(**kwargs):
            result = original(**kwargs)
            result['lane'].update(tick=0, revision=0)
            return result
        cap.frame = constant
        summary, rows = self._run(cap, [fixtures.proposal('type', text='Canberra')] * 3, max_steps=3)
        self.assertEqual(len(cap.inputs), 2)
        self.assertEqual(summary['stop_reason'], 'no_progress')
        self.assertIn('repeated_action_target_state_pair', summary['escalate_reason'])
        # Positive control: changed semantic state permits a third dispatch.
        cap = self._form_cap([0] * 4)
        summary, rows = self._run(cap, [fixtures.proposal('type', text='Canberra')] * 3, max_steps=3)
        self.assertEqual(len(cap.inputs), 3)
        self.assertEqual(summary['stop_reason'], 'verify_failures')

    # @anchor:wa.tests.cu_loop_progress.ProgressTests.test_fixture_two_consecutive_no_progress_steps_fail_to_trigger_escalate_reason
    def test_fixture_two_consecutive_no_progress_steps_fail_to_trigger_escalate_reason(self):
        summary, _ = self._run(self._form_cap([0] * 3), [fixtures.proposal('type', text='x')] * 2, max_steps=2)
        self.assertEqual(summary['escalate_reason'], 'two_consecutive_no_progress')
        self.assertGreaterEqual(len(summary['escalations']), 1)
        cap = self._form_cap([0, 1, 2])
        summary, _ = self._run(cap, [fixtures.proposal('type', text='x')] * 2, max_steps=2,
                              goal_distance=lambda frame: 2 - frame['lane']['tick'],
                              goal_check=lambda frame: frame['lane']['tick'] == 2)
        self.assertEqual(summary['escalations'], [])
        self.assertIsNone(summary['escalate_reason'])
        self.assertEqual(summary['stop_reason'], 'goal_reached')

    # @anchor:wa.tests.cu_loop_progress.ProgressTests.test_fixture_dream_journal_merkle_prev_sha256_tampered_without_verification_error
    def test_fixture_dream_journal_merkle_prev_sha256_tampered_without_verification_error(self):
        path = self.root / 'dream.jsonl'
        writer = dream.JournalWriter(path)
        for i in range(4):
            writer.append(dict(pre_hash=str(i), action_sig='click:element:button:-', post_hash=str(i + 1), score_q16=i))
        self.assertEqual(dream.verify_chain(path), (0, 4))
        self.assertEqual(dream.verify_journal(path), (0, 4))
        data = path.read_bytes()
        records = [json.loads(line) for line in data.splitlines()]
        records[2]['prev_sha256'] = 'f' * 64
        path.write_text(''.join(json.dumps(record) + '\n' for record in records))
        with self.assertRaises(dream.JournalVerificationError) as raised:
            dream.verify_chain(path)
        self.assertEqual((raised.exception.rc, raised.exception.slot), (71, 2))
        self.assertEqual(dream.verify_journal(path), (71, 2))
        path.write_bytes(data[:-1])
        self.assertEqual(dream.verify_journal(path), (65, 3))
        path.write_bytes(b'')
        self.assertEqual(dream.verify_journal(path), (64, 0))
        path.write_bytes(data)
        Path(str(path) + '.head').write_text('f' * 64)
        self.assertEqual(dream.verify_journal(path), (71, 4))
        Path(str(path) + '.head').unlink()
        self.assertEqual(dream.verify_journal(path), (71, 4))

    # @anchor:wa.tests.cu_loop_progress.ProgressTests.test_working_memory_ring_eviction_and_consumer_split
    def test_working_memory_ring_eviction_and_consumer_split(self):
        ring = mem.WorkingMemoryRing()
        for step in range(1, 10):
            ring.append(_receipt(step, option_id=f'id-{step}'))
        self.assertEqual([r['step'] for r in ring.for_muse()], list(range(2, 10)))
        self.assertEqual([r['id'] for r in ring.for_local()], ['id-7', 'id-8', 'id-9'])
        self.assertEqual([r['intent'] for r in ring.for_local()], ['click'] * 3)
        self.assertLessEqual(len(json.dumps(ring.for_muse())) // 4, 1200)
        ring.clear()
        for step in range(1, 4):
            ring.append(_receipt(step))
        self.assertEqual(len(ring.for_muse()), 3)
        self.assertEqual(len(ring.for_local()), 3)
        self.assertEqual(ring.last(0), [])
        sig = mem.canonical_action_sig(dict(kind='type', text='PRIVATE HISTORY TITLE'))
        self.assertNotIn('PRIVATE HISTORY TITLE', sig)
        self.assertIn('len:21', sig)
        ring.append(_receipt(4, refusal_code='ILLEGAL_RULE'))
        self.assertEqual(sum(ring.refused.values()), 1)
        ring.clear()
        self.assertEqual(list(ring.visited_state_hashes), [])
        self.assertEqual(dict(ring.refused), {})

    # @anchor:wa.tests.cu_loop_progress.ProgressTests.test_four_way_fsm_already_done_noop_does_not_increment_consecutive_failures
    def test_four_way_fsm_already_done_noop_does_not_increment_consecutive_failures(self):
        for expected, values in (
            ('already_done_noop', dict(changed=False, verify_ok=False, snapshot_revisited=True, predicate_before=True, predicate_after=True)),
            ('wrong_target_noop', dict(changed=False, verify_ok=False, snapshot_revisited=True, predicate_before=False, predicate_after=False)),
            ('stagnant_verified', dict(changed=True, verify_ok=True, snapshot_revisited=True, predicate_before=False, predicate_after=False)),
            ('sealed', dict(changed=True, verify_ok=True, snapshot_revisited=False, predicate_before=False, predicate_after=False))):
            self.assertEqual(mem.classify_step_outcome(**values), expected)
        cap = self._form_cap([0] * 4)
        actions = [fixtures.proposal('type', text='x')] * 3 + [fixtures.proposal('done')]
        summary, rows = self._run(cap, actions, goal_check=lambda frame: True)
        self.assertEqual(summary['failures'], 0)
        self.assertEqual(summary['stop_reason'], 'done')
        self.assertEqual(summary['escalations'], [])
        cap = self._form_cap([0] * 4)
        summary, _ = self._run(cap, actions, goal_check=lambda frame: False)
        self.assertEqual(summary['stop_reason'], 'verify_failures')
        self.assertEqual(summary['failures'], 3)

    # @anchor:wa.tests.cu_loop_progress.ProgressTests.test_retry_feedback_enrichment_preserves_request_equality_and_prompt_sentence
    def test_retry_feedback_enrichment_preserves_request_equality_and_prompt_sentence(self):
        case = repair.TestRepair()
        case.setUp()
        self.addCleanup(case.doCleanups)
        bad = fixtures.proposal('click', x=900, y=0)
        result = case.run_actions([bad, fixtures.proposal()])
        self.assertEqual(result['verified_ok'], 1)
        first, second = case.requests
        self.assertEqual({k: v for k, v in second.items() if k != 'feedback'}, first)
        feedback = second['feedback']
        self.assertEqual(feedback['blocked_targets'], [dict(target='coord:900,0', reason='ActionValidationError')])
        self.assertIn('Your previous action was rejected: coordinates outside frame. Return one corrected action.', _prompt(second))
        self.assertIsInstance(feedback['state_delta']['regions_n'], int)
        for rejected in (None, 'not-json{{{', '[]', '5'):
            minimal = {'validator_message': 'retry'}
            if rejected is not None:
                minimal['rejected_action'] = rejected
            row = _enrich_retry_feedback(minimal, {}, {}, [])
            self.assertEqual(row['blocked_targets'], [])
            self.assertIn('state_delta', row)
            self.assertEqual(row['validator_message'], 'retry')
        row = _enrich_retry_feedback(dict(validator_message='bad mark', error_class='ActionValidationError',
                                         rejected_action=json.dumps({'kind': 'click', 'mark_id': 99})), {}, {}, [])
        self.assertEqual(row['blocked_targets'], [dict(target='mark:99', reason='ActionValidationError')])

    # @anchor:wa.tests.cu_loop_progress.ProgressTests.test_feedback_bounds_overlap_and_privacy
    def test_feedback_bounds_overlap_and_privacy(self):
        frame = _form()
        frame['lane'].update(tick=2, last_refusal_code='ILLEGAL_RULE')
        frame['lane']['elements'][0]['bbox'] = dict(x=0, y=0, w=4, h=4)
        desc = dict(changed=True, changed_regions=[dict(x=1, y=1, w=2, h=2)])
        delta = _compact_state_delta(frame, desc, [{'status': {'tick': 1}}])
        self.assertEqual(delta['overlaps'], ['input'])
        self.assertEqual(delta['status_diff']['tick'], [1, 2])
        self.assertNotIn('focus', delta['status_diff'])
        row = _enrich_retry_feedback(dict(validator_message='retry', rejected_action=json.dumps(dict(kind='type', text='PRIVATE HISTORY TITLE')), extra='x' * 6000), frame, desc, [])
        self.assertLessEqual(len(json.dumps(row)), 2000)
        self.assertLessEqual(len(json.dumps(row['state_delta'])), 600)
        self.assertLessEqual(len(json.dumps(row['blocked_targets'])), 400)
        self.assertNotIn('PRIVATE HISTORY TITLE', json.dumps(row))
        row = _enrich_retry_feedback(dict(validator_message='retry', rejected_action=json.dumps(dict(kind='click', element_id='https://private.test/secret'))), {}, {}, [])
        self.assertNotIn('https://', json.dumps(row))

    # @anchor:wa.tests.cu_loop_progress.ProgressTests.test_fnv_vectors_focus_independence_and_missingness
    def test_fnv_vectors_focus_independence_and_missingness(self):
        self.assertEqual(mem.fnv1a_64_hex(''), 'cbf29ce484222325')
        self.assertEqual(mem.fnv1a_64_hex(b'a'), 'af63dc4c8601ec8c')
        self.assertEqual(mem.fnv1a_64_hex('a', width=12), 'af63dc4c8601')
        before, after = _form(), _form(focused=False)
        self.assertEqual(mem.canonical_snapshot_bytes({'focus': 'a'}, before['lane']['elements']),
                         mem.canonical_snapshot_bytes({'focus': 'b'}, after['lane']['elements']))
        self.assertNotEqual(mem.canonical_snapshot_bytes({}, before['lane']['elements']),
                            mem.canonical_snapshot_bytes({}, _form(1)['lane']['elements']))
        unknown = _form()
        del unknown['lane']['elements'][0]['value_len']
        self.assertNotEqual(mem.canonical_snapshot_bytes({}, before['lane']['elements']),
                            mem.canonical_snapshot_bytes({}, unknown['lane']['elements']))
        with self.assertRaises(ValueError):
            mem.fnv1a_64_hex('a', width=0)

    # @anchor:wa.tests.cu_loop_progress.ProgressTests.test_explicit_focus_scroll_and_missing_value_evidence
    def test_explicit_focus_scroll_and_missing_value_evidence(self):
        action = dict(kind='click', element_id='input')
        expectation = dict(kind='focus_acquired', anchor='input')
        self.assertTrue(_verify(_form(focused=False), _form(), {}, expectation, action=action)['ok'])
        self.assertFalse(_verify(_form(), _form(focused=False), {}, expectation, action=action)['ok'])
        # Focus on another textbox is not evidence for the requested target.
        other = _form(focused=False)
        other['lane']['elements'].append(dict(id='other', role='textbox', focused=True))
        self.assertFalse(_verify(_form(), other, {}, expectation, action=action)['ok'])
        for scroll, ok in ((1, True), (0, False), (121, False), (None, False)):
            self.assertEqual(_verify({'scroll': 0}, {'scroll': scroll}, {}, {'kind': 'scroll_moved'})['ok'], ok)
        self.assertFalse(_verify({}, {}, {}, {'kind': 'value_delta'}, action={'kind': 'type'})['ok'])

    # @anchor:wa.tests.cu_loop_progress.ProgressTests.test_loop_score_tracker_refusal_and_block_filter
    def test_loop_score_tracker_refusal_and_block_filter(self):
        t = ('click', 'click:element:input:-', 'a')
        self.assertEqual(mem.compute_loop_score([t, t], t), 2)
        self.assertEqual(mem.compute_loop_score([t], ('click', 'different', 'a')), 0)
        self.assertEqual(mem.compute_loop_score([], repeated_refusal=True), 2)
        tracker = mem.NoProgressTracker()
        for i in range(2):
            row = tracker.record_step({'kind': 'click'}, str(i), str(i + 1), outcome='wrong_target_noop', refusal_code='ILLEGAL_RULE')
        self.assertEqual(row['escalate_reason'], 'repeated_refusal')
        tracker.record_step({'kind': 'click'}, 'new', 'next', outcome='sealed')
        self.assertEqual(tracker.no_progress_streak, 0)
        block = mem.BlockedTargetFilter()
        self.assertEqual(block.check('Upgrade plan'), (False, 'blocked_control'))
        self.assertEqual(block.check('planet'), (True, None))
        self.assertEqual(block.counts()['blocked_control_present'], 1)

    # @anchor:wa.tests.cu_loop_progress.ProgressTests.test_journal_support_resume_aliases_and_framing
    def test_journal_support_resume_aliases_and_framing(self):
        path = self.root / 'support.jsonl'
        writer = dream.JournalWriter(path)
        writer.append(dict(pre_hash='a', action_sig='x', post_hash='b', score_q16=9831))
        writer = dream.JournalWriter(path)
        writer.append(dict(pre_hash='b', action_sig='y', post_hash='c', score_q16=0))
        self.assertEqual(dream.verify_chain(path), (0, 2))
        support = dream.build_support(path)
        self.assertEqual(support['table'][('a', 'x')], ('b', 0, 9831))
        self.assertEqual(support['table'][('c', '0xFFFFFFFF')][1], 70)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        bad = copy.deepcopy(records)
        bad[1]['sha256'] = 'f' * 64
        self.assertEqual(dream.verify_journal(bad), (71, 1))
        bad = copy.deepcopy(records)
        bad[1]['payload']['score_q16'] += 1
        self.assertEqual(dream.verify_journal(bad), (71, 1))
        self.assertEqual(dream.build_support(bad)['table'], {})
        original = path.read_bytes()
        for raw in (original.replace(b'\n', b'\r\n'), b'{"x":1,"x":2}\n', b'{"x":NaN}\n', b'x' * 4097 + b'\n'):
            path.write_bytes(raw)
            self.assertEqual(dream.verify_journal(path)[0], 65)
        path.write_bytes(original.splitlines(keepends=True)[0])
        self.assertEqual(dream.verify_journal(path), (71, 1))
        with self.assertRaises(ValueError):
            dream.JournalWriter('/tmp/forbidden-dream.jsonl')
        with self.assertRaises(ValueError):
            dream.JournalWriter(path, anchor_path='/tmp/forbidden-head')
        self.assertEqual(dream.ns('é'), b'2:\xc3\xa9')

    # @anchor:wa.tests.cu_loop_progress.ProgressTests.test_journal_limits_conflicts_and_integer_replay
    def test_journal_limits_conflicts_and_integer_replay(self):
        path = self.root / 'limits.jsonl'
        writer = dream.JournalWriter(path)
        with self.assertRaises(ValueError):
            writer.append({str(i): i for i in range(33)})
        with self.assertRaises(ValueError):
            writer.append({'blob': 'x' * 4096})
        deep = {}
        for _ in range(33):
            deep = {'nested': deep}
        with self.assertRaises(ValueError):
            writer.append(deep)
        for i in range(64):
            writer.append(dict(pre_hash=str(i), action_sig='click', post_hash=str(i + 1), score_q16=i))
        self.assertEqual(dream.verify_journal(path), (0, 64))
        with self.assertRaises(ValueError):
            writer.append({'step': 64})
        path = self.root / 'conflict.jsonl'
        writer = dream.JournalWriter(path)
        writer.append(dict(pre_hash='a', action_sig='x', post_hash='b', score_q16=1))
        writer.append(dict(pre_hash='a', action_sig='x', post_hash='c', score_q16=2))
        self.assertEqual(dream.build_support(path)['rc'], 65)
        path = self.root / 'float.jsonl'
        dream.JournalWriter(path).append(dict(pre_hash='a', action_sig='x', post_hash='b', score_q16=.5))
        self.assertEqual(dream.build_support(path)['rc'], 65)

    # @anchor:wa.tests.cu_loop_progress.ProgressTests.test_loop_journal_records_only_structural_evidence
    def test_loop_journal_records_only_structural_evidence(self):
        cap = self._form_cap([0, 8])
        path = self.root / 'loop.jsonl'
        summary, rows = self._run(cap, [fixtures.proposal('type', text='Canberra')], max_steps=1, journal_path=path)
        self.assertEqual(summary['verified_ok'], 1)
        self.assertEqual(dream.verify_journal(path), (0, 1))
        self.assertEqual(dream.build_support(path)['rc'], 0)
        self.assertNotIn('Canberra', path.read_text())
        self.assertNotIn('Canberra', json.dumps(rows))

    # @anchor:wa.tests.cu_loop_progress.ProgressTests.test_configured_escalation_hands_compact_memory_to_recovery_decider
    def test_configured_escalation_hands_compact_memory_to_recovery_decider(self):
        def capsule(final_len=1):
            cap = self._form_cap([0, 0, 0, final_len])
            original = cap.frame
            def frame(**kwargs):
                result = original(**kwargs)
                result['lane'].update(tick=0, revision=0)
                return result
            cap.frame = frame
            return cap
        actions = [fixtures.proposal('key', keysym='A')] * 3
        summary, _ = self._run(capsule(), actions, max_steps=3)
        self.assertEqual(summary['stop_reason'], 'no_progress')
        self.assertEqual(summary['steps'], 2)
        requests = []
        # @anchor:wa.tests.cu_loop_progress.ProgressTests.RecoveryBackend
        class RecoveryBackend:
            # @anchor:wa.tests.cu_loop_progress.ProgressTests.RecoveryBackend.generate
            def generate(self, request):
                requests.append(copy.deepcopy(request))
                return fixtures.proposal('type', text='x')
        cap = capsule()
        recovery = ModelDecider(RecoveryBackend())
        self.addCleanup(recovery.close)
        summary, _ = self._run(cap, actions, max_steps=3, escalate_decider=recovery,
                              goal_check=lambda frame: frame['lane']['elements'][0]['value_len'] == 1)
        self.assertEqual(summary['stop_reason'], 'goal_reached')
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(cap.inputs), 3)
        self.assertEqual([row['schema'] for row in requests[0]['trace_steps']], ['cu.receipt/v1'] * 2)
        self.assertFalse(recovery.crops_dir.exists())
        summary, _ = self._run(capsule(final_len=0), actions, max_steps=4,
                              escalate_decider=ReplayDecider([fixtures.proposal('key', keysym='B')]))
        self.assertEqual(summary['stop_reason'], 'no_progress')
        self.assertEqual(summary['steps'], 3)

    # @anchor:wa.tests.cu_loop_progress.ProgressTests.test_web_memory_hooks_lessons_and_block_counts
    def test_web_memory_hooks_lessons_and_block_counts(self):
        # @anchor:wa.tests.cu_loop_progress.ProgressTests.Hooks
        class Hooks:
            def __init__(self):
                self.events = []
            # @anchor:wa.tests.cu_loop_progress.ProgressTests.Hooks.on_plan
            def on_plan(self, memory):
                self.events.append('plan')
            # @anchor:wa.tests.cu_loop_progress.ProgressTests.Hooks.on_escalation
            def on_escalation(self, reason, intent):
                self.events.append(reason)
            # @anchor:wa.tests.cu_loop_progress.ProgressTests.Hooks.summary
            def summary(self):
                return {'events': self.events}
        # @anchor:wa.tests.cu_loop_progress.ProgressTests.Planner
        class Planner(web.FakePlanner):
            # @anchor:wa.tests.cu_loop_progress.ProgressTests.Planner.choose
            def choose(self, intent, options, history, lessons=None):
                self.received_lessons = lessons
                return super().choose(intent, options, history)
        # @anchor:wa.tests.cu_loop_progress.ProgressTests.Surface
        class Surface(web.FakeSurface):
            # @anchor:wa.tests.cu_loop_progress.ProgressTests.Surface.ax
            def ax(self):
                result = super().ax()
                result['elements'].append(web.element('Upgrade plan'))
                return result
        hooks, planner, clock = Hooks(), Planner(), web.Clock()
        memory = json.loads(web.MEMORY.read_text())
        memory['lessons'] = [{'rule': 'visible_control'}]
        result = run_web_cycle(Surface(), planner, web.FakeExecutor(abstain=True), memory,
                               task='Canberra', trace=io.StringIO(), clock=clock, sleep=clock.sleep, memory_hooks=hooks)
        self.assertTrue(result['goal_verified'], result)
        self.assertEqual(planner.received_lessons, memory['lessons'])
        self.assertEqual(hooks.events[0], 'plan')
        self.assertIn('abstain', hooks.events)
        self.assertGreater(result['blocked_controls']['blocked_control_present'], 0)
        self.assertEqual(result['drift'], hooks.summary())
        bad_hooks = Hooks()
        result = run_web_cycle(web.FakeSurface(stuck=True), web.FakePlanner(), web.FakeExecutor(), memory,
                               task='Canberra', trace=io.StringIO(), clock=clock, sleep=clock.sleep, memory_hooks=bad_hooks)
        self.assertFalse(result['goal_verified'])
        self.assertNotIn('postcheck_failed_twice', bad_hooks.events)
        self.assertEqual(bad_hooks.events, ['plan'])
