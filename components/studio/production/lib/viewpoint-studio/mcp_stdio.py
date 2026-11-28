# SPDX-License-Identifier: AGPL-3.0-only
"""Stdio binding for the same operations and receipts as the installed JSON CLI."""

import sys

from providers import MAX_JSON, Refusal, fields
from registry import canonical

PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}
LATEST = "2025-06-18"


def serve(studio, decode, refusal):
    tools = studio.manifest["tools"]
    names = [tool["name"] for tool in tools]
    operations = dict(zip(names, (studio.capabilities, studio.dry_run, studio.run)))

    def write(value):
        sys.stdout.buffer.write(canonical(value))
        sys.stdout.buffer.flush()

    def error(request_id, code, message):
        write({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    while raw := sys.stdin.buffer.readline(MAX_JSON + 1):
        if len(raw) > MAX_JSON:
            error(None, -32700, "Request exceeds the stdio message limit")
            return 1
        try:
            request = decode(raw)
        except (ValueError, RecursionError):
            error(None, -32700, "Invalid JSON")
            continue
        if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0"
                or not isinstance(request.get("method"), str)
                or ("id" in request and type(request["id"]) not in (str, int))):
            error(None, -32600, "Invalid request")
            continue
        if "id" not in request:
            continue
        request_id, method = request["id"], request["method"]
        params = request.get("params", {})
        if not isinstance(params, dict):
            error(request_id, -32602, "Invalid params")
            continue
        if method == "initialize":
            version = params.get("protocolVersion")
            if not isinstance(version, str):
                error(request_id, -32602, "Missing protocol version")
                continue
            value = {"protocolVersion": version if version in PROTOCOLS else LATEST,
                     "capabilities": {"tools": {}},
                     "serverInfo": {"name": studio.manifest["server"]["name"], "version": "1.0.0"}}
        elif method == "ping":
            value = {}
        elif method == "tools/list":
            value = {"tools": tools}
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments", {})
            if not isinstance(name, str) or name not in operations:
                error(request_id, -32602, "Unknown tool")
                continue
            try:
                if name == names[0] and fields(args, ""):
                    receipt = operations[name]()
                elif name != names[0] and fields(args, "request"):
                    receipt = operations[name](args["request"])
                else:
                    raise Refusal("request_invalid", "the arguments match the advertised tool schema")
                failed = False
            except Refusal as exc:
                cli_mode = dict(zip(names, ("--list-capabilities", "--dry-run", "--run")))[name]
                flags = [arg for arg in sys.argv[1:] if arg != "--mcp"]
                receipt = refusal(exc.reason, exc.retry_when, ["viewpoint-studio", cli_mode, *flags], exc.detail)
                failed = True
            value = {"content": [{"type": "text", "text": canonical(receipt).decode("ascii").strip()}],
                     "isError": failed}
        else:
            error(request_id, -32601, "Method not found")
            continue
        write({"jsonrpc": "2.0", "id": request_id, "result": value})
    return 0
