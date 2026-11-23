"""Check an operator-provisioned H1 MCP stdio command against its registry."""

import argparse
import json
from pathlib import Path
import subprocess
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from control.core.control import CapabilityRegistry, canonical_bytes
from expose.mcp import PROTOCOL_VERSION
from expose.mcp.tools import registration_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--registry-sha256", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("an operator-provisioned stdio command is required")
    entries = json.loads(args.registry.read_bytes())
    if not isinstance(entries, list):
        parser.error("expected an array of C1 capability entries")
    manifest = registration_manifest(CapabilityRegistry(entries))
    if manifest["registry_sha256"] != args.registry_sha256:
        raise SystemExit("Registry digest mismatch")
    names = {tool["name"] for tool in manifest["tools"]}
    if {name for name in names if name.startswith("mv_")} != {
            "mv_observe", "mv_list", "mv_propose", "mv_wait_verify"}:
        raise SystemExit("Delegated MCP tool set mismatch")

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "vp-direct-live-registration"},
        }},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "vp-direct.invalid-method"},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "vp-direct.invalid-tool", "arguments": {}}},
    ]
    process = subprocess.run(command, input=b"".join(map(canonical_bytes, requests)),
                             stdout=subprocess.PIPE, timeout=10, check=True)
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    if [response.get("id") for response in responses] != [1, 2, 3, 4]:
        raise SystemExit("Response IDs mismatch")
    if responses[0]["result"]["protocolVersion"] != PROTOCOL_VERSION:
        raise SystemExit("MCP protocol version mismatch")
    if responses[1]["result"]["tools"] != manifest["tools"]:
        raise SystemExit("MCP listing differs from the pinned registry projection")
    if [response["error"]["code"] for response in responses[2:]] != [-32601, -32602]:
        raise SystemExit("MCP error codes mismatch")
    print(manifest["registry_sha256"])


if __name__ == "__main__":
    main()
