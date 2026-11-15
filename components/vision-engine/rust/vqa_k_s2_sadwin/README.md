# `vqa_k_s2_sadwin` - Rust S2 Tile SAD Window Kernel

`vqa_k_s2_sadwin` is a standalone Rust crate (std-only) porting `_tile_sad_windows`, `_tile_sad`, and `_tile_sad_clip` from `visioncore/change.py` bit-exactly.

## Protocol Byte Layout (stdin / stdout, Little-Endian)

| Op | Request Payload (stdin) | Response Payload (stdout) |
|----|-------------------------|---------------------------|
| `0` | *(none)* (quit) | *(none, process terminates)* |
| `1` | `i64 h, w, dx, dy, s; u64 len_pixels, n_req; len_pixels x i64 y_cur; len_pixels x i64 y_prev; n_req x i64 tx; n_req x i64 ty` | `i32 status; u64 n_sad; n_sad x i64 sad; u64 n_cnt; n_cnt x i64 count` |
| `3` | As op 1 request, followed by `u32 warmup, iters` | `u32 n_iters; n_iters x u64 nanoseconds` |

### Status Codes (`status: i32`)
- `0`: OK
- `1`: Index Out Of Bounds (`tx`/`ty` outside `[-max_tx, max_tx)` or `[-max_ty, max_ty)`)
- `2`: Dimension Mismatch (`y_cur`/`y_prev` length != `h * w` or `tx` length != `ty` length)
- `3`: Invalid Tile Size (`s == 0`)
- `98`: Other / Unknown Op

---

## Benchmark Workload (1080p, SplitMix64 seed `0x5EED`)

The 1080p timing workload operates on a 1920x1080 frame with tile size $s = 16$ ($120 \times 67$ grid = 8040 tiles per frame).
Both `y_cur` and `y_prev` are int16 luma planes generated in RAM using SplitMix64 state initialized to `0x5EED`.

To run the timing benchmark pinned in the VM:

```bash
cargo build --release --offline --manifest-path rust/vqa_k_s2_sadwin/Cargo.toml
taskset -c 2-4 python3 -m unittest tests.test_k_s2_sadwin
```

*Note: Any timing figures recorded inside the sandbox environment are labeled **VM**, as host hardware determines final performance.*
