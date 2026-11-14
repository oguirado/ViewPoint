# ref_demo: the integrated Track P demo stack in ONE command (lane int-demo)

Track P, declared non-Gemini product work. Written by claude-fable-5-2. Stdlib Python + bash only.
Branch `p-fable-demo` = `p-fable-r4` (r0..r4) + merges of `p-fable-au`, `-f2`, `-f3`, `-f4`, `-ct`, `-ai2`, `-ai3`
and of the fix commits that landed on `p-fable-r1/-r2/-r3/-au/-f2/-f3/-f4/-ct/-ai2/-ai3` until 13:36 CET, 14 Nov 2026.
All merges were `--no-ff` and conflict-free. No unit file is edited here.

## Run

```sh
product/ref_demo/run_demo.sh [tag] [--no-auction] [--no-settle]   # evidence -> product/ref_demo/evidence/<tag>/
python3 -m unittest test_demo_stack                                # in product/ref_demo, inside the usual systemd-run scope
```

`run_demo.sh` puts everything in one `systemd-run` scope (MemoryHigh 6G, MemoryMax 8G, no swap) and runs `demo_live.py`:

| Stage | Unit used | What happens |
|---|---|---|
| Blender | `ref_broker/bridge.py`, vision | private Blender 4.0.2, vision job `vpintdemo`, 1920x1080, RT `/dev/shm/vqa-cu/ref-int-demo-<tag>` |
| Broker | `ref_rewind_lanes` RewindBroker + `ref_rewind_ckpt` + `ref_rewind_verify` judge | rewind layer, checkpoints, judge |
| Scheduler | `ref_auction` | `AuctionScheduler` fills the `_pick_agent` seam; the seam is applied to `ref_broker/broker.py` in memory (`broker_adapter.patched_source`), the file stays byte-identical |
| Scenario | `ref_rewind_demo/rewind_live.scenario` (imported) | modeler `m1` commits; props `e1.a1` is the deliberate wrong edit: judge rejects it, the capsule rewinds to `p1`; `e1.a2` commits; camera `g1` goes `STALE_FRAME`, `g2` commits |
| Recording | `ref_overlay/record.sh` | x11grab of the job's own display (found from the Blender process environment; `:1` and `:2` refused) |
| Overlay | `ref_overlay/compose.py` | cursors + queue strip over the recording -> `demo_composed.mp4` |
| Projection | `ref_projection/project.py --once` | OpenSwitchboard room `viewpoint-capsule-test`, TaskLight room `demo` |
| Stub agents | `ref_agents/runner.py run --stub` | 4 scripted agents, 2 intents each, in their own RT `...-<tag>-agents`, seeded with the last accepted live frame |

Exit 0 = 21 of 21 scenario checks hold, compose rc 0 and projection rc 0. The vision job is always stopped.

## Live runs (14 Nov 2026, this host) [MEASURED]

| Run | Stack | Result |
|---|---|---|
| `live1`, `live3` | first merge wave, no settle | 21 of 21, rc 0 |
| `flake_live2`, `flake_live4` | same, no settle | 20 of 21: `3_viewport_frame_equals_p1` RED (`WITNESS_NOT_RESTORED in=1 first=[0,48,1584,936]`) |
| `live5`, `live6`, `live7` | all fix merges until 13:30 + settle | 21 of 21, rc 0 |
| `live8` | final tree (plus `p-fable-r2` fixes of 13:36) + settle | 21 of 21, rc 0 |

Every run: 5 `auction` + 5 `auction_settle` records for 5 turns (policy `vickrey_aging_v1`), 4 `judge` records (one
`pick: false` on `e1.a1`), one `control rewind to=p1 reason=JUDGE_REJECT digest_match=true`, `rewindctl audit` rc 0,
projection 53 lines with 0 osb errors and 0 TaskLight errors, 8 stub intents. `live7/demo_composed.mp4`: 1920x1080, about 48 s.
Each evidence directory holds `events.jsonl`, `cursor.jsonl`, `ckpt_manifest.jsonl`, `frame_p0..p3.png`,
`frame_e1a1_wrong.png`, `frame_restored_p1.png`, `rewind_live.mp4`, `demo_composed.mp4`, `projection/`, `stub_agents/`,
`live.json`, `console.log` (the two flake directories keep no mp4).

## Defect found by the integration and fixed here

The frame the broker takes right after a restore job can show Blender before its redraw (blank viewport, stale outliner:
`evidence/flake_live2/frame_restored_p1.png`) although the scene digest already matches; the rollback witness then fails.
It showed in 2 of 4 runs with the display recorder on. `demo_live.wire_settle` subclasses the RewindBroker so that the
capture of a `ctl.restore.*` job first waits for two identical consecutive looks (`settle`, bounded at 8 looks).
After: 3 of 3 runs green. Tests `test_demo_stack.Settle` (4, with the negative control `tries=0`). The proper place for
this wait is `ref_rewind_lanes/rewind_broker.py` (owner R2); it is not edited from this lane.

## Tests

`test_demo_stack.py`, 11 tests: refusal of a foreign RT or job; auction wired into the RewindBroker while `broker.py`
on disk has no seam; settle (4); the newest `evidence/live*` log is green, has the judge reject and the rewind, one
auction record per turn, `rewindctl audit` rc 0, artifacts present.

Merged unit suites on this branch after the last merge [MEASURED]: `ref_broker` 34, `ref_rewind` 29, `ref_rewind_ckpt` 49,
`ref_rewind_lanes` 42, `ref_rewind_verify` 46, `ref_auction` 95, `ref_overlay` 41, `ref_projection` 52 (1 skipped, live),
`ref_rewind_demo` 21, `ref_capsule` 31, `agent_interface/mcp` 22, `ref_demo` 11: all pass. The `Untouched` tests of `ref_rewind_demo` and `ref_rewind_ckpt` compare against the unit branch tips and go red when a unit branch gets a commit that is not merged here yet, or when a `__pycache__` is committed.

## Not done / open

- The 4 stub agents write into their own RT; the live broker is fed by the scripted scenario, not by the stub agents.
  No real agy agent was run.
- `look-agent` takes no turn in the scenario (3 agents act live).
- `ref_agents` `test_hardening.BridgeSlice.test_budget_stop_through_the_real_bridge` fails here and in its own worktree
  `vp-wt-fable-f4` (`ran == []`): not caused by the merge, owner f4.
- `agent_interface/cli` `test_defaults_follow_the_checkout` fails only in the merged tree: it expects the sibling
  worktree path `/srv/ws/vp-wt-fable-ct/product/ref_capsule`, while `vp` now finds `product/ref_capsule` in the same
  checkout. Owner ai2 decides which is right.
- `ref_auction/judge` tests do not load with `unittest discover` from inside `judge/` (relative imports); they run from `ref_auction`.
- Projection posts go to the real rooms on every run (53 lines per run); the TaskLight wall UI was not opened.
- The settle fix was verified on 3 runs only.
