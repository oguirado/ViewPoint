# SPDX-License-Identifier: AGPL-3.0-only
"""Structured CLI, JSON and MCP stdio entry points for the host handler."""

import argparse
from pathlib import Path
import sys

from .compiler import EXIT, canonical, parse
from .host import Host

MAX_REQUEST_BYTES = 65_536


def emit(document):
    sys.stdout.buffer.write(canonical(document))
    sys.stdout.buffer.flush()


def load_arguments(inline, path):
    if inline is not None:
        payload = inline.encode("utf-8")
    elif path == "-":
        payload = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    elif path is not None:
        with Path(path).open("rb") as stream:
            payload = stream.read(MAX_REQUEST_BYTES + 1)
    else:
        return {}
    if len(payload) > MAX_REQUEST_BYTES:
        raise ValueError("Request too large")
    return parse(payload)


def mcp_message(host, message):
    base = {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None}
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return base | {"error": {"code": -32600, "message": "Invalid Request"}}
    if "id" not in message:
        return None
    method = message.get("method")
    params = message.get("params", {})
    if not isinstance(params, dict):
        return base | {"error": {"code": -32602, "message": "Invalid params"}}
    if method == "initialize":
        supported = ("2025-06-18", "2025-03-26")
        version = params.get("protocolVersion")
        result = {"protocolVersion": version if version in supported else supported[0],
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "vp3d-host-faces", "version": "1.0.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": host.catalog.tools()}
    elif method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            return base | {"error": {"code": -32602, "message": "Missing tool name"}}
        reply = host.face("mcp", name, params.get("arguments", {}))
        result = {"content": [{"type": "text", "text": canonical(reply).decode("utf-8")}],
                  "structuredContent": reply, "isError": not reply["ok"]}
    else:
        return base | {"error": {"code": -32601, "message": "Method not found"}}
    return base | {"result": result}


def mcp_stdio(host):
    stream = sys.stdin.buffer
    while line := stream.readline(MAX_REQUEST_BYTES + 1):
        try:
            if len(line) > MAX_REQUEST_BYTES:
                while line and not line.endswith(b"\n"):
                    line = stream.readline(MAX_REQUEST_BYTES + 1)
                raise ValueError("Request too large")
            message = parse(line)
        except (ValueError, RecursionError):
            emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        reply = mcp_message(host, message)
        if reply is not None:
            emit(reply)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def call_flags(parser, input_required=False):
    inputs = parser.add_mutually_exclusive_group(required=input_required)
    inputs.add_argument("--args")
    inputs.add_argument("--args-file")
    if not input_required:
        parser.add_argument("--request-id")
        parser.add_argument("--dry-run", action="store_true", default=None)


def command_parser():
    parser = Parser(prog="host_faces")
    subs = parser.add_subparsers(dest="face", required=True)
    subs.add_parser("discover")
    subs.add_parser("mcp")
    call_flags(subs.add_parser("json"), input_required=True)
    router = subs.add_parser("router")
    router.add_argument("route")
    call_flags(router)
    slot = subs.add_parser("slot")
    actions = slot.add_subparsers(dest="action", required=True)
    call = actions.add_parser("call")
    call.add_argument("slot_id")
    call.add_argument("operation")
    call_flags(call)
    return parser


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument("--config", required=True)
    config, remainder = bootstrap.parse_known_args(argv)
    host = Host(parse(Path(config.config).read_bytes()))
    wrapper = None
    try:
        args = command_parser().parse_args(remainder)
        if args.face == "discover":
            emit(host.catalog.discovery())
            return 0
        if args.face == "mcp":
            mcp_stdio(host)
            return 0
        wrapper = load_arguments(args.args, args.args_file)
        if args.face == "json":
            reply = host.handle(wrapper)
        else:
            if not isinstance(wrapper, dict):
                raise ValueError("Call arguments must be an object")
            for field in ("dry_run", "request_id"):
                flag = getattr(args, field)
                if flag is not None:
                    if field in wrapper and canonical(wrapper[field]) != canonical(flag):
                        raise ValueError(f"Conflicting {field}")
                    wrapper[field] = flag
            if args.face == "router":
                reply = host.face("router", args.route, wrapper)
            else:
                reply = host.face("cli", ("slot", "call", args.slot_id, args.operation), wrapper)
    except (OSError, ValueError, RecursionError):
        reply = host.invalid(wrapper.get("request_id") if isinstance(wrapper, dict) else None)
    emit(reply)
    return EXIT[reply["code"]]


if __name__ == "__main__":
    sys.exit(main())
