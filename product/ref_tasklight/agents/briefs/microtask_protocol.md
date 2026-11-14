## Microtask protocol (tm1): plan first, then one microtask at a time

You are `{{AGENT_ID}}`. You may not write an intent until your plan is on disk, and every
intent must cite the microtask it executes. An intent without `tm.microtask_id` is refused
(`MICROTASK_MISSING`), and so is one whose microtask is not in your plan (`MICROTASK_UNKNOWN`),
is already closed (`MICROTASK_NOT_OPEN`), or arrives while another of yours is in flight
(`MICROTASK_BUSY`).

1. PLAN. Write `steps.json`: a list of 2 to 8 microtasks, each small enough to verify alone:
   `{"text": "<what the step does, <= 72 chars>", "expect": "<what must be true afterwards>",
     "lane": "script"|"gui", "body": "<bpy source>" | ["KP_1", ...],
     "after": ["<microtask_id of another agent you need first>"]}`.
   Then run `python3 {{TOOLS}}/mt.py plan --agent {{AGENT_ID}} --rt {{RT}} --goal "<your task>" --steps steps.json`.
   It writes your task `t.{{AGENT_ID}}.1` and its microtasks `t.{{AGENT_ID}}.1.m1..mN`.
2. ACT. Run `python3 {{TOOLS}}/mt.py next --agent {{AGENT_ID}} --rt {{RT}}`. It reads the broker's
   records, updates your plan, and enqueues the ONE runnable microtask as an intent (it adds the
   `tm` object and the newest `based_on_frame` for you). It prints one JSON line:
   `{"enqueued": ...}`, `{"wait": "in_flight"|"handoff", ...}`, or `{"task": "done"|"blocked"}`.
3. READ THE RESULT. Run `next` again after the broker closes your event. Per microtask:
   - `verified`: go on; the next microtask is enqueued by the same call.
   - `rejected` or `rolled_back` (the scene was restored): the tool has already added a NEW
     microtask that supersedes it, with the same body. If the same body would fail again, look at
     the newest frame and replace the body first:
     `python3 {{TOOLS}}/mt.py revise --agent {{AGENT_ID}} --rt {{RT}} --microtask <new id> --body-file fix.py`.
     Never re-send the old microtask. After 2 retries of one step the task ends `blocked`.
   - `queued` after `STALE_FRAME`: nothing to do, the broker serves the same event again.
   - `blocked`: stop; report the reason in your final message. Do not work around it.
4. Never edit `{{RT}}/plans`, `{{RT}}/tasks` or `{{RT}}/events.jsonl` by hand, and never write an
   intent file yourself in this mode.
5. TaskLight (only if the `context-wall` MCP server is configured; room from your brief, tests use
   `demo`): `post_plan` once after step 1 with the microtask texts, `post_step(index, "active")`
   when `next` enqueues, `"done"` or `"blocked"` when it reports the result, `post_plan` again
   with the full list after a re-plan, `close_task` at the end. Skip this if the projector does it.
6. Final message, one line: task state and `microtask_id=state` for every microtask.
