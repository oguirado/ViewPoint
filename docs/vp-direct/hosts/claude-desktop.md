# Claude Desktop — viewpoint-agent MCP host

Claude Desktop launches the ViewPoint server over stdio. The server binary is the same one described in `VP_DIRECT.md` §3.1; the host recipe only adds the MCP client configuration.

## Configuration

Create a server entry in the Claude Desktop MCP settings (GUI `Settings → Connectors` or `claude_desktop_config.json` owned by the operator; the file stays outside this repository). No command below installs or modifies the repository.

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

Requirements:

- `command` is exactly `/usr/bin/python3` with `-I` (isolated). `-I` keeps the server immune to user `PYTHON*` / `site` pollution.
- `VP_MCP_SESSION` is an absolute path to a JSON session file validated against `hackathon/mcp/vp_mcp/session.schema.json`. Startup refuses on missing, relative or invalid JSON. The session file provides `env_launcher`, `srf_bin`, `state_dir`, `repo`, `xdg_state`, `attach_env` and `capsules[]` (see `hackathon/mcp/vp_mcp/README.md`). Never place the session file inside the repository and never open browser profiles, cookies, tokens or auth stores to build it.
- Server info is `viewpoint-agent` version `0.2.0`, protocol `2024-11-05`, `MAX_LINE_BYTES = 262144`.

## Verify (dry-run, local)

Do not launch the live host from this recipe. Instead, prove the three-lane digest parity for the same normalized `vp.control.req/v1` from a capped scope:

```bash
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -- \
  python3 -I -B -m unittest discover -s <repo>/docs/vp-direct/tests -p 'test_*.py' -v
```

And check that the published manifests match the accepted registry:

```bash
python3 -I <repo>/components/vp-direct/expose/mcp/generate.py --catalog-only --check
python3 -I <repo>/components/vp-direct/expose/cli/generate.py --catalog-only --check
```

Manual dry-run (replace `<session.json>` and `<request.json>` with provisioned fixtures; always `--dry-run`):

```bash
VP_MCP_SESSION=<session.json> /usr/bin/python3 -I <repo>/hackathon/mcp/vp_mcp/vp_mcp.py --dry-run
# then MCP: tools/list  (expect 14 tools) and tools/call with {"dry_run": true, ...}
# then CLI:  mvctl <verb> --socket <core.sock> --session-id <session> \
#              --agent-instance <agent> --core-instance <core> \
#              --timeout-ms 500 --max-line-bytes 2048 --max-reply-bytes 4096 \
#              --json-arg <request.json> --dry-run
# then mailbox: submit <request.json> with dry_run true and compare effective_request_sha256
```

Expected: `registry_sha256 = 0d22e8f21b317e6b83e762694702f4c4265dd7da064ffa4284704d3daf69cd21`. A changed digest, an unknown tool, a missing binding (`HOLD` / `ARGS_SCHEMA_ABSENT`), or a digest mismatch between lanes is a failure — fix the manifest, not the recipe.

## Limits

- All 105 selectors across the 14 tools are `supported:false` (0 supported) until a verified binding lands; execution is `HOLD` by design at this edition.
- First login is always human (`LOGIN_PENDING`); login, 2FA and captchas happen only in human mode. OAuth callback qualification is operator-owned and is `NOT RUN` in the W-L0 receipt.
