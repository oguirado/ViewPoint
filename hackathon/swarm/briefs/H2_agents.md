# Brief H2: Multi-Agent Runner, Shared-Prefix Briefs & Intent SDK (`swarm/agents/`)

## Role & Strict Surface Boundaries
You are Gemini writer unit **H2** on branch `swarm-1114` in `~/Documentos/viewpoint-hackathon`.
- Read `~/Documentos/viewpoint-hackathon/swarm/CONTRACT.md` first and adhere strictly to every path, field name, claim prefix, and verdict code in it.
- Write **ONLY** inside `~/Documentos/viewpoint-hackathon/swarm/agents/`. Do NOT create or modify any file outside `swarm/agents/`.
- **FORBIDDEN**:
  - Never open, read, list, copy, or execute anything under `~/Documentos/ViewPoint/hackathon/playbooks/` or any subdirectory inside it.
  - Never call `vision` (`look`, `act`, `start`, `stop`) from any agent or helper — agents read `/dev/shm/vqa-cu/swarm/frames/` read-only and write ONLY to `/dev/shm/vqa-cu/swarm/intents/<agent_id>/`.
  - Never write directly to `SWARM_INBOX` (`/dev/shm/vqa-cu/swarm/inbox`), except inside an isolated unit test using a `/tmp` test inbox to verify negative-control refusal behavior.
  - Run tests inside `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0`. No `sudo`, no package installs, kill by PID only (never `pkill -f`), never read credentials/tokens/auth files.

## Deliverables Inside `swarm/agents/`
1. **`swarm/agents/__init__.py`**
2. **`swarm/agents/intent_cli.py`**: Deterministic Python CLI used by headless `agy` agents (and scripted rehearsal modes) so every enqueued intent strictly matches `CONTRACT.md` §5.1:
   - `observe --frames-dir /dev/shm/vqa-cu/swarm/frames --events-log swarm/run/events.jsonl --agent <agent_id>`: reads `latest.json` (waiting up to `--wait-s 30` if not yet published) and inspects `events.jsonl` to report current `frame`, `frame_png` path, `view_epoch`, `scene_epoch`, active `lease_epoch` for `<agent_id>`, and the last `close` receipts for `<agent_id>`.
   - `enqueue-script --agent <agent_id> --event-id <id> --write-set col:CHAR_mesh,obj:CHAR_body --expect "..." --script-file <path.py> [--intents-dir ...] [--frames-dir ...] [--events-log ...]`: reads current `latest.json` (`based_on_frame`, `view_epoch`, `scene_epoch`) and `<agent_id>`'s `lease_epoch` (default `1` if `claim` seen/default), computes `script_sha256`, validates schema locally, writes `/dev/shm/vqa-cu/swarm/intents/<agent_id>/<event_id>.json` atomically via `.tmp` + `os.replace()`.
   - `enqueue-gui --agent camera-agent --event-id <id> --keys KP_1,KP_3,KP_7 [--footprint 0,80,1580,900] --expect "Front/side/top camera orbit review"`: reads FRESH `latest.json` right at enqueue time (`based_on_frame`), builds valid `lane: "gui"`, `action_class: "view_change"`, `kind: "key"`, `keys: [...]`, `max_acts: len(keys)`, `footprint: [0, 80, 1580, 900]`, writes atomically to `/dev/shm/vqa-cu/swarm/intents/camera-agent/<event_id>.json`.
   - `wait-event --event-id <id> --events-log swarm/run/events.jsonl --timeout-s 180`: tails `events.jsonl` until a `rec: "close"` record for `<event_id>` appears; prints the JSON receipt and exits `0` on `STATE_VERIFIED` / `VERIFIED` / `VERIFIED_WITH_SPILL`, `3` on `STALE_FRAME` / `NO_VISIBLE_CHANGE` (telling `camera-agent` to re-read `latest.json` and re-enqueue with a fresh `event_id` and fresh `based_on_frame`!), or `1` on hard error (`CLAIM_REFUSED`, `JOB_ERROR`, `INPUT_UNCERTAIN`). Never auto-repeats `INPUT_UNCERTAIN` or `OUTCOME_UNCERTAIN`!
3. **`swarm/agents/prompts.py`**:
   - Defines `SHARED_BRIEF_PREFIX` (exact byte-identical string >= 1,200 bytes shared at byte offset `0` across ALL 4 agent prompts: `modeler-agent`, `props-agent`, `look-agent`, `camera-agent`!) covering:
     - Collaborative asset goal: **"StarForge Sentinel"** — a stylized sci-fi articulated guardian droid standing on an illuminated hexagonal holographic launch pad (`CHAR_Sentinel` + `PROPS_Pad` + PBR/emissive materials & 3-point studio lights + multi-angle camera orbit review, `.glb` export, and 1080p Eevee renders).
     - Strict capsule rules: read `/dev/shm/vqa-cu/swarm/frames/latest.json` + `frame_<N>.png` ONLY via `intent_cli.py observe` / image reader; NEVER call `vision`; NEVER write to `SWARM_INBOX`; write intents ONLY via `python3 -m swarm.agents.intent_cli`; post TaskLight plan/steps/close via MCP `context-wall` (room `"viewpoint"`, exact agent ID) or allow projection layer sync; stay strictly inside your `write_prefixes` claim.
   - Defines `ROLE_SUFFIXES` for:
     - `modeler-agent` (`write_prefixes: ["col:CHAR_", "obj:CHAR_"]`): 2–3 incremental Blender `bpy` script turns creating collection `CHAR_mesh` ( removing default Cube safely if present or hiding it cleanly inside claim rules — note: create `CHAR_mesh` and remove default `Cube` only if allowed or move/build character at origin `(0, 0, 1.1)` above pad, building torso chassis,visor head, shoulder pauldrons, articulated arms & legs with Subdivision/Bevel modifiers, ~8k–25k polys).
     - `props-agent` (`write_prefixes: ["col:PROPS_", "obj:PROP_"]`): 2–3 script turns creating collection `PROPS_set` with hexagonal launch pedestal (`PROP_Pedestal` at `z=0`), energy pylons (`PROP_Pylon_*`), and floating telemetry drone (`PROP_Drone`).
     - `look-agent` (`write_prefixes: ["mat:", "light:", "world:"]`): 2–3 script turns creating Principled BSDF materials (`mat:SentinelArmor`, `mat:CyanEmissive`, `mat:PedestalDarkSteel`, `mat:GoldTrim`), binding them idempotently to existing `CHAR_*` and `PROP_*` mesh objects as they appear, setting dark sci-fi studio `world:StudioWorld`, and 3-point `light:KeyLight`, `light:RimLight`, `light:FillLight` + Eevee bloom/ambient occlusion settings.
     - `camera-agent` (`write_prefixes: ["cam:", "render:"]`, `allow_gui: true`):
       - Script turn 1 (`cam:MainCam`): create/position `MainCam` (`cam:MainCam`) framing `(0, 0, 1.0)` cleanly and set `scene.camera`.
       - **Mandatory GUI Camera Review turns** (`enqueue-gui`):
         1. Pass 1: `--keys KP_1,KP_3,KP_7` with `--footprint 0,80,1580,900` (front, side, top inspection). If `wait-event` returns `STALE_FRAME` (`rc == 3`), re-observe `latest.json` and submit a fresh `event_id` citing the new `based_on_frame`!
         2. Pass 2: `--keys KP_4,KP_4,KP_Add,Home` with `--footprint 0,80,1580,900` (turntable orbit + zoom + frame all).
       - Final Script turn (`render:FinalDeliverables`): save `.blend` (`swarm/run/output/starforge_sentinel.blend`), export `.glb` (`swarm/run/output/starforge_sentinel.glb`), and render 3 PNG views (`front.png`, `turntable.png`, `hero.png` at `1280x720` or `1920x1080` BLENDER_EEVEE) to `swarm/run/output/`.
4. **`swarm/agents/scripted_smoke.py`**:
   - Non-LLM deterministic scripted producer (`--agents 2` or `--agents 4`, `--output-dir swarm/run/output`) that exercises the EXACT same `intent_cli.py` + `bpy` payloads (2-agent mode for the 12:15 target, 4-agent mode including `camera-agent` GUI key list + `.glb` + renders + one intentional `STALE_FRAME` recovery demonstration if `--demo-stale-rebase` is passed!). This guarantees deterministic end-to-end verification of `H1` + `H2` even before spending LLM quota, while `runner.py` launches real `agy` `gemini-4-hydrogen-high` agents!
5. **`swarm/agents/runner.py`**:
   - Launches `N` (`--roles modeler-agent,props-agent,look-agent,camera-agent`, `--model gemini-4-hydrogen-high`) headless `agy` sub-agents in parallel, each inside `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0`, using exact command form:
     `~/.local/bin/shims/agy --model <model> --dangerously-skip-permissions --output-format stream-json --log-file swarm/proof/run_<HHMM>_<agent_id>.cli.log --print-timeout 1800s -p "<brief>"`
     saving stdout stream to `swarm/proof/run_<HHMM>_<agent_id>.jsonl`, stderr to `.stderr.log`, PID to `.pid`, start/finish timestamps, and SHA-256 summary JSON at exit! Supports `--dry-run` (writes rendered briefs to `swarm/run/agent_briefs/` and prints exact argv without spawning `agy`) and `--kill-all` (kills only recorded PIDs from `.pid` files, never `pkill -f`).
6. **`swarm/agents/test_agents.py` & `swarm/agents/README.md`**:
   - Unit tests verifying:
     1. Byte-identical shared prefix (`prompts.build_brief(r)[:len(SHARED_BRIEF_PREFIX)] == SHARED_BRIEF_PREFIX` for all 4 roles, `len >= 1200`).
     2. `intent_cli.py` produces valid `CONTRACT.md` §5.1 script and GUI intents atomically (`v=1`, `script_sha256`, `footprint`, `keys`).
     3. Negative control: `intent_cli.py` refuses disallowed GUI keys (e.g. `KP_9` or `Delete`), refuses GUI intents from non-camera agents locally or marks test vector, and never writes to `SWARM_INBOX`.
     4. `runner.py --dry-run` validates exact `agy` flag order (`-p` MUST be the very last flag!).
     5. `scripted_smoke.py` generates valid `bpy` scripts and intent files against a mock `events.jsonl` responder.
   - Run all unit tests via `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 python3 -m unittest discover -s swarm/agents -p 'test_*.py' -v`.
