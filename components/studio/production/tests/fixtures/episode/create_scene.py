# SPDX-License-Identifier: AGPL-3.0-only
"""Original primitive geometry for the Signal Cart conformance fixture."""
import sys
from pathlib import Path

import bpy
from mathutils import Vector

output = Path(sys.argv[sys.argv.index("--") + 1])
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene


def material(name, color):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*color, 1)
    return mat


def cube(name, dimensions, location, mat):
    bpy.ops.mesh.primitive_cube_add(size=1, location=location)
    obj = bpy.context.object
    obj.name = name
    obj.dimensions = dimensions
    obj.data.materials.append(mat)
    return obj


def camera(name, location, target, lens):
    data = bpy.data.cameras.new(name)
    obj = bpy.data.objects.new(name, data)
    scene.collection.objects.link(obj)
    obj.location = location
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()
    data.lens = lens


floor = material("Slate floor", (0.075, 0.14, 0.22))
cart = material("Orange enamel", (0.9, 0.20, 0.05))
trim = material("Slate trim", (0.05, 0.07, 0.11))
beacon = material("Signal green", (0.03, 0.9, 0.25))
cube("Floor", (10, 8, 0.15), (0, 0, -0.11), floor)
cube("Platform", (1.5, 1.0, 0.3), (1.25, 0.5, 0.15), trim)
cube("Beacon", (0.42, 0.42, 0.42), (1.25, 0.5, 0.58), beacon)
body = cube("Cart", (0.95, 0.48, 0.48), (-1.6, 0, 0.34), cart)
for side in (-1, 1):
    wheel = cube(f"Wheel {side}", (0.65, 0.14, 0.22), (0, side * 0.25, -0.20), trim)
    wheel.parent = body
cube("Guide", (4.0, 0.025, 0.007), (-0.5, -0.52, 0), beacon)
camera("Wide", (4, -7, 4), (0, 0, 0.30), 42)
camera("Signal", (3, -5, 2.5), (0.62, 0.16, 0.42), 52)
scene.world = bpy.data.worlds.new("Fixture world")
scene.world.color = (0.18, 0.18, 0.18)
# Save the scene and its dependencies without bundling file-browser UI state.
bpy.data.libraries.write(str(output), {scene}, path_remap="RELATIVE_ALL", fake_user=True)
