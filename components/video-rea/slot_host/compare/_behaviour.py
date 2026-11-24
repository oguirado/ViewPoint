# SPDX-License-Identifier: AGPL-3.0-only
"""Compare tagged observations and validate host-approved difference waivers."""

from math import isfinite

from ._model import Finding, Rejected, is_digest, is_identifier


def _value_key(value):
    """Preserve scalar types and array order when comparing decoded JSON."""
    kind = type(value)
    if kind is str:
        value.encode('utf-8')
    elif kind is float:
        if not isfinite(value):
            raise ValueError('Observation numbers must be finite.')
    elif kind is list:
        return list, tuple(_value_key(item) for item in value)
    elif kind is dict:
        if any(type(key) is not str for key in value):
            raise ValueError('Observation objects require string keys.')
        return dict, tuple((_value_key(key), _value_key(value[key])) for key in sorted(value))
    elif kind not in (int, bool, type(None)):
        raise ValueError('Observation values must be JSON data.')
    return kind, value


def _observation_key(observation):
    if type(observation) is not dict:
        raise ValueError('Expected a tagged observation object.')
    kind = observation.get('kind')
    if kind == 'value' and observation.keys() == {'kind', 'payload'}:
        return kind, _value_key(observation['payload'])
    if kind == 'undefined' and observation.keys() == {'kind'}:
        return kind,
    if kind == 'throw' and observation.keys() == {'kind', 'error_class'}:
        error = observation['error_class']
        if type(error) is str and error:
            return kind, _value_key(error)
    if kind == 'ordered_pairs' and observation.keys() == {'kind', 'pairs'}:
        pairs = observation['pairs']
        if type(pairs) is list and all(type(pair) is list and len(pair) == 2
                                      and type(pair[0]) is str for pair in pairs):
            return kind, _value_key(pairs)
    raise ValueError('Unsupported or malformed observation tag.')


def _cases(side):
    indexed = {}
    for case in side.cases:
        if (type(case) is not dict or case.keys() != {'case_id', 'input_digest', 'observation'}
                or not is_identifier(case.get('case_id')) or not is_digest(case.get('input_digest'))
                or case['case_id'] in indexed):
            raise Rejected('evidence_binding_rejected', 'A structured observation has an invalid or duplicate case binding.')
        try:
            indexed[case['case_id']] = case['input_digest'], _observation_key(case['observation'])
        except (ValueError, UnicodeError, RecursionError) as error:
            raise Rejected('evidence_binding_rejected', 'A tagged structured observation is malformed.') from error
    return indexed


def _waivers(waivers, policy, reference, candidate):
    accepted = {}
    stale = []
    seen_refs = set()
    seen_cases = set()
    fields = {'ref', 'case_id', 'source', 'scope', 'policy', 'input_digest', 'actual', 'reason'}
    for row in waivers:
        if (type(row) is not dict or row.keys() != fields or not is_identifier(row.get('ref'))
                or not is_identifier(row.get('case_id')) or row['ref'] in seen_refs
                or row['case_id'] in seen_cases or type(row['policy']) is not dict
                or any(type(row[name]) is not str or not row[name].strip()
                       for name in ('source', 'scope', 'reason'))):
            raise Rejected('evidence_binding_rejected', 'A waiver has invalid or duplicate custody fields.')
        key = row['case_id']
        seen_refs.add(row['ref'])
        seen_cases.add(key)
        try:
            actual = _observation_key(row['actual'])
        except (ValueError, UnicodeError, RecursionError) as error:
            raise Rejected('evidence_binding_rejected', 'A waiver has a malformed actual observation.') from error
        if (is_digest(row['input_digest']) and row['source'] == policy.waiver_source
                and row['scope'] == policy.waiver_scope and row['policy'] == policy.pin()
                and key in reference and key in candidate
                and reference[key][0] == row['input_digest']
                and candidate[key] == (row['input_digest'], actual)
                and reference[key][1] != actual):
            accepted[key] = row['ref']
        else:
            stale.append(row['ref'])
    return accepted, sorted(stale)


def compare_behaviour(reference, candidate, policy, waivers):
    if reference is None or not reference.cases:
        return Finding('structured', 'structured_value', 'abstain',
                       'No reference cases were retained.', 'missing_reference_coverage')
    if candidate is None:
        return Finding('structured', 'structured_value', 'abstain',
                       'No candidate cases were retained.', 'coverage_insufficient')
    ref = _cases(reference)
    cand = _cases(candidate)
    for key in ref.keys() & cand.keys():
        if ref[key][0] != cand[key][0]:
            raise Rejected('evidence_binding_rejected', f'Case {key} resolves to different causal inputs.')
    accepted, stale = _waivers(waivers, policy, ref, cand)
    diagnostics = {
        'reference_count': len(ref), 'candidate_count': len(cand),
        'reference_complete': reference.complete is True,
        'candidate_complete': candidate.complete is True,
        'exact_case_ids': [], 'accepted_differences': [], 'unknown_case_ids': [],
        'mismatches': [], 'stale_waiver_refs': stale,
    }
    if stale:
        finding = Finding('structured', 'structured_value', 'abstain',
                          'A waiver failed its exact binding.', 'evidence_binding_rejected',
                          diagnostics=diagnostics)
        raise Rejected(finding.reason, finding.detail, finding=finding)
    first = None
    for key in sorted(ref.keys() | cand.keys()):
        if key not in ref:
            detail = 'case_extra' if reference.complete is True else None
        elif key not in cand:
            detail = 'case_missing' if candidate.complete is True else None
        elif ref[key][1] == cand[key][1]:
            diagnostics['exact_case_ids'].append(key)
            continue
        elif key in accepted:
            diagnostics['accepted_differences'].append({'case_id': key, 'waiver_ref': accepted[key]})
            continue
        else:
            detail = 'error_class_changed' if 'throw' in (ref[key][1][0], cand[key][1][0]) else 'value_changed'
        if detail is None:
            diagnostics['unknown_case_ids'].append(key)
        else:
            diagnostics['mismatches'].append({'case_id': key, 'detail': detail})
            if first is None:
                first = Finding('structured', 'structured_value', 'mismatch', f'structured.{detail}',
                                divergence={'case_id': key}, diagnostics=diagnostics)
    if first is not None:
        return first
    if reference.complete is not True or candidate.complete is not True:
        reason = 'missing_reference_coverage' if reference.complete is not True else 'coverage_insufficient'
        return Finding('structured', 'structured_value', 'abstain',
                       'Capture coverage is incomplete.', reason, diagnostics=diagnostics)
    return Finding('structured', 'structured_value', 'match',
                   'All required cases agree under the pinned policy.', diagnostics=diagnostics)
