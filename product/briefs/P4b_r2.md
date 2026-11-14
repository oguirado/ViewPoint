# Brief P4b_r2: Fable Re-Review of `H2_r2` (`swarm/agents/`)

## Role & Strict Isolation Rules
You are Track P reviewer unit **P4b** (`claude-fable-5-2`, findings only) running inside isolated worktree `vp-wt-P4b` on branch `p-P4b`.
- Read `~/Documentos/viewpoint-hackathon/product/reviews/P4b_H2.md` and `~/Documentos/viewpoint-hackathon/swarm/agents/` **READ-ONLY** (`H2_r2` has already exited cleanly).
- **NEVER** edit any file in `~/Documentos/viewpoint-hackathon/` or under `swarm/`. Run all probes on a `/tmp/vp_p4b_r2_probe` copy inside `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0` (never touch `:1`, `:2`, `vpswarm`, `/dev/shm/vqa-cu/`, or `playbooks/`).
- Write **ONLY** `product/reviews/P4b_H2_r2.md` inside `vp-wt-P4b` and commit:
  `git add product/reviews/P4b_H2_r2.md && git -c commit.gpgsign=false commit -m "P4b_r2 H2_r2 review (claude-fable-5-2, Track P)"`

## Gate Scope (`VERDICT: APPROVED` or `VERDICT: REJECTED`)
Gate strictly on BLOCKERs + live-path defects (`L1` + `L2` + regressions):
1. Verify `L1`: `prompts.py` uses explicit absolute paths under `~/Documentos/viewpoint-hackathon/swarm/run/output/` for `.blend`, `.glb`, and `.png` deliverables (and partial-role builder settle wording), and `scripted_smoke.py` resolves relative `output_dir` against `REPO_ROOT` even when invoked from `/tmp`.
2. Verify `L2` & `K7`: `intent_cli.py` uses `\A...\Z` `.fullmatch()` and rejects `"abc\n"`, `"abc\nxyz"`, and `"a..b"` with non-zero exit and zero intent files created, matching `bridge.py`'s job filename regex.
3. Verify `K2`: binary newline framing handles mid-character multibyte UTF-8 splits without `UnicodeDecodeError`.
4. Confirm unit suite (`19/19`) passes; classify any non-live-path items strictly as `BACKLOG` (zero code snippets).
