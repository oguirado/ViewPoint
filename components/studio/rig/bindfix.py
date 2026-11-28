# SPDX-License-Identifier: AGPL-3.0-only
"""Recompute binary32 inverse binds from serialized rest transforms."""
import math
from pathlib import Path
import struct
import sys
import time

# Use the same GLB parser and transform conventions as the provider gate.
_FACTORY = Path(__file__).resolve().parents[2] / "asset-factory"
if str(_FACTORY) not in sys.path:
    sys.path.insert(0, str(_FACTORY))
from providers.unirig.common import Refusal, integer, require
from providers.unirig.gate import GLB, IDENTITY, node_matrix, point


def _rest_mul(left, right):
    # Compensate products before the binary64 result is rounded. The serialized
    # binary32 coefficients can have a much smaller error bound near zero.
    return tuple(math.sumprod(left[row::4], right[column:column + 4])
                 for column in range(0, 16, 4) for row in range(4))


def _inverse_affine(matrix):
    """Invert a column-major affine transform in binary64."""
    a, b, c, d, e, f, g, h, i = (matrix[k] for k in (0, 4, 8, 1, 5, 9, 2, 6, 10))
    cofactors = (
        math.sumprod((e, -f), (i, h)), math.sumprod((f, -d), (g, i)), math.sumprod((d, -e), (h, g)),
        math.sumprod((c, -b), (h, i)), math.sumprod((a, -c), (i, g)), math.sumprod((b, -a), (g, h)),
        math.sumprod((b, -c), (f, e)), math.sumprod((c, -a), (d, f)), math.sumprod((a, -b), (e, d)),
    )
    determinant = math.sumprod((a, b, c), cofactors[:3])
    require(math.isfinite(determinant) and determinant != 0, 5, "singular_bind_matrix")
    inverse = tuple(value / determinant for value in cofactors)
    linear = (*inverse[:3], 0, *inverse[3:6], 0, *inverse[6:], 0, 0, 0, 0, 1)
    translation = point(linear, tuple(-value for value in matrix[12:15]))
    return (*linear[:12], *translation, 1)


def _normalized_rest_matrix(node):
    if "rotation" not in node:
        return node_matrix(node)
    x, y, z, w = node["rotation"]
    quaternion = (x, y, z, w)
    norm = math.sumprod(quaternion, quaternion)
    factor = 2 / norm
    sx, sy, sz = node.get("scale", (1, 1, 1))
    tx, ty, tz = node.get("translation", (0, 0, 0))
    return (
        sx * math.sumprod((x, -y, -z, w), quaternion) / norm,
        sx * factor * math.sumprod((x, z), (y, w)),
        sx * factor * math.sumprod((x, -y), (z, w)), 0,
        sy * factor * math.sumprod((x, -z), (y, w)),
        sy * math.sumprod((-x, y, -z, w), quaternion) / norm,
        sy * factor * math.sumprod((y, x), (z, w)), 0,
        sz * factor * math.sumprod((x, y), (z, w)),
        sz * factor * math.sumprod((y, -x), (z, w)),
        sz * math.sumprod((-x, -y, z, w), quaternion) / norm, 0,
        tx, ty, tz, 1,
    )


def recompute_inverse_binds(data, *, timeout_s):
    """Re-encode UniRig's bind matrices against the serialized glTF rest pose."""
    require(integer(timeout_s, 1, 300), 2, "policy_invalid")
    glb = GLB(data, time.monotonic_ns() + timeout_s * 10**9, 4096)
    parents, _ = glb.hierarchy()
    active = glb.scene_nodes(parents)
    doc = glb.doc
    nodes, skins = doc["nodes"], doc.get("skins", [])
    require(len(skins) == 1, 3, "inverse_bind_single_skin_required")
    skin = skins[0]
    joints = skin.get("joints", [])
    require(type(joints) is list and joints and
            all(integer(joint, 0, len(nodes) - 1) and joint in active for joint in joints) and
            len(set(joints)) == len(joints),
            5, "joints_invalid")
    accessor_id = skin.get("inverseBindMatrices")
    old_binds, normalized = glb.accessor(accessor_id, "MAT4", (5126,))
    require(not normalized and len(old_binds) == len(joints), 5, "inverse_bind_count")
    accessor = doc["accessors"][accessor_id]
    view_id = accessor["bufferView"]
    view = doc["bufferViews"][view_id]
    start = view.get("byteOffset", 0)
    end = start + view["byteLength"]
    require(all(i == view_id or end <= other.get("byteOffset", 0) or
                other.get("byteOffset", 0) + other["byteLength"] <= start
                for i, other in enumerate(doc["bufferViews"])), 3, "inverse_bind_view_shared")
    # An accessor, image or sparse record may reference the same view directly.
    entries = [doc]
    while entries:
        glb.check_time()
        entry = entries.pop()
        if isinstance(entry, dict) and entry is not accessor:
            require(entry.get("bufferView") != view_id, 3, "inverse_bind_view_shared")
            entries.extend(entry.values())
        elif isinstance(entry, list):
            entries.extend(entry)

    worlds, locals_by_node, paths = {}, {}, {}
    pending = list(doc["scenes"][doc["scene"]]["nodes"])
    while pending:
        glb.check_time()
        index = pending.pop()
        node = nodes[index]
        local = _normalized_rest_matrix(node)
        require(local[3:16:4] == (0, 0, 0, 1), 5, "matrix_not_affine")
        parent = parents[index]
        worlds[index] = local if parent is None else _rest_mul(worlds[parent], local)
        locals_by_node[index] = local
        paths[index] = (index,) if parent is None else (*paths[parent], index)
        pending.extend(node.get("children", []))

    meshes = sorted(index for index in active if "mesh" in nodes[index])
    mesh_worlds = {worlds[index] for index in meshes}
    require(len(mesh_worlds) == 1, 3, "inverse_bind_ambiguous_mesh_world")
    mesh_path = paths[meshes[0]]
    stride = view.get("byteStride", 64)
    offset = len(data) - len(glb.binary) + start + accessor.get("byteOffset", 0)
    output = bytearray(data)
    for slot, joint in enumerate(joints):
        glb.check_time()
        # The shared ancestor product cancels in inverse(J) * M. Drop it before
        # multiplying either branch so that its roundoff cannot contaminate the bind.
        joint_path = paths[joint]
        common = 0
        for joint_node, mesh_node in zip(joint_path, mesh_path):
            if joint_node != mesh_node:
                break
            common += 1
        relative = []
        for path in (joint_path, mesh_path):
            matrix = IDENTITY
            for index in path[common:]:
                matrix = _rest_mul(matrix, locals_by_node[index])
            relative.append(matrix)
        joint_rest, mesh_rest = relative
        bind = _rest_mul(_inverse_affine(joint_rest), mesh_rest)
        require(all(math.isfinite(value) for value in bind), 5, "inverse_bind_nonfinite")
        try:
            struct.pack_into("<16f", output, offset + slot * stride, *bind)
        except OverflowError as exc:
            raise Refusal(5, "inverse_bind_float32_range") from exc
    return bytes(output)

