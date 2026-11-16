"""Blender-only side script: bpy and standard library, no host package imports."""
import base64
from contextlib import contextmanager
from decimal import Decimal, ROUND_HALF_EVEN
import json
import math
import os
import sys


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


def scene_state(bpy, paths=None):
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
    return {"frame_current": scene.frame_current, "objects": objects, "images": images}


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


def sample_scene(bpy, frames, paths, max_sample_frames=2000):
    if (type(frames) is not list or not 1 <= len(frames) <= max_sample_frames or
            any(type(frame) is not int for frame in frames)):
        raise ValueError("invalid_sample_frames")
    if type(paths) is not list or not paths or any(type(path) is not str or not path for path in paths):
        raise ValueError("invalid_sample_paths")
    samples = {}
    with preserve_settings(bpy):
        for frame in frames:
            bpy.context.scene.frame_set(frame)
            state = scene_state(bpy, paths)
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


def main():
    import bpy
    request_path, response_path = sys.argv[sys.argv.index("--") + 1:]
    with open(request_path, encoding="utf-8") as stream:
        request = json.load(stream)
    scene = bpy.context.scene
    mode = request["mode"]
    if mode == "init":
        # Saved on the scratch scene, so reopening cannot relabel fallback as scene.
        scene["vqa3d_camera_source"] = "scene" if scene.camera else "fallback"
        ensure_camera(bpy, scene)
    if mode == "act":
        apply(bpy, request["action"])
    if mode in ("init", "act"):
        bpy.context.preferences.filepaths.save_version = 0
        bpy.ops.wm.save_as_mainfile(filepath=bpy.data.filepath)
    result = {"ok": True, "runtime_version": bpy.app.version_string,
              "camera_source": scene.get("vqa3d_camera_source", "unknown") if scene.camera else "none",
              "camera_name": scene.camera.name if scene.camera else ""}
    if mode == "sample":
        result["samples"] = sample_scene(bpy, request["frames"], request["paths"], request.get("max_sample_frames", 2000))
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
    with open(response_path, "w", encoding="utf-8") as stream:
        json.dump(result, stream, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # A typed error survives subprocess failures; source traceback stays in the log.
        response_path = sys.argv[sys.argv.index("--") + 2]
        with open(response_path, "w", encoding="utf-8") as stream:
            json.dump({"ok": False, "error": str(exc), "error_type": type(exc).__name__}, stream)
        raise
