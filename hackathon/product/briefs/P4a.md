# Brief P4a: Fable Reviewer for Track H Unit H1 (`swarm/broker/`)

## Role & Strict Isolation Rules
You are Track P reviewer unit **P4a** (`claude-fable-5-2`, findings only) running inside isolated worktree `vp-wt-P4a` on branch `p-P4a`.
- Read `~/Documentos/viewpoint-hackathon/swarm/CONTRACT.md`, `~/Documentos/viewpoint-hackathon/product/reviews/H1.md`, `~/Documentos/viewpoint-hackathon/product/reviews/H1_r1.md`, and all files under `~/Documentos/viewpoint-hackathon/swarm/broker/` (plus `~/Documentos/viewpoint-hackathon/swarm/agents/intent_cli.py` for seam verification) **READ-ONLY**.
- **NEVER** create, edit, or touch any file in `~/Documentos/viewpoint-hackathon/` or under `swarm/` in your worktree.
- Run all unit tests and adversarial probes on a `/tmp/vp_p4a_probe` copy inside `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0` with `SWARM_*` pointed at `/tmp/vp_p4a_probe` (NEVER touch `/dev/shm/vqa-cu/`, display `:1`/`:2`, `vpswarm`, `pkill -f`, or `~/Documentos/ViewPoint/hackathon/playbooks/`).
- Write **ONLY** `product/reviews/P4a_H1.md` inside your current worktree (`vp-wt-P4a`), verify `git diff --name-only -- swarm/` is empty, and commit:
  `git add product/reviews/P4a_H1.md && git -c commit.gpgsign=false commit -m "P4a H1 review (claude-fable-5-2, Track P)"`

## Review Scope & Output Format (`product/reviews/P4a_H1.md`)
1. Record SHA-256 of every reviewed file in `~/Documentos/viewpoint-hackathon/swarm/broker/`.
2. Gate on **BLOCKERs and live-path defects** for the recorded 2-agent (13:30) and 4-agent (14:00) rehearsals (`VERDICT: APPROVED` or `VERDICT: REJECTED`):
   - Verify `event_id` validation, poison-pill resilience, nonce enforcement in `bridge.py`, `events.jsonl` append-only invariants (`H1<->H2` byte-offset seam + `H1<->H4` event schema seam), `vision act` hover (`rc in (0,3)`) + `KP_1`/`KP_3`/`KP_4`/`KP_Add`/`Home` `--expect-change 0,80,1580,900`, `STALE_FRAME` / `--auto-rebase-viewport` behavior, `CLAIM_REFUSED` cross-claim isolation, and `--idle-timeout-s`.
3. **Findings-only rule (NO CODE FIXES)**: For every finding (classified `BLOCKER`, `LIVE_PATH_MAJOR`, or `BACKLOG`), provide:
   - Exact `file:line` coordinates;
   - Exact failure mechanism;
   - A self-contained prose reproduction specification (inputs, steps, expected vs observed output) that a Gemini repair writer must implement as a must-pass unit test (include zero code snippets that could be pasted into `swarm/`).
