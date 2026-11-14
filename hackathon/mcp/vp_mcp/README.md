# viewpoint-agent MCP server

Stdlib MCP stdio server (JSON-RPC 2.0: `initialize`, `tools/list`, `tools/call`).
Lets Antigravity 2 (Gemini) drive ViewPoint browser capsules through the core
admission path (`mvctl list/observe/propose/wait-verify` + bounded `mw_run`),
plus read-only `srf ax` for page text.

Native input/capture calls on app-owned capsules bypass core admission and
adapter epochs and are never used here. The only native verb is `ax`
(read-only). All core calls run through the app's clean env with the live
attachment (`attach_env`), exactly like `mw_run`.

Server name: `viewpoint-agent` (version `0.2.0`).

## Files

- `vp_mcp.py` — server (stdlib only; no pip deps)
- `session.schema.json` — JSON Schema for the session file
- `tests/test_vp_mcp.py` — unit tests with a fake runner replaying real shapes

## Session file

One JSON session file given by env `VP_MCP_SESSION` (absolute path), validated
against `session.schema.json`. Startup refuses on any missing/relative/unsafe path.

```json
{
  "env_launcher": "/abs/path/generated/env.sh",
  "srf_bin": "/abs/path/srf-identity-surface",
  "state_dir": "/abs/path/MV_SRF_STATE_DIR",
  "repo": "/path/to/metaviewer",
  "xdg_state": "/dev/shm/vqa-cu/vp-mwrun",
  "attach_env": "$XDG_RUNTIME_DIR/metaviewer/attach/current.env",
  "capsules": [
    {"identity": "caps4-1", "container": "srf-id-caps4-1", "window": "w1", "surface_id": "surface-abc"}
  ]
}
```

- `env_launcher`: generated `env.sh` that runs a command in the app's clean env.
- `srf_bin`: absolute `srf-identity-surface` binary (read-only `ax` only).
- `state_dir`: the app's `MV_SRF_STATE_DIR`.
- `repo`: CAND1 root (source of `agents/` + `components/wender-agent`).
- `xdg_state`: `/dev/shm/...` private evidence base (`XDG_STATE_HOME`); also
  holds the browser-task lock file `vp_mcp.browser_task.lock`.
- `attach_env`: absolute path of the live attachment `current.env` (the
  `METAVIEWER_ATTACH` value). It must export `MV_CORE_CONFIG` and
  `XDG_RUNTIME_DIR`; the trusted registration in `MV_CORE_CONFIG` supplies
  `session_id`, the agent peer and the core instance. Exactly one agent peer
  is required (or `MV_AGENT_INSTANCE` in the attach file selects one). Core
  calls run as
  `env_launcher /usr/bin/env MV_CORE_CONFIG=... XDG_RUNTIME_DIR=... PYTHONPATH=<repo>/agents:<repo> python3 -B -m metaviewer_agents.mvctl <verb> ...`.
- `capsules`: list of `{identity, container, window:"w1", surface_id?}`.
  `window` must match `w[1-16]`. `surface_id` is optional: when present,
  core calls observe it directly; when absent, the identity is resolved to
  its single active browser surface via `mvctl list` (`profile_id`).

## Tools

Each tool is bounded (timeouts + length caps), takes argv-list-only subprocesses
through the injectable runner seam, and returns compact JSON text (no secrets).

| Tool | Arguments | Behaviour |
|---|---|---|
| `vp_list_capsules` | — | `mvctl list --limit 256` (paginated, through the clean env + attach file) per page; maps each configured identity to its single active browser surface (`profile_id` → `surface_id`); returns identities/containers + `surface_id`/`kind`/`state`. No title/url (the core surface has none). |
| `vp_navigate` | `identity`, `url` | Only `https://` (literal-only private/loopback/link-local refusal; no DNS is performed, so DNS names resolving to private addresses, e.g. `127.0.0.1.nip.io`, are not refused here — capability claims must say "literal private hosts refused" and rely on the capsule egress policy for the real boundary). A URL with a non-empty query (`?x=...`) or a non-empty fragment (`#x`) is refused before any input (no list, ax or proposal) with `isError` true and payload `{error, code, url, completed_proposal_ids: [], hint}`, where `code` is `URL_QUERY_UNOBSERVABLE` or `URL_FRAGMENT_UNOBSERVABLE`: the capsule AX surface reports `window.url` without query or fragment by design, so a query could never verify and a fragment (e.g. a hash route) could never be proven; an empty `?` or `#` is accepted. Verification is therefore document-URL verification (scheme, host, port, path). Sends `Control_L+l`, the URL text, and `Return` as three core proposals (two when already on a genuine `about:blank` — empty URL, exact title `about:blank - Chromium`, and no ax page elements — where Chromium already selects the omnibox text). Each step first observes a fresh frame and binds `based_on_frame {sha256, generation}` to it, then `mvctl propose` + `mvctl wait-verify`; the verified receipt is required before the next step. Any refusal (notably `STALE_FRAME`) or unverified input (`INPUT_UNCERTAIN`) stops navigation without blind replay. Success only if the observed URL equals the requested URL after normalisation (scheme, host lowercase, trailing-slash tolerant path, query preserved); on mismatch the whole sequence is retried once, then `isError` with `observed_url` and `url_mismatch`. No verification failure is tolerated; every step must verify. Returns identity, surface, URL and proposal ids. |
| `vp_browser_task` | `identity`, `goal`, `text?`, `key?` in `{Enter}` | Runs `env_launcher /usr/bin/env XDG_STATE_HOME=... MV_WENDER_ROOT=<repo>/components/wender-agent PYTHONPATH=<repo>/agents:<repo> python3 -B -m metaviewer_agents.mw_run --identity I --surface-kind browser --goal G [--text T] [--key Enter] --steps 6 --timeout-ms 30000 --planner rules`; returns `rc`, `verdict_code`, `report` path, `attempts` status list from `report.json`. Serialised: a process-wide lock plus a file lock under `xdg_state` queue concurrent calls (bounded wait); on timeout the call fails with `agent busy` instead of running concurrently. |
| `vp_read_page` | `identity`, `max_chars?` (≤6000) | `srf ax --container C --window w1 --state-dir S` (read-only); returns `title` + compact projection of heading/link/paragraph/static-text names in order, truncated; marked `UNTRUSTED PAGE CONTENT`. |
| `vp_identity_list` | none | Lists identities with public metadata only (`profile_id`, `browser`, `account_label`, `allowed_domains`, `created_unix`, `last_login_unix`). Runs `tools/mv_identity.py list` through the session `env_launcher`. |
| `vp_identity_create` | `id`, `label`, `domains` | Creates an identity (`id` 2-40 of `[a-z0-9-]`, label 1-128 chars, comma-separated registrable domains). Login stays manual by the operator. Refused by `mv_identity.py`: `mv-` ids, the live profile, foreign prefixes. On `IDENTITY_TIMEOUT` (600 s) nothing is retried: check the identity's status in the host menu first. |
| `vp_identity_set_label` | `id`, `label` | Changes only the label (metadata, native per-id guard, audited `label`). Needs `srf-identity-admin` with the `label` verb (IMP-MCPID). Refused while the identity holds an active lease (`IDENTITY_BOUND`) and for `mv-` ids, the live profile and foreign ids. |
| `vp_identity_delete` | `id`, `confirm` | Deletes an identity; `confirm` must equal `id` (else `CONFIRM_MISMATCH`, nothing runs). Refused while the identity holds an active lease, and for `mv-` ids, the live profile and foreign ids. |
| `vp_show` | none | Shows the running take as a native desktop window: runs the operator launcher `ctl/vp_demo_take.sh show` (xpra seamless attach of :98; nothing restarts). Fixed argv, no shell; `TAKE_NOT_RUNNING` unless the session app is alive and :98 exists. Launcher path overridable with `VP_TAKE_LAUNCHER`. |
| `vp_hide` | none | Hides it (`ctl/vp_demo_take.sh hide`, xpra detach); the program and agents keep running. Same refusals. |

Bounds: `goal` 1..500 chars, `text` 1..4000 chars, `url` ≤2048 chars,
`max_chars` 1..6000. `key` only `Enter`. Unknown `identity` is refused
before any subprocess. Tool-level failures return `isError: true` with
`{"error": "..."}`; protocol errors use JSON-RPC codes (`-32700`, `-32600`,
`-32601`, `-32602`).

## AGY plugin files

Create these two files (see `AGY.md` for discovery: workspace-local
`.agents` dirs; plugin MCP config key `mcpServers`):

`.agents/plugins/viewpoint/plugin.json` (at the workspace root, e.g. `CAND1/`):

```json
{"name":"viewpoint"}
```

`.agents/plugins/viewpoint/mcp_config.json`:

```json
{
  "mcpServers": {
    "viewpoint-agent": {
      "command": "/usr/bin/python3",
      "args": ["-I", "<repo>/mcp/vp_mcp/vp_mcp.py"],
      "env": {"VP_MCP_SESSION": "/abs/path/to/session.json"}
    }
  }
}
```

`-I` (isolated mode) keeps the server immune to user `PYTHON*`/`site` pollution.
`VP_MCP_SESSION` must be the absolute session file path.

Run the server directly:

```bash
VP_MCP_SESSION=/abs/path/to/session.json /usr/bin/python3 -I vp_mcp.py
```

## Tests

```bash
cd mcp/vp_mcp
python3 -B -m unittest discover -s tests -v
```

The fake runner replays real shapes: `mvctl list` (`surfaces[{...}]`),
`observe` (`observation/v0.2` with `frame_ref`), `propose` (`action_verdict/v0.2`
`admitted`), `wait-verify` (`action_receipt/v0.2` verified), `ax` JSON
(`elements[{id,name,role,box,focused}]`), and `mw_run` JSON
(`rc:0, verdict_code:BROWSER_BOUNDED_ACTIONS_VERIFIED`).
Covers `tools/list` schema, happy paths, navigation argv order
(`observe → propose → wait-verify` per step, each bound to the fresh
observation), no native input/capture argv ever built, URL refusals
(`http`, `file:`, `127.0.0.1`, `169.254.x`, `10.x`, `192.168.x`, `localhost`, …),
`STALE_FRAME`/`INPUT_UNCERTAIN` stops without replay, browser-task
serialisation and `agent busy`, unknown identity, timeout handling,
oversized inputs, JSON-RPC errors, and session validation
(`attach_env` required, absolute, existing).
