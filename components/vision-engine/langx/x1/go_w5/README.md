# LANG-X Experiment X1 Go Seed (go_w5)

## Overview
Implementation of LANG-X experiment X1 fluency seed `go_w5` in Go 1.22 standard library.
Package `x1` provides kernels:
- `RepackBGRXToRGB`: Repacks BGRX32 buffer to RGB24 buffer with row strides.
- `DamageTiles`: Maps 2D damage rectangles to grid tile bitset and popcount.

## Build and Test Status
- First-try compile / import result: No (failed initial compile due to unused `strconv` import and hex constant typo in unit test).
- Fix iterations: 2
- Tests passed: 6 (`TestSplitMixAndFNV1a`, `TestUnitRepack2x2`, `TestErrorSentinels`, `TestWorkedCheck`, `TestDamageZeroWidthRect`, `TestDamagePartialBottomRow`)
- Tests skipped: 1 (`TestFixtures` skipped with `fixtures absent at langx/x1/fixtures`)

## Lines of Code
- Source (`x1.go`): 118 lines
- Tests (`x1_test.go`): 363 lines

## Performance & Timing
- VM execution time: `go test ./...` completed in approx. 0.005s in VM.
