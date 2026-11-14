# LANG-X Experiment X1 Fluency Seed py_w2

Python 3 reference-style implementation of LANG-X experiment X1 kernels:
- Kernel K1: BGRX32 to RGB24 repack with stride
- Kernel K2: Damage rectangles to tile bitset

## First-Try Compile or Import Result
- **First-try compile or import result**: Clean import and first test run succeeded with 0 fix iterations required.
- **Fix iterations**: 0

## Test Results
- **Tests passed**: 9
- **Tests skipped**: 1 (`test_conformance_fixtures` skipped with message `fixtures absent at langx/x1/fixtures`)

## Lines of Code
- **Source (`x1.py`)**: 173 lines
- **Tests (`test_x1.py`)**: 239 lines

## VM Test Execution Command
- **VM command**: `python3 -m unittest discover -s langx/x1/py_w2 -p test_x1.py`
