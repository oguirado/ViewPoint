# SPDX-License-Identifier: AGPL-3.0-only
"""Measure fixture outputs from authorized readback and sealed source bytes."""

from fractions import Fraction
from io import BytesIO
from struct import error as StructError

from slot_host.compare import tools as compare
from slot_host.compare._behaviour import compare_behaviour
from slot_host.compare._model import exact_ms
from slot_host.compare._timeline import compare_timeline
from slot_host.projections import SlotProjector
from slot_host.projections.schema import digest, jcs_bytes
from slot_host.runtime import error_diagnose, journal_verify, reconstruction_obligations
from video_rea.expose.wire import loads

from .host import Host
from .runtime import INSTALLATION, Runtime
from .seal import FixtureError, read_regular, sha
from .visual import PPM, rgb8_frame, rgb8_header


def same(actual, expected, label):
    if jcs_bytes(actual) != jcs_bytes(expected):
        raise FixtureError(f'{label} differs from independent fixture evidence.')


def stateless_result(host, runtime, request_id, reply):
    args = host.assignment['arguments']
    if host.face == 'error.diagnose':
        result = error_diagnose.diagnose(args)
        seen = [{'occurrence': args['attempt_ref'], 'value': result['category']}]
    elif host.face == 'reconstruction.obligations':
        source = Host(host.seal, host.face, host.assignment, host.store)
        result = reconstruction_obligations.evaluate(args, request_id=request_id, host=source)
        seen = [{'occurrence': row['id'], 'value': row['status']} for row in result['obligations']]
    else:
        manifest = host.seal.inputs['slot']
        operation = next(row for row in manifest['operations'] if row['id'] == 'record')
        checksum = sha(read_regular(runtime.path))
        same(checksum, manifest['provider_ref']['implementation_sha256'], 'Installed fixture executable')
        same(args['slot_id'], manifest['slot_id'], 'Inspected slot')
        manifest_digest = digest(manifest)
        slot = {key: manifest[key] for key in SlotProjector._DESCRIBE_SLOT_KEYS - {'manifest_digest'}}
        slot['manifest_digest'] = manifest_digest
        same(reply, {'slot': slot, 'installation_refs': [INSTALLATION],
                     'selected_installation_ref': INSTALLATION, 'conformance_receipt_ref': None,
                     'unresolved_conformance_refs': []}, 'Projected slot inspection')
        policy = Runtime.policy(manifest, operation['id'])
        result = {
            'validation': {'ok': True, 'slot_id': args['slot_id'], 'manifest_digest': manifest_digest},
            'plan': {'operation_id': operation['id'], 'role': operation['role'],
                     'transport_kind': manifest['transport']['kind']},
            'required_grants': operation['required_grants'], 'effective_grants': sorted(policy['grants']),
            'readiness': {'ready': True, 'availability': 'admitted', 'execution_profile': 'external_process',
                          'observed_sha256': checksum},
        }
        return result, [{'occurrence': args['slot_id'], 'value': result['readiness']['availability']}]
    same(reply, result, f'Projected {host.face}')
    return result, seen


def journal_result(host, journal, ref, request_id):
    args = host.assignment['arguments']
    raw, seal = host.resolve_journal(args['journal_ref'], args['trusted_seal_ref'],
                                     request_id=request_id, max_bytes=journal_verify.MAX_JOURNAL_BYTES)
    policy = args.get('diagnostic_policy', 'fail-closed')
    result, root = journal_verify._inspect(raw, seal, diagnostics=policy)
    same(journal, {
        'schema': 'rea.journal_verification_receipt/v1', 'request_id': request_id,
        'journal_ref': args['journal_ref'], 'trusted_seal_ref': args['trusted_seal_ref'],
        'grammar_profile': seal.grammar_profile, 'diagnostic_policy': policy,
        'journal_sha256': sha(raw), 'computed_root_sha256': root, 'trusted_root_sha256': seal.root_sha256,
        'trusted_evidence_ids': list(seal.evidence_ids), 'result': result,
    }, 'Paged journal verification')
    steps = [loads(line) for line in raw.splitlines()]
    seen = [{'occurrence': step['event']['event_id'], 'ticks': step['event']['t_ticks'],
             'pts_ticks': step['presentation']['pts_ticks']} for step in steps]
    return journal['result'] | {'receipt_ref': ref}, seen


def tape_result(host, artifacts, refs):
    tape, log = artifacts['tape'], artifacts['log']
    args = host.assignment['arguments']
    same([log['tape_ref'], log['request'], log['operation_schema_sha256']],
         [refs['tape'], args, host.asset(args['operation_schema_ref'], 'operation-schema')['sha256']],
         'Paged tape and generation log linkage')
    result = {'tape_ref': refs['tape'], 'generation_log_ref': refs['log'],
              'skipped_exports': log['skipped_exports'], 'origin_by_case': log['origin_by_case']}
    return result, [{'occurrence': str(i), 'value': item} for i, item in enumerate(tape, 1)]


def effect_result(host, artifacts, refs, request_id):
    fixture = artifacts['fixture']
    args = host.assignment['arguments']
    sides = ('original', 'candidate')
    same([fixture['request_id'], fixture['request'], fixture['plan_sha256']],
         [request_id, args, host.asset(args['run_ref'], 'effect-plan')['sha256']], 'Paged effect fixture')
    seen = []
    for side in sides:
        capture, reset = (artifacts[f'{kind}-{side}'] for kind in ('effects', 'reset'))
        for row in (capture, reset):
            same([row['side'], row['request_id'], row['plan_sha256']],
                 [side, request_id, fixture['plan_sha256']], 'Paged effect capture or reset')
        case, = capture['cases']
        same([case['input_digest'], reset['scratch_removed']], [fixture['input_digest'], True],
             'Paged effect input and cleanup')
        seen.extend({'occurrence': f'{side}-{index}', 'value': write}
                    for index, write in enumerate(case['observation']['payload']['writes']))
    result = {'fixture_ref': refs['fixture'], **{key: fixture[key] for key in
              ('captured_effect_refs', 'reset_receipt_refs', 'unobserved_channels', 'unavailable_controls')}}
    same(result['captured_effect_refs'], [refs[f'effects-{side}'] for side in sides], 'Effect capture references')
    same(result['reset_receipt_refs'], [refs[f'reset-{side}'] for side in sides], 'Effect reset references')
    return result, seen


def _recording(host, episode, decoder_ref):
    media = episode.media
    row = host.seal.inputs['assets'][media.recording_ref]
    raw = bytes.fromhex(row['data'])
    same([row['role'], row['identity'], sha(raw)],
         ['recording', episode.episode_id, media.record['recording_sha256']], 'Sealed RGB8 recording')
    with BytesIO(raw) as stream:
        try:
            count, den = rgb8_header(stream, decoder_ref)
            same([count, 1, den], [len(media.record['frames']), media.record['time_base']['num'],
                                   media.record['time_base']['den']], 'RGB8 frame count and time base')
            frames = [rgb8_frame(stream) for _ in range(count)]
        except StructError as error:
            raise FixtureError('Truncated RGB8 recording.') from error
        if stream.read(1):
            raise FixtureError('Trailing bytes in RGB8 recording.')
    for index, ((ticks, _), frame) in enumerate(zip(frames, media.record['frames'], strict=True)):
        same([index, ticks], [frame['frame_index'], frame['pts_ticks']], 'RGB8 frame occurrence and PTS')
    return den, frames


def _visual_occurrences(host, resolved, pts, spatial):
    policy = resolved.policy.spatial
    ref_den, reference = _recording(host, resolved.reference, policy.decoder_ref)
    cand_den, candidate = _recording(host, resolved.candidate, policy.decoder_ref)
    pairs, seen = [], []
    times_match = len(reference) == len(candidate)
    for index in range(max(len(reference), len(candidate))):
        left = Fraction(reference[index][0] * 1000, ref_den) if index < len(reference) else None
        right = Fraction(candidate[index][0] * 1000, cand_den) if index < len(candidate) else None
        times_match &= left == right
        delta = right - left if left is not None and right is not None else None
        pairs.append({'frame_index': index, 'reference_ms_exact': exact_ms(left),
                      'candidate_ms_exact': exact_ms(right), 'delta_ms_exact': exact_ms(delta)})
        if right is not None:
            seen.append({'occurrence': f'frame-{index}', 'ticks': candidate[index][0]})
    same([pts['outcome'], pts['diagnostics']],
         ['match' if times_match else 'mismatch',
          {'pairs': pairs, 'reference_count': len(reference), 'candidate_count': len(candidate)}],
         'Paged presentation check')
    if not times_match:
        same([spatial['outcome'], spatial['diagnostics']], ['abstain', None], 'Paged pixel abstention')
        return seen

    x, y, width, height = policy.region or (0, 0, 2, 1)
    region_available = x + width <= 2 and y + height <= 1
    pixels = []
    for index, ((_, left), (_, right)) in enumerate(zip(reference, candidate, strict=True)):
        for episode, raw in ((resolved.reference, left), (resolved.candidate, right)):
            frame = episode.media.record['frames'][index]
            same([sha(PPM + raw), sha(raw)], [frame['frame_file_sha256'], frame['decoded_frame_sha256']],
                 'Independently decoded RGB8 bytes')
        if not region_available:
            pixels.append({'frame_index': index, 'outcome': 'region_unavailable'})
            continue
        mismatch = sum(any(abs(left[offset] - right[offset]) > policy.max_channel_delta
                           for offset in range(start, start + 3)) for start in range(x * 3, (x + width) * 3, 3))
        outcome = 'mismatch' if mismatch * 1_000_000 > policy.max_mismatch_ppm * width else 'match'
        pixels.append({'frame_index': index, 'mismatched_pixels': mismatch, 'compared_pixels': width,
                       'outcome': outcome})
        seen[index]['value'] = sha(right)
    outcome = 'abstain'
    if region_available:
        outcome = 'mismatch' if any(row['outcome'] == 'mismatch' for row in pixels) else 'match'
    same([spatial['outcome'], spatial['diagnostics']],
         [outcome, {'frames': pixels}], 'Paged spatial check')
    return seen


def comparison_result(host, artifacts, objects, owner):
    report, retained = artifacts['report'], artifacts['comparison-receipt']
    source = Host(host.seal, host.face, host.assignment, host.store)
    resolved = source.resolve_comparison(host.face, owner['request_id'], **host.assignment['arguments'])
    same(report['binding'], {**owner, 'reference_episode_id': resolved.reference.episode_id,
                           'candidate_episode_id': resolved.candidate.episode_id,
                           'policy_sha256': resolved.policy.sha256, 'slot_result_digest': digest(None)},
         'Paged comparison binding')
    artifact_ref = objects['report']['ref']
    same(retained, {**owner, 'artifact_ref': artifact_ref, 'artifact_sha256': objects['report']['sha256']},
         'Paged comparison receipt')
    checks = report['diagnostics']['checks']
    same([row['check_id'] for row in checks], list(resolved.policy.checks), 'Paged comparison check inventory')
    same([{key: value for key, value in row.items() if key != 'diagnostics'} for row in checks],
         report['verdict']['checks'], 'Paged admitted comparison checks')
    check = checks[0]
    reference, candidate, policy = resolved.reference, resolved.candidate, resolved.policy
    if host.face == 'behaviour.compare':
        expected = compare_behaviour(reference.behaviour, candidate.behaviour, policy, resolved.waivers)
        detail = check['diagnostics']
        keys = [*detail['exact_case_ids'], *(row['case_id'] for row in detail['accepted_differences']),
                *(row['case_id'] for row in detail['mismatches'] if row['detail'] != 'case_missing')]
        values = {row['case_id']: row['observation']['payload'] for row in candidate.behaviour.cases}
        seen = [{'occurrence': key, 'value': values[key]} for key in keys]
    else:
        expected = compare_timeline(reference.timeline, candidate.timeline, policy.timeline)
        ticks = {row['event_id']: row['t_ticks'] for row in candidate.timeline.record['events']}
        seen = [{'occurrence': row['candidate_event_id'], 'ticks': ticks[row['candidate_event_id']]}
                for row in check['diagnostics']['events'] if row['candidate_event_id'] is not None]
    same(check, {**expected.receipt_fields(), 'diagnostics': expected.diagnostics}, 'Paged comparison observations')
    if host.face == 'visual.compare':
        seen.extend(_visual_occurrences(host, resolved, checks[1], checks[2]))
    fields = compare._result_fields(host.face, report)
    same(report['result_fields'], fields, 'Paged comparison result fields')
    result = {'artifact_ref': artifact_ref,
              'receipt_ref': 'fixture:comparison-receipt:sha256:' + objects['comparison-receipt']['sha256'],
              'verdict': report['verdict'], 'observation': report['observation'], **fields}
    return result, seen
