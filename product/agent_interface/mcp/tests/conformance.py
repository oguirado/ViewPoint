#!/usr/bin/env python3
"""Dry-run every tool of the active table through the server and the active
`vp`, and print one row per tool whose dry run is not ok. Starts nothing:
only dry_run=true calls are made. Exit status 0 = all ok, 1 = some not ok.

Run: python3 -I tests/conformance.py
"""
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import viewpoint_ops_mcp as m  # noqa: E402


def sample(key, prop):
    kind = prop.get("type", "string")
    if "enum" in prop:
        return prop["enum"][0]
    if kind in ("integer", "number"):
        return prop.get("minimum", 1)
    if kind == "boolean":
        return True
    if kind == "array":
        return ["vpops-x"]
    if kind == "object":
        return {}
    return "https://example.invalid/" if key == "url" else "vpops-x"


def main():
    env = dict(os.environ, VP_OPS_TOOLS="all")
    env.setdefault("VP_OPS_SPOOL", os.path.join(env.get("TMPDIR", "/tmp"), "vpops-conformance-spool"))
    server = m.OpsServer(env=env)
    counts, bad = collections.Counter(), []
    for tool in server.all_tools:
        schema = tool["inputSchema"]
        props = schema.get("properties", {})
        # The published example when there is one; made-up values otherwise.
        args = dict((schema.get("examples") or [None])[0]
                    or {k: sample(k, props.get(k, {})) for k in schema.get("required", [])})
        args["dry_run"] = True
        _, receipt = server.call_tool(tool["name"], args)
        counts[receipt["code"]] += 1
        if not receipt["ok"]:
            msg = receipt["data"].get("error") or ""
            bad.append((tool["name"], receipt["code"], receipt["rc"], str(msg)[:110]))
    for row in bad:
        print("NOT_OK %s code=%s rc=%s %s" % row)
    print(m.compact_json({"ok": not bad, "code": "OK" if not bad else "FAIL", "rc": 0 if not bad else 1,
                          "data": {"tools": len(server.all_tools), "by_code": dict(counts),
                                   "cli": server.cli, "tools_path": server.tools_path}, "next": []}))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
