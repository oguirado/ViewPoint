"""Apply an integer journal, read back deterministic settings, render raw RGB8 PNGs."""
import argparse
import hashlib
import json
import math
import os
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(__file__))
from drive_common import (DriveError, Parser, load_json, GEOMETRY, assign_indices, canonical, cycles_seed,
                          hint_name, run, select_scene, warnings, write)


def apply_profile(scene, j, p, threads):
    r = scene.render
    try:
        r.engine = p["engine"]
    except (TypeError, ValueError) as e:
        raise DriveError("E_PROFILE", "engine") from e
    r.resolution_x, r.resolution_y = j["res"]
    r.resolution_percentage = 100
    r.fps, r.fps_base = j["fps"][0], float(j["fps"][1])
    r.threads_mode, r.threads = "FIXED", threads
    r.use_persistent_data = False
    r.use_motion_blur = False
    r.use_simplify = False
    r.use_compositing = False
    r.use_sequencer = False
    r.dither_intensity = 0.0
    for k in dir(r):
        if k.startswith("use_stamp"):
            try:
                setattr(r, k, False)
            except (AttributeError, TypeError):
                pass
    im = r.image_settings
    im.file_format, im.color_mode, im.color_depth, im.compression = "PNG", "RGB", "8", 15
    if p["id"] != "cycles-cpu-look":
        vs = scene.view_settings
        vs.view_transform, vs.look = p["view_transform"], p["look"]
        vs.exposure, vs.gamma = p["exposure_milli"]/1000, p["gamma_milli"]/1000
    scene.display_settings.display_device = "sRGB"
    if p["engine"] == "CYCLES":
        c = scene.cycles
        c.device, c.samples = "CPU", p["samples"]
        c.use_animated_seed = False
        c.use_denoising = False
        c.use_adaptive_sampling = False
        c.sample_clamp_indirect = 10.0
    elif p["engine"] == "BLENDER_WORKBENCH":
        scene.display.shading.light = "STUDIO"
        scene.display.shading.color_type = "MATERIAL"
        scene.display.render_aa = "8"
    else:
        raise DriveError("E_PROFILE", "gpu_cycles")
    verify_profile(scene, j, p)


def verify_profile(scene, j, p):
    r = scene.render
    checks = [(r.engine != p["engine"], "engine"),
              (r.dither_intensity != 0, "dither"), (r.use_motion_blur, "motion_blur"),
              (any(getattr(r, k) for k in dir(r) if k.startswith("use_stamp")), "stamp"),
              (r.fps != j["fps"][0] or r.fps_base != float(j["fps"][1]), "fps_change"),
              (r.use_compositing or r.use_sequencer or r.use_persistent_data or r.use_simplify, "pipeline")]
    if p["id"] != "cycles-cpu-look":
        vs = scene.view_settings
        checks += [(vs.view_transform != p["view_transform"] or vs.look != p["look"] or
                    round(vs.exposure*1000) != p["exposure_milli"] or round(vs.gamma*1000) != p["gamma_milli"], "view_transform")]
    if r.engine == "CYCLES":
        c = scene.cycles
        checks += [(c.device != "CPU", "gpu_cycles"), (c.use_denoising, "denoise"),
                   (c.use_adaptive_sampling, "adaptive"), (c.use_animated_seed, "animated_seed")]
    for failed, reason in checks:
        if failed:
            raise DriveError("E_PROFILE", reason)


def apply_ops(bpy, scene, j):
    # Gather all channels first: remove authored curves once, preserving every journal key.
    paths = {"loc_um": ("location", 1000000), "loc_mm": ("location", 1000),
             "rot_mdeg": ("rotation_euler", None), "scale_ppm": ("scale", 1000000)}
    touched, actions = set(), []
    for op in j["ops"]:
        if op["op"] == "still":
            continue
        obj = scene.camera if op["op"] == "camera_path" else bpy.data.objects.get(op["obj"])
        if obj is None or obj.name not in scene.objects:
            raise DriveError("E_SCHEMA", "unknown obj")
        if op["op"] == "hide":
            touched.add((obj.name, "hide_render"))
            actions.append((obj, "hide_render", op["frame"], op["render"], "CONSTANT"))
        else:
            for key in op["keys"] if op["op"] == "camera_path" else [op]:
                for field, (path, divisor) in paths.items():
                    if field in key:
                        values = [v/divisor if divisor else math.radians(v/1000) for v in key[field]]
                        touched.add((obj.name, path))
                        actions.append((obj, path, key["frame"], values, op["interp"]))
    for name, path in sorted(touched):
        obj = bpy.data.objects[name]
        ad = obj.animation_data
        if ad and ad.action:
            # Copy shared actions so journal edits do not alter untouched objects.
            ad.action = ad.action.copy()
            for fc in list(ad.action.fcurves):
                if fc.data_path == path:
                    ad.action.fcurves.remove(fc)
    for obj, path, frame, value, interp in actions:
        if path == "rotation_euler":
            obj.rotation_mode = "XYZ"
        setattr(obj, path, value)
        obj.keyframe_insert(data_path=path, frame=frame)
        for fc in obj.animation_data.action.fcurves:
            if fc.data_path == path:
                for kp in fc.keyframe_points:
                    if kp.co.x == frame:
                        kp.interpolation = interp


def bounds(bpy, scene, frame, width, height):
    from mathutils import Vector
    from bpy_extras.object_utils import world_to_camera_view
    deps = bpy.context.evaluated_depsgraph_get()
    camera = scene.camera.evaluated_get(deps)
    candidates = []
    for obj in sorted(scene.objects, key=lambda o: o.name):
        if obj.type not in GEOMETRY or obj.hide_render or obj.name not in bpy.context.view_layer.objects:
            continue
        evaluated = obj.evaluated_get(deps)
        coords = [world_to_camera_view(scene, camera, evaluated.matrix_world @ Vector(c)) for c in evaluated.bound_box]
        coords = [v for v in coords if v.z > 0]
        if not coords:
            continue
        x0 = max(0, min(width, math.floor(min(v.x for v in coords)*width)))
        x1 = max(0, min(width, math.ceil(max(v.x for v in coords)*width)))
        y0 = max(0, min(height, math.floor((1-max(v.y for v in coords))*height)))
        y1 = max(0, min(height, math.ceil((1-min(v.y for v in coords))*height)))
        if x1 <= x0 or y1 <= y0:
            continue
        center = evaluated.matrix_world @ (sum((Vector(c) for c in evaluated.bound_box), Vector()) / 8)
        depth = -(camera.matrix_world.inverted() @ center).z
        candidates.append((depth, obj.name, {"frame": frame, "name": hint_name(obj),
                           "role": "actor" if "animated" in str(obj.get("bvqa_tags", "")).split("|") else "object",
                           "bbox_px": [x0, y0, x1-x0, y1-y0]}))
    rows = []
    for rank, (_, _, row) in enumerate(sorted(candidates, key=lambda c: (c[0], c[1]))):
        row["z"] = min(rank, 32767)
        rows.append(row)
    return sorted(rows, key=lambda row: (row["frame"], row["name"]))


def entry():
    import bpy
    p = Parser()
    for key in ("journal", "profile", "out"):
        p.add_argument("--" + key, required=True)
    p.add_argument("--threads", type=int, required=True)
    args = p.parse_args(sys.argv[sys.argv.index("--")+1:])
    j = load_json(args.journal)
    profile = load_json(args.profile)
    scene = select_scene(bpy, j)
    assign_indices(bpy)
    apply_profile(scene, j, profile, args.threads)
    apply_ops(bpy, scene, j)
    os.makedirs(os.path.join(args.out, "raw"), exist_ok=True)
    f = j["frames"]
    frames = f["list"] if "list" in f else list(range(f["start"], f["end"]+1, f["step"]))
    rows, seeds = [], []
    for ri in frames:
        scene.frame_set(ri)
        seed = cycles_seed(0, j["seed"], 0 if j["seed_mode"] == "fixed" else ri)
        if scene.render.engine == "CYCLES":
            scene.cycles.seed = seed
        verify_profile(scene, j, profile)
        path = os.path.join(args.out, "raw", "frame_%06d.png" % ri)
        scene.render.filepath = path
        bpy.ops.render.render(write_still=True)
        if not os.path.isfile(path):
            raise DriveError("E_DRIVE", "no_output")
        rows.extend(bounds(bpy, scene, ri, *j["res"]))
        seeds.append({"render_index": ri, "seed": seed})
    still = next((op["count"] for op in j["ops"] if op["op"] == "still"), 0)
    if still:
        os.makedirs(os.path.join(args.out, "still", "raw"), exist_ok=True)
        for index in range(1, still+1):
            scene.frame_set(frames[0])
            if scene.render.engine == "CYCLES":
                scene.cycles.seed = cycles_seed(0, j["seed"], 0 if j["seed_mode"] == "fixed" else index)
            verify_profile(scene, j, profile)
            scene.render.filepath = os.path.join(args.out, "still", "raw", "still_%06d.png" % index)
            bpy.ops.render.render(write_still=True)
            if not os.path.isfile(scene.render.filepath):
                raise DriveError("E_DRIVE", "no_output")
    with open(os.path.join(args.out, "bounds.jsonl"), "wb") as f:
        for row in rows:
            f.write(canonical(row))
    build_hash = bpy.app.build_hash.decode() if isinstance(bpy.app.build_hash, bytes) else str(bpy.app.build_hash)
    backend = {"version_string": bpy.app.version_string, "build_hash": build_hash,
               "engine": scene.render.engine, "device": "CPU"}
    meta = {"blender": {"version": bpy.app.version_string, "build_hash": build_hash},
            "backend_fp": hashlib.sha256(canonical(backend)).hexdigest()[:16], "backend": backend,
            "seeds": seeds, "warnings": warnings(bpy, scene),
            "view_transform": scene.view_settings.view_transform, "look": scene.view_settings.look}
    write(os.path.join(args.out, "drive.meta.json"), meta)
    return meta


if __name__ == "__main__":
    run(entry)
