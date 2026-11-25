"""Independent byte reader and bounded topology measurement for static fixture GLBs."""

from collections import Counter, defaultdict
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
import struct
import sys


MAX_ELEMENTS = 48
MAX_BYTES = 65536


class Refusal(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Refusal(reason)


def encoded(document):
    return (json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def digest(data):
    return sha256(data).hexdigest()


def mesh(path, validate_glb):
    return decode_mesh(read_mesh(path), validate_glb)


def read_mesh(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    require(len(raw) <= MAX_BYTES, "GLB_BUDGET")
    return raw


def decode_mesh(raw, validate_glb):
    doc = validate_glb(raw)
    require(doc.keys() == {"asset", "scene", "scenes", "nodes", "meshes", "buffers", "bufferViews", "accessors"}
            and doc["scene"] == 0 and doc["scenes"] == [{"nodes": [0]}]
            and doc["nodes"] == [{"mesh": 0}] and len(doc["meshes"]) == 1
            and doc["meshes"][0] == {"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "mode": 4}]}
            and len(doc["accessors"]) == len(doc["bufferViews"]) == 2, "GLB_PROFILE")
    positions, indices = doc["accessors"]
    first, second = doc["bufferViews"]
    vertex_count, index_count = positions["count"], indices["count"]
    position_bytes, index_bytes = 12 * vertex_count, 2 * index_count
    require(3 <= vertex_count <= MAX_ELEMENTS and 3 <= index_count <= 3 * MAX_ELEMENTS and not index_count % 3
            and positions == {"bufferView": 0, "componentType": 5126, "count": vertex_count, "type": "VEC3",
                              "min": positions.get("min"), "max": positions.get("max")}
            and indices == {"bufferView": 1, "componentType": 5123, "count": index_count, "type": "SCALAR"}
            and first == {"buffer": 0, "byteOffset": 0, "byteLength": position_bytes, "target": 34962}
            and second == {"buffer": 0, "byteOffset": position_bytes, "byteLength": index_bytes, "target": 34963}
            and doc["buffers"] == [{"byteLength": position_bytes + index_bytes}], "GLB_ACCESSOR_PROFILE")
    asset = doc["asset"]
    require(asset == {"version": "2.0", "extras": {"vp3d_parent_sha256": asset.get("extras", {}).get("vp3d_parent_sha256")}},
            "GLB_LINEAGE_PROFILE")
    parent = asset["extras"]["vp3d_parent_sha256"]
    require(parent is None or (isinstance(parent, str) and len(parent) == 64
                               and all(symbol in "0123456789abcdef" for symbol in parent)), "GLB_LINEAGE_PROFILE")
    offset = 28 + struct.unpack_from("<I", raw, 12)[0]
    vertices = list(struct.iter_unpack("<3f", raw[offset:offset + position_bytes]))
    require(all(abs(axis) <= 100 for vertex in vertices for axis in vertex), "GLB_POSITION_RANGE")
    scaled = [tuple(Fraction(axis) * 1000000 for axis in vertex) for vertex in vertices]
    require(all(axis.denominator == 1 for point in scaled for axis in point), "GLB_MICROMETER_LATTICE")
    points = [tuple(map(int, vertex)) for vertex in scaled]
    require(len(set(points)) == len(points), "GLB_UNWELDED_VERTICES")
    require(positions["min"] == [min(axis) for axis in zip(*vertices)]
            and positions["max"] == [max(axis) for axis in zip(*vertices)], "GLB_POSITION_BOUNDS")
    faces = list(struct.iter_unpack("<3H", raw[offset + position_bytes:offset + position_bytes + index_bytes]))
    require(all(index < vertex_count for face in faces for index in face), "GLB_INDEX_RANGE")
    return {"sha256": digest(raw), "parent_sha256": parent, "vertices_um": points, "faces": faces}


def topology(model, geometry):
    faces, vertices = model["faces"], model["vertices_um"]
    edge_uses, edge_orientation = Counter(), Counter()
    graph = defaultdict(set)
    for face in faces:
        for start, end in geometry.cycle(face):
            edge = tuple(sorted((start, end)))
            edge_uses[edge] += 1
            edge_orientation[edge] += 1 if start < end else -1
            graph[start].add(end)
            graph[end].add(start)
    remaining = set(graph)
    components = 0
    while remaining:
        frontier = [remaining.pop()]
        components += 1
        while frontier:
            neighbors = graph[frontier.pop()] & remaining
            remaining.difference_update(neighbors)
            frontier.extend(neighbors)
    return {
        "vertices": len(vertices), "triangles": len(faces), "components": components,
        "boundary_edges": sum(uses == 1 for uses in edge_uses.values()),
        "nonmanifold_edges": sum(uses > 2 for uses in edge_uses.values()),
        "orientation_conflicts": sum(uses == 2 and edge_orientation[edge] != 0 for edge, uses in edge_uses.items()),
        "duplicate_faces": len(faces) - len({tuple(sorted(face)) for face in faces}),
        "degenerate_faces": sum(not any(geometry.normal(tuple(vertices[index] for index in face))) for face in faces),
        "unreferenced_vertices": len(vertices) - len(graph),
        "bounds_um": [[min(axis) for axis in zip(*vertices)], [max(axis) for axis in zip(*vertices)]],
    }


def clean(report):
    return report["components"] == 1 and not any(report[key] for key in (
        "nonmanifold_edges", "orientation_conflicts", "duplicate_faces", "degenerate_faces", "unreferenced_vertices"))


def triangles(model):
    return [tuple(model["vertices_um"][index] for index in face) for face in model["faces"]]


def directed(source, target, geometry):
    samples = {point for face in source for point in face}
    for face in source:
        samples.add(tuple(Fraction(sum(axis), 3) for axis in zip(*face)))
        samples.update(tuple(Fraction(a_axis + b_axis, 2) for a_axis, b_axis in zip(start, end))
                       for start, end in geometry.cycle(face))
    squared = max(min(geometry.triangle_d2(point, face) for face in target) for point in samples)
    lower, upper = geometry.sqrt_bounds(squared)
    return {"samples": len(samples), "squared_um2": [squared.numerator, squared.denominator],
            "interval_um": [[item.numerator, item.denominator] for item in (lower, upper)]}


def certify(paths, policy, geometry, validate_glb):
    models = [mesh(path, validate_glb) for path in paths]
    reports = [topology(model, geometry) for model in models]
    if len(models) == 1:
        return {"mesh": models[0], "topology": reports[0]}
    require(models[0]["sha256"] == policy["base_revision_sha256"], "WRONG_ANCESTOR")
    require(all(clean(report) for report in reports[1:]) and not reports[0]["degenerate_faces"], "TOPOLOGY_NOT_CLEAN")
    limit = policy["max_adjacent_um"]
    pairs = []
    for index, (before, after) in enumerate(zip(models, models[1:])):
        require(after["parent_sha256"] == before["sha256"], "WRONG_ANCESTOR")
        require(reports[index + 1]["triangles"] <= reports[index]["triangles"], "LOD_TRIANGLE_INCREASE")
        source, target = triangles(before), triangles(after)
        forward, reverse = directed(source, target, geometry), directed(target, source, geometry)
        pairs.append({"from": index, "to": index + 1, "forward": forward, "reverse": reverse})
    failures = [{"from": pair["from"], "to": pair["to"]} for pair in pairs if any(
        Fraction(*pair[direction]["squared_um2"]) > limit**2 for direction in ("forward", "reverse"))]
    return {"schema": "vp3d.topology_fixture_certificate/v1", "mode": "offline_fixture",
            "perception": "none", "distance_kind": "sampled_bidirectional_point_to_triangle",
            "max_adjacent_um": limit, "subjects": [model["sha256"] for model in models],
            "topology": reports, "adjacent_pairs": pairs, "failed_pairs": failures}


def main():
    factory, *names = sys.argv[1:]
    sys.path.insert(0, factory)
    from tools.mesh import certificate as geometry
    from tools.mesh.contracts import MeshError
    from tools.mesh.worker import _document

    try:
        report = certify([Path(name) for name in names], json.load(sys.stdin), geometry, _document)
    except Refusal as exc:
        sys.stdout.buffer.write(encoded({"reason": str(exc)}))
        return 3
    except (MeshError, AttributeError, KeyError, ValueError) as exc:
        sys.stdout.buffer.write(encoded({"reason": getattr(exc, "code", "GLB_PROFILE")}))
        return 3
    sys.stdout.buffer.write(encoded(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
