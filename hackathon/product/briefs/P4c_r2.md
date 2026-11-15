# Brief P4c_r2: Fable Re-Review of `H3_r2` (`swarm/overlay/` & `swarm/tools/`)

## Role & Strict Isolation Rules
You are Track P reviewer unit **P4c** (`claude-fable-5-2`, findings only) running inside isolated worktree `vp-wt-P4c` on branch `p-P4c`.
- **Wait for `H3_r2` writer quiescent state first**: Check `~/Documentos/viewpoint-hackathon/swarm/proof/H3_r2_1303.pid`: while that PID is alive (`kill -0 $(cat ...)`), loop `sleep 5` (up to 300s max) until it exits!
- Read `~/Documentos/viewpoint-hackathon/product/reviews/P4c_H3.md` and `~/Documentos/viewpoint-hackathon/swarm/{overlay,tools}/` **READ-ONLY**.
- **NEVER** edit any file in `~/Documentos/viewpoint-hackathon/` or under `swarm/`. Run all probes on a `/tmp/vp_p4c_r2_probe` copy inside `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0` with `DISPLAY=""` (or fake `ffmpeg` on `PATH` when testing `DISPLAY=:1` scrubbing; never touch real `:1`, `:2`, `vpswarm`, `/dev/shm/vqa-cu/`, or `playbooks/`).
- Write **ONLY** `product/reviews/P4c_H3_r2.md` inside `vp-wt-P4c` and commit:
  `git add product/reviews/P4c_H3_r2.md && git -c commit.gpgsign=false commit -m "P4c_r2 H3_r2 review (claude-fable-5-2, Track P)"`

## Gate Scope (`VERDICT: APPROVED` or `VERDICT: REJECTED`)
Gate strictly on BLOCKERs + live-path defects (`N1` + `N2` + regressions):
1. Verify `N1`: `record.sh` strictly rejects `+0,0:5`, `+0,0:2`, `+10,20:7.0`, `+:5`, `+0,0:0` (with and without `--allow-display0`) with `rc == 2`, zero `ffmpeg` spawns, and zero PID/output files; accepts `:5+0,0` with `DISPLAY` and `WAYLAND_DISPLAY` scrubbed from `ffmpeg`'s environment (`LC_ALL=C`).
2. Verify `N2`: when timeline is anchored by sidecar or explicit `--start-ts-ms`, `compositor.py` excludes pre-window records (`ts_ms < window_start_ts_ms`) so earlier runs leave zero stale parks, queued counts, `stop` state, verdicts, or cursors at frame 0.
3. Confirm unit suite passes; classify any non-live-path items strictly as `BACKLOG` (zero code snippets).
