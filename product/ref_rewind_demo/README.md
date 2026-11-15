# ref_rewind_demo: integration, `rewindctl` CLI and live proof of the rewindable Blender capsule (unit R4)

Track P, declared non-Gemini product work. Written by claude-fable-5-2. Stdlib Python only.
Plan: `rw-build.md` §3 "R4", §4 (live proof), §5, §6. Contract: `product/ref_rewind/CONTRACT.md` (rw1).
Integrates by import, edits none of them (`test_demo.Untouched`): F1 `ref_broker` @ `b255057`, R0 `ref_rewind`,
R1 `ref_rewind_ckpt` (@ `456b996`, digest coverage fix included), R2 `ref_rewind_lanes`, R3 `ref_rewind_verify`.

| File | Role |
|---|---|
| `rewindctl.py` | Frozen CLI: `run`, `mark`, `rewind --to`, `export`, `status`, `audit`, `emit-script mark\|restore\|digest`. |
| `rewind_live.py` | Live proof (rw-build §4 steps 1-6) and the two must-FAIL runs (`--mutant`). Starts and stops its own vision job with `--launch`. |
| `test_demo.py` | 21 unit tests, no Blender; fixtures are the real logs and frames under `evidence/`. |
| `evidence/` | `nominal/`, `mutant_no_restore/`, `mutant_judge_accept_all/` (events, cursor, checkpoint manifest, frames, `live.json`), console logs, `nominal/rewind_live.mp4`. |
| `CONSUME.md` | How Antigravity Track H uses this without copying or relabelling it. |
| `OPENSUPERFICIES.md` | Notes for the move into OpenSuperficies capsules (rw-build §5). |

## Run

```sh
cd product/ref_rewind_demo
S="systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0"
$S python3 -m unittest -v test_demo
# live: fresh RT under /dev/shm/vqa-cu/ref-rw*, job named vprw*; --launch starts Blender + F1 bridge and stops the job
$S python3 rewind_live.py --job vprw4 --rt /dev/shm/vqa-cu/ref-rw4 --evidence evidence/nominal --launch                         # rc 0 = all checks hold
$S python3 rewind_live.py --job vprw4 --rt /dev/shm/vqa-cu/ref-rw4-m1 --evidence evidence/mutant_no_restore --launch --mutant no-restore          # rc 0 = designated check went RED
$S python3 rewind_live.py --job vprw4 --rt /dev/shm/vqa-cu/ref-rw4-m2 --evidence evidence/mutant_judge_accept_all --launch --mutant judge-accept-all
python3 rewindctl.py audit --events evidence/nominal/events.jsonl        # rc 0; the two mutant logs give rc 1
```

## Live proof (2026-11-14, Blender 4.0.2, vision job `vprw4`, RT `/dev/shm/vqa-cu/ref-rw4-n3`) [MEASURED]

`evidence/nominal.log`: exit 0, 21 of 21 checks. Events: `evidence/nominal/events.jsonl` (57 records, sha256 `43bda17c…`).
Video: `evidence/nominal/rewind_live.mp4` (9 frames, 9.9 s).

| §4 step | Observed |
|---|---|
| 1 modeler `m1` creates `CHAR_body` | `STATE_VERIFIED`, judge pick, commit `p1` (genesis `p0` marked first) |
| 2 props `e1.a1` (table 10x, moves `CHAR_body`) | judge reject: `expect` `EXPECT_MISMATCH got false want true` and `delta` `DELTA_OUTSIDE ["col:CHAR_mesh"]` |
| 3 rewind | `control rewind to=p1 by=broker digest_match=true`, `scene_epoch` 1 → 2; frame after restore equals the `p1` frame inside FOOTPRINT (`frame_p1.png` vs `frame_restored_p1.png`); the same witness on `frame_e1a1_wrong.png` is `WITNESS_NOT_RESTORED` |
| 4 props `e1.a2` | commit `p2` from base `p1`; digest delta `p1`→`p2` = `["col:PROPS_set"]` |
| 5 camera `g1` queued before the rewind | `STALE_FRAME` ("enqueued before scene_epoch 2"); resubmitted as `g2` (KP_1) → `VERIFIED`, commit `p3` |
| 6 audit | in process and `rewindctl.py audit` rc 0, no problems; live Blender digest equals `p3`; final probe `table_z [-1,1]`, `body_z [2,4]`, `under: true`; mode `IDLE`; export refused beyond `confirmed_tick`; snapshot equal after a broker restart |

Must-FAIL runs (same scenario, one guard broken, same job name, fresh RT each):

| Run | Broken guard | Designated check | Result |
|---|---|---|---|
| `--mutant no-restore` | restore job restores nothing | `audit_clean` | RED: R2 logs `digest_match=false` (`RESTORE_MISMATCH`), capsule `FROZEN`, txn `e1.a1` open, `rewindctl audit` rc 1; final probe shows the wrong edit still present (`table_z [-10,10]`, `body_z [8,10]`). 12 of 21 checks red. |
| `--mutant judge-accept-all` | judge always picks | `final_probe_only_accepted_work` | RED: `e1.a1` committed as `p2`, final probe `under: false`; `rewindctl audit` rc 1 on `commit_delta` (undeclared `col:CHAR_mesh`). 9 of 21 checks red. |

Merged unit suites on this branch [MEASURED]: `test_broker` 34, `test_schema` 29, `test_ckpt` 38, `test_lanes` 35, `test_judge` 44, `test_demo` 21: 201 of 201 pass.

## `rewindctl audit`

Pure function of `events.jsonl` (`rewindctl.audit(records)`), six checks: `replay` (R0 fold has no violation), `closed` (no open
txn, not frozen), `rollback_restored` (every rollback has its own rewind to its base with `digest_match=true`),
`commit_judged`, `commit_chain` (accepted points form one chain from genesis to `last_good`), `commit_delta` (what a commit
added is inside its intent's `write_set` + `touches`). Exit 0 clean, 1 problems, 2 unreadable.

Negative controls (`test_demo.py`): each check has a log (real mutant log or a one-field mutation of the nominal log) that it
reports, and `audit(..., skip=(check,))` no longer reports it; `commit_delta` skipped accepts the accept-all log outright.

## Integration decisions made here

| Decision | Reason |
|---|---|
| Demo judge = R3 `make_judge(strict_outside=False)` | F1's gui dispatch clicks the viewport before the key; the selection repaint in outliner/properties is in the base→after frame diff (R2 live: `WITNESS_SPILL [0,0,1920,984]`). Spill of the key itself stays with F1 (`VERIFIED_WITH_SPILL` rolls back). With it the step-5 resubmit reaches `VERIFIED`. |
| Rollback frame witness with `thresh=8` gray levels per 8x8 cell [TARGET] | First run: one cell differed by 3 (viewport anti-aliasing still converging at capture) [MEASURED]; the wrong-edit frame differs by 113 over 21,866 cells [MEASURED]. |
| Setup control job before genesis | Creates the claimed collections (R2 note), removes the factory Cube; splash dismissed by one click (R3 note). |
| Blender launched with `XDG_CONFIG_HOME=RT/xdg` | R1 note: `open_mainfile` writes the recent-files list. |

## Not verified / limits

- Interpreter poisoning is outside the rollback domain: a job that patches `bpy`, handlers or the bridge survives `open_mainfile` (same process). Stated limit, not tested.
- Digest v2 is a subset of the scene (R1 README); undo stack, addon prefs, selection and modal state are not compared. The `strict_outside=False` judge does not decide on pixels outside FOOTPRINT.
- The `no-restore` mutant is caught by R2's digest check at the restore; the audit reports the consequence (open txn, frozen). A restore that lies about its digest is not covered (the digest is computed inside the same Blender).
- `rewindctl run|mark|rewind|export` were exercised through their library paths in `rewind_live.py` (`make_broker`, `step`, `export_point`); the `mark` and `rewind` subcommands were not run live from the shell. `status`, `audit`, `emit-script` are tested as CLI.
- Crash recovery, `OUTCOME_UNCERTAIN`, FROZEN → director rewind and rung A′ are covered by R2 unit tests only, not live here.
- One Blender, serial attempts; K ≤ 3. Video is assembled from the broker's frames (1 per second), not a screen recording.
