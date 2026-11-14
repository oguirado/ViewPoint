# Track P Review Brief `P4f` — Cross-Family Adversarial Review of `H6` (`swarm/docs/` & `README.md`)

You are Fable Reviewer `P4f` (`claude-fable-5-2`, Track P).
- **Working Directory**: `~/Documentos/vp-wt-P4f` (branch `p-P4f`).
- **Target to Inspect (Read-Only)**:
  - `~/Documentos/viewpoint-hackathon/swarm/docs/`
  - `~/Documentos/viewpoint-hackathon/README.md`
  - `~/Documentos/viewpoint-hackathon/PROVENANCE.md`
- **Output File (ONLY file you write/commit in `~/Documentos/vp-wt-P4f`)**: `product/reviews/P4f_H6.md`.
- **HARD BOUNDARIES**:
  - NEVER create, edit, or touch any file under `swarm/` or `README.md` in either `~/Documentos/viewpoint-hackathon` or `~/Documentos/vp-wt-P4f`.
  - Run all probes inside `systemd-run --user --scope -q -p MemoryHigh=4G -p MemoryMax=6G -p MemorySwapMax=0` with `PYTHONDONTWRITEBYTECODE=1`.
  - NEVER read credentials, keys, or files under `~/.gemini`.

## Gate Checklist to Probe & Report (`file:line` + reproducible command)
1. **Unit Suite**: Run `H6`'s `test_docs.py` on a probe copy and report exact count & timing `[MEASURED]`.
2. **D1 (Committed-Only Link & Citation Audit)**: Independently extract every Markdown link and repo-relative path in `swarm/docs/*.md` and `README.md` (`## Hackathon build (swarm-1114)`) and verify against `git -C ~/Documentos/viewpoint-hackathon ls-tree -r HEAD` union `~/Documentos/viewpoint-hackathon/swarm/docs/*` (and `swarm/viewer/*` if marked Wave 2 / `[TARGET]`). Flag any broken or untracked worktree citation as `LIVE_PATH`/`BLOCKER`.
3. **D2 (Independent Proof SHA-256 Re-Hash)**: Independently re-compute `sha256sum` on disk for EVERY proof file listed in `swarm/docs/PROOF.md` and `PROVENANCE.md` (`swarm/proof/*.jsonl` and `product/proof/*.jsonl`), verifying 100% match and zero missing proof rows.
4. **D3 (Anti-Overclaim & Banned-Phrase Audit)**:
   - Check that the shared-UID / memory-scope process boundary and best-effort projection caveats are stated accurately.
   - Grep `swarm/docs/*.md` and `README.md` (`## Hackathon build (swarm-1114)`) for banned phrases: `secure against agents`, `sandboxed agents`, `tamper-proof`, `authenticated agents`, `sealed-bid`, `truthful auction`, `guaranteed delivery`, `exactly-once`, `at-least-once`, `rewinds everything`, unqualified `isolated` for the Blender swarm capsule, `"in parallel"` for Blender actuation, or `"audit ok"` as standalone proof.
5. **D4 (Two-Track Integrity & Existing `README.md` Preservation)**:
   - Confirm `README.md` lines prior to `## Hackathon build (swarm-1114)` and `## License` were preserved intact without destructive edits.
   - Confirm every `swarm/` unit (`H0..H6`) is attributed to Gemini (`agy`) and every `product/` unit (`P1..P6`) + pre-wave Claude specs are declared honestly as Claude-authored.

## Verdict Format in `product/reviews/P4f_H6.md`
- List SHA-256 of every file reviewed in `swarm/docs/` plus `README.md`.
- Emit explicit `## VERDICT: APPROVED` or `## VERDICT: REJECTED` (`BLOCKER` / `LIVE_PATH` vs `BACKLOG`).
- Commit ONLY `product/reviews/P4f_H6.md` on branch `p-P4f` (`git add product/reviews/P4f_H6.md && git commit -m "P4f H6 review (claude-fable-5-2, Track P)"`).
