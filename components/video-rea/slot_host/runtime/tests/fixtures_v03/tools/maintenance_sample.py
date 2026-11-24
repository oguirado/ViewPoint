#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Run or verify a fixed maintenance sample on an explicitly bound command guest."""

import argparse
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from .arm64_capsules import private_dir, read
from .check_contracts import Validator, loads_strict

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = Validator(ROOT)
SCHEMA = 'maintenance_sample_receipt/v0.2'
RECEIPT = CONTRACTS.load('maintenance_sample_receipt.schema.json')
COMMANDS = {'base': "printf '%s' hello", 'candidate': "printf '%s\\n' hello"}
EXPECTED = {'base': b'hello', 'candidate': b'hello\n'}
TIMEOUT_MS = 10000


class SampleError(ValueError):
    """A sample precondition, oracle or evidence check failed."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, allow_nan=False) + '\n').encode()


FIXTURE_DIGEST = digest(json_bytes({
    'commands': COMMANDS,
    'expected_sha256': {key: digest(value) for key, value in EXPECTED.items()},
    'expected_stderr_sha256': digest(b''),
    'required_exit_code': 0,
    'timeout_ms': TIMEOUT_MS,
}))


def check_binding(binding):
    CONTRACTS.validate(binding, RECEIPT['$defs']['binding'])


def require_ready(guest, binding):
    session = guest.session
    if (guest.uncertain or guest.closed or session is None
            or session.get('phase') != 'ready' or session.get('ownership') != 'held'
            or any(session.get(key) != value for key, value in binding.items())):
        raise SampleError('guest binding unavailable or changed')


def sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(path, content):
    """Commit an evidence file without replacing an existing record."""
    temp = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, path, follow_symlinks=False)
    finally:
        temp.unlink()
    sync_directory(path.parent)


def publish_receipt(path, receipt):
    CONTRACTS.validate(receipt, RECEIPT)
    publish(path, json_bytes(receipt))


def payload_path(directory, phase, stream):
    return directory / f'{phase}.{stream}.b64.json'


def write_payload(path, content):
    # Preserve exact stream bytes, including empty streams, in the bounded file format.
    publish(path, json_bytes({'base64': base64.b64encode(content).decode('ascii')}))


def read_payload(path):
    document = loads_strict(read(path, limit=88000, private=True))
    if not isinstance(document, dict) or set(document) != {'base64'} or not isinstance(document['base64'], str):
        raise SampleError('invalid payload document')
    encoded = document['base64']
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise SampleError('invalid payload encoding') from error
    if base64.b64encode(content).decode('ascii') != encoded or len(content) > 65536:
        raise SampleError('invalid payload bytes or size')
    return content


def run_phase(guest, binding, directory, attempt_id, phase):
    require_ready(guest, binding)
    start = dict(schema=SCHEMA, attempt_id=attempt_id, phase=phase, state='started',
                 binding=binding, fixture_sha256=FIXTURE_DIGEST)
    publish_receipt(directory / f'{phase}.start.json', start)
    require_ready(guest, binding)
    result = guest.run(COMMANDS[phase], timeout_ms=TIMEOUT_MS)
    require_ready(guest, binding)
    stdout, stderr = result['stdout'], result['stderr']
    if not isinstance(stdout, bytes) or not isinstance(stderr, bytes) or len(stdout) + len(stderr) > 65536:
        raise SampleError('unexpected guest output format or size')
    terminal = {**start, 'state': 'observed', 'stdout_sha256': digest(stdout), 'stderr_sha256': digest(stderr)}
    for key in ('job_id', 'exit_code', 'signal', 'cause', 'output_complete'):
        terminal[key] = result[key]
    CONTRACTS.validate(terminal, RECEIPT)
    write_payload(payload_path(directory, phase, 'stdout'), stdout)
    write_payload(payload_path(directory, phase, 'stderr'), stderr)
    publish_receipt(directory / f'{phase}.end.json', terminal)


def exists(path):
    return path.exists() or path.is_symlink()


def read_phase(directory, phase):
    start_path = directory / f'{phase}.start.json'
    end_path = directory / f'{phase}.end.json'
    payloads = [payload_path(directory, phase, stream) for stream in ('stdout', 'stderr')]
    if not exists(start_path):
        if any(exists(path) for path in [end_path, *payloads]):
            raise SampleError('phase evidence without intent')
        return 'not_started', None
    start = loads_strict(read(start_path, private=True))
    CONTRACTS.validate(start, RECEIPT)
    if start['state'] != 'started' or start['attempt_id'] != directory.name or start['phase'] != phase or start['fixture_sha256'] != FIXTURE_DIGEST:
        raise SampleError('receipt does not identify this attempt and fixture')
    if not exists(end_path):
        return 'unknown', start
    result = loads_strict(read(end_path, private=True))
    CONTRACTS.validate(result, RECEIPT)
    if result['state'] != 'observed' or any(result[key] != value for key, value in start.items() if key != 'state'):
        raise SampleError('terminal receipt does not match dispatch')
    stdout, stderr = map(read_payload, payloads)
    if len(stdout) + len(stderr) > 65536 or (digest(stdout), digest(stderr)) != (result['stdout_sha256'], result['stderr_sha256']):
        raise SampleError('payload does not match receipt')
    if (result['cause'], result['exit_code'], result['signal'], result['output_complete']) != ('exit', 0, None, True):
        return 'command_failed', result
    if stdout == EXPECTED[phase] and not stderr:
        return ('reproduced' if phase == 'base' else 'passed'), result
    if phase == 'base' and stdout == EXPECTED['candidate'] and not stderr:
        return 'not_reproduced', result
    return 'wrong_output', result


def verify(directory):
    directory = private_dir(directory)
    base, base_receipt = read_phase(directory, 'base')
    candidate, candidate_receipt = read_phase(directory, 'candidate')
    if candidate_receipt is not None and (base != 'reproduced'
            or base_receipt['binding'] != candidate_receipt['binding']
            or base_receipt['job_id'] == candidate_receipt.get('job_id')):
        raise SampleError('candidate evidence lacks a matching completed base and a distinct job')
    return {'base': base, 'candidate': candidate,
            'comparison_passed': base == 'reproduced' and candidate == 'passed',
            'verification_scope': 'local_receipt_consistency',
            'retirement': 'operator_confirmation_required'}


def evidence_parent(directory):
    parent = private_dir(directory)
    if parent.is_relative_to(ROOT):
        raise SampleError('evidence directory must be outside the checkout')
    return parent


def run_sample(guest, binding, directory):
    parent = evidence_parent(directory)
    check_binding(binding)
    require_ready(guest, binding)
    attempt_id = uuid.uuid4().hex
    attempt = parent / attempt_id
    attempt.mkdir(mode=0o700)
    sync_directory(parent)
    print(f'Evidence directory {attempt}', file=sys.stderr, flush=True)
    run_phase(guest, binding, attempt, attempt_id, 'base')
    if read_phase(attempt, 'base')[0] == 'reproduced':
        run_phase(guest, binding, attempt, attempt_id, 'candidate')
    return verify(attempt)


def run_live(directory):
    env = dict(os.environ)
    if not all(env.get(key) for key in ('MV_CORE_CONFIG', 'XDG_RUNTIME_DIR', 'MV_TASK_BINDING')):
        raise SampleError('an explicit prepared MicroVM attachment and binding are required')
    binding = loads_strict(env['MV_TASK_BINDING'])
    check_binding(binding)
    parent = evidence_parent(directory)
    from .mw_run_backend import connect

    with connect(env) as client:
        if not client.rewind.wait_ready(timeout=5):
            raise SampleError('prepared guest readiness unavailable')
        result = run_sample(client.guest_commands, binding, parent)
    return {**result, 'transport': 'prepared_sdk_session'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('operation', choices=('run', 'verify'))
    parser.add_argument('directory', type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_live(args.directory) if args.operation == 'run' else verify(args.directory)
    except (OSError, ValueError, RuntimeError) as error:
        print(f'Maintenance sample stopped: {error}', file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result['comparison_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
