# Blender visual QA

Blender capture and a ground-truth calibration bench for the pinned vision engine.
The existing truth modules, Blender pass scripts and calibration report are retained.
This component is licensed under [AGPL-3.0](tools/UPSTREAM_LICENSE).

Start with [QUICKSTART.md](QUICKSTART.md); AI navigation starts at [llms.txt](llms.txt).
`blender-visual-qa` is the installed entry point; `python3 -m blender_visual_qa.cli` works from this checkout.
DRIVE writes scratch captures; TRUTH labels/scoring, scene oracles, stored-evidence judging and reporting have separate contracts.
Pixel oracles consume canonical PNG pixels; MP4 is judge evidence only.

## Readiness

B0 skeleton only; future stages refuse with E_INCOMPLETE until implemented.
config/vision.json pins the engine wheel committed at tools/wheels/vision_engine-1.1.0-py3-none-any.whl (sha256 and source commit recorded in the pin). python3 tools/build_engine_wheel.py rebuilds it from the recorded source commit (needs the operator's vision-engine checkout; not required to run the tests).
Judge/report are adapted from the committed baseline, preserving its evidence validation and verdict precedence.
Inherited judge/report transport schemas retain numeric confidence/metrics; Blender stage integer-only conversion is pending.
No live judge dispatch or calibration claim is certified by this skeleton.

## Provenance and verification

copy_manifest.tsv accounts for the baseline and adopted truth bench. tools/VENDORED.json binds committed engine tools.
`python3 tools/gates.py --all` emits deterministic stage JSON and fails closed on missing prerequisites.
`python3 tools/refresh_catalogue.py --check` checks Blender catalogue freshness.
The upstream tools/build_catalogue.py is vendored unchanged; its engine-specific formats are not the Blender registry compiler. The pinned router needs `--root catalogue/router` to consume the generated compatibility view; direct default invocation cannot consume the required six-column threshold registry.
Threshold provenance and calibration obligations live in catalogue/thresholds.tsv; design choices are uncalibrated.
