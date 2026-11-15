# RT0D02 lane D evidence

`visioncore/change.py` gathers only requested tile windows from strided views,
reduces their SAD in bounded batches, and clips boundary windows separately.
Int16 luma subtraction widens safely to int32; sums remain int64. Wider inputs
retain the sealed int64 casts. Tile indices normalize to signed coordinates before source shifts.
Full source-window validity and clipped pixel counts
remain separate. Classification caches SAD/count pairs by vector within one frame.
Projection sums widen their reductions instead of copying entire luma planes.

Candidate offers, vector sorting, strict best-cost comparisons, connected-component
raster labels, and state updates retain their original order because their ties and
history affect output. Independent window reductions run in NumPy batches.

`PROOF_SHA_TABLE.json` reruns the D01 input generators and compares every output
blob hash against the D01 sealed hashes, requiring identical input hashes first.
Its prior-proof hash is pinned by the unchanged D01 receipt. The RT0D01 reference
change source is checked byte-for-byte against the sealed tag. Added odd-size and
large-scroll sources run the sealed pipeline directly. Receipt generation happens
only after the complete proof passes. D02 authorizes only `visioncore/change.py`;
the gate independently validates the union of the D01 and D02 receipts.

The D01 corpus limitations carry forward: executable X6/X7 image inputs are
AUSENTE — no inventar; X9 covers metadata through the existing suite. Calibration
and held-out GUI inputs are outside this card's DEV corpus.

RAM proof and timing reproduction (commands use the working repository):

```sh
taskset -c 5-9 python3 -B eval/results/rt0d02/profile_s2.py --arm before
taskset -c 5-9 python3 -B eval/results/rt0d02/profile_s2.py --arm after
taskset -c 5-9 python3 -B eval/results/rt0d02/verify_equivalence.py
taskset -c 5-9 python3 -B eval/results/rt0d02/benchmark_s2.py
python3 -B eval/results/rt0d02/check_sad_equivalence.py --exhaustive
python3 -B eval/results/rt0d02/check_window_allocation.py
python3 -B eval/results/rt0d02/check_index_types.py
python3 -B eval/results/rt0d02/check_speed_target.py
python3 -B tools/gates.py --all
```

Run unittest discovery only from an isolated checkout beneath `build/room_D`,
with its own Git metadata and `TMPDIR` inside that scratch tree. Overlay current
lane files and evidence before testing. The prepared room copy is
`build/room_D/checktree_rt0d02`; its generated catalogue is refreshed there only.
The live catalogue remains outside the lane surface.

```sh
cd build/room_D/checktree_rt0d02
python3 -B tools/build_catalogue.py
python3 -B tools/gates.py --all
python3 -B -m unittest tests.test_rt0_d01 tests.test_rt0_d02 -q
python3 -B -m unittest discover -s tests -q
```

`PROFILE_BEFORE.json` and `PROFILE_AFTER.json` contain cProfile and separate line
measurements. `TIMINGS.json` contains per-repeat load, S2 and whole-pipeline timings,
plus medians. `REGRESSIONS.json` records executed failing-first allocation and
receipt-union controls. `PERFORMANCE_TARGET.json` verifies the measured speedup and rejects an unchanged-performance negative. `FINAL_GATES.json` records final command return codes;
full-suite source-pin failures, when present, require conductor-owned resealing.
