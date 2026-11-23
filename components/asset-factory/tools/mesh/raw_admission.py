"""G02/G03 checks on untouched static GLB bytes and independently authorized intent.

Triangles are decoded on an exact binary32 lattice and compared in the factory
Z-up frame. Geometry is never repaired, normalized to its own bounds or deleted.
"""
import math
import struct
from fractions import Fraction
from itertools import product

from asset_factory import core
from asset_factory.domains import FRAME, UM_PER_METER, integers, pair
from tools.input.evidence import (InputIssue, bytes_digest, checked_record, closed, native_receipt,
                                  point, refuse, require)
from tools.mesh.contracts import MeshError
from tools.mesh.worker import _document

# Every finite binary32 position is an integer multiple of 2**-149 metres.
_LATTICE = 1 << 149
_AXES = ((1, 0, 0), (0, 1, 0), (0, 0, 1))
_IDENTITY = tuple(int(i % 5 == 0) for i in range(16))


def _cross(a, b):
    return tuple(a[i - 2] * b[i - 1] - a[i - 1] * b[i - 2] for i in range(3))


def _sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _bounds(points):
    return tuple(zip(*((min(axis), max(axis)) for axis in zip(*points))))


def _overlap(triangle, box):
    triangle_min, triangle_max = _bounds(triangle)
    low, high = box
    if any(b < c or d < a for a, b, c, d in zip(triangle_min, triangle_max, low, high)):
        return False
    edges = (_sub(triangle[1], triangle[0]), _sub(triangle[2], triangle[1]), _sub(triangle[0], triangle[2]))
    axes = [_cross(edges[0], edges[1])]
    axes.extend(_cross(edge, axis) for edge in edges for axis in _AXES)
    for axis in axes:
        if not any(axis):
            continue
        projections = [_dot(point, axis) for point in triangle]
        box_min = sum(min(v * a, v * b) for v, a, b in zip(axis, low, high))
        box_max = sum(max(v * a, v * b) for v, a, b in zip(axis, low, high))
        if max(projections) < box_min or box_max < min(projections):
            return False
    return True


def _point(x, y, z):
    require(all(map(math.isfinite, (x, y, z))), 2, "nonfinite_glb_position")
    # glTF +Z forward and +Y up, factory -Y forward and +Z up.
    return tuple(int(math.ldexp(value, 149)) * UM_PER_METER for value in (x, -z, y))


def _active_meshes(doc):
    scenes = doc.get("scenes", [])
    require(scenes and "scene" in doc, 3, "explicit_glb_scene_missing")
    nodes = doc.get("nodes", [])
    stack = list(scenes[doc["scene"]].get("nodes", []))
    seen, meshes = set(), set()
    while stack:
        index = stack.pop()
        require(index not in seen, 2, "aliased_or_cyclic_glb_node")
        seen.add(index)
        node = nodes[index]
        require(tuple(node.get("matrix", _IDENTITY)) == _IDENTITY
                and node.get("translation", [0, 0, 0]) == [0, 0, 0]
                and node.get("rotation", [0, 0, 0, 1]) == [0, 0, 0, 1]
                and node.get("scale", [1, 1, 1]) == [1, 1, 1],
                3, "nonidentity_glb_node_transform")
        if "mesh" in node:
            index = node["mesh"]
            require(index not in meshes, 3, "instanced_glb_mesh_unsupported")
            meshes.add(index)
        stack.extend(node.get("children", []))
    require(meshes, 3, "empty_glb_scene")
    return sorted(meshes)


def _accessor(doc, data, index, layout):
    accessor = doc["accessors"][index]
    view = doc["bufferViews"][accessor["bufferView"]]
    offset = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    unpacker = struct.Struct("<" + layout)
    stride = view.get("byteStride", unpacker.size)
    for i in range(accessor["count"]):
        yield unpacker.unpack_from(data, offset + stride * i)


def _primitives(doc, data, max_faces):
    layouts = {5121: "B", 5123: "H", 5125: "I"}
    remaining = max_faces
    for mesh_id in _active_meshes(doc):
        for i, prim in enumerate(doc["meshes"][mesh_id]["primitives"]):
            position = prim["attributes"]["POSITION"]
            vertex_count = doc["accessors"][position]["count"]
            indices = prim.get("indices")
            count = doc["accessors"][indices]["count"] if indices is not None else vertex_count
            require(count > 0 and count % 3 == 0, 2, "invalid_triangle_count")
            remaining -= count // 3
            require(remaining >= 0 and vertex_count <= 3 * max_faces, 6, "raw_face_or_vertex_budget")
            if indices is None:
                corners = range(vertex_count)
            else:
                index_format = layouts[doc["accessors"][indices]["componentType"]]
                corners = [row[0] for row in _accessor(doc, data, indices, index_format)]
            require(all(index < vertex_count for index in corners), 2, "invalid_triangle_index")
            points = [_point(*row) for row in _accessor(doc, data, position, "fff")]
            corners = iter(corners)
            yield f"{mesh_id}:{i}", ((points[a], points[b], points[c]) for a, b, c in zip(corners, corners, corners))


def _root(parent, point):
    while parent[point] != point:
        parent[point] = parent[parent[point]]
        point = parent[point]
    return point


def _component_bounds(parent):
    # Connect exact shared positions, including coincident glTF seam duplicates.
    components = {}
    for point in parent:
        root = _root(parent, point)
        if root not in components:
            components[root] = [list(point), list(point)]
        low, high = components[root]
        for axis, value in enumerate(point):
            low[axis], high[axis] = min(low[axis], value), max(high[axis], value)
    return list(components.values())


def _intent_geometry(intent):
    boxes = intent["forbidden_boxes_um"]
    require(isinstance(boxes, list) and boxes, 3, "forbidden_region_missing")
    lattice_boxes = []
    for box in boxes:
        require(type(box) is list and len(box) == 2, 2, "invalid_forbidden_region")
        low, high = (integers(point, 3) for point in box)
        require(all(a < b for a, b in zip(low, high)), 2, "invalid_forbidden_region")
        lattice_boxes.append(tuple(tuple(v * _LATTICE for v in point) for point in (low, high)))
    extents = intent["extent_ranges_um"]
    require(type(extents) is list and len(extents) == 3, 2, "invalid_intent_extents")
    extents = [integers(interval, 2) for interval in extents]
    require(all(0 <= low <= high and high > 0 for low, high in extents), 2, "invalid_intent_extents")
    require(any(low > 0 for low, _ in extents), 3, "physical_scale_witness_missing")
    return lattice_boxes, extents


def inspect_raw(raw, *, intent_digest, registration_digest, read_checked):
    """Verify source-bound roles, physical extents and forbidden support geometry."""
    subject = bytes_digest(raw)
    receipt = native_receipt("input.raw", subject, intent_digest)
    try:
        intent = checked_record(read_checked, "raw-intent", intent_digest)
        intent_fields = ("forbidden_boxes_um", "component_counts", "extent_ranges_um",
                         "max_glb_bytes", "max_faces", "max_box_checks")
        has_degenerate_budget = "max_degenerate_faces" in intent
        if has_degenerate_budget:
            intent_fields += ("max_degenerate_faces",)
        closed(intent, "af.raw-intent/v1", *intent_fields)
        registration = checked_record(read_checked, "raw-registration", registration_digest)
        closed(registration, "af.raw-registration/v1", "subject_digest", "intent_digest", "frame", "roles")
        require(registration["subject_digest"] == subject and registration["intent_digest"] == intent_digest
                and registration["frame"] == FRAME, 4, "raw_registration_drift")
        limits = [intent[key] for key in ("max_glb_bytes", "max_faces", "max_box_checks")]
        require(all(type(n) is int and n > 0 for n in limits), 2, "invalid_raw_budget")
        encoded, max_faces, max_checks = limits
        max_degenerate = intent.get("max_degenerate_faces")
        if has_degenerate_budget:
            require(type(max_degenerate) is int and 0 <= max_degenerate <= max_faces, 2,
                    "invalid_degenerate_face_budget")
        receipt["max_degenerate_faces"] = max_degenerate
        require(len(raw) <= encoded, 6, "raw_glb_byte_budget")
        boxes, extents = _intent_geometry(intent)
        require(len(boxes) * max_faces <= max_checks, 6, "raw_support_work_budget")

        expected = intent["component_counts"]
        require(type(expected) is dict and expected, 3, "component_intent_missing")
        for row in expected.values():
            require(type(row) is dict and row.keys() == {"role", "count"}
                    and type(row["role"]) is str and row["role"] and type(row["count"]) is int
                    and row["count"] >= 1, 2, "invalid_component_intent")
        require(registration["roles"] == {key: row["role"] for key, row in expected.items()},
                4, "raw_role_registration_drift")
        receipt.update(input_refs=[subject], reference_refs=[intent_digest, registration_digest],
                       frame=FRAME, registration_digest=registration_digest)

        doc = _document(raw)
        binary = memoryview(raw)[28 + struct.unpack_from("<I", raw, 12)[0]:]
        components, observed, area_by_primitive = [], {}, {}
        face_count = degenerate_count = 0
        degenerate_overlap = None
        for key, triangles in _primitives(doc, binary, max_faces):
            require(key in expected, 5, "undeclared_raw_component")
            parent, areas = {}, {}
            for triangle in triangles:
                face_count += 1
                a, b, c = triangle
                normal = _cross(_sub(b, a), _sub(c, a))
                degenerate = not any(normal)
                degenerate_count += degenerate
                for index, box in enumerate(boxes):
                    if not _overlap(triangle, box):
                        continue
                    witness = {"primitive": key, "face_ordinal": face_count - 1, "forbidden_box_ordinal": index}
                    if degenerate:
                        if degenerate_overlap is None:
                            degenerate_overlap = witness
                    else:
                        receipt["support_witness"] = witness
                        raise InputIssue(5, "forbidden_support_intersection")
                for p in triangle:
                    if p not in parent:
                        parent[p] = p
                        areas[p] = (0, 0)
                root = _root(parent, a)
                square = _dot(normal, normal)
                lower = math.isqrt(square)
                upper = lower + int(lower * lower != square)
                for other in {_root(parent, b), _root(parent, c)} - {root}:
                    area_lower, area_upper = areas.pop(other)
                    lower += area_lower
                    upper += area_upper
                    parent[other] = root
                area_lower, area_upper = areas[root]
                areas[root] = (area_lower + lower, area_upper + upper)
            connected = _component_bounds(parent)
            observed[key] = len(connected)
            area_by_primitive[key] = list(areas.values())
            components.extend(connected)

        receipt["work_counts"] = {"faces": face_count, "forbidden_box_checks": len(boxes) * face_count}
        receipt["metrics"]["degenerate_faces"] = point(degenerate_count)
        if degenerate_overlap is not None:
            receipt["degenerate_support_witness"] = degenerate_overlap
            raise InputIssue(3, "degenerate_forbidden_overlap_unresolved")
        if degenerate_count:
            require(has_degenerate_budget, 3, "degenerate_face_budget_missing")
            require(degenerate_count <= max_degenerate, 6, "degenerate_face_budget_exceeded")
        require(all(lo > 0 for areas in area_by_primitive.values() for lo, _ in areas),
                3, "raw_component_area_unresolved")
        require(observed == {key: row["count"] for key, row in expected.items()},
                5, "component_inventory_mismatch")
        extent_min, extent_max = _bounds(p for box in components for p in box)
        sizes = [Fraction(b - a, _LATTICE) for a, b in zip(extent_min, extent_max)]
        require(all(low <= size <= high for size, (low, high) in zip(sizes, extents)), 5,
                "extent_outside_intent")
        # A box entirely in a component AABB can lie inside an enclosed solid
        # without touching the surface. A surface check supplies no inside witness.
        for (lo, hi), (box_lo, box_hi) in product(components, boxes):
            require(not all(a <= b <= c <= d for a, b, c, d in zip(lo, box_lo, box_hi, hi)),
                    3, "forbidden_volume_containment_unresolved")

        receipt["component_counts"] = observed
        total_lower = sum(lo for values in area_by_primitive.values() for lo, _ in values)
        total_upper = sum(hi for values in area_by_primitive.values() for _, hi in values)
        receipt["component_area_shares"] = {
            primitive: [[pair(Fraction(lo, total_upper)), pair(min(1, Fraction(hi, total_lower)))]
                        for lo, hi in areas]
            for primitive, areas in area_by_primitive.items()}
        receipt["metrics"].update(component_count=point(len(components)),
                                  forbidden_support_intersections=point(0),
                                  **{"extent_" + axis: point(size) for axis, size in zip("xyz", sizes)})
        return receipt
    except MeshError as exc:
        return refuse(receipt, InputIssue(exc.rc, exc.code))
    except (ValueError, TypeError, KeyError, IndexError, struct.error, core.ContractError, MemoryError,
            RecursionError) as exc:
        return refuse(receipt, exc)
