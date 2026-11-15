# LANG-X Experiment X1 Go Implementation (go_w3)

## Summary
Implementation of LANG-X experiment X1 fluency seed `go_w3` in Go 1.22 standard library.

## Build and Test Metrics
- **First-try compile or import result**: Clean compilation on first try (`go vet ./...` exit code 0).
- **Fix iterations**: 1 fix iteration during test file development (unused import / format string overflow adjustment in `x1_test.go`).
- **Tests passed**: 7 hand-computed unit tests passed (`TestRepack2x2WithPadding`, `TestRepackZeroDim`, `TestErrorCodes`, `TestWorkedCheck`, `TestZeroWidthRect`, `TestPartialBottomRow`, `TestSplitMixAndFNV1aPinned`).
- **Tests skipped**: 1 test skipped (`TestFixtures` skipped with message `fixtures absent at langx/x1/fixtures`).
- **Lines of code**:
  - Source (`x1.go`): 197 lines
  - Tests (`x1_test.go`): 343 lines
- **Timing / Execution Commands (VM)**:
  - Command: `cd langx/x1/go_w3 && go test -v ./...`
  - Output execution time: ~0.007s in VM.
