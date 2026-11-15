# W6 X-zero results at E_FROZEN b7d7fe9 (SEAL-1 candidate)

Conductor run 2026-10-17 19:13-19:22 from `git archive b7d7fe9` (build/w6_frozen), `taskset -c 5-9` (X5 `taskset -c 5`, one X925 core), host load1 5.6-6.8 [MEASURED].
Held-out test split **v3** (`eval/splits/x4_v3.json`, sha 5269d68e..., X4/X3 test seeds 2000..2029, registered before any analysis, used exactly once).
Engine parameters fitted on CAL only (X4 seeds 30..199, X3 seeds 40..69): NR `SIG_DEAD_Q4` 24 + `H_DEAD` 2 (tests/golden/nr_sigdead_fit.json).
Three repeats; jsonl sha256 prefixes per repeat (X5 rows hold wall timings, so they differ per repeat by design).

| driver | gate | verdict | key numbers [MEASURED] | sha r1 r2 r3 |
|---|---|---|---|---|
| X1 | G-E1 | PASS | {"runs": 18} | 1bc4db19785f 1bc4db19785f 1bc4db19785f |
| X3 | G-E3 | PASS | {"aggregate_cp95_lower_ppm": 986075, "seeds": 30} | 1ac99e19b2e4 1ac99e19b2e4 1ac99e19b2e4 |
| X4 | G-E2 | PASS | {"fp_runs": 0, "fp_tiles": 0, "runs": 180, "seeds": 30, "tile_bound_ppb": 680, "tile_fp_cp95_upper_ppb": 427, "tile_frames": 7020000} | 9e67ae6a37ba 9e67ae6a37ba 9e67ae6a37ba |
| X5 | G-E4 | PASS | {"median_of_medians_us": {"1280x720": 8996, "1920x1080": 18245}, "median_of_p95_us": {"1280x720": 9395, "1920x1080": 18820}} | 97b46417208f b3921553c05b 2d9c2d9a4621 |
| X8 | G-E6a | PASS | {} | 97c61b5e2f18 97c61b5e2f18 97c61b5e2f18 |
| X9 | G-E8 | PASS | {} | b1caa04c4793 b1caa04c4793 b1caa04c4793 |
| X10 | G-E8 | PASS | {"l2_clean_after_bytes": {"clean_static": 508951, "gpu_jitter": 508951}, "l2_clean_before_bytes": {"clean_static": 1120641, "gpu_jitter": 1123415}} | fe47d48e22cd fe47d48e22cd fe47d48e22cd |

History: split v1 (seeds 0..29) retired after inspection during diagnosis (Argon ADV_SEAL1); split v2 (1000..1029) consumed by the a66d99e run
(G-E2 FAIL 2/180 iid_noise FP runs, KL-5); H_DEAD re-fit infeasible (HD), solved by the NR CORE lever. Storage budgets: AMD-L2-1/AMD-L2-2 (derived).
Final candidate b7d7fe9 adds FX7 (real S7 looks in X10, FG episode budget, X4 live binding) and AMD-L2-3 (operator option 1 storage budgets). X1/X3/X4 jsonl identical to 05adee8 (classification unchanged).
