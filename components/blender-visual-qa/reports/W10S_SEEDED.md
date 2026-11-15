# W10S seeded truth scenes (card W10S2 lane F)
`bench_scenes.py --seed k` (SplitMix64 seeded with `(bench_no<<32)^k`; integer-px pose offsets, colour +-0.08, light energy +-15 %, camera +-2 px; slot0 injective in k mod 169). `--seed` absent = golden bytes. `run_bench` passes `scene_seed` to seed arms only (legacy arm keeps golden) and refuses colliding scene slots (`check_scene_collisions`); seeded B6 adds a lit diffuse plane + area light (noise-bearing still probe); `calibrate` refuses an all-zero still probe (`still_probe_degenerate`). Engine, thresholds untouched.

## Probe [MEASURED] (working tree, not a frozen copy; `taskset -c 15-19`, no foreign blender, RAM-only)
`BVQA_CPU_CAP=15-19 PYTHONPATH=config python3 -B -m blender_visual_qa.truth.run_bench --frames 60 --res 320x180 --cpu-plan w9 --jobs 1 --decode external --campaign w10s --spp 4 --clean-spp 0 --seeds fit:0-1,gate:10-11 --run-id w10sprobe2 --out build/room_F/probe`
- 52 units (13 benches x 4 seeds), failures 0, wall 869 s = **217 s/seed** (clean capture OFF), ram_peak 75.5 MB, released 3.67 GB.
- capture_sha256 distinct per seed for all 13 benches (4/4 each); scene.json sha distinct 4/4 for all 13 benches (bench_scenes, 60 f, seeds 0,1,10,11).
- split_violations [] / split_detail empty (no FIT/GATE overlap). rc 5 = `clean_evidence_absent` only, expected because `--clean-spp 0` (probe-only; night-2 runs clean).
- B6 still probe: 28320 samples, 24947 non-zero, max 549 (q4): non-degenerate, seed-bound.
- An earlier attempt with clean capture (default) was aborted at the 3rd of 52 units' B5: B3 clean 330 s, B5 clean > 870 s on seed 0 (not finished). Declared: full 4-seed clean probe did not fit the lane wall budget.

## Night-2 wall estimate [H]
Per seed: 217 s (measured) + B3 clean ~330 s (measured) + B5 clean >= 870 s (lower bound) >= ~24 min. 20 seeds (FIT 0-9, GATE 10-19) >= ~8 h, versus the 5.5 h of the night-1 plan; split into GATE blocks if evidence passes 64 MB.

## Night-2 commands
```sh
RB="timeout 36000 taskset -c 15-19 python3 -B -m blender_visual_qa.truth.run_bench --frames 60 --res 320x180 --cpu-plan w9 --jobs 1 --decode external --strict --campaign w10"
$RB --out w10/run_spp4 --spp 4 --seeds fit:0-9,gate:10-19 --run-id n2spp4      # run from a frozen git archive of the commit
$RB --out w10/rep_spp4 --spp 4 --seeds fit:0,gate:10-11 --run-id n2rep         # determinism repeat
```
Readoption receipts: `tools/ADOPTED.json` carries wave `W10F` + `readoption_card: W10S` (`tools/gates.py` READOPTION_WAVES has no W10S and is outside this lane).
