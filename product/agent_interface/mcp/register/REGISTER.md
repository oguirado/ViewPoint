# Registering `viewpoint-ops`

One stdio server, no install: `python3 -I REPO/product/agent_interface/mcp/viewpoint_ops_mcp.py`.
Replace `REPO` with the absolute path of the checkout. `-I` keeps Python from loading code from the current
directory. Check the server before registering it:

    python3 -I REPO/product/agent_interface/mcp/viewpoint_ops_mcp.py --check

The receipt must show `cli_is_stub: false`, `tools: 91`, `prompts` with five names and `examples: 91`.

Useful variables for any client (all optional): `VP_OPS_TOOLS=all` (list all 91 tools instead of the 54 of
tier core), `VP_SWARM_RT=<runtime root>` (what `viewpoint://state/*` reads), `VP_JOB_PREFIX=<prefix>` (job
names this session may create; default `vpops`), `VP_OPS_AUDIT=<file>` (one line per call).

## Antigravity (`agy`)

Flags go before the server name; `--` goes before the command because its arguments start with `-`:

    agy mcp add --type stdio viewpoint-ops -- python3 -I REPO/product/agent_interface/mcp/viewpoint_ops_mcp.py

With variables:

    agy mcp add --type stdio --env VP_OPS_TOOLS=all --env VP_SWARM_RT=/dev/shm/vqa-cu/ref-demo viewpoint-ops -- python3 -I REPO/product/agent_interface/mcp/viewpoint_ops_mcp.py

Wrong orders that fail: flags after the name are passed to the server as arguments; without `--`, `-I` is
read as a flag of `agy`. If your Antigravity version edits a JSON file instead, add the `viewpoint-ops` entry
of `claude.mcp.json` (same shape) under its MCP servers section with an absolute path in `args`.

## Claude Code

Project scope (paths relative to the repository root): copy `claude.mcp.json` to `REPO/.mcp.json`, or

    claude mcp add --scope project viewpoint-ops -- python3 -I product/agent_interface/mcp/viewpoint_ops_mcp.py
    claude mcp add --scope user --env VP_OPS_TOOLS=all viewpoint-ops -- python3 -I REPO/product/agent_interface/mcp/viewpoint_ops_mcp.py

Headless, with this server only and no built-in tools (the form used by `tests/e2e_agent.py`):

    claude -p --verbose --model claude-sonnet-5-5 --mcp-config REPO/product/agent_interface/mcp/register/claude.mcp.json --strict-mcp-config --output-format stream-json --tools "" --allowedTools mcp__viewpoint-ops < task.txt

Run it from the repository root (the path in `claude.mcp.json` is relative). Tools appear as
`mcp__viewpoint-ops__vp_<noun>_<verb>`; resources with `@viewpoint-ops:viewpoint://docs/agent-guide`;
prompts as `/mcp__viewpoint-ops__<prompt>`.

## Codex

Append `codex.config.toml` to `~/.codex/config.toml` with the absolute path filled in, or

    codex mcp add viewpoint-ops -- python3 -I REPO/product/agent_interface/mcp/viewpoint_ops_mcp.py
    codex mcp add --env VP_OPS_TOOLS=all viewpoint-ops -- python3 -I REPO/product/agent_interface/mcp/viewpoint_ops_mcp.py

## After registering

1. Ask the client to list the tools: 54 (or 91 with `VP_OPS_TOOLS=all`).
2. Call `vp_sys_doctor {}`: every row PASS, or `DEPENDENCY` names what is missing.
3. Read `viewpoint://docs/agent-guide`, then pick a prompt.

## What was run

Run by this lane: the Claude Code headless form above (through `tests/e2e_agent.py`, with a generated
config holding an absolute path). Not run: `agy mcp add`, `claude mcp add`, `codex mcp add` and the copy to
`.mcp.json`; they write client configuration outside the worktree. The `agy` and `codex` flag orders follow
the lane brief and were not executed here.
