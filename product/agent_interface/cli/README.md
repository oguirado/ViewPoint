# `vp` dispatcher (lane ops-cli, Track P, claude-fable-5-2)

One entry point, standard library only: `python3 -I product/agent_interface/cli/vp <noun> <verb> ...`
Taxonomy: vpops/1.x of `product/agent_interface/design/` (COMMANDS.md, MCP_TOOLS.json). All 88 commands are dispatched.

## Contract

- stdout: bounded human text (at most `--tail` lines, 240 chars each, 4,096 bytes), then one last line, the receipt
  `{"ok","code","rc","data","next"}`. `--json` prints the receipt only. Exit code = `rc`.
- `rc` classes: 0 OK, 1 FAIL, 2 USAGE, 3 REFUSED, 4 CONFIRM_REQUIRED, 5 NOT_FOUND, 6 BUSY, 7 STATE, 8 DEPENDENCY, 9 UPSTREAM, 124 TIMEOUT.
- `--dry-run` on every command: `code` DRY_RUN, `data.argv` = the upstream command, `data.env` = variable names only,
  `data.would_refuse` when the real call would be refused for ownership. Nothing starts, the ledger is not written.
- Class D needs `--confirm <id>`; the id is the literal named per command (`take`, the job, the pid, the `--to` point...).
- Guards before any side effect: display `:1` (`FORBIDDEN_DISPLAY`, also inside command strings; an inherited `DISPLAY=:1`
  is removed from the child environment), `/dev/shm/vqa-cu/hybrid` (`FORBIDDEN_PATH`), credential-looking paths and
  `~/.gemini` (`SECRET_PATH`), jobs outside the ledger or without the `vpops` prefix (`FOREIGN_JOB`), `--kill-all` (USAGE).
  Overrides are explicit: `--allow-display :1`, `--allow-hybrid`, `--foreign-ok <job|pid>`.
- Secrets are redacted from human text and from every string of the receipt (`data.redactions` counts them).
- Processes are stopped by recorded PID + kernel start time only (`vp record stop --pid N --confirm N`).

## What is reused from newton-py (called, not copied)

| newton-py verb | Used for |
|---|---|
| `sh --tail N --head N --timeout-s S -- argv` | runs every wrapped entry point: spool, sha256, line/byte counts, bounded preview, timeout |
| `read PATH A:B` | `vp out read`, `vp queue events`, `vp queue observe` (ref) |
| `find`, `tok.count` | `vp out find`, `vp out tokens` |
| `route` | `vp docs route` |

If the newton-py executable is missing, a builtin spooler takes over (`data.spooler: "builtin"`, spool under `$VP_OPS_STATE/spool/`).
`vp docs card|ask|pack|locate|show|check|build` call the router of lane ops-docs (`../router/vp_docs.py`).

## Config

Paths: defaults in `vp` (`DEFAULT_PATHS`), overridden by a JSON file (`$VP_OPS_CONFIG` or `cli/vp_paths.json`) and by
env `VP_OPS_<KEY>`. State: `$VP_OPS_STATE` (default `$XDG_RUNTIME_DIR/viewpoint-ops`): `jobs.json` (JSON lines ledger), `spool/`.
`vp sys doctor` checks every entry point and the coverage against MCP_TOOLS.json; `vp sys help [noun [verb]]` is the index.

## Tests

```
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 python3 -I product/agent_interface/cli/tests/test_vp.py
```

## Known gaps

- `engine observe|list|propose|wait-verify`: the mvctl connection flags are passed through by the caller; they are not derived from the session file.
- `queue enqueue --impl ref` takes a ready intent (`--file INTENT.json`); flag-built intents only with `--impl swarm`.
- `blender exec --bridge ref`: AUSENTE (use the queue); `blender verify`, `agents transcript`: child args pass through untranslated.
- newton-py `sh` spools under its own directory (`$XDG_RUNTIME_DIR/newton-spool`), not `$VP_OPS_STATE/spool/`; the raw spool is not redacted.
- Upstream-specific codes (FROZEN, STALE_FRAME, HOLD...) are surfaced only when the child prints a JSON `code`; otherwise FAIL/UPSTREAM with `data.upstream_rc`.
- Class X/H/D verbs were exercised in `--dry-run` only (no take, Blender or recorder was started by this lane).
