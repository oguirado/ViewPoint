# OpenSwitchboard & TaskLight Event Projection + Reconciler (`swarm/projection/`)

**Unit**: Gemini Writer Unit **H4_r2 (Repair Loop 2)**  
**Branch**: `swarm-1114`  
**Workspace**: `~/Documentos/viewpoint-hackathon/swarm/projection/`  
**Contract Authority**: [CONTRACT.md](file://~/Documentos/viewpoint-hackathon/swarm/CONTRACT.md) §5.2, §6, and §7.

---

## 1. Overview & Architecture

The projection layer consumes the authoritative broker discrete event simulation log (`events.jsonl`) and projects turn-level transitions into two external visibility systems:
1. **OpenSwitchboard (`osb`)**: Runtime projection room `viewpoint-capsule` (§7.1).
2. **TaskLight (`context-wall`)**: Headless agent coordination room `viewpoint` via stdio JSON-RPC MCP server (`bridge.room.mcp`, §7.2).

```text
       +---------------------------------------------+
       |   Authoritative Log: swarm/run/events.jsonl |
       +---------------------------------------------+
                              |
                              v
       +---------------------------------------------+
       |     Projector Core (swarm.projection)       |
       |  - Cursor offset tracking (--state-file)    |
       |  - Partial trailing line hold (ends \n)     |
       |  - Turn-level filtering (skips PASS/cursor) |
       |  - 12-step plan rollover (cont. #N)         |
       |  - OSB text sanitization (\r, <= 3800 B)    |
       +---------------------------------------------+
                |                           |
                v                           v
  +--------------------------+   +-------------------------------+
  |  OpenSwitchboard (`osb`) |   |  TaskLight (`context-wall`)   |
  |  Room: viewpoint-capsule |   |  Room: viewpoint (4 agents)   |
  |  - claim (enqueue)       |   |  - post_plan (first enqueue)  |
  |  - handoff (turn shift)  |   |  - post_status (acting turn)  |
  |  - ack (turn granted)    |   |  - post_status (pause/resume) |
  |  - evidence (admitted)   |   |  - post_step (done/blocked)   |
  |  - refute (--ref mand.)  |   |  - rollover post_plan (cont.) |
  |  - verdict (control)     |   |  - close_task (--finalize)    |
  +--------------------------+   +-------------------------------+
                                                |
                                                v
                                 +-------------------------------+
                                 |  Reconciler Audit Engine      |
                                 |  - Negative control check     |
                                 |  - Room store sole authority  |
                                 |  - STATE_ROOM_MISMATCH guard  |
                                 |  - MISSING_CLOSE_TASK check   |
                                 |  - Per-event reconcile check  |
                                 |  - Exits rc=0 or rc=1         |
                                 +-------------------------------+
```

---

## 2. Protocol Mappings & Repair Invariants

### 2.1 OpenSwitchboard (`osb`) Mappings (CONTRACT.md §7.1)
- **CLI**: `XDG_RUNTIME_DIR=/run/user/1000 python3 -I ~/Documentos/switchboard-skill/bin/osb` (with `OSB_LANE` scrubbed from child environment).
- **Room auto-open**: If `osb status --room <room>` exits `rc == 4`, automatically emits `--act open --agent broker-projector --text "ViewPoint Swarm capsule projection room"`.
- **Text sanitization (`sanitize_osb_text`)**: Control characters (`ord(ch) < 32` other than `\n` and `\t`, notably `\r`, plus `U+007F` DEL) are stripped/replaced with space, lone UTF-16 surrogates (`0xD800..0xDFFF`) are replaced with `\ufffd`, and payload is capped at `<= 3800` UTF-8 bytes to ensure `osb` never rejects with `rc 2`.
- **Filtering**: Skips zero-hold (`hold_ms == 0`) `PASS` turn records and off-display `cursor` records.

| Broker Event | Trigger Condition | `osb` Act | Agent Slug | `--ref` Required Value | Formatted Message Text |
|---|---|---|---|---|---|
| `enqueue` | Every admitted enqueue | `claim` | `<agent_id>` | `<event_id>` | `enqueue <event_id> (<lane>/<kind>) frame=<based_on_frame>` |
| `turn` | Token transfer (`prev != next`, `hold_ms > 0`) | `handoff` | `<prev_agent>` | `turn-<turn_epoch>` | `handoff -> <next_agent> (turn_epoch=<turn_epoch>)` |
| `turn` | Turn admission granted (`hold_ms > 0`) | `ack` | `broker-projector` | `turn-<turn_epoch>` | `turn=<turn> turn_epoch=<turn_epoch> head=<agent_id> frame=<head_frame>` |
| `close` | Admitted / verified verdict | `evidence` | `<agent_id>` | `<event_id>` | `close <event_id> verdict=<verdict> tick=<tick> verify_frame=<verify_frame_ref>` |
| `close` | Refusal / timeout / error (non-PASS) | `refute` | `broker-projector` | **Mandatory** `<event_id>` or `refusal-turn-<turn>-<agent>` | `refute <ref> verdict=<verdict> drops=<drops> reason=<reason>` |
| `control` | Priority HI `park` | `refute` | `broker-projector` | **Mandatory** `<target>` parsed from reason | `refute <target> verdict=<verdict> drops=<drops> reason=<reason>` |
| `control` | Priority HI `stop`/`resume` | `verdict` | `by` or `director-agent` | `control-<op>` | `control op=<op> reason=<reason>` |

> [!IMPORTANT]
> **Strict Non-Empty Ref Rule on `refute`**: For any refusal `close` where `event_id` is null/empty, the projector synthesizes a deterministic ref `f"refusal-turn-{turn}-{agent_id}"`. For `control op="park"`, the projector extracts `event_id`, `agent_id`, or parses `<id>` after `:` in `reason` (e.g. `parked_after_3_stale_drops:<event_id>`).

### 2.2 TaskLight (`context-wall`) Mappings (CONTRACT.md §7.2)
- **Subprocess**: `python3 -m bridge.room.mcp` with `PYTHONPATH=~/Documentos/FireBase_BuildHybridAI/repo` and `CW_HOME` (default `~/.context-wall`).
- **Room**: `viewpoint`
- **Tracked Agents (4)**: `modeler-agent`, `props-agent`, `look-agent`, `camera-agent`.

| Lifecycle Moment | MCP Method | Payload Schema Constraints |
|---|---|---|
| First `enqueue` of agent | `post_plan` | `{"room": "viewpoint", "agent": agent, "title": str (<= 120), "steps": [...]}` (`1 <= len(steps) <= 12`) |
| 12-step Rollover (`index >= 12`) | `post_plan` | `{"room": "viewpoint", "agent": agent, "title": f"Task plan for {agent} (cont. #{gen})", ...}` (resets step index to 0) |
| `turn` granted (`hold_ms > 0`) | `post_status` | `{"room": "viewpoint", "agent": agent, "text": "Acting on turn #<turn>"}` |
| `control op="stop"` | `post_status` | `{"room": "viewpoint", "agent": agent, "text": "Paused: broker control stop"}` |
| `control op="resume"` | `post_status` | `{"room": "viewpoint", "agent": agent, "text": "Resumed: broker control resume"}` |
| Verified `close` | `post_step` | `{"room": "viewpoint", "agent": agent, "index": int (0..11), "state": "done", "outcome": f"{event_id}: {verdict}"[:240]}` |
| Refusal / Error `close` | `post_step` | `{"room": "viewpoint", "agent": agent, "index": int (0..11), "state": "blocked", "outcome": f"{event_id}: {verdict}"[:240]}` |
| `--finalize` (terminal completion) | `close_task` | `{"room": "viewpoint", "agent": agent, "outcome": str (single-line, non-empty, <= 240)}` |

---

## 3. Cursor-Tracked State Persistence & Safety

State is maintained in `--state-file` (e.g. `swarm/run/projection_state.json`) with atomic swap (`.tmp` + `os.replace`):
```json
{
  "offset_bytes": 1048576,
  "line_number": 4200,
  "prev_turn_agent": "look-agent",
  "planned_agents": ["modeler-agent", "props-agent", "look-agent", "camera-agent"],
  "closed_agents": ["modeler-agent", "props-agent", "look-agent", "camera-agent"],
  "step_indices": {"modeler-agent": 3, "props-agent": 2, "look-agent": 1, "camera-agent": 4},
  "plan_generations": {"modeler-agent": 1, "props-agent": 1, "look-agent": 1, "camera-agent": 1},
  "open_intents": {},
  "uncertain_events": [],
  "refusals": []
}
```

### Safety & Hygiene Invariants:
1. **Binary Newline Framing & Partial Line Safety (m5, r1-4)**: `run_once` reads `events.jsonl` in binary mode (`"rb"`) and only consumes lines ending with `b"\n"`. If a partial ASCII line or split multibyte UTF-8 sequence without trailing `b"\n"` is read at EOF, the file position is held / restored without advancing `offset_bytes` until the writer finishes flushing `b"\n"`, decoding complete lines with `errors="replace"`.
2. **Idle Poll Economy (m8)**: State is only rewritten and fsynced if `offset_bytes` actually changed during the poll.
3. **Delivery Semantics**: Projection is at-least-once with offset tracking; per-record projection exceptions are isolated so one malformed record never aborts the run (`r1-3`).
4. **Independent `close_task` Verification & Error Propagation (M1, R1, r1-5)**: `agent_id` is ONLY added to `state.closed_agents` if `sink.close_task` succeeded without error (`not res.get("isError")` and no exception). Exceptions during `finalize_open_tasks` are isolated per agent so all remaining open agents still receive `close_task` attempts, and every recorded error in `last_finalize_errors` appends `CLOSE_TASK_REFUSED` (`CLOSE_TASK_REFUSED:<err>`) in `finalize()`, forcing `report.clean = False` and `rc != 0`.

---

## 4. Reconciler Audit & Negative Control

The reconciler (`reconciler.py`) audits `events.jsonl` against TaskLight room events:
1. **Room Store Sole Authority & Latest-Plan Scoping (M1, R1)**: Whenever `cw_home` is available, `<cw_home>/rooms/<room>/events.jsonl` (`act == "close"`) is the sole ground truth, and a `close` event satisfies `MISSING_CLOSE_TASK` **only** if it appears strictly after that agent's latest `plan` event in room order (earlier `close` records from previous runs never mask a new unclosed `plan`). If `state.closed_agents` claims an agent is closed while the room store lacks a matching post-plan `close`, the reconciler flags both `STATE_ROOM_MISMATCH` and `MISSING_CLOSE_TASK` (`rc = 1`).
2. **Negative Control (`MISSING_CLOSE_TASK`)**: If any agent emitted `post_plan` (or `enqueue`) without a matching `close_task` in the room store, flags `MISSING_CLOSE_TASK` (`rc = 1`).
3. **Per-Event Reconcile Tracking (m6)**: Uncertain events (`INPUT_UNCERTAIN`, `OUTCOME_UNCERTAIN`) are cleared only by an explicit `control op="reconcile"` occurring in the stream. `control op="resume"` does not clear uncertain events. Unreconciled events are flagged with `UNRECONCILED_UNCERTAIN_EVENT`.
4. **Refusal Ref Verification (`MISSING_REF_ON_REFUSAL`)**: Flags any refusal or park lacking non-empty `--ref`.
5. **Clean State**: When all checks pass and no anomalies exist, exits `rc == 0`.

---

## 5. CLI Usage Examples

### Run One-Shot Projection
```bash
python3 -m swarm.projection.projector \
  --events-file swarm/run/events.jsonl \
  --state-file swarm/run/projection_state.json \
  --once
```

### Run Daemon Follow Mode
```bash
python3 -m swarm.projection.projector \
  --events-file swarm/run/events.jsonl \
  --state-file swarm/run/projection_state.json \
  --follow
```

### Finalize Run and Execute Reconciler
```bash
python3 -m swarm.projection.projector \
  --events-file swarm/run/events.jsonl \
  --state-file swarm/run/projection_state.json \
  --finalize
```

### Direct Reconciler Execution
```bash
python3 -m swarm.projection.reconciler \
  --events swarm/run/events.jsonl \
  --cw-home ~/.context-wall \
  --cw-room viewpoint
```

---

## 6. Verification & Test Suite

All tests execute in isolated sandbox cgroups (`PYTHONDONTWRITEBYTECODE=1`):
```bash
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
  env PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s swarm/projection -p 'test_*.py' -v
```

Coverage (`test_projection.py`, 29 tests):
- `TestOsbActMapping`: §7.1 OSB act mappings (`open`, `claim`, `handoff`, `ack`, `evidence`, `refute --ref`, `verdict`) and `PASS`/`cursor` skip rules.
- `TestTaskLightRealMcp`: Subprocess `bridge.room.mcp` verification across all 4 tracked agents.
- `TestCursorStatePersistence`: Incremental byte-offset persistence preventing duplicate posts.
- `TestReconcilerNegativeControl`: `MISSING_CLOSE_TASK` negative control and `UNRECONCILED_UNCERTAIN_EVENT` detection.
- `TestEndToEndProjectorPipeline`: Full capsule lifecycle + `--finalize` reconciliation.
- `TestMandatoryRepairRegressions`: H4_r1 regressions (`M1`, `M2`, `m1`, `m3`, `m4/m2`, `m5`, `m6`, `m8`, `n6`).
- `TestH4RepairLoop2Regressions`: H4_r2 regressions (`Probe STALE` R1 dry-run & real MCP, `r1-5` exception isolation, `Probe P3` DEL/surrogate/per-record guard, `Probe P4b` binary framing across 3-byte/4-byte UTF-8 splits).
