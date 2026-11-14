# RT0D01 equivalence evidence

`verify_equivalence.py` compares every `VisionRun._blobs()` byte digest against
`ve-sealed-1` serial execution, then current workers 1, 2 and 5 [TARGET]. It also
records whole-pipeline wall timing, including pool startup and S5, using three
repeats [TARGET] of the six requested resolution/scenario combinations [TARGET].
This deliberately measures `analyze`, because `eval.rt_bench.run_pass` still
recomputes S0 in its independent stage-timing harness and is outside this lane.
Frames are generated before timing and remain in RAM. Timing rows include load1.

Run from the repository root:

```sh
timeout 1600 taskset -c 5-9 env OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 python3 eval/results/rt0/verify_equivalence.py
python3 eval/results/rt0/check_evidence.py
```

The proof covers the synth inputs used by X1/X2/X8 and the tests/golden fixtures,
all registered test seeds for X3/X4, X5 inputs, X10 clean/advisory inputs, the
requested rt_bench scenes, and each GUI DEV kind at the GUI runner defaults.
X6/X7 executable input modules are AUSENTE; X9 constructs metadata without
calling the image pipeline. Calibration and held-out GUI partitions are excluded.
Each row retains the sealed and current file SHA tables, covering tiles, GOPs,
regions, links, frame rows and vision.json. `EQUIV_RECEIPT.json` binds changed
sealed files to this proof and to their old and current SHA values.

Workers own contiguous index ranges. At each range start, the preceding input
pixels determine equality while every descriptor is calculated in full. Interior
frames reuse only raw S1 descriptor fields; classified and hinted tiles never
flow backwards into S1. This is equivalent because byte-equal pixels have equal
S1 descriptors, while all temporal classification and lazy hashing remain in the
ordered consumer. Whole-frame repeats also share immutable padded/luma planes.
The worker produces equality once, so the consumer performs no duplicate S0.
Anonymous shared mappings are retained by ndarray views and released with them;
workers are joined or killed before the call returns. Changing scenes retain a
padded RGB plane and an int16 luma plane per frame, increasing peak RAM compared
with serial processing. Repeated frames within a range alias those planes.

The default `describe_many` return remains a tile ndarray. The internal pipeline
uses `return_s0=True` to receive `(tiles, padded_views, luma_views, words_eq)`.
Non-array pipeline inputs use a fork-inherited list of frame arrays, avoiding a
second full RGB stack. The sequential `analyze` behavior is preserved.
