"""FACE fixture observations. The caller supplies the protected reference reader."""
from fractions import Fraction

from asset_factory import domains, math_acceptance
from tools.blender.uv_quality import raster_size
from tools.input.evidence import bytes_digest, checked_record, closed, is_digest, point, require
from .fit import camera_projection, project_errors, read_inputs, registered

MAX_CHANNEL_PPM = 20_000
WORST_TILE_PPM = 10_000
LANDMARK_MILLIPX = 1  # Fixture-only budget for rounding the independent Blender observations.


# Fixture-only angle target. Round cos(10°) down to a rational ceiling so the
# admission comparison is exact and requires at least the target separation.
CAMERA_POLICY = {"basis": "TARGET_UNCALIBRATED", "max_axis_cosine": [984_807, 1_000_000]}
I02_DECISION = "local_comparison_of_uncalibrated_TARGET_limits"


def require_separation(camera, designated):
    rotation, _, _ = camera_projection(camera)
    max_cosine = Fraction(*CAMERA_POLICY["max_axis_cosine"])
    for rows, _, _ in designated:
        cosine = sum(a * b for a, b in zip(rotation[2], rows[2]))
        require(cosine <= max_cosine, 5, "held_out_camera_reused")


def projection_record(source_sha256, track_pin, frame_marks):
    return {"schema": "face.source-projection/v1", "source_sha256": source_sha256,
            "track_pin": track_pin, "views": frame_marks}


def require_reference(origin, source, registered_source, candidate_sources):
    require(origin == "pre_fit_authored_scene" and source not in candidate_sources,
            5, "face_reference_origin")
    require(type(source) is str and is_digest("sha256:" + source) and source == registered_source,
            4, "face_reference_source_drift")


def protected_residuals(reference, candidate, size, rectangle, tile_size):
    """I02 on fixed opaque linear RGB8 samples in top-left scanline order."""
    w, h = raster_size(size)
    require(isinstance(reference, bytes) and isinstance(candidate, bytes)
            and len(reference) == len(candidate) == 3*w*h, 2, "invalid_face_rgb")
    x0, y0, x1, y1 = domains.rectangle(rectangle, size)
    require(type(tile_size) is int and tile_size > 0, 2, "invalid_tile_size")
    maximum, worst = 0, Fraction(0)
    for y in range(y0, y1, tile_size):
        for x in range(x0, x1, tile_size):
            total = 0
            right, bottom = min(x1, x+tile_size), min(y1, y+tile_size)
            for row in range(y, bottom):
                start, stop = 3*(row*w+x), 3*(row*w+right)
                for a, b in zip(reference[start:stop], candidate[start:stop]):
                    error = abs(a-b)
                    maximum = max(maximum, error)
                    total += error
            worst = max(worst, Fraction(total, (right-x)*(bottom-y)*3*255))
    return Fraction(maximum, 255), worst


def image_errors(reference, candidate, size, rectangle, tile_size):
    channel, worst = protected_residuals(reference, candidate, size, rectangle, tile_size)
    channel_upper, tile_upper = (math_acceptance.outward_bounds(*point(value), scale=domains.PPM)[1]
                                 for value in (channel, worst))
    return {"max_channel_error_upper_ppm": channel_upper, "worst_tile_error_upper_ppm": tile_upper,
            "within_targets": channel_upper <= MAX_CHANNEL_PPM and tile_upper <= WORST_TILE_PPM}


def heldout(args, reader, candidate_sha256):
    mesh, registration, fit = read_inputs(args, reader)
    witness = checked_record(reader, "face-independent-observations", registration["heldout_pin"])
    require_reference(witness["origin"], witness["source_sha256"], registration["source_sha256"],
                      {candidate_sha256, args["mesh_sha256"]})
    source_pin = "sha256:" + registration["source_sha256"]
    source = reader("face-reference-scene", source_pin)
    require(source is not None, 3, "face_reference_scene_missing")
    require(type(source) is bytes and source.startswith(b"BLENDER") and bytes_digest(source) == source_pin,
            4, "face_reference_scene_drift")
    registered(witness["views"])
    require(witness["views"]["front"]["size"] == witness["views"]["profile"]["size"],
            3, "fixture_common_raster_required")
    fit_cameras = [camera_projection(v["camera"]) for v in fit.values()]
    for view in witness["views"].values():
        require_separation(view["camera"], fit_cameras)
    require("source_projection_pin" in witness, 3, "missing_source_projection")
    closed(witness, "face.heldout-observations/v1", "origin", "source_sha256", "views",
           "source_projection_pin")
    projection = checked_record(reader, "face-source-projection", witness["source_projection_pin"])
    track = checked_record(reader, "face-viseme-track", registration["track_pin"])
    frame_count = len(track["frames"])
    require(0 < frame_count <= 32, 6, "fixture_render_frame_budget")
    frame_marks = {}
    used = {item["vertex"] for view in fit.values() for item in view["landmarks"]}
    required_parts = {"left_eye", "nose", "upper_lip", "lower_lip", "chin"}
    for name, view in witness["views"].items():
        seen = set()
        rect = domains.rectangle(view["protected_rect"], view["size"])
        for item in view["landmarks"]:
            idx = item["vertex"]
            require(type(idx) is int and 0 <= idx < len(mesh["parts"]), 2, "invalid_landmark_index")
            seen.add(mesh["parts"][idx])
            x, y = (domains.rational(v) / domains.MILLIPX_PER_PIXEL for v in item["xy_millipx"])
            require(rect[0] <= x < rect[2] and rect[1] <= y < rect[3], 3, "unprotected_landmark")
        require(required_parts <= seen, 3, "held_out_anatomical_coverage")
        poses = view.get("frame_landmarks")
        require(isinstance(poses, list) and len(poses) == frame_count, 3, "source_pose_coverage")
        ids = [mark["vertex"] for mark in view["landmarks"]]
        require(len(ids) <= 256, 6, "landmark_budget")
        require(len(ids) == len(set(ids)) and used.isdisjoint(ids), 5, "landmark_not_held_out")
        require(all(isinstance(marks, list) and [m["vertex"] for m in marks] == ids for marks in poses),
                3, "source_pose_landmark_coverage")
        require(poses[0] == view["landmarks"], 4, "source_projection_drift")
        for marks in poses:
            for mark in marks:
                x, y = (domains.rational(v) / domains.MILLIPX_PER_PIXEL for v in mark["xy_millipx"])
                require(rect[0] <= x < rect[2] and rect[1] <= y < rect[3], 3, "unprotected_landmark")
        frame_marks[name] = poses
    require(projection == projection_record(registration["source_sha256"], registration["track_pin"],
                                            frame_marks), 4, "source_projection_drift")
    return witness["views"], used


def evaluate_frames(*, views, used_vertices, track, frames, read_rgb):
    """Input samples must come from the reopened scene and approved reference images."""
    from tools.lipsync import validate_track
    validate_track(track)
    require(len(frames) == len(track["frames"]) > 0, 3, "missing_rendered_frame")
    # The pose table uses this neutral frame as its rest observation.
    require(track["frames"][0]["mouth_ppm"]["rest"] == domains.PPM
            and track["frames"][0]["jaw_ppm"] == 0, 3, "fixture_neutral_frame_required")
    require(any(c["mouth_ppm"]["closed"] == domains.PPM and c["jaw_ppm"] == 0
                for c in track["frames"])
            and any(c["mouth_ppm"]["open"] == domains.PPM and c["jaw_ppm"] > 0
                    for c in track["frames"]), 3, "fixture_control_coverage")
    per_frame = []
    for index, frame in enumerate(frames):
        pose = {name: {**view, "landmarks": view["frame_landmarks"][index]} for name, view in views.items()}
        per_frame.append(project_errors(frame["vertices_um"], pose, used_vertices))
    landmark = {name: max(errors[name] for errors in per_frame) for name in views}
    reasons = ["held_out_landmark_limit"] if max(landmark.values()) > LANDMARK_MILLIPX else []
    measured = {}
    for name, view in views.items():
        require(len(view["reference_images"]) == len(frames), 3, "reference_frame_coverage")
        metrics = []
        reference_poses = {}
        for cue, frame, pin in zip(track["frames"], frames, view["reference_images"], strict=True):
            require(cue["index"] == frame["index"] and cue["pts"] == frame["pts"],
                    4, "render_time_drift")
            require(set(frame["rendered"]) == set(views), 3, "render_view_coverage")
            reference = read_rgb("face-reference-render", pin, view["size"])
            candidate = read_rgb("face-reopened-render", frame["rendered"][name], view["size"])
            for pose in ("rest", "closed", "open"):
                if cue["mouth_ppm"][pose] == domains.PPM and pose not in reference_poses:
                    reference_poses[pose] = reference
            metrics.append(image_errors(reference, candidate, view["size"], view["protected_rect"],
                                        view["tile_size"]))
        measured[name] = metrics
        for pose in ("closed", "open"):
            delta = image_errors(reference_poses["rest"], reference_poses[pose], view["size"],
                                 view["protected_rect"], view["tile_size"])
            require(delta["max_channel_error_upper_ppm"] > 0, 3, "reference_control_invisible")
        if any(m["max_channel_error_upper_ppm"] > MAX_CHANNEL_PPM for m in metrics):
            reasons.append("protected_channel_limit." + name)
        if any(m["worst_tile_error_upper_ppm"] > WORST_TILE_PPM for m in metrics):
            reasons.append("protected_tile_limit." + name)
    return landmark, per_frame, measured, reasons
