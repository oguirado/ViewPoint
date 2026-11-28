# SPDX-License-Identifier: AGPL-3.0-only
"""Generate a closed, opaque synthetic source and a registered pose scene."""
from pathlib import Path
import sys

import bpy
from mathutils import Vector

root = Path(sys.argv[sys.argv.index("--") + 1])
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
bpy.ops.mesh.primitive_cube_add(size=1)
cube = bpy.context.object
material = bpy.data.materials.new("Fixture surface")
material.use_nodes = True
shader = material.node_tree.nodes.get("Principled BSDF")
shader.inputs["Base Color"].default_value = (.24, .42, .6, 1)
shader.inputs["Roughness"].default_value = .55
shader.inputs["Metallic"].default_value = .25
cube.data.materials.append(material)
bpy.ops.export_scene.gltf(filepath=str(root / "source.glb"), export_format="GLB",
                          use_selection=True, export_yup=True)
cube.name = "Actor"
ref = cube.copy()
ref.data = cube.data.copy()
ref.name = "Reference"
bpy.context.collection.objects.link(ref)
arm = bpy.data.armatures.new("Armature")
rig = bpy.data.objects.new("Rig", arm)
bpy.context.collection.objects.link(rig)
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode="EDIT")
for side, x in (("left", -.25), ("right", .25)):
    for role, z in (("shoulder", .23), ("elbow", 0), ("wrist", -.23)):
        bone = arm.edit_bones.new(f"{side}-{role}")
        bone.head, bone.tail = (x, 0, z), (x, 0, z + .1)
bpy.ops.object.mode_set(mode="OBJECT")

# Two independently skinned closed surfaces give the projection gate real arm weights.
bpy.ops.object.select_all(action="DESELECT")
bpy.context.view_layer.objects.active = cube
for obj in (cube, ref):
    bpy.ops.mesh.primitive_cube_add(size=1, location=(-.25, 0, 0))
    left = bpy.context.object
    left.scale = (.40, .10, .75)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    left.data.materials.append(material)
    bpy.ops.mesh.primitive_cube_add(size=1, location=(.25, 0, 0))
    right = bpy.context.object
    right.scale = (.40, .10, .75)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    right.data.materials.append(material)
    if obj == cube:
        for side, mesh in (("left", left), ("right", right)):
            group = mesh.vertex_groups.new(name=side + "-shoulder")
            group.add(list(range(len(mesh.data.vertices))), 1.0, "REPLACE")
    left.select_set(True)
    bpy.context.view_layer.objects.active = left
    bpy.ops.object.join()
    name = obj.name
    bpy.data.objects.remove(obj, do_unlink=True)
    left.name = name

bpy.ops.object.camera_add(location=(0, -3, 0))
camera = bpy.context.object
camera.rotation_euler = (Vector((0, 0, 0)) - camera.location).to_track_quat("-Z", "Y").to_euler()
bpy.context.scene.camera = camera
scene = bpy.context.scene
scene.render.resolution_x = scene.render.resolution_y = 160
scene.render.resolution_percentage = 100
scene.world.color = (.45, .45, .45)
scene.cycles.samples = 2
bpy.ops.wm.save_as_mainfile(filepath=str(root / "pose.blend"))
