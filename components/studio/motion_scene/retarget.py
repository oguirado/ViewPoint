# SPDX-License-Identifier: AGPL-3.0-only
"""Sample BVH clips, register them to the SET and round-trip animation through GLB."""
from decimal import Decimal
import json
import struct
import warnings

import numpy as np
from scipy.spatial.transform import Rotation

import bvh
import clip_pose
from evidence import checked_json, digest, demand as require, encode

FPS, COUNT = 24, 288
UP = np.array([0., 1., 0.])
SIDES = {"left": (18, 19, 20, 21), "right": (14, 15, 16, 17)}
ARMS = {"left": (11, 12, 13), "right": (7, 8, 9)}
CMU = ("Hips", "LowerBack", "Spine", "Spine1", "Neck1", "Head",
       "RightShoulder", "RightArm", "RightForeArm", "RightHand",
       "LeftShoulder", "LeftArm", "LeftForeArm", "LeftHand",
       "RightUpLeg", "RightLeg", "RightFoot", "RightToeBase",
       "LeftUpLeg", "LeftLeg", "LeftFoot", "LeftToeBase")
WALK, JAB = "walk", "jab"
WALK_PERIOD = 30
STOP_START, JAB_START, STRIKE, RECOVERY = 120, 144, 162, 192
PERIOD = Decimal("0.04166666666666667")

ACTIONS = {"studio_mocap": (0, COUNT), "studio_walk": (0, STOP_START), "studio_jab": (JAB_START, COUNT)}
DEFAULT_LIMITS = {
    "max_plant_slide_m_strict": .010,
    "max_adjacent_local_rotation_deg_strict": 60.,
    "max_joint_rotation_determinant_error_strict": 1e-9,
    "reach_tolerance_m_strict": .020,
    "min_guard_and_recovery_distance_m_strict": .05,
    "max_stop_to_jab_root_step_m_strict": .010,
    "max_walk_to_stop_root_step_m_strict": .05,
    "min_approach_root_travel_m_strict": 1.,
    "min_unlocked_plant_slide_m_strict": .01,
}


def load_clip(raw, start, stop):
    clip = bvh.parse(raw)
    require(0 <= start < stop < len(clip.rows), "invalid_source_frame_range")
    names = {joint.name: index for index, joint in enumerate(clip.joints)}
    require(set(CMU) <= names.keys(), "source_joint_inventory")
    values = np.array(clip.rows, dtype=float)
    points = np.empty((len(values), len(names), 3))
    rotations = np.empty((len(values), len(names), 3, 3))
    offsets = np.array([joint.offset for joint in clip.joints], dtype=float)
    neutral = offsets.copy()
    for j, joint in enumerate(clip.joints):
        width = 6 if joint.parent is None else 3
        require(len(joint.channels) == width, "unsupported_clip_channels")
        axes = "".join(channel[0] for channel in joint.channels[-3:])
        axes_slice = slice(joint.start + width - 3, joint.start + width)
        rot = Rotation.from_euler(axes, values[:, axes_slice], degrees=True).as_matrix()
        if joint.parent is None:
            points[:, j] = offsets[j] + values[:, :3]
            rotations[:, j] = rot
        else:
            parent = joint.parent
            neutral[j] += neutral[parent]
            points[:, j] = points[:, parent] + rotations[:, parent] @ offsets[j]
            rotations[:, j] = rotations[:, parent] @ rot
    for frame in {0, start, stop}:
        check_points, check_rotations, *_ = bvh.forward(clip, clip.rows[frame], 1)
        require(np.allclose(points[frame], check_points, atol=1e-10, rtol=0)
                and np.allclose(rotations[frame], check_rotations, atol=1e-10, rtol=0), "source_fk_mismatch")
    return {"clip": clip, "p": points, "r": rotations, "n": neutral, "names": names,
            "item": {"selection": {"source_start_frame": start, "source_end_frame_exclusive": stop}}}


def punch_extension(m, side):
    p, ids = m["p"], m["names"]
    shoulder = p[:, ids[side+"Arm"]]
    hand = p[:, ids[side+"Hand"]]
    lateral = p[:, ids["LeftArm"]] - p[:, ids["RightArm"]]
    forward = np.cross(lateral, UP)
    forward /= np.linalg.norm(forward, axis=1)[:,None]
    return forward, np.einsum("fi,fi->f", hand - shoulder, forward)


def unit(x):
    size = np.linalg.norm(x)
    require(size > 1e-9, "degenerate direction")
    return x / size


def shortest(a, b):
    a, b = unit(a), unit(b)
    axis = np.cross(a, b)
    sine, cosine = np.linalg.norm(axis), np.clip(a @ b, -1., 1.)
    if sine < 1e-10:
        if cosine > 0:
            return np.eye(3)
        axis = unit(np.cross(a, np.eye(3)[np.argmin(abs(a))]))
        return Rotation.from_rotvec(axis * np.pi).as_matrix()
    return Rotation.from_rotvec(axis * (np.arctan2(sine, cosine) / sine)).as_matrix()


def interpolate(a, b, mix):
    if mix == 0:
        return a
    return a @ Rotation.from_rotvec(Rotation.from_matrix(a.T @ b).as_rotvec() * mix).as_matrix()


def smooth(x):
    x = np.clip(x, 0., 1.)
    return float(x*x*(3 - 2*x))


def yaw_from(direction):
    return Rotation.from_rotvec(UP * -np.arctan2(direction[0], direction[2])).as_matrix()


def glb_chunks(raw):
    require(28 <= len(raw) <= 512 * 2**20, "GLB byte budget")
    magic, version, length, doc_length, doc_type = struct.unpack_from("<4sIII4s", raw)
    require((magic, version, length, doc_type) == (b"glTF", 2, len(raw), b"JSON"), "GLB header")
    binary_length, binary_type = struct.unpack_from("<I4s", raw, 20 + doc_length)
    require(doc_length % 4 == binary_length % 4 == 0 and binary_type == b"BIN\x00" and len(raw) == 28 + doc_length + binary_length, "GLB binary")
    return checked_json(raw[20:20 + doc_length]), raw[28 + doc_length:]


def accessor(doc, binary, index):
    spec = doc["accessors"][index]
    view = doc["bufferViews"][spec["bufferView"]]
    types = {5126: "<f4", 5121: "u1", 5123: "<u2", 5125: "<u4"}
    widths = {"SCALAR": 1, "VEC3": 3, "VEC4": 4, "MAT4": 16}
    dtype, columns = np.dtype(types[spec["componentType"]]), widths[spec["type"]]
    offset = view.get("byteOffset", 0) + spec.get("byteOffset", 0)
    require(not spec.get("sparse") and not spec.get("normalized"), "unsupported skin accessor")
    return np.ndarray((spec["count"], columns), dtype, binary, offset,
                      (view.get("byteStride", dtype.itemsize * columns), dtype.itemsize))


def target_rest(raw, adapter):
    native = adapter.read_rest(raw, digest(raw))
    rests = [native[f"bone_{i}"] for i in range(len(CMU))]
    parents = [None if joint.parent is None else int(joint.parent.split("_")[1]) for joint in rests]
    require(parents[0] is None and all(p is not None and p < j for j, p in enumerate(parents[1:], 1)),
            "unexpected target parent order")
    nodes = [joint.node for joint in rests]
    return rests, parents, nodes, np.array([joint.world_origin for joint in rests]), np.array(
        [joint.world_rotation for joint in rests])


def skin_data(raw, nodes):
    doc, binary = glb_chunks(raw)
    primitive, = doc["meshes"][0]["primitives"]
    fields = primitive["attributes"]
    vg = accessor(doc, binary, fields["POSITION"]).astype(float)
    weights = accessor(doc, binary, fields["WEIGHTS_0"]).astype(float)
    ids = accessor(doc, binary, fields["JOINTS_0"]).astype(int)
    skin, = doc["skins"]
    bind = accessor(doc, binary, skin["inverseBindMatrices"]).reshape(-1, 4, 4).transpose(0, 2, 1)
    skinned_node = next(node for node in doc["nodes"] if node.get("skin") == 0)
    require(set(skinned_node) == {"name", "skin", "mesh"}, "nonidentity mesh node")
    bone_numbers = np.array([nodes.index(node) for node in skin["joints"]])
    require(np.all(np.isfinite(weights)) and weights.min() >= 0 and
            np.max(abs(weights.sum(axis=1) - 1)) < 1e-5, "invalid studio rig blend weights")
    return vg, weights, bone_numbers[ids], bind[np.argsort(bone_numbers)]


def markers_for(raw, nodes):
    vertices, weights, bones, inverse_binds = skin_data(raw, nodes)
    markers = {}
    for side, (_, _, ankle, toe) in SIDES.items():
        direction = 1 if side == "left" else -1
        influence = np.sum(weights * np.isin(bones, (ankle, toe)), axis=1)
        viable = np.flatnonzero((vertices[:,0] * direction > .06) & (influence >= .97) & (vertices[:,1] < .018))
        require(len(viable) >= 12, f"insufficient sole markers: {side}")
        points = vertices[viable]
        rear, fore = np.quantile(points[:,2], [.18,.82])
        target_points = ((np.median(points[:,0]), rear), (np.median(points[:,0]), fore),
                         (np.quantile(points[:,0], .28), fore), (np.quantile(points[:,0], .72), fore))
        chosen = []
        for x, z in target_points:
            score = (points[:,0]-x)**2 + (points[:,2]-z)**2 + 4 * points[:,1]**2
            for i in np.argsort(score, kind="stable"):
                if not any(np.linalg.norm(points[i] - vertices[j]) < .009 for j in chosen):
                    chosen.append(int(viable[i]))
                    break
        require(len(chosen) == 4, "sole marker diversity")
        markers[side] = {"indices": chosen, "positions": vertices[chosen], "weights": weights[chosen],
                         "joints": bones[chosen], "inverse_binds": inverse_binds}
    hand_id = ARMS["left"][-1]
    h = np.flatnonzero(np.sum(weights * (bones == hand_id), axis=1) >= .995)
    points = vertices[h]
    local = np.column_stack([points, np.ones(len(h))]) @ inverse_binds[hand_id].T
    require(len(h) >= 16, "insufficient weighted hand markers")
    chosen = []
    for offset_x,offset_z in ((-.012,-.008),(.012,-.008),(-.012,.008),(.012,.008)):
        score = (local[:,0]-offset_x)**2 + (local[:,2]-offset_z)**2 + 4*(local[:,1]-.092)**2
        for i in np.argsort(score, kind="stable"):
            if not any(np.linalg.norm(points[i] - vertices[j]) < .007 for j in chosen):
                chosen.append(int(h[i]))
                break
    require(len(chosen) == 4, "hand marker diversity")
    homogeneous = np.column_stack([vertices[chosen],np.ones(len(chosen))])
    hand_local = np.mean((homogeneous @ inverse_binds[hand_id].T)[:,:3],axis=0)
    markers["hand"] = {"indices": chosen, "positions": vertices[chosen], "weights": weights[chosen],
                       "joints": bones[chosen], "inverse_binds": inverse_binds, "local_reach": hand_local}
    return markers


def skin_points(world, spec):
    bones = spec["joints"]
    posed = world[:,bones] @ spec["inverse_binds"][bones]
    vertices = np.column_stack([spec["positions"], np.ones(len(bones))])
    moved = np.einsum("fmwij,mj->fmwi", posed, vertices)
    return np.einsum("fmwi,mw->fmi", moved[...,:3], spec["weights"])


def anatomy(motion, parents, origins):
    names = list(CMU)
    lookup = motion["names"]
    source_index = [lookup[name] for name in names]
    corrections = []
    primary = {0: 2, 1: 2, 2: 3, 3: 4, 4: 5, 6: 7, 7: 8, 8: 9,
               10: 11, 11: 12, 12: 13, 14: 15, 15: 16, 16: 17, 18: 19, 19: 20, 20: 21}
    for j, parent in enumerate(parents):
        child = primary.get(j)
        if child is None:
            corrections.append(corrections[parent])
        else:
            source_axis = motion["n"][source_index[child]] - motion["n"][source_index[j]]
            target_axis = origins[child] - origins[j]
            corrections.append(shortest(target_axis, source_axis))
    return names, np.array(source_index), np.array(corrections)


def sample(m, t, indices):
    f = int(t)
    n = min(f+1, len(m["p"])-1)
    amount = t - f
    delta = m["r"][f,indices].transpose(0,2,1) @ m["r"][n,indices]
    rotation = m["r"][f,indices] @ Rotation.from_rotvec(Rotation.from_matrix(delta).as_rotvec() * amount).as_matrix()
    return m["p"][f,0]*(1-amount) + m["p"][n,0]*amount, rotation


def contact_source(m):
    result = {}
    for side in SIDES:
        toe = m["p"][:,m["names"][side.title()+"ToeBase"]]
        speed = np.linalg.norm(np.gradient(toe[:, [0,2]], axis=0), axis=1) / float(m["clip"].frame_time)
        selection = m["item"]["selection"]
        section = slice(selection["source_start_frame"], selection["source_end_frame_exclusive"])
        floor = np.min(toe[section,1])
        result[side] = (speed <= 10.) & (toe[:,1] <= floor + .65)
    return result


def fk_local(translations, rotations, scales, parents):
    count = len(translations)
    world = np.broadcast_to(np.eye(4), (count, len(parents), 4, 4)).copy()
    for j, parent in enumerate(parents):
        world[:,j,:3,:3] = rotations[:,j] * scales[j]
        world[:,j,:3,3] = translations[:,j]
        if parent is not None:
            world[:,j] = world[:,parent] @ world[:,j]
    return world


def intervals(flags):
    changes = np.flatnonzero(np.diff(np.r_[False, flags, False]))
    return [[int(a),int(b)] for a,b in changes.reshape(-1,2)]


def adapter_input(origins, parents, names, striking):
    rows = []
    for j, parent in enumerate(parents):
        offset = origins[j] if parent is None else origins[j] - origins[parent]
        end = None if j in parents else [0., .03, 0.]
        rows.append({"source": names[j], "target": f"bone_{j}", "parent": parent,
                     "source_rest_m": offset.tolist(), "target_rest_m": offset.tolist(),
                     "source_end_m": end, "target_end_m": end, "rotation_order": "ZXY"})
    roles = {"hips": 0, "left_foot": 21, "right_foot": 17, "striking_hand": striking}
    semantics = {"source": {role: names[j] for role,j in roles.items()},
                 "target": {role: f"bone_{j}" for role,j in roles.items()}}
    feet = {side: {"joint": f"bone_{chain[-1]}", "local_m": [0.,0.,0.], "floor_y_m": 0.}
            for side, chain in SIDES.items()}
    return {"schema": "vp3d.bvh_retarget_map/v1", "basis": "RH_Y_UP_METRE", "metres_scale": "1/1",
            "joints": rows, "feet": feet}, semantics


def rest_playback(derived, bvh, rig_raw, rests, origins, parents, names, bases, roots):
    mapping, semantics = adapter_input(origins, parents, names, ARMS["left"][-1])
    mapping_raw = encode(mapping)
    source, _, _ = derived.skeleton(mapping_raw, target=False)
    local = bases.copy()
    for j, parent in enumerate(parents):
        if parent is not None:
            local[:,j] = bases[:,parent].transpose(0,2,1) @ bases[:,j]
    with warnings.catch_warnings():
        # ZXY charts may reach a singularity. The matrix round trip below is the criterion.
        warnings.filterwarnings("ignore", message="Gimbal lock detected")
        angles = Rotation.from_matrix(local.reshape(-1,3,3)).as_euler("ZXY", degrees=True)
    require(np.allclose(Rotation.from_euler("ZXY", angles, degrees=True).as_matrix(), local.reshape(-1,3,3),
                        atol=2e-12, rtol=0), "projection Euler conversion altered rotation")
    rows = np.column_stack([roots - origins[0], angles.reshape(COUNT,-1)])
    clip = bvh.Clip(source, tuple(tuple(Decimal(str(round(value,12))) for value in row) for row in rows),
                    PERIOD, ())
    # Serialize and read through the supplied-BVH codec before calling accepted REST.
    projected_bytes = derived.render(clip)
    clip = bvh.parse(projected_bytes)
    retarget, binding = derived.retarget_unirig(clip, mapping_raw, rig_raw,
        admitted_rig_sha256=digest(rig_raw), semantics=semantics)
    frames = tuple(binding.playback(retarget))
    require(all(tuple(joint.node for joint in f) == tuple(joint.node for joint in rests) for f in frames),
            "adapter node identity")
    position = np.array([[joint.translation_m for joint in f] for f in frames])
    rotation = np.array([[joint.rotation for joint in f] for f in frames])
    world = fk_local(position, rotation, np.array([joint.scale for joint in rests]), parents)
    require(np.allclose(world[:,0,:3,3], roots, atol=2e-11, rtol=0), "root or clock lost in REST")
    return position, rotation, mapping, semantics, projected_bytes


def two_bone(a, b, c, goal, pole):
    ab, bc = np.linalg.norm(b-a), np.linalg.norm(c-b)
    direction = unit(goal-a)
    reach = np.clip(np.linalg.norm(goal-a), abs(ab-bc)+.0002, ab+bc-.0003)
    middle = (ab*ab - bc*bc + reach*reach) / (2*reach)
    pole = pole - (pole @ direction)*direction
    if np.linalg.norm(pole) < 1e-7:
        pole = np.cross(direction, [1.,0.,0.])
    elbow = a + middle*direction + np.sqrt(max(0.,ab*ab-middle*middle))*unit(pole)
    return elbow, a + reach*direction


class Retarget:
    """An isolated SET registration and retarget profile for one invocation."""

    COUNT, CMU, SIDES, ARMS, UP = COUNT, CMU, SIDES, ARMS, UP
    STOP_START, WALK = STOP_START, WALK
    require, unit, smooth, shortest, interpolate = map(staticmethod, (require, unit, smooth, shortest, interpolate))
    skin_data, skin_points, intervals = map(staticmethod, (skin_data, skin_points, intervals))
    target_rest, markers_for, two_bone = map(staticmethod, (target_rest, markers_for, two_bone))
    rest_playback, fk_local = map(staticmethod, (rest_playback, fk_local))
    clip_pose = clip_pose

    def __init__(self, set_doc, *, plant_offset=(.17, .04, 0.)):
        require(set_doc["units"] == "metres, Z up" and (set_doc["fps"], set_doc["end_frame"]) == (FPS, COUNT)
                and set_doc["markers"]["HAND_CONTACT"] == STRIKE + 1, "SET registration mismatch")
        self.SET = set_doc
        framing = set_doc["robot_framing"]
        self.BUTTON = clip_pose.target_at(set_doc, STRIKE)
        self.STAGE_START = clip_pose.yup(framing["approach_root_world_m"])
        self.STAGE_STOP = clip_pose.yup(np.add(framing["plant_root_world_m"], plant_offset))
        self.HEADING = self.STAGE_STOP - self.STAGE_START
        require(np.linalg.norm(self.HEADING[[0, 2]]) > 0, "invalid SET approach")
        self.FACING = Rotation.from_rotvec(UP * np.arctan2(self.HEADING[0], self.HEADING[2])).as_matrix()
        self.FORWARD = self.FACING[:, 2]


    def schedule(self, motions, parents, origins, jab_interval):
        walk, jab = motions[WALK], motions[JAB]
        names, walk_indices, walk_axes = anatomy(walk, parents, origins)
        _, jab_indices, jab_axes = anatomy(jab, parents, origins)
        pick = walk["item"]["selection"]
        begin, stop = pick["source_start_frame"], pick["source_end_frame_exclusive"]
        source_period = stop - begin
        first = walk["p"][begin,0]
        stride = walk["p"][stop,0] - first
        stride[1] = 0
        walk_yaw = yaw_from(stride)
        jab_yaw = yaw_from(punch_extension(jab, "Left")[0][jab_interval[1]])
        # Use a leg-length ratio. This measures character translation scale without assuming
        # an absolute physical unit for the source conversion.
        metric = np.mean([sum(np.linalg.norm(origins[c] - origins[p]) for p,c in zip(chain[:2],chain[1:3]))
                          for chain in SIDES.values()])
        source = np.mean([sum(np.linalg.norm(walk["n"][walk["names"][side.title()+c]] -
                                          walk["n"][walk["names"][side.title()+p]])
                                    for p,c in (("UpLeg","Leg"),("Leg","Foot"))) for side in SIDES])
        scale = metric / source
        flags = contact_source(walk)
        annotations = {side: [] for side in SIDES}
        bases, positions, ancestry = [], [], []
        walk_anchor = None
        jab_base, jab_pose = sample(jab, jab_interval[0], jab_indices)
        for frame in range(COUNT):
            if frame < STOP_START:
                cycle, phase = divmod(frame, WALK_PERIOD)
                source_frame = begin + phase * source_period / WALK_PERIOD
                root, pose = sample(walk, source_frame, walk_indices)
                relative = root - first + cycle * stride
                root = origins[0] + scale * (walk_yaw @ relative)
                pose = walk_yaw @ pose @ walk_axes
                for side in SIDES:
                    annotations[side].append(bool(flags[side][int(round(source_frame))]))
                ancestry.append({"clip": WALK, "source_frame": source_frame, "cycle": cycle})
                walk_anchor, walk_end_pose = root, pose
            elif frame < JAB_START:
                weight = smooth((frame - STOP_START + 1) / (JAB_START - STOP_START))
                root = walk_anchor.copy()
                pose = np.array([interpolate(a,b,weight) for a,b in zip(walk_end_pose, jab_yaw @ jab_pose @ jab_axes)])
                for side in SIDES:
                    annotations[side].append(True)
                ancestry.append({"clip": JAB, "source_frame": jab_interval[0], "walk_to_guard_blend": weight})
            else:
                first_jab, peak, last_jab = jab_interval
                if frame <= STRIKE:
                    t = first_jab + (frame-JAB_START) * (peak-first_jab) / (STRIKE-JAB_START)
                elif frame < RECOVERY:
                    t = peak + (frame-STRIKE) * (last_jab-peak) / (RECOVERY-STRIKE)
                else:
                    t = last_jab
                source_root, source_pose = sample(jab, t, jab_indices)
                root = walk_anchor + scale * (jab_yaw @ (source_root-jab_base))
                pose = jab_yaw @ source_pose @ jab_axes
                if frame >= RECOVERY:
                    weight = smooth((frame-RECOVERY) / 35)
                    pose = np.array([interpolate(a,b,weight) for a,b in zip(pose, jab_yaw @ jab_pose @ jab_axes)])
                    root = root*(1-weight) + walk_anchor*weight
                for side in SIDES:
                    annotations[side].append(True)
                ancestry.append({"clip": JAB, "source_frame": t})
            bases.append(pose)
            positions.append(root)
        bases, roots = clip_pose.registered_pose(np.array(bases), np.array(positions), ancestry, motions,
                                              origins, walk_yaw, jab_yaw, self)
        return names, bases, roots, annotations, ancestry, scale, walk_yaw, jab_yaw

    def solve(self, world, positions, local, scales, parents, origins, rest_world, markers, annotations):
        solve_position, solve_local = positions.copy(), local.copy()
        palm_local = markers["hand"]["local_reach"]
        floor_points = {side: skin_points(world, markers[side]) for side in SIDES}
        nominal = world[:,:,:3,3]
        offs = {}
        active = {}
        for side, (_,_,ankle,toe) in SIDES.items():
            marker_height = np.mean(markers[side]["positions"][:,1])
            offs[side] = origins[ankle,1] - marker_height
            active[side] = []
            for start, stop in intervals(annotations[side]):
                anchor = nominal[start,ankle].copy()
                anchor[1] = offs[side]
                active[side].append((start,stop,anchor))

        soft = {side: np.zeros(COUNT) for side in SIDES}
        for f in range(COUNT):
            pose = world[f].copy()
            feet = {}
            root_delta = np.zeros(3)
            for side, (hip,knee,ankle,toe) in SIDES.items():
                chosen = None
                for a,b,anchor in active[side]:
                    weight = smooth(min((f-a+5)/5,(b-f+4)/5))
                    if weight > soft[side][f]:
                        soft[side][f], chosen = weight, anchor
                amount = soft[side][f]
                goal = pose[ankle,:3,3].copy()
                # Lift the sole only as needed in swings. Stance constraints straighten toe roll.
                goal[1] += max(0., -.002 - float(np.min(floor_points[side][f,:,1])))
                if chosen is not None:
                    goal = goal*(1-amount) + chosen*amount
                feet[side] = goal
            for _ in range(14):
                for side,(hip,knee,ankle,_) in SIDES.items():
                    h,k,a = pose[[hip,knee,ankle],:3,3]
                    bound = np.linalg.norm(k-h)+np.linalg.norm(a-k)-.0007
                    delta = feet[side] - (h+root_delta)
                    distance = np.linalg.norm(delta)
                    if distance > bound:
                        root_delta += delta * (1-bound/distance)
            solve_position[f,0] += root_delta
            pose[:,:3,3] += root_delta
            for side, (hip,knee,ankle,toe) in SIDES.items():
                h,k,a = pose[[hip,knee,ankle],:3,3]
                bent, reached = two_bone(h,k,a,feet[side], self.FORWARD + UP * .04)
                side_mix = soft[side][f]
                ankle_r = interpolate(Rotation.from_matrix(pose[ankle,:3,:3]).as_matrix(),
                                      self.FACING @ rest_world[ankle],side_mix)
                toe_r = interpolate(Rotation.from_matrix(pose[toe,:3,:3]).as_matrix(),
                                    self.FACING @ rest_world[toe],side_mix)
                desired = {
                    hip: shortest(k-h,bent-h) @ Rotation.from_matrix(pose[hip,:3,:3]).as_matrix(),
                    knee: shortest(a-k,reached-bent) @ Rotation.from_matrix(pose[knee,:3,:3]).as_matrix(),
                    ankle: ankle_r, toe: toe_r,
                }
                for j, basis in desired.items():
                    parent = parents[j]
                    parent_basis = desired.get(parent)
                    if parent_basis is None:
                        parent_basis = Rotation.from_matrix(pose[parent,:3,:3]).as_matrix()
                    solve_local[f,j] = parent_basis.T @ basis
            shoulder, elbow, wrist = ARMS["left"]
            if STRIKE - 18 <= f <= STRIKE + 21:
                if f < STRIKE:
                    weight = smooth((f - STRIKE + 18)/18)
                elif f <= self.SET["markers"]["BUTTON_BOTTOMED"] - 1:
                    weight = 1.
                else:
                    weight = smooth((STRIKE + 21 - f)/15)
                cap = clip_pose.target_at(self.SET, f)
                a,b,c = pose[[shoulder,elbow,wrist],:3,3]
                wrist_basis = Rotation.from_matrix(pose[wrist,:3,:3]).as_matrix()
                forearm = unit(c - b)
                # Solve to the palm along the forearm chain, then recover the wrist
                # from the original forearm length. Transport the wrist with the
                # forearm so nearby weighted skin stays coherent during extension.
                palm = c + forearm * np.linalg.norm(palm_local)
                direction = unit(palm - a)
                old_pole = (b - a) - direction * ((b - a) @ direction)
                pole = unit(old_pole) * (1 - weight) + (-UP + .68 * self.FACING[:,0]) * weight
                goal = palm * (1 - weight) + cap * weight
                bent, reached_palm = two_bone(a,b,palm,goal,pole)
                lower_dir = unit(reached_palm - bent)
                swing = shortest(forearm,lower_dir)
                aligned_wrist = shortest(wrist_basis @ palm_local,forearm) @ wrist_basis
                hand_rotation = swing @ interpolate(wrist_basis,aligned_wrist,weight)
                shoulder_r = shortest(b-a,bent-a) @ Rotation.from_matrix(pose[shoulder,:3,:3]).as_matrix()
                elbow_r = swing @ Rotation.from_matrix(pose[elbow,:3,:3]).as_matrix()
                collar_r = Rotation.from_matrix(pose[parents[shoulder],:3,:3]).as_matrix()
                solve_local[f,shoulder] = collar_r.T @ shoulder_r
                solve_local[f,elbow] = shoulder_r.T @ elbow_r
                solve_local[f,wrist] = elbow_r.T @ hand_rotation
        return solve_position, solve_local, fk_local(solve_position, solve_local, scales, parents)

    def metrics(self, world, local, parents, markers, annotations, positions, before, button):
        per_side = {}
        for side in SIDES:
            points = skin_points(world, markers[side])
            original = skin_points(before, markers[side])
            rows = []
            for start, stop in intervals(annotations[side]):
                path = points[start:stop]
                slide = np.linalg.norm(path[:,:,[0,2]]-path[0,:,[0,2]].T, axis=2)
                raw_path = original[start:stop]
                raw_slide = np.linalg.norm(raw_path[:,:,[0,2]]-raw_path[0,:,[0,2]].T, axis=2)
                rows.append({"frames": [start,stop], "horizontal_slide_m": float(slide.max()),
                             "horizontal_slide_before_m": float(raw_slide.max()),
                             "floor_error_m": float(abs(path[:,:,1]).max())})
            per_side[side] = rows
        rel = local[:-1].transpose(0,1,3,2) @ local[1:]
        angle = np.degrees(Rotation.from_matrix(rel.reshape(-1,3,3)).magnitude()).reshape(COUNT-1,-1)
        i, j = np.unravel_index(np.argmax(angle), angle.shape)
        hand = skin_points(world, markers["hand"]).mean(axis=1)
        untouched_hand = skin_points(before, markers["hand"]).mean(axis=1)
        distance = np.linalg.norm(hand - button, axis=1)
        during_press = range(STRIKE, self.SET["markers"]["BUTTON_BOTTOMED"])
        press_errors = [float(np.linalg.norm(hand[f] - clip_pose.target_at(self.SET,f))) for f in during_press]
        joint_angles = np.linalg.det(local)
        return {"stance": per_side,
                "max_stance_horizontal_slide_m": max(row["horizontal_slide_m"] for rows in per_side.values() for row in rows),
                "max_unlocked_stance_slide_m": max(row["horizontal_slide_before_m"] for rows in per_side.values()
                                                  for row in rows),
                "max_stance_floor_error_m": max(row["floor_error_m"] for rows in per_side.values() for row in rows),
                "max_frame_local_rotation_deg": float(angle[i,j]), "max_rotation_frame": int(i+1),
                "max_rotation_joint": int(j), "max_rotation_determinant_error": float(abs(joint_angles-1).max()),
                "jab_contact_error_m": float(distance[STRIKE]), "hand_contact_m": hand[STRIKE].tolist(),
                "max_press_window_error_m": max(press_errors),
                "press_window_error_m_by_frame": dict(zip(during_press,press_errors,strict=True)),
                "closest_hand_frame": int(np.argmin(distance)),
                "hand_guard_distance_m": float(distance[JAB_START]),
                "hand_recover_distance_m": float(distance[RECOVERY+24]),
                "approach_root_travel_m": float(np.linalg.norm(positions[STOP_START-1,0,[0,2]] - positions[0,0,[0,2]])),
                "join_root_step_m": float(np.linalg.norm(positions[JAB_START,0]-positions[JAB_START-1,0])),
                "stop_root_step_m": float(np.linalg.norm(positions[STOP_START,0]-positions[STOP_START-1,0])),
                "reach_shoulder_m": world[STRIKE,11,:3,3].tolist(),
                "hand_before_ik_m": untouched_hand[STRIKE].tolist()}

    def compute(self, bvh, derived, adapter, raw, motions, jab_interval):
        rests, parents, nodes, origins, world_rest = target_rest(raw, adapter)
        markers = markers_for(raw, nodes)
        names, bases, roots, annotations, ancestry, scale, walk_yaw, jab_yaw = self.schedule(motions, parents, origins, jab_interval)
        pos, local, mapping, semantics, projected = rest_playback(derived, bvh, raw, rests, origins, parents, names, bases, roots)
        scales = np.array([rest.scale for rest in rests])
        before = fk_local(pos, local, scales, parents)
        anim_pos, anim_local, final = self.solve(before, pos, local, scales, parents, origins, world_rest, markers, annotations)
        return {"markers": markers, "annotations": annotations, "mapping": mapping, "semantics": semantics, "projected": projected,
                "ancestry": ancestry, "scale": scale, "walk_yaw": walk_yaw, "jab_yaw": jab_yaw, "parents": parents,
                "nodes": nodes, "world": final, "positions": anim_pos, "local": anim_local, "scales": scales, "before": before}

    def evaluate(self, state, reference):
        return self.metrics(state["world"], state["local"], state["parents"], reference["markers"], reference["annotations"],
                       state["positions"], state["before"], self.BUTTON)


def export_gltf(raw, state, actions=ACTIONS):
    doc, original_bin = glb_chunks(raw)
    require(not doc.get("animations") and len(doc["buffers"]) == 1, "unexpected raw GLB animation")
    data = bytearray(original_bin)

    def add(values, kind):
        values = np.ascontiguousarray(values, dtype="<f4")
        payload = values.tobytes()
        view = len(doc["bufferViews"])
        doc["bufferViews"].append({"buffer": 0, "byteOffset": len(data), "byteLength": len(payload)})
        data.extend(payload)
        result = {"bufferView": view, "componentType": 5126, "type": kind, "count": len(values)}
        if kind == "SCALAR":
            result.update(min=[float(values[0])], max=[float(values[-1])])
        doc["accessors"].append(result)
        return len(doc["accessors"])-1

    quaternions = Rotation.from_matrix(state["local"].reshape(-1,3,3)).as_quat(canonical=True).reshape(len(state['local']), -1, 4)
    for i in range(1, len(quaternions)):
        flipped = np.einsum("ij,ij->i", quaternions[i-1], quaternions[i]) < 0
        quaternions[i,flipped] *= -1
    doc["animations"] = []
    for name, (start,stop) in actions.items():
        channels, samplers = [], []
        time = add(np.arange(stop-start) / FPS, "SCALAR")
        for j, node in enumerate(state["nodes"]):
            properties = [("rotation", quaternions[start:stop,j], "VEC4")]
            if j == 0:
                properties.append(("translation", state["positions"][start:stop,j], "VEC3"))
            for path, values, kind in properties:
                channels.append({"sampler": len(samplers), "target": {"node": node, "path": path}})
                samplers.append({"input": time, "output": add(values,kind), "interpolation": "LINEAR"})
        doc["animations"].append({"name": name, "channels": channels, "samplers": samplers})
    doc["buffers"][0]["byteLength"] = len(data)
    contents = json.dumps(doc, separators=(",", ":"), allow_nan=False).encode()
    contents += b" " * (-len(contents) % 4)
    return (struct.pack("<4sIII4s", b"glTF", 2, 28+len(contents)+len(data), len(contents), b"JSON")
            + contents + struct.pack("<I4s", len(data),b"BIN\x00") + data)


def read_animation(raw, animation_raw, state, actions=ACTIONS):
    doc, data = glb_chunks(animation_raw)
    old, previous_data = glb_chunks(raw)
    require(data.startswith(previous_data), "skin, geometry or inverse bind bytes changed")
    for key in ("asset", "nodes", "skins", "meshes", "scene", "scenes"):
        require(doc[key] == old[key], f"raw rig metadata changed: {key}")
    for key in ("accessors", "bufferViews"):
        require(doc[key][:len(old[key])] == old[key], f"raw rig resource pointer changed: {key}")
    require([item["name"] for item in doc["animations"]] == list(actions), "named animation inventory")
    parsed = {}
    for animation in doc["animations"]:
        name = animation["name"]
        start, stop = actions[name]
        static_translations = np.array([doc["nodes"][node].get("translation",[0.,0.,0.])
                                        for node in state["nodes"]])
        translations = np.broadcast_to(static_translations, (stop-start,*static_translations.shape)).copy()
        rotations = np.empty((stop-start,len(state["nodes"]),3,3))
        seen = set()
        for channel in animation["channels"]:
            node, prop = channel["target"]["node"], channel["target"]["path"]
            j = state["nodes"].index(node)
            require((j,prop) not in seen, "duplicated animation channel")
            seen.add((j,prop))
            sampler = animation["samplers"][channel["sampler"]]
            t = accessor(doc, data, sampler["input"])[:,0]
            values = accessor(doc, data, sampler["output"])
            require(sampler["interpolation"] == "LINEAR" and np.array_equal(
                t, (np.arange(stop-start)/FPS).astype("<f4")), "serialized GLB clock")
            if prop == "rotation":
                require(np.max(abs(np.linalg.norm(values,axis=1)-1)) < 1e-6 and
                        np.all(np.einsum("ij,ij->i", values[1:],values[:-1]) > 0), "quaternion branch discontinuity")
                rotations[:,j] = Rotation.from_quat(values).as_matrix()
            elif prop == "translation" and j == 0:
                translations[:,j] = values
            else:
                raise RuntimeError("unexpected animated GLB property")
        require(seen == ({(j,"rotation") for j in range(len(state["nodes"]))} | {(0,"translation")}),
                "incomplete GLB channel set")
        parsed[name] = (translations, rotations)
    translations, rotations = parsed[next(iter(actions))]
    for name, (start,stop) in list(actions.items())[1:]:
        require(np.array_equal(translations[start:stop], parsed[name][0]) and
                np.array_equal(rotations[start:stop], parsed[name][1]), "named animation differs from master")
    scales = np.array([doc["nodes"][node].get("scale",[1.,1.,1.]) for node in state["nodes"]])
    return state | {"positions": translations, "local": rotations,
                    "world": fk_local(translations, rotations, scales, state["parents"])}


def gate(report, limits=DEFAULT_LIMITS):
    require(report["max_stance_horizontal_slide_m"] < limits["max_plant_slide_m_strict"], "foot_slide")
    require(report["max_stance_floor_error_m"] < limits["max_plant_slide_m_strict"], "foot_floor_distance")
    require(report["max_frame_local_rotation_deg"] < limits["max_adjacent_local_rotation_deg_strict"] and
            report["max_rotation_determinant_error"] < limits["max_joint_rotation_determinant_error_strict"], "joint_flip")
    require(max(report["jab_contact_error_m"], report["max_press_window_error_m"]) < limits["reach_tolerance_m_strict"] and
            min(report["hand_guard_distance_m"], report["hand_recover_distance_m"]) >
            limits["min_guard_and_recovery_distance_m_strict"], "jab_reach")
    require(report["join_root_step_m"] < limits["max_stop_to_jab_root_step_m_strict"] and
            report["stop_root_step_m"] < limits["max_walk_to_stop_root_step_m_strict"] and
            report["approach_root_travel_m"] > limits["min_approach_root_travel_m_strict"], "root_alignment")
    require(report["max_unlocked_stance_slide_m"] > limits["min_unlocked_plant_slide_m_strict"], "stance_oracle_insensitive")
