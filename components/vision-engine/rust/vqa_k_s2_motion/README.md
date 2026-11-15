# `vqa_k_s2_motion`

Standalone Rust crate providing high-performance S2 motion helpers (`global_vector`, `_axis_shift`, `_tile_sums`, `_islands`, `island_vector`) ported bit-exactly from `visioncore/change.py`.

## Protocol Byte Layout (stdin / stdout, little-endian)

The binary `vqa_k_s2_motion` communicates via stdin and stdout using a little-endian binary protocol.

### Operations (stdin -> binary)

#### Op 0: Quit
- `u8`: `0`
- Response: None.

#### Op 1: Compute One Request
- `u8`: `1`
- `i64`: `width` (frame width in pixels)
- `i64`: `height` (frame height in pixels)
- `i64`: `s` (tile size in pixels)
- `u64`: `len_cur` (byte length of `y_cur` luma plane, `width * height * 2`)
- `u64`: `len_prev` (byte length of `y_prev` luma plane, `width * height * 2`)
- `len_cur` bytes: `y_cur` (int16 array, little-endian)
- `len_prev` bytes: `y_prev` (int16 array, little-endian)
- `u64`: `n_tiles` (count of tile mask elements, `gh * gw`)
- `n_tiles` bytes: `mask` (bool array, uint8 where `0` = false, `1` = true)

**Response (stdout <- binary):**
- `i32`: `status` (`0` on success)
- `i32`: `gdx` (global vector dx)
- `i32`: `gdy` (global vector dy)
- `u64`: `lab_len` (tile count of island label map, `gh * gw`)
- `lab_len * 4` bytes: `lab` (int32 array, little-endian)
- `i32`: `n_islands` (number of islands labelled)
- `i32`: `idx` (island vector dx)
- `i32`: `idy` (island vector dy)

#### Op 3: Bench Request
- `u8`: `3`
- Followed by the same request parameters as Op 1 (`width`, `height`, `s`, `len_cur`, `len_prev`, `y_cur`, `y_prev`, `n_tiles`, `mask`)
- `u32`: `warmup` (number of warmup iterations)
- `u32`: `iters` (number of timed iterations)

**Response (stdout <- binary):**
- `u32`: `n` (number of iterations, equal to `iters`)
- `n * 8` bytes: `times_ns` (array of u64 elapsed nanoseconds per iteration)

---

## 1080p Timing Workload (RAM resident, SplitMix64 seed `0x5EED`)

The 1080p benchmark workload generates frame buffers entirely in RAM:
- Dimensions: `1920 x 1080` (`width = 1920`, `height = 1080`), tile size `s = 16` (`gw = 120`, `gh = 67`).
- Buffers: `y_cur` and `y_prev` initialized with pseudo-random `i16` values generated from SplitMix64 seeded with `0x5EED`.
- Mask: `gh * gw` tile mask filled with pseudo-random boolean values generated from SplitMix64 seeded with `0x5EED`.

## Host Command to Time Pinned with `taskset -c 2-4`

To time the release binary pinned to CPU cores 2-4:

```bash
cargo build --release --offline
taskset -c 2-4 ./target/release/vqa_k_s2_motion
```

Or via python harness:

```bash
taskset -c 2-4 python3 -m unittest tests.test_k_s2_motion
```
