<!-- SPDX-License-Identifier: AGPL-3.0-only -->

# Studio motion and scene

Run from this directory with Python and locally installed NumPy, SciPy and Blender. The optional H.264 render uses local FFmpeg with `libx264`. All Blender commands use background mode, disable automatic script execution and use CPU rendering. Stage commands consume caller-supplied files.

The geometry contract uses metres and one-based Blender frames. The default SET runs at 24 fps for 288 frames [TARGET]. Motion GLBs use right-handed Y-up metres, with the first sample at time zero. Motion commands expect a caller-qualified single-skin template GLB with the ordered `bone_0` through `bone_21` hierarchy and weighted soles and palms.

## Layout, animation and scene gate

```sh
sha() { sha256sum "$1" | cut -d ' ' -f 1; }
python3 -B motion_scene.py layout --camera CAM_M --output build/layout
set_json=build/layout/SET.json
rig=inputs/template.glb

python3 -B motion_scene.py animate --mode keyed \
  --rig "$rig" --rig-sha256 "$(sha "$rig")" \
  --registration "$set_json" --registration-sha256 "$(sha "$set_json")" \
  --output build/motion

animation=build/motion/retargeted.glb
motion=build/motion/motion-report.json
python3 -B motion_scene.py compose --mode keyed --camera authored \
  --registration "$set_json" --registration-sha256 "$(sha "$set_json")" \
  --animation "$animation" --animation-sha256 "$(sha "$animation")" \
  --motion-report "$motion" --motion-report-sha256 "$(sha "$motion")" \
  --output build/scene

scene_report=build/scene/scene-report.json
scene=build/scene/scene.blend
python3 -B motion_scene.py gate --mode keyed \
  --registration "$set_json" --registration-sha256 "$(sha "$set_json")" \
  --motion-report "$motion" --motion-report-sha256 "$(sha "$motion")" \
  --scene-report "$scene_report" --scene-report-sha256 "$(sha "$scene_report")" \
  --animation "$animation" --animation-sha256 "$(sha "$animation")" \
  --scene "$scene" --scene-sha256 "$(sha "$scene")"
```

`layout` writes `set.blend` and `SET.json`. It offers the wide SET camera and the fixed `CAM_M`, `CAM_A1` and `CAM_A2` cameras. `animate` writes named GLB actions, a rest-projected BVH, the joint map and the stance and trajectory reports. `compose` reopens the saved scene, samples the evaluated root and palm against the GLB replay and measures PLAY travel, CRT timing and sightlines. Its authored camera follows the action. A fixed camera must independently pass the same visibility gate.

For captured motion, choose `--mode mocap` throughout. Supply licensed, caller-qualified CMU-hierarchy BVHs. The numeric frame ranges below are examples [TARGET]. Walk uses a zero-based start and exclusive stop with an available boundary sample. Jab uses zero-based start, peak and final samples. `--plant-offset` optionally sets the mocap registration offset in Z-up metres.

```sh
walk=inputs/walk.bvh
jab=inputs/jab.bvh
python3 -B motion_scene.py animate --mode mocap \
  --rig "$rig" --rig-sha256 "$(sha "$rig")" \
  --registration "$set_json" --registration-sha256 "$(sha "$set_json")" \
  --walk "$walk" --walk-sha256 "$(sha "$walk")" --walk-range 60 180 \
  --jab "$jab" --jab-sha256 "$(sha "$jab")" --jab-range 20 60 110 \
  --output build/mocap
```

## Clay blocking, curves and shadow samples

```sh
python3 -B motion_scene.py clay --mode blocking --output build/blocking
python3 -B motion_scene.py clay --mode final --shadow-frame 158 --output build/clay
python3 -B motion_scene.py clay --mode miss --output build/miss
```

`blocking` writes an editable stepped scene. `final` writes dense clamped curves with a traveling cap and a lit CRT. `miss` writes the corresponding dark control. Each receipt includes the evaluated pad-to-cap clearance. Add `--preview-frame` for a still or `--movie` for the curved take's full CPU-rendered H.264 master. `--shadow-frame` writes matched renders with the actor and stand selectively hidden, plus near-sole and stand-foot darkening measurements. The clay stage uses a dimensioned rehearsal actor and its own cap mechanism.

## Stage execution and resume

`./run_morning.sh --plan plan.json --output build/run` runs a versioned, caller-authored JSON plan. Each stage has a unique name, an ordered list of earlier dependencies, an argv array, named relative output paths and a list of required evidence names. Use absolute executable and script paths or executables available on `PATH`. Stage commands run in their own output directories. Configuration is trusted local input.

```json
{
  "schema": "viewpoint.motion-scene.plan/v1",
  "stages": [
    {
      "name": "layout",
      "parents": [],
      "argv": ["python3", "/path/to/motion_scene.py", "layout", "--output", "{output}/bundle"],
      "outputs": {"registration": "bundle/SET.json", "scene": "bundle/set.blend"},
      "evidence": []
    }
  ]
}
```

Use `{parent:stage:output}` for a pinned parent artifact and `{evidence:name}` for a verified copy of external evidence. To supply evidence after a hold, re-run with `--evidence STAGE=/path/to/evidence.json`. The manifest has schema `viewpoint.motion-scene.evidence/v1`, fields `stage`, `plan_sha256`, `parents` keyed by dependency name with receipt SHA-256s, and `files` keyed by evidence name with `path` and `sha256`. File paths are relative to that manifest. An optional stage `producer` argv can populate `{evidence_manifest}` when external evidence is absent. The producer also receives `{stage}`, `{plan_sha256}` and `{parents}` when those placeholders appear in its argv.

The runner locks the run directory, verifies stored parent receipts, evidence and output hashes, and resumes incomplete stages. Pass the same plan bytes on retry. To apply a local quiet-time window, set plan field `quiet_hours` to `["HH:MM", "HH:MM", "Region/City"]`.

Heavy commands enter a systemd user scope with 6 GiB memory high, 8 GiB memory max, zero swap and CPU affinity 0–15 [TARGET]. Admission accounts for active 8 GiB scopes before reserving memory. The program preserves the caller's niceness. A successful bundle carries a SHA-256 receipt. A policy refusal returns rc 1 with `reason`, `retry_when` and `next_command`. CLI and harness errors return rc 2 or higher.

The offline fixtures use `unittest` and the same Linux scope. Set `MOTION_SCENE_SCOPED_NICE` to the inherited niceness inside that scope and run from this directory.

```sh
PYTHONPATH=. python3 -B tests/test_motion_scene.py positive
PYTHONPATH=. python3 -B tests/test_motion_scene.py negative
```
