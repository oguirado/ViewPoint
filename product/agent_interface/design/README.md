# agent_interface/design (lane ops-design)

Track P, declared Claude work (claude-fable-5-2). The frozen contract that the `vp` CLI (lane ops-cli), the `viewpoint-ops`
MCP server (lane ops-mcp) and the docs router (lane ops-docs) build on.

| File | What it is | How it is made |
|---|---|---|
| `CAPABILITIES.md` | every ViewPoint capability with its real entry point as file:line and the `vp` command that wraps it; declared gaps | `python3 -I caps.py` (line numbers are found on disk from anchor text) |
| `COMMANDS.md` | every `vp <noun> <verb>`: arguments, receipt `data` fields, codes, safety class; call shape, safety rules, exit codes | `python3 -I gen.py` |
| `MCP_TOOLS.json` | one MCP tool per command: name, description, inputSchema, argv mapping, tier, codes | `python3 -I gen.py` |
| `E2E_SCENARIOS.md` | five end-to-end runs as exact command sequences with decision tables | written by hand, checked by the tests |
| `validate_cmd.py` | checks any `vp ...` line (or every line of a Markdown file) against `MCP_TOOLS.json` | `python3 -I validate_cmd.py 'vp ...'` |
| `tests/test_design.py` | consistency tests with negative controls | see below |

Change the taxonomy only in `gen.py` (`SPEC`) and the inventory only in `caps.py` (`ROWS`), then regenerate.

```sh
python3 -I gen.py && python3 -I caps.py
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 python3 -I tests/test_design.py
```

`python3 -I gen.py --check` and `python3 -I caps.py --check` return rc 1 when a generated file drifted or a cited anchor
moved or disappeared in the wrapped code.

Reused from newton-py (called, never copied): `read`, `find`, `sh --tail/--head`, `tok.count`, `route` behind the `vp out *`
and `vp docs route` commands, and the route engine `newton-py/route/route.py` behind `vp docs *` (lane ops-docs).
