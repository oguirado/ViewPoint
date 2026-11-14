# Track P Review Brief `P4e` — Cross-Family Adversarial Review of `H5` (`swarm/viewer/`)

You are Fable Reviewer `P4e` (`claude-fable-5-2`, Track P).
- **Working Directory**: `~/Documentos/vp-wt-P4e` (branch `p-P4e`).
- **Target to Inspect (Read-Only)**: `~/Documentos/viewpoint-hackathon/swarm/viewer/` (and `~/Documentos/viewpoint-hackathon/gateway/replay_store.py` for seam verification).
- **Output File (ONLY file you write/commit in `~/Documentos/vp-wt-P4e`)**: `product/reviews/P4e_H5.md`.
- **HARD BOUNDARIES**:
  - NEVER create, edit, or touch any file under `swarm/` in either `~/Documentos/viewpoint-hackathon` or `~/Documentos/vp-wt-P4e`.
  - Run all probes on a temporary copy under `/tmp/vp_p4e_probe` inside `systemd-run --user --scope -q -p MemoryHigh=4G -p MemoryMax=6G -p MemorySwapMax=0` with `PYTHONDONTWRITEBYTECODE=1`.
  - NEVER read credentials, keys, or files under `~/.gemini`.

## Gate Checklist to Probe & Report (`file:line` + reproducible command)
1. **Unit Suite**: Run `H5`'s `test_viewer.py` on the probe copy and report exact count & timing `[MEASURED]`.
2. **V1 (Torn & Garbage Log Fail-Closed)**: Probe `export_replay.py` with (a) a non-empty `events.jsonl` missing trailing `\n`, (b) invalid UTF-8 / lone surrogate / corrupt JSON in the middle, (c) `v: 2`. Verify non-zero exit naming byte offset and zero partial output left behind.
3. **V2 (Two-Run Shared Log Cutoff)**: Probe `export_replay.py` with records before and after sidecar `start_ts_ms` (and hostile boolean/negative `start_ts_ms`); verify strict filtering.
4. **V3 (XSS / Injection & Path Traversal)**:
   - Verify `static/app.js` uses `textContent` exclusively (zero `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval`, or unsafe `href="javascript:..."` / `src` injection).
   - Probe `server.py` HTTP headers (`Content-Security-Policy`), method allowlist (`POST` -> `405`), symlink refusal, and `..` / `%2e%2e` static path escape refusal.
5. **V4 & V5 (`gateway/replay_store.py` Seam & `REPLAY` Pin Enforcement)**:
   - Verify the pre-seeded `swarm/viewer/replays/take1-w1-2agents.json` loads cleanly through `~/Documentos/viewpoint-hackathon/gateway/replay_store.py` `ReplayStore(..., require_pin=True)` AND fails closed on extra top-level keys, pin mismatch, `live: true`, or `mode != "REPLAY"`.
6. **Cloud Run `Dockerfile` Hygiene**: Check non-root user, explicit allowlist `COPY`, no secrets/writable code, and verify `server.py --host 127.0.0.1 --port <ephemeral>` boots cleanly from the repo copy.

## Verdict Format in `product/reviews/P4e_H5.md`
- List SHA-256 of every file reviewed in `swarm/viewer/`.
- Emit explicit `## VERDICT: APPROVED` or `## VERDICT: REJECTED` (only `BLOCKER` or `LIVE_PATH` defects reject; `BACKLOG` items are recorded without rejecting).
- For any finding, give exact `file:line` and a reproduction test description (prose/CLI probe, never drop replacement code for `swarm/`).
- Commit ONLY `product/reviews/P4e_H5.md` on branch `p-P4e` (`git add product/reviews/P4e_H5.md && git commit -m "P4e H5 review (claude-fable-5-2, Track P)"`).
