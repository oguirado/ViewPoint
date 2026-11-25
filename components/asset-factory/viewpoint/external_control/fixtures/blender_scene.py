# SPDX-License-Identifier: AGPL-3.0-only
"""Real Blender engine export/reimport for the local command lane."""

from base64 import b64encode
import json
from pathlib import Path
import sys

import bpy


def main():
    action, name, artifact = sys.argv[sys.argv.index("--") + 1:]
    artifact = str(Path(artifact).resolve())
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if action == "export":
        mesh = bpy.data.meshes.new(name)
        mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 0, 1)], [], [(0, 1, 2)])
        mesh.update()
        bpy.context.collection.objects.link(bpy.data.objects.new(name, mesh))
        result = bpy.ops.export_scene.gltf(filepath=artifact, export_format="GLTF_SEPARATE")
        if result == {"FINISHED"}:
            path = Path(artifact)
            document = json.loads(path.read_bytes())
            buffer, = document["buffers"]
            sidecar = path.with_suffix(".bin")
            if buffer["uri"] != sidecar.name:
                raise ValueError("Unexpected local Blender buffer")
            buffer["uri"] = "data:application/octet-stream;base64," + b64encode(
                sidecar.read_bytes()).decode()
            path.write_text(json.dumps(document))
            sidecar.unlink()
    elif action == "reimport":
        result = bpy.ops.import_scene.gltf(filepath=artifact)
        meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
        for obj in meshes:
            obj.data.calc_loop_triangles()
        Path("blender-reimport.json").write_text(json.dumps({
            "nodes": sorted(obj.name for obj in meshes),
            "vertices": sum(len(obj.data.vertices) for obj in meshes),
            "triangles": sum(len(obj.data.loop_triangles) for obj in meshes),
        }))
    else:
        raise ValueError("Unknown fixture action")
    if result != {"FINISHED"}:
        raise RuntimeError("Blender fixture action failed")


main()
