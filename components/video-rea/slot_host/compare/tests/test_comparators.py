# SPDX-License-Identifier: AGPL-3.0-only
"""Independent synthetic host witnesses and comparator regression controls."""

import hashlib
import io
import json
import struct
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace

from pixel_compare import RGB8Frame
from slot_host.compare import behaviour, timeline, tools, visual
from slot_host.compare._model import Behaviour, Comparison, Episode, Media, Policy, SpatialPolicy, Timeline, TimelinePolicy
from slot_host.compare._visual import RecordingCompletion, Vision
from slot_host.compare.visual import _ObserveSource


FRONT = 'ViewPoint_REA'
REQUEST = 'req-fixture-0001'
ATTEMPT = 'attempt:fixture-1'
PAYLOAD = {'reference_ref': 'ep:reference', 'candidate_ref': 'ep:candidate', 'policy_ref': 'policy:fixture'}
BASE = {'num': 1, 'den': 2000}
EVENT_KINDS = frozenset({'key_down', 'pointer_click', 'key_up'})
TIME_POLICY = TimelinePolicy(kinds=EVENT_KINDS, required_kinds=EVENT_KINDS,
                             late_tolerance_ms=33, integer_milliseconds=False)
PIXELS = (bytes.fromhex('001020304050'), bytes.fromhex('60708090a0b0'))
FILE_HEADER = b'P6\n2 1\n255\n'
FRAME_BYTES = len(FILE_HEADER) + 12


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(document):
    return json.dumps(document, sort_keys=True, separators=(',', ':')).encode('ascii')


def event(event_id, ticks, kind, input_bytes):
    return {'event_id': event_id, 't_ticks': ticks, 't_ms': ticks // 2,
            'kind': kind, 'input_digest': digest(input_bytes)}


def event_trace(ticks=(20, 80, 500), *, prefix='e'):
    return [event(f'{prefix}{index}', tick, kind, data)
            for index, (tick, kind, data) in enumerate(zip(ticks, ('key_down', 'pointer_click', 'key_up'),
                                                           (b'press', b'click', b'release'), strict=True), 1)]


def timeline_side(events, *, domain='fixture.clock', anchor=None, clock='pts'):
    record = {'events': events, 'timeline_sha256': digest(encoded(events)),
              'canonicalization': 'rea.timeline_digest/v1', 'clock_source': clock,
              'origin': 'episode_start', 'time_base': dict(BASE)}
    return Timeline(record=record, clock_verified=True, complete=True, ordinal_prefix_verified=True,
                    clock_domain=domain, common_anchor=anchor)


def tagged(value):
    return {'kind': 'value', 'payload': value}


def cases(value):
    return Behaviour(cases=({'case_id': 'press-1', 'input_digest': digest(b'press'),
                             'observation': tagged(value)},), complete=True)


def comparison(checks, *, ref=None, cand=None, **overrides):
    policy = Policy(id='host.compare', version='1', sha256=digest(b'host policy v1'),
                    comparator_id='host.compare', comparator_version='1', checks=checks,
                    determinism_modes=frozenset({'uncontrolled'}), repeatable_effects_required=False,
                    contrast_ids=('contrast-1',), negative_control_ids=('neg-1',), timeline=TIME_POLICY,
                    waiver_source='fixture.approval', waiver_scope='press-1:return_value')
    tape = digest(b'input tape')
    defaults = dict(policy=policy, reference=ref or Episode(tape_sha256=tape),
                    candidate=cand or Episode(tape_sha256=tape), exchange_valid=True, evidence_bound=True,
                    custody_verified=True, lineage_verified=True, fresh=True, determinism_mode='uncontrolled',
                    effects_repeatable=True, perception_ceiling='none', vision_authorized=False,
                    producer={'kind': 'host_mini_tool', 'id': 'host.compare', 'process_bound': True},
                    oracle_independent=True, contrast_outcomes={'contrast-1': True},
                    negative_controls=({'control_id': 'neg-1', 'executed': True,
                                        'activated': True, 'outcome': 'rejected'},), attempt_id=ATTEMPT)
    return Comparison(**(defaults | overrides))


def event_comparison(reference=None, candidate=None):
    comp = comparison(('timeline',))
    if reference is None:
        reference = timeline_side(event_trace())
    if candidate is None:
        candidate = timeline_side(event_trace(prefix='c'))
    return replace(comp, reference=replace(comp.reference, timeline=reference),
                   candidate=replace(comp.candidate, timeline=candidate))


def behaviour_comparison(actual='on'):
    comp = comparison(('structured',))
    return replace(comp, reference=replace(comp.reference, behaviour=cases('on')),
                   candidate=replace(comp.candidate, behaviour=cases(actual)))


def waiver_for(comp, actual='allowed'):
    return {'ref': 'waiver:press-1', 'case_id': 'press-1', 'input_digest': digest(b'press'),
            'source': comp.policy.waiver_source, 'scope': comp.policy.waiver_scope,
            'policy': comp.policy.pin(), 'actual': tagged(actual), 'reason': 'Approved marker for this occurrence.'}


def process_start(pid):
    with open(f'/proc/{pid}/stat', encoding='utf-8') as stat:
        return int(stat.read(4096).rsplit(')', 1)[1].split()[19])


class Pool:
    def __init__(self, *, fault=None):
        self.fault = fault
        self.owner = None
        self.lifecycle = []
        self.token = 'fixture-lease-token'

    def acquire(self, front, pid, timeout, ttl):
        assert self.owner is None and front == FRONT and pid > 1
        if self.fault == 'timeout':
            self.lifecycle.append('timeout')
            return None
        self.owner = (front, pid, self.token)
        start = process_start(pid)
        grant = {'front': front, 'pid': pid, 'starttime': start, 'lease_id': self.token,
                 'expires_at_monotonic': time.monotonic() + ttl}
        if self.fault == 'expired':
            grant['expires_at_monotonic'] = time.monotonic() - 1
        elif self.fault == 'front':
            grant['front'] = 'incorrect.front'
        elif self.fault == 'starttime':
            grant['starttime'] = start + 1
        elif self.fault == 'incomplete':
            del grant['starttime']
        self.lifecycle.append('granted')
        return grant

    def release(self, front, pid, lease_id):
        assert self.owner == (front, pid, lease_id)
        self.owner = None
        self.lifecycle.append('released')
        return self.fault != 'release'


class VisualFixture:
    """The encoded bytes contain a frame count, tick rate and fixed records.

    The observer reads the header and frame bytes from the verified stream.
    The retained rows below use the fixture's independently declared expected
    order, exact ticks and digests. Changes to a row leave the recording intact.
    """

    def __init__(self, *, candidate_tick=80, candidate_den=2000, candidate_pixels=PIXELS, fault=None):
        self.pool = Pool(fault=fault)
        self.recordings = {}
        self.records = {}
        self.lookup_limit = {}
        self.mutations = {}
        self.calls = []
        self.live_allocations = []
        for side, ticks, den, pixels in (('reference', (0, 80), 2000, PIXELS),
                                         ('candidate', (0, candidate_tick), candidate_den, candidate_pixels)):
            recording = struct.pack('>II', len(ticks), den) + b''.join(
                struct.pack('>I6s', tick, raster) for tick, raster in zip(ticks, pixels, strict=True))
            self.recordings[side] = recording
            self.records[side] = {'recording_sha256': digest(recording), 'container': 'synthetic',
                                  'codec': 'rgb8', 'clock_source': 'pts', 'time_base': {'num': 1, 'den': den},
                                  'frames': [{'frame_index': index, 'pts_ticks': tick, 'pts_ms': tick * 1000 // den,
                                              'decoded_frame_sha256': digest(raster),
                                              'frame_file_sha256': digest(FILE_HEADER + raster)}
                                             for index, (tick, raster) in enumerate(zip(ticks, pixels, strict=True))]}

    def open_recording(self, side):
        assert self.pool.owner is not None
        return io.BytesIO(self.recordings[side])

    @contextmanager
    def observe(self, side, index, *, stream, budget, deadline):
        assert self.pool.owner is not None and time.monotonic() < deadline
        self.calls.append((side, index))
        if side in self.lookup_limit and index >= self.lookup_limit[side]:
            raise KeyError(index)
        stream.seek(0)
        count, den = struct.unpack('>II', stream.read(8))
        if index == count:
            if stream.seek(0, io.SEEK_END) != 8 + 10 * count:
                raise EOFError('The decode is incomplete.')
            yield RecordingCompletion(recording_sha256=digest(self.recordings[side]), frame_count=count)
            return
        stream.seek(8 + 10 * index)
        ticks, = struct.unpack('>I', stream.read(4))
        with budget.reserve(FRAME_BYTES) as reservation:
            self.live_allocations.append(budget.live)
            pixels = stream.read(6)
            if len(pixels) != 6:
                raise EOFError('The raster is incomplete.')
            obs = {'frame_index': index, 'pts_ticks': ticks, 'pts_ms': ticks * 1000 // den,
                   'recording_bytes': self.recordings[side], 'time_base': {'num': 1, 'den': den},
                   'raster': RGB8Frame(2, 1, pixels), 'frame_file_bytes': FILE_HEADER + pixels,
                   'reservation': reservation}
            mutate = self.mutations.get((side, index))
            if mutate is not None:
                mutate(obs)
            yield obs
            del obs, pixels

    def vision(self, *, ceiling=2 * FRAME_BYTES):
        return Vision(pool=self.pool, source=_ObserveSource(self.observe, self.open_recording),
                      front=FRONT, timeout=5, ttl=30, attempt_id=ATTEMPT, decoded_byte_ceiling=ceiling)

    def comparison(self, *, indices=None, combined=None):
        comp = combined or comparison(())
        spatial = SpatialPolicy(decoder_ref='fixture.rgb8.decode', max_mismatch_ppm=0,
                                max_channel_delta=0, region=(0, 0, 2, 1), frame_indices=indices)
        ref = Media(recording_ref='reference', record=self.records['reference'])
        cand = Media(recording_ref='candidate', record=self.records['candidate'])
        return replace(comp, reference=replace(comp.reference, media=ref), candidate=replace(comp.candidate, media=cand),
                       policy=replace(comp.policy, checks=(*comp.policy.checks, 'pts', 'spatial'), spatial=spatial),
                       perception_ceiling='vision', vision_authorized=True)

    def direct(self, **kwargs):
        arguments = {'vision_pool': self.pool, 'front': FRONT, 'attempt_id': ATTEMPT,
                     'decoded_byte_ceiling': 2 * FRAME_BYTES, 'open_recording': self.open_recording,
                     'observe_frame': self.observe}
        return visual.compare(self.records['reference'], self.records['candidate'], **(arguments | kwargs))


class Host:
    """Fixture adapter for the authorized comparator handler boundary."""

    def __init__(self, comp, vision=None, *, artifact_only=False):
        self.comp = comp
        self.vision = vision
        self.artifact_only = artifact_only
        self.seen = []
        self.artifacts = {}
        self.receipts = {}
        self.report = None

    def resolve_comparison(self, tool_id, request_id, **refs):
        self.seen.append((tool_id, request_id, refs, self.comp.policy.pin()))
        return self.comp

    def vision_for(self, tool_id, request_id):
        return self.vision

    def inspect(self, receipt_ref, *, authorized):
        if not authorized:
            raise PermissionError('The receipt is restricted.')
        receipt = self.receipts[receipt_ref]
        if digest(self.artifacts[receipt.artifact_ref]) != receipt.artifact_sha256:
            raise ValueError('The comparison artifact digest disagrees with the receipt.')
        return receipt

    def retain_comparison(self, tool_id, request_id, report):
        self.report = report
        artifact_ref = 'evidence:artifact/' + tool_id
        if self.artifact_only or self.comp.attempt_id is None:
            return artifact_ref
        self.artifacts[artifact_ref] = encoded(report)
        checksum = digest(self.artifacts[artifact_ref])
        receipt_ref = 'evidence:receipt/' + tool_id
        self.receipts[receipt_ref] = tools.VerifiedComparisonReceipt(
            receipt_ref, self.comp.attempt_id, request_id, artifact_ref, checksum)
        return tools.RetainedComparison(artifact_ref, checksum, self.inspect(receipt_ref, authorized=True))

    def call(self, tool_id):
        return tools.handle(tool_id, PAYLOAD, request_id=REQUEST, host=self)


class DirectRegressionTests(unittest.TestCase):
    def test_late_events_do_not_hide_behind_equal_pixels(self):
        refs = event_comparison(candidate=timeline_side(event_trace((20, 240, 500), prefix='c')))
        fixture = VisualFixture()
        report = Host(fixture.comparison(combined=refs), fixture.vision()).call('visual.compare')
        self.assertEqual(report['verdict']['result'], 'mismatch')
        self.assertEqual([(row['kind'], row['outcome']) for row in report['verdict']['checks']],
                         [('event_timeline', 'mismatch'), ('presentation_time', 'match'), ('pixels', 'match')])

    def test_changed_raster_is_a_spatial_mismatch(self):
        fixture = VisualFixture(candidate_pixels=(PIXELS[0], bytes.fromhex('61708090a0b0')))
        result = fixture.direct()
        self.assertEqual(result['result'], 'mismatch')
        self.assertEqual(result['checks'][0]['outcome'], 'match')
        self.assertEqual(result['checks'][1]['first_divergence']['frame_index'], 1)

    def test_sparse_pins_select_occurrences_and_unpinned_requires_full_coverage(self):
        fixture = VisualFixture()
        for media in fixture.records.values():
            media['frames'] = media['frames'][1:]
        self.assertEqual(fixture.direct(frame_indices=[1])['result'], 'match')
        self.assertEqual(fixture.direct()['abstain_reason'], 'missing_reference_coverage')
        self.assertIsNone(fixture.pool.owner)

    def test_timeline_wrapper_preserves_empty_digest_gate(self):
        empty = timeline_side([]).record
        candidate = timeline_side(event_trace()).record
        binding = {'clock_source': 'pts', 'time_base_numerator': 1,
                   'time_base_denominator': 2000, 'origin_tick': 0}
        report = timeline.compare(empty, candidate, clock_binding=binding)
        self.assertEqual((report['abstain_reason'], report['checks']), ('empty_reference', []))
        empty['timeline_sha256'] = digest(b'tampered')
        report = timeline.compare(empty, candidate, clock_binding=binding)
        self.assertEqual((report['abstain_reason'], report['checks']), ('timeline_invalid', []))

    def test_behaviour_wrapper_preserves_exact_cases_and_approved_differences(self):
        comp = behaviour_comparison('allowed')
        waiver = waiver_for(comp)
        waiver['source'] = waiver['scope']
        extra = {'case_id': 'exact', 'input_digest': digest(b'exact'), 'kind': 'undefined'}
        ref = [extra, *comp.reference.behaviour.cases]
        cand = [extra, *comp.candidate.behaviour.cases]
        report = behaviour.compare(ref, cand, policy=comp.policy.pin(), scope=waiver['scope'], waivers=[waiver])
        self.assertEqual(report['result'], 'match')
        self.assertEqual(report['exact_case_ids'], ['exact'])
        self.assertEqual(report['accepted_differences'], [{'case_id': 'press-1', 'waiver_ref': waiver['ref']}])
        changed = [extra, *cases('off').cases]
        report = behaviour.compare(ref, changed, policy=comp.policy.pin(), scope=waiver['scope'], waivers=[waiver])
        self.assertEqual((report['abstain_reason'], report['checks'], report['exception_refs']),
                         ('evidence_binding_rejected', [], []))
