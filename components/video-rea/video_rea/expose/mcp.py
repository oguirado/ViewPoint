# SPDX-License-Identifier: AGPL-3.0-only
"""Newline-delimited JSON-RPC for local ViewPoint video reconstruction."""

import sys

from . import local
from .wire import MAX_LINE_BYTES, encode, loads


PROTOCOL_VERSION = "2024-11-05"


def error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def respond(message, api):
    if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
            or "id" in message and type(message["id"]) not in (int, str)):
        return error(None, -32600, "Invalid request")
    if "id" not in message:
        return None

    rid, method = message["id"], message["method"]
    params = message.get("params", {})
    invalid = error(rid, -32602, "Invalid params")
    if not isinstance(params, dict):
        return invalid
    if method == "initialize":
        if (not isinstance(params.get("protocolVersion"), str)
                or not isinstance(params.get("capabilities"), dict)
                or not isinstance(params.get("clientInfo"), dict)):
            return invalid
        result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "video-rea", "version": api.catalog.envelope["title"].split("/")[-1]}}
    elif method == "tools/list":
        if params:
            return invalid
        result = {"tools": api.catalog.tools()}
    elif method == "tools/call":
        if (set(params) != {"name", "arguments"} or not isinstance(params["name"], str)
                or not isinstance(params["arguments"], dict)):
            return invalid
        if params["name"] != api.catalog.tool:
            return error(rid, -32601, "Unknown tool")
        args = dict(params["arguments"])
        if not api.catalog.metadata.keys() <= args.keys():
            return invalid
        request_id, dry_run = args.pop("request_id"), args.pop("dry_run")
        if not isinstance(args.get("op"), str):
            return invalid
        row = api.catalog.rows.get(args["op"])
        if row is None:
            return error(rid, -32601, "Unknown selector")
        fields = api.catalog.resources[row["args_schema"]]["properties"]
        context = {k: args.pop(k) for k in api.catalog.context_fields if k in args and k not in fields}
        reply = api.handle(api.request(args, request_id, dry_run, context))
        if reply["code"] == "-32602":
            return invalid
        result = {"isError": not reply["ok"],
                  "content": [{"type": "text", "text": encode(reply).decode("utf-8").rstrip("\n")}]}
    else:
        return error(rid, -32601, "Method not found")
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def serve(reader, writer, api):
    while raw := reader.readline(MAX_LINE_BYTES + 1):
        bad_frame = len(raw) > MAX_LINE_BYTES or not raw.endswith(b"\n")
        if bad_frame:
            response = error(None, -32700, "Invalid frame")
        else:
            try:
                message = loads(raw)
            except (ValueError, RecursionError):
                response = error(None, -32700, "Parse error")
            else:
                response = respond(message, api)
        if response is not None:
            writer.write(encode(response))
            writer.flush()
        if bad_frame:
            break


if __name__ == "__main__":
    serve(sys.stdin.buffer, sys.stdout.buffer, local())

