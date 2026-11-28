# SPDX-License-Identifier: AGPL-3.0-only
"""Exact geometry conditioning for a Studio mesh."""

import numpy as np

from rig_common import need
from glb_utils import add, pack, primitive, unpack


UNWELDED = "studio_projection_unwelded_seams"


def accessor(doc, data, index, shape, types):
    item = doc["accessors"][index]
    view = doc["bufferViews"][item["bufferView"]]
    need(item["type"] == shape and item["componentType"] in types
         and not item.get("normalized") and "sparse" not in item and view["buffer"] == 0,
         "studio_projection_accessor")
    dtype = np.dtype(types[item["componentType"]])
    columns = 3 if shape == "VEC3" else 1
    width = columns * dtype.itemsize
    stride = view.get("byteStride", width)
    start = view.get("byteOffset", 0)
    offset = item.get("byteOffset", 0)
    count = item["count"]
    need(count > 0 and width <= stride and start >= 0 and offset >= 0
         and offset + (count - 1) * stride + width <= view["byteLength"]
         and start + view["byteLength"] <= len(data), "studio_projection_accessor_bounds")
    return np.ndarray((count, columns), dtype=dtype, buffer=data, offset=start + offset,
                      strides=(stride, dtype.itemsize))


def geometry(doc, data):
    part = primitive(doc)
    need(part.get("mode", 4) == 4 and not part.get("targets") and not part.get("extensions"),
         "studio_projection_primitive")
    pos = accessor(doc, data, part["attributes"]["POSITION"], "VEC3", {5126: "<f4"})
    indices = accessor(doc, data, part["indices"], "SCALAR", {5121: "u1", 5123: "<u2", 5125: "<u4"})
    need(np.isfinite(pos).all() and indices.size % 3 == 0 and int(indices.max()) < len(pos)
         and len(np.unique(indices)) == len(pos), "studio_projection_coverage")
    return pos, indices.reshape(-1, 3)


def validate_projection(raw):
    doc, data = unpack(raw)
    pos, faces = geometry(doc, data)
    need(doc["nodes"] == [{"mesh": 0}] and primitive(doc)["attributes"].keys() == {"POSITION"},
         "studio_projection_not_geometry_only")
    need(len(np.unique(pos, axis=0)) == len(pos), UNWELDED)
    # Use the generated-rig digest grid for the collision check. Keep the actual positions exact.
    grid = np.rint(pos.astype("f8") * 100_000)
    need(len(np.unique(grid, axis=0)) == len(pos), "studio_projection_grid_collision")
    need(set(doc) == {"asset", "scene", "scenes", "nodes", "meshes", "buffers", "bufferViews", "accessors"}
         and doc["scene"] == 0 and doc["scenes"] == [{"nodes": [0]}]
         and len(doc["bufferViews"]) == len(doc["accessors"]) == 2
         and doc["bufferViews"][0].get("byteOffset", 0) == 0
         and doc["bufferViews"][0]["byteLength"] == pos.nbytes
         and doc["bufferViews"][1].get("byteOffset", 0) == pos.nbytes
         and doc["bufferViews"][1]["byteLength"] == faces.nbytes
         and len(data) == pos.nbytes + faces.nbytes, "studio_projection_not_compact")
    extent = np.ptp(pos, axis=0)
    need(extent[1] > max(extent[0], extent[2]) > 0 and min(extent) > 0,
         "studio_projection_y_up_extent")
    corners = pos[faces].astype("f8")
    area = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    need(np.all(np.any(area != 0, axis=1)), "studio_projection_degenerate_face")
    edges = faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2)
    _, ids, counts = np.unique(np.sort(edges, axis=1), axis=0, return_inverse=True, return_counts=True)
    signs = np.where(edges[:, 0] < edges[:, 1], 1, -1)
    need(np.all(counts == 2) and not np.any(np.bincount(ids, weights=signs)),
         "studio_projection_open_or_inconsistent_winding")
    return pos, faces


def normalise(selected_raw):
    doc, data = unpack(selected_raw)
    need(doc["scene"] == 0 and doc["scenes"][0]["nodes"] == [0]
         and len(doc["nodes"]) == 1 and doc["nodes"][0]["mesh"] == 0
         and set(doc["nodes"][0]) <= {"name", "mesh"}, "studio_projection_node_transform")
    pos, faces = geometry(doc, data)
    # Keep the first position record for each exact coordinate, preserving face and corner order.
    _, first, inverse = np.unique(pos, axis=0, return_index=True, return_inverse=True)
    order = np.argsort(first)
    remap = np.empty_like(order)
    remap[order] = np.arange(len(order))
    packed_pos = pos[first[order]]
    packed_faces = remap[inverse[faces]]
    need(np.array_equal(packed_pos[packed_faces], pos[faces]), "studio_projection_triangle_changed")
    payload = bytearray()
    output = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
              "nodes": [{"mesh": 0}], "buffers": [{}], "bufferViews": [], "accessors": []}
    p = add(output, payload, packed_pos, "VEC3", "<f4", 5126, bounds=True)
    i = add(output, payload, packed_faces.ravel(), "SCALAR", "<u4", 5125)
    output["meshes"] = [{"primitives": [{"attributes": {"POSITION": p}, "indices": i}]}]
    output["bufferViews"][p]["target"] = 34962
    output["bufferViews"][i]["target"] = 34963
    raw = pack(output, payload)
    validate_projection(raw)
    return raw
