# LANG-X Experiment X1 Fluency Seed (rust_w4)

## First-Try Compile or Import Result
- **First-try result**: Clean compile and test run on first attempt.
- **Fix iterations**: 0 (only minor compiler warning cleanup on unused `mut`).

## Tests Passed and Skipped
- **Tests passed**: 8 always-on unit tests (100% pass rate).
- **Tests skipped**: 1 fixture test skipped gracefully with message `fixtures absent at langx/x1/fixtures` (as fixture directory was not present).

## Lines of Code
- **Source (`src/lib.rs`)**: 273 LOC
- **Tests (`tests/conformance.rs`)**: 250 LOC

## VM Execution Timing
- **Command**: `cd langx/x1/rust_w4 && cargo test`
- **Timing (VM)**: 0.37s (test runner execution after clean build)
