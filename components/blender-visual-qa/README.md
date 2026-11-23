# Blender visual QA

Blender capture and a ground-truth calibration bench for the pinned vision engine.
The existing truth modules, Blender pass scripts and calibration report are retained.

Start with [QUICKSTART.md](QUICKSTART.md); AI navigation starts at [llms.txt](llms.txt).
`blender-visual-qa` is the installed entry point; `python3 -m blender_visual_qa.cli` works from this checkout.
DRIVE writes scratch captures; TRUTH labels/scoring, scene oracles, stored-evidence judging and reporting have separate contracts.
Pixel oracles consume canonical PNG pixels; MP4 is judge evidence only.

## Readiness

B0 skeleton only; future stages refuse with E_INCOMPLETE until implemented.
config/vision.json pins the engine wheel committed at tools/wheels/vision_engine-1.1.1-py3-none-any.whl (sha256 and source commit recorded in the pin). python3 tools/build_engine_wheel.py rebuilds it from the recorded source commit (needs the operator's vision-engine checkout; not required to run the tests).
Judge/report are adapted from the committed baseline, preserving its evidence validation and verdict precedence.
Inherited judge/report transport schemas retain numeric confidence/metrics; Blender stage integer-only conversion is pending.
No live judge dispatch or calibration claim is certified by this skeleton.

## Provenance and verification

copy_manifest.tsv accounts for the baseline and adopted truth bench. tools/VENDORED.json binds committed engine tools.
`python3 tools/gates.py --all` emits deterministic stage JSON and fails closed on missing prerequisites.
Set `BVQA_MIGRATION_SRC` to a local checkout containing the commit pinned by `tools/MIGRATION_SOURCE.json` before running the migration gate.
`python3 tools/refresh_catalogue.py --check` checks Blender catalogue freshness.
The upstream tools/build_catalogue.py is vendored unchanged; its engine-specific formats are not the Blender registry compiler. The pinned router needs `--root catalogue/router` to consume the generated compatibility view; direct default invocation cannot consume the required six-column threshold registry.
Threshold provenance and calibration obligations live in catalogue/thresholds.tsv; design choices are uncalibrated.

## Blender worker isolation

The Blender worker and its descendants have one writable filesystem mount, the approved RAM workspace. A tmpfs workspace may be swapped by the host. Headless CUDA separately admits pinned character devices for driver I/O. The CUDA supervisor may provide a narrowly scoped descriptor for an enrolled worker to name its own thread through `/proc/<pid>/task/<tid>/comm`.

Timeout enforcement comes from the owning process outside the worker's PID namespace. Stock headless runs without device bindings can emit diagnostics about `/dev/null` and the mount table. Worker admission and output validation determine the result.

Linux namespace and captive CUDA admission tests are opt-in. Run `tests.test_drive_containment` and `tests.test_drive_cuda_comm` with `BVQA_DRIVE_BLENDER_TESTS=1` and `BVQA_CAPTIVE_CUDA_GATE_TESTS=1` on the private RAM-root fixture for isolation acceptance. Use the extra writable RAM mount case to verify refusal before worker execution.
