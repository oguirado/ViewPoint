# Antigravity 2 — viewpoint-agent MCP host

Antigravity 2 is the default desktop host in the strategy (the operator works here). It launches the same `viewpoint-agent` server as Claude Desktop and Codex; the only difference is the plugin discovery layout.

## Configuration

Create the workspace-local plugin files (see `AGY.md` for discovery; plugin MCP config key `mcpServers`). Paths are relative to the workspace root; no absolute home path is baked in:

`.agents/plugins/viewpoint/plugin.json` (workspace root):

```json
{ "name": "viewpoint" }
```

`.agents/plugins/viewpoint/mcp_config.json`:

```json
{
  "mcpServers": {
    "viewpoint-agent": {
      "command": "/usr/bin/python3",
      "args": ["-I", "<repo>/hackathon/mcp/vp_mcp/vp_mcp.py"],
      "env": { "VP_MCP_SESSION": "<session.json>" }
    }
  }
}
```

Alternatively run the server directly:

```bash
VP_MCP_SESSION=<session.json> /usr/bin/python3 -I <repo>/hackathon/mcp/vp_mcp/vp_mcp.py
```

Requirements:

- The session JSON is absolute, validated, and lives outside the repository (generated `env.sh`, `srf_bin`, `state_dir`, `repo`, `xdg_state`, `attach_env`, `capsules[]` per `hackathon/mcp/vp_mcp/README.md`). Do not commit the session file.
- The server speaks `2024-11-05`, enforces `MAX_LINE_BYTES = 262144`, and reports `viewpoint-agent 0.2.0`. Flat per-tool `inputSchema`s are derived from the frozen `vp.control.req/v1` contracts; `caller`/`agent_instance`/`request`/`context` never appear in the wire.
- AT-SPI for Antigravity 2 is shallow (114 nodes, depth 2); CDP is primary. See `VP_DIRECT.md` §5 for the W-L0 backend table.

## Verify (dry-run, local)

```bash
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -- \
  python3 -I -B -m unittest discover -s <repo>/docs/vp-direct/tests -p 'test_*.py' -v
```

```bash
python3 -I <repo>/components/vp-direct/expose/mcp/generate.py --catalog-only --check
python3 -I <repo>/components/vp-direct/expose/cli/generate.py --catalog-only --check
```

```bash
VP_MCP_SESSION=<session.json> /usr/bin/python3 -I <repo>/hackathon/mcp/vp_mcp/vp_mcp.py --dry-run
# MCP: tools/list → tools/call (dry_run true)
mvctl <verb> --socket <core.sock> --session-id <session> \
  --agent-instance <agent> --core-instance <core> \
  --timeout-ms 500 --max-line-bytes 2048 --max-reply-bytes 4096 \
  --json-arg <request.json> --dry-run
```

Expected: `registry_sha256 = 0d22e8f21b317e6b83e762694702f4c4265dd7da064ffa4284704d3daf69cd21`. Compare `effective_request_sha256` across lanes. A stale manifest check, an undocumented op or an undisclosed binding change fails the suite in `docs/vp-direct/tests/`.

## Limits

- All 105 selectors across the 14 tools are `supported:false` (0 supported) until a verified binding lands; execution is `HOLD` by design at this edition.
