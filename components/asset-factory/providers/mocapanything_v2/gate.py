"""G07 measurements on authenticated BVH and independently supplied sole traces.

Bone-only evidence never establishes surface clearance. Observed-motion PASS
also needs a calibrated policy; synthetic fixture PASS is explicitly scoped.
"""

import math
import time

from asset_factory import core
from . import bvh
from .contract import digest, inspect_rig, load, ROOT, validate


def _distance(a, b, axes):
    return math.ceil(math.sqrt(sum((a[k] - b[k]) ** 2 for k in axes)))


def evaluate(data, rig, reference, observation, *, request_sha256, video_sha256,
             policy=None, scope="observed_motion", deadline_ns=None):
    started = time.monotonic_ns()
    if deadline_ns is None:
        deadline_ns = started + 10**10
    report = {"schema": "af.motion-contact/v1", "rc": 3, "status": "INCOMPLETE",
              "verdict": "CANNOT_SEE", "scope": scope, "reason_codes": [],
              "bvh_sha256": digest(data), "rig_sha256": digest(rig),
              "request_sha256": request_sha256, "video_sha256": video_sha256,
              "reference_sha256": None, "observation_sha256": None,
              "policy_sha256": None, "checks_evaluated": 0, "metrics": {}, "wall_ms": 0}
    try:
        bvh.check_time(deadline_ns)
        bvh.require(scope in ("synthetic_fixture", "observed_motion"), 2, "scope_invalid")
        policy = load(ROOT / "contact-policy.json", "policy") if policy is None else policy
        for value, schema in ((reference, "reference"), (observation, "observation"), (policy, "policy")):
            validate(value, schema)
        report.update(reference_sha256=core.sha256_json(reference),
                      observation_sha256=core.sha256_json(observation), policy_sha256=core.sha256_json(policy))
        # Bounds are frozen in this revision. Calibration is an integration task,
        # never a flag the caller can set to turn an unmeasured asset green.
        bvh.require(policy == load(ROOT / "contact-policy.json", "policy"), 4, "policy_stale")
        bvh.require(reference["rig_sha256"] == digest(rig), 4, "reference_stale")
        for key, expected in (("bvh_sha256", digest(data)), ("rig_sha256", digest(rig)),
                              ("request_sha256", request_sha256), ("video_sha256", video_sha256),
                              ("reference_sha256", core.sha256_json(reference))):
            bvh.require(observation[key] == expected, 4, "observation_stale")
        for key in (request_sha256, video_sha256):
            bvh.require(type(key) is str and len(key) == 64
                        and all(c in "0123456789abcdef" for c in key), 2, "identity_invalid")
        bvh.require(reference["basis"] == "RH_Y_UP" and reference["units_um"] > 0,
                    2, "basis_invalid")
        clip = bvh.parse(data, deadline_ns=deadline_ns)
        rig_joints = inspect_rig(rig)
        expected = reference["joints"]
        bvh.require(len(expected) == len(clip.joints) and len(rig_joints) == len(expected),
                    5, "skeleton_mismatch")
        names = [j["name"] for j in expected]
        mapped = [j["rig_name"] for j in expected]
        bvh.require(all(len(j["offset_um"]) == 3 for j in expected), 2, "vector_invalid")
        bvh.require(len(set(names)) == len(names) and len(set(mapped)) == len(mapped), 2, "bone_map_invalid")
        name_to_rig = dict(zip(names, mapped))
        for joint, spec in zip(clip.joints, expected):
            bvh.require(joint.name == spec["name"] and spec["rig_name"] in rig_joints,
                        5, "skeleton_mismatch")
            parent = None if joint.parent is None else clip.joints[joint.parent].name
            actual = rig_joints[spec["rig_name"]]
            bvh.require(spec["parent"] == parent and actual["parent"] == name_to_rig.get(parent),
                        5, "skeleton_mismatch")
            offsets = [v * reference["units_um"] for v in joint.offset]
            bvh.require(all(abs(offsets[k] - spec["offset_um"][k]) <= 1
                            and abs(actual["offset_um"][k] - spec["offset_um"][k]) <= 1
                            for k in range(3)), 5, "rest_pose_mismatch")
        bvh.require(observation["measurement_kind"] != "bone_only", 3, "sole_surface_unobserved")
        bvh.require((scope == "synthetic_fixture") == (observation["measurement_kind"] == "synthetic_fixture"),
                    2, "evidence_scope_mismatch")
        n = len(clip.rows)
        fps_num, fps_den = reference["fps_num"], reference["fps_den"]
        bvh.require(fps_num > 0 and fps_den > 0, 2, "fps_invalid")
        bvh.require(abs(clip.frame_time * fps_num - fps_den) <= clip.frame_time / 1000,
                    5, "fps_mismatch")
        bvh.require(n == reference["frame_count"] == len(observation["frames"]), 5, "frame_count_mismatch")
        bvh.require(n >= policy["minimum_frames"], 3, "clip_too_short")
        feet = reference["feet"]
        bvh.require(bool(feet) and set(feet) <= set(names), 3, "feet_unbound")
        bvh.require(all(len(j["offset_um"]) == 3 for j in expected)
                    and all(len(v) == 3 for v in feet.values()), 2, "vector_invalid")
        bvh.require(set(reference["contacts"]) == set(feet), 3, "contact_coverage_missing")
        for foot in feet:
            mask = reference["contacts"][foot]
            bvh.require(len(mask) == n and all(type(bit) is int and bit in (0, 1) for bit in mask),
                        3, "contact_coverage_missing")
            bvh.require(sum(mask) >= policy["minimum_stance_frames"], 3, "stance_unobserved")
            bvh.require(any(a == c == 1 for a, c in zip(mask, mask[1:])), 3, "stance_interval_unobserved")
        max_rotation, max_gap, max_slide, max_speed, max_height = 0, 0, 0, 0, 0
        min_clearance = None
        last_rot = None
        anchors, last_sole = {}, {}
        for index, row in enumerate(clip.rows):
            bvh.check_time(deadline_ns)
            frame = observation["frames"][index]
            bvh.require(frame["frame_id"] == index and frame["source_index"] == index,
                        5, "frame_identity_mismatch")
            bvh.require(frame["pose_sha256"] == clip.frame_hashes[index], 4, "pose_frame_stale")
            source_hash = frame["source_rgb_sha256"]
            bvh.require(len(source_hash) == 64 and all(c in "0123456789abcdef" for c in source_hash),
                        2, "frame_hash_invalid")
            bvh.require(set(frame["sole_um"]) == set(feet) and set(frame["contacts"]) == set(feet),
                        3, "sole_surface_unobserved")
            bvh.require(all(len(v) == 3 for v in frame["sole_um"].values()), 2, "vector_invalid")
            positions, rotations, local = bvh.forward(clip, row, reference["units_um"])
            if last_rot is not None:
                max_rotation = max(max_rotation, max(bvh.rotation_delta_mdeg(a, c) for a, c in zip(last_rot, local)))
            last_rot = local
            for foot, offset in feet.items():
                bvh.require(frame["contacts"][foot] == reference["contacts"][foot][index],
                            5, "contact_relabelled")
                joint_index = names.index(foot)
                delta = bvh.rotate(rotations[joint_index], offset)
                predicted = tuple(round(positions[joint_index][k] + delta[k]) for k in range(3))
                sole = frame["sole_um"][foot]
                max_gap = max(max_gap, _distance(sole, predicted, (0, 1, 2)))
                clearance = sole[1] - reference["floor_y_um"]
                min_clearance = clearance if min_clearance is None else min(min_clearance, clearance)
                if reference["contacts"][foot][index]:
                    max_height = max(max_height, clearance)
                    if foot not in anchors:
                        anchors[foot] = sole
                    max_slide = max(max_slide, _distance(sole, anchors[foot], (0, 2)))
                    if foot in last_sole:
                        max_speed = max(max_speed, _distance(sole, last_sole[foot], (0, 1, 2)))
                    last_sole[foot] = sole
                else:
                    anchors.pop(foot, None)
                    last_sole.pop(foot, None)
        metrics = {"frames": n, "fps_num": fps_num, "fps_den": fps_den,
                   "max_rotation_mdeg_per_frame": max_rotation, "max_surface_fk_gap_um": max_gap,
                   "min_clearance_um": min_clearance, "max_plant_height_um": max_height,
                   "max_stance_slide_um": max_slide, "max_plant_speed_um_per_frame": max_speed,
                   "contact_coverage_ppm": 1000000}
        report["metrics"] = metrics
        failed = []
        for condition, code in ((max_rotation > policy["max_rotation_mdeg_per_frame"], "bone_rotation_continuity"),
                                (max_gap > policy["max_surface_fk_gap_um"], "sole_binding_mismatch"),
                                (min_clearance < policy["min_clearance_um"], "feet_floor"),
                                (max_height > policy["max_plant_height_um"], "plant_height"),
                                (max_slide > policy["max_stance_slide_um"], "foot_slide"),
                                (max_speed > policy["max_plant_speed_um_per_frame"], "plant_speed")):
            if condition:
                failed.append(code)
        report["checks_evaluated"] = 6
        if failed:
            report["reason_codes"] = failed
            rc = 5
        elif scope != "synthetic_fixture":
            report["reason_codes"] = ["contact_thresholds_uncalibrated", "source_capture_unverified"]
            rc = 3
        else:
            rc = 0
    except bvh.Refusal as exc:
        rc, report["reason_codes"] = exc.rc, [exc.reason]
    except core.ContractError as exc:
        rc, report["reason_codes"] = 2, [exc.code]
    except (KeyError, TypeError, ValueError, RecursionError, OverflowError) as exc:
        rc, report["reason_codes"] = 2, ["motion_evidence_invalid"]
    report.update(rc=rc, status=core.STATUS[rc], verdict=core.VERDICT[rc],
                  wall_ms=(time.monotonic_ns() - started) // 1000000)
    return report
