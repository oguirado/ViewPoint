"""Tiny deterministic render of one blend INSIDE Blender (FO3 e2e kill harness; pure bpy, no host imports).

blender -b --factory-startup -noaudio --python-exit-code 9 <x.blend> --python adapters/example_scene/render_run.py -- \
  --out <dir> --frames 1:4 --res 160x90 --spp 2 [--engine CYCLES|BLENDER_WORKBENCH]
Writes <dir>/render/frame_####.png, <dir>/render/scene.json, <dir>/meta.json (pass indices, materials, view settings,
pair bboxes of the 'Static' object family in pixels) and <dir>/pi.blend (scratch copy with pass indices, for passes.py).
Pass index: objects and materials sorted by name, starting at 1 (material k -> materials[k-1], BSPEC_BORACLES section 3).
"""
import argparse
import json
import os
import sys

import bpy
from bpy_extras.object_utils import world_to_camera_view


def args():
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--frames", default="1:4")
    p.add_argument("--res", default="160x90")
    p.add_argument("--spp", type=int, default=2)
    p.add_argument("--engine", default="CYCLES")
    return p.parse_args(sys.argv[sys.argv.index("--") + 1:])


def bbox_px(scene, ob, w, h):
    cam = scene.camera
    xs, ys = [], []
    for c in ob.bound_box:
        v = world_to_camera_view(scene, cam, ob.matrix_world @ __import__("mathutils").Vector(c))
        xs.append(v.x)
        ys.append(v.y)
    x0, x1 = max(0, int(min(xs) * w)), min(w, int(max(xs) * w) + 1)
    y0, y1 = max(0, int((1 - max(ys)) * h)), min(h, int((1 - min(ys)) * h) + 1)
    return [x0, y0, x1, y1]


def main():
    a = args()
    scene = bpy.context.scene
    w, h = (int(v) for v in a.res.split("x"))
    f0, f1 = (int(v) for v in a.frames.split(":"))
    r = scene.render
    r.engine = a.engine
    r.resolution_x, r.resolution_y, r.resolution_percentage = w, h, 100
    r.threads_mode, r.threads = "FIXED", 2
    r.use_motion_blur = False
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGB"
    r.image_settings.color_depth = "8"
    r.use_file_extension = True
    r.use_compositing = False
    r.use_sequencer = False
    r.dither_intensity = 0
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.view_settings.exposure = 0
    scene.view_settings.gamma = 1
    if a.engine == "CYCLES":
        cy = scene.cycles
        cy.device, cy.samples, cy.seed = "CPU", a.spp, 7
        cy.use_animated_seed = False
        cy.use_denoising = False
        cy.use_adaptive_sampling = False
    else:
        scene.display.render_aa = "OFF"
        scene.display.shading.light = "FLAT"
        scene.display.shading.color_type = "OBJECT"
    objs = sorted(bpy.data.objects, key=lambda o: o.name)
    for i, ob in enumerate(objs, 1):
        ob.pass_index = i
    mats = sorted(bpy.data.materials, key=lambda m: m.name)
    for i, m in enumerate(mats, 1):
        m.pass_index = i
    os.makedirs(os.path.join(a.out, "render"), exist_ok=True)
    for f in range(f0, f1 + 1):
        scene.frame_set(f)
        r.filepath = os.path.join(a.out, "render", "frame_%04d.png" % f)
        bpy.ops.render.render(write_still=True)
    meta = {"objects": {o.name: o.pass_index for o in objs if o.type == "MESH"}, "materials": [m.name for m in mats],
            "width": w, "height": h, "frames": [f0, f1], "engine": a.engine,
            "view_settings": {"view_transform": scene.view_settings.view_transform, "look": scene.view_settings.look,
                              "exposure": int(scene.view_settings.exposure), "gamma": int(scene.view_settings.gamma)},
            "bboxes": {o.name: bbox_px(scene, o, w, h) for o in objs if o.type == "MESH"}}
    with open(os.path.join(a.out, "meta.json"), "w") as fh:
        json.dump(meta, fh, sort_keys=True)
    with open(os.path.join(a.out, "render", "scene.json"), "w") as fh:
        json.dump({"app": "blender", "scene": "fo3", "build_seq": 0, "fps_num": 24, "fps_den": 1,
                   "adapter_version": "1+cycles-cpu-det"}, fh, sort_keys=True)
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(a.out, "pi.blend"), copy=True, compress=False)


main()
