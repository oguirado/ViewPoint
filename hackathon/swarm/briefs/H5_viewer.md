# Brief H5 — Static Replay Exporter & Cloud Run Viewer (`swarm/viewer/`)

## Ownership & Strict Boundaries
- **Unit**: `H5` (Track H, model `gemini-4-carbon-high`)
- **Owned Directory**: `~/Documentos/viewpoint-hackathon/swarm/viewer/` ONLY.
- **Read-Only Reference Paths**:
  - `~/Documentos/viewpoint-hackathon/swarm/CONTRACT.md`
  - `~/Documentos/viewpoint-hackathon/gateway/replay_store.py`
  - `~/Documentos/viewpoint-hackathon/gateway/replay/h06-sample-v1.json`
  - `~/Documentos/viewpoint-hackathon/swarm/overlay/compositor.py` (for sidecar `<video>.meta.json` `start_ts_ms` cutoff convention)
  - `~/Documentos/viewpoint-hackathon/swarm/run/take1_w1_2agents/evidence/events.jsonl` and `frames/`
  - `~/Vídeos/ViewPoint_hackathon/swarm_take1_w1_2agents_131916.mp4.meta.json`
- **STRICT FORBIDDEN PATHS**:
  - NEVER open, read, or mention `~/Documentos/ViewPoint/hackathon/playbooks/` or `prior_playbook_helpers`.
  - NEVER modify `gateway/` or any file outside `swarm/viewer/`.
  - NEVER read any credentials, keys, tokens, or files under `~/.gemini`.
  - Run all tests inside `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0` with `PYTHONDONTWRITEBYTECODE=1`. Never leave `.pyc` or `__pycache__` committed.

## Deliverables Required Under `swarm/viewer/`
Build a clean, zero-pip-dependency (Python 3.12 stdlib only) static replay exporter and read-only Cloud Run viewer under `swarm/viewer/`:

1. `swarm/viewer/__init__.py`
2. `swarm/viewer/export_replay.py` — **Fail-Closed Replay Exporter CLI**:
   - Reads an authoritative `events.jsonl` (`swarm/CONTRACT.md` §5.2: `claim`, `enqueue`, `turn`, `close`, `cursor`, `control`, `v: 1`) plus an optional `<video>.meta.json` sidecar (written by `swarm/tools/record.sh`, carrying `start_ts_ms`) and a frames directory (`frame_<N>.png` + `frame_<N>.json`).
   - **Fail-Closed Parsing (Control V1)**:
     - Every line (including the final line: a non-empty file MUST end with `\n` — a torn last line without trailing `\n` or any malformed UTF-8 / invalid JSON / non-dict record or unsupported `v != 1` MUST raise `ExportError` naming the exact 0-based `byte_offset` and exit non-zero!). NEVER silently skip corrupt or torn lines.
   - **Run-Window Cutoff (Control V2)**:
     - Reuse H3's exact rule: when `--sidecar <video>.meta.json` (or `--min-ts-ms <int>`) is given, keep ONLY records whose integer `ts_ms >= start_ts_ms` so prior runs in a shared `events.jsonl` are excluded cleanly. Validate `start_ts_ms` is a non-boolean integer `>= 0`.
   - **Dual Validated Output**:
     - Copies referenced still frames (`1..40` frames max, evenly sampled or deduplicated in monotonic `t_ms` order if > 40, rejecting symlinks and `..` traversal, verifying SHA-256) into `<out_dir>/static/frames/f<NN>.png`.
     - Emits `<out_dir>/replays/<replay_id>.json` conforming **100% to `gateway/replay_store.py` (`viewpoint.replay/v1`)** (`TOP_LEVEL_FIELDS` exact match, `mode: "REPLAY"`, `labels: {"mode": "REPLAY", "live": false, "sample": false}`, `actors` mapped deterministically into `{"operator", "engine", "planner", "system"}` with the swarm `agent_id` + `event_id` + `verdict` preserved in `action`/`target`/`outcome`, `steps` `1..60` in non-decreasing `t_ms`, `limitations` honest list including shared-uid host scope note) **AND** verifies the exported replay passes `gateway.replay_store.ReplayStore` with `require_pin=True`!
     - Also emits `<out_dir>/static/data/<replay_id>.swarm.json` (`schema: "viewpoint.swarm_timeline/v1"`, `mode: "REPLAY"`, `live: false`, exact per-agent colour palette from `swarm/CONTRACT.md` for `modeler-agent`, `props-agent`, `look-agent`, `camera-agent`, full turn/refusal/cursor timeline, frame SHA-256s, and optional relative video URL `/static/videos/<name>.mp4` if `--video` is supplied) for rich multi-agent timeline scrubbing in the static viewer page, validated by a strict fail-closed validator `validate_swarm_bundle()` (`_require_exact_fields`, `mode == "REPLAY"`, `live is False`, SHA-256 pin verification).
   - **Pre-seed Real Verified Bundle**:
     - Run `export_replay.py` on `~/Documentos/viewpoint-hackathon/swarm/run/take1_w1_2agents/evidence/events.jsonl`, frames `~/Documentos/viewpoint-hackathon/swarm/run/take1_w1_2agents/evidence/frames`, and sidecar `~/Vídeos/ViewPoint_hackathon/swarm_take1_w1_2agents_131916.mp4.meta.json` (`--replay-id take1-w1-2agents`) to generate a real, verified replay bundle under `swarm/viewer/replays/take1-w1-2agents.json`, `swarm/viewer/static/data/take1-w1-2agents.swarm.json`, `swarm/viewer/static/frames/`, and `swarm/viewer/replays/PINS.txt` (`take1-w1-2agents=<sha256>`). Keep total size under `swarm/viewer/` compact (`<= 4 MiB` total — copy only the referenced still frames from `take1_w1_2agents`, NOT the `.mp4` binaries into git!).
3. `swarm/viewer/server.py` + `swarm/viewer/static/{index.html,app.js,style.css}`:
   - Read-only stdlib HTTP server (`http.server` / `ThreadingHTTPServer`, `GET` and `HEAD` only; `POST`/`PUT`/`DELETE` return `405`), listening on `HOST` (default `127.0.0.1` CLI, `0.0.0.0` in `Dockerfile`) and `PORT` (default `8080`).
   - Startup validates `replays/` + `PINS.txt` fail-closed using `ReplayStore(..., require_pin=True)` and `validate_swarm_bundle()`.
   - Serves strict security headers (`Content-Security-Policy: default-src 'self'; img-src 'self' data:; media-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`). Rejects symlink static assets and any `..` path traversal (`403`/`404`).
   - `static/index.html` + `static/app.js` + `static/style.css`: clean, responsive dark-mode replay UI with explicit `REPLAY · NOT LIVE` badge, replay selector, `<video controls preload="metadata">` slot (gracefully showing frame-by-frame scrubber when offline `.mp4` is not mounted), per-agent coloured badges (`modeler-agent`, `props-agent`, `look-agent`, `camera-agent`), turn-by-turn scrubber (`STATE_VERIFIED`, `VERIFIED_WITH_SPILL`, `VERIFIED`, `JOB_ERROR`, `STALE_FRAME`, `CLAIM_REFUSED`), and SHA-256 receipt panel.
   - **XSS Safety (Control V3)**: `app.js` MUST NEVER use `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, or `eval`. Every dynamic field (`title`, `label`, `action`, `outcome`, `reason`, `expect`, `caption`) MUST be assigned exclusively via `textContent`!
4. `swarm/viewer/Dockerfile` + `swarm/viewer/.dockerignore` + `swarm/viewer/README.md`:
   - `Dockerfile` modeled on `gateway/Dockerfile`: `FROM python:3.12-slim`, `PYTHONDONTWRITEBYTECODE=1`, non-root `USER viewer` (`uid 10001`), explicit allowlist `COPY` (`server.py`, `export_replay.py`, `__init__.py`, `static/`, `replays/`), `chmod 0444`/`0555`, `EXPOSE 8080`, `CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8080"]`.
   - `README.md`: local run, export command, test command, Cloud Run operator deploy command (noting operator runs deploy; never deploy yourself), and honest scope statement ("Read-only `REPLAY` viewer (`live=false`); displays recorded `events.jsonl` + verified frames + optional composed `.mp4`").
5. `swarm/viewer/test_viewer.py` — **Unit Suite + All 5 Mandatory Must-Fail Controls**:
   - **V1 (Torn & Garbage Line Fail-Closed)**: Feed a log with a torn last line (no trailing `\n`) and a log with one garbage/corrupt line in the middle; assert `export_replay` exits non-zero / raises `ExportError` naming the exact byte offset and writes no output file.
   - **V2 (Two-Run Shared Log Sidecar Cutoff)**: Feed a shared log containing events at `ts_ms = 1000` (prior run) and `ts_ms = 5000` (current run) with sidecar `start_ts_ms = 5000`; assert ONLY records with `ts_ms >= 5000` appear in both outputs.
   - **V3 (XSS Label `textContent` Enforcement)**: Export a log containing `<img src=x onerror=alert(1)>` in `expect`/`label`/`reason`; assert (a) static source check confirms zero `innerHTML`/`outerHTML`/`insertAdjacentHTML`/`document.write` in `static/app.js`, (b) HTTP server sends strict CSP header (`script-src 'self'`, `object-src 'none'`), and (c) JSON API encodes safely.
   - **V4 (Extra Key & Pin Mismatch Refusal)**: Mutate exported replay JSON by adding one extra top-level key OR changing 1 byte so SHA-256 mismatches `PINS.txt`; assert both `gateway.replay_store.ReplayStore(..., require_pin=True)` and `validate_swarm_bundle` reject fail-closed.
   - **V5 (Live Mode Refusal)**: Mutate `labels.live = True` or `mode = "LIVE"`; assert startup/validation rejects fail-closed.
   - **HTTP Integration Test**: Boot `server.py` on an ephemeral localhost port (`127.0.0.1:0`), test `GET /`, `GET /api/replays`, `GET /api/replays/take1-w1-2agents`, `GET /static/app.js`, `POST / -> 405`, and path-escape `/static/../server.py -> 404/403`.

## Verification Before Finishing
Run:
`PYTHONDONTWRITEBYTECODE=1 systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 python3 -B -m unittest discover -s swarm/viewer -p 'test_*.py' -v`
Confirm 100% pass and clean up any `__pycache__` if created. Report file list, test count, and pre-seeded `take1-w1-2agents` SHA-256 pin.
