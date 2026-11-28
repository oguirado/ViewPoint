# SPDX-License-Identifier: AGPL-3.0-only
"""Motion refinement and shared contact contract for the studio template take."""

SCHEMA = "viewpoint.motion-scene.polish/v1"
CONTACT, BOTTOM, POWER, HOLD_END, RELEASE = 163, 169, 171, 175, 180
ANTICIPATE, RECOVER = 145, 199
ARM = (11, 12, 13)
ROOT = 0
PRESS_TURN_RAD = -1.
ELBOW_UP = -.6
ELBOW_OUT = -1.5
PALM_AIM = (.74, .0, -.67)
PRESS_CAMERA = (5.071, -1.359, 2.55)
PRESS_LENS_MM = 49
RECOVERY_LENS_MM = 45
RECOVERY_CAMERA = (4.4, -2.3, 2.18)
CAMERA_TARGET = (-.04, .26, .86)
GLYPH_FRAME = 199
GLYPH = "PLAY triangle"
GLYPH_XZ_M = ((-.047, .059), (-.047, -.059), (.061, 0.))
GLYPH_FACE_OFFSET_M = -.0012
# Sample the cap face around the palm so a covered center can still show travel.
CAP_SIGHT_XZ_M = ((.068, -.034), (.02, -.069), (-.06, .035), (-.061, -.049), (-.06, .065),
                  (-.015, .078), (.045, .069), (.079, .029), (.025, -.079), (-.077, 0))
SLIDE_M = 0.010
FLOOR_M = 0.005
PENETRATION_M = 0.002
CONTACT_M = 0.010
ROOT_REPLAY_M = 0.010
HAND_REPLAY_M = 0.001
TRAVEL_TOLERANCE_M = 0.001
ARM_SAMPLES = 5
SIGHT_FRAMES = tuple(range(153, 186))


from evidence import Rejected


def cap_fraction(frame, hold_end=HOLD_END):
    keys = ((1, 0), (CONTACT, 0), (BOTTOM, 1), (hold_end, 1), (RELEASE, 0))
    for (a, start), (b, end) in zip(keys, keys[1:]):
        if a <= frame <= b:
            return start + (end - start) * (frame - a) / (b - a)
    return 0.


def verify_set(set_doc):
    markers, button, screen = (set_doc[key] for key in ("markers", "button", "screen"))
    if (markers["HAND_CONTACT"], markers["BUTTON_BOTTOMED"], markers["BUTTON_RELEASED"]) != (
            CONTACT, BOTTOM, RELEASE) or (set_doc["fps"], set_doc["end_frame"]) != (24, 288):
        raise RuntimeError("polish_set_timeline_changed")
    if (button["axis_world"] != [0, 1, 0] or button["object"] != "PLAY Button"
            or screen["object"] != "CRT Screen" or not 0 < button["travel_m"] < .1):
        raise RuntimeError("polish_set_registration_changed")


def sole_specs(p, raw, state):
    import numpy as np

    vertices, weights, joints, binds = p.skin_data(raw, state["nodes"])
    specs = {}
    for side, chain in p.SIDES.items():
        sign = 1 if side == "left" else -1
        influence = np.sum(weights * np.isin(joints, chain[-2:]), axis=1)
        # Capture the curved shoe underside, including edges below the old markers.
        ix = np.flatnonzero((vertices[:, 0] * sign > .03)
                            & (vertices[:, 1] < .04) & (influence >= .90))
        if len(ix) < 12 or vertices[ix, 1].min() > .005:
            raise RuntimeError("polish_sole_surface_missing:" + side)
        specs[side] = {"positions": vertices[ix], "weights": weights[ix],
                       "joints": joints[ix], "inverse_binds": binds}
    return specs


def correct(p, raw, state):
    import numpy as np
    from scipy.spatial.transform import Rotation

    positions, local = state["positions"].copy(), state["local"].copy()
    world = state["world"]
    parents, scales = state["parents"], state["scales"]
    soles = sole_specs(p, raw, state)
    sole_y = {side: p.skin_points(world, spec)[:, :, 1].min(axis=1)
              for side, spec in soles.items()}
    points = world[:, :, :3, 3]
    goals, blend = {}, {}
    for side, (_, _, ankle, _) in p.SIDES.items():
        goals[side] = points[:, ankle].copy()
        goals[side][:, 1] += np.maximum(0., .0003 - sole_y[side])
        blend[side] = np.zeros(p.COUNT)
        previous = 0
        for start, stop in p.intervals(state["annotations"][side]):
            # Preserve the source swing and ease onto each fixed world-space footprint.
            if start > previous:
                t = np.linspace(0., 1., start - previous + 2)[1:-1]
                goals[side][previous:start, 1] += .020 * np.sin(np.pi * t)**2
            previous = stop
            anchor = points[start, ankle].copy()
            anchor[1] += .0003 - sole_y[side][start]
            for f in range(max(0, start - 3), min(p.COUNT, stop + 3)):
                w = p.smooth(min((f - start + 4) / 4, (stop - f + 3) / 4))
                if w > blend[side][f]:
                    goals[side][f] = (1 - w) * goals[side][f] + w * anchor
                    blend[side][f] = w

    # Carry the pelvis toward each supporting side, then transfer to the arriving
    # left foot before anticipation. This intentionally keeps both soles down at the stop.
    lateral = p.FACING[:, 0]
    forward = p.FORWARD
    for f in range(p.STOP_START):
        balance = blend["left"][f] - blend["right"][f]
        positions[f, 0] += lateral * (.022 * balance)
    for f in range(p.STOP_START, p.COUNT):
        settle = p.smooth((f - 119) / 18)
        ease = p.smooth((f - 137) / 13)
        dip = np.sin(np.pi * p.smooth((f - 118) / 28))
        positions[f, 0] += lateral * (.074 * settle - .022 * ease)
        positions[f, 0] += forward * (.037 * settle - .023 * ease) - p.UP * (.027 * dip + .008 * settle)

    for f in range(p.COUNT):
        pose = world[f].copy()
        delta = positions[f, 0] - state["positions"][f, 0]
        # Keep the hip inside both reachable spheres before aiming the leg chains.
        for _ in range(14):
            for side, (hip, knee, ankle, _) in p.SIDES.items():
                h, k, a = points[f, [hip, knee, ankle]]
                limit = np.linalg.norm(k-h) + np.linalg.norm(a-k) - .001
                offset = goals[side][f] - h - delta
                if np.linalg.norm(offset) > limit:
                    delta += offset * (1 - limit / np.linalg.norm(offset))
        positions[f, 0] = state["positions"][f, 0] + delta
        pose[:, :3, 3] += delta
        bases = Rotation.from_matrix(pose[:, :3, :3]).as_matrix()
        for side, (hip, knee, ankle, toe) in p.SIDES.items():
            h, k, a = pose[[hip, knee, ankle], :3, 3]
            bent, reached = p.two_bone(h, k, a, goals[side][f], forward + .04 * p.UP)
            desired = {hip: p.shortest(k-h, bent-h) @ bases[hip],
                       knee: p.shortest(a-k, reached-bent) @ bases[knee],
                       ankle: bases[ankle], toe: bases[toe]}
            for j, basis in desired.items():
                local[f, j] = desired.get(parents[j], bases[parents[j]]).T @ basis

    result = state | {"positions": positions, "local": local}
    press_and_react(p, result)
    result["world"] = p.fk_local(positions, local, scales, parents)
    return result


def press_and_react(p, state):
    import numpy as np
    from scipy.spatial.transform import Rotation

    local, parents = state["local"], state["parents"]
    # Open the torso toward the camera from the planted pelvis, then turn back
    # with the withdrawal. Leg chains are siblings and retain their world locks.
    for frame in range(121, 216):
        f = frame - 1
        turn = p.smooth(min((frame - 121) / 28, (215 - frame) / 30))
        carry = Rotation.from_matrix(state["world"][f, 0, :3, :3]).as_matrix()
        turn_r = Rotation.from_rotvec(p.UP * PRESS_TURN_RAD * turn).as_matrix()
        local[f, 1] = carry.T @ turn_r @ carry @ local[f, 1]
    source_local = local.copy()
    before = state["before"][:, :, :3, :3]
    source_bases = Rotation.from_matrix(before.reshape(-1, 3, 3)).as_matrix().reshape(before.shape)
    parent_arm = [parents[j] for j in ARM]
    source_local[:, ARM] = source_bases[:, parent_arm].transpose(0, 1, 3, 2) @ source_bases[:, ARM]
    pose = p.fk_local(state["positions"], local, state["scales"], parents)
    source = p.fk_local(state["positions"], source_local, state["scales"], parents)
    shoulder, elbow, wrist = ARM
    palm_local = state["markers"]["hand"]["local_reach"]
    rest = p.clip_pose.yup(p.SET["button"]["rest_contact_world_m"])
    pressed = p.clip_pose.yup(p.SET["button"]["depressed_contact_world_m"])
    aim = p.unit(np.asarray(PALM_AIM)) * np.linalg.norm(palm_local)
    # Bend toward the camera side of the enclosure while preserving the palm target.
    press_pole = ELBOW_UP * p.UP + ELBOW_OUT * p.FACING[:, 0]
    for frame in range(ANTICIPATE, RECOVER + 1):
        f = frame - 1
        a, b, c = source[f, [shoulder, elbow, wrist], :3, 3]
        bases = Rotation.from_matrix(source[f, :, :3, :3]).as_matrix()
        forearm = p.unit(c - b)
        w = p.smooth(min((frame - ANTICIPATE) / (CONTACT - ANTICIPATE),
                         (RECOVER - frame) / (RECOVER - RELEASE)))
        target = rest + (pressed - rest) * cap_fraction(frame)
        goal = c * (1 - w) + (target - aim) * w
        old_pole = (b - a) - forearm * ((b - a) @ forearm)
        pole = p.unit(old_pole) * (1 - w) + press_pole * w
        bent, reached = p.two_bone(a, b, c, goal, pole)
        lower = p.unit(reached - bent)
        swing = p.shortest(forearm, lower)
        upper_r = p.shortest(b-a, bent-a) @ bases[shoulder]
        elbow_r = swing @ bases[elbow]
        transported = swing @ bases[wrist]
        aligned = p.shortest(transported @ palm_local, aim) @ transported
        hand_r = p.interpolate(transported, aligned, w)
        local[f, shoulder] = bases[parents[shoulder]].T @ upper_r
        local[f, elbow] = upper_r.T @ elbow_r
        local[f, wrist] = elbow_r.T @ hand_r

    for frame in range(RELEASE + 1, p.COUNT + 1):
        f = frame - 1
        look = p.smooth((frame - RELEASE - 1) / 36)
        follow = p.smooth((frame - 200) / 36)
        nod = p.smooth((frame - 239) / 11) - p.smooth((frame - 251) / 18)
        for bone, yaw, pitch in ((2, .22 * follow, -.07 * follow), (5, .24 * look, .23 * nod)):
            base = Rotation.from_matrix(pose[f, bone, :3, :3]).as_matrix()
            carry = Rotation.from_matrix(pose[f, parents[bone], :3, :3]).as_matrix()
            turn = Rotation.from_rotvec(p.UP * yaw + p.FACING[:, 0] * pitch).as_matrix()
            local[f, bone] = carry.T @ turn @ base


def measure_motion(p, raw, replay, *, arm=ARM):
    import numpy as np

    verify_set(p.SET)
    world = replay["world"]
    feet = {}
    for side, spec in sole_specs(p, raw, replay).items():
        pts = p.skin_points(world, spec)
        rows = []
        for start, end in p.intervals(replay["annotations"][side]):
            anchor = pts[start][:, [0, 2]]
            rows.extend({"frame": f + 1, "stance_start": start + 1,
                         "height_m": float(pts[f, :, 1].min()),
                         "slide_m": float(np.linalg.norm(pts[f][:, [0, 2]] - anchor, axis=1).max())}
                        for f in range(start, end))
        feet[side] = rows
    hand = p.skin_points(world, replay["markers"]["hand"]).mean(axis=1)
    axes = np.array([1., -1., 1.])
    hand_world = hand[:, [0, 2, 1]] * axes
    wrist_world = world[:, arm[-1], [0, 2, 1], 3] * axes
    root_world = world[:, ROOT, [0, 2, 1], 3] * axes
    hand_rest = replay["markers"]["hand"]["positions"][:, [0, 2, 1]] * axes
    return {"schema": SCHEMA, "feet": feet, "hand_world_m": hand_world.tolist(), "wrist_world_m": wrist_world.tolist(),
            "root_world_m": root_world.tolist(),
            "hand_vertex_indices": replay["markers"]["hand"]["indices"],
            "hand_rest_world_m": hand_rest.tolist(),
            "settle_pelvis_delta_m": float(np.linalg.norm(world[139, 0, [0, 2]]
                                                         - world[120, 0, [0, 2]]))}


def motion_rejections(report):
    if report["schema"] != SCHEMA or set(report["feet"]) != {"left", "right"}:
        raise RuntimeError("polish_motion_schema")
    rows = [row for side in report["feet"].values() for row in side]
    for side in report["feet"].values():
        frames = [row["frame"] for row in side]
        if frames != sorted(set(frames)) or any(
                not 1 <= row["stance_start"] <= row["frame"] <= 288 for row in side):
            raise RuntimeError("polish_invalid_stance_frames")
    coverage = {row["frame"] for row in rows}
    if not all(report["feet"].values()) or coverage != set(range(1, 289)):
        raise RuntimeError("polish_stance_coverage")
    reasons = []
    if any(not -PENETRATION_M <= row["height_m"] <= FLOOR_M for row in rows):
        reasons.append("polish_foot_height")
    if any(not 0 <= row["slide_m"] <= SLIDE_M for row in rows):
        reasons.append("polish_foot_slide")
    if not .035 <= report["settle_pelvis_delta_m"] <= .13:
        reasons.append("polish_pelvis_settle")
    return reasons


def motion_gate(report):
    reasons = motion_rejections(report)
    if reasons:
        raise Rejected(",".join(reasons))


def assess_interaction(set_doc, motion, samples, sightlines, glyph_visible, *, arm=ARM, hold_end=HOLD_END):
    """Evaluate SET and the imported root and palm against the pinned GLB replay."""
    import math

    verify_set(set_doc)
    motion_gate(motion)
    if ([sample["frame"] for sample in samples] != list(range(1, 289))
            or any(len(motion[key]) != 288 for key in ("root_world_m", "hand_world_m"))):
        raise RuntimeError("polish_interaction_frame_coverage")
    if set(sightlines) != {str(f) for f in SIGHT_FRAMES}:
        raise RuntimeError("polish_camera_frame_coverage")

    button = set_doc["button"]
    rest = button["rest_contact_world_m"]
    travel = button["travel_m"]
    displacements, emission, distances, wrist_clearance = [], [], [], []
    root_errors, hand_errors = [], []
    for sample, expected_root, expected_palm in zip(
            samples, motion["root_world_m"], motion["hand_world_m"], strict=True):
        cap, strength, palm = sample["cap_world_m"], sample["screen_emission"], sample["hand_world_m"]
        points = (cap, palm, sample["wrist_world_m"], sample["root_world_m"], expected_root, expected_palm)
        if (any(len(point) != 3 or not all(map(math.isfinite, point)) for point in points)
                or not math.isfinite(strength)):
            raise RuntimeError("polish_invalid_scene_sample")
        displacements.append(cap[1] - rest[1])
        emission.append(strength)
        distances.append(math.dist(cap, palm))
        root_errors.append(math.dist(sample["root_world_m"], expected_root))
        hand_errors.append(math.dist(palm, expected_palm))
        wrist_clearance.append(cap[1] - sample["wrist_world_m"][1])

    peak = max(displacements)
    lit = [f for f, value in enumerate(emission, 1) if value > .01]
    screen_on = min(lit, default=None)
    window = None
    if screen_on is not None and distances[screen_on - 1] <= CONTACT_M:
        first = last = screen_on
        while first > 1 and distances[first - 2] <= CONTACT_M:
            first -= 1
        while last < len(distances) and distances[last] <= CONTACT_M:
            last += 1
        window = [first, last]

    reasons = []
    if max(root_errors) > ROOT_REPLAY_M:
        reasons.append("polish_blender_root_replay")
    if max(hand_errors) > HAND_REPLAY_M:
        reasons.append("polish_blender_hand_replay")
    if (abs(peak - travel) > TRAVEL_TOLERANCE_M
            or any(math.dist(sample["cap_world_m"], (rest[0], rest[1] + travel * cap_fraction(f, hold_end), rest[2]))
                   > TRAVEL_TOLERANCE_M for f, sample in enumerate(samples, 1))):
        reasons.append("polish_button_travel")
    if (window is None or window[0] > CONTACT or window[1] < RELEASE
            or window[0] < CONTACT - 3 or window[1] > RELEASE + 5
            or distances[RECOVER - 1] < .05
            or min(wrist_clearance[CONTACT - 1:RELEASE]) < .02):
        reasons.append("polish_hand_contact_window")
    if (screen_on != POWER or lit != list(range(POWER, 289))
            or any(abs(emission[f - 1] - set_doc["screen"]["on_emission_strength"]) > .01 for f in lit)
            or abs(displacements[POWER - 1] - travel) > TRAVEL_TOLERANCE_M or window is None
            or not window[0] < POWER < window[1]):
        reasons.append("polish_screen_outside_pressed_hold")

    for frame, sight in sightlines.items():
        if set(sight["arm"]) != {f"bone_{i}" for i in arm}:
            raise RuntimeError("polish_arm_sample_roster")
        if (not sight["cap_visible"] or not sight["screen_visible"]
                or any(row["visible_samples"] < ARM_SAMPLES or len(row["uv"]) != 2
                       or not all(.015 <= value <= .985 for value in row["uv"])
                       for row in sight["arm"].values())):
            reasons.append("polish_striking_arm_or_prop_visibility:" + frame)
    if not glyph_visible:
        reasons.append("polish_play_glyph_hidden")
    return {"schema": SCHEMA, "status": "REJECT" if reasons else "PASS", "reasons": reasons,
            "button_travel_m": peak, "screen_on_frame": screen_on, "hand_contact_frames": window,
            "max_press_hand_distance_m": max(distances[CONTACT - 1:RELEASE]),
            "max_blender_root_replay_error_m": max(root_errors),
            "max_blender_hand_replay_error_m": max(hand_errors),
            "samples": samples, "sightlines": sightlines, "glyph_visible": glyph_visible}
