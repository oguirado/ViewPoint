"""Registry-backed ViewPoint MCP lane. The service owner provisions C1 and the caller."""

from copy import deepcopy
import json

from control.core.control import InvalidRequest, canonical_bytes

from .._json import unique_object as _unique_object
from .tools import registration_manifest


# The existing ViewPoint MCP lane uses this protocol version and inbound frame limit.
PROTOCOL_VERSION = "2024-11-05"
MAX_LINE_BYTES = 262144


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_result(reply, *, is_error):
    return {"content": [{"type": "text", "text": canonical_bytes(reply).decode("utf-8").rstrip("\n")}],
            "isError": is_error}


class MCPServer:
    def __init__(self, dispatcher, identity, *, binding_context=None):
        self._dispatcher = dispatcher
        self._identity = identity
        self._binding_context = deepcopy(binding_context)
        self._manifest = registration_manifest(dispatcher.registry)
        self._names = frozenset(tool["name"] for tool in self._manifest["tools"])

    def handle_request(self, message):
        if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
                or not isinstance(message.get("method"), str)
                or "id" in message and type(message["id"]) not in (str, int)):
            return _error(None, -32600, "Invalid Request")
        if "id" not in message:
            return None

        request_id = message["id"]
        method = message["method"]
        params = message.get("params", {})
        if method == "initialize":
            if (not isinstance(params, dict) or not isinstance(params.get("protocolVersion"), str)
                    or not isinstance(params.get("capabilities"), dict)
                    or not isinstance(params.get("clientInfo"), dict)):
                return _error(request_id, -32602, "Invalid params")
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "viewpoint-agent", "version": "0.2.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            if params != {}:
                return _error(request_id, -32602, "Invalid params")
            result = {"tools": deepcopy(self._manifest["tools"])}
        elif method == "tools/call":
            if (not isinstance(params, dict) or set(params) != {"name", "arguments"}
                    or not isinstance(params["name"], str) or not isinstance(params["arguments"], dict)):
                return _error(request_id, -32602, "Invalid params")
            if params["name"] not in self._names:
                return _error(request_id, -32602, "Unknown tool")
            try:
                reply = self._dispatcher.dispatch_mcp(
                    params["name"], params["arguments"], self._identity,
                    binding_context=self._binding_context,
                )
            except InvalidRequest as exc:
                if isinstance(exc.code, int):
                    return _error(request_id, exc.code, "Invalid params" if exc.code == -32602 else "Unknown tool")
                result = _tool_result({"code": exc.code}, is_error=True)
            else:
                result = _tool_result(reply, is_error=not reply["ok"])
        else:
            return _error(request_id, -32601, "Method not found")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _reject_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def serve(server, reader, writer):
    """Serve newline-delimited MCP on binary streams supplied by the service owner."""
    while raw := reader.readline(MAX_LINE_BYTES + 1):
        bad_frame = len(raw) > MAX_LINE_BYTES or not raw.endswith(b"\n")
        if bad_frame:
            response = _error(None, -32700, "Invalid frame")
        else:
            try:
                message = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                                     parse_constant=_reject_constant)
            except (ValueError, RecursionError):
                response = _error(None, -32700, "Parse error")
            else:
                response = server.handle_request(message)
        if response is not None:
            writer.write(canonical_bytes(response))
            writer.flush()
        if bad_frame:
            return
