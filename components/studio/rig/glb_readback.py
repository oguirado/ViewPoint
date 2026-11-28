# SPDX-License-Identifier: AGPL-3.0-only
"""Read geometry and joint names from the diagnostic GLBs."""
from array import array
import json
import math
import struct
import sys


def glb(path):
    with path.open("rb") as stream:
        magic, version, length, json_bytes, chunk = struct.unpack("<4s4I", stream.read(20))
        if (magic, version, length, chunk) != (b"glTF", 2, path.stat().st_size, 0x4E4F534A):
            raise ValueError(f"invalid_glb_header: {path}")
        document = json.loads(stream.read(json_bytes))
        bin_bytes, bin_kind = struct.unpack("<2I", stream.read(8))
        offset = stream.tell()
        if bin_kind != 0x004E4942 or offset + bin_bytes != length:
            raise ValueError(f"invalid_glb_bin: {path}")
    return document, offset, bin_bytes


def component_count(indices, vertices):
    parents = array("i", [-1]) * vertices
    components = 0

    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for a, b, c in zip(indices[::3], indices[1::3], indices[2::3], strict=True):
        for i in (a, b, c):
            if parents[i] == -1:
                parents[i] = i
                components += 1
        head = root(a)
        for i in (b, c):
            other = root(i)
            if head != other:
                parents[other] = head
                components -= 1
    return components


def geometry(path, *, topology=False):
    document, offset, bin_bytes = glb(path)
    accessors = document["accessors"]
    triangles = vertices = 0
    components = 0
    extents = []
    with path.open("rb") as stream:
        for mesh in document["meshes"]:
            for primitive in mesh["primitives"]:
                if primitive.get("mode", 4) != 4:
                    raise ValueError(f"non_triangle_primitive: {path}")
                positions = accessors[primitive["attributes"]["POSITION"]]
                index = accessors[primitive["indices"]]
                if index["count"] % 3 or not index["count"] or not positions["count"]:
                    raise ValueError(f"invalid_geometry_counts: {path}")
                triangles += index["count"] // 3
                vertices += positions["count"]
                extents.append((positions["min"], positions["max"]))
                if not topology:
                    continue
                view = document["bufferViews"][index["bufferView"]]
                code = {5121: "B", 5123: "H", 5125: "I"}[index["componentType"]]
                values = array(code)
                start = view.get("byteOffset", 0) + index.get("byteOffset", 0)
                length = index["count"] * values.itemsize
                if (view["buffer"] != 0 or index["type"] != "SCALAR" or "sparse" in index
                        or view.get("byteStride", values.itemsize) != values.itemsize
                        or not 0 <= start <= start + length <= bin_bytes):
                    raise ValueError(f"invalid_index_accessor: {path}")
                stream.seek(offset + start)
                values.fromfile(stream, index["count"])
                if sys.byteorder != "little":
                    values.byteswap()
                if max(values) >= positions["count"]:
                    raise ValueError(f"index_out_of_range: {path}")
                components += component_count(values, positions["count"])
    if not extents or not all(math.isfinite(x) for bounds in extents for point in bounds for x in point):
        raise ValueError(f"missing_mesh_bounds: {path}")
    return {"triangles": triangles, "vertices": vertices,
            "indexed_components": components if topology else None,
            "bounds": [[min(v[0][i] for v in extents) for i in range(3)],
                       [max(v[1][i] for v in extents) for i in range(3)]]}


def joint_names(path):
    document, _, _ = glb(path)
    ids = list(dict.fromkeys(i for skin in document.get("skins", []) for i in skin["joints"]))
    nodes = document.get("nodes", [])
    if not ids or any(type(i) is not int or not 0 <= i < len(nodes) for i in ids):
        raise ValueError(f"missing_or_invalid_skin_joints: {path}")
    names = [nodes[index].get("name") for index in ids]
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError(f"missing_joint_name: {path}")
    return names
