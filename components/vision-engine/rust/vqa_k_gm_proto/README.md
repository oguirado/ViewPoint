# vqa_k_gm_proto

Standalone Rust crate providing a bit-exact port of the `estimate_global_motion` and `consensus_tiles` global motion estimation kernels from `eval/v2proto_global_motion.py`.

## IPC Protocol Byte Layout (Little-Endian)

The binary `vqa_k_gm_proto` communicates over `stdin` and `stdout` using little-endian binary framing.

### Op 0: Quit
- **Request**: `op` (`i32` = 0)
- **Response**: None (process exits loop)

### Op 1: Compute Global Motion and Consensus Tiles
- **Request**:
  - `op`: `i32` (1)
  - `h`: `u32` (frame height)
  - `w`: `u32` (frame width)
  - `max_d`: `i32` (maximum displacement search range)
  - `k_top`: `u32` (top coarse candidates count, e.g. 16)
  - `s`: `u32` (tile size, e.g. 16)
  - `prev_luma`: `h * w` raw bytes (`uint8`)
  - `cur_luma`: `h * w` raw bytes (`uint8`)
- **Response**:
  - `status`: `i32` (0 for success)
  - `has_gm`: `u8` (1 if motion estimated, 0 if None/flat/ambiguous)
  - `dx`: `i32` (horizontal displacement)
  - `dy`: `i32` (vertical displacement)
  - `score`: `i64` (normalized full SAD score)
  - `gh`: `u32` (grid height in tiles)
  - `gw`: `u32` (grid width in tiles)
  - `consensus_mask`: `gh * gw` bytes (`0` or `1` boolean mask in row-major order)

### Op 2: Explicit Consensus Tiles Calculation
- **Request**:
  - `op`: `i32` (2)
  - `h`: `u32`
  - `w`: `u32`
  - `s`: `u32`
  - `dx`: `i32`
  - `dy`: `i32`
  - `prev_luma`: `h * w` bytes
  - `cur_luma`: `h * w` bytes
- **Response**:
  - `status`: `i32` (0)
  - `gh`: `u32`
  - `gw`: `u32`
  - `consensus_mask`: `gh * gw` bytes (`0` or `1`)

### Op 3: Bench Workload Timing
- **Request**:
  - Same fields as Op 1 (`op=3`, `h`, `w`, `max_d`, `k_top`, `s`, `prev_luma`, `cur_luma`), followed by:
  - `warmup`: `u32` (warmup iteration count)
  - `iters`: `u32` (benchmark iteration count)
- **Response**:
  - `n`: `u32` (iteration count = `iters`)
  - `n` x `u64`: array of execution time in nanoseconds per iteration

---

## 1080p Timing Workload (SplitMix64 Seed 0x5EED)

The 1080p benchmark frame pair (1920x1080) is generated deterministically in RAM from SplitMix64 seed `0x5EED` for `prev` and `0x5EED + 1` for `cur`.

- Frame dimensions: `1920 x 1080`
- Parameters: `max_d = 100`, `k_top = 16`, `s = 16`, `warmup = 5`, `iters = 20`.
- Diagnostic VM Timing: median ~1.86 s (VM)

---

## Exact Host Pinned Timing Command

To build and run the pinned benchmark workload pinned on CPU cores 2-4:

```bash
cargo build --release --offline --manifest-path rust/vqa_k_gm_proto/Cargo.toml
taskset -c 2-4 ./rust/vqa_k_gm_proto/target/release/vqa_k_gm_proto
```
