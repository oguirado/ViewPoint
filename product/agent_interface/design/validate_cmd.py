#!/usr/bin/env python3
"""Validate `vp` command lines against MCP_TOOLS.json (Track P, claude-fable-5-2, lane ops-design).

    python3 -I validate_cmd.py 'vp queue run --job vpops1 --rt /x --turns 3'   # rc 0 valid, 1 invalid
    python3 -I validate_cmd.py --file E2E_SCENARIOS.md                         # every `vp ` line inside ```sh blocks

Checks: command exists; flags exist in its inputSchema; required arguments present; class D carries --confirm unless
--dry-run; value flags have a value; integer flags parse unless the value is a <placeholder> or $VAR. Stdlib only.
"""
import json
import os
import shlex
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load(path=None):
    with open(path or os.path.join(HERE, "MCP_TOOLS.json"), encoding="utf-8") as f:
        doc = json.load(f)
    return {tuple(t["argv"]): t for t in doc["tools"]}


def validate(line, tools):
    """Return a list of error strings (empty = valid)."""
    try:
        w = shlex.split(line, comments=True)
    except ValueError as e:
        return ["unparseable: %s" % e]
    if len(w) < 3 or w[0] != "vp":
        return ["not a `vp <noun> <verb>` line"]
    t = tools.get((w[1], w[2]))
    if t is None:
        return ["unknown command: vp %s %s" % (w[1], w[2])]
    props = t["inputSchema"]["properties"]
    seen, errs, pos, i, rest = set(), [], list(t["positional"]), 3, w[3:]
    i = 0
    while i < len(rest):
        a = rest[i]
        if not a.startswith("--"):
            if not pos:
                errs.append("unexpected positional %r" % a)
            else:
                seen.add(pos.pop(0))
            i += 1
            continue
        name = a[2:].replace("-", "_")
        if name not in props:
            errs.append("unknown flag %s" % a)
            i += 1
            continue
        seen.add(name)
        typ = props[name]["type"]
        if typ == "boolean":
            i += 1
            continue
        if i + 1 >= len(rest) or rest[i + 1].startswith("--"):
            errs.append("flag %s needs a value" % a)
            i += 1
            continue
        v = rest[i + 1]
        literal = not (v.startswith("<") or "$" in v)
        if typ == "integer" and literal:
            try:
                int(v)
            except ValueError:
                errs.append("flag %s needs an integer, got %r" % (a, v))
        if typ == "number" and literal:
            try:
                float(v)
            except ValueError:
                errs.append("flag %s needs a number, got %r" % (a, v))
        i += 2
    for r in t["inputSchema"]["required"]:
        if r not in seen:
            errs.append("missing required %s" % r)
    if t["destructive"] and "confirm" not in seen and "dry_run" not in seen:
        errs.append("class D command without --confirm")
    return errs


def sh_lines(path):
    out, inside = [], False
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            s = line.strip()
            if s.startswith("```"):
                inside = s == "```sh"
                continue
            if inside and s.startswith("vp "):
                out.append((n, s))
    return out


def main(argv):
    tools = load()
    if argv[:1] == ["--file"]:
        rows = sh_lines(argv[1])
    else:
        rows = [(0, a) for a in argv]
    bad = [{"line": n, "cmd": s, "errors": e} for n, s in rows for e in [validate(s, tools)] if e]
    print(json.dumps({"ok": not bad, "checked": len(rows), "invalid": bad}))
    return 1 if bad or not rows else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
