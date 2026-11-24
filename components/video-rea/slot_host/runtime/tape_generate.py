# SPDX-License-Identifier: AGPL-3.0-only
"""Generate and retain ordered, typed input cases for offline fixture replay.

Tapes are canonical JSON arrays of operation argument objects. A fixture host
authorizes and resolves the exact schema and, for from-tests, a retained capture
of an admitted workload. The capture is data supplied by the host. This module
has no workload runner or production retention client.
"""

import hashlib
import itertools
import random
from typing import Protocol

from video_rea.expose.wire import loads

from ..projections.schema import PublicationError, accepts, digest, jcs_bytes, require

TOOL_ID = 'tape.generate'
SUMMARY = 'Generate a typed input tape or extract cases from an admitted fixture test capture.'
KEYWORDS = ('input tape', 'edge cases', 'from-tests', 'seeded generation')
_MAX_SAFE_INTEGER = (1 << 53) - 1
_MAX_CASES = 64
_MAX_CASE_BYTES = 4096
_MAX_SCHEMA_BYTES = 16384
_MAX_CAPTURE_BYTES = 262144
_MAX_CAPTURE_CALLS = 1024
_MAX_SKIPPED_EXPORTS = 128
_SCHEMA_ANNOTATIONS = {'$id', '$schema', '$comment', 'title', 'description', 'default', 'examples'}
_MISSING = object()
_DRAFT = 'https://json-schema.org/draft/2020-12/schema'

_REF = {'type': 'string', 'maxLength': 512, 'pattern': r'^[A-Za-z0-9][A-Za-z0-9._:@/+-]*(?![\s\S])'}
_EXPORT = {**_REF, 'maxLength': 128}
_SHA = {'type': 'string', 'pattern': r'^[0-9a-f]{64}(?![\s\S])'}
_UINT = {'type': 'integer', 'minimum': 0, 'maximum': _MAX_SAFE_INTEGER}
_MODE = {'enum': ['synthetic', 'from-tests']}
_SKIPPED = {'type': 'array', 'maxItems': _MAX_SKIPPED_EXPORTS, 'uniqueItems': True, 'items': _EXPORT}


def _closed(properties, *required):
    return {'type': 'object', 'additionalProperties': False, 'properties': properties,
            'required': list(required or properties)}


ARGUMENT = {
    **_closed({
        'operation_schema_ref': _REF,
        'mode': _MODE,
        'generation_budget': {**_UINT, 'minimum': 1, 'maximum': _MAX_CASES},
        'seed': _UINT,
        'salt': {'type': 'string', 'maxLength': 128},
        'admitted_test_workload_ref': _REF,
    }, 'operation_schema_ref', 'mode', 'generation_budget', 'seed'),
    'if': {'properties': {'mode': {'const': 'from-tests'}}},
    'then': {'required': ['admitted_test_workload_ref']},
    'else': {'not': {'required': ['admitted_test_workload_ref']}},
}


def _artifact_prefix(kind):
    return f'fixture:tape.generate:{kind}:sha256:'


def _artifact_schema(kind):
    return {'type': 'string', 'pattern': rf'^fixture:tape\.generate:{kind}:sha256:[0-9a-f]{{64}}(?![\s\S])'}


RESULT = _closed({
    'tape_ref': _artifact_schema('tape'),
    'generation_log_ref': _artifact_schema('log'),
    'skipped_exports': _SKIPPED,
    'origin_by_case': {
        'type': 'object', 'minProperties': 1, 'maxProperties': _MAX_CASES,
        'propertyNames': {'pattern': r'^case-[1-9][0-9]*(?![\s\S])'},
        'additionalProperties': {'oneOf': [
            _closed({'mode': {'const': 'synthetic'}}),
            _closed({'mode': {'const': 'from-tests'}, 'export': _EXPORT,
                     'capture_index': {**_UINT, 'minimum': 1, 'maximum': _MAX_CAPTURE_CALLS}}),
        ]},
    },
})

_CAPTURE = _closed({
    'schema': {'const': 'rea.fixture_test_capture/v1'},
    'workload_ref': _REF,
    'operation_schema_ref': _REF,
    'operation_schema_sha256': _SHA,
    'calls': {'type': 'array', 'minItems': 1, 'maxItems': _MAX_CAPTURE_CALLS,
              'items': _closed({'export': _EXPORT, 'arguments': {'type': 'object'}})},
    'skipped_exports': _SKIPPED,
})


class TapeUnavailable(RuntimeError):
    """The authorized fixture source or retention could not be verified."""


class FixtureTapeHost(Protocol):
    """Request-scoped authority injected by the offline control owner.

    Schema and workload references are opaque. A capture binds the requested
    workload and schema reference to the JCS SHA-256 of the admitted schema.
    Retention must preserve both byte snapshots together under the request's
    authority.
    """

    def resolve_operation_schema(self, ref: str, *, request_id: str, max_bytes: int) -> bytes | None:
        """Resolve only an admitted operation argument schema."""
        ...

    def resolve_test_capture(self, ref: str, *, request_id: str, max_bytes: int) -> bytes | None:
        """Resolve only the captured inputs of an admitted fixture workload."""
        ...

    def retain_generated_tape(self, request_id: str, artifacts: dict[str, bytes]) -> dict[str, bytes]:
        """Commit the tape and log together and read them back by their references."""
        ...


def _invalid_schema():
    return PublicationError('SCHEMA_INVALID', 'Unsupported or invalid operation argument schema.')


def _decode_fixture(resolve, ref, request_id, max_bytes):
    try:
        raw = resolve(ref, request_id=request_id, max_bytes=max_bytes)
        if type(raw) is not bytes or len(raw) > max_bytes:
            raise TapeUnavailable('Fixture reference is unavailable or exceeds its byte budget.')
        return loads(raw), hashlib.sha256(raw).hexdigest()
    except TapeUnavailable:
        raise
    except Exception as error:
        raise TapeUnavailable('Could not resolve an authorized fixture reference.') from error


def _size_limits(schema, minimum, maximum, default_maximum, hard_maximum):
    lower = schema.get(minimum, 0)
    upper = schema.get(maximum, max(lower, default_maximum)) if type(lower) is int else None
    if type(lower) is not int or type(upper) is not int or not 0 <= lower <= upper <= hard_maximum:
        raise _invalid_schema()
    return lower, upper


def _case_bytes(value):
    encoded = jcs_bytes(value)
    if len(encoded) > _MAX_CASE_BYTES:
        raise PublicationError('SCHEMA_INVALID', 'An input case exceeds the byte budget.')
    return encoded


def _distinct_bounded(values, limit):
    seen = set()
    cases = []
    for value in values:
        key = _case_bytes(value)
        if key not in seen:
            seen.add(key)
            cases.append(value)
            if len(cases) == limit:
                break
    return cases


def _combinations(axes):
    """Exercise a boundary on each axis before exploring combined variations."""
    baseline = tuple(values[0] for values in axes)
    yield baseline
    for offset in range(1, max(map(len, axes), default=1)):
        for index, axis in enumerate(axes):
            if offset < len(axis):
                yield (*baseline[:index], axis[offset], *baseline[index + 1:])
    yield from itertools.product(*axes)


def _edge_cases(schema, limit, rng, depth=0):
    """Compile a bounded subset of Draft 2020-12 and enumerate its typed edges."""
    if type(schema) is not dict or depth > 5:
        raise _invalid_schema()
    kind = schema.get('type')
    keywords = {
        'object': {'properties', 'required', 'additionalProperties'},
        'array': {'items', 'minItems', 'maxItems'},
        'integer': {'minimum', 'maximum'},
        'number': {'minimum', 'maximum'},
        'string': {'minLength', 'maxLength'},
        'boolean': set(),
        'null': set(),
        None: set(),
    }
    if (kind is not None and type(kind) is not str) or kind not in keywords:
        raise _invalid_schema()
    if set(schema) - {'type', 'const', 'enum'} - keywords[kind] - _SCHEMA_ANNOTATIONS:
        raise _invalid_schema()
    if '$schema' in schema and schema['$schema'] != _DRAFT:
        raise _invalid_schema()
    if 'const' in schema or 'enum' in schema:
        # Literal alternatives are complete. Constraints on their shape must
        # live in the literals themselves, so no nested keyword is ignored.
        if keywords[kind] & schema.keys() or 'const' in schema and 'enum' in schema:
            raise _invalid_schema()
        choices = [schema['const']] if 'const' in schema else schema['enum']
        if (type(choices) is not list or not 1 <= len(choices) <= 128
                or not all(accepts(choice, schema) for choice in choices)):
            raise _invalid_schema()
        values = list(choices)
    elif kind == 'object':
        properties = schema.get('properties')
        required = schema.get('required', [])
        if (schema.get('additionalProperties') is not False or type(properties) is not dict
                or len(properties) > 16 or type(required) is not list
                or any(type(key) is not str or key not in properties for key in required)
                or len(required) != len(set(required))):
            raise _invalid_schema()
        keys = sorted(properties)
        axes = []
        for key in keys:
            options = _edge_cases(properties[key], limit, rng, depth + 1)
            if key not in required:
                options = [_MISSING, *options]
                rng.shuffle(options)
            axes.append(options)
        return _distinct_bounded(
            ({key: value for key, value in zip(keys, row) if value is not _MISSING}
             for row in _combinations(axes)), limit)
    elif kind == 'array':
        lower, upper = _size_limits(schema, 'minItems', 'maxItems', 3, 8)
        if 'items' not in schema:
            raise _invalid_schema()
        options = _edge_cases(schema['items'], limit, rng, depth + 1)
        lengths = list(dict.fromkeys((lower, min(lower + 1, upper), upper)))
        rng.shuffle(lengths)
        # Interleave lengths so the smallest budget that covers all lengths
        # sees both boundaries, independently of the number of item variants.
        streams = [_combinations([options] * length) for length in lengths]
        return _distinct_bounded((list(row) for group in itertools.zip_longest(*streams)
                                  for row in group if row is not None), limit)
    elif kind in ('integer', 'number'):
        number_types = (int,) if kind == 'integer' else (int, float)
        for key in ('minimum', 'maximum'):
            if key in schema and (type(schema[key]) not in number_types
                                  or not -_MAX_SAFE_INTEGER <= schema[key] <= _MAX_SAFE_INTEGER):
                raise _invalid_schema()
        upper = schema.get('maximum', 1)
        lower = schema.get('minimum', min(-1, max(-_MAX_SAFE_INTEGER, upper - 2)))
        upper = schema.get('maximum', max(1, min(_MAX_SAFE_INTEGER, lower + 2)))
        if lower > upper:
            raise _invalid_schema()
        midpoint = (lower + upper) // 2 if kind == 'integer' else lower + (upper - lower) / 2
        values = [value for value in (lower, upper, 0, -1, 1, midpoint) if lower <= value <= upper]
    elif kind == 'string':
        lower, upper = _size_limits(schema, 'minLength', 'maxLength', 8, 128)
        values = [char * length for length in (lower, min(lower + 1, upper), upper)
                  for char in ('a', 'é', '\n')]
    elif kind == 'boolean':
        values = [False, True]
    elif kind == 'null':
        values = [None]
    else:
        raise _invalid_schema()
    values = _distinct_bounded(values, len(values))
    rng.shuffle(values)
    return values[:limit]


def _capture_cases(capture, payload, schema, schema_digest):
    require(capture, _CAPTURE, 'Admitted fixture test capture')
    if (capture['workload_ref'] != payload['admitted_test_workload_ref']
            or capture['operation_schema_ref'] != payload['operation_schema_ref']
            or capture['operation_schema_sha256'] != schema_digest):
        raise TapeUnavailable('Fixture capture is bound to a different workload or operation schema.')
    skipped = sorted(capture['skipped_exports'])
    skipped_set = set(skipped)
    seen = set()
    cases, origins = [], []
    for index, call in enumerate(capture['calls'], 1):
        if call['export'] in skipped_set:
            raise PublicationError('SCHEMA_INVALID', 'A captured export is also marked skipped.')
        arguments = call['arguments']
        key = call['export'], _case_bytes(arguments)
        require(arguments, schema, 'Captured operation arguments')
        if key not in seen and len(cases) < payload['generation_budget']:
            seen.add(key)
            cases.append(arguments)
            origins.append({'mode': 'from-tests', 'export': call['export'], 'capture_index': index})
    return cases, origins, skipped


def generate(payload, *, request_id, host: FixtureTapeHost | None = None):
    """Issue references only after exact readback of both fixture artifacts."""
    require(payload, ARGUMENT, 'Tape generation payload')
    if host is None or not all(callable(getattr(host, method, None)) for method in (
            'resolve_operation_schema', 'retain_generated_tape')):
        raise TapeUnavailable('Fixture tape authority unavailable (owner-pending: T04).')

    schema_ref = payload['operation_schema_ref']
    schema, _ = _decode_fixture(host.resolve_operation_schema, schema_ref, request_id, _MAX_SCHEMA_BYTES)
    if type(schema) is not dict or schema.get('$id') != schema_ref:
        raise TapeUnavailable('Fixture operation schema identity was not verified.')
    if schema.get('type') != 'object':
        raise _invalid_schema()
    schema_digest = digest(schema)
    # Use the caller's seed directly in the unsalted case. An explicit salt
    # changes the seed domain while retaining deterministic replay of a request.
    seed = int(payload['seed'])
    if 'salt' in payload:
        seed = int(digest([seed, payload['salt']]), 16)
    mode = payload['mode']
    log_metadata = {}
    if mode == 'synthetic':
        cases = _edge_cases(schema, int(payload['generation_budget']), random.Random(seed))
        origins, skipped = [{'mode': mode} for _ in cases], []
        for arguments in cases:
            require(arguments, schema, 'Generated operation arguments')
    else:
        resolve_capture = getattr(host, 'resolve_test_capture', None)
        if not callable(resolve_capture):
            raise TapeUnavailable('Admitted fixture test capture unavailable (owner-pending: T04).')
        _edge_cases(schema, 1, random.Random(0))  # Reject unsupported keywords before accepting captures.
        capture, capture_digest = _decode_fixture(resolve_capture, payload['admitted_test_workload_ref'],
                                                 request_id, _MAX_CAPTURE_BYTES)
        cases, origins, skipped = _capture_cases(capture, payload, schema, schema_digest)
        log_metadata['capture_sha256'] = capture_digest

    tape = jcs_bytes(cases)
    tape_ref = _artifact_prefix('tape') + hashlib.sha256(tape).hexdigest()
    origin_by_case = {f'case-{index}': origin for index, origin in enumerate(origins, 1)}
    log = jcs_bytes({'schema': 'rea.tape_generation_log/v1', 'request': payload,
                     'operation_schema_sha256': schema_digest, 'tape_ref': tape_ref,
                     'skipped_exports': skipped, 'origin_by_case': origin_by_case, **log_metadata})
    log_ref = _artifact_prefix('log') + hashlib.sha256(log).hexdigest()
    artifacts = {tape_ref: tape, log_ref: log}
    try:
        readback = host.retain_generated_tape(request_id, dict(artifacts))
    except Exception as error:
        raise TapeUnavailable('Fixture tape artifacts could not be retained.') from error
    if (type(readback) is not dict or readback.keys() != artifacts.keys()
            or any(type(readback[ref]) is not bytes or readback[ref] != value
                   for ref, value in artifacts.items())):
        raise TapeUnavailable('Fixture tape artifacts lack exact readback.')
    return {'tape_ref': tape_ref, 'generation_log_ref': log_ref,
            'skipped_exports': skipped, 'origin_by_case': origin_by_case}
