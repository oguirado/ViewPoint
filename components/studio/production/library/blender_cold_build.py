# SPDX-License-Identifier: AGPL-3.0-only
"""Cold-build one recipe from a staged cold root, inside Blender in background mode on the CPU.

The build plan names staged paths and their SHA-256 digests. This script
checks the bytes and the bindings before it renders a pixel, renders, then checks the pixels. A
failed check is a named rejection: one JSON line on stderr and exit 1. Anything else is a harness
fault and leaves exit 2.

Checks, in order: staged_digests, textures_present (glTF), rig_motion_compatible, character_bound,
set_metadata, camera_binding, motion_clip, joints_driven, textures_present (Blender), interaction,
cap_contact and actor_in_frame (geometry), blank_frame and the screen response (pixels).
"""

import hashlib
import importlib.util
import json
import os
import struct
import sys
import time
from pathlib import Path

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector

ABSENT = "AUSENTE"
CAP_TOLERANCE_M = 0.02     # the nearest actor vertex at the contact frame
FAR_M = 1.0                # the nearest actor vertex on the first frame
SCREEN_RESPONSE = 0.12     # green rise of the CRT after power-on
SCREEN_SILENCE = 0.06      # the largest green change tolerated when nothing is pressed
PIXEL_SPAN, PIXEL_MAX = 0.14, 0.20
FLOOR = 20 << 30


class Reject(Exception):
    """A named rejection with what was measured."""

    def __init__(self, reason, detail, measured=None):
        super().__init__(detail)
        self.reason, self.detail, self.measured = reason, str(detail), measured or {}


def need(condition, reason, detail="", measured=None):
    if not condition:
        raise Reject(reason, detail, measured)


def sha_file(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def available_ram():
    with open("/proc/meminfo", encoding="ascii") as stream:
        return next(int(line.split()[1]) for line in stream if line.startswith("MemAvailable:")) * 1024


def floor():
    """Hold while MemAvailable is under 20 GiB."""
    while available_ram() < FLOOR:
        time.sleep(10)


# ---------------------------------------------------------------- glTF, read before Blender opens anything

def glb(path):
    data = Path(path).read_bytes()
    magic, version, length, json_bytes, json_type = struct.unpack_from("<4sIIII", data)
    need(magic == b"glTF" and version == 2 and length == len(data) and json_type == 0x4E4F534A,
         "invalid_glb", path)
    return json.loads(data[20:20 + json_bytes]), data[28 + json_bytes:]


def accessor(document, blob, index):
    item, view = document["accessors"][index], document["bufferViews"][document["accessors"][index]["bufferView"]]
    width = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}[item["type"]]
    size = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}[item["componentType"]]
    start = view.get("byteOffset", 0) + item.get("byteOffset", 0)
    return blob[start:start + width * size * item["count"]]


def rest_of(document, blob):
    primitive = document["meshes"][0]["primitives"][0]
    skin = document["skins"][0]
    return (accessor(document, blob, primitive["attributes"]["POSITION"]),
            accessor(document, blob, skin["inverseBindMatrices"]))


def images_of(document, blob, folder):
    """Image name to the SHA-256 of its bytes; an external image is read from beside the GLB or is AUSENTE."""
    out = {}
    for index, image in enumerate(document.get("images", [])):
        name = image.get("name", f"image_{index}")
        if "bufferView" in image:
            view = document["bufferViews"][image["bufferView"]]
            start = view.get("byteOffset", 0)
            out[name] = hashlib.sha256(blob[start:start + view["byteLength"]]).hexdigest()
        else:
            local = folder / str(image.get("uri"))
            out[name] = sha_file(local) if local.is_file() else ABSENT
    return out


def joints_of(document):
    nodes = document.get("nodes", [])
    return sorted(nodes[joint].get("name", f"node_{joint}") for skin in document.get("skins", []) for joint in skin["joints"])


# ---------------------------------------------------------------- the build

def build(plan, receipt):
    checks, measured = receipt["checks"], receipt["measured"]
    inputs = plan["inputs"]

    def record(check, ok, **values):
        checks[check] = {"ok": bool(ok), **values}

    bad = sorted(role for role, item in inputs.items() if sha_file(item["path"]) != item["sha256"])
    record("staged_digests", not bad, files=len(inputs))
    need(not bad, "staged_digest_mismatch", bad)
    documents = {role: glb(inputs[role]["path"]) for role in ("character", "rig", "motion")}
    atlases = {role: images_of(document, blob, Path(inputs[role]["path"]).parent)
               for role, (document, blob) in documents.items()}
    missing = [f"{role}:{name}" for role, images in atlases.items() for name, sha in images.items() if sha == ABSENT]
    record("textures_present", not missing, images={role: len(images) for role, images in atlases.items()})
    need(not missing, "missing_texture", {"absent": missing})

    rig_joints, motion_joints = joints_of(documents["rig"][0]), joints_of(documents["motion"][0])
    need(motion_joints and set(motion_joints) <= set(rig_joints), "motion_rig_joint_mismatch",
         {"rig_joints": len(rig_joints), "motion_joints": len(motion_joints)})
    need(rest_of(*documents["rig"]) == rest_of(*documents["motion"]), "rig_motion_rest_mismatch",
         {"rig_triangles": documents["rig"][0]["accessors"][documents["rig"][0]["meshes"][0]["primitives"][0]["indices"]]["count"] // 3})
    record("rig_motion_compatible", True, joints=len(rig_joints))
    need(atlases["character"] and atlases["character"] == atlases["rig"], "character_rig_unbound",
         {"character_images": sorted(atlases["character"]), "rig_images": sorted(atlases["rig"])})
    record("character_bound", True, images=sorted(atlases["rig"]))

    spec = importlib.util.spec_from_file_location("studio_set_cold", inputs["set_producer"]["path"])
    stage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stage)
    scene = stage.build_scene()
    pinned = json.loads(Path(inputs["set"]["path"]).read_bytes())
    described = json.loads(json.dumps(stage.describe_scene(scene)))
    record("set_metadata", described == pinned, differ=sorted(key for key in set(pinned) | set(described)
                                                               if pinned.get(key) != described.get(key)))
    need(described == pinned, "set_metadata_mismatch", checks["set_metadata"])

    camera = scene.camera
    want = plan["camera"]["camera"]
    same = (camera.name == plan["camera"].get("camera_name") and abs(camera.data.lens - want["lens_mm"]) < 1e-4
            and (camera.location - Vector(want["location_m"])).length < 1e-4
            and max(abs(a - b) for a, b in zip(camera.rotation_euler, want["rotation_euler_rad"])) < 1e-4)
    record("camera_binding", same, name=camera.name, lens_mm=camera.data.lens)
    need(same, "camera_binding_mismatch", {"built": {"name": camera.name, "lens_mm": camera.data.lens,
                                                     "location_m": list(camera.location)}, "pinned": want})

    if plan.get("remove_actor"):   # a control only: the stage, its camera and its keyed props, without the actor
        record("actor", True, mode="control_without_actor")
    else:
        place_actor(plan, checks, measured, scene, stage)
    return render_and_check(plan, receipt, scene, stage)


def place_actor(plan, checks, measured, scene, stage):
    """Import the rig's bound geometry, bind the named clip to it, key the props and check the poses."""
    inputs, first, last = plan["inputs"], plan["frame_start"], plan["frame_end"]

    def record(check, ok, **values):
        checks[check] = {"ok": bool(ok), **values}

    def imported(path):
        before = set(bpy.data.objects)
        bpy.ops.import_scene.gltf(filepath=path, merge_vertices=False)
        return [obj for obj in bpy.data.objects if obj not in before]

    rig_objects = imported(inputs["rig"]["path"])
    armature, = [obj for obj in rig_objects if obj.type == "ARMATURE"]
    skinned, = [obj for obj in rig_objects if obj.type == "MESH" and obj.parent is armature]
    motion_objects = imported(inputs["motion"]["path"])
    clip, action = plan["clip"], None
    for obj in motion_objects:
        if obj.type == "ARMATURE" and obj.animation_data:
            for track in obj.animation_data.nla_tracks:
                for strip in track.strips:
                    if track.name == clip or (strip.action and strip.action.name.startswith(clip + "_")):
                        action = strip.action
    need(action is not None, "motion_clip_missing", {"clip": clip})
    action.use_fake_user = True
    for obj in motion_objects:   # the rig's bound geometry is the actor; the motion import lent its clip only
        bpy.data.objects.remove(obj, do_unlink=True)
    if armature.animation_data is None:
        armature.animation_data_create()
    for track in list(armature.animation_data.nla_tracks):
        armature.animation_data.nla_tracks.remove(track)
    armature.animation_data.action = action
    shift = plan.get("frame_shift", 1)   # glTF time starts at 0; the scene's first frame is 1
    for curve in action.fcurves:
        for key in curve.keyframe_points:
            key.co.x += shift
            key.handle_left.x += shift
            key.handle_right.x += shift
        curve.update()
    driven = {curve.data_path.split('"')[1] for curve in action.fcurves if curve.data_path.startswith("pose.bones")}
    need(driven and driven == set(armature.data.bones.keys()), "motion_rig_joint_mismatch",
         {"driven": len(driven), "rig_bones": len(armature.data.bones)})
    need(action.frame_range[0] <= first and action.frame_range[1] >= last, "clip_shorter_than_shot",
         {"clip_frames": list(action.frame_range), "shot": [first, last]})
    record("motion_clip", True, clip=clip, action=action.name, frame_range=list(action.frame_range), shift=shift)
    used = {node.image for material in skinned.data.materials if material and material.use_nodes
            for node in material.node_tree.nodes if node.type == "TEX_IMAGE" and node.image}
    empty = sorted(image.name for image in used if image.size[0] == 0 or image.size[1] == 0)
    record("blender_textures", bool(used) and not empty, images=sorted(image.name for image in used))
    need(used and not empty, "missing_texture", {"empty": empty})

    button, glass = scene.objects["PLAY Button"], scene.objects["CRT Screen"]
    markers, cues = plan["markers"], ABSENT
    if plan["interaction"] == "press":
        need("motion_report" in inputs, "motion_report_absent", "a press needs the motion's own contact frame")
        report = json.loads(Path(inputs["motion_report"]["path"]).read_bytes())
        need(isinstance(report.get("closest_hand_frame"), int), "motion_report_absent", "closest_hand_frame is not recorded")
        contact = plan.get("contact_frame_override") or report["closest_hand_frame"] + 1
        delta = contact - markers["HAND_CONTACT"]   # the SET cues follow the motion's contact
        cues = {"contact": contact, "bottomed": markers["BUTTON_BOTTOMED"] + delta,
                "released": markers["BUTTON_RELEASED"] + delta, "power_on": markers["RECOVERY_AND_SCREEN"] + delta}
        try:
            stage.key_interaction(button, glass, **cues)
        except ValueError as exc:
            raise Reject("interaction_frame_order", exc, cues) from exc
    else:
        hold_props(button, glass)
        if plan.get("force_screen_on_frame"):
            key_screen(glass, plan["force_screen_on_frame"], stage.SCREEN_STRENGTH, stage.LAST_FRAME)
    record("interaction", True, mode=plan["interaction"], cues=cues)

    armature.location += Vector(plan.get("actor_offset", (0.0, 0.0, 0.0)))
    cap = Vector(stage.CONTACT)

    def pose(frame):
        scene.frame_set(frame)
        evaluated = skinned.evaluated_get(bpy.context.evaluated_depsgraph_get())
        mesh = evaluated.to_mesh()
        points = [evaluated.matrix_world @ vertex.co for vertex in mesh.vertices]
        bounds = [[min(point[axis] for point in points), max(point[axis] for point in points)] for axis in range(3)]
        nearest = min((point - cap).length for point in points)
        evaluated.to_mesh_clear()
        corners = [Vector((x, y, z)) for x in bounds[0] for y in bounds[1] for z in bounds[2]]
        view = [world_to_camera_view(scene, scene.camera, corner) for corner in corners]
        inside = all(0 <= point.x <= 1 and 0 <= point.y <= 1 and point.z > 0 for point in view)
        return {"nearest_m": round(nearest, 5), "bbox_m": [[round(value, 3) for value in span] for span in bounds],
                "in_frame": inside}

    pose_frames = sorted(set(plan["sampled_frames"]) | ({cues["contact"], first} if cues != ABSENT else set()))
    poses = {frame: pose(frame) for frame in pose_frames}
    measured["poses"] = {str(frame): value for frame, value in poses.items()}
    if cues != ABSENT:
        near, far = poses[cues["contact"]]["nearest_m"], poses[first]["nearest_m"]
        record("cap_contact", near <= CAP_TOLERANCE_M and far >= FAR_M, at_contact_m=near, on_first_frame_m=far)
        need(checks["cap_contact"]["ok"], "cap_contact_absent")
    record("actor_in_frame", poses[last]["in_frame"], frame=last, bbox_m=poses[last]["bbox_m"])
    need(poses[last]["in_frame"], "actor_out_of_frame", checks["actor_in_frame"])


def render_and_check(plan, receipt, scene, stage):
    """Render the planned frames, then read the pixels back."""
    checks, measured, files = receipt["checks"], receipt["measured"], receipt["frame_files"]
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = int(plan["samples"])
    scene.render.resolution_x, scene.render.resolution_y = plan["resolution"]
    scene.render.resolution_percentage = 100
    scene.render.fps, scene.render.fps_base = plan["fps"], 1.0
    scene.render.threads_mode = "FIXED"
    scene.render.threads = int(plan["threads"])
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    first, last = plan["frame_start"], plan["frame_end"]
    frames = {"all": lambda: range(first, last + 1), "sampled": lambda: plan["sampled_frames"],
              "one": lambda: [plan["one_frame"]]}[plan["render"]]()
    out = Path(plan["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("frame_*.png"):
        stale.unlink()
    started_all = time.perf_counter()
    for frame in frames:
        floor()
        scene.frame_set(frame)
        scene.render.filepath = str(out / f"frame_{frame:04d}.png")
        started = time.perf_counter()
        bpy.ops.render.render(write_still=True)
        files.append({"frame": frame, "file": f"frames/frame_{frame:04d}.png", "sha256": sha_file(scene.render.filepath),
                      "seconds": round(time.perf_counter() - started, 3)})
    receipt["render_seconds"] = round(time.perf_counter() - started_all, 3)
    if plan["render"] == "one":
        return
    screen = world_to_camera_view(scene, scene.camera, Vector(stage.SCREEN_CENTER))

    def pixels(frame):
        image = bpy.data.images.load(str(out / f"frame_{frame:04d}.png"), check_existing=False)
        try:
            width, height = image.size
            values = image.pixels[:]
            step = 4 * 7
            spatial = [max(values[i:i + 3]) for i in range(0, len(values), step)]
            x, y = round(screen[0] * width), round(screen[1] * height)
            block = [values[4 * (row * width + column):4 * (row * width + column) + 3]
                     for row in range(y - 6, y + 7) for column in range(x - 8, x + 9)]
            mean = [sum(sample[channel] for sample in block) / len(block) for channel in range(3)]
            return {"size": [width, height], "span": round(max(spatial) - min(spatial), 6),
                    "peak": round(max(spatial), 6), "screen_rgb": [round(channel, 6) for channel in mean]}
        finally:
            bpy.data.images.remove(image)

    read = {item["frame"]: pixels(item["frame"]) for item in files}
    blank = [frame for frame, values in read.items() if values["span"] < PIXEL_SPAN or values["peak"] < PIXEL_MAX]
    checks["blank_frame"] = {"ok": not blank, "frames": len(read), "blank": blank[:8]}
    need(not blank, "blank_frame", checks["blank_frame"])
    greens = {frame: values["screen_rgb"][1] for frame, values in read.items()}
    measured["screen_green"] = {str(frame): green for frame, green in greens.items()}
    if plan.get("remove_actor"):
        return
    if plan["interaction"] == "press":
        power_on = receipt["checks"]["interaction"]["cues"]["power_on"]
        dark = greens[receipt["checks"]["interaction"]["cues"]["contact"]]
        early = sorted(frame for frame, green in greens.items() if frame >= power_on and green - dark < SCREEN_RESPONSE)
        late = sorted(frame for frame, green in greens.items() if frame < power_on and green - dark >= SCREEN_SILENCE)
        checks["screen_response"] = {"ok": not early and not late, "green_at_contact": dark, "green_after_power_on": greens.get(power_on),
                                     "unlit_after_power_on": early[:8], "lit_before_power_on": late[:8]}
        need(not early and not late, "screen_unlit_after_contact" if early else "screen_lit_before_contact")
    else:
        checks["screen_silent"] = {"ok": max(greens.values()) - min(greens.values()) < SCREEN_SILENCE and max(greens.values()) < 0.2,
                                   "green": {str(frame): green for frame, green in greens.items()}}
        need(checks["screen_silent"]["ok"], "screen_lit_without_press")


def hold_props(button, glass):
    """Without a press the cap stays at rest and the screen stays dark."""
    action = button.animation_data.action if button.animation_data else None
    curve = action.fcurves.find("location", index=1) if action else None
    if curve:
        action.fcurves.remove(curve)
    button.location.y = button["rest_y"]
    tree = glass.active_material.node_tree
    if tree.animation_data and tree.animation_data.action:
        for curve in list(tree.animation_data.action.fcurves):
            tree.animation_data.action.fcurves.remove(curve)
    tree.nodes["Principled BSDF"].inputs["Emission Strength"].default_value = 0


def key_screen(glass, frame, strength, last):
    """A control only: light the screen inside a recipe that presses nothing."""
    tree = glass.active_material.node_tree
    value = tree.nodes["Principled BSDF"].inputs["Emission Strength"]
    for at, level in ((1, 0.0), (frame - 1, 0.0), (frame, strength), (last, strength)):
        value.default_value = level
        value.keyframe_insert("default_value", frame=at)
    for curve in tree.animation_data.action.fcurves:
        for key in curve.keyframe_points:
            key.interpolation = "CONSTANT"


def main():
    plan_path = Path(sys.argv[sys.argv.index("--") + 1])
    plan = json.loads(plan_path.read_bytes())
    plan["out_dir"] = str(plan_path.parent / "frames")
    receipt = {"schema": "sn-c-reuse.build-receipt/v1", "recipe": plan["recipe"], "title": plan["title"],
               "control": plan.get("control", ABSENT), "render": plan["render"], "status": "RUNNING",
               "blender": bpy.app.version_string, "device": "CPU", "samples": plan["samples"],
               "resolution": plan["resolution"], "frame_range": [plan["frame_start"], plan["frame_end"]],
               "clip": plan["clip"], "interaction": plan["interaction"], "checks": {}, "measured": {}, "frame_files": []}
    started = time.perf_counter()
    try:
        build(plan, receipt)
        receipt["status"] = "PASS"
    except Reject as reject:
        receipt.update(status="REJECT", reason=reject.reason, detail=reject.detail)
        receipt["measured"].update(reject.measured)
    except Exception as exc:  # a harness fault keeps Blender's exit code 2
        receipt.update(status="FAULT", reason="harness_error", detail=f"{type(exc).__name__}: {exc}")
        write(plan_path, receipt)
        print(json.dumps({"reason": "harness_error", "detail": receipt["detail"]}), file=sys.stderr, flush=True)
        sys.exit(2)
    receipt["build_seconds"] = round(time.perf_counter() - started, 3)
    with open("/proc/self/status", encoding="ascii") as stream:
        receipt["peak_rss_kib"] = next(int(line.split()[1]) for line in stream if line.startswith("VmHWM:"))
    receipt["frames"] = len(receipt["frame_files"])
    receipt["frame_seconds"] = {"first": receipt["frame_files"][0]["seconds"] if receipt["frame_files"] else ABSENT,
                                "median": sorted(item["seconds"] for item in receipt["frame_files"])[len(receipt["frame_files"]) // 2]
                                if receipt["frame_files"] else ABSENT}
    middle = plan["sampled_frames"][len(plan["sampled_frames"]) // 2]
    receipt["replay_frame"] = plan["markers"].get("BUTTON_BOTTOMED", middle) if plan["interaction"] == "press" else middle
    write(plan_path, receipt)
    if receipt["status"] == "REJECT":
        print(json.dumps({"reason": receipt["reason"], "detail": receipt["detail"]}), file=sys.stderr, flush=True)
        sys.exit(1)
    sys.exit(0)


def write(plan_path, receipt):
    (plan_path.parent / "build.receipt.json").write_bytes(
        (json.dumps(receipt, sort_keys=True, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
