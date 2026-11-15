# Computer Use (cubook) — W4 operator page (campaign os-cu-0, wave 4)

> Release note (PUB): bare names (`gates.sh`, `harvest.sh`, `labels_v3.py`, `livedriver.go`, ...) resolve inside this repo; `C/w4/slive/run_slive4.sh`, `README_slive4.md`, `SYNC_VQA.md` and `w4/PREREG_WL_V3.json` are conductor-side campaign files and are NOT tracked here. `scripts/cu/mutants.json` holds 312 mutants at HEAD (303 at the W4 close, +9 in W5a).


Pin: `ve-sealed-1-law4` (wheel `5e2395d5…a4a5`, witness `equal:5e2395d5`), unchanged. Writers V, B, M, R7 landed at `1e13b7b`; this INT room adds mutants, gates, the S-LIVE v4 runner and docs.
Gates: `taskset -c 10-14 timeout 900 bash scripts/cu/gates.sh` (rc 0 = every gate + every mutant KILLED, 0 INVALID); `bash scripts/cu/run_suite.sh`. Nothing here was run live: S-LIVE v4 is a later conductor step and every live value stays [H] (`docs/CU_KNOWN_LIMITATIONS.md` KL-30, KL-31, KL-32).

## What W4 adds
| lane | change | where | limits |
|---|---|---|---|
| V | prereg v3 (`os.cu.prereg-wl/v3`, order WL-1s, WL-1c, WL-1L, WL-1h, WL-4, WL-3, 1920x1080, 60 fps, 6000 ms each, `xvfb_cursor 0`), scenes `bench/wl/scenes/v3/*.html` (lowercase names, no `Math.random`/`Date`/`performance.now`/network), `labels_v3.py` (true dx/dy on the actuator `t_us` timebase), `wlplan_v3.go` (ops `hscroll`, `key` PageDown, `drag` = press/move/release, assigned to the `parseV3Step`/`fireV3Step` hooks in `init()`) | `bench/wl/**`, `src/cubook/exec/wlplan_v3.go` | WL-7 stays on prereg v2; rAF-to-capture skew unmeasured |
| B | `srf-cu-wlcap --mode bench` (receipt `os.cu.wlcap-bench/v1`; no START, handover, keep or analysis; `--keep` / `--analyze` rc 2), START carries `run_nonce` | `src/cubook/capture/livedriver.go`, `cmd/srf-cu-wlcap`, `bench/cu/pace4k/plan.json` v2 | no 4K60 claim without a measured cell |
| M | one marker reader on both sides (NOFOLLOW, NONBLOCK, CLOEXEC, S_ISREG, nlink 1, caps), `run_nonce` + chunk fencing, stale ANALYZED refused, START reader rc 5, `InScreen`/`InRect` int64 bounds vs the START (w,h), RD3-03 `--run-prefix` scoping | `analyzer.go`, `bench/wl/driver.py`, `cmd/srf-cu-exec/wl.go`, `src/cubook/exec/{wlplan,bounds}.go`, `scripts/cu/g_disk_frames.py` | nonce enforced only when the run has one |
| R7 | `docker/cu/diag/harvest.sh` (before every `docker rm`, idempotent `.harvested`, <= 100 KB), `capsule-diag.sh` (+ `--classify RC`), launcher v2+v3 scene roots and raw chromium rc side file, `bench/cu/wl7_diag/plan.json` | `docker/cu/**`, `bench/cu/wl7_diag` | WL-7 cause still open |
| INT | 292 mutants (254 + 38; 7 INVALID ids re-anchored; 303 after INT2, see FX1), `gates.sh` runs the W4 suites, `g_disk_frames.py` `check_build` skips foreign `build/room_*`, runner `run_slive4.sh` | `scripts/cu/**`, `docs/**` | |

## Pre-registration (journalled before any W4 capture)
- v1 `505aac99904e99e15dbba13242dd8c2731bf803259bb39d80deb684bd934cd58` and v2 `b2f585b89aec55ffded950614a180c66e42b6ab0b2a2725a4c9f1f6333cc2a12` are frozen (files untouched).
- v3 `bench/wl/prereg_wl_v3.json`: canonical `15be6e44b486a310a429ac42c902f7f9ba0d7ae5ea98a7471e4e7c3fb4e8cb20` (= `labels_v3.PINNED_PREREG_V3_SHA256`, the driver's default want for v3; `python3 bench/wl/labels_v3.py validate bench/wl/prereg_wl_v3.json`).

## S-LIVE v4 (conductor only; `C/w4/slive/run_slive4.sh`, `README_slive4.md`)
One item per invocation: `STEPS=wl7diag` (non-kept) -> batch A `wl1s`, `wl1c`, `wl1L`, `wl1h` (kept, 4 x 2400 MiB = 9600 MiB <= 10 GiB) -> handover/ACK/release -> batch B `wl4`, `wl3` (+ `wl7` if cursor-on rendered) -> `pace4k` (bench A/B, receipts only). `--quota-bytes 2516582400` (integer bytes). No `docker run` before visual-qa-0 replies OK.

## FX1 (judge JW4-01..06 + co-sign ADC-W4-01..03, OS `e6d34e6`) and INT2
| id | closure | where |
|---|---|---|
| JW4-01 | runner refuses rc 5 before any v3 capture unless `SYNC_VQA.md` holds the canonical and file sha and `w4/PREREG_WL_V3.json` is byte-identical to `bench/wl/prereg_wl_v3.json` (conductor runner, `test_diag.sh` `runner_prereg_*`, `test_runner_v4_patch_markers`) | C `w4/slive/run_slive4.sh` |
| JW4-02 / ADC-W4-03 | `harvest.sh` budget = 100 KB - 2 KB - bytes already in OUTDIR, `CU_HARVEST_SUB`, diag tails 8 KiB, `.harvested` checked before `docker inspect` | `docker/cu/diag/harvest.sh`, `docker/cu/tests/test_diag.sh` |
| JW4-03 | RECORDED: INT's W4 commit `263d542` edited `scripts/cu/g_disk_frames.py` (`check_build`, lane M's file; surface extension by INT). ADC-W4-01 then re-cut it in X2 (single `check_disk(build)` scan, foreign `room_*` results filtered and counted). No revert. Tests: `Build.test_gdf_build_*`, `test_gdf_check_build_skips_foreign_rooms`, `test_gdf_fails_own_frames` | `scripts/cu/g_disk_frames.py` |
| JW4-04 | `CPUSET` accepts `10-14` only (`case "$CPUSET" in 10-14)`), asserted by `test_runner_v4_patch_markers` | C runner |
| JW4-05 | `capture.NewRunNonce` is a var; empty nonce -> rc 5 `E_GATE run_nonce` in `srf-cu-wlcap` and `capture.Run`; `HostAnalyzer` shares START's nonce | `cmd/srf-cu-wlcap/main.go`, `livedriver.go` |
| JW4-06 | `InRect` runs on `pane_rect`, `div_rect0` and the drag-translated div rect in `LoadWLItem` and `CheckBounds` (rc 5) | `src/cubook/exec/wlplan{,_v3}.go` |
| ADC-W4-02 | `_analyze_chunk` nonce comparison unconditional; `_put_marker` uses `os.fchmod` on the fd | `bench/wl/driver.py` |

INT2: 303 mutants (292 + 11 new `os.x1.*` / `os.x2.*`; `os.r7.idem.R1`, `os.r7.cap.R2` re-anchored on the moved `harvest.sh` text); `gates.sh` adds `wl-driver-fx1`, `go-fx1-capture`, `go-fx1-exec`. Runner-side checks (prereg refusal, cpuset, diag tail size) live in C, outside the mutant copy, so they are covered by `test_diag.sh` `runner_*` and `test_runner_v4_patch_markers`, not by `mutants.json`. The prereg v3 sha stays `15be6e44b486a310a429ac42c902f7f9ba0d7ae5ea98a7471e4e7c3fb4e8cb20` (file sha `b8fb0812…0cb4`).
