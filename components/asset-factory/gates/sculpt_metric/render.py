"""Blender-side CPU normal witnesses on a raw-bound orthographic view ring.

Called only on owned tmpfs snapshots. Never imports host packages. All PNGs and
the temporary render record stay in the admitted RAM workspace.
"""
import hashlib
import json
from pathlib import Path
import sys

import bpy
from mathutils import Vector

VIEWS = {"front": (0, -1, 0), "back": (0, 1, 0),
         "right": (1, 0, 0), "left": (-1, 0, 0),
         "top": (0, 0, 1), "bottom": (0, 0, -1)}


def import_mesh(path):
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    bpy.ops.import_scene.gltf(filepath=str(path))
    objects = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not objects:
        raise ValueError("empty_mesh")
    for obj in objects:
        obj.hide_render = False
        obj.data.materials.clear()
    return objects


def material():
    mat = bpy.data.materials.new("sculpt_normals")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    nodes.clear()
    geom = nodes.new("ShaderNodeNewGeometry")
    scale = nodes.new("ShaderNodeVectorMath")
    scale.operation = "SCALE"
    scale.inputs[3].default_value = 0.35
    offset = nodes.new("ShaderNodeVectorMath")
    offset.operation = "ADD"
    offset.inputs[1].default_value = (0.5, 0.5, 0.5)
    emission = nodes.new("ShaderNodeEmission")
    emission.inputs["Strength"].default_value = 1
    output = nodes.new("ShaderNodeOutputMaterial")
    links = mat.node_tree.links
    # Geometric face normals cannot be hidden by stale imported custom normals.
    links.new(geom.outputs["True Normal"], scale.inputs[0])
    links.new(scale.outputs["Vector"], offset.inputs[0])
    links.new(offset.outputs["Vector"], emission.inputs["Color"])
    links.new(emission.outputs[0], output.inputs["Surface"])
    return mat


def main():
    request_path = Path(sys.argv[sys.argv.index("--") + 1])
    req = json.loads(request_path.read_text())
    root = request_path.parent
    reference = import_mesh(root / "reference.glb")
    points = [o.matrix_world @ Vector(corner) for o in reference for corner in o.bound_box]
    lo = Vector(tuple(min(p[a] for p in points) for a in range(3)))
    hi = Vector(tuple(max(p[a] for p in points) for a in range(3)))
    center, extent = (lo + hi) * 0.5, max(hi - lo)
    if extent <= 0:
        raise ValueError("empty_scale")
    witness = material()
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 1
    scene.cycles.seed = 0
    scene.cycles.use_adaptive_sampling = False
    scene.cycles.use_denoising = False
    scene.render.threads_mode = "FIXED"
    scene.render.threads = 2
    scene.render.resolution_x = scene.render.resolution_y = req["resolution"]
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "8"
    scene.render.film_transparent = False
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs["Color"].default_value = (0, 0, 0, 1)
    scene.world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.view_settings.exposure = 0
    scene.view_settings.gamma = 1
    scene.render.dither_intensity = 0
    scene.render.use_file_extension = True
    scene.render.use_compositing = False
    scene.render.use_sequencer = False
    for role in ("reference", "candidate"):
        objects = reference if role == "reference" else import_mesh(root / "candidate.glb")
        for obj in objects:
            obj.data.materials.append(witness)
        for view, direction in VIEWS.items():
            camera_data = bpy.data.cameras.new("witness_camera")
            camera = bpy.data.objects.new("witness_camera", camera_data)
            scene.collection.objects.link(camera)
            camera.location = center + Vector(direction) * (extent * 4)
            camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
            camera_data.type = "ORTHO"
            camera_data.ortho_scale = extent * 1.25
            camera_data.clip_start = extent * 0.0001
            camera_data.clip_end = extent * 20
            scene.camera = camera
            scene.render.filepath = str(root / (role + "-" + view + ".png"))
            bpy.ops.render.render(write_still=True)
            bpy.data.objects.remove(camera, do_unlink=True)
    result = {"schema": "af.sculpt-render/v1", "blender_version": bpy.app.version_string,
              "device": "CPU", "engine": "CYCLES", "resolution": req["resolution"],
              "views": list(VIEWS), "witness": "world_normals_emission",
              "reference_sha256": hashlib.sha256((root / "reference.glb").read_bytes()).hexdigest(),
              "candidate_sha256": hashlib.sha256((root / "candidate.glb").read_bytes()).hexdigest()}
    target = root / "render.json"
    staged = target.with_suffix(".tmp")
    staged.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    staged.replace(target)


if __name__ == "__main__":
    main()
