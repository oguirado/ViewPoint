# SPDX-License-Identifier: AGPL-3.0-only
"""Read the persisted branch without importing the authoring code."""
import json
from pathlib import Path
import sys

import bpy

scene_path, report_path, *frames = sys.argv[sys.argv.index("--") + 1:]
bpy.ops.wm.open_mainfile(filepath=scene_path)
scene = bpy.context.scene
mesh = scene.objects["FixtureMesh"]
rig = scene.objects["FixtureRig"]
marker = scene.objects["ContactBody"]
report = {"camera_x_um": round(scene.camera.location.x * 1_000_000), "scene": scene.name,
          "groups": len(mesh.vertex_groups), "action": rig.animation_data.action.name,
          "samples": []}
for frame in map(int, frames):
    scene.frame_set(frame)
    graph = bpy.context.evaluated_depsgraph_get()
    mesh_eval = mesh.evaluated_get(graph)
    report["samples"].append({
        "frame": frame,
        "mesh_um": [list(map(lambda v: round(v * 1_000_000), vertex.co)) for vertex in mesh_eval.data.vertices],
        "contact_body_z_um": round(marker.evaluated_get(graph).location.z * 1_000_000),
    })
Path(report_path).write_text(json.dumps(report, sort_keys=True) + "\n")
