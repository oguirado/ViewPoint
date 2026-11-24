# SPDX-License-Identifier: AGPL-3.0-only
"""Acceptance matrix through the comparator handler and projection boundaries."""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from unittest.mock import Mock

from slot_host.compare import tools
from .test_comparators import (
    ATTEMPT, EVENT_KINDS, FRAME_BYTES, FRONT, Host, PAYLOAD, REQUEST, VisualFixture,
    behaviour_comparison, cases, digest, event_comparison, event_trace, timeline_side, waiver_for,
)


def fixture_host(tool):
    if tool == 'timeline.compare':
        return Host(event_comparison())
    if tool == 'behaviour.compare':
        return Host(behaviour_comparison())
    fixture = VisualFixture()
    return Host(fixture.comparison(), fixture.vision())


class MatrixTests(unittest.TestCase):
    def assert_match(self, result, kinds):
        verdict = result['verdict']
        self.assertEqual(verdict['result'], 'match', result)
        self.assertEqual([(row['kind'], row['outcome']) for row in verdict['checks']],
                         [(kind, 'match') for kind in kinds])

    def assert_abstains(self, result, reason, *, empty=False):
        verdict = result['verdict']
        self.assertEqual((verdict['result'], verdict['abstain_reason']), ('abstain', reason), result)
        self.assertFalse(verdict['independent_qualification'])
        if empty:
            self.assertEqual(verdict['checks'], [])

    def assert_timeline_mismatch(self, result, detail, event_id):
        self.assertEqual(result['verdict']['result'], 'mismatch', result)
        check, = result['verdict']['checks']
        self.assertEqual(check['detail'], detail)
        self.assertEqual(check['first_divergence']['event_id'], event_id)

    def test_public_comparators_match_independently_witnessed_pairs(self):
        for tool, kinds in (('behaviour.compare', ('structured_value',)),
                            ('timeline.compare', ('event_timeline',)),
                            ('visual.compare', ('presentation_time', 'pixels'))):
            with self.subTest(tool=tool):
                host = fixture_host(tool)
                result = host.call(tool)
                self.assert_match(result, kinds)
                self.assertTrue(result['verdict']['independent_qualification'])
                self.assertNotEqual(result['receipt_ref'], result['artifact_ref'])
                self.assertEqual(host.seen, [(tool, REQUEST, PAYLOAD, host.comp.policy.pin())])

    def test_timeline_reports_inverted_order_within_tolerance(self):
        reference = timeline_side(event_trace())
        normal = timeline_side(event_trace((80, 82, 500), prefix='c'))
        self.assert_match(Host(event_comparison(reference, normal)).call('timeline.compare'), ('event_timeline',))
        inverse = event_trace((82, 80, 500), prefix='c')
        result = Host(event_comparison(reference, timeline_side([inverse[1], inverse[0], inverse[2]]))).call('timeline.compare')
        self.assert_timeline_mismatch(result, 'timeline.order_inverted', 'e2')
        self.assertEqual(result['order_conflicts'], [{'earlier_reference_event_id': 'e1', 'later_reference_event_id': 'e2'}])
        self.assertEqual([row['delta_ms_exact'] for row in result['deltas']], [[31, 1], [0, 1], [0, 1]])
        self.assertEqual(result['coverage']['reference_missing_kinds'], [])

    def test_equal_pixels_with_submillisecond_pts_shift_mismatch(self):
        base = VisualFixture()
        self.assert_match(Host(base.comparison(), base.vision()).call('visual.compare'), ('presentation_time', 'pixels'))
        fixture = VisualFixture(candidate_tick=81)
        self.assertEqual(fixture.records['reference']['frames'][1]['pts_ms'], fixture.records['candidate']['frames'][1]['pts_ms'])
        result = Host(fixture.comparison(), fixture.vision()).call('visual.compare')
        pts, spatial = result['verdict']['checks']
        self.assertEqual(result['verdict']['result'], 'mismatch')
        self.assertEqual(pts['detail'], 'pts.occurrence_or_time_differs')
        self.assertEqual(spatial['outcome'], 'abstain')
        self.assertEqual(result['spatial_difference'], [])
        self.assertEqual(result['aligned_occurrences'][1]['delta_ms_exact'], [1, 2])
        other_base = VisualFixture(candidate_tick=160, candidate_den=4000)
        self.assert_match(Host(other_base.comparison(), other_base.vision()).call('visual.compare'),
                          ('presentation_time', 'pixels'))

    def test_empty_reference_retains_incomplete_coverage_and_abstains(self):
        self.assert_match(fixture_host('timeline.compare').call('timeline.compare'), ('event_timeline',))
        host = Host(event_comparison(reference=timeline_side([])))
        result = host.call('timeline.compare')
        self.assert_abstains(result, 'empty_reference', empty=True)
        check, = host.report['diagnostics']['checks']
        self.assertEqual(check['diagnostics']['reference_count'], 0)
        self.assertEqual(check['diagnostics']['reference_missing_kinds'], sorted(EVENT_KINDS))
        self.assertNotIn('coverage', result)

    def test_changed_waived_actual_rejects_stale_exception(self):
        comp = behaviour_comparison('allowed')
        waiver = waiver_for(comp)
        comp = replace(comp, waivers=(waiver,))
        positive = Host(comp).call('behaviour.compare')
        self.assert_match(positive, ('structured_value',))
        self.assertEqual(positive['exception_refs'], [waiver['ref']])
        host = Host(replace(comp, candidate=replace(comp.candidate, behaviour=cases('off'))))
        result = host.call('behaviour.compare')
        self.assert_abstains(result, 'evidence_binding_rejected', empty=True)
        self.assertEqual(result['exception_refs'], [])
        self.assertEqual(host.report['diagnostics']['checks'][0]['diagnostics']['stale_waiver_refs'], [waiver['ref']])

    def test_wrong_decoded_frame_digest_rejects_visual_evidence(self):
        fixture = VisualFixture()
        host = Host(fixture.comparison(), fixture.vision())
        self.assert_match(host.call('visual.compare'), ('presentation_time', 'pixels'))
        fixture.records['candidate']['frames'][1]['decoded_frame_sha256'] = digest(b'other raster')
        result = host.call('visual.compare')
        self.assert_abstains(result, 'evidence_binding_rejected', empty=True)
        self.assertNotIn('aligned_occurrences', result)
        self.assertNotIn('spatial_difference', result)
        self.assertIsNone(fixture.pool.owner)

    def test_timeline_requires_common_domain_or_verified_anchor(self):
        ref = timeline_side(event_trace())
        cand = timeline_side(event_trace(prefix='c'))
        self.assert_match(Host(event_comparison(ref, cand)).call('timeline.compare'), ('event_timeline',))
        anchored_ref = replace(ref, common_anchor='anchor:fixture')
        anchored_cand = replace(cand, clock_domain='other.clock', common_anchor='anchor:fixture')
        self.assert_match(Host(event_comparison(anchored_ref, anchored_cand)).call('timeline.compare'), ('event_timeline',))
        for absent in (None, '', '   '):
            with self.subTest(domain=absent):
                broken = event_comparison(replace(ref, clock_domain=absent), replace(cand, clock_domain=absent))
                self.assert_abstains(Host(broken).call('timeline.compare'), 'missing_clock_binding')

    def test_equal_reference_times_impose_no_precedence(self):
        reference = timeline_side(event_trace((80, 80, 500)))
        candidate = event_trace((82, 80, 500), prefix='c')
        result = Host(event_comparison(reference, timeline_side([candidate[1], candidate[0], candidate[2]]))).call('timeline.compare')
        self.assert_match(result, ('event_timeline',))
        self.assertEqual(result['order_conflicts'], [])
        self.assertEqual(result['verdict']['timeline_policy']['equal_time_precedence'], 'none')
        self.assertEqual(result['coverage']['candidate_count'], 3)

    def test_timeline_rejects_early_arrival_at_exact_tick_boundary(self):
        self.assert_match(fixture_host('timeline.compare').call('timeline.compare'), ('event_timeline',))
        host = Host(event_comparison(candidate=timeline_side(event_trace((20, 79, 500), prefix='c'))))
        result = host.call('timeline.compare')
        self.assert_timeline_mismatch(result, 'timeline.event_early', 'e2')
        self.assertEqual(result['deltas'][1]['delta_ms_exact'], [-1, 2])
        self.assertTrue(result['coverage']['candidate_complete'])

    def test_timeline_late_tolerance_is_inclusive_in_exact_time(self):
        boundary = Host(event_comparison(candidate=timeline_side(event_trace((20, 146, 500), prefix='c')))).call('timeline.compare')
        self.assert_match(boundary, ('event_timeline',))
        self.assertEqual(boundary['deltas'][1]['delta_ms_exact'], [33, 1])
        result = Host(event_comparison(candidate=timeline_side(event_trace((20, 147, 500), prefix='c')))).call('timeline.compare')
        self.assert_timeline_mismatch(result, 'timeline.late_beyond_tolerance: event_late', 'e2')
        self.assertEqual(result['deltas'][1]['delta_ms_exact'], [67, 2])
        self.assertTrue(result['coverage']['reference_complete'])

    def test_duplicate_required_control_blocks_match_and_preserves_bound_mismatch(self):
        comp = event_comparison()
        self.assertTrue(Host(comp).call('timeline.compare')['verdict']['independent_qualification'])
        original, = comp.negative_controls
        for duplicate in (dict(original, outcome='survived'), {'control_id': original['control_id']}):
            with self.subTest(duplicate=duplicate):
                conflicting = (original, duplicate)
                host = Host(replace(comp, negative_controls=conflicting))
                self.assert_abstains(host.call('timeline.compare'), 'negative_control_missing')
                self.assertEqual(host.report['diagnostics']['ambiguous_negative_controls']['neg-1'], list(conflicting))
                diverges = event_comparison(candidate=timeline_side(event_trace((20, 79, 500), prefix='c')))
                result = Host(replace(diverges, negative_controls=conflicting)).call('timeline.compare')
                self.assert_timeline_mismatch(result, 'timeline.event_early', 'e2')
                self.assertFalse(result['verdict']['independent_qualification'])

    def test_waiver_requires_its_own_complete_custody_fields(self):
        comp = behaviour_comparison('allowed')
        waiver = waiver_for(comp)
        self.assert_match(Host(replace(comp, waivers=(waiver,))).call('behaviour.compare'), ('structured_value',))
        trials = [(key, {field: value for field, value in waiver.items() if field != key}) for key in waiver]
        trials.extend((key, dict(waiver, **{key: value})) for key, value in (
            ('policy', {'version': '1'}), ('policy', dict(comp.policy.pin(), sha256=digest(b'other policy'))),
            ('source', 'other.source'), ('scope', 'other.scope'), ('input_digest', digest(b'other input')),
            ('actual', {'kind': 'undefined'}), ('reason', '  ')))
        for key, row in trials:
            with self.subTest(field=key, row=row):
                result = Host(replace(comp, waivers=(row,))).call('behaviour.compare')
                self.assert_abstains(result, 'evidence_binding_rejected', empty=True)
                self.assertEqual(result['exception_refs'], [])

    def test_observer_millisecond_projection_cannot_supply_encoded_ticks(self):
        fixture = VisualFixture()
        host = Host(fixture.comparison(), fixture.vision())
        self.assert_match(host.call('visual.compare'), ('presentation_time', 'pixels'))
        fixture.mutations['candidate', 1] = lambda row: row.pop('pts_ticks')
        self.assert_abstains(host.call('visual.compare'), 'evidence_binding_rejected', empty=True)

    def test_retained_time_base_cannot_supply_missing_decoder_witness(self):
        fixture = VisualFixture()
        host = Host(fixture.comparison(), fixture.vision())
        self.assert_match(host.call('visual.compare'), ('presentation_time', 'pixels'))
        fixture.mutations['candidate', 1] = lambda row: row.pop('time_base')
        self.assert_abstains(host.call('visual.compare'), 'missing_clock_binding')

    def test_visual_rejects_each_substituted_frame_custody_field(self):
        for field, value in (('frame_index', 19), ('pts_ticks', 81), ('time_base', {'num': 2, 'den': 4000}),
                             ('recording_bytes', b'other stream'), ('frame_file_bytes', b'other extraction')):
            with self.subTest(field=field):
                fixture = VisualFixture()
                host = Host(fixture.comparison(), fixture.vision())
                self.assert_match(host.call('visual.compare'), ('presentation_time', 'pixels'))
                fixture.mutations['candidate', 1] = lambda row: row.update({field: value})
                self.assert_abstains(host.call('visual.compare'), 'evidence_binding_rejected', empty=True)
                self.assertIsNone(fixture.pool.owner)
        fixture = VisualFixture()
        fixture.records['candidate']['recording_sha256'] = digest(b'other recording')
        self.assert_abstains(Host(fixture.comparison(), fixture.vision()).call('visual.compare'),
                             'evidence_binding_rejected', empty=True)

    def test_truncated_candidate_lookup_cannot_certify_recording_completion(self):
        fixture = VisualFixture()
        host = Host(fixture.comparison(), fixture.vision())
        self.assert_match(host.call('visual.compare'), ('presentation_time', 'pixels'))
        self.assertIn(('candidate', 2), fixture.calls)
        fixture.lookup_limit['candidate'] = 1
        fixture.records['candidate']['frames'] = fixture.records['candidate']['frames'][:1]
        self.assert_abstains(host.call('visual.compare'), 'coverage_insufficient')
        self.assertIsNone(fixture.pool.owner)

    def test_missing_pinned_reference_frame_refuses_lease_acquisition(self):
        fixture = VisualFixture()
        host = Host(fixture.comparison(indices=(0, 1)), fixture.vision())
        result = host.call('visual.compare')
        self.assert_match(result, ('presentation_time', 'pixels'))
        self.assertEqual([row['delta_ms_exact'] for row in result['aligned_occurrences']], [[0, 1], [0, 1]])
        fixture.records['reference']['frames'].pop()
        fixture.pool.lifecycle.clear()
        fixture.calls.clear()
        self.assert_abstains(host.call('visual.compare'), 'missing_reference_coverage')
        self.assertEqual(fixture.pool.lifecycle, [])
        self.assertEqual(fixture.calls, [])

    def test_visual_raster_budget_bounds_decoder_and_observer_memory(self):
        fixture = VisualFixture()
        full = Host(fixture.comparison(), fixture.vision())
        self.assert_match(full.call('visual.compare'), ('presentation_time', 'pixels'))
        budget = full.report['diagnostics']['vision_custody']['raster_budget']
        self.assertEqual(budget['peak_reserved_bytes'], 2 * FRAME_BYTES)
        self.assertEqual(budget['remaining_reserved_bytes'], 0)
        fixture.live_allocations.clear()
        ceiling = 2 * FRAME_BYTES - 1
        limited = Host(fixture.comparison(), fixture.vision(ceiling=ceiling))
        self.assert_abstains(limited.call('visual.compare'), 'coverage_insufficient')
        budget = limited.report['diagnostics']['vision_custody']['raster_budget']
        self.assertEqual(budget['denied_allocation_bytes'], FRAME_BYTES)
        self.assertEqual(budget['remaining_reserved_bytes'], 0)
        self.assertEqual(fixture.live_allocations, [FRAME_BYTES])
        self.assertLessEqual(budget['peak_reserved_bytes'], ceiling)
        self.assertIsNone(fixture.pool.owner)
        self.assertEqual(fixture.pool.lifecycle[-1], 'released')

    def test_expired_grant_performs_no_vision_and_publishes_no_ticket(self):
        fixture = VisualFixture()
        host = Host(fixture.comparison(), fixture.vision())
        self.assertEqual(host.call('visual.compare')['observation']['perception_level'], 'vision')
        fixture.pool.fault = 'expired'
        fixture.calls.clear()
        result = host.call('visual.compare')
        self.assert_abstains(result, 'lease_custody_unverified')
        self.assertEqual(result['observation']['perception_level'], 'none')
        self.assertNotIn('vision_pool_ticket', result['observation'])
        self.assertEqual(fixture.calls, [])
        custody = host.report['diagnostics']['vision_custody']
        self.assertEqual(custody['acquire']['grant']['lease_id'], fixture.pool.token)
        self.assertFalse(custody['acquire']['verified'])
        self.assertTrue(custody['release']['verified'])

    def test_visual_requires_restricted_grant_and_release_bound_to_attempt(self):
        for fault in (None, 'front', 'starttime', 'incomplete', 'release'):
            with self.subTest(fault=fault):
                fixture = VisualFixture(fault=fault)
                host = Host(fixture.comparison(), fixture.vision())
                result = host.call('visual.compare')
                custody = host.report['diagnostics']['vision_custody']
                if fault is None:
                    self.assert_match(result, ('presentation_time', 'pixels'))
                    grant, release = custody['acquire'], custody['release']
                    self.assertTrue(grant['verified'] and release['verified'])
                    for key in ('attempt_id', 'front', 'pid', 'starttime'):
                        self.assertEqual(grant[key], release[key])
                    self.assertEqual(grant['attempt_id'], ATTEMPT)
                    self.assertEqual(grant['ticket']['lease_id'], release['lease_id'])
                    self.assertEqual(grant['ticket']['projection'], 'restricted')
                    self.assertEqual(result['observation']['vision_pool_ticket'], {
                        'projection': 'redacted', 'status': 'granted', 'front': FRONT,
                        'lease_id_sha256': digest(fixture.pool.token.encode('utf-8'))})
                    self.assertNotIn(fixture.pool.token, json.dumps(result))
                else:
                    self.assert_abstains(result, 'lease_custody_unverified')
                    self.assertFalse(result.get('spatial_difference'))
                    if fault == 'release':
                        self.assertTrue(fixture.calls)
                        self.assertEqual(result['observation']['perception_level'], 'vision')
                        self.assertFalse(custody['release']['verified'])
                        self.assertEqual(custody['discarded_checks'][1]['outcome'], 'match')
                    else:
                        self.assertEqual(fixture.calls, [])
                        self.assertEqual(result['observation']['perception_level'], 'none')
                self.assertIsNone(fixture.pool.owner)
        fixture = VisualFixture()
        host = Host(fixture.comparison(), replace(fixture.vision(), attempt_id='other.attempt'))
        self.assert_abstains(host.call('visual.compare'), 'lease_custody_unverified')
        self.assertEqual(fixture.pool.lifecycle, [])

    def test_rejected_timeline_findings_stay_in_retained_artifact(self):
        comp = event_comparison()
        positive = Host(comp).call('timeline.compare')
        self.assertTrue(positive['coverage'] and positive['deltas'])
        self.assertIn('order_conflicts', positive)
        host = Host(replace(comp, contrast_outcomes={'contrast-1': False}))
        result = host.call('timeline.compare')
        self.assert_abstains(result, 'contrast_failed', empty=True)
        for field in ('coverage', 'deltas', 'order_conflicts'):
            self.assertNotIn(field, result)
        check, = host.report['diagnostics']['checks']
        self.assertEqual(check['outcome'], 'match')
        self.assertEqual(check['diagnostics']['events'], positive['deltas'])

    def test_public_comparator_receipt_round_trips_and_artifact_alias_cannot_admit(self):
        for tool in tools.MINI_TOOLS:
            with self.subTest(tool=tool):
                host = fixture_host(tool)
                result = host.call(tool)
                receipt = host.inspect(result['receipt_ref'], authorized=True)
                self.assertEqual((receipt.attempt_id, receipt.request_id), (ATTEMPT, REQUEST))
                self.assertEqual(receipt.artifact_ref, result['artifact_ref'])
                self.assertEqual(receipt.artifact_sha256, digest(host.artifacts[result['artifact_ref']]))
                with self.assertRaises(PermissionError):
                    host.inspect(result['receipt_ref'], authorized=False)
                host.artifact_only = True
                with self.assertRaisesRegex(ValueError, 'no verified comparison receipt'):
                    host.call(tool)
                host.artifact_only = False
                retain = host.retain_comparison

                def aliased(*args):
                    retained = retain(*args)
                    return replace(retained, receipt=replace(retained.receipt, receipt_ref=retained.artifact_ref))

                host.retain_comparison = aliased
                with self.assertRaisesRegex(ValueError, 'does not bind'):
                    host.call(tool)
                descriptor = tools.ROUTER_ENTRIES[f'host.{tool}']
                self.assertEqual(descriptor['availability'], 'parked')
                self.assertIsNone(descriptor['conformance_receipt_ref'])
        comp = replace(event_comparison(), attempt_id=None)
        result = Host(comp, artifact_only=True).call('timeline.compare')
        self.assertIn('artifact_ref', result)
        self.assertNotIn('receipt_ref', result)

    def test_comparator_projections_share_dispatch_and_refuse_parked_route(self):
        arguments = {'request_id': REQUEST, 'payload': PAYLOAD}
        context = {'front': FRONT}
        for tool, module in tools.MINI_TOOLS.items():
            with self.subTest(tool=tool):
                descriptor = tools.ROUTER_ENTRIES[f'host.{tool}']
                mcp = tools.MCP_TOOLS[descriptor['mcp_name']]
                self.assertEqual(mcp['inputSchema']['$id'], descriptor['input_schema_ref'])
                self.assertEqual(descriptor['json_contract_ref']['op'], 'rea.slot.project')
                expected = tools.control_request(tool, arguments, context=context)
                host = fixture_host(tool)

                def dispatch(envelope):
                    self.assertEqual(envelope, expected)
                    payload = envelope['args']['arguments']['payload']
                    result = module['handler'](payload, request_id=envelope['request_id'], host=host)
                    return {'ok': True, 'code': 'OK', 'value': {'host_result': result}}

                argv = ['mini', tool, '--args', json.dumps(arguments)]
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    self.assertEqual(tools.main(argv, dispatch=dispatch, context=context), 0)
                self.assertEqual(json.loads(stdout.getvalue()), dispatch(expected))
                seen = list(host.seen)
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(tools.main(argv, context=context), 16)
                self.assertEqual(stdout.getvalue(), '')
                self.assertIn('AVAILABILITY_PARKED', stderr.getvalue())
                self.assertEqual(host.seen, seen)
                self.assertEqual(descriptor['availability'], 'parked')
                parked = Mock(return_value={'ok': False, 'code': 'AVAILABILITY_PARKED'})
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    self.assertEqual(tools.main(argv, dispatch=parked, context=context), 16)
                parked.assert_called_once_with(expected)
                self.assertEqual(json.loads(stdout.getvalue()), {'ok': False, 'code': 'AVAILABILITY_PARKED'})

    def test_distribution_remains_unsupported_for_ordinary_evidence(self):
        comp = behaviour_comparison()
        comp = replace(comp, policy=replace(comp.policy, determinism_modes=frozenset({'uncontrolled', 'distribution'})))
        positive = Host(comp).call('behaviour.compare')
        self.assert_match(positive, ('structured_value',))
        self.assertNotEqual(positive['receipt_ref'], positive['artifact_ref'])
        result = Host(replace(comp, determinism_mode='distribution')).call('behaviour.compare')
        self.assert_abstains(result, 'determinism_mode_unsupported', empty=True)
