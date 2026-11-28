# SPDX-License-Identifier: AGPL-3.0-only
"""Compose and cold-render one bound episode shot in Blender background mode.

    blender -b --factory-startup --disable-autoexec -noaudio --python-exit-code 2 \
        --python episode_shot.py -- settings.json

episode.py writes the settings from the bound episode. Every input is re-hashed here. The
stage and camera come from the pinned SET producer, the pinned clip drives the rig-bound
geometry, and the prop state is keyed to the certified contact contract. A refusal is written
to the receipt with status REFUSED and exits 0; a crash exits 2.
"""
import hashlib
import importlib.util
import json
import math
import os
import sys
import time
from pathlib import Path

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector

SCHEMA = "viewpoint.episode-shot/v1"
PALM_TOLERANCE_M, ROOT_TOLERANCE_M = 0.001, 0.010
TRANSFORM_TOLERANCE = 1e-5
TARGET_TOLERANCE_RAD = 1e-4
WITNESS_TOLERANCE_UV = 0.01
TEXTURES = {"base-color": "sRGB", "metallic-roughness": "Non-Color", "normal": "Non-Color"}
# Blender 4.0 writes every enabled stamp into the PNG; each carries the wall clock or the host.
STAMPS = ("use_stamp", "use_stamp_date", "use_stamp_time", "use_stamp_render_time", "use_stamp_frame",
          "use_stamp_frame_range", "use_stamp_memory", "use_stamp_hostname", "use_stamp_camera", "use_stamp_lens",
          "use_stamp_scene_time", "use_stamp_marker", "use_stamp_filename", "use_stamp_note",
          "use_stamp_sequencer_strip", "use_stamp_labels")


class Refused(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason, self.detail = reason, str(detail)


def need(condition, reason, detail=""):
    if not condition:
        raise Refused(reason, detail)


def sha_file(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checked(ref, reason):
    path = Path(ref["path"])
    need(path.is_file() and sha_file(path) == ref["sha256"], reason, f"{path.name} against {ref['sha256'][:16]}")
    return path


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def close(measured, expected, tolerance):
    return max(abs(a - b) for a, b in zip(measured, expected, strict=True)) <= tolerance


def cap_fraction(frame, cap):
    """Linear travel between the keys of the bound cap state, as the polish contract defines it."""
    keys = ((1, 0.0), (cap["contact"], 0.0), (cap["bottomed"], 1.0), (cap["hold_end"], 1.0), (cap["release"], 0.0))
    for (a, start), (b, end) in zip(keys, keys[1:]):
        if a <= frame <= b:
            return start + (end - start) * (frame - a) / (b - a)
    return 0.0


def bind_camera(scene, camera, screen_center):
    cameras = [obj for obj in scene.objects if obj.type == "CAMERA"]
    need(len(cameras) == 1, "camera_ambiguous", f"the built SET holds {len(cameras)} cameras")
    cam, = cameras
    need(cam.name == camera["name"], "camera_binding_mismatch", f"built {cam.name!r}, bound {camera['name']!r}")
    need(scene.camera is cam, "camera_binding_mismatch", "the scene does not render through the bound camera")
    need(cam.data.sensor_fit == "AUTO" and scene.render.resolution_x >= scene.render.resolution_y, "camera_sensor_fit",
         "the sensor width applies to the larger image dimension only")
    need(cam.animation_data is None and cam.data.animation_data is None, "camera_ambiguous",
         "the bound camera carries animation of its own")
    need(cam.data.lens == camera["lens_mm"] and cam.data.sensor_width == camera["sensor_width_mm"],
         "camera_binding_mismatch", "lens or sensor width")
    need(close(cam.location, camera["location_m"], TRANSFORM_TOLERANCE)
         and close(cam.rotation_euler, camera["rotation_euler_rad"], TRANSFORM_TOLERANCE),
         "camera_binding_mismatch", f"location {tuple(cam.location)} rotation {tuple(cam.rotation_euler)}")
    forward = cam.matrix_world.to_quaternion() @ Vector((0.0, 0.0, -1.0))
    aim = Vector(camera["target_m"]) - cam.location
    need(forward.angle(aim) <= TARGET_TOLERANCE_RAD, "camera_binding_mismatch",
         f"the camera aims {math.degrees(forward.angle(aim)):.4f} degrees away from its target")
    witness = world_to_camera_view(scene, cam, Vector(screen_center))
    delta = max(abs(witness.x - camera["screen_center_uv"][0]), abs(witness.y - camera["screen_center_uv"][1]))
    need(delta <= WITNESS_TOLERANCE_UV, "camera_witness_drift",
         f"the screen centre projects {delta:.5f} UV away from the registered witness")
    return {"name": cam.name, "lens_mm": cam.data.lens, "sensor_width_mm": cam.data.sensor_width,
            "location_m": list(cam.location), "rotation_euler_rad": list(cam.rotation_euler), "cameras_in_scene": 1,
            "screen_center_m": list(screen_center), "screen_center_uv": [witness.x, witness.y],
            "witness_delta_uv": delta, "target_error_deg": math.degrees(forward.angle(aim))}


def apply_clip(clip, armature):
    name = clip["name"]
    actions = [item for item in bpy.data.actions if item.name == name or item.name.startswith(name + "_")]
    need(actions, "motion_clip_missing", f"no imported action for {name!r}")
    need(len(actions) == 1, "clip_ambiguous", f"{[item.name for item in actions]} all answer to {name!r}")
    action, = actions
    for track in armature.animation_data.nla_tracks:
        track.mute = True
    armature.animation_data.action = action
    # The importer writes glTF time as zero-based frames; the SET and the contact contract are one-based.
    keys = [key for curve in action.fcurves for key in curve.keyframe_points]
    need(keys and all(float(key.co.x).is_integer() and 0 <= key.co.x <= clip["frames"] - 1 for key in keys),
         "clip_timebase_unexpected", f"{min(key.co.x for key in keys)}..{max(key.co.x for key in keys)}")
    for key in keys:
        key.co.x += 1
        key.handle_left.x += 1
        key.handle_right.x += 1
    for curve in action.fcurves:
        curve.update()
    need(close(action.frame_range, (1, clip["frames"]), 1e-6), "clip_length_mismatch",
         f"{tuple(action.frame_range)} against 1..{clip['frames']}")
    return action


def key_props(scene, stage, shot, registration):
    button = scene.objects[registration["button"]["object"]]
    screen = scene.objects[registration["screen"]["object"]]
    cap, crt = shot["prop_state"]["PLAY Button"], shot["prop_state"]["CRT Screen"]
    # A CRT that lights before the cap bottoms is an early activation, whatever the document says.
    need(cap["bottomed"] <= crt["power_on"], "early_screen_activation",
         f"power-on {crt['power_on']} before bottomed {cap['bottomed']}")
    try:
        stage.key_interaction(button, screen, contact=cap["contact"], bottomed=cap["bottomed"],
                              released=cap["release"], power_on=crt["power_on"])
    except ValueError as exc:
        raise Refused("prop_state_order", f"{exc}: {cap} {crt}") from None
    if cap["hold_end"] > cap["bottomed"]:
        button.location.y = button["rest_y"] + stage.TRAVEL
        button.keyframe_insert("location", index=1, frame=cap["hold_end"])
    for key in button.animation_data.action.fcurves.find("location", index=1).keyframe_points:
        key.interpolation = "LINEAR"
    for name, frame in (("CRT_POWER_ON", crt["power_on"]), ("PLAY_HOLD_END", cap["hold_end"])):
        scene.timeline_markers.new(name, frame=frame)
    return button, screen


def opaque_actor(meshes, expected_material):
    """Eevee BLEND sorts whole objects; the baked coverage belongs to one closed, skinned mesh."""
    materials = {mat for obj in meshes for mat in obj.data.materials}
    need(materials and all(mat is not None and mat.name.split(".")[0] == expected_material for mat in materials),
         "actor_look_graph", [mat.name if mat else None for mat in materials])
    for mat in materials:
        mat.blend_method = "OPAQUE"
        mat.shadow_method = "OPAQUE"
        (shader,) = [node for node in mat.node_tree.nodes if node.type == "BSDF_PRINCIPLED"]
        textures = {node.image.name.split(".")[0]: node.image.colorspace_settings.name
                    for node in mat.node_tree.nodes if node.type == "TEX_IMAGE"}
        need(textures == TEXTURES and all(node.image.packed_file for node in mat.node_tree.nodes
                                          if node.type == "TEX_IMAGE"), "actor_look_graph", textures)
        need(shader.inputs["Transmission Weight"].default_value == 0
             and not shader.inputs["Transmission Weight"].is_linked
             and all(shader.inputs[name].is_linked for name in ("Base Color", "Alpha", "Normal", "Metallic", "Roughness")),
             "actor_look_graph", "the baked PBR graph is not wired to the Principled shader")


def actor(scene, shot):
    armatures = [obj for obj in scene.objects if obj.type == "ARMATURE"]
    need(len(armatures) == 1, "rig_ambiguous", f"{len(armatures)} armatures after import")
    armature, = armatures
    meshes = [obj for obj in scene.objects if obj.type == "MESH" and not obj.get("rp4_ankle_gaiter")
              and any(mod.type == "ARMATURE" and mod.object == armature for mod in obj.modifiers)]
    need(len(meshes) == 1, "bound_geometry_ambiguous", f"{len(meshes)} skinned meshes after import")
    need([bone.name for bone in armature.data.bones] == shot["rig_binding"]["joints"], "rig_binding_mismatch",
         "the imported bones differ from the bound roster")
    return armature, meshes[0]


def sample(scene, armature, mesh, button, screen, shot, rest_y):
    """Measure the posed scene on every recorded frame against the certified replay."""
    indices = shot["rig_binding"]["hand_vertex_indices"]
    root_bone = armature.pose.bones[shot["rig_binding"]["joints"][0]]
    strength = screen.active_material.node_tree.nodes["Principled BSDF"].inputs["Emission Strength"]
    face = Vector((0.0, 0.0, min(vertex.co.z for vertex in button.data.vertices)))
    cap, crt = shot["prop_state"]["PLAY Button"], shot["prop_state"]["CRT Screen"]
    rows = []
    for frame in range(shot["source"]["record_in"], shot["source"]["record_out"] + 1):
        scene.frame_set(frame)
        evaluated = mesh.evaluated_get(bpy.context.evaluated_depsgraph_get())
        palm = sum((evaluated.matrix_world @ evaluated.data.vertices[i].co for i in indices), Vector((0.0, 0.0, 0.0))) / len(indices)
        root = armature.matrix_world @ root_bone.head
        travel = (button.matrix_world @ face).y - rest_y
        hand_error = (palm - Vector(shot["pose_replay"]["hand_world_m"][frame - 1])).length
        root_error = (root - Vector(shot["pose_replay"]["root_world_m"][frame - 1])).length
        emission = strength.default_value
        need(hand_error <= PALM_TOLERANCE_M and root_error <= ROOT_TOLERANCE_M, "pose_replay_mismatch",
             f"frame {frame}: palm {hand_error:.6f} m, root {root_error:.6f} m from the certified motion")
        # The shader keeps the strength as a 32-bit float, so the bound value matches within that precision.
        need(abs(emission - (crt["on"] if frame >= crt["power_on"] else crt["off"])) <= 1e-6,
             "early_screen_activation" if emission and frame < crt["power_on"] else "screen_state_mismatch",
             f"frame {frame}: emission {emission}")
        need(abs(travel - cap_fraction(frame, cap) * cap["travel_m"]) <= 1e-5, "cap_travel_mismatch",
             f"frame {frame}: {travel:.6f} m against the bound cap state")
        rows.append({"frame": frame, "hand_error_m": hand_error, "root_error_m": root_error,
                     "screen_emission": emission, "cap_travel_m": travel})
    return rows


def expect_settings(scene, settings, end_frame):
    need(scene.render.engine == "BLENDER_EEVEE" and scene.eevee.taa_render_samples == settings["samples"]
         and [scene.render.resolution_x, scene.render.resolution_y] == settings["pixels"]
         and scene.render.resolution_percentage == 100 and scene.render.fps == settings["fps"]
         and scene.render.fps_base == 1.0 and (scene.frame_start, scene.frame_end) == (1, end_frame)
         and scene.render.threads_mode == "FIXED" and scene.render.threads == settings["threads"]
         and not any(getattr(scene.render, key) for key in STAMPS if hasattr(scene.render, key)),
         "render_settings_binding",
         "the reopened scene does not carry the bound render settings")
    need(os.environ.get("GALLIUM_DRIVER") == "llvmpipe" and os.environ.get("LIBGL_ALWAYS_SOFTWARE") == "1",
         "software_gl_unverified", "the render must run on the CPU software renderer")


def render_settings(scene, settings, registration):
    scene.render.engine = "BLENDER_EEVEE"
    scene.eevee.taa_render_samples = settings["samples"]
    scene.render.resolution_x, scene.render.resolution_y = settings["pixels"]
    scene.render.resolution_percentage = 100
    scene.render.fps, scene.render.fps_base = settings["fps"], 1.0
    scene.render.threads_mode = "FIXED"
    scene.render.threads = settings["threads"]
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    # Stamps carry the wall clock; without them a cold render reproduces the same bytes.
    for key in STAMPS:
        if hasattr(scene.render, key):
            setattr(scene.render, key, False)
    scene.frame_start, scene.frame_end = 1, registration["end_frame"]
    expect_settings(scene, settings, registration["end_frame"])


def write_receipt(path, receipt):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".pending")
    temporary.write_text(json.dumps(receipt, sort_keys=True, indent=1, allow_nan=False) + "\n")
    temporary.replace(path)


def compose(settings, shot, receipt):
    producer = checked(settings["set_producer"], "stale_companion")
    registration = json.loads(checked(shot["assets"]["set"], "asset_digest_mismatch").read_bytes())
    stage = load_module("rp4_set", producer)
    # The producer must still build the stage its registration describes.
    need(stage.FPS == registration["fps"] and stage.LAST_FRAME == registration["end_frame"]
         and stage.MARKERS == registration["markers"]
         and tuple(stage.CONTACT) == tuple(registration["button"]["rest_contact_world_m"])
         and stage.TRAVEL == registration["button"]["travel_m"]
         and stage.SCREEN_STRENGTH == registration["screen"]["on_emission_strength"], "set_producer_drift",
         "set.py constants disagree with SET.json")
    scene = stage.build_scene()
    scene["rp4_episode_shot"] = shot["id"]
    scene["rp4_episode_bound"] = settings["bound_sha256"]
    motion = checked(shot["assets"]["motion"], "asset_digest_mismatch")
    checked(shot["assets"]["rig"], "asset_digest_mismatch")
    checked(shot["assets"]["character"], "asset_digest_mismatch")
    # The camera is bound before the actor exists, so an empty stage cannot pass for the registered one.
    receipt["camera"] = bind_camera(scene, shot["camera"], stage.SCREEN_CENTER)
    bpy.ops.import_scene.gltf(filepath=str(motion), merge_vertices=False)
    scene["rp4_motion_sha256"] = shot["assets"]["motion"]["sha256"]
    armature, mesh = actor(scene, shot)
    action = apply_clip(shot["clip"], armature)
    receipt["clip"] = {"name": action.name, "range": list(action.frame_range), "fcurves": len(action.fcurves)}
    opaque_actor([mesh], settings["material_name"])
    button, screen = key_props(scene, stage, shot, registration)
    curve = button.animation_data.action.fcurves.find("location", index=1)
    receipt["cap_keys"] = sorted([key.co.x, key.co.y - button["rest_y"]] for key in curve.keyframe_points)
    render_settings(scene, settings, registration)
    return scene, armature, mesh, button, screen, registration


def render(settings):
    shot = settings["shot"]
    frames, blend = Path(settings["frame_dir"]), Path(settings["blend"])
    receipt = {"schema": SCHEMA, "status": "REFUSED", "mode": "render", "shot": shot["id"],
               "bound_sha256": settings["bound_sha256"], "blender": bpy.app.version_string, "engine": "BLENDER_EEVEE",
               "samples": settings["samples"], "pixels": settings["pixels"], "fps": settings["fps"],
               "threads": settings["threads"], "gallium_driver": os.environ.get("GALLIUM_DRIVER")}
    try:
        # Cold: nothing of an earlier render of this shot may exist.
        need(not blend.exists() and not list(frames.glob("*.png")), "warm_cache_present", str(frames))
        scene, armature, mesh, button, screen, registration = compose(settings, shot, receipt)
        receipt["geometry"] = {"armature": armature.name, "bones": len(armature.data.bones), "skinned_mesh": mesh.name,
                               "polygons": len(mesh.data.polygons), "vertices": len(mesh.data.vertices)}
        rest_y = registration["button"]["rest_contact_world_m"][1]
        receipt["rows"] = sample(scene, armature, mesh, button, screen, shot, rest_y)
        scene.frame_set(1)
        bpy.ops.file.pack_all()
        bpy.context.preferences.filepaths.save_version = 0
        bpy.ops.wm.save_as_mainfile(filepath=str(blend))
        receipt["blend_sha256"] = sha_file(blend)
        frames.mkdir(parents=True, exist_ok=True)
        receipt["frames"] = []
        for row in receipt["rows"]:
            scene.frame_set(row["frame"])
            target = frames / f"{row['frame']:03d}.png"
            scene.render.filepath = str(target)
            started = time.monotonic()
            bpy.ops.render.render(write_still=True)
            receipt["frames"].append({"frame": row["frame"], "file": target.name, "sha256": sha_file(target),
                                      "seconds": round(time.monotonic() - started, 3)})
        receipt["status"] = "PASS"
    except Refused as exc:
        receipt["reason"], receipt["detail"] = exc.reason, exc.detail
    write_receipt(Path(settings["receipt"]), receipt)
    print("SHOT_RECEIPT " + json.dumps({key: receipt.get(key) for key in ("status", "reason", "shot")}, sort_keys=True))


def reopen(settings):
    """A fresh process reopens the saved shot, replays the binding and re-renders one frame."""
    shot = settings["shot"]
    blend = Path(settings["blend"])
    receipt = {"schema": SCHEMA, "status": "REFUSED", "mode": "reopen", "shot": shot["id"],
               "bound_sha256": settings["bound_sha256"], "blender": bpy.app.version_string}
    try:
        original = json.loads(Path(settings["receipt"]).read_bytes())
        need(original["status"] == "PASS" and original["shot"] == shot["id"] and sha_file(blend) == original["blend_sha256"],
             "shot_blend_changed", f"{blend.name} against the render receipt")
        bpy.ops.wm.open_mainfile(filepath=str(blend), use_scripts=False)
        scene = bpy.context.scene
        need(scene.get("rp4_episode_shot") == shot["id"] and scene.get("rp4_episode_bound") == settings["bound_sha256"]
             and scene.get("rp4_motion_sha256") == shot["assets"]["motion"]["sha256"], "reopened_scene_binding",
             "the saved scene belongs to another shot or binding")
        registration = json.loads(Path(shot["assets"]["set"]["path"]).read_bytes())
        expect_settings(scene, settings, registration["end_frame"])
        receipt["camera"] = bind_camera(scene, shot["camera"], original["camera"]["screen_center_m"])
        need(receipt["camera"] == original["camera"], "reopened_scene_binding", "the camera measures differently")
        armature, mesh = actor(scene, shot)
        active = armature.animation_data.action
        need(active is not None and active.name == original["clip"]["name"]
             and all(track.mute for track in armature.animation_data.nla_tracks), "reopened_scene_binding",
             "another clip or an NLA track is active")
        button = scene.objects[registration["button"]["object"]]
        screen = scene.objects[registration["screen"]["object"]]
        receipt["rows"] = sample(scene, armature, mesh, button, screen, shot, registration["button"]["rest_contact_world_m"][1])
        delta = max((abs(a[key] - b[key]) for a, b in zip(receipt["rows"], original["rows"], strict=True)
                     for key in ("hand_error_m", "root_error_m", "screen_emission", "cap_travel_m")), default=0.0)
        need(delta <= 1e-9, "reopen_replay_diverged", f"the reopened scene measures {delta} away from the render")
        receipt["replay_delta"] = delta
        if "reopen_frame" in settings:
            target = Path(settings["reopen_frame_png"])
            target.parent.mkdir(parents=True, exist_ok=True)
            scene.frame_set(settings["reopen_frame"])
            scene.render.filepath = str(target)
            bpy.ops.render.render(write_still=True)
            receipt["reopen_frame"] = {"frame": settings["reopen_frame"], "sha256": sha_file(target)}
            need(receipt["reopen_frame"]["sha256"] == next(frame["sha256"] for frame in original["frames"]
                                                           if frame["frame"] == settings["reopen_frame"]),
                 "reopen_render_diverged", f"frame {settings['reopen_frame']} did not reproduce")
        receipt["status"] = "PASS"
    except Refused as exc:
        receipt["reason"], receipt["detail"] = exc.reason, exc.detail
    write_receipt(Path(settings["reopen_receipt"]), receipt)
    print("SHOT_RECEIPT " + json.dumps({key: receipt.get(key) for key in ("status", "reason", "shot")}, sort_keys=True))


if __name__ == "__main__":
    arguments = json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_bytes())
    (render if arguments["mode"] == "render" else reopen)(arguments)
