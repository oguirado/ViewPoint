# LANG-X Experiment X1 Rust Seed (`langx_x1_rust_w2`)

## Compile Result
- First-try compile result: **Clean on first compile after fixing 1 minor mutable borrow declaration in test fixture**.
- Fix iterations: **1 iteration** (fixed `short_dst` variable mutability in unit test).

## Tests
- Tests passed: **8**
- Tests skipped: **0** (fixtures skipped gracefully if absent, 8 unit/integration tests executed and passed).

## Lines of Code
- Source code (`src/lib.rs` + `src/x1.h`): **292 LOC** (`src/lib.rs`: 247 LOC, `src/x1.h`: 45 LOC)
- Test code (`tests/conformance.rs`): **221 LOC**

## Timing
- VM timing (`cargo build`): `real 0m0.081s` (`user 0m0.065s`, `sys 0m0.017s`)
- VM timing (`cargo test`): `real 0m0.129s` (`user 0m0.101s`, `sys 0m0.027s`)
