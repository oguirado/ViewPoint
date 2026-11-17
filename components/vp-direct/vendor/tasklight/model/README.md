# ref_tasklight/model (lane tl-model): TASK and MICROTASK in the capsule queue

Track P, declared Claude work (claude-fable-5-2). Stdlib Python only. Contract: `SCHEMA.md` (`tm1`, frozen; sha256 posted
in room `viewpoint-tasklight`, event `fable-tl-model-2`).

| File | Role |
|---|---|
| `SCHEMA.md` | frozen contract: ids, intent field `tm`, `rec:"task"`, stamping rule per record, states, rewind rule R, TaskLight mapping and read-back |
| `schema.py` | ids and `validate_tm` / `stamp_intent`; `Model` + `fold` (events.jsonl → tasks, microtasks, violations); `admissible`; `stamp_record` (what a tm1 broker appends); `Wall` + `calls` (pure mapping to context-wall MCP calls); `parse_inbox`; CLI |
| `push.py` | sends `schema.calls` to the real context-wall MCP server (client imported from `product/ref_projection`), reads the inbox back; refuses room `viewpoint` |
| `tests/test_model.py` | 29 tests, 5 negative controls, 4 against the real MCP server with a private `CW_HOME` |
| `tests/fixtures/` | `events_rewind.jsonl`, `events_director_rewind.jsonl` (synthetic, stamped) |
| `evidence/` | live run in TaskLight room `demo`; audits of two real R2 broker logs |

## Run

```sh
cd product/ref_tasklight/model
python3 -I schema.py audit <events.jsonl>        # rc 1 when the fold reports violations
python3 -I schema.py state <events.jsonl>        # tasks + microtasks as JSON
python3 -I schema.py calls <events.jsonl> demo   # the TaskLight calls, one JSON per line
python3 push.py <events.jsonl> --room demo --out responses.jsonl --inbox-agent look-agent
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 python3 -m unittest discover -s tests -t .
```

Agent side: `schema.stamp_intent(intent, task_id, k, text, accept, after, supersedes)`.
Broker side: `schema.stamp_record(model, rec)` then `model.feed(rec)` (for `enqueue`: feed first, then stamp);
`schema.admissible(model, microtask_id)` before a dispatch.

## Verified

| Claim | Where |
|---|---|
| A rejected attempt that is rewound shows the microtask as `pending, queued, acting, rolled_back`; the retry is a new microtask with `supersedes`, a new `event_id`, and ends `verified` | `test_rewind_shows_rolled_back_then_new_microtask`; mutant `test_nc_rewind_mutant_goes_red` |
| Director rewind to an older point rolls back exactly the verified microtasks committed after it (rule R); the attempt-level rewind rolls back none | `test_director_rewind_rule_R`, `test_attempt_rewind_does_not_touch_verified`; mutant `test_nc_rule_R_mutant_goes_red` |
| Handoff: a microtask of another agent is `blocked` by the failed dependency, returns to `queued` when the retry is verified, and the notice reaches the downstream agent's inbox | `test_full_scenario_states_tasks_handoff`, `RealContextWall.test_server_accepts_every_call_and_inbox_round_trip` |
| Serving a microtask before its dependencies is reported | `test_dependency_order_and_admissible`; mutant `test_nc_dependency_mutant_goes_red` |
| A stamped id that disagrees with the join is reported, also on `control rewind` | `test_stamped_id_that_disagrees_is_a_violation`, `test_rewind_stamp_must_match_rule_R` |
| Logs of a broker that stamps nothing are folded by the join | `test_unstamped_log_is_joined`, `test_unstamped_turn_guess_is_corrected`, `evidence/audit_real_r2_logs.jsonl` (2 real logs, 0 violations, 0 unjoined) |
| Exact TaskLight call sequence for the rewind; pure function of the log; unique keys | `test_exact_calls_for_the_rewind` |
| No `done` reaches the wall while the txn is open | `test_gate_holds_calls_of_an_open_txn`; mutant `test_nc_gate_mutant_goes_red` |
| The real server accepts every generated call, including 15 microtasks in one task | `RealContextWall`; mutant `test_nc_tasklight_limit_mutant_goes_red` |
| The real server refuses `rolled_back` as a step state and lists no state-read tool | `test_nc_server_refuses_rolled_back_as_a_step_state`, `test_state_read_tool_is_absent` |

Live: `evidence/live_demo_summary.json` (35 calls, 35 accepted, room `demo`, handoff read back through `read_inbox`),
`evidence/live_demo_responses.jsonl` (every call with the server's response).

## Clarifications to SCHEMA.md (no field changed)

- Agent ids inside `task_id` have no dot (`[a-z0-9][a-z0-9_-]{0,47}`), so the id parses one way only.
- `control resume` returns microtasks blocked by `control stop` to the state they had.
- `close_task` is sent on `task close`, or by `Wall.finalize()` at the end of a run for every task whose microtasks are all
  terminal or superseded.
- An unstamped `turn` is joined to the agent's oldest queued microtask; if the broker then serves another one, the guess is
  undone (`requeue`, reason `turn served <id>`). A stamped `turn` needs no guess.
- When a task passes 12 microtasks, step 11 changes from the 12th microtask to the aggregate.

## Not done

- No broker stamps `microtask_id` yet: the F1/R2/auction brokers are other lanes' code. Until one calls `stamp_record`, ids
  on `turn`, `close`, `auction`, `txn`, `control rewind` come from the join.
- `rec:"task"` is not written by any broker yet (`RT/tasks/<agent_id>/<task_id>.json` is defined, not consumed). Without it,
  tasks are implicit (`derived: true`, title = task id).
- Card and step state cannot be read back over MCP: AUSENTE in the server. Only inbox events are read.
- A rewind that rolls back microtasks of a task already closed on the wall posts step and status updates; whether the wall
  reopens a closed card was not checked.
- Not run against a live Blender capsule; the lane tl-live owns that run.
