# SPDX-License-Identifier: AGPL-3.0-only
"""Build the small native fixtures for the background and bearing gates."""
import json
import math
import sys
from pathlib import Path

import bmesh
import bpy
from mathutils import Matrix

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pose"))
from pose_oracle.common import checkpoint, dump_json, effective_scope

PIVOT = (0.0, 0.0, 0.255)
BEARING = (0.0, 0.028, 0.255)
RADIUS = 0.018
RACE = 0.0185


def attach(obj, arm, bone, name):
    obj.name = name
    bpy.context.view_layer.update()
    world = obj.matrix_world.copy()
    obj.parent = arm
    obj.parent_type = "BONE"
    obj.parent_bone = bone
    obj.matrix_world = world
    return obj


def box(arm, bone, name, low, high):
    bpy.ops.mesh.primitive_cube_add(size=1, location=[(a + b) / 2 for a, b in zip(low, high)])
    obj = bpy.context.object
    obj.data.transform(Matrix.Diagonal([*(b - a for a, b in zip(low, high)), 1.0]))
    return attach(obj, arm, bone, name)


def socket(arm):
    sides, rows = 48, 24
    angles = [math.radians(55 + i * 120 / rows) for i in range(rows + 1)]
    profile = [(a, RACE) for a in angles] + [(a, RACE + 0.003) for a in reversed(angles)]
    vertices, faces = [], []
    for angle, radius in profile:
        for j in range(sides):
            azimuth = 2 * math.pi * j / sides
            vertices.append((radius * math.sin(angle) * math.cos(azimuth),
                             radius * math.sin(angle) * math.sin(azimuth), radius * math.cos(angle)))
    for i in range(len(profile)):
        next_ring = (i + 1) % len(profile)
        for j in range(sides):
            k = (j + 1) % sides
            faces.append((i * sides + j, i * sides + k, next_ring * sides + k, next_ring * sides + j))
    mesh = bpy.data.meshes.new("Closed spherical cup")
    mesh.from_pydata(vertices, [], faces)
    edit = bmesh.new()
    edit.from_mesh(mesh)
    bmesh.ops.recalc_face_normals(edit, faces=list(edit.faces))
    edit.to_mesh(mesh)
    edit.free()
    obj = bpy.data.objects.new("Socket", mesh)
    bpy.context.collection.objects.link(obj)
    obj.location = BEARING
    return attach(obj, arm, "hips", "Socket")


def build(path, slit_m=0.0, displacement_m=0.0, lock_shoulder=False, camera_shift=0.0):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    collection = bpy.data.collections.new("Fixture assembly")
    bpy.context.scene.collection.children.link(collection)
    bpy.context.view_layer.active_layer_collection = bpy.context.view_layer.layer_collection.children[collection.name]
    skeleton = bpy.data.armatures.new("Control")
    arm = bpy.data.objects.new("Control", skeleton)
    collection.objects.link(arm)
    bpy.context.view_layer.objects.active = arm
    arm.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    hips = skeleton.edit_bones.new("hips")
    hips.head, hips.tail = (0, 0, 0), (0, 0, 0.1)
    shoulder = skeleton.edit_bones.new("left_shoulder")
    shoulder.head, shoulder.tail, shoulder.parent = PIVOT, (0, 0, 0.30), hips
    wrist = skeleton.edit_bones.new("left_wrist")
    wrist.head, wrist.tail, wrist.parent = (0, 0, 0.30), (0, 0, 0.35), shoulder
    bpy.ops.object.mode_set(mode="OBJECT")
    if lock_shoulder:
        lock = arm.pose.bones["left_shoulder"].constraints.new("LIMIT_ROTATION")
        lock.owner_space = "LOCAL"
        lock.use_limit_x = lock.use_limit_y = lock.use_limit_z = True
    box(arm, "hips", "Floor plate", (-0.035, -0.03, 0), (0.035, 0.03, 0.016))
    box(arm, "hips", "Left slit wall", (-0.05, -0.007, 0.220), (-slit_m / 2, -0.003, 0.290))
    box(arm, "left_shoulder", "Right slit wall", (slit_m / 2, -0.007, 0.220), (0.05, -0.003, 0.290))
    cup = socket(arm)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=RADIUS, location=BEARING)
    ball = attach(bpy.context.object, arm, "left_shoulder", "Ball")
    # Keep the object origin, joint head, and authored axes fixed in all variants.
    ball.data.transform(Matrix.Translation((displacement_m, 0.0, 0.0)))
    scene = bpy.context.scene
    scene.frame_end = 2
    bpy.ops.object.camera_add(location=(camera_shift, -1, .5))
    scene.camera = bpy.context.object
    scene["studio_rig"] = json.dumps({
        "collection": collection.name, "soles": {},
        "joints": [{"name": "left_shoulder", "socket": cup.name, "ball": ball.name,
                    "mouth_axis_local": [0, 0, 1], "stem_axis_local": [0, 0, 1],
                    "swing_hard_stop_deg": 45.0}],
    })
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(path))


if __name__ == "__main__":
    ok, scope = effective_scope()
    if not bpy.app.background or not ok:
        raise RuntimeError(f"effective_scope_mismatch:{scope}")
    checkpoint()
    args = sys.argv[sys.argv.index("--") + 1:]
    target = Path(args[0])
    if args[1:] == ["--scene-only"]:
        build(target / "scene-original.blend")
        build(target / "scene-displaced.blend", camera_shift=.1)
        sys.exit(0)
    if args[1:] == ["--readback-only"]:
        build(target / "native_readback_locked.blend", lock_shoulder=True)
        sys.exit(0)
    build(target / "native_seated.blend")
    build(target / "native_slit.blend", slit_m=0.00040)
    build(target / "native_displaced.blend", displacement_m=0.002)
    build(target / "native_readback_locked.blend", lock_shoulder=True)
    dump_json(target / "cameras.native.json", {
        "schema": "rp4.pose-oracle.cameras.v1", "resolution": [481, 271], "lens_mm": 50.0,
        "sensor_width_mm": 36.0,
        "groups": [{"name": "body", "target": "body", "views": [
            {"az_deg": 0.0, "el_deg": 0.0, "fill": 0.96},
            {"az_deg": 11.0, "el_deg": 0.0, "fill": 0.96},
        ]}],
    })
