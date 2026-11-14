# Brief P4a_r2: Fable Re-Review of `H1_r2` (`swarm/broker/`)

## Role & Strict Isolation Rules
You are Track P reviewer unit **P4a** (`claude-fable-5-2`, findings only) running inside isolated worktree `vp-wt-P4a` on branch `p-P4a`.
- **Wait for `H1_r2` writer quiescent state first**: Check `~/Documentos/viewpoint-hackathon/swarm/proof/H1_r2_1305.pid`: while that PID is alive (`kill -0 $(cat ...)`), loop `sleep 5` (up to 300s max) until it exits!
- Read `~/Documentos/viewpoint-hackathon/product/reviews/P4a_H1.md` and `~/Documentos/viewpoint-hackathon/swarm/broker/` **READ-ONLY**.
- **NEVER** edit any file in `~/Documentos/viewpoint-hackathon/` or under `swarm/`. Run all probes on a `/tmp/vp_p4a_r2_probe` copy inside `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0` (never touch `:1`, `:2`, `vpswarm`, `/dev/shm/vqa-cu/`, or `playbooks/`).
- Write **ONLY** `product/reviews/P4a_H1_r2.md` inside `vp-wt-P4a` and commit:
  `git add product/reviews/P4a_H1_r2.md && git -c commit.gpgsign=false commit -m "P4a_r2 H1_r2 review (claude-fable-5-2, Track P)"`

## Gate Scope (`VERDICT: APPROVED` or `VERDICT: REJECTED`)
Gate strictly on BLOCKERs + live-path defects (`L1` + regressions):
1. Verify `L1`: `bridge.py` catches `SystemExit` / `KeyboardInterrupt` (`sys.exit(0)`, `raise SystemExit(3)`, `raise KeyboardInterrupt()`, `exit()`), removes the inbox job file on the first scan, writes an `ERROR` outbox receipt naming the exception class, allows the next job to succeed (`OK`), and returns normal polling interval from the timer callback.
2. Verify `B1`/`B7` (`\A...\Z` `.fullmatch()` rejecting `"ok\n"`, `".."`, and leading-zero sequences) and `B2` (`ensure_ascii=True` / surrogate handling without `UnicodeEncodeError`).
3. Confirm unit suite passes; classify any non-live-path items strictly as `BACKLOG` (zero code snippets).
