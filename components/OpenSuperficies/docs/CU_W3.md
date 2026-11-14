# Computer Use (cubook) — W3 operator page (campaign os-cu-0, wave 3)

Pin: `ve-sealed-1-law4` (wheel `5e2395d5…a4a5`, witness `equal:5e2395d5`), unchanged from W2 (`docs/CU_W2.md`). Landed at `1c86a87` (lanes E, P, W, C) plus this INT room.
Gates: `taskset -c 0,1 timeout 900 bash scripts/cu/gates.sh` (rc 0 = every gate + every mutant KILLED, 0 INVALID); `bash scripts/cu/run_suite.sh`. Nothing here was run live: S-LIVE v3 is a later conductor step.

## What W3 adds
| lane | change | where | limits |
|---|---|---|---|
| E | `--analyze off\|engine` (default off; required for 4K `live` rows-only). The pinned law4 engine classifies each chunk before it is released (one worker K=1; queue 1, or one slot per chunk on a kept run so the pacer never blocks; a failed analysis still releases the chunk and ends rc 5 `analyze_failed`); fills `analyze_us_per_frame`, `grid`, `class_counts`, `engine_wheel_sha8` per chunk row. `--analyze-script` overrides the tool path. | `src/cubook/capture/analyzer.go`, `vision/chunk_analyze.py` | KL-20 closed on fakes only; ER-012 open |
| P | receipt `span_ms` (published slots × Δ, tail gaps included) and `longest_run_ms`; `segment_ms` keeps the old meaning = longest gap-free run. `--require-continuous` (default on `wl`, off `handover-1s`) → rc 5 `segment_gappy`. `--src readat\|mmap\|xshm` (`xshm` rc 5 `xshm_unavailable` without an injected getter). Chunk dirs `0o770`, files `0o660`, no `Chmod`; entrypoint `umask 027`; `docker/cu/exec-umask.sh` for `docker exec`. `XVFB_CURSOR=on\|off` (cursor only for WL-7). | `src/cubook/capture/livedriver.go`, `fbsrc.go`, `cmd/srf-cu-wlcap/main.go`, `docker/cu/entrypoint-cu.sh`, `bench/cu/pace4k/plan.json` | no 4K 60 fps claim (KL-24); KL-22, KL-25 |
| W | prereg v2 (`os.cu.prereg-wl/v2`, order WL-2b, WL-2, WL-1, WL-7), synthetic file:// scenes with a bundled font, `labels_v2.py` (FLICKER with measured-median `period_ms`, typing CHANGED glyph + caret MOVED dx, scroll MOVED dy + exposed strip, frame = floor(t·60/1e6)), `driver.py` v2 + `--require-continuous` / `--analyze` passthrough. | `bench/wl/**` | KL-23, ER-014 |
| C | Chromium launcher: `file:///opt/cu/scenes/v2/<name>.html` only (realpath inside the root), no `--remote-debugging-*`, tmpfs profile removed by trap, `umask 027`, det flags, closed `--sandbox-mode namespace\|suid\|nosandbox` (default `nosandbox` [H], receipt `sandbox: none`), runargs allow-lists `XVFB_CURSOR`. | `docker/cu/launch-chromium-cu.sh`, `docker/cu/profiles/chromium.flags`, `src/cubook/capture/runargs.go` | KL-21 |

## Pre-registration (journalled before any W3 capture)
- v1 `bench/wl/prereg_wl.json`: canonical `505aac99904e99e15dbba13242dd8c2731bf803259bb39d80deb684bd934cd58` (FROZEN, file sha `4d9262f9…0f0b`).
- v2 `bench/wl/prereg_wl_v2.json`: canonical `b2f585b89aec55ffded950614a180c66e42b6ab0b2a2725a4c9f1f6333cc2a12` (`python3 bench/wl/labels_v2.py validate bench/wl/prereg_wl_v2.json`), file sha `e9ef6aad5c34fa76ce85aba3d6aad99cda45ca31e159377423cb3d6a4534e201`, `parent_prereg` = the v1 sha. The conductor journals it in SYNC_VQA and copies it to C `w3/PREREG_WL_V2.json` (present, same file sha).
- WL-2b pre-registers `period_ms 500`; the measured period goes to the receipt only. The engine reports that caret as CHANGED (ER-014); the label stays FLICKER.

## S-LIVE v3 prerequisites (conductor only; none are satisfied by this commit alone)
1. W3 committed and `gates.sh` rc 0; prereg v2 sha journalled in SYNC_VQA; `vision/build_wheel.py --check` = `equal:5e2395d5`.
2. Host: `MemAvailable` ≥ 16 GiB, `/dev/shm` ≥ 8 GiB, `PARADA.txt` LIBRE, `ramws_shim.py sweep` rc 0, sessions with `--share-uid 10001` (the open line must carry `share`).
3. RESOURCE reply OK from visual-qa-0 before any `docker run`; image `opensuperficies-desktop-arm64:1.0.0` (no build), uid 10001 without `--user`, `--network none --read-only --cap-drop ALL`, no-new-privileges, `--cpuset-cpus 10-14` (`RunConfig.CPUSet`, allow-list `0,1` / `10-14`; FX2 CF-03).
4. Staging into ToolsDir (`/opt/cu`, read-only): `srf-cu-snap`, `srf-cu-wlcap`, `srf-cu-exec`, `launch-chromium-cu.sh`, `profiles/chromium.flags`, `exec-umask.sh`, `scenes/v2` (with `fonts/fonts.conf`). `RunArgs` rejects `-v`; do not add a mount. Every `docker exec` goes through `exec-umask.sh`.
5. Asserts added to W2 a-f: g capsule `umask` 0027 and chunk files `&0o007==0`; h host unlinks a released chunk through the default ACL (KL-25).
6. Smoke order: `--sandbox-mode` namespace → suid → nosandbox, first that renders (KL-21); `xdpyinfo -ext MIT-SHM` logged (KL-22).
7. Memory: WL-1 three speeds in one run; each kept 1080p run ≤ 2400 MiB; kept total ≤ 10 GiB (< 16 GiB host max); only kept runs survive the trap; kept runs end on ACK or the 2 h TTL.
8. WL-7 needs a cursor-on Xvfb (`XVFB_CURSOR=on`, second capsule start, the only cursor-on run); 4K A/B per `bench/cu/pace4k/plan.json` (3 reps × 1 s, cpus 10-14, `--analyze off`); baseline [M] 33/60 readat det on.
9. Any assert FAIL → rc 5, teardown, no further item.

## Engine requests
ER-012 OPEN (stable per-chunk entry point), ER-014 ACCEPTED (vision-v2 track B). ER-013 WITHDRAWN (already covered by ER-006).

## FX1 (fix round 1, commit 1412a5d) and INT2

| finding | what is enforced | gate |
|---|---|---|
| JW3-01 | kept run with `--analyze engine`: queue `ceil(Frames/ChunkFrames)+1`, pacer never blocked | `os.p.keepq.P8` |
| JW3-02, ADC-W3-01 | `RAMWS_ROOT` containment tested alone; `frames.rgb24` must be a regular non-symlink file (O_NOFOLLOW, mapped through `/proc/self/fd`) | `os.e.root.E7`, `os.e.frames.E8` |
| JW3-03 | receipt `status` ok / segment_gappy / segment_short | `os.p.status.P9` |
| ADC-W3-02..04, 06, 08 | kept 1080p quota 2400 MiB (refuse more, plan <= 10 GiB), closed-schema ints, negative `dy` rect, umask 027 + 0o640/0o750, resolutions from the prereg | `os.w.*.W5..W11` |
| ADC-W3-05, 06 | launcher ERR trap rc 5, extra-token/CDP refusal, random profile name, `exec-umask` rc 1 -> 5, `srf-cu-snap` umask | `os.c.*.F1..F6` |
| ADC-W3-07, 09 | chunk index check, XWD header cached on frame 0, `Close()` | `os.e.release.E1`, `os.e.carrier.E2`, `os.p.close.P10` |
| ADC-W3-10 | `g_disk_size.py`: every episode dir under `--out` <= 100 KB (`episode_bytes`) | `scripts/cu/tests/test_gates_w3_fx1.py` |

`os.e.release.E1`, `os.e.carrier.E2`, `os.c.umask.C3` keep their ids but are re-anchored to current code (the Offload methods are gone).

## FX2 (fix round 2, commit 7f782be) and INT3

| item | what is enforced | gate |
|---|---|---|
| RJ3-01 / ADC2-W3-01 | `closeJobs := sync.OnceFunc(close(jobs))`; every abort path releases and accounts all chunks; two probes | `os.p.closeonce.P11`, `os.p.relchk.P12` |
| CF-02 | `srf-cu-wlcap --analyze host --analyze-wait-ms N`: `chunk_<k>/READY` -> host `ANALYZED` / `ANALYZE_FAILED`; timeout = row `analyze_timeout`, no stall. Host side: `driver.py --analyze host --analyze-timeout S` runs `vision/chunk_analyze.py` with the pinned wheel | `os.p.host*.P17..P23`, `os.h.*.H3,H4,H7,H10,H11` |
| CF-01 | `<run>/START` (`os.cu.wlcap-start/v1`, CLOCK_MONOTONIC `t0_ns`); `srf-cu-exec run` fires events at `t0 + t_ms` and writes `os.cu.wl-act-step/v1`; labels from the AJSON. INT3: a v2 item with actions without `--exec` is rc 2 | `os.p.start*.P14..P16`, `os.x.*`, `os.h.start.H5`, `os.h.actlab.H6`, `os.i.execreq.I4` |
| CF-03 | `RunConfig.CPUSet` allow-list | `os.r.cpu*.R1..R4` |
| ADC2-W3-02 | `frames.raw`, `frames.linear.raw`, `raw.json` 0o640 + fchmod | `os.r.mode.R5`, `ringchmod.R6`, `jsonchmod.R7`, `linchmod.R8` |
| ADC2-W3-03 | `--quota-bytes` 1..2400 MiB, plan total on the effective quota | `os.h.quota.H1`, `os.h.effq.H2` |
| ADC2-W3-04 | scroll `dy` != 0, `cursor_px`, `caret_rect` required by item kind | `os.x.dy0.X1`, `cursorpx.X2`, `caret.X3` |
| ADC2-W3-05 | `g_disk_size.py` covers `bin/` and top-level files; `gates.sh` fills and asserts `build/gate_disk/smoke` | `os.i.diskbin.I2`, `os.i.disktop.I3`, `os.i.diskbinrm.I5` |
| ADC2-W3-06 | `XshmSource.Close` test (overlay fixture) | `os.i.xshmclose.I1` |

S-LIVE v3 runner (conductor, `w3/slive/run_slive3.sh`) follows the FX2 design: capsule `--cpuset-cpus 10-14`, `wlcap --analyze host` with the host driver analysing through the pinned wheel (no engine and no numpy in the capsule, KL-28), `srf-cu-exec` in the capsule started from START via `exec-umask.sh`, kept quotas <= 2400 MiB with a sum <= 10 GiB (live kept sessions of earlier invocations count), cursor-on only for WL-7, per-run evidence ACL removed at teardown, FIFO TTL writer for kept sessions. Live items stay [H] (KL-28).
