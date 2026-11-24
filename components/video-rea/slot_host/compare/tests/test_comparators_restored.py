# SPDX-License-Identifier: AGPL-3.0-only
"""Restored round-3 regressions consolidated by the round-4 matrix.

Each test below guarded behaviour that still holds after the round-4
rewrite and has no overlapping matrix row. Tests whose expectation the
frozen matrix supersedes are deliberately not restored.
"""

import unittest
from dataclasses import replace

from slot_host.compare import behaviour, timeline, tools
from slot_host.compare._evaluate import evaluate
from slot_host.compare._model import Timeline, TimelinePolicy
from slot_host.compare._timeline import compare_timeline
from .test_comparators import Host, VisualFixture, comparison, digest, event, event_trace, timeline_side


def _record(events, clock='pts'):
    return timeline_side(events, clock=clock).record


_BINDING = {'clock_source': 'pts', 'time_base_numerator': 1,
            'time_base_denominator': 2000, 'origin_tick': 0}


class RestoredTimelineTests(unittest.TestCase):
    def test_monotonic_match_with_verified_anchor(self):
        binding = {'clock_source': 'monotonic', 'time_base_numerator': 1,
                   'time_base_denominator': 2000, 'origin_tick': 7}
        reference = event_trace()
        candidate = [dict(row, event_id=f"c-{row['event_id']}") for row in reference]
        report = timeline.compare(
            _record(reference, clock='monotonic'),
            _record(candidate, clock='monotonic'),
            clock_binding=dict(binding), late_tolerance_ms=33)
        self.assertEqual(report['result'], 'match')
        self.assertEqual(report['checks'][0]['outcome'], 'match')

    def test_wall_binding_and_sides_yield_clock_source_unsuitable(self):
        reference = _record(event_trace())
        candidate = _record(event_trace())
        wall_binding = dict(_BINDING, clock_source='wall')
        report = timeline.compare(reference, candidate, clock_binding=wall_binding)
        self.assertEqual(report['abstain_reason'], 'clock_source_unsuitable')
        wall_side = _record(event_trace(), clock='wall')
        report = timeline.compare(wall_side, candidate, clock_binding=dict(_BINDING))
        self.assertEqual(report['abstain_reason'], 'clock_source_unsuitable')

    def test_missing_side_clock_or_origin_yields_missing_clock_binding(self):
        reference = _record(event_trace())
        candidate = _record(event_trace(), clock='monotonic')
        missing_clock = dict(candidate)
        del missing_clock['clock_source']
        report = timeline.compare(reference, missing_clock, clock_binding=dict(_BINDING))
        self.assertEqual(report['abstain_reason'], 'missing_clock_binding')
        bad_origin = dict(candidate)
        bad_origin['origin'] = 'somewhere_else'
        report = timeline.compare(reference, bad_origin, clock_binding=dict(_BINDING))
        self.assertEqual(report['abstain_reason'], 'missing_clock_binding')

    def test_out_of_scope_kind_remains_visible_in_diagnostics(self):
        reference = _record(event_trace())
        candidate = _record(event_trace())
        policy = TimelinePolicy(kinds=frozenset({'key_down', 'pointer_click'}),
                                required_kinds=frozenset({'key_down', 'pointer_click'}),
                                late_tolerance_ms=33, integer_milliseconds=False)
        anchor = 'pts:1/2000:0'
        ref = Timeline(record=reference, clock_verified=True, complete=True,
                       ordinal_prefix_verified=True, clock_domain=anchor, common_anchor=anchor)
        cand = Timeline(record=candidate, clock_verified=True, complete=True,
                        ordinal_prefix_verified=True, clock_domain=anchor, common_anchor=anchor)
        finding = compare_timeline(ref, cand, policy)
        self.assertEqual(finding.outcome, 'match')
        unscoped = finding.diagnostics['unscoped_events']
        self.assertEqual(sorted(row['kind'] for row in unscoped), ['key_up', 'key_up'])
        self.assertEqual({row['side'] for row in unscoped}, {'candidate', 'reference'})
        wrapper = timeline.compare(reference, candidate, clock_binding=dict(_BINDING))
        self.assertIn('events', wrapper)
        self.assertIn('coverage', wrapper)
        self.assertEqual(wrapper['events'], wrapper['deltas'])

    def test_recorded_array_order_reaches_comparator(self):
        import slot_host.compare._timeline as timeline_mod
        reference_events = [
            event('z1', 20, 'zebra', b'z-input'),
            event('a1', 40, 'apple', b'a-input'),
        ]
        candidate_events = [dict(row, event_id=f"c-{row['event_id']}") for row in reference_events]
        captured = {}
        real = timeline_mod.compare_replay_events

        def spy(ref_trace, cand_trace, *, late_tolerance_us):
            captured['ref'] = list(ref_trace)
            captured['cand'] = list(cand_trace)
            return real(ref_trace, cand_trace, late_tolerance_us=late_tolerance_us)

        timeline_mod.compare_replay_events = spy
        try:
            report = timeline.compare(
                _record(reference_events), _record(candidate_events),
                clock_binding=dict(_BINDING))
        finally:
            timeline_mod.compare_replay_events = real
        self.assertEqual(report['result'], 'match')
        ref_times = [row['time_us'] for row in captured['ref']]
        self.assertEqual(ref_times, sorted(ref_times),
                         'recorded order must survive')

    def test_tampered_empty_timeline_yields_timeline_invalid_not_empty_reference(self):
        tampered = _record([])
        tampered['timeline_sha256'] = digest(b'tampered-empty-events')
        anchor = 'pts:1/2000:0'
        bad = Timeline(record=tampered, clock_verified=True, complete=True,
                       ordinal_prefix_verified=True, clock_domain=anchor, common_anchor=anchor)
        good = timeline_side(event_trace())
        comp = comparison(('timeline',))
        comp = replace(comp, reference=replace(comp.reference, timeline=bad),
                       candidate=replace(comp.candidate, timeline=good))
        result = Host(comp).call('timeline.compare')
        self.assertEqual(result['verdict']['abstain_reason'], 'timeline_invalid')
        self.assertEqual(result['verdict']['checks'], [])


class RestoredBehaviourAndEvaluateTests(unittest.TestCase):
    def test_multiple_mismatches_are_retained_and_exposed(self):
        ref_a = {'case_id': 'a-1', 'input': {'action': 'a'}, 'kind': 'value', 'value': 'on'}
        ref_b = {'case_id': 'b-1', 'input': {'action': 'b'}, 'kind': 'value', 'value': 'on'}
        cand_a = dict(ref_a, value='off')
        cand_b = dict(ref_b, value='off')
        policy = {'id': 'fixture.behaviour', 'version': '1', 'sha256': digest(b'fixture policy v1')}
        report = behaviour.compare([ref_a, ref_b], [cand_a, cand_b],
                                   policy=policy, scope='a-1:return_value', waivers=[])
        self.assertEqual(report['result'], 'mismatch')
        self.assertEqual(sorted(row['case_id'] for row in report['mismatches']), ['a-1', 'b-1'])
        self.assertEqual(report['deltas'], report['mismatches'])
        self.assertEqual(report['coverage']['reference_count'], 2)
        self.assertEqual(report['exact_case_ids'], [])
        self.assertEqual(report['accepted_differences'], [])

    def test_empty_check_list_yields_slot_claim_only(self):
        report = evaluate('timeline.compare', comparison(()))
        self.assertEqual(report['verdict']['result'], 'abstain')
        self.assertEqual(report['verdict']['abstain_reason'], 'slot_claim_only')
        self.assertEqual(report['verdict']['checks'], [])

    def test_malformed_control_without_stop_abstains_as_diagnostic(self):
        from .test_comparators import event_comparison
        malformed = ({'control_id': 'neg-1', 'executed': 'yes',
                      'activated': True, 'outcome': 'rejected'},)
        comp = replace(event_comparison(), negative_controls=malformed)
        report = evaluate('timeline.compare', comp)
        self.assertNotEqual(report['verdict']['result'], 'match')
        self.assertTrue(report['diagnostics']['malformed_negative_controls'])
        host = Host(comp)
        result = host.call('timeline.compare')
        self.assertEqual(result['verdict']['abstain_reason'], 'negative_control_missing')
        self.assertTrue(host.report['diagnostics']['malformed_negative_controls'])

    def test_result_schema_declares_declared_fields(self):
        for result_schema in tools.RESULT_SCHEMAS.values():
            for name in ('coverage', 'deltas', 'exception_refs', 'order_conflicts',
                         'aligned_occurrences', 'spatial_difference', 'receipt_ref'):
                self.assertIn(name, result_schema['properties'])


class RestoredVisualPreflightTests(unittest.TestCase):
    def test_malformed_media_never_acquires_lease(self):
        cases = [
            ('extra-key', lambda rec: rec.update(extra_field='nope'), 'evidence_binding_rejected'),
            ('long-container', lambda rec: rec.update(container='x' * 65), 'evidence_binding_rejected'),
            ('invalid-clock', lambda rec: rec.update(clock_source='sidereal'), 'evidence_binding_rejected'),
            ('wall-clock', lambda rec: rec.update(clock_source='wall'), 'clock_source_unsuitable'),
            ('empty-no-reason', lambda rec: rec.update(frames=[]), 'evidence_binding_rejected'),
            ('empty-with-reason',
             lambda rec: rec.update(frames=[], frames_unavailable_reason='No frames were retained.'),
             'missing_reference_coverage'),
            ('nonempty-with-reason',
             lambda rec: rec.update(frames_unavailable_reason='Should not be present.'),
             'evidence_binding_rejected'),
            ('bad-time-base', lambda rec: rec.update(time_base={'num': 1}), 'missing_clock_binding'),
        ]
        for label, mutate, expected in cases:
            with self.subTest(label=label):
                fixture = VisualFixture()
                mutate(fixture.records['reference'])
                result = fixture.direct()
                self.assertEqual(result['abstain_reason'], expected)
                self.assertEqual(fixture.pool.lifecycle, [])
                if expected == 'evidence_binding_rejected':
                    self.assertEqual(result['checks'], [])

    def test_media_gate_beats_whitespace_front(self):
        fixture = VisualFixture()
        fixture.records['reference']['frames'] = []
        fixture.records['reference']['frames_unavailable_reason'] = 'No frames were retained.'
        result = fixture.direct(front='   ')
        self.assertEqual(result['abstain_reason'], 'missing_reference_coverage')
        self.assertEqual(fixture.pool.lifecycle, [])
