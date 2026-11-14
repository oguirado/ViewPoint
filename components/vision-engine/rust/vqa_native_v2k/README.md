# `vqa_native_v2k`

In-process PyO3 (`=0.22.6`, `extension-module`) shared library wrapping the merged Vision v2 prototype Rust kernels (`vqa_k_st_proto8`, `vqa_k_st_block4`, `vqa_k_gm_proto`) for `V2EVAL2` post-pass variants.

## Offline Build

Build the release `cdylib` (`rust/vqa_native_v2k/target/release/libvqa_native_v2k.so`) using the offline Cargo cache:

```bash
cargo build --release --offline --manifest-path rust/vqa_native_v2k/Cargo.toml
```

## Loading in Python

Use `eval.native_v2k` to load the compiled shared object via `importlib.util.spec_from_file_location` under the module name `vqa_native_v2k`:

```python
from eval.native_v2k import load_native_v2k

nat = load_native_v2k()
```

- **Default path**: `rust/vqa_native_v2k/target/release/libvqa_native_v2k.so`
- **Override environment variable**: `VQA_NATIVE_V2K_SO=/path/to/libvqa_native_v2k.so`
- Raises `ImportError` naming the resolved `.so` path if the shared library file is absent.

## Exposed Functions

All entry points read `prev_luma` and `cur_luma` zero-copy via `pyo3::buffer::PyBuffer<u8>`, enforce C-contiguity (`is_c_contiguous()`), never copy frames per call, and release the Python GIL (`py.allow_threads`) during kernel execution:

- `st_proto8_subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d) -> Optional[ tuple[int, int, int] ]`
  Wraps `vqa_k_st_proto8::subtile_motion` (also accepts explicit `(w, h)` or unpacked `tx, ty`).
- `st_block4_subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d) -> Optional[ tuple[int, int, int] ]`
  Wraps `vqa_k_st_block4::subtile_motion_opt` (also accepts explicit `(w, h)` or unpacked `tx, ty`).
- `st_block4_batch(prev_luma, cur_luma, s, tiles, max_d) -> list[ Optional[ tuple[int, int, int] ] ]`
  Wraps `vqa_k_st_block4::subtile_motion_batch` (accepts `(s, tiles, max_d)` or `(s, max_d, tiles)`, with optional explicit `(w, h)`).
- `gm_estimate(prev_luma, cur_luma, max_d, k_top=16) -> Optional[ tuple[int, int, int] ]`
  Wraps `vqa_k_gm_proto::estimate_global_motion` (also accepts explicit `(h, w)`).
- `gm_consensus(prev_luma, cur_luma, s, dx, dy) -> list[bool]`
  Wraps `vqa_k_gm_proto::consensus_tiles` returning the row-major `gh * gw` boolean consensus mask (reshaped to `(gh, gw)` `bool` `ndarray` in `eval.v2x.gm_proto_nat.consensus_tiles`).

## V2EVAL2 Plug-in Modules & Benchmark

- `st=eval.v2x.st_proto8_nat`: `subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d)`
- `st=eval.v2x.st_block4_nat`: `subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d)`
- `gm=eval.v2x.gm_proto_nat`: `estimate_global_motion(prev_luma, cur_luma, max_d, k_top=16)` and `consensus_tiles(prev, cur, s, dx, dy)`
- Benchmark CLI: `python3 -m eval.native_v2k --bench`
