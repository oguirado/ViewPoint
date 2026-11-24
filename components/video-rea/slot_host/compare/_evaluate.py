# SPDX-License-Identifier: AGPL-3.0-only
"""Evaluate authorized observation channels and issue host verdict fields."""

from ._behaviour import compare_behaviour
from ._model import Finding, Rejected, is_digest, is_identifier, unsigned
from ._timeline import compare_timeline
from ._visual import _media_rows, compare_visual


TOOL_CHECKS = {
    'timeline.compare': frozenset({'timeline'}),
    'visual.compare': frozenset({'timeline', 'pts', 'spatial'}),
    'behaviour.compare': frozenset({'timeline', 'structured'}),
}
_STATUS_CODES = {'OK': 0, 'ERROR': 2, 'INCOMPLETE': 3, 'STALE': 4, 'FAIL': 5, 'BUDGET_EXCEEDED': 6}
_STATUS_STOPS = {'ERROR': 'slot_error', 'STALE': 'slot_stale', 'BUDGET_EXCEEDED': 'slot_budget_exceeded'}
_MODES = frozenset({'exact', 'seeded', 'distribution', 'uncontrolled'})
_CONTROL_OUTCOMES = ('rejected', 'survived', 'invalid', 'not_executed')
_LEVELS = ('none', 'cdp_atspi', 'vision')
_COVERAGE_REASONS = ('coverage_insufficient', 'missing_reference_coverage')


def _gate(exchange):
    if exchange.control_reason is not None:
        reason = exchange.control_reason
        if reason not in ('control_refused', 'uncertain_outcome'):
            raise ValueError('Unknown host control reason.')
        raise Rejected(reason, 'The host control outcome stops comparison.')
    if exchange.exchange_valid is not True:
        raise Rejected('exchange_invalid', 'The provider exchange was not validated.')
    status = None
    if exchange.slot_result is not None:
        result = exchange.slot_result
        if type(result) is not dict:
            raise Rejected('exchange_invalid', 'The provider result reference is invalid.')
        status = result.get('status')
        rc = result.get('rc')
        if (result.get('schema') != 'rea.slot_result/v1' or type(status) is not str
                or type(rc) is not int or _STATUS_CODES.get(status) != rc
                or not is_digest(result.get('sha256'))):
            raise Rejected('exchange_invalid', 'The provider result reference is invalid.')
    if exchange.evidence_bound is not True:
        raise Rejected('evidence_binding_rejected', 'An exchange, producer or artifact binding was rejected.')
    if exchange.custody_verified is not True:
        raise Rejected('custody_unverified', 'The host did not establish evidence custody.')
    if exchange.producer['process_bound'] is not True:
        raise Rejected('custody_unverified', 'The host comparator producer lacks a process binding.')
    if exchange.lineage_verified is not True:
        raise Rejected('evidence_binding_rejected', 'The episode lineage did not qualify.')
    reference_tape = exchange.reference.tape_sha256
    candidate_tape = exchange.candidate.tape_sha256
    if not is_digest(reference_tape) or not is_digest(candidate_tape):
        raise Rejected('evidence_binding_rejected', 'Both episodes require verified input tape digests.')
    if reference_tape != candidate_tape:
        raise Rejected('tape_mismatch', 'The retained episode input tapes differ.')
    if status == 'STALE':
        raise Rejected('slot_stale', 'The provider reported stale evidence.')
    if exchange.fresh is not True:
        raise Rejected('stale_evidence', 'The retained bindings failed the host freshness gate.')
    if status in _STATUS_STOPS:
        raise Rejected(_STATUS_STOPS[status], 'The provider status stops comparison.')
    return status


def _verify_policy(tool, policy):
    checks = policy.checks
    valid = (type(checks) is tuple and all(type(check) is str for check in checks)
             and len(set(checks)) == len(checks) and set(checks) <= TOOL_CHECKS[tool])
    if valid and checks and tool == 'visual.compare':
        valid = checks[-2:] == ('pts', 'spatial')
    if valid and checks and tool == 'behaviour.compare':
        valid = 'structured' in checks
    if not valid:
        raise Rejected('policy_undeclared', 'The pinned policy does not declare a supported check order.')
    if 'timeline' in checks:
        time = policy.timeline
        if (time is None or not time.kinds or not time.required_kinds <= time.kinds
                or any(type(kind) is not str or not kind for kind in time.kinds)
                or not unsigned(time.late_tolerance_ms) or type(time.integer_milliseconds) is not bool):
            raise Rejected('policy_undeclared', 'The timeline policy is incomplete or unsupported.')
    if 'spatial' in checks:
        spatial = policy.spatial
        if (spatial is None or type(spatial.decoder_ref) is not str or not spatial.decoder_ref
                or not unsigned(spatial.max_mismatch_ppm) or spatial.max_mismatch_ppm > 1_000_000
                or not unsigned(spatial.max_channel_delta) or spatial.max_channel_delta > 255):
            raise Rejected('policy_undeclared', 'The raster policy is incomplete or unsupported.')
        region = spatial.region
        if region is not None and (type(region) is not tuple or len(region) != 4
                                   or not all(unsigned(value) for value in region) or min(region[2:]) == 0):
            raise Rejected('policy_undeclared', 'The pinned raster region is invalid.')
        indices = spatial.frame_indices
        if indices is not None and (type(indices) is not tuple or not indices
                                    or not all(unsigned(value) for value in indices)
                                    or indices != tuple(sorted(set(indices)))):
            raise Rejected('policy_undeclared', 'Required frame indices must be unique and increasing.')
    for ids in (policy.contrast_ids, policy.negative_control_ids):
        if (type(ids) is not tuple or not all(is_identifier(value) for value in ids)
                or len(set(ids)) != len(ids)):
            raise Rejected('policy_undeclared', 'The pinned control declarations are invalid.')
    if (not policy.determinism_modes or not policy.determinism_modes <= _MODES
            or type(policy.repeatable_effects_required) is not bool):
        raise Rejected('policy_undeclared', 'The pinned determinism policy is invalid.')


def _valid_control(row):
    fields = {'control_id', 'executed', 'activated', 'outcome'}
    if (row.keys() != fields or type(row.get('executed')) is not bool
            or type(row.get('activated')) is not bool or row.get('outcome') not in _CONTROL_OUTCOMES):
        return False
    outcome = row['outcome']
    return ((outcome == 'not_executed') != row['executed']
            and (row['executed'] or not row['activated'])
            and (outcome != 'rejected' or row['activated']))


def _controls(comparison):
    grouped = {}
    malformed = []
    controls = {}
    ambiguous = {}
    rows = comparison.negative_controls
    if type(rows) not in (tuple, list):
        malformed.append(rows)
        rows = ()
    for row in rows:
        if type(row) is not dict or not is_identifier(row.get('control_id')):
            malformed.append(row)
        else:
            grouped.setdefault(row['control_id'], []).append(row)
    for identity, group in grouped.items():
        if len(group) > 1:
            ambiguous[identity] = group
        elif _valid_control(group[0]):
            controls[identity] = dict(group[0])
        else:
            malformed.extend(group)
    required = comparison.policy.negative_control_ids
    if type(required) is tuple:
        for identity in required:
            if is_identifier(identity):
                controls.setdefault(identity, {'control_id': identity, 'executed': False,
                                               'activated': False, 'outcome': 'not_executed'})
    return controls, malformed, ambiguous


def _negative_failure(required, controls):
    for control in required:
        row = controls[control]
        if not row['executed']:
            return 'negative_control_missing', control
        if not row['activated'] or row['outcome'] == 'invalid':
            return 'negative_control_unactivated', control
        if row['outcome'] != 'rejected':
            return 'negative_control_survived', control
    return None, None


def _visual_denied(reason):
    detail = 'The host ceiling or grant prevents the required vision work.'
    return (Finding('pts', 'presentation_time', 'abstain', detail, reason),
            Finding('spatial', 'pixels', 'abstain', detail, reason))


def evaluate(tool, comparison, *, vision=None):
    """Return complete comparator diagnostics for retention and profile projection."""
    if tool not in TOOL_CHECKS:
        raise ValueError('Unknown comparison mini-tool.')
    policy = comparison.policy
    producer = comparison.producer
    if (not is_identifier(policy.id) or not is_identifier(policy.comparator_id)
            or not is_digest(policy.sha256)
            or any(type(version) is not str or not 1 <= len(version) <= 64
                   for version in (policy.version, policy.comparator_version))):
        raise ValueError('The comparator has no usable policy identity.')
    if (type(producer) is not dict or not {'kind', 'id', 'process_bound'} <= producer.keys()
            or not producer.keys() <= {'kind', 'id', 'process_bound', 'version', 'implementation_sha256'}
            or producer['kind'] not in ('host_level', 'host_mini_tool') or not is_identifier(producer['id'])
            or type(producer['process_bound']) is not bool
            or ('version' in producer and (type(producer['version']) is not str or not 1 <= len(producer['version']) <= 128))
            or ('implementation_sha256' in producer and not is_digest(producer['implementation_sha256']))
            or comparison.perception_ceiling not in _LEVELS):
        raise ValueError('The comparator has no usable producer or perception identity.')
    contrasts = comparison.contrast_outcomes
    if type(contrasts) is not dict or any(not is_identifier(key) or type(outcome) is not bool
                                         for key, outcome in contrasts.items()):
        raise ValueError('A retained contrast record is invalid.')
    observation = {
        'outside_capsule': True, 'producer': dict(producer),
        'perception_level': 'none', 'perception_ceiling': comparison.perception_ceiling,
    }
    controls, malformed_controls, ambiguous_controls = _controls(comparison)
    findings = []
    admitted = []
    vision_custody = None
    result = 'abstain'
    reason = None
    detail = None
    try:
        status = _gate(comparison)
        _verify_policy(tool, policy)
        if comparison.determinism_mode not in policy.determinism_modes:
            raise Rejected('determinism_mode_unsupported', 'The retained replay mode is outside the pinned policy.')
        if comparison.determinism_mode == 'distribution':
            raise Rejected('determinism_mode_unsupported',
                           'No qualified distribution comparator validates the sample evidence.')
        if policy.repeatable_effects_required and comparison.effects_repeatable is not True:
            raise Rejected('unrepeatable_effect', 'The policy depends on an effect without a repeatable witness.')
        reference = comparison.reference
        candidate = comparison.candidate
        if (policy.checks and 'timeline' not in policy.checks
                and (reference.timeline is not None or candidate.timeline is not None)):
            raise Rejected('policy_undeclared', 'A timeline result requires the event-timeline check.')
        if ('timeline' in policy.checks and reference.timeline is not None
                and type(reference.timeline.record) is dict and reference.timeline.record.get('events') == []):
            compare_timeline(reference.timeline, candidate.timeline, policy.timeline)
        for check in policy.checks:
            if check == 'timeline':
                findings.append(compare_timeline(reference.timeline, candidate.timeline, policy.timeline))
            elif check == 'structured':
                findings.append(compare_behaviour(reference.behaviour, candidate.behaviour, policy, comparison.waivers))
            elif check == 'pts':
                denied = None
                if comparison.perception_ceiling != 'vision':
                    denied = 'perception_ceiling'
                elif comparison.vision_authorized is not True:
                    denied = 'perception_unavailable'
                if denied is not None:
                    for side, media in (('reference', reference.media), ('candidate', candidate.media)):
                        try:
                            _media_rows(media, side)
                        except Rejected as error:
                            if error.reason not in _COVERAGE_REASONS:
                                raise
                    findings.extend(_visual_denied(denied))
                else:
                    visual = compare_visual(reference.media, candidate.media, policy.spatial, vision,
                                            attempt_id=comparison.attempt_id)
                    findings.extend(visual.findings)
                    vision_custody = visual.custody
                    if visual.used_vision:
                        observation.update(perception_level='vision', vision_pool_ticket=visual.ticket)
                    if visual.rejection is not None:
                        raise visual.rejection
        if (any(outcome is False for outcome in contrasts.values())
                or any(contrasts.get(control) is not True for control in policy.contrast_ids)):
            raise Rejected('contrast_failed', 'A required contrast was missing or a retained contrast failed.')
        admitted = findings
        unavailable = next((finding for finding in admitted if finding.outcome == 'abstain'), None)
        non_coverage = next((finding for finding in admitted if finding.outcome == 'abstain'
                             and finding.reason not in _COVERAGE_REASONS), None)
        if any(finding.outcome == 'mismatch' for finding in admitted):
            result = 'mismatch'
        elif non_coverage is not None:
            reason, detail = non_coverage.reason, non_coverage.detail
        elif status == 'INCOMPLETE':
            reason, detail = 'slot_incomplete', 'The partial provider outputs establish no bound divergence.'
        elif unavailable is not None:
            reason, detail = unavailable.reason, unavailable.detail
        elif admitted:
            reason, control = _negative_failure(policy.negative_control_ids, controls)
            if reason:
                detail = f'Required negative control {control} did not qualify.'
            elif status == 'FAIL':
                reason, detail = 'slot_host_disagreement', 'The provider reported FAIL and the host checks matched.'
            else:
                result = 'match'
        else:
            reason, detail = 'slot_claim_only', 'There is no admitted host check for the provider claim.'
    except Rejected as error:
        reason, detail = error.reason, str(error)
        admitted = []
        if error.finding is not None:
            findings.append(error.finding)

    independent = comparison.oracle_independent is True
    qualified = (result == 'match' and independent and bool(policy.negative_control_ids)
                 and all(row['outcome'] == 'rejected' for row in controls.values()))
    verdict = {
        'comparator_id': policy.comparator_id, 'comparator_version': policy.comparator_version,
        'result': result, 'policy': policy.pin(), 'checks': [finding.receipt_fields() for finding in admitted],
        'contrast_ids': sorted(contrasts), 'negative_controls': [controls[key] for key in sorted(controls)],
        'oracle_independence': 'independent' if independent else 'derived_from_same_source',
        'independent_qualification': qualified,
    }
    if any(finding.kind == 'event_timeline' for finding in admitted):
        verdict['timeline_policy'] = policy.timeline.receipt_fields()
    if result == 'abstain':
        verdict.update(abstain_reason=reason, abstain_detail=detail[:2000])
    if type(comparison.slot_reported_outcome) is str:
        verdict['slot_reported_outcome'] = comparison.slot_reported_outcome[:256]
    diagnostics = {
        'checks_admitted': bool(admitted), 'contrasts': dict(contrasts),
        'malformed_negative_controls': malformed_controls,
        'ambiguous_negative_controls': ambiguous_controls,
        'checks': [{**finding.receipt_fields(), 'diagnostics': finding.diagnostics}
                   for finding in findings],
    }
    if vision_custody is not None:
        diagnostics['vision_custody'] = vision_custody
    return {'observation': observation, 'verdict': verdict, 'diagnostics': diagnostics}
