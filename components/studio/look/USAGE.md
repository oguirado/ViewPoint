<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Studio LOOK

Run from `components/studio/look` with a local Python 3.11+ interpreter, NumPy, OpenCV, Pillow 10.1+ and Blender with Cycles. Blender workers run in background with CPU Cycles, fixed seeds and one thread. Supply self-contained GLBs and packed Blender scenes. Keep outputs in a caller-owned directory.

## Build the LOOK bundle

```sh
python3 -B look.py build --profile panel-actor \
  --source inputs/textured.glb --geometry inputs/repaired.glb \
  --camera inputs/camera.json --output work/look
```

`--source` is the one-mesh, one-material PBR reference. `--geometry` is the geometry to remesh, in the same coordinate system. The camera document defines the perspective distance, lens sensor and three-quarter azimuth from a registered shot. The component uses the same camera and lighting for each paired source and candidate render.

```json
{
  "camera": {
    "location_m": [2, -3, 0.6],
    "target_m": [0, 0, 0],
    "sensor_width_mm": 36
  },
  "robot_framing": {"reference_height_m": 1}
}
```

The `panel-actor` profile expects the registered front at negative Y, a cyan torso panel, a raised emblem, and two antenna tips in the source-space crop used by the profile. It applies the tip refinement, torso smoothing, local exterior cages, and fixed image regions at the original registration. Its landmark and streak limits require a 432 px orthographic render [TARGET]. The `generic` profile applies BLOCKS closure, surface cleanup and a global cage. It checks topology, bake transfer, the 14-view comparison roster [TARGET], view IoU and color.

The finished bundle contains `studio.glb`, `studio-metrics.json`, `contact-sheet.png`, the exported PNG atlases, per-view measurements, bake evidence, native readback and `bundle.json`. Bake outputs include base color and opacity, packed metallic and roughness, and tangent-space normals. Successful retries verify and reuse the completed bundle. A changed input, setting, or executable requires a fresh destination.

For a small fixture, use `--profile generic --atlas-size 256 --render-size 96 --samples 3` [TARGET]. Remesh depth, triangle cap, smooth iterations, cage extrusion, ray distance and bake margin can be set on the CLI.

## Request independent vision review

```sh
python3 -B look.py vision-request \
  --bundle work/look --bundle-sha256 "$bundle_sha256" --run-id shot-a --request work/studio-vision-request.json

python3 -B look.py vision-gate \
  --bundle work/look --bundle-sha256 "$bundle_sha256" --run-id shot-a \
  --request work/studio-vision-request.json --response work/studio-vision-response.json
```

Set `bundle_sha256` to the digest emitted by the successful build. Give the reviewer the request and the exact `contact-sheet.png` from the bundle. The request's contact-sheet filename is relative to the bundle root. Have the reviewer hash the exact request bytes, sample the requested coordinates in order, and return a JSON file. The illustrative digest strings and RGB triples below must be replaced with values for that review.

```json
{
  "schema": "viewpoint.studio-look.vision-response/v1",
  "run_id": "shot-a",
  "request_sha256": "<request SHA-256>",
  "contact_sheet_sha256": "<contact sheet SHA-256>",
  "decision": "ACCEPT",
  "pixel_access": true,
  "pixel_rgb": [[18, 24, 32], [38, 46, 55], [18, 24, 32]],
  "observations": {
    "front": "The antenna tips and emblem are visible.",
    "flank": "The outer contour and finish follow the reference.",
    "back": "An enlarged highlight breaks briefly near the shoulder."
  },
  "findings": [
    {"view": "back", "severity": "MINOR", "detail": "Short highlight break in enlarged grazing view."}
  ]
}
```

A MAJOR finding requires `REJECT` and withholds the selected artifact. An `ACCEPT` returns `MEASURED_AND_VISION` and preserves MINOR findings in `polish_backlog`. When the reviewer explicitly returns `AUSENTE`, include the same bindings, `pixel_access: false` and a nonempty `reason`. A bundle that passes the measured gate in this case returns `MEASURED_ONLY`.

## Gate a render scene

In the packed scene, align the static reference mesh with the skinned actor's rest pose. Configure the perspective camera, scene lights and framing before capture. Each arm chain lists shoulder, elbow and wrist bone names. The gate checks anatomy on all configured frames and projected elbow and wrist visibility on `visibility_frames`.

```json
{
  "armature": "Rig",
  "actors": ["Actor"],
  "reference": ["Reference"],
  "rest_frame": 1,
  "frames": [1, 24],
  "visibility_frames": [24],
  "shaded_frames": [24],
  "arms": {
    "left": ["shoulder.L", "elbow.L", "wrist.L"],
    "right": ["shoulder.R", "elbow.R", "wrist.R"]
  }
}
```

```sh
python3 -B look.py pose-capture --blend inputs/shot.blend \
  --config inputs/pose.json --output work/pose
python3 -B look.py pose-gate --config work/pose/witnesses.json
```

Use `"opaque_studio_material": true` in the capture config to apply the closed-surface Studio PBR material treatment to the in-memory scene. The gate compares same-camera rest silhouette and CIE76 color, checks the arm samples and measures high-frequency dark fragments inside each posed mask. The capture writes a replayable witness document and quality report. A completed pose capture retains its witness files when a quality gate rejects it.

Each command emits JSON on stdout. Exit `0` means the requested operation completed [TARGET]. Exit `1` means a gate observed a rejection [TARGET]. Exit `2` identifies input or harness errors [TARGET]. Refusals provide `reason`, `retry_when` and `next_command`. Blender logs go to stderr.

## Offline tests

```sh
PYTHONPATH=. python3 -B -m unittest discover -s tests -v
```

The tests generate meshes, renders and review responses locally in temporary directories. Set `STUDIO_TEST_BLENDER` to select an installed Blender executable.
