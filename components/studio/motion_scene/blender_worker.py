# SPDX-License-Identifier: AGPL-3.0-only
"""Background-only Blender entry for scene construction and evaluated evidence."""
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector
from mathutils.bvhtree import BVHTree
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import animation_polish as polish
from contact import cap_face, contact_clearance
import layout
import performance
import scope
import studio_set
from evidence import Rejected, atomic, checked_json, demand, encode, file_sha


def save(path):
    bpy.context.preferences.filepaths.save_version = 0
    bpy.context.scene.render.filepath = "//"
    bpy.ops.wm.save_as_mainfile(filepath=str(path), compress=True)


def set_scene(camera):
    scene = studio_set.build_scene()
    layout.add_cameras(scene, None if camera in ("wide", "authored") else camera)
    return scene


def stage_plunger(scene):
    """Give the gold plunger exposed sidewall throughout the registered stroke."""
    cap_scale, collar_lip_y = 1.34, .153

    cap = scene.objects["PLAY Button"]
    for vertex in cap.data.vertices:
        vertex.co.x *= cap_scale
        vertex.co.y *= cap_scale
        if vertex.co.z > 0.:
            vertex.co.z += .025
    collar = scene.objects["Button collar"]
    for vertex in collar.data.vertices:
        vertex.co.x = studio_set.CONTACT[0] + cap_scale * (vertex.co.x - studio_set.CONTACT[0])
        vertex.co.y = max(vertex.co.y, collar_lip_y)
        vertex.co.z = studio_set.CONTACT[2] + cap_scale * (vertex.co.z - studio_set.CONTACT[2])
    enclosure = scene.objects["Button enclosure"]
    enclosure.scale = Vector((1.23, 1., 1.34))
    cap.data.update()
    collar.data.update()


def interaction(scene, job):
    if job["mode"] == "keyed":
        stage_plunger(scene)
    button, glass = (scene.objects[name] for name in ("PLAY Button", "CRT Screen"))
    studio_set.key_interaction(button, glass, contact=polish.CONTACT, bottomed=polish.BOTTOM,
                              released=polish.RELEASE, power_on=polish.POWER, hold_end=job["hold"])
    for name, frame in (("CRT_POWER_ON", polish.POWER), ("PLAY_HOLD_END", job["hold"]),
                        ("SCREEN_REACTION", polish.RECOVER)):
        scene.timeline_markers.new(name, frame=frame)
    scene.frame_set(1)
    inverse = button.matrix_world.inverted()
    x, y, z = studio_set.CONTACT
    boundary = [(x + dx, y + polish.GLYPH_FACE_OFFSET_M, z + dz) for dx, dz in polish.GLYPH_XZ_M]
    mesh = bpy.data.meshes.new(polish.GLYPH)
    mesh.from_pydata([inverse @ Vector(point) for point in boundary], [], [(0, 1, 2)])
    mesh.update()
    icon = bpy.data.objects.new(polish.GLYPH, mesh)
    scene.collection.objects.link(icon)
    icon.parent = button
    mesh.materials.append(studio_set.material("PLAY | triangle ink", "141518", .54))
    bpy.data.objects.remove(scene.objects["PLAY lettering"], do_unlink=True)

    if job["camera"] == "authored":
        camera = scene.camera
        start = camera.location.copy()
        keyed = job["mode"] == "keyed"
        press = (2.28, -4.83, 2.03) if keyed else (4.563, -3.332, 1.55)
        press_lens = polish.PRESS_LENS_MM if keyed else 44
        recovery = (2.639, -4.997, 1.95) if keyed else polish.RECOVERY_CAMERA
        keys = [(1, start, 38), (108, start, 38)]
        if keyed:
            approach = (-3.15, -3.35, 1.82)
            keys = [(1, approach, 29), (98, approach, 29), (118, (2.55, -4.66, 2.06), 45)]
        keys.extend(((polish.ANTICIPATE, press, press_lens),
                     (199 if keyed else 188, press, press_lens),
                     (216, recovery, polish.RECOVERY_LENS_MM)))
        if not keyed:
            keys.extend(((220, recovery, polish.RECOVERY_LENS_MM), (238, (5.5, -2., 2.35), 48)))
        for frame, position, lens in keys:
            camera.location = position
            target = (-.17, .26, .65) if keyed and frame <= 98 else polish.CAMERA_TARGET
            camera.rotation_euler = (Vector(target) - camera.location).to_track_quat("-Z", "Y").to_euler()
            for prop in ("location", "rotation_euler"):
                camera.keyframe_insert(prop, frame=frame)
            camera.data.lens = lens
            camera.data.keyframe_insert("lens", frame=frame)
        for owner in (camera, camera.data):
            for curve in owner.animation_data.action.fcurves:
                for key in curve.keyframe_points:
                    key.interpolation = "BEZIER"
                    key.handle_left_type = key.handle_right_type = "AUTO_CLAMPED"
    scene.frame_set(1)


def bound(job, key):
    item = job[key]
    path = Path(item["path"])
    demand(path.is_file() and path.resolve() == path and file_sha(path) == item["sha256"],
           "input_digest_mismatch:" + key)
    return path


def actor(scene):
    armature, = (obj for obj in scene.objects if obj.type == "ARMATURE")
    mesh, = (obj for obj in scene.objects if obj.type == "MESH"
             and any(mod.type == "ARMATURE" and mod.object == armature for mod in obj.modifiers))
    return armature, mesh


def load_actor(scene, path, master):
    bpy.ops.import_scene.gltf(filepath=str(path), merge_vertices=False, import_pack_images=False)
    armature, _ = actor(scene)
    action, = (item for item in bpy.data.actions if item.name.startswith(master))
    demand(tuple(action.frame_range) == (0., scene.frame_end - 1.), "glb_clock_mismatch")
    for track in armature.animation_data.nla_tracks:
        track.mute = True
    armature.animation_data.action = action
    for curve in action.fcurves:
        for key in curve.keyframe_points:
            key.co.x += 1
            key.handle_left.x += 1
            key.handle_right.x += 1
        curve.update()


def visible_object(scene, depsgraph, point):
    origin = scene.camera.matrix_world.translation
    ray = point - origin
    uv = world_to_camera_view(scene, scene.camera, point)
    if uv.z <= 0 or not (0 < uv.x < 1 and 0 < uv.y < 1):
        return None
    hit = scene.ray_cast(depsgraph, origin, ray.normalized(), distance=ray.length + .006)
    return hit[4].name if hit[0] else None


def arm_coverage(scene, depsgraph, rig, mesh, evaluated, chain):
    groups = {group.index for group in mesh.vertex_groups if group.name in {f"bone_{i}" for i in chain}}
    verts = [evaluated.matrix_world @ v.co for v in evaluated.data.vertices]
    weights = [sum(group.weight for group in v.groups if group.group in groups)
               for v in evaluated.data.vertices]
    evaluated.data.calc_loop_triangles()
    faces = [tri.vertices[:] for tri in evaluated.data.loop_triangles
             if sum(weights[i] for i in tri.vertices) >= 1.5]
    demand(bool(faces), "striking_arm_surface_missing")
    arm = BVHTree.FromPolygons(verts, faces, all_triangles=True)
    cam = scene.camera
    origin, axes = cam.matrix_world.translation, cam.matrix_world.to_3x3()
    offsets = ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1))
    result = {}
    for bone in chain:
        name = f"bone_{bone}"
        center = rig.matrix_world @ rig.pose.bones[name].head
        uv = world_to_camera_view(scene, cam, center)
        visible = 0
        for x, y in offsets:
            ray = center + .045 * (axes @ Vector((x, y, 0))) - origin
            direction = ray.normalized()
            start = max(0., ray.length - .15)
            hit, _, _, _ = arm.ray_cast(origin + start * direction, direction, .30)
            if hit is None:
                continue
            depth = (hit - origin).length
            first = scene.ray_cast(depsgraph, origin, direction, distance=depth + .004)
            visible += first[0] and first[4].original == mesh and (first[1] - origin).length >= depth - .004
        result[name] = {"uv": [uv.x, uv.y], "visible_samples": visible}
    return result


def scene_samples(scene, job, motion):
    armature, mesh = actor(scene)
    root_bone = armature.pose.bones[f"bone_{polish.ROOT}"]
    wrist_bone = armature.pose.bones[f"bone_{job['arm'][-1]}"]
    button, screen, icon = (scene.objects[name] for name in ("PLAY Button", "CRT Screen", polish.GLYPH))
    strength = studio_set.emission_input(screen)
    face = Vector((0., 0., min(vertex.co.z for vertex in button.data.vertices)))
    indices = motion["hand_vertex_indices"]
    demand(len(set(indices)) == 4 and all(type(i) is int and 0 <= i < len(mesh.data.vertices)
                                         for i in indices), "polish_hand_marker_roster")
    scene.frame_set(1)
    armature.data.pose_position = "REST"
    bpy.context.view_layer.update()
    evaluated = mesh.evaluated_get(bpy.context.evaluated_depsgraph_get())
    demand(all((evaluated.matrix_world @ evaluated.data.vertices[i].co - Vector(expected)).length < .0001
               for i, expected in zip(indices, motion["hand_rest_world_m"], strict=True)),
           "polish_blender_hand_marker_order")
    armature.data.pose_position = "POSE"
    samples, sights = [], {}
    glyph_visible = False
    for frame in range(1, scene.frame_end + 1):
        if frame % 24 == 1:
            scope.checkpoint()
        scene.frame_set(frame)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = mesh.evaluated_get(depsgraph)
        palm = sum((evaluated.matrix_world @ evaluated.data.vertices[i].co for i in indices),
                   Vector((0., 0., 0.))) / len(indices)
        cap = button.matrix_world @ face
        root = armature.matrix_world @ root_bone.head
        wrist = armature.matrix_world @ wrist_bone.head
        samples.append({"frame": frame, "root_world_m": list(root), "cap_world_m": list(cap),
                        "screen_emission": strength.default_value, "hand_world_m": list(palm),
                        "wrist_world_m": list(wrist)})
        if frame in polish.SIGHT_FRAMES:
            cap_points = (cap + Vector((x, 0, z)) for x, z in polish.CAP_SIGHT_XZ_M)
            sights[str(frame)] = {
                "arm": arm_coverage(scene, depsgraph, armature, mesh, evaluated, job["arm"]),
                "cap_visible": any(visible_object(scene, depsgraph, point) in {button.name, icon.name}
                                   for point in cap_points),
                "screen_visible": visible_object(scene, depsgraph, Vector(studio_set.SCREEN_CENTER)) == screen.name,
            }
        if frame == polish.GLYPH_FRAME:
            glyph_visible = visible_object(scene, depsgraph, icon.matrix_world @ icon.data.polygons[0].center) == icon.name
    return samples, sights, glyph_visible


def compose(job, output):
    animation = bound(job, "animation")
    registration = checked_json(bound(job, "registration").read_bytes())
    motion = checked_json(bound(job, "motion_report").read_bytes())
    demand(motion["motion_sha256"] == job["animation"]["sha256"] and motion["status"] == "PASS"
           and motion["mode"] == job["mode"] and motion["registration_sha256"] == job["registration"]["sha256"],
           "polish_report_motion_digest_mismatch")
    scene = set_scene(job["camera"])
    interaction(scene, job)
    load_actor(scene, animation, job["master"])
    path = output / "scene.blend"
    scene.frame_set(1)
    bpy.ops.file.pack_all()
    save(path)
    bpy.ops.wm.open_mainfile(filepath=str(path), use_scripts=False)
    scene = bpy.context.scene
    samples, sights, glyph_visible = scene_samples(scene, job, motion)
    report = polish.assess_interaction(registration, motion, samples, sights, glyph_visible,
                                       arm=job["arm"], hold_end=job["hold"])
    demand(report["status"] == "PASS", ",".join(report["reasons"]))
    report.update(animation_sha256=job["animation"]["sha256"],
                  motion_report_sha256=job["motion_report"]["sha256"], blend_sha256=file_sha(path))
    atomic(output / "scene-report.json", encode(report))
    return {"mode": job["mode"], "camera": job["camera"], "inputs":
            {key: job[key]["sha256"] for key in ("animation", "registration", "motion_report")}}


def render(scene, frame, path):
    scope.checkpoint()
    scene.frame_set(frame)
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.use_denoising = False
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)


def pixels(file):
    image = bpy.data.images.load(str(file), check_existing=False)
    try:
        width, height = image.size[:]
        values = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(values)
        return values.reshape(height, width, 4)[:, :, :3]
    finally:
        bpy.data.images.remove(image)


def shadow_regions(scene, shape, boots, stand_feet):
    height, width = shape
    camera = scene.camera
    origin = camera.matrix_world.translation
    axes = camera.matrix_world.to_3x3()
    focal = camera.data.lens * width / camera.data.sensor_width
    depsgraph = bpy.context.evaluated_depsgraph_get()
    matrices = [[(obj.matrix_world.inverted(), dimensions) for obj, dimensions in objects]
                for objects in (boots, stand_feet)]
    regions = [np.zeros(shape, dtype=bool) for _ in matrices]

    def ring(ground, boxes):
        for inverse, (depth, width) in boxes:
            local = inverse @ ground
            distance = np.hypot(max(0., abs(local.x) - depth / 2), max(0., abs(local.y) - width / 2))
            if .005 < distance <= .040:
                return True
        return False

    for y in range(height):
        for x in range(width):
            direction = axes @ Vector(((x + .5 - width / 2) / focal, (y + .5 - height / 2) / focal, -1))
            if direction.z >= 0:
                continue
            ground = origin - origin.z / direction.z * direction
            in_regions = [ring(ground, boxes) for boxes in matrices]
            if not any(in_regions):
                continue
            hit = scene.ray_cast(depsgraph, origin, direction.normalized(), distance=(ground - origin).length + .004)
            if hit[0] and hit[4].name == "Floor":
                for region, inside in zip(regions, in_regions, strict=True):
                    region[y, x] = inside
    return regions


def shadow(scene, frame, output):
    """Compare near-sole and near-stand floor pixels across matched renders."""
    scene.frame_set(frame)
    scene.cycles.samples = 48
    actor_objects = [obj for obj in scene.objects if obj.get("studio_actor")]
    demand(bool(actor_objects), "shadow_actor_missing")
    boots = [(scene.objects[name], performance.SOLE) for name in ("L | boot sole", "R | boot sole")]
    stand_objects = [obj for obj in scene.objects if obj.name.startswith("Stand ")]
    stand = [(obj, (.078, .083)) for obj in stand_objects if obj.name.startswith("Stand foot")]
    path = output / "shadow-full.png"
    render(scene, frame, path)
    full = pixels(path)
    boot_mask, stand_mask = shadow_regions(scene, full.shape[:2], boots, stand)
    for obj in actor_objects:
        obj.hide_render = True
    path = output / "shadow-without-actor.png"
    render(scene, frame, path)
    without_actor = pixels(path)
    for obj in stand_objects:
        obj.hide_render = True
    path = output / "shadow-bare-floor.png"
    render(scene, frame, path)
    bare = pixels(path)
    return {"frame": frame, "boots": layout.contact_shadow(full, without_actor, boot_mask),
            "stand": layout.contact_shadow(without_actor, bare, stand_mask),
            "region_outside_contact_m": [.005, .040]}


def movie(scene, output):
    with TemporaryDirectory(prefix=".frames-", dir=output) as temporary:
        frames = Path(temporary)
        for frame in range(scene.frame_start, scene.frame_end + 1):
            render(scene, frame, frames / f"{frame:04d}.png")
        video = output / "clay.mp4"
        rate = scene.render.fps / scene.render.fps_base
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-framerate", str(rate),
                        "-start_number", str(scene.frame_start), "-i", str(frames / "%04d.png"),
                        "-an", "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p",
                        "-crf", "18", "-movflags", "+faststart", str(video)],
                       check=True, env=scope.environment(), timeout=180)
        demand(video.is_file() and video.stat().st_size > 0, "clay_movie_missing")


def evaluated_mesh(obj, depsgraph):
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.data
    mesh.calc_loop_triangles()
    points = np.array([evaluated.matrix_world @ vertex.co for vertex in mesh.vertices])
    triangles = np.array([triangle.vertices[:] for triangle in mesh.loop_triangles])
    return points, triangles


def cap_contact(scene, frame, axis):
    scene.frame_set(frame)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    cap = evaluated_mesh(scene.objects["PLAY cap"], depsgraph)
    points, triangles = evaluated_mesh(scene.objects["R pressure pad"], depsgraph)
    direction = np.asarray(axis)
    return contact_clearance(cap_face(*cap, direction), points[triangles], direction)


def read_clay(path, mode):
    bpy.ops.wm.open_mainfile(filepath=str(path), use_scripts=False)
    scene = bpy.context.scene
    demand(scene["curve_stage"] == mode, "clay_mode_mismatch")
    cap = scene.objects["PLAY Cap | front plane"]
    glass = scene.objects["CRT Screen"]
    strength = studio_set.emission_input(glass)
    rest, axis = Vector(scene["cap_rest_m"]), Vector(scene["cap_axis"])
    travel, on = [], []
    for frame in range(scene.frame_start, scene.frame_end + 1):
        scene.frame_set(frame)
        travel.append((cap.location - rest).dot(axis))
        if strength.default_value > .01:
            on.append(frame)
    peak = max(travel)
    if mode == "miss":
        demand(abs(peak) < 1e-5 and not on, "miss_triggered_play")
    else:
        demand(abs(peak - scene["cap_travel_m"]) < 1e-5 and on
               and on == list(range(on[0], scene.frame_end + 1))
               and abs(travel[on[0] - scene.frame_start] - peak) < 1e-5, "clay_press_sync")
    demand(abs(travel[0]) < 1e-5 and abs(travel[-1]) < 1e-5, "clay_cap_did_not_reset")
    contact = cap_contact(scene, on[0] if on else 195, axis)
    scene.frame_set(scene.frame_start)
    return scene, {"peak_travel_m": round(peak, 7), "screen_on_frame": on[0] if on else None,
                   "cap_contact": contact}


def clay(job, output):
    parts = performance.setup(job["camera"])
    metadata = performance.bake(parts, performance.character(), job["mode"], output / "clay.blend")
    scene, readback = read_clay(output / "clay.blend", job["mode"])
    if job["movie"]:
        movie(scene, output)
    if job["preview_frame"] is not None:
        render(scene, job["preview_frame"], output / "clay.png")
    if job["shadow_frame"] is not None:
        report = shadow(scene, job["shadow_frame"], output)
        report["blend_sha256"] = file_sha(output / "clay.blend")
        atomic(output / "shadow-report.json", encode(report))
    metadata.update(readback, camera=job["camera"], preview_frame=job["preview_frame"],
                    shadow_frame=job["shadow_frame"], movie=job["movie"])
    return metadata


def execute(job):
    output = Path(job["output"])
    match job["operation"]:
        case "layout":
            scene = set_scene(job["camera"])
            registration = studio_set.describe_scene(scene)
            atomic(output / "SET.json", encode(registration))
            save(output / "set.blend")
            return {"camera": job["camera"], "frames": scene.frame_end, "fps": scene.render.fps}
        case "clay":
            return clay(job, output)
        case "compose":
            return compose(job, output)
        case _:
            raise ValueError("unknown_blender_operation")


if __name__ == "__main__":
    job_path = Path(sys.argv[sys.argv.index("--") + 1])
    demand(bpy.app.background, "foreground_blender_refused")
    scope.effective(int(os.environ["MOTION_SCENE_SCOPED_NICE"]))
    try:
        data = execute(checked_json(job_path.read_bytes()))
        atomic(job_path.with_name("result.json"), encode(data))
    except Rejected as error:
        atomic(job_path.with_name("refusal.json"), encode(error.record()))
        raise
