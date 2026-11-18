# SPDX-License-Identifier: AGPL-3.0-only
"""Command-line access to the component-local REA selector table."""

import argparse
import os
from pathlib import Path
import stat
import sys
from uuid import uuid4

from . import local
from .wire import MAX_LINE_BYTES, encode, loads


def _args_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as data:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("expected a regular argument file")
        raw = data.read(MAX_LINE_BYTES + 1)
    if len(raw) > MAX_LINE_BYTES:
        raise ValueError("argument file too large")
    return loads(raw)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    commands.add_parser("describe").add_argument("op")
    call = commands.add_parser("call")
    call.add_argument("op")
    source = call.add_mutually_exclusive_group(required=True)
    source.add_argument("--args")
    source.add_argument("--args-file", type=Path)
    call.add_argument("--dry-run", action="store_true")
    options = parser.parse_args(argv)
    api = local()
    if options.command == "list":
        result = api.catalog.table
    elif options.op not in api.catalog.rows:
        result = {"ok": False, "code": "-32601"}
    elif options.command == "describe":
        result = api.catalog.describe(options.op)
    else:
        try:
            args = _args_file(options.args_file) if options.args_file else loads(options.args)
            if not isinstance(args, dict) or args.get("op", options.op) != options.op:
                raise ValueError("argument selector must match the CLI selector")
            args["op"] = options.op
            result = api.handle(api.request(args, str(uuid4()), options.dry_run))
        except (OSError, ValueError, RecursionError):
            result = {"ok": False, "code": "-32602"}
    sys.stdout.buffer.write(encode(result))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())

