"""Registry-backed mvctl verbs for a host-provisioned C1 dispatcher."""

import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import stat
import sys

from control.core.control import InvalidRequest, LaneIdentity, canonical_bytes

from .._json import unique_object as _unique_object
from .verbs import REQUEST_FILE_FLAG, TRANSPORT_FLAGS, registration_manifest


@dataclass(frozen=True)
class TransportBinding:
    """Transport settings and authenticated identity supplied by the host."""

    socket: str
    session_id: str
    core_instance: str
    identity: LaneIdentity
    timeout_ms: int
    max_line_bytes: int
    max_reply_bytes: int

    def matches(self, args):
        return (
            os.path.isabs(args.socket)
            and args.socket == self.socket
            and args.session_id == self.session_id
            and args.core_instance == self.core_instance
            and args.agent_instance == self.identity.agent_instance
            and args.timeout_ms == self.timeout_ms
            and args.max_line_bytes <= self.max_line_bytes
            and args.max_reply_bytes <= self.max_reply_bytes
        )


class _InvalidUsage(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise _InvalidUsage("usage_error")


class _Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if hasattr(namespace, self.dest):
            parser.error("duplicate option")
        setattr(namespace, self.dest, True if self.nargs == 0 else values)


def _positive_int(value):
    if not re.fullmatch(r"[1-9][0-9]{0,9}", value):
        raise argparse.ArgumentTypeError("expected a positive decimal integer")
    return int(value)


def build_parser(registry):
    manifest = registration_manifest(registry)
    parser = _Parser(prog="mvctl", allow_abbrev=False, argument_default=argparse.SUPPRESS)
    parser.add_argument("verb", choices=sorted({row["verb"] for row in manifest["verbs"]}))
    for flag in TRANSPORT_FLAGS:
        parser.add_argument(flag, required=True, action=_Once,
                            type=_positive_int if flag.endswith(("-ms", "-bytes")) else str)
    parser.add_argument(REQUEST_FILE_FLAG, required=True, action=_Once, type=Path)
    parser.add_argument(manifest["dry_run_flag"], nargs=0, action=_Once)
    return parser


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def _read_request(path, byte_limit):
    if str(path).lstrip().startswith(("{", "[")):
        raise ValueError("expected a file path")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("expected a regular file")
        data = handle.read(byte_limit + 1)
    if len(data) > byte_limit:
        raise ValueError("request exceeds --max-line-bytes")
    request = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                         parse_constant=_finite_float, parse_float=_finite_float)
    if not isinstance(request, dict):
        raise ValueError("expected a JSON object")
    return request


def _refuse(stderr, code, status):
    print(json.dumps({"code": code}, separators=(",", ":")), file=stderr)
    return status


def run(argv, dispatcher, binding, *, stdout=None, stderr=None):
    """Run a registered verb. The host owns transport deadlines and C1 policy."""
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    parser = build_parser(dispatcher.registry)
    try:
        args = parser.parse_args(argv)
    except _InvalidUsage as exc:
        return _refuse(stderr, str(exc), 2)
    except SystemExit as exc:
        return exc.code

    if not binding.matches(args):
        return _refuse(stderr, "usage_error", 2)
    try:
        request = _read_request(args.json_arg, args.max_line_bytes)
    except (OSError, ValueError, RecursionError):
        return _refuse(stderr, "input_error", 2)

    if type(request.get("dry_run")) is not bool:
        return _refuse(stderr, -32602, 2)
    for name in ("context", "args"):
        fields = request.get(name)
        if not isinstance(fields, dict) or fields.get("session_id", args.session_id) != args.session_id:
            return _refuse(stderr, -32602, 2)
    if getattr(args, "dry_run", False):
        request["dry_run"] = True

    try:
        reply = dispatcher.dispatch_cli(args.verb, request, binding.identity)
    except InvalidRequest as exc:
        return _refuse(stderr, exc.code, 2 if isinstance(exc.code, int) else 3)

    data = canonical_bytes(reply)
    if len(data) > args.max_reply_bytes:
        return _refuse(stderr, "reply_too_large", 3)
    stdout.write(data.decode("utf-8"))
    return 0 if reply["ok"] else 3
