# SPDX-License-Identifier: AGPL-3.0-only
"""Regression controls for verification order and fail-closed preflight."""

import unittest
from dataclasses import replace

from slot_host.compare._evaluate import evaluate
from .test_comparators import Host, VisualFixture, behaviour_comparison, digest, event_comparison, event_trace, timeline_side


class PreflightRegressionTests(unittest.TestCase):
    def test_malformed_media_under_low_ceiling_is_binding_rejection(self):
        fixture = VisualFixture()
        fixture.records['reference']['extra_field'] = 'unexpected'
        result = evaluate('visual.compare', replace(fixture.comparison(), perception_ceiling='none'), vision=fixture.vision())
        self.assertEqual(result['verdict']['abstain_reason'], 'evidence_binding_rejected')
        self.assertEqual(result['verdict']['checks'], [])
        self.assertEqual(fixture.pool.lifecycle, [])

    def test_malformed_media_precedes_missing_vision_and_time_base_errors(self):
        fixture = VisualFixture()
        fixture.records['reference'].update(extra_field='unexpected', time_base={'num': 1})
        result = fixture.direct(vision_pool=None)
        self.assertEqual((result['abstain_reason'], result['checks']), ('evidence_binding_rejected', []))
        del fixture.records['reference']['extra_field']
        self.assertEqual(fixture.direct()['abstain_reason'], 'missing_clock_binding')
        self.assertEqual(fixture.pool.lifecycle, [])

    def test_invalid_policy_and_lease_bounds_require_no_acquisition(self):
        fixture = VisualFixture()
        for bad in ([5, 2], [2, 2], [], [-1, 2], ['2', 5]):
            self.assertEqual(fixture.direct(frame_indices=bad)['abstain_reason'], 'policy_undeclared')
        for bad in ((0, 0, 0, 1), (-1, 0, 2, 1)):
            self.assertEqual(fixture.direct(region=bad)['abstain_reason'], 'policy_undeclared')
        for options in ({'front': '   '}, {'front': 'x' * 129}, {'timeout': 0},
                        {'timeout': '60'}, {'ttl': -1}, {'ttl': 2**31}):
            self.assertEqual(fixture.direct(**options)['abstain_reason'], 'lease_custody_unverified')
        self.assertEqual(fixture.pool.lifecycle, [])

    def test_timeout_and_missing_pool_do_not_observe(self):
        fixture = VisualFixture(fault='timeout')
        self.assertEqual(fixture.direct()['abstain_reason'], 'vision_pool_timeout')
        self.assertEqual(fixture.direct(vision_pool=None)['abstain_reason'], 'vision_pool_not_granted')
        self.assertEqual(fixture.calls, [])
        self.assertEqual(fixture.pool.lifecycle, ['timeout'])

    def test_custody_precedes_tape_mismatch_and_freshness(self):
        comp = event_comparison()
        unbound = replace(comp, producer=dict(comp.producer, process_bound=False))
        for mutation in (replace(unbound, fresh=False),
                         replace(unbound, candidate=replace(unbound.candidate, tape_sha256=digest(b'other tape')))):
            result = Host(mutation).call('timeline.compare')
            self.assertEqual(result['verdict']['abstain_reason'], 'custody_unverified')
            self.assertEqual(result['verdict']['checks'], [])

    def test_malformed_control_does_not_preempt_control_refusal(self):
        comp = replace(event_comparison(), negative_controls=({'control_id': 'neg-1', 'executed': 'yes'},))
        host = Host(replace(comp, control_reason='control_refused'))
        result = host.call('timeline.compare')
        self.assertEqual(result['verdict']['abstain_reason'], 'control_refused')
        self.assertEqual(result['verdict']['checks'], [])
        self.assertTrue(host.report['diagnostics']['malformed_negative_controls'])
        result = Host(comp).call('timeline.compare')
        self.assertEqual(result['verdict']['abstain_reason'], 'negative_control_missing')

    def test_noncoverage_abstention_precedes_slot_incomplete(self):
        wall = timeline_side(event_trace(), clock='wall')
        comp = event_comparison(reference=wall, candidate=wall)
        incomplete = {'schema': 'rea.slot_result/v1', 'status': 'INCOMPLETE', 'rc': 3,
                      'sha256': digest(b'provider partial result')}
        report = evaluate('timeline.compare', replace(comp, slot_result=incomplete))
        self.assertEqual(report['verdict']['abstain_reason'], 'clock_source_unsuitable')
        comp = event_comparison()
        missing = replace(comp, candidate=replace(comp.candidate, timeline=None), slot_result=incomplete)
        report = evaluate('timeline.compare', missing)
        self.assertEqual(report['verdict']['abstain_reason'], 'slot_incomplete')
        visual = VisualFixture().comparison(combined=missing)
        report = evaluate('visual.compare', replace(visual, perception_ceiling='none'))
        self.assertEqual(report['verdict']['abstain_reason'], 'perception_ceiling')

    def test_timeline_present_requires_event_timeline_check(self):
        comp = behaviour_comparison()
        ref = replace(comp.reference, timeline=timeline_side(event_trace()))
        report = evaluate('behaviour.compare', replace(comp, reference=ref))
        self.assertEqual(report['verdict']['abstain_reason'], 'policy_undeclared')
        self.assertEqual(report['verdict']['checks'], [])
