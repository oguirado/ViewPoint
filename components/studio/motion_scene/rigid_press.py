# SPDX-License-Identifier: AGPL-3.0-only
"""Aim the rigid glove's leading surface at PLAY, so the shell stops at the cap."""
import numpy as np
from scipy.spatial.transform import Rotation


ELBOW_POLE = (1., .81, -.59)
TORSO_TURN = -.80


def register_floor(p, polish, raw, state):
    """Take the stance sole from the completed IK solve to the SET origin."""
    height = np.full(p.COUNT, np.inf)
    for side, spec in polish.sole_specs(p, raw, state).items():
        lower = p.skin_points(state["world"], spec)[:, :, 1].min(axis=1)
        stance = np.asarray(state["annotations"][side], dtype=bool)
        height[stance] = np.minimum(height[stance], lower[stance])
    if not np.isfinite(height).all():
        raise RuntimeError("rigid_motion_unsupported_stance")
    state["positions"][:, 0, 1] -= height
    state["world"] = p.fk_local(state["positions"], state["local"], state["scales"], state["parents"])
    return state


def aim_press(p, polish, raw, original, state):
    shoulder, elbow, wrist = polish.ARM
    parents = state["parents"]
    vertices, weights, joints, binds = p.skin_data(raw, state["nodes"])
    hand_ids = np.flatnonzero((weights[:, 0] == 1) & (joints[:, 0] == wrist))
    _, unique = np.unique(vertices[hand_ids], axis=0, return_index=True)
    hand_ids = hand_ids[unique]
    palm = vertices[hand_ids] @ binds[wrist, :3, :3].T + binds[wrist, :3, 3]
    lock_frame = polish.CONTACT - 1
    wrist_basis = Rotation.from_matrix(state["world"][lock_frame, wrist, :3, :3]).as_matrix()
    # The contact witness is the foremost patch, in the actual press direction.
    # The older wrist-relative marker falls inside this shell at this pose.
    foremost = np.argsort(palm @ wrist_basis[2], kind="stable")[:4]
    indices = hand_ids[foremost]
    reach = palm[foremost].mean(axis=0)
    marker = {"indices": indices.tolist(), "positions": vertices[indices], "weights": weights[indices],
              "joints": joints[indices], "inverse_binds": binds, "local_reach": reach}
    state["markers"] = state["markers"] | {"hand": marker}

    local = state["local"]
    # Use torso yaw rather than folding the elbow back through the shoulder.
    # The planted leg chains are siblings of the rotating torso joint.
    for frame in range(120, 199):
        f = frame - 1
        weight = p.smooth(min((frame - 120) / 30, (199 - frame) / 14))
        basis = Rotation.from_matrix(state["world"][f, 0, :3, :3]).as_matrix()
        turn = Rotation.from_rotvec(p.UP * TORSO_TURN * weight).as_matrix()
        local[f, 1] = basis.T @ turn @ basis @ local[f, 1]
    source_local = local.copy()
    before = original["before"][:, :, :3, :3]
    bases = Rotation.from_matrix(before.reshape(-1, 3, 3)).as_matrix().reshape(before.shape)
    source_local[:, polish.ARM] = bases[:, [parents[j] for j in polish.ARM]].transpose(0, 1, 3, 2) @ bases[:, polish.ARM]
    source = p.fk_local(state["positions"], source_local, state["scales"], parents)
    cap = p.clip_pose.yup(p.SET["button"]["rest_contact_world_m"])
    pressed = p.clip_pose.yup(p.SET["button"]["depressed_contact_world_m"])
    press_pole = p.unit(np.asarray(ELBOW_POLE))
    for frame in range(polish.ANTICIPATE, polish.RECOVER + 1):
        f = frame - 1
        upper, fore, hand = source[f, [shoulder, elbow, wrist], :3, 3]
        bases = Rotation.from_matrix(source[f, :, :3, :3]).as_matrix()
        factor = p.smooth(min((frame - polish.ANTICIPATE) / (polish.CONTACT - polish.ANTICIPATE),
                              (polish.RECOVER - frame) / (polish.RECOVER - polish.RELEASE)))
        target = cap + (pressed - cap) * polish.cap_fraction(frame)
        hand_basis = p.interpolate(bases[wrist], wrist_basis, factor)
        goal = hand * (1 - factor) + (target - hand_basis @ reach) * factor
        # Arc outside the torso and in front of the collar on the way in and out.
        goal += np.array([.265, 0., .095]) * np.sin(np.pi * factor)
        # Prevent early entry even if the swinging glove passes the cap center
        # before the marker reaches its contact pose.
        if polish.ANTICIPATE < frame < polish.RECOVER:
            clearance = .001 + .026 * (1 - factor)
            goal[2] = max(goal[2], target[2] + clearance - float((palm @ hand_basis[2]).min()))
        line = p.unit(hand - upper)
        source_pole = p.unit((fore - upper) - line * ((fore - upper) @ line))
        pole = source_pole * (1 - factor) + press_pole * factor
        bend, reached = p.two_bone(upper, fore, hand, goal, pole)
        upper_basis = p.shortest(fore - upper, bend - upper) @ bases[shoulder]
        fore_basis = p.shortest(hand - fore, reached - bend) @ bases[elbow]
        local[f, shoulder] = bases[parents[shoulder]].T @ upper_basis
        local[f, elbow] = upper_basis.T @ fore_basis
        local[f, wrist] = fore_basis.T @ hand_basis
    state["world"] = p.fk_local(state["positions"], local, state["scales"], parents)
    return state
