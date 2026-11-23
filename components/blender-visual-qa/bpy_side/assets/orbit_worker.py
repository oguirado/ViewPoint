"""Turntable orbit worker. Run INSIDE Blender:
  blender -b --factory-startup -noaudio --python bpy_side/assets/orbit_worker.py -- \
      --glb G --out DIR --first 1 --last 72 --frames 72 --step-mdeg 5000 --res 512 --spp 64 \
      --pitch-mdeg 15000 --fov-mdeg 40000 [--threads N] [--scale-x-permille 1100] [--save-blend F]
Writes DIR/%06d.png (frame n has azimuth (n-1)*step) and prints one line `BVQA_ORBIT {json}` (integers only).
Camera: D = 1.2*R/tan(FOV/2) from the AABB centre of the visible meshes, +pitch above the horizon.
rc 0 ok; rc 2 + `BVQA_ORBIT_ERROR {json}` on bad input.
"""
import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import q  # noqa: E402
from canon import RAMWS_ROOT, ram_child, require_ram_out as _require_ram_out, verify_written  # noqa: E402

MDEG = 1000.0


def require_ram_out(path):
    """STORAGE LAW L2: frames/maps/scratch may only be written under the ramws root (tmpfs); anything else exits 5."""
    return _require_ram_out(path, os.environ.get("VQA_RAMWS_ROOT", RAMWS_ROOT))


def parse_args(argv):
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser(prog="orbit_worker.py")
    p.add_argument("--glb", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--first", type=int, required=True)
    p.add_argument("--last", type=int, required=True)
    p.add_argument("--frames", type=int, required=True)
    p.add_argument("--step-mdeg", type=int, required=True)
    p.add_argument("--res", type=int, required=True)
    p.add_argument("--spp", type=int, required=True)
    p.add_argument("--pitch-mdeg", type=int, required=True)
    p.add_argument("--fov-mdeg", type=int, required=True)
    p.add_argument("--fit-num", type=int, default=6)
    p.add_argument("--fit-den", type=int, default=5)
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--scale-x-permille", type=int, default=0)
    p.add_argument("--save-blend", default=None)
    p.add_argument("--keyframes", default="", help="comma list of frames keyed on the camera in --save-blend")
    return p.parse_args(argv)


def fail(code, msg):
    print("BVQA_ORBIT_ERROR " + json.dumps({"code": code, "error": msg}, sort_keys=True))
    sys.stdout.flush()
    raise SystemExit(2)


def aabb(bpy, mathutils):
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    count = 0
    for ob in bpy.context.scene.objects:
        if ob.type != "MESH" or ob.hide_render or ob.name.endswith(("-col", "-convcol", "_collision")):
            continue
        count += 1
        for c in ob.bound_box:
            w = ob.matrix_world @ mathutils.Vector(c)
            for k in range(3):
                lo[k] = min(lo[k], w[k])
                hi[k] = max(hi[k], w[k])
    return lo, hi, count


def main():
    import bpy
    import mathutils
    a = parse_args(sys.argv)
    a.out = require_ram_out(a.out)
    blend_targets = []
    if a.save_blend:
        a.save_blend = require_ram_out(a.save_blend)
        blend_targets.append(a.save_blend)
        if not a.save_blend.lower().endswith(".blend"):
            blend_targets.append(require_ram_out(a.save_blend + ".blend"))
    if not os.path.isfile(a.glb):
        fail("E_IO", "glb missing")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    try:
        bpy.ops.import_scene.gltf(filepath=a.glb)
    except Exception as exc:  # importer raises RuntimeError on garbage input
        fail("E_SCHEMA", str(exc)[:200])
    scene = bpy.context.scene
    bpy.context.view_layer.update()
    lo, hi, count = aabb(bpy, mathutils)
    if not count:
        fail("E_SCHEMA", "no visible mesh")
    centre = mathutils.Vector([(lo[k] + hi[k]) / 2.0 for k in range(3)])
    radius = (mathutils.Vector(hi) - mathutils.Vector(lo)).length / 2.0
    if a.scale_x_permille:
        roots = [o for o in scene.objects if o.parent is None and o.type in ("MESH", "EMPTY")]
        for o in roots:
            o.scale.x *= a.scale_x_permille / 1000.0
    fov = math.radians(a.fov_mdeg / MDEG)
    dist = a.fit_num * radius / (a.fit_den * math.tan(fov / 2.0))
    pitch = math.radians(a.pitch_mdeg / MDEG)

    cam_data = bpy.data.cameras.new("orbit_cam")
    cam_data.angle = fov
    cam_data.clip_start = max(dist - 2.0 * radius, 1e-3)
    cam_data.clip_end = dist + 4.0 * radius
    cam = bpy.data.objects.new("orbit_cam", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    cam.rotation_mode = "QUATERNION"
    sun_data = bpy.data.lights.new("orbit_sun", "SUN")
    sun_data.energy = 3.0
    sun = bpy.data.objects.new("orbit_sun", sun_data)
    scene.collection.objects.link(sun)
    sun.rotation_euler = (math.radians(50), 0.0, math.radians(30))
    world = bpy.data.worlds.new("orbit_world")
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (0.5, 0.5, 0.5, 1.0)
    bg.inputs[1].default_value = 1.0
    scene.world = world

    r = scene.render
    r.engine = "CYCLES"
    r.resolution_x = r.resolution_y = a.res
    r.resolution_percentage = 100
    r.film_transparent = False
    r.threads_mode = "FIXED"
    r.threads = max(1, a.threads)
    r.use_persistent_data = False
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGB"
    r.image_settings.color_depth = "8"
    r.image_settings.compression = 15
    scene.view_settings.view_transform = "Standard"
    cy = scene.cycles
    cy.device = "CPU"
    cy.samples = a.spp
    cy.seed = 0
    cy.use_animated_seed = False
    cy.use_denoising = False
    cy.use_adaptive_sampling = False
    scene.frame_start, scene.frame_end = 1, a.frames

    os.makedirs(a.out, exist_ok=True)
    frame_paths = [(n, ram_child(a.out, "%06d.png" % n)) for n in range(a.first, a.last + 1)]
    written = []

    def place(n):
        az = math.radians(((n - 1) * a.step_mdeg) / MDEG)
        # glTF azimuth 0 = +Z, toward +X; Blender (x, y, z) = (x, -z, y)
        d = mathutils.Vector((math.sin(az) * math.cos(pitch), -math.cos(az) * math.cos(pitch), math.sin(pitch)))
        cam.location = centre + d * dist
        cam.rotation_quaternion = (-d).to_track_quat("-Z", "Y")
        scene.frame_set(n)

    for n, path in frame_paths:
        place(n)
        r.filepath = path
        bpy.ops.render.render(write_still=True)
        written.append(n)
    if a.save_blend:
        for n in [int(v) for v in a.keyframes.split(",") if v]:
            place(n)
            cam.keyframe_insert("location", frame=n)
            cam.keyframe_insert("rotation_quaternion", frame=n)
        bpy.ops.wm.save_as_mainfile(filepath=a.save_blend)
    verify_written([a.out] + blend_targets)
    print("BVQA_ORBIT " + json.dumps({
        "frames": written, "blender": bpy.app.version_string,
        "centre_um": [q.um(v) for v in centre], "radius_um": q.um(radius), "distance_um": q.um(dist),
        "meshes": count}, sort_keys=True))


main()
