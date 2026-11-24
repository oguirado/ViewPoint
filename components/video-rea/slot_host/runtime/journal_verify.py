# SPDX-License-Identifier: AGPL-3.0-only
"""Offline verification of retained REA input journals.

rea.input_journal/v1 is bounded UTF-8 JSONL with one closed object per line.
step_index is one-based and parent_hash names the previous step_hash. The
genesis digest is 64 ASCII zeroes. The recurrence is SHA-256 over the REA
domain and four fields, each with an eight-byte big-endian length prefix:
previous digest in lowercase hex, decimal step index, parent digest in
lowercase hex, and JCS of the record excluding the three chain fields.

Event source time and optional presentation time have separate bindings.
The trusted seal supplies both the expected root and the ordered inventory
of event IDs. A root mismatch by itself cannot locate a bad record.
"""

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from typing import Protocol

from ..projections.schema import accepts, jcs_bytes, require

TOOL_ID = 'journal.verify'
SUMMARY = 'Verify a retained input journal against its grammar and trusted seal.'
KEYWORDS = ('journal grammar', 'Merkle attestation', 'event stream verification')
GRAMMAR = 'rea.input_journal/v1'
MAX_JOURNAL_BYTES = 1024 * 1024
_MAX_STEP_BYTES = 8192
_MAX_STEPS = 1024
_GENESIS = '0' * 64
_DOMAIN = b'rea/input-journal/step/v1'
_RECEIPT_PREFIX = 'fixture:journal.verify:sha256:'

_REF = {'type': 'string', 'maxLength': 512, 'pattern': r'^[A-Za-z0-9][A-Za-z0-9._:@/+-]*(?![\s\S])'}
_EVENT_ID = {**_REF, 'maxLength': 256}
_SHA = {'type': 'string', 'pattern': r'^[0-9a-f]{64}(?![\s\S])'}
_UINT = {'type': 'integer', 'minimum': 0, 'maximum': (1 << 53) - 1}
_POSITIVE = {**_UINT, 'minimum': 1}
_STEP_INDEX = {**_POSITIVE, 'maximum': _MAX_STEPS}


def _closed(properties, *required):
    return {'type': 'object', 'additionalProperties': False, 'properties': properties,
            'required': list(required or properties)}


ARGUMENT = _closed({
    'journal_ref': _REF,
    'trusted_seal_ref': _REF,
    'grammar_profile': {'const': GRAMMAR},
    'diagnostic_policy': {'enum': ['fail-closed', 'record-and-continue'], 'default': 'fail-closed'},
}, 'journal_ref', 'trusted_seal_ref', 'grammar_profile')

RESULT = _closed({
    'structure_verdict': {'enum': ['valid', 'invalid']},
    'seal_verdict': {'enum': ['bound', 'mismatch', 'unverified']},
    'completeness': {'enum': ['complete', 'incomplete', 'unknown']},
    'violation_steps': {'type': 'array', 'items': _STEP_INDEX},
    'affected_evidence_ids': {'type': 'array', 'items': _EVENT_ID},
    'receipt_ref': {**_REF, 'pattern': r'^fixture:journal\.verify:sha256:[0-9a-f]{64}(?![\s\S])'},
})

_TIME_BASE = _closed({'num': _POSITIVE, 'den': _POSITIVE})
_EVENT = _closed({
    'event_id': _EVENT_ID, 'kind': {'type': 'string', 'minLength': 1, 'maxLength': 128},
    'input_digest': _SHA, 't_ms': _UINT, 't_ticks': _UINT, 'occurrence': _UINT,
}, 'event_id', 'kind', 'input_digest', 't_ms')
_PRESENTATION = _closed({'pts_ms': _UINT, 'pts_ticks': _UINT, 'time_base': _TIME_BASE}, 'pts_ms')
_STEP = _closed({
    'step_index': _STEP_INDEX, 'parent_hash': _SHA, 'step_hash': _SHA,
    'event': _EVENT, 'event_time_base': _TIME_BASE, 'presentation': _PRESENTATION,
}, 'step_index', 'parent_hash', 'step_hash', 'event')


@dataclass(frozen=True)
class TrustedSeal:
    """The fixture host's independently retained expectation for one journal."""

    journal_ref: str
    grammar_profile: str
    root_sha256: str
    evidence_ids: tuple[str, ...]


class JournalUnavailable(RuntimeError):
    """The host could not supply or retain authorized fixture evidence."""


class FixtureJournalHost(Protocol):
    """Offline ports. The production retention and authorization remain owner-pending."""

    def resolve_journal(self, journal_ref: str, trusted_seal_ref: str, *,
                        request_id: str, max_bytes: int) -> tuple[bytes, TrustedSeal] | None:
        """Resolve both opaque references through the authenticated fixture host."""
        ...

    def retain_journal_verification(self, request_id: str, receipt_ref: str,
                                    receipt_bytes: bytes) -> bytes | None:
        """Store the exact fixture receipt and return its authorized readback bytes."""
        ...


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate journal field.')
        result[key] = value
    return result


def _reject_number(_):
    raise ValueError('Journal numbers must be JSON integers.')


def _exact_time(record, milliseconds, ticks, base):
    if (ticks in record) != (base is not None):
        raise ValueError('Exact ticks require their own time base.')
    if base is None:
        return record[milliseconds]
    exact = Fraction(record[ticks] * base['num'] * 1000, base['den'])
    if exact // 1 != record[milliseconds]:
        raise ValueError('Millisecond projection disagrees with source ticks.')
    return exact


def _decode_step(line):
    if len(line) > _MAX_STEP_BYTES:
        raise ValueError('Journal line exceeds its byte limit.')
    record = json.loads(line.decode('utf-8'), object_pairs_hook=_unique_object,
                        parse_float=_reject_number, parse_constant=_reject_number)
    require(record, _STEP, 'Journal step')
    fields = {key: value for key, value in record.items()
              if key not in ('step_index', 'parent_hash', 'step_hash')}
    encoded = jcs_bytes(fields)
    time = _exact_time(record['event'], 't_ms', 't_ticks', record.get('event_time_base'))
    if 'presentation' in record:
        presentation = record['presentation']
        _exact_time(presentation, 'pts_ms', 'pts_ticks', presentation.get('time_base'))
    return record, encoded, time


def _step_digest(previous, record, encoded):
    fields = (previous.encode('ascii'), str(record['step_index']).encode('ascii'),
              record['parent_hash'].encode('ascii'), encoded)
    chain = hashlib.sha256(_DOMAIN)
    for field in fields:
        chain.update(len(field).to_bytes(8, 'big'))
        chain.update(field)
    return chain.hexdigest()


def _inventory_findings(observed, expected):
    """Locate missing, unexpected and reordered events in the trusted inventory."""
    observed_ids = set(observed)
    expected_ids = set(expected)
    findings = {step: [evidence_id] for step, evidence_id in enumerate(expected, 1)
                if evidence_id not in observed_ids}
    observed_steps = {}
    for step, evidence_id in enumerate(observed, 1):
        if evidence_id in expected_ids:
            observed_steps.setdefault(evidence_id, step)
        else:
            findings.setdefault(step, [])
    # Compare shared IDs in relative order while keeping their original step numbers.
    expected_present = (evidence_id for evidence_id in expected if evidence_id in observed_ids)
    for (actual, step), wanted in zip(observed_steps.items(), expected_present, strict=True):
        if actual != wanted:
            findings.setdefault(step, []).append(wanted)
    return findings


def _inspect(raw, seal, *, diagnostics):
    lines = raw.removesuffix(b'\n').split(b'\n', _MAX_STEPS) if raw else []
    if len(lines) > _MAX_STEPS:
        return {'structure_verdict': 'invalid', 'seal_verdict': 'unverified',
                'completeness': 'unknown', 'violation_steps': [], 'affected_evidence_ids': []}, None

    observed = []
    issues = {}
    previous_hash = _GENESIS
    previous_time = None
    occurrences = Counter()
    evidence_ids = set(seal.evidence_ids)
    seen = set()
    chain_mismatch = False
    proved_incomplete = len(lines) < len(seal.evidence_ids)
    for step, line in enumerate(lines, 1):
        try:
            record, encoded, time = _decode_step(line)
        except (ValueError, RecursionError):
            issues[step] = []
            previous_hash = None
            continue

        event = record['event']
        evidence_id = event['event_id']
        observed.append(evidence_id)
        group = (event['kind'], event['input_digest'])
        ordinal = occurrences[group]
        if (record['step_index'] != step or evidence_id in seen
                or (previous_time is not None and time < previous_time)
                or event.get('occurrence', ordinal) != ordinal):
            issues[step] = [evidence_id]
        seen.add(evidence_id)
        previous_time = time
        occurrences[group] += 1

        if previous_hash is not None:
            computed = _step_digest(previous_hash, record, encoded)
            if record['parent_hash'] != previous_hash or record['step_hash'] != computed:
                issues[step] = [evidence_id]
                chain_mismatch = True
            previous_hash = computed

    all_decoded = len(observed) == len(lines)
    inventory_mismatch = all_decoded and tuple(observed) != seal.evidence_ids
    if inventory_mismatch:
        issues.update(_inventory_findings(observed, seal.evidence_ids))
    missing = evidence_ids - seen
    if proved_incomplete or (all_decoded and missing):
        completeness = 'incomplete'
    else:
        completeness = 'unknown' if missing else 'complete'

    if chain_mismatch or inventory_mismatch or proved_incomplete or (
        previous_hash is not None and previous_hash != seal.root_sha256
    ):
        seal_verdict = 'mismatch'
    elif issues or previous_hash is None:
        seal_verdict = 'unverified'
    else:
        seal_verdict = 'bound'

    steps = sorted(issues)
    if diagnostics == 'fail-closed':
        steps = steps[:1]
    affected = list(dict.fromkeys(evidence_id for step in steps for evidence_id in issues[step]
                                  if evidence_id in evidence_ids))
    return {
        'structure_verdict': 'valid' if lines and not issues and not inventory_mismatch else 'invalid',
        'seal_verdict': seal_verdict, 'completeness': completeness,
        'violation_steps': steps, 'affected_evidence_ids': affected,
    }, previous_hash


def _valid_seal(seal, payload):
    return (type(seal) is TrustedSeal and seal.journal_ref == payload['journal_ref']
            and seal.grammar_profile == payload['grammar_profile'] and accepts(seal.root_sha256, _SHA)
            and type(seal.evidence_ids) is tuple and 0 < len(seal.evidence_ids) <= _MAX_STEPS
            and all(accepts(evidence_id, _EVENT_ID) for evidence_id in seal.evidence_ids)
            and len(set(seal.evidence_ids)) == len(seal.evidence_ids))


def verify(payload, *, request_id, host=None):
    """Verify and retain a report through the request's authorized offline host."""
    require(payload, ARGUMENT, 'Journal verification payload')
    if host is None:
        raise JournalUnavailable('Journal retention unavailable (owner-pending: T04).')
    try:
        resolved = host.resolve_journal(payload['journal_ref'], payload['trusted_seal_ref'],
                                        request_id=request_id, max_bytes=MAX_JOURNAL_BYTES)
    except Exception as error:
        raise JournalUnavailable('Journal or trusted seal could not be resolved.') from error
    if type(resolved) is not tuple or len(resolved) != 2:
        raise JournalUnavailable('Journal or trusted seal is unavailable.')
    raw, seal = resolved
    if type(raw) is not bytes or len(raw) > MAX_JOURNAL_BYTES or not _valid_seal(seal, payload):
        raise JournalUnavailable('Journal or seal exceeds its limits or lacks trusted binding.')

    policy = payload.get('diagnostic_policy', 'fail-closed')
    result, computed_root = _inspect(raw, seal, diagnostics=policy)
    receipt = jcs_bytes({
        'schema': 'rea.journal_verification_receipt/v1', 'request_id': request_id,
        'journal_ref': payload['journal_ref'], 'trusted_seal_ref': payload['trusted_seal_ref'],
        'grammar_profile': seal.grammar_profile, 'diagnostic_policy': policy,
        'journal_sha256': hashlib.sha256(raw).hexdigest(), 'computed_root_sha256': computed_root,
        'trusted_root_sha256': seal.root_sha256, 'trusted_evidence_ids': list(seal.evidence_ids),
        'result': result,
    })
    receipt_ref = _RECEIPT_PREFIX + hashlib.sha256(receipt).hexdigest()
    try:
        readback = host.retain_journal_verification(request_id, receipt_ref, receipt)
    except Exception as error:
        raise JournalUnavailable('Journal verification receipt could not be retained.') from error
    if type(readback) is not bytes or readback != receipt:
        raise JournalUnavailable('Journal verification receipt lacks exact readback.')
    return {**result, 'receipt_ref': receipt_ref}
