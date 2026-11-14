# LANG-X X1 host conformance + timing harness

Checks the K1 (BGRX32 -> RGB24 repack) and K2 (damage rects -> tile bitset) kernels of all 15 seeds in `langx/x1/` against `langx/x1/fixtures/`, and times them at 1080p. It does not modify any seed or fixture. Results go to `results.json` (machine) and are written up in `RESULTS.md`.

## Rerun

```bash
cd langx/x1/host
export TMPDIR=${TMPDIR:-/tmp}           # Go/Rust caches + adapter binaries go to $TMPDIR/langx_x1_host
python3 conformance.py                  # conformance only, all 15 seeds (about 30 s)
python3 conformance.py --time           # + timing: 101 runs, 5 warm-up, pinned to cpus 2-4 (about 15 s more)
python3 conformance.py --seeds go_w2,rust_w1 --time --runs 201
python3 conformance.py --use-committed-bins   # use the committed (corrupt) repack_*.bin, to reproduce the fixture defect
```

Options: `--no-pin` turns off pinning, `--out PATH` changes the results file (default `results.json`).

Requirements: python3 + numpy, go (any version >= 1.22; built with `GOTOOLCHAIN=local GOPROXY=off GOFLAGS=-mod=mod GOWORK=off`), cargo (built with `--release --offline`). No network. `taskset` (util-linux) is needed for pinning.

## How it works

- **Fixtures.** Both JSON files are unwrapped (`{"schema":..., "cases":[...]}`), so seed fixture loaders that expect a list are never used. The committed `repack_src.bin` and `repack_dst.bin` are corrupt: they hold 5099 and 4217 bytes of escaped text where 2216 and 1682 raw bytes are expected, and `fixtures/check_fixtures.py` also flags them. When the lengths do not match, the harness reads the `small_specs` table out of `gen_fixtures.py` with `ast` (it does not execute the file), checks every spec against the JSON (dims, strides, lengths, offsets), and rebuilds the small-case buffers in RAM: SplitMix64 source, `0xEE`-filled destination, and an independent numpy reference.
- **Case handling.** Large K1 cases are regenerated in RAM from their seed. The host reference output is checked once against `expected_fnv64`, and seed outputs are then compared byte for byte. For K1 error cases, the right error code must come back and the destination must be left unchanged.
- **Python seeds** are imported by path under an isolated module name (`langx_x1_<seed>_x1`) and run in-process. Their alternate plain-loop K2 entry points are also run on the 30 non-error K2 cases and reported separately. Error cases are skipped there because some plain paths skip validation.
- **Go and Rust seeds** run through adapters in `adapters/<seed>/`:
  - Go: `go.mod` with `replace langx/x1/<seed> => ../../../<seed>` plus a generic `main.go`. The only per-seed line is the import path. Errors are mapped with `errors.Is` against the seed's `ErrDim` and the other error values.
  - Rust: a bin crate that depends on the seed by path, built with `opt-level=3`, `lto`, `codegen-units=1` and no `target-cpu=native`. A generic `main.rs` maps errors from their Debug variant names (`EDim` or `Dim` becomes `E_DIM`). Its only per-seed setting is `SIGNED_DIMS`: rust_w1 takes `i32` dims, while rust_w2..w5 take `u32`/`usize`, so negative dims are reported as N/A and left out of the denominator.
- **Timing.** K1 runs at 1920x1080 with src_stride 7680 and dst_stride 5760. K2 runs at W=1920, H=1080, s=16 with 64 rects from SplitMix64 seed 0x5EED (`bench_rects`). Each call is timed inside the seed's own runtime (Go `time.Now`, Rust `Instant`, Python `perf_counter_ns`). The harness reports the median and the minimum over N runs. The K2 timing workload's output is checked against the host reference. Frames exist only in RAM and are piped to the adapters, never written to disk.

## Adapter protocol (stdin/stdout, little-endian)

| op | request after the op byte | response |
|---|---|---|
| 0 | nothing (quit) | nothing |
| 1 K1 | i64 w,h,src_stride,dst_stride; u64 src_len,dst_len; src; dst_init | status; u64 n; dst bytes |
| 2 K2 | i64 w,h,s; u64 n; n x (i32 x,y,w,h) | status; u64 popcount; u64 n; n x u64 words |
| 3 K1 bench | as op 1, then u32 warmup, u32 iters | u32 n; n x u64 ns |
| 4 K2 bench | as op 2, then u32 warmup, u32 iters | u32 n; n x u64 ns |

`status` is an i32 with these values:
- 0 ok
- 1 E_DIM, 2 E_STRIDE, 3 E_SHORT, 4 E_RECT, 5 E_GRID
- 97 panic or 98 unknown error, each followed by u32 len and a message
- 99 N/A (the value cannot be represented in the seed's parameter type)
