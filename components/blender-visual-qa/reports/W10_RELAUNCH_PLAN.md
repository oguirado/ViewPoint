# W10 RELAUNCH PLAN (card W10D lane P; base blender-visual-qa main 2d90665, engine pin ve-sealed-1-law2)

Sources: `v12w/CARDS_WBLD.md` W10a/W10b (cut list, no refit on GATE data, ADV-15.1), `v12m/STORAGE_LAW.md` L2-L6, `v12ar/k3_B3.md`,
`v12bs/BSPEC_BTRUTH.md` §10, lane D bus D2 (`reports/W10D_MOVED_DIAG.md`). Labels: [M] measured, [T] target, [H] hypothesis.
The engine is SEALED: nothing here changes `visioncore`/`vmem`; engine findings go to `proposals[]` (C7/X2 row).

## 0. Measured inputs [M] (1-seed timing probe, this lane, the only run)
Command (frozen archive, RAM workspace, cpus 15-19 only):
`git archive 2d90665 | tar -x -C build/room_P/frozen_2d90665`, then in it
`PYTHONPATH=config BVQA_CPU_CAP=15-19 timeout 1500 taskset -c 15-19 python3 -B -m blender_visual_qa.truth.run_bench --out ../probe_60f --frames 60 --res 320x180 --spp 4 --jobs 1 --clean-spp 1024 --campaign room_P --run-id probe1`

| quantity | value |
|---|---|
| rc / failures | 5 (`seed_partition_absent`: legacy arm, expected) / 0 |
| wall, 13 benches x 60 f, 1 seed, spp 4 | 895 s (`/usr/bin/time` 14:55) |
| non-clean bench (11 benches) | 12-14 s each = 151 s total (~0.22 s/frame, 4 threads) |
| B3 (clean 1024 spp, 2 captures x 60 f) | 330 s |
| B5 (clean doubled to 2048 spp by the self-check) | 410 s |
| clean share of the seed wall | ~712 s = 80 % |
| `ram_peak_bytes` / `unit_peak_max_bytes` / quota | 72,085,504 / 74,686,464 / 2,147,483,648 (tmpfs) |
| image files left in `--out` / workspace after exit | 0 / removed (L4) |
| python max RSS | 279 MB |

Caveat: lane D's diag Blender overlapped part of this probe (declared on bus D2), so these walls are an upper bound.

## 1. Host and CPU rules (deviations from the card's command shape)
- The W10b shape `taskset -c 0-19 ... --jobs 4` is NOT usable: `CPU_PLANS["w10"]` pins jobs to 5-9 (RT bench) and 10-14 (os-cu-0). `blender_cmd` and the decode worker call
  `taskset -c` themselves, which can widen the outer mask. So every run MUST export `BVQA_CPU_CAP=15-19` (all jobs share 15-19) and use `--cpu-plan w9 --jobs 1`.
- `tools/gates.py` smoke defaults to `BVQA_CPU_CAP=5-9` (gates.py:352). Run the gates with `BVQA_CPU_CAP=15-19` exported.
- One Blender at a time on the host, never left running. Admission (W10a): load1 < 20, MemAvailable >= 16 GiB, at most 2 concurrent review rooms alongside.

## 2. RAM-only run root (STORAGE LAW L2/L3)
Per-unit planned peak (`admit_unit`) = 60 x (1,094,400 + 65,536) + 8 x 4,600,000 = 106.4 MB at 320x180. Measured: 74.7 MB.
Quota per run = 60 f x res x jobs: jobs 1 -> about 107 MB (512 MB with margin); jobs 2 -> 213 MB; jobs 3 -> 320 MB. All are <= 2 GiB/run (the default `--ram-quota-mb 2048` stays).
Host: at most 2 concurrent runs x 2 GiB = 4 GiB, below 16 GiB in `<RAMWS_ROOT>/**`; `/dev/shm` free >= 8 GiB [M 58 GiB free].
`--out` holds evidence only (L6, <= 64 MB/run; probe 7.7 MB/seed -> 40 seeds about 300 MB/tier: **over the 64 MB cap**, so split runs per seed block, see §4).

## 3. Wall model (5 cores 15-19; per seed = 13 benches x 60 f)
Beauty at spp 16/64 has not been measured. [H] Half of the 0.22 s/frame scales with spp: spp16 = 378 s/seed, spp64 = 1284 s/seed non-clean.
Clean capture is repeated for every unit (`run_one` -> `clean_resid`, also per spp tier): 712 s/seed/tier.

| schedule (FIT 10 fixed) | GATE seeds | tiers | wall at 5 cores | at 10 cores [H x0.5] |
|---|---|---|---|---|
| card spec | 30 | 4,16,64 | 43.9 h | 21.9 h |
| cut 7 | 30 | 4,16 | 21.7 h | 10.9 h |
| cut 7 + cut 8 | 20 | 4,16 | 16.3 h | 8.1 h |
| 10-seed fallback (LB 0.741) | 10 | 4,16 | 10.9 h | 5.4 h |
| **spp4 tier only, fallback** | 10 | 4 | **4.8 h** (+0.7 h repeat on 3 seeds) | 2.4 h |
| spp4 tier only, cut 8 | 20 | 4 | 7.2 h | 3.6 h |

Conclusion: on 5 cores, the card's 6 h cap fits only "spp4 tier first, 10 GATE + 10 FIT". It is declared as the BTRUTH §10 fallback (Clopper-Pearson one-sided 95 % LB: 30/30 -> 0.905, 20/20 -> 0.861, 10/10 -> 0.741).
Tool proposal **P-PROP-1** (BVQA truth code, not the engine; a future card, not this one): compute the B3/B5 clean residual once per (bench, seed) and reuse it across spp tiers. This cuts 712 s/seed for every tier after the first; then cut 7 + fallback costs about 6.9 h (still > 6 h on 5 cores, about 3.4 h on 10).

## 4. Relaunch sequence (copy-pasteable; run from `BVQA=components/blender-visual-qa`)
```sh
export BVQA=components/blender-visual-qa SHA=$(git -C $BVQA rev-parse --short HEAD) BVQA_CPU_CAP=15-19 PYTHONPATH=config
F=$BVQA/build/frozen_$SHA; mkdir -p $F && git -C $BVQA archive HEAD | tar -x -C $F && cd $F   # frozen copy, never the live tree
python3 tools/gates.py --all                                                                 # G0: rc 0 on the frozen copy
RB="timeout 21600 taskset -c 15-19 python3 -B -m blender_visual_qa.truth.run_bench --frames 60 --res 320x180 --cpu-plan w9 --jobs 1 --decode external --strict --campaign w10"
# W10a C0 (determinism, threads 1 vs 4 are covered by --determinism-check), 1 FIT + 1 GATE seed, spp 4
$RB --out w10/c0 --spp 4 --seeds fit:0,gate:10 --benches B1,B6 --determinism-check --run-id c0
# W10a re-base [M]: the measured s/frame per tier on 1 seed pair replaces the [H] rows of §3 -> w10/REBASE.json
for t in 4 16 64; do $RB --out w10/rebase_spp$t --spp $t --seeds fit:0,gate:10 --run-id rebase$t; done
# W10b night 1: spp 4 tier, FIT 0-9 + GATE 10-19 (fallback; widen to gate:10-29 only if REBASE says <= 6 h)
$RB --out w10/run_spp4 --spp 4 --seeds fit:0-9,gate:10-19 --run-id n1spp4
# determinism repeat on 3 seeds -> rows.jsonl sha identical to the same arms of run_spp4
$RB --out w10/rep_spp4 --spp 4 --seeds fit:0,gate:10-11 --run-id n1rep
```
If the `--out` evidence goes over 64 MB (L6), split night 1 into GATE blocks (`gate:10-14`, `gate:15-19`) with the same FIT arm; T0 comes from FIT only.

## 5. Cut list (card order; apply when the REBASE extrapolation is > 6 h)
1. spp {4,16,64} -> {4,16} (cut 7; EEVEE stays cut). 2. GATE seeds 30 -> 20 (cut 8; CP LB declared). 3. 10-seed fallback (LB 0.741, BTRUTH §10).
4. (this plan, declared) spp4 tier alone on night 1; spp16 needs P-PROP-1 or a second night.

## 6. MOVED (lane D D2 input) and stop rules
D2 [M, CAL seeds 0-1, 8 f, spp 4]: 464 RIGID misses = a 2 / b 312 / c 77 / d 72 / e 1. Recall 0.205; recall on observable tiles 0.138 (n=174).
Amendment D proposes: truth flag bit 8 `FLAG_UNOBSERVABLE`, and the score drops flagged tiles from the MOVED denominator. This is a label change, not a threshold change.
- Before W10b, the amendment must land via its own card (truth/score code), or MOVED/`mv_exact` are reported advisory-only (G-E4 KNOWN_LIMITATIONS).
- Never refit on GATE seeds. Never change thresholds to pass. A miss at `moved_class` >= 0.90 with (e) close to 0 -> KNOWN_LIMITATIONS plus a C7 `proposals[]` row (X2), not a fix in this wave.
- Stop rule: if the profile is refused 2 nights in a row, `bvqa-1.0-rc` ships with profile=candidate, declared.

## 7. Gate list
G0 `python3 tools/gates.py --all` rc 0 (frozen copy, `BVQA_CPU_CAP=15-19`). C0 `identical=true`, else rc 5 `E_DETERMINISM` and W10b is blocked.
`t0_in_bounds` (T0 <= 4080), FIT/GATE disjoint (`split_violations` = []). The W10b acceptance list is GATE only, Clopper-Pearson.
The repeat run gives an identical `rows.jsonl` sha. `ram_receipt.json`: tmpfs, `ram_peak_bytes` <= quota, swap delta < 1 GiB.
`g_disk_frames`/`g_disk_size` rc 0 after each night. The full unittest discover passes.
