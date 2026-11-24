# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic-host structured behaviour comparison for retained REA cases."""

import hashlib
import json

from ._behaviour import compare_behaviour
from ._model import Behaviour, Policy, Rejected, is_digest, is_identifier


def _digest_of(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _observation(case):
    if 'observation' in case:
        return case['observation']
    kind = case.get('kind')
    if kind == 'value':
        return {'kind': 'value', 'payload': case['value']}
    if kind == 'undefined':
        return {'kind': 'undefined'}
    if kind == 'throw':
        return {'kind': 'throw', 'error_class': case['error_class']}
    if kind == 'ordered_pairs':
        return {'kind': 'ordered_pairs', 'pairs': case['pairs']}
    raise ValueError('Unsupported behaviour case kind.')


def _case_internal(case):
    if type(case) is not dict or 'case_id' not in case:
        raise ValueError('A behaviour case needs a case_id.')
    digest = case['input_digest'] if 'input_digest' in case else _digest_of(case['input'])
    return {'case_id': case['case_id'], 'input_digest': digest,
            'observation': _observation(case)}


def _waiver_internal(waiver):
    """Preserve the waiver's own binding for the shared validator."""
    if type(waiver) is not dict:
        raise ValueError('A waiver must be an object.')
    return dict(waiver)


def _reject(reason, detail):
    return {'result': 'abstain', 'abstain_reason': reason,
            'abstain_detail': detail[:2000], 'checks': [], 'exception_refs': []}


def compare(reference, candidate, *, policy, scope, waivers):
    """Compare retained structured cases and scoped waiver records."""
    if (type(policy) is not dict or not is_identifier(policy.get('id'))
            or type(policy.get('version')) is not str or not 1 <= len(policy['version']) <= 64
            or not is_digest(policy.get('sha256'))):
        return _reject('policy_undeclared', 'The behaviour policy is invalid.')
    if type(scope) is not str or not scope.strip():
        return _reject('policy_undeclared', 'The waiver scope is invalid.')
    pin = {key: policy[key] for key in ('id', 'version', 'sha256')}
    host_policy = Policy(**pin, comparator_id='rea.fixture.behaviour', comparator_version='1',
                         checks=('structured',), determinism_modes=frozenset({'uncontrolled'}),
                         repeatable_effects_required=False, contrast_ids=(),
                         negative_control_ids=(), waiver_source=scope, waiver_scope=scope)
    try:
        reference_side = None if reference is None else Behaviour(
            cases=tuple(_case_internal(row) for row in reference), complete=True)
        candidate_side = None if candidate is None else Behaviour(
            cases=tuple(_case_internal(row) for row in candidate), complete=True)
        waiver_rows = tuple(_waiver_internal(row) for row in (waivers or ()))
        finding = compare_behaviour(reference_side, candidate_side, host_policy, waiver_rows)
    except Rejected as error:
        return _reject(error.reason, str(error))
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as error:
        return _reject('evidence_binding_rejected', str(error) or 'Malformed behaviour input.')
    diagnostics = finding.diagnostics or {}
    accepted = list(diagnostics.get('accepted_differences', []))
    mismatches = list(diagnostics.get('mismatches', []))
    report = {'result': finding.outcome, 'checks': [finding.receipt_fields()],
              'exception_refs': [row['waiver_ref'] for row in accepted]}
    if diagnostics:
        report.update(exact_case_ids=list(diagnostics['exact_case_ids']),
                      accepted_differences=accepted, mismatches=mismatches, deltas=list(mismatches))
        report['coverage'] = {key: diagnostics[key] for key in (
            'reference_count', 'candidate_count', 'reference_complete',
            'candidate_complete', 'unknown_case_ids', 'stale_waiver_refs')}
    if finding.outcome == 'abstain':
        report.update(abstain_reason=finding.reason, abstain_detail=finding.detail[:2000])
    return report
