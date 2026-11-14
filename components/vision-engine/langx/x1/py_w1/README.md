# LANG-X Experiment X1 Fluency Seed (py_w1)

Python 3 + numpy reference implementation of two kernels for LANG-X Experiment X1.

## Results

- **First-try compile / import result**: Failed on first test run due to `len(dst)` on 2D numpy array evaluating to row count instead of total bytes (yielding `E_SHORT`); clean import / pass achieved on 2nd attempt (1 fix iteration).
- **Fix iterations**: 1
- **Tests passed**: 10
- **Tests skipped**: 1 (`TestX1Fixtures.test_fixtures`: fixtures absent at `langx/x1/fixtures`)

## Lines of Code

- **Source (`x1.py`)**: 168 lines
- **Tests (`test_x1.py`)**: 311 lines

## Timing

- **VM Execution Time**: 2.463s total (`real 0m2.463s`, `user 0m2.673s`, `sys 0m0.156s`)
- **Command**: `python3 -m unittest discover -s langx/x1/py_w1 -p test_x1.py`
