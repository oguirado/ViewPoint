# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic control rigs for the oracle's own gate (numpy only, written as GLB).

The test article is a small seated mechanism in the character's rest frame: a
post on a flat base plate, an arm on a coaxial ring joint held by a two-plate
clevis at the post top, and a hand on a second ring joint in a clevis at the
end of the arm.  Each ring turns about the common axis of its clevis and
stays seated; bars attach only to their own ring and keep clear of the mating
part.  Every damaged variant changes exactly one thing, so each rejection
class has a control that is clean on the other classes.
"""
import itertools

import numpy as np

from . import glb

POSE_SCHEMA = "rp4.pose-oracle.pose.v1"
EXTERIOR, INTERIOR = 0, 1
SEGMENTS = 48
CLEARANCE = 0.0002

# Joint pivots in Blender world axes (Z up, metres).
HIPS = np.array([0.0, 0.0, 0.0])
SHOULDER = np.array([0.0, 0.0, 0.30])
WRIST = np.array([0.0, -0.24, 0.30])
POST2 = np.array([0.09, 0.0, 0.0])
PARENT = {"hips": None, "left_shoulder": "hips", "left_wrist": "left_shoulder", "post2": "hips"}
JOINTS = ("hips", "left_shoulder", "left_wrist", "post2")
TRAVEL = 45.0  # degrees each way; the hand stays 10 mm above the floor at full travel

GOOD = {"wrist_pivot_offset": [0.0, 0.0, 0.0], "hollow_wrist_head": False, "unplugged_socket": False,
        "neighbour_x0": 0.07, "fork_shortened": False}
VARIANTS = {
    "seated": GOOD,
    "seated_closed": dict(GOOD, wrist_shroud=True),
    "eccentric_pivot": dict(GOOD, wrist_pivot_offset=[0.0, 0.0, 0.005]),
    "socket_open_shell": dict(GOOD, hollow_wrist_head=True),
    "socket_marked": dict(GOOD, unplugged_socket=True),
    "overlap": dict(GOOD, neighbour_x0=0.025),
    "fork_shortened": dict(GOOD, fork_shortened=True),
}


# ------------------------------------------------------------------ meshes
class Part:
    """Triangles of one rigid part; ``kinds`` marks faces made of the interior material."""

    def __init__(self):
        self.tris, self.kinds = [], []

    def face(self, points, normal, kind=EXTERIOR):
        points = [np.asarray(p, dtype=np.float64) for p in points]
        for i in range(1, len(points) - 1):
            a, b, c = points[0], points[i], points[i + 1]
            if np.cross(b - a, c - a) @ normal < 0:
                b, c = c, b
            self.tris.append((a, b, c))
            self.kinds.append(kind)

    def box(self, low, high):
        low, high = np.asarray(low, float), np.asarray(high, float)
        for axis in range(3):
            others = [a for a in range(3) if a != axis]
            for side in (0, 1):
                normal = np.zeros(3)
                normal[axis] = 1.0 if side else -1.0
                quad = []
                for bit in ((0, 0), (1, 0), (1, 1), (0, 1)):
                    point = [low[a] for a in range(3)]
                    point[axis] = high[axis] if side else low[axis]
                    point[others[0]], point[others[1]] = (high if bit[0] else low)[others[0]], (high if bit[1] else low)[others[1]]
                    quad.append(point)
                self.face(quad, normal)

    def revolve(self, origin, axis, r_out, t0, t1, r_in=0.0, inner_from=None, end0=None, end1=None,
                inner_kind=EXTERIOR, end0_kind=EXTERIOR, end1_kind=EXTERIOR):
        """Surface of revolution about ``axis`` through ``origin``.

        Outer wall of radius ``r_out`` from ``t0`` to ``t1``; optionally an
        inner wall of radius ``r_in`` from ``inner_from`` to ``t1``; ends in
        {None, "annulus", "disc"} at ``t0`` and ``t1``.
        """
        origin, axis = np.asarray(origin, float), np.asarray(axis, float) / np.linalg.norm(axis)
        u = np.cross(axis, [0.0, 0.0, 1.0])
        if np.linalg.norm(u) < 1e-9:
            u = np.cross(axis, [0.0, 1.0, 0.0])
        u /= np.linalg.norm(u)
        v = np.cross(axis, u)
        theta = np.linspace(0.0, 2.0 * np.pi, SEGMENTS + 1)

        def point(t, r, k):
            return origin + axis * t + r * (np.cos(theta[k]) * u + np.sin(theta[k]) * v)

        def radial(k):
            return np.cos(0.5 * (theta[k] + theta[k + 1])) * u + np.sin(0.5 * (theta[k] + theta[k + 1])) * v

        for k in range(SEGMENTS):
            self.face([point(t0, r_out, k), point(t0, r_out, k + 1), point(t1, r_out, k + 1), point(t1, r_out, k)], radial(k))
            if inner_from is not None:
                self.face([point(inner_from, r_in, k), point(inner_from, r_in, k + 1), point(t1, r_in, k + 1),
                           point(t1, r_in, k)], -radial(k), inner_kind)
            if end0 == "annulus":
                self.face([point(t0, r_in, k), point(t0, r_in, k + 1), point(t0, r_out, k + 1), point(t0, r_out, k)], -axis, end0_kind)
            elif end0 == "disc":
                self.face([origin + axis * t0, point(t0, r_out, k), point(t0, r_out, k + 1)], -axis, end0_kind)
            if end1 == "annulus":
                self.face([point(t1, r_in, k), point(t1, r_in, k + 1), point(t1, r_out, k + 1), point(t1, r_out, k)], axis, end1_kind)
            elif end1 == "disc":
                self.face([origin + axis * t1, point(t1, r_out, k), point(t1, r_out, k + 1)], axis, end1_kind)

    def cylinder(self, centre, axis, radius, length):
        start = np.asarray(centre, float) - np.asarray(axis, float) / np.linalg.norm(axis) * length / 2
        self.revolve(start, axis, radius, 0.0, length, end0="disc", end1="disc")

    def tube(self, centre, axis, r_in, r_out, length):
        start = np.asarray(centre, float) - np.asarray(axis, float) / np.linalg.norm(axis) * length / 2
        self.revolve(start, axis, r_out, 0.0, length, r_in=r_in, inner_from=0.0, end0="annulus", end1="annulus")

    def open_shell(self, centre, axis, radius, length):
        """Single-sided tube without caps: its inside shows as back faces."""
        start = np.asarray(centre, float) - np.asarray(axis, float) / np.linalg.norm(axis) * length / 2
        self.revolve(start, axis, radius, 0.0, length)

    def cup(self, mouth, axis, r_in, r_out, depth, wall):
        """Closed-solid cup open at ``mouth``; the cavity faces carry the interior material."""
        axis = np.asarray(axis, float) / np.linalg.norm(axis)
        back = np.asarray(mouth, float) - axis * depth
        self.revolve(back, axis, r_out, 0.0, depth, end0="disc")
        self.revolve(back, axis, r_out, 0.0, depth, r_in=r_in, inner_from=wall, end1="annulus", inner_kind=INTERIOR)
        self.revolve(back, axis, r_in, 0.0, wall, end1="disc", end1_kind=INTERIOR)


# ------------------------------------------------------------------ assembly
def plates(part, x_in, x_out, low, high):
    """Two side plates of a clevis, symmetrical about x=0."""
    part.box([x_in, low[1], low[2]], [x_out, high[1], high[2]])
    part.box([-x_out, low[1], low[2]], [-x_in, high[1], high[2]])


def parts_for(variant):
    base, arm, hand, neighbour = Part(), Part(), Part(), Part()
    # base: sole plate at z=0, column, clevis, shoulder head, a socket cup that is normally plugged
    base.box([-0.06, -0.08, 0.0], [0.06, 0.08, 0.02])
    base.box([-0.03, -0.026, 0.02], [0.03, 0.026, 0.24])
    plates(base, 0.016, 0.030, [0, -0.03, 0.24], [0, 0.03, 0.285])
    base.cylinder(SHOULDER, [1, 0, 0], 0.030, 0.060)
    mouth = np.array([0.0, -0.05, 0.15])
    base.cup(mouth, [0, -1, 0], 0.015, 0.020, 0.030, 0.004)
    if not variant["unplugged_socket"]:
        base.cylinder([0.0, -0.035, 0.15], [0, 1, 0], 0.015, 0.030)
    # arm: ring seated on the shoulder head, bar, clevis, wrist head
    arm.tube(SHOULDER, [1, 0, 0], 0.030 + CLEARANCE, 0.042, 0.028)
    shrouded = variant.get("wrist_shroud", False)
    arm.box([-0.014, -0.198 if shrouded else -0.205, 0.285], [0.014, -0.040, 0.315])
    if shrouded:
        arm.box([-0.026, -0.201, 0.285], [0.026, -0.194, 0.315])
    plates(arm, 0.021 if shrouded else 0.014, 0.026, [0, -0.258, 0.285], [0, -0.200, 0.315])
    if variant["hollow_wrist_head"]:
        arm.open_shell(WRIST, [1, 0, 0], 0.024, 0.050)
    else:
        arm.cylinder(WRIST, [1, 0, 0], 0.024, 0.050)
    # hand: ring seated on the wrist head, hand bar
    if shrouded:
        # Annular lips overlap in projection while retaining radial and axial running clearance.
        hand.tube(WRIST, [1, 0, 0], 0.024 + CLEARANCE, 0.038, 0.028)
        for side in (-1, 1):
            flange = WRIST + np.array([side * 0.017, 0, 0])
            lip = WRIST + np.array([side * 0.016, 0, 0])
            arm.tube(flange, [1, 0, 0], 0.023, 0.033, 0.0056)
            hand.tube(lip, [1, 0, 0], 0.033 + CLEARANCE, 0.038, 0.008)
    else:
        hand.tube(WRIST, [1, 0, 0], 0.024 + CLEARANCE, 0.034, 0.024)
    hand.box([-0.012, -0.36, 0.286], [0.012, -0.270, 0.314])
    for x in (-0.012, 0.006):
        end = -0.370 if variant["fork_shortened"] and x > 0 else -0.390
        hand.box([x, end, 0.293], [x + 0.006, -0.36, 0.307])
    # neighbour post on its own joint
    x0 = variant["neighbour_x0"]
    neighbour.box([x0, -0.02, 0.0], [x0 + 0.04, 0.02, 0.20])
    return {"hips": base, "left_shoulder": arm, "left_wrist": hand, "post2": neighbour}


def pivots(variant):
    return {"hips": HIPS, "left_shoulder": SHOULDER,
            "left_wrist": WRIST + np.asarray(variant["wrist_pivot_offset"], float), "post2": POST2}


def to_gltf(point):
    """Blender world (Z up) to glTF (Y up)."""
    return [float(point[0]), float(point[2]), float(-point[1])]


def write_rig(path, variant):
    """Write one synthetic GLB deterministically; returns its sha256."""
    parts = parts_for(variant)
    joints = pivots(variant)
    writer = glb.Writer()
    positions, triangles, joint_idx, kinds = [], [], [], []
    for j, name in enumerate(JOINTS):
        part = parts[name]
        base = len(positions)
        positions.extend(vertex for tri in part.tris for vertex in tri)
        joint_idx.extend([j] * (3 * len(part.tris)))
        kinds.extend(kind for kind in part.kinds for _ in range(3))
        triangles.extend((base + 3 * i, base + 3 * i + 1, base + 3 * i + 2) for i in range(len(part.tris)))
    positions = np.array([to_gltf(p) for p in positions])
    kinds = np.array(kinds)
    triangles = np.array(triangles, dtype=np.int64)
    weights = np.zeros((len(positions), 4))
    weights[np.arange(len(positions)), 0] = 1.0
    joints_attr = np.zeros((len(positions), 4), dtype=np.uint8)
    joints_attr[:, 0] = np.array(joint_idx)
    primitives = []
    for kind, material in ((EXTERIOR, 0), (INTERIOR, 1)):
        tri_ids = np.flatnonzero(kinds[triangles[:, 0]] == kind)
        if not len(tri_ids):
            continue
        vertex_ids = np.unique(triangles[tri_ids])
        remap = np.full(len(positions), -1, dtype=np.int64)
        remap[vertex_ids] = np.arange(len(vertex_ids))
        primitives.append({"attributes": {
            "POSITION": writer.accessor(positions[vertex_ids], 5126, "VEC3", target=34962),
            "JOINTS_0": writer.accessor(joints_attr[vertex_ids], 5121, "VEC4", target=34962),
            "WEIGHTS_0": writer.accessor(weights[vertex_ids], 5126, "VEC4", target=34962)},
            "indices": writer.accessor(remap[triangles[tri_ids]].reshape(-1), 5125, "SCALAR", target=34963),
            "material": material})
    inverse = []
    for name in JOINTS:
        bind = np.eye(4)
        bind[:3, 3] = to_gltf(joints[name])
        inverse.append(np.linalg.inv(bind).T.reshape(-1))  # column-major
    nodes = [{"name": "Armature", "children": [1, 2]},
             {"name": "Synth control", "mesh": 0, "skin": 0}]
    for name in JOINTS:
        node = {"name": name}
        node["translation"] = to_gltf(joints[name] - (joints[PARENT[name]] if PARENT[name] else 0.0))
        children = [2 + k for k, other in enumerate(JOINTS) if PARENT[other] == name]
        if children:
            node["children"] = children
        nodes.append(node)
    writer.doc.update({
        "scene": 0, "scenes": [{"name": "Scene", "nodes": [0]}], "nodes": nodes,
        "meshes": [{"name": "Synth control", "primitives": primitives}],
        # No "skeleton" member: this Blender's parser only accepts the pre-final single-int form.
        "skins": [{"name": "Armature", "joints": [2 + k for k in range(len(JOINTS))],
                   "inverseBindMatrices": writer.accessor(np.array(inverse), 5126, "MAT4")}],
        "materials": [{"name": "SYN_exterior", "pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 0.8}},
                      {"name": "SYN_interior", "pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 0.9}}]})
    return writer.save(path)


def joint_map(pins=None):
    return {"schema": "rp4.pose-oracle.joint-map.v1", "rig": "synthetic control",
            "frame": "Blender world (Z up, metres), same floor plane z=0 as the character.",
            "anatomical_axes": {"fwd": [0, -1, 0], "left": [1, 0, 0], "up": [0, 0, 1]},
            "joints": {"hips": {"bone": "hips", "side": "centre"},
                       "left_shoulder": {"bone": "left_shoulder", "side": "left"},
                       "left_wrist": {"bone": "left_wrist", "side": "left"},
                       "post2": {"bone": "post2", "side": "right", "attachment": True}},
            "required_for_replay": ["hips", "left_shoulder", "left_wrist"],
            "chains": {"left_arm": ["hips", "left_shoulder", "left_wrist"]},
            "soles": {"base": {"joint": "hips", "part": "hips", "band_m": 0.002}},
            "intended": {"interior_materials": ["SYN_interior"]},
            "asset": dict(pins or {})}


# ------------------------------------------------------------------ poses
def _qmul(a, b):
    x1, y1, z1, w1 = a
    x2, y2, z2, w2 = b
    return np.array([w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                     w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
                     w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
                     w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2])


def _hinge(deg):
    """Rotation about the anatomical left axis, as (fwd, left, up) quaternion."""
    half = np.radians(deg) / 2
    return np.array([0.0, np.sin(half), 0.0, np.cos(half)])


def _pose(pose_id, family, expect, shoulder=0.0, wrist=0.0, root=None):
    d_shoulder = _hinge(shoulder)
    d_wrist = _qmul(d_shoulder, _hinge(wrist))
    identity = [0.0, 0.0, 0.0, 1.0]
    joints = {"hips": dict(root or {"q": identity, "t": [0.0, 0.0, 0.0]}),
              "left_shoulder": {"q": [float(x) for x in d_shoulder]},
              "left_wrist": {"q": [float(x) for x in d_wrist]}}
    return {"schema": POSE_SCHEMA, "pose_id": pose_id, "family": family, "plant": ["base"], "floor_z_m": 0.0,
            "angles_deg": {"left_shoulder": shoulder, "left_wrist": wrist}, "joints": joints, "expect": expect}


def poses(rig):
    """Hinge sweep plus two deliberately invalid plants."""
    out = [_pose("neutral", "neutral", {"verdict": "pass"})]
    for shoulder, wrist in itertools.product((0.0, -TRAVEL, TRAVEL), repeat=2):
        if shoulder == 0.0 and wrist == 0.0:
            continue
        family = "single" if 0.0 in (shoulder, wrist) else "pair"
        out.append(_pose(f"hinge-s{shoulder:+.0f}-w{wrist:+.0f}", family, {"verdict": "pass"}, shoulder, wrist))
    out.append(_pose("hover-base", "hover", {"verdict": "reject", "classes": ["sole_not_touching"]},
                     root={"q": [0.0, 0.0, 0.0, 1.0], "t": [0.0, 0.0, 0.004]}))
    # Base plate rolled 12 degrees about the forward axis and set down on one edge.
    half = np.radians(12.0) / 2
    roll = [float(np.sin(half)), 0.0, 0.0, float(np.cos(half))]
    verts = np.array(rig["soles"]["base"]["foot_vertices"], dtype=np.float64)
    angle = 2 * half
    lowest = float((-np.sin(angle) * verts[:, 0] + np.cos(angle) * verts[:, 2]).min())
    tip = _pose("tip-base", "tip", {"verdict": "reject", "classes": ["sole_contact_insufficient"]},
                root={"q": roll, "t": [0.0, 0.0, -lowest]})
    for joint in tip["joints"].values():
        joint["q"] = list(roll)
    out.append(tip)
    return out
