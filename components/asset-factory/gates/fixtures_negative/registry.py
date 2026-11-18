"""Original-case ledger. Registry metadata is never visual proof."""
from pathlib import Path
from gates.appearance_gate.evaluate import core, evaluate, _schema, Refusal

ORIGINAL_CASES = ('cart_boxed_raw', 'cart_white_cube_processed', 'orc_spheres', 'barrel_spikes')
BOUND_FIELDS = ('asset_sha256', 'reference_sha256', 'render_manifest_sha256',
                'reference_rgb_sha256', 'render_rgb_sha256', 'comparison_rgb_sha256')


def _bound(bindings):
    return (type(bindings) is dict and set(bindings) == set(BOUND_FIELDS) and
            all(type(pin) is str and len(pin) == 64 and all(c in '0123456789abcdef' for c in pin)
                for pin in bindings.values()))


def assess_fixture(entry, request=None, upstream=None, review=None):
    """Require verified provenance and a pinned revision before opening evidence."""
    result = {'case_id': entry.get('id'),
              'rc': 3, 'verdict': 'CANNOT_SEE', 'reason_codes': ['fixture_unavailable'],
              'bindings': {}, 'substitute_for': entry.get('substitute_for')}
    if not (entry.get('execution_allowed') is True and entry.get('provenance_state') == 'VERIFIED'
            and type(entry.get('revision')) is str and entry['revision'].strip()
            and entry['revision'].lower() not in ('main', 'master', 'latest', 'head')):
        return result
    if request is None:
        return result
    if not (entry.get('id') == request.get('case_id') and entry.get('asset_ref') == request.get('asset')
            and entry.get('reference_ref') == request.get('reference')
            and request.get('provider') == {'id': entry['provider'], 'revision': entry['revision']}):
        return {**result, 'rc': 4, 'reason_codes': ['fixture_binding_stale']}
    observed = evaluate(request, upstream, review)
    return {key: observed[key] for key in ('case_id', 'rc', 'verdict', 'reason_codes', 'bindings')} | {
        'substitute_for': entry.get('substitute_for')}


def _reduce_unchecked_observations(observations):
    """Unchecked ledger arithmetic only; never an acceptance gate."""
    by_id = {}
    for item in observations:
        case = item.get('case_id')
        if case in by_id:
            return {'rc': 2, 'verdict': 'ERROR', 'reason_codes': ['duplicate_case'], 'cases': {}}
        by_id[case] = item
    cases = {}
    for case in ORIGINAL_CASES:
        item = by_id.get(case)
        if item is None or item.get('substitute_for') is not None:
            cases[case] = 'CANNOT_SEE'
        elif type(item.get('rc')) is not int or item['rc'] not in core.RC or item.get('verdict') != core.VERDICT[item['rc']]:
            cases[case] = 'ERROR'
        elif item['rc'] == 2:
            cases[case] = 'ERROR'
        elif item.get('rc') == 5 and item.get('verdict') == 'FAIL' and _bound(item.get('bindings')):
            cases[case] = 'OBSERVED_FAIL'
        elif item.get('rc') == 0 and item.get('verdict') == 'PASS':
            cases[case] = 'FALSE_PASS'
        else:
            cases[case] = 'CANNOT_SEE'
    rc = (2 if 'ERROR' in cases.values() else 5 if 'FALSE_PASS' in cases.values()
          else 3 if 'CANNOT_SEE' in cases.values() else 0)
    return {'rc': rc, 'verdict': core.VERDICT[rc], 'cases': cases}


def coverage(observations, *, registry_data=None, registry_sha256=None):
    """Re-evaluate evidence against a registry pinned independently by the caller.

    Observations contain case_id, request, upstream and review. Stored result
    dictionaries prove nothing. Unavailable originals remain unobserved.
    """
    unavailable = {'rc': 3, 'verdict': 'CANNOT_SEE', 'reason_codes': ['registry_evidence_missing'],
                   'cases': dict.fromkeys(ORIGINAL_CASES, 'CANNOT_SEE')}
    if registry_data is None or registry_sha256 is None:
        return unavailable
    try:
        if not (type(registry_sha256) is str and len(registry_sha256) == 64 and
                core.sha256_json(registry_data) == registry_sha256):
            return {**unavailable, 'rc': 4, 'reason_codes': ['registry_stale']}
        if not (type(registry_data) is dict and registry_data.get('schema') == 'af.fixtures-negative/v1'
                and type(registry_data.get('fixtures')) is list and type(observations) is list):
            return {**unavailable, 'rc': 2, 'verdict': 'ERROR', 'reason_codes': ['registry_invalid']}
        schema = core.load_json(Path(__file__).with_name('fixtures-negative.schema.json'), max_bytes=2 ** 20)
        _schema(registry_data, schema)
        if not all(type(item) is dict and type(item.get('case_id')) is str and
                   (item.get('request') is None or type(item['request']) is dict)
                   for item in observations):
            return {**unavailable, 'rc': 2, 'verdict': 'ERROR', 'reason_codes': ['observation_invalid']}
        entries = {entry['id']: entry for entry in registry_data['fixtures']}
        supplied = {item['case_id']: item for item in observations}
        if len(entries) != len(registry_data['fixtures']) or len(supplied) != len(observations):
            return {**unavailable, 'rc': 2, 'verdict': 'ERROR', 'reason_codes': ['duplicate_case']}
        verified = []
        for case in ORIGINAL_CASES:
            entry, item = entries.get(case), supplied.get(case)
            if entry is None or item is None or entry.get('substitute_for') is not None:
                continue
            verified.append(assess_fixture(entry, item.get('request'), item.get('upstream'), item.get('review')))
        return _reduce_unchecked_observations(verified)
    except (Refusal, OSError, KeyError, TypeError, ValueError, RecursionError, core.ContractError):
        return {**unavailable, 'rc': 2, 'verdict': 'ERROR', 'reason_codes': ['registry_invalid']}
