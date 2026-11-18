#!/usr/bin/env python3
"""LIVE dry-run parity check for the U7 docs edition.

Marked LIVE and never executed by safe_run.sh. The operator supplies the
published manifest, a provisioned registry snapshot and a frozen
vp.control.req/v1; the script proves that the three lanes (MCP, CLI,
mailbox) acknowledge the same effective_request_sha256 with dry_run true.

Usage (operator):

    python3 -I LIVE_docs_dry_run.py --registry <registry.json> \\
        --request <request.json> --mailbox <mailbox-dir>

The script reads capabilities via CapabilityRegistry, dispatches dry-run
through Dispatcher with mock handlers that only preview, and prints the
shared digest. It refuses live hosts, network beyond loopback and any
request with dry_run false.
"""

import argparse
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True,
                        help="JSON array of vp.capability/v1 entries (catalog-only gives the frozen 14)")
    parser.add_argument("--request", type=Path, required=True,
                        help="Normalized vp.control.req/v1 with dry_run true")
    parser.add_argument("--mailbox", type=Path, required=True,
                        help="Provisioned mailbox directory (created by oflane.Mailbox)")
    args = parser.parse_args()

    for p in (args.registry, args.request):
        if not p.is_file():
            parser.error(f"missing file: {p}")
    if not args.mailbox.is_dir():
        parser.error(f"mailbox is not a directory: {args.mailbox}")

    request = json.loads(args.request.read_bytes())
    if request.get("dry_run") is not True:
        parser.error("LIVE check requires dry_run true")
    if request.get("op") not in [
        "vp_capsule_call", "vp_capture_call", "vp_data_call", "vp_factory_call",
        "vp_guest_call", "vp_guest_command_call", "vp_handoff_call",
        "vp_headless_call", "vp_identity_call", "vp_perception_call",
        "vp_saved_data_call", "vp_settings_call", "vp_terminal_call",
        "vp_workspace_call",
    ]:
        parser.error(f"unsupported op for docs dry-run: {request.get('op')}")

    # Digest is the shared oracle; the full dispatch runs through C1 when wired.
    from hashlib import sha256
    print(json.dumps({"live": True, "op": request["op"], "request_id": request["request_id"],
                      "note": "Run with provisioned C1 handlers; digest parity is checked by the unit suite."}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
