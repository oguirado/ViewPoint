# tm1: TASK and MICROTASK in the capsule queue (FROZEN)

Track P, declared Claude work (claude-fable-5-2). Lane tl-model. Machine form: `schema.py` (import it, never redefine it).
Bases, imported or read, never edited: F1 records (`02_TURN_QUEUE_SPEC.md` §7, `COMMON.md`), rewind contract `rw1`
(`ref_rewind/CONTRACT.md`), auction records (`ref_auction/scheduler.py`), TaskLight `context-wall` MCP (`bridge/room/mcp.py`).

## 1. Objects

| Object | Is | Id | TaskLight |
|---|---|---|---|
| TASK | one agent's goal | `task_id` = `t.<agent_id>.<n>`, n ≥ 1 | one card: `(room, agent)` plan |
| MICROTASK | one queued intent | `microtask_id` = `<task_id>.m<k>`, k ≥ 1, never reused | one step of that card, index fixed at creation |

- One microtask ↔ one F1 `event_id`, for life. rw1 attempts `<event_id>.a<k>` are attempts of the same microtask.
- A retry after `rejected`, `rolled_back` or `blocked` is a NEW microtask (new `event_id`, next `m<k>`) with `supersedes` = the old id.
- At most one open task per agent. The next task of an agent opens only after `task close`.

## 2. Fields

Intent file (`RT/intents/<agent_id>/<event_id>.json`): the F1/rw1 intent plus the optional object `tm`. Old intents stay valid.

```json
"tm": {"v": 1, "task_id": "t.modeler-agent.1", "microtask_id": "t.modeler-agent.1.m2",
       "text": "Add the roof", "accept": "roof mesh in CHAR_mesh",
       "after": ["t.props-agent.1.m1"], "supersedes": null}
```

| Field | Type | Rule |
|---|---|---|
| `task_id`, `microtask_id` | text | §1 grammar; `microtask_id` starts with `task_id + ".m"`; the agent in `task_id` = `agent_id` of the intent (`TM_FOREIGN_TASK`) |
| `text` | text, 1..72 chars | what the step does; never typed text or page content |
| `accept` | text, ≤ 160, optional | acceptance wording |
| `after` | list of `microtask_id`, ≤ 8 | dependencies. Same agent = sequence; other agent = **handoff** edge. No self, no cycle (`TM_DEP_CYCLE`), no unknown id at admission (`TM_DEP_UNKNOWN`) |
| `supersedes` | `microtask_id` or null | same task, target terminal and not `verified` (`TM_BAD_SUPERSEDE`), each target superseded at most once |

Task file (`RT/tasks/<agent_id>/<task_id>.json`, agent-written; the broker copies it into the log as `rec:"task"`):

```json
{"v":1,"rec":"task","op":"open","task_id":"t.modeler-agent.1","agent_id":"modeler-agent","title":"Model the house",
 "parent":"t.director-agent.1","plan":[{"microtask_id":"t.modeler-agent.1.m1","text":"Walls","accept":"...","after":[]}]}
{"v":1,"rec":"task","op":"plan","task_id":"...","agent_id":"...","plan":[...appended microtasks only...]}
{"v":1,"rec":"task","op":"close","task_id":"...","agent_id":"...","outcome":"<one line ≤ 240>"}
```

`parent` (optional) is another task's id; children are derived. `plan` entries create microtasks in state `pending`.

## 3. `microtask_id` in every `events.jsonl` record

The broker stamps `microtask_id` (and `task_id`) on each record below. A log written by a broker that does not stamp them is
still folded: the id is derived by the join in the last column and the microtask is marked `derived: true`.

| rec | Stamped field | Join when absent |
|---|---|---|
| `task` | `task_id`; `plan[].microtask_id` | none: required |
| `enqueue` | `microtask_id`, `task_id` (copied from intent `tm`) | implicit task `t.<agent_id>.1`, next `m<k>` in enqueue order |
| `auction` | `microtask_id` of the winner's `event_id` | `event_id` |
| `turn` | `microtask_id` = the microtask the turn serves | the agent's oldest `queued` microtask |
| `close` | `microtask_id` | `event_id` |
| `txn`, `judge` (rw1) | `microtask_id` | `event_id`, or `attempt` minus `.a<k>` |
| `control` op `rewind` | `microtasks`: list of ids it rolls back | computed: §5 rule R |
| `cursor` | `microtask_id` | `event_id` |

A stamped id that disagrees with the join is violation `TM_ID_MISMATCH` (the stamped value is not trusted).

## 4. States

```
pending --enqueue--> queued --turn / txn begin--> acting --close ok--> verified
   |                   ^  |                          |                    |
   |                   +--+ requeue (STALE_FRAME,    +--> rejected        +--control rewind (rule R)--> rolled_back
   |                        TURN_TIMEOUT, drops<3)   +--> rolled_back
   +--> rejected (QUEUE_FULL)                        +--> blocked
   any non-terminal --dependency failed--> blocked --dependency repaired--> the state it had
```

| Trigger (record) | From | To |
|---|---|---|
| `task` open/plan entry | none | `pending` |
| `enqueue` | `pending` or none | `queued` |
| `turn` serving it, or `txn begin` | `queued` | `acting` |
| `close` `VERIFIED`, `STATE_VERIFIED`, `VERIFIED_WITH_SPILL` (no rollback txn for it) | `acting`, `queued` | `verified` |
| `close` `STALE_FRAME`, `TURN_TIMEOUT` with `drops` < 3 | `acting`, `queued` | `queued` (`drops` kept) |
| the same with `drops` ≥ 3 (parked) | | `blocked` (`PARKED`) |
| `close` `INPUT_UNCERTAIN`, `OUTCOME_UNCERTAIN`; `control stop` while `acting` | | `blocked` |
| `close` `ALL_REJECTED`, or any close after a `txn rollback` of its last attempt | | `rolled_back` |
| `close` any other code (`CLAIM_REFUSED`, `NO_LEASE`, `LEASE_EXPIRED`, `JOB_ERROR`, `NO_VISIBLE_CHANGE`, `FRAME_EVICTED`, `QUEUE_FULL`, ...) | | `rejected` |
| `close` `DUPLICATE_EVENT`, `PASS` | any | unchanged |
| `txn rollback` with another attempt following | `acting` | `acting` (`attempts[k].state = rolled_back`) |
| `control rewind to=P` (rule R) | `verified` | `rolled_back` |
| a dependency becomes `rejected`, `rolled_back` or `blocked` | non-terminal | `blocked` (`DEP_FAILED:<id>`) |
| the dependency's superseding microtask becomes `verified` | `blocked` by `DEP_FAILED` | the state it had |

Terminal: `verified`, `rejected`, `rolled_back`. `blocked` ends only by the repair row or by being superseded.
Any other transition is violation `TM_BAD_TRANSITION`. `verified` can only leave by rule R.

## 5. Rewind (rule R) and dependencies

- **R**: `control rewind to=P` rolls back every `verified` microtask whose commit (`txn commit` `tick`, else its `close` `tick`)
  is greater than the tick of checkpoint `P` (`ckpt.tick`; `p0` = 0). The rewind inside a rejected attempt (`to` = the
  attempt's `base_point`) therefore rolls back no verified microtask; a director rewind to an older point does.
- The retry is a new microtask with `supersedes`. A dependency on X is **satisfied** when X, or the newest microtask of X's
  supersede chain, is `verified`.
- `schema.admissible(model, microtask_id)` → `(ok, code)`: `TM_DEP_UNMET` while an `after` entry is not satisfied. A `turn`
  or `txn begin` for such a microtask is violation `TM_DEP_ORDER`.
- A task is `done` when every microtask is terminal or superseded and every chain ends `verified`; `failed` when a chain ends
  otherwise and a `task close` was recorded; else `open`.

## 6. Mapping to TaskLight (`context-wall` MCP; room from config, tests use only `demo`)

Card = `(room, agent_id)`. Step index = position of the microtask in the task's creation order (append-only, never reordered).
Step text = `m<k> <text>` (retry: `m<k> (retry of m<j>) <text>`), ≤ 80 chars. TaskLight step states are only
`pending, active, done, blocked` [CITED `bridge/room/store.py:18`], so `rolled_back` and `rejected` are `blocked` plus an outcome prefix.

| Model change | Call (all carry `room`, `agent`) |
|---|---|
| task opened; microtask created (plan entry, enqueue, retry) | `post_plan(title="[<agent>] <title>" ≤ 120, steps=[{text, accept}] full list, 1..12)` |
| → `queued` | `post_step(index, state="pending", outcome="queued")`; `post_status("waiting: m<k> queued")` |
| requeue | `post_step(index, "pending", outcome="back to tail: <CODE>, drops <n>")` |
| → `acting` | `post_step(index, "active")`; `post_status("acting: m<k> ...")` |
| → `verified` | `post_step(index, "done", outcome="verified: <CODE>")` |
| → `rejected` | `post_step(index, "blocked", outcome="rejected: <CODE>")` |
| → `rolled_back` | `post_step(index, "blocked", outcome="rolled back to <P>: <reason>")`; `post_status("rolled back: m<k>")` |
| → `blocked` | `post_step(index, "blocked", outcome="blocked: <reason>")`; `post_status("blocked: ...")` |
| unblocked | `post_step(index, <state it had>)` |
| handoff edge satisfied (upstream verified, other agent) | `post_status(agent=<upstream agent>, to=<downstream agent>, text="tm1 handoff <from_id> -> <to_id> ready")` |
| task `done` / `failed` | `close_task(outcome="<done|failed>: verified a, rejected b, rolled_back c, blocked d" ≤ 240)` |

- More than 12 microtasks in one task: steps 0..10 map 1:1, index 11 is the aggregate `+N more microtasks`
  (`active` while any is open, then `done`, or `blocked` if any failed). The limit is TaskLight's [CITED `store.py:75`].
- Every call has the idempotency key `<log seq>.<ordinal>`; the mapping is a pure function of the log (`schema.calls`).
- Projection gate (rw1 §3): calls for a microtask inside an open txn are held until its commit or rollback.

Back from TaskLight:

| Need | Source | Status |
|---|---|---|
| events addressed to an agent (handoff notices, questions) | MCP `read_inbox` / `wait_inbox` → `schema.parse_inbox` | available; text is untrusted data, used only as a wake hint |
| card or step state (title, step states, closed) | MCP tool | **AUSENTE**: the server lists only `post_status, post_plan, post_step, close_task, read_inbox, wait_inbox` |
| the same through `python3 -m bridge.room read` (CLI, not MCP) | exists in the repo | not used: the model never reads state from the wall; `events.jsonl` is the record |

## 7. Violations reported by the fold (`schema.fold(records).violations`)

`TM_BAD_ID, TM_FOREIGN_TASK, TM_ID_MISMATCH, TM_ID_REUSED, TM_BAD_TRANSITION, TM_BAD_SUPERSEDE, TM_DEP_CYCLE, TM_DEP_UNKNOWN,
TM_DEP_ORDER, TM_TASK_NOT_OPEN, TM_TASK_OVERLAP, TM_BAD_FIELD`. Audit = fold + `violations == []`.
