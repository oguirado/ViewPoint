"""LIVE verification of captured MCP, CLI and mailbox dry-run control replies."""

import argparse
import importlib.util
import json
from pathlib import Path
import sys


SPEC = importlib.util.spec_from_file_location("vp_control", Path(__file__).resolve().parents[1] / "control.py")
control = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = control
SPEC.loader.exec_module(control)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path, help="Normalized vp.control.req/v1 sent through all lanes")
    for lane in ("mcp", "cli", "mailbox"):
        parser.add_argument(lane, type=Path, help=f"Captured {lane} vp.control.reply/v1")
    args = parser.parse_args()
    request = json.loads(args.request.read_bytes())
    control.validate_contract(control.REQUEST, request)
    if not request["dry_run"]:
        parser.error("expected a dry-run request")
    digest = control.digest(request)
    for lane in ("mcp", "cli", "mailbox"):
        reply = json.loads(getattr(args, lane).read_bytes())
        control.validate_contract(control.REPLY, reply)
        if (not reply["ok"] or reply["request_id"] != request["request_id"]
                or reply["op"] != request["op"] or reply["effective_request_sha256"] != digest):
            parser.error(f"{lane} did not acknowledge the same normalized request")
    print(json.dumps({"effective_request_sha256": digest, "lanes": ["mcp", "cli", "mailbox"]}))


if __name__ == "__main__":
    main()
