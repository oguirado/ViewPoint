# H vs Ref Stack Comparison (P6, claude-fable-5-2, Track P)

Snapshot taken 2026-11-14 12:56 CET. Track H files were read in place under `viewpoint-hackathon/swarm/` (branch `swarm-1114`, HEAD `df0946d`); the reference side was read with `git show` from the `p-fable-*` branches. Both late Track H writers (`H3_r1_1249`, `H4_r2_1249`) had exited before the overlay and projection files were read.

How to read the evidence:

- Nothing was executed for this report. Test counts are static counts of test functions [MEASURED]; pass/fail figures are quoted from each side's own logs and READMEs [CITED].
- This document contains no code from `product/ref_*`. Wire literals (paths, field names, flags, room names) are quoted because the seam table needs them.
- All Track H unit directories (`swarm/broker`, `agents`, `overlay`, `projection`, `tools`) are **untracked in git**. A stray clean or checkout would lose them. Commit them before the 13:30 gate.

## Verdict for the kill gates

- **13:30 gate: go with the H stack.** It is the only integrated stack (broker, agents, overlay, projection built to one contract, with `vpswarm` already up). Two things must be done first: commit the files, and get one live GUI-lane turn verified on the repaired broker with viewport auto-rebase switched on. The only live H run so far (12:03) predates the repairs and verified no GUI turn.
- **Fallbacks are not symmetrical.**
  - Broker or agents failing means a **whole-pair swap to F1+F4**. That pair has never been run together, so it is a risky fallback.
  - Overlay failing: **F2 is a drop-in** on the H event log with one flag.
  - Projection failing: **F3 is a drop-in with four explicit flags**, and loses handoff and park projection.
- **Rewind (r0..r4) is not a 13:30 item.** Track H has no checkpoint or restore code, and the merged reference tree has no post-merge live run and no launcher.

## 1. Executive readiness matrix

| Pair | H side | F side | Live-rehearsal readiness | H strengths | H gaps to close |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **H1 vs F1** broker | Untracked; 16 tests [MEASURED], "Ran 16 tests" OK [CITED] | `10efbf7`; 54 tests (34 + 20 crash) [MEASURED], 54 OK [CITED] | **H:** integrated, but live run predates repair; 4 script turns verified, 0 GUI turns. **F:** clean 12-turn live smoke on `vpref1`, crash hardening not run live | Leases with epochs and renewal, script hash check, footprint check, in-run park/checkpoint controls, view and scene epochs, mock vision mode | No restart replay, no fsync, forgeable receipts (nonce file readable by agents), unbounded reads, no timeout retraction, auto-rebase off by default |
| **H2 vs F4** agents | Untracked; 15 tests [MEASURED], 15/15 [CITED] | `ff6b6b1`; 53 tests (24 + 29) [MEASURED], 53/53 [CITED] | **H:** scripted two-agent producer ran on real Blender (script lane only); no model-driven run. **F:** one real modeler agent, no broker involved | Closed feedback loop (wait on close verdict with exit codes), never-repeat rules for uncertain verdicts, identity binding, per-agent memory-capped scope | No script content guard, no runner deadline of its own, GUI lane never verified live |
| **H3 vs F2** overlay | Untracked; 12 tests [MEASURED], 12 OK [CITED]; review REJECTED before r1, no re-review | `d856292`; 36 tests (17 + 19) [MEASURED] | **H:** composed the real event log over a synthetic clip (pre-repair). **F:** synthetic clip plus one hand-run Xvfb take. Neither composed a real Blender take | Rich HUD (cursor motion, phase rings, verdict badge, queue, park, lane, tick, caption), automatic start-time alignment, hardened display gate | Output written in place, unguarded numeric casts, stale prior-run state at frame 0, strip and cursor colours disagree |
| **H4 vs F3** projection | Untracked; 29 tests [MEASURED], 29 OK [CITED]; r1 review REJECTED, r2 unreviewed | `ac847f2`; 42 tests (16 + 25 + 1) [MEASURED] | **H:** no live room run. **F:** live round trip on test rooms with a synthetic stream (20 osb posts, 48 MCP calls, 0 errors) [CITED] | Contract-complete mapping (handoff, park, rollover, PASS-turn suppression), room auto-open, audit against the real room store | Duplicates on crash, silent loss on osb failure, MCP read can hang forever, no truncation detection, no SIGTERM handling, state not persisted unless a flag is passed |

Rewind capability (reference side only; Track H equivalent is absent):

| Branch | Head | Adds | Tests [MEASURED] | Live evidence [CITED] | Readiness and gaps |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `p-fable-r0` | `4d33836` | Contract `rw1`, shared schema, fakes | 29 | None | Interface only; restore and attempt caps unmeasured |
| `p-fable-r1` | `456b996` | Checkpoint store, scene digest v2, mark/restore jobs | 46 (38 + 8 real-Blender) | 21/21 live checks | Proven live, but the GUI run was not repeated after the digest fix |
| `p-fable-r2` | `bdc0f99` | Transactional broker on top of F1 (attempts, rollback, freeze) | 35 | 16/16 live checks, 0 violations | Closest to a rehearsal; a GUI numpad turn cannot commit under the witness check (unresolved) |
| `p-fable-r3` | `ba07977` | Judge (expect, delta, witness, cost) with mutant controls | 44 | 10/10 live; 22/22 mutants red | Live run used a stand-in digest and a hand-written undo |
| `p-fable-r4` | `8646b25` | Merge of r1 + r2 + r3 only | 154 total | No new evidence | No post-merge live run, no launcher, `ref_agents` not merged in |

Track H status on rewind: the contract names a `checkpoint` control and says the watchdog restores the last checkpoint file, but no code saves, loads, digests or rolls back anything.

## 2. Stack seam compatibility table

### 2.1 Wire and runtime differences

| Seam | H stack | F stack | Compatible? |
| :--- | :--- | :--- | :--- |
| Vision job name | `vpswarm` (broker default, also named in agent prompts) | No default; `--job` required; docs and smoke use `vpref1`; rewind runs use `vprw*` | No, but config only |
| Runtime root | `/dev/shm/vqa-cu/swarm/` | `/dev/shm/vqa-cu/ref` (rewind: `ref-rw*`); the F4 runner refuses any root not under `/dev/shm/vqa-cu/ref*` | No |
| Root layout | `inbox`, `outbox`, `rejected`, `frames`, `intents/<agent>/{processed,rejected}`, `nonce.txt` | `inbox/{rejected,retracted}`, `outbox/late`, `frames`, `intents/<agent>`, `claims.json`, `broker.lock`, `cursor.jsonl`, `events.torn` | No |
| Event log location | Outside the runtime root: `swarm/run/events.jsonl` | Inside it: `<root>/events.jsonl` | Path only |
| Environment variables | `SWARM_INBOX`, `SWARM_OUTBOX`, `SWARM_REJECTED`, `SWARM_FRAMES_DIR`, `SWARM_INTENTS_DIR`, `SWARM_EVENTS_LOG`, `SWARM_NONCE_FILE`, `SWARM_BRIDGE_NONCE`, `SWARM_AGENT_ID`, `SWARM_RUN_DIR`, `CW_HOME` | `VP_SWARM_RT`, `VP_SWARM_NONCE` (env only, never on disk, stripped from agent processes), `VP_RECORD_NO_SCOPE` | No shared names |
| `events.jsonl` envelope | One JSON object per line, `v:1`, `rec`, `ts_ms`; **no `seq`** (line order is the order) | Same, plus a strictly increasing `seq` on every record | Readers yes, writers no |
| `events.jsonl` record kinds | `claim`, `enqueue`, `turn`, `close`, `cursor`, `control` | `enqueue`, `turn`, `close`, `cursor`, `control` (no `claim`); rewind adds `ckpt`, `txn`, `judge` and control op `rewind` | F is a subset for base runs |
| `enqueue` record | Carries `script_sha256` only | Embeds the full script plus an intent signature | Consumers tolerate both |
| `close` record | `verdict`, `event_id`, `agent_id`, `turn`, `tick`, `verify_frame_ref`, `drops`, `reason` | Adds `changed_write_set`, `job_ms`, `out_tail`, `requeued`, `turn_epoch_after`, `recovered`; extra verdict `BAD_INTENT` | Consumers tolerate both |
| Job filename | `<nonce>__<seq>__<event_id>.py` | `<nonce>_<event_id>.py`, staged as `.py.tmp` | No |
| Job receipt | JSON file, same stem, with `v, nonce, seq, event_id, status, elapsed_ms, stdout, error, ts_ms` | Text file `<event_id>.out`: status line, then a `META` line with change flag, duration, script hash and a MAC | No |
| `event_id` format | Up to 128 chars, underscore allowed; pattern `<run_id>-<agent_id>-<step>` | Up to 80 chars, **no underscore**; pattern `<agent_id>-<n>`, must equal the filename | H ids with `_` are refused by F |
| Intent file | 20 fields, no `rec`; needs `lease_epoch`, `script_sha256`, GUI `footprint` | Needs `rec:"enqueue"`; unknown fields refused; no lease, hash or footprint | No, both directions |
| Intent lifecycle | Moved to `processed/` or `rejected/`; reused id is `DUPLICATE_EVENT` | Left in place; resubmitting the same id with new content is the rebase path | No |
| Frame JSON | `v, frame, sha256, width, height, view_epoch, scene_epoch, changed_regions, ts_ms`, plus `frames/latest.json` | `v, frame, png, sha256, ts_ms, changed_regions, by_event` (optional `regions_exact`); **no `latest.json`, no epochs** | No |
| Cursor event | In `events.jsonl` only: `agent_id, event_id, phase, x, y, frame, label, color, ts_ms` | In `events.jsonl` and mirrored to `cursor.jsonl`: same minus `label` and `color`, plus `seq` | Overlays tolerate both |
| Cursor slots | 480,420 / 1080,620 / 780,300 / 800,500, or the intent's `cursor_xy` | Script lane parked at x=1700 per-agent rows; GUI at 790,530 | Cosmetic |
| Claims and leases | `claim` records with `lease_epoch`, TTL, prefix lists (`col:CHAR_`, `obj:PROP_`, `mat:`, `cam:` ...) | Static `claims.json`, exact collection names (`col:CHAR_mesh` ...), no lease | No |
| Control channel | Intent files under `intents/director-agent/`; ops `stop, resume, reconcile, park, checkpoint`, applied mid-run | CLI subcommand needing the broker lock, so only between runs; ops `stop, resume, reconcile` | No |
| Recording | 15 fps, system mouse drawn, JSON sidecar with `start_ts_ms` | 30 fps, system mouse hidden, plain-number `.start_ms` sidecar | Sidecars differ |
| `osb` room | `viewpoint-capsule`; auto-opened; acts `claim`, `evidence`, `handoff`, `ack`, `refute`, `verdict`; broker identity `broker-projector` | Default `viewpoint-capsule-test`; never opens the room; no `handoff`; everything posted as the agent | Flag changes room; acts differ |
| TaskLight room | `viewpoint`; card `Task plan for <agent>`, 12 generic steps with rollover | Default `demo`; card `[ViewPoint capsule] <agent> on <prefixes>`, one step per event, hard ceiling of 12 | Same tools, different card semantics |
| TaskLight tools | `post_plan`, `post_status`, `post_step`, `close_task` | Same four | Yes |
| Verdict to status | `NO_VISIBLE_CHANGE` counts as done | `NO_VISIBLE_CHANGE` counts as blocked; stale or timeout under 3 drops goes back to pending | Differs |

### 2.2 Which pairings interoperate

| Pairing | Works unchanged? | What it takes |
| :--- | :--- | :--- |
| H1 broker + H2 agents | Yes (designed pair) | One live GUI turn on the repaired code |
| **F1 broker + F4 agents** | Designed pair, **never run together** | Whole-stack swap: new runtime root, new vision job, new env names, new claims file. Also note F1 does not yet call F4's script guard |
| H2 agents on F1 broker | **No** | Blocked by missing `rec`, missing `latest.json` and epochs, no `claim` records, exact-match claims, underscore ids, hard-coded paths |
| F4 agents on H1 broker | **No** | Blocked by the root guard, missing `lease_epoch`, `script_sha256` and `footprint`, claims outside H prefixes |
| H bridge with F broker (or reverse) | **No** | Job filename and receipt format both differ |
| **F2 overlay on H event log** (standalone) | **Yes** | Point it at `swarm/run/events.jsonl` and pass the start time as a number (it cannot read H's JSON sidecar). It ignores the log's colours and labels and badges every PASS close |
| H3 overlay on F event log | Yes | Pass the start time explicitly; falls back to its own palette |
| **F3 projection on H event log** (standalone) | **Yes, with four flags** | Events path, output directory, `osb` room `viewpoint-capsule`, TaskLight room `viewpoint`. Loses handoff and park, acks every PASS turn (32 acks against H's 11 on the smoke log [MEASURED]), and stalls a card after 12 events per agent |
| H4 and F3 both live on `viewpoint` | **Never** | Both write the same per-agent cards with different step meanings; the cards corrupt and H's reconciler misreads F's rows |

Summary: brokers and agents are bound to their own side, so a failure there is a whole-pair swap. Overlay and projection only read the event log, whose envelope is shared, so F2 and F3 are usable as standalone replacements.

## 3. Prose design lessons for Gemini (no code)

### H1 broker

1. **Make the event log the source of truth across restarts.** Today a restart begins again from tick zero with an empty duplicate set and a fresh nonce. On start, the broker should rebuild its queue, counters, duplicate set and epochs by reading its own log, so a restart continues rather than replays.
2. **Write ahead of every act.** Record the intention to dispatch before the job becomes visible to the bridge. After a crash, a job still in its staging name proves it never ran; a recorded act without a close is resolved as uncertain and is never dispatched a second time.
3. **Make appends durable and heal a torn tail.** Sync each record and the directory after renames. On start, cut a half-written last line into a quarantine file. A corrupt record in the middle of the log should force reconcile mode, not be skipped.
4. **Authenticate receipts.** A nonce stored in a file inside the agent-readable root lets any agent forge a receipt. Keep the secret in the broker and bridge environment only, strip it from agent processes, and bind each receipt to the event id, status and script hash with a keyed digest.
5. **Retract on timeout.** When a turn times out, move the job out of the inbox. If the move succeeds, the job provably never ran and the turn can close cleanly. If the job is already gone, close as outcome-uncertain. Route receipts that arrive after the deadline to a separate late area and never let them satisfy a later turn.
6. **Bound every read.** Cap intent and receipt sizes, refuse symlinks, and treat a malformed, oversized or deeply nested intent as a poison pill that is rejected and logged once.
7. **Turn on pixel-identical viewport rebase by default.** In the 12:03 run the camera agent got six stale-frame closes and was parked twice. When the frame the intent was based on is pixel-identical to the head frame in the intent's footprint, the intent is still valid and should proceed.
8. **Match the contract's lease lifetime.** The code default is twelve times the contract's stated value [MEASURED: 3,600,000 ms vs 300,000 ms].
9. **Add a sequence number to every record.** It costs nothing and gives the overlay, projector and reconciler a stable identity that survives log rewrites.
10. **Open gap on both sides:** neither broker detects a hung Blender or a dead agent process. A simple heartbeat from the bridge timer, checked before each dispatch, would close it.

### H2 agents

1. **Validate script content before it is enqueued.** Presence and claim prefixes are the only checks today. Adopt an allow-list approach: permitted imports, permitted operator categories, no reflection or aliasing tricks, and size caps. Run the check at the broker's enqueue path too, since agents are not trusted.
2. **Fix the camera prompt.** It asks the camera agent to save the main file from inside a job. That lets an agent overwrite shared state, and under a checkpoint scheme it would overwrite the checkpoint itself. Saving must be a broker-only action.
3. **Give every job a wall-clock budget.** Instrument scripts so they stop themselves with a recognisable marker after a fixed time, so one long loop cannot hold the single Blender instance.
4. **Give the runner its own deadline.** It currently waits on each agent with no limit and relies on the model CLI's own timeout. Add a hard deadline, kill by process group, and confirm the process identity before killing so a recycled pid is never hit.
5. **Cap intents per session and fail loudly.** A session that produces an invalid intent should end with a non-zero exit so the operator sees it.
6. **Keep a model-free rehearsal path.** A stub agent and a frame generator let the whole pipeline be rehearsed without model calls or a broker. H's scripted producer covers part of this; extend it to all four roles.
7. **Create shared structure before the first agent turn.** Collections that agents will fill should be created by a broker-owned setup job. Otherwise each agent's first job changes the scene root, which no agent can claim.
8. **Keep what H already does better:** the wait-for-verdict loop, the never-repeat rules for uncertain outcomes, and identity binding. The reference agents write a batch and exit without reacting to verdicts.

### H3 overlay

1. **Write the output atomically.** Encode to a temporary name next to the target and rename on success. Refuse an output path that resolves to the input. A failed encode must leave the previous file intact.
2. **Guard every value read from the log.** Coordinates, turn, tick and drop counts are converted without checks; a single non-numeric or non-finite value aborts the render. Skip such records and count them in the summary.
3. **Drop records outside the recording window.** The log is append-only across runs, so earlier runs currently colour the state at frame zero. Count what was skipped.
4. **Use half-open time windows** for anything with a start and an end, so a badge never appears on two adjacent intervals at the boundary frame. Test it with a negative control.
5. **Use one colour table.** The broker emits its own colours, the cursor follows them, and the strip uses a different table, so the same agent shows two colours. Pick the broker's colours everywhere or the overlay's everywhere.
6. **Add a manual time offset.** Recorder start-up lag is unmeasured; a calibration offset lets the operator fix a visible lead or lag without re-recording.
7. **Check glyph coverage before drawing labels**, and replace unsupported characters, so the take does not show empty boxes.
8. **Make cursor evaluation incremental.** Replaying all earlier cursor records for every frame grows with frames times records; keep the last state per agent and advance it.
9. **Recording choices to settle:** H draws the system mouse and records at 15 fps; the reference hides the system mouse (so only attributed cursors are visible) and records at 30 fps. Hiding the system mouse avoids a fifth, unattributed pointer on screen. Also give take filenames second-level resolution so two takes in one minute do not collide.
10. **Camera framing for the take:** dismiss the Blender splash before frame zero (on the reference side it made a correct edit look like no change), and park script-lane cursors in a fixed side column so they do not cover the model while GUI-lane cursors move over the viewport.

### H4 projection

1. **Give every outward call an idempotency key** derived from the log line and its ordinal, and keep a ledger of acknowledged keys. Save position after each record, not each batch, so a crash replays at most one record and the ledger suppresses the duplicates.
2. **Verify before retrying an `osb` post.** On failure, read the room back and check whether the post landed before sending again, with a small bounded number of attempts and backoff. Never advance past a failed post silently.
3. **Put a timeout on every MCP read.** A hung TaskLight server blocks the projector forever today. On timeout or transport error, restart the server and retry once, but only for calls that are safe to repeat (plan, step, close), never for free-text status.
4. **Decouple the two sinks.** A TaskLight failure must not stop or reorder room posts, and the reverse.
5. **Detect log truncation and replacement.** Remember a hash of the first line and the last size; if the file shrinks or its first line changes, stop with a distinct exit code instead of reading nothing.
6. **Handle SIGTERM** the same way as an interrupt, finalising state before exit.
7. **Persist state by default.** With no state file flag, nothing survives a restart.
8. **Validate ids from the log** against a strict pattern before using them as references, and never project script bodies or key sequences into rooms.
9. **Make card steps informative.** One step per real event, named after it, is far more useful on the wall than twelve generic batch steps. Keep H's rollover so a thirteenth event does not stall the card.
10. **Do not close cards that still have open work.** Finalising every planned agent unconditionally makes the missing-close check unable to fire. Also remove or fix the refusal-reference check, which can never fail because the reference is always synthesised first.
11. **Extend the reconciler to the `osb` side:** every enqueue should have exactly one claim post, with duplicates and gaps reported. Keep H's audit against the real room store, which the reference lacks.

### H5 viewer

No H5 brief, contract section or code exists yet, so this is forward guidance drawn from the seams above.

1. **Read only.** The viewer should consume the event log and the frames directory and write nothing into the runtime root.
2. **Tolerate both envelopes.** Treat `seq`, `color`, `label` and `claim` records as optional so it works on either stack's log.
3. **Take the head frame from the frame records, not from directory listing order,** and show the frame's epoch so a stale view is visible as stale.
4. **Hold a partial last line** until it is complete, and detect truncation the same way as the projector.
5. **Show only committed state** once rewind exists: display up to the last confirmed point and mark anything inside an open transaction as provisional.
6. **Reuse the overlay's agent colour table** so the viewer, the composed video and the task wall agree.

### Rewind (if Track H adopts it after the gates)

Essential invariants, in order of importance:

1. **Checkpoint after verification, not before the act.** Each accepted action produces one saved point; the next action's base is that point. Rejected actions cost a restore, not a save.
2. **Only the broker may checkpoint, restore or rewind.** Agents cannot issue these, and control answers travel over the authenticated channel.
3. **A restore counts only if the scene digest afterwards equals the point's digest.** If it does not, or the restore times out, freeze the swarm and wait for the director; never guess.
4. **Every restore bumps the scene epoch,** which invalidates queued intents based on older frames.
5. **Log before acting, in both directions:** transaction begin before dispatch, and the rewind record before the rollback record. On restart, undo any open transaction before dispatching anything.
6. **The digest must survive a save and reload unchanged.** Three things broke this on the reference side: unused data that a saved file drops, more than one scene sharing a key, and properties Blender injects on first save. Hash only what a round trip preserves.
7. **Digest blind spots are correctness bugs.** A change the digest cannot see can never commit and looks identical to a failed restore. Cover lights, cameras, mesh topology, visibility and custom properties from the start.
8. **Take a genesis point after shared setup and before any agent turn.**
9. **Bound the cost:** a small cap on attempts per event, a fixed-size ring of points with the last good point and the open base pinned, and a restore deadline.
10. **Prove each guard with a negative control:** a deliberately broken variant of each check must turn the test suite red.

Optional refinements: alternative scripts per intent, read-only expectation probes, a visual witness check, cost tie-breaking, digest-based deduplication of points.

Known unresolved issue to avoid inheriting: on the reference side a GUI numpad view change cannot commit under the visual witness check, because the whole viewport changes and spills outside the declared footprint. View-only GUI acts need their own acceptance rule.
