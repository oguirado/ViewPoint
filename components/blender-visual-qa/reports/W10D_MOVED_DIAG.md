# W10D lane D — MOVED-miss diagnosis (truth vs engine)

Base: blender-visual-qa main 2d90665, engine pin ve-sealed-1-law2 (wheel ebcf3854). Engine untouched.
Tool: `blender_visual_qa/truth/diag_moved.py` (schema `bvqa.moved-diag/v1`); tests `tests/test_w10d_moved_diag.py` (6).
Command (CAL seeds only; GATE seeds 10..39 are refused with rc 5):

    BVQA_CPU_CAP=15-19 timeout 900 taskset -c 15-19 env PYTHONPATH=. python3 -m blender_visual_qa.truth.diag_moved \
      --out build/room_D/diag_cal_s0-1.r3.json --seeds 0-1 --frames 8 --res 320x180 --spp 4 --benches B1,B1x,B2,B4,B5o,B7,BS

Scale: 2 CAL seeds (0,1), 8 f, 320x180, spp 4, 7 benches with RIGID tiles, ~46 s wall. Frames only in the RAM workspace
(`/dev/shm/vqa-ramws/bvqa/w10d/...`, each unit released after its diagnosis); only numbers on disk. Ran 3 times; per-tile
records and summary byte-identical (sha 394d3c5b… / 1edd7cdf…). Assumption: CAL ≡ the W10b FIT arm `fit:0-9`.
Cross-check: leaving out B1x (not one of the 9 CALIB scenes), recall is 120/492 = 0.244, vs CALIB_2026-10-17b 0.249.

## Cause rule (one cause per RIGID tile, frames t >= 1, first match wins)
SAD uses the engine's own convention: cur(p) ≈ prev(p − c) on the YCoCg-R luma, search window d = min(s, MOTION_D_MAX) = 16.
Noise allowance = max(p90 of the zero-vector SAD over STATIC tiles, s²·4/16).
- **a aperture**: SAD(0) ≤ noise **and** SAD(true) ≤ noise.
- **b ambiguity**: the true vector is in the window and among the near-minima (SAD ≤ m + max(m/4, noise)), and that
  set has more than one 8-connected component (**periodic**) or more than 9 vectors (**plateau**, i.e. a 1-D edge).
- **c boundary/occlusion**: any tile pixel is occluded or disoccluded (`labels.pixel_predicates` occ), lies on the
  object-edge ring, or has a motion vector other than the tile's.
- **d range/subpix**: |mv|∞ > d, or the source block is outside the padded frame, or the median |frac(mv)| ≥ 1/4 px.
- **e engine miss**: none of the above.

## Result (RIGID n = 584; hit = engine class MOVED)
| cause | n | ppm of RIGID | miss | ppm of misses | hit | mv exact |
|---|---|---|---|---|---|---|
| a aperture | 2 | 3424 | 2 | 4310 | 0 | 0 |
| b ambiguity | 408 | 698630 | 312 | 672413 | 96 | 90 |
| c boundary/occlusion | 97 | 166095 | 77 | 165948 | 20 | 20 |
| d range/subpix | 72 | 123287 | 72 | 155172 | 0 | 0 |
| e engine miss | 5 | 8561 | 1 | 2155 | 4 | 4 |

- b splits into periodic: 308 tiles (34 hits, all in B5o), and plateau: 100 tiles (62 hits).
- All 72 d tiles are out of range (B1x, mv 48 px > 16). None is sub-pixel.
- Recall over all RIGID tiles: **0.205** (120/584). mv_exact 114/584.
- **recall_obs** (a and b excluded, as the card defines it): **0.138** (24/174). mv_exact_obs 0.138.
- Variant that counts plateau tiles as observable: 0.314 (n=274).
- Engine-addressable tiles (e only): n = 5, recall 0.800. Too few to test the ≥ 0.95 target. The single e miss is
  B7 s0 f7 t215 (tx19, ty5), mv (16,0), engine class CHANGED.

Per bench, n(miss) for a / b / c / d / e:
| bench | RIGID | hit | a / b / c / d / e | recall_obs ppm |
|---|---|---|---|---|
| B1 | 296 | 10 | 0(0) / 282(272) / 14(14) / 0(0) / 0(0) | 0 |
| B1x | 92 | 0 | 0(0) / 0(0) / 20(20) / 72(72) / 0(0) | 0 |
| B2 | 26 | 18 | 2(2) / 20(4) / 4(2) / 0(0) / 0(0) | 500000 |
| B4 | 4 | 4 | 0(0) / 2(0) / 2(0) / 0(0) / 0(0) | 1000000 |
| B5o | 84 | 84 | 0(0) / 66(0) / 14(0) / 0(0) / 4(0) | 1000000 |
| B7 | 78 | 0 | 0(0) / 36(36) / 41(41) / 0(0) / 1(1) | 0 |
| BS | 4 | 4 | 0(0) / 2(0) / 2(0) / 0(0) / 0(0) | 1000000 |

## Examples (bench, seed, frame, Morton tile, tx, ty, truth mv, engine class)
- a: (B2,0,7,74,8,3,(4,0),JITTER); (B2,1,7,74,8,3,(4,0),JITTER). Only 2 tiles exist.
- b: (B1,0,1,7,3,1,(16,0),CHANGED); (B1,0,2,18,4,1,(16,0),CHANGED); (B1,0,3,19,5,1,(16,0),CHANGED)
- c: (B1,0,1,37,3,4,(8,0),CHANGED); (B1,0,2,37,3,4,(8,0),CHANGED); (B1,0,3,48,4,4,(8,0),CHANGED)
- d: (B1x,0,1,19,5,1,(48,0),CHANGED); (B1x,0,2,66,8,1,(48,0),CHANGED); (B1x,0,3,71,11,1,(48,0),CHANGED)
- e: (B7,0,7,215,19,5,(16,0),CHANGED), a miss; then two hits for contrast: (B5o,0,2,105,9,6,(-3,0),MOVED) and (B5o,0,3,105,9,6,(-3,0),MOVED)

## Verdict
1. Truth/bench causes explain **463 of 464 misses (99.8 %)**:
   - b, 67 %: B1's 12 px checkers and 1-D edges, and B7.
   - c, 17 %: tiles that straddle a boundary. The RIGID label tolerates 10 % non-rigid pixels; the engine's SAD-ratio
     test does not.
   - d, 16 %: B1x moves 48 px/f, which the engine by design cannot reach (search range = tile size).
   - a, 0.4 %.

   Only 1 miss is a genuine engine miss.
2. Cause a is small because CF1 already demotes residual-free geometric tiles to INVISIBLE (excluded). The remaining
   measurement gap is b.
3. b is not unobservable *in context*: in B5o the engine resolves every periodic and plateau tile through the published
   global vector (camera pan). The card's mask still excludes b, so recall_obs is the conservative figure.
4. Even with the mask, recall_obs is 0.138 against a 0.90 target. The rest of the gap is c and d, which are not engine
   defects either. **No threshold change and no engine change is justified by this data.**

## Truth-label amendment (exact; this lane does not apply it)
- `labels.py`: add `FLAG_UNOBSERVABLE = 8`. Bit 3 of the existing `flags` byte is free (1 EDGE_ONLY, 2 BG, 4 CAM_MOTION
  are taken), so `bvqa.truth-tile/v1` stays byte-compatible.
- `run_bench.analyze_bench`: after `truth_labels`, run `diag_moved.diagnose_sequence(rgbs, truths, cls, mv, g, labs)` on
  the decoded beauty (the clean capture when one exists). Then set `flags[f, ~diag_moved.observable_mask(tiles_f, N)] |= 8`
  and repack `tiles_truth.bin`. The mask depends only on beauty and truth, never on the engine's verdict.
- `score.score_frames(..., flags=None)`: `rig = (tl == 3) & (|mv_truth| > 0) & ((flags & 8) == 0)` and emit
  `row["mv_n_unobs"]`. `moved_class` and `mv_exact` then use the observable denominator. Report the unmasked recall
  alongside it.
- Register `NOISE_MIN_Q4=4`, `NOISE_PCT=90`, `BASIN_MAX=9`, `TOL_DEN=4`, `SUBPIX_Q4=4` in `catalogue/thresholds.tsv` as
  DESIGN_CHOICE (uncalibrated). They are truth constants, not engine thresholds.
- Optional second bit (not in the card's mask): `FLAG_OUT_OF_RANGE = 16` for |mv|∞ > s, with CHANGED accepted.

## proposals[] (C7/X2; engine sealed, nothing applied)
- P-D1: for tiles straddling a boundary (cause c), score SAD on the clipped/majority-motion window (the analogue of the
  existing `mexp` clipped test) instead of the full tile. Evidence: 77 c misses, 20/97 hits.
- P-D2: island/global vector search beyond s for B1x-type motion, or keep it documented as out of scope (72 d misses).
