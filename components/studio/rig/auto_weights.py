# SPDX-License-Identifier: AGPL-3.0-only
"""Run Blender bone heat on a welded rest mesh."""
from pathlib import Path
import sys

import bpy
import numpy as np


def weights(work):
    with np.load(work / "fit.npz") as fit:
        xyz, faces, heads, tails, parents = (fit[k] for k in ("xyz", "faces", "heads", "tails", "parents"))
    bpy.ops.wm.read_factory_settings(use_empty=True)
    collection = bpy.context.collection
    mesh = bpy.data.meshes.new("Rest mesh")
    mesh.from_pydata(xyz.tolist(), [], faces.tolist())
    mesh.update()
    actor = bpy.data.objects.new("Rest mesh", mesh)
    collection.objects.link(actor)
    data = bpy.data.armatures.new("Fitted template")
    rig = bpy.data.objects.new("Fitted template", data)
    collection.objects.link(rig)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    bones = []
    for i, (head, tail, parent) in enumerate(zip(heads, tails, parents, strict=True)):
        bone = data.edit_bones.new(f"bone_{i}")
        bone.head, bone.tail = head.tolist(), tail.tolist()
        bones.append(bone)
    for bone, parent in zip(bones, parents, strict=True):
        if parent >= 0:
            bone.parent = bones[parent]
    bpy.ops.object.mode_set(mode="OBJECT")
    actor.select_set(True)
    if bpy.ops.object.parent_set(type="ARMATURE_AUTO") != {"FINISHED"}:
        raise RuntimeError("template_bone_heat_failed")
    dense = np.zeros((len(xyz), len(heads)), dtype=np.float64)
    groups = {g.index: int(g.name.split("_")[-1]) for g in actor.vertex_groups}
    for vertex in mesh.vertices:
        for group in vertex.groups:
            dense[vertex.index, groups[group.group]] = group.weight
    np.save(work / "heat.npy", dense, allow_pickle=False)


if __name__ == "__main__":
    if not bpy.app.background:
        raise RuntimeError("blender_background_required")
    weights(Path(sys.argv[sys.argv.index("--") + 1]))
