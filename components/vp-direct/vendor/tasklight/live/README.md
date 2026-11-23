# ref_tasklight/live: live proof of tasks and microtasks (lane tl-live)

Track P, declared Claude work (claude-fable-5-2). Stdlib Python. Schema: `../model/SCHEMA.md` (tm1).

| File | Role |
|---|---|
| `tl_live.py` | One live run: private Blender (vision job `vptl`, 1920x1080), F1 bridge + broker, auction scheduler, rewind broker + judge (imported read-only from `--stack`, default the p-fable-demo worktree), 3 stub agents, projection, checks, evidence. |
| `microtasks.py` | Tasks/microtasks of the stubs and the fold of `events.jsonl` into tm1 states (the subset the proof exercises; join by `event_id`). |
| `tl_project.py` | Transition -> OpenSwitchboard act + TaskLight `context-wall` MCP call (table in the docstring), through `ref_projection`'s sinks. |
| `tests/test_tl_live.py` | 6 offline tests, 3 negative controls. |
| `evidence/live1/` | The run of 2026-11-14. |

Run (room `demo` and `viewpoint-capsule-test` by default; TaskLight room `viewpoint` is refused):

```
export XDG_RUNTIME_DIR=/run/user/1000
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
  python3 tl_live.py --rt /dev/shm/vqa-cu/ref-tl-live-<tag> --evidence evidence/<tag>
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 python3 -m unittest discover -s tests
```

## Scenario

| Agent (wall id) | Task | Microtasks |
|---|---|---|
| modeler-agent (`tl-modeler-agent`) | Model the character | m1 body; m2 head (after m1) |
| props-agent (`tl-props-agent`) | Put a table under the character | m1 table, deliberately wrong (10x, moves the body), after modeler m1 (handoff); re-planned as m2 `supersedes` m1 |
| camera-agent (`tl-camera-agent`) | Frame the scene from the front | m1 numpad 1 (gui lane), after props m1's chain and modeler m2 |

Each agent posts its plan first, then enqueues one microtask at a time as a contract intent with the `tm` object;
state changes come only from broker records. look-agent, the 4th ring seat, has no stub.

## Verified (ran 2026-11-14, `evidence/live1/live.json`: 19 checks, 0 red)

- props m1: `pending, queued, acting, rolled_back`, with the judge reject (`EXPECT_MISMATCH`, `DELTA_OUTSIDE`) and a
  `control rewind` to p1 with `digest_match: true` in `events.jsonl`; m2 (retry of m1): `verified`, committed.
- Rewind audit clean; final probe: only accepted work in the scene; auction order respected the handoffs.
- TaskLight room `demo`: 39 MCP calls, all acknowledged (`tasklight_mcp_responses.jsonl`); second `post_plan` of
  props appends the retry step; step 0 `blocked` with outcome `rolled back to p1: ...`; `close_task` for 3 agents;
  handoff `post_status(to=...)` read back with MCP `read_inbox`; the wall store read back (`tasklight_state_dump.json`).
- OpenSwitchboard `viewpoint-capsule-test`: 25 posts, rc 0, read back from the room (`osb_room_readback.jsonl`).
- Blender stopped by `vision stop --job vptl` (rc 0); no process with this RT remains.
- Negative controls checked by mutation in a scratch copy: digest-proof guard removed -> 1 test red; transition
  table guard removed -> 1 test red.

## Not verified / gaps

- No screenshot of the TaskLight wall: the running wall is the live demo wall (`--viewpoint`); it was not touched.
  The state dump stands in for it; how the cards render is not verified.
- The reference brokers do not stamp `microtask_id` on turn/close/txn/judge and do not copy `RT/tasks/*` into the
  log as `rec:"task"` (tm1 sections 2-3): ids are joined by `event_id`; the task files are in `evidence/live1/tasks/`.
- `microtasks.py` is this lane's own fold, not tl-model's `schema.py` (absent when the lane ran); not cross-checked
  against it. The tl-agents planner is not used: the stubs are in-process and deterministic, they read the frame
  number from the broker object.
- tm1 calls not exercised live: requeue, blocked/unblocked by dependency, rule R on verified microtasks, > 12 steps,
  idempotency keys `<seq>.<ordinal>` and the projection gate (calls here are made after each broker step).
- One run, one scheduler (auction); `--no-auction` not run.
