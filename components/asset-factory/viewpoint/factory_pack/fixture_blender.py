# SPDX-License-Identifier: AGPL-3.0-only
"""Build the local procedural/rig fixture and its editable action scene."""
import json
import math
from pathlib import Path
import sys

import bpy
from mathutils import Vector


def coordinates(path):
    return json.loads(path.read_text())


def aim(obj, target):
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()


def export(path, selected):
    bpy.ops.object.select_all(action="DESELECT")
    for obj in selected:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = selected[-1]
    bpy.ops.export_scene.gltf(filepath=str(path), export_format="GLB", use_selection=True)


def material(name, colour):
    medium = bpy.data.materials.new(name)
    medium.diffuse_color = (*colour, 1)
    medium.use_nodes = True
    medium.node_tree.nodes.get("Principled BSDF").inputs["Base Color"].default_value = (*colour, 1)
    return medium


def generated(work, job, remove_skin):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.preferences.filepaths.save_version = 0
    width = job["width_um"] / 1_000_000
    height = job["height_um"] / 1_000_000
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, height / 2))
    mesh = bpy.context.object
    mesh.name = "FixtureMesh"
    mesh.dimensions = (width, width * 0.66, height)
    bpy.ops.object.transform_apply(location=True, rotation=False, scale=True)
    mesh.data.materials.append(material("Pivot finish", (0.08, 0.42, 0.65)))
    export(work / "asset.glb", [mesh])

    armature = bpy.data.armatures.new("FixtureSkeleton")
    rig = bpy.data.objects.new("FixtureRig", armature)
    bpy.context.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    root = armature.edit_bones.new("Base")
    root.head, root.tail = (0, 0, 0), (0, 0, height / 2)
    tip = armature.edit_bones.new("Tip")
    tip.head, tip.tail, tip.parent = root.tail, (0, 0, height), root
    bpy.ops.object.mode_set(mode="OBJECT")
    if not remove_skin:
        for name in ("Base", "Tip"):
            group = mesh.vertex_groups.new(name=name)
            indices = [v.index for v in mesh.data.vertices
                       if (v.co.z < height / 2) == (name == "Base")]
            group.add(indices, 1.0, "REPLACE")
        mesh.modifiers.new("Skin", "ARMATURE").object = rig
        mesh.parent = rig
    tip_pose = rig.pose.bones["Tip"]
    tip_pose.rotation_mode = "XYZ"
    for frame, angle in zip(job["frames"], (0, job["angle_mdeg"] / 2000, job["angle_mdeg"] / 1000)):
        tip_pose.rotation_euler[0] = math.radians(angle)
        tip_pose.keyframe_insert("rotation_euler", frame=frame)
    rig.animation_data.action.name = "Pivot action"
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = job["frames"][0], job["frames"][-1]
    scene.render.fps = 24
    scene.frame_set(scene.frame_start)
    export(work / "rig-action.glb", [mesh, rig])
    bpy.ops.wm.save_as_mainfile(filepath=str(work / "rig-action.blend"))


def sort_fixture_sphere(marker):
    """Keep Blender's sphere geometry and UVs in a stable mesh element order."""
    original = marker.data
    vertices = [tuple(vertex.co) for vertex in original.vertices]
    edges = sorted(tuple(sorted(edge.vertices)) for edge in original.edges)
    uv = original.uv_layers.active
    faces = sorted(
        (tuple(face.vertices), tuple(tuple(uv.data[i].uv) for i in face.loop_indices))
        for face in original.polygons
    )
    sphere = bpy.data.meshes.new("OrderedFixtureSphere")
    sphere.from_pydata(vertices, edges, [indices for indices, _ in faces])
    ordered_uv = sphere.uv_layers.new(name=uv.name)
    for face, (_, coordinates) in zip(sphere.polygons, faces, strict=True):
        for loop, coordinate in zip(face.loop_indices, coordinates, strict=True):
            ordered_uv.data[loop].uv = coordinate
    marker.data = sphere
    name = original.name
    bpy.data.meshes.remove(original)
    sphere.name = name


def composed(work, job):
    bpy.ops.wm.open_mainfile(filepath=str(work / "rig-action.blend"))
    bpy.context.preferences.filepaths.save_version = 0
    scene = bpy.context.scene
    scene.name = "FactoryScene"
    bpy.ops.mesh.primitive_plane_add(size=200)
    bpy.context.object.name = "Ground"
    bpy.context.object.data.materials.append(material("Ground finish", (0.23, 0.23, 0.27)))
    bpy.ops.mesh.primitive_uv_sphere_add(radius=job["radius_um"] / 1_000_000, location=(0.8, 0, 0))
    marker = bpy.context.object
    marker.name = "ContactBody"
    sort_fixture_sphere(marker)
    marker.data.materials.append(material("Contact finish", (0.74, 0.15, 0.046)))
    for sample in coordinates(work / "simulation.json")["presentation"]:
        marker.location.z = sample["height_um"] / 1_000_000
        marker.keyframe_insert("location", frame=sample["frame"])

    bpy.ops.object.camera_add(location=(2.8, -5, 2.65))
    camera = bpy.context.object
    camera.name = "ReviewCamera"
    aim(camera, (0.35, 0, 0.56))
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = 3.0
    scene.camera = camera
    bpy.ops.object.light_add(type="AREA", location=(0, -2.5, 4))
    lamp = bpy.context.object
    aim(lamp, (0, 0, 0))
    lamp.data.energy = 460
    lamp.data.shape = "DISK"
    lamp.data.size = 4.0
    scene.world = bpy.data.worlds.new("World")
    scene.world.color = (0.14, 0.14, 0.14)
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 4
    scene.cycles.use_denoising = False
    scene.render.threads_mode = "FIXED"
    scene.render.threads = 4
    scene.render.resolution_x, scene.render.resolution_y = 160, 120
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.frame_set(scene.frame_start)
    bpy.ops.wm.save_as_mainfile(filepath=str(work / "composed-native.blend"))


def rendered(work, job):
    bpy.ops.wm.open_mainfile(filepath=str(work / "edited.blend"))
    scene = bpy.context.scene
    for frame in job["frames"]:
        scene.frame_set(frame)
        scene.render.filepath = str(work / f"frame-{frame:03}.png")
        bpy.ops.render.render(write_still=True)


if __name__ == "__main__":
    stage, folder, job_path = sys.argv[sys.argv.index("--") + 1:][:3]
    work, job = Path(folder), coordinates(Path(job_path))
    match stage:
        case "asset":
            generated(work, job, "--remove-skin" in sys.argv)
        case "scene":
            composed(work, job)
        case "render":
            rendered(work, job)
        case _:
            raise ValueError("unknown Blender stage")
