# PREP-X P0 baseline (branch rt eac6d05, python backend, cpus 5-9)

rt_bench --res 720p,1080p,4k --cores 1,5 --frames 60 --repeats 3; median of per-repeat medians; load1 per row in rt_baseline.json (rose 1.4 -> 8.1 during the run: Blender night-2 on 15-19 + Argon workers; loaded_host=false at start).

| res | cores | scenario | S0-S4 ms/f [MEASURED] | p95 | GOP ms | load1 |
|---|---|---|---|---|---|---|
| 720p | 1 | gpu_jitter | 8.8 | 9.1 | 0.26 | 1.39 |
| 720p | 1 | clean_static | 3.3 | 3.5 | 0.26 | 1.39 |
| 720p | 1 | scroll | 64.8 | 67.6 | 5.03 | 2.03 |
| 1080p | 1 | gpu_jitter | 19.2 | 20.1 | 0.15 | 2.1 |
| 1080p | 1 | clean_static | 6.7 | 6.9 | 0.16 | 2.1 |
| 1080p | 1 | scroll | 130.3 | 135.1 | 2.85 | 2.32 |
| 4k | 1 | gpu_jitter | 89.8 | 91.9 | 0.14 | 3.05 |
| 4k | 1 | clean_static | 31.6 | 32.5 | 0.14 | 3.29 |
| 4k | 1 | scroll | 420.0 | 432.3 | 2.74 | 5.88 |
| 720p | 5 | gpu_jitter | 6.6 | 7.0 | 0.26 | 5.89 |
| 720p | 5 | clean_static | 6.4 | 6.7 | 0.26 | 5.89 |
| 720p | 5 | scroll | 63.8 | 66.9 | 5.06 | 6.15 |
| 1080p | 5 | gpu_jitter | 14.5 | 15.2 | 0.16 | 6.46 |
| 1080p | 5 | clean_static | 15.0 | 15.6 | 0.17 | 6.66 |
| 1080p | 5 | scroll | 130.4 | 136.6 | 2.93 | 6.95 |
| 4k | 5 | gpu_jitter | 64.2 | 65.3 | 0.14 | 7.64 |
| 4k | 5 | clean_static | 64.1 | 64.9 | 0.14 | 7.36 |
| 4k | 5 | scroll | 389.7 | 404.7 | 2.75 | 8.06 |

Note: 5-core clean_static is slower than 1 core at 1080p/4K (fork/IPC overhead in the Python parallel path) — a P2 audit item.

## X1-X10 harness sha256

| file | sha256 |
|---|---|
| eval/x10_compaction.py | 7172c2fd67104035 |
| eval/x1_determinism.py | 68465a296c787843 |
| eval/x2_crossrun.py | ecf3cb7ece94a61e |
| eval/x3_power.py | 7d072d52f9348f28 |
| eval/x4_fp.py | 1365a1180c974792 |
| eval/x5_cpu.py | 85f3d87758495387 |
| eval/x8_citations.py | 636b3cdd22a72fc4 |
| eval/x9_vmem.py | 776303dbfe2e7ed8 |
| eval/rt_bench.py | 29c34505b3747e1c |
| eval/run_all.py | a0980e1b11634fa3 |
