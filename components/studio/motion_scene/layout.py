# SPDX-License-Identifier: AGPL-3.0-only
"""Stationary production cameras, clay mechanism and contact-shadow evidence."""
import math

import bpy
from mathutils import Matrix, Vector
import numpy as np

from evidence import demand
import layout_geometry as geometry

CAMERAS = {
    "CAM_M": ((.0408, .3296, .8137), 50., 12., 5.9968, 40.),
    "CAM_A1": ((.049, .3318, .7891), 52., 14., 6.0799, 40.),
    "CAM_A2": ((.054, .3114, .802), 44., 16., 5.9422, 40.),
}
CLAY = {
    "bg": (.20, .20, .21), "wall": (.34, .37, .41), "set": (.30, .31, .33),
    "screen": (.012, .016, .018), "cap": (.78, .55, .21), "glyph": (.02, .02, .02),
}


def add_cameras(scene, active):
    for name, spec in CAMERAS.items():
        demand(name not in scene.objects, "camera_already_exists")
        facts = geometry.camera_matrix(*spec)
        data = bpy.data.cameras.new(name)
        data.lens, data.sensor_width, data.sensor_fit = spec[-1], geometry.SENSOR_MM, "HORIZONTAL"
        data.clip_start, data.clip_end = .06, 60.
        camera = bpy.data.objects.new(name, data)
        scene.collection.objects.link(camera)
        camera.location = tuple(facts["pos"])
        camera.rotation_euler = Matrix(facts["rot"].tolist()).to_euler()
        camera["target_m"] = spec[0]
    if active is not None:
        scene.camera = scene.objects[active]
    bpy.context.view_layer.update()


def build_clay_set(camera):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    for prim in geometry.build_set() + geometry.cap_primitives():
        part = (prim.role if prim.role in ("cap", "glyph", "screen") else
                "wall" if "wall" in prim.name.lower() else geometry.PART_NAME[prim.part])
        material = bpy.data.materials.get("Clay " + part)
        if material is None:
            material = bpy.data.materials.new("Clay " + part)
            material.use_nodes = True
            shader = material.node_tree.nodes["Principled BSDF"]
            shader.inputs["Base Color"].default_value = (*CLAY[part], 1)
            shader.inputs["Roughness"].default_value = .75
            if part == "screen":
                shader.inputs["Emission Color"].default_value = (.25, .90, .62, 1)
        mesh = bpy.data.meshes.new(prim.name)
        mesh.from_pydata(prim.verts.tolist(), [], prim.faces.tolist())
        mesh.update()
        mesh.materials.append(material)
        obj = bpy.data.objects.new(prim.name, mesh)
        scene.collection.objects.link(obj)
        obj.matrix_world = Matrix(prim.matrix.tolist())
        if prim.smooth:
            for polygon in mesh.polygons:
                polygon.use_smooth = True
        if prim.bevel:
            bevel = obj.modifiers.new("Edge radius", "BEVEL")
            bevel.width, bevel.segments = prim.bevel, 3
    focus = Vector((.15, .05, .95))
    theta = CAMERAS["CAM_M"][1]
    for name, azimuth, elevation, distance, energy, size, color in (
            ("Key", theta - 30, 50, 4.2, 620, 1., (1., .87, .72)),
            ("Fill", theta + 36, 22, 4.6, 170, 2.6, (.74, .84, 1.)),
            ("Rim", theta + 148, 46, 4., 330, 1.3, (.64, .84, 1.))):
        azimuth, elevation = math.radians(azimuth), math.radians(elevation)
        offset = Vector((math.sin(azimuth) * math.cos(elevation),
                         -math.cos(azimuth) * math.cos(elevation), math.sin(elevation)))
        light = bpy.data.lights.new(name, "AREA")
        light.energy, light.size, light.color = energy, size, color
        obj = bpy.data.objects.new(name, light)
        scene.collection.objects.link(obj)
        obj.location = focus + distance * offset
        obj.rotation_euler = (-offset).to_track_quat("-Z", "Y").to_euler()
    world = bpy.data.worlds.new("Clay ambient")
    world.use_nodes = True
    background = world.node_tree.nodes["Background"]
    background.inputs["Color"].default_value = (.36, .42, .50, 1.)
    background.inputs["Strength"].default_value = .1
    scene.world = world
    scene.frame_start, scene.frame_end = 1, geometry.LAST_FRAME
    scene.render.fps, scene.render.fps_base = geometry.FPS, 1.
    scene.render.resolution_x, scene.render.resolution_y = 640, 360
    scene.render.resolution_percentage = 100
    scene.render.engine = "CYCLES"
    scene.cycles.device, scene.cycles.samples, scene.cycles.seed = "CPU", 16, 24
    scene.cycles.use_denoising = False
    scene.render.image_settings.file_format = "PNG"
    scene.view_settings.view_transform = "AgX"
    add_cameras(scene, camera)
    return scene


def contact_shadow(full, unobstructed, region):
    """Compare matched linear RGB samples on a caller-registered floor patch."""
    full, unobstructed, region = map(np.asarray, (full, unobstructed, region))
    demand(full.shape == unobstructed.shape and full.ndim == 3 and full.shape[-1] == 3
           and full.shape[:-1] == region.shape and region.dtype == np.bool_, "shadow_sample_shape")
    demand(region.any() and np.isfinite(full).all() and np.isfinite(unobstructed).all()
           and (full >= 0).all() and (unobstructed >= 0).all(), "invalid_shadow_evidence")
    luma = np.array((.2126, .7152, .0722))
    reference = float((unobstructed[region] @ luma).mean())
    demand(reference > 1e-6, "unlit_shadow_reference")
    return {"pixels": int(region.sum()), "darkening": 1. - float((full[region] @ luma).mean()) / reference}
