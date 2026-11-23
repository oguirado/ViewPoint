# ref_broker: reference turn-queue actor broker + Blender bridge (unit F1)

Track P, declared non-Gemini product work. Written by claude-fable-5-2. Stdlib Python only.
Design source: `02_TURN_QUEUE_SPEC.md` (sections 2-7, 9) and the shared reference contract (RT layout below).

| File | Role |
|---|---|
| `bridge.py` | Blender startup script. Runs `RT/inbox/<nonce>_<event_id>.py` on the main thread, writes `RT/outbox/<event_id>.out` (line 1 `OK`/`ERROR`, line 2 `META {json}`), moves `*.py` without the nonce to `RT/inbox/rejected/`. |
| `broker.py` | Turn queue + actor broker. Only writer of `RT/events.jsonl`, `RT/cursor.jsonl`, `RT/frames/`; only caller of `vision look/act`. |
| `test_broker.py` | 34 unit tests: fake vision, real bridge core on a thread, no Blender. |
| `smoke_live.py` | Live smoke against a real Blender under a vision job. |
| `evidence/` | Output of the live smoke run (events, cursor records, 5 frames, nonce-reject listing). `evidence/smoke.log` is its console log. |

## Run

```sh
N=$(python3 -c 'import secrets;print(secrets.token_hex(8))'); RT=/dev/shm/vqa-cu/ref1; mkdir -p $RT
V="python3 -I ~/Documentos/vis-skill/vision"
cd /tmp && $V start --job vpref1 --size 1920x1080 --mem 8G \
  --app "env VP_SWARM_RT=$RT VP_SWARM_NONCE=$N blender --factory-startup --python $PWD_OF_REPO/product/ref_broker/bridge.py" --wait-title Blender
# RT/claims.json = {"modeler-agent":["col:CHAR_mesh"],...}; agents write RT/intents/<agent_id>/<event_id>.json (tmp + rename)
VP_SWARM_NONCE=$N python3 broker.py run --job vpref1 --rt $RT --turns 12     # rc 0 idle, 3 RECONCILE/FROZEN, 2 error
python3 broker.py control --rt $RT --op resume --by director-agent --reason "..."   # leave RECONCILE
VP_SWARM_NONCE=$N python3 smoke_live.py --job vpref1 --rt $RT --evidence evidence   # needs a fresh RT
$V stop --job vpref1
python3 -m unittest -v test_broker                                           # from this directory
```

The broker reads the nonce from env `VP_SWARM_NONCE` only (never from RT, which agents can read). `run` can be
called repeatedly: state (queues, turn, tick, frame, drops, parked agents, mode) is rebuilt from `events.jsonl`.

## Behaviour

One turn = one agent of the ring `modeler-agent, props-agent, look-agent, camera-agent`, one event at most.

| Verdict | When | Event afterwards |
|---|---|---|
| `PASS` | head agent has nothing queued (or is parked); tick and frame do not advance | — |
| `STATE_VERIFIED` | script job answered `OK`, bridge-measured changed collections ⊆ `write_set` | closed |
| `VERIFIED_WITH_SPILL` | script: `OK` but the bridge measured changes outside `write_set`; gui: a changed region outside the viewport footprint (+64 px slack) | closed |
| `JOB_ERROR` | script job raised | closed |
| `CLAIM_REFUSED` | script `write_set` empty or outside the agent's claim; gui key outside `KP_1 KP_3 KP_7 KP_4 KP_Add Home` or more than 8 keys | dropped, never dispatched |
| `TURN_TIMEOUT` | bridge did not pick the job up within the cap (120 s); the job file is retracted by atomic rename, so it never ran | tail, `drops+1`; 3 drops close it; 2 in a row park the agent (`control park`) |
| `OUTCOME_UNCERTAIN` | bridge took the job (retraction lost the rename race) but no outbox within the cap, or the outbox is not authenticated | closed; queue enters RECONCILE (`control reconcile`), no dispatch and no repeat until `control resume` |
| `LEASE_EXPIRED` | an outbox answer shows up for an event whose turn already timed out | answer moved to `outbox/late/`, ignored |
| `VERIFIED` | gui: hover `--xy 800,500`, then every key passed `--expect-change 0,80,1580,900` | closed |
| `NO_VISIBLE_CHANGE` | a key returned rc 3; remaining keys are not sent | closed |
| `INPUT_UNCERTAIN` | hover/key returned another rc (8, 9, ...) | closed; RECONCILE |
| `ACTUATOR_BUSY` | rc 6 before any key landed | tail, no drop |
| `STALE_FRAME` | gui `based_on_frame` < head frame and some frame in between differs in pixels | tail, `drops+1`; the agent may resubmit the same `event_id` citing a newer frame (`rebased:true`); 3 drops park it |
| `FRAME_EVICTED` | gui `based_on_frame` in the future or more than 16 frames old | dropped |
| `NO_LEASE` | gui intent from an agent other than camera-agent; intent whose `agent_id` differs from its directory | dropped |
| `QUEUE_FULL` | agent FIFO already holds 8; logged once per file, admitted later if room appears, nothing overwritten | — |
| `DUPLICATE_EVENT` | `event_id` already queued or closed (new file content) | refused |
| `BAD_INTENT` (local extension, not in 02 §7) | malformed intent file | refused |

Notes on choices where the unit brief and the spec differ or are silent:

- The brief says `VERIFIED_STATE`; 02 §7 (which the shared contract names as the source of verdict codes) says `STATE_VERIFIED`. The broker emits `STATE_VERIFIED`.
- A new frame is captured after every *dispatched* event (including `JOB_ERROR`, timeouts, uncertain). Refusals and `PASS` inject nothing, so they do not produce a frame.
- `changed_regions`: gui events use the regions reported by `vision act`; script events diff the previous and new PNG (ffmpeg downscale to 8 px cells + connected components). If ffmpeg fails the record holds the whole screen and `"regions_exact": false`.
- Pixel-identical rebase: a gui intent whose cited frame has the same sha256 as every later frame up to the head is admitted (`rebased:true`). Without it a read-only job from any other agent starves camera-agent forever (seen in the first live run, see below).
- Cursor records (`intent`, `act`, `result` per event) go to both `events.jsonl` and `cursor.jsonl`: fixed slot per agent at x=1700 for script events, viewport centre (790,530) for gui.
- Additions to the contract's outbox format, all after line 1: `META` carries `changed` (per-collection digest diff measured inside Blender), `ms`, `script_sha256` and `mac` = HMAC-SHA256(nonce, event_id, status, script sha). The broker requires the mac, so an agent cannot answer for the bridge. Other reference units that write outbox files themselves must use `bridge.poll_once` or compute the same mac.
- `event_id` must match `[A-Za-z0-9][A-Za-z0-9.-]{0,79}` (no `_`: it separates the nonce in inbox names).

## Verified

Unit tests, `python3 -W error::ResourceWarning -m unittest test_broker` under `systemd-run --user --scope -p MemoryMax=8G`: **34 run, 34 passed, 0 failed, 0 skipped** (2.9 s).
They cover every verdict in the table, restart recovery, the single-writer lock, the bridge nonce gate and the real ffmpeg differ.
Negative controls (the scenario is re-run with the check removed and must fail its assertions):

| Control | Check removed | Test |
|---|---|---|
| 02 §9.1 out of turn / no lease: no act injected, no dispatch record, frame unchanged | `_check_lease` | `test_nc1_negative_control` |
| 02 §9.2 stale frame: no input, event at tail with `drops:1` | `_check_fresh` | `test_nc2_negative_control` |
| 02 §9.3 dead head: `TURN_TIMEOUT` < 15 s, next agent's turn opens, late answer for the old `turn_epoch` gets `LEASE_EXPIRED` | `_late_outbox_scan` | `test_nc3_negative_control` |
| claim: job outside the claim never runs | `_check_claim` | `test_negative_control_claim_check` |
| idempotency | status table cleared | `test_negative_control_duplicate_check` |
| bridge nonce gate | `bridge._split` | `test_negative_control_nonce_gate` |

Live smoke (Blender 4.0.2, vision job `vpref1`, display `:2`, RT `/dev/shm/vqa-cu/ref1`), 3 rounds = 12 turns, `SMOKE PASS (0 failed)`:

- turn 1 `modeler-agent-1` `STATE_VERIFIED` (bridge measured `col:CHAR_mesh` changed), turn 3 `look-agent-1` `STATE_VERIFIED` (`col:LOOK_lights`), turn 4 `camera-agent-1` `STALE_FRAME` (cited frame 0, head 2, no key sent).
- turn 8 `camera-agent-1` resubmitted citing frame 2: `VERIFIED`, `rebased:true`, 2 acts (KP_1, KP_3); frame 3 shows "Right Orthographic" with the sphere above the cube (read by eye from `evidence/frame_3.png`).
- turn 10 `props-agent-1` read-only inspection job through the broker: Blender reports `CHAR_mesh: [CHAR_body]`, `LOOK_lights: [LOOK_key]`, bridge-measured change `[]`, frame 4 pixel-identical to frame 3.
- A `evil_job.py` without the nonce dropped in the live inbox was moved to `rejected/` and not run (`evidence/live_nonce_reject.txt`).
- `vision stop --job vpref1` returned rc 0 after the run.

A first live run failed its expectations (2 of 3 rounds as planned, then camera-agent parked after three `STALE_FRAME`): the smoke queued the inspection job ahead of the camera's turn, which advanced the frame each round. That led to the pixel-identical rebase above and to reordering the smoke; the committed evidence is from the second, clean run on a fresh Blender.

## Not verified / not implemented

- `write_set` is a declaration. Enforcement is after the fact (`VERIFIED_WITH_SPILL` from the bridge digest); the bridge does not sandbox bpy. The digest covers objects per collection (name, type, world matrix, data name, vertex count, material slot names), not materials, node trees, modifiers or render settings.
- No watchdog kills a Blender whose main thread hangs, no checkpoint .blend restore. A hung job ends as `OUTCOME_UNCERTAIN` + RECONCILE.
- No agent PID liveness test (02 §5 "agent dies before dispatch"); `NO_LEASE`/`LEASE_EXPIRED` are the non-interactive readings described above, not an interactive act API. `FOOTPRINT_MISSING` is not emitted (the footprint is fixed by the broker). HI-tier `control` events are only the CLI `control` subcommand, usable between `run` calls (the lock is exclusive).
- `TURN_TIMEOUT`, `OUTCOME_UNCERTAIN`, `LEASE_EXPIRED`, `JOB_ERROR`, `CLAIM_REFUSED`, `NO_LEASE`, `QUEUE_FULL`, `DUPLICATE_EVENT`, `VERIFIED_WITH_SPILL`, `NO_VISIBLE_CHANGE`, `INPUT_UNCERTAIN`, `ACTUATOR_BUSY`, `FRAME_EVICTED` and RECONCILE/resume were exercised only with the fake vision / bridge thread, not on live Blender. The 120 s cap is uncalibrated; live jobs took 0-3 ms.
- Live frame 0 is a blank grey window: the broker took it about 3 s after `--wait-title` returned, before Blender painted. Frames 1-2 still show the Blender splash; the camera hover click at 800,500 dismissed it. A capsule launcher should wait for a painted frame and dismiss the splash before frame 0.
- The hover is `vision act --xy 800,500`, which is a click: in another scene it can change the selection. rc 0 and rc 3 are both accepted for it.
- Frame records in `evidence/` keep their runtime `png` paths under `/dev/shm`; the PNG copies sit next to them.
- No OpenSwitchboard / TaskLight projection, no overlay compositing (other units).
