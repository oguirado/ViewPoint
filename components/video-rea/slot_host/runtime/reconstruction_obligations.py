# SPDX-License-Identifier: AGPL-3.0-only
"""Evaluate reviewed obligations against an admitted offline evidence bundle."""

import hashlib
from collections import Counter, defaultdict
from typing import Protocol

from video_rea.expose.wire import MAX_LINE_BYTES, loads

from ..projections.schema import PublicationError, digest, require

TOOL_ID = 'reconstruction.obligations'
SUMMARY = 'Evaluate reviewed reconstruction obligations against admitted evidence.'
KEYWORDS = ('obligation ledger', 'reconstruction coverage', 'required cases')
_BUNDLE_SCHEMA = 'rea.reconstruction_obligation_evidence/v1'
_LEDGER_SCHEMA = 'rea.reconstruction_obligation_ledger/v1'
_MAX_OBLIGATIONS = 32
_CASES = ('positive', 'negative', 'malformed', 'cancellation', 'teardown')
_ORIGINAL_AUTHORITIES = {
    'static': ('static',),
    'runtime': ('runtime', 'process', 'external'),
    'process': ('process',),
    'external': ('external',),
}
_PROOF_AUTHORITIES = {
    'unit': ('unit', 'integration', 'protocol', 'renderer', 'packaged-process',
             'native-abi', 'live-observation', 'external'),
    'integration': ('integration', 'protocol', 'renderer', 'packaged-process',
                    'native-abi', 'live-observation'),
    'protocol': ('protocol', 'packaged-process', 'live-observation'),
    'renderer': ('renderer', 'packaged-process', 'live-observation'),
    'packaged-process': ('packaged-process', 'live-observation'),
    'native-abi': ('native-abi',),
    'live-observation': ('live-observation',),
    'external': ('external',),
}
_STATUSES = ('open', 'closed', 'failed', 'unknown')
_REF = {'type': 'string', 'maxLength': 128, 'pattern': r'^[A-Za-z0-9][A-Za-z0-9._:@/+-]*(?![\s\S])'}
_SHA = {'type': 'string', 'pattern': r'^[0-9a-f]{64}(?![\s\S])'}
_VERSION = {'type': 'integer', 'minimum': 1, 'maximum': (1 << 53) - 1}
_CASE = {'enum': list(_CASES)}


def _closed(properties, *, optional=()):
    return {'type': 'object', 'additionalProperties': False, 'properties': properties,
            'required': [key for key in properties if key not in optional]}


def _array(items, limit, *, distinct=False):
    return {'type': 'array', 'items': items, 'maxItems': limit, 'uniqueItems': distinct}


_REVISION = {'obligation_id': _REF, 'obligation_version': _VERSION}
_REVIEW = _closed({
    **_REVISION,
    'required_case_kinds': {**_array(_CASE, len(_CASES), distinct=True), 'minItems': 1},
    'required_original_authority': {'enum': list(_ORIGINAL_AUTHORITIES)},
    'required_fixture_authority': {'enum': list(_PROOF_AUTHORITIES)},
    'required_verifier_authority': {'enum': list(_PROOF_AUTHORITIES)},
    'requires_parser_type': {'type': 'boolean'},
    'dependency_obligation_ids': _array(_REF, _MAX_OBLIGATIONS, distinct=True),
    'residual_unknown_ids': _array(_REF, _MAX_OBLIGATIONS, distinct=True),
    'unavailable_authority': _array(_REF, _MAX_OBLIGATIONS, distinct=True),
    'disposition': {'enum': ['active', 'blocked', 'out-of-scope']},
}, optional=('requires_parser_type', 'dependency_obligation_ids', 'residual_unknown_ids',
             'unavailable_authority', 'disposition'))
_PARSER = _closed({key: _REF for key in ('parser', 'schema', 'domain_type')})
_ORIGINAL = _closed({'kind': _CASE, 'evidence_ref': _REF})
_FIXTURE = _closed({'fixture_id': _REF, 'case_kind': _CASE, 'evidence_ref': _REF})
_VERIFIER = _closed({
    'verifier_id': _REF, 'claim_id': _REF,
    'enumerated_obligation_ids': _array(_REF, _MAX_OBLIGATIONS, distinct=True),
    'result_evidence_ref': _REF,
}, optional=('result_evidence_ref',))
_BINDING = _closed({
    'obligation_id': _REF, 'owner_sha256': _SHA, 'parser_type': _PARSER,
    'original_cases': _array(_ORIGINAL, 16, distinct=True),
    'fixtures': _array(_FIXTURE, 16, distinct=True), 'verifier': _VERIFIER,
}, optional=('parser_type', 'verifier'))
_CONTRADICTION = _closed({**_REVISION, 'evidence_ref': _REF})
_SUBJECT = {'boundary_id': _REF, 'evidence_bundle_ref': _REF}
ARGUMENT = _closed({
    **_SUBJECT,
    'reviewed_obligations': _array(_REVIEW, _MAX_OBLIGATIONS),
    'bindings': _array(_BINDING, 2 * _MAX_OBLIGATIONS),
    'contradictions': _array(_CONTRADICTION, 2 * _MAX_OBLIGATIONS, distinct=True),
})

_OUTCOME = {'enum': ['pass', 'fail', 'unknown', 'skipped', 'unsupported']}
_PROOF = {**_REVISION, 'evidence_ref': _REF, 'owner_sha256': _SHA,
          'authority': {'enum': list(_PROOF_AUTHORITIES)}, 'outcome': _OUTCOME}
_BUNDLE = _closed({
    'schema': {'const': _BUNDLE_SCHEMA}, **_SUBJECT,
    'reviewed_obligations': _array(_REVIEW, _MAX_OBLIGATIONS),
    'originals': _array(_closed({**_REVISION, **_ORIGINAL['properties'],
                                'authority': {'enum': list(_ORIGINAL_AUTHORITIES)}}), 512),
    'fixtures': _array(_closed({**_PROOF, **_FIXTURE['properties']}), 512),
    'verifiers': _array(_closed({**_PROOF, 'verifier_id': _REF, 'claim_id': _REF}), 512),
    'contradictions': _array(_CONTRADICTION, 512),
})
_DIAGNOSTICS = _array({'type': 'string', 'maxLength': 192}, 128, distinct=True)
RESULT = _closed({
    'obligations': _array(_closed({
        'id': _REF, 'status': {'enum': list(_STATUSES)},
        'required_case_kinds': _REVIEW['properties']['required_case_kinds'],
        'authority_gaps': _DIAGNOSTICS, 'unknowns': _DIAGNOSTICS,
    }), _MAX_OBLIGATIONS),
    'closure_digest': _SHA,
    'summary': _closed({
        'status': {'enum': ['complete', 'incomplete', 'unknown']},
        **{status: {'type': 'integer', 'minimum': 0, 'maximum': _MAX_OBLIGATIONS} for status in _STATUSES},
        'outstanding_obligation_ids': _array(_REF, _MAX_OBLIGATIONS, distinct=True),
    }),
})


class EvidenceUnavailable(RuntimeError):
    """The authenticated fixture bundle could not be admitted."""


class FixtureObligationHost(Protocol):
    def resolve_reconstruction_evidence(self, evidence_bundle_ref: str, *, boundary_id: str,
                                        request_id: str, max_bytes: int) -> bytes | None:
        """Return the authorized normalized bytes for this boundary and request.

        The fixture host supplies the complete reviewed inventory and only facts
        verified from retained records. Original authority requires an observed
        record. Process authority requires a validated process capture. Proof
        authority and outcome come from the admitted proof and its provenance.
        """
        ...


def _read_bundle(payload, request_id, host: FixtureObligationHost | None):
    resolve = getattr(host, 'resolve_reconstruction_evidence', None)
    if not callable(resolve):
        raise EvidenceUnavailable('owner-pending: T04')
    try:
        raw = resolve(payload['evidence_bundle_ref'], boundary_id=payload['boundary_id'],
                      request_id=request_id, max_bytes=MAX_LINE_BYTES)
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_LINE_BYTES:
            raise ValueError('Evidence bundle is unavailable or exceeds its byte limit.')
        bundle = loads(raw)
        require(bundle, _BUNDLE, 'Admitted reconstruction evidence')
        if any(bundle[key] != payload[key] for key in _SUBJECT):
            raise ValueError('Evidence bundle belongs to a different subject.')
    except (OSError, TypeError, ValueError, RecursionError) as error:
        raise EvidenceUnavailable('The host could not resolve admitted reconstruction evidence.') from error
    return bundle, hashlib.sha256(raw).hexdigest()


def _by_obligation(records):
    grouped = defaultdict(list)
    for record in records:
        grouped[record['obligation_id']].append(record)
    return grouped


def _proof_gaps(label, ref, claims, required_authority, gaps):
    if ref is None or not claims:
        gaps.add(f'{label}:missing_or_inapplicable_evidence' + (f':{ref}' if ref else ''))
        return False
    if any(claim['outcome'] == 'fail' for claim in claims):
        gaps.add(f'{label}:failed:{ref}')
        return True
    comparable = [claim for claim in claims if claim['authority'] in _PROOF_AUTHORITIES[required_authority]]
    if not comparable:
        gaps.add(f'{label}:insufficient_authority:{ref}')
    elif any(claim['outcome'] != 'pass' for claim in comparable):
        gaps.add(f'{label}:unavailable_outcome:{ref}')
    return False


def _check_binding(review, binding, admitted, gaps):
    if review.get('requires_parser_type') and 'parser_type' not in binding:
        gaps.add('missing_parser_type')
    failed = False
    for link in binding['original_cases']:
        candidates = [fact for fact in admitted['originals'] if all(fact[key] == value for key, value in link.items())]
        label = f"original:{link['kind']}"
        if not candidates:
            gaps.add(f"{label}:missing_or_inapplicable_evidence:{link['evidence_ref']}")
        elif not any(fact['authority'] in _ORIGINAL_AUTHORITIES[review['required_original_authority']]
                     for fact in candidates):
            gaps.add(f"{label}:insufficient_authority:{link['evidence_ref']}")
    for link in binding['fixtures']:
        candidates = [fact for fact in admitted['fixtures'] if all(fact[key] == value for key, value in link.items())]
        failed |= _proof_gaps(f"fixture:{link['case_kind']}", link['evidence_ref'], candidates,
                              review['required_fixture_authority'], gaps)
    for kind in review['required_case_kinds']:
        if not any(link['kind'] == kind for link in binding['original_cases']):
            gaps.add(f'original:{kind}:missing_case')
        if not any(link['case_kind'] == kind for link in binding['fixtures']):
            gaps.add(f'fixture:{kind}:missing_case')
    verifier = binding.get('verifier')
    if verifier is None:
        gaps.add('missing_verifier')
    else:
        if review['obligation_id'] not in verifier['enumerated_obligation_ids']:
            gaps.add('verifier:does_not_enumerate_obligation')
        ref = verifier.get('result_evidence_ref')
        claims = [fact for fact in admitted['verifiers']
                  if fact['evidence_ref'] == ref and fact['verifier_id'] == verifier['verifier_id']
                  and fact['claim_id'] == verifier['claim_id']]
        failed |= _proof_gaps('verifier', ref, claims, review['required_verifier_authority'], gaps)
    return failed


def _evaluate_obligation(review, bindings, facts, contradictions):
    gaps = set()
    unknowns = {f'residual_unknown:{ref}' for ref in review.get('residual_unknown_ids', ())}
    unknowns.update(f'unavailable_authority:{ref}' for ref in review.get('unavailable_authority', ()))
    revision = review['obligation_version']
    current = {kind: [fact for fact in records if fact['obligation_version'] == revision]
               for kind, records in facts.items()}
    refs = {fact['evidence_ref'] for fact in current['contradictions']}
    failed = bool(refs)
    if failed:
        gaps.add('admitted_contradiction')
    if any(link['evidence_ref'] not in refs or link['obligation_version'] != revision
           for link in contradictions):
        unknowns.add('unresolved_contradiction')
    disposition = review.get('disposition', 'active')
    if disposition != 'active':
        unknowns.add(f'disposition:{disposition}')
    if not bindings:
        gaps.add('missing_owner')
    elif len(bindings) != 1:
        gaps.add('ambiguous_owner_binding')
        failed = True
    else:
        binding = bindings[0]
        for kind in ('fixtures', 'verifiers'):
            current[kind] = [fact for fact in current[kind] if fact['owner_sha256'] == binding['owner_sha256']]
            # Admitted failures count even when the binding omits their links.
            if any(fact['outcome'] == 'fail' for fact in current[kind]):
                gaps.add('admitted_failed_proof')
                failed = True
        failed |= _check_binding(review, binding, current, gaps)
    if failed:
        status = 'failed'
    elif gaps:
        status = 'open'
    elif unknowns:
        status = 'unknown'
    else:
        status = 'closed'
    return {'id': review['obligation_id'], 'status': status,
            'required_case_kinds': [kind for kind in _CASES if kind in review['required_case_kinds']],
            'authority_gaps': sorted(gaps), 'unknowns': sorted(unknowns)}


def _apply_dependencies(reviews, rows):
    # An independent closed leaf is the seed. Cycles and missing edges stay open.
    pending = {key for key, row in rows.items() if row['status'] == 'closed'}
    closed = set()
    while ready := {
        key for key in pending if closed.issuperset(reviews[key].get('dependency_obligation_ids', ()))
    }:
        closed.update(ready)
        pending.difference_update(ready)
    for key, row in rows.items():
        missing = set(reviews[key].get('dependency_obligation_ids', ())) - closed
        if missing and row['status'] == 'closed':
            row['status'] = 'open'
        row['authority_gaps'] = sorted([*row['authority_gaps'], *(f'dependency:{ref}' for ref in missing)])


def evaluate(payload, *, request_id, host=None):
    """Report closure for one admitted boundary without launching anything."""
    require(payload, ARGUMENT, 'Reconstruction obligation request')
    bundle, bundle_sha256 = _read_bundle(payload, request_id, host)
    admitted = _by_obligation(bundle['reviewed_obligations'])
    if any(len(entries) != 1 for entries in admitted.values()):
        raise EvidenceUnavailable('The admitted review inventory has conflicting definitions.')
    requested = _by_obligation(payload['reviewed_obligations'])
    if requested != admitted:
        raise PublicationError('SCHEMA_INVALID', 'The reviewed obligations differ from the admitted inventory.')

    bindings = _by_obligation(payload['bindings'])
    contradictions = _by_obligation(payload['contradictions'])
    if (bindings.keys() | contradictions.keys()) - admitted.keys():
        raise PublicationError('SCHEMA_INVALID', 'An obligation link is outside the admitted inventory.')
    evidence = {kind: _by_obligation(bundle[kind]) for kind in ('originals', 'fixtures', 'verifiers', 'contradictions')}
    reviews = {key: admitted[key][0] for key in sorted(admitted)}
    rows = {key: _evaluate_obligation(
        review, bindings.get(key, ()), {kind: groups.get(key, ()) for kind, groups in evidence.items()},
        contradictions.get(key, ()),
    ) for key, review in reviews.items()}
    _apply_dependencies(reviews, rows)
    outstanding = [key for key, row in rows.items() if row['status'] != 'closed']
    counts = Counter(row['status'] for row in rows.values())
    summary = {'status': ('incomplete' if outstanding else 'complete') if rows else 'unknown',
               **{status: counts[status] for status in _STATUSES},
               'outstanding_obligation_ids': outstanding}
    result = {'obligations': list(rows.values()), 'summary': summary}
    result['closure_digest'] = digest({
        'schema': _LEDGER_SCHEMA, 'evidence_bundle_sha256': bundle_sha256, 'request': payload, 'result': result,
    })
    return result
