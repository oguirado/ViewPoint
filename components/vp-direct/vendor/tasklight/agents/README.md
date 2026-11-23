# product/ref_tasklight/agents: agents that plan with tasks and microtasks (lane tl-agents)

Written by Claude (claude-fable-5-2), Track P: declared non-Gemini product work. Stdlib only.
Schema: **tm1** (`../model/SCHEMA.md`, frozen by lane tl-model; `../model/schema.py` is imported,
never redefined). `product/ref_agents` and `../model` are imported and not edited.

## What an agent does

1. **Plan first.** It writes its task `t.<agent>.1` and a list of microtasks `t.<agent>.1.m<k>`
   (text, expect predicate, `after` dependencies) to `RT/tasks/<agent>/<task_id>.json` (tm1 `rec:"task"`
   records, one per line: open / plan / close) and its working plan to `RT/plans/<agent>/<task_id>.json`.
   No intent can be written before that (`PlanMissing`).
2. **One microtask at a time.** `next_intent()` enqueues the single runnable microtask as an F1/rw1
   intent carrying the tm1 object `tm` (`task_id`, `microtask_id`, `text`, `accept`, `after`, `supersedes`).
   Runnable = nothing of this agent in flight and every `after` entry satisfied in the model
   (own predecessor verified; handoff from another agent verified).
3. **Update from the broker.** `poll()` feeds new `events.jsonl` lines to `schema.Model` and adopts the
   model's state of each microtask (close, txn rollback, `control rewind` rule R, requeue, DEP_FAILED).
4. **Re-plan.** `rejected` or `rolled_back` creates a NEW microtask (next `m<k>`, `supersedes` = the failed
   one, appended to the task file as `op:"plan"`). After 2 retries of one step [TARGET], or on a parked /
   uncertain microtask, the task ends `blocked` and is closed `failed: ...`. `STALE_FRAME` / `TURN_TIMEOUT`
   keep the same microtask queued (the broker serves the same event again).

| file | what it is |
| --- | --- |
| `protocol.py` | the planning gate `validate_planned_intent`: `tm` + plan checks here, the rest by `ref_agents/contract.py` |
| `planner.py` | `Plan`: microtask list, sync with the tm1 model, re-plan, task file records |
| `planned_agent.py` | `PlannedAgent` + the deterministic stub CLI (steps from `ref_agents/stub_agent.py` PLAYBOOK) |
| `run_planned.py` | `ref_agents/runner.py` with planning agents: `--stub` starts `planned_agent.py`; real mode appends the protocol to the agy brief |
| `briefs/microtask_protocol.md` | the section a real agy agent prompt gets |
| `mt.py` | the shell tool that section tells the agent to use: `plan`, `next`, `revise`, `show` |
| `fake_broker.py` | TEST DOUBLE of the broker: scripted verdicts, F1 + rw1 record shapes, stamps `microtask_id`, copies task files into the log |
| `tests/test_tl_agents.py` | 17 tests |
| `evidence/live_stub/` | one run of three stub agents through the unmodified `ref_agents` runner |

## Run

```bash
S="systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0"
cd product/ref_tasklight/agents
(cd tests && $S python3 -m unittest test_tl_agents)

RT=/dev/shm/vqa-cu/ref-tlagents-live          # a frame must exist in RT/frames before the agents start
$S python3 fake_broker.py --rt $RT --policy '{"t.props-agent.1.m1":["JUDGE_REJECT"]}' &   # or a real broker
VP_TL_AGENT_ARGS="--rewind --deps" $S python3 run_planned.py run --stub --init-rt --rt $RT \
    --evidence evidence/x --agents modeler-agent,props-agent,camera-agent
python3 ../model/schema.py audit $RT/events.jsonl
```

Stub flags: `--rewind` adds rw1 `touches` and `{probe, equals}` expect predicates (objects exist in the
agent's collection); `--deps` plans the handoffs props m1 after modeler m3, camera m1 after modeler m1.

## A real agy agent

`run_planned.py run` without `--stub` builds `ref_agents` brief + `briefs/microtask_protocol.md`
(placeholders `{{AGENT_ID}}`, `{{RT}}`, `{{TOOLS}}` filled). The agent writes `steps.json`, calls
`mt.py plan`, then loops `mt.py next`; the tool owns ids, the `tm` object, `based_on_frame` and the plan
files, so the model only authors step text, expect and bpy bodies, and revises a body after a rollback
(`mt.py revise`). The section sits after the per-session tail, i.e. after `<!-- CACHE_BREAKPOINT -->`;
moving it into `ref_agents/briefs/_shared_prefix.md` (cached, shared file) is a decision for that file's owner.

## Verified (17 tests, all with the fake broker; no model, no Blender)

- plan and task file on disk before any intent; an agent without a plan cannot enqueue (negative)
- one microtask in flight per agent; handoff order props m1 after modeler m3
- three agents end `done`; `schema.fold(events).violations == []`; every enqueue/turn/close/txn/judge carries `microtask_id`
- rollback (`JUDGE_REJECT` -> `ALL_REJECTED`) and plain rejection (`CLAIM_REFUSED`) each produce a superseding microtask that is then verified; the dependent step unblocks
- three failures of one step: task `blocked`, no fourth attempt, the later step never enqueued
- `STALE_FRAME`: same microtask, `drops` 1, no new microtask
- director rewind to `p1`: verified m2 becomes `rolled_back` (rule R), m1 stays, m4 supersedes m2
- **negative control**: an intent without `tm.microtask_id` is refused `MICROTASK_MISSING` by the validator and by the broker gate (no enqueue/turn/txn record, tick 0, no new frame). With the check removed both tests fail (mutation run by hand, 2 failures); `test_mutant_gate_off_dispatches` shows the same intent dispatched when the gate is off
- unknown / foreign / closed microtask, missing plan, foreign write_set refused; `mt.py` plan-next-replan loop; `mt.py plan` refuses a body that names another agent's collection

## Not verified / gaps

- No run against the real `ref_broker` (+ auction + rewind): it is not in this worktree. The F1 validator refuses unknown fields (`tm: unknown field`, tested), so a real broker needs the gate of `protocol.py` in front of its `_validate` and must stamp `microtask_id` and copy task files; `fake_broker.py` is only a stand-in for that.
- No real agy run of the microtask protocol; the prompt section and `mt.py` are tested with scripted calls only.
- The task file as JSON lines in `RT/tasks/<agent>/<task_id>.json` is this lane's reading of tm1 section 2 (it shows three records for one file); not confirmed by tl-model.
- The stub re-plans with the same body (it has no model to write a different one); `fallbacks` in a step and `mt.py revise` are the hooks for a different body.
- A rollback that arrives after `task close` is not re-planned (tm1 forbids planning in a closed task; opening `t.<agent>.2` is not implemented).
- `control stop` only pauses enqueueing; no TaskLight MCP call is made by this lane (projection is tl-model / tl-live).
