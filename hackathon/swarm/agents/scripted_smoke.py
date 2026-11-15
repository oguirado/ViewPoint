"""Deterministic non-LLM scripted producer for ViewPoint Swarm (H1 + H2 verification).

Exercises the exact same intent_cli SDK and Blender 4.0.2 bpy payloads as live
agents, supporting 2-agent and 4-agent modes plus optional STALE_FRAME rebase demo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from swarm.agents import (
    DEFAULT_EVENTS_LOG,
    DEFAULT_FRAMES_DIR,
    DEFAULT_GUI_FOOTPRINT,
    DEFAULT_INTENTS_DIR,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_WAIT_TIMEOUT_S,
    REPO_ROOT,
)
from swarm.agents.intent_cli import (
    atomic_write_json,
    build_gui_intent,
    build_script_intent,
    observe_state,
    record_enqueue_offset,
    resolve_events_log,
    validate_intent_dict,
    wait_for_close_event,
)

# ---------------------------------------------------------------------------
# Blender 4.0.2 pure bmesh + bpy.data payloads (context-free for bpy.app.timers)
# ---------------------------------------------------------------------------

_BMESH_HELPERS = """
import bpy
import bmesh
from math import radians
from mathutils import Euler, Vector

def ensure_collection(name):
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(col)
    elif col.name not in bpy.context.scene.collection.children:
        bpy.context.scene.collection.children.link(col)
    return col

def replace_mesh_object(name, col, location=(0.0, 0.0, 0.0), rotation=(0.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0)):
    old = bpy.data.objects.get(name)
    if old is not None:
        old_mesh = old.data if old.type == 'MESH' else None
        bpy.data.objects.remove(old, do_unlink=True)
        if old_mesh is not None and old_mesh.users == 0:
            bpy.data.meshes.remove(old_mesh)
    mesh = bpy.data.meshes.new(f"{name}_Mesh")
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    obj.rotation_euler = Euler(rotation, 'XYZ')
    obj.scale = scale
    col.objects.link(obj)
    return obj, mesh

def set_smooth_and_bevel(obj, mesh, bevel_width=0.015, segments=2, subsurf=1):
    for poly in mesh.polygons:
        poly.use_smooth = True
    if bevel_width > 0:
        bev = obj.modifiers.new(name="Bevel", type='BEVEL')
        bev.width = bevel_width
        bev.segments = segments
        bev.limit_method = 'ANGLE'
    if subsurf > 0:
        sub = obj.modifiers.new(name="Subsurf", type='SUBSURF')
        sub.levels = subsurf
        sub.render_levels = subsurf

def make_box(name, col, size, location, rotation=(0.0, 0.0, 0.0), bevel=0.015, subsurf=1):
    obj, mesh = replace_mesh_object(name, col, location=location, rotation=rotation)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, vec=size, verts=bm.verts)
    bm.to_mesh(mesh)
    bm.free()
    set_smooth_and_bevel(obj, mesh, bevel_width=bevel, subsurf=subsurf)
    return obj

def make_cylinder(name, col, radius1, radius2, depth, segments, location, rotation=(0.0, 0.0, 0.0), bevel=0.008, subsurf=0):
    obj, mesh = replace_mesh_object(name, col, location=location, rotation=rotation)
    bm = bmesh.new()
    bmesh.ops.create_cone(
        bm,
        cap_ends=True,
        cap_tris=False,
        segments=segments,
        radius1=radius1,
        radius2=radius2,
        depth=depth,
    )
    bm.to_mesh(mesh)
    bm.free()
    set_smooth_and_bevel(obj, mesh, bevel_width=bevel, subsurf=subsurf)
    return obj

def make_sphere(name, col, radius, location, scale=(1.0, 1.0, 1.0), subdivisions=3):
    obj, mesh = replace_mesh_object(name, col, location=location, scale=scale)
    bm = bmesh.new()
    bmesh.ops.create_icosphere(bm, subdivisions=subdivisions, radius=radius)
    bm.to_mesh(mesh)
    bm.free()
    set_smooth_and_bevel(obj, mesh, bevel_width=0.0, subsurf=0)
    return obj

def bind_material(obj, mat_name):
    mat = bpy.data.materials.get(mat_name)
    if mat is None:
        mat = bpy.data.materials.new(name=mat_name)
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    return obj
"""


def modeler_turn_1_script() -> str:
    return _BMESH_HELPERS + """
col = ensure_collection("CHAR_mesh")
bind_material(make_box("CHAR_Torso", col, (0.58, 0.36, 0.62), (0.0, 0.0, 2.18), bevel=0.03, subsurf=1), "SentinelArmor")
bind_material(make_box("CHAR_Head", col, (0.30, 0.32, 0.30), (0.0, 0.0, 2.72), bevel=0.025, subsurf=1), "SentinelArmor")
bind_material(make_box("CHAR_Visor", col, (0.26, 0.08, 0.09), (0.0, -0.145, 2.73), bevel=0.01, subsurf=0), "VisorGlass")
bind_material(make_cylinder("CHAR_Core", col, 0.11, 0.09, 0.08, 24, (0.0, -0.17, 2.24), rotation=(radians(90.0), 0.0, 0.0), bevel=0.006), "CyanEmissive")
"""


def modeler_turn_2_script() -> str:
    return _BMESH_HELPERS + """
col = ensure_collection("CHAR_mesh")
bind_material(make_box("CHAR_Shoulder_L", col, (0.26, 0.30, 0.22), (-0.42, 0.0, 2.42), rotation=(0.0, radians(14.0), 0.0), bevel=0.02, subsurf=1), "GoldTrim")
bind_material(make_box("CHAR_Shoulder_R", col, (0.26, 0.30, 0.22), (0.42, 0.0, 2.42), rotation=(0.0, radians(-14.0), 0.0), bevel=0.02, subsurf=1), "GoldTrim")
bind_material(make_cylinder("CHAR_Arm_L", col, 0.08, 0.07, 0.34, 16, (-0.45, 0.0, 2.15), rotation=(0.0, radians(8.0), 0.0)), "SentinelArmor")
bind_material(make_cylinder("CHAR_Arm_R", col, 0.08, 0.07, 0.34, 16, (0.45, 0.0, 2.15), rotation=(0.0, radians(-8.0), 0.0)), "SentinelArmor")
bind_material(make_box("CHAR_Forearm_L", col, (0.14, 0.16, 0.34), (-0.48, -0.08, 1.84), rotation=(radians(-18.0), 0.0, 0.0), bevel=0.015, subsurf=1), "SentinelArmor")
bind_material(make_box("CHAR_Forearm_R", col, (0.14, 0.16, 0.34), (0.48, -0.08, 1.84), rotation=(radians(-18.0), 0.0, 0.0), bevel=0.015, subsurf=1), "SentinelArmor")
"""


def modeler_turn_3_script() -> str:
    return _BMESH_HELPERS + """
col = ensure_collection("CHAR_mesh")
bind_material(make_box("CHAR_Pelvis", col, (0.44, 0.30, 0.20), (0.0, 0.0, 1.78), bevel=0.02, subsurf=1), "SentinelArmor")
bind_material(make_cylinder("CHAR_Leg_L", col, 0.105, 0.085, 0.58, 18, (-0.17, 0.0, 1.42), bevel=0.01, subsurf=1), "SentinelArmor")
bind_material(make_cylinder("CHAR_Leg_R", col, 0.105, 0.085, 0.58, 18, (0.17, 0.0, 1.42), bevel=0.01, subsurf=1), "SentinelArmor")
bind_material(make_box("CHAR_Foot_L", col, (0.19, 0.32, 0.12), (-0.17, -0.04, 1.11), bevel=0.015, subsurf=1), "SentinelArmor")
bind_material(make_box("CHAR_Foot_R", col, (0.19, 0.32, 0.12), (0.17, -0.04, 1.11), bevel=0.015, subsurf=1), "SentinelArmor")
"""


def props_turn_1_script() -> str:
    return _BMESH_HELPERS + """
col = ensure_collection("PROPS_set")
bind_material(make_cylinder("PROP_Pedestal", col, 2.20, 2.05, 1.05, 6, (0.0, 0.0, 0.525), bevel=0.02, subsurf=0), "PedestalDarkSteel")
bind_material(make_cylinder("PROP_PadSteps", col, 2.45, 2.30, 0.25, 6, (0.0, 0.0, 0.125), bevel=0.015, subsurf=0), "PedestalDarkSteel")

obj_ring, mesh_ring = replace_mesh_object("PROP_PadRing", col, location=(0.0, 0.0, 1.056))
bm = bmesh.new()
bmesh.ops.create_circle(bm, cap_ends=False, radius=1.45, segments=48)
edges = list(bm.edges)
res = bmesh.ops.extrude_edge_only(bm, edges=edges)
ring_verts = [e for e in res["geom"] if isinstance(e, bmesh.types.BMVert)]
bmesh.ops.scale(bm, vec=(0.91, 0.91, 1.0), verts=ring_verts)
bmesh.ops.solidify(bm, geom=bm.faces[:] + bm.edges[:] + bm.verts[:], thickness=0.025)
bm.to_mesh(mesh_ring)
bm.free()
set_smooth_and_bevel(obj_ring, mesh_ring, bevel_width=0.0, subsurf=0)
bind_material(obj_ring, "CyanEmissive")
"""


def props_turn_2_script() -> str:
    return _BMESH_HELPERS + """
import math
col = ensure_collection("PROPS_set")
for idx in range(6):
    angle = math.radians(idx * 60.0 + 30.0)
    px = math.cos(angle) * 1.88
    py = math.sin(angle) * 1.88
    bind_material(
        make_cylinder(
            f"PROP_Pylon_{idx + 1:02d}",
            col,
            0.09,
            0.055,
            0.68,
            6,
            (px, py, 1.38),
            rotation=(0.0, 0.0, angle),
            bevel=0.008,
        ),
        "PedestalDarkSteel",
    )
bind_material(make_box("PROP_Conduit_01", col, (0.10, 2.4, 0.03), (0.0, 0.0, 1.06), bevel=0.005, subsurf=0), "PedestalDarkSteel")
"""


def props_turn_3_script() -> str:
    return _BMESH_HELPERS + """
col = ensure_collection("PROPS_set")
bind_material(make_sphere("PROP_Drone", col, 0.18, (-1.45, -0.65, 2.65), scale=(1.1, 1.0, 0.72), subdivisions=3), "GoldTrim")
bind_material(make_cylinder("PROP_DroneRing", col, 0.28, 0.28, 0.03, 32, (-1.45, -0.65, 2.65), rotation=(radians(15.0), radians(-12.0), 0.0), bevel=0.004), "CyanEmissive")
"""


def look_materials_script() -> str:
    return """
import bpy

def set_bsdf_input(bsdf, names, value):
    for name in names:
        if name in bsdf.inputs:
            bsdf.inputs[name].default_value = value
            return

def ensure_material(name, base_color, metallic=0.0, roughness=0.4, emission_color=None, emission_strength=0.0):
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    set_bsdf_input(bsdf, ("Base Color",), base_color)
    set_bsdf_input(bsdf, ("Metallic",), metallic)
    set_bsdf_input(bsdf, ("Roughness",), roughness)
    if emission_color is not None and emission_strength > 0.0:
        set_bsdf_input(bsdf, ("Emission Color", "Emission"), emission_color)
        set_bsdf_input(bsdf, ("Emission Strength",), emission_strength)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    return mat

ensure_material("SentinelArmor", (0.14, 0.22, 0.34, 1.0), metallic=0.88, roughness=0.28)
ensure_material("CyanEmissive", (0.02, 0.85, 1.0, 1.0), metallic=0.1, roughness=0.15, emission_color=(0.0, 0.9, 1.0, 1.0), emission_strength=10.0)
ensure_material("PedestalDarkSteel", (0.07, 0.08, 0.11, 1.0), metallic=0.82, roughness=0.38)
ensure_material("GoldTrim", (0.85, 0.62, 0.18, 1.0), metallic=0.92, roughness=0.24)
ensure_material("VisorGlass", (0.01, 0.04, 0.07, 1.0), metallic=0.2, roughness=0.08, emission_color=(0.0, 0.75, 1.0, 1.0), emission_strength=6.0)
"""


def look_lighting_script() -> str:
    return """
import bpy
from mathutils import Vector

scene = bpy.context.scene
world = bpy.data.worlds.get("StudioWorld")
if world is None:
    world = bpy.data.worlds.new("StudioWorld")
scene.world = world
world.use_nodes = True
bg = world.node_tree.nodes.get("Background")
if bg is not None:
    bg.inputs["Color"].default_value = (0.015, 0.022, 0.038, 1.0)
    bg.inputs["Strength"].default_value = 0.8

def ensure_light(name, ltype, location, energy, color, size=2.0, aim=(0.0, 0.0, 1.8)):
    old = bpy.data.objects.get(name)
    if old is not None:
        old_data = old.data
        bpy.data.objects.remove(old, do_unlink=True)
        if old_data is not None and old_data.users == 0:
            bpy.data.lights.remove(old_data)
    ldata = bpy.data.lights.new(name=f"{name}_Data", type=ltype)
    ldata.energy = energy
    ldata.color = color
    if hasattr(ldata, "size") and ltype == 'AREA':
        ldata.size = size
    lobj = bpy.data.objects.new(name=name, object_data=ldata)
    lobj.location = location
    direction = Vector(aim) - Vector(location)
    if direction.length > 1e-6:
        lobj.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
    scene.collection.objects.link(lobj)
    return lobj

ensure_light("KeyLight", 'AREA', (3.8, -3.6, 5.2), 380.0, (1.0, 0.95, 0.90), size=2.4)
ensure_light("RimLight", 'AREA', (-3.4, 3.8, 4.0), 300.0, (0.35, 0.85, 1.0), size=2.0)
ensure_light("FillLight", 'AREA', (-4.0, -2.6, 3.0), 150.0, (0.72, 0.82, 1.0), size=3.0)
ensure_light("PadUnderGlow", 'POINT', (0.0, 0.0, 1.25), 75.0, (0.10, 0.90, 1.0))
"""


def camera_setup_script() -> str:
    return """
import bpy
from mathutils import Vector

scene = bpy.context.scene
old = bpy.data.objects.get("MainCam")
if old is not None:
    old_data = old.data
    bpy.data.objects.remove(old, do_unlink=True)
    if old_data is not None and old_data.users == 0:
        bpy.data.cameras.remove(old_data)

cam_data = bpy.data.cameras.new(name="MainCam_Data")
cam_data.lens = 50.0
cam_obj = bpy.data.objects.new("MainCam", cam_data)
cam_obj.location = (4.4, -4.8, 3.0)
direction = Vector((0.0, 0.0, 1.8)) - Vector(cam_obj.location)
cam_obj.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
scene.collection.objects.link(cam_obj)
scene.camera = cam_obj
scene.render.engine = 'BLENDER_EEVEE'
if hasattr(scene.eevee, "use_bloom"):
    scene.eevee.use_bloom = True
if hasattr(scene.eevee, "use_gtao"):
    scene.eevee.use_gtao = True
"""


def resolve_output_dir(output_dir: str | Path) -> Path:
    """Resolve relative deliverable output paths against REPO_ROOT, not process cwd."""
    path = Path(output_dir)
    if not path.is_absolute():
        return (REPO_ROOT / path).resolve()
    return path.resolve()


def camera_deliverables_script(output_dir: Path, res_x: int = 1280, res_y: int = 720) -> str:
    out_str = str(resolve_output_dir(output_dir))
    return f"""
import os
import bpy
from mathutils import Vector

out_dir = {out_str!r}
os.makedirs(out_dir, exist_ok=True)
scene = bpy.context.scene
cam = bpy.data.objects.get("MainCam") or scene.camera
if cam is None:
    cam_data = bpy.data.cameras.new(name="MainCam_Data")
    cam = bpy.data.objects.new("MainCam", cam_data)
    scene.collection.objects.link(cam)
scene.camera = cam
scene.render.engine = 'BLENDER_EEVEE'
scene.render.resolution_x = {res_x}
scene.render.resolution_y = {res_y}
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'PNG'

def aim_cam(loc, target=(0.0, 0.0, 1.8)):
    cam.location = loc
    direction = Vector(target) - Vector(loc)
    cam.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()

aim_cam((4.4, -4.8, 3.0))
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(out_dir, "starforge_sentinel.blend"), copy=True)
bpy.ops.export_scene.gltf(
    filepath=os.path.join(out_dir, "starforge_sentinel.glb"),
    export_format='GLB',
)

views = [
    ("front.png", (0.0, -5.4, 2.0)),
    ("turntable.png", (-4.8, -3.6, 2.8)),
    ("hero.png", (4.4, -4.8, 3.0)),
]
for fname, loc in views:
    aim_cam(loc)
    scene.render.filepath = os.path.join(out_dir, fname)
    bpy.ops.render.render(write_still=True)
aim_cam((4.4, -4.8, 3.0))
"""


def build_smoke_plan(
    *,
    num_agents: int,
    output_dir: Path,
    res_x: int = 1280,
    res_y: int = 720,
) -> dict[str, list[dict[str, Any]]]:
    """Return ordered per-agent turn definitions for 2-agent or 4-agent smoke runs."""
    resolved_output_dir = resolve_output_dir(output_dir)
    plan: dict[str, list[dict[str, Any]]] = {
        "modeler-agent": [
            {
                "lane": "script",
                "suffix": "m1",
                "write_set": [
                    "col:CHAR_mesh",
                    "obj:CHAR_Torso",
                    "obj:CHAR_Head",
                    "obj:CHAR_Visor",
                    "obj:CHAR_Core",
                ],
                "expect": "CHAR_mesh collection with Sentinel torso, head, visor, and core",
                "script": modeler_turn_1_script(),
            },
            {
                "lane": "script",
                "suffix": "m2",
                "write_set": [
                    "obj:CHAR_Shoulder_L",
                    "obj:CHAR_Shoulder_R",
                    "obj:CHAR_Arm_L",
                    "obj:CHAR_Arm_R",
                    "obj:CHAR_Forearm_L",
                    "obj:CHAR_Forearm_R",
                ],
                "expect": "Articulated Sentinel shoulders, arms, and forearms",
                "script": modeler_turn_2_script(),
            },
            {
                "lane": "script",
                "suffix": "m3",
                "write_set": [
                    "obj:CHAR_Pelvis",
                    "obj:CHAR_Leg_L",
                    "obj:CHAR_Leg_R",
                    "obj:CHAR_Foot_L",
                    "obj:CHAR_Foot_R",
                ],
                "expect": "Articulated Sentinel pelvis, legs, and boots planted at z=0.25",
                "script": modeler_turn_3_script(),
            },
        ],
    }

    if num_agents == 4:
        plan["props-agent"] = [
            {
                "lane": "script",
                "suffix": "p1",
                "write_set": [
                    "col:PROPS_set",
                    "obj:PROP_Pedestal",
                    "obj:PROP_PadRing",
                    "obj:PROP_PadSteps",
                ],
                "expect": "PROPS_set collection with hexagonal launch pad and ring",
                "script": props_turn_1_script(),
            },
            {
                "lane": "script",
                "suffix": "p2",
                "write_set": [
                    "obj:PROP_Pylon_01",
                    "obj:PROP_Pylon_02",
                    "obj:PROP_Pylon_03",
                    "obj:PROP_Pylon_04",
                    "obj:PROP_Pylon_05",
                    "obj:PROP_Pylon_06",
                    "obj:PROP_Conduit_01",
                ],
                "expect": "Six perimeter energy pylons and pad conduit",
                "script": props_turn_2_script(),
            },
            {
                "lane": "script",
                "suffix": "p3",
                "write_set": ["obj:PROP_Drone", "obj:PROP_DroneRing"],
                "expect": "Floating telemetry drone with gyroscopic ring",
                "script": props_turn_3_script(),
            },
        ]
        plan["look-agent"] = [
            {
                "lane": "script",
                "suffix": "l1",
                "write_set": [
                    "mat:SentinelArmor",
                    "mat:CyanEmissive",
                    "mat:PedestalDarkSteel",
                    "mat:GoldTrim",
                    "mat:VisorGlass",
                ],
                "expect": "PBR material datablocks created/updated in-place via bpy.data.materials",
                "script": look_materials_script(),
            },
            {
                "lane": "script",
                "suffix": "l2",
                "write_set": [
                    "world:StudioWorld",
                    "light:KeyLight",
                    "light:RimLight",
                    "light:FillLight",
                    "light:PadUnderGlow",
                ],
                "expect": "StudioWorld and 3-point studio lights configured",
                "script": look_lighting_script(),
            },
            {
                "lane": "script",
                "suffix": "l3",
                "write_set": [
                    "mat:SentinelArmor",
                    "mat:CyanEmissive",
                    "mat:PedestalDarkSteel",
                    "mat:GoldTrim",
                    "mat:VisorGlass",
                ],
                "expect": "Idempotent pure-datablock shader polish pass on bpy.data.materials",
                "script": look_materials_script(),
            },
        ]

    plan["camera-agent"] = [
        {
            "lane": "script",
            "suffix": "c1",
            "write_set": ["cam:MainCam", "render:engine", "render:eevee"],
            "expect": "MainCam positioned at hero 3/4 framing looking at (0,0,1.8) with Eevee configured",
            "script": camera_setup_script(),
        },
        {
            "lane": "gui",
            "suffix": "c2-gui",
            "after_builders": True,
            "keys": ["KP_1", "KP_3", "KP_7"],
            "footprint": list(DEFAULT_GUI_FOOTPRINT),
            "expect": "Front/side/top camera orbit review",
        },
        {
            "lane": "gui",
            "suffix": "c3-gui",
            "after_builders": True,
            "keys": ["KP_4", "KP_4", "KP_Add", "Home"],
            "footprint": list(DEFAULT_GUI_FOOTPRINT),
            "expect": "Turntable orbit, zoom, and frame-all review",
        },
        {
            "lane": "script",
            "suffix": "c4-render",
            "after_builders": True,
            "write_set": ["cam:MainCam", "render:engine", "render:FinalDeliverables"],
            "expect": "Save .blend (copy=True), export .glb, and render front/turntable/hero PNGs",
            "script": camera_deliverables_script(resolved_output_dir, res_x=res_x, res_y=res_y),
        },
    ]
    return plan


def _enqueue_and_wait_gui_with_rebase(
    *,
    agent_id: str,
    base_event_id: str,
    keys: list[str],
    footprint: list[int],
    expect: str,
    frames_dir: Path,
    intents_dir: Path,
    events_log: Path,
    timeout_s: float,
    force_initial_stale: bool = False,
) -> dict[str, Any]:
    """Enqueue GUI intent, handling STALE_FRAME (rc=3 at drops>=3) by re-observing latest.json."""
    stale_observed: list[dict[str, Any]] = []
    max_attempts = 4

    for attempt in range(max_attempts):
        event_id = base_event_id if attempt == 0 else f"{base_event_id}-r{attempt}"
        obs = observe_state(frames_dir=frames_dir, events_log=events_log, agent_id=agent_id)
        frame_override = None
        if force_initial_stale and attempt == 0:
            cur_frame = int(obs["frame"])
            if cur_frame < 1:
                raise RuntimeError(
                    f"--demo-stale-rebase requires head_frame >= 1 before GUI step, got {cur_frame}"
                )
            frame_override = cur_frame - 1

        intent, _ = build_gui_intent(
            agent_id=agent_id,
            event_id=event_id,
            keys=keys,
            footprint=footprint,
            expect=expect,
            frames_dir=frames_dir,
            events_log=events_log,
            based_on_frame_override=frame_override,
        )
        offset_bytes = record_enqueue_offset(
            intents_dir=intents_dir,
            agent_id=agent_id,
            event_id=event_id,
            events_log=events_log,
            created_ts_ms=intent["created_ts_ms"],
            script_sha256=intent["script_sha256"],
        )
        target_path = intents_dir / agent_id / f"{event_id}.json"
        atomic_write_json(target_path, intent, intents_dir)
        rc, receipt = wait_for_close_event(
            event_id=event_id,
            events_log=events_log,
            timeout_s=timeout_s,
            from_offset=offset_bytes,
            intents_dir=intents_dir,
            agent_id=agent_id,
        )
        if rc in (0, 2):
            if force_initial_stale and not stale_observed:
                raise RuntimeError(
                    f"--demo-stale-rebase expected at least one STALE_FRAME receipt for {base_event_id}"
                )
            return {
                "event_id": event_id,
                "rc": rc,
                "receipt": receipt,
                "stale_retries": stale_observed,
            }
        if rc == 3 and receipt.get("verdict") == "STALE_FRAME":
            stale_observed.append(receipt)
            continue
        raise RuntimeError(
            f"GUI event {event_id} failed (rc={rc}, verdict={receipt.get('verdict')}): {receipt}"
        )

    raise RuntimeError(f"GUI event {base_event_id} exhausted STALE_FRAME retries: {stale_observed}")


def run_smoke(
    *,
    num_agents: int,
    output_dir: Path,
    frames_dir: Path,
    intents_dir: Path,
    events_log: Path,
    run_id: str,
    timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
    demo_stale_rebase: bool = False,
    dry_run: bool = False,
    res_x: int = 1280,
    res_y: int = 720,
) -> dict[str, Any]:
    resolved_output_dir = resolve_output_dir(output_dir)
    plan = build_smoke_plan(
        num_agents=num_agents,
        output_dir=resolved_output_dir,
        res_x=res_x,
        res_y=res_y,
    )

    if dry_run:
        validated: list[dict[str, Any]] = []
        for agent_id, steps in plan.items():
            for step in steps:
                event_id = f"{run_id}-{agent_id}-{step['suffix']}"
                if step["lane"] == "script":
                    script = step["script"]
                    compile(script, f"{event_id}.py", "exec")
                    intent = {
                        "v": 1,
                        "event_id": event_id,
                        "agent_id": agent_id,
                        "lane": "script",
                        "action_class": "state_job",
                        "kind": "job",
                        "based_on_frame": 0,
                        "view_epoch": 0,
                        "scene_epoch": 0,
                        "lease_epoch": 1,
                        "write_set": step["write_set"],
                        "target_region": None,
                        "footprint": None,
                        "expect": step["expect"],
                        "script": script,
                        "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
                        "keys": None,
                        "max_acts": 1,
                        "cursor_xy": None,
                        "created_ts_ms": int(time.time() * 1000),
                    }
                else:
                    intent = {
                        "v": 1,
                        "event_id": event_id,
                        "agent_id": agent_id,
                        "lane": "gui",
                        "action_class": "view_change",
                        "kind": "key",
                        "based_on_frame": 0,
                        "view_epoch": 0,
                        "scene_epoch": 0,
                        "lease_epoch": 1,
                        "write_set": [],
                        "target_region": None,
                        "footprint": step["footprint"],
                        "expect": step["expect"],
                        "script": None,
                        "script_sha256": None,
                        "keys": step["keys"],
                        "max_acts": len(step["keys"]),
                        "cursor_xy": [800, 500],
                        "created_ts_ms": int(time.time() * 1000),
                    }
                validate_intent_dict(intent)
                validated.append(
                    {
                        "event_id": event_id,
                        "agent_id": agent_id,
                        "lane": step["lane"],
                        "write_set": intent["write_set"],
                        "keys": intent["keys"],
                    }
                )
        return {
            "mode": "dry_run",
            "agents": num_agents,
            "run_id": run_id,
            "intents_validated": len(validated),
            "steps": validated,
        }

    builders_done = threading.Event()
    builder_roles = [r for r in plan if r != "camera-agent"]
    builder_remaining = len(builder_roles)
    lock = threading.Lock()
    results: dict[str, list[dict[str, Any]]] = {r: [] for r in plan}
    errors: list[str] = []

    def worker(agent_id: str, steps: list[dict[str, Any]]) -> None:
        nonlocal builder_remaining
        try:
            for step_idx, step in enumerate(steps):
                if step.get("after_builders") and not builders_done.wait(timeout=timeout_s):
                    raise TimeoutError("Timed out waiting for builder agents to finish")

                base_event_id = f"{run_id}-{agent_id}-{step['suffix']}"
                if step["lane"] == "script":
                    intent, _ = build_script_intent(
                        agent_id=agent_id,
                        event_id=base_event_id,
                        write_set=step["write_set"],
                        expect=step["expect"],
                        script_source=step["script"],
                        frames_dir=frames_dir,
                        events_log=events_log,
                    )
                    offset_bytes = record_enqueue_offset(
                        intents_dir=intents_dir,
                        agent_id=agent_id,
                        event_id=base_event_id,
                        events_log=events_log,
                        created_ts_ms=intent["created_ts_ms"],
                        script_sha256=intent["script_sha256"],
                    )
                    target_path = intents_dir / agent_id / f"{base_event_id}.json"
                    atomic_write_json(target_path, intent, intents_dir)
                    rc, receipt = wait_for_close_event(
                        event_id=base_event_id,
                        events_log=events_log,
                        timeout_s=timeout_s,
                        from_offset=offset_bytes,
                        intents_dir=intents_dir,
                        agent_id=agent_id,
                    )
                    if rc != 0:
                        raise RuntimeError(
                            f"Script event {base_event_id} failed (rc={rc}): {receipt}"
                        )
                    results[agent_id].append(
                        {"event_id": base_event_id, "rc": rc, "receipt": receipt}
                    )
                else:
                    use_stale = demo_stale_rebase and step_idx == 1
                    res = _enqueue_and_wait_gui_with_rebase(
                        agent_id=agent_id,
                        base_event_id=base_event_id,
                        keys=step["keys"],
                        footprint=step["footprint"],
                        expect=step["expect"],
                        frames_dir=frames_dir,
                        intents_dir=intents_dir,
                        events_log=events_log,
                        timeout_s=timeout_s,
                        force_initial_stale=use_stale,
                    )
                    results[agent_id].append(res)
        except Exception as exc:
            with lock:
                errors.append(f"{agent_id}: {exc}")
            if agent_id != "camera-agent":
                builders_done.set()
        finally:
            if agent_id != "camera-agent":
                with lock:
                    builder_remaining -= 1
                    if builder_remaining <= 0:
                        builders_done.set()

    threads = [
        threading.Thread(target=worker, args=(role, steps), daemon=True)
        for role, steps in plan.items()
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        raise RuntimeError("; ".join(errors))

    return {
        "mode": "live",
        "agents": num_agents,
        "run_id": run_id,
        "demo_stale_rebase": demo_stale_rebase,
        "output_dir": str(resolved_output_dir),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m swarm.agents.scripted_smoke",
        description="Deterministic scripted multi-agent smoke producer for ViewPoint Swarm.",
    )
    parser.add_argument("--agents", type=int, choices=(2, 4), default=4)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--frames-dir", default=str(DEFAULT_FRAMES_DIR))
    parser.add_argument("--intents-dir", default=str(DEFAULT_INTENTS_DIR))
    parser.add_argument("--events-log", default=None)
    parser.add_argument("--run-id", default=f"smoke-{int(time.time())}")
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_WAIT_TIMEOUT_S)
    parser.add_argument("--resolution", default="1280x720")
    parser.add_argument("--demo-stale-rebase", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    res_parts = args.resolution.lower().split("x")
    res_x, res_y = int(res_parts[0]), int(res_parts[1])

    summary = run_smoke(
        num_agents=args.agents,
        output_dir=resolve_output_dir(args.output_dir),
        frames_dir=Path(args.frames_dir),
        intents_dir=Path(args.intents_dir),
        events_log=resolve_events_log(args.events_log),
        run_id=args.run_id,
        timeout_s=args.timeout_s,
        demo_stale_rebase=args.demo_stale_rebase,
        dry_run=args.dry_run,
        res_x=res_x,
        res_y=res_y,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
