# SPDX-License-Identifier: AGPL-3.0-only
"""Reopen the original and studio GLBs and render matched CPU comparisons."""
import argparse
import json
from pathlib import Path
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mesh import checkpoint, clear, import_mesh, overlaps, points, require_topology, stats


def oriented(obj, target):
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()


def setup():
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 12
    scene.cycles.use_adaptive_sampling = False
    scene.cycles.use_denoising = False
    scene.cycles.seed = 0
    scene.cycles.max_bounces = 3
    scene.cycles.transparent_max_bounces = 4
    scene.render.use_persistent_data = True
    scene.render.film_transparent = True
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.image_settings.color_depth = "8"
    scene.render.resolution_percentage = 100
    scene.view_settings.view_transform = "Standard"
    camera = bpy.data.objects.new("Comparison camera", bpy.data.cameras.new("Comparison camera"))
    scene.collection.objects.link(camera)
    scene.camera = camera
    scene.world = bpy.data.worlds.new("Comparison world")
    scene.world.use_nodes = True
    lamps = []
    for name in ("key", "fill", "rim"):
        lamp = bpy.data.objects.new(name, bpy.data.lights.new(name, "AREA"))
        scene.collection.objects.link(lamp)
        lamps.append(lamp)
    return scene, camera, lamps


def lights(world, lamps, center, look, mode):
    tree = world.node_tree
    bg = next(node for node in tree.nodes if node.type == "BACKGROUND")
    for link in list(bg.inputs["Color"].links):
        tree.links.remove(link)
    bg.inputs["Color"].default_value = (0.15, 0.15, 0.15, 1)
    bg.inputs["Strength"].default_value = 0.45
    if mode == "environment":
        sky = next((node for node in tree.nodes if node.type == "TEX_SKY"), None)
        if sky is None:
            sky = tree.nodes.new("ShaderNodeTexSky")
            sky.sky_type = "NISHITA"
            sky.sun_elevation = 0.73
            sky.sun_rotation = 1.12
            sky.sun_disc = False
        tree.links.new(sky.outputs["Color"], bg.inputs["Color"])
        bg.inputs["Strength"].default_value = 0.18
    up = Vector((0, 0, 1))
    forward = Vector((look.x, look.y, 0)).normalized()
    right = up.cross(forward)
    positions = {
        "neutral": ((1.1, -0.55, 0.9, 105, 1.05), (0.85, 0.86, 0.20, 48, 0.85),
                    (-0.9, 0.45, 0.75, 42, 0.60)),
        "grazing": ((0.07, -1.35, 0.72, 115, 0.25), (1.0, 0.44, 0.13, 18, 0.65),
                    (-0.82, 0.84, 0.65, 58, 0.35)),
    }
    for i, lamp in enumerate(lamps):
        lamp.hide_render = mode == "environment"
        if mode != "environment":
            f, r, z, watts, size = positions[mode][i]
            lamp.location = center + f * forward + r * right + z * up
            oriented(lamp, center)
            lamp.data.energy = watts
            lamp.data.shape = "DISK"
            lamp.data.size = size


def describe_cameras(reference, camera, settings):
    coordinates = points(reference)
    low, high = coordinates.min(axis=0), coordinates.max(axis=0)
    center = Vector((low + high) / 2)
    height = float(high[2] - low[2])
    set_camera = settings["camera"]
    set_offset = Vector(set_camera["location_m"]) - Vector(set_camera["target_m"])
    azimuth = Vector((set_offset.x, set_offset.y, 0)).normalized()
    horizontal = {"front": Vector((0, -1, 0)), "flank": Vector((1, 0, 0)),
                  "back": Vector((0, 1, 0)), "three-quarter": azimuth}
    radius = height * set_offset.length / settings["robot_framing"]["reference_height_m"]
    units = {"verified_m_per_source_unit": None, "reference_height_set_m": settings["robot_framing"]["reference_height_m"],
             "original_bounds_source_units": [low.tolist(), high.tolist()],
             "source_unit_height": height, "set_distance_m": set_offset.length,
             "shot_distance_source_units": radius}
    camera.data.sensor_width = set_camera["sensor_width_mm"]
    camera.data.sensor_fit = "HORIZONTAL"
    camera.data.lens = 38
    return center, height, horizontal, set_offset.normalized().z, units


def layout(camera, scene, center, height, look, elevation, distance, projection, size):
    if projection == "ortho":
        camera.data.type = "ORTHO"
        camera.data.ortho_scale = height * 1.12
        scene.render.resolution_x, scene.render.resolution_y = size, size
        camera.location = center + 3 * height * look
    else:
        camera.data.type = "PERSP"
        scene.render.resolution_x, scene.render.resolution_y = round(size * 16 / 9), size
        direction = look * (1 - elevation ** 2) ** 0.5 + Vector((0, 0, elevation))
        camera.location = center + distance * direction
    oriented(camera, center)
    return {"position_source_units": list(camera.location), "target_source_units": list(center),
            "resolution_px": [scene.render.resolution_x, scene.render.resolution_y],
            "orthographic_width_source_units": camera.data.ortho_scale if projection == "ortho" else None,
            "focal_length_mm": camera.data.lens if projection == "shot" else None,
            "sensor_width_mm": camera.data.sensor_width if projection == "shot" else None}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--asset", type=Path, required=True)
    parser.add_argument("--camera", type=Path, required=True)
    parser.add_argument("--size", type=int, default=432)
    parser.add_argument("--samples", type=int, default=12)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    clear()
    source = import_mesh(args.source)
    candidate = import_mesh(args.asset)
    topology = stats(candidate)
    require_topology(topology)
    topology["nonincident_face_intersection_pairs"] = len(overlaps(candidate, weld=True)[1])
    if topology["nonincident_face_intersection_pairs"]:
        raise RuntimeError(f"reopened GLB has triangle crossings: {topology}")
    (output / "topology.json").write_text(json.dumps(topology, indent=2) + "\n")
    subjects = {"original": source, "studio": candidate}

    scene, camera, lamps = setup()
    scene.cycles.samples = args.samples
    center, height, headings, elevation, units = describe_cameras(source, camera, json.loads(args.camera.read_text()))
    cameras = {}
    for name, subject in subjects.items():
        for obj in subjects.values():
            obj.hide_render = obj != subject
        for projection in ("ortho", "shot"):
            for view, heading in headings.items():
                cameras[f"{projection}_{view}"] = layout(camera, scene, center, height, heading, elevation,
                                                          units["shot_distance_source_units"], projection, args.size)
                modes = ["neutral"]
                if projection == "ortho" and view != "three-quarter":
                    modes += ["grazing", "environment"]
                for mode in modes:
                    lights(scene.world, lamps, center, heading, mode)
                    label = f"{name}_{projection}_{view}_{mode}"
                    checkpoint(label)
                    scene.render.filepath = str(output / f"{label}.png")
                    bpy.ops.render.render(write_still=True)
    (output / "cameras.json").write_text(json.dumps({"scale": units, "views": cameras}, indent=2) + "\n")


if __name__ == "__main__":
    main()
