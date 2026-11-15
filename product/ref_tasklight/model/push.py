#!/usr/bin/env python3
"""Send the tm1 TaskLight calls of an events.jsonl to the context-wall MCP server and read the inbox back.

Track P (claude-fable-5-2). Reuses the MCP stdio client of product/ref_projection (imported, not copied).
Room is `demo` unless --room is given; `viewpoint` (the live demo wall) is always refused.
usage: push.py <events.jsonl> [--room demo] [--out responses.jsonl] [--inbox-agent AGENT]
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "ref_projection"))
import schema  # noqa: E402

FORBIDDEN_ROOMS = {"viewpoint"}


def push(records, room, client, out=None, inbox_agent=None):
    """-> summary dict. Every call and its response go to `out` (one JSON per line)."""
    if room in FORBIDDEN_ROOMS:
        raise ValueError("room %r is the live demo wall: refused" % room)
    sent = ok_n = 0
    rows = []
    for c in schema.calls(records, room):
        ok, res = client.call(c["tool"], c["arguments"])
        sent += 1
        ok_n += 1 if ok else 0
        rows.append({"key": c["key"], "tool": c["tool"], "arguments": c["arguments"], "ok": ok, "response": res})
    summary = {"room": room, "calls": sent, "ok": ok_n, "refused": sent - ok_n}
    if inbox_agent:
        ok, res = client.call("read_inbox", {"room": room, "agent": inbox_agent})
        rows.append({"tool": "read_inbox", "arguments": {"room": room, "agent": inbox_agent}, "ok": ok, "response": res})
        events = []
        for part in res.get("content", []) if ok else []:
            try:
                events.extend(json.loads(part.get("text", "{}")).get("events", []))
            except (ValueError, AttributeError):
                pass
        summary["inbox_events"] = len(events)
        summary["handoffs_read_back"] = schema.parse_inbox(events)
    if out:
        with open(out, "w") as f:
            for r in rows:
                f.write(json.dumps(r, sort_keys=True) + "\n")
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("events")
    ap.add_argument("--room", default="demo")
    ap.add_argument("--out")
    ap.add_argument("--inbox-agent")
    a = ap.parse_args(argv)
    if a.room in FORBIDDEN_ROOMS:
        sys.stderr.write("room %r refused\n" % a.room)
        return 2
    from project import McpStdioClient
    client = McpStdioClient()
    client.start()
    try:
        s = push(schema.read_events(a.events), a.room, client, a.out, a.inbox_agent)
    finally:
        client.stop()
    print(json.dumps(s, sort_keys=True))
    return 0 if s["refused"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
