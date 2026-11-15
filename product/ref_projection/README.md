# ref_projection: room + TaskLight projection of broker events (unit F3)

Reference implementation, Track P (declared non-Gemini product work), written by Claude (claude-fable-5-2).
Stdlib Python only. It reads the broker's `RT/events.jsonl` and never writes it: the JSONL is the order of
record, both rooms are projections.

## Files

| File | Role |
|---|---|
| `project.py` | Tails `RT/events.jsonl`; posts to OpenSwitchboard through the `osb` CLI and to TaskLight through the context-wall MCP server (stdio JSON-RPC: `initialize`, `tools/list`, `tools/call`). |
| `reconcile.py` | Flags agents without an acknowledged `close_task` (`MISSING_CLOSE_TASK`) or without a plan (`NO_PLAN`). rc 0 clean, 1 flagged, 2 bad input. |
| `tests/test_projection.py` | 16 offline tests against test doubles of osb and of the MCP server. |
| `tests/test_live.py` | 1 live test (only with `VP_LIVE=1`): real osb room `viewpoint-capsule-test`, real context-wall MCP, TaskLight room `demo`. |
| `tests/fixtures/` | Synthetic streams: `events_complete.jsonl`, `events_missing_close.jsonl`. |
| `evidence/` | Output of the live run: `osb_mirror/` (room mirror + `LOG.md`), `mcp_responses.jsonl`, `live_ledger.jsonl`, `live_summary.json`. |

## Run

```
export XDG_RUNTIME_DIR=/run/user/1000
# one shot: read to EOF, close the cards, exit
python3 product/ref_projection/project.py --once --osb-room viewpoint-capsule --tasklight-room viewpoint
# follow the broker; SIGTERM / SIGINT (or --idle-exit S) finalizes
python3 product/ref_projection/project.py --osb-room viewpoint-capsule --tasklight-room viewpoint
# reconcile
python3 product/ref_projection/reconcile.py --events $RT/events.jsonl --ledger $RT/projection/ledger.jsonl
```

`RT` is `$VP_SWARM_RT` (default `/dev/shm/vqa-cu/ref`) or `--rt`. Defaults for the rooms are the test rooms
(`viewpoint-capsule-test`, `demo`). The projector writes only `RT/projection/state.json` (byte offset and card
state, so a restart does not replay) and `RT/projection/ledger.jsonl` (every outbound call with its response).
`RT/claims.json`, if present, names the collection in the card title.

Tests:

```
cd product/ref_projection
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 python3 -m unittest discover -s tests -t .
VP_LIVE=1 systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 python3 -m unittest tests.test_live
```

## Mapping

| Broker record | osb act | TaskLight |
|---|---|---|
| `enqueue` | `claim` (`--ref <event_id>`) | first of an agent: `post_plan`; later ones: `post_plan` again, same title, step appended; `post_status waiting` |
| `turn` | `ack` (turn, turn_epoch, head_frame) | `post_step active` on the agent's oldest open step; `post_status acting` |
| `close` `VERIFIED`, `VERIFIED_WITH_SPILL`, `STATE_VERIFIED` | `evidence` (`--ref`) | `post_step done`; `post_status waiting` |
| `close` `STALE_FRAME`, `TURN_TIMEOUT`, drops < 3 | `refute` (`--ref`) | `post_step pending` (back to the tail); `post_status waiting` |
| the same with drops >= 3 (parked) | `refute` (`--ref`) | `post_step blocked`; `post_status blocked` |
| `close` `QUEUE_FULL`, `DUPLICATE_EVENT` | `refute` (`--ref`) | `post_status blocked` only; an existing step is never changed |
| `close` any other code (`CLAIM_REFUSED`, `JOB_ERROR`, `NO_VISIBLE_CHANGE`, `INPUT_UNCERTAIN`, ...) | `refute` (`--ref`) | `post_step blocked`; `post_status blocked` |
| `close` `PASS` | none | `post_status waiting` |
| `control` `stop` / `resume` | `verdict` from `by` | `post_status blocked` (paused) / `waiting` for every open card |
| `cursor`, other `control` ops | none (not turn-level; counted in `state.ignored`) | none |
| end of run | none | `close_task` for each agent whose steps are all `done` or `blocked` |

Design decisions:
- A refute without a ref is refused inside `OsbSink.post` before the CLI is called. A close without an
  `event_id` uses `turn:<n>` as the ref.
- `script` and `keys` never leave the machine: room texts carry only lane, kind, write set, frame numbers and
  `expect` (clipped). Ids are checked against a slug pattern and passed as `--text=` / `--ref=` so a hostile
  id cannot become a CLI option; bad records become `state.anomalies`, not posts.
- An unverified close is never posted as `evidence`. Unknown verdict codes are projected as refusals and logged.
- An agent with an open step is deliberately not closed; the reconciler then flags it. The reconciler reads
  the event log and the ledger of server-acknowledged calls, not the projector's state.
- Server limits are respected by construction: 12 steps per card, text 80, accept 160, outcome 240, title 120.
  A 13th enqueue of one agent is kept in `overflow`, reported by `post_status`, and keeps the card open.

## Verified (ran on 2026-11-14)

- Offline: 16 tests pass. Mapping counts per act, exact step sequences per agent, torn-line handling and
  restart from saved state, follow mode with SIGTERM, MCP handshake and schema guard, `isError` surfaced.
- Negative controls, checked by mutation (guards removed in a scratch copy, 3 tests went red):
  `test_negative_control_refute_without_ref_is_refused` (remove the ref guard) and
  `test_negative_control_missing_close_is_flagged` plus `test_cli_exit_codes` (make `finalize` close every card).
- Live: 1 test passes. The 23-line fixture produced 20 osb posts in `viewpoint-capsule-test` (5 claim, 7 ack,
  4 evidence, 2 refute, 2 verdict; all rc 0, read back from the room by id) and 48 context-wall tool calls in
  TaskLight room `demo` (all `isError: false`), with `close_task` acknowledged for modeler-agent, props-agent,
  look-agent and camera-agent; reconciler clean. The per-agent ids are accepted by the server.

## Not verified

- No run against a real broker: the stream is synthetic and follows the contract and 02 section 7. Field names
  of a broker `PASS` close and of `control` ops other than stop/resume are assumptions.
- The TaskLight wall UI was not opened: how the wall renders a re-posted plan (appended step) and a step that
  returns to `pending` is not checked, only that the server stores the events.
- Delivery is at-least-once (failed calls are retried from the ledger outbox at the next start, see fix-io below): a crash between a post and the state save can repeat the posts of one batch.
- The spec's `handoff` (token transfer) act is not projected: the contract has no such record.
- `close_task` is driven by end of run, not by a `verdict accept` record (absent from the contract).
- osb posts are sequential subprocess calls; throughput under a long event burst was not measured.

## Hardening (w186 cell, lane sw-overlay-projection)

Tests: `python3 -m unittest discover -s tests -t .` (25 new in
`tests/test_harden.py`, doubles `fake_osb_flaky.py`, `fake_mcp_hang.py`).

- No duplicate posts after a restart: every outbound call carries an
  idempotency key `<record line>.<ordinal>` written to `ledger.jsonl`; the
  state is saved after every record; a restarted projector redoes at most one
  record and skips the calls the ledger already shows acknowledged. Remaining
  window: a kill between a sink's acknowledgement and the ledger append.
- osb: up to 3 attempts; before each retry the room is read back (`osb read
  --json --since <last acked Lamport>`) and an act that already landed is not
  posted again.
- MCP: one reader thread per server; any transport error stops the server and
  the next call starts a fresh one. `post_plan`, `post_step`, `close_task`
  are retried once; `post_status` never (a retry could show it twice). A
  server that cannot start no longer stops the osb projection.
- Untrusted records: NUL, control, bidi characters and lone surrogates are
  removed from every projected text; wrong JSON types and NaN in `drops`,
  `write_set`, `spill_regions`, `rec`, `op` are tolerated; a close for an
  already terminal event is an anomaly, not a second evidence post.
- A truncated or replaced `events.jsonl` (first-line sha256 kept in the
  state) is refused with exit 3: use a fresh `--out`.
- `reconcile.py`: new flags `CLOSED_WITH_OPEN_STEPS`, `PLAN_MISSING_EVENTS`,
  `OSB_MISSING_CLAIM`, `OSB_DUPLICATE_POST`, `UNPARSEABLE_ENQUEUE`; output
  gains `bad_lines`.

## fix-io (w192 cross-review xr-io, findings P1, P2, P3, P5)

Tests: `tests/test_fix_io.py`, 10 tests; each fix test was red before its fix. Full suite: 52 run, OK, 1 skip (live).

| Finding | Fix | Test (red before) |
|---|---|---|
| P1 poison line | `pump` and `reconcile.load_jsonl` catch `RecursionError`/`MemoryError` and do not parse lines over `MAX_LINE_BYTES` (1 MiB): one anomaly, the offset advances, later records are projected. The ledger loader and the claims loader catch it too. | `TestP1PoisonLine` (3) |
| P3 lost call | The ledger is the durable outbox: a keyed call that failed and was never acknowledged stays in `Ledger.pending` (rebuilt from disk). `retry_outbox` runs at every start, before new records. The summary has `outbox_retried` and `unacked`; rc is 1 while anything is unacked. `reconcile.py` flags `UNACKED_CALL` for any act or tool. An unacked `post_plan`/`post_status` is dropped when a later acknowledged call of the same tool and agent supersedes it. | `TestP3LostCall` (3); negative control: with the `retry_outbox` call removed, `test_failed_osb_post_is_retried_on_restart` fails `0 != 1` |
| P2 false dedup | The verify-before-retry only looks above a baseline: the room head read once before the first post of the process, then each acknowledged or matched act. History of earlier runs never counts as delivered. If the room cannot be read, nothing is assumed delivered. | `TestP2FalseDedup` (1 red before, 2 regression guards) |
| P5 claims | `clean_claims`: only `str` elements matching `col:<name>` are kept. | `TestP5Claims` (1) |

Still open:
- P2 residue: the match above the baseline is still (agent, act, text); the idempotency key is not written into the posted act, so a different process posting the same text for the same agent in the retry window would count as ours.
- P3 residue: outbox retries run before new records, not in the original order relative to calls that succeeded later in the failed run; a call that fails for a permanent reason (a TaskLight validation code) is retried at every start and keeps rc 1.
- A post that landed while both the CLI and the room read-back failed is posted again on restart (at-least-once, a duplicate is possible).
- P4 (active step after a requeue) and P6 are not addressed.
- Not run against the live osb room or the real MCP server in this lane (doubles only).
