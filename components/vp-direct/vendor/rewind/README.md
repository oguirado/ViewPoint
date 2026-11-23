# ref_rewind (R0): frozen contract of the rewindable Blender capsule

Track P: declared Claude work (claude-fable-5-2), not Gemini-written. Branch `p-fable-r0`, base `p-fable-f1@b255057`.

| File | Role |
|---|---|
| `CONTRACT.md` | Normative contract `rw1`: records, order, counters, control jobs, intent extension, unit interfaces |
| `schema.py` | Constants, id helpers, record builders/validators, digest helpers, `dispatch_control`, `RewindState`/`replay` (stdlib only) |
| `fakes.py` | `Capsule` (real F1 broker + real bridge core on a thread, fake scene/vision), `FakeOps`, `FakeStore`, `fake_judge`, mutants `noop_restore_script`, `accept_all_judge` |
| `test_schema.py` | 29 tests |

## Run

```
cd product/ref_rewind
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 python3 -m unittest -v test_schema
```

Use from another unit: `sys.path.insert(0, "<repo>/product/ref_rewind"); import schema as RW, fakes`.

## Verified (2026-11-14, this host)

- 29/29 tests pass, 0.8 s [MEASURED].
- Reference transaction through the real F1 broker and bridge core: wrong attempt → judge reject → restore → digest equals base, frame equals base frame, `scene_epoch` 1 → 2 → correct attempt → commit `p1`; `replay(events.jsonl)` gives the same state with zero violations.
- F1 `_recover` restarts over a log containing the four new record types; `git diff --stat b255057 -- product/ref_broker` is empty; the F1 hook signatures used are pinned by a test.
- Negative controls: 12 guards removed one at a time (rollback-needs-restore, epoch bump, commit-needs-judge, dead-oracle pick, rewind broker-only, authenticated control answer, commit frontier, K_MAX, agent control op, sha check, restore body, probe read-only): each made the suite fail [MEASURED, 12/12 red]. In-suite mutants: no-op restore → `FROZEN`; accept-all judge → final state assertion fails; sha check off → tamper test fails.

## Not verified here

- No Blender, no vision job, no live run: R0 is contract + fakes. `open_mainfile` from the bridge timer (spike S1) belongs to R1; the live proof to R4.
- `FakeStore` has no ring, eviction or dedup (R1). `fake_judge` is a stand-in for R3 (footprint-aware witness is R3's).
- `RewindState` treats a failed restore as frozen until a `control resume`; the director-rewind exit path is specified in `CONTRACT.md` §3 but enforced by R2.
- Restore deadline 30 s and K ≤ 3 are [TARGET] values, unmeasured.
