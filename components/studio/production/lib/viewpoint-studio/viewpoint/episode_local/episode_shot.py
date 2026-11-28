# SPDX-License-Identifier: AGPL-3.0-only
"""Blender-side episode adapter: bind the native scene, selected clip and camera, render and reopen."""
import math
import os
import signal
import sys
from pathlib import Path

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from viewpoint.episode_local import common as C

SCHEMA = "viewpoint.episode-shot/v1"
TRANSFORM_TOLERANCE = 1e-5
STAMPS = ("use_stamp", "use_stamp_date", "use_stamp_time", "use_stamp_render_time", "use_stamp_frame",
          "use_stamp_frame_range", "use_stamp_memory", "use_stamp_hostname", "use_stamp_camera", "use_stamp_lens",
          "use_stamp_scene_time", "use_stamp_marker", "use_stamp_filename", "use_stamp_note",
          "use_stamp_sequencer_strip", "use_stamp_labels")


def close(actual, expected):
    return all(abs(left - right) <= TRANSFORM_TOLERANCE for left, right in zip(actual, expected, strict=True))


def bind_camera(scene, camera, activate):
    """Check the declared camera against native data before setting or reading the active camera."""
    obj = scene.objects.get(camera["name"])
    C.require(obj is not None and obj.type == "CAMERA", "camera_binding_mismatch")
    aimed = (Vector(camera["target_m"]) - Vector(camera["location_m"])).to_track_quat("-Z", "Y").to_euler()
    C.require(obj.parent is None and not obj.constraints and obj.animation_data is None
              and obj.data.animation_data is None and obj.data.type == "PERSP"
              and obj.data.sensor_fit == "AUTO" and obj.data.shift_x == obj.data.shift_y == 0
              and close(obj.location, camera["location_m"]) and close(obj.rotation_euler, aimed)
              and obj.data.lens == camera["lens_mm"] and obj.data.sensor_width == camera["sensor_width_mm"],
              "camera_binding_mismatch")
    if activate:
        scene.camera = obj
    C.require(scene.camera is obj, "camera_binding_mismatch")
    return {"name": obj.name, "lens_mm": obj.data.lens, "sensor_width_mm": obj.data.sensor_width,
            "location_m": list(obj.location), "rotation_euler_rad": list(obj.rotation_euler)}


def apply_clip(scene, clip):
    actor = scene.objects.get(clip["actor"])
    signal_obj = scene.objects.get(clip["signal"])
    C.require(actor is not None and signal_obj is not None, "clip_binding_mismatch")
    for obj in (actor, signal_obj):
        obj.animation_data_clear()
    for key in clip["keys"]:
        actor.location = key["location"]
        actor.keyframe_insert(data_path="location", frame=key["frame"])
        signal_obj.hide_render = not key["signal_on"]
        signal_obj.keyframe_insert(data_path="hide_render", frame=key["frame"])
    actor.animation_data.action.name = clip["name"]
    signal_obj.animation_data.action.name = clip["name"] + "-signal"
    for curve in actor.animation_data.action.fcurves:
        for key in curve.keyframe_points:
            key.interpolation = "LINEAR"


def read_clip(scene, clip):
    actor = scene.objects[clip["actor"]]
    signal_obj = scene.objects[clip["signal"]]
    action = actor.animation_data.action
    C.require(action.name == clip["name"] and signal_obj.animation_data.action.name == clip["name"] + "-signal"
              and not actor.animation_data.nla_tracks and not signal_obj.animation_data.nla_tracks,
              "clip_binding_mismatch")
    C.require(close(action.frame_range, [clip["keys"][0]["frame"], clip["keys"][-1]["frame"]]),
              "clip_binding_mismatch")
    for key in clip["keys"]:
        scene.frame_set(key["frame"])
        C.require(close(actor.location, key["location"]) and signal_obj.hide_render != key["signal_on"],
                  "clip_binding_mismatch")
    return {"name": action.name, "range": list(action.frame_range), "fcurves": len(action.fcurves)}


def open_scene(path):
    bpy.ops.wm.open_mainfile(filepath=str(path), use_scripts=False, load_ui=False)
    C.require(not bpy.data.libraries and all(image.packed_file or image.source in {"GENERATED", "VIEWER"}
                                           for image in bpy.data.images if image.name != "Render Result"),
              "external_scene_dependencies")
    return bpy.context.scene


def render_settings(scene, settings):
    render = scene.render
    render.engine = "BLENDER_WORKBENCH"
    scene.display.render_aa = "OFF"
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "MATERIAL"
    scene.display.shading.show_cavity = True
    scene.display.shading.show_shadows = True
    scene.display.shading.background_type = "WORLD"
    render.resolution_x, render.resolution_y = settings["pixels"]
    render.resolution_percentage = 100
    render.fps, render.fps_base = settings["fps"], 1
    render.threads_mode, render.threads = "FIXED", 2
    render.image_settings.file_format = "PNG"
    render.image_settings.color_mode = "RGB"
    for name in STAMPS:
        if hasattr(render, name):
            setattr(render, name, False)
    render.filepath = "//"
    scene.frame_start, scene.frame_end = settings["source"]


def render_frame(scene, frame, target):
    scene.frame_set(frame)
    scene.render.filepath = str(target)
    bpy.ops.render.render(write_still=True)
    return {"frame": frame, "file": target.name, "sha256": C.sha_file(target)}


def run(settings, work):
    where = Path(settings["artifact_root"])
    shot = settings["shot"]
    mode = settings["mode"]
    camera = C.load(C.pin(where, shot["camera"], "camera_substitution"))
    clip = C.load(C.pin(where, shot["clip"], "clip_substitution"))
    receipt = {"schema": SCHEMA, "status": "PASS", "mode": mode, "shot": shot["id"],
               "binding_sha256": settings["binding_sha256"], "blender": bpy.app.version_string,
               "engine": "BLENDER_WORKBENCH", "input_pins": {name: shot[name] for name in ("scene", "clip", "camera")},
               "pixels": settings["pixels"], "fps": settings["fps"]}
    blend = work / "shot.blend"
    if mode == "render":
        scene = open_scene(C.pin(where, shot["scene"], "scene_substitution"))
        receipt["camera"] = bind_camera(scene, camera, activate=True)
        apply_clip(scene, clip)
        receipt["clip"] = read_clip(scene, clip)
        render_settings(scene, settings)
        scene["episode_binding_sha256"] = settings["binding_sha256"]
        scene.frame_set(scene.frame_start)
        bpy.ops.file.pack_all()
        bpy.data.libraries.write(str(blend), {scene}, path_remap="RELATIVE_ALL", fake_user=True)
        frames = work / "frames"
        frames.mkdir()
        receipt["frames"] = []
        for frame in range(scene.frame_start, scene.frame_end + 1):
            receipt["frames"].append(render_frame(scene, frame, frames / f"{frame:04d}.png"))
            if settings["interrupt"]:
                C.write(work / "interrupted.json", {"pid": os.getpid(), **receipt["frames"][-1]})
                os.kill(os.getpid(), signal.SIGKILL)
    else:
        original = C.load(work / "shot-receipt.json")
        C.require(C.sha_file(blend) == original["blend_sha256"], "saved_scene_changed")
        scene = open_scene(blend)
        C.require(scene["episode_binding_sha256"] == settings["binding_sha256"]
                  and (scene.frame_start, scene.frame_end) == tuple(settings["source"])
                  and (scene.render.resolution_x, scene.render.resolution_y) == tuple(settings["pixels"])
                  and scene.render.fps == settings["fps"] and scene.render.fps_base == 1
                  and scene.render.engine == receipt["engine"], "saved_scene_binding_mismatch")
        receipt["camera"] = bind_camera(scene, camera, activate=False)
        receipt["clip"] = read_clip(scene, clip)
        C.require(receipt["camera"] == original["camera"] and receipt["clip"] == original["clip"],
                  "saved_scene_binding_mismatch")
        first = original["frames"][0]
        witness = render_frame(scene, first["frame"], work / "witness.png")
        C.require(witness["sha256"] == first["sha256"], "reopen_render_diverged")
        receipt["witness"] = witness
    receipt["blend_sha256"] = C.sha_file(blend)
    return receipt


def main():
    settings = C.load(Path(sys.argv[sys.argv.index("--") + 1]))
    work = Path(settings["work"])
    C.require(os.environ.get("GALLIUM_DRIVER") == "llvmpipe" and os.environ.get("LIBGL_ALWAYS_SOFTWARE") == "1"
              and not Path("/dev/dri").exists(), "software_render_unverified")
    try:
        result = run(settings, work)
    except C.Refusal as exc:
        result = {"schema": SCHEMA, "status": "REFUSED", "reason": exc.reason, "detail": str(exc)}
    name = "shot-receipt.json" if settings["mode"] == "render" else "reopen-receipt.json"
    C.write(work / name, result)


if __name__ == "__main__":
    main()
