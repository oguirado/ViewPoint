# Brief H4: OpenSwitchboard & TaskLight Event Projection + Reconciler (`swarm/projection/`)

## Role & Strict Surface Boundaries
You are Gemini writer unit **H4** on branch `swarm-1114` in `~/Documentos/viewpoint-hackathon`.
- Read `~/Documentos/viewpoint-hackathon/swarm/CONTRACT.md` first and adhere strictly to Section 7 (`OpenSwitchboard` & `TaskLight` projections) and Section 5.2 (`events.jsonl` schema).
- Write **ONLY** inside `~/Documentos/viewpoint-hackathon/swarm/projection/`. Do NOT create or modify any file outside `swarm/projection/`.
- **FORBIDDEN**:
  - Never open, read, list, copy, or execute anything under `~/Documentos/ViewPoint/hackathon/playbooks/` or any subdirectory inside it.
  - Never touch display `:1` or `/dev/shm/vqa-cu/hybrid/` or call `vision`.
  - Run tests inside `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0`. No `sudo`, no package installs, kill by PID only, never read credentials/tokens/auth files.

## Verified External CLI & MCP Contracts (`[MEASURED]`)
1. **OpenSwitchboard (`osb` CLI)**:
   - Command: `XDG_RUNTIME_DIR=/run/user/1000 python3 -I ~/Documentos/switchboard-skill/bin/osb`
   - Default target room: `viewpoint-capsule` (auto-open the room on first run if `osb status --room <room>` exits `rc == 4` by posting `--act open --agent broker-projector --text "ViewPoint Swarm capsule projection room"`!).
   - Turn-level mapping (`CONTRACT.md` §7.1 — skip noisy `PASS` records and skip `cursor` records so only meaningful turn-level transitions are projected):
     - `rec == "enqueue"` -> `post --room <room> --agent <agent_id> --act claim --ref <event_id> --text "enqueue <event_id> (<lane>/<kind>) frame=<based_on_frame>"`
     - `rec == "turn"` -> `post --room <room> --agent broker-projector --act ack --ref turn-<turn_epoch> --text "turn=<turn> turn_epoch=<turn_epoch> head=<agent_id> frame=<head_frame>"`
     - `rec == "close"` with admitted/verified verdict (`VERIFIED`, `VERIFIED_WITH_SPILL`, `STATE_VERIFIED`, `NO_VISIBLE_CHANGE`) -> `post --room <room> --agent <agent_id> --act evidence --ref <event_id> --text "close <event_id> verdict=<verdict> tick=<tick> verify_frame=<verify_frame_ref>"`
     - `rec == "close"` with refusal/error/timeout verdict (`STALE_FRAME`, `FRAME_EVICTED`, `NO_LEASE`, `LEASE_EXPIRED`, `ACTUATOR_BUSY`, `FOOTPRINT_MISSING`, `TURN_TIMEOUT`, `QUEUE_FULL`, `DUPLICATE_EVENT`, `CLAIM_REFUSED`, `JOB_ERROR`, `INPUT_UNCERTAIN`, `OUTCOME_UNCERTAIN`) OR `rec == "control"` with `op == "park"` -> `post --room <room> --agent broker-projector --act refute --ref <event_id_or_agent_id> --text "refute <event_id> verdict=<verdict> drops=<drops> reason=<reason>"` (**CRITICAL**: `--ref <non_empty_str>` MUST ALWAYS be passed on `refute`, otherwise `osb` exits `rc 2`!).
     - Token transfer between consecutive `turn` records (`prev_agent != next_agent`) -> `post --room <room> --agent <prev_agent> --act handoff --ref turn-<turn_epoch> --text "handoff -> <next_agent> (turn_epoch=<turn_epoch>)"`
     - `rec == "control"` with `op in ("stop", "resume")` -> `post --room <room> --agent director-agent --act verdict --ref control-<op> --text "control op=<op> reason=<reason>"`

2. **TaskLight (`context-wall` stdio MCP server)**:
   - Command: `python3 -m bridge.room.mcp` with env `PYTHONPATH=~/Documentos/FireBase_BuildHybridAI/repo` and `CW_HOME=~/.context-wall` (overridable via `--cw-home` for isolated unit tests!).
   - Protocol: newline-delimited JSON-RPC 2.0 over stdio (`initialize` -> `tools/call`).
   - **EXACT VERIFIED SCHEMA** (`bridge/room/store.py` constraints — violating lengths/keys raises `bad_plan` / `bad_step`):
     - `room`: `"viewpoint"`, `agent`: one of `"modeler-agent"`, `"props-agent"`, `"look-agent"`, `"camera-agent"`.
     - `post_plan`: `{"room": "viewpoint", "agent": agent_id, "title": str (<= 120 chars), "steps": [{"text": str (<= 80 chars), "accept": str (<= 160 chars)}, ...]}` (`1 <= len(steps) <= 12`, ONLY keys `"text"` and optional `"accept"` allowed in each step dict!). Call automatically on an agent's FIRST `enqueue` (if not yet planned in this run).
     - `post_step`: `{"room": "viewpoint", "agent": agent_id, "index": int (>= 0, clamped 0..len(steps)-1 or incrementing microtask index), "state": "done"|"active"|"blocked"|"pending", "outcome": str (<= 240 chars), "took": str (<= 32 chars)}`. Map `turn` -> `post_status` `"Acting on turn #<turn>"`; map verified `close` -> `post_step` (`state="done"`, `outcome=f"{event_id}: {verdict}"[:240]`); map refusal/error `close` -> `post_step` (`state="blocked"`, `outcome=f"{event_id}: {verdict}"[:240]`).
     - `post_status`: `{"room": "viewpoint", "agent": agent_id, "text": str}`.
     - `close_task`: `{"room": "viewpoint", "agent": agent_id, "outcome": str (non-empty, single-line, <= 240 chars)}`. Called when `--finalize` / `control op=="stop"` / run completes cleanly for every agent whose work succeeded (or explicitly via CLI).

## Deliverables Inside `swarm/projection/`
1. **`swarm/projection/__init__.py`**
2. **`swarm/projection/projector.py`**:
   - Incremental cursor-tracked (`--state-file` byte/line offset so restarts never double-post!) + live `--follow` daemon mode + one-shot `--once` + `--finalize` (closes open tasks for completed agents and runs reconciler).
   - Supports pluggable/mockable sinks (`OsbSink`, `TaskLightMcpSink`, `DryRunSink`) so unit tests run hermetically and fast without mutating the live `viewpoint-capsule` or `~/.context-wall` rooms!
3. **`swarm/projection/reconciler.py`**:
   - Audits projected state (and/or `~/.context-wall/rooms/viewpoint/events.jsonl` when `--cw-home` is given) against `events.jsonl`:
     - **Negative-control rule**: Flags any agent that emitted `post_plan` (or `enqueue`) without a matching `close_task` (`MISSING_CLOSE_TASK`), any refusal lacking `--ref`, or any `INPUT_UNCERTAIN` un-reconciled event, exiting non-zero (`rc = 1`) with structured JSON report when anomalies exist and `rc = 0` when clean!
4. **`swarm/projection/test_projection.py` & `swarm/projection/README.md`**:
   - Unit + real-MCP-subprocess tests (using a temporary `CW_HOME=/tmp/...` with real `python3 -m bridge.room.mcp` if `~/Documentos/FireBase_BuildHybridAI/repo` exists, plus mock `osb` argv verification!):
     1. Verifies every `CONTRACT.md` §7.1 `osb` act mapping and proves every `refute` carries `--ref <event_id>`.
     2. Verifies `post_plan`, `post_step`, `post_status`, `close_task` succeed (`isError: False`) for all 4 agent IDs (`modeler-agent`, `props-agent`, `look-agent`, `camera-agent`).
     3. **Negative control**: When `look-agent` has `post_plan` + `post_step` but NO `close_task`, `reconciler` flags `look-agent` with `MISSING_CLOSE_TASK` and returns `rc == 1`; once `close_task` is posted, `reconciler` returns `rc == 0`!
   - Run all tests via `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 python3 -m unittest discover -s swarm/projection -p 'test_*.py' -v`.
