# SPDX-License-Identifier: AGPL-3.0-only
"""Quantify local volume loss on the Blender-evaluated rig."""
import numpy as np
from scipy.spatial import ConvexHull, QhullError, cKDTree

from rig_common import need

POSES = ("arms-up", "step", "jab")
LIMBS = {
    "right_arm": (7, 8, 9), "left_arm": (11, 12, 13),
    "right_leg": (14, 15, 16, 17), "left_leg": (18, 19, 20, 21),
}
MAX_LOSS = .25


def hull_volume(points):
    try:
        return float(ConvexHull(points).volume)
    except QhullError:
        return 0.0


def measure(work, xyz, joint_ids, weights, heads, tails):
    with np.load(work / "poses.npz") as source:
        geometries = {name: source[name] for name in ("rest", *POSES)}
    rest = geometries["rest"]
    distances, original = cKDTree(xyz).query(rest)
    max_neutral_error = float(distances.max())
    need(max_neutral_error < 1e-6 and len(rest) >= len(np.unique(xyz, axis=0)),
         "template_blender_neutral_replay")
    report = {pose: {} for pose in POSES}
    for limb, chain in LIMBS.items():
        influence = np.sum(weights[original] * np.isin(joint_ids[original], chain), axis=1)
        selected = influence >= .75
        need(np.count_nonzero(selected) >= 60, "template_insufficient_pose_coverage:" + limb)
        for pose in POSES:
            movement = np.linalg.norm(geometries[pose][selected] - rest[selected], axis=1)
            baseline = hull_volume(rest[selected])
            need(baseline > 0, "template_degenerate_pose_support:" + limb)
            ratio = hull_volume(geometries[pose][selected]) / baseline
            report[pose][limb] = {"convex_hull_volume_ratio": ratio, "support_vertices": int(selected.sum()),
                                  "max_displacement_m": float(movement.max()), "local_sections": {}}
        # Small overlapping sections expose local thinning which the full limb hull can hide.
        for joint in chain[:-1]:
            head, tail = heads[joint], tails[joint]
            axis = tail - head
            t = (rest - head) @ axis / (axis @ axis)
            radial = np.linalg.norm(rest - head - t[:, None] * axis, axis=1)
            candidates = ((t >= -.2) & (t <= 1.2) & (influence >= .45)
                          & (radial < max(.14, np.linalg.norm(axis) * .85)))
            for number, (lo, hi) in enumerate(((-.1, .45), (.30, .85), (.70, 1.15))):
                mask = candidates & (t >= lo) & (t <= hi)
                if np.count_nonzero(mask) < 16:
                    continue
                baseline = hull_volume(rest[mask])
                if baseline < 1e-7:
                    continue
                for pose in POSES:
                    posed = hull_volume(geometries[pose][mask])
                    report[pose][limb]["local_sections"][f"bone_{joint}:{number}"] = {
                        "volume_ratio": posed / baseline, "vertices": int(mask.sum()),
                        "rest_hull_m3": baseline, "pose_hull_m3": posed,
                    }
        need(all(len(report[p][limb]["local_sections"]) >= 2 for p in POSES),
             "template_insufficient_pose_sections:" + limb)
    min_ratio = min(section["volume_ratio"] for sides in report.values() for limb in sides.values()
                    for section in limb["local_sections"].values())
    # Rigidly bending a section can enlarge its enclosing hull. Read decreases as
    # local shrinkage and record the full-limb hull separately from that decision.
    flags = [{"pose": pose, "limb": limb, "section": key, "volume_loss": 1 - section["volume_ratio"]}
             for pose, row in report.items() for limb, measurements in row.items()
             for key, section in measurements["local_sections"].items()
             if section["volume_ratio"] < 1 - MAX_LOSS]
    height, width = rest[:, 1].max(), abs(rest[:, 0]).max()
    lower_body_shell = ((rest[:, 1] >= .24 * height) & (rest[:, 1] <= .49 * height)
                        & (abs(rest[:, 0]) <= .48 * width))
    need(np.count_nonzero(lower_body_shell) >= 40, "template_lower_shell_coverage")
    step_shell_motion = float(np.linalg.norm(
        (geometries["step"] - rest)[lower_body_shell], axis=1).max())
    need(step_shell_motion < .015, "template_step_distorts_lower_shell")
    need(all(max(limb["max_displacement_m"] for limb in row.values()) > .10 for row in report.values()),
         "template_pose_not_applied")
    return {"poses": report, "min_local_volume_ratio": min_ratio, "flagged_sections": flags,
            "volume_loss_threshold": MAX_LOSS, "max_blender_neutral_error_m": max_neutral_error,
            "max_step_lower_body_shell_displacement_m": step_shell_motion}

