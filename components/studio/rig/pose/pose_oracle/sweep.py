# SPDX-License-Identifier: AGPL-3.0-only
"""Worst-pose sweep generator (deterministic, seeded, numpy only).

Poses are written in D format: every mapped joint carries its rest-relative
rotation as a quaternion with components along fwd, left, up, plus a root
offset in world axes.  That makes one pose file replay identically on any rig
registered in the same rest frame, whatever its bone rolls or hierarchy.
"""
import itertools
import json

import numpy as np

from .common import digest_obj

SCHEMA = "rp4.pose-oracle.pose.v1"
SWEEP_SCHEMA = "rp4.pose-oracle.sweep.v1"
ANAT = np.array([[0.0, 1.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])  # columns fwd, left, up
AXES = {"up": (0, 0, 1), "down": (0, 0, -1), "left": (1, 0, 0), "right": (-1, 0, 0), "fwd": (0, -1, 0), "back": (0, 1, 0)}
TRIPLES = [
    ("left_arm", ("left_shoulder", "pitch"), ("left_elbow", "flex"), ("left_wrist", "flex")),
    ("right_arm", ("right_shoulder", "pitch"), ("right_elbow", "flex"), ("right_wrist", "flex")),
    ("left_leg", ("left_hip", "pitch"), ("left_knee", "flex"), ("left_ankle", "pitch")),
    ("right_leg", ("right_hip", "pitch"), ("right_knee", "flex"), ("right_ankle", "pitch")),
    ("torso", ("spine", "pitch"), ("chest", "yaw"), ("neck", "yaw")),
]
HOVER_M = 0.004
CARRY_HIP_DEG = 35.0   # isolated ankle tests ride on a lifted leg
CARRY_KNEE_DEG = 70.0


def axis_rot(axis, deg):
    v = np.array(AXES[axis], dtype=np.float64)
    a = np.radians(deg)
    x, y, z = v * np.sin(a / 2)
    w = np.cos(a / 2)
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def mat_to_quat(m):
    trace = m[0, 0] + m[1, 1] + m[2, 2]
    if trace > 0:
        s = np.sqrt(trace + 1.0) * 2
        q = [(m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s, 0.25 * s]
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = np.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        q = [0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s, (m[2, 1] - m[1, 2]) / s]
    elif m[1, 1] > m[2, 2]:
        s = np.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        q = [(m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s, (m[0, 2] - m[2, 0]) / s]
    else:
        s = np.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        q = [(m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s, (m[1, 0] - m[0, 1]) / s]
    q = np.array(q)
    if q[3] < 0:
        q = -q
    return [float(x) for x in q / np.linalg.norm(q)]


def local_rotation(contract, semantic, side, angles, dofs):
    rotation = np.eye(3)
    for dof in dofs:
        value = float(angles.get(dof["name"], 0.0))
        if dof.get("mirror") and side == "right":
            value = -value
        rotation = axis_rot(dof["axis"], value) @ rotation
    return rotation


class Reference:
    """Reference skeleton and sole geometry taken from an inspect record."""

    def __init__(self, rig, joint_map):
        self.rig = rig
        self.joint_map = joint_map
        self.order = rig["order"]
        self.parent = {b: rig["bones"][b]["parent"] for b in self.order}
        self.head = {b: np.array(rig["bones"][b]["head"], dtype=np.float64) for b in self.order}
        self.bone_of = {s: j["bone"] for s, j in joint_map["joints"].items() if j.get("bone")}
        self.side_of = {s: j.get("side") for s, j in joint_map["joints"].items()}
        self.soles = rig["soles"]
        roots = [b for b in self.order if self.parent[b] is None]
        self.root = {b: s for s, b in self.bone_of.items()}[roots[0]] if len(roots) == 1 and roots[0] in self.bone_of.values() else None

    def fk(self, local, offset):
        delta, pivot = {}, {}
        for bone in self.order:
            parent = self.parent[bone]
            d = local[bone] if parent is None else delta[parent] @ local[bone]
            delta[bone] = d
            if parent is None:
                pivot[bone] = self.head[bone] + offset
            else:
                pivot[bone] = pivot[parent] + delta[parent] @ (self.head[bone] - self.head[parent])
        return delta, pivot


def contract_dofs(contract, semantic):
    key = semantic.split("_", 1)[1] if semantic.startswith(("left_", "right_")) else semantic
    return contract["dof"].get(key, [])


class Generator:
    """Builds D-format poses from DOF angles, with a plant solver on the reference geometry."""

    def __init__(self, rig, joint_map, contract, floor_z_m=0.0):
        self.ref = Reference(rig, joint_map)
        self.joint_map = joint_map
        self.contract = contract
        self.floor_z = floor_z_m
        if self.ref.root is None:
            raise ValueError("reference_root_unresolved")

    def _local(self, angles):
        local = {b: np.eye(3) for b in self.ref.order}
        for semantic, values in angles.items():
            bone = self.ref.bone_of[semantic]
            local[bone] = local_rotation(self.contract, semantic, self.ref.side_of[semantic], values,
                                         contract_dofs(self.contract, semantic))
        return local

    def _foot_low(self, delta, pivot, side):
        sole = self.ref.soles[side]
        verts = np.array(sole.get("foot_vertices") or sole["vertices"], dtype=np.float64)
        if not len(verts):
            return self.floor_z
        bone = sole["part"]
        return float((pivot[bone] + (verts - self.ref.head[bone]) @ delta[bone].T)[:, 2].min())

    def _lift(self, side):
        return {f"{side}_hip": {"pitch": CARRY_HIP_DEG}, f"{side}_knee": {"flex": CARRY_KNEE_DEG}}

    def build(self, pose_id, family, angles, plant="auto", hover_m=0.0, **extra):
        """Write one pose.  ``plant`` is "auto" (the lowest foot is stance) or an explicit list."""
        angles = {s: dict(a) for s, a in angles.items()}
        if plant == "auto":
            delta, pivot = self.ref.fk(self._local(angles), np.zeros(3))
            lows = {side: self._foot_low(delta, pivot, side) for side in self.ref.soles}
            plant = ["left", "right"] if not any(angles.get(f"{s}_hip") or angles.get(f"{s}_knee") or angles.get(f"{s}_ankle")
                                                  for s in lows) else [min(lows, key=lows.get)]
        else:
            plant = list(plant)
            for side in self.ref.soles:
                if side not in plant and not any(angles.get(f"{side}_{j}") for j in ("hip", "knee", "ankle")):
                    angles.update(self._lift(side))
        offset = np.zeros(3)
        delta, pivot = self.ref.fk(self._local(angles), offset)
        if plant:
            ankle = self.ref.bone_of[self.ref.soles[plant[0]]["joint"]]
            offset[:2] = self.ref.head[ankle][:2] - pivot[ankle][:2]
            delta, pivot = self.ref.fk(self._local(angles), offset)
            lowest = min(self._foot_low(delta, pivot, side) for side in plant)
            offset[2] = self.floor_z - lowest + hover_m
            delta, pivot = self.ref.fk(self._local(angles), offset)
        below = min((self._foot_low(delta, pivot, side) - self.floor_z for side in self.ref.soles if side not in plant),
                    default=0.0)
        # A foot through the floor makes a pose physically invalid for every asset; say so in the expectation.
        if below < -1e-4 and extra.get("expect", {}).get("verdict") == "pass":
            extra["expect"] = {"verdict": "reject", "classes": ["floor_penetration"]}
        joints = {}
        for semantic, bone in self.ref.bone_of.items():
            entry = {"q": mat_to_quat(ANAT.T @ delta[bone] @ ANAT)}
            if semantic == self.ref.root:
                entry["t"] = [float(x) for x in offset]
            joints[semantic] = entry
        pose = {"schema": SCHEMA, "pose_id": pose_id, "family": family, "plant": plant,
                "angles_deg": {s: {k: round(float(v), 4) for k, v in sorted(a.items())} for s, a in sorted(angles.items())},
                "floor_z_m": self.floor_z, "root_offset_m": [float(x) for x in offset],
                "swing_foot_below_floor_m": round(min(0.0, below), 6), "joints": joints}
        pose.update(extra)
        return pose

    def envelope(self):
        """(semantic, dof) pairs that exist in both the contract and the joint map."""
        return [(semantic, dof) for semantic in self.ref.bone_of for dof in contract_dofs(self.contract, semantic)]

    @staticmethod
    def _range(dof, edge):
        return dof["range_deg"][edge == "max"]

    def generate(self, profile, seed):
        spec = self.contract["sweep"]["profiles"][profile]
        rng = np.random.default_rng(seed)
        poses = [self.build("neutral", "neutral", {}, expect={"verdict": "pass"})]
        envelope = self.envelope()
        single = [(s, d) for s, d in envelope if spec["single"] == "all" or d.get("primary")]
        for semantic, dof in single:
            for edge in ("min", "max"):
                angles = {semantic: {dof["name"]: self._range(dof, edge)}}
                if dof["name"] in ("pitch", "roll") and semantic.endswith("_ankle"):
                    angles.update(self._lift(semantic.split("_")[0]))
                poses.append(self.build(f"single-{semantic}-{dof['name']}-{edge}", "single", angles,
                                        expect={"verdict": "pass"}, focus=[semantic]))
        if "primary-triples" in spec["pair"]:
            for chain, *triple in TRIPLES:
                if all(s in self.ref.bone_of for s, *_ in triple):
                    for edges in itertools.product(("min", "max"), repeat=3):
                        angles, tag = {}, []
                        for (semantic, name), edge in zip(triple, edges):
                            dof = next(d for d in contract_dofs(self.contract, semantic) if d["name"] == name)
                            angles[semantic] = {name: self._range(dof, edge)}
                            tag.append("hi" if edge == "max" else "lo")
                        poses.append(self.build(f"pair-{chain}-{''.join(tag)}", "pair", angles,
                                                expect={"verdict": "pass"}, focus=[s for s, _ in triple]))
        if "secondary-pairs" in spec["pair"]:
            for semantic in sorted({s for s, _ in envelope if len(contract_dofs(self.contract, s)) >= 2}):
                dofs = contract_dofs(self.contract, semantic)
                for edges in itertools.product(("min", "max"), repeat=2):
                    angles = {semantic: {dof["name"]: self._range(dof, edge) for dof, edge in zip(dofs[:2], edges)}}
                    tag = "".join("hi" if edge == "max" else "lo" for edge in edges)
                    poses.append(self.build(f"pair-{semantic}-{tag}", "pair", angles, expect={"verdict": "pass"}, focus=[semantic]))
        dimension = len(envelope)
        for k in range(int(spec["random"])):
            strata = [rng.permutation(int(spec["random"])) for _ in range(dimension)]
            angles = {}
            for i, (semantic, dof) in enumerate(envelope):
                lo, hi = dof["range_deg"]
                cell = (strata[i][k] + rng.uniform(0.1, 0.9)) / int(spec["random"])
                angles.setdefault(semantic, {})[dof["name"]] = lo + (hi - lo) * cell
            poses.append(self.build(f"random-{k:02d}", "random", angles, expect={"verdict": "pass"}))
        tips = list(itertools.product(("left", "right"), (-20.0, 20.0), (-38.0, 28.0)))[:int(spec["tip"])]
        for side, roll, pitch in tips:
            poses.append(self.build(f"tip-{side}-roll{roll:+.0f}-pitch{pitch:+.0f}", "tip",
                                    {f"{side}_ankle": {"roll": roll, "pitch": pitch}}, [side],
                                    expect={"verdict": "reject", "classes": ["sole_contact_insufficient"]}, focus=[f"{side}_ankle"]))
        for sides, name in ((["left", "right"], "hover-both"), (["left"], "hover-left")):
            poses.append(self.build(name, "hover", {}, sides, hover_m=HOVER_M,
                                    expect={"verdict": "reject", "classes": ["sole_not_touching"]}))
        ids = [p["pose_id"] for p in poses]
        repeated = sorted({i for i in ids if ids.count(i) > 1})
        if repeated:
            raise ValueError("sweep_pose_id_collision:" + ",".join(repeated[:6]))
        return poses


def manifest(records, profile, seed, rig, joint_map, contract, floor_z):
    """Sweep manifest: one record per pose plus a digest of the whole pose set."""
    body = {"schema": SWEEP_SCHEMA, "generator": "pose-oracle sweep", "profile": profile, "seed": seed,
            "floor_z_m": floor_z,
            "reference": {"rig_sha256": rig["asset"]["file_sha256"], "structure_sha256": rig["asset"]["structure_sha256"]},
            "joint_map_sha256": digest_obj(joint_map), "contract_sha256": digest_obj(contract),
            "count": len(records), "poses": records}
    body["poses_digest"] = digest_obj([[r["pose_id"], r["sha256"]] for r in records])
    return body


def pose_bytes(pose):
    return (json.dumps(pose, sort_keys=True, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
