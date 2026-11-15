#!/usr/bin/env python3
"""Test double of the context-wall MCP server: same tool names and the limits the
real server documents (1..12 steps, text <= 80, accept <= 160, outcome <= 240)."""
import json
import sys

def schema(req, **props):
    return {"type": "object", "properties": {k: {"type": v} for k, v in props.items()}, "required": req}

TOOLS = [
    {"name": "post_status", "inputSchema": schema(["room", "agent", "text"], room="string", agent="string", text="string", to="string", re="string")},
    {"name": "post_plan", "inputSchema": schema(["room", "agent", "title", "steps"], room="string", agent="string", title="string", steps="array")},
    {"name": "post_step", "inputSchema": schema(["room", "agent", "index", "state"], room="string", agent="string", index="integer", state="string", outcome="string", took="string")},
    {"name": "close_task", "inputSchema": schema(["room", "agent", "outcome"], room="string", agent="string", outcome="string")},
]

def bad(name, a):
    if name == "post_plan":
        s = a.get("steps")
        if not isinstance(s, list) or not 1 <= len(s) <= 12 or len(a.get("title", "")) > 120:
            return "bad_plan"
        for st in s:
            if set(st) - {"text", "accept"} or not st.get("text") or len(st["text"]) > 80 or len(st.get("accept", "")) > 160:
                return "bad_plan"
    if name == "post_step":
        if a.get("state") not in ("done", "active", "blocked", "pending") or not isinstance(a.get("index"), int) or len(a.get("outcome", "")) > 240:
            return "bad_step"
    if name == "close_task" and (not a.get("outcome", "").strip() or len(a["outcome"]) > 240):
        return "text_too_long"
    return None

seq = 0
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    if m["method"] == "initialize":
        res = {"protocolVersion": "2024-11-05", "serverInfo": {"name": "fake-context-wall"}, "capabilities": {"tools": {}}}
    elif m["method"] == "tools/list":
        res = {"tools": TOOLS}
    elif m["method"] == "tools/call":
        p = m["params"]
        code = bad(p["name"], p["arguments"])
        seq += 1
        payload = {"ok": False, "code": code} if code else {"event": {"seq": seq, "agent": p["arguments"]["agent"]}}
        res = {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": bool(code)}
    else:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "method not found"}}) + "\n")
        sys.stdout.flush()
        continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": res}) + "\n")
    sys.stdout.flush()
