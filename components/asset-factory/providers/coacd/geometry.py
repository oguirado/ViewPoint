"""Measured geometry gates. Sampling estimates are never labelled exact volume.

GLB decoding/export is delegated to trimesh; numerical work uses numpy. The
gate never repairs the visual source, normalises units, or welds by tolerance.
Only byte-identical positions are welded when testing edge incidence.
"""
import io
import json
import struct

from .contracts import MAX_BYTES, MAX_PAIRS, Refusal, require


def dependencies():
    try:
        import numpy as np
        import trimesh
    except ImportError as exc:
        raise Refusal(3, "geometry_runtime_unavailable") from exc
    return np, trimesh


def load_glb(data):
    np, trimesh = dependencies()
    require(0 < len(data) <= MAX_BYTES, 6, "input_budget_exceeded")
    require(len(data) >= 20 and data[:4] == b"glTF", 2, "glb_invalid")
    version, length, chunk_length, chunk_type = struct.unpack_from("<IIII", data, 4)
    require(version == 2 and length == len(data) and chunk_type == 0x4E4F534A
            and 20 + chunk_length <= len(data), 2, "glb_invalid")
    try:
        doc = json.loads(data[20:20 + chunk_length])
    except (ValueError, UnicodeError) as exc:
        raise Refusal(2, "glb_invalid") from exc
    def offline(node):
        if isinstance(node, dict):
            require("uri" not in node, 2, "external_resource_refused")
            for value in node.values():
                offline(value)
        elif isinstance(node, list):
            for value in node:
                offline(value)
    offline(doc)
    require(not doc.get("skins") and not doc.get("animations")
            and not doc.get("extensionsRequired"), 2, "unsupported_asset")
    for mesh in doc.get("meshes", []):
        for prim in mesh.get("primitives", []):
            require(prim.get("mode", 4) == 4 and not prim.get("targets"), 2, "unsupported_asset")
    try:
        scene = trimesh.load_scene(io.BytesIO(data), file_type="glb", process=False,
                                   skip_materials=True)
        parts = []
        for name in sorted(scene.graph.nodes_geometry):
            transform, geometry = scene.graph[name]
            mesh = scene.geometry[geometry].copy()
            mesh.apply_transform(transform)
            require(isinstance(mesh, trimesh.Trimesh), 2, "unsupported_asset")
            require(np.isfinite(mesh.vertices).all(), 2, "nonfinite_geometry")
            parts.append(mesh)
        require(bool(parts), 3, "empty_geometry")
        return parts
    except Refusal:
        raise
    except (ValueError, TypeError, KeyError, IndexError, struct.error) as exc:
        raise Refusal(2, "glb_invalid") from exc


def canonical_mesh(mesh):
    np, trimesh = dependencies()
    verts, faces = np.asarray(mesh.vertices), np.asarray(mesh.faces)
    require(verts.ndim == 2 and verts.shape[1] == 3 and len(verts) > 0
            and faces.ndim == 2 and faces.shape[1] == 3 and len(faces) > 0, 3, "empty_geometry")
    require(np.isfinite(verts).all() and np.issubdtype(faces.dtype, np.integer)
            and faces.min() >= 0 and faces.max() < len(verts), 2, "mesh_invalid")
    verts, inverse = np.unique(verts, axis=0, return_inverse=True)
    return trimesh.Trimesh(vertices=verts, faces=inverse[faces], process=False)


def inventory(mesh):
    np, _ = dependencies()
    mesh = canonical_mesh(mesh)
    edges = mesh.edges_sorted
    _, counts = np.unique(edges, axis=0, return_counts=True)
    triangles = mesh.triangles
    degenerate = int(np.count_nonzero(np.linalg.norm(np.cross(
        triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]), axis=1) == 0))
    return mesh, {"vertices": len(mesh.vertices), "faces": len(mesh.faces),
                  "boundary_edges": int(np.count_nonzero(counts == 1)),
                  "nonmanifold_edges": int(np.count_nonzero(counts > 2)),
                  "degenerate_faces": degenerate, "closed": bool(mesh.is_watertight),
                  "winding_consistent": bool(mesh.is_winding_consistent),
                  "positive_volume": bool(mesh.volume > 0),
                  "components": int(mesh.body_count)}


def convex_planes(mesh, tolerance_um):
    np, _ = dependencies()
    triangles = mesh.triangles
    normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    norms = np.linalg.norm(normals, axis=1)
    require(bool((norms > 0).all()), 5, "degenerate_hull")
    normals /= norms[:, None]
    offsets = np.einsum("ij,ij->i", normals, triangles[:, 0])
    require(len(mesh.vertices) * len(triangles) <= MAX_PAIRS, 6, "convexity_budget_exceeded")
    tolerance = tolerance_um / 1000000
    for start in range(0, len(mesh.vertices), 128):
        require(not np.any(mesh.vertices[start:start + 128] @ normals.T - offsets > tolerance),
                5, "hull_nonconvex")
    return normals, offsets


def contains_source(meshes, points):
    """Oriented solid-angle occupancy, unioned across source mesh instances.

    Preconditions are closed, consistently wound source shells. The work cap
    fails closed rather than downsampling the source or silently dropping faces.
    """
    np, _ = dependencies()
    require(len(points) * sum(len(m.faces) for m in meshes) <= MAX_PAIRS,
            6, "volume_budget_exceeded")
    occupied = np.zeros(len(points), dtype=bool)
    for mesh in meshes:
        tri = mesh.triangles
        for start in range(0, len(points), 64):
            q = points[start:start + 64, None, :]
            a, b, c = tri[None, :, 0] - q, tri[None, :, 1] - q, tri[None, :, 2] - q
            la, lb, lc = (np.linalg.norm(v, axis=2) for v in (a, b, c))
            numerator = np.einsum("ijk,ijk->ij", a, np.cross(b, c))
            denominator = (la * lb * lc + np.einsum("ijk,ijk->ij", a, b) * lc
                           + np.einsum("ijk,ijk->ij", b, c) * la
                           + np.einsum("ijk,ijk->ij", c, a) * lb)
            winding = np.sum(2 * np.arctan2(numerator, denominator), axis=1)
            occupied[start:start + 64] |= np.abs(winding) > 2 * np.pi
    return occupied


def contains_hulls(planes, points):
    np, _ = dependencies()
    require(len(points) * sum(len(n) for n, _ in planes) <= MAX_PAIRS,
            6, "volume_budget_exceeded")
    occupied = np.zeros(len(points), dtype=bool)
    for normals, offsets in planes:
        for start in range(0, len(points), 128):
            occupied[start:start + 128] |= np.all(
                points[start:start + 128] @ normals.T <= offsets, axis=1)
    return occupied


def segment_hits_mesh(mesh, segment):
    """Delegate triangle/ray intersection to trimesh, without an rtree dependency."""
    np, trimesh = dependencies()
    start, stop = np.array(segment, dtype=float) / 1000000
    direction = stop - start
    length = np.linalg.norm(direction)
    direction /= length
    # Broad-phase all source triangles deliberately: bounded by caller face cap.
    class AllTriangles:
        bounds = mesh.bounds.reshape(-1)
        def intersection(self, _bounds):
            return range(len(mesh.faces))
    _, _, locations = trimesh.ray.ray_triangle.ray_triangle_id(
        mesh.triangles, np.array([start]), np.array([direction]),
        tree=AllTriangles(), multiple_hits=True)
    return bool(len(locations) and np.any(np.linalg.norm(locations - start, axis=1) <= length))


def segment_hits_hull(planes, segment):
    """Clip a finite segment against convex supporting half-spaces, inclusive."""
    np, _ = dependencies()
    start, stop = np.array(segment, dtype=float) / 1000000
    normals, offsets = planes
    low, high = 0.0, 1.0
    for distance, rate in zip(offsets - normals @ start, normals @ (stop - start)):
        if rate == 0:
            if distance < 0:
                return False
        elif rate > 0:
            high = min(high, distance / rate)
        else:
            low = max(low, distance / rate)
        if low > high:
            return False
    return True


def exact_surface_identity(source_parts, hull_parts):
    """Prove equal solids only for identical surfaces and exactly convex hulls.

    Coordinates are the decoded world-space binary values. Integer lifting
    makes supporting-plane predicates exact, without the gate's plane tolerance.
    This deliberately refuses changed triangulations and approximate matches.
    Sampling remains diagnostic and acquires no claimed uncertainty bound.
    """
    def surface(parts):
        return sorted(tuple(sorted(tuple(float(x) for x in v) for v in tri))
                      for mesh in parts for tri in mesh.triangles)
    if surface(source_parts) != surface(hull_parts):
        return False
    for mesh in hull_parts:
        require(len(mesh.vertices) * len(mesh.faces) <= MAX_PAIRS,
                6, "convexity_budget_exceeded")
        ratios = [[float(x).as_integer_ratio() for x in v] for v in mesh.vertices]
        scale = max(d for v in ratios for _, d in v)
        vertices = [[n * (scale // d) for n, d in v] for v in ratios]
        for face in mesh.faces:
            a, b, c = [vertices[int(i)] for i in face]
            u, v = [b[i]-a[i] for i in range(3)], [c[i]-a[i] for i in range(3)]
            normal = [u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2],
                      u[0]*v[1]-u[1]*v[0]]
            if not any(normal) or any(sum(normal[i]*(p[i]-a[i]) for i in range(3)) > 0
                                      for p in vertices):
                return False
    return True


def evaluate(source_parts, hull_parts, policy):
    """Returns measured G11 data; raises an exact refusal code on unseen geometry."""
    np, _ = dependencies()
    require(bool(hull_parts) and len(hull_parts) <= policy["max_hulls"], 5, "hull_count_exceeded")
    sources, source_stats = [], []
    for mesh in source_parts:
        mesh, stats = inventory(mesh)
        sources.append(mesh)
        source_stats.append(stats)
    require(all(s["closed"] and s["winding_consistent"] and s["positive_volume"]
                and s["components"] == 1
                and not s["degenerate_faces"] for s in source_stats), 3, "source_volume_unavailable")
    stats, planes = [], []
    for hull in hull_parts:
        hull, row = inventory(hull)
        require(row["closed"] and row["winding_consistent"] and row["positive_volume"]
                and row["components"] == 1 and not row["degenerate_faces"], 5, "hull_not_closed_solid")
        require(row["faces"] <= policy["max_faces_per_hull"], 5, "hull_face_budget_exceeded")
        planes.append(convex_planes(hull, policy["plane_tolerance_um"]))
        stats.append(row)
    require(bool(policy["clearance_segments_um"]), 3, "clearance_unmeasured")
    for segment in policy["clearance_segments_um"]:
        require(not any(segment_hits_mesh(m, segment) for m in sources), 2, "probe_crosses_source")
        endpoints = np.array(segment, dtype=float) / 1000000
        require(not contains_source(sources, endpoints).any(), 2, "probe_inside_source")
        require(not any(segment_hits_hull(p, segment) for p in planes), 5, "clearance_blocked")
    # Sample the union AABB so displacement and excess volume are also visible.
    bounds = np.vstack([m.bounds for m in sources + hull_parts])
    low, high = bounds.min(axis=0), bounds.max(axis=0)
    require(bool(np.all(high > low)), 3, "zero_extent_geometry")
    points = np.random.default_rng(policy["sampling_seed"]).uniform(
        low, high, size=(policy["sample_count"], 3))
    visual = contains_source(sources, points)
    collision = contains_hulls(planes, points)
    seen = int(visual.sum())
    require(seen > 0, 3, "source_sample_empty")
    lost, extra = int((visual & ~collision).sum()), int((~visual & collision).sum())
    # Ceiling prevents rounding a real threshold violation down to the bound.
    loss_ppm = (lost * 1000000 + seen - 1) // seen
    extra_ppm = (extra * 1000000 + seen - 1) // seen
    metrics = {"hull_count": len(hull_parts), "sample_count": len(points),
               "source_samples": seen, "lost_samples": lost, "extra_samples": extra,
               "lost_volume_ppm": loss_ppm, "extra_volume_ppm": extra_ppm,
               "clearance_probes": len(policy["clearance_segments_um"]),
               "clearance_hits": 0, "volume_method": "seeded_uniform_aabb_winding_union",
               "volume_uncertainty_ppm": None}
    failures = []
    if loss_ppm > policy["max_lost_volume_ppm"]:
        failures.append("lost_volume_exceeded")
    if extra_ppm > policy["max_extra_volume_ppm"]:
        failures.append("extra_volume_exceeded")
    return metrics, stats, failures
