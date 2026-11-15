# LANG-X Experiment X1 Fluency Seed py_w3

Python 3 and NumPy reference-style implementation of LANG-X X1 kernels (Kernel K1 `repack_bgrx_to_rgb` and Kernel K2 `damage_tiles`).

## First-Try Result & Fix Iterations
- First-try compile / import result: Clean execution on first test run without any fix needed.
- Number of fix iterations: 0

## Test Results
- Tests passed: 8
- Tests skipped: 1 (`fixtures absent at langx/x1/fixtures`)

## Lines of Code
- Source (`x1.py`): 233
- Tests (`test_x1.py`): 303

## VM Timing
- VM timing: 1.712s
- Command used: `python3 -m unittest discover -s langx/x1/py_w3 -p test_x1.py`
