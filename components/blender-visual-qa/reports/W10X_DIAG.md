# W10X — night-1 diagnosis: every calibration check reads None

Input (read only): `build/frozen_301d38a/w10/run_spp4` (main 301d38a, spp4, FIT 0-9, GATE 10-19, 13 benches x 60 f, 320x180, rc 5).
`rows.jsonl` sha256 6f2ae5d15c36825713be62d96772245608e25409852fe3171ee3ceb19646130f (260 rows, 13 per seed). No render, no refit, thresholds unchanged.

## 1. Why every check is None (CAUSE, measured)
This is not a W10F key mismatch, not a lost GATE row and not profile=None. The cause is the `split_leak` refusal in `calibrate.calibrate`:
`calib_report.json` → `calibration.refusal = "split_leak"`, `split_violations = [capture_sha_overlap, still_probe_capture_in_gate, still_probe_seed_unbound]`.
When `leaks` is non-empty, `calibrate` sets `fit_rows = gate_rows = []` (calibrate.py, refusal path), so `summary.tl = {}` and every TARGET or required check gets value None (`absent: true`).
Only `clean_evidence_missing_rows` reads all rows, which is why it alone computes (0: PASS). The same `T0_Q4 32` shown in the SUMMARY is the refusal-path fallback (T0_still only); the GATE rows were really labelled at `frozen_t0_q4 = 281`.
Overlapping capture groups (`split_detail`, new):

| bench | FIT seeds sharing one capture | GATE seeds sharing it |
|---|---|---|
| B1x, B2, B6, BS | 0-9 | 10-19 (1 distinct capture for all 20 seeds) |
| B5o | 1,2,3,9 | 12,16 |
| B8 | 0,2,5,6,7,8 | 15,17,18,19 |

The B6 still probe is one capture for all seeds, so it is also in GATE, and its seed binding collapses to 1 seed. All 141600 still samples are 0, so the still probe is degenerate.
Test: `tests/test_w10x_aggregate.py` `CauseTests.test_capture_overlap_refuses_and_names_the_bench` and `NightOneEvidenceTests.test_night1_cause_is_capture_overlap` (both fail before the fix: no `split_detail`).

## 2. Why the per-seed metrics look identical
- The scenes do not depend on the seed. All 260 `*.scene.json` files reduce to 1 distinct sha256 per bench (13/13). `render_bench` calls `bench_scenes.py --bench <B>` with no seed. The seed k only moves the Cycles sampling seed (`cycles_seed(bench_no, k, ri)`).
  Test: `test_night1_scenes_do_not_depend_on_seed`.
- B1x, B2, B6 and BS give bit-identical captures (truth + decoded beauty) for all 20 seeds, so the noise seed has no effect on them. B5o and B8 are partly identical. The other 7 benches give 10/10 distinct captures, but only as noise replicates of one fixed scene.
- Two regimes are visible in SUMMARY.md: blocks 1-10 are FIT, labelled at T0 32; blocks 11-20 are GATE, labelled at T0 281. The table had no seed or arm column, so it read as "one block repeated". Small differences exist (for example B7 tl3 50.0/44.1/47.2 %).

**Verdict: the W10 statistics are invalid as seed-level evidence.** The 10 GATE "seeds" are noise replicates of one scene per bench, and for 4 benches they are 0 independent trials. A Clopper-Pearson bound over them (10/10 → 0.741) does not bound scene-level generalisation.

## 3. Fix
- In surface (done): `calibrate.py` adds `split_detail` (emitted only on refusal, so golden bytes are unchanged). `run_bench.summary_md` prints `REFUSED: **<refusal>**`, the split_violations, the frozen GATE T0 and the overlap groups, and adds arm and seed columns. Offline `tools/rescore_w10.py` added.
- Out of surface (needs a new card): give `bpy_side/bench_scenes.py` a `--seed` that jitters the scene parameters (poses, materials, lights) and pass k from `render_bench`. Make the B1x/B2/B6/BS beauty noise-bearing, or declare them single-capture benches that are excluded from seed statistics. Fix the B6 still probe (residual is 0 everywhere).
- Re-render cost: the scenes have to change, so all labels and evidence must be re-rendered. Same schedule as night-1 (spp4, 10 FIT + 10 GATE): about 5.5 h on cpus 15-19 (W10_RELAUNCH_PLAN). 20 GATE seeds: about 7.2 h plus FIT. Full card (30 GATE, spp 4,16,64): 43.9 h at 5 cores.

## 4. Rescored table (DIAGNOSTIC, GATE 10-19 only, T0 281 as labelled, bounds from code; `build/room_D/rescore_w10_n1/`)
Per-seed k/n means: the target evaluated on that seed's rows only (declared assumption). CP95 is the one-sided Clopper-Pearson lower bound.

| check | bound | GATE value | status | k/n | CP95 LB |
|---|---|---|---|---|---|
| static_fp | <= 0 | 30328 | FAIL | 0/10 | 0 |
| rigid_dirty_recall | >= 990000 | 887278 | FAIL | 0/10 | 0 |
| moved_class | >= 900000 | 431025 | FAIL | 0/10 | 0 |
| occlude_recall | >= 980000 | 927740 | FAIL | 0/10 | 0 |
| deform_recall | >= 980000 | 850111 | FAIL | 0/10 | 0 |
| shade_strong_recall | >= 980000 | 1000000 | PASS | 10/10 | 0.741 |
| shade_weak_fp_ppm | <= 10000 | 134433 | FAIL | 0/10 | 0 |
| mv_exact | >= 950000 | 431025 | FAIL | 0/10 | 0 |
| mv_err_p95_px | <= 1 | 0 | PASS | 10/10 | 0.741 |
| drift_duty_ppm | >= 500000 | 775949 | PASS | 10/10 | 0.741 |
| flicker_fp_tiles | <= 0 | 0 | PASS | 10/10 | 0.741 |
| flicker_seed_success | >= 10 | 0 | FAIL | 0/10 | 0 |
| drift_seed_success | >= 10 | 0 | FAIL | 0/10 | 0 |
| frozen_ge300k_seed_success | >= 10 | None (no stratum rows) | FAIL | - | - |
| flicker_latency_max_frames | <= 4 | 60 | FAIL | 0/10 | 0 |
| b4n_temporal_fp_tiles | <= 0 | 0 | PASS | 10/10 | 0.741 |
| c1b_class_flips | <= 0 | 0 | PASS | 10/10 | 0.741 |
| c5_select_ppm | >= 1000000 | 1000000 | PASS | 10/10 | 0.741 |
| b5o_global_mv_ppm | >= 1000000 | 0 | FAIL | 0/10 | 0 |
| s4_iou_median_ppm | >= 800000 | 250000 | FAIL | 0/10 | 0 |

## 5. SEAL-2 implications
No profile exists, and none can come from night-1: the official result stays `split_leak` (reproduced offline). Even ignoring the leak, 13 of 20 GATE checks fail at the frozen T0 281, and the 7 passes are 10/10 noise replicates, not independent seeds.
W10 cannot feed SEAL-2. The order is: seeded scenes (new card), then re-render, then calibrate. CP bounds become meaningful only once `split_detail` is empty and the scene sha varies per seed.
