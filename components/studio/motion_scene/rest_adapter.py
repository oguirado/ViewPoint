# SPDX-License-Identifier: AGPL-3.0-only
"""Read studio rig rest transforms and bind supplied BVH motion to the same nodes."""
from dataclasses import dataclass
import math
import json
import struct

import bvh
from bvh import Refusal, require
from evidence import HASH, demand, digest, shape

MAX_GLB_BYTES = 256 * 2**20
MAX_METADATA = 16 * 2**20


def strict_asset_json(data):
    def pairs(items):
        out = {}
        for key, value in items:
            require(key not in out, 2, "glb_duplicate_key")
            out[key] = value
        return out

    def constant(_):
        raise Refusal(2, "glb_nonfinite")

    try:
        obj = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Refusal(2, "glb_invalid") from exc
    return obj


def rig_document(data):
    """Decode the bounded GLB metadata and check every chunk boundary."""
    require(len(data) >= 20, 2, "glb_invalid")
    magic, version, total = struct.unpack_from("<4sII", data)
    require(magic == b"glTF" and version == 2 and total == len(data), 2, "glb_invalid")
    length, kind = struct.unpack_from("<I4s", data, 12)
    require(kind == b"JSON" and 0 < length <= MAX_METADATA and length % 4 == 0
            and 20 + length <= len(data), 2, "glb_invalid")
    # Validate every chunk boundary; a truncated BIN cannot authenticate a rig.
    cursor = 20 + length
    while cursor < len(data):
        require(cursor + 8 <= len(data), 2, "glb_invalid")
        size, tag = struct.unpack_from("<I4s", data, cursor)
        require(tag == b"BIN\x00" and size % 4 == 0 and cursor + 8 + size == len(data), 2, "glb_invalid")
        cursor += 8 + size
    doc = strict_asset_json(data[20:20 + length])
    require(type(doc) is dict and type(doc.get("asset")) is dict
            and doc["asset"].get("version") == "2.0", 2, "glb_invalid")
    return doc


def rig_node_tree(doc):
    """Return nodes, skin joint slots and node parent IDs."""
    skins, nodes = doc.get("skins", []), doc.get("nodes", [])
    require(type(skins) is list and type(nodes) is list, 2, "glb_invalid")
    require(bool(skins), 3, "rig_skin_missing")
    require(len(skins) == 1 and type(skins[0]) is dict, 3, "rig_skin_unsupported")
    ids = skins[0].get("joints")
    require(type(ids) is list and 0 < len(ids) <= 150
            and all(type(i) is int and 0 <= i < len(nodes) for i in ids)
            and len(set(ids)) == len(ids), 2, "rig_joints_invalid")
    parents = {}
    for i, node in enumerate(nodes):
        require(type(node) is dict, 2, "glb_invalid")
        children = node.get("children", [])
        require(type(children) is list, 2, "rig_hierarchy_invalid")
        for child in children:
            require(type(child) is int and 0 <= child < len(nodes)
                    and child not in parents, 2, "rig_hierarchy_invalid")
            parents[child] = i
    return nodes, ids, parents


IDENTITY = bvh.IDENTITY
ROLES = "hips left_foot right_foot striking_hand"
OFFSET_TOLERANCE_M = 1e-8
# Allow float32 exporter round-off near unit TRS scale and preserve the exact values.
UNIT_SCALE_TOLERANCE = 2**-20


def transpose(matrix):
    return tuple(zip(*matrix))


def xyz_add(a, b):
    return tuple(x + y for x, y in zip(a, b, strict=True))


def xyz_sub(a, b):
    return tuple(x - y for x, y in zip(a, b, strict=True))


def vector(node, key, default, maximum, reason):
    values = node.get(key, default)
    demand(type(values) is list and len(values) == len(default)
           and all(type(v) in (int, float) and abs(v) <= maximum for v in values), reason)
    return tuple(values)


def local_rest(node):
    reason = "rig_rest_transform_unsupported"
    # The adapter supports the UniRig TRS profile. Matrix nodes, including shear,
    # and axis reflections are outside that profile.
    demand("matrix" not in node, reason)
    t = vector(node, "translation", [0, 0, 0], 1_000_000, reason)
    q = vector(node, "rotation", [0, 0, 0, 1], 2, reason)
    s = vector(node, "scale", [1, 1, 1], 2, reason)
    norm = math.hypot(*q)
    demand(abs(norm - 1) <= UNIT_SCALE_TOLERANCE
           and all(abs(v - 1) <= UNIT_SCALE_TOLERANCE for v in s), reason)
    normalized = [v / norm for v in q]
    x, y, z, w = normalized
    rotation = ((1 - 2*(y*y + z*z), 2*(x*y - z*w), 2*(x*z + y*w)),
                (2*(x*y + z*w), 1 - 2*(x*x + z*z), 2*(y*z - x*w)),
                (2*(x*z - y*w), 2*(y*z + x*w), 1 - 2*(x*x + y*y)))
    return t, rotation, s


@dataclass(frozen=True)
class RestJoint:
    node: int
    parent: str | None
    translation: tuple
    scale: tuple
    world_origin: tuple
    world_rotation: tuple
    world_linear: tuple
    world_linear_inverse: tuple


def read_rest(raw, expected_sha256):
    """Read rest transforms from exact bytes. Skin admission is upstream."""
    demand(type(raw) is bytes and len(raw) <= MAX_GLB_BYTES, "rig_byte_budget")
    demand(type(expected_sha256) is str and HASH.fullmatch(expected_sha256)
           and digest(raw) == expected_sha256, "rig_digest_mismatch")
    doc = rig_document(raw)
    nodes, ids, parents = rig_node_tree(doc)
    demand(len(nodes) <= 4096, "rig_node_budget")
    joint_ids = set(ids)
    names = [nodes[i].get("name") for i in ids]
    demand(all(type(name) is str and name for name in names)
           and len(set(names)) == len(names), "rig_joint_name")
    roots = [i for i in ids if parents.get(i) not in joint_ids]
    demand(len(roots) == 1, "rig_hierarchy_invalid")
    ancestor, visited = parents.get(roots[0]), set()
    while ancestor is not None:
        demand(ancestor not in joint_ids and ancestor not in visited, "rig_hierarchy_invalid")
        visited.add(ancestor)
        demand(local_rest(nodes[ancestor]) == ((0, 0, 0), IDENTITY, (1, 1, 1)),
               "rig_ancestor_transform_unsupported")
        ancestor = parents.get(ancestor)

    rest = {}
    for i in ids:
        chain, current = [], i
        while current in joint_ids and current not in rest:
            demand(current not in chain, "rig_hierarchy_invalid")
            chain.append(current)
            current = parents.get(current)
        for index in reversed(chain):
            parent_id = parents.get(index)
            parent = rest.get(parent_id)
            t, r, s = local_rest(nodes[index])
            linear = tuple(tuple(v * s[col] for col, v in enumerate(row)) for row in r)
            inverse = tuple(tuple(v / scale for v in row)
                            for row, scale in zip(transpose(r), s, strict=True))
            rest[index] = RestJoint(
                index, nodes[parent_id]["name"] if parent else None, t, s,
                xyz_add(parent.world_origin, bvh.rotate(parent.world_linear, t)) if parent else t,
                bvh.multiply(parent.world_rotation, r) if parent else r,
                bvh.multiply(parent.world_linear, linear) if parent else linear,
                bvh.multiply(inverse, parent.world_linear_inverse) if parent else inverse,
            )
    return {name: rest[i] for name, i in zip(names, ids, strict=True)}


@dataclass(frozen=True)
class LocalPose:
    node: int
    translation_m: tuple
    rotation: tuple  # Row-major matrix acting on column vectors.
    scale: tuple


@dataclass(frozen=True)
class RestBinding:
    rig_sha256: str
    joints: tuple
    rest: tuple
    frame_time: object
    frame_count: int

    def playback(self, clip):
        """Yield original-GLB local TRS for each frame of a bound BVH revision."""
        demand(clip.joints == self.joints, "REST_JOINT_MAP_MISMATCH")
        demand(clip.frame_time == self.frame_time and len(clip.rows) == self.frame_count,
               "REST_FRAME_CLOCK_MISMATCH")
        for row in clip.rows:
            # BVH rest bases are world-aligned. Its posed world rotations are
            # therefore D = source_pose_world * inverse(source_rest_world).
            _, deltas, _ = bvh.forward(clip, row, 1)
            posed_bases = [
                bvh.multiply(delta, joint.world_rotation)
                for delta, joint in zip(deltas, self.rest, strict=True)
            ]
            frame = []
            for index, (joint, rest) in enumerate(zip(self.joints, self.rest, strict=True)):
                parent = joint.parent
                world = posed_bases[index]
                local = world if parent is None else bvh.multiply(transpose(posed_bases[parent]), world)
                translation = rest.translation
                if len(joint.channels) == 6:
                    delta = tuple(map(float, row[joint.start:joint.start + 3]))
                    if parent is not None:
                        delta = bvh.rotate(self.rest[parent].world_linear_inverse, delta)
                    translation = xyz_add(translation, delta)
                frame.append(LocalPose(rest.node, translation, local, rest.scale))
            yield tuple(frame)


def bind_rest(raw, sha256, source, target, markers, semantics, frame_time, frame_count):
    rest = read_rest(raw, sha256)
    reason = "REST_JOINT_MAP_MISMATCH"
    demand(len(source) == len(target) and {joint.name for joint in target} == rest.keys(), reason)
    pairs = {src.name: dst.name for src, dst in zip(source, target, strict=True)}
    shape(semantics, "source target", reason)
    for side in semantics.values():
        shape(side, ROLES, reason)
        demand(all(type(name) is str and name for name in side.values())
               and len(set(side.values())) == len(side), reason)
    src_roles, dst_roles = semantics["source"], semantics["target"]
    demand(all(pairs.get(name) == dst_roles[role] for role, name in src_roles.items())
           and src_roles["hips"] == source[0].name and dst_roles["hips"] == target[0].name
           and markers["left"][0] == dst_roles["left_foot"]
           and markers["right"][0] == dst_roles["right_foot"], reason)
    for joint in target:
        parent = None if joint.parent is None else target[joint.parent].name
        native = rest[joint.name]
        demand(native.parent == parent, reason)
        offset = native.world_origin if parent is None else xyz_sub(native.world_origin, rest[parent].world_origin)
        demand(math.dist(offset, map(float, joint.offset)) <= OFFSET_TOLERANCE_M, reason)
    return RestBinding(sha256, target, tuple(rest[joint.name] for joint in target), frame_time, frame_count)
