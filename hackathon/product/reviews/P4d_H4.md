# P4d Review of H4_r2: `swarm/projection/` (Repair Loop 2)

- **Reviewer**: `claude-fable-5-2` (Track P, reviewer unit P4d, findings only)
- **Timestamp**: 2026-11-14T11:57:27Z
- **Worktree / branch / HEAD**: `vp-wt-P4d` / `p-P4d` @ `df0946d`
- **Writer quiescence**: `swarm/proof/H4_r2_1249.pid` (PID 154268) had exited before the first read. The hashes below were taken twice, before the read and after the last probe (by which time `H4_r2_1249.finished_at.txt` existed), and were identical.
- **Mode**: read-only on `~/Documentos/viewpoint-hackathon/`. This file is the only file written. All tests and probes ran on copies under `/tmp/vp_p4d_probe`, inside `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0`, with `PYTHONDONTWRITEBYTECODE=1`.
- **Live rooms**: the TaskLight room `~/.context-wall/rooms/viewpoint/events.jsonl` was only copied (SHA-256 `10cc88b3…996f8de8`, 307 lines, same hash after the probes [MEASURED]). The real `bridge.room.mcp` was always started with `CW_HOME` under `/tmp`. No `osb post` was executed: `osb` acceptance was checked by loading its pure `text_error` function and its `--ref` rule, and all `osb` traffic went to the in-memory `DryRunSink`.

## Files reviewed

| File | SHA-256 |
|---|---|
| `swarm/projection/__init__.py` | `7590f743c990d350f15c041c95187186fad1f5eeed191864fbac36d5a97b34d3` (unchanged since H4) |
| `swarm/projection/projector.py` | `bbe0c5fd99ba7e1a96a23196e9c187a007d88f3a7fa880343ff0eab41dee4693` |
| `swarm/projection/reconciler.py` | `6972908cabb197164d9aa7444de462a9e2a072194f2a7943e70917c9c78b7148` |
| `swarm/projection/test_projection.py` | `444a599ed814a956a6bb9c867ea881f3718108ca53fcf2e533c672759094e038` |
| `swarm/projection/README.md` | `18aae6087da92a02bf2f4f734a969e1be843b2f297c1ef71daded9d97879c2d3` |

Cross-checked, not under review: `swarm/CONTRACT.md` (`b392f7dd…9eec8fa4`), `swarm/broker/broker.py` (`b58c7411…7e36dbe5`), `switchboard-skill/bin/osb` (`text_error` at 143-153, ref rule at 601-603), and `FireBase_BuildHybridAI/repo/bridge/room/store.py` and `mcp.py`.

VERDICT: APPROVED

Every gate item named in the brief passes, with measured evidence, including the R1 reproduction against a copy of the live room and against the real `bridge.room.mcp`. No BLOCKER and no LIVE_PATH_MAJOR was found. Eleven BACKLOG findings follow. Two of them (B1, B2) leave ways for the audit to report CLEAN on a room that is not actually in a clean state. I rated them BACKLOG because B1 needs a transport fault inside a single MCP call and B2 needs the projector to keep running on the same state file after `--finalize`, which the README describes as terminal. If either condition is expected in the rehearsal, treat that finding as gating.

## Gate results

| # | Gate item | Result | Evidence [MEASURED unless noted] |
|---|---|---|---|
| R1a | In a persistent room with earlier `close` events, a new `plan` needs a `close` strictly after it in room order | PASS | `reconciler.py:204-236`. On the live-room copy (which holds one `plan` and two `close` per tracked agent from W0), a new `props-agent` plan with a refused close gave `MISSING_CLOSE_TASK` plus `CLOSE_TASK_REFUSED`, `clean=False`. The standalone reconciler on the same room, with no projector state, also gave `MISSING_CLOSE_TASK` for `props-agent` only. The happy path on the same room gave `clean=True` with no anomalies |
| R1b | `finalize()` turns every `last_finalize_errors` entry into `CLOSE_TASK_REFUSED`, `clean=False`, `rc != 0` | PASS | `projector.py:1217-1222`, `reconciler.py:51-55`, `projector.py:1321`. CLI run with a refusing sink returned rc 1. Real MCP with the room file made read-only before finalize: the store answered `isError` (`code: internal`), and the report held `MISSING_CLOSE_TASK` and `CLOSE_TASK_REFUSED` |
| R1c / r1-5 | An exception in one agent's `close_task` does not stop the remaining attempts | PASS | `projector.py:1112-1122`. With the first agent raising, all four agents were attempted in sorted order, three were closed, and the report held `MISSING_CLOSE_TASK` for the failing agent plus `CLOSE_TASK_REFUSED` |
| r1-2 | U+007F is sanitized for `osb` | PASS | `projector.py:83`. Sanitized DEL, CR, NUL and ESC inputs all passed the real `osb.text_error` |
| r1-3 | Lone UTF-16 surrogates do not end the run | PASS | `projector.py:85-86`, `92`, `517-520`, `600`, `1158`. A log with raw `\ud800`, `\udfff` and `\udc00` escapes in `reason`, `kind` and `event_id` was processed fully (5 of 5 lines), every `osb` text passed `text_error`, the real MCP accepted the plan and both steps, the state file saved, and `--finalize` printed a report. 5,000 lone surrogates were capped to 3,798 B |
| r1-4 | Binary newline framing on partial UTF-8 tails | PASS | `projector.py:1144-1156`. A record cut inside a four-byte character: 0 lines and offset 0 on two consecutive polls; after the rest arrived, 1 line, offset 120 of 120, one `claim` with the text intact, and no replay. A line with invalid UTF-8 bytes and a CRLF ending was still projected. A restart from the state file processed 0 lines |
| Rollover | 12-step TaskLight plan rollover | PASS | `projector.py:953-965`, `1013-1025`. Real MCP, 14 closes: steps `0..11`, a `(cont. #2)` plan, then steps `0, 1`, then one `close`, `clean=True`. Mirror sink, 30 closes with refusals mixed in and a projector restart from the state file after close 13: indices `0..11, 0..11, 0..5`, plans up to `(cont. #3)`, 30 steps for 30 closes, `clean=True` |
| stop / resume vs `--finalize` | `stop` and `resume` post statuses only; `close_task` happens only in `--finalize` | PASS | `projector.py:1059-1077`, `1102-1129`. Real MCP: `Paused…` and `Resumed…` heartbeats for both planned agents, zero `close` events before finalize, exactly one per agent after it. Finalizing while still stopped closed the task and gave `clean=True`. See B2 for what happens when events continue after a finalize |
| `--ref` on refutes | Every `osb` `refute` carries a non-empty `--ref`, and it is the event id whenever one exists | PASS | `projector.py:991-1003`, `1081-1092`, `499-500`, `516-521`. Fuzz of 17 refusal closes and 8 parks: every emitted `refute` had a non-empty ref. Broker refusals use the validated `event_id`; a null id (broker `CLAIM_REFUSED`, `broker.py:657`) gets `refusal-turn-<turn>-<agent>`. Both broker park forms now carry a top-level `event_id` (`broker.py:1203-1212`, `1298-1305`), and the park ref resolved to it. See B7 for refs that `osb` would refuse, none of which the broker can emit |
| Record schema match | Projector reads the fields the broker writes | PASS | `broker.py:399-401`, `564-580`, `721-737`, `800-809`, `1437-1472` against `projector.py:850-1098` |
| Forbidden surfaces | No `vision`, display `:1`, hybrid path, `playbooks/` or `pkill` | PASS | none found in `swarm/projection/` |
| Test suite | Unit tests on the `/tmp` copy | PASS, 29/29 in 0.338 s | includes regressions for the stale-close room, DEL, surrogates, split UTF-8 and r1-5 |

## Findings

All findings are BACKLOG. Each has a location, a mechanism, and a reproduction written as a must-pass unit test.

### B1. BACKLOG (highest). The per-record exception guard also swallows sink failures, and a lost `post_plan` is then hidden by an old `close` in the room

- **Where**: `projector.py:1165-1172` (guard), `projector.py:1174` (offset advances regardless), `projector.py:871-877` (plan bookkeeping), `reconciler.py:233-236` and `264-273`.
- **Mechanism**: the guard added for r1-3 catches every exception from `process_record`, including a transport failure raised by the TaskLight sink (`projector.py:607-611`, `627-628`). The record is logged as skipped and the offset moves past it. Before this repair the same failure ended the run with a non-zero exit and the record was replayed on restart.
  - If `post_plan` fails this way, the agent never enters `planned_agents`, so `--finalize` issues no `close_task` for it. In the live room the agent's W0 `close` sits after its W0 `plan`, so the reconciler counts the agent as closed.
  - If `post_step` fails this way, that close receipt has no microtask and nothing reports it.
- **Measured**, on a copy of the live room with the real MCP and the child ended during one call:
  - Failure during `post_plan`, one enqueue: the room gained a heartbeat and a step for `look-agent`, no plan and no close; the report was `clean=True` with no anomalies.
  - Failure during the second of three `post_step` calls: two steps for three closes; `clean=True`.
- **Why BACKLOG**: the fault has to land inside one call. A child that dies between calls is restarted by `projector.py:559`, and store refusals come back as `isError`, not as exceptions. A second enqueue by the same agent also retries the plan.
- **Must-pass test**: seed a room with an earlier plan followed by a close for one tracked agent. Run the projector on one enqueue, one turn and one admitted close for that agent, using a sink whose `post_plan` raises on its first call. Finalize. The test passes only if the report is not clean and the exit code is non-zero. A second case uses a sink whose `post_step` raises once among three admitted closes, and passes only if the run either reports an anomaly or leaves the offset before the failed record so that a second run posts the missing step.

### B2. BACKLOG. Events that arrive after a `--finalize` are written into closed tasks and the next audit is CLEAN

- **Where**: `projector.py:868` (no new plan for an agent already in `planned_agents`), `projector.py:1108` (no second `close_task` for an agent in `closed_agents`), `reconciler.py:233-236` (only compares the latest plan with the latest close).
- **Mechanism**: after finalize the agent is in both sets. If the same state file is used for later events, steps and statuses are posted after the `close`, no plan reopens the task, and the next finalize closes nothing. The room order still has the close after the latest plan, so the reconciler is satisfied.
- **Measured**: finalize, then a `resume`, an enqueue, a turn and a close, then finalize again. Room order was plan, heartbeat, step 0, heartbeat, close, heartbeat, step 1. Second report: `clean=True`.
- **Why BACKLOG**: `README.md:92` describes `--finalize` as terminal completion. This becomes a live-path defect if the projector is restarted with `--follow --finalize` on one state file during a rehearsal.
- **Must-pass test**: run and finalize one agent with a mirror sink, append a further enqueue, turn and admitted close to the log, run and finalize again with the same state file. The test passes only if the room ends with a close after the last step of that agent (through a new plan and a new close, or a second close), or the second report is not clean.

### B3. BACKLOG. `isError` results from `post_plan`, `post_step` and `post_status` are ignored

- **Where**: `projector.py:871-877`, `921-925`, `959-975`, `1019-1035`; the only handling is the warning at `projector.py:630-631`.
- **Mechanism**: only `close_task` results are inspected. A store refusal on a plan or a step (for example `room_full` at the 8 MiB cap [CITED: `store.py:13`, `434`], or `internal` on an I/O error) is logged and then treated as success: the agent is marked planned and the step index advances. If the store recovers before finalize, the close succeeds and the report is clean with the step missing.
- **Reproduction basis**: by reading; the `internal` refusal itself was observed on `close_task` in the read-only probe.
- **Must-pass test**: use a sink whose `post_step` returns an error result for one of three admitted closes and succeeds otherwise. Finalize. The test passes only if the report carries an anomaly naming the refused step, or the step is retried and present in the room.

### B4. BACKLOG. An operator `checkpoint` does not clear uncertain events

- **Where**: `reconciler.py:185-187`, `projector.py:1094-1096`; broker semantics at `broker.py:793-797`.
- **Mechanism**: the broker leaves `RECONCILE` on either `reconcile` or `checkpoint`. The projector and the reconciler clear uncertain events only on `reconcile`. A run resolved by `checkpoint` ends with `UNRECONCILED_UNCERTAIN_EVENT` and exit code 1. This fails closed.
- **Measured**: an `OUTCOME_UNCERTAIN` close followed by `checkpoint` gave `clean=False` with that anomaly.
- **Must-pass test**: one uncertain close followed by a `checkpoint` control record, then finalize. The team must first decide whether `checkpoint` resolves the event; the test asserts that decision, and the README states it.

### B5. BACKLOG. The reconciler crashes on a JSON line that is not an object

- **Where**: `reconciler.py:133` (broker log) and `reconciler.py:220-221` (room file).
- **Mechanism**: a valid JSON line holding an array, a string or `null` reaches a field lookup on a non-object and raises. The projector skips such lines (`projector.py:836-837`). The exit is still non-zero, but through a traceback, after `close_task` has already been sent, and without a report.
- **Measured**: `AttributeError` on a log containing an array line.
- **Must-pass test**: a broker log with one valid enqueue plus lines holding an array, a string and `null`. The audit must return a report (with a malformed-line anomaly for each, or by skipping them) and must not raise. Repeat with the same lines in the room file.

### B6. BACKLOG. A truncated or replaced events log stalls the projector silently

- **Where**: `projector.py:1145-1152`.
- **Mechanism**: when the saved offset is beyond the end of the file, the seek succeeds, the read returns nothing, and every later poll processes 0 lines with no warning.
- **Measured**: offset 228 against a file of 120 B gave 0 lines.
- **Must-pass test**: process a log, replace it with a shorter one, run again. The test passes only if the run logs or reports the condition (or restarts from 0 by a documented rule) and does not return 0 lines silently.

### B7. BACKLOG. A ref with control characters or more than 256 characters still reaches `osb`, which refuses it

- **Where**: `projector.py:516-521` (only surrogates and NUL are replaced), `projector.py:100-112` (park target taken from `reason`); `osb:601-603`.
- **Mechanism**: `osb` refuses a ref that is empty, longer than 256 characters, or contains a character below U+0020. The projector only logs the rc 2, so that `refute` is lost.
- **Reachability**: not from broker-generated records. Event ids are validated to 128 safe characters (`broker.py:53`, `657`). The remaining path is an operator `park` control with no `agent_id` and no `event_id`, whose `reason` has a colon followed by a newline or by more than 256 characters (`broker.py:759`).
- **Measured**: of 23 fuzzed refute refs, 4 would be refused by the `osb` rule (newline in id, 300-character id, newline in a parsed park reason, 400-character parsed park reason).
- **Must-pass test**: a park record with no ids and a reason containing a colon followed by multi-line text, and a refusal close with a 300-character id. Every ref handed to the sink must satisfy the `osb` rule (1 to 256 characters, none below U+0020).

### B8. BACKLOG. `MISSING_REF_ON_REFUSAL` still cannot fire (r1-6, open)

- **Where**: `reconciler.py:156-164` and `178-184`; `README.md:128`.
- **Mechanism**: both checks test a value that is built to be non-empty on the line above. The README still lists the check as active.
- **Must-pass test**: either remove the check and the README line, or audit what the projector actually sent (the recorded refusal refs in the state) and assert that a state entry with an empty ref produces the anomaly.

### B9. BACKLOG. The README delivery claim no longer matches the code

- **Where**: `README.md:117`, `README.md:180`.
- **Mechanism**: the README says delivery is at-least-once. Since B1's guard, a record whose projection raises is delivered at most once, and partially (the `osb` post may have gone out while the TaskLight call did not).
- **Must-pass test**: none in code; the README must state the semantics chosen when B1 is fixed.

### B10. BACKLOG. A trailing line with no newline is held by the projector but read by the reconciler

- **Where**: `projector.py:1154-1156` against `reconciler.py:117-130`.
- **Mechanism**: at finalize, the projector has not projected the unterminated last line. The reconciler iterates the whole file and either reports it as malformed or, if it is complete JSON, counts a record the projector never projected (for example an enqueue for an agent with no plan, which yields `MISSING_CLOSE_TASK`). This fails closed. By reading; not exercised.
- **Must-pass test**: a log whose last record is a complete enqueue for a second tracked agent with no trailing newline. Finalize. The reconciler must apply the same framing rule as the projector, and the test asserts the report does not count the unterminated record.

### B11. BACKLOG. Carried over, still open

- A `close` record with a null or empty verdict is ignored without a log line (`projector.py:983`). Must-pass test: such a record produces a warning or a refute.
- SIGTERM during `--follow` skips `--finalize` (`projector.py:1197`). Must-pass test: a follower sent SIGTERM saves its state and, when `--finalize` was given, still attempts the closes.
- `--once` has no effect (`projector.py:1269-1272`, `1313-1316`).
- The MCP child's stderr is never drained and the response read has no timeout (`projector.py:565-573`, `604-611`). Must-pass test: a stub server that never answers makes the call fail within a bounded time.
- The classification of `JOB_ERROR`, `INPUT_UNCERTAIN`, `OUTCOME_UNCERTAIN` and `TURN_TIMEOUT` as refusals is still not documented against contract §6 (`projector.py:40-54`).
- Room auto-open against the live `sbd` (`projector.py:456-487`): AUSENTE — no inventar. Not exercised, to avoid writing to a live room.

## Probe provenance

Probe scripts: `/tmp/vp_p4d_probe/probes/probe.py` and `probe2.py`. Sources under test: `/tmp/vp_p4d_probe/src/swarm/`. All values are [MEASURED] on 2026-11-14.

| Probe | What it exercised | Result |
|---|---|---|
| Suite | `python3 -m unittest swarm.projection.test_projection` | 29/29, 0.338 s |
| R1a | Live-room copy, new plan, refused close; standalone reconciler; CLI; happy path | `clean=False` with both anomalies; `MISSING_CLOSE_TASK` for the one agent; rc 1; `clean=True` |
| r1-5 | First agent raises in `close_task` | 4 attempts, 3 closed, 2 anomalies |
| MCP happy | Real `bridge.room.mcp` on the live-room copy: 14 closes, stop, resume, one refusal | `clean=True`; rollover and status sequence as in the gate table; 35 of 35 `osb` texts accepted by `text_error` |
| MCP read-only | Room file read-only before finalize | `isError`, `MISSING_CLOSE_TASK`, `CLOSE_TASK_REFUSED` |
| MCP killed between calls | Child ended before a call | restarted transparently, `clean=True`, room correct |
| MCP killed mid-call | Child ended during `post_step` and during `post_plan` | B1 |
| Text | Sanitizer against `osb.text_error`; surrogate log through the real MCP | all accepted; 5 of 5 lines processed |
| Framing | Split four-byte character, invalid UTF-8, CRLF, non-object lines, truncation | r1-4 PASS; B5; B6 |
| Rollover | 30 closes with a restart | 30 steps, 3 plans |
| Lifecycle | Finalize while stopped; finalize, continue, finalize; checkpoint | PASS; B2; B4 |
| Refs | 25 fuzzed refusal and park records | 23 refutes, all with a non-empty ref; B7 |
