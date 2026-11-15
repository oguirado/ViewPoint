# P4c Re-Review — H3_r2 (`swarm/overlay/`, `swarm/tools/`)

- **Reviewer**: `claude-fable-5-2` (Track P, unit P4c; findings only, no edits under `swarm/`)
- **Timestamp**: 2026-11-14T12:09:41Z
- **Reviewed tree**: `~/Documentos/viewpoint-hackathon` on `swarm-1114` at `f667745` (`swarm/overlay/` and `swarm/tools/` are untracked there), read after the `H3_r2` writer (PID 311687, `swarm/proof/H3_r2_1303.pid`) had exited; the PID was already dead at the first check, so no wait was needed. Hashes were re-checked after the last probe and had not changed.
- **Prior review**: `product/reviews/P4c_H3.md` (REJECTED on N1; N2 recorded as LIVE_PATH_MAJOR).
- **Test isolation**: every run on the copy `/tmp/vp_p4c_r2_probe`, inside `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0`, with `DISPLAY=""`. `record.sh` ran with `HOME` and `SWARM_RUN_DIR` pointed into the copy and a fake `ffmpeg` first on `PATH`; in those runs `DISPLAY=:1` and `WAYLAND_DISPLAY=wayland-0` were set only as environment strings to test scrubbing, and nothing connected to a display. Two real-`ffmpeg` parser checks used display `:9188` (no socket) with `DISPLAY` unset. `:1`, `:2`, `vpswarm`, `/dev/shm/vqa-cu/` and the playbooks directory were never touched; the operator's `ffmpeg … -i :1` recorders were still running afterwards.

## Files reviewed

| File | SHA-256 | Changed since r1 |
|---|---|---|
| `swarm/overlay/compositor.py` | `a9223e5b0e0ef4b9786c9db1dbf02573734eec8c7b1beb98b8d93073eabcffc0` | yes |
| `swarm/overlay/__init__.py` | `3e5eef87a7a1f61ea0bd64054dcc1ce09991a9633091ca7103e7961933d02004` | yes |
| `swarm/overlay/test_overlay.py` | `32fe45157ec7f3d16631fb8ebcac188e7427bda49cd0906523c1d548b435115a` | yes |
| `swarm/overlay/README.md` | `aea70699ce487ad37172f587910b89fa403a6e218071692467dd2f2add041ecb` | no |
| `swarm/tools/record.sh` | `e93672762cb097b5b6cc28fe80890f218829056de0c4e0177d2b11ae99899f9c` | yes |
| `swarm/tools/compose.sh` | `1364b6e559c0cdfc1f127a588a4df6f0dd081da4e487a273fa0532f037216cfd` | no |

VERDICT: APPROVED

N1 and N2 are both fixed and verified by independent probes, the unit suite passes, and no regression or new live-path defect was found. Everything else is BACKLOG.

## Gate results

| # | Gate item | Result | Evidence |
|---|---|---|---|
| 1 | N1: leading-offset and malformed display specs refused | PASS | 78 of 78 refusal cases returned `rc == 2` with 0 `ffmpeg` spawns and 0 PID/output pointer files [MEASURED] |
| 1 | N1: `:5+0,0` accepted with scrubbed environment | PASS | fake `ffmpeg` saw `DISPLAY` unset, `WAYLAND_DISPLAY` unset, `LC_ALL=C` |
| 2 | N2: pre-window records excluded when anchored | PASS | composed video is pixel-identical to a clean-log reference in sidecar and explicit modes (max difference 0 over 15 frames [MEASURED]) |
| 3 | Unit suite | PASS | 15 tests, `OK`, rc 0, 3.5 s [MEASURED] |

## N1 — verified fixed

`swarm/tools/record.sh:46-50` now anchors the display grammar as `[host]:<N>[.<S>][+<x>,<y>]`, so an offset can only trail the display number. `swarm/tools/record.sh:97` launches `ffmpeg` through `env -u DISPLAY -u WAYLAND_DISPLAY`, so even a spec that slipped past the gate could not fall back to the ambient display. `LC_ALL=C` is exported at line 7.

Probe: `record.sh` run with a fake `ffmpeg` that logs its arguments and environment, caller environment `DISPLAY=:1 WAYLAND_DISPLAY=wayland-0 LC_ALL=es_ES.UTF-8`.

| Input | Without `--allow-display0` | With `--allow-display0` |
|---|---|---|
| `+0,0:5` | rc 2, 0 spawns, 0 files | rc 2, 0 spawns, 0 files |
| `+0,0:2` | rc 2, 0 spawns, 0 files | rc 2, 0 spawns, 0 files |
| `+10,20:7.0` | rc 2, 0 spawns, 0 files | rc 2, 0 spawns, 0 files |
| `+:5` | rc 2, 0 spawns, 0 files | rc 2, 0 spawns, 0 files |
| `+0,0:0` | rc 2, 0 spawns, 0 files | rc 2, 0 spawns, 0 files |
| `:5+0,0` | rc 0, 1 spawn, `-i :5+0,0`, `DISPLAY` and `WAYLAND_DISPLAY` unset, `LC_ALL=C` | same |

"Files" covers the `/tmp` and `SWARM_RUN_DIR` PID/outpath pointers; for the five required inputs the isolated `HOME` also stayed empty (no video directory, no sidecar).

Additional refusals, each tried with and without the flag, all `rc == 2` with 0 spawns and 0 pointer files: `+0,0`, `+0,0:1`, `:5+0,0:1`, `:5+0,0+1,1`, trailing and leading space, `:5+0`, `:5+,0`, `:5+-1,0`, `:5.+0,0`, empty string, `:`, a full-width digit, `:5:1`, `5`, `:+5`, embedded newline (including a `:5`⏎`:1` pair), `:5.0.0`, `a/b:5`, `:5,0`, `::5`, `:0x1`, `:1e0`, and the display-1 aliases `:1+0,0`, `:01`, a 20-digit zero-padded 1, `unix:1`, `localhost:1.3`, `:1.9`, plus the oversized numbers `:4294967297` and `:18446744073709551617`. `:0` and `:0+0,0` are refused without the flag and accepted with it, as designed.

Other accepted forms behaved the same way as `:5+0,0` (scrubbed environment, argument passed through unchanged): `:5`, `:99.0+10,20`, `unix:7`.

Real `ffmpeg` 6.1.1 with `DISPLAY` unset fails to open both `:9188+0,0` and `+0,0:9188` (input/output error), which confirms the scrub leaves no ambient fallback.

One note on my own probe: its first pass listed `--allow-display0 :0` and `--allow-display0 :0+0,0` under "expect refusal". They were correctly accepted; the expectation was wrong, not the script. The leftover files from those two runs made the probe's home-directory counter read non-zero for a few later display-1 cases, so for those the evidence is rc 2, 0 spawns and 0 pointer files, and the refusal message itself.

## N2 — verified fixed

`swarm/overlay/compositor.py:471-480` drops every record with `ts_ms` below the window start when the timeline source is `sidecar`, `sidecar_empty` or `explicit`, and `compose_video` builds the tracker from that filtered list (`swarm/overlay/compositor.py:1153-1154`). Cursor streams are partitioned from the same filtered list, so cursors are covered too.

Probe: six takes composed through `swarm/tools/compose.sh` on a 640x360, 15-frame synthetic video. The "prior run" block holds 16 records: four claims, a turn, a PASS close, a `TURN_TIMEOUT` close, a broker park, three enqueues (two left unclosed), an acting cursor for every agent, and a director stop.

| Take | Events log | Anchor | Records kept | Versus clean reference |
|---|---|---|---|---|
| reference | new run only (4 records) | sidecar | 4 of 4 | — |
| stale, sidecar | prior run 10 min earlier + new run | sidecar | 4 of 20 | identical, max pixel difference 0 |
| stale, explicit | same | `--start-ts-ms` | 4 of 20 | identical, max pixel difference 0 |
| boundary | prior run ending 1 ms before window start | sidecar | 4 of 20 | identical, max pixel difference 0 |
| control: prior block inside window | prior block starting at window start | sidecar | 20 of 20 | differs (max 255, frame 0 differs by 64) |
| control: unanchored | prior block 5 s earlier, `--timeline-mode span` | none | 20 of 20 | differs (max 255, frame 0 differs by 73) |

The two controls show the comparison can fail, so the three zero-difference results are meaningful.

State at frame 0 was also checked directly for the stale and boundary logs in all three anchored modes: no parked agents, all queues empty, no control state, no last verdict, no last PASS, no close records, turn 0, tick 0, no active head, and every agent cursor idle at its home slot. The same logs under `mtime` or `span` (unanchored) still show the parked camera agent, 3 queued events and the stop, which is the behaviour the gate scopes out.

## Regressions

None found. The suite covers the fixes verified in r1 (display-1 and display-0 refusal, event park versus agent park, encoder stderr drain, sidecar JSON escaping, pointer-file cleanup on immediate `ffmpeg` exit) and all 15 tests pass. `compose.sh` and the README are byte-identical to r1. The positive `record.sh` paths still write the sidecar with the correct `start_ts_ms`, display and escaped output path.

## BACKLOG

None of these gate the verdict.

- **K1 — README does not describe the pre-window exclusion.** `swarm/overlay/README.md` is unchanged since r1; an operator reading it will not learn that anchored timelines discard earlier records.
- **K2 — Legitimate state from before the window is discarded too.** If recording starts after the broker run has begun, the turn in progress, existing queue entries, parks and claims are not shown until new records arrive. The runbook should start the recorder before the broker run.
- **K3 — Unanchored modes still inherit prior-run state.** `mtime` and `span`, including `--timeline-mode span` when a sidecar exists, apply no cutoff. This only matters for takes not made with `record.sh` or composed with the span override.
- **K4 — The N2 unit test asserts tracker state, not rendered pixels.** The pixel comparison in this review is not part of the suite.
- **K5 — Pointer and log files live at fixed `/tmp` paths.** The suite leaves `swarm_record_h3_esc_*.ffmpeg.log` files in `/tmp` after each run, and two takes with the same name share one set of pointers.
- **K6 — Output name uses a second-resolution time stamp.** Two takes with the same name in the same second target one file, and the recorder overwrites.
- **K7 — Remote display hosts are accepted.** `host:N` passes the grammar when `N` is neither 0 nor 1; the demo only needs local displays.
- **K8 — `start_ts_ms` is sampled before `ffmpeg` starts.** Any grab start-up delay becomes a fixed overlay offset; it was not measured here.
- **K9 — A bare `--` separator is refused as an unknown flag.** Harmless, but unconventional.

## Disposition

| Class | Count | Items |
|---|---|---|
| BLOCKER | 0 | N1 closed |
| LIVE_PATH_MAJOR | 0 | N2 closed |
| BACKLOG | 9 | K1–K9 |

Not covered: no real X11 capture was made (fake `ffmpeg` only, by the isolation rules), so actual grab output on a live display is untested here.
