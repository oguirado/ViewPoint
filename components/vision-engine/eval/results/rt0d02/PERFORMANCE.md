# RT0D02 measured timing

All timings are ms/frame; medians of 3 repeats over 60 RAM-resident frames [MEASURED]. CPU affinity: CPU 5 for W=1 and CPUs 5–9 for W=5 [MEASURED]. The before arm uses a4d95ac S2 with the same S0/S1/S3+ implementation.

| Resolution | Scene | W | S2 before | S2 after | S2 speedup | Whole before | Whole after |
|---|---|---:|---:|---:|---:|---:|---:|
| 1280x720 | clean_static | 1 | 1.33 | 1.33 | 1.00x | 3.93 | 3.92 |
| 1280x720 | clean_static | 5 | 1.32 | 1.36 | 0.98x | 3.55 | 3.53 |
| 1280x720 | scroll | 1 | 31.39 | 15.89 | 1.98x | 72.50 | 57.28 |
| 1280x720 | scroll | 5 | 30.58 | 15.73 | 1.94x | 70.15 | 55.21 |
| 1920x1080 | clean_static | 1 | 1.52 | 1.53 | 0.99x | 7.42 | 7.34 |
| 1920x1080 | clean_static | 5 | 1.55 | 1.51 | 1.03x | 5.44 | 5.26 |
| 1920x1080 | scroll | 1 | 64.76 | 20.87 | 3.10x | 106.53 | 62.53 |
| 1920x1080 | scroll | 5 | 59.04 | 20.66 | 2.86x | 95.67 | 57.35 |
| 3840x2160 | clean_static | 1 | 6.47 | 6.42 | 1.01x | 31.68 | 31.44 |
| 3840x2160 | clean_static | 5 | 6.34 | 6.27 | 1.01x | 16.77 | 16.52 |
| 3840x2160 | scroll | 1 | 257.18 | 71.18 | 3.61x | 380.30 | 193.91 |
| 3840x2160 | scroll | 5 | 265.34 | 77.23 | 3.44x | 361.60 | 174.38 |

Load1 range: 5.75–9.51 [MEASURED]. Every timed run produced matching output hashes. The 4K scroll target is at least 3x S2 speedup [TARGET]; both worker counts passed. The executed negative replaces after timings with before timings and is rejected.
