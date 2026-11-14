# ref_rewind_verify (R3): the judge of the rewindable Blender capsule

Track P: declared Claude work (claude-fable-5-2), not Gemini-written. Branch `p-fable-r3`, base `p-fable-r0@4d33836` (contract `rw1`). Imports `product/ref_rewind/schema.py` and `product/ref_broker/broker.py`; edits neither.

| File | Role |
|---|---|
| `judge.py` | `judge(attempt, base, after, run_probe=None) -> rec:judge` (R0 signature), `make_judge(differ, footprint, slack, strict_outside)`, `rollback_witness(base_png, restored_png)`, `best(judge_recs)`, `reject_reason(rec)`, `cost_ms(rec)` |
| `test_judge.py` | 46 tests: one class per oracle with a wrong-edit fixture, the wrong-edit scenario through the real F1 broker + bridge core (R0 `fakes.Capsule`, R0 `run_txn`), real-ffmpeg witness |
| `negctl.py` | 22 mutants of `judge.py` (one guard removed each); exit 0 only if every mutant turns the suite red |
| `live_judge.py` | Live check against Blender under a vision job |
| `evidence/` | `live_judge.json` (records + checks), `live_events.jsonl`, 4 frames |

## Oracles (fixed order, inapplicable ones omitted, every one fails closed)

| # | Oracle | Passes when | Rejection codes (first token of `detail`) |
|---|---|---|---|
| 1 | `expect` | intent has `expect: {probe, equals}`; `run_probe(probe)` is `ok`, its digest equals `after.digest`, its result equals `equals` | `PROBE_FAILED`, `PROBE_NOT_READONLY`, `EXPECT_MISMATCH` |
| 2 | `delta` | F1 verdict ∈ `STATE_VERIFIED, VERIFIED`; `digest_delta(base, after)` non-empty and ⊆ `write_set` + `touches` (gui: + `ui:view3d`) | `NO_DIGEST`, `VERDICT_NOT_COMMITTABLE`, `NO_EFFECT`, `DELTA_OUTSIDE` |
| 3 | `witness` | both frames given (gui lane: a missing frame is a failed witness `WITNESS_ERROR no frame`, script lane: the oracle is omitted); a changed region touches `FOOTPRINT` when the delta has a `col:`/`mat:`/`ui:view3d` key; gui lane: no region outside `FOOTPRINT` (+64 px slack, as F1) | `WITNESS_ERROR`, `WITNESS_NO_CHANGE`, `WITNESS_SPILL` |
| 4 | `cost` | records `job_ms=<n>`; never decides; `best()` breaks ties between accepted attempts by lowest ms, then attempt order | |

`pick` = every deciding oracle ok. `rollback_witness` (after a restore): ok iff no changed region intersects `FOOTPRINT`; pixels outside it are not part of the claim (`WITNESS_NOT_RESTORED` otherwise).

Use from R2/R4: `sys.path.insert(0, ".../product/ref_rewind_verify"); import judge as J`, then `RewindBroker(..., judge=J.make_judge())` (default differ = F1 `diff_regions`, needs ffmpeg) and `J.rollback_witness(base_png, b.frame_png)` after each restore.

## Run

```
cd product/ref_rewind_verify
S="systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0"
$S python3 -m unittest -v test_judge
$S python3 negctl.py
# live (own job, fresh RT):
N=$(python3 -c 'import secrets;print(secrets.token_hex(8))'); RT=/dev/shm/vqa-cu/ref-rw3; mkdir -p $RT
V="python3 -I tools/vis-skill/vision"
(cd /tmp && $S $V start --job vprw3 --size 1920x1080 --mem 8G --app "env VP_SWARM_RT=$RT VP_SWARM_NONCE=$N blender --factory-startup --python <repo>/product/ref_broker/bridge.py" --wait-title Blender)
VP_SWARM_NONCE=$N $S python3 live_judge.py --job vprw3 --rt $RT --evidence evidence
$V stop --job vprw3
```

## Verified (2026-11-14, this host)

- 44/44 tests pass, 1.5 s [MEASURED].
- `negctl.py`: 22/22 mutants red, unmutated green [MEASURED]. Includes the two the plan names: always-accept judge → the wrong-edit scenario fails; probe read-only check removed → the mutating-probe tests fail. In-suite must-FAIL: `fakes.accept_all_judge` commits the 10× table and the final assertion fails.
- Live, Blender 4.0.2 under vision job `vprw3`, F1 broker + bridge, real frames and real `diff_regions`: 10/10 checks [MEASURED], `evidence/live_judge.json`.

| Live step | Result |
|---|---|
| a1 wrong (10× table, `CHAR_body` moved) | F1 `VERIFIED_WITH_SPILL`; judge `pick=false`: expect `EXPECT_MISMATCH`, delta `VERDICT_NOT_COMMITTABLE` (delta `col:CHAR_mesh, col:PROPS`), witness ok |
| rollback witness before any undo | red (`WITNESS_NOT_RESTORED`) |
| after undo | digest equals base; frame sha256 equals base frame; rollback witness `in=0 out=0` |
| a2 right | F1 `STATE_VERIFIED`; judge `pick=true`, all four oracles ok, witness region `[728,416,128,128]`; `best()` = a2 |
| probe that moves the table 6 cm and answers `equals` | `PROBE_NOT_READONLY` |

## Not verified / open

- The live run did not use R1: the digest is an inline stand-in (F1 collection digest + `ui:view3d`), and the "restore" is a hand-written undo script, not `open_mainfile`. It proves the judge, not the rewind. Frame equality after a real restore is unmeasured.
- Deviation from rw-build §R3 "empty outside the footprint": enforced for the gui lane only. On the script lane every correct edit repaints the outliner (live: 3 regions outside `FOOTPRINT` on the accepted a2), so outside regions are reported in `detail` and decide only with `strict_outside=True`.
- The Blender splash covers the viewport centre: without dismissing it the witness rejected the correct a2 (`WITNESS_NO_CHANGE`, first live run). `live_judge.py` clicks it away; R4's launcher must do the same before frame 0.
- An edit that changes a `col:` key but is off-screen or occluded is rejected by the witness; there is no per-intent opt-out.
- `VERIFIED_WITH_SPILL` and `JOB_ERROR` are reported under the `delta` oracle (the contract has no fifth name).
- `run_probe` must return the digest read after the probe; the judge cannot detect a runner that returns a stale one.
- Not run against R2's `RewindBroker` (not merged here); the scenario uses R0's `run_txn`.
