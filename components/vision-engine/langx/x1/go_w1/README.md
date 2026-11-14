# LANG-X Experiment X1 Fluency Seed: Go (go_w1)

## First-Try Compile / Import Result
- **Clean compile on first try**: No (1 fix iteration required due to an unused import `encoding/hex` in `x1_test.go` and pinning the FNV1a64 expected test vector).
- **Number of fix iterations**: 1

## Tests Passed and Skipped
- **Unit tests passed**: 7 / 7 subtests in `TestHandComputedUnitTests` passed.
- **Fixture tests skipped**: 1 (`TestFixtures` skipped with message `fixtures absent at langx/x1/fixtures`).

## Lines of Code
- **Source (`x1.go`)**: 168 lines
- **Tests (`x1_test.go`)**: 404 lines

## Timing (VM Execution)
- **Command used**: `cd langx/x1/go_w1 && go test -v ./...`
- **Execution time**: 0.005s
