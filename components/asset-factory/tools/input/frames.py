"""Select independently registered frames from a bounded, pinned CPU video decode."""
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
import re
from statistics import median
from subprocess import TimeoutExpired
from tempfile import TemporaryDirectory

from asset_factory import core, proc
from asset_factory.domains import (MILLIPX_PER_PIXEL, REQUIRED_VIEWS, camera_center, contains, crop_transform,
                                   integers, pinhole, rational, rectangle)
from tools.input.evidence import (InputIssue, bytes_digest, canonical_digest, checked_record, closed, invented_details,
                                  is_digest, native_receipt, point, refuse, require)


@dataclass(frozen=True)
class Frame:
    index: int
    pts: tuple[int, int]
    rgb: bytes

    @property
    def digest(self):
        return bytes_digest(self.rgb)


@dataclass(frozen=True)
class DecodedVideo:
    source_digest: str
    stream: int
    width: int
    height: int
    frames: tuple[Frame, ...]


_PLAN_FIELDS = ("source_digest", "stream", "raster", "phase", "rights_digest", "split_digest",
                "invented_details", "observations")
_ROW_FIELDS = {"id", "view", "index", "pts", "frame_digest", "camera", "crop", "pose_digest",
               "regions", "landmarks", "light_samples"}
_REGION_NAMES = {"face": {"face"}, "hands": {"left_hand", "right_hand"}}
_VIEW_AXES = {"front": (1, -1), "back": (1, 1), "left": (0, 1), "right": (0, -1),
              "face": (1, -1)}
_SIDE_EARS = {"left": ("left_ear", 1), "right": ("right_ear", -1)}
_MIRROR_LIMIT = ("Checks independently annotated profile landmarks against the pinned camera. "
                 "Pixels with jointly relabelled or stale landmark annotations can still look mirrored.")


def _plan(read_checked, pin, subject):
    plan = checked_record(read_checked, "input-plan", pin)
    closed(plan, "af.input-view-plan/v1", *_PLAN_FIELDS)
    require(plan["source_digest"] == subject, 4, "source_video_drift")
    require(type(plan["stream"]) is int and plan["stream"] >= 0, 2, "invalid_video_stream")
    require(min(integers(plan["raster"], 2)) > 0, 2, "invalid_video_raster")
    invented_details(plan["invented_details"])
    phase = plan["phase"]
    require(phase in ("calibration", "held_out", "production"), 2, "invalid_split_phase")

    rights = checked_record(read_checked, "input-rights", plan["rights_digest"])
    closed(rights, "af.input-rights/v1", "source_digest", "allowed_uses")
    require(rights["source_digest"] == subject, 4, "source_rights_drift")
    uses = rights["allowed_uses"]
    require(type(uses) is list and all(type(use) is str for use in uses), 2, "invalid_rights_uses")
    require(phase in uses, 5, "source_rights_denied")

    split = checked_record(read_checked, "input-split", plan["split_digest"])
    closed(split, "af.input-split/v1", "calibration", "held_out")
    for group in (split["calibration"], split["held_out"]):
        require(type(group) is list and group and all(map(is_digest, group))
                and len(set(group)) == len(group), 3, "invalid_or_missing_capture_split")
    require(not set(split["calibration"]).intersection(split["held_out"]), 5, "capture_split_overlap")
    if phase == "production":
        require(subject not in set(split["calibration"]).union(split["held_out"]),
                5, "production_reuses_calibration_or_held_out_capture")
    else:
        require(subject in split[phase], 4, "source_not_in_declared_split")
    return plan


def _lighting(samples):
    require(type(samples) is list and len(samples) >= 2, 3, "diffuse_card_evidence_missing")
    values = []
    for row in samples:
        require(type(row) is dict and row.keys() == {"signal", "dark", "reflectance"},
                2, "invalid_diffuse_card_sample")
        signal, dark, reflectance = (rational(row[key]) for key in ("signal", "dark", "reflectance"))
        require(0 <= dark <= signal <= 1 and 0 < reflectance <= 1, 2, "invalid_diffuse_card_domain")
        values.append((signal - dark) / reflectance)
    center = median(values)
    require(center > 0, 3, "diffuse_card_empty")
    return max(abs(value - center) for value in values) / center


def _side_landmark_sign(view, landmarks):
    ear_id, side = _SIDE_EARS[view]
    require(ear_id in landmarks and "nose_tip" in landmarks, 3, "side_landmark_witness_missing")
    ear, ear_projection = landmarks[ear_id]
    nose, nose_projection = landmarks["nose_tip"]
    require(side * (ear["world_um"][0] - nose["world_um"][0]) > 0
            and nose["world_um"][1] < ear["world_um"][1], 5, "side_landmark_world_order")
    expected = nose_projection[0] - ear_projection[0]
    observed = nose["pixel_millipx"][0] - ear["pixel_millipx"][0]
    uncertainty = ear["uncertainty_millipx"] + nose["uncertainty_millipx"]
    require(abs(expected) > uncertainty and abs(observed) > uncertainty,
            3, "side_landmark_sign_unresolved")
    require(expected * observed > 0, 5, "side_landmark_mirror_sign")
    return {"ear": ear_id, "anterior": "nose_tip", "expected_x_sign": 1 if expected > 0 else -1,
            "observed_dx_millipx": observed, "uncertainty_millipx": uncertainty}


def _registered_observation(row, raster):
    require(row["camera"] is not None and row["crop"] is not None and is_digest(row["pose_digest"]),
            3, "unregistered_observation")
    project = pinhole(row["camera"])
    transform = crop_transform(row["crop"], raster)
    crop = row["crop"]["rect"]
    view = row["view"]

    if view in _VIEW_AXES:
        center = camera_center(row["camera"])
        axis, sign = _VIEW_AXES[view]
        require(sign * center[axis] > abs(center[1 - axis]), 5, "camera_view_direction_mismatch")

    regions = row["regions"]
    require(type(regions) is dict and _REGION_NAMES.get(view, {"subject"}) <= regions.keys(),
            3, "protected_view_region_missing")
    margin = min(min(region[0] - crop[0], region[1] - crop[1], crop[2] - region[2], crop[3] - region[3])
                 for region in (rectangle(rect, raster) for rect in regions.values()))
    require(margin >= 0, 5, "protected_view_region_cropped")

    require(transform[0] == transform[1], 5, "anisotropic_registered_crop")

    landmarks = row["landmarks"]
    require(type(landmarks) is list and landmarks, 3, "held_out_landmarks_missing")
    errors, witnesses = [], {}
    x0, y0, x1, y1 = [v * MILLIPX_PER_PIXEL for v in crop]
    for item in landmarks:
        require(type(item) is dict and item.keys() ==
                {"id", "world_um", "pixel_millipx", "uncertainty_millipx"}, 2, "invalid_landmark")
        name, uncertainty = item["id"], item["uncertainty_millipx"]
        require(type(name) is str and name and name not in witnesses, 2, "invalid_landmark_id")
        require(type(uncertainty) is int and uncertainty >= 0, 2, "invalid_landmark_uncertainty")
        x, y = integers(item["pixel_millipx"], 2)
        require(contains((x0, y0, x1, y1),
                         (x - uncertainty, y - uncertainty, x + uncertainty + 1, y + uncertainty + 1)),
                5, "landmark_uncertainty_cropped")
        projected = project(item["world_um"])
        witnesses[name] = item, projected
        errors.append((max(abs(a - b) for a, b in zip(projected, (x, y))), uncertainty))
    side_sign = _side_landmark_sign(view, witnesses) if view in _SIDE_EARS else None
    require(all(error <= uncertainty for error, uncertainty in errors), 5, "held_out_reprojection_violation")
    flatness = _lighting(row["light_samples"])
    return (max(error for error, _ in errors), flatness, -margin, row["index"], row["id"]), transform, side_sign


def select_frames(video, *, plan_digest, read_checked, choices=None):
    """Select by reprojection, card flatness, crop margin, then earliest ordinal.

    input-plan grants semantic registration authority independently of the decoded
    bytes. This lower-level entry consumes the trusted decoder's RGB/PTS output.
    Use select_video when the starting point is encoded media.
    """
    receipt = native_receipt("input.views", video.source_digest, plan_digest)
    try:
        plan = _plan(read_checked, plan_digest, video.source_digest)
        raster = [video.width, video.height]
        require(raster == plan["raster"] and video.stream == plan["stream"], 4, "decoded_video_scope_drift")
        receipt.update(input_refs=[video.source_digest],
                       reference_refs=[plan_digest, plan["rights_digest"], plan["split_digest"]],
                       invented_details=plan["invented_details"], phase=plan["phase"])
        previous = None
        for index, frame in enumerate(video.frames):
            require(type(frame.index) is int and frame.index == index, 5, "decoded_indices_not_contiguous")
            timestamp = rational(frame.pts)
            require(previous is None or timestamp > previous, 5, "decoded_pts_not_increasing")
            previous = timestamp

        rows = plan["observations"]
        require(type(rows) is list and rows, 3, "registered_observations_missing")
        if choices is not None:
            require(type(choices) is dict and choices.keys() == set(REQUIRED_VIEWS)
                    and all(type(value) is str for value in choices.values()), 2, "invalid_view_choices")

        candidates = {view: [] for view in REQUIRED_VIEWS}
        owners, ids, indexes = {}, set(), set()
        for row in rows:
            require(type(row) is dict and row.keys() == _ROW_FIELDS, 2, "invalid_view_observation")
            view, name, index = row["view"], row["id"], row["index"]
            require(type(view) is str and view in candidates and type(name) is str and name
                    and name not in ids and type(index) is int and index >= 0, 2, "invalid_view_identity")
            require(index not in indexes, 5, "replayed_view_index")
            require(index < len(video.frames), 3, "registered_frame_missing")
            frame = video.frames[index]
            pin = frame.digest
            timestamp = rational(frame.pts)
            require(frame.index == index and len(frame.rgb) == video.width * video.height * 3
                    and pin == row["frame_digest"] and timestamp == rational(row["pts"]),
                    4, "registered_frame_drift")
            require(owners.get(pin, view) == view, 5, "replayed_view_pixels")
            owners[pin] = view
            ids.add(name)
            indexes.add(index)
            candidates[view].append(row)

        selected, scores, side_signs = {}, [], {}
        for view, options in candidates.items():
            if choices is not None:
                require(choices[view] in ids, 3, "view_selection_missing")
                options = [row for row in options if row["id"] == choices[view]]
                require(options, 5, "view_role_mismatch")
            require(options, 3, "required_view_missing:" + view)
            valid, refusals = [], []
            for row in options:
                try:
                    score, transform, side_sign = _registered_observation(row, raster)
                    valid.append((score, row, transform, side_sign))
                except InputIssue as exc:
                    refusals.append(exc)
            if not valid:
                raise refusals[0]
            score, row, transform, side_sign = min(valid, key=lambda candidate: candidate[0])
            selected[view] = {**row, "crop_transform": transform}
            scores.append(score)
            if side_sign is not None:
                side_signs[view] = side_sign

        poses = {row["pose_digest"] for row in selected.values()}
        require(len(poses) == 1, 5, "selected_view_pose_mismatch")
        selection = {"schema": "af.input-frame-selection/v1", "source_digest": video.source_digest,
                     "stream": video.stream, "raster": raster, "plan_digest": plan_digest,
                     "selected": selected, "pose_digest": next(iter(poses)),
                     "side_landmark_signs": side_signs, "invented_details": plan["invented_details"]}
        receipt.update(subject_digest=canonical_digest(selection), selection=selection, residual_mirror_risk=_MIRROR_LIMIT)
        receipt["metrics"].update(coverage=point(1), landmark_error=point(max(s[0] for s in scores)),
                                  lighting_flatness=point(max(s[1] for s in scores)))
        return receipt
    except (ValueError, TypeError, KeyError, core.ContractError, MemoryError, RecursionError) as exc:
        return refuse(receipt, exc)


_FRAMEHASH_ROW = re.compile(
    r"0,\s*(-?\d+),\s*(-?\d+),\s*-?\d+,\s*(\d+),\s*([0-9a-f]{64})"
)


def _read_limited(path, limit):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    require(len(data) <= limit, 6, "decoder_output_budget")
    return data


def _decode(raw, plan, config, source):
    closed(config, "af.input-decoder/v1", "ffmpeg_path", "ffmpeg_digest", "format",
           "max_encoded_bytes", "max_decoded_bytes", "max_frames", "timeout_seconds", "max_codec_pixels")
    limits = [config[key] for key in ("max_encoded_bytes", "max_decoded_bytes", "max_frames", "timeout_seconds", "max_codec_pixels")]
    require(all(type(n) is int and n > 0 for n in limits) and limits[3] <= 86400, 2, "invalid_decoder_budget")
    encoded_limit, decoded_limit, frame_limit, timeout, codec_pixels = limits
    width, height = plan["raster"]
    frame_bytes = width * height * 3
    require(0 < len(raw) <= encoded_limit and width * height <= codec_pixels
            and frame_bytes * (frame_limit + 1) <= decoded_limit,
            6, "video_byte_budget")
    require(config["format"] in ("matroska", "mov"), 2, "unsupported_video_format")
    executable = Path(config["ffmpeg_path"])
    require(executable.is_absolute() and executable.is_file(), 3, "decoder_executable_missing")
    require("sha256:" + core.sha256_file(executable) == config["ffmpeg_digest"], 4, "decoder_executable_drift")
    with TemporaryDirectory(prefix="af-input-") as directory:
        framehash = Path(directory) / "rgb.framehash"
        argv = [str(executable), "-hide_banner", "-nostdin", "-loglevel", "error", "-xerror",
                "-copyts", "-noautorotate", "-threads", "1", "-max_pixels", str(codec_pixels),
                "-protocol_whitelist", "pipe", "-f", config["format"], "-i", "pipe:0",
                "-map", "0:" + str(plan["stream"]), "-an", "-sn", "-dn",
                "-filter_threads", "1", "-vf", "format=rgb24", "-noautoscale",
                "-frames:v", str(frame_limit + 1), "-fps_mode", "passthrough", "-enc_time_base", "-1",
                "-threads", "1", "-c:v", "rawvideo", "-f", "tee",
                "[f=rawvideo]pipe:1|[f=framehash:hash=sha256]" + str(framehash)]
        job = proc.popen(argv, start_new_session=True, input=raw, runtime_s=timeout,
                         env={"PATH": "/usr/bin:/bin", "LANG": "C"})
        try:
            require(job.wait(timeout) == 0, 2, "video_decode_failed")
            pixels = _read_limited(job.stdout_path, decoded_limit)
            index = _read_limited(framehash, 4096 + 256 * (frame_limit + 1)).decode("ascii")
        finally:
            errors = proc.cleanup(job)
        require(not errors, 3, "decoder_release_unproven")

    timebases = re.findall(r"^#tb 0: (\d+)/([1-9]\d*)$", index, re.MULTILINE)
    dimensions = re.findall(r"^#dimensions 0: (\d+)x(\d+)$", index, re.MULTILINE)
    frames = [line for line in index.splitlines() if line and not line.startswith("#")]
    require(len(frames) <= frame_limit and len(pixels) < (frame_limit + 1) * frame_bytes,
            6, "decoded_frame_budget")
    require(len(timebases) == 1 and dimensions == [(str(width), str(height))]
            and frames and len(pixels) == len(frames) * frame_bytes, 3, "unregistered_decoder_output")
    timebase = Fraction(*map(int, timebases[0]))
    require(timebase > 0, 2, "invalid_decoder_timebase")
    result = []
    previous = None
    for i, record in enumerate(frames):
        row = _FRAMEHASH_ROW.fullmatch(record)
        require(row is not None, 3, "decoder_framehash_row_invalid")
        dts, pts, size, digest = row.groups()
        timestamp = int(pts) * timebase
        frame = Frame(i, timestamp.as_integer_ratio(), pixels[i * frame_bytes:(i + 1) * frame_bytes])
        require(dts == pts and int(size) == frame_bytes and frame.digest == "sha256:" + digest
                and (previous is None or timestamp > previous), 3, "decoder_frame_discontinuity")
        result.append(frame)
        previous = timestamp
    return DecodedVideo(source, plan["stream"], width, height, tuple(result))


def select_video(raw, *, plan_digest, decoder_digest, read_checked, choices=None):
    """Return the evidence receipt and its selected RGB frames, keyed by view."""
    source = bytes_digest(raw)
    try:
        plan = _plan(read_checked, plan_digest, source)
        config = checked_record(read_checked, "input-decoder", decoder_digest)
        video = _decode(raw, plan, config, source)
    except TimeoutExpired:
        return refuse(native_receipt("input.views", source, plan_digest), InputIssue(6, "decode_timeout")), {}
    except (proc.SignalRefusal, OSError):
        return refuse(native_receipt("input.views", source, plan_digest), InputIssue(3, "decoder_unavailable")), {}
    except (ValueError, TypeError, KeyError, core.ContractError, MemoryError, RecursionError) as exc:
        return refuse(native_receipt("input.views", source, plan_digest), exc), {}
    result = select_frames(video, plan_digest=plan_digest, read_checked=read_checked, choices=choices)
    if "reference_refs" in result:
        result["reference_refs"].append(decoder_digest)
    result["decoder_digest"] = decoder_digest
    selected = result.get("selection", {}).get("selected", {})
    return result, {view: video.frames[row["index"]] for view, row in selected.items()}
