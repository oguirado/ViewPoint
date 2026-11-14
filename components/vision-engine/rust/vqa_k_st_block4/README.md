# `vqa_k_st_block4`

Standalone Rust implementation of the 4x4 block subtile motion search variant (`subtile_motion`) bit-exact with `eval/v2x/st_block4.py`.

## Protocol Specification (stdin / stdout, Little-Endian)

The binary process `vqa_k_st_block4` reads operations from standard input and writes binary responses to standard output using standard little-endian formatting.

| Op Byte | Input Payload | Output Response |
|---|---|---|
| `0` (Quit) | None | None (process exits loop and terminates) |
| `1` (Compute) | `i32` W, H, s, max_d<br>`u32` n_tiles<br>n_tiles x (`i32` tx, ty)<br>`u64` frame_bytes<br>frame_bytes x `u8` prev_luma<br>frame_bytes x `u8` cur_luma | `i32` status (0 = ok)<br>`u32` n_outputs (equals n_tiles)<br>n_outputs x (`i32` dx, dy, n_blocks, valid) |
| `3` (Bench) | Same request as Op 1,<br>then `u32` warmup, `u32` iters | `u32` n_iters<br>n_iters x `u64` nanoseconds |

### Result Encoding
In Op 1 output:
- `status`: `i32` zero on success.
- For each evaluated tile:
  - `valid == 1`: Match found; `dx`, `dy` are displacements and `n_blocks` is changed 4x4 block count.
  - `valid == 0`: No match or invalid translation; `dx=0, dy=0, n_blocks=0`.

## Benchmark Workload (1080p RAM Workload)

The benchmark workload evaluates a 1920x1080 frame pair with `s=32`, `max_d=16` over all `gw * gh` tiles (60 x 34 = 2040 tiles). Frames are generated in RAM using SplitMix64 PRNG with seed `0x5EED`.

### Host Timing Command

To time the benchmark workload pinned on CPU cores 2-4:

```bash
cargo build --release --offline --manifest-path rust/vqa_k_st_block4/Cargo.toml
taskset -c 2-4 rust/vqa_k_st_block4/target/release/vqa_k_st_block4
```

Note: Label any timing measurements obtained inside a virtual machine as `VM`, never `MEASURED`. The host decides performance.
