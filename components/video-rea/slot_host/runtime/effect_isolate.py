# SPDX-License-Identifier: AGPL-3.0-only
"""Observe paired filesystem effects from an admitted offline fixture."""

from hashlib import sha256
from typing import Protocol

from video_rea.expose.wire import loads

from ..projections.schema import digest, jcs_bytes, require
from . import process_lifecycle

TOOL_ID = 'effect.isolate'
SUMMARY = 'Reset paired offline fixture state and capture admitted local effects.'
KEYWORDS = ('effect isolation', 'local effect sinks', 'fresh working directory', 'database reset')
_MAX_PLAN_BYTES = 128 * 1024
_MAX_VALUE_LENGTH = 1024
_REF = {'type': 'string', 'maxLength': 512, 'pattern': r'^[A-Za-z0-9][A-Za-z0-9._:@/+-]*(?![\s\S])'}
_VALUE = {'type': 'string', 'maxLength': _MAX_VALUE_LENGTH}
_SIDES = ('original', 'candidate')
_UNOBSERVED = ('database', 'network', 'external_process', 'filesystem_outside_fixture')
_UNAVAILABLE = ('database_reset', 'network_sink', 'external_execution')


def _closed(properties):
    return {'type': 'object', 'additionalProperties': False,
            'properties': properties, 'required': list(properties)}


ARGUMENT = _closed({
    'run_ref': _REF,
    'admitted_sink_profile': _REF,
    'initial_state_ref': _REF,
    'reset_policy': {'const': 'fresh_per_side'},
})


def _artifact_schema(kind):
    return {'type': 'string', 'pattern': rf'^fixture:effect\.isolate:{kind}:sha256:[0-9a-f]{{64}}(?![\s\S])'}


RESULT = _closed({
    'fixture_ref': _artifact_schema('fixture'),
    'captured_effect_refs': {'type': 'array', 'minItems': 2, 'maxItems': 2, 'uniqueItems': True,
                             'items': _artifact_schema('effects')},
    'reset_receipt_refs': {'type': 'array', 'minItems': 2, 'maxItems': 2, 'uniqueItems': True,
                           'items': _artifact_schema('reset')},
    'unobserved_channels': {'const': list(_UNOBSERVED)},
    'unavailable_controls': {'const': list(_UNAVAILABLE)},
})

_WRITES = {'type': 'array', 'maxItems': 8, 'items': _closed({
    'target': {'enum': ['allowed', 'prohibited']}, 'value': _VALUE,
})}
_PLAN = _closed({
    'schema': {'const': 'rea.effect_fixture_plan/v1'},
    'request': ARGUMENT,
    'initial_value': _VALUE,
    'original': _WRITES,
    'candidate': _WRITES,
})


class EffectUnavailable(RuntimeError):
    """The host could not verify fixture admission, observation, or retention."""


class FixtureEffectHost(Protocol):
    """Offline host authority for request-scoped fixture plans and artifacts."""

    def resolve_effect_fixture(self, request: dict, *, request_id: str, max_bytes: int) -> bytes | None:
        """Resolve a host-authored fixture for the exact run, scope and reset inputs."""
        ...

    def retain_effect_isolation(self, request_id: str, artifacts: dict[str, bytes]) -> dict[str, bytes]:
        """Commit the fixture, captures and reset receipts together and read them back."""
        ...


def _assert_absent(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise EffectUnavailable('An unadmitted write target was present in fixture scratch.')


def _observe_value(path):
    with path.open('rb') as stream:
        data = stream.read(4 * _MAX_VALUE_LENGTH + 1)
    if len(data) > 4 * _MAX_VALUE_LENGTH:
        raise EffectUnavailable('Fixture state exceeded its byte limit.')
    return data


def _run_side(initial, writes):
    scratch, created = process_lifecycle.create_run_scratch(None)
    if not created:
        raise EffectUnavailable('Fresh host-owned fixture scratch was unavailable.')
    state, prohibited = scratch / 'state', scratch / 'prohibited'
    try:
        _assert_absent(prohibited)
        with state.open('xb') as stream:
            stream.write(initial)
        before = _observe_value(state)
        if before != initial:
            raise EffectUnavailable('The initial state did not survive fixture seeding.')
        observations = []
        for write in writes:
            target = write['target']
            if target == 'prohibited':
                _assert_absent(prohibited)
                observations.append({'target': target, 'decision': 'denied', 'present': False})
                continue
            value = write['value'].encode('utf-8')
            state.write_bytes(value)
            observed = _observe_value(state)
            if observed != value:
                raise EffectUnavailable('An admitted write could not be observed.')
            observations.append({'target': target, 'decision': 'permitted', 'present': True,
                                 'observed_sha256': sha256(observed).hexdigest()})
        _assert_absent(prohibited)
        effect = {'writes': observations, 'final_state_sha256': sha256(_observe_value(state)).hexdigest(),
                  'prohibited_present': False}
    finally:
        removed, error = process_lifecycle.destroy_run_scratch(scratch, created)
        if error is not None and not isinstance(error, Exception):
            raise error
        if not removed or error is not None:
            raise EffectUnavailable('Fixture scratch removal was not verified.') from error
    return effect, sha256(before).hexdigest()


def _retain_document(artifacts, kind, document):
    raw = jcs_bytes(document)
    ref = f'fixture:{TOOL_ID}:{kind}:sha256:{sha256(raw).hexdigest()}'
    artifacts[ref] = raw
    return ref


def isolate(payload, *, request_id, host: FixtureEffectHost | None = None):
    """Run only the typed fixture effect policy in fresh scratch for each side."""
    require(payload, ARGUMENT, 'Effect isolation payload')
    resolve = getattr(host, 'resolve_effect_fixture', None)
    retain = getattr(host, 'retain_effect_isolation', None)
    if not callable(resolve) or not callable(retain):
        raise EffectUnavailable('Fixture effect authority unavailable (owner-pending: T04).')
    try:
        raw = resolve(dict(payload), request_id=request_id, max_bytes=_MAX_PLAN_BYTES)
        if type(raw) is not bytes or len(raw) > _MAX_PLAN_BYTES:
            raise EffectUnavailable('The admitted fixture plan is unavailable or oversized.')
        plan = loads(raw)
        require(plan, _PLAN, 'Effect fixture plan')
        if plan['request'] != payload:
            raise EffectUnavailable('The fixture plan did not bind the requested scope.')
    except EffectUnavailable:
        raise
    except Exception as error:
        raise EffectUnavailable('The admitted fixture plan could not be verified.') from error

    initial = plan['initial_value'].encode('utf-8')
    initial_digest = sha256(initial).hexdigest()
    input_digest = digest({'request': payload, 'initial_state_sha256': initial_digest})
    plan_sha256 = sha256(raw).hexdigest()
    artifacts = {}
    captures, resets = [], []
    for side in _SIDES:
        try:
            effect, observed_initial = _run_side(initial, plan[side])
        except OSError as error:
            raise EffectUnavailable('The fixture scratch effect could not be verified.') from error
        binding = {'side': side, 'request_id': request_id, 'plan_sha256': plan_sha256}
        captures.append(_retain_document(artifacts, 'effects', {
            'schema': 'rea.fixture_effect_capture/v1', **binding,
            'cases': [{'case_id': 'fixture-file-effects', 'input_digest': input_digest,
                       'observation': {'kind': 'value', 'payload': effect}}],
        }))
        resets.append(_retain_document(artifacts, 'reset', {
            'schema': 'rea.fixture_effect_reset/v1', **binding,
            'initial_state_sha256': observed_initial, 'scratch_removed': True,
        }))
    result = {
        'captured_effect_refs': captures, 'reset_receipt_refs': resets,
        'unobserved_channels': list(_UNOBSERVED), 'unavailable_controls': list(_UNAVAILABLE),
    }
    fixture_ref = _retain_document(artifacts, 'fixture', {
        'schema': 'rea.effect_isolation_fixture/v1', 'request_id': request_id,
        'request': payload, 'plan_sha256': plan_sha256, 'input_digest': input_digest, **result,
    })
    try:
        readback = retain(request_id, dict(artifacts))
    except Exception as error:
        raise EffectUnavailable('Fixture effect artifacts could not be retained.') from error
    if (type(readback) is not dict or readback.keys() != artifacts.keys()
            or any(type(readback[ref]) is not bytes or readback[ref] != value
                   for ref, value in artifacts.items())):
        raise EffectUnavailable('Fixture effect artifacts lack exact readback.')
    return {'fixture_ref': fixture_ref, **result}
