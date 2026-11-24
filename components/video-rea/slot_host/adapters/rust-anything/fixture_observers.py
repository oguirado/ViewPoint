# SPDX-License-Identifier: AGPL-3.0-only
"""Observers for the frozen host-authored kit. None invokes an installed tool."""

from contextlib import contextmanager
from hashlib import sha256
from io import BytesIO
import time
from uuid import uuid4

from pixel_compare import RGB8Frame, Region, compare_checkpoints
from slot_host.compare import behaviour, timeline, visual
from slot_host.compare._visual import RecordingCompletion
from slot_host.projections import (
    cli_invoke, control_envelope, json_invoke, mcp_invoke, router_invoke,
)
from slot_host.projections.schema import digest, jcs_bytes
from slot_host.runtime import tools
from slot_host.runtime.binding import BindingRejected, check_profile
from slot_host.runtime.result_custody import proc_starttime, producer_binding_verified
from video_rea.expose.wire import loads


def _check(condition, detail):
    if not condition:
        raise ValueError(detail)


def _hash(data):
    return sha256(data).hexdigest()


def _compare(reference, candidate):
    policy = {'id': 'fixture-exact', 'version': '1'}
    return behaviour.compare(reference, candidate,
                             policy={**policy, 'sha256': digest(policy)},
                             scope='host-oracle-bank', waivers=[])


def admission(fixture, host):
    manifest = host.manifest
    record = next(op for op in manifest['operations'] if op['id'] == 'record')
    scope = {'fixture_preview': 'offline_fixture', 'live_execution': 'external_process'}
    _check(fixture['licence_disposition'] == 'host_owned_fixture', 'unreviewed fixture provenance')
    try:
        profile = check_profile(manifest, record, scope[fixture['requested_scope']])
    except BindingRejected as error:
        if error.reason != 'profile_not_admitted':
            raise
        return 'ORACLE_LIVE_PROFILE_NOT_ADMITTED', {
            'admitted': False, 'process_starts': 0, 'target_after': fixture['target_before']}
    _check(profile == fixture['admitted_profile'], 'fixture profile differs')
    contract = next(row for row in manifest['contract_refs'] if row['role'] == record['role'])
    schema_refs = {
        'request_schema': contract['request_schema_ref'],
        'result_schema': contract['result_schema_ref'],
        'provider_slot_manifest_schema': 'urn:rea.provider_slot_manifest:v1',
    }
    pins_resolved = all(host.registry[ref]['$id'] == 'urn:' + fixture[key].replace('/', ':')
                        for key, ref in schema_refs.items())
    _check(pins_resolved and record['role'] == fixture['slot'], 'fixture role or contract identities differ')
    route = 'host.slot.inspect'
    projected = _projections(host, route, 'kit-projection-check')
    channels = [name for name in ('ROUTER', 'MCP', 'CLI', 'JSON') if name in projected]
    compiles = [jcs_bytes(host.compile()) for _ in range(fixture['compile_count'])]
    readiness = tools.handle_slot_inspect({'slot_id': manifest['slot_id']}, host=host.inventory)
    _check(readiness['validation']['ok'] and not readiness['readiness']['ready'],
           'expected passive discovery before installation')
    return 'ORACLE_PASS', {
        'pin_verified': _hash(fixture['contract_pin_bytes_utf8'].encode()) == fixture['expected_fixture_pin_sha256'],
        'role_resolved': readiness['plan']['role'],
        'projected_channels': channels,
        'projections_byte_stable': len(compiles) > 1 and all(value == compiles[0] for value in compiles),
        'target_after': fixture['target_before'], 'process_starts': 0}


def record_replay(fixture, host):
    tape = fixture['input_tape']

    def run(table):
        return [{'case_id': event['event_id'], 'input': event['action'], 'kind': 'value',
                 'value': table['transition_returns'][event['action']]} for event in tape]

    original, candidate = (run(fixture[side]) for side in ('original', 'candidate'))
    _check(fixture['original']['fixture_id'] != fixture['candidate']['fixture_id'],
           'fixture identities must be distinct')
    oracle = fixture['host_oracle']
    occurrence = next(row for row in original if row['case_id'] == oracle['event_id'])
    _check(occurrence['value'] == oracle['return_value'], 'original differs from independent expectation')
    report = _compare(original, candidate)
    observation = {
        'outside_capsule': host.manifest['transport']['driver_location'] == 'outside_capsule',
        'original_fixture_id': fixture['original']['fixture_id'],
        'candidate_fixture_id': fixture['candidate']['fixture_id'],
        'activated_event_ids': [row['case_id'] for row in candidate],
        'original_return': occurrence['value'],
        'candidate_return': next(row['value'] for row in candidate if row['case_id'] == oracle['event_id']),
        'candidate_exit_rc': 0,  # Both fixture tables completed the synthetic protocol.
        'verdict': report['result'],
    }
    if report['result'] == 'mismatch':
        observation['first_divergence'] = {'event_id': report['checks'][0]['first_divergence']['case_id']}
        return 'ORACLE_BEHAVIOUR_MISMATCH', observation
    _check(report['result'] == 'match', 'structured comparison did not decide')
    return 'ORACLE_PASS', observation


def perception(fixture, host):
    """Exercise actual comparator lease ordering against an in-memory raster."""
    sample = host.perception_recording
    pixels = bytes([sample['pixel_u8']]) * 3
    raw = sample['pts_ticks'].to_bytes(4, 'big') + pixels
    raw_hash = _hash(raw)
    base = {'num': 1, 'den': 1000}
    media = {'recording_sha256': raw_hash, 'clock_source': 'pts', 'time_base': base,
             'container': 'fixture', 'codec': 'rgb8', 'frames': [{
                 'frame_index': 0, 'pts_ms': sample['pts_ticks'], 'pts_ticks': sample['pts_ticks'],
                 'decoded_frame_sha256': _hash(pixels), 'frame_file_sha256': raw_hash}]}
    lifecycle = []
    requests = dict.fromkeys(('none', 'cdp_atspi', 'vision'), 0)
    active_streams = set()
    engine_starts = 0

    class Pool:
        owner = None

        def acquire(self, front, pid, timeout, ttl):
            requests['vision'] += 1
            reply = fixture['mock_pool_reply']
            lifecycle.append(reply['status'])
            if reply['status'] == 'timeout':
                return reply
            _check(reply['status'] == 'granted' and self.owner is None, 'invalid fixture grant')
            self.owner = front, pid, reply['lease_id']
            return {**reply, 'front': front, 'pid': pid, 'starttime': proc_starttime(pid),
                    'expires_at_monotonic': time.monotonic() + ttl}

        def release(self, front, pid, token):
            if self.owner != (front, pid, token) or active_streams:
                return False
            self.owner = None
            lifecycle.append('released')
            return True

    pool = Pool()

    @contextmanager
    def open_recording(side):
        _check(pool.owner is not None, 'opened a recording without a grant')
        active_streams.add(side)
        try:
            with BytesIO(raw) as stream:
                yield stream
        finally:
            active_streams.remove(side)
            if engine_starts and not active_streams:
                lifecycle.append('engine_stopped')

    @contextmanager
    def observe(_side, index, *, stream, budget, deadline):
        nonlocal engine_starts
        if not engine_starts:
            lifecycle.append('engine_started')
            engine_starts += 1
        if index:
            _check(stream.read(1) == b'', 'unexpected extra raster bytes')
            yield RecordingCompletion(recording_sha256=raw_hash, frame_count=index)
            return
        with budget.reserve(len(raw) + len(pixels)) as reservation:
            data = stream.read(len(raw))
            yield {'frame_index': index, 'pts_ticks': int.from_bytes(data[:4], 'big'),
                   'time_base': base, 'raster': RGB8Frame(1, 1, data[4:]), 'recording_bytes': data,
                   'frame_file_bytes': data, 'reservation': reservation}

    levels = ('none', 'cdp_atspi', 'vision')
    actual = []
    report = None
    for probe in fixture['probes']:
        level = probe['requested_level']
        if levels.index(level) > levels.index(probe['capsule_maximum']):
            continue
        if level == 'vision' and fixture['pixel_observation_required']:
            report = visual.compare(media, media, observe_frame=observe, vision_pool=pool,
                                    front='rust-anything-offline', region=(0, 0, 1, 1),
                                    open_recording=open_recording, decoded_byte_ceiling=64,
                                    attempt_id='rust-anything-pool-fixture')
            if report.get('abstain_reason') == 'vision_pool_timeout':
                return 'ORACLE_VISION_LEASE_TIMEOUT', {
                    'vision_lifecycle': lifecycle, 'vision_engine_starts': engine_starts,
                    'verdict': report['result']}
            _check(report['result'] == 'match', f'fixture observation failed: {report.get("abstain_reason")}')
        actual.append(level)
    _check(report is not None, 'fixture omitted the required vision probe')
    ticket = report.get('vision_pool_ticket') or {}
    return 'ORACLE_PASS', {'actual_levels': actual,
                          'outside_capsule': host.manifest['transport']['driver_location'] == 'outside_capsule',
                          'pool_requests_by_level': requests, 'vision_lifecycle': lifecycle,
                          'public_ticket_disclosed': 'lease_id' in ticket}


def freshness(fixture, host):
    if _hash(fixture['current_original_utf8'].encode()) != fixture['recorded_original_sha256']:
        return 'ORACLE_ORIGINAL_STALE', {
            'fresh': False, 'stale_artifact_role': 'original', 'comparison_started': False}
    _check(fixture['recorded_bindings'] == fixture['current_bindings'], 'recorded bindings changed')
    probes = loads(jcs_bytes(fixture['value_probes']))
    cases = {probe['case_id']: probe for probe in probes}
    retained = [{'case_id': probe['case_id'], 'input': probe['case_id'],
                 'observation': {k: v for k, v in probe.items() if k != 'case_id'}} for probe in probes]
    literal, special = cases['literal'], cases['special']
    _check(_compare(retained, retained)['result'] == 'match', 'fixture tagged values did not compare')
    collision = [{**row, 'observation': {'kind': 'undefined'}} if row['case_id'] == 'literal'
                 else row for row in retained]
    return 'ORACLE_PASS', {
        'fresh': True, 'comparison_started': True, 'observed_literal': literal['payload'],
        'observed_special_kind': special['kind'],
        'literal_distinct_from_special': _compare(retained, collision)['result'] == 'mismatch',
        'observed_ordered_keys': [key for key, _ in cases['ordered']['pairs']],
        'observed_error_class': cases['error']['error_class']}


def timing(fixture, host):
    clock = fixture['clock_binding']

    def side(rows):
        base = {'num': clock['time_base_numerator'], 'den': clock['time_base_denominator']}
        events = []
        for row in rows:
            ticks, remainder = divmod(row['t_ms'] * base['den'], 1000 * base['num'])
            _check(remainder == 0, 'fixture cannot represent an event at exact ticks')
            events.append({**row, 'kind': 'fixture_event', 'input_digest': digest(row['event_id']),
                           't_ticks': ticks})
        return {'events': events, 'clock_source': clock['clock_source'], 'origin': 'episode_start',
                'canonicalization': 'rea.timeline_digest/v1', 'timeline_sha256': digest(events),
                'time_base': base}

    report = timeline.compare(side(fixture['reference_events']), side(fixture['candidate_events']),
                              clock_binding=clock, late_tolerance_ms=fixture['late_tolerance_ms'])
    pair_frames = list(zip(fixture['reference_frames'], fixture['candidate_frames'], strict=True))
    aligned = [ref['pts_tick'] for ref, cand in pair_frames if ref['pts_tick'] == cand['pts_tick']]

    def raster(frame):
        return RGB8Frame(1, 1, bytes([frame['pixel_u8']]) * 3)

    observation = {'aligned_pts_ticks': aligned, 'spatial_match': len(aligned) == len(pair_frames) and all(
        compare_checkpoints(raster(ref), raster(cand), Region(0, 0, 1, 1), max_mismatch_ppm=0).verdict == 'PASS'
        for ref, cand in pair_frames), 'verdict': report['result']}
    if report['result'] == 'match':
        return 'ORACLE_PASS', {**observation, 'matched_event_ids': sorted(
            entry['reference_event_id'] for entry in report['events'] if entry['outcome'] == 'match'), 'order_conflicts': report['order_conflicts']}
    _check(report['result'] == 'mismatch' and report['checks'], 'timeline comparison did not decide')
    divergence = report['checks'][0]['first_divergence']
    return 'ORACLE_EVENT_TIME_MISMATCH', {**observation, 'first_timing_divergence': {
        'event_id': divergence['event_id'], 'late_by_ms': divergence['delta_ms']}}


def custody(fixture, host):
    attempt = fixture['host_attempt']
    captured = fixture['captured_result']
    lifecycle = fixture['host_lifecycle']
    opened = lifecycle.index('attempt_started') < lifecycle.index('dispatch')
    cleanup = lifecycle.index('capture') < lifecycle.index('cleanup_observed')
    artifact_verified = _hash(captured['artifact_utf8'].encode()) == captured['artifact_sha256']
    identities = ('request_id', 'episode_id', 'target_id', 'provider_digest')
    exchange_verified = all(attempt[key] == captured[key] for key in identities)
    # Oracle labels denote synthetic processes. These values are never used as OS PIDs.
    synthetic_pids = {name: index for index, name in enumerate(
        sorted({attempt['bound_process_id'], captured['observed_process_id']}), start=2)}
    started_pid = synthetic_pids[attempt['bound_process_id']]
    observed_pid = synthetic_pids[captured['observed_process_id']]
    verified = producer_binding_verified(
        started_pid=started_pid, observed_pid=observed_pid, bound_starttime=started_pid,
        observed_starttime=observed_pid, witness_bound=opened and attempt['outside_capsule'],
        cleanup_observed=cleanup, observed_now=captured['provider_digest'],
        implementation_sha256=attempt['provider_digest'], overflow=False, timed_out=False,
        interrupted=False, exchange_mismatch=not exchange_verified, rc_int=0)
    accepted = verified and artifact_verified
    retained = {'attempt_started_before_dispatch': opened, 'producer_binding_verified': verified,
                'artifact_digest_verified': artifact_verified, 'cleanup_observed': cleanup,
                'parent_episode_id': attempt['parent_episode_id'], 'evidence_accepted': accepted}
    if not accepted:
        _check(not verified and attempt['bound_process_id'] != captured['observed_process_id']
               and exchange_verified and artifact_verified and opened and cleanup,
               'custody rejected for an unexpected reason')
        retained['failed_attempt_retained'] = True  # The caller seals this record with the opened receipt.
        return 'ORACLE_PRODUCER_BINDING_MISMATCH', retained
    return 'ORACLE_PASS', retained


def _projections(host, route, request_id):
    manifest, bundle = host.manifest, host.bundle
    context = {'caller': 'operator', 'agent_instance': 'rust-anything-offline'}
    opts = {'policy': host.policy, 'registry': host.registry, 'context': context}
    slot = manifest['slot_id']
    arguments = {'request_id': request_id, 'slot_id': slot, 'dry_run': True}
    envelope = control_envelope(manifest, route, arguments, context=context)
    return {
        'ROUTER': router_invoke(manifest, bundle, route, arguments, **opts),
        'MCP': mcp_invoke(manifest, bundle, 'rea_slot_inspect', arguments, **opts),
        'CLI': cli_invoke(manifest, bundle, ['slot', 'inspect', slot, '--request-id', request_id,
                                             '--dry-run'], **opts),
        'JSON': json_invoke(manifest, bundle, envelope, **opts),
    }


def transport(fixture, host):
    """Exercise the runtime's offline local claim. MAIN's durable lane stays owner-pending."""
    recovery = fixture['recovery_probe']
    _check(recovery['isolated_counter'] is True and recovery['fault_point'] == 'after_effect_before_reply',
           'unsupported fixture fault')
    prefix = 'rust-anything-fixture-' + uuid4().hex
    counter = fixture['initial_counter']
    commits = 0
    increment = 0
    interrupt = False

    class Inventory(dict):
        def items(self):
            nonlocal counter, commits
            counter += increment
            commits += 1
            if interrupt:
                raise InterruptedError('synthetic interruption after effect')
            return super().items()

    inventory = Inventory(host.inventory)
    normalizations = {}

    def request(lane, request_id, args):
        nonlocal increment
        increment = args['increment']
        if request_id not in normalizations:
            normalizations[request_id] = _projections(host, 'host.slot.inspect', f'{prefix}-{request_id}')
        normalized = normalizations[request_id][lane]
        return tools.dispatch('slot.inspect', {'request_id': normalized['request_id'],
                                               'payload': normalized['arguments']},
                              context={**fixture['common_context'], 'operation': fixture['operation'],
                                       'effect_args': args}, host=inventory)

    replies = []
    for row in fixture['requests_in_order']:
        try:
            replies.append(request(row['lane'], row['request_id'], row['effective_args']))
        except ValueError as error:
            if getattr(error, 'code', None) != 'REQUEST_ID_CONFLICT':
                raise
            return 'ORACLE_REQUEST_ID_CONFLICT', {'conflicting_lane': row['lane'],
                                                  'control_outcome': error.code,
                                                  'shared_request_effect_commits': commits,
                                                  'shared_counter': counter}
    bindings = [jcs_bytes(reply) for reply in replies]
    common = {'projection_parity': all(
                  len({jcs_bytes(value) for value in normalized.values()}) == 1
                  for normalized in normalizations.values()),
              'common_evidence_binding': len(set(bindings)) == 1,
              'shared_request_effect_commits': commits, 'shared_counter': counter}
    counter, commits = fixture['initial_counter'], 0
    interrupt = True
    try:
        request('CLI', recovery['request_id'], fixture['requests_in_order'][0]['effective_args'])
    except InterruptedError:
        interrupt = False
    _check(not interrupt and commits > 0, 'fixture interruption did not activate')
    try:
        request(recovery['retry_lane'], recovery['request_id'], fixture['requests_in_order'][0]['effective_args'])
    except ValueError as error:
        if getattr(error, 'code', None) == 'UNCERTAIN_OUTCOME':
            return 'ORACLE_PASS', {**common, 'recovery_probe': {'control_outcome': error.code,
                                                               'effect_commits': commits}}
        raise
    raise ValueError('fixture interruption retry did not preserve uncertainty')


def independence(fixture, host):
    expected, candidate = (fixture[key] for key in ('expected_observation', 'candidate_observation'))
    if expected['producer_id'] == candidate['producer_id']:
        return 'ORACLE_EXPECTATION_NOT_INDEPENDENT', {
            'oracle_independence': 'derived_from_same_source', 'qualified': False}
    _check(fixture['producers'][expected['producer_id']] == 'host_authored_expectation'
           and fixture['producers'][candidate['producer_id']] == 'candidate',
           'fixture producer roles differ')
    by_id = {event['event_id']: event for event in fixture['workload']}
    event_id = candidate['event_id']
    control = fixture['control']
    _check(expected['event_id'] == event_id == control['event_id'] and event_id in by_id
           and control['field'] == 'candidate_observation.value'
           and jcs_bytes(control['from']) == jcs_bytes(candidate['value'])
           and jcs_bytes(control['from']) != jcs_bytes(control['to']),
           'negative control did not target an exercised observation')

    def observed(value):
        return [{'case_id': event_id, 'input': by_id[event_id], 'kind': 'value', 'value': value}]

    reference = observed(expected['value'])
    baseline = _compare(reference, observed(candidate['value']))
    challenge = _compare(reference, observed(control['to']))
    first = challenge['checks'][0].get('first_divergence', {})
    return 'ORACLE_PASS', {
        'oracle_independence': 'independent', 'executed_negative_control_ids': [control['id']],
        'control_witness': {'event_id': first.get('case_id'), 'observed_value': control['to'],
                            'verdict': challenge['result']},
        'qualified': baseline['result'] == 'match' and challenge['result'] == 'mismatch'
                     and first.get('case_id') == event_id}


def lifecycle(fixture, host):
    active = fixture['active']
    user_installations = {row['binding_id']: dict(row) for row in (active, fixture['staged'])}
    original = dict(user_installations)
    staged = None
    pins, history = {}, {}
    reconciled = []
    observed = {}
    for step in fixture['steps']:
        action = step['op']
        if action == 'start':
            if active is not None:
                pins[step['attempt_id']] = active['binding_id']
                history[step['attempt_id']] = dict(active)
            else:
                observed['late_run_admitted'] = step['attempt_id'] in pins
        elif action == 'stage':
            staged = user_installations[step['binding_id']]
            observed['active_after_stage'] = active['binding_id']
        elif action == 'activate':
            _check(staged['binding_id'] == step['binding_id'], 'activation targets the wrong staged binding')
            verdict = fixture['host_conformance']
            _check(all(verdict[key] == staged[key] for key in ('binding_id', 'implementation_sha256')),
                   'conformance identifies another binding')
            _check(verdict['verdict'] in ('match', 'mismatch'), 'fixture conformance did not decide')
            if verdict['verdict'] == 'mismatch':
                return 'ORACLE_REPLACEMENT_NOT_QUALIFIED', {
                    'activation_committed': active == staged, 'active_after_activation': active['binding_id'],
                    'old_run_binding_id': pins['old-run'],
                    'new_binding_attempts': list(pins.values()).count(staged['binding_id'])}
            active = staged
            observed['active_after_activation'] = active['binding_id']
        elif action == 'remove_registration':
            active = None
        elif action == 'reconcile':
            attempt_id = step['attempt_id']
            _check(history[attempt_id]['binding_id'] == pins.pop(attempt_id), 'lost attempt binding')
            reconciled.append(attempt_id)
        else:
            raise ValueError(f'Unknown fixture lifecycle action {action!r}')
    return 'ORACLE_PASS', {**observed, 'attempt_binding_ids': {
        key: row['binding_id'] for key, row in history.items()},
        'reconciled_attempt_ids': reconciled,
        'historical_receipts_retained': bool(history) and all(key in history for key in reconciled),
        'user_installation_preserved': original == user_installations}
