# LANG-X X2 Host Comparison Harness

`langx/x2/host/compare.py` compares LANG-X X2 seeds across generated or provided page fixtures. It verifies output determinism, calculates majority conformance (identifying dissenting seeds, byte diff offsets, and JSON diff paths), and aggregates median step and launch timing metrics per seed.

## Rerun Commands

### Run tests in VM (Browser-free unit tests)
```bash
python3 -m unittest discover -s langx/x2/host -p test_compare.py
```

### Run comparison harness on real seeds
```bash
export CHROME_BIN=/usr/bin/chromium-browser
python3 langx/x2/host/compare.py --seeds py_w1,py_w2,py_w3,ts_w1,ts_w2,ts_w3 --reps 5 --out results.json
```

### Options
- `--seeds`: Comma-separated list of seed directory names (default: `py_w1,py_w2,py_w3,ts_w1,ts_w2,ts_w3`).
- `--fixtures`: Path to custom fixtures directory (optional; if omitted, automatically calls `python3 langx/x2/fixtures/gen_fixtures.py --out <temp_dir>`).
- `--reps`: Number of repetitions per page per seed (default: `5`).
- `--out`: Output path for canonical JSON report (required).

## Behavior & Reporting

- **SKIP check**: If `CHROME_BIN` is not set in the environment, the script outputs a line starting with `SKIP` and exits immediately with code 0 without executing any processes.
- **Determinism**: True if all repetitions for a seed on a page yield returncode 0 and identical stdout SHA-256 hashes.
- **Conformance**: Determines the majority stdout SHA-256 hash among deterministic seeds. Dissenting seeds are reported along with their first differing byte offset and first differing JSON path relative to the majority output document.
- **Missing Seeds**: Reported as `ABSENT` rather than failures of other seeds.
- **Timing Medians**: Calculates the median of `steps_ms` (over all steps and reps) and median of `launch_ms` per seed. All VM timings are labeled VM.
