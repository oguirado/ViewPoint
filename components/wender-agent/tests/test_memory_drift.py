# @module {"id":"wa.tests.memory_drift","layer":2,"owner":"LEARN","deps":["wa.learn.memory","wa.learn.drift"],"book":"HB-WA-s4-learn"}
"""Acceptance predicates, rejected fixtures and positive controls for W6."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from wender.learn import memory as m, drift as d

MEMORY = Path(__file__).resolve().parents[1] / 'wender/learn/executor_memory/perplexity.json'


# @anchor:wa.tests.memory_drift.TestMemoryAndDrift
class TestMemoryAndDrift(unittest.TestCase):
    # @anchor:wa.tests.memory_drift.setUp
    def setUp(self):
        self.memory = m.load_memory(MEMORY)
        self.summary = dict(goal_verified=True, completed=['new_thread'], task='What is the capital of Australia Canberra?', harvested='Answer with arxiv+2')
        self.stored = frozenset(('link|sidebar', 'textbox|composer', 'button|main', 'heading|main'))

    def _record(self, memory=None, **kwargs):
        return m.record_reproduction(self.memory if memory is None else memory, self.summary, epoch=100, **kwargs)

    def _trusted(self):
        return self._record(self._record())

    # @anchor:wa.tests.memory_drift.test_fixture_workflow_promoted_to_trusted_with_fewer_than_2_verified_reproductions
    def test_fixture_workflow_promoted_to_trusted_with_fewer_than_2_verified_reproductions(self):
        # Predicate: trusted iff exact evidence satisfies the admission floor.
        for repro in (0, 1, True, 2.0, None):
            self.assertFalse(m.is_trusted(dict(verified_reproductions=repro, alignment_violations=0, last_verified_epoch=100)))
        draft = self._record()
        self.assertEqual(draft['status'], 'draft')
        for meta in (draft, draft['workflow']):
            meta['status'] = 'trusted'
        with self.assertRaises(m.MemoryRejected):
            m.validate_memory(draft)
        trusted = self._trusted()
        self.assertEqual(trusted['verified_reproductions'], 2)
        self.assertEqual(trusted['workflow']['status'], 'trusted')
        self.assertTrue(m.is_trusted(trusted))
        self.assertEqual(self.memory['verified_reproductions'], 0)

    # @anchor:wa.tests.memory_drift.test_fixture_workflow_promoted_when_alignment_violations_greater_than_zero
    def test_fixture_workflow_promoted_when_alignment_violations_greater_than_zero(self):
        trusted = self._trusted()
        self.assertTrue(m.is_trusted(trusted))
        vetoed = self._record(trusted, violations=1)
        self.assertFalse(m.is_trusted(vetoed))
        self.assertEqual(vetoed['status'], 'draft')
        self.assertEqual(vetoed['workflow']['status'], 'draft')
        vetoed['status'] = 'trusted'
        with self.assertRaises(m.MemoryRejected):
            m.validate_memory(vetoed)
        failed = m.record_reproduction(trusted, dict(goal_verified=False, alignment_violations=1))
        self.assertEqual(failed['status'], 'draft')
        self.assertEqual(trusted['status'], 'trusted')

    # @anchor:wa.tests.memory_drift.test_fixture_ui_drift_jaccard_below_050_continues_stale_macro_without_regrounding
    def test_fixture_ui_drift_jaccard_below_050_continues_stale_macro_without_regrounding(self):
        macros = {'new_thread': 'e0'}
        result = d.guard_macro_step(macros, self.stored, frozenset(('link|sidebar',)), intent='new_thread')
        self.assertEqual(result['verdict'], 'UI_DRIFT')
        self.assertFalse(result['allow_cached_macro'])
        self.assertIsNone(result['cached_element_id'])
        self.assertFalse(result['known'])
        self.assertEqual(result['macros'], {})
        self.assertEqual(result['receipt']['invalidated_macros'], 1)
        self.assertEqual(macros, {'new_thread': 'e0'})
        boundary = d.guard_macro_step(macros, self.stored, frozenset(('link|sidebar', 'textbox|composer')), intent='new_thread')
        self.assertEqual((boundary['jaccard_num'], boundary['jaccard_den']), (2, 4))
        self.assertTrue(boundary['allow_cached_macro'])
        self.assertEqual(boundary['cached_element_id'], 'e0')
        self.assertIsNone(boundary['receipt'])

    # @anchor:wa.tests.memory_drift.test_fixture_raw_task_query_or_harvested_answer_persisted_in_executor_memory_json
    def test_fixture_raw_task_query_or_harvested_answer_persisted_in_executor_memory_json(self):
        serialized = m.canonical_dump(self._record())
        for private in ('Canberra', 'arxiv+2', self.summary['task'], self.summary['harvested']):
            self.assertNotIn(private, serialized)
        for key in ('task', 'harvested', 'ax', 'cookies', 'prompt'):
            dirty = copy.deepcopy(self.memory)
            dirty[key] = 'private'
            with self.assertRaises(m.MemoryRejected):
                m.validate_memory(dirty)
        for lesson in ('Never print arxiv+2', 'Never print GPT-6.1 Sol Razonamiento', 'Never repeat Canberra'):
            dirty = copy.deepcopy(self.memory)
            dirty['lessons'] = [lesson]
            with self.assertRaises(m.MemoryRejected):
                m.validate_memory(dirty, forbidden_substrings=('Canberra',))
        dirty_summary = dict(self.summary, completed=['Canberra'])
        with self.assertRaises(m.MemoryRejected):
            m.record_reproduction(self.memory, dirty_summary)
        self.assertEqual(m.load_memory(MEMORY), self.memory)

    # @anchor:wa.tests.memory_drift.test_fixture_more_than_3_lessons_or_over_120_tokens_injected_into_planner_request
    def test_fixture_more_than_3_lessons_or_over_120_tokens_injected_into_planner_request(self):
        for lessons in (['Never repeat.'] * 4, ['Never ' + 'x' * 158] * 3):
            dirty = copy.deepcopy(self.memory)
            dirty['lessons'] = lessons
            with self.assertRaises(m.MemoryRejected):
                m.validate_lessons(lessons)
            with self.assertRaises(m.MemoryRejected):
                m.build_planner_memory_view(dirty)
        self.assertEqual(len(m.build_planner_memory_view(self.memory)['lessons']), 3)
        self.assertEqual(m.canonical_dump(self.memory), m.canonical_dump(m.load_memory(MEMORY)))
        self.assertEqual(m.build_planner_memory_view(self.memory), m.build_planner_memory_view(self._trusted()))
        self.assertEqual(m.validate_lessons(['Never ' + 'x' * 154] * 3), ['Never ' + 'x' * 154] * 3)

    # @anchor:wa.tests.memory_drift.test_rename_detener_to_alto_structural_bind_and_no_drift_fire
    def test_rename_detener_to_alto_structural_bind_and_no_drift_fire(self):
        # @anchor:wa.tests.memory_drift.element
        def element(name):
            return dict(role='button', region='main', name=name, actionable=True, box=[300, 20, 20, 20])
        control = dict(role='button', region='main', container_rule='last', pattern='^Detener')
        for name in ('Detener', 'Alto'):
            targets = {'e0': element(name)}
            self.assertEqual(m.resolve_control_candidates(control, [{'id': 'e0'}], targets), {'e0'})
            self.assertEqual(d.check_drift(['button|main'], list(targets.values()))['verdict'], 'OK')
        required = [{'role': token.split('|')[0], 'landmark': token.split('|')[1], 'required': token == 'textbox|composer'} for token in self.stored]
        missing = d.check_drift(required, self.stored - {'textbox|composer'})
        self.assertEqual((missing['jaccard_num'], missing['jaccard_den']), (3, 4))
        self.assertEqual(missing['verdict'], 'UI_DRIFT')
        self.assertEqual(missing['vanished'], ['textbox|composer'])

    # @anchor:wa.tests.memory_drift.test_empty_signatures_refused_and_20_cycle_tally_freezes
    def test_empty_signatures_refused_and_20_cycle_tally_freezes(self):
        for stored, current in (([], self.stored), (self.stored, frozenset())):
            result = d.guard_macro_step({'new_thread': 'e0'}, stored, current, intent='new_thread')
            self.assertEqual(result['verdict'], 'UNKNOWN')
            self.assertFalse(result['allow_cached_macro'])
            self.assertTrue(result['reground_required'])
            self.assertIsNone(result['receipt'])
        tally = d.DriftTally()
        self.assertIsNone(tally.record('perplexity', 2, 'ask', 'unknown_screen'))
        self.assertIsNone(tally.record('perplexity', 2, 'ask', 'unknown_screen'))
        self.assertTrue(tally.record('perplexity', 2, 'ask', 'unknown_screen')['freeze_promotion'])
        for votes in (5, 6):
            tally = d.DriftTally()
            for i in range(20):
                result = tally.record('perplexity', 2, 'ask', 'abstain' if i < votes else 'ok')
            if votes == 5:
                self.assertIsNone(result)
            else:
                self.assertEqual(result['drift_votes'], 6)
                self.assertTrue(result['freeze_promotion'])
            self.assertIsNone(tally.record('perplexity', 2, 'ask', 'ok'))

    # @anchor:wa.tests.memory_drift.test_loader_digest_version_horizon_and_exact_integer_gates
    def test_loader_digest_version_horizon_and_exact_integer_gates(self):
        for mutate in (
            lambda x: x['control_map']['busy'].update(region='sidebar'),
            lambda x: x.update(min_cycle_version=2),
            lambda x: x.update(horizon_runs=0),
            lambda x: x.update(schema_version=True),
            lambda x: x.update(schema_version=2.0),
            lambda x: x.update(verified_reproductions=1),
            lambda x: x['control_map']['busy'].update(bbox_norm=[True, 0, 1, 1]),
        ):
            bad = copy.deepcopy(self.memory)
            mutate(bad)
            with self.assertRaises(m.MemoryRejected):
                m.validate_memory(bad)
        future = copy.deepcopy(self.memory)
        future['min_cycle_version'] = 2
        self.assertEqual(m.validate_memory(future, cycle_version=2)['min_cycle_version'], 2)
        window = self._trusted()
        window['horizon_runs'] = 1
        with self.assertRaises(m.MemoryRejected):
            m.validate_memory(window)
        legacy = copy.deepcopy(self.memory)
        legacy['schema'] = 'wender.executor_memory/v1'
        for key in ('schema_version', 'min_cycle_version', 'control_map_digest'):
            legacy.pop(key)
        self.assertEqual(m.validate_memory(legacy)['schema'], 'wender.executor_memory/v1')
        for payload in ('{"schema":1,"schema":2}', '{"x":NaN}', MEMORY.read_text().replace('\n', '\r\n')):
            with tempfile.TemporaryDirectory(dir='/dev/shm') as tmp:
                path = Path(tmp) / 'memory.json'
                path.write_bytes(payload.encode())
                with self.assertRaises(m.MemoryRejected):
                    m.load_memory(path)
        with self.assertRaises(m.MemoryRejected):
            m.load_memory(MEMORY.parent / 'absent-memory.json')

    # @anchor:wa.tests.memory_drift.test_budget_and_lesson_privacy_boundaries
    def test_budget_and_lesson_privacy_boundaries(self):
        for lessons in (['Use this control'], ['Never a\nb'], ['Never https://private.invalid'], ['Never ' + 'x' * 400]):
            with self.assertRaises(m.MemoryRejected):
                m.validate_lessons(lessons)
        with self.assertRaises(m.MemoryRejected):
            m.validate_lessons(['Never repeat secret-fragment'], forbidden_substrings=('prefix secret-fragment suffix',))
        huge = copy.deepcopy(self.memory)
        huge['extra'] = 'x' * 8000
        with self.assertRaises(m.MemoryRejected):
            m.validate_memory(huge)
        self.assertLessEqual(len(m.canonical_dump(self.memory).encode()), 8000)
        self.assertEqual(m.estimate_tokens('Never'), 2)
        with self.assertRaises(m.MemoryRejected):
            m.estimate_tokens(True)

    # @anchor:wa.tests.memory_drift.test_signature_privacy_eligibility_and_invalid_tokens
    def test_signature_privacy_eligibility_and_invalid_tokens(self):
        base = dict(role='button', region='main', name='PRIVATE HISTORY', actionable=True, box=[300, 20, 30, 30])
        self.assertEqual(d.current_signature([base]), frozenset(('button|main',)))
        for change in (dict(visible=False), dict(actionable=False), dict(box=[0, 0, 0, 0]), dict(states={'enabled': False})):
            self.assertFalse(d.current_signature([dict(base, **change)]))
        self.assertFalse(d.current_signature([dict(role='button', region='main', name='private')]))
        self.assertIsNone(d.normalize_landmark({'role': 'button', 'region': 'unknown'}))
        self.assertEqual(d.normalize_landmark({'ancestors': [{'role': 'navigation'}]}), 'sidebar')
        self.assertEqual(d.normalize_landmark({'box': [20, 0, 20, 20]}), 'sidebar')
        self.assertEqual(d.normalize_landmark({'region': 'main', 'role': 'textbox'}), 'composer')
        for token in ('button|main|private', 'button|answer:private', 'invalid|main'):
            with self.assertRaises(ValueError):
                d.stored_signatures([token])
        self.assertEqual(d.stored_signatures(['link|navigation'])[0], frozenset(('link|sidebar',)))
        self.assertEqual(d.check_drift(['textbox|main'], [{'role': 'textbox', 'landmark': 'composer'}])['verdict'], 'OK')
        self.assertEqual(d.check_drift(['button|answer:last'], [{'role': 'button', 'landmark': 'answer:last'}])['verdict'], 'OK')

    # @anchor:wa.tests.memory_drift.test_structural_candidates_refuse_blocked_disabled_and_unproven_containers
    def test_structural_candidates_refuse_blocked_disabled_and_unproven_containers(self):
        control = dict(role='button', region='main', pattern='^Detener', container_rule='last')
        rows = {'e0': dict(role='button', region='main', name='Alto'),
                'e1': dict(role='button', region='main', name='Upgrade'),
                'e2': dict(role='button', region='main', name='Alto', states={'enabled': False})}
        options = [{'id': key} for key in rows]
        self.assertEqual(m.resolve_control_candidates(control, options, rows), {'e0'})
        self.assertEqual(m.resolve_control_candidates(dict(control, structural_fallback=False), options, rows), set())
        self.assertEqual(m.resolve_control_candidates(dict(control, container_rule='model submenu'), options, rows), set())
        self.assertEqual(m.resolve_control_candidates(control, [], rows), set())
        with self.assertRaises(m.MemoryRejected):
            m.validate_control_map({'bad': dict(control, name_match={'kind': 'regex', 'text': ''})})
        for rule, expected in (('first', {'e0'}), ('last', {'e3'}), ('any', {'e0', 'e3'})):
            two = {'e0': rows['e0'], 'e3': dict(rows['e0'])}
            self.assertEqual(m.resolve_control_candidates(dict(control, container_rule=rule), [{'id': k} for k in two], two), expected)
        geo = {'e0': dict(rows['e0'], bbox_norm=[0, 0, 1, 1]), 'e3': dict(rows['e0'], bbox_norm=[100, 100, 1, 1])}
        self.assertEqual(m.resolve_control_candidates(dict(control, container_rule='any', bbox_norm=[99, 99, 1, 1]), [{'id': k} for k in geo], geo), {'e3'})

    # @anchor:wa.tests.memory_drift.test_tally_merge_laws_atomicity_partitioning_and_nonconsecutive_controls
    def test_tally_merge_laws_atomicity_partitioning_and_nonconsecutive_controls(self):
        tallies = [d.DriftTally() for _ in range(3)]
        for i, tally in enumerate(tallies):
            for _ in range(12):
                tally.record('perplexity', 2, 'ask', 'abstain' if i else 'ok')
        a, b, c = tallies
        self.assertEqual(a.merge(b)._history, b.merge(a)._history)
        self.assertEqual(a.merge(b).merge(c)._history, a.merge(b.merge(c))._history)
        self.assertEqual(a.merge(d.DriftTally())._history, a._history)
        self.assertEqual(a.merge(a)._history, a._history)
        before = copy.deepcopy(a._history)
        with self.assertRaises(ValueError):
            a.record('perplexity', 2, 'ask', 'PRIVATE HISTORY')
        self.assertEqual(before, a._history)
        for outcome in ('unknown_screen', 'ok', 'unknown_screen', 'ok'):
            self.assertIsNone(a.record('perplexity', 2, 'separate', outcome))
        self.assertEqual(len(a._history[('perplexity', 2, 'ask')]), 12)
        macros = {'ask': {'element_id': 'e0'}}
        with self.assertRaises(ValueError):
            d.invalidate_macros(macros, dict(verdict='UI_DRIFT', jaccard_num=0, jaccard_den=1, vanished=['button|private']))
        self.assertEqual(macros, {'ask': {'element_id': 'e0'}})


    # @anchor:wa.tests.memory_drift.test_planted_gate_mutants_are_killed
    def test_planted_gate_mutants_are_killed(self):
        # Each production mutant must turn its paired acceptance fixture red.
        real_guard = d.guard_macro_step
        def stale_macro(*args, **kwargs):
            result = real_guard(*args, **kwargs)
            result.update(allow_cached_macro=True, cached_element_id='e0', known=True)
            return result
        mutants = (
            (m, 'is_trusted', lambda meta: True,
             'fixture_workflow_promoted_to_trusted_with_fewer_than_2_verified_reproductions'),
            (d, 'guard_macro_step', stale_macro,
             'fixture_ui_drift_jaccard_below_050_continues_stale_macro_without_regrounding'),
            (m, 'validate_lessons', lambda lessons, **kwargs: list(lessons),
             'fixture_more_than_3_lessons_or_over_120_tokens_injected_into_planner_request'),
        )
        for module, symbol, mutant, fixture in mutants:
            with self.subTest(mutant=symbol), patch.object(module, symbol, mutant):
                result = unittest.TestResult()
                TestMemoryAndDrift('test_' + fixture).run(result)
                self.assertTrue(result.failures, 'mutant escaped the acceptance predicate')
                self.assertFalse(result.errors, 'fixture crashed instead of killing mutant')


# Harness lookup aliases preserve standard unittest discovery names.
for _fixture in (
    'fixture_workflow_promoted_to_trusted_with_fewer_than_2_verified_reproductions',
    'fixture_workflow_promoted_when_alignment_violations_greater_than_zero',
    'fixture_ui_drift_jaccard_below_050_continues_stale_macro_without_regrounding',
    'fixture_raw_task_query_or_harvested_answer_persisted_in_executor_memory_json',
    'fixture_more_than_3_lessons_or_over_120_tokens_injected_into_planner_request',
):
    setattr(TestMemoryAndDrift, _fixture, getattr(TestMemoryAndDrift, 'test_' + _fixture))
