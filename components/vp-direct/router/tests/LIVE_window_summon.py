#!/usr/bin/env python3
"""LIVE check for the orchestrator using the published index and live registry."""

import argparse
import importlib.util
import json
from pathlib import Path
import shlex
import sys


MODULE = Path(__file__).resolve().parents[1] / "router.py"
SPEC = importlib.util.spec_from_file_location("vp_router_live", MODULE)
router = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = router
SPEC.loader.exec_module(router)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True, help="JSON list of live capabilities")
    parser.add_argument("--manifest-generation", type=int, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--agent-instance", required=True)
    parser.add_argument("--request-id", required=True)
    options = parser.parse_args()

    raw = router.vp_tool_route(
        "bring the window to my desktop",
        index=json.loads(options.index.read_bytes()),
        registry=json.loads(options.registry.read_bytes()),
        manifest_generation=options.manifest_generation,
        source_root=options.source_root,
        args={"session_id": options.session_id},
        context={"caller": "agent", "agent_instance": options.agent_instance},
        request_id=options.request_id,
        dry_run=True,
        policy=lambda entry, row, request: entry["tool"] == "vp_window_summon" and request["dry_run"],
    )
    sys.stdout.buffer.write(raw)
    reply = json.loads(raw)
    if reply["verdict"] != "MATCH" or len(reply["candidates"]) != 1:
        return 1
    (candidate,) = reply["candidates"]
    expected_args = {"session_id": options.session_id}
    exact = (
        candidate["mcp_call"] == {"name": "vp_window_summon", "arguments": {
            **expected_args, "request_id": options.request_id, "dry_run": True,
        }}
        and shlex.split(candidate["cli"]) == [
            "mvctl", "window-summon", "--session-id", options.session_id, "--dry-run",
        ]
        and candidate["json"]["op"] == "vp_window_summon"
        and candidate["json"]["args"] == expected_args
        and candidate["json"]["dry_run"] is True
        and all(citation["sha256"] for citation in candidate["citations"])
    )
    return 0 if exact else 1


if __name__ == "__main__":
    sys.exit(main())
