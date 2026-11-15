# viewpoint-ops: MCP server over the `vp` command line

Track P, lane ops-mcp. One stdio MCP server, `viewpoint-ops`, that lets any
MCP client (Gemini in Antigravity, Claude Code, Codex) run every `vp <noun>
<verb>` command as a tool. Python standard library only; no install step.

| File | Role |
|---|---|
| `viewpoint_ops_mcp.py` | The server: JSON-RPC 2.0, one message per line (`initialize`, `ping`, `tools/list`, `tools/call`, `resources/list`, `resources/templates/list`, `resources/read`, `prompts/list`, `prompts/get`, `notifications/progress`). |
| `MCP_EXTRAS.json` | Per tool: one example call and the usual follow-up tools; the five workflow prompts. Written by `build_extras.py` (edit that, then run it). |
| `tests/test_mcp_max.py` | Resources, prompts, progress, examples, follow-up hints; negative controls. |
| `tests/e2e_agent.py` | End-to-end conformance: a real headless agent with only this server; checks the server audit against the transcript. |
| `tests/test_e2e_check.py` | The e2e checker against the stored audit of a real run, plus the same run broken one thing at a time. |
| `register/REGISTER.md` | Registration for Antigravity, Claude Code and Codex. |
| `MCP_TOOLS.fallback.json` | Tool table used only while `../design/MCP_TOOLS.json` (lane ops-design) is absent. |
| `stub_vp` | Stand-in for `vp` used only while `../cli/vp` (lane ops-cli) is absent. Starts nothing. |
| `register/` | Ready-to-copy registration files. |
| `tests/test_viewpoint_ops_mcp.py` | Scripted JSON-RPC client with negative controls. |
| `tests/conformance.py` | Dry-runs every tool of the active table against the active `vp`; lists the ones `vp` does not accept. |

## What a tool call does

1. Looks the tool up in the table. Unknown name: JSON-RPC error `-32602`.
2. Checks `arguments` against the tool's `inputSchema` (type, required, enum,
   length, range, pattern, items, `additionalProperties`). On a violation
   nothing is started; the reply is a receipt with `code: "SCHEMA"`, `rc: 2`.
3. If the tool is destructive and the call has neither `confirm` nor
   `dry_run: true`, nothing is started; the reply is `code:
   "CONFIRM_REQUIRED"`, `rc: 4`, and `next` holds the same command with
   `--dry-run`. The dry run prints the confirm id. The server only checks
   that an id is present; `vp` checks that it is the right one.
4. Runs `vp <noun> <verb> [positionals] [--flags]` in its own session, with a
   time limit. No shell is involved: arguments are passed as an argv list.
5. Takes the last stdout line that is a JSON object with `ok`, `code` and
   `rc` as the receipt. The rest is the human text.
6. Returns `content[0].text` = bounded human text, then the receipt as the
   last line; `structuredContent` = the same receipt; `isError` = `not ok`.

### Receipt

`{"ok": bool, "code": str, "rc": int, "data": {...}, "next": [...], "mcp": {"tool", "ms", "spool"?}}`

`mcp` is added by the server; `mcp.next_tools` names the tools that match the
`vp <noun> <verb>` lines of `next`. `rc` stays inside the closed classes of
vpops/1.1 (`design/COMMANDS.md`). Codes the server itself can produce:

| code | rc | meaning | was `vp` started |
|---|---|---|---|
| `SCHEMA` | 2 | arguments do not fit the schema | no |
| `CONFIRM_REQUIRED` | 4 | destructive tool without `confirm` | no |
| `CLI_MISSING` | 8 | no executable `vp` | no |
| `TIMEOUT` | 124 | time limit passed; the child's process group was stopped | yes |
| `NO_RECEIPT` | 9 or exit status | `vp` printed no receipt line | yes |
| `RC_MISMATCH` | exit status | receipt said ok but exit status was not 0 | yes |

Every other code comes from `vp` unchanged.

### Bounded output and paging (newton-py reuse)

The human text is cut at `VP_OPS_MAX_BYTES` (default 4096) on a line
boundary. When it is cut, or the receipt alone is over the ceiling, the whole
output goes to a spool file (mode 0600) and the receipt gains

    "mcp": {"spool": {"path", "bytes", "lines", "sha256", "read"}}

and one more `next` entry: `<newton-py> read <spool> 1:60`. Paging is not
re-implemented here: that is newton-py's bounded `read` (60 lines / 4,096 B
pages with a `next:` continuation), and `newton find '<terms>' <spool>` finds
a line first. `vp` itself spools through `newton sh`; this layer only covers
what still arrives too long.

### Argument to flag mapping

| argument | command line |
|---|---|
| name listed in the tool's `positional` | bare word, in the listed order (after `--` if it starts with `-`) |
| `"dry_run": true` | `--dry-run` |
| `"confirm": "c-12ab"` | `--confirm c-12ab` |
| `"max_steps": 5` | `--max-steps 5` |
| `"tags": ["a","b"]` | `--tags a --tags b` |
| `false` or `null` | omitted |
| object | one JSON word |
| name given by the tool's `stdin` key | sent on standard input, not on the command line |

Flags are emitted in sorted order, so a call always gives the same command.

### Tool table format (`MCP_TOOLS.json`)

A list, or `{"tools": [...]}`. Per tool: `name`, `description`,
`inputSchema`, `annotations` (only these four are published), plus routing
keys:
`tier` (`core` or `extended`), `argv` (`["capsule","start"]`; also accepted as `command`/`cli`/`vp`, list or
string, with or without a leading `vp`), `destructive` (or
`annotations.destructiveHint`), `positional`, `stdin`, `timeout_s`. Without
`argv` the words are derived from the name: `vp_rewind_restore` gives
`rewind restore`, `vp_agents_wait_verify` gives `agents wait-verify`.

A table that is empty, not JSON, or has a duplicate or malformed name stops
the server at start with exit status 78 and one line on stderr.

## Tools: examples and follow-ups

Every tool of the table is published with `inputSchema.examples[0]` (one call that fits the schema) and two
description lines, `Example arguments: {...}` and `Usually followed by: ...`. An example that does not fit the
schema is not published; a follow-up that is not a tool is dropped. `tests/test_mcp_max.py` sends all 91
examples through the real `vp` as dry runs: 90 answer `DRY_RUN`; `vp_record_stop` answers `FOREIGN_JOB`
because the example pid is not a recording of the session (that refusal is the CLI's ownership guard).

`mcp.next_tools` in a receipt = the tools matching the `vp <noun> <verb>` lines of `next`, then, only when
the call succeeded, the tool's usual follow-ups. After a failure only what `vp` named is listed.

## Resources (bounded)

`resources/read` returns at most `VP_OPS_RES_MAX_BYTES` (default 12288) of text per reply, cut on a line
boundary. A longer resource ends with `[cut: bytes A..B of N; next page: <uri>?offset=B]` and
`_meta = {bytes:[A,B], total_bytes, sha256, next}`; read `next` to continue. Pages reassemble to the exact file.

| URI | Content |
|---|---|
| `viewpoint://docs/agent-guide` | `docs/AGENT_GUIDE.md` (start here) |
| `viewpoint://docs/troubleshooting` | `docs/TROUBLESHOOTING.md` |
| `viewpoint://docs/family/{noun}` | `docs/FAMILY_<noun>.md`, 18 pages, each also listed by `resources/list` |
| `viewpoint://docs/scenarios`, `viewpoint://docs/commands` | `design/E2E_SCENARIOS.md`, `design/COMMANDS.md` |
| `viewpoint://tools/index` | every tool: name, tier, destructive, vp command (the default `tools/list` shows tier core only) |
| `viewpoint://state/capsules` | receipts of `vp capsule status` and `vp sys jobs` (two read-only calls, 20 s limit each) |
| `viewpoint://state/queue-head` | default runtime root (`VP_SWARM_RT`): mode from the last control record, last turn, last close, events enqueued and not closed, intent files per agent |
| `viewpoint://state/last-frame` | the frame record with the highest number (no image bytes; lists over 12 items and strings over 160 characters are cut) |
| `viewpoint://state/events-tail` | last events of `events.jsonl`, at most 40 and at most half the byte ceiling |
| `viewpoint://state/microtasks` | one row per `close` record (event, turn, verdict, frame), last 30, with counts by verdict |
| `viewpoint://rt/{rt}/queue-head` and `/last-frame`, `/events-tail`, `/microtasks` | the same four views of another runtime root; `{rt}` is a name under `VP_OPS_RT_BASE` or a percent-encoded absolute path |

The state views are computed by the server from the files of the runtime root (contract of the reference
units: `events.jsonl`, `frames/frame_<N>.json`, `intents/<agent>/`). They read the last 256 KiB of the log,
never write, never open a PNG. `queue-head` is derived from the log; `vp_queue_status` remains the broker's
own answer. A runtime root that resolves (after symlinks) inside `/dev/shm/vqa-cu/hybrid` or
`/dev/shm/vqa-cu/swarm` is refused with `FORBIDDEN_PATH`. Unknown URI: error `-32002`. Bad offset or bad
`{rt}`: `-32602`. Subscriptions are not implemented (`resources/subscribe` answers `-32601`).

## Prompts (ready workflows)

`prompts/list` names five; `prompts/get` returns one user message with the numbered tool sequence, the
decision points and the safety rules. Arguments are single-line strings of at most 512 characters; a missing
required one, an unknown one, or one holding `{{` or a control character is refused with `-32602`.
`_meta.tools` lists the tools the text names (the tests check that each exists in the table).

| Prompt | Arguments (required in bold) | Source scenario |
|---|---|---|
| `hybrid_blender_build` | job, rt, evidence, room, **asset** | E2E 1 |
| `four_agent_queue_build` | job, rt, evidence, room, tasklight_room (default `demo`), turns | E2E 2 + `vp_queue_simulate` (auction A/B, offline) + a checkpoint before the run |
| `rewind_wrong_edit` | job, rt, room, to (default `last_good`), **reason** | E2E 3 |
| `browser_capsule_search` | identity, url, **query**, room | E2E 4 |
| `human_handback` | rt, room, by, **reason**, display | E2E 5 |

The auction scheduler is not wired into the live broker (docs/FAMILY_queue.md); the prompt says so and uses
only the offline comparison.

## Progress notifications

A `tools/call` whose `params._meta.progressToken` is a string or an integer gets `notifications/progress`
messages before its result: one when `vp` starts (pid and time limit), one every `VP_OPS_PROGRESS_S` seconds
while it runs (elapsed of limit), one when it ends (receipt code, rc, ms; `total` set). `progress` increases
by one each time. Without a token, or when the call is refused before `vp` starts, none is sent. The server
handles one request at a time; `notifications/cancelled` is not implemented (the time limit stops the child).

## Audit file

With `VP_OPS_AUDIT=<file>` the server appends one JSON line per `tools/call` (tool, arguments, dry_run,
destructive, ok, code, rc, started, next_tools), `resources/read` and `prompts/get`. Strings over 400
characters are cut and secret values are redacted. This is what the end-to-end check trusts.

## End-to-end conformance with a real agent

    python3 -I tests/e2e_agent.py --live --model claude-sonnet-5-5 --out /dev/shm/vqa-cu/ref-mcp-max-e2e-run

It runs, inside the memory-capped scope,
`claude -p --verbose --model claude-sonnet-5-5 --mcp-config <json> --strict-mcp-config --output-format stream-json --tools "" --allowedTools mcp__viewpoint-ops`
with the task: check health, start a private Blender capsule (dry run), run one 2-agent queue scenario
(stub agents + 2 turns, dry runs), read the events for real, stop everything (dry runs), list the owned jobs.
The checker (`check()`) fails the run when: a tool outside this server was used; the transcript and the audit
disagree in count, order or receipt code; a class X, D or H tool ran without `dry_run`; a runtime root, job,
room or display outside the assignment appears; or one of the eight steps is missing.

Stored run (`tests/evidence/e2e_agent_run/`: `audit.jsonl`, `verdict.json`): 9 tool calls, all through
`mcp__viewpoint-ops__*`, 0 violations, 15.3 s, model claude-sonnet-5-5. The raw transcript is not stored (it
carries host paths); `--check DIR` re-checks a run that still has it. Limits of this evidence: the events
were a hand-written 2-agent log fixture, and no Blender, agent or broker really started, so it shows that an
agent can drive the tools and read the receipts, not that the wrapped units work live.

## Configuration

| Variable | Default |
|---|---|
| `VP_OPS_TOOLS` | `core`: `tools/list` shows the tools of tier core (54 of 88 in vpops/1.1). `all` shows every tool. Every tool is callable in both modes. |
| `VP_OPS_TOOLS_PATH` | `../design/MCP_TOOLS.json`, then the ops-design worktree, then `MCP_TOOLS.fallback.json` |
| `VP_OPS_CLI` | `../cli/vp`, then the ops-cli worktree, then `stub_vp` |
| `VP_OPS_SPOOL` | `$XDG_RUNTIME_DIR/vp-ops/spool` |
| `VP_OPS_MAX_BYTES` | 4096 |
| `VP_OPS_TIMEOUT_S` | 120 (a tool's `timeout_s` overrides it) |
| `VP_OPS_NPY` | `~/.local/share/newton-py/venv/bin/newton` |
| `VP_OPS_EXTRAS` | `MCP_EXTRAS.json` next to the server; missing or broken file = no examples, no prompts |
| `VP_OPS_DOCS` | `../docs` |
| `VP_OPS_RES_MAX_BYTES` | 12288 |
| `VP_OPS_RT_BASE` | `/dev/shm/vqa-cu` |
| `VP_SWARM_RT` | runtime root of `viewpoint://state/*` (default `$VP_OPS_RT_BASE/ref`) |
| `VP_OPS_PROGRESS_S` | 2 |
| `VP_OPS_AUDIT` | unset (no audit file) |

`python3 -I viewpoint_ops_mcp.py --check` prints one receipt naming the table
and command line in use, the tool count, the destructive tools, and whether
the stand-in is active (`cli_is_stub`). The server answers `initialize` with
the client's protocol version when it is 2024-11-05, 2025-03-26 or
2025-06-18, and with 2025-06-18 otherwise.

Secrets: the server never reads credential files. Values of environment
variables whose name contains TOKEN, SECRET, PASSWORD, API_KEY, PRIVATE_KEY
or CREDENTIAL are replaced by `[REDACTED]` in everything returned.

## Registration

Full notes per client, with the checks to run afterwards: `register/REGISTER.md`.
Replace `REPO` with the absolute path of the checkout.

Antigravity (flags go before the name; `--` before the command because its
arguments start with `-`):

    agy mcp add --type stdio viewpoint-ops -- python3 -I REPO/product/agent_interface/mcp/viewpoint_ops_mcp.py

With a non-default command line or tool table:

    agy mcp add --env VP_OPS_TOOLS=all --env VP_OPS_MAX_BYTES=4096 viewpoint-ops -- python3 -I REPO/product/agent_interface/mcp/viewpoint_ops_mcp.py

Claude Code: copy `register/claude.mcp.json` to `REPO/.mcp.json` (paths in it
are relative to the repository root), or:

    claude mcp add --scope project viewpoint-ops -- python3 -I product/agent_interface/mcp/viewpoint_ops_mcp.py

Codex: append `register/codex.config.toml` to `~/.codex/config.toml`, or:

    codex mcp add viewpoint-ops -- python3 -I REPO/product/agent_interface/mcp/viewpoint_ops_mcp.py

Check by hand, without any client:

    printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | python3 -I viewpoint_ops_mcp.py

None of the registration commands above was run by this lane: they write
client configuration outside the worktree.

## Tests

    systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 python3 -I tests/test_viewpoint_ops_mcp.py
    systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 python3 -I tests/test_mcp_max.py
    systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 python3 -I tests/test_e2e_check.py

`test_mcp_max.py` writes its runtime fixture under `/dev/shm/vqa-cu/ref-mcp-max-t<pid>/` and removes it.

The client starts the server as a child process and talks to it over pipes.
Negative controls: parse error, invalid request, unknown method, unknown
tool, seven schema violations, destructive tool without confirm (missing,
blank, `dry_run: false`, wrong id), missing command line, bad tool tables,
no receipt, failing receipt, exit status contradicting the receipt, time
limit, secret echo, positional that looks like a flag.

## Scope of the confirm gate (xr-ops-docs A5)

`confirm` guards against slips, not against misuse. Each confirm id is computable from the call's own
arguments (`vp_vision_stop`: the job id; `vp_record_stop`: the pid; `vp_capsule_start|stop`: `take`;
`vp_agents_stop`: the evidence dir basename; source: the `confirm=` entries of `VERBS` in `cli/vp`), so a
model can satisfy it without a human seeing the request. The independent guard is the CLI's session
ledger: jobs not started by this session are refused with `FOREIGN_JOB` (rc 3). A server-minted nonce
returned by the dry run is not implemented.
