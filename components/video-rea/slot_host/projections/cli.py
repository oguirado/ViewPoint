# SPDX-License-Identifier: AGPL-3.0-only
"""Generated slot argument parsing and compatibility CLI entry point."""

import argparse
import os
import stat
import sys
import uuid

from .mcp_lane import SlotProjector, control_reply
from .project import _REQUEST_ID_RE, PublicationError, control_envelope, digest, jcs_bytes

MAX_ARGS_BYTES = 262144
_EXIT = {'HOLD': 10, 'HUMAN_NEEDED': 10, 'NO_LEASE': 11, 'REQUEST_ID_CONFLICT': 12,
         'UNCERTAIN_OUTCOME': 13, 'SCHEMA_INVALID': 14, 'PERCEPTION_DENIED': 15,
         'POOL_TIMEOUT': 15, 'PROFILE_NOT_ADMITTED': 16, 'AVAILABILITY_NOT_INSTALLED': 16,
         'AVAILABILITY_BLOCKED': 16, 'AVAILABILITY_PARKED': 16, 'LIVE_EXECUTION_PARKED': 16}


def _read_descriptor(fd):
    stat_result = os.fstat(fd)
    if not stat.S_ISREG(stat_result.st_mode):
        raise PublicationError('SCHEMA_INVALID', 'Arguments require a regular opened descriptor.')
    if stat_result.st_size > MAX_ARGS_BYTES:
        raise PublicationError('SCHEMA_INVALID', 'Argument file exceeds the byte limit.')
    data = bytearray()
    while len(data) <= MAX_ARGS_BYTES:
        chunk = os.read(fd, min(65536, MAX_ARGS_BYTES + 1 - len(data)))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
    raise PublicationError('SCHEMA_INVALID', 'Argument file exceeds the byte limit.')


def _read_stdin_bounded():
    """Read stdin bounded to MAX_ARGS_BYTES before decoding.

    Stdin may be a pipe or FIFO (not a regular file), so the opened-
    descriptor regular-file check does not apply. Enforce only the byte
    cap; strict UTF-8 handling happens in _load_args via wire.loads.
    """
    stream = getattr(sys.stdin, 'buffer', sys.stdin)
    data = bytearray()
    while len(data) <= MAX_ARGS_BYTES:
        chunk = stream.read(min(65536, MAX_ARGS_BYTES + 1 - len(data)))
        if isinstance(chunk, str):
            chunk = chunk.encode('utf-8')
        if not isinstance(chunk, (bytes, bytearray)):
            raise PublicationError('SCHEMA_INVALID', 'Stdin must yield bytes.')
        if not chunk:
            return bytes(data)
        data.extend(chunk)
    raise PublicationError('SCHEMA_INVALID', 'Argument input exceeds the byte limit.')


def _read_bounded_file(path):
    if path == '-':
        return _read_stdin_bounded()
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        return _read_descriptor(fd)
    finally:
        os.close(fd)


def _load_args(encoded):
    from video_rea.expose import wire

    try:
        raw = encoded.encode('utf-8') if type(encoded) is str else encoded
        if len(raw) > MAX_ARGS_BYTES:
            raise PublicationError('SCHEMA_INVALID', 'Arguments exceed the byte limit.')
        arguments = wire.loads(raw)
        jcs_bytes(arguments)
    except (ValueError, TypeError, RecursionError) as error:
        if isinstance(error, PublicationError):
            raise
        raise PublicationError('SCHEMA_INVALID', 'Arguments must be finite, strict UTF-8 JSON.') from error
    if type(arguments) is not dict:
        raise PublicationError('SCHEMA_INVALID', 'Expected a JSON argument object.')
    return arguments


def _load_args_or_file(encoded, path):
    if path is not None:
        return _load_args(_read_bounded_file(path))
    return _load_args(encoded) if encoded is not None else None


def _merge(arguments, field, value):
    if value is None:
        return
    if field in arguments and jcs_bytes(arguments[field]) != jcs_bytes(value):
        raise PublicationError('SCHEMA_INVALID', f'CLI value disagrees with argument {field}.')
    arguments[field] = value


class _SlotParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, allow_abbrev=False, **kwargs)

    def error(self, message):
        raise PublicationError('SCHEMA_INVALID', message)


def _add_argument_options(command):
    inputs = command.add_mutually_exclusive_group()
    inputs.add_argument('--args')
    inputs.add_argument('--args-file')
    command.add_argument('--request-id')
    command.add_argument('--dry-run', action='store_true', default=None)


def add_slot_commands(commands):
    """Register the generated namespace's subcommands on a REA parser."""
    for name in ('list', 'describe', 'call', 'conform', 'inspect', 'mini'):
        command = commands.add_parser(name)
        _add_argument_options(command)
        if name in ('describe', 'call', 'conform', 'inspect'):
            command.add_argument('slot_id')
        if name == 'describe':
            command.add_argument('op_id', nargs='?')
            command.add_argument('--op-id', dest='op_flag')
        if name == 'call':
            command.add_argument('op_id')
            command.add_argument('--strict', action='store_true')
        if name == 'conform':
            command.add_argument('case_id', nargs='?')
        if name in ('conform', 'inspect'):
            command.add_argument('--installation')
        if name == 'mini':
            command.add_argument('mini_id')
        if name == 'list':
            command.add_argument('--all', action='store_true', default=None)
            command.add_argument('--json', action='store_true')


def _route_and_arguments(args, args_document, issue_request_id):
    name = args.command
    if args_document is not None and type(args_document) is not dict:
        raise PublicationError('SCHEMA_INVALID', 'Argument document must be an object.')
    if args.args == '<json>' and args_document is not None:
        document = args_document
    else:
        document = _load_args_or_file(args.args, args.args_file)
        if document is None:
            document = args_document
        elif args_document is not None and jcs_bytes(document) != jcs_bytes(args_document):
            raise PublicationError('SCHEMA_INVALID', 'CLI document and supplied arguments differ.')
    if name in ('call', 'mini') and document is None:
        raise PublicationError('SCHEMA_INVALID', 'The selected command requires an argument document.')
    arguments = dict(document) if document is not None else {}
    _merge(arguments, 'request_id', args.request_id)
    _merge(arguments, 'dry_run', args.dry_run)
    if document is None and 'request_id' not in arguments and issue_request_id is not None:
        arguments['request_id'] = issue_request_id()
    if name == 'list':
        _merge(arguments, 'all', args.all)
        return 'host.slot.list', arguments
    if name == 'inspect':
        _merge(arguments, 'slot_id', args.slot_id)
        _merge(arguments, 'installation_ref', args.installation)
        return 'host.slot.inspect', arguments
    if name == 'mini':
        if args.mini_id == 'slot.inspect':
            raise PublicationError('SCHEMA_INVALID', 'Use slot inspect for slot inspection.')
        return f'host.{args.mini_id}', arguments
    route = f'ext.{args.slot_id}'
    if name == 'call':
        if args.op_id == 'conform':
            raise PublicationError('SCHEMA_INVALID', 'Use slot conform for qualification.')
        return f'{route}.{args.op_id}', arguments
    if name == 'describe':
        _merge(arguments, 'op_id', args.op_id)
        _merge(arguments, 'op_id', args.op_flag)
        return route, arguments
    _merge(arguments, 'installation_ref', args.installation)
    suffix = f'.{args.case_id}' if args.case_id else ''
    return f'{route}.conform{suffix}', arguments


def _new_cli_request_id():
    return 'cli-' + uuid.uuid4().hex


def _parse_slot_options(argv):
    if type(argv) is not list or any(type(word) is not str for word in argv):
        raise PublicationError('SCHEMA_INVALID', 'CLI argv must be a list of strings.')
    words = argv[1:] if argv and argv[0] == 'slot' else argv
    parser = _SlotParser(prog='rea slot')
    add_slot_commands(parser.add_subparsers(dest='command', required=True))
    return parser.parse_args(words)


def _checked_slot_arguments(parsed, args_document, issue_request_id):
    try:
        route, arguments = _route_and_arguments(parsed, args_document, issue_request_id)
    except (OSError, ValueError, RecursionError) as error:
        if isinstance(error, PublicationError):
            raise
        raise PublicationError('SCHEMA_INVALID', str(error)) from error
    return route, arguments, getattr(parsed, 'strict', False)


def parse_slot_command(argv, *, args_document=None, request_id=None, dry_run=None, issue_request_id=None):
    if issue_request_id is None:
        issue_request_id = _new_cli_request_id
    route, arguments, strict = _checked_slot_arguments(
        _parse_slot_options(argv), args_document, issue_request_id)
    _merge(arguments, 'request_id', request_id)
    _merge(arguments, 'dry_run', dry_run)
    return route, arguments, strict


def _exit_for(reply, strict):
    if reply['ok'] is False:
        return _EXIT.get(reply['code'], 1)
    result = reply.get('result')
    if isinstance(result, dict) and result.get('kind') == 'slot_exchange':
        if result['rc']:
            return result['rc']
        if strict:
            return {'mismatch': 5, 'abstain': 3}.get((result.get('verdict') or {}).get('result'), 0)
    return 0


def main(argv=None, *, projector=None, dispatch=None, context=None, manifest=None, bundle=None,
         binding=None, policy=None, registry=None, issue_request_id=None,
         authenticated_context=None, vision_admission=None, perception_ceiling=None,
         current_licence=None, **publication):
    arguments = {}
    route = None
    options = None
    if issue_request_id is None:
        issue_request_id = _new_cli_request_id
    try:
        options = _parse_slot_options(list(sys.argv[1:] if argv is None else argv))
        route, arguments, strict = _checked_slot_arguments(options, None, issue_request_id)
        if projector is None:
            if manifest is None or bundle is None:
                raise PublicationError('AVAILABILITY_PARKED', 'The verified publication is unavailable.')
            projector = SlotProjector(manifest, bundle, binding=binding, policy=policy, registry=registry,
                                      dispatch=dispatch, context=context,
                                      authenticated_context=authenticated_context,
                                      vision_admission=vision_admission,
                                      perception_ceiling=perception_ceiling,
                                      current_licence=current_licence, **publication)
        reply = projector.invoke(route, arguments, context=context,
                                 authenticated_context=authenticated_context,
                                 vision_admission=vision_admission,
                                 perception_ceiling=perception_ceiling,
                                 current_licence=current_licence)
    except PublicationError as error:
        print(str(error), file=sys.stderr)
        request_id = arguments.get('request_id', options.request_id if options is not None else None)
        if type(request_id) is str and _REQUEST_ID_RE.fullmatch(request_id):
            effective = None
            if route is not None:
                try:
                    fallback_env = control_envelope(manifest if manifest is not None else {}, route, arguments, context=context)
                    effective = digest(fallback_env)
                except Exception:
                    effective = None
            print(jcs_bytes(control_reply(request_id, error.code, ok=False, effective=effective)).decode('utf-8'))
        return _EXIT.get(error.code, 14)
    if reply.get('code') == 'LIVE_EXECUTION_PARKED':
        for detail in reply.get('not_verified', ()):
            print(detail, file=sys.stderr)
    print(jcs_bytes(reply).decode('utf-8'))
    return _exit_for(reply, strict)
