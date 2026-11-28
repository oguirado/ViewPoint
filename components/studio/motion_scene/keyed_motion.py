# SPDX-License-Identifier: AGPL-3.0-only
"""Studio's authored poses in glTF Y-up metres, replayed through REST and CLIPS."""
import numpy as np
from scipy.spatial.transform import Rotation

MASTER = "Studio_milestone_keyed"
ACTIONS = {MASTER: (0, 288), "Studio_keyed_waddle": (0, 120), "Studio_keyed_press": (144, 288)}
ARM = (7, 8, 9)
FACING_BONE = 1
HEAD = 5
ELBOW_MIN_DEG = 8.
ELBOW_MAX_DEG = 52.
EXTENSION_MAX_DEG = 30.
PRESS_YAW_DEG = 108.74
PRESS_LEAN_AXIS = (-.965, 0., -.262)
COLLAR_SWEEP_DEG = 21.86
JAB_POLE = (.55, 1.287, .83)
FOREARM_ROLL_DEG = -1.53
STRIKE_DIRECTION = (-.20, 0., -.98)
PLANT_XZ = (-.585, -.084)
FOOT_YAW_DEG = 48.
BOOT_YAW_DEG = 67.
TRACK_WIDEN_M = .052
SOLE_LIFT_M = .120
FLOOR_CLEARANCE_M = .00035
# Author contact marks directly so the short legs have time to swing past the
# planted boot. The last two marks register against the original press pose.
FOOTFALLS = ((2, 14, .10), (17, 29, .225), (32, 44, .355), (47, 59, .49),
             (62, 74, .625), (76, 88, .76), (90, 105, None), (110, 124, None))


def yaw(angle):
    return Rotation.from_rotvec([0., angle, 0.]).as_matrix()


def along(p, frame, keys):
    """Ease between explicit poses with zero velocity at a held pose."""
    for (start, first), (end, last) in zip(keys, keys[1:]):
        if start <= frame <= end:
            t = p.smooth((frame - start) / (end - start))
            return first * (1 - t) + last * t
    return keys[0][1] if frame < keys[0][0] else keys[-1][1]


def waddle(p, origins, rest_basis, raw, nodes, polish):
    start = p.STAGE_START.copy()
    stop = np.array([PLANT_XZ[0], 0., PLANT_XZ[1]])
    forward = p.unit(stop - start)
    walk_yaw = np.arctan2(forward[0], forward[2])
    approach_yaw = walk_yaw - np.radians(5.)
    plant_yaw = np.radians(FOOT_YAW_DEG)
    approach_basis = yaw(approach_yaw)
    plant_basis = yaw(plant_yaw)
    travel_m = np.linalg.norm(stop - start)
    feet_bases = {}
    feet = {}
    flags = {}
    tracks = {}
    transfers = np.zeros(p.COUNT)
    soles = polish.sole_specs(p, raw, {"nodes": nodes})
    for side, (_, _, ankle, _) in p.SIDES.items():
        side_sign = np.sign(origins[ankle, 0])
        lateral = approach_basis[:, 0] * (origins[ankle, 0] + side_sign * .025)
        final_lateral = plant_basis[:, 0] * (origins[ankle, 0] + side_sign * TRACK_WIDEN_M)
        tracks[side] = np.tile(lateral, (p.COUNT, 1))
        sole_y = soles[side]["positions"][:, 1].min()
        anchor = start + lateral
        anchor[1] = origins[ankle, 1] - sole_y + FLOOR_CLEARANCE_M
        feet[side] = np.tile(anchor, (p.COUNT, 1))
        boot_angle = approach_yaw
        boot_bind = yaw(boot_angle) @ rest_basis[ankle]
        feet_bases[side] = np.tile(boot_bind, (p.COUNT, 1, 1))
        flags[side] = np.ones(p.COUNT, dtype=bool)
        first_step = 0 if side == "right" else 1
        for begin, end, progress in FOOTFALLS[first_step::2]:
            next_anchor = anchor.copy()
            step_center = (stop + forward * (.016 * side_sign) if progress is None
                           else start + forward * travel_m * progress)
            next_lateral = final_lateral if progress is None else lateral
            next_angle = np.radians(BOOT_YAW_DEG) if progress is None else boot_angle
            next_anchor[[0, 2]] = (step_center + next_lateral)[[0, 2]]
            height = SOLE_LIFT_M if progress is not None else (.114 if side == "right" else .110)
            skew = .85 if progress is None and side == "right" else 1.
            for frame in range(begin, end + 1):
                phase = (frame - begin) / (end - begin)
                blend = p.smooth(min(phase / .90, 1.))
                lift = np.sin(np.pi * phase ** skew) ** 2
                feet[side][frame] = anchor * (1 - blend) + next_anchor * blend
                feet[side][frame, 1] += height * lift
                tracks[side][frame] = lateral * (1 - blend) + next_lateral * blend
                foot_yaw = yaw(boot_angle + (next_angle - boot_angle) * blend)
                boot_right = foot_yaw[:, 0]
                toe_up = Rotation.from_rotvec(boot_right * np.radians(-8. * lift)).as_matrix()
                feet_bases[side][frame] = toe_up @ foot_yaw @ rest_basis[ankle]
                flags[side][frame] = frame in (begin, end)
            feet[side][end + 1:] = next_anchor
            tracks[side][end + 1:] = next_lateral
            feet_bases[side][end + 1:] = yaw(next_angle) @ rest_basis[ankle]
            anchor = next_anchor
            lateral, boot_angle = next_lateral, next_angle
            for frame in range(begin, end + 2):
                transfer = p.smooth((frame - begin + 1) / 6) * p.smooth((end + 2 - frame) / 6)
                transfers[frame] -= side_sign * transfer

    roots, walk_bases = [], []
    torso = []
    for f in range(p.COUNT):
        # Carry the pelvis between the actual footmarks. Ease the forward drive
        # into the two-foot landing rather than translating through a planted pose.
        pace = min(f / 124., 1.)
        travel = pace * (1 - p.smooth((f - 110) / 14)) + p.smooth((f - 110) / 14)
        shift = transfers[f]
        # Match the hip stance to the authored tracks while the two boots turn
        # onto their original press marks on separate steps.
        track_axis = p.unit(tracks["left"][f] - tracks["right"][f])
        walk_basis = yaw(np.arctan2(-track_axis[2], track_axis[0]))
        outward = walk_basis[:, 0]
        walk_bases.append(walk_basis)
        settle = p.smooth((f - 119) / 10) - .42 * p.smooth((f - 129) / 12)
        landing = p.smooth((f - 124) / 6) * p.smooth((143 - f) / 13)
        stride_center = sum(feet[side][f] - offset[f] for side, offset in tracks.items()) / 2
        distance = .35 * travel_m * travel + .65 * (stride_center - start) @ forward
        walk_envelope = p.smooth(f / 18) * p.smooth((112 - f) / 18)
        bind_center = .5 * sum(origins[chain[2], 0] for chain in p.SIDES.values())
        track_center = .5 * (tracks["left"][f] + tracks["right"][f])
        transfer = (.088 + .022 * p.smooth((f - 89) / 7) * p.smooth((122 - f) / 5)
                    - .017 * p.smooth((f - 117) / 5))
        track_shift = .88 * p.smooth((124 - f) / 9)
        root = (start + forward * distance + track_shift * (track_center - outward * bind_center)
                + outward * (transfer * shift - .015 * walk_envelope))
        # Lower the body as weight transfers, rise over the planted leg, and
        # absorb the final landing on both soles before the reach starts.
        support = abs(shift)
        root[1] = (origins[0, 1] - .049 + .023 * support - .034 * settle
                   - .014 * walk_envelope * (1 - support) - .017 * landing)
        lower_height, upper_height = -np.inf, np.inf
        for side, (hip, knee, ankle, _) in p.SIDES.items():
            hip_offset = walk_basis @ (origins[hip] - origins[0])
            ab = np.linalg.norm(origins[knee] - origins[hip])
            bc = np.linalg.norm(origins[ankle] - origins[knee])
            separation = (root + hip_offset - feet[side][f])[[0, 2]]
            horizontal_sq = separation @ separation
            base = feet[side][f, 1] - hip_offset[1]
            lower_height = max(lower_height, base + np.sqrt(max(0., (abs(ab - bc) + .001) ** 2 - horizontal_sq)))
            upper_height = min(upper_height, base + np.sqrt(max(0., (ab + bc - .002) ** 2 - horizontal_sq)))
        p.require(lower_height <= upper_height,
                  f"keyed_leg_reach_envelope:{f + 1}:{lower_height:.6f}>{upper_height:.6f}")
        root[1] = np.clip(root[1], lower_height, upper_height)
        roots.append(root)
        turn = p.smooth((f - 100) / 43)
        face_travel = p.smooth((f - 1) / 34)
        approach_angle = walk_yaw - np.radians(62. - 36. * face_travel)
        angle = approach_angle + turn * (np.radians(PRESS_YAW_DEG) - approach_angle)
        open_up = p.smooth((f - 193) / 21)
        recovery_yaw = along(p, f + 1, ((216, 55.), (233, 60.), (252, 49.), (270, 43.), (288, 55.)))
        body = yaw(angle * (1 - open_up) + np.radians(recovery_yaw) * open_up)
        rock = Rotation.from_rotvec(body[:, 2] * np.radians(-4.5 * shift)).as_matrix()
        walk_pitch = Rotation.from_rotvec(body[:, 0] * np.radians(5. * walk_envelope + 4. * landing)).as_matrix()
        lean = along(p, f + 1, ((145, 0.), (156, 21.56), (180, 21.56), (199, 0.)))
        lean += 4.63 * polish.cap_fraction(f + 1)
        pitch = Rotation.from_rotvec(p.unit(np.array(PRESS_LEAN_AXIS)) * np.radians(lean)).as_matrix()
        torso.append(pitch @ walk_pitch @ rock @ body)
    return np.array(roots), np.array(torso), feet, feet_bases, flags, walk_bases, transfers


def matrices_for_frame(p, origin, basis, parents, root, walk_basis, torso, support_shift, f, polish, cap, screen):
    posed_basis = np.empty_like(basis)
    points = np.empty_like(origin)
    points[0] = root
    posed_basis[0] = walk_basis @ basis[0]
    for j, parent in enumerate(parents[1:], 1):
        carry = posed_basis[parent] @ basis[parent].T
        points[j] = points[parent] + carry @ (origin[j] - origin[parent])
        posed_basis[j] = carry @ basis[j]
        if j == FACING_BONE:
            posed_basis[j] = torso @ basis[j]
        if j == parents[ARM[0]]:
            windup = along(p, f + 1, ((145, 0.), (156, .64), (163, 1.), (180, 1.),
                                      (184, .52), (190, .12), (199, 0.)))
            windup *= COLLAR_SWEEP_DEG
            posed_basis[j] = yaw(np.radians(windup)) @ posed_basis[j]
        if j in (7, 11):
            envelope = p.smooth(f / 10) * p.smooth((124 - f) / 14)
            swing = np.radians(21. * support_shift) * envelope * (1 if j == 7 else -1)
            posed_basis[j] = Rotation.from_rotvec(torso[:, 0] * swing).as_matrix() @ posed_basis[j]
        if j == HEAD:
            initial = p.unit(torso[:, 2] + np.array([0., -.06, 0.]))
            target = p.unit(cap - points[j])
            look = p.smooth((f - 111) / 26)
            reaction = p.smooth((f - polish.RELEASE - 1) / 23)
            target[1] = np.clip(target[1], -.14, .14)
            target = p.unit((1 - reaction) * target + reaction * p.unit(screen - points[j]))
            direction = p.unit(initial * (1 - look) + target * look)
            posed_basis[j] = p.shortest(torso[:, 2], direction) @ torso @ basis[j]
            look_up = along(p, f + 1, ((216, 0.), (233, 8.), (252, -3.), (272, 2.), (285, 0.)))
            posed_basis[j] = Rotation.from_rotvec(torso[:, 0] * np.radians(look_up)).as_matrix() @ posed_basis[j]
    return points, posed_basis


def solve_leg(p, points, basis, chain, goal, foot_basis, toes_basis, forward, hinge_mix):
    hip, knee, ankle, toe = chain
    a, b, c = points[[hip, knee, ankle]]
    bent, reached = p.two_bone(a, b, c, goal, forward)
    p.require(np.linalg.norm(reached - goal) < .00005,
              f"keyed_leg_target_outside_reach:{np.linalg.norm(goal-a):.6f}:{np.linalg.norm(b-a):.6f}:"
              f"{np.linalg.norm(c-b):.6f}:{goal.tolist()}:{a.tolist()}")
    hip_rotation = p.shortest(b - a, bent - a)
    if hinge_mix:
        # Keep the side of the hinge consistent through a high swing. A
        # shortest-arc rotation alone becomes unstable near a folded thigh.
        def hinge_frame(segment, axle):
            direction = p.unit(segment)
            side = p.unit(axle - direction * (axle @ direction))
            return np.column_stack((direction, side, np.cross(direction, side)))

        bind = hinge_frame(b - a, np.cross(c - a, forward))
        swing = hinge_frame(bent - a, np.cross(reached - a, forward))
        hip_rotation = p.interpolate(hip_rotation, swing @ bind.T, hinge_mix)
    basis[hip] = hip_rotation @ basis[hip]
    basis[knee] = p.shortest(c - b, reached - bent) @ basis[knee]
    basis[ankle], basis[toe] = foot_basis, toes_basis


def strike_surface(p, raw, nodes, origin, basis):
    shoulder, elbow, wrist = ARM
    vertices, weights, joints, binds = p.skin_data(raw, nodes)
    ids = np.flatnonzero((weights[:, 0] == 1) & (joints[:, 0] == wrist))
    _, unique = np.unique(vertices[ids], axis=0, return_index=True)
    ids = ids[unique]
    local = vertices[ids] @ binds[wrist, :3, :3].T + binds[wrist, :3, 3]
    direction = p.unit(np.array(STRIKE_DIRECTION))
    aim_basis = p.shortest(origin[wrist] - origin[elbow], direction) @ basis[wrist]
    leading = np.argsort(local @ aim_basis[2], kind="stable")[:4]
    indices = ids[leading]
    marker = {"indices": indices.tolist(), "positions": vertices[indices], "weights": weights[indices],
              "joints": joints[indices], "inverse_binds": binds, "local_reach": local[leading].mean(axis=0)}
    return marker, aim_basis, local


def solve_strike(p, polish, points, basis, marker, aim_basis, glove, f, cap, pressed):
    shoulder, elbow, wrist = ARM
    frame = f + 1
    if frame < polish.ANTICIPATE or frame > polish.RECOVER:
        return
    a, b, c = points[[shoulder, elbow, wrist]]
    upper, lower = b - a, c - b
    reach = marker["local_reach"]
    cap_now = cap + (pressed - cap) * polish.cap_fraction(frame)
    target = cap_now - aim_basis @ reach
    target[2] += .00125
    # Pull the hand back and outward, then make one fast, legible approach.
    factor = along(p, frame, ((145, 0.), (151, .56), (155, .48), (163, 1.),
                               (180, 1.), (184, .60), (190, .10), (199, 0.)))
    hand_basis = p.interpolate(basis[wrist], aim_basis, factor)
    goal = c * (1 - factor) + target * factor
    goal += np.array([.08, .042, .045]) * np.sin(np.pi * factor)
    # Keep the anticipation on the camera flank, leaving the PLAY face in view.
    goal[2] += along(p, frame, ((155, 0.), (160, .045), (162, .105), (163, 0.), (180, 0.)))
    clear = along(p, frame, ((156, 0.), (160, .65), (162, 1.), (163, 0.), (180, 0.)))
    goal += np.array([-.045, -.131, .028]) * clear
    # Keep the whole leading glove surface out of the cap until the strike.
    goal[2] = max(goal[2], cap_now[2] + .0011 - float((glove @ hand_basis[2]).min()))
    ab, bc = np.linalg.norm(upper), np.linalg.norm(lower)
    bounds = np.sqrt(ab**2 + bc**2 + 2 * ab * bc * np.cos(np.radians([ELBOW_MAX_DEG, ELBOW_MIN_DEG])))
    line = p.unit(goal - a)
    goal = a + line * np.clip(np.linalg.norm(goal - a), *bounds)
    straight = p.unit(c - a)
    idle_pole = p.unit(upper - straight * (upper @ straight))
    jab_pole = p.unit(np.array(JAB_POLE))
    pole = p.unit(idle_pole * (1 - factor) + jab_pole * factor)
    bent, reached = p.two_bone(a, b, c, goal, pole)
    # Carry the upper sleeve's roll into the forearm before opening the elbow hinge.
    shoulder_swing = p.shortest(upper, bent - a)
    basis[shoulder] = shoulder_swing @ basis[shoulder]
    hinge = p.shortest(shoulder_swing @ lower, reached - bent)
    roll = Rotation.from_rotvec(p.unit(reached - bent) * np.radians(FOREARM_ROLL_DEG * factor)).as_matrix()
    basis[elbow] = roll @ hinge @ shoulder_swing @ basis[elbow]
    basis[wrist] = hand_basis


def compute(p, bvh, derived, rest, polish, raw):
    rests, parents, nodes, origins, rest_basis = p.target_rest(raw, rest)
    p.require(len(nodes) == 22 and np.allclose([joint.scale for joint in rests], 1.), "keyed_rig_units")
    scales = np.array([joint.scale for joint in rests])
    roots, torso, feet, foot_basis, flags, walk_bases, transfers = waddle(p, origins, rest_basis, raw, nodes, polish)
    markers = p.markers_for(raw, nodes)
    marker, aim_basis, glove = strike_surface(p, raw, nodes, origins, rest_basis)
    markers["hand"] = marker
    cap = p.clip_pose.yup(p.SET["button"]["rest_contact_world_m"])
    pressed = p.clip_pose.yup(p.SET["button"]["depressed_contact_world_m"])
    screen = p.clip_pose.yup(p.SET["screen"]["center_world_m"])
    authored, uncorrected = [], []
    for f, root in enumerate(roots):
        walk_basis = walk_bases[f]
        points, basis = matrices_for_frame(p, origins, rest_basis, parents, root, walk_basis,
                                           torso[f], transfers[f], f, polish, cap, screen)
        before = np.broadcast_to(np.eye(4), (len(nodes), 4, 4)).copy()
        before[:, :3, :3], before[:, :3, 3] = basis, points
        uncorrected.append(before)
        hinge_mix = p.smooth(f / 6) * p.smooth((124 - f) / 15)
        for side, chain in p.SIDES.items():
            boot = foot_basis[side][f]
            toes = boot @ rest_basis[chain[-2]].T @ rest_basis[chain[-1]]
            solve_leg(p, points, basis, chain, feet[side][f], boot, toes, walk_basis[:, 2], hinge_mix)
        solve_strike(p, polish, points, basis, marker, aim_basis, glove, f, cap, pressed)
        authored.append(basis @ rest_basis.transpose(0, 2, 1))
    # Keep the accepted rig adapter calibration identical to the pinned template.
    # The right-hand contact marker belongs to the keyed action, independently of
    # the fixed left-hand role in the certified REST map.
    pos, local, mapping, semantics, projected = p.rest_playback(
        derived, bvh, raw, rests, origins, parents, p.CMU, np.array(authored), roots)
    world = p.fk_local(pos, local, scales, parents)
    p.require(np.allclose(world[:, :, :3, :3], np.array(authored) @ rest_basis, atol=2e-6, rtol=0),
              "keyed_rest_pose_changed")
    return {"nodes": nodes, "parents": parents, "positions": pos, "local": local, "scales": scales,
            "world": world, "before": np.array(uncorrected), "markers": markers, "annotations": flags,
            "mapping": mapping, "semantics": semantics, "projected": projected}
