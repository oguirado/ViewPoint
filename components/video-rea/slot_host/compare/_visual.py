# SPDX-License-Identifier: AGPL-3.0-only
"""Verify recordings, encoded PTS and RGB8 rasters under a shared pool lease."""

import hashlib
import math
import os
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from fractions import Fraction
from typing import BinaryIO, ContextManager, Generator, Protocol

from pixel_compare import RGB8Frame, Region

from ._model import (
    Finding, Rejected, check_time_base, divergence_times, exact_ms, is_digest,
    is_identifier, unsigned,
)


@dataclass(eq=False)
class _Reservation:
    size: int
    in_use: bool = False


class _RasterBudget:
    def __init__(self, ceiling, deadline):
        self.ceiling = ceiling
        self.deadline = deadline
        self.live = 0
        self.peak = 0
        self.denied_size = None
        self.reservations = set()

    @contextmanager
    def reserve(self, size):
        """Reserve bytes before allocation and release after dropping the buffers."""
        _guard(self.deadline)
        if not unsigned(size):
            raise Rejected('evidence_binding_rejected', 'The decoder declared an invalid allocation.')
        if size > self.ceiling - self.live:
            self.denied_size = size
            raise Rejected('coverage_insufficient', 'The next decoded allocation exceeds the host raster budget.')
        reservation = _Reservation(size)
        self.reservations.add(reservation)
        self.live += size
        self.peak = max(self.peak, self.live)
        try:
            yield reservation
        finally:
            self.reservations.remove(reservation)
            self.live -= size

    @contextmanager
    def hold(self, reservation, size):
        if (type(reservation) is not _Reservation or reservation not in self.reservations
                or reservation.in_use or size > reservation.size):
            raise Rejected('coverage_insufficient', 'A decoder frame has no sufficient live raster reservation.')
        reservation.in_use = True
        try:
            yield
        finally:
            reservation.in_use = False

    def diagnostics(self):
        return {'ceiling_bytes': self.ceiling, 'peak_reserved_bytes': self.peak,
                'remaining_reserved_bytes': self.live, 'denied_allocation_bytes': self.denied_size}


@dataclass(frozen=True, kw_only=True)
class DecodedFrame:
    index: int
    pts_ticks: int
    time_base: tuple[int, int] | None
    raster: RGB8Frame
    recording_sha256: str
    reservation: _Reservation
    retained_file_bytes: bytes | None = None


@dataclass(frozen=True, kw_only=True)
class RecordingCompletion:
    """Positive clean end-of-stream witness produced by the trusted decoder."""

    recording_sha256: str
    frame_count: int


class VisualSource(Protocol):
    """Host adapter for an immutable recording and a pinned decoder.

    Decode the supplied open stream. Reserve aggregate live decoded buffers,
    including scratch, rasters, file derivatives and cache, before allocating
    them. Hold each frame reservation through its yield. Close drops every
    outstanding buffer and stops decoder work before the pool is released.
    Only a clean decode may return RecordingCompletion. A failed lookup or
    read is incomplete. Keep materialized pixel derivatives under /dev/shm.
    """

    def open_recording(self, ref: str) -> ContextManager[BinaryIO]: ...

    def decode(self, stream: BinaryIO, decoder_ref: str, *, recording_ref: str,
               deadline: float, budget: _RasterBudget
               ) -> Generator[DecodedFrame, None, RecordingCompletion | None]: ...


class VisionPool(Protocol):
    """The trusted shared pool verifies release for the supplied owner and lease."""

    def acquire(self, front: str, pid: int, timeout: int, ttl: int) -> dict | None: ...

    def release(self, front: str, pid: int, lease_id: str) -> bool: ...


@dataclass(frozen=True, kw_only=True)
class Vision:
    pool: VisionPool
    source: VisualSource
    front: str
    timeout: int
    ttl: int
    attempt_id: str | None = None
    decoded_byte_ceiling: int | None = None


@dataclass(frozen=True)
class _VisualResult:
    findings: tuple[Finding, Finding]
    ticket: dict | None = None
    rejection: Rejected | None = None
    used_vision: bool = False
    custody: dict | None = None


def _unavailable(reason, detail, *, rejection=None):
    return _VisualResult((
        Finding('pts', 'presentation_time', 'abstain', detail, reason),
        Finding('spatial', 'pixels', 'abstain', detail, reason),
    ), rejection=rejection)


def _guard(deadline):
    if time.monotonic() >= deadline:
        raise Rejected('lease_custody_unverified', 'The vision work exceeded the lease deadline.')


def _process_start(pid):
    if pid <= 1:
        return None
    try:
        with open(f'/proc/{pid}/stat', encoding='utf-8', errors='replace') as stat:
            fields = stat.read(4096)
        return int(fields.rsplit(')', 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


_MEDIA_REQUIRED = {'recording_sha256', 'container', 'codec', 'clock_source', 'frames'}
_MEDIA_ALLOWED = _MEDIA_REQUIRED | {'time_base', 'frames_unavailable_reason'}
_FRAME_REQUIRED = {'frame_index', 'pts_ms', 'decoded_frame_sha256'}
_FRAME_ALLOWED = _FRAME_REQUIRED | {'pts_ticks', 'frame_file_sha256'}


def _coverage_reason(side):
    return 'missing_reference_coverage' if side == 'reference' else 'coverage_insufficient'


def _media_rows(media, side):
    if media is None:
        raise Rejected(_coverage_reason(side), f'The {side} recording is unavailable.')
    record = media.record
    if (type(record) is not dict or not _MEDIA_REQUIRED <= record.keys() <= _MEDIA_ALLOWED
            or not is_digest(record.get('recording_sha256'))
            or any(type(record.get(key)) is not str or not 1 <= len(record[key]) <= 64
                   for key in ('container', 'codec')) or type(record.get('frames')) is not list):
        raise Rejected('evidence_binding_rejected', f'The {side} recording binding is malformed.')
    clock_source = record.get('clock_source')
    if clock_source not in ('pts', 'wall', 'monotonic'):
        raise Rejected('evidence_binding_rejected', f'The {side} recording has an invalid clock source.')
    if clock_source != 'pts':
        raise Rejected('clock_source_unsuitable', f'The {side} recording lacks encoded PTS.')
    if 'time_base' in record:
        check_time_base(record['time_base'])
    if not record['frames']:
        reason = record.get('frames_unavailable_reason')
        if type(reason) is not str or not 1 <= len(reason) <= 2000:
            raise Rejected('evidence_binding_rejected', f'The {side} recording lacks an unavailability reason.')
        raise Rejected(_coverage_reason(side), f'The {side} recording has no bound frame occurrences.')
    if 'frames_unavailable_reason' in record:
        raise Rejected('evidence_binding_rejected', f'The {side} recording forbids an unavailability reason.')
    rows = {}
    previous = -1
    for row in record['frames']:
        if (type(row) is not dict or not _FRAME_REQUIRED <= row.keys() <= _FRAME_ALLOWED
                or not unsigned(row.get('frame_index')) or row['frame_index'] <= previous
                or not unsigned(row.get('pts_ms')) or not is_digest(row.get('decoded_frame_sha256'))
                or ('pts_ticks' in row and not unsigned(row['pts_ticks']))
                or ('frame_file_sha256' in row and not is_digest(row['frame_file_sha256']))):
            raise Rejected('evidence_binding_rejected', f'The {side} frame binding is malformed or out of order.')
        previous = row['frame_index']
        rows[previous] = row
    if 'time_base' not in record and any('pts_ticks' in row for row in rows.values()):
        raise Rejected('missing_clock_binding', f'The {side} retained ticks lack a time base.')
    return rows


def _frame_pts(frame, index, recording_digest, retained_base, observed_base):
    if (type(frame) is not DecodedFrame or not unsigned(frame.index) or frame.index != index
            or not unsigned(frame.pts_ticks) or frame.recording_sha256 != recording_digest):
        raise Rejected('evidence_binding_rejected', 'The decoder returned an unbound occurrence or exact PTS.')
    base = frame.time_base
    if base is None:
        raise Rejected('missing_clock_binding', 'The decoder did not witness its source time base.')
    if (type(base) is not tuple or len(base) != 2 or any(not unsigned(term) or term == 0 for term in base)
            or (observed_base is not None and base != observed_base)
            or (retained_base is not None and base != retained_base)):
        raise Rejected('evidence_binding_rejected', 'The decoder returned a conflicting source time base.')
    return Fraction(frame.pts_ticks * base[0] * 1000, base[1])


def _frame_size(frame):
    raster = frame.raster
    file_bytes = frame.retained_file_bytes
    if (type(raster) is not RGB8Frame or not unsigned(raster.width) or not unsigned(raster.height)
            or not raster.width or not raster.height or type(raster.pixels) is not bytes
            or len(raster.pixels) != raster.width * raster.height * 3
            or (file_bytes is not None and type(file_bytes) is not bytes)):
        raise Rejected('evidence_binding_rejected', 'The decoder returned an invalid raster or frame file.')
    return len(raster.pixels) + (len(file_bytes) if file_bytes is not None else 0)


def _verify_frame(row, frame, pts):
    if (pts.numerator // pts.denominator != row['pts_ms']
            or ('pts_ticks' in row and row['pts_ticks'] != frame.pts_ticks)):
        raise Rejected('evidence_binding_rejected', f'Frame {frame.index} has a different encoded PTS.')
    if hashlib.sha256(frame.raster.pixels).hexdigest() != row['decoded_frame_sha256']:
        raise Rejected('evidence_binding_rejected', f'Frame {frame.index} has a different decoded raster digest.')
    if 'frame_file_sha256' in row:
        data = frame.retained_file_bytes
        if data is None or hashlib.sha256(data).hexdigest() != row['frame_file_sha256']:
            raise Rejected('evidence_binding_rejected', f'Frame {frame.index} has no matching retained file digest.')


def _verify_completion(completion, recording_digest, count, last):
    if completion is None:
        raise Rejected('coverage_insufficient', 'The decode stopped without a completion witness.')
    if (type(completion) is not RecordingCompletion
            or completion.recording_sha256 != recording_digest or not unsigned(completion.frame_count)):
        raise Rejected('evidence_binding_rejected', 'The completion witness is unbound to the recording.')
    if completion.frame_count != count:
        raise Rejected('coverage_insufficient', 'The decode did not deliver every witnessed occurrence.')
    if count <= last:
        raise Rejected('evidence_binding_rejected', 'A retained occurrence is beyond the verified end of the recording.')


def _bound_frames(media, rows, source, policy, budget, side, work):
    selected = None if policy.frame_indices is None else set(policy.frame_indices)
    retained_base = check_time_base(media.record['time_base']) if 'time_base' in media.record else None
    last = max(rows)
    recording_digest = media.record['recording_sha256']
    observed_base = None
    previous = None
    index = 0
    deadline = budget.deadline
    try:
        _guard(deadline)
        with source.open_recording(media.recording_ref) as stream:
            hasher = hashlib.sha256()
            stream.seek(0)
            while True:
                _guard(deadline)
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
            if hasher.hexdigest() != recording_digest:
                raise Rejected('evidence_binding_rejected', f'The {side} recording digest failed recomputation.')
            stream.seek(0)
            _guard(deadline)
            work['started'] = True
            with closing(source.decode(stream, policy.decoder_ref, recording_ref=media.recording_ref,
                                       deadline=deadline, budget=budget)) as frames:
                while selected is None or index <= last:
                    _guard(deadline)
                    try:
                        frame = next(frames)
                    except StopIteration as stopped:
                        _verify_completion(stopped.value, recording_digest, index, last)
                        return
                    _guard(deadline)
                    pts = _frame_pts(frame, index, recording_digest, retained_base, observed_base)
                    observed_base = frame.time_base
                    if previous is not None and pts < previous:
                        raise Rejected('evidence_binding_rejected', f'The {side} decoded PTS decreased.')
                    previous = pts
                    row = rows.get(index)
                    wanted = selected is None or index in selected
                    with budget.hold(frame.reservation, _frame_size(frame)):
                        if row is not None:
                            _verify_frame(row, frame, pts)
                        elif wanted:
                            raise Rejected(_coverage_reason(side), f'The {side} lacks a required frame binding.')
                        if wanted:
                            yield index, pts, frame.raster
                    del frame
                    index += 1
    except Rejected:
        raise
    except (KeyError, EOFError, OSError, ValueError) as error:
        raise Rejected('coverage_insufficient', f'The {side} recording could not be decoded completely.') from error


def _raster_check(index, pts, left, right, policy, deadline):
    divergence = {'frame_index': index, **divergence_times(pts, pts)}
    if (left.width, left.height) != (right.width, right.height):
        return {'frame_index': index, 'outcome': 'geometry_mismatch'}, ('spatial.geometry_mismatch', divergence)
    region = Region(*(policy.region or (0, 0, left.width, left.height)))
    if region.x + region.width > left.width or region.y + region.height > left.height:
        return {'frame_index': index, 'outcome': 'region_unavailable'}, None
    mismatched = 0
    limit = policy.max_channel_delta
    for y in range(region.y, region.y + region.height):
        start = (y * left.width + region.x) * 3
        for x in range(region.width):
            if x % 1024 == 0:
                _guard(deadline)
            offset = start + x * 3
            if (abs(left.pixels[offset] - right.pixels[offset]) > limit
                    or abs(left.pixels[offset + 1] - right.pixels[offset + 1]) > limit
                    or abs(left.pixels[offset + 2] - right.pixels[offset + 2]) > limit):
                mismatched += 1
    area = region.width * region.height
    fails = mismatched * 1_000_000 > policy.max_mismatch_ppm * area
    row = {'frame_index': index, 'mismatched_pixels': mismatched, 'compared_pixels': area,
           'outcome': 'mismatch' if fails else 'match'}
    failure = (f'spatial.pixels_over_threshold: {mismatched} of {area} pixels differ.', divergence) if fails else None
    return row, failure


def _presentation(pairs, counts, first):
    diagnostics = {'reference_count': counts[0], 'candidate_count': counts[1], 'pairs': pairs}
    if first is not None:
        return Finding('pts', 'presentation_time', 'mismatch', 'pts.occurrence_or_time_differs',
                       divergence=first, diagnostics=diagnostics)
    return Finding('pts', 'presentation_time', 'match', 'All required frame occurrences pair at identical encoded PTS.',
                   diagnostics=diagnostics)


def _compare_frames(reference, candidate, ref_rows, cand_rows, source, policy, budget, work):
    pairs = []
    compared = []
    counts = [0, 0]
    first_pts = None
    first_spatial = None
    invalid_region = False
    left_frames = _bound_frames(reference, ref_rows, source, policy, budget, 'reference', work)
    right_frames = _bound_frames(candidate, cand_rows, source, policy, budget, 'candidate', work)
    with closing(left_frames), closing(right_frames):
        while True:
            left = next(left_frames, None)
            right = next(right_frames, None)
            if left is None and right is None:
                break
            counts[0] += left is not None
            counts[1] += right is not None
            index = left[0] if left is not None else right[0]
            ref_time = left[1] if left is not None else None
            cand_time = right[1] if right is not None else None
            paired = ref_time is not None and cand_time is not None and left[0] == right[0]
            delta = cand_time - ref_time if paired else None
            pairs.append({'frame_index': index, 'reference_ms_exact': exact_ms(ref_time),
                          'candidate_ms_exact': exact_ms(cand_time), 'delta_ms_exact': exact_ms(delta)})
            if first_pts is None and (not paired or delta != 0):
                first_pts = {'frame_index': index, **divergence_times(ref_time, cand_time)}
                compared.clear()
            if first_pts is None:
                row, failure = _raster_check(index, ref_time, left[2], right[2], policy, budget.deadline)
                compared.append(row)
                invalid_region |= row['outcome'] == 'region_unavailable'
                if first_spatial is None:
                    first_spatial = failure
            del left, right
    _guard(budget.deadline)
    if budget.live:
        raise Rejected('coverage_insufficient', 'The decoder left raster reservations active after closing.')
    pts = _presentation(pairs, counts, first_pts)
    diagnostics = {'frames': compared}
    if first_pts is not None:
        spatial = Finding('spatial', 'pixels', 'abstain', 'Presentation-time pairing did not qualify.', 'coverage_insufficient')
    elif first_spatial is not None:
        spatial = Finding('spatial', 'pixels', 'mismatch', first_spatial[0],
                          divergence=first_spatial[1], diagnostics=diagnostics)
    elif invalid_region:
        spatial = Finding('spatial', 'pixels', 'abstain', 'The pinned region lies outside a required raster.',
                          'coverage_insufficient', diagnostics=diagnostics)
    else:
        spatial = Finding('spatial', 'pixels', 'match', 'All required regions satisfy the pinned spatial thresholds.',
                          diagnostics=diagnostics)
    return _VisualResult((pts, spatial))


def _verify_grant(grant, front, pid, starttime):
    token = grant.get('lease_id')
    deadline = grant.get('expires_at_monotonic')
    if (type(token) is not str or not 1 <= len(token) <= 256 or grant.get('front') != front
            or type(grant.get('pid')) is not int or grant['pid'] != pid
            or type(grant.get('starttime')) is not int or starttime is None or starttime <= 0
            or grant['starttime'] != starttime or _process_start(pid) != starttime
            or grant.get('status', 'granted') != 'granted'
            or type(deadline) not in (int, float) or not 0 < deadline < math.inf):
        raise Rejected('lease_custody_unverified', 'The vision-pool grant could not be verified.')
    _guard(deadline)
    try:
        digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
    except UnicodeError as error:
        raise Rejected('lease_custody_unverified', 'The lease identity is not valid UTF-8.') from error
    return deadline, {'projection': 'redacted', 'status': 'granted',
                      'lease_id_sha256': digest, 'front': front}


def compare_visual(reference, candidate, policy, vision, *, attempt_id=None):
    try:
        ref_rows = _media_rows(reference, 'reference')
        cand_rows = _media_rows(candidate, 'candidate')
        if policy.frame_indices is not None:
            for side, rows in (('reference', ref_rows), ('candidate', cand_rows)):
                if any(index not in rows for index in policy.frame_indices):
                    raise Rejected(_coverage_reason(side), f'The {side} frames do not cover the pinned policy.')
        if vision is None:
            return _unavailable('vision_pool_not_granted', 'No host vision-pool client is available.')
        if (not is_identifier(attempt_id) or vision.attempt_id != attempt_id
                or type(vision.front) is not str or not 1 <= len(vision.front) <= 128 or not vision.front.strip()
                or any(type(bound) is not int or not 0 < bound <= 2**31 - 1
                       for bound in (vision.timeout, vision.ttl))):
            raise Rejected('lease_custody_unverified', 'The vision-pool owner, attempt or lease bounds are invalid.')
        if not unsigned(vision.decoded_byte_ceiling) or vision.decoded_byte_ceiling == 0:
            result = _unavailable('coverage_insufficient', 'The host supplied no positive decoded-byte ceiling.')
            return replace(result, custody={'attempt_id': attempt_id, 'raster_budget': {'ceiling_verified': False}})
    except Rejected as error:
        fatal = error if error.reason == 'evidence_binding_rejected' else None
        return _unavailable(error.reason, str(error), rejection=fatal)
    pid = os.getpid()
    starttime = _process_start(pid)
    if starttime is None or starttime <= 0:
        return _unavailable('lease_custody_unverified', 'The vision-pool owner process could not be verified.')
    try:
        lease = vision.pool.acquire(vision.front, pid, vision.timeout, vision.ttl)
    except (OSError, ValueError, RuntimeError):
        return _unavailable('vision_pool_not_granted', 'The host vision-pool acquisition failed.')
    if lease is None:
        return _unavailable('vision_pool_timeout', 'The shared pool did not grant a lease before timeout.')
    if type(lease) is dict and lease.get('status') == 'timeout' and not lease.get('lease_id'):
        return _unavailable('vision_pool_timeout', 'The shared pool did not grant a lease before timeout.')
    if (type(lease) is dict and lease.get('status') not in (None, 'granted')
            and not lease.get('lease_id')):
        return _unavailable('vision_pool_not_granted', 'The shared pool refused the lease.')
    grant = dict(lease) if type(lease) is dict else {}
    token = grant.get('lease_id')
    owner = {'attempt_id': attempt_id, 'front': vision.front, 'pid': pid, 'starttime': starttime}
    custody = {'attempt_id': attempt_id, 'acquire': {**owner, 'grant': grant, 'verified': False}}
    released = False
    release_outcome = None
    ticket = None
    budget = None
    work = {'started': False}
    try:
        deadline, ticket = _verify_grant(grant, vision.front, pid, starttime)
        custody['acquire']['verified'] = True
        custody['acquire']['ticket'] = {**ticket, 'projection': 'restricted', 'lease_id': token}
        budget = _RasterBudget(vision.decoded_byte_ceiling, deadline)
        result = _compare_frames(reference, candidate, ref_rows, cand_rows, vision.source, policy, budget, work)
    except Rejected as error:
        fatal = Rejected(error.reason, str(error)) if error.reason == 'evidence_binding_rejected' else None
        result = _unavailable(error.reason, str(error), rejection=fatal)
    except (OSError, ValueError, RuntimeError):
        result = _unavailable('perception_unavailable', 'The host could not re-derive the required visual observations.')
    finally:
        # Streams and decoder iterators are closed before cleanup of the lease.
        if type(token) is str and token:
            try:
                before = _process_start(pid)
                release_outcome = vision.pool.release(vision.front, pid, token)
                released = release_outcome is True and before == starttime == _process_start(pid)
            except (OSError, ValueError, RuntimeError) as error:
                release_outcome = type(error).__name__
        custody['release'] = {**owner, 'lease_id': token, 'verified': released, 'outcome': release_outcome}
    if budget is not None:
        custody['raster_budget'] = budget.diagnostics()
    if not released:
        custody['discarded_checks'] = [{**finding.receipt_fields(), 'diagnostics': finding.diagnostics}
                                      for finding in result.findings]
        result = _unavailable('lease_custody_unverified', 'The vision-pool release could not be verified.')
    return replace(result, ticket=ticket if work['started'] else None,
                   used_vision=work['started'], custody=custody)
