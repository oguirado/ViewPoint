# vqa_k_st_proto8

Standalone Rust crate for subtile motion search kernel (`subtile_motion`), exact 8x8 block matching over 3x3 tile neighbourhood.

Uses Rust `std` only with zero external dependencies, supporting offline build (`cargo build --offline`).

## Little-Endian Binary Protocol Layout

Communication over process `stdin` (input) and `stdout` (output) in little-endian binary format.

### Opcodes

- `op 0` (quit):
  - Request: `0x00` (1 byte `u8`).
  - Response: None. Process exits with status 0.

- `op 1` (compute single request):
  - Request:
    - `op`: `u8` (value `1`)
    - `w`: `u32` (frame width)
    - `h`: `u32` (frame height)
    - `s`: `u32` (tile size in pixels, e.g., 16 or 32; 0 for grid_params default)
    - `max_d`: `i32` (max search displacement in pixels)
    - `prev_luma`: `w * h` bytes (`u8`)
    - `cur_luma`: `w * h` bytes (`u8`)
    - `n_tiles`: `u32` (number of tiles to evaluate)
    - `tiles`: `n_tiles` pairs of `(tx: i32, ty: i32)` (each tile coordinate is 4 + 4 = 8 bytes)
  - Response:
    - `status`: `i32` (`0` for success)
    - `results`: `n_tiles` x 16-byte tile result structures:
      - `found`: `i32` (`1` if match found, `0` if None)
      - `dx`: `i32` (horizontal displacement)
      - `dy`: `i32` (vertical displacement)
      - `n_blocks`: `u32` (number of changed 8x8 blocks in union region)

- `op 3` (benchmark request):
  - Request:
    - Same header, frame buffers, `n_tiles`, and `tiles` array as `op 1`.
    - `warmup`: `u32` (number of warmup iterations)
    - `iters`: `u32` (number of measured iterations)
  - Response:
    - `n`: `u32` (equals `iters`)
    - `durations`: `n` x `u64` nanoseconds taken for each iteration.

## 1080p Timing Workload (RAM)

Generated in RAM from SplitMix64 PRNG with seed `0x5EED`:
- Frame dimensions: `W = 1920`, `H = 1080`, `s = 16`, `max_d = 15`.
- `prev_luma` and `cur_luma` filled from SplitMix64 stream (`seed = 0x5EED`).
- Tile evaluation set: All `120 x 68 = 8160` tiles evaluated over full 1080p frame.

## Host Timing Command

To time the 1080p workload pinned to CPUs 2-4 on host:

```bash
cargo build --release --offline
taskset -c 2-4 ./target/release/vqa_k_st_proto8
```

Note: Any timing taken inside a Virtual Machine sandbox is labeled as **VM** (diagnostic only, host decides performance).
