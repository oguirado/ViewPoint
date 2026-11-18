# Codex — viewpoint-agent MCP host

Codex drives ViewPoint through the same `viewpoint-agent` stdio server. The executable and protocol are identical to the Claude Desktop recipe; only the client configuration differs.

## Configuration

Add an MCP server entry in the Codex MCP configuration owned by the operator (example `config.toml` TOML fragment; the file stays outside this repository):

```toml
[mcp_servers.viewpoint-agent]
command = "/usr/bin/python3"
args = ["-I", "<repo>/hackathon/mcp/vp_mcp/vp_mcp.py"]
env = { VP_MCP_SESSION = "<session.json>" }
```

Requirements:

- `command` is `/usr/bin/python3 -I <repo>/hackathon/mcp/vp_mcp/vp_mcp.py`. The `-I` flag is required; do not substitute bare `python3` or add `-m` module wrappers.
- `VP_MCP_SESSION` is an absolute path to the session JSON validated by `session.schema.json`. The server refuses to start when it is missing, relative or malformed. No profile, cookie, token or `~/.codex` file is opened to obtain it.
- Protocol `2024-11-05`, `MAX_LINE_BYTES = 262144`, server name `viewpoint-agent`.

## Verify (dry-run, local)

Never use a live Codex session as the acceptance oracle. Run the capped suite:

```bash
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -- \
  python3 -I -B -m unittest discover -s <repo>/docs/vp-direct/tests -p 'test_*.py' -v
```

```bash
python3 -I <repo>/components/vp-direct/expose/mcp/generate.py --catalog-only --check
python3 -I <repo>/components/vp-direct/expose/cli/generate.py --catalog-only --check
```

Dry-run the three lanes for one request and compare `effective_request_sha256`:

```bash
VP_MCP_SESSION=<session.json> /usr/bin/python3 -I <repo>/hackathon/mcp/vp_mcp/vp_mcp.py --dry-run
# MCP: tools/list → 14 tools; tools/call with dry_run true
mvctl <verb> --socket <core.sock> --session-id <session> \
  --agent-instance <agent> --core-instance <core> \
  --timeout-ms 500 --max-line-bytes 2048 --max-reply-bytes 4096 \
  --json-arg <request.json> --dry-run
```

Expected: `registry_sha256 = 0d22e8f21b317e6b83e762694702f4c4265dd7da064ffa4284704d3daf69cd21`. Any digest mismatch, stale registry or `ARGS_SCHEMA_ABSENT` / `HOLD` is a refusal to fix in the registry or binding, not in the host file.

## Limits

- All 105 selectors across the 14 tools are `supported:false` (0 supported) until a verified binding lands; execution is `HOLD` by design at this edition.
