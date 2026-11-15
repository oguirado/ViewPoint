# Cross-Family Review: Sol 6.1 (`gpt-6.1-sol`) on `H5_r1` (`swarm/viewer/`)

- **Model**: `gpt-6.1-sol` (`seat=sol`, `wave_id=ac78e147466040a6`)
- **Pack SHA-256**: `None`

- **LIVE_PATH_MAJOR — Swarm integrity can bypass pin verification.** `swarm/viewer/server.py:195–209` permits missing or invalid local swarm pins to become `{}` when `REPLAY_PINS` is supplied. `swarm/viewer/export_replay.py:1197–1204` checks the swarm digest only when that mapping is nonempty. Consequently, an unchanged externally pinned replay can accompany a modified, schema-valid swarm timeline. Require independently trusted swarm pins for every loaded replay and reject missing pins. Add a negative test modifying only the swarm timeline while preserving the original `REPLAY_PINS`.

- **LIVE_PATH_MAJOR — Sidecar cutoff admits frames from before the recording window.** `swarm/viewer/export_replay.py:737–740` retains the original frames when none satisfy the cutoff; `swarm/viewer/export_replay.py:1283–1285` disables frame cutoff filtering when only a single frame was loaded. Their timestamps are subsequently clamped to the window start at `swarm/viewer/export_replay.py:1329`. The boundary test at `swarm/viewer/test_viewer.py:942–947` covers an available in-window frame set, leaving these branches unchecked. Apply the cutoff regardless of frame count and fail closed when no eligible frame remains. Test both a lone pre-cutoff frame and an entirely pre-cutoff frame set.

- **LIVE_PATH_MAJOR — Export output paths lack ancestor symlink protection.** Output directories are created or reused at `swarm/viewer/export_replay.py:1962–1964`, while checks at `swarm/viewer/export_replay.py:1973–1978`, `1985–1990`, and `1992–1997` inspect only destination files. A symlinked `static/frames`, `static/data`, or `replays` directory can redirect writes outside `out_dir`; subsequent validation occurs after those writes at `swarm/viewer/export_replay.py:2014–2037`. Validate the complete output directory chains and canonical containment before any creation, overwrite, pruning, or legacy-pin removal. Add negative tests requiring rejection before external files change.

- **LIVE_PATH_MAJOR — `GATEWAY_URL` has no rendered link support.** `swarm/viewer/server.py:101–124` validates HTTPS URLs and HTTP localhost URLs, and `swarm/viewer/server.py:319–324` exposes `gateway_url`. However, `swarm/viewer/static/app.js:786–809` consumes the catalog without using `gateway_url`, and `swarm/viewer/static/index.html:10–22` provides no gateway link. Add an accessible link populated from the validated catalog value, hidden when absent, and test its rendered destination.

- **BACKLOG — Host-specific `/home/` paths remain in published documentation.** `swarm/viewer/README.md:29–55` exposes repository, evidence, and video-sidecar locations; `swarm/viewer/README.md:82` repeats the local deployment source path. Replace them with portable relative paths or explicitly supplied variables. The supplied exported replay excerpts contain no demonstrated `/home/` leak, but their complete contents are **AUSENTE — no inventar**; the manifest alone cannot establish their absence.

The supplied execution receipt reports passing tests [CITED], but these cases are outside its demonstrated assertions. The actual `DEPLOY_PLAN §4 / §6 Step 6 / §7.2` text is **AUSENTE — no inventar**; findings above apply the explicit checks supplied in the question. Review scope is limited to the inline corpus.

**VERDICT: REJECTED**
