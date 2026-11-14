# LANG-X Experiment X1 (Rust W3)

This crate implements LANG-X experiment X1 fluency seed `rust_w3` in Rust 2021 edition with no external dependencies.

## Build and Test Summary
- **First-try compile result**: Compiled cleanly on the first build/test run. 0 fix iterations required.
- **Tests passed and skipped**: 8 unit/conformance tests passed, 0 skipped.
- **Lines of code**:
  - Source: 277 lines (`src/lib.rs`: 233 lines, `src/x1.h`: 44 lines)
  - Tests: 278 lines (`tests/conformance.rs`: 278 lines)
- **VM timing**:
  - `time cargo build`: `real 0m0.080s`
  - `time cargo test`: `real 0m0.117s`

## Kernels
- **K1 (`repack_bgrx_to_rgb`)**: BGRX32 to RGB24 repack with stride and padding handling.
- **K2 (`damage_tiles`)**: Damage rectangles clipping and conversion to tile bitset words and popcount.
- **C ABI**: Functions `x1_repack` and `x1_damage` exported via C ABI (`src/x1.h`).
