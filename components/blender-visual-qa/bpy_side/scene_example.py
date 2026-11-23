"""Build a small procedural DRIVE fixture exclusively at the requested scratch path."""
import argparse
import math
import os
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(__file__))
from drive_common import Parser, run


def entry():
    import bpy
    p = Parser()
    p.add_argument("--out", required=True)
    args = p.parse_args(sys.argv[sys.argv.index("--")+1:])
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.name = "Scene"
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, 0))
    cube = bpy.context.object
    cube.name = "Cube.001 Big"
    cube["bvqa_tags"] = "subject|animated"
    mat = bpy.data.materials.new("Cube Material")
    mat.diffuse_color = (0.12, 0.45, 0.8, 1)
    cube.data.materials.append(mat)
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, -10))
    bpy.context.object.name = "A Hidden"
    bpy.context.object.hide_render = True
    # Hidden non-MESH geometry exercises the full pass_index domain.
    curve = bpy.data.curves.new("Hidden Curve", "CURVE")
    curve.dimensions = "3D"
    spline = curve.splines.new("POLY")
    spline.points.add(1)
    spline.points[0].co = (0, 0, 0, 1)
    spline.points[1].co = (1, 0, 0, 1)
    obj = bpy.data.objects.new("B Hidden Curve", curve)
    scene.collection.objects.link(obj)
    obj.hide_render = True
    bpy.ops.object.camera_add(location=(3, -4, 3))
    cam = bpy.context.object
    cam.name = "Camera"
    direction = -cam.location
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    scene.camera = cam
    bpy.ops.object.light_add(type="AREA", location=(1, -2, 5))
    bpy.context.object.name = "Key"
    bpy.context.object.data.energy = 500
    scene.world = bpy.data.worlds.new("World")
    scene.world.color = (0.1, 0.1, 0.1)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(args.out))
    return {"blend": os.path.abspath(args.out)}


if __name__ == "__main__":
    run(entry)
