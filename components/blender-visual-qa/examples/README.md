# CPU DRIVE fixture

`cube3.blend` is the procedural output of `bpy_side/scene_example.py`. It contains an animated cube with a deliberately awkward name, a hidden mesh, a hidden curve, a camera and a light. The committed-input fixture is needed because goldens include the exact blend digest; rebuilding a blend may change its serialization.

Run captures only in scratch:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m blender_visual_qa.cli drive \
  --blend examples/cube3.blend --journal examples/cube3.journal.json \
  --profile cycles-cpu-det --threads 1 --out build/room_SB1/example_run
PYTHONDONTWRITEBYTECODE=1 python3 -m blender_visual_qa.cli drive-check \
  --run build/room_SB1/example_run \
  --golden examples/golden/cube3/cycles-cpu-det.aarch64.json
```

The stored golden is a measured capture receipt for Blender 4.0.2 on aarch64, CPU Cycles, Standard/None, 64 samples, 160×120, render indices 1/5/9 [MEASURED]. Repeats with thread counts 1/8/1 agree on decoded RGB and canonical PNG bytes [MEASURED]. A different binary, platform, source, journal, profile or backend must refuse comparison as stale. MP4 is judge-only.

The example builder itself can be exercised with:

```sh
timeout 120 taskset -c 14-19 /usr/bin/blender -b --factory-startup \
  --disable-autoexec -noaudio --python-exit-code 9 -t 1 \
  --python bpy_side/scene_example.py -- --out build/room_SB1/rebuilt.blend
```

Do not overwrite the fixture or its golden as part of a test. A rebuilt fixture needs separate review and fresh measured goldens. Arbitrary source blends should pack external assets: relocating the scratch copy changes resolution of relative asset paths; missing images are reported as warnings.

Host gates use `python3 -m unittest tests.test_drive -v`. Opt-in CPU gates use `BVQA_DRIVE_BLENDER_TESTS=1 python3 -m unittest tests.test_drive_blender -v`. The integration gate reads the wheel and its digest from `config/vision.json` and extracts the verified wheel into scratch without touching the engine checkout. It requires jsonschema for the emitted schema checks. The independently enabled planted-negative suite is `BVQA_DRIVE_MUTANTS=1 python3 -m unittest tests.test_drive_mutants -v`.

The global CLI is supplied by SB0. Profiles and all capture schemas are under `blender_visual_qa/drive/`; the inspector is `bpy_side/drive_manifest.py`. These paths respect the room's SB1 surface, which excludes the master specification's shared helper and inspector paths. EEVEE and planted scene mutation remain unavailable in this CPU build; they fail before launch. Shared `bpy_side/canon.py` remains outside this lane: the seed formula has stdlib twins in `drive/journal.py` and `bpy_side/drive_common.py`, with golden-vector equality checks.
