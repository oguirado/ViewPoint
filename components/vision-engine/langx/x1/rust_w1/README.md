# LANG-X Experiment X1 Fluency Seed (rust_w1)

## First-try Compile / Import Result
- **First-try result**: No (failed initial build with 2 minor compiler error types).
  1. `i32` type missing `is_power_of_two()` method -> converted `s` to `u32`.
  2. Simultaneous mutable and immutable borrow of `dummy` buffer in `conformance.rs` -> split into `src_dummy` and `dst_dummy`.
- **Fix iterations**: 2.

## Tests Passed and Skipped
- **Passed**: 2 tests (`test_always_on_unit_tests`, `test_conformance_fixtures`).
- **Skipped**: 0 tests (when fixtures directory `langx/x1/fixtures` is absent, `test_conformance_fixtures` prints `fixtures absent at langx/x1/fixtures` and completes cleanly).

## Lines of Code
- **Source LOC**: 347 lines (`src/lib.rs`: 295 lines, `src/x1.h`: 52 lines).
- **Tests LOC**: 198 lines (`tests/conformance.rs`: 198 lines).

## Timing
- **VM timing**: `cargo build && cargo test` executed inside VM in ~0.37s.
