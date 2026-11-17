"""Blender subprocess only; modifies the loaded in-memory scene, never saves it.

Usage: blender -b SCENE --disable-autoexec --python-exit-code 2 --python THIS -- JOB
JOB is a list of explicit render tasks or tone-map tasks in owned scratch.
"""
import hashlib
import json
import math
from pathlib import Path
import sys
import time

import bpy
import numpy as np


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, obj):
    Path(path).write_text(json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n")


def dependencies():
    result = []
    # Linked libraries and unpacked image/volume/font inputs are independently bound.
    for group in (bpy.data.libraries, bpy.data.images, bpy.data.fonts, bpy.data.volumes,
                  bpy.data.movieclips, bpy.data.sounds, bpy.data.cache_files):
        for item in group:
            source = getattr(item, "source", "")
            if source in ("GENERATED", "VIEWER") or getattr(item, "packed_file", None):
                continue
            name = getattr(item, "filepath", "")
            if not name or name == "<builtin>":
                continue
            if source in ("SEQUENCE", "TILED", "MOVIE"):
                raise ValueError("unsupported_dynamic_dependency:" + name)
            path = Path(bpy.path.abspath(name, library=getattr(item, "library", None))).resolve()
            if not path.is_file():
                raise ValueError("missing_dependency:" + str(path))
            result.append({"role": "scene_dependency", "path": str(path), "sha256": digest(path)})
    return sorted({r["path"]: r for r in result}.values(), key=lambda r: r["path"])


def pfm_write(path, array):
    h, w, _ = array.shape
    if not np.isfinite(array).all():
        raise ValueError("nonfinite_pass")
    with open(path, "wb") as stream:
        stream.write(f"PF\n{w} {h}\n-1.0\n".encode("ascii"))
        stream.write(np.ascontiguousarray(array, dtype="<f4").tobytes())


def pfm_read(path):
    with open(path, "rb") as stream:
        if stream.readline() != b"PF\n":
            raise ValueError("pfm_header")
        w, h = map(int, stream.readline().split())
        if stream.readline().strip() != b"-1.0" or not (0 < w * h <= 40000000):
            raise ValueError("pfm_dimensions_or_scale")
        data = stream.read(w * h * 12 + 1)
    if len(data) != w * h * 12:
        raise ValueError("pfm_size")
    a = np.frombuffer(data, dtype="<f4").reshape(h, w, 3)
    if not np.isfinite(a).all():
        raise ValueError("nonfinite_pass")
    return a


def main():
    job_path = Path(sys.argv[sys.argv.index("--") + 1])
    job = json.loads(job_path.read_text())
    scene = bpy.context.scene
    if job["mode"] == "tone_map":
        scene.render.image_settings.file_format = "PNG"
        scene.render.image_settings.color_mode = "RGB"
        scene.render.image_settings.color_depth = "8"
        for task in job["tasks"]:
            start = time.monotonic_ns()
            a = pfm_read(task["input"])
            h, w, _ = a.shape
            im = bpy.data.images.new("af_denoised", w, h, alpha=True, float_buffer=True)
            rgba = np.concatenate((a, np.ones((h, w, 1), dtype=np.float32)), axis=2)
            im.pixels.foreach_set(rgba.ravel())
            im.save_render(task["output"], scene=scene)
            bpy.data.images.remove(im)
            write_json(task["meta"], {"tone_map_ms": (time.monotonic_ns() - start) // 1000000})
        return
    deps = dependencies()
    scene.render.engine = "CYCLES"
    device = job["device"]
    if device == "CPU":
        scene.cycles.device = "CPU"
    else:
        raise ValueError("unsupported_device")
    scene.render.threads_mode = "FIXED"
    scene.render.threads = 16
    scene.cycles.use_denoising = False
    scene.cycles.use_adaptive_sampling = False
    scene.cycles.use_animated_seed = True
    scene.cycles.seed = job["seed"]
    scene.render.resolution_percentage = job["resolution_percentage"]
    for layer in scene.view_layers:
        layer.cycles.use_denoising = False
        layer.cycles.denoising_store_passes = True
    scene.use_nodes = True
    nt = scene.node_tree
    nt.nodes.clear()
    rl = nt.nodes.new("CompositorNodeRLayers")
    comp = nt.nodes.new("CompositorNodeComposite")
    nt.links.new(rl.outputs["Image"], comp.inputs["Image"])
    output = nt.nodes.new("CompositorNodeOutputFile")
    output.format.file_format = "OPEN_EXR"
    output.format.color_mode = "RGB"
    output.format.color_depth = "32"
    output.format.exr_codec = "ZIP"
    output.file_slots.clear()
    for key, name in (("colour", "Image"), ("albedo", "Denoising Albedo"), ("normal", "Denoising Normal")):
        output.file_slots.new(key + "_")
        nt.links.new(rl.outputs[name], output.inputs[-1])
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "8"
    for task in job["tasks"]:
        directory = job_path.parent / task["dir"]
        directory.mkdir(parents=True)
        scene.frame_set(task["frame"])
        scene.cycles.samples = task["samples"]
        output.base_path = str(directory)
        scene.render.filepath = str(directory / "raw.png")
        start = time.monotonic_ns()
        bpy.ops.render.render(write_still=True)
        ms = (time.monotonic_ns() - start) // 1000000
        for key in ("colour", "albedo", "normal"):
            matches = list(directory.glob(key + "_*.exr"))
            if len(matches) != 1:
                raise ValueError("missing_or_duplicate_pass:" + key)
            im = bpy.data.images.load(str(matches[0]), check_existing=False)
            im.colorspace_settings.name = "Non-Color"
            w, h = im.size
            a = np.empty(w * h * 4, dtype=np.float32)
            im.pixels.foreach_get(a)
            pfm_write(directory / (key + ".pfm"), a.reshape(h, w, 4)[:, :, :3])
            bpy.data.images.remove(im)
        view = scene.view_settings
        write_json(directory / "render.json", {
            "render_ms": ms, "width": w, "height": h, "dependencies": deps,
            "blender_version": bpy.app.version_string,
            "settings": {"device": scene.cycles.device, "seed": scene.cycles.seed,
                         "resolution_percentage": scene.render.resolution_percentage,
                         "frame": scene.frame_current, "samples": scene.cycles.samples},
            "request_sha256": job["request_sha256"], "job_sha256": digest(job_path),
            "view": {"transform": view.view_transform, "look": view.look,
                     "exposure_milli": round(view.exposure * 1000),
                     "gamma_milli": round(view.gamma * 1000),
                     "display": scene.display_settings.display_device}})
        print("AF_FRAME_READY", task["frame"], task["samples"], ms, flush=True)


if __name__ == "__main__":
    main()
