# R0 contract: rewindable Blender capsule (`rw1`, FROZEN)

Track P: declared Claude work (claude-fable-5-2). Design input for Track H renderers, like `02_TURN_QUEUE_SPEC.md`.
Machine form: `schema.py` (import it, never redefine it). Fakes: `fakes.py`. Plan: `rw-build.md` §3.
Base: `product/ref_broker` @ `b255057`, imported, never edited (`test_schema.F1Untouched`).

## 1. Runtime layout (added to COMMON.md)

| Path | Owner | Content |
|---|---|---|
| `RT/events.jsonl` | broker (only writer, via `Broker._append`) | F1 records + the four record types of §2 |
| `RT/ckpt/<point_id>.blend` | written by the mark job, admitted by the R1 store | checkpoint file |
| `RT/ckpt/manifest.jsonl` | R1 store | ring manifest |
| `RT/inbox/<nonce>_ctl.<kind>.<...>.py` | broker | control jobs (§4) |

RT for these units: `/dev/shm/vqa-cu/ref-rw*/`; vision jobs `vprw*`.

## 2. Records (appended to `events.jsonl`; F1 adds `v`, `seq`, `ts_ms`)

| rec | Fields | Rules (`schema.validate_record`) |
|---|---|---|
| `ckpt` | `point_id, tick, scene_epoch, blend_sha256, bytes, digest{}, frame, digest_version=2, [dedup_of]` | `point_id` = `p<digits>`, `p0` = genesis; `scene_epoch` = epoch at mark time; sha = 64 hex of the `.blend` |
| `txn` | `phase, event_id, attempt, base_point, verdict, reason, [point], [tick]` | `attempt` = `<event_id>.a<k>`, k ≤ 3; begin: `verdict` null; commit: verdict ∈ `STATE_VERIFIED, VERIFIED` and **`point` + `tick` required** (the point marked on the committed state); rollback: verdict ∈ `JOB_ERROR, VERIFIED_WITH_SPILL, JUDGE_REJECT, OUTCOME_UNCERTAIN, INPUT_UNCERTAIN` |
| `judge` | `attempt, pick, oracles[{name, ok, detail}], judge_version` | oracle names in fixed order `expect, delta, witness, cost`, each at most once, inapplicable ones omitted; `pick=true` needs ≥ 1 deciding (non-cost) oracle and all of them ok; `pick=false` needs a failed deciding oracle |
| `control` op `rewind` | `to, by, scene_epoch_after, digest_match, [reason], [attempt]` | `by` ∈ `broker, director` (an agent value is `NO_LEASE`); `scene_epoch_after` = previous + 1, also when `digest_match` is false |

Two fields beyond `rw-build.md` §"R0 contract": `txn.point`/`txn.tick` on commit (so `last_good` and `confirmed_tick` are derivable from the log alone) and the optional intent field `touches` (§5).

## 3. Order of records and counters (`schema.RewindState`, `schema.replay`)

Accepted attempt: `txn begin` → F1 dispatch → `judge pick=true` → mark job → `ckpt pN` → `txn commit point=pN tick=T`.
Rejected attempt: `txn begin` → F1 dispatch → `judge pick=false` (omitted when the F1 verdict already decides) → restore job → `control rewind to=base digest_match=true` → `txn rollback`. Next alternative = new `begin` with the same `base_point`. No alternative left: F1-style `close` with verdict `ALL_REJECTED`.
Restore failed (`digest_match=false`, unauthenticated answer, deadline 30 s [TARGET]): `control rewind digest_match=false` then `control stop`; the txn stays open; only a director rewind to `last_good` is admitted.

| Counter | Definition |
|---|---|
| `scene_epoch` | starts at 1; +1 per `control rewind`; never decreases. After a bump every queued gui intent closes `STALE_FRAME`. |
| `confirmed_tick` | max `tick` over `txn commit`. Exports and room/TaskLight projections only for ticks ≤ it and with no open txn (`schema.exportable`). |
| `last_good` | genesis point, then the `point` of the newest commit. `txn begin.base_point` must equal it. |
| `open_txn` | a begin without commit/rollback. On restart: restore its `base_point` before any dispatch. |

Violations the fold reports (audit = replay + `violations == []`): `ROLLBACK_WITHOUT_RESTORE`, `ROLLBACK_UNJUDGED`, `COMMIT_UNJUDGED`, `EPOCH_NOT_BUMPED`, `EPOCH_SKEW`, `TXN_OVERLAP`, `TXN_NOT_OPEN`, `ATTEMPT_REUSED`, `BASE_NOT_LAST_GOOD`, `BASE_CHANGED`, `UNKNOWN_POINT`, `POINT_REUSED`, `TICK_BACKWARDS`, plus any schema code.

## 4. Control jobs (broker-owned bridge jobs)

- Name: `schema.control_job_id(kind, *parts)` → `ctl.mark.<point>`, `ctl.restore.<point>.e<epoch_after>`, `ctl.digest.<n>`, `ctl.probe.<n>`. Agent `event_id`s starting with `ctl.` are refused `NO_LEASE`.
- Dispatch: `schema.dispatch_control(broker, job_id, script)` → `{ok, code, result, job_ms, changed, reason}`. It calls F1 `Broker._dispatch_script`, so the answer is HMAC-checked; `ok` only for an authenticated `OK`. Job ids are single-use (include a counter).
- Result: the script's last stdout line is `RW_RESULT <one-line json>` (≤ 16000 chars), built with `schema.result_line`, read with `schema.parse_result`.

| Script (R1 `ops.py`) | Result object |
|---|---|
| `mark_script(point_path)` | `{"op":"mark","path","bytes","digest":{...}}` |
| `restore_script(point_path, load_ui=True)` | `{"op":"restore","path","load_ui","digest":{...}}` (digest computed after the load) |
| `digest_script()` | `{"op":"digest","digest":{...}}` |
| probe (`expect.probe`) | free JSON; the judge compares it with `expect.equals` and re-reads the digest: any change is `PROBE_NOT_READONLY` |

Digest v2 keys: `col:<Collection>` (F1 fields + modifiers of its objects), `mat:<Material>`, `render`, `ui:view3d`; values hex strings. Helpers: `digest_delta`, `digest_match` (two empty digests do not match), `digest_id`, `delta_outside(delta, intent)`.

## 5. Intent extension (optional; old intents stay valid; `schema.validate_intent_ext` runs after F1 `_validate`)

| Field | Type | Rule |
|---|---|---|
| `alternatives` | list of script sources | lane script only; `1 + len ≤ 3`; tried serially, each from the same base (`schema.attempt_scripts`) |
| `expect` | text (F1) or `{"probe": "<read-only bpy source printing RW_RESULT>", "equals": <json>}` | |
| `touches` | list of `mat:<name>`, `render`, `ui:view3d` | non-collection digest keys the job may change; `col:*` stays in `write_set` (F1 claim check). gui intents implicitly touch `ui:view3d`. |
| `event_id` | ≤ 73 chars | room for `.a<k>` |

`op`, `rec:"control"` or a `ctl.` id in an agent intent → `NO_LEASE`.

## 6. Interfaces between units

| Unit | Must provide | Fake in `fakes.py` |
|---|---|---|
| R1 `ops` | `mark_script`, `restore_script`, `digest_script` (§4) | `fakes.FakeOps` (scene = dict, `.blend` = JSON) |
| R1 `store.Store(rt, slots=8, budget_bytes=None)` | `path_for(point_id)`, `admit(point_id, tick, scene_epoch, digest, frame, pinned=()) -> rec:ckpt` (raises `schema.CkptError` `CKPT_MISSING`/`CKPT_BUDGET`), `verify(point_id) -> path` (sha re-check; `CKPT_CORRUPT`/`CKPT_MISSING`), `get(point_id)`, `points()` | `fakes.FakeStore` (no ring) |
| R3 `judge(attempt, base, after, run_probe=None) -> rec:judge` | `attempt={"attempt","intent","script","verdict","extra"}`, `base={"point_id","digest","frame_png"}`, `after={"digest","frame_png","job_ms"}`, `run_probe(script) -> {"ok","result","digest"}` | `fakes.fake_judge`, mutant `fakes.accept_all_judge` |
| R2 `RewindBroker(Broker)` | appends §2 records through `_append`; constructor takes `ops`, `store`, `judge` so fakes and real units swap | `fakes.Capsule(cls=..., broker_kw=...)` |
| R4 | `audit` = `schema.replay(schema.read_events(path))`, exit non-zero on violations or a failed final probe | |

Reference flow (hand-driven, what R2 automates): `test_schema.run_txn`.

Codes: `JUDGE_REJECT, ALL_REJECTED, CKPT_BUDGET, CKPT_CORRUPT, CKPT_MISSING, RESTORE_UNAUTH, RESTORE_MISMATCH, RESTORE_TIMEOUT, PROBE_NOT_READONLY, PROBE_FAILED`.
