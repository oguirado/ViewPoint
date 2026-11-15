# W9 CPU smoke — candidate engine

STATUS: MEASURED; calibration targets missed; integration BLOCKED.

Engine commit `6b6679a925ff69249faf508aa510e25b2dd19500` [MEASURED]. Wheel SHA256 `73cd0cdd96544b6a06c9e8c3404761701bcaf71d42b50e510e90b2e5a7a52991` [MEASURED]. Built from the unchanged git archive; complete Python package bytes verified by `g_pin`.

Command (run twice; Python startup uses `PYTHONPATH=config` to prefer the pinned wheel):

```sh
python3 -B -m blender_visual_qa.truth.run_bench --out build/room_I/smoke_{a,b} --frames 8 --res 320x180 --spp 4 --seeds fit:0-1,gate:2-3 --jobs 3 --decode external --determinism-check
```

Command quantities above are requested [TARGET]; both runs used those settings [MEASURED]. CPU affinity is assigned by run_bench; Cycles CPU only. Each process is bounded by timeout. The second run used the scratch snapshot; the first read live scripts while writing scenes/results only in lane scratch (deviation).

Both runs: rc `5`, bench failures `0`, rows `52` [MEASURED]. Rows byte-identical [MEASURED], SHA256 `1717aa938c0ad0a39b8c7790fc9887385579ed0734cd94c66ec3249f652fd987` [MEASURED]. Receipts: `build/room_I/smoke_equality.json`, `smoke_a/calib_report.json`, `smoke_b/calib_report.json`.

GATE seeds only determine these checks; FIT seeds remain disjoint. Rates are integer ppm.

| Metric | Value [MEASURED] | Target [TARGET] | Result |
|---|---:|---:|---|
| static_fp | 10 | <= 0 | FAIL |
| rigid_dirty_recall | 968944 | >= 990000 | FAIL |
| moved_class | 274844 | >= 900000 | FAIL |
| occlude_recall | 998882 | >= 980000 | PASS |
| deform_recall | 992788 | >= 980000 | PASS |
| shade_strong_recall | 1000000 | >= 980000 | PASS |
| shade_weak_fp_ppm | 0 | <= 10000 | PASS |
| mv_exact | 265527 | >= 950000 | FAIL |
| mv_err_p95_px | 0 | <= 1 | PASS |
| drift_duty_ppm | 862068 | >= 500000 | PASS |
| flicker_fp_tiles | 0 | <= 0 | PASS |

Temporal class observations (tile rates do not certify seed-level acceptance):

| Class | Dirty ppm [MEASURED] | Class-correct ppm [MEASURED] | Seed target [TARGET] |
|---|---:|---:|---|
| FLICKER | 714285 | 392857 | 30/30; smoke cannot certify |
| FROZEN | 0 | 0 | 30/30; smoke cannot certify |
| DRIFT | 714285 | 714285 | 30/30; smoke cannot certify |

FROZEN coverage-stratum evidence: AUSENTE — no inventar (all stratum denominators empty). FROZEN tile observations are present, but do not supply the missing stratum evidence.

STATIC false positives: B4 / STATIC, 10 tiles [MEASURED]. T0_still `32`, T0_fp `44`, T0_Q4 `44` [MEASURED]. No accepted profile was emitted. No GATE refit or threshold change was made.

Aggregate motion p95 is reported above as emitted by calibration. Worst per-bench GATE p95 is 11 px [MEASURED]; the aggregate result must not be read as a per-bench pass.

Integration blockers: the decoder contains a lazy bpy import; the existing calibration golden has a different params_sha256 under this candidate. Both owner files are outside lane write scope. Smoke misses are observations only.

## Final integration addendum (FX r1, JW9-7)

HISTORICAL (true when the smoke ran, resolved by the W9 integration commit 2390e8d): the decoder's lazy bpy import (now a pure EXR decoder boundary, `g_bpy_boundary` rc 0) and the old calibration golden params_sha256 (refreshed with `tests/golden/truth/params_change_proof.json`: only `H_DEAD` 2 -> 4 differs, old report reproduced byte-identical with the old engine). They are NOT open blockers.

REMAINING for W10, unchanged by this addendum: the smoke calibration misses above stay as measured (STATIC FP 10 B4 tiles, T0 `44` without an accepted profile, FLICKER/FROZEN/DRIFT counts, worst per-bench p95 11 px, empty FROZEN coverage strata). Receipt hashes in this document are retained as emitted.

FX r1 changes to the acceptance path (calibrate.py): a FIT/GATE partition is mandatory (seed AND capture-sha disjoint, still probe bound to FIT, else rc 5 `split_leak`), and every mandatory metric (FROZEN/FLICKER seed-level success, flicker latency, B4n, C1b, C5, B5o motion, S4 IoU) must be present or no profile is emitted. This smoke ran before that gate, so the report above is NOT acceptance evidence; W10 re-runs it. Rows must carry `capture_sha256` and the optional evidence keys listed in `tests/test_calibrate.py::evidence_rows` (lane R emits them).
