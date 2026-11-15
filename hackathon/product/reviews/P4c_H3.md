# P4c Review — H3_r1 (`swarm/overlay/`, `swarm/tools/`)

- **Reviewer**: `claude-fable-5-2` (Track P, unit P4c; findings only, no edits under `swarm/`)
- **Timestamp**: 2026-11-14T12:00:52Z
- **Reviewed tree**: `~/Documentos/viewpoint-hackathon` on `swarm-1114` at `df0946d`, read after the `H3_r1` writer (PID 154265, `swarm/proof/H3_r1_1249.pid`) had exited. Hashes were re-checked after the last probe and had not changed.
- **Test isolation**: every run on the copy `/tmp/vp_p4c_probe`, inside `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0`, with `DISPLAY=""`. `record.sh` ran with `HOME` and `SWARM_RUN_DIR` pointed into the copy and, for every argument that passes its gate, with a fake `ffmpeg` first on `PATH`. No X server was started. Display numbers used in parser probes (`:9188`, `:9189`) had no socket. `:1`, `:2`, `vpswarm`, `/dev/shm/vqa-cu/` and the playbooks directory were never touched; the operator's own `ffmpeg … -i :1` recorder was seen running and left alone.

## Files reviewed

| File | SHA-256 |
|---|---|
| `swarm/overlay/compositor.py` | `cc6c24dc0f52fa387aa6c16aa4aba24f57daabffff359e914948b2ad80618272` |
| `swarm/overlay/__init__.py` | `282535101ba37ee9915b78e172e4c9ff8b017e93ad4e45ad39c79d44e4cc39c4` |
| `swarm/overlay/test_overlay.py` | `5f4705c148f759466afa5150b713db4b6f3556970637396f5fbc28e1af4b3b8c` |
| `swarm/overlay/README.md` | `aea70699ce487ad37172f587910b89fa403a6e218071692467dd2f2add041ecb` |
| `swarm/tools/record.sh` | `0e0b6c838240b5427d73672a49879499c62bb64aed7afd8a17bfdd383f952883` |
| `swarm/tools/compose.sh` | `1364b6e559c0cdfc1f127a588a4df6f0dd081da4e487a273fa0532f037216cfd` |
| `swarm/CONTRACT.md` (reference) | `b392f7dd16a3689b4af34e66a25a990dc131eb07d4f34fc7c47312069eec8fa4` |
| `product/reviews/H3.md` (reference) | `de8598310c211c7ecf159098169f8536f2637ea6feff0db818e0c6c3edba2035` |

Cross-referenced read-only: `swarm/broker/broker.py` (control and park emitters at lines 762-812, 984-998, 1198-1213) and a copy of `swarm/run/events.jsonl` (100 records [MEASURED]).

VERDICT: REJECTED

All four requested fixes (B1, M1, M2, M3) are verified as working. The rejection rests on one new BLOCKER of the same class as B1: a display argument that starts with a `+x,y` offset passes the gate, and `ffmpeg` then ignores the display written in the argument and records whatever the ambient `DISPLAY` is, which on the operator host is `:1`. One new live-path defect (N2) is also recorded; it has an operational workaround.

---

## Requested fix verification

| Fix | Result | Evidence |
|---|---|---|
| **B1** leading-zero normalisation (`record.sh:53-72`) | PASS | `:1`, `:01`, `:001`, `localhost:01.0`, `unix:001`, `:1.0`, `:1+0,0`, `:0000000001`, a 20-digit zero-padded 1, `+0,0:1` → rc 2 with and without `--allow-display0`, and no `ffmpeg` spawned [MEASURED]. `:0`, `:00`, `:0.0`, `unix:000` → rc 2 without the flag, rc 0 with it [MEASURED]. Malformed forms (`:1 `, `:+1`, `::1`, `a:b:1`, `:0x1`, `:1e0`, embedded newline) → rc 2 [MEASURED] |
| B1 sidecar escaping (`record.sh:127-128`) | PASS | A display host containing a double quote and a backslash produced a sidecar that parses as JSON and round-trips the string [MEASURED] |
| B1 second-resolution naming (`record.sh:82-83`) | PASS | Output names carry `%H%M%S` [MEASURED]. Same-second collisions remain (backlog K3) |
| **M1** sidecar authority (`compositor.py:434-438`) | PASS | 80 prior-run events plus 20 events one hour later, sidecar at the later run: `resolve_timeline` returned the sidecar window with mode `sidecar` and a 10 000 ms span equal to the video duration [MEASURED]. Without a sidecar the same log still falls to a 64.9-minute `span` (backlog K1) |
| **M2** event park vs agent park (`compositor.py:624-661`) | PASS | Three `STALE_FRAME` closes plus the broker's `parked_after_3_stale_drops:<event_id>` control record (both the r1 shape with `agent_id`/`event_id` and the legacy shape in the real log): `parked_agents` empty, event removed from the FIFO. `<agent>: 2 consecutive TURN_TIMEOUT` → agent parked. Operator park with `agent_id` only → parked. Targeted `resume` clears only its target [MEASURED]. Reason strings match `broker.py:994` and `broker.py:1210` |
| **M3** stderr drain (`compositor.py:1061-1079`, `1183-1192`, `1146-1148`) | PASS | 1920x1080, 15 fps, 180 s (2700 frames) composed in 27.6 s with the encoder's stderr pipe shrunk to 4 KiB and its log level forced to `debug` [MEASURED]. Negative control with the drain function replaced by a no-op: hung, killed at 90 s (rc 124) [MEASURED], so the probe is not vacuous. The shipped command also passes `-nostats -loglevel error` |
| M3 `compose.sh` verification (`compose.sh:42-95`) | PASS | Nominal run rc 0. With an `ffmpeg` wrapper that drops `-movflags +faststart`: rc 1, "faststart verification failed (moov=2514287, mdat=40)". With a wrapper that swaps in `yuv444p`: rc 1, "expected pix_fmt=yuv420p" [MEASURED] |
| M3 zero X11 connections | PASS | `strace -f -e trace=connect` over a full `compose.sh` run of the 180 s 1080p input: 0 `connect()` calls of any kind, rc 0, 33 s wall, output `h264`, `yuv420p`, 1920x1080, 2700 frames [MEASURED] |
| Unit suite on the copy | PASS | `python3 -m unittest discover -s swarm/overlay`: 12 tests OK in 3.588 s [MEASURED] |
| Prior minors m1, m2 | PASS | A `DUPLICATE_EVENT` close leaves the original queued item and `close_by_event` untouched; a torn trailing line is skipped in non-strict mode [MEASURED] |

---

## Findings

### BLOCKER

**N1 — A leading `+x,y` offset makes `ffmpeg` drop the gated display and record the ambient `DISPLAY` (`:1` on the operator host).** `swarm/tools/record.sh:39-46`, `swarm/tools/record.sh:95-100`

Failure mechanism. The gate takes everything after the first colon, cuts at the first `+` and the first `.`, and validates that remainder as the display number (lines 44-46). It never checks what comes before the colon. For the argument `+0,0:5` the gate therefore sees display 5 and passes it. The raw argument is then handed to `ffmpeg -f x11grab -i` (line 100). The x11grab input splits its argument at the first `+`: with nothing before the `+`, the display name is empty, the trailing `:5` is discarded, and it calls `xcb_connect` with a null name, which means "use the `DISPLAY` environment variable". `record.sh` does not clear `DISPLAY` for the child (line 95), so the recorder captures the caller's ambient display.

Evidence, all with `DISPLAY=""` and no X server:
- `record.sh "+0,0:5" <name>` → rc 0, and the fake `ffmpeg` received `-i +0,0:5` [MEASURED].
- Real `ffmpeg` 6.1.1 under `gdb` with a breakpoint on `xcb_connect`: for `-i +0,0:9188` the first argument is a null pointer; for the control `-i :9188+0,0` it is the string `:9188` [MEASURED].
- `strace` on the same two inputs: the control tries `/tmp/.X11-unix/X9188`; the leading-offset form makes 0 connection attempts to it [MEASURED].
- A null name resolving to `DISPLAY` is the documented `xcb_connect` behaviour [CITED]; it was not exercised against a live display because the brief forbids a non-empty `DISPLAY`.

Consequence. From a desktop shell where `DISPLAY=:1`, `record.sh "+0,0:2" take` exits 0 and records the reserved operator display. The same shape with ambient `DISPLAY=:0` records `:0` without `--allow-display0`. The sidecar then records a display string that is not the one captured. This needs an unusual argument, so it is less likely to be typed than `:01`, but it defeats the same "unconditional" invariant and is a small fix.

Required fix (prose). Refuse any display argument whose shape is not exactly: optional host, one colon, digits, optional dot and screen digits, optional trailing offset. In particular refuse a `+` before the colon and any character after the validated parts. As defence in depth, launch the recorder with `DISPLAY` and `WAYLAND_DISPLAY` removed from its environment so that no parsing difference can ever fall back to the ambient display.

Must-pass unit test (prose). Put a fake `ffmpeg` first on `PATH` that appends its arguments and the value of its `DISPLAY` variable to a log file and sleeps briefly. Run `record.sh` with ambient `DISPLAY` set to `:1` and each of these display arguments: `+0,0:5`, `+0,0:2`, `+10,20:7.0`, `+:5`, `+0,0:0` (the last one both with and without `--allow-display0`). Assert for each that the exit code is 2, the fake `ffmpeg` log is empty, and no output directory or PID file was created. Add one positive case, `:5+0,0`, and assert exit code 0, that the fake `ffmpeg` received exactly that argument, and that the `DISPLAY` value it logged is empty or unset.

### LIVE_PATH_MAJOR

**N2 — With the sidecar timeline, broker state from earlier runs in `events.jsonl` is carried into the new video.** `swarm/overlay/compositor.py:577-581`, `swarm/overlay/compositor.py:487-517`, `swarm/overlay/compositor.py:1132`

Failure mechanism. The M1 fix correctly anchors the window to the current take, but the tracker is still built from every record in the append-only log (line 1132). On the first frame `advance_to` applies all records older than the window (lines 577-581), and `evaluate_agent_cursor` takes the latest cursor record at or before the frame time, however old (line 487). A new broker process starts with empty queues and no parks (`broker.py:345-362`) and writes no run-boundary record, so nothing ever clears what the earlier run left behind.

Evidence [MEASURED]. Log: an earlier run ending with two unclosed `props-agent` enqueues, a `look-agent` cursor in phase `act`, a `camera-agent` timeout park and an operator `stop`; then a new run one hour later with 20 `camera-agent` turns; sidecar at the new run. At the last frame of the new video the tracker still reports `camera-agent` parked while it is the active head, `props-agent` with 2 queued events, control state `stop`, and the `look-agent` cursor shown as `[ACTING]` at its old position for the whole video. At the first frame the strip also shows the old run's turn 80 and its `TURN_TIMEOUT` verdict.

Live-path exposure. The real log currently ends clean (no parks, empty queues, no control state, last verdict `INPUT_UNCERTAIN`, two cursors in `result`) [MEASURED]. A take appended to it would open with the old `TURN #32`, the old red verdict pill and two stale result cursors until the new run overwrites them. A prior run that ended with a park, a `stop` or pending events gives the permanent errors above.

Operational workaround. Start each take with a fresh events log (rotate the file or set `SWARM_EVENTS_LOG`), in which case this defect cannot occur. With that discipline in the demo runbook, N2 does not block; without it, it does.

Required fix (prose). When the timeline source is the sidecar or an explicit start, records older than the window start must not contribute state: either ignore them, or reset queue, park, control, verdict and cursor state at the window start. A run-boundary record from the broker would be the cleaner long-term signal and needs a contract amendment.

Must-pass unit test (prose). Build a log with an earlier run that leaves one agent timeout-parked, two unclosed enqueues for a second agent, a `stop` control record, and an `act` cursor for a third agent. Append a later run, at least ten minutes after, containing only turns for the parked agent. Supply a sidecar whose start equals the later run's first timestamp. Evaluate the tracker at the window start and at the window end and assert: no parked agents, all queue counts zero, no control state, no verdict from the earlier run, the turn number never exceeds the later run's highest turn, and the third agent's cursor is `idle` at its default slot.

### BACKLOG

Each item has a file and line, the mechanism, and the test that should pass once fixed. None gates.

| ID | Location | Mechanism | Must-pass test (prose) |
|---|---|---|---|
| K1 | `compositor.py:440-450` | With no sidecar and a multi-run log, `auto` mode still stretches a `span` over all history (64.9 min over a 10 s video [MEASURED]); the only signal is one stderr line. A raw video that was copied or renamed loses its sidecar and lands here | Multi-run log, no sidecar, `auto` mode: assert that composition either fails with a clear message or reports a warning field in the summary, and never silently returns `span` when the event span exceeds the video duration by a large factor |
| K2 | `compositor.py:656` | An operator park carrying both `agent_id` and `event_id` is ignored, but the broker parks the agent for any targeted park (`broker.py:790-792`) [MEASURED] | Apply a director park with both fields and a non-timeout reason; assert the agent is in the parked set |
| K3 | `record.sh:82-83`, `record.sh:95` | Two takes with the same name in the same second share one output path; `-y` overwrites the video and the sidecar (observed during the probe matrix [MEASURED]) | With a fake `ffmpeg`, start two takes with the same name within one second; assert two distinct output paths and two sidecars, or a refusal of the second |
| K4 | `record.sh:109-124` | PID and path files are written before the liveness check and are not removed when `ffmpeg` dies at once. A dead display gave rc 1 and left `/tmp/swarm_record_<name>.pid` pointing at a dead PID [MEASURED]; a later `kill -INT` on it could hit a recycled PID | With a fake `ffmpeg` that exits 1 immediately, assert rc 1 and that no PID or path file for that name remains |
| K5 | `record.sh:119-128` | The liveness window is 0.15 s; an `ffmpeg` that fails later is reported as a success. If the sidecar step fails, the script exits non-zero without printing the PID, leaving a running recorder nobody can address | Fake `ffmpeg` that exits 1 after 0.5 s: assert the failure is reported. Fake `python3` that fails: assert the recorder is stopped or its PID is still printed |
| K6 | `compositor.py:336`, `505-506`, `597`, `610-612`, `625` | Non-strict loading skips a torn line, but single bad values still abort the whole composition: an infinite `ts_ms` raises `OverflowError`, a null cursor `x` raises `TypeError`, a non-numeric `turn` or `drops` raises `ValueError` [MEASURED] | One valid record followed by each of these malformed records in turn, non-strict mode: assert loading, tracker replay and cursor evaluation finish without an exception and the bad record is skipped |
| K7 | `compositor.py:417-424` | Any numeric sidecar start is accepted, including 0, a negative number or a value in seconds; the overlay then sits in its first or last state for the whole video with no warning | Sidecar start of 0 with a normal log: assert a refusal or a warning in the summary |
| K8 | `record.sh:94` | The wall-clock anchor is sampled before `ffmpeg` starts, so the sidecar runs early by the capture start-up latency (not measured) | Fake `ffmpeg` that records its own start time: assert the sidecar start is within an agreed tolerance of it |
| K9 | `record.sh:48`, `record.sh:60` | In this host's locale the digit check accepts a non-ASCII digit, and the arithmetic then fails with rc 1 instead of rc 2. No `ffmpeg` is started, so it fails closed [MEASURED] | Display `:` followed by an Arabic-Indic digit: assert rc 2 and the invalid-display message |
| K10 | `compositor.py:503-517` | Cursor evaluation walks every past cursor record for each agent on every frame; cost grows with the whole append-only log, not with the take | Log with 20 000 earlier cursor records and a 10 s take: assert composition time stays within a small factor of the same take with an empty history |
| K11 | `compose.sh:31-37` | Extra flags are appended after the wrapper's own, so a second `--output` redirects the compositor while the wrapper verifies the original path | Pass an extra `--output` to a different path: assert the wrapper refuses it or verifies the file that was actually written |

---

## Disposition

| Severity | Count | IDs |
|---|---|---|
| BLOCKER | 1 | N1 |
| LIVE_PATH_MAJOR | 1 | N2 (workaround: fresh events log per take) |
| BACKLOG | 11 | K1-K11 |

Re-review needs N1 fixed with its negative-control test. N2 should either be fixed in the same pass or be covered by an explicit "fresh events log per take" step in the demo runbook.
