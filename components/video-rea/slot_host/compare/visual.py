# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic-host visual comparison under an injected shared-pool lease."""

import hashlib

from ._model import Media, Rejected, SpatialPolicy, check_time_base, unsigned
from ._visual import DecodedFrame, RecordingCompletion, Vision, _guard, compare_visual


_FIXTURE_DECODER = 'fixture.rgb8.decode'


class _ObserveSource:
    """Bridge a synthetic decoder and its independent recording opener.

    observe_frame(ref, index, stream=..., budget=..., deadline=...) returns
    a context manager yielding an observation or positive RecordingCompletion.
    The observer reserves before decoding and holds that reservation until
    context exit. Each observation supplies exact encoded metadata and the
    recording bytes from which it was obtained.
    """

    def __init__(self, observe_frame, open_recording):
        self._observe = observe_frame
        self._open = open_recording

    def open_recording(self, ref):
        if not callable(self._open):
            raise Rejected('perception_unavailable', 'The observer has no independent recording opener.')
        return self._open(ref)

    def decode(self, stream, decoder_ref, *, recording_ref, deadline, budget):
        if decoder_ref != _FIXTURE_DECODER or not callable(self._observe):
            raise Rejected('perception_unavailable', 'The pinned fixture decoder is unavailable.')
        index = 0
        while True:
            _guard(deadline)
            with self._observe(recording_ref, index, stream=stream, budget=budget, deadline=deadline) as obs:
                if type(obs) is RecordingCompletion:
                    return obs
                if (type(obs) is not dict or not unsigned(obs.get('frame_index'))
                        or obs['frame_index'] != index or not unsigned(obs.get('pts_ticks'))):
                    raise Rejected('evidence_binding_rejected', 'The observer did not witness the occurrence and exact PTS.')
                data = obs.get('recording_bytes')
                if type(data) is not bytes:
                    raise Rejected('evidence_binding_rejected', 'The observer did not bind the frame to recording bytes.')
                observed_bases = [obs[key] for key in ('time_base', 'stream_time_base') if key in obs]
                if not observed_bases:
                    raise Rejected('missing_clock_binding', 'The observer did not witness the source time base.')
                try:
                    base = check_time_base(observed_bases[0])
                    if any(check_time_base(value) != base for value in observed_bases[1:]):
                        raise ValueError('Conflicting source time bases.')
                except ValueError as error:
                    raise Rejected('evidence_binding_rejected', 'The observer supplied an invalid source time base.') from error
                projected = obs['pts_ticks'] * base[0] * 1000 // base[1]
                if ('pts_ms' in obs and (not unsigned(obs['pts_ms']) or obs['pts_ms'] != projected)):
                    raise Rejected('evidence_binding_rejected', 'The observed millisecond projection disagrees with encoded PTS.')
                yield DecodedFrame(index=index, pts_ticks=obs['pts_ticks'], time_base=base,
                                   raster=obs.get('raster'), recording_sha256=hashlib.sha256(data).hexdigest(),
                                   reservation=obs.get('reservation'), retained_file_bytes=obs.get('frame_file_bytes'))
            del obs, data
            index += 1


def _region_tuple(region):
    if region is None:
        return None
    if type(region) in (tuple, list) and len(region) == 4:
        values = tuple(region)
    else:
        try:
            values = (region.x, region.y, region.width, region.height)
        except AttributeError as error:
            raise ValueError('Invalid region.') from error
    if not all(unsigned(value) for value in values) or min(values[2:]) <= 0:
        raise ValueError('Invalid region.')
    return values


def _abstain(reason, detail, checks=None):
    report = {'result': 'abstain', 'abstain_reason': reason, 'abstain_detail': detail[:2000],
              'perception_level': 'none'}
    report['checks'] = checks if checks is not None else [
        {'check_id': 'pts', 'kind': 'presentation_time', 'outcome': 'abstain', 'detail': detail},
        {'check_id': 'spatial', 'kind': 'pixels', 'outcome': 'abstain', 'detail': detail}]
    return report


def _frame_indices_tuple(value):
    if value is None:
        return None
    if type(value) not in (list, tuple):
        raise ValueError('Invalid frame indices.')
    values = tuple(value)
    if not values or not all(unsigned(item) for item in values) or values != tuple(sorted(set(values))):
        raise ValueError('Invalid frame indices.')
    return values


def _with_visual_fields(report, result):
    report['perception_level'] = 'vision' if result.used_vision else 'none'
    if result.used_vision:
        report['vision_pool_ticket'] = result.ticket
    admitted = {check['check_id'] for check in report['checks']}
    details = {finding.check_id: finding.diagnostics or {} for finding in result.findings
               if finding.check_id in admitted}
    pts = details.get('pts', {})
    report['coverage'] = {key: pts[key] for key in ('reference_count', 'candidate_count') if key in pts}
    report['aligned_occurrences'] = list(pts.get('pairs', []))
    report['spatial_difference'] = list(details.get('spatial', {}).get('frames', []))
    return report


def compare(reference, candidate, *, observe_frame, vision_pool, front,
            perception_ceiling='vision', region=None, max_mismatch_ppm=0,
            max_channel_delta=0, timeout=60, ttl=300, frame_indices=None,
            open_recording=None, decoded_byte_ceiling=None, attempt_id=None):
    """Compare two retained fixture media sides in the profile vocabulary."""
    if perception_ceiling != 'vision':
        return _abstain('perception_ceiling', 'The host ceiling prevents the required vision work.')
    if (not unsigned(max_mismatch_ppm) or max_mismatch_ppm > 1_000_000
            or not unsigned(max_channel_delta) or max_channel_delta > 255):
        return _abstain('policy_undeclared', 'The raster thresholds are invalid.', checks=[])
    try:
        region = _region_tuple(region)
        frame_indices = _frame_indices_tuple(frame_indices)
    except ValueError as error:
        return _abstain('policy_undeclared', str(error), checks=[])
    ref_media = None if reference is None else Media(recording_ref='reference', record=reference)
    cand_media = None if candidate is None else Media(recording_ref='candidate', record=candidate)
    spatial = SpatialPolicy(decoder_ref=_FIXTURE_DECODER, max_mismatch_ppm=max_mismatch_ppm,
                            max_channel_delta=max_channel_delta, region=region, frame_indices=frame_indices)
    vision = None
    if vision_pool is not None:
        vision = Vision(pool=vision_pool, source=_ObserveSource(observe_frame, open_recording), front=front,
                        timeout=timeout, ttl=ttl, attempt_id=attempt_id,
                        decoded_byte_ceiling=decoded_byte_ceiling)
    result = compare_visual(ref_media, cand_media, spatial, vision, attempt_id=attempt_id)
    if result.rejection is not None:
        report = _abstain(result.rejection.reason, str(result.rejection), checks=[])
        return _with_visual_fields(report, result)
    checks = [finding.receipt_fields() for finding in result.findings]
    if any(finding.outcome == 'mismatch' for finding in result.findings):
        return _with_visual_fields({'result': 'mismatch', 'checks': checks}, result)
    abstain = next((finding for finding in result.findings if finding.outcome == 'abstain'), None)
    if abstain is not None:
        return _with_visual_fields(_abstain(abstain.reason, abstain.detail, checks), result)
    return _with_visual_fields({'result': 'match', 'checks': checks}, result)
