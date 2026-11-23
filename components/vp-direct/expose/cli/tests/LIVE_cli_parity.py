"""Compare an H1-provisioned mvctl dry run with captured MCP and mailbox replies."""

import argparse
import json
from pathlib import Path
import subprocess
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from control.core import control
from expose.cli.verbs import TRANSPORT_FLAGS


def verify(reply, request):
    control.validate_contract(control.REPLY, reply)
    if (not reply["ok"] or reply["op"] != request["op"]
            or reply["request_id"] != request["request_id"]
            or reply["effective_request_sha256"] != control.digest(request)):
        raise ValueError("reply did not verify the same dry-run request")


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--mvctl", type=Path, required=True)
    parser.add_argument("--mcp-reply", type=Path, required=True)
    parser.add_argument("--mailbox-reply", type=Path, required=True)
    parser.add_argument("--json-arg", type=Path, required=True)
    parser.add_argument("verb")
    for flag in TRANSPORT_FLAGS:
        parser.add_argument(flag, required=True)
    args = parser.parse_args()
    if not args.mvctl.is_absolute():
        parser.error("an absolute path to the provisioned mvctl is required")

    request = json.loads(args.json_arg.read_bytes())
    control.validate_contract(control.REQUEST, request)
    if not request["dry_run"]:
        parser.error("the request must be an explicit dry run")
    verify(json.loads(args.mcp_reply.read_bytes()), request)
    verify(json.loads(args.mailbox_reply.read_bytes()), request)

    argv = [str(args.mvctl), args.verb, "--dry-run", "--json-arg", str(args.json_arg)]
    for flag in TRANSPORT_FLAGS:
        argv.extend((flag, getattr(args, flag[2:].replace("-", "_"))))
    completed = subprocess.run(argv, check=True, capture_output=True,
                               timeout=int(args.timeout_ms) / 1000)
    verify(json.loads(completed.stdout), request)
    print(control.digest(request))


if __name__ == "__main__":
    main()
