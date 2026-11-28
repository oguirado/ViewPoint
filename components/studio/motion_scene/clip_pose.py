# SPDX-License-Identifier: AGPL-3.0-only
"""SET registration and Studio pose constraints."""
import numpy as np
from scipy.spatial.transform import Rotation


def yup(point):
    x, y, z = point
    return np.array([x, z, -y])


def target_at(scene, frame):
    cap = scene["button"]
    compression = np.interp(frame, np.array(cap["travel_key_frames"]) - 1, [0., 0., 1., 0.])
    rest = yup(cap["rest_contact_world_m"])
    pressed = yup(cap["depressed_contact_world_m"])
    return rest + (pressed - rest) * compression


def registered_pose(bases, roots, ancestry, motions, origins, walk_yaw, jab_yaw, api):
    start = api.STAGE_START
    stride = roots[api.STOP_START - 1, 2] - roots[0, 2]
    api.require(stride > 0, "invalid approach distance")
    fraction = (roots[:, 2] - roots[0, 2]) / stride
    scene_roots = start + fraction[:, None] * api.HEADING
    scene_roots[:, 1] = roots[:, 1]
    scene_roots += (roots[:, 0] - roots[0, 0])[:, None] * api.FACING[:, 0] * .25
    offset = api.STAGE_STOP - scene_roots[api.STOP_START - 1]
    offset[1] = 0
    scene_roots[api.STOP_START - 1:] += offset
    result = api.FACING @ bases
    for f, datum in enumerate(ancestry):
        moving = datum["clip"] == api.WALK
        motion = motions[datum["clip"]]
        sample_f = datum["source_frame"]
        i = int(sample_f)
        t = sample_f - i
        future = min(i + 1, len(motion["p"]) - 1)
        source = motion["p"][i] * (1 - t) + motion["p"][future] * t
        basis = walk_yaw if moving else jab_yaw
        names = motion["names"]
        lean = Rotation.from_matrix(bases[f,0]).as_rotvec()
        lean[1] = 0
        trunk = api.FACING @ Rotation.from_rotvec(np.clip(lean, -.045, .045)).as_matrix()
        result[f, [0,1,2,3,4,5,6,10]] = trunk
        for side, (shoulder, elbow, wrist) in api.ARMS.items():
            label = side.title()
            sign = 1 if side == "left" else -1
            upper = basis @ api.unit(source[names[label+"ForeArm"]] - source[names[label+"Arm"]])
            lower = basis @ api.unit(source[names[label+"Hand"]] - source[names[label+"ForeArm"]])
            upper_forward = np.clip(upper[2] * .38, -.22, .28)
            lower_forward = np.clip(lower[2] * .53, -.21, .40)
            upper_pose = np.array([.49 * sign, -.91, upper_forward])
            lower_pose = np.array([.12 * sign, -.94, lower_forward])
            if "walk_to_guard_blend" in datum:
                w = datum["walk_to_guard_blend"]
                for joint, desired in ((shoulder, upper_pose), (elbow, lower_pose)):
                    endpoint = trunk @ api.shortest(origins[joint+1] - origins[joint], desired)
                    result[f,joint] = api.interpolate(result[api.STOP_START - 1,joint], endpoint, w)
            else:
                result[f,shoulder] = trunk @ api.shortest(origins[elbow] - origins[shoulder], upper_pose)
                result[f,elbow] = trunk @ api.shortest(origins[wrist] - origins[elbow], lower_pose)
            result[f,wrist] = result[f,elbow]
    return result, scene_roots
