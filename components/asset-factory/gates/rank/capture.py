"""Blender-only CPU capture on fresh scratch output, no source writes.

    blender -b -t 4 --factory-startup -P gates/rank/capture.py -- SOURCE OUTDIR
    This is diagnostic frontal orthographic framing, not camera calibration.
    OUTDIR must not exist. Geometry comes exclusively from the input GLB.
"""
import hashlib
import json
import math
from pathlib import Path
import sys


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"


def digest_file(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    import bpy
    from mathutils import Vector
    source, destination = map(Path, sys.argv[sys.argv.index("--") + 1:])
    if destination.exists() or source.suffix != ".glb" or not source.is_file():
        raise ValueError("fresh scratch output and GLB source required")
    source_sha = digest_file(source)
    destination.mkdir(parents=True, exist_ok=False)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(source.resolve()))
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    points = [obj.matrix_world @ Vector(corner) for obj in meshes for corner in obj.bound_box]
    if not points:
        raise ValueError("empty mesh")
    low = Vector(tuple(min(p[i] for p in points) for i in range(3)))
    high = Vector(tuple(max(p[i] for p in points) for i in range(3)))
    centre, diameter = (low + high) / 2, (high - low).length
    if not math.isfinite(diameter) or diameter <= 0:
        raise ValueError("zero scale")
    material = bpy.data.materials.new("rank_neutral_clay")
    material.use_nodes = True
    material.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (.5, .5, .5, 1)
    material.node_tree.nodes["Principled BSDF"].inputs["Roughness"].default_value = .8
    for obj in meshes:
        obj.data.materials.clear()
        obj.data.materials.append(material)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 8
    scene.cycles.seed = 0
    scene.cycles.use_denoising = False
    scene.render.threads_mode = "FIXED"
    scene.render.threads = 4
    scene.render.resolution_x = scene.render.resolution_y = 256
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = True
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.view_settings.view_transform = "Standard"
    world = bpy.data.worlds.new("rank_world")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (.5, .5, .5, 1)
    scene.world = world
    for name, offset, energy in (("key", (-2, -3, 3), 300), ("fill", (3, -1, 1), 150)):
        light = bpy.data.lights.new(name, "AREA")
        light.energy, light.size = energy, diameter * 3
        obj = bpy.data.objects.new(name, light)
        scene.collection.objects.link(obj)
        obj.location = centre + Vector(offset) * diameter
        obj.rotation_euler = (centre - obj.location).to_track_quat("-Z", "Y").to_euler()
    data = bpy.data.cameras.new("rank_front")
    camera = bpy.data.objects.new("rank_front", data)
    scene.collection.objects.link(camera)
    data.type, data.ortho_scale = "ORTHO", max(high.z - low.z, high.x - low.x, high.y - low.y) * 1.2
    data.clip_start, data.clip_end = diameter * .001, diameter * 100
    camera.location = centre + Vector((0, -3, 0)) * diameter
    camera.rotation_euler = (centre - camera.location).to_track_quat("-Z", "Y").to_euler()
    scene.camera = camera
    # Shared camera recipe, not a claim about the artist's reference camera.
    recipe = {"direction": [0, -3, 0], "projection": "orthographic",
              "framing": "aabb-largest-extent-times-1200000ppm", "size": [256, 256],
              "material": "neutral-clay", "samples": 8, "device": "CPU"}
    capture = {"id": "diagnostic-front-v1", "width": 256, "height": 256,
               "projection": "orthographic", "axis": "front", "framing": "explicit-no-registration/v1",
               "camera_sha256": hashlib.sha256(canonical(recipe).encode()).hexdigest()}
    scene.render.filepath = str(destination / "front.png")
    bpy.ops.render.render(write_still=True)
    if digest_file(source) != source_sha:
        raise ValueError("source drift")
    record = {"schema": "af.rank-capture-raw/v1", "rc": 0, "input_sha256": source_sha,
              "capture": capture, "camera_recipe": recipe,
              "render_sha256": digest_file(destination / "front.png"),
              "renderer_sha256": digest_file(Path(__file__)), "blender_version": bpy.app.version_string,
              "mask_policy": "alpha_ge_128; binary L; performed by host after capture",
              "asset_verdict": "CANNOT_SEE", "scope": "diagnostic-capture-only"}
    temporary = destination / ".capture.json.tmp"
    temporary.write_text(canonical(record))
    temporary.replace(destination / "capture.json")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except (OSError, ValueError, RuntimeError) as error:
        print(canonical({"rc": 2, "reason_codes": ["capture_failed"], "scope": "diagnostic-capture-only"}))
        code = 2
    raise SystemExit(code)
