# SPDX-License-Identifier: AGPL-3.0-only
"""Blender background replay of the fitted rig stress poses."""
from pathlib import Path
import sys

import bpy
from mathutils import Quaternion, Vector
import numpy as np


POSES = {
    "arms-up": {7: ((0, 1, 0), 111), 11: ((0, 1, 0), -111),
                8: ((1, 0, 0), -21), 12: ((1, 0, 0), -21)},
    "step": {18: ((1, 0, 0), -28), 19: ((1, 0, 0), 37), 20: ((1, 0, 0), -9),
             14: ((1, 0, 0), 13), 15: ((1, 0, 0), 18), 16: ((1, 0, 0), -31),
             7: ((1, 0, 0), -16), 11: ((1, 0, 0), 16)},
    "jab": {2: ((0, 0, 1), 17), 11: ((1, 0, 0), -69),
            12: ((0, 0, 1), -28), 13: ((0, 1, 0), 35),
            7: ((1, 0, 0), -21), 8: ((1, 0, 0), -40)},
}


def evaluated_geometry(obj):
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    result = evaluated.to_mesh()
    try:
        coords = np.empty(len(result.vertices) * 3, dtype=np.float32)
        result.vertices.foreach_get("co", coords)
        m = np.array(evaluated.matrix_world, dtype=np.float64)
        coords = coords.reshape(-1, 3) @ m[:3, :3].T + m[:3, 3]
        return coords[:, [0, 2, 1]] * [1, 1, -1]
    finally:
        evaluated.to_mesh_clear()


def poses(work):
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    bpy.ops.import_scene.gltf(filepath=str(work / "template-rig.glb"), merge_vertices=False)
    rig, = [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH" and
              any(m.type == "ARMATURE" and m.object == rig for m in o.modifiers)]
    if len(rig.pose.bones) != 22 or len(meshes) != 1:
        raise RuntimeError("No deforming 22-joint actor on Blender readback")
    mesh, = meshes
    rest = evaluated_geometry(mesh)
    geometries = {"rest": rest}
    for name, rotations in POSES.items():
        for bone in rig.pose.bones:
            number = int(bone.name.split("_")[-1])
            bone.rotation_mode = "QUATERNION"
            bone.rotation_quaternion = Quaternion()
            if number in rotations:
                axis, angle = rotations[number]
                local = (rig.matrix_world @ bone.bone.matrix_local).to_quaternion().inverted() @ Vector(axis)
                bone.rotation_quaternion = Quaternion(local, np.deg2rad(angle))
        bpy.context.view_layer.update()
        geometries[name] = evaluated_geometry(mesh)
    np.savez(work / "poses.npz", **geometries)


if __name__ == "__main__":
    mode, folder = sys.argv[sys.argv.index("--") + 1:]
    if not bpy.app.background or mode != "poses":
        raise RuntimeError("background pose evaluation required")
    poses(Path(folder))
