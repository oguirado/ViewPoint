# `vqa_k_s1_desc` - Standalone S1a Perceptual Descriptor Kernel Crate

`vqa_k_s1_desc` is a standalone, pure Rust crate (std only, offline, no external dependencies) porting the S1a perceptual descriptor kernel (`_compute`, `luma_of_padded`, `ycocg`, `isqrt`) from `visioncore/desc.py` bit-exactly.

## Protocol Byte Layout (Little-Endian stdin/stdout)

The process communicates via little-endian binary requests over `stdin` and writes little-endian binary responses to `stdout`.

### Op 0: Quit
- Request: `u8 op = 0`
- Response: None (process terminates with exit code 0)

### Op 1: Compute Single Request
- Request Payload:
  - `u32 W`: Frame width in pixels.
  - `u32 H`: Frame height in pixels.
  - `u32 n_tiles`: Number of requested tile rank indices (0 evaluates all $N$ tiles in Morton order).
  - `u32[n_tiles] requested_tiles`: Requested tile rank indices ($0 \le \text{rank} < N$).
  - `u32 frame_len`: Length of frame RGB24 bytes ($W \times H \times 3$ raw or $(gw \cdot s) \times (gh \cdot s) \times 3$ padded).
  - `u8[frame_len] frame_bytes`: Frame pixel buffer in RAM.
  - `u32 prev_len`: Length of previous frame RGB24 bytes (0 if none).
  - `u8[prev_len] prev_bytes`: Optional previous frame pixel buffer in RAM.
- Response Payload:
  - `i32 status`: Exit status (0 = OK, non-zero = error).
  - `u32 n_out_tiles`: Number of returned tile descriptor records.
  - `u32 out_bytes_len`: Total length of output bytes ($n_{\text{out\_tiles}} \times 32$).
  - `u8[out_bytes_len] descs`: Contiguous 32-byte packed tile records (`TileDesc`).

### Op 3: Benchmark Request
- Request Payload:
  - Same as Op 1 request payload, followed by:
  - `u32 warmup`: Warmup iteration count.
  - `u32 iters`: Benchmark iteration count.
- Response Payload:
  - `u32 n`: Number of recorded iteration timings ($n = \text{iters}$).
  - `u64[n] dur_ns`: Individual iteration durations in nanoseconds.

### Tile Descriptor Record (32 Bytes Little-Endian)
Offset | Type | Field Name
--- | --- | ---
0 | `u16` | `morton`
2 | `u8` | `cls`
3 | `u8` | `flags` (`F_EDGE` 0x01, `F_TEXTURE` 0x02, `F_SAT` 0x08, `F_PAD` 0x80)
4 | `u16` | `y_q4`
6 | `i8` | `co_h`
7 | `i8` | `cg_h`
8 | `u16` | `sig_q4`
10 | `u16` | `grad_q6`
12 | `u8` | `edge`
13 | `u8` | `resid`
14 | `i8` | `mv_dx`
15 | `i8` | `mv_dy`
16 | `u64` | `sig64`
24 | `u64` | `xh64`

## 1080p Timing Workload (Generated in RAM from SplitMix64 Seed `0x5EED`)

- Frame Dimensions: $1920 \times 1080$ ($s = 32$, $gw = 60$, $gh = 34$, $N = 2040$ tiles).
- Input Generation: Generated in RAM using SplitMix64 seed `0x5EED` (never written to disk).

## Host Timing Command (VM)

To run tests and benchmark timing pinned to CPU cores 2-4:

```bash
taskset -c 2-4 cargo test --release --offline --manifest-path rust/vqa_k_s1_desc/Cargo.toml
```
