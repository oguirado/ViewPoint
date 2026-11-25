# SPDX-License-Identifier: AGPL-3.0-only
"""Local motion diagnostics for reference-bound native fixture inspection."""

import math
from itertools import zip_longest

from providers.mocapanything_v2 import bvh


SOLE_PARITY_UM = 10  # [TARGET] The offline Blender readback tolerance.


def angular_velocity(before, after, period):
    # Express the shortest inter-frame rotation in the parent joint's basis.
    delta = bvh.multiply(after, tuple(zip(*before)))
    skew = (delta[2][1] - delta[1][2], delta[0][2] - delta[2][0], delta[1][0] - delta[0][1])
    sine = math.hypot(*skew) / 2
    cosine = (sum(delta[i][i] for i in range(3)) - 1) / 2
    if sine > 1e-8:
        axis = tuple(value / (2 * sine) for value in skew)
    elif cosine >= 0:
        return (0.0, 0.0, 0.0)
    else:
        # At a half turn the skew vanishes. Recover the axis from the symmetric part.
        pivot = max(range(3), key=lambda i: delta[i][i])
        component = math.sqrt((delta[pivot][pivot] + 1) / 2)
        axis = tuple(component if i == pivot else (delta[i][pivot] + delta[pivot][i]) / (4 * component)
                     for i in range(3))
    rate = math.degrees(math.atan2(sine, cosine)) / period
    return tuple(value * rate for value in axis)


def joint_motion(poses, joints, period):
    rates = {}
    for index, joint in enumerate(joints):
        vectors = [angular_velocity(a[index], b[index], period) for a, b in zip(poses, poses[1:])]
        speeds = [None, *(round(math.hypot(*vector), 4) for vector in vectors)]
        changes = [None] * min(2, len(poses))
        changes.extend(round(math.dist(a, b), 4) for a, b in zip(vectors, vectors[1:]))
        rates[joint.name] = (speeds, changes)
    return rates


def inspect(clip, target, reference, timeline, poses, observed, expected, policy_refusals):
    joints = clip.joints
    joint_names = [joint.name for joint in joints]
    feet = reference["feet"]
    foot_indices = {name: joint_names.index(name) for name in feet}
    rates = joint_motion(poses, joints, float(clip.frame_time))
    peak_motion, first_pose_mismatches = {}, {}
    changed_joint_samples = 0
    first_contact_mismatches, first_sole_mismatches = {}, {}
    max_sole_error, max_plant = dict.fromkeys(feet, 0.0), dict.fromkeys(feet, (0.0, None))
    anchors = {}
    events = {(name, frame) for name, onsets in observed.items() for frame in onsets}
    events.update((name, frame) for name, onsets in expected.items() for frame in onsets)

    for joint in joints:
        speed, change = rates[joint.name]
        peak_motion[joint.name] = {}
        for name, series in (("angular_speed_deg_s", speed), ("angular_velocity_change_deg_s", change)):
            peak = max((i for i, value in enumerate(series) if value is not None), key=series.__getitem__, default=None)
            peak_motion[joint.name][name] = {"frame": peak, "value": series[peak] if peak is not None else None}
            if peak is not None and series[peak] > 0:
                events.add((joint.name, peak))

    for frame, (row, target_row, sample) in enumerate(zip(clip.rows, target.rows, timeline, strict=True)):
        if clip.frame_hashes[frame] != target.frame_hashes[frame]:
            for joint in joints:
                channels = slice(joint.start, joint.start + len(joint.channels))
                if row[channels] != target_row[channels]:
                    changed_joint_samples += 1
                    first_pose_mismatches.setdefault(joint.name, frame)
        positions, rotations, _ = bvh.forward(target, target_row, reference["units_um"])
        for name, offset in feet.items():
            index = foot_indices[name]
            target_sole = [round(a + b) for a, b in zip(positions[index], bvh.rotate(rotations[index], offset))]
            sole = sample["soles"][name]
            point = sole["position_um"]
            error = math.dist(point, target_sole)
            max_sole_error[name] = max(max_sole_error[name], error)
            if error > SOLE_PARITY_UM:
                first_sole_mismatches.setdefault(name, frame)
            if sole["contact"] != bool(reference["contacts"][name][frame]):
                first_contact_mismatches.setdefault(name, frame)
            if sole["contact"]:
                anchor = anchors.setdefault(name, point)
                displacement = math.dist(point, anchor)
                if max_plant[name][1] is None or displacement > max_plant[name][0]:
                    max_plant[name] = (displacement, frame)
            else:
                anchors.pop(name, None)

    onset_errors = {name: [{"expected_frame": a, "observed_frame": b,
                            "delta_frames": b - a if a is not None and b is not None else None}
                           for a, b in zip_longest(expected[name], observed[name])]
                    for name in feet}
    parents = {joint.name: joint_names[joint.parent] if joint.parent is not None else None for joint in joints}
    windows = []
    for name, frame in sorted(events, key=lambda event: (event[1], event[0])):
        neighbors = [other for other in joint_names if other in (name, parents[name]) or parents[other] == name]
        windows.append({"joint": name, "frame": frame,
                        "frame_range": [max(frame - 1, 0), min(frame + 1, len(clip.rows) - 1)],
                        "joints": neighbors})

    return {"verdict": "FAIL" if (observed != expected or first_pose_mismatches
                                  or first_sole_mismatches or first_contact_mismatches) else "PASS",
            "frames_checked": len(clip.rows), "joint_samples_checked": len(clip.rows) * len(joints),
            "sole_samples_checked": len(clip.rows) * len(feet), "frame_period_s": float(clip.frame_time),
            "hierarchy_match": True, "first_pose_mismatch_frame_by_joint": first_pose_mismatches,
            "pose_mismatch_samples": changed_joint_samples, "first_contact_mismatch_frame_by_joint": first_contact_mismatches,
            "onset_errors": onset_errors, "first_sole_mismatch_frame_by_joint": first_sole_mismatches,
            "max_sole_error_um": {name: round(value, 4) for name, value in max_sole_error.items()},
            "max_planted_sole_displacement": {name: {"um": round(value, 4), "frame": frame}
                                             for name, (value, frame) in max_plant.items()},
            "joint_motion_peaks": peak_motion, "inspection_windows": windows, "native_policy_refusals": policy_refusals}
