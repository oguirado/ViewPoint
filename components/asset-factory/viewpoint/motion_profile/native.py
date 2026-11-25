# SPDX-License-Identifier: AGPL-3.0-only
"""Mini-tools for a supplied BVH and independently observed synthetic video clock."""

import math
import re
import resource
import time

from asset_factory import core
from providers.mocapanything_v2 import bvh
from providers.mocapanything_v2.contract import ROOT, inspect_rig, load, validate
from providers.mocapanything_v2.gate import evaluate

from motion import checked_json, demand, digest, shape

MODE = "supplied_bvh_with_video_clock"
DIAGNOSTIC_ROLES = {"rig_glb", "action_bvh", "video", "observation"}
REFERENCE_ROLES = {"reference", "pose_reference"}
REPAIR_ROLES = {"derived_observation"}
CONTACT_BAND_UM = 500  # Synthetic Blender observer contract.


def onsets(samples, feet):
    return {foot: [index for index, frame in enumerate(samples) if frame["contacts"][foot]
                   and (index == 0 or not samples[index - 1]["contacts"][foot])] for foot in feet}



def observed_clip(data, rig, video, raw_observation):
    observation = checked_json(raw_observation)
    validate(observation, "observation")
    demand(observation["rig_sha256"] == digest(rig), "WRONG_RIG_BINDING")
    demand(observation["video_sha256"] == digest(video) and observation["bvh_sha256"] == digest(data),
           "NATIVE_OBSERVATION_BINDING")
    demand(observation["measurement_kind"] == "synthetic_fixture", "EVIDENCE_SCOPE_MISMATCH")
    clip = bvh.parse(data)
    samples = observation["frames"]
    demand(len(samples) == len(clip.rows), "FRAME_COUNT_MISMATCH")
    feet = samples[0]["contacts"].keys()
    names = {joint.name for joint in clip.joints}
    demand(bool(feet) and feet <= names, "NATIVE_SAMPLE_BINDING")
    demand(all(frame["frame_id"] == frame["source_index"] == index
               and frame["pose_sha256"] == clip.frame_hashes[index] and frame["sole_um"].keys() == feet
               and frame["contacts"].keys() == feet and all(len(point) == 3 for point in frame["sole_um"].values())
               for index, frame in enumerate(samples)), "NATIVE_SAMPLE_BINDING")
    return clip, observation, feet


def contact_strip(clip, samples):
    return [{"frame": i, "time_s": float(i * clip.frame_time),
             "soles": {foot: {"position_um": point, "contact": bool(frame["contacts"][foot])}
                       for foot, point in frame["sole_um"].items()}}
            for i, frame in enumerate(samples)]


def diagnose(raw, rig, video, raw_observation):
    clip, observation, feet = observed_clip(raw, rig, video, raw_observation)
    joints = inspect_rig(rig)
    demand(joints.keys() == {joint.name for joint in clip.joints}, "RIG_BVH_MISMATCH")
    for joint in clip.joints:
        parent = clip.joints[joint.parent].name if joint.parent is not None else None
        measured = joints[joint.name]
        demand(parent == measured["parent"]
               and all(abs(offset * 1000000 - actual) <= 1
                       for offset, actual in zip(joint.offset, measured["offset_um"], strict=True)),
               "RIG_BVH_MISMATCH")
    samples = observation["frames"]
    return clip, contact_strip(clip, samples), onsets(samples, feet)


def trace(data, rig, video, ref, raw_observation, *, local_poses=None):
    clip, observation, feet = observed_clip(data, rig, video, raw_observation)
    validate(ref, "reference")
    rig_sha, video_sha = digest(rig), digest(video)
    ref_sha = core.sha256_json(ref)
    identity = core.sha256_json({"rig_sha256": rig_sha, "video_sha256": video_sha, "reference_sha256": ref_sha})
    demand(ref["rig_sha256"] == rig_sha, "WRONG_RIG_BINDING")
    demand(observation["request_sha256"] == identity and observation["reference_sha256"] == ref_sha,
           "NATIVE_OBSERVATION_BINDING")
    samples = observation["frames"]
    demand(ref["frame_count"] == len(samples), "FRAME_COUNT_MISMATCH")
    demand(feet == ref["feet"].keys(), "NATIVE_SAMPLE_BINDING")
    feet = ref["feet"]

    report = evaluate(data, rig, ref, observation, request_sha256=identity, video_sha256=video_sha,
                      scope="synthetic_fixture")
    # The native gate stops at the first independently observed contact label drift.
    # Preceding checks already verified the GLB hierarchy, BVH rest offsets and clock.
    policy_failure = local_poses is not None and report["rc"] == 5 and bool(report["metrics"])
    demand(report["rc"] == 0 or report["reason_codes"] == ["contact_relabelled"] or policy_failure,
           (report["reason_codes"] or ["NATIVE_GATE_FAILED"])[0])
    max_gap = 0
    foot_indices = {joint.name: index for index, joint in enumerate(clip.joints) if joint.name in feet}
    for row, frame in zip(clip.rows, samples, strict=True):
        positions, rotations, local = bvh.forward(clip, row, ref["units_um"])
        if local_poses is not None:
            local_poses.append(local)
        for foot, offset in feet.items():
            index = foot_indices[foot]
            shifted = bvh.rotate(rotations[index], offset)
            predicted = [round(a + b) for a, b in zip(positions[index], shifted, strict=True)]
            sole = frame["sole_um"][foot]
            max_gap = max(max_gap, math.ceil(math.dist(predicted, sole)))
            demand(bool(frame["contacts"][foot]) == (abs(sole[1] - ref["floor_y_um"]) <= CONTACT_BAND_UM),
                   "NATIVE_SOLE_CONTACT_MISMATCH")
    policy = load(ROOT / "contact-policy.json", "policy")
    demand(max_gap <= policy["max_surface_fk_gap_um"], "sole_binding_mismatch")
    observed = onsets(samples, feet)
    expected = {foot: [i for i, contact in enumerate(mask) if contact and (i == 0 or not mask[i - 1])]
                for foot, mask in ref["contacts"].items()}
    return clip, contact_strip(clip, samples), observed, expected, max_gap, report


def hierarchy_sha(data):
    return digest(b" ".join(re.split(rb"(?m)^[ \t]*MOTION[ \t]*\r?$", data, maxsplit=1)[0].split()))


def compare(clip, observed, expected, report, pose):
    demand(observed == expected, "CONTACT_ONSET_MISMATCH")
    demand(report["rc"] == 0, (report["reason_codes"] or ["NATIVE_GATE_FAILED"])[0])
    demand(list(clip.frame_hashes) == pose["frame_sha256"], "STRUCTURAL_MOTION_MISMATCH")



def retime(raw, clip, feet, advance):
    count = len(clip.rows)
    demand(type(advance) is int and 0 < abs(advance) < count, "RETIMING_NOT_ADMISSIBLE")
    joints = [joint for joint in clip.joints if joint.name in feet]
    demand(len(joints) == len(feet) and all(joint.parent is not None for joint in joints),
           "RETIMING_NOT_ADMISSIBLE")
    channels = [channel for joint in joints for channel in range(joint.start, joint.start + len(joint.channels))]
    # Repeating the nearest sample is lossless at the boundary only if the discarded
    # samples have the same values there. The independent pose oracle checks all rows.
    discarded = range(advance + 1) if advance > 0 else range(count - 1 + advance, count)
    boundary = advance if advance > 0 else count - 1 + advance
    demand(all(clip.rows[index][channel] == clip.rows[boundary][channel]
               for index in discarded for channel in channels), "RETIMING_EDGE_MOTION")

    lines = raw.splitlines(keepends=True)
    motion_line = next(index for index, line in enumerate(lines) if line.strip() == b"MOTION")
    payload = [index for index in range(motion_line + 1, len(lines)) if lines[index].strip()]
    row_lines = payload[2:]
    tokens = [re.split(rb"(\S+)", lines[index]) for index in row_lines]
    for index, line in enumerate(row_lines):
        source = max(0, min(count - 1, index + advance))
        row = tokens[index].copy()
        for channel in channels:
            source_token = tokens[source][channel * 2 + 1]
            token_index = channel * 2 + 1
            if clip.rows[source][channel] != clip.rows[index][channel]:
                row[token_index] = source_token
        lines[line] = b"".join(row)
    return b"".join(lines)


def perform(operation, params, files, started, create_revision):
    raw, rig, video = (files[role] for role in ("action_bvh", "rig_glb", "video"))
    result = {"scope": "offline_fixture", "source_mode": MODE, "rig_sha256": digest(rig), "bvh_sha256": digest(raw)}
    if operation in {"inspect", "preview"} and "reference" not in files:
        clip, timeline, observed = diagnose(raw, rig, video, files["observation"])
        if operation == "preview":
            return result | {"contact_strip": timeline}
        return result | {"measurements": {"contact_onsets_frame": observed, "timeline": timeline},
                         "joint_order": [joint.name for joint in clip.joints]}
    ref = checked_json(files["reference"])
    pose = checked_json(files["pose_reference"])
    shape(pose, "rig_sha256 video_sha256 reference_sha256 hierarchy_sha256 frame_sha256", "POSE_REFERENCE_INVALID")
    demand((pose["rig_sha256"], pose["video_sha256"], pose["reference_sha256"])
           == (digest(rig), digest(video), digest(files["reference"])), "POSE_REFERENCE_INVALID")
    reference_bvh = files.get("pose_reference_bvh")
    local_poses = [] if reference_bvh is not None else None
    clip, timeline, observed, expected, gap, report = trace(
        raw, rig, video, ref, files["observation"], local_poses=local_poses)
    demand(hierarchy_sha(raw) == pose["hierarchy_sha256"], "RIG_BVH_MISMATCH")
    if operation == "inspect":
        measurements = {"contact_onsets_frame": observed, "expected_onsets_frame": expected,
                        "timeline": timeline, "max_surface_fk_gap_um": gap, "floor_y_um": ref["floor_y_um"]}
        if reference_bvh is not None:
            from concentration import inspect

            demand(hierarchy_sha(reference_bvh) == pose["hierarchy_sha256"], "POSE_REFERENCE_INVALID")
            target = bvh.parse(reference_bvh)
            demand(target.joints == clip.joints and target.frame_time == clip.frame_time
                   and list(target.frame_hashes) == pose["frame_sha256"], "POSE_REFERENCE_INVALID")
            measurements["temporal_concentration"] = inspect(
                clip, target, ref, timeline, local_poses, observed, expected, report["reason_codes"])
            measurements["temporal_concentration"]["reference_bvh_sha256"] = digest(reference_bvh)
        return result | {"measurements": measurements, "joint_order": [joint.name for joint in clip.joints]}
    if operation == "preview":
        return result | {"contact_strip": timeline}
    if operation == "repair":
        demand(params["base_revision_sha256"] == digest(raw), "STALE_BASE_REVISION")
        demand(observed != expected, "RETIMING_NOT_NEEDED")
        advance = params.get("advance_foot_frames")
        revised = retime(raw, clip, ref["feet"], advance)
        after, _, on, wanted, after_gap, after_report = trace(revised, rig, video, ref, files["derived_observation"])
        demand(hierarchy_sha(revised) == pose["hierarchy_sha256"] and clip.frame_time == after.frame_time,
               "RIG_BVH_MISMATCH")
        compare(after, on, wanted, after_report, pose)
        revised_sha = digest(revised)
        demand(revised_sha != digest(raw), "RETIMING_NOT_NEEDED")
        binding = {"schema": "vp3d.motion_native_revision/v1", "parent_bvh_sha256": digest(raw),
                   "bvh_sha256": revised_sha, "source_mode": MODE, "rig_sha256": digest(rig),
                   "source_sha256": digest(video), "observation_sha256": digest(files["derived_observation"]),
                   "advance_foot_frames": advance, "max_surface_fk_gap_um": after_gap}
        create_revision(revised, binding)
        return result | {"derived_binding": binding, "derived_bvh": revised.decode("ascii"),
                         "before_onsets_frame": observed, "after_onsets_frame": on}
    compare(clip, observed, expected, report, pose)
    result |= {"match": True, "reference_sha256": digest(files["reference"]),
               "frames": len(clip.rows), "joints": len(clip.joints)}
    if operation == "benchmark":
        result |= {"device": "cpu", "wall_ns": time.perf_counter_ns() - started,
                   "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}
    return result
