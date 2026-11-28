# SPDX-License-Identifier: AGPL-3.0-only
"""Verify embedded GLB maps and bake coverage against exported UVs."""
import hashlib
import io
import json
import struct

import cv2
import numpy as np
from PIL import Image

def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def glb(path):
    with path.open("rb") as stream:
        magic, version, size, text_size, code = struct.unpack("<5I", stream.read(20))
        if magic != 0x46546C67 or version != 2 or size != path.stat().st_size or code != 0x4E4F534A:
            raise RuntimeError(f"invalid GLB header: {path}")
        scene = json.loads(stream.read(text_size))
        binary_size, binary_code = struct.unpack("<2I", stream.read(8))
        binary = stream.read()
        if binary_code != 0x004E4942 or len(binary) != binary_size:
            raise RuntimeError(f"invalid GLB binary chunk: {path}")
    return scene, binary


def view_bytes(doc, data, index):
    view = doc["bufferViews"][index]
    start, length = view.get("byteOffset", 0), view["byteLength"]
    if view["buffer"] != 0 or not 0 <= start <= start + length <= len(data):
        raise RuntimeError("invalid embedded buffer view")
    return data[start:start + length]


def embedded(doc, data, index):
    tex = doc["textures"][index]
    img = doc["images"][tex["source"]]
    if img.get("mimeType") != "image/png" or "uri" in img:
        raise RuntimeError("expected an embedded PNG")
    return view_bytes(doc, data, img["bufferView"])


def array(doc, data, index):
    acc = doc["accessors"][index]
    dtype = np.dtype({5123: "<u2", 5125: "<u4", 5126: "<f4"}[acc["componentType"]])
    width = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}[acc["type"]]
    v = doc["bufferViews"][acc["bufferView"]]
    if acc.get("sparse") or acc.get("normalized"):
        raise RuntimeError("unsupported studio accessor")
    offset = acc.get("byteOffset", 0)
    count = acc["count"]
    stride = v.get("byteStride", dtype.itemsize * width)
    payload = view_bytes(doc, data, acc["bufferView"])
    if (count <= 0 or offset < 0 or stride < dtype.itemsize * width
            or offset + (count - 1) * stride + dtype.itemsize * width > len(payload)):
        raise RuntimeError("studio accessor is outside its buffer view")
    return np.ndarray((count, width), dtype=dtype, buffer=payload,
                      offset=offset, strides=(stride, dtype.itemsize))


def welded_shells(positions, triangles):
    """Recount exact-position shells, verifying oriented edges and vertex fans."""
    vertices, inverse = np.unique(positions, axis=0, return_inverse=True)
    faces = inverse[triangles]
    unique = np.sort(faces, axis=1)
    if (np.any(unique[:, :-1] == unique[:, 1:]) or len(np.unique(unique, axis=0)) != len(faces)
            or len(np.unique(faces)) != len(vertices)):
        raise RuntimeError("studio GLB contains collapsed, duplicate or orphan geometry")
    coordinates = vertices[faces].astype(np.float64)
    normal = np.cross(coordinates[:, 1] - coordinates[:, 0], coordinates[:, 2] - coordinates[:, 0])
    if np.any(np.linalg.norm(normal, axis=1) <= 2e-14):
        raise RuntimeError("studio GLB contains a degenerate face")

    edges = np.stack((faces, np.roll(faces, -1, axis=1)), axis=2).reshape(-1, 2)
    undirected = np.sort(edges, axis=1)
    order = np.lexsort(undirected.T[::-1])
    sorted_edges = undirected[order]
    if (len(order) % 2 or np.any(sorted_edges[::2] != sorted_edges[1::2])
            or np.any(np.all(sorted_edges[1:-1:2] == sorted_edges[2::2], axis=1))
            or np.any(edges[order[::2]] != edges[order[1::2], ::-1])):
        raise RuntimeError("studio GLB is open, nonmanifold or inconsistently wound")
    adjacent = np.empty_like(order)
    adjacent[order[::2]] = order[1::2] // 3
    adjacent[order[1::2]] = order[::2] // 3
    neighbors = adjacent.reshape(-1, 3).tolist()
    face_vertices = faces.tolist()
    incident = [[] for _ in vertices]
    for face, vertex_ids in enumerate(face_vertices):
        for vertex in vertex_ids:
            incident[vertex].append(face)
    for vertex, around in enumerate(incident):
        fan = {around[0]}
        frontier = [around[0]]
        while frontier:
            face = frontier.pop()
            slot = face_vertices[face].index(vertex)
            for neighbor in (neighbors[face][slot], neighbors[face][slot - 1]):
                if neighbor not in fan:
                    fan.add(neighbor)
                    frontier.append(neighbor)
        if len(fan) != len(around):
            raise RuntimeError("studio GLB has a disconnected vertex fan")

    visited = set()
    counts = []
    for face in range(len(faces)):
        if face in visited:
            continue
        shell, frontier = [face], [face]
        visited.add(face)
        while frontier:
            for adjacent_face in neighbors[frontier.pop()]:
                if adjacent_face not in visited:
                    visited.add(adjacent_face)
                    shell.append(adjacent_face)
                    frontier.append(adjacent_face)
        shell_vertices = coordinates[shell] - coordinates[face, 0]
        a, b, c = shell_vertices.transpose(1, 0, 2)
        if float(np.sum(np.einsum("ij,ij->i", a, np.cross(b, c)))) <= 0:
            raise RuntimeError("studio GLB contains a shell with nonpositive volume")
        counts.append(len(np.unique(faces[shell])))
    return sorted(counts, reverse=True)


def bake_uv_cells(uv, triangles, width):
    mask = np.zeros((width, width), dtype=np.uint8)
    screen = np.round((uv * width - 0.5) * 16).astype(np.int32)
    for face in triangles:
        cv2.fillConvexPoly(mask, screen[face], 1, lineType=cv2.LINE_8, shift=4)
    # Erode one texel so edge-rasterization conventions do not characterize misses.
    return cv2.erode(mask, np.ones((3, 3), np.uint8)).astype(np.bool_)


def hit_mask(evidence, name, width):
    packed = np.load(evidence / name, allow_pickle=False)
    if packed.dtype != np.uint8 or packed.shape != (width * width // 8,):
        raise RuntimeError(f"invalid bake witness mask: {name}")
    return np.unpackbits(packed).reshape(width, width)[::-1].astype(np.bool_)


def coverage(folder, used, width):
    evidence = folder / "evidence"
    direct = hit_mask(evidence, "bake-ray-direct.npy", width)
    extended = hit_mask(evidence, "bake-ray-extended.npy", width)
    if (direct & ~extended).any():
        raise RuntimeError("direct ray hits disappeared with the bake margin enabled")
    margin_only = extended & ~direct
    interiors = int(used.sum())
    if not interiors:
        raise RuntimeError("empty exported UV coverage")
    direct_inside = int((used & direct).sum())
    margin_inside = int((used & margin_only).sum())
    opacity = hit_mask(evidence, "bake-hits.npy", width)
    return {"atlas_direct_hit_texels": int(direct.sum()), "atlas_margin_extended_only_texels": int(margin_only.sum()),
            "occupied_interior_texels": interiors, "interior_direct_hit_texels": direct_inside,
            "interior_margin_extended_only_texels": margin_inside, "interior_uncovered_texels": interiors - direct_inside - margin_inside,
            "interior_direct_ray_coverage": direct_inside / interiors,
            "interior_coverage_after_margin": (direct_inside + margin_inside) / interiors,
            "interior_opacity_transfer_witness_texels_after_margin": int((used & opacity).sum())}


def verify_asset(folder, *, require_channel_variation=False):
    path = folder / "studio.glb"
    doc, data = glb(path)
    meshes = doc["meshes"]
    if len(meshes) != 1 or len(meshes[0]["primitives"]) != 1:
        raise RuntimeError("expected one mesh and one primitive")
    primitive = meshes[0]["primitives"][0]
    if primitive.get("mode", 4) != 4:
        raise RuntimeError("studio GLB primitive is not triangles")
    material = doc["materials"][primitive["material"]]
    pbr = material["pbrMetallicRoughness"]
    attrs = primitive["attributes"]
    pos = array(doc, data, attrs["POSITION"])
    uv = array(doc, data, attrs["TEXCOORD_0"])
    tangent = array(doc, data, attrs["TANGENT"])
    normal = array(doc, data, attrs["NORMAL"])
    flat = array(doc, data, primitive["indices"]).ravel()
    if flat.dtype.kind != "u" or flat.size % 3 or not 0 < flat.size // 3 <= 32000:
        raise RuntimeError("invalid triangle count")
    if (len(uv) != len(pos) or len(normal) != len(pos) or len(tangent) != len(pos)
            or tuple(x.shape[1] for x in (pos, uv, normal, tangent)) != (3, 2, 3, 4)
            or flat.max() >= len(pos) or not all(np.isfinite(x).all() for x in (pos, uv, normal, tangent))
            or (uv < 0).any() or (uv > 1).any()):
        raise RuntimeError("invalid UV, normal, tangent, position or index data")
    tri = flat.reshape(-1, 3)
    components = welded_shells(pos, tri)
    vertices = uv[tri]
    a, b = vertices[:, 1] - vertices[:, 0], vertices[:, 2] - vertices[:, 0]
    if np.any(np.abs(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]) <= 1.0e-12):
        raise RuntimeError("collapsed UV triangle")
    bake = json.loads((folder / "evidence/bake.json").read_text())
    width = bake["image_size_px"]
    if type(width) is not int or width not in (64, 128, 256, 512, 1024, 2048, 4096):
        raise RuntimeError("unexpected bake resolution")
    maps = {"base-color.png": pbr["baseColorTexture"], "metallic-roughness.png": pbr["metallicRoughnessTexture"],
            "normal.png": material["normalTexture"]}
    textures = {}
    for name, entry in maps.items():
        bits = embedded(doc, data, entry["index"])
        with Image.open(io.BytesIO(bits)) as image, Image.open(folder / name) as copy:
            rgba = np.asarray(image.convert("RGBA"))
            local = np.asarray(copy.convert("RGBA"))
            if (image.size != (width, width) or not np.array_equal(local[:, :, :3], rgba[:, :, :3])
                    or (name == "base-color.png" and not np.array_equal(local[:, :, 3], rgba[:, :, 3]))):
                raise RuntimeError(f"embedded {name} failed pixel parity")
        color = rgba[:, :, :3]
        channel_min = color.min(axis=(0, 1)).tolist()
        channel_max = color.max(axis=(0, 1)).tolist()
        active = (1, 2) if name == "metallic-roughness.png" else (0, 1, 2)
        if require_channel_variation and any(channel_min[i] == channel_max[i] for i in active):
            raise RuntimeError(f"constant mapped channel for {name}")
        textures[name] = {"sha256_glb_png": hashlib.sha256(bits).hexdigest(),
                          "pixel_size": [width, width], "min_rgb": channel_min, "max_rgb": channel_max}

    used = bake_uv_cells(uv, tri, width)
    return {"triangles": flat.size // 3, "position_accessor_count": len(pos), "glb_bytes": path.stat().st_size, "glb_sha256": sha(path),
            "component_vertices": components, "embedded_textures": textures,
            "coverage": coverage(folder, used, width)}
