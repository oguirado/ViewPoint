# SPDX-License-Identifier: AGPL-3.0-only
"""Decode the two native SET files and compare their animation and scene bindings."""
import hashlib
import json
import sys
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pose_oracle.common import Refusal, checkpoint, effective_scope, dump_json, load_json, sha256


def matrix(obj):
    return [list(row) for row in obj.matrix_world]


def record(path, witness_names):
    bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    arm, = [o for o in scene.objects if o.type == "ARMATURE"]
    camera = scene.camera
    lights = sorted((o for o in scene.objects if o.type == "LIGHT"), key=lambda obj: obj.name)
    witnesses = [scene.objects[name] for name in witness_names]
    clock = [scene.frame_start, scene.frame_end, scene.render.fps, scene.render.fps_base]
    digest = {key: hashlib.sha256() for key in ("motion", "camera", "lights", "set")}
    for frame in range(scene.frame_start, scene.frame_end + 1):
        if frame % 24 == 0:
            checkpoint()
        scene.frame_set(frame)
        camera_data = [camera.name, camera.data.type, camera.data.lens, camera.data.sensor_fit,
                       camera.data.sensor_width, camera.data.sensor_height, camera.data.shift_x, camera.data.shift_y]
        light_data = [[obj.name, obj.data.type, obj.data.energy, list(obj.data.color),
                       getattr(obj.data, "shape", None), getattr(obj.data, "size", None),
                       getattr(obj.data, "size_y", None)] for obj in lights]
        values = {"motion": [(b.name, [list(row) for row in arm.matrix_world @ b.matrix]) for b in arm.pose.bones],
                  "camera": [camera_data, matrix(camera)],
                  "lights": [light_data, [matrix(light) for light in lights]],
                  "set": [[obj.name, matrix(obj)] for obj in witnesses]}
        for key, value in values.items():
            digest[key].update(json.dumps([frame, value], separators=(",", ":")).encode())
    return {"sha256": sha256(path), "clock": clock, "camera": camera.name, "lights": len(lights),
            "motion_samples": scene.frame_end - scene.frame_start + 1,
            "meters_per_blender_unit": scene.unit_settings.scale_length,
            "bindings": {key: value.hexdigest() for key, value in digest.items()}}


def main():
    ok, _ = effective_scope()
    if not bpy.app.background or not ok:
        raise Refusal("unverified_scope")
    checkpoint()
    spec = load_json(sys.argv[sys.argv.index("--") + 1])
    paths = [Path(spec[label]["path"]) for label in ("incumbent", "candidate")]
    rows = []
    for label, path in zip(("incumbent", "candidate"), paths, strict=True):
        expected = spec[label]["sha256"]
        if sha256(path) != expected:
            raise Refusal(f"{label}_hash_mismatch")
        row = record(path, spec["witnesses"])
        if row["sha256"] != expected:
            raise Refusal(f"{label}_changed")
        rows.append(row)
    matched = (rows[0]["bindings"] == rows[1]["bindings"] and rows[0]["clock"] == rows[1]["clock"]
               and rows[0]["meters_per_blender_unit"] == rows[1]["meters_per_blender_unit"])
    result = {"schema": "rp4.pose-oracle.scene-binding.v1", "matched": matched,
              "witnesses": spec["witnesses"], "rc": int(not matched),
              "incumbent": rows[0], "candidate": rows[1]}
    dump_json(spec["out"], result)


if __name__ == "__main__":
    try:
        main()
    except Refusal as error:
        print(json.dumps(error.payload(["./oracle", "scene"])))
        raise SystemExit(2)
