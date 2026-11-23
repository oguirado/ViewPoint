# HB-BVQA-TRUTH — Cycles ground-truth bench, labels, score, calibration (`blender_visual_qa.truth`, `bpy_side/{passes,bench_scenes,decode_exr,q}`)

Status: active · Subject: blender-visual-qa HEAD 2390e8d · Normative sources (CITED, not copied): `BSPEC_BTRUTH`, `BDESIGN_TRUTH`, `BSPEC_BINT` R-B8/R-B9 (campaign `v12bs/`); vision-engine `SPEC_CORE` §6.1 (T0 rule) and `vqa.profile/v1`. Etags: MEASURED, CITED, TARGET. Calibration numbers live in `reports/`, never restated here.

## <a id="hb-s0"></a>0. Reading map

| Persona | Read | Question → section |
|---|---|---|
| implementer | hb-s1, hb-s5 | What does a bench run produce? → hb-s1 |
| calibrator | hb-s2, hb-s4, hb-s6 | How is `T0_Q4` fitted, what refuses? → hb-s4, hb-s6 |
| verifier | hb-s4, hb-s9 | Which claims have tests? → hb-s4 |

Prerequisites: HB-BVQA-DRIVE (seed, journal, boundary). Conventions: integers only (ppm, q4, mm, um); tile tables in ascending Morton order over the visioncore grid; a label is a pure function of (truth arrays, journal, profile).

## <a id="hb-s1"></a>1. Contracts

Pipeline: bench scene (procedural, no `.blend` in git) → Blender beauty + truth passes (EXR) → quantised `.npy` → visioncore analyze (read-only import) → `labels.label_sequence` → `score.score_frames` (`bvqa.calib-row/v1`) → `calibrate.calibrate` → `vqa.profile/v1` (only `T0_Q4` changes; other findings go to `proposals[]`) + `calib_report.json` + `SUMMARY.md`.
`run_bench` rc: 0 ok · 2 usage/environment/step error · 3 some bench failed (report written) · 5 calibration refused (`E_GATE truth_targets`, report written, no profile). Flags: `--out --benches --frames --res --spp --renderer --strict --seeds fit:<a-b>,gate:<c-d> --jobs N --decode inblender|external --determinism-check`.
Stale grid between truth and vision → `E_STALE reason grid` (rc 4). Quantisers (`bpy_side/q.py`: `mm um milli_deg ppm q4 i8_normal`) are the only place floats become integers.

## <a id="hb-s2"></a>2. Types, weights and budgets

Thresholds in `catalogue/thresholds.tsv`: `MOV_Q4` 8, `STILL_N` 8, `OCC_PERMILLE` 10, `RIGID_SHARE_PCT` 90, `EDGE_RING` 1 (DESIGN_CHOICE until the calibration step passes). `T0_still = max(32, (5q+3)//4)` with q the p99.9-rank still-probe residual (CITED SPEC_CORE §6.1); `t0_fp` = least T0 giving zero false positives under the band. Run caps: tiny renders, ≤ 4 pinned decode jobs (`--jobs`), CPU only.
Seeds: `beauty_seed`, `seed_expression` use `canon.cycles_seed(bench_no,k,ri)`; FIT and GATE seed sets must be disjoint (`parse_seeds` → `E_GATE fit_gate_overlap` rc 5; `calibrate` refusal `seeds_overlap`); `check_seed_collisions` guards per-frame seed collisions.

## <a id="hb-s3"></a>3. DAG and commands

Layers: `bpy_side/bench_scenes.py` (B1..B8 incl. B1x, B4f, B4n) and `bpy_side/passes.py` run inside Blender; `bpy_side/decode_exr.py` is pure (stdlib+numpy, no bpy at any depth; `g_bpy_boundary` proves it); host `truth/exr_none.py` + `truth/decode_worker.py` decode NONE-codec EXRs externally (`pending.jsonl`); `labels` → `score` → `calibrate` are numpy-only host modules.
Commands: `cd components/blender-visual-qa && PYTHONDONTWRITEBYTECODE=1 timeout 300 python3 -m unittest tests.test_labels tests.test_score tests.test_calibrate tests.test_run_bench tests.test_exr_none tests.test_decode_worker`
Smoke (needs Blender, CPU, scratch): `python3 -m blender_visual_qa.truth.run_bench --out <scratch> --frames 8 --res 320x180 --spp 4 --seeds fit:0-1,gate:0-1 --jobs 4`.

## <a id="hb-s4"></a>4. Triptych

**Claims**
- C-TRU-LABEL (determinism, MEASURED `tests/test_labels*.py`): labelling the same inputs 3× gives identical bytes.
- C-TRU-CAL (determinism, MEASURED `tests/test_calibrate*.py`): calibrate goldens give byte-identical profile + report; failing rows → rc 5, no profile.
- C-TRU-SPLIT (precondition, MEASURED `tests/test_calibrate_w9.py`, `tests/test_bpy_side_w9.py`): FIT ∩ GATE ≠ ∅ refused.
- C-TRU-EXR (equality, MEASURED `tests/test_exr_none.py`, `tests/test_decode_worker.py`): external NONE-EXR decode equals in-Blender decode byte-for-byte; unsupported layouts fail closed.
- C-TRU-THREADS (invariant, MEASURED when Blender runs): truth sha equal at 1 and 4 threads; beauty sha equal with truth passes on/off.
- C-TRU-SCORE (postcondition, MEASURED `tests/test_score*.py`): all metrics are integer ppm; class ids are visioncore CLS ids.
- C-TRU-NUMBERS (TARGET): acceptance recall/FP thresholds are a W10 calibration outcome (`reports/CALIB_*.md`), not a claim of this book.
**Anti-uses** (blocked_use): never refit on GATE data; never patch visioncore (engine fixes go to `proposals[]`); do not read `.blend` assets (all benches are procedural); do not add a seed flag that bypasses `canon.cycles_seed`.
**Oracle**: tests listed in hb-s9; reviewer mutants `bt.*` (BTRUTH §8) are killed by oracle execution, rc 5 (TARGET: conductor gate).

## <a id="hb-s5"></a>5. Subsystems

- Labels — `blender_visual_qa/truth/labels.py` (`label_frame`, `label_sequence`, `apply_temporal`, `pack_truth_tiles`, `parse_truth_tiles`, `regions_truth`).
- Score — `blender_visual_qa/truth/score.py` (`score_frames`, `summarize`, `ppm`).
- Calibrate — `blender_visual_qa/truth/calibrate.py` (`t0_still`, `t0_fp`, `band_ppm`, `fp_attribution`, `check_targets`, `calibrate`).
- Runner — `blender_visual_qa/truth/run_bench.py` (`run_one`, `render_bench`, `vision_analyze`, `determinism_fingerprint`, `parse_seeds`).
- EXR — `blender_visual_qa/truth/exr_none.py#read`, `blender_visual_qa/truth/decode_worker.py#consume`.
- In Blender — `bpy_side/passes.py#run_pass_set`, `bpy_side/bench_scenes.py`, `bpy_side/decode_exr.py#decode_frame`, quantisers anchors `bpy.q.mm`, `bpy.q.um`, `bpy.q.milli_deg`, `bpy.q.ppm`, `bpy.q.q4`, `bpy.q.i8_normal`.

## <a id="hb-s6"></a>6. Limits and search

| limit | blocked_use |
|---|---|
| `fp_pop_contaminated` refusal when the still population has false positives | do not lower `T0` by hand; read the bench and tile class named in the refusal |
| Cycles GPU unsupported on GB10 | do not benchmark GPU; CPU pinned procs only |
| tiny renders (≤ 320x180 in smoke) | do not extrapolate smoke recall to the W10 report |
| Blender noise ≠ Godot noise | do not transplant Godot profiles; adapter is `blender` |
Router entries: "fit T0" → `t0_still` / `t0_fp` in `calibrate.py`; "tile label for pass" → `label_frame`; "decode exr none" → `exr_none.read`; "ppm quantiser" → `bpy.q.ppm`.

## <a id="hb-s7"></a>7. Narrative

For bench B6 (still) the runner renders N frames per FIT seed, vision analyzes them, `raw_resid` collects S1 residuals, `calibrate.t0_still` takes the p99.9 rank and `t0_fp` the least T0 with zero false positives under the band; the profile gets `max(...)`. GATE seeds then score the frozen profile; any failing acceptance row ends in rc 5 with the report but no profile. Decisions: D1 labels are Blender-derived, so no learned component; D2 NONE EXR plus external decode trades disk for A725 CPU time; D3 one runner flag set so W10 can scale seeds without code change.

## <a id="hb-s8"></a>8. Symbols

Generated: `catalogue/symbols.jsonl` / TARGET `hyperbooks/anchors.jsonl`. This book cites anchors and `path#Symbol` forms only; line numbers are not hand-kept.

## <a id="hb-s9"></a>9. Tests and gates

`tests/test_labels.py`, `test_labels_cf1.py`, `test_labels_w9.py`, `test_score.py`, `test_score_cf1.py`, `test_score_w9.py`, `test_score_pin.py`, `test_calibrate.py`, `test_calibrate_cf1.py`, `test_calibrate_w9.py`, `test_run_bench.py`, `test_run_bench_cf1.py`, `test_run_bench_w9.py`, `test_exr_none.py`, `test_decode_worker.py`, `test_bpy_side.py`, `test_bpy_side_cf1.py`, `test_bpy_side_quant.py`, `test_bpy_side_w9.py`, `test_gates_w9.py`. Gates: `python3 tools/gates.py --all`.

## <a id="hb-s10"></a>10. Provenance

Back-reference (code→book): the modules `truth/{labels,score,calibrate,run_bench,exr_none,decode_worker}.py` and `bpy_side/{passes,bench_scenes,decode_exr,q}.py` name this book. The `# @module {..., "book":"HB-BVQA-TRUTH"}` header field is TARGET: code files are outside this lane; required edits are listed in `v12e/room_W13/REPORT_S2.md`.

Source sha256 table (MEASURED at write time, HEAD 2390e8d + working tree; re-seal when sources change):

| file | sha256 |
|---|---|
| `blender_visual_qa/truth/labels.py` | f7a11243eed1769becf9b0e6064a91f3b10931d95b56bd777ccf84345c603b0c |
| `blender_visual_qa/truth/score.py` | 9d02adce9c74eea865a952f885788b255773644e2362c0c75fb0c604d7e7dfa4 |
| `blender_visual_qa/truth/calibrate.py` | 8355cff47588457e60bfa7c4d5a89b1de4b5d7ed31bd2ca382d14b77729d8d39 |
| `blender_visual_qa/truth/run_bench.py` | cb97717ed1201a7062f62d2b73c2a9327fa295bea873f189fb1fdcb4b4c5516c |
| `blender_visual_qa/truth/exr_none.py` | 156804b41b0522db102e7a50ad3af45abd58a686601b59bfbfbfa7e70f71ab7e |
| `blender_visual_qa/truth/decode_worker.py` | 4d8b2cbbc0604a4a6cf58e70712405eb3385a3fc3feb463e42cf86691820bd79 |
| `bpy_side/passes.py` | 3a7184bf7c4b7a2a65909ad6a6e31272eccabf2f09ecb821539998f80de12612 |
| `bpy_side/bench_scenes.py` | 34f246b801369cd0c816e6f71635cac7cf53db2b5d5169ac9a899e6c394b7a05 |
| `bpy_side/decode_exr.py` | 155ad4fc9240d24eed4a71db1dd9fe87ce285db70d5427f64fddf2809ac59a85 |
| `bpy_side/q.py` | 2f8e21cd74071336c3e388f4df5883570e0abb299fdc7f9eaa5ed298709f2566 |
