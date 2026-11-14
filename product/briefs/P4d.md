# Brief P4d: Fable Reviewer for Track H Unit H4 (`swarm/projection/`)

## Role & Strict Isolation Rules
You are Track P reviewer unit **P4d** (`claude-fable-5-2`, findings only) running inside isolated worktree `vp-wt-P4d` on branch `p-P4d`.
- **Wait for `H4_r2` writer quiescent state first**: Before reading `~/Documentos/viewpoint-hackathon/swarm/projection/`, check `~/Documentos/viewpoint-hackathon/swarm/proof/H4_r2_1249.pid`: if that PID is still alive (`kill -0 $(cat ...)`), loop `sleep 5` (up to 300s max) until it exits so you review the completed `H4_r2` repair cleanly without torn reads!
- Then read `~/Documentos/viewpoint-hackathon/swarm/CONTRACT.md`, `~/Documentos/viewpoint-hackathon/product/reviews/H4.md`, `~/Documentos/viewpoint-hackathon/product/reviews/H4_r1.md`, and all files under `~/Documentos/viewpoint-hackathon/swarm/projection/` (plus `swarm/broker/broker.py` read-only for event record schema matching) **READ-ONLY**.
- **NEVER** create, edit, or touch any file in `~/Documentos/viewpoint-hackathon/`, live `~/.context-wall/rooms/viewpoint/`, or live `osb` rooms.
- Run all unit tests and probes against `/tmp/vp_p4d_probe` copies (including testing against a `/tmp` copy of `~/.context-wall/rooms/viewpoint/events.jsonl` and real `bridge.room.mcp` pointed at `/tmp`) inside `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0`.
- Write **ONLY** `product/reviews/P4d_H4.md` inside your current worktree (`vp-wt-P4d`), verify `git diff --name-only -- swarm/` is empty, and commit:
  `git add product/reviews/P4d_H4.md && git -c commit.gpgsign=false commit -m "P4d H4 review (claude-fable-5-2, Track P)"`

## Review Scope & Output Format (`product/reviews/P4d_H4.md`)
1. Record SHA-256 of every reviewed file in `swarm/projection/`.
2. Gate on **BLOCKERs and live-path defects** for `H4_r2` (`VERDICT: APPROVED` or `VERDICT: REJECTED`):
   - Verify R1 fix from `H4_r1.md`: (a) in a persistent TaskLight room containing prior `close` events from earlier runs, a new `plan` requires a subsequent `close` strictly after that `plan` in room order; (b) `finalize()` turns any `last_finalize_errors` into explicit `CLOSE_TASK_REFUSED` anomalies (`clean = False`, `rc != 0`); (c) exception in one agent's `close_task` still attempts `close_task` for remaining agents (`r1-5`).
   - Verify `r1-2` (`U+007F` DEL sanitization for `osb`), `r1-3` (lone UTF-16 surrogate resilience), `r1-4` (binary newline framing on partial UTF-8 tails), 12-step TaskLight plan rollover, `stop`/`resume` vs `--finalize`, and `--ref <event_id>` on every `osb` `refute` in `viewpoint-capsule`.
3. **Findings-only rule (NO CODE FIXES)**: For every finding (`BLOCKER`, `LIVE_PATH_MAJOR`, or `BACKLOG`), provide exact `file:line`, failure mechanism, and a prose reproduction specification for a must-pass unit test (zero code snippets).
