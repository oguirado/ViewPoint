# ref_rewind_lanes: transactional multi-lane queue for the rewindable Blender capsule (unit R2)

Track P, declared non-Gemini product work. Written by claude-fable-5-2. Stdlib Python only.
Plan: `rw-build.md` §3 "R2". Contract: `product/ref_rewind/CONTRACT.md` (rw1, frozen, imported).
Base: `product/ref_broker` @ `b255057`, imported and subclassed, not edited (`test_lanes.F1Contract.test_f1_untouched`).

| File | Role |
|---|---|
| `rewind_broker.py` | `RewindBroker(Broker)`: per-event transaction (attempts, commit/rollback), `scene_epoch`, crash recovery, director `rewind`, commit frontier (`export_point`, `projection`), `snapshot`. |
| `test_lanes.py` | 35 unit tests: real F1 broker + real F1 bridge core on a thread, R0 fakes for scene/vision/store/judge. No Blender. |
| `lanes_live.py` | Live run against a real Blender under a vision job, with R1 ops/store and R3 judge loaded from their directories. |
| `evidence/` | Output of the live run (events, cursor records, checkpoint frames, check list, console log). |

## Use

```python
sys.path.insert(0, "product/ref_rewind_lanes"); from rewind_broker import RewindBroker
b = RewindBroker(rt, job, nonce, ops=<R1 ops>, store=<R1 Store(rt)>, judge=<R3 judge>,
                 restore_cap_s=30.0, load_ui=True)       # other kwargs are F1 Broker's
b.step()                      # one turn; same return as F1 plus the codes below
b.rewind(b.rw.last_good)      # director control: {"ok","code","to","mode","scene_epoch"}
b.control("resume", "director", "...")   # refused (BrokerError RESTORE_PENDING) while a restore is outstanding
b.export_point(tick)          # {"ok","point","path",...} or {"ok": False, "code": "NOT_CONFIRMED"}
b.projection()                # room/TaskLight records, ticks <= confirmed_tick only
b.rw                          # schema.RewindState, folded on every append (same fold as `rewindctl audit`)
```

Tests: `python3 -m unittest -v test_lanes` from this directory.

## Behaviour

Per admitted event (F1 admission unchanged): `txn begin` (base = `last_good`, already marked by the previous
commit or the genesis `p0`) → F1 dispatch under the job name `<event_id>.a<k>` → F1 verdict → `ctl.digest` job +
frame + judge → `ctl.mark` job + `ckpt` + `txn commit`, or `ctl.restore` job + `control rewind` + `txn rollback`
and the next alternative from the same base. The F1 `close` carries the final verdict.

| Outcome of an attempt | What the broker does | F1 close verdict |
|---|---|---|
| judge `pick=true` | mark `pN`, `txn commit point=pN tick=T` | `STATE_VERIFIED` / `VERIFIED` (+ `attempt`, `point`) |
| `JOB_ERROR`, `VERIFIED_WITH_SPILL`, judge reject | restore(base), digest equal, rollback, next alternative | `ALL_REJECTED` when none is left |
| `OUTCOME_UNCERTAIN`, `INPUT_UNCERTAIN` | same; the attempt id is never reused; mode stays `IDLE` (no `control reconcile`, no human resume); a late outbox of the attempt is quarantined by F1 (`LEASE_EXPIRED`) | as above |
| `TURN_TIMEOUT`, `ACTUATOR_BUSY`, `NO_VISIBLE_CHANGE` | rolled back as `OUTCOME_UNCERTAIN` / `INPUT_UNCERTAIN` with `reason: "f1=<verdict>..."`; never requeued | as above |
| restore unauthenticated, timed out, digest mismatch, checkpoint corrupt | `control rewind digest_match=false` (epoch still +1), `control stop`, txn stays open | `RESTORE_UNAUTH` / `RESTORE_TIMEOUT` / `RESTORE_MISMATCH` / `CKPT_CORRUPT`; mode `FROZEN` |
| mark refused (`CKPT_BUDGET`, mark job failed) after an accept | restore(base), rollback `JOB_ERROR`, no further alternative | the store code |
| broker died with an open txn | on the next `step()`, before any dispatch: restore its base, `txn rollback OUTCOME_UNCERTAIN`; the crashed attempt index is consumed | next alternative or `ALL_REJECTED` |
| broker died between `txn commit` and the F1 `close` | the event is closed from the log (`recovered_commit: true`), not dispatched again | commit verdict |

- `FROZEN` is left only by `rewind(to=last_good, by="director")` followed by `control resume`. `resume` alone, a rewind
  to another point, or `by` ≠ `director` raise `BrokerError`.
- Any `control rewind` record: `scene_epoch + 1`, a fresh frame is captured, and every gui intent enqueued before
  it closes `STALE_FRAME` (F1 requeue/resubmit rules apply). Script intents stay queued.
- Agents: an intent with `op`, `rec: "control"` or a `ctl.*` event id closes `NO_LEASE` at ingest; other malformed
  extension fields close `BAD_INTENT` (`schema.validate_intent_ext`).
- Commit frontier: `export_point(tick)` and `projection()` answer only for ticks ≤ `confirmed_tick` with no open
  txn (`schema.exportable`). A tick that closed `ALL_REJECTED` projects with `committed: false`; its state is the
  previous committed point.
- Replay: all rewind state (`rw`, `scene_epoch`, the stale boundary, commits) is a fold of `events.jsonl`;
  `snapshot()` is equal before and after a restart.

## Tests and negative controls (35 tests)

| Guard | Positive test | Negative control (guard removed → property violated) |
|---|---|---|
| restore really restores | `Row1Rejects.test_rollback_restores_base_digest` | `test_nc_noop_restore_breaks_digest_equals_base` |
| epoch bump | `Row5Epoch.test_restore_makes_queued_gui_intents_stale` | `test_nc_epoch_bump_removed_dispatches_stale_gui`, `..._is_seen_by_the_audit` |
| frontier check | `Frontier.test_export_and_projection_stop_at_confirmed_tick` | `test_nc_frontier_check_removed_exports_unconfirmed_tick` |
| undo of an open txn | `Row4Crash.test_open_txn_is_undone_before_any_dispatch` | `test_nc_recovery_removed_leaves_the_effect` |
| agent cannot rewind | `NoLease.test_agent_cannot_submit_rewind` | `test_nc_ext_validation_removed_admits_op_rewind` |
| replay determinism | `Replay.test_rebuild_from_events_gives_the_same_state` | `test_nc_snapshot_detects_a_lost_record` |

Also checked by patching `rewind_broker.py` itself (digest check, epoch bump, frontier, intent check, pre-dispatch
undo, resume guard removed, restore made a no-op): 13 of 13 targeted positive tests went red.

## Live evidence (`evidence/`, 2026-11-14, Blender under vision job `vprw2`, RT `/dev/shm/vqa-cu/ref-rw2`)

`lanes_live.py` with R1 `ops`/`Store` and R3 `judge` loaded unmodified from their worktrees (uncommitted at the
time, `--ckpt-dir`/`--verify-dir`): exit 0, 16 of 16 checks (`evidence/live.log`, `live_lanes.json`, `events.jsonl`).

| Step | Observed |
|---|---|
| modeler `m1` | `STATE_VERIFIED`, commit `p1` |
| props `e1.a1` (table 10x, moves `CHAR_body`) | R3 judge reject `DELTA_OUTSIDE [col:CHAR_mesh]` → `control rewind to=p1 digest_match=true`, `scene_epoch` 1 → 2 |
| props `e1.a2` | commit `p2`; digest delta `p1`→`p2` = `["col:PROPS_set"]` only |
| camera `g1` queued before the restore | `STALE_FRAME` ("enqueued before scene_epoch 2") |
| camera `g1` resubmitted | dispatched, then rejected by the R3 witness oracle (`WITNESS_SPILL ... [0,0,1920,984]`), restored to `p2` (epoch 3), closed `ALL_REJECTED` |
| end | `violations: []`, mode `IDLE`, export refused beyond `confirmed_tick` 2, snapshot equal after restart |

The claimed collections are created by a setup control job before the genesis mark: linking a new collection changes
`col:Scene Collection`, which no agent can claim (first live run: every attempt was rejected for that reason and
restored to `p0` four times with `digest_match=true`).

## Limits

- A txn that F1 would have requeued (`TURN_TIMEOUT`, `ACTUATOR_BUSY`) costs a restore here and is not retried;
  with the bridge down the restore times out and the capsule freezes.
- A crashed single-script event closes `ALL_REJECTED`; the agent resubmits under a new event id.
- Attempts are capped at `K_MAX = 3` per event id, crash-consumed indices included.
- The restore answer's own digest is the equality witness (authenticated by the F1 HMAC); no second digest job.
- The frame witness after a rollback is R3's (`rollback_witness`); this unit only captures the frame.
- Live: a gui `KP_1` turn does not commit with the R3 witness oracle as it is (diff region spills the footprint); to settle between R3/R4.
- Rung A′ (relaunch Blender on the checkpoint after `RESTORE_TIMEOUT`) is not automated: the capsule freezes.
- F1's `broker.py control` CLI does not know the resume guard; use `RewindBroker.control` (R4 `rewindctl`).
