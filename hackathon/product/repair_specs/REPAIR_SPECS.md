# P5 Prose Repair Specifications for Track H (`H1`..`H4`)

- **Author**: `claude-fable-5-2` (Track P, unit P5, prose only)
- **Date**: 2026-11-14
- **Inputs**: `swarm/CONTRACT.md` (sha256 prefix `b392f7dd`, unchanged since all reviews), `product/reviews/H1.md`, `H1_r1.md`, `H2.md`, `H3.md`, `H4.md`, `H4_r1.md`, and a read-only inspection of the Track H working tree on branch `swarm-1114` at HEAD `df0946d` (Track H files are untracked there).
- **No-code rule**: this document contains prose only. It has no source code, no pseudocode and no diff hunks. Every specification can be pasted into a Gemini repair brief as written.

## 0. How to read this document

### 0.1 Tree snapshot the statuses refer to

Statuses describe the tree after the `H2_r1`, `H3_r1` and `H4_r2` repair runs. The `H3_r1_1249` and `H4_r2_1249` processes had exited and the file hashes were stable across two reads before line numbers were taken.

| File | sha256 prefix [MEASURED] | Last reviewed by P4? |
|---|---|---|
| `swarm/broker/broker.py` | `b58c7411` | Yes, identical to the `H1_r1` review hash |
| `swarm/broker/bridge.py` | `5c1d67c6` | Yes, identical to the `H1_r1` review hash |
| `swarm/agents/intent_cli.py` | `20eb195d` | No, changed since the `H2` review |
| `swarm/agents/runner.py` | `1bfee643` | No |
| `swarm/agents/prompts.py` | `7630dad0` | No |
| `swarm/agents/scripted_smoke.py` | `c16cf31b` | No |
| `swarm/overlay/compositor.py` | `cc6c24dc` | No, changed since the `H3` review |
| `swarm/tools/record.sh` | `0e0b6c83` | No |
| `swarm/tools/compose.sh` | `1364b6e5` | No |
| `swarm/projection/projector.py` | `bbe0c5fd` | No, changed since the `H4_r1` review |
| `swarm/projection/reconciler.py` | `6972908c` | No |

### 0.2 How each status was established

- **`H1`**: the tree is byte-identical to the one the `H1_r1` review approved, so `H1` statuses and line numbers are the reviewer's probe-verified results.
- **`H2`, `H3`, `H4`**: the tree is newer than the last review. Statuses for these units come from **reading the current source only**. No test suite and no probe was run for this document. A `FIXED` in those rows means "the repair is present in the code as read"; it still needs a P4 re-review with probes before it counts as verified.

### 0.3 Vocabulary

- **Severity**: `BLOCKER` (the review rejected on it), `LIVE_PATH` (can change what happens during the live rehearsal or the published demo), `BACKLOG` (does not affect the scripted rehearsal).
- **Status**: `FIXED`, `PARTIAL`, `OPEN`.
- **Rehearsal Risk**: `NONE`, `LOW`, `MED`, `HIGH`, with the trigger.
- "Residual" in a `FIXED` row points to the follow-up specification that covers what the fix left behind.

---

## 1. Master Findings Ledger

### 1.1 `H1` (`swarm/broker/`)

| Finding | Unit | `file:line` (current tree) | Severity | Status | Rehearsal Risk |
|---|---|---|---|---|---|
| B1 `event_id` path traversal | H1 | `swarm/broker/broker.py:53`, `238-253`, `657-668`, `916-929` | BLOCKER | FIXED (residual R2) | NONE |
| B2 poison-pill intents crash and re-crash the broker | H1 | `swarm/broker/broker.py:602-611`, `629-647`, `1000-1014` | BLOCKER | FIXED (residual R1) | NONE |
| M1 agent-set `rebased` / `_drops` | H1 | `swarm/broker/broker.py:698-704`, `1171-1177` | LIVE_PATH | FIXED | NONE |
| M2 script timeout leaves actuator unknown | H1 | `swarm/broker/broker.py:967-998`, `1486-1495` | LIVE_PATH | FIXED (residual R3) | LOW |
| M3 GUI timeout mid-keys skips `RECONCILE` | H1 | `swarm/broker/broker.py:1269-1307` | LIVE_PATH | FIXED (residuals R4, R6) | LOW |
| M4 one rc 3 hides other keys' view change | H1 | `swarm/broker/broker.py:1253-1265`, `1326` | LIVE_PATH | FIXED | NONE |
| M5 leases never renewed | H1 | `swarm/broker/broker.py:830-841` | LIVE_PATH | FIXED (residual R5) | NONE |
| M6 README dry-run drove the live capsule; no single-writer lock | H1 | `swarm/broker/broker.py:323-330`, `276-277`; `swarm/broker/README.md:20-29` | LIVE_PATH | FIXED | NONE |
| M7 CONTRACT §3.3 watchdog and checkpoint restore | H1 | `swarm/broker/broker.py:793-797` (`checkpoint` only clears the busy flag) | BACKLOG | OPEN | MED: a hung Blender main thread needs manual recovery |
| M8 failed `vision look` crashes mid-turn | H1 | `swarm/broker/broker.py:1043-1060`, `1329-1346` | LIVE_PATH | FIXED (residual R4) | NONE |
| R1 lone UTF-16 surrogate crashes the broker once | H1 | `swarm/broker/broker.py:397-401`, `892`, `932` | LIVE_PATH | OPEN | LOW with SDK-driven ASCII agents; HIGH for any unattended or adversarial run |
| R2 `event_id` check accepts a trailing newline | H1 + H2 | `swarm/broker/broker.py:53`, `243`; `swarm/agents/intent_cli.py:37`, `260` | BACKLOG | OPEN | LOW: one such id stalls the queue for the full script cap |
| R3 late timed-out job runs unobserved after `reconcile` | H1 | `swarm/broker/broker.py:968`, `780-797`, `766-779` | LIVE_PATH | OPEN | MED: reached by any script timeout during the rehearsal |
| R4 epochs understate real change after uncertain outcomes | H1 | `swarm/broker/broker.py:1272-1293`, `1046`, `1369` | BACKLOG | OPEN | LOW: queue is in `RECONCILE` while the epoch is wrong |
| R5 lease TTL effectively unenforced | H1 | `swarm/broker/broker.py:851-853` | BACKLOG | OPEN | NONE |
| R6 GUI timeout verdict diverges from §3.1 | H1 + H0 | `swarm/broker/broker.py:1288`; `swarm/CONTRACT.md:110`, `264` | BACKLOG | OPEN (needs H0 ruling) | LOW: an agent may re-enqueue half-delivered keys |
| R7 `--auto-rebase-viewport` off by default | H1 | `swarm/broker/broker.py:290`, `1173-1179`; `swarm/broker/README.md:18` | LIVE_PATH | OPEN | HIGH: camera GUI intents reach `drops == 3` while builders are active |
| R8 carried validation gaps (`script_sha256` null, `v`, `action_class`, `max_acts`, restart idempotency, frame files never deleted) | H1 | `swarm/broker/broker.py:890-891`, `357`, `509-511` | BACKLOG | OPEN | LOW: `/dev/shm` growth on a long run; duplicates after a broker restart |
| R9 GUI stdout parser catches only JSON decode errors | H1 | `swarm/broker/broker.py:1255-1263` | BACKLOG | OPEN | NONE today (not reachable with current `vision` output) |
| R10 bridge returns a non-hex nonce-file value verbatim | H1 | `swarm/broker/bridge.py:75` | BACKLOG | OPEN | NONE |

### 1.2 `H2` (`swarm/agents/`)

| Finding | Unit | `file:line` (current tree) | Severity | Status | Rehearsal Risk |
|---|---|---|---|---|---|
| B1 `wait-event` false success from an old receipt | H2 | `swarm/agents/intent_cli.py:413-471`, `614-684`; `swarm/agents/prompts.py:25`; test at `swarm/agents/test_agents.py:412` | BLOCKER | FIXED (residual H2-S1) | LOW |
| M1 6G/8G memory scope did not reach `agy` | H2 | `swarm/agents/runner.py:26`, `50-64`, `70-75`; test at `swarm/agents/test_agents.py:590` | LIVE_PATH | PARTIAL: real binary now runs inside the scope, but nothing reads the cgroup to prove it | MED until one live assertion is recorded |
| M2 `write_set` understates script writes | H2 + H0 | `swarm/agents/scripted_smoke.py:121-127`, `510`, `534`; `swarm/agents/prompts.py:30-33` | LIVE_PATH | PARTIAL: `Cube` hide and `look-agent` render writes removed; material datablock creation by non-owners is still undeclared and has no H0 ruling | LOW |
| M3 `STALE_FRAME` re-enqueue fought the tail-requeue | H2 | `swarm/agents/intent_cli.py:671-675`; `swarm/agents/scripted_smoke.py:517-533`, `555-617`; `swarm/agents/prompts.py:38` | LIVE_PATH | FIXED (depends on H1 R7) | MED while R7 is open |
| M4 `wait-event` timeout too short and indistinguishable | H2 | `swarm/agents/__init__.py:51`; `swarm/agents/intent_cli.py:898-900`; `swarm/agents/prompts.py:40` | LIVE_PATH | FIXED | NONE |
| M5 TaskLight posted twice | H2 | `swarm/agents/prompts.py:48-49`; test at `swarm/agents/test_agents.py:94` | LIVE_PATH | FIXED | NONE |
| M6 `--kill-all` could signal recycled PIDs | H2 | `swarm/agents/runner.py:116-135`, `165`, `175-296`, `412-413` | LIVE_PATH | FIXED | NONE |

### 1.3 `H3` (`swarm/overlay/`, `swarm/tools/`)

| Finding | Unit | `file:line` (current tree) | Severity | Status | Rehearsal Risk |
|---|---|---|---|---|---|
| B1 `:1` gate bypassed by leading zeros | H3 | `swarm/tools/record.sh:53-66`, `68-72` | BLOCKER | FIXED | NONE |
| M1 timeline heuristic overrode the sidecar | H3 | `swarm/overlay/compositor.py:434-438`, `446-450` | LIVE_PATH | FIXED (residuals H3-E1, H3-E3) | LOW |
| M2 event-level park drawn as agent park | H3 | `swarm/overlay/compositor.py:641-661` | LIVE_PATH | FIXED | NONE |
| M3 encoder deadlock on undrained stderr | H3 | `swarm/overlay/compositor.py:1061-1080`, `1147-1148`, `1183-1192` | LIVE_PATH | FIXED | NONE |
| m1 `DUPLICATE_EVENT` corrupted queue counts and verdicts | H3 | `swarm/overlay/compositor.py:73`, `613-617` | BACKLOG | FIXED | NONE |
| m2 torn trailing line crashed non-strict loading | H3 | `swarm/overlay/compositor.py:306-311`, `334-341` | BACKLOG | FIXED (residual H3-E2) | NONE |
| m3 `compose.sh` did not assert `yuv420p` or faststart | H3 | `swarm/tools/compose.sh:51-54`, `61-95` | BACKLOG | FIXED | NONE |
| m4 recorder naming and time anchor | H3 | `swarm/tools/record.sh:82`, `94-95` | BACKLOG | PARTIAL: names now have second resolution; silent overwrite and the early anchor remain | LOW: overlay timing offset equals recorder start-up latency |

### 1.4 `H4` (`swarm/projection/`)

| Finding | Unit | `file:line` (current tree) | Severity | Status | Rehearsal Risk |
|---|---|---|---|---|---|
| M1 `--finalize` hid a failed `close_task` (self-state mask) | H4 | `swarm/projection/projector.py:1112-1128`; `swarm/projection/reconciler.py:246-257` | LIVE_PATH (rejection ground) | FIXED | NONE |
| M2 `stop` closed every TaskLight task | H4 | `swarm/projection/projector.py:1060-1076` | LIVE_PATH (rejection ground) | FIXED | NONE |
| R1 stale `close` in the persistent room masked a failed `close_task` | H4 | `swarm/projection/projector.py:1217-1223`; `swarm/projection/reconciler.py:238-243`; tests at `swarm/projection/test_projection.py:1050`, `1119`, `1156` | LIVE_PATH (rejection ground) | FIXED (residual H4-E2) | LOW |
| r1-2 DEL passed the sanitizer | H4 | `swarm/projection/projector.py:83` | BACKLOG | FIXED | NONE |
| r1-3 lone surrogate in `reason` ended the run | H4 | `swarm/projection/projector.py:63-95`, `1164-1171` | BACKLOG | FIXED (residual H4-E1) | NONE |
| r1-4 multibyte split crashed the reader | H4 | `swarm/projection/projector.py:1144-1158` | BACKLOG | FIXED | NONE |
| r1-5 JSON-RPC error aborted the finalize loop | H4 | `swarm/projection/projector.py:1112-1122` | BACKLOG | FIXED | NONE |
| r1-6 `MISSING_REF_ON_REFUSAL` cannot fire | H4 | `swarm/projection/reconciler.py:153-162`, `174-181`; `swarm/projection/projector.py:100-112`; `swarm/projection/README.md:128` | BACKLOG | OPEN | NONE (a documented check that never runs) |
| r1-7 carried items (m7 classification undocumented, SIGTERM skips save and finalize, `--once` no-op, MCP stderr undrained and read without timeout, room auto-open unverified) | H4 | `swarm/projection/projector.py:40-54`, `1184-1201`, `1269-1272`, `569`, `604-611`; `swarm/projection/README.md:61` | BACKLOG | OPEN | LOW: stopping the follower with SIGTERM loses `--finalize` |

### 1.5 Counts

| Unit | Rows | FIXED | PARTIAL | OPEN |
|---|---|---|---|---|
| H1 | 20 | 9 | 0 | 11 |
| H2 | 7 | 5 | 2 | 0 |
| H3 | 8 | 7 | 1 | 0 |
| H4 | 9 | 7 | 0 | 2 |

### 1.6 Suggested repair order for the rehearsal

| Order | Item | Why first |
|---|---|---|
| 1 | H1 R7 (launch flag and README) | Highest rehearsal risk; it is a configuration and documentation change |
| 2 | H1 R3 (reconcile checks the late job) plus the director runbook | Reached by any script timeout |
| 3 | H1 R1 (surrogates) | Only remaining way for one intent to crash the broker |
| 4 | H2 M1 (cgroup assertion) | One measured proof closes the finding |
| 5 | H4-E2 and H4-E1 | Both can make the final audit report clean when it is not |
| 6 | Everything else | Backlog |

---

## 2. Prose Repair Specifications: `H1`

### SPEC-H1-R7: launch the rehearsal with viewport auto-rebase

- **Target**: `swarm/broker/broker.py`, the stale-frame branch of `_handle_gui_intent` and the `auto_rebase_viewport` configuration field; `swarm/broker/README.md`, the live launch command.
- **Invariant**: a GUI intent whose keys are all view-only navigation keys from the contract allowlist must not be parked merely because other agents' script turns produced newer frames between its enqueue and its turn.
- **Rules on valid input**: when auto-rebase is enabled and the intent's `based_on_frame` is older than the head frame but still inside the frame ring, the broker admits the intent, records `rebased` as true in the `close` record, and leaves the per-event stale drop counter untouched. The README live command must show the flag, with one sentence saying it is the recommended rehearsal setting and why.
- **Rules on boundary input**: `based_on_frame` equal to the head frame is never rebased and `rebased` stays false. A `based_on_frame` that has left the ring is still refused with `FRAME_EVICTED`, whether or not auto-rebase is on. A `based_on_frame` greater than the head frame stays an invalid-frame refusal.
- **Rules on malformed input**: an agent-supplied `rebased` field stays stripped at ingest (the M1 fix); the flag is broker configuration only. With the flag off, behaviour is exactly today's: `STALE_FRAME`, drops 1, 2, 3, then an event park.
- **Seams**: `H1<->H2`: `wait-event` returns rc 3 only at `drops == 3`, so with auto-rebase on the camera agent sees a normal success code and never needs a fresh id. `H1<->H3`: the overlay shows fewer event parks. `H1<->H4`: fewer `refute` posts and fewer step slots consumed by refusals.
- **Must-pass test**: start a mock-vision broker with auto-rebase on. Run one script intent to `STATE_VERIFIED` so the head frame advances, then submit a camera GUI intent that names the earlier frame. Assert the verdict is `VERIFIED` or `VERIFIED_WITH_SPILL`, `rebased` is true, `drops` is zero, `view_epoch` rose by one, and no `park` control record exists. **Negative control**: repeat with the flag off and assert three `STALE_FRAME` closes with drops 1, 2, 3 followed by one event-level `park` record. Second negative control: with the flag on, an evicted frame number still yields `FRAME_EVICTED`.

### SPEC-H1-R3: a reconcile must account for the timed-out job

- **Target**: `swarm/broker/broker.py`, `apply_control_events` (the `reconcile`, `checkpoint` and `resume` branches) and the script-timeout branch of `_handle_script_intent`; the director runbook section of `swarm/broker/README.md`.
- **Invariant**: the broker must not clear `actuator_busy` while a job it dispatched can still execute unobserved. Every scene mutation by a dispatched job is followed by a frame capture and is reflected in `scene_epoch` before the next job is dispatched.
- **Rules on valid input**: on script timeout the broker remembers the timed-out job's identity (nonce, sequence, event id). When a `reconcile` or `checkpoint` control arrives, it looks for that job in three places and acts on the first that applies:
  1. A receipt exists in the outbox: consume it, increment `scene_epoch` if its status is OK, capture a frame, clear the remembered job, clear busy.
  2. The job file is still in the inbox and no receipt exists: the bridge has not picked it up. Move the file out of the inbox into the broker's own archive so it can never run, then clear busy.
  3. Neither a job file nor a receipt exists: the bridge is executing it now. Keep busy set, keep the queue in `RECONCILE`, and emit a control record whose reason says the reconcile is deferred because the job is still running.
- **Rules on boundary input**: a second timeout while one job is already remembered is impossible by the `ACTUATOR_BUSY` guard; keep that guard. `reconcile` while `FROZEN` keeps the current rule (no exit from `FROZEN`) but still performs the three checks. `resume` never clears busy. If busy is set when `resume` is applied, the control record's reason must say that a `reconcile` is still required, so the director sees why intents keep returning `ACTUATOR_BUSY`.
- **Rules on malformed input**: a late receipt that is corrupt or not a JSON object counts as "the job ran with unknown result": increment `scene_epoch`, capture, clear busy, and note the reason in the control record. An inbox file removed by the race between the check and the move is treated as case 3.
- **Runbook text to add**: after any `TURN_TIMEOUT`, the director issues `reconcile`, not `resume`. If the reconcile record says deferred, wait and issue `reconcile` again.
- **Seams**: `H1<->H2`: agents keep receiving `ACTUATOR_BUSY` until reconcile completes; the brief's rule for that code (observe, do not flood) stays valid. `H1<->H3`: the overlay keeps showing the control state until the successful reconcile. `H1<->H4`: the reconciler clears uncertain events on any `reconcile` record; a deferred reconcile must therefore be distinguishable, so H4 should clear only on a reconcile that is not marked deferred. State the marker wording in both briefs.
- **Must-pass test**: with a bridge stub that does nothing, dispatch a script, let it time out, then drop an OK receipt for that job into the outbox and send `reconcile`. Assert `scene_epoch` rose by one, a new frame exists, busy is false, the queue is `IDLE`. Second case: no receipt, job file still in the inbox, send `reconcile`; assert the file is gone from the inbox and present in the archive, busy false. Third case: remove the job file without writing a receipt, send `reconcile`; assert busy stays true and the queue stays `RECONCILE`. **Negative control**: in the third case, enqueue a fresh intent and assert it closes with `ACTUATOR_BUSY`, not a dispatch.

### SPEC-H1-R1: no agent string may crash event emission or job writing

- **Target**: `swarm/broker/broker.py`, `_emit_event`, `atomic_write_json`, `atomic_write_text` as used at dispatch, and the validation inside the `try` of `ingest_intents`.
- **Invariant**: no string that originates in an intent file can raise an encoding error anywhere after ingest. A rejected intent is archived to `rejected/` and closed with a refusal; the broker process keeps running.
- **Rules on valid input**: well-formed Unicode, including non-ASCII text in `expect` and in script source, is accepted and round-trips unchanged.
- **Rules on boundary input**: event records are serialised so that every line is pure ASCII with non-ASCII characters escaped. That makes emission incapable of failing on any string and keeps one record per line. Supplementary-plane characters written as a valid surrogate pair in the JSON source are legal and must be accepted.
- **Rules on malformed input**: at ingest, inside the existing guarded block, every string field of the intent (including each `write_set` entry, each key name, and `script`) is checked for encodability as UTF-8. An intent with any lone surrogate is refused with `CLAIM_REFUSED`, with a reason naming the offending field but not echoing its value, and is archived to `rejected/`. The same check applies to director control files: `reason` is sanitised or the file is rejected.
- **Seams**: `H1<->H2`: `wait-event` reads the log as UTF-8 text; ASCII-only lines are a strict subset, so no change is needed, and the byte offsets H2 records stay valid. `H1<->H3` and `H1<->H4`: both parse lines as JSON and will see the same decoded strings; the H4 multibyte-split case (r1-4) becomes unreachable from broker output. Confirm that neither unit compares raw line bytes against expected non-ASCII text in tests.
- **Must-pass test**: submit one valid intent whose `kind` is a JSON-escaped lone high surrogate, then one fully valid intent. Assert the broker does not raise, the first file is in `rejected/`, a `CLAIM_REFUSED` close exists for it, and the second intent reaches `STATE_VERIFIED`. Repeat with the surrogate in `script`, in `expect`, and in a `write_set` entry. Assert every byte of the events log is below 128. **Negative control**: an intent whose `expect` contains accented letters and an emoji is accepted and its `enqueue` record decodes back to the same text.

### SPEC-H1-R2: `event_id` must match in full, in both units

- **Target**: `swarm/broker/broker.py`, `_is_valid_event_id`; `swarm/agents/intent_cli.py`, the event-id check in `validate_intent_dict` and anywhere else the shared pattern is applied.
- **Invariant**: an accepted `event_id` consists only of the allowed characters from first to last, with nothing after them. The broker, the agent CLI and the bridge accept exactly the same set of ids.
- **Rules on valid input**: ids of length 1 to 128 that start with a letter or digit and continue with letters, digits, dot, underscore or hyphen are accepted, as today.
- **Rules on boundary input**: an id with a trailing newline, a trailing carriage return, or any trailing whitespace is refused. Length 128 is accepted; length 129 is refused.
- **Rules on malformed input**: non-string ids, the empty string, and ids with an embedded newline are refused. In the broker the refusal is `CLAIM_REFUSED` with the existing "missing or invalid event_id" reason and the file goes to `rejected/`. In the CLI it is a validation error before any file is written.
- **Seams**: `H1<->H2`: this is a joint change; if only H1 lands, the CLI still writes such a file and the agent learns of the refusal one ring cycle later. `H1<->H4`: refusal `close` records for such ids carry a null event id, which H4 already maps to a synthetic ref.
- **Must-pass test**: for the broker, write an intent file by hand whose `event_id` is a valid id plus a newline. Assert `CLAIM_REFUSED`, the file is in `rejected/`, nothing was written to the inbox, and the broker returned to `IDLE` immediately without waiting the script cap. For the CLI, call the enqueue command with the same id and assert a non-zero exit and no intent file. **Negative control**: the same id without the newline is accepted by both.

### SPEC-H1-R4: epochs must not understate actuator changes

- **Target**: `swarm/broker/broker.py`, the GUI timeout branch and the post-dispatch capture-failure branches of `_handle_script_intent` and `_handle_gui_intent`; the `reconcile` branch of `apply_control_events`.
- **Invariant**: `view_epoch` and `scene_epoch` are monotonic and never lower than the number of actuator changes the broker has positive evidence for. An epoch may overstate change; it must never understate it.
- **Rules on valid input**: unchanged for clean outcomes.
- **Rules on boundary input**: on a GUI timeout after at least one key returned success, increment `view_epoch` once. On a capture failure after a script receipt with status OK, keep the `scene_epoch` increment. On a capture failure after GUI keys returned success, keep the `view_epoch` increment. In each case the verdict and the `RECONCILE` transition stay as they are. The first frame successfully published after reconcile carries the already-incremented epochs.
- **Rules on malformed input**: a GUI timeout before any key was delivered (the hover step timed out) does not increment `view_epoch`.
- **Seams**: `H1<->H2`: agents read epochs from the frame sidecar and copy them into intents; a higher epoch only makes them re-observe. `H1<->H3`: none. `H1<->H4`: none.
- **Must-pass test**: with an injected vision runner, deliver one successful key and then time out on the second. Assert `view_epoch` is one after the close and stays one after `reconcile`. Second case: make the post-script capture fail after an OK receipt; assert `scene_epoch` is one. **Negative control**: hover times out before any key; assert `view_epoch` stays zero.

### SPEC-H1-R5: lease expiry must be observable

- **Target**: `swarm/broker/broker.py`, `_validate_lease` and `_maybe_renew_lease`.
- **Invariant**: a lease whose expiry time has already passed is never renewed by the act of checking it. Renewal happens only while the lease is still valid.
- **Rules on valid input**: inside the last quarter of the TTL and before expiry, the lease is renewed and a `claim` record is logged, as today.
- **Rules on boundary input**: a check at exactly the expiry millisecond counts as still valid. One millisecond later the intent is refused with `LEASE_EXPIRED` and no renewal `claim` record is written. To keep an idle but healthy agent from expiring, the broker also runs the renewal check for every ring agent once per sweep, independent of whether that agent has an intent at the head.
- **Rules on malformed input**: an intent whose `lease_epoch` differs from the active one is refused with `LEASE_EXPIRED` regardless of time, as today.
- **Seams**: `H1<->H2`: the brief already tells agents to re-observe on `LEASE_EXPIRED`; since `observe` reads the latest `claim`, an agent can recover only if a later grant exists. State in the H1 brief which actor re-grants an expired lease (director `resume` for that agent is the natural place) and log that grant as a `claim`. `H1<->H4`: more `claim` records in the log; H4 ignores them.
- **Must-pass test**: use a short TTL and an injectable clock. Advance past expiry with no sweep in between, submit an intent, assert `LEASE_EXPIRED` and that the count of `claim` records did not change. **Negative control**: advance to the last quarter of the TTL but before expiry, submit an intent, assert it is dispatched and exactly one new `claim` record exists.

### SPEC-H1-R6: one verdict for a GUI timeout (needs H0 ruling first)

- **Target**: `swarm/CONTRACT.md` §3.1 step 4.5 against §6 (owner H0); then `swarm/broker/broker.py` GUI timeout branch and `swarm/agents/prompts.py` verdict guidance.
- **Invariant**: a GUI turn that timed out after input may have been delivered is reported with one contract-defined verdict, and every consumer treats that verdict as "never auto-repeat".
- **Decision to request from H0**: keep `TURN_TIMEOUT` for the GUI timeout (current broker behaviour, matches §6) and amend §3.1 step 4.5 to say so; or switch the broker to `INPUT_UNCERTAIN`. The recommendation is to keep `TURN_TIMEOUT`, because the consecutive-timeout park rule already depends on it, and to fix the agent guidance.
- **Rules after the ruling**: whichever verdict wins, the close reason must say whether any key was delivered before the timeout. The H2 brief must state that after a GUI `TURN_TIMEOUT` the agent re-observes and decides from the new frame, and must not re-send the same key list.
- **Seams**: `H1<->H2` as above. `H1<->H3`: the verdict pill text follows the verdict. `H1<->H4`: both candidates are already mapped to `refute`.
- **Must-pass test**: deliver one key, time out on the second; assert the chosen verdict, the `RECONCILE` state, and a reason that reports one delivered key. For H2, a prompt test asserts the brief contains the no-repeat rule for that verdict. **Negative control**: a clean two-key run yields `VERIFIED` and no timeout reason.

### SPEC-H1-R8: carried validation and housekeeping gaps

- **Target**: `swarm/broker/broker.py`, the script admission checks in `_handle_script_intent`, the ingest validation in `ingest_intents`, the seen-id set, and the frame-ring eviction in `capture_frame`.
- **Invariants**: (a) a script is dispatched only if the intent carries a SHA-256 that matches the script bytes; (b) schema-level fields have contract values before the intent is queued; (c) an event id closed in an earlier broker process is still a duplicate; (d) frame files on disk are bounded by the ring size.
- **Rules on valid input**: an intent with version 1, a contract `action_class`, `max_acts` between 1 and 8 that equals 1 for the script lane and the key count for the GUI lane, and a matching hash is accepted.
- **Rules on boundary input**: on start-up the broker rebuilds the seen-id set from the events log it is about to append to (every `enqueue` and every `close` event id). When a frame number leaves the ring, its image and sidecar files are deleted; `latest.json` and frames still in the ring are never deleted.
- **Rules on malformed input**: a null or missing `script_sha256` on the script lane, a wrong version, an unknown `action_class`, or an out-of-range `max_acts` is refused with `CLAIM_REFUSED` and a reason naming the field; the file is archived to `rejected/`.
- **Seams**: `H1<->H2`: the SDK already sends all of these; hand-written intents are the only ones affected. Rebuilding seen ids from the log makes `DUPLICATE_EVENT` (CLI rc 4) possible for an id from an earlier run, which is what the run-scoped id rule already assumes. `H1<->H3`: the compositor reads only the log, never frame files, so deletion is safe. `H1<->H4`: none.
- **Must-pass test**: submit a script intent with a null hash and assert `CLAIM_REFUSED`. Run a broker, close one event, stop it, start a new broker on the same log, resubmit the same id, assert `DUPLICATE_EVENT` and no dispatch. Capture more frames than the ring holds and assert the count of frame images on disk never exceeds the ring size. **Negative control**: a correct intent with a fresh id is dispatched after the restart.

### SPEC-H1-R9: GUI output parsing must not raise after keys were sent

- **Target**: `swarm/broker/broker.py`, the block in `_handle_gui_intent` that parses `vision act` standard output.
- **Invariant**: once at least one key has been delivered, no shape of `vision` output can raise out of the handler; an unusable output degrades to "no region information".
- **Rules**: valid object output with a receipt object is used as today. Output that is valid JSON but not an object, an object whose receipt is null or not an object, or regions of the wrong type are treated as an empty region list for that key; the exit code still decides the verdict. Non-JSON output is already handled.
- **Seams**: `H1<->H3`: `own_regions` may be empty in a `close`; the overlay must already tolerate that. Others: none.
- **Must-pass test**: an injected runner returns exit code 0 with a JSON list as output for one key, then exit code 0 with a null receipt for the next. Assert a `VERIFIED` close with empty regions and no exception. **Negative control**: exit code 1 with the same odd output still yields `INPUT_UNCERTAIN` and `RECONCILE`.

### SPEC-H1-R10: bridge nonce discovery returns hex or nothing

- **Target**: `swarm/broker/bridge.py`, the nonce discovery function that reads the environment variable and then the nonce file.
- **Invariant**: the function returns either a lowercase hex nonce or the empty string. With an empty nonce the bridge executes nothing.
- **Rules**: a hex value in the file is returned lowercased. A file with trailing whitespace is trimmed first. A non-hex value, an empty file, a missing file or a read error all return the empty string, and the bridge logs once that no valid nonce was found.
- **Seams**: `H1<->H2`, `H1<->H3`, `H1<->H4`: none; this is internal to the bridge.
- **Must-pass test**: write a file path string into the nonce file, call discovery, assert the empty string; then place a correctly named job for some hex nonce in the inbox and assert the bridge did not execute it. **Negative control**: a valid hex nonce in the file is returned and a matching job runs.

### SPEC-H1-M7: watchdog and crash recovery (CONTRACT §3.3)

- **Target**: `swarm/broker/broker.py`, the main sweep in `run` and `step_sweep`, the `checkpoint` branch of `apply_control_events`; `swarm/broker/bridge.py` only if a liveness signal is added.
- **Invariant**: a dead agent's queued events never reach dispatch, and a hung Blender main thread is detected by the broker and surfaced as a frozen queue with a control record, without the broker killing anything it did not record.
- **Missing inputs (AUSENTE, do not invent)**: the contract does not say where an agent's PID is recorded, where the Blender PID is recorded, or where checkpoint files live. Ask H0 for those three facts before implementing the kill-and-restore part.
- **Minimum behaviour that needs no new contract facts**:
  - Liveness of the bridge is inferred from receipts: when a script timeout occurs and, at the following reconcile, the job is in the "being executed" state of SPEC-H1-R3 for longer than a configurable limit, the broker emits a `stop` control record by `broker` with a reason naming a suspected hung main thread, and enters `FROZEN`.
  - If the runner supplies agent PIDs (H2 already writes them to its proof directory with start times), the broker may be given that directory by an option. Before each dispatch it checks that the head agent's recorded process still exists with the recorded start time; if not, it drops that agent's queued events with a refusal close whose reason says the agent is dead, and parks the agent.
- **Rules on boundary and malformed input**: missing or unreadable PID records mean "liveness unknown": dispatch proceeds as today. A PID that exists with a different start time counts as dead. The broker never signals any process in this minimum version.
- **Seams**: `H1<->H2`: the runner's process records become an input to the broker; the two units must agree on the directory and field names already used by the runner. `H1<->H3` and `H1<->H4`: the new `stop` record by `broker` is an existing record type.
- **Must-pass test**: give the broker a process record for an agent whose PID does not exist, queue two intents for it, run one sweep, assert both close with a refusal naming a dead agent, no inbox file was written, and a `park` record names the agent. **Negative control**: with a live PID and matching start time, the intents are dispatched normally.

---

## 3. Prose Repair Specifications: `H2`

### SPEC-H2-M1: prove the memory cap on the running agent

- **Target**: `swarm/agents/runner.py`, `run_agents` (the step right after each process is started) and the dry-run output; `swarm/agents/README.md` line 14.
- **Invariant**: each launched agent process is a member of the scope the runner created for it, and that scope's memory maximum is 8 GiB [CITED from the H2 review requirement]. The runner records the evidence.
- **Rules on valid input**: after starting an agent, the runner reads the process's cgroup membership and the memory maximum of that cgroup, and writes both into the per-agent process record next to the PID and start time. The run summary lists, per role, the scope name and the measured maximum.
- **Rules on boundary input**: if the process has already exited when the read happens, record "exited before verification" and do not fail the run. A measured maximum lower than 8 GiB is accepted and recorded.
- **Rules on malformed input**: if the cgroup path does not contain the expected unit name, or the maximum is larger than 8 GiB or unlimited, the runner stops that agent through its unit, marks the role as failed with a reason, and exits non-zero after the other roles are handled.
- **Seams**: `H1<->H2`: none. Host seam: the check guards against a future change that routes the launch back through the 12G/16G shim.
- **Must-pass test**: the unit test supplies a fake process-information reader that reports a cgroup with the expected unit name and an 8 GiB maximum; assert the record contains both values and the role is marked verified. One live assertion is also required for the proof log: launch one role with a trivial brief and record the measured value with a [MEASURED] tag. **Negative control**: the fake reader reports a 16 GiB maximum in a different slice; assert the role is marked failed and the exit code is non-zero.

### SPEC-H2-M2: every mutated resource is declared, and material ownership is ruled

- **Target**: `swarm/agents/scripted_smoke.py`, the shared `bind_material` helper and the plan entries in `build_smoke_plan`; `swarm/agents/prompts.py`, shared prefix rule 6 (lines 30-33); `swarm/CONTRACT.md` §4.1 (owner H0).
- **Remaining defect**: the shared helper lets `modeler-agent` and `props-agent` create a material datablock when the named one does not exist yet. Creating a material is a write in the `mat:` namespace, which belongs to `look-agent`, and it is not in those agents' `write_set`. The brief currently teaches this as allowed. The contract has no rule for it.
- **Decision to request from H0**: either (a) add a contract sentence that referencing an existing material by name on one's own mesh is not a `mat:` write, while creating or editing a material is; or (b) move all slot assignment to `look-agent` and allow it by contract. Option (a) is recommended because it keeps writes disjoint.
- **Invariant under option (a)**: only `look-agent` creates, deletes or edits material datablocks. Builders may only link a material that already exists to their own meshes, and that link is part of the mesh resource they already declare.
- **Rules on valid input**: a builder script that finds the named material links it. A builder script that does not find it leaves the slot empty and does not fail. `look-agent`'s material script creates all named materials; a later builder or look turn completes the linking.
- **Rules on boundary input**: plan order must make this converge: the smoke plan either runs the look material turn before the builder turns that bind, or includes a final idempotent bind pass per builder after the look turn. Re-running any script leaves exactly one material per name.
- **Rules on malformed input**: a `write_set` entry outside the agent's prefixes remains a broker `CLAIM_REFUSED`; the smoke must treat that as a hard failure, not retry.
- **Seams**: `H1<->H2`: the broker checks declared prefixes only (contract §8 item 1), so this invariant is enforced by H2's own tests, not by H1. `H2<->H0`: the ruling must be written into the contract before the brief cites it.
- **Must-pass test**: a static test walks every script in the smoke plan and asserts that scripts owned by `modeler-agent` and `props-agent` contain no material creation or removal call and no edit of material node settings, and that scripts owned by `look-agent` contain no write to render or Eevee settings and no mesh creation. It also asserts every plan entry's `write_set` is non-empty and prefix-valid. **Negative control**: feed the checker a builder script that creates a material and assert the checker fails.

### SPEC-H2-S1 (seam hazard): `wait-event` with no recorded offset

- **Target**: `swarm/agents/intent_cli.py`, `wait_for_close_event` and `load_enqueue_offset`.
- **Hazard**: when no offset record exists for the event id (the intent was written by another process, the offsets directory was cleaned, or a different intents directory was passed), the tail silently starts at byte zero. That is the original B1 behaviour: an old `VERIFIED` close for the same id returns rc 0.
- **Invariant**: `wait-event` never reports a close that was written before the matching enqueue of the current run.
- **Rules on valid input**: with an offset record, behaviour is unchanged.
- **Rules on boundary input**: without an offset record and without an explicit offset option, the command first locates the last `enqueue` record for that id in the log and accepts only a `close` after it. If no `enqueue` record exists yet, it waits for one until the timeout. A recorded offset larger than the current log size (the log was replaced) is treated as "no record".
- **Rules on malformed input**: an unreadable or non-object offset record is treated as "no record", not as zero. An explicit negative offset is clamped to zero only when the caller passed it explicitly.
- **Seams**: `H1<->H2`: relies on the broker emitting `enqueue` before any `close` for accepted intents and emitting no `enqueue` for `DUPLICATE_EVENT` (true in the current broker). Because of that, a duplicate's refusal close has no fresh `enqueue` before it; the rule must therefore also accept a `DUPLICATE_EVENT` or `QUEUE_FULL` close that appears after the command started, and return rc 4 for it. `H1<->H4`: none.
- **Must-pass test**: build a log with `enqueue` and `VERIFIED` close for an id, delete the offset record, then append a `DUPLICATE_EVENT` close for the same id while `wait-event` is running. Assert rc 4. **Negative control**: with the offset record present and a fresh `enqueue` plus `VERIFIED` close appended after it, assert rc 0.

### SPEC-H2-S2 (seam hazard): fresh ids after a parked GUI event must not multiply

- **Target**: `swarm/agents/scripted_smoke.py`, `_enqueue_and_wait_gui_with_rebase`; `swarm/agents/prompts.py`, rule for rc 3 (line 38) and the camera suffix (line 132).
- **Hazard**: the retry model is now correct (wait through requeues, mint one fresh id only after the event is parked), but with broker auto-rebase off, every GUI attempt made while builders are active costs three camera turns and one park record. Four attempts are allowed.
- **Invariant**: at most one GUI event per step is pending in the camera FIFO at any time, and a new id is minted only after the previous one reached a terminal close.
- **Rules**: on rc 3 the agent re-observes and enqueues exactly one fresh id. On rc 124 it waits again on the same id and never enqueues. On rc 2 it does not repeat the keys. GUI steps stay gated behind builder completion in the smoke. When the brief is generated for a rehearsal that runs with auto-rebase (SPEC-H1-R7), the brief text does not need to change; the rule simply stops firing.
- **Seams**: `H1<->H2`: retry budget and the broker's three-drop limit multiply; document the product (attempts times three turns) in the smoke docstring. `H1<->H4`: each park becomes a `refute`; fewer is better.
- **Must-pass test**: the mock broker models tail requeue and parks at three drops. Run the GUI helper while the mock advances the head frame on every sweep. Assert that at no time two events for the same step are pending, and that the helper stops after the configured attempts with an error naming the stale history. **Negative control**: with the mock in auto-rebase mode, the first id returns rc 0 and no fresh id is minted.

---

## 4. Prose Repair Specifications: `H3`

### SPEC-H3-m4: recorder must not overwrite a take, and the time anchor must be honest

- **Target**: `swarm/tools/record.sh`, the output naming block and the block that samples the start time and launches the recorder.
- **Invariant**: starting a recording never destroys an existing file, and the sidecar start time is never earlier than the moment the recorder process was started.
- **Rules on valid input**: a new name in a new second produces a new file and sidecar, as today.
- **Rules on boundary input**: if the computed output path already exists, the script refuses with exit code 2 and a message naming the file; it does not start the recorder. The recorder is launched in a mode that fails rather than overwrites. The start time is sampled immediately after the launch instead of before it, and the sidecar gains a field stating that the anchor is the spawn time and that the first captured frame is later by the grabber's start-up latency (value not measured: AUSENTE, do not invent a number).
- **Rules on malformed input**: unchanged refusals for bad display strings and bad names. If the recorder exits within the liveness wait, no sidecar is written (already true).
- **Seams**: `H1<->H3`: the sidecar is the only wall-clock link between the video and broker timestamps; an anchor that is late by a small amount shifts overlays slightly late, which is less harmful than early (a cursor appearing before its event). `H3<->operator`: the operator can pass an explicit start offset to the compositor if a measured latency becomes available.
- **Must-pass test**: on a private virtual display (never display 1), start a recording, stop it, then create a file at the path a second invocation would compute and assert that invocation exits 2 without touching the file. Assert the sidecar start time is not earlier than a timestamp taken by the test just before invoking the script. **Negative control**: a different name records normally and produces a valid sidecar.

### SPEC-H3-E1 (edge case): events from earlier runs must not leak into the overlay state

- **Target**: `swarm/overlay/compositor.py`, `BrokerTimelineTracker` (its constructor, `advance_to`, `_apply`) and the cursor lookup in `evaluate_agent_cursor`.
- **Hazard**: the events log is append-only across broker runs. The sidecar now fixes the time window, but the tracker still replays every earlier record to reach the window start. Queued items that never closed in an earlier run, parks, and a stop state from an earlier run are shown at the first frame of the new video.
- **Invariant**: overlay state at any frame is derived only from records of the broker run that was live at that frame.
- **Run boundary**: the contract has no explicit run marker (AUSENTE). Use the broker's start-up behaviour as the marker: a group of initial lease `claim` records, one per ring agent, each with lease epoch 1. When the tracker meets such a group it resets the FIFOs, parked agents, control state, last verdict and head agent. Ask H0 for an explicit run-start record as the durable fix.
- **Rules on boundary input**: a renewal `claim` (lease epoch above 1) is not a boundary. A log with no `claim` records at all is treated as one run. Cursor interpolation never blends a position from before the boundary with one after it.
- **Rules on malformed input**: a `claim` record without a lease epoch is ignored for boundary detection.
- **Seams**: `H1<->H3`: depends on the broker issuing initial leases at start (`_issue_initial_leases`); if H1 later rebuilds lease epochs across restarts, the marker breaks, which is why the explicit record is preferable. `H3<->H4`: none.
- **Must-pass test**: build a log with a first run that ends with one queued, never-closed intent and an agent park, then an initial claim group, then a second run with one enqueue and one close. Advance the tracker to the end. Assert the first run's intent is not in any FIFO and no agent is parked. **Negative control**: remove the claim group from the same log and assert the stale intent and park are still visible, proving the boundary is what cleared them.

### SPEC-H3-E2 (edge case): non-finite timestamps in non-strict loading

- **Target**: `swarm/overlay/compositor.py`, `load_events`.
- **Hazard**: a timestamp that parses as an infinite number raises an overflow error that the current guard does not catch, so non-strict loading still crashes on that line.
- **Invariant**: in non-strict mode no single line can abort loading.
- **Rules**: integer and integral-float timestamps are accepted. Not-a-number, positive or negative infinity, negative values, booleans and non-numeric values cause the line to be skipped in non-strict mode and raise a line-numbered error in strict mode.
- **Seams**: `H1<->H3`: the broker only writes integer milliseconds, so this is defence against a hand-edited or foreign log.
- **Must-pass test**: a log with one valid record and one record whose timestamp is infinity loads in non-strict mode and returns exactly the valid record. **Negative control**: the same file in strict mode raises an error that names the line number.

### SPEC-H3-E3 (edge case): the span fallback must be loud in rehearsal use

- **Target**: `swarm/overlay/compositor.py`, `resolve_timeline`; `swarm/tools/compose.sh`, argument handling.
- **Hazard**: when no sidecar is found (renamed video, sidecar not copied), `auto` mode can still stretch the whole multi-run log over the video. It is logged on standard error but the wrapper exits 0.
- **Invariant**: a composed demo video is never produced from a guessed timeline unless the operator asked for it.
- **Rules**: `compose.sh` gains a default "require anchor" behaviour: if the resolved timeline source is the span guess, the wrapper exits non-zero with a message that names the missing sidecar path and the option that permits the guess. Explicit start and end options, a sidecar, and the explicitly chosen span mode are all accepted. The compositor module itself keeps its current behaviour so tests with synthetic input still work.
- **Seams**: `H1<->H3`: none beyond the multi-run log. Operator seam: the README command for the demo must show the sidecar being used.
- **Must-pass test**: compose a short synthetic video with no sidecar against a log whose timestamps are an hour away from the file time; assert the wrapper exits non-zero and writes no output file. **Negative control**: the same call with the option that permits the span guess succeeds and the log line names the span source.

---

## 5. Prose Repair Specifications: `H4`

### SPEC-H4-E2 (edge case): a failed `post_plan` must not look planned, and the audit must be scoped to this run

- **Target**: `swarm/projection/projector.py`, the enqueue branch of `process_record` (the call at lines 871-877) and the plan rollover path; `swarm/projection/reconciler.py`, the room audit in `audit`.
- **Hazard**: the agent is added to `planned_agents` without looking at what `post_plan` returned. If the store refuses the plan, the room's latest plan for that agent is from an earlier run, and that run's close comes after it, so the reconciler counts the agent as closed. This is the R1 failure class through a different door. Later steps are also posted into a task that does not exist.
- **Invariant**: an agent is recorded as planned only after a non-error `post_plan`. The reconciler accepts a room `close` for an agent only if it follows a room `plan` that this run created.
- **Rules on valid input**: a successful plan marks the agent planned and remembers enough to recognise that plan in the room later (the plan title the projector sent, including its continuation number, is sufficient).
- **Rules on boundary input**: on a refused or failed plan the agent stays unplanned, the projector records the failure in a list exposed like the finalize errors, and it tries the plan again on that agent's next enqueue. At finalize, every recorded plan failure becomes an anomaly with its own code and makes the report not clean. The reconciler, when it has projector state, ignores room plans that the state does not recognise when it decides which close counts.
- **Rules on malformed input**: a result that is not an object is treated as an error result. When no projector state is available (standalone audit), the reconciler keeps today's rule and says in its report that the audit was not run-scoped.
- **Seams**: `H1<->H4`: none on the record format. `H2<->H4`: H4 is the single TaskLight owner, so nobody else will repair a missing plan.
- **Must-pass test**: seed the room with an earlier plan followed by an earlier close for one agent. Run the projector with a sink whose `post_plan` returns an error result for that agent, then finalize with a working `close_task`. Assert the report is not clean and contains the plan-failure anomaly and a missing-close anomaly for that agent. **Negative control**: with a working `post_plan`, the same seeded room yields a clean report.

### SPEC-H4-E1 (edge case): a sink failure must not silently drop a record

- **Target**: `swarm/projection/projector.py`, `run_once` (the per-record guard at lines 1164-1171) and `process_record`.
- **Hazard**: the guard added for r1-3 catches every exception, logs a warning, and advances the offset. A transient sink failure (the switchboard CLI timing out, the TaskLight process dying) therefore drops that record's projection for good, and the final report can still be clean.
- **Invariant**: a record is consumed only when it was projected or when it is permanently unprojectable for a reason in the record itself. Failures of the outside world are retried or reported, never swallowed.
- **Rules on valid input**: unchanged.
- **Rules on boundary input**: distinguish two failure kinds. Data failures (a field of the wrong type, text that cannot be sanitised) are skipped as today and counted. Sink failures (process errors, timeouts, transport errors) leave the offset before the record so the next poll retries it; after a small fixed number of consecutive failures on the same record, the projector skips it, records an entry in a dropped-records list with the line number and record type, and continues.
- **Rules on malformed input**: at finalize, every entry in the dropped-records list becomes an anomaly and makes the report not clean. Retrying means the earlier part of a multi-sink projection may be posted twice; the README already states at-least-once delivery, so that is acceptable and must stay documented.
- **Seams**: `H1<->H4`: none. Switchboard seam: duplicate posts on retry are visible to room readers.
- **Must-pass test**: use a sink that raises a transport error on the first two attempts for one specific close record and then succeeds. Assert the record's evidence post appears, the offset passed the record only after the success, and the report is clean. Second case: the sink always fails for that record; assert the projector moves on after the retry limit and the finalize report contains a dropped-record anomaly. **Negative control**: a record with a wrongly typed field is skipped at once with no retry and no sink call.

### SPEC-H4-r1-6: make the missing-ref check real or remove it

- **Target**: `swarm/projection/reconciler.py`, the refusal branch and the park branch of `audit`; `swarm/projection/projector.py`, `extract_park_target`; `swarm/projection/README.md` line 128.
- **Hazard**: both checks build a non-empty fallback before testing for emptiness, so the anomaly can never be produced, while the README lists it as a live check.
- **Invariant**: the audit reports what the contract asks for in §7.1: a `refute` must reference the event it refuses. A synthetic ref is allowed for delivery but is reported.
- **Rules on valid input**: a refusal close with a real event id, and a park record with an event id or agent id, produce no anomaly.
- **Rules on boundary input**: a refusal close with a null or blank event id, and a park record whose target could only be recovered from the reason text or not at all, each produce an informational finding with a new code meaning "synthetic ref used". Informational findings are listed in the report but do not make it not clean. Enqueue-time refusals for an invalid event id are expected to have a null id and are counted separately.
- **Rules on malformed input**: a park record with no event id, no agent id and no parsable reason produces the real `MISSING_REF_ON_REFUSAL` anomaly and makes the report not clean. The park-target helper returns an empty result in that case, and the projector substitutes its literal fallback only at the point of posting.
- **Seams**: `H1<->H4`: the current broker always puts agent and event ids on park records, so the anomaly should not fire on broker output; it guards against a regression in H1. The README text must describe the two levels.
- **Must-pass test**: audit a log with one park record that has no ids and an empty reason; assert the anomaly is present and the report is not clean. Audit a log with a `NO_LEASE` close whose event id is null; assert one informational finding and a clean report. **Negative control**: a park record with both ids yields neither.

### SPEC-H4-r1-7: carried items

- **Target**: `swarm/projection/projector.py`, `REFUSAL_VERDICTS`, `run_follow`, `parse_args`, `main`, and `TaskLightMcpSink` (`_start`, `_read_raw`, `call_tool`); `swarm/projection/README.md`.

| Sub-item | Invariant | Behaviour to implement | Must-pass test and negative control |
|---|---|---|---|
| a. Verdict classification (m7) | The mapping from verdict to switchboard act is a documented decision | The README gains a table listing every contract §6 code, its contract category, and the act H4 posts, with one sentence explaining why dispatched failures and timeouts are posted as `refute` | Test: every §6 code appears in the table and in exactly one of the projector's verdict sets. Negative control: adding an unknown verdict to a record produces the existing warning log |
| b. SIGTERM in follow mode (m8) | Stopping the follower by signal is equivalent to an interrupt | On SIGTERM the follow loop ends after the current record, state is saved, and `--finalize` runs if it was requested | Test: start the follower on a temp log with finalize requested, send SIGTERM, assert exit within a few seconds, a saved state file, and a printed report. Negative control: SIGKILL leaves no report, and a restart resumes from the saved offset without duplicating earlier posts |
| c. `--once` (n4) | Every documented option changes behaviour or does not exist | Either remove the option from the parser and README, or make `--once` together with `--follow` an argument error | Test: the chosen behaviour. Negative control: plain invocation without either flag still runs one pass |
| d. MCP child robustness (n5) | A silent or chatty TaskLight child cannot stall the projector forever | The child's error stream is drained continuously or sent to a log file; each response read has a time limit; on expiry the child is terminated, the call fails as a sink failure (see SPEC-H4-E1), and the next call starts a fresh child | Test: a fake child that never answers; assert the call fails within the limit and the next call spawns a new child. Negative control: a fake child that writes a large amount to its error stream and then answers is handled without blocking |
| e. Room auto-open (n8) | The README states only verified behaviour | The auto-open path against the live daemon is unverified: AUSENTE, do not invent. Either verify it once against a scratch room with operator approval and record the result, or change the README to say the room must exist before the projector starts and make a missing room a clear start-up error | Test: with a fake switchboard CLI that reports a missing room, assert the documented behaviour. Negative control: with an existing room no open act is posted |

- **Seams**: `H1<->H4`: none of these change the record format. `H2<->H4`: item b matters to whoever stops the rehearsal; the runbook should stop the projector with an interrupt or SIGTERM and wait for the report.

---

## 6. Cross-unit items that need an H0 ruling

| Item | Units | Question for H0 | Blocks |
|---|---|---|---|
| GUI timeout verdict | H1, H2 | `TURN_TIMEOUT` or `INPUT_UNCERTAIN` for a GUI turn that timed out (`swarm/CONTRACT.md:110` against `264`) | SPEC-H1-R6 |
| Material ownership | H2 | Is linking an existing material to one's own mesh a `mat:` write? | SPEC-H2-M2 |
| Run marker | H1, H3, H4 | Add an explicit run-start record to §5.2, or accept the initial claim group as the marker | SPEC-H3-E1, and it would simplify SPEC-H4-E2 |
| §3.3 facts | H1, H2 | Where agent PIDs, the Blender PID and checkpoint files are recorded | SPEC-H1-M7 |
| `SWARM_BRIDGE_NONCE` table row | H1 | `swarm/CONTRACT.md:34` still lists a path as the default value of a variable that holds a hex nonce | Erratum only |
| `control` target fields | H1, H3, H4 | The broker now writes `agent_id` and `event_id` on `control` records, which §5.2 does not define | Erratum only |

## 7. Limits of this document

- `H2`, `H3` and `H4` statuses were established by reading the post-repair source. No suite and no probe was executed for this document, and no P4 re-review of `H2_r1`, `H3_r1` or `H4_r2` existed when it was written.
- The edge-case specifications labelled `S` and `E` are new observations from that reading, not review findings. Each names the lines it rests on so a reviewer can confirm or reject it.
- Nothing under `swarm/` was created or edited.
