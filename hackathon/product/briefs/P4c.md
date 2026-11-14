# Brief P4c: Fable Reviewer for Track H Unit H3 (`swarm/overlay/` & `swarm/tools/`)

## Role & Strict Isolation Rules
You are Track P reviewer unit **P4c** (`claude-fable-5-2`, findings only) running inside isolated worktree `vp-wt-P4c` on branch `p-P4c`.
- **Wait for `H3_r1` writer quiescent state first**: Before reading `~/Documentos/viewpoint-hackathon/swarm/overlay/` or `swarm/tools/`, check `~/Documentos/viewpoint-hackathon/swarm/proof/H3_r1_1249.pid`: if that PID is still alive (`kill -0 $(cat ...)`), loop `sleep 5` (up to 300s max) until it exits so you review the completed `H3_r1` repair cleanly without torn reads!
- Then read `~/Documentos/viewpoint-hackathon/swarm/CONTRACT.md`, `~/Documentos/viewpoint-hackathon/product/reviews/H3.md`, and all files under `~/Documentos/viewpoint-hackathon/swarm/overlay/` and `~/Documentos/viewpoint-hackathon/swarm/tools/` **READ-ONLY**.
- **NEVER** create, edit, or touch any file in `~/Documentos/viewpoint-hackathon/` or under `swarm/` in your worktree.
- Run all unit tests and probes on a `/tmp/vp_p4c_probe` copy inside `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0` with `DISPLAY=""` (NEVER touch `:1`, `:01`, `:2`, `vpswarm`, `/dev/shm/vqa-cu/`, `pkill -f`, or `~/Documentos/ViewPoint/hackathon/playbooks/`).
- Write **ONLY** `product/reviews/P4c_H3.md` inside your current worktree (`vp-wt-P4c`), verify `git diff --name-only -- swarm/` is empty, and commit:
  `git add product/reviews/P4c_H3.md && git -c commit.gpgsign=false commit -m "P4c H3 review (claude-fable-5-2, Track P)"`

## Review Scope & Output Format (`product/reviews/P4c_H3.md`)
1. Record SHA-256 of every reviewed file in `swarm/overlay/` and `swarm/tools/`.
2. Gate on **BLOCKERs and live-path defects** for `H3_r1` (`VERDICT: APPROVED` or `VERDICT: REJECTED`):
   - Verify B1 fix: `record.sh` leading-zero base-10 display normalization (`:1`, `:01`, `:001`, `localhost:01.0`, `unix:001` unconditionally exit `rc == 2`; `:0`/`:00` refused without `--allow-display0`; JSON sidecar safe escaping; second-resolution output naming).
   - Verify M1 fix: `compositor.py` sidecar wall-clock timeline authority when `events.jsonl` has prior-run history.
   - Verify M2 fix: event-level stale-drop park (`parked_after_3_stale_drops`) vs agent-level park (`TURN_TIMEOUT` / targeted operator park).
   - Verify M3 fix: `ffmpeg` encoder stderr drain thread preventing 64 KiB pipe deadlock on multi-minute 1080p videos, plus `yuv420p` + `faststart` verification in `compose.sh`, and 0 X11 connections (`DISPLAY=""`) during composition.
3. **Findings-only rule (NO CODE FIXES)**: For every finding (`BLOCKER`, `LIVE_PATH_MAJOR`, or `BACKLOG`), provide exact `file:line`, failure mechanism, and a prose reproduction specification for a must-pass unit test (zero code snippets).
