"""Blender-only side script: bpy and standard library, no host package imports."""
import base64
import hashlib
from contextlib import contextmanager
from decimal import Decimal, ROUND_HALF_EVEN
import json
import math
import os
import sys
import traceback


def quantise(value, scale):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("nonfinite_state")
    return int((number * scale).to_integral_value(rounding=ROUND_HALF_EVEN))


def unit_quat_ppm(quaternion):
    """[w, x, y, z] of a unit quaternion in ppm, sign-canonical: q and -q encode the
    same rotation, so the leading nonzero quantised component is kept positive."""
    q = quaternion.normalized()
    row = [quantise(v, 1000000) for v in (q.w, q.x, q.y, q.z)]
    lead = next((v for v in row if v), 1)
    return [-v for v in row] if lead < 0 else row


def bone_rows(evaluated):
    """Pose-bone joints and orientations from the evaluated armature.

    quat_world_ppm: armature matrix_world @ pose_bone.matrix (bone +Y runs head to tail).
    quat_local_ppm: parent.matrix^-1 @ matrix; a root bone uses armature space.
    """
    rows = {}
    for bone in evaluated.pose.bones:
        local = (bone.parent.matrix.inverted_safe() @ bone.matrix) if bone.parent else bone.matrix
        rows[bone.name] = {
            "head_world_um": [quantise(v, 1000000) for v in evaluated.matrix_world @ bone.head],
            "tail_world_um": [quantise(v, 1000000) for v in evaluated.matrix_world @ bone.tail],
            "parent": bone.parent.name if bone.parent else "",
            "quat_world_ppm": unit_quat_ppm((evaluated.matrix_world @ bone.matrix).to_quaternion()),
            "quat_local_ppm": unit_quat_ppm(local.to_quaternion())}
    return rows


def skin_groups(obj, evaluated, mesh, points, weight_floor=0.5):
    """World bounds of evaluated vertices per armature vertex group (weight >= 0.5).

    Only armature-deformed meshes report this: it lets a unified character mesh
    expose its foot geometry. Groups without such vertices are omitted."""
    names = {group.index: group.name for group in obj.vertex_groups}
    bounds = {}
    for vertex, point in zip(mesh.vertices, points):
        for element in vertex.groups:
            name = names.get(element.group)
            if name is None or element.weight < weight_floor:
                continue
            row = bounds.get(name)
            if row is None:
                bounds[name] = [list(point), list(point), 1]
            else:
                for axis in range(3):
                    row[0][axis] = min(row[0][axis], point[axis])
                    row[1][axis] = max(row[1][axis], point[axis])
                row[2] += 1
    return {name: {"world_bbox_min_um": [quantise(v, 1000000) for v in low],
                   "world_bbox_max_um": [quantise(v, 1000000) for v in high], "verts": count}
            for name, (low, high, count) in sorted(bounds.items())}


def animation_paths(scene):
    """Declared animation channels and their evaluated transform dependencies."""
    fields = {}
    world, geometry = set(), set()
    local_fields = {"location": "location_um", "rotation_euler": "rotation_mdeg",
                    "rotation_quaternion": "rotation_mdeg", "rotation_axis_angle": "rotation_mdeg",
                    "scale": "scale_ppm", "hide_render": "hide_render",
                    "energy": "energy_mw", "color": "color_ppm"}

    def curves(owner):
        animation = getattr(owner, "animation_data", None)
        if animation is None:
            return []
        actions = [animation.action] if animation.action else []
        if getattr(animation, "use_nla", True):
            for track in getattr(animation, "nla_tracks", ()):
                if not track.mute:
                    actions.extend(strip.action for strip in track.strips if strip.action and not strip.mute)
        return [curve for action in actions for curve in getattr(action, "fcurves", ()) if not curve.mute] + [
            curve for curve in getattr(animation, "drivers", ()) if not curve.mute]

    def constrained(owner):
        return any(not c.mute and getattr(c, "influence", 1) != 0 for c in getattr(owner, "constraints", ()))

    objects = list(scene.objects)
    for obj in objects:
        driven = fields[obj.name] = set()
        for curve in curves(obj):
            path = curve.data_path
            if path in local_fields:
                driven.add(local_fields[path])
            elif path.startswith("pose.bones["):
                driven.add("bones")
            elif path.startswith(("modifiers[", "data.")):
                geometry.add(obj.name)
        for curve in curves(getattr(obj, "data", None)):
            if curve.data_path in local_fields:
                driven.add(local_fields[curve.data_path])
        if curves(getattr(getattr(obj, "data", None), "shape_keys", None)):
            geometry.add(obj.name)
        if any(constrained(b) for b in getattr(getattr(obj, "pose", None), "bones", ())):
            driven.add("bones")
        if driven & {"location_um", "rotation_mdeg", "scale_ppm"} or constrained(obj):
            world.add(obj.name)
        if "bones" in driven:
            geometry.add(obj.name)
    # Parent, target and deform chains can span several objects and rigs.
    for _ in objects:
        previous = (set(world), set(geometry))
        for obj in objects:
            if obj.parent and (obj.parent.name in world or obj.parent.name in geometry):
                world.add(obj.name)
            for constraint in getattr(obj, "constraints", ()):
                target = getattr(constraint, "target", None)
                if target and not constraint.mute and (target.name in world or target.name in geometry):
                    world.add(obj.name)
            for modifier in getattr(obj, "modifiers", ()):
                rig = getattr(modifier, "object", None)
                if modifier.type == "ARMATURE" and rig and (rig.name in geometry or rig.name in world):
                    geometry.add(obj.name)
            if obj.type == "ARMATURE" and obj.name in world:
                fields[obj.name].add("bones")
                geometry.add(obj.name)
        if previous == (world, geometry):
            break
    camera = getattr(scene, "camera", None)
    result = []
    for obj in objects:
        driven = fields[obj.name]
        if obj.name in world:
            driven.update({"world_location_um", "world_rotation_mdeg", "world_scale_ppm", "bone_relative_um"})
        if obj.name in world or obj.name in geometry:
            driven.update({"world_bbox_min_um", "world_bbox_max_um", "vertex_groups", "view_depth_um", "view_ppm"})
        if camera and getattr(camera, "name", None) in world:
            driven.update({"view_depth_um", "view_ppm"})
        name = obj.name.replace("~", "~0").replace("/", "~1")
        result.extend("/objects/" + name + "/" + field for field in driven)
    return sorted(result)


def scene_state(bpy, paths=None, contact_feet=None, floor=None):
    from mathutils import Vector
    from bpy_extras.object_utils import world_to_camera_view
    scene = bpy.context.scene
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    names = None
    if paths is not None:
        parts = [path_parts(p) for p in paths]
        if ["objects"] not in parts:
            names = {p[1] for p in parts if len(p) > 1 and p[0] == "objects"}
    if names is not None and contact_feet:
        names.update(path_parts(path)[1] for foot in contact_feet for path in foot["z"])
        names.update(rig for foot in contact_feet for rig,bone in foot["bones"])
    objects = {}
    for obj in sorted(scene.objects, key=lambda o: o.name):
        if obj.get("vqa3d_fallback_camera", False):
            continue
        if names is not None and obj.name not in names:
            continue
        evaluated = obj.evaluated_get(depsgraph)
        location, rotation, scale = evaluated.matrix_world.decompose()
        row = {"type": obj.type,
            "location_um": [quantise(v, 1000000) for v in obj.location],
            "rotation_mdeg": [quantise(Decimal(str(v)) * 180 / Decimal(str(math.pi)), 1000) for v in obj.rotation_euler],
            "scale_ppm": [quantise(v, 1000000) for v in obj.scale],
            "hide_render": int(obj.hide_render),
            "parent": obj.parent.name if obj.parent else "", "parent_type": obj.parent_type,
            "parent_bone": obj.parent_bone,
            "world_location_um": [quantise(v, 1000000) for v in location],
            "world_rotation_mdeg": [quantise(math.degrees(v), 1000) for v in rotation.to_euler()],
            "world_scale_ppm": [quantise(v, 1000000) for v in scale]}
        # Author declarations used by profiles: vqa3d_role custom property and collections.
        role = obj.get("vqa3d_role", "")
        row["role"] = role if type(role) is str else ""
        attach = obj.get("vqa3d_attach", "")
        if type(attach) is not str or attach not in {"", "rigid", "grip"}:
            raise ValueError("invalid_vqa3d_attach:" + obj.name)
        row["attach"] = attach
        grip = obj.get("vqa3d_grip_point", None)
        if grip is not None:
            row["grip_point_um"] = [quantise(v, 1000000) for v in grip]
        if obj.parent and obj.parent_type == "BONE" and obj.parent_bone and hasattr(obj.parent, "evaluated_get"):
            rig = obj.parent.evaluated_get(depsgraph)
            bone = rig.pose.bones.get(obj.parent_bone)
            if bone:
                relative = (rig.matrix_world @ bone.matrix).inverted_safe() @ evaluated.matrix_world
                row["bone_relative_um"] = [quantise(v, 1000000) for v in relative.translation]

        row["collections"] = sorted(collection.name for collection in obj.users_collection)
        if obj.type == "LIGHT":
            row.update(energy_mw=quantise(obj.data.energy, 1000),
                       color_ppm=[quantise(v, 1000000) for v in obj.data.color],
                       light_type=obj.data.type)
        if obj.type == "ARMATURE":
            row["bones"] = bone_rows(evaluated)
        bbox = None
        if obj.type == "MESH":
            mesh = evaluated.to_mesh()
            try:
                mesh.calc_loop_triangles()
                row["tris"] = len(mesh.loop_triangles)
                points = [evaluated.matrix_world @ vertex.co for vertex in mesh.vertices]
                if points:
                    low = [min(v[i] for v in points) for i in range(3)]
                    high = [max(v[i] for v in points) for i in range(3)]
                    row["world_bbox_min_um"] = [quantise(v, 1000000) for v in low]
                    row["world_bbox_max_um"] = [quantise(v, 1000000) for v in high]
                    bbox = [Vector((x, y, z)) for x in (low[0], high[0])
                            for y in (low[1], high[1]) for z in (low[2], high[2])]
                skins = [m.object.name for m in obj.modifiers if m.type == "ARMATURE" and m.object]
                if skins:
                    row["skin_armature"] = skins[0]
                    row["vertex_groups"] = skin_groups(obj, evaluated, mesh, points)
            finally:
                evaluated.to_mesh_clear()
        elif hasattr(evaluated, "bound_box") and any(tuple(v) != (-1, -1, -1) for v in evaluated.bound_box):
            bbox = [evaluated.matrix_world @ Vector(v) for v in evaluated.bound_box]
        if bbox and scene.camera:
            clipped = clip_bbox(bbox, scene.camera)
            visible = [world_to_camera_view(scene, scene.camera, point) for point in clipped]
            row["view_depth_um"] = quantise(sum(p.z for p in visible) / len(visible), 1000000) if visible else 0
            row["view_ppm"] = projected_fraction(visible)
        objects[obj.name] = row
    images = {image.name: {"size_px": [int(v) for v in image.size]}
              for image in bpy.data.images if image.type != "RENDER_RESULT"}
    result = {"frame_current": scene.frame_current, "objects": objects, "images": images,
              "animation_paths": animation_paths(scene)}
    if contact_feet:
        result["contacts"] = {foot["label"]: contact_floor(bpy, result, foot, floor) for foot in contact_feet}
    return result


def clip_bbox(points, camera):
    """Clip bbox edge segments to the camera's near/far slab before projection."""
    inverse = camera.matrix_world.inverted()
    depths = [-(inverse @ point).z for point in points]
    near, far = camera.data.clip_start, camera.data.clip_end
    clipped = [p for p, depth in zip(points, depths) if near <= depth <= far]
    # Corner ordering can differ (mesh world AABB versus Blender bound_box).
    # Every corner pair includes all true edges; extra diagonals stay inside
    # the convex box and cannot enlarge its projected extrema.
    for i, a in enumerate(points):
        for j in range(i+1, len(points)):
            da, db = depths[i], depths[j]
            if da == db:
                continue
            for plane in (near, far):
                t = (plane-da)/(db-da)
                if 0 <= t <= 1:
                    clipped.append(a.lerp(points[j], t))
    return clipped


def projected_fraction(points):
    if not points:
        return 0
    left, right = min(p.x for p in points), max(p.x for p in points)
    bottom, top = min(p.y for p in points), max(p.y for p in points)
    area = (right-left) * (top-bottom)
    clipped = max(0, min(1, right)-max(0, left)) * max(0, min(1, top)-max(0, bottom))
    return min(1000000, max(0, quantise(clipped / area, 1000000))) if area > 0 else 0


def path_parts(path):
    if path.startswith("objects."):
        raise ValueError("blender_object_paths_require_json_pointer")
    return ([p.replace("~1", "/").replace("~0", "~") for p in path[1:].split("/")]
            if path.startswith("/") else path.split("."))


def lookup(state, path):
    for key in path_parts(path):
        if type(state) is list:
            if not key.isascii() or not key.isdigit() or str(int(key)) != key:
                raise ValueError("noncanonical_list_index:" + key)
            state = state[int(key)]
        else:
            state = state[key]
    return state


def apply(bpy, action):
    if type(action) is dict and action.get("op") == "set_frame":
        if set(action) != {"op", "frame"} or type(action["frame"]) is not int:
            raise ValueError("invalid_set_frame")
        bpy.context.scene.frame_set(action["frame"])
        bpy.context.view_layer.update()
        return
    if type(action) is not dict or action.get("op") not in ("set_location", "set_rotation", "set_scale", "hide"):
        raise ValueError("unsupported_action_op:" + str(action.get("op") if type(action) is dict else None))
    obj = bpy.data.objects.get(action.get("object", ""))
    if obj is None:
        raise ValueError("object_not_found:" + str(action.get("object", "")))
    field = {"set_location": "value_um", "set_rotation": "value_mdeg",
             "set_scale": "value_ppm", "hide": "value"}[action["op"]]
    if set(action) != {"op", "object", field}:
        raise ValueError("invalid_blender_action_fields")
    value = action.get(field)
    if action["op"] == "hide":
        if type(value) is not int or value not in (0, 1):
            raise ValueError("hide_requires_zero_one")
        obj.hide_render = bool(value)
    else:
        if type(value) is not list or len(value) != 3 or not all(type(v) is int for v in value):
            raise ValueError("transform_requires_integer_vector")
        value = ([math.radians(v / 1000) for v in value] if action["op"] == "set_rotation"
                 else [v / 1000000 for v in value])
        attr = {"set_location": "location", "set_rotation": "rotation_euler", "set_scale": "scale"}[action["op"]]
        setattr(obj, attr, value)
    bpy.context.view_layer.update()


def ensure_camera(bpy, scene):
    """Preserve a source scene's camera; configure only our fallback."""
    if scene.camera is None:
        camera = bpy.data.objects.new("VQA3D_Camera", bpy.data.cameras.new("VQA3D_Camera"))
        scene.collection.objects.link(camera)
        scene.camera = camera
        camera["vqa3d_fallback_camera"] = True
        camera.location = (5, -7, 5)
        camera.rotation_euler = (math.radians(60), 0, math.radians(35))
        camera.data.type = "ORTHO"
        camera.data.ortho_scale = 8


@contextmanager
def preserve_settings(bpy):
    """Restore every field touched by probes, capture and sampling, on errors too."""
    scene = bpy.context.scene
    saved = []
    groups = [(getattr(scene, "render", None),
               ("engine", "resolution_x", "resolution_y", "resolution_percentage", "filepath",
                "use_file_extension", "film_transparent")),
              (getattr(getattr(scene, "render", None), "image_settings", None),
               ("file_format", "color_mode", "color_depth")),
              (getattr(scene, "cycles", None), ("device", "samples", "use_denoising", "denoiser")),
              (getattr(scene, "eevee", None), ("taa_render_samples",)),
              (getattr(scene, "view_settings", None), ("view_transform", "look")),
              (getattr(getattr(scene, "display", None), "shading", None),
               ("light", "color_type", "show_shadows", "show_cavity"))]
    for owner, keys in groups:
        for key in keys:
            if owner is not None and hasattr(owner, key):
                saved.append((owner, key, getattr(owner, key)))
    addon = bpy.context.preferences.addons.get("cycles")
    prefs = addon.preferences if addon else None
    if prefs is not None and hasattr(prefs, "compute_device_type"):
        saved.append((prefs, "compute_device_type", prefs.compute_device_type))
    devices = [(d, d.use) for d in getattr(prefs, "devices", ()) if hasattr(d, "use")]
    original_frame = getattr(scene, "frame_current", None)
    try:
        yield
    finally:
        # Restore dependencies first: engine before Cycles settings, transform before look.
        for owner, key, value in saved:
            if getattr(owner, key) != value:
                setattr(owner, key, value)
        for device, use in devices:
            device.use = use
        if original_frame is not None and scene.frame_current != original_frame:
            scene.frame_set(original_frame)
            bpy.context.view_layer.update()


def enum_ids(owner, key):
    import ast
    missing = object()
    original = getattr(owner, key, missing)
    try:
        try:
            setattr(owner, key, "__VQA3D_INVALID_ENUM__")
        except (TypeError, ValueError) as exc:
            message = str(exc)
            if "not found in" in message:
                try:
                    items = ast.literal_eval(message.split("not found in", 1)[1].strip())
                    if type(items) in (tuple, list) and all(type(v) is str for v in items):
                        return list(items)
                except (ValueError, SyntaxError):
                    pass
        return [item.identifier for item in owner.bl_rna.properties[key].enum_items]
    finally:
        if original is not missing:
            if getattr(owner, key) != original:
                setattr(owner, key, original)
        elif hasattr(owner, key):
            delattr(owner, key)


def render_capabilities(bpy):
    scene = bpy.context.scene
    with preserve_settings(bpy):
        engines = enum_ids(scene.render, "engine")
        transforms = enum_ids(scene.view_settings, "view_transform")
        looks = {}
        for transform in transforms:
            scene.view_settings.view_transform = transform
            looks[transform] = enum_ids(scene.view_settings, "look")
        devices, denoisers = {}, []
        addon = bpy.context.preferences.addons.get("cycles")
        if addon:
            prefs = addon.preferences
            for device_type in enum_ids(prefs, "compute_device_type"):
                try:
                    prefs.compute_device_type = device_type
                    prefs.refresh_devices()
                    for device in prefs.devices:
                        devices.setdefault(device.type, [])
                        if device.name not in devices[device.type]:
                            devices[device.type].append(device.name)
                except Exception:
                    continue
            denoisers = enum_ids(scene.cycles, "denoiser")
        return {"engines": engines, "cycles_devices": devices, "denoisers": denoisers,
                "view_transforms": transforms, "looks": looks}


def gpu_probe(bpy, device_type, *, width=8, height=8, samples=1):
    scene = bpy.context.scene
    with preserve_settings(bpy):
        prefs = bpy.context.preferences.addons["cycles"].preferences
        prefs.compute_device_type = device_type
        prefs.refresh_devices()
        devices = [(d, d.use) for d in prefs.devices]
        try:
            selected = [d for d in prefs.devices if d.type == device_type and d.type != "CPU"]
            if not selected:
                raise RuntimeError("no_cycles_gpu_device:" + device_type)
            for device in prefs.devices:
                device.use = device in selected
            scene.render.engine = "CYCLES"
            scene.cycles.device = "GPU"
            scene.cycles.samples = samples
            scene.cycles.use_denoising = False
            scene.render.resolution_x, scene.render.resolution_y = width, height
            scene.render.resolution_percentage = 100
            bpy.ops.render.render()
            return True
        finally:
            for device, use in devices:
                device.use = use


def sample_scene(bpy, frames, paths, max_sample_frames=2000, contact_feet=None, floor=None):
    if (type(frames) is not list or not 1 <= len(frames) <= max_sample_frames or
            any(type(frame) is not int for frame in frames)):
        raise ValueError("invalid_sample_frames")
    if type(paths) is not list or not paths or any(type(path) is not str or not path for path in paths):
        raise ValueError("invalid_sample_paths")
    samples = {}
    with preserve_settings(bpy):
        for frame in frames:
            bpy.context.scene.frame_set(frame)
            state = scene_state(bpy, paths, contact_feet, floor)
            row = {}
            for path in paths:
                try:
                    row[path] = lookup(state, path)
                except (KeyError, IndexError):
                    # Missing entries remain absent; the host emits sample_state_key_missing.
                    pass
            samples[str(frame)] = row
    return samples


def configure_capture(bpy, request):
    scene = bpy.context.scene
    engine = request.get("capture_engine", "workbench")
    scene.render.engine = {"workbench": "BLENDER_WORKBENCH", "eevee": "BLENDER_EEVEE",
                           "cycles": "CYCLES"}[engine]
    samples = request.get("capture_samples", 1)
    if engine == "cycles":
        scene.cycles.device = "CPU"
        scene.cycles.samples = samples
        scene.cycles.use_denoising = request.get("capture_denoising", False)
    elif engine == "eevee":
        scene.eevee.taa_render_samples = samples
    scene.render.resolution_x, scene.render.resolution_y = request["width"], request["height"]
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = request.get("color_mode", "RGB")
    scene.render.image_settings.color_depth = request.get("color_depth", "8")
    scene.render.use_file_extension = False
    scene.render.film_transparent = False
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "OBJECT"
    scene.display.shading.show_shadows = True
    scene.display.shading.show_cavity = True
    return [json.dumps({"capture_engine": engine, "capture_samples": samples}, sort_keys=True, separators=(",", ":"))]


def source_metadata(bpy):
    s = bpy.context.scene
    keys = set()
    for action in bpy.data.actions:
        for curve in getattr(action, "fcurves", ()):
            keys.update(round(p.co.x) for p in curve.keyframe_points if s.frame_start <= p.co.x <= s.frame_end)
    for obj in s.objects:
        animation = obj.animation_data
        if animation is None:
            continue
        for track in animation.nla_tracks:
            for strip in track.strips:
                keys.update(round(v) for v in (strip.frame_start, strip.frame_end) if s.frame_start <= v <= s.frame_end)
                if strip.action:
                    duration = strip.action_frame_end-strip.action_frame_start
                    for curve in getattr(strip.action, "fcurves", ()):
                        for point in curve.keyframe_points:
                            for repeat in range(math.ceil(strip.repeat)):
                                frame = strip.frame_start+(point.co.x-strip.action_frame_start+repeat*duration)*strip.scale
                                if strip.frame_start <= frame <= strip.frame_end and s.frame_start <= frame <= s.frame_end:
                                    keys.add(round(frame))
    return {"frame_start": s.frame_start, "frame_end": s.frame_end, "keyframes": sorted(keys),
            "source_camera": s.camera.name if s.camera else "",
            "render": {"engine": s.render.engine, "samples": s.cycles.samples,
                       "view_transform": s.view_settings.view_transform, "look": s.view_settings.look,
                       "motion_blur": int(s.render.use_motion_blur), "resolution_pct": s.render.resolution_percentage,
                       "denoising": int(s.cycles.use_denoising), "device": s.cycles.device, "fps": s.render.fps}}


def declared_floor_at(objects, point, declaration):
    if declaration and "z_um" in declaration:
        return {"object": "declared_z", "z_um": declaration["z_um"], "method": "declared"}
    candidates = []
    for name, row in objects.items():
        selected = (row.get("role") == "floor" or declaration and
                    (declaration.get("object") == name or declaration.get("collection") in row.get("collections", [])))
        low, high = row.get("world_bbox_min_um"), row.get("world_bbox_max_um")
        if selected and low and high and all(low[i] <= point[i] <= high[i] for i in (0, 1)) and high[2] <= point[2]:
            candidates.append((high[2], name))
    if candidates:
        z, name = max(candidates)
        return {"object": name, "z_um": z, "method": "declared_bbox"}
    return {"object": "", "method": "unresolved"}


FOOTPRINT_QUARTERS = (1, 2, 3)  # 3 x 3 rays at the quarter points of the foot's world bbox


def contact_floor(bpy, state, foot, declaration=None):
    """Surface under one foot: the highest set surface hit by a grid of downward rays.

    One centre ray falls through a joint between paving stones and reports the
    sub-base below, so the footprint is sampled at 3 x 3 interior points and the
    highest hit wins. Meshes that belong to any character (parented under or
    deformed by an armature) are never a floor unless declared as one.
    """
    from mathutils import Vector
    scene = bpy.context.scene
    objects = state["objects"]
    if declaration and "z_um" in declaration:
        return declared_floor_at(objects, [0, 0, 0], declaration)
    # The upper sole bounds put the rays above shallow penetrations while staying
    # below a raised platform. The measured clearance uses the lowest sole vertex.
    path = foot["z"][0]
    parts = path_parts(path)
    if "world_bbox_min_um" in parts:
        prefix = parts[:parts.index("world_bbox_min_um")]
        row = state
        for part in prefix:
            row = row[part]
        low, high = row["world_bbox_min_um"], row["world_bbox_max_um"]
        point = [(low[0]+high[0])//2, (low[1]+high[1])//2, high[2]]
        footprint = [[low[0]+(high[0]-low[0])*i//4, low[1]+(high[1]-low[1])*j//4]
                     for i in FOOTPRINT_QUARTERS for j in FOOTPRINT_QUARTERS]
    else:
        vector_path = path.rsplit("/", 1)[0]
        point = list(lookup(state, vector_path))
        footprint = [point[:2]]
    for rig, bone in foot["bones"]:
        head = objects.get(rig, {}).get("bones", {}).get(bone, {}).get("head_world_um")
        if head:
            point[2] = max(point[2], head[2])
    own_rigs = {rig for rig, bone in foot["bones"]}

    def is_declared(obj):
        get = getattr(obj, "get", None)
        if callable(get) and get("vqa3d_role", "") == "floor":
            return True
        return bool(declaration) and (declaration.get("object") == obj.name or declaration.get("collection") in [
            c.name for c in getattr(obj, "users_collection", ())])

    def is_character(obj):
        parent = obj
        while parent:
            if parent.name in own_rigs or getattr(parent, "type", "") == "ARMATURE":
                return True
            parent = parent.parent
        return any(m.type == "ARMATURE" and m.object for m in obj.modifiers)
    excluded = {obj.name for obj in scene.objects if is_character(obj) and not is_declared(obj)}
    graph = bpy.context.evaluated_depsgraph_get()
    best = None
    for x, y in footprint:
        origin = Vector([x/1000000, y/1000000, point[2]/1000000])
        origin.z += 0.00001
        for _ in range(128):
            hit, location, normal, index, obj, matrix = scene.ray_cast(graph, origin, Vector((0, 0, -1)), distance=10000)
            if not hit:
                break
            if obj.name not in excluded and (not declaration or is_declared(obj)):
                candidate = (quantise(location.z, 1000000), obj.name)
                # Highest surface under the footprint; the name breaks ties deterministically.
                if best is None or candidate[0] > best[0] or (candidate[0] == best[0] and candidate[1] < best[1]):
                    best = candidate
                break
            origin = location - Vector((0, 0, 0.0001))
    if best is not None:
        return {"object": best[1], "z_um": best[0], "method": "ray_cast", "rays": len(footprint)}
    # Fallback considers declarations, never a floor-shaped name.
    return declared_floor_at(objects, point, declaration)


def execute(bpy, request):
    scene = bpy.context.scene
    mode = request["mode"]
    metadata = source_metadata(bpy) if mode == "init" else None
    if mode == "init":
        # Saved on the scratch scene, so reopening cannot relabel fallback as scene.
        scene["vqa3d_camera_source"] = "scene" if scene.camera else "fallback"
        ensure_camera(bpy, scene)
    if mode == "act":
        apply(bpy, request["action"])
    if mode in ("init", "act") and not request.get("session"):
        bpy.context.preferences.filepaths.save_version = 0
        bpy.ops.wm.save_as_mainfile(filepath=bpy.data.filepath)
    result = {"ok": True, "runtime_version": bpy.app.version_string,
              "camera_source": scene.get("vqa3d_camera_source", "unknown") if scene.camera else "none",
              "camera_name": scene.camera.name if scene.camera else ""}
    if mode == "sample":
        result["samples"] = sample_scene(bpy, request["frames"], request["paths"], request.get("max_sample_frames", 2000), request.get("contact_feet"), request.get("floor"))
    elif mode == "render_caps":
        result["render_capabilities"] = render_capabilities(bpy)
    elif mode == "gpu_probe":
        result["gpu_render_ok"] = gpu_probe(bpy, request["device_type"], width=request.get("probe_width", 8),
                                            height=request.get("probe_height", 8), samples=request.get("probe_samples", 1))
    elif mode != "capture":
        result["state"] = scene_state(bpy)
    if mode == "capture":
        result["notes"] = configure_capture(bpy, request)
        bpy.ops.render.render()
        # save_render writes into an anonymous RAM file: no PNG disk artifact.
        fd = os.memfd_create("vqa3d-frame.png")
        try:
            bpy.data.images["Render Result"].save_render("/proc/self/fd/" + str(fd), scene=scene)
            os.lseek(fd, 0, os.SEEK_SET)
            with os.fdopen(os.dup(fd), "rb") as stream:
                result["frame_png_base64"] = base64.b64encode(stream.read()).decode("ascii")
        finally:
            os.close(fd)
        # Read evaluated geometry after Workbench has finished consuming the
        # depsgraph; avoid clearing temporary evaluated meshes before rendering.
        result["state"] = scene_state(bpy)
    if metadata is not None:
        result["profile_metadata"] = metadata
    return result


def main():
    import bpy
    request_path, response_path = sys.argv[sys.argv.index("--") + 1:]
    with open(request_path, encoding="utf-8") as stream:
        request = json.load(stream)
    session = request.get("session", False)
    while True:
        environment = {key: {"sha256": hashlib.sha256(os.environ[key].encode()).hexdigest(),
                             **({"path": os.environ[key]} if key.endswith(("_PATH", "_DIR")) and os.path.isabs(os.environ[key]) else {})}
                       for key in request.get("environment_keys", []) if key in os.environ}
        if request["mode"] in {"init", "gpu_probe"}:
            print("VQA3D_ENV:" + json.dumps(environment, sort_keys=True), flush=True)
        try:
            result = execute(bpy, request)
        except Exception as exc:
            result = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
            traceback.print_exc()
        result["environment"] = environment
        with open(response_path + ".tmp", "w", encoding="utf-8") as stream:
            json.dump(result, stream, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        os.replace(response_path + ".tmp", response_path)
        if not session:
            break
        line = sys.stdin.readline()
        if not line:
            break
        request = json.loads(line)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # A typed error survives subprocess failures; source traceback stays in the log.
        response_path = sys.argv[sys.argv.index("--") + 2]
        with open(response_path, "w", encoding="utf-8") as stream:
            json.dump({"ok": False, "error": str(exc), "error_type": type(exc).__name__}, stream)
        raise
