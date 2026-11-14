# P4a Review of `H1` (`swarm/broker/`): Fable Reviewer, Track P

- **Reviewer model**: `claude-fable-5-2` (Track P, unit P4a, findings only, no code fixes)
- **Timestamp (UTC)**: 2026-11-14T12:02:57Z
- **Prior reviews read**: `product/reviews/H1.md` (REJECTED), `product/reviews/H1_r1.md` (APPROVED). The bytes reviewed here are the same bytes `H1_r1.md` approved.
- **Mode**: read-only on `~/Documentos/viewpoint-hackathon/`. All execution ran on a copy in `/tmp/vp_p4a_probe/` inside `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0`, with every `SWARM_*` variable pointed at `/tmp/vp_p4a_probe/run/`, `--vision-mode mock` or an injected vision runner, and the bridge driven through its pure-Python entry point. Live `/dev/shm/vqa-cu/swarm/nonce.txt` and `swarm/run/events.jsonl` mtimes were identical before and after the probes [MEASURED]. No `vision` call, no display, no `vpswarm` interaction.

## Files reviewed (SHA-256)

| File | SHA-256 |
|---|---|
| `swarm/broker/broker.py` | `b58c741142f6d6740fd0a650de9f9c90dc7bc0553e934342cf7b74dc7e36dbe5` |
| `swarm/broker/bridge.py` | `5c1d67c6a01240275e7ee27ad8418ca93bdd90b190e8fd904538319185ae9cb1` |
| `swarm/broker/test_broker.py` | `a89797b20f556bcfcb72090cc7af4d3c93da6c2d228b754da79062a538756231` |
| `swarm/broker/README.md` | `aa2095448edbd38a68a92917d58c526e91791678d09f78f36d00a95518944950` |
| `swarm/broker/__init__.py` | `48863c4e39ddd5c9928d62fad97c2ae62b504d3a2a9223ccd4ceed4b6a4258af` |
| `swarm/CONTRACT.md` (reference) | `b392f7dd16a3689b4af34e66a25a990dc131eb07d4f34fc7c47312069eec8fa4` |
| `swarm/agents/intent_cli.py` (seam, read-only) | `20eb195d9e7afbcf0de6acb28b7d4afa4f5aae27d7d518172528e5138237080c` |

## VERDICT: REJECTED

One live-path defect, and it sits in a named gate item (poison-pill resilience):

- **L1 (LIVE_PATH_MAJOR)**: one agent script that ends by exiting the interpreter (`sys.exit`, `exit`, `quit`, or a raised `SystemExit` / `KeyboardInterrupt`) permanently wedges the Blender bridge. The job is never removed, never receipted, and blocks every later job. The director's `reconcile` does not clear it.

Everything else in the gate list passes. No BLOCKER was found: path traversal, broker-side poison pills, nonce enforcement, append-only logging and both seams hold.

The repair surface for L1 is small (the bridge's exception boundary and the order of job removal). What is not measured: what Blender 4.0.2 itself does when a timer callback raises `SystemExit`. All three possible outcomes end the run (see L1).

Two launch conditions also need operator action before 13:30 CET. They are not code defects in `swarm/broker/`:

1. **The live Blender is running the pre-repair bridge.** The `blender` process for `vpswarm` (PID 3984943) started at 11:56:15 CET; `swarm/broker/bridge.py` was last written at 12:37:41 CET [MEASURED]. Blender loaded the old file, so the capsule must be relaunched to pick up any bridge change, including the L1 fix.
2. **The documented live command omits `--auto-rebase-viewport`** (`swarm/broker/README.md:18`). The live smoke log already shows the cost: 6 `STALE_FRAME` closes and 2 event parks before one camera intent was admitted (`swarm/run/events.jsonl`) [MEASURED]. With three script agents in the 14:00 run the camera will starve more often. Launch with the flag.

## Test results

- H1 suite on the copy: 16/16 pass in 3.897 s [MEASURED].
- Independent probes (`/tmp/vp_p4a_probe/probe.py`): 20 probes [MEASURED], results cited per item below.

## Gate checklist

| # | Gate item | Result | Code | Probe evidence [MEASURED] |
|---|---|---|---|---|
| 1 | `event_id` validation | PASS | `swarm/broker/broker.py:53`, `swarm/broker/broker.py:238-253`, `swarm/broker/broker.py:657-668`, `swarm/broker/broker.py:916-929` | 20 hostile ids (traversal, separators, NUL, 129 chars, non-ASCII digit, trailing space, non-string types): 19 refused `CLAIM_REFUSED`, planted `victim.py` unchanged, no `.py` outside the inbox. The one accepted id is `ok` plus newline (B1, not a traversal). |
| 2 | Poison-pill resilience, broker | PASS | `swarm/broker/broker.py:602-611`, `swarm/broker/broker.py:629-647`, `swarm/broker/broker.py:1000-1036` | 23 malformed agent and director payloads (list body, truncated, non-UTF-8, empty, 100000-deep nesting, 5000-digit integer, `NaN`/`Infinity` timestamps, wrong-typed `lane`, `write_set`, `script`, `script_sha256`, `lease_epoch`, `footprint`, `keys`, `based_on_frame`): no crash, no file left in an intent dir. Residual crash only on lone surrogates (B2). |
| 2b | Poison-pill resilience, bridge | **FAIL** | `swarm/broker/bridge.py:176-182`, `swarm/broker/bridge.py:194-197`, `swarm/broker/bridge.py:223-226` | L1 below. |
| 3 | Nonce enforcement in `bridge.py` | PASS | `swarm/broker/bridge.py:35`, `swarm/broker/bridge.py:59-78`, `swarm/broker/bridge.py:144-158` | Wrong nonce, no nonce, empty nonce field, non-numeric seq, `..` in id, leading `-`, wrong suffix: all moved to rejected and logged, none executed. Empty `nonce.txt`: everything rejected. Upper-case nonce accepted by design. Two cosmetic leaks in B6. |
| 4 | `events.jsonl` append-only + H1↔H2 byte-offset seam | PASS | `swarm/broker/broker.py:397-402`; reader `swarm/agents/intent_cli.py:413-434`, `swarm/agents/intent_cli.py:614-683` | Pre-seeded sentinel line survived a start, 3 dispatches and a restart; size monotonic; 0 unparseable lines. H2's own offset recorder and waiter, run against the broker, returned rc 0 `STATE_VERIFIED` for 3 of 3 events, each offset landing on a line boundary, with non-ASCII text in `expect`. The broker ignores H2's `.offsets/` directory. Edge case in B4. |
| 5 | H1↔H4 event schema seam | PASS | `swarm/broker/broker.py:410-421`, `swarm/broker/broker.py:530-543`, `swarm/broker/broker.py:562-580`, `swarm/broker/broker.py:719-739`, `swarm/broker/broker.py:799-810`, `swarm/broker/broker.py:1435-1473` | For `claim`, `enqueue`, `turn`, `close`, `cursor` and director `control`, the emitted key set equals the CONTRACT §5.2 key set exactly (symmetric difference empty) across script, GUI, stale, malformed-file and invalid-id paths. Broker-issued `park` controls add `agent_id`, `event_id` (and `drops`, `verdict` for stale parks); additive, and H4 reads them (`swarm/projection/projector.py:100-112`). |
| 6 | Hover accepted on `rc in (0,3)` | PASS | `swarm/broker/broker.py:1237-1242` | Hover rc 0 and rc 3 both proceed to keys. Hover rc 2 or 5 gives `INPUT_UNCERTAIN` + `RECONCILE` with 0 keys sent. Hover timeout gives `TURN_TIMEOUT` + `RECONCILE`. See B11 for what the hover really does. |
| 7 | `KP_1`/`KP_3`/`KP_4`/`KP_Add`/`Home` with `--expect-change 0,80,1580,900` | PASS | `swarm/broker/broker.py:52`, `swarm/broker/broker.py:1129-1144`, `swarm/broker/broker.py:1249-1252` | Captured argv per key is exactly `act --job vpswarm --key <KEY> --expect-change 0,80,1580,900`, in order, after exactly one `--xy 800,500 --expect-change none`; `KP_7` also allowed. 9 keys, a disallowed key, or a string instead of a list: `CLAIM_REFUSED`. Mixed rc 0 / rc 3 in any order: `VERIFIED`, `view_epoch` +1, `tick` +1. Key rc 6 or 8: `INPUT_UNCERTAIN` + `RECONCILE`, no further keys. |
| 8 | `STALE_FRAME` | PASS | `swarm/broker/broker.py:1171-1214` | Camera intent based on frame 0 with head 1: drops 1, 2, 3, then an event-level park record; the agent itself is not parked. Future, negative and ring-evicted frames: `FRAME_EVICTED`. Intent based on the head frame: `VERIFIED`. Agent-supplied `rebased` is ignored. Timing note in B10. |
| 9 | `--auto-rebase-viewport` | PASS | `swarm/broker/broker.py:290`, `swarm/broker/broker.py:1173-1174`, `swarm/broker/broker.py:1595` | Same stale intent with the flag: admitted in the first sweep, `VERIFIED`, `rebased: true`, `view_epoch` +1. `FRAME_EVICTED` is still enforced for future, negative and evicted frames. |
| 10 | `CLAIM_REFUSED` cross-claim isolation | PASS | `swarm/broker/broker.py:847-848`, `swarm/broker/broker.py:861-875`, `swarm/broker/broker.py:1104-1114` | props writing `obj:CHAR_*` (alone or mixed with its own prefix), empty `write_set`, a look entry without the colon, a look intent adding `cam:`, wrong case, leading space, a modeler GUI intent, and a modeler-authored intent dropped into the props directory: all `CLAIM_REFUSED`; the bridge executed only claim-conformant jobs. Stale `lease_epoch`: `LEASE_EXPIRED`. Declared-only limits in B9 and B11. |
| 11 | `--idle-timeout-s` | PASS | `swarm/broker/broker.py:1538-1548`, `swarm/broker/broker.py:1562-1569` | No work: exit rc 0 after 1.10 s with a 1.0 s timeout. Unresolved `RECONCILE`: rc 2. `FROZEN`: rc 0. Activity resets the timer: two intents arriving 0.3 s and 0.7 s into a 0.6 s timeout were both dispatched before a clean exit. Hazard with `--log-idle-pass` in B3. |

## Findings

### LIVE_PATH_MAJOR

**L1. A script that exits the interpreter wedges the bridge for the rest of the run.**

- **Coordinates**: `swarm/broker/bridge.py:176-182` (the job runs inside a handler that catches only ordinary exceptions), `swarm/broker/bridge.py:194-197` (the job file is removed only after the run returns), `swarm/broker/bridge.py:162-164` (lowest sequence first, one job per tick), `swarm/broker/bridge.py:223-226` (the timer wrapper also catches only ordinary exceptions).
- **Mechanism**: `SystemExit` and `KeyboardInterrupt` do not derive from the ordinary exception base class. When an agent script raises either, control leaves the job loop before the job file is removed and before any receipt is written, then leaves the timer callback too. The job stays in the inbox under the still-valid nonce with the lowest sequence number. One of three things then happens, and each ends the run:
  1. Blender treats the escaped `SystemExit` as a request to quit. On relaunch the bridge finds the same job and quits again.
  2. Blender drops the timer after the error. Nothing is ever polled again.
  3. The timer survives. Every 0.2 s the bridge re-runs the same script from the top, repeating every side effect that precedes the exit, and never reaches the next job.
  Which of the three Blender 4.0.2 does was not measured (the capsule must not be touched). The pure-Python part was measured.
- **Broker side**: the dispatch times out after 120 s [CITED `swarm/CONTRACT.md:96`], the queue enters `RECONCILE` with the actuator marked busy, and the job is deliberately left in place (`swarm/broker/broker.py:967-972`). A director `reconcile` clears the busy flag (`swarm/broker/broker.py:780-781`) but the wedged job is still first in line, so every later script also times out and each agent is parked after 2 timeouts.
- **Why this is live-path**: the trigger is an ordinary, schema-valid script submitted through the H2 SDK. Agents are language models writing Blender Python, and `swarm/agents/prompts.py` does not forbid exit calls. No adversary and no malformed bytes are needed.
- **Probe evidence [MEASURED]**: for each of four script bodies (import `sys` then call its exit function with 0; raise `SystemExit` with 3; raise `KeyboardInterrupt`; call the builtin `exit`), two successive single-job scans both propagated the exception to the caller, the timer callback propagated it too, both job files were still in the inbox and the outbox was empty. A control script dividing by zero behaved correctly (receipt `ERROR`, next job `OK`, inbox empty). With a script that appends one character to a marker file and then exits, 5 scans produced 5 characters and 0 receipts.
- **Reproduction specification (must-pass unit test for the repair writer)**:
  - *Setup*: create empty temporary inbox, outbox and rejected directories and a nonce file containing a 16-character hex nonce. Place two job files in the inbox, both named with that nonce: sequence 1 with event id `evil`, sequence 2 with event id `next`. The `evil` source first appends a single character to a marker file at a known temporary path, then terminates the interpreter. The `next` source is a harmless assignment. Use the bridge's real default executor, not an injected one. Parametrise the test over the four termination styles listed above.
  - *Steps*: call the bridge's single-scan entry point three times with the four directory arguments and a one-job limit. Then point the bridge's environment variables at the same directories and call the timer callback once.
  - *Expected*: no call raises anything. After the first scan the `evil` job file is gone from the inbox and the outbox holds a receipt for sequence 1 / `evil` whose status is `ERROR` and whose error text names the exception type. After the second scan the outbox holds a receipt for sequence 2 / `next` with status `OK`. The marker file holds exactly one character after all scans. The timer callback returns its normal polling interval.
  - *Observed today*: every call raises; the marker file gains one character per call; the inbox still holds both jobs; the outbox is empty.
  - *Companion broker-level test*: drive the broker in mock vision mode with an outbox poll hook that runs one real bridge scan (default executor) and swallows nothing. Enqueue a modeler script intent whose script terminates the interpreter, then a props script intent with a harmless script. Expected: first close `JOB_ERROR`, second close `STATE_VERIFIED`, queue state `IDLE`, inbox empty.

### BACKLOG

None of these blocks the scripted rehearsals when agents go through the H2 SDK. B1, B2, B5 and B8 confirm residuals already listed in `H1_r1.md`; they are kept short.

**B1. An `event_id` ending in a newline passes validation and stalls the queue for the full script cap.** (confirms R2)
- **Coordinates**: `swarm/broker/broker.py:53`, `swarm/broker/broker.py:243`.
- **Mechanism**: the pattern's end anchor also matches just before a final newline. The broker writes a job whose name contains the newline; the bridge rejects that name; no receipt ever arrives.
- **Reproduction**: start a mock-mode broker with a 0.3 s script timeout and a hook that runs real bridge scans. Submit a valid modeler script intent whose `event_id` is the two letters `ok` followed by one newline character. Run one sweep. Expected: a `CLAIM_REFUSED` close with reason "missing or invalid event_id", the intent archived under `rejected`, queue state `IDLE`, nothing written to the inbox or the bridge's rejected directory. Observed: `TURN_TIMEOUT`, queue state `RECONCILE`, and the job file in the bridge's rejected directory.

**B2. One lone UTF-16 surrogate in an agent string crashes the broker.** (confirms R1)
- **Coordinates**: `swarm/broker/broker.py:399-401` (event serialisation keeps non-ASCII, then encodes UTF-8), `swarm/broker/broker.py:892` and `swarm/broker/broker.py:932` (script hashing and job write).
- **Mechanism**: a JSON escape for an unpaired surrogate decodes to a string that cannot be encoded as UTF-8. The intent is already archived, so there is no restart loop, but FIFOs and seen ids are lost.
- **Reproduction**: three cases, each on a fresh mock-mode broker: a valid modeler script intent whose `kind` is the single escaped code unit D800; one whose `expect` is that; one whose `script` contains it (with `script_sha256` null). Write the file as ASCII JSON. Run one sweep. Expected: no exception; a `CLAIM_REFUSED` close for that `event_id`; the queue stays `IDLE`. Observed: `UnicodeEncodeError` in all three; in the first two the last record in the log is a `claim` (no `enqueue`, no `close`); in the script case the last record is the `act` cursor with no `close`.

**B3. `--log-idle-pass` spins without sleeping and floods the log; it also disables `--idle-timeout-s`.**
- **Coordinates**: `swarm/broker/broker.py:1411-1413` (the flag forces a sweep with no work), `swarm/broker/broker.py:1520` (the sweep reports work), `swarm/broker/broker.py:1562-1569` (sleep and idle exit happen only when no work was reported).
- **Measured**: an idle mock broker with the flag and a 1.0 s idle timeout was still running after 4 s and had written 74,328 lines, 14,460,782 bytes [MEASURED].
- **Reproduction**: launch the broker CLI in mock mode with isolated directories, `--log-idle-pass`, `--idle-timeout-s 1.0` and no intents, under a 5 s outer timeout. Expected: the process exits with code 0 within about 1.5 s and the log holds at most a few tens of lines (at most one PASS sweep per poll interval). Observed: no exit, and about 18,000 lines per second.
- **Operator note**: do not pass `--log-idle-pass` in a live run.

**B4. A log that does not end in a newline swallows the first record of the next run.**
- **Coordinates**: `swarm/broker/broker.py:397-402`.
- **Mechanism**: the appender never checks whether the existing file ends with a newline. After a crash mid-write, the next broker's first record (the modeler `claim`) is glued onto the torn line and both become unparseable. H2 then falls back to default lease values for the modeler.
- **Reproduction**: pre-seed the events log with a JSON object fragment that lacks its closing brace and has no trailing newline. Construct a mock-mode broker and close it. Parse the log line by line. Expected: four parseable `claim` records, one per ring agent, each on its own line (the torn fragment may stay unparseable on a line of its own). Observed: three parseable claims; the modeler claim shares a line with the fragment.

**B5. After `reconcile`, a timed-out job still runs later and is never observed; `resume` instead of `reconcile` burns queued intents.** (confirms R3)
- **Coordinates**: `swarm/broker/broker.py:967-972`, `swarm/broker/broker.py:766-797`.
- **Reproduction A**: mock-mode broker, 0.3 s script timeout, no bridge. Submit one modeler script intent and sweep (`TURN_TIMEOUT`). Submit a director `reconcile` and sweep. Then run one real bridge scan. Expected, any one of: the stale job is no longer in the inbox when `reconcile` completes; or its late receipt is consumed and recorded with a `scene_epoch` increment and a new frame. Observed: queue `IDLE`, actuator free, `scene_epoch` 0; the bridge then executes the stale job; its receipt sits unread in the outbox.
- **Reproduction B**: same start, but send `resume` instead of `reconcile`, then submit three props script intents and sweep four times. Expected: the queue does not return to `IDLE` while the actuator is busy, or the intents stay queued. Observed: queue `IDLE` with the actuator still busy, and all three intents closed `ACTUATOR_BUSY`.

**B6. A receipt already present in the outbox is accepted as the result of a new dispatch.**
- **Coordinates**: `swarm/broker/broker.py:932-946` (no check for, or removal of, an existing receipt before the job is written), `swarm/broker/broker.py:310-317` with `swarm/broker/broker.py:345` (a pinned `SWARM_BRIDGE_NONCE` plus a counter that restarts at 0 make names repeat across broker restarts).
- **Reproduction**: set `SWARM_BRIDGE_NONCE` to a fixed 16-hex value. Run a mock-mode broker with a bridge hook, submit a modeler script intent with id `s1`, sweep, close the broker. Start a second broker on the same directories with no bridge and a 0.3 s timeout; submit a different script under the same id `s1`; sweep. Expected: the second dispatch does not report `STATE_VERIFIED` (either the stale receipt is cleared before dispatch and the result is `TURN_TIMEOUT`, or the id is refused as a duplicate from the existing log). Observed: `STATE_VERIFIED` for a script the bridge never ran, and that job left in the inbox to run later.
- **Operator note**: do not pin `SWARM_BRIDGE_NONCE` across the 13:30 and 14:00 runs unless the outbox is emptied in between.

**B7. Bridge filename matching has two loose ends.**
- **Coordinates**: `swarm/broker/bridge.py:35` and `swarm/broker/bridge.py:144` (end anchor matches before a trailing newline), `swarm/broker/bridge.py:160` and `swarm/broker/bridge.py:210` (sequence is re-rendered from its integer value).
- **Reproduction**: with a valid nonce file, place two inbox files under the active nonce: one whose name is a well-formed job name followed by a newline character, one whose sequence is written `010`. Run one scan with a recording executor. Expected: both are moved to rejected as `INVALID_NAME` and neither runs. Observed: both run; the receipts are named without the newline and with sequence `10`, names no broker dispatch would wait for.
- **Reach**: needs write access to the inbox, which agents must not have. The broker never produces such names.

**B8. A `vision act` that exits 0 with a non-object body crashes the broker after keys were delivered.** (confirms R9)
- **Coordinates**: `swarm/broker/broker.py:1255-1263`.
- **Reproduction**: live-mode broker with an injected runner whose look calls return a real 1920x1080 PNG path and whose act calls return exit 0 with standard output equal to a JSON list (second case: an object whose `receipt` is null and whose `changed_regions` is empty). Submit a camera GUI intent with one key; sweep. Expected: no exception; a close of `VERIFIED` or `INPUT_UNCERTAIN`. Observed: `AttributeError` and no close record. Non-JSON and empty output are handled.
- **Reach**: not reachable with the current `vis`, which always prints an object with an object `receipt` (`~/Documentos/vis-skill/vis:834-836`, `~/Documentos/vis-skill/vis:1454`).

**B9. Any agent can consume another agent's future `event_id`.**
- **Coordinates**: `swarm/broker/broker.py:673` and `swarm/broker/broker.py:697` (one global seen set; no check that the id belongs to the submitting agent).
- **Reproduction**: mock-mode broker with a bridge hook. The modeler submits a valid script intent whose id is `run1-props-agent-7`; sweep. Then props submits its own valid intent under the same id; sweep. Expected: the modeler's intent is refused (id not in its namespace) or the props intent is still admitted. Observed: the modeler's runs `STATE_VERIFIED`; the props intent is closed `DUPLICATE_EVENT`.

**B10. A stale GUI intent spends all three drops within one poll interval, and its park record looks like an agent park.**
- **Coordinates**: `swarm/broker/broker.py:1176-1213`, `swarm/broker/broker.py:1562` (no sleep between sweeps while work is queued).
- **Mechanism**: a requeued intent keeps its original `based_on_frame`, and the next sweep starts immediately, so "return to the tail" never gives it a chance to become fresh. Measured: 3 sweeps and all 3 drops in 0.141 s [MEASURED]. The resulting `control` record uses `op: park` with the agent's id although the agent is not parked.
- **Reproduction**: live-mode broker with an injected runner, auto-rebase off. In one batch submit a modeler script intent and a camera GUI intent based on frame 0. Sweep until idle. Expected (design decision for H0/H1): either a single `STALE_FRAME` close with the event dropped at once, or requeues that wait for a new observation; and a park record that a reader can tell apart from an agent park without parsing `reason`. Observed: three `STALE_FRAME` closes and one `park` control in consecutive sweeps.

**B11. The "hover" is a left click, so every camera turn can change Blender's selection.** (contract-level, for H0)
- **Coordinates**: `swarm/broker/broker.py:1237-1240` implements `swarm/CONTRACT.md:105` verbatim. On a desktop target `vision act --xy` moves the pointer and then presses and releases button 1 (`~/Documentos/vis-skill/vis:762-764`).
- **Effect**: a click at 800,500 in the 3D viewport selects whatever is under it or clears the selection. That is scene state outside the camera's `cam:` / `render:` claim and it is not counted in `scene_epoch`. A later script from another agent that acts on the current selection or the active object would act on the wrong target. The live smoke log shows the visible side of this: the hover returned rc 3, i.e. pixels changed (`swarm/run/events.jsonl`, the pre-repair `INPUT_UNCERTAIN` close with reason "vision act exited rc=3").
- **Reproduction (integration, needs a scratch Blender, not the capsule)**: open a scene with one mesh object centred in the viewport and nothing selected. Run one admitted camera GUI turn with a single `KP_1`. Expected: the set of selected objects and the active object are the same before and after. Observed by code reading only, not executed: the click selects the object.
- **Mitigation without a contract change**: tell script agents never to rely on selection or the active object, only on names.

**B12. Non-finite numbers from an intent reach the log as bare `NaN` / `Infinity`.**
- **Coordinates**: `swarm/broker/broker.py:630` (the parser accepts them), `swarm/broker/broker.py:399` (the serialiser writes them), `swarm/broker/broker.py:727-728`.
- **Reproduction**: submit a valid modeler script intent in which `based_on_frame` is the bare token NaN and `view_epoch` is the bare token Infinity. Sweep. Re-read the log with a parser configured to reject non-standard constants. Expected: every line parses (the intent is refused, or the values are nulled). Observed: the `enqueue` line fails strict parsing; the script still runs `STATE_VERIFIED`. Python readers (H2, H4) tolerate it; a strict or JavaScript reader does not.

**B13. Epochs understate reality after a GUI timeout.** (confirms R4)
- **Coordinates**: `swarm/broker/broker.py:1272-1293`.
- **Reproduction**: live-mode broker with an injected runner: hover exit 0, first key exit 0, second key raises a timeout. Expected: `view_epoch` is 1 after the close, since one key was verified. Observed: `TURN_TIMEOUT`, `RECONCILE`, `view_epoch` 0.

## Cross-unit notes

- **H0**: B11 (hover is a click) and B10 (requeue semantics, park record shape) need a contract decision.
- **H2**: the waiter correctly keeps waiting through `STALE_FRAME` drops 1 and 2 and returns on drop 3 (`swarm/agents/intent_cli.py:671-675`); the earlier m10 concern is closed. The same end-anchor weakness as B1 exists at `swarm/agents/intent_cli.py:37`. Add one prompt line forbidding interpreter exit calls in scripts (belt and braces for L1).
- **Operator runbook**: after any script `TURN_TIMEOUT`, look in the inbox before sending `reconcile`; send `reconcile`, never `resume` (B5).
