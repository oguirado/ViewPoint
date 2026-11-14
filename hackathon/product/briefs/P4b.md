# Brief P4b: Fable Reviewer for Track H Unit H2 (`swarm/agents/`)

## Role & Strict Isolation Rules
You are Track P reviewer unit **P4b** (`claude-fable-5-2`, findings only) running inside isolated worktree `vp-wt-P4b` on branch `p-P4b`.
- Read `~/Documentos/viewpoint-hackathon/swarm/CONTRACT.md`, `~/Documentos/viewpoint-hackathon/product/reviews/H2.md`, and all files under `~/Documentos/viewpoint-hackathon/swarm/agents/` (plus `~/Documentos/viewpoint-hackathon/swarm/broker/broker.py` for H1<->H2 seam verification) **READ-ONLY**.
- **NEVER** create, edit, or touch any file in `~/Documentos/viewpoint-hackathon/` or under `swarm/` in your worktree.
- Run all unit tests and adversarial probes on a `/tmp/vp_p4b_probe` copy inside `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0` (NEVER touch `/dev/shm/vqa-cu/`, display `:1`/`:2`, `vpswarm`, `pkill -f`, or `~/Documentos/ViewPoint/hackathon/playbooks/`).
- Write **ONLY** `product/reviews/P4b_H2.md` inside your current worktree (`vp-wt-P4b`), verify `git diff --name-only -- swarm/` is empty, and commit:
  `git add product/reviews/P4b_H2.md && git -c commit.gpgsign=false commit -m "P4b H2 review (claude-fable-5-2, Track P)"`

## Review Scope & Output Format (`product/reviews/P4b_H2.md`)
1. Record SHA-256 of every reviewed file in `~/Documentos/viewpoint-hackathon/swarm/agents/`.
2. Gate on **BLOCKERs and live-path defects** for `H2_r1` (`VERDICT: APPROVED` or `VERDICT: REJECTED`):
   - Verify B1 fix: `wait-event` byte-offset / persistent append-only `events.jsonl` handling (pre-existing events from prior runs, partial/torn UTF-8 tail lines, inode rotation or truncation safety).
   - Verify M1 fix: `runner.py` true `8 GiB` cgroup scope / `agy` launch wrapper so child `agy` shims cannot escape memory bounds, and `-p` is strictly the LAST flag on `agy` CLI (`--model gemini-4-hydrogen-high`, stream output to `swarm/proof/run_<HHMM>_<agent_id>.jsonl`).
   - Verify byte-identical shared brief prefix across all 4 agents (`modeler-agent`, `props-agent`, `look-agent`, `camera-agent`), zero `vision` CLI calls by agents, claim/lease handshake before script/camera intents, `camera-agent` numpad orbit key lists (`KP_1`, `KP_3`, `KP_4`, `KP_Add`, `Home`), and never auto-repeating `INPUT_UNCERTAIN` / `OUTCOME_UNCERTAIN`.
   - Verify `H2 <-> H1` intent JSON schema and `event_id` regex compatibility (`fullmatch` vs trailing newline).
3. **Findings-only rule (NO CODE FIXES)**: For every finding (`BLOCKER`, `LIVE_PATH_MAJOR`, or `BACKLOG`), provide exact `file:line`, failure mechanism, and a prose reproduction specification for a must-pass unit test (zero code snippets).
