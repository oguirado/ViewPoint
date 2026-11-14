# LANG-X X1: host conformance and timing results

- **Repo:** `vision-engine`, branch `rt` at 1cefd35. This harness is uncommitted, under `langx/x1/host/`.
- **Seeds:** 15 (go_w1..w5, py_w1..w5, rust_w1..w5).
- **Fixtures:** `langx.x1.fixtures/v1`, with 38 K1 cases and 41 K2 cases [MEASURED].
- **Raw data:** `results.json`. To rerun, see `README.md`.

Host: NVIDIA GB10, aarch64. The timing runs were pinned with `taskset -c 2-4`; cpus 2-4 are Cortex-A725 efficiency cores (MIDR 0x410fd871, max 2808 MHz) [MEASURED]. Load average was about 8 during the runs (another user's Blender was at about 360% CPU), so absolute numbers carry some noise. Two back-to-back runs agreed within about 1.5% on every median [MEASURED].

Toolchains: go1.26.4 (default `go build`, `-trimpath`); rustc 1.97.0 (`--release`, opt-level 3, LTO, codegen-units 1, no `target-cpu=native`); CPython 3.12.3 with numpy 2.4.2 [MEASURED].

**Budget:** 1080p60 allows 16.7 ms per frame for everything [TARGET]. The "% budget" column below is (K1 + K2) / 16.7 ms.

## Fixture defect found (not a seed defect)

The committed `fixtures/repack_src.bin` and `repack_dst.bin` are corrupt [MEASURED]:

| File | Committed size | Expected size (sum of `*_buf_len`) |
|---|---|---|
| `repack_src.bin` | 5099 B | 2216 B |
| `repack_dst.bin` | 4217 B | 1682 B |

The content is Python-escaped text (for example the literal characters `d\xd7...`) instead of raw bytes. The fixture suite's own `check_fixtures.py` reports "not byte-identical to regenerated fixture" yet still exits with rc 0.

With the committed bins, all 15 seeds fail the same 23 small K1 cases (15/38), and the first byte differs in each case. The harness therefore rebuilds the small-case buffers in RAM. It reads the `small_specs` table from `gen_fixtures.py` (parsed, not executed) and checks every spec against the JSON's dims, strides, lengths and offsets. `--use-committed-bins` reproduces the failure. The fixtures should be regenerated and recommitted. That fix is out of scope for this harness.

## Per seed

All numbers are [MEASURED] on the host, cpus 2-4, as the median of 101 calls after 5 warm-ups. N/A marks a fixture input that the seed's API types cannot represent: negative w/h/s passed to a `u32`/`usize` parameter. N/A cases are left out of the denominator.

| Seed | K1 pass | K2 pass | N/A (K1/K2) | First mismatch | K1 @1080p ms | K2 @1080p ms | % budget | Adapter glue |
|---|---|---|---|---|---|---|---|---|
| go_w1 | 38/38 | 41/41 | 0/0 | none | 3.770 | 0.0112 | 22.6 | generic main.go; only the import path differs |
| go_w2 | 38/38 | 41/41 | 0/0 | none | 1.622 | 0.0112 | 9.8 | same |
| go_w3 | 38/38 | 41/41 | 0/0 | none | 1.645 | 0.0111 | 9.9 | same; error messages are not `E_*` strings, so `errors.Is` mapping is required |
| go_w4 | 38/38 | 41/41 | 0/0 | none | 3.755 | 0.0112 | 22.6 | same |
| go_w5 | 38/38 | 41/41 | 0/0 | none | 3.768 | 0.0113 | 22.6 | same |
| py_w1 | 38/38 | 41/41 | 0/0 | none | 11.591 | 0.1439 | 70.3 | none (in-process import); error code taken from `args[0]` |
| py_w2 | 38/38 | 41/41 | 0/0 | none | 11.585 | 0.1456 | 70.2 | none; error code taken from `.code` |
| py_w3 | 38/38 | 41/41 | 0/0 | none | 11.595 | 0.8397 | 74.5 | none |
| py_w4 | 38/38 | 41/41 | 0/0 | none | 11.588 | 0.1414 | 70.2 | none |
| py_w5 | 38/38 | 41/41 | 0/0 | none | 2.352 | 0.0993 | 14.7 | none |
| rust_w1 | 38/38 | 41/41 | 0/0 | none | 0.809 | 0.0121 | 4.9 | generic main.rs with `SIGNED_DIMS=true` (API takes `i32` w/h/s) |
| rust_w2 | 36/36 | 40/40 | 2/1 | none | 0.814 | 0.0121 | 5.0 | generic main.rs with `SIGNED_DIMS=false` (`usize` dims) |
| rust_w3 | 36/36 | 40/40 | 2/1 | none | 0.811 | 0.0121 | 4.9 | same (`u32` dims) |
| rust_w4 | 36/36 | 40/40 | 2/1 | none | 0.815 | 0.0091 | 4.9 | same (`u32` dims) |
| rust_w5 | 36/36 | 40/40 | 2/1 | none | 0.814 | 0.0121 | 5.0 | same (`u32` dims) |

### Details

- **N/A cases.** The N/A cases are K1 `err_dim_w_negative` and `err_dim_h_negative`, and K2 `err_grid_s_negative`. Rust's type system rejects these inputs at compile time.
- **K1 error cases.** All 8 K1 error cases returned the expected code and left the destination untouched, for every seed.
- **Python alternate K2 path.** The plain-loop entry point (`use_numpy_path=False`, `use_numpy=False`, `damage_tiles_plain`, `use_plain_loop=True`) passed 30/30 non-error K2 cases on all 5 Python seeds [MEASURED].
- **Timed K2 workload.** The output of the timed K2 workload (64 rects, s=16) matched the host reference for all 15 seeds [MEASURED].
- **Negative control.** Mutating one output byte (K1) or one word bit (K2) in an isolated Python seed module dropped the pass counts to 11/38 and 11/41, the error-only cases. The harness discriminates [MEASURED].

### Why the speeds differ (read from the source)

- **go_w2 / go_w3** unroll 4 pixels per iteration and run about 2.3x faster than the plain per-pixel loop in go_w1, go_w4 and go_w5.
- **py_w5** writes the three channels with separate strided assignments (`dst[:,:,0] = src[:,:,2]` and so on). py_w1..w4 use a fancy-index gather (`src[:,:,[2,1,0]]`), which builds a temporary and is about 4.9x slower.
- **py_w3 K2** calls `np.arange` per rect, then `np.unique` and `bitwise_or.at`, which is about 5.8x slower than the other Python seeds.

## Per language

The numbers below are [MEASURED] on the host, cpus 2-4, as medians.

| Language | Seeds | Conformance (all seeds) | K1 @1080p ms best / worst | K2 @1080p ms best / worst | Best (K1 + K2) as % of 16.7 ms |
|---|---|---|---|---|---|
| Rust | 5 | 100% (4 seeds have 3 N/A inputs: unsigned API) | 0.809 / 0.815 | 0.0091 / 0.0121 | 4.9 |
| Go | 5 | 100% | 1.622 / 3.770 | 0.0111 / 0.0113 | 9.8 |
| Python + numpy | 5 | 100% | 2.352 / 11.595 | 0.0993 / 0.8397 | 14.7 |

## Update 21:55 (conductor)
The committed `repack_src.bin` / `repack_dst.bin` from the web fixtures PR (#60) were escaped text (5099 B / 4217 B), written through a text-only GitHub API. The conductor regenerated them on the host with the PR's own deterministic `gen_fixtures.py` (2216 B / 1682 B); `check_fixtures.py` now rc 0 (it was rc 1 before). Re-run with `--use-committed-bins`: all 15 seeds pass every representable case (K1 38/38 or 36/36 + 2 N/A, K2 41/41 or 40/40 + 1 N/A) [MEASURED].
