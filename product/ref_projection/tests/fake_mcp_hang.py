#!/usr/bin/env python3
"""MCP double that goes silent on chosen tools/call numbers.

$FAKE_MCP_HANG = JSON list of global call numbers (1-based, counted across
restarts in $FAKE_MCP_COUNT) on which the server answers 3 s late: longer
than the client timeout of the tests, so the client sees a timeout."""
import json, os, sys, time
TOOLS = [{"name": n, "inputSchema": {"type": "object", "properties": {}, "required": []}}
         for n in ("post_status", "post_plan", "post_step", "close_task")]
for t in TOOLS:
    del t["inputSchema"]["properties"]
hang = set(json.loads(os.environ.get("FAKE_MCP_HANG", "[]")))
cf = os.environ["FAKE_MCP_COUNT"]
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    if m["method"] == "initialize":
        res = {"protocolVersion": "2024-11-05", "serverInfo": {"name": "fake-hang"}, "capabilities": {}}
    elif m["method"] == "tools/list":
        res = {"tools": TOOLS}
    else:
        n = (int(open(cf).read()) if os.path.exists(cf) else 0) + 1
        open(cf, "w").write(str(n))
        a = m["params"]["arguments"]
        with open(cf + ".calls", "a") as f:
            f.write(json.dumps([n, m["params"]["name"], a.get("agent")]) + "\n")
        if n in hang:
            time.sleep(3)
        res = {"content": [{"type": "text", "text": json.dumps({"n": n, "agent": a.get("agent")})}], "isError": False}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": res}) + "\n")
    sys.stdout.flush()
