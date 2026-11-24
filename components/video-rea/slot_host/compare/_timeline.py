# SPDX-License-Identifier: AGPL-3.0-only
"""Check pinned event timelines with exact source-time arithmetic."""

import hashlib
import json
from collections import Counter
from fractions import Fraction
from math import lcm

from replay_compare import compare_replay_events

from ._model import (
    Finding, Rejected, check_time_base, divergence_times, exact_ms,
    is_digest, is_identifier, unsigned,
)


_REQUIRED = {'event_id', 'kind', 'input_digest', 't_ms'}
_ALLOWED = _REQUIRED | {'occurrence', 't_ticks'}
_MAX_SAFE_INTEGER = (1 << 53) - 1


def _events(side, policy):
    record = side.record
    if (type(record) is not dict or type(record.get('events')) is not list
            or record.get('canonicalization') != 'rea.timeline_digest/v1'
            or not is_digest(record.get('timeline_sha256'))):
        raise Rejected('timeline_invalid', 'The timeline has no valid digest declaration or event array.')
    unscoped = []
    base = check_time_base(record['time_base']) if 'time_base' in record else None
    identities = set()
    counters = Counter()
    scoped = {}
    previous = None
    for item in record['events']:
        if (type(item) is not dict or not _REQUIRED <= item.keys() <= _ALLOWED
                or not is_identifier(item.get('event_id'))
                or type(item.get('kind')) is not str or not 1 <= len(item['kind']) <= 128
                or not is_digest(item.get('input_digest'))):
            raise Rejected('timeline_invalid', 'An event has an invalid identity, kind, digest or shape.')
        for name in item.keys() & {'t_ms', 't_ticks', 'occurrence'}:
            if not unsigned(item[name]) or item[name] > _MAX_SAFE_INTEGER:
                raise Rejected('timeline_invalid', f'Event {name} is outside the lossless JCS integer range.')
        event_id = item['event_id']
        if event_id in identities:
            raise Rejected('timeline_invalid', f'Duplicate event id {event_id}.')
        identities.add(event_id)
        if 't_ticks' in item:
            if base is None:
                raise Rejected('missing_clock_binding', 'Event ticks require a verified time base.')
            time = Fraction(item['t_ticks'] * base[0] * 1000, base[1])
            if time.numerator // time.denominator != item['t_ms']:
                raise Rejected('timeline_invalid', f'Event {event_id} has an inconsistent tick projection.')
        elif policy.integer_milliseconds is True:
            time = Fraction(item['t_ms'])
        else:
            raise Rejected('missing_clock_binding', f'Event {event_id} has no exact source ticks.')
        if previous is not None and time < previous:
            raise Rejected('timeline_invalid', f'Event {event_id} decreases source time.')
        previous = time
        group = item['kind'], item['input_digest']
        ordinal = counters[group]
        if 'occurrence' in item and item['occurrence'] != ordinal:
            raise Rejected('timeline_invalid', f'Event {event_id} has an inconsistent occurrence ordinal.')
        counters[group] += 1
        if item['kind'] in policy.kinds:
            scoped[(*group, ordinal)] = (event_id, time)
        else:
            unscoped.append({'event_id': event_id, 'kind': item['kind'],
                             'input_digest': item['input_digest'], 'occurrence': ordinal})

    # Closed ASCII-key objects and safe nonnegative integers admit this JCS encoding.
    try:
        encoded = json.dumps(record['events'], ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (UnicodeError, ValueError) as error:
        raise Rejected('timeline_invalid', 'The events do not have a lossless JCS encoding.') from error
    if hashlib.sha256(encoded).hexdigest() != record['timeline_sha256']:
        raise Rejected('timeline_invalid', 'The timeline digest failed recomputation.')
    return scoped, unscoped


def _shared_identity(left, right):
    return type(left) is str and type(right) is str and bool(left.strip()) and left == right


def _verify_clocks(reference, candidate):
    clocks = tuple(side.record.get('clock_source') for side in (reference, candidate))
    origins = tuple(side.record.get('origin') for side in (reference, candidate))
    if 'wall' in clocks:
        raise Rejected('clock_source_unsuitable', 'Wall clocks cannot establish event timing.')
    if any(clock not in ('pts', 'monotonic') for clock in clocks):
        raise Rejected('missing_clock_binding', 'A timeline lacks a supported clock source.')
    if any(origin not in ('episode_start', 'recording_start', 'first_input') for origin in origins):
        raise Rejected('missing_clock_binding', 'A timeline lacks a supported origin.')
    if origins[0] != origins[1]:
        raise Rejected('origin_mismatch', 'The timelines use different anchoring rules.')
    if reference.clock_verified is not True or candidate.clock_verified is not True:
        raise Rejected('missing_clock_binding', 'The host has no verified clock binding for both sides.')
    common_anchor = _shared_identity(reference.common_anchor, candidate.common_anchor)
    if clocks[0] != clocks[1] and not common_anchor:
        raise Rejected('clock_source_mismatch', 'The different clock sources have no verified bridge.')
    if not common_anchor and not _shared_identity(reference.clock_domain, candidate.clock_domain):
        raise Rejected('missing_clock_binding', 'The timelines have no verified common coordinate system.')


def _abstain(reason, detail, diagnostics=None):
    return Finding('timeline', 'event_timeline', 'abstain', detail, reason,
                   diagnostics=diagnostics)


def compare_timeline(reference, candidate, policy):
    if reference is None:
        return _abstain('missing_reference_coverage', 'No reference timeline was retained.')
    is_empty = type(reference.record) is dict and reference.record.get('events') == []
    if candidate is None:
        if is_empty:
            raise Rejected('coverage_insufficient', 'No candidate timeline was retained.')
        return _abstain('coverage_insufficient', 'No candidate timeline was retained.')
    try:
        ref, ref_unscoped = _events(reference, policy)
        cand, cand_unscoped = _events(candidate, policy)
        _verify_clocks(reference, candidate)
    except Rejected as error:
        if is_empty:
            raise
        return _abstain(error.reason, str(error))

    reference_complete = reference.complete is True
    candidate_complete = candidate.complete is True
    reference_ordinal_bound = reference_complete or reference.ordinal_prefix_verified is True
    candidate_ordinal_bound = candidate_complete or candidate.ordinal_prefix_verified is True
    ref_kinds = {key[0] for key in ref}
    cand_kinds = {key[0] for key in cand}
    reference_covered = reference_complete and bool(ref) and policy.required_kinds <= ref_kinds
    candidate_covered = candidate_complete and policy.required_kinds <= cand_kinds
    diagnostics = {
        'reference_count': len(ref), 'candidate_count': len(cand),
        'reference_complete': reference_complete, 'candidate_complete': candidate_complete,
        'reference_missing_kinds': sorted(policy.required_kinds - ref_kinds),
        'candidate_missing_kinds': sorted(policy.required_kinds - cand_kinds),
        'events': [], 'order_conflicts': [],
        'unscoped_events': ([{'side': 'reference', **row} for row in ref_unscoped]
                            + [{'side': 'candidate', **row} for row in cand_unscoped]),
    }
    if is_empty:
        finding = _abstain('empty_reference', 'The reference timeline has no events.', diagnostics)
        raise Rejected(finding.reason, finding.detail, finding=finding)
    if not ref or not reference_ordinal_bound or not candidate_ordinal_bound:
        reason = 'coverage_insufficient' if ref and reference_ordinal_bound else 'missing_reference_coverage'
        return _abstain(reason, 'Required event occurrences cannot be bound.', diagnostics)

    # Give the existing timing oracle a common integer subdivision of
    # microseconds so the exact source-time boundaries survive the adapter.
    keys = sorted(ref.keys() | cand.keys())
    key_by_id = {str(index): key for index, key in enumerate(keys)}
    id_by_key = {key: str(index) for index, key in enumerate(keys)}
    scale = 1000
    for trace in (ref, cand):
        for _, time in trace.values():
            scale = lcm(scale, time.denominator)

    def adapter_trace(trace):
        return [{'event_id': id_by_key[key], 'time_us': int(time * scale)}
                for key, (_, time) in trace.items()]

    report = compare_replay_events(adapter_trace(ref), adapter_trace(cand),
                                   late_tolerance_us=policy.late_tolerance_ms * scale)
    labels = {
        'pass': 'match', 'early': 'event_early', 'late': 'event_late',
        'missing': 'event_missing', 'unexpected': 'event_extra',
    }
    order = {}
    for conflict in report['order_conflicts']:
        earlier = key_by_id[conflict['earlier_reference_event_id']]
        later = key_by_id[conflict['later_reference_event_id']]
        order[later] = ref[earlier][0]
        diagnostics['order_conflicts'].append({
            'earlier_reference_event_id': ref[earlier][0],
            'later_reference_event_id': ref[later][0],
        })

    first = None
    rows = {key_by_id[row['event_id']]: row for row in report['events']}
    for key in dict.fromkeys((*ref, *cand)):
        ref_id, ref_time = ref.get(key, (None, None))
        cand_id, cand_time = cand.get(key, (None, None))
        outcome = labels[rows[key]['verdict']]
        if ((outcome == 'event_missing' and not candidate_complete)
                or (outcome == 'event_extra' and not reference_covered)):
            outcome = 'unknown'
        if outcome == 'match' and key in order:
            outcome = 'order_inverted'
        diagnostics['events'].append({
            'reference_event_id': ref_id, 'candidate_event_id': cand_id,
            'kind': key[0], 'input_digest': key[1], 'occurrence': key[2],
            'reference_ms_exact': exact_ms(ref_time), 'candidate_ms_exact': exact_ms(cand_time),
            'delta_ms_exact': exact_ms(cand_time - ref_time) if ref_time is not None and cand_time is not None else None,
            'outcome': outcome,
        })
        if first is None and outcome not in ('match', 'unknown'):
            divergence = {'event_id': ref_id or cand_id, 'occurrence': key[2],
                          **divergence_times(ref_time, cand_time)}
            if outcome == 'order_inverted':
                divergence['note'] = f'Reference precedence after {order[key]} was reversed or collapsed.'
            detail = 'timeline.late_beyond_tolerance: event_late' if outcome == 'event_late' else f'timeline.{outcome}'
            first = Finding('timeline', 'event_timeline', 'mismatch', detail,
                            divergence=divergence, diagnostics=diagnostics)
    if first is not None:
        return first
    if not reference_covered:
        return _abstain('missing_reference_coverage', 'The reference capture lacks required coverage.', diagnostics)
    if not candidate_covered:
        return _abstain('coverage_insufficient', 'The candidate capture lacks required coverage.', diagnostics)
    return Finding('timeline', 'event_timeline', 'match', 'All required event occurrences agree.',
                   diagnostics=diagnostics)
