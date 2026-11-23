"""Local GLB reconstruction and exact rigid contact on the micrometre lattice."""
from collections import Counter
from fractions import Fraction
from itertools import combinations
import math
import struct

from asset_factory.domains import PPM, UM_PER_METER
from tools.blender.glb_io import decode_glb
from tools.blender.helpers_contracts import GlbError

from .fixture import AssemblyError


PLANAR_PROFILE = "planar-surface-seams"
FLIP_X = ((1, 0, 0), (0, -1, 0), (0, 0, -1))
MESH_TRIANGLE_LIMIT = 200_000
PATCH_TRIANGLE_LIMIT = 128


def oriented_triangles(vertices, faces):
    triangles = Counter()
    for face in faces:
        a, b, c = (tuple(vertices[index]) for index in face)
        triangles[min((a, b, c), (b, c, a), (c, a, b))] += 1
    return triangles


def _accessor(doc, data, index, kind, formats):
    acc = doc["accessors"][index]
    fmt = formats.get(acc["componentType"])
    if acc["type"] != kind or fmt is None or acc.get("normalized", False):
        raise AssemblyError("unsupported_part_accessor", 3)
    if acc["count"] > MESH_TRIANGLE_LIMIT * 3:
        raise AssemblyError("part_triangle_budget", 6)
    view = doc["bufferViews"][acc["bufferView"]]
    unpacker = struct.Struct("<" + fmt)
    start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = view.get("byteStride", unpacker.size)
    return [unpacker.unpack_from(data, start + stride * i) for i in range(acc["count"])]


def decode_mesh(mesh, tolerance_nm):
    """Read the single static mesh format consumed by the Blender assembly worker."""
    try:
        doc, data = decode_glb(mesh)
    except GlbError as exc:
        raise AssemblyError("part_glb:" + exc.code, exc.rc) from exc
    if len(doc.get("nodes", [])) != 1 or len(doc.get("meshes", [])) != 1:
        raise AssemblyError("unsupported_part_mesh_scene", 3)
    node, = doc["nodes"]
    if (node.get("mesh") != 0 or node.get("children")
            or any(key in node for key in ("matrix", "translation", "rotation", "scale"))
            or len(doc.get("scenes", [])) != 1 or doc["scenes"][0].get("nodes") != [0]):
        raise AssemblyError("unsupported_part_mesh_scene", 3)

    vertices, faces, residual = [], [], 0
    for primitive in doc["meshes"][0]["primitives"]:
        points = _accessor(doc, data, primitive["attributes"]["POSITION"], "VEC3", {5126: "fff"})
        indices = ([i for i, in _accessor(doc, data, primitive["indices"], "SCALAR",
                                         {5121: "B", 5123: "H", 5125: "I"})]
                   if "indices" in primitive else range(len(points)))
        if len(indices) % 3 or any(i >= len(points) for i in indices):
            raise AssemblyError("invalid_part_triangle_indices", 2)
        if len(faces) + len(indices) // 3 > MESH_TRIANGLE_LIMIT:
            raise AssemblyError("part_triangle_budget", 6)
        mapped = {}
        for index in indices:
            if index in mapped:
                continue
            x, y, z = points[index]
            point = tuple(value * UM_PER_METER for value in (x, -z, y))
            if not all(math.isfinite(value) for value in point):
                raise AssemblyError("nonfinite_part_position", 2)
            snapped = tuple(round(value) for value in point)
            residual = max(residual, *(math.ceil(abs(value - integer) * 1000)
                                       for value, integer in zip(point, snapped)))
            mapped[index] = len(vertices)
            vertices.append(snapped)
        faces.extend(tuple(mapped[j] for j in indices[i:i + 3]) for i in range(0, len(indices), 3))
    if not faces or residual > tolerance_nm:
        raise AssemblyError("part_coordinate_residual")
    return vertices, faces, residual


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def transpose(matrix):
    return tuple(zip(*matrix))


def multiply(a, b):
    return tuple(tuple(dot(row, col) for col in transpose(b)) for row in a)


def offset(a, b):
    return tuple(x - y for x, y in zip(a, b))


def transform_point(matrix, translation, point):
    return tuple(dot(row, point) + t for row, t in zip(matrix, translation))


def frame(transform):
    if transform["uniform_scale_ppm"] != PPM:
        raise AssemblyError("physical_scale")
    x, y, z, w = transform["rotation_xyzw_ppm"]
    n = x*x + y*y + z*z + w*w
    if n == 0 or abs(n - PPM**2) > 4 * PPM:
        raise AssemblyError("invalid_rigid_rotation", 2)
    # Division by the squared norm gives an exact proper rotation for the encoded quaternion.
    matrix = ((n - 2*(y*y + z*z), 2*(x*y - z*w), 2*(x*z + y*w)),
              (2*(x*y + z*w), n - 2*(x*x + z*z), 2*(y*z - x*w)),
              (2*(x*z - y*w), 2*(y*z + x*w), n - 2*(x*x + y*y)))
    return tuple(tuple(Fraction(value, n) for value in row) for row in matrix), tuple(transform["translation_um"])


def compose(outer, inner):
    matrix, translation = outer
    return multiply(matrix, inner[0]), transform_point(matrix, translation, inner[1])


def bounds(vertices):
    axes = tuple(zip(*vertices))
    return tuple(map(min, axes)), tuple(map(max, axes))


def _cross(a, b, p):
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def _area2(poly):
    return sum(a[0]*b[1] - a[1]*b[0] for a, b in zip(poly, poly[1:] + poly[:1]))


def _clip(poly, clipper):
    """Intersect convex polygons against the left half-plane of every clip edge."""
    for a, b in zip(clipper, clipper[1:] + clipper[:1]):
        result = []
        for p, q in zip(poly, poly[1:] + poly[:1]):
            u, v = _cross(a, b, p), _cross(a, b, q)
            if u >= 0:
                result.append(p)
            if (u < 0 < v) or (v < 0 < u):
                result.append(tuple(x + (y - x) * u / (u - v) for x, y in zip(p, q)))
        poly = result
        if not poly:
            break
    return poly


def _patch_area(vertices, faces, socket, dims):
    rotation, origin = socket
    inverse = transpose(rotation)
    in_socket = [transform_point(inverse, (0, 0, 0), offset(point, origin)) for point in vertices]
    if any(point[2] > 0 for point in in_socket):
        raise AssemblyError("socket_half_space_unproven", 3)
    x, y = (Fraction(d, 2) for d in dims)
    rectangle = [(-x, -y), (x, -y), (x, y), (-x, y)]
    pieces, total = [], 0
    for face in faces:
        points = [in_socket[i] for i in face]
        if any(point[2] != 0 for point in points):
            continue
        planar = [point[:2] for point in points]
        area = _area2(planar)
        if area < 0 and _area2(_clip(list(reversed(planar)), rectangle)):
            raise AssemblyError("socket_face_orientation")
        clipped = _clip(planar, rectangle)
        area = _area2(clipped)
        if area <= 0:
            continue
        if len(pieces) == PATCH_TRIANGLE_LIMIT:
            raise AssemblyError("socket_patch_triangle_budget", 6)
        if any(_area2(_clip(clipped, other)) > 0 for other in pieces):
            raise AssemblyError("socket_patch_self_overlap")
        pieces.append(clipped)
        total += area
    return total / 2


def check_mating(parent, child, mating, parent_part, child_part, profile):
    """Certify a full rectangular patch in both actual triangle meshes."""
    width, height, depth = parent["mating_dimensions_um"]
    if (profile != {"type": "surface_seam", "dimensions_um": [width, height, depth],
                    "orientation": "keyed-opposed-x"}
            or depth != 1
            or child["mating_dimensions_um"] != [width, height, depth]
            or parent["profile_digest"] != mating["profile_sha256"]
            or child["profile_digest"] != mating["profile_sha256"]
            or parent["mating_type"] != "surface_seam" or child["mating_type"] != "surface_seam"
            or parent["polarity"] != "receptacle" or child["polarity"] != "plug"
            or parent["semantic_role"] not in child["allowed_peer_roles"]
            or child["semantic_role"] not in parent["allowed_peer_roles"]):
        raise AssemblyError("incompatible_socket_profile")
    wp = compose(parent_part["frame"], frame(parent["frame"]))
    wc = compose(child_part["frame"], frame(child["frame"]))
    if multiply(wp[0], FLIP_X) != wc[0]:
        raise AssemblyError("socket_frame_orientation")
    if wp[1] != wc[1]:
        raise AssemblyError("socket_gap")
    wanted = width * height
    for part, socket in ((parent_part, wp), (child_part, wc)):
        if _patch_area(part["world"], part["faces"], socket, (width, height)) != wanted:
            raise AssemblyError("socket_patch_overlap")
    return {"projected_overlap_lower_um2": wanted, "projected_overlap_upper_um2": wanted,
            "axis_gap_um": 0, "frame_anchor_error_um": 0, "frame_matrix_error": 0}


def check_separation(parts, mated_pairs):
    boxes = {name: bounds(part["world"]) for name, part in parts.items()}
    for a, b in combinations(boxes, 2):
        if frozenset((a, b)) in mated_pairs:
            continue  # Their socket planes already separated the entire meshes.
        (lo, hi), (other_lo, other_hi) = boxes[a], boxes[b]
        if all(min(x, y) > max(p, q) for x, y, p, q in zip(hi, other_hi, lo, other_lo)):
            raise AssemblyError("non_mating_separation_unproven", 3)
    return bounds([point for box in boxes.values() for point in box])
