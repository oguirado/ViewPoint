# ROADMAP_V1_2 — measured storage levers after the seal

Source: maintainer decision 2026-10-17: seal the engine with lever 1 now; levers 2 and 3 become measured v1.2 improvements.
Option 1 is applied as AMD-L2-3 (KL-4 RESOLVED). Options 2 and 3 are v1.2 levers; the metric is X10 (`python3 -m eval.x10_compaction --out <dir>`).
Baseline at the seal [M] (720p x 200 frames, clean_static, full S7 look workload n_look 3,728, n_lu 62): 1,120,641 B before compaction (limit 1,187,264 B),
508,951 B after window expiry (limit 598,016 B). gpu_jitter: 1,123,415 / 508,951 B.

| lever | change | expected effect [H] | spec impact | X10 metric |
|---|---|---|---|---|
| 2 | Drop all-PASS GOP blobs at ingest (ADV-S1-3; SPEC_VMEM §4 classes them BG; §5 keeps a PASS episode pointer only when the blob exists) | before ~213 KB (1,120,641 − 907,254 B `gops.bin`); loses replay of PASS GOPs; does not reduce "after" | explicit §5 rule | `l2_clean_before_bytes` falls below the AMD-L2-3 limit with margin; FG/FAIL episodes untouched (fg_sha_stable x3) |
| 3 | Content-addressed gen files shared across the VM_GEN_KEEP window (store.py/gen.py; SPEC_VMEM §6/§7) | after ~220 KB (one metadata copy + blobs) instead of 4 x ~97.6 KB | §6/§7 change | `l2_clean_after_bytes` and `breakdown.gen`; window_ok stays true |

Acceptance for either lever: re-derive the X10 limits from the measured new floor via a new AMD (no silent tightening), keep all other X10 checks (3/3 cycles, fg_sha_stable, fg_cap control, E-V6) PASS, and report numbers before/after from three identical runs.
Not scheduled here: compact `vmem.unit/v1` look units (option 4, schema change) and any GOP re-encoding.

- JD4-3 (SEAL-1 judge, MINOR): route_code default k misses direct anchor vc.s5.rle for "Where is the GOP XOR run length encoding?" — tune catalogue doc/aliases in v1.2.
