"""Build and reopen a local procedural fixture using the af.face_fit arguments.

The reader is owned by the caller and authorizes each input by role and digest.
These functions execute inside an already scoped background CPU Blender.
"""
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from asset_factory import core, domains
from tools.input.evidence import InputIssue, bytes_digest, canonical_bytes, checked_record, require
from tools.blender.uv_quality import raster_size
from .blender_fixture import decoded_rgb, install_camera, make_scene, render
from .controls import bind_controls, reopened_samples
from .evaluation import CAMERA_POLICY, I02_DECISION, evaluate_frames, heldout, projection_record
from .fit import fit_request, read_inputs


def build(args, reader, filename):
    import bpy
    filename = Path(filename)
    require(not filename.exists(), 4, "fixture_output_exists")
    mesh, registration, _ = read_inputs(args, reader)
    vertices, scales = fit_request(args, reader)
    scene, rig = make_scene(mesh, vertices)
    track = checked_record(reader, "face-viseme-track", registration["track_pin"])
    require(0 < len(track["frames"]) <= 32, 6, "fixture_render_frame_budget")
    reference, _ = heldout(args, reader, "")
    size = reference["front"]["size"]
    scene.render.resolution_x, scene.render.resolution_y = raster_size(size)
    for name, view in reference.items():
        install_camera(scene, name, view["camera"])
    scene.camera = scene.objects["front"]
    scene.render.fps, scene.render.fps_base = track["fps"]
    scene.frame_start, scene.frame_end = 1, len(track["frames"])
    bind_controls(rig, mesh["parts"], scene, track)
    scene.frame_set(1)
    scene["face_request"] = core.canonical_json(args)
    scene["face_scales"] = core.canonical_json(scales)
    with TemporaryDirectory(dir=filename.parent) as staging:
        staged = Path(staging) / "head.blend"
        result = bpy.ops.wm.save_as_mainfile(filepath=str(staged), copy=True)
        require(result == {"FINISHED"}, 3, "fixture_scene_write_failed")
        try:
            os.link(staged, filename)
        except FileExistsError as exc:
            raise InputIssue(4, "fixture_output_exists") from exc
    return scales


def replay_source_projection(reader, registration, views):
    """Project the authored pose geometry through cameras loaded from the pinned source bytes."""
    import bpy
    from bpy_extras.object_utils import world_to_camera_view

    source_pin = "sha256:" + registration["source_sha256"]
    raw = reader("face-reference-scene", source_pin)
    require(type(raw) is bytes and bytes_digest(raw) == source_pin, 4, "face_reference_scene_drift")
    witness = checked_record(reader, "face-independent-observations", registration["heldout_pin"])
    with TemporaryDirectory() as directory:
        path = Path(directory) / "source.blend"
        path.write_bytes(raw)
        with bpy.data.libraries.load(str(path), link=True) as (available, loaded):
            require(len(available.scenes) == 1, 3, "source_scene_coverage")
            loaded.scenes = list(available.scenes)
        source = loaded.scenes[0]
        library = source.library
        try:
            rig = source.objects.get("FixtureHead")
            require(rig is not None and rig.data.shape_keys is not None, 3, "source_pose_coverage")
            poses = rig.data.shape_keys.key_blocks
            observed = {}
            for name, view in views.items():
                camera = source.objects.get("held-" + name)
                require(camera is not None, 3, "source_camera_missing")
                width, height = raster_size(view["size"])
                require((source.render.resolution_x, source.render.resolution_y) == (width, height)
                        and source.render.resolution_percentage == 100, 4, "source_raster_drift")
                observed[name] = []
                for index in range(len(view["frame_landmarks"])):
                    pose = poses.get(f"authored-{index}")
                    require(pose is not None, 3, "source_pose_coverage")
                    marks = []
                    for item in view["landmarks"]:
                        vertex = item["vertex"]
                        require(vertex < len(pose.data), 4, "source_vertex_drift")
                        pos = rig.matrix_world @ pose.data[vertex].co
                        ndc = world_to_camera_view(source, camera, pos)
                        require(ndc.z > 0, 5, "source_landmark_behind_camera")
                        x = round(ndc.x * width * domains.MILLIPX_PER_PIXEL)
                        y = round((1-ndc.y) * height * domains.MILLIPX_PER_PIXEL)
                        marks.append({"vertex": vertex, "xy_millipx": [domains.pair(x), domains.pair(y)]})
                    observed[name].append(marks)
        finally:
            bpy.data.libraries.remove(library)
    record = projection_record(registration["source_sha256"], registration["track_pin"], observed)
    pin = bytes_digest(canonical_bytes(record))
    require(pin == witness["source_projection_pin"], 4, "source_replay_projection_drift")
    return pin[7:]


def verify_open_scene(args, reader, directory):
    """Read the opened scene, render the action and assess the frozen observations."""
    import bpy
    from bpy_extras.object_utils import world_to_camera_view
    from mathutils import Vector
    scene = bpy.context.scene
    rig = scene.objects["FixtureHead"]
    subject = core.sha256_file(bpy.data.filepath)
    require(scene.get("face_request") == core.canonical_json(args), 4, "reopened_request_drift")
    _, registration, _ = read_inputs(args, reader)
    _, scales = fit_request(args, reader)
    require(scene.get("face_scales") == core.canonical_json(scales), 4, "reopened_fit_drift")
    track = checked_record(reader, "face-viseme-track", registration["track_pin"])
    require(0 < len(track["frames"]) <= 32, 6, "fixture_render_frame_budget")
    views, used = heldout(args, reader, subject)
    projection_sha = replay_source_projection(reader, registration, views)
    directory = Path(directory)
    directory.mkdir(exist_ok=False)
    render_paths = {}
    frames = []
    for frame in reopened_samples(rig, scene, track):
        frame["rendered"] = {}
        for name, view in views.items():
            width, height = raster_size(view["size"])
            require((scene.render.resolution_x, scene.render.resolution_y) == (width, height)
                    and scene.render.resolution_percentage == 100, 4, "reopened_raster_drift")
            cam = scene.objects[name]
            expected = domains.pinhole(view["camera"])
            for mark in view["landmarks"]:
                point = frame["vertices_um"][mark["vertex"]]
                xyz = Vector([c / domains.UM_PER_METER for c in point])
                observed = world_to_camera_view(scene, cam, xyz)
                x, y = expected(point)
                scale = domains.MILLIPX_PER_PIXEL
                require(abs(float(x) - width * scale * observed.x) <= 1 and
                        abs(float(y) - height * scale * (1-observed.y)) <= 1, 4, "reopened_camera_drift")
            path = directory / f"{name}-{frame['index']}.png"
            render(scene, cam, path)
            pin = "sha256:" + core.sha256_file(path)
            frame["rendered"][name] = pin
            render_paths[pin] = path
        frames.append(frame)

    def read_rgb(role, pin, size):
        raw = render_paths[pin].read_bytes() if role == "face-reopened-render" else reader(role, pin)
        return decoded_rgb(raw, pin, size)

    landmarks, posed, metrics, reasons = evaluate_frames(
        views=views, used_vertices=used, track=track, frames=frames, read_rgb=read_rgb)
    return {"schema":"af.face-fit/v1", "operation":"af.face_fit", "args":args,
            "scope":"procedural_fixture", "checkpoint":"face_controls",
            "scene_sha256":subject, "track_sha256":registration["track_pin"][7:],
            "heldout_sha256":registration["heldout_pin"][7:], "source_sha256":registration["source_sha256"],
            "source_projection_sha256":projection_sha, "camera_separation_policy":dict(CAMERA_POLICY),
            "i02_decision":I02_DECISION, "scales":scales, "landmark_error_millipx":landmarks,
            "protected_metrics":metrics, "frames":[
                {**{k:v for k,v in frame.items() if k != "vertices_um"}, "landmark_error_millipx":errors}
                for frame, errors in zip(frames, posed, strict=True)],
            "fixture_verdict":"FAIL" if reasons else "PASS", "rc":5 if reasons else 0,
            "reasons":reasons, "criterion_basis":"TARGET", "character_verdict":"CANNOT_SEE",
            "authorized_character_fit":None}
