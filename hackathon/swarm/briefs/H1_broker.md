# Brief H1: Turn Queue / Actor Broker & Blender Bridge (`swarm/broker/`)

## Role & Strict Surface Boundaries
You are Gemini writer unit **H1** on branch `swarm-1114` in `~/Documentos/viewpoint-hackathon`.
- Read `~/Documentos/viewpoint-hackathon/swarm/CONTRACT.md` first and implement it verbatim.
- Write **ONLY** inside `~/Documentos/viewpoint-hackathon/swarm/broker/`. Do NOT create or modify any file outside `swarm/broker/` (use `/tmp` or `/dev/shm/vqa-cu/swarm_test_*` for unit test temp dirs, never `swarm/run/` during unit tests).
- **FORBIDDEN**:
  - Never open, read, list, copy, or call anything under `~/Documentos/ViewPoint/hackathon/playbooks/` or any subdirectory inside it.
  - Never touch display `:1`, `/dev/shm/vqa-cu/hybrid/`, or any `vision` job during unit tests (mock `vision` in unit tests; the orchestrator starts `vpswarm` itself once `bridge.py` exists).
  - Run any test command inside `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0`.
  - No `sudo`, no package installs, no `pkill -f` (kill by recorded PID only), never read credentials/tokens/auth files.

## CRITICAL DELIVERY ORDER
**Step 1 (DO THIS FIRST)**: Write `~/Documentos/viewpoint-hackathon/swarm/broker/__init__.py` and `~/Documentos/viewpoint-hackathon/swarm/broker/bridge.py` immediately as your first files on disk so the orchestrator can start the live Blender 4.0.2 `vpswarm` capsule as soon as `bridge.py` exists!
**Step 2**: Write `~/Documentos/viewpoint-hackathon/swarm/broker/broker.py` (turn queue + actor broker + CLI).
**Step 3**: Write `~/Documentos/viewpoint-hackathon/swarm/broker/test_broker.py` and `~/Documentos/viewpoint-hackathon/swarm/broker/README.md`, and run all tests inside `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 python3 -m unittest discover -s swarm/broker -p 'test_*.py' -v`.

## Component 1: `swarm/broker/bridge.py` (Blender 4.0.2 Main-Thread Bridge)
Runs inside Blender (`blender --factory-startup --python ~/Documentos/viewpoint-hackathon/swarm/broker/bridge.py`). Must also expose pure-Python helper functions (`discover_nonce()`, `process_inbox_once(exec_fn=...)`) so unit tests can test nonce filtering and rejection without requiring `bpy` when imported outside Blender!
- When `bpy` is importable, register `bpy.app.timers.register(_timer_tick, first_interval=0.2, persistent=True)` returning `0.2` seconds so it polls continuously on Blender's main thread without crashing on exceptions.
- Paths from env with defaults per `CONTRACT.md`:
  - `SWARM_INBOX` = `/dev/shm/vqa-cu/swarm/inbox`
  - `SWARM_OUTBOX` = `/dev/shm/vqa-cu/swarm/outbox`
  - `SWARM_REJECTED` = `/dev/shm/vqa-cu/swarm/rejected`
  - Nonce discovery on each poll: check `os.environ.get("SWARM_BRIDGE_NONCE")` first; if unset/empty, read `/dev/shm/vqa-cu/swarm/nonce.txt` (`SWARM_NONCE_FILE` override for tests) stripped. Reading `nonce.txt` dynamically lets a restarted broker rotate the run nonce without restarting Blender!
- Ignore transient `.tmp` / hidden dotfiles (`name.startswith(".")` or `name.endswith(".tmp")`).
- Match filenames strictly against `^([0-9a-fA-F]+)__(\d+)__([A-Za-z0-9._-]+)\.py$` where group 1 equals the active `nonce`:
  - **Mismatch (wrong nonce, missing nonce, wrong extension, direct un-nonced write)**: move immediately (`os.replace` / `shutil.move`) to `SWARM_REJECTED/<filename>`, append a JSON log entry to `SWARM_REJECTED/rejected.jsonl` (`{"ts_ms":...,"file":...,"reason":"NONCE_MISMATCH"|"INVALID_NAME"}`), and NEVER execute!
  - **Valid match**: sort matching jobs by integer `<seq>`, execute one job per timer tick (or sequentially) using `exec(compile(source, job_path, "exec"), {"__name__": "__main__", "bpy": bpy_mod})` while capturing `stdout`/`stderr` (`contextlib.redirect_stdout/stderr`). Remove the inbox `.py` file and atomically write (`<out>.tmp` -> `os.replace`) `<nonce>__<seq>__<event_id>.json` in `SWARM_OUTBOX`:
    ```json
    {"v":1,"nonce":"<hex>","seq":1,"event_id":"modeler-agent-1","status":"OK","elapsed_ms":42,"stdout":"...","error":null,"ts_ms":1731580000000}
    ```
    On exception, set `"status": "ERROR"` and `"error": traceback_str`.

## Component 2: `swarm/broker/broker.py` (Turn Queue + Actor Broker)
Implements the exact `CONTRACT.md` state machine and schemas:
1. **Per-Run Nonce & Lease Table**:
   - Generates a fresh 16-hex-char nonce (`secrets.token_hex(8)`) unless `SWARM_BRIDGE_NONCE` is set, writes it atomically to `/dev/shm/vqa-cu/swarm/nonce.txt` (`SWARM_NONCE_FILE`), and ensures `inbox`, `outbox`, `rejected`, `frames`, and `intents/<agent_id>` directories exist.
   - Issues initial `rec: "claim"` leases (`lease_epoch = 1`, `ttl_ms = 3600000` default configurable via `--lease-ttl-ms`) to `modeler-agent`, `props-agent`, `look-agent`, `camera-agent` with exact `CONTRACT.md` §4.1 `write_prefixes` and `allow_gui` flags, appending each `claim` record to `events.jsonl`.
2. **Frame Capture & Fan-out (`vision` Sole Caller)**:
   - Broker is the ONLY caller of `python3 -I ~/Documentos/vis-skill/vision`.
   - `capture_frame()` runs `python3 -I ~/Documentos/vis-skill/vision look --job <job>` (parses JSON stdout; looks up `frame` / `png` / `screenshot` path in JSON; computes SHA-256 and `changed_regions` either from `vision` JSON or deterministic `numpy`/`PIL` bounding-box diff vs prior frame PNG), copies image atomically to `/dev/shm/vqa-cu/swarm/frames/frame_<N>.png`, writes `frame_<N>.json` and atomic `latest.json` (`CONTRACT.md` §2.2). Maintains a bounded in-memory frame history ring (default last `64` frame numbers, configurable `--frame-ring-size`) to detect `FRAME_EVICTED`.
   - Also support `--vision-mode live|mock` (default `live`) where `mock` synthesizes valid `1920x1080` PNGs (via `PIL`) for deterministic offline unit tests and dry-runs!
3. **Intent Ingestion (`ENQUEUE`)**:
   - Scans `/dev/shm/vqa-cu/swarm/intents/<agent_id>/*.json` (ignoring `.tmp` files), sorted by `(created_ts_ms, filename)`. Moves consumed/ingested intent files into `/dev/shm/vqa-cu/swarm/intents/<agent_id>/processed/` so each file is ingested once.
   - Checks idempotency (`event_id` already seen -> emit `enqueue` + `close` refusal `DUPLICATE_EVENT`, do not queue).
   - Checks bounded FIFO (`len(fifo[agent_id]) >= 8` -> emit `close` refusal `QUEUE_FULL`, never overwrite).
   - Otherwise appends to `fifo[agent_id]`, logs `rec: "enqueue"` and `rec: "cursor"` (`phase: "intent"`, coordinates from `cursor_xy` or deterministic per-agent slot `CONTRACT.md` §5.2).
   - Also check `/dev/shm/vqa-cu/swarm/intents/director-agent/*.json` for priority HI `control` intents (`stop`, `resume`, `reconcile`, `park`, `checkpoint`) applied strictly at turn boundaries!
4. **NON-INTERACTIVE Turn Loop (`TURN` -> `ADMIT` -> `EXECUTE` -> `CLOSE` -> `RETURN`)**:
   - Round-robin over active unparked ring `["modeler-agent", "props-agent", "look-agent", "camera-agent"]`.
   - If `fifo[agent_id]` is empty: emit `turn` + `close` with `verdict: "PASS"` (`event_id: null`, `tick` unchanged) — note: to avoid spamming `events.jsonl` during idle polling when ALL agents are empty, emit `PASS` when at least one agent in the current ring sweep has work OR when `--log-idle-pass` is enabled, otherwise sleep `--poll-ms`! (Make sure explicit ring sweeps in scripted/unit tests emit `PASS` cleanly when an agent has nothing queued while peers have work).
   - Pop oldest queued intent from `fifo[agent_id]`. Emit `rec: "turn"` (`turn`, `turn_epoch`, `agent_id`, `head_frame = H`, `hold_ms`).
   - Validate lease (`NO_LEASE` if missing, `LEASE_EXPIRED` if `intent.lease_epoch != claim.lease_epoch` or `now_ms > claim.expires_ts_ms`).
   - **If `lane == "script"`**:
     - Validate `write_set` non-empty and every entry starts with an allowed `claim.write_prefixes` (`CLAIM_REFUSED` otherwise). Verify `script` is non-empty string (`CLAIM_REFUSED` if missing) and `script_sha256` matches if provided.
     - Emit `rec: "cursor"` (`phase: "act"`).
     - Write `<nonce>__<next_tick>__<event_id>.py` atomically to `SWARM_INBOX`. Poll `SWARM_OUTBOX/<nonce>__<next_tick>__<event_id>.json` up to `--script-timeout-s 120`.
     - On `status == "OK"`: `verdict = "STATE_VERIFIED"`, `scene_epoch += 1`, `tick += 1`, capture `frame_<N+1>`, reset consecutive timeouts for agent.
     - On `status == "ERROR"`: `verdict = "JOB_ERROR"`, `tick += 1`, capture `frame_<N+1>`.
     - On timeout: `verdict = "TURN_TIMEOUT"`, `turn_epoch += 1`, increment agent's consecutive timeout counter (if `== 2`, park agent and log `rec: "control", op: "park"`). If outbox reply is malformed/corrupt -> `verdict = "OUTCOME_UNCERTAIN"`, enter `RECONCILE` state (halt further dispatches until `control` `reconcile`/`resume`, NEVER auto-repeat!).
   - **If `lane == "gui"`**:
     - Only `camera-agent` (`claim.allow_gui == True`) may run GUI intents; any other agent gets `CLAIM_REFUSED` (or `NO_LEASE` if unleased).
     - Require non-null `footprint` (`[x,y,w,h]`, else `FOOTPRINT_MISSING`) and `1 <= len(keys) <= 8` with every key in `{"KP_1", "KP_3", "KP_7", "KP_4", "KP_Add", "Home"}` (else `CLAIM_REFUSED`).
     - Frame freshness: if `based_on_frame` not in broker's frame ring -> `FRAME_EVICTED` (drop). If `based_on_frame < H` and not `rebased`: refuse with `STALE_FRAME`, `drops += 1`; if `drops < 3`, push back to tail of `camera-agent` FIFO (with `drops` persisted; note: allow optional `--auto-rebase-viewport` flag default `False`, keeping strict `STALE_FRAME` refusal by default per `CONTRACT.md`!); if `drops >= 3`, drop/park event with `reason: "parked_after_3_stale_drops"`.
     - When admitted (`based_on_frame == H`): emit `rec: "cursor"` (`phase: "act"`). First hover viewport once:
       `python3 -I ~/Documentos/vis-skill/vision act --job <job> --xy 800,500 --expect-change none`
       Then for each `<KEY>` in `keys` (bounded by `--gui-timeout-s 60` total):
       `python3 -I ~/Documentos/vis-skill/vision act --job <job> --key <KEY> --expect-change 0,80,1580,900`
     - Classify `vision act` exit codes:
       - All `rc == 0`: capture `frame_<N+1>`, split `changed_regions` against `footprint` into `own_regions` and `spill_regions`; `verdict = "VERIFIED"` if `not spill_regions` else `"VERIFIED_WITH_SPILL"`, `view_epoch += 1`, `tick += 1`.
       - Any `rc == 3` (and none worse): capture `frame_<N+1>`, `verdict = "NO_VISIBLE_CHANGE"`, `tick += 1` (do NOT repeat blindly).
       - Timeout (`subprocess.TimeoutExpired` / `> 60s`): `verdict = "TURN_TIMEOUT"`, `turn_epoch += 1` (2 in a row parks agent).
       - Uncertain (`rc` in `{4,6,8,9}` or unexpected): `verdict = "INPUT_UNCERTAIN"`, `tick += 1`, enter `RECONCILE` state, NEVER auto-repeat!
   - Emit `rec: "close"` and `rec: "cursor"` (`phase: "result"`). Agent returns to ring tail.
5. **CLI Flags on `swarm/broker/broker.py`**:
   - `--job vpswarm`, `--vision-mode live|mock`, `--inbox DIR`, `--outbox DIR`, `--rejected DIR`, `--frames-dir DIR`, `--intents-dir DIR`, `--events-log FILE`, `--nonce-file FILE`, `--max-ticks N`, `--max-sweeps N`, `--drain-and-exit` (process all currently queued intents until FIFOs empty, then exit cleanly 0!), `--idle-timeout-s S`, `--poll-ms 200`.

## Required Unit & Negative-Control Tests (`swarm/broker/test_broker.py`)
Verify ALL of these with deterministic unit tests (`--vision-mode mock` + in-process `bridge.process_inbox_once` thread/helper):
1. **3-agent FIFO rotation**: `modeler-agent`, `props-agent`, `look-agent` enqueue valid scripts; broker rotates round-robin, runs via bridge, writes `STATE_VERIFIED` to `events.jsonl`, advances `tick` 1..3 and `scene_epoch`.
2. **Negative control — Direct inbox write / wrong nonce rejected**: writing `evil_job.py` or `wrongnonce__1__ev.py` directly to `SWARM_INBOX` moves it to `SWARM_REJECTED` without execution!
3. **Negative control — `STALE_FRAME` + 3-drop park**: `camera-agent` queues GUI intent citing frame `0` after `modeler-agent` advances frame to `1`; refused `STALE_FRAME` (`drops=1`, `2`, then parked at `3`).
4. **Negative control — `CLAIM_REFUSED`**: `modeler-agent` attempts `write_set: ["mat:Gold"]` -> `CLAIM_REFUSED`, not dispatched; `modeler-agent` attempts `lane: "gui"` -> `CLAIM_REFUSED`.
5. **Negative control — `QUEUE_FULL` & `DUPLICATE_EVENT`**: 9th queued intent returns `QUEUE_FULL` without overwriting first 8; duplicate `event_id` returns `DUPLICATE_EVENT`.
6. **Negative control — `LEASE_EXPIRED` & `TURN_TIMEOUT` & `INPUT_UNCERTAIN` -> `RECONCILE` (no auto-repeat)**.
